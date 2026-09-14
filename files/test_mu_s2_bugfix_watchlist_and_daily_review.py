# 2026-09-14不具合対応の回帰テスト（MU-S1後に報告された2件）。
#
# 1. 監視銘柄の価格更新（調査の結果、実際には正常に動作していたことを実機で確認済み。
#    ここではMU-S1のSHARED化がlist_watchlist/get_stock_quotesの経路に個人スコープを
#    残していないことをコード面でも固定する回帰テストを追加する）。
# 2. 「地合い情報が未記録のため判定不能（満点扱い）」の廃止。データ不足・休場日を
#    満点扱いせず、分母からも除外する。PRIVATE daily_review + SHARED market_intelligence_
#    reportsを日付だけで結合する（user_idでJOINしない）。
#
# 実行方法： cd files && python -m unittest test_mu_s2_bugfix_watchlist_and_daily_review -v

import contextlib
import datetime
import unittest
from unittest import mock

import server
import investment_db


# ---- 1. watchlist：価格更新経路にPRIVATEスコープが残っていないことの回帰テスト ----

class WatchlistSharedPriceRefreshTests(unittest.TestCase):
    """list_watchlist（SHARED）→ /api/stock-quotes（無状態・user_id非依存）の経路を確認する。
    実機（本番DB・実ブラウザ）でも56件の監視銘柄・現在値・前日比が更新されることを確認済み
    （2026-09-14）。ここではコード面で「読み込みはSHARED化されたが更新経路だけ個人スコープの
    まま」という回帰が今後起きないことを固定する。"""

    def test_list_watchlist_ignores_caller_user_id(self):
        # user_idに何を渡しても、内部的には_SHARED_SCOPEが使われる（MU-S1の設計）。
        pool = mock.Mock()
        cur = mock.MagicMock()
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        cur.fetchall.return_value = []
        conn_cm = mock.MagicMock()
        conn_cm.__enter__.return_value = mock.Mock(cursor=mock.Mock(return_value=cur))
        conn_cm.__exit__.return_value = False
        pool.connection.return_value = conn_cm
        with mock.patch.object(investment_db, "_get_pool", return_value=pool):
            investment_db.list_watchlist("dummy_url", "matsuura")
            sql_matsuura, params_matsuura = cur.execute.call_args[0]
            investment_db.list_watchlist("dummy_url", "another_user_added_later")
            sql_other, params_other = cur.execute.call_args[0]
        # どちらの呼び出しでも実際に絞り込みに使うuser_idは_SHARED_SCOPEで同一
        self.assertEqual(params_matsuura[0], investment_db._SHARED_SCOPE)
        self.assertEqual(params_other[0], investment_db._SHARED_SCOPE)
        self.assertEqual(params_matsuura, params_other)

    def test_stock_quotes_pipeline_takes_watchlist_items_directly_no_user_scoping(self):
        # /api/stock-quotesが叩くget_stock_quotes()はuser_idを一切受け取らない
        # （呼び出し元がどのuser_idでも、送られたwatchlist配列だけで動く＝
        # 「price refresh対象抽出側だけcurrent_userを見ている」という回帰を防ぐ）。
        import inspect
        sig = inspect.signature(server.get_stock_quotes)
        self.assertNotIn("user_id", sig.parameters)
        self.assertNotIn("current_user", sig.parameters)


# ---- 2. daily_review：地合い判定の不具合対応 ----

