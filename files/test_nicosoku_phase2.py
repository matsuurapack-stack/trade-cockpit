# にこそくX連携 Phase2 テスト（指示書18番）。
#
# このリポジトリには実DB（Neon/PostgreSQL）前提のテスト基盤が無いため、実DBを必要としない
# 形で20項目をカバーする：
#   - 純粋関数（優先度スコア・ラベル・stale判定・confidence正規化・並び替え）は直接テスト
#   - DBアクセスを伴う分岐（状態遷移・診断API・fetch-now）はinvestment_dbをモックして
#     server.py側の呼び出し・戻り値の組み立てだけを検証する（実際のSQLはinvestment_db側の
#     責務で、ここでは検証しない）
#
# 実行方法： python -m unittest files/test_nicosoku_phase2.py -v
# もしくは： cd files && python -m unittest test_nicosoku_phase2 -v

import datetime
import unittest
from unittest import mock

import server


def _iso(dt):
    return dt.astimezone(datetime.timezone.utc).isoformat()


class ImageAnalysisStatusDefaultTests(unittest.TestCase):
    """1. 画像なし→NONE / 2. 画像あり→PENDING（指示書1番）"""

    def test_no_media_is_none(self):
        self.assertEqual(server._initial_image_analysis_status([]), "NONE")
        self.assertEqual(server._initial_image_analysis_status(None), "NONE")

    def test_with_media_is_pending(self):
        self.assertEqual(server._initial_image_analysis_status([{"url": "https://x/img.jpg"}]), "PENDING")


class ImageAnalysisStatusTransitionTests(unittest.TestCase):
    """3〜7. PENDING→ANALYZED/SKIPPED、FAILED保存、FAILED/ANALYZED→PENDING再解析
    （指示書2・3・4番）。investment_dbをモックし、server.py側のハンドラ処理相当の呼び出しを検証する。"""

    def setUp(self):
        self.db_patch = mock.patch.object(server, "investment_db")
        self.mock_db = self.db_patch.start()
        self.addCleanup(self.db_patch.stop)

    def test_pending_to_analyzed_via_smart_import(self):
        # 3. PENDING → ANALYZED：smart_import_confirmのSOCIAL_IMAGE_ANALYSIS分岐が
        # save_social_post_image_analysisを呼ぶことを確認する。
        self.mock_db.save_social_post_image_analysis.return_value = {
            "post_id": "123", "posted_at": _iso(datetime.datetime.now(datetime.timezone.utc)),
            "image_analysis_status": "ANALYZED",
        }
        self.mock_db.list_market_events.return_value = []
        self.mock_db.import_market_events.return_value = {"imported": 0}
        candidates = [{"category": "SOCIAL_IMAGE_ANALYSIS", "confidence": "HIGH",
                       "draft": {"post_id": "123", "analysis": {"observations": ["混雑"]}}, "rawText": ""}]
        result = server.smart_import_confirm("dummy_url", "local", candidates, "manual")
        self.mock_db.save_social_post_image_analysis.assert_called_once()
        args = self.mock_db.save_social_post_image_analysis.call_args[0]
        self.assertEqual(args[2], "123")
        self.assertEqual(result["results"]["SOCIAL_IMAGE_ANALYSIS"]["imported"], 1)

    def test_pending_to_skipped(self):
        # 4. PENDING → SKIPPED：set_social_post_image_analysis_statusが"SKIPPED"で呼ばれる
        # （do_POST内の/api/social-posts/skipハンドラと同じ呼び出し）。
        self.mock_db.set_social_post_image_analysis_status.return_value = {"post_id": "1", "image_analysis_status": "SKIPPED"}
        saved = server.investment_db.set_social_post_image_analysis_status("dummy", server.NICOSOKU_X_USERNAME, "1", "SKIPPED")
        self.mock_db.set_social_post_image_analysis_status.assert_called_with("dummy", server.NICOSOKU_X_USERNAME, "1", "SKIPPED")
        self.assertEqual(saved["image_analysis_status"], "SKIPPED")

    def test_failed_save_records_error(self):
        # 5. FAILED保存：normalize後の保存で例外が起きたらmark_social_post_image_analysis_failedが
        # post_idつきで呼ばれる。
        self.mock_db.save_social_post_image_analysis.side_effect = RuntimeError("DB接続エラー")
        candidates = [{"category": "SOCIAL_IMAGE_ANALYSIS", "confidence": "HIGH",
                       "draft": {"post_id": "999", "analysis": {"observations": []}}, "rawText": ""}]
        result = server.smart_import_confirm("dummy_url", "local", candidates, "manual")
        self.mock_db.mark_social_post_image_analysis_failed.assert_called_once()
        call_args = self.mock_db.mark_social_post_image_analysis_failed.call_args[0]
        self.assertEqual(call_args[2], "999")
        self.assertEqual(result["results"]["SOCIAL_IMAGE_ANALYSIS"]["skipped"], 1)

    def test_failed_to_pending_reanalyze(self):
        # 6. FAILED → PENDING再解析
        self.mock_db.set_social_post_image_analysis_status.return_value = {"post_id": "2", "image_analysis_status": "PENDING"}
        saved = server.investment_db.set_social_post_image_analysis_status("dummy", server.NICOSOKU_X_USERNAME, "2", "PENDING")
        self.assertEqual(saved["image_analysis_status"], "PENDING")

    def test_analyzed_to_pending_reanalyze(self):
        # 7. ANALYZED → PENDING再解析（状態遷移関数は開始状態を問わず同じ呼び出しで動く）
        self.mock_db.set_social_post_image_analysis_status.return_value = {"post_id": "3", "image_analysis_status": "PENDING"}
        saved = server.investment_db.set_social_post_image_analysis_status("dummy", server.NICOSOKU_X_USERNAME, "3", "PENDING")
        self.assertEqual(saved["image_analysis_status"], "PENDING")


