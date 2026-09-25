# Catalyst Confirmation（Phase F）のサーバー統合テスト：既存資産の再利用・非同期・トリガー・ログ/集計・既存判定への影響ゼロ。
#   cd files && python -m unittest test_catalyst_server -v

import datetime
import inspect
import json
import time
import unittest
from unittest import mock

import catalyst_engine as ce
import catalyst_lookup as cl
import chart_signal_log as sl
import server
from test_movement_integration import ShadowRefreshTests as _Base, pool_cand

JST = datetime.timezone(datetime.timedelta(hours=9))


def reset():
    server._CATALYST_SVC.clear()
    server._CATALYST_PREV.clear()
    server._CATALYST_HOLDINGS.clear()
    server._TDNET_DAY_CACHE.clear()
    server._EARNINGS_CACHE.clear()
    server._CATALYST_HOLDINGS.update({u: (time.time() + 3600, set()) for u in ("u1", "u9", "nobody")})     # テストでDB(list_portfolio)へ接続しない
    server._REGULATION.update({"date": None, "flags": None, "prev_active": None, "prev_known": False, "fetched_at": None, "error": None,
                               "duration_ms": None, "count": 0, "active": 0})


class TdnetTests(unittest.TestCase):
    def setUp(self):
        reset()

    def test_past_days_cached_forever_today_short_ttl_and_maps_titles(self):
        today = datetime.datetime.now(JST).strftime("%Y%m%d")
        calls = []
        def fake(ds):
            calls.append(ds)
            return {"6270": [{"time": "09:05", "date": ds, "title": "通期業績予想の上方修正に関するお知らせ", "url": "http://x/" + ds}]}
        with mock.patch.object(server, "_tdnet_disclosures_for_date", side_effect=fake), \
                mock.patch.object(server, "_is_jp_market_business_day", return_value=True):
            rows = server._catalyst_tdnet_fetch("6270")
            self.assertEqual(len(rows), 6)                                    # 直近6営業日ぶん
            self.assertEqual({r["source"] for r in rows}, {"TDNET"})
            self.assertEqual(rows[0]["published_at"].hour, 9)
            self.assertIsNotNone(rows[0]["published_at"].tzinfo)
            n = len(calls)
            server._catalyst_tdnet_fetch("6270")
            self.assertEqual(len(calls), n)                                   # 5分以内は再取得しない（既存のTDnet取得を使い回す）
            self.assertEqual(server._catalyst_tdnet_fetch("9999"), [])

    def test_empty_business_day_today_is_a_failure_not_no_news(self):
        with mock.patch.object(server, "_tdnet_disclosures_for_date", return_value={}), \
                mock.patch.object(server, "_is_jp_market_business_day", return_value=True):
            with self.assertRaises(RuntimeError):
                server._catalyst_tdnet_fetch("6270")


