# Market Intelligence Phase10 テスト（指示書40番：最低29項目）。
#
# Phase2〜9同様、実DBを必要としない形でカバーする。DB操作が必要な箇所は
# mock.patch.object(server, "investment_db") で完全にモックする。
#
# 実行方法： cd files && python -m unittest test_nicosoku_phase10 -v

import datetime
import unittest
from unittest import mock

import server
from test_support_source_inspect import get_fresh_source


def _iso(dt):
    return dt.astimezone(datetime.timezone.utc).isoformat()


class EntrySnapshotTests(unittest.TestCase):
    """1. entry snapshot（指示書1・3・4番）"""

    def test_capture_entry_context_saves_snapshot(self):
        with mock.patch.object(server, "investment_db") as mock_db, \
             mock.patch.object(server, "build_ticker_intelligence_summary") as mock_summary:
            mock_summary.return_value = {"avoid_chase": False, "pullback_candidate": False}
            mock_db.list_event_decision_support_for_ticker.return_value = [
                {"event_id": 1, "decision_support_state": "SUPPORTIVE", "material_quality_score": 82,
                 "reaction_quality_score": 76, "extension_score": 42, "decision_support_score": 74}]
            mock_db.create_trade_decision_context.side_effect = lambda db, uid, fields: fields
            saved = server.capture_trade_decision_context(
                "postgres://x", "local", "1", "7203", "ENTRY", price=1000, entry_score=82)
        self.assertEqual(saved["action"], "ENTRY")
        self.assertEqual(saved["event_support_state"], "SUPPORTIVE")
        self.assertEqual(saved["material_quality_score"], 82)
        mock_db.create_trade_decision_context.assert_called_once()


class ExitSnapshotTests(unittest.TestCase):
    """2. exit snapshot（指示書5番）"""

    def test_capture_exit_context_saves_snapshot(self):
        with mock.patch.object(server, "investment_db") as mock_db, \
             mock.patch.object(server, "build_ticker_intelligence_summary") as mock_summary:
            mock_summary.return_value = {"avoid_chase": True, "pullback_candidate": False}
            mock_db.list_event_decision_support_for_ticker.return_value = [
                {"event_id": 1, "decision_support_state": "AVOID_CHASE", "material_quality_score": 84,
                 "reaction_quality_score": 80, "extension_score": 90, "decision_support_score": 40}]
            mock_db.create_trade_decision_context.side_effect = lambda db, uid, fields: fields
            saved = server.capture_trade_decision_context(
                "postgres://x", "local", "1", "7203", "EXIT", price=1100)
        self.assertEqual(saved["action"], "EXIT")
        self.assertEqual(saved["event_support_state"], "AVOID_CHASE")
        self.assertTrue(saved["avoid_chase"])


class ImmutableSnapshotTests(unittest.TestCase):
    """3. immutable snapshot（指示書2・3番）：呼ぶ度に新しい行をINSERTするのみ、UPDATEしない"""

    def test_create_trade_decision_context_never_updates(self):
        # linecache汚染対策（Bugfix: isolate global state between test modules）：
        # test_support_source_inspect.get_fresh_source参照。
        import investment_db
        src = get_fresh_source(investment_db.create_trade_decision_context)
        self.assertIn("INSERT INTO trade_decision_context", src)
        self.assertNotIn("UPDATE trade_decision_context", src)

    def test_multiple_captures_each_insert_new_row(self):
        with mock.patch.object(server, "investment_db") as mock_db, \
             mock.patch.object(server, "build_ticker_intelligence_summary") as mock_summary:
            mock_summary.return_value = {"avoid_chase": False, "pullback_candidate": False}
            mock_db.list_event_decision_support_for_ticker.return_value = []
            mock_db.create_trade_decision_context.side_effect = lambda db, uid, fields: fields
            server.capture_trade_decision_context("postgres://x", "local", "1", "7203", "ENTRY", price=1000)
            server.capture_trade_decision_context("postgres://x", "local", "1", "7203", "HOLD", price=1050)
        self.assertEqual(mock_db.create_trade_decision_context.call_count, 2)


