# 買付余力ベース候補評価・IPO再評価・shadow_watch（2026-09-24、akippa事例）のテスト。
# 実行： cd files && python -m unittest test_capital_ranking -v
#
# TEST1-2  余力による買える/買えない判定
# TEST3    余力を手動変更するとキャッシュ済みTOP5が即時再ランキングされる
# TEST4    NOT_BUYABLE銘柄はTOP5に入らず、別枠「余力不足だが注目」に出る
# TEST5-7  IPO銘柄：朝の下落でもWATCH_LOWに残る／VWAP回復でWATCH／出来高急増＋高値更新でBUY_CANDIDATE
# TEST8-9  ユーザーがIPOを監視削除してもshadow_watch継続／急騰したら再注目候補
# TEST10   余力使用率85%以上でCAPITAL_CONCENTRATION_RISK
# TEST11-12 既存TOP5選定・既存ポジション管理との回帰（余力未設定時は完全に従来どおり）

import datetime
import unittest
from unittest import mock

import capital_ranking as cr
import server


def cand(code, price, score, state="ENTRY_READY", **kw):
    return {"code": code, "name": code, "current": price, "entryScore": score, "entryState": state,
            "changePct": kw.pop("changePct", 2.0), **kw}


class BuyabilityTests(unittest.TestCase):
    def test1_300k_stock_buyable_with_700k(self):
        b = cr.compute_buyability(3000, 700000)
        self.assertEqual(b["minimumPurchaseAmount"], 300000)
        self.assertTrue(b["buyable"])
        self.assertEqual(b["cashRemainingAfterBuy"], 400000)

    def test1b_spec_example_5801(self):
        b = cr.compute_buyability(3850, 700000)
        self.assertEqual((b["minimumPurchaseAmount"], b["cashRemainingAfterBuy"], b["buyable"]),
                         (385000, 315000, True))

    def test2_800k_stock_not_buyable(self):
        b = cr.compute_buyability(8000, 700000)
        self.assertEqual(b["minimumPurchaseAmount"], 800000)
        self.assertFalse(b["buyable"])
        self.assertIsNone(b["cashRemainingAfterBuy"])

    def test_no_cash_set_means_unknown_not_false(self):
        self.assertIsNone(cr.compute_buyability(3000, None)["buyable"])

    def test10_concentration_risk_levels(self):
        b = cr.compute_buyability(6500, 700000)  # 650,000 / 700,000 = 92.9%
        self.assertEqual(b["concentrationRisk"], "STRONG")
        self.assertEqual(cr.compute_buyability(5000, 700000)["concentrationRisk"], "CAUTION")  # 71.4%
        self.assertIsNone(cr.compute_buyability(3000, 700000)["concentrationRisk"])
        a = cr.annotate_capital(cand("X", 6500, 80), 700000)
        self.assertIn("CAPITAL_CONCENTRATION_RISK", a["capitalFlags"])


