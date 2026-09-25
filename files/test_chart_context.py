# Chart Context Engine（Phase C）のテスト。実行： cd files && python -m unittest test_chart_context -v
#
# 必須10ケース（early breakout / breakout confirmed / pullback ready / VWAP reclaim / chase /
# failed breakout / exhaustion / higher low / VWAP loss / insufficient bars）＋
# 「同じ現在値でも過去12本の形が違えばENTRY判定が変わる」＋強さ/タイミングの分離・ゲート・
# Learning Rule連携・未来データ非参照・軽量再スコアとの一致。

import unittest

import chart_context as cc


def mk(rows):
    """rows: [(open, high, low, close, volume), ...]（最新が末尾）→ dict-of-arrays。"""
    return {"opens": [r[0] for r in rows], "highs": [r[1] for r in rows], "lows": [r[2] for r in rows],
            "closes": [r[3] for r in rows], "volumes": [r[4] for r in rows]}


def bar(prev_close, close, vol=1000, wu=0.0, wl=0.0):
    o = prev_close
    return (o, max(o, close) + wu, min(o, close) - wl, close, vol)


def series(start, closes, vols=None, wus=None, wls=None):
    rows, prev = [], start
    for i, c in enumerate(closes):
        rows.append(bar(prev, c, (vols or [1000] * len(closes))[i], (wus or [0] * len(closes))[i], (wls or [0] * len(closes))[i]))
        prev = c
    return rows


# ---- シナリオ（現在値はすべて1016前後に揃えられるものは揃える）
def early_breakout():
    # 1000〜1005のレンジで安値切り上げ、出来高が増えながら水準(1005.2)の直下へ
    closes = [1000, 1001, 1000.5, 1002, 1001.5, 1003, 1002.5, 1004, 1003.5, 1004.6, 1004.8, 1005.0]
    highs = [1002, 1002.5, 1002, 1003, 1003, 1004, 1004, 1005.2, 1005, 1005.1, 1005.15, 1005.3]
    rows = []
    prev = 1000
    vols = [900, 800, 800, 900, 800, 900, 800, 900, 800, 1000, 1200, 1500]
    for i, c in enumerate(closes):
        lo = min(prev, c) - 0.3 + 0.05 * i
        rows.append((prev, max(highs[i], c), lo, c, vols[i]))
        prev = c
    return mk(rows)


def breakout_confirmed():
    rows = early_breakout()
    r = [(rows["opens"][i], rows["highs"][i], rows["lows"][i], rows["closes"][i], rows["volumes"][i]) for i in range(len(rows["closes"]))]
    r.append((1005.0, 1006.9, 1004.9, 1006.6, 2400))   # 水準を上抜け（出来高増）
    r.append((1006.6, 1007.4, 1006.3, 1007.0, 1300))   # 定着
    return mk(r)


def pullback_ready(cur=1016.0):
    closes = [1000, 1001, 1003, 1005, 1008, 1010, 1012, 1014, 1016.5, 1014.5, 1013.5, cur]
    vols = [1000, 1100, 1200, 1300, 1400, 1500, 1500, 1600, 1700, 500, 400, 900]
    rows = series(1000, closes, vols)
    # 直近: 押し(赤2本)→下ヒゲ付きで反発の陽線
    rows[-1] = (1013.5, cur + 0.3, 1012.4, cur, 900)
    return mk(rows)


def vwap_reclaim():
    closes = [1000, 999.5, 999, 998.5, 998, 997.5, 997.8, 998.5, 999.6, 1000.6]
    vols = [1500, 1400, 1300, 1000, 900, 900, 1000, 1200, 1600, 1900]
    return mk(series(1000, closes, vols))


def chase(cur=1016.0):
    closes = [1000] * 9 + [1004, 1010, cur]
    rows = series(1000, closes, [800] * 9 + [1500, 2200, 1200])
    rows[-1] = (1010, cur + 8, 1009.5, cur, 1200)       # 直近足に長い上ヒゲ
    return mk(rows)


def failed_breakout():
    closes = [1000, 1001, 1002, 1003, 1004, 1004.5, 1004.8, 1005.0, 1006.0, 1003.0]
    rows = series(1000, closes, [900, 900, 900, 900, 900, 900, 900, 900, 2000, 700])
    rows[-2] = (1005.0, 1010.0, 1004.9, 1006.0, 2000)   # 上抜け→長い上ヒゲ
    rows[-1] = (1006.0, 1007.0, 1002.5, 1003.0, 700)    # 次足は高値を切り下げ、水準を割る
    return mk(rows)


