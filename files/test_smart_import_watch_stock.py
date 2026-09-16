# Smart Import「監視候補銘柄」（WATCH_STOCK）テスト。
#
# 会社四季報・ChatGPT/Claude・ニュース分析等から取得した監視候補銘柄JSON
# （type: watch_stock、code/name/theme/source/reason/tags/priority）が、従来
# category=UNKNOWN・confidence=LOW/DEGRADEDに落ちて保存できなかった不具合の修正。
# STEP1（type正規化）・STEP2（typeなしfallback）・STEP3（confidence判定）・
# STEP6（既存watchlistへ候補として保存・重複防止・マージ）・STEP7（配列/wrapper対応）・
# STEP8（146A等新フォーマットコード対応）を検証する。
#
# 実行方法： cd files && python -m unittest test_smart_import_watch_stock -v

import json
import unittest
from unittest import mock

import server
import investment_db


def _six_stocks_sample():
    """指示書に記載の7件のうち、typeを明示した代表例（実データE2Eは別スクリプトで検証）。"""
    return {"type": "watch_stock", "code": "3110", "name": "日東紡",
            "theme": "先端半導体・特殊ガラス・データセンター", "source": "会社四季報 秋号",
            "reason": "先端半導体向け特殊ガラスが好調。データセンター需要を背景に低熱膨張ガラスなどが伸長。",
            "tags": ["半導体", "データセンター", "素材"], "priority": "high"}


class ClassifyWatchStockTypeAliasesTests(unittest.TestCase):
    """STEP1：type文字列のゆれをすべてWATCH_STOCKへ正規化する。UNKNOWNへ落とさない。"""

    def test_watch_stock_type_detected(self):
        item = _six_stocks_sample()
        category, confidence, draft = server._classify_json_item(item)
        self.assertEqual(category, "WATCH_STOCK")
        self.assertNotEqual(category, "UNKNOWN")
        self.assertIs(draft, item)

    def test_all_type_aliases_normalize_to_watch_stock(self):
        aliases = ["watch_stock", "watchlist", "watchlist_stock", "stock_candidate",
                   "watch_candidate", "external_watchlist", "monitor_stock", "monitor_candidate"]
        for alias in aliases:
            with self.subTest(alias=alias):
                item = {"type": alias, "code": "6480", "name": "日本トムソン", "theme": "ベアリング"}
                category, _confidence, _draft = server._classify_json_item(item)
                self.assertEqual(category, "WATCH_STOCK")

    def test_type_alias_case_insensitive(self):
        item = {"type": "WATCH_STOCK", "code": "6227", "name": "AIメカテック", "source": "ニュース"}
        category, _confidence, _draft = server._classify_json_item(item)
        self.assertEqual(category, "WATCH_STOCK")


class FallbackWithoutTypeTests(unittest.TestCase):
    """STEP2：typeが無くてもcode+name+補足情報が揃えば株候補としてfallback判定する。"""

    def test_code_name_reason_fallback_to_watch_stock(self):
        item = {"code": "4419", "name": "Finatextホールディングス", "reason": "金融インフラSaaSが拡大中"}
        category, confidence, _draft = server._classify_json_item(item)
        self.assertEqual(category, "WATCH_STOCK")
        self.assertNotEqual(category, "UNKNOWN")
        self.assertEqual(confidence, "MEDIUM")

    def test_no_supporting_field_stays_unknown(self):
        """code+nameだけでtheme/reason/tags/sourceが1つも無ければfallbackしない
        （UNKNOWNを何でもWATCH_STOCKへ昇格させることの禁止、指示書の重要制約）。"""
        item = {"code": "4180", "name": "Appier Group"}
        category, _confidence, _draft = server._classify_json_item(item)
        self.assertEqual(category, "UNKNOWN")

    def test_missing_code_stays_unknown(self):
        item = {"name": "日東紡", "theme": "半導体"}
        category, _confidence, _draft = server._classify_json_item(item)
        self.assertEqual(category, "UNKNOWN")

    def test_missing_name_stays_unknown(self):
        item = {"code": "3110", "theme": "半導体"}
        category, _confidence, _draft = server._classify_json_item(item)
        self.assertEqual(category, "UNKNOWN")

    def test_invalid_code_format_stays_unknown(self):
        item = {"code": "not-a-code", "name": "何か", "theme": "半導体"}
        category, _confidence, _draft = server._classify_json_item(item)
        self.assertEqual(category, "UNKNOWN")

    def test_does_not_hijack_existing_catalyst_heuristic(self):
        """STEP9-8：既存のtitle/catalyst_dateベースのCATALYST判定を壊さない。"""
        item = {"title": "○○社、新製品発表", "catalyst_date": "2026-09-20"}
        category, _confidence, _draft = server._classify_json_item(item)
        self.assertEqual(category, "CATALYST")

    def test_does_not_hijack_existing_event_heuristic(self):
        item = {"event_date": "2026-09-20", "title": "FOMC政策金利発表"}
        category, _confidence, _draft = server._classify_json_item(item)
        self.assertEqual(category, "EVENT")


