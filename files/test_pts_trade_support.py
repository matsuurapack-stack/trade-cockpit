# 振り返り：PTS売買対応（2026-09-23新規）の回帰テスト。
#
# 通常市場とPTS（私設取引システム、立会時間外）の約定を区別して記録し、市場を跨いだ
# 一連の取引（例：通常市場で買い→PTSで同値撤退）を既存のtrade_history 1行=1トレード
# ライフサイクル設計のまま自然に紐付ける。実例：フジクラ（5803）、通常市場で保有 → PTS
# 5,000円で同値撤退。
#
# 最重要の検証事項（ユーザー指示）：
#   「PTSで同値撤退した後さらに上がった」という結果論で判断品質を減点しないこと。
#   BREAKEVEN_RECOVERY/HOPE_HOLDING_AVOIDED/EVENT_RISK_EXIT/THESIS_WEAKENEDはすべて
#   決済時点までに確定していた情報だけから判定できる設計になっていることをテストで裏付ける
#   （classify_exit_judgment_tags()の引数にexit後の値動き・翌日の価格等が一切無いことを確認）。
#
# 実行方法： cd files && python -m unittest test_pts_trade_support -v

import contextlib
import inspect
import unittest
from unittest import mock

import server
import investment_db


class NormalizeTradeVenueTests(unittest.TestCase):
    def test_regular_and_pts_accepted(self):
        self.assertEqual(server.normalize_trade_venue("REGULAR"), "REGULAR")
        self.assertEqual(server.normalize_trade_venue("PTS"), "PTS")

    def test_case_insensitive(self):
        self.assertEqual(server.normalize_trade_venue("pts"), "PTS")
        self.assertEqual(server.normalize_trade_venue("regular"), "REGULAR")

    def test_unknown_or_empty_returns_none(self):
        self.assertIsNone(server.normalize_trade_venue("night_session"))
        self.assertIsNone(server.normalize_trade_venue(""))
        self.assertIsNone(server.normalize_trade_venue(None))


class ComputeThesisWeakenedTests(unittest.TestCase):
    def test_none_when_either_snapshot_missing(self):
        self.assertIsNone(server.compute_thesis_weakened(None, {"trend_up": True})["thesis_weakened"])
        self.assertIsNone(server.compute_thesis_weakened({"trend_up": True}, None)["thesis_weakened"])

    def test_two_or_more_broken_conditions_is_weakened(self):
        entry = {"trend_up": True, "above_vwap": True, "market_supportive": True}
        exit_ = {"trend_up": False, "above_vwap": False, "market_supportive": True}
        result = server.compute_thesis_weakened(entry, exit_)
        self.assertTrue(result["thesis_weakened"])
        self.assertEqual(set(result["broken_conditions"]), {"trend_up", "above_vwap"})
        self.assertEqual(result["broken_count"], 2)

    def test_one_broken_condition_not_weakened(self):
        entry = {"trend_up": True, "above_vwap": True}
        exit_ = {"trend_up": False, "above_vwap": True}
        result = server.compute_thesis_weakened(entry, exit_)
        self.assertFalse(result["thesis_weakened"])
        self.assertEqual(result["broken_count"], 1)

    def test_unknown_entry_field_not_counted_as_broken(self):
        """entry側でNone（未取得）だった項目はexit側で False でも「崩れた」扱いにしない
        （entry_thesis.get(k) is True の厳格チェックで担保）。"""
        entry = {"trend_up": None, "above_vwap": True}
        exit_ = {"trend_up": False, "above_vwap": False}
        result = server.compute_thesis_weakened(entry, exit_)
        self.assertEqual(result["broken_conditions"], ["above_vwap"])


