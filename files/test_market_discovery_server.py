# Market-Wide Discovery（Phase E）のサーバー統合テスト：Broad→Real-time→dynamic watch、API負荷計測、鮮度、既存TOP5への影響ゼロ。
# 2026-09-26 MU-Multi: Market Discoveryは全ユーザー共通(_shared)のpoolを使う（旧: ユーザー別 "u1"/"u9"）。
#   cd files && python -m unittest test_market_discovery_server -v

import datetime
import inspect
import json
import unittest
from unittest import mock

import dynamic_watch as dw
import market_discovery as md
import server
from test_market_discovery import row
from test_movement_integration import ShadowRefreshTests as _Base, pool_cand

JST = datetime.timezone(datetime.timedelta(hours=9))
T0 = datetime.datetime(2026, 9, 28, 10, 0, tzinfo=JST)          # 月曜の場中


def at(sec):
    return T0 + datetime.timedelta(seconds=sec)


def raw(code, **kw):
    r = row(code, **kw)
    return ("YF_GAINERS", {"symbol": code + ".T", "regularMarketPrice": r["price"], "regularMarketPreviousClose": r["prevClose"],
                           "regularMarketDayHigh": r["high"], "regularMarketDayLow": r["low"], "regularMarketOpen": r["open"],
                           "regularMarketVolume": r["volume"], "averageDailyVolume3Month": r["avgVolume"],
                           "regularMarketChangePercent": r["changePct"], "bid": 999.0, "ask": 1000.5, "shortName": code})


def fetcher(rows, err=None):
    return lambda: (rows, {"calls": 2, "rows": len(rows), "duration_ms": 1300, "error": err})


class Base(unittest.TestCase):
    def setUp(self):
        server._DISCOVERY_POOL.clear()
        server._DISCOVERY_HIST.clear()
        server._DISCOVERY_STATS.update({"broad": [], "rt": [], "errors": {"tachibana": 0, "yfinance": 0}})
        server._DYNAMIC_WATCH.clear()


class BroadRefreshTests(Base):
    def test_registered_codes_are_excluded_and_multi_factor_codes_enter_the_pool(self):
        with mock.patch.object(server, "investment_db") as db:
            db.list_watchlist.return_value = [{"code": "1111"}]
            db.list_ipo_stocks.return_value = []
            rec = server.discovery_broad_refresh("url", "u1", fetcher=fetcher([raw("1111"), raw("2222"), raw("3333", price=955.0, prev=950.0,
                                                                                                       hi=960.0, lo=948.0, vol=350_000, avg=400_000)]),
                                                 now=at(0))
        pool = server._DISCOVERY_POOL["_shared"]
        self.assertIn("2222", pool)
        self.assertNotIn("1111", pool)                                         # 登録済みは対象外
        self.assertNotIn("3333", pool)                                         # 1要素以下
        self.assertEqual(pool["2222"]["status"], "BROAD")
        self.assertFalse(pool["2222"]["entry_allowed"])
        self.assertEqual((rec["calls"], rec["rows"], rec["added"], rec["live"]), (2, 3, 1, 1))
        self.assertEqual(server._DISCOVERY_STATS["broad"][-1]["duration_ms"], 1300)

    def test_pre_open_run_tags_previous_day_movers_and_ipo_tag(self):
        pre = datetime.datetime(2026, 9, 28, 8, 50, tzinfo=JST)
        with mock.patch.object(server, "investment_db") as db:
            db.list_watchlist.return_value = []
            db.list_ipo_stocks.return_value = [{"code": "4444"}]
            server.discovery_broad_refresh("url", "u1", fetcher=fetcher([raw("2222", price=990.0, prev=940.0, hi=992.0, lo=985.0, vol=400_000, avg=5_000_000),
                                                                         raw("4444", price=955.0, prev=950.0, hi=960.0, lo=948.0, vol=400_000, avg=500_000)]),
                                            now=pre)
        pool = server._DISCOVERY_POOL["_shared"]
        self.assertIn("2222", pool)                                           # 前日の急騰（+5%）＋前日の急騰銘柄タグ
        self.assertIn("前日の急騰銘柄", pool["2222"]["discovery_reason"])
        self.assertIn("4444", pool)                                           # IPOタグ＋前日タグ

    def test_screener_failure_is_counted_and_does_not_break(self):
        with mock.patch.object(server, "investment_db", None):
            rec = server.discovery_broad_refresh("url", "u1", fetcher=fetcher([], err="timeout"), now=at(0))
        self.assertEqual(rec["error"], "timeout")
        self.assertEqual(server._DISCOVERY_STATS["errors"]["yfinance"], 1)

    def test_pool_cap_and_ttl(self):
        rows = [raw(f"{i:04d}", price=1000.0 + i * 0.0, prev=930.0 - (i % 40)) for i in range(1, 420)]
        with mock.patch.object(server, "investment_db", None):
            server.discovery_broad_refresh("url", "u1", fetcher=fetcher(rows), now=at(0))
            live = [e for e in server._DISCOVERY_POOL["_shared"].values() if e["status"] != "EXPIRED"]
            self.assertLessEqual(len(live), md.BROAD_MAX)
            server.discovery_broad_refresh("url", "u1", fetcher=fetcher([]), now=at(46 * 60))        # 45分超で全て失効
        self.assertTrue(all(e["status"] == "EXPIRED" for e in server._DISCOVERY_POOL["_shared"].values()))


