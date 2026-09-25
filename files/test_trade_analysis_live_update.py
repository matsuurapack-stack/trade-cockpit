# トレード分析 リアルタイム自動更新 実装指示（2026-09-12）の回帰テスト。
#
# FAST UPDATE用の軽量スナップショット（GET /api/trade-analysis/live）のロジックを検証する。
# analyze_stock()（HEAVY）は一切呼ばないこと、既存entry_score/entry_state計算を変更しないこと、
# API負荷対策（日足履歴のキャッシュ）が効いていることを中心に見る。
#
# 実行方法： cd files && python -m unittest test_trade_analysis_live_update -v

import unittest
from unittest import mock

import server


def make_comp(momentum=0, vwap=0, five_min=0, market_rel=0, volume=0, **extra):
    c = {"momentum": momentum, "vwap": vwap, "fiveMinStructure": five_min,
         "marketRelative": market_rel, "volume": volume, "negativeCatalysts": []}
    c.update(extra)
    return c


class LightEntryConditionsTests(unittest.TestCase):
    """_light_entry_conditions()：5項目のうち何個「成立」しているかの軽量カウント。"""

    def test_all_five_passed(self):
        comp = make_comp(momentum=10, vwap=15, five_min=15, market_rel=10, volume=5)
        cond = server._light_entry_conditions(comp)
        self.assertEqual(cond, {"passed": 5, "total": 5})

    def test_none_passed(self):
        comp = make_comp()
        cond = server._light_entry_conditions(comp)
        self.assertEqual(cond, {"passed": 0, "total": 5})

    def test_partial_four_of_five(self):
        comp = make_comp(momentum=10, vwap=15, five_min=15, market_rel=10, volume=0)
        cond = server._light_entry_conditions(comp)
        self.assertEqual(cond, {"passed": 4, "total": 5})


class LightSnapshotFieldsTests(unittest.TestCase):
    """_light_snapshot_fields()：レスポンス整形（純粋関数）。既存entry_score/entry_stateは
    そのまま乗せるだけで一切変更しない。"""

    def test_basic_fields(self):
        row = {"current": 2624, "changePct": 2.1}
        comp = make_comp(momentum=10, vwap=15, five_min=15, market_rel=10, volume=5)
        snapshot = {"vwap": 2825.91, "aboveVwap": True}
        fields = server._light_snapshot_fields("4440", row, comp, "WAIT_PULLBACK", snapshot, 42.3, 2612.4, 4.2)
        self.assertEqual(fields["symbol"], "4440")
        self.assertEqual(fields["price"], 2624)
        self.assertEqual(fields["pct"], 2.1)
        self.assertEqual(fields["vwap"], 2825.91)
        self.assertEqual(fields["rsi"], 42.3)
        self.assertEqual(fields["short_ma"], 2612.4)
        self.assertEqual(fields["entry_state"], "WAIT_PULLBACK")
        self.assertEqual(fields["signal"], "WAIT_PULLBACK")
        self.assertEqual(fields["entry_conditions"], {"passed": 5, "total": 5})
        self.assertFalse(fields["risk"])
        self.assertEqual(fields["experience_score"], 4.2)

    def test_risk_state_flagged(self):
        row = {"current": 1000, "changePct": 8.5}
        comp = make_comp()
        fields = server._light_snapshot_fields("1234", row, comp, "CHASE_RISK", None, None, None, None)
        self.assertTrue(fields["risk"])

    def test_invalid_state_flagged_as_risk(self):
        row = {"current": 1000, "changePct": -3.0}
        comp = make_comp()
        fields = server._light_snapshot_fields("1234", row, comp, "INVALID", None, None, None, None)
        self.assertTrue(fields["risk"])

    def test_non_risk_state_not_flagged(self):
        row = {"current": 1000, "changePct": 1.0}
        comp = make_comp()
        fields = server._light_snapshot_fields("1234", row, comp, "WATCH", None, None, None, None)
        self.assertFalse(fields["risk"])

    def test_missing_snapshot_vwap_is_none(self):
        row = {"current": 1000, "changePct": 1.0}
        comp = make_comp()
        fields = server._light_snapshot_fields("1234", row, comp, "WATCH", None, None, None, None)
        self.assertIsNone(fields["vwap"])

    def test_none_rsi_and_short_ma_pass_through_as_none(self):
        row = {"current": 1000, "changePct": 1.0}
        comp = make_comp()
        fields = server._light_snapshot_fields("1234", row, comp, "WATCH", None, None, None, None)
        self.assertIsNone(fields["rsi"])
        self.assertIsNone(fields["short_ma"])


