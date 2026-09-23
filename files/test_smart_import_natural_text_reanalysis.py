# Smart Import 入力原本の永続化（smart_import_sources）と汎用再解析のテスト（2026-09-24）。
# 設計原則：入力原本（自然文・JSON）は解析結果とは別のsmart_import_sourcesに保存し、UI用
# raw_text（短縮プレビュー）と分離する。解析結果(EVENT等)は原本を source_hash/source_id で参照する
# だけで、特定の解析結果行が原文を抱える構造は持たない。
# 実行方法： cd files && python -m unittest test_smart_import_natural_text_reanalysis -v

import json
import unittest
from unittest import mock

import server
import investment_db

FILLER = "本日の相場は方向感に乏しく参加者は様子見姿勢でした。" * 120  # 3000文字超・イベント語なし


def _long_text():
    return FILLER + "\n2026年10月30日 FOMC結果発表\n" + "以上です。"


class _FakeStore:
    """market_events と smart_import_sources の最小インメモリ模倣。"""

    def __init__(self):
        self.rows = {}      # (event_date,title) -> row
        self.sources = {}   # (user_id, source_hash) -> row
        self.tombstones = set()  # (event_date, title)
        self.writes = 0
        self._id = 0
        self._sid = 0

    # --- market_events ---
    def list_market_events(self, database_url, user_id, from_date=None, to_date=None, limit=200):
        return [dict(r) for (d, t), r in self.rows.items()
                if not (from_date and d < from_date) and not (to_date and d > to_date)]

    def import_market_events(self, database_url, user_id, events):
        imported = updated = 0
        for ev in events:
            key = (str(ev["event_date"])[:10], ev["title"])
            self.writes += 1
            self.tombstones.discard(key)  # 明示import＝残したい意思、tombstone解除（実装と同じ）
            if key in self.rows:
                updated += 1
                self.rows[key].update(ev)
            else:
                imported += 1
                self._id += 1
                self.rows[key] = {**ev, "id": self._id}
        return {"imported": imported, "updated": updated, "skipped": 0, "errors": 0}

    # --- smart_import_sources ---
    def save_smart_import_source(self, database_url, user_id, source_hash, input_type, original_text,
                                 original_payload=None, wrapper_context=None, source_type=None):
        key = (user_id, source_hash)
        if key in self.sources:
            self.sources[key]["import_count"] += 1
            return self.sources[key]["id"]
        self._sid += 1
        self.sources[key] = {"id": self._sid, "user_id": user_id, "source_hash": source_hash,
                             "input_type": input_type, "original_text": original_text,
                             "original_payload": original_payload, "wrapper_context": wrapper_context,
                             "source_type": source_type, "import_count": 1, "status": "PREVIEW",
                             "confirmed_at": None}
        return self._sid

    def mark_smart_import_sources_confirmed(self, database_url, user_id, source_hashes):
        n = 0
        for h in source_hashes or []:
            src = self.sources.get((user_id, h))
            if src and src["status"] != "CONFIRMED":
                src["status"] = "CONFIRMED"
                src["confirmed_at"] = "now"
                n += 1
        return n

    def delete_market_event(self, database_url, user_id, event_id, user_initiated=False, reason=None):
        for key, r in list(self.rows.items()):
            if r["id"] == event_id:
                if user_initiated:
                    self.tombstones.add(key)
                del self.rows[key]

    def list_event_tombstones(self, database_url, start_date=None, end_date=None):
        return [{"event_date": d, "title": t} for (d, t) in self.tombstones
                if not (start_date and d < start_date) and not (end_date and d > end_date)]

    def list_smart_import_sources(self, database_url, user_id=None, limit=1000):
        return [dict(r) for (u, _h), r in self.sources.items() if user_id is None or u == user_id]

    def patches(self):
        return [mock.patch.object(investment_db, name, side_effect=getattr(self, name))
                for name in ("list_market_events", "import_market_events", "save_smart_import_source",
                             "list_smart_import_sources", "mark_smart_import_sources_confirmed",
                             "delete_market_event", "list_event_tombstones")]


