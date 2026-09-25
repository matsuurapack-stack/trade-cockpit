# Rolling Momentum Radar（Phase D.2）のサーバー統合テスト：shadow（既存判定・TOP5に影響しない）、
# dynamic watchのhot pool/通常watch、rollingRadar、ログ・マイルストーン・先行時間・false positive。
#   cd files && python -m unittest test_rolling_integration -v

import datetime
import inspect
import json
import unittest
from unittest import mock

import chart_signal_log as sl
import dynamic_watch as dw
import rolling_radar as rr
import server
from test_early_radar import build
from test_chart_context_integration import fields, strong_ctx
from test_movement_integration import ShadowRefreshTests as _Base, pool_cand

JST = datetime.timezone(datetime.timedelta(hours=9))
T0 = datetime.datetime(2026, 9, 24, 9, 40, tzinfo=JST)


def at(m):
    return T0 + datetime.timedelta(minutes=m)


QUIET8 = [(0.05, 0.30, 1000)] * 7
SURGE_SPEC = QUIET8 + [(1.2, 1.2, 4000)]        # 静かな7本 → 1本だけ急伸（627A 9/24型）


class FieldsTests(unittest.TestCase):
    def test_rolling_runs_after_handoff_and_early_radar_stays_handed_off(self):
        b = build(SURGE_SPEC)
        f = fields(strong_ctx(b["closes"][-1]), b)
        self.assertEqual(f["rolling"]["state"], "SINGLE_BAR_SURGE")
        self.assertTrue(f["radar"]["handoff"])                       # D.1は6本以上で引き継ぎ（従来どおり）
        self.assertNotEqual(f["movement_recommendation"], "ENTRY_READY")
        c = server._movement_candidate_fields(f)
        for k in ("rollingState", "rollingBaseState", "rollingScore", "rollingReasons", "rollingConfirm", "rollingHot", "rollingWatch"):
            self.assertIn(k, c)
        self.assertTrue(c["rollingHot"])
        json.dumps(c, default=str)

    def test_rolling_does_not_change_existing_or_movement_or_radar_decisions(self):
        b = build(SURGE_SPEC)
        ctx = strong_ctx(b["closes"][-1])
        base = fields(ctx, b)
        none = {"state": "NONE", "base_state": "NONE", "rolling_score": 0, "reasons": [], "features": None,
                "confirmations": {"known": 0, "passed": 0, "failed": [], "detail": {}}, "hot": False, "watch": False, "entry_allowed": False}
        with mock.patch.object(rr, "evaluate_rolling", return_value=none):
            other = fields(ctx, b)
        for k in ("entry_state", "entry_state_pre_chart", "entry_score", "entry_decision", "movement_recommendation"):
            self.assertEqual(base[k], other[k], k)
        self.assertEqual(base["movement"]["movement_potential_score"], other["movement"]["movement_potential_score"])
        self.assertEqual(base["radar"], other["radar"])

    def test_entry_side_code_never_references_rolling(self):
        for obj in (server.rescore_entry_candidate_with_quote, server._select_entry_ready_top5):
            self.assertNotIn("rolling", inspect.getsource(obj).lower())
        call = inspect.getsource(server._compute_price_dependent_entry_fields).split("movement_potential.movement_recommendation(")[1].split(")")[0]
        self.assertEqual(call, "entry_state_pre_chart, entry_state, movement, chart")


