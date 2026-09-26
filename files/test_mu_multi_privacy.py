# MU-Multi（2026-09-26）：6人利用向けの認証・個人データ分離・共有処理一回化の回帰テスト。
# 実DB・実HTTPを使う総合確認は e2e_multiuser_privacy.py（管理者が利用開始前に実行）。
# ここでは構造的な保証（新テーブルの分類強制・個人テーブルが共有固定になっていないこと・
# リクエストからuser_idを受け取らないこと・CORS/CSRF・パスワードハッシュ）を高速に検証する。
#
# 実行方法： cd files & python -m unittest test_mu_multi_privacy -v

import inspect
import re
import time
import unittest
from unittest import mock

import auth_core
import investment_db
import server


def _db_source():
    return inspect.getsource(investment_db)


def _tables_with_user_id():
    src = _db_source()
    out = set()
    for name, body in re.findall(r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\n\);", src, re.S):
        if re.search(r"^\s*user_id\s", body, re.M):
            out.add(name)
    return out


class PasswordHashTests(unittest.TestCase):
    def test_hash_is_not_plaintext_and_verifies(self):
        h = auth_core.hash_password("correct horse 123")
        self.assertNotIn("correct horse", h)
        self.assertTrue(h.startswith("pbkdf2_sha256$"))
        self.assertTrue(auth_core.verify_password("correct horse 123", h))

    def test_wrong_password_rejected(self):
        h = auth_core.hash_password("secret-pass-1")
        self.assertFalse(auth_core.verify_password("secret-pass-2", h))
        self.assertFalse(auth_core.verify_password("", h))

    def test_salt_is_unique_per_hash(self):
        self.assertNotEqual(auth_core.hash_password("same-pass-1"), auth_core.hash_password("same-pass-1"))

    def test_malformed_stored_hash_is_false_not_exception(self):
        for bad in (None, "", "plain", "a$b$c", "md5$1$x$y"):
            self.assertFalse(auth_core.verify_password("x", bad))

    def test_iterations_are_strong(self):
        self.assertGreaterEqual(auth_core.PBKDF2_ITERATIONS, 200_000)


class ValidationTests(unittest.TestCase):
    def test_username_rules(self):
        for ok in ("owner", "user1", "a.b-c_d", "matsuura"):
            self.assertTrue(auth_core.validate_username(ok)[0], ok)
        for bad in ("", "a", "_shared", "local", "system", "has space", "日本語", "x" * 40, "-lead"):
            self.assertFalse(auth_core.validate_username(bad)[0], bad)

    def test_password_min_length(self):
        self.assertFalse(auth_core.validate_password_strength("short")[0])
        self.assertTrue(auth_core.validate_password_strength("long-enough-1")[0])


class SessionTokenTests(unittest.TestCase):
    def test_tokens_random_and_digest_not_reversible_form(self):
        a, b = auth_core.new_session_token(), auth_core.new_session_token()
        self.assertNotEqual(a, b)
        self.assertNotEqual(auth_core.token_digest(a), a)
        self.assertEqual(len(auth_core.token_digest(a)), 64)

    def test_constant_time_equal(self):
        self.assertTrue(auth_core.constant_time_equal("abc", "abc"))
        self.assertFalse(auth_core.constant_time_equal("abc", "abd"))
        self.assertFalse(auth_core.constant_time_equal("", ""))
        self.assertFalse(auth_core.constant_time_equal(None, "x"))

    def test_session_expiry_is_sliding_but_capped(self):
        created = 1_000_000.0
        self.assertEqual(auth_core.session_expiry(created, created), created + auth_core.SESSION_IDLE_SECONDS)
        late = created + auth_core.SESSION_ABSOLUTE_SECONDS - 60
        self.assertEqual(auth_core.session_expiry(created, late), created + auth_core.SESSION_ABSOLUTE_SECONDS)

    def test_same_origin(self):
        self.assertTrue(auth_core.same_origin("http://localhost:8765", "localhost:8765"))
        self.assertTrue(auth_core.same_origin("https://app.example.com", "app.example.com"))
        self.assertFalse(auth_core.same_origin("http://evil.example.com", "localhost:8765"))
        self.assertFalse(auth_core.same_origin("null", "localhost:8765"))
        self.assertTrue(auth_core.same_origin(None, "localhost:8765"))  # Origin無し（同一オリジンのGET等）


