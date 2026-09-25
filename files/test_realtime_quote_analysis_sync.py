# リアルタイム価格・分析同期（2026-09-25）の回帰テスト。
# 症状：「quoteは新値なのにanalysisは旧値」（登録時/13:28の価格で詳細分析が固定される）の再現と
# 修正の確認。実行： cd files && python -m unittest test_realtime_quote_analysis_sync -v

import unittest
from unittest import mock

import server


def _fq(code, t, ts="2026-09-25T10:00:00+09:00", stale=False, p=2200.0):
    return {"t": t, "p": p, "change": t - p, "changePct": (t - p) / p * 100, "volume": 1000, "open": p,
            "high": t, "low": p, "ask": None, "bid": None, "source": "tachibana",
            "quote_timestamp": ts, "fetched_at": ts, "is_stale": stale}


class LatestQuoteSharedReuseTests(unittest.TestCase):
    def setUp(self):
        server._fast_quote_cache.clear()

    def test_two_consumers_within_window_get_same_price_and_timestamp(self):
        calls = []

        def fake_get_market_price(codes):
            calls.append(list(codes))
            return {c: {"t": 2273.0, "p": 2200.0, "change": 73.0, "changePct": 3.3, "volume": 1, "open": 2200.0,
                        "high": 2280.0, "low": 2199.0, "ask": None, "bid": None} for c in codes}
        with mock.patch.object(server.tachibana_api, "get_market_price", side_effect=fake_get_market_price):
            q1, _ = server.get_fast_quotes([{"code": "5301", "market": "JP"}])
            q2, st2 = server.get_fast_quotes([{"code": "5301", "market": "JP"}])
        self.assertEqual(len(calls), 1)  # 2回目は共有latest quoteを再利用（別取得しない）
        self.assertEqual(q1["5301"]["t"], q2["5301"]["t"])
        self.assertEqual(q1["5301"]["quote_timestamp"], q2["5301"]["quote_timestamp"])
        self.assertEqual(st2.get("reused"), 1)

    def test_fallback_quote_is_shared_but_stays_marked_stale(self):
        """立花が使えずyfinanceフォールバックになった場合も、窓内は全consumerが同じ値・同じ
        timestampを見る。ただしis_stale=Trueは維持する（古い値を新鮮と偽らない）。"""
        with mock.patch.object(server.tachibana_api, "get_market_price", return_value={}),              mock.patch("server.time.sleep"),              mock.patch.object(server, "get_stock_quotes",
                                return_value={"5301": {"t": 2273.0, "p": 2200.0, "volume": 1}}) as yf:
            q1, _ = server.get_fast_quotes([{"code": "5301", "market": "JP"}])
            q2, _ = server.get_fast_quotes([{"code": "5301", "market": "JP"}])
        self.assertEqual(yf.call_count, 1)
        self.assertEqual(q1["5301"], q2["5301"])
        self.assertTrue(q2["5301"]["is_stale"])
        self.assertEqual(q2["5301"]["source"], "yfinance_fallback")

    def test_expired_entry_is_refetched(self):
        with mock.patch.object(server.tachibana_api, "get_market_price",
                                return_value={"5301": {"t": 2273.0, "p": 2200.0}}) as m:
            server.get_fast_quotes([{"code": "5301", "market": "JP"}])
            server._fast_quote_cache["5301"]["at"] -= (server.LATEST_QUOTE_REUSE_SEC + 1)
            server.get_fast_quotes([{"code": "5301", "market": "JP"}])
        self.assertEqual(m.call_count, 2)