class _StoreCase(unittest.TestCase):
    def setUp(self):
        self.store = _FakeStore()
        for p in self.store.patches():
            p.start()
            self.addCleanup(p.stop)

    def save(self, text, user="u", only_events=False):
        cands = server.classify_content(text, "db", user)
        if only_events:
            cands = [c for c in cands if c["category"] == "EVENT"]
        server.smart_import_confirm("db", user, cands, import_source="manual")
        return cands

    def reanalyze(self, fn=None, **kw):
        fn = fn or server.reanalyze_smart_import_events
        return fn("db", "u", **kw)


class SourcePersistenceTests(_StoreCase):
    def test_source_saved_at_classify_time_full_text_and_candidates_light(self):  # TEST1/7
        text = _long_text()
        cands = server.classify_content(text, "db", "u")
        src = self.store.sources[("u", server._compute_source_hash(text))]
        self.assertEqual(src["original_text"], text)  # 確定前でもDBに全文
        for c in cands:
            self.assertEqual(c["source_id"], src["id"])
            self.assertLessEqual(len(c["raw_text"]), 800)
            self.assertNotIn("original_text", c)
            if c["category"] != "MARKET_ANALYSIS":  # narrativeのdraft.summaryは既存設計
                self.assertLess(len(json.dumps(c, ensure_ascii=False)), 1500)

    def test_event_rows_reference_source_and_do_not_carry_text(self):
        self.save(_long_text(), only_events=True)
        for row in self.store.rows.values():
            pl = row["raw_payload"]
            self.assertIn("source_hash", pl)
            self.assertIn("source_id", pl)
            self.assertNotIn("original_text", pl)  # carrier行は存在しない
            self.assertNotIn("original_text_missing", pl)

    def test_same_text_twice_does_not_duplicate_source(self):  # TEST E
        text = _long_text()
        server.classify_content(text, "db", "u")
        server.classify_content(text, "db", "u")
        self.assertEqual(len(self.store.sources), 1)
        self.assertEqual(next(iter(self.store.sources.values()))["import_count"], 2)
        server.classify_content(text, "db", "other")  # 別ユーザーは別原本
        self.assertEqual(len(self.store.sources), 2)

    def test_cache_cleared_before_confirm_still_full_text_in_db(self):  # TEST C/D（再起動相当）
        text = _long_text()
        cands = server.classify_content(text, "db", "u")
        server._SMART_IMPORT_TEXT_CACHE.clear()  # プロセス再起動相当
        server.smart_import_confirm("db", "u", [c for c in cands if c["category"] == "EVENT"])
        pl = next(iter(self.store.rows.values()))["raw_payload"]
        self.assertNotIn("original_text_missing", pl)
        self.assertEqual(self.store.sources[("u", pl["source_hash"])]["original_text"], text)
        self.assertTrue(self.store.sources[("u", pl["source_hash"])]["original_text"].endswith("以上です。"))

    def test_reanalysis_without_cache(self):  # TEST C
        self.save(_long_text(), only_events=True)
        server._SMART_IMPORT_TEXT_CACHE.clear()
        res = self.reanalyze(dry_run=True)
        self.assertEqual(res["unchanged"], 1)
        self.assertEqual(res["failed"], 0)

    def test_db_failure_falls_back_to_memory_then_persists_at_confirm(self):
        text = _long_text()
        with mock.patch.object(investment_db, "save_smart_import_source", side_effect=RuntimeError("db down")):
            cands = server.classify_content(text, "db", "u")
        self.assertTrue(all(c["source_id"] is None for c in cands))
        server.smart_import_confirm("db", "u", [c for c in cands if c["category"] == "EVENT"])
        pl = next(iter(self.store.rows.values()))["raw_payload"]
        self.assertIn("source_id", pl)  # confirm時にキャッシュから原本を再保存
        self.assertEqual(self.store.sources[("u", pl["source_hash"])]["original_text"], text)


