# Market Data高速化 Phase 2（ENTRY TOP5 DB N+1解消）の回帰テスト。
#
# get_entry_candidate_support_context()（server.py）と
# list_event_decision_support_for_tickers()（investment_db.py）を中心に、
# 「バッチ経路 == 既存の個別呼び出し経路」をゴールデン比較で確認する。
#
# 実行方法： cd files && python -m unittest test_entry_candidate_db_batch -v

import unittest
from unittest import mock

import server
import investment_db


def make_catalyst(title, codes=None, sectors=None, importance="high", sentiment="positive",
                   catalyst_date="2026-09-15"):
    return {"title": title, "affected_stocks": codes or [], "affected_sectors": sectors or [],
            "importance": importance, "sentiment": sentiment, "catalyst_date": catalyst_date}


def make_event(title, event_date, codes=None, sectors=None, importance="high"):
    return {"title": title, "event_date": event_date, "affected_stocks": codes or [],
            "affected_sectors": sectors or [], "importance": importance}


def make_experience(symbol, result_class="WIN", gross_pnl_pct=5.0, trade_date="2026-09-01"):
    return {"symbol": symbol, "result_class": result_class, "gross_pnl_pct": gross_pnl_pct,
            "trade_date": trade_date, "learning_weight": 1.0, "pattern_tags_json": [],
            "max_favorable_excursion_pct": None, "max_adverse_excursion_pct": None}


class ListEventDecisionSupportForTickersGoldenTests(unittest.TestCase):
    """investment_db.list_event_decision_support_for_tickers()：単体版
    list_event_decision_support_for_ticker()を複数回呼んだ結果と一致すること（R3）。"""

    def _fake_pool_with_rows(self, all_rows):
        """WHERE ticker = ANY(%s)・ticker = %s の両方に対応する簡易フェイクpool/cursor。"""

        class FakeCursor:
            def __init__(self, rows):
                self._rows = rows
                self._result = []

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, params):
                if "ANY(%s)" in sql:
                    tickers, since_iso = params
                    filtered = [r for r in self._rows if r["ticker"] in tickers and r["evaluated_at"] >= since_iso]
                else:
                    ticker, since_iso, limit = params
                    filtered = [r for r in self._rows if r["ticker"] == ticker and r["evaluated_at"] >= since_iso]
                    filtered = filtered[:limit]
                # DISTINCT ON (ticker, event_id) / (event_id) 相当：evaluated_at最大の行だけ残す
                best = {}
                key_fn = (lambda r: (r["ticker"], r["event_id"])) if "ANY(%s)" in sql else (lambda r: r["event_id"])
                for r in sorted(filtered, key=lambda r: r["evaluated_at"], reverse=True):
                    k = key_fn(r)
                    if k not in best:
                        best[k] = r
                self._result = sorted(best.values(), key=key_fn)

            def fetchall(self):
                return self._result

        class FakeConn:
            def __init__(self, rows):
                self._rows = rows

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def cursor(self, row_factory=None):
                return FakeCursor(self._rows)

        class FakePool:
            def __init__(self, rows):
                self._rows = rows

            def connection(self):
                return FakeConn(self._rows)

        return FakePool(all_rows)

    def test_batch_matches_individual_calls(self):
        rows = [
            {"ticker": "7203", "event_id": 1, "evaluated_at": "2026-09-14T01:00:00", "event_direction": "POSITIVE",
             "decision_support_score": 70, "avoid_chase": False, "pullback_candidate": False},
            {"ticker": "7203", "event_id": 1, "evaluated_at": "2026-09-14T03:00:00", "event_direction": "POSITIVE",
             "decision_support_score": 80, "avoid_chase": False, "pullback_candidate": True},  # 最新（採用されるべき）
            {"ticker": "7203", "event_id": 2, "evaluated_at": "2026-09-14T02:00:00", "event_direction": "NEGATIVE",
             "decision_support_score": 30, "avoid_chase": True, "pullback_candidate": False},
            {"ticker": "9984", "event_id": 3, "evaluated_at": "2026-09-14T02:00:00", "event_direction": "NEUTRAL",
             "decision_support_score": 50, "avoid_chase": False, "pullback_candidate": False},
        ]
        pool = self._fake_pool_with_rows(rows)
        since_iso = "2026-09-01T00:00:00"
        with mock.patch.object(investment_db, "_get_pool", return_value=pool):
            individual_7203 = investment_db.list_event_decision_support_for_ticker(None, "7203", since_iso)
            individual_9984 = investment_db.list_event_decision_support_for_ticker(None, "9984", since_iso)
            individual_missing = investment_db.list_event_decision_support_for_ticker(None, "6501", since_iso)
            batch = investment_db.list_event_decision_support_for_tickers(None, ["7203", "9984", "6501"], since_iso)

        def _norm(rs):
            return sorted([{k: v for k, v in r.items() if k != "evaluated_at"} for r in rs], key=lambda r: r["event_id"])

        self.assertEqual(_norm(batch["7203"]), _norm(individual_7203))
        self.assertEqual(_norm(batch["9984"]), _norm(individual_9984))
        self.assertEqual(batch["6501"], individual_missing)
        self.assertEqual(batch["6501"], [])

    def test_empty_tickers_returns_empty_without_query(self):
        pool = self._fake_pool_with_rows([])
        with mock.patch.object(investment_db, "_get_pool", return_value=pool) as mock_get_pool:
            result = investment_db.list_event_decision_support_for_tickers("db_url", [], "2026-09-01T00:00:00")
        self.assertEqual(result, {})

    def test_no_pool_returns_empty_dict_per_ticker(self):
        with mock.patch.object(investment_db, "_get_pool", return_value=None):
            result = investment_db.list_event_decision_support_for_tickers("db_url", ["7203", "9984"], "2026-09-01")
        self.assertEqual(result, {"7203": [], "9984": []})


