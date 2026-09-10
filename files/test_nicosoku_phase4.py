# にこそくX連携 Phase4 テスト（指示書17番）。
#
# Phase2/3同様、実DBを必要としない形で20項目をカバーする。
#
# 実行方法： cd files && python -m unittest test_nicosoku_phase4 -v

import datetime
import unittest
from unittest import mock

import server


def _iso(dt):
    return dt.astimezone(datetime.timezone.utc).isoformat()


class BaselineSnapshotTests(unittest.TestCase):
    """1. baseline即時保存（指示書1番）"""

    def test_baseline_captured_at_signal_generation_time(self):
        as_of = datetime.datetime.now(datetime.timezone.utc)
        with mock.patch.object(server, "_fetch_raw_price_snapshot") as mock_fetch:
            mock_fetch.return_value = {"price": 53000.0, "captured_at": _iso(as_of),
                                        "source": "yfinance", "quality_hint": "INTRADAY"}
            baseline = server.capture_signal_baseline("dummy", "local", "MARKET", "NIKKEI225", as_of)
        self.assertEqual(baseline["baseline_value"], 53000.0)
        self.assertEqual(baseline["baseline_status"], "OK")
        self.assertEqual(baseline["baseline_source"], "yfinance")
        self.assertIsNotNone(baseline["baseline_at"])

    def test_baseline_no_data_status_on_fetch_failure(self):
        with mock.patch.object(server, "_fetch_raw_price_snapshot", return_value=None):
            baseline = server.capture_signal_baseline("dummy", "local", "MARKET", "NIKKEI225",
                                                        datetime.datetime.now(datetime.timezone.utc))
        self.assertIsNone(baseline["baseline_value"])
        self.assertEqual(baseline["baseline_status"], "NO_DATA")
        self.assertEqual(baseline["evaluation_quality"], "NO_DATA")


class ResultCaptureTests(unittest.TestCase):
    """2. result時点取得 / 3. change_pct計算（指示書3番）"""

    def test_result_captured_at_evaluation_time(self):
        evaluation = {"target_type": "MARKET", "target_key": "NIKKEI225", "baseline_value": 53000.0}
        with mock.patch.object(server, "_fetch_raw_price_snapshot") as mock_fetch:
            mock_fetch.return_value = {"price": 53530.0, "captured_at": _iso(datetime.datetime.now(datetime.timezone.utc)),
                                        "source": "yfinance", "quality_hint": "INTRADAY"}
            captured = server.capture_signal_result("dummy", "local", evaluation, datetime.datetime.now(datetime.timezone.utc))
        self.assertIsNotNone(captured)
        self.assertEqual(captured["result_value"], 53530.0)
        self.assertIn("result_at", captured)

    def test_change_pct_formula(self):
        # change_pct = (result - baseline) / baseline * 100（前日終値比%を使わない、指示書3番）
        evaluation = {"target_type": "STOCK", "target_key": "7203", "baseline_value": 2000.0}
        with mock.patch.object(server, "_fetch_raw_price_snapshot") as mock_fetch:
            mock_fetch.return_value = {"price": 2050.0, "captured_at": _iso(datetime.datetime.now(datetime.timezone.utc)),
                                        "source": "yfinance", "quality_hint": "INTRADAY"}
            captured = server.capture_signal_result("dummy", "local", evaluation, datetime.datetime.now(datetime.timezone.utc))
        self.assertEqual(captured["change_pct"], 2.5)  # (2050-2000)/2000*100


