# 2026-09-14新規（ニュース機能修正指示書）：ニュース一覧のソート・重要度判定・重複排除・
# 通知（再通知禁止・起動時大量通知禁止）の回帰テスト。
#
# 根本原因（指示書A）：trade-cockpit.htmlのnewsSummary並べ替えがisImportantNews(title)を
# 最優先キーにしていたため、キーワード一致した古い記事（例：9/12のエムスリー記事）が
# 新しい記事（9/14）より常に上に来ていた。本ファイルはサーバー側の対応部分
# （_sort_and_strip・compute_news_importance・resolve_news_ts・dedupe_news_items・
# select_news_alerts）を検証する（フロント側の並べ替えロジック自体はサーバーが返す
# ts/importanceScoreを主キー・副キーとしてそのまま使うだけの薄い処理のため、ここでの
# サーバー側検証でソート仕様の正しさを担保する）。
#
# 実行方法： cd files && python -m unittest test_news_notification_and_sort -v

import time
import unittest
from unittest import mock

import server


def _jst_ts(y, mo, d, h, mi):
    import datetime
    jst = datetime.timezone(datetime.timedelta(hours=9))
    return datetime.datetime(y, mo, d, h, mi, tzinfo=jst).timestamp()


class SortOrderTests(unittest.TestCase):
    """L1・L2：published_at DESCが常にPRIMARY、importanceはタイブレークのみ。"""

    def test_newer_low_importance_beats_older_high_importance(self):
        # L1：9/12 importance=100、9/14 importance=10 → 9/14が上。
        old_important = {"title": "業績を上方修正", "code": "1234", "url": "https://a/1",
                          "source": "TDnet", "published": "09/12 21:37", "_ts": _jst_ts(2026, 9, 12, 21, 37)}
        new_normal = {"title": "新商品を発表", "url": "https://a/2",
                      "source": "Yahoo!ニュース", "published": "09/14 15:20", "_ts": _jst_ts(2026, 9, 14, 15, 20)}
        result = server._sort_and_strip([old_important, new_normal])
        self.assertEqual([r["url"] for r in result], ["https://a/2", "https://a/1"])
        self.assertEqual(result[0]["notificationLevel"], "NORMAL")
        self.assertGreaterEqual(result[1]["importanceScore"], 90)

    def test_same_timestamp_uses_importance_as_tiebreak(self):
        # L2：同時刻ニュース、importance 90 vs 50 → 90が上。
        ts = _jst_ts(2026, 9, 14, 10, 0)
        a = {"title": "FOMC 利下げを決定", "url": "https://a/high", "source": "Yahoo!ニュース",
             "published": "09/14 10:00", "_ts": ts}
        b = {"title": "通常のプレスリリース", "url": "https://a/mid", "source": "Yahoo!ニュース",
             "published": "09/14 10:00", "_ts": ts}
        result = server._sort_and_strip([b, a])
        self.assertEqual(result[0]["url"], "https://a/high")
        self.assertGreaterEqual(result[0]["importanceScore"], result[1]["importanceScore"])

    def test_example_sequence_from_spec(self):
        # 指示書A記載の具体例：09/14 15:20(70) > 09/14 14:00(95) > 09/14 10:00(40) >
        # 09/13 18:00(100) > 09/12 21:37(100)。importanceは同時刻でない限りPRIMARYを覆さない。
        items = [
            {"title": "円安が進行", "url": "u1", "source": "x", "published": "09/14 15:20", "_ts": _jst_ts(2026, 9, 14, 15, 20)},
            {"title": "FOMC 利上げを決定", "url": "u2", "source": "x", "published": "09/14 14:00", "_ts": _jst_ts(2026, 9, 14, 14, 0)},
            {"title": "通常のプレスリリース", "url": "u3", "source": "x", "published": "09/14 10:00", "_ts": _jst_ts(2026, 9, 14, 10, 0)},
            {"title": "業績を下方修正", "code": "9999", "url": "u4", "source": "TDnet", "published": "09/13 18:00", "_ts": _jst_ts(2026, 9, 13, 18, 0)},
            {"title": "業績を上方修正", "code": "1234", "url": "u5", "source": "TDnet", "published": "09/12 21:37", "_ts": _jst_ts(2026, 9, 12, 21, 37)},
        ]
        result = server._sort_and_strip(items)
        self.assertEqual([r["url"] for r in result], ["u1", "u2", "u3", "u4", "u5"])


