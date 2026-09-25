# 買付余力の実現損益連動（2026-09-25）のテスト。
# 実効買付余力 ＝ 手動基準余力(manual_cash_anchor) ＋ anchor_at以降に確定した税引後実現損益。
# 含み損益は反映しない。実行： cd files && python -m unittest test_cash_realized_pnl -v

import contextlib
import datetime
import inspect
import unittest
from unittest import mock

import investment_db
import server

ANCHOR_AT = datetime.datetime(2026, 9, 25, 0, 1, 42, tzinfo=datetime.timezone.utc)


class _FakePool:
    """get_cash_balanceが発行する2本のSQL（基準行の取得→実現損益の合計）だけを模倣する。"""

    def __init__(self, anchor, realized, anchor_at=ANCHOR_AT):
        self.anchor, self.realized, self.anchor_at = anchor, realized, anchor_at
        self.sql = []
        self.params = []

    @contextlib.contextmanager
    def connection(self):
        yield self

    def cursor(self, row_factory=None):
        return _Cur(self)


class _Cur:
    def __init__(self, pool):
        self.pool, self._next = pool, None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.pool.sql.append(sql)
        self.pool.params.append(params)
        if "FROM portfolio_cash_balance" in sql:
            self._next = {"cash_available": self.pool.anchor, "currency": "JPY", "source": "MANUAL",
                          "auto_reference": None, "updated_at": ANCHOR_AT, "anchor_at": self.pool.anchor_at}
        elif "FROM trade_history" in sql:
            self._next = {"realized": self.pool.realized}

    def fetchone(self):
        return self._next


def effective(anchor, realized):
    pool = _FakePool(anchor, realized)
    with mock.patch.object(investment_db, "_get_pool", return_value=pool):
        return investment_db.get_cash_balance("url", "u"), pool


class EffectiveCashTests(unittest.TestCase):
    def test_spec_sequence_anchor_plus_realized(self):
        # 基準600,000 → +20,000利確=620,000 → -30,000損切り=590,000 → +10,000=600,000
        running = 0
        for pnl, expected in ((20000, 620000), (-30000, 590000), (10000, 600000)):
            running += pnl
            rec, _ = effective(600000, running)
            self.assertEqual(rec["cash_available"], expected)
            self.assertEqual(rec["manual_cash_anchor"], 600000)
            self.assertEqual(rec["realized_pnl_since_anchor"], running)

    def test_only_realized_after_anchor_from_trade_history_no_unrealized(self):
        rec, pool = effective(600000, 0)
        realized_sql = next(s for s in pool.sql if "trade_history" in s)
        self.assertIn("closed_at >", realized_sql)          # 基準点より後に確定したものだけ
        self.assertIn("COALESCE(net_pnl, pnl)", realized_sql)  # 税引後（旧行はpnl）
        self.assertNotIn("portfolio ", realized_sql.replace("portfolio_cash_balance", ""))  # 含み損益は見ない
        self.assertEqual(pool.params[1], ["u", ANCHOR_AT])

    def test_no_row_returns_none(self):
        class Empty(_FakePool):
            pass
        p = Empty(1, 0)
        p.anchor = None
        cur_exec = _Cur.execute

        def fake_exec(self, sql, params=None):
            self.pool.sql.append(sql)
            self._next = None
        with mock.patch.object(investment_db, "_get_pool", return_value=p), mock.patch.object(_Cur, "execute", fake_exec):
            self.assertIsNone(investment_db.get_cash_balance("url", "u"))

    def test_anchor_at_falls_back_to_updated_at_for_legacy_rows(self):
        _, pool = effective(600000, 0)
        self.assertIn("COALESCE(anchor_at, updated_at)", pool.sql[0])

    def test_manual_set_resets_anchor_to_now(self):
        captured = []

        class P(_FakePool):
            @contextlib.contextmanager
            def connection(self):
                yield self

            def commit(self):
                pass

        class C(_Cur):
            def execute(self, sql, params=None):
                captured.append(sql)
                super().execute(sql, params)
        p = P(700000, 5000)
        p.cursor = lambda row_factory=None: C(p)
        with mock.patch.object(investment_db, "_get_pool", return_value=p):
            rec = investment_db.set_cash_balance("url", "u", 700000)
        upsert = next(s for s in captured if s.startswith("INSERT INTO portfolio_cash_balance"))
        self.assertIn("anchor_at = now()", upsert)   # 手動設定＝新しい基準点
        self.assertIn("source = 'MANUAL'", upsert)
        self.assertEqual(rec["cash_available"], 705000)

    def test_auto_update_does_not_move_manual_anchor(self):
        sql = inspect.getsource(investment_db.set_cash_balance)
        self.assertIn("THEN portfolio_cash_balance.anchor_at ELSE now() END", sql)


