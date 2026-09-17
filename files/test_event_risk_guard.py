# Event Risk Guard（2026-09-17新規、Event Risk Guard Phase A）テスト。
#
# 2026-09-17の実トレード反省（FOMC通過後の上昇を見て翌日の日銀会合を楽観視し、通常どおり
# エントリーしてしまった）を機械的に検知できるようにする機能。
# 単なる「イベント当日」だけでHIGHにはせず、「連続重要イベント」（前日重要イベント
# RELEASED＋本日別の重要イベントIMMINENT/IN_PROGRESS＋市場GU）の組み合わせを明示的に
# 検証する（レビュー指摘2）。ENTRY判定（_entry_score_components/_classify_entry_state）への
# 統合と、9:00直後WAIT→確認後ENTRY候補化可能という状態遷移も検証する。
#
# 実行方法： cd files && python -m unittest test_event_risk_guard -v

import unittest

import server


def _macro(events):
    return {"events": events, "market_event_risk": "LOW"}


class ClassifyEventGuardLevelTests(unittest.TestCase):
    def test_thresholds(self):
        self.assertEqual(server.classify_event_guard_level(0), "LOW")
        self.assertEqual(server.classify_event_guard_level(29.9), "LOW")
        self.assertEqual(server.classify_event_guard_level(30), "MEDIUM")
        self.assertEqual(server.classify_event_guard_level(49.9), "MEDIUM")
        self.assertEqual(server.classify_event_guard_level(50), "HIGH")
        self.assertEqual(server.classify_event_guard_level(100), "HIGH")


class MarketComponentSequentialMajorEventsTests(unittest.TestCase):
    """レビュー指摘2：単なる「イベント当日」だけでは常にHIGHにしない。連続重要イベント
    （前日RELEASED＋本日IMMINENT/IN_PROGRESS＋市場GU）の組み合わせでのみ追加加点する。"""

    def test_event_day_alone_is_not_high(self):
        # 本日イベント当日のみ（前日の重要イベント通過も市場GUも無い）→+20のみ、LOW域に留まる
        # （単なる「イベント当日」だけでHIGH/MEDIUMへ引き上げない、という設計の確認）。
        active = _macro([{"event_status": "IMMINENT", "importance": "HIGH", "title": "日銀政策決定会合"}])
        m = server.compute_event_risk_guard_market(active, prior_day_events=None, market_gu_pct=None)
        self.assertEqual(m["score"], 20.0)
        self.assertEqual(server.classify_event_guard_level(m["score"]), "LOW")
        self.assertNotIn("SEQUENTIAL_MAJOR_EVENTS", " ".join(m["reasons"]))

    def test_sequential_major_events_pushes_into_high_band(self):
        # 2026-09-17実例：前日FOMC RELEASED（HIGH）＋本日日銀IMMINENT＋市場GU+0.5%。
        # 市場共通分だけでMEDIUM域まで上がり、銘柄固有分（GU等）が加わって初めてHIGHへ達する
        # 設計（単独銘柄要因では届かない・市場要因だけでも届かない、の両方を要求する）。
        active = _macro([{"event_status": "IMMINENT", "importance": "HIGH", "title": "日銀政策決定会合"}])
        prior = [{"event_status": "RELEASED", "importance": "HIGH", "title": "FOMC"}]
        m = server.compute_event_risk_guard_market(active, prior_day_events=prior, market_gu_pct=0.5)
        self.assertEqual(m["score"], 40.0)  # 20（当日）+20（連続イベント）
        self.assertEqual(server.classify_event_guard_level(m["score"]), "MEDIUM")
        self.assertTrue(any("SEQUENTIAL_MAJOR_EVENTS" in r for r in m["reasons"]))

    def test_sequential_bonus_requires_market_gu(self):
        # 前日RELEASED＋本日IMMINENTは揃っていても、市場GUが無ければ連続イベント加点は付かない
        active = _macro([{"event_status": "IMMINENT", "importance": "HIGH", "title": "日銀政策決定会合"}])
        prior = [{"event_status": "RELEASED", "importance": "HIGH", "title": "FOMC"}]
        m = server.compute_event_risk_guard_market(active, prior_day_events=prior, market_gu_pct=0.0)
        self.assertEqual(m["score"], 20.0)
        self.assertFalse(any("SEQUENTIAL_MAJOR_EVENTS" in r for r in m["reasons"]))

    def test_sequential_bonus_requires_prior_day_high_importance(self):
        # 前日イベントの重要度がLOWなら連続イベント加点は付かない
        active = _macro([{"event_status": "IMMINENT", "importance": "HIGH", "title": "日銀政策決定会合"}])
        prior = [{"event_status": "RELEASED", "importance": "LOW", "title": "雑多な指標"}]
        m = server.compute_event_risk_guard_market(active, prior_day_events=prior, market_gu_pct=0.5)
        self.assertEqual(m["score"], 20.0)


