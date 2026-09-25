# Rolling Radarエピソード（寿命・Radar後の遷移・発生時特徴量）の観測ロジックのテスト。観測専用で、判定は変えない。
#   cd files && python -m unittest test_radar_episode -v

import datetime
import inspect
import json
import unittest
from unittest import mock

import chart_signal_log as sl
import server
from test_movement_integration import ShadowRefreshTests as _Base, pool_cand

JST = datetime.timezone(datetime.timedelta(hours=9))
T0 = datetime.datetime(2026, 9, 26, 9, 40, tzinfo=JST)


def at(m):
    return T0 + datetime.timedelta(minutes=m)


def rec(state="NONE", chart="WATCH", pattern="BASE_BUILDING", act="ACTIVE", pre=False, mrec="NONE", score=70, code="Z",
        features=None, market_rs=1.0, spread=0.1, sector=5):
    c = pool_cand(code, movementScore=None, recentActivityScore=None, activityState=act, chartEntryState=chart, entryState=chart,
                  legacyEntryState="WATCH", chartPattern=pattern, preBreakout=pre, movementRecommendation=mrec, rollingState=state,
                  rollingScore=score, rollingFeatures=features or {"aboveVwap": True, "newHigh": True, "volSurge": 4.0, "rangeSurge": 3.0,
                                                                     "turnoverSurge": 5.0, "vwapDistPct": 0.4, "distFromHighPct": -0.1},
                  marketRS=market_rs, spreadPct=spread, scoreBreakdown={"autoSector": sector})
    c["chartContext"]["pattern"] = pattern
    return sl.build_signal_record("u", c, T0, "SCAN")


def step(mem, minutes, **kw):
    r = rec(**kw)
    new = sl.update_radar_episode(mem, ("u", "Z"), r, at(minutes))
    return r, new


class EpisodeTests(unittest.TestCase):
    def test_start_snapshot_age_and_no_episode_without_radar(self):
        mem = {}
        r, new = step(mem, 0, state="NONE")
        self.assertEqual(new, [])
        self.assertNotIn("radarEpisode", r["context"])
        r, new = step(mem, 1, state="SINGLE_BAR_SURGE")
        self.assertIn("radar_start", new)
        ep = r["context"]["radarEpisode"]
        self.assertEqual((ep["startState"], ep["ageMinutes"], ep["ended"]), ("SINGLE_BAR_SURGE", 0.0, False))
        snap = ep["snapshot"]                       # 発生時の特徴量（良い/悪いRadarの比較用）
        for k, v in {"aboveVwap": True, "newHigh": True, "marketRS": 1.0, "spreadPct": 0.1, "sector": 5, "volSurge": 4.0,
                     "rangeSurge": 3.0, "turnoverSurge": 5.0}.items():
            self.assertEqual(snap[k], v, k)
        r2, _ = step(mem, 8, state="ROLLING_EXPANDING")
        self.assertEqual(r2["context"]["radarEpisode"]["ageMinutes"], 7.0)   # radar_age_minutes

    def test_lifespan_end_when_radar_returns_to_none(self):
        mem = {}
        step(mem, 0, state="ROLLING_SURGE")
        r, new = step(mem, 12, state="NONE")
        self.assertIn("radar_end", new)
        ep = r["context"]["radarEpisode"]
        self.assertTrue(ep["ended"])
        self.assertEqual(ep["lifeMinutes"], 12.0)
        self.assertTrue(sl.is_loggable(dict(r, legacy_entry_state="WEAK", chart_entry_state="WEAK")))    # 終了行も必ず残す
        _, new2 = step(mem, 14, state="NONE")
        self.assertNotIn("radar_end", new2)                                                                # 二重に終了しない

    def test_new_radar_after_end_starts_a_new_episode(self):
        mem = {}
        step(mem, 0, state="ROLLING_SURGE")
        step(mem, 5, state="NONE")
        r, new = step(mem, 30, state="SINGLE_BAR_SURGE")
        self.assertIn("radar_start", new)
        self.assertEqual(r["context"]["radarEpisode"]["startedAt"], at(30).isoformat())

    def test_radar_to_wait_to_pullback_to_entry_sequence(self):
        mem = {}
        step(mem, 0, state="SINGLE_BAR_SURGE", chart="WAIT_PULLBACK", pattern="CHASE")            # 急騰中：待ち（CHASE）
        step(mem, 6, state="NONE", chart="WAIT_PULLBACK", pattern="BASE_BUILDING")
        step(mem, 12, state="NONE", chart="WAIT_PULLBACK", pattern="PULLBACK_READY")               # 押し目
        r, new = step(mem, 15, state="NONE", chart="ENTRY_READY", pattern="PULLBACK_READY", mrec="ENTRY_READY")
        self.assertIn("step:entry_ready_at", new)
        st = r["context"]["radarEpisode"]["steps"]
        self.assertEqual([st[k] for k in ("wait_at", "pullback_or_pre_at", "entry_ready_at")],
                         [at(0).isoformat(), at(12).isoformat(), at(15).isoformat()])
        self.assertEqual(st["chase_at"], at(0).isoformat())
        self.assertTrue(all(sl.is_loggable(dict(x, legacy_entry_state="WEAK", chart_entry_state="WEAK")) for x in (r,)))

    def test_steps_recorded_once_and_tracking_stops_after_window(self):
        mem = {}
        step(mem, 0, state="ROLLING_SURGE", pattern="CHASE")
        _, new = step(mem, 5, state="ROLLING_SURGE", pattern="CHASE")
        self.assertEqual(new, [])                                # 同じ出来事は1回だけ
        r, new = step(mem, 100, state="NONE", chart="ENTRY_READY", mrec="ENTRY_READY")     # 90分を超えたら別エピソード扱い（追跡しない）
        self.assertNotIn("radarEpisode", r["context"])


