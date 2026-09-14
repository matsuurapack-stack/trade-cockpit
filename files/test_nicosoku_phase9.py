# Market Intelligence Phase9 テスト（指示書42番：最低34項目）。
#
# Phase2〜8同様、実DBを必要としない形でカバーする。DB操作が必要な箇所は
# mock.patch.object(server, "investment_db") で完全にモックする。
#
# 実行方法： cd files && python -m unittest test_nicosoku_phase9 -v

import datetime
import unittest
from unittest import mock

import server
from test_support_source_inspect import get_fresh_source


def _iso(dt):
    return dt.astimezone(datetime.timezone.utc).isoformat()


class MaterialQualityScoreTests(unittest.TestCase):
    """1. material_quality_score（指示書2番）"""

    def test_official_confirmed_high_impact_scores_high(self):
        event = {"confidence_level": "OFFICIAL_CONFIRMED", "independent_source_count": 3,
                 "impact_score": 90, "material_magnitude": 10, "event_type": "BUYBACK"}
        score = server.compute_material_quality_score(event)
        self.assertGreaterEqual(score, 70)

    def test_unverified_social_only_scores_low(self):
        event = {"confidence_level": "UNVERIFIED", "independent_source_count": 0,
                 "impact_score": 10, "material_magnitude": None, "event_type": "OTHER"}
        score = server.compute_material_quality_score(event)
        self.assertLess(score, 30)

    def test_good_historical_performance_boosts_score(self):
        event = {"confidence_level": "SINGLE_RELIABLE_SOURCE", "independent_source_count": 1,
                 "impact_score": 50, "event_type": "BUYBACK"}
        perf_good = {"BUYBACK": {"sample_count": 20, "positive_rate": 0.9}}
        perf_bad = {"BUYBACK": {"sample_count": 20, "positive_rate": 0.1}}
        self.assertGreater(server.compute_material_quality_score(event, perf_good),
                            server.compute_material_quality_score(event, perf_bad))

    def test_direction_independent_of_material_quality(self):
        # 指示書15番：material_quality=高 かつ event_direction=NEGATIVEも成立する。
        event = {"confidence_level": "OFFICIAL_CONFIRMED", "independent_source_count": 3,
                 "impact_score": 90, "material_magnitude": 15, "event_type": "CAPITAL_RAISE"}
        score = server.compute_material_quality_score(event)
        direction, _ = server.classify_event_direction("CAPITAL_RAISE", "第三者割当増資を実施")
        self.assertGreaterEqual(score, 70)
        self.assertEqual(direction, "NEGATIVE")


class ReactionQualityScoreTests(unittest.TestCase):
    """2. reaction_quality_score（指示書3番）——dead-cat popの検出を含む"""

    def test_persistent_positive_scores_high(self):
        returns = {"5M": 3.0, "30M": 5.0, "1H": 6.0, "CLOSE": 7.0}
        score = server.compute_reaction_quality_score(returns, reaction_pattern="PERSISTENT")
        self.assertGreaterEqual(score, 60)

    def test_dead_cat_pop_scores_low(self):
        # 指示書3番の明示例：5M+8%→CLOSE+0.5%でFADE。
        returns = {"5M": 8.0, "CLOSE": 0.5}
        score = server.compute_reaction_quality_score(returns, reaction_pattern="FADE")
        persistent_score = server.compute_reaction_quality_score({"5M": 8.0, "CLOSE": 8.0}, reaction_pattern="PERSISTENT")
        self.assertLess(score, persistent_score)
        self.assertLessEqual(score, 20)

    def test_no_data_returns_zero(self):
        self.assertEqual(server.compute_reaction_quality_score({}), 0.0)


class ExtensionScoreTests(unittest.TestCase):
    """3. extension_score（指示書4番）"""

    def test_far_above_baseline_and_vwap_scores_high(self):
        score = server.compute_extension_score(current_price=1200, event_baseline_price=1000, vwap=1150, ma5=1100)
        self.assertGreaterEqual(score, 60)

    def test_no_data_returns_none(self):
        self.assertIsNone(server.compute_extension_score())

    def test_below_baseline_scores_low(self):
        score = server.compute_extension_score(current_price=980, event_baseline_price=1000)
        self.assertEqual(score, 0.0)