class LoginThrottleTests(unittest.TestCase):
    def test_locks_after_max_failures_and_unlocks_after_time(self):
        t = auth_core.LoginThrottle(max_failures=3, lock_seconds=60, window=60)
        now = 1000.0
        for _ in range(3):
            t.record_failure("u", "1.1.1.1", now=now)
        self.assertTrue(t.is_locked("u", "1.1.1.1", now=now + 1))
        self.assertFalse(t.is_locked("u", "1.1.1.1", now=now + 61))
        self.assertFalse(t.is_locked("other", "1.1.1.1", now=now + 1))  # 他ユーザーは無関係

    def test_success_resets(self):
        t = auth_core.LoginThrottle(max_failures=3, lock_seconds=60, window=60)
        t.record_failure("u", "ip", now=1.0); t.record_failure("u", "ip", now=2.0)
        t.record_success("u", "ip")
        t.record_failure("u", "ip", now=3.0)
        self.assertFalse(t.is_locked("u", "ip", now=4.0))


class ScopeRegistryTests(unittest.TestCase):
    """user_id列を持つ全テーブルが個人/共有のどちらかに分類されていること（新テーブル追加時に分類を強制）。"""

    def test_registry_sets_are_disjoint(self):
        p, s, m = investment_db.PRIVATE_TABLES, investment_db.SHARED_TABLES, investment_db.MIXED_TABLES
        self.assertFalse(p & s); self.assertFalse(p & m); self.assertFalse(s & m)

    def test_every_user_id_table_is_classified(self):
        classified = investment_db.PRIVATE_TABLES | investment_db.SHARED_TABLES | investment_db.MIXED_TABLES
        unclassified = _tables_with_user_id() - classified
        self.assertEqual(unclassified, set(),
                         "user_id列を持つテーブルが未分類です。investment_db.PRIVATE_TABLES/SHARED_TABLES/"
                         "MIXED_TABLESのどれかへ追加してください（個人データを共有にしないための強制）: %s" % sorted(unclassified))

    def test_registry_has_no_unknown_tables(self):
        classified = investment_db.PRIVATE_TABLES | investment_db.SHARED_TABLES | investment_db.MIXED_TABLES
        self.assertEqual(classified - _tables_with_user_id(), set())

    def test_personal_tables_named_in_the_requirements_are_private(self):
        for t in ("portfolio", "trade_history", "trade_reflections", "trade_experiences", "daily_reviews",
                  "trade_rules", "trade_rule_history", "daily_log", "journal", "portfolio_cash_balance",
                  "position_risk_rules", "morning_market_check_private_overlay"):
            self.assertIn(t, investment_db.PRIVATE_TABLES, t)


class PrivateTablesNeverForcedSharedTests(unittest.TestCase):
    """個人テーブルを読み書きするDB関数が `_SHARED_SCOPE` 固定になっていないこと。"""

    # 共有テーブルと個人テーブルを1関数の中で併用する関数（個人側は本物のuser_idで扱う）。
    ALLOWED_MIXED_FUNCTIONS = {
        "cleanup_expired_auto_tags",  # 共有watchlistの掃除。保有中かの判定でportfolioのコード集合だけ参照（誰の保有かは返さない）
        "save_morning_check", "get_latest_morning_check", "list_morning_checks", "mark_morning_check_read",
        "_merge_morning_check_overlay",  # 共有コア(_shared)＋個人overlay(本人)を分けて扱う（MU-S3C）
        "record_trade_outcome_for_playbooks", "get_trade_playbook_user_stats", "list_trade_playbooks",  # 共有定義＋個人統計
        "_get_pool",  # 定数定義の行を誤検出
    }

    def test_no_function_touching_private_tables_forces_shared_scope(self):
        src = _db_source()
        funcs = re.split(r"\n(?=def )", src)
        violations = []
        for f in funcs:
            if not f.startswith("def "):
                continue
            fname = re.match(r"def (\w+)", f).group(1)
            if fname in self.ALLOWED_MIXED_FUNCTIONS:
                continue
            forces = bool(re.search(r"^\s*\w*user_id\s*=\s*_SHARED_SCOPE", f, re.M)) or bool(re.search(r"^\s*shared_id\s*=\s*_SHARED_SCOPE", f, re.M))
            if not forces:
                continue
            for t in investment_db.PRIVATE_TABLES:
                if re.search(r"(FROM|INTO|UPDATE|JOIN)\s+" + t + r"\b", f):
                    violations.append((fname, t))
        self.assertEqual(violations, [], "個人テーブルを_SHARED_SCOPE固定で扱う関数があります: %s" % violations[:5])


