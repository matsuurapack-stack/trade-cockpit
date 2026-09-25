# Early Momentum Radar（Phase D.1）のサーバー統合テスト：shadow（既存判定・TOP5に影響しない）、
# dynamic watchへの即追加、earlyRadar、ログ・マイルストーン・先行時間、逆指値品質。
#   cd files && python -m unittest test_radar_integration -v

import datetime
import json
import unittest
from unittest import mock

import chart_signal_log as sl
import dynamic_watch as dw
import early_radar as er
import movement_potential as mp
import server
from test_early_radar import QUIET3, build
from test_chart_context_integration import fields, strong_ctx
from test_movement_integration import ShadowRefreshTests as _Base, pool_cand

JST = datetime.timezone(datetime.timedelta(hours=9))
T0 = datetime.datetime(2026, 9, 26, 9, 10, tzinfo=JST)


def at(m):
    return T0 + datetime.timedelta(minutes=m)


def few_bars(n_surge=True):
    return build(QUIET3 + [(1.2, 1.2, 4000)] if n_surge else QUIET3)


class FieldsTests(unittest.TestCase):
    def test_radar_runs_with_few_bars_while_normal_logic_stays_unknown(self):
        b = few_bars()
        ctx = strong_ctx(b["closes"][-1])
        f = fields(ctx, b)
        self.assertEqual(f["radar"]["state"], "RADAR_SURGE")
        self.assertEqual(f["chart_context"]["confidence"], "LOW")             # 4本：通常のChart Contextは弱い信頼度のまま
        self.assertNotEqual(f["movement_recommendation"], "ENTRY_READY")      # Radarは買い判定ではない
        c = server._movement_candidate_fields(f)
        for k in ("radarState", "earlyMomentumScore", "radarRankScore", "radarConfidence", "radarHandoff", "radarReasons", "radarText",
                  "radarHot", "spreadPct", "atr5Pct"):
            self.assertIn(k, c)
        json.dumps(c, default=str)

    def test_radar_does_not_change_existing_or_movement_decisions(self):
        b = few_bars()
        ctx = strong_ctx(b["closes"][-1])
        base = fields(ctx, b)
        wild = {"state": "RADAR_SURGE", "early_momentum_score": 100, "rank_score": 100, "confidence": "MEDIUM", "handoff": False,
                "reasons": ["出来高加速"], "features": {}, "entry_allowed": False, "bars": 4}
        with mock.patch.object(er, "evaluate_radar", return_value=dict(wild, state="RADAR_NONE", early_momentum_score=0, rank_score=0)):
            other = fields(ctx, b)
        for k in ("entry_state", "entry_state_pre_chart", "entry_score", "entry_decision", "movement_recommendation"):
            self.assertEqual(base[k], other[k], k)
        self.assertEqual(base["movement"]["movement_potential_score"], other["movement"]["movement_potential_score"])

    def test_handoff_at_six_bars(self):
        b = build(QUIET3 * 2)
        f = fields(strong_ctx(b["closes"][-1]), b)
        self.assertTrue(f["radar"]["handoff"])
        self.assertFalse(early_hot(f))

    def test_spread_pct_is_computed_from_ask_bid(self):
        b = few_bars()
        ctx = strong_ctx(b["closes"][-1])
        ctx["row"].update({"ask": 1001.0, "bid": 999.0})
        f = fields(ctx, b)
        self.assertAlmostEqual(f["spread_pct"], 0.2, places=2)
        ctx["row"].update({"ask": None, "bid": None})
        self.assertIsNone(fields(ctx, b)["spread_pct"])


def early_hot(f):
    return er.radar_hot(f["radar"])


class DynamicWatchRadarTests(unittest.TestCase):
    def cand(self, code, **kw):
        c = {"code": code, "name": code, "movement": 5, "recent_activity": 10, "activity_state": "ACTIVE", "pre_breakout": False,
             "above_vwap": True, "momentum_state": None, "is_manual": True, "rank": 5, "radar_hot": False, "radar_state": None}
        c.update(kw)
        return c

    def test_radar_hot_is_added_to_hot_pool_even_with_low_movement(self):
        r = dw.update_dynamic_watch({}, [self.cand("R", radar_hot=True, radar_state="RADAR_SURGE", rank=80), self.cand("Q")], T0)
        self.assertEqual([a["code"] for a in r["adds"]], ["R"])
        self.assertEqual(r["adds"][0]["source"], "RADAR:RADAR_SURGE")
        self.assertIn("R", r["hot"])

    def test_radar_hot_stock_is_not_weak(self):
        r = dw.update_dynamic_watch({}, [self.cand("R", radar_hot=True, radar_state="RADAR_SURGE", activity_state="LOW_ACTIVITY")], T0)
        r2 = dw.update_dynamic_watch(r["state"], [self.cand("R", radar_hot=True, radar_state="RADAR_SURGE", activity_state="LOW_ACTIVITY")],
                                     T0 + datetime.timedelta(minutes=60))
        self.assertEqual(r2["removes"], [])