class NewsAndDbTests(unittest.TestCase):
    def setUp(self):
        reset()

    def test_tachibana_stock_news_is_reused(self):
        heads = [{"id": "1", "date": datetime.datetime.now(JST).strftime("%Y%m%d"), "time": "0930", "codes": ["6270"], "headline": "6270、大型受注を発表"}]
        with mock.patch.object(server, "tachibana_api") as api:
            api.get_stock_news.return_value = heads
            rows = server._catalyst_news_fetch("6270")
        self.assertEqual((rows[0]["title"], rows[0]["source"], rows[0]["published_at"].minute), ("6270、大型受注を発表", "NQN", 30))
        self.assertEqual(api.get_stock_news.call_args[0][0], "6270")

    def test_tdnet_derived_headlines_from_tachibana_are_primary_sources(self):
        d = datetime.datetime.now(JST).strftime("%Y%m%d")
        heads = [{"id": "1", "date": d, "time": "0800", "codes": ["627A"], "headline": "<TDnet>AI: アキッパ(627A) 主要株主の異動に関するお知らせ"},
                 {"id": "2", "date": d, "time": "0900", "codes": ["627A"], "headline": "アキッパ、9月の売上高が最高"}]
        with mock.patch.object(server, "tachibana_api") as api:
            api.get_stock_news.return_value = heads
            rows = server._catalyst_news_fetch("627A")
        self.assertEqual([r["source"] for r in rows], ["TACHIBANA_DISCLOSURE", "NQN"])
        self.assertEqual(ce.source_confidence(rows[0]["source"]), "HIGH")
        self.assertEqual(ce.source_confidence(rows[1]["source"]), "MEDIUM")

    def test_lookback_reaches_back_over_a_holiday_streak(self):
        def fake(ds):
            return {"6270": [{"time": "09:00", "date": ds, "title": "資本業務提携に関するお知らせ", "url": "u"}]} if ds.endswith("18") else {"1": [{"time": "09:00", "date": ds, "title": "x", "url": "u"}]}
        n = datetime.datetime.now(JST)
        holiday = lambda d: d.strftime("%Y%m%d") not in {(n - datetime.timedelta(days=k)).strftime("%Y%m%d") for k in (1, 2, 3, 4)}
        with mock.patch.object(server, "_tdnet_disclosures_for_date", side_effect=fake), mock.patch.object(server, "_is_jp_market_business_day", side_effect=holiday):
            server._TDNET_DAY_CACHE.clear()
            rows = server._catalyst_tdnet_fetch("6270")
        self.assertIsInstance(rows, list)                                    # 連休（4日）を挟んでも例外なく遡れる

    def test_existing_news_catalysts_table_is_reused(self):
        row = {"title": "6270の提携", "catalyst_date": "2026-09-26", "verification_status": "VERIFIED", "affected_stocks": ["6270"]}
        macro = {"title": "米国 AI半導体への資金流入", "catalyst_date": "2026-09-26", "verification_status": "UNVERIFIED", "affected_stocks": []}
        other = {"title": "別銘柄の材料", "catalyst_date": "2026-09-26", "affected_stocks": ["1111"]}
        with mock.patch.object(server, "investment_db") as db, mock.patch.object(server, "DATABASE_URL", "url"):
            db.relevant_catalysts_for.return_value = [row, macro, other, {"title": "x", "catalyst_date": "bad", "affected_stocks": ["6270"]}]
            rows = server._catalyst_db_fetch_for("u1")("6270")
        self.assertEqual([(r["title"], r["source"], r["verified"]) for r in rows], [("6270の提携", "DB_VERIFIED", True)])   # 市場全体・他銘柄の材料は除外


class EarningsTests(unittest.TestCase):
    def setUp(self):
        reset()

    def test_market_events_then_yfinance_then_unknown(self):
        today = datetime.datetime.now(JST).date()
        with mock.patch.object(server, "investment_db") as db, mock.patch.object(server, "DATABASE_URL", "url"):
            db.upcoming_event_signals.return_value = {"events": [{"event_type": "EARNINGS", "event_date": (today + datetime.timedelta(days=2)).isoformat()},
                                                                 {"event_type": "ECONOMIC", "event_date": today.isoformat()}]}
            self.assertEqual(server._catalyst_earnings_for("u1")("6270"), today + datetime.timedelta(days=2))
        reset()
        with mock.patch.object(server, "investment_db", None), mock.patch.object(server, "yf", mock.MagicMock()), \
                mock.patch.object(server, "_days_to_earnings", return_value=4):
            self.assertEqual(server._catalyst_earnings_for("u1")("6270"), today + datetime.timedelta(days=4))
        reset()
        with mock.patch.object(server, "investment_db", None), mock.patch.object(server, "yf", mock.MagicMock()), \
                mock.patch.object(server, "_days_to_earnings", return_value=None) as d2e:
            with self.assertRaises(RuntimeError):
                server._catalyst_earnings_for("u1")("6270")                    # 決算日が分からない＝推測しない
            with self.assertRaises(RuntimeError):
                server._catalyst_earnings_for("u1")("6270")
            self.assertEqual(d2e.call_count, 1)                                # 同日は再取得しない