class NoRequestSuppliedUserIdTests(unittest.TestCase):
    """user_idはログインセッションからのみ決まる（URL/本文の値を信用しない）。"""

    def test_handler_source_never_reads_user_id_from_request(self):
        src = inspect.getsource(server.Handler)
        for pat in ('get("user_id"', "get('user_id'", 'get("userId"', '["user_id"]', "params.get(\"user\"",
                    'query.get("user'):
            self.assertNotIn(pat, src, pat)

    def test_current_user_is_set_from_session_only(self):
        src = inspect.getsource(server.Handler._authorized)
        self.assertIn('sess["username"]', src)
        self.assertNotIn("Authorization", src)  # Basic認証は廃止


class CorsAndCsrfTests(unittest.TestCase):
    def test_no_cors_allow_origin_header_is_sent_anywhere(self):
        src = inspect.getsource(server)
        self.assertNotIn('send_header("Access-Control-Allow-Origin"', src)

    def test_basic_auth_removed(self):
        src = inspect.getsource(server.Handler)
        self.assertNotIn("WWW-Authenticate", src)

    def test_do_post_checks_csrf_after_auth_and_before_routing(self):
        src = inspect.getsource(server.Handler.do_POST)
        self.assertLess(src.index("_authorized"), src.index("_csrf_ok"))
        self.assertLess(src.index("_csrf_ok"), src.index("WRITE_E2E_ALLOWED"))

    def _handler(self, headers, session=None):
        h = server.Handler.__new__(server.Handler)
        h.headers = headers
        h.session = session or {"csrf_token": "tok123", "username": "u1"}
        return h

    def test_csrf_ok_requires_matching_token(self):
        with mock.patch.object(server, "AUTH_BYPASS", False):
            self.assertTrue(self._handler({"X-CSRF-Token": "tok123", "Host": "h"})._csrf_ok())
            self.assertFalse(self._handler({"X-CSRF-Token": "wrong", "Host": "h"})._csrf_ok())
            self.assertFalse(self._handler({"Host": "h"})._csrf_ok())

    def test_csrf_rejects_foreign_origin_even_with_token(self):
        with mock.patch.object(server, "AUTH_BYPASS", False):
            hdr = {"X-CSRF-Token": "tok123", "Host": "app.local:8765", "Origin": "http://evil.example.com"}
            self.assertFalse(self._handler(hdr)._csrf_ok())

    def test_session_cookie_flags(self):
        h = self._handler({})
        with mock.patch.object(server, "IS_CLOUD", False):
            c = h._session_cookie_header("tok", 100)
            self.assertIn("HttpOnly", c); self.assertIn("SameSite=Lax", c); self.assertNotIn("Secure", c)
        with mock.patch.object(server, "IS_CLOUD", True):
            self.assertIn("Secure", h._session_cookie_header("tok", 100))  # HTTPS本番はSecure必須

    def test_unauthenticated_api_gets_401_and_page_redirects(self):
        for path, expect in (("/api/portfolio", 401), ("/api/x?user_id=other", 401)):
            h = self._handler({})
            h.path, h.command = path, "GET"
            sent = []
            h._load_session = lambda: None
            h._send_json = lambda obj, status=200, _s=sent: _s.append(status)
            self.assertFalse(h._authorized())
            self.assertEqual(sent, [expect])


