# News Intelligence Phase 2（2026-09-14新規）：国内市場ニュース「日経中心化」＋分析エンジン
# 連携の回帰テスト。Phase1（新着順ソート・importance・dedupe・notification_log・5分アラート）
# は無変更であることを前提に、差分追加部分（source_tier／event_key／related_sectors・themes
# ／sector_news_score／market_news_context／ENTRY TOP5への注記）を検証する。
#
# 実行方法： cd files && python -m unittest test_news_intelligence_phase2 -v

import time
import unittest
from unittest import mock

import server


def _ts(offset_sec=0):
    return time.time() + offset_sec


class SourcePriorityVsSortTests(unittest.TestCase):
    """最重要原則（指示書2）：SOURCE PRIORITYはSORTに混ぜない。1. 古い日経HIGHより新しい
    YahooのNORMALが上に来ること。"""

    def test_newer_yahoo_normal_beats_older_nikkei_high(self):
        old_nikkei_high = {"title": "日銀、金融政策決定会合で利上げを検討", "url": "u1",
                            "source": "日本経済新聞", "published": "09/14 09:00", "_ts": _ts(-3600)}
        new_yahoo_normal = {"title": "新商品を発表", "url": "u2", "source": "Yahoo!ニュース",
                             "published": "09/14 10:00", "_ts": _ts(-1800)}
        result = server._sort_and_strip([old_nikkei_high, new_yahoo_normal])
        self.assertEqual(result[0]["url"], "u2")
        self.assertEqual(result[1]["sourceType"], "MARKET_MEDIA")


class EventLinkingTests(unittest.TestCase):
    """2. TDnetと日経が同一企業イベント → 完全重複としてdedupeせず、
    FACT / MARKET_REACTIONとしてevent_keyで関連付け。"""

    def test_tdnet_fact_and_nikkei_reaction_both_kept_and_linked(self):
        tdnet = {"title": "通期業績予想の上方修正に関するお知らせ", "code": "1234", "url": "u1",
                 "source": "TDnet", "published": "09/14 14:00", "_ts": _ts(-1200)}
        nikkei = {"title": "○○社株が急伸、上方修正を好感", "code": "1234", "url": "u2",
                  "source": "日本経済新聞", "published": "09/14 14:20", "_ts": _ts(-600)}
        result = server._sort_and_strip([nikkei, tdnet])
        self.assertEqual(len(result), 2)  # 重複排除で消えていない
        by_url = {r["url"]: r for r in result}
        self.assertEqual(by_url["u1"]["eventKey"], by_url["u2"]["eventKey"])
        self.assertEqual(by_url["u1"]["eventRole"], "FACT")
        self.assertEqual(by_url["u2"]["eventRole"], "MARKET_REACTION")


class NikkeiDuplicateTests(unittest.TestCase):
    """3. 同一日経記事の重複取得 → dedupe。"""

    def test_same_nikkei_article_from_two_queries_deduped(self):
        a = {"title": "日経平均、続落し1カ月半ぶり安値", "url": "https://nikkei.com/article/123?ref=a", "source": "日本経済新聞"}
        b = {"title": "日経平均、続落し1カ月半ぶり安値", "url": "https://nikkei.com/article/123?ref=b", "source": "日本経済新聞"}
        result = server.dedupe_news_items([a, b])
        self.assertEqual(len(result), 1)


class WatchlistRelatedTests(unittest.TestCase):
    """4. 登録銘柄の日経重大記事 → watchlist_related=true。"""

    def test_registered_stock_nikkei_article_flagged_watchlist_related(self):
        item = {"title": "○○社、業績予想を上方修正", "code": "1928", "url": "u1", "source": "日本経済新聞"}
        result = server._sort_and_strip([item])
        self.assertTrue(result[0]["watchlistRelated"])
        self.assertEqual(result[0]["relatedStockCodes"], ["1928"])


class SourceAloneDoesNotElevateTests(unittest.TestCase):
    """5. 通常の日経記事 → 日経という理由だけでHIGHにならない。"""

    def test_ordinary_nikkei_article_stays_normal(self):
        r = server.compute_news_importance("値上げの春、家計に影響広がる", source_tier=2)
        self.assertEqual(r["level"], "NORMAL")
        self.assertEqual(r["score"], 0)


class BojPolicyNikkeiFlashTests(unittest.TestCase):
    """6. 日銀政策変更の日経速報 → HIGH/CRITICAL候補。"""

    def test_boj_policy_change_nikkei_flash_is_high_or_critical(self):
        r = server.compute_news_importance("日銀、金融政策決定会合でマイナス金利解除を決定", source_tier=2)
        self.assertIn(r["level"], ("CRITICAL", "HIGH"))


class GeopoliticalNotificationGateTests(unittest.TestCase):
    """7. 単なる戦況記事 → 通知なし（一覧表示はされてもよいが重要度は上がらない）。
    8. 戦争＋原油供給ショック → HIGH/CRITICAL候補。"""

    def test_plain_war_report_not_flagged(self):
        r = server.compute_news_importance("現地で激しい戦闘が継続", source_tier=2)
        self.assertEqual(r["level"], "NORMAL")

    def test_war_plus_oil_supply_shock_is_high_or_critical(self):
        r = server.compute_news_importance("ホルムズ海峡で緊張、原油供給に懸念", source_tier=3)
        self.assertIn(r["level"], ("CRITICAL", "HIGH"))

    def test_plain_war_report_not_selected_as_alert(self):
        item = {"title": "現地で激しい戦闘が継続", "url": "u1", "notificationLevel": "NORMAL",
                "importanceScore": 0, "ts": time.time()}
        alerts = server.select_news_alerts(None, [item], watcher_started_at=0)
        self.assertEqual(alerts, [])


