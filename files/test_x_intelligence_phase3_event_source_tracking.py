# X Intelligence Phase3（2026-09-15新規）：X由来イベントのsource追跡テスト。
#
# 方針（指示書）：
#  1. 既存のmarket_events互換性を壊さない
#  2. raw_payloadは残す
#  3. 専用列追加は最小限にする（source_handle/source_post_id/source_post_url/
#     source_published_atの4列。source_typeは既存列を再利用）
#  4. X由来でない既存イベントはNULL許容
#  5. 一般テキストSmart Importや手動イベント登録も壊さない
#  6. 同一イベントの重複判定にsource_post_idを使う場合、既存dedupeロジックとの競合を確認
#
# 実SQLのCASE/COALESCE分岐（_upsert_market_event_conn）は実DBでのみ検証可能なため、
# ここではPython側の純粋ロジック（_normalize_market_event昇格・_merge_market_event_
# additional_sources）と、タイトル生成の後方互換をカバーする。実DB E2Eは別途実施。
#
# 実行方法： cd files && python -m unittest test_x_intelligence_phase3_event_source_tracking -v

import unittest

import server
import investment_db


class NormalizeMarketEventSourcePromotionTests(unittest.TestCase):
    """_normalize_market_event()がraw_payloadから専用列へ昇格させること（指示書2・3番）。"""

    def test_promotes_source_fields_from_raw_payload(self):
        ev = {
            "event_date": "2026-09-17", "title": "FOMC結果発表（X投稿より検出）",
            "raw_payload": {"source_handle": "nicosokufx", "source_post_id": "111",
                             "source_post_url": "https://x.com/nicosokufx/status/111",
                             "published_at": "2026-09-15"},
        }
        norm = investment_db._normalize_market_event(ev)
        self.assertEqual(norm["source_handle"], "nicosokufx")
        self.assertEqual(norm["source_post_id"], "111")
        self.assertEqual(norm["source_post_url"], "https://x.com/nicosokufx/status/111")
        self.assertEqual(norm["source_published_at"], "2026-09-15")

    def test_explicit_top_level_value_not_overwritten_by_raw_payload(self):
        ev = {"event_date": "2026-09-17", "title": "t", "source_handle": "explicit_handle",
              "raw_payload": {"source_handle": "from_payload"}}
        norm = investment_db._normalize_market_event(ev)
        self.assertEqual(norm["source_handle"], "explicit_handle")

    def test_non_x_event_has_no_source_fields(self):
        # 指示書4番：X由来でない既存イベント・一般テキスト/手動登録はNULL許容
        # （キー自体が付与されないことを確認）。
        ev = {"event_date": "2026-09-17", "title": "通常のイベント", "notes": "メモ"}
        norm = investment_db._normalize_market_event(ev)
        self.assertNotIn("source_handle", norm)
        self.assertNotIn("source_post_id", norm)
        self.assertNotIn("source_post_url", norm)
        self.assertNotIn("source_published_at", norm)

    def test_raw_payload_not_dict_does_not_crash(self):
        # raw_payloadが無い/不正な形でも例外にならないこと（後方互換）。
        ev = {"event_date": "2026-09-17", "title": "t", "raw_payload": "not-a-dict"}
        norm = investment_db._normalize_market_event(ev)  # 例外を送出しないことが確認事項
        self.assertNotIn("source_handle", norm)


class MergeAdditionalSourcesTests(unittest.TestCase):
    """_merge_market_event_additional_sources()：複数source保持のコア関数。"""

    def test_first_additional_source_appended(self):
        existing_payload = {"confidence": "LOW", "source_handle": "nicosokufx"}
        incoming = {"source_handle": "nikkei", "source_post_id": "222", "source_post_url": "u2",
                    "source_type": "X_POST"}
        merged = investment_db._merge_market_event_additional_sources(existing_payload, incoming)
        self.assertEqual(merged["additional_sources"], [incoming])
        self.assertEqual(merged["confidence"], "LOW")  # 既存の他フィールドは保持

    def test_second_additional_source_accumulates(self):
        existing_payload = {"additional_sources": [{"source_handle": "nikkei", "source_post_id": "222"}]}
        incoming = {"source_handle": "reutersjapan", "source_post_id": "333"}
        merged = investment_db._merge_market_event_additional_sources(existing_payload, incoming)
        self.assertEqual(len(merged["additional_sources"]), 2)
        self.assertEqual(merged["additional_sources"][1]["source_handle"], "reutersjapan")

    def test_same_post_id_not_duplicated(self):
        # 指示書「同一投稿を2回Smart Importしても重複しない」の一部：
        # additional_sources側でも同一post_idの再追加を防ぐ。
        existing_payload = {"additional_sources": [{"source_handle": "nikkei", "source_post_id": "222"}]}
        incoming = {"source_handle": "nikkei", "source_post_id": "222"}
        merged = investment_db._merge_market_event_additional_sources(existing_payload, incoming)
        self.assertEqual(len(merged["additional_sources"]), 1)

    def test_none_existing_payload_handled(self):
        merged = investment_db._merge_market_event_additional_sources(None, {"source_post_id": "1"})
        self.assertEqual(len(merged["additional_sources"]), 1)


