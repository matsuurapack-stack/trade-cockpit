# Phase C shadow運用の最終安定化テスト：peak-update Decimal、legacy/chart独立性、厳密なtransition、
# outcome遅延統計、ログ統合。 cd files && python -m unittest test_shadow_stabilization -v

import datetime
import decimal
import json
import unittest
from unittest import mock

import chart_context
import chart_signal_log as sl
import investment_db
import server
from test_chart_context import chase
from test_chart_context_integration import fields, scaled, strong_ctx

JST = datetime.timezone(datetime.timedelta(hours=9))
T0 = datetime.datetime(2026, 9, 25, 10, 0, tzinfo=JST)


def at(minutes):
    return T0 + datetime.timedelta(minutes=minutes)


# ---------------------------------------------------------------- peak-update
class _Cur:
    def __init__(self, row):
        self.row = row

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        pass

    def fetchone(self):
        return self.row


class _Conn(_Cur):
    def cursor(self, **k):
        return _Cur(self.row)

    def commit(self):
        pass


class _Pool:
    def __init__(self, row):
        self.row = row

    def connection(self):
        return _Conn(self.row)


class PeakUpdateDecimalTests(unittest.TestCase):
    def test_update_position_peak_result_is_json_serializable(self):
        row = {"peak_price": decimal.Decimal("1234.50"), "peak_pnl_pct": decimal.Decimal("3.25")}
        with mock.patch.object(investment_db, "_get_pool", return_value=_Pool(row)):
            out = investment_db.update_position_peak("url", "u", 1, 1234.5, 3.25)
        self.assertEqual(out, {"peakPrice": 1234.5, "peakPnlPct": 3.25})
        json.dumps(out)   # Decimalのままだと TypeError（UI呼び出しごとに例外だった不具合）

    def test_no_row_returns_none(self):
        with mock.patch.object(investment_db, "_get_pool", return_value=_Pool(None)):
            self.assertIsNone(investment_db.update_position_peak("url", "u", 1, 1.0, 1.0))


# ---------------------------------------------------------------- legacy vs chart
def scan_shaped_candidate(f, code="5301", price=2300.0):
    """スキャン候補dictと同じキー（legacyEntryState/chartEntryStateを含む）を計算結果から組み立てる。"""
    return {"code": code, "name": "T", "current": price, "stockStrengthScore": f["stock_strength_score"],
            "entryStatePreChart": f["entry_state_pre_chart"], "legacyEntryState": f["entry_state_pre_chart"],
            "chartEntryState": f["entry_state"], "entryState": f["entry_state"], "entryDecision": f["entry_decision"],
            "chartContext": f["chart_context"], "marketRS": 1.0, "entryScore": f["entry_score"]}


class LegacyChartIndependenceTests(unittest.TestCase):
    def test_legacy_is_the_pre_gate_state_and_chart_is_post_gate(self):
        b = scaled(chase(1016.0))
        ctx = strong_ctx(b["closes"][-1])
        f = fields(ctx, b)
        self.assertIn(f["entry_state_pre_chart"], ("NOW_BUYABLE", "ENTRY_READY"))   # 旧判定はENTRY可
        self.assertEqual(f["entry_state"], "WAIT_PULLBACK")                        # Phase Cが降格
        # ゲートを恒等にした計算＝旧判定そのもの。legacyはそれと一致し、chartとは独立に計算されている。
        with mock.patch.object(chart_context, "apply_chart_gate", side_effect=lambda st, *a, **k: (st, [])):
            ungated = fields(ctx, b)
        self.assertEqual(ungated["entry_state"], f["entry_state_pre_chart"])
        self.assertEqual(ungated["entry_state_pre_chart"], f["entry_state_pre_chart"])
        self.assertNotEqual(f["entry_state_pre_chart"], f["entry_state"])

    def test_forced_legacy_entry_ready_chart_wait_pullback_is_saved_and_counted(self):
        b = scaled(chase(1016.0))
        f = fields(strong_ctx(b["closes"][-1]), b)
        f["entry_state_pre_chart"] = "ENTRY_READY"           # 強制：旧=ENTRY_READY / 新=WAIT_PULLBACK（CHASE）
        cand = scan_shaped_candidate(f)
        cand["legacyEntryState"], cand["entryStatePreChart"] = "ENTRY_READY", "ENTRY_READY"
        rec = sl.build_signal_record("u", cand, T0, "SCAN")
        self.assertEqual((rec["legacy_entry_state"], rec["chart_entry_state"]), ("ENTRY_READY", "WAIT_PULLBACK"))
        self.assertTrue(sl.is_loggable(rec))
        self.assertTrue(sl.is_priority(rec))
        self.assertTrue(sl.should_log(None, rec, T0))
        self.assertTrue(sl.should_log(sl.last_state(mk_rec("WATCH", "WATCH"), T0), rec, at(1)))   # 差分発生は即保存
        row = dict(rec, price_15m=rec["current_price"] * 0.995, price_5m=None, price_30m=None)
        day = sl.summarize_day([row])
        self.assertEqual(day["chase_stop_count"], 1)
        self.assertEqual(day["legacy_vs_chart_diff"]["ENTRY_READY→WAIT_PULLBACK"]["count"], 1)
        self.assertEqual(day["chase_stop_success_rate"], 1.0)

    def test_scan_and_rescore_wire_legacy_to_pre_gate_state(self):
        import inspect
        self.assertIn('"legacyEntryState": pd_fields["entry_state_pre_chart"]', inspect.getsource(server._score_entry_candidates_impl))
        self.assertIn('"chartEntryState": pd_fields["entry_state"]', inspect.getsource(server._score_entry_candidates_impl))
        src = inspect.getsource(server.rescore_entry_candidate_with_quote)
        self.assertIn('"legacyEntryState": f["entry_state_pre_chart"]', src)
        self.assertIn('"chartEntryState": f["entry_state"]', src)


