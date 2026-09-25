# 買付余力ランキング 実戦運用フェーズ（2026-09-24）のテスト。
# 実行： cd files && python -m unittest test_capital_ranking_phase2 -v
#
# TEST13 analysis_top5では余力不足銘柄も評価される
# TEST14 actionable_top5では余力不足銘柄が除外される
# TEST15 余力増加でactionable_top5へ復帰
# TEST16 shadow_watch → BUY_CANDIDATE → actionable_top5
# TEST17 IPOファンダメンタルデータ不足時にdata_qualityが返る
# TEST18 既存業績データの情報がIPOスコアへ利用される（優先順位つき）
# TEST19 3ポジション保有時ENTRY_LIMIT
# TEST20 capital efficiencyだけで低品質銘柄が高品質銘柄を大きく逆転しない

import datetime
import inspect
import unittest
from unittest import mock

import capital_ranking as cr
import server

SAMPLE_IPO_DISCLOSURE = """
【個別】        （単位：百万円、％）
決算期
売上高 4,300  100 12.3 1,915 100 3,828 100
営業利益 340  7.9 69.1 119 6.2 201 5.2
経常利益 341  7.9 67.0 123 6.5 204 5.3
当期（中間）純利益 302 7.0 35.5 104 5.5 223 5.8
"""


def cand(code, price, score, state="ENTRY_READY", **kw):
    return {"code": code, "name": code, "current": price, "entryScore": score, "entryState": state,
            "changePct": kw.pop("changePct", 2.0), **kw}


def dual(cands, cash, positions=None):
    cands = sorted(cands, key=lambda c: -c["entryScore"])
    return server._build_capital_selection(cands, {"cash_available": cash, "source": "MANUAL"} if cash is not None else None,
                                           positions)


class AnalysisVsActionableTests(unittest.TestCase):
    def setUp(self):
        # 余力700,000円：A(必要900,000/90点→分析1位) B(420,000) C(300,000)
        self.cands = [cand("A", 9000, 95), cand("B", 4200, 90), cand("C", 3000, 85)]

    def test13_analysis_top5_ignores_capital(self):
        d = dual(self.cands, 700000)
        self.assertEqual([c["code"] for c in d["analysis"]["top5"]], ["A", "B", "C"])
        self.assertEqual([c["analysisRank"] for c in d["analysis"]["top5"]], [1, 2, 3])
        a = d["analysis"]["top5"][0]
        self.assertIsNone(a["actionableRank"])
        self.assertIn("余力不足", a["actionableReason"])  # 「良い銘柄なのに出ない理由」が分かる

    def test14_actionable_top5_excludes_not_buyable(self):
        d = dual(self.cands, 700000)
        self.assertEqual([c["code"] for c in d["top5"]], ["B", "C"])
        self.assertEqual([c["actionableRank"] for c in d["top5"]], [1, 2])
        self.assertEqual([c["analysisRank"] for c in d["top5"]], [2, 3])  # 分析順位と実戦順位を区別
        self.assertEqual([c["code"] for c in d["capital"]["notBuyableNotable"]], ["A"])

    def test15_cash_increase_brings_A_back(self):
        pool = sorted(self.cands, key=lambda c: -c["entryScore"])
        server._ENTRY_TOP5_CACHE["u_p2"] = {"entryReadyTop5": [], "watchCandidates": [], "debug": {}, "_candidatePool": pool}
        try:
            server.recompute_entry_top5_cache_for_cash("u_p2", {"cash_available": 700000, "source": "MANUAL"})
            self.assertNotIn("A", [c["code"] for c in server._ENTRY_TOP5_CACHE["u_p2"]["actionableTop5"]])
            server.recompute_entry_top5_cache_for_cash("u_p2", {"cash_available": 1000000, "source": "MANUAL"})
            e = server._ENTRY_TOP5_CACHE["u_p2"]
            self.assertEqual(e["actionableTop5"][0]["code"], "A")
            self.assertEqual(e["actionableTop5"][0]["actionableRank"], 1)
            self.assertEqual([c["code"] for c in e["analysisTop5"]][:3], ["A", "B", "C"])  # 分析順位は不変
        finally:
            server._ENTRY_TOP5_CACHE.pop("u_p2", None)

    def test_no_cash_actionable_equals_analysis(self):
        d = dual(self.cands, None)
        self.assertEqual([c["code"] for c in d["top5"]], [c["code"] for c in d["analysis"]["top5"]])

    def test_precision_verification_snapshots_use_analysis_top5(self):
        src = inspect.getsource(server._score_entry_candidates_impl)
        self.assertIn('enumerate(analysis["top5"])', src)
        self.assertIn('for c in analysis["watch"]', src)

    def test_morning_thesis_persistence_uses_analysis_top5(self):
        src = inspect.getsource(server.generate_morning_check) if hasattr(server, "generate_morning_check") else ""
        if not src:
            src = inspect.getsource(server.generate_morning_market_check)
        self.assertIn('analysisTop5', src)
        self.assertIn('persist_morning_theses(database_url, user_id, check_date, saved.get("id"), analysis_top5_for_theses)', src)