class CheckMarketFitTests(unittest.TestCase):
    """_check_market_fit：データ不足・休場日を満点扱いしない（指示書の核心）。"""

    def test_non_business_day_is_out_of_scope_not_missing_data(self):
        score, good, bad, applicable_max = investment_db._check_market_fit([], None, is_business_day=False)
        self.assertEqual(score, 0)
        self.assertEqual(applicable_max, 0)  # 分母から除外
        self.assertEqual(bad, ["市場休場日のため評価対象外"])
        self.assertNotIn("満点", " ".join(bad))

    def test_business_day_missing_data_is_not_evaluated_not_full_score(self):
        score, good, bad, applicable_max = investment_db._check_market_fit([], None, is_business_day=True)
        self.assertEqual(score, 0)
        self.assertEqual(applicable_max, 0)  # 分母から除外＝満点加算されない
        self.assertEqual(bad, ["地合い評価：データ不足のため未評価"])
        self.assertNotIn("満点", " ".join(bad))

    def test_normal_market_condition_scores_full_and_labeled_good(self):
        score, good, bad, applicable_max = investment_db._check_market_fit([], "通常運用", is_business_day=True)
        self.assertEqual(score, investment_db.MARKET_FIT_MAX)
        self.assertEqual(applicable_max, investment_db.MARKET_FIT_MAX)
        self.assertTrue(any("地合い評価：良好" in g for g in good))

    def test_risk_off_with_no_new_entry_scores_full_and_labeled_warning(self):
        score, good, bad, applicable_max = investment_db._check_market_fit([], "リスクオフ", is_business_day=True)
        self.assertEqual(score, investment_db.MARKET_FIT_MAX)
        self.assertTrue(any("地合い評価：警戒" in g for g in good))

    def test_shared_market_regime_english_enum_is_recognized_as_risk_off(self):
        # market_intelligence_reports.market_regime（RISK_OFF等の英語enum）から組み立てた
        # market_conditionでも、日本語キーワードと同様にリスクオフとして判定できること
        # （2026-09-14不具合対応：旧実装は日本語キーワードしか見ておらず「良好」に誤判定していた）。
        score, good, bad, applicable_max = investment_db._check_market_fit(
            [{"code": "1234"}], "RISK_OFF：寄り30分レポート", is_business_day=True)
        self.assertTrue(any("地合い評価：不一致" in b for b in bad))
        self.assertFalse(any("良好" in g for g in good))

    def test_risk_off_with_new_entries_deducts_and_labeled_mismatch(self):
        score, good, bad, applicable_max = investment_db._check_market_fit(
            [{"code": "1234"}], "軟調", is_business_day=True)
        self.assertLess(score, investment_db.MARKET_FIT_MAX)
        self.assertTrue(any("地合い評価：不一致" in b for b in bad))


class _FakeCursor:
    def __init__(self, rows_queue):
        self._rows_queue = rows_queue

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.last_sql, self.last_params = sql, params

    def fetchall(self):
        return self._rows_queue.pop(0) if self._rows_queue else []

    def fetchone(self):
        rows = self.fetchall()
        return rows[0] if rows else None


class _FakeConn:
    def __init__(self, rows_queue):
        self.rows_queue = rows_queue

    def cursor(self, row_factory=None):
        return _FakeCursor(self.rows_queue)

    def execute(self, sql, params=None):
        pass

    def commit(self):
        pass


class _FakePool:
    def __init__(self, rows_queue):
        self._conn = _FakeConn(rows_queue)

    @contextlib.contextmanager
    def connection(self):
        yield self._conn


class ResolveMarketConditionForReviewTests(unittest.TestCase):
    """PRIVATE daily_review（daily_log.market_env）とSHARED market_intelligence_reportsを
    日付だけで結合する（user_idでJOINしない）ことを確認する。"""

    def test_private_daily_log_takes_priority_when_present(self):
        rows = [[{"market_env": "個人メモ：地合い良好"}]]  # daily_logのSELECT結果
        pool = _FakePool(rows)
        with mock.patch.object(investment_db, "_get_pool", return_value=pool), \
             mock.patch.object(investment_db, "list_market_intelligence_reports") as mock_list:
            result = investment_db._resolve_market_condition_for_review("dummy_url", "matsuura", "2026-09-11")
        self.assertEqual(result, "個人メモ：地合い良好")
        mock_list.assert_not_called()  # PRIVATEが見つかればSHAREDへは問い合わせない

    def test_falls_back_to_shared_market_intelligence_report_when_private_missing(self):
        rows = [[]]  # daily_logのSELECT結果：無し
        pool = _FakePool(rows)
        reports = [
            {"report_type": "OPENING_30M", "market_regime": "RISK_ON", "market_summary": "寄り堅調"},
            {"report_type": "MARKET_CLOSE", "market_regime": "DEFENSE", "market_summary": "大引けにかけて軟調"},
        ]
        with mock.patch.object(investment_db, "_get_pool", return_value=pool), \
             mock.patch.object(investment_db, "list_market_intelligence_reports", return_value=reports) as mock_list:
            result = investment_db._resolve_market_condition_for_review("dummy_url", "matsuura", "2026-09-11")
        # 優先順位（MARKET_CLOSE > AFTERNOON_30M > MORNING_CLOSE > OPENING_30M）で
        # MARKET_CLOSEが選ばれること
        self.assertEqual(result, "DEFENSE：大引けにかけて軟調")
        # SHAREDテーブル参照はtrade_dateだけで行い、呼び出し時のuser_idの値に関わらず
        # 同じ結果になること（list_market_intelligence_reports自体がMU-S1で_SHARED_SCOPE
        # 固定のため、ここに渡すuser_idは実質無視される）
        mock_list.assert_called_once()
        self.assertEqual(mock_list.call_args[1].get("trade_date") or mock_list.call_args[0][2], "2026-09-11")

    def test_returns_none_when_neither_private_nor_shared_data_exists(self):
        rows = [[]]
        pool = _FakePool(rows)
        with mock.patch.object(investment_db, "_get_pool", return_value=pool), \
             mock.patch.object(investment_db, "list_market_intelligence_reports", return_value=[]):
            result = investment_db._resolve_market_condition_for_review("dummy_url", "matsuura", "2026-09-11")
        self.assertIsNone(result)

    def test_same_result_regardless_of_caller_user_id(self):
        """指示書：daily_reviews.user_id=matsuuraとmarket_intelligence_reports.user_id=_shared
        を同じuser_idでJOINしようとしていないか確認。異なるuser_id値を渡しても
        （SHARED側は常に_sharedを見るため）結果が変わらないこと。"""
        reports = [{"report_type": "MARKET_CLOSE", "market_regime": "DEFENSE", "market_summary": "軟調"}]
        results = []
        for uid in ("matsuura", "_shared", "future_second_user"):
            pool = _FakePool([[]])
            with mock.patch.object(investment_db, "_get_pool", return_value=pool), \
                 mock.patch.object(investment_db, "list_market_intelligence_reports", return_value=reports):
                results.append(investment_db._resolve_market_condition_for_review("dummy_url", uid, "2026-09-11"))
        self.assertEqual(len(set(results)), 1)  # 全員同じ結果


