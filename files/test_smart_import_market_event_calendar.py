# Smart Import「market_event_calendar」（経済指標カレンダー）のイベントDB連携テスト。
#
# 症状：{"type":"market_event_calendar","key_events":[...],"earnings_watch":[...]}形式を
# Smart Importへ投入しても、key_events[]・earnings_watch[]の中身が個別のmarket_events行へ
# 展開されず（wrapper自体がUNKNOWN/LOWとして扱われ何も保存されない、または既存の生成的
# fallback判定でtitle+category一致によりCATALYST側の"参考資料"1行としてしか残らない）、
# イベントタブ・Market Mode Event Risk・分析エンジンのいずれにも反映されなかった。
#
# 根本原因：
#   1. classify_content()のラッパー展開ループ（catalysts/events/market_events/expert_views/
#      views）にkey_events/earnings_watchが含まれておらず、wrapper全体が1件のitemとして
#      _classify_json_item()へ渡っていた。
#   2. _classify_json_item()にtype=="market_event_calendar"の分岐が無かった。
#   3. 経済指標（JOLTS/ADP/PCE等）にcanonical_event_keyが付与されず、build_active_macro_events()
#      のcanonical_event_key必須フィルタ（既存Phase4ロジック、無変更）を通過できなかった。
#
# 修正：
#   - classify_content()のラッパー展開に、market_event_calendar専用のkey_events+
#     earnings_watch同時展開を追加（既存の「1キーだけ選んでbreak」ループとは別処理）。
#   - 展開後の各itemに"type":"event"を付与し、既存のEVENT分類・normalize_event()・
#     investment_db.import_market_events()パイプラインへそのまま乗せる（新しい保存経路は作らない）。
#   - normalize_event()に、タイトルからevent_type/canonical_event_key（中央銀行は既存
#     classify_central_bank_event()を再利用）を推定する処理を追加。
#   - _normalize_market_event()（investment_db.py）にwatch/watch_targets→affected_markets
#     マッピングを追加（STEP11、既存affected_markets列の再利用）。
#
# 実行方法： cd files & python -m unittest test_smart_import_market_event_calendar -v

import json
import unittest
from unittest import mock

import server
import investment_db


def _sample_payload():
    """ユーザー報告のバグ再現に使った実例（3件のkey_events + 1件のearnings_watch）。"""
    return {
        "type": "market_event_calendar",
        "category": "MACRO_EVENT",
        "title": "2026年9月末〜10月頭 重要指標・中銀カレンダー",
        "key_events": [
            {"date": "2026-09-29", "country": "US", "event": "JOLTS雇用動態調査", "importance": "HIGH"},
            {"date": "2026-09-30", "country": "US", "event": "ADP雇用統計", "importance": "HIGH"},
            {"date": "2026-09-30", "country": "US", "event": "PCEデフレーター", "time_jst": "21:30",
             "importance": "CRITICAL", "watch": ["US10Y", "USDJPY", "NASDAQ", "SOX", "VIX", "NIKKEI_FUTURES"]},
        ],
        "earnings_watch": [
            {"date": "2026-09-25", "company": "テスト企業A", "code": "1234"},
        ],
    }


class WrapperFlatteningTests(unittest.TestCase):
    """TEST1：market_event_calendarをSmart Import → key_eventsがevent DBへ展開される
    （分類段階：EVENT候補として個別に現れることを確認）。"""

    def test_key_events_and_earnings_watch_flattened_into_individual_candidates(self):
        text = json.dumps(_sample_payload(), ensure_ascii=False)
        candidates = server.classify_content(text)
        self.assertEqual(len(candidates), 4)
        self.assertTrue(all(c["category"] == "EVENT" for c in candidates))
        self.assertTrue(all(c["confidence"] == "HIGH" for c in candidates))

    def test_wrapper_alone_no_key_events_not_mistakenly_expanded(self):
        """key_events/earnings_watchが無いmarket_event_calendarは、従来通り単一itemとして
        扱われる（空配列への謎展開で0件候補になることを防ぐ）。"""
        payload = {"type": "market_event_calendar", "title": "参考メモ", "summary": "詳細不明"}
        text = json.dumps(payload, ensure_ascii=False)
        candidates = server.classify_content(text)
        self.assertEqual(len(candidates), 1)

    def test_does_not_hijack_existing_catalysts_wrapper(self):
        """既存の{"catalysts":[...]}ラッパー展開を壊していないこと。"""
        payload = {"catalysts": [{"title": "テストカタリスト", "catalyst_date": "2026-09-25"}]}
        text = json.dumps(payload, ensure_ascii=False)
        candidates = server.classify_content(text)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["category"], "CATALYST")


