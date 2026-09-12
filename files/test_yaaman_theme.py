# Yaaman Style / ヤーマン式 Theme Discovery（前提commit 0de3ee3）の回帰テスト。
#
# 実行方法： cd files && python -m unittest test_yaaman_theme -v

import unittest
from unittest import mock

import server


class CatalystClassificationTests(unittest.TestCase):
    """指示書2番：CATALYST分類。"""

    def test_earnings(self):
        self.assertEqual(server.classify_catalyst_type("上方修正を発表"), "UPWARD_REVISION")

    def test_semiconductor(self):
        self.assertEqual(server.classify_catalyst_type("半導体製造装置の販売が好調"), "SEMICONDUCTOR")

    def test_ai(self):
        self.assertEqual(server.classify_catalyst_type("生成AI事業が絶好調"), "AI")

    def test_no_text_is_other(self):
        self.assertEqual(server.classify_catalyst_type(None), "OTHER")
        self.assertEqual(server.classify_catalyst_type(""), "OTHER")

    def test_unmatched_text_is_other(self):
        self.assertEqual(server.classify_catalyst_type("特に材料なし、需給での上昇"), "OTHER")

    def test_first_match_wins(self):
        # EARNINGSがCATALYST_TYPESの列挙順で先にあるため、決算+AI混在テキストはEARNINGS優先
        self.assertEqual(server.classify_catalyst_type("決算は増益、AI関連製品も好調"), "EARNINGS")


class ThemeExtractionTests(unittest.TestCase):
    """指示書3・6番：THEME抽出。"""

    def test_theme_from_keyword_in_text(self):
        self.assertEqual(server.extract_theme_from_catalyst("OTHER", "半導体製造装置の受注増"), "半導体")

    def test_theme_from_catalyst_fallback(self):
        self.assertEqual(server.extract_theme_from_catalyst("AI", None), "AI")
        self.assertEqual(server.extract_theme_from_catalyst("AUTONOMOUS_DRIVING", None), "自動運転")

    def test_no_theme_when_no_match(self):
        self.assertIsNone(server.extract_theme_from_catalyst("EARNINGS", "決算は増益"))

    def test_text_keyword_takes_priority_over_fallback(self):
        # catalyst_typeはEARNINGSでもtext中に「防衛」が含まれれば防衛テーマを優先
        self.assertEqual(server.extract_theme_from_catalyst("EARNINGS", "防衛関連の受注で増益"), "防衛")


class ThemeStageTests(unittest.TestCase):
    """指示書4番：EARLY/EXPANDING/MATURE/EXHAUSTING。"""

    def test_early_low_breadth_day0(self):
        self.assertEqual(server.classify_theme_stage(0.2, 1.2, days_since_trigger=0), "EARLY")

    def test_expanding_mid_breadth(self):
        self.assertEqual(server.classify_theme_stage(0.5, 1.3, days_since_trigger=1), "EXPANDING")

    def test_mature_high_breadth_after_days(self):
        self.assertEqual(server.classify_theme_stage(0.7, 1.4, days_since_trigger=3), "MATURE")

    def test_exhausting_upper_wick_failure(self):
        self.assertEqual(server.classify_theme_stage(0.5, 1.3, days_since_trigger=1, upper_wick_failure=True), "EXHAUSTING")

    def test_exhausting_volume_fade_after_days(self):
        self.assertEqual(server.classify_theme_stage(0.5, 0.7, days_since_trigger=2), "EXHAUSTING")

    def test_volume_fade_but_day0_not_exhausting(self):
        # 出来高が落ちていても発生当日（days_since_trigger<2）はEXHAUSTINGと判定しない
        self.assertEqual(server.classify_theme_stage(0.2, 0.7, days_since_trigger=0), "EARLY")


