# 保有中撤退判断支援アラート Phase A（2026-09-18新規）テスト。
#
# 2026-09-18の実トレード（最大含み益+1.67%→現在+0.10%、高値更新なし8分、VWAP割れ、
# 出来高低下、エントリー根拠5→2に崩壊、建値付近で迷う）を検出できることが完成条件。
# ユーザースコープPhase 1-9・13・15・17・31：保有中監視エンジン・利益消失アラート・
# 同値撤退候補・HOPE_HOLDING・アラート階層・PC通知dedup・イベント日補正・類似失敗照合・
# Learning Rule登録。iPhone Web Push（PHASE10-12）・USER_BEHAVIOR_PROFILE・アラート
# 自己評価・recency重み付け・同日流用・ENTRY TOP5反映はPhase Bのためテスト対象外。
#
# 実行方法： cd files && python -m unittest test_position_exit_alerts -v

import unittest
from unittest import mock

import server
import investment_db


class CaptureEntryThesisSnapshotTests(unittest.TestCase):
    """PHASE2：エントリー根拠キャプチャ。既存の軽量関数だけを使い、新しい取得経路を
    増やさない。取得できない場合は推測せずNoneを返す。"""

    def test_non_jp_market_returns_none(self):
        self.assertIsNone(server.capture_entry_thesis_snapshot("AAPL", "US"))

    def test_missing_stage1_row_returns_none(self):
        with mock.patch.object(server, "run_momentum_stage1", return_value={"rows": {}}):
            self.assertIsNone(server.capture_entry_thesis_snapshot("9999", "JP"))

    def test_builds_nine_dimension_thesis_from_existing_helpers(self):
        stage1 = {"rows": {"6327": {"current": 1234, "changePct": 1.5, "marketRS": 2.0,
                                      "sectorRS": 1.0, "high": 1240}}}
        snapshot = {"aboveVwap": True, "fiveMinStructure": "higher_highs", "vwap": 1220}
        stage2 = {"aboveRecentHigh": True, "timeAdjustedVolumeRatio": 1.8}
        with mock.patch.object(server, "run_momentum_stage1", return_value=stage1), \
             mock.patch.object(server, "_intraday_stock_snapshot", return_value=snapshot), \
             mock.patch.object(server, "_volume_stage2_detail", return_value=stage2), \
             mock.patch.object(server, "_volume_type", return_value="POSITIVE_VOLUME"):
            thesis = server.capture_entry_thesis_snapshot("6327", "JP")
        self.assertTrue(thesis["trend_up"])
        self.assertTrue(thesis["volume_expanding"])
        self.assertTrue(thesis["above_vwap"])
        self.assertTrue(thesis["market_supportive"])
        self.assertTrue(thesis["sector_supportive"])
        self.assertTrue(thesis["breakout_detected"])
        self.assertTrue(thesis["momentum_positive"])
        self.assertIn("captured_at", thesis)

    def test_exception_returns_none_not_raise(self):
        with mock.patch.object(server, "run_momentum_stage1", side_effect=RuntimeError("boom")):
            self.assertIsNone(server.capture_entry_thesis_snapshot("6327", "JP"))


class _FakeExecResult:
    def __init__(self, row=None):
        self._row = row

    def fetchone(self):
        return self._row


class _FakeConn:
    def __init__(self, row=None):
        self._row = row
        self.executed = []
        self.committed = False

    def cursor(self, row_factory=None):
        conn = self

        class _Cur:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, params=None):
                conn.executed.append((sql, params))
                return _FakeExecResult(conn._row)

            def fetchone(self):
                return conn._row
        return _Cur()

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        return _FakeExecResult(self._row)

    def commit(self):
        self.committed = True


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def connection(self):
        pool = self

        class _Ctx:
            def __enter__(self):
                return pool._conn

            def __exit__(self, *a):
                return False
        return _Ctx()


