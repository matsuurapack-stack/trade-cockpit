# Phase QF-1（2026-09-14新規）：FAST QUOTE経路（立花証券API主ソース化）の回帰テスト。
#
# 設計：TACHIBANA → fallback（直近キャッシュ） → yfinance → last known value の優先順位で
# リアルタイム現在値を取得する。get_stock_quotes()（既存・yfinance history付き、spark用の
# SLOW analytics）は一切変更せず、FAST経路（get_fast_quotes）からは通常呼ばない
# （tachibana・キャッシュのどちらも失敗した銘柄のみの少数フォールバックとしてのみ呼ぶ）。
#
# 実行方法： cd files && python -m unittest test_fast_quote_qf1 -v

import time
import unittest
from unittest import mock

import server
from test_support_source_inspect import get_fresh_source


def _watchlist(codes):
    return [{"code": c, "market": "JP", "name": f"銘柄{c}"} for c in codes]


class FastQuoteHappyPathTests(unittest.TestCase):
    """正常系：立花証券API成功時はyfinanceを一切呼ばない。"""

    def setUp(self):
        server._fast_quote_cache.clear()

    @mock.patch("server.get_stock_quotes")
    @mock.patch("server.tachibana_api")
    def test_tachibana_success_never_calls_yfinance(self, mock_tachibana, mock_yf):
        mock_tachibana.get_market_price.return_value = {
            "6753": {"t": 691.5, "p": 677.0, "change": 14.5, "changePct": 2.14,
                       "volume": 1726500, "open": 680.0, "high": 695.0, "low": 675.0,
                       "ask": 691.6, "bid": 691.4},
        }
        quotes, stats = server.get_fast_quotes(_watchlist(["6753"]))
        mock_yf.assert_not_called()
        self.assertEqual(quotes["6753"]["t"], 691.5)
        self.assertEqual(quotes["6753"]["source"], "tachibana")
        self.assertEqual(stats["tachibana"], 1)
        self.assertEqual(stats["fallback_yf"], 0)

    @mock.patch("server.tachibana_api")
    def test_volume_and_source_present(self, mock_tachibana):
        mock_tachibana.get_market_price.return_value = {
            "8001": {"t": 2358.5, "p": 2295.0, "change": 63.5, "changePct": 2.77,
                       "volume": 5000000, "open": 2300.0, "high": 2360.0, "low": 2290.0,
                       "ask": 2358.6, "bid": 2358.4},
        }
        quotes, stats = server.get_fast_quotes(_watchlist(["8001"]))
        self.assertEqual(quotes["8001"]["volume"], 5000000)
        self.assertEqual(quotes["8001"]["source"], "tachibana")
        self.assertIn("quote_timestamp", quotes["8001"])
        self.assertIn("fetched_at", quotes["8001"])
        self.assertFalse(quotes["8001"]["is_stale"])

    @mock.patch("server.tachibana_api")
    def test_56_codes_all_succeed(self, mock_tachibana):
        codes = [f"{1000+i}" for i in range(56)]
        mock_tachibana.get_market_price.side_effect = lambda chunk: {
            c: {"t": 100.0, "p": 99.0, "change": 1.0, "changePct": 1.01, "volume": 1000,
                 "open": 99.5, "high": 101.0, "low": 98.5, "ask": 100.1, "bid": 99.9} for c in chunk
        }
        quotes, stats = server.get_fast_quotes(_watchlist(codes))
        self.assertEqual(stats["requested"], 56)
        self.assertEqual(stats["tachibana"], 56)
        self.assertEqual(len(quotes), 56)
        # PRICE_CHUNK=40を維持しているため56件は2チャンクに分かれて呼ばれる
        self.assertEqual(mock_tachibana.get_market_price.call_count, 2)


