# Market Data高速化指示書（2026-09-15）の回帰テスト。
#
# prefetch_market_data_for_watchlist()・_intraday_regime_batch_prefetch()・_regime_from_bars()
# （既存_intraday_regime_uncached()からのリファクタ抽出）の単体テスト。
# 「分析ロジックは変えない」が絶対条件のため、_regime_from_bars()は既存の判定式を1文字も
# 変えずに切り出しただけであることをゴールデン値で確認する。
#
# 実行方法： cd files && python -m unittest test_market_data_prefetch -v

import unittest
from unittest import mock

import server


class RegimeFromBarsGoldenTests(unittest.TestCase):
    """指示書「絶対条件：分析ロジックを変えない」：_regime_from_bars()は_intraday_regime_uncached()
    から計算式を一切変えずに切り出しただけであることを、既知の入出力パターンで確認する。"""

    def _bars(self, closes, highs, lows, volumes):
        return {"closes": closes, "highs": highs, "lows": lows, "volumes": volumes}

    def test_risk_on_higher_highs_above_vwap(self):
        # 後半の高値が前半を上回り、終値がVWAP以上 → RISK_ON/higher_highs
        closes = [100, 101, 102, 103, 104, 105]
        highs = [100, 101, 100, 104, 105, 106]
        lows = [99, 100, 99, 102, 103, 104]
        volumes = [10, 10, 10, 10, 10, 10]
        r = server._regime_from_bars(self._bars(closes, highs, lows, volumes))
        self.assertEqual(r["regime"], "RISK_ON")
        self.assertEqual(r["pattern"], "higher_highs")
        self.assertTrue(r["aboveVwap"])

    def test_risk_off_lower_lows_below_vwap(self):
        closes = [105, 104, 103, 102, 101, 95]
        highs = [106, 105, 104, 100, 99, 98]
        lows = [104, 103, 102, 97, 96, 90]
        volumes = [10, 10, 10, 10, 10, 10]
        r = server._regime_from_bars(self._bars(closes, highs, lows, volumes))
        self.assertEqual(r["regime"], "RISK_OFF")
        self.assertEqual(r["pattern"], "lower_lows")
        self.assertFalse(r["aboveVwap"])

    def test_insufficient_bars_returns_none(self):
        closes = [100, 101, 102]
        r = server._regime_from_bars(self._bars(closes, closes, closes, [1, 1, 1]))
        self.assertIsNone(r)

    def test_empty_bars_returns_none(self):
        self.assertIsNone(server._regime_from_bars(None))
        self.assertIsNone(server._regime_from_bars({"closes": []}))

    def test_uncached_wrapper_matches_pure_function(self):
        """_intraday_regime_uncached()が_regime_from_bars()と同じ結果になること
        （リファクタで計算ロジックが分岐していないことの確認）。"""
        bars = self._bars([100, 101, 102, 103, 104, 105],
                            [100, 101, 100, 104, 105, 106],
                            [99, 100, 99, 102, 103, 104],
                            [10, 10, 10, 10, 10, 10])
        with mock.patch.object(server, "_fetch_intraday", return_value=bars):
            with mock.patch.object(server, "yf") as mock_yf:
                mock_yf.Ticker.return_value = mock.Mock()
                direct = server._intraday_regime_uncached("7203.T", "5m")
        pure = server._regime_from_bars(bars)
        self.assertEqual(direct, pure)