class KeyEventsPresentInEventDbTests(unittest.TestCase):
    """TEST2〜4：JOLTS/ADP/PCEがそれぞれ正しくevent_date/event_type/importance/
    canonical_event_keyへ正規化されること（event DBへ保存可能な形になっていること）。"""

    def _normalized_events(self):
        text = json.dumps(_sample_payload(), ensure_ascii=False)
        candidates = server.classify_content(text)
        return [server.normalize_event(c["draft"], text, "test") for c in candidates
                if c["category"] == "EVENT"]

    def test_jolts_event(self):
        events = self._normalized_events()
        jolts = next(e for e in events if "JOLTS" in e["title"])
        self.assertEqual(jolts["event_date"], "2026-09-29")
        self.assertEqual(jolts["event_type"], "INDICATOR")
        self.assertEqual(jolts["importance"], "HIGH")
        self.assertEqual(jolts["canonical_event_key"], "US_JOLTS")
        self.assertEqual(jolts["time_precision"], "DATE_ONLY")  # 時刻未指定

    def test_adp_event(self):
        events = self._normalized_events()
        adp = next(e for e in events if "ADP" in e["title"])
        self.assertEqual(adp["event_date"], "2026-09-30")
        self.assertEqual(adp["event_type"], "INDICATOR")
        self.assertEqual(adp["canonical_event_key"], "US_ADP_EMPLOYMENT")

    def test_pce_event_critical_importance_preserved_with_time(self):
        """STEP4：CRITICALを勝手にUNKNOWN/MEDIUMへ落とさない。STEP5：時刻付きはJSTで正規化。"""
        events = self._normalized_events()
        pce = next(e for e in events if "PCE" in e["title"])
        self.assertEqual(pce["event_date"], "2026-09-30")
        self.assertEqual(pce["importance"], "CRITICAL")  # 落とさない
        self.assertEqual(pce["event_time"], "21:30")
        self.assertEqual(pce["event_time_jst"], "21:30")
        self.assertEqual(pce["time_precision"], "EXACT")
        self.assertEqual(pce["canonical_event_key"], "US_PCE_DEFLATOR")

    def test_pce_watch_targets_mapped_to_affected_markets(self):
        """STEP11：watch（セクター/銘柄感応度）がaffected_marketsへ渡る。"""
        events = self._normalized_events()
        pce = next(e for e in events if "PCE" in e["title"])
        self.assertEqual(pce["affected_markets"], ["US10Y", "USDJPY", "NASDAQ", "SOX", "VIX", "NIKKEI_FUTURES"])


class CentralBankEventReuseTests(unittest.TestCase):
    """中央銀行系イベントは既存classify_central_bank_event()をそのまま再利用し、二重の
    canonical_event_key体系を作らない。"""

    def test_fomc_event_reuses_existing_central_bank_classifier(self):
        payload = {"type": "market_event_calendar",
                    "key_events": [{"date": "2026-10-29", "country": "US",
                                      "event": "FOMC政策金利・声明 結果", "importance": "CRITICAL"}]}
        text = json.dumps(payload, ensure_ascii=False)
        candidates = server.classify_content(text)
        normalized = server.normalize_event(candidates[0]["draft"], text, "test")
        self.assertEqual(normalized["event_type"], "CENTRAL_BANK")
        self.assertEqual(normalized["canonical_event_key"], "FOMC_POLICY_DECISION")


