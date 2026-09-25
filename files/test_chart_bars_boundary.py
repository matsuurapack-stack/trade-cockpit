# 立花足へ接続した5分足の「混在」と「境界」の厳格テスト、および5分足構造入力切替のshadow比較のテスト。
# 実行： cd files && python -m unittest test_chart_bars_boundary -v
import datetime
import unittest
from unittest import mock

import chart_context as cc
import movement_potential as mp
import server
import technical_fusion as tf
from test_chart_bars_lineage import dt, internal, yf_bars

JST = server._JST


def _tick(code, hh, mm, ss, price, vol):
    server._record_quote_tick(code, {"t": price, "volume": vol, "quote_timestamp": dt(hh, mm, ss).isoformat()})


def _store_bars(code):
    return [server._INTRADAY_BAR_STORE[code]["bars"][k] for k in sorted(server._INTRADAY_BAR_STORE[code]["bars"])]


def wild_yf(n):
    """遅延したyfinance足：値が大きく荒れている（立花足と混ざるとRSI/ATR等が明確に狂う）。"""
    out = yf_bars(9, 0, n, base=90.0)
    for i in range(n):
        sign = 1 if i % 2 == 0 else -1
        out["closes"][i] = 90.0 + sign * 7.0
        out["highs"][i] = out["closes"][i] + 4.0
        out["lows"][i] = out["closes"][i] - 4.0
        out["opens"][i] = 90.0
        out["volumes"][i] = 9000.0
    return out


def smooth_internal(start, n):
    out, t = [], start
    for i in range(n):
        c = 100.0 + i * 0.12 + (0.05 if i % 3 == 0 else 0.0)
        out.append({"start": t.isoformat(), "open": c - 0.06, "high": c + 0.10, "low": c - 0.11, "close": c,
                    "volume": 800.0 + i * 10, "ticks": 4})
        t += datetime.timedelta(minutes=5)
    return out


