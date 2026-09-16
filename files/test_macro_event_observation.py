# 金融政策イベント統合 Phase5A（2026-09-16新規）：MACRO EVENT OBSERVATIONの回帰テスト。
#
# canonical market_events → underlying_events橋渡し → 既存event_market_reactionsパイプライン
# への接続（bridge identity・precision別window・Level1/2/3観測対象選定・baseline provenance・
# 未来情報リーク防止）を検証する。ENTRY SCORE/Entry Gate/LOSS Learning weightには一切
# 触れていないため、それらのgolden動作は既存テストでカバーされる前提（本ファイルでは扱わない）。
#
# 実行方法： cd files && python -m unittest test_macro_event_observation -v

import datetime
import unittest
from unittest import mock

import server

JST = server._JST


def _dt(y, m, d, h, mi):
    return datetime.datetime(y, m, d, h, mi, tzinfo=JST)


def _fomc_event(event_time_jst="03:00", time_precision="EXACT", event_date="2026-09-17",
                 canonical_event_key="FOMC_POLICY_DECISION", title="FOMC政策金利・声明",
                 importance="CRITICAL", raw_payload=None, id_=96, country="US"):
    return {"id": id_, "event_date": event_date, "event_time_jst": event_time_jst,
             "time_precision": time_precision, "canonical_event_key": canonical_event_key,
             "title": title, "importance": importance, "raw_payload": raw_payload or {}, "country": country}


class MacroBridgeEventKeyTests(unittest.TestCase):
    """_macro_bridge_event_key()：表示titleではなくcanonical_event_key+event_date+
    event_time_jstから決定的なidentityを作ること。"""

    def test_deterministic_for_same_inputs(self):
        e = _fomc_event()
        self.assertEqual(server._macro_bridge_event_key(e), server._macro_bridge_event_key(e))

    def test_title_change_does_not_affect_identity(self):
        e1 = _fomc_event(title="FOMC 政策金利・声明・SEP・ドットチャート")
        e2 = _fomc_event(title="FOMC結果")  # 表記揺れ、Phase2/3の実データと同じ状況
        self.assertEqual(server._macro_bridge_event_key(e1), server._macro_bridge_event_key(e2))

    def test_different_canonical_key_gives_different_identity(self):
        decision = _fomc_event(canonical_event_key="FOMC_POLICY_DECISION")
        press = _fomc_event(canonical_event_key="FED_CHAIR_PRESS_CONFERENCE", event_time_jst="03:30")
        self.assertNotEqual(server._macro_bridge_event_key(decision), server._macro_bridge_event_key(press))

    def test_date_only_events_use_dateonly_marker_not_none(self):
        e = _fomc_event(event_time_jst=None, time_precision="DATE_ONLY", canonical_event_key="BOJ_POLICY_DECISION",
                          event_date="2026-09-18")
        key = server._macro_bridge_event_key(e)
        self.assertIn("DATEONLY", key)


class MacroBridgeEventTypeTests(unittest.TestCase):
    """_macro_bridge_event_type()：EXACT→CANONICAL、DATE_ONLY単発→SESSION、複数日→PERIOD。
    BOJ_MEETING（会合期間）とBOJ_POLICY_DECISION（結果）のsemantic distinctionを保持する。"""

    def test_exact_precision_is_canonical_type(self):
        e = _fomc_event()
        self.assertEqual(server._macro_bridge_event_type(e), server.MACRO_CANONICAL_EVENT_TYPE)

    def test_date_only_single_moment_is_session_type(self):
        e = _fomc_event(event_time_jst=None, time_precision="DATE_ONLY", canonical_event_key="BOJ_POLICY_DECISION")
        self.assertEqual(server._macro_bridge_event_type(e), server.MACRO_CANONICAL_SESSION_EVENT_TYPE)

    def test_multi_day_meeting_is_period_type(self):
        e = _fomc_event(event_time_jst=None, time_precision="DATE_ONLY", canonical_event_key="BOJ_MEETING",
                          event_date="2026-09-17", raw_payload={"end_date": "2026-09-18"})
        self.assertEqual(server._macro_bridge_event_type(e), server.MACRO_CANONICAL_PERIOD_EVENT_TYPE)

    def test_boj_meeting_and_boj_decision_get_different_bridge_types(self):
        """指示書D：会合期間そのものと結果発表は別物として扱う。"""
        meeting = _fomc_event(event_time_jst=None, time_precision="DATE_ONLY", canonical_event_key="BOJ_MEETING",
                                event_date="2026-09-17", raw_payload={"end_date": "2026-09-18"})
        decision = _fomc_event(event_time_jst=None, time_precision="DATE_ONLY", canonical_event_key="BOJ_POLICY_DECISION",
                                 event_date="2026-09-18")
        self.assertNotEqual(server._macro_bridge_event_type(meeting), server._macro_bridge_event_type(decision))
        self.assertEqual(server._macro_bridge_event_type(meeting), server.MACRO_CANONICAL_PERIOD_EVENT_TYPE)
        self.assertEqual(server._macro_bridge_event_type(decision), server.MACRO_CANONICAL_SESSION_EVENT_TYPE)


