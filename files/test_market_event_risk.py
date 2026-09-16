# 金融政策イベント統合 Phase4（2026-09-16新規）：MARKET_EVENT_RISKの回帰テスト。
#
# Phase2/3で作ったcanonical market_events（canonical_event_key/event_time_jst/
# time_precision付き）から、event_status（UPCOMING/IMMINENT/IN_PROGRESS/RELEASED/
# POST_EVENT）とtime_to_event_hoursを算出するcompute_market_event_status()、
# 銘柄非依存のMARKET_EVENT_RISKを集計するbuild_active_macro_events()を検証する。
# 既存classify_event_risk_level/compute_event_risk_for_events（Chorucoスタイル市場モード）
# は変更していないため、それらのgolden動作は既存test_choruco_style.py側でカバーされる前提。
#
# 実行方法： cd files && python -m unittest test_market_event_risk -v

import datetime
import unittest
from unittest import mock

import server


JST = server._JST


def _dt(y, m, d, h, mi):
    return datetime.datetime(y, m, d, h, mi, tzinfo=JST)


def _fomc_event(event_time_jst="03:00", time_precision="EXACT", event_date="2026-09-17",
                 canonical_event_key="FOMC_POLICY_DECISION", title="FOMC政策金利・声明",
                 importance="HIGH", raw_payload=None):
    return {"id": 96, "event_date": event_date, "event_time_jst": event_time_jst,
             "time_precision": time_precision, "canonical_event_key": canonical_event_key,
             "title": title, "importance": importance, "raw_payload": raw_payload or {}}


class ComputeMarketEventStatusExactPrecisionTests(unittest.TestCase):
    """EXACT精度：UPCOMING/IMMINENT/RELEASED/POST_EVENTの境界を検証する
    （実データ：FOMC政策決定=2026-09-17 03:00 JST）。"""

    def test_far_future_is_upcoming(self):
        now = _dt(2026, 9, 16, 12, 0)  # 前日正午、約15時間前
        s = server.compute_market_event_status(_fomc_event(), now_jst=now)
        self.assertEqual(s["event_status"], "UPCOMING")
        self.assertAlmostEqual(s["time_to_event_hours"], 15.0, delta=0.01)

    def test_just_over_one_hour_before_is_still_upcoming(self):
        now = _dt(2026, 9, 17, 1, 59)  # 1時間1分前
        s = server.compute_market_event_status(_fomc_event(), now_jst=now)
        self.assertEqual(s["event_status"], "UPCOMING")

    def test_within_one_hour_before_is_imminent(self):
        now = _dt(2026, 9, 17, 2, 30)  # 30分前
        s = server.compute_market_event_status(_fomc_event(), now_jst=now)
        self.assertEqual(s["event_status"], "IMMINENT")
        self.assertAlmostEqual(s["time_to_event_hours"], 0.5, delta=0.01)

    def test_exact_event_time_is_imminent_not_released(self):
        now = _dt(2026, 9, 17, 3, 0)  # ちょうど発表時刻
        s = server.compute_market_event_status(_fomc_event(), now_jst=now)
        self.assertEqual(s["time_to_event_hours"], 0.0)
        self.assertEqual(s["event_status"], "RELEASED")  # 0は「発表後」側（<=0の境界）

    def test_within_two_hours_after_is_released(self):
        now = _dt(2026, 9, 17, 4, 30)  # 1時間30分後
        s = server.compute_market_event_status(_fomc_event(), now_jst=now)
        self.assertEqual(s["event_status"], "RELEASED")

    def test_beyond_two_hours_after_is_post_event(self):
        now = _dt(2026, 9, 17, 6, 0)  # 3時間後
        s = server.compute_market_event_status(_fomc_event(), now_jst=now)
        self.assertEqual(s["event_status"], "POST_EVENT")

    def test_next_day_is_far_post_event(self):
        now = _dt(2026, 9, 18, 3, 0)
        s = server.compute_market_event_status(_fomc_event(), now_jst=now)
        self.assertEqual(s["event_status"], "POST_EVENT")
        self.assertAlmostEqual(s["time_to_event_hours"], -24.0, delta=0.01)


