# Market Data高速化 Phase 5（get_stock_quotes()の隠れfallback修正）の回帰テスト。
#
# STEP5〜6・STEP11のネットワーク禁止fixtureテストで発見：_overlay_tachibana_prices()が
# cache_ttl>0でも毎回無条件にtachibana_api.get_market_price()を呼んでいたため、
# _intraday_stock_snapshot()がwatchlist 1件ずつget_stock_quotes([item], cache_ttl=90)を
# 呼ぶ既存ループで、キャッシュヒット時にも毎回Tachibana個別呼び出しが発生していた
# （Phase1のバッチ化を部分的に無効化していた隠れfallback）。
#
# 実行方法： cd files && python -m unittest test_stock_quote_overlay_cache -v

import unittest
from unittest import mock

import server


def make_frame(close, high, low, open_, volume):
    import pandas as pd
    idx = pd.date_range("2026-09-08", periods=5, freq="D")
    return pd.DataFrame({
        "Open": [open_] * 5, "High": [high] * 5, "Low": [low] * 5,
        "Close": [close - 1, close - 0.5, close - 0.2, close - 0.1, close],
        "Volume": [volume] * 5,
    }, index=idx)


class GetStockQuotesOverlayCachingTests(unittest.TestCase):
    def setUp(self):
        for code in ["7203", "9984", "6501"]:
            sym = code + ".T"
            server._CACHE_STORE.pop(f"stockquote:{sym}", None)

    def _watchlist(self, codes):
        return [{"code": c, "market": "JP", "name": f"銘柄{c}"} for c in codes]

    def test_cache_ttl_zero_overlays_every_call_unchanged_behavior(self):
        """既存呼び出し元（cache_ttl省略=0）は従来通り毎回オーバーレイし続けること
        （メインダッシュボード等の動作を完全維持）。"""
        wl = self._watchlist(["7203"])
        frame = make_frame(3000, 3010, 2990, 2995, 10000)
        with mock.patch.object(server, "_download_chunk", return_value={"7203.T": frame}), \
             mock.patch.object(server.tachibana_api, "get_market_price",
                                return_value={"7203": {"t": 3050, "p": None, "open": None,
                                                         "high": None, "low": None, "volume": None}}) as mock_gmp:
            server.get_stock_quotes(wl)  # cache_ttl=0
            server.get_stock_quotes(wl)  # 2回目
        self.assertEqual(mock_gmp.call_count, 2)  # 毎回呼ばれる（既存動作）

    def test_cache_ttl_positive_skips_overlay_on_cache_hit(self):
        """指示書STEP5〜6：cache_ttl>0の場合、2回目の呼び出し（cache hit）では
        tachibana_api.get_market_price()を一切呼ばないこと（隠れfallbackの解消）。"""
        wl = self._watchlist(["7203"])
        frame = make_frame(3000, 3010, 2990, 2995, 10000)
        with mock.patch.object(server, "_download_chunk", return_value={"7203.T": frame}), \
             mock.patch.object(server.tachibana_api, "get_market_price",
                                return_value={"7203": {"t": 3050, "p": None, "open": None,
                                                         "high": None, "low": None, "volume": None}}) as mock_gmp:
            out1 = server.get_stock_quotes(wl, cache_ttl=90)
            out2 = server.get_stock_quotes(wl, cache_ttl=90)
        mock_gmp.assert_called_once()  # 1回目だけ
        self.assertEqual(out1["7203"]["t"], 3050)  # 1回目でオーバーレイ済み
        self.assertEqual(out2["7203"]["t"], 3050)  # 2回目もオーバーレイ済みの値（キャッシュから）

    def test_partial_cache_hit_only_overlays_new_symbols(self):
        """一部銘柄だけキャッシュ切れの場合、オーバーレイ対象は新規取得分のみに絞られること
        （watchlist全体ではない）。"""
        wl = self._watchlist(["7203", "9984"])
        frame7203 = make_frame(3000, 3010, 2990, 2995, 10000)
        frame9984 = make_frame(8000, 8010, 7990, 7995, 20000)

        with mock.patch.object(server, "_download_chunk",
                                return_value={"7203.T": frame7203, "9984.T": frame9984}), \
             mock.patch.object(server.tachibana_api, "get_market_price",
                                return_value={"7203": {"t": 3050, "p": None, "open": None,
                                                         "high": None, "low": None, "volume": None},
                                               "9984": {"t": 8050, "p": None, "open": None,
                                                         "high": None, "low": None, "volume": None}}):
            server.get_stock_quotes(wl, cache_ttl=90)  # 両方キャッシュへ投入

        # 9984だけキャッシュを飛ばす（=cache miss化）
        server._CACHE_STORE.pop("stockquote:9984.T", None)

        with mock.patch.object(server, "_download_chunk", return_value={"9984.T": frame9984}), \
             mock.patch.object(server.tachibana_api, "get_market_price",
                                return_value={"9984": {"t": 8100, "p": None, "open": None,
                                                         "high": None, "low": None, "volume": None}}) as mock_gmp2:
            out = server.get_stock_quotes(wl, cache_ttl=90)
        # get_market_priceに渡されたのは9984だけ（7203はcache hitのため対象外）
        called_codes = mock_gmp2.call_args[0][0]
        self.assertEqual(called_codes, ["9984"])
        self.assertEqual(out["7203"]["t"], 3050)  # キャッシュ済みの値のまま
        self.assertEqual(out["9984"]["t"], 8100)  # 新たにオーバーレイされた値

    def test_no_cache_miss_means_zero_tachibana_calls(self):
        """指示書STEP11の核心：全銘柄がcache hitならtachibana_api.get_market_price()は
        1回も呼ばれないこと（281回の個別ループでもcache hitが続く限りネットワーク呼び出し
        ゼロになることの単体確認）。"""
        wl = self._watchlist(["7203"])
        frame = make_frame(3000, 3010, 2990, 2995, 10000)
        with mock.patch.object(server, "_download_chunk", return_value={"7203.T": frame}), \
             mock.patch.object(server.tachibana_api, "get_market_price",
                                return_value={"7203": {"t": 3050, "p": None, "open": None,
                                                         "high": None, "low": None, "volume": None}}):
            server.get_stock_quotes(wl, cache_ttl=90)  # ウォームアップ

        with mock.patch.object(server, "_download_chunk") as mock_dl, \
             mock.patch.object(server.tachibana_api, "get_market_price") as mock_gmp:
            for _ in range(5):  # 5回連続で呼んでも
                server.get_stock_quotes(wl, cache_ttl=90)
        mock_dl.assert_not_called()
        mock_gmp.assert_not_called()

    def test_tachibana_unavailable_does_not_break_cache_ttl_path(self):
        wl = self._watchlist(["7203"])
        frame = make_frame(3000, 3010, 2990, 2995, 10000)
        with mock.patch.object(server, "_download_chunk", return_value={"7203.T": frame}), \
             mock.patch.object(server, "tachibana_api", None):
            out = server.get_stock_quotes(wl, cache_ttl=90)
        self.assertIn("7203", out)
        self.assertIsNotNone(out["7203"]["t"])  # yfinance値のまま（オーバーレイなしでも壊れない）


if __name__ == "__main__":
    unittest.main()
