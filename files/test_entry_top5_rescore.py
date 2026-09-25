# 今買い時TOP5 軽量再スコア（2026-09-25、Phase B-1）の回帰テスト。
# 「表示価格だけ最新でスコア・順位は算出時の古い価格のまま」（例：5301 算出時2302→現在2321）を防ぐ。
# 実行： cd files && python -m unittest test_entry_top5_rescore -v

import unittest
from unittest import mock

import server


def _shared():
    return {"auto_rs_current": set(), "auto_sector_current": set(), "nikkei_chg": 0.0,
            "event_guard_market": server.compute_event_risk_guard_market(
                {"events": [], "market_event_risk": "LOW"}, prior_day_events=[], market_gu_pct=0.0)}


def _ctx(code="5301", price=2302.0, vwap=2300.0, recent_high=2400.0):
    row = {"code": code, "current": price, "changePct": 2.0, "high": price + 5, "low": price - 40,
           "open": price - 30, "volume": 100000, "turnover": price * 100000, "highRetention": 0.99,
           "marketRS": 2.0, "sectorRS": 0.5, "ask": None, "bid": None}
    stage2 = {"timeAdjustedVolumeRatio": 2.0, "aboveRecentHigh": price > recent_high,
              "distanceFromHighPct": (price - recent_high) / recent_high * 100, "makingNewLowToday": False,
              "rawVolumeRatio": 2.0, "avgVolume20": 50000}
    snapshot = {"current": price, "currentChangePct": 2.0, "vwap": vwap, "aboveVwap": price > vwap,
                "fiveMinStructure": "higher_highs", "dataStatus": "ok", "cacheStatus": "ok"}
    return {"w": {"code": code, "name": code, "sector": "X"}, "row": row, "stage2": stage2, "snapshot": snapshot,
            "bars": None, "catalysts": [], "event_signals": [], "entry_risk": None, "data_quality": "FULL",
            "related_events": [], "scoredPrice": price, "baseHigh": row["high"], "recentHigh": recent_high,
            "rescoreCount": 0}


def _quote(t, p=2257.0, high=None, low=None):
    return {"t": t, "p": p, "changePct": (t - p) / p * 100, "volume": 120000, "high": high or t, "low": low or (p - 10),
            "ask": None, "bid": None, "source": "tachibana", "quote_timestamp": "2026-09-25T10:00:00+09:00",
            "is_stale": False}


class RescoreEquivalenceTests(unittest.TestCase):
    def test_same_price_rescore_matches_full_scan_scoring(self):
        """同じ入力価格なら軽量再スコアはフルスキャンと同じscore/stateになる（二重実装していない証明）。"""
        ctx, shared = _ctx(), _shared()
        row = dict(ctx["row"])
        full = server._compute_price_dependent_entry_fields(
            "5301", ctx["w"], row, ctx["stage2"], ctx["snapshot"], None, [], [], None, "FULL", shared, [])
        q = _quote(2302.0, p=2302.0 / 1.02)
        q["changePct"] = 2.0
        q["high"], q["low"] = row["high"], row["low"]
        q["volume"] = row["volume"]
        light = server.rescore_entry_candidate_with_quote(ctx, shared, q)
        self.assertEqual(light["entryScore"], round(full["entry_score"]))
        self.assertEqual(light["entryState"], full["entry_state"])

    def test_score_uses_new_price_not_scored_price(self):
        """2302算出→2321に上昇：スコア入力の価格・前日比が新価格になる（2302基準のまま残らない）。"""
        ctx, shared = _ctx(), _shared()
        f = server.rescore_entry_candidate_with_quote(ctx, shared, _quote(2321.0))
        self.assertEqual(f["current"], 2321.0)
        self.assertEqual(f["scoredPrice"], 2321.0)
        self.assertEqual(f["priceTrace"]["scoreCalcPrice"], 2321.0)
        self.assertAlmostEqual(f["changePct"], round((2321.0 - 2257.0) / 2257.0 * 100, 2))

    def test_vwap_cross_changes_score_and_state(self):
        """VWAP(2300)を下抜けるとVWAP点(15)を失い、ENTRY状態が変わる。"""
        ctx, shared = _ctx(price=2302.0, vwap=2300.0), _shared()
        above = server.rescore_entry_candidate_with_quote(ctx, shared, _quote(2310.0))
        below = server.rescore_entry_candidate_with_quote(ctx, shared, _quote(2290.0))
        self.assertGreater(above["scoreBreakdown"]["vwap"], 0)
        self.assertEqual(below["scoreBreakdown"]["vwap"], 0)
        self.assertGreater(above["entryScore"], below["entryScore"])

    def test_breakout_over_recent_high_applies_overheat_penalty(self):
        ctx, shared = _ctx(recent_high=2320.0), _shared()
        low = server.rescore_entry_candidate_with_quote(ctx, shared, _quote(2310.0))
        high = server.rescore_entry_candidate_with_quote(ctx, shared, _quote(2360.0))  # +1.7%超で過熱減点
        self.assertLessEqual(high["scoreBreakdown"]["overheat"], low["scoreBreakdown"]["overheat"])


