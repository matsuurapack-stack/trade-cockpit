# 今買い時TOP5 場中リアルタイム化指示書 レビュー反映（続）：scan-local quote/5分足 reuseの回帰テスト。
#
# daily arraysで見つけたのと同じ問題（CACHE_TTL=90秒がscan全体の所要時間より短く、同一scan内で
# 前半にprefetchした値が後半のcandidate loopに到達する頃には期限切れになり、Tachibana/yfinanceへの
# 二重取得が発生する）が、quote（現在値）・5分足レジームにも独立して存在していた
# （AFTER測定でcandidateLoopTotalが改善しなかったことから判明）。ここでは
# _intraday_stock_snapshot()のprefetched引数と、prefetch_market_data_for_watchlist()が返す
# marketDataByCodeの両方を検証する。
#
# 実行方法： cd files && python -m unittest test_scan_local_market_data_reuse -v

import time
import unittest
from unittest import mock

import server


def _fake_get_stock_quotes_factory(quote, status):
    """get_stock_quotes()の個別呼び出し経路（items=[単一watchlist_item]）を模倣するfake。
    実際の関数と同じくstatus_out（呼び出し元が渡した辞書）へ副作用で書き込む。"""
    def _fake(items, cache_ttl=0, status_out=None):
        code = items[0].get("code")
        if status_out is not None:
            status_out[code] = status
        return {code: quote} if quote is not None else {}
    return _fake


class IntradayStockSnapshotPrefetchedGoldenTests(unittest.TestCase):
    """_intraday_stock_snapshot(prefetched=...)：prefetched経由の結果が、同じ値を個別取得
    経路（get_stock_quotes・_intraday_regime_cached）で返した場合の結果と完全一致すること
    （指示書「既存挙動を全面変更しない」「stale判定を壊さない」）。ok/stale_cache/
    rate_limited/failed の主要な組み合わせをgolden比較する。"""

    CASES = [
        ("full_ok", {"t": 100, "p": 95}, "ok",
         {"vwap": 98, "aboveVwap": True, "pattern": "higher_highs", "current": 100}, "ok"),
        ("quote_stale_cache", {"t": 100, "p": 95}, "stale_cache",
         {"vwap": 98, "aboveVwap": True, "pattern": "higher_highs", "current": 100}, "ok"),
        ("regime_rate_limited", {"t": 100, "p": 95}, "ok",
         {"vwap": 98, "aboveVwap": True, "pattern": "higher_highs", "current": 100}, "rate_limited"),
        ("regime_missing_failed", {"t": 100, "p": 95}, "ok", None, "failed"),
        ("quote_missing_failed", None, "failed",
         {"vwap": 98, "aboveVwap": True, "pattern": "higher_highs", "current": 100}, "ok"),
        ("both_missing_failed", None, "failed", None, "failed"),
        ("regime_stale_cache_quote_ok", {"t": 100, "p": 95}, "ok",
         {"vwap": 98, "aboveVwap": True, "pattern": "mixed", "current": 100}, "stale_cache"),
    ]

    def test_prefetched_matches_individual_fetch_for_various_status_combos(self):
        w = {"code": "7203", "market": "JP"}
        for name, quote, quote_status, regime, regime_status in self.CASES:
            with self.subTest(case=name):
                with mock.patch.object(server, "get_stock_quotes",
                                        side_effect=_fake_get_stock_quotes_factory(quote, quote_status)), \
                     mock.patch.object(server, "_intraday_regime_cached", return_value=(regime, regime_status)):
                    individual = server._intraday_stock_snapshot(w)

                prefetched = {"quote": quote, "quoteStatus": quote_status, "regime": regime, "regimeStatus": regime_status}
                with mock.patch.object(server, "get_stock_quotes", side_effect=AssertionError("get_stock_quotes should not be called when prefetched is given")), \
                     mock.patch.object(server, "_intraday_regime_cached", side_effect=AssertionError("_intraday_regime_cached should not be called when prefetched is given")):
                    via_prefetch = server._intraday_stock_snapshot(w, prefetched=prefetched)

                self.assertEqual(individual, via_prefetch)

    def test_no_prefetched_arg_uses_individual_fetch_as_before(self):
        """指示書「既存call siteは従来動作」：prefetched省略時（他の既存呼び出し元、FAST UPDATE等）
        は従来通りget_stock_quotes()・_intraday_regime_cached()を呼ぶ。"""
        w = {"code": "7203", "market": "JP"}
        with mock.patch.object(server, "get_stock_quotes",
                                side_effect=_fake_get_stock_quotes_factory({"t": 100, "p": 95}, "ok")) as mock_gq, \
             mock.patch.object(server, "_intraday_regime_cached",
                                return_value=({"vwap": 98, "aboveVwap": True, "pattern": "higher_highs", "current": 100}, "ok")) as mock_regime:
            snap = server._intraday_stock_snapshot(w)
        mock_gq.assert_called_once()
        mock_regime.assert_called_once()
        self.assertEqual(snap["current"], 100)
        self.assertEqual(snap["cacheStatus"], "ok")


