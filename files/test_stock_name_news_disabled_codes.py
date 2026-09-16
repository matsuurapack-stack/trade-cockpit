# 登録銘柄ニュース：4銘柄の一般ニュース完全除外（2026-09-16新規）の回帰テスト。
#
# STOCK_NAME_NEWS_DISABLED_CODES（1812鹿島・2282日本ハム・2801キッコーマン・2802味の素）を
# build_stock_name_news()の一般ニュース（企業名検索によるGoogleニュース）取得対象から
# 除外する。TDnet/公式IR（build_stock_news・build_disclosure_news）には影響させない。
#
# 実行方法： cd files && python -m unittest test_stock_name_news_disabled_codes -v

import unittest
from unittest import mock

import server


def make_watchlist_item(code, name, watch="優先", market="JP"):
    return {"code": code, "name": name, "watch": watch, "market": market}


class StockNameNewsDisabledCodesTests(unittest.TestCase):
    """指示書：build_stock_name_news()はdisabled codeについてGoogleニュース検索を
    一切行わない（1. disabled codeは一般ニュースを返さない）。"""

    def test_disabled_code_skips_google_news_entirely(self):
        wl = [make_watchlist_item("2282", "日本ハム")]
        with mock.patch.object(server, "_tachibana_stock_news", return_value=[]), \
             mock.patch.object(server, "google_news") as mock_gn:
            items = server.build_stock_name_news(wl)
        mock_gn.assert_not_called()
        self.assertEqual(items, [])

    def test_all_four_disabled_codes_skip_google_news(self):
        wl = [make_watchlist_item("1812", "鹿島"), make_watchlist_item("2282", "日本ハム"),
              make_watchlist_item("2801", "キッコーマン"), make_watchlist_item("2802", "味の素")]
        with mock.patch.object(server, "_tachibana_stock_news", return_value=[]), \
             mock.patch.object(server, "google_news") as mock_gn:
            server.build_stock_name_news(wl)
        mock_gn.assert_not_called()

    def test_disabled_code_keeps_nqn_disclosure_category_only(self):
        """2026-09-17緊急修正：NQNのうちAI開示速報（カテゴリ120/129）は公式開示速報として
        除外対象外だが、カテゴリ100（一般ニュース、スポーツ・地域記事等を含みうる）は
        disabled codeについて除外されること（前回修正の漏れの直接原因）。"""
        wl = [make_watchlist_item("2282", "日本ハム")]
        disclosure_item = {"code": "2282", "name": "日本ハム", "title": "日本ハム、通期業績予想を上方修正",
                             "url": "", "source": "NQN", "published": "09/16 09:00", "_ts": 1,
                             "nqnCategory": "120"}
        general_item = {"code": "2282", "name": "日本ハム", "title": "“超逸材”がまさか…日本ハム、期待外れのドラ1戦士",
                         "url": "", "source": "NQN", "published": "09/16 09:00", "_ts": 2,
                         "nqnCategory": "100"}
        with mock.patch.object(server, "_tachibana_stock_news", return_value=[disclosure_item, general_item]), \
             mock.patch.object(server, "google_news") as mock_gn:
            items = server.build_stock_name_news(wl)
        mock_gn.assert_not_called()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["source"], "NQN")
        self.assertIn("上方修正", items[0]["title"])  # 開示速報だけが残る、一般記事は落ちる

    def test_non_disabled_code_unaffected(self):
        """4. disabledでない銘柄には影響しない（従来通りGoogleニュースを取得する）。"""
        wl = [make_watchlist_item("7203", "トヨタ自動車")]
        with mock.patch.object(server, "_tachibana_stock_news", return_value=[]), \
             mock.patch.object(server, "google_news", return_value=[
                 {"title": "トヨタ自動車、新型EV発表", "url": "https://example.com/1",
                  "source": "日本経済新聞", "published": "09/16 10:00", "_ts": 100}]) as mock_gn:
            items = server.build_stock_name_news(wl)
        mock_gn.assert_called()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["code"], "7203")

    def test_mixed_watchlist_only_disabled_codes_excluded(self):
        wl = [make_watchlist_item("2282", "日本ハム"), make_watchlist_item("7203", "トヨタ自動車")]
        with mock.patch.object(server, "_tachibana_stock_news", return_value=[]), \
             mock.patch.object(server, "google_news", return_value=[
                 {"title": "トヨタ自動車、新型EV発表", "url": "https://example.com/1",
                  "source": "日本経済新聞", "published": "09/16 10:00", "_ts": 100}]):
            items = server.build_stock_name_news(wl)
        codes = {it["code"] for it in items}
        self.assertNotIn("2282", codes)
        self.assertIn("7203", codes)