def exhaustion():
    closes = [1000, 1003, 1007, 1012, 1017, 1022, 1026, 1029, 1030, 1030.5, 1030.6, 1030.8]
    vols = [1000, 1400, 2000, 2600, 3200, 3600, 3000, 2200, 1500, 1000, 800, 700]
    wus = [0, 0, 0, 0, 0, 0.5, 1.0, 1.6, 2.2, 2.6, 2.8, 3.0]
    return mk(series(1000, closes, vols, wus))


def vwap_loss():
    closes = [1000, 1002, 1004, 1005, 1005.5, 1005, 1003.5, 1001, 999.5]
    vols = [1000, 1200, 1300, 1200, 1000, 1000, 1200, 1500, 1600]
    return mk(series(1000, closes, vols))


def higher_low_series():
    closes = [1000, 998.5, 1000.5, 999.5, 1002, 1001, 1003.5, 1002.5, 1005, 1004]
    rows = series(1000, closes)
    lows = [998, 997.5, 998.5, 998.3, 999.5, 999.6, 1000.8, 1001.0, 1002.5, 1002.6]
    return mk([(r[0], r[1], lows[i], r[3], r[4]) for i, r in enumerate(rows)])


class PatternTests(unittest.TestCase):
    def ev(self, bars, **kw):
        return cc.evaluate_chart_context(bars, **kw)

    def test01_early_breakout(self):
        r = self.ev(early_breakout())
        self.assertEqual(r["pattern"], "EARLY_BREAKOUT", r)
        self.assertGreaterEqual(r["entry_timing_score"], 70)
        self.assertEqual(r["confidence"], "HIGH")

    def test02_breakout_confirmed(self):
        r = self.ev(breakout_confirmed())
        self.assertEqual(r["pattern"], "BREAKOUT_CONFIRMED", r)

    def test03_pullback_ready(self):
        r = self.ev(pullback_ready())
        self.assertEqual(r["pattern"], "PULLBACK_READY", r)
        self.assertGreaterEqual(r["entry_timing_score"], 70)

    def test04_vwap_reclaim(self):
        r = self.ev(vwap_reclaim())
        self.assertEqual(r["pattern"], "VWAP_RECLAIM", r)

    def test05_chase(self):
        r = self.ev(chase())
        self.assertEqual(r["pattern"], "CHASE", r)
        self.assertLessEqual(r["entry_timing_score"], 35)
        self.assertTrue(any("上ヒゲ" in x for x in r["penalties"]))

    def test06_failed_breakout(self):
        r = self.ev(failed_breakout())
        self.assertEqual(r["pattern"], "FAILED_BREAKOUT", r)
        self.assertLessEqual(r["entry_timing_score"], 30)

    def test07_exhaustion(self):
        r = self.ev(exhaustion())
        self.assertEqual(r["pattern"], "EXHAUSTION", r)

    def test08_higher_low(self):
        f = cc.compute_features(higher_low_series())
        self.assertTrue(f["higherLows"])
        self.assertFalse(f["lowerLows"])
        r = self.ev(higher_low_series())
        self.assertTrue(any("安値切り上げ" in x for x in r["reasons"]))

    def test09_vwap_loss(self):
        r = self.ev(vwap_loss())
        self.assertEqual(r["pattern"], "VWAP_LOSS", r)
        self.assertLess(r["entry_timing_score"], 40)

    def test10_insufficient_bars(self):
        two = mk(series(1000, [1001, 1002]))
        r = self.ev(two)
        self.assertEqual((r["pattern"], r["confidence"], r["entry_timing_score"]), ("UNKNOWN", "UNKNOWN", None))
        four = self.ev(mk(series(1000, [1001, 1002, 1003, 1004])))
        self.assertEqual(four["confidence"], "LOW")
        six = self.ev(mk(series(1000, [1001, 1002, 1003, 1004, 1005, 1006])))
        self.assertEqual(six["confidence"], "MEDIUM")
        self.assertEqual(cc.confidence_for(12), "HIGH")
        self.assertEqual(cc.confidence_for(2), "UNKNOWN")


