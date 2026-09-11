# TOP5表示ロジック修正指示（2026-09-12）の回帰テスト。
#
# 「今買い時TOP5」がENTRY READY銘柄が存在するのに0件表示になる不整合と、
# 「今日の注目TOP5」に前日比マイナス銘柄が通常枠で紛れ込む不整合、それぞれの修正を検証する。
#
# 実行方法： cd files && python -m unittest test_top5_selection_fix -v

import unittest
from unittest import mock

import server


def make_candidate(code, entry_state, entry_score, **extra):
    c = {"code": code, "name": f"銘柄{code}", "entryState": entry_state, "entryScore": entry_score}
    c.update(extra)
    return c


class SelectEntryReadyTop5Tests(unittest.TestCase):
    """server._select_entry_ready_top5()：候補があるだけ表示する（0〜5件、必ず5件ではない）。"""

    def test_one_entry_ready_candidate_shows_one(self):
        candidates = [make_candidate("7203", "ENTRY_READY", 82)]
        top5, watch, debug = server._select_entry_ready_top5(candidates)
        self.assertEqual(len(top5), 1)
        self.assertEqual(top5[0]["code"], "7203")
        self.assertEqual(debug["entry_ready"], 1)
        self.assertEqual(debug["final_candidates"], 1)
        self.assertEqual(debug["rendered"], 1)

    def test_three_entry_ready_candidates_shows_three(self):
        candidates = [make_candidate(str(i), "ENTRY_READY", 80 - i) for i in range(3)]
        top5, watch, debug = server._select_entry_ready_top5(candidates)
        self.assertEqual(len(top5), 3)
        self.assertEqual(debug["entry_ready"], 3)

    def test_seven_entry_ready_candidates_shows_top_five_only(self):
        # 呼び出し元でentry_score降順ソート済みという前提（本関数はソートしない）。
        candidates = [make_candidate(str(i), "NOW_BUYABLE", 90 - i) for i in range(7)]
        top5, watch, debug = server._select_entry_ready_top5(candidates)
        self.assertEqual(len(top5), 5)
        self.assertEqual([c["code"] for c in top5], ["0", "1", "2", "3", "4"])
        self.assertEqual(debug["entry_ready"], 7)
        self.assertEqual(debug["final_candidates"], 5)

    def test_zero_candidates_returns_empty_list_not_error(self):
        top5, watch, debug = server._select_entry_ready_top5([])
        self.assertEqual(top5, [])
        self.assertEqual(debug["scanned"], 0)
        self.assertEqual(debug["final_candidates"], 0)

    def test_risk_state_excluded_from_top5(self):
        candidates = [make_candidate("1", "CHASE_RISK", 90), make_candidate("2", "INVALID", 85)]
        top5, watch, debug = server._select_entry_ready_top5(candidates)
        self.assertEqual(top5, [])
        self.assertEqual(debug["risk_excluded"], 2)

    def test_weak_state_excluded_from_top5(self):
        candidates = [make_candidate("1", "WEAK", 20)]
        top5, watch, debug = server._select_entry_ready_top5(candidates)
        self.assertEqual(top5, [])
        self.assertEqual(debug["weak_excluded"], 1)

    def test_provisional_state_excluded_from_top5(self):
        candidates = [make_candidate("1", "PROVISIONAL", 70)]
        top5, watch, debug = server._select_entry_ready_top5(candidates)
        self.assertEqual(top5, [])
        self.assertEqual(debug["provisional_excluded"], 1)

    def test_existing_entry_score_not_modified(self):
        # 指示書9番「既存ENTRY SCORE計算自体は変更しない」。
        candidates = [make_candidate("7203", "ENTRY_READY", 82)]
        top5, watch, debug = server._select_entry_ready_top5(candidates)
        self.assertEqual(top5[0]["entryScore"], 82)

    def test_tier1_entry_ready_prioritized_over_tier2_active_break(self):
        # ENTRY READY最優先（候補優先順位1）。Tier2は高スコアでもTier1の後に並ぶ。
        candidates = [
            make_candidate("A", "WAIT_BREAKOUT", 95),  # Tier2（高スコア）
            make_candidate("B", "ENTRY_READY", 55),    # Tier1
        ]
        top5, watch, debug = server._select_entry_ready_top5(candidates)
        self.assertEqual([c["code"] for c in top5], ["B", "A"])
        self.assertEqual(debug["entry_ready"], 1)
        self.assertEqual(debug["active_break"], 1)

    def test_tier2_requires_min_score(self):
        candidates = [make_candidate("A", "WAIT_BREAKOUT", 59)]  # 閾値60未満
        top5, watch, debug = server._select_entry_ready_top5(candidates)
        self.assertEqual(top5, [])
        self.assertEqual(debug["active_break"], 0)

    def test_tier3_watch_near_ready_included_when_pool_thin(self):
        candidates = [make_candidate("A", "WATCH", 50)]  # 閾値45以上
        top5, watch, debug = server._select_entry_ready_top5(candidates)
        self.assertEqual(len(top5), 1)
        self.assertEqual(debug["watch_near_ready"], 1)

    def test_tier3_below_threshold_excluded(self):
        candidates = [make_candidate("A", "WATCH", 30)]  # 閾値45未満
        top5, watch, debug = server._select_entry_ready_top5(candidates)
        self.assertEqual(top5, [])
        self.assertEqual(debug["watch_near_ready"], 0)

    def test_full_priority_ordering_tier1_then_tier2_then_tier3(self):
        candidates = [
            make_candidate("C", "WATCH", 50),          # Tier3
            make_candidate("B", "WAIT_PULLBACK", 65),  # Tier2
            make_candidate("A", "ENTRY_READY", 60),    # Tier1
        ]
        top5, watch, debug = server._select_entry_ready_top5(candidates)
        self.assertEqual([c["code"] for c in top5], ["A", "B", "C"])

    def test_watch_candidates_excludes_top5_codes(self):
        candidates = [
            make_candidate("A", "ENTRY_READY", 80),
            make_candidate("B", "WATCH", 40),
        ]
        top5, watch, debug = server._select_entry_ready_top5(candidates)
        top5_codes = {c["code"] for c in top5}
        self.assertNotIn("B", top5_codes)  # WATCH<45はTOP5対象外だがwatch_candidatesには残る
        watch_codes = {c["code"] for c in watch}
        self.assertIn("B", watch_codes)


class ScoreEntryCandidatesDebugFieldTests(unittest.TestCase):
    """compute_entry_ready_candidates()のempty応答にもdebugキーが含まれること（フロント側の
    entryResult.debug?.xxx参照が常に安全であることを保証）。"""

    def test_empty_result_has_debug_key_when_watchlist_empty(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_watchlist.return_value = []
            result = server._score_entry_candidates("postgres://x", "someuser")
        self.assertIn("debug", result)
        self.assertEqual(result["debug"]["scanned"], 0)
        self.assertEqual(result["debug"]["final_candidates"], 0)


if __name__ == "__main__":
    unittest.main()
