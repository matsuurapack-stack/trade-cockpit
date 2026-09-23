# Smart Import 元データ保全（original_payload）と再解析のテスト（2026-09-24）。
#
# 背景：raw_textが500文字に短縮されて保存されていたため、market_event_calendarの
# key_events後半が失われbackfill不能だった（news_catalysts.id=57）。表示用プレビュー
# （raw_text）と保存用original_payloadを分離し、既存raw_payload(JSONB)へ完全な元データを残す。
#
# 実行方法： cd files && python -m unittest test_smart_import_original_payload -v

import json
import unittest
from unittest import mock

import server
import investment_db


def _big_calendar(n=40):
    return {
        "type": "market_event_calendar", "category": "MACRO_EVENT", "title": "長い週間カレンダー",
        "summary": "あ" * 300,
        "key_events": [{"date": f"2026-10-{(i % 28) + 1:02d}", "country": "US",
                          "event": f"テスト指標{i}（PMI）", "importance": "HIGH"} for i in range(n)],
    }


class _NoSourceDbMixin:
    """原本テーブル(smart_import_sources)はDB無しのため、要素単位original_payload経路のみ検証する。"""

    def _patch_sources(self):
        for name, ret in (("list_smart_import_sources", []), ("save_smart_import_source", 1),
                          ("list_event_tombstones", []), ("mark_smart_import_sources_confirmed", 0)):
            p = mock.patch.object(investment_db, name, return_value=ret)
            p.start()
            self.addCleanup(p.stop)


class OriginalPayloadPreservedTests(_NoSourceDbMixin, unittest.TestCase):
    def setUp(self):
        self._patch_sources()
        self.payload = _big_calendar()
        self.text = json.dumps(self.payload, ensure_ascii=False)
        self.assertGreater(len(self.text), 500)  # TEST1: 500文字超

    def test_preview_truncated_but_original_full(self):
        cands = server.classify_content(self.text)
        self.assertEqual(len(cands), 40)
        for c in cands:
            self.assertLessEqual(len(c["raw_text"]), 500)  # 表示用は短縮のまま
        # 最後のkey_eventまで完全に保持
        self.assertEqual(cands[-1]["original_payload"], self.payload["key_events"][-1])
        self.assertEqual(cands[0]["wrapper_context"]["title"], "長い週間カレンダー")
        self.assertNotIn("key_events", cands[0]["wrapper_context"])  # 巨大配列は重複保持しない

    def test_saved_raw_payload_contains_original_and_preview_is_short(self):
        cands = server.classify_content(self.text)
        with mock.patch.object(investment_db, "import_market_events",
                                 return_value={"imported": 40, "updated": 0, "skipped": 0, "errors": 0}) as m:
            server.smart_import_confirm("x", "u", cands)
        saved = m.call_args[0][2]
        self.assertEqual(len(saved), 40)
        last = saved[-1]["raw_payload"]
        self.assertEqual(last["original_payload"], self.payload["key_events"][-1])
        self.assertLessEqual(len(last["raw_text"]), 500)
        self.assertEqual(last["wrapper_context"]["category"], "MACRO_EVENT")

    def test_api_preview_size_not_exploding(self):
        cands = server.classify_content(self.text)
        # 各候補は自分の要素分だけ（全体payloadの複製ではない）
        self.assertLess(max(len(json.dumps(c, ensure_ascii=False)) for c in cands), 1500)


class ReanalysisTests(_NoSourceDbMixin, unittest.TestCase):
    def setUp(self):
        self._patch_sources()

    def _saved_rows(self):
        payload = _big_calendar()
        text = json.dumps(payload, ensure_ascii=False)
        cands = server.classify_content(text)
        with mock.patch.object(investment_db, "import_market_events",
                                 return_value={"imported": 40}) as m:
            server.smart_import_confirm("x", "u", cands, import_source="manual")
        return payload, m.call_args[0][2]

    def test_reanalyze_from_saved_original_restores_late_events_without_duplicates(self):
        payload, saved = self._saved_rows()
        rows = [{"raw_payload": e["raw_payload"], "title": e["title"], "event_date": e["event_date"]} for e in saved]
        rows.append({"raw_payload": {"smart_import": True, "raw_text": "旧500文字"}, "title": "旧", "event_date": "2026-09-01"})
        with mock.patch.object(investment_db, "list_market_events", return_value=rows):
            dry = server.reanalyze_smart_import_events("x", "u", dry_run=True)
            self.assertEqual(dry["scanned"], 41)
            self.assertEqual(dry["skipped_no_original_payload"], 1)  # 旧データは推測復元しない
            with mock.patch.object(investment_db, "import_market_events",
                                     return_value={"imported": 0, "updated": 40}) as m:
                res = server.reanalyze_smart_import_events("x", "u", dry_run=False)
        events = m.call_args[0][2]
        titles = [e["title"] for e in events]
        self.assertIn(payload["key_events"][-1]["event"], titles)  # 後半イベント復元
        self.assertEqual(sorted(titles), sorted(e["title"] for e in saved))  # 増えない
        self.assertEqual(len(set((e["event_date"], e["title"]) for e in events)), 40 - 0
                          if len(set((e["event_date"], e["title"]) for e in saved)) == 40
                          else len(set((e["event_date"], e["title"]) for e in saved)))

    def test_dry_run_never_writes(self):
        _payload, saved = self._saved_rows()
        rows = [{"raw_payload": e["raw_payload"]} for e in saved]
        with mock.patch.object(investment_db, "list_market_events", return_value=rows), \
             mock.patch.object(investment_db, "import_market_events") as m:
            server.reanalyze_smart_import_events("x", "u", dry_run=True)
        m.assert_not_called()


if __name__ == "__main__":
    unittest.main()
