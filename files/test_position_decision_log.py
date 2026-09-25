# 実ポジション売却判断ログ（2026-09-25、Phase B-3）のテスト。
import json
import os
import tempfile
import unittest

import server


class PositionDecisionLogTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)

    def tearDown(self):
        os.remove(self.path)

    def _rec(self, decision, prev=None, kind="TRANSITION", pnl=1.2, **kw):
        base = {"code": "5301", "market": "JP", "kind": kind, "quoteAt": "2026-09-25T10:00:00+09:00",
                "analysisAt": "2026-09-25T10:00:01+09:00", "entryPrice": 2273.0, "currentPrice": 2300.0,
                "unrealizedPct": pnl, "highSinceEntry": 2310.0, "distanceFromHighPct": -0.43, "exitRule": "OK",
                "exitDecision": decision, "prevDecision": prev, "riskState": None, "profitThenDeterioration": False,
                "reasons": []}
        base.update(kw)
        return base

    def test_transitions_and_snapshots_are_appended_with_required_fields(self):
        recs = [self._rec("HOLD_STRONG"), self._rec("CAUTION", "HOLD_STRONG"), self._rec("EXIT", "CAUTION", pnl=-0.2,
                profitThenDeterioration=True), self._rec("EXIT", "EXIT", kind="SNAPSHOT")]
        n = server.append_position_decision_log(recs, path=self.path)
        self.assertEqual(n, 4)
        rows = server.read_position_decision_log(100, path=self.path)
        self.assertEqual([r["exitDecision"] for r in rows], ["HOLD_STRONG", "CAUTION", "EXIT", "EXIT"])
        for k in ("quoteAt", "analysisAt", "entryPrice", "currentPrice", "unrealizedPct", "highSinceEntry",
                  "distanceFromHighPct", "exitRule", "exitDecision", "riskState", "receivedAt"):
            self.assertIn(k, rows[0])

    def test_profit_then_deterioration_is_recorded(self):
        server.append_position_decision_log(
            [self._rec("EXIT", "HOLD_STRONG", pnl=-0.1, peakPnlPct=1.8, profitThenDeterioration=True)], path=self.path)
        row = server.read_position_decision_log(1, path=self.path)[0]
        self.assertTrue(row["profitThenDeterioration"])
        self.assertEqual(row["peakPnlPct"], 1.8)

    def test_unknown_keys_dropped_and_invalid_records_skipped(self):
        n = server.append_position_decision_log(
            [{"code": "5301", "kind": "SNAPSHOT", "evil": "x"}, {"nocode": 1}, "str", None], path=self.path)
        self.assertEqual(n, 1)
        self.assertNotIn("evil", server.read_position_decision_log(1, path=self.path)[0])

    def test_read_missing_file_returns_empty(self):
        self.assertEqual(server.read_position_decision_log(5, path=self.path + ".none"), [])


if __name__ == "__main__":
    unittest.main()
