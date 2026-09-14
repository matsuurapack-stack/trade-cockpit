# Phase MU-S3C（2026-09-14新規）：MIXEDテーブル分離（trade_playbooks / morning_market_checks）
# の回帰テスト。
#
# 設計：
# - trade_playbooks：GLOBAL定義（trade_rulesと同じuser_id IN (本人, _shared)方式）＋
#   個人実践成績はtrade_playbook_user_stats（別テーブル）へ完全分離。個人の売買結果は
#   trade_playbooksのevidence_count/success_count/failure_countには一切混入させない。
# - morning_market_checks：SHARED MARKET CORE（_shared固定）＋
#   morning_market_check_private_overlay（PRIVATE、position_risk等）に分離。
#
# 実行方法： cd files && python -m unittest test_mu_s3c_mixed_table_separation -v

import unittest
from unittest import mock

import investment_db


class _ScriptedCursor:
    """execute()呼び出しを全て記録し、fetchoneはあらかじめ渡された値を呼び出し順に返す
    最小フェイクカーソル。"""

    def __init__(self, fetchone_sequence=None, fetchall_sequence=None):
        self.calls = []
        self._fetchone_seq = list(fetchone_sequence or [])
        self._fetchall_seq = list(fetchall_sequence or [])

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        return self

    def fetchone(self):
        return self._fetchone_seq.pop(0) if self._fetchone_seq else None

    def fetchall(self):
        return self._fetchall_seq.pop(0) if self._fetchall_seq else []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _ScriptedConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self, row_factory=None):
        return self._cursor

    def execute(self, sql, params=None):
        return self._cursor.execute(sql, params)

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _ScriptedPool:
    def __init__(self, cursor):
        self._cursor = cursor

    def connection(self):
        return _ScriptedConn(self._cursor)


class TradePlaybookGlobalUserVisibilityTests(unittest.TestCase):
    """trade_playbooksの読み取りがtrade_rulesと同じuser_id IN (本人, _shared)方式であること。"""

    def test_list_trade_playbooks_queries_user_and_shared(self):
        cur = _ScriptedCursor(fetchall_sequence=[[]])
        with mock.patch("investment_db._get_pool", return_value=_ScriptedPool(cur)):
            investment_db.list_trade_playbooks("dummy_url", "user_a")
        sql, params = cur.calls[0]
        self.assertIn("user_id IN (%s, %s)", sql)
        self.assertEqual(params[:2], ["user_a", investment_db._SHARED_SCOPE])

    def test_find_similar_trade_playbooks_uses_list_trade_playbooks(self):
        # find_similar_trade_playbooksは内部でlist_trade_playbooksを呼ぶだけなので、
        # 同じuser_id IN方式が自動的に適用される（別クエリを持たないことの確認）。
        cur = _ScriptedCursor(fetchall_sequence=[[]])
        with mock.patch("investment_db._get_pool", return_value=_ScriptedPool(cur)):
            result = investment_db.find_similar_trade_playbooks(
                "dummy_url", "user_a", {"entry_conditions_json": ["a"]})
        self.assertEqual(result, [])
        sql, params = cur.calls[0]
        self.assertIn("user_id IN (%s, %s)", sql)