class ShadowWatchPipelineTests(unittest.TestCase):
    def test16_shadow_buy_candidate_returns_to_actionable_top5(self):
        # akippa型：朝は弱く監視解除→shadow_watch→出来高急増＋高値更新でBUY_CANDIDATE→余力で買える
        revived = cand("627A", 1300, 40, state="WATCH", revivalStage="BUY_CANDIDATE", shadow=True)
        others = [cand("A", 3000, 80), cand("B", 5000, 70, state="WAIT_PULLBACK")]
        d = dual(others + [revived], 2000000)
        codes = [c["code"] for c in d["top5"]]
        self.assertIn("627A", codes)
        r = next(c for c in d["top5"] if c["code"] == "627A")
        self.assertTrue(r["revivedBuyCandidate"])
        self.assertEqual(r["capitalStatus"], "WATCH")
        # BUY_CANDIDATEでなければ（同じ内容でも）TOP5に戻らない＝復活枠がこの経路だけを開いている
        plain = cand("627A", 1300, 40, state="WATCH", shadow=True)
        d2 = dual(others + [plain], 2000000)
        self.assertNotIn("627A", [c["code"] for c in d2["top5"]])

    def test16b_finalize_reports_pipeline_status(self):
        cands = [cand("627A", 1300, 58, state="WATCH", revivalStage="BUY_CANDIDATE", shadow=True, shadowKind="IPO",
                      actionableRank=2),
                 cand("XXXX", 90000, 90, state="ENTRY_READY", revivalStage="BUY_CANDIDATE", shadow=True,
                      buyable=False, minimumPurchaseAmount=9000000),
                 cand("YYYY", 500, 40, state="WATCH", revivalStage="WATCH", shadow=True)]
        sig = {"627A": {"volume_surge": True, "day_high_update": True}, "XXXX": {"volume_surge": True},
               "YYYY": {"day_high_update": True}}
        with mock.patch.object(server, "investment_db") as db:
            out = server.finalize_shadow_scan("url", "u", cands, sig, {"627A"})
        by = {o["code"]: o for o in out}
        self.assertEqual(by["627A"]["pipelineStatus"], "ACTIONABLE_TOP5")
        self.assertEqual(by["XXXX"]["pipelineStatus"], "BUY_CANDIDATE_BLOCKED")
        self.assertIn("余力不足", by["XXXX"]["blockedReason"])
        self.assertEqual(by["YYYY"]["pipelineStatus"], "WATCHING")

    def test16d_finalize_uses_actionable_rank_map(self):
        cands = [cand("627A", 1300, 58, state="WATCH", revivalStage="BUY_CANDIDATE", shadow=True, shadowKind="IPO")]
        with mock.patch.object(server, "investment_db"):
            out = server.finalize_shadow_scan("url", "u", cands, {"627A": {"volume_surge": True}}, {"627A"},
                                              actionable_ranks={"627A": 3})
        self.assertEqual(out[0]["actionableRank"], 3)

    def test16e_capital_selection_exposes_annotated_candidates(self):
        d = dual([cand("A", 9000, 95, shadow=True, revivalStage="BUY_CANDIDATE")], 700000)
        a = d["annotated"][0]
        self.assertFalse(a["buyable"])  # 余力不足の理由判定に使う注釈が付いている

    def test16c_risk_state_buy_candidate_is_not_forced_in(self):
        risky = cand("Z", 1000, 90, state="CHASE_RISK", revivalStage="BUY_CANDIDATE")
        d = dual([risky, cand("A", 3000, 80)], 2000000)
        self.assertNotIn("Z", [c["code"] for c in d["top5"]])

    def test_shadow_codes_join_the_scan_targets(self):
        rows = [{"code": "627A", "kind": "IPO", "name": "akippa", "until_date": "2099-01-01"},
                {"code": "7203", "kind": "SURGE", "until_date": "2099-01-01"}]
        with mock.patch.object(server, "investment_db") as db, \
                mock.patch.object(server, "get_jp_issue_master", return_value={"627A": {"name": "akippa", "sector": "情報・通信業"}}):
            db.list_shadow_watch.return_value = rows
            targets, _ = server._active_shadow_targets("url", "u", {"7203"})
        self.assertEqual([t["code"] for t in targets], ["627A"])  # 監視銘柄に既にある7203は重複させない
        self.assertTrue(targets[0]["_shadow"])
        self.assertEqual(targets[0]["sector"], "情報・通信業")

    def test_no_per_symbol_fetch_for_scanned_shadow_codes(self):
        with mock.patch.object(server, "investment_db") as db, \
                mock.patch.object(server, "get_stock_quotes") as gq:
            db.list_shadow_watch.return_value = [{"code": "627A", "kind": "IPO", "until_date": "2099-01-01"}]
            out = server.evaluate_shadow_watches("url", "u", skip_codes={"627A"})
        self.assertEqual(out, [])
        gq.assert_not_called()  # scan内バッチで処理済み＝N+1の個別取得をしない


