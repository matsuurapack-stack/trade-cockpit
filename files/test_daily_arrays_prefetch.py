# Market Data高速化 Phase 3（Volume Stage2 / Tachibana日足取得高速化）の回帰テスト。
#
# _tachibana_daily_arrays()のlive_quote引き継ぎ（get_market_price個別呼び出しの排除）と
# prefetch_daily_arrays_for_watchlist()（ThreadPoolExecutorによる有界並列prefetch）を検証する。
#
# 実行方法： cd files && python -m unittest test_daily_arrays_prefetch -v

import unittest
from unittest import mock

import server


def make_hist(dates_closes):
    """[(date, close), ...] から get_daily_history() 相当のリストを作る。"""
    return [{"date": d, "open": c - 1, "high": c + 1, "low": c - 2, "close": c, "volume": 1000}
            for d, c in dates_closes]


class TachibanaDailyArraysLiveQuoteGoldenTests(unittest.TestCase):
    """_tachibana_daily_arrays()：live_quoteを渡した場合と、省略時（get_market_priceを
    個別に呼ぶ従来経路）で同じ結果になること（指示書STEP11・R9）。
    2026-09-15更新（Market Data Phase 4）：日足履歴の取得元がtrading_date scopedキャッシュ
    経由になったため、他テスト（test_morning_warmup.py等）との"daily_history:{実行日}:7203"
    キー衝突を避けるべく、固定のtrading_dateを明示的に渡してテスト間の独立性を保つ。"""

    _TEST_TRADING_DATE = "2099-01-01"  # 他テストの実行日と衝突しない固定値

    def setUp(self):
        server._CACHE_STORE.pop(f"daily_history:{self._TEST_TRADING_DATE}:7203", None)

    def _hist_missing_today(self):
        # 直近日が確実に「_TEST_TRADING_DATEではない」過去日。
        # len(hist) < 20 の早期returnを避けるため25日分用意する。
        return make_hist([(f"2020-01-{d:02d}", 100 + d) for d in range(1, 26)])

    def test_live_quote_matches_individual_get_market_price(self):
        hist = self._hist_missing_today()
        live = {"t": 130.0, "open": 129.0, "high": 131.0, "low": 128.0, "volume": 5000}

        with mock.patch.object(server.tachibana_api, "get_daily_history", return_value=hist):
            with mock.patch.object(server.tachibana_api, "get_market_price",
                                    return_value={"7203": live}) as mock_gmp:
                legacy = server._tachibana_daily_arrays("7203", trading_date=self._TEST_TRADING_DATE)
            mock_gmp.assert_called_once()  # 省略時は従来通りget_market_priceを呼ぶ

            server._CACHE_STORE.pop(f"daily_history:{self._TEST_TRADING_DATE}:7203", None)
            with mock.patch.object(server.tachibana_api, "get_market_price") as mock_gmp2:
                preloaded = server._tachibana_daily_arrays("7203", live_quote=live, trading_date=self._TEST_TRADING_DATE)
            mock_gmp2.assert_not_called()  # live_quote指定時はget_market_priceを呼ばない

        self.assertEqual(legacy, preloaded)

    def test_no_live_quote_and_no_preloaded_when_today_already_present(self):
        """既に当日分が日足履歴に含まれている場合は、live_quote/get_market_priceどちらも
        不要（分岐に入らない）——この場合は元々1回のAPI呼び出しで済んでいたため、Phase3の
        削減対象はあくまで「当日分が無い場合」に限られることの確認。"""
        today_str = self._TEST_TRADING_DATE
        hist = make_hist([(f"2020-01-{d:02d}", 100 + d) for d in range(1, 25)] + [(today_str, 105)])
        with mock.patch.object(server, "_jst_today_date_str", return_value=today_str):
            with mock.patch.object(server.tachibana_api, "get_daily_history", return_value=hist):
                with mock.patch.object(server.tachibana_api, "get_market_price") as mock_gmp:
                    result = server._tachibana_daily_arrays("7203", live_quote={"t": 999})
                mock_gmp.assert_not_called()
        self.assertIsNotNone(result)
        self.assertEqual(result[0][-1], 105)  # todayの終値がそのまま使われる（合成なし）