class ConfidenceQualityTests(unittest.TestCase):
    """STEP3：code+name+推奨項目1つ以上で最低MEDIUM、code/name/theme/reason/sourceが
    すべて揃えばHIGHでもよい。priorityはimport confidenceとは別物として扱う。"""

    def test_all_fields_present_is_high_confidence(self):
        item = _six_stocks_sample()
        _category, confidence, _draft = server._classify_json_item(item)
        self.assertEqual(confidence, "HIGH")

    def test_one_supporting_field_is_at_least_medium(self):
        for field in ("theme", "reason", "source"):
            with self.subTest(field=field):
                item = {"type": "watch_stock", "code": "9766", "name": "コナミグループ", field: "値"}
                _category, confidence, _draft = server._classify_json_item(item)
                self.assertIn(confidence, ("MEDIUM", "HIGH"))
                self.assertNotEqual(confidence, "LOW")

    def test_missing_code_is_low_confidence(self):
        item = {"type": "watch_stock", "name": "コード無し", "theme": "テーマ"}
        _category, confidence, _draft = server._classify_json_item(item)
        self.assertEqual(confidence, "LOW")

    def test_priority_field_is_not_used_as_confidence(self):
        """"priority":"high"は監視優先度であり、import confidenceとは無関係。"""
        item = {"type": "watch_stock", "code": "9766", "name": "コナミグループ", "priority": "high"}
        _category, confidence, _draft = server._classify_json_item(item)
        # priorityしか無くtheme/reason/sourceが無い場合でもLOW/DEGRADEDにはしない
        # （code+nameが揃っている＝必要十分の下限、MEDIUM以上を維持する設計）。
        self.assertNotEqual(confidence, "LOW")


class JpStockCodeValidationTests(unittest.TestCase):
    """STEP8：数字4桁だけでなく英字入り新コード（146A等）にも対応する。"""

    def test_required_codes_all_valid(self):
        codes = ["6480", "4419", "4180", "3110", "146A", "9766", "6227"]
        for code in codes:
            with self.subTest(code=code):
                self.assertTrue(server._is_valid_jp_stock_code(code))

    def test_lowercase_alnum_code_valid(self):
        self.assertTrue(server._is_valid_jp_stock_code("146a"))

    def test_too_short_or_too_long_invalid(self):
        self.assertFalse(server._is_valid_jp_stock_code("146"))
        self.assertFalse(server._is_valid_jp_stock_code("14600"))

    def test_non_stock_code_invalid(self):
        self.assertFalse(server._is_valid_jp_stock_code("ABCD"))
        self.assertFalse(server._is_valid_jp_stock_code(""))
        self.assertFalse(server._is_valid_jp_stock_code(None))