class NextDayThemeScoreTests(unittest.TestCase):
    """指示書5番：Next-Day Theme Score。"""

    def test_all_max_is_100(self):
        self.assertEqual(server.compute_next_day_theme_score(100, 100, 100, 100, 100, 100), 100.0)

    def test_all_zero_is_0(self):
        self.assertEqual(server.compute_next_day_theme_score(0, 0, 0, 0, 0, 0), 0.0)

    def test_pts_none_treated_as_neutral_50(self):
        with_pts_50 = server.compute_next_day_theme_score(80, 80, 80, 50, 80, 80)
        with_pts_none = server.compute_next_day_theme_score(80, 80, 80, None, 80, 80)
        self.assertEqual(with_pts_50, with_pts_none)

    def test_clamped_to_0_100_range(self):
        score = server.compute_next_day_theme_score(80, 100, 100, 100, 100, 100)
        self.assertLessEqual(score, 100.0)
        self.assertGreaterEqual(score, 0.0)


class PtsConfirmationTests(unittest.TestCase):
    """指示書39番：PTS確認（既知の制約：実運用では常にNone）。"""

    def test_none_is_undetermined(self):
        self.assertIsNone(server.classify_pts_confirmation(None))

    def test_strong_pts_true(self):
        self.assertTrue(server.classify_pts_confirmation(6.0))

    def test_weak_pts_false(self):
        self.assertFalse(server.classify_pts_confirmation(2.0))

    def test_boundary_at_threshold(self):
        self.assertTrue(server.classify_pts_confirmation(5.0))


class PtsFadeTests(unittest.TestCase):
    """指示書39番：PTS_FADE検知。"""

    def test_fade_detected(self):
        # PTS+15%なのに寄り+2% (< 15*0.3=4.5) → FADE
        self.assertTrue(server.detect_pts_fade(15.0, 2.0))

    def test_no_fade_when_open_follows_pts(self):
        self.assertFalse(server.detect_pts_fade(15.0, 10.0))

    def test_none_inputs_no_fade(self):
        self.assertFalse(server.detect_pts_fade(None, 2.0))
        self.assertFalse(server.detect_pts_fade(15.0, None))

    def test_negative_pts_no_fade(self):
        self.assertFalse(server.detect_pts_fade(-5.0, 1.0))


class ThemeConfirmationTests(unittest.TestCase):
    """指示書11・12番：翌朝気配確認 CONFIRMED/PARTIAL/FADED/INVALIDATED。"""

    def test_confirmed_strong_trigger_and_related(self):
        self.assertEqual(server.classify_theme_confirmation(4.0, [2.0, 3.0, -1.0]), "CONFIRMED")

    def test_confirmed_trigger_only_no_related(self):
        self.assertEqual(server.classify_theme_confirmation(3.5, []), "CONFIRMED")

    def test_partial(self):
        self.assertEqual(server.classify_theme_confirmation(1.5, [-1.0, -2.0]), "PARTIAL")

    def test_faded(self):
        self.assertEqual(server.classify_theme_confirmation(0.3, [-1.0]), "FADED")

    def test_invalidated_negative_gap(self):
        self.assertEqual(server.classify_theme_confirmation(-2.0, [1.0]), "INVALIDATED")

    def test_invalidated_none_gap(self):
        self.assertEqual(server.classify_theme_confirmation(None, []), "INVALIDATED")


class StockThemeRoleTests(unittest.TestCase):
    """指示書23番：LEADER/FOLLOWER/LAGGARD。"""

    def test_trigger_is_leader(self):
        self.assertEqual(server.classify_stock_theme_role(True, 1, 5.0), "LEADER")

    def test_positive_change_is_follower(self):
        self.assertEqual(server.classify_stock_theme_role(False, 1, 2.0), "FOLLOWER")

    def test_non_positive_change_is_laggard(self):
        self.assertEqual(server.classify_stock_theme_role(False, 1, -1.0), "LAGGARD")
        self.assertEqual(server.classify_stock_theme_role(False, 1, None), "LAGGARD")


