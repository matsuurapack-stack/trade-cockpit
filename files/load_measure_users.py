"""N人が同時にアプリを開いた状態の外部API呼び出し数を計測する（2026-09-26 MU-Multi Release Gate STEP5）。

使い方（files/ で実行。キャッシュを毎回空にするため、Nごとに別プロセスで実行）：
  python load_measure_users.py 1
  python load_measure_users.py 6
結果: load_measure_N.json

安全策：
  - 立花証券API：本番のログインセッションを壊さないよう、tachibana_api._http.request と _ensure_session を
    偽物に差し替える（実通信しない。リクエスト数とCLMIDだけを計測し、もっともらしい応答を返す）。
  - DB：investment_db をスタブに差し替える（読み取りは固定データ、書き込みは全て無視・記録のみ）。
    本番DB・共有テーブルには一切書き込まない。DB呼び出し回数は関数名別に記録する。
  - yfinance／RSS(feedparser)／urllib：実際の通信を通しつつ回数を数える。
"""
import collections
import json
import os
import sys
import threading
import time

N = int(sys.argv[1]) if len(sys.argv) > 1 else 1
threading.Timer(900, lambda: os._exit(3)).start()  # 保険：15分で強制終了

import tachibana_api
import server

COUNT = collections.Counter()
LOCK = threading.Lock()
CURRENT = {"workload": "setup"}
PER_WORKLOAD = collections.defaultdict(collections.Counter)


SITES = collections.Counter()

import concurrent.futures as _cf
_TL = threading.local()
_orig_submit = _cf.ThreadPoolExecutor.submit


def _submit(self, fn, *a, **kw):
    import traceback
    site = " > ".join(os.path.basename(f.filename) + ":" + f.name for f in traceback.extract_stack()[:-1]
                      if "site-packages" not in f.filename and "threading.py" not in f.filename and "concurrent" not in f.filename)[-200:]

    def run(*aa, **kk):
        _TL.site = site
        return fn(*aa, **kk)
    return _orig_submit(self, run, *a, **kw)


_cf.ThreadPoolExecutor.submit = _submit


def bump(kind, sub=None):
    if kind == "yfinance" and CURRENT["workload"] == "top5_scan":
        import traceback
        names = [os.path.basename(f.filename) + ":" + f.name for f in traceback.extract_stack() if "site-packages" not in f.filename and "threading.py" not in f.filename and "concurrent" not in f.filename]
        with LOCK:
            SITES[(getattr(_TL, "site", "") or " > ".join(names[-3:]))[-160:]] += 1
    with LOCK:
        COUNT[kind] += 1
        PER_WORKLOAD[CURRENT["workload"]][kind] += 1
        if sub:
            COUNT[kind + ":" + sub] += 1


# ---------- 立花：偽物（実通信なし・リクエスト数のみ計測） ----------
class _FakeResp:
    def __init__(self, data):
        self.data = data


def _fake_request(self_or_method, *a, **kw):
    # _SerializedHttp.request(method, url, body=...) のbody(JSON)からsCLMIDを読む
    body = kw.get("body")
    clm = "unknown"
    try:
        payload = json.loads(body.decode("utf-8")) if body else {}
        clm = payload.get("sCLMID", "unknown")
    except Exception:
        payload = {}
    bump("tachibana", clm)
    if clm == "CLMMfdsGetMarketPrice":
        codes = [c for c in (payload.get("sTargetIssueCode") or "").split(",") if c]
        rows = [{"sIssueCode": c, "pDPP": "1000", "pPRP": "990", "pDOP": "995", "pDHP": "1010", "pDLP": "990",
                 "pDYWP": "10", "pDYRP": "1.0", "pDV": "500000", "pQAP": "1001", "pQBP": "999", "pVWAP": "1000"} for c in codes]
        out = {"p_errno": "0", "aCLMMfdsMarketPrice": rows}
    elif clm == "CLMMfdsGetMarketPriceHistory":
        import datetime as _dt
        d0, rows = _dt.date.today() - _dt.timedelta(days=430), []
        for i in range(300):
            d = d0 + _dt.timedelta(days=i)
            if d.weekday() >= 5:
                continue
            base = 1000 + (i % 40) * 3
            rows.append({"sDate": d.strftime("%Y%m%d"), "pDOPxK": str(base), "pDHPxK": str(base + 8), "pDLPxK": str(base - 8),
                         "pDPPxK": str(base + 2), "pDVxK": str(400000 + i * 10)})
        out = {"p_errno": "0", "aCLMMfdsMarketPriceHistory": rows}
    else:
        out = {"p_errno": "0"}
    return _FakeResp(json.dumps(out).encode("shift_jis"))


tachibana_api._http.request = _fake_request
tachibana_api._ensure_session = lambda use_prod=True, force=False: {
    "sUrlPrice": "http://fake/price", "sUrlRequest": "http://fake/request", "sUrlMaster": "http://fake/master",
    "sUrlEvent": "http://fake/event", "p_errno": "0"}