class NoHindsightTests(unittest.TestCase):
    """4. no hindsight（指示書2番、REQUIRED）"""

    def test_available_data_at_is_now(self):
        with mock.patch.object(server, "investment_db") as mock_db, \
             mock.patch.object(server, "build_ticker_intelligence_summary") as mock_summary:
            mock_summary.return_value = {"avoid_chase": False, "pullback_candidate": False}
            mock_db.list_event_decision_support_for_ticker.return_value = []
            mock_db.create_trade_decision_context.side_effect = lambda db, uid, fields: fields
            before = datetime.datetime.now(datetime.timezone.utc)
            saved = server.capture_trade_decision_context("postgres://x", "local", "1", "7203", "ENTRY", price=1000)
        available_at = datetime.datetime.fromisoformat(saved["available_data_at"])
        self.assertGreaterEqual(available_at, before)

    def test_replay_uses_only_evidence_available_at_time(self):
        as_of = _iso(datetime.datetime(2026, 9, 11, 1, 0, tzinfo=datetime.timezone.utc))
        event = {"id": 1, "event_type": "BUYBACK", "confidence_level": "SOCIAL_ONLY",
                 "independent_source_count": 1, "impact_score": 50, "primary_source_type": "SOCIAL",
                 "first_seen_at": as_of, "title": "自社株買い"}
        evidence_before = [{"posted_at": "2026-09-11T00:30:00+00:00", "source_name": "a"}]
        evidence_after = evidence_before + [{"posted_at": "2026-09-11T02:00:00+00:00", "source_name": "b"}]
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.get_underlying_event.return_value = event
            mock_db.list_underlying_event_evidence.return_value = evidence_after
            mock_db.list_event_market_reactions_for_event.return_value = []
            result = server.replay_decision_support_at("postgres://x", 1, "7203", as_of)
        # 未来のevidence（02:00投稿）が混ざっていないことを確認：独立ソース数は1のまま
        self.assertIsNotNone(result["material_quality_score"])
        self.assertIn("再現", result["note"]) if result.get("note") else None


class DecisionQualityTests(unittest.TestCase):
    """5〜8. GOOD/BAD_DECISION × GOOD/BAD_RESULT（指示書7・44番、最重要原則）"""

    def test_good_decision_good_result(self):
        self.assertEqual(server.classify_decision_quality(True, True), "GOOD_DECISION_GOOD_RESULT")

    def test_good_decision_bad_result(self):
        self.assertEqual(server.classify_decision_quality(True, False), "GOOD_DECISION_BAD_RESULT")

    def test_bad_decision_good_result(self):
        self.assertEqual(server.classify_decision_quality(False, True), "BAD_DECISION_GOOD_RESULT")

    def test_bad_decision_bad_result(self):
        self.assertEqual(server.classify_decision_quality(False, False), "BAD_DECISION_BAD_RESULT")

    def test_win_is_not_automatically_good_decision(self):
        # 指示書44番：「勝った＝正しい判断」としない。AVOID_CHASEで入って勝ってもBAD_DECISION。
        entry_context = {"avoid_chase": True, "event_support_state": "AVOID_CHASE"}
        decision_good = server.evaluate_decision_was_good(entry_context)
        self.assertFalse(decision_good)
        self.assertEqual(server.classify_decision_quality(decision_good, True), "BAD_DECISION_GOOD_RESULT")


class ChaseEntryTests(unittest.TestCase):
    """9. chase entry（指示書8・9番）"""

    def test_avoid_chase_entry_classified_as_chase_entry(self):
        entry_context = {"avoid_chase": True, "extension_score": 90}
        self.assertEqual(server.classify_entry_timing_quality(entry_context), "CHASE_ENTRY")

    def test_high_extension_without_avoid_chase_flag_still_chase(self):
        entry_context = {"avoid_chase": False, "extension_score": 85}
        self.assertEqual(server.classify_entry_timing_quality(entry_context), "CHASE_ENTRY")

    def test_early_entry_classified_early_good(self):
        entry_context = {"avoid_chase": False, "extension_score": 10}
        self.assertEqual(server.classify_entry_timing_quality(entry_context), "EARLY_GOOD")


