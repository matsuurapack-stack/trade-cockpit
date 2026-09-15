# 今買い時TOP5 場中リアルタイム化指示書（2026-09-15）の回帰テスト。
#
# 2026-09-15に15:36頃までまともなENTRY候補が出なかった根本原因はDB N+1・Tachibana個別API
# 呼び出しの性能バグ（本日Phase1〜5で解消済み）だった。ここではその性能修正を再利用しつつ
# 追加した「5分足更新に合わせた自動再評価スケジューラ」「軽量キャッシュ読み出しAPI」
# 「stale data安全弁」「scheduler/手動更新の二重スキャン防止」を検証する。
#
# 実行方法： cd files && python -m unittest test_entry_top5_realtime -v

import threading
import time
import unittest
from unittest import mock

import server


class EntryTop5ScanTimeMarksTests(unittest.TestCase):
    """_entry_top5_scan_time_marks() / ENTRY_TOP5_SCAN_TIMES：5分刻みの発火時刻集合が
    仕様（寄り後09:05から前場終了11:30まで、後場12:30から大引け15:30まで、昼休みは対象外）
    通りに生成されているかを検証する（指示書STEP3・4・STEP5の「昼休みは不要な再計算を
    避ける」）。"""

    def test_morning_window_starts_at_0905_and_ends_at_1130(self):
        marks = server._entry_top5_scan_time_marks(("09:05", "11:30"))
        self.assertEqual(marks[0], "09:05")
        self.assertEqual(marks[-1], "11:30")
        self.assertIn("09:10", marks)
        self.assertIn("09:15", marks)

    def test_afternoon_window_starts_at_1230_and_ends_at_1530(self):
        marks = server._entry_top5_scan_time_marks(("12:30", "15:30"))
        self.assertEqual(marks[0], "12:30")
        self.assertEqual(marks[-1], "15:30")

    def test_lunch_break_is_excluded_from_combined_scan_times(self):
        self.assertNotIn("11:35", server.ENTRY_TOP5_SCAN_TIMES)
        self.assertNotIn("12:00", server.ENTRY_TOP5_SCAN_TIMES)
        self.assertNotIn("12:25", server.ENTRY_TOP5_SCAN_TIMES)

    def test_key_marks_present_in_combined_scan_times(self):
        for hhmm in ("09:05", "09:10", "11:30", "12:30", "13:00", "15:30"):
            self.assertIn(hhmm, server.ENTRY_TOP5_SCAN_TIMES)

    def test_09_04_is_not_a_scan_time(self):
        """指示書STEP18テスト1：09:04時点（最初の5分足未確定）はscan対象外。"""
        self.assertNotIn("09:04", server.ENTRY_TOP5_SCAN_TIMES)


def _make_cache_entry(age_sec, entry_states, cache_statuses=None):
    now = time.time()
    cache_statuses = cache_statuses or ["ok"] * len(entry_states)
    top5 = [{"code": f"{1000+i}", "entryState": st, "entryScore": 70, "marketDataCacheStatus": cs}
            for i, (st, cs) in enumerate(zip(entry_states, cache_statuses))]
    return {
        "entryReadyTop5": top5,
        "watchCandidates": [],
        "dataQuality": "FULL",
        "generatedAt": "2026-09-15T00:00:00+00:00",
        "generatedAtEpoch": now - age_sec,
        "marketDataAt": "2026-09-15T00:00:00+00:00",
        "watchlistCount": 56,
        "readyCount": 40,
        "scoredCount": 40,
        "entryReadyCount": sum(1 for s in entry_states if s in ("NOW_BUYABLE", "ENTRY_READY")),
        "waitCount": 5,
        "riskCount": 1,
        "durationMs": 1234,
        "trigger": "SCHEDULER",
    }


