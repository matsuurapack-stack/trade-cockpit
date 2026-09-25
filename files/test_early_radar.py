# Early Momentum Radar（Phase D.1）の単体テスト。 cd files && python -m unittest test_early_radar -v

import inspect
import unittest

import chart_context
import early_radar as er
import movement_potential


def build(spec, start=1000.0):
    """spec: [(close変化%, 値幅%, 出来高[, 上側割合])...] → dict-of-arrays（opens付き）。"""
    o, h, l, c, v = [], [], [], [], []
    px = start
    for item in spec:
        chg, rng, vol = item[:3]
        up = item[3] if len(item) > 3 else 0.5
        op = px
        cl = px * (1 + chg / 100.0)
        o.append(op)
        h.append(max(op, cl) * (1 + rng * up / 100.0))
        l.append(min(op, cl) * (1 - rng * (1 - up) / 100.0))
        c.append(cl)
        v.append(vol)
        px = cl
    return {"opens": o, "highs": h, "lows": l, "closes": c, "volumes": v}


def radar(spec, **kw):
    b = build(spec)
    return er.evaluate_radar(b, quote={"t": b["closes"][-1]}, **kw)


QUIET3 = [(0.05, 0.30, 1000), (0.05, 0.30, 1000), (0.05, 0.30, 1000)]


class ConfidenceTests(unittest.TestCase):
    def test_bar_count_to_confidence_and_handoff(self):
        self.assertEqual(radar([(0.1, 0.3, 1000)])["state"], "RADAR_NONE")
        self.assertEqual(radar([(0.1, 0.3, 1000)])["confidence"], "UNKNOWN")
        self.assertEqual(radar(QUIET3[:2])["confidence"], "LOW")
        self.assertEqual(radar(QUIET3)["confidence"], "MEDIUM_LOW")
        self.assertEqual(radar(QUIET3 + [(0.05, 0.3, 1000)])["confidence"], "MEDIUM_LOW")
        self.assertEqual(radar(QUIET3 + [(0.05, 0.3, 1000)] * 2)["confidence"], "MEDIUM")
        r = radar(QUIET3 + [(0.05, 0.3, 1000)] * 3)
        self.assertTrue(r["handoff"])                       # 6本以上は通常Phase Dへ引き継ぎ
        self.assertEqual((r["state"], r["confidence"]), ("RADAR_NONE", "HANDOFF"))

    def test_rank_score_is_discounted_by_low_confidence_only(self):
        spec = [(0.05, 0.3, 1000), (0.8, 1.0, 5000)]
        low = radar(spec)
        self.assertEqual(low["confidence"], "LOW")
        self.assertAlmostEqual(low["rank_score"], low["early_momentum_score"] * 0.7, delta=0.4)


