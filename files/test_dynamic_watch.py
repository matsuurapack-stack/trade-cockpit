# dynamic_watchlist（Phase D）のテスト。 cd files && python -m unittest test_dynamic_watch -v

import datetime
import unittest

import dynamic_watch as dw

T0 = datetime.datetime(2026, 9, 26, 10, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=9)))


def c(code, movement=50, state="ACTIVE", pre=False, recent=50, manual=False, rank=None, above_vwap=True, mstate=None):
    return {"code": code, "name": code, "movement": movement, "recent_activity": recent, "activity_state": state,
            "pre_breakout": pre, "is_manual": manual, "rank": rank if rank is not None else movement,
            "above_vwap": above_vwap, "momentum_state": mstate}


def at(minutes):
    return T0 + datetime.timedelta(minutes=minutes)


class AddTests(unittest.TestCase):
    def test_auto_add_on_score_expanding_pre_breakout_and_hot(self):
        r = dw.update_dynamic_watch({}, [c("A", 66), c("B", 40, "EXPANDING"), c("C", 40, pre=True), c("D", 80, recent=70),
                                         c("E", 60), c("F", 30, "LOW_ACTIVITY")], T0)
        got = {a["code"]: a["source"] for a in r["adds"]}
        self.assertEqual(set(got), {"A", "B", "C", "D"})
        self.assertEqual(got["B"], "EXPANDING")
        self.assertEqual(got["C"], "PRE_BREAKOUT")
        self.assertEqual(set(r["hot"]), {"B", "C", "D"})

    def test_hot_pool_and_active_caps(self):
        cands = [c(f"H{i}", 90, "EXPANDING", rank=100 - i) for i in range(30)]
        r = dw.update_dynamic_watch({}, cands, T0, max_active=25, max_hot=10)
        self.assertLessEqual(len(r["state"]), 25)
        self.assertEqual(len(r["hot"]), 10)
        self.assertEqual(r["hot"][0], "H0")                     # rank順
        self.assertTrue(all(x["reason"] == "CAP" for x in r["removes"]))


class RemoveTests(unittest.TestCase):
    def setup_active(self, manual=False):
        r = dw.update_dynamic_watch({}, [c("A", 70, manual=manual)], T0)
        return r["state"]

    def test_weak_state_needs_grace_period_before_removal(self):
        st = self.setup_active()
        r1 = dw.update_dynamic_watch(st, [c("A", 30, "LOW_ACTIVITY")], at(5))
        self.assertEqual(r1["removes"], [])                      # 一時的な休みでは外さない
        r2 = dw.update_dynamic_watch(r1["state"], [c("A", 30, "LOW_ACTIVITY")], at(26))
        self.assertEqual(r2["removes"], [{"code": "A", "reason": "LOW_ACTIVITY"}])
        self.assertNotIn("A", r2["state"])

    def test_recovery_resets_weak_timer(self):
        st = self.setup_active()
        r1 = dw.update_dynamic_watch(st, [c("A", 30, "FADING")], at(5))
        r2 = dw.update_dynamic_watch(r1["state"], [c("A", 70)], at(15))          # 回復
        self.assertIsNone(r2["state"]["A"]["weak_since"])
        r3 = dw.update_dynamic_watch(r2["state"], [c("A", 30, "FADING")], at(30))
        self.assertEqual(r3["removes"], [])                      # タイマーは回復で再スタート

    def test_removal_reasons(self):
        for cand, reason in ((c("A", 30, "FADING"), "FADING"), (c("A", 30, mstate="MOMENTUM_DECAY"), "MOMENTUM_DECAY"),
                             (c("A", 30, above_vwap=False), "BELOW_VWAP_NO_RECOVERY")):
            st = self.setup_active()
            r1 = dw.update_dynamic_watch(st, [cand], at(1))
            r2 = dw.update_dynamic_watch(r1["state"], [cand], at(25))
            self.assertEqual(r2["removes"][0]["reason"], reason)

    def test_manual_codes_can_leave_the_overlay_but_the_overlay_is_capped_including_manual(self):
        # 手動銘柄もオーバーレイ（dynamic_watchlist）上では上限・削除の対象。手動のwatchlistテーブルは別管理で無変更。
        st = self.setup_active(manual=True)
        r1 = dw.update_dynamic_watch(st, [c("A", 20, "LOW_ACTIVITY", manual=True)], at(1))
        r2 = dw.update_dynamic_watch(r1["state"], [c("A", 20, "LOW_ACTIVITY", manual=True)], at(60))
        self.assertEqual(r2["removes"], [{"code": "A", "reason": "LOW_ACTIVITY"}])
        cands = [c(f"M{i}", 90, "EXPANDING", manual=True, rank=i) for i in range(120)]
        r = dw.update_dynamic_watch({}, cands, T0)
        self.assertLessEqual(len(r["state"]), dw.MAX_ACTIVE)
        self.assertLessEqual(len(r["hot"]), dw.MAX_HOT)
        self.assertIn("M119", r["state"])            # rankの高い順に残る
        self.assertNotIn("M0", r["state"])

    def test_unseen_codes_keep_state(self):
        st = self.setup_active()
        r = dw.update_dynamic_watch(st, [], at(60))
        self.assertIn("A", r["state"])
        self.assertEqual(r["removes"], [])


if __name__ == "__main__":
    unittest.main()