N_SYMBOLS = 10


def _make_watchlist(n=N_SYMBOLS):
    return [{"code": f"M{i:04d}", "name": f"テスト銘柄{i}", "market": "JP", "sector": "テスト業種"}
            for i in range(n)]


def _make_stage1_rows(watchlist):
    rows = {}
    for i, w in enumerate(watchlist):
        rows[w["code"]] = {
            "current": 1000.0 + i, "changePct": (i % 7) - 3, "high": 1010.0 + i, "low": 990.0 + i,
            "open": 995.0 + i, "volume": 100000 + i * 10, "turnover": (1000.0 + i) * (100000 + i * 10),
            "highRetention": 0.9, "marketRS": (i % 5) - 2, "sectorRS": (i % 3) - 1, "sector": "テスト業種",
        }
    return rows


def _make_hist():
    return [{"date": f"2020-01-{d:02d}", "open": 100 + d - 1, "high": 100 + d + 1,
              "low": 100 + d - 2, "close": 100 + d, "volume": 1000} for d in range(1, 26)]


class NetworkForbidden(Exception):
    pass


def _forbid(*a, **kw):
    raise NetworkForbidden("live network/API call attempted during scan-local market data fixture test")


class ScoreEntryCandidatesScanLocalMarketDataTests(unittest.TestCase):
    """_score_entry_candidates()レベルでのscan-local quote/5分足 reuseを検証する。
    quote（stockquote:{sym}）・5分足（intraday5m:{sym}:5m）のグローバルキャッシュをあえて
    投入しない（cold）状態でも、prefetch_market_data_for_watchlist()が1回だけ取得した値を
    candidate loop全体で使い回し、個別のget_stock_quotes/yf.download呼び出しが発生しないことを
    証明する。"""

    def setUp(self):
        self.watchlist = _make_watchlist()
        self.stage1_rows = _make_stage1_rows(self.watchlist)
        for w in self.watchlist:
            sym = server._yf_symbol(w)
            server._CACHE_STORE.pop(f"stockquote:{sym}", None)
            server._CACHE_STORE.pop(f"intraday5m:{sym}:5m", None)
            server._CACHE_STORE.pop(f"daily_arrays:{w['code']}", None)

    def tearDown(self):
        for w in self.watchlist:
            sym = server._yf_symbol(w)
            server._CACHE_STORE.pop(f"stockquote:{sym}", None)
            server._CACHE_STORE.pop(f"intraday5m:{sym}:5m", None)
            server._CACHE_STORE.pop(f"daily_arrays:{w['code']}", None)

    def _run(self):
        stage1_payload = {"rows": self.stage1_rows, "nikkeiChangePct": 0.3, "scanFailed": False,
                            "builtAt": time.time(), "codesScanned": N_SYMBOLS, "pricesReturned": N_SYMBOLS,
                            "durationSec": 0, "requestCount": 1, "usedStaleCache": False}
        quote_call_log = []

        def fake_get_stock_quotes(watchlist_arg, cache_ttl=0, status_out=None):
            # prefetch_market_data_for_watchlist()からの1回だけのバッチ呼び出しを許可する
            # （watchlist全体を1回で渡してくる＝len>1のケース）。candidate loop側からの
            # 1件ずつの個別呼び出し（len==1）が発生したらテスト対象のバグとして記録する。
            quote_call_log.append(len(watchlist_arg))
            out = {}
            for w in watchlist_arg:
                code = w.get("code")
                row = self.stage1_rows.get(code, {})
                out[code] = {"t": row.get("current"), "p": row.get("current", 0) - 5,
                              "open": row.get("open"), "high": row.get("high"), "low": row.get("low"),
                              "volume": row.get("volume"), "turnover": row.get("turnover"), "spark": [1, 2, 3]}
                if status_out is not None:
                    status_out[code] = "ok"
            return out

        with mock.patch.dict(server.CACHE_TTL, {"stock_quote": 0, "stock5m": 0, "daily_arrays": 90}), \
             mock.patch.object(server.investment_db, "list_watchlist", return_value=self.watchlist), \
             mock.patch.object(server.investment_db, "get_codes_with_auto_tag", return_value=set()), \
             mock.patch.object(server, "run_momentum_stage1", return_value=stage1_payload), \
             mock.patch.object(server.investment_db, "list_news_catalysts", return_value=[]), \
             mock.patch.object(server.investment_db, "list_market_events", return_value=[]), \
             mock.patch.object(server.investment_db, "list_trade_experiences", return_value=[]), \
             mock.patch.object(server.investment_db, "list_event_decision_support_for_tickers",
                                return_value={w["code"]: [] for w in self.watchlist}), \
             mock.patch.object(server, "capture_entry_candidate_snapshot_safe", return_value=None), \
             mock.patch.object(server.tachibana_api, "get_market_price", side_effect=_forbid), \
             mock.patch.object(server.tachibana_api, "get_daily_history", return_value=_make_hist()), \
             mock.patch.object(server, "get_stock_quotes", side_effect=fake_get_stock_quotes), \
             mock.patch.object(server, "_download_intraday_chunk", side_effect=_forbid), \
             mock.patch.object(server, "_intraday_regime_cached", side_effect=_forbid):
            result = server._score_entry_candidates("dummy_db_url", "dummy_user")
        return result, quote_call_log

    def test_quote_and_intraday_scan_local_hit_full_coverage(self):
        """CACHE_TTL(quote/5分足)=0（即期限切れ）という極端な条件でも、candidate loop側の
        get_stock_quotes個別呼び出し（len==1）・_intraday_regime_cached個別呼び出しが
        一切発生しない（scan-local reuseが機能している）ことを証明する。"""
        result, quote_call_log = self._run()
        # 1回目はprefetch_market_data_for_watchlist()からのバッチ呼び出し（len==N_SYMBOLS）。
        # candidate loop側の個別呼び出し（len==1）が1件でもあればscan-local reuseの穴。
        individual_calls = [n for n in quote_call_log if n == 1]
        self.assertEqual(individual_calls, [], f"quote call log (should have no len==1 entries): {quote_call_log}")
        debug = result["debug"]
        self.assertEqual(debug["quoteScanLocalHit"], N_SYMBOLS)
        self.assertEqual(debug["quoteFallbackCount"], 0)
        self.assertEqual(debug["intradayScanLocalHit"], N_SYMBOLS)
        self.assertEqual(debug["intradayFallbackCount"], 0)
        self.assertEqual(debug["scanned"], N_SYMBOLS)

    def test_market_data_by_code_not_leaked_into_api_facing_debug_payload(self):
        """バグ再発防止（arraysByCodeと同種）：marketDataByCode（281銘柄分のquote/5分足そのもの）
        がdebug["marketDataDiagnostics"]へ漏れ出していないことを確認する。"""
        result, _log = self._run()
        mdd = result["debug"]["marketDataDiagnostics"]
        self.assertNotIn("marketDataByCode", mdd)

    def test_prefetch_exception_falls_back_to_individual_path_for_all_codes(self):
        """prefetch_market_data_for_watchlist()自体が丸ごと例外を出した場合（指示書I：安全側）、
        marketDataByCodeが空になり、全銘柄が個別フォールバック経路に回ることを確認する
        （個別呼び出し自体は既存の安全側動作でありnetwork_forbidden例外は握りつぶされる）。"""
        stage1_payload = {"rows": self.stage1_rows, "nikkeiChangePct": 0.3, "scanFailed": False,
                            "builtAt": time.time(), "codesScanned": N_SYMBOLS, "pricesReturned": N_SYMBOLS,
                            "durationSec": 0, "requestCount": 1, "usedStaleCache": False}
        with mock.patch.dict(server.CACHE_TTL, {"daily_arrays": 90}), \
             mock.patch.object(server.investment_db, "list_watchlist", return_value=self.watchlist), \
             mock.patch.object(server.investment_db, "get_codes_with_auto_tag", return_value=set()), \
             mock.patch.object(server, "run_momentum_stage1", return_value=stage1_payload), \
             mock.patch.object(server.investment_db, "list_news_catalysts", return_value=[]), \
             mock.patch.object(server.investment_db, "list_market_events", return_value=[]), \
             mock.patch.object(server.investment_db, "list_trade_experiences", return_value=[]), \
             mock.patch.object(server.investment_db, "list_event_decision_support_for_tickers",
                                return_value={w["code"]: [] for w in self.watchlist}), \
             mock.patch.object(server, "capture_entry_candidate_snapshot_safe", return_value=None), \
             mock.patch.object(server.tachibana_api, "get_daily_history", return_value=_make_hist()), \
             mock.patch.object(server, "prefetch_market_data_for_watchlist", side_effect=RuntimeError("boom")), \
             mock.patch.object(server, "get_stock_quotes",
                                side_effect=_fake_get_stock_quotes_factory({"t": 1, "p": 1}, "ok")), \
             mock.patch.object(server, "_intraday_regime_cached", return_value=(None, "failed")):
            result = server._score_entry_candidates("dummy_db_url", "dummy_user")
        debug = result["debug"]
        self.assertEqual(debug["quoteScanLocalHit"], 0)
        self.assertEqual(debug["quoteFallbackCount"], N_SYMBOLS)
        self.assertEqual(debug["intradayScanLocalHit"], 0)
        self.assertEqual(debug["intradayFallbackCount"], N_SYMBOLS)


if __name__ == "__main__":
    unittest.main()
