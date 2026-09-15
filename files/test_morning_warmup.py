# Market Data高速化 Phase 4（Morning Warmup / Cold Start解消）の回帰テスト。
#
# _tachibana_daily_history_cached()（trading_date scopedの長期キャッシュ）と
# run_morning_market_warmup()（寄り前の事前ウォームアップ）を検証する。
#
# 実行方法： cd files && python -m unittest test_morning_warmup -v

import unittest
from unittest import mock

import server


def make_hist(dates_closes):
    return [{"date": d, "open": c - 1, "high": c + 1, "low": c - 2, "close": c, "volume": 1000}
            for d, c in dates_closes]


class TachibanaDailyHistoryCachedTests(unittest.TestCase):
    """_tachibana_daily_history_cached()：trading_date scopedキャッシュキー・fresh判定・
    翌営業日での自動失効（キー自体が変わる）を検証する（R1・R2）。"""

    def setUp(self):
        for c in ["7203", "9984"]:
            for d in ["2026-09-15", "2026-09-16"]:
                server._CACHE_STORE.pop(f"daily_history:{d}:{c}", None)

    def test_first_call_hits_api_and_caches(self):
        hist = make_hist([(f"2020-01-{d:02d}", 100 + d) for d in range(1, 26)])
        with mock.patch.object(server.tachibana_api, "get_daily_history", return_value=hist) as mock_gdh:
            result1 = server._tachibana_daily_history_cached("7203", trading_date="2026-09-15")
            result2 = server._tachibana_daily_history_cached("7203", trading_date="2026-09-15")
        mock_gdh.assert_called_once()  # 2回目はキャッシュヒットでAPIを呼ばない
        self.assertEqual(result1, result2)

    def test_different_trading_date_is_different_cache_key(self):
        """指示書STEP6：翌営業日になれば自動的に別keyになり、古い日の履歴を誤用しない。"""
        hist1 = make_hist([(f"2020-01-{d:02d}", 100 + d) for d in range(1, 26)])
        hist2 = make_hist([(f"2020-02-{d:02d}", 200 + d) for d in range(1, 26)])
        with mock.patch.object(server.tachibana_api, "get_daily_history", side_effect=[hist1, hist2]) as mock_gdh:
            r_day1 = server._tachibana_daily_history_cached("7203", trading_date="2026-09-15")
            r_day2 = server._tachibana_daily_history_cached("7203", trading_date="2026-09-16")
        self.assertEqual(mock_gdh.call_count, 2)  # 日付が違えば別キー＝再取得される
        self.assertNotEqual(r_day1, r_day2)

    def test_default_trading_date_uses_jst_today(self):
        with mock.patch.object(server, "_jst_today_date_str", return_value="2026-09-15"):
            with mock.patch.object(server.tachibana_api, "get_daily_history", return_value=None) as mock_gdh:
                mock_gdh.return_value = make_hist([(f"2020-01-{d:02d}", 100 + d) for d in range(1, 26)])
                server._tachibana_daily_history_cached("7203")
        self.assertIsNotNone(server._cache_get("daily_history:2026-09-15:7203"))

    def test_short_history_returns_none_and_is_not_cached(self):
        with mock.patch.object(server.tachibana_api, "get_daily_history", return_value=make_hist([("2020-01-01", 100)])):
            result = server._tachibana_daily_history_cached("7203", trading_date="2026-09-15")
        self.assertIsNone(result)
        self.assertIsNone(server._cache_get("daily_history:2026-09-15:7203"))

    def test_api_failure_returns_none_gracefully(self):
        with mock.patch.object(server.tachibana_api, "get_daily_history", side_effect=RuntimeError("boom")):
            result = server._tachibana_daily_history_cached("7203", trading_date="2026-09-15")
        self.assertIsNone(result)


class TachibanaDailyArraysUsesHistoricalCacheTests(unittest.TestCase):
    """_tachibana_daily_arrays()：日足履歴取得元がhistorical cache経由になっても、
    最終的な出力（当日分合成後の配列）はPhase3までと完全に同一であること（R3・R10）。"""

    def setUp(self):
        server._CACHE_STORE.pop("daily_history:2026-09-15:7203", None)

    def test_output_unchanged_when_backed_by_historical_cache(self):
        hist = make_hist([(f"2020-01-{d:02d}", 100 + d) for d in range(1, 26)])
        live = {"t": 130.0, "open": 129.0, "high": 131.0, "low": 128.0, "volume": 5000}
        with mock.patch.object(server.tachibana_api, "get_daily_history", return_value=hist):
            result = server._tachibana_daily_arrays("7203", live_quote=live, trading_date="2026-09-15")
        expected_closes = [r["close"] for r in hist] + [130.0]
        self.assertEqual(result[0], expected_closes)  # closes配列に当日分が正しく合成されている

    def test_second_call_same_day_does_not_call_api_again(self):
        hist = make_hist([(f"2020-01-{d:02d}", 100 + d) for d in range(1, 26)])
        live = {"t": 130.0, "open": 129.0, "high": 131.0, "low": 128.0, "volume": 5000}
        with mock.patch.object(server.tachibana_api, "get_daily_history", return_value=hist) as mock_gdh:
            server._tachibana_daily_arrays("7203", live_quote=live, trading_date="2026-09-15")
            server._tachibana_daily_arrays("7203", live_quote=live, trading_date="2026-09-15")
        mock_gdh.assert_called_once()  # historical warmup済みなら2回目はTachibanaへ行かない


