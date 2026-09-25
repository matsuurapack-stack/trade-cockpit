# Market-Wide Discovery（Phase E）の純粋ロジックのテスト。 cd files && python -m unittest test_market_discovery -v

import datetime
import inspect
import unittest

import chart_context
import early_radar
import market_discovery as md
import movement_potential
import rolling_radar
from test_early_radar import build

JST = datetime.timezone(datetime.timedelta(hours=9))
T0 = datetime.datetime(2026, 9, 28, 10, 0, tzinfo=JST)


def at(sec):
    return T0 + datetime.timedelta(seconds=sec)


def row(code="6270", price=1000.0, prev=940.0, hi=1005.0, lo=950.0, vol=1_000_000, avg=300_000, bid=999.0, ask=1000.5, **kw):
    r = {"symbol": code + ".T", "regularMarketPrice": price, "regularMarketPreviousClose": prev, "regularMarketDayHigh": hi,
         "regularMarketDayLow": lo, "regularMarketOpen": 950.0, "regularMarketVolume": vol, "averageDailyVolume3Month": avg,
         "regularMarketChangePercent": (price / prev - 1) * 100, "bid": bid, "ask": ask, "shortName": code, "marketCap": 5e10,
         "marketState": "REGULAR", "exchangeDataDelayedBy": 20}
    r.update(kw)
    return md.normalize_screener_row(r)


class BroadTests(unittest.TestCase):
    def test_multiple_factors_required(self):
        # 前日比+6.4%・相対出来高(時間調整)・値幅・高値圏 → 多要素
        s = md.broad_score(row(), minutes_since_open=60)
        self.assertIsNotNone(s)
        score, factors, reasons = s
        self.assertGreaterEqual(len(factors), 2)
        self.assertIn("gainer", factors)
        self.assertTrue(any("前日比" in r for r in reasons))
        # 前日比だけ（出来高は通常・値幅小・高値圏でもない）→ 1要素なので入れない
        one = row(price=975.0, prev=940.0, hi=1000.0, lo=970.0, vol=100_000, avg=300_000)
        self.assertIsNone(md.broad_score(one, minutes_since_open=240))

    def test_liquidity_and_spread_filters(self):
        self.assertIsNone(md.broad_score(row(vol=10_000, avg=1000), minutes_since_open=60))              # 売買代金が小さい
        self.assertIsNone(md.broad_score(row(bid=980.0, ask=1000.0), minutes_since_open=60))              # spread 2%
        self.assertIsNone(md.broad_score(row(price=30.0, prev=28.0, hi=30.0, lo=28.0), minutes_since_open=60))

    def test_time_adjusted_relative_volume(self):
        base = dict(price=1000.0, prev=985.0, hi=1004.0, lo=990.0, vol=400_000, avg=1_000_000)
        early = md.broad_score(row(**base), minutes_since_open=20)      # 寄り直後：1日の10%の想定 → 0.4/0.1=4倍
        late = md.broad_score(row(**base), minutes_since_open=280)      # 引け前：0.4/0.93≈0.43倍
        self.assertTrue(early and "rel_volume" in early[1])
        self.assertTrue(late is None or "rel_volume" not in late[1])

    def test_tags_can_supply_the_second_factor(self):
        r = row(price=960.0, prev=940.0, hi=985.0, lo=950.0, vol=400_000, avg=500_000)                   # 前日比+2%（gainer未満）
        self.assertIsNone(md.broad_score(r, minutes_since_open=200))
        s = md.broad_score(r, minutes_since_open=200, tags=("ipo", "news"))
        self.assertEqual(sorted(s[1]), ["ipo", "news"])


def cand(code, score, source="YF_GAINERS"):
    return {"code": code, "name": code, "source": source, "score": score, "reasons": ["前日比+5%"], "factors": ["gainer", "rel_volume"], "price": 1000.0}