class SymbolComponentTests(unittest.TestCase):
    def test_gu_and_vwap_deviation_add_score(self):
        market = {"score": 0.0, "reasons": []}
        row = {"open": 1050.0, "p": 1000.0, "t": 1060.0}  # GU +5%
        snapshot = {"vwap": 1000.0}  # VWAP乖離 6%
        g = server.compute_event_risk_guard_for_symbol("9999", market, row, stage2=None, snapshot=snapshot)
        self.assertGreater(g["score"], 0)
        self.assertTrue(any("GU" in r for r in g["reasons"]))
        self.assertTrue(any("VWAP乖離" in r for r in g["reasons"]))

    def test_confirmation_factors_reduce_score(self):
        market = {"score": 60.0, "reasons": ["本日イベント当日"]}
        row = {"open": 1000.0, "p": 1000.0, "t": 1000.0}
        snapshot = {"aboveVwap": True, "fiveMinStructure": "higher_highs"}
        stage2 = {"volumeType": "POSITIVE_VOLUME"}
        g = server.compute_event_risk_guard_for_symbol("9999", market, row, stage2=stage2, snapshot=snapshot)
        self.assertLess(g["score"], 60.0)
        self.assertTrue(any("VWAP維持" in r for r in g["reasons"]))

    def test_score_clamped_0_100(self):
        market = {"score": 90.0, "reasons": []}
        row = {"open": 1200.0, "p": 1000.0, "t": 1200.0}
        snapshot = {"vwap": 1000.0}
        g = server.compute_event_risk_guard_for_symbol("9999", market, row, stage2=None, snapshot=snapshot)
        self.assertLessEqual(g["score"], 100.0)


class EntryScoreComponentsEventGuardTests(unittest.TestCase):
    """_entry_score_componentsのriskEventが、固定-5.0ではなくevent_guardスコアに応じた
    連続値になること（既存の「小さめweight」方針＝最大-15を踏襲）。"""

    def _base_args(self):
        row = {"changePct": 1.0, "marketRS": 0.5, "code": "9999"}
        stage2 = {"timeAdjustedVolumeRatio": 1.0}
        snapshot = {"aboveVwap": True, "fiveMinStructure": "mixed"}
        return row, stage2, snapshot

    def test_no_event_guard_falls_back_to_legacy_behavior(self):
        row, stage2, snapshot = self._base_args()
        comp = server._entry_score_components(row, stage2, snapshot, set(), set(), [], ["EVENT_RISK_HIGH"])
        self.assertEqual(comp["riskEvent"], -5.0)

    def test_event_guard_high_score_scales_penalty_beyond_legacy_five(self):
        row, stage2, snapshot = self._base_args()
        event_guard = {"score": 80.0, "level": "HIGH", "reasons": []}
        comp = server._entry_score_components(row, stage2, snapshot, set(), set(), [], [], event_guard=event_guard)
        self.assertEqual(comp["riskEvent"], -12.0)  # -min(15, 80*0.15)

    def test_event_guard_penalty_capped_at_fifteen(self):
        row, stage2, snapshot = self._base_args()
        event_guard = {"score": 100.0, "level": "HIGH", "reasons": []}
        comp = server._entry_score_components(row, stage2, snapshot, set(), set(), [], [], event_guard=event_guard)
        self.assertEqual(comp["riskEvent"], -15.0)

    def test_event_guard_low_score_small_penalty(self):
        row, stage2, snapshot = self._base_args()
        event_guard = {"score": 10.0, "level": "LOW", "reasons": []}
        comp = server._entry_score_components(row, stage2, snapshot, set(), set(), [], [], event_guard=event_guard)
        self.assertEqual(comp["riskEvent"], -1.5)