class ArrayAndWrapperParsingTests(unittest.TestCase):
    """STEP7：配列JSON対応は現状維持。{"stocks":[...]}形式もflatten対応する。"""

    def test_top_level_array_of_seven_all_watch_stock(self):
        stocks = [
            {"type": "watch_stock", "code": "6480", "name": "日本トムソン"},
            {"type": "watch_stock", "code": "4419", "name": "Finatextホールディングス"},
            {"type": "watch_stock", "code": "4180", "name": "Appier Group"},
            {"type": "watch_stock", "code": "3110", "name": "日東紡", "theme": "半導体"},
            {"type": "watch_stock", "code": "146A", "name": "コロンビア・ワークス", "reason": "新規上場"},
            {"type": "watch_stock", "code": "9766", "name": "コナミグループ", "source": "ニュース"},
            {"type": "watch_stock", "code": "6227", "name": "AIメカテック", "theme": "AI"},
        ]
        text = json.dumps(stocks, ensure_ascii=False)
        candidates = server.classify_content(text)
        self.assertEqual(len(candidates), 7)
        self.assertTrue(all(c["category"] == "WATCH_STOCK" for c in candidates))
        self.assertEqual(sum(1 for c in candidates if c["category"] == "UNKNOWN"), 0)

    def test_stocks_wrapper_object_flattened(self):
        payload = {"stocks": [
            {"type": "watch_stock", "code": "3110", "name": "日東紡", "theme": "半導体"},
            {"type": "watch_stock", "code": "6227", "name": "AIメカテック", "theme": "AI"},
        ]}
        text = json.dumps(payload, ensure_ascii=False)
        candidates = server.classify_content(text)
        self.assertEqual(len(candidates), 2)
        self.assertTrue(all(c["category"] == "WATCH_STOCK" for c in candidates))

    def test_watchlist_master_update_stocks_field_not_hijacked_by_wrapper_flatten(self):
        """"stocks"キーの衝突防止：type付きのwatchlist_master_update payloadは、その
        stocks配列をWATCH_STOCK群として誤って展開しない（既存カテゴリ判定の保護）。"""
        payload = {"type": "watchlist_master_update", "update_mode": "add",
                    "stocks": [{"code": "7203", "name": "トヨタ自動車"}]}
        text = json.dumps(payload, ensure_ascii=False)
        candidates = server.classify_content(text)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["category"], "WATCHLIST_MASTER_UPDATE")

    def test_malformed_json_stays_safely_degraded(self):
        """STEP9-7：malformed JSONは従来通り安全にUNKNOWN/自然文解析へフォールバックする
        （例外を投げない）。"""
        text = "{type: watch_stock, code: 3110 not valid json"
        candidates = server.classify_content(text)
        self.assertIsInstance(candidates, list)  # 例外を投げずに何らかのリストを返す


class NormalizeWatchStockTests(unittest.TestCase):
    """normalize_watch_stock()の正規化ルール。146A等の英字混在コードを文字列のまま保持する。"""

    def test_none_when_code_missing(self):
        self.assertIsNone(server.normalize_watch_stock({"name": "コード無し"}))

    def test_alnum_code_preserved_as_string(self):
        normalized = server.normalize_watch_stock({"code": "146A", "name": "コロンビア・ワークス"})
        self.assertEqual(normalized["code"], "146A")
        self.assertIsInstance(normalized["code"], str)

    def test_fields_carried_through(self):
        normalized = server.normalize_watch_stock(_six_stocks_sample())
        self.assertEqual(normalized["code"], "3110")
        self.assertEqual(normalized["name"], "日東紡")
        self.assertEqual(normalized["theme"], "先端半導体・特殊ガラス・データセンター")
        self.assertEqual(normalized["source"], "会社四季報 秋号")
        self.assertEqual(normalized["reason"].startswith("先端半導体向け"), True)
        self.assertEqual(normalized["tags"], ["半導体", "データセンター", "素材"])
        self.assertEqual(normalized["priority"], "high")

    def test_invalid_priority_dropped(self):
        normalized = server.normalize_watch_stock({"code": "3110", "name": "日東紡", "priority": "urgent!!"})
        self.assertIsNone(normalized["priority"])

    def test_non_list_tags_ignored(self):
        normalized = server.normalize_watch_stock({"code": "3110", "name": "日東紡", "tags": "半導体"})
        self.assertEqual(normalized["tags"], [])