class MarketDataPrefetchDiagnosticsTests(unittest.TestCase):
    """prefetch_market_data_for_watchlist()：diagnostics（Phase I）とduplicate suppression。"""

    def _watchlist(self, codes):
        return [{"code": c, "name": f"銘柄{c}", "market": "JP"} for c in codes]

    def test_duplicate_codes_counted_once(self):
        """同一実行内での重複銘柄（Phase A監査対象）はunique_tickersとして1回だけ数える。"""
        wl = self._watchlist(["7203", "7203", "9984"])
        with mock.patch.object(server, "get_stock_quotes", return_value={}) as mock_gq:
            with mock.patch.object(server, "_intraday_regime_batch_prefetch",
                                    return_value={"attempted": 2, "cached": 2, "batches": 1}) as mock_regime:
                diag = server.prefetch_market_data_for_watchlist(wl)
        self.assertEqual(diag["unique_tickers"], 2)  # 7203が2件あっても1
        mock_gq.assert_called_once()  # watchlist全体を1回のバッチ呼び出しにまとめている（個別呼び出しをしない）
        mock_regime.assert_called_once()

    def test_empty_watchlist_returns_zeroed_diagnostics_without_calling_apis(self):
        with mock.patch.object(server, "get_stock_quotes") as mock_gq:
            diag = server.prefetch_market_data_for_watchlist([])
        mock_gq.assert_not_called()
        self.assertEqual(diag["unique_tickers"], 0)
        self.assertEqual(diag["total_requests"], 0)

    def test_cache_status_ok_counts_as_hit(self):
        wl = self._watchlist(["7203"])

        def fake_get_stock_quotes(watchlist, cache_ttl=0, status_out=None):
            if status_out is not None:
                status_out["7203"] = "ok"
            return {"7203": {"t": 100}}

        with mock.patch.object(server, "get_stock_quotes", side_effect=fake_get_stock_quotes):
            with mock.patch.object(server, "_intraday_regime_batch_prefetch",
                                    return_value={"attempted": 1, "cached": 1, "batches": 1}):
                diag = server.prefetch_market_data_for_watchlist(wl)
        self.assertEqual(diag["cache_hits"], 2)  # quote分1 + 5m regime分1
        self.assertEqual(diag["errors"], 0)

    def test_stale_and_error_status_tracked_separately(self):
        wl = self._watchlist(["7203", "9984"])

        def fake_get_stock_quotes(watchlist, cache_ttl=0, status_out=None):
            if status_out is not None:
                status_out["7203"] = "stale_cache"
                status_out["9984"] = "rate_limited"
            return {}

        with mock.patch.object(server, "get_stock_quotes", side_effect=fake_get_stock_quotes):
            with mock.patch.object(server, "_intraday_regime_batch_prefetch",
                                    return_value={"attempted": 2, "cached": 0, "batches": 1}):
                diag = server.prefetch_market_data_for_watchlist(wl)
        self.assertEqual(diag["stale_fallbacks"], 1)
        self.assertEqual(diag["errors"], 1 + 2)  # quote側rate_limited 1件 + 5m regime側 attempted-cached=2

    def test_prefetch_failure_does_not_raise(self):
        """バッチ取得が例外を出しても呼び出し元へは伝播しない（個別フォールバック経路が
        既存通り動く前提を壊さない、指示書「安全側」）。"""
        wl = self._watchlist(["7203"])
        with mock.patch.object(server, "get_stock_quotes", side_effect=RuntimeError("boom")):
            with mock.patch.object(server, "_intraday_regime_batch_prefetch", side_effect=RuntimeError("boom2")):
                diag = server.prefetch_market_data_for_watchlist(wl)  # 例外を投げない
        self.assertGreaterEqual(diag["errors"], 1)

    def test_include_5m_false_skips_regime_batch(self):
        wl = self._watchlist(["7203"])
        with mock.patch.object(server, "get_stock_quotes", return_value={}):
            with mock.patch.object(server, "_intraday_regime_batch_prefetch") as mock_regime:
                server.prefetch_market_data_for_watchlist(wl, include_5m=False)
        mock_regime.assert_not_called()

    def test_us_symbols_excluded_from_5m_batch(self):
        wl = self._watchlist(["7203"]) + [{"code": "AAPL", "name": "Apple", "market": "US"}]
        with mock.patch.object(server, "get_stock_quotes", return_value={}):
            with mock.patch.object(server, "_intraday_regime_batch_prefetch",
                                    return_value={"attempted": 1, "cached": 1, "batches": 1}) as mock_regime:
                server.prefetch_market_data_for_watchlist(wl)
        called_symbols = mock_regime.call_args[0][0]
        self.assertEqual(called_symbols, ["7203.T"])  # AAPLは対象外


