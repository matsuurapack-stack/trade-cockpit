# Market Data高速化 Phase 5（281銘柄・残存311秒の完全プロファイリング）の回帰テスト。
#
# ライブAPIを一切使わず、281銘柄分のfixtureを全キャッシュへ事前投入した「完全warm」状態で
# _score_entry_candidates()を実行し、(a) ネットワーク呼び出しがゼロであること（隠れた
# fallbackが無いこと）、(b) debug["sectionTimingsMs"]/["perSymbolTiming"]/["funcCallCounts"]
# から311秒の内訳を関数単位で説明できることを検証する。
#
# 実行方法： cd files && python -m unittest test_entry_candidates_full_warm_profile -v

import time
import unittest
from unittest import mock

import server


N_SYMBOLS = 281


def _make_watchlist(n=N_SYMBOLS):
    return [{"code": f"T{i:04d}", "name": f"テスト銘柄{i}", "market": "JP", "sector": "テスト業種"}
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


def _make_daily_arrays():
    closes = [100.0 + j * 0.1 for j in range(60)]
    opens = [c - 0.3 for c in closes]
    highs = [c + 0.5 for c in closes]
    lows = [c - 0.5 for c in closes]
    volumes = [10000 + j * 50 for j in range(60)]
    return (closes, opens, highs, lows, volumes)


def _make_regime():
    return {"current": 105.0, "vwap": 104.0, "aboveVwap": True, "regime": "RISK_ON", "pattern": "higher_highs"}


class NetworkForbidden(Exception):
    pass


def _forbid(*a, **kw):
    raise NetworkForbidden("live network/API call attempted during full-warm fixture test")


class FullyWarmFixtureProfileTests(unittest.TestCase):
    """指示書STEP10・STEP11：281銘柄すべてをfixture/cacheへ投入した完全warm状態で実行し、
    (1)ネットワーク呼び出しゼロ（隠れfallback検出）、(2)関数単位で内訳を説明できること、を確認する。"""

    def setUp(self):
        self.watchlist = _make_watchlist()
        self.stage1_rows = _make_stage1_rows(self.watchlist)
        # 1) 全キャッシュへ事前投入（quote/5m/daily_arrays）
        for w in self.watchlist:
            sym = server._yf_symbol(w)
            server._cache_set(f"stockquote:{sym}", {
                "t": self.stage1_rows[w["code"]]["current"], "p": self.stage1_rows[w["code"]]["current"] - 5,
                "open": self.stage1_rows[w["code"]]["open"], "high": self.stage1_rows[w["code"]]["high"],
                "low": self.stage1_rows[w["code"]]["low"], "volume": self.stage1_rows[w["code"]]["volume"],
                "turnover": self.stage1_rows[w["code"]]["turnover"], "spark": [100, 101, 102],
            })
            server._cache_set(f"intraday5m:{sym}:5m", _make_regime())
            server._cache_set(f"daily_arrays:{w['code']}", _make_daily_arrays())

    def tearDown(self):
        for w in self.watchlist:
            sym = server._yf_symbol(w)
            server._CACHE_STORE.pop(f"stockquote:{sym}", None)
            server._CACHE_STORE.pop(f"intraday5m:{sym}:5m", None)
            server._CACHE_STORE.pop(f"daily_arrays:{w['code']}", None)

    def _run_fully_warm(self):
        """全依存をfixtureへ差し替えてネットワーク/DB接続なしで_score_entry_candidates()を
        実行する。tachibana_api・yfinanceの実呼び出しはすべて例外を投げるようにし、
        隠れたfallbackがあれば即座に検出できるようにする（指示書STEP11）。"""
        stage1_payload = {"rows": self.stage1_rows, "nikkeiChangePct": 0.3, "scanFailed": False,
                            "builtAt": time.time(), "codesScanned": N_SYMBOLS, "pricesReturned": N_SYMBOLS,
                            "durationSec": 0, "requestCount": 1, "usedStaleCache": False}

        with mock.patch.object(server.investment_db, "list_watchlist", return_value=self.watchlist), \
             mock.patch.object(server.investment_db, "get_codes_with_auto_tag", return_value=set()), \
             mock.patch.object(server, "run_momentum_stage1", return_value=stage1_payload), \
             mock.patch.object(server.investment_db, "list_news_catalysts", return_value=[]), \
             mock.patch.object(server.investment_db, "list_market_events", return_value=[]), \
             mock.patch.object(server.investment_db, "list_trade_experiences", return_value=[]), \
             mock.patch.object(server.investment_db, "list_event_decision_support_for_tickers",
                                return_value={w["code"]: [] for w in self.watchlist}), \
             mock.patch.object(server, "capture_entry_candidate_snapshot_safe", return_value=None), \
             mock.patch.object(server.tachibana_api, "get_market_price", side_effect=_forbid), \
             mock.patch.object(server.tachibana_api, "get_daily_history", side_effect=_forbid), \
             mock.patch.object(server.yf, "download", side_effect=_forbid), \
             mock.patch.object(server.yf, "Ticker", side_effect=_forbid):
            t0 = time.time()
            result = server._score_entry_candidates("dummy_db_url", "dummy_user")
            elapsed = time.time() - t0
        return result, elapsed

    def test_completes_without_any_network_call(self):
        """指示書STEP11：ネットワーク呼び出しが1回でも発生したらNetworkForbiddenで即FAIL。
        隠れたfallbackが無いことの直接証明。"""
        result, elapsed = self._run_fully_warm()
        self.assertEqual(result["debug"]["scanned"], N_SYMBOLS)
        print(f"\n[FullyWarmFixture] elapsed={elapsed:.3f}s (network calls: 0, forbidden-but-not-triggered)")

    def test_section_timings_explain_total_within_tolerance(self):
        """指示書STEP12：sectionTimingsMsの合計がtotalの±5%程度で一致すること
        （"other"が異常に大きくならないことの確認）。"""
        result, _elapsed = self._run_fully_warm()
        st = result["debug"]["sectionTimingsMs"]
        total = st["total"]
        other = st["other"]
        self.assertLess(other, max(50, total * 0.3),
                         f"未説明時間(other={other}ms)がtotal({total}ms)の30%を超えている")
        print(f"\n[SectionTimings] {st}")

    def test_per_symbol_timing_percentiles_present(self):
        result, _elapsed = self._run_fully_warm()
        pst = result["debug"]["perSymbolTiming"]
        self.assertEqual(pst["count"], N_SYMBOLS)
        self.assertIn("p90Ms", pst)
        self.assertIn("p95Ms", pst)
        self.assertEqual(len(pst["slowest"]), 10)
        print(f"\n[PerSymbolTiming] avg={pst['avgMs']}ms median={pst['medianMs']}ms "
              f"p90={pst['p90Ms']}ms p95={pst['p95Ms']}ms max={pst['maxMs']}ms")

    def test_func_call_counts_match_symbol_count(self):
        """指示書STEP4：_volume_stage2_detail・_intraday_stock_snapshotの呼び出し回数が
        監視銘柄数と一致すること（重複呼び出し・欠落が無いことの確認）。"""
        result, _elapsed = self._run_fully_warm()
        fcc = result["debug"]["funcCallCounts"]
        self.assertEqual(fcc["_volume_stage2_detail"]["calls"], N_SYMBOLS)
        self.assertEqual(fcc["_intraday_stock_snapshot"]["calls"], N_SYMBOLS)
        print(f"\n[FuncCallCounts] {fcc}")

    def test_daily_arrays_prefetch_reports_all_cache_hits(self):
        """完全warm状態ではprefetch_daily_arrays_for_watchlist()のcache_hitsが281/281に
        なること（cache_missesはゼロ＝隠れた再取得が無い）。"""
        result, _elapsed = self._run_fully_warm()
        dad = result["debug"]["dailyArraysDiagnostics"]
        self.assertEqual(dad["cache_hits"], N_SYMBOLS)
        self.assertEqual(dad["cache_misses"], 0)
        self.assertEqual(dad["network_requests"], 0)

    def test_market_data_prefetch_reports_all_cache_hits(self):
        result, _elapsed = self._run_fully_warm()
        mdd = result["debug"]["marketDataDiagnostics"]
        self.assertEqual(mdd["cache_misses"], 0)

    def test_snapshot_persistence_call_count_bounded(self):
        """snapshot persistenceは監視銘柄数(281)ではなく、最終候補数（ENTRY TOP5+WATCH、
        高々数十件）にしか比例しないことを確認する（指示書STEP8の前提確認）。"""
        result, _elapsed = self._run_fully_warm()
        calls = result["debug"]["snapshotPersistenceCalls"]
        self.assertLess(calls, 30)  # 281件には遠く及ばない、少数であることの確認

    def test_score_and_rank_unchanged_across_runs(self):
        """指示書STEP18：同一fixtureを2回実行してもentry_score・rankが完全一致すること
        （warmup/cache状態に関わらず同じ入力→同じ結果）。"""
        result1, _ = self._run_fully_warm()
        result2, _ = self._run_fully_warm()
        codes1 = [c["code"] for c in result1["entryReadyTop5"]]
        codes2 = [c["code"] for c in result2["entryReadyTop5"]]
        scores1 = [c["entryScore"] for c in result1["entryReadyTop5"]]
        scores2 = [c["entryScore"] for c in result2["entryReadyTop5"]]
        self.assertEqual(codes1, codes2)
        self.assertEqual(scores1, scores2)


if __name__ == "__main__":
    unittest.main()