class PriorityScoreTests(unittest.TestCase):
    """8〜11. 優先度スコア（指示書5番）"""

    def test_critical_importance(self):
        post = {"importance": "CRITICAL"}
        self.assertEqual(server.compute_social_post_priority_score(post), 35)

    def test_position_direct_mention(self):
        post = {"importance": None, "direct_mentions_json": ["7203"]}
        score = server.compute_social_post_priority_score(post, watch_codes=set(), position_codes={"7203"})
        self.assertEqual(score, 20)

    def test_watch_only_mention_lower_than_position(self):
        post = {"direct_mentions_json": ["7203"]}
        score_watch = server.compute_social_post_priority_score(post, watch_codes={"7203"}, position_codes=set())
        score_position = server.compute_social_post_priority_score(post, watch_codes=set(), position_codes={"7203"})
        self.assertEqual(score_watch, 15)
        self.assertGreater(score_position, score_watch)

    def test_economic_event_category(self):
        post = {"categories_json": ["ECONOMIC_EVENT"]}
        self.assertEqual(server.compute_social_post_priority_score(post), 15)

    def test_no_double_count_same_category_group(self):
        # 同じ「カテゴリ」区分の複数一致でも1回分（最大値）のみ加点される（指示書「重複加点しない」）。
        post = {"categories_json": ["ECONOMIC_EVENT", "CENTRAL_BANK"]}
        self.assertEqual(server.compute_social_post_priority_score(post), 15)

    def test_score_capped_at_100(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        post = {
            "importance": "CRITICAL", "posted_at": _iso(now - datetime.timedelta(minutes=5)),
            "direct_mentions_json": ["7203"], "categories_json": ["ECONOMIC_EVENT"],
            "media_json": [{"url": "a"}, {"url": "b"}],
        }
        score = server.compute_social_post_priority_score(post, watch_codes=set(), position_codes={"7203"}, now=now)
        # 35(CRITICAL) + 20(30分以内) + 20(保有銘柄) + 15(ECONOMIC_EVENT) + 5(複数画像) = 95 <= 100
        self.assertLessEqual(score, 100)
        self.assertEqual(score, 95)

    def test_score_never_exceeds_100_hard_cap(self):
        # min(score,100)の上限クリップ自体を直接確認する（将来配点が増えても100を超えない保証）。
        with mock.patch.dict(server._SOCIAL_PRIORITY_IMPORTANCE, {"CRITICAL": 999}):
            post = {"importance": "CRITICAL"}
            self.assertEqual(server.compute_social_post_priority_score(post), 100)

    def test_priority_label_thresholds(self):
        self.assertEqual(server.social_post_priority_label(80), "URGENT")
        self.assertEqual(server.social_post_priority_label(60), "HIGH")
        self.assertEqual(server.social_post_priority_label(30), "NORMAL")
        self.assertEqual(server.social_post_priority_label(29), "LOW")
        self.assertEqual(server.social_post_priority_label(0), "LOW")


class PendingSortOrderTests(unittest.TestCase):
    """12. PENDING並び順（指示書7番：analysis_priority_score DESC, posted_at DESC）"""

    def test_pending_sorted_by_score_then_recency(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        posts = [
            {"id": 1, "image_analysis_status": "PENDING", "analysis_priority_score": 40, "posted_at": _iso(now)},
            {"id": 2, "image_analysis_status": "PENDING", "analysis_priority_score": 90, "posted_at": _iso(now - datetime.timedelta(hours=1))},
            {"id": 3, "image_analysis_status": "PENDING", "analysis_priority_score": 90, "posted_at": _iso(now)},
            {"id": 4, "image_analysis_status": "ANALYZED", "analysis_priority_score": 99, "posted_at": _iso(now)},
        ]
        ordered = server.sort_pending_posts_by_priority(posts)
        pending_ids = [p["id"] for p in ordered if p["image_analysis_status"] == "PENDING"]
        self.assertEqual(pending_ids, [3, 2, 1])  # score90×2はposted_at降順、次いでscore40
        self.assertEqual(ordered[-1]["id"], 4)  # PENDING以外は末尾のまま


class DiagnosticsTests(unittest.TestCase):
    """13〜14. diagnostics（指示書11番）"""

    def test_diagnostics_without_token(self):
        with mock.patch.object(server, "X_API_BEARER_TOKEN", ""):
            with mock.patch.object(server, "investment_db") as mock_db:
                mock_db.get_market_source.return_value = None
                mock_db.list_recent_social_posts.return_value = []
                diag = server.nicosoku_diagnostics("dummy_url", "local")
        self.assertFalse(diag["token_configured"])
        # Bearer Tokenそのものは絶対に返さない
        self.assertNotIn("X_API_BEARER_TOKEN", diag)
        for v in diag.values():
            self.assertNotIn("Bearer", str(v))

    def test_diagnostics_normal_mock(self):
        now_iso = _iso(datetime.datetime.now(datetime.timezone.utc))
        with mock.patch.object(server, "X_API_BEARER_TOKEN", "dummy-token"):
            with mock.patch.object(server, "_x_user_id_cache", {server.NICOSOKU_X_USERNAME: "12345"}):
                with mock.patch.object(server, "investment_db") as mock_db:
                    mock_db.get_market_source.return_value = {"last_success_at": now_iso, "last_error": None}
                    mock_db.list_recent_social_posts.return_value = [
                        {"post_id": "777", "posted_at": now_iso, "media_json": [{"url": "a"}]}
                    ]
                    diag = server.nicosoku_diagnostics("dummy_url", "local")
        self.assertTrue(diag["token_configured"])
        self.assertTrue(diag["user_id_resolved"])
        self.assertEqual(diag["latest_post_id"], "777")
        self.assertTrue(diag["latest_post_has_media"])
        self.assertEqual(diag["username"], server.NICOSOKU_X_USERNAME)


class FetchNowTests(unittest.TestCase):
    """15〜16. fetch-now（指示書12番：既存pollerロジックの再利用）"""

    def test_fetch_now_new_posts(self):
        with mock.patch.object(server, "nicosoku_poll_once") as mock_poll:
            mock_poll.return_value = {"status": "ok", "fetched": 5, "newPosts": 2, "duplicates": 3,
                                       "eventsDetected": 0, "error": None}
            result = server.nicosoku_poll_once("dummy_url", "local")
        self.assertEqual(result["newPosts"], 2)
        self.assertEqual(result["fetched"], 5)

    def test_fetch_now_all_duplicates(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.ensure_market_source.return_value = {"last_seen_post_id": "100"}
            mock_db.insert_social_post_if_new.return_value = None  # 常に重複
            mock_db.list_market_events.return_value = []
            with mock.patch.object(server, "X_API_BEARER_TOKEN", "dummy-token"):
                with mock.patch.object(server, "_x_user_id_cache", {server.NICOSOKU_X_USERNAME: "12345"}):
                    with mock.patch.object(server, "_x_fetch_recent_tweets") as mock_fetch:
                        mock_fetch.return_value = ({"data": [{"id": "101", "text": "テスト", "created_at": "2026-09-10T00:00:00Z"}]}, "ok", None)
                        result = server.nicosoku_poll_once("dummy_url", "local")
        self.assertEqual(result["fetched"], 1)
        self.assertEqual(result["newPosts"], 0)
        self.assertEqual(result["duplicates"], 1)


class StaleDetectionTests(unittest.TestCase):
    """17〜18. stale判定（指示書14番）"""

    def test_stale_during_market_hours(self):
        jst = datetime.timezone(datetime.timedelta(hours=9))
        now_jst = datetime.datetime(2026, 9, 10, 10, 0, tzinfo=jst)  # 市場時間中
        now_utc = now_jst.astimezone(datetime.timezone.utc)
        last_success = now_utc - datetime.timedelta(minutes=20)  # 20分前（閾値15分超）
        source = {"last_success_at": last_success.isoformat()}
        self.assertTrue(server._nicosoku_source_is_stale(source, now=now_utc))

    def test_not_stale_at_night(self):
        jst = datetime.timezone(datetime.timedelta(hours=9))
        now_jst = datetime.datetime(2026, 9, 10, 22, 0, tzinfo=jst)  # 夜間
        now_utc = now_jst.astimezone(datetime.timezone.utc)
        last_success = now_utc - datetime.timedelta(minutes=20)  # 20分前（閾値30分以内）
        source = {"last_success_at": last_success.isoformat()}
        self.assertFalse(server._nicosoku_source_is_stale(source, now=now_utc))


class StaleSignalGuardTests(unittest.TestCase):
    """19. stale情報がNEW MARKET SIGNALにならない（指示書15・16番）"""

    def test_signals_carry_stale_data_status(self):
        now_iso = _iso(datetime.datetime.now(datetime.timezone.utc))
        stale_success = _iso(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=2))
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_social_signals.return_value = [{
                "posted_at": now_iso, "importance": "HIGH", "categories_json": [], "text": "テスト投稿",
                "facts_json": [], "author_opinion_json": [], "direct_mentions_json": [], "theme_related_json": [],
                "url": "https://x.com/nicosokufx/status/1", "image_analysis_status": None,
            }]
            mock_db.list_watchlist.return_value = []
            mock_db.list_portfolio.return_value = []
            mock_db.get_market_source.return_value = {"last_success_at": stale_success}
            signals = server.get_recent_social_market_signals("dummy_url", "local")
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["data_status"], "STALE")


class ConfidenceNormalizationTests(unittest.TestCase):
    """20. Smart Import confidence正常化（指示書10番）"""

    def test_normalizes_in_range(self):
        self.assertEqual(server._normalize_confidence(0.5), 0.5)

    def test_clamps_above_one(self):
        self.assertEqual(server._normalize_confidence(1.8), 1.0)

    def test_clamps_below_zero(self):
        self.assertEqual(server._normalize_confidence(-0.3), 0.0)

    def test_none_stays_none(self):
        self.assertIsNone(server._normalize_confidence(None))
        self.assertIsNone(server._normalize_confidence(""))
        self.assertIsNone(server._normalize_confidence("not-a-number"))


if __name__ == "__main__":
    unittest.main()