class RefreshTests(_Base):
    def rc(self, code, state, score, hot, watch, base=None):
        return pool_cand(code, movementScore=None, recentActivityScore=None, activityState="UNKNOWN", rollingState=state,
                         rollingBaseState=base or state, rollingScore=score, rollingHot=hot, rollingWatch=watch,
                         rollingReasons=["直近足の出来高が過去4本平均の4.0倍"], rollingConfirm={"known": 5, "passed": 3, "failed": ["vwap", "marketRS"]},
                         isManual=False)

    def test_hot_pool_watch_level_and_rolling_radar_list(self):
        pool = [self.rc("S1", "SINGLE_BAR_SURGE", 80, True, False), self.rc("P1", "ROLLING_PRE_BREAKOUT", 60, True, False),
                self.rc("W1", "RADAR_WEAK", 70, False, True, base="ROLLING_SURGE"), self.rc("E1", "ROLLING_EXPANDING", 50, False, True),
                pool_cand("N", movementScore=30)]
        existing = self.put_cache(pool)
        with mock.patch.object(server, "investment_db") as db, mock.patch.object(server, "WRITE_E2E_ALLOWED", True), \
                mock.patch.object(server, "_in_jp_session", return_value=True):
            db.load_dynamic_watch.return_value = {}
            shadow = server.refresh_shadow_movement("url", "u9")
        cur = server._ENTRY_TOP5_CACHE["u9"]
        for k, v in existing.items():
            self.assertEqual(cur[k], v, k)                                     # 既存TOP5は不変
        lst = shadow["rollingRadar"]
        self.assertEqual([d["code"] for d in lst], ["S1", "P1", "E1", "W1"])   # hot状態が先、次にwatch系（同格はスコア順）
        self.assertTrue(all(d["entryAllowed"] is False and d["label"] == "📡 警戒レーダー" for d in lst))
        adds = {a["code"]: a["source"] for a in db.sync_dynamic_watch.call_args[0][2]}
        self.assertEqual(adds["S1"], "ROLLING:SINGLE_BAR_SURGE")
        self.assertEqual(adds["P1"], "ROLLING:ROLLING_PRE_BREAKOUT")
        self.assertEqual(adds["W1"], "ROLLING:RADAR_WEAK")                      # 通常のdynamic watchまで
        hot = shadow["dynamicWatch"]["hot"]
        self.assertIn("S1", hot)
        self.assertIn("P1", hot)
        self.assertNotIn("W1", hot)                                             # RADAR_WEAKはhot poolに入れない
        self.assertNotIn("E1", hot)
        called = {name for name, *_ in db.method_calls}
        self.assertTrue(called <= {"load_dynamic_watch", "sync_dynamic_watch", "list_portfolio"})   # 手動watchlistは触らない
        json.dumps(shadow, default=str)


class DynamicWatchTests(unittest.TestCase):
    def cand(self, code, **kw):
        c = {"code": code, "name": code, "movement": 5, "recent_activity": 10, "activity_state": "ACTIVE", "pre_breakout": False,
             "above_vwap": True, "momentum_state": None, "is_manual": True, "rank": 5}
        c.update(kw)
        return c

    def test_rolling_watch_states_are_not_weak_and_not_hot(self):
        r = dw.update_dynamic_watch({}, [self.cand("W", rolling_watch=True, rolling_state="RADAR_WEAK", activity_state="LOW_ACTIVITY")], T0)
        self.assertEqual(r["adds"][0]["source"], "ROLLING:RADAR_WEAK")
        self.assertEqual(r["hot"], [])
        r2 = dw.update_dynamic_watch(r["state"], [self.cand("W", rolling_watch=True, rolling_state="RADAR_WEAK", activity_state="LOW_ACTIVITY")],
                                     at(60))
        self.assertEqual(r2["removes"], [])