class EvaluationQualityTests(unittest.TestCase):
    """4〜7. EXACT/NEAR_EXACT/ESTIMATED/NO_DATA判定（指示書4番）"""

    def test_exact_within_2_minutes(self):
        as_of = datetime.datetime(2026, 9, 10, 3, 0, tzinfo=datetime.timezone.utc)
        captured_at = (as_of + datetime.timedelta(minutes=1, seconds=30)).isoformat()
        self.assertEqual(server._classify_evaluation_quality(captured_at, as_of, "INTRADAY"), "EXACT")

    def test_near_exact_within_5_minutes(self):
        as_of = datetime.datetime(2026, 9, 10, 3, 0, tzinfo=datetime.timezone.utc)
        captured_at = (as_of + datetime.timedelta(minutes=4)).isoformat()
        self.assertEqual(server._classify_evaluation_quality(captured_at, as_of, "INTRADAY"), "NEAR_EXACT")

    def test_estimated_when_daily_only(self):
        as_of = datetime.datetime(2026, 9, 10, 3, 0, tzinfo=datetime.timezone.utc)
        captured_at = as_of.isoformat()  # 時刻は一致していても分足でなければESTIMATED
        self.assertEqual(server._classify_evaluation_quality(captured_at, as_of, "DAILY"), "ESTIMATED")

    def test_estimated_when_far_from_as_of(self):
        as_of = datetime.datetime(2026, 9, 10, 3, 0, tzinfo=datetime.timezone.utc)
        captured_at = (as_of + datetime.timedelta(hours=3)).isoformat()
        self.assertEqual(server._classify_evaluation_quality(captured_at, as_of, "INTRADAY"), "ESTIMATED")

    def test_no_data_status_when_snapshot_missing(self):
        with mock.patch.object(server, "_fetch_raw_price_snapshot", return_value=None):
            baseline = server.capture_signal_baseline("dummy", "local", "MARKET", "NIKKEI225",
                                                        datetime.datetime.now(datetime.timezone.utc))
        self.assertEqual(baseline["evaluation_quality"], "NO_DATA")


class JapanCalendarTests(unittest.TestCase):
    """8〜9. 日本祝日NEXT_OPEN / NEXT_CLOSE（指示書5・6番）"""

    def test_fixed_holiday_is_not_trading_day(self):
        # 建国記念の日（2/11）は固定祝日（曜日に関わらず）
        self.assertFalse(server.is_jp_trading_day(datetime.date(2026, 2, 11)))

    def test_new_year_closure_is_not_trading_day(self):
        self.assertFalse(server.is_jp_trading_day(datetime.date(2026, 1, 2)))

    def test_next_open_skips_holiday(self):
        # 2/11が祝日の年、その前日に投稿されたとしてもNEXT_OPENは祝日を飛ばす
        posted = datetime.datetime(2026, 2, 10, 6, 0, tzinfo=datetime.timezone.utc)  # JST15:00
        due = server._social_eval_due_at(posted, "NEXT_OPEN")
        due_date_jst = due.astimezone(server._JST).date()
        self.assertTrue(server.is_jp_trading_day(due_date_jst))
        self.assertNotEqual(due_date_jst, datetime.date(2026, 2, 11))

    def test_next_close_skips_holiday(self):
        posted = datetime.datetime(2026, 2, 10, 6, 0, tzinfo=datetime.timezone.utc)
        due = server._social_eval_due_at(posted, "NEXT_CLOSE")
        due_date_jst = due.astimezone(server._JST).date()
        self.assertTrue(server.is_jp_trading_day(due_date_jst))
        jst = server._JST
        self.assertEqual((due.astimezone(jst).hour, due.astimezone(jst).minute), server.JP_MARKET_CLOSE_TIME)


class EventMatchTests(unittest.TestCase):
    """10〜13. event EXACT_MATCH / DATE_MATCH / PARTIAL_MATCH / NO_MATCH（指示書7番）"""

    def _candidate(self, name="CPI", event_type="ECONOMIC", date=datetime.date(2026, 9, 12)):
        jst = server._JST
        return {"event_name": name, "event_type": event_type,
                "event_start_at": datetime.datetime(date.year, date.month, date.day, 21, 30, tzinfo=jst).astimezone(datetime.timezone.utc)}

    def test_exact_match_same_date_and_type(self):
        existing = [{"event_date": "2026-09-12", "title": "米CPI発表", "event_type": "ECONOMIC"}]
        self.assertEqual(server.classify_event_match(self._candidate(), existing), "EXACT_MATCH")

    def test_date_match_only(self):
        existing = [{"event_date": "2026-09-12", "title": "決算発表ラッシュ", "event_type": "OTHER"}]
        self.assertEqual(server.classify_event_match(self._candidate(), existing), "DATE_MATCH")

    def test_partial_match_title_close_date_off(self):
        existing = [{"event_date": "2026-09-14", "title": "CPI速報値", "event_type": "OTHER"}]
        self.assertEqual(server.classify_event_match(self._candidate(), existing), "PARTIAL_MATCH")

    def test_no_match(self):
        existing = [{"event_date": "2026-10-30", "title": "FOMC会合", "event_type": "CENTRAL_BANK"}]
        self.assertEqual(server.classify_event_match(self._candidate(), existing), "NO_MATCH")


