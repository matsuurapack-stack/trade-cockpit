# Movement Potential Engine（Phase D）の単体テスト。 cd files && python -m unittest test_movement_potential -v

import unittest

import chart_context as cc
import movement_potential as mp


def build(spec, start=1000.0):
    """spec: [(close変化%, 値幅%, 出来高)...] → dict-of-arrays（opens付き）。値幅は終値を中心に上下へ。"""
    o, h, l, c, v = [], [], [], [], []
    px = start
    for item in spec:
        chg, rng, vol = item[:3]
        up = item[3] if len(item) > 3 else 0.5      # 値幅のうち上側（上ヒゲ側）に振る割合
        op = px
        cl = px * (1 + chg / 100.0)
        hi = max(op, cl) * (1 + rng * up / 100.0)
        lo = min(op, cl) * (1 - rng * (1 - up) / 100.0)
        o.append(op); h.append(hi); l.append(lo); c.append(cl); v.append(vol)
        px = cl
    return {"opens": o, "highs": h, "lows": l, "closes": c, "volumes": v}


QUIET = [(0.02, 0.25, 1000)] * 14


def evaluate(bars, **kw):
    b = cc.normalize_bars(bars)
    chart = kw.pop("chart", None)
    if chart is None:
        chart = cc.evaluate_chart_context(bars, quote={"t": b["closes"][-1]}, vwap=kw.get("vwap"),
                                           day_high=kw.get("day_high"), day_low=kw.get("day_low"))
    return mp.evaluate_movement(bars, quote={"t": b["closes"][-1]}, chart=chart, **kw), chart


def expanding_bars():
    # 通常値幅0.25%の静かな展開 → 0.3→0.4→0.7→1.1%と拡大、出来高も1.3→2→3倍、価格は高値へ接近
    return build(QUIET + [(0.10, 0.30, 1300), (0.15, 0.40, 2000), (0.25, 0.70, 3000), (0.35, 1.10, 3300)])


def morning_only_faded():
    # 寄り〜9:30で+8%、その後は値幅0.15%・出来高減・高値切り下げ・VWAP付近横ばい
    up = [(1.4, 1.8, 9000), (1.5, 1.9, 8000), (1.3, 1.6, 7000), (1.4, 1.7, 6000), (1.2, 1.5, 5000), (1.2, 1.4, 4500)]
    down = [(-0.30, 0.6, 2500), (-0.25, 0.5, 2000), (-0.20, 0.4, 1500), (0.02, 0.15, 900), (-0.05, 0.15, 800),
            (0.03, 0.15, 700), (-0.06, 0.15, 700), (0.02, 0.15, 650), (-0.04, 0.14, 600), (-0.05, 0.14, 600),
            (0.01, 0.12, 550), (-0.03, 0.12, 500), (-0.02, 0.12, 500), (-0.04, 0.12, 480)]
    return build(up + down)