class RunMorningMarketWarmupTests(unittest.TestCase):
    """run_morning_market_warmup()：diagnostics・idempotency・部分失敗時の継続・
    281件への無制限フォールバック無しを検証する（R4・R5・R9）。"""

    def _watchlist(self, codes):
        return [{"code": c, "name": f"銘柄{c}", "market": "JP"} for c in codes]

    def setUp(self):
        for c in ["W1", "W2", "W3"]:
            server._CACHE_STORE.pop(f"daily_history:2026-09-15:{c}", None)

    def test_diagnostics_shape_and_ready_status(self):
        wl = self._watchlist(["W1", "W2"])
        with mock.patch.object(server.investment_db, "list_watchlist", return_value=wl), \
             mock.patch.object(server, "_jst_today_date_str", return_value="2026-09-15"), \
             mock.patch.object(server, "run_momentum_stage1", return_value={"rows": {"W1": {}}, "scanFailed": False}), \
             mock.patch.object(server, "prefetch_market_data_for_watchlist", return_value={"errors": 0}), \
             mock.patch.object(server, "_tachibana_daily_history_cached",
                                return_value=make_hist([(f"2020-01-{d:02d}", 100 + d) for d in range(1, 26)])):
            diag = server.run_morning_market_warmup("db_url", "user")
        self.assertEqual(diag["status"], "READY")
        self.assertEqual(diag["symbols_requested"], 2)
        self.assertEqual(diag["historical_success"], 2)
        self.assertTrue(diag["stage1_ready"])
        self.assertTrue(diag["market_data_ready"])
        self.assertIn("trading_date", diag)
        self.assertIn("total_elapsed_ms", diag)

    def test_idempotent_second_call_is_all_cache_hits(self):
        """指示書STEP12：同じtrading_dateでの再実行は、freshなhistorical cacheがある銘柄を
        再取得しない（2回目はhistorical_network_requests=0）。"""
        wl = self._watchlist(["W1", "W2"])
        hist = make_hist([(f"2020-01-{d:02d}", 100 + d) for d in range(1, 26)])
        with mock.patch.object(server.investment_db, "list_watchlist", return_value=wl), \
             mock.patch.object(server, "_jst_today_date_str", return_value="2026-09-15"), \
             mock.patch.object(server, "run_momentum_stage1", return_value={"rows": {}, "scanFailed": False}), \
             mock.patch.object(server, "prefetch_market_data_for_watchlist", return_value={"errors": 0}), \
             mock.patch.object(server.tachibana_api, "get_daily_history", return_value=hist) as mock_gdh:
            diag1 = server.run_morning_market_warmup("db_url", "user")
            diag2 = server.run_morning_market_warmup("db_url", "user")
        self.assertEqual(diag1["historical_network_requests"], 2)
        self.assertEqual(diag2["historical_network_requests"], 0)
        self.assertEqual(diag2["historical_cache_hits"], 2)
        self.assertEqual(mock_gdh.call_count, 2)  # 1回目分だけ、2回目はゼロ

    def test_partial_failure_for_some_symbols_still_continues(self):
        wl = self._watchlist(["W1", "W2", "W3"])

        def fake_hist(code, trading_date=None):
            if code == "W2":
                return None
            return make_hist([(f"2020-01-{d:02d}", 100 + d) for d in range(1, 26)])

        with mock.patch.object(server.investment_db, "list_watchlist", return_value=wl), \
             mock.patch.object(server, "_jst_today_date_str", return_value="2026-09-15"), \
             mock.patch.object(server, "run_momentum_stage1", return_value={"rows": {}, "scanFailed": False}), \
             mock.patch.object(server, "prefetch_market_data_for_watchlist", return_value={"errors": 0}), \
             mock.patch.object(server, "_tachibana_daily_history_cached", side_effect=fake_hist):
            diag = server.run_morning_market_warmup("db_url", "user")
        self.assertEqual(diag["status"], "PARTIAL")
        self.assertEqual(diag["historical_success"], 2)
        self.assertEqual(diag["historical_failed"], 1)
        self.assertEqual(diag["historical_network_requests"], 3)

    def test_no_watchlist_or_no_db_returns_failed_without_raising(self):
        with mock.patch.object(server, "investment_db", None):
            diag = server.run_morning_market_warmup("db_url", "user")
        self.assertEqual(diag["status"], "FAILED")

    def test_stage1_and_market_data_failure_does_not_raise(self):
        """指示書STEP11：warmupの一部（Stage1/Market Data）が失敗しても例外を投げず、
        historical部分は継続する。"""
        wl = self._watchlist(["W1"])
        with mock.patch.object(server.investment_db, "list_watchlist", return_value=wl), \
             mock.patch.object(server, "_jst_today_date_str", return_value="2026-09-15"), \
             mock.patch.object(server, "run_momentum_stage1", side_effect=RuntimeError("stage1 boom")), \
             mock.patch.object(server, "prefetch_market_data_for_watchlist", side_effect=RuntimeError("md boom")), \
             mock.patch.object(server, "_tachibana_daily_history_cached",
                                return_value=make_hist([(f"2020-01-{d:02d}", 100 + d) for d in range(1, 26)])):
            diag = server.run_morning_market_warmup("db_url", "user")  # 例外を投げない
        self.assertFalse(diag["stage1_ready"])
        self.assertFalse(diag["market_data_ready"])
        self.assertEqual(diag["historical_success"], 1)
        self.assertEqual(diag["status"], "PARTIAL")

    def test_status_readable_via_get_morning_warmup_status(self):
        wl = self._watchlist(["W1"])
        with mock.patch.object(server.investment_db, "list_watchlist", return_value=wl), \
             mock.patch.object(server, "_jst_today_date_str", return_value="2026-09-15"), \
             mock.patch.object(server, "run_momentum_stage1", return_value={"rows": {"W1": {}}, "scanFailed": False}), \
             mock.patch.object(server, "prefetch_market_data_for_watchlist", return_value={"errors": 0}), \
             mock.patch.object(server, "_tachibana_daily_history_cached",
                                return_value=make_hist([(f"2020-01-{d:02d}", 100 + d) for d in range(1, 26)])):
            server.run_morning_market_warmup("db_url", "restart_test_user")
        status = server.get_morning_warmup_status("restart_test_user")
        self.assertIsNotNone(status)
        self.assertEqual(status["status"], "READY")

    def test_no_status_before_first_run(self):
        self.assertIsNone(server.get_morning_warmup_status("never_run_user_xyz"))