class StateTests(unittest.TestCase):
    def test_surge_needs_volume_and_range_acceleration_and_up_move(self):
        r = radar(QUIET3 + [(1.2, 1.2, 4000)])            # 出来高4倍・値幅4倍・上昇
        self.assertEqual(r["state"], "RADAR_SURGE")
        self.assertTrue(any("出来高加速" in x for x in r["reasons"]))
        self.assertEqual(er.radar_reason_text(r).split(" / ")[0], "出来高加速")

    def test_falling_bar_with_big_volume_is_not_surge(self):
        r = radar(QUIET3 + [(-1.2, 1.2, 4000)])
        self.assertNotIn(r["state"], ("RADAR_SURGE", "RADAR_EXPANDING", "RADAR_PRE_BREAKOUT"))

    def test_expanding(self):
        r = radar(QUIET3 + [(0.4, 1.2, 1700, 0.95)])       # 値幅4倍・出来高1.7倍・高値から1%超下（PRE_BREAKOUTの範囲外）
        self.assertEqual(r["state"], "RADAR_EXPANDING")

    def test_pre_breakout_near_high_above_vwap_with_volume(self):
        spec = [(0.25, 0.30, 1000), (0.25, 0.30, 1000), (0.20, 0.30, 1000), (0.10, 0.30, 1400, 0.1)]
        b = build(spec)
        day_high = max(b["highs"]) * 1.0045                # 当日高値まで約0.45%
        r = er.evaluate_radar(b, quote={"t": b["closes"][-1]}, day_high=day_high, vwap=b["closes"][-1] * 0.995)
        self.assertEqual(r["state"], "RADAR_PRE_BREAKOUT")

    def test_active_when_score_moderate(self):
        b = build([(0.5, 0.4, 1000), (0.6, 0.5, 1300), (0.6, 0.9, 1600, 0.98)])
        r = er.evaluate_radar(b, quote={"t": b["closes"][-1]}, vwap=1005)
        self.assertEqual(r["state"], "RADAR_ACTIVE")
        self.assertGreaterEqual(r["early_momentum_score"], er.ACTIVE_SCORE)

    def test_quiet_stock_is_none(self):
        r = radar([(0.0, 0.10, 500), (0.0, 0.10, 500), (0.0, 0.10, 500), (-0.02, 0.10, 500)])
        self.assertEqual(r["state"], "RADAR_NONE")

    def test_absolute_volume_alone_does_not_trigger(self):
        r = radar([(0.05, 0.3, 900000)] * 4)               # 出来高は巨大だが加速なし
        self.assertNotIn(r["state"], ("RADAR_SURGE", "RADAR_EXPANDING"))
        self.assertLess(r["early_momentum_score"], er.ACTIVE_SCORE + 20)

    def test_wide_spread_and_below_vwap_reduce_score(self):
        b = build(QUIET3 + [(0.8, 1.0, 3000)])
        base = er.evaluate_radar(b, quote={"t": b["closes"][-1]}, vwap=b["closes"][-1] * 0.99)
        wide = er.evaluate_radar(b, quote={"t": b["closes"][-1]}, vwap=b["closes"][-1] * 0.99, spread_pct=0.9)
        below = er.evaluate_radar(b, quote={"t": b["closes"][-1]}, vwap=b["closes"][-1] * 1.02)
        self.assertLess(wide["early_momentum_score"], base["early_momentum_score"])
        self.assertLess(below["early_momentum_score"], base["early_momentum_score"])
        self.assertTrue(any("スプレッド" in x for x in wide["reasons"]))


class NeverEntryTests(unittest.TestCase):
    def test_radar_never_allows_entry(self):
        for spec in (QUIET3 + [(1.2, 1.2, 4000)], QUIET3 + [(0.4, 0.5, 1700)]):
            self.assertFalse(radar(spec)["entry_allowed"])

    def test_entry_logic_does_not_reference_radar(self):
        for obj in (movement_potential, chart_context):
            self.assertNotIn("early_radar", inspect.getsource(obj))

    def test_radar_hot_conditions(self):
        self.assertTrue(er.radar_hot({"state": "RADAR_SURGE", "early_momentum_score": 10}))
        self.assertTrue(er.radar_hot({"state": "RADAR_PRE_BREAKOUT", "early_momentum_score": 10}))
        self.assertTrue(er.radar_hot({"state": "RADAR_ACTIVE", "early_momentum_score": er.HOT_SCORE}))
        self.assertFalse(er.radar_hot({"state": "RADAR_ACTIVE", "early_momentum_score": er.HOT_SCORE - 1}))
        self.assertFalse(er.radar_hot({"state": "RADAR_SURGE", "handoff": True, "early_momentum_score": 90}))
        self.assertFalse(er.radar_hot(None))


class NoFutureDataTests(unittest.TestCase):
    def test_depends_only_on_given_bars(self):
        a = build(QUIET3 + [(0.8, 1.0, 3000)])
        r1 = er.evaluate_radar(a, quote={"t": a["closes"][-1]})
        r2 = er.evaluate_radar({k: list(v) for k, v in a.items()}, quote={"t": a["closes"][-1]})
        self.assertEqual(r1["early_momentum_score"], r2["early_momentum_score"])


if __name__ == "__main__":
    unittest.main()