class IpoFundamentalTests(unittest.TestCase):
    def test17_insufficient_data_quality_returned(self):
        s = cr.compute_ipo_fundamental_score({})
        self.assertEqual(s["fundamental_data_quality"], "INSUFFICIENT")
        self.assertIsNone(s["ipo_fundamental_score"])
        low = cr.compute_ipo_fundamental_score({"revenue_growth_pct": 30, "profit_growth_pct": 50})
        self.assertEqual(low["fundamental_data_quality"], "LOW")
        full = cr.compute_ipo_fundamental_score({
            "revenue_growth_pct": 30, "profit_growth_pct": 50, "margin_change_pt": 2, "guidance": "UPWARD",
            "kpi_growth_pct": 30, "business_quality": 80, "market_size_score": 70, "per": 25, "lockup_risk": 30,
            "vc_holding_pct": 20, "float_ratio_pct": 25})
        self.assertEqual(full["fundamental_data_quality"], "HIGH")

    def test17c_derived_fields_do_not_raise_data_quality(self):
        data = {"revenue_growth_pct": 12.3, "profit_growth_pct": 69.2, "margin_change_pt": 2.7, "per": 37.5,
                "float_ratio_pct": 22.3}
        plain = cr.compute_ipo_fundamental_score(data)
        derived = cr.compute_ipo_fundamental_score(data, derived_fields=["float_ratio_pct"])
        self.assertEqual(plain["fundamental_data_quality"], "MEDIUM")
        self.assertEqual(derived["fundamental_data_quality"], "LOW")  # 推定値は品質の根拠に数えない
        self.assertEqual(derived["ipo_fundamental_score"], plain["ipo_fundamental_score"])  # スコア自体には使う
        self.assertEqual(derived["derived_components"], ["float_size"])

    def test17b_score_high_but_data_quality_low_is_flagged_in_ipo_info(self):
        row = {"listing_date": "2026-09-18", "watch_stage": "WATCH_LOW", "offer_price": 570}
        info = cr.build_ipo_info(row, cand("627A", 1300, 80), {}, datetime.date(2026, 9, 24), None,
                                 fundamentals={"revenue_growth_pct": 12.3, "profit_growth_pct": 69.2})
        self.assertEqual(info["fundamental_data_quality"], "LOW")
        for k in ("ipo_fundamental_score", "ipo_supply_demand_score", "ipo_momentum_score", "ipo_total_score"):
            self.assertIn(k, info)

    def test18_existing_earnings_data_feeds_ipo_score_with_priority(self):
        history = [{"title": "東京証券取引所スタンダードへの上場に伴う当社決算情報等のお知らせ", "pubdate": "2026-09-18", "url": "http://x/ipo.pdf"}]
        with mock.patch.object(server, "_tdnet_company_history", return_value=history), \
                mock.patch.object(server, "_fetch_pdf_text", return_value=SAMPLE_IPO_DISCLOSURE), \
                mock.patch.object(server, "_ipo_market_snapshot", return_value={"per": 37.5, "market_cap": 8.37e9}), \
                mock.patch.object(server, "_smart_import_fundamentals", return_value=({"guidance": "UPWARD"}, [{"type": "market_event", "id": 1}])), \
                mock.patch.object(server, "build_earnings_trend", return_value=[]):
            auto = server.collect_ipo_fundamentals("url", "u", "627A")
        ex = auto["existing"]
        self.assertEqual(ex["revenue_growth_pct"], 12.3)
        self.assertEqual(ex["profit_growth_pct"], 69.2)
        self.assertEqual(ex["margin_change_pt"], 2.7)
        self.assertEqual(ex["per"], 37.5)
        self.assertEqual(auto["smart_import"], {"guidance": "UPWARD"})
        # 優先順位：既存＞Smart Import＞明示入力（明示入力は無い項目だけを補う）
        row = {"auto_fundamentals_json": auto, "fundamentals_json": {"revenue_growth_pct": 99, "business_quality": 80, "guidance": "DOWNWARD"}}
        merged, sources = server._ipo_fundamentals_for(row)
        self.assertEqual(merged["revenue_growth_pct"], 12.3)
        self.assertEqual(sources["revenue_growth_pct"], "existing")
        self.assertEqual(merged["guidance"], "UPWARD")
        self.assertEqual(sources["guidance"], "smart_import")
        self.assertEqual(merged["business_quality"], 80)
        self.assertEqual(sources["business_quality"], "explicit")
        info = cr.build_ipo_info({"listing_date": "2026-09-18", "offer_price": 570}, cand("627A", 1300, 70), {},
                                 datetime.date(2026, 9, 24), None, fundamentals=merged)
        self.assertGreater(info["ipo_fundamental_score"], 50)

    def test_parse_forecast_table_never_guesses(self):
        p = cr.parse_ipo_forecast_table(SAMPLE_IPO_DISCLOSURE)
        self.assertEqual((p["sales_forecast_mn"], p["op_forecast_mn"], p["op_margin_pct"]), (4300.0, 340.0, 7.9))
        self.assertIsNone(cr.parse_ipo_forecast_table("関係のない文章"))

    def test_guidance_only_from_explicit_revision_titles(self):
        self.assertEqual(cr.derive_guidance_from_titles(["業績予想の上方修正に関するお知らせ"]), "UPWARD")
        self.assertIsNone(cr.derive_guidance_from_titles(["会社説明及び今後の戦略概要"]))


