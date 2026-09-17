# watchTargets同期の運用診断（2026-09-18新規）テスト。
#
# 「iPhoneだけ監視銘柄が3件のまま」調査で、PC/iPhone間でサーバープロセスの再起動漏れ・
# 古いコードが応答していることを切り分けるための/api/version診断エンドポイントを
# 検証する。フロント側のマージ計算式そのものの正しさはtest_watch_targets_merge.js
# （node test_watch_targets_merge.js、DB/HTTP不要の純粋ロジック検証）で別途検証済み。
#
# 実行方法： cd files && python -m unittest test_watch_targets_diagnostics -v

import unittest

import server
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


if __name__ == "__main__":
    unittest.main()