class LegacyUsersMigrationTests(unittest.TestCase):
    def test_plaintext_users_are_hashed_first_is_owner_and_existing_untouched(self):
        with mock.patch.object(server, "USERS", {"matsuura": "plain-pass-1", "bad name": "x"}), \
             mock.patch.object(server, "investment_db") as db:
            db.auth_upsert_user.return_value = True
            migrated = server.migrate_legacy_users_to_db("dummy")
        self.assertEqual(migrated, ["matsuura"])
        args, kwargs = db.auth_upsert_user.call_args
        self.assertNotIn("plain-pass-1", args[2])
        self.assertTrue(args[2].startswith("pbkdf2_sha256$"))
        self.assertEqual(kwargs["role"], "owner")
        self.assertTrue(kwargs["create_only"])


class WatchlistScopeTests(unittest.TestCase):
    def _run_list(self, rows, user):
        cur = mock.MagicMock(); cur.__enter__.return_value = cur; cur.__exit__.return_value = False
        cur.fetchall.return_value = rows
        conn = mock.MagicMock(); conn.__enter__.return_value = mock.Mock(cursor=mock.Mock(return_value=cur)); conn.__exit__.return_value = False
        pool = mock.Mock(); pool.connection.return_value = conn
        with mock.patch.object(investment_db, "_get_pool", return_value=pool):
            return investment_db.list_watchlist("dummy", user)

    def test_own_row_overrides_system_row_and_scope_labelled(self):
        rows = [{"user_id": "_shared", "code": "1111", "market": "JP", "added_at": 1, "name": "sys"},
                {"user_id": "u1", "code": "1111", "market": "JP", "added_at": 2, "name": "mine"},
                {"user_id": "_shared", "code": "2222", "market": "JP", "added_at": 3, "name": "sys2"},
                {"user_id": "u1", "code": "3333", "market": "JP", "added_at": 4, "name": "manual"}]
        with mock.patch.object(investment_db, "_watchlist_row_to_camel", side_effect=lambda r: dict(r)):
            out = self._run_list(rows, "u1")
        by = {o["code"]: o for o in out}
        self.assertEqual(by["1111"]["name"], "mine"); self.assertEqual(by["1111"]["scope"], "user")
        self.assertEqual(by["2222"]["scope"], "system"); self.assertEqual(by["3333"]["scope"], "user")

    def test_query_is_limited_to_shared_and_own_user_only(self):
        cur = mock.MagicMock(); cur.__enter__.return_value = cur; cur.__exit__.return_value = False
        cur.fetchall.return_value = []
        conn = mock.MagicMock(); conn.__enter__.return_value = mock.Mock(cursor=mock.Mock(return_value=cur)); conn.__exit__.return_value = False
        pool = mock.Mock(); pool.connection.return_value = conn
        with mock.patch.object(investment_db, "_get_pool", return_value=pool):
            investment_db.list_watchlist("dummy", "u1")
        _, params = cur.execute.call_args[0]
        self.assertEqual(params[0], ["_shared", "u1"])

    def test_manual_writes_are_personal_not_shared(self):
        for fn in ("upsert_watchlist_item", "delete_watchlist_item", "migrate_watchlist_from_client",
                   "upsert_watch_stock_candidate", "upsert_watchlist_master_stocks", "set_watch_target"):
            src = inspect.getsource(getattr(investment_db, fn))
            self.assertNotRegex(src, r"^\s*user_id\s*=\s*_SHARED_SCOPE", fn)

    def test_system_autoregistration_stays_shared(self):
        for fn in ("auto_register_or_tag_watchlist_item", "remove_auto_tag_key", "get_codes_with_auto_tag"):
            self.assertIn("_SHARED_SCOPE", inspect.getsource(getattr(investment_db, fn)), fn)