class SameCurrentPriceDifferentShapeTests(unittest.TestCase):
    def test_same_price_different_history_changes_entry_decision(self):
        a_bars, b_bars = pullback_ready(1016.0), chase(1016.0)
        self.assertEqual(a_bars["closes"][-1], b_bars["closes"][-1])  # 現在値は同じ
        a = cc.evaluate_chart_context(a_bars)
        b = cc.evaluate_chart_context(b_bars)
        strength = 75  # 銘柄の強さは同じ
        self.assertEqual(a["pattern"], "PULLBACK_READY")
        self.assertEqual(b["pattern"], "CHASE")
        self.assertEqual(cc.entry_decision(strength, a), "ENTRY_READY")
        self.assertEqual(cc.entry_decision(strength, b), "NO_ENTRY_CHASE")
        # 既存stateがENTRY_READYでも、Bはチャート判定で格下げされ、Aは維持される
        self.assertEqual(cc.apply_chart_gate("ENTRY_READY", strength, 70, a)[0], "ENTRY_READY")
        self.assertEqual(cc.apply_chart_gate("ENTRY_READY", strength, 70, b)[0], "WAIT_PULLBACK")
        self.assertGreater(a["entry_timing_score"], b["entry_timing_score"] + 40)

    def test_chase_reasons_are_explained(self):
        b = cc.evaluate_chart_context(chase())
        joined = " ".join(b["reasons"] + b["penalties"])
        self.assertIn("15分で", joined)
        self.assertIn("上ヒゲ", joined)


class GateAndDecisionTests(unittest.TestCase):
    def test_existing_breakout_true_but_chase_never_entry_ready(self):
        b = cc.evaluate_chart_context(chase(), existing_signals={"structure": "higher_highs", "recentHighBreak": True})
        state, why = cc.apply_chart_gate("NOW_BUYABLE", 90, 90, b)
        self.assertEqual(state, "WAIT_PULLBACK")
        self.assertTrue(why)

    def test_failed_breakout_blocks_entry(self):
        f = cc.evaluate_chart_context(failed_breakout())
        self.assertEqual(cc.apply_chart_gate("ENTRY_READY", 80, 80, f)[0], "WATCH")
        self.assertEqual(cc.entry_decision(80, f), "NO_ENTRY_FAILED_BREAK")

    def test_weak_existing_pullback_upgraded_by_higher_low_vwap_bounce_wick(self):
        r = cc.evaluate_chart_context(pullback_ready())
        state, _ = cc.apply_chart_gate("WAIT_PULLBACK", 70, 60, r)
        self.assertEqual(state, "ENTRY_READY")
        # 寄り直後は昇格させない／データ不足は昇格させない
        early = cc.evaluate_chart_context(pullback_ready(), minutes_since_open=8)
        self.assertEqual(cc.apply_chart_gate("WAIT_PULLBACK", 70, 60, early)[0], "WAIT_PULLBACK")
        self.assertEqual(cc.apply_chart_gate("WAIT_PULLBACK", 70, 60, cc.evaluate_chart_context(mk(series(1000, [1001, 1002]))))[0], "WAIT_PULLBACK")

    def test_unknown_chart_caps_now_buyable(self):
        u = cc.evaluate_chart_context(mk(series(1000, [1001, 1002])))
        self.assertEqual(cc.apply_chart_gate("NOW_BUYABLE", 90, 90, u)[0], "ENTRY_READY")

    def test_strong_stock_bad_timing_is_strong_but_wait(self):
        b = cc.evaluate_chart_context(chase())
        self.assertEqual(cc.entry_decision(95, b), "NO_ENTRY_CHASE")
        n = cc.evaluate_chart_context(mk(series(1000, [1000] * 12, [800] * 12)))
        self.assertIn(cc.entry_decision(95, n), ("WAIT_BREAKOUT", "WAIT_PULLBACK", "WATCH"))

    def test_other_states_untouched(self):
        b = cc.evaluate_chart_context(chase())
        for s in ("WEAK", "CHASE_RISK", "INVALID", "PROVISIONAL", "WATCH"):
            self.assertEqual(cc.apply_chart_gate(s, 90, 90, b)[0], s)

    def test_strength_excludes_timing_components(self):
        comp = {"momentum": 20, "marketRelative": 15, "volume": 10, "autoRs": 10, "autoSector": 5, "catalyst": 5,
                "vwap": 15, "fiveMinStructure": 15, "overheat": -10, "rangeBonus": 3}
        self.assertEqual(cc.stock_strength_score(comp), 100)
        comp2 = dict(comp, vwap=0, fiveMinStructure=0, overheat=-10)   # タイミング側だけ悪化
        self.assertEqual(cc.stock_strength_score(comp2), 100)


