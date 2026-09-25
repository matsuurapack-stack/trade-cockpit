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
    def __init__(self, fetchers, now_fn=None, ttl_sec=600, cooldown_sec=120, max_queue=200, on_snapshot=None, bdays_fn=None, async_regulation=False):
        self.f = fetchers
        self.now_fn = now_fn or (lambda: datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))))
        self.ttl, self.cooldown, self.max_queue = ttl_sec, cooldown_sec, max_queue
        self.on_snapshot = on_snapshot
        self.bdays_fn = bdays_fn or ce.business_days_between
        self.cache = {}          # code -> {"snap","at","reason"}
        self.pending = {}        # code -> {"reason","queued_at","anomaly"}
        self.last_request = {}   # code -> epoch
        self.q = queue.Queue()
        self.bg = queue.Queue()        # 後追い更新（規制・古いTDnet）。本体とは別ワーカー
        self.async_regulation = async_regulation
        self._bg_thread = None
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
    def _timed(self, name, fn, code, lookup, errors, timing, tkey=None):
        """1つの情報源を取得（失敗は他に影響しない）。所要時間を timing[tkey or name] に記録。失敗時は None。"""
        t0 = time.time()
        rows = None
        try:
            rows = fn(code) or []
            lookup[name] = True
        except Exception as e:
            lookup[name] = False
            errors.append(f"{name}:{str(e)[:120]}")
            with self.lock:
                self.stats["source_failures"][name] += 1
        timing[tkey or name] = round((time.time() - t0) * 1000)
        return rows

    def _items_from(self, rows_by_source, now):
        seen = {}
        for name, rows in rows_by_source.items():
            for r in rows or []:
                it = ce.build_item(r.get("title"), r.get("source") or {"tdnet": "TDNET", "news": "NEWS", "db": "DB_UNVERIFIED"}[name],
                                   r.get("published_at"), now, url=r.get("url"), bdays_fn=self.bdays_fn, verified=r.get("verified"))
                key = (it["title"] or "").strip()
                if key not in seen or ce.CONFIDENCE_ORDER.index(it["confidence"]) < ce.CONFIDENCE_ORDER.index(seen[key]["confidence"]):
                    seen[key] = it            # 同じ見出しは信頼度の高い情報源を採用
        return list(seen.values())

    def _build(self, code, ctx, now):
        snap = ce.build_snapshot(code, self._items_from(ctx["rows"], now), now, earnings_next=ctx["nxt"], calendar_known=bool(ctx["lookup"].get("calendar")),
                                 margin=ctx["margin"], lookup=dict(ctx["lookup"]), anomaly=ctx["anomaly"], bdays_fn=self.bdays_fn)
        snap["trigger"] = ctx["reason"]
        snap["errors"] = list(ctx["errors"])
        snap["timing_ms"] = dict(ctx["timing"])
        snap["duration_ms"] = ctx["timing"].get("total_ms")
        snap["regulation_state"] = ctx.get("regulation_state")
        snap["tdnet_depth"] = ctx.get("tdnet_depth")
        return snap

    def run_one(self, code):
        """Catalyst本体（TDnet・立花ニュース・材料DB・決算日）だけで先にSnapshotを確定する（4情報源は並列取得）。
        規制情報は本体のクリティカルパスに入れない（async_regulation=Trueなら別ワーカーで後追い：REGULATION_PENDING）。
        TDnetは直近（当日〜前営業日）を先に確定し、古い分は後追いで足す（tdnet_recentがある場合）。"""
        with self.lock:
            p = self.pending.get(code) or {"reason": "MANUAL", "anomaly": None}
        t0 = time.time()
        now = self.now_fn()
        lookup, errors, timing = {}, [], {}
        staged = bool(self.f.get("tdnet_recent")) and self.f.get("tdnet") is not None
        jobs = {"tdnet": self.f["tdnet_recent"] if staged else self.f.get("tdnet"), "news": self.f.get("news"), "db": self.f.get("db")}
        results = {}
        cal = {"nxt": None}

        def work(name):
            fn = jobs.get(name)
            if fn is None:
                lookup[name] = False
                timing[name] = 0
                results[name] = []
                return
            results[name] = self._timed(name, fn, code, lookup, errors, timing) or []

        def work_cal():
            fn = self.f.get("earnings_next")
            if fn is None:
                lookup["calendar"] = False
                timing["calendar"] = 0
                return
            t1 = time.time()
            try:
                cal["nxt"] = fn(code)
                lookup["calendar"] = True
            except Exception as e:
                lookup["calendar"] = False
                errors.append(f"calendar:{str(e)[:120]}")
                with self.lock:
                    self.stats["source_failures"]["calendar"] += 1
            timing["calendar"] = round((time.time() - t1) * 1000)
        ths = [threading.Thread(target=work, args=(n,), daemon=True) for n in ("tdnet", "news", "db")] + [threading.Thread(target=work_cal, daemon=True)]
        for t in ths:
            t.start()
        for t in ths:
            t.join()
        rows = {n: results.get(n, []) for n in ("tdnet", "news", "db")}
        timing = {"tdnet_ms": timing.get("tdnet"), "tachibana_news_ms": timing.get("news"), "db_ms": timing.get("db"),
                  "earnings_ms": timing.get("calendar"), "regulation_ms": None, "total_ms": None}
        ctx = {"rows": rows, "nxt": cal["nxt"], "lookup": lookup, "errors": errors, "timing": timing, "reason": p.get("reason"),
               "anomaly": p.get("anomaly"), "margin": None, "regulation_state": None, "tdnet_depth": "RECENT" if staged else "FULL"}
        do_reg = bool(self.f.get("regulation"))
        if do_reg and self.async_regulation:
            ctx["regulation_state"] = "REGULATION_PENDING"
            lookup["regulation"] = None
        elif do_reg:
            self._do_regulation(code, ctx)
        else:
            lookup["regulation"] = False
        timing["total_ms"] = round((time.time() - t0) * 1000)
        snap = self._build(code, ctx, now)
        with self.lock:
            self.cache[code] = {"snap": snap, "at": time.time(), "reason": p.get("reason"), "ctx": ctx}
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
        if self.async_regulation and do_reg:
            self.bg.put(("regulation", code))
        if staged:
            self.bg.put(("deep", code))
        return snap

    def _do_regulation(self, code, ctx):
        t0 = time.time()
        try:
            flags, prev_active = self.f["regulation"](code)
            ctx["margin"] = ce.margin_restriction_from_flags(flags, prev_active)
            ctx["lookup"]["regulation"] = flags is not None
        except Exception as e:
            ctx["lookup"]["regulation"] = False
            ctx["errors"].append(f"regulation:{str(e)[:120]}")
            with self.lock:
                self.stats["source_failures"]["regulation"] += 1
        ctx["timing"]["regulation_ms"] = round((time.time() - t0) * 1000)
        ctx["regulation_state"] = "DONE" if ctx["lookup"]["regulation"] else "UNAVAILABLE"

    def _refresh(self, code, mutate, log_if=None):
        """後追い更新：キャッシュ済みctxに追加情報を反映してSnapshotを作り直す（cache置換のみ・ENTRY判定は待たせない）。"""
        with self.lock:
            c = self.cache.get(code)
        if c is None or "ctx" not in c:
            return
        ctx = c["ctx"]
        if not mutate(ctx):
            return
        snap = self._build(code, ctx, self.now_fn())
        with self.lock:
            cur = self.cache.get(code)
            if cur is not None and cur.get("ctx") is ctx:
                cur["snap"] = snap
        if self.on_snapshot and (log_if is None or log_if(ctx)):
            try:
                self.on_snapshot(code, snap, ctx["reason"])
            except Exception:
                pass

    def run_background_one(self, kind, code):
        if kind == "regulation":
            def m(ctx):
                self._do_regulation(code, ctx)
                return True
            self._refresh(code, m, log_if=lambda ctx: bool(ctx["lookup"].get("regulation")))   # 規制が判明した時だけ再記録
        elif kind == "deep":
            def m(ctx):
                tm = {}
                rows = self._timed("tdnet", self.f["tdnet"], code, ctx["lookup"], ctx["errors"], tm)
                ctx["timing"]["tdnet_full_ms"] = tm.get("tdnet")
                if rows is None:
                    ctx["lookup"]["tdnet"] = True          # 直近ぶんは取得済み。古い分だけ失敗
                    return False
                ctx["tdnet_depth"] = "FULL"
                changed = {r.get("title") for r in rows} != {r.get("title") for r in ctx["rows"]["tdnet"]}
                ctx["rows"]["tdnet"] = rows
                return changed
            self._refresh(code, m, log_if=lambda ctx: True)

    def drain_background(self, max_items=50):
        """テスト・手動用：後追い（規制・古いTDnet）を同期的に処理する。"""
        n = 0
        while n < max_items:
            try:
                kind, code = self.bg.get_nowait()
            except queue.Empty:
                break
            self.run_background_one(kind, code)
            n += 1
        return n

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

    def _bg_loop(self):
        while not self._stop:
            try:
                kind, code = self.bg.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self.run_background_one(kind, code)
            except Exception:
                with self.lock:
                    self.stats["failed"] += 1

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
        if self._bg_thread is None:
            self._bg_thread = threading.Thread(target=self._bg_loop, daemon=True)
            self._bg_thread.start()

    def stop(self):
        self._stop = True

    def summary(self):
        with self.lock:
            d = sorted(self.stats["durations_ms"])
            return {**{k: v for k, v in self.stats.items() if k != "durations_ms"}, "queue": self.q.qsize(), "pending": len(self.pending),
                    "cached": len(self.cache), "duration_ms_median": d[len(d) // 2] if d else None, "duration_ms_max": d[-1] if d else None}
