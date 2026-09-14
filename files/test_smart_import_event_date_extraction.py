# Smart Import「イベント抽出→DB保存→イベント画面」不具合の回帰テスト（2026-09-14）。
#
# 症状：FOMC・日銀会合等を日付付きでSmart Importしても、抽出されたEVENT候補の
# draft["event_date"]が常にNoneになっており、investment_db.import_market_events()側の
# 必須チェック（event_date/titleが無い行はMISSING_EVENT_DATEでスキップ）に毎回引っかかり、
# イベント画面が0件になっていた。
#
# 原因：_classify_text_chunk()のEVENTブランチは、_SMART_IMPORT_DATE_PATTERNSで
# 「日付らしき表現があるか」（has_date、confidence判定用）を見るだけで、実際の日付値を
# event_dateへ変換していなかった（常にNone固定）。
#
# 修正：_extract_event_date_from_text()を追加し、本文中に明示された日付だけを
# ISO日付へ変換してevent_dateへ入れる（幅のある表現「今週」「来月」等は変換不能として
# 従来通りNoneのまま＝値を推測して埋めない）。
#
# 実行方法： cd files && python -m unittest test_smart_import_event_date_extraction -v

import datetime
import unittest
from unittest import mock

import server


def _today():
    return server._jst_today_date_str()


class ExtractEventDateFromTextTests(unittest.TestCase):
    """_extract_event_date_from_text()単体の日付変換テスト。"""

    def test_full_date_with_year(self):
        d = server._extract_event_date_from_text("2027年1月20日にFOMC結果発表")
        self.assertEqual(d, "2027-01-20")

    def test_month_day_without_year_assumes_current_year(self):
        today = datetime.date.fromisoformat(_today())
        d = server._extract_event_date_from_text("9月17日は日銀会合", today_str=_today())
        self.assertEqual(d, datetime.date(today.year, 9, 17).isoformat())

    def test_slash_date_assumes_current_year(self):
        today = datetime.date.fromisoformat(_today())
        d = server._extract_event_date_from_text("9/17 FOMC結果", today_str=_today())
        self.assertEqual(d, datetime.date(today.year, 9, 17).isoformat())

    def test_relative_today(self):
        today_str = _today()
        d = server._extract_event_date_from_text("今日FOMC結果発表", today_str=today_str)
        self.assertEqual(d, today_str)

    def test_relative_tomorrow(self):
        today = datetime.date.fromisoformat(_today())
        d = server._extract_event_date_from_text("明日は日銀会合", today_str=_today())
        self.assertEqual(d, (today + datetime.timedelta(days=1)).isoformat())

    def test_relative_day_after_tomorrow(self):
        today = datetime.date.fromisoformat(_today())
        d = server._extract_event_date_from_text("明後日CPI発表", today_str=_today())
        self.assertEqual(d, (today + datetime.timedelta(days=2)).isoformat())

    def test_vague_range_expression_returns_none(self):
        # 「来週」「今月」等は幅がある表現のため、日付を推測せずNoneのまま。
        self.assertIsNone(server._extract_event_date_from_text("来週FOMCがある"))
        self.assertIsNone(server._extract_event_date_from_text("今月中に日銀会合"))

    def test_no_date_at_all_returns_none(self):
        self.assertIsNone(server._extract_event_date_from_text("FOMCが注目される"))

    def test_invalid_date_falls_back_to_none(self):
        # 2/30のような実在しない日付は無視してNone（例外にしない）。
        self.assertIsNone(server._extract_event_date_from_text("2月30日にFOMC"))