class EarlySessionTests(unittest.TestCase):
    def test_early_session_lowers_confidence(self):
        bars = pullback_ready()
        self.assertEqual(cc.evaluate_chart_context(bars)["confidence"], "HIGH")
        e = cc.evaluate_chart_context(bars, minutes_since_open=10)
        self.assertEqual(e["confidence"], "MEDIUM")
        self.assertTrue(e["earlySession"])
        self.assertEqual(cc.evaluate_chart_context(bars, minutes_since_open=40)["confidence"], "HIGH")


class LearningRulePenaltyTests(unittest.TestCase):
    RULES = [{"id": 1, "rule_text": "上昇確認後に入ると高値掴みになりやすい。高値追いはしない", "status": "TESTING", "confidence": "MEDIUM"},
             {"id": 2, "rule_text": "ブレイク失敗（ダマシ）の直後は入らない", "status": "ACTIVE", "confidence": "HIGH"},
             {"id": 3, "rule_text": "基本は持ち越さない", "status": "ACTIVE", "confidence": "HIGH"}]

    def test_penalties_derived_from_db_rule_text_not_hardcoded(self):
        p = cc.derive_rule_penalties(self.RULES)
        self.assertEqual(p["byPattern"]["CHASE"], 3)
        self.assertEqual(p["byPattern"]["FAILED_BREAKOUT"], 5)
        self.assertNotIn("EARLY_BREAKOUT", p["byPattern"])
        self.assertEqual({s["ruleId"] for s in p["sources"]}, {1, 2})

    def test_penalty_applied_to_matching_pattern_only(self):
        p = cc.derive_rule_penalties(self.RULES)
        base = cc.evaluate_chart_context(chase())["entry_timing_score"]
        pen = cc.evaluate_chart_context(chase(), rule_penalties=p)
        self.assertLessEqual(pen["entry_timing_score"], base)
        self.assertTrue(any("Learning Rule" in x for x in pen["penalties"]))
        good = cc.evaluate_chart_context(pullback_ready())["entry_timing_score"]
        self.assertEqual(cc.evaluate_chart_context(pullback_ready(), rule_penalties=p)["entry_timing_score"], good)

    def test_no_rules_no_penalty(self):
        self.assertEqual(cc.derive_rule_penalties([]), {"byPattern": {}, "sources": []})


class NoFutureDataTests(unittest.TestCase):
    def test_result_depends_only_on_bars_up_to_now(self):
        full = pullback_ready()
        upto = {k: v[:-1] for k, v in full.items()}
        a = cc.evaluate_chart_context(upto)
        # 未来の足を追加しても、過去時点までの入力に対する結果は不変（純粋関数）
        extended = {k: v + [v[-1]] for k, v in full.items()}
        self.assertEqual(cc.evaluate_chart_context(upto), a)
        self.assertNotEqual(cc.evaluate_chart_context(extended)["barCount"], a["barCount"])

    def test_latest_quote_updates_only_last_bar(self):
        bars = pullback_ready()
        r0 = cc.evaluate_chart_context(bars)
        r1 = cc.evaluate_chart_context(bars, quote={"t": 1016.0})
        self.assertEqual(r0["pattern"], r1["pattern"])
        self.assertEqual(r1["barCount"], r0["barCount"])


class BarFormatTests(unittest.TestCase):
    def test_accepts_internal_bar_list_and_missing_opens(self):
        lst = [{"open": o, "high": h, "low": l, "close": c, "volume": v} for (o, h, l, c, v) in pullback_ready()["opens"] and
               zip(*[pullback_ready()[k] for k in ("opens", "highs", "lows", "closes", "volumes")])]
        self.assertEqual(cc.evaluate_chart_context(lst)["pattern"], "PULLBACK_READY")
        no_open = {k: v for k, v in pullback_ready().items() if k != "opens"}
        self.assertIn("pattern", cc.evaluate_chart_context(no_open))
        self.assertIsNone(cc.normalize_bars(None))


if __name__ == "__main__":
    unittest.main()
