# Catalyst Lookup Service（Phase F・shadow）：価格の異変があった銘柄だけを非同期で調査し、結果をキャッシュする。
#   ・チャート判定（Phase C/D）をブロックしない：get()は常に即座に返る。調査中/未調査は state="PENDING"（CATALYST_PENDING）
#   ・銘柄ごとにクールダウン（同じ銘柄を連続で調べない）とTTL。キュー上限あり（溢れたら捨てる＝重い調査を常時走らせない）
#   ・情報源（TDnet・立花ニュース・登録済み材料DB・決算カレンダー・規制情報）は呼び出し側が注入する（既存の取得関数を再利用）。
#     各取得の失敗は他に影響しない（失敗した情報源は lookup[...]=False。UNEXPLAINED_MOVEは全部取れた時だけ判定）。

import datetime
import queue
import threading
import time

import catalyst_engine as ce

SOURCE_KEYS = ("tdnet", "news", "db")


class CatalystLookup:
    def __init__(self, fetchers, now_fn=None, ttl_sec=600, cooldown_sec=120, max_queue=200, on_snapshot=None, bdays_fn=None):
        self.f = fetchers
        self.now_fn = now_fn or (lambda: datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))))
        self.ttl, self.cooldown, self.max_queue = ttl_sec, cooldown_sec, max_queue
        self.on_snapshot = on_snapshot
        self.bdays_fn = bdays_fn or ce.business_days_between
        self.cache = {}          # code -> {"snap","at","reason"}
        self.pending = {}        # code -> {"reason","queued_at","anomaly"}
        self.last_request = {}   # code -> epoch
        self.q = queue.Queue()
        self.lock = threading.RLock()
        self.stats = {"requested": 0, "queued": 0, "dropped": 0, "cooldown": 0, "cache_hits": 0, "done": 0, "failed": 0,
                      "durations_ms": [], "source_failures": {k: 0 for k in SOURCE_KEYS + ("calendar", "regulation")}}
        self._thread = None
        self._stop = False

    # ---- 非ブロッキングAPI
    def request(self, code, reason, anomaly=None):
        """調査を依頼する（即座に返る）。戻り値: QUEUED / CACHED / COOLDOWN / DROPPED / INFLIGHT。"""
        now = time.time()
        with self.lock:
            self.stats["requested"] += 1
            if code in self.pending:
                return "INFLIGHT"
            c = self.cache.get(code)
            if c is not None and now - c["at"] <= self.ttl:
                self.stats["cache_hits"] += 1
                return "CACHED"
            if now - self.last_request.get(code, 0) < self.cooldown:
                self.stats["cooldown"] += 1
                return "COOLDOWN"
            if self.q.qsize() >= self.max_queue:
                self.stats["dropped"] += 1
                return "DROPPED"
            self.last_request[code] = now
            self.pending[code] = {"reason": reason, "queued_at": self.now_fn().isoformat(), "anomaly": anomaly}
            self.q.put(code)
            self.stats["queued"] += 1
            return "QUEUED"

    def get(self, code):
        """キャッシュ済みのスナップショット（古くても返す。stale=True）。調査中なら PENDING。未調査ならNone。"""
        with self.lock:
            c = self.cache.get(code)
            if c is not None:
                snap = dict(c["snap"])
                snap["stale"] = (time.time() - c["at"]) > self.ttl
                return snap
            p = self.pending.get(code)
            if p is not None:
                return {"code": code, "state": "PENDING", "reason": p["reason"], "queued_at": p["queued_at"]}
            return None

    # ---- 調査本体（ワーカースレッドまたはテストから同期実行）
    def _fetch(self, name, code, lookup, errors):
        fn = self.f.get(name)
        if fn is None:
            lookup[name] = False
            return []
        try:
            rows = fn(code) or []
            lookup[name] = True
            return rows
        except Exception as e:
            lookup[name] = False
            errors.append(f"{name}:{str(e)[:120]}")
            with self.lock:
                self.stats["source_failures"][name] += 1
            return []

    def run_one(self, code):
        with self.lock:
            p = self.pending.get(code) or {"reason": "MANUAL", "anomaly": None}
        t0 = time.time()
        now = self.now_fn()
        lookup, errors, items = {}, [], []
        seen = {}
        for name in SOURCE_KEYS:
            for r in self._fetch(name, code, lookup, errors):
                it = ce.build_item(r.get("title"), r.get("source") or {"tdnet": "TDNET", "news": "NEWS", "db": "DB_UNVERIFIED"}[name],
                                   r.get("published_at"), now, url=r.get("url"), bdays_fn=self.bdays_fn, verified=r.get("verified"))
                key = (it["title"] or "").strip()
                if key not in seen or ce.CONFIDENCE_ORDER.index(it["confidence"]) < ce.CONFIDENCE_ORDER.index(seen[key]["confidence"]):
                    seen[key] = it            # 同じ見出しは信頼度の高い情報源を採用
        items = list(seen.values())
        nxt = None
        try:
            if self.f.get("earnings_next"):
                nxt = self.f["earnings_next"](code)
                lookup["calendar"] = True
            else:
                lookup["calendar"] = False
        except Exception as e:
            lookup["calendar"] = False
            errors.append(f"calendar:{str(e)[:120]}")
            with self.lock:
                self.stats["source_failures"]["calendar"] += 1
        margin = None
        try:
            if self.f.get("regulation"):
                flags, prev_active = self.f["regulation"](code)
                margin = ce.margin_restriction_from_flags(flags, prev_active)
                lookup["regulation"] = flags is not None
            else:
                lookup["regulation"] = False
        except Exception as e:
            lookup["regulation"] = False
            errors.append(f"regulation:{str(e)[:120]}")
            with self.lock:
                self.stats["source_failures"]["regulation"] += 1
        snap = ce.build_snapshot(code, items, now, earnings_next=nxt, calendar_known=bool(lookup.get("calendar")), margin=margin,
                                 lookup=lookup, anomaly=p.get("anomaly"), bdays_fn=self.bdays_fn)
        snap["trigger"] = p.get("reason")
        snap["duration_ms"] = round((time.time() - t0) * 1000)
        snap["errors"] = errors
        with self.lock:
            self.cache[code] = {"snap": snap, "at": time.time(), "reason": p.get("reason")}
            self.pending.pop(code, None)
            self.stats["done"] += 1
            if errors and not any(lookup.get(k) for k in SOURCE_KEYS):
                self.stats["failed"] += 1
            self.stats["durations_ms"].append(snap["duration_ms"])
            del self.stats["durations_ms"][:-200]
        if self.on_snapshot:
            try:
                self.on_snapshot(code, snap, p.get("reason"))
            except Exception:
                pass
        return snap

    def drain(self, max_items=50):
        """テスト・手動用：キューを同期的に処理する。"""
        n = 0
        while n < max_items:
            try:
                code = self.q.get_nowait()
            except queue.Empty:
                break
            self.run_one(code)
            n += 1
        return n

    def _loop(self):
        while not self._stop:
            try:
                code = self.q.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self.run_one(code)
            except Exception:
                with self.lock:
                    self.pending.pop(code, None)
                    self.stats["failed"] += 1

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    def stop(self):
        self._stop = True

    def summary(self):
        with self.lock:
            d = sorted(self.stats["durations_ms"])
            return {**{k: v for k, v in self.stats.items() if k != "durations_ms"}, "queue": self.q.qsize(), "pending": len(self.pending),
                    "cached": len(self.cache), "duration_ms_median": d[len(d) // 2] if d else None, "duration_ms_max": d[-1] if d else None}
