# Market Intelligence Phase12.5 テスト（指示書48番：最低13項目）。
#
# Phase12.5は運用フェーズであり新規ロジックは少ない。主にprogress/readiness判定・
# config change監査・replay drift理由分類の補助関数をカバーする。
#
# 実行方法： cd files && python -m unittest test_nicosoku_phase12_5 -v

import unittest
from unittest import mock

import server


class RealSourceValidatorHelperTests(unittest.TestCase):
    """1. real-source validator helper（指示書1・2・41番）"""

    def test_validate_market_sources_callable(self):
        self.assertTrue(callable(server.validate_market_sources))

    def test_not_configured_without_token(self):
        with mock.patch.object(server, "X_API_BEARER_TOKEN", None):
            results = server.validate_market_sources()
        self.assertTrue(all(r["status"] == "NOT_CONFIGURED" for r in results.values()))


class FourSourceResultStructureTests(unittest.TestCase):
    """2. four-source result structure（指示書2・3番）"""

    def test_all_four_handles_present(self):
        with mock.patch.object(server, "X_API_BEARER_TOKEN", None):
            results = server.validate_market_sources()
        self.assertEqual(set(results.keys()), {"nicosokufx", "polymarketjapan", "kgbukabu", "aryarya"})


class ParserAuditStorageTests(unittest.TestCase):
    """3. parser audit storage（指示書5・6・25番）"""

    def test_source_quality_report_uses_parser_failures(self):
        with mock.patch.object(server, "investment_db") as mock_db, \
             mock.patch.object(server, "get_market_source_diagnostics", return_value={"poller_running": True}):
            mock_db.list_watchlist.return_value = []
            mock_db.list_parser_failures.return_value = [
                {"source": "kgbukabu", "status": "PENDING"}, {"source": "kgbukabu", "status": "DEAD_LETTER"},
                {"source": "aryarya", "status": "PENDING"}]
            report = server.build_source_quality_report("postgres://x")
        self.assertEqual(report["kgbukabu"]["parser_failures"], 2)
        self.assertEqual(report["aryarya"]["parser_failures"], 1)
        self.assertEqual(report["nicosokufx"]["parser_failures"], 0)


class ValidationProgressTests(unittest.TestCase):
    """4. validation progress（指示書41番）"""

    def test_progress_computes_current_target_pct(self):
        counts = {"candidate_snapshots": 50, "evaluated_outcomes": 40, "wait_samples": 10,
                   "avoid_chase_samples": 5, "underlying_events": 15, "trading_days": 3}
        progress = server.compute_phase13_progress(counts)
        self.assertEqual(progress["candidate_snapshots"]["current"], 50)
        self.assertEqual(progress["candidate_snapshots"]["target"], 100)
        self.assertEqual(progress["candidate_snapshots"]["pct"], 50.0)


class ReadinessNotReadyTests(unittest.TestCase):
    """5. readiness NOT_READY（指示書42番）"""

    def test_all_zero_is_not_ready(self):
        counts = {k: 0 for k in server.PHASE13_SAMPLE_TARGETS}
        self.assertEqual(server.compute_phase13_readiness(counts), "NOT_READY")


class ReadinessCollectingTests(unittest.TestCase):
    """6. readiness COLLECTING（指示書42番）"""

    def test_partial_progress_is_collecting(self):
        counts = {"candidate_snapshots": 40, "evaluated_outcomes": 20, "wait_samples": 5,
                   "avoid_chase_samples": 3, "underlying_events": 10, "trading_days": 2}
        self.assertEqual(server.compute_phase13_readiness(counts), "COLLECTING")


class ReadinessReadyTests(unittest.TestCase):
    """7. readiness READY（指示書42・43番）"""

    def test_all_targets_met_is_ready_for_review(self):
        counts = {"candidate_snapshots": 120, "evaluated_outcomes": 90, "wait_samples": 25,
                   "avoid_chase_samples": 20, "underlying_events": 35, "trading_days": 6}
        self.assertEqual(server.compute_phase13_readiness(counts), "READY_FOR_PHASE13_REVIEW")

    def test_readiness_never_auto_enables_phase13(self):
        # 指示書43番REQUIRED：判定を返すだけで、他の状態を書き換えるような副作用が無いこと。
        counts = {"candidate_snapshots": 120, "evaluated_outcomes": 90, "wait_samples": 25,
                   "avoid_chase_samples": 20, "underlying_events": 35, "trading_days": 6}
        before = dict(server.DECISION_SUPPORT_WEIGHTS)
        server.compute_phase13_readiness(counts)
        self.assertEqual(server.DECISION_SUPPORT_WEIGHTS, before)

    def test_critical_duplicate_forces_review_required(self):
        counts = {"candidate_snapshots": 120, "evaluated_outcomes": 90, "wait_samples": 25,
                   "avoid_chase_samples": 20, "underlying_events": 35, "trading_days": 6}
        self.assertEqual(server.compute_phase13_readiness(counts, critical_duplicate=1), "REVIEW_REQUIRED")