class PreloadedArgGoldenTests(unittest.TestCase):
    """既存helper（relevant_catalysts_for等）に_preloaded_*を渡した結果が、省略時（DBから
    毎回取得）の結果と一致すること（R1・R2・R4）。"""

    def test_relevant_catalysts_for_preloaded_matches_default(self):
        catalysts = [make_catalyst("7203決算好調", codes=["7203"]), make_catalyst("9984決算", codes=["9984"])]
        with mock.patch.object(investment_db, "list_news_catalysts", return_value=catalysts) as mock_list:
            legacy = investment_db.relevant_catalysts_for("db_url", "user", code="7203", limit=3)
        preloaded = investment_db.relevant_catalysts_for("db_url", "user", code="7203", limit=3,
                                                            _preloaded_catalysts=catalysts)
        self.assertEqual(legacy, preloaded)
        mock_list.assert_called_once()  # 省略時は1回だけDBへ行く（今回追加した引数が既存動作を変えていないことの確認）

    def test_upcoming_event_signals_preloaded_matches_default(self):
        events = [make_event("決算発表", "2026-09-16", codes=["7203"])]
        with mock.patch.object(investment_db, "list_market_events", return_value=events):
            legacy = investment_db.upcoming_event_signals("db_url", "user", code="7203",
                                                             today=__import__("datetime").date(2026, 9, 15))
        preloaded = investment_db.upcoming_event_signals("db_url", "user", code="7203",
                                                            today=__import__("datetime").date(2026, 9, 15),
                                                            _preloaded_events=events)
        self.assertEqual(legacy, preloaded)

    def test_find_similar_trade_experiences_preloaded_matches_default(self):
        exps = [make_experience("7203"), make_experience("9984", result_class="LOSS", gross_pnl_pct=-3.0)]
        with mock.patch.object(server.investment_db, "list_trade_experiences", return_value=exps):
            legacy = server.find_similar_trade_experiences("db_url", "user", symbol="7203")
        preloaded = server.find_similar_trade_experiences("db_url", "user", symbol="7203", _preloaded_experiences=exps)
        self.assertEqual(legacy, preloaded)

    def test_build_ticker_intelligence_summary_preloaded_matches_default(self):
        rows = [{"event_id": 1, "event_direction": "POSITIVE", "decision_support_score": 80,
                 "avoid_chase": False, "pullback_candidate": True}]
        with mock.patch.object(server.investment_db, "list_event_decision_support_for_ticker", return_value=rows):
            legacy = server.build_ticker_intelligence_summary("db_url", "7203")
        preloaded = server.build_ticker_intelligence_summary("db_url", "7203", _preloaded_rows=rows)
        self.assertEqual(legacy, preloaded)


