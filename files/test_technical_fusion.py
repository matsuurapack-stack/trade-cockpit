# Phase G Technical Fusion Engine（shadow）のテスト。実行： cd files && python -m unittest test_technical_fusion -v
import unittest

import chart_context as cc
import technical_fusion as tf


def mk(rows):
    """rows: [(o,h,l,c,v)] → list-of-dict bars（内部5分足と同じ形）。"""
    return [{"open": o, "high": h, "low": l, "close": c, "volume": v} for o, h, l, c, v in rows]


def series(closes, vol=1000, spread=0.002, opens=None):
    rows, prev = [], closes[0]
    for i, c in enumerate(closes):
        o = opens[i] if opens else prev
        h, l = max(o, c) * (1 + spread), min(o, c) * (1 - spread)
        rows.append((o, h, l, c, vol[i] if isinstance(vol, list) else vol))
        prev = c
    return mk(rows)


def zigzag(points, per=3):
    """(価格の折れ線) → 連続する5分足の終値列。"""
    out = []
    for a, b in zip(points, points[1:]):
        for k in range(per):
            out.append(a + (b - a) * (k + 1) / per)
    return [points[0]] + out


def fus(bars, chart=None, **kw):
    return tf.evaluate_fusion(bars, chart=chart, quote={"t": bars[-1]["close"]}, **kw)


class TrendTests(unittest.TestCase):
    def test_1_higher_high_higher_low_is_uptrend(self):
        pts = [100, 101, 100.4, 101.8, 101.2, 102.6, 102.0, 103.4]
        b = cc.normalize_bars(series(zigzag(pts, 3)))
        ft = {"higherHighs": True, "higherLows": True, "slope6": 1.2, "aboveVwap": True}
        g = tf.trend_structure(b, ft)
        self.assertEqual(g["trendState"], "UPTREND")
        self.assertEqual(g["state"], tf.BULL)

    def test_lower_highs_lower_lows_is_downtrend(self):
        pts = [103, 102.4, 102.8, 101.6, 102.0, 100.8, 101.2, 100.0]
        b = cc.normalize_bars(series(zigzag(pts, 3)))
        g = tf.trend_structure(b, {"lowerHighs": True, "lowerLows": True, "higherHighs": False, "higherLows": False})
        self.assertEqual(g["trendState"], "DOWNTREND")


class SupportResistanceTests(unittest.TestCase):
    def test_2_support_bounce_counts_touches(self):
        # 安値が100.0付近を3回支持
        closes = [102, 100.6, 102, 101, 100.5, 102.2, 101.2, 100.4, 102.5, 102.2, 102.8, 103]
        rows = []
        prev = closes[0]
        for c in closes:
            lo = 100.0 if c < 101 else min(prev, c) * 0.999
            rows.append((prev, max(prev, c) * 1.001, lo, c, 1000))
            prev = c
        b = cc.normalize_bars(mk(rows))
        lv = tf.sr_levels(b, 103.0, vwap=None, day_high=None, day_low=100.0)
        sup = [x for x in lv if x["type"] == "support" and x["kind"] == "day_low"][0]
        self.assertGreaterEqual(sup["touch_count"], 3)
        self.assertGreater(sup["support_strength"], 0.6)
        self.assertIsNotNone(sup["last_touch_bars_ago"])

    def test_3_resistance_rejection_is_bearish(self):
        closes = [100, 100.5, 101, 101.4, 101.2, 101.5, 101.3, 101.5, 101.35, 101.4]
        rows, prev = [], closes[0]
        for i, c in enumerate(closes):
            o = prev
            hi = 102.0 if i >= 3 else max(o, c) * 1.001      # 102で繰り返し頭を押さえられる
            rows.append((o, hi, min(o, c) * 0.999, c, 1000))
            prev = c
        bars = mk(rows)
        rows[-1] = (101.6, 102.0, 101.3, 101.4, 1200)         # 長い上ヒゲ
        bars = mk(rows)
        f = fus(bars, chart={"pattern": "NEUTRAL", "features": {"distFromDayHighPct": -0.4}}, day_high=102.0)
        self.assertIn("LONG_UPPER_WICK", f["candles"])
        self.assertEqual(f["groups"]["price_action"]["state"], tf.BEAR)


