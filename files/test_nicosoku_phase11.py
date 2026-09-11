# Market Intelligence Phase11 テスト（指示書45番：最低31項目）。
#
# Phase2〜10同様、実DBを必要としない形でカバーする。DB操作が必要な箇所は
# mock.patch.object(server, "investment_db") で完全にモックする。
#
# 実行方法： cd files && python -m unittest test_nicosoku_phase11 -v

import datetime
import unittest
from unittest import mock

import server


def _iso(dt):
    return dt.astimezone(datetime.timezone.utc).isoformat()


class CandidateSnapshotTests(unittest.TestCase):
    """1. candidate snapshot（指示書1・3・4・29・30番）"""

    def test_capture_builds_dedupe_key_and_saves(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.create_entry_candidate_snapshot.side_effect = lambda db, uid, fields: fields
            saved = server.capture_entry_candidate_snapshot(
                "postgres://x", "local", "7203", "ENTRY", price=1000, entry_score=82,
                event_support="STRONG", candidate_state="STRONG", candidate_rank=1)
        self.assertEqual(saved["candidate_type"], "ENTRY")
        self.assertIn("7203:ENTRY:", saved["dedupe_key"])
        self.assertIn("STRONG", saved["dedupe_key"])
        self.assertEqual(saved["config_version"], server.DECISION_SUPPORT_CONFIG_VERSION)


class CandidateDedupeTests(unittest.TestCase):
    """2. candidate dedupe（指示書3・4番）：同日・同銘柄・同状態は同じdedupe_key、状態が
    変われば別key"""

    def test_same_state_same_day_same_key(self):
        t1 = "2026-09-11T09:10:00+00:00"
        t2 = "2026-09-11T09:40:00+00:00"
        key1 = server.build_candidate_dedupe_key("7203", "WAIT", t1, "WAIT")
        key2 = server.build_candidate_dedupe_key("7203", "WAIT", t2, "WAIT")
        self.assertEqual(key1, key2)

    def test_state_change_produces_different_key(self):
        key_wait = server.build_candidate_dedupe_key("7203", "WAIT", "2026-09-11T09:10:00+00:00", "WAIT")
        key_supportive = server.build_candidate_dedupe_key("7203", "WAIT", "2026-09-11T10:05:00+00:00", "SUPPORTIVE")
        self.assertNotEqual(key_wait, key_supportive)

    def test_unique_index_present_in_schema(self):
        import investment_db
        self.assertIn("idx_entry_candidate_snapshots_dedupe", investment_db._MIGRATE_ENTRY_CANDIDATE_SNAPSHOTS_V2_SQL)


class ThirtyMinuteOutcomeTests(unittest.TestCase):
    """3. 30M outcome（指示書1・6番）"""

    def test_scheduler_fills_30m_after_elapsed(self):
        snap = {"id": 1, "code": "7203", "price_at_candidate": 1000, "subsequent_30m_pct": None,
                "subsequent_1h_pct": None, "subsequent_close_pct": None, "candidate_type": "ENTRY",
                "candidate_at": _iso(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=35)),
                "extension_score": None}
        with mock.patch.object(server, "investment_db") as mock_db, \
             mock.patch.object(server, "get_stock_quotes") as mock_quotes, \
             mock.patch.object(server, "classify_event_timing", return_value="IN_SESSION"):
            mock_db.list_due_entry_candidate_snapshots_for_backfill.return_value = [snap]
            mock_quotes.return_value = {"7203": {"t": 1030, "high": 1040, "low": 990}}
            server.run_due_candidate_outcomes("postgres://x", "local")
        args, kwargs = mock_db.save_entry_candidate_snapshot_result.call_args
        self.assertAlmostEqual(kwargs["subsequent_30m_pct"], 3.0, places=1)


class OneHourOutcomeTests(unittest.TestCase):
    """4. 1H outcome（指示書1・6番）"""

    def test_scheduler_fills_1h_after_elapsed(self):
        snap = {"id": 1, "code": "7203", "price_at_candidate": 1000, "subsequent_30m_pct": 2.0,
                "subsequent_1h_pct": None, "subsequent_close_pct": None, "candidate_type": "ENTRY",
                "candidate_at": _iso(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=65)),
                "extension_score": None}
        with mock.patch.object(server, "investment_db") as mock_db, \
             mock.patch.object(server, "get_stock_quotes") as mock_quotes, \
             mock.patch.object(server, "classify_event_timing", return_value="IN_SESSION"):
            mock_db.list_due_entry_candidate_snapshots_for_backfill.return_value = [snap]
            mock_quotes.return_value = {"7203": {"t": 1050, "high": 1060, "low": 995}}
            server.run_due_candidate_outcomes("postgres://x", "local")
        args, kwargs = mock_db.save_entry_candidate_snapshot_result.call_args
        self.assertAlmostEqual(kwargs["subsequent_1h_pct"], 5.0, places=1)


