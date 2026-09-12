# Cross-Market Link Phase 2（前提commit 668b02e）の回帰テスト。
#
# 実行方法： cd files && python -m unittest test_cross_market_link -v

import unittest
from unittest import mock

import server


def make_series(n, start=100.0, step=0.0, noise=None):
    """テスト用の終値系列を作る（等差＋ノイズ）。"""
    noise = noise or [0.0] * n
    return [start + step * i + noise[i] for i in range(n)]


class PearsonCorrelationTests(unittest.TestCase):
    """server.pearson_correlation()：純粋関数の相関計算。"""

    def test_perfect_positive_correlation(self):
        xs = [1, 2, 3, 4, 5]
        ys = [2, 4, 6, 8, 10]
        self.assertAlmostEqual(server.pearson_correlation(xs, ys), 1.0, places=4)

    def test_perfect_negative_correlation(self):
        xs = [1, 2, 3, 4, 5]
        ys = [10, 8, 6, 4, 2]
        self.assertAlmostEqual(server.pearson_correlation(xs, ys), -1.0, places=4)

    def test_insufficient_sample_returns_none(self):
        self.assertIsNone(server.pearson_correlation([1, 2], [1, 2]))

    def test_zero_variance_returns_none(self):
        self.assertIsNone(server.pearson_correlation([1, 1, 1, 1], [1, 2, 3, 4]))


class LeadLagTests(unittest.TestCase):
    """server.compute_lead_lag()：指示書3番、0/5/10/15分の中で最も強い相関を採用する。"""

    def test_same_time_correlation_when_lag_zero_is_best(self):
        stock = [1, 2, 3, 4, 5, 6, 7, 8]
        driver = [1, 2, 3, 4, 5, 6, 7, 8]
        lag, corr, direction = server.compute_lead_lag(stock, driver)
        self.assertEqual(lag, 0)
        self.assertAlmostEqual(corr, 1.0, places=2)
        self.assertEqual(direction, "POSITIVE")

    def test_5min_lead_detected(self):
        # driverが1本先行：stock[t] == driver[t-1]
        driver = [1, 2, 3, 4, 5, 6, 7, 8, 9]
        stock = driver[:-1]  # stock[t]はdriver[t]と同じ値だが、driver配列でのインデックスは1つ後
        # stock[i] = driver[i] なので、driver[t-1]=stock[t]が成立するためにはstockをdriverの
        # 1本先の値列にする必要がある。ここでは明示的に構築する。
        driver_full = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        stock_full = driver_full[1:]  # stock[t] = driver[t+1] → driver[t]がstock[t+1]に一致＝driverが1本先行
        lag, corr, direction = server.compute_lead_lag(stock_full, driver_full, max_lag_bars=3)
        self.assertEqual(lag, 5)
        self.assertEqual(direction, "POSITIVE")

    def test_10min_lead_detected(self):
        # 単純な等差数列だと全lagで相関が1.0に張り付き判別できないため、非線形パターンを使う。
        pattern = [1, 3, 2, 5, 4, 8, 6, 2, 9, 3, 7, 5, 6, 1, 8, 4]
        driver_full = pattern
        stock_full = [0, 0] + pattern[:-2]  # stock[i] = driver[i-2]（driverが2本=10分先行）
        lag, corr, direction = server.compute_lead_lag(stock_full, driver_full, max_lag_bars=3)
        self.assertEqual(lag, 10)

    def test_15min_lead_detected(self):
        pattern = [1, 3, 2, 5, 4, 8, 6, 2, 9, 3, 7, 5, 6, 1, 8, 4]
        driver_full = pattern
        stock_full = [0, 0, 0] + pattern[:-3]  # stock[i] = driver[i-3]（driverが3本=15分先行）
        lag, corr, direction = server.compute_lead_lag(stock_full, driver_full, max_lag_bars=3)
        self.assertEqual(lag, 15)

    def test_insufficient_sample_returns_none_lag(self):
        lag, corr, direction = server.compute_lead_lag([1, 2], [1, 2])
        self.assertIsNone(lag)
        self.assertIsNone(corr)
        self.assertIsNone(direction)


class CrossMarketConfidenceTests(unittest.TestCase):
    def test_reuses_classify_pattern_confidence(self):
        self.assertEqual(server.classify_cross_market_confidence(4), "LOW")
        self.assertEqual(server.classify_cross_market_confidence(10), "MEDIUM")
        self.assertEqual(server.classify_cross_market_confidence(20), "HIGH")


