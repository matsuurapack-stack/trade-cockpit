# Chart Context Engine と TOP5（フルスキャン／軽量再スコア共通の
# _compute_price_dependent_entry_fields）の統合テスト。実行： cd files && python -m unittest test_chart_context_integration -v

import unittest
from unittest import mock

import server
import chart_context as cc
from test_chart_context import chase, pullback_ready, early_breakout
from test_entry_top5_rescore import _ctx, _shared, _quote

K = 2.3  # 合成足（1000円台）を2,300円台へ拡大


def scaled(bars, k=K):
    return {key: [x * k for x in vals] if key != "volumes" else list(vals) for key, vals in bars.items()}


def strong_ctx(price):
    """銘柄自体は強い（上昇率+5%・対市場+5pt・出来高3倍・VWAP上・5分足高値切り上げ）状態。"""
    ctx = _ctx(price=price, vwap=price * 0.99, recent_high=price * 1.1)
    ctx["row"].update({"changePct": 5.0, "marketRS": 5.0})
    ctx["stage2"]["timeAdjustedVolumeRatio"] = 3.0
    return ctx


def fields(ctx, bars, shared=None):
    row = dict(ctx["row"])
    return server._compute_price_dependent_entry_fields(
        "5301", ctx["w"], row, ctx["stage2"], ctx["snapshot"], bars, [], [], None, "FULL", shared or _shared(), [])


class SameCurrentPriceTests(unittest.TestCase):
    def test_same_price_different_shape_changes_entry_state(self):
        a_bars, b_bars = scaled(pullback_ready(1016.0)), scaled(chase(1016.0))
        price = a_bars["closes"][-1]
        self.assertAlmostEqual(price, b_bars["closes"][-1], places=6)   # 現在値は同じ
        ctx = strong_ctx(price)
        a = fields(ctx, a_bars)
        b = fields(ctx, b_bars)
        self.assertLessEqual(abs(a["entry_score"] - b["entry_score"]), 3)   # 既存の総合スコアはほぼ同一（値幅余地の小差のみ）
        self.assertEqual(a["stock_strength_score"], b["stock_strength_score"])  # 銘柄の強さも同一
        self.assertIn(a["entry_state_pre_chart"], ("NOW_BUYABLE", "ENTRY_READY"))
        self.assertIn(b["entry_state_pre_chart"], ("NOW_BUYABLE", "ENTRY_READY"))  # 既存判定は両方ENTRY可
        self.assertIn(a["entry_state"], ("NOW_BUYABLE", "ENTRY_READY"))     # A：じわじわ→押し目→再上昇
        self.assertEqual(b["entry_state"], "WAIT_PULLBACK")                 # B：急騰→上ヒゲ→CHASE
        self.assertEqual(a["chart_context"]["pattern"], "PULLBACK_READY")
        self.assertEqual(b["chart_context"]["pattern"], "CHASE")
        self.assertEqual(b["entry_decision"], "NO_ENTRY_CHASE")
        self.assertGreater(a["entry_timing_score"], b["entry_timing_score"] + 40)
        # 強い銘柄でもENTRYタイミングが悪ければ STOCK STRONG / ENTRY WAIT（理由が表示される）
        self.assertGreaterEqual(b["stock_strength_score"], 50)
        self.assertTrue(any("CHASE" in r or "走りすぎ" in r for r in b["risks"]))

    def test_existing_breakout_signal_true_but_chase_is_not_entry_ready(self):
        b = scaled(chase(1016.0))
        ctx = strong_ctx(b["closes"][-1])
        ctx["stage2"]["aboveRecentHigh"] = True   # 既存のBREAKOUT系signalが成立していても
        ctx["stage2"]["distanceFromHighPct"] = 0.5
        f = fields(ctx, b)
        self.assertNotIn(f["entry_state"], ("NOW_BUYABLE", "ENTRY_READY"))

    def test_legacy_result_keys_unchanged(self):
        f = fields(strong_ctx(2300.0), scaled(pullback_ready()))
        for k in ("range_metrics", "comp", "entry_score", "entry_state", "reasons", "risks", "resilience"):
            self.assertIn(k, f)

    def test_no_chart_bars_never_gives_strong_entry(self):
        ctx = strong_ctx(2300.0)
        f = fields(ctx, None)   # 5分足なし
        self.assertEqual(f["chart_context"]["confidence"], "UNKNOWN")
        self.assertNotEqual(f["entry_state"], "NOW_BUYABLE")
        self.assertIsNone(f["entry_timing_score"])