class UpdatePositionPeakTests(unittest.TestCase):
    """PHASE1：high-water mark。SQL側のWHERE句で「改善した時だけ更新」を保証する
    （フロント側の楽観的更新がズレても悪化方向への上書きは起きない）。"""

    def test_returns_none_when_no_improvement(self):
        # WHERE句がマッチしない＝改善なしのシミュレーション（RETURNING行なし）
        conn = _FakeConn(row=None)
        with mock.patch.object(investment_db, "_get_pool", return_value=_FakePool(conn)):
            result = investment_db.update_position_peak("dummy_url", "matsuura", 1, 1250, 1.2)
        self.assertIsNone(result)

    def test_returns_updated_peak_when_improved(self):
        conn = _FakeConn(row={"peak_price": 1250, "peak_pnl_pct": 1.67})
        with mock.patch.object(investment_db, "_get_pool", return_value=_FakePool(conn)):
            result = investment_db.update_position_peak("dummy_url", "matsuura", 1, 1250, 1.67)
        self.assertEqual(result, {"peakPrice": 1250, "peakPnlPct": 1.67})
        self.assertTrue(conn.committed)

    def test_missing_position_id_returns_none(self):
        with mock.patch.object(investment_db, "_get_pool", return_value=_FakePool(_FakeConn())):
            self.assertIsNone(investment_db.update_position_peak("dummy_url", "matsuura", None, 1250, 1.0))


class AddPositionEntryThesisTests(unittest.TestCase):
    """entry_thesisは新規建て時だけ書き込み、既存行にentry_thesis_jsonが既にあれば
    買い増し時に上書きしない（最初になぜ入ったかを保持する）。"""

    def test_new_position_stores_entry_thesis(self):
        class _Cur:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, params=None):
                self.last_sql, self.last_params = sql, params

            def fetchone(self):
                if "SELECT * FROM portfolio WHERE user_id" in self.last_sql and "active = true" in self.last_sql:
                    return None  # 既存ポジション無し＝新規
                return {"id": 1, "code": "6327", "market": "JP", "entries": [], "entry_thesis_json": None}

        class _Conn:
            def __init__(self):
                self.cur = _Cur()
                self.executed = []
                self.committed = False

            def cursor(self, row_factory=None):
                return self.cur

            def execute(self, sql, params=None):
                self.executed.append((sql, params))
                self.cur.last_sql, self.cur.last_params = sql, params

            def commit(self):
                self.committed = True

        class _Pool:
            def __init__(self, conn):
                self._conn = conn

            def connection(self):
                pool = self

                class _Ctx:
                    def __enter__(self):
                        return pool._conn

                    def __exit__(self, *a):
                        return False
                return _Ctx()

        conn = _Conn()
        with mock.patch.object(investment_db, "_get_pool", return_value=_Pool(conn)):
            investment_db.add_position_entry(
                "dummy_url", "matsuura", "6327", "北川精機", "JP", 1234, 100,
                entry_thesis={"trend_up": True})
        insert_calls = [c for c in conn.executed if "INSERT INTO portfolio" in c[0]]
        self.assertEqual(len(insert_calls), 1)
        self.assertIn("entry_thesis_json", insert_calls[0][0])


class ExitDecisionAlertSeedRulesTests(unittest.TestCase):
    """PHASE31：今回の3件のLearning Rule登録。既存upsert_trade_rule_from_text()を
    そのまま呼ぶだけ（新規テーブルは作らない）。"""

    def test_seeds_exactly_three_rules_with_expected_categories(self):
        calls = []

        def fake_upsert(database_url, user_id, rule_text, **kwargs):
            calls.append({"rule_text": rule_text, **kwargs})
            return {"action": "created", "id": len(calls)}

        with mock.patch.object(investment_db, "upsert_trade_rule_from_text", side_effect=fake_upsert):
            results = server.seed_exit_decision_alert_learning_rules("dummy_url", "matsuura")
        self.assertEqual(len(results), 3)
        categories = {c["category"] for c in calls}
        self.assertEqual(categories, {"EXIT", "BEHAVIOR", "EXECUTION"})
        for c in calls:
            self.assertEqual(c["initial_confidence"], "MEDIUM")  # 1回の実例でHIGHにしない
            self.assertEqual(c["rule_type"], "TESTING")
            self.assertIn("priority", c["source_info"])

    def test_idempotent_rerun_does_not_error(self):
        # 実際のupsert_trade_rule_from_textはrule_keyで重複判定するため、モックでは
        # 単に複数回呼べることだけを確認する（実際の冪等性はupsert_trade_rule_from_text
        # 自体の既存動作に委ねる、ここでは重複ロジックを二重実装しない）。
        with mock.patch.object(investment_db, "upsert_trade_rule_from_text", return_value={"action": "matched"}):
            first = server.seed_exit_decision_alert_learning_rules("dummy_url", "matsuura")
            second = server.seed_exit_decision_alert_learning_rules("dummy_url", "matsuura")
        self.assertEqual(len(first), len(second), 3)


