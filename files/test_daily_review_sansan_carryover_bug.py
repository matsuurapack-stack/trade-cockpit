# 2026-09-14不具合対応（追加）：Sansanを当日中に取得→引け成売りで利確完了したにも関わらず、
# 日次振り返りが「デイトレ想定の建玉を例外登録なしで持ち越し」と誤判定した不具合の回帰テスト。
#
# 根本原因：
#   1. add_position_exit()は全株売却でportfolioの行自体を削除する仕様のため、trade_historyに
#      「その建玉をいつ取得したか（acquired_at）」「デイトレ想定だったか（trade_style）」が
#      一切残らず、generate_daily_review()が過去のreview_date大引け時点の保有状態を
#      trade_history側から再構成できなかった（list_portfolio()の「今この瞬間」の
#      activeな行しか見られない）。
#   2. さらに、15:30〜15:35に自動生成する_daily_review_scheduler_loopと、引け成売りの
#      約定がこのアプリへ実際に記録される（add_position_exit呼び出し）タイミングは競合し得る。
#      レビュー生成がわずかに先行すると、その時点ではまだSansanがactiveなportfolio行として
#      残っており「持ち越し」と誤判定されたまま保存されてしまう。
#
# 対応：
#   - trade_history.acquired_at / trade_style（新設列）に、売却確定時点でportfolio行から
#     値を複製して残す（investment_db.add_position_exit）。
#   - generate_daily_review()のpositions_as_of_review再構成で、
#       acquired_at <= review_date AND (closed_at is NULL OR closed_at > review_date)
#     を正しく満たすものだけを「review_date大引け時点で保有していた」ポジションとして扱う
#     （trade_historyから「review_date当日またはそれ以前に決済済み」の行を明示的に除外し、
#     「review_dateより後に決済された」行は逆に保有していたものとして合流させる）。
#   - 売却が実際に記録された時点（/api/portfolio/exitハンドラ）で、その決済日のdaily_reviews
#     が既に存在するなら再生成し、生成タイミングの競合による誤判定を自己修復する
#     （server.py側、DBアクセスを伴うため本ファイルでは対象外・investment_db側のみ検証）。
#
# 実行方法： cd files && python -m unittest test_daily_review_sansan_carryover_bug -v

import contextlib
import json as _json
import unittest
from unittest import mock

import investment_db

NO_CARRY_ACTIVE_RULE = {"rule_text": "持ち越しは原則なし、その日のうちに手仕舞う", "status": "ACTIVE", "rule_type": "PERMANENT"}
SANSAN_CODE = "4443"
SANSAN_NAME = "Sansan"


