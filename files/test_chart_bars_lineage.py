# 場中5分足のデータ系統（立花内部足＋yfinance履歴bootstrap）と Technical Fusion の source/stale guard のテスト。
# 実行： cd files && python -m unittest test_chart_bars_lineage -v
import datetime
import inspect
import unittest
from unittest import mock

import server
import technical_fusion as tf

JST = server._JST


def dt(h, m, s=0):
    return datetime.datetime(2026, 9, 26, h, m, s, tzinfo=JST)


def yf_bars(start_h, start_m, n, base=100.0):
    starts, t = [], dt(start_h, start_m)
    closes = []
    for i in range(n):
        starts.append(t.isoformat())
        closes.append(base + i * 0.1)
        t += datetime.timedelta(minutes=5)
    return {"closes": closes, "highs": [c + 0.1 for c in closes], "lows": [c - 0.1 for c in closes],
            "opens": [c - 0.05 for c in closes], "volumes": [1000.0] * n, "starts": starts}


def internal(start, n, base=102.0):
    out, t = [], start
    for i in range(n):
        out.append({"start": t.isoformat(), "open": base, "high": base + 0.2, "low": base - 0.2, "close": base + 0.1,
                    "volume": 500.0, "ticks": 3})
        t += datetime.timedelta(minutes=5)
    return out


class BuildChartBarsTests(unittest.TestCase):
    def test_yfinance_history_then_tachibana_tail(self):
        yf = yf_bars(9, 0, 10)                    # 09:00〜09:45（遅延足）
        ib = internal(dt(9, 50), 4)               # 立花の足 09:50〜10:05
        with mock.patch.object(server, "get_internal_intraday_bars", return_value={"bars": ib}):
            bars, lin = server.build_chart_bars("627A", yf, now=dt(10, 8))
        self.assertEqual(lin["intraday_source"], "YF_HISTORY+TACHIBANA_INTERNAL_5M")
        self.assertEqual(lin["yf_bars_used"], 10)
        self.assertEqual(lin["gap_bars"], 0)       # 09:45の次が09:50＝欠落なし
        self.assertEqual(len(bars["closes"]), 14)
        self.assertEqual(bars["starts"][10], dt(9, 50).isoformat())   # 履歴の後ろに立花足が続く
        self.assertEqual(bars["closes"][-1], 102.1)
        self.assertEqual(lin["bar_at"], dt(10, 5).isoformat())
        self.assertFalse(lin["stale"])

    def test_yfinance_bars_overlapping_tachibana_are_dropped_not_duplicated(self):
        yf = yf_bars(9, 0, 12)                    # 09:00〜09:55（遅延足だが立花足と重なる）
        ib = internal(dt(9, 50), 3)
        with mock.patch.object(server, "get_internal_intraday_bars", return_value={"bars": ib}):
            bars, lin = server.build_chart_bars("627A", yf, now=dt(10, 3))
        self.assertEqual(lin["yf_bars_used"], 10)  # 09:50以降のyf足は捨てる
        self.assertEqual(len(bars["closes"]), 13)
        self.assertEqual(len(set(bars["starts"])), len(bars["starts"]))

    def test_gap_between_history_and_tachibana_is_reported_and_marks_stale(self):
        yf = yf_bars(9, 0, 6)                     # 09:00〜09:25
        ib = internal(dt(9, 55), 3)               # 立花足は09:55から（09:30〜09:50の5本が欠落）
        with mock.patch.object(server, "get_internal_intraday_bars", return_value={"bars": ib}):
            bars, lin = server.build_chart_bars("627A", yf, now=dt(10, 8))
        self.assertEqual(lin["gap_bars"], 5)
        self.assertTrue(lin["stale"])
        self.assertTrue(any("欠落" in r for r in lin["reasons"]))

    def test_lunch_break_is_not_counted_as_gap(self):
        self.assertEqual(server._slots_between(dt(11, 25), dt(12, 30)), 0)
        self.assertEqual(server._slots_between(dt(11, 20), dt(12, 30)), 1)   # 11:25のみ

    def test_no_tachibana_bars_marks_yfinance_as_delayed_and_stale(self):
        yf = yf_bars(9, 0, 20)
        with mock.patch.object(server, "get_internal_intraday_bars", return_value={"bars": []}):
            bars, lin = server.build_chart_bars("627A", yf, now=dt(10, 40))
        self.assertIs(bars, yf)
        self.assertEqual(lin["intraday_source"], "YFINANCE_DELAYED")
        self.assertTrue(lin["stale"])

    def test_old_tachibana_bar_during_session_is_stale(self):
        ib = internal(dt(9, 0), 6)               # 最新bar開始09:25
        with mock.patch.object(server, "get_internal_intraday_bars", return_value={"bars": ib}):
            _, lin = server.build_chart_bars("627A", None, now=dt(10, 0))
        self.assertTrue(lin["stale"])
        self.assertEqual(lin["intraday_source"], "TACHIBANA_INTERNAL_5M")

    def test_fresh_tachibana_only_is_not_stale(self):
        ib = internal(dt(9, 0), 8)               # 最新bar開始09:35
        with mock.patch.object(server, "get_internal_intraday_bars", return_value={"bars": ib}):
            bars, lin = server.build_chart_bars("627A", None, now=dt(9, 38))
        self.assertFalse(lin["stale"])
        self.assertEqual(len(bars["closes"]), 8)

    def test_parse_frame_keeps_bar_start_times(self):
        import pandas as pd
        idx = pd.date_range("2026-09-26 09:00", periods=4, freq="5min", tz="Asia/Tokyo")
        h = pd.DataFrame({"Open": [1, 2, 3, 4], "High": [2, 3, 4, 5], "Low": [1, 1, 2, 3], "Close": [2, 3, 4, 5],
                          "Volume": [1, 1, 1, 1]}, index=idx)
        out = server._parse_intraday_bars_frame(h)
        self.assertEqual(len(out["starts"]), 4)
        self.assertTrue(out["starts"][0].startswith("2026-09-26T09:00:00"))