class RecordTradeOutcomeForPlaybooksTests(unittest.TestCase):
    """個人の売買結果はtrade_playbook_user_statsにのみ書き込み、trade_playbooks本体
    （GLOBAL定義・evidence_count/success_count/failure_count）には一切触れないこと。"""

    def _run(self, playbook_lookup_result):
        # 呼び出し順：
        # 1) SELECT used_context_json FROM analysis_context_log ... -> fetchall (ログ1件、playbook id=42を参照)
        # 2) SELECT id FROM trade_playbooks WHERE id=%s AND user_id IN (...) -> fetchone（存在確認）
        # 3) SELECT * FROM trade_playbook_user_stats ... -> fetchone（既存statsなし＝None）
        # 4) INSERT ... ON CONFLICT ... trade_playbook_user_stats -> execute（fetchoneは呼ばれない）
        cur = _ScriptedCursor(
            fetchall_sequence=[[{"used_context_json": {"playbooks": [42]}}]],
            fetchone_sequence=[playbook_lookup_result, None],
        )
        with mock.patch("investment_db._get_pool", return_value=_ScriptedPool(cur)):
            result = investment_db.record_trade_outcome_for_playbooks(
                "dummy_url", "user_a", "6753",
                {"net_pnl": 5000, "entry_price": 1000, "shares": 100})
        return cur, result

    def test_writes_to_user_stats_table_not_trade_playbooks(self):
        cur, result = self._run(playbook_lookup_result={"id": 42})
        self.assertEqual(result["updated"], 1)
        insert_calls = [c for c in cur.calls if c[0].strip().upper().startswith("INSERT")]
        self.assertEqual(len(insert_calls), 1)
        sql, params = insert_calls[0]
        self.assertIn("trade_playbook_user_stats", sql)
        self.assertNotIn("UPDATE trade_playbooks", " ".join(s for s, _ in cur.calls))

    def test_never_touches_global_evidence_success_failure_counts(self):
        cur, _ = self._run(playbook_lookup_result={"id": 42})
        for sql, _ in cur.calls:
            if "trade_playbooks" in sql and "trade_playbook_user_stats" not in sql:
                # trade_playbooks本体へのSQL（存在確認のSELECTのみのはず）にevidence_count等の
                # 更新句が絶対に含まれないこと。
                self.assertNotIn("evidence_count", sql)
                self.assertNotIn("success_count", sql)
                self.assertNotIn("failure_count", sql)

    def test_playbook_lookup_allows_global_scope(self):
        cur, _ = self._run(playbook_lookup_result={"id": 42})
        lookup_calls = [c for c in cur.calls if c[0].strip().upper().startswith("SELECT ID FROM TRADE_PLAYBOOKS")]
        self.assertEqual(len(lookup_calls), 1)
        sql, params = lookup_calls[0]
        self.assertIn("user_id IN (%s, %s)", sql)
        self.assertIn(investment_db._SHARED_SCOPE, params)

    def test_playbook_not_found_skips_update(self):
        cur, result = self._run(playbook_lookup_result=None)
        self.assertEqual(result["updated"], 0)
        insert_calls = [c for c in cur.calls if c[0].strip().upper().startswith("INSERT")]
        self.assertEqual(len(insert_calls), 0)


class MorningCheckSharedPrivateSeparationTests(unittest.TestCase):
    """morning_market_checksのSHARED本体とmorning_market_check_private_overlayの分離。"""

    def test_save_morning_check_forces_shared_scope_for_core_row(self):
        cur = _ScriptedCursor(fetchone_sequence=[{"id": 99}, {"id": 1, "check_id": 99, "is_read": False,
                                                                 "position_risk_json": [], "position_critical_warnings_json": []}])
        with mock.patch("investment_db._get_pool", return_value=_ScriptedPool(cur)):
            investment_db.save_morning_check(
                "dummy_url", "user_a", "2026-09-14", "T0800",
                {"market_regime": "RISK_ON", "position_risk_json": [{"level": "CRITICAL"}],
                 "position_critical_warnings_json": [{"level": "CRITICAL", "message": "保有銘柄が損切りルールに到達"}]})
        insert_core = cur.calls[0]
        sql, params = insert_core
        self.assertIn("INSERT INTO morning_market_checks", sql)
        self.assertEqual(params[0], investment_db._SHARED_SCOPE)
        # SHARED本体へのINSERT文にposition_risk_json列が含まれないこと
        self.assertNotIn("position_risk_json", sql)

    def test_save_morning_check_writes_overlay_with_real_user_id(self):
        cur = _ScriptedCursor(fetchone_sequence=[{"id": 99}, {"id": 1, "check_id": 99, "is_read": False,
                                                                 "position_risk_json": [{"level": "CRITICAL"}],
                                                                 "position_critical_warnings_json": []}])
        with mock.patch("investment_db._get_pool", return_value=_ScriptedPool(cur)):
            investment_db.save_morning_check(
                "dummy_url", "user_a", "2026-09-14", "T0800",
                {"position_risk_json": [{"level": "CRITICAL"}]})
        overlay_call = cur.calls[1]
        sql, params = overlay_call
        self.assertIn("morning_market_check_private_overlay", sql)
        self.assertEqual(params[0], "user_a")  # 実際のユーザーidのまま（_sharedにしない）

    def test_merge_combines_shared_and_overlay_for_response(self):
        shared_row = {"id": 5, "market_risk_warnings_json": [{"level": "WATCH", "message": "Brent 105ドル超"}]}
        overlay_row = {"position_risk_json": [{"level": "WARNING"}],
                       "position_critical_warnings_json": [{"level": "CRITICAL", "message": "保有銘柄が損切りルールに到達"}],
                       "is_read": True}
        merged = investment_db._merge_morning_check_overlay(shared_row, overlay_row)
        self.assertEqual(merged["position_risk_json"], [{"level": "WARNING"}])
        self.assertEqual(merged["risk_warnings_json"][0]["message"], "保有銘柄が損切りルールに到達")
        self.assertTrue(merged["is_read"])

    def test_merge_with_no_overlay_returns_empty_private_fields(self):
        shared_row = {"id": 5, "market_risk_warnings_json": [{"level": "WATCH", "message": "Brent 105ドル超"}]}
        merged = investment_db._merge_morning_check_overlay(shared_row, None)
        self.assertEqual(merged["position_risk_json"], [])
        self.assertEqual(merged["risk_warnings_json"], [{"level": "WATCH", "message": "Brent 105ドル超"}])
        self.assertFalse(merged["is_read"])

    def test_get_latest_morning_check_looks_up_overlay_by_real_user(self):
        shared = {"id": 7, "market_risk_warnings_json": []}
        cur = _ScriptedCursor(fetchone_sequence=[shared, None])
        with mock.patch("investment_db._get_pool", return_value=_ScriptedPool(cur)):
            investment_db.get_latest_morning_check("dummy_url", "user_b", check_date="2026-09-14")
        shared_sql, shared_params = cur.calls[0]
        self.assertIn("morning_market_checks", shared_sql)
        self.assertEqual(shared_params[0], investment_db._SHARED_SCOPE)
        overlay_sql, overlay_params = cur.calls[1]
        self.assertIn("morning_market_check_private_overlay", overlay_sql)
        self.assertEqual(overlay_params, ["user_b", 7])

    def test_mark_morning_check_read_only_touches_own_overlay(self):
        cur = _ScriptedCursor()
        with mock.patch("investment_db._get_pool", return_value=_ScriptedPool(cur)):
            investment_db.mark_morning_check_read("dummy_url", "user_a", 7)
        sql, params = cur.calls[0]
        self.assertIn("morning_market_check_private_overlay", sql)
        self.assertIn("user_a", params)
        self.assertNotIn("UPDATE morning_market_checks", sql)


