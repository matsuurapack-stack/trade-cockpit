# trade_reflections（2026-09-17新規、Event Risk Guard＋トレード反省メモ Phase A）テスト。
#
# 「今日の反省・気づき」の自由記述からのルールベース構造化（structure_trade_reflection）、
# Smart Import TRADE_REFLECTION型の分類・normalize、DB CRUD（本人専用）、
# find_similar_reflectionsの類似度閾値（レビュー指摘4：タグ1個一致だけではノイズ警告にしない）
# を検証する。
#
# 実行方法： cd files && python -m unittest test_trade_reflections -v

import unittest
from unittest import mock

import server
import investment_db


USER_2026_09_17_TEXT = (
    "FOMC後に下がると考えていたが、実際には上昇したことで地合いを楽観視した。"
    "翌日は日銀政策決定会合という重要イベント日で、寄り天になる可能性をもっと警戒すべきだった。"
    "イベントリスクが高い日に買う場合は、少し上昇した段階で利益保護を優先し、"
    "建値付近への逆指値引上げも検討する。"
)


class StructureTradeReflectionTests(unittest.TestCase):
    """PHASE 11：ルールベース構造化。完全な自然言語理解は目指さない前提での最小限の抽出。"""

    def test_extracts_expected_tags_from_2026_09_17_example(self):
        result = server.structure_trade_reflection(USER_2026_09_17_TEXT)
        for tag in ("FOMC", "BOJ", "OVERCONFIDENCE", "OPENING_FADE", "EVENT_RISK", "BREAKEVEN_STOP", "PROFIT_PROTECTION"):
            self.assertIn(tag, result["tags"], f"{tag} が抽出されていない: {result['tags']}")

    def test_event_types_subset_of_macro_tags(self):
        result = server.structure_trade_reflection(USER_2026_09_17_TEXT)
        self.assertIn("FOMC", result["event_types"])
        self.assertIn("BOJ", result["event_types"])
        self.assertNotIn("OVERCONFIDENCE", result["event_types"])  # マクロイベント種別ではない

    def test_category_includes_event(self):
        result = server.structure_trade_reflection(USER_2026_09_17_TEXT)
        self.assertIn("EVENT", result["category"])

    def test_lesson_extracted_from_should_have_sentence(self):
        result = server.structure_trade_reflection(USER_2026_09_17_TEXT)
        self.assertIsNotNone(result["lesson"])
        self.assertIn("警戒すべき", result["lesson"])

    def test_future_rule_extracted_from_action_sentence(self):
        result = server.structure_trade_reflection(USER_2026_09_17_TEXT)
        self.assertIsNotNone(result["future_rule"])
        self.assertIn("検討する", result["future_rule"])

    def test_no_keywords_gives_low_confidence(self):
        result = server.structure_trade_reflection("特に大きな学びはなかった一日でした。")
        self.assertEqual(result["confidence"], "LOW")
        self.assertEqual(result["tags"], [])

    def test_tags_present_gives_medium_confidence(self):
        result = server.structure_trade_reflection(USER_2026_09_17_TEXT)
        self.assertEqual(result["confidence"], "MEDIUM")

    def test_empty_text_does_not_crash(self):
        result = server.structure_trade_reflection("")
        self.assertEqual(result["tags"], [])
        self.assertEqual(result["category"], ["OTHER"])


class SmartImportTradeReflectionClassifyTests(unittest.TestCase):
    """Smart Import TRADE_REFLECTION型：既存WATCH_STOCKと同じ配線パターンの分類。"""

    def test_type_alias_classified_as_trade_reflection(self):
        item = {"type": "trade_reflection", "reflection_text": USER_2026_09_17_TEXT}
        category, confidence, draft = server._classify_json_item(item)
        self.assertEqual(category, "TRADE_REFLECTION")
        self.assertIn(confidence, ("MEDIUM", "HIGH"))

    def test_missing_reflection_text_is_low_confidence(self):
        item = {"type": "trade_reflection"}
        category, confidence, _draft = server._classify_json_item(item)
        self.assertEqual(category, "TRADE_REFLECTION")
        self.assertEqual(confidence, "LOW")

    def test_chatgpt_prestructured_json_is_high_confidence(self):
        item = {"type": "trade_reflection", "reflection_text": USER_2026_09_17_TEXT,
                "lesson": "直前イベント後の上昇だけで翌日のリスクを低く見積もらない。",
                "future_rule": "重要イベント当日は寄り直後WAIT。"}
        category, confidence, _draft = server._classify_json_item(item)
        self.assertEqual(category, "TRADE_REFLECTION")
        self.assertEqual(confidence, "HIGH")

    def test_trade_reflection_in_categories_and_implemented(self):
        self.assertIn("TRADE_REFLECTION", server.SMART_IMPORT_CATEGORIES)
        self.assertIn("TRADE_REFLECTION", server.SMART_IMPORT_IMPLEMENTED_CATEGORIES)
        self.assertIn("TRADE_REFLECTION", server.SMART_IMPORT_SAFE_CATEGORIES_BACKEND)

    def test_existing_categories_unaffected(self):
        item = {"type": "trade_rule", "rule_text": "寄り後30分は様子見する"}
        category, _confidence, _draft = server._classify_json_item(item)
        self.assertEqual(category, "TRADE_RULE")


