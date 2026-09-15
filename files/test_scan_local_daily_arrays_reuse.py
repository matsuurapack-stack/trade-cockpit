# 今買い時TOP5 場中リアルタイム化指示書 レビュー反映：scan-local daily arrays reuseの回帰テスト。
#
# 実測で判明した問題：CACHE_TTL["daily_arrays"]=90秒は、281銘柄を逐次取得する
# prefetch_daily_arrays_for_watchlist()自体の所要時間（実測約295秒）より短いため、
# 同一scan内でもprefetch前半で書き込んだキャッシュがcandidate loopに到達する頃には
# TTL切れになり、Tachibana daily historyの二重取得が発生していた（実測682秒中の主要因）。
#
# ここでは「グローバルTTLが極端に短い（=常に期限切れ）状態でも、同一scan内では
# prefetch結果（arraysByCode）を使い回し、tachibana_api.get_daily_history()が
# 銘柄あたり1回しか呼ばれないこと」を実測ではなくunit testで機械的に証明する。
#
# 実行方法： cd files && python -m unittest test_scan_local_daily_arrays_reuse -v

import time
import unittest
from unittest import mock

import server


N_SYMBOLS = 10


def _make_watchlist(n=N_SYMBOLS):
    return [{"code": f"S{i:04d}", "name": f"テスト銘柄{i}", "market": "JP", "sector": "テスト業種"}
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
    # 前営業日までの確定値のみ（当日分はlive_quoteから合成される想定、_tachibana_daily_arrays()の既存分岐）。
    return [{"date": f"2020-01-{d:02d}", "open": 100 + d - 1, "high": 100 + d + 1,
              "low": 100 + d - 2, "close": 100 + d, "volume": 1000} for d in range(1, 26)]


def _make_regime():
    return {"current": 105.0, "vwap": 104.0, "aboveVwap": True, "regime": "RISK_ON", "pattern": "higher_highs"}


class NetworkForbidden(Exception):
    pass


def _forbid(*a, **kw):
    raise NetworkForbidden("get_market_price should not be called: live_quote was available")


class ScanLocalDailyArraysReuseTests(unittest.TestCase):
    """CACHE_TTL["daily_arrays"]を0秒（常に即期限切れ）にした極端な条件でも、同一scan内は
    tachibana_api.get_daily_history()が銘柄あたり1回しか呼ばれないことを検証する。"""

    def setUp(self):
        self.watchlist = _make_watchlist()
        self.stage1_rows = _make_stage1_rows(self.watchlist)
        for w in self.watchlist:
            sym = server._yf_symbol(w)
            server._cache_set(f"stockquote:{sym}", {
                "t": self.stage1_rows[w["code"]]["current"], "p": self.stage1_rows[w["code"]]["current"] - 5,
                "open": self.stage1_rows[w["code"]]["open"], "high": self.stage1_rows[w["code"]]["high"],
                "low": self.stage1_rows[w["code"]]["low"], "volume": self.stage1_rows[w["code"]]["volume"],
                "turnover": self.stage1_rows[w["code"]]["turnover"], "spark": [100, 101, 102],
            })
            server._cache_set(f"intraday5m:{sym}:5m", _make_regime())
            server._CACHE_STORE.pop(f"daily_arrays:{w['code']}", None)
            server._CACHE_STORE.pop(f"daily_history:{server._jst_today_date_str()}:{w['code']}", None)

    def tearDown(self):
        for w in self.watchlist:
            sym = server._yf_symbol(w)
            server._CACHE_STORE.pop(f"stockquote:{sym}", None)
            server._CACHE_STORE.pop(f"intraday5m:{sym}:5m", None)
            server._CACHE_STORE.pop(f"daily_arrays:{w['code']}", None)
            server._CACHE_STORE.pop(f"daily_history:{server._jst_today_date_str()}:{w['code']}", None)

    def _run(self, daily_arrays_ttl):
        stage1_payload = {"rows": self.stage1_rows, "nikkeiChangePct": 0.3, "scanFailed": False,
                            "builtAt": time.time(), "codesScanned": N_SYMBOLS, "pricesReturned": N_SYMBOLS,
                            "durationSec": 0, "requestCount": 1, "usedStaleCache": False}
        call_log = []

        def fake_get_daily_history(code):
            call_log.append(code)
            time.sleep(0.01)  # prefetchの逐次取得が現実的な時間を要することをシミュレート
            return _make_hist()

        with mock.patch.dict(server.CACHE_TTL, {"daily_arrays": daily_arrays_ttl}), \
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
             mock.patch.object(server.tachibana_api, "get_daily_history", side_effect=fake_get_daily_history), \
             mock.patch.object(server.yf, "download", side_effect=_forbid), \
             mock.patch.object(server.yf, "Ticker", side_effect=_forbid):
            result = server._score_entry_candidates("dummy_db_url", "dummy_user")
        return result, call_log

    def test_get_daily_history_called_at_most_once_per_code_even_with_zero_ttl(self):
        """本質のテスト：TTLが0秒（＝取得した瞬間から期限切れ）でも、同一scan内では
        prefetch結果を使い回すためget_daily_history()は銘柄数と同じ回数（1銘柄1回）しか
        呼ばれない。scan-local reuseが無ければTTL=0はcandidate loopで即再取得を引き起こし、
        呼び出し回数がN_SYMBOLSの2倍になるはずの構成。"""
        result, call_log = self._run(daily_arrays_ttl=0)
        self.assertEqual(len(call_log), N_SYMBOLS, f"get_daily_history call count mismatch: {call_log}")
        self.assertEqual(len(set(call_log)), N_SYMBOLS)  # 重複コードなし＝二重取得が無いことの直接証明
        self.assertEqual(result["debug"]["dailyArraysScanLocalHit"], N_SYMBOLS)
        self.assertEqual(result["debug"]["dailyArraysFallbackCount"], 0)

    def test_scan_local_hit_matches_watchlist_when_ttl_generous(self):
        """TTLが十分長い通常ケースでも、scan-local reuse自体は変わらず機能し、
        get_daily_history呼び出しは銘柄あたり1回のまま（regressionでないことの確認）。"""
        result, call_log = self._run(daily_arrays_ttl=90)
        self.assertEqual(len(call_log), N_SYMBOLS)
        self.assertEqual(result["debug"]["dailyArraysScanLocalHit"], N_SYMBOLS)
        self.assertEqual(result["debug"]["dailyArraysFallbackCount"], 0)

    def test_score_entry_candidates_scanned_count_unaffected(self):
        result, _call_log = self._run(daily_arrays_ttl=0)
        self.assertEqual(result["debug"]["scanned"], N_SYMBOLS)

    def test_arrays_by_code_not_leaked_into_api_facing_debug_payload(self):
        """バグ再発防止：arraysByCode（281銘柄×400営業日分のOHLCV配列、実測で数MB級）が
        debug["dailyArraysDiagnostics"]（/api/entry-candidates・/api/entry-candidates/live
        のレスポンスにそのまま乗る）へ漏れ出していないことを確認する。scan-local reuse実装の
        初回AFTER測定で実際にこの漏れが発生し、検証スクリプトの出力が4.7MBに膨張した
        （ログ出力上の問題にとどまらず、本番APIレスポンスも同様に膨張する実害があった）。"""
        result, _call_log = self._run(daily_arrays_ttl=90)
        dad = result["debug"]["dailyArraysDiagnostics"]
        self.assertNotIn("arraysByCode", dad)


if __name__ == "__main__":
    unittest.main()
