# 「今日の振り返り」独立タブ化 + 15:30自動評価 + トレード経験/銘柄クセ学習（2026-09-12）の
# 回帰テスト。
#
# 実行方法： cd files && python -m unittest test_daily_review_learning -v

import unittest
from unittest import mock

import server


class ClassifyBehaviorTimeBucketTests(unittest.TestCase):
    """server.classify_behavior_time_bucket()：JST基準で6つの時間帯へ分類する。"""

    def test_morning_open(self):
        self.assertEqual(server.classify_behavior_time_bucket("2026-09-11T00:10:00+00:00"), "09:00-09:30")  # UTC00:10=JST09:10

    def test_afternoon_bucket(self):
        self.assertEqual(server.classify_behavior_time_bucket("2026-09-11T04:45:00+00:00"), "13:30-14:30")  # JST13:45

    def test_lunch_break_returns_none(self):
        self.assertIsNone(server.classify_behavior_time_bucket("2026-09-11T02:45:00+00:00"))  # JST11:45〜12:30の隙間

    def test_none_input(self):
        self.assertIsNone(server.classify_behavior_time_bucket(None))

    def test_unparseable_input(self):
        self.assertIsNone(server.classify_behavior_time_bucket("not-a-date"))


class ComputeTradeResultScoreTests(unittest.TestCase):
    """server.compute_trade_result_score()：結果だけを機械的に0-100へ写像する。"""

    def test_none_is_neutral(self):
        self.assertEqual(server.compute_trade_result_score(None), 50.0)

    def test_plus_ten_pct_is_max(self):
        self.assertEqual(server.compute_trade_result_score(10.0), 100.0)

    def test_minus_ten_pct_is_min(self):
        self.assertEqual(server.compute_trade_result_score(-10.0), 0.0)

    def test_zero_pct_is_midpoint(self):
        self.assertEqual(server.compute_trade_result_score(0.0), 50.0)

    def test_clamped_beyond_range(self):
        self.assertEqual(server.compute_trade_result_score(50.0), 100.0)
        self.assertEqual(server.compute_trade_result_score(-50.0), 0.0)


class EvaluateWaitDecisionTests(unittest.TestCase):
    """server.evaluate_wait_decision()：指示書13番「単純にBAD WAITにしない、当時の情報で
    妥当だったかを評価する」の中核ロジック。事後の値動きではなく判断時点のスコアだけで判定する。"""

    def test_low_score_is_good_wait(self):
        result_class, reason = server.evaluate_wait_decision(40)
        self.assertEqual(result_class, "GOOD_WAIT")

    def test_high_score_is_neutral_not_bad(self):
        # 「単純にBAD WAITにしない」——entry_score>=55でも即BADにはしない
        result_class, reason = server.evaluate_wait_decision(70)
        self.assertEqual(result_class, "NEUTRAL_WAIT")
        self.assertNotIn("BAD", result_class)

    def test_none_score_is_neutral_unevaluable(self):
        result_class, reason = server.evaluate_wait_decision(None)
        self.assertEqual(result_class, "NEUTRAL_WAIT")
        self.assertIn("評価不能", reason)

    def test_boundary_at_55(self):
        result_class, _ = server.evaluate_wait_decision(55)
        self.assertEqual(result_class, "NEUTRAL_WAIT")
        result_class, _ = server.evaluate_wait_decision(54.9)
        self.assertEqual(result_class, "GOOD_WAIT")


class ComputeBehaviorScoreTests(unittest.TestCase):
    """server.compute_behavior_score()：BEHAVIOR_SCORE 0-10。サンプル不足は過大評価しない
    （指示書19・20番）。"""

    def test_no_stats_returns_zero(self):
        self.assertEqual(server.compute_behavior_score(None), 0.0)
        self.assertEqual(server.compute_behavior_score({}), 0.0)

    def test_high_sample_high_win_rate(self):
        score = server.compute_behavior_score({"sample_count": 20, "win_rate": 0.8})
        self.assertEqual(score, 8.0)

    def test_low_sample_capped_at_2(self):
        # サンプル<5は勝率が高くても2.0を超えない（過大評価防止）
        score = server.compute_behavior_score({"sample_count": 1, "win_rate": 1.0})
        self.assertLessEqual(score, 2.0)

    def test_medium_sample_capped_at_7(self):
        score = server.compute_behavior_score({"sample_count": 10, "win_rate": 1.0})
        self.assertLessEqual(score, 7.0)

    def test_none_win_rate_is_zero(self):
        self.assertEqual(server.compute_behavior_score({"sample_count": 20, "win_rate": None}), 0.0)


