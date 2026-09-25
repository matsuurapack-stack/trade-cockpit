# Phase C shadow運用（判定ログ・事後リターン・日次集計）のテスト。cd files && python -m unittest test_chart_signal_log -v

import datetime
import inspect
import unittest
from unittest import mock

import chart_signal_log as sl
import chart_context
import server
from test_chart_context_integration import fields, scaled, strong_ctx
from test_chart_context import chase, pullback_ready

JST = datetime.timezone(datetime.timedelta(hours=9))
T0 = datetime.datetime(2026, 9, 25, 10, 0, tzinfo=JST)


def cand(code="5301", price=1000.0, pattern="CHASE", legacy="ENTRY_READY", chart="WAIT_PULLBACK", **kw):
    ft = {"vwapDistPct": 1.5, "chg15m": 1.8, "consecGreen": 3, "upperWickAvg3": 0.4, "breakoutVolRatio": 2.1,
          "distFromDayHighPct": -0.2, "brokeBarsAgo": 1}
    c = {"code": code, "name": "テスト", "current": price, "stockStrengthScore": 62, "entryStatePreChart": legacy,
         "entryState": chart, "entryDecision": "NO_ENTRY_CHASE", "marketRS": 1.2, "entryScore": 70,
         "chartContext": {"pattern": pattern, "entry_timing_score": 30, "confidence": "HIGH", "barCount": 14,
                          "reasons": ["r1"], "penalties": ["p1"], "features": ft}}
    c.update(kw)
    return c


def row(code="5301", at=0, price=1000.0, pattern="CHASE", legacy="ENTRY_READY", chart="WAIT_PULLBACK", p5=None, p15=None, p30=None, **kw):
    r = {"code": code, "logged_at": T0 + datetime.timedelta(minutes=at), "current_price": price, "chart_pattern": pattern,
         "legacy_entry_state": legacy, "chart_entry_state": chart, "entry_decision": None,
         "price_5m": p5, "price_15m": p15, "price_30m": p30, "upper_wick_ratio": 0.3, "breakout_volume_ratio": 2.0,
         "vwap_distance": 1.0, "features": {}, "context": {}}
    r.update(kw)
    return r


class RecordTests(unittest.TestCase):
    def test_record_has_all_required_fields(self):
        rec = sl.build_signal_record("u", cand(), T0, "SCAN", {"actionableTop5": {"5301"}})
        for k in ("logged_at", "code", "current_price", "stock_strength", "entry_timing", "chart_pattern", "chart_confidence",
                  "legacy_entry_state", "chart_entry_state", "vwap", "vwap_distance", "change_15m", "consecutive_green",
                  "upper_wick_ratio", "breakout_volume_ratio", "reasons", "penalties"):
            self.assertIn(k, rec)
            self.assertIsNotNone(rec[k], k)
        self.assertEqual(rec["legacy_entry_state"], "ENTRY_READY")
        self.assertEqual(rec["chart_entry_state"], "WAIT_PULLBACK")
        self.assertEqual(rec["context"]["top5"], ["actionableTop5"])
        self.assertAlmostEqual(rec["vwap"], 1000 / 1.015, places=1)

    def test_no_chart_or_price_gives_none(self):
        self.assertIsNone(sl.build_signal_record("u", {"code": "1", "current": 1.0}, T0, "SCAN"))
        c = cand()
        c["current"] = None
        self.assertIsNone(sl.build_signal_record("u", c, T0, "SCAN"))

    def test_should_log_dedupe_change_and_heartbeat(self):
        rec = sl.build_signal_record("u", cand(), T0, "SCAN")
        last = {"pattern": "CHASE", "chart": "WAIT_PULLBACK", "legacy": "ENTRY_READY", "at": T0}
        self.assertTrue(sl.should_log(None, rec, T0))
        self.assertFalse(sl.should_log(last, rec, T0 + datetime.timedelta(seconds=60)))
        self.assertTrue(sl.should_log(last, rec, T0 + datetime.timedelta(seconds=301)))
        rec2 = sl.build_signal_record("u", cand(pattern="PULLBACK_READY"), T0, "RESCORE")
        self.assertTrue(sl.should_log(last, rec2, T0 + datetime.timedelta(seconds=10)))


