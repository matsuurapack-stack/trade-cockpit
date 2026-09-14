# 2026-09-14不具合対応（追加）：日次振り返り生成が「今この瞬間のportfolio」をそのまま
# 過去日のreview_dateに流用し、今日買った銘柄が過去日の「持ち越し違反」として誤検出される
# 不具合の回帰テスト。
#
# 実データで確認した再現条件（本番DB）：
#   portfolio: code=4704（トレンドマイクロ）trade_style=DAY, acquired_at=2026-09-14（JST）
#   → generate_daily_review(review_date="2026-09-11") / ("2026-09-12") を実行すると、
#     list_portfolio()が返す「現在のportfolio」がそのまま_check_rule_adherence()に渡され、
#     acquired_atがreview_dateより後でも「その日も持っていた」と誤判定されていた。
#
# 実行方法： cd files && python -m unittest test_daily_review_position_time_travel_bug -v

import contextlib
import unittest
from unittest import mock

import investment_db


class ToJstDateStrTests(unittest.TestCase):
    """_to_jst_date_str：UTC TIMESTAMPTZをJST日付に正しく変換する
    （JST 00:00〜08:59はUTC前日日付になるため、素の[:10]切り出しでは1日ずれる）。"""

    def test_utc_morning_is_same_jst_date_when_well_after_midnight(self):
        # 実データ：2026-09-14T00:55:19+00:00（UTC）= 2026-09-14 09:55 JST
        self.assertEqual(investment_db._to_jst_date_str("2026-09-14T00:55:19.326520+00:00"), "2026-09-14")

    def test_utc_late_evening_rolls_over_to_next_jst_date(self):
        # UTC 2026-09-13 15:30 = JST 2026-09-14 00:30（日付が繰り上がる境界ケース）
        self.assertEqual(investment_db._to_jst_date_str("2026-09-13T15:30:00+00:00"), "2026-09-14")

    def test_naive_timestamp_assumed_utc(self):
        self.assertEqual(investment_db._to_jst_date_str("2026-09-13T15:30:00"), "2026-09-14")

    def test_empty_or_none_returns_empty_string(self):
        self.assertEqual(investment_db._to_jst_date_str(None), "")
        self.assertEqual(investment_db._to_jst_date_str(""), "")

    def test_unparseable_falls_back_to_naive_slice(self):
        self.assertEqual(investment_db._to_jst_date_str("not-a-date"), "not-a-date"[:10])


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


# 実データを模した「トレンドマイクロ」ポジション（本日9/14取得、DAYトレード想定）
TODAY_ACQUIRED_DAY_POSITION = {
    "code": "4704", "name": "トレンドマイクロ", "trade_style": "DAY",
    "acquired_at": "2026-09-14T00:55:19.326520+00:00",  # UTC。JSTでは2026-09-14 09:55
}

NO_CARRY_ACTIVE_RULE = {"rule_text": "持ち越しは原則なし、その日のうちに手仕舞う", "status": "ACTIVE", "rule_type": "PERMANENT"}


class CheckRuleAdherencePositionDateFilterTests(unittest.TestCase):
    """_check_rule_adherence自体は渡されたpositionsをそのまま見るだけの関数のため、
    「今日買った銘柄を過去日のpositionsに含めない」責務はgenerate_daily_review側の
    フィルタが正しく担っていることを確認する（下のGenerateDailyReviewPositionTimeTravelTests
    で結合確認）。ここでは_check_rule_adherence自体が受け取ったpositionsをそのまま
    信頼する（フィルタしない）設計を明示しておく回帰テスト。"""

    def test_check_rule_adherence_flags_whatever_positions_it_is_given(self):
        # _check_rule_adherenceは「positionsに入っている全銘柄をその日保有していた」前提で
        # 動く。正しい日付だけを渡すのは呼び出し側（generate_daily_review）の責務。
        with mock.patch.object(investment_db, "_get_pool", return_value=None):
            score, good, bad = investment_db._check_rule_adherence(
                "dummy_url", "matsuura", "2026-09-11", [TODAY_ACQUIRED_DAY_POSITION], [NO_CARRY_ACTIVE_RULE])
        self.assertTrue(any("トレンドマイクロ" in b and "持ち越し" in b for b in bad))