class SummaryTests(unittest.TestCase):
    def rows_for(self, mem_steps, code, start_min, state, ret30=None, mfe=None, snapshot=None, ended=False, life=None):
        """エピソード1件ぶんのログ行（開始行＋最終行）を作る。"""
        started = at(start_min).isoformat()
        base = rec(state=state, code=code)
        base.update({"code": code, "logged_at": at(start_min), "price_5m": 1000.0, "price_15m": 1000.0,
                     "price_30m": None if ret30 is None else 1000.0 * (1 + ret30 / 100),
                     "max_30m": None if mfe is None else 1000.0 * (1 + mfe / 100), "min_30m": 995.0, "current_price": 1000.0})
        base["context"] = dict(base["context"], radarEpisode={"startedAt": started, "startState": state, "ageMinutes": 0.0, "ended": False,
                                                              "lifeMinutes": None, "steps": {}, "snapshot": snapshot or base["context"].get("radarEpisode", {}).get("snapshot")})
        last = dict(base)
        steps = {k: at(start_min + v).isoformat() for k, v in mem_steps.items()}
        last["logged_at"] = at(start_min + 40)
        last["context"] = dict(base["context"], radarEpisode={"startedAt": started, "startState": state, "ageMinutes": 40.0,
                                                              "ended": ended, "lifeMinutes": life, "steps": steps, "snapshot": snapshot})
        return [base, last]

    def test_radar_to_entry_and_radar_to_chase_are_counted_separately(self):
        rows = []
        rows += self.rows_for({"expanding_at": 10, "chase_at": 10}, "A", 0, "SINGLE_BAR_SURGE", ret30=8.0, mfe=9.0)          # CHASEにしか繋がらない
        rows += self.rows_for({"wait_at": 0, "pullback_or_pre_at": 12, "entry_ready_at": 15, "expanding_at": 20}, "B", 5,
                              "ROLLING_SURGE", ret30=2.0, mfe=3.0)                                                          # 待てば買えた
        rows += self.rows_for({}, "C", 10, "ROLLING_EXPANDING", ret30=-1.0, mfe=0.3)
        s = sl.summarize_radar_episodes(rows)
        self.assertEqual(s["total"], 3)
        self.assertEqual(s["by_start_state"], {"SINGLE_BAR_SURGE": 1, "ROLLING_SURGE": 1, "ROLLING_EXPANDING": 1})
        self.assertEqual(s["hot_started"], 2)
        self.assertEqual((s["radar_to_chase_count"], s["radar_to_entry_ready_count"]), (1, 1))
        self.assertEqual(s["lead_to_entry_ready"], {"n": 1, "avg_minutes": 15.0})
        self.assertEqual(s["lead_to_expanding"], {"n": 2, "avg_minutes": 15.0})     # (10 + 20) / 2
        self.assertEqual(s["lead_to_chase"], {"n": 1, "avg_minutes": 10.0})
        self.assertEqual(s["waited_then_entry_count"], 1)
        self.assertEqual(s["waited_then_entry"][0]["code"], "B")

    def test_immediate_entry_is_not_counted_as_waited(self):
        rows = self.rows_for({"wait_at": 0, "pullback_or_pre_at": 0, "entry_ready_at": 0}, "A", 0, "ROLLING_SURGE", ret30=1.0, mfe=1.5)
        self.assertEqual(sl.summarize_radar_episodes(rows)["waited_then_entry_count"], 0)

    def test_lifespan_buckets_and_age_distribution(self):
        rows = []
        rows += self.rows_for({}, "A", 0, "ROLLING_SURGE", ended=True, life=4.0)
        rows += self.rows_for({}, "B", 0, "ROLLING_SURGE", ended=True, life=12.0)
        rows += self.rows_for({}, "C", 0, "ROLLING_SURGE", ended=True, life=25.0)
        rows += self.rows_for({}, "D", 0, "ROLLING_SURGE", ended=False)                # 40分経過しても継続
        rows += self.rows_for({}, "E", 0, "ROLLING_SURGE", ended=True, life=45.0)
        s = sl.summarize_radar_episodes(rows)
        self.assertEqual(s["lifespan"], {"expired_within_5m": 1, "expired_within_15m": 1, "expired_15_30m": 1, "persisted_30m_plus": 2})
        self.assertEqual(s["age_minutes"]["max"], 40.0)

    def test_snapshot_comparison_false_positive_vs_good(self):
        good_snap = {"aboveVwap": True, "newHigh": True, "marketRS": 2.0, "spreadPct": 0.1, "volSurge": 4.0, "rangeSurge": 3.0}
        bad_snap = {"aboveVwap": False, "newHigh": False, "marketRS": -1.0, "spreadPct": 0.6, "volSurge": 2.0, "rangeSurge": 2.0}
        rows = self.rows_for({}, "G", 0, "SINGLE_BAR_SURGE", ret30=8.0, mfe=9.0, snapshot=good_snap)
        rows += self.rows_for({}, "F", 5, "ROLLING_EXPANDING", ret30=-1.5, mfe=0.2, snapshot=bad_snap)
        cmp_ = sl.summarize_radar_episodes(rows)["snapshot_compare"]
        self.assertEqual(cmp_["good"]["rate_aboveVwap"], 1.0)
        self.assertEqual(cmp_["false_positive"]["rate_aboveVwap"], 0.0)
        self.assertEqual((cmp_["good"]["avg_marketRS"], cmp_["false_positive"]["avg_marketRS"]), (2.0, -1.0))
        self.assertEqual((cmp_["good"]["avg_spreadPct"], cmp_["false_positive"]["avg_spreadPct"]), (0.1, 0.6))
        self.assertEqual(cmp_["good"]["avg_volSurge"], 4.0)

    def test_summary_is_json_serializable_and_part_of_movement_summary(self):
        rows = self.rows_for({"chase_at": 3}, "A", 0, "SINGLE_BAR_SURGE", ret30=1.0, mfe=1.5)
        json.dumps(sl.summarize_movement(rows)["radar_episodes"], ensure_ascii=False, default=str)
        self.assertEqual(sl.summarize_radar_episodes([])["total"], 0)


