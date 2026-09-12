# Sector Rotation / Capital Flow Engine（前提commit 9f396a1）の回帰テスト。
#
# 実行方法： cd files && python -m unittest test_sector_rotation -v

import unittest
from unittest import mock

import server


class SectorStateClassificationTests(unittest.TestCase):
    """指示書9番：Sector State判定。"""

    def test_leading(self):
        self.assertEqual(server.classify_sector_state(85), "LEADING")

    def test_improving(self):
        self.assertEqual(server.classify_sector_state(70), "IMPROVING")

    def test_neutral(self):
        self.assertEqual(server.classify_sector_state(50), "NEUTRAL")

    def test_weakening(self):
        self.assertEqual(server.classify_sector_state(35), "WEAKENING")

    def test_lagging(self):
        self.assertEqual(server.classify_sector_state(10), "LAGGING")

    def test_boundary_values(self):
        self.assertEqual(server.classify_sector_state(80), "LEADING")
        self.assertEqual(server.classify_sector_state(65), "IMPROVING")
        self.assertEqual(server.classify_sector_state(45), "NEUTRAL")
        self.assertEqual(server.classify_sector_state(30), "WEAKENING")
        self.assertEqual(server.classify_sector_state(29.9), "LAGGING")


class BreadthTests(unittest.TestCase):
    """指示書5番：breadth。"""

    def test_example_breadth(self):
        # 半導体製造装置6銘柄中5上昇 = 83%
        self.assertEqual(server.compute_sector_breadth(5, 6), 83.3)

    def test_zero_total_returns_none(self):
        self.assertIsNone(server.compute_sector_breadth(0, 0))

    def test_all_up(self):
        self.assertEqual(server.compute_sector_breadth(6, 6), 100.0)


class VolumeExpansionTests(unittest.TestCase):
    """指示書7番：volume。1.0未満は弱い、1.5以上は強い。"""

    def test_below_1_0_is_zero(self):
        self.assertEqual(server.classify_volume_expansion_score(0.8), 0.0)

    def test_at_or_above_1_5_is_max(self):
        self.assertEqual(server.classify_volume_expansion_score(1.5), 15.0)
        self.assertEqual(server.classify_volume_expansion_score(2.0), 15.0)

    def test_none_is_zero(self):
        self.assertEqual(server.classify_volume_expansion_score(None), 0.0)


class CapitalFlowTests(unittest.TestCase):
    """指示書1・11番：CAPITAL_FLOW_SCORE / CAPITAL_FLOW_DIRECTION。"""

    def test_capital_inflow(self):
        score = server.compute_capital_flow_score(80, 80, 80, 80, 80)
        self.assertGreater(score, 0)
        self.assertEqual(server.classify_capital_flow_direction(score), "INFLOW")

    def test_capital_outflow(self):
        score = server.compute_capital_flow_score(-80, -80, -80, -80, -80)
        self.assertLess(score, 0)
        self.assertEqual(server.classify_capital_flow_direction(score), "OUTFLOW")

    def test_neutral_flow(self):
        score = server.compute_capital_flow_score(0, 0, 0, 0, 0)
        self.assertEqual(server.classify_capital_flow_direction(score), "NEUTRAL")

    def test_score_capped_in_range(self):
        score = server.compute_capital_flow_score(200, 200, 200, 200, 200)
        self.assertLessEqual(score, 100.0)
        score2 = server.compute_capital_flow_score(-200, -200, -200, -200, -200)
        self.assertGreaterEqual(score2, -100.0)


class RotationDetectionTests(unittest.TestCase):
    """指示書10・12番：ROTATION DETECTED。"""

    def test_rotation_pair_detected(self):
        prev = {"sectors": {"半導体": {"score": 82}, "重工/防衛": {"score": 50}}}
        curr = {"sectors": {"半導体": {"score": 54}, "重工/防衛": {"score": 81}}}
        pairs = server.detect_rotation_pairs(prev, curr)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["from_sector"], "半導体")
        self.assertEqual(pairs[0]["to_sector"], "重工/防衛")
        self.assertEqual(pairs[0]["confidence"], "HIGH")

    def test_no_rotation_when_no_prev_snapshot(self):
        curr = {"sectors": {"半導体": {"score": 54}}}
        pairs = server.detect_rotation_pairs(None, curr)
        self.assertEqual(pairs, [])

    def test_no_rotation_when_changes_below_threshold(self):
        prev = {"sectors": {"半導体": {"score": 60}, "海運": {"score": 55}}}
        curr = {"sectors": {"半導体": {"score": 58}, "海運": {"score": 57}}}
        pairs = server.detect_rotation_pairs(prev, curr)
        self.assertEqual(pairs, [])