class FreshnessScoreTests(unittest.TestCase):
    """4. freshness_score（指示書5番）"""

    def test_buckets(self):
        self.assertEqual(server.compute_freshness_score(5), 100.0)
        self.assertEqual(server.compute_freshness_score(20), 90.0)
        self.assertEqual(server.compute_freshness_score(45), 75.0)
        self.assertEqual(server.compute_freshness_score(120), 55.0)
        self.assertEqual(server.compute_freshness_score(600), 40.0)
        self.assertEqual(server.compute_freshness_score(2000), 25.0)

    def test_none_input(self):
        self.assertIsNone(server.compute_freshness_score(None))


class HistoricalEdgeScoreTests(unittest.TestCase):
    """5. historical_edge_score（指示書6・32番：sample不足はPROVISIONAL/無視）"""

    def test_insufficient_sample_ignored(self):
        score, quality = server.compute_historical_edge_score({"sample_count": 3, "positive_rate": 0.9})
        self.assertEqual(score, 0.0)
        self.assertEqual(quality, "INSUFFICIENT_SAMPLE")

    def test_provisional_sample_weakened(self):
        score, quality = server.compute_historical_edge_score({"sample_count": 7, "positive_rate": 0.8})
        self.assertEqual(quality, "PROVISIONAL")
        self.assertLess(score, 80)

    def test_calibrated_full_sample(self):
        score, quality = server.compute_historical_edge_score({"sample_count": 20, "positive_rate": 0.8})
        self.assertEqual(quality, "CALIBRATED")
        self.assertEqual(score, 80.0)

    def test_no_data(self):
        score, quality = server.compute_historical_edge_score(None)
        self.assertEqual((score, quality), (0.0, "NO_DATA"))


class DecisionSupportScoreTests(unittest.TestCase):
    """6. decision_support_score（指示書7番：加重合成＋extension減点）"""

    def test_high_inputs_high_score(self):
        score = server.compute_decision_support_score(90, 90, 100, 80, 0, 100)
        self.assertGreaterEqual(score, 80)

    def test_extension_penalizes_score(self):
        base = server.compute_decision_support_score(90, 90, 100, 80, 0, 100)
        extended = server.compute_decision_support_score(90, 90, 100, 80, 100, 100)
        self.assertLess(extended, base)

    def test_score_clamped_0_100(self):
        score = server.compute_decision_support_score(0, 0, 0, 0, 100, 0)
        self.assertEqual(score, 0.0)


class DecisionSupportStateTests(unittest.TestCase):
    """7〜10. STRONG_SUPPORT/SUPPORTIVE/CAUTION/AVOID_CHASE分類（指示書8・9番）"""

    def test_strong_support(self):
        self.assertEqual(server.classify_decision_support_state(85), "STRONG_SUPPORT")

    def test_supportive(self):
        self.assertEqual(server.classify_decision_support_state(70), "SUPPORTIVE")

    def test_neutral(self):
        self.assertEqual(server.classify_decision_support_state(50), "NEUTRAL")

    def test_caution(self):
        self.assertEqual(server.classify_decision_support_state(35), "CAUTION")

    def test_avoid(self):
        self.assertEqual(server.classify_decision_support_state(10), "AVOID")

    def test_avoid_chase_condition_extension_and_return(self):
        self.assertTrue(server.detect_avoid_chase(85, current_return_from_event_pct=9.0))
        self.assertFalse(server.detect_avoid_chase(85, current_return_from_event_pct=2.0))

    def test_avoid_chase_condition_vwap_deviation(self):
        self.assertTrue(server.detect_avoid_chase(50, vwap_deviation_pct=7.0))

    def test_avoid_chase_overrides_high_score_state(self):
        # 指示書8番：スコアが高くてもAVOID_CHASE成立時はoverrideされる（呼び出し側の責務）。
        score = server.compute_decision_support_score(90, 90, 100, 80, 0, 100)
        state = server.classify_decision_support_state(score)
        self.assertIn(state, ("STRONG_SUPPORT", "SUPPORTIVE"))
        avoid_chase = server.detect_avoid_chase(85, current_return_from_event_pct=10.0)
        self.assertTrue(avoid_chase)


