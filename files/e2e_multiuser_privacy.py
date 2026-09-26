"""6ユーザー プライバシー/認証 E2E（実HTTP・実DB）。2026-09-26 MU-Multi。

管理者が「利用開始前の必須確認」として実行できるスクリプト。一時アカウント（e2etmp_*）6人を作り、
本物のHTTPハンドラに対して、ログイン→書き込み→読み取り→他人データへのアクセス試行までを行い、
1件でも他人のデータが返ったら失敗（終了コード1）にする。終了時に一時アカウントと一時データを
全て削除する（本物の利用者のデータには触れない：対象は user_id が 'e2etmp_' で始まる行と、
一時の共有行 'E2ESYS' のみ）。定時処理（scheduler）は起動しない（HTTPハンドラだけを起動）。

使い方（files/ で実行）：  python e2e_multiuser_privacy.py
結果は e2e_multiuser_result.txt（合否一覧）に保存される（個人データ・パスワードは書かない）。
"""
import datetime
import http.client
import json
import secrets
import sys
import threading

import auth_core
import investment_db
import server
from socketserver import ThreadingTCPServer

DB = server.DATABASE_URL
PREFIX = "e2etmp_"
NAMES = [PREFIX + n for n in ("owner", "user1", "user2", "user3", "user4", "user5")]
RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((bool(cond), name, detail))
    print(("PASS " if cond else "FAIL ") + name + ((" :: " + detail) if (detail and not cond) else ""))
    return bool(cond)


class Client:
    def __init__(self, port):
        self.port, self.cookie, self.csrf = port, None, None

    def req(self, method, path, body=None, csrf=True, headers=None, timeout=90):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        h = {"Host": "127.0.0.1:%d" % self.port}
        if self.cookie:
            h["Cookie"] = self.cookie
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            h["Content-Type"] = "application/json"
        if method != "GET" and csrf and self.csrf:
            h["X-CSRF-Token"] = self.csrf
        h.update(headers or {})
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        raw = r.read()
        hdrs = {k.lower(): v for k, v in r.getheaders()}
        cookies = [v for k, v in r.getheaders() if k.lower() == "set-cookie"]
        c.close()
        try:
            js = json.loads(raw.decode("utf-8"))
        except Exception:
            js = None
        return r.status, js, raw.decode("utf-8", "replace"), hdrs, cookies

    def login(self, username, password):
        st, js, _, _, cookies = self.req("POST", "/api/auth/login", {"username": username, "password": password}, csrf=False)
        if st == 200 and cookies:
            self.cookie = cookies[0].split(";")[0]
            self.csrf = js.get("csrfToken")
        return st, js, cookies


def cleanup():
    pool = investment_db._get_pool(DB)
    with pool.connection() as conn:
        tables = [r[0] for r in conn.execute(
            "SELECT DISTINCT table_name FROM information_schema.columns WHERE column_name='user_id' "
            "AND table_schema='public'").fetchall()]
        total = 0
        for t in tables:
            try:
                total += conn.execute(f'DELETE FROM "{t}" WHERE user_id LIKE %s', [PREFIX + "%"]).rowcount
            except Exception:
                conn.rollback()
        conn.execute("DELETE FROM watchlist WHERE user_id=%s AND code=%s", [investment_db._SHARED_SCOPE, "E2ESYS"])
        conn.execute("DELETE FROM app_sessions WHERE username LIKE %s", [PREFIX + "%"])
        conn.execute("DELETE FROM app_users WHERE username LIKE %s", [PREFIX + "%"])
        conn.commit()
    return total


