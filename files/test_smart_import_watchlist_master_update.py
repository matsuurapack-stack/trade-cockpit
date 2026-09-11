# Smart Import「監視銘柄更新」（type: watchlist_master_update）テスト。
#
# SBI証券等の監視銘柄マスターエクスポート（stocks配列）を一括でwatchlistへupsertする機能。
# 既存のWATCHLIST_UPDATE（1件ずつADD/UPDATE/REMOVE）とは別カテゴリとして実装。
#
# 実行方法： cd files && python -m unittest test_smart_import_watchlist_master_update -v

import unittest
from unittest import mock

import server


class ClassifyWatchlistMasterUpdateTests(unittest.TestCase):
    """type: watchlist_master_updateの検出。UNKNOWN扱いにしない。"""

    def test_detected_as_watchlist_master_update_with_stocks(self):
        item = {"type": "watchlist_master_update", "update_mode": "add",
                 "stocks": [{"code": "7203", "name": "トヨタ自動車"}]}
        category, confidence, draft = server._classify_json_item(item)
        self.assertEqual(category, "WATCHLIST_MASTER_UPDATE")
        self.assertEqual(confidence, "HIGH")
        self.assertIs(draft, item)

    def test_low_confidence_without_stocks_but_not_unknown(self):
        item = {"type": "watchlist_master_update", "update_mode": "sync"}
        category, confidence, _ = server._classify_json_item(item)
        self.assertEqual(category, "WATCHLIST_MASTER_UPDATE")
        self.assertEqual(confidence, "LOW")

    def test_empty_stocks_list_is_low_confidence(self):
        item = {"type": "watchlist_master_update", "stocks": []}
        category, confidence, _ = server._classify_json_item(item)
        self.assertEqual(category, "WATCHLIST_MASTER_UPDATE")
        self.assertEqual(confidence, "LOW")

    def test_classify_content_end_to_end_via_json_text(self):
        import json
        text = json.dumps({"type": "watchlist_master_update", "update_mode": "sync",
                             "stocks": [{"code": "417A", "name": "テスト銘柄"}]})
        candidates = server.classify_content(text)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["category"], "WATCHLIST_MASTER_UPDATE")
        self.assertNotEqual(candidates[0]["category"], "UNKNOWN")


class AlphanumericCodeTests(unittest.TestCase):
    """417A/593Aのような英字入り銘柄コードを文字列としてそのまま保持する。"""

    def test_alnum_code_preserved_as_string(self):
        draft = {"stocks": [{"code": "417A", "name": "銘柄A"}, {"code": "593A", "name": "銘柄B"}]}
        normalized = server.normalize_watchlist_master_update(draft)
        codes = [s["code"] for s in normalized["stocks"]]
        self.assertEqual(codes, ["417A", "593A"])
        self.assertIsInstance(codes[0], str)

    def test_numeric_looking_code_not_converted_to_int(self):
        draft = {"stocks": [{"code": "7203", "name": "トヨタ自動車"}]}
        normalized = server.normalize_watchlist_master_update(draft)
        self.assertEqual(normalized["stocks"][0]["code"], "7203")
        self.assertIsInstance(normalized["stocks"][0]["code"], str)


class NormalizeWatchlistMasterUpdateTests(unittest.TestCase):
    """normalize_watchlist_master_update()の正規化ルール。"""

    def test_none_when_stocks_missing(self):
        self.assertIsNone(server.normalize_watchlist_master_update({}))

    def test_none_when_stocks_not_a_list(self):
        self.assertIsNone(server.normalize_watchlist_master_update({"stocks": "not-a-list"}))

    def test_none_when_stocks_empty(self):
        self.assertIsNone(server.normalize_watchlist_master_update({"stocks": []}))

    def test_none_when_all_stocks_invalid(self):
        draft = {"stocks": [{"code": ""}, {"name": "コード無し"}]}
        self.assertIsNone(server.normalize_watchlist_master_update(draft))

    def test_duplicate_code_in_batch_keeps_first_only(self):
        draft = {"stocks": [{"code": "7203", "name": "先"}, {"code": "7203", "name": "後"}]}
        normalized = server.normalize_watchlist_master_update(draft)
        self.assertEqual(len(normalized["stocks"]), 1)
        self.assertEqual(normalized["stocks"][0]["name"], "先")

    def test_update_mode_add_default(self):
        draft = {"stocks": [{"code": "7203"}]}
        normalized = server.normalize_watchlist_master_update(draft)
        self.assertEqual(normalized["update_mode"], "add")

    def test_update_mode_sync_recognized(self):
        draft = {"stocks": [{"code": "7203"}], "update_mode": "sync"}
        normalized = server.normalize_watchlist_master_update(draft)
        self.assertEqual(normalized["update_mode"], "sync")

    def test_unknown_update_mode_falls_back_to_add(self):
        draft = {"stocks": [{"code": "7203"}], "update_mode": "delete_everything"}
        normalized = server.normalize_watchlist_master_update(draft)
        self.assertEqual(normalized["update_mode"], "add")

    def test_name_optional(self):
        draft = {"stocks": [{"code": "7203"}]}
        normalized = server.normalize_watchlist_master_update(draft)
        self.assertIsNone(normalized["stocks"][0]["name"])