class ActivityTests(unittest.TestCase):
    def test_expanding_is_detected(self):
        mv, _ = evaluate(expanding_bars(), rel_volume=2.0, market_rs=1.0)
        self.assertEqual(mv["activity_state"], "EXPANDING")
        self.assertGreaterEqual(mv["movement_potential_score"], 55)
        self.assertTrue(any("値幅" in r for r in mv["activity_reasons"]))

    def test_morning_only_move_is_ranked_down_even_with_big_day_gain(self):
        faded = morning_only_faded()
        mv_f, _ = evaluate(faded, rel_volume=1.0, market_rs=8.0)     # 前日比は大きい（対市場RS+8）
        mv_e, _ = evaluate(expanding_bars(), rel_volume=2.0, market_rs=1.0)   # 前日比は小さい
        self.assertIn(mv_f["activity_state"], ("FADING", "LOW_ACTIVITY"))
        self.assertLess(mv_f["movement_potential_score"], mv_e["movement_potential_score"])
        self.assertLess(mp.attention_rank_score(mv_f), mp.attention_rank_score(mv_e))
        self.assertLess(mv_f["recent_activity_score"], 30)

    def test_low_activity_flat_stock(self):
        mv, _ = evaluate(build(QUIET + [(0.0, 0.10, 500)] * 8))
        self.assertEqual(mv["activity_state"], "LOW_ACTIVITY")
        self.assertLess(mv["movement_potential_score"], 40)
        self.assertLess(mv["recent_activity_score"], 25)

    def test_same_day_change_different_recent_activity_gives_different_score(self):
        a, _ = evaluate(expanding_bars(), rel_volume=1.0, market_rs=3.0)
        b, _ = evaluate(morning_only_faded(), rel_volume=1.0, market_rs=3.0)
        self.assertGreater(a["recent_activity_score"], b["recent_activity_score"] + 30)

    def test_insufficient_bars_is_unknown_and_never_strong(self):
        mv = mp.evaluate_movement(build([(0.1, 0.3, 1000)] * 5), quote={"t": 1005.0})
        self.assertEqual(mv["activity_state"], "UNKNOWN")
        self.assertIsNone(mv["movement_potential_score"])
        self.assertFalse(mv["pre_breakout"])
        self.assertIsNone(mv["recommended_stop"])
        self.assertEqual(mp.movement_recommendation("ENTRY_READY", "ENTRY_READY", mv, None), "NONE")

    def test_fading_after_high(self):
        mv, _ = evaluate(morning_only_faded(), rel_volume=0.8)
        self.assertEqual(mv["activity_state"], "FADING")
        self.assertTrue(any("継続" in r or "失速" in r or "更新なし" in r or "下落" in r for r in mv["reasons"] + mv["activity_reasons"]))


def pre_breakout_bars():
    # 高値(1005付近)まで約0.5%下でBASE形成：higher low・出来高増・値幅拡大・VWAP上
    spec = QUIET[:8] + [(0.20, 0.30, 1000), (0.20, 0.30, 1000), (0.25, 0.30, 1100), (0.25, 0.30, 1200), (0.20, 0.30, 1200),
                        (0.10, 0.40, 1500, 0.1), (-0.10, 0.35, 1400, 0.1), (0.10, 0.35, 1700, 0.1), (0.05, 0.40, 2000, 0.1)]
    return build(spec)


class PreBreakoutTests(unittest.TestCase):
    def test_pre_breakout_detected_and_not_entry_ready(self):
        bars = pre_breakout_bars()
        b = cc.normalize_bars(bars)
        day_high = max(b["highs"]) * 1.0055                 # 当日高値まで約0.55%
        mv, chart = evaluate(bars, rel_volume=1.8, day_high=day_high, vwap=b["closes"][-1] * 0.99)
        self.assertTrue(mv["pre_breakout"], mv["features"])
        self.assertTrue(any("VWAP" in r for r in mv["pre_breakout_reasons"]))
        rec = mp.movement_recommendation("WAIT_BREAKOUT", "WAIT_BREAKOUT", mv, {"pattern": "BASE_BUILDING"})   # まだブレイクしていない
        self.assertEqual(rec, "PRE_BREAKOUT")               # まだENTRY READYではない

    def test_no_pre_breakout_when_below_vwap(self):
        bars = pre_breakout_bars()
        b = cc.normalize_bars(bars)
        mv, _ = evaluate(bars, day_high=max(b["highs"]) * 1.0055, vwap=b["closes"][-1] * 1.01)
        self.assertFalse(mv["pre_breakout"])

    def test_no_pre_breakout_when_far_from_high(self):
        bars = pre_breakout_bars()
        b = cc.normalize_bars(bars)
        mv, _ = evaluate(bars, day_high=max(b["highs"]) * 1.03, vwap=b["closes"][-1] * 0.99)
        self.assertFalse(mv["pre_breakout"])