class DriverRankingTests(unittest.TestCase):
    def test_highest_abs_correlation_wins(self):
        drivers = [
            {"driver": "sox", "correlation": 0.5, "confidence": "HIGH"},
            {"driver": "kospi", "correlation": -0.8, "confidence": "MEDIUM"},
        ]
        primary, secondary = server.rank_cross_market_drivers(drivers)
        self.assertEqual(primary["driver"], "kospi")
        self.assertEqual(secondary["driver"], "sox")

    def test_empty_list_returns_none(self):
        primary, secondary = server.rank_cross_market_drivers([])
        self.assertIsNone(primary)
        self.assertIsNone(secondary)

    def test_ties_broken_by_confidence(self):
        drivers = [
            {"driver": "a", "correlation": 0.7, "confidence": "LOW"},
            {"driver": "b", "correlation": 0.7, "confidence": "HIGH"},
        ]
        primary, _ = server.rank_cross_market_drivers(drivers)
        self.assertEqual(primary["driver"], "b")


class DivergenceTests(unittest.TestCase):
    """指示書6番：POSITIVE_DIVERGENCE/NEGATIVE_DIVERGENCE/NONE。"""

    def test_positive_divergence_example(self):
        # KOSPI -1.5%, 個別+0.5% → POSITIVE_DIVERGENCE
        result = server.classify_cross_market_divergence(-1.5, 0.5, "POSITIVE")
        self.assertEqual(result, "POSITIVE_DIVERGENCE")

    def test_negative_divergence_example(self):
        # SOX +2%, 個別-0.3% → NEGATIVE_DIVERGENCE
        result = server.classify_cross_market_divergence(2.0, -0.3, "POSITIVE")
        self.assertEqual(result, "NEGATIVE_DIVERGENCE")

    def test_no_divergence_when_aligned(self):
        result = server.classify_cross_market_divergence(1.5, 0.8, "POSITIVE")
        self.assertEqual(result, "NONE")

    def test_no_divergence_when_driver_move_small(self):
        result = server.classify_cross_market_divergence(0.2, -1.0, "POSITIVE")
        self.assertEqual(result, "NONE")

    def test_missing_data_is_none_divergence(self):
        self.assertEqual(server.classify_cross_market_divergence(None, 0.5, "POSITIVE"), "NONE")


class LeadingAlertTests(unittest.TestCase):
    """指示書7番：LEADING_MARKET_ALERT。"""

    def test_alert_triggers_when_all_conditions_met(self):
        result = server.detect_leading_market_alert(-1.5, 0.78, 0.1)
        self.assertTrue(result)

    def test_no_alert_when_stock_already_reacted(self):
        result = server.detect_leading_market_alert(-1.5, 0.78, -1.2)
        self.assertFalse(result)

    def test_no_alert_when_driver_move_small(self):
        result = server.detect_leading_market_alert(-0.3, 0.78, 0.1)
        self.assertFalse(result)

    def test_no_alert_when_correlation_low(self):
        result = server.detect_leading_market_alert(-1.5, 0.3, 0.1)
        self.assertFalse(result)


class CatchUpCandidateTests(unittest.TestCase):
    """指示書8番：CATCH_UP_CANDIDATE。"""

    def test_catch_up_detected(self):
        result = server.detect_catch_up_candidate(1.5, 0.1, 0.78)
        self.assertTrue(result)

    def test_no_catch_up_when_stock_already_moved(self):
        result = server.detect_catch_up_candidate(1.5, 1.2, 0.78)
        self.assertFalse(result)

    def test_no_catch_up_when_driver_barely_moved(self):
        result = server.detect_catch_up_candidate(0.5, 0.1, 0.78)
        self.assertFalse(result)


class CrossMarketScoreTests(unittest.TestCase):
    def test_catch_up_positive_score(self):
        score = server.compute_cross_market_score(False, True, "NONE", 0.7)
        self.assertGreater(score, 0)

    def test_leading_alert_negative_score(self):
        score = server.compute_cross_market_score(True, False, "NONE", 0.7)
        self.assertLess(score, 0)

    def test_score_capped_in_range(self):
        score = server.compute_cross_market_score(True, True, "NEGATIVE_DIVERGENCE", 0.95)
        self.assertGreaterEqual(score, -5.0)
        self.assertLessEqual(score, 5.0)


