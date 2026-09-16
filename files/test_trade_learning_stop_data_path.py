# Trade Learning Phase B：STOP LOSS記録のデータ欠損修正の回帰テスト。
#
# 背景（6227 AIメカテック実例で判明した不具合）：
#   add_position_exit()は決済直前にportfolio行を丸ごとSELECTしており、initial_stop/current_stop
#   も取得済みなのに、その後のtrade_historyへのINSERT列に含まれておらず、決済と同時に
#   STOP情報が失われていた。ここではUIを変更せず、以下のデータパスのみを塞ぐ：
#     trade_history.initial_stop_price  <- 決済直前のportfolio.initial_stop
#     trade_history.final_stop_price    <- 決済直前のportfolio.current_stop
#     trade_history.stop_reason_category/text <- portfolio.stop_reason_category/text
#     trade_history.stop_quality_evidence      <- ACTUAL_STOP（initial_stop_priceがある）
#                                                  / UNKNOWN（無い＝過去トレード等）
#   stop_history（変更履歴の時系列保存）はPhase Bでは実装しない（INITIAL→FINALの2点保存のみ）。
#   過去トレードは遡って埋め戻さない（推測禁止、ユーザー指示）＝実データが無ければUNKNOWNのまま。
#
# 実行方法： cd files && python -m unittest test_trade_learning_stop_data_path -v

import contextlib
import unittest
from unittest import mock

import investment_db


class _FakePortfolioExitDB:
    """add_position_exit()が発行するSQL（SELECT portfolio → INSERT trade_history RETURNING *
    → DELETE or UPDATE portfolio）だけを対象にした最小限のインメモリ模倣。
    部分決済シナリオ（複数回のadd_position_exit呼び出しをまたぐ状態変化）を正しく検証するため、
    _FakePortfolioExitDBインスタンスをテスト側で保持し、複数回exitを呼んでも状態が引き継がれる。"""

    def __init__(self, portfolio_row):
        # portfolio_row: dict。キーはportfolioテーブルの列名一式を想定。
        self.row = dict(portfolio_row)
        self.deleted = False
        self.trade_history = []
        self._next_id = 1

    @contextlib.contextmanager
    def connection(self):
        yield self

    def cursor(self, row_factory=None):
        return _FakeCursor(self)

    def execute(self, sql, params=None):
        if sql.startswith("DELETE FROM portfolio"):
            self.deleted = True
        elif sql.startswith("UPDATE portfolio SET quantity"):
            self.row["quantity"] = params[0]

    def commit(self):
        pass


class _FakeCursor:
    def __init__(self, db):
        self.db = db
        self._pending = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        if sql.strip().startswith("SELECT * FROM portfolio"):
            if self.db.deleted:
                self._pending = ("select", None)
            else:
                self._pending = ("select", dict(self.db.row))
        elif sql.strip().startswith("INSERT INTO trade_history"):
            cols = ["user_id", "code", "name", "market", "entry_price", "exit_price", "shares",
                    "pnl", "gross_pnl", "tax", "net_pnl", "acquired_at", "trade_style",
                    "initial_stop_price", "final_stop_price", "stop_reason_category",
                    "stop_reason_text", "stop_quality_evidence"]
            record = dict(zip(cols, params))
            record["id"] = self.db._next_id
            self.db._next_id += 1
            self.db.trade_history.append(record)
            self._pending = ("insert", record)
        else:
            self._pending = (None, None)

    def fetchone(self):
        kind, val = self._pending
        return val


def _make_exit_pool(portfolio_row):
    db = _FakePortfolioExitDB(portfolio_row)
    return db


BASE_ROW = {
    "name": "AIメカテック", "quantity": 200.0, "average_price": 5600.0,
    "acquired_at": "2026-09-16T04:02:18+00:00", "trade_style": "DAY",
    "initial_stop": 5540.0, "current_stop": 5540.0,
    "stop_reason_category": "VWAP", "stop_reason_text": "VWAP(5512.8)を割ったら損切りと判断",
}


class StopDataCarriedThroughOnFullExitTests(unittest.TestCase):
    """全株決済でも、決済直前のinitial_stop/current_stop/stop_reasonがtrade_historyへ
    正しく引き継がれることを検証する（従来は完全に失われていた）。"""

    def test_full_exit_carries_initial_and_final_stop(self):
        pool = _make_exit_pool(BASE_ROW)
        with mock.patch.object(investment_db, "_get_pool", return_value=pool):
            result = investment_db.add_position_exit("dummy_url", "matsuura", "6227", "JP", 5700.0, 200.0)
        self.assertNotIn("error", result)
        trade = result["trade"]
        self.assertEqual(trade["initial_stop_price"], 5540.0)
        self.assertEqual(trade["final_stop_price"], 5540.0)
        self.assertEqual(trade["stop_reason_category"], "VWAP")
        self.assertEqual(trade["stop_reason_text"], "VWAP(5512.8)を割ったら損切りと判断")
        self.assertEqual(trade["stop_quality_evidence"], "ACTUAL_STOP")
        self.assertTrue(result["closed"])
        self.assertTrue(pool.deleted)