class NormalizeTradeReflectionTests(unittest.TestCase):
    def test_missing_text_returns_none(self):
        self.assertIsNone(server.normalize_trade_reflection({}))

    def test_fills_missing_fields_via_rule_based_structuring(self):
        draft = {"reflection_text": USER_2026_09_17_TEXT}
        normalized = server.normalize_trade_reflection(draft, import_source="smart_import")
        self.assertIn("FOMC", normalized["tags"])
        self.assertIsNotNone(normalized["lesson"])
        self.assertIsNotNone(normalized["future_rule"])
        self.assertEqual(normalized["source"], "smart_import_chatgpt")

    def test_user_supplied_fields_take_precedence_over_rule_based(self):
        draft = {"reflection_text": USER_2026_09_17_TEXT,
                 "lesson": "ChatGPT生成のより詳細な教訓文",
                 "future_rule": "ChatGPT生成のより詳細なルール文",
                 "tags": ["CUSTOM_TAG"]}
        normalized = server.normalize_trade_reflection(draft, import_source="smart_import")
        self.assertEqual(normalized["lesson"], "ChatGPT生成のより詳細な教訓文")
        self.assertEqual(normalized["future_rule"], "ChatGPT生成のより詳細なルール文")
        self.assertEqual(normalized["tags"], ["CUSTOM_TAG"])


class _FakeCursor:
    def __init__(self, fetchone_result=None, fetchall_result=None, fetchone_results=None):
        # fetchone_results: 呼び出し順に消費するキュー（dedupチェック→INSERT、の2回呼び出しに対応）。
        # 指定が無ければfetchone_resultを毎回返す（既存テストとの後方互換）。
        self._fetchone_result = fetchone_result
        self._fetchone_queue = list(fetchone_results) if fetchone_results is not None else None
        self._fetchall_result = fetchall_result or []
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        if self._fetchone_queue is not None:
            return self._fetchone_queue.pop(0) if self._fetchone_queue else None
        return self._fetchone_result

    def fetchall(self):
        return self._fetchall_result


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.committed = False

    def cursor(self, row_factory=None):
        return self._cursor

    def commit(self):
        self.committed = True


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def connection(self):
        pool = self

        class _Ctx:
            def __enter__(self):
                return pool._conn

            def __exit__(self, *a):
                return False
        return _Ctx()


class CreateTradeReflectionDbTests(unittest.TestCase):
    """DB CRUD：本人専用scope・JSON列のシリアライズを検証する（trade_rules等と同じ書き味）。"""

    def test_create_uses_caller_user_id_private_and_serializes_json_cols(self):
        saved_row = {"id": 1, "trade_date": "2026-09-17", "reflection_text": USER_2026_09_17_TEXT,
                     "tags": ["FOMC", "BOJ"], "category": ["EVENT"], "event_types": ["FOMC", "BOJ"]}
        # fetchoneは2回呼ばれる：①重複チェックSELECT（既存なし=None）②INSERT RETURNINGの結果
        cursor = _FakeCursor(fetchone_results=[None, saved_row])
        conn = _FakeConn(cursor)
        pool = _FakePool(conn)
        with mock.patch.object(investment_db, "_get_pool", return_value=pool):
            result = investment_db.create_trade_reflection(
                "dummy_url", "matsuura",
                {"reflection_text": USER_2026_09_17_TEXT, "tags": ["FOMC", "BOJ"], "category": ["EVENT"]})
        self.assertIsNotNone(result)
        self.assertTrue(conn.committed)
        insert_sql, insert_params = cursor.executed[-1]  # 最後の実行文=INSERT（先頭は重複チェックSELECT）
        self.assertIn("INSERT INTO trade_reflections", insert_sql)
        self.assertEqual(insert_params[0], "matsuura")  # 2026-09-26 MU-Multi: 本人専用（呼び出し元user_idをそのまま使う）
        self.assertNotEqual(insert_params[0], investment_db._SHARED_SCOPE)

    def test_duplicate_same_date_and_text_returns_existing_without_insert(self):
        # 2026-09-17追記：同一trade_date＋完全一致reflection_textの再保存は新規行を作らない。
        existing_row = {"id": 1, "trade_date": "2026-09-17", "reflection_text": USER_2026_09_17_TEXT,
                         "tags": ["FOMC"], "category": ["EVENT"], "event_types": ["FOMC"]}
        cursor = _FakeCursor(fetchone_results=[existing_row])
        conn = _FakeConn(cursor)
        pool = _FakePool(conn)
        with mock.patch.object(investment_db, "_get_pool", return_value=pool):
            result = investment_db.create_trade_reflection(
                "dummy_url", "matsuura",
                {"trade_date": "2026-09-17", "reflection_text": USER_2026_09_17_TEXT})
        self.assertEqual(result["id"], 1)
        self.assertFalse(conn.committed)  # INSERTを実行していない＝コミットもしていない
        self.assertEqual(len(cursor.executed), 1)  # 重複チェックSELECTのみ、INSERTは実行されない
        self.assertNotIn("INSERT", cursor.executed[0][0])

    def test_create_without_reflection_text_returns_none(self):
        with mock.patch.object(investment_db, "_get_pool", return_value=_FakePool(_FakeConn(_FakeCursor()))):
            result = investment_db.create_trade_reflection("dummy_url", "matsuura", {})
        self.assertIsNone(result)