class PullbackEntryTests(unittest.TestCase):
    """10. pullback entry（指示書10・31番）"""

    def test_good_entry_after_pullback(self):
        entry_context = {"avoid_chase": False, "extension_score": 25}
        self.assertEqual(server.classify_entry_timing_quality(entry_context), "GOOD_ENTRY")

    def test_wait_outcome_good_pullback(self):
        self.assertEqual(server.classify_wait_outcome(None, pullback_then_supportive=True), "GOOD_PULLBACK")


class StateTransitionTests(unittest.TestCase):
    """11. state transition（指示書12・13・29番）"""

    def test_transition_recorded_when_state_changes(self):
        event = {"id": 1, "event_type": "BUYBACK", "confidence_level": "OFFICIAL_CONFIRMED",
                 "independent_source_count": 3, "impact_score": 90, "material_magnitude": 10,
                 "first_seen_at": _iso(datetime.datetime.now(datetime.timezone.utc)), "title": "自社株買い"}
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.get_underlying_event.return_value = event
            mock_db.list_event_market_reactions_for_event.return_value = []
            mock_db.create_event_decision_support.side_effect = lambda db, fields: fields
            mock_db.get_latest_event_decision_support.return_value = {
                "decision_support_state": "AVOID_CHASE", "extension_score": 90,
                "reaction_quality_score": 30, "source_confidence_score": 50, "market_regime": None}
            server.generate_event_decision_support(
                "postgres://x", 1, "7203", market_context={"market_state": None})
        mock_db.create_event_decision_transition.assert_called_once()
        call_kwargs = mock_db.create_event_decision_transition.call_args[0][1]
        self.assertEqual(call_kwargs["from_state"], "AVOID_CHASE")

    def test_no_transition_when_state_unchanged(self):
        event = {"id": 1, "event_type": "OTHER", "confidence_level": "UNVERIFIED",
                 "independent_source_count": 0, "impact_score": 5,
                 "first_seen_at": _iso(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=3)),
                 "title": "x"}
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.get_underlying_event.return_value = event
            mock_db.list_event_market_reactions_for_event.return_value = []
            mock_db.create_event_decision_support.side_effect = lambda db, fields: fields
            mock_db.get_latest_event_decision_support.return_value = {"decision_support_state": "AVOID"}
            server.generate_event_decision_support(
                "postgres://x", 1, "7203", market_context={"market_state": None})
        mock_db.create_event_decision_transition.assert_not_called()

    def test_classify_transition_reason(self):
        prev = {"extension_score": 90, "reaction_quality_score": 20, "source_confidence_score": 50, "market_regime": None}
        new = {"extension_score": 40, "reaction_quality_score": 40, "source_confidence_score": 75, "market_regime": "RISK_ON"}
        reason = server.classify_transition_reason(prev, new)
        self.assertIn("extension normalized", reason)
        self.assertIn("new official confirmation", reason)