class ReanalysisFromSourceTests(_StoreCase):
    def test_deleted_carrier_equivalent_row_restored(self):  # TEST A
        text = "2026年10月30日 FOMC結果発表\n2026年11月2日 日銀会合結果\n" + FILLER
        self.save(text, only_events=True)
        first_key = min(self.store.rows)
        del self.store.rows[first_key]  # 旧仕様でcarrierだった最初の行を削除
        res = self.reanalyze(dry_run=False)
        self.assertEqual(res["created"], 1)
        self.assertIn(first_key, self.store.rows)
        self.assertEqual(len(self.store.rows), 2)

    def test_all_event_rows_deleted_restored_from_source_only(self):  # TEST B
        text = "2026年10月30日 FOMC結果発表\n2026年11月2日 日銀会合結果\n" + FILLER
        self.save(text, only_events=True)
        self.store.rows.clear()
        dry = self.reanalyze(fn=server.reanalyze_smart_import_sources, dry_run=True)
        self.assertEqual(dry["created"], 2)
        self.assertEqual(len(self.store.rows), 0)
        res = self.reanalyze(fn=server.reanalyze_smart_import_sources, dry_run=False)
        self.assertEqual(res["created"], 2)
        self.assertEqual(len(self.store.rows), 2)
        for row in self.store.rows.values():
            self.assertIn("source_hash", row["raw_payload"])

    def test_dry_run_writes_nothing(self):  # TEST3
        self.save(_long_text(), only_events=True)
        key = next(iter(self.store.rows))
        self.store.rows[key]["importance"] = "LOW"
        w = self.store.writes
        res = self.reanalyze(dry_run=True)
        self.assertEqual(self.store.writes, w)
        self.assertEqual(res["updated"], 1)
        self.assertEqual(self.store.rows[key]["importance"], "LOW")

    def test_second_reanalysis_unchanged_no_writes(self):  # TEST4
        self.save(_long_text(), only_events=True)
        self.store.rows.clear()
        self.reanalyze(dry_run=False)
        rows, writes = len(self.store.rows), self.store.writes
        second = self.reanalyze(dry_run=False)
        self.assertEqual(second["created"] + second["updated"], 0)
        self.assertEqual(second["unchanged"], rows)
        self.assertEqual(self.store.writes, writes)
        self.assertEqual(len(self.store.rows), rows)

    def test_legacy_rows_without_any_original_skipped(self):  # TEST5
        self.store.rows[("2026-09-01", "旧")] = {"id": 1, "event_date": "2026-09-01", "title": "旧",
                                                   "raw_payload": {"smart_import": True, "raw_text": "旧500文字"}}
        res = self.reanalyze(dry_run=False)
        self.assertEqual(res["skipped_no_original_payload"], 1)
        self.assertEqual(res["created"] + res["updated"], 0)
        self.assertEqual(self.store.writes, 0)

    def test_legacy_carrier_rows_still_supported(self):
        text = "2026年10月30日 FOMC結果発表\n" + FILLER
        h = server._compute_source_hash(text)
        self.store.rows[("2026-10-30", "FOMC結果発表")] = {
            "id": 1, "event_date": "2026-10-30", "title": "FOMC結果発表",
            "raw_payload": {"smart_import": True, "source_input_hash": h, "original_text": text}}
        res = self.reanalyze(dry_run=True)
        self.assertEqual(res["skipped_no_original_payload"], 0)
        self.assertEqual(res["reanalyzed"], 1)

    def test_category_and_date_range_filters(self):  # TEST6
        self.save("2026年10月30日 FOMC結果発表\n2026年11月20日 日銀会合結果\n" + FILLER, only_events=True)
        for r in self.store.rows.values():
            r["importance"] = "LOW"
        res = self.reanalyze(dry_run=False, start_date="2026-10-01", end_date="2026-10-31")
        self.assertEqual(res["updated"], 1)
        nov = next(r for k, r in self.store.rows.items() if k[0] == "2026-11-20")
        self.assertEqual(nov["importance"], "LOW")
        w = self.store.writes
        res2 = self.reanalyze(dry_run=False, category="CATALYST")
        self.assertEqual(res2["skipped_unsupported_category"], 1)
        self.assertEqual(self.store.writes, w)

    def test_user_edited_fields_preserved(self):
        self.save(_long_text(), only_events=True)
        key = next(iter(self.store.rows))
        self.store.rows[key].update({"verification_status": "VERIFIED", "notes": "手修正", "importance": "LOW"})
        self.reanalyze(dry_run=False)
        self.assertEqual(self.store.rows[key]["verification_status"], "VERIFIED")
        self.assertEqual(self.store.rows[key]["notes"], "手修正")

    def test_errors_capped(self):
        for i in range(30):
            self.store.save_smart_import_source("db", "u", f"h{i}", "TEXT", f"t{i}")
        with mock.patch.object(server, "_classify_content_impl", side_effect=RuntimeError("boom")):
            res = self.reanalyze(dry_run=True, max_errors=5)
        self.assertEqual(res["failed"], 30)
        self.assertEqual(len(res["errors"]), 5)