class RegulationTests(unittest.TestCase):
    def setUp(self):
        reset()

    FLAGS = {"7777": {"sSokuzituNyukinC": "1", "sSinyouSyutyuKubun": "0"}, "8888": {"sSokuzituNyukinC": "0"}}

    def test_load_once_per_day_compare_with_previous_snapshot_and_save(self):
        with mock.patch.object(server, "tachibana_api") as api, mock.patch.object(server, "investment_db") as db, \
                mock.patch.object(server, "DATABASE_URL", "url"), mock.patch.object(server, "WRITE_E2E_ALLOWED", True):
            api.get_issue_regulation_kabu.return_value = self.FLAGS
            db.load_margin_restriction_active.return_value = (True, {"8888": ["MARGIN_DEPOSIT_SAME_DAY"]})
            f7, p7 = server._catalyst_regulation("7777")
            f8, p8 = server._catalyst_regulation("8888")
            self.assertEqual((f7["sSokuzituNyukinC"], p7), ("1", False))       # 今日から規制（前営業日は無し）→ NEW_RESTRICTION
            self.assertEqual(p8, True)                                          # 前営業日は規制あり・今日は無し → RELEASED
            self.assertEqual(ce.margin_restriction_from_flags(f7, p7)["state"], "NEW_RESTRICTION")
            self.assertEqual(ce.margin_restriction_from_flags(f8, p8)["state"], "RELEASED")
            self.assertEqual(api.get_issue_regulation_kabu.call_count, 1)      # 全銘柄1回・営業日の朝に1回だけ
            self.assertEqual(server._catalyst_regulation("0000"), (None, None))   # 応答に無い銘柄は判定しない
            saved = db.save_margin_restriction_snapshot.call_args[0]
            self.assertEqual(set(saved[2]), {"7777"})                            # 有効な銘柄だけ保存
            self.assertEqual(server._REGULATION["active"], 1)

    def test_failure_is_cached_so_lookups_fail_fast_instead_of_waiting_20s_each(self):
        with mock.patch.object(server, "tachibana_api") as api:
            api.get_issue_regulation_kabu.side_effect = RuntimeError("p_errno=-1 引数エラー")
            with self.assertRaises(RuntimeError):
                server._catalyst_regulation("7777")
            with self.assertRaises(RuntimeError) as cm:
                server._catalyst_regulation("8888")
            self.assertIn("再試行待ち", str(cm.exception))
            self.assertEqual(api.get_issue_regulation_kabu.call_count, 1)      # 30分は再試行しない（毎回長い待ちを発生させない）
            server._REGULATION["failed_at"] = time.time() - 31 * 60
            with self.assertRaises(RuntimeError):
                server._catalyst_regulation("7777")
            self.assertEqual(api.get_issue_regulation_kabu.call_count, 2)

    def test_previous_snapshot_unknown_gives_none_and_failure_is_recorded(self):
        with mock.patch.object(server, "tachibana_api") as api, mock.patch.object(server, "investment_db") as db, \
                mock.patch.object(server, "DATABASE_URL", "url"), mock.patch.object(server, "WRITE_E2E_ALLOWED", False):
            api.get_issue_regulation_kabu.return_value = self.FLAGS
            db.load_margin_restriction_active.return_value = (False, {})
            self.assertEqual(server._catalyst_regulation("7777")[1], None)
            db.save_margin_restriction_snapshot.assert_not_called()
        reset()
        with mock.patch.object(server, "tachibana_api") as api:
            api.get_issue_regulation_kabu.side_effect = RuntimeError("p_errno=2")
            with self.assertRaises(RuntimeError):
                server._catalyst_regulation("7777")
            self.assertIn("p_errno", server._REGULATION["error"])


class FakeSvc:
    def __init__(self, snaps=None):
        self.requests, self.snaps = [], snaps or {}

    def request(self, code, reason, anomaly=None):
        self.requests.append((code, reason, anomaly))
        return "QUEUED"

    def get(self, code):
        return self.snaps.get(code)

    def summary(self):
        return {}


def strong_snap():
    now = datetime.datetime.now(JST)
    items = [ce.build_item("通期業績予想の上方修正", "TDNET", now, now), ce.build_item("自己株式取得に係る事項の決定", "TDNET", now, now)]
    return ce.build_snapshot("6270", items, now, lookup={"tdnet": True, "news": True, "db": True},
                             margin={"state": "NONE", "kinds": []}, earnings_next=now.date() + datetime.timedelta(days=30))