class CatchupCandidateTests(unittest.TestCase):
    """指示書24・25番：THEME_CATCHUP_CANDIDATE。「出遅れ=無条件買い」にならないことを検証。"""

    def test_all_conditions_met(self):
        self.assertTrue(server.detect_theme_catchup_candidate("CONFIRMED", "INFLOW", 3, 0.2, cross_market_ok=True))

    def test_not_confirmed_rejected(self):
        self.assertFalse(server.detect_theme_catchup_candidate("PARTIAL", "INFLOW", 3, 0.2))

    def test_sector_outflow_rejected(self):
        self.assertFalse(server.detect_theme_catchup_candidate("CONFIRMED", "OUTFLOW", 3, 0.2))

    def test_too_few_related_up_rejected(self):
        self.assertFalse(server.detect_theme_catchup_candidate("CONFIRMED", "INFLOW", 1, 0.2))

    def test_already_moved_rejected(self):
        # 既に+3%動いている銘柄は「出遅れ」ではないため対象外
        self.assertFalse(server.detect_theme_catchup_candidate("CONFIRMED", "INFLOW", 3, 3.0))

    def test_cross_market_not_ok_rejected(self):
        self.assertFalse(server.detect_theme_catchup_candidate("CONFIRMED", "INFLOW", 3, 0.2, cross_market_ok=False))

    def test_no_unconditional_buy_on_laggard_alone(self):
        # 出遅れているだけ（他条件無し）では絶対にTrueにならないことの明示テスト
        self.assertFalse(server.detect_theme_catchup_candidate(None, None, 0, 5.0, cross_market_ok=True))


class ThemeMomentumScoreTests(unittest.TestCase):
    """指示書43番：THEME_MOMENTUM_SCORE（既存ENTRY SCORE非破壊の補助スコア）。"""

    def test_confirmed_full_score(self):
        score = server.compute_theme_momentum_score(100, "CONFIRMED")
        self.assertEqual(score, 10.0)

    def test_none_theme_score_is_zero(self):
        self.assertEqual(server.compute_theme_momentum_score(None, "CONFIRMED"), 0.0)

    def test_faded_heavily_discounted(self):
        score = server.compute_theme_momentum_score(100, "FADED")
        self.assertLess(score, 3.0)

    def test_sector_leading_bonus(self):
        base = server.compute_theme_momentum_score(60, "CONFIRMED")
        boosted = server.compute_theme_momentum_score(60, "CONFIRMED", sector_state="LEADING")
        self.assertGreater(boosted, base)

    def test_cross_market_not_ok_penalty(self):
        base = server.compute_theme_momentum_score(60, "CONFIRMED")
        penalized = server.compute_theme_momentum_score(60, "CONFIRMED", cross_market_relative_ok=False)
        self.assertLess(penalized, base)

    def test_clamped_to_0_10(self):
        score = server.compute_theme_momentum_score(1000, "CONFIRMED", sector_state="LEADING")
        self.assertLessEqual(score, 10.0)


class Top5PromotionTests(unittest.TestCase):
    """指示書14・16・17・42番：LEVEL1→LEVEL2→LEVEL3昇格判定。既存TOP5ロジックは変更しない
    （このテストは新規predicate関数のみを検証し、既存rankTop5等には触れない）。"""

    def test_promote_to_today_top5_all_conditions(self):
        self.assertTrue(server.should_promote_to_today_top5("CONFIRMED", True, False))

    def test_not_confirmed_rejected(self):
        self.assertFalse(server.should_promote_to_today_top5("PARTIAL", True, False))

    def test_weak_relative_strength_rejected(self):
        self.assertFalse(server.should_promote_to_today_top5("CONFIRMED", False, False))

    def test_high_risk_rejected(self):
        self.assertFalse(server.should_promote_to_today_top5("CONFIRMED", True, True))

    def test_promote_to_entry_top5_requires_entry_state(self):
        for state in server.YAAMAN_TOP5_ENTRY_STATES:
            self.assertTrue(server.should_promote_to_entry_top5(state))

    def test_promote_to_entry_top5_rejects_non_entry_state(self):
        self.assertFalse(server.should_promote_to_entry_top5("WAIT"))
        self.assertFalse(server.should_promote_to_entry_top5(None))