class VolumeStage2DetailUnchangedTests(unittest.TestCase):
    """_volume_stage2_detail()：stage1_rowからlive_quoteを組み立てて渡すようになったが、
    計算結果（volume ratio等）自体は変更していないこと。"""

    def test_stage1_row_adapts_to_live_quote_shape(self):
        """stage1_rowのキー名（current/open/high/low/volume）が、live_quoteが期待する
        キー名（t/open/high/low/volume）へ正しく変換されて渡ること。"""
        stage1_row = {"current": 3000, "open": 2950, "high": 3010, "low": 2940, "volume": 12345,
                       "changePct": 1.5, "turnover": 1e9}
        arrays = ([100] * 25, [99] * 25, [101] * 25, [98] * 25, [1000] * 25)
        with mock.patch.object(server, "_cached_daily_arrays", return_value=arrays) as mock_cached:
            server._volume_stage2_detail("7203", stage1_row)
        call_kwargs = mock_cached.call_args.kwargs
        self.assertEqual(call_kwargs["live_quote"],
                          {"t": 3000, "open": 2950, "high": 3010, "low": 2940, "volume": 12345})

    def test_no_stage1_current_means_no_live_quote(self):
        with mock.patch.object(server, "_cached_daily_arrays", return_value=None) as mock_cached:
            server._volume_stage2_detail("7203", {"current": None})
        self.assertIsNone(mock_cached.call_args.kwargs["live_quote"])

    def test_score_identical_with_and_without_prefetch(self):
        """指示書STEP11「Stage2判定が完全一致」：daily_arraysの中身が同じなら、prefetch
        経由でキャッシュ済みでも、都度取得でも_volume_stage2_detail()の結果は同一。"""
        stage1_row = {"current": 3000, "open": 2950, "high": 3010, "low": 2940, "volume": 20000,
                       "changePct": 1.5, "turnover": 1e9}
        closes = [100 + i * 0.1 for i in range(30)]
        opens = [c - 0.5 for c in closes]
        highs = [c + 0.5 for c in closes]
        lows = [c - 1 for c in closes]
        volumes = [1000 + i * 10 for i in range(30)]
        arrays = (closes, opens, highs, lows, volumes)

        with mock.patch.object(server, "_cached_daily_arrays", return_value=arrays):
            direct = server._volume_stage2_detail("7203", stage1_row)

        # prefetch経路：daily_arrays:{code}キャッシュへ直接投入してから同じ関数を呼ぶ
        server._cache_set("daily_arrays:7203", arrays)
        prefetched = server._volume_stage2_detail("7203", stage1_row)

        self.assertEqual(direct, prefetched)


