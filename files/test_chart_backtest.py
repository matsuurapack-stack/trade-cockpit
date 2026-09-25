# Chart Context バックテスト（hindsight bias禁止）のテスト。実行： cd files && python -m unittest test_chart_backtest -v
import copy
import datetime
import unittest

import backtest_chart_context as bt

JST = bt.JST


def day_bars(n=30, base=1000.0):
    t0 = datetime.datetime(2026, 9, 24, 9, 0, tzinfo=JST)
    out, prev = [], base
    for i in range(n):
        c = base + i * 0.5
        out.append({"start": t0 + datetime.timedelta(minutes=5 * i), "open": prev, "high": max(prev, c) + 0.3,
                    "low": min(prev, c) - 0.3, "close": c, "volume": 1000.0 + i})
        prev = c
    return out


class NoFutureTests(unittest.TestCase):
    def test_slice_excludes_forming_and_future_bars(self):
        bars = day_bars()
        entry = datetime.datetime(2026, 9, 24, 10, 7, 30, tzinfo=JST)   # 10:05の足は形成中
        prior = bt.slice_bars_before(bars, entry)
        self.assertEqual(prior[-1]["start"], datetime.datetime(2026, 9, 24, 10, 0, tzinfo=JST))
        self.assertTrue(all(b["start"] + datetime.timedelta(minutes=5) <= entry for b in prior))

    def test_decision_unchanged_when_future_bars_are_altered(self):
        bars = day_bars()
        entry = datetime.datetime(2026, 9, 24, 10, 20, tzinfo=JST)
        trade = {"id": 1, "code": "X", "entry_price": 1010.0, "net_pnl": -1000.0, "entry_dt": entry,
                 "exit_dt": entry + datetime.timedelta(minutes=30)}
        a = bt.evaluate_trade(trade, bars)
        mutated = copy.deepcopy(bars)
        for b in mutated:
            if b["start"] >= entry:   # ENTRY以降の足を大きく改変（暴騰/暴落）
                b["high"] *= 1.2
                b["low"] *= 0.7
                b["close"] *= 1.15
        b2 = bt.evaluate_trade(trade, mutated)
        for k in ("pattern", "timing", "confidence", "bars", "decision"):
            self.assertEqual(a[k], b2[k])

    def test_outcome_label_uses_post_entry_bars_only_for_labeling(self):
        bars = day_bars()
        post = bars[13:19]
        lab, mfe = bt.outcome_label(1006.0, 500.0, post)
        self.assertIn(lab, ("QUICK_WIN", "WIN", "SIDEWAYS"))
        self.assertEqual(bt.outcome_label(1006.0, -500.0, post)[0], "DECLINE")

    def test_summary_avoidance_counts(self):
        rows = [{"decision": "NO_ENTRY_CHASE", "net_pnl": -100.0, "pattern": "CHASE"},
                {"decision": "NO_ENTRY_FAILED_BREAK", "net_pnl": 50.0, "pattern": "FAILED_BREAKOUT"},
                {"decision": "ENTRY_READY", "net_pnl": 30.0, "pattern": "PULLBACK_READY"}]
        s = bt.summarize(rows)
        self.assertEqual(s["avoidance"], {"blocked_trades": 2, "blocked_losers": 1, "loss_avoided_yen": 100, "profit_forgone_yen": 50})
        self.assertEqual(s["decision_ENTRY_READY"]["win_rate"], 1.0)


if __name__ == "__main__":
    unittest.main()
