# Phase D（Movement Potential）のサーバー統合テスト：shadow（既存TOP5・entry_stateを変えない）、
# 15〜30秒再評価との共通化、dynamic_watchlist、マイルストーン、ログ、shadow比較集計。
#   cd files && python -m unittest test_movement_integration -v

import datetime
import json
import unittest
from unittest import mock

import chart_signal_log as sl
import movement_potential as mp
import server
from test_chart_context import chase, pullback_ready
from test_chart_context_integration import fields, scaled, strong_ctx
from test_entry_top5_rescore import _shared, _quote

JST = datetime.timezone(datetime.timedelta(hours=9))
T0 = datetime.datetime(2026, 9, 26, 10, 0, tzinfo=JST)


def at(m):
    return T0 + datetime.timedelta(minutes=m)


class FieldsTests(unittest.TestCase):
    def test_fields_contain_movement_and_recommendation(self):
        b = scaled(pullback_ready(1016.0))
        f = fields(strong_ctx(b["closes"][-1]), b)
        self.assertIn("movement", f)
        self.assertIn("movement_recommendation", f)
        self.assertIsNotNone(f["movement"]["movement_potential_score"])
        c = server._movement_candidate_fields(f)
        for k in ("movementScore", "recentActivityScore", "activityState", "preBreakout", "tooLate", "momentumMode",
                  "recommendedStop", "riskReward", "movementRecommendation"):
            self.assertIn(k, c)
        json.dumps(c, default=str)

    def test_shadow_does_not_change_existing_entry_state(self):
        b = scaled(pullback_ready(1016.0))
        ctx = strong_ctx(b["closes"][-1])
        base = fields(ctx, b)
        wild = {"movement_potential_score": 0, "recent_activity_score": 0, "activity_state": "FADING", "pre_breakout": True,
                "too_late": True, "too_late_reasons": ["x"], "momentum_mode": True, "momentum_flags": [], "recommended_stop": None,
                "risk_reward": None, "reasons": [], "breakdown": {}, "confidence": "HIGH", "features": {}}
        with mock.patch.object(mp, "evaluate_movement", return_value=wild):
            other = fields(ctx, b)
        for k in ("entry_state", "entry_state_pre_chart", "entry_score", "entry_decision", "stock_strength_score", "entry_timing_score"):
            self.assertEqual(base[k], other[k], k)

    def test_same_price_different_shape_gives_different_movement_and_too_late_for_chase(self):
        a_bars, b_bars = scaled(pullback_ready(1016.0)), scaled(chase(1016.0))
        ctx = strong_ctx(a_bars["closes"][-1])
        a, b = fields(ctx, a_bars), fields(ctx, b_bars)
        self.assertTrue(b["movement"]["too_late"])                       # 15分急騰・上ヒゲ等＝強いが遅い
        self.assertFalse(a["movement"]["too_late"])
        self.assertEqual(b["movement_recommendation"], "TOO_LATE")       # legacyがENTRY可でもTOO_LATE
        self.assertNotEqual(a["movement_recommendation"], "TOO_LATE")
        self.assertTrue(any("15分" in r or "VWAP" in r for r in b["movement"]["too_late_reasons"]))

    def test_rescore_has_same_movement_fields_as_scan(self):
        bars = scaled(pullback_ready(1016.0))
        price = bars["closes"][-1]
        ctx = strong_ctx(price)
        ctx["bars"] = bars
        shared = _shared()
        full = fields(ctx, bars, shared)
        q = _quote(price, p=price / 1.05)
        q.update({"changePct": 5.0, "high": ctx["row"]["high"], "low": ctx["row"]["low"], "volume": ctx["row"]["volume"]})
        with mock.patch.object(server, "get_internal_intraday_bars", return_value={"bars": []}):
            light = server.rescore_entry_candidate_with_quote(ctx, shared, q)
        self.assertEqual(light["movementScore"], full["movement"]["movement_potential_score"])
        self.assertEqual(light["activityState"], full["movement"]["activity_state"])
        self.assertEqual(light["movementRecommendation"], full["movement_recommendation"])

    def test_rescore_interval_is_within_15_to_30_seconds(self):
        self.assertTrue(15 <= server.ENTRY_RESCORE_INTERVAL_SEC <= 30)