class CapitalRankingTests(unittest.TestCase):
    def _select(self, cands, cash):
        cr_result = server._build_capital_selection(cands, {"cash_available": cash, "source": "MANUAL"} if cash is not None else None)
        return cr_result

    def test4_not_buyable_excluded_from_top5_and_listed_separately(self):
        cands = [cand("A", 3000, 90), cand("B", 5000, 88), cand("C", 9000, 95)]
        top5, watch, _, _, debug, capital = self._select(cands, 700000)
        self.assertEqual([c["code"] for c in top5], ["A", "B"])  # 95点のCは1位にならない
        self.assertEqual([c["code"] for c in capital["notBuyableNotable"]], ["C"])
        self.assertEqual(capital["notBuyableNotable"][0]["capitalStatus"], "NOT_BUYABLE")
        self.assertEqual(top5[0]["capitalStatus"], "BUY_NOW")
        self.assertEqual(debug["capitalNotBuyableCount"], 1)

    def test_avoid_state_stays_avoid_even_if_buyable(self):
        a = cr.annotate_capital(cand("Z", 2000, 70, state="CHASE_RISK"), 700000)
        self.assertEqual(a["capitalStatus"], "AVOID")

    def test_capital_efficiency_not_simple_price_inverse(self):
        cheap = cr.annotate_capital(cand("LOW", 500, 60), 700000)   # 低位株
        strong = cr.annotate_capital(cand("HI", 3000, 90), 700000)
        self.assertGreater(strong["capitalEfficiencyScore"], cheap["capitalEfficiencyScore"])
        surged = cr.annotate_capital(cand("S", 3000, 90, changePct=20), 700000)
        self.assertLess(surged["capitalEfficiencyScore"], strong["capitalEfficiencyScore"])  # 急騰リスク減点

    def test_spec_example_A_B_ahead_of_C(self):
        cands = [cand("A", 3000, 90), cand("B", 5000, 88), cand("C", 9000, 95)]
        top5, *_ = self._select(cands, 700000)
        self.assertNotEqual(top5[0]["code"], "C")

    def test_combinations_respect_cash_and_report_remaining(self):
        cands = [cand("A", 3000, 90), cand("B", 2500, 80), cand("C", 4000, 70)]
        annotated = [cr.annotate_capital(c, 700000) for c in cands]
        combos = cr.suggest_combinations(annotated, 700000)
        self.assertTrue(combos)
        for c in combos:
            self.assertLessEqual(c["totalAmount"], 700000)
            self.assertEqual(c["cashRemaining"], 700000 - c["totalAmount"])
        ab = [c for c in combos if set(c["codes"]) == {"A", "B"}]
        self.assertEqual(ab[0]["totalAmount"], 550000)
        self.assertEqual(ab[0]["cashRemaining"], 150000)

    def test3_cash_change_reranks_cached_top5_immediately(self):
        pool = [cand("A", 3000, 90), cand("B", 5000, 88), cand("C", 9000, 95)]
        server._ENTRY_TOP5_CACHE["u_cash_test"] = {
            "entryReadyTop5": [], "watchCandidates": [], "debug": {}, "_candidatePool": pool}
        try:
            self.assertTrue(server.recompute_entry_top5_cache_for_cash("u_cash_test", {"cash_available": 700000, "source": "MANUAL"}))
            e1 = server._ENTRY_TOP5_CACHE["u_cash_test"]
            self.assertEqual([c["code"] for c in e1["entryReadyTop5"]], ["A", "B"])
            # 余力を1,000,000円へ引き上げると、9,000円(=900,000円)のCが買える＝1位に
            self.assertTrue(server.recompute_entry_top5_cache_for_cash("u_cash_test", {"cash_available": 1000000, "source": "MANUAL"}))
            e2 = server._ENTRY_TOP5_CACHE["u_cash_test"]
            self.assertIn("C", [c["code"] for c in e2["entryReadyTop5"]])
            self.assertEqual(e2["capital"]["cashAvailable"], 1000000)
            self.assertEqual(e2["capital"]["notBuyableNotable"], [])
        finally:
            server._ENTRY_TOP5_CACHE.pop("u_cash_test", None)

    def test3b_no_pool_returns_false(self):
        server._ENTRY_TOP5_CACHE.pop("u_none", None)
        self.assertFalse(server.recompute_entry_top5_cache_for_cash("u_none", {"cash_available": 1}))

    def test11_no_cash_set_is_identical_to_existing_selection(self):
        cands = [cand("A", 3000, 90), cand("B", 5000, 88), cand("C", 9000, 95)]
        cands.sort(key=lambda c: -c["entryScore"])
        import copy
        expected = server._select_entry_ready_top5(copy.deepcopy(cands))
        top5, watch, rc, rw, debug, capital = server._build_capital_selection(copy.deepcopy(cands), None)
        self.assertEqual([c["code"] for c in top5], [c["code"] for c in expected[0]])
        self.assertFalse(capital["applied"])
        self.assertNotIn("capitalStatus", top5[0])