class PoolTests(unittest.TestCase):
    def test_merge_reconfirms_and_caps_at_300(self):
        pool = {}
        self.assertEqual(md.merge_broad(pool, [cand("A", 50)], at(0)), ["A"])
        added = md.merge_broad(pool, [cand("A", 70, "YF_VOLUME")], at(600))
        self.assertEqual(added, [])
        self.assertEqual(pool["A"]["sources"], ["YF_GAINERS", "YF_VOLUME"])
        self.assertEqual(pool["A"]["broad_score"], 70)
        self.assertEqual(pool["A"]["last_seen_at"], at(600))
        big = [cand(f"C{i:03d}", i % 100) for i in range(400)]
        md.merge_broad(pool, big, at(700))
        live = [e for e in pool.values() if e["status"] != "EXPIRED"]
        self.assertLessEqual(len(live), md.BROAD_MAX)
        self.assertTrue(all(e["expire_reason"] == "BROAD_CAP" for e in pool.values() if e["status"] == "EXPIRED"))

    def test_ttl_freshness_broad_45min_realtime_20min(self):
        pool = {}
        md.merge_broad(pool, [cand("B", 60), cand("R", 60)], at(0))
        pool["R"].update({"status": "PROMOTED", "promoted_at": at(0), "rt_confirmed_at": at(0)})
        self.assertEqual(md.expire_pool(pool, at(19 * 60)), [])
        self.assertEqual(md.expire_pool(pool, at(21 * 60)), ["R"])                    # realtime確認済みは20分で失効
        self.assertEqual(pool["R"]["expire_reason"], "RT_STALE")
        self.assertEqual(md.expire_pool(pool, at(44 * 60)), [])
        self.assertEqual(md.expire_pool(pool, at(46 * 60)), ["B"])                    # broadは45分で失効
        self.assertEqual(pool["B"]["expire_reason"], "BROAD_STALE")

    def test_reseen_broad_candidate_stays_fresh(self):
        pool = {}
        md.merge_broad(pool, [cand("B", 60)], at(0))
        md.merge_broad(pool, [cand("B", 60)], at(40 * 60))
        self.assertEqual(md.expire_pool(pool, at(70 * 60)), [])                      # 40分時点で再確認 → 70分時点でも生きている

    def test_radar_active_entries_are_handed_to_radar_age_not_expired(self):
        pool = {}
        md.merge_broad(pool, [cand("R", 60)], at(0))
        pool["R"].update({"status": "PROMOTED", "promoted_at": at(0), "rt_confirmed_at": at(0), "radar_active": True})
        self.assertEqual(md.expire_pool(pool, at(60 * 60)), [])

    def test_select_realtime_caps_at_120_and_prefers_promoted(self):
        pool = {}
        md.merge_broad(pool, [cand(f"C{i:03d}", 10 + i % 80) for i in range(250)], at(0))
        pool["C000"].update({"status": "HOT"})
        sel = md.select_realtime(pool)
        self.assertEqual(len(sel), md.RT_MAX)
        self.assertEqual(sel[0], "C000")


def feed(code, samples):
    """samples: [(秒, 価格, 出来高)] → 履歴dict"""
    h = {}
    for s, p, v in samples:
        md.update_history(h, code, {"t": p, "volume": v}, at(s))
    return h[code]