class EntryTop5StalenessTests(unittest.TestCase):
    """_apply_entry_top5_staleness()：ranking_generated_at（=market_data_at）の経過時間に
    応じてENTRY READY等を安全側へ倒すロジックを検証する（指示書「stale data対策」）。"""

    def test_fresh_cache_is_not_stale_or_delayed(self):
        entry = _make_cache_entry(age_sec=5, entry_states=["NOW_BUYABLE", "WAIT_PULLBACK"])
        result = server._apply_entry_top5_staleness(entry)
        self.assertFalse(result["dataStale"])
        self.assertFalse(result["updateDelayWarning"])
        self.assertEqual(result["entryReadyTop5"][0]["entryState"], "NOW_BUYABLE")

    def test_age_over_warning_threshold_sets_update_delay_warning_only(self):
        entry = _make_cache_entry(age_sec=server.ENTRY_TOP5_UPDATE_DELAY_WARNING_SEC + 30,
                                   entry_states=["ENTRY_READY"])
        result = server._apply_entry_top5_staleness(entry)
        self.assertTrue(result["updateDelayWarning"])
        self.assertFalse(result["dataStale"])
        self.assertEqual(result["entryReadyTop5"][0]["entryState"], "ENTRY_READY")  # まだ降格しない

    def test_age_over_stale_threshold_downgrades_now_buyable_and_entry_ready(self):
        entry = _make_cache_entry(age_sec=server.ENTRY_TOP5_STALE_DATA_SEC + 30,
                                   entry_states=["NOW_BUYABLE", "ENTRY_READY", "WAIT_PULLBACK"])
        result = server._apply_entry_top5_staleness(entry)
        self.assertTrue(result["dataStale"])
        self.assertTrue(result["updateDelayWarning"])
        states = [c["entryState"] for c in result["entryReadyTop5"]]
        self.assertEqual(states, ["WAIT_DATA_STALE", "WAIT_DATA_STALE", "WAIT_PULLBACK"])
        self.assertTrue(result["entryReadyTop5"][0]["staleDowngraded"])
        self.assertNotIn("staleDowngraded", result["entryReadyTop5"][2])

    def test_staleness_check_does_not_mutate_original_cache_entry(self):
        entry = _make_cache_entry(age_sec=server.ENTRY_TOP5_STALE_DATA_SEC + 30, entry_states=["ENTRY_READY"])
        server._apply_entry_top5_staleness(entry)
        self.assertEqual(entry["entryReadyTop5"][0]["entryState"], "ENTRY_READY")  # キャッシュ本体は無変更

    def test_fresh_ranking_but_stale_symbol_market_data_still_downgrades(self):
        """レビュー指摘の核心：ranking_generated_at自体は新しい（age_sec=5）が、その銘柄の
        quote/5分足取得が失敗してstale_cacheフォールバック値を使っていた場合、ranking年齢
        だけでは検出できない。marketDataCacheStatusを個別に見て安全側へ倒すことを確認する。"""
        entry = _make_cache_entry(age_sec=5, entry_states=["NOW_BUYABLE", "ENTRY_READY", "WAIT_PULLBACK"],
                                   cache_statuses=["stale_cache", "ok", "stale_cache"])
        result = server._apply_entry_top5_staleness(entry)
        self.assertFalse(result["dataStale"])  # ranking全体は新しいまま
        self.assertTrue(result["anySymbolMarketDataStale"])
        states = [c["entryState"] for c in result["entryReadyTop5"]]
        # 1件目（NOW_BUYABLE・stale_cache）だけ降格、2件目（ENTRY_READY・ok）はそのまま、
        # 3件目（WAIT_PULLBACK・stale_cache）はもともとNOW_BUYABLE/ENTRY_READYではないため降格対象外
        self.assertEqual(states, ["WAIT_DATA_STALE", "ENTRY_READY", "WAIT_PULLBACK"])
        self.assertEqual(result["entryReadyTop5"][0]["staleReason"], "symbol_market_data")

    def test_rate_limited_symbol_is_also_treated_as_stale(self):
        entry = _make_cache_entry(age_sec=5, entry_states=["ENTRY_READY"], cache_statuses=["rate_limited"])
        result = server._apply_entry_top5_staleness(entry)
        self.assertEqual(result["entryReadyTop5"][0]["entryState"], "WAIT_DATA_STALE")

    def test_all_ok_symbols_report_no_symbol_staleness(self):
        entry = _make_cache_entry(age_sec=5, entry_states=["NOW_BUYABLE", "ENTRY_READY"])
        result = server._apply_entry_top5_staleness(entry)
        self.assertFalse(result["anySymbolMarketDataStale"])