def mk_rec(legacy, chart, pattern="BASE_BUILDING", top5=None):
    c = {"code": "5301", "current": 1000.0, "stockStrengthScore": 50, "entryStatePreChart": legacy, "entryState": chart,
         "entryDecision": None, "chartContext": {"pattern": pattern, "entry_timing_score": 50, "confidence": "HIGH", "barCount": 12,
                                                "reasons": [], "penalties": [], "features": {}}}
    return sl.build_signal_record("u", c, T0, "SCAN", top5)


# ---------------------------------------------------------------- strict transitions
class TransitionTests(unittest.TestCase):
    def step(self, mem, pat, chart, minute, legacy="WAIT_PULLBACK"):
        return sl.detect_transition(mem, ("u", "5301"), mk_rec(legacy, chart, pat), at(minute))

    def test_chase_to_pullback_ready_to_entry_ready_full_path(self):
        mem = {}
        self.assertEqual(self.step(mem, "CHASE", "WAIT_PULLBACK", 0), ([], None))
        self.assertEqual(self.step(mem, "BASE_BUILDING", "WAIT_PULLBACK", 5), ([], None))
        self.assertEqual(self.step(mem, "PULLBACK_READY", "WAIT_PULLBACK", 10), (["CHASE_TO_PULLBACK_READY"], None))
        t, origin = self.step(mem, "PULLBACK_READY", "ENTRY_READY", 15)
        self.assertEqual((t, origin), (["PULLBACK_READY_TO_ENTRY_READY"], "CHASE"))

    def test_both_types_in_one_evaluation_when_gate_promotes_immediately(self):
        mem = {}
        self.step(mem, "CHASE", "WAIT_PULLBACK", 0)
        t, origin = self.step(mem, "PULLBACK_READY", "ENTRY_READY", 5)
        self.assertEqual(t, ["CHASE_TO_PULLBACK_READY", "PULLBACK_READY_TO_ENTRY_READY"])
        self.assertEqual(origin, "CHASE")

    def test_pullback_ready_without_chase_origin_is_not_chase_transition(self):
        mem = {}
        self.step(mem, "BASE_BUILDING", "WAIT_PULLBACK", 0)
        t, origin = self.step(mem, "PULLBACK_READY", "ENTRY_READY", 5)
        self.assertEqual((t, origin), (["PULLBACK_READY_TO_ENTRY_READY"], None))

    def test_chase_origin_expires_after_window(self):
        mem = {}
        self.step(mem, "CHASE", "WAIT_PULLBACK", 0)
        t, _ = self.step(mem, "PULLBACK_READY", "WAIT_PULLBACK", 95)     # 90分超
        self.assertEqual(t, [])

    def test_failed_breakout_recovery(self):
        mem = {}
        self.step(mem, "FAILED_BREAKOUT", "WATCH", 0)
        self.assertEqual(self.step(mem, "BASE_BUILDING", "WATCH", 5), ([], None))
        t, origin = self.step(mem, "VWAP_RECLAIM", "WATCH", 10)
        self.assertEqual((t, origin), (["FAILED_BREAKOUT_TO_RECOVERY"], "FAILED_BREAKOUT"))
        self.assertEqual(self.step(mem, "PULLBACK_READY", "WATCH", 15), ([], None))   # 二重発火しない

    def test_entry_ready_to_chase_and_to_failed_breakout(self):
        mem = {}
        self.step(mem, "BASE_BUILDING", "ENTRY_READY", 0, legacy="ENTRY_READY")
        t, _ = self.step(mem, "CHASE", "WAIT_PULLBACK", 5, legacy="ENTRY_READY")
        self.assertEqual(t, ["ENTRY_READY_TO_CHASE"])
        mem = {}
        self.step(mem, "BREAKOUT_CONFIRMED", "ENTRY_READY", 0, legacy="ENTRY_READY")
        t, _ = self.step(mem, "FAILED_BREAKOUT", "WATCH", 5, legacy="ENTRY_READY")
        self.assertEqual(t, ["ENTRY_READY_TO_FAILED_BREAKOUT"])

    def test_no_transition_on_first_observation_or_unrelated_change(self):
        mem = {}
        self.assertEqual(self.step(mem, "CHASE", "ENTRY_READY", 0, legacy="ENTRY_READY"), ([], None))   # 初回は遷移でない
        self.assertEqual(self.step(mem, "CHASE", "ENTRY_READY", 5, legacy="ENTRY_READY"), ([], None))    # 継続は遷移でない

    def test_transition_summary_by_type_and_origin(self):
        rows = [{"code": "5301", "logged_at": at(0), "current_price": 1000.0, "chart_pattern": "PULLBACK_READY",
                 "legacy_entry_state": "WAIT_PULLBACK", "chart_entry_state": "ENTRY_READY", "price_15m": 1010.0,
                 "transition_type": "CHASE_TO_PULLBACK_READY,PULLBACK_READY_TO_ENTRY_READY", "transition_origin": "CHASE"}]
        s = sl.summarize_day(rows)["transition_types"]
        self.assertEqual(s["PULLBACK_READY_TO_ENTRY_READY"]["origins"], {"CHASE": 1})
        self.assertEqual(s["CHASE_TO_PULLBACK_READY"]["avg_ret_15m"], 1.0)


