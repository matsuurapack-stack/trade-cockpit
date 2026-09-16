# Trade Learning Phase C（2026-09-16新規）：ENTRY_QUALITY / STOP_QUALITY / EXIT_QUALITY /
# REENTRY_QUALITYの4軸独立評価の回帰テスト。6227 AIメカテック実例（5600円ENTRY→5540円EXIT→
# 14:10回復→14:50出来高急増→当日高値5700円）を基準ケースとする。
#
# 最重要の検証事項（ユーザー指示）：
#   「損切りしたあと上がった → STOPが悪かった」という結果論の一括学習を絶対にさせないこと。
#   このためSTOP_QUALITY/ENTRY_QUALITYはP&L・MAE・exit後の値動きを一切参照できない設計に
#   なっている（関数シグネチャ自体にそれらの引数が無い）ことをテストで裏付ける。
#   REENTRY_QUALITYだけは例外的にexit後のデータを使ってよいが、各バーの判定はそのバーまでの
#   情報だけで行う（未来情報リーク禁止）ことをバーごと truncation で検証する。
#
# 実行方法： cd files && python -m unittest test_trade_learning_quality_axes -v

import unittest

import server

T0 = 1700000000


def _bar(i, o, h, l, c, v):
    return {"time": T0 + i * 300, "open": o, "high": h, "low": l, "close": c, "volume": v}


def _6227_like_post_exit_trigger_bars():
    """6227実例（13:02 ENTRY5600円→5540円EXIT→14:10回復→14:50出来高急増→高値5700円）を
    模した合成5分足。実データそのものではなく、レンジ→出来高を伴う高値ブレイクという
    構造だけを再現した合成データ（実測値の再利用ではない、推測データである点に注意）。"""
    pre_exit = [
        _bar(0, 5600, 5610, 5595, 5600, 12000), _bar(1, 5600, 5605, 5570, 5580, 11000),
        _bar(2, 5580, 5585, 5555, 5560, 10000), _bar(3, 5560, 5565, 5540, 5545, 9500),
        _bar(4, 5545, 5550, 5535, 5540, 9000), _bar(5, 5540, 5545, 5525, 5530, 8500),
        _bar(6, 5530, 5540, 5520, 5535, 8200), _bar(7, 5535, 5545, 5525, 5538, 8000),
        _bar(8, 5538, 5545, 5530, 5540, 8300), _bar(9, 5540, 5545, 5535, 5540, 8100),
    ]
    range_period = [
        _bar(10, 5540, 5560, 5535, 5555, 8600), _bar(11, 5555, 5575, 5550, 5565, 8300),
        _bar(12, 5565, 5580, 5555, 5570, 8100), _bar(13, 5570, 5578, 5558, 5562, 8400),
        _bar(14, 5562, 5572, 5550, 5558, 8200), _bar(15, 5558, 5566, 5548, 5560, 8000),
        _bar(16, 5560, 5568, 5552, 5562, 7900), _bar(17, 5562, 5570, 5555, 5565, 8100),
        _bar(18, 5565, 5572, 5558, 5568, 8000), _bar(19, 5568, 5575, 5560, 5570, 7950),
    ]
    breakout = [
        _bar(20, 5570, 5650, 5568, 5645, 25000),  # TRIGGER候補：ミクロ高値ブレイク＋出来高急増
        _bar(21, 5645, 5665, 5630, 5660, 20000),  # CONFIRMED_BREAK候補
        _bar(22, 5660, 5720, 5655, 5700, 22000),  # CHASE候補
        _bar(23, 5700, 5740, 5690, 5720, 18000),
        _bar(24, 5720, 5760, 5710, 5750, 15000),
    ]
    return pre_exit, range_period, breakout