class CachedDailyArraysTests(unittest.TestCase):
    """_cached_daily_arrays()：API負荷対策（指示書9番）。TTL内は_tachibana_daily_arrays()を
    再度呼ばない。"""

    def setUp(self):
        server._CACHE_STORE.clear()

    def test_second_call_within_ttl_uses_cache_not_refetch(self):
        with mock.patch.object(server, "_tachibana_daily_arrays") as mock_fetch:
            mock_fetch.return_value = ([1, 2, 3], [1, 2, 3], [1, 2, 3], [1, 2, 3], [100, 100, 100])
            r1 = server._cached_daily_arrays("4440", 90)
            r2 = server._cached_daily_arrays("4440", 90)
            self.assertEqual(r1, r2)
            mock_fetch.assert_called_once()

    def test_stale_fallback_when_refetch_fails(self):
        with mock.patch.object(server, "_tachibana_daily_arrays") as mock_fetch:
            mock_fetch.return_value = ([1, 2, 3], [1, 2, 3], [1, 2, 3], [1, 2, 3], [100, 100, 100])
            server._cached_daily_arrays("4440", -1)  # 即失効するTTLで初回キャッシュ
            mock_fetch.return_value = None  # 2回目は取得失敗
            result = server._cached_daily_arrays("4440", -1)
            self.assertIsNotNone(result)  # 失効していても直近キャッシュにフォールバック

    def test_none_when_never_cached_and_fetch_fails(self):
        with mock.patch.object(server, "_tachibana_daily_arrays") as mock_fetch:
            mock_fetch.return_value = None
            result = server._cached_daily_arrays("9999", 90)
            self.assertIsNone(result)


class ComputeLightTradeAnalysisSnapshotTests(unittest.TestCase):
    """compute_light_trade_analysis_snapshot()：I/Oラッパー全体の統合的な振る舞い。
    analyze_stock()を一切呼ばないこと（HEAVY分析との分離）を確認する。"""

    def test_returns_none_when_symbol_not_in_stage1(self):
        with mock.patch.object(server, "run_momentum_stage1", return_value={"rows": {}, "nikkeiChangePct": 0.0}):
            result = server.compute_light_trade_analysis_snapshot("postgres://x", "user", "9999")
        self.assertIsNone(result)

    def test_does_not_call_analyze_stock(self):
        stage1 = {"rows": {"4440": {"current": 2624, "changePct": 2.1, "code": "4440"}}, "nikkeiChangePct": -0.5}
        with mock.patch.object(server, "run_momentum_stage1", return_value=stage1), \
             mock.patch.object(server, "get_fast_quotes", return_value=({}, {})), \
             mock.patch.object(server, "investment_db") as mock_db, \
             mock.patch.object(server, "_volume_stage2_detail", return_value=None), \
             mock.patch.object(server, "_intraday_stock_snapshot", return_value={"dataStatus": "failed"}), \
             mock.patch.object(server, "_cached_daily_arrays", return_value=None), \
             mock.patch.object(server, "build_trade_experience_summary_for_symbol", return_value={"experience_score": None, "similar": {}}), \
             mock.patch.object(server, "analyze_stock") as mock_analyze:
            mock_db.get_codes_with_auto_tag.return_value = set()
            mock_db.relevant_catalysts_for.return_value = []
            mock_db.upcoming_event_signals.return_value = {"signals": []}
            result = server.compute_light_trade_analysis_snapshot("postgres://x", "user", "4440")
        mock_analyze.assert_not_called()
        self.assertIsNotNone(result)
        self.assertEqual(result["symbol"], "4440")
        self.assertIn("updated_at", result)

    def test_existing_entry_score_logic_reused_unchanged(self):
        # 既存_classify_entry_state()のCHASE_RISK判定（volume_type==CLIMAX_UP等）に一切手を
        # 加えていないことを、entry_scoreが高くてもriskとして正しく検出できることで確認する。
        stage1 = {"rows": {"4440": {"current": 2624, "changePct": 9.0, "code": "4440", "marketRS": 5.0}},
                   "nikkeiChangePct": 0.0}
        stage2 = {"avgVolume20": 1000, "currentVolume": 5000, "rawVolumeRatio": 5.0,
                   "timeAdjustedVolumeRatio": 5.0, "distanceFromHighPct": 6.0, "aboveRecentHigh": True,
                   "makingNewLowToday": False, "gapDown": False}
        snapshot = {"current": 2624, "currentChangePct": 9.0, "vwap": 2600, "aboveVwap": True,
                    "fiveMinStructure": "higher_highs", "dataStatus": "ok"}
        with mock.patch.object(server, "run_momentum_stage1", return_value=stage1), \
             mock.patch.object(server, "get_fast_quotes", return_value=({}, {})), \
             mock.patch.object(server, "investment_db") as mock_db, \
             mock.patch.object(server, "_volume_stage2_detail", return_value=stage2), \
             mock.patch.object(server, "_intraday_stock_snapshot", return_value=snapshot), \
             mock.patch.object(server, "_cached_daily_arrays", return_value=None), \
             mock.patch.object(server, "build_trade_experience_summary_for_symbol", return_value={"experience_score": None, "similar": {}}):
            mock_db.get_codes_with_auto_tag.return_value = set()
            mock_db.relevant_catalysts_for.return_value = []
            mock_db.upcoming_event_signals.return_value = {"signals": []}
            result = server.compute_light_trade_analysis_snapshot("postgres://x", "user", "4440")
        self.assertEqual(result["entry_state"], "CHASE_RISK")
        self.assertTrue(result["risk"])


if __name__ == "__main__":
    unittest.main()