def pool_cand(code, **kw):
    c = {"code": code, "name": code, "current": 1000.0, "changePct": 1.0, "stockStrengthScore": 60, "entryScore": 70,
         "entryStatePreChart": "WATCH", "legacyEntryState": "WATCH", "chartEntryState": "WATCH", "entryState": "WATCH",
         "entryDecision": None, "movementScore": 50, "recentActivityScore": 50, "activityState": "ACTIVE", "preBreakout": False,
         "tooLate": False, "momentumMode": False, "movementRecommendation": "NONE", "entryTimingScore": 60, "isManual": True,
         "chartPattern": "BASE_BUILDING", "movementFeatures": {"aboveVwap": True},
         "chartContext": {"pattern": "BASE_BUILDING", "entry_timing_score": 60, "confidence": "HIGH", "barCount": 14,
                          "reasons": [], "penalties": [], "features": {}}}
    c.update(kw)
    return c


class ShadowRefreshTests(unittest.TestCase):
    def setUp(self):
        server._DYNAMIC_WATCH.clear()
        server._CHART_SIGNAL_LAST.clear()
        server._CHART_SIGNAL_MEM.clear()
        server._MOVEMENT_MILESTONES.clear()

    def put_cache(self, pool):
        existing = {"analysisTop5": [{"code": "A"}], "actionableTop5": [{"code": "A"}], "entryReadyTop5": [{"code": "A"}],
                    "watchCandidates": [{"code": "B"}]}
        with server._ENTRY_TOP5_CACHE_LOCK:
            server._ENTRY_TOP5_CACHE["u9"] = {"_candidatePool": pool, **json.loads(json.dumps(existing))}
        return existing

    def test_refresh_builds_shadow_lists_without_touching_existing_top5(self):
        pool = [pool_cand("A", movementScore=40, activityState="LOW_ACTIVITY", recentActivityScore=10),
                pool_cand("X", movementScore=80, recentActivityScore=70, activityState="EXPANDING", movementRecommendation="WATCH_EXPANDING"),
                pool_cand("E", movementRecommendation="ENTRY_READY", chartEntryState="ENTRY_READY", entryTimingScore=85,
                          chartPattern="PULLBACK_READY", movementFeatures={"aboveVwap": True, "higherLow": True}),
                pool_cand("L", movementRecommendation="TOO_LATE", legacyEntryState="ENTRY_READY", tooLate=True,
                          tooLateReasons=["15分+4.8%", "VWAP+2.1%"])]
        existing = self.put_cache(pool)
        with mock.patch.object(server, "investment_db", None):
            shadow = server.refresh_shadow_movement("url", "u9")
        cur = server._ENTRY_TOP5_CACHE["u9"]
        for k, v in existing.items():                                    # 既存TOP5は不変
            self.assertEqual(cur[k], v, k)
        self.assertIs(cur["shadowMovement"], shadow)
        self.assertEqual(shadow["attentionTop5"][0]["code"], "X")
        self.assertNotIn("A", [d["code"] for d in shadow["attentionTop5"]])      # LOW_ACTIVITYは注目から外れる
        self.assertEqual(shadow["attentionTop5"][0]["existingRank"], None)
        self.assertEqual([d["code"] for d in shadow["entryBoard"]], ["E"])
        self.assertEqual(shadow["entryBoard"][0]["entryReason"], "VWAP押し目 → higher low → 再上昇")
        self.assertEqual([d["code"] for d in shadow["tooLate"]], ["L"])
        self.assertEqual(shadow["tooLate"][0]["tooLateReasons"], ["15分+4.8%", "VWAP+2.1%"])
        self.assertIn("X", shadow["dynamicWatch"]["hot"])
        json.dumps(shadow, default=str)

    def test_dynamic_watch_persists_only_dynamic_table_never_watchlist(self):
        pool = [pool_cand("X", movementScore=80, recentActivityScore=70, activityState="EXPANDING", isManual=False)]
        self.put_cache(pool)
        with mock.patch.object(server, "investment_db") as db, mock.patch.object(server, "WRITE_E2E_ALLOWED", True), \
                mock.patch.object(server, "_in_jp_session", return_value=True):
            db.load_dynamic_watch.return_value = {}
            server.refresh_shadow_movement("url", "u9")
        (_, user, adds, removes, pool_updates), _kw = db.sync_dynamic_watch.call_args
        self.assertEqual([a["code"] for a in adds], ["X"])
        called = {name for name, *_ in db.method_calls}
        self.assertTrue(called <= {"load_dynamic_watch", "sync_dynamic_watch", "list_portfolio"})   # watchlistを触る関数は呼ばない

    def test_manual_code_may_leave_overlay_but_manual_watchlist_is_never_touched(self):
        real_now = datetime.datetime.now(JST)   # refresh_shadow_movementは実時刻で経過を測る
        server._DYNAMIC_WATCH["u9"] = {"M": {"code": "M", "name": "M", "is_manual": True, "pool": "ACTIVE", "source": "HOT",
                                             "added_at": real_now - datetime.timedelta(hours=2), "last_seen_at": real_now,
                                             "weak_since": real_now - datetime.timedelta(hours=1)}}
        self.put_cache([pool_cand("M", movementScore=10, activityState="LOW_ACTIVITY", isManual=True)])
        with mock.patch.object(server, "investment_db") as db, mock.patch.object(server, "WRITE_E2E_ALLOWED", True), \
                mock.patch.object(server, "_in_jp_session", return_value=True):
            shadow = server.refresh_shadow_movement("url", "u9")
        self.assertNotIn("M", server._DYNAMIC_WATCH["u9"])
        self.assertEqual(shadow["dynamicWatch"]["removed"], [{"code": "M", "reason": "LOW_ACTIVITY"}])
        called = {name for name, *_ in db.method_calls}
        self.assertTrue(called <= {"sync_dynamic_watch"})            # watchlist（手動）を更新・削除する関数は呼ばない