class SmartImportConfirmDispatchTests(unittest.TestCase):
    """smart_import_confirm()がWATCH_STOCKをupsert_watch_stock_candidate()へ正しく振り分けること。"""

    def test_dispatches_to_upsert_watch_stock_candidate(self):
        candidates = [{"category": "WATCH_STOCK", "confidence": "HIGH", "draft": _six_stocks_sample(),
                        "raw_text": "{}"}]
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.upsert_watch_stock_candidate.return_value = {"created": True, "code": "3110", "market": "JP"}
            result = server.smart_import_confirm("postgres://x", "local", candidates)
        mock_db.upsert_watch_stock_candidate.assert_called_once()
        self.assertEqual(result["results"]["WATCH_STOCK"]["imported"], 1)
        detail = result["results"]["WATCH_STOCK"]["details"][0]
        self.assertTrue(detail["ok"])
        self.assertEqual(detail["code"], "3110")

    def test_seven_candidates_all_saved(self):
        codes = ["6480", "4419", "4180", "3110", "146A", "9766", "6227"]
        candidates = [{"category": "WATCH_STOCK", "confidence": "HIGH",
                        "draft": {"code": c, "name": f"銘柄{c}", "theme": "テーマ"}, "raw_text": "{}"}
                       for c in codes]
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.upsert_watch_stock_candidate.return_value = {"created": True}
            result = server.smart_import_confirm("postgres://x", "local", candidates)
        self.assertEqual(result["results"]["WATCH_STOCK"]["imported"], 7)
        self.assertEqual(mock_db.upsert_watch_stock_candidate.call_count, 7)

    def test_invalid_draft_reports_failure_without_raising(self):
        candidates = [{"category": "WATCH_STOCK", "confidence": "HIGH", "draft": {"name": "コード無し"},
                        "raw_text": "{}"}]
        with mock.patch.object(server, "investment_db") as mock_db:
            result = server.smart_import_confirm("postgres://x", "local", candidates)
        mock_db.upsert_watch_stock_candidate.assert_not_called()
        detail = result["results"]["WATCH_STOCK"]["details"][0]
        self.assertFalse(detail["ok"])

    def test_low_confidence_without_force_is_rejected(self):
        candidates = [{"category": "WATCH_STOCK", "confidence": "LOW", "draft": {"name": "コード無し"},
                        "raw_text": "{}"}]
        with mock.patch.object(server, "investment_db") as mock_db:
            result = server.smart_import_confirm("postgres://x", "local", candidates)
        self.assertEqual(result["rejected_low_confidence"], 1)
        mock_db.upsert_watch_stock_candidate.assert_not_called()

    def test_db_exception_survives_and_reports_failure(self):
        candidates = [{"category": "WATCH_STOCK", "confidence": "HIGH", "draft": _six_stocks_sample(),
                        "raw_text": "{}"}]
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.upsert_watch_stock_candidate.side_effect = Exception("db down")
            result = server.smart_import_confirm("postgres://x", "local", candidates)
        detail = result["results"]["WATCH_STOCK"]["details"][0]
        self.assertFalse(detail["ok"])
        self.assertIn("db down", detail["reason"])


class CategoryRegistrationTests(unittest.TestCase):
    """UNKNOWN扱いにせずカテゴリ一覧・実装済み・安全集合へ正しく登録されていること。"""

    def test_category_listed(self):
        self.assertIn("WATCH_STOCK", server.SMART_IMPORT_CATEGORIES)

    def test_category_implemented(self):
        self.assertIn("WATCH_STOCK", server.SMART_IMPORT_IMPLEMENTED_CATEGORIES)

    def test_category_safe(self):
        self.assertIn("WATCH_STOCK", server.SMART_IMPORT_SAFE_CATEGORIES_BACKEND)

    def test_category_label_shown_in_html_not_unknown(self):
        with open("trade-cockpit.html", encoding="utf-8") as f:
            html = f.read()
        self.assertIn("WATCH_STOCK:{icon:", html)
        self.assertIn("監視候補", html)
        self.assertIn('"WATCH_STOCK"', html)  # SMART_IMPORT_IMPLEMENTED/SAFE_CATEGORIESへの登録


class ExistingCategoriesUnaffectedTests(unittest.TestCase):
    """STEP9-8：ニュース・イベント・有識者意見等、既存Smart Importカテゴリの判定を壊していないこと。"""

    def test_expert_opinion_still_classified(self):
        item = {"type": "expert_opinion", "expert_name": "アナリストA", "published_at": "2026-09-16"}
        category, _confidence, _draft = server._classify_json_item(item)
        self.assertEqual(category, "EXPERT_OPINION")

    def test_market_analysis_still_classified(self):
        item = {"type": "market_analysis", "summary": "地合いは中立"}
        category, _confidence, _draft = server._classify_json_item(item)
        self.assertEqual(category, "MARKET_ANALYSIS")

    def test_watchlist_update_still_classified(self):
        item = {"type": "watchlist_update", "ticker": "7203", "action": "ADD"}
        category, _confidence, _draft = server._classify_json_item(item)
        self.assertEqual(category, "WATCHLIST_UPDATE")

    def test_position_update_still_classified(self):
        item = {"type": "position_update", "ticker": "7203", "action": "OPEN"}
        category, _confidence, _draft = server._classify_json_item(item)
        self.assertEqual(category, "POSITION_UPDATE")


