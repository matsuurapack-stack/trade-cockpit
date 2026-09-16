# 緊急修正：4銘柄の一般ニュース除外が実画面で効いていない（2026-09-17）の回帰テスト。
#
# STEP1原因調査で判明：build_stock_name_news()はGoogleニュース検索だけを除外していたが、
# NQN（立花証券API速報）はカテゴリ（100=一般ニュース／120・129=AI開示速報）を無視して
# 無条件に残していたため、カテゴリ100（スポーツ・地域記事等を含みうる）が漏れていた。
#
# 本ファイルは、複数source→merge→dedupe→filter→最終API payloadまで通した
# integration testを中心に、STEP2で追加した最終payload安全弁
# （_filter_general_news_for_disabled_codes）を検証する。
#
# 実行方法： cd files && python -m unittest test_general_news_final_safety_net -v

import unittest
from unittest import mock

import server


def make_watchlist_item(code, name, watch="優先", market="JP"):
    return {"code": code, "name": name, "watch": watch, "market": market}


class IsOfficialDisclosureNewsItemTests(unittest.TestCase):
    """_is_official_disclosure_news_item()：構造化情報（source/sourceType/nqnCategory）
    だけで判定し、タイトルの文字列一致に依存しないこと（指示書STEP3・4）。"""

    def test_tdnet_source_is_official(self):
        self.assertTrue(server._is_official_disclosure_news_item({"source": "TDnet", "title": "何でもいい"}))

    def test_source_type_primary_disclosure_is_official(self):
        self.assertTrue(server._is_official_disclosure_news_item({"sourceType": "PRIMARY_DISCLOSURE"}))

    def test_source_type_primary_ir_is_official(self):
        self.assertTrue(server._is_official_disclosure_news_item({"sourceType": "PRIMARY_IR"}))

    def test_nqn_category_120_without_source_type_is_official_fallback(self):
        self.assertTrue(server._is_official_disclosure_news_item(
            {"source": "NQN", "nqnCategory": "120", "sourceType": None}))

    def test_nqn_category_100_is_not_official(self):
        self.assertFalse(server._is_official_disclosure_news_item(
            {"source": "NQN", "nqnCategory": "100", "sourceType": None}))

    def test_google_news_source_is_not_official_even_with_ir_like_title(self):
        """指示書STEP3「タイトルに決算という文字があるから残す、のような曖昧な救済はしない」。"""
        self.assertFalse(server._is_official_disclosure_news_item(
            {"source": "日本経済新聞", "sourceType": "MARKET_MEDIA",
             "title": "決算発表を控え投資家が注目"}))

    def test_ir_word_in_title_alone_does_not_make_it_official(self):
        self.assertFalse(server._is_official_disclosure_news_item(
            {"source": "千葉テレビ放送株式会社", "sourceType": "AGGREGATOR",
             "title": "BE:FIRST、味の素スタジアム公演のダイジェスト映像公開"}))