class FindSimilarReflectionsThresholdTests(unittest.TestCase):
    """レビュー指摘4：タグ1個一致だけでは「過去の反省あり」としない。
    (event_type一致 AND タグ1個以上追加一致) OR (タグ一致数2以上) を満たす行だけを返す。"""

    def _rows(self):
        return [
            {"id": 1, "event_types": ["FOMC"], "tags": ["FOMC", "OVERCONFIDENCE"]},  # event一致+タグ1個追加一致
            {"id": 2, "event_types": [], "tags": ["OVERCONFIDENCE", "OPENING_FADE"]},  # タグ2個一致（他candidateと）
            {"id": 3, "event_types": [], "tags": ["OVERCONFIDENCE"]},  # タグ1個のみ一致→ノイズ、返さない
            {"id": 4, "event_types": ["BOJ"], "tags": []},  # event一致のみ、タグ追加一致なし→返さない
        ]

    def _run(self, event_types, tags):
        cursor = _FakeCursor(fetchall_result=self._rows())
        pool = _FakePool(_FakeConn(cursor))
        with mock.patch.object(investment_db, "_get_pool", return_value=pool):
            return investment_db.find_similar_reflections("dummy_url", "matsuura", event_types=event_types, tags=tags)

    def test_single_tag_match_alone_is_excluded(self):
        results = self._run(event_types=[], tags=["OVERCONFIDENCE"])
        ids = [r["id"] for r in results]
        self.assertNotIn(3, ids)  # タグ1個だけの一致は除外

    def test_event_type_plus_one_extra_tag_is_included(self):
        results = self._run(event_types=["FOMC"], tags=["OVERCONFIDENCE"])
        ids = [r["id"] for r in results]
        self.assertIn(1, ids)  # event一致+タグ1個追加一致 → 含む

    def test_two_tag_overlap_is_included(self):
        results = self._run(event_types=[], tags=["OVERCONFIDENCE", "OPENING_FADE"])
        ids = [r["id"] for r in results]
        self.assertIn(2, ids)  # タグ2個一致 → 含む

    def test_event_type_alone_without_extra_tag_is_excluded(self):
        results = self._run(event_types=["BOJ"], tags=[])
        ids = [r["id"] for r in results]
        self.assertNotIn(4, ids)  # event一致のみ（タグ追加一致なし）→ 除外

    def test_no_query_input_returns_empty(self):
        results = self._run(event_types=[], tags=[])
        self.assertEqual(results, [])


class ExtractEventTypesFromTitlesTests(unittest.TestCase):
    """PHASE 12-13：本日のmarket_eventsタイトルからevent_typesを推定する（reflection
    構造化と同じキーワード辞書を再利用、二重語彙を作らない）。"""

    def test_boj_title_detected(self):
        types = server.extract_event_types_from_titles([{"title": "日銀政策決定会合"}])
        self.assertIn("BOJ", types)

    def test_fomc_title_detected(self):
        types = server.extract_event_types_from_titles([{"title": "FOMC結果発表"}])
        self.assertIn("FOMC", types)

    def test_no_match_returns_empty(self):
        types = server.extract_event_types_from_titles([{"title": "特に関係ないイベント"}])
        self.assertEqual(types, [])

    def test_empty_events_returns_empty(self):
        self.assertEqual(server.extract_event_types_from_titles([]), [])


class FindSimilarPastReflectionsForTodayTests(unittest.TestCase):
    def test_no_macro_events_skips_db_call(self):
        with mock.patch.object(investment_db, "find_similar_reflections") as mock_find:
            result = server.find_similar_past_reflections_for_today(
                "dummy_url", "matsuura", {"events": [{"title": "無関係"}]})
        mock_find.assert_not_called()
        self.assertEqual(result, [])

    def test_macro_event_triggers_search_with_event_risk_tag(self):
        with mock.patch.object(investment_db, "find_similar_reflections", return_value=[{"id": 9}]) as mock_find:
            result = server.find_similar_past_reflections_for_today(
                "dummy_url", "matsuura", {"events": [{"title": "日銀政策決定会合"}]})
        self.assertEqual(result, [{"id": 9}])
        _args, kwargs = mock_find.call_args
        self.assertIn("BOJ", kwargs["event_types"])
        self.assertIn("EVENT_RISK", kwargs["tags"])


if __name__ == "__main__":
    unittest.main()