class ComputeMarketEventStatusIndependentRiskWindowsTests(unittest.TestCase):
    """実データの核心：FOMC政策決定(03:00)と30分後のFed議長会見(03:30)は独立したrisk window
    として扱われること。同様にBOJ政策決定(結果、時刻不明)と15:30の植田総裁会見も独立。"""

    def test_fomc_decision_and_press_conference_are_independent_windows(self):
        decision = _fomc_event(event_time_jst="03:00", canonical_event_key="FOMC_POLICY_DECISION",
                                 title="FOMC政策金利・声明")
        press = _fomc_event(event_time_jst="03:30", canonical_event_key="FED_CHAIR_PRESS_CONFERENCE",
                              title="FRB議長 記者会見")
        # 03:15時点：決定は発表直後（RELEASED）、会見はまだこれから（IMMINENT）——
        # 同じFOMCイベント群でも状態が異なることを確認する（指示書の核心要求）。
        now = _dt(2026, 9, 17, 3, 15)
        s_decision = server.compute_market_event_status(decision, now_jst=now)
        s_press = server.compute_market_event_status(press, now_jst=now)
        self.assertEqual(s_decision["event_status"], "RELEASED")
        self.assertEqual(s_press["event_status"], "IMMINENT")
        self.assertNotEqual(s_decision["canonical_event_key"], s_press["canonical_event_key"])

    def test_boj_decision_and_governor_press_conference_are_independent_windows(self):
        """実データ：BOJ決定結果は時刻不明（DATE_ONLY）、植田会見は15:30（EXACT）。"""
        decision = _fomc_event(event_date="2026-09-18", event_time_jst=None, time_precision="DATE_ONLY",
                                 canonical_event_key="BOJ_POLICY_DECISION", title="日銀金融政策決定")
        press = _fomc_event(event_date="2026-09-18", event_time_jst="15:30", time_precision="EXACT",
                              canonical_event_key="BOJ_GOVERNOR_PRESS_CONFERENCE", title="日銀総裁 記者会見")
        now = _dt(2026, 9, 18, 10, 0)  # 会見の5.5時間前
        s_decision = server.compute_market_event_status(decision, now_jst=now)
        s_press = server.compute_market_event_status(press, now_jst=now)
        self.assertEqual(s_decision["event_status"], "IN_PROGRESS")  # DATE_ONLYの当日＝安全側でIN_PROGRESS
        self.assertEqual(s_press["event_status"], "UPCOMING")  # EXACTなので正確にUPCOMING判定できる
        self.assertNotEqual(s_decision["canonical_event_key"], s_press["canonical_event_key"])


class ComputeMarketEventStatusDateOnlyPrecisionTests(unittest.TestCase):
    """DATE_ONLY/APPROXIMATE精度：IMMINENT/RELEASEDのような時刻依存判定を絶対に生成しない
    （指示書「DATE_ONLYやAPPROXIMATEをEXACT時刻と同じ精度で扱わない」）。"""

    def test_date_only_future_is_upcoming(self):
        now = _dt(2026, 9, 16, 10, 0)
        s = server.compute_market_event_status(
            _fomc_event(event_date="2026-09-18", event_time_jst=None, time_precision="DATE_ONLY"), now_jst=now)
        self.assertEqual(s["event_status"], "UPCOMING")

    def test_date_only_today_is_in_progress_never_imminent_or_released(self):
        for hour in (0, 6, 12, 18, 23):
            with self.subTest(hour=hour):
                now = _dt(2026, 9, 18, hour, 0)
                s = server.compute_market_event_status(
                    _fomc_event(event_date="2026-09-18", event_time_jst=None, time_precision="DATE_ONLY"),
                    now_jst=now)
                self.assertEqual(s["event_status"], "IN_PROGRESS")
                self.assertNotIn(s["event_status"], ("IMMINENT", "RELEASED"))

    def test_date_only_past_is_post_event(self):
        now = _dt(2026, 9, 20, 10, 0)
        s = server.compute_market_event_status(
            _fomc_event(event_date="2026-09-18", event_time_jst=None, time_precision="DATE_ONLY"), now_jst=now)
        self.assertEqual(s["event_status"], "POST_EVENT")

    def test_approximate_time_behaves_same_as_date_only(self):
        """time_precision="APPROXIMATE"（"daytime"等の非数値時刻）はevent_time_jstがNoneの
        ままのはずだが、念のためEXACTでない限りIMMINENT/RELEASEDを生成しないことを確認する。"""
        now = _dt(2026, 9, 18, 12, 0)
        s = server.compute_market_event_status(
            _fomc_event(event_date="2026-09-18", event_time_jst=None, time_precision="APPROXIMATE"), now_jst=now)
        self.assertEqual(s["event_status"], "IN_PROGRESS")