class SectorExhaustionTests(unittest.TestCase):
    """指示書20番：SECTOR_EXHAUSTION。"""

    def test_exhaustion_detected(self):
        history = [
            {"state": "LEADING", "breadth": 90, "volume_expansion": 15, "breakout_failures": 0},
            {"state": "LEADING", "breadth": 60, "volume_expansion": 8, "breakout_failures": 3},
        ]
        self.assertTrue(server.detect_sector_exhaustion(history))

    def test_no_exhaustion_when_not_previously_leading(self):
        history = [
            {"state": "NEUTRAL", "breadth": 50, "volume_expansion": 5, "breakout_failures": 1},
            {"state": "NEUTRAL", "breadth": 40, "volume_expansion": 3, "breakout_failures": 2},
        ]
        self.assertFalse(server.detect_sector_exhaustion(history))

    def test_no_exhaustion_when_metrics_improving(self):
        history = [
            {"state": "LEADING", "breadth": 60, "volume_expansion": 8, "breakout_failures": 3},
            {"state": "LEADING", "breadth": 90, "volume_expansion": 15, "breakout_failures": 0},
        ]
        self.assertFalse(server.detect_sector_exhaustion(history))

    def test_insufficient_history_is_false(self):
        self.assertFalse(server.detect_sector_exhaustion([{"state": "LEADING"}]))


class RelativeStrengthWeaknessTests(unittest.TestCase):
    """指示書16・17番：MARKET_UP_STOCK_DOWN / market down・stock strong。"""

    def test_market_up_stock_down_alert(self):
        self.assertTrue(server.detect_relative_weakness_alert(2.0, -0.5))

    def test_no_alert_when_stock_also_up(self):
        self.assertFalse(server.detect_relative_weakness_alert(2.0, 0.5))

    def test_relative_strength_alert(self):
        self.assertTrue(server.detect_relative_strength_alert(-1.5, 1.2))

    def test_no_relative_strength_alert_when_market_flat(self):
        self.assertFalse(server.detect_relative_strength_alert(-0.5, 1.2))


class SectorDivergenceTests(unittest.TestCase):
    """指示書18番：STOCK_UNDERPERFORMING_SECTOR（かなり重要）。"""

    def test_underperformance_detected(self):
        # 半導体セクター+2%、個別-1% → 差3% >= 閾値1.0
        self.assertTrue(server.detect_stock_underperforming_sector(2.0, -1.0))

    def test_no_underperformance_when_sector_weak(self):
        self.assertFalse(server.detect_stock_underperforming_sector(0.5, -1.0))

    def test_no_underperformance_when_stock_keeps_up(self):
        self.assertFalse(server.detect_stock_underperforming_sector(2.0, 1.5))


class SectorFlowScoreForEntryTests(unittest.TestCase):
    """指示書22番：ENTRY TOP5への補助SECTOR_FLOW_SCORE。既存ENTRY SCORE非破壊。"""

    def test_leading_positive(self):
        self.assertEqual(server.compute_sector_flow_score_for_entry("LEADING"), 8)

    def test_weakening_negative_relative(self):
        score = server.compute_sector_flow_score_for_entry("WEAKENING")
        self.assertLess(score, server.compute_sector_flow_score_for_entry("NEUTRAL"))

    def test_does_not_touch_entry_score_components(self):
        import inspect
        sig = inspect.signature(server.compute_sector_flow_score_for_entry)
        self.assertEqual(list(sig.parameters.keys()), ["sector_state"])


class RotationCandidateTests(unittest.TestCase):
    """指示書19番：ROTATION_CANDIDATE。"""

    def test_candidate_detected(self):
        result = server.detect_rotation_candidate("LEADING", "INFLOW", 10, 3)
        self.assertTrue(result)

    def test_no_candidate_when_weakening(self):
        result = server.detect_rotation_candidate("WEAKENING", "INFLOW", 10, 3)
        self.assertFalse(result)

    def test_no_candidate_when_single_stock(self):
        result = server.detect_rotation_candidate("LEADING", "INFLOW", 10, 1)
        self.assertFalse(result)


class StoryIntegrationTests(unittest.TestCase):
    """指示書24番：Story Engineとの統合。"""

    def test_sector_weakening_triggers_break_reason(self):
        story = {"sector_state_at_entry": "LEADING"}
        snapshot = {"price": 1000, "sector_rotation": {"state": "WEAKENING"}}
        reasons = server.detect_story_break(story, snapshot)
        self.assertTrue(any("セクター弱化" in r for r in reasons))

    def test_no_reason_when_sector_still_leading(self):
        story = {"sector_state_at_entry": "LEADING"}
        snapshot = {"price": 1000, "sector_rotation": {"state": "LEADING"}}
        reasons = server.detect_story_break(story, snapshot)
        self.assertFalse(any("セクター弱化" in r for r in reasons))

    def test_existing_break_reasons_unaffected(self):
        story = {"support": 2590}
        snapshot = {"price": 2580}
        reasons = server.detect_story_break(story, snapshot)
        self.assertIn("support割れ", reasons)