class PullbackCandidateTests(unittest.TestCase):
    """11. pullback_candidate + preferred_pullback_zone（指示書10・11番）"""

    def test_pullback_candidate_requires_avoid_chase_and_strong_material_reaction(self):
        self.assertTrue(server.detect_pullback_candidate(70, 65, avoid_chase=True))
        self.assertFalse(server.detect_pullback_candidate(70, 65, avoid_chase=False))
        self.assertFalse(server.detect_pullback_candidate(40, 65, avoid_chase=True))

    def test_preferred_pullback_zone_range_and_disclaimer(self):
        zone = server.compute_preferred_pullback_zone(vwap=1000, ma5=980, recent_breakout_price=1050,
                                                         event_baseline_price=950)
        self.assertEqual(zone["low"], 950)
        self.assertEqual(zone["high"], 1050)
        self.assertIn("disclaimer", zone)

    def test_preferred_pullback_zone_none_without_data(self):
        self.assertIsNone(server.compute_preferred_pullback_zone())


class FailedReactionTests(unittest.TestCase):
    """12. failed_reaction（指示書12番）"""

    def test_strong_material_weak_reaction_flags(self):
        self.assertTrue(server.detect_failed_reaction(90, 25))

    def test_weak_material_not_flagged(self):
        self.assertFalse(server.detect_failed_reaction(40, 25))

    def test_strong_reaction_not_flagged(self):
        self.assertFalse(server.detect_failed_reaction(90, 70))


class SellTheNewsTests(unittest.TestCase):
    """13. sell_the_news（指示書13番）"""

    def test_gap_up_then_fade_flags(self):
        self.assertTrue(server.detect_sell_the_news(5.0, "FADE"))

    def test_no_fade_not_flagged(self):
        self.assertFalse(server.detect_sell_the_news(5.0, "PERSISTENT"))

    def test_small_gap_not_flagged(self):
        self.assertFalse(server.detect_sell_the_news(1.0, "FADE"))


class EventDirectionTests(unittest.TestCase):
    """14〜16. POSITIVE/NEGATIVE/MIXED event_direction（指示書14・15・16番）"""

    def test_positive_event(self):
        direction, confidence = server.classify_event_direction("BUYBACK", "自社株買いを発表")
        self.assertEqual(direction, "POSITIVE")
        self.assertGreater(confidence, 0.5)

    def test_negative_event(self):
        direction, confidence = server.classify_event_direction("GUIDANCE_REVISION", "通期業績の下方修正を発表")
        self.assertEqual(direction, "NEGATIVE")

    def test_mixed_event_simultaneous_keywords(self):
        direction, confidence = server.classify_event_direction(
            "GUIDANCE_REVISION", "上方修正と同時に第三者割当増資を発表")
        self.assertEqual(direction, "MIXED")

    def test_default_direction_from_event_type_without_text(self):
        direction, _ = server.classify_event_direction("BUYBACK", None)
        self.assertEqual(direction, "POSITIVE")


class EventConflictTests(unittest.TestCase):
    """17. EVENT_CONFLICT（指示書17番）"""

    def test_positive_and_negative_conflict(self):
        self.assertTrue(server.detect_event_conflict(["POSITIVE", "NEGATIVE"]))

    def test_single_direction_no_conflict(self):
        self.assertFalse(server.detect_event_conflict(["POSITIVE", "POSITIVE"]))

    def test_empty_no_conflict(self):
        self.assertFalse(server.detect_event_conflict([]))