class TriggerTests(unittest.TestCase):
    def setUp(self):
        reset()

    def test_only_anomalies_are_requested_and_calls_are_nonblocking(self):
        svc = FakeSvc()
        pool = [pool_cand("QUIET"), pool_cand("RAD", rollingState="SINGLE_BAR_SURGE", changePct=6.0),
                pool_cand("EXP", activityState="EXPANDING", movementFeatures={"volRatioRecent": 4.0})]
        with server._ENTRY_TOP5_CACHE_LOCK:
            server._ENTRY_TOP5_CACHE["u9"] = {"entryReadyTop5": [{"code": "QUIET"}], "actionableTop5": [], "analysisTop5": []}
        with mock.patch.object(server, "_CATALYST_AUTOSTART", True), mock.patch.object(server, "catalyst_service", return_value=svc), mock.patch.object(server, "investment_db", None):
            n = server.catalyst_request_triggers("u9", pool, [])
            self.assertEqual(n, 2)
            self.assertEqual({r[0] for r in svc.requests}, {"RAD", "EXP"})     # 何も起きていないQUIETは調べない
            self.assertEqual(svc.requests[0][2]["chgPct"], 6.0)
            svc.requests.clear()
            with server._ENTRY_TOP5_CACHE_LOCK:                                # TOP5に新規採用された銘柄
                server._ENTRY_TOP5_CACHE["u9"]["entryReadyTop5"].append({"code": "NEW"})
            server.catalyst_request_triggers("u9", pool + [pool_cand("NEW")], [])
            self.assertIn(("NEW", "TOP5_NEW"), [(r[0], r[1]) for r in svc.requests])

    def test_dynamic_promotion_and_holding_move_trigger(self):
        svc = FakeSvc()
        with mock.patch.object(server, "_CATALYST_AUTOSTART", True), mock.patch.object(server, "catalyst_service", return_value=svc), mock.patch.object(server, "_catalyst_holdings", return_value={"HOLD"}):
            with server._ENTRY_TOP5_CACHE_LOCK:
                server._ENTRY_TOP5_CACHE["u9"] = {}
            server.catalyst_request_triggers("u9", [pool_cand("HOLD", changePct=-4.0), pool_cand("X")], ["PROMO"])
        reasons = {r[0]: r[1] for r in svc.requests}
        self.assertEqual(reasons["HOLD"], "HOLDING_MOVE")
        self.assertEqual(reasons["PROMO"], "DYNAMIC_PROMOTED")                # Discoveryから昇格した登録外銘柄も調べる
        self.assertNotIn("X", reasons)

    def test_startup_enables_autostart_globally(self):
        with mock.patch.object(server, "_CATALYST_AUTOSTART", False), mock.patch.object(server, "_morning_check_scheduler_users", return_value=["u1"]),                 mock.patch.object(server, "catalyst_service") as cs:
            server._enable_catalyst_autostart()
            self.assertTrue(server._CATALYST_AUTOSTART)                        # main()内のローカル変数ではなくモジュール変数が更新される
            cs.assert_called_once_with("u1")

    def test_no_service_means_no_lookup_and_no_network(self):
        with mock.patch.object(server, "catalyst_service") as cs:
            self.assertEqual(server.catalyst_request_triggers("nobody", [pool_cand("RAD", rollingState="ROLLING_SURGE")], []), 0)
            cs.assert_not_called()                                              # 起動時以外はサービスを自動生成しない

    def test_errors_are_swallowed(self):
        with mock.patch.object(server, "_CATALYST_AUTOSTART", True), mock.patch.object(server, "catalyst_service", side_effect=RuntimeError("boom")):
            self.assertEqual(server.catalyst_request_triggers("u9", [pool_cand("RAD", rollingState="ROLLING_SURGE")], []), 0)


class ViewTests(unittest.TestCase):
    def setUp(self):
        reset()

    def view(self, snap, **cand):
        server._CATALYST_SVC["u1"] = FakeSvc({"6270": snap} if snap is not None else {})
        c = pool_cand("6270", **cand)
        return server._catalyst_view("u1", c)

    def test_pending_none_and_state_passthrough(self):
        self.assertIsNone(self.view(None))
        v = self.view({"code": "6270", "state": "PENDING", "reason": "RADAR"})
        self.assertEqual((v["state"], v["verdict"]), ("PENDING", "CATALYST_PENDING"))

    def test_strong_catalyst_with_chase_is_not_now_and_good_chart_is_high_confidence(self):
        chase = self.view(strong_snap(), chartPattern="CHASE", entryTimingScore=24, movementScore=70, changePct=5.0)
        self.assertEqual(chase["verdict"], "CATALYST_STRONG_BUT_NOT_NOW")
        self.assertGreaterEqual(chase["score"], 85)
        self.assertEqual(chase["combinedLabel"], "上方修正 + 自社株買い")
        good = self.view(strong_snap(), chartPattern="EARLY_BREAKOUT", entryTimingScore=79, movementScore=82)
        self.assertEqual(good["verdict"], "HIGH_CONFIDENCE")
        self.assertTrue(good["entryConfidence"] >= 75)
        for k in ("state", "direction", "type", "confidence", "ageHours", "earningsState", "marginState", "flags", "exitHints", "stopHint"):
            self.assertIn(k, good)
        json.dumps(good, default=str)

    def test_good_news_bad_reaction_flag_from_chart_features(self):
        v = self.view(strong_snap(), chartPattern="VWAP_LOSS", entryTimingScore=30, movementScore=40, changePct=0.5,
                      movementFeatures={"aboveVwap": False, "lowerHighs": True})
        self.assertIn("GOOD_NEWS_BAD_REACTION", v["flags"])
        self.assertIn("CATALYST_FAILURE_WARNING", v["exitHints"])

    def test_attach_returns_copies_and_leaves_cache_untouched(self):
        server._CATALYST_SVC["u1"] = FakeSvc({"6270": strong_snap()})
        res = {"entryReadyTop5": [pool_cand("6270"), pool_cand("0000")], "watchCandidates": [], "dataQuality": "OK"}
        out = server.attach_catalyst_to_result("u1", res)
        self.assertIn("catalyst", out["entryReadyTop5"][0])
        self.assertNotIn("catalyst", out["entryReadyTop5"][1])              # 未調査の銘柄は付けない
        self.assertNotIn("catalyst", res["entryReadyTop5"][0])
        self.assertIs(server.attach_catalyst_to_result("nobody", res), res)


