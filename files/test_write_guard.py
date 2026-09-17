# 本番DB書き込み安全ガード（2026-09-18新規）テスト。
#
# 運用前確認で、分離テスト環境（secrets.jsonのusersを空にしてauth bypassしたコピー）が
# database_urlを本番Neonとそのまま共有していたため、テスト用write操作（watch-target・
# trade_reflections）が本番DBへそのまま書き込まれてしまう事故があった。再発防止として、
# auth bypassモードでTEST_DATABASE_URLが明示指定されていない限り、全POST（この実装の
# 唯一の書き込み経路）をrouting入口で一括拒否するcentral guardを追加した。
#
# 実行方法： cd files && python -m unittest test_write_guard -v

import unittest
from unittest import mock

import server
from test_support_source_inspect import get_fresh_source


class ComputeWriteE2EPolicyTests(unittest.TestCase):
    """compute_write_e2e_policy()は純粋関数として、users/test_database_url/secrets側の
    database_urlの3つの入力だけから安全ガードの判定を決める（グローバル状態を読まない）。"""

    def test_auth_bypass_without_test_db_blocks_write_and_keeps_secrets_url(self):
        # 必須テスト1：auth bypass + 本番DATABASE_URL（secrets.json由来）→ write拒否
        policy = server.compute_write_e2e_policy(
            users={}, test_database_url="", secrets_database_url="postgresql://prod-host/proddb")
        self.assertTrue(policy["auth_bypass"])
        self.assertFalse(policy["write_e2e_allowed"])
        # 本番DATABASE_URLをTEST_DATABASE_URLで上書きしていないこと（推測でDB切替をしない）
        self.assertEqual(policy["database_url"], "postgresql://prod-host/proddb")

    def test_auth_bypass_with_test_db_allows_write_and_overrides_url(self):
        # 必須テスト2：auth bypass + TEST_DATABASE_URL明示指定 → write許可、DB接続先も切替
        policy = server.compute_write_e2e_policy(
            users={}, test_database_url="postgresql://test-host/testdb",
            secrets_database_url="postgresql://prod-host/proddb")
        self.assertTrue(policy["auth_bypass"])
        self.assertTrue(policy["write_e2e_allowed"])
        self.assertEqual(policy["database_url"], "postgresql://test-host/testdb")

    def test_normal_auth_mode_always_allows_write_regardless_of_test_db(self):
        # 必須テスト3：通常認証モード（users設定済み）→ 本番DATABASE_URLでも既存write挙動を壊さない
        policy = server.compute_write_e2e_policy(
            users={"matsuura": "xxxx"}, test_database_url="", secrets_database_url="postgresql://prod-host/proddb")
        self.assertFalse(policy["auth_bypass"])
        self.assertTrue(policy["write_e2e_allowed"])
        self.assertEqual(policy["database_url"], "postgresql://prod-host/proddb")

    def test_normal_auth_mode_ignores_test_database_url(self):
        # 通常認証モードでは、TEST_DATABASE_URLが設定されていてもsecrets側のdatabase_urlを
        # そのまま使う（本番運用中に誤ってTEST_DATABASE_URLが環境に残っていても無害）。
        policy = server.compute_write_e2e_policy(
            users={"matsuura": "xxxx"}, test_database_url="postgresql://test-host/testdb",
            secrets_database_url="postgresql://prod-host/proddb")
        self.assertEqual(policy["database_url"], "postgresql://prod-host/proddb")


class MaskDatabaseUrlTests(unittest.TestCase):
    """必須テスト5：起動ログにhost/dbnameは出るがpasswordは出ない。"""

    def test_password_not_present_in_masked_output(self):
        url = "postgresql://neondb_owner:supersecretpassword@ep-example-host.aws.neon.tech/neondb?sslmode=require"
        masked = server._mask_database_url(url)
        self.assertNotIn("supersecretpassword", str(masked))
        self.assertNotIn("neondb_owner", str(masked))  # ユーザー名も出さない

    def test_host_and_dbname_present(self):
        url = "postgresql://neondb_owner:supersecretpassword@ep-example-host.aws.neon.tech/neondb?sslmode=require"
        masked = server._mask_database_url(url)
        self.assertEqual(masked["host"], "ep-example-host.aws.neon.tech")
        self.assertEqual(masked["dbname"], "neondb")

    def test_empty_url_does_not_crash(self):
        masked = server._mask_database_url("")
        self.assertIsNone(masked["host"])
        self.assertIsNone(masked["dbname"])


class _FakeHandler:
    """do_POST()をソケット無しで呼ぶための最小限のダミー（このリポジトリの既存テストは
    Handlerクラスを一切インスタンス化しない方針のため、do_POST冒頭のguardだけを検証する
    最小限のスタブに留める——ルーティング本体の個別分岐はここでは呼ばれない）。"""
    def __init__(self):
        self.path = "/api/watchlist/watch-target"
        self.sent = []

    def _authorized(self):
        return True

    def _send_json(self, obj, status=200):
        self.sent.append((status, obj))
        return None

    def _read_json_body(self):
        return {}


class DoPostCentralGuardTests(unittest.TestCase):
    """必須テスト1・4：write_e2e_allowed=Falseのとき、POSTのrouting入口で一括403拒否される
    こと（個別endpoint実装まで到達しないこと）を確認する。エンドポイントを1つずつ検証する
    のではなく、do_POST冒頭のcentral guardという設計そのものをテストする。"""

    def test_write_blocked_returns_403_before_routing(self):
        handler = _FakeHandler()
        with mock.patch.object(server, "WRITE_E2E_ALLOWED", False):
            server.Handler.do_POST(handler)
        self.assertEqual(len(handler.sent), 1)
        status, body = handler.sent[0]
        self.assertEqual(status, 403)
        self.assertIn("error", body)

    def test_write_allowed_proceeds_past_guard(self):
        # write_e2e_allowed=Trueのときはguardで即returnしない（この後の実際のルーティング分岐
        # へ進むことをverify——本テストでは_investment_db_readyが無いスタブのため実際の分岐で
        # AttributeErrorになるが、それ自体が「guardを通過してルーティングへ進んだ」証拠になる）。
        handler = _FakeHandler()
        with mock.patch.object(server, "WRITE_E2E_ALLOWED", True):
            with self.assertRaises(AttributeError):
                server.Handler.do_POST(handler)
        # guardの403は送っていない（guardをすり抜けて別の分岐で失敗した）
        self.assertFalse(any(s == 403 for s, _ in handler.sent))

    def test_guard_is_first_check_in_do_post_source(self):
        # do_POSTの中で、WRITE_E2E_ALLOWEDチェックが_authorized()の直後・個別ルーティングより
        # 前に置かれていること（後から新しいwrite endpointが足されても自動的に保護される
        # central guard設計であることの構造的確認）。
        src = get_fresh_source(server.Handler.do_POST)
        auth_pos = src.index("_authorized")
        guard_pos = src.index("WRITE_E2E_ALLOWED")
        first_route_pos = src.index("self.path ==")
        self.assertLess(auth_pos, guard_pos)
        self.assertLess(guard_pos, first_route_pos)


class DoGetUnaffectedTests(unittest.TestCase):
    """必須テスト1：GET/HEADは引き続き許可される（do_GETにはwrite guardを入れていないこと
    の構造的確認）。"""

    def test_do_get_source_has_no_write_e2e_guard(self):
        src = get_fresh_source(server.Handler.do_GET)
        self.assertNotIn("WRITE_E2E_ALLOWED", src)


if __name__ == "__main__":
    unittest.main()
