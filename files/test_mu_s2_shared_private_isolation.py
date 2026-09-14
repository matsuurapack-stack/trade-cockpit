# Phase MU-S2（2026-09-14・SHARED/PRIVATE再分類 続き）の回帰テスト。
#
# 設計ルール（このテストが守っているもの。将来SHARED化するテーブルにも同じ方針を適用すること）：
#   SHARED market report（DB保存・全員共通） + PRIVATE position overlay（閲覧時にその場で
#   current_userのportfolioから計算し、レスポンスに合成するだけでDBには保存しない）
#
# 実行方法： cd files && python -m unittest test_mu_s2_shared_private_isolation -v

import unittest
from unittest import mock

import server
import investment_db


class ApplyPersonalPositionOverlayTests(unittest.TestCase):
    """_apply_personal_position_overlay：SHARED行のコピーにPRIVATEなoverlayをその場で
    合成するだけで、DBに保存される元の行オブジェクトは変更しない（copy semantics）。"""

    def test_none_report_returns_none(self):
        self.assertIsNone(server._apply_personal_position_overlay(None, {"position_alerts": [], "has_critical_position_alert": False}))

    def test_no_critical_alert_only_sets_position_alerts(self):
        report = {"position_alerts_json": [], "risk_alerts_json": [{"level": "WARNING", "message": "重要イベントが目前"}],
                   "strategy_update_json": {"overnight_notes": []}}
        overlay = {"position_alerts": [{"code": "7203", "level": "WATCH"}], "has_critical_position_alert": False}
        out = server._apply_personal_position_overlay(report, overlay)
        self.assertEqual(out["position_alerts_json"], [{"code": "7203", "level": "WATCH"}])
        # CRITICALではないので risk_alerts_json / overnight_notes には何も追加しない
        self.assertEqual(out["risk_alerts_json"], [{"level": "WARNING", "message": "重要イベントが目前"}])
        self.assertEqual(out["strategy_update_json"]["overnight_notes"], [])

    def test_critical_alert_adds_generic_risk_alert_and_overnight_note(self):
        report = {"position_alerts_json": [], "risk_alerts_json": [], "strategy_update_json": {"overnight_notes": []}}
        overlay = {"position_alerts": [{"code": "2354", "name": "ＹＥＤＩＧＩＴＡＬ", "pnlPct": -8.27, "level": "CRITICAL"}],
                    "has_critical_position_alert": True}
        out = server._apply_personal_position_overlay(report, overlay)
        self.assertEqual(out["position_alerts_json"][0]["code"], "2354")
        self.assertIn({"level": "CRITICAL", "message": server._POSITION_RISK_ALERT_MESSAGE}, out["risk_alerts_json"])
        self.assertIn(server._POSITION_OVERNIGHT_NOTE, out["strategy_update_json"]["overnight_notes"])

    def test_does_not_mutate_input_report(self):
        report = {"position_alerts_json": [], "risk_alerts_json": [], "strategy_update_json": {"overnight_notes": []}}
        overlay = {"position_alerts": [{"code": "X"}], "has_critical_position_alert": True}
        out = server._apply_personal_position_overlay(report, overlay)
        self.assertIsNot(out, report)
        self.assertEqual(report["position_alerts_json"], [])  # 元のSHARED行は無変更
        self.assertEqual(report["risk_alerts_json"], [])

    def test_overnight_note_not_duplicated_if_already_present(self):
        report = {"position_alerts_json": [], "risk_alerts_json": [],
                   "strategy_update_json": {"overnight_notes": [server._POSITION_OVERNIGHT_NOTE]}}
        overlay = {"position_alerts": [{"code": "X", "level": "CRITICAL"}], "has_critical_position_alert": True}
        out = server._apply_personal_position_overlay(report, overlay)
        self.assertEqual(out["strategy_update_json"]["overnight_notes"].count(server._POSITION_OVERNIGHT_NOTE), 1)


class ComputePersonalPositionOverlayTests(unittest.TestCase):
    """_compute_personal_position_overlay：DB未設定時は安全に空を返す。例外はここで握りつぶす。"""

    def test_no_database_url_returns_empty(self):
        out = server._compute_personal_position_overlay(None, "matsuura")
        self.assertEqual(out, {"position_alerts": [], "has_critical_position_alert": False})

    @mock.patch("server.evaluate_position_risk_warnings")
    @mock.patch("server.get_stock_quotes")
    def test_computes_from_own_portfolio_only(self, mock_quotes, mock_eval):
        mock_quotes.return_value = {"2354": {"t": 1000}}
        mock_eval.return_value = [{"code": "2354", "level": "CRITICAL", "tier": "EXIT"}]
        with mock.patch.object(investment_db, "list_watchlist", return_value=[]), \
             mock.patch.object(investment_db, "list_portfolio", return_value=[{"code": "2354"}]):
            out = server._compute_personal_position_overlay("dummy_url", "matsuura")
        # evaluate_position_risk_warningsが「呼び出しユーザー自身」のuser_idで呼ばれていることを確認
        mock_eval.assert_called_once()
        self.assertEqual(mock_eval.call_args[0][1], "matsuura")
        self.assertTrue(out["has_critical_position_alert"])
        self.assertEqual(out["position_alerts"][0]["code"], "2354")

    @mock.patch("server.evaluate_position_risk_warnings", side_effect=RuntimeError("boom"))
    def test_exception_is_swallowed_and_returns_empty_alerts(self, _mock_eval):
        with mock.patch.object(investment_db, "list_watchlist", return_value=[]), \
             mock.patch.object(investment_db, "list_portfolio", return_value=[{"code": "2354"}]):
            out = server._compute_personal_position_overlay("dummy_url", "matsuura")
        self.assertEqual(out["position_alerts"], [])
        self.assertFalse(out["has_critical_position_alert"])