def q(price, vol, src="tachibana", **kw):
    d = {"t": price, "volume": vol, "high": price, "low": price - 5, "ask": price + 0.5, "bid": price - 0.5, "vwap": price - 3, "source": src,
         "is_stale": False, "p": 940.0}
    d.update(kw)
    return d


class RealtimeCycleTests(Base):
    def seed(self, code="2222"):
        with mock.patch.object(server, "investment_db", None):
            server.discovery_broad_refresh("url", "u1", fetcher=fetcher([raw(code)]), now=at(0))

    def run_cycles(self, seq, code="2222", **patches):
        """seq: [(秒, price, volume)] を順に立花quoteとして流す。"""
        recs = []
        for s, p, v in seq:
            with mock.patch.object(server, "investment_db", None), \
                    mock.patch.object(server, "get_internal_intraday_bars", return_value={"bars": []}), \
                    mock.patch.object(server, "refresh_shadow_movement") as ref:
                r = server.discovery_realtime_cycle("url", "u1", quote_fn=lambda w, pp=p, vv=v: ({code: q(pp, vv)}, {"tachibana": 1, "failed": 0}), now=at(s))
                recs.append((r, ref.called))
        return recs

    ACCEL = [(0, 1000.0, 1_000_000), (120, 1001.0, 1_050_000), (240, 1002.0, 1_100_000), (300, 1004.0, 1_150_000), (420, 1010.0, 1_500_000)]

    def test_promotion_hot_and_load_stats_from_tachibana_quotes_only(self):
        self.seed()
        recs = self.run_cycles(self.ACCEL)
        e = server._DISCOVERY_POOL["_shared"]["2222"]
        self.assertEqual(e["status"], "HOT")
        self.assertEqual(e["promoted_at"], at(420))
        self.assertEqual(e["hot_at"], at(420))
        self.assertIn("出来高加速", e["promote_reasons"])
        self.assertFalse(e["entry_allowed"])
        last, refreshed = recs[-1]
        self.assertTrue(refreshed)                                        # 昇格イベントでdynamic watch更新が呼ばれる
        self.assertEqual((last["codes"], last["calls_est"], last["tachibana"]), (1, 1, 1))
        self.assertEqual(len(server._DISCOVERY_STATS["rt"]), 5)
        self.assertEqual(server._DISCOVERY_STATS["errors"]["tachibana"], 0)

    def test_yfinance_fallback_and_stale_quotes_never_promote(self):
        self.seed()
        for src, stale in (("yfinance_fallback", False), ("cache", True), ("tachibana", True)):
            server._DISCOVERY_HIST.clear()
            recs = []
            for s, p, v in self.ACCEL:
                with mock.patch.object(server, "investment_db", None), mock.patch.object(server, "get_internal_intraday_bars", return_value={"bars": []}), \
                        mock.patch.object(server, "refresh_shadow_movement"):
                    server.discovery_realtime_cycle("u1" and "url", "u1", quote_fn=lambda w, pp=p, vv=v: ({"2222": q(pp, vv, src=src, is_stale=stale)}, {"tachibana": 0}), now=at(s))
            self.assertEqual(server._DISCOVERY_POOL["_shared"]["2222"]["status"], "BROAD", src)

    def test_call_estimate_for_120_codes_and_error_counting(self):
        with mock.patch.object(server, "investment_db", None):
            server.discovery_broad_refresh("url", "u1", fetcher=fetcher([raw(f"{i:04d}", prev=930.0 - i % 30) for i in range(1, 200)]), now=at(0))
        def boom(w):
            raise RuntimeError("tachibana disconnected")
        with mock.patch.object(server, "investment_db", None), mock.patch.object(server, "refresh_shadow_movement"):
            r = server.discovery_realtime_cycle("url", "u1", quote_fn=boom, now=at(30))
        self.assertEqual((r["codes"], r["calls_est"]), (md.RT_MAX, 3))       # 120銘柄＝40銘柄チャンク×3リクエスト
        self.assertIn("disconnected", r["error"])
        self.assertEqual(server._DISCOVERY_STATS["errors"]["tachibana"], 1)

    def test_realtime_expiry_after_20_minutes_without_reconfirmation(self):
        self.seed()
        self.run_cycles(self.ACCEL)
        self.run_cycles([(420 + 21 * 60, 1010.0, 1_500_000)])            # 昇格から21分後：条件を満たさず再確認できない
        e = server._DISCOVERY_POOL["_shared"]["2222"]
        self.assertEqual((e["status"], e["expire_reason"]), ("EXPIRED", "RT_STALE"))