class TombstoneTests(_StoreCase):
    """ユーザー明示削除のtombstone。内部deleteでは作らず、再解析で復元できる。"""

    def _seed(self):
        text = "2026年10月30日 FOMC結果発表\n2026年11月2日 日銀会合結果\n" + FILLER
        self.save(text, only_events=True)
        return min(self.store.rows)  # 10/30のFOMC

    def _delete(self, key, user_initiated):
        eid = self.store.rows[key]["id"]
        investment_db.delete_market_event("db", "u", eid, user_initiated=user_initiated)

    def test_user_delete_creates_tombstone_and_reanalysis_does_not_revive(self):  # 1-6
        key = self._seed()
        self.assertEqual(len(self.store.rows), 2)
        self._delete(key, user_initiated=True)
        self.assertIn(key, self.store.tombstones)  # tombstone確認
        dry = self.reanalyze(fn=server.reanalyze_smart_import_sources, dry_run=True)
        self.assertEqual(dry["skipped_user_deleted"], 1)
        self.assertEqual(dry["created"], 0)
        res = self.reanalyze(fn=server.reanalyze_smart_import_sources, dry_run=False)
        self.assertNotIn(key, self.store.rows)  # 復活しない
        self.assertEqual(res["skipped_user_deleted"], 1)
        self.assertEqual(res["created"], 0)
        self.assertEqual(len(self.store.rows), 1)
        res_ev = self.reanalyze(dry_run=False)  # events版でも同じ
        self.assertEqual(res_ev["skipped_user_deleted"], 1)
        self.assertNotIn(key, self.store.rows)

    def test_internal_delete_makes_no_tombstone_and_is_restored(self):  # 7
        key = self._seed()
        self._delete(key, user_initiated=False)
        self.assertEqual(self.store.tombstones, set())
        res = self.reanalyze(fn=server.reanalyze_smart_import_sources, dry_run=False)
        self.assertEqual(res["created"], 1)
        self.assertEqual(res["skipped_user_deleted"], 0)
        self.assertIn(key, self.store.rows)

    def test_all_events_deleted_internally_still_restored(self):
        self._seed()
        self.store.rows.clear()
        res = self.reanalyze(fn=server.reanalyze_smart_import_sources, dry_run=False)
        self.assertEqual(res["created"], 2)

    def test_reimport_clears_tombstone(self):
        key = self._seed()
        self._delete(key, user_initiated=True)
        self.assertIn(key, self.store.tombstones)
        self.save("2026年10月30日 FOMC結果発表\n", only_events=True)  # ユーザーが明示的に再登録
        self.assertNotIn(key, self.store.tombstones)
        self.assertIn(key, self.store.rows)

    def test_tombstone_respects_date_filter_and_protection_rules_intact(self):
        key = self._seed()
        self._delete(key, user_initiated=True)
        other = max(self.store.rows)
        self.store.rows[other].update({"verification_status": "VERIFIED", "importance": "LOW"})
        res = self.reanalyze(dry_run=False, start_date="2026-11-01", end_date="2026-11-30")
        self.assertEqual(res["skipped_user_deleted"], 0)  # 範囲外のtombstoneは対象外
        self.assertEqual(res["updated"], 1)
        self.assertEqual(self.store.rows[other]["verification_status"], "VERIFIED")
        second = self.reanalyze(dry_run=False, start_date="2026-11-01", end_date="2026-11-30")
        self.assertEqual(second["updated"], 0)  # idempotent

    def test_tombstone_load_failure_is_fail_closed(self):
        key = self._seed()
        self._delete(key, user_initiated=True)  # ユーザーが明示削除
        self.store.rows.clear()  # EVENTは全て無い状態
        writes = self.store.writes
        with mock.patch.object(investment_db, "list_event_tombstones", side_effect=RuntimeError("db down")):
            for fn in (server.reanalyze_smart_import_sources, server.reanalyze_smart_import_events):
                for dry in (True, False):
                    res = self.reanalyze(fn=fn, dry_run=dry)
                    self.assertEqual(res["created"], 0)  # dry_runでもcreated予定にしない
                    self.assertEqual(res["updated"], 0)
                    self.assertEqual(res["reanalyzed"], 0)
                    self.assertEqual(res["failed"], 1)   # 1 source
                    self.assertEqual(res["errors"][0]["reason"], "failed to load event tombstones")
                    self.assertIn("source_id", res["errors"][0])
        self.assertEqual(self.store.writes, writes)
        self.assertEqual(len(self.store.rows), 0)  # 削除済みEVENTは復活しない

    def test_tombstone_failure_does_not_block_normal_import(self):
        with mock.patch.object(investment_db, "list_event_tombstones", side_effect=RuntimeError("db down")):
            self.save(_long_text(), only_events=True)  # 通常のSmart Import確定は影響を受けない
        self.assertEqual(len(self.store.rows), 1)