class SourceMixingTests(unittest.TestCase):
    """YF_HISTORY+TACHIBANA_INTERNAL_5M のとき、直近部分は立花足だけから計算されること。"""

    def _merge(self, n_internal=30, n_yf=8):
        yf = wild_yf(n_yf)                                 # 09:00〜（遅延した荒れた履歴）
        first = dt(9, 0) + datetime.timedelta(minutes=5 * n_yf)
        ib = smooth_internal(first, n_internal)
        with mock.patch.object(server, "get_internal_intraday_bars", return_value={"bars": ib}):
            bars, lin = server.build_chart_bars("627A", yf, now=first + datetime.timedelta(minutes=5 * n_internal + 2))
        return bars, lin, ib

    def test_last_n_bars_are_exactly_the_tachibana_bars(self):
        bars, lin, ib = self._merge()
        n = len(ib)
        self.assertEqual(lin["intraday_source"], "YF_HISTORY+TACHIBANA_INTERNAL_5M")
        self.assertEqual(bars["closes"][-n:], [b["close"] for b in ib])
        self.assertEqual(bars["highs"][-n:], [b["high"] for b in ib])
        self.assertEqual(bars["volumes"][-n:], [b["volume"] for b in ib])
        self.assertEqual(bars["starts"][-n:], [b["start"] for b in ib])      # yfinanceの遅延終端は残っていない

    def test_short_window_features_use_tachibana_only(self):
        """立花足が十分ある（>=lookback）とき、Chart Contextの特徴量は立花足だけで計算したものと完全一致する。"""
        bars, lin, ib = self._merge(n_internal=30)
        internal_only = server._internal_to_arrays(ib)
        f_merged = cc.compute_features(bars, quote={"t": bars["closes"][-1]})
        f_internal = cc.compute_features(internal_only, quote={"t": internal_only["closes"][-1]})
        self.assertEqual(f_merged, f_internal)               # slope・higherHighs・上ヒゲ・breakoutLevel・VWAP乖離…全て一致
        # RSI / ATR / 足の形 / 出来高比 も同様
        self.assertEqual(tf.rsi_series(bars["closes"])[-1], tf.rsi_series(internal_only["closes"])[-1])
        self.assertEqual(tf.stochastic_k(cc.normalize_bars(bars)), tf.stochastic_k(cc.normalize_bars(internal_only)))
        a1 = mp.compute_movement_features(bars, cur=bars["closes"][-1], vwap=None, day_high=None, day_low=None)["atr5"]
        a2 = mp.compute_movement_features(internal_only, cur=internal_only["closes"][-1], vwap=None, day_high=None, day_low=None)["atr5"]
        self.assertEqual(a1, a2)
        self.assertEqual(tf.candle_signals(cc.normalize_bars(bars)), tf.candle_signals(cc.normalize_bars(internal_only)))
        # ブレイク/押し目/S-Rの判定（chartのpattern・breakout状態）
        c1 = cc.evaluate_chart_context(bars, quote={"t": bars["closes"][-1]})
        c2 = cc.evaluate_chart_context(internal_only, quote={"t": internal_only["closes"][-1]})
        self.assertEqual(c1["pattern"], c2["pattern"])
        self.assertEqual(c1["entry_timing_score"], c2["entry_timing_score"])
        f1 = tf.evaluate_fusion(bars, chart=c1, quote={"t": bars["closes"][-1]})
        f2 = tf.evaluate_fusion(internal_only, chart=c2, quote={"t": internal_only["closes"][-1]})
        self.assertEqual(f1["candles"], f2["candles"])
        self.assertEqual(f1["breakout"], f2["breakout"])
        self.assertEqual(f1["groups"]["momentum"]["rsi"], f2["groups"]["momentum"]["rsi"])
        self.assertEqual(f1["groups"]["trend"]["trendState"], f2["groups"]["trend"]["trendState"])

    def test_wild_yfinance_history_does_not_leak_into_recent_window(self):
        bars, _, ib = self._merge(n_internal=20)
        recent = cc.normalize_bars({k: v[-20:] for k, v in bars.items() if k != "starts"})
        self.assertTrue(all(abs(c - 100.0) < 4 for c in recent["closes"]))     # 荒れた履歴値(83/97等)が直近窓に無い
        self.assertLess(max(recent["highs"]), 110)

    def test_yfinance_terminal_after_first_tachibana_bar_is_discarded(self):
        yf = wild_yf(14)                                   # 09:00〜10:05（遅延足のはずが立花足と重なる）
        ib = smooth_internal(dt(9, 50), 6)                 # 09:50〜
        with mock.patch.object(server, "get_internal_intraday_bars", return_value={"bars": ib}):
            bars, lin = server.build_chart_bars("627A", yf, now=dt(10, 20))
        self.assertEqual(lin["yf_bars_used"], 10)
        self.assertTrue(all(c < 105 for c in bars["closes"][-6:]))
        self.assertEqual(len(bars["closes"]), 16)