class SharedDiscoveryOnceTests(unittest.TestCase):
    def test_broad_loop_and_realtime_loop_run_once_not_per_user(self):
        for fn in (server._discovery_broad_loop, server._discovery_realtime_loop):
            src = inspect.getsource(fn)
            self.assertNotIn("for user_id in", src)
            self.assertIn("_DISCOVERY_SCOPE", src)

    def test_pool_is_shared_regardless_of_caller(self):
        server._DISCOVERY_POOL.clear()
        calls = {"n": 0}

        def fetcher():
            calls["n"] += 1
            return [], {"calls": 1, "rows": 0, "duration_ms": 1, "error": None}
        with mock.patch.object(server, "investment_db", None):
            server.discovery_broad_refresh("dummy", "user_a", fetcher=fetcher)
            server.discovery_broad_refresh("dummy", "user_b", fetcher=fetcher)
        self.assertEqual(list(server._DISCOVERY_POOL.keys()), ["_shared"])
        p1 = server.discovery_api_payload("user_a"); p2 = server.discovery_api_payload("user_b")
        self.assertEqual(p1["pool"], p2["pool"])
        server._DISCOVERY_POOL.clear()

    def test_market_discovery_db_functions_force_shared_scope(self):
        for fn in ("upsert_market_discovery", "list_market_discovery"):
            self.assertRegex(inspect.getsource(getattr(investment_db, fn)), r"user_id\s*=\s*_SHARED_SCOPE")


class ReflectionsArePrivateTests(unittest.TestCase):
    def test_reflection_functions_use_caller_user_id(self):
        for fn in ("create_trade_reflection", "list_trade_reflections", "update_trade_reflection",
                   "delete_trade_reflection", "find_similar_reflections"):
            self.assertNotRegex(inspect.getsource(getattr(investment_db, fn)), r"^\s*user_id\s*=\s*_SHARED_SCOPE", fn)


class FrontendAuthWiringTests(unittest.TestCase):
    """画面側：ログイン中表示・ログアウト・CSRF・ユーザー別localStorage・iPhone向け配慮。"""

    @classmethod
    def setUpClass(cls):
        import os
        base = os.path.dirname(os.path.abspath(__file__))
        cls.html = open(os.path.join(base, "trade-cockpit.html"), encoding="utf-8").read()
        cls.login = open(os.path.join(base, "login.html"), encoding="utf-8").read()

    def test_user_bar_and_logout_present(self):
        self.assertIn("ログイン中：", self.html)
        self.assertIn("ログアウト", self.html)
        self.assertIn("tcLogout", self.html)

    def test_fetch_wrapper_adds_csrf_only_for_same_origin_writes(self):
        self.assertIn("X-CSRF-Token", self.html)
        self.assertIn("same=", self.html)  # 同一オリジン判定を経由して付与する

    def test_local_storage_is_per_user(self):
        self.assertIn('"trade-cockpit-v1:" + TC_BOOT.user', self.html)

    def test_logout_button_is_tap_friendly(self):
        m = re.search(r"\.user-bar-logout\{[^}]*min-height:(\d+)px", self.html)
        self.assertTrue(m and int(m.group(1)) >= 44)

    def test_pwa_icon_and_viewport(self):
        self.assertIn('rel="apple-touch-icon"', self.html)
        self.assertIn("viewport-fit=cover", self.html)

    def test_login_page_is_mobile_friendly(self):
        self.assertIn("width=device-width", self.login)
        self.assertRegex(self.login, r"input\{[^}]*height:5\d+px")   # 入力欄の高さ50px台
        self.assertRegex(self.login, r"font-size:1[6-9]px")            # iOSで自動ズームされない16px以上
        self.assertIn('autocomplete="current-password"', self.login)


class StartupMigrationNeverReSharesPersonalDataTests(unittest.TestCase):
    """起動のたびに実行されるinit_schema()内のSQLが、個人/混合テーブルを_sharedへ戻したり重複行を
    削除したりしないこと（2026-09-26 Release Gate：旧MU-S1のwatchlist共有化が再起動のたびに個人watchlist
    を共有へ戻していた実バグの再発防止）。"""

    def test_no_startup_sql_reshares_or_dedupes_private_or_mixed_tables(self):
        src = _db_source()
        reshared = set(re.findall(r"UPDATE\s+(\w+)\s+SET\s+user_id\s*=\s*'_shared'", src))
        deduped = set(re.findall(r"DELETE FROM\s+(\w+)\s+a\s+USING", src))
        bad = (reshared | deduped) & (investment_db.PRIVATE_TABLES | investment_db.MIXED_TABLES)
        self.assertEqual(bad, set(), "起動時SQLが個人/混合テーブルを共有化・重複削除しています: %s" % sorted(bad))


if __name__ == "__main__":
    unittest.main()