class SectorRotationNewsScoreTests(unittest.TestCase):
    """9. 日経のSaaS好材料＋価格/RS弱い → Sector Rotation／ENTRY TOP5をニュースだけで
    強制昇格させない（sector_news_scoreは既存score/stateの計算式に混ざらない補助情報）。"""

    def test_sector_news_score_does_not_affect_classify_sector_state(self):
        # classify_sector_state()はprice由来のscoreだけを見る関数のままであること
        # （sector_news_scoreという引数自体を受け取らない＝混入していないことの構造的確認）。
        import inspect
        sig = inspect.signature(server.classify_sector_state)
        self.assertEqual(list(sig.parameters.keys()), ["score"])

    def test_sector_news_score_computed_from_related_sectors_only(self):
        saas_news = {"title": "SaaS株に買い、米金利低下でグロース選好", "relatedSectors": ["SaaS"],
                     "importanceScore": 50}
        other_news = {"title": "銀行株が軟調", "relatedSectors": ["銀行"], "importanceScore": 80}
        score = server.compute_sector_news_score("SaaS", [saas_news, other_news])
        self.assertGreater(score, 0)
        zero_score = server.compute_sector_news_score("海運", [saas_news, other_news])
        self.assertEqual(zero_score, 0)

    def test_sector_news_score_none_when_no_cache(self):
        self.assertIsNone(server.compute_sector_news_score("SaaS", []))


class EntryTop5NewsCatalystAnnotationTests(unittest.TestCase):
    """10. 日経材料＋価格＋出来高＋RSが一致 → candidate/news catalystへ反映
    （ただし既存のENTRY SCORE自体は書き換えない、参考情報の付加のみ）。"""

    def test_attach_news_catalyst_flags_only_annotates_matching_codes(self):
        news_items = [
            {"code": "4478", "title": "SaaS株に買い、米金利低下でグロース選好",
             "relatedThemes": ["SaaS"], "importanceScore": 60},
            {"code": "9999", "title": "無関係銘柄のニュース", "importanceScore": 90},
        ]
        result = server.attach_news_catalyst_flags({"4478", "1111"}, news_items)
        self.assertIn("4478", result)
        self.assertTrue(result["4478"]["matched"])
        self.assertNotIn("9999", result)   # candidate_codesに無いためスキップ
        self.assertNotIn("1111", result)   # 該当ニュースが無い

    def test_entry_top5_scoring_function_signature_unchanged(self):
        # _score_entry_candidatesのシグネチャにnews関連引数が増えていないこと
        # （ニュース注記は呼び出し元での後付けであり、スコア関数自体は無改変）。
        import inspect
        sig = inspect.signature(server._score_entry_candidates)
        self.assertEqual(list(sig.parameters.keys()), ["database_url", "user_id"])


class MarketNewsContextTests(unittest.TestCase):
    """指示書12：market_news_contextは構造化情報のみ（記事本文の長文保存はしない）。"""

    def test_market_news_context_uses_top_importance_item(self):
        items = [
            {"title": "通常のプレスリリース", "importanceScore": 0, "_ts": _ts(-100), "sourceLabel": "その他"},
            {"title": "米金利低下を受けグロース優位", "importanceScore": 80, "_ts": _ts(-50),
             "sourceLabel": "日経", "relatedSectors": ["SaaS", "半導体"], "published": "09/14 12:00", "url": "u1"},
        ]
        ctx = server.build_market_news_context(items)
        self.assertEqual(ctx["main_driver"], "米金利低下を受けグロース優位")
        self.assertEqual(ctx["source"], "日経")
        self.assertEqual(ctx["affected_sectors"], ["SaaS", "半導体"])
        self.assertAlmostEqual(ctx["confidence"], 0.8)

    def test_market_news_context_none_when_no_important_news(self):
        items = [{"title": "通常のプレスリリース", "importanceScore": 0}]
        self.assertIsNone(server.build_market_news_context(items))

    def test_market_news_context_none_when_empty(self):
        self.assertIsNone(server.build_market_news_context([]))


class NotificationLogUnaffectedTests(unittest.TestCase):
    """11. notification_log → 従来通り再通知なし（Phase2のsource_tier等追加が
    select_news_alertsの再通知禁止ロジックを壊していないこと）。"""

    def test_already_notified_item_still_excluded_with_new_fields(self):
        with mock.patch("investment_db.was_already_notified", return_value=True), \
             mock.patch("investment_db.record_notification") as rec:
            item = {"title": "日銀、利上げを決定", "url": "u1", "notificationLevel": "HIGH",
                    "importanceScore": 83, "ts": time.time(), "sourceTier": 2, "sourceType": "MARKET_MEDIA"}
            alerts = server.select_news_alerts("dummy_url", [item], watcher_started_at=0)
            self.assertEqual(alerts, [])
            rec.assert_not_called()


class SportsNewsRegressionTests(unittest.TestCase):
    """12. スポーツニュース除外 → 従来通りPASS（Phase2のsource_tier/related_tags追加が
    _is_promo_newsの判定より前段に影響していないこと）。"""

    def test_sports_matchup_still_excluded(self):
        self.assertTrue(server._is_promo_news("日本ハム・清宮虎、移籍後初登板もサヨナラ負け", "デイリースポーツ", "日本ハム"))

    def test_sports_news_gets_normal_importance_even_if_not_filtered_upstream(self):
        # 万一フィルタをすり抜けても、重要度側でCRITICAL/HIGHへ誤って引き上げられないこと。
        r = server.compute_news_importance("日本ハム・清宮虎、移籍後初登板もサヨナラ負け", source_tier=4)
        self.assertEqual(r["level"], "NORMAL")


if __name__ == "__main__":
    unittest.main()