class FilterGeneralNewsForDisabledCodesTests(unittest.TestCase):
    def test_disabled_code_general_item_is_stripped(self):
        items = [{"code": "2282", "source": "千葉テレビ放送株式会社", "sourceType": "AGGREGATOR",
                   "title": "日本ハムに激震…レイエスが『右ハムストリングス肉離れ』"}]
        self.assertEqual(server._filter_general_news_for_disabled_codes(items), [])

    def test_disabled_code_tdnet_item_survives(self):
        items = [{"code": "1812", "source": "TDnet", "title": "配当予想の修正に関するお知らせ"}]
        result = server._filter_general_news_for_disabled_codes(items)
        self.assertEqual(len(result), 1)

    def test_disabled_code_nqn_disclosure_item_survives(self):
        items = [{"code": "2802", "source": "NQN", "sourceType": "PRIMARY_IR", "nqnCategory": "120",
                   "title": "味の素、上方修正"}]
        result = server._filter_general_news_for_disabled_codes(items)
        self.assertEqual(len(result), 1)

    def test_non_disabled_code_general_item_survives(self):
        items = [{"code": "7203", "source": "日本経済新聞", "sourceType": "MARKET_MEDIA",
                   "title": "トヨタ自動車、新型EV発表"}]
        result = server._filter_general_news_for_disabled_codes(items)
        self.assertEqual(len(result), 1)

    def test_mixed_list_only_disabled_general_items_removed(self):
        items = [
            {"code": "2282", "source": "NQN", "sourceType": "AGGREGATOR", "nqnCategory": "100",
             "title": "日本ハム、期待外れのドラ1戦士"},   # 除外される
            {"code": "1812", "source": "TDnet", "title": "独占禁止法違反事件における決定について"},  # 残る
            {"code": "7203", "source": "日本経済新聞", "sourceType": "MARKET_MEDIA",
             "title": "トヨタ自動車、新型EV発表"},          # 残る（非対象銘柄）
            {"code": "2801", "source": "NQN", "sourceType": "PRIMARY_IR", "nqnCategory": "129",
             "title": "キッコーマン、自己株式取得"},          # 残る（開示速報）
        ]
        result = server._filter_general_news_for_disabled_codes(items)
        result_codes = [it["code"] for it in result]
        self.assertNotIn("2282", result_codes)
        self.assertIn("1812", result_codes)
        self.assertIn("7203", result_codes)
        self.assertIn("2801", result_codes)
        self.assertEqual(len(result), 3)