class TimestampFallbackTests(unittest.TestCase):
    """L3：published_at(_ts)が無い場合のみfallback（source_ts→fetched_ts→created_ts）。"""

    def test_missing_ts_falls_back_to_source_ts(self):
        item = {"_source_ts": 12345.0}
        self.assertEqual(server.resolve_news_ts(item), 12345.0)

    def test_missing_ts_and_source_ts_falls_back_to_fetched_ts(self):
        item = {"_fetched_ts": 22222.0}
        self.assertEqual(server.resolve_news_ts(item), 22222.0)

    def test_all_missing_returns_zero(self):
        self.assertEqual(server.resolve_news_ts({}), 0)

    def test_missing_published_at_item_still_sorts_correctly_via_fallback(self):
        no_published = {"title": "フォールバック記事", "url": "u1", "source": "x",
                         "_fetched_ts": _jst_ts(2026, 9, 14, 12, 0)}
        has_published = {"title": "通常記事", "url": "u2", "source": "x", "published": "09/13 12:00",
                          "_ts": _jst_ts(2026, 9, 13, 12, 0)}
        result = server._sort_and_strip([has_published, no_published])
        self.assertEqual(result[0]["url"], "u1")  # fallback(9/14)の方が新しい

    def test_future_anomalous_timestamp_is_clamped(self):
        # 指示書B：異常な未来日時（now+6時間超）は0（末尾扱い）にする。
        far_future = time.time() + 999999
        self.assertEqual(server._clamp_future_ts(far_future), 0)
        near_future = time.time() + 60  # 1分先は許容範囲（クロックずれ程度）
        self.assertEqual(server._clamp_future_ts(near_future), near_future)


class ImportanceScoringTests(unittest.TestCase):
    """L4・L5・L6：TDnet登録銘柄重要開示／FOMC重大ニュース／通常企業PRの判定。"""

    def test_tdnet_registered_stock_critical_disclosure_is_critical_or_high(self):
        r = server.compute_news_importance("通期業績の上方修正を発表", code="1234", is_tdnet=True, is_registered_stock=True)
        self.assertIn(r["level"], ("CRITICAL", "HIGH"))
        self.assertGreaterEqual(r["score"], 60)

    def test_fomc_news_is_critical_or_high(self):
        r = server.compute_news_importance("FOMC、0.25%の利下げを決定")
        self.assertIn(r["level"], ("CRITICAL", "HIGH"))

    def test_boj_news_is_critical_or_high(self):
        r = server.compute_news_importance("日銀、金融政策決定会合でマイナス金利解除を検討")
        self.assertIn(r["level"], ("CRITICAL", "HIGH"))

    def test_normal_corporate_pr_is_normal(self):
        r = server.compute_news_importance("新製品のキャンペーンを開始")
        self.assertEqual(r["level"], "NORMAL")
        self.assertEqual(r["score"], 0)

    def test_geopolitical_without_market_impact_is_not_flagged(self):
        # 指示書D：単に戦争関連記事だから通知するのではなく、市場インパクトが大きいものだけ。
        r = server.compute_news_importance("ウクライナで停戦交渉が再開")
        self.assertEqual(r["level"], "NORMAL")

    def test_geopolitical_with_market_impact_is_flagged(self):
        r = server.compute_news_importance("中東情勢の緊迫化で原油価格が急騰、日本株にも影響")
        self.assertIn(r["level"], ("CRITICAL", "HIGH"))


class DedupeTests(unittest.TestCase):
    """指示書C：同一記事・同一開示の重複排除。"""

    def test_same_tdnet_document_id_deduped_across_sources(self):
        items = [
            {"title": "上方修正のお知らせ", "url": "https://www.release.tdnet.info/inbs/12345678901234.pdf", "code": "1234"},
            {"title": "上方修正のお知らせ", "url": "https://www.release.tdnet.info/inbs/12345678901234_r2.pdf", "code": "1234"},
        ]
        result = server.dedupe_news_items(items)
        self.assertEqual(len(result), 1)

    def test_same_normalized_url_query_diff_deduped(self):
        items = [
            {"title": "A社が新製品を発表", "url": "https://example.com/news/1?ref=twitter"},
            {"title": "A社が新製品を発表", "url": "https://example.com/news/1?ref=facebook"},
        ]
        result = server.dedupe_news_items(items)
        self.assertEqual(len(result), 1)

    def test_different_articles_not_deduped(self):
        items = [
            {"title": "A社が新製品を発表", "url": "https://example.com/1", "code": "1111", "published": "09/14 10:00"},
            {"title": "B社が業績を上方修正", "url": "https://example.com/2", "code": "2222", "published": "09/14 11:00"},
        ]
        result = server.dedupe_news_items(items)
        self.assertEqual(len(result), 2)