class ClassifyExitJudgmentTagsNoHindsightTests(unittest.TestCase):
    """classify_exit_judgment_tags()が決済時点までの情報だけを引数に取ることを、関数の
    シグネチャそのもので裏付ける（exit後の値動き・翌日の価格等を受け取れない設計）。"""

    def test_signature_excludes_post_exit_outcome_fields(self):
        params = list(inspect.signature(server.classify_exit_judgment_tags).parameters)
        self.assertEqual(params, ["trade", "ctx", "gross_pnl_pct", "active_macro_events_at_exit",
                                    "thesis_comparison"])
        for forbidden in ("post_exit_direction", "post_exit_max_price", "next_day", "future"):
            self.assertNotIn(forbidden, params)

    def test_pts_exit_tag(self):
        tags = server.classify_exit_judgment_tags({"exit_venue": "PTS"}, {}, 0.0, None, {})
        self.assertIn("PTS_EXIT", tags)

    def test_regular_exit_no_pts_tag(self):
        tags = server.classify_exit_judgment_tags({"exit_venue": "REGULAR"}, {}, 0.0, None, {})
        self.assertNotIn("PTS_EXIT", tags)

    def test_breakeven_recovery_and_hope_holding_avoided_on_real_drawdown_then_flat(self):
        """フジクラ実例に相当するケース：決済までにMAE(-2%程度)の含み損があったが、
        最終的にほぼ同値（gross_pnl_pct≈0）で撤退した。"""
        ctx = {"max_adverse_excursion_pct": -2.0}
        tags = server.classify_exit_judgment_tags({"exit_venue": "PTS"}, ctx, 0.0, None, {})
        self.assertIn("BREAKEVEN_RECOVERY", tags)
        self.assertIn("HOPE_HOLDING_AVOIDED", tags)
        self.assertIn("PTS_EXIT", tags)

    def test_no_breakeven_recovery_without_meaningful_drawdown(self):
        """MAEがほぼ0（実質的に一度も含み損になっていない）なら、たとえ結果が同値でも
        「回復」とは呼ばない（回復するには先に落ちている必要がある）。"""
        ctx = {"max_adverse_excursion_pct": -0.2}
        tags = server.classify_exit_judgment_tags({"exit_venue": "REGULAR"}, ctx, 0.1, None, {})
        self.assertNotIn("BREAKEVEN_RECOVERY", tags)
        self.assertNotIn("HOPE_HOLDING_AVOIDED", tags)

    def test_no_breakeven_recovery_when_final_pnl_not_near_zero(self):
        """含み損はあったが、最終的に大きくプラス/マイナスで着地した場合はBREAKEVEN_RECOVERYではない。"""
        ctx = {"max_adverse_excursion_pct": -2.0}
        tags = server.classify_exit_judgment_tags({"exit_venue": "REGULAR"}, ctx, 5.0, None, {})
        self.assertNotIn("BREAKEVEN_RECOVERY", tags)

    def test_event_risk_exit_tag_only_on_high_or_extreme(self):
        for level, expect_tag in (("HIGH", True), ("EXTREME", True), ("MEDIUM", False), ("LOW", False), (None, False)):
            with self.subTest(level=level):
                tags = server.classify_exit_judgment_tags({}, {}, 0.0, {"market_event_risk": level}, {})
                self.assertEqual("EVENT_RISK_EXIT" in tags, expect_tag)

    def test_thesis_weakened_tag_from_comparison_result(self):
        tags = server.classify_exit_judgment_tags({}, {}, 0.0, None, {"thesis_weakened": True})
        self.assertIn("THESIS_WEAKENED", tags)
        tags2 = server.classify_exit_judgment_tags({}, {}, 0.0, None, {"thesis_weakened": False})
        self.assertNotIn("THESIS_WEAKENED", tags2)

    def test_unrecoverable_past_trade_with_no_data_yields_no_speculative_tags(self):
        """フジクラのような過去トレード（entry_thesis_json等が無い）では、推測でタグを
        付けず、判定可能なものだけを返す（PTS_EXITは事実として確定しているので付く）。"""
        tags = server.classify_exit_judgment_tags(
            {"exit_venue": "PTS"}, {"max_adverse_excursion_pct": None}, 0.0, None,
            server.compute_thesis_weakened(None, None))
        self.assertEqual(tags, ["PTS_EXIT"])


