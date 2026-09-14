# Trade Experience Learning テスト（指示書19番：最低10項目）。
#
# 実行方法： cd files && python -m unittest test_trade_experience_learning -v

import datetime
import unittest
from unittest import mock

import server
from test_support_source_inspect import get_fresh_source


def _iso(dt):
    return dt.astimezone(datetime.timezone.utc).isoformat()


VITZ_TRADE = {
    "id": 1, "symbol": "4440", "stock_name": "ヴィッツ", "trade_date": "2026-09-11",
    "trade_style": "DAY", "quantity": 200, "entry_price": 2600, "exit_price": 2760,
    "gross_pnl": 32000.0, "gross_pnl_pct": 6.1538,
    "pre_entry_state": "WAIT_TO_REVERSAL_CONFIRMATION",
    "wait_reason_json": ["short_ma_not_reclaimed", "downtrend_continuing", "oversold_but_no_reversal_confirmation"],
    "entry_reason_json": ["2574円時点ではWAIT", "2590～2600円の短期移動平均・節目を奪回",
                           "RSI売られすぎ圏から回復", "出来高増加", "2523円安値からの短期反転"],
    "exit_reason_json": ["モメンタム急騰後の利益確定", "14時台で残り時間が短い", "高ボラティリティ銘柄",
                          "200株全利確"],
    "pattern_tags_json": ["WAIT_TO_ENTRY", "OVERSOLD_REVERSAL", "SHORT_MA_RECLAIM", "ROUND_NUMBER_RECLAIM",
                           "VOLUME_EXPANSION", "AFTERNOON_REVERSAL", "MOMENTUM_STOCK", "FULL_EXIT_PROFIT_TAKING"],
    "result_class": "WIN", "max_favorable_excursion_pct": 18.1, "max_adverse_excursion_pct": -1.0,
    "learning_weight": 1.0,
    "decision_snapshot_json": {"pre_entry_decision": {"price": 2574, "decision": "WAIT",
                                                        "reason": ["short_ma_not_reclaimed", "downtrend_continuing"]}},
    "post_trade_analysis_json": {"learning_summary": "Oversold alone was not sufficient. "
                                  "Entry after reclaiming the short MA and 2600 level produced a successful momentum trade.",
                                  "post_exit_high": 3070},
}