class MacroBridgeEventAtTests(unittest.TestCase):
    """_macro_bridge_event_at()：EXACTは正確なJST→UTC変換。DATE_ONLY/APPROXIMATE/PERIODは
    Noneを返さない（指示書D：resolve_event_market_relevant_at()がfirst_seen_atへ誤フォール
    バックするのを防ぐ、これが最重要のテスト）。"""

    def test_exact_precision_gives_correct_utc_datetime(self):
        e = _fomc_event(event_time_jst="03:00", event_date="2026-09-17")
        event_at = server._macro_bridge_event_at(e, server.MACRO_CANONICAL_EVENT_TYPE)
        expected = _dt(2026, 9, 17, 3, 0).astimezone(datetime.timezone.utc)
        self.assertEqual(event_at, expected)

    def test_session_type_never_returns_none(self):
        """最重要：DATE_ONLYイベントでevent_at=Noneを返すと、既存resolve_event_market_
        relevant_at()がfirst_seen_at（同期ジョブが実行された偶然の時刻）へフォールバックし、
        偽のイベント時刻を作ってしまう。event_atは必ず何か（この日のJST 00:00）を返すこと。"""
        e = _fomc_event(event_time_jst=None, time_precision="DATE_ONLY", event_date="2026-09-18")
        event_at = server._macro_bridge_event_at(e, server.MACRO_CANONICAL_SESSION_EVENT_TYPE)
        self.assertIsNotNone(event_at)
        self.assertEqual(event_at, _dt(2026, 9, 18, 0, 0).astimezone(datetime.timezone.utc))

    def test_period_type_never_returns_none(self):
        e = _fomc_event(event_time_jst=None, time_precision="DATE_ONLY", event_date="2026-09-17",
                          raw_payload={"end_date": "2026-09-18"})
        event_at = server._macro_bridge_event_at(e, server.MACRO_CANONICAL_PERIOD_EVENT_TYPE)
        self.assertIsNotNone(event_at)

    def test_missing_event_date_returns_none_gracefully(self):
        e = _fomc_event(event_date=None)
        self.assertIsNone(server._macro_bridge_event_at(e, server.MACRO_CANONICAL_SESSION_EVENT_TYPE))