class DetectReentryStageSequenceTriggerTests(unittest.TestCase):
    def test_trigger_confirmed_break_and_chase_detected_in_order(self):
        pre_exit, range_period, breakout = _6227_like_post_exit_trigger_bars()
        bars = pre_exit + range_period + breakout
        seq = server.detect_reentry_stage_sequence(bars, exit_idx=9, exit_price=5540.0)
        self.assertEqual(seq["dataQuality"], "RECONSTRUCTED_5M")
        self.assertIsNotNone(seq["trigger"])
        self.assertEqual(seq["trigger"]["price"], 5645)
        self.assertTrue(seq["trigger"]["hardGate"]["volumeReexpansion"])
        self.assertTrue(seq["trigger"]["hardGate"]["microHighBreak"])
        self.assertIsNotNone(seq["confirmedBreak"])
        self.assertIsNotNone(seq["chase"])
        # 段階は時系列順に記録される
        stage_names = [s["stage"] for s in seq["stages"]]
        self.assertEqual(stage_names, sorted(stage_names, key=lambda s: {
            "EARLY_SETUP": 0, "TRIGGER": 1, "CONFIRMED_BREAK": 2, "CHASE": 3}[s]))

    def test_no_lookahead_truncating_future_bars_does_not_change_trigger_detection(self):
        """未来情報リーク防止の核心テスト：TRIGGER検出後に続くバー（CONFIRMED_BREAK/CHASE用）を
        削っても、TRIGGERの検出結果（時刻・価格・hardGate）は一切変わらない——つまりTRIGGER
        判定はそのバーまでの情報だけで完結しており、後に続くデータを先読みしていないことを保証する。"""
        pre_exit, range_period, breakout = _6227_like_post_exit_trigger_bars()
        full_bars = pre_exit + range_period + breakout
        truncated_bars = pre_exit + range_period + breakout[:1]  # TRIGGERバー直後で打ち切り
        seq_full = server.detect_reentry_stage_sequence(full_bars, exit_idx=9, exit_price=5540.0)
        seq_truncated = server.detect_reentry_stage_sequence(truncated_bars, exit_idx=9, exit_price=5540.0)
        self.assertEqual(seq_full["trigger"], seq_truncated["trigger"])

    def test_flat_range_after_exit_yields_no_trigger(self):
        pre_exit, _range_period, _breakout = _6227_like_post_exit_trigger_bars()
        flat_post_exit = [
            _bar(10, 5540, 5548, 5535, 5542, 8000), _bar(11, 5542, 5548, 5536, 5540, 7900),
            _bar(12, 5540, 5546, 5535, 5541, 7800), _bar(13, 5541, 5547, 5536, 5542, 7850),
            _bar(14, 5542, 5548, 5537, 5543, 7800), _bar(15, 5543, 5548, 5538, 5542, 7750),
            _bar(16, 5542, 5547, 5537, 5541, 7700), _bar(17, 5541, 5546, 5536, 5540, 7650),
            _bar(18, 5540, 5545, 5535, 5539, 7600), _bar(19, 5539, 5544, 5534, 5538, 7550),
        ]
        bars = pre_exit + flat_post_exit
        seq = server.detect_reentry_stage_sequence(bars, exit_idx=9, exit_price=5540.0)
        self.assertIsNone(seq["trigger"])
        # レンジ収縮のみ＝EARLY_SETUP(RANGE_COMPRESSION)は検出されてよい
        self.assertEqual((seq["earlySetup"] or {}).get("setupType"), "RANGE_COMPRESSION")

    def test_insufficient_post_exit_bars_returns_no_data(self):
        pre_exit, _range_period, _breakout = _6227_like_post_exit_trigger_bars()
        bars = pre_exit + [_bar(10, 5540, 5548, 5535, 5542, 8000)]  # 1本のみ
        seq = server.detect_reentry_stage_sequence(bars, exit_idx=9, exit_price=5540.0)
        self.assertEqual(seq["dataQuality"], "NO_DATA")
        self.assertIsNone(seq["trigger"])

    def test_no_exit_idx_or_bars_returns_no_data(self):
        self.assertEqual(server.detect_reentry_stage_sequence(None, 9, 5540.0)["dataQuality"], "NO_DATA")
        self.assertEqual(server.detect_reentry_stage_sequence([], None, 5540.0)["dataQuality"], "NO_DATA")


class EvaluateReentryQualityTests(unittest.TestCase):
    def test_trigger_detected_classification(self):
        pre_exit, range_period, breakout = _6227_like_post_exit_trigger_bars()
        bars = pre_exit + range_period + breakout
        seq = server.detect_reentry_stage_sequence(bars, exit_idx=9, exit_price=5540.0)
        axis = server.evaluate_reentry_quality(seq)
        self.assertEqual(axis["classification"], "TRIGGER_DETECTED")
        self.assertEqual(axis["evidence"]["trigger"]["price"], 5645)

    def test_no_trigger_classification(self):
        axis = server.evaluate_reentry_quality({"dataQuality": "RECONSTRUCTED_5M", "trigger": None,
                                                  "earlySetup": None, "confirmedBreak": None, "chase": None,
                                                  "postExitBarCount": 10})
        self.assertEqual(axis["classification"], "NO_TRIGGER_DETECTED")

    def test_unknown_when_no_data(self):
        axis = server.evaluate_reentry_quality({"dataQuality": "NO_DATA"})
        self.assertEqual(axis["classification"], "UNKNOWN")