class RefreshTests(_Base):
    def test_early_radar_list_hot_add_and_existing_lists_untouched(self):
        def rc(code, state, score, rank, hot=True, handoff=False):
            return pool_cand(code, movementScore=None, recentActivityScore=None, activityState="UNKNOWN", radarState=state,
                             earlyMomentumScore=score, radarRankScore=rank, radarConfidence="LOW", radarHandoff=handoff, radarHot=hot,
                             radarText="出来高加速 / 値幅拡大", radarReasons=["出来高加速", "値幅拡大"], isManual=False)
        pool = [rc("S1", "RADAR_SURGE", 80, 56), rc("S2", "RADAR_PRE_BREAKOUT", 62, 60.0), rc("S3", "RADAR_ACTIVE", 45, 38, hot=False),
                rc("H1", "RADAR_NONE", 0, None, hot=False, handoff=True), pool_cand("N", movementScore=30)]
        existing = self.put_cache(pool)
        with mock.patch.object(server, "investment_db") as db, mock.patch.object(server, "WRITE_E2E_ALLOWED", True), \
                mock.patch.object(server, "_in_jp_session", return_value=True):
            db.load_dynamic_watch.return_value = {}
            shadow = server.refresh_shadow_movement("url", "u9")
        cur = server._ENTRY_TOP5_CACHE["u9"]
        for k, v in existing.items():
            self.assertEqual(cur[k], v, k)                                   # 既存TOP5は不変
        er_list = shadow["earlyRadar"]
        self.assertEqual([d["code"] for d in er_list], ["S2", "S1", "S3"])   # rank_score順。handoffとNONEは載らない
        self.assertTrue(all(d["entryAllowed"] is False and d["label"] == "🚨 初動監視" for d in er_list))
        self.assertEqual(er_list[0]["text"], "出来高加速 / 値幅拡大")
        self.assertLessEqual(len(er_list), 5)
        adds = {a["code"]: a["source"] for a in db.sync_dynamic_watch.call_args[0][2]}
        self.assertEqual(adds["S1"], "RADAR:RADAR_SURGE")                     # 通常Movementが未算出でもdynamic watchへ
        self.assertEqual(adds["S2"], "RADAR:RADAR_PRE_BREAKOUT")
        self.assertNotIn("S3", adds)
        self.assertIn("S1", shadow["dynamicWatch"]["hot"])
        called = {name for name, *_ in db.method_calls}
        self.assertTrue(called <= {"load_dynamic_watch", "sync_dynamic_watch", "list_portfolio"})   # 手動watchlistは触らない
        json.dumps(shadow, default=str)

    def test_top_list_limited_to_five(self):
        pool = [pool_cand(f"S{i}", movementScore=None, radarState="RADAR_SURGE", earlyMomentumScore=70, radarRankScore=50 + i,
                          radarHandoff=False, radarHot=True, radarConfidence="MEDIUM") for i in range(9)]
        self.assertEqual(len(er.build_early_radar_list(pool)), 5)
        self.assertEqual(er.build_early_radar_list(pool)[0]["code"], "S8")