class SyncCanonicalMarketEventToUnderlyingEventTests(unittest.TestCase):
    """sync_canonical_market_event_to_underlying_event()：idempotency（再同期で同じ行へ
    収束）・既存CENTRAL_BANK型に触れないことを確認する。"""

    def test_creates_new_bridge_when_none_exists(self):
        with mock.patch.object(server.investment_db, "list_underlying_event_candidates", return_value=[]), \
             mock.patch.object(server.investment_db, "create_underlying_event",
                                return_value={"id": 501, "event_key": "MACRO:FOMC_POLICY_DECISION:2026-09-17:03:00"}) as mock_create:
            result = server.sync_canonical_market_event_to_underlying_event("db", "user", _fomc_event())
        self.assertEqual(result["id"], 501)
        mock_create.assert_called_once()
        created_data = mock_create.call_args[0][1]
        self.assertEqual(created_data["event_type"], server.MACRO_CANONICAL_EVENT_TYPE)
        self.assertEqual(created_data["primary_source_type"], "CENTRAL_BANK_OFFICIAL")
        self.assertEqual(created_data["backfill_source"], "canonical_market_events_sync")

    def test_extended_move_explicitly_false_not_left_null(self):
        """実DB E2Eで検出した回帰：underlying_events.extended_moveはNOT NULL DEFAULT false
        だが、create_underlying_event()は未指定キーをNoneのままINSERTするためDB側DEFAULTが
        効かずNOT NULL違反になっていた。明示的にFalseを渡すことを固定する。"""
        with mock.patch.object(server.investment_db, "list_underlying_event_candidates", return_value=[]), \
             mock.patch.object(server.investment_db, "create_underlying_event", return_value={"id": 501}) as mock_create:
            server.sync_canonical_market_event_to_underlying_event("db", "user", _fomc_event())
        created_data = mock_create.call_args[0][1]
        self.assertIs(created_data["extended_move"], False)

    def test_returns_existing_bridge_without_creating_duplicate(self):
        """再同期テスト：同じevent_keyの行が既にあれば新規作成しない。"""
        existing_key = server._macro_bridge_event_key(_fomc_event())
        existing_row = {"id": 501, "event_key": existing_key, "event_type": server.MACRO_CANONICAL_EVENT_TYPE}
        with mock.patch.object(server.investment_db, "list_underlying_event_candidates", return_value=[existing_row]), \
             mock.patch.object(server.investment_db, "create_underlying_event") as mock_create:
            result = server.sync_canonical_market_event_to_underlying_event("db", "user", _fomc_event())
        self.assertEqual(result["id"], 501)
        mock_create.assert_not_called()

    def test_different_events_produce_different_bridges(self):
        decision = _fomc_event(canonical_event_key="FOMC_POLICY_DECISION", id_=96)
        press = _fomc_event(canonical_event_key="FED_CHAIR_PRESS_CONFERENCE", event_time_jst="03:30", id_=97)
        with mock.patch.object(server.investment_db, "list_underlying_event_candidates", return_value=[]), \
             mock.patch.object(server.investment_db, "create_underlying_event", side_effect=lambda db, data: {"id": 1, **data}) as mock_create:
            server.sync_canonical_market_event_to_underlying_event("db", "user", decision)
            server.sync_canonical_market_event_to_underlying_event("db", "user", press)
        self.assertEqual(mock_create.call_count, 2)
        keys = [c[0][1]["event_key"] for c in mock_create.call_args_list]
        self.assertEqual(len(set(keys)), 2)


class CaptureMacroDriverSnapshotTests(unittest.TestCase):
    """capture_macro_driver_snapshot()：Level1 driverの取得とunit区別（US10Yはyield%）。"""

    def test_all_drivers_present_with_correct_units(self):
        with mock.patch.object(server, "_get_market_snapshot",
                                return_value={"price": 100.0, "captured_at": "2026-09-16T12:00:00+00:00", "source": "yfinance"}):
            snap = server.capture_macro_driver_snapshot()
        self.assertEqual(set(snap.keys()), {"NIKKEI_FUT", "TOPIX", "USDJPY", "US10Y", "SOX", "NASDAQ"})
        self.assertEqual(snap["US10Y"]["unit"], "PERCENT_YIELD")
        self.assertEqual(snap["NIKKEI_FUT"]["unit"], "PRICE")
        self.assertEqual(snap["US10Y"]["status"], "OK")

    def test_driver_fetch_failure_reports_no_data_without_raising(self):
        with mock.patch.object(server, "_get_market_snapshot", side_effect=RuntimeError("boom")):
            snap = server.capture_macro_driver_snapshot()
        self.assertEqual(snap["US10Y"]["status"], "NO_DATA")
        self.assertIsNone(snap["US10Y"]["value"])