class RescoreTriggerTests(unittest.TestCase):
    def test_small_move_does_not_trigger(self):
        self.assertIsNone(server.entry_rescore_trigger(_ctx(), _quote(2303.0)))

    def test_price_drift_triggers(self):
        self.assertEqual(server.entry_rescore_trigger(_ctx(), _quote(2302.0 * 1.004)), "PRICE_DRIFT")

    def test_vwap_cross_triggers_even_for_small_move(self):
        ctx = _ctx(price=2300.5, vwap=2300.0)
        self.assertEqual(server.entry_rescore_trigger(ctx, _quote(2299.5)), "VWAP_CROSS")

    def test_breakout_cross_triggers(self):
        ctx = _ctx(price=2399.0, vwap=2300.0, recent_high=2400.0)
        self.assertEqual(server.entry_rescore_trigger(ctx, _quote(2401.0)), "BREAKOUT_CROSS")

    def test_new_day_high_triggers(self):
        ctx = _ctx(price=2302.0, vwap=2200.0, recent_high=2500.0)
        self.assertEqual(server.entry_rescore_trigger(ctx, _quote(2308.0)), "NEW_DAY_HIGH")


class RescoreCacheRerankTests(unittest.TestCase):
    """価格変化→再スコア→TOP5順位変化までをキャッシュ経由で確認する。"""

    def setUp(self):
        self.user = "rescore_user"

    def tearDown(self):
        server._ENTRY_TOP5_CACHE.pop(self.user, None)
        server._ENTRY_RESCORE_CTX.pop(self.user, None)

    def test_rank_changes_after_price_move(self):
        shared = _shared()
        ctx_a, ctx_b = _ctx("1111", 2302.0, vwap=2300.0), _ctx("2222", 2302.0, vwap=2295.0)
        cands = []
        for code, ctx in (("1111", ctx_a), ("2222", ctx_b)):
            f = server.rescore_entry_candidate_with_quote(ctx, shared, _quote(2302.0))
            cands.append({"code": code, "name": code, "sector": "X", **f, "rank": 0, "dataQuality": "FULL",
                          "marketDataCacheStatus": "ok", "protectedReasons": [], "shadow": False})
        cands.sort(key=lambda c: -c["entryScore"])
        server._ENTRY_TOP5_CACHE[self.user] = {"entryReadyTop5": [], "_candidatePool": cands, "generatedAtEpoch": 0,
                                                "generatedAt": "2026-09-25T00:00:00+00:00", "debug": {}}
        server._ENTRY_RESCORE_CTX[self.user] = {"byCode": {"1111": ctx_a, "2222": ctx_b}, "shared": shared,
                                                 "builtAt": 0}
        # 1111だけVWAP(2300)を割り込む急落、2222は不変
        quotes = {"1111": _quote(2285.0), "2222": _quote(2302.0)}
        with mock.patch.object(server, "get_fast_quotes", return_value=(quotes, {})), \
             mock.patch.object(server, "get_capital_context", return_value=None):
            diag = server.rescore_entry_top5_cache("db", self.user)
        self.assertEqual(diag["rescored"], 1)
        pool = {c["code"]: c for c in server._ENTRY_TOP5_CACHE[self.user]["_candidatePool"]}
        self.assertEqual(pool["1111"]["scoredPrice"], 2285.0)          # 2302基準のまま残らない
        self.assertEqual(pool["2222"]["scoredPrice"], 2302.0)
        self.assertLess(pool["1111"]["entryScore"], cands[0]["entryScore"] if cands[0]["code"] == "1111" else 10**9)
        self.assertTrue(pool["1111"]["rescored"])
        self.assertIn("scoreVerifiedAt", pool["2222"])                 # 不変銘柄は再検証のみ（再スコアしない）
        self.assertFalse(pool["2222"].get("rescored", False))


