# 金融政策イベント統合（2026-09-16新規）：CATALYST→EVENT同期の回帰テスト。
#
# 実データ較正（news_catalysts中銀関連10件）を根拠にしたclassify_central_bank_event()の
# 分類ルール、_resolve_central_bank_event_datetime_for_promotion()の日時解決優先順位、
# promote_central_bank_catalysts_to_market_events()のgrouping/linking/idempotencyを検証する。
#
# 実行方法： cd files && python -m unittest test_central_bank_event_sync -v

import json
import unittest
from unittest import mock

import server
import investment_db


def _fake_pool_capturing(captured, fetchone_value=None):
    """execute()に渡されたparamsを記録するだけの最小フェイクpool（test_mu_s3b_shared_safe_
    tables.pyと同じパターン）。SHARED scope強制のE2Eで実際に踏んだバグ（user_id不一致で
    SELECTが常に0件になりFalseを返していた）の再発防止用。"""

    class _FakeCursor:
        def execute(self, sql, params=None):
            captured["sql"] = sql
            captured["params"] = params
            return self

        def fetchone(self):
            return fetchone_value

        def fetchall(self):
            return []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _FakeConn:
        def cursor(self, row_factory=None):
            return _FakeCursor()

        def commit(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _FakePool:
        def connection(self):
            return _FakeConn()

    return _FakePool()


class UpdateMarketEventCentralBankSyncSharedScopeTests(unittest.TestCase):
    """update_market_event_central_bank_sync()：market_eventsはSHARED化済みテーブルのため、
    呼び出し元のuser_idに関わらず実際にDBへ渡るuser_idは_shared固定であること。
    2026-09-16実データE2Eで実際に踏んだバグ（この強制が抜けていたためSELECTが常に0件になり、
    id=95のlink処理が毎回Falseで失敗していた）の再発防止テスト。"""

    def test_forces_shared_scope_and_finds_existing_row(self):
        captured = {}
        with mock.patch("investment_db._get_pool",
                         return_value=_fake_pool_capturing(captured, fetchone_value={"raw_payload": {}})):
            ok = investment_db.update_market_event_central_bank_sync(
                "dummy_url", "matsuura", 95, "BOJ_POLICY_DECISION", None, "DATE_ONLY", None,
                {"linked_from": "catalyst_sync"}, [44, 36])
        self.assertTrue(ok)
        self.assertIn(investment_db._SHARED_SCOPE, captured["params"])
        self.assertNotIn("matsuura", captured["params"])

    def test_returns_false_when_row_not_found(self):
        captured = {}
        with mock.patch("investment_db._get_pool",
                         return_value=_fake_pool_capturing(captured, fetchone_value=None)):
            ok = investment_db.update_market_event_central_bank_sync(
                "dummy_url", "matsuura", 999, "BOJ_POLICY_DECISION", None, "DATE_ONLY", None, {}, [1])
        self.assertFalse(ok)

    def test_merges_linked_catalyst_ids_with_existing(self):
        captured = {}
        with mock.patch("investment_db._get_pool",
                         return_value=_fake_pool_capturing(
                             captured, fetchone_value={"raw_payload": {"linked_catalyst_ids": [36]}})):
            investment_db.update_market_event_central_bank_sync(
                "dummy_url", "matsuura", 95, "BOJ_POLICY_DECISION", None, "DATE_ONLY", None, {}, [44])
        # UPDATE文のparamsにraw_payload(JSON文字列)が含まれ、その中でlinked_catalyst_idsが
        # 既存[36]と新規[44]のunion（[36,44]）になっていることを確認する。
        raw_payload_json = next(p for p in captured["params"] if isinstance(p, str) and "linked_catalyst_ids" in p)
        merged = json.loads(raw_payload_json)
        self.assertEqual(merged["linked_catalyst_ids"], [36, 44])


class ClassifyCentralBankEventRealDataTests(unittest.TestCase):
    """実DBの中銀関連カタリスト10件（2026-09-16較正）のtitleで分類結果を検証する。"""

    def test_fomc_policy_decision_variants(self):
        for title in ["FOMC 政策金利・声明・SEP・ドットチャート", "FOMC結果"]:
            with self.subTest(title=title):
                key, canon, country = server.classify_central_bank_event(title)
                self.assertEqual(key, "FOMC_POLICY_DECISION")
                self.assertEqual(country, "US")

    def test_fed_chair_press_conference_variants(self):
        for title in ["パウエルFRB議長 記者会見", "パウエルFRB議長 会見"]:
            with self.subTest(title=title):
                key, canon, country = server.classify_central_bank_event(title)
                self.assertEqual(key, "FED_CHAIR_PRESS_CONFERENCE")

    def test_boj_meeting_vs_policy_decision_distinction(self):
        """会合開催そのもの（BOJ_MEETING）と結果発表（BOJ_POLICY_DECISION）は別イベント。"""
        key1, _, _ = server.classify_central_bank_event("日銀金融政策決定会合")
        self.assertEqual(key1, "BOJ_MEETING")
        key2, _, _ = server.classify_central_bank_event("日銀金融政策決定会合 結果")
        self.assertEqual(key2, "BOJ_POLICY_DECISION")
        self.assertNotEqual(key1, key2)

    def test_boj_policy_decision_matches_legacy_title_without_kaigou(self):
        """既存market_events（id=95相当、"日銀金融政策決定"＝会合の文字が無い旧形式）も
        BOJ_POLICY_DECISIONとして識別できること（既存イベントとのlink判定に必須）。"""
        key, _, _ = server.classify_central_bank_event("日銀金融政策決定")
        self.assertEqual(key, "BOJ_POLICY_DECISION")

    def test_boj_governor_press_conference_variants(self):
        for title in ["植田日銀総裁 記者会見", "植田日銀総裁 会見"]:
            with self.subTest(title=title):
                key, _, _ = server.classify_central_bank_event(title)
                self.assertEqual(key, "BOJ_GOVERNOR_PRESS_CONFERENCE")

    def test_boe_policy_decision(self):
        key, _, country = server.classify_central_bank_event("BOE 政策金利")
        self.assertEqual(key, "BOE_POLICY_DECISION")
        self.assertEqual(country, "UK")

    def test_category_casing_does_not_affect_classification(self):
        """category列の"CENTRAL_BANK"/"central_bank"表記揺れが実データに存在したが、
        classify_central_bank_event()はcategoryを引数に取らずtitle/summaryのみで判定する
        ため、この表記揺れの影響を受けない設計であることを確認する。"""
        key_upper, _, _ = server.classify_central_bank_event("FOMC結果")
        self.assertEqual(key_upper, "FOMC_POLICY_DECISION")


class ClassifyCentralBankEventNonEventTests(unittest.TestCase):
    """NON_EVENT（解説記事等）が誤って昇格しないことを確認する（実データに実例が無かった
    ため合成データで検証、指示書の完了条件5番に対応）。"""

    def test_commentary_about_fomc_reaction_is_not_an_event(self):
        key, _, _ = server.classify_central_bank_event("FOMC後の米国株の反応を解説")
        self.assertIsNone(key)

    def test_generic_boj_policy_commentary_is_not_an_event(self):
        key, _, _ = server.classify_central_bank_event("日銀の金融政策の方向性について考察")
        self.assertIsNone(key)

    def test_org_keyword_alone_without_occurrence_type_is_not_an_event(self):
        key, _, _ = server.classify_central_bank_event("FOMCが今週の主役")
        self.assertIsNone(key)

    def test_empty_text_is_not_an_event(self):
        key, _, _ = server.classify_central_bank_event("", "")
        self.assertIsNone(key)

    def test_unrelated_title_is_not_an_event(self):
        key, _, _ = server.classify_central_bank_event("トヨタ自動車 決算発表")
        self.assertIsNone(key)


class ResolveCentralBankDatetimeTests(unittest.TestCase):
    """_resolve_central_bank_event_datetime_for_promotion()：日時解決の優先順位（① event_date
    列 → ② raw_payload.raw_text明示 → ③ 相対日付テキスト → ④ DATE_UNKNOWN）を検証する。"""

    def _raw_payload(self, **kwargs):
        return {"raw_text": json.dumps(kwargs, ensure_ascii=False)}

    def test_real_fomc_example_exact_time(self):
        """実データid=41相当：date/timeが明示、03:00 JST → EXACT。"""
        cat = {"title": "FOMC 政策金利・声明・SEP・ドットチャート", "catalyst_date": "2026-09-16",
               "event_date": None,
               "raw_payload": self._raw_payload(date="2026-09-17", time="03:00", country="US")}
        r = server._resolve_central_bank_event_datetime_for_promotion(cat)
        self.assertEqual(r["event_date"], "2026-09-17")
        self.assertEqual(r["event_time_jst"], "03:00")
        self.assertEqual(r["time_precision"], "EXACT")
        self.assertEqual(r["date_source"], "RAW_PAYLOAD_EXPLICIT")
        self.assertEqual(r["timezone_source"], "SMART_IMPORT_ASSUMED_JST")

    def test_real_boj_meeting_example_with_end_date(self):
        """実データid=43相当：2日間の会合、end_dateを保持。"""
        cat = {"title": "日銀金融政策決定会合", "catalyst_date": "2026-09-16", "event_date": None,
               "raw_payload": self._raw_payload(date="2026-09-17", end_date="2026-09-18", country="JP")}
        r = server._resolve_central_bank_event_datetime_for_promotion(cat)
        self.assertEqual(r["event_date"], "2026-09-17")
        self.assertEqual(r["end_date"], "2026-09-18")
        self.assertEqual(r["time_precision"], "DATE_ONLY")  # time未指定

    def test_daytime_placeholder_is_approximate_not_a_guessed_clock_time(self):
        """実データid=36相当：time="daytime"（非数値）は具体的な時刻へ変換しない。"""
        cat = {"title": "日銀金融政策決定会合 結果", "catalyst_date": "2026-09-14", "event_date": None,
               "raw_payload": self._raw_payload(date="2026-09-18", time="daytime", country="JP")}
        r = server._resolve_central_bank_event_datetime_for_promotion(cat)
        self.assertEqual(r["event_date"], "2026-09-18")
        self.assertIsNone(r["event_time_jst"])
        self.assertEqual(r["time_precision"], "APPROXIMATE")
        self.assertIsNone(r["timezone_source"])

    def test_null_time_is_date_only(self):
        cat = {"title": "日銀金融政策決定会合 結果", "catalyst_date": "2026-09-16", "event_date": None,
               "raw_payload": self._raw_payload(date="2026-09-18", time=None, country="JP")}
        r = server._resolve_central_bank_event_datetime_for_promotion(cat)
        self.assertEqual(r["time_precision"], "DATE_ONLY")

    def test_explicit_event_date_column_takes_priority_over_raw_payload(self):
        cat = {"title": "FOMC結果", "catalyst_date": "2026-09-14", "event_date": "2026-09-20",
               "raw_payload": self._raw_payload(date="2026-09-17", time="03:00")}
        r = server._resolve_central_bank_event_datetime_for_promotion(cat)
        self.assertEqual(r["event_date"], "2026-09-20")  # event_date列を優先
        self.assertEqual(r["date_source"], "EVENT_DATE_FIELD")
        self.assertEqual(r["event_time_jst"], "03:00")  # 時刻は列に無いのでraw_payloadから補完

    def test_relative_text_resolved_against_catalyst_date_not_today(self):
        """優先順位3：本文の相対日付をcatalyst_date（確実な基準日）で解決する。
        datetime.date.today()（テスト実行時の実日付）に依存しないことを確認する。"""
        cat = {"title": "明日FOMC政策決定", "catalyst_date": "2026-09-16", "event_date": None,
               "raw_payload": {}}
        r = server._resolve_central_bank_event_datetime_for_promotion(cat)
        self.assertEqual(r["event_date"], "2026-09-17")
        self.assertEqual(r["date_source"], "RELATIVE_TEXT")

    def test_catalyst_date_alone_is_never_used_as_event_date(self):
        """重要：catalyst_date単独をevent_dateとして採用しない（指示書の明示的な禁止事項）。
        本文に日付情報が一切無い場合はDATE_UNKNOWNとしてNoneを返す。"""
        cat = {"title": "FOMC政策決定", "catalyst_date": "2026-09-16", "event_date": None,
               "raw_payload": {}}
        r = server._resolve_central_bank_event_datetime_for_promotion(cat)
        self.assertIsNone(r)

    def test_no_date_anywhere_returns_none(self):
        cat = {"title": "FOMC結果", "catalyst_date": None, "event_date": None, "raw_payload": {}}
        r = server._resolve_central_bank_event_datetime_for_promotion(cat)
        self.assertIsNone(r)

    def test_unparseable_raw_text_falls_back_gracefully(self):
        cat = {"title": "FOMC結果", "catalyst_date": "2026-09-14", "event_date": None,
               "raw_payload": {"raw_text": "not valid json{{{"}}
        r = server._resolve_central_bank_event_datetime_for_promotion(cat)
        self.assertIsNone(r)


class PromoteCentralBankCatalystsTests(unittest.TestCase):
    """promote_central_bank_catalysts_to_market_events()：grouping・既存イベントとのlink・
    AMBIGUOUS判定・idempotencyを検証する（investment_dbをモック）。"""

    def _catalyst(self, id_, title, event_date_payload, catalyst_date="2026-09-16", importance="critical",
                   information_kind=None):
        payload = {"raw_text": json.dumps(event_date_payload, ensure_ascii=False)}
        if information_kind:
            payload["information_kind"] = information_kind
        return {"id": id_, "title": title, "catalyst_date": catalyst_date, "event_date": None,
                "importance": importance, "summary": None, "raw_payload": payload}

    def test_duplicate_catalysts_for_same_event_create_only_one_market_event(self):
        """実データの核心ケース：catalyst 29(FOMC結果)+41(FOMC 政策金利・声明…)は同一FOMC決定。
        2件のcatalystから1件のmarket_eventだけが作られ、linked_catalyst_idsに両方のidが入る。"""
        catalysts = [
            self._catalyst(41, "FOMC 政策金利・声明・SEP・ドットチャート",
                            {"date": "2026-09-17", "time": "03:00"}),
            self._catalyst(29, "FOMC結果", {"date": "2026-09-17", "time": "03:00"}),
        ]
        with mock.patch.object(server.investment_db, "list_market_events", return_value=[]), \
             mock.patch.object(server.investment_db, "import_market_events",
                                return_value={"imported": 1, "updated": 0}) as mock_import:
            result = server.promote_central_bank_catalysts_to_market_events("db", "user", catalysts)
        self.assertEqual(result["promoted_new"], 1)
        self.assertEqual(result["linked_existing"], 0)
        mock_import.assert_called_once()
        created_event = mock_import.call_args[0][2][0]
        self.assertEqual(created_event["canonical_event_key"], "FOMC_POLICY_DECISION")
        self.assertEqual(sorted(created_event["raw_payload"]["linked_catalyst_ids"]), [29, 41])
        self.assertEqual(created_event["event_time_jst"], "03:00")

    def test_links_to_existing_legacy_event_instead_of_duplicating(self):
        """実データの核心ケース：既存market_events（id=95相当、canonical_event_key無し、
        title="日銀金融政策決定"）にBOJ_POLICY_DECISIONカタリストをリンクし、新規作成しない。"""
        catalysts = [self._catalyst(44, "日銀金融政策決定会合 結果", {"date": "2026-09-18", "time": None})]
        existing = [{"id": 95, "event_date": "2026-09-18", "title": "日銀金融政策決定",
                     "event_type": "OTHER", "canonical_event_key": None}]
        with mock.patch.object(server.investment_db, "list_market_events", return_value=existing), \
             mock.patch.object(server.investment_db, "import_market_events") as mock_import, \
             mock.patch.object(server.investment_db, "update_market_event_central_bank_sync",
                                return_value=True) as mock_update:
            result = server.promote_central_bank_catalysts_to_market_events("db", "user", catalysts)
        self.assertEqual(result["linked_existing"], 1)
        self.assertEqual(result["promoted_new"], 0)
        mock_import.assert_not_called()  # 二重化させない＝新規INSERT経路を一切呼ばない
        mock_update.assert_called_once()
        call_args = mock_update.call_args[0]
        self.assertEqual(call_args[2], 95)  # event_id
        self.assertEqual(call_args[3], "BOJ_POLICY_DECISION")  # canonical_event_key
        call_kwargs_linked_ids = mock_update.call_args[0][-1]
        self.assertEqual(call_kwargs_linked_ids, [44])

    def test_second_sync_run_is_idempotent_no_new_event_created(self):
        """再同期を2回行ってもmarket_events件数が増えないことを確認する。1回目でlinked_
        catalyst_idsが設定された前提で2回目を実行し、new作成が発生しないことを見る。"""
        catalyst = self._catalyst(44, "日銀金融政策決定会合 結果", {"date": "2026-09-18", "time": None})
        # 1回目のsyncで既にcanonical_event_keyが付与された状態をシミュレート。
        existing_after_first_run = [{"id": 95, "event_date": "2026-09-18", "title": "日銀金融政策決定",
                                       "event_type": "CENTRAL_BANK", "canonical_event_key": "BOJ_POLICY_DECISION"}]
        with mock.patch.object(server.investment_db, "list_market_events", return_value=existing_after_first_run), \
             mock.patch.object(server.investment_db, "import_market_events") as mock_import, \
             mock.patch.object(server.investment_db, "update_market_event_central_bank_sync",
                                return_value=True) as mock_update:
            result = server.promote_central_bank_catalysts_to_market_events("db", "user", [catalyst])
        self.assertEqual(result["promoted_new"], 0)
        self.assertEqual(result["linked_existing"], 1)
        mock_import.assert_not_called()

    def test_ambiguous_multiple_candidates_not_auto_merged(self):
        catalysts = [self._catalyst(50, "FOMC結果", {"date": "2026-09-17", "time": "03:00"})]
        existing = [
            {"id": 1, "event_date": "2026-09-17", "title": "FOMC政策金利発表", "canonical_event_key": "FOMC_POLICY_DECISION"},
            {"id": 2, "event_date": "2026-09-18", "title": "FOMC結果まとめ", "canonical_event_key": "FOMC_POLICY_DECISION"},
        ]
        with mock.patch.object(server.investment_db, "list_market_events", return_value=existing), \
             mock.patch.object(server.investment_db, "import_market_events") as mock_import, \
             mock.patch.object(server.investment_db, "update_market_event_central_bank_sync") as mock_update:
            result = server.promote_central_bank_catalysts_to_market_events("db", "user", catalysts)
        self.assertEqual(result["ambiguous"], 1)
        mock_import.assert_not_called()
        mock_update.assert_not_called()

    def test_non_event_catalyst_is_skipped(self):
        catalysts = [self._catalyst(60, "トヨタ自動車 決算発表", {"date": "2026-09-17"})]
        with mock.patch.object(server.investment_db, "list_market_events", return_value=[]), \
             mock.patch.object(server.investment_db, "import_market_events") as mock_import:
            result = server.promote_central_bank_catalysts_to_market_events("db", "user", catalysts)
        self.assertEqual(result["skipped_non_event"], 1)
        mock_import.assert_not_called()

    def test_market_analysis_source_is_excluded_even_if_keywords_match(self):
        """MARKET_ANALYSIS由来（information_kind=ANALYSIS）は、内容がFOMC結果に言及していても
        意見・分析であり実イベントの事実ではないため対象外にする。"""
        catalysts = [self._catalyst(70, "FOMC結果を受けた株式市場への影響分析",
                                     {"date": "2026-09-17"}, information_kind="ANALYSIS")]
        with mock.patch.object(server.investment_db, "list_market_events", return_value=[]), \
             mock.patch.object(server.investment_db, "import_market_events") as mock_import:
            result = server.promote_central_bank_catalysts_to_market_events("db", "user", catalysts)
        self.assertEqual(result["skipped_non_event"], 1)
        mock_import.assert_not_called()

    def test_date_unknown_catalyst_is_not_promoted(self):
        catalyst = {"id": 80, "title": "FOMC結果", "catalyst_date": None, "event_date": None,
                     "importance": "critical", "summary": None, "raw_payload": {}}
        with mock.patch.object(server.investment_db, "list_market_events", return_value=[]), \
             mock.patch.object(server.investment_db, "import_market_events") as mock_import:
            result = server.promote_central_bank_catalysts_to_market_events("db", "user", [catalyst])
        self.assertEqual(result["skipped_date_unknown"], 1)
        mock_import.assert_not_called()

    def test_empty_catalyst_list_returns_zeroed_result_without_db_calls(self):
        with mock.patch.object(server.investment_db, "list_market_events") as mock_list:
            result = server.promote_central_bank_catalysts_to_market_events("db", "user", [])
        mock_list.assert_not_called()
        self.assertEqual(result["processed"], 0)

    def test_no_database_url_returns_zeroed_result(self):
        result = server.promote_central_bank_catalysts_to_market_events(None, "user", [{"title": "x"}])
        self.assertEqual(result["processed"], 0)


class NormalizeCatalystEventDateTests(unittest.TestCase):
    """normalize_catalyst()：新規Smart Importでdraftのdate/event_dateがnews_catalysts.
    event_date列へ保存されるようになったことを確認する（バックフィルではなく将来データの
    構造化保持、指示書「新規データ = 最初から構造化フィールドを保持」）。"""

    def test_date_key_is_promoted_to_event_date(self):
        draft = {"title": "FOMC 政策金利・声明", "category": "CENTRAL_BANK", "date": "2026-09-17", "time": "03:00"}
        out = server.normalize_catalyst(draft, raw_text="{}", import_source="manual")
        self.assertEqual(out["event_date"], "2026-09-17")

    def test_explicit_event_date_key_is_preserved(self):
        draft = {"title": "FOMC結果", "category": "CENTRAL_BANK", "event_date": "2026-09-20"}
        out = server.normalize_catalyst(draft, raw_text="{}", import_source="manual")
        self.assertEqual(out["event_date"], "2026-09-20")

    def test_no_date_key_leaves_event_date_unset(self):
        draft = {"title": "何らかの解説記事", "category": "MACRO"}
        out = server.normalize_catalyst(draft, raw_text="{}", import_source="manual")
        self.assertNotIn("event_date", out)

    def test_catalyst_date_is_not_used_as_event_date_source(self):
        """draftにcatalyst_dateキーがあっても、それをevent_dateへ流用しないことを確認する
        （catalyst_dateは「取込日」、event_dateは「開催日」で意味が異なる、指示書の禁止事項）。"""
        draft = {"title": "FOMC結果", "category": "CENTRAL_BANK", "catalyst_date": "2026-09-14"}
        out = server.normalize_catalyst(draft, raw_text="{}", import_source="manual")
        self.assertNotIn("event_date", out)

    def test_raw_payload_still_preserves_full_original_draft_for_backfill(self):
        """raw_payload.raw_textには元の完全なJSONが引き続き渡ることを確認する
        （event_date抽出後もbackfill経路が壊れていないこと）。"""
        draft = {"title": "FOMC結果", "category": "CENTRAL_BANK", "date": "2026-09-17", "time": "03:00"}
        raw_text_str = json.dumps(draft, ensure_ascii=False)
        out = server.normalize_catalyst(draft, raw_text=raw_text_str, import_source="manual")
        self.assertEqual(out["raw_payload"]["raw_text"], raw_text_str)


if __name__ == "__main__":
    unittest.main()