class _FakeCursor:
    def __init__(self, rows_queue, captured=None):
        self._rows_queue = rows_queue
        self._captured = captured if captured is not None else {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.last_sql, self.last_params = sql, params
        if "INSERT INTO daily_reviews" in sql:
            self._captured["params"] = params

    def fetchall(self):
        return self._rows_queue.pop(0) if self._rows_queue else []

    def fetchone(self):
        if "params" in self._captured:
            p = self._captured["params"]
            # 本物のpsycopg（dict_row + jsonbアダプタ）ならJSONB列は既にPythonのlist/dictへ
            # 変換されて返るため、テストでも同様にjson.loadsして模倣する。
            return {"score_total": p[2], "score_rule_adherence": p[3],
                    "improvement_points": _json.loads(p[10])}
        rows = self.fetchall()
        return rows[0] if rows else None


class _FakeConn:
    def __init__(self, rows_queue, captured):
        self.rows_queue = rows_queue
        self._captured = captured

    def cursor(self, row_factory=None):
        return _FakeCursor(self.rows_queue, self._captured)

    def execute(self, sql, params=None):
        pass

    def commit(self):
        pass


class _FakePool:
    def __init__(self, rows_queue):
        self._captured = {}
        self._conn = _FakeConn(rows_queue, self._captured)

    @contextlib.contextmanager
    def connection(self):
        yield self._conn


def _run_review(review_date, positions, history):
    """generate_daily_review()を、list_portfolio/list_trade_historyをモックして実行するヘルパー。
    既存test_daily_review_position_time_travel_bug.pyと同じ構造。"""
    pool = _FakePool([[]])  # daily_reviewsのuser_feedback SELECT結果（無し）
    with mock.patch.object(investment_db, "_get_pool", return_value=pool), \
         mock.patch.object(investment_db, "list_portfolio", return_value=positions), \
         mock.patch.object(investment_db, "list_trade_history", return_value=history), \
         mock.patch.object(investment_db, "list_trade_rules", return_value=[NO_CARRY_ACTIVE_RULE]), \
         mock.patch.object(investment_db, "_check_known_risk_ignored", return_value=[]), \
         mock.patch.object(investment_db, "_check_playbook_discipline", return_value=([], [])), \
         mock.patch.object(investment_db, "_resolve_market_condition_for_review", return_value=None):
        return investment_db.generate_daily_review("dummy_url", "matsuura", review_date, is_business_day=True)


def _carry_flagged(review):
    return any(SANSAN_NAME in s and "持ち越し" in s for s in review["improvement_points"])


class SansanSameDayExitNotCarryoverTests(unittest.TestCase):
    """1・2・4：当日中に取得→当日中に決済（利確・損切り・引け成売りいずれも）した場合、
    portfolioの行は既に削除済み（add_position_exit仕様）で、trade_historyにだけ
    acquired_at/closed_atが残る。closed_atがreview_date当日のうちに収まっていれば
    「持ち越し」として扱われないこと。"""

    def test_bought_morning_sold_at_close_same_day_no_violation(self):
        """1: DAYポジションを当日午前に買い、当日引けで売却 → 持ち越し違反なし。"""
        history = [{
            "code": SANSAN_CODE, "name": SANSAN_NAME, "trade_style": "DAY",
            "acquired_at": "2026-09-14T00:30:00+00:00",  # JST 2026-09-14 09:30（寄り付き後）
            "closed_at": "2026-09-14T06:30:00+00:00",    # JST 2026-09-14 15:30（大引け成売り）
        }]
        review = _run_review("2026-09-14", positions=[], history=history)
        self.assertFalse(_carry_flagged(review))
        self.assertEqual(review["score_rule_adherence"], investment_db.RULE_ADHERENCE_MAX)

    def test_closing_auction_sell_exact_boundary_no_violation(self):
        """2: 大引け成売り（15:30 JST ちょうど）で当日中に決済 → 持ち越し違反なし。"""
        history = [{
            "code": SANSAN_CODE, "name": SANSAN_NAME, "trade_style": "DAY",
            "acquired_at": "2026-09-14T01:00:00+00:00",  # JST 10:00
            "closed_at": "2026-09-14T06:30:00+00:00",    # JST 15:30（大引け）
        }]
        review = _run_review("2026-09-14", positions=[], history=history)
        self.assertFalse(_carry_flagged(review))

    def test_stop_loss_exit_same_day_no_violation(self):
        """4: 損切りで当日決済 → 持ち越し違反なし（結果の損益に関わらず、同日決済なら
        持ち越しではない）。"""
        history = [{
            "code": SANSAN_CODE, "name": SANSAN_NAME, "trade_style": "DAY",
            "acquired_at": "2026-09-14T01:00:00+00:00",  # JST 10:00
            "closed_at": "2026-09-14T02:15:00+00:00",    # JST 11:15（損切り、寄り後まもなく）
        }]
        review = _run_review("2026-09-14", positions=[], history=history)
        self.assertFalse(_carry_flagged(review))


class GenuineCarryoverStillDetectedTests(unittest.TestCase):
    """3・5：実際に翌営業日（土日またぎ含む）まで残った場合は、引き続き正しく
    「持ち越し」として検出されること（誤検出をなくすために本当の検出まで消してはいけない）。"""

    def test_still_active_next_business_day_flags_violation(self):
        """3: DAYポジションが翌営業日まで残る（まだ保有中＝portfolioにactiveなまま） →
        取得日のレビューで持ち越し違反として検出される。"""
        positions = [{
            "code": SANSAN_CODE, "name": SANSAN_NAME, "trade_style": "DAY",
            "acquired_at": "2026-09-11T00:30:00+00:00",  # JST 2026-09-11 09:30（金曜）
        }]
        review = _run_review("2026-09-11", positions=positions, history=[])
        self.assertTrue(_carry_flagged(review))

    def test_friday_position_closed_the_following_monday_flags_violation_for_friday(self):
        """5: 金曜に取得し土日をまたいで月曜に決済（＝金曜時点で未決済） →
        金曜(review_date)のレビューでは持ち越し違反として検出される
        （trade_history再構成：closed_atがreview_dateより後の日付）。
        portfolioの行は既に月曜の決済でpop削除済み（active=falseにはならず行ごと消える
        仕様）のため、list_portfolio()には出てこない前提でテストする。"""
        history = [{
            "code": SANSAN_CODE, "name": SANSAN_NAME, "trade_style": "DAY",
            "acquired_at": "2026-09-11T00:30:00+00:00",  # JST 2026-09-11 09:30（金曜）
            "closed_at": "2026-09-14T01:00:00+00:00",    # JST 2026-09-14 10:00（月曜）
        }]
        review = _run_review("2026-09-11", positions=[], history=history)
        self.assertTrue(_carry_flagged(review))


class WeekendBoundaryNotMisjudgedTests(unittest.TestCase):
    """6：金曜の大引けで売却が完了していれば、土日を挟んでいても「持ち越し」扱いには
    ならないこと（決済が同日中に収まっている限り、その後何日空こうと無関係）。"""

    def test_friday_close_exit_no_weekend_carryover(self):
        """6: 金曜引け売却 → 土日の持ち越し扱いなし。"""
        history = [{
            "code": SANSAN_CODE, "name": SANSAN_NAME, "trade_style": "DAY",
            "acquired_at": "2026-09-11T00:30:00+00:00",  # JST 金曜 09:30
            "closed_at": "2026-09-11T06:30:00+00:00",    # JST 金曜 15:30（大引け）
        }]
        review = _run_review("2026-09-11", positions=[], history=history)
        self.assertFalse(_carry_flagged(review))


class JstUtcBoundaryTests(unittest.TestCase):
    """7：JST/UTC境界で誤判定しないこと。UTC保存の生タイムスタンプが日付をまたいでいても、
    JSTへ変換した実際の売買日で正しく同日決済と判定されること。"""

    def test_utc_midnight_crossing_still_same_jst_trading_day(self):
        # 取得：UTC 2026-09-13T15:00:00 = JST 2026-09-14 00:00（日付がUTCから繰り上がる）
        # 決済：UTC 2026-09-13T20:30:00 = JST 2026-09-14 05:30（同じJST日のうち）
        # generate_daily_review側はJST日付で判定するため、raw値のUTC日付（09-13）に
        # 引きずられて「9/13に持ち越した」等と誤判定してはいけない。
        history = [{
            "code": SANSAN_CODE, "name": SANSAN_NAME, "trade_style": "DAY",
            "acquired_at": "2026-09-13T15:00:00+00:00",
            "closed_at": "2026-09-13T20:30:00+00:00",
        }]
        review_0913 = _run_review("2026-09-13", positions=[], history=history)
        review_0914 = _run_review("2026-09-14", positions=[], history=history)
        self.assertFalse(_carry_flagged(review_0913))
        self.assertFalse(_carry_flagged(review_0914))

    def test_naive_utc_timestamp_without_tzinfo_still_resolved_correctly(self):
        # tzinfo無し（naive）のタイムスタンプもUTC想定でJSTへ変換される
        # （_to_jst_date_str参照）。同日決済なら持ち越し扱いにならないこと。
        history = [{
            "code": SANSAN_CODE, "name": SANSAN_NAME, "trade_style": "DAY",
            "acquired_at": "2026-09-13T23:00:00",  # naive UTC = JST 2026-09-14 08:00
            "closed_at": "2026-09-14T05:00:00",    # naive UTC = JST 2026-09-14 14:00
        }]
        review = _run_review("2026-09-14", positions=[], history=history)
        self.assertFalse(_carry_flagged(review))


if __name__ == "__main__":
    unittest.main()