class BuildAnalysisUsesLatestQuoteTests(unittest.TestCase):
    """症状の再現：クライアントが古い現在値(2273)を送っても、サーバーlatest quote(2290)で分析される。"""

    def setUp(self):
        server._ANALYSIS_DYNAMIC_CACHE.clear()

    def _run(self, quote_t, client_current, dynamic_only=True, ts="2026-09-25T10:00:00+09:00"):
        seen = {}

        def fake_analyze(w, market_env=None, external_intelligence=None):
            seen["current"] = w.get("current")
            return {"code": w["code"], "current": w["current"], "score": w["current"] / 100.0}
        with mock.patch.object(server, "get_fast_quotes",
                                return_value=({"5301": _fq("5301", quote_t, ts)}, {})), \
             mock.patch.object(server, "analyze_stock", side_effect=fake_analyze), \
             mock.patch.object(server, "_market_environment", return_value="x"), \
             mock.patch.object(server, "yf", object()):
            out = server.build_analysis([{"code": "5301", "market": "JP", "current": client_current}],
                                         dynamic_only=dynamic_only)
        return out["5301"], seen

    def test_stale_client_price_is_overridden_by_latest_quote(self):
        r, seen = self._run(2290.0, 2273.0)
        self.assertEqual(seen["current"], 2290.0)
        self.assertEqual(r["current"], 2290.0)
        self.assertEqual(r["currentSource"], "SERVER_LATEST_QUOTE")
        self.assertEqual(r["clientCurrent"], 2273.0)
        self.assertEqual(r["quoteAt"], "2026-09-25T10:00:00+09:00")
        self.assertIsNotNone(r["analyzedAt"])

    def test_price_change_recomputes_analysis_and_advances_timestamp(self):
        r1, _ = self._run(2273.0, 2273.0)
        r2, _ = self._run(2250.0, 2273.0)  # 下落 → 分析が再計算されscoreが変わる
        self.assertNotEqual(r1["score"], r2["score"])
        self.assertFalse(r2["analysisReused"])
        self.assertGreaterEqual(r2["analyzedAt"], r1["analyzedAt"])

    def test_unchanged_price_reuses_dynamic_analysis(self):
        r1, _ = self._run(2273.0, 2273.0)
        r2, _seen2 = self._run(2273.0, 2273.0)
        self.assertTrue(r2["analysisReused"])
        self.assertEqual(r2["analyzedAt"], r1["analyzedAt"])  # 再計算していない事実を隠さない

    def test_falls_back_to_client_price_when_no_quote(self):
        with mock.patch.object(server, "get_fast_quotes", return_value=({}, {})), \
             mock.patch.object(server, "analyze_stock",
                                side_effect=lambda w, *a, **k: {"code": w["code"], "current": w["current"]}), \
             mock.patch.object(server, "_market_environment", return_value="x"), \
             mock.patch.object(server, "yf", object()):
            out = server.build_analysis([{"code": "5301", "market": "JP", "current": 2273.0}], dynamic_only=True)
        self.assertEqual(out["5301"]["currentSource"], "CLIENT_SENT")
        self.assertEqual(out["5301"]["current"], 2273.0)

    def test_dynamic_only_skips_static_extras(self):
        with mock.patch.object(server, "get_fast_quotes", return_value=({"5301": _fq("5301", 2290.0)}, {})), \
             mock.patch.object(server, "analyze_stock", side_effect=lambda w, *a, **k: {"code": w["code"]}), \
             mock.patch.object(server, "_market_environment", return_value="x"), \
             mock.patch.object(server, "yf", object()), \
             mock.patch.object(server.tachibana_api, "get_issue_detail") as gid:
            server.build_analysis([{"code": "5301", "market": "JP"}], dynamic_only=True)
        gid.assert_not_called()


class Stage1OverlayTests(unittest.TestCase):
    def test_overlay_replaces_price_without_mutating_cache(self):
        base = {"5301": {"code": "5301", "current": 2200.0, "changePct": 0.0, "high": 2210.0, "low": 2190.0,
                          "volume": 10, "turnover": 22000.0, "highRetention": 0.99, "marketRS": 0.0, "sectorRS": 0.1}}
        with mock.patch.object(server, "get_fast_quotes", return_value=({"5301": _fq("5301", 2290.0, p=2200.0)}, {})):
            rows, quotes = server.overlay_latest_quotes_on_stage1_rows(base, ["5301"], nikkei_chg=1.0)
        self.assertEqual(rows["5301"]["current"], 2290.0)
        self.assertEqual(rows["5301"]["_priceSource"], "LATEST_QUOTE")
        self.assertAlmostEqual(rows["5301"]["marketRS"], rows["5301"]["changePct"] - 1.0)
        self.assertEqual(rows["5301"]["sectorRS"], 0.1)  # 再計算できない値はStage1のまま
        self.assertEqual(base["5301"]["current"], 2200.0)  # 共有Stage1キャッシュ本体は不変

    def test_overlay_marks_stage1_fallback_when_no_quote(self):
        base = {"5301": {"code": "5301", "current": 2200.0, "changePct": 0.0}}
        with mock.patch.object(server, "get_fast_quotes", return_value=({}, {})):
            rows, _ = server.overlay_latest_quotes_on_stage1_rows(base, ["5301"])
        self.assertEqual(rows["5301"]["_priceSource"], "STAGE1_CACHE")
        self.assertEqual(rows["5301"]["current"], 2200.0)


