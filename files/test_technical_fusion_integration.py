# Technical Fusion（shadow）とTOP5／shadowログの統合テスト。
# 実行： cd files && python -m unittest test_technical_fusion_integration -v
import datetime
import unittest
from unittest import mock

import chart_signal_log as sl
import server
import technical_fusion as tf
from test_chart_context import chase, pullback_ready
from test_chart_context_integration import fields, scaled, strong_ctx
from test_entry_top5_rescore import _shared, _quote


class FusionInPipelineTests(unittest.TestCase):
    def test_fusion_is_returned_and_flags_chase_as_late(self):
        b = scaled(chase(1016.0))
        f = fields(strong_ctx(b["closes"][-1]), b)
        fu = f["fusion"]
        self.assertIsNotNone(fu)
        self.assertEqual(fu["role"], "shadow")
        self.assertTrue(fu["late"])                                 # CHASE＝遅い
        self.assertEqual(fu["recommendation"], "WAIT")              # テクニカルが強くてもENTRYタイミングが悪ければWAIT
        self.assertNotEqual(fu["technicalState"], "STRONG")

    def test_pullback_shape_gets_pullback_setup(self):
        b = scaled(pullback_ready(1016.0))
        fu = fields(strong_ctx(b["closes"][-1]), b)["fusion"]
        self.assertEqual(fu["setupType"], "PULLBACK")
        self.assertFalse(fu["late"])

    def test_fusion_never_changes_production_decision(self):
        """shadowの証明：fusionが例外でもゴミ値でも、entry_score/entry_state/entry_decisionは不変。"""
        b = scaled(pullback_ready(1016.0))
        ctx = strong_ctx(b["closes"][-1])
        base = fields(ctx, b)
        with mock.patch.object(server.technical_fusion, "evaluate_fusion", side_effect=RuntimeError("boom")):
            broken = fields(ctx, b)
        with mock.patch.object(server.technical_fusion, "evaluate_fusion",
                               return_value={"recommendation": "WAIT", "late": True, "confluence": {"score": 0}}):
            junk = fields(ctx, b)
        for other in (broken, junk):
            for k in ("entry_score", "entry_state", "entry_decision", "entry_timing_score", "stock_strength_score"):
                self.assertEqual(base[k], other[k])
        self.assertIsNone(broken["fusion"])

    def test_rescore_carries_compact_fusion_json_safe(self):
        import json
        ctx = strong_ctx(2300.0)
        ctx["bars"] = scaled(pullback_ready(1016.0))
        out = server.rescore_entry_candidate_with_quote(ctx, _shared(), _quote(2310.0))
        self.assertIn("technicalFusion", out)
        json.dumps(out["technicalFusion"], ensure_ascii=False)
        self.assertEqual(out["technicalFusion"]["level"] in ("HIGH", "MEDIUM", "LOW", "UNKNOWN"), True)


class FusionShadowLogTests(unittest.TestCase):
    def _cand(self, level, setup="PULLBACK", state="ENTRY_READY", pattern="PULLBACK_READY", breakout=None, ma=None, tech="STRONG"):
        return {"code": "5301", "name": "x", "current": 2300.0, "chartContext": {"pattern": pattern, "entry_timing_score": 70,
                "confidence": "HIGH", "features": {}}, "entryState": state, "entryDecision": "ENTRY_OK",
                "technicalFusion": {"level": level, "score": 80, "setupType": setup, "breakout": breakout, "ma": ma or [],
                                    "technicalState": tech, "recommendation": "ENTRY_SUPPORTED", "groups": {}}}

    def test_build_signal_record_stores_fusion_in_context_json(self):
        now = datetime.datetime(2026, 9, 26, 10, 0, tzinfo=server._JST)
        rec = sl.build_signal_record("u", self._cand("HIGH"), now, "SCAN")
        self.assertEqual(rec["context"]["technicalFusion"]["level"], "HIGH")
        self.assertEqual(rec["context"]["technicalFusion"]["setupType"], "PULLBACK")

    def test_summarize_fusion_groups_by_confluence_and_existing_state(self):
        base = datetime.datetime(2026, 9, 26, 10, 0, tzinfo=server._JST)
        rows = []
        specs = [("A", "HIGH", "ENTRY_READY", "PULLBACK_READY", None, 0.8), ("B", "HIGH", "CHASE_RISK", "CHASE", None, -0.6),
                 ("C", "LOW", "ENTRY_READY", "PULLBACK_READY", None, -0.3), ("D", "MEDIUM", "WATCH", "NEUTRAL", "STRONG_VOLUME_BREAKOUT", 0.9),
                 ("E", "MEDIUM", "WATCH", "NEUTRAL", "WEAK_BREAKOUT", -0.4)]
        for i, (code, lvl, st, pat, brk, ret) in enumerate(specs):
            c = self._cand(lvl, state=st, pattern=pat, breakout=brk)
            c["code"] = code
            rec = sl.build_signal_record("u", c, base + datetime.timedelta(minutes=i), "SCAN")
            rec.update({"price_5m": 2300 * (1 + ret / 200), "price_15m": 2300 * (1 + ret / 100), "price_30m": 2300 * (1 + ret / 100),
                        "max_30m": 2300 * (1 + max(ret, 0) / 100), "min_30m": 2300 * (1 - abs(min(ret, 0)) / 100),
                        "chart_entry_state": st, "chart_pattern": pat, "logged_at": rec["logged_at"]})
            rows.append(rec)
        out = sl.summarize_fusion(rows)
        g = out["groups"]
        self.assertEqual(out["n_events"], 5)
        self.assertEqual(g["HIGH_CONFLUENCE_and_ENTRY_READY"]["n"], 1)
        self.assertEqual(g["HIGH_CONFLUENCE_and_CHASE"]["n"], 1)
        self.assertEqual(g["LOW_CONFLUENCE_and_ENTRY_READY"]["n"], 1)
        self.assertEqual(g["BREAKOUT_strong_volume"]["n"], 1)
        self.assertEqual(g["BREAKOUT_weak_volume"]["n"], 1)
        self.assertGreater(g["BREAKOUT_strong_volume"]["avg_ret_15m"], g["BREAKOUT_weak_volume"]["avg_ret_15m"])
        self.assertIn("PULLBACK", out["by_setup_type"])

    def test_rows_without_fusion_are_ignored(self):
        self.assertEqual(sl.summarize_fusion([{"code": "x", "logged_at": "2026-09-26T10:00:00", "current_price": 1, "context": {}}])["n_events"], 0)


if __name__ == "__main__":
    unittest.main()