class EntryTop5ScanAndCacheTests(unittest.TestCase):
    """_run_entry_top5_scan() / get_entry_top5_cached()：スキャン結果のキャッシュ保存・
    diagnostics変換・scheduler/手動更新の二重スキャン防止を検証する。"""

    def setUp(self):
        server._ENTRY_TOP5_CACHE.clear()
        # 前のテストが例外で終わってロックを持ったままにならないよう安全側でリセットする
        if server._ENTRY_TOP5_SCAN_LOCK.locked():
            server._ENTRY_TOP5_SCAN_LOCK.release()

    def tearDown(self):
        server._ENTRY_TOP5_CACHE.clear()
        if server._ENTRY_TOP5_SCAN_LOCK.locked():
            server._ENTRY_TOP5_SCAN_LOCK.release()

    def _fake_score_result(self):
        return {
            "entryReadyTop5": [{"code": "7203", "entryState": "ENTRY_READY", "entryScore": 66}],
            "watchCandidates": [{"code": "9984", "entryState": "WATCH", "entryScore": 40}],
            "dataQuality": "FULL",
            "generatedAt": "2026-09-15T01:00:00+00:00",
            "debug": {"watchlistCount": 56, "readyCount": 43, "scanned": 43, "entry_ready": 1,
                      "active_break": 2, "watch_near_ready": 3, "risk_excluded": 1},
        }

    def test_scan_stores_cache_with_mapped_diagnostics(self):
        with mock.patch.object(server, "_score_entry_candidates", return_value=self._fake_score_result()) as m:
            cache_entry = server._run_entry_top5_scan("dummy-db-url", "user1", trigger="SCHEDULER")
        m.assert_called_once_with("dummy-db-url", "user1")
        self.assertEqual(cache_entry["watchlistCount"], 56)
        self.assertEqual(cache_entry["readyCount"], 43)
        self.assertEqual(cache_entry["scoredCount"], 43)
        self.assertEqual(cache_entry["entryReadyCount"], 1)
        self.assertEqual(cache_entry["waitCount"], 5)  # active_break(2) + watch_near_ready(3)
        self.assertEqual(cache_entry["riskCount"], 1)
        self.assertEqual(cache_entry["trigger"], "SCHEDULER")
        self.assertIsNotNone(cache_entry["durationMs"])
        self.assertEqual(server.get_entry_top5_cached("user1")["entryReadyCount"], 1)

    def test_get_entry_top5_cached_returns_none_when_absent(self):
        self.assertIsNone(server.get_entry_top5_cached("nobody"))

    def test_scheduler_trigger_skips_when_lock_already_held(self):
        """指示書STEP16・17：scheduler tick中にもう1本schedulerが走ろうとしても二重実行しない
        （非ブロッキング、即座に諦めて既存キャッシュを返す＝処理を積み重ねない）。"""
        server._ENTRY_TOP5_SCAN_LOCK.acquire()
        try:
            with mock.patch.object(server, "_score_entry_candidates") as m:
                result = server._run_entry_top5_scan("dummy-db-url", "user1", trigger="SCHEDULER", wait_for_lock=False)
            m.assert_not_called()
            self.assertIsNone(result)  # まだキャッシュが無ければNone（既存キャッシュがあればそれを返す）
        finally:
            server._ENTRY_TOP5_SCAN_LOCK.release()

    def test_manual_trigger_waits_for_lock_then_scans(self):
        """指示書「手動更新は必要に応じて即時再評価」：ロックが空けば必ずフルスキャンする。"""
        with mock.patch.object(server, "_score_entry_candidates", return_value=self._fake_score_result()) as m:
            cache_entry = server._run_entry_top5_scan("dummy-db-url", "user1", trigger="MANUAL", wait_for_lock=True)
        m.assert_called_once()
        self.assertEqual(cache_entry["trigger"], "MANUAL")

    def test_manual_trigger_waits_for_concurrent_scan_to_finish(self):
        """自動更新（scheduler）実行中に手動ボタンが押されても、二重スキャンせず
        スキャン完了を待ってから1本だけ実行される。"""
        release_after = threading.Event()
        call_order = []

        def slow_scan(_db, _user):
            call_order.append("scheduler-scan-start")
            release_after.wait(timeout=2)
            call_order.append("scheduler-scan-end")
            return self._fake_score_result()

        def manual_scan(_db, _user):
            call_order.append("manual-scan")
            return self._fake_score_result()

        # schedulerスレッドを先に走らせてロックを握らせる
        t = threading.Thread(target=lambda: server._run_entry_top5_scan(
            "dummy-db-url", "user1", trigger="SCHEDULER", wait_for_lock=False))
        with mock.patch.object(server, "_score_entry_candidates", side_effect=slow_scan):
            t.start()
            time.sleep(0.05)  # schedulerがロックを取得しスキャンに入るのを待つ
            self.assertTrue(server._ENTRY_TOP5_SCAN_LOCK.locked())
            with mock.patch.object(server, "_score_entry_candidates", side_effect=manual_scan):
                release_after.set()  # schedulerのスキャンを完了させる
                result = server._run_entry_top5_scan("dummy-db-url", "user1", trigger="MANUAL", wait_for_lock=True)
            t.join(timeout=2)
        self.assertEqual(result["trigger"], "MANUAL")
        self.assertIn("scheduler-scan-end", call_order)


if __name__ == "__main__":
    unittest.main()