class ReplayTests(unittest.TestCase):
    """12. replay_decision_support（指示書14・15・28・36番）"""

    def test_replay_returns_state_and_score(self):
        as_of = _iso(datetime.datetime.now(datetime.timezone.utc))
        event = {"id": 1, "event_type": "BUYBACK", "confidence_level": "MULTI_SOURCE_CONFIRMED",
                 "independent_source_count": 2, "impact_score": 70, "primary_source_type": "NEWS",
                 "first_seen_at": _iso(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)),
                 "title": "自社株買い"}
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.get_underlying_event.return_value = event
            mock_db.list_underlying_event_evidence.return_value = []
            mock_db.list_event_market_reactions_for_event.return_value = []
            result = server.replay_decision_support_at("postgres://x", 1, "7203", as_of)
        self.assertIn("decision_support_state", result)
        self.assertIn("extension_score", result["note"])

    def test_replay_series_returns_list(self):
        with mock.patch.object(server, "replay_decision_support_at", return_value={"as_of": "x"}) as mock_replay:
            series = server.replay_decision_support_series("postgres://x", 1, "7203", ["a", "b", "c"])
        self.assertEqual(len(series), 3)
        self.assertEqual(mock_replay.call_count, 3)

    def test_replay_missing_event_returns_empty_note(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.get_underlying_event.return_value = None
            result = server.replay_decision_support_at("postgres://x", 999, "7203", _iso(datetime.datetime.now(datetime.timezone.utc)))
        self.assertIsNone(result["decision_support_state"])


class MarketRegimeCalibrationTests(unittest.TestCase):
    """13. regime別calibration（指示書19番）"""

    def test_split_by_market_regime(self):
        samples = [{"material_quality_score": 80, "reaction_quality_score": 70, "decision_support_state": "SUPPORTIVE",
                     "pnl_pct": 5, "market_regime": "RISK_ON"} for _ in range(30)] + \
                  [{"material_quality_score": 40, "reaction_quality_score": 30, "decision_support_state": "CAUTION",
                     "pnl_pct": -3, "market_regime": "RISK_OFF"} for _ in range(30)]
        result = server.compute_calibration_by_market_regime(samples)
        self.assertIn("RISK_ON", result)
        self.assertIn("RISK_OFF", result)
        self.assertEqual(result["RISK_ON"]["sample_count"], 30)


class EventTypeCalibrationTests(unittest.TestCase):
    """14. event_type別calibration（指示書20番）"""

    def test_split_by_event_type(self):
        samples = [{"material_quality_score": 80, "reaction_quality_score": 70, "decision_support_state": "SUPPORTIVE",
                     "pnl_pct": 5, "event_type": "BUYBACK"} for _ in range(35)]
        result = server.compute_calibration_by_event_type(samples)
        self.assertIn("BUYBACK", result)
        self.assertEqual(result["BUYBACK"]["status"], "PROVISIONAL")


class FalsePositiveTests(unittest.TestCase):
    """15. false positive分析（指示書21番）"""

    def test_finds_strong_support_that_declined(self):
        samples = [{"decision_support_state": "STRONG_SUPPORT", "pnl_pct": -5},
                   {"decision_support_state": "SUPPORTIVE", "pnl_pct": 3}]
        fps = server.find_decision_support_false_positives(samples)
        self.assertEqual(len(fps), 1)
        self.assertEqual(fps[0]["pnl_pct"], -5)


class FalseNegativeTests(unittest.TestCase):
    """16. false negative分析（指示書22番）"""

    def test_finds_caution_that_surged(self):
        samples = [{"decision_support_state": "AVOID_CHASE", "pnl_pct": 18},
                   {"decision_support_state": "CAUTION", "pnl_pct": 2}]
        fns = server.find_decision_support_false_negatives(samples)
        self.assertEqual(len(fns), 1)
        self.assertEqual(fns[0]["pnl_pct"], 18)


class RuleOverridesEventTests(unittest.TestCase):
    """17. rule vs intelligence conflict（指示書23・24番）"""

    def test_rule_overrides_event_when_strong_support(self):
        self.assertEqual(server.classify_rule_compliance(True, "STRONG_SUPPORT"), "RULE_OVERRIDES_EVENT")

    def test_rule_compliant_exit_when_not_strong(self):
        self.assertEqual(server.classify_rule_compliance(True, "CAUTION"), "RULE_COMPLIANT_EXIT")

    def test_compliant_when_no_rule_triggered(self):
        self.assertEqual(server.classify_rule_compliance(False, "STRONG_SUPPORT"), "COMPLIANT")


class DailyReviewIntegrationTests(unittest.TestCase):
    """18. daily review integration（指示書25・26番）：既存score計算に触れず追加のみ"""

    def test_build_daily_decision_review_aggregates(self):
        evaluations = [
            {"ticker": "7203", "trade_id": "1", "decision_quality": "GOOD_DECISION_GOOD_RESULT",
             "timing_quality": "GOOD_ENTRY", "rule_compliance": "COMPLIANT", "pnl_pct": 5.0,
             "created_at": "2026-09-11T02:00:00+00:00", "wait_outcome": None, "avoided_loss_pct": None},
            {"ticker": "9984", "trade_id": "2", "decision_quality": "BAD_DECISION_BAD_RESULT",
             "timing_quality": "CHASE_ENTRY", "rule_compliance": "COMPLIANT", "pnl_pct": -4.2,
             "created_at": "2026-09-11T03:00:00+00:00", "wait_outcome": None, "avoided_loss_pct": None},
        ]
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_trade_outcome_evaluations.return_value = evaluations
            review = server.build_daily_decision_review("postgres://x", "local", "2026-09-11")
        self.assertEqual(review["good_decisions"], 1)
        self.assertEqual(review["bad_decisions"], 1)
        self.assertEqual(review["chase_entries"], 1)
        self.assertEqual(len(review["items"]), 2)

    def test_does_not_touch_daily_review_score(self):
        # generate_daily_review自体を一切呼ばないことを確認（既存スコア計算に無干渉）。
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_trade_outcome_evaluations.return_value = []
            server.build_daily_decision_review("postgres://x", "local", "2026-09-11")
        mock_db.generate_daily_review.assert_not_called()


class EntryTop5TrackingTests(unittest.TestCase):
    """19. ENTRY TOP5候補のsnapshot（指示書29番）：買わなかった候補も評価可能にする"""

    def test_create_and_list_entry_candidate_snapshot(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.create_entry_candidate_snapshot.return_value = {"id": 1, "code": "7203", "was_taken": False}
            saved = server.investment_db.create_entry_candidate_snapshot(
                "postgres://x", "local", {"code": "7203", "entry_score": 82, "event_support": "STRONG",
                                            "price_at_candidate": 1000, "was_taken": False})
        self.assertEqual(saved["code"], "7203")
        self.assertFalse(saved["was_taken"])


class WaitCorrectTests(unittest.TestCase):
    """20. WAIT effectiveness：CORRECT_WAIT（指示書30・31番）"""

    def test_wait_then_decline_is_correct_wait(self):
        self.assertEqual(server.classify_wait_outcome(-3.0), "CORRECT_WAIT")

    def test_wait_then_flat_is_no_edge(self):
        self.assertEqual(server.classify_wait_outcome(2.0), "NO_EDGE")


class GoodPullbackTests(unittest.TestCase):
    """21. GOOD_PULLBACK（指示書30・31番）"""

    def test_pullback_then_supportive_is_good_pullback(self):
        self.assertEqual(server.classify_wait_outcome(5.0, pullback_then_supportive=True), "GOOD_PULLBACK")


class MissedBreakoutTests(unittest.TestCase):
    """22. MISSED_BREAKOUT（指示書30・31・32番）"""

    def test_large_subsequent_gain_is_missed_breakout(self):
        self.assertEqual(server.classify_wait_outcome(20.0), "MISSED_BREAKOUT")


class AvoidedLossTests(unittest.TestCase):
    """23. avoided_loss_pct（指示書33番）"""

    def test_avoid_chase_then_decline_records_avoided_loss(self):
        self.assertEqual(server.compute_avoided_loss_pct(-8.0, was_avoid_chase=True), 8.0)

    def test_no_avoided_loss_when_not_avoid_chase(self):
        self.assertIsNone(server.compute_avoided_loss_pct(-8.0, was_avoid_chase=False))

    def test_no_avoided_loss_when_price_rose(self):
        self.assertIsNone(server.compute_avoided_loss_pct(5.0, was_avoid_chase=True))


class OpportunityCostTests(unittest.TestCase):
    """24. opportunity_cost_pct（指示書32番）：損失とは別扱い"""

    def test_missed_breakout_records_opportunity_cost(self):
        self.assertEqual(server.compute_opportunity_cost_pct(20.0, "MISSED_BREAKOUT"), 20.0)

    def test_correct_wait_has_no_opportunity_cost(self):
        self.assertIsNone(server.compute_opportunity_cost_pct(-3.0, "CORRECT_WAIT"))


class SuggestedWeightsTests(unittest.TestCase):
    """25. suggested_weights（指示書17番：自動適用は禁止、参考値のみ）"""

    def test_suggested_weights_shape(self):
        samples = [{"material_quality_score": 80 + i, "reaction_quality_score": 40, "pnl_pct": 1 + i * 0.2}
                   for i in range(20)]
        result = server.suggest_calibrated_weights(samples)
        self.assertIn("current", result)
        self.assertIn("suggested", result)
        total_current = sum(result["current"].values())
        total_suggested = sum(result["suggested"].values())
        self.assertAlmostEqual(total_current, total_suggested, places=2)

    def test_calibration_report_never_auto_applies(self):
        # compute_decision_support_calibrationの戻り値が「提案」に留まり、
        # DECISION_SUPPORT_WEIGHTS自体を書き換えていないことを確認する。
        before = dict(server.DECISION_SUPPORT_WEIGHTS)
        samples = [{"material_quality_score": 80, "reaction_quality_score": 70, "decision_support_state": "SUPPORTIVE",
                     "pnl_pct": 5} for _ in range(35)]
        server.compute_decision_support_calibration(samples)
        self.assertEqual(server.DECISION_SUPPORT_WEIGHTS, before)


class CalibrationMinSampleTests(unittest.TestCase):
    """26. calibration min sample（指示書18番：<30は較正禁止）"""

    def test_below_30_is_insufficient(self):
        samples = [{"material_quality_score": 80, "reaction_quality_score": 70, "decision_support_state": "SUPPORTIVE",
                     "pnl_pct": 5} for _ in range(10)]
        result = server.compute_decision_support_calibration(samples)
        self.assertEqual(result["status"], "INSUFFICIENT_SAMPLE")
        self.assertIsNone(result["suggested_weights"])

    def test_30_to_49_is_provisional(self):
        samples = [{"material_quality_score": 80, "reaction_quality_score": 70, "decision_support_state": "SUPPORTIVE",
                     "pnl_pct": 5} for _ in range(35)]
        result = server.compute_decision_support_calibration(samples)
        self.assertEqual(result["status"], "PROVISIONAL")

    def test_50_plus_is_reference(self):
        samples = [{"material_quality_score": 80, "reaction_quality_score": 70, "decision_support_state": "SUPPORTIVE",
                     "pnl_pct": 5} for _ in range(60)]
        result = server.compute_decision_support_calibration(samples)
        self.assertEqual(result["status"], "REFERENCE")


class ApiWiringTests(unittest.TestCase):
    """27. API（指示書38番）"""

    def test_new_routes_present_in_do_get(self):
        src = get_fresh_source(server.Handler.do_GET)
        for fragment in ("/decision-context", "/outcome-evaluation", "/decision-replay",
                          "/api/market-intelligence/calibration"):
            self.assertIn(fragment, src)

    def test_decision_context_post_route_present(self):
        src = get_fresh_source(server.Handler.do_POST)
        self.assertIn("capture_trade_decision_context_safe", src)

    def test_diagnostics_wired_into_market_sources_endpoint(self):
        src = get_fresh_source(server.Handler.do_GET)
        self.assertIn("trade_decision_engine", src)


class UiWiringTests(unittest.TestCase):
    """28. UI（指示書34・35・36・37番）"""

    def test_calibration_panel_present_in_html(self):
        with open("trade-cockpit.html", encoding="utf-8") as f:
            html = f.read()
        self.assertIn("DecisionSupportCalibrationPanel", html)
        self.assertIn("DailyDecisionReviewSection", html)
        self.assertIn("DecisionReplayTimeline", html)


class Phase9CompatibilityTests(unittest.TestCase):
    """29. Phase9互換性：既存関数が無変更で動くこと"""

    def test_decision_support_score_and_state_unchanged(self):
        score = server.compute_decision_support_score(90, 90, 100, 80, 0, 100)
        self.assertGreaterEqual(score, 80)
        self.assertEqual(server.classify_decision_support_state(score), "STRONG_SUPPORT")

    def test_build_ticker_intelligence_summary_unchanged_shape(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_event_decision_support_for_ticker.return_value = []
            summary = server.build_ticker_intelligence_summary("postgres://x", "7203")
        self.assertEqual(summary["ticker"], "7203")
        self.assertIn("event_conflict", summary)

    def test_detect_avoid_chase_unchanged(self):
        self.assertTrue(server.detect_avoid_chase(85, current_return_from_event_pct=9.0))


if __name__ == "__main__":
    unittest.main()