class VolumeStage2ScoreUnchangedByWarmupTests(unittest.TestCase):
    """指示書STEP19：warmup経由（historical cache）でも、都度取得でも、
    Stage2判定・volume ratioは完全一致すること。"""

    def test_volume_stage2_detail_identical_with_warmed_cache(self):
        stage1_row = {"current": 3000, "open": 2950, "high": 3010, "low": 2940, "volume": 20000,
                       "changePct": 1.5, "turnover": 1e9}
        closes = [100 + i * 0.1 for i in range(30)]
        opens = [c - 0.5 for c in closes]
        highs = [c + 0.5 for c in closes]
        lows = [c - 1 for c in closes]
        volumes = [1000 + i * 10 for i in range(30)]
        hist = [{"date": f"2020-01-{i+1:02d}" if i < 26 else "2026-09-14", "open": o, "high": h, "low": l,
                 "close": c, "volume": v}
                for i, (o, h, l, c, v) in enumerate(zip(opens, highs, lows, closes, volumes))]

        with mock.patch.object(server.tachibana_api, "get_daily_history", return_value=hist):
            server._CACHE_STORE.pop("daily_history:2026-09-15:7203", None)
            server._CACHE_STORE.pop("daily_arrays:7203", None)
            with mock.patch.object(server, "_jst_today_date_str", return_value="2026-09-15"):
                # warmup経由：先にhistorical cacheへ投入
                server._tachibana_daily_history_cached("7203", trading_date="2026-09-15")
                result_warmed = server._volume_stage2_detail("7203", stage1_row)

        server._CACHE_STORE.pop("daily_history:2026-09-16:7203", None)
        server._CACHE_STORE.pop("daily_arrays:7203", None)
        with mock.patch.object(server.tachibana_api, "get_daily_history", return_value=hist):
            with mock.patch.object(server, "_jst_today_date_str", return_value="2026-09-16"):
                # 都度取得（warmupなし）：日付を変えてhistorical cacheをコールドにする
                result_cold = server._volume_stage2_detail("7203", stage1_row)

        self.assertEqual(result_warmed, result_cold)


if __name__ == "__main__":
    unittest.main()
