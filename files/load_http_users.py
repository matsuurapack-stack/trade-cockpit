"""N人が同時にアプリを開いた状態のHTTP応答性能を、稼働中サーバー(8765)に対して計測する（Release Gate STEP6）。

使い方（files/ で実行。サーバーが起動していること）：
  python load_http_users.py 1 120     # 1人・120秒
  python load_http_users.py 6 120     # 6人同時・120秒
一時アカウント(e2etmp_load*)を作り、実際のログイン→ページ読み込み直後の連続リクエスト→
フロントの実ポーリング間隔（÷3で圧縮＝実運用の約3倍の負荷）で継続、を各ユーザーが並行して行う。
計測：応答時間p50/p95、HTTP 5xx・エラー数、サーバーログ内の p_errno=6 / p_errno=2 の件数、
Tachibana FastQuote要求数（[FastQuote]ログ）。終了時に一時アカウントを削除する。結果は load_http_N.json。
"""
import collections
import json
import os
import sys
import threading
import time

import auth_core
import investment_db
import server
import e2e_multiuser_privacy as e

N = int(sys.argv[1]) if len(sys.argv) > 1 else 1
DURATION = int(sys.argv[2]) if len(sys.argv) > 2 else 120
PORT = int(os.environ.get("LOAD_PORT", "8765"))
LOG = os.environ.get("SERVER_LOG", r"C:/Users/MATSUURA/AppData/Local/Temp/claude/qf1split/server_8765b.log")
DB = server.DATABASE_URL
WL = [{"code": c, "market": "JP", "name": "n" + c} for c in ("6758", "7203", "8035", "9984", "6857", "4063")]

# (名前, メソッド, パス, body, 実運用の間隔秒)  ※間隔÷3で圧縮
BURST = [("me", "GET", "/api/auth/me", None), ("watchlist", "GET", "/api/watchlist", None),
         ("portfolio", "GET", "/api/portfolio", None), ("events", "GET", "/api/market-events", None),
         ("morning", "GET", "/api/morning-check", None), ("discovery", "GET", "/api/market-discovery", None),
         ("entry_live", "GET", "/api/entry-candidates/live", None), ("quotes_fast", "POST", "/api/stock-quotes/fast", WL),
         ("quotes", "POST", "/api/stock-quotes", WL), ("review_latest", "GET", "/api/daily-review/latest", None)]
POLL = [("quotes_fast", "POST", "/api/stock-quotes/fast", WL, 30), ("entry_live", "GET", "/api/entry-candidates/live", None, 30),
        ("discovery", "GET", "/api/market-discovery", None, 30), ("review_latest", "GET", "/api/daily-review/latest", None, 60),
        ("portfolio", "GET", "/api/portfolio", None, 90), ("quotes", "POST", "/api/stock-quotes", WL, 90)]

names = ["e2etmp_load%d" % i for i in range(1, N + 1)]
pw = "Load-Pass-" + str(os.getpid())
for n in names:
    investment_db.auth_upsert_user(DB, n, auth_core.hash_password(pw))


def log_counts():
    try:
        t = open(LOG, "rb").read().decode("utf-8", "replace")
    except Exception:
        return {"p_errno6": 0, "p_errno2": 0, "fastquote_lines": 0}
    return {"p_errno6": t.count("p_errno=6"), "p_errno2": t.count("p_errno=2"), "fastquote_lines": t.count("[FastQuote]")}


before = log_counts()
REC = []  # (endpoint, ms, status)
LOCK = threading.Lock()
stop_at = time.time() + DURATION


def one_req(c, name, method, path, body):
    t = time.time()
    try:
        st = c.req(method, path, body, timeout=90)[0]
    except Exception as ex:
        st = "ERR:" + type(ex).__name__
    with LOCK:
        REC.append((name, round((time.time() - t) * 1000), st))


def user_loop(n):
    c = e.Client(PORT)
    st, js, _ = c.login(n, pw)
    if st != 200:
        with LOCK:
            REC.append(("login", 0, st))
        return
    for name, m, p, b in BURST:
        one_req(c, name, m, p, b)
    next_due = {name: time.time() + iv / 3.0 for name, m, p, b, iv in POLL}
    while time.time() < stop_at:
        now = time.time()
        for name, m, p, b, iv in POLL:
            if now >= next_due[name]:
                one_req(c, name, m, p, b)
                next_due[name] = time.time() + iv / 3.0
        time.sleep(0.5)


try:
    ths = [threading.Thread(target=user_loop, args=(n,), daemon=True) for n in names]
    t0 = time.time()
    [t.start() for t in ths]
    [t.join(timeout=DURATION + 240) for t in ths]
    elapsed = time.time() - t0
finally:
    pool = investment_db._get_pool(DB)
    with pool.connection() as conn:
        conn.execute("DELETE FROM app_sessions WHERE username LIKE 'e2etmp_load%'")
        conn.execute("DELETE FROM app_users WHERE username LIKE 'e2etmp_load%'")
        conn.commit()
after = log_counts()


def pct(v, p):
    v = sorted(v)
    return v[min(len(v) - 1, int(len(v) * p))] if v else None


lat = [ms for _, ms, st in REC if st == 200]
by = collections.defaultdict(list)
for name, ms, st in REC:
    by[name].append(ms)
res = {
    "users": N, "duration_s": round(elapsed), "requests": len(REC),
    "http_5xx": sum(1 for _, _, st in REC if isinstance(st, int) and 500 <= st < 600),
    "errors_non200": collections.Counter(str(st) for _, _, st in REC if st != 200).most_common(6),
    "latency_ms": {"p50": pct(lat, 0.5), "p95": pct(lat, 0.95), "max": max(lat) if lat else None},
    "per_endpoint_p50_p95": {k: [pct(v, 0.5), pct(v, 0.95), len(v)] for k, v in by.items()},
    "requests_per_user_per_min": round(len(REC) / max(N, 1) / (elapsed / 60.0), 1),
    "server_log_delta": {k: after[k] - before[k] for k in after},
}
with open("load_http_%d.json" % N, "w", encoding="utf-8") as f:
    json.dump(res, f, ensure_ascii=False, indent=2)
print(json.dumps(res, ensure_ascii=False))