class ServerWiringTests(unittest.TestCase):
    def test_cash_response_exposes_breakdown(self):
        rec = {"cash_available": 620000.0, "manual_cash_anchor": 600000.0, "anchor_at": "2026-09-25T00:01:42+00:00",
               "realized_pnl_since_anchor": 20000, "source": "MANUAL", "updated_at": "x", "currency": "JPY"}
        r = server._cash_response(rec)
        self.assertEqual((r["cash_available"], r["effective_cash"], r["manual_cash_anchor"], r["realized_pnl_since_anchor"]),
                         (620000.0, 620000.0, 600000.0, 20000))

    def test_ranking_uses_effective_cash(self):
        cands = sorted([{"code": "A", "name": "A", "current": 6100, "entryScore": 90, "entryState": "ENTRY_READY", "changePct": 1.0},
                        {"code": "B", "name": "B", "current": 3000, "entryScore": 80, "entryState": "ENTRY_READY", "changePct": 1.0}],
                       key=lambda c: -c["entryScore"])
        base = server._build_capital_selection([dict(c) for c in cands], {"cash_available": 600000.0, "source": "MANUAL"})
        self.assertEqual([c["code"] for c in base["top5"]], ["B"])          # 610,000円のAは買えない
        after = server._build_capital_selection([dict(c) for c in cands], {"cash_available": 620000.0, "source": "MANUAL"})
        self.assertEqual([c["code"] for c in after["top5"]], ["A", "B"])    # +20,000の利確でAが買える

    def test_refresh_after_exit_reranks_cache_with_effective_cash(self):
        pool = sorted([{"code": "A", "name": "A", "current": 6100, "entryScore": 90, "entryState": "ENTRY_READY", "changePct": 1.0}],
                      key=lambda c: -c["entryScore"])
        server._ENTRY_TOP5_CACHE["u_cash_rt"] = {"entryReadyTop5": [], "watchCandidates": [], "debug": {}, "_candidatePool": pool}
        try:
            with mock.patch.object(server, "get_capital_context", return_value={"cash_available": 620000.0, "source": "MANUAL"}), \
                    mock.patch.object(server, "get_position_summary", return_value={"newEntryCapacity": "NORMAL"}):
                server.refresh_cash_dependent_ranking("u_cash_rt")
            self.assertEqual([c["code"] for c in server._ENTRY_TOP5_CACHE["u_cash_rt"]["actionableTop5"]], ["A"])
        finally:
            server._ENTRY_TOP5_CACHE.pop("u_cash_rt", None)

    def test_refresh_failure_never_breaks_exit(self):
        with mock.patch.object(server, "get_capital_context", side_effect=RuntimeError("db down")):
            server.refresh_cash_dependent_ranking("u")  # 例外を握りつぶす

    def test_exit_handler_refreshes_ranking_after_success(self):
        src = inspect.getsource(server.Handler.do_POST)
        i = src.index('elif self.path == "/api/portfolio/exit"')
        self.assertIn("refresh_cash_dependent_ranking(self.current_user)", src[i:i + 6000])


if __name__ == "__main__":
    unittest.main()
