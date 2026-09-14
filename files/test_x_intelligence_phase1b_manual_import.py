# X Intelligence Phase1B（2026-09-15新規）：手動X投稿取り込み経路のテスト。
#
# 監査（2026-09-14）で判明した2つの問題の回帰防止：
#  1. X_API_BEARER_TOKEN未設定で自動取得が0件のまま
#  2. 既存のSOCIAL_IMAGE_ANALYSIS（画像解析追記）はUPDATE-onlyで、対象投稿が
#     事前に存在しない限り保存できない（新規X投稿を取り込む手段が無い）
#
# 実DBには接続せず、investment_dbをモックしてSmart Import経路のロジックのみを検証する。
#
# 実行方法： cd files && python -m unittest test_x_intelligence_phase1b_manual_import -v

import datetime
import unittest
from unittest import mock

import server


class ParseXPostUrlTests(unittest.TestCase):
    def test_x_com_url(self):
        h, pid = server.parse_x_post_url("https://x.com/nicosokufx/status/1234567890123456789")
        self.assertEqual(h, "nicosokufx")
        self.assertEqual(pid, "1234567890123456789")

    def test_twitter_com_url(self):
        h, pid = server.parse_x_post_url("https://twitter.com/nikkei/status/987654321")
        self.assertEqual(h, "nikkei")
        self.assertEqual(pid, "987654321")

    def test_url_with_query_string(self):
        h, pid = server.parse_x_post_url("https://x.com/aryarya/status/555?s=21")
        self.assertEqual(h, "aryarya")
        self.assertEqual(pid, "555")

    def test_none_url(self):
        self.assertEqual(server.parse_x_post_url(None), (None, None))

    def test_non_matching_url(self):
        self.assertEqual(server.parse_x_post_url("https://example.com/foo"), (None, None))


class ClassifyJsonItemSocialPostImportTests(unittest.TestCase):
    """classify_content経由でSOCIAL_POST_IMPORTとして検出されること。"""

    def test_detected_via_explicit_type(self):
        item = {"type": "social_post_import", "post_url": "https://x.com/nicosokufx/status/1",
                "text": "9/17 FOMC 9/18 日銀会合"}
        category, confidence, draft = server._classify_json_item(item)
        self.assertEqual(category, "SOCIAL_POST_IMPORT")
        self.assertEqual(confidence, "HIGH")

    def test_detected_via_shape_without_explicit_type(self):
        item = {"post_url": "https://x.com/nikkei/status/2", "text": "日銀、追加利上げ観測"}
        category, confidence, _ = server._classify_json_item(item)
        self.assertEqual(category, "SOCIAL_POST_IMPORT")
        self.assertEqual(confidence, "HIGH")

    def test_low_confidence_when_handle_unresolvable(self):
        item = {"type": "social_post_import", "text": "本文のみ、URLもhandleも無い"}
        category, confidence, _ = server._classify_json_item(item)
        self.assertEqual(category, "SOCIAL_POST_IMPORT")
        self.assertEqual(confidence, "LOW")