class IpoStageTests(unittest.TestCase):
    def test5_morning_drop_stays_watch_low_not_deleted(self):
        stage, _ = cr.evaluate_ipo_stage("WATCH", {})  # 朝の下落＝シグナル無し
        self.assertEqual(stage, "WATCH_LOW")
        self.assertIn(stage, cr.IPO_STAGES)  # DELETEという段階は存在しない
        self.assertFalse(cr.evaluate_watch_removal({"morning_drop_only": True, "volume_dried_up": True,
                                                    "catalyst_gone": True, "trend_broken": True}))
        self.assertFalse(cr.evaluate_watch_removal({"volume_dried_up": True}, ipo_in_window=True))

    def test6_vwap_recovery_promotes_to_watch(self):
        self.assertEqual(cr.evaluate_ipo_stage("WATCH_LOW", {"vwap_recovered": True})[0], "WATCH")

    def test7_volume_surge_and_high_update_promotes_to_buy_candidate(self):
        self.assertEqual(cr.evaluate_ipo_stage("WATCH_LOW", {"volume_surge": True, "day_high_update": True})[0],
                         "BUY_CANDIDATE")
        self.assertEqual(cr.evaluate_ipo_stage("WATCH_LOW", {"volume_surge": True})[0], "WATCH")  # 出来高だけでは昇格しない
        self.assertEqual(cr.evaluate_ipo_stage("WATCH_LOW", {}, entry_score=88)[0], "BUY_CANDIDATE")

    def test13_intraday_revival_sequence(self):
        seq = [({}, "WATCH_LOW"), ({"volume_surge": True}, "WATCH"), ({"volume_surge": True, "vwap_recovered": True}, "WATCH"),
               ({"volume_surge": True, "vwap_recovered": True, "day_high_update": True}, "BUY_CANDIDATE")]
        for sig, exp in seq:
            self.assertEqual(cr.evaluate_ipo_stage(None, sig)[0], exp)

    def test_ipo_window_20_business_days(self):
        listing = datetime.date(2026, 9, 1)
        self.assertTrue(cr.is_within_ipo_watch_window(listing, datetime.date(2026, 9, 24)))
        self.assertFalse(cr.is_within_ipo_watch_window(listing, datetime.date(2026, 10, 15)))

    def test_apply_ipo_reevaluation_sets_stage_and_persists_change(self):
        c = cand("5031", 900, 60, state="WATCH", scoreBreakdown={}, reasons=[])
        row = {"code": "5031", "listing_date": "2026-09-10", "watch_stage": "WATCH_LOW", "offer_price": 570,
               "fundamentals_json": {"revenue_growth_pct": 30, "profit_growth_pct": 50, "guidance": "UPWARD"}}
        sigs = {"5031": {"volume_surge": True, "day_high_update": True}}
        with mock.patch.object(server, "investment_db") as db:
            db.list_ipo_stocks.return_value = [row]
            server.apply_ipo_reevaluation("url", "u", [c], sigs)
            db.set_ipo_watch_stage.assert_called_once()
        self.assertEqual(c["ipoInfo"]["stage"], "BUY_CANDIDATE")
        self.assertGreater(c["ipoInfo"]["fundamental"]["ipo_fundamental_score"], 70)
        self.assertEqual(c["ipoInfo"]["priceVsOfferPct"], 57.9)
        self.assertIsNotNone(c["ipoInfo"]["ipoTotalScore"])

    def test_ipo_scores_weights_configurable_and_missing_renormalized(self):
        s = {"fundamental": 80, "momentum": 60, "supply_demand": None, "volume": 100, "material": 50}
        default = cr.compute_ipo_total_score(s)
        alt = cr.compute_ipo_total_score(s, {"fundamental": 0.1, "momentum": 0.7, "supply_demand": 0.1, "volume": 0.05, "material": 0.05})
        self.assertNotEqual(default, alt)
        self.assertIsNone(cr.compute_ipo_total_score({}))