class PreBreakoutNeedsActivityTests(unittest.TestCase):
    def test_low_activity_stock_is_never_pre_breakout(self):
        # 高値0.5%下・VWAP上・higher low・BASE形成でも、値幅が通常の半分以下なら「動かない銘柄」としてPRE_BREAKOUTにしない
        spec = [(0.10, 0.6, 1000)] * 10 + [(0.02, 0.10, 400)] * 4 + [(0.05, 0.10, 400, 0.1), (0.03, 0.10, 380, 0.1)]
        bars = build(spec)
        b = cc.normalize_bars(bars)
        mv, _ = evaluate(bars, day_high=max(b["highs"]) * 1.005, vwap=b["closes"][-1] * 0.99)
        self.assertIn(mv["activity_state"], ("LOW_ACTIVITY", "COILING", "ACTIVE"))
        if mv["activity_state"] == "LOW_ACTIVITY":
            self.assertFalse(mv["pre_breakout"])


class TooLateTests(unittest.TestCase):
    def chart(self, pattern="BASE_BUILDING", **ft):
        return {"pattern": pattern, "features": ft, "entry_timing_score": 60, "confidence": "HIGH"}

    def test_two_late_flags_is_too_late(self):
        late, why = mp.detect_too_late(self.chart(chg15m=4.8, vwapDistPct=2.1))
        self.assertTrue(late)
        self.assertIn("15分+4.8%", why)
        self.assertIn("VWAP+2.1%", why)

    def test_single_flag_is_not_too_late(self):
        self.assertFalse(mp.detect_too_late(self.chart(chg15m=1.5))[0])

    def test_chase_pattern_is_too_late_even_with_high_scores(self):
        self.assertTrue(mp.detect_too_late(self.chart("CHASE"))[0])
        mv = {"activity_state": "ACTIVE", "too_late": True, "pre_breakout": False, "recent_activity_score": 80,
              "movement_potential_score": 90}
        self.assertEqual(mp.movement_recommendation("ENTRY_READY", "ENTRY_READY", mv, self.chart("CHASE")), "TOO_LATE")

    def test_upper_wick_and_volume_peakout_flags(self):
        late, why = mp.detect_too_late(self.chart(upperWick=0.5, volPeakout=True))
        self.assertTrue(late)
        self.assertIn("上ヒゲ出現", why)

    def test_rr_shortfall_is_too_late(self):
        late, why = mp.detect_too_late(self.chart(), rr_ok=False, rr=1.1)
        self.assertTrue(late)
        self.assertTrue(any("RR不足" in w for w in why))


class RecommendationTests(unittest.TestCase):
    def mv(self, **kw):
        base = {"activity_state": "ACTIVE", "too_late": False, "pre_breakout": False, "recent_activity_score": 60,
                "movement_potential_score": 70}
        base.update(kw)
        return base

    def test_entry_ready_passes_when_active_and_not_late(self):
        self.assertEqual(mp.movement_recommendation("ENTRY_READY", "ENTRY_READY", self.mv(), {"pattern": "EARLY_BREAKOUT"}), "ENTRY_READY")

    def test_low_activity_blocks_existing_entry(self):
        self.assertEqual(mp.movement_recommendation("ENTRY_READY", "ENTRY_READY", self.mv(activity_state="LOW_ACTIVITY"),
                                                    {"pattern": "BASE_BUILDING"}), "BLOCKED_LOW_ACTIVITY")

    def test_expanding_is_watched_not_entry(self):
        self.assertEqual(mp.movement_recommendation("WATCH", "WATCH", self.mv(activity_state="EXPANDING"), {"pattern": "BASE_BUILDING"}),
                         "WATCH_EXPANDING")

    def test_pre_breakout_to_early_breakout_with_volume_promotes(self):
        mv = self.mv(pre_breakout=True, recent_activity_score=70)
        self.assertEqual(mp.movement_recommendation("WAIT_BREAKOUT", "WAIT_BREAKOUT", mv, {"pattern": "EARLY_BREAKOUT"}), "ENTRY_READY")

    def test_expanding_then_chase_is_not_bought(self):
        mv = self.mv(activity_state="EXPANDING", too_late=True)
        self.assertEqual(mp.movement_recommendation("ENTRY_READY", "ENTRY_READY", mv, {"pattern": "CHASE"}), "TOO_LATE")

    def test_attention_rank_prefers_expanding_and_pre_breakout(self):
        base = {"movement_potential_score": 60, "recent_activity_score": 50, "activity_state": "ACTIVE", "pre_breakout": False}
        exp = dict(base, activity_state="EXPANDING")
        pre = dict(base, pre_breakout=True)
        low = dict(base, activity_state="LOW_ACTIVITY")
        self.assertGreater(mp.attention_rank_score(exp), mp.attention_rank_score(base))
        self.assertGreater(mp.attention_rank_score(pre), mp.attention_rank_score(exp))
        self.assertLess(mp.attention_rank_score(low), mp.attention_rank_score(base))


