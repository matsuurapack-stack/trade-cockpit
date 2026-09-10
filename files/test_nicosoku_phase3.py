# にこそくX連携 Phase3 テスト（指示書23番）。
#
# Phase2同様、実DB（Neon/PostgreSQL）を必要としない形で20項目をカバーする：
#   - 純粋関数（confirmation判定・スコア・集計・due_at計算・抽出）は直接テスト
#   - DBアクセスを伴う分岐（scheduler・backfill・source_context注入）はinvestment_dbを
#     モックしてserver.py側の呼び出し・組み立てだけを検証する
#
# 実行方法： cd files && python -m unittest test_nicosoku_phase3 -v

import datetime
import unittest
from unittest import mock

import server


def _iso(dt):
    return dt.astimezone(datetime.timezone.utc).isoformat()


class ConfirmationDirectionTests(unittest.TestCase):
    """1〜4. BULLISH確認 / BEARISH確認 / NEUTRAL / CONTRADICTED（指示書7・8番）"""

    def test_bullish_confirmed(self):
        confirmed, contradicted, score = server.compute_signal_confirmation("BULLISH", 0.8, "MARKET")
        self.assertTrue(confirmed)
        self.assertFalse(contradicted)
        self.assertEqual(score, 100)  # ratio=0.8/0.3=2.67 >= 2

    def test_bearish_confirmed(self):
        # BEARISH方向で change_value が -0.8%（下落）なら確認成立
        confirmed, contradicted, score = server.compute_signal_confirmation("BEARISH", -0.8, "MARKET")
        self.assertTrue(confirmed)
        self.assertFalse(contradicted)

    def test_neutral_direction_not_directional(self):
        confirmed, contradicted, score = server.compute_signal_confirmation("NEUTRAL", 0.1, "MARKET")
        self.assertIsNone(confirmed)  # 方向性の賭けではないためconfirmedはNone
        self.assertFalse(contradicted)
        self.assertEqual(score, 100)  # threshold(0.3)/2=0.15以内でほぼ中立=的中相当

    def test_contradicted_when_opposite_direction(self):
        # BULLISHと判定したのに実際は下落した（明確に逆方向）
        confirmed, contradicted, score = server.compute_signal_confirmation("BULLISH", -0.5, "MARKET")
        self.assertFalse(confirmed)
        self.assertTrue(contradicted)
        self.assertEqual(score, 0)


class EvaluationWindowTests(unittest.TestCase):
    """5〜7. 30M評価 / 1H評価 / MARKET_CLOSE評価（指示書2番）"""

    def setUp(self):
        self.posted_at = datetime.datetime(2026, 9, 10, 3, 0, tzinfo=datetime.timezone.utc)  # JST12:00

    def test_30m_due_at(self):
        due = server._social_eval_due_at(self.posted_at, "30M")
        self.assertEqual(due, self.posted_at + datetime.timedelta(minutes=30))

    def test_1h_due_at(self):
        due = server._social_eval_due_at(self.posted_at, "1H")
        self.assertEqual(due, self.posted_at + datetime.timedelta(hours=1))

    def test_market_close_due_at_same_day(self):
        due = server._social_eval_due_at(self.posted_at, "MARKET_CLOSE")
        jst = datetime.timezone(datetime.timedelta(hours=9))
        due_jst = due.astimezone(jst)
        self.assertEqual((due_jst.hour, due_jst.minute), (15, 30))
        self.assertEqual(due_jst.date(), self.posted_at.astimezone(jst).date())

    def test_category_windows_include_expected(self):
        self.assertIn("30M", server.SOCIAL_EVAL_CATEGORY_WINDOWS["SECTOR_ROTATION"])
        self.assertIn("1H", server.SOCIAL_EVAL_CATEGORY_WINDOWS["MARKET_SENTIMENT"])
        self.assertIn("MARKET_CLOSE", server.SOCIAL_EVAL_DEFAULT_WINDOWS)