class FastQuoteFallbackTests(unittest.TestCase):
    """fallback系：TACHIBANA失敗時の優先順位（キャッシュ→yfinance→last known）。"""

    def setUp(self):
        server._fast_quote_cache.clear()
        self._sleep_patch = mock.patch("server.time.sleep")  # リトライのバックオフを実待機しない
        self._sleep_patch.start()
        self.addCleanup(self._sleep_patch.stop)

    @mock.patch("server.get_stock_quotes")
    @mock.patch("server.tachibana_api")
    def test_tachibana_timeout_falls_back_to_yfinance(self, mock_tachibana, mock_yf):
        mock_tachibana.get_market_price.side_effect = TimeoutError("timeout")
        mock_yf.return_value = {"6753": {"t": 690.0, "p": 677.0, "volume": 100, "open": 680, "high": 695, "low": 675}}
        quotes, stats = server.get_fast_quotes(_watchlist(["6753"]))
        mock_yf.assert_called_once()
        self.assertEqual(quotes["6753"]["source"], "yfinance_fallback")
        self.assertTrue(quotes["6753"]["is_stale"])
        self.assertEqual(stats["fallback_yf"], 1)
        self.assertEqual(stats["tachibana"], 0)

    @mock.patch("server.get_stock_quotes")
    @mock.patch("server.tachibana_api")
    def test_tachibana_empty_response_falls_back(self, mock_tachibana, mock_yf):
        mock_tachibana.get_market_price.return_value = {}  # 空応答
        mock_yf.return_value = {"6753": {"t": 690.0, "p": 677.0, "volume": 100, "open": 680, "high": 695, "low": 675}}
        quotes, stats = server.get_fast_quotes(_watchlist(["6753"]))
        self.assertEqual(quotes["6753"]["source"], "yfinance_fallback")

    @mock.patch("server.tachibana_api")
    def test_session_error_retries_before_falling_back(self, mock_tachibana):
        # 1回目は失敗（例外）、2回目で成功＝再ログイン再試行で復旧するケース
        mock_tachibana.get_market_price.side_effect = [
            RuntimeError("立花証券APIログイン失敗"),
            {"6753": {"t": 691.5, "p": 677.0, "change": 14.5, "changePct": 2.14, "volume": 1726500,
                        "open": 680.0, "high": 695.0, "low": 675.0, "ask": 691.6, "bid": 691.4}},
        ]
        with mock.patch("server.time.sleep"):  # バックオフの実待機はテストでは省略
            quotes, stats = server.get_fast_quotes(_watchlist(["6753"]))
        self.assertEqual(quotes["6753"]["source"], "tachibana")
        self.assertEqual(mock_tachibana.get_market_price.call_count, 2)

    @mock.patch("server.get_stock_quotes")
    @mock.patch("server.tachibana_api")
    def test_retry_exhausted_falls_back(self, mock_tachibana, mock_yf):
        # FAST_QUOTE_MAX_ATTEMPTS回すべて失敗 → yfinanceへ
        mock_tachibana.get_market_price.side_effect = RuntimeError("boom")
        mock_yf.return_value = {"6753": {"t": 690.0, "p": 677.0, "volume": 100, "open": 680, "high": 695, "low": 675}}
        with mock.patch("server.time.sleep"):
            quotes, stats = server.get_fast_quotes(_watchlist(["6753"]))
        self.assertEqual(mock_tachibana.get_market_price.call_count, server.FAST_QUOTE_MAX_ATTEMPTS)
        self.assertEqual(quotes["6753"]["source"], "yfinance_fallback")

    @mock.patch("server.get_stock_quotes")
    @mock.patch("server.tachibana_api")
    def test_all_sources_fail_omits_code_for_last_known_value(self, mock_tachibana, mock_yf):
        # tachibana・yfinanceどちらも失敗 → quotesにそのコードを含めない
        # （フロント側は直前のstockQuotes値＝last known valueをそのまま保持する設計）。
        mock_tachibana.get_market_price.side_effect = RuntimeError("boom")
        mock_yf.return_value = {}
        with mock.patch("server.time.sleep"):
            quotes, stats = server.get_fast_quotes(_watchlist(["6753"]))
        self.assertNotIn("6753", quotes)
        self.assertEqual(stats["failed"], 1)

    @mock.patch("server.tachibana_api")
    def test_recent_cache_used_before_yfinance(self, mock_tachibana):
        # 直近成功キャッシュがTTL内なら、tachibana失敗時にまずキャッシュを使う
        # （yfinanceを呼ばない＝FAST経路がyfinance履歴取得に頼らない設計の確認）。
        server._fast_quote_cache["6753"] = {
            "value": {"t": 688.0, "p": 677.0, "change": 11.0, "changePct": 1.62, "volume": 1500000,
                        "open": 680.0, "high": 690.0, "low": 675.0, "ask": 688.1, "bid": 687.9,
                        "source": "tachibana", "quote_timestamp": "x", "fetched_at": "x", "is_stale": False},
            "at": time.time(),
        }
        mock_tachibana.get_market_price.return_value = {}
        with mock.patch("server.time.sleep"), mock.patch("server.get_stock_quotes") as mock_yf:
            quotes, stats = server.get_fast_quotes(_watchlist(["6753"]))
            mock_yf.assert_not_called()
        self.assertEqual(quotes["6753"]["source"], "cache")
        self.assertTrue(quotes["6753"]["is_stale"])
        self.assertEqual(stats["fallback_cache"], 1)

    @mock.patch("server.tachibana_api")
    def test_expired_cache_not_used(self, mock_tachibana):
        server._fast_quote_cache["6753"] = {
            "value": {"t": 688.0, "source": "tachibana"},
            "at": time.time() - (server.FAST_QUOTE_CACHE_TTL_SEC + 5),
        }
        mock_tachibana.get_market_price.return_value = {}
        with mock.patch("server.time.sleep"), mock.patch("server.get_stock_quotes", return_value={}) as mock_yf:
            quotes, stats = server.get_fast_quotes(_watchlist(["6753"]))
            mock_yf.assert_called_once()
        self.assertEqual(stats["fallback_cache"], 0)


class FastQuoteEndpointRoutingTests(unittest.TestCase):
    """/api/stock-quotes/fastが既存/api/stock-quotesのルーティングを壊していないこと
    （startswithマッチのため順序に依存する）。"""

    def test_fast_route_checked_before_generic_route(self):
        # linecache汚染対策（Bugfix: isolate global state between test modules）：
        # test_support_source_inspect.get_fresh_source参照。
        src = get_fresh_source(server.Handler.do_POST)
        fast_idx = src.find('"/api/stock-quotes/fast"')
        generic_idx = src.find('startswith("/api/stock-quotes")')
        self.assertNotEqual(fast_idx, -1)
        self.assertNotEqual(generic_idx, -1)
        self.assertLess(fast_idx, generic_idx, "/api/stock-quotes/fastは既存の/api/stock-quotesより先に判定されること")


if __name__ == "__main__":
    unittest.main()
