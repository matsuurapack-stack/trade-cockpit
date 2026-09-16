# Choruco Style / ちょる子式 実装指示書（2026-09-12）の回帰テスト。
#
# 実行方法： cd files && python -m unittest test_choruco_style -v

import unittest
from unittest import mock

import server


class BuildChorucoMarketModeTests(unittest.TestCase):
    """server.build_choruco_market_mode()：MARKET MODE判定（指示書1〜5番）。"""

    def test_strong_market_yields_attack(self):
        result = server.build_choruco_market_mode(90, 90, 90, 90, 90, 90, 90)
        self.assertEqual(result["mode"], "ATTACK")
        self.assertGreaterEqual(result["score"], server.CHORUCO_MODE_ATTACK_THRESHOLD)

    def test_neutral_market_yields_normal(self):
        result = server.build_choruco_market_mode(55, 55, 55, 55, 55, 55, 55)
        self.assertEqual(result["mode"], "NORMAL")

    def test_weak_market_yields_defense(self):
        result = server.build_choruco_market_mode(10, 10, 10, 10, 10, 10, 10)
        self.assertEqual(result["mode"], "DEFENSE")

    def test_force_defense_overrides_high_score(self):
        result = server.build_choruco_market_mode(95, 95, 95, 95, 95, 95, 95,
                                                     force_defense_reasons=["VIX急騰"])
        self.assertEqual(result["mode"], "DEFENSE")
        self.assertTrue(result["force_defense"])

    def test_output_shape_has_required_keys(self):
        result = server.build_choruco_market_mode(60, 60, 60, 60, 60, 60, 60)
        for key in ("mode", "score", "confidence", "positive_factors", "negative_factors",
                     "event_penalty", "risk_penalty", "updated_at"):
            self.assertIn(key, result)

    def test_existing_entry_score_not_touched(self):
        # build_choruco_market_modeは既存_entry_score_componentsを一切呼ばない（引数はスコアのみ）
        import inspect
        sig = inspect.signature(server.build_choruco_market_mode)
        self.assertNotIn("entry_score", sig.parameters)


class DetectChorucoForceDefenseTests(unittest.TestCase):
    """指示書5番：強制DEFENSE条件。"""

    def test_extreme_event_forces_defense(self):
        reasons = server.detect_choruco_force_defense(event_risk_level="EXTREME")
        self.assertTrue(reasons)

    def test_vix_spike_forces_defense(self):
        reasons = server.detect_choruco_force_defense(vix_value=30, vix_prev=20)
        self.assertTrue(reasons)

    def test_calm_market_no_force_defense(self):
        reasons = server.detect_choruco_force_defense(vix_value=15, vix_prev=15, nikkei_futures_chg=0.2,
                                                          sox_chg=0.5, event_risk_level="LOW")
        self.assertEqual(reasons, [])

    def test_failed_break_surge_forces_defense(self):
        reasons = server.detect_choruco_force_defense(failed_break_ratio=0.6)
        self.assertTrue(reasons)


class EventRiskLevelTests(unittest.TestCase):
    """指示書6・7・8番：EVENT_RISK_LEVEL・ロット倍率。"""

    def test_far_event_is_low(self):
        self.assertEqual(server.classify_event_risk_level(72), "LOW")

    def test_24_to_48h_is_medium(self):
        self.assertEqual(server.classify_event_risk_level(30), "MEDIUM")

    def test_6_to_24h_is_high(self):
        self.assertEqual(server.classify_event_risk_level(10), "HIGH")

    def test_under_6h_is_extreme(self):
        self.assertEqual(server.classify_event_risk_level(3), "EXTREME")

    def test_critical_importance_bumps_one_level(self):
        # 30h（本来MEDIUM）でもcriticalならHIGHへ繰り上げ
        self.assertEqual(server.classify_event_risk_level(30, importance="critical"), "HIGH")

    def test_fomc_keyword_bumps_one_level(self):
        self.assertEqual(server.classify_event_risk_level(30, title="FOMC政策金利発表"), "HIGH")

    def test_no_event_is_low(self):
        self.assertEqual(server.classify_event_risk_level(None), "LOW")

    def test_lot_multiplier_mapping(self):
        self.assertEqual(server.event_risk_lot_multiplier("LOW"), 1.00)
        self.assertEqual(server.event_risk_lot_multiplier("MEDIUM"), 0.75)
        self.assertEqual(server.event_risk_lot_multiplier("HIGH"), 0.50)
        self.assertEqual(server.event_risk_lot_multiplier("EXTREME"), 0.25)

    def test_multiple_events_takes_strictest(self):
        events = [{"hours_to_event": 72}, {"hours_to_event": 3}]
        self.assertEqual(server.compute_event_risk_for_events(events), "EXTREME")