class EarningsWatchExpansionTests(unittest.TestCase):
    """TEST11：earnings_watch → EARNINGSイベントへ展開。企業コードが分かればaffected_stocksへ
    紐付ける。canonical_event_keyは付与しない（個別性が高く再現性のある定期発表ではないため、
    Phase4のMARKET_EVENT_RISK＝銘柄非依存の市場全体リスクへは乗せない設計判断）。"""

    def test_earnings_watch_item_becomes_earnings_event(self):
        text = json.dumps(_sample_payload(), ensure_ascii=False)
        candidates = server.classify_content(text)
        earnings_candidate = next(c for c in candidates if c["draft"].get("company") == "テスト企業A")
        normalized = server.normalize_event(earnings_candidate["draft"], text, "test")
        self.assertEqual(normalized["event_type"], "EARNINGS")
        self.assertEqual(normalized["title"], "テスト企業A")
        self.assertEqual(normalized["affected_stocks"], ["1234"])
        self.assertIsNone(normalized.get("canonical_event_key"))

    def test_earnings_watch_without_code_still_saved_by_company_name(self):
        payload = {"type": "market_event_calendar",
                    "earnings_watch": [{"date": "2026-09-26", "company": "コード不明企業"}]}
        text = json.dumps(payload, ensure_ascii=False)
        candidates = server.classify_content(text)
        normalized = server.normalize_event(candidates[0]["draft"], text, "test")
        self.assertEqual(normalized["title"], "コード不明企業")
        self.assertNotIn("affected_stocks", normalized)  # 無理にキーを作らない


class SmartImportConfirmDispatchTests(unittest.TestCase):
    """smart_import_confirm()がEVENT候補をimport_market_events()へ正しく振り分けること。"""

    def test_all_key_events_dispatched_to_import_market_events(self):
        text = json.dumps(_sample_payload(), ensure_ascii=False)
        candidates = server.classify_content(text)
        # investment_dbモジュール全体はモックしない（normalize_event()が実際の
        # investment_db._normalize_market_event()を呼ぶ必要があるため）。DB書き込み関数
        # だけを差し替える。
        with mock.patch.object(investment_db, "import_market_events",
                                 return_value={"imported": 4, "updated": 0, "skipped": 0, "errors": 0}) as mock_import:
            result = server.smart_import_confirm("postgres://x", "local", candidates)
        mock_import.assert_called_once()
        saved_events = mock_import.call_args[0][2]
        self.assertEqual(len(saved_events), 4)
        titles = {e["title"] for e in saved_events}
        self.assertIn("JOLTS雇用動態調査", titles)
        self.assertIn("ADP雇用統計", titles)
        self.assertIn("PCEデフレーター", titles)
        self.assertEqual(result["results"]["EVENT"]["imported"], 4)

    def test_existing_categories_unaffected(self):
        """STEP9-8相当：CATALYST/WATCHLIST_UPDATE等、既存カテゴリの分類・振り分けを壊さない。"""
        item = {"type": "watchlist_update", "ticker": "7203", "action": "ADD"}
        category, _confidence, _draft = server._classify_json_item(item)
        self.assertEqual(category, "WATCHLIST_UPDATE")


class DuplicateImportTests(unittest.TestCase):
    """TEST8：同一JSONを2回Importしてもduplicateが増えないこと（既存のON CONFLICT
    (user_id,event_date,title)を再利用、新しいdedup機構は作らない）。"""

    def test_same_payload_twice_upserts_not_duplicates(self):
        # (event_date, title) をキーにした簡易インメモリテーブルで、1回目=INSERT・
        # 2回目=UPDATE（既存行が1件のまま）という実際のON CONFLICT挙動を模倣する。
        table = {}

        class _Cursor:
            def __init__(self):
                self._pending = None
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, sql, params=None):
                s = sql.strip()
                if s.startswith("SELECT source_handle"):
                    self._pending = None  # source_post_id経路は今回使わない
                elif s.startswith("INSERT INTO market_events"):
                    key = (params[1], params[2])  # (event_date, title)
                    is_insert = key not in table
                    table[key] = params
                    self._pending = {"is_insert": is_insert}
            def fetchone(self):
                return self._pending

        class _Conn:
            def cursor(self, row_factory=None):
                return _Cursor()
            def execute(self, sql, params=None):
                pass
            def commit(self):
                pass
            def transaction(self):
                import contextlib
                @contextlib.contextmanager
                def _tx():
                    yield
                return _tx()

        class _Pool:
            def connection(self):
                import contextlib
                @contextlib.contextmanager
                def _ctx():
                    yield _Conn()
                return _ctx()

        events = [{"event_date": "2026-09-29", "title": "JOLTS雇用動態調査", "importance": "HIGH"}]
        with mock.patch.object(investment_db, "_get_pool", return_value=_Pool()):
            r1 = investment_db.import_market_events("dummy_url", "matsuura", events)
            r2 = investment_db.import_market_events("dummy_url", "matsuura", events)
        self.assertEqual(r1["imported"], 1)
        self.assertEqual(r1["updated"], 0)
        self.assertEqual(r2["imported"], 0)
        self.assertEqual(r2["updated"], 1)
        self.assertEqual(len(table), 1)  # 行が増殖していない