class ConfigFreezeTests(unittest.TestCase):
    """8. config freeze（指示書14・33・34番）"""

    def test_decision_support_weights_unchanged(self):
        self.assertEqual(server.DECISION_SUPPORT_WEIGHTS["material_quality"], 0.30)
        self.assertEqual(server.DECISION_SUPPORT_WEIGHTS["reaction_quality"], 0.30)

    def test_shadow_mode_and_validation_version_constants_present(self):
        self.assertTrue(server.SHADOW_MODE)
        self.assertEqual(server.DECISION_SUPPORT_CONFIG_VERSION, "DS_V1")


class ConfigChangeAuditTests(unittest.TestCase):
    """9. config change audit（指示書35番）"""

    def test_record_config_change_safe_calls_db_with_before_after(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.record_config_change.return_value = {"id": 1, "reason": "bugfix"}
            result = server.record_config_change_safe(
                "postgres://x", "material_quality weight bugfix", {"material_quality": 0.30},
                {"material_quality": 0.28}, commit_hash="abc123")
        self.assertEqual(result["id"], 1)
        mock_db.record_config_change.assert_called_once_with(
            "postgres://x", "material_quality weight bugfix", {"material_quality": 0.30},
            {"material_quality": 0.28}, "abc123")

    def test_record_config_change_safe_survives_db_exception(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.record_config_change.side_effect = Exception("db down")
            result = server.record_config_change_safe("postgres://x", "x", {}, {})
        self.assertIsNone(result)


class ReplayDriftReasonTests(unittest.TestCase):
    """10. replay drift reason（指示書38番）"""

    def test_price_data_difference(self):
        reason = server.classify_replay_drift_reason({"price": 1000}, {"price": 1010})
        self.assertEqual(reason, "PRICE_DATA_DIFFERENCE")

    def test_vwap_difference(self):
        reason = server.classify_replay_drift_reason({"price": 1000, "vwap": 1000}, {"price": 1000, "vwap": 1010})
        self.assertEqual(reason, "VWAP_DIFFERENCE")

    def test_event_data_difference(self):
        reason = server.classify_replay_drift_reason(
            {"independent_source_count": 1}, {"independent_source_count": 2})
        self.assertEqual(reason, "EVENT_DATA_DIFFERENCE")

    def test_code_version_difference(self):
        reason = server.classify_replay_drift_reason(
            {"config_version": "DS_V1"}, {"config_version": "DS_V2"})
        self.assertEqual(reason, "CODE_VERSION_DIFFERENCE")

    def test_unknown_when_no_signal(self):
        self.assertEqual(server.classify_replay_drift_reason({}, {}), "UNKNOWN")


class LiveCandidateCountsTests(unittest.TestCase):
    """11. live candidate counts（指示書26・41番）"""

    def test_counts_aggregated_from_db(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.count_entry_candidate_snapshots_total.side_effect = lambda db, uid, candidate_type=None: {
                None: 60, "WAIT": 12, "AVOID_CHASE": 8}[candidate_type]
            mock_db.count_entry_candidate_snapshots_evaluated_total.return_value = 45
            mock_db.count_underlying_events_total.return_value = 20
            mock_db.count_validation_sessions.return_value = 4
            counts = server.get_live_candidate_counts("postgres://x", "local")
        self.assertEqual(counts["candidate_snapshots"], 60)
        self.assertEqual(counts["wait_samples"], 12)
        self.assertEqual(counts["avoid_chase_samples"], 8)
        self.assertEqual(counts["evaluated_outcomes"], 45)
        self.assertEqual(counts["underlying_events"], 20)
        self.assertEqual(counts["trading_days"], 4)


class DailyValidationReportPhase125Tests(unittest.TestCase):
    """12. daily validation report（指示書18・19番、Phase12から継続）"""

    def test_daily_validation_report_still_works(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_active_underlying_events.return_value = []
            mock_db.list_entry_candidate_snapshots.return_value = []
            mock_db.count_duplicate_underlying_events.return_value = 0
            mock_db.count_duplicate_candidate_snapshots.return_value = 0
            mock_db.count_duplicate_event_market_reactions.return_value = 0
            mock_db.list_recent_event_decision_support.return_value = []
            mock_db.count_parser_failures.return_value = 0
            report = server.build_daily_validation_report("postgres://x", "local", "2026-09-11")
        self.assertIn("event_count", report)
        self.assertIn("outcome_completion", report)


class Phase12CompatibilityTests(unittest.TestCase):
    """13. Phase12互換性：既存関数が無変更で動くこと"""

    def test_compute_overall_health_unchanged(self):
        status = {"db_ok": True, "sources": {"a": "OK"}, "duplicates": {"x": 0},
                    "snapshot_coverage": 0.97, "schedulers_running": True, "replay_match_rate": 0.9,
                    "parser_failures": 0, "outcome_backlog": 0}
        self.assertEqual(server.compute_overall_health(status), "GREEN")

    def test_classify_replay_match_unchanged(self):
        self.assertEqual(server.classify_replay_match(actual_state="SUPPORTIVE", replay_state="SUPPORTIVE"),
                          "EXACT_STATE_MATCH")

    def test_scheduler_registry_unchanged(self):
        self.assertIn("candidate_outcome", server.SCHEDULER_REGISTRY)


if __name__ == "__main__":
    unittest.main()