class ShadowWatchTests(unittest.TestCase):
    def test_business_day_windows(self):
        start = datetime.date(2026, 9, 24)  # 木
        self.assertEqual(cr.shadow_watch_until("SURGE", start), datetime.date(2026, 9, 28))    # 金、月
        self.assertEqual(cr.shadow_watch_until("MATERIAL", start), datetime.date(2026, 9, 29))
        self.assertEqual(cr.shadow_watch_until("IPO", start), datetime.date(2026, 10, 22))

    def test8_user_delete_of_ipo_registers_shadow_watch(self):
        row = {"code": "5031", "name": "akippa", "listing_date": "2026-09-10"}
        with mock.patch.object(server, "investment_db") as db, \
                mock.patch.object(server, "_jst_today", return_value=datetime.date(2026, 9, 24)):
            db.list_ipo_stocks.return_value = [row]
            db.upsert_shadow_watch.side_effect = lambda url, u, code, kind, until, **kw: {"code": code, "kind": kind, "until_date": until}
            res = server.register_shadow_watch_before_delete("url", "u", "5031")
        self.assertEqual(res["kind"], "IPO")
        self.assertGreater(res["until_date"], datetime.date(2026, 10, 1))

    def test8b_ordinary_stock_delete_registers_nothing(self):
        with mock.patch.object(server, "investment_db") as db:
            db.list_ipo_stocks.return_value = []
            with mock.patch.object(server, "get_entry_top5_cached", return_value={}):
                self.assertIsNone(server.register_shadow_watch_before_delete("url", "u", "7203"))
            db.upsert_shadow_watch.assert_not_called()

    def test8c_surging_stock_delete_registers_surge(self):
        with mock.patch.object(server, "investment_db") as db, \
                mock.patch.object(server, "get_entry_top5_cached", return_value={"_candidatePool": [{"code": "1234", "changePct": 9.5}]}), \
                mock.patch.object(server, "_jst_today", return_value=datetime.date(2026, 9, 24)):
            db.list_ipo_stocks.return_value = []
            db.upsert_shadow_watch.side_effect = lambda url, u, code, kind, until, **kw: {"kind": kind}
            self.assertEqual(server.register_shadow_watch_before_delete("url", "u", "1234")["kind"], "SURGE")

    def test9_shadow_watch_surge_becomes_recheck_candidate(self):
        sw = {"code": "5031", "name": "akippa", "kind": "IPO", "until_date": "2026-10-22"}
        quote = {"t": 1000.0, "p": 900.0, "high": 1000.0, "low": 910.0, "volume": 5e6}
        with mock.patch.object(server, "investment_db") as db, \
                mock.patch.object(server, "_jst_today", return_value=datetime.date(2026, 9, 24)), \
                mock.patch.object(server, "get_stock_quotes", return_value={"5031": quote}), \
                mock.patch.object(server, "_intraday_stock_snapshot", return_value={"aboveVwap": True}), \
                mock.patch.object(server, "_volume_stage2_detail", return_value={"timeAdjustedVolumeRatio": 4.0, "aboveRecentHigh": False}):
            db.list_shadow_watch.return_value = [sw]
            db.relevant_catalysts_for.return_value = []
            out = server.evaluate_shadow_watches("url", "u")
            self.assertEqual(len(out), 1)
            self.assertEqual(out[0]["label"], "再注目候補")
            self.assertIn("出来高急増", out[0]["reasons"])
            self.assertIn("高値更新", out[0]["reasons"])
            db.update_shadow_watch_check.assert_called()

    def test9b_quiet_stock_not_flagged_and_expired_marked(self):
        quiet = {"t": 900.0, "p": 900.0, "high": 950.0, "low": 890.0}
        expired = {"code": "OLD", "kind": "SURGE", "until_date": "2026-09-01"}
        live = {"code": "5031", "kind": "IPO", "until_date": "2026-10-22"}
        with mock.patch.object(server, "investment_db") as db, \
                mock.patch.object(server, "_jst_today", return_value=datetime.date(2026, 9, 24)), \
                mock.patch.object(server, "get_stock_quotes", return_value={"5031": quiet}), \
                mock.patch.object(server, "_intraday_stock_snapshot", return_value={"aboveVwap": False}), \
                mock.patch.object(server, "_volume_stage2_detail", return_value={"timeAdjustedVolumeRatio": 0.8, "aboveRecentHigh": False}):
            db.list_shadow_watch.return_value = [expired, live]
            db.relevant_catalysts_for.return_value = []
            out = server.evaluate_shadow_watches("url", "u")
        self.assertEqual(out, [])
        db.update_shadow_watch_check.assert_any_call("url", "u", "OLD", status="EXPIRED")


class RegressionTests(unittest.TestCase):
    def test12_morning_top5_ignores_capital_constraint(self):
        with mock.patch.object(server, "_score_entry_candidates_impl", return_value={}) as m:
            server.generate_morning_entry_top5("url", "u")
        m.assert_called_once_with("url", "u", apply_capital=False)

    def test_learning_rule_spec(self):
        r = cr.IPO_LEARNING_RULE
        self.assertEqual((r["rule_name"], r["category"], r["priority"]), ("直近IPOの早期監視解除禁止", "ENTRY", "HIGH"))
        with mock.patch.object(server, "investment_db") as db:
            db.upsert_trade_rule_from_text.return_value = {"ok": True}
            server.seed_ipo_early_unwatch_learning_rule("url", "u")
            kw = db.upsert_trade_rule_from_text.call_args.kwargs
            self.assertEqual(kw["initial_status"], "TESTING")
            self.assertEqual(kw["category"], "ENTRY")

    def test_fundamental_score_akippa_like(self):
        s = cr.compute_ipo_fundamental_score({"revenue_growth_pct": 35, "profit_growth_pct": 55, "guidance": "UPWARD",
                                              "kpi_growth_pct": 30, "per": 25, "lockup_risk": 30, "vc_holding_pct": 20,
                                              "float_ratio_pct": 25, "business_quality": 80, "market_size_score": 70,
                                              "margin_change_pt": 2})
        self.assertGreater(s["ipo_fundamental_score"], 65)
        self.assertEqual(s["coverage"], 1.0)


if __name__ == "__main__":
    unittest.main()