class OutcomeTests(unittest.TestCase):
    def test_due_and_missed_horizons(self):
        m = datetime.timedelta
        self.assertEqual(sl.due_horizons(T0, T0 + m(minutes=4), set()), ([], []))
        self.assertEqual(sl.due_horizons(T0, T0 + m(minutes=5, seconds=10), set()), ([5], []))
        self.assertEqual(sl.due_horizons(T0, T0 + m(minutes=16), set()), ([15], [5]))   # 5分は取り逃し
        self.assertEqual(sl.due_horizons(T0, T0 + m(minutes=5, seconds=10), {5}), ([], []))
        self.assertTrue(sl.outcome_finished(T0, T0 + m(minutes=35)))
        self.assertFalse(sl.outcome_finished(T0, T0 + m(minutes=20)))

    def test_return_and_class(self):
        self.assertEqual(sl.return_pct(1000, 1010), 1.0)
        self.assertIsNone(sl.return_pct(1000, None))
        self.assertEqual(sl.classify_return(1.0), "UP")
        self.assertEqual(sl.classify_return(0.1), "FLAT")
        self.assertEqual(sl.classify_return(-0.5), "DECLINE")

    def test_fill_writes_prices_from_latest_quote_only(self):
        pending = [{"id": 1, "code": "5301", "logged_at": datetime.datetime.now(JST) - datetime.timedelta(minutes=5, seconds=20),
                    "current_price": 1000.0, "day_high": 1005.0, "price_5m": None, "price_15m": None, "price_30m": None,
                    "max_30m": None, "min_30m": None, "new_high_after_sec": None}]
        with mock.patch.object(server, "investment_db") as db, mock.patch.object(server, "WRITE_E2E_ALLOWED", True), \
                mock.patch.object(server, "get_fast_quotes", return_value=({"5301": {"t": 1010.0}}, "OK")):
            db.list_pending_chart_signals.return_value = pending
            server.fill_chart_signal_outcomes("url")
            (_, ups), _kw = db.update_chart_signal_outcomes.call_args
        self.assertEqual(ups[0]["price_5m"], 1010.0)
        self.assertNotIn("price_15m", ups[0])
        self.assertEqual(ups[0]["max_30m"], 1010.0)
        self.assertIn("new_high_after_sec", ups[0])   # 1010 > 日中高値1005

    def test_writes_disabled_when_not_allowed(self):
        with mock.patch.object(server, "investment_db") as db, mock.patch.object(server, "WRITE_E2E_ALLOWED", False):
            self.assertEqual(server.log_chart_signals("url", "u", "SCAN"), 0)
            self.assertEqual(server.fill_chart_signal_outcomes("url"), 0)
            db.insert_chart_signals.assert_not_called()