class PositionCapacityTests(unittest.TestCase):
    def rows(self, n):
        return [{"code": str(i), "quantity": 100, "average_price": 1000, "active": True} for i in range(n)]

    def test19_three_positions_entry_limit(self):
        for n, exp in ((0, "NORMAL"), (1, "NORMAL"), (2, "CAUTION"), (3, "ENTRY_LIMIT"), (5, "ENTRY_LIMIT")):
            self.assertEqual(cr.summarize_positions(self.rows(n))["newEntryCapacity"], exp)
        pos = cr.summarize_positions(self.rows(3))
        self.assertEqual(pos["openPositionCount"], 3)
        self.assertEqual(pos["portfolioExposure"], 300000)
        d = dual([cand("A", 3000, 90), cand("B", 2000, 80)], 700000, pos)
        self.assertEqual(d["capital"]["newEntryCapacity"], "ENTRY_LIMIT")
        self.assertTrue(all(c["entryCapacity"] == "ENTRY_LIMIT" and c["entryCapacityWarning"] for c in d["top5"]))
        self.assertEqual(len(d["top5"]), 2)  # 候補を除外しない（補助判断）

    def test_normal_capacity_adds_no_warning(self):
        d = dual([cand("A", 3000, 90)], 700000, cr.summarize_positions(self.rows(1)))
        self.assertNotIn("entryCapacity", d["top5"][0])

    def test_closed_or_zero_quantity_positions_not_counted(self):
        rows = self.rows(3) + [{"code": "x", "quantity": 0, "average_price": 1}, {"code": "y", "quantity": 5, "active": False}]
        self.assertEqual(cr.summarize_positions(rows)["openPositionCount"], 3)