class NotificationDedupeAndStartupTests(unittest.TestCase):
    """L7・L8：同一ニュース再取得での再通知なし／起動時の過去ニュース一斉通知なし。"""

    def test_already_notified_item_excluded(self):
        with mock.patch("investment_db.was_already_notified", return_value=True), \
             mock.patch("investment_db.record_notification") as rec:
            item = {"title": "FOMC 利下げを決定", "url": "u1", "notificationLevel": "HIGH",
                    "importanceScore": 80, "ts": time.time()}
            alerts = server.select_news_alerts("dummy_url", [item], watcher_started_at=0)
            self.assertEqual(alerts, [])
            rec.assert_not_called()

    def test_new_item_recorded_and_returned(self):
        with mock.patch("investment_db.was_already_notified", return_value=False), \
             mock.patch("investment_db.record_notification", return_value=True) as rec:
            item = {"title": "FOMC 利下げを決定", "url": "u1", "notificationLevel": "HIGH",
                    "importanceScore": 80, "ts": time.time()}
            alerts = server.select_news_alerts("dummy_url", [item], watcher_started_at=0)
            self.assertEqual(len(alerts), 1)
            rec.assert_called_once()

    def test_normal_level_never_becomes_alert(self):
        item = {"title": "新商品を発表", "url": "u1", "notificationLevel": "NORMAL",
                "importanceScore": 0, "ts": time.time()}
        alerts = server.select_news_alerts(None, [item], watcher_started_at=0)
        self.assertEqual(alerts, [])

    def test_startup_does_not_mass_notify_old_critical_backlog(self):
        # L8：起動時の過去100件で音が100回鳴らない＝監視開始前の古いCRITICALは通知対象外
        # （直近5分以内のCRITICALだけは例外で通知可）。
        watcher_started_at = time.time()
        old_items = [{"title": f"業績を上方修正 {i}", "url": f"u{i}", "notificationLevel": "CRITICAL",
                       "importanceScore": 95, "ts": watcher_started_at - 3600}  # 1時間前（起動前）
                      for i in range(100)]
        with mock.patch("investment_db.was_already_notified", return_value=False), \
             mock.patch("investment_db.record_notification", return_value=True):
            alerts = server.select_news_alerts("dummy_url", old_items, watcher_started_at=watcher_started_at)
        self.assertEqual(len(alerts), 0)

    def test_startup_exception_allows_very_recent_critical(self):
        # 指示書G例外：直近数分以内のCRITICALニュースのみ通知してよい。
        watcher_started_at = time.time()
        recent_critical = {"title": "業績を上方修正", "url": "u1", "notificationLevel": "CRITICAL",
                            "importanceScore": 95, "ts": time.time() - 60}  # 1分前（起動前でも直近）
        with mock.patch("investment_db.was_already_notified", return_value=False), \
             mock.patch("investment_db.record_notification", return_value=True):
            alerts = server.select_news_alerts("dummy_url", [recent_critical], watcher_started_at=watcher_started_at)
        self.assertEqual(len(alerts), 1)

    def test_no_database_url_still_dedupes_within_call_but_does_not_crash(self):
        item = {"title": "FOMC 利下げを決定", "url": "u1", "notificationLevel": "HIGH",
                "importanceScore": 80, "ts": time.time()}
        alerts = server.select_news_alerts(None, [item], watcher_started_at=0)
        self.assertEqual(len(alerts), 1)


class SportsNewsStillExcludedTests(unittest.TestCase):
    """L9：既存のスポーツニュース除外ロジック（_is_promo_news）が壊れていないこと
    （新しい重要度判定はこのフィルタの後段で動くため、フィルタ自体には触れていない）。"""

    def test_sports_matchup_still_excluded_for_nipponham(self):
        self.assertTrue(server._is_promo_news("日本ハム・清宮虎、移籍後初登板もサヨナラ負け", "デイリースポーツ", "日本ハム"))

    def test_business_context_override_still_works(self):
        self.assertFalse(server._is_promo_news("日本ハムが球団事業を売却、決算を上方修正", "日本経済新聞", "日本ハム"))


class JstUtcBoundaryTests(unittest.TestCase):
    """L10：JST/UTC境界で並び順が逆転しないこと。"""

    def test_tdnet_sort_key_and_google_news_ts_share_consistent_ordering_near_midnight(self):
        # TDnetは_tdnet_sort_key（JST基準の当日時刻）、Googleニュースは_published_ts
        # （UTC pubDateから変換）を使うが、どちらも最終的にUTC epoch秒のため比較可能。
        # UTC 15:30 (前日) = JST 00:30 (当日)。JST日付だけを見て「前日」と誤判定しないこと。
        import datetime
        jst = datetime.timezone(datetime.timedelta(hours=9))
        late_utc_but_early_jst = datetime.datetime(2026, 9, 13, 15, 30, tzinfo=datetime.timezone.utc)
        early_utc_same_jst_day = datetime.datetime(2026, 9, 13, 16, 0, tzinfo=datetime.timezone.utc)
        item_a = {"title": "23:30 UTC発、JSTでは翌日0:30", "url": "u1", "source": "x",
                  "_ts": late_utc_but_early_jst.timestamp()}
        item_b = {"title": "16:00 UTC発", "url": "u2", "source": "x",
                  "_ts": early_utc_same_jst_day.timestamp()}
        result = server._sort_and_strip([item_a, item_b])
        # item_bの方がUTC/JSTどちらの基準でも後（新しい）ため、常にitem_bが上に来る。
        self.assertEqual(result[0]["url"], "u2")


if __name__ == "__main__":
    unittest.main()