class RefreshAndLogTests(_Base):
    def setUp(self):
        super().setUp()
        reset()

    def test_refresh_requests_lookups_without_blocking_and_keeps_existing_lists(self):
        def slow(code):
            time.sleep(0.6)
            return []
        svc = cl.CatalystLookup({"tdnet": slow, "news": slow, "db": slow}, ttl_sec=60, cooldown_sec=0)
        svc.start()
        server._CATALYST_SVC["u9"] = svc
        pool = [pool_cand("RAD", movementScore=50, rollingState="SINGLE_BAR_SURGE", rollingHot=True, changePct=6.0)]
        existing = self.put_cache(pool)
        try:
            t0 = time.time()
            with mock.patch.object(server, "investment_db", None):
                server.refresh_shadow_movement("url", "u9")
            self.assertLess(time.time() - t0, 0.4)                              # 材料調査（0.6秒×3）を待たない＝チャート処理をブロックしない
            self.assertEqual(svc.get("RAD")["state"], "PENDING")               # 調査中はCATALYST_PENDING
            cur = server._ENTRY_TOP5_CACHE["u9"]
            for k, v in existing.items():
                self.assertEqual(cur[k], v, k)
        finally:
            svc.stop()

    def test_record_carries_catalyst_fields_and_milestone_and_is_loggable(self):
        server._CATALYST_SVC["u1"] = FakeSvc({"6270": strong_snap()})
        c = pool_cand("6270", chartPattern="CHASE", entryTimingScore=24, movementScore=70)
        c["catalyst"] = server._catalyst_view("u1", c)
        r = sl.build_signal_record("u1", c, datetime.datetime.now(JST), "SCAN")
        self.assertEqual((r["catalyst_state"], r["catalyst_direction"], r["catalyst_type"], r["catalyst_confidence"]), ("CONFIRMED", "POSITIVE", "UPWARD_REVISION", "HIGH"))
        self.assertGreaterEqual(r["catalyst_score"], 85)
        self.assertEqual((r["earnings_state"], r["margin_restriction_state"], r["entry_verdict"]), ("NO_NEAR_EARNINGS", "NONE", "CATALYST_STRONG_BUT_NOT_NOW"))
        self.assertTrue(sl.is_loggable(dict(r, legacy_entry_state="WEAK", chart_entry_state="WEAK")))
        self.assertIn("first_catalyst_at", sl.update_milestones({}, ("u1", "6270"), r, datetime.datetime.now(JST)))