class LogTests(unittest.TestCase):
    def rec(self, **kw):
        c = pool_cand("Z", **kw)
        return sl.build_signal_record("u", c, T0, "SCAN")

    def test_movement_columns_are_saved(self):
        r = self.rec(movementScore=72, recentActivityScore=66, activityState="EXPANDING", preBreakout=True,
                     recommendedStop={"price": 1226.0, "distancePct": 1.9, "method": "BREAKOUT_LEVEL"},
                     riskReward={"rr": 2.1, "targetPrice": 1300.0}, movementRecommendation="WATCH_EXPANDING", momentumMode=True,
                     movementEntryReason="ブレイク接近 → 出来高2.4倍")
        self.assertEqual((r["movement_score"], r["recent_activity"], r["activity_state"], r["pre_breakout"]), (72, 66, "EXPANDING", True))
        self.assertEqual((r["recommended_stop"], r["stop_distance_pct"], r["risk_reward"]), (1226.0, 1.9, 2.1))
        self.assertEqual(r["movement_recommendation"], "WATCH_EXPANDING")
        self.assertTrue(sl.is_loggable(dict(r, legacy_entry_state="WEAK", chart_entry_state="WEAK")))   # 値幅系は状態がWEAKでも記録

    def test_milestones_first_time_only_and_force_log(self):
        mem = {}
        key = ("u", "Z")
        r1 = self.rec(activityState="ACTIVE", movementScore=40)
        self.assertEqual(sl.update_milestones(mem, key, r1, at(0)), [])
        r2 = self.rec(activityState="EXPANDING", movementScore=70, preBreakout=False)
        self.assertEqual(sl.update_milestones(mem, key, r2, at(5)), ["first_movement_at", "first_expanding_at"])
        r3 = self.rec(activityState="EXPANDING", movementScore=72, preBreakout=True)
        self.assertEqual(sl.update_milestones(mem, key, r3, at(10)), ["first_pre_breakout_at"])
        r4 = self.rec(activityState="EXPANDING", preBreakout=True, chartPattern="EARLY_BREAKOUT")
        r4["chart_pattern"] = "EARLY_BREAKOUT"
        self.assertEqual(sl.update_milestones(mem, key, r4, at(15)), ["first_early_breakout_at"])
        r5 = self.rec(chartPattern="CHASE")
        r5["chart_pattern"] = "CHASE"
        self.assertEqual(sl.update_milestones(mem, key, r5, at(20)), ["first_chase_at"])
        self.assertEqual(sl.update_milestones(mem, key, r5, at(25)), [])                   # 2回目は新規でない
        ms = r5["context"]["milestones"]
        self.assertEqual(ms["first_expanding_at"], at(5).isoformat())
        self.assertLess(ms["first_movement_at"], ms["first_early_breakout_at"])
        last = sl.last_state(r1, at(0))
        self.assertTrue(sl.should_log(last, r2, at(1)))                                    # 新マイルストーンは即記録

    def test_milestone_resets_next_day(self):
        mem = {}
        r = self.rec(activityState="EXPANDING", movementScore=70)
        sl.update_milestones(mem, ("u", "Z"), r, at(0))
        nxt = self.rec(activityState="EXPANDING", movementScore=70)
        self.assertEqual(sl.update_milestones(mem, ("u", "Z"), nxt, at(24 * 60)), ["first_movement_at", "first_expanding_at"])