class ClassifyEntryStateEventGuardTests(unittest.TestCase):
    """PHASE 5「確認前に買わない」＋PHASE 8「確認後は昇格可能」の状態遷移。
    fixtureはFOMC翌日・日銀当日・前日米株高・日経GU・銘柄GUがEVENT HIGHになることを保証する。"""

    def _sequential_event_guard(self, sector_gu_pct=1.0):
        # fixture：前日FOMC RELEASED（HIGH）＋本日日銀IMMINENT＋日経GU＋銘柄GU＋セクター全体GU
        # （ユーザー要求の5条件）でEVENT HIGHになることを保証する。
        active = _macro([{"event_status": "IMMINENT", "importance": "HIGH", "title": "日銀政策決定会合"}])
        prior = [{"event_status": "RELEASED", "importance": "HIGH", "title": "FOMC"}]
        market = server.compute_event_risk_guard_market(active, prior_day_events=prior, market_gu_pct=0.5)  # 日経GU
        row = {"open": 1010.0, "p": 1000.0, "t": 1010.0}  # 銘柄GU +1%
        return server.compute_event_risk_guard_for_symbol("9999", market, row, stage2=None, snapshot={},
                                                             sector_gu_pct=sector_gu_pct)

    def test_sequential_fixture_is_event_high(self):
        g = self._sequential_event_guard()
        self.assertEqual(g["level"], "HIGH")

    def test_nine_am_no_confirmation_stays_wait(self):
        # 9:00直後：寄り後の確認材料（VWAP維持・高値更新）がまだ無い状態ではWATCHに留まる
        event_guard = self._sequential_event_guard()
        row = {"changePct": 1.0, "marketRS": 1.0, "code": "9999"}
        stage2 = {"timeAdjustedVolumeRatio": 1.0}  # aboveRecentHighなし
        snapshot = {"aboveVwap": True, "fiveMinStructure": "higher_highs"}
        state, _ = server._classify_entry_state(75, row, stage2, snapshot, "FULL", [], False,
                                                   nikkei_chg=0.3, event_guard=event_guard)
        self.assertEqual(state, "WATCH")

    def test_no_vwap_hold_stays_wait(self):
        event_guard = self._sequential_event_guard()
        row = {"changePct": 1.0, "marketRS": 1.0, "code": "9999"}
        stage2 = {"timeAdjustedVolumeRatio": 1.0, "aboveRecentHigh": True, "distanceFromHighPct": 0.5}
        snapshot = {"aboveVwap": False, "fiveMinStructure": "mixed"}  # VWAP維持なし
        state, _ = server._classify_entry_state(75, row, stage2, snapshot, "FULL", [], False,
                                                   nikkei_chg=0.3, event_guard=event_guard)
        self.assertEqual(state, "WATCH")

    def test_vwap_hold_and_new_high_allows_entry_ready(self):
        # VWAP維持＋高値更新の確認材料が揃えばENTRY_READY/NOW_BUYABLEへ昇格できる
        event_guard = self._sequential_event_guard()
        row = {"changePct": 2.0, "marketRS": 1.0, "code": "9999"}
        stage2 = {"timeAdjustedVolumeRatio": 1.0, "aboveRecentHigh": True, "distanceFromHighPct": 0.5}
        snapshot = {"aboveVwap": True, "fiveMinStructure": "higher_highs"}
        state, _ = server._classify_entry_state(75, row, stage2, snapshot, "FULL", [], False,
                                                   nikkei_chg=0.3, event_guard=event_guard)
        self.assertIn(state, ("NOW_BUYABLE", "ENTRY_READY"))

    def test_event_medium_does_not_block_only_penalizes_score(self):
        # MEDIUMは除外しない（entry_score減点のみ、状態遷移はブロックしない）
        market = {"score": 40.0, "reasons": []}  # MEDIUM域
        event_guard = {"score": 40.0, "level": "MEDIUM", "reasons": []}
        row = {"changePct": 2.0, "marketRS": 1.0, "code": "9999"}
        stage2 = {"timeAdjustedVolumeRatio": 1.0, "aboveRecentHigh": True, "distanceFromHighPct": 0.5}
        snapshot = {"aboveVwap": True, "fiveMinStructure": "higher_highs"}
        state, _ = server._classify_entry_state(75, row, stage2, snapshot, "FULL", [], False,
                                                   nikkei_chg=0.3, event_guard=event_guard)
        self.assertIn(state, ("NOW_BUYABLE", "ENTRY_READY"))