class StopAndRRTests(unittest.TestCase):
    def test_stop_is_structure_based_short_and_below_price(self):
        bars = pre_breakout_bars()
        b = cc.normalize_bars(bars)
        mv, _ = evaluate(bars, rel_volume=2.5, market_rs=3.0, day_high=max(b["highs"]) * 1.0055, vwap=b["closes"][-1] * 0.99)
        st = mv["recommended_stop"]
        self.assertIsNotNone(st)
        self.assertLess(st["price"], b["closes"][-1])
        self.assertGreaterEqual(st["distancePct"], mp.STOP_MIN_PCT)
        self.assertLess(st["distancePct"], 4.0)          # 固定-8%ではなく短いデイトレ用
        self.assertIn(st["method"].split("+")[0], ("BREAKOUT_LEVEL", "RECENT_5M_LOW", "SWING_LOW", "VWAP", "ATR"))

    def test_stop_method_follows_structure(self):
        f = mp.compute_movement_features(pre_breakout_bars())
        brk = mp.recommended_stop(f, {"pattern": "EARLY_BREAKOUT", "features": {"breakoutLevel": f["price"] * 0.997}})
        self.assertEqual(brk["method"].split("+")[0], "BREAKOUT_LEVEL")
        vw = mp.recommended_stop(f, {"pattern": "VWAP_RECLAIM", "features": {"vwapDistPct": 0.6}})
        self.assertEqual(vw["method"].split("+")[0], "VWAP")

    def test_too_shallow_stop_is_widened_to_minimum(self):
        f = mp.compute_movement_features(pre_breakout_bars())
        f["lows"] = f["lows"][:-2] + [f["price"] * 0.9999] * 2      # 直近安値がほぼ現在値
        st = mp.recommended_stop(f, {"pattern": "PRE_BREAKOUT", "features": {}})
        self.assertGreaterEqual(st["distancePct"], mp.STOP_MIN_PCT - 1e-9)

    def test_rr_computation_and_momentum_requires_rr(self):
        f = mp.compute_movement_features(pre_breakout_bars())
        st = {"price": f["price"] * 0.99}
        rr = mp.risk_reward(f, st)
        self.assertGreater(rr["targetPrice"], f["price"])
        self.assertAlmostEqual(rr["rr"], (rr["targetPrice"] - f["price"]) / (f["price"] - st["price"]), places=1)
        self.assertIsNone(mp.risk_reward(f, None))

    def test_momentum_mode_needs_three_flags(self):
        f = mp.compute_movement_features(expanding_bars())
        flags = mp.momentum_flags(f, rel_volume=2.5, market_rs=3.0, chg15m=1.2)
        self.assertGreaterEqual(len(flags), mp.MOMENTUM_FLAGS_NEEDED)
        self.assertLess(len(mp.momentum_flags(mp.compute_movement_features(build(QUIET + QUIET))) ), mp.MOMENTUM_FLAGS_NEEDED)