class StopQualityEvidenceUnknownWhenNoRecordedStopTests(unittest.TestCase):
    """実STOPが記録されていない建玉（過去トレード相当）を決済した場合、初期STOP列はNULLの
    まま・stop_quality_evidenceはUNKNOWNになる（過去データの埋め戻し禁止・推測禁止）。"""

    def test_no_stop_recorded_yields_unknown_evidence(self):
        row = {**BASE_ROW, "initial_stop": None, "current_stop": None,
               "stop_reason_category": None, "stop_reason_text": None}
        pool = _make_exit_pool(row)
        with mock.patch.object(investment_db, "_get_pool", return_value=pool):
            result = investment_db.add_position_exit("dummy_url", "matsuura", "6227", "JP", 5700.0, 200.0)
        trade = result["trade"]
        self.assertIsNone(trade["initial_stop_price"])
        self.assertIsNone(trade["final_stop_price"])
        self.assertEqual(trade["stop_quality_evidence"], "UNKNOWN")


class PartialExitStopDataIntegrityTests(unittest.TestCase):
    """ユーザー必須指定シナリオ：200株保有→100株決済→残100株のSTOPを変更→残り決済。
    各trade_history行は「その決済時点でのcurrent_stop」をfinal_stop_priceとして個別に
    捕捉し、かつ残存ポジション（まだ保有中の100株）のSTOPデータが決済処理によって
    破壊・消失しないことを検証する。"""

    def test_partial_exit_then_stop_change_then_final_exit(self):
        pool = _make_exit_pool(BASE_ROW)  # 200株保有、initial_stop=current_stop=5540

        # 1回目：100株だけ決済（残り100株は保有継続）
        with mock.patch.object(investment_db, "_get_pool", return_value=pool):
            result1 = investment_db.add_position_exit("dummy_url", "matsuura", "6227", "JP", 5650.0, 100.0)
        self.assertNotIn("error", result1)
        self.assertFalse(result1["closed"])
        self.assertEqual(result1["remainingShares"], 100.0)
        trade1 = result1["trade"]
        self.assertEqual(trade1["shares"], 100.0)
        self.assertEqual(trade1["initial_stop_price"], 5540.0)
        self.assertEqual(trade1["final_stop_price"], 5540.0)

        # 残存ポジションのSTOPデータが1回目の決済で破壊されていないことを確認
        self.assertFalse(pool.deleted)
        self.assertEqual(pool.row["initial_stop"], 5540.0)
        self.assertEqual(pool.row["current_stop"], 5540.0)
        self.assertEqual(pool.row["quantity"], 100.0)

        # 残り100株についてトレーリングでcurrent_stopを引き上げる（initial_stopは不変のまま）
        pool.row["current_stop"] = 5620.0

        # 2回目：残り100株を決済
        with mock.patch.object(investment_db, "_get_pool", return_value=pool):
            result2 = investment_db.add_position_exit("dummy_url", "matsuura", "6227", "JP", 5700.0, 100.0)
        self.assertNotIn("error", result2)
        self.assertTrue(result2["closed"])
        trade2 = result2["trade"]
        self.assertEqual(trade2["shares"], 100.0)
        # 2回目の決済はSTOP変更後に行われたので、final_stop_priceはその時点のcurrent_stop(5620)を反映する
        self.assertEqual(trade2["initial_stop_price"], 5540.0)
        self.assertEqual(trade2["final_stop_price"], 5620.0)

        # 2件のtrade_history行が別々に、それぞれ決済時点のSTOP値を保持していることを確認
        self.assertEqual(len(pool.trade_history), 2)
        self.assertEqual(pool.trade_history[0]["final_stop_price"], 5540.0)
        self.assertEqual(pool.trade_history[1]["final_stop_price"], 5620.0)
        self.assertTrue(pool.deleted)


class UpsertPortfolioItemPersistsStopReasonTests(unittest.TestCase):
    """upsert_portfolio_item()がstop_reason_category/stop_reason_textを他のSTOP列と同様に
    保存できることを確認する（_PORTFOLIO_COLSへの追加のみ、新規テーブル・新規APIなし）。"""

    def test_stop_reason_columns_included_when_present(self):
        captured = {}

        class _Conn:
            def execute(self, sql, params):
                captured["sql"] = sql
                captured["params"] = params

            def commit(self):
                pass

        class _Pool:
            @contextlib.contextmanager
            def connection(self):
                yield _Conn()

        item = {
            "code": "6227", "market": "JP", "initial_stop": 5540.0, "current_stop": 5540.0,
            "stop_reason_category": "VWAP", "stop_reason_text": "VWAP割れで損切り",
        }
        with mock.patch.object(investment_db, "_get_pool", return_value=_Pool()):
            ok = investment_db.upsert_portfolio_item("dummy_url", "matsuura", item)
        self.assertTrue(ok)
        self.assertIn("stop_reason_category", captured["sql"])
        self.assertIn("stop_reason_text", captured["sql"])
        self.assertIn("VWAP", captured["params"])
        self.assertIn("VWAP割れで損切り", captured["params"])

    def test_stop_reason_columns_omitted_when_absent_does_not_error(self):
        """既存の呼び出し元（stop_reasonを送らないパス）が壊れないことの後方互換確認。"""
        captured = {}

        class _Conn:
            def execute(self, sql, params):
                captured["sql"] = sql
                captured["params"] = params

            def commit(self):
                pass

        class _Pool:
            @contextlib.contextmanager
            def connection(self):
                yield _Conn()

        item = {"code": "6227", "market": "JP", "current_stop": 5620.0}
        with mock.patch.object(investment_db, "_get_pool", return_value=_Pool()):
            ok = investment_db.upsert_portfolio_item("dummy_url", "matsuura", item)
        self.assertTrue(ok)
        self.assertNotIn("stop_reason_category", captured["sql"])


if __name__ == "__main__":
    unittest.main()