class AddPositionEntryVenueTests(unittest.TestCase):
    """add_position_entry()：新規建てのみentry_venueを書き込み、買い増しでは既存値を変更しない。"""

    def test_new_position_writes_entry_venue(self):
        captured = {}

        class _Cursor:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, sql, params=None):
                if sql.strip().startswith("SELECT"):
                    self._result = None
                elif sql.strip().startswith("SELECT * FROM portfolio WHERE user_id"):
                    self._result = None
            def fetchone(self):
                return getattr(self, "_result", {"id": 1, "code": "5803", "entry_venue": "PTS"})

        class _Conn:
            def cursor(self, row_factory=None):
                return _Cursor()
            def execute(self, sql, params=None):
                if sql.strip().startswith("INSERT INTO portfolio"):
                    captured["sql"], captured["params"] = sql, params
            def commit(self):
                pass

        class _Pool:
            @contextlib.contextmanager
            def connection(self):
                yield _Conn()

        with mock.patch.object(investment_db, "_get_pool", return_value=_Pool()):
            investment_db.add_position_entry(
                "dummy_url", "matsuura", "5803", "フジクラ", "JP", 5000.0, 100.0,
                trade_style="DAY", entry_venue="PTS")
        self.assertIn("entry_venue", captured["sql"])
        self.assertIn("PTS", captured["params"])

    def test_buy_more_does_not_overwrite_existing_entry_venue(self):
        existing_row = {"id": 1, "code": "5803", "quantity": 100.0, "average_price": 5000.0,
                          "entries": [], "entry_thesis_json": {"trend_up": True}, "entry_venue": "REGULAR"}
        captured = {}

        class _Cursor:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, sql, params=None):
                pass
            def fetchone(self):
                return dict(existing_row)

        class _Conn:
            def cursor(self, row_factory=None):
                return _Cursor()
            def execute(self, sql, params=None):
                if sql.strip().startswith("UPDATE portfolio SET quantity"):
                    captured["sql"] = sql
            def commit(self):
                pass

        class _Pool:
            @contextlib.contextmanager
            def connection(self):
                yield _Conn()

        with mock.patch.object(investment_db, "_get_pool", return_value=_Pool()):
            investment_db.add_position_entry(
                "dummy_url", "matsuura", "5803", "フジクラ", "JP", 5100.0, 100.0,
                trade_style="DAY", entry_venue="PTS")
        # 買い増し時のUPDATE文にentry_venueが含まれない（既存の初回建て時の値を変更しない）
        self.assertNotIn("entry_venue", captured["sql"])


class AddPositionExitVenueAndThesisTests(unittest.TestCase):
    """add_position_exit()：exit_venue・entry_venue・entry_thesis_json/exit_thesis_jsonが
    trade_historyへ正しく引き継がれることを検証する。"""

    def _fake_pool(self, portfolio_row):
        captured = {}

        class _Cursor:
            def __init__(self, outer):
                self.outer = outer
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, sql, params=None):
                if sql.strip().startswith("SELECT * FROM portfolio"):
                    self._pending = dict(portfolio_row)
                elif sql.strip().startswith("INSERT INTO trade_history"):
                    cols = ["user_id", "code", "name", "market", "entry_price", "exit_price", "shares",
                            "pnl", "gross_pnl", "tax", "net_pnl", "acquired_at", "trade_style",
                            "initial_stop_price", "final_stop_price", "stop_reason_category",
                            "stop_reason_text", "stop_quality_evidence", "entry_venue", "exit_venue",
                            "entry_thesis_json", "exit_thesis_json"]
                    record = dict(zip(cols, params))
                    record["id"] = 1
                    captured["trade"] = record
                    self._pending = record
                else:
                    self._pending = None
            def fetchone(self):
                return self._pending

        class _Conn:
            def cursor(self, row_factory=None):
                return _Cursor(self)
            def execute(self, sql, params=None):
                pass
            def commit(self):
                pass

        class _Pool:
            @contextlib.contextmanager
            def connection(self):
                yield _Conn()

        return _Pool(), captured

    def test_exit_venue_and_entry_venue_carried_through(self):
        row = {"name": "フジクラ", "quantity": 100.0, "average_price": 5000.0,
               "acquired_at": "2026-09-18T04:09:57+00:00", "trade_style": "DAY",
               "entry_venue": "REGULAR", "entry_thesis_json": None}
        pool, captured = self._fake_pool(row)
        with mock.patch.object(investment_db, "_get_pool", return_value=pool):
            result = investment_db.add_position_exit(
                "dummy_url", "matsuura", "5803", "JP", 5000.0, 100.0, exit_venue="PTS")
        self.assertNotIn("error", result)
        self.assertEqual(captured["trade"]["entry_venue"], "REGULAR")
        self.assertEqual(captured["trade"]["exit_venue"], "PTS")

    def test_entry_and_exit_thesis_snapshots_persisted(self):
        entry_thesis = {"trend_up": True, "above_vwap": True}
        exit_thesis = {"trend_up": False, "above_vwap": False}
        row = {"name": "フジクラ", "quantity": 100.0, "average_price": 5000.0,
               "acquired_at": "2026-09-18T04:09:57+00:00", "trade_style": "DAY",
               "entry_venue": "REGULAR", "entry_thesis_json": entry_thesis}
        pool, captured = self._fake_pool(row)
        with mock.patch.object(investment_db, "_get_pool", return_value=pool):
            investment_db.add_position_exit(
                "dummy_url", "matsuura", "5803", "JP", 5000.0, 100.0,
                exit_venue="PTS", exit_thesis_snapshot=exit_thesis)
        import json as _json
        self.assertEqual(_json.loads(captured["trade"]["entry_thesis_json"]), entry_thesis)
        self.assertEqual(_json.loads(captured["trade"]["exit_thesis_json"]), exit_thesis)

    def test_no_venue_given_stays_none_not_defaulted(self):
        """呼び出し側が市場区分を渡さない（旧UI経由等）場合、REGULARへ勝手に推測しない。"""
        row = {"name": "フジクラ", "quantity": 100.0, "average_price": 5000.0,
               "acquired_at": "2026-09-18T04:09:57+00:00", "trade_style": "DAY",
               "entry_venue": None, "entry_thesis_json": None}
        pool, captured = self._fake_pool(row)
        with mock.patch.object(investment_db, "_get_pool", return_value=pool):
            investment_db.add_position_exit("dummy_url", "matsuura", "5803", "JP", 5000.0, 100.0)
        self.assertIsNone(captured["trade"]["exit_venue"])
        self.assertIsNone(captured["trade"]["entry_venue"])