class GetEntryCandidateSupportContextTests(unittest.TestCase):
    """get_entry_candidate_support_context()：バッチ経路の統合テスト。"""

    def _patch_all(self, catalysts=None, events=None, experiences=None, event_support_rows=None,
                    raise_on=None):
        raise_on = raise_on or set()

        def _catalysts(*a, **kw):
            if "catalysts" in raise_on:
                raise RuntimeError("catalysts db down")
            return catalysts or []

        def _events(*a, **kw):
            if "events" in raise_on:
                raise RuntimeError("events db down")
            return events or []

        def _experiences(*a, **kw):
            if "experiences" in raise_on:
                raise RuntimeError("experiences db down")
            return experiences or []

        def _event_support(db_url, codes, since_iso):
            if "event_support" in raise_on:
                raise RuntimeError("event_support db down")
            return {c: (event_support_rows or {}).get(c, []) for c in codes}

        return (mock.patch.object(server.investment_db, "list_news_catalysts", side_effect=_catalysts),
                mock.patch.object(server.investment_db, "list_market_events", side_effect=_events),
                mock.patch.object(server.investment_db, "list_trade_experiences", side_effect=_experiences),
                mock.patch.object(server.investment_db, "list_event_decision_support_for_tickers", side_effect=_event_support))

    def test_empty_codes_returns_immediately_without_db_calls(self):
        p1, p2, p3, p4 = self._patch_all()
        with p1 as m1, p2 as m2, p3 as m3, p4 as m4:
            result, diag = server.get_entry_candidate_support_context("db_url", "user", [])
        self.assertEqual(result, {})
        self.assertEqual(diag["symbols"], 0)
        m1.assert_not_called()
        m2.assert_not_called()
        m3.assert_not_called()
        m4.assert_not_called()

    def test_happy_path_uses_at_most_4_db_calls_for_any_symbol_count(self):
        catalysts = [make_catalyst("7203好材料", codes=["7203"]), make_catalyst("9984好材料", codes=["9984"])]
        events = [make_event("決算", "2026-09-20", codes=["7203"])]
        experiences = [make_experience("7203"), make_experience("9984")]
        event_support_rows = {"7203": [{"event_id": 1, "event_direction": "POSITIVE",
                                         "decision_support_score": 80, "avoid_chase": False,
                                         "pullback_candidate": False}]}
        p1, p2, p3, p4 = self._patch_all(catalysts, events, experiences, event_support_rows)
        codes_with_sector = [(f"CODE{i}", "半導体") for i in range(50)] + [("7203", "自動車"), ("9984", "通信")]
        with p1 as m1, p2 as m2, p3 as m3, p4 as m4:
            result, diag = server.get_entry_candidate_support_context("db_url", "user", codes_with_sector)
        # 52銘柄でもDB呼び出しは4回だけ（catalysts/events/experiences/event_support、各1回）
        self.assertEqual(m1.call_count, 1)
        self.assertEqual(m2.call_count, 1)
        self.assertEqual(m3.call_count, 1)
        self.assertEqual(m4.call_count, 1)
        self.assertEqual(diag["db_connections"], 4)
        self.assertEqual(diag["fallback_queries"], 0)
        self.assertTrue(diag["batch_loaded"])
        self.assertEqual(len(result), 52)
        self.assertEqual(result["7203"]["eventSupport"], "STRONG")  # score80 >= 65
        self.assertEqual(len(result["7203"]["catalysts"]), 1)

    def test_duplicate_codes_deduped(self):
        p1, p2, p3, p4 = self._patch_all()
        codes = [("7203", "自動車"), ("7203", "自動車"), ("9984", "通信")]
        with p1, p2, p3, p4:
            result, diag = server.get_entry_candidate_support_context("db_url", "user", codes)
        self.assertEqual(diag["symbols"], 2)
        self.assertEqual(set(result.keys()), {"7203", "9984"})

    def test_partial_failure_catalysts_only_others_continue(self):
        """指示書H：catalysts取得だけ失敗してもevents/event_support/trade_experienceは
        通常通り計算される（全体を落とさない）。"""
        events = [make_event("決算", "2026-09-20", codes=["7203"])]
        experiences = [make_experience("7203")]
        p1, p2, p3, p4 = self._patch_all(events=events, experiences=experiences, raise_on={"catalysts"})
        with p1, p2, p3, p4:
            result, diag = server.get_entry_candidate_support_context("db_url", "user", [("7203", "自動車")])
        self.assertEqual(result["7203"]["catalysts"], [])  # 失敗した種別だけ空
        self.assertEqual(len(result["7203"]["eventInfo"]["events"]), 1)  # 他は通常通り
        self.assertEqual(result["7203"]["tradeExperience"]["similar"]["similar_count"], 1)
        self.assertEqual(len(diag["errors"]), 1)

    def test_all_sources_fail_returns_empty_without_raising(self):
        """指示書I：batch全滅でも例外を投げず、全項目が空のまま返す
        （281件への無制限フォールバックはしない）。"""
        p1, p2, p3, p4 = self._patch_all(raise_on={"catalysts", "events", "experiences", "event_support"})
        with p1, p2, p3, p4:
            result, diag = server.get_entry_candidate_support_context("db_url", "user", [("7203", "自動車")])
        self.assertEqual(result["7203"], {"catalysts": [], "eventInfo": {"events": [], "signals": []},
                                            "eventSupport": None, "tradeExperience": None})
        self.assertEqual(len(diag["errors"]), 4)
        self.assertEqual(diag["fallback_queries"], 0)

    def test_no_investment_db_returns_empty_gracefully(self):
        codes = [("7203", "自動車")]
        with mock.patch.object(server, "investment_db", None):
            result, diag = server.get_entry_candidate_support_context("db_url", "user", codes)
        self.assertEqual(result["7203"]["catalysts"], [])
        self.assertEqual(diag["batch_loaded"], False)