class CapitalEfficiencyBoundTests(unittest.TestCase):
    def test20_low_quality_cheap_stock_does_not_overtake_high_quality(self):
        d = dual([cand("HIQ", 6500, 92), cand("LOWQ", 500, 60)], 700000)  # HIQは余力使用率92.9%
        self.assertEqual([c["code"] for c in d["top5"]][:1], ["HIQ"])
        for c in d["top5"]:
            self.assertLessEqual(abs(c["capitalEfficiencyAdjustment"]), cr.MAX_CAPITAL_ADJUSTMENT)
        hi = next(c for c in d["top5"] if c["code"] == "HIQ")
        lo = next(c for c in d["top5"] if c["code"] == "LOWQ")
        self.assertGreater(hi["capitalEfficiencyScore"], lo["capitalEfficiencyScore"] + 20)

    def test6_close_scores_allow_flexibility_tiebreak_but_not_more(self):
        # A: 90点・必要650,000(残50,000) / B: 87点・必要350,000(残350,000)
        d = dual([cand("A", 6500, 90), cand("B", 3500, 87)], 700000)
        a = next(c for c in d["top5"] if c["code"] == "A")
        b = next(c for c in d["top5"] if c["code"] == "B")
        self.assertLessEqual(abs(a["capitalEfficiencyAdjustment"]), 5)
        self.assertLessEqual(abs(b["capitalEfficiencyAdjustment"]), 5)
        self.assertGreater(b["capitalEfficiencyAdjustment"], a["capitalEfficiencyAdjustment"])
        # 同等評価(3点差)ではBが上に来てよいが、10点差なら余力自由度では逆転しない
        d2 = dual([cand("A", 6500, 90), cand("B", 3500, 80)], 700000)
        self.assertEqual(d2["top5"][0]["code"], "A")

    def test_adjustment_is_not_a_price_inverse(self):
        for price in (300, 3000, 6000):
            b = cr.compute_buyability(price, 700000)
            adj = cr.capital_adjustment(cand("X", price, 70), b, 0.5)
            self.assertLessEqual(abs(adj), cr.MAX_CAPITAL_ADJUSTMENT)


class MorningWeaknessTests(unittest.TestCase):
    def arrays(self, prev_vol_mult=1.0, prev_gain=0.0, open_gap=0.0):
        n = 30
        closes = [1000.0] * n
        closes[-2] = 1000 * (1 + prev_gain / 100)
        opens = [1000.0] * n
        opens[-1] = closes[-2] * (1 + open_gap / 100)
        vols = [1000.0] * n
        vols[-2] = 1000 * prev_vol_mult
        return (closes, opens, closes, closes, vols)

    def test_protective_reasons(self):
        row = {}
        r = cr.detect_protective_context(row, self.arrays(prev_vol_mult=3.0, prev_gain=8, open_gap=-4), {}, [], is_ipo=True,
                                         recent_limit_up=True, recent_earnings=True)
        for k in ("直近IPO", "決算直後", "ストップ高経験", "前日出来高急増", "前日大幅高", "寄り前GD大"):
            self.assertIn(k, r)
        self.assertIn("材料発生", cr.detect_protective_context(row, None, {}, [{"title": "x"}]))
        self.assertEqual(cr.detect_protective_context(row, self.arrays(), {}, []), [])
        self.assertIn("寄り前GU大", cr.detect_protective_context(row, self.arrays(open_gap=4), {}, []))

    def test_gd_then_vwap_recovery_promotes_protected_candidate(self):
        self.assertEqual(cr.evaluate_ipo_stage(None, {}, 30)[0], "WATCH_LOW")
        self.assertEqual(cr.evaluate_ipo_stage(None, {"vwap_recovered": True}, 30)[0], "WATCH")

    def test_rule_extends_existing_ipo_rule_instead_of_creating(self):
        with mock.patch.object(server, "investment_db") as db:
            db.find_trade_rule_by_source_type.return_value = {"id": 120}
            db.extend_trade_rule.return_value = {"action": "extended", "id": 120}
            r = server.seed_morning_weakness_learning_rule("url", "u")
        self.assertEqual(r["id"], 120)
        db.upsert_trade_rule_from_text.assert_not_called()
        args = db.extend_trade_rule.call_args
        self.assertIn("決算直後", args.args[3])
        self.assertEqual(args.kwargs["source_entry"]["rule_name"], "朝の弱さだけで候補除外しない")

    def test_rule_created_only_when_no_base_rule(self):
        with mock.patch.object(server, "investment_db") as db:
            db.find_trade_rule_by_source_type.return_value = None
            db.upsert_trade_rule_from_text.return_value = {"action": "created", "id": 999}
            server.seed_morning_weakness_learning_rule("url", "u")
        kw = db.upsert_trade_rule_from_text.call_args.kwargs
        self.assertEqual((kw["category"], kw["initial_status"]), ("ENTRY", "TESTING"))

    def test_ipo_seed_does_not_duplicate_after_extension(self):
        with mock.patch.object(server, "investment_db") as db:
            db.find_trade_rule_by_source_type.return_value = {"id": 120}
            r = server.seed_ipo_early_unwatch_learning_rule("url", "u")
        self.assertEqual(r, {"action": "exists", "id": 120})
        db.upsert_trade_rule_from_text.assert_not_called()