class EntryResultOverlayTests(unittest.TestCase):
    def test_live_endpoint_overlay_updates_price_but_not_score(self):
        result = {"generatedAt": "2026-09-25T01:00:00+00:00",
                  "entryReadyTop5": [{"code": "5301", "current": 2273.0, "changePct": 1.0, "entryScore": 70,
                                       "entryState": "ENTRY_READY"}],
                  "watchCandidates": []}
        with mock.patch.object(server, "get_fast_quotes", return_value=({"5301": _fq("5301", 2290.0, p=2200.0)}, {})):
            out = server.overlay_latest_quotes_on_entry_result(result)
        c = out["entryReadyTop5"][0]
        self.assertEqual(c["current"], 2290.0)
        self.assertEqual(c["scoredPrice"], 2273.0)
        self.assertEqual(c["entryScore"], 70)  # スコア自体は再計算しない（スキャンが担当）
        self.assertTrue(c["priceOverlaid"])
        self.assertEqual(result["entryReadyTop5"][0]["current"], 2273.0)  # キャッシュ本体は不変


class SnapshotVsLatestQuoteRegressionTests(unittest.TestCase):
    """必須ケース：snapshot=2273 → latest quote=2290 → API取得 → current_price=2290 →
    analysisにも2290が渡される → analysis_timestampが更新される。
    「表示値だけ2290、分析内部は2273」ならFAILする。"""

    def setUp(self):
        server._ANALYSIS_DYNAMIC_CACHE.clear()

    def test_display_and_analysis_input_both_use_latest_quote_and_timestamp_advances(self):
        inputs = []

        def fake_analyze(w, market_env=None, external_intelligence=None):
            inputs.append(w["current"])
            # 分析内部の価格依存値（前日比%）が入力価格から計算される
            return {"code": w["code"], "current": w["current"], "changePct": (w["current"] - 2200.0) / 2200.0 * 100}
        snapshot = {"code": "5301", "market": "JP", "current": 2273.0}  # 登録時/古いsnapshot
        with mock.patch.object(server, "get_fast_quotes",
                                return_value=({"5301": _fq("5301", 2273.0, "2026-09-25T10:00:00+09:00")}, {})),              mock.patch.object(server, "analyze_stock", side_effect=fake_analyze),              mock.patch.object(server, "_market_environment", return_value="x"),              mock.patch.object(server, "yf", object()):
            first = server.build_analysis([snapshot], dynamic_only=True)["5301"]
        import time as _t
        _t.sleep(0.01)
        with mock.patch.object(server, "get_fast_quotes",
                                return_value=({"5301": _fq("5301", 2290.0, "2026-09-25T10:00:30+09:00")}, {})),              mock.patch.object(server, "analyze_stock", side_effect=fake_analyze),              mock.patch.object(server, "_market_environment", return_value="x"),              mock.patch.object(server, "yf", object()):
            second = server.build_analysis([snapshot], dynamic_only=True)["5301"]  # クライアントは2273のまま
        self.assertEqual(second["current"], 2290.0)                 # 表示値
        self.assertEqual(inputs[-1], 2290.0)                        # analysis内部入力
        self.assertAlmostEqual(second["changePct"], (2290.0 - 2200.0) / 2200.0 * 100)  # 内部計算も新価格
        self.assertNotEqual(first["changePct"], second["changePct"])
        self.assertGreater(second["analyzedAt"], first["analyzedAt"])   # analysis_timestamp更新
        self.assertGreater(second["quoteAt"], first["quoteAt"])         # quote_timestamp更新


if __name__ == "__main__":
    unittest.main()