class LegacyIntradayInputsUseTachibanaBarsTests(unittest.TestCase):
    """5分足構造・値幅余地・Chart/Fusionが、yfinance遅延足ではなく立花足を接続した足を見ること。"""

    def _falling(self, n=24):
        closes = [110 - i * 0.3 for i in range(n)]
        return {"closes": closes, "highs": [c + 0.1 for c in closes], "lows": [c - 0.1 for c in closes],
                "opens": [c + 0.05 for c in closes], "volumes": [1000.0] * n}

    def test_structure_is_rederived_from_tachibana_connected_bars(self):
        from test_chart_context_integration import fields, strong_ctx, scaled
        from test_chart_context import pullback_ready
        yf_rising = scaled(pullback_ready(1016.0))                 # 遅延したyfinance足は「上昇」構造
        ctx = strong_ctx(yf_rising["closes"][-1])
        lin = {"intraday_source": "TACHIBANA_INTERNAL_5M", "stale": False, "reasons": [], "bar_at": "x"}
        with mock.patch.object(server, "build_chart_bars", return_value=(self._falling(), lin)):
            f = fields(ctx, yf_rising)
        self.assertEqual(f["chart_lineage"]["intraday_source"], "TACHIBANA_INTERNAL_5M")
        self.assertEqual(f["comp"]["fiveMinStructure"], 0.0)       # 立花足は下落構造→構造点なし（遅延足の上昇構造15点を使わない）
        self.assertEqual(f["fusion"]["source"]["intraday_source"], "TACHIBANA_INTERNAL_5M")

    def test_stale_delayed_bars_keep_legacy_behavior_but_are_flagged(self):
        from test_chart_context_integration import fields, strong_ctx, scaled
        from test_chart_context import pullback_ready
        yf_rising = scaled(pullback_ready(1016.0))
        ctx = strong_ctx(yf_rising["closes"][-1])
        lin = {"intraday_source": "YFINANCE_DELAYED", "stale": True, "reasons": ["遅延足のみ"], "bar_at": "x"}
        with mock.patch.object(server, "build_chart_bars", return_value=(yf_rising, lin)):
            f = fields(ctx, yf_rising)
        self.assertEqual(f["fusion"]["recommendation"], "STALE_INTRADAY")
        self.assertIn("STALE_INTRADAY", f["fusion"]["flags"])