class GenerateDailyReviewScoreRescalingTests(unittest.TestCase):
    """generate_daily_review：地合いが評価対象外の日は、100点満点の分母からも
    地合い分（15点）を除いて再配分すること（他項目の合計がそのまま100点にならない）。"""

    def _run_with_market_fit_stub(self, market_fit_return):
        pool = _FakePool([[{"id": 1}]])  # daily_reviewsのuser_feedback SELECT
        saved_payload = {}

        def fake_execute(sql, params=None):
            if "INSERT INTO daily_reviews" in sql:
                saved_payload["params"] = params

        pool._conn.execute = fake_execute
        # cursorのexecute/fetchoneもINSERT...RETURNING *を模倣する必要があるため、
        # _FakeCursorを差し替える
        class _InsertCapturingCursor(_FakeCursor):
            def execute(self2, sql, params=None):
                super().execute(sql, params)
                if "INSERT INTO daily_reviews" in sql:
                    saved_payload["params"] = params
                    saved_payload["row"] = {"score_total": params[2], "score_market_fit": params[6]}

            def fetchone(self2):
                if "row" in saved_payload:
                    return saved_payload["row"]
                return super().fetchone()

        pool._conn.cursor = lambda row_factory=None: _InsertCapturingCursor(pool._conn.rows_queue)

        with mock.patch.object(investment_db, "_get_pool", return_value=pool), \
             mock.patch.object(investment_db, "list_portfolio", return_value=[]), \
             mock.patch.object(investment_db, "list_trade_history", return_value=[]), \
             mock.patch.object(investment_db, "list_trade_rules", return_value=[]), \
             mock.patch.object(investment_db, "_check_rule_adherence", return_value=(25, [], [])), \
             mock.patch.object(investment_db, "_check_risk_management", return_value=(10, [], [])), \
             mock.patch.object(investment_db, "_check_known_risk_ignored", return_value=[]), \
             mock.patch.object(investment_db, "_check_playbook_discipline", return_value=([], [])), \
             mock.patch.object(investment_db, "_resolve_market_condition_for_review", return_value=None), \
             mock.patch.object(investment_db, "_check_market_fit", return_value=market_fit_return):
            return investment_db.generate_daily_review("dummy_url", "matsuura", "2026-09-11", is_business_day=True)

    def test_market_fit_not_evaluated_rescales_to_100_without_free_points(self):
        # rule=25(満点)/entry=20(新規なし満点)/exit=20(決済なし満点)/market=評価対象外(0,max=0)
        # /risk=10(満点)/reflection=4(未入力) の場合：
        # 分子=25+20+20+0+10+4=79、分母=25+20+20+0+10+10=85 → 79/85*100 ≈ 93
        # （地合い15点を分子分母どちらにも含めないため、旧仕様の「79/100=79点」より高くなる
        #   ＝データ不足で不当に減点されない、かつ満点加算でもない）
        review = self._run_with_market_fit_stub((0, [], ["地合い評価：データ不足のため未評価"], 0))
        self.assertEqual(review["score_market_fit"], 0)
        self.assertEqual(review["score_total"], round(79 / 85 * 100))
        self.assertNotEqual(review["score_total"], 79)  # 旧仕様（分母100固定）の値ではない

    def test_market_fit_evaluated_uses_full_100_denominator(self):
        review = self._run_with_market_fit_stub((investment_db.MARKET_FIT_MAX, ["地合い評価：良好"], [], investment_db.MARKET_FIT_MAX))
        # 25+20+20+15+10+4 = 94 / 100
        self.assertEqual(review["score_total"], 94)


if __name__ == "__main__":
    unittest.main()