class TradeExperienceIntegrationTests(unittest.TestCase):
    """指示書36番：Trade Experience保存。"""

    def test_sync_populates_sector_fields(self):
        history = [{"id": 1, "code": "8035", "name": "東京エレクトロン", "closed_at": "2026-09-12T06:00:00+00:00",
                     "entry_price": 25000, "exit_price": 25500, "shares": 100, "gross_pnl": 50000, "pnl": 50000}]
        rotation_snapshot = {"sectors": {"半導体製造装置": {"state": "LEADING", "score": 82}},
                               "rotation_pairs": []}
        with mock.patch.object(server, "investment_db") as mock_db, \
             mock.patch.object(server, "compute_choruco_market_mode", return_value={"mode": "NORMAL", "event_risk_level": "LOW"}), \
             mock.patch.object(server, "build_cross_market_link", return_value={"primary_driver": None, "drivers": []}), \
             mock.patch.object(server, "build_sector_rotation_snapshot", return_value=rotation_snapshot):
            mock_db.list_trade_history.return_value = history
            mock_db.list_trade_decision_events.return_value = []
            mock_db.upsert_trade_experience_by_sync_key.return_value = {"id": 1}
            server.sync_trade_experiences_for_date("postgres://x", "user", "2026-09-12")
            call_args = mock_db.upsert_trade_experience_by_sync_key.call_args
        fields = call_args.args[3]
        self.assertEqual(fields["sector_at_entry"], "半導体製造装置")
        self.assertEqual(fields["sector_state_at_entry"], "LEADING")
        self.assertEqual(fields["sector_flow_score_at_entry"], 8)


class DailyReviewLearningTests(unittest.TestCase):
    """指示書35番：Daily Review学習。"""

    def test_build_sector_rotation_daily_learning_reports_best_rotation(self):
        snapshot = {
            "sectors": {"半導体": {"state": "WEAKENING", "score": 40}, "重工/防衛": {"state": "LEADING", "score": 85}},
            "rotation_pairs": [{"from_sector": "半導体", "to_sector": "重工/防衛", "confidence": "HIGH",
                                  "from_score_change": -30, "to_score_change": 33}],
        }
        with mock.patch.object(server, "investment_db") as mock_db, \
             mock.patch.object(server, "build_sector_rotation_snapshot", return_value=snapshot):
            mock_db.list_trade_experiences.return_value = []
            result = server.build_sector_rotation_daily_learning("postgres://x", "user", "2026-09-12")
        self.assertEqual(result["best_rotation"], "半導体 → 重工/防衛")
        self.assertEqual(len(result["rotation_pairs"]), 1)


class GetStockThemesTests(unittest.TestCase):
    """指示書3番：sector mapping。"""

    def test_known_semiconductor_equipment_stock(self):
        self.assertIn("半導体製造装置", server.get_stock_themes("8035"))

    def test_unknown_stock_returns_empty(self):
        self.assertEqual(server.get_stock_themes("9999"), [])


class RegressionCrossMarketAndChorucoUnaffectedTests(unittest.TestCase):
    """指示書16・17番：Cross-Market・Choruco Style非破壊確認。"""

    def test_build_cross_market_link_signature_unchanged(self):
        import inspect
        sig = inspect.signature(server.build_cross_market_link)
        self.assertEqual(list(sig.parameters.keys()), ["symbol", "sector", "market"])

    def test_compute_choruco_fit_unchanged_v1_still_works(self):
        # 既存compute_choruco_fit（v1）はSector Rotation追加後も無変更で動く。
        fit = server.compute_choruco_fit("ATTACK", "STRONG", "LOW", story_score=80)
        self.assertEqual(fit, 10.0)

    def test_compute_choruco_fit_v2_is_separate_function(self):
        breakdown = server.compute_choruco_fit_v2("ATTACK", "LOW", story_score=80,
                                                      cross_market_correlation=0.78, sector_state="LEADING")
        self.assertIn("sector_flow", breakdown)
        self.assertIn("total", breakdown)
        self.assertLessEqual(breakdown["total"], 10.0)

    def test_entry_score_components_unchanged(self):
        import inspect
        sig = inspect.signature(server._entry_score_components)
        self.assertEqual(list(sig.parameters.keys()),
                          ["row", "stage2", "snapshot", "auto_rs_current", "auto_sector_current",
                           "catalysts", "event_signals"])


if __name__ == "__main__":
    unittest.main()