class NoFutureDataTests(unittest.TestCase):
    def test_result_depends_only_on_given_bars(self):
        a = build(QUIET + [(0.1, 0.3, 1000)] * 3)
        mv1 = mp.evaluate_movement(a, quote={"t": a["closes"][-1]})
        mv2 = mp.evaluate_movement({k: list(v) for k, v in a.items()}, quote={"t": a["closes"][-1]})
        self.assertEqual(mv1["movement_potential_score"], mv2["movement_potential_score"])


if __name__ == "__main__":
    unittest.main()


class ShadowListTests(unittest.TestCase):
    def cand(self, code, rec="NONE", state="ACTIVE", score=50, recent=50, pre=False, timing=60, existing="WATCH", **kw):
        c = {"code": code, "name": code, "current": 1000.0, "movementScore": score, "recentActivityScore": recent,
             "activityState": state, "preBreakout": pre, "movementRecommendation": rec, "entryTimingScore": timing,
             "chartEntryState": existing, "legacyEntryState": existing, "chartPattern": "BASE_BUILDING"}
        c.update(kw)
        return c

    def test_attention_excludes_low_activity_and_fading_and_prefers_expanding(self):
        cands = [self.cand("LOW", state="LOW_ACTIVITY", score=90), self.cand("FAD", state="FADING", score=95),
                 self.cand("EXP", state="EXPANDING", score=60), self.cand("ACT", score=70), self.cand("UNK", state="UNKNOWN", score=99)]
        out = mp.build_shadow_lists(cands, existing_analysis_codes=["LOW", "ACT"])
        codes = [d["code"] for d in out["attentionTop5"]]
        self.assertEqual(codes, ["EXP", "ACT"])
        self.assertEqual(out["attentionTop5"][1]["existingRank"], 2)
        self.assertIsNone(out["attentionTop5"][0]["existingRank"])

    def test_entry_board_only_movement_ready_and_too_late_listed_separately(self):
        cands = [self.cand("OK", rec="ENTRY_READY", timing=80), self.cand("OK2", rec="ENTRY_READY", timing=90),
                 self.cand("LATE", rec="TOO_LATE", tooLateReasons=["15分+4.8%", "VWAP+2.1%"], existing="ENTRY_READY"),
                 self.cand("PRE", rec="PRE_BREAKOUT", pre=True), self.cand("EXP", rec="WATCH_EXPANDING", state="EXPANDING")]
        out = mp.build_shadow_lists(cands)
        self.assertEqual([d["code"] for d in out["entryBoard"]], ["OK2", "OK"])
        self.assertEqual([d["code"] for d in out["tooLate"]], ["LATE"])
        self.assertEqual(out["tooLate"][0]["tooLateReasons"], ["15分+4.8%", "VWAP+2.1%"])
        self.assertEqual([d["code"] for d in out["preBreakout"]], ["PRE"])
        self.assertEqual([d["code"] for d in out["expanding"]], ["EXP"])

    def test_entry_reason_line(self):
        pb = {"chartPattern": "PULLBACK_READY", "movementFeatures": {"aboveVwap": True, "higherLow": True}}
        self.assertEqual(mp.entry_reason_line(pb), "VWAP押し目 → higher low → 再上昇")
        br = {"chartPattern": "EARLY_BREAKOUT", "preBreakout": True,
              "movementFeatures": {"volRatioRecent": 2.4, "newHighCount12": 2}}
        self.assertEqual(mp.entry_reason_line(br), "ブレイク接近 → 高値ブレイク → 出来高2.4倍 → 高値更新")
        other = {"chartPattern": "BASE_BUILDING", "chartContext": {"reasons": ["a", "b", "c"]}}
        self.assertEqual(mp.entry_reason_line(other), "a → b")