class MultiSourceMergeDedupeFilterIntegrationTests(unittest.TestCase):
    """指示書STEP7の核心：複数source→merge→dedupe→filter→最終API payloadまで通した
    integration test。一般ニュースを別経路（NQNカテゴリ100を模したfixture）から意図的に
    混入させても、最終的にbuild_stock_name_news()＋_filter_general_news_for_disabled_codes()
    を通した後は4銘柄が除外されることを確認する。"""

    def _run_pipeline(self, watchlist, tachibana_items, google_items_by_name=None):
        google_items_by_name = google_items_by_name or {}

        def fake_google_news(query, n=4, max_age_days=None):
            for name, items in google_items_by_name.items():
                if query.startswith(name):
                    return items
            return []

        with mock.patch.object(server, "_tachibana_stock_news", return_value=tachibana_items), \
             mock.patch.object(server, "google_news", side_effect=fake_google_news):
            stock_name_news = server.build_stock_name_news(watchlist)
        # /api/newsハンドラと同じ最終安全弁を適用する
        return server._filter_general_news_for_disabled_codes(stock_name_news)

    def test_2282_nqn_general_news_excluded_end_to_end(self):
        wl = [make_watchlist_item("2282", "日本ハム")]
        tachibana_items = [
            {"code": "2282", "name": "日本ハム", "title": "“超逸材”がまさか…日本ハム、期待外れのドラ1戦士",
             "url": "", "source": "NQN", "published": "09/16 09:00", "_ts": 1, "nqnCategory": "100"},
        ]
        result = self._run_pipeline(wl, tachibana_items)
        self.assertEqual(result, [])

    def test_2282_plus_tdnet_stays(self):
        """2282 + TDnet → 残る。"""
        wl = [make_watchlist_item("2282", "日本ハム")]
        tachibana_items = [
            {"code": "2282", "name": "日本ハム", "title": "レイエスが『右ハムストリングス肉離れ』",
             "url": "", "source": "NQN", "published": "09/16 09:00", "_ts": 1, "nqnCategory": "100"},
        ]
        stock_name_result = self._run_pipeline(wl, tachibana_items)
        self.assertEqual(stock_name_result, [])  # 一般ニュース側は0件

        # TDnetは別経路（build_stock_news）で独立して残る
        with mock.patch.object(server, "_tdnet_today_disclosures",
                                return_value={"2282": [{"title": "日本ハム 業績予想の上方修正に関するお知らせ",
                                                          "url": "https://tdnet.example/1", "time": "15:00"}]}):
            tdnet_result = server.build_stock_news(wl)
        self.assertEqual(len(tdnet_result), 1)
        self.assertEqual(tdnet_result[0]["source"], "TDnet")

    def test_1812_plus_official_ir_stays(self):
        """1812 + official IR（NQN開示速報）→ 残る。"""
        wl = [make_watchlist_item("1812", "鹿島")]
        tachibana_items = [
            {"code": "1812", "name": "鹿島", "title": "鹿島、大型受注に関するお知らせ",
             "url": "", "source": "NQN", "published": "09/16 09:00", "_ts": 1, "nqnCategory": "120"},
        ]
        result = self._run_pipeline(wl, tachibana_items)
        self.assertEqual(len(result), 1)
        self.assertIn("大型受注", result[0]["title"])

    def test_2801_plus_company_general_article_removed(self):
        """2801 + COMPANY一般記事 → 消える（Google Newsが混入しても最終安全弁で除去）。"""
        wl = [make_watchlist_item("2801", "キッコーマン")]
        # STOCK_NAME_NEWS_DISABLED_CODESの入口フィルタが正しく働かなかった想定で、
        # 最終安全弁側の効果だけを検証するため、build_stock_name_news()の結果へ
        # 直接一般記事を混ぜてから安全弁を通す。
        leaked_general_item = {"code": "2801", "name": "キッコーマン",
                                 "title": "『キッコーマン総合病院』ってなんなんだ!?",
                                 "url": "https://example.com/x", "source": "デイリーポータルZ",
                                 "sourceType": "AGGREGATOR", "published": "09/16 09:00", "ts": 1}
        result = server._filter_general_news_for_disabled_codes([leaked_general_item])
        self.assertEqual(result, [])

    def test_2802_ir_misclassified_general_article_removed(self):
        """2802 + IR誤分類一般記事 → 消える。タイトルに"IR"に見える文字列（英語表記"FIRST"等）が
        含まれても、source/sourceTypeが構造的にIRでなければ安全弁で除去されることを確認
        （指示書STEP4：フロント側classifyNewsCategoryの誤爆と同種の入力を、サーバー側
        安全弁が正しく弾けることの確認）。"""
        wl = [make_watchlist_item("2802", "味の素")]
        tachibana_items = [
            {"code": "2802", "name": "味の素",
             "title": "BE:FIRST、グループ史上最大規模となった味の素スタジアム公演のダイジェスト映像公開",
             "url": "", "source": "NQN", "published": "09/16 09:00", "_ts": 1, "nqnCategory": "100"},
        ]
        result = self._run_pipeline(wl, tachibana_items)
        self.assertEqual(result, [])

    def test_non_disabled_code_end_to_end_unaffected(self):
        wl = [make_watchlist_item("7203", "トヨタ自動車")]
        tachibana_items = []
        google_items = {"トヨタ自動車": [{"title": "トヨタ自動車、新型EV発表", "url": "https://example.com/1",
                                            "source": "日本経済新聞", "published": "09/16 10:00", "_ts": 100}]}
        result = self._run_pipeline(wl, tachibana_items, google_items)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["code"], "7203")


class DedupeStillWorksAfterFilterTests(unittest.TestCase):
    """指示書STEP7「既存ニュース重要度・通知・dedupeを壊さない」：安全弁適用後も
    importanceScore等の既存アノテーションフィールドが保持されていること。"""

    def test_annotated_fields_survive_final_filter(self):
        items = [
            {"code": "7203", "source": "日本経済新聞", "sourceType": "MARKET_MEDIA",
             "title": "トヨタ自動車、新型EV発表", "importanceScore": 10, "ts": 100},
            {"code": "2282", "source": "NQN", "sourceType": "AGGREGATOR", "nqnCategory": "100",
             "title": "日本ハム、期待外れのドラ1戦士", "importanceScore": 0, "ts": 1},
        ]
        result = server._filter_general_news_for_disabled_codes(items)
        self.assertEqual(len(result), 1)
        self.assertIn("importanceScore", result[0])
        self.assertEqual(result[0]["code"], "7203")


if __name__ == "__main__":
    unittest.main()