class SelectSectorRepresentativeStocksTests(unittest.TestCase):
    """select_sector_representative_stocks()：カテゴリごとに1銘柄、決定的選択、
    ハードコードは業種名対応表のみ。"""

    def _watchlist(self):
        return [
            {"code": "8035", "name": "東京エレクトロン", "sector": "電気機器"},
            {"code": "6501", "name": "日立", "sector": "電気機器"},
            {"code": "8306", "name": "三菱UFJ", "sector": "銀行業"},
            {"code": "7203", "name": "トヨタ", "sector": "輸送用機器"},
            {"code": "1234", "name": "無業種銘柄", "sector": None},
        ]

    def test_one_representative_per_category(self):
        reps = server.select_sector_representative_stocks(self._watchlist())
        categories = [r["category"] for r in reps]
        self.assertEqual(len(categories), len(set(categories)))  # カテゴリ重複なし

    def test_deterministic_lowest_code_chosen(self):
        reps = server.select_sector_representative_stocks(self._watchlist())
        electronics = next(r for r in reps if r["category"] == "SEMICONDUCTOR_ELECTRONICS")
        self.assertEqual(electronics["code"], "6501")  # 6501 < 8035

    def test_missing_category_is_skipped_not_fabricated(self):
        wl = [{"code": "8306", "name": "銀行", "sector": "銀行業"}]
        reps = server.select_sector_representative_stocks(wl)
        self.assertEqual(len(reps), 1)
        self.assertEqual(reps[0]["category"], "BANK")

    def test_empty_watchlist_returns_empty(self):
        self.assertEqual(server.select_sector_representative_stocks([]), [])


class SelectUserRelevantStocksTests(unittest.TestCase):
    """select_user_relevant_stocks()：position/today trade/ENTRY TOP5のdedup・reason統合。"""

    def test_dedup_across_sources_merges_reasons(self):
        with mock.patch.object(server.investment_db, "list_portfolio", return_value=[{"code": "7203", "name": "トヨタ"}]), \
             mock.patch.object(server.investment_db, "list_trade_history", return_value=[]), \
             mock.patch.object(server, "get_entry_top5_cached", return_value={"entryReadyTop5": [{"code": "7203", "name": "トヨタ"}]}):
            result = server.select_user_relevant_stocks("db", "user")
        self.assertEqual(len(result), 1)
        self.assertIn("POSITION", result[0]["reason"])
        self.assertIn("ENTRY_TOP5", result[0]["reason"])

    def test_only_todays_trades_included(self):
        today = server._jst_today_date_str()
        with mock.patch.object(server.investment_db, "list_portfolio", return_value=[]), \
             mock.patch.object(server.investment_db, "list_trade_history",
                                return_value=[{"code": "6997", "name": "A", "closed_at": f"{today}T10:00:00+00:00"},
                                               {"code": "9999", "name": "B", "closed_at": "2020-01-01T10:00:00+00:00"}]), \
             mock.patch.object(server, "get_entry_top5_cached", return_value=None):
            result = server.select_user_relevant_stocks("db", "user")
        codes = [r["code"] for r in result]
        self.assertIn("6997", codes)
        self.assertNotIn("9999", codes)

    def test_no_database_url_returns_empty(self):
        self.assertEqual(server.select_user_relevant_stocks(None, "user"), [])


class BuildMacroObservationSymbolSetTests(unittest.TestCase):
    """build_macro_observation_symbol_set()：Level2+3統合・dedup・上限件数。"""

    def test_respects_max_symbols_cap(self):
        big_watchlist = [{"code": f"{1000+i}", "name": f"銘柄{i}", "sector": "電気機器"} for i in range(50)]
        many_positions = [{"code": f"{2000+i}", "name": f"保有{i}"} for i in range(50)]
        with mock.patch.object(server.investment_db, "list_watchlist", return_value=big_watchlist), \
             mock.patch.object(server.investment_db, "list_portfolio", return_value=many_positions), \
             mock.patch.object(server.investment_db, "list_trade_history", return_value=[]), \
             mock.patch.object(server, "get_entry_top5_cached", return_value=None):
            result = server.build_macro_observation_symbol_set("db", "user")
        self.assertLessEqual(len(result), server.MACRO_OBSERVATION_MAX_SYMBOLS)

    def test_user_relevant_stocks_prioritized_over_sector_representatives_when_capped(self):
        watchlist = [{"code": "8035", "name": "東京エレクトロン", "sector": "電気機器"}]
        with mock.patch.object(server.investment_db, "list_watchlist", return_value=watchlist), \
             mock.patch.object(server.investment_db, "list_portfolio", return_value=[{"code": "9999", "name": "保有株"}]), \
             mock.patch.object(server.investment_db, "list_trade_history", return_value=[]), \
             mock.patch.object(server, "get_entry_top5_cached", return_value=None), \
             mock.patch.object(server, "MACRO_OBSERVATION_MAX_SYMBOLS", 1):
            result = server.build_macro_observation_symbol_set("db", "user")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["code"], "9999")  # Level3(保有株)が優先される