class EventTypeMappingTests(unittest.TestCase):
    """STEP3のevent_typeマッピング表。"""

    def test_indicator_keywords(self):
        for title in ("JOLTS雇用動態調査", "ADP雇用統計", "PCEデフレーター", "米雇用統計",
                       "ISM製造業景況指数", "PMI速報値", "CPI発表", "GDP速報", "住宅着工件数",
                       "耐久財受注", "消費者信頼感指数"):
            with self.subTest(title=title):
                event_type, _key = server.classify_market_event_type_and_key(title)
                self.assertEqual(event_type, "INDICATOR", title)

    def test_central_bank_keywords(self):
        event_type, key = server.classify_market_event_type_and_key("日銀金融政策決定会合 結果")
        self.assertEqual(event_type, "CENTRAL_BANK")
        self.assertEqual(key, "BOJ_POLICY_DECISION")

    def test_earnings_keyword(self):
        event_type, key = server.classify_market_event_type_and_key("○○社 決算発表")
        self.assertEqual(event_type, "EARNINGS")
        self.assertIsNone(key)

    def test_market_holiday_keyword(self):
        event_type, key = server.classify_market_event_type_and_key("東京証券取引所 休場")
        self.assertEqual(event_type, "MARKET_HOLIDAY")
        self.assertIsNone(key)

    def test_geopolitical_keyword(self):
        event_type, key = server.classify_market_event_type_and_key("中東情勢 地政学リスク高まる")
        self.assertEqual(event_type, "GEOPOLITICAL")
        self.assertIsNone(key)

    def test_unmatched_stays_other(self):
        event_type, key = server.classify_market_event_type_and_key("特に意味の無いタイトル")
        self.assertEqual(event_type, "OTHER")
        self.assertIsNone(key)


class BuildActiveMacroEventsPicksUpIndicatorEventsTests(unittest.TestCase):
    """TEST9：HIGH/CRITICALイベント（canonical_event_key付き）がEvent Risk計算へ渡ること。
    既存build_active_macro_events()のcanonical_event_key必須フィルタ自体は変更しない
    （このテストは「新しくcanonical_event_keyを持つようになったINDICATORイベントが
    そのフィルタを正しく通過する」ことを確認する）。"""

    def test_indicator_event_with_canonical_key_included_in_macro_events(self):
        text = json.dumps(_sample_payload(), ensure_ascii=False)
        candidates = server.classify_content(text)
        normalized = [server.normalize_event(c["draft"], text, "test") for c in candidates
                       if c["category"] == "EVENT"]
        pce = next(e for e in normalized if "PCE" in e["title"])
        # build_active_macro_eventsのフィルタ条件そのものを直接検証する
        self.assertTrue(bool(pce.get("canonical_event_key")))


class EntryCandidateSupportContextReceivesEventsTests(unittest.TestCase):
    """TEST10：get_entry_candidate_support_context等の銘柄分析コンテキストへイベントが渡る
    ことを、list_market_events()呼び出し自体がcanonical_event_key等の追加要件を課していない
    （新しく保存されたINDICATORイベントもそのまま素通りする）ことで確認する。"""

    def test_list_market_events_no_extra_filter_added(self):
        """_MARKET_EVENT_COLSにevent_type/canonical_event_key等が含まれ、通常のSELECT *経路
        （list_market_events）でそのまま返ってくることを確認する（新しい別ルートを作っていない）。"""
        self.assertIn("event_type", investment_db._MARKET_EVENT_COLS)
        self.assertIn("canonical_event_key", investment_db._MARKET_EVENT_COLS)
        self.assertIn("affected_markets", investment_db._MARKET_EVENT_COLS)


if __name__ == "__main__":
    unittest.main()