class SummaryTests(unittest.TestCase):
    def row(self, code, ret15, chart="ENTRY_READY", pattern="EARLY_BREAKOUT", direction="POSITIVE", score=85, state="CONFIRMED",
            flags=None, earn="NO_NEAR_EARNINGS", margin="NONE", momentum=False, mfe=2.0, mae=-0.5):
        p = 1000.0
        return {"code": code, "logged_at": datetime.datetime(2026, 9, 28, 10, int(hash(code) % 50), tzinfo=JST), "current_price": p,
                "chart_pattern": pattern, "chart_entry_state": chart, "legacy_entry_state": chart, "catalyst_state": state,
                "catalyst_score": score, "catalyst_direction": direction, "earnings_state": earn, "margin_restriction_state": margin,
                "momentum_mode": momentum, "entry_verdict": "NORMAL", "price_5m": p * 1.001, "price_15m": p * (1 + ret15 / 100),
                "price_30m": p * (1 + ret15 / 100), "max_30m": p * (1 + mfe / 100), "min_30m": p * (1 + mae / 100),
                "movement": {"catalyst": {"flags": flags or []}}, "context": {}}

    def test_combination_groups(self):
        rows = [self.row("A", 1.0),                                                           # 強い材料 + ENTRY_READY
                self.row("B", -1.0, chart="WAIT_PULLBACK", pattern="CHASE"),                  # 強い材料 + CHASE
                self.row("C", -0.5, direction="UNKNOWN", score=0, state="NONE_FOUND"),        # 材料なし + ENTRY_READY
                self.row("D", -0.2, earn="PRE_EARNINGS", direction="UNKNOWN", score=0, state="NONE_FOUND"),
                self.row("E", -2.0, margin="ACTIVE", momentum=True, chart="WATCH", pattern="BASE_BUILDING", score=10, direction="UNKNOWN"),
                self.row("F", -1.5, pattern="FAILED_BREAKOUT", chart="WATCH"),                # 好材料 + 失敗ブレイク
                self.row("G", 3.0, direction="NEGATIVE", score=60, chart="WATCH", pattern="BASE_BUILDING", flags=["BAD_NEWS_STRONG_PRICE"])]
        s = sl.summarize_catalyst(rows)
        c = s["combinations"]
        self.assertEqual(c["strong_catalyst_and_ENTRY_READY"]["n"], 1)
        self.assertEqual(c["strong_catalyst_and_CHASE"]["avg_ret_15m"], -1.0)
        self.assertEqual(c["no_catalyst_and_ENTRY_READY"]["n"], 2)                          # C, D
        self.assertEqual(c["pre_earnings_and_ENTRY_READY"]["n"], 1)
        self.assertEqual(c["margin_restriction_and_momentum"]["n"], 1)
        self.assertEqual(c["positive_catalyst_and_FAILED_BREAKOUT"]["n"], 1)
        self.assertEqual(c["negative_catalyst_and_price_strength"]["avg_ret_15m"], 3.0)
        self.assertEqual(s["states"]["NONE_FOUND"], 2)
        self.assertEqual(s["flags"]["BAD_NEWS_STRONG_PRICE"]["n"], 1)
        self.assertEqual(s["earnings_states"]["PRE_EARNINGS"], 1)
        self.assertEqual(s["margin_states"]["ACTIVE"], 1)
        json.dumps(sl.summarize_day(rows), ensure_ascii=False, default=str)


class IsolationTests(unittest.TestCase):
    def test_entry_side_code_never_references_catalyst(self):
        for obj in (server._compute_price_dependent_entry_fields, server.rescore_entry_candidate_with_quote, server._select_entry_ready_top5,
                    server._movement_candidate_fields):
            src = inspect.getsource(obj).lower()
            self.assertNotIn("catalyst_engine", src)
            self.assertNotIn("catalyst_service", src)
            self.assertNotIn("_catalyst_", src)

    def test_pure_entry_modules_do_not_import_catalyst(self):
        import chart_context, movement_potential, early_radar, rolling_radar, market_discovery
        for mod in (chart_context, movement_potential, early_radar, rolling_radar, market_discovery):
            self.assertNotIn("import catalyst", inspect.getsource(mod))

    def test_probe_throttle_and_api_payload(self):
        reset()
        server._CATALYST_PROBE["last"] = time.time()
        self.assertEqual(server.catalyst_probe("u1", "6270")["error"], "throttled")
        p = server.catalyst_api_payload("u1", "6270")
        self.assertIsNone(p["service"])
        self.assertIn("regulation", p)
        json.dumps(p, default=str)

    def test_snapshot_persistence_is_gated(self):
        snap = strong_snap()
        with mock.patch.object(server, "investment_db") as db, mock.patch.object(server, "DATABASE_URL", "url"), mock.patch.object(server, "WRITE_E2E_ALLOWED", True):
            server._catalyst_on_snapshot_for("u1")("6270", snap, "RADAR")
            db.insert_catalyst_snapshot.assert_called_once()
        with mock.patch.object(server, "investment_db") as db, mock.patch.object(server, "DATABASE_URL", "url"), mock.patch.object(server, "WRITE_E2E_ALLOWED", False):
            server._catalyst_on_snapshot_for("u1")("6270", snap, "RADAR")
            db.insert_catalyst_snapshot.assert_not_called()


if __name__ == "__main__":
    unittest.main()