class SummaryTests(unittest.TestCase):
    def row(self, code="M1", at_min=0, entry=1000.0, chart="ENTRY_READY", mrec="ENTRY_READY", mom=True, stop=990.0, **kw):
        r = {"code": code, "logged_at": at(at_min), "current_price": entry, "chart_pattern": "EARLY_BREAKOUT",
             "legacy_entry_state": "WAIT_BREAKOUT", "chart_entry_state": chart, "movement_recommendation": mrec,
             "activity_state": "EXPANDING", "pre_breakout": False, "momentum_mode": mom, "recommended_stop": stop,
             "stop_distance_pct": 1.0, "risk_reward": 2.0, "price_5m": None, "price_15m": None, "price_30m": None,
             "max_30m": None, "min_30m": None, "movement": {"entryReason": "ブレイク接近 → 出来高2.4倍"}, "context": {}}
        r.update(kw)
        return r

    def test_stop_evaluation_classes(self):
        shallow = self.row(min_30m=985.0, price_30m=1012.0)     # 刈られたが戻った
        proper = self.row(min_30m=985.0, price_30m=980.0)       # 刈られて下落継続
        deep = self.row(min_30m=998.0, price_30m=1010.0)        # 逆行が浅く、逆指値が遠すぎた
        ok = self.row(min_30m=993.0, price_30m=1010.0)          # 届かず、逆行が逆指値幅の70%
        self.assertEqual(sl.stop_evaluation(shallow), "TOO_SHALLOW")
        self.assertEqual(sl.stop_evaluation(proper), "APPROPRIATE")
        self.assertEqual(sl.stop_evaluation(deep), "TOO_DEEP")
        self.assertEqual(sl.stop_evaluation(ok), "APPROPRIATE")
        self.assertIsNone(sl.stop_evaluation(self.row()))

    def test_momentum_entry_shadow_record_has_stop_mfe_mae_and_returns(self):
        r = self.row(price_5m=1004.0, price_15m=1012.0, price_30m=1020.0, max_30m=1025.0, min_30m=997.0)
        m = sl.summarize_movement([r])["momentum_entries"][0]
        self.assertEqual((m["entry_price"], m["stop"], m["stop_distance_pct"]), (1000.0, 990.0, 1.0))
        self.assertEqual((m["ret_5m"], m["ret_15m"], m["ret_30m"]), (0.4, 1.2, 2.0))
        self.assertEqual((m["mfe_30m"], m["mae_30m"]), (2.5, -0.3))
        self.assertEqual(m["stop_evaluation"], "TOO_DEEP")

    def test_existing_vs_movement_comparison_groups(self):
        rows = [self.row("A", chart="ENTRY_READY", mrec="ENTRY_READY", price_15m=1010.0),
                self.row("B", chart="ENTRY_READY", mrec="TOO_LATE", price_15m=990.0),
                self.row("C", chart="WATCH", mrec="ENTRY_READY", price_15m=1010.0),
                self.row("D", chart="WATCH", mrec="PRE_BREAKOUT", price_15m=1005.0, pre_breakout=True),
                self.row("E", chart="ENTRY_READY", mrec="BLOCKED_LOW_ACTIVITY", price_15m=1000.0)]
        cmp_ = sl.summarize_movement(rows)["comparison"]
        self.assertEqual(cmp_["existing_ENTRY_and_movement_ENTRY"]["n"], 1)
        self.assertEqual(cmp_["existing_ENTRY_but_movement_TOO_LATE"]["avg_ret_15m"], -1.0)
        self.assertEqual(cmp_["movement_ENTRY_but_existing_not_ENTRY"]["n"], 1)
        self.assertEqual(cmp_["movement_PRE_BREAKOUT"]["avg_ret_15m"], 0.5)
        self.assertEqual(cmp_["existing_ENTRY_but_movement_LOW_ACTIVITY"]["n"], 1)

    def test_milestone_timeline_orders_by_first_movement(self):
        a = self.row("A", context={"milestones": {"first_movement_at": at(20).isoformat(), "first_expanding_at": at(20).isoformat()}})
        b = self.row("B", context={"milestones": {"first_movement_at": at(5).isoformat(), "first_pre_breakout_at": at(9).isoformat()}})
        tl = sl.summarize_movement([a, b])["milestone_timeline"]
        self.assertEqual([t["code"] for t in tl], ["B", "A"])
        self.assertEqual(tl[0]["first_pre_breakout_at"], at(9).isoformat())

    def test_daily_summary_includes_movement_and_is_json_serializable(self):
        s = sl.summarize_day([self.row(price_15m=1010.0, logged_at=at(0).isoformat())])
        json.dumps(s, ensure_ascii=False, default=str)
        self.assertIn("movement", s)