class CloseOutcomeTests(unittest.TestCase):
    """5. CLOSE outcome（指示書1・6番）：AFTER_CLOSEタイミングでoutcome_statusが確定する"""

    def test_scheduler_finalizes_outcome_after_close(self):
        snap = {"id": 1, "code": "7203", "price_at_candidate": 1000, "subsequent_30m_pct": 2.0,
                "subsequent_1h_pct": 3.0, "subsequent_close_pct": None, "candidate_type": "ENTRY",
                "candidate_at": _iso(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=6)),
                "extension_score": 40}
        with mock.patch.object(server, "investment_db") as mock_db, \
             mock.patch.object(server, "get_stock_quotes") as mock_quotes, \
             mock.patch.object(server, "classify_event_timing", return_value="AFTER_CLOSE"):
            mock_db.list_due_entry_candidate_snapshots_for_backfill.return_value = [snap]
            mock_quotes.return_value = {"7203": {"t": 1030, "high": 1040, "low": 990}}
            server.run_due_candidate_outcomes("postgres://x", "local")
        args, kwargs = mock_db.save_entry_candidate_snapshot_result.call_args
        self.assertIsNotNone(kwargs["subsequent_close_pct"])
        self.assertEqual(kwargs["outcome_status"], "SUCCESS")
        self.assertIsNotNone(kwargs["outcome_evaluated_at"])


class MfeTests(unittest.TestCase):
    """6. MFE（指示書11番）：intraday high/lowベース"""

    def test_mfe_from_intraday_high(self):
        mfe, mae, quality = server.compute_candidate_mfe_mae(1000, high_since_candidate=1050,
                                                                low_since_candidate=990, data_quality_hint="EXACT")
        self.assertEqual(mfe, 5.0)
        self.assertEqual(quality, "EXACT")


class MaeTests(unittest.TestCase):
    """7. MAE（指示書11番）"""

    def test_mae_from_intraday_low(self):
        mfe, mae, quality = server.compute_candidate_mfe_mae(1000, high_since_candidate=1010,
                                                                low_since_candidate=950, data_quality_hint="EXACT")
        self.assertEqual(mae, -5.0)

    def test_missing_high_low_is_estimated(self):
        mfe, mae, quality = server.compute_candidate_mfe_mae(1000)
        self.assertIsNone(mfe)
        self.assertIsNone(mae)
        self.assertEqual(quality, "ESTIMATED")


class CorrectWaitOutcomeTests(unittest.TestCase):
    """8. CORRECT_WAIT（指示書7番）"""

    def test_wait_then_decline_is_correct_wait(self):
        self.assertEqual(server.classify_candidate_outcome("WAIT", subsequent_close_pct=-3.0), "CORRECT_WAIT")


class GoodPullbackOutcomeTests(unittest.TestCase):
    """9. GOOD_PULLBACK（指示書7番）"""

    def test_pullback_then_supportive(self):
        self.assertEqual(
            server.classify_candidate_outcome("WAIT", subsequent_close_pct=4.0, pullback_then_supportive=True),
            "GOOD_PULLBACK")


class MissedBreakoutOutcomeTests(unittest.TestCase):
    """10. MISSED_BREAKOUT（指示書7・8番）：WAIT・AVOID_CHASE双方で発生し得る"""

    def test_wait_large_gain_is_missed_breakout(self):
        self.assertEqual(server.classify_candidate_outcome("WAIT", subsequent_close_pct=20.0), "MISSED_BREAKOUT")

    def test_avoid_chase_large_gain_is_missed_breakout(self):
        self.assertEqual(server.classify_candidate_outcome("AVOID_CHASE", subsequent_close_pct=18.0), "MISSED_BREAKOUT")


class AvoidedLossOutcomeTests(unittest.TestCase):
    """11. AVOIDED_LOSS（指示書8番）"""

    def test_avoid_chase_then_decline_is_avoided_loss(self):
        self.assertEqual(server.classify_candidate_outcome("AVOID_CHASE", subsequent_close_pct=-8.0), "AVOIDED_LOSS")