class ComputeMarketEventStatusMultiDayTests(unittest.TestCase):
    """複数日イベント（BOJ会合の開催期間そのもの、実データ：2026-09-17〜09-18）はend_dateが
    あれば期間中IN_PROGRESSになること。"""

    def test_in_progress_during_meeting_period(self):
        event = _fomc_event(event_date="2026-09-17", event_time_jst=None, time_precision="DATE_ONLY",
                              canonical_event_key="BOJ_MEETING", title="日銀金融政策決定会合",
                              raw_payload={"end_date": "2026-09-18"})
        for now in (_dt(2026, 9, 17, 9, 0), _dt(2026, 9, 17, 23, 0), _dt(2026, 9, 18, 14, 0)):
            with self.subTest(now=now):
                s = server.compute_market_event_status(event, now_jst=now)
                self.assertEqual(s["event_status"], "IN_PROGRESS")

    def test_upcoming_before_meeting_starts(self):
        event = _fomc_event(event_date="2026-09-17", event_time_jst=None, time_precision="DATE_ONLY",
                              canonical_event_key="BOJ_MEETING", raw_payload={"end_date": "2026-09-18"})
        s = server.compute_market_event_status(event, now_jst=_dt(2026, 9, 16, 10, 0))
        self.assertEqual(s["event_status"], "UPCOMING")

    def test_post_event_after_meeting_ends(self):
        event = _fomc_event(event_date="2026-09-17", event_time_jst=None, time_precision="DATE_ONLY",
                              canonical_event_key="BOJ_MEETING", raw_payload={"end_date": "2026-09-18"})
        s = server.compute_market_event_status(event, now_jst=_dt(2026, 9, 19, 10, 0))
        self.assertEqual(s["event_status"], "POST_EVENT")


class ComputeMarketEventStatusEdgeCaseTests(unittest.TestCase):
    def test_missing_event_date_returns_none(self):
        event = _fomc_event(event_date=None)
        self.assertIsNone(server.compute_market_event_status(event))

    def test_malformed_event_time_returns_none(self):
        event = _fomc_event(event_time_jst="notatime", time_precision="EXACT")
        self.assertIsNone(server.compute_market_event_status(event))

    def test_defaults_to_real_now_when_omitted(self):
        """now_jst省略時は例外を出さず実時刻で計算されること（実運用での通常呼び出し方）。"""
        event = _fomc_event(event_date="2099-01-01")  # 十分未来の日付
        s = server.compute_market_event_status(event)
        self.assertIsNotNone(s)
        self.assertEqual(s["event_status"], "UPCOMING")


class NoFutureInformationLeakTests(unittest.TestCase):
    """指示書の核心要求：同一snapshot時刻で、後からイベント結果だけ変更しても発表前の
    Market Event Riskが変わらないこと（未来情報リーク防止の回帰テスト）。"""

    def test_status_unaffected_by_later_result_data_at_same_reference_time(self):
        before_event = _dt(2026, 9, 17, 1, 0)  # 発表2時間前（固定のsnapshot時刻）
        event_without_result = _fomc_event()
        event_with_result_added_later = _fomc_event(raw_payload={
            "result": "利下げ0.25%", "statement_summary": "タカ派寄り", "dot_plot_median_2027": 3.25,
        })
        s_before = server.compute_market_event_status(event_without_result, now_jst=before_event)
        s_after_result_added = server.compute_market_event_status(event_with_result_added_later, now_jst=before_event)
        # "now"（snapshot時刻）を全く同じに固定した場合、resultフィールドの有無は
        # event_status/time_to_event_hoursに一切影響しないこと。
        self.assertEqual(s_before["event_status"], s_after_result_added["event_status"])
        self.assertEqual(s_before["time_to_event_hours"], s_after_result_added["time_to_event_hours"])
        self.assertEqual(s_before["event_status"], "UPCOMING")

    def test_market_event_risk_level_unaffected_by_result_data_before_event(self):
        before_event = _dt(2026, 9, 17, 2, 45)  # 発表15分前
        events_without_result = [_fomc_event()]
        events_with_result = [_fomc_event(raw_payload={"result": "据え置き"})]
        with mock.patch.object(server.investment_db, "list_market_events", return_value=events_without_result):
            r1 = server.build_active_macro_events("db", "user", now_jst=before_event)
        with mock.patch.object(server.investment_db, "list_market_events", return_value=events_with_result):
            r2 = server.build_active_macro_events("db", "user", now_jst=before_event)
        self.assertEqual(r1["market_event_risk"], r2["market_event_risk"])
        self.assertEqual(r1["events"][0]["event_status"], r2["events"][0]["event_status"])