class RefreshAgeTests(_Base):
    def test_radar_age_is_observed_but_does_not_change_order(self):
        def rc(code, score):
            return pool_cand(code, movementScore=None, rollingState="SINGLE_BAR_SURGE", rollingBaseState="SINGLE_BAR_SURGE",
                             rollingScore=score, rollingHot=True, rollingWatch=False, rollingReasons=[], rollingConfirm={"failed": []}, isManual=False)
        self.put_cache([rc("A", 60), rc("B", 90)])
        now = datetime.datetime.now(JST)
        server._RADAR_EPISODES[("u9", "A")] = {"start": now - datetime.timedelta(minutes=95), "ended": False}
        with mock.patch.object(server, "investment_db", None):
            shadow = server.refresh_shadow_movement("url", "u9")
        lst = shadow["rollingRadar"]
        self.assertEqual([d["code"] for d in lst], ["B", "A"])                     # 並びはスコア順のまま（古いRadarでも下げない）
        by = {d["code"]: d for d in lst}
        self.assertGreaterEqual(by["A"]["radarAgeMinutes"], 95)
        self.assertIsNone(by["B"]["radarAgeMinutes"])

    def test_episode_and_age_code_is_isolated_from_entry_logic(self):
        for obj in (server._select_entry_ready_top5, server.rescore_entry_candidate_with_quote):
            self.assertNotIn("radarEpisode", inspect.getsource(obj))
            self.assertNotIn("_RADAR_EPISODES", inspect.getsource(obj))


if __name__ == "__main__":
    unittest.main()