class IntradayRegimeBatchPrefetchTests(unittest.TestCase):
    """_intraday_regime_batch_prefetch()：バッチ取得結果を既存キャッシュキーへ正しくseedする。"""

    def test_seeds_existing_cache_key_format(self):
        """既存_intraday_regime_cached()が読むキー（"intraday5m:{symbol}:{interval}"）と
        同じキーへ書き込むこと（Phase B：既存キャッシュ機構をそのまま拡張、新規キャッシュを
        乱立させない）。"""
        import pandas as pd
        idx = pd.date_range("2026-09-15 09:00", periods=10, freq="5min", tz="Asia/Tokyo")
        frame = pd.DataFrame({
            "Open": [100] * 10, "High": [100 + i for i in range(10)],
            "Low": [99] * 10, "Close": [100 + i * 0.5 for i in range(10)],
            "Volume": [1000] * 10,
        }, index=idx)
        with mock.patch.object(server, "_download_intraday_chunk", return_value=frame):
            stats = server._intraday_regime_batch_prefetch(["7203.T"], "5m", 90)
        self.assertEqual(stats["attempted"], 1)
        self.assertEqual(stats["cached"], 1)
        cached = server._cache_get("intraday5m:7203.T:5m")
        self.assertIsNotNone(cached)
        self.assertIn("vwap", cached["value"])

    def test_duplicate_symbols_deduped_before_batching(self):
        with mock.patch.object(server, "_download_intraday_chunk", return_value=None) as mock_dl:
            server._intraday_regime_batch_prefetch(["7203.T", "7203.T", "9984.T"], "5m", 90)
        # _download_intraday_chunkに渡されるのは重複除去済みの3件未満のはず
        called_chunk = mock_dl.call_args[0][0]
        self.assertEqual(len(called_chunk), 2)

    def test_empty_symbols_returns_zero_stats_without_network(self):
        with mock.patch.object(server, "_download_intraday_chunk") as mock_dl:
            stats = server._intraday_regime_batch_prefetch([], "5m", 90)
        mock_dl.assert_not_called()
        self.assertEqual(stats, {"attempted": 0, "cached": 0, "batches": 0, "values": {}})

    def test_download_failure_does_not_raise(self):
        with mock.patch.object(server, "_download_intraday_chunk", side_effect=RuntimeError("boom")):
            stats = server._intraday_regime_batch_prefetch(["7203.T"], "5m", 90)
        self.assertEqual(stats["cached"], 0)


class ScoreEntryCandidatesInterfaceUnchangedTests(unittest.TestCase):
    """指示書「絶対条件：分析ロジックを変えない」「X Intelligence非干渉」：
    prefetchを追加してもentry_score／entryState／rankのロジック自体は変わらないこと
    （prefetch関数はモックし、既存の_select_entry_ready_top5等の判定関数は一切変更していない
    ことを別テストファイル test_top5_selection_fix.py が既にカバーしている——ここでは
    prefetch呼び出しの有無に関わらず_score_entry_candidatesが例外を出さず動くことだけ確認）。"""

    def test_prefetch_called_before_per_item_loop_and_failure_is_non_fatal(self):
        """prefetch_market_data_for_watchlist()が例外を出しても_score_entry_candidates全体は
        落ちない（Phase F「1 tickerの取得失敗で分析全体が何分も止まらない」の安全側設計）。
        stage1_rowsを空にしてwatchlistループ自体を早期スキップさせ、prefetch呼び出しの有無・
        タイミングだけを確認する（既存の候補選定ロジックはこのテストの対象外）。"""
        if server.investment_db is None:
            self.skipTest("investment_db not available")
        watchlist = [{"code": "7203", "name": "トヨタ自動車", "market": "JP"}]
        with mock.patch.object(server.investment_db, "list_watchlist", return_value=watchlist), \
             mock.patch.object(server.investment_db, "get_codes_with_auto_tag", return_value=set()), \
             mock.patch.object(server, "prefetch_market_data_for_watchlist",
                                side_effect=RuntimeError("prefetch boom")) as mock_prefetch, \
             mock.patch.object(server, "run_momentum_stage1", return_value={"rows": {}, "nikkeiChangePct": 0}):
            result = server._score_entry_candidates("dummy_url", "dummy_user")  # 例外を投げない
        mock_prefetch.assert_called_once()
        self.assertEqual(result["entryReadyTop5"], [])  # stage1_rowsが空なので候補0件（正常系）


if __name__ == "__main__":
    unittest.main()