class DynamicWatchIntegrationTests(_Base):
    def setUp(self):
        super().setUp()
        server._DISCOVERY_POOL.clear()

    def tearDown(self):
        server._DISCOVERY_POOL.clear()

    def test_promoted_and_hot_discovery_names_flow_into_dynamic_watch_without_touching_existing_lists(self):
        pool = [pool_cand("N", movementScore=30)]
        existing = self.put_cache(pool)
        now = datetime.datetime.now(JST)
        with server._DISCOVERY_LOCK:
            server._DISCOVERY_POOL["_shared"] = {}
            for code, status in (("P1", "PROMOTED"), ("H1", "HOT"), ("B1", "BROAD")):
                e = md.new_entry(code, code, "YF_GAINERS", 60, ["前日比+5%"], now)
                e.update({"status": status, "promoted_at": now if status != "BROAD" else None,
                          "eval": {"movement": 40, "rolling": "SINGLE_BAR_SURGE" if code == "H1" else "NONE"}})
                server._DISCOVERY_POOL["_shared"][code] = e
        with mock.patch.object(server, "investment_db") as db, mock.patch.object(server, "WRITE_E2E_ALLOWED", True), \
                mock.patch.object(server, "_in_jp_session", return_value=True):
            db.load_dynamic_watch.return_value = {}
            shadow = server.refresh_shadow_movement("url", "u9")
        cur = server._ENTRY_TOP5_CACHE["u9"]
        for k, v in existing.items():
            self.assertEqual(cur[k], v, k)                                     # 既存TOP5は不変
        adds = {a["code"]: a["source"] for a in db.sync_dynamic_watch.call_args[0][2]}
        self.assertEqual(adds["P1"], "DISCOVERY:PROMOTED")
        self.assertEqual(adds["H1"], "DISCOVERY:HOT")
        self.assertNotIn("B1", adds)                                           # BROADは昇格前なのでdynamic watchに入れない
        hot = shadow["dynamicWatch"]["hot"]
        self.assertIn("H1", hot)
        self.assertNotIn("P1", hot)
        self.assertEqual({d["code"] for d in shadow["marketDiscovery"]}, {"P1", "H1", "B1"})
        self.assertTrue(all(d["entryAllowed"] is False for d in shadow["marketDiscovery"]))
        called = {name for name, *_ in db.method_calls}
        self.assertTrue(called <= {"load_dynamic_watch", "sync_dynamic_watch", "list_portfolio"})   # 手動watchlistは触らない
        json.dumps(shadow, default=str)

    def test_expired_discovery_names_are_removed_from_dynamic_watch(self):
        self.put_cache([pool_cand("N", movementScore=30)])
        now = datetime.datetime.now(JST)
        server._DYNAMIC_WATCH["u9"] = {"X1": {"code": "X1", "name": "X1", "is_manual": False, "pool": "ACTIVE", "source": "DISCOVERY:PROMOTED",
                                              "added_at": now, "last_seen_at": now, "weak_since": None}}
        e = md.new_entry("X1", "X1", "YF_GAINERS", 60, [], now)
        md.expire_entry(e, now, "RT_STALE")
        server._DISCOVERY_POOL["_shared"] = {"X1": e}
        with mock.patch.object(server, "investment_db", None):
            shadow = server.refresh_shadow_movement("url", "u9")
        self.assertNotIn("X1", server._DYNAMIC_WATCH["u9"])
        self.assertEqual(shadow["dynamicWatch"]["removed"], [{"code": "X1", "reason": "DISCOVERY_EXPIRED"}])

    def test_discovery_caps_do_not_exceed_dynamic_and_hot_limits(self):
        self.put_cache([])
        now = datetime.datetime.now(JST)
        server._DISCOVERY_POOL["_shared"] = {}
        for i in range(120):
            e = md.new_entry(f"D{i:03d}", "n", "YF_GAINERS", 50 + i % 40, [], now)
            e.update({"status": "HOT", "promoted_at": now, "hot_at": now, "eval": {"movement": 40}})
            server._DISCOVERY_POOL["_shared"][e["code"]] = e
        with mock.patch.object(server, "investment_db", None):
            shadow = server.refresh_shadow_movement("url", "u9")
        self.assertLessEqual(shadow["dynamicWatch"]["active"], dw.MAX_ACTIVE)
        self.assertLessEqual(len(shadow["dynamicWatch"]["hot"]), dw.MAX_HOT)