class ClassifyTextChunkEventBranchTests(unittest.TestCase):
    """_classify_text_chunk()のEVENTブランチがevent_dateを実際に埋めること。"""

    def test_event_with_slash_date_gets_resolved_event_date(self):
        today = datetime.date.fromisoformat(_today())
        category, confidence, draft = server._classify_text_chunk("9/17 FOMCの結果発表に注目。")
        self.assertEqual(category, "EVENT")
        self.assertEqual(confidence, "MEDIUM")
        self.assertEqual(draft["event_date"], datetime.date(today.year, 9, 17).isoformat())

    def test_event_with_kanji_date_gets_resolved_event_date(self):
        category, confidence, draft = server._classify_text_chunk("9月20日に日銀会合が開催される。")
        self.assertEqual(category, "EVENT")
        self.assertEqual(confidence, "MEDIUM")
        self.assertIsNotNone(draft["event_date"])

    def test_event_without_date_stays_low_confidence_and_none(self):
        category, confidence, draft = server._classify_text_chunk("FOMCが今後の焦点になる。")
        self.assertEqual(category, "EVENT")
        self.assertEqual(confidence, "LOW")
        self.assertIsNone(draft["event_date"])

    def test_event_with_vague_date_keeps_medium_confidence_but_no_event_date(self):
        # has_date=True（「来週」がマッチ）だがevent_date自体は解決不能→Noneのまま。
        category, confidence, draft = server._classify_text_chunk("来週FOMCが開催予定。")
        self.assertEqual(category, "EVENT")
        self.assertEqual(confidence, "MEDIUM")
        self.assertIsNone(draft["event_date"])


class NormalizeEventPassesThroughResolvedDateTests(unittest.TestCase):
    """normalize_event()が_classify_text_chunkで解決済みのevent_dateをそのまま
    import_market_events()向けpayloadへ渡すこと。"""

    def test_normalize_event_keeps_resolved_event_date(self):
        _, _, draft = server._classify_text_chunk("9/17 FOMCの結果発表に注目。")
        normalized = server.normalize_event(draft, raw_text="9/17 FOMCの結果発表に注目。")
        self.assertIsNotNone(normalized["event_date"])
        self.assertEqual(normalized["title"], draft["title"])


class EndToEndSmartImportSavesEventTests(unittest.TestCase):
    """classify_content→smart_import_confirmの一連の流れで、日付付きEVENTが
    実際にimport_market_events()へ渡り、0件スキップされないことを確認する
    （DBアクセス自体はモックし、渡されたpayloadのevent_dateを検証する）。"""

    def test_event_with_date_reaches_import_market_events_with_event_date_set(self):
        text = "9/17 FOMCの結果発表に注目。日銀の追加利上げ観測も強まっている。"
        candidates = server.classify_content(text)
        event_candidates = [c for c in candidates if c["category"] == "EVENT"]
        self.assertTrue(event_candidates, "EVENTとして分類されていること")

        captured = {}

        def fake_import_market_events(database_url, user_id, events):
            captured["events"] = events
            return {"imported": len(events), "updated": 0, "skipped": 0, "errors": 0,
                    "skipped_details": [], "error_details": []}

        with mock.patch("investment_db.import_market_events", side_effect=fake_import_market_events):
            result = server.smart_import_confirm("dummy_url", "matsuura", candidates, "test")

        self.assertIn("EVENT", result["results"])
        self.assertEqual(result["results"]["EVENT"]["skipped"], 0)
        self.assertGreater(len(captured.get("events", [])), 0)
        for ev in captured["events"]:
            self.assertIsNotNone(ev.get("event_date"))

    def test_event_without_date_is_rejected_before_reaching_db_not_silently_lost(self):
        # 日付が全く無い場合はLOW confidenceのため保存されない（意図的な安全側動作）。
        # ここでは「rejected_low_confidence」として明示的にカウントされ、EVENTの
        # skipped扱いに紛れ込まないことを確認する（原因の切り分けやすさのため）。
        text = "FOMCが今後の焦点になる。"
        candidates = server.classify_content(text)
        event_candidates = [c for c in candidates if c["category"] == "EVENT"]
        self.assertTrue(event_candidates)
        self.assertEqual(event_candidates[0]["confidence"], "LOW")

        with mock.patch("investment_db.import_market_events") as fake_import:
            result = server.smart_import_confirm("dummy_url", "matsuura", candidates, "test")

        fake_import.assert_not_called()
        self.assertGreaterEqual(result["rejected_low_confidence"], 1)


if __name__ == "__main__":
    unittest.main()