class NoHindsightTests(unittest.TestCase):
    def test_movement_code_never_reads_outcomes(self):
        import inspect
        for obj in (mp, server._movement_candidate_fields, server.refresh_shadow_movement):
            src = inspect.getsource(obj)
            for banned in ("price_5m", "price_15m", "price_30m", "list_chart_signals", "max_30m", "min_30m"):
                self.assertNotIn(banned, src)


if __name__ == "__main__":
    unittest.main()


class ExposureAndStopFloorTests(unittest.TestCase):
    def test_shadow_movement_is_exposed_by_live_response_and_existing_lists_untouched(self):
        from test_entry_top5_realtime import _make_cache_entry
        entry = _make_cache_entry(age_sec=5, entry_states=["ENTRY_READY", "WATCH"])
        entry["shadowMovement"] = {"attentionTop5": [{"code": "X"}], "entryBoard": [], "hotPool": []}
        res = server._apply_entry_top5_staleness(entry)
        self.assertEqual(res["shadowMovement"]["attentionTop5"][0]["code"], "X")
        self.assertEqual(res["entryReadyTop5"][0]["entryState"], "ENTRY_READY")
        entry.pop("shadowMovement")
        self.assertIsNone(server._apply_entry_top5_staleness(entry)["shadowMovement"])

    def test_stop_distance_is_at_least_one_5m_atr(self):
        from test_movement_potential import pre_breakout_bars
        f = mp.compute_movement_features(pre_breakout_bars())
        atr_pct = f["atr5"] / f["price"] * 100.0
        f["lows"] = f["lows"][:-2] + [f["price"] * 0.9999] * 2          # 直近安値が現在値の直下（ノイズ幅の逆指値になる）
        st = mp.recommended_stop(f, {"pattern": "PRE_BREAKOUT", "features": {}})
        self.assertGreaterEqual(st["distancePct"], round(atr_pct, 2) - 0.011)
        self.assertGreater(st["distancePct"], mp.STOP_MIN_PCT)
        rr = mp.risk_reward(f, st)
        self.assertLess(rr["rr"], 6)                                     # 極小リスクでRRが水増しされない