class PrefetchDailyArraysForWatchlistTests(unittest.TestCase):
    """prefetch_daily_arrays_for_watchlist()：diagnostics・キャッシュ活用・部分失敗時の継続・
    無制限フォールバック無し・重複排除を検証する。"""

    def _watchlist(self, codes, market="JP"):
        return [{"code": c, "name": f"銘柄{c}", "market": market} for c in codes]

    def setUp(self):
        # 各テストでキャッシュを汚さないよう、対象キーをクリアしておく
        for c in ["A1", "A2", "A3", "B1", "B2", "US1"]:
            server._CACHE_STORE.pop(f"daily_arrays:{c}", None)

    def test_cache_hit_symbols_are_not_refetched(self):
        server._cache_set("daily_arrays:A1", ([100], [99], [101], [98], [1000]))
        wl = self._watchlist(["A1", "A2"])
        with mock.patch.object(server, "_tachibana_daily_arrays", return_value=([1], [1], [1], [1], [1])) as mock_fetch:
            diag = server.prefetch_daily_arrays_for_watchlist(wl, {}, max_workers=1)
        self.assertEqual(diag["cache_hits"], 1)
        self.assertEqual(diag["cache_misses"], 1)
        mock_fetch.assert_called_once()  # A1（fresh cache）は再取得しない、A2だけ

    def test_us_symbols_excluded(self):
        wl = self._watchlist(["A1"], market="JP") + self._watchlist(["US1"], market="US")
        with mock.patch.object(server, "_tachibana_daily_arrays", return_value=([1], [1], [1], [1], [1])):
            diag = server.prefetch_daily_arrays_for_watchlist(wl, {}, max_workers=1)
        self.assertEqual(diag["unique_symbols"], 1)  # US1は対象外

    def test_duplicate_codes_deduped(self):
        wl = self._watchlist(["A1", "A1", "A2"])
        with mock.patch.object(server, "_tachibana_daily_arrays", return_value=None):
            diag = server.prefetch_daily_arrays_for_watchlist(wl, {}, max_workers=1)
        self.assertEqual(diag["unique_symbols"], 2)

    def test_partial_failure_continues_and_records_diagnostics(self):
        wl = self._watchlist(["A1", "A2", "A3"])

        def fake_fetch(code, live_quote=None):
            if code == "A2":
                raise RuntimeError("boom")
            return ([1], [1], [1], [1], [1])

        with mock.patch.object(server, "_tachibana_daily_arrays", side_effect=fake_fetch):
            diag = server.prefetch_daily_arrays_for_watchlist(wl, {}, max_workers=1)
        self.assertEqual(diag["successful"], 2)
        self.assertEqual(diag["failed"], 1)
        self.assertIsNotNone(server._cache_get("daily_arrays:A1"))
        self.assertIsNotNone(server._cache_get("daily_arrays:A3"))
        self.assertIsNone(server._cache_get("daily_arrays:A2"))

    def test_remote_disconnect_classified_separately(self):
        wl = self._watchlist(["A1"])
        with mock.patch.object(server, "_tachibana_daily_arrays",
                                side_effect=Exception("('Connection aborted.', RemoteDisconnected('boom'))")):
            diag = server.prefetch_daily_arrays_for_watchlist(wl, {}, max_workers=1)
        self.assertEqual(diag["remote_disconnects"], 1)
        self.assertEqual(diag["failed"], 1)

    def test_total_failure_does_not_raise_and_does_not_mass_fallback(self):
        """指示書STEP8：prefetch全滅でも例外を投げず、diagnosticsに記録するだけ
        （呼び出し元へは"failed"件数として伝わるのみで、ここから281件への無制限個別再取得は
        発生しない——このprefetch関数自体が「一度だけの有界並列試行」であることの確認）。"""
        wl = self._watchlist([f"C{i}" for i in range(5)])
        with mock.patch.object(server, "_tachibana_daily_arrays", side_effect=RuntimeError("boom")):
            diag = server.prefetch_daily_arrays_for_watchlist(wl, {}, max_workers=1)
        self.assertEqual(diag["failed"], 5)
        self.assertEqual(diag["network_requests"], 5)  # 5回だけ（281回のような再帰的拡大はしない）

    def test_no_tachibana_api_returns_zeroed_diagnostics(self):
        wl = self._watchlist(["A1"])
        with mock.patch.object(server, "tachibana_api", None):
            diag = server.prefetch_daily_arrays_for_watchlist(wl, {})
        self.assertEqual(diag["successful"], 0)
        self.assertEqual(diag["failed"], 0)

    def test_empty_watchlist_returns_immediately(self):
        with mock.patch.object(server, "_tachibana_daily_arrays") as mock_fetch:
            diag = server.prefetch_daily_arrays_for_watchlist([], {})
        mock_fetch.assert_not_called()
        self.assertEqual(diag["unique_symbols"], 0)

    def test_default_worker_count_is_conservative(self):
        """STEP3実測（workers=8で失敗率93%）に基づき、既定値は並列化しない
        （DAILY_ARRAYS_PREFETCH_WORKERS=1）ことを固定する回帰テスト。"""
        self.assertEqual(server.DAILY_ARRAYS_PREFETCH_WORKERS, 1)

    def test_stage1_row_used_as_live_quote_in_prefetch(self):
        stage1_rows = {"A1": {"current": 500, "open": 495, "high": 505, "low": 490, "volume": 999}}
        wl = self._watchlist(["A1"])
        with mock.patch.object(server, "_tachibana_daily_arrays", return_value=None) as mock_fetch:
            server.prefetch_daily_arrays_for_watchlist(wl, stage1_rows, max_workers=1)
        _, kwargs = mock_fetch.call_args
        self.assertEqual(kwargs["live_quote"], {"t": 500, "open": 495, "high": 505, "low": 490, "volume": 999})


class ScoreEntryCandidatesDailyArraysWiringTests(unittest.TestCase):
    """_score_entry_candidates()がdaily arrays prefetchを呼び、失敗しても全体を落とさない
    こと（指示書STEP8・R8）。"""

    def test_prefetch_failure_is_non_fatal(self):
        if server.investment_db is None:
            self.skipTest("investment_db not available")
        watchlist = [{"code": "7203", "name": "トヨタ自動車", "market": "JP"}]
        with mock.patch.object(server.investment_db, "list_watchlist", return_value=watchlist), \
             mock.patch.object(server.investment_db, "get_codes_with_auto_tag", return_value=set()), \
             mock.patch.object(server, "run_momentum_stage1", return_value={"rows": {}, "nikkeiChangePct": 0}), \
             mock.patch.object(server, "prefetch_market_data_for_watchlist", return_value={}), \
             mock.patch.object(server, "get_entry_candidate_support_context", return_value=({}, {})), \
             mock.patch.object(server, "prefetch_daily_arrays_for_watchlist",
                                side_effect=RuntimeError("daily prefetch boom")) as mock_prefetch:
            result = server._score_entry_candidates("dummy_url", "dummy_user")  # 例外を投げない
        mock_prefetch.assert_called_once()
        self.assertEqual(result["entryReadyTop5"], [])


if __name__ == "__main__":
    unittest.main()