class TdnetAndOfficialIrUnaffectedTests(unittest.TestCase):
    """指示書：TDnet/公式IRは従来どおり表示する（2. disabled codeでもTDnetは残る、
    3. disabled codeでも公式IRは残る）。build_stock_news()・build_disclosure_news()は
    STOCK_NAME_NEWS_DISABLED_CODESを一切参照しないため、コードを読むだけでも自明だが、
    実際に対象4銘柄のTDnet開示が通ることを動作で確認する。"""

    def test_build_stock_news_tdnet_path_ignores_disabled_codes(self):
        wl = [make_watchlist_item("2282", "日本ハム")]
        tdnet_disclosures = {"2282": [{"title": "日本ハム 通期業績予想の上方修正に関するお知らせ",
                                          "url": "https://tdnet.example/1", "time": "15:00"}]}
        with mock.patch.object(server, "_tdnet_today_disclosures", return_value=tdnet_disclosures):
            items = server.build_stock_news(wl)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["code"], "2282")
        self.assertEqual(items[0]["source"], "TDnet")

    def test_build_disclosure_news_ignores_disabled_codes(self):
        wl = [make_watchlist_item("1812", "鹿島")]
        tdnet_disclosures = {"1812": [{"title": "鹿島 配当予想の修正に関するお知らせ",
                                          "url": "https://tdnet.example/2", "time": "12:00", "date": "20260916"}]}
        with mock.patch.object(server, "_tdnet_recent_disclosures", return_value=tdnet_disclosures):
            items = server.build_disclosure_news(wl)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["code"], "1812")
        self.assertEqual(items[0]["source"], "TDnet")

    def test_all_four_disabled_codes_still_get_tdnet(self):
        codes_names = [("1812", "鹿島"), ("2282", "日本ハム"), ("2801", "キッコーマン"), ("2802", "味の素")]
        wl = [make_watchlist_item(c, n) for c, n in codes_names]
        tdnet_disclosures = {c: [{"title": f"{n} 決算短信", "url": f"https://tdnet.example/{c}", "time": "15:00"}]
                               for c, n in codes_names}
        with mock.patch.object(server, "_tdnet_today_disclosures", return_value=tdnet_disclosures), \
             mock.patch.object(server, "summarize_earnings_pdf", return_value=None):
            items = server.build_stock_news(wl)
        result_codes = {it["code"] for it in items}
        self.assertEqual(result_codes, {"1812", "2282", "2801", "2802"})


class ExistingNewsBehaviorUnbrokenTests(unittest.TestCase):
    """指示書：5. 既存ニュース重要度・通知・dedupeを壊さない。build_stock_name_news()の
    戻り値が引き続き_annotate_news_importance/dedupe_news_items相当の処理
    （_sort_and_strip内部）を通っていることを確認する。"""

    def test_disabled_code_result_still_annotated_and_sorted(self):
        wl = [make_watchlist_item("2282", "日本ハム"), make_watchlist_item("7203", "トヨタ自動車")]
        nqn_item = {"code": "2282", "name": "日本ハム", "title": "日本ハム、通期業績予想を上方修正",
                     "url": "", "source": "NQN", "published": "09/16 09:00", "_ts": 200, "nqnCategory": "120"}
        google_item = {"title": "トヨタ自動車、新型EV発表", "url": "https://example.com/1",
                        "source": "日本経済新聞", "published": "09/16 10:00", "_ts": 100}
        with mock.patch.object(server, "_tachibana_stock_news", return_value=[nqn_item]), \
             mock.patch.object(server, "google_news", return_value=[google_item]):
            items = server.build_stock_name_news(wl)
        # importanceScore等の付与フィールドが存在する（_sort_and_stripを通っている証拠）
        for it in items:
            self.assertIn("importanceScore", it)
            self.assertIn("ts", it)
        # 新しい順（_ts降順）でソートされている
        self.assertEqual(items[0]["code"], "2282")  # _ts=200が先

    def test_disabled_codes_constant_scoped_to_stock_name_news_only(self):
        """STOCK_NAME_NEWS_DISABLED_CODESがbuild_stock_news/build_disclosure_newsの
        コード内で参照されていないこと（影響範囲が登録銘柄一般ニュースのみであることの
        静的確認）。"""
        import inspect
        for fn in (server.build_stock_news, server.build_disclosure_news):
            src = inspect.getsource(fn)
            self.assertNotIn("STOCK_NAME_NEWS_DISABLED_CODES", src)


if __name__ == "__main__":
    unittest.main()