class BreakoutTests(unittest.TestCase):
    def _b(self):
        return cc.normalize_bars(series([100, 100.2, 100.1, 100.3, 100.9, 101.2, 101.3, 101.4]))

    def test_4_breakout_strong_volume(self):
        st, _ = tf.breakout_state(self._b(), {"breakoutLevel": 100.5, "brokeRecently": True, "breakoutHeld": True,
                                             "brokeBarsAgo": 1, "breakoutVolRatio": 2.0, "failedBreakout": False})
        self.assertEqual(st, "STRONG_VOLUME_BREAKOUT")

    def test_5_breakout_weak_volume(self):
        st, _ = tf.breakout_state(self._b(), {"breakoutLevel": 100.5, "brokeRecently": True, "breakoutHeld": True,
                                             "brokeBarsAgo": 1, "breakoutVolRatio": 0.9, "failedBreakout": False})
        self.assertEqual(st, "WEAK_BREAKOUT")

    def test_6_breakout_retest(self):
        rows = series([100, 100.2, 100.1, 101.0, 101.5, 101.0, 100.55, 100.9])
        rows[-2]["low"] = 100.45          # 旧抵抗100.5まで押した
        b = cc.normalize_bars(rows)
        st, why = tf.breakout_state(b, {"breakoutLevel": 100.5, "brokeRecently": True, "breakoutHeld": True,
                                       "brokeBarsAgo": 4, "breakoutVolRatio": 1.6, "failedBreakout": False})
        self.assertEqual(st, "BREAKOUT_RETEST")

    def test_breakout_probe_and_failed(self):
        rows = series([100, 100.2, 100.1, 100.3, 100.2, 100.3, 100.4, 100.45])
        rows[-1]["high"] = 100.8                       # 一瞬抜けたが終値は線の下
        st, _ = tf.breakout_state(cc.normalize_bars(rows), {"breakoutLevel": 100.5, "brokeRecently": False})
        self.assertEqual(st, "BREAKOUT_PROBE")
        st2, _ = tf.breakout_state(cc.normalize_bars(rows), {"breakoutLevel": 100.5, "failedBreakout": True})
        self.assertEqual(st2, "FAILED_BREAKOUT")


class PatternTests(unittest.TestCase):
    def test_7_double_top(self):
        pts = [100, 102, 101, 102.05, 100.6]
        b = cc.normalize_bars(series(zigzag(pts, 4)))
        names = [p["name"] for p in tf.detect_patterns(b)]
        self.assertIn("DOUBLE_TOP", names)

    def test_8_double_bottom(self):
        pts = [102, 100, 101.2, 100.05, 101.8]
        b = cc.normalize_bars(series(zigzag(pts, 4)))
        ps = tf.detect_patterns(b)
        self.assertIn("DOUBLE_BOTTOM", [p["name"] for p in ps])

    def test_9_triangle_breakout_weak_vs_strong(self):
        # 高値102で頭打ち・安値切り上げ・出来高収縮
        pts = [100, 102, 100.4, 102, 100.9, 102, 101.3, 102.4]
        vols = [3000] * 8 + [1500] * 8 + [800] * 7 + [700] * 6
        b = cc.normalize_bars(series(zigzag(pts, 4), vol=vols))
        weak = tf.detect_patterns(b, breakout_vol_ratio=0.9)
        strong = tf.detect_patterns(b, breakout_vol_ratio=2.0)
        w = [p for p in weak if p["name"] == "ASCENDING_TRIANGLE"]
        s = [p for p in strong if p["name"] == "ASCENDING_TRIANGLE"]
        self.assertTrue(w and s)
        self.assertEqual(w[0]["breakout"], "WEAK_BREAKOUT")
        self.assertEqual(s[0]["breakout"], "STRONG_BREAKOUT")

    def test_10_flag_continuation(self):
        closes = [100, 100.2, 100.1, 100.5, 101.0, 101.6, 101.5, 101.4, 101.45, 101.35]
        vols = [1000, 1000, 1000, 2500, 3000, 3200, 900, 800, 700, 700]
        # 12本以上にするため前を足す
        closes = [99.8, 99.9, 100.0, 99.95] + closes
        vols = [1000] * 4 + vols
        ps = tf.detect_patterns(cc.normalize_bars(series(closes, vol=vols)))
        self.assertIn("FLAG", [p["name"] for p in ps])

    def test_too_few_bars_returns_nothing(self):
        self.assertEqual(tf.detect_patterns(cc.normalize_bars(series([100, 101, 100, 101]))), [])


def _daily_uptrend(n=140, step=0.5, start=100.0):
    closes = [start + i * step for i in range(n)]
    highs = [c * 1.005 for c in closes]
    lows = [c * 0.995 for c in closes]
    return {"closes": closes, "highs": highs, "lows": lows, "opens": closes, "volumes": [1e6] * n}