def make_experience(symbol="4440", result_class="WIN", pattern_tags=None, entry_time=None,
                     mfe=None, mae=None, side="BUY"):
    return {
        "symbol": symbol, "result_class": result_class, "side": side,
        "pattern_tags_json": pattern_tags or [], "entry_time": entry_time,
        "max_favorable_excursion_pct": mfe, "max_adverse_excursion_pct": mae,
        "gross_pnl_pct": 5.0 if result_class == "WIN" else -3.0,
        "learning_weight": 1.0, "trade_date": "2026-09-11",
    }


class AggregateStockBehaviorStatsTests(unittest.TestCase):
    """server.aggregate_stock_behavior_stats()：統計値から作る（文章だけではない）。"""

    def test_empty_returns_zero_sample(self):
        stats = server.aggregate_stock_behavior_stats([])
        self.assertEqual(stats["sample_count"], 0)
        self.assertEqual(stats["confidence_level"], "LOW")

    def test_sample_count_and_confidence(self):
        exps = [make_experience() for _ in range(3)]
        stats = server.aggregate_stock_behavior_stats(exps)
        self.assertEqual(stats["sample_count"], 3)
        self.assertEqual(stats["confidence_level"], "LOW")  # <5

    def test_high_confidence_at_15_plus(self):
        exps = [make_experience() for _ in range(15)]
        stats = server.aggregate_stock_behavior_stats(exps)
        self.assertEqual(stats["confidence_level"], "HIGH")

    def test_preferred_setup_reuses_pattern_statistics(self):
        exps = [make_experience(pattern_tags=["OVERSOLD_REVERSAL"]) for _ in range(5)]
        stats = server.aggregate_stock_behavior_stats(exps)
        self.assertIn("OVERSOLD_REVERSAL", stats["preferred_setup_json"])
        self.assertEqual(stats["preferred_setup_json"]["OVERSOLD_REVERSAL"]["sample_count"], 5)

    def test_time_bucket_stats_computed(self):
        exps = [make_experience(entry_time="2026-09-11T00:10:00+00:00") for _ in range(6)]  # JST09:10
        stats = server.aggregate_stock_behavior_stats(exps)
        self.assertIn("09:00-09:30", stats["time_bucket_stats_json"])
        self.assertEqual(stats["time_bucket_stats_json"]["09:00-09:30"]["count"], 6)

    def test_danger_pattern_flagged_when_low_win_rate_and_enough_samples(self):
        exps = [make_experience(result_class="LOSS", pattern_tags=["FALLING_KNIFE"]) for _ in range(5)]
        stats = server.aggregate_stock_behavior_stats(exps)
        self.assertIn("FALLING_KNIFE", stats["danger_patterns_json"])

    def test_no_fabricated_values_when_no_mfe_mae(self):
        exps = [make_experience() for _ in range(3)]
        stats = server.aggregate_stock_behavior_stats(exps)
        self.assertIsNone(stats["avg_mfe_pct"])
        self.assertIsNone(stats["avg_mae_pct"])