class EntryQualityNoHindsightTests(unittest.TestCase):
    """ENTRY_QUALITY軸：結果（P&L・MAE・exit後の値動き）を一切参照できないことを、関数の
    引数構造そのもので裏付ける（結果論での評価を混入させないための設計保証）。"""

    def test_signature_takes_only_pre_entry_context(self):
        import inspect
        params = list(inspect.signature(server.evaluate_entry_quality).parameters)
        self.assertEqual(params, ["ctx"])  # gross_pnl/mae/exit系の引数が無いことを保証

    def test_good_timing_breakout_no_negative_tags(self):
        ctx = {"data_quality": "RECONSTRUCTED_5M", "entry_setup_type": "BREAKOUT",
               "rsi_at_entry": 55, "trend_5m_before_entry": "UP", "distance_from_high_pct": -0.1,
               "above_vwap_at_entry": True}
        axis = server.evaluate_entry_quality(ctx)
        self.assertEqual(axis["classification"], "GOOD_TIMING")
        self.assertEqual(axis["evidence"], [])

    def test_chase_entry_high_rsi_flagged(self):
        ctx = {"data_quality": "RECONSTRUCTED_5M", "entry_setup_type": "CHASE",
               "rsi_at_entry": 78.6, "trend_5m_before_entry": "UP", "distance_from_high_pct": -0.5,
               "above_vwap_at_entry": True}
        axis = server.evaluate_entry_quality(ctx)
        self.assertIn("CHASE_ENTRY", axis["evidence"])

    def test_unknown_when_no_data(self):
        axis = server.evaluate_entry_quality({"data_quality": "NO_DATA"})
        self.assertEqual(axis["classification"], "UNKNOWN")

    def test_6227_like_entry_context_is_questionable_not_hindsight_graded(self):
        """6227実例に近い（13:02の遅めのENTRY、RSI高め、当日高値から離れていない＝BREAKOUT寄り
        ではなくCHASE寄り）pre-entryコンテキストで評価する。事後の5540円EXITや5700円到達といった
        情報は一切渡していない＝関数はそれらを知り得ない設計になっていることの実例確認。"""
        ctx = {"data_quality": "RECONSTRUCTED_5M", "entry_setup_type": "CHASE",
               "rsi_at_entry": 78.6, "trend_5m_before_entry": "UP", "distance_from_high_pct": -1.8,
               "above_vwap_at_entry": True}
        axis = server.evaluate_entry_quality(ctx)
        self.assertIn(axis["classification"], ("QUESTIONABLE_TIMING", "CHASE_OR_LATE_ENTRY"))