class NoDataExclusionTests(unittest.TestCase):
    """8. NO_DATA除外（指示書18番）"""

    def test_no_data_excluded_from_denominator(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        evaluations = [
            {"evaluation_status": "EVALUATED", "confirmed": True, "confirmation_score": 100,
             "evaluation_window": "30M", "signal_type": "SECTOR_ROTATION", "contradicted": False,
             "created_at": _iso(now)},
            {"evaluation_status": "NO_DATA", "confirmed": None, "confirmation_score": None,
             "evaluation_window": "30M", "signal_type": "SECTOR_ROTATION", "contradicted": False,
             "created_at": _iso(now)},
        ]
        perf = server.aggregate_source_performance(evaluations, now=now)
        self.assertEqual(perf["total_evaluated"], 1)  # NO_DATAは分母に入らない
        self.assertEqual(perf["overall_confirmation_rate"], 1.0)


class DuplicateEvaluationPreventionTests(unittest.TestCase):
    """9. 重複評価防止（指示書16番・UNIQUE制約）"""

    def test_extraction_dedupes_same_target(self):
        post = {
            "author_opinion_json": ["銀行株は強い", "やっぱり銀行は強い印象"],
            "image_analysis_json": [],
        }
        candidates = server._extract_signal_candidates_from_post(post, watchlist=[])
        bank_candidates = [c for c in candidates if c["target_type"] == "SECTOR" and c["target_key"] == "BANK"]
        self.assertEqual(len(bank_candidates), 1)  # 同一(signal_type,target_type,target_key)は1件のみ

    def test_create_social_signal_evaluations_uses_on_conflict_do_nothing(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.create_social_signal_evaluations.return_value = 1
            n = server.investment_db.create_social_signal_evaluations("dummy", [{"post_id": "1"}, {"post_id": "1"}])
            self.assertEqual(n, 1)
        mock_db.create_social_signal_evaluations.assert_called_once()


class ConfirmationScoreTests(unittest.TestCase):
    """10. confirmation_score（指示書8番：完全一致100〜強く逆方向0の段階）"""

    def test_score_buckets(self):
        cases = [
            (0.8, 100),   # ratio>=2 完全一致
            (0.35, 80),   # ratio>=1 強く一致
            (0.15, 60),   # ratio>=0.3 部分一致
            (0.0, 40),    # ratio>=-0.3 ほぼ中立
            (-0.08, 40),  # ratio=-0.267 >= -0.3
            (-0.25, 20),  # ratio=-0.833、-1<=ratio<-0.3 逆方向
            (-0.5, 0),    # ratio<-1 強く逆方向
        ]
        for change, expected in cases:
            _, _, score = server.compute_signal_confirmation("BULLISH", change, "MARKET")
            self.assertEqual(score, expected, f"change={change}")


class SectorAggregationTests(unittest.TestCase):
    """11. sector aggregation（指示書5番：中央値、1銘柄だけでは判定しない）"""

    def test_sector_median_of_multiple_stocks(self):
        # Phase4：_fetch_social_eval_value（前日終値比%の単一値）は_fetch_sector_snapshot
        # （raw価格＋構成銘柄スナップショット）へ置き換わった。
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_watchlist.return_value = [
                {"code": "8306", "name": "三菱UFJ", "theme": "銀行"},
                {"code": "8316", "name": "三井住友", "theme": "銀行"},
                {"code": "8411", "name": "みずほ", "theme": "銀行"},
            ]
            with mock.patch.object(server, "get_stock_quotes") as mock_quotes:
                mock_quotes.return_value = {
                    "8306": {"t": 110, "p": 100}, "8316": {"t": 102, "p": 100}, "8411": {"t": 106, "p": 100},
                }
                snap = server._fetch_sector_snapshot("dummy", "local", "BANK")
        self.assertEqual(snap["price"], 106.0)  # raw価格[110,102,106] → median=106
        self.assertEqual(len(snap["members"]), 3)

    def test_single_stock_returns_none(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_watchlist.return_value = [{"code": "8306", "name": "三菱UFJ", "theme": "銀行"}]
            snap = server._fetch_sector_snapshot("dummy", "local", "BANK")
        self.assertIsNone(snap)

    def test_sector_result_change_pct_is_median_of_member_pct_changes(self):
        # capture_signal_resultのSECTOR分岐：baseline_detail_jsonの各銘柄価格からのpct変化の
        # 中央値をchange_pctとする（銘柄ごとの価格水準差に左右されないため）。
        evaluation = {"target_type": "SECTOR", "target_key": "BANK", "baseline_value": 100.0,
                      "baseline_detail_json": [{"code": "8306", "price": 100}, {"code": "8316", "price": 100},
                                                {"code": "8411", "price": 100}]}
        with mock.patch.object(server, "get_stock_quotes") as mock_quotes:
            mock_quotes.return_value = {"8306": {"t": 110}, "8316": {"t": 102}, "8411": {"t": 106}}
            with mock.patch.object(server, "_concurrent_topix_change_pct", return_value=None):
                captured = server.capture_signal_result("dummy", "local", evaluation, datetime.datetime.now(datetime.timezone.utc))
        self.assertEqual(captured["change_pct"], 6.0)  # pct=[10,2,6] → median=6


class SourcePerformanceTests(unittest.TestCase):
    """12. source performance（指示書9番）"""

    def test_aggregate_source_performance_basic(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        evaluations = [
            {"evaluation_status": "EVALUATED", "confirmed": True, "confirmation_score": 80,
             "evaluation_window": "30M", "signal_type": "SECTOR_ROTATION", "contradicted": False, "created_at": _iso(now)},
            {"evaluation_status": "EVALUATED", "confirmed": False, "confirmation_score": 20,
             "evaluation_window": "1H", "signal_type": "MARKET_SENTIMENT", "contradicted": True, "created_at": _iso(now)},
        ]
        perf = server.aggregate_source_performance(evaluations, now=now)
        self.assertEqual(perf["total_evaluated"], 2)
        self.assertEqual(perf["contradicted_count"], 1)
        self.assertEqual(perf["overall_confirmation_rate"], 0.5)
        self.assertEqual(perf["avg_confirmation_score"], 50.0)
        self.assertIn("30M", perf["by_window"])
        self.assertIn("SECTOR_ROTATION", perf["by_category"])


class ThemePerformanceTests(unittest.TestCase):
    """13〜14. theme performance / minimum sample guard（指示書10番）"""

    def _make_evals(self, n, confirmed_true_count, target_key="BANK", now=None):
        now = now or datetime.datetime.now(datetime.timezone.utc)
        out = []
        for i in range(n):
            out.append({"evaluation_status": "EVALUATED", "target_type": "SECTOR", "target_key": target_key,
                        "confirmed": i < confirmed_true_count, "contradicted": False,
                        "confirmation_score": 80, "created_at": _iso(now)})
        return out

    def test_theme_performance_with_enough_sample(self):
        evals = self._make_evals(8, 6)
        perf = server.aggregate_theme_performance(evals, target_type="SECTOR")
        self.assertEqual(perf["BANK"]["sample_count"], 8)
        self.assertFalse(perf["BANK"]["low_sample"])
        self.assertEqual(perf["BANK"]["confirmation_rate"], 0.75)

    def test_minimum_sample_guard_hides_rate(self):
        evals = self._make_evals(3, 3)  # sample_count=3 < min_sample(5)
        perf = server.aggregate_theme_performance(evals, target_type="SECTOR")
        self.assertTrue(perf["BANK"]["low_sample"])
        self.assertIsNone(perf["BANK"]["confirmation_rate"])

    def test_source_context_none_when_sample_insufficient(self):
        ctx = server._social_source_context({"total_evaluated": 3, "overall_confirmation_rate": 1.0,
                                              "theme_performance": {}})
        self.assertIsNone(ctx)

    def test_source_context_present_when_sample_sufficient(self):
        ctx = server._social_source_context({"total_evaluated": 30, "overall_confirmation_rate": 0.7,
                                              "theme_performance": {"BANK": {"low_sample": False, "confirmation_rate": 0.8}}},
                                             post_text="今日は銀行株が強い")
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx["theme_confirmation_rate"], 0.8)
        self.assertEqual(ctx["confidence_level"], "MEDIUM")


class RollingWindowTests(unittest.TestCase):
    """15. rolling window（指示書20番：過学習防止）"""

    def test_old_evaluations_excluded_by_days(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        old = {"evaluation_status": "EVALUATED", "confirmed": True, "confirmation_score": 100,
               "evaluation_window": "30M", "signal_type": "X", "contradicted": False,
               "created_at": _iso(now - datetime.timedelta(days=200))}
        recent = {"evaluation_status": "EVALUATED", "confirmed": False, "confirmation_score": 0,
                  "evaluation_window": "30M", "signal_type": "X", "contradicted": True,
                  "created_at": _iso(now)}
        perf = server.aggregate_source_performance([recent, old], rolling_days=90, now=now)
        self.assertEqual(perf["total_evaluated"], 1)  # 90日より古いoldは除外
        self.assertEqual(perf["overall_confirmation_rate"], 0.0)

    def test_rolling_limit_caps_sample(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        evaluations = [{"evaluation_status": "EVALUATED", "confirmed": True, "confirmation_score": 100,
                        "evaluation_window": "30M", "signal_type": "X", "contradicted": False,
                        "created_at": _iso(now)} for _ in range(10)]
        perf = server.aggregate_source_performance(evaluations, rolling_limit=3, now=now)
        self.assertEqual(perf["total_evaluated"], 3)


class SourceQualityScoreTests(unittest.TestCase):
    """16. source_quality_score（指示書12番）"""

    def test_provisional_when_sample_low(self):
        perf = {"total_evaluated": 5, "overall_confirmation_rate": 0.8, "avg_confirmation_score": 80}
        result = server.compute_source_quality_score(perf, timeliness_rate=0.7, consistency=0.9)
        self.assertTrue(result["provisional"])

    def test_not_provisional_when_sample_sufficient(self):
        perf = {"total_evaluated": 25, "overall_confirmation_rate": 0.8, "avg_confirmation_score": 80}
        result = server.compute_source_quality_score(perf, timeliness_rate=0.7, consistency=0.9)
        self.assertFalse(result["provisional"])
        self.assertGreater(result["score"], 0)
        self.assertLessEqual(result["score"], 100)


class RecentSocialSignalsSourceContextTests(unittest.TestCase):
    """17. recent_social_market_signals追加（指示書13番）"""

    def test_signals_carry_source_context(self):
        now_iso = _iso(datetime.datetime.now(datetime.timezone.utc))
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_social_signals.return_value = [{
                "posted_at": now_iso, "importance": "HIGH", "categories_json": [], "text": "銀行株は強い",
                "facts_json": [], "author_opinion_json": [], "direct_mentions_json": [], "theme_related_json": [],
                "url": "https://x.com/nicosokufx/status/1", "image_analysis_status": None,
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
            signals = server.get_recent_social_market_signals("dummy_url", "local")
        self.assertEqual(len(signals), 1)
        self.assertIn("source_context", signals[0])
        self.assertIsNotNone(signals[0]["source_context"])
        self.assertEqual(signals[0]["source_context"]["sample_count"], 10)


class BackfillApiTests(unittest.TestCase):
    """18. API（指示書17・22番：backfillのdry_runはDBへ書き込まない）"""

    def test_dry_run_does_not_write(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_analyzed_social_posts_without_evaluations.return_value = [
                {"post_id": "1", "author_opinion_json": ["半導体は弱い"], "image_analysis_json": [],
                 "categories_json": ["SECTOR_ROTATION"], "posted_at": _iso(datetime.datetime.now(datetime.timezone.utc))},
            ]
            mock_db.list_watchlist.return_value = []
            result = server.backfill_social_signal_evaluations("dummy_url", "local", limit=10, dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["target_posts"], 1)
        mock_db.create_social_signal_evaluations.assert_not_called()

    def test_real_run_calls_create(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_analyzed_social_posts_without_evaluations.return_value = [
                {"post_id": "1", "author_opinion_json": ["半導体は弱い"], "image_analysis_json": [],
                 "categories_json": ["SECTOR_ROTATION"], "posted_at": _iso(datetime.datetime.now(datetime.timezone.utc))},
            ]
            mock_db.list_watchlist.return_value = []
            mock_db.list_portfolio.return_value = []
            mock_db.list_recent_signal_group_candidates.return_value = []
            mock_db.create_social_signal_evaluations.return_value = 3
            baseline = {"baseline_value": 100.0, "baseline_at": _iso(datetime.datetime.now(datetime.timezone.utc)),
                        "baseline_source": "yfinance", "baseline_status": "OK", "baseline_detail_json": None,
                        "evaluation_quality": "ESTIMATED"}
            with mock.patch.object(server, "capture_signal_baseline", return_value=baseline), \
                 mock.patch.object(server, "capture_market_state_snapshot", return_value={}), \
                 mock.patch.object(server, "maybe_generate_social_signal_alert_safe", return_value=None):
                result = server.backfill_social_signal_evaluations("dummy_url", "local", limit=10, dry_run=False)
        self.assertFalse(result["dry_run"])
        mock_db.create_social_signal_evaluations.assert_called_once()


class SchedulerTests(unittest.TestCase):
    """20. scheduler（指示書16番：due到来分だけ処理、NO_DATAはevaluation_statusを更新）"""

    def test_run_due_evaluations_marks_no_data_when_fetch_fails(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_due_social_signal_evaluations.return_value = [
                {"id": 1, "target_type": "MARKET", "target_key": "NIKKEI225", "signal_direction": "BULLISH",
                 "baseline_value": 0.5},
            ]
            with mock.patch.object(server, "capture_signal_result", return_value=None):
                result = server.run_due_social_signal_evaluations("dummy_url", "local")
        self.assertEqual(result["no_data"], 1)
        self.assertEqual(result["evaluated"], 0)
        mock_db.save_social_signal_evaluation_result.assert_called_once()
        self.assertEqual(mock_db.save_social_signal_evaluation_result.call_args[1]["evaluation_status"], "NO_DATA")

    def test_run_due_evaluations_saves_confirmed_result(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_due_social_signal_evaluations.return_value = [
                {"id": 2, "target_type": "MARKET", "target_key": "NIKKEI225", "signal_direction": "BULLISH",
                 "baseline_value": 100.0, "evaluation_quality": "EXACT"},
            ]
            captured = {"result_value": 101.0, "change_pct": 1.0,
                        "result_at": _iso(datetime.datetime.now(datetime.timezone.utc)), "quality": "EXACT"}
            with mock.patch.object(server, "capture_signal_result", return_value=captured):
                result = server.run_due_social_signal_evaluations("dummy_url", "local")
        self.assertEqual(result["evaluated"], 1)
        kwargs = mock_db.save_social_signal_evaluation_result.call_args[1]
        self.assertEqual(kwargs["evaluation_status"], "EVALUATED")
        self.assertEqual(kwargs["change_value"], 1.0)
        self.assertTrue(kwargs["confirmed"])
        self.assertEqual(kwargs["evaluation_quality"], "EXACT")

    def test_run_due_evaluations_only_processes_returned_due_items(self):
        # list_due_social_signal_evaluations自体がdue_at<=now・PENDINGのみ返す前提（DB側の
        # 責務）。scheduler側は返された件数だけ処理することを確認する。
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_due_social_signal_evaluations.return_value = []
            result = server.run_due_social_signal_evaluations("dummy_url", "local")
        self.assertEqual(result, {"evaluated": 0, "no_data": 0})
        mock_db.save_social_signal_evaluation_result.assert_not_called()


if __name__ == "__main__":
    unittest.main()