class ChaseTrapOutcomeTests(unittest.TestCase):
    """12. CHASE_TRAP（指示書9番）：過熱＋直後上昇→baseline以下"""

    def test_extended_then_reversal_is_chase_trap(self):
        outcome = server.classify_candidate_outcome(
            "ENTRY", subsequent_close_pct=-1.0, mfe_pct=6.0, mae_pct=-5.0, extension_score=88)
        self.assertEqual(outcome, "CHASE_TRAP")

    def test_not_chase_trap_when_extension_low(self):
        outcome = server.classify_candidate_outcome(
            "ENTRY", subsequent_close_pct=-1.0, mfe_pct=6.0, mae_pct=-5.0, extension_score=30)
        self.assertNotEqual(outcome, "CHASE_TRAP")


class EntryPerformanceTests(unittest.TestCase):
    """13. ENTRY performance（指示書17番）"""

    def test_aggregate_entry_type(self):
        snapshots = [{"candidate_type": "ENTRY", "outcome_status": "SUCCESS", "subsequent_30m_pct": 1.5,
                       "mfe_pct": 2.8, "mae_pct": -1.1}] * 10
        result = server.aggregate_candidate_performance(snapshots)
        self.assertEqual(result["ENTRY"]["count"], 10)
        self.assertEqual(result["ENTRY"]["success_rate"], 1.0)
        self.assertEqual(result["ENTRY"]["avg_30m_return"], 1.5)


class WaitPerformanceTests(unittest.TestCase):
    """14. WAIT performance（指示書17番）"""

    def test_aggregate_wait_type(self):
        snapshots = [{"candidate_type": "WAIT", "outcome_status": "CORRECT_WAIT", "subsequent_30m_pct": -1.0}] * 6
        result = server.aggregate_candidate_performance(snapshots)
        self.assertEqual(result["WAIT"]["count"], 6)
        self.assertEqual(result["WAIT"]["success_rate"], 1.0)


class AvoidChasePerformanceTests(unittest.TestCase):
    """15. AVOID_CHASE performance（指示書17番）"""

    def test_aggregate_avoid_chase_type(self):
        snapshots = [{"candidate_type": "AVOID_CHASE", "outcome_status": "AVOIDED_LOSS", "mfe_pct": 0.5,
                       "mae_pct": -6.0}] * 4
        result = server.aggregate_candidate_performance(snapshots)
        self.assertEqual(result["AVOID_CHASE"]["count"], 4)
        self.assertEqual(result["AVOID_CHASE"]["success_rate"], 1.0)


class RankPerformanceTests(unittest.TestCase):
    """16. rank performance（指示書18番）：entry_score自体は変更しない"""

    def test_top1_vs_top5_breakdown(self):
        snapshots = ([{"candidate_type": "ENTRY", "candidate_rank": 1, "subsequent_30m_pct": 1.8}] * 5 +
                      [{"candidate_type": "ENTRY", "candidate_rank": 5, "subsequent_30m_pct": 0.3}] * 5)
        result = server.aggregate_candidate_performance_by_rank(snapshots)
        self.assertEqual(result["TOP1"]["avg_30m_return"], 1.8)
        self.assertEqual(result["TOP5"]["avg_30m_return"], 0.3)


class WaitReasonPerformanceTests(unittest.TestCase):
    """17. wait reason performance（指示書20番）"""

    def test_wait_reason_breakdown(self):
        snapshots = ([{"candidate_type": "WAIT", "wait_reason": "EXTENDED", "outcome_status": "CORRECT_WAIT"}] * 3 +
                      [{"candidate_type": "WAIT", "wait_reason": "LOW_VOLUME", "outcome_status": "MISSED_BREAKOUT"}] * 3)
        result = server.aggregate_candidate_performance_by_wait_reason(snapshots)
        self.assertEqual(result["EXTENDED"]["correct_rate"], 1.0)
        self.assertEqual(result["LOW_VOLUME"]["correct_rate"], 0.0)


class HistoricalVwapReplayTests(unittest.TestCase):
    """18. historical VWAP replay（指示書12・13番）"""

    def test_vwap_computed_from_bars_up_to_as_of(self):
        bars = [
            {"timestamp": "2026-09-11T00:00:00+00:00", "high": 1010, "low": 990, "close": 1000, "volume": 100},
            {"timestamp": "2026-09-11T00:05:00+00:00", "high": 1020, "low": 1000, "close": 1010, "volume": 200},
        ]
        snap = server.get_intraday_snapshot_at("2026-09-11T00:05:00+00:00", bars)
        self.assertAlmostEqual(snap["vwap"], (1000 * 100 + 1010 * 200) / 300, places=2)