class FusionSourceAndStaleTests(unittest.TestCase):
    def _bars(self):
        closes = [100 + i * 0.1 for i in range(24)]
        return [{"open": c - 0.05, "high": c + 0.1, "low": c - 0.1, "close": c, "volume": 1000.0 + i * 50}
                for i, c in enumerate(closes)]

    def test_source_metadata_is_attached(self):
        lin = {"intraday_source": "TACHIBANA_INTERNAL_5M", "bar_at": "2026-09-26T10:05:00+09:00", "stale": False,
               "gap_bars": 0, "internal_bars": 24, "yf_bars_used": 0, "quote_at": "2026-09-26T10:08:01+09:00", "reasons": []}
        daily = {"closes": [100 + i for i in range(100)], "highs": [101 + i for i in range(100)],
                 "lows": [99 + i for i in range(100)]}
        f = tf.evaluate_fusion(self._bars(), quote={"t": 102.3}, lineage=lin, daily=daily, daily_source="TACHIBANA_DAILY",
                               analyzed_at="2026-09-26T10:08:03+09:00")
        s = f["source"]
        self.assertEqual(s["intraday_source"], "TACHIBANA_INTERNAL_5M")
        self.assertEqual(s["daily_source"], "TACHIBANA_DAILY")
        self.assertEqual((s["quote_at"], s["bar_at"], s["analyzed_at"]),
                         ("2026-09-26T10:08:01+09:00", "2026-09-26T10:05:00+09:00", "2026-09-26T10:08:03+09:00"))
        self.assertEqual(f["flags"], [])
        self.assertEqual(tf.compact(f)["source"]["intraday_source"], "TACHIBANA_INTERNAL_5M")

    def test_stale_intraday_lowers_confidence_caps_score_and_blocks_entry_support(self):
        lin = {"intraday_source": "YFINANCE_DELAYED", "bar_at": "2026-09-26T10:20:00+09:00", "stale": True,
               "reasons": ["遅延足のみ"]}
        chart = {"pattern": "PULLBACK_READY", "entry_timing_score": 80,
                 "features": {"higherHighs": True, "higherLows": True, "slope6": 1.0, "aboveVwap": True, "volRatioLast": 1.6}}
        f = tf.evaluate_fusion(self._bars(), chart=chart, quote={"t": 102.3}, lineage=lin,
                               market={"marketRS": 3.0, "sectorLead": True})
        self.assertIn("STALE_INTRADAY", f["flags"])
        self.assertEqual(f["confidence"], "LOW")
        self.assertLessEqual(f["confluence"]["score"], 50.0)
        self.assertEqual(f["recommendation"], "STALE_INTRADAY")
        self.assertTrue(f["source"]["stale_intraday"])

    def test_no_daily_means_daily_source_none(self):
        f = tf.evaluate_fusion(self._bars(), quote={"t": 102.3}, lineage={"intraday_source": "TACHIBANA_INTERNAL_5M"},
                               daily_source="TACHIBANA_DAILY")
        self.assertIsNone(f["source"]["daily_source"])          # 日足を渡していないのに出どころだけ主張しない

    def test_technical_fusion_is_a_pure_function_without_data_fetching(self):
        src = inspect.getsource(tf)
        for banned in ("import yfinance", "import requests", "import urllib", "tachibana_api", "import server", "yf."):
            self.assertNotIn(banned, src)


if __name__ == "__main__":
    unittest.main()