class RevivalQualityGateTests(unittest.TestCase):
    """実市場（2026-09-25 09:28）で、entry score 10〜15のWEAK/AVOID銘柄が復活枠経由で
    実戦TOP5に入った不具合の最低品質ゲート。"""

    def test_gate_1_weak_avoid_low_score_revival_stays_out(self):
        weak = cand("5301", 3000, 15, state="WEAK", revivalStage="BUY_CANDIDATE", protectedReasons=["前日出来高急増"])
        d = dual([weak, cand("A", 3000, 80)], 700000)
        self.assertEqual(d["annotated"][[c["code"] for c in d["annotated"]].index("5301")]["capitalStatus"], "AVOID")
        self.assertNotIn("5301", [c["code"] for c in d["top5"]])
        self.assertNotIn("5301", [c["code"] for c in d["analysis"]["top5"]])
        self.assertEqual(server._select_entry_ready_top5([weak])[4]["revived_buy_candidate"], 0)

    def test_gate_1b_weak_state_blocked_even_with_high_score_or_no_annotation(self):
        w = cand("W", 3000, 60, state="WEAK", revivalStage="BUY_CANDIDATE")
        self.assertEqual(server._select_entry_ready_top5([w])[4]["revived_buy_candidate"], 0)
        avoid = cand("V", 3000, 60, state="WATCH", revivalStage="BUY_CANDIDATE", capitalStatus="AVOID")
        self.assertEqual(server._select_entry_ready_top5([avoid])[4]["revived_buy_candidate"], 0)

    def test_gate_1c_score_floor(self):
        low = cand("L", 3000, server.REVIVAL_MIN_ENTRY_SCORE - 1, state="WATCH", revivalStage="BUY_CANDIDATE")
        self.assertEqual(server._select_entry_ready_top5([low])[4]["revived_buy_candidate"], 0)

    def test_gate_2_qualified_revival_enters(self):
        ok = cand("627A", 1300, server.REVIVAL_MIN_ENTRY_SCORE, state="WATCH", revivalStage="BUY_CANDIDATE", shadow=True)
        d = dual([ok, cand("A", 3000, 80)], 700000)
        r = next(c for c in d["top5"] if c["code"] == "627A")
        self.assertTrue(r["revivedBuyCandidate"])
        self.assertNotEqual(r["capitalStatus"], "AVOID")

    def test_gate_3_regular_entry_ready_unaffected(self):
        d = dual([cand("A", 3000, 80), cand("B", 2000, 70, state="NOW_BUYABLE")], 700000)
        self.assertEqual([c["code"] for c in d["top5"]], ["A", "B"])

    def test_gate_4_chase_risk_still_excluded(self):
        risky = cand("Z", 1000, 90, state="CHASE_RISK", revivalStage="BUY_CANDIDATE")
        d = dual([risky, cand("A", 3000, 80)], 700000)
        self.assertNotIn("Z", [c["code"] for c in d["top5"]])

    def test_gate_5_e2e3_style_revival_lane_still_works(self):
        # E2E3と同条件：entry score 44・状態WATCH（Tier1〜3の外）・BUY_CANDIDATE → 復活枠でのみ入る
        rev = cand("627A", 1337, 44, state="WATCH", revivalStage="BUY_CANDIDATE")
        others = [cand("A", 3000, 80), cand("B", 5000, 70, state="WAIT_PULLBACK")]
        self.assertIn("627A", [c["code"] for c in dual(others + [rev], 850000)["top5"]])
        plain = cand("627A", 1337, 44, state="WATCH", revivalStage="WATCH")
        self.assertNotIn("627A", [c["code"] for c in dual(others + [plain], 850000)["top5"]])


if __name__ == "__main__":
    unittest.main()