class HistoricalMaReplayTests(unittest.TestCase):
    """19. historical MA replay（指示書12・13番）"""

    def test_ma5m20_uses_last_20_bars_only(self):
        bars = [{"timestamp": f"2026-09-11T00:{i:02d}:00+00:00", "high": 1000 + i, "low": 990 + i,
                  "close": 1000 + i, "volume": 10} for i in range(30)]
        snap = server.get_intraday_snapshot_at("2026-09-11T00:29:00+00:00", bars)
        expected = sum(b["close"] for b in bars[-20:]) / 20
        self.assertAlmostEqual(snap["ma5m20"], expected, places=2)


class NoFutureLeakageTests(unittest.TestCase):
    """20. no future high/low leakage（指示書13番、REQUIRED）"""

    def test_future_bar_excluded_from_day_high(self):
        bars = [
            {"timestamp": "2026-09-11T00:00:00+00:00", "high": 1010, "low": 990, "close": 1000, "volume": 100},
            {"timestamp": "2026-09-11T00:30:00+00:00", "high": 9999, "low": 990, "close": 1005, "volume": 100},
        ]
        snap = server.get_intraday_snapshot_at("2026-09-11T00:05:00+00:00", bars)
        self.assertEqual(snap["day_high_so_far"], 1010)  # 未来(00:30)の9999を含まない


class ReplayParityTests(unittest.TestCase):
    """21. replay parity（指示書14番）：同一入力ならreplayとrealtimeがほぼ同じstateになる"""

    def test_replay_and_realtime_agree_on_same_inputs(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        event = {"id": 1, "event_type": "BUYBACK", "confidence_level": "OFFICIAL_CONFIRMED",
                 "primary_source_type": "TDNET", "independent_source_count": 1, "impact_score": 80,
                 "material_magnitude": 5, "first_seen_at": _iso(now - datetime.timedelta(minutes=10)),
                 "title": "自社株買い"}
        evidence = [{"posted_at": _iso(now - datetime.timedelta(minutes=10)), "source_name": "a", "is_primary": True,
                      "source_kind": "TDNET"}]
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.get_underlying_event.return_value = event
            mock_db.list_underlying_event_evidence.return_value = evidence
            mock_db.list_event_market_reactions_for_event.return_value = []
            replay = server.replay_decision_support_at("postgres://x", 1, "7203", _iso(now))
            realtime_material = server.compute_material_quality_score(event, {})
        self.assertAlmostEqual(replay["material_quality_score"], realtime_material, places=1)


class ReplayDriftTests(unittest.TestCase):
    """22. replay drift（指示書15・16番）"""

    def test_matching_states_no_drift(self):
        actual = [{"evaluated_at": "2026-09-11T00:00:00+00:00", "decision_support_state": "SUPPORTIVE"}]
        replay = [{"as_of": "2026-09-11T00:05:00+00:00", "decision_support_state": "SUPPORTIVE"}]
        result = server.compute_replay_drift(actual, replay)
        self.assertEqual(result["replay_drift_count"], 0)
        self.assertEqual(result["replay_match_rate"], 1.0)

    def test_mismatched_states_produce_drift(self):
        actual = [{"evaluated_at": "2026-09-11T00:00:00+00:00", "decision_support_state": "AVOID_CHASE"}]
        replay = [{"as_of": "2026-09-11T00:05:00+00:00", "decision_support_state": "SUPPORTIVE"}]
        result = server.compute_replay_drift(actual, replay)
        self.assertEqual(result["replay_drift_count"], 1)
        self.assertEqual(result["replay_match_rate"], 0.0)


class DataQualityScoreTests(unittest.TestCase):
    """23. data quality score（指示書27番）"""

    def test_exact_official_scores_high(self):
        score = server.compute_data_quality_score(evaluation_quality="EXACT", confidence_level="OFFICIAL_CONFIRMED")
        self.assertGreaterEqual(score, 80)

    def test_estimated_social_only_scores_low(self):
        score = server.compute_data_quality_score(evaluation_quality="ESTIMATED", confidence_level="SOCIAL_ONLY",
                                                     has_vwap=False, source_only_social=True)
        self.assertLess(score, 40)


class LowQualityExclusionTests(unittest.TestCase):
    """24. calibrationから低品質データ除外（指示書27・28番）"""

    def test_filters_below_threshold(self):
        samples = [{"data_quality_score": 80, "pnl_pct": 3}, {"data_quality_score": 40, "pnl_pct": -2}]
        filtered = server.filter_calibration_samples_by_quality(samples)
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["data_quality_score"], 80)

    def test_none_quality_not_excluded_for_backward_compat(self):
        samples = [{"data_quality_score": None, "pnl_pct": 3}]
        filtered = server.filter_calibration_samples_by_quality(samples)
        self.assertEqual(len(filtered), 1)