class LogTests(unittest.TestCase):
    def rec(self, **kw):
        c = pool_cand("Z", movementScore=None, recentActivityScore=None, activityState="UNKNOWN", **kw)
        return sl.build_signal_record("u", c, T0, "SCAN")

    def test_radar_columns_and_loggable_even_when_state_is_weak(self):
        r = self.rec(radarState="RADAR_SURGE", earlyMomentumScore=81, spreadPct=0.12, atr5Pct=0.2, radarConfidence="LOW",
                     radarReasons=["出来高加速"], radarFeatures={"volAccel": 4.0})
        self.assertEqual((r["radar_state"], r["early_momentum_score"], r["spread_pct"], r["atr5_pct"]), ("RADAR_SURGE", 81, 0.12, 0.2))
        self.assertEqual(r["movement"]["radar"]["features"], {"volAccel": 4.0})
        self.assertTrue(sl.is_loggable(dict(r, legacy_entry_state="WEAK", chart_entry_state="WEAK")))
        quiet = self.rec(radarState="RADAR_NONE")
        self.assertFalse(sl.is_loggable(dict(quiet, legacy_entry_state="WEAK", chart_entry_state="WEAK")))

    def test_radar_milestones_and_state_change_forces_log(self):
        mem, key = {}, ("u", "Z")
        r1 = self.rec(radarState="RADAR_ACTIVE")
        self.assertEqual(sl.update_milestones(mem, key, r1, at(0)), [])
        r2 = self.rec(radarState="RADAR_SURGE", earlyMomentumScore=80)
        self.assertEqual(sl.update_milestones(mem, key, r2, at(5)), ["first_radar_at", "first_radar_surge_at"])
        self.assertTrue(sl.should_log(sl.last_state(r1, at(0)), r2, at(6)))
        r3 = self.rec(radarState="RADAR_EXPANDING")
        self.assertEqual(sl.update_milestones(mem, key, r3, at(10)), [])

    def test_radar_lead_time_vs_expanding_and_pre_breakout(self):
        def row(ms):
            return {"code": "627A", "logged_at": at(0), "current_price": 1000.0, "chart_entry_state": "WATCH", "legacy_entry_state": "WATCH",
                    "movement_recommendation": "NONE", "activity_state": "ACTIVE", "context": {"milestones": ms}}
        ms = {"first_radar_at": at(0).isoformat(), "first_expanding_at": at(15).isoformat(), "first_pre_breakout_at": at(20).isoformat(),
              "first_movement_entry_at": at(25).isoformat(), "first_chase_at": at(40).isoformat()}
        lead = sl.summarize_movement([row(ms)])["radar"]["lead_times"][0]
        self.assertEqual((lead["minutes_before_expanding"], lead["minutes_before_pre_breakout"]), (15.0, 20.0))
        self.assertEqual((lead["minutes_before_movement_entry"], lead["minutes_before_chase"]), (25.0, 40.0))
        self.assertIsNone(lead["minutes_before_early_breakout"])

    def test_stop_quality_hit_recovery_and_context_columns(self):
        def row(code, lo, p30, stop=990.0):
            return {"code": code, "logged_at": at(0), "current_price": 1000.0, "chart_entry_state": "ENTRY_READY", "legacy_entry_state": "WATCH",
                    "movement_recommendation": "ENTRY_READY", "activity_state": "EXPANDING", "recommended_stop": stop, "stop_distance_pct": 1.0,
                    "min_30m": lo, "max_30m": 1010.0, "price_30m": p30, "spread_pct": 0.1, "atr5_pct": 0.3, "context": {}, "movement": {}}
        rows = [row("A", 985.0, 1010.0), row("B", 985.0, 980.0), row("C", 995.0, 1005.0)]
        q = sl.summarize_movement(rows)["stop_quality"]
        self.assertEqual((q["n"], q["n_with_outcome"], q["stop_hit"]), (3, 3, 2))
        self.assertEqual(q["stop_hit_rate"], round(2 / 3, 3))
        self.assertEqual((q["recovered_to_entry_after_stop"], q["recovered_rate"]), (1, 0.5))
        self.assertEqual((q["avg_stop_distance_pct"], q["avg_atr5_pct"], q["avg_spread_pct"]), (1.0, 0.3, 0.1))
        self.assertIsNotNone(q["avg_mae_30m"])


class IsolationTests(unittest.TestCase):
    def test_entry_side_code_never_references_radar(self):
        import inspect
        for obj in (mp, server.rescore_entry_candidate_with_quote, server._select_entry_ready_top5):
            self.assertNotIn("early_radar", inspect.getsource(obj))
            self.assertNotIn("radarState", inspect.getsource(obj))
        src = inspect.getsource(server._compute_price_dependent_entry_fields)
        call = src.split("movement_potential.movement_recommendation(")[1].split(")")[0]
        self.assertEqual(call, "entry_state_pre_chart, entry_state, movement, chart")   # recommendationの入力にradarは入らない

    def test_daily_summary_is_json_serializable(self):
        json.dumps(sl.summarize_day([]), ensure_ascii=False, default=str)


if __name__ == "__main__":
    unittest.main()