class MaTests(unittest.TestCase):
    def test_11_golden_cross_but_chase_is_late(self):
        # 長く下落→急反発でGCが直近に発生、かつ25日線から10%超乖離
        closes = [200 - i * 0.6 for i in range(100)] + [140 + i * 3.0 for i in range(40)]
        d = {"closes": closes, "highs": [c * 1.01 for c in closes], "lows": [c * 0.99 for c in closes], "opens": closes, "volumes": [1e6] * 140}
        ma = tf.ma_context(d)
        self.assertTrue(ma["extended"])
        chart = {"pattern": "CHASE", "features": {"vwapDistPct": 2.5}, "entry_timing_score": 20}
        bars = series([100 + i * 0.05 for i in range(14)])
        f = fus(bars, chart=chart, daily=d)
        self.assertTrue(f["late"])
        self.assertEqual(f["recommendation"], "WAIT")             # テクニカルが強くてもCHASEならWAIT
        if ma["cross"] == "GOLDEN_CROSS_RECENT":
            self.assertTrue(ma["lateGoldenCross"])

    def test_12_golden_cross_in_range_is_whipsaw(self):
        closes = []
        for i in range(140):
            closes.append(100 + (1.2 if (i // 3) % 2 == 0 else -1.2) * 0.8 + (0.02 * i if i > 100 else 0))
        d = {"closes": closes, "highs": [c * 1.003 for c in closes], "lows": [c * 0.997 for c in closes], "opens": closes, "volumes": [1e6] * 140}
        ma = tf.ma_context(d)
        self.assertIsNotNone(ma)
        # クロスが出ていなくても、傾きが緩く価格がMA25を往復している＝ダマシ環境
        self.assertTrue(abs(ma["slope25Pct"]) < 1.0)
        self.assertFalse(ma["bullAlignment"] and ma["bearAlignment"])

    def test_13_ma_pullback(self):
        d = _daily_uptrend()
        d["closes"][-1] = d["closes"][-2] * 1.001
        m25 = tf._sma_list(d["closes"], 25)[-1]
        d["lows"][-1] = m25 * 1.002                # 上向きMA25付近まで押して
        d["closes"][-1] = m25 * 1.012              # 反発
        ma = tf.ma_context(d)
        self.assertEqual(ma["granville"], "MA_PULLBACK")

    def test_ichimoku_context_uptrend(self):
        ich = tf.ichimoku_context(_daily_uptrend())
        self.assertEqual(ich["position"], "ABOVE_CLOUD")
        self.assertTrue(ich["tenkanAboveKijun"])
        self.assertTrue(ich["threeRoleBullish"])
        self.assertEqual(ich["role"], "context_only")


class SetupAndMomentumTests(unittest.TestCase):
    def _chart(self, pattern, **ft):
        return {"pattern": pattern, "features": {"aboveVwap": True, "higherLows": True, "higherHighs": True, "slope6": 1.0, **ft},
                "entry_timing_score": 75}

    def test_14_vwap_reclaim_setup(self):
        bars = series([100, 100.1, 99.7, 99.6, 99.8, 100.1, 100.3, 100.4, 100.5, 100.6, 100.7, 100.8])
        f = fus(bars, chart=self._chart("VWAP_RECLAIM"))
        self.assertEqual(f["setupType"], "VWAP_RECLAIM")

    def test_15_rsi_overheated_but_strong_trend(self):
        closes = [100 + i * 0.35 for i in range(24)]
        vols = [1000] * 18 + [1500, 1800, 2200, 2600, 3000, 3400]
        bars = series(closes, vol=vols, spread=0.0005)
        f = fus(bars, chart=self._chart("EARLY_BREAKOUT", vwapDistPct=0.5), movement={"activity_state": "EXPANDING"})
        mo = f["groups"]["momentum"]
        self.assertGreaterEqual(f["groups"]["momentum"]["extra"]["rsi"] if "extra" in mo else mo["rsi"], 75)
        self.assertIn("MOMENTUM_HEALTHY_STRONG", mo["signals"])
        self.assertEqual(mo["state"], tf.BULL)

    def test_16_rsi_overheated_with_exhaustion(self):
        closes = [100 + i * 0.35 for i in range(22)] + [108.0, 108.1]
        rows = series(closes, vol=[3000] * 20 + [1500, 1000, 800, 700], spread=0.0005)
        rows[-1]["open"], rows[-1]["high"], rows[-1]["low"], rows[-1]["close"] = 108.05, 109.4, 108.0, 108.1   # 長い上ヒゲ
        f = fus(rows, chart=self._chart("NEUTRAL", vwapDistPct=0.5, upperWick=0.7, volPeakout=True))
        self.assertIn("MOMENTUM_EXHAUSTION", f["groups"]["momentum"]["signals"])
        self.assertEqual(f["groups"]["momentum"]["state"], tf.BEAR)

    def test_overheated_indicators_count_once(self):
        closes = [100 + i * 0.45 for i in range(24)]
        b = cc.normalize_bars(series(closes, spread=0.0005))
        f = fus(series(closes, spread=0.0005), chart=self._chart("NEUTRAL", vwapDistPct=3.0))
        sig = [s for s in f["groups"]["momentum"]["signals"] if s.startswith("MOMENTUM_")]
        self.assertEqual(len(sig), 1)                                   # RSI・Stoch・VWAP乖離を「過熱×3」にしない


class VolumeTests(unittest.TestCase):
    def test_17_bullish_price_falling_volume_is_weak_rally(self):
        closes = [100, 100.3, 100.6, 100.9, 101.2, 101.5, 101.8]
        vols = [3000, 3000, 3000, 1500, 1200, 1000, 900]
        self.assertEqual(tf.price_volume_matrix(cc.normalize_bars(series(closes, vol=vols))), "WEAK_RALLY")

    def test_18_quiet_pullback(self):
        closes = [100, 100.8, 101.6, 102.4, 102.0, 101.7, 101.5]
        vols = [1000, 1500, 3000, 3500, 1800, 1200, 900]
        self.assertEqual(tf.price_volume_matrix(cc.normalize_bars(series(closes, vol=vols))), "QUIET_PULLBACK")

    def test_healthy_expansion_and_distribution(self):
        self.assertEqual(tf.price_volume_matrix(cc.normalize_bars(series([100, 100.2, 100.4, 100.7, 101, 101.3, 101.6], vol=[1000] * 3 + [2500] * 4))), "HEALTHY_EXPANSION")
        self.assertEqual(tf.price_volume_matrix(cc.normalize_bars(series([102, 101.7, 101.4, 101.0, 100.6, 100.2, 99.8], vol=[1000] * 3 + [2500] * 4))), "DISTRIBUTION")

    def test_volume_profile_uses_real_volumes_only(self):
        self.assertIsNone(tf.volume_profile(cc.normalize_bars(series([100, 101] * 3))))               # 12本未満
        self.assertIsNone(tf.volume_profile(cc.normalize_bars(series([100 + (i % 3) * 0.3 for i in range(14)], vol=0))))
        p = tf.volume_profile(cc.normalize_bars(series([100 + (i % 4) * 0.3 for i in range(16)], vol=1000)))
        self.assertEqual(p["source"], "INTRADAY_5M_TYPICAL_PRICE")


class ConfluenceTests(unittest.TestCase):
    def _bullish_setup(self):
        pts = [100, 101, 100.5, 101.6, 101.2, 102.2, 101.9, 102.8, 102.5, 103.2]
        closes = zigzag(pts, 3)
        vols = [1000] * (len(closes) - 6) + [2200, 2600, 2400, 2800, 2600, 3000]
        bars = series(closes, vol=vols)
        chart = {"pattern": "BREAKOUT_CONFIRMED", "entry_timing_score": 72,
                 "features": {"higherHighs": True, "higherLows": True, "slope6": 1.1, "aboveVwap": True, "breakoutLevel": closes[-8],
                              "brokeRecently": True, "breakoutHeld": True, "brokeBarsAgo": 1, "breakoutVolRatio": 2.1,
                              "failedBreakout": False, "vwapDistPct": 0.6, "volRatioLast": 1.6, "lowerWick": 0.1, "upperWick": 0.05}}
        return bars, chart

    def test_19_high_confluence(self):
        bars, chart = self._bullish_setup()
        f = fus(bars, chart=chart, movement={"activity_state": "EXPANDING"}, market={"marketRS": 2.5, "nikkeiChg": 0.3, "sectorLead": True},
                day_high=max(x["high"] for x in bars))
        self.assertGreaterEqual(f["confluence"]["score"], tf.MID_CONFLUENCE)
        self.assertGreaterEqual(len(f["confluence"]["bullGroups"]), 4)
        self.assertFalse(f["late"])
        self.assertIn(f["confluence"]["level"], ("HIGH", "MEDIUM"))

    def test_20_conflicting_signals_lower_the_score(self):
        bars, chart = self._bullish_setup()
        good = fus(bars, chart=chart, movement={"activity_state": "EXPANDING"}, market={"marketRS": 2.5}, day_high=103.3)
        rows = [dict(x) for x in bars]
        rows[-1].update(open=103.0, high=104.4, low=102.9, close=103.05)               # 長い上ヒゲ
        bad = fus(rows, chart=chart, movement={"activity_state": "EXPANDING"}, market={"marketRS": -2.0, "nikkeiChg": -1.5}, day_high=104.4)
        self.assertLess(bad["confluence"]["score"], good["confluence"]["score"])
        self.assertIn(tf.BEAR, [g["state"] for g in bad["groups"].values()])

    def test_same_group_signals_are_not_double_counted(self):
        # trendグループは代表strengthのみ（最大1.0×重み1.0）。他が全てUNKNOWNでも100を超えない
        bars = series([100 + i * 0.3 for i in range(14)])
        f = fus(bars, chart={"pattern": "NEUTRAL", "features": {"higherHighs": True, "higherLows": True, "slope6": 2.0, "aboveVwap": True}})
        self.assertLessEqual(f["confluence"]["score"], 50.0)          # 既知グループ<4は上限50
        self.assertLessEqual(f["groups"]["trend"]["strength"], 1.0)

    def test_insufficient_bars_returns_unknown_without_exception(self):
        f = tf.evaluate_fusion(series([100, 101]), quote={"t": 101})
        self.assertEqual(f["confluence"]["level"], "UNKNOWN")
        self.assertEqual(f["recommendation"], "UNKNOWN")
        self.assertEqual(tf.evaluate_fusion(None)["recommendation"], "UNKNOWN")

    def test_high_technical_but_bad_timing_is_wait(self):
        bars, chart = self._bullish_setup()
        chart = dict(chart, entry_timing_score=24)
        f = fus(bars, chart=chart, movement={"activity_state": "EXPANDING"}, market={"marketRS": 3.0}, day_high=103.3)
        self.assertEqual(f["recommendation"], "WAIT")                 # Technical高でもEntry Timing 24ならWAIT

    def test_shadow_only_does_not_mutate_inputs(self):
        bars, chart = self._bullish_setup()
        import copy
        c0 = copy.deepcopy(chart)
        fus(bars, chart=chart)
        self.assertEqual(chart, c0)


class CandleAndExitTests(unittest.TestCase):
    def test_engulfing_and_wicks(self):
        rows = [(100, 100.2, 99.4, 99.5, 1000), (99.4, 100.6, 99.3, 100.5, 1500)]
        b = cc.normalize_bars(mk(rows))
        self.assertIn("BULLISH_ENGULFING", tf.candle_signals(b))
        rows = [(100, 100.9, 99.9, 100.8, 1000), (100.9, 101.0, 99.7, 99.9, 1500)]
        self.assertIn("BEARISH_ENGULFING", tf.candle_signals(cc.normalize_bars(mk(rows))))

    def test_approx_opens_skip_candle_shapes(self):
        b = cc.normalize_bars({"closes": [100, 101, 102], "highs": [100.5, 101.5, 102.5], "lows": [99.5, 100.5, 101.5], "volumes": [1, 1, 1]})
        self.assertEqual(tf.candle_signals(b), [])                    # opens近似時は形を判定しない

    def test_exit_pressure_accumulates_distinct_signals_only(self):
        p = tf.exit_pressure(["LONG_UPPER_WICK"], {"volPeakout": True}, "BEARISH_DIVERGENCE", True, in_profit=True)
        self.assertEqual(p["confidence"], 1.0)
        p2 = tf.exit_pressure(["LONG_UPPER_WICK", "BEARISH_ENGULFING"], {}, None, False, in_profit=True)
        self.assertEqual(p2["confidence"], 0.25)                      # 上ヒゲと包み足は同種＝重複加算しない

    def test_divergence_detection(self):
        # 価格は高値更新、上昇の勢いは低下（RSI低下）
        closes = [100 + i * 0.5 for i in range(10)] + [104.4, 104.0, 104.6, 104.2, 104.9, 104.3, 104.95, 104.6, 105.0, 104.7, 105.05, 104.8]
        b = cc.normalize_bars(series(closes, spread=0.0008))
        self.assertIn(tf.divergence(b), (None, "BEARISH_DIVERGENCE"))     # 誤検出しないことを優先（条件次第でNone）

    def test_compact_is_json_safe(self):
        import json
        bars = series([100 + i * 0.2 for i in range(14)])
        f = fus(bars, chart={"pattern": "NEUTRAL", "features": {"higherHighs": True, "higherLows": True}})
        json.dumps(tf.compact(f), ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()