class LogIntegrationTests(unittest.TestCase):
    def setUp(self):
        server._CHART_SIGNAL_LAST.clear()
        server._CHART_SIGNAL_MEM.clear()

    def run_log(self, pool, now_offset_min):
        with server._ENTRY_TOP5_CACHE_LOCK:
            server._ENTRY_TOP5_CACHE["u1"] = {"_candidatePool": pool, "actionableTop5": [], "analysisTop5": [],
                                              "entryReadyTop5": [], "watchCandidates": []}
        saved = []
        fake_now = at(now_offset_min)
        with mock.patch.object(server, "investment_db") as db, mock.patch.object(server, "WRITE_E2E_ALLOWED", True), \
                mock.patch.object(server, "_is_jp_market_business_day", return_value=True), \
                mock.patch.object(server.datetime, "datetime", wraps=datetime.datetime) as dt:
            dt.now.return_value = fake_now
            db.insert_chart_signals.side_effect = lambda url, recs: saved.extend(recs) or len(recs)
            server.log_chart_signals("url", "u1", "SCAN")
        return saved

    def cand(self, pattern, legacy, chart):
        return {"code": "5301", "current": 1000.0, "stockStrengthScore": 50, "entryStatePreChart": legacy,
                "entryState": chart, "entryDecision": None,
                "chartContext": {"pattern": pattern, "entry_timing_score": 50, "confidence": "HIGH", "barCount": 12,
                                 "reasons": [], "penalties": [], "features": {}}}

    def test_transition_is_recorded_at_the_moment_and_plain_continuation_is_skipped(self):
        a = self.run_log([self.cand("CHASE", "ENTRY_READY", "WAIT_PULLBACK")], 0)
        self.assertEqual(len(a), 1)                               # 初回＋legacy/chart差分
        self.assertIsNone(a[0]["transition_type"])
        b = self.run_log([self.cand("PULLBACK_READY", "WAIT_PULLBACK", "ENTRY_READY")], 5)
        self.assertEqual(len(b), 1)
        self.assertEqual(b[0]["transition_type"], "CHASE_TO_PULLBACK_READY,PULLBACK_READY_TO_ENTRY_READY")
        self.assertEqual(b[0]["transition_origin"], "CHASE")
        c = self.run_log([self.cand("PULLBACK_READY", "WAIT_PULLBACK", "ENTRY_READY")], 10)
        self.assertEqual(c, [])                                   # 同一状態の継続は省略


# ---------------------------------------------------------------- outcome delay stats
class OutcomeQualityTests(unittest.TestCase):
    def test_median_p95_max_of_actual_delay(self):
        rows = []
        for i, d in enumerate([2, 4, 6, 8, 10, 12, 14, 16, 18, 100]):   # 5分後を取れた実遅延（秒）
            rows.append({"logged_at": T0.isoformat(), "price_5m": 1.0, "outcome_done": True,
                         "at_5m": (T0 + datetime.timedelta(minutes=5, seconds=d)).isoformat()})
        rows.append({"logged_at": T0.isoformat(), "price_5m": None, "outcome_done": True, "at_5m": None})   # 取り逃し
        q = sl.outcome_quality(rows)["5m"]
        self.assertEqual(q["measurable"], 10)
        self.assertEqual(q["delay_sec_median"], 11.0)
        self.assertEqual(q["delay_sec_max"], 100.0)
        self.assertGreater(q["delay_sec_p95"], 18)
        self.assertEqual(q["missed(done but no price)"], 1)
        self.assertEqual(sl.outcome_quality([])["15m"]["delay_sec_max"], None)


if __name__ == "__main__":
    unittest.main()