class RescoreParityTests(unittest.TestCase):
    def test_light_rescore_includes_chart_fields_and_matches_scan(self):
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
        self.assertEqual(light["chartPattern"], full["chart_context"]["pattern"])
        self.assertEqual(light["entryTimingScore"], full["entry_timing_score"])
        self.assertEqual(light["entryState"], full["entry_state"])
        self.assertEqual(light["stockStrengthScore"], full["stock_strength_score"])
        self.assertIn("chartContext", light)

    def test_rescore_prefers_internal_bars_when_more_complete(self):
        ctx_bars = {"closes": [1, 2, 3], "highs": [1, 2, 3], "lows": [1, 2, 3], "volumes": [1, 1, 1]}
        internal = [{"open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}] * 12
        with mock.patch.object(server, "get_internal_intraday_bars", return_value={"bars": internal}):
            self.assertIs(server._chart_bars_for_rescore("5301", ctx_bars), internal)
        with mock.patch.object(server, "get_internal_intraday_bars", return_value={"bars": internal[:2]}):
            self.assertIs(server._chart_bars_for_rescore("5301", ctx_bars), ctx_bars)

    def test_rescore_uses_only_latest_quote_and_bars_no_heavy_fetch(self):
        ctx = strong_ctx(2300.0)
        ctx["bars"] = scaled(pullback_ready())
        with mock.patch.object(server, "get_internal_intraday_bars", return_value={"bars": []}), \
                mock.patch.object(server, "_volume_stage2_detail", side_effect=AssertionError("heavy")), \
                mock.patch.object(server, "_cached_daily_arrays", side_effect=AssertionError("heavy")):
            server.rescore_entry_candidate_with_quote(ctx, _shared(), _quote(2310.0))


class TimeOfDayTests(unittest.TestCase):
    def test_minutes_since_open(self):
        import datetime
        jst = server._JST
        self.assertEqual(server._minutes_since_open(datetime.datetime(2026, 9, 25, 9, 10, tzinfo=jst)), 10)
        self.assertEqual(server._minutes_since_open(datetime.datetime(2026, 9, 25, 10, 30, tzinfo=jst)), 90)
        self.assertIsNone(server._minutes_since_open(datetime.datetime(2026, 9, 25, 13, 0, tzinfo=jst)))

    def test_early_session_blocks_upgrade_in_full_pipeline(self):
        # 既存はWAIT_PULLBACK（強さが足りずNOW/READYにならない）→ 通常時はチャートで昇格、寄り直後は昇格しない
        bars = scaled(pullback_ready(1016.0))
        ctx = _ctx(price=bars["closes"][-1], vwap=bars["closes"][-1] * 0.99, recent_high=bars["closes"][-1] * 1.1)
        ctx["row"].update({"changePct": 5.0, "marketRS": 3.0})
        ctx["snapshot"]["fiveMinStructure"] = "mixed"
        ctx["stage2"]["timeAdjustedVolumeRatio"] = 3.0
        shared_normal = dict(_shared(), minutes_since_open=60)
        shared_early = dict(_shared(), minutes_since_open=8)
        normal = fields(ctx, bars, shared_normal)
        early = fields(ctx, bars, shared_early)
        self.assertEqual(early["entry_state"], early["entry_state_pre_chart"])
        self.assertFalse(early["chart_context"]["confidence"] == "HIGH")
        self.assertTrue(early["chart_context"]["earlySession"])
        self.assertIn(normal["entry_state"], (normal["entry_state_pre_chart"], "ENTRY_READY"))


class LearningRuleIntegrationTests(unittest.TestCase):
    def test_load_penalties_reads_db_rules(self):
        rules = [{"id": 9, "rule_text": "高値掴みを避ける", "status": "ACTIVE", "confidence": "HIGH"}]
        with mock.patch.object(server, "investment_db") as db:
            db.relevant_trade_rules_for.return_value = rules
            p = server.load_chart_rule_penalties("url", "u")
        self.assertEqual(p["byPattern"]["CHASE"], 5)
        with mock.patch.object(server, "investment_db") as db:
            db.relevant_trade_rules_for.side_effect = RuntimeError("down")
            self.assertEqual(server.load_chart_rule_penalties("url", "u"), {"byPattern": {}, "sources": []})

    def test_penalty_lowers_timing_score_in_pipeline(self):
        b = scaled(chase(1016.0))
        ctx = strong_ctx(b["closes"][-1])
        shared = dict(_shared(), chart_rule_penalties=cc.derive_rule_penalties(
            [{"id": 1, "rule_text": "高値追いをしない", "status": "ACTIVE", "confidence": "HIGH"}]))
        with_rule = fields(ctx, b, shared)
        without = fields(ctx, b)
        self.assertLessEqual(with_rule["entry_timing_score"], without["entry_timing_score"])


if __name__ == "__main__":
    unittest.main()
