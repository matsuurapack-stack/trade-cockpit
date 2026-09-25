# Rolling Momentum Radar（Phase D.2）の単体テスト。 cd files && python -m unittest test_rolling_radar -v

import inspect
import unittest

import chart_context
import early_radar
import movement_potential
import rolling_radar as rr
from test_early_radar import build

QUIET5 = [(0.05, 0.30, 1000)] * 5


def roll(spec, **kw):
    b = build(spec)
    kw.setdefault("market_rs", 1.0)
    kw.setdefault("vwap", b["closes"][-1] * 0.99)
    return rr.evaluate_rolling(b, quote={"t": b["closes"][-1]}, **kw), b


class StateTests(unittest.TestCase):
    def test_needs_five_bars(self):
        r, _ = roll(QUIET5[:4])
        self.assertEqual(r["state"], "NONE")
        self.assertIsNone(r["features"])

    def test_single_bar_surge_after_quiet_period(self):
        # 静かな7本 → 1本だけ急伸（出来高4倍・値幅4倍・陽線・高値更新）
        r, _ = roll(QUIET5 + [(0.05, 0.30, 1000)] * 2 + [(1.2, 1.2, 4000)])
        self.assertEqual(r["state"], "SINGLE_BAR_SURGE")
        self.assertTrue(r["hot"])
        self.assertFalse(r["entry_allowed"])
        self.assertTrue(any("出来高" in x for x in r["reasons"]))

    def test_single_bar_surge_conditions_each_required(self):
        base = QUIET5 + [(0.05, 0.30, 1000)] * 2
        self.assertNotEqual(roll(base + [(-1.2, 1.2, 4000)])[0]["state"], "SINGLE_BAR_SURGE")       # 陰線
        self.assertNotEqual(roll(base + [(1.2, 1.2, 1500)])[0]["state"], "SINGLE_BAR_SURGE")        # 出来高不足
        self.assertNotEqual(roll(base + [(0.2, 0.35, 4000)])[0]["state"], "SINGLE_BAR_SURGE")       # 値幅不足

    def test_rolling_surge_when_previous_bar_was_also_surging(self):
        r, _ = roll(QUIET5 + [(0.05, 0.30, 1000)] + [(0.7, 0.8, 3000), (1.0, 1.1, 4200)])
        self.assertEqual(r["state"], "ROLLING_SURGE")
        self.assertTrue(r["hot"])

    def test_rolling_expanding_continuous_growth(self):
        r, _ = roll(QUIET5 + [(0.10, 0.35, 1300), (0.15, 0.50, 1700), (0.25, 0.80, 2000)])
        self.assertIn(r["state"], ("ROLLING_EXPANDING", "ROLLING_SURGE"))
        self.assertEqual(r["base_state"], r["state"])

    def test_rolling_pre_breakout(self):
        spec = QUIET5 + [(0.25, 0.30, 1000), (0.25, 0.30, 1000), (0.20, 0.30, 1500, 0.2), (0.10, 0.30, 1600, 0.1)]
        b = build(spec)
        r = rr.evaluate_rolling(b, quote={"t": b["closes"][-1]}, day_high=max(b["highs"]) * 1.0045,
                                vwap=b["closes"][-1] * 0.995, market_rs=1.0)
        self.assertEqual(r["state"], "ROLLING_PRE_BREAKOUT")
        self.assertTrue(r["hot"])

    def test_absolute_volume_alone_does_not_trigger(self):
        r, _ = roll([(0.05, 0.3, 900000)] * 8)
        self.assertEqual(r["base_state"], "NONE")

    def test_quiet_is_none(self):
        self.assertEqual(roll([(0.0, 0.1, 500)] * 8)[0]["state"], "NONE")