class RealtimeTests(unittest.TestCase):
    Q = {"t": 1010.0, "volume": 1_500_000, "high": 1010.0, "ask": 1010.5, "bid": 1009.5}

    def test_price_and_volume_acceleration_new_high_and_spread(self):
        h = feed("A", [(0, 1000.0, 1_000_000), (120, 1001.0, 1_050_000), (240, 1002.0, 1_100_000), (300, 1004.0, 1_150_000), (420, 1010.0, 1_500_000)])
        sig = md.realtime_signals(h, self.Q, at(420))
        self.assertTrue(sig["price_accel"])
        self.assertTrue(sig["vol_accel"])
        self.assertTrue(sig["new_high"] and sig["near_high"])
        self.assertTrue(sig["spread_ok"])
        level, reasons = md.evaluate_promotion(sig)
        self.assertEqual(level, "HOT")                                   # 価格加速＋出来高加速＋高値更新＋spread許容
        self.assertIn("出来高加速", reasons)

    def test_short_history_gives_unknown_not_true(self):
        h = feed("A", [(0, 1000.0, 1_000_000), (30, 1010.0, 1_500_000)])
        sig = md.realtime_signals(h, self.Q, at(30))
        self.assertIsNone(sig["price_accel"])
        self.assertIsNone(sig["vol_accel"])
        self.assertEqual(md.evaluate_promotion(sig)[0], None)

    def test_promoted_needs_three_signals_including_acceleration(self):
        sig = {"price_accel": True, "vol_accel": False, "near_high": True, "new_high": False, "spread_ok": True}
        self.assertEqual(md.evaluate_promotion(sig)[0], "PROMOTED")     # 価格加速＋高値接近＋spread = 3
        sig2 = {"price_accel": False, "vol_accel": False, "near_high": True, "new_high": True, "spread_ok": True}
        self.assertIsNone(md.evaluate_promotion(sig2)[0])               # 加速系が無い
        sig3 = {"price_accel": True, "vol_accel": False, "near_high": False, "new_high": False, "spread_ok": None}
        self.assertIsNone(md.evaluate_promotion(sig3)[0])               # 要素不足
        self.assertEqual(md.evaluate_promotion(sig3, movement_up=True)[0], None)
        sig4 = {"price_accel": True, "vol_accel": False, "near_high": False, "new_high": False, "spread_ok": True}
        self.assertEqual(md.evaluate_promotion(sig4, movement_up=True)[0], "PROMOTED")   # movement上昇が3つ目

    def test_wide_spread_blocks_promotion_and_hot(self):
        sig = {"price_accel": True, "vol_accel": True, "near_high": True, "new_high": True, "spread_ok": False}
        self.assertIsNone(md.evaluate_promotion(sig)[0])


class LifecycleTests(unittest.TestCase):
    def test_states_and_milestones(self):
        pool = {}
        md.merge_broad(pool, [cand("A", 60)], at(0))
        e = pool["A"]
        self.assertEqual(e["status"], "BROAD")
        ev = {"radar_hot": False, "rolling_hot": False, "rolling_watch": False, "expanding": False, "entry_eligible": False, "chase": False,
              "n": 3, "radar": "RADAR_NONE", "rolling": "NONE", "pattern": None, "activity": None, "movement": None,
              "movement_recommendation": None, "confidence": "LOW"}
        self.assertEqual(md.apply_realtime(e, {}, None, [], ev, at(30)), ["realtime_first"])
        self.assertEqual(e["status"], "REALTIME")
        new = md.apply_realtime(e, {}, "PROMOTED", ["価格加速"], dict(ev, rolling_hot=True, rolling="SINGLE_BAR_SURGE"), at(60))
        self.assertEqual(set(new), {"promoted", "radar_at"})
        self.assertEqual((e["status"], e["promoted_at"], e["radar_at"]), ("PROMOTED", at(60), at(60)))
        self.assertTrue(e["radar_active"])
        new = md.apply_realtime(e, {}, "HOT", ["価格加速", "出来高加速"], dict(ev, expanding=True, chase=True), at(90))
        self.assertEqual(set(new), {"hot", "expanding_at", "chase_at"})
        self.assertEqual(e["status"], "HOT")
        self.assertEqual(md.apply_realtime(e, {}, "HOT", [], dict(ev, expanding=True), at(120)), [])     # マイルストーンは初回だけ
        md.apply_realtime(e, {}, "HOT", [], dict(ev, entry_eligible=True), at(150))
        self.assertEqual(e["entry_at"], at(150))
        self.assertFalse(e["entry_allowed"])                                                              # ENTRYには直接接続しない