class PositionSizeMultiplierTests(unittest.TestCase):
    """指示書9〜12番：ロット調整。既存ENTRY SCOREは非破壊。"""

    def test_attack_low_event_full_multiplier(self):
        m = server.compute_choruco_position_multiplier("ATTACK", "LOW")
        self.assertEqual(m, 1.00)

    def test_defense_reduces_multiplier(self):
        m = server.compute_choruco_position_multiplier("DEFENSE", "LOW")
        self.assertLess(m, 1.00)

    def test_high_event_risk_reduces_lot(self):
        m_low = server.compute_choruco_position_multiplier("ATTACK", "LOW")
        m_high = server.compute_choruco_position_multiplier("ATTACK", "HIGH")
        self.assertLess(m_high, m_low)

    def test_momentum_stock_reduces_lot(self):
        m_plain = server.compute_choruco_position_multiplier("ATTACK", "LOW")
        m_momentum = server.compute_choruco_position_multiplier("ATTACK", "LOW", ["MOMENTUM_STOCK"])
        self.assertLess(m_momentum, m_plain)

    def test_failed_break_zeroes_multiplier(self):
        m = server.compute_choruco_position_multiplier("ATTACK", "LOW", ["FAILED_BREAK"])
        self.assertEqual(m, 0.0)

    def test_extreme_event_defense_mode_no_entry(self):
        shares, mult = server.compute_choruco_final_position_size(200, "DEFENSE", "EXTREME")
        self.assertEqual(shares, 0)

    def test_extreme_event_attack_mode_reduced_but_nonzero(self):
        # base=800（0.25倍=200株、100株単位に揃うベース値）で「0株に丸め潰されない」ことを確認する
        shares, mult = server.compute_choruco_final_position_size(800, "ATTACK", "EXTREME")
        self.assertGreater(shares, 0)
        self.assertLess(mult, 1.0)

    def test_lot_rounding_160_to_100(self):
        self.assertEqual(server.round_position_size_to_lot(160), 100)

    def test_lot_rounding_240_to_200(self):
        self.assertEqual(server.round_position_size_to_lot(240), 200)

    def test_lot_rounding_negative_is_zero(self):
        self.assertEqual(server.round_position_size_to_lot(-50), 0)

    def test_final_position_size_example_attack(self):
        shares, mult = server.compute_choruco_final_position_size(200, "ATTACK", "LOW")
        self.assertEqual(shares, 200)
        self.assertEqual(mult, 1.0)

    def test_final_position_size_example_defense(self):
        # 指示書11番の例：DEFENSE x0.5 = 100株（base_position_size=200株）
        shares, mult = server.compute_choruco_final_position_size(200, "DEFENSE", "LOW")
        self.assertEqual(shares, 100)
        self.assertEqual(mult, 0.5)