class SectorExposureHypothesisTests(unittest.TestCase):
    """sector_exposure_hypothesis_for()：中立的なラベル（方向を持たない）・バージョン管理。"""

    def test_bank_sector_hypothesis_has_no_directional_label(self):
        h = server.sector_exposure_hypothesis_for("銀行業")
        for key in ("RATE_EXPOSURE_HYPOTHESIS", "FX_EXPOSURE_HYPOTHESIS", "GROWTH_LIQUIDITY_EXPOSURE_HYPOTHESIS"):
            self.assertIn(h[key], ("HIGH", "POSSIBLE", "LOW", None))
            # 「上がりやすい/下がりやすい」的な文字列が混ざっていないことを確認
            self.assertNotIn("UP", str(h[key]))
            self.assertNotIn("DOWN", str(h[key]))
            self.assertNotIn("BULLISH", str(h[key]))
            self.assertNotIn("BEARISH", str(h[key]))

    def test_unknown_sector_returns_none_not_guessed(self):
        h = server.sector_exposure_hypothesis_for("未知の業種")
        self.assertIsNone(h["RATE_EXPOSURE_HYPOTHESIS"])
        self.assertIsNone(h["category"])

    def test_version_is_stamped(self):
        h = server.sector_exposure_hypothesis_for("銀行業")
        self.assertEqual(h["sector_prior_version"], server.SECTOR_PRIOR_HYPOTHESIS_VERSION)


class CaptureMacroEventBaselineSnapshotTests(unittest.TestCase):
    """capture_macro_event_baseline_snapshot()：baseline非上書き・LIVE/BACKFILLED区別・
    未来情報リーク防止（最重要）。"""

    def _common_mocks(self):
        return [
            mock.patch.object(server, "capture_macro_driver_snapshot",
                               return_value={"NIKKEI_FUT": {"value": 44000, "unit": "PRICE", "status": "OK"}}),
            mock.patch.object(server, "build_macro_observation_symbol_set", return_value=[]),
            mock.patch.object(server, "get_stock_quotes", return_value={}),
        ]

    def test_live_pre_event_when_now_is_before_event(self):
        bridge = {"id": 501, "numerical_fingerprint_json": {}}
        before = _dt(2026, 9, 16, 12, 0).astimezone(datetime.timezone.utc)  # FOMC 03:00 09/17の15時間前
        with mock.patch.object(server, "capture_macro_driver_snapshot", return_value={}), \
             mock.patch.object(server, "build_macro_observation_symbol_set", return_value=[]), \
             mock.patch.object(server, "get_stock_quotes", return_value={}), \
             mock.patch.object(server.investment_db, "update_underlying_event",
                                side_effect=lambda db, eid, fields: {**bridge, **fields}) as mock_update:
            result = server.capture_macro_event_baseline_snapshot("db", "user", _fomc_event(), bridge, now_utc=before)
        payload = mock_update.call_args[0][2]["numerical_fingerprint_json"]["macro_observation"]
        self.assertEqual(payload["baseline_status"], "LIVE_PRE_EVENT")

    def test_backfilled_pre_event_when_now_is_after_event(self):
        bridge = {"id": 501, "numerical_fingerprint_json": {}}
        after = _dt(2026, 9, 17, 6, 0).astimezone(datetime.timezone.utc)  # FOMC後3時間
        with mock.patch.object(server, "capture_macro_driver_snapshot", return_value={}), \
             mock.patch.object(server, "build_macro_observation_symbol_set", return_value=[]), \
             mock.patch.object(server, "get_stock_quotes", return_value={}), \
             mock.patch.object(server.investment_db, "update_underlying_event",
                                side_effect=lambda db, eid, fields: {**bridge, **fields}) as mock_update:
            server.capture_macro_event_baseline_snapshot("db", "user", _fomc_event(), bridge, now_utc=after)
        payload = mock_update.call_args[0][2]["numerical_fingerprint_json"]["macro_observation"]
        self.assertEqual(payload["baseline_status"], "BACKFILLED_PRE_EVENT")

    def test_existing_baseline_is_never_overwritten(self):
        """指示書B最重要事項：result取得時にbaselineを再取得して置換してはいけない。"""
        bridge_with_baseline = {"id": 501, "numerical_fingerprint_json": {
            "macro_observation": {"baseline_status": "LIVE_PRE_EVENT", "baseline_captured_at": "2026-09-16T12:00:00+00:00",
                                    "baseline_drivers": {"NIKKEI_FUT": {"value": 44000}}}}}
        with mock.patch.object(server.investment_db, "update_underlying_event") as mock_update:
            result = server.capture_macro_event_baseline_snapshot(
                "db", "user", _fomc_event(), bridge_with_baseline,
                now_utc=_dt(2026, 9, 17, 6, 0).astimezone(datetime.timezone.utc))
        mock_update.assert_not_called()
        self.assertEqual(result, bridge_with_baseline)