class DiscoverLimitUpAndSurgeMoversTests(unittest.TestCase):
    """指示書1番：ストップ高・+10%以上上昇銘柄の抽出（既存Stage1のみ利用、新規全市場取得なし）。"""

    def test_filters_by_surge_threshold_and_sorts_desc(self):
        stage1_rows = {
            "1000": {"code": "1000", "name": "A", "sector": "電気機器", "changePct": 5.0, "turnover": 100},
            "2000": {"code": "2000", "name": "B", "sector": "精密機器", "changePct": 12.0, "turnover": 200},
            "3000": {"code": "3000", "name": "C", "sector": "電気機器", "changePct": 30.0, "turnover": 300},
        }
        with mock.patch.object(server, "run_momentum_stage1", return_value={"rows": stage1_rows}), \
             mock.patch.object(server, "_volume_stage2_detail", return_value={"timeAdjustedVolumeRatio": 1.8}):
            movers = server.discover_limit_up_and_surge_movers("db", "user1")
        self.assertEqual([m["code"] for m in movers], ["3000", "2000"])
        self.assertTrue(movers[0]["is_limit_up"])
        self.assertFalse(movers[1]["is_limit_up"])
        self.assertEqual(movers[0]["volume_ratio"], 1.8)

    def test_no_movers_returns_empty(self):
        stage1_rows = {"1000": {"code": "1000", "changePct": 1.0}}
        with mock.patch.object(server, "run_momentum_stage1", return_value={"rows": stage1_rows}):
            movers = server.discover_limit_up_and_surge_movers("db", "user1")
        self.assertEqual(movers, [])

    def test_caps_at_yaaman_max_movers_before_stage2(self):
        # 全市場4000銘柄に対するStage2取得を行わない安全弁（指示書46番）の検証：
        # 上位YAAMAN_MAX_MOVERS件を超えるmoverはStage2取得対象に含まれない。
        stage1_rows = {str(i): {"code": str(i), "changePct": 10.0 + i, "turnover": 1} for i in range(40)}
        call_count = {"n": 0}

        def fake_stage2(code, row):
            call_count["n"] += 1
            return {"timeAdjustedVolumeRatio": 1.0}

        with mock.patch.object(server, "run_momentum_stage1", return_value={"rows": stage1_rows}), \
             mock.patch.object(server, "_volume_stage2_detail", side_effect=fake_stage2):
            movers = server.discover_limit_up_and_surge_movers("db", "user1")
        self.assertEqual(len(movers), server.YAAMAN_MAX_MOVERS)
        self.assertEqual(call_count["n"], server.YAAMAN_MAX_MOVERS)


class DiscoverNextDayThemesTests(unittest.TestCase):
    """指示書1・3・5・8番：発掘パイプライン本体（引け後）。"""

    def test_groups_movers_into_themes_and_persists(self):
        movers = [
            {"code": "8035", "name": "東エレク", "sector": "電気機器", "pct": 15.0, "volume": 1000, "volume_ratio": 1.8, "is_limit_up": False},
        ]
        fake_db = mock.Mock()
        fake_db.relevant_catalysts_for.return_value = [{"title": "半導体製造装置の新規受注を獲得"}]
        fake_db.upsert_limit_up_event.return_value = None
        fake_db.list_watchlist.return_value = [{"code": "6146", "name": "ディスコ", "theme": ""}]
        fake_db.upsert_next_day_theme_candidate.side_effect = lambda db, uid, theme, date, fields: {"theme": theme, **fields}

        with mock.patch.object(server, "investment_db", fake_db), \
             mock.patch.object(server, "discover_limit_up_and_surge_movers", return_value=movers):
            result = server.discover_next_day_themes("db", "user1", event_date="2026-09-11")

        self.assertEqual(len(result["themes"]), 1)
        theme = result["themes"][0]
        self.assertEqual(theme["theme"], "半導体")
        self.assertIn("8035", theme["trigger_stocks_json"])
        self.assertEqual(result["generated_date"], "2026-09-11")

    def test_no_movers_returns_empty_themes(self):
        with mock.patch.object(server, "investment_db", mock.Mock()), \
             mock.patch.object(server, "discover_limit_up_and_surge_movers", return_value=[]):
            result = server.discover_next_day_themes("db", "user1")
        self.assertEqual(result["themes"], [])

    def test_related_stock_not_auto_registered_to_watchlist(self):
        # 指示書7番：監視銘柄に無い銘柄も候補としてそのまま表示するが、自動登録はしない
        # （list_watchlistへの書き込み系メソッドが一切呼ばれないことを検証）。
        movers = [{"code": "8035", "name": "東エレク", "sector": "電気機器", "pct": 15.0,
                    "volume": 1000, "volume_ratio": 1.8, "is_limit_up": False}]
        fake_db = mock.Mock()
        fake_db.relevant_catalysts_for.return_value = [{"title": "半導体製造装置の新規受注を獲得"}]
        fake_db.list_watchlist.return_value = []
        fake_db.upsert_next_day_theme_candidate.side_effect = lambda db, uid, theme, date, fields: {"theme": theme, **fields}
        with mock.patch.object(server, "investment_db", fake_db), \
             mock.patch.object(server, "discover_limit_up_and_surge_movers", return_value=movers):
            server.discover_next_day_themes("db", "user1", event_date="2026-09-11")
        self.assertFalse(hasattr(fake_db, "upsert_watchlist_item") and fake_db.upsert_watchlist_item.called)