class SectorGatingTests(unittest.TestCase):
    """指示書1・18番：まず半導体・AI関連のみ。全56銘柄×全driverは禁止。"""

    def test_semiconductor_sector_eligible(self):
        self.assertTrue(server.is_cross_market_eligible_sector("半導体"))
        self.assertTrue(server.is_cross_market_eligible_sector("半導体製造装置"))

    def test_unrelated_sector_not_eligible(self):
        self.assertFalse(server.is_cross_market_eligible_sector("銀行業"))

    def test_none_sector_not_eligible(self):
        self.assertFalse(server.is_cross_market_eligible_sector(None))

    def test_known_semiconductor_codes_eligible_despite_sector_string(self):
        # 実データで発覚：東京エレクトロン等はTSE業種分類上「電気機器」であり「半導体」を
        # 含まないため、sector文字列だけでは判定できない。既知コードリストで救済する。
        for code in ("8035", "6857", "6146", "285A"):
            with self.subTest(code=code):
                self.assertTrue(server.is_cross_market_eligible_sector("電気機器", code=code))
                self.assertTrue(server.is_cross_market_eligible_sector("精密機器", code=code))

    def test_unknown_code_with_unrelated_sector_not_eligible(self):
        self.assertFalse(server.is_cross_market_eligible_sector("電気機器", code="9999"))

    def test_build_cross_market_link_gated_by_sector(self):
        result = server.build_cross_market_link("9999", sector="銀行業")
        self.assertIsNone(result["primary_driver"])
        self.assertEqual(result["drivers"], [])


class BuildCrossMarketLinkIntegrationTests(unittest.TestCase):
    """build_cross_market_link()：I/Oラッパー全体（yfinance呼び出しはモック）。"""

    def test_finds_primary_driver_with_synthetic_data(self):
        stock_bars = [{"close": c} for c in make_series(20, start=1000, step=1.0)]
        driver_bars = [{"close": c} for c in make_series(20, start=500, step=0.5)]

        def fake_cached_bars(symbol, ttl=None):
            if symbol == "8035.T":
                return stock_bars
            return driver_bars

        with mock.patch.object(server, "_cached_5m_bars", side_effect=fake_cached_bars):
            result = server.build_cross_market_link("8035", sector="半導体")
        self.assertIsNotNone(result["primary_driver"])
        self.assertIn(result["confidence"], ("LOW", "MEDIUM", "HIGH"))
        self.assertEqual(len(result["drivers"]), len(server.CROSS_MARKET_DRIVERS))

    def test_insufficient_stock_bars_returns_empty(self):
        with mock.patch.object(server, "_cached_5m_bars", return_value=[{"close": 100}]):
            result = server.build_cross_market_link("8035", sector="半導体")
        self.assertIsNone(result["primary_driver"])

    def test_does_not_call_analyze_stock_or_score_entry_candidates(self):
        # 負荷対策（指示書18番）：重い既存関数を呼ばないことの確認。
        stock_bars = [{"close": c} for c in make_series(20, start=1000, step=1.0)]
        with mock.patch.object(server, "_cached_5m_bars", return_value=stock_bars), \
             mock.patch.object(server, "_score_entry_candidates") as mock_heavy, \
             mock.patch.object(server, "analyze_stock", create=True) as mock_analyze:
            server.build_cross_market_link("8035", sector="半導体")
        mock_heavy.assert_not_called()
        mock_analyze.assert_not_called()


class StoryIntegrationTests(unittest.TestCase):
    """指示書9番：Choruco Story Engineへの統合。"""

    def test_driver_adverse_move_triggers_weakening_or_break(self):
        story = {"primary_driver": "KOSPI", "driver_corr": 0.78}
        snapshot = {"price": 3000, "cross_market": {"primary_driver": "KOSPI", "driver_recent_change_pct": -1.5,
                                                         "direction": "POSITIVE"}}
        reasons = server.detect_story_break(story, snapshot)
        self.assertTrue(any("KOSPI" in r for r in reasons))

    def test_story_broken_when_combined_with_break_reasons(self):
        story = {"primary_driver": "KOSPI", "driver_corr": 0.78}
        snapshot = {"price": 3000, "cross_market": {"primary_driver": "KOSPI", "driver_recent_change_pct": -1.5,
                                                         "direction": "POSITIVE"}}
        reasons = server.detect_story_break(story, snapshot)
        status = server.classify_story_status(80, reasons)  # スコアが高くても崩れ理由があればBROKEN
        self.assertEqual(status, "BROKEN")

    def test_no_driver_reason_when_driver_moves_favorably(self):
        story = {"primary_driver": "KOSPI", "driver_corr": 0.78}
        snapshot = {"price": 3000, "cross_market": {"primary_driver": "KOSPI", "driver_recent_change_pct": 1.5,
                                                         "direction": "POSITIVE"}}
        reasons = server.detect_story_break(story, snapshot)
        self.assertFalse(any("KOSPI" in r for r in reasons))

    def test_existing_story_break_reasons_unaffected(self):
        # 既存のsupport割れ等の判定はcross_market未提供でも変わらず動く（非破壊確認）。
        story = {"support": 2590}
        snapshot = {"price": 2580}
        reasons = server.detect_story_break(story, snapshot)
        self.assertIn("support割れ", reasons)