class NoFutureLeakInBaselineTests(unittest.TestCase):
    """指示書10番必須：同一のevent前snapshotに対し、event結果A/Bパターンを後から与えても
    pre_event_snapshot（baseline_drivers等）が完全一致すること。変わってよいのはpost-event
    reactionだけ（baseline captureそのものはevent結果を一切参照しない設計のため、
    構造的に保証されることを確認する）。"""

    def test_baseline_identical_regardless_of_event_result_field(self):
        driver_snapshot = {"NIKKEI_FUT": {"value": 44000, "unit": "PRICE", "status": "OK"}}
        before = _dt(2026, 9, 17, 1, 0).astimezone(datetime.timezone.utc)

        event_no_result = _fomc_event()
        event_with_result_a = _fomc_event(raw_payload={"result": "利下げ0.25%"})
        event_with_result_b = _fomc_event(raw_payload={"result": "据え置き"})

        payloads = []
        for event in (event_no_result, event_with_result_a, event_with_result_b):
            bridge = {"id": 501, "numerical_fingerprint_json": {}}
            with mock.patch.object(server, "capture_macro_driver_snapshot", return_value=driver_snapshot), \
                 mock.patch.object(server, "build_macro_observation_symbol_set", return_value=[]), \
                 mock.patch.object(server, "get_stock_quotes", return_value={}), \
                 mock.patch.object(server.investment_db, "update_underlying_event",
                                    side_effect=lambda db, eid, fields: fields) as mock_update:
                server.capture_macro_event_baseline_snapshot("db", "user", event, bridge, now_utc=before)
            payload = mock_update.call_args[0][2]["numerical_fingerprint_json"]["macro_observation"]
            payload.pop("baseline_captured_at")  # 呼び出しタイミング差は許容（now_utc固定なので実質同じだが念のため除外）
            payloads.append(payload)

        self.assertEqual(payloads[0], payloads[1])
        self.assertEqual(payloads[0], payloads[2])