class PreviewStatsTests(unittest.TestCase):
    """解析後プレビューの「追加予定○件 / 既存○件 / 無効○件」集計。"""

    def test_add_existing_invalid_counts(self):
        draft = {"stocks": [{"code": "7203"}, {"code": "9984"}, {"code": ""}, {"code": "7203"}]}
        existing_codes = {"9984"}
        stats = server.compute_watchlist_master_update_preview_stats(draft, existing_codes)
        self.assertEqual(stats["add_count"], 1)       # 7203（新規）
        self.assertEqual(stats["existing_count"], 1)  # 9984（既存）
        self.assertEqual(stats["invalid_count"], 2)   # 空code + 重複7203
        self.assertEqual(stats["total"], 4)

    def test_no_stocks_returns_zeroed_stats(self):
        stats = server.compute_watchlist_master_update_preview_stats({}, set())
        self.assertEqual(stats, {"add_count": 0, "existing_count": 0, "invalid_count": 0, "total": 0})


class SmartImportConfirmDispatchTests(unittest.TestCase):
    """smart_import_confirm()がWATCHLIST_MASTER_UPDATEをupsert_watchlist_master_stocks()へ
    正しく振り分けること。"""

    def test_dispatches_to_upsert_watchlist_master_stocks(self):
        candidates = [{"category": "WATCHLIST_MASTER_UPDATE", "confidence": "HIGH",
                        "draft": {"stocks": [{"code": "7203", "name": "トヨタ自動車"}], "update_mode": "sync"},
                        "raw_text": "{}"}]
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.upsert_watchlist_master_stocks.return_value = {
                "added": 1, "updated": 0, "invalid": 0, "inactive_candidates": 3}
            result = server.smart_import_confirm("postgres://x", "local", candidates)
        mock_db.upsert_watchlist_master_stocks.assert_called_once_with(
            "postgres://x", "local", [{"code": "7203", "name": "トヨタ自動車"}], update_mode="sync")
        self.assertEqual(result["results"]["WATCHLIST_MASTER_UPDATE"]["imported"], 1)
        detail = result["results"]["WATCHLIST_MASTER_UPDATE"]["details"][0]
        self.assertTrue(detail["ok"])
        self.assertEqual(detail["inactive_candidates"], 3)

    def test_invalid_draft_reports_failure_without_raising(self):
        candidates = [{"category": "WATCHLIST_MASTER_UPDATE", "confidence": "HIGH",
                        "draft": {"stocks": []}, "raw_text": "{}"}]
        with mock.patch.object(server, "investment_db") as mock_db:
            result = server.smart_import_confirm("postgres://x", "local", candidates)
        mock_db.upsert_watchlist_master_stocks.assert_not_called()
        detail = result["results"]["WATCHLIST_MASTER_UPDATE"]["details"][0]
        self.assertFalse(detail["ok"])

    def test_low_confidence_without_force_is_rejected(self):
        candidates = [{"category": "WATCHLIST_MASTER_UPDATE", "confidence": "LOW",
                        "draft": {"stocks": []}, "raw_text": "{}"}]
        with mock.patch.object(server, "investment_db") as mock_db:
            result = server.smart_import_confirm("postgres://x", "local", candidates)
        self.assertEqual(result["rejected_low_confidence"], 1)
        mock_db.upsert_watchlist_master_stocks.assert_not_called()

    def test_db_exception_survives_and_reports_failure(self):
        candidates = [{"category": "WATCHLIST_MASTER_UPDATE", "confidence": "HIGH",
                        "draft": {"stocks": [{"code": "7203"}]}, "raw_text": "{}"}]
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.upsert_watchlist_master_stocks.side_effect = Exception("db down")
            result = server.smart_import_confirm("postgres://x", "local", candidates)
        detail = result["results"]["WATCHLIST_MASTER_UPDATE"]["details"][0]
        self.assertFalse(detail["ok"])
        self.assertIn("db down", detail["reason"])


class PreviewAnnotationTests(unittest.TestCase):
    """smart_import_check_duplicates()がpreview_statsをdraftへ付与すること。"""

    def test_preview_stats_attached_to_candidate(self):
        candidates = [{"category": "WATCHLIST_MASTER_UPDATE", "confidence": "HIGH",
                        "draft": {"stocks": [{"code": "7203"}, {"code": "9984"}]}}]
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_watchlist.return_value = [{"code": "9984"}]
            result = server.smart_import_check_duplicates("postgres://x", "local", candidates)
        stats = result[0]["draft"]["preview_stats"]
        self.assertEqual(stats["add_count"], 1)
        self.assertEqual(stats["existing_count"], 1)
        self.assertEqual(stats["invalid_count"], 0)


class CategoryRegistrationTests(unittest.TestCase):
    """UNKNOWN扱いにせずカテゴリ一覧・実装済み集合へ正しく登録されていること。"""

    def test_category_listed(self):
        self.assertIn("WATCHLIST_MASTER_UPDATE", server.SMART_IMPORT_CATEGORIES)

    def test_category_implemented(self):
        self.assertIn("WATCHLIST_MASTER_UPDATE", server.SMART_IMPORT_IMPLEMENTED_CATEGORIES)

    def test_category_label_shown_in_html_not_unknown(self):
        with open("trade-cockpit.html", encoding="utf-8") as f:
            html = f.read()
        self.assertIn("WATCHLIST_MASTER_UPDATE:{icon:", html)
        self.assertTrue("WATCHLIST_UPDATE（監視銘柄更新）" in html or "監視銘柄更新" in html)


if __name__ == "__main__":
    unittest.main()