class StoryEngineTests(unittest.TestCase):
    """指示書13〜17番：STORY ENGINE。"""

    def test_story_score_full_marks(self):
        components = {"market_alignment": 100, "sector_alignment": 100, "technical_setup": 100,
                       "volume_confirmation": 100, "relative_strength": 100, "event_safety": 100,
                       "risk_reward": 100}
        self.assertEqual(server.compute_story_score(components), 100.0)

    def test_story_score_missing_components_treated_as_zero(self):
        self.assertEqual(server.compute_story_score({}), 0.0)
        self.assertEqual(server.compute_story_score(None), 0.0)

    def test_status_active_high_score(self):
        self.assertEqual(server.classify_story_status(78, []), "ACTIVE")

    def test_status_weakening_mid_score(self):
        self.assertEqual(server.classify_story_status(50, []), "WEAKENING")

    def test_status_broken_low_score(self):
        self.assertEqual(server.classify_story_status(20, []), "BROKEN")

    def test_break_reasons_force_broken_even_with_high_score(self):
        # スコアが高くても、明確な崩れ条件があれば即BROKEN
        self.assertEqual(server.classify_story_status(90, ["support割れ"]), "BROKEN")

    def test_detect_story_break_support_violation(self):
        story = {"support": 2590}
        snapshot = {"price": 2580}
        reasons = server.detect_story_break(story, snapshot)
        self.assertIn("support割れ", reasons)

    def test_detect_story_break_vwap_violation(self):
        story = {}
        snapshot = {"price": 2600, "vwap": 2650}
        reasons = server.detect_story_break(story, snapshot)
        self.assertIn("VWAP割れ", reasons)

    def test_detect_story_break_multiple_conditions(self):
        story = {"support": 2590}
        snapshot = {"price": 2580, "vwap": 2650, "short_ma": 2620, "entry_state": "CHASE_RISK"}
        reasons = server.detect_story_break(story, snapshot)
        self.assertGreaterEqual(len(reasons), 3)

    def test_detect_story_break_no_violation_when_data_healthy(self):
        story = {"support": 2500}
        snapshot = {"price": 2700, "vwap": 2650, "short_ma": 2600, "entry_state": "ENTRY_READY"}
        reasons = server.detect_story_break(story, snapshot)
        self.assertEqual(reasons, [])

    def test_market_mode_defense_change_triggers_break(self):
        story = {"market_mode_at_entry": "ATTACK"}
        snapshot = {"market_mode": "DEFENSE"}
        reasons = server.detect_story_break(story, snapshot)
        self.assertIn("地合いDEFENSE化", reasons)


class PriceActionOverrideTests(unittest.TestCase):
    """指示書29〜31番：材料出尽くし・PRICE_ACTION_OVERRIDE。"""

    def test_good_news_weak_price_detected(self):
        detected = server.detect_good_news_weak_price("positive", -1.5, "NEGATIVE_VOLUME")
        self.assertTrue(detected)

    def test_good_news_strong_price_not_flagged(self):
        detected = server.detect_good_news_weak_price("positive", 1.5, "POSITIVE_VOLUME")
        self.assertFalse(detected)

    def test_negative_news_not_flagged_by_this_rule(self):
        detected = server.detect_good_news_weak_price("negative", -1.5, "NEGATIVE_VOLUME")
        self.assertFalse(detected)

    def test_single_condition_alone_insufficient(self):
        # 出来高条件が揃わなければ検知しない（単独条件では判定しない）
        detected = server.detect_good_news_weak_price("positive", -1.5, "NEUTRAL_VOLUME")
        self.assertFalse(detected)

    def test_price_action_override_forces_wait(self):
        self.assertEqual(server.apply_price_action_override("POSITIVE", "WEAK"), "WAIT")

    def test_price_action_override_no_change_when_strong(self):
        self.assertEqual(server.apply_price_action_override("POSITIVE", "STRONG"), "POSITIVE")