class NormalizeSocialPostImportTests(unittest.TestCase):
    def _mock_db(self):
        patcher = mock.patch.object(server, "investment_db")
        mock_db = patcher.start()
        self.addCleanup(patcher.stop)
        mock_db.list_portfolio.return_value = []
        mock_db.list_watchlist.return_value = []
        return mock_db

    def test_handle_resolved_from_url(self):
        self._mock_db()
        draft = {"post_url": "https://x.com/nicosokufx/status/111", "text": "9/17 FOMC結果に注目"}
        result = server.normalize_social_post_import("dummy_url", "matsuura", draft)
        self.assertIsNotNone(result)
        self.assertEqual(result["resolved_handle"], "nicosokufx")
        self.assertTrue(result["is_official_source"])
        self.assertEqual(result["post_record"]["post_id"], "111")
        self.assertEqual(result["post_record"]["source_handle"], "nicosokufx")

    def test_explicit_source_handle_overrides_url(self):
        self._mock_db()
        draft = {"post_url": "https://x.com/someoneelse/status/222", "source_handle": "nikkei",
                  "text": "日銀会合が近づく"}
        result = server.normalize_social_post_import("dummy_url", "matsuura", draft)
        self.assertEqual(result["resolved_handle"], "nikkei")

    def test_unofficial_handle_flagged_not_official(self):
        self._mock_db()
        draft = {"post_url": "https://x.com/random_trader_123/status/333", "text": "適当な投稿"}
        result = server.normalize_social_post_import("dummy_url", "matsuura", draft)
        self.assertIsNotNone(result)
        self.assertFalse(result["is_official_source"])
        self.assertEqual(result["resolved_handle"], "random_trader_123")

    def test_manual_post_id_generated_when_missing_and_stable(self):
        self._mock_db()
        draft = {"source_handle": "kgbukabu", "text": "急騰銘柄あり", "posted_at": "2026-09-15T08:00:00+09:00"}
        r1 = server.normalize_social_post_import("dummy_url", "matsuura", draft)
        r2 = server.normalize_social_post_import("dummy_url", "matsuura", draft)
        self.assertTrue(r1["post_record"]["post_id"].startswith("manual:"))
        self.assertEqual(r1["post_record"]["post_id"], r2["post_record"]["post_id"])  # 同一入力→同一ID

    def test_no_text_and_no_url_returns_none(self):
        self._mock_db()
        self.assertIsNone(server.normalize_social_post_import("dummy_url", "matsuura", {}))

    def test_no_resolvable_handle_returns_none(self):
        self._mock_db()
        draft = {"text": "本文はあるがURLもhandleも無い"}
        self.assertIsNone(server.normalize_social_post_import("dummy_url", "matsuura", draft))

    def test_invalid_posted_at_falls_back_to_none_not_guessed(self):
        self._mock_db()
        draft = {"source_handle": "nicosokufx", "text": "テスト", "posted_at": "not-a-date"}
        result = server.normalize_social_post_import("dummy_url", "matsuura", draft)
        self.assertIsNone(result["post_record"]["posted_at"])

    def test_image_analysis_facts_and_author_view_merged(self):
        self._mock_db()
        draft = {"source_handle": "nicosokufx", "text": "画像投稿の本文",
                  "image_analysis": {"facts": ["9/17 FOMC", "9/18 日銀会合"], "author_view": "利上げ観測強まる"}}
        result = server.normalize_social_post_import("dummy_url", "matsuura", draft)
        facts = result["post_record"]["facts"]
        opinions = result["post_record"]["author_opinion"]
        self.assertIn("9/17 FOMC", facts)
        self.assertIn("9/18 日銀会合", facts)
        self.assertIn("利上げ観測強まる", opinions)

    def test_author_opinion_not_treated_as_fact(self):
        # 指示書2番「個人発信の見解をFACT扱いしない」：author_viewはfactsへ混ざらない。
        self._mock_db()
        draft = {"source_handle": "aryarya", "text": "本文",
                  "image_analysis": {"facts": ["需給が良い"], "author_view": "上昇継続と予想する"}}
        result = server.normalize_social_post_import("dummy_url", "matsuura", draft)
        self.assertNotIn("上昇継続と予想する", result["post_record"]["facts"])
        self.assertIn("上昇継続と予想する", result["post_record"]["author_opinion"])