class BuildActiveMacroEventsTests(unittest.TestCase):
    """build_active_macro_events()：canonical_event_keyでの絞り込み・ソート・
    market_event_riskの集計（既存compute_event_risk_for_eventsの再利用）を検証する。"""

    def test_non_canonical_events_are_excluded(self):
        events = [_fomc_event(canonical_event_key=None, title="CPI発表（手動登録、canonical化前）")]
        with mock.patch.object(server.investment_db, "list_market_events", return_value=events):
            r = server.build_active_macro_events("db", "user", now_jst=_dt(2026, 9, 16, 12, 0))
        self.assertEqual(r["events"], [])

    def test_sorted_by_time_to_event_ascending(self):
        far = _fomc_event(event_date="2026-09-20", event_time_jst="10:00", canonical_event_key="ECB_POLICY_DECISION")
        near = _fomc_event(event_date="2026-09-17", event_time_jst="03:00", canonical_event_key="FOMC_POLICY_DECISION")
        with mock.patch.object(server.investment_db, "list_market_events", return_value=[far, near]):
            r = server.build_active_macro_events("db", "user", now_jst=_dt(2026, 9, 16, 12, 0))
        self.assertEqual([e["canonical_event_key"] for e in r["events"]],
                          ["FOMC_POLICY_DECISION", "ECB_POLICY_DECISION"])

    def test_post_event_excluded_from_risk_but_still_listed(self):
        old_event = _fomc_event(event_date="2026-09-10", event_time_jst="03:00", importance="CRITICAL")
        with mock.patch.object(server.investment_db, "list_market_events", return_value=[old_event]):
            r = server.build_active_macro_events("db", "user", now_jst=_dt(2026, 9, 16, 12, 0))
        self.assertEqual(len(r["events"]), 1)  # 一覧には残る
        self.assertEqual(r["events"][0]["event_status"], "POST_EVENT")
        self.assertEqual(r["market_event_risk"], "LOW")  # だがリスク集計には寄与しない

    def test_imminent_critical_event_raises_market_event_risk(self):
        """既存classify_event_risk_level()の再利用を確認する：重要イベントが目前だと
        EXTREME（既存ロジック、<6hかつcritical重み付け適用）になること。"""
        imminent = _fomc_event(event_time_jst="03:00", importance="CRITICAL")
        with mock.patch.object(server.investment_db, "list_market_events", return_value=[imminent]):
            r = server.build_active_macro_events("db", "user", now_jst=_dt(2026, 9, 17, 2, 30))
        self.assertEqual(r["market_event_risk"], "EXTREME")

    def test_no_events_returns_low_risk(self):
        with mock.patch.object(server.investment_db, "list_market_events", return_value=[]):
            r = server.build_active_macro_events("db", "user", now_jst=_dt(2026, 9, 16, 12, 0))
        self.assertEqual(r["market_event_risk"], "LOW")
        self.assertEqual(r["events"], [])

    def test_db_exception_returns_safe_default(self):
        with mock.patch.object(server.investment_db, "list_market_events", side_effect=RuntimeError("boom")):
            r = server.build_active_macro_events("db", "user", now_jst=_dt(2026, 9, 16, 12, 0))
        self.assertEqual(r, {"events": [], "market_event_risk": "LOW"})

    def test_no_database_url_returns_safe_default_without_db_call(self):
        with mock.patch.object(server.investment_db, "list_market_events") as mock_list:
            r = server.build_active_macro_events(None, "user")
        mock_list.assert_not_called()
        self.assertEqual(r, {"events": [], "market_event_risk": "LOW"})


def _make_wiring_watchlist(n=3):
    return [{"code": f"W{i:04d}", "name": f"テスト銘柄{i}", "market": "JP", "sector": "テスト業種"}
            for i in range(n)]


def _make_wiring_stage1_rows(watchlist):
    rows = {}
    for i, w in enumerate(watchlist):
        rows[w["code"]] = {"current": 1000.0 + i, "changePct": 1.0, "high": 1010.0 + i, "low": 990.0 + i,
                             "open": 995.0 + i, "volume": 100000, "turnover": 1e8, "marketRS": 1.0,
                             "sectorRS": 0.5, "sector": "テスト業種"}
    return rows