class SummaryTests(unittest.TestCase):
    def test_chase_stop_success_and_miss(self):
        rows = [row("A", pattern="CHASE", p15=995.0), row("B", pattern="EXTENDED", p15=1015.0),
                row("C", pattern="CHASE", p15=1001.0), row("D", pattern="CHASE")]   # Dは事後未取得
        s = sl.summarize_day(rows)
        self.assertEqual(s["chase_stop_count"], 4)
        self.assertEqual(s["chase_stop_success_rate"], round(2 / 3, 3))   # A(下落) C(横横)
        self.assertEqual(s["chase_miss_rate"], round(1 / 3, 3))           # B(+1.5%)
        self.assertEqual(s["chase_stop_outcome_15m"], {"n": 3, "UP": 1, "FLAT": 1, "DECLINE": 1})

    def test_pullback_and_entry_ready_rates(self):
        rows = [row("A", pattern="PULLBACK_READY", legacy="WAIT_PULLBACK", chart="ENTRY_READY", p15=1010.0, p30=1020.0),
                row("B", pattern="PULLBACK_READY", legacy="WAIT_PULLBACK", chart="ENTRY_READY", p15=1000.0, p30=990.0)]
        s = sl.summarize_day(rows)
        self.assertEqual(s["pullback_ready_count"], 2)
        self.assertEqual(s["pullback_success_rate"], 0.5)
        self.assertEqual(s["pullback_picked_by_chart"]["count"], 2)
        self.assertEqual(s["entry_ready_count"], 2)
        self.assertEqual(s["entry_ready_plus_rate_15m"], {"n": 2, "rate": 0.5})
        self.assertEqual(s["entry_ready_plus_rate_30m"], {"n": 2, "rate": 0.5})
        self.assertEqual(s["legacy_vs_chart_diff"]["WAIT_PULLBACK→ENTRY_READY"]["count"], 2)

    def test_failed_breakout_misjudge(self):
        rows = [row("4440", pattern="FAILED_BREAKOUT", legacy="ENTRY_READY", chart="WATCH", p30=1043.0, max_30m=1050.0,
                    new_high_after_sec=900, features={"brokeBarsAgo": 1}),
                row("X", pattern="FAILED_BREAKOUT", legacy="ENTRY_READY", chart="WATCH", p30=990.0)]
        s = sl.summarize_day(rows)
        self.assertEqual(s["failed_breakout_count"], 2)
        self.assertEqual(s["failed_breakout_misjudge_rate"], 0.5)
        case = [c for c in s["failed_breakout_cases"] if c["code"] == "4440"][0]
        self.assertEqual(case["ret_30m"], 4.3)
        self.assertEqual(case["new_high_after_sec"], 900)
        self.assertEqual(case["max_ret_30m"], 5.0)

    def test_transition_chase_to_entry_ready_is_tracked(self):
        seq = [("CHASE", "ENTRY_READY", "WAIT_PULLBACK"), ("CHASE", "ENTRY_READY", "WAIT_PULLBACK"),
               ("BASE_BUILDING", "WAIT_PULLBACK", "WAIT_PULLBACK"), ("PULLBACK_READY", "WAIT_PULLBACK", "ENTRY_READY")]
        rows = [row("5301", at=i * 5, pattern=p, legacy=lg, chart=ch) for i, (p, lg, ch) in enumerate(seq)]
        t = sl.summarize_day(rows)["transitions"]
        self.assertEqual(len(t["completed_chase_to_entry"]), 1)
        self.assertEqual(t["completed_chase_to_entry"][0]["steps"][0], ("CHASE", "WAIT_PULLBACK"))
        self.assertEqual(t["completed_chase_to_entry"][0]["steps"][-1], ("PULLBACK_READY", "ENTRY_READY"))
        self.assertEqual(t["edges"]["CHASE→BASE_BUILDING"], 1)

    def test_empty_day(self):
        s = sl.summarize_day([])
        self.assertEqual(s["events"], 0)
        self.assertIsNone(s["chase_stop_success_rate"])


class ShadowFieldTests(unittest.TestCase):
    def test_legacy_and_chart_states_both_kept_and_diverge_on_chase(self):
        b = scaled(chase(1016.0))
        f = fields(strong_ctx(b["closes"][-1]), b)
        self.assertIn(f["entry_state_pre_chart"], ("NOW_BUYABLE", "ENTRY_READY"))   # legacy
        self.assertEqual(f["entry_state"], "WAIT_PULLBACK")                        # chart（CHASEは待機へ送る）
        self.assertNotEqual(f["entry_state_pre_chart"], f["entry_state"])

    def test_rescore_output_has_legacy_and_chart_aliases(self):
        bars = scaled(pullback_ready(1016.0))
        ctx = strong_ctx(bars["closes"][-1])
        ctx["bars"] = bars
        from test_entry_top5_rescore import _shared, _quote
        with mock.patch.object(server, "get_internal_intraday_bars", return_value={"bars": []}):
            out = server.rescore_entry_candidate_with_quote(ctx, _shared(), _quote(bars["closes"][-1]))
        self.assertEqual(out["legacyEntryState"], out["entryStatePreChart"])
        self.assertEqual(out["chartEntryState"], out["entryState"])

    def test_new_bar_trigger_only_when_slot_tracked(self):
        ctx = {"scoredPrice": 1000.0, "recentHigh": None, "baseHigh": 1010.0, "snapshot": {}}
        q = {"t": 1000.0}
        self.assertIsNone(server.entry_rescore_trigger(ctx, q))            # 未追跡ctx（既存動作は不変）
        ctx["lastBarSlot"] = -1
        self.assertEqual(server.entry_rescore_trigger(ctx, q), "NEW_BAR")
        self.assertIsNone(server.entry_rescore_trigger(ctx, q))            # 同じ5分内は再発火しない


class NoHindsightTests(unittest.TestCase):
    def test_decision_code_never_reads_outcomes(self):
        for obj in (chart_context, server._compute_price_dependent_entry_fields, server.rescore_entry_candidate_with_quote,
                    server._select_entry_ready_top5, server.entry_rescore_trigger):
            src = inspect.getsource(obj)
            for banned in ("chart_signal_log", "price_5m", "price_15m", "price_30m", "list_chart_signals"):
                self.assertNotIn(banned, src, f"{getattr(obj, '__name__', obj)} が事後データ({banned})を参照している")


if __name__ == "__main__":
    unittest.main()