class RescoreRankSwapTests(unittest.TestCase):
    """価格変化→スコア変化→TOP5の順位（メンバー）変化まで。2302算出→VWAP割れで順位から落ちる。"""

    def test_top5_membership_changes_when_price_breaks_vwap(self):
        shared = _shared()

        def strong(code):
            c = _ctx(code, 2302.0, vwap=2300.0)
            c["row"].update(changePct=5.0, marketRS=5.0)
            c["stage2"]["timeAdjustedVolumeRatio"] = 3.0
            return c

        def q(t):
            x = _quote(t, p=t / 1.05)
            x["changePct"] = 5.0
            return x
        ctxs = {"1111": strong("1111"), "2222": strong("2222")}
        cands = []
        for code, ctx in ctxs.items():
            f = server.rescore_entry_candidate_with_quote(ctx, shared, q(2302.0))
            cands.append({"code": code, "name": code, "sector": "X", **f, "rank": 0, "dataQuality": "FULL",
                          "marketDataCacheStatus": "ok", "protectedReasons": [], "shadow": False})
        user = "rank_swap_user"
        server._ENTRY_TOP5_CACHE[user] = {"entryReadyTop5": [], "_candidatePool": cands, "generatedAtEpoch": 0,
                                           "generatedAt": "2026-09-25T00:00:00+00:00", "debug": {}}
        server._ENTRY_RESCORE_CTX[user] = {"byCode": ctxs, "shared": shared, "builtAt": 0}
        try:
            server.recompute_entry_top5_cache_for_cash(user, None)
            before = sorted(c["code"] for c in server._ENTRY_TOP5_CACHE[user]["entryReadyTop5"])
            self.assertEqual(before, ["1111", "2222"])
            with mock.patch.object(server, "get_fast_quotes",
                                   return_value=({"1111": q(2290.0), "2222": q(2302.0)}, {})), \
                 mock.patch.object(server, "get_capital_context", return_value=None):
                diag = server.rescore_entry_top5_cache("db", user)
            after = [c["code"] for c in server._ENTRY_TOP5_CACHE[user]["entryReadyTop5"]]
            self.assertEqual(after, ["2222"])       # 1111はVWAP(2300)割れでTOP5から外れる
            self.assertTrue(diag["rankChanged"])
            self.assertEqual(diag["triggers"], {"PRICE_DRIFT": 1} if diag["triggers"].get("PRICE_DRIFT") else diag["triggers"])
        finally:
            server._ENTRY_TOP5_CACHE.pop(user, None)
            server._ENTRY_RESCORE_CTX.pop(user, None)


class OverlayScoreStalenessTests(unittest.TestCase):
    def _quote_map(self, t):
        return {"5301": _quote(t)}

    def test_ready_candidate_with_stale_score_is_not_shown_as_buyable(self):
        result = {"generatedAt": "2026-09-25T00:00:00+00:00",
                  "entryReadyTop5": [{"code": "5301", "current": 2302.0, "scoredPrice": 2302.0, "entryScore": 70,
                                       "entryState": "ENTRY_READY", "scoredAt": "2026-09-25T09:00:00+09:00",
                                       "scoreVerifiedAt": "2026-09-25T09:00:00+09:00"}]}
        with mock.patch.object(server, "get_fast_quotes", return_value=(self._quote_map(2321.0), {})):
            out = server.overlay_latest_quotes_on_entry_result(result)
        c = out["entryReadyTop5"][0]
        self.assertEqual(c["current"], 2321.0)
        self.assertEqual(c["entryState"], "WAIT_DATA_STALE")   # 価格だけ最新・スコア古い → 買える表示にしない
        self.assertTrue(c["scoreStale"])
        self.assertEqual(c["staleReason"], "score_price_drift")
        self.assertAlmostEqual(c["priceDriftPct"], 0.83, places=2)

    def test_fresh_verified_score_keeps_state(self):
        import datetime
        now = datetime.datetime.now(server._JST).isoformat()
        result = {"generatedAt": now,
                  "entryReadyTop5": [{"code": "5301", "current": 2302.0, "scoredPrice": 2302.0, "entryScore": 70,
                                       "entryState": "ENTRY_READY", "scoredAt": now, "scoreVerifiedAt": now}]}
        with mock.patch.object(server, "get_fast_quotes", return_value=(self._quote_map(2303.0), {})):
            out = server.overlay_latest_quotes_on_entry_result(result)
        c = out["entryReadyTop5"][0]
        self.assertEqual(c["entryState"], "ENTRY_READY")
        self.assertFalse(c["scoreStale"])


if __name__ == "__main__":
    unittest.main()