class TitleGenerationBackwardCompatTests(unittest.TestCase):
    """タイトル生成の後方互換：発信者名を埋め込まない汎用サフィックスへ変更したことで、
    複数アカウントが同一内容を検出した場合に同じタイトルになる（自然キー一致→統合可能）。"""

    def test_different_handles_same_text_produce_same_title(self):
        posted_date = __import__("datetime").date(2026, 9, 15)
        drafts1 = server._detect_events_from_social_text("9/17 FOMC結果発表", posted_date, source_handle="nicosokufx")
        drafts2 = server._detect_events_from_social_text("9/17 FOMC結果発表", posted_date, source_handle="nikkei")
        self.assertEqual(drafts1[0]["title"], drafts2[0]["title"])
        self.assertEqual(drafts1[0]["event_date"], drafts2[0]["event_date"])
        # source側は別アカウントとして区別されている
        self.assertEqual(drafts1[0]["raw_payload"]["source_handle"], "nicosokufx")
        self.assertEqual(drafts2[0]["raw_payload"]["source_handle"], "nikkei")

    def test_title_no_longer_embeds_display_name(self):
        posted_date = __import__("datetime").date(2026, 9, 15)
        drafts = server._detect_events_from_social_text("9/18 日銀会合", posted_date, source_handle="nicosokufx")
        self.assertNotIn("にこそく", drafts[0]["title"])
        self.assertIn("（X投稿より検出）", drafts[0]["title"])


class FilterFreshEventDraftsTests(unittest.TestCase):
    """_filter_fresh_event_drafts()：SmartImport層の重複チェックがDB側の複数source統合と
    競合していた実データE2Eで発見した不具合の回帰テスト（指示書6番）。"""

    def test_different_source_post_id_same_date_title_is_still_fresh(self):
        # 別アカウントが同一(date,title)の同じイベントを投稿した場合、SmartImport層で
        # 弾かれず、import_market_eventsへ送られること（DB側の複数source統合が発動できる）。
        existing = [{"event_date": "2026-09-17", "title": "FOMC結果発表（X投稿より検出）",
                     "source_post_id": "111"}]
        drafts = [{"event_date": "2026-09-17", "title": "FOMC結果発表（X投稿より検出）",
                   "raw_payload": {"source_post_id": "222"}}]
        fresh = server._filter_fresh_event_drafts(drafts, existing)
        self.assertEqual(len(fresh), 1)

    def test_same_source_post_id_is_not_fresh(self):
        # 同一投稿の再Smart Importは従来通り送信不要（無駄な書き込みをしない）。
        existing = [{"event_date": "2026-09-17", "title": "FOMC結果発表（X投稿より検出）",
                     "source_post_id": "111"}]
        drafts = [{"event_date": "2026-09-17", "title": "FOMC結果発表（X投稿より検出）",
                   "raw_payload": {"source_post_id": "111"}}]
        fresh = server._filter_fresh_event_drafts(drafts, existing)
        self.assertEqual(len(fresh), 0)

    def test_non_x_draft_uses_date_title_only_unchanged_behavior(self):
        # source_post_idが無い（一般テキスト/手動由来）draftは従来通りdate+titleのみで判定。
        existing = [{"event_date": "2026-09-20", "title": "決算発表", "source_post_id": None}]
        drafts = [{"event_date": "2026-09-20", "title": "決算発表", "raw_payload": {}}]
        fresh = server._filter_fresh_event_drafts(drafts, existing)
        self.assertEqual(len(fresh), 0)

    def test_genuinely_new_event_is_fresh(self):
        existing = []
        drafts = [{"event_date": "2026-09-25", "title": "CPI発表", "raw_payload": {"source_post_id": "999"}}]
        fresh = server._filter_fresh_event_drafts(drafts, existing)
        self.assertEqual(len(fresh), 1)


class LegacySourceColumnStaysConsistentTests(unittest.TestCase):
    """実データE2Eで発見した不具合の回帰テスト：複数source統合時、専用列source_handleは
    既存を維持するのに旧来のsource列（自由記述）だけEXCLUDEDへ上書きされ、両者が食い違って
    見えていた。ソースコードレベルでは_upsert_market_event_conn内の_first_source_wins_cols
    に"source"が含まれることを確認する（実際のSQL実行結果はE2Eで検証済み）。"""

    def test_source_included_in_first_source_wins_handling(self):
        # 関数のソースを直接検査するのではなく、実際に呼び出して确認するのが理想だが、
        # SQL実行を伴うため、ここでは定数レベルの構成確認に留める（実DB検証は別途実施）。
        import inspect
        src = inspect.getsource(investment_db._upsert_market_event_conn)
        self.assertIn('"source_type", "source"', src.replace("'", '"'))


class MarketEventColsIncludeSourceTrackingTests(unittest.TestCase):
    """_MARKET_EVENT_COLSに新4列が追加されていること（指示書3番：最小限の専用列）。"""

    def test_source_tracking_cols_present(self):
        for c in ("source_handle", "source_post_id", "source_post_url", "source_published_at"):
            self.assertIn(c, investment_db._MARKET_EVENT_COLS)

    def test_source_tracking_cols_not_jsonb(self):
        # TEXT/TIMESTAMPTZ列であり、JSONB castは不要。
        for c in ("source_handle", "source_post_id", "source_post_url", "source_published_at"):
            self.assertNotIn(c, investment_db._MARKET_EVENT_JSONB_COLS)


if __name__ == "__main__":
    unittest.main()