class ScoreEntryCandidatesUsesBatchContextTests(unittest.TestCase):
    """_score_entry_candidates()がper-item DB呼び出しをせず、事前計算済みcontextから
    読むだけになっていること（指示書「281回接続をやめる」の実効性確認）。"""

    def test_no_per_item_db_calls_when_support_context_provided(self):
        if server.investment_db is None:
            self.skipTest("investment_db not available")
        watchlist = [{"code": "7203", "name": "トヨタ自動車", "market": "JP"},
                     {"code": "9984", "name": "ソフトバンクグループ", "market": "JP"}]
        stage1 = {"rows": {"7203": {"current": 3000, "changePct": 1.0, "marketRS": 0.5, "sector": "自動車", "turnover": 1e9},
                             "9984": {"current": 8000, "changePct": -0.5, "marketRS": -0.2, "sector": "通信", "turnover": 1e9}},
                   "nikkeiChangePct": 0.3}
        with mock.patch.object(server.investment_db, "list_watchlist", return_value=watchlist), \
             mock.patch.object(server.investment_db, "get_codes_with_auto_tag", return_value=set()), \
             mock.patch.object(server, "run_momentum_stage1", return_value=stage1), \
             mock.patch.object(server, "prefetch_market_data_for_watchlist", return_value={}), \
             mock.patch.object(server, "_volume_stage2_detail", return_value=None), \
             mock.patch.object(server, "_intraday_stock_snapshot", return_value={"dataStatus": "failed"}), \
             mock.patch.object(server, "capture_entry_candidate_snapshot_safe"), \
             mock.patch.object(server.investment_db, "relevant_catalysts_for") as mock_catalysts, \
             mock.patch.object(server.investment_db, "upcoming_event_signals") as mock_events, \
             mock.patch.object(server, "build_entry_top5_event_support_label") as mock_support, \
             mock.patch.object(server, "build_trade_experience_summary_for_symbol") as mock_experience, \
             mock.patch.object(server, "get_entry_candidate_support_context",
                                return_value=({"7203": {"catalysts": [], "eventInfo": {"events": [], "signals": []},
                                                          "eventSupport": None, "tradeExperience": None},
                                                "9984": {"catalysts": [], "eventInfo": {"events": [], "signals": []},
                                                          "eventSupport": None, "tradeExperience": None}},
                                               {"symbols": 2, "db_connections": 4})) as mock_ctx:
            server._score_entry_candidates("db_url", "user")
        mock_ctx.assert_called_once()
        # ループ内の個別DB呼び出しヘルパーは1度も呼ばれない（=N+1が解消されている）
        mock_catalysts.assert_not_called()
        mock_events.assert_not_called()
        mock_support.assert_not_called()
        mock_experience.assert_not_called()


if __name__ == "__main__":
    unittest.main()