class EntryStateTests(unittest.TestCase):
    QUIET7 = [(0.05, 0.30, 1000)] * 7

    def test_existing_radar_chart_and_movement_are_reused(self):
        b = build(self.QUIET7 + [(1.2, 1.2, 4000)])
        bars = [{"open": o, "high": h, "low": l, "close": c, "volume": v}
                for o, h, l, c, v in zip(b["opens"], b["highs"], b["lows"], b["closes"], b["volumes"])]
        q = {"t": b["closes"][-1], "high": max(b["highs"]), "low": min(b["lows"]), "ask": None, "bid": None}
        ev = md.evaluate_entry_state(bars, q, minutes_since_open=40, market_rs=1.0)
        self.assertEqual(ev["rolling"], "SINGLE_BAR_SURGE")            # Rolling Radarをそのまま使う
        self.assertTrue(ev["rolling_hot"])
        self.assertIsNotNone(ev["pattern"])                           # Chart Contextを通す
        self.assertIsNotNone(ev["movement"])                          # Movementを通す
        self.assertFalse(ev["entry_eligible"])                        # 急伸直後（CHASE系）はENTRY可にならない

    def test_few_bars_use_early_radar_only(self):
        b = build([(0.05, 0.3, 1000), (0.05, 0.3, 1000), (1.0, 1.0, 4000)])
        bars = [{"open": o, "high": h, "low": l, "close": c, "volume": v}
                for o, h, l, c, v in zip(b["opens"], b["highs"], b["lows"], b["closes"], b["volumes"])]
        ev = md.evaluate_entry_state(bars, {"t": b["closes"][-1], "high": max(b["highs"])}, minutes_since_open=15)
        self.assertEqual(ev["n"], 3)
        self.assertIsNone(ev["pattern"])                              # 6本未満は通常のChart Context/Movementを走らせない
        self.assertFalse(ev["entry_eligible"])

    def test_no_bars_is_unknown(self):
        ev = md.evaluate_entry_state([], {"t": 1000.0})
        self.assertEqual(ev["n"], 0)
        self.assertFalse(ev["entry_eligible"])


class ListAndIsolationTests(unittest.TestCase):
    def test_shadow_list_orders_by_status_and_is_never_entry(self):
        pool = {}
        md.merge_broad(pool, [cand("B", 90), cand("P", 50), cand("H", 40)], at(0))
        pool["P"]["status"], pool["H"]["status"] = "PROMOTED", "HOT"
        pool["P"]["promoted_at"] = at(30)
        pool["P"]["eval"] = {"movement": 55, "rolling": "ROLLING_SURGE", "pattern": "BASE_BUILDING"}
        lst = md.build_shadow_list(pool, at(600))
        self.assertEqual([d["code"] for d in lst], ["H", "P", "B"])
        self.assertTrue(all(d["entryAllowed"] is False and d["label"] == "🌐 市場発見" for d in lst))
        by = {d["code"]: d for d in lst}
        self.assertEqual((by["P"]["radarState"], by["P"]["movementScore"]), ("ROLLING_SURGE", 55))
        self.assertEqual(by["B"]["discoveryAgeMinutes"], 10.0)

    def test_entry_side_modules_never_reference_market_discovery(self):
        for mod in (chart_context, movement_potential, early_radar, rolling_radar):
            self.assertNotIn("market_discovery", inspect.getsource(mod))

    def test_pool_summary(self):
        pool = {}
        md.merge_broad(pool, [cand("A", 1), cand("B", 2)], at(0))
        md.expire_entry(pool["B"], at(1), "X")
        self.assertEqual(md.summarize_pool(pool), {"live": 1, "by_status": {"BROAD": 1}, "expired": 1, "total_seen": 2})


if __name__ == "__main__":
    unittest.main()