class GenerateDailyReviewPositionTimeTravelTests(unittest.TestCase):
    """本命の回帰テスト：generate_daily_review()が「今日買った銘柄」を過去日のreview_dateへ
    誤って流用しないこと（2026-09-14不具合の実データ再現条件そのもの）。"""

    def _run(self, review_date, positions):
        pool = _FakePool([[]])  # daily_reviewsのuser_feedback SELECT結果（無し）
        captured = {}

        class _CapturingCursor(_FakeCursor):
            def execute(self2, sql, params=None):
                super().execute(sql, params)
                if "INSERT INTO daily_reviews" in sql:
                    captured["params"] = params

            def fetchone(self2):
                if "params" in captured:
                    p = captured["params"]
                    # 本物のpsycopg（dict_row + jsonbアダプタ）ならJSONB列は既にPythonの
                    # list/dictへ変換されて返るため、テストでも同様にjson.loadsして模倣する
                    # （生のJSON文字列のまま返すと呼び出し側のforループが文字単位で回ってしまう）。
                    import json as _json
                    return {"score_total": p[2], "score_rule_adherence": p[3],
                            "improvement_points": _json.loads(p[10])}
                return super().fetchone()

        pool._conn.cursor = lambda row_factory=None: _CapturingCursor(pool._conn.rows_queue)

        with mock.patch.object(investment_db, "_get_pool", return_value=pool), \
             mock.patch.object(investment_db, "list_portfolio", return_value=positions), \
             mock.patch.object(investment_db, "list_trade_history", return_value=[]), \
             mock.patch.object(investment_db, "list_trade_rules", return_value=[NO_CARRY_ACTIVE_RULE]), \
             mock.patch.object(investment_db, "_check_known_risk_ignored", return_value=[]), \
             mock.patch.object(investment_db, "_check_playbook_discipline", return_value=([], [])), \
             mock.patch.object(investment_db, "_resolve_market_condition_for_review", return_value=None):
            return investment_db.generate_daily_review("dummy_url", "matsuura", review_date, is_business_day=True)

    def test_position_acquired_today_does_not_leak_into_past_review(self):
        """実データ再現：本日(2026-09-14)取得のトレンドマイクロが、2026-09-11のレビューに
        「持ち越し違反」として出てこないこと。"""
        review = self._run("2026-09-11", [TODAY_ACQUIRED_DAY_POSITION])
        self.assertFalse(any("トレンドマイクロ" in s for s in review["improvement_points"]))
        self.assertEqual(review["score_rule_adherence"], investment_db.RULE_ADHERENCE_MAX)

    def test_position_acquired_today_does_not_leak_into_saturday_review(self):
        """実データ再現：土曜(2026-09-12)のレビューにも同様に混入しないこと。"""
        review = self._run("2026-09-12", [TODAY_ACQUIRED_DAY_POSITION])
        self.assertFalse(any("トレンドマイクロ" in s for s in review["improvement_points"]))

    def test_genuinely_carried_over_position_still_flagged(self):
        """前日以前から本当に持ち越しているポジションは、引き続き正しく検出されること
        （「誤検出をなくす」ために「本当の検出」まで消してはいけない）。"""
        carried = {**TODAY_ACQUIRED_DAY_POSITION, "acquired_at": "2026-09-10T01:00:00+00:00"}  # JST 2026-09-10
        review = self._run("2026-09-11", [carried])
        self.assertTrue(any("トレンドマイクロ" in s and "持ち越し" in s for s in review["improvement_points"]))

    def test_same_day_review_still_flags_a_day_position_still_open_at_that_close(self):
        """review_date当日分のレビューでは、その日取得したDAY想定ポジションが引き続き
        アクティブなら従来通り「持ち越し」として検出されること（今回の修正はあくまで
        「未来の取得日を過去日に誤って含めない」ことが目的で、当日分の検出精度は変えない）。"""
        review = self._run("2026-09-14", [TODAY_ACQUIRED_DAY_POSITION])
        self.assertTrue(any("トレンドマイクロ" in s and "持ち越し" in s for s in review["improvement_points"]))


if __name__ == "__main__":
    unittest.main()
