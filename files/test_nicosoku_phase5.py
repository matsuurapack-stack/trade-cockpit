# にこそくX連携 Phase5 テスト（指示書27番）。
#
# Phase2〜4同様、実DBを必要としない形で28項目をカバーする。
#
# 実行方法： cd files && python -m unittest test_nicosoku_phase5 -v

import datetime
import unittest
from unittest import mock

import server


def _iso(dt):
    return dt.astimezone(datetime.timezone.utc).isoformat()


class SignalKindClassificationTests(unittest.TestCase):
    """1〜4. OBSERVATION/FORECAST/TECHNICAL_VIEW/EVENT_NOTICE判定（指示書1番）"""

    def test_observation(self):
        self.assertEqual(server.classify_signal_kind("銀行強い"), "OBSERVATION")

    def test_forecast(self):
        self.assertEqual(server.classify_signal_kind("このまま銀行優位が続きそう"), "FORECAST")

    def test_technical_view(self):
        self.assertEqual(server.classify_signal_kind("日経は5日線割れ"), "TECHNICAL_VIEW")

    def test_event_notice(self):
        self.assertEqual(server.classify_signal_kind("CPIは9/12 21:30発表予定"), "EVENT_NOTICE")


class ObservationExclusionTests(unittest.TestCase):
    """5. observationをprediction hit率から除外（指示書2番）"""

    def test_observation_excluded_from_predictive_rate(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        evaluations = [
            {"evaluation_status": "EVALUATED", "confirmed": True, "confirmation_score": 100,
             "evaluation_window": "30M", "signal_type": "SECTOR_ROTATION", "contradicted": False,
             "signal_kind": "OBSERVATION", "created_at": _iso(now)},
            {"evaluation_status": "EVALUATED", "confirmed": False, "confirmation_score": 0,
             "evaluation_window": "30M", "signal_type": "SECTOR_ROTATION", "contradicted": True,
             "signal_kind": "FORECAST", "created_at": _iso(now)},
        ]
        perf = server.aggregate_source_performance(evaluations, now=now)
        self.assertEqual(perf["observation_sample_count"], 1)
        self.assertEqual(perf["predictive_sample_count"], 1)
        self.assertEqual(perf["overall_confirmation_rate_predictive"], 0.0)  # OBSERVATIONのTrueは混ざらない
        self.assertEqual(perf["overall_confirmation_rate"], 0.5)  # v1（後方互換）は全件対象のまま

    def test_extraction_marks_observed_at_post(self):
        post = {"author_opinion_json": ["銀行強い"], "image_analysis_json": []}
        candidates = server._extract_signal_candidates_from_post(post, watchlist=[])
        self.assertTrue(candidates[0]["observed_at_post"])
        self.assertEqual(candidates[0]["signal_kind"], "OBSERVATION")

    def test_forecast_not_observed_at_post(self):
        post = {"author_opinion_json": ["銀行はこのまま強い動きが続きそう"], "image_analysis_json": []}
        candidates = server._extract_signal_candidates_from_post(post, watchlist=[])
        self.assertFalse(candidates[0]["observed_at_post"])


class SignalConfidenceTests(unittest.TestCase):
    """6. signal_confidence（指示書3番）"""

    def test_explicit_text_high_confidence(self):
        self.assertEqual(server.compute_signal_confidence("銀行は強い"), 0.95)

    def test_image_derived_medium_confidence(self):
        self.assertEqual(server.compute_signal_confidence("銀行強い", from_image=True), 0.75)

    def test_ambiguous_low_confidence(self):
        self.assertEqual(server.compute_signal_confidence("銀行が強いかもしれない"), 0.50)


class AuthorCertaintyTests(unittest.TestCase):
    """7. author_certainty（指示書4番）"""

    def test_high(self):
        self.assertEqual(server.classify_author_certainty("銀行はかなり強い"), "HIGH")

    def test_medium(self):
        self.assertEqual(server.classify_author_certainty("銀行は強そう"), "MEDIUM")

    def test_low(self):
        self.assertEqual(server.classify_author_certainty("もしかすると銀行が強い"), "LOW")

    def test_unknown(self):
        self.assertEqual(server.classify_author_certainty("銀行強い"), "UNKNOWN")


class SignalGroupingTests(unittest.TestCase):
    """8〜10. signal grouping / continuation / reversal（指示書5・6番）"""

    def test_no_prior_creates_primary(self):
        group_id, role = server.resolve_signal_group([], "BULLISH", datetime.datetime.now(datetime.timezone.utc))
        self.assertEqual(role, "PRIMARY")
        self.assertTrue(group_id)

    def test_same_direction_within_30min_is_confirmation(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        prior = [{"signal_group_id": "g1", "signal_direction": "BEARISH", "created_at": _iso(now - datetime.timedelta(minutes=8))}]
        group_id, role = server.resolve_signal_group(prior, "BEARISH", now)
        self.assertEqual(role, "CONFIRMATION")
        self.assertEqual(group_id, "g1")

    def test_same_direction_after_30min_is_continuation(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        prior = [{"signal_group_id": "g1", "signal_direction": "BEARISH", "created_at": _iso(now - datetime.timedelta(hours=1))}]
        group_id, role = server.resolve_signal_group(prior, "BEARISH", now)
        self.assertEqual(role, "CONTINUATION")
        self.assertEqual(group_id, "g1")

    def test_opposite_direction_is_reversal_new_group(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        prior = [{"signal_group_id": "g1", "signal_direction": "BULLISH", "created_at": _iso(now - datetime.timedelta(hours=1))}]
        group_id, role = server.resolve_signal_group(prior, "BEARISH", now)
        self.assertEqual(role, "REVERSAL")
        self.assertNotEqual(group_id, "g1")

    def test_dedup_within_post_by_kind_priority(self):
        # 同一(signal_type,target_type,target_key)でOBSERVATIONとFORECASTが両方出た場合、
        # FORECAST（予測性が高い）を優先する（DBのUNIQUE制約に合わせた設計）。
        post = {"author_opinion_json": ["銀行強い", "銀行はこのまま強い動きが続きそう"], "image_analysis_json": []}
        candidates = server._extract_signal_candidates_from_post(post, watchlist=[])
        bank = [c for c in candidates if c["target_key"] == "BANK"]
        self.assertEqual(len(bank), 1)
        self.assertEqual(bank[0]["signal_kind"], "FORECAST")


class SectorBreadthRelativeStrengthTests(unittest.TestCase):
    """11〜13. sector breadth / relative sector return / stock relative return（指示書9・10・11番）"""

    def test_sector_breadth(self):
        evaluation = {"target_type": "SECTOR", "target_key": "BANK", "baseline_value": 100.0,
                      "baseline_detail_json": [{"code": "1", "price": 100}, {"code": "2", "price": 100},
                                                {"code": "3", "price": 100}, {"code": "4", "price": 100},
                                                {"code": "5", "price": 100}]}
        with mock.patch.object(server, "get_stock_quotes") as mock_quotes:
            # 5銘柄中4銘柄が上昇 → breadth=0.8
            mock_quotes.return_value = {"1": {"t": 105}, "2": {"t": 102}, "3": {"t": 101}, "4": {"t": 103}, "5": {"t": 99}}
            with mock.patch.object(server, "_concurrent_topix_change_pct", return_value=0.0):
                captured = server.capture_signal_result("dummy", "local", evaluation, datetime.datetime.now(datetime.timezone.utc))
        self.assertEqual(captured["breadth"], 0.8)

    def test_relative_sector_return_vs_topix(self):
        evaluation = {"target_type": "SECTOR", "target_key": "BANK", "baseline_value": 100.0,
                      "baseline_detail_json": [{"code": "1", "price": 100}, {"code": "2", "price": 100}]}
        with mock.patch.object(server, "get_stock_quotes") as mock_quotes:
            mock_quotes.return_value = {"1": {"t": 102.2}, "2": {"t": 102.2}}  # セクター+2.2%
            with mock.patch.object(server, "_concurrent_topix_change_pct", return_value=2.0):  # TOPIX+2.0%
                captured = server.capture_signal_result("dummy", "local", evaluation, datetime.datetime.now(datetime.timezone.utc))
        self.assertAlmostEqual(captured["relative_return_pct"], 0.2, places=2)  # 「銀行が強い」を高評価しすぎない

    def test_stock_relative_return_vs_topix(self):
        evaluation = {"target_type": "STOCK", "target_key": "7203", "baseline_value": 2000.0}
        with mock.patch.object(server, "_fetch_raw_price_snapshot") as mock_fetch:
            mock_fetch.return_value = {"price": 2050.0, "captured_at": _iso(datetime.datetime.now(datetime.timezone.utc)),
                                        "source": "yfinance", "quality_hint": "INTRADAY"}
            with mock.patch.object(server, "_concurrent_topix_change_pct", return_value=1.0):
                captured = server.capture_signal_result("dummy", "local", evaluation, datetime.datetime.now(datetime.timezone.utc))
        # change_pct=2.5%、TOPIX+1.0% → 銘柄自体の強さ=1.5%
        self.assertAlmostEqual(captured["relative_return_pct"], 1.5, places=2)


class ConfirmationV2Tests(unittest.TestCase):
    """14〜15. confirmation_v2 bullish/bearish（指示書12番）"""

    def test_v2_bullish_confirmed(self):
        confirmed, contradicted, score = server.compute_signal_confirmation_v2(
            "BULLISH", 0.8, "MARKET", "FORECAST")
        self.assertTrue(confirmed)
        self.assertEqual(score, 100)

    def test_v2_bearish_confirmed(self):
        confirmed, contradicted, score = server.compute_signal_confirmation_v2(
            "BEARISH", -0.8, "MARKET", "FORECAST")
        self.assertTrue(confirmed)

    def test_v2_excludes_observation(self):
        confirmed, contradicted, score = server.compute_signal_confirmation_v2(
            "BULLISH", 0.8, "MARKET", "OBSERVATION")
        self.assertIsNone(confirmed)
        self.assertIsNone(score)

    def test_v2_low_breadth_downgrades_sector_confirmation(self):
        # breadth<0.5（一部銘柄だけの急騰）ならconfirmed_v2が却下されうる。
        confirmed_high, _, score_high = server.compute_signal_confirmation_v2(
            "BULLISH", 0.6, "SECTOR", "FORECAST", breadth=0.9)
        confirmed_low, _, score_low = server.compute_signal_confirmation_v2(
            "BULLISH", 0.6, "SECTOR", "FORECAST", breadth=0.2)
        self.assertLess(score_low, score_high)


class FalseUsefulSignalRateTests(unittest.TestCase):
    """16〜17. false signal / useful signal（指示書16・17番）"""

    def test_false_signal_rate(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        evaluations = [
            {"evaluation_status": "EVALUATED", "confirmed": True, "contradicted": False, "confirmation_score": 90,
             "evaluation_window": "30M", "signal_type": "X", "created_at": _iso(now)},
            {"evaluation_status": "EVALUATED", "confirmed": False, "contradicted": True, "confirmation_score": 0,
             "evaluation_window": "30M", "signal_type": "X", "created_at": _iso(now)},
        ]
        perf = server.aggregate_source_performance(evaluations, now=now)
        self.assertEqual(perf["false_signal_rate"], 0.5)

    def test_useful_signal_rate(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        evaluations = [
            {"evaluation_status": "EVALUATED", "confirmed": True, "confirmed_v2": True, "contradicted": False,
             "confirmation_score": 90, "evaluation_window": "30M", "signal_type": "X",
             "signal_kind": "FORECAST", "signal_confidence": 0.95, "created_at": _iso(now)},
            {"evaluation_status": "EVALUATED", "confirmed": False, "confirmed_v2": False, "contradicted": True,
             "confirmation_score": 0, "evaluation_window": "1H", "signal_type": "X",
             "signal_kind": "FORECAST", "signal_confidence": 0.95, "created_at": _iso(now)},
            # confidence不足→useful候補から除外
            {"evaluation_status": "EVALUATED", "confirmed": True, "confirmed_v2": True, "contradicted": False,
             "confirmation_score": 90, "evaluation_window": "30M", "signal_type": "X",
             "signal_kind": "FORECAST", "signal_confidence": 0.5, "created_at": _iso(now)},
        ]
        perf = server.aggregate_source_performance(evaluations, now=now)
        self.assertEqual(perf["useful_signal_rate"], 0.5)  # 2件中1件


class WilsonConfidenceIntervalTests(unittest.TestCase):
    """18. Wilson CI（指示書14番）"""

    def test_ci_brackets_point_estimate(self):
        low, high = server.wilson_confidence_interval(74, 100)
        self.assertLess(low, 0.74)
        self.assertGreater(high, 0.74)

    def test_ci_none_when_no_sample(self):
        low, high = server.wilson_confidence_interval(0, 0)
        self.assertIsNone(low)
        self.assertIsNone(high)

    def test_ci_narrower_with_more_samples(self):
        low1, high1 = server.wilson_confidence_interval(7, 10)
        low2, high2 = server.wilson_confidence_interval(70, 100)
        self.assertGreater(high1 - low1, high2 - low2)


class StatisticalConfidenceTests(unittest.TestCase):
    """19. statistical confidence（指示書13番）"""

    def test_low_sample(self):
        self.assertEqual(server.classify_statistical_confidence(5), "LOW")

    def test_medium_sample(self):
        self.assertEqual(server.classify_statistical_confidence(20), "MEDIUM")

    def test_high_sample_low_variance(self):
        scores = [80, 82, 78, 81, 79] * 10  # 分散が小さい
        self.assertEqual(server.classify_statistical_confidence(50, confirmation_scores=scores), "HIGH")

    def test_high_sample_high_variance_stays_medium(self):
        scores = ([0] * 25 + [100] * 25)  # 分散が非常に大きい
        self.assertEqual(server.classify_statistical_confidence(50, confirmation_scores=scores), "MEDIUM")


class SourceQualityScoreV3Tests(unittest.TestCase):
    """20. source_quality_score_v3（指示書15番）"""

    def test_v3_reflects_prediction_and_statistical_confidence(self):
        perf_good = {"total_evaluated": 30, "overall_confirmation_rate_predictive": 0.8,
                     "overall_confirmation_rate_v2": 0.8, "statistical_confidence": "HIGH",
                     "event_performance": {"exact_match_rate": 0.9}}
        perf_bad = {"total_evaluated": 30, "overall_confirmation_rate_predictive": 0.2,
                    "overall_confirmation_rate_v2": 0.2, "statistical_confidence": "LOW",
                    "event_performance": {"exact_match_rate": 0.1}}
        v3_good = server.compute_source_quality_score_v3(perf_good, timeliness_rate=0.7, consistency=0.9)
        v3_bad = server.compute_source_quality_score_v3(perf_bad, timeliness_rate=0.7, consistency=0.9)
        self.assertGreater(v3_good["score"], v3_bad["score"])
        self.assertFalse(v3_good["provisional"])

    def test_v1_v2_untouched_by_v3(self):
        perf = {"total_evaluated": 30, "overall_confirmation_rate": 0.8, "avg_confirmation_score": 80}
        v1 = server.compute_source_quality_score(perf)
        v2 = server.compute_source_quality_score_v2(perf)
        self.assertIsNotNone(v1["score"])
        self.assertIsNotNone(v2["score"])


class MarketRegimeTests(unittest.TestCase):
    """21. market regime（指示書7・8番）"""

    def test_trend_up_and_risk_on(self):
        state = {"nikkei_change_pct": 1.5, "sox_change_pct": 1.2, "vix": 15}
        regimes = server.classify_market_regime(state)
        self.assertIn("TREND_UP", regimes)
        self.assertIn("RISK_ON", regimes)

    def test_high_vol(self):
        state = {"nikkei_change_pct": -1.5, "sox_change_pct": -2.0, "vix": 30}
        regimes = server.classify_market_regime(state)
        self.assertIn("HIGH_VOL", regimes)
        self.assertIn("TREND_DOWN", regimes)
        self.assertIn("RISK_OFF", regimes)

    def test_empty_state_returns_empty(self):
        self.assertEqual(server.classify_market_regime({}), [])
        self.assertEqual(server.classify_market_regime(None), [])


class AlertGenerationTests(unittest.TestCase):
    """22〜24. alert生成 / cooldown / reversal alert（指示書19・20番）"""

    def _candidate(self, kind="FORECAST", confidence=0.9):
        return {"signal_kind": kind, "signal_confidence": confidence, "signal_direction": "BULLISH",
                "target_type": "SECTOR", "target_key": "BANK"}

    def _good_performance(self):
        return {"total_evaluated": 40, "overall_confirmation_rate": 0.7,
                "source_quality_score_v3": {"score": 70}}

    def test_alert_generated_when_conditions_met(self):
        ok = server.should_generate_social_signal_alert(self._candidate(), "PRIMARY", 65, self._good_performance())
        self.assertTrue(ok)

    def test_no_alert_for_confirmation_role(self):
        ok = server.should_generate_social_signal_alert(self._candidate(), "CONFIRMATION", 65, self._good_performance())
        self.assertFalse(ok)

    def test_no_alert_low_confidence(self):
        ok = server.should_generate_social_signal_alert(self._candidate(confidence=0.5), "PRIMARY", 65, self._good_performance())
        self.assertFalse(ok)

    def test_no_alert_low_relevance(self):
        ok = server.should_generate_social_signal_alert(self._candidate(), "PRIMARY", 10, self._good_performance())
        self.assertFalse(ok)

    def test_no_alert_insufficient_sample(self):
        low_perf = {"total_evaluated": 5, "overall_confirmation_rate": 0.9, "source_quality_score_v3": {"score": 90}}
        ok = server.should_generate_social_signal_alert(self._candidate(), "PRIMARY", 65, low_perf)
        self.assertFalse(ok)

    def test_no_alert_low_quality_v3(self):
        low_quality_perf = {"total_evaluated": 40, "overall_confirmation_rate": 0.7, "source_quality_score_v3": {"score": 10}}
        ok = server.should_generate_social_signal_alert(self._candidate(), "PRIMARY", 65, low_quality_perf)
        self.assertFalse(ok)

    def test_alert_cooldown_blocks_repeat(self):
        with mock.patch.object(server, "investment_db") as mock_db, \
             mock.patch.object(server, "get_social_source_performance", return_value=self._good_performance()):
            mock_db.list_recent_alerts_for_group.return_value = [{"id": 1}]  # cooldown中
            result = server.maybe_generate_social_signal_alert(
                "dummy_url", "local", {"post_id": "1", "text": "銀行強い", "url": "https://x"},
                self._candidate(), "g1", "PRIMARY", 65)
        self.assertIsNone(result)
        mock_db.create_social_signal_alert.assert_not_called()

    def test_reversal_can_alert_even_after_recent_confirmation_group(self):
        # REVERSALは新しいgroup_idを持つため、旧グループのcooldownとは無関係にalert可能。
        with mock.patch.object(server, "investment_db") as mock_db, \
             mock.patch.object(server, "get_social_source_performance", return_value=self._good_performance()):
            mock_db.list_recent_alerts_for_group.return_value = []  # 新groupなのでcooldown無し
            mock_db.create_social_signal_alert.return_value = {"id": 99}
            result = server.maybe_generate_social_signal_alert(
                "dummy_url", "local", {"post_id": "2", "text": "銀行失速", "url": "https://x"},
                self._candidate(), "g2-reversal", "REVERSAL", 65)
        self.assertIsNotNone(result)
        mock_db.create_social_signal_alert.assert_called_once()


class RecentSocialSignalsPhase5Tests(unittest.TestCase):
    """25. recent_social_market_signals拡張（指示書21番）"""

    def test_signals_carry_phase5_fields(self):
        now_iso = _iso(datetime.datetime.now(datetime.timezone.utc))
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_social_signals.return_value = [{
                "post_id": "1", "posted_at": now_iso, "importance": "HIGH", "categories_json": [],
                "text": "銀行は強含み継続を示唆", "facts_json": [], "author_opinion_json": [],
                "direct_mentions_json": [], "theme_related_json": [], "url": "https://x.com/1",
                "image_analysis_status": None,
            }]
            mock_db.list_watchlist.return_value = []
            mock_db.list_portfolio.return_value = []
            mock_db.get_market_source.return_value = {"last_success_at": now_iso}
            mock_db.list_social_signal_evaluations_since.return_value = [
                {"evaluation_status": "EVALUATED", "confirmed": True, "confirmation_score": 90,
                 "evaluation_window": "30M", "signal_type": "SECTOR_ROTATION", "target_type": "SECTOR",
                 "target_key": "BANK", "contradicted": False, "post_id": "1", "created_at": now_iso}
                for _ in range(10)
            ]
            mock_db.list_social_event_evaluations_since.return_value = []
            mock_db.list_social_signal_evaluations_for_post.return_value = [
                {"signal_kind": "FORECAST", "signal_confidence": 0.9, "author_certainty": "HIGH",
                 "signal_group_id": "g1", "market_regime_json": ["RISK_OFF"]},
            ]
            signals = server.get_recent_social_market_signals("dummy_url", "local")
        self.assertEqual(len(signals), 1)
        s = signals[0]
        self.assertEqual(s["signal_kind"], "FORECAST")
        self.assertEqual(s["signal_confidence"], 0.9)
        self.assertEqual(s["author_certainty"], "HIGH")
        self.assertEqual(s["signal_group_id"], "g1")
        self.assertEqual(s["market_regime"], ["RISK_OFF"])
        self.assertIsNotNone(s["historical_performance"])
        self.assertEqual(s["historical_performance"]["sample_count"], 10)


class DiagnosticsPhase5Tests(unittest.TestCase):
    """26. diagnostics（指示書24番）"""

    def test_diagnostics_includes_phase5_fields(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.get_market_source.return_value = None
            mock_db.list_recent_social_posts.return_value = []
            mock_db.count_social_signal_evaluations.return_value = 0
            mock_db.count_duplicate_signal_groups_since.return_value = 2
            mock_db.count_social_signal_alerts_since.return_value = 3
            mock_db.count_high_confidence_signals_since.return_value = 5
            diag = server.nicosoku_diagnostics("dummy_url", "local")
        self.assertEqual(diag["duplicate_signal_groups"], 2)
        self.assertEqual(diag["alerts_generated_today"], 3)
        self.assertEqual(diag["high_confidence_signals_today"], 5)
        self.assertIn("pending_signal_evaluations", diag)


class BackfillDryRunPhase5Tests(unittest.TestCase):
    """27. backfill dry_run（指示書25番：低精度は書き込まない＝dry_runは常に書き込まない）"""

    def test_dry_run_never_writes_even_with_signal_kind_fields(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_analyzed_social_posts_without_evaluations.return_value = [
                {"post_id": "1", "author_opinion_json": ["半導体は弱い動きが継続しそう"], "image_analysis_json": [],
                 "categories_json": ["SECTOR_ROTATION"], "posted_at": _iso(datetime.datetime.now(datetime.timezone.utc))},
            ]
            mock_db.list_watchlist.return_value = []
            result = server.backfill_social_signal_evaluations("dummy_url", "local", limit=10, dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertGreaterEqual(result["candidate_signals"], 1)
        mock_db.create_social_signal_evaluations.assert_not_called()
        mock_db.create_social_signal_alert.assert_not_called()


class Phase4CompatibilityTests(unittest.TestCase):
    """28. Phase4互換性"""

    def test_v1_and_v2_score_functions_still_work(self):
        perf = {"total_evaluated": 25, "overall_confirmation_rate": 0.75, "avg_confirmation_score": 75,
                "quality_breakdown": {"EXACT": 0.5, "NEAR_EXACT": 0.5}, "event_performance": {"exact_match_rate": 0.8}}
        v1 = server.compute_source_quality_score(perf)
        v2 = server.compute_source_quality_score_v2(perf)
        self.assertIsNotNone(v1["score"])
        self.assertIsNotNone(v2["score"])

    def test_legacy_rows_without_signal_kind_still_aggregate(self):
        # Phase3/4時代の行（signal_kind列が無い＝None）でも集計が壊れないこと。
        now = datetime.datetime.now(datetime.timezone.utc)
        evaluations = [{"evaluation_status": "EVALUATED", "confirmed": True, "confirmation_score": 80,
                        "evaluation_window": "30M", "signal_type": "SECTOR_ROTATION", "contradicted": False,
                        "created_at": _iso(now)}]
        perf = server.aggregate_source_performance(evaluations, now=now)
        self.assertEqual(perf["predictive_sample_count"], 1)  # kind不明はpredictive側に残す
        self.assertEqual(perf["observation_sample_count"], 0)

    def test_confirmation_v1_engine_unchanged(self):
        confirmed, contradicted, score = server.compute_signal_confirmation("BULLISH", 0.8, "MARKET")
        self.assertTrue(confirmed)
        self.assertEqual(score, 100)


if __name__ == "__main__":
    unittest.main()