class SmartImportConfirmSocialPostImportE2ETests(unittest.TestCase):
    """smart_import_confirm()経由でinsert_social_post_if_new→イベント抽出まで一気通貫。"""

    def test_new_post_inserted_and_fomc_event_extracted(self):
        candidates = [{"category": "SOCIAL_POST_IMPORT", "confidence": "HIGH",
                       "draft": {"post_url": "https://x.com/nicosokufx/status/999",
                                 "text": "9/17 FOMC 9/18 日銀会合",
                                 "posted_at": "2026-09-15T08:00:00+09:00"}}]
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_portfolio.return_value = []
            mock_db.list_watchlist.return_value = []
            mock_db.insert_social_post_if_new.return_value = {
                "id": 1, "posted_at": "2026-09-15T08:00:00+09:00", "source_handle": "nicosokufx", "post_id": "999"}
            mock_db.list_market_events.return_value = []
            mock_db.import_market_events.return_value = {"imported": 2, "updated": 0, "skipped": 0, "errors": 0}
            result = server.smart_import_confirm("dummy_url", "matsuura", candidates, "test")
        self.assertEqual(result["results"]["SOCIAL_POST_IMPORT"]["imported"], 1)
        detail = result["results"]["SOCIAL_POST_IMPORT"]["details"][0]
        self.assertTrue(detail["ok"])
        self.assertEqual(detail["source_handle"], "nicosokufx")
        self.assertTrue(detail["is_official_source"])
        self.assertEqual(detail["events_imported"], 2)
        mock_db.insert_social_post_if_new.assert_called_once()
        mock_db.import_market_events.assert_called_once()

    def test_duplicate_post_reported_not_silently_dropped(self):
        candidates = [{"category": "SOCIAL_POST_IMPORT", "confidence": "HIGH",
                       "draft": {"post_url": "https://x.com/nicosokufx/status/999", "text": "重複投稿"}}]
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_portfolio.return_value = []
            mock_db.list_watchlist.return_value = []
            mock_db.insert_social_post_if_new.return_value = None  # 既存（ON CONFLICT DO NOTHING）
            result = server.smart_import_confirm("dummy_url", "matsuura", candidates, "test")
        detail = result["results"]["SOCIAL_POST_IMPORT"]["details"][0]
        self.assertFalse(detail["ok"])
        self.assertIn("重複", detail["reason"])

    def test_image_analysis_attached_after_insert_no_update_only_deadlock(self):
        # 回帰確認：従来のUPDATE-only設計では対象投稿が存在せず失敗していたケースが、
        # このSOCIAL_POST_IMPORT経路では投稿を先に作ってから解析結果を追記するため成功する。
        candidates = [{"category": "SOCIAL_POST_IMPORT", "confidence": "HIGH",
                       "draft": {"post_url": "https://x.com/nikkei/status/888",
                                 "text": "画像付き投稿の本文",
                                 "image_analysis": {"facts": ["9/20 米小売売上高"], "author_view": "円安継続を注視"}}}]
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_portfolio.return_value = []
            mock_db.list_watchlist.return_value = []
            mock_db.insert_social_post_if_new.return_value = {
                "id": 2, "posted_at": None, "source_handle": "nikkei", "post_id": "888"}
            mock_db.list_market_events.return_value = []
            mock_db.import_market_events.return_value = {"imported": 1, "updated": 0, "skipped": 0, "errors": 0}
            result = server.smart_import_confirm("dummy_url", "matsuura", candidates, "test")
        detail = result["results"]["SOCIAL_POST_IMPORT"]["details"][0]
        self.assertTrue(detail["ok"])
        self.assertTrue(detail["has_image_analysis"])
        mock_db.save_social_post_image_analysis.assert_called_once()

    def test_low_confidence_rejected_without_force(self):
        candidates = [{"category": "SOCIAL_POST_IMPORT", "confidence": "LOW",
                       "draft": {"text": "本文のみ"}}]
        with mock.patch.object(server, "investment_db") as mock_db:
            result = server.smart_import_confirm("dummy_url", "matsuura", candidates, "test")
        self.assertEqual(result["rejected_low_confidence"], 1)
        mock_db.insert_social_post_if_new.assert_not_called()


if __name__ == "__main__":
    unittest.main()