class BuildPositionAlertTagsTests(unittest.TestCase):
    """PHASE17：アラート状態→反省タグのマッピング。ノイズ防止のため、意味のある
    シグナルが無ければタグを返さない（find_similar_reflectionsの2タグ一致条件を
    満たせるよう、複合状態では複数タグを返す設計）。"""

    def test_todays_scenario_produces_multiple_tags(self):
        # 2026-09-18の実例：giveback93%・HOPE_HOLDING・根拠3件崩壊・CRITICAL
        alert = {"profitGivebackPct": 93, "isHopeHolding": True,
                  "brokenConditions": ["NO_NEW_HIGH", "VWAP_BREAK", "VOLUME_FADE"], "tier": "CRITICAL"}
        tags = server.build_position_alert_tags(alert)
        self.assertIn("PROFIT_GIVEBACK", tags)
        self.assertIn("HOPE_HOLDING", tags)
        self.assertIn("THESIS_BREAK", tags)
        self.assertIn("LATE_EXIT", tags)
        self.assertGreaterEqual(len(tags), 2)

    def test_mild_state_produces_no_tags(self):
        alert = {"profitGivebackPct": 10, "isHopeHolding": False, "brokenConditions": [], "tier": "INFO"}
        self.assertEqual(server.build_position_alert_tags(alert), [])

    def test_empty_alert_does_not_crash(self):
        self.assertEqual(server.build_position_alert_tags(None), [])
        self.assertEqual(server.build_position_alert_tags({}), [])


class FindSimilarReflectionsForPositionTests(unittest.TestCase):
    def test_no_tags_skips_db_call(self):
        with mock.patch.object(investment_db, "find_similar_reflections") as mock_find:
            result = server.find_similar_reflections_for_position(
                "dummy_url", "matsuura", {"profitGivebackPct": 5, "brokenConditions": [], "tier": "INFO"})
        mock_find.assert_not_called()
        self.assertEqual(result, [])

    def test_significant_alert_triggers_search(self):
        with mock.patch.object(investment_db, "find_similar_reflections", return_value=[{"id": 1}]) as mock_find:
            result = server.find_similar_reflections_for_position(
                "dummy_url", "matsuura",
                {"profitGivebackPct": 93, "isHopeHolding": True, "brokenConditions": ["a", "b"], "tier": "CRITICAL"})
        self.assertEqual(result, [{"id": 1}])
        _args, kwargs = mock_find.call_args
        self.assertGreaterEqual(len(kwargs["tags"]), 2)


class ReflectionTagKeywordsExpansionTests(unittest.TestCase):
    """PHASE15：新タグがREFLECTION_TAG_KEYWORDSに正しく追加され、既存のstructure_
    trade_reflection()がそのまま拾えること（新しい抽出ロジックは作らない）。"""

    def test_profit_giveback_keyword_detected(self):
        result = server.structure_trade_reflection("利益消失が大きく、建値まで戻してしまった。")
        self.assertIn("PROFIT_GIVEBACK", result["tags"])

    def test_hope_holding_keyword_detected(self):
        result = server.structure_trade_reflection("戻るかもしれないという期待で保有を続けてしまった。")
        self.assertIn("HOPE_HOLDING", result["tags"])

    def test_existing_tags_unaffected(self):
        result = server.structure_trade_reflection("FOMC後に上昇し楽観視した。")
        self.assertIn("FOMC", result["tags"])
        self.assertIn("OVERCONFIDENCE", result["tags"])


if __name__ == "__main__":
    unittest.main()