class ConfigVersionTests(unittest.TestCase):
    """25. config version（指示書25番）"""

    def test_config_version_constant_used_in_snapshot(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.create_entry_candidate_snapshot.side_effect = lambda db, uid, fields: fields
            saved = server.capture_entry_candidate_snapshot("postgres://x", "local", "7203", "ENTRY", price=1000)
        self.assertEqual(saved["config_version"], "DS_V1")


class IndependentSampleCountTests(unittest.TestCase):
    """26. independent sample count（指示書29番）"""

    def test_five_snapshots_same_day_count_as_one_independent(self):
        snapshots = [{"code": "7203", "candidate_type": "WAIT", "candidate_at": "2026-09-11T09:10:00+00:00"}] * 5
        result = server.compute_sample_integrity(snapshots)
        self.assertEqual(result["raw_snapshot_count"], 5)
        self.assertEqual(result["independent_candidate_count"], 1)


class CoverageRateTests(unittest.TestCase):
    """27. coverage rate（指示書30・31番）"""

    def test_coverage_rate_computed(self):
        self.assertEqual(server.compute_candidate_coverage_rate(100, 82), 0.82)

    def test_zero_eligible_returns_none(self):
        self.assertIsNone(server.compute_candidate_coverage_rate(0, 0))


class SchedulerDuplicatePreventionTests(unittest.TestCase):
    """28. scheduler duplicate prevention（指示書10・28・43番）：outcome_status確定済みは
    list_due側で除外される前提（scheduler本体は評価済みsnapshotを再取得しない）。"""

    def test_only_due_and_unevaluated_snapshots_processed(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_due_entry_candidate_snapshots_for_backfill.return_value = []
            result = server.run_due_candidate_outcomes("postgres://x", "local")
        self.assertEqual(result, {"evaluated": 0, "no_data": 0})
        mock_db.list_due_entry_candidate_snapshots_for_backfill.assert_called_once()


class BackfillDryRunTests(unittest.TestCase):
    """29. backfill dry_run（指示書32番）"""

    def test_dry_run_does_not_call_run_due(self):
        with mock.patch.object(server, "investment_db") as mock_db, \
             mock.patch.object(server, "run_due_candidate_outcomes") as mock_run:
            mock_db.list_due_entry_candidate_snapshots_for_backfill.return_value = [{"id": 1}, {"id": 2}]
            result = server.backfill_candidate_outcomes("postgres://x", "local", limit=10, dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["target_count"], 2)
        mock_run.assert_not_called()

    def test_non_dry_run_calls_run_due(self):
        with mock.patch.object(server, "investment_db") as mock_db, \
             mock.patch.object(server, "run_due_candidate_outcomes") as mock_run:
            mock_db.list_due_entry_candidate_snapshots_for_backfill.return_value = [{"id": 1}]
            mock_run.return_value = {"evaluated": 1, "no_data": 0}
            result = server.backfill_candidate_outcomes("postgres://x", "local", limit=10, dry_run=False)
        self.assertFalse(result["dry_run"])
        mock_run.assert_called_once()


class TradeDetailPayloadTests(unittest.TestCase):
    """30. trade detail payload（指示書36・37番）：候補クリック時の詳細に必要な項目が
    candidate snapshotに揃っていること"""

    def test_candidate_snapshot_has_detail_fields(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.create_entry_candidate_snapshot.side_effect = lambda db, uid, fields: {
                **fields, "id": 1, "candidate_at": "2026-09-11T09:10:00+00:00"}
            saved = server.capture_entry_candidate_snapshot(
                "postgres://x", "local", "7203", "WAIT", price=1000, entry_score=60,
                event_support="CAUTION", wait_reason="EXTENDED")
        for key in ("code", "entry_score", "event_support", "price_at_candidate", "wait_reason", "candidate_at"):
            self.assertIn(key, saved)


class Phase10CompatibilityTests(unittest.TestCase):
    """31. Phase10互換性：既存関数が無変更で動くこと"""

    def test_classify_wait_outcome_unchanged(self):
        self.assertEqual(server.classify_wait_outcome(-3.0), "CORRECT_WAIT")

    def test_evaluate_trade_outcome_functions_still_present(self):
        self.assertTrue(callable(server.evaluate_trade_outcome))
        self.assertTrue(callable(server.classify_decision_quality))

    def test_decision_support_weights_unchanged(self):
        self.assertIn("material_quality", server.DECISION_SUPPORT_WEIGHTS)


if __name__ == "__main__":
    unittest.main()