class EventTimelinessTests(unittest.TestCase):
    """14〜15. lead_time_minutes / event_timeliness分類（指示書8番）"""

    def test_lead_time_minutes_calculation(self):
        posted_at = datetime.datetime(2026, 9, 10, 0, 0, tzinfo=datetime.timezone.utc)
        candidates = server._extract_event_candidates_for_evaluation("9/12 CPI 21:30発表予定", posted_at)
        self.assertEqual(len(candidates), 1)
        lead_minutes = round((candidates[0]["event_start_at"] - posted_at).total_seconds() / 60)
        self.assertGreater(lead_minutes, 0)

    def test_timeliness_classification_buckets(self):
        self.assertEqual(server.classify_event_timeliness(60 * 30), "EARLY")     # 30h
        self.assertEqual(server.classify_event_timeliness(60 * 10), "GOOD")      # 10h
        self.assertEqual(server.classify_event_timeliness(60 * 2), "SHORT_NOTICE")  # 2h
        self.assertEqual(server.classify_event_timeliness(30), "LAST_MINUTE")    # 30min
        self.assertIsNone(server.classify_event_timeliness(None))


class PerformanceEventAggregationTests(unittest.TestCase):
    """16. performance event集計（指示書10番）"""

    def test_aggregate_event_performance(self):
        evals = [
            {"match_status": "EXACT_MATCH", "lead_time_minutes": 720},
            {"match_status": "EXACT_MATCH", "lead_time_minutes": 300},
            {"match_status": "NO_MATCH", "lead_time_minutes": 60},
        ]
        perf = server.aggregate_event_performance(evals)
        self.assertEqual(perf["total_events"], 3)
        self.assertAlmostEqual(perf["exact_match_rate"], 0.667, places=2)
        self.assertAlmostEqual(perf["avg_lead_time_minutes"], 360.0, places=1)

    def test_empty_event_performance_is_none_not_zero(self):
        perf = server.aggregate_event_performance([])
        self.assertEqual(perf["total_events"], 0)
        self.assertIsNone(perf["exact_match_rate"])


class SourceQualityScoreV2Tests(unittest.TestCase):
    """17. source_quality_score_v2（指示書11番）"""

    def test_v2_reflects_quality_and_event_accuracy(self):
        perf_high = {"total_evaluated": 30, "overall_confirmation_rate": 0.8, "avg_confirmation_score": 80,
                     "quality_breakdown": {"EXACT": 0.9, "NEAR_EXACT": 0.1},
                     "event_performance": {"exact_match_rate": 0.9}}
        perf_low = {"total_evaluated": 30, "overall_confirmation_rate": 0.8, "avg_confirmation_score": 80,
                    "quality_breakdown": {"ESTIMATED": 1.0},
                    "event_performance": {"exact_match_rate": 0.1}}
        v2_high = server.compute_source_quality_score_v2(perf_high, timeliness_rate=0.7, consistency=0.9)
        v2_low = server.compute_source_quality_score_v2(perf_low, timeliness_rate=0.7, consistency=0.9)
        self.assertGreater(v2_high["score"], v2_low["score"])
        self.assertFalse(v2_high["provisional"])

    def test_v1_score_unchanged_by_v2_addition(self):
        perf = {"total_evaluated": 30, "overall_confirmation_rate": 0.8, "avg_confirmation_score": 80}
        v1 = server.compute_source_quality_score(perf, timeliness_rate=0.7, consistency=0.9)
        self.assertIsNotNone(v1["score"])  # v1の意味は変えない（指示書11番）


class BackfillEstimatedTests(unittest.TestCase):
    """18. backfill ESTIMATED（指示書13番：過去データをEXACT扱いしない）"""

    def test_backfilled_old_post_is_estimated(self):
        old_posted_at = _iso(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=10))
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_watchlist.return_value = []
            mock_db.create_social_signal_evaluations.side_effect = lambda url, rows: (
                [self.assertEqual(r["evaluation_quality"], "ESTIMATED") for r in rows] and len(rows))
            with mock.patch.object(server, "_fetch_raw_price_snapshot") as mock_fetch:
                # captured_atは「いま」なので、10日前のposted_atとは大きく乖離する→ESTIMATED
                mock_fetch.return_value = {"price": 53000.0, "captured_at": _iso(datetime.datetime.now(datetime.timezone.utc)),
                                            "source": "yfinance", "quality_hint": "INTRADAY"}
                post = {"post_id": "old1", "posted_at": old_posted_at, "author_opinion_json": ["日経は強い"],
                        "image_analysis_json": [], "categories_json": ["MARKET_OVERVIEW"]}
                server.generate_social_signal_evaluations_for_post("dummy_url", "local", post)
        mock_db.create_social_signal_evaluations.assert_called_once()