class WatchStockCandidateDbMergeTests(unittest.TestCase):
    """STEP6：investment_db.upsert_watch_stock_candidate()の重複防止・マージ・既存情報の
    非破壊を検証する。"""

    def test_new_candidate_created_with_watch_candidate_state(self):
        captured = {}

        class _Cursor:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, sql, params=None):
                captured["sql"], captured["params"] = sql, params
            def fetchone(self):
                return None  # 既存行なし

        class _Conn:
            def cursor(self, row_factory=None):
                return _Cursor()
            def execute(self, sql, params=None):
                captured["insert_sql"], captured["insert_params"] = sql, params
            def commit(self):
                pass

        class _Pool:
            def connection(self):
                class _Ctx:
                    def __enter__(self): return _Conn()
                    def __exit__(self, *a): return False
                return _Ctx()

        with mock.patch.object(investment_db, "_get_pool", return_value=_Pool()):
            result = investment_db.upsert_watch_stock_candidate(
                "dummy_url", "matsuura",
                {"code": "3110", "market": "JP", "name": "日東紡", "theme": "半導体",
                 "reason": "好調", "source": "四季報", "tags": ["半導体"], "priority": "high"})
        self.assertEqual(result["created"], True)
        self.assertIn("候補", captured["insert_params"])  # watch='候補'として保存される

    def test_existing_row_not_duplicated_and_merged_without_destroying_data(self):
        """既存監視銘柄に同じcodeがある場合、重複INSERTせず、既存のtheme/source/added_reasonへ
        新規値をマージする（破壊しない）。"""
        existing_row = {"code": "3110", "market": "JP", "name": "日東紡", "theme": "既存テーマ",
                          "added_reason": None, "source": "既存ソース", "tags": ["既存タグ"],
                          "watch": "優先"}  # ユーザーが既に優先へ昇格させている

        class _Cursor:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, sql, params=None): pass
            def fetchone(self):
                return dict(existing_row)

        captured = {}

        class _Conn:
            def cursor(self, row_factory=None):
                return _Cursor()
            def execute(self, sql, params=None):
                captured["sql"], captured["params"] = sql, params
            def commit(self):
                pass

        class _Pool:
            def connection(self):
                class _Ctx:
                    def __enter__(self): return _Conn()
                    def __exit__(self, *a): return False
                return _Ctx()

        with mock.patch.object(investment_db, "_get_pool", return_value=_Pool()):
            result = investment_db.upsert_watch_stock_candidate(
                "dummy_url", "matsuura",
                {"code": "3110", "market": "JP", "name": "日東紡", "theme": "新規テーマ",
                 "reason": "新規理由", "source": "新規ソース", "tags": ["新規タグ"], "priority": "medium"})
        self.assertEqual(result["created"], False)
        # watch='候補'へ書き換えられていない（優先のまま） = SQLに"候補"という値が含まれない
        self.assertNotIn("候補", (captured["params"] or []))
        sql = captured["sql"]
        self.assertNotIn(" watch ", sql.replace("=", " = "))  # watch列がUPDATE対象に含まれない

    def test_merge_text_field_appends_without_duplicating(self):
        self.assertEqual(investment_db._merge_text_field(None, "新規"), "新規")
        self.assertEqual(investment_db._merge_text_field("既存", None), "既存")
        self.assertEqual(investment_db._merge_text_field("既存", "新規"), "既存 / 新規")
        self.assertEqual(investment_db._merge_text_field("既存 / 新規", "新規"), "既存 / 新規")  # 重複しない

    def test_no_code_returns_false(self):
        with mock.patch.object(investment_db, "_get_pool", return_value=mock.Mock()):
            self.assertFalse(investment_db.upsert_watch_stock_candidate("dummy_url", "matsuura", {}))


if __name__ == "__main__":
    unittest.main()