class SyncMacroEventObservationPerformanceTests(unittest.TestCase):
    """実DB E2Eで検出した性能問題の回帰テスト：
    (1) capture_macro_event_baseline_snapshot()がsector参照でwatchlistをN+1取得しないこと
    (2) 既にreaction行があるbridgeへ毎回generate_event_market_reactions_for_event()を
        呼ばない（5分間隔schedulerから無限に外部価格取得が積み重なるのを防ぐ）。"""

    def test_baseline_snapshot_fetches_watchlist_only_once(self):
        symbol_set = [{"code": f"{9000+i}", "name": f"銘柄{i}", "level": 2, "reason": ["SECTOR_REPRESENTATIVE"]}
                       for i in range(5)]
        bridge = {"id": 501, "numerical_fingerprint_json": {}}
        with mock.patch.object(server, "capture_macro_driver_snapshot", return_value={}), \
             mock.patch.object(server, "build_macro_observation_symbol_set", return_value=symbol_set), \
             mock.patch.object(server, "get_stock_quotes", return_value={}), \
             mock.patch.object(server.investment_db, "list_watchlist", return_value=[]) as mock_watchlist, \
             mock.patch.object(server.investment_db, "update_underlying_event",
                                side_effect=lambda db, eid, fields: {**bridge, **fields}):
            server.capture_macro_event_baseline_snapshot("db", "user", _fomc_event(), bridge,
                                                             now_utc=_dt(2026, 9, 16, 12, 0).astimezone(datetime.timezone.utc))
        mock_watchlist.assert_called_once()  # 5銘柄あってもwatchlist取得は1回だけ

    def test_reaction_generation_skipped_when_reactions_already_exist(self):
        bridge = {"id": 501, "numerical_fingerprint_json": {"macro_observation": {"baseline_captured_at": "2026-09-16T12:00:00+00:00"}}}
        with mock.patch.object(server, "sync_canonical_market_event_to_underlying_event", return_value=bridge), \
             mock.patch.object(server, "capture_macro_event_baseline_snapshot", return_value=bridge), \
             mock.patch.object(server.investment_db, "list_event_market_reactions_for_event",
                                return_value=[{"id": 1}]), \
             mock.patch.object(server, "build_macro_observation_symbol_set", return_value=[]), \
             mock.patch.object(server, "generate_event_market_reactions_for_event") as mock_generate:
            result = server.sync_macro_event_observation("db", "user", _fomc_event())
        mock_generate.assert_not_called()
        self.assertTrue(result["skipped_generation"])

    def test_reaction_generation_runs_when_none_exist_yet(self):
        bridge = {"id": 501, "numerical_fingerprint_json": {}}
        with mock.patch.object(server, "sync_canonical_market_event_to_underlying_event", return_value=bridge), \
             mock.patch.object(server, "capture_macro_event_baseline_snapshot", return_value=bridge), \
             mock.patch.object(server.investment_db, "list_event_market_reactions_for_event", return_value=[]), \
             mock.patch.object(server, "build_macro_observation_symbol_set", return_value=[]), \
             mock.patch.object(server, "generate_event_market_reactions_for_event", return_value=5) as mock_generate:
            result = server.sync_macro_event_observation("db", "user", _fomc_event())
        mock_generate.assert_called_once()
        self.assertFalse(result["skipped_generation"])
        self.assertEqual(result["reactions_created"], 5)


class SyncPendingMacroEventObservationsTests(unittest.TestCase):
    """sync_pending_macro_event_observations()：canonical_event_keyの無いイベントを除外し、
    エラーでも他のイベント処理を止めないこと。"""

    def test_non_canonical_events_are_skipped(self):
        events = [{"id": 1, "title": "手動登録イベント", "canonical_event_key": None, "event_date": "2026-09-17"}]
        with mock.patch.object(server.investment_db, "list_market_events", return_value=events):
            result = server.sync_pending_macro_event_observations("db", "user")
        self.assertEqual(result["processed"], 0)

    def test_one_event_failure_does_not_block_others(self):
        events = [_fomc_event(id_=96, canonical_event_key="FOMC_POLICY_DECISION"),
                  _fomc_event(id_=97, canonical_event_key="FED_CHAIR_PRESS_CONFERENCE", event_time_jst="03:30")]
        with mock.patch.object(server.investment_db, "list_market_events", return_value=events), \
             mock.patch.object(server, "sync_macro_event_observation",
                                side_effect=[RuntimeError("boom"), {"bridge_id": 502, "reactions_created": 2, "symbol_count": 5}]):
            result = server.sync_pending_macro_event_observations("db", "user")
        self.assertEqual(result["processed"], 2)
        self.assertEqual(result["errors"], 1)
        self.assertEqual(result["synced"], 1)


if __name__ == "__main__":
    unittest.main()