class Phase3CompatibilityTests(unittest.TestCase):
    """19. 既存Phase3互換性"""

    def test_confirmation_engine_unchanged(self):
        # Phase3のcompute_signal_confirmationはPhase4でも変更していない
        confirmed, contradicted, score = server.compute_signal_confirmation("BULLISH", 0.8, "MARKET")
        self.assertTrue(confirmed)
        self.assertEqual(score, 100)

    def test_v1_performance_fields_still_present(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        evaluations = [{"evaluation_status": "EVALUATED", "confirmed": True, "confirmation_score": 80,
                        "evaluation_window": "30M", "signal_type": "SECTOR_ROTATION", "contradicted": False,
                        "evaluation_quality": "EXACT", "created_at": _iso(now)}]
        perf = server.aggregate_source_performance(evaluations, now=now)
        for key in ("total_evaluated", "overall_confirmation_rate", "avg_confirmation_score", "by_window", "by_category"):
            self.assertIn(key, perf)

    def test_old_rows_without_quality_default_to_estimated_in_breakdown(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        evaluations = [{"evaluation_status": "EVALUATED", "confirmed": True, "confirmation_score": 80,
                        "evaluation_window": "30M", "signal_type": "X", "contradicted": False,
                        "created_at": _iso(now)}]  # evaluation_quality列が無い（Phase3時代の行）
        perf = server.aggregate_source_performance(evaluations, now=now)
        self.assertEqual(perf["quality_breakdown"].get("ESTIMATED"), 1.0)


class ApiUiTests(unittest.TestCase):
    """20. API/UI"""

    def test_diagnostics_includes_phase4_fields(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.get_market_source.return_value = None
            mock_db.list_recent_social_posts.return_value = []
            mock_db.count_social_signal_evaluations.return_value = 0
            diag = server.nicosoku_diagnostics("dummy_url", "local")
        for key in ("snapshot_status", "market_calendar_status", "pending_evaluations", "no_data_evaluations"):
            self.assertIn(key, diag)

    def test_post_evaluation_summary_includes_change_and_quality(self):
        evals = [{"evaluation_window": "30M", "evaluation_status": "EVALUATED", "confirmed": True,
                  "contradicted": False, "change_value": 0.74, "evaluation_quality": "EXACT"}]
        summary = server._post_evaluation_summary(evals)
        self.assertEqual(summary[0]["mark"], "✅")
        self.assertEqual(summary[0]["change_label"], "+0.74%")
        self.assertEqual(summary[0]["quality"], "EXACT")

    def test_backfill_recompute_deletes_existing_then_regenerates(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_analyzed_social_posts.return_value = [
                {"post_id": "1", "author_opinion_json": ["半導体は弱い"], "image_analysis_json": [],
                 "categories_json": ["SECTOR_ROTATION"], "posted_at": _iso(datetime.datetime.now(datetime.timezone.utc))},
            ]
            mock_db.list_watchlist.return_value = []
            mock_db.create_social_signal_evaluations.return_value = 1
            baseline = {"baseline_value": 100.0, "baseline_at": _iso(datetime.datetime.now(datetime.timezone.utc)),
                        "baseline_source": "yfinance", "baseline_status": "OK", "baseline_detail_json": None,
                        "evaluation_quality": "ESTIMATED"}
            with mock.patch.object(server, "capture_signal_baseline", return_value=baseline):
                result = server.backfill_social_signal_evaluations("dummy_url", "local", limit=10, dry_run=False, recompute=True)
        mock_db.delete_social_signal_evaluations_for_post.assert_called_once_with("dummy_url", server.NICOSOKU_X_USERNAME, "1")
        mock_db.list_analyzed_social_posts.assert_called_once()  # recompute時は「除外なし」の一覧を使う
        mock_db.list_analyzed_social_posts_without_evaluations.assert_not_called()
        self.assertTrue(result["recompute"])

    def test_backfill_default_never_deletes(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_analyzed_social_posts_without_evaluations.return_value = []
            server.backfill_social_signal_evaluations("dummy_url", "local", limit=10, dry_run=False)
        mock_db.delete_social_signal_evaluations_for_post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