class SourceStatusTests(_StoreCase):
    def _src(self, text):
        return self.store.sources[("u", server._compute_source_hash(text))]

    def test_preview_then_confirm_becomes_confirmed(self):  # C
        text = _long_text()
        cands = server.classify_content(text, "db", "u")
        self.assertEqual(self._src(text)["status"], "PREVIEW")
        server.smart_import_confirm("db", "u", [c for c in cands if c["category"] == "EVENT"])
        self.assertEqual(self._src(text)["status"], "CONFIRMED")
        self.assertIsNotNone(self._src(text)["confirmed_at"])

    def test_nothing_saved_stays_preview(self):
        text = _long_text()
        cands = server.classify_content(text, "db", "u")
        server.smart_import_confirm("db", "u", [])  # 何も選択せず確定
        self.assertEqual(self._src(text)["status"], "PREVIEW")

    def test_reimport_keeps_confirmed_and_only_bumps_count(self):  # D
        text = _long_text()
        cands = server.classify_content(text, "db", "u")
        server.smart_import_confirm("db", "u", [c for c in cands if c["category"] == "EVENT"])
        server.classify_content(text, "db", "u")
        self.assertEqual(len(self.store.sources), 1)
        self.assertEqual(self._src(text)["import_count"], 2)
        self.assertEqual(self._src(text)["status"], "CONFIRMED")  # PREVIEWに戻らない

    def test_repeated_confirm_is_idempotent(self):
        text = _long_text()
        cands = server.classify_content(text, "db", "u")
        ev = [c for c in cands if c["category"] == "EVENT"]
        server.smart_import_confirm("db", "u", ev)
        first = self._src(text)["confirmed_at"]
        server.smart_import_confirm("db", "u", ev)
        self.assertEqual(self._src(text)["confirmed_at"], first)


class JsonSourceTests(_StoreCase):
    def test_long_json_source_reanalysis(self):  # TEST F
        payload = {"type": "market_event_calendar", "category": "MACRO_EVENT", "title": "長い週間", "summary": "あ" * 300,
                   "key_events": [{"date": f"2026-10-{i % 28 + 1:02d}", "country": "US",
                                    "event": f"テスト指標{i}（PMI）", "importance": "HIGH"} for i in range(40)]}
        text = json.dumps(payload, ensure_ascii=False)
        self.assertGreater(len(text), 500)
        self.save(text)
        src = next(iter(self.store.sources.values()))
        self.assertEqual(src["input_type"], "JSON")
        self.assertEqual(src["original_text"], text)
        self.assertEqual(src["original_payload"], payload)  # 最後のkey_eventまで
        self.assertEqual(src["wrapper_context"]["title"], "長い週間")
        n = len(self.store.rows)
        last = max(self.store.rows)
        del self.store.rows[last]
        self.store.rows.clear()
        res = self.reanalyze(fn=server.reanalyze_smart_import_sources, dry_run=False)
        self.assertEqual(len(self.store.rows), n)
        self.assertIn(payload["key_events"][-1]["event"], {t for (_d, t) in self.store.rows})
        again = self.reanalyze(dry_run=False)
        self.assertEqual(again["created"] + again["updated"], 0)


if __name__ == "__main__":
    unittest.main()