class TickerIntelligenceSummaryTests(unittest.TestCase):
    """18. build_ticker_intelligence_summary(ticker)（指示書18番）"""

    def test_summary_shape_and_conflict(self):
        rows = [
            {"event_id": 1, "event_direction": "POSITIVE", "decision_support_score": 78,
             "avoid_chase": True, "pullback_candidate": True},
            {"event_id": 2, "event_direction": "NEGATIVE", "decision_support_score": 40,
             "avoid_chase": False, "pullback_candidate": False},
        ]
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_event_decision_support_for_ticker.return_value = rows
            summary = server.build_ticker_intelligence_summary("postgres://x", "7203")
        self.assertEqual(summary["ticker"], "7203")
        self.assertEqual(summary["active_events"], 2)
        self.assertEqual(summary["net_event_direction"], "MIXED")
        self.assertTrue(summary["event_conflict"])
        self.assertEqual(summary["best_event_support_score"], 78)
        self.assertTrue(summary["avoid_chase"])
        self.assertTrue(summary["pullback_candidate"])

    def test_summary_empty_when_no_events(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_event_decision_support_for_ticker.return_value = []
            summary = server.build_ticker_intelligence_summary("postgres://x", "9999")
        self.assertEqual(summary["active_events"], 0)
        self.assertEqual(summary["net_event_direction"], "NEUTRAL")
        self.assertFalse(summary["event_conflict"])


class RecencyWeightingTests(unittest.TestCase):
    """19. recency weighting（指示書19番）"""

    def test_decay_buckets(self):
        self.assertEqual(server.compute_recency_weight(0), 1.0)
        self.assertEqual(server.compute_recency_weight(1), 0.8)
        self.assertEqual(server.compute_recency_weight(2), 0.6)
        self.assertEqual(server.compute_recency_weight(7), 0.4)
        self.assertEqual(server.compute_recency_weight(10), 0.2)

    def test_persistence_class_slows_decay(self):
        self.assertGreater(server.compute_recency_weight(10, persistence_class="STRUCTURAL"),
                            server.compute_recency_weight(10, persistence_class="INTRADAY"))


class PersistenceClassTests(unittest.TestCase):
    """20. persistence_class（指示書20番）"""

    def test_default_classes(self):
        self.assertEqual(server.classify_persistence_class("TOB_MA"), "STRUCTURAL")
        self.assertEqual(server.classify_persistence_class("ECONOMIC_INDICATOR"), "INTRADAY")

    def test_large_magnitude_upgrades_class(self):
        base = server.classify_persistence_class("EARNINGS")
        upgraded = server.classify_persistence_class("EARNINGS", material_magnitude=25)
        order = ["INTRADAY", "SHORT_TERM", "SWING", "STRUCTURAL"]
        self.assertGreater(order.index(upgraded), order.index(base))


class ExtendedMoveIntegrationTests(unittest.TestCase):
    """21. Phase8 EXTENDED_MOVEとの統合（指示書26番）"""

    def test_extended_move_flag_forces_high_extension_when_no_price_context(self):
        event = {"id": 1, "event_type": "BUYBACK", "confidence_level": "OFFICIAL_CONFIRMED",
                 "independent_source_count": 2, "impact_score": 80, "extended_move": True,
                 "first_seen_at": _iso(datetime.datetime.now(datetime.timezone.utc)), "title": "自社株買い"}
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.get_underlying_event.return_value = event
            mock_db.list_event_market_reactions_for_event.return_value = []
            mock_db.create_event_decision_support.side_effect = lambda db, fields: fields
            mock_db.get_latest_event_decision_support.return_value = None
            result = server.generate_event_decision_support("postgres://x", 1, "7203", market_context={"market_state": None})
        self.assertEqual(result["extension_score"], 85.0)


class MarketRegimeSectorRegimeContextTests(unittest.TestCase):
    """22・23. market_regime / sector_strength_at_event storage（指示書33・34番）"""

    def test_market_regime_stored_from_market_context(self):
        event = {"id": 1, "event_type": "BUYBACK", "confidence_level": "MULTI_SOURCE_CONFIRMED",
                 "independent_source_count": 2, "impact_score": 60,
                 "first_seen_at": _iso(datetime.datetime.now(datetime.timezone.utc)), "title": "自社株買い"}
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.get_underlying_event.return_value = event
            mock_db.list_event_market_reactions_for_event.return_value = []
            mock_db.create_event_decision_support.side_effect = lambda db, fields: fields
            mock_db.get_latest_event_decision_support.return_value = None
            result = server.generate_event_decision_support(
                "postgres://x", 1, "7203",
                market_context={"market_state": {"nikkei_change_pct": 1.5, "sox_change_pct": 1.0},
                                 "sector_strength_at_event": 0.7})
        self.assertIn("TREND_UP", result["market_regime"])
        self.assertEqual(result["sector_strength_at_event"], 0.7)


class ContradictionFlagTests(unittest.TestCase):
    """24. contradiction flags（指示書35番）"""

    def test_good_material_bad_reaction(self):
        flags = server.detect_contradiction_flags(material_quality_score=85, reaction_quality_score=20)
        self.assertIn("GOOD_MATERIAL_BAD_REACTION", flags)

    def test_bad_material_strong_price(self):
        flags = server.detect_contradiction_flags(material_quality_score=20, reaction_quality_score=80,
                                                     price_move_pct=8.0)
        self.assertIn("BAD_MATERIAL_STRONG_PRICE", flags)

    def test_source_consensus_price_divergence(self):
        flags = server.detect_contradiction_flags(source_consensus_direction="POSITIVE", price_direction="NEGATIVE")
        self.assertIn("SOURCE_CONSENSUS_PRICE_DIVERGENCE", flags)

    def test_no_flags_when_aligned(self):
        flags = server.detect_contradiction_flags(material_quality_score=80, reaction_quality_score=75,
                                                     source_consensus_direction="POSITIVE", price_direction="POSITIVE")
        self.assertEqual(flags, [])


class EntryTop5BridgeTests(unittest.TestCase):
    """25. ENTRY TOP5 bridge：entry_score自体は不変（指示書21・43番）"""

    def test_event_support_label_does_not_touch_entry_score(self):
        with mock.patch.object(server, "build_ticker_intelligence_summary") as mock_summary:
            mock_summary.return_value = {"active_events": 1, "avoid_chase": True, "best_event_support_score": 90,
                                           "pullback_candidate": True}
            label = server.build_entry_top5_event_support_label("postgres://x", "7203")
        self.assertEqual(label, "AVOID_CHASE")

    def test_no_events_returns_none(self):
        with mock.patch.object(server, "build_ticker_intelligence_summary") as mock_summary:
            mock_summary.return_value = {"active_events": 0}
            label = server.build_entry_top5_event_support_label("postgres://x", "7203")
        self.assertIsNone(label)

    def test_strong_and_caution_labels(self):
        with mock.patch.object(server, "build_ticker_intelligence_summary") as mock_summary:
            mock_summary.return_value = {"active_events": 1, "avoid_chase": False, "best_event_support_score": 70,
                                           "pullback_candidate": False}
            self.assertEqual(server.build_entry_top5_event_support_label("postgres://x", "7203"), "STRONG")
            mock_summary.return_value = {"active_events": 1, "avoid_chase": False, "best_event_support_score": 20,
                                           "pullback_candidate": False}
            self.assertEqual(server.build_entry_top5_event_support_label("postgres://x", "7203"), "CAUTION")


class WatchlistBridgeTests(unittest.TestCase):
    """26. Watchlist bridge tags（指示書22・37番）"""

    def test_tags_avoid_chase_and_pullback(self):
        with mock.patch.object(server, "build_ticker_intelligence_summary") as mock_summary:
            mock_summary.return_value = {"active_events": 1, "avoid_chase": True, "best_event_support_score": 90,
                                           "pullback_candidate": True}
            tags = server.build_watchlist_event_tags("postgres://x", "7203")
        self.assertIn("EVENT⚠", tags)
        self.assertIn("PULLBACK", tags)

    def test_no_tags_when_no_events(self):
        with mock.patch.object(server, "build_ticker_intelligence_summary") as mock_summary:
            mock_summary.return_value = {"active_events": 0}
            tags = server.build_watchlist_event_tags("postgres://x", "7203")
        self.assertEqual(tags, [])


class ChatGptPayloadTests(unittest.TestCase):
    """27. ChatGPT相談payload拡張（指示書23番）"""

    def test_payload_format_matches_spec_example(self):
        rows = [{"ticker": "7203", "material_quality_score": 84, "reaction_quality_score": 77,
                 "extension_score": 82, "decision_support_state": "AVOID_CHASE", "pullback_candidate": True,
                 "failed_reaction": False, "decision_support_score": 60,
                 "reasons_json": ["officially confirmed catalyst"]}]
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_event_decision_support_history.return_value = rows
            payload = server.build_event_decision_support_payload("postgres://x", 1)
        self.assertEqual(payload["material_quality_score"], 84)
        self.assertEqual(payload["decision_support_state"], "AVOID_CHASE")
        self.assertTrue(payload["pullback_candidate"])
        self.assertIn("reasons", payload)

    def test_none_when_no_history(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_event_decision_support_history.return_value = []
            self.assertIsNone(server.build_event_decision_support_payload("postgres://x", 1))


class MorningIntradayIntegrationTests(unittest.TestCase):
    """28・29. morning/intraday report統合（指示書24・25・26番）——GU≠buyの注記含む"""

    def test_intraday_digest_buckets(self):
        rows = [
            {"ticker": "7203", "event_id": 1, "decision_support_state": "STRONG_SUPPORT", "decision_support_score": 90,
             "avoid_chase": False, "pullback_candidate": False, "failed_reaction": False},
            {"ticker": "9984", "event_id": 2, "decision_support_state": "AVOID_CHASE", "decision_support_score": 40,
             "avoid_chase": True, "pullback_candidate": True, "failed_reaction": False},
            {"ticker": "6758", "event_id": 3, "decision_support_state": "AVOID", "decision_support_score": 10,
             "avoid_chase": False, "pullback_candidate": False, "failed_reaction": True},
        ]
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_recent_event_decision_support.return_value = rows
            digest = server.build_intraday_event_decision_digest("postgres://x")
        self.assertEqual(len(digest["supported"]), 1)
        self.assertEqual(len(digest["avoid_chase"]), 1)
        self.assertEqual(len(digest["pullback_candidate"]), 1)
        self.assertEqual(len(digest["failed_reaction"]), 1)

    def test_morning_overnight_digest_includes_gu_note(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_active_underlying_events.return_value = []
            mock_db.list_evaluated_event_market_reactions_since.return_value = []
            digest = server.build_morning_overnight_event_digest("postgres://x")
        self.assertIn("ギャップアップ", digest["note"])
        self.assertEqual(digest["overnight_events"], [])

    def test_safe_wrappers_swallow_exceptions(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_recent_event_decision_support.side_effect = Exception("boom")
            digest = server.build_intraday_event_decision_digest_safe("postgres://x")
        self.assertEqual(digest["supported"], [])


class NoHindsightTests(unittest.TestCase):
    """30. no hindsight（指示書31番、REQUIRED）"""

    def test_available_data_at_is_now_not_backdated(self):
        event = {"id": 1, "event_type": "BUYBACK", "confidence_level": "OFFICIAL_CONFIRMED",
                 "independent_source_count": 2, "impact_score": 70,
                 "first_seen_at": _iso(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=5)),
                 "title": "自社株買い"}
        before = datetime.datetime.now(datetime.timezone.utc)
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.get_underlying_event.return_value = event
            mock_db.list_event_market_reactions_for_event.return_value = []
            mock_db.create_event_decision_support.side_effect = lambda db, fields: fields
            mock_db.get_latest_event_decision_support.return_value = None
            result = server.generate_event_decision_support("postgres://x", 1, "7203", market_context={"market_state": None})
        available_at = datetime.datetime.fromisoformat(result["available_data_at"])
        self.assertGreaterEqual(available_at, before)


class DecisionTransitionTests(unittest.TestCase):
    """31. decision transition history（指示書28・29番）"""

    def test_history_is_append_only_per_call(self):
        event = {"id": 1, "event_type": "BUYBACK", "confidence_level": "OFFICIAL_CONFIRMED",
                 "independent_source_count": 2, "impact_score": 70,
                 "first_seen_at": _iso(datetime.datetime.now(datetime.timezone.utc)), "title": "自社株買い"}
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.get_underlying_event.return_value = event
            mock_db.list_event_market_reactions_for_event.return_value = []
            mock_db.create_event_decision_support.side_effect = lambda db, fields: fields
            mock_db.get_latest_event_decision_support.return_value = None
            server.generate_event_decision_support("postgres://x", 1, "7203", market_context={"market_state": None})
            server.generate_event_decision_support("postgres://x", 1, "7203", market_context={"market_state": None})
        self.assertEqual(mock_db.create_event_decision_support.call_count, 2)


class ApiEndpointTests(unittest.TestCase):
    """32. API（指示書40番）：ハンドラ関数を直接呼ぶのではなく、経路になる関数群を検証する。"""

    def test_ticker_intelligence_summary_function_exists_and_callable(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_event_decision_support_for_ticker.return_value = []
            summary = server.build_ticker_intelligence_summary("postgres://x", "7203")
        self.assertEqual(summary["ticker"], "7203")

    def test_market_intelligence_generic_route_is_exact_match_not_prefix(self):
        # 不具合修正の回帰確認：/api/market-intelligence/events等をstartswithで飲み込まない。
        # linecache汚染対策（Bugfix: isolate global state between test modules）：
        # test_support_source_inspect.get_fresh_source参照。
        src = get_fresh_source(server.Handler.do_GET)
        self.assertIn('self.path.split("?")[0] == "/api/market-intelligence"', src)


class UiDataShapeTests(unittest.TestCase):
    """33. UI向けデータ形状（指示書36・37・38番）：event_decision_supportがbuild_event_summaryに
    additiveに載ること。"""

    def test_event_summary_includes_decision_support_when_present(self):
        event = {"id": 1, "event_type": "BUYBACK", "title": "自社株買い", "confidence_level": "OFFICIAL_CONFIRMED",
                 "status": "ACTIVE", "first_seen_at": _iso(datetime.datetime.now(datetime.timezone.utc)),
                 "last_seen_at": _iso(datetime.datetime.now(datetime.timezone.utc)),
                 "independent_source_count": 2, "raw_source_count": 2, "direct_tickers_json": ["7203"],
                 "related_tickers_json": [], "impact_score": 80, "primary_source_type": "TDNET",
                 "primary_source_url": None, "effectiveness_score": None, "reaction_pattern": None,
                 "extended_move": False}
        rows = [{"ticker": "7203", "material_quality_score": 84, "reaction_quality_score": 77,
                 "extension_score": 82, "decision_support_state": "SUPPORTIVE", "pullback_candidate": False,
                 "failed_reaction": False, "decision_support_score": 70, "reasons_json": []}]
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_underlying_event_evidence.return_value = []
            mock_db.list_event_market_reactions_for_event.return_value = []
            mock_db.list_event_decision_support_history.return_value = rows
            summary = server.build_event_summary("postgres://x", event)
        self.assertIn("event_decision_support", summary)
        self.assertEqual(summary["event_decision_support"]["decision_support_state"], "SUPPORTIVE")

    def test_event_summary_omits_key_when_no_decision_support(self):
        event = {"id": 2, "event_type": "OTHER", "title": "x", "confidence_level": "SOCIAL_ONLY",
                 "status": "ACTIVE", "first_seen_at": _iso(datetime.datetime.now(datetime.timezone.utc)),
                 "last_seen_at": _iso(datetime.datetime.now(datetime.timezone.utc)),
                 "independent_source_count": 1, "raw_source_count": 1, "direct_tickers_json": [],
                 "related_tickers_json": [], "impact_score": 10, "primary_source_type": "SOCIAL",
                 "primary_source_url": None, "effectiveness_score": None, "reaction_pattern": None,
                 "extended_move": False}
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_underlying_event_evidence.return_value = []
            mock_db.list_event_market_reactions_for_event.return_value = []
            mock_db.list_event_decision_support_history.return_value = []
            summary = server.build_event_summary("postgres://x", event)
        self.assertNotIn("event_decision_support", summary)


class DiagnosticsTests(unittest.TestCase):
    """指示書41番：diagnostics追加項目。"""

    def test_diagnostics_includes_phase9_counts(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.count_active_underlying_events.return_value = 1
            mock_db.count_underlying_events_since.return_value = 0
            mock_db.count_underlying_event_evidence_since.return_value = 0
            mock_db.count_event_market_reactions.return_value = 0
            mock_db.count_event_market_reactions_by_quality.return_value = 0
            mock_db.count_pending_prediction_resolutions.return_value = 0
            mock_db.count_event_decision_support_since.return_value = 3
            diag = server.get_underlying_event_diagnostics("postgres://x")
        for key in ("decision_support_generated_today", "avoid_chase_count", "pullback_candidate_count",
                    "failed_reaction_count", "event_conflict_count"):
            self.assertIn(key, diag)
            self.assertEqual(diag[key], 3)


class Phase8CompatibilityTests(unittest.TestCase):
    """34. Phase8互換性：既存のPhase8関数がPhase9追加後も無変更で動くこと。"""

    def test_classify_reaction_pattern_unchanged(self):
        self.assertEqual(server.classify_reaction_pattern({"5M": 8, "CLOSE": 1}), "FADE")

    def test_compute_event_effectiveness_score_unchanged(self):
        score = server.compute_event_effectiveness_score(magnitude=5, relative_strength=3, breadth=0.6,
                                                            persistence_pattern="PERSISTENT")
        self.assertGreater(score, 0)

    def test_aggregate_event_type_performance_unchanged(self):
        reactions = [{"event_id": 1, "event_type": "BUYBACK", "reaction_window": "30M", "stock_return_pct": 3.0}]
        perf = server.aggregate_event_type_performance(reactions)
        self.assertEqual(perf["BUYBACK"]["sample_count"], 1)


if __name__ == "__main__":
    unittest.main()