class StopQualityStructuralOnlyTests(unittest.TestCase):
    """STOP_QUALITY軸：P&L・MAE・exit後の値動きを一切参照できないことを関数シグネチャで保証し、
    実STOPが記録されていないトレード（6227含む）はUNKNOWNのままになることを検証する。"""

    def test_signature_excludes_outcome_fields(self):
        import inspect
        params = list(inspect.signature(server.evaluate_stop_quality).parameters)
        self.assertEqual(params, ["ctx", "stop_quality_evidence", "initial_stop_price", "entry_price"])
        for forbidden in ("gross_pnl", "mae", "pnl", "exit_price"):
            self.assertNotIn(forbidden, params)

    def test_6227_unknown_stop_evidence_stays_unknown(self):
        """6227は実STOP未記録（Phase B以前のトレード）＝stop_quality_evidence=UNKNOWNのため、
        STOP_QUALITYも常にUNKNOWN。ここで「5540円は結果的に浅かった」等の推測を絶対にしない。"""
        ctx = {"data_quality": "RECONSTRUCTED_5M", "pre_entry_avg_bar_range_pct": 0.3}
        axis = server.evaluate_stop_quality(ctx, "UNKNOWN", None, 5600.0)
        self.assertEqual(axis["classification"], "UNKNOWN")

    def test_actual_stop_structurally_tight(self):
        ctx = {"data_quality": "RECONSTRUCTED_5M", "pre_entry_avg_bar_range_pct": 0.5}
        # stop距離0.2% < avg_bar_range 0.5% → ratio<1.0 → 構造的に浅い
        axis = server.evaluate_stop_quality(ctx, "ACTUAL_STOP", 5589.0, 5600.0)
        self.assertEqual(axis["classification"], "STRUCTURALLY_TIGHT")

    def test_actual_stop_structurally_reasonable(self):
        ctx = {"data_quality": "RECONSTRUCTED_5M", "pre_entry_avg_bar_range_pct": 0.5}
        # stop距離1.0% / avg_bar_range 0.5% = ratio 2.0 → 妥当範囲
        axis = server.evaluate_stop_quality(ctx, "ACTUAL_STOP", 5544.0, 5600.0)
        self.assertEqual(axis["classification"], "STRUCTURALLY_REASONABLE")

    def test_actual_stop_structurally_wide(self):
        ctx = {"data_quality": "RECONSTRUCTED_5M", "pre_entry_avg_bar_range_pct": 0.2}
        axis = server.evaluate_stop_quality(ctx, "ACTUAL_STOP", 5488.0, 5600.0)  # 2%幅 / 0.2%=ratio10
        self.assertEqual(axis["classification"], "STRUCTURALLY_WIDE")

    def test_no_volatility_reference_falls_back_to_unknown(self):
        ctx = {"data_quality": "RECONSTRUCTED_5M", "pre_entry_avg_bar_range_pct": None}
        axis = server.evaluate_stop_quality(ctx, "ACTUAL_STOP", 5540.0, 5600.0)
        self.assertEqual(axis["classification"], "UNKNOWN")


class ExitQualityExecutionOnlyTests(unittest.TestCase):
    def test_stop_honored(self):
        axis = server.evaluate_exit_quality("ACTUAL_STOP", 5540.0, 5541.0)
        self.assertEqual(axis["classification"], "STOP_HONORED")

    def test_discretionary_early_exit(self):
        axis = server.evaluate_exit_quality("ACTUAL_STOP", 5540.0, 5580.0)
        self.assertEqual(axis["classification"], "DISCRETIONARY_EARLY_EXIT")

    def test_slippage_beyond_stop(self):
        axis = server.evaluate_exit_quality("ACTUAL_STOP", 5540.0, 5500.0)
        self.assertEqual(axis["classification"], "SLIPPAGE_BEYOND_STOP")

    def test_unknown_without_actual_stop(self):
        axis = server.evaluate_exit_quality("UNKNOWN", None, 5540.0)
        self.assertEqual(axis["classification"], "UNKNOWN")


class EvaluateTradeQualityAxesIndependenceTests(unittest.TestCase):
    """4軸が互いに独立していること（1つの軸の悪化が他の軸へ波及しない）を検証する——
    6227は「STOP_QUALITY/EXIT_QUALITYはUNKNOWNだが、ENTRY_QUALITY/REENTRY_QUALITYは
    独立に評価できる」という組み合わせが起きるはずのケース。"""

    def test_unknown_stop_does_not_force_other_axes_unknown(self):
        ctx = {"data_quality": "RECONSTRUCTED_5M", "entry_setup_type": "BREAKOUT",
               "rsi_at_entry": 55, "trend_5m_before_entry": "UP", "distance_from_high_pct": -0.1,
               "above_vwap_at_entry": True, "pre_entry_avg_bar_range_pct": 0.4}
        pre_exit, range_period, breakout = _6227_like_post_exit_trigger_bars()
        bars = pre_exit + range_period + breakout
        seq = server.detect_reentry_stage_sequence(bars, exit_idx=9, exit_price=5540.0)
        axes = server.evaluate_trade_quality_axes(ctx, seq, stop_quality_evidence="UNKNOWN",
                                                    initial_stop_price=None, final_stop_price=None,
                                                    entry_price=5600.0, exit_price=5540.0)
        self.assertEqual(axes["stop_quality"]["classification"], "UNKNOWN")
        self.assertEqual(axes["exit_quality"]["classification"], "UNKNOWN")
        self.assertEqual(axes["entry_quality"]["classification"], "GOOD_TIMING")
        self.assertEqual(axes["reentry_quality"]["classification"], "TRIGGER_DETECTED")


if __name__ == "__main__":
    unittest.main()