class BootstrapBoundaryTests(unittest.TestCase):
    def setUp(self):
        server._INTRADAY_BAR_STORE.clear()

    def test_timezone_mismatch_is_normalised(self):
        yf = yf_bars(9, 0, 10)
        # yfinanceの時刻がUTC表記（09:50JST == 00:50Z）でも同じスロットとして扱う
        yf["starts"] = [(dt(9, 0) + datetime.timedelta(minutes=5 * i)).astimezone(datetime.timezone.utc).isoformat() for i in range(10)]
        ib = internal(dt(9, 50), 3)
        with mock.patch.object(server, "get_internal_intraday_bars", return_value={"bars": ib}):
            bars, lin = server.build_chart_bars("627A", yf, now=dt(10, 5))
        self.assertEqual(lin["yf_bars_used"], 10)
        self.assertEqual(lin["gap_bars"], 0)
        self.assertEqual(len(bars["closes"]), 13)

    def test_same_slot_is_merged_once_without_double_counting_volume(self):
        yf = yf_bars(9, 0, 11)                             # 09:00〜09:50（09:50の完成足あり）
        yf["volumes"][-1] = 4000.0
        ib = internal(dt(9, 50), 3)                        # 立花の09:50足は途中から始まった足（出来高500）
        with mock.patch.object(server, "get_internal_intraday_bars", return_value={"bars": ib}):
            bars, lin = server.build_chart_bars("627A", yf, now=dt(10, 3))
        self.assertEqual(len(bars["closes"]), 13)          # 10本の履歴 + 立花3本（09:50は1本に統合）
        self.assertEqual(len(set(bars["starts"])), 13)
        i = bars["starts"].index(dt(9, 50).isoformat())
        self.assertEqual(bars["volumes"][i], 4000.0)       # 二重計上せず大きい方
        self.assertEqual(bars["closes"][i], 102.1)         # closeは立花（最新）
        self.assertTrue(any("統合" in r for r in lin["reasons"]))

    def test_lunch_break_boundary(self):
        yf = yf_bars(11, 0, 6)                             # 11:00〜11:25
        ib = internal(dt(12, 30), 3)
        with mock.patch.object(server, "get_internal_intraday_bars", return_value={"bars": ib}):
            _, lin = server.build_chart_bars("627A", yf, now=dt(12, 45))
        self.assertEqual(lin["gap_bars"], 0)               # 昼休みは欠落ではない
        self.assertFalse(lin["stale"])

    def test_open_boundary_0900_starts_bar_and_close_boundary_1530_is_excluded(self):
        _tick("A", 9, 0, 0, 100.0, 1000)
        _tick("A", 9, 0, 40, 100.5, 1300)
        _tick("A", 15, 29, 59, 101.0, 90000)               # 大引け直前：15:25の足
        _tick("A", 15, 30, 0, 101.2, 91000)                # 15:30ちょうど以降は場中ではない
        bars = _store_bars("A")
        self.assertTrue(bars[0]["start"].startswith("2026-09-26T09:00:00"))
        self.assertTrue(bars[-1]["start"].startswith("2026-09-26T15:25:00"))
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[-1]["close"], 101.0)         # 15:30の値は取り込まない

    def test_first_tick_after_restart_creates_no_fake_volume_bar(self):
        _tick("B", 10, 12, 0, 500.0, 8_000_000)            # 再起動直後：当日累積は既に800万株
        _tick("B", 10, 12, 20, 500.5, 8_000_400)
        bars = _store_bars("B")
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0]["volume"], 400.0)         # 800万株を偽の出来高にしない（増分だけ）

    def test_lunch_reopen_cumulative_jump_is_not_added(self):
        _tick("C", 11, 28, 0, 200.0, 1_000_000)
        _tick("C", 11, 29, 30, 200.2, 1_001_000)
        _tick("C", 12, 30, 5, 200.8, 1_060_000)            # 昼休み明け：累積の差分(5.9万株)を1本の足に載せない
        _tick("C", 12, 30, 35, 201.0, 1_060_500)
        bars = _store_bars("C")
        morning, afternoon = bars[0], bars[-1]
        self.assertEqual(morning["volume"], 1000.0)
        self.assertEqual(afternoon["volume"], 500.0)       # 通常の増分だけ
        self.assertTrue(afternoon.get("volume_gap"))
        lin = None
        with mock.patch.object(server, "get_internal_intraday_bars", return_value={"bars": bars}):
            _, lin = server.build_chart_bars("C", None, now=dt(12, 40))
        self.assertEqual(lin["volume_gap_bars"], 1)

    def test_polling_gap_within_session_is_not_added_as_one_bar(self):
        _tick("D", 9, 30, 0, 300.0, 100_000)
        _tick("D", 9, 30, 30, 300.2, 100_300)
        _tick("D", 10, 10, 0, 301.0, 400_000)              # 40分取得が空いた：30万株は複数本分
        bars = _store_bars("D")
        self.assertEqual(bars[-1]["volume"], 0.0)
        self.assertTrue(bars[-1].get("volume_gap"))
        _tick("D", 10, 10, 30, 301.1, 400_200)             # 以降は通常の差分に戻る
        self.assertEqual(_store_bars("D")[-1]["volume"], 200.0)

    def test_cumulative_volume_decrease_is_ignored(self):
        _tick("E", 10, 0, 0, 100.0, 5000)
        _tick("E", 10, 0, 20, 100.1, 4000)                 # 異常（累積が減る）
        self.assertEqual(_store_bars("E")[0]["volume"], 0.0)