class TradeExperienceIntegrationTests(unittest.TestCase):
    """指示書14番：Trade Experience保存。"""

    def test_sync_populates_primary_driver_fields(self):
        history = [{"id": 1, "code": "8035", "name": "東京エレクトロン", "closed_at": "2026-09-12T06:00:00+00:00",
                     "entry_price": 25000, "exit_price": 25500, "shares": 100, "gross_pnl": 50000, "pnl": 50000}]
        cm_result = {"primary_driver": "KOSPI", "correlation": 0.78, "best_lag_minutes": 10,
                      "drivers": [{"label": "KOSPI", "recent_change_pct": 0.8}], "cross_market_score": 2.0}
        with mock.patch.object(server, "investment_db") as mock_db, \
             mock.patch.object(server, "compute_choruco_market_mode", return_value={"mode": "NORMAL", "event_risk_level": "LOW"}), \
             mock.patch.object(server, "build_cross_market_link", return_value=cm_result):
            mock_db.list_trade_history.return_value = history
            mock_db.list_trade_decision_events.return_value = []
            mock_db.upsert_trade_experience_by_sync_key.return_value = {"id": 1}
            server.sync_trade_experiences_for_date("postgres://x", "user", "2026-09-12")
            call_args = mock_db.upsert_trade_experience_by_sync_key.call_args
        fields = call_args.args[3]
        self.assertEqual(fields["primary_driver"], "KOSPI")
        self.assertEqual(fields["driver_corr_at_entry"], 0.78)
        self.assertEqual(fields["driver_lag_at_entry"], 10)
        self.assertEqual(fields["driver_state_at_entry"], "UP")


class DailyReviewLearningTests(unittest.TestCase):
    """指示書16番：Daily Review学習。"""

    def test_build_cross_market_daily_learning_aggregates_items(self):
        experiences = [
            {"symbol": "8035", "stock_name": "東京エレクトロン", "primary_driver": "KOSPI",
             "driver_corr_at_entry": 0.81, "driver_lag_at_entry": 10, "driver_state_at_entry": "DOWN",
             "result_class": "LOSS"},
            {"symbol": "6857", "stock_name": "アドバンテスト", "primary_driver": None, "result_class": "WIN"},
        ]
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_trade_experiences.return_value = experiences
            result = server.build_cross_market_daily_learning("postgres://x", "user", "2026-09-12")
        self.assertEqual(len(result["items"]), 1)  # primary_driverがNoneの行は除外
        self.assertEqual(result["items"][0]["symbol"], "8035")
        self.assertTrue(result["items"][0]["prediction_valid"])  # DOWN予測でLOSS＝予測的中

    def test_prediction_validity_up_win(self):
        self.assertTrue(server._cross_market_prediction_was_valid(
            {"driver_state_at_entry": "UP", "result_class": "WIN"}))

    def test_prediction_validity_up_loss_is_invalid(self):
        self.assertFalse(server._cross_market_prediction_was_valid(
            {"driver_state_at_entry": "UP", "result_class": "LOSS"}))

    def test_prediction_validity_none_when_no_driver_state(self):
        self.assertIsNone(server._cross_market_prediction_was_valid(
            {"driver_state_at_entry": None, "result_class": "WIN"}))


class RegressionChorucoStyleUnaffectedTests(unittest.TestCase):
    """指示書「既存Choruco Style非破壊」の直接確認。"""

    def test_build_choruco_market_mode_signature_unchanged(self):
        import inspect
        sig = inspect.signature(server.build_choruco_market_mode)
        self.assertEqual(list(sig.parameters.keys()),
                          ["us_market_score", "japan_breadth_score", "sector_rotation_score",
                           "volatility_score", "rates_fx_score", "entry_quality_score", "event_risk_score",
                           "force_defense_reasons"])

    def test_compute_choruco_position_multiplier_unchanged(self):
        m = server.compute_choruco_position_multiplier("ATTACK", "LOW")
        self.assertEqual(m, 1.00)

    def test_evaluate_trade_experience_unchanged(self):
        vitz = {"pattern_tags_json": ["WAIT_TO_ENTRY", "OVERSOLD_REVERSAL", "SHORT_MA_RECLAIM",
                                        "ROUND_NUMBER_RECLAIM", "VOLUME_EXPANSION", "AFTERNOON_MOMENTUM",
                                        "MOMENTUM_STOCK", "FULL_EXIT_PROFIT_TAKING"],
                 "max_adverse_excursion_pct": -2.96, "result_class": "WIN", "rule_compliance_score": 70}
        total, _ = server.evaluate_trade_experience(vitz)
        self.assertEqual(total, 94)


if __name__ == "__main__":
    unittest.main()