class PerfectOrderTests(unittest.TestCase):
    """指示書32〜34番：パーフェクトオーダー・押し目判定。"""

    def test_perfect_order_true_when_ordered_and_rising(self):
        self.assertTrue(server.classify_perfect_order(110, 100, 90, ma5_prev=108, ma25_prev=99, ma75_prev=89))

    def test_perfect_order_false_when_order_broken(self):
        self.assertFalse(server.classify_perfect_order(90, 100, 110))

    def test_perfect_order_false_when_slope_negative(self):
        self.assertFalse(server.classify_perfect_order(110, 100, 90, ma5_prev=112, ma25_prev=99, ma75_prev=89))

    def test_perfect_order_missing_data_is_false(self):
        self.assertFalse(server.classify_perfect_order(None, 100, 90))

    def test_pullback_candidate_requires_perfect_order(self):
        self.assertFalse(server.classify_pullback_candidate(False, 100, 100, 99))

    def test_pullback_candidate_true_when_near_ma_and_support_ok(self):
        result = server.classify_pullback_candidate(True, 101, 100, 99, support=95, volume_ok=True, sector_strong=True)
        self.assertTrue(result)

    def test_pullback_candidate_false_when_below_support(self):
        result = server.classify_pullback_candidate(True, 90, 100, 99, support=95)
        self.assertFalse(result)


class ChorucoFitTests(unittest.TestCase):
    """指示書53・54番：CHORUCO FIT。"""

    def test_attack_strong_sector_low_event_high_story(self):
        fit = server.compute_choruco_fit("ATTACK", "STRONG", "LOW", story_score=80)
        self.assertEqual(fit, 10.0)

    def test_defense_weak_sector_extreme_event_low_story(self):
        fit = server.compute_choruco_fit("DEFENSE", "WEAKENING", "EXTREME", story_score=20)
        self.assertLessEqual(fit, 2.0)


class ChorucoDailyPerformanceTests(unittest.TestCase):
    """指示書39〜44番：ちょる子式スコア。「取引しなかった」を単純に失敗扱いしない。"""

    def test_good_defense_no_trade_on_defense_day(self):
        result = server.evaluate_choruco_daily_performance("DEFENSE", [])
        self.assertEqual(result["breakdown"]["market_mode_adaptation"], 30)
        self.assertTrue(any("GOOD_DEFENSE" in n for n in result["notes"]))

    def test_no_trade_not_treated_as_failure_score_max(self):
        result = server.evaluate_choruco_daily_performance("DEFENSE", [])
        self.assertEqual(result["total"], 100.0)

    def test_chase_entry_on_defense_day_penalized_heavily(self):
        trades = [{"pattern_tags_json": ["CHASE_RISK"], "gross_pnl_pct": 1.0}]
        result = server.evaluate_choruco_daily_performance("DEFENSE", trades)
        self.assertLess(result["breakdown"]["market_mode_adaptation"], 30)
        self.assertTrue(any("RULE_VIOLATION_HIGH" in n for n in result["notes"]))

    def test_missed_opportunity_penalty_is_mild(self):
        result = server.evaluate_choruco_daily_performance("ATTACK", [], missed_opportunity=True)
        # 減点は弱め（30点満点中20点＝-10点の弱い減点）
        self.assertEqual(result["breakdown"]["market_mode_adaptation"], 20)
        self.assertGreater(result["breakdown"]["market_mode_adaptation"], 0)

    def test_attack_day_with_trades_full_marks(self):
        trades = [{"pattern_tags_json": [], "gross_pnl_pct": 2.0}]
        result = server.evaluate_choruco_daily_performance("ATTACK", trades)
        self.assertEqual(result["breakdown"]["market_mode_adaptation"], 30)

    def test_high_event_risk_entry_penalized(self):
        trades = [{"event_risk_at_entry": "HIGH", "gross_pnl_pct": 1.0}]
        result = server.evaluate_choruco_daily_performance("NORMAL", trades)
        self.assertLess(result["breakdown"]["event_awareness"], 20)

    def test_total_capped_at_100(self):
        trades = [{"gross_pnl_pct": 5.0}]
        result = server.evaluate_choruco_daily_performance("ATTACK", trades)
        self.assertLessEqual(result["total"], 100.0)


class SectorFlowTests(unittest.TestCase):
    """指示書27番：CHORUCO_SECTOR_FLOW。"""

    def test_strong_sector_label(self):
        self.assertEqual(server.classify_sector_flow_label(2.5), "STRONG")

    def test_weakening_sector_label(self):
        self.assertEqual(server.classify_sector_flow_label(-2.5), "WEAKENING")

    def test_neutral_sector_label(self):
        self.assertEqual(server.classify_sector_flow_label(0.3), "NEUTRAL")

    def test_none_is_neutral(self):
        self.assertEqual(server.classify_sector_flow_label(None), "NEUTRAL")