class FalsePositiveSuppressionTests(unittest.TestCase):
    SPEC = QUIET5 + [(0.05, 0.30, 1000)] * 2 + [(1.2, 1.2, 4000)]

    def test_missing_confirmations_downgrade_to_radar_weak(self):
        b = build(self.SPEC)
        r = rr.evaluate_rolling(b, quote={"t": b["closes"][-1]}, vwap=b["closes"][-1] * 1.02, market_rs=-1.0)    # VWAP下・対市場マイナス
        self.assertEqual(r["base_state"], "SINGLE_BAR_SURGE")
        self.assertEqual(r["state"], "RADAR_WEAK")
        self.assertIn("vwap", r["confirmations"]["failed"])
        self.assertIn("marketRS", r["confirmations"]["failed"])
        self.assertFalse(r["hot"])
        self.assertTrue(r["watch"])                          # 通常のdynamic watchまで

    def test_one_missing_confirmation_is_tolerated(self):
        b = build(self.SPEC)
        r = rr.evaluate_rolling(b, quote={"t": b["closes"][-1]}, vwap=b["closes"][-1] * 0.99, market_rs=-1.0)   # 対市場だけ不足
        self.assertEqual(r["state"], "SINGLE_BAR_SURGE")
        self.assertEqual(r["confirmations"]["failed"], ["marketRS"])

    def test_unknown_confirmations_are_not_counted_as_failures(self):
        b = build(self.SPEC)
        r = rr.evaluate_rolling(b, quote={"t": b["closes"][-1]}, vwap=b["closes"][-1] * 0.99)   # RS・sector・spreadが不明
        self.assertEqual(r["confirmations"]["known"], 2)                                        # vwap と recentHigh のみ
        self.assertEqual(r["state"], "SINGLE_BAR_SURGE")

    def test_wide_spread_and_weak_sector_count_as_missing(self):
        b = build(self.SPEC)
        r = rr.evaluate_rolling(b, quote={"t": b["closes"][-1]}, vwap=b["closes"][-1] * 0.99, market_rs=1.0,
                                sector_weak=True, spread_pct=0.9)
        self.assertEqual(r["state"], "RADAR_WEAK")
        self.assertEqual(set(r["confirmations"]["failed"]), {"sector", "spread"})

    def test_weak_never_promotes_none(self):
        b = build([(0.0, 0.1, 500)] * 8)
        r = rr.evaluate_rolling(b, quote={"t": b["closes"][-1]}, vwap=b["closes"][-1] * 1.05, market_rs=-2.0)
        self.assertEqual(r["state"], "NONE")


class ListAndIsolationTests(unittest.TestCase):
    def test_list_orders_hot_before_weak_and_caps_at_five(self):
        cands = [{"code": f"W{i}", "rollingState": "RADAR_WEAK", "rollingScore": 90 - i} for i in range(4)]
        cands += [{"code": "S", "rollingState": "SINGLE_BAR_SURGE", "rollingScore": 50},
                  {"code": "R", "rollingState": "ROLLING_SURGE", "rollingScore": 80},
                  {"code": "N", "rollingState": "NONE", "rollingScore": 99}]
        out = rr.build_rolling_radar_list(cands)
        self.assertEqual([d["code"] for d in out][:2], ["S", "R"])
        self.assertEqual(len(out), 5)
        self.assertNotIn("N", [d["code"] for d in out])
        self.assertTrue(all(d["entryAllowed"] is False for d in out))

    def test_entry_side_code_never_references_rolling(self):
        for obj in (movement_potential, chart_context, early_radar):
            self.assertNotIn("rolling_radar", inspect.getsource(obj))

    def test_depends_only_on_given_bars(self):
        a = build(QUIET5 + [(1.2, 1.2, 4000)])
        r1 = rr.evaluate_rolling(a, quote={"t": a["closes"][-1]})
        r2 = rr.evaluate_rolling({k: list(v) for k, v in a.items()}, quote={"t": a["closes"][-1]})
        self.assertEqual(r1["rolling_score"], r2["rolling_score"])


if __name__ == "__main__":
    unittest.main()
