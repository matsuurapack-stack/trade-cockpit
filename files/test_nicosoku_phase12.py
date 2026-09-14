# Market Intelligence Phase12 テスト（指示書63番：最低26項目）。
#
# Phase2〜11同様、実DBを必要としない形でカバーする。DB操作が必要な箇所は
# mock.patch.object(server, "investment_db") で完全にモックする。
#
# 実行方法： cd files && python -m unittest test_nicosoku_phase12 -v

import datetime
import unittest
from unittest import mock

import server
from test_support_source_inspect import get_fresh_source


class ValidationSessionCreationTests(unittest.TestCase):
    """1. validation session creation（指示書2・3番）"""

    def test_start_validation_session_calls_db_with_versions(self):
        with mock.patch.object(server, "investment_db") as mock_db, \
             mock.patch.object(server, "_current_git_commit_hash", return_value="abc1234"):
            mock_db.get_or_create_validation_session.return_value = {"session_date": "2026-09-11"}
            server.DATABASE_URL = "postgres://x"
            result = server.start_validation_session()
        self.assertIsNotNone(result)
        args, kwargs = mock_db.get_or_create_validation_session.call_args
        self.assertEqual(kwargs["validation_version"], server.MARKET_INTELLIGENCE_VALIDATION_VERSION)
        self.assertEqual(kwargs["config_version"], server.DECISION_SUPPORT_CONFIG_VERSION)
        self.assertEqual(kwargs["commit_hash"], "abc1234")


class ConfigVersionPersistenceTests(unittest.TestCase):
    """2. config version persistence（指示書1・2・25番：Phase12期間中は固定）"""

    def test_validation_version_and_config_version_are_module_constants(self):
        self.assertEqual(server.MARKET_INTELLIGENCE_VALIDATION_VERSION, "MI_VALIDATION_V1")
        self.assertEqual(server.DECISION_SUPPORT_CONFIG_VERSION, "DS_V1")

    def test_decision_support_weights_frozen(self):
        # 指示書1番：Feature Freeze——weightsはバグ修正以外で変更しない。
        self.assertEqual(server.DECISION_SUPPORT_WEIGHTS["material_quality"], 0.30)
        self.assertEqual(server.DECISION_SUPPORT_WEIGHTS["reaction_quality"], 0.30)