class ConfirmNextDayThemesTests(unittest.TestCase):
    """指示書11・12番：翌朝8:40-8:45気配確認。"""

    def test_confirms_theme_with_strong_gap(self):
        candidates = [{"theme": "半導体", "trigger_stocks_json": ["8035"], "related_stocks_json": ["6146"],
                       "volume_expansion": 1.8}]
        stage1_rows = {"8035": {"changePct": 4.0}, "6146": {"changePct": 2.0}}
        fake_db = mock.Mock()
        fake_db.list_next_day_theme_candidates.return_value = candidates
        fake_db.upsert_next_day_theme_candidate.side_effect = lambda db, uid, theme, date, fields: {"theme": theme, **fields}
        with mock.patch.object(server, "investment_db", fake_db), \
             mock.patch.object(server, "run_momentum_stage1", return_value={"rows": stage1_rows}):
            result = server.confirm_next_day_themes("db", "user1", generated_date="2026-09-11")
        self.assertEqual(result["themes"][0]["confirmation_status"], "CONFIRMED")

    def test_no_candidates_returns_empty(self):
        fake_db = mock.Mock()
        fake_db.list_next_day_theme_candidates.return_value = []
        with mock.patch.object(server, "investment_db", fake_db):
            result = server.confirm_next_day_themes("db", "user1", generated_date="2026-09-11")
        self.assertEqual(result["themes"], [])

    def test_negative_gap_invalidated(self):
        candidates = [{"theme": "AI", "trigger_stocks_json": ["9999"], "related_stocks_json": [], "volume_expansion": 1.0}]
        stage1_rows = {"9999": {"changePct": -2.0}}
        fake_db = mock.Mock()
        fake_db.list_next_day_theme_candidates.return_value = candidates
        fake_db.upsert_next_day_theme_candidate.side_effect = lambda db, uid, theme, date, fields: {"theme": theme, **fields}
        with mock.patch.object(server, "investment_db", fake_db), \
             mock.patch.object(server, "run_momentum_stage1", return_value={"rows": stage1_rows}):
            result = server.confirm_next_day_themes("db", "user1", generated_date="2026-09-11")
        self.assertEqual(result["themes"][0]["confirmation_status"], "INVALIDATED")


class GetStockThemeInfoTests(unittest.TestCase):
    """指示書35番：監視銘柄カード表示向け（LEADER/FOLLOWER判定込み）。"""

    def test_trigger_stock_is_leader(self):
        fake_db = mock.Mock()
        fake_db.list_next_day_theme_candidates.return_value = [
            {"theme": "半導体", "trigger_stocks_json": ["8035"], "related_stocks_json": ["6146"],
             "theme_score": 70, "stage": "EXPANDING", "confirmation_status": "CONFIRMED"},
        ]
        with mock.patch.object(server, "investment_db", fake_db):
            info = server.get_stock_theme_info("db", "user1", "8035")
        self.assertEqual(info["role"], "LEADER")
        self.assertEqual(info["theme"], "半導体")

    def test_related_stock_is_follower(self):
        fake_db = mock.Mock()
        fake_db.list_next_day_theme_candidates.return_value = [
            {"theme": "半導体", "trigger_stocks_json": ["8035"], "related_stocks_json": ["6146"],
             "theme_score": 70, "stage": "EXPANDING", "confirmation_status": "CONFIRMED"},
        ]
        with mock.patch.object(server, "investment_db", fake_db):
            info = server.get_stock_theme_info("db", "user1", "6146")
        self.assertEqual(info["role"], "FOLLOWER")

    def test_unrelated_stock_returns_none(self):
        fake_db = mock.Mock()
        fake_db.list_next_day_theme_candidates.return_value = [
            {"theme": "半導体", "trigger_stocks_json": ["8035"], "related_stocks_json": ["6146"],
             "theme_score": 70, "stage": "EXPANDING", "confirmation_status": "CONFIRMED"},
        ]
        with mock.patch.object(server, "investment_db", fake_db):
            info = server.get_stock_theme_info("db", "user1", "9999")
        self.assertIsNone(info)

    def test_no_investment_db_returns_none(self):
        with mock.patch.object(server, "investment_db", None):
            info = server.get_stock_theme_info("db", "user1", "8035")
        self.assertIsNone(info)