class LogTests(unittest.TestCase):
    def rec(self, **kw):
        c = pool_cand("Z", movementScore=None, recentActivityScore=None, activityState="UNKNOWN", **kw)
        return sl.build_signal_record("u", c, T0, "SCAN")

    def test_columns_loggable_and_milestones(self):
        r = self.rec(rollingState="SINGLE_BAR_SURGE", rollingBaseState="SINGLE_BAR_SURGE", rollingScore=77,
                     rollingConfirm={"failed": []}, rollingReasons=["x"], rollingFeatures={"volSurge": 4.0})
        self.assertEqual((r["rolling_state"], r["rolling_score"]), ("SINGLE_BAR_SURGE", 77))
        self.assertEqual(r["movement"]["rolling"]["features"], {"volSurge": 4.0})
        self.assertTrue(sl.is_loggable(dict(r, legacy_entry_state="WEAK", chart_entry_state="WEAK")))
        mem, key = {}, ("u", "Z")
        self.assertEqual(sl.update_milestones(mem, key, r, at(0)), ["first_rolling_at", "first_single_bar_surge_at"])
        weak = self.rec(rollingState="RADAR_WEAK")
        self.assertEqual(sl.update_milestones({}, ("u", "W"), weak, at(0)), ["first_rolling_weak_at"])
        self.assertTrue(sl.should_log(sl.last_state(self.rec(rollingState="NONE"), at(0)), r, at(1)))
        self.assertFalse(sl.is_loggable(dict(self.rec(rollingState="NONE"), legacy_entry_state="WEAK", chart_entry_state="WEAK")))

    def row(self, ms=None, **kw):
        r = {"code": "627A", "logged_at": at(0), "current_price": 1000.0, "chart_entry_state": "WATCH", "legacy_entry_state": "WATCH",
             "movement_recommendation": "NONE", "activity_state": "ACTIVE", "context": {"milestones": ms or {}}, "movement": {},
             "chart_pattern": "BASE_BUILDING"}
        r.update(kw)
        return r

    def test_lead_time_rolling_0940_vs_expanding_0950(self):
        ms = {"first_rolling_at": at(0).isoformat(), "first_expanding_at": at(10).isoformat(), "first_pre_breakout_at": at(25).isoformat(),
              "first_chase_at": at(0).isoformat()}
        lead = sl.summarize_movement([self.row(ms)])["rolling"]["lead_times"][0]
        self.assertEqual(lead["minutes_before_expanding"], 10.0)
        self.assertEqual(lead["minutes_before_pre_breakout"], 25.0)
        self.assertEqual(lead["minutes_before_chase"], 0.0)
        self.assertIsNone(lead["minutes_before_movement_entry"])

    def test_false_positive_tracking_with_outcomes_mfe_mae(self):
        fp = self.row(rolling_state="ROLLING_EXPANDING", price_5m=1001.0, price_15m=1002.0, price_30m=1003.0, max_30m=1006.0, min_30m=996.0,
                      rolling_score=66, movement={"rolling": {"confirmations": {"failed": ["vwap"]}}})           # 9/25 13:25型：伸びなかった
        tp = self.row(code="X", rolling_state="SINGLE_BAR_SURGE", price_5m=1010.0, price_15m=1025.0, price_30m=1040.0, max_30m=1045.0, min_30m=1004.0)
        weak = self.row(code="Y", rolling_state="RADAR_WEAK", price_30m=990.0, max_30m=1002.0, min_30m=985.0)
        s = sl.summarize_movement([fp, tp, weak])["rolling"]
        self.assertEqual(s["states"]["ROLLING_EXPANDING"]["false_positive"], 1)
        self.assertEqual(s["states"]["ROLLING_EXPANDING"]["false_positive_rate"], 1.0)
        self.assertEqual(s["states"]["SINGLE_BAR_SURGE"]["false_positive"], 0)
        self.assertEqual(s["states"]["RADAR_WEAK"]["false_positive_rate"], 1.0)
        ev = {e["code"]: e for e in s["events"]}
        self.assertEqual((ev["627A"]["ret_5m"], ev["627A"]["ret_15m"], ev["627A"]["ret_30m"]), (0.1, 0.2, 0.3))
        self.assertEqual((ev["627A"]["mfe_30m"], ev["627A"]["mae_30m"]), (0.6, -0.4))
        self.assertTrue(ev["627A"]["false_positive"])
        self.assertEqual(ev["627A"]["confirmations_failed"], ["vwap"])
        self.assertFalse(ev["X"]["false_positive"])
        json.dumps(sl.summarize_day([fp, tp]), ensure_ascii=False, default=str)


if __name__ == "__main__":
    unittest.main()