tachibana_api.login = lambda use_prod=False: {"p_errno": "0", "sUrlPrice": "http://fake/price", "sUrlRequest": "http://fake/request",
                                              "sUrlMaster": "http://fake/master", "sUrlEvent": "http://fake/event"}

# ---------- yfinance / feedparser / urllib：実通信を数える ----------
try:
    import yfinance.data as _yd
    for _n in ("get", "post"):
        _orig = getattr(_yd.YfData, _n)

        def _wrap(orig, name):
            def f(self, *a, **kw):
                bump("yfinance", name)
                return orig(self, *a, **kw)
            return f
        setattr(_yd.YfData, _n, _wrap(_orig, _n))
except Exception as e:
    print("yfinance計測フック失敗", e)
try:
    import feedparser as _fp
    _orig_parse = _fp.parse

    def _parse(*a, **kw):
        bump("rss")
        return _orig_parse(*a, **kw)
    _fp.parse = _parse
    server.feedparser = _fp
except Exception as e:
    print("feedparser計測フック失敗", e)
import urllib.request as _ur
_orig_urlopen = _ur.urlopen


def _urlopen(*a, **kw):
    bump("urllib_urlopen")
    return _orig_urlopen(*a, **kw)


_ur.urlopen = _urlopen


# ---------- DBスタブ ----------
WATCH = [{"code": c, "market": "JP", "name": "銘柄" + c, "watch": "優先", "manualRegistered": True, "isWatchTarget": True,
          "sector": "電気機器", "tvSymbol": "TSE:" + c} for c in ("6758", "7203", "8035", "9984", "6857", "4063")]
DB_CALLS = collections.Counter()


class StubDB:
    _SHARED_SCOPE = "_shared"
    _LEGACY_OWNER = "matsuura"

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)

        def f(*a, **kw):
            with LOCK:
                DB_CALLS[name] += 1
            if name == "list_watchlist":
                return [dict(w) for w in WATCH]
            if name.startswith("list_") or name.startswith("find_") or name.startswith("search_"):
                return []
            if name.startswith("load_"):
                return {}
            return None
        return f


server.investment_db = StubDB()
server.DATABASE_URL = "stub://db"
server.WRITE_E2E_ALLOWED = False

WL = [{"code": w["code"], "market": "JP", "name": w["name"]} for w in WATCH]
users = ["e2etmp_l%d" % i for i in range(1, N + 1)]
LAT = collections.defaultdict(list)
ERR = collections.defaultdict(list)


def run_workload(name, fn):
    CURRENT["workload"] = name
    threads = []

    def one(u):
        t = time.time()
        try:
            fn(u)
        except Exception as e:
            with LOCK:
                ERR[name].append(type(e).__name__ + ":" + str(e)[:80])
        with LOCK:
            LAT[name].append(round((time.time() - t) * 1000))
    t0 = time.time()
    for u in users:
        th = threading.Thread(target=one, args=(u,), daemon=True)
        threads.append(th); th.start()
    for th in threads:
        th.join(timeout=240)
    print("workload %-10s done in %.1fs  counts=%s" % (name, time.time() - t0, dict(PER_WORKLOAD[name])), flush=True)


workloads = [
    ("quotes_fast", lambda u: server.get_fast_quotes(WL)),
    ("quotes", lambda u: server.get_stock_quotes(WL)),
    ("top5_scan", lambda u: server._run_entry_top5_scan("stub://db", u, wait_for_lock=True)),
    ("news", lambda u: server.build_stock_news(WL)),
    ("catalyst", lambda u: server.catalyst_api_payload(u)),
    ("radar", lambda u: server.refresh_shadow_movement("stub://db", u)),
    ("chart", lambda u: [server.get_position_intraday_chart(c, "JP", "5m") for c in ("6758", "7203")]),
    ("discovery", lambda u: server.discovery_api_payload(u)),
]
for name, fn in workloads:
    try:
        run_workload(name, fn)
    except Exception as e:
        print("workload", name, "失敗", e, flush=True)

res = {
    "users": N,
    "external_calls_total": {k: v for k, v in COUNT.items() if ":" not in k},
    "external_calls_detail": {k: v for k, v in COUNT.items() if ":" in k},
    "per_workload": {w: dict(c) for w, c in PER_WORKLOAD.items()},
    "latency_ms": {w: {"n": len(v), "p50": sorted(v)[len(v) // 2] if v else None, "max": max(v) if v else None} for w, v in LAT.items()},
    "errors": {w: sorted(set(v))[:3] for w, v in ERR.items()},
    "yfinance_sites_top5": SITES.most_common(8),
    "db_function_calls_total": sum(DB_CALLS.values()),
    "db_function_calls_top": DB_CALLS.most_common(8),
}
with open("load_measure_%d.json" % N, "w", encoding="utf-8") as f:
    json.dump(res, f, ensure_ascii=False, indent=2)
print("DONE", json.dumps(res["external_calls_total"]), flush=True)
os._exit(0)
