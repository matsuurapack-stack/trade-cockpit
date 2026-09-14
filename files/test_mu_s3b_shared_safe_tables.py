# Phase MU-S3B（2026-09-14新規）：SHARED_SAFE 5テーブル（auto_signal_events / limit_up_events /
# theme_momentum_history / next_day_theme_candidates / entry_candidate_snapshots）の共有化回帰テスト。
#
# 設計：MU-S1と同じパターンで、DB層内部が呼び出し元のuser_idを無視し_SHARED_SCOPEを強制する。
# 関数シグネチャ・呼び出し側は変更しない。
#
# 実行方法： cd files && python -m unittest test_mu_s3b_shared_safe_tables -v

import unittest
from unittest import mock

import investment_db


def _fake_pool_capturing(captured, fetchone_value=None):
    """execute()に渡されたparamsのuser_id位置を記録するだけの最小フェイクpool。
    fetchone_value: count系関数（cur.fetchone()[0]を期待）は[0]、その他（rowを期待、
    無ければNone扱いでよいもの）はNone（既定）を渡す。"""

    class _FakeCursor:
        def execute(self, sql, params=None):
            captured["sql"] = sql
            captured["params"] = params
            return self

        def fetchone(self):
            return fetchone_value

        def fetchall(self):
            return []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _FakeConn:
        def cursor(self, row_factory=None):
            return _FakeCursor()

        def execute(self, sql, params=None):
            captured["sql"] = sql
            captured["params"] = params
            return mock.MagicMock(fetchone=lambda: fetchone_value)

        def commit(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _FakePool:
        def connection(self):
            return _FakeConn()

    return _FakePool()


class UserIdForcedToSharedTests(unittest.TestCase):
    """呼び出し元がどんなuser_idを渡しても、実際にDBへ渡るuser_idは_shared固定であること。"""

    def test_upsert_limit_up_event_forces_shared(self):
        captured = {}
        with mock.patch("investment_db._get_pool", return_value=_fake_pool_capturing(captured)):
            investment_db.upsert_limit_up_event(
                "dummy_url", "matsuura", "2026-09-14", "1234", {"stock_name": "テスト銘柄"})
        self.assertIn(investment_db._SHARED_SCOPE, captured["params"])
        self.assertNotIn("matsuura", captured["params"])

    def test_upsert_theme_momentum_history_forces_shared(self):
        captured = {}
        with mock.patch("investment_db._get_pool", return_value=_fake_pool_capturing(captured)):
            investment_db.upsert_theme_momentum_history(
                "dummy_url", "matsuura", "AI関連", "2026-09-14", {"stage": "EXPANDING"})
        self.assertIn(investment_db._SHARED_SCOPE, captured["params"])
        self.assertNotIn("matsuura", captured["params"])

    def test_upsert_next_day_theme_candidate_forces_shared(self):
        captured = {}
        with mock.patch("investment_db._get_pool", return_value=_fake_pool_capturing(captured)):
            investment_db.upsert_next_day_theme_candidate(
                "dummy_url", "matsuura", "半導体", "2026-09-14", {"theme_score": 80})
        self.assertIn(investment_db._SHARED_SCOPE, captured["params"])
        self.assertNotIn("matsuura", captured["params"])

    def test_log_auto_signal_event_forces_shared(self):
        captured = {}
        with mock.patch("investment_db._get_pool", return_value=_fake_pool_capturing(captured)):
            investment_db.log_auto_signal_event(
                "dummy_url", "matsuura", "6753", "JP", "MOMENTUM_DAY", "ENTER_CURRENT", "2026-09-14")
        self.assertIn(investment_db._SHARED_SCOPE, captured["params"])
        self.assertNotIn("matsuura", captured["params"])

    def test_create_entry_candidate_snapshot_forces_shared(self):
        captured = {}
        with mock.patch("investment_db._get_pool", return_value=_fake_pool_capturing(captured)):
            investment_db.create_entry_candidate_snapshot(
                "dummy_url", "matsuura", {"code": "6753", "candidate_type": "ENTRY_READY"})
        self.assertIn(investment_db._SHARED_SCOPE, captured["params"])
        self.assertNotIn("matsuura", captured["params"])


class ListFunctionsUseSharedScopeTests(unittest.TestCase):
    """list系関数もWHERE句に渡すuser_idが_sharedに強制されていること
    （＝User AがcallしてもUser Bが書いた行が見える＝共有される設計の確認）。"""

    def _assert_where_uses_shared(self, fn, *args, fetchone_value=None):
        captured = {}
        with mock.patch("investment_db._get_pool",
                         return_value=_fake_pool_capturing(captured, fetchone_value=fetchone_value)):
            fn("dummy_url", "user_a", *args)
        self.assertIn(investment_db._SHARED_SCOPE, captured["params"])
        self.assertNotIn("user_a", captured["params"])

    def test_list_limit_up_events(self):
        self._assert_where_uses_shared(investment_db.list_limit_up_events)

    def test_list_theme_momentum_history(self):
        self._assert_where_uses_shared(investment_db.list_theme_momentum_history)

    def test_list_next_day_theme_candidates(self):
        self._assert_where_uses_shared(investment_db.list_next_day_theme_candidates)

    def test_list_auto_signal_events(self):
        self._assert_where_uses_shared(investment_db.list_auto_signal_events)

    def test_list_entry_candidate_snapshots(self):
        self._assert_where_uses_shared(investment_db.list_entry_candidate_snapshots, "2026-01-01")

    def test_get_next_day_theme_candidate(self):
        self._assert_where_uses_shared(investment_db.get_next_day_theme_candidate, "AI関連", "2026-09-14")

    def test_count_entry_candidate_snapshots_since(self):
        self._assert_where_uses_shared(investment_db.count_entry_candidate_snapshots_since, "2026-01-01",
                                         fetchone_value=[0])

    def test_count_entry_candidate_snapshots_pending(self):
        self._assert_where_uses_shared(investment_db.count_entry_candidate_snapshots_pending, fetchone_value=[0])

    def test_count_entry_candidate_snapshots_evaluated_since(self):
        self._assert_where_uses_shared(investment_db.count_entry_candidate_snapshots_evaluated_since, "2026-01-01",
                                         fetchone_value=[0])

    def test_count_entry_candidate_snapshots_total(self):
        self._assert_where_uses_shared(investment_db.count_entry_candidate_snapshots_total, fetchone_value=[0])

    def test_count_entry_candidate_snapshots_evaluated_total(self):
        self._assert_where_uses_shared(investment_db.count_entry_candidate_snapshots_evaluated_total,
                                         fetchone_value=[0])


class MigrationSqlIdempotencyTests(unittest.TestCase):
    """_MIGRATE_MUS3B_SHARED_SCOPE_SQLの内容検査（実DBは叩かない、文字列レベルの安全性確認）。"""

    def test_migration_sql_targets_all_five_tables(self):
        sql = investment_db._MIGRATE_MUS3B_SHARED_SCOPE_SQL
        for table in ("auto_signal_events", "limit_up_events", "theme_momentum_history",
                      "next_day_theme_candidates", "entry_candidate_snapshots"):
            self.assertIn(table, sql)

    def test_migration_sql_is_idempotent_pattern(self):
        # 冪等性の要：最終的に必ず user_id <> '_shared' の行を '_shared' へ更新する句を持つ
        sql = investment_db._MIGRATE_MUS3B_SHARED_SCOPE_SQL
        self.assertGreaterEqual(sql.count("SET user_id = '_shared' WHERE user_id <> '_shared'"), 5)

    def test_migration_wired_into_init_schema(self):
        import inspect
        src = inspect.getsource(investment_db.init_schema)
        self.assertIn("_MIGRATE_MUS3B_SHARED_SCOPE_SQL", src)


class EntryCandidateSnapshotWasTakenGuardTests(unittest.TestCase):
    """was_taken列に個人執行情報を書き込んでいないことのガード（instructions明示要求）。
    entry_candidate_snapshotsはSHARED化済みのため、was_takenへ「自分が実際に買ったか」等の
    個人情報を入れてはならない——将来入れる場合はprivate_execution_status等の別PRIVATE
    テーブルへ分離すること。"""

    def test_create_entry_candidate_snapshot_default_was_taken_is_false(self):
        captured = {}
        with mock.patch("investment_db._get_pool", return_value=_fake_pool_capturing(captured)):
            investment_db.create_entry_candidate_snapshot(
                "dummy_url", "matsuura", {"code": "6753", "candidate_type": "ENTRY_READY"})
        # fieldsにwas_takenを指定しなければNone（Falsey）のまま渡ることを確認
        # （cols順は code, entry_score, event_support, price_at_candidate, was_taken, ...）
        cols_order = ["code", "entry_score", "event_support", "price_at_candidate", "was_taken"]
        was_taken_index = 1 + cols_order.index("was_taken")  # 先頭はuser_id
        self.assertIn(captured["params"][was_taken_index], (None, False))

    def test_server_capture_function_has_no_execution_status_kwarg(self):
        # server.capture_entry_candidate_snapshotのシグネチャに「実際に買ったか」的な個人情報
        # 引数（executed/filled/personal等）が無いことを確認（将来の誤混入への回帰ガード）。
        import inspect
        import server
        sig = inspect.signature(server.capture_entry_candidate_snapshot)
        forbidden = {"executed", "filled", "personal_execution", "user_executed", "actually_bought"}
        self.assertFalse(forbidden & set(sig.parameters.keys()))


if __name__ == "__main__":
    unittest.main()