class SyncTradeExperiencesForDateTests(unittest.TestCase):
    """server.sync_trade_experiences_for_date()：勝ち/負け/同値/GOOD_WAIT全部を登録する
    （勝ちトレードだけの登録は禁止、指示書12・26番）。冪等性（sync_key）も確認する。"""

    def test_win_and_loss_both_synced(self):
        history = [
            {"id": 1, "code": "4440", "name": "ヴィッツ", "closed_at": "2026-09-11T06:00:00+00:00",
             "entry_price": 2600, "exit_price": 2760, "shares": 200, "gross_pnl": 32000, "pnl": 32000},
            {"id": 2, "code": "9999", "name": "テスト損失銘柄", "closed_at": "2026-09-11T05:00:00+00:00",
             "entry_price": 1000, "exit_price": 950, "shares": 100, "gross_pnl": -5000, "pnl": -5000},
        ]
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_trade_history.return_value = history
            mock_db.list_trade_decision_events.return_value = []
            mock_db.upsert_trade_experience_by_sync_key.side_effect = lambda du, uid, key, fields: {"id": key, **fields}
            result = server.sync_trade_experiences_for_date("postgres://x", "user", "2026-09-11")
        self.assertEqual(result["trades_synced"], 2)
        calls = mock_db.upsert_trade_experience_by_sync_key.call_args_list
        synced_result_classes = [c.args[3]["result_class"] for c in calls]
        self.assertIn("WIN", synced_result_classes)
        self.assertIn("LOSS", synced_result_classes)

    def test_breakeven_also_synced(self):
        history = [{"id": 3, "code": "1111", "name": "同値", "closed_at": "2026-09-11T05:00:00+00:00",
                     "entry_price": 1000, "exit_price": 1000, "shares": 100, "gross_pnl": 0, "pnl": 0}]
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_trade_history.return_value = history
            mock_db.list_trade_decision_events.return_value = []
            mock_db.upsert_trade_experience_by_sync_key.side_effect = lambda du, uid, key, fields: {"id": 1, **fields}
            result = server.sync_trade_experiences_for_date("postgres://x", "user", "2026-09-11")
        self.assertEqual(result["trades_synced"], 1)

    def test_uses_sync_key_for_idempotency(self):
        history = [{"id": 42, "code": "4440", "name": "ヴィッツ", "closed_at": "2026-09-11T06:00:00+00:00",
                     "entry_price": 2600, "exit_price": 2760, "shares": 200, "gross_pnl": 32000, "pnl": 32000}]
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_trade_history.return_value = history
            mock_db.list_trade_decision_events.return_value = []
            mock_db.upsert_trade_experience_by_sync_key.return_value = {"id": 1}
            server.sync_trade_experiences_for_date("postgres://x", "user", "2026-09-11")
            call_args = mock_db.upsert_trade_experience_by_sync_key.call_args
        self.assertEqual(call_args.args[2], "trade:42")

    def test_wait_only_decision_synced_as_good_wait(self):
        events = [{"symbol": "5555", "decision_type": "WAIT", "event_time": "2026-09-11T05:00:00+00:00",
                    "reason_json": ["oversold"], "technical_snapshot_json": {"entry_score": 30}}]
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_trade_history.return_value = []
            mock_db.list_trade_decision_events.return_value = events
            mock_db.upsert_trade_experience_by_sync_key.return_value = {"id": 2}
            result = server.sync_trade_experiences_for_date("postgres://x", "user", "2026-09-11")
        self.assertEqual(result["waits_synced"], 1)
        call_args = mock_db.upsert_trade_experience_by_sync_key.call_args
        self.assertEqual(call_args.args[3]["result_class"], "GOOD_WAIT")

    def test_wait_followed_by_same_day_entry_not_double_synced_as_wait(self):
        events = [
            {"symbol": "6666", "decision_type": "WAIT", "event_time": "2026-09-11T05:00:00+00:00",
             "reason_json": [], "technical_snapshot_json": {"entry_score": 30}},
            {"symbol": "6666", "decision_type": "ENTRY", "event_time": "2026-09-11T05:30:00+00:00"},
        ]
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_trade_history.return_value = []
            mock_db.list_trade_decision_events.return_value = events
            result = server.sync_trade_experiences_for_date("postgres://x", "user", "2026-09-11")
        self.assertEqual(result["waits_synced"], 0)
        mock_db.upsert_trade_experience_by_sync_key.assert_not_called()

    def test_no_hindsight_no_future_price_used_in_wait_fields(self):
        # evaluate_wait_decisionの引数はentry_score_at_waitのみ——事後の値動きを一切受け取らない
        # 構造そのものが後知恵混入を防ぐ（指示書22番）。
        import inspect
        sig = inspect.signature(server.evaluate_wait_decision)
        self.assertEqual(list(sig.parameters.keys()), ["entry_score_at_wait"])


class VitzExistingDataCompatibilityTests(unittest.TestCase):
    """指示書23番：既存の4440ヴィッツサンプル（sample_count=1、confidence=LOW）との互換確認。"""

    def test_single_sample_stays_low_confidence(self):
        vitz = make_experience(symbol="4440", result_class="WIN",
                                 pattern_tags=["OVERSOLD_REVERSAL", "WAIT_TO_ENTRY", "AFTERNOON_MOMENTUM"])
        stats = server.aggregate_stock_behavior_stats([vitz])
        self.assertEqual(stats["sample_count"], 1)
        self.assertEqual(stats["confidence_level"], "LOW")

    def test_behavior_score_capped_for_single_sample(self):
        setup_stats = {"sample_count": 1, "win_rate": 1.0}
        self.assertLessEqual(server.compute_behavior_score(setup_stats), 2.0)


class RegressionExistingScoreUnaffectedTests(unittest.TestCase):
    """既存のENTRY SCORE・classify_pattern_confidence等、Trade Experience Learning（Task E）の
    関数群に一切手を加えていないことの確認（指示書冒頭「AIが勝手にACTIVEルールを変更しない」等
    の既存方針を壊していないかの間接確認）。"""

    def test_classify_pattern_confidence_unchanged(self):
        self.assertEqual(server.classify_pattern_confidence(4), "LOW")
        self.assertEqual(server.classify_pattern_confidence(5), "MEDIUM")
        self.assertEqual(server.classify_pattern_confidence(15), "HIGH")

    def test_compute_experience_score_signature_unchanged(self):
        import inspect
        sig = inspect.signature(server.compute_experience_score)
        self.assertEqual(list(sig.parameters.keys()), ["similar_result"])


if __name__ == "__main__":
    unittest.main()
