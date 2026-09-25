# 場中5分足・VWAPの国内ソース化（2026-09-25、Phase B-2）のテスト。
import datetime
import unittest
from unittest import mock

import server

JST = server._JST


def _q(t, vol, hh, mm, ss=0, vwap=None):
    ts = datetime.datetime(2026, 9, 25, hh, mm, ss, tzinfo=JST).isoformat()
    return {"t": t, "volume": vol, "quote_timestamp": ts, "vwap": vwap, "is_stale": False, "source": "tachibana"}


class InternalBarTests(unittest.TestCase):
    def setUp(self):
        server._INTRADAY_BAR_STORE.clear()

    def test_ticks_build_5min_bars_with_ohlc_and_volume_delta(self):
        for q in [_q(100, 1000, 9, 1), _q(103, 1500, 9, 2), _q(99, 1800, 9, 4), _q(101, 2000, 9, 6)]:
            server._record_quote_tick("1111", q)
        fake_now = datetime.datetime(2026, 9, 25, 9, 7, tzinfo=JST)
        with mock.patch.object(server.datetime, "datetime", wraps=datetime.datetime) as dt:
            dt.now.return_value = fake_now
            dt.fromisoformat = datetime.datetime.fromisoformat
            res = server.get_internal_intraday_bars("1111")
        b0, b1 = res["bars"][0], res["bars"][1]
        self.assertEqual((b0["open"], b0["high"], b0["low"], b0["close"]), (100, 103, 99, 99))
        self.assertEqual(b0["volume"], 800)      # 1500-1000 + 1800-1500（最初のtickの出来高増分は不明として0）
        self.assertEqual(b1["open"], 101)
        self.assertTrue(b0["start"].endswith("09:00:00+09:00"))
        self.assertTrue(b1["start"].endswith("09:05:00+09:00"))
        self.assertEqual(res["barAt"], b1["start"])
        self.assertEqual(res["source"], "INTERNAL_TICKS")

    def test_out_of_session_ticks_are_ignored(self):
        server._record_quote_tick("1111", _q(100, 1000, 12, 0))   # 昼休み
        server._record_quote_tick("1111", _q(100, 1000, 15, 45))  # 大引け後
        self.assertNotIn("1111", server._INTRADAY_BAR_STORE)

    def test_no_ticks_reports_reason_instead_of_fake_bar(self):
        res = server.get_internal_intraday_bars("9999")
        self.assertEqual(res["bars"], [])
        self.assertIsNone(res["barAt"])
        self.assertTrue(res["unavailableReason"])

    def test_late_start_is_marked_partial(self):
        server._record_quote_tick("1111", _q(100, 1000, 9, 40))  # プロセス起動が場中(9:40)だった
        fake_now = datetime.datetime(2026, 9, 25, 9, 41, tzinfo=JST)
        with mock.patch.object(server.datetime, "datetime", wraps=datetime.datetime) as dt:
            dt.now.return_value = fake_now
            dt.fromisoformat = datetime.datetime.fromisoformat
            res = server.get_internal_intraday_bars("1111")
        self.assertTrue(res["partial"])


class VwapTests(unittest.TestCase):
    def test_exchange_vwap_overrides_snapshot_vwap_and_side(self):
        snap = {"vwap": 2000.0, "aboveVwap": False, "fiveMinStructure": "mixed", "dataStatus": "partial"}
        row = {"current": 2310.0, "_vwap": 2300.0}
        out = server._apply_exchange_vwap_to_snapshot(snap, row)
        self.assertEqual(out["vwap"], 2300.0)
        self.assertTrue(out["aboveVwap"])
        self.assertEqual(out["vwapSource"], "TACHIBANA_EXCHANGE")
        self.assertEqual(snap["vwap"], 2000.0)  # 元のdictは変更しない

    def test_no_exchange_vwap_leaves_snapshot_unchanged_and_never_zero(self):
        snap = {"vwap": None, "aboveVwap": None}
        self.assertIs(server._apply_exchange_vwap_to_snapshot(snap, {"current": 100.0, "_vwap": None}), snap)

    def test_analysis_fields_state_vwap_unavailable_explicitly(self):
        r = {"current": 100.0, "barAt": "2026-09-24T15:20:00+09:00"}
        server._apply_quote_derived_fields(r, {"code": "1111", "_quote": {"vwap": None, "quote_timestamp": "x"}})
        self.assertIsNone(r["vwap"])
        self.assertIn("VWAP unavailable", r["vwapUnavailableReason"])
        self.assertEqual(r["yfBarAt"], "2026-09-24T15:20:00+09:00")  # 古いyfinanceのbarをbarAtとして偽らない

    def test_analysis_fields_with_vwap(self):
        r = {"current": 102.0}
        server._apply_quote_derived_fields(r, {"code": "1111", "_quote": {"vwap": 100.0, "quote_timestamp": "x"}})
        self.assertEqual(r["vwap"], 100.0)
        self.assertEqual(r["vwapDistPct"], 2.0)
        self.assertIsNone(r["vwapUnavailableReason"])

    def test_fast_quotes_pass_vwap_through_and_record_ticks(self):
        server._fast_quote_cache.clear()
        server._INTRADAY_BAR_STORE.clear()
        with mock.patch.object(server.tachibana_api, "get_market_price",
                                return_value={"1111": {"t": 100.0, "p": 99.0, "volume": 10, "vwap": 99.5}}):
            q, _ = server.get_fast_quotes([{"code": "1111", "market": "JP"}])
        self.assertEqual(q["1111"]["vwap"], 99.5)

    def test_tachibana_requests_vwap_column(self):
        import tachibana_api
        self.assertIn("pVWAP", tachibana_api.PRICE_COLUMNS)


if __name__ == "__main__":
    unittest.main()