def main():
    if server.AUTH_BYPASS:
        print("認証なしモード(AUTH_BYPASS)のため実行できません（アカウント/USERSを設定してください）"); return 2
    investment_db.init_schema(DB)
    cleanup()  # 前回の残骸があれば先に掃除
    pw = {n: secrets.token_urlsafe(12) + "aA1" for n in NAMES}
    for i, n in enumerate(NAMES):
        investment_db.auth_upsert_user(DB, n, auth_core.hash_password(pw[n]), role="owner" if i == 0 else "user")
    investment_db.auth_upsert_user(DB, PREFIX + "disabled", auth_core.hash_password("Disabled-pass-1"))
    investment_db.auth_set_enabled(DB, PREFIX + "disabled", False)

    httpd = ThreadingTCPServer(("127.0.0.1", 0), server.Handler)
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    ok_all = True
    try:
        # ---------- A. 認証・セッション・CSRF・CORS ----------
        anon = Client(port)
        st, js, _, _, _ = anon.req("GET", "/api/portfolio")
        check("A1 未ログインのAPIは401", st == 401 and (js or {}).get("error") == "login_required")
        st, _, _, h, _ = anon.req("GET", "/trade-cockpit.html")
        check("A2 未ログインの本体HTMLは/loginへ302", st == 302 and h.get("location") == "/login")
        st, _, raw, _, _ = anon.req("GET", "/login")
        check("A3 ログイン画面は未ログインで表示できる", st == 200 and "ログイン" in raw)
        st, _, _, _, _ = anon.req("POST", "/api/portfolio/save", {"code": "X"}, csrf=False)
        check("A4 未ログインの書き込みは401", st == 401)
        st, js, ck = Client(port).login(NAMES[1], "wrong-password")
        check("A5 誤パスワードは401（汎用メッセージ）", st == 401 and not ck)
        st, js, _, _, ck = Client(port).req("POST", "/api/auth/login", {"username": PREFIX + "disabled", "password": "Disabled-pass-1"}, csrf=False)
        check("A6 停止中アカウントはログイン不可", st == 401)
        st, _, _, _, _ = Client(port).req("POST", "/api/auth/login", {"username": NAMES[1], "password": pw[NAMES[1]]},
                                          csrf=False, headers={"Origin": "http://evil.example.com"})
        check("A7 他サイトOriginからのログインは403", st == 403)

        clients = {}
        for n in NAMES:
            c = Client(port)
            st, js, ck = c.login(n, pw[n])
            clients[n] = c
            ok = st == 200 and js and js.get("username") == n
            check("B0 ログイン成功 " + n, ok)
        st, js, ck = Client(port).login(NAMES[1], pw[NAMES[1]])
        cookie_line = ck[0] if ck else ""
        check("A8 CookieはHttpOnly+SameSite付き", "HttpOnly" in cookie_line and "SameSite=Lax" in cookie_line)
        c1 = clients[NAMES[1]]
        st, js, _, _, _ = c1.req("GET", "/api/auth/me")
        check("A9 /api/auth/meはログイン中ユーザー名を返す", st == 200 and js.get("username") == NAMES[1])
        st, _, _, _, _ = c1.req("POST", "/api/portfolio/save", {"code": "E2ECSRF", "name": "csrf"}, csrf=False)
        check("A10 CSRFトークン無しの書き込みは403", st == 403)
        st, _, _, _, _ = c1.req("POST", "/api/portfolio/save", {"code": "E2ECSRF", "name": "csrf"},
                                headers={"Origin": "http://evil.example.com"})
        check("A11 他サイトOriginの書き込みは403", st == 403)
        st, _, _, h, _ = c1.req("OPTIONS", "/api/portfolio/save")
        check("A12 CORS許可ヘッダを出さない(OPTIONS)", "access-control-allow-origin" not in h)
        st, _, _, h, _ = c1.req("GET", "/api/portfolio")
        check("A13 CORS許可ヘッダを出さない(GET)", "access-control-allow-origin" not in h)
        # 総当たり制限
        bf = Client(port)
        codes = [bf.req("POST", "/api/auth/login", {"username": NAMES[5], "password": "bad%d" % i}, csrf=False)[0] for i in range(6)]
        check("A14 連続失敗でロック(429)", 429 in codes, str(codes))
        st, _, _, _, _ = Client(port).req("POST", "/api/auth/login", {"username": NAMES[5], "password": pw[NAMES[5]]}, csrf=False)
        # 5番目のユーザーは別IP扱いにできないため、ロック中は正しいパスワードでも429になる想定
        check("A15 ロック中は正しいパスワードでも429", st == 429)
        auth_core_throttle = server._login_throttle
        auth_core_throttle.record_success(NAMES[5], "127.0.0.1")  # 以降のE2Eのためロック解除

        # ---------- B. 個人データ分離（6人） ----------
        exp_ids, refl_ids, markers = {}, {}, {}
        for i, n in enumerate(NAMES):
            m = "MARK_%s_%s" % (n[len(PREFIX):], secrets.token_hex(3))
            markers[n] = m
            c = clients[n]
            st, js, _, _, _ = c.req("POST", "/api/portfolio/save", {"code": "E2E%d" % i, "market": "JP", "name": m,
                                                                     "quantity": 100, "average_price": 1000 + i})
            check("B1 ポジション登録 " + n, st == 200 and js.get("ok") is True)
            st, js, _, _, _ = c.req("POST", "/api/trade-reflections/save", {"reflection_text": "反省 " + m, "lesson": m,
                                                                             "trade_date": "2026-09-01", "stock_code": "E2E%d" % i})
            refl = (js or {}).get("reflection") or (js or {}).get("item") or {}
            refl_ids[n] = refl.get("id")
            check("B2 反省メモ登録 " + n, st == 200 and refl_ids[n] is not None, str(js)[:120])
            st, js, _, _, _ = c.req("POST", "/api/trade-experiences", {"symbol": "E2E%d" % i, "side": "BUY", "entry_price": 1000,
                                                                        "exit_price": 990, "quantity": 100, "notes": m, "trade_date": "2026-09-01"})
            exp_ids[n] = ((js or {}).get("experience") or {}).get("id")
            check("B3 トレード分析(experience)登録 " + n, st == 200 and exp_ids[n] is not None, str(js)[:120])
            investment_db.generate_daily_review(DB, n, "2026-01-05", user_feedback="今日の振り返り " + m)
            investment_db.upsert_trade_rule_from_text(DB, n, "個人ルール " + m)

        from concurrent.futures import ThreadPoolExecutor
        endpoints = ["/api/portfolio", "/api/trade-reflections", "/api/trade-experiences", "/api/daily-review/list",
                     "/api/trade-rules", "/api/trade-history"]

        def read_all(n):
            """1人分：全個人APIを、通常＋user_id改ざん付きの2通りで読む。"""
            res = {}
            for ep in endpoints:
                for variant in (ep, ep + "?user_id=" + NAMES[1] + "&userId=" + NAMES[2]):
                    res[variant] = clients[n].req("GET", variant)[2]
            return res
        with ThreadPoolExecutor(max_workers=6) as ex:
            fetched = dict(zip(NAMES, ex.map(read_all, NAMES)))
        leaks = []
        for n in NAMES:
            others = [markers[o] for o in NAMES if o != n]
            for variant, raw in fetched[n].items():
                for om in others:
                    if om in raw:
                        leaks.append((n, variant, om))
            check("B4 自分のポジションが見える " + n, markers[n] in fetched[n]["/api/portfolio"])
            check("B5 自分の反省メモが見える " + n, markers[n] in fetched[n]["/api/trade-reflections"])
            check("B6 自分のトレード分析が見える " + n, markers[n] in fetched[n]["/api/trade-experiences"])
            check("B7 自分の今日の振り返りが見える " + n, markers[n] in fetched[n]["/api/daily-review/list"])
        check("B8 他人のデータが1件も返らない（全ユーザー×全個人API×user_id改ざん）", not leaks, str(leaks[:3]))

        # 重いAPI（決定レビュー計算を伴う日次レビュー詳細）は2人分だけ、長めのタイムアウトで確認
        def read_review(n):
            return n, clients[n].req("GET", "/api/daily-review?date=2026-01-05", timeout=240)[2]
        with ThreadPoolExecutor(max_workers=2) as ex:
            rv = dict(ex.map(read_review, [NAMES[0], NAMES[3]]))
        rv_ok = all(markers[n] in raw and all(markers[o] not in raw for o in NAMES if o != n) for n, raw in rv.items())
        check("B8b 日次レビュー詳細も本人分だけ（owner・user3で確認）", rv_ok)

        # 他人のIDを直接指定しても取得/変更/削除できない（各ユーザー→次のユーザーを攻撃）
        def attack(pair):
            a, b = pair
            ca = clients[a]
            found = []
            _, _, raw, _, _ = ca.req("GET", "/api/trade-experiences/%s" % exp_ids[b])
            if markers[b] in raw:
                found.append(("get-exp", a, b))
            ca.req("POST", "/api/trade-reflections/%s/update" % refl_ids[b], {"lesson": "HACKED_BY_" + a})
            ca.req("POST", "/api/portfolio/delete", {"code": "E2E%d" % NAMES.index(b), "market": "JP"})
            return found
        pairs = [(NAMES[i], NAMES[(i + 1) % len(NAMES)]) for i in range(len(NAMES))]
        idor = []
        with ThreadPoolExecutor(max_workers=6) as ex:
            for f in ex.map(attack, pairs):
                idor += f

        def verify(b):
            return b, clients[b].req("GET", "/api/trade-reflections")[2], clients[b].req("GET", "/api/portfolio")[2]
        with ThreadPoolExecutor(max_workers=6) as ex:
            for b, raw_r, raw_p in ex.map(verify, NAMES):
                if "HACKED_BY_" in raw_r:
                    idor.append(("update-reflection", b))
                if markers[b] not in raw_p:
                    idor.append(("portfolio-deleted-by-other", b))
        check("B9 他人のIDを直接指定しても取得・変更・削除できない", not idor, str(idor[:3]))

        # ---------- C. 共有データは全員同じ / 監視銘柄の system と user の分離 ----------
        shared_eps = ["/api/market-events"]
        for ep in shared_eps:
            bodies = {n: clients[n].req("GET", ep)[2] for n in NAMES}
            check("C1 共有API %s は6人とも同一" % ep, len(set(bodies.values())) == 1)
        pool = investment_db._get_pool(DB)
        with pool.connection() as conn:
            conn.execute("DELETE FROM watchlist WHERE user_id=%s AND code=%s", [investment_db._SHARED_SCOPE, "E2ESYS"])
            conn.execute("INSERT INTO watchlist (user_id, code, market, name, source) VALUES (%s,'E2ESYS','JP','E2E共通','auto_test')",
                         [investment_db._SHARED_SCOPE])
            conn.commit()
        c2 = clients[NAMES[2]]
        st, js, _, _, _ = c2.req("POST", "/api/watchlist/save", {"code": "E2EPRIV", "market": "JP", "name": "E2E個人", "source": "manual"})
        check("C2 個人の監視銘柄を追加できる", st == 200 and js.get("ok") is True)
        seen_sys, seen_priv = {}, {}
        for n in NAMES:
            _, js, raw, _, _ = clients[n].req("GET", "/api/watchlist")
            seen_sys[n] = "E2ESYS" in raw
            seen_priv[n] = "E2EPRIV" in raw
        check("C3 システム監視銘柄は6人全員に見える", all(seen_sys.values()), str(seen_sys))
        check("C4 個人追加の監視銘柄は追加した本人だけ", seen_priv[NAMES[2]] and not any(v for k, v in seen_priv.items() if k != NAMES[2]),
              str(seen_priv))

        # ---------- D. ログアウト ----------
        c3 = clients[NAMES[3]]
        old_cookie = c3.cookie
        st, js, _, _, ck = c3.req("POST", "/api/auth/logout", {})
        check("D1 ログアウトできる（Cookie削除指示）", st == 200 and ck and "Max-Age=0" in ck[0])
        stale = Client(port)
        stale.cookie = old_cookie
        st, _, _, _, _ = stale.req("GET", "/api/portfolio")
        check("D2 ログアウト後は古いCookieで見られない", st == 401)
        # パスワード変更で既存セッション失効・アカウント停止で即時遮断
        investment_db.auth_upsert_user(DB, NAMES[4], auth_core.hash_password("New-pass-12345"))
        st, _, _, _, _ = clients[NAMES[4]].req("GET", "/api/portfolio")
        check("D3 パスワード変更後は既存セッションが失効", st == 401)
        investment_db.auth_set_enabled(DB, NAMES[5], False)
        st, _, _, _, _ = clients[NAMES[5]].req("GET", "/api/portfolio")
        check("D4 アカウント停止で即時ログアウト", st == 401)
    finally:
        httpd.shutdown()
        removed = cleanup()
        with investment_db._get_pool(DB).connection() as conn:
            left = conn.execute("SELECT count(*) FROM app_users WHERE username LIKE %s", [PREFIX + "%"]).fetchone()[0]
        check("Z1 一時データを全て削除した", left == 0, "removed_rows=%d left_users=%d" % (removed, left))
    passed = sum(1 for r in RESULTS if r[0])
    ok_all = passed == len(RESULTS)
    lines = ["%s %s%s" % ("PASS" if ok else "FAIL", name, (" :: " + d) if (d and not ok) else "") for ok, name, d in RESULTS]
    lines.append("")
    lines.append("合計 %d / %d 件 PASS  → %s" % (passed, len(RESULTS), "全て合格" if ok_all else "不合格あり（完了扱いにしない）"))
    with open("e2e_multiuser_result.txt", "w", encoding="utf-8") as f:
        f.write("実行時刻: %s\n" % datetime.datetime.now().isoformat(timespec="seconds"))
        f.write("\n".join(lines) + "\n")
    print(lines[-1])
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