class SchemaIntegrityTests(unittest.TestCase):
    """3. schema integrity（指示書11番）"""

    def test_check_schema_integrity_reports_missing_tables(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.check_schema_integrity.return_value = {
                "ok": False, "tables": {"underlying_events": True, "parser_failure_queue": False},
                "missing": ["parser_failure_queue"]}
            server.DATABASE_URL = "postgres://x"
            result = server.check_market_intelligence_schema_integrity()
        self.assertFalse(result["ok"])
        self.assertIn("parser_failure_queue", result["missing"])

    def test_no_db_configured_reports_all_missing(self):
        with mock.patch.object(server, "investment_db", None):
            result = server.check_market_intelligence_schema_integrity()
        self.assertFalse(result["ok"])


class SchedulerHeartbeatTests(unittest.TestCase):
    """4. scheduler heartbeat（指示書13番）"""

    def test_tick_success_updates_heartbeat(self):
        server._scheduler_heartbeats["event_reaction"] = server._empty_heartbeat()
        server._mark_scheduler_tick("event_reaction")
        server._mark_scheduler_success("event_reaction", processed_count=3)
        hb = server.get_scheduler_diagnostics()["event_reaction"]
        self.assertIsNotNone(hb["last_run_at"])
        self.assertIsNotNone(hb["last_success_at"])
        self.assertEqual(hb["processed_count"], 3)

    def test_error_updates_heartbeat(self):
        server._scheduler_heartbeats["candidate_outcome"] = server._empty_heartbeat()
        server._mark_scheduler_tick("candidate_outcome")
        server._mark_scheduler_error("candidate_outcome", Exception("boom"))
        hb = server.get_scheduler_diagnostics()["candidate_outcome"]
        self.assertIsNotNone(hb["last_error_at"])
        self.assertIn("boom", hb["last_error"])


class SchedulerDuplicateRestartProtectionTests(unittest.TestCase):
    """5. scheduler duplicate restart protection（指示書14・28・43番）"""

    def test_evaluated_snapshot_not_reprocessed(self):
        # list_due側でoutcome_status IS NULLのみ返す前提——評価済みは再取得されない。
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_due_entry_candidate_snapshots_for_backfill.return_value = []
            result = server.run_due_candidate_outcomes("postgres://x", "local")
        self.assertEqual(result["evaluated"], 0)
        mock_db.save_entry_candidate_snapshot_result.assert_not_called()


class SourceHealthTests(unittest.TestCase):
    """6. source health（指示書6・40番）"""

    def test_build_source_health_report_includes_all_configured_sources(self):
        with mock.patch.object(server, "investment_db") as mock_db, \
             mock.patch.object(server, "get_market_source_diagnostics", return_value={"poller_running": True}):
            mock_db.list_watchlist.return_value = []
            report = server.build_source_health_report("postgres://x")
        handles = {cfg["handle"] for cfg in server.MARKET_SOURCE_CONFIGS}
        self.assertEqual(set(report.keys()), handles)


class SourceStaleVsNoNewPostTests(unittest.TestCase):
    """7. source stale vs no-new-post（指示書8番）"""

    def test_fetch_failed_when_fetch_itself_failing(self):
        self.assertEqual(server.classify_source_freshness("kgbukabu", 100, 5, last_fetch_failed=True), "FETCH_FAILED")

    def test_stale_when_fetch_ok_but_no_new_post(self):
        self.assertEqual(server.classify_source_freshness("kgbukabu", 2, 30), "STALE")

    def test_ok_when_within_sla(self):
        self.assertEqual(server.classify_source_freshness("kgbukabu", 1, 5), "OK")


class RateLimitTrackingTests(unittest.TestCase):
    """8. rate limit tracking（指示書6・7番）：既存source診断構造にconsecutive_failures等が
    存在すること"""

    def test_source_health_report_surfaces_consecutive_failures(self):
        with mock.patch.object(server, "investment_db") as mock_db, \
             mock.patch.object(server, "get_market_source_diagnostics",
                                 return_value={"poller_running": True, "consecutive_failures": 2}):
            mock_db.list_watchlist.return_value = []
            report = server.build_source_health_report("postgres://x")
        any_handle = next(iter(report))
        self.assertEqual(report[any_handle]["consecutive_failures"], 2)


class ParserFailureTests(unittest.TestCase):
    """9. parser failure（指示書27番）"""

    def test_record_parser_failure_safe_calls_db(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.record_parser_failure.return_value = {"id": 1, "status": "PENDING"}
            result = server.record_parser_failure_safe("postgres://x", "kgbukabu", "123", "stock_breaking", "parse error")
        self.assertEqual(result["status"], "PENDING")


class DeadLetterTests(unittest.TestCase):
    """10. dead letter（指示書28番）：無限retry禁止、閾値超過でDEAD_LETTER"""

    def test_dead_letter_threshold_constant_used(self):
        import investment_db
        self.assertGreater(investment_db.PARSER_FAILURE_DEAD_LETTER_RETRY_THRESHOLD, 0)

    def test_record_parser_failure_sql_sets_dead_letter_on_threshold(self):
        # linecache汚染対策（Bugfix: isolate global state between test modules）：
        # test_support_source_inspect.get_fresh_source参照。
        import investment_db
        src = get_fresh_source(investment_db.record_parser_failure)
        self.assertIn("DEAD_LETTER", src)
        self.assertIn("PARSER_FAILURE_DEAD_LETTER_RETRY_THRESHOLD", src)


class OrphanDetectionTests(unittest.TestCase):
    """11. orphan detection（指示書16番）"""

    def test_run_orphan_audit_aggregates_counts(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.count_orphan_evidence.return_value = 0
            mock_db.count_orphan_event_market_reactions.return_value = 1
            mock_db.count_orphan_event_decision_support.return_value = 0
            mock_db.count_orphan_event_decision_transitions.return_value = 0
            result = server.run_orphan_audit("postgres://x")
        self.assertEqual(result["orphan_event_market_reactions"], 1)


class DuplicateAuditTests(unittest.TestCase):
    """12. duplicate audit（指示書15番）"""

    def test_run_duplicate_audit_aggregates_counts(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.count_duplicate_underlying_events.return_value = 2
            mock_db.count_duplicate_candidate_snapshots.return_value = 0
            mock_db.count_duplicate_event_market_reactions.return_value = 0
            result = server.run_duplicate_audit("postgres://x")
        self.assertEqual(result["duplicate_underlying_events"], 2)


class CandidateCoverageTests(unittest.TestCase):
    """13. candidate coverage（指示書24・25番）"""

    def test_coverage_rate_reused_from_phase11(self):
        self.assertEqual(server.compute_candidate_coverage_rate(100, 95), 0.95)


class OutcomeCompletionTests(unittest.TestCase):
    """14. outcome completion（指示書25番）"""

    def test_completion_rate_computed(self):
        snapshots = [{"outcome_status": "SUCCESS"}, {"outcome_status": None}, {"outcome_status": "FAILED"}]
        self.assertAlmostEqual(server.compute_outcome_completion_rate(snapshots), 2 / 3, places=3)

    def test_empty_returns_none(self):
        self.assertIsNone(server.compute_outcome_completion_rate([]))


class ReplayDiagnosticsToleranceTests(unittest.TestCase):
    """15. replay diagnostics（指示書20・21番）"""

    def test_exact_state_match(self):
        self.assertEqual(server.classify_replay_match(actual_state="SUPPORTIVE", replay_state="SUPPORTIVE"), "EXACT_STATE_MATCH")

    def test_near_match_within_price_tolerance(self):
        result = server.classify_replay_match(actual_state="SUPPORTIVE", replay_state=None,
                                                  actual_price=1000, replay_price=1001)
        self.assertEqual(result, "NEAR_MATCH")

    def test_drift_outside_tolerance(self):
        result = server.classify_replay_match(actual_state="SUPPORTIVE", replay_state="AVOID_CHASE",
                                                  actual_price=1000, replay_price=1050)
        self.assertEqual(result, "DRIFT")

    def test_build_replay_match_summary(self):
        samples = [{"actual_state": "SUPPORTIVE", "replay_state": "SUPPORTIVE"},
                    {"actual_state": "AVOID_CHASE", "replay_state": "SUPPORTIVE"}]
        summary = server.build_replay_match_summary(samples)
        self.assertEqual(summary["samples"], 2)
        self.assertEqual(summary["exact_state_matches"], 1)
        self.assertEqual(summary["drifts"], 1)
        self.assertEqual(summary["match_rate"], 0.5)


class LowQualitySampleTests(unittest.TestCase):
    """16. low quality sample（指示書27・28番）：日次レポートに反映される"""

    def test_daily_report_counts_low_quality_samples(self):
        snapshots = [{"candidate_at": "2026-09-11T01:00:00+00:00", "data_quality_score": 40,
                       "candidate_type": "WAIT", "outcome_status": "CORRECT_WAIT"}]
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_active_underlying_events.return_value = []
            mock_db.list_entry_candidate_snapshots.return_value = snapshots
            mock_db.count_duplicate_underlying_events.return_value = 0
            mock_db.count_duplicate_candidate_snapshots.return_value = 0
            mock_db.count_duplicate_event_market_reactions.return_value = 0
            mock_db.list_recent_event_decision_support.return_value = []
            mock_db.count_parser_failures.return_value = 0
            report = server.build_daily_validation_report("postgres://x", "local", "2026-09-11")
        self.assertEqual(report["low_quality_samples"], 1)


class GracefulDegradationDbTests(unittest.TestCase):
    """17. graceful degradation DB（指示書52・53番）"""

    def test_dashboard_safe_survives_db_exception(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_entry_candidate_snapshots.side_effect = Exception("db down")
            result = server.build_data_quality_dashboard_safe("postgres://x", "local")
        self.assertIn("overall_health", result)


class GracefulDegradationXTests(unittest.TestCase):
    """18. graceful degradation X（指示書41・52番）"""

    def test_validate_market_sources_not_configured_without_token(self):
        with mock.patch.object(server, "X_API_BEARER_TOKEN", None):
            results = server.validate_market_sources()
        self.assertTrue(all(r["status"] == "NOT_CONFIGURED" for r in results.values()))

    def test_validate_market_sources_survives_exception(self):
        with mock.patch.object(server, "X_API_BEARER_TOKEN", "dummy"), \
             mock.patch.object(server, "_x_resolve_user_id", side_effect=Exception("network error")):
            results = server.validate_market_sources()
        self.assertTrue(all(r["status"] == "FAIL" for r in results.values()))


class GracefulDegradationParserTests(unittest.TestCase):
    """19. graceful degradation parser（指示書27・52番）"""

    def test_record_parser_failure_safe_survives_db_exception(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.record_parser_failure.side_effect = Exception("db down")
            result = server.record_parser_failure_safe("postgres://x", "kgbukabu", "1", "p", "err")
        self.assertIsNone(result)


class UiTimeoutTests(unittest.TestCase):
    """20. UI timeout（指示書54番）"""

    def test_timeout_wrapper_present_in_html(self):
        with open("trade-cockpit.html", encoding="utf-8") as f:
            html = f.read()
        self.assertIn("fetchJsonSafeTimeout", html)
        self.assertIn("AbortController", html)


class HealthGreenTests(unittest.TestCase):
    """21. health GREEN（指示書41番）"""

    def test_all_clean_is_green(self):
        status = {"db_ok": True, "sources": {"a": "OK", "b": "OK"}, "duplicates": {"x": 0},
                    "snapshot_coverage": 0.97, "schedulers_running": True, "replay_match_rate": 0.9,
                    "parser_failures": 0, "outcome_backlog": 0}
        self.assertEqual(server.compute_overall_health(status), "GREEN")


class HealthYellowTests(unittest.TestCase):
    """22. health YELLOW（指示書43番）"""

    def test_stale_source_is_yellow(self):
        status = {"db_ok": True, "sources": {"a": "OK", "b": "STALE"}, "duplicates": {"x": 0},
                    "snapshot_coverage": 0.9, "schedulers_running": True, "replay_match_rate": 0.9,
                    "parser_failures": 0, "outcome_backlog": 0}
        self.assertEqual(server.compute_overall_health(status), "YELLOW")

    def test_outcome_backlog_is_yellow(self):
        status = {"db_ok": True, "sources": {"a": "OK"}, "duplicates": {"x": 0},
                    "snapshot_coverage": 0.9, "schedulers_running": True, "replay_match_rate": 0.9,
                    "parser_failures": 0, "outcome_backlog": 60}
        self.assertEqual(server.compute_overall_health(status), "YELLOW")


class HealthRedTests(unittest.TestCase):
    """23. health RED（指示書42番）"""

    def test_db_error_is_red(self):
        status = {"db_ok": False, "sources": {}, "duplicates": {}, "schedulers_running": True}
        self.assertEqual(server.compute_overall_health(status), "RED")

    def test_low_coverage_is_red(self):
        status = {"db_ok": True, "sources": {"a": "OK"}, "duplicates": {"x": 0},
                    "snapshot_coverage": 0.5, "schedulers_running": True}
        self.assertEqual(server.compute_overall_health(status), "RED")

    def test_duplicate_corruption_is_red(self):
        status = {"db_ok": True, "sources": {"a": "OK"}, "duplicates": {"x": 3},
                    "snapshot_coverage": 0.9, "schedulers_running": True}
        self.assertEqual(server.compute_overall_health(status), "RED")


class DailyValidationReportTests(unittest.TestCase):
    """24. daily validation report（指示書37・38・39番）"""

    def test_report_separates_system_quality_from_trading_score(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_active_underlying_events.return_value = []
            mock_db.list_entry_candidate_snapshots.return_value = []
            mock_db.count_duplicate_underlying_events.return_value = 0
            mock_db.count_duplicate_candidate_snapshots.return_value = 0
            mock_db.count_duplicate_event_market_reactions.return_value = 0
            mock_db.list_recent_event_decision_support.return_value = []
            mock_db.count_parser_failures.return_value = 0
            report = server.build_daily_validation_report("postgres://x", "local", "2026-09-11")
        # 指示書39番：売買評価（score_total等）に触れるキーが無いことを確認。
        self.assertNotIn("score_total", report)
        self.assertIn("event_count", report)
        mock_db.generate_daily_review.assert_not_called()


class ProductionSmokeHelperTests(unittest.TestCase):
    """25. production smoke helper（指示書57番）"""

    def test_checklist_reports_not_run_without_infra(self):
        with mock.patch.object(server, "investment_db", None), \
             mock.patch.object(server, "X_API_BEARER_TOKEN", None):
            checklist = server.run_production_smoke_checklist()
        steps = {c["step"]: c["status"] for c in checklist}
        self.assertEqual(steps["db_migration"], "NOT_RUN")
        self.assertEqual(steps["x_source_fetch"], "NOT_RUN")
        self.assertEqual(steps["server_boot"], "PASS")


class Phase11CompatibilityTests(unittest.TestCase):
    """26. Phase11互換性：既存関数が無変更で動くこと"""

    def test_classify_candidate_outcome_unchanged(self):
        self.assertEqual(server.classify_candidate_outcome("AVOID_CHASE", subsequent_close_pct=-8.0), "AVOIDED_LOSS")

    def test_aggregate_candidate_performance_unchanged(self):
        snapshots = [{"candidate_type": "ENTRY", "outcome_status": "SUCCESS", "subsequent_30m_pct": 1.0}]
        result = server.aggregate_candidate_performance(snapshots)
        self.assertEqual(result["ENTRY"]["count"], 1)


if __name__ == "__main__":
    unittest.main()