class CheckExitQualityVenueLabelTests(unittest.TestCase):
    """_check_exit_quality()：スコア自体は変更せず、市場区分ラベルと同値撤退の明示ラベルだけ
    追加されることを検証する（daily reviewの高速・DB専用という既存の役割分担を維持）。"""

    def test_breakeven_exit_labeled_distinctly_with_venue(self):
        exits_today = [{"code": "5803", "name": "フジクラ", "pnl": 0.0, "net_pnl": 0.0, "exit_venue": "PTS"}]
        score, good, bad = investment_db._check_exit_quality(exits_today, [])
        self.assertEqual(score, investment_db.EXIT_QUALITY_MAX)  # 点数は変更しない
        self.assertEqual(bad, [])
        self.assertTrue(any("同値撤退" in g and "PTS" in g for g in good))

    def test_loss_exit_still_shows_venue_label(self):
        exits_today = [{"code": "1234", "name": "テスト", "pnl": -1000.0, "net_pnl": -1000.0, "exit_venue": "REGULAR"}]
        score, good, bad = investment_db._check_exit_quality(exits_today, [])
        self.assertTrue(any("通常" in g for g in good))

    def test_missing_venue_produces_no_label_without_error(self):
        exits_today = [{"code": "1234", "name": "テスト", "pnl": 500.0, "net_pnl": 400.0}]
        score, good, bad = investment_db._check_exit_quality(exits_today, [])
        self.assertTrue(any("利益確定" in g for g in good))


class FujikuraRealTradeRegressionTests(unittest.TestCase):
    """実例：フジクラ（5803）2026-09-18、通常市場保有→PTS 5,000円で同値撤退（entry_price=
    exit_price=5000.0, pnl=0）。既存trade_history.id=38の実データ形状を模したケースで、
    4/5軸の後知恵禁止と、PTS/BREAKEVEN_RECOVERY/HOPE_HOLDING_AVOIDEDタグが一貫して
    導けることを確認する（実DBの値そのものは実DB E2Eで別途検証する）。"""

    def test_fujikura_like_trade_tags_and_labels(self):
        trade = {"code": "5803", "name": "フジクラ", "entry_price": 5000.0, "exit_price": 5000.0,
                   "shares": 100.0, "pnl": 0.0, "net_pnl": 0.0, "entry_venue": "REGULAR", "exit_venue": "PTS"}
        gross_pnl_pct = 0.0
        ctx = {"max_adverse_excursion_pct": -1.8, "data_quality": "RECONSTRUCTED_5M"}
        thesis_comparison = server.compute_thesis_weakened(None, None)  # 過去トレード、根拠データ無し
        tags = server.classify_exit_judgment_tags(trade, ctx, gross_pnl_pct, None, thesis_comparison)
        self.assertIn("PTS_EXIT", tags)
        self.assertIn("BREAKEVEN_RECOVERY", tags)
        self.assertIn("HOPE_HOLDING_AVOIDED", tags)
        self.assertNotIn("THESIS_WEAKENED", tags)  # データ無し＝推測でタグを付けない

        score, good, bad = investment_db._check_exit_quality([trade], [])
        self.assertEqual(bad, [])
        self.assertTrue(any("同値撤退" in g and "PTS" in g for g in good))


if __name__ == "__main__":
    unittest.main()