# ---- investment_db.py：trade_rulesの GLOBAL/USER 可視性クエリ ----
# 実DBに接続せず、_get_poolが返すpool/connection/cursorを最小限のフェイクに差し替えて、
# 実行されたSQL・パラメータだけを検証する（他のDB系テストと同じ方針で新規に導入）。

class _FakeCursor:
    def __init__(self, rows_by_call):
        self._rows_by_call = list(rows_by_call)
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return self._rows_by_call.pop(0) if self._rows_by_call else []

    def fetchone(self):
        rows = self.fetchall()
        return rows[0] if rows else None


class _FakeConn:
    def __init__(self, rows_by_call):
        self.rows_by_call = rows_by_call
        self.cursors = []

    def cursor(self, row_factory=None):
        cur = _FakeCursor(self.rows_by_call)
        self.cursors.append(cur)
        return cur

    def execute(self, sql, params=None):
        pass

    def commit(self):
        pass


import contextlib as _contextlib


class _FakePool2:
    """本物の_DirectConn.connection()と同じ`with pool.connection() as conn:`が使える
    最小限のフェイク。"""
    def __init__(self, rows_by_call=None):
        self._conn = _FakeConn(rows_by_call or [])

    @_contextlib.contextmanager
    def connection(self):
        yield self._conn


class TradeRuleVisibilityQueryTests(unittest.TestCase):
    """Phase MU-S2：GLOBALルール（user_id=_SHARED_SCOPE）がUSERルールと合わせて
    読み取られること（＝User A/B双方から同じGLOBALルールが見えるはずの前提）を、
    発行されるSQLパラメータで確認する。"""

    def _capture(self, fn, *args, **kwargs):
        pool = _FakePool2(rows_by_call=[[]])
        with mock.patch.object(investment_db, "_get_pool", return_value=pool):
            fn(*args, **kwargs)
        return pool._conn.cursors[-1].executed

    def test_list_trade_rules_includes_shared_scope(self):
        executed = self._capture(investment_db.list_trade_rules, "dummy_url", "matsuura")
        sql, params = executed[-1]
        self.assertIn("user_id IN (%s, %s)", sql)
        self.assertIn("matsuura", params)
        self.assertIn(investment_db._SHARED_SCOPE, params)

    def test_relevant_trade_rules_for_includes_shared_scope(self):
        executed = self._capture(investment_db.relevant_trade_rules_for, "dummy_url", "matsuura")
        sql, params = executed[-1]
        self.assertIn("user_id IN (%s, %s)", sql)
        self.assertIn(investment_db._SHARED_SCOPE, params)

    def test_find_similar_trade_rules_includes_shared_scope(self):
        executed = self._capture(investment_db.find_similar_trade_rules, "dummy_url", "matsuura", "テストルール")
        sql, params = executed[-1]
        self.assertIn("user_id IN (%s, %s)", sql)
        self.assertIn(investment_db._SHARED_SCOPE, params)


class RuleVisibilityMigrationSqlTests(unittest.TestCase):
    """_MIGRATE_RULE_VISIBILITY_SQL：新規列がUSERデフォルトで安全側に倒れていること
    （既存データを自動でGLOBAL化しない）をSQL文面で確認する。"""

    def test_visibility_column_defaults_to_user(self):
        sql = investment_db._MIGRATE_RULE_VISIBILITY_SQL
        self.assertIn("trade_rules", sql)
        self.assertIn("trade_playbooks", sql)
        self.assertIn("DEFAULT 'USER'", sql)
        self.assertNotIn("SET visibility", sql)  # 既存行を書き換えるUPDATE文が無いこと

    def test_shared_scope_constant_is_shared_underscore(self):
        # MU-S1で導入した固定scope値と一致していること（食い違うと両フェーズが噛み合わない）
        self.assertEqual(investment_db._SHARED_SCOPE, "_shared")


class ClearPrivateFromSharedReportsMigrationTests(unittest.TestCase):
    """_MIGRATE_CLEAR_PRIVATE_FROM_SHARED_REPORTS_SQL：過去に保存されたPRIVATE情報
    （position_alerts_json等）を後始末するSQLが存在し、対象列を含むこと。"""

    def test_targets_position_alerts_and_related_fields(self):
        sql = investment_db._MIGRATE_CLEAR_PRIVATE_FROM_SHARED_REPORTS_SQL
        self.assertIn("position_alerts_json", sql)
        self.assertIn("risk_alerts_json", sql)
        self.assertIn("strategy_update_json", sql)
        self.assertIn("EXIT_RULE_HIT", sql)


class CreateTradeExperienceRuleCandidateDefaultsToUserTests(unittest.TestCase):
    """指示書6番：PRIVATEなtrade_experiencesから直接GLOBALルールを作らない
    （新規candidateのvisibilityは常にUSERデフォルトのまま、明示的にGLOBALを書き込まない）。
    実DB呼び出しを伴わずに確認するため、関数のソース中のINSERT文を直接検査する
    （fetchone/fetchoneの返り値をフェイクDBで正確に模倣する複雑さを避ける）。"""

    def test_insert_sql_does_not_set_visibility(self):
        import inspect
        src = inspect.getsource(investment_db.create_trade_experience_rule_candidate)
        self.assertIn("INSERT INTO trade_rules", src)
        self.assertNotIn("visibility", src)  # DEFAULT 'USER'に委ねる（明示的にGLOBALを書かない）


if __name__ == "__main__":
    unittest.main()