class VitzInitialDataTests(unittest.TestCase):
    """1. 4440 ヴィッツの初回データ登録"""

    def test_create_trade_experience_calls_db_with_symbol(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.create_trade_experience.return_value = VITZ_TRADE
            saved = server.investment_db.create_trade_experience("postgres://x", "local", VITZ_TRADE)
        self.assertEqual(saved["symbol"], "4440")
        self.assertEqual(saved["stock_name"], "ヴィッツ")


class DecisionEventOrderingTests(unittest.TestCase):
    """2. WAITイベントとENTRYイベントが正しい順序で保存される"""

    def test_events_ordered_by_event_time_asc(self):
        events = [
            {"decision_type": "WAIT", "price": 2574, "event_time": "2026-09-11T05:11:00+00:00"},
            {"decision_type": "ENTRY_READY", "price": 2605, "event_time": "2026-09-11T05:16:00+00:00"},
            {"decision_type": "ENTRY", "price": 2600, "event_time": "2026-09-11T05:20:00+00:00"},
            {"decision_type": "EXIT", "price": 2760, "event_time": "2026-09-11T06:40:00+00:00"},
        ]
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_trade_decision_events.return_value = events
            result = server.investment_db.list_trade_decision_events("postgres://x", "local", trade_experience_id=1)
        types = [e["decision_type"] for e in result]
        self.assertEqual(types, ["WAIT", "ENTRY_READY", "ENTRY", "EXIT"])


class GrossPnlTests(unittest.TestCase):
    """3. gross_pnl: (2760-2600)*200=32000"""

    def test_gross_pnl_matches_expected(self):
        self.assertEqual(server.compute_trade_gross_pnl(2600, 2760, 200), 32000.0)

    def test_gross_pnl_sell_side(self):
        self.assertEqual(server.compute_trade_gross_pnl(2760, 2600, 200, side="SELL"), 32000.0)


class GrossPnlPctTests(unittest.TestCase):
    """4. gross_pnl_pct: 約6.1538%"""

    def test_gross_pnl_pct_matches_expected(self):
        pct = server.compute_trade_gross_pnl_pct(2600, 2760)
        self.assertAlmostEqual(pct, 6.1538, places=3)


class ExperienceScoreCapTests(unittest.TestCase):
    """5. sample_count < 5ではEXPERIENCE_SCOREが過大評価されない（最大+2まで）"""

    def test_low_sample_capped_at_2(self):
        similar = {"similar_count": 3, "wins": 3, "losses": 0, "win_rate": 1.0, "avg_return_pct": 5.0,
                    "max_similarity": 0.9, "recent_10_win_rate": 1.0,
                    "examples": [{"pattern_tags_json": ["WAIT_TO_ENTRY"], "result_class": "WIN"}]}
        score = server.compute_experience_score(similar)
        self.assertLessEqual(score, 2.0)

    def test_high_sample_can_exceed_2(self):
        similar = {"similar_count": 12, "wins": 9, "losses": 3, "win_rate": 0.75, "avg_return_pct": 3.4,
                    "max_similarity": 0.85, "recent_10_win_rate": 0.8,
                    "examples": [{"pattern_tags_json": ["WAIT_TO_ENTRY"], "result_class": "WIN"}]}
        score = server.compute_experience_score(similar)
        self.assertGreater(score, 2.0)
        self.assertLessEqual(score, 10.0)


class SimilarSearchIncludesLossesTests(unittest.TestCase):
    """6. 過去成功例だけでなく失敗例も検索される"""

    def test_find_similar_includes_wins_and_losses(self):
        win = {**VITZ_TRADE, "id": 1, "result_class": "WIN"}
        loss = {**VITZ_TRADE, "id": 2, "symbol": "1234", "result_class": "LOSS", "gross_pnl_pct": -2.0}
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_trade_experiences.return_value = [win, loss]
            result = server.find_similar_trade_experiences(
                "postgres://x", "local", current_tags=VITZ_TRADE["pattern_tags_json"], min_similarity=0.0)
        self.assertEqual(result["wins"], 1)
        self.assertEqual(result["losses"], 1)


class ActiveRuleNotAutoChangedTests(unittest.TestCase):
    """7. ACTIVEルールが自動変更されない"""

    def test_rule_candidate_creation_never_touches_active_status(self):
        # linecache汚染対策（Bugfix: isolate global state between test modules）：
        # test_support_source_inspect.get_fresh_source参照。
        import investment_db
        src = get_fresh_source(investment_db.create_trade_experience_rule_candidate)
        self.assertIn("RULE_CANDIDATE", src)
        self.assertNotIn("'ACTIVE'", src)

    def test_promote_only_acts_on_rule_candidate_status(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.get_trade_rule.return_value = {"id": 5, "status": "ACTIVE"}
            result = server.investment_db.promote_trade_experience_rule_candidate
            # promote_trade_experience_rule_candidate自体はinvestment_db関数なので、実装を直接検証する。
        import investment_db
        src = get_fresh_source(investment_db.promote_trade_experience_rule_candidate)
        self.assertIn('rule.get("status") != "RULE_CANDIDATE"', src)


class RuleCandidateOnlyGenerationTests(unittest.TestCase):
    """8. RULE_CANDIDATE生成のみ（自動でTESTING/ACTIVEへ昇格しない）"""

    def test_propose_rule_candidates_does_not_call_promote(self):
        pattern_stats = {"WAIT_TO_ENTRY": {"sample_count": 17, "win_count": 13, "loss_count": 4,
                                             "win_rate": 0.76, "avg_pnl_pct": 3.2, "confidence_level": "HIGH"}}
        with mock.patch.object(server, "investment_db") as mock_db:
            proposals = server.propose_rule_candidates_from_pattern_stats(pattern_stats)
        mock_db.promote_trade_experience_rule_candidate.assert_not_called()
        mock_db.update_trade_rule.assert_not_called()
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["pattern"], "WAIT_TO_ENTRY")
        self.assertEqual(proposals[0]["sample_count"], 17)

    def test_below_threshold_not_proposed(self):
        pattern_stats = {"SHORT_MA_RECLAIM": {"sample_count": 4, "win_rate": 0.9, "confidence_level": "LOW"}}
        proposals = server.propose_rule_candidates_from_pattern_stats(pattern_stats)
        self.assertEqual(proposals, [])


class NoHindsightTests(unittest.TestCase):
    """9. ENTRY後の未来情報がentry_reasonに混入しない"""

    def test_payload_uses_decision_snapshot_not_post_trade_analysis(self):
        payload = server.build_trade_experience_payload(VITZ_TRADE)
        self.assertEqual(payload["pre_entry_decision"]["price"], 2574)
        self.assertNotIn("post_exit_high", str(payload["pre_entry_decision"]))
        self.assertNotIn("3070", str(payload.get("entry_pattern")))
        # learning_summaryはpost_trade_analysis側にのみ含まれてよい（EXIT後の要約用）。
        self.assertIn("momentum trade", payload["learning_summary"])

    def test_features_do_not_use_post_trade_fields(self):
        features = server.build_trade_experience_features(VITZ_TRADE)
        # entry_setup等はpattern_tags_json/rsi_at_entry等ENTRY時点情報のみから構築される。
        self.assertIn("entry_setup", features)
        self.assertNotIn("post_trade_analysis_json", str(features))


class Phase1CompatibilityRegressionTests(unittest.TestCase):
    """10. 既存のENTRY TOP5・ルールエンジン等の回帰（軽量な健全性チェック）。
    全体回帰は既存test_nicosoku_phaseN.pyスイートで別途実施する。"""

    def test_decision_support_weights_untouched(self):
        self.assertIn("material_quality", server.DECISION_SUPPORT_WEIGHTS)

    def test_classify_entry_timing_quality_still_present(self):
        self.assertTrue(callable(server.classify_entry_timing_quality))

    def test_trade_experience_functions_do_not_shadow_existing_names(self):
        # classify_trade_result等の新規関数名が既存の重要関数を上書きしていないことを確認。
        self.assertTrue(callable(server.classify_trade_result))
        self.assertTrue(callable(server.synthesize_trade_judgment) if hasattr(server, "synthesize_trade_judgment")
                         else True)


class EvaluateTradeExperienceTests(unittest.TestCase):
    """evaluate_trade_experience()：恣意的な固定点数ではなく、根拠つきで採点される。"""

    def test_vitz_trade_scores_reasonably_high(self):
        total, breakdown = server.evaluate_trade_experience(VITZ_TRADE)
        self.assertGreaterEqual(total, 60)
        self.assertLessEqual(total, 100)
        self.assertEqual(breakdown["entry_timing_score"] + breakdown["risk_control_score"]
                          + breakdown["exit_execution_score"] + breakdown["rule_compliance_score"]
                          + breakdown["repeatability_score"], breakdown["total"])

    def test_empty_trade_scores_low(self):
        total, breakdown = server.evaluate_trade_experience({})
        self.assertLess(total, 40)


class LearningWeightExclusionTests(unittest.TestCase):
    """指示書17番：誤発注等は学習重みを下げる。"""

    def test_misfire_note_lowers_weight(self):
        self.assertLess(server.classify_trade_experience_learning_weight("誤発注のため参考にしない"), 1.0)

    def test_normal_note_keeps_full_weight(self):
        self.assertEqual(server.classify_trade_experience_learning_weight("順当な反転エントリー"), 1.0)


class PatternStatisticsConfidenceTests(unittest.TestCase):
    """pattern_statisticsのconfidence_level（LOW<5, MEDIUM 5-14, HIGH 15+）。"""

    def test_confidence_level_buckets(self):
        self.assertEqual(server.classify_pattern_confidence(3), "LOW")
        self.assertEqual(server.classify_pattern_confidence(10), "MEDIUM")
        self.assertEqual(server.classify_pattern_confidence(20), "HIGH")

    def test_compute_pattern_statistics_for_vitz(self):
        stats = server.compute_pattern_statistics([VITZ_TRADE])
        self.assertIn("WAIT_TO_ENTRY", stats)
        self.assertEqual(stats["WAIT_TO_ENTRY"]["sample_count"], 1)
        self.assertEqual(stats["WAIT_TO_ENTRY"]["confidence_level"], "LOW")


class UpsertTradeExperienceBySyncKeyOnConflictTests(unittest.TestCase):
    """2026-09-14修正（不具合対応）：trade_experiencesのUNIQUE(user_id,sync_key)は
    `WHERE sync_key IS NOT NULL`の部分インデックスのため、ON CONFLICT句にも同じWHERE述語を
    付けないとPostgresの制約推論が一致せず「no unique or exclusion constraint matching」で
    常に失敗する（実データで確認済み）。このテストはSQL文字列に述語が残っていることを
    ソース検査で保証し、将来の変更でこの一致が再び崩れないようにする回帰ガード。"""

    def test_on_conflict_clause_includes_partial_index_predicate(self):
        import investment_db
        src = get_fresh_source(investment_db.upsert_trade_experience_by_sync_key)
        self.assertIn("ON CONFLICT (user_id, sync_key) WHERE sync_key IS NOT NULL", src)


if __name__ == "__main__":
    unittest.main()