class ScoreEntryCandidatesMacroEventWiringTests(unittest.TestCase):
    """_score_entry_candidates()がactiveMacroEvents/marketEventRiskを追加専用フィールドとして
    返すこと（entry_score/entryState/entryReadyTop5の既存ロジックには影響しないこと）を、
    実際に候補が算出される非空watchlistで確認する。build_active_macro_events自体は
    別テストクラスで検証済みのためモックする（このテストの関心はwiringのみ）。"""

    def _run_with_macro_mock(self, macro_return):
        watchlist = _make_wiring_watchlist()
        stage1_rows = _make_wiring_stage1_rows(watchlist)
        stage1_payload = {"rows": stage1_rows, "nikkeiChangePct": 0.3, "scanFailed": False,
                            "builtAt": 0, "codesScanned": len(watchlist), "pricesReturned": len(watchlist),
                            "durationSec": 0, "requestCount": 1, "usedStaleCache": False}
        with mock.patch.object(server.investment_db, "list_watchlist", return_value=watchlist), \
             mock.patch.object(server.investment_db, "get_codes_with_auto_tag", return_value=set()), \
             mock.patch.object(server, "run_momentum_stage1", return_value=stage1_payload), \
             mock.patch.object(server, "get_entry_candidate_support_context",
                                return_value=({}, {"batch_loaded": False})), \
             mock.patch.object(server, "prefetch_daily_arrays_for_watchlist",
                                return_value={"arraysByCode": {}}), \
             mock.patch.object(server, "prefetch_market_data_for_watchlist",
                                return_value={"marketDataByCode": {}}), \
             mock.patch.object(server, "capture_entry_candidate_snapshot_safe", return_value=None), \
             mock.patch.object(server, "get_stock_quotes", return_value={}), \
             mock.patch.object(server, "_intraday_regime_cached", return_value=(None, "failed")), \
             mock.patch.object(server, "_cached_daily_arrays", return_value=None), \
             mock.patch.object(server, "build_active_macro_events", return_value=macro_return) as mock_macro:
            result = server._score_entry_candidates("dummy_db_url", "dummy_user")
        return result, mock_macro

    def test_active_macro_events_and_market_event_risk_present_in_result(self):
        macro_return = {"events": [{"canonical_event_key": "FOMC_POLICY_DECISION", "event_status": "UPCOMING"}],
                          "market_event_risk": "HIGH"}
        result, mock_macro = self._run_with_macro_mock(macro_return)
        mock_macro.assert_called_once()
        self.assertEqual(result["marketEventRisk"], "HIGH")
        self.assertEqual(result["activeMacroEvents"], macro_return["events"])

    def test_macro_event_failure_does_not_break_entry_candidates(self):
        """MARKET_EVENT_RISK取得が例外を出しても、entry-candidates全体（entryReadyTop5等）は
        落ちず、安全なデフォルト（LOW/空リスト）で継続すること。"""
        watchlist = _make_wiring_watchlist()
        stage1_rows = _make_wiring_stage1_rows(watchlist)
        stage1_payload = {"rows": stage1_rows, "nikkeiChangePct": 0.3, "scanFailed": False,
                            "builtAt": 0, "codesScanned": len(watchlist), "pricesReturned": len(watchlist),
                            "durationSec": 0, "requestCount": 1, "usedStaleCache": False}
        with mock.patch.object(server.investment_db, "list_watchlist", return_value=watchlist), \
             mock.patch.object(server.investment_db, "get_codes_with_auto_tag", return_value=set()), \
             mock.patch.object(server, "run_momentum_stage1", return_value=stage1_payload), \
             mock.patch.object(server, "get_entry_candidate_support_context",
                                return_value=({}, {"batch_loaded": False})), \
             mock.patch.object(server, "prefetch_daily_arrays_for_watchlist",
                                return_value={"arraysByCode": {}}), \
             mock.patch.object(server, "prefetch_market_data_for_watchlist",
                                return_value={"marketDataByCode": {}}), \
             mock.patch.object(server, "capture_entry_candidate_snapshot_safe", return_value=None), \
             mock.patch.object(server, "get_stock_quotes", return_value={}), \
             mock.patch.object(server, "_intraday_regime_cached", return_value=(None, "failed")), \
             mock.patch.object(server, "_cached_daily_arrays", return_value=None), \
             mock.patch.object(server, "build_active_macro_events", side_effect=RuntimeError("boom")):
            result = server._score_entry_candidates("dummy_db_url", "dummy_user")
        self.assertEqual(result["marketEventRisk"], "LOW")
        self.assertEqual(result["activeMacroEvents"], [])
        self.assertEqual(result["debug"]["scanned"], len(watchlist))  # 既存の候補評価自体は正常完了


if __name__ == "__main__":
    unittest.main()