class VitzChorucoRegressionTests(unittest.TestCase):
    """指示書64番：4440ヴィッツ事例で既存Trade Experienceと矛盾しないことを確認する。
    2574 WAIT → 2600 ENTRY → 2760 EXIT、既存のresult_class/execution_score計算
    （evaluate_trade_experience、Task E）はChoruco Style導入で一切変わらないことを確認する。"""

    def test_existing_evaluate_trade_experience_unchanged_by_choruco(self):
        vitz = {
            "pattern_tags_json": ["WAIT_TO_ENTRY", "OVERSOLD_REVERSAL", "SHORT_MA_RECLAIM",
                                    "ROUND_NUMBER_RECLAIM", "VOLUME_EXPANSION", "AFTERNOON_MOMENTUM",
                                    "MOMENTUM_STOCK", "FULL_EXIT_PROFIT_TAKING"],
            "max_adverse_excursion_pct": -2.96, "result_class": "WIN", "rule_compliance_score": 70,
        }
        total, breakdown = server.evaluate_trade_experience(vitz)
        self.assertEqual(total, 94)  # Task Eで確認済みの既存スコアと同じ

    def test_wait_at_2574_low_score_is_good_wait(self):
        # 2574時点はENTRY条件未達（story insufficient）だった、という指示書の記述と整合。
        result_class, _ = server.evaluate_wait_decision(30)
        self.assertEqual(result_class, "GOOD_WAIT")

    def test_post_entry_high_volatility_profit_taking_not_penalized(self):
        # 「急騰後、高ボラティリティのため利確は妥当」——GOOD_NEWS_WEAK_PRICEのような弱気
        # シグナルとして誤検知しないことを確認（ポジティブ材料が無い通常の値上がりのため）。
        detected = server.detect_good_news_weak_price(None, 5.0, "POSITIVE_VOLUME")
        self.assertFalse(detected)


class RegressionExistingUnaffectedTests(unittest.TestCase):
    """指示書「既存ENTRY SCOREやRule Engineを壊さない」の直接確認。"""

    def test_entry_score_components_signature_unchanged(self):
        # 2026-09-16更新：ENTRY_RISK_SCORE（cf528a7）・値幅余地/反転モメンタム選考（c388aeb）で
        # entry_risk/range_metrics/momentum_stateがオプション引数（デフォルトNone）として追加
        # された。既存7引数の並び・意味は無変更で、末尾に追加されているだけ（choruco importが
        # この関数に一切触れていないことの確認という本テストの目的は変わらない）。
        import inspect
        sig = inspect.signature(server._entry_score_components)
        self.assertEqual(list(sig.parameters.keys()),
                          ["row", "stage2", "snapshot", "auto_rs_current", "auto_sector_current",
                           "catalysts", "event_signals", "entry_risk", "range_metrics", "momentum_state"])

    def test_classify_entry_state_signature_unchanged(self):
        import inspect
        sig = inspect.signature(server._classify_entry_state)
        self.assertIn("entry_score", sig.parameters)

    def test_select_entry_ready_top5_unaffected_by_choruco_import(self):
        # 2026-09-16更新：値幅余地・反転モメンタム選考（c388aeb）でreversal_confirmed/
        # reversal_watchが戻り値へ追加された（3要素→5要素）。choruco importがこの関数に
        # 一切触れていないことの確認という本テストの目的は変わらない。
        candidates = [{"code": "1", "entryState": "ENTRY_READY", "entryScore": 80}]
        top5, watch, reversal_confirmed, reversal_watch, debug = server._select_entry_ready_top5(candidates)
        self.assertEqual(len(top5), 1)
        self.assertEqual(top5[0]["entryScore"], 80)


if __name__ == "__main__":
    unittest.main()