class StructureShadowTests(unittest.TestCase):
    def _falling(self, n=24):
        closes = [110 - i * 0.3 for i in range(n)]
        return {"closes": closes, "highs": [c + 0.1 for c in closes], "lows": [c - 0.1 for c in closes],
                "opens": [c + 0.05 for c in closes], "volumes": [1000.0] * n}

    def test_structure_change_is_logged_with_old_vs_new_scores(self):
        from test_chart_context import pullback_ready
        from test_chart_context_integration import fields, scaled, strong_ctx
        yf_rising = scaled(pullback_ready(1016.0))
        ctx = strong_ctx(yf_rising["closes"][-1])
        lin = {"intraday_source": "TACHIBANA_INTERNAL_5M", "stale": False, "reasons": [], "bar_at": "x"}
        with mock.patch.object(server, "build_chart_bars", return_value=(self._falling(), lin)):
            f = fields(ctx, yf_rising)
        ss = f["structure_shadow"]
        self.assertEqual(ss["oldStructure"], "higher_highs")
        self.assertEqual(ss["newStructure"], "lower_lows")
        self.assertEqual(ss["oldStructureScore"], 15.0)
        self.assertEqual(ss["newStructureScore"], 0.0)
        self.assertEqual(ss["structureScoreDelta"], -15.0)
        self.assertGreater(ss["oldEntryScore"], ss["newEntryScore"])
        self.assertEqual(ss["entryScoreDelta"], ss["newEntryScore"] - ss["oldEntryScore"])
        self.assertEqual(f["comp"]["fiveMinStructure"], 0.0)          # 本番は新入力（旧値へは戻さない）
        self.assertEqual(ss["source"], "TACHIBANA_INTERNAL_5M")

    def test_no_shadow_when_no_override(self):
        from test_chart_context import pullback_ready
        from test_chart_context_integration import fields, scaled, strong_ctx
        b = scaled(pullback_ready(1016.0))
        f = fields(strong_ctx(b["closes"][-1]), b)          # 立花足なし（UNTIMED）→ 構造は上書きされない
        self.assertIsNone(f["structure_shadow"])

    def test_summary_reports_rank_and_top5_before_after(self):
        def cand(code, new, old, new_state, old_state):
            return {"code": code, "entryScore": new, "entryState": new_state, "current": 100.0, "changePct": 1.0,
                    "structureShadow": {"oldEntryScore": old, "oldEntryState": old_state, "newEntryScore": new,
                                        "newEntryState": new_state, "oldStructure": "higher_highs", "newStructure": "lower_lows",
                                        "entryScoreDelta": new - old}}
        cands = [cand("A", 70, 70, "ENTRY_READY", "ENTRY_READY"), cand("B", 62, 85, "WAIT_PULLBACK", "ENTRY_READY"),
                 cand("C", 40, 40, "WATCH", "WATCH")]
        cands.sort(key=lambda c: -c["entryScore"])
        with mock.patch.object(server, "_select_entry_ready_top5",
                               side_effect=lambda lst: ([c for c in sorted(lst, key=lambda x: -x["entryScore"])
                                                          if c["entryState"] == "ENTRY_READY"][:5], [], [], [], {})):
            out = server.summarize_structure_shadow(cands)
        by = {c["code"]: c["structureShadow"] for c in cands}
        self.assertEqual((by["B"]["rankOld"], by["B"]["rankNew"]), (1, 2))   # 旧入力ならBが1位
        self.assertEqual(out["entryStateChanged"], 1)
        self.assertEqual(out["top5Old"], ["B", "A"])
        self.assertEqual(out["top5New"], ["A"])
        self.assertTrue(out["top5Changed"])


if __name__ == "__main__":
    unittest.main()