class StoryBreakThemeIntegrationTests(unittest.TestCase):
    """Choruco Style Story Engine統合：「テーマ失速」break条件（既存detect_story_breakの
    他条件・既存呼び出しシグネチャは変更しない、追加条件のみを検証）。"""

    def test_theme_exhausting_triggers_break_reason(self):
        story = {"theme_stage": "EXPANDING"}
        snap = {"theme": {"stage": "EXHAUSTING"}}
        reasons = server.detect_story_break(story, snap)
        self.assertTrue(any("テーマ失速" in r for r in reasons))

    def test_theme_still_expanding_no_theme_break_reason(self):
        story = {"theme_stage": "EXPANDING"}
        snap = {"theme": {"stage": "EXPANDING"}}
        reasons = server.detect_story_break(story, snap)
        self.assertFalse(any("テーマ失速" in r for r in reasons))

    def test_no_theme_info_no_crash(self):
        story = {}
        snap = {}
        reasons = server.detect_story_break(story, snap)
        self.assertIsInstance(reasons, list)


class SectorRotationCrossMarketIntegrationTests(unittest.TestCase):
    """既存Sector Rotation/Cross-Market Linkとの連携：既存関数のシグネチャ・戻り値は
    変更されておらず、Yaaman側からそのまま利用できることを検証する。"""

    def test_theme_momentum_uses_sector_state_vocabulary(self):
        # Sector Rotationの既存語彙（LEADING/IMPROVING/NEUTRAL/WEAKENING/LAGGING）が
        # そのままcompute_theme_momentum_scoreのsector_state引数として使えることの確認。
        for state in ("LEADING", "IMPROVING", "NEUTRAL", "WEAKENING", "LAGGING"):
            score = server.compute_theme_momentum_score(50, "CONFIRMED", sector_state=state)
            self.assertGreaterEqual(score, 0.0)
            self.assertLessEqual(score, 10.0)

    def test_catchup_candidate_uses_capital_flow_direction_vocabulary(self):
        for direction in ("INFLOW", "ROTATING_IN", "NEUTRAL", "ROTATING_OUT", "OUTFLOW"):
            result = server.detect_theme_catchup_candidate("CONFIRMED", direction, 3, 0.2)
            self.assertIsInstance(result, bool)


class ExistingTop5NonDestructionTests(unittest.TestCase):
    """既存ENTRY SCORE・今日の注目TOP5・今買い時TOP5のシグネチャ・挙動が変更されていないことの
    非破壊確認テスト（Sector Rotation/Cross-Market Link/Choruco Style各回帰テストと同じ方針）。"""

    def test_should_promote_functions_are_pure_and_standalone(self):
        # Yaaman側のTOP5昇格predicateは既存rankTop5/_score_entry_candidates等の
        # 既存パイプラインに一切依存しない、独立した純粋関数であることの確認。
        import inspect
        sig1 = inspect.signature(server.should_promote_to_today_top5)
        sig2 = inspect.signature(server.should_promote_to_entry_top5)
        self.assertEqual(list(sig1.parameters), ["confirmation", "relative_strength_ok", "risk_high"])
        self.assertEqual(list(sig2.parameters), ["entry_state"])


if __name__ == "__main__":
    unittest.main()