class EvaluateEventQualityTests(unittest.TestCase):
    """PHASE 14：「損切りは良かったが、入った判断は悪かった」を独立評価する5軸目。
    レビュー指摘1：後知恵バイアス禁止——entry_time時点のctxスナップショットだけを使い、
    現在のstockQuotes/VWAP/stage2は一切参照しない（このテストでは現在データを一切渡さず、
    ctx=entry時点snapshotのみを入力にすることでそれを検証する）。"""

    def test_no_event_risk_day_is_not_applicable(self):
        ctx = {"data_quality": "RECONSTRUCTED_5M", "above_vwap_at_entry": False, "trend_5m_before_entry": "DOWN"}
        result = server.evaluate_event_quality(ctx, event_risk_at_entry="LOW")
        self.assertEqual(result["classification"], "N/A")

    def test_missing_snapshot_is_unknown_not_poor(self):
        # イベントHIGH日でもctxが無ければ「入った判断が悪かった」と決めつけずUNKNOWNにする
        result = server.evaluate_event_quality({}, event_risk_at_entry="HIGH")
        self.assertEqual(result["classification"], "UNKNOWN")

    def test_high_risk_without_confirmation_is_poor(self):
        # 2026-09-17実例：イベントHIGH日にVWAP未回復・反転未確認のままエントリー
        ctx = {"data_quality": "RECONSTRUCTED_5M", "above_vwap_at_entry": False, "trend_5m_before_entry": "DOWN"}
        result = server.evaluate_event_quality(ctx, event_risk_at_entry="HIGH")
        self.assertEqual(result["classification"], "POOR")
        self.assertTrue(len(result["evidence"]) > 0)

    def test_high_risk_with_confirmation_is_good(self):
        ctx = {"data_quality": "RECONSTRUCTED_5M", "above_vwap_at_entry": True, "trend_5m_before_entry": "UP"}
        result = server.evaluate_event_quality(ctx, event_risk_at_entry="HIGH")
        self.assertEqual(result["classification"], "GOOD")

    def test_result_independent_of_win_or_loss(self):
        # 結果（勝敗）を一切引数に取らない設計そのものが「結果を見て評価しない」の保証。
        import inspect
        sig = inspect.signature(server.evaluate_event_quality)
        params = list(sig.parameters.keys())
        for forbidden in ("pnl", "result_class", "gross_pnl", "win", "loss"):
            self.assertFalse(any(forbidden in p.lower() for p in params),
                              f"evaluate_event_qualityが結果由来の引数を持っている: {params}")

    def test_included_as_fifth_axis(self):
        axes = server.evaluate_trade_quality_axes(
            {"data_quality": "RECONSTRUCTED_5M", "above_vwap_at_entry": True, "trend_5m_before_entry": "UP"},
            stage_sequence={}, stop_quality_evidence=None, initial_stop_price=None,
            final_stop_price=None, entry_price=1000, exit_price=1010, event_risk_at_entry="HIGH")
        self.assertIn("event_quality", axes)
        self.assertEqual(axes["event_quality"]["classification"], "GOOD")
        # 他の4軸は既存どおり独立に存在し続けること（回帰確認）
        for axis in ("entry_quality", "stop_quality", "exit_quality", "reentry_quality"):
            self.assertIn(axis, axes)


if __name__ == "__main__":
    unittest.main()