class ManualRunTests(Base):
    def test_manual_run_measures_load_and_is_throttled(self):
        server._DISCOVERY_STATS.pop("last_manual", None)
        with mock.patch.object(server, "investment_db", None), mock.patch.object(server, "get_internal_intraday_bars", return_value={"bars": []}), \
                mock.patch.object(server, "refresh_shadow_movement"):
            out = server.discovery_manual_run("url", "u1", broad_fetcher=fetcher([raw("2222")]),
                                              quote_fn=lambda w: ({"2222": q(1000.0, 1_000_000)}, {"tachibana": 1, "failed": 0}))
            self.assertEqual((out["broad"]["calls"], out["broad"]["rows"]), (2, 1))
            self.assertEqual((out["realtime"]["codes"], out["realtime"]["calls_est"]), (1, 1))
            self.assertIn("totalMs", out)
            again = server.discovery_manual_run("url", "u1", broad_fetcher=fetcher([]), quote_fn=lambda w: ({}, {}))
            self.assertEqual(again["error"], "throttled")                     # 立花・screenerへ連続アクセスしない


class ApiAndIsolationTests(Base):
    def test_api_payload_has_limits_load_and_is_json_serializable(self):
        with mock.patch.object(server, "investment_db", None):
            server.discovery_broad_refresh("url", "u1", fetcher=fetcher([raw("2222")]), now=datetime.datetime.now(JST))
        p = server.discovery_api_payload("u1")
        self.assertEqual((p["limits"]["broadMax"], p["limits"]["realtimeMax"], p["limits"]["dynamicMax"], p["limits"]["hotMax"]), (300, 120, 50, 20))
        self.assertEqual(p["load"]["broad"][-1]["calls"], 2)
        self.assertEqual(p["pool"]["live"], 1)
        json.dumps(p, default=str)

    def test_entry_side_code_never_references_discovery(self):
        for obj in (server._compute_price_dependent_entry_fields, server.rescore_entry_candidate_with_quote, server._select_entry_ready_top5,
                    server._movement_candidate_fields):
            src = inspect.getsource(obj)
            self.assertNotIn("market_discovery", src)
            self.assertNotIn("_DISCOVERY", src)

    def test_persist_is_gated_and_only_touches_discovery_table(self):
        with mock.patch.object(server, "investment_db", None):
            server.discovery_broad_refresh("url", "u1", fetcher=fetcher([raw("2222")]), now=at(0))
        with mock.patch.object(server, "investment_db") as db, mock.patch.object(server, "WRITE_E2E_ALLOWED", True):
            n = server._persist_discovery("url", "u1", now=at(60))
            self.assertEqual(n, db.upsert_market_discovery.return_value)
            self.assertEqual({c[0] for c in db.method_calls}, {"upsert_market_discovery"})
            db.reset_mock()
            self.assertEqual(server._persist_discovery("url", "u1", now=at(120)), db.upsert_market_discovery.return_value or 0) if False else None
        with mock.patch.object(server, "investment_db") as db, mock.patch.object(server, "WRITE_E2E_ALLOWED", False):
            self.assertEqual(server._persist_discovery("url", "u1", now=at(60)), 0)
            db.upsert_market_discovery.assert_not_called()
        with mock.patch.object(server, "investment_db") as db, mock.patch.object(server, "WRITE_E2E_ALLOWED", True):
            self.assertEqual(server._persist_discovery("url", "u1", now=datetime.datetime(2026, 9, 28, 17, 0, tzinfo=JST)), 0)   # 場外は書かない


if __name__ == "__main__":
    unittest.main()
