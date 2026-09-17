# watchTargets同期の運用診断（2026-09-18新規）テスト。
#
# 「iPhoneだけ監視銘柄が3件のまま」→「PC localStorage 73件がNeonへ反映されない」調査で、
# PC/iPhone間でサーバープロセスの再起動漏れ・古いコードが応答していることを切り分ける
# ための/api/version診断エンドポイントと、set_watch_target()が実際に行を更新できたかを
# 正しく報告するかを検証する。フロント側のマージ計算式そのものの正しさは
# test_watch_targets_merge.js（node test_watch_targets_merge.js、DB/HTTP不要の純粋ロジック
# 検証）で別途検証済み。
#
# 実行方法： cd files && python -m unittest test_watch_targets_diagnostics -v

import unittest
from unittest import mock

import server
import investment_db
from test_support_source_inspect import get_fresh_source


class AppBuildVersionTests(unittest.TestCase):
    def test_build_version_is_nonempty_string(self):
        self.assertIsInstance(server.APP_BUILD_VERSION, str)
        self.assertTrue(server.APP_BUILD_VERSION.strip())


class VersionEndpointRoutingTests(unittest.TestCase):
    """/api/versionがdo_GET冒頭（認証チェック直後）でAPP_BUILD_VERSIONを返すことを
    ソース検査で確認する（このリポジトリの既存規約：Handlerクラスはインスタンス化しない）。"""

    def test_version_route_exists_and_returns_build_constant(self):
        src = get_fresh_source(server.Handler.do_GET)
        self.assertIn('self.path.startswith("/api/version")', src)
        version_pos = src.index('/api/version')
        build_pos = src.index("APP_BUILD_VERSION", version_pos)
        # /api/versionの分岐の直後（次のelifより前）でAPP_BUILD_VERSIONを参照していること
        next_elif_pos = src.index("elif self.path.startswith", version_pos + 1)
        self.assertLess(version_pos, build_pos)
        self.assertLess(build_pos, next_elif_pos)

    def test_version_route_checked_before_first_business_route(self):
        # /api/versionは診断専用のため、既存のビジネスロジック（/api/quotes等）より前に
        # 判定されること（どのルートより先に確実に応答できることの確認）。
        src = get_fresh_source(server.Handler.do_GET)
        version_pos = src.index('self.path.startswith("/api/version")')
        quotes_pos = src.index('self.path.startswith("/api/quotes")')
        self.assertLess(version_pos, quotes_pos)


class _FakeExecResult:
    def __init__(self, rowcount):
        self.rowcount = rowcount


class _FakeConn:
    def __init__(self, rowcount):
        self._rowcount = rowcount
        self.committed = False
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        return _FakeExecResult(self._rowcount)

    def commit(self):
        self.committed = True


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def connection(self):
        pool = self

        class _Ctx:
            def __enter__(self):
                return pool._conn

            def __exit__(self, *a):
                return False
        return _Ctx()


class SetWatchTargetRowcountTests(unittest.TestCase):
    """2026-09-18修正：set_watch_target()はUPDATEが実際に行を更新できたかどうか
    （cursor.rowcount）を見て結果を返すこと。従来は常にTrueを返し、code/marketの不一致で
    0行しか更新されなかった場合でもフロント側が「成功」と誤認していた（PC localStorage
    73件がNeonへ反映されない不具合の直接原因の一つ）。"""

    def test_returns_true_when_row_actually_matched(self):
        conn = _FakeConn(rowcount=1)
        with mock.patch.object(investment_db, "_get_pool", return_value=_FakePool(conn)):
            result = investment_db.set_watch_target("dummy_url", "matsuura", "1332", "JP", True)
        self.assertTrue(result)
        self.assertTrue(conn.committed)

    def test_returns_false_when_no_row_matched(self):
        # code/marketの組み合わせがwatchlistに存在しない場合（0行更新）はFalseを返す
        # ——「成功したように見えて実際は何も書き込まれていない」を無くす。
        conn = _FakeConn(rowcount=0)
        with mock.patch.object(investment_db, "_get_pool", return_value=_FakePool(conn)):
            result = investment_db.set_watch_target("dummy_url", "matsuura", "9999999", "JP", True)
        self.assertFalse(result)

    def test_market_defaults_to_jp_in_where_clause(self):
        conn = _FakeConn(rowcount=1)
        with mock.patch.object(investment_db, "_get_pool", return_value=_FakePool(conn)):
            investment_db.set_watch_target("dummy_url", "matsuura", "1332", None, True)
        _sql, params = conn.executed[0]
        self.assertIn("JP", params)

    def test_forces_shared_scope(self):
        conn = _FakeConn(rowcount=1)
        with mock.patch.object(investment_db, "_get_pool", return_value=_FakePool(conn)):
            investment_db.set_watch_target("dummy_url", "some_other_user", "1332", "JP", True)
        _sql, params = conn.executed[0]
        self.assertIn(investment_db._SHARED_SCOPE, params)
        self.assertNotIn("some_other_user", params)


if __name__ == "__main__":
    unittest.main()