class MigrationSqlContentTests(unittest.TestCase):
    """スキーマ・migration SQLの内容検査。"""

    def test_trade_playbook_user_stats_table_created(self):
        self.assertIn("trade_playbook_user_stats", investment_db._SCHEMA_TRADE_PLAYBOOK_USER_STATS_SQL)

    def test_trade_playbooks_personal_columns_dropped(self):
        sql = investment_db._MIGRATE_TRADE_PLAYBOOK_USER_STATS_SQL
        for col in ("user_attempt_count", "user_success_count", "user_failure_count",
                    "user_avg_return", "user_compatibility_score"):
            self.assertIn(f"DROP COLUMN IF EXISTS {col}", sql)

    def test_morning_check_overlay_table_created(self):
        self.assertIn("morning_market_check_private_overlay",
                       investment_db._SCHEMA_MORNING_CHECK_PRIVATE_OVERLAY_SQL)

    def test_morning_check_private_columns_dropped_from_shared(self):
        sql = investment_db._MIGRATE_MORNING_CHECK_PRIVATE_OVERLAY_SQL
        self.assertIn("DROP COLUMN IF EXISTS risk_warnings_json", sql)
        self.assertIn("DROP COLUMN IF EXISTS position_risk_json", sql)

    def test_migrations_wired_into_init_schema(self):
        import inspect
        src = inspect.getsource(investment_db.init_schema)
        for name in ("_SCHEMA_TRADE_PLAYBOOK_USER_STATS_SQL", "_MIGRATE_TRADE_PLAYBOOK_USER_STATS_SQL",
                     "_SCHEMA_MORNING_CHECK_PRIVATE_OVERLAY_SQL", "_MIGRATE_MORNING_CHECK_PRIVATE_OVERLAY_SQL",
                     "_MIGRATE_MORNING_CHECK_SHARED_SCOPE_SQL"):
            self.assertIn(name, src)


class ServerPayloadSeparationTests(unittest.TestCase):
    """server.pyのgenerate_morning_market_checkが個人由来警告をSHARED側キーに混ぜていないこと。"""

    def test_generate_morning_market_check_source_separates_warnings(self):
        import inspect
        import server
        src = inspect.getsource(server.generate_morning_market_check)
        self.assertIn("market_risk_warnings_json", src)
        self.assertIn("position_critical_warnings_json", src)
        # 個人ポジション由来のCRITICALメッセージがmarket_risk_warnings（SHARED）側の
        # リストへappendされていないこと（position_critical_warningsへのappendのみであること）。
        self.assertNotIn('market_risk_warnings.append({"level": "CRITICAL", "message": "保有銘柄が損切りルールに到達"})', src)


if __name__ == "__main__":
    unittest.main()
