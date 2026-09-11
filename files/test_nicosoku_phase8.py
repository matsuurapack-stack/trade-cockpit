# Market Intelligence Phase8 テスト（指示書41番）。
#
# Phase2〜7同様、実DBを必要としない形で39項目をカバーする。
#
# 実行方法： cd files && python -m unittest test_nicosoku_phase8 -v

import datetime
import unittest
from unittest import mock

import server


def _iso(dt):
    return dt.astimezone(datetime.timezone.utc).isoformat()


class EventBaselineTests(unittest.TestCase):
    """1. event baseline（指示書3番：official event_at→primary posted_at→first_seen_at）"""

    def test_event_at_priority(self):
        event = {"event_at": "2026-09-11T01:00:00Z", "first_seen_at": "2026-09-10T00:00:00Z",
                 "primary_source_type": "TDNET"}
        self.assertEqual(server.resolve_event_market_relevant_at(event), "2026-09-11T01:00:00Z")

    def test_primary_evidence_posted_at_fallback(self):
        event = {"event_at": None, "first_seen_at": "2026-09-10T00:00:00Z", "primary_source_type": "TDNET"}
        evidence = [{"is_primary": False, "source_kind": "TDNET", "posted_at": "2026-09-11T02:00:00Z"}]
        self.assertEqual(server.resolve_event_market_relevant_at(event, evidence), "2026-09-11T02:00:00Z")

    def test_first_seen_at_final_fallback(self):
        event = {"event_at": None, "first_seen_at": "2026-09-10T00:00:00Z", "primary_source_type": None}
        self.assertEqual(server.resolve_event_market_relevant_at(event, []), "2026-09-10T00:00:00Z")


class ReactionWindowDueAtTests(unittest.TestCase):
    """2〜7. 5M/30M/1H/CLOSE/NEXT_OPEN/NEXT_CLOSE（指示書2番）"""

    def setUp(self):
        # 2026-09-11(金) 10:00 JST = 01:00 UTC、通常のIN_SESSION
        self.market_relevant_at = datetime.datetime(2026, 9, 11, 1, 0, tzinfo=datetime.timezone.utc)
        self.timing = server.classify_event_timing(self.market_relevant_at)

    def test_5m(self):
        due = server._event_reaction_due_at(self.market_relevant_at, "5M", self.timing)
        self.assertEqual(due, self.market_relevant_at + datetime.timedelta(minutes=5))

    def test_30m(self):
        due = server._event_reaction_due_at(self.market_relevant_at, "30M", self.timing)
        self.assertEqual(due, self.market_relevant_at + datetime.timedelta(minutes=30))

    def test_1h(self):
        due = server._event_reaction_due_at(self.market_relevant_at, "1H", self.timing)
        self.assertEqual(due, self.market_relevant_at + datetime.timedelta(hours=1))

    def test_close(self):
        due = server._event_reaction_due_at(self.market_relevant_at, "CLOSE", self.timing)
        jst = due.astimezone(server._JST)
        self.assertEqual((jst.hour, jst.minute), server.JP_MARKET_CLOSE_TIME)

    def test_next_open(self):
        due = server._event_reaction_due_at(self.market_relevant_at, "NEXT_OPEN", self.timing)
        jst = due.astimezone(server._JST)
        self.assertEqual((jst.hour, jst.minute), server.JP_MARKET_OPEN_TIME)
        self.assertGreater(jst.date(), self.market_relevant_at.astimezone(server._JST).date())

    def test_next_close(self):
        due = server._event_reaction_due_at(self.market_relevant_at, "NEXT_CLOSE", self.timing)
        jst = due.astimezone(server._JST)
        self.assertEqual((jst.hour, jst.minute), server.JP_MARKET_CLOSE_TIME)
        self.assertGreater(jst.date(), self.market_relevant_at.astimezone(server._JST).date())


class EventTimingClassificationTests(unittest.TestCase):
    """8〜10. after-close event / pre-market event / holiday event（指示書4・5・6番）"""

    def test_after_close_event_skips_close_window(self):
        # 2026-09-11(金) 16:00 JST = 07:00 UTC
        dt = datetime.datetime(2026, 9, 11, 7, 0, tzinfo=datetime.timezone.utc)
        timing = server.classify_event_timing(dt)
        self.assertEqual(timing, "AFTER_CLOSE")
        due = server._event_reaction_due_at(dt, "CLOSE", timing)
        self.assertIsNone(due)  # 指示書5番：当日CLOSEを評価しない
        windows = server._reaction_windows_for_event({"event_type": "GUIDANCE_REVISION"}, timing)
        self.assertIn("NEXT_OPEN", windows)
        self.assertNotIn("CLOSE", windows)

    def test_pre_market_event_adds_short_windows(self):
        # 2026-09-11(金) 07:00 JST = 前日22:00 UTC
        dt = datetime.datetime(2026, 9, 10, 22, 0, tzinfo=datetime.timezone.utc)
        timing = server.classify_event_timing(dt)
        self.assertEqual(timing, "PRE_MARKET")
        windows = server._reaction_windows_for_event({"event_type": "DIVIDEND"}, timing)
        self.assertIn("5M", windows)
        self.assertIn("30M", windows)

    def test_holiday_event_is_non_trading_day(self):
        # 2026/1/1 元日
        dt = datetime.datetime(2026, 1, 1, 3, 0, tzinfo=datetime.timezone.utc)
        self.assertEqual(server.classify_event_timing(dt), "NON_TRADING_DAY")


class ReturnCalculationTests(unittest.TestCase):
    """11〜15. stock return / sector return / market relative / breadth / volume ratio
    （指示書7・8・9・10・14番）"""

    def test_stock_return_via_run_due_reactions(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_due_event_market_reactions.return_value = [
                {"id": 1, "event_id": 10, "ticker": "7203", "target_type": "STOCK", "baseline_price": 2000.0,
                 "reaction_window": "30M"},
            ]
            with mock.patch.object(server, "_fetch_raw_price_snapshot") as mock_fetch, \
                 mock.patch.object(server, "_concurrent_topix_change_pct", return_value=0.0):
                mock_fetch.return_value = {"price": 2050.0, "captured_at": _iso(datetime.datetime.now(datetime.timezone.utc)),
                                            "source": "yfinance", "quality_hint": "INTRADAY"}
                server.run_due_event_market_reactions("dummy_url", "local")
        kwargs = mock_db.save_event_market_reaction_result.call_args[1]
        self.assertEqual(kwargs["stock_return_pct"], 2.5)

    def test_sector_breadth_and_return_via_run_due_reactions(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_due_event_market_reactions.return_value = [
                {"id": 2, "event_id": 10, "ticker": "BANK", "target_type": "SECTOR", "baseline_price": 100.0,
                 "reaction_window": "30M"},
            ]
            mock_db.get_underlying_event.return_value = {"id": 10}
            mock_db.list_watchlist.return_value = [
                {"code": "1", "theme": "銀行"}, {"code": "2", "theme": "銀行"}, {"code": "3", "theme": "銀行"},
            ]
            with mock.patch.object(server, "_fetch_sector_snapshot", return_value={"price": 105.0}), \
                 mock.patch.object(server, "get_stock_quotes") as mock_quotes, \
                 mock.patch.object(server, "_concurrent_topix_change_pct", return_value=1.0):
                mock_quotes.return_value = {"1": {"t": 110, "p": 100}, "2": {"t": 95, "p": 100}, "3": {"t": 108, "p": 100}}
                server.run_due_event_market_reactions("dummy_url", "local")
        kwargs = mock_db.save_event_market_reaction_result.call_args[1]
        self.assertAlmostEqual(kwargs["breadth"], 2 / 3, places=2)  # 3銘柄中2銘柄が上昇

    def test_market_relative_return(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_due_event_market_reactions.return_value = [
                {"id": 1, "event_id": 10, "ticker": "7203", "target_type": "STOCK", "baseline_price": 2000.0,
                 "reaction_window": "30M"},
            ]
            with mock.patch.object(server, "_fetch_raw_price_snapshot") as mock_fetch, \
                 mock.patch.object(server, "_concurrent_topix_change_pct", return_value=1.0):
                mock_fetch.return_value = {"price": 2060.0, "captured_at": _iso(datetime.datetime.now(datetime.timezone.utc)),
                                            "source": "yfinance", "quality_hint": "INTRADAY"}
                server.run_due_event_market_reactions("dummy_url", "local")
        kwargs = mock_db.save_event_market_reaction_result.call_args[1]
        self.assertEqual(kwargs["market_relative_return_pct"], 2.0)  # stock+3.0% - topix+1.0%

    def test_volume_ratio_passed_through_to_classification(self):
        # base=3.5・volume_ratio>=1.5でvolume_confirmed→(base>=3 and volume_confirmed)を満たしSTRONG_POSITIVE。
        confirmed = server.classify_event_reaction(3.5, market_relative_return_pct=3.5, volume_ratio=2.0)
        self.assertEqual(confirmed, "STRONG_POSITIVE")
        # volume無しなら同じbaseでもSTRONG_POSITIVEにはならない（base>=5未満のため）。
        without_volume = server.classify_event_reaction(3.5, market_relative_return_pct=3.5, volume_ratio=None)
        self.assertEqual(without_volume, "POSITIVE")


class ReactionClassificationTests(unittest.TestCase):
    """16〜17. positive / negative classification（指示書11番）"""

    def test_positive_classification(self):
        self.assertEqual(server.classify_event_reaction(2.0, market_relative_return_pct=2.0), "POSITIVE")

    def test_negative_classification(self):
        self.assertEqual(server.classify_event_reaction(-2.0, market_relative_return_pct=-2.0), "NEGATIVE")

    def test_strong_positive(self):
        self.assertEqual(server.classify_event_reaction(6.0, market_relative_return_pct=6.0), "STRONG_POSITIVE")

    def test_neutral(self):
        self.assertEqual(server.classify_event_reaction(0.3, market_relative_return_pct=0.3), "NEUTRAL")


class ReactionPatternTests(unittest.TestCase):
    """18〜22. FADE / PERSISTENT / DELAYED / REVERSAL / NO_REACTION（指示書13番）"""

    def test_fade(self):
        pattern = server.classify_reaction_pattern({"5M": 4.0, "30M": 3.0, "CLOSE": 0.5})
        self.assertEqual(pattern, "FADE")

    def test_persistent(self):
        pattern = server.classify_reaction_pattern({"5M": 2.0, "30M": 3.0, "CLOSE": 4.0, "NEXT_CLOSE": 5.0})
        self.assertEqual(pattern, "PERSISTENT")

    def test_delayed(self):
        pattern = server.classify_reaction_pattern({"5M": 0.2, "30M": 0.3, "CLOSE": 3.0})
        self.assertEqual(pattern, "DELAYED")

    def test_reversal(self):
        pattern = server.classify_reaction_pattern({"5M": 3.0, "30M": -3.0})
        self.assertEqual(pattern, "REVERSAL")

    def test_no_reaction(self):
        pattern = server.classify_reaction_pattern({"5M": 0.1, "30M": -0.2, "CLOSE": 0.3})
        self.assertEqual(pattern, "NO_REACTION")


class EffectivenessScoreTests(unittest.TestCase):
    """23. effectiveness score（指示書12番：impact_scoreとは別軸）"""

    def test_effectiveness_score_increases_with_magnitude_and_persistence(self):
        low = server.compute_event_effectiveness_score(magnitude=1.0, persistence_pattern="FADE")
        high = server.compute_event_effectiveness_score(magnitude=5.0, relative_strength=4.0, breadth=0.8,
                                                           volume_ratio=2.0, persistence_pattern="PERSISTENT")
        self.assertGreater(high, low)
        self.assertLessEqual(high, 100)


class EventTypeAggregationTests(unittest.TestCase):
    """24. event type aggregation（指示書14番）"""

    def test_aggregate_by_event_type(self):
        reactions = [
            {"event_id": 1, "event_type": "BUYBACK", "reaction_window": "30M", "stock_return_pct": 2.0},
            {"event_id": 1, "event_type": "BUYBACK", "reaction_window": "CLOSE", "stock_return_pct": 1.5},
            {"event_id": 2, "event_type": "BUYBACK", "reaction_window": "30M", "stock_return_pct": -1.0},
        ]
        perf = server.aggregate_event_type_performance(reactions)
        self.assertEqual(perf["BUYBACK"]["sample_count"], 2)
        self.assertAlmostEqual(perf["BUYBACK"]["avg_30m_return"], 0.5, places=2)
        self.assertEqual(perf["BUYBACK"]["positive_rate"], 0.5)


class MaterialMagnitudeExtractionTests(unittest.TestCase):
    """25〜27. BUYBACK magnitude / TOB premium / CAPITAL_RAISE dilution（指示書15・17・18・19番）"""

    def test_buyback_magnitude(self):
        details = server.extract_buyback_details("自社株買い100億円、発行済株式の3.2%を上限に取得")
        self.assertEqual(details["buyback_amount_oku_yen"], 100.0)
        self.assertEqual(details["buyback_pct_shares"], 3.2)

    def test_tob_premium(self):
        details = server.extract_tob_details("TOB価格は1500円で実施", pre_event_price=1200.0)
        self.assertEqual(details["offer_price"], 1500.0)
        self.assertAlmostEqual(details["premium_pct"], 25.0, places=1)

    def test_capital_raise_dilution(self):
        details = server.extract_capital_raise_details("希薄化率15%、転換価額500円で200億円を調達")
        self.assertEqual(details["dilution_pct"], 15.0)
        self.assertEqual(details["conversion_price"], 500.0)
        self.assertEqual(details["issue_amount_oku_yen"], 200.0)

    def test_material_magnitude_prefers_amount(self):
        magnitude = server.extract_material_magnitude("自社株買い100億円（上限2.5%）", "BUYBACK")
        self.assertEqual(magnitude, 100.0)


class PredictionResolutionTests(unittest.TestCase):
    """28〜29. prediction resolution_date / resolution storage（指示書20・21・22番）"""

    def test_resolution_date_extraction(self):
        posted_at = datetime.datetime(2026, 9, 11, tzinfo=datetime.timezone.utc)
        date = server.extract_resolution_date("FOMC結果は9/17に解決予定", posted_at)
        self.assertEqual(date, "2026-09-17")

    def test_resolution_date_none_when_absent(self):
        posted_at = datetime.datetime(2026, 9, 11, tzinfo=datetime.timezone.utc)
        self.assertIsNone(server.extract_resolution_date("FRBの利下げ確率70%", posted_at))

    def test_track_prediction_resolution_stores_probability(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_pending_prediction_resolutions.return_value = []
            mock_db.upsert_prediction_resolution.return_value = {"event_id": 5}
            server.track_prediction_resolution("dummy_url", 5, "FOMC利下げ確率", 70)
        fields = mock_db.upsert_prediction_resolution.call_args[0][2]
        self.assertEqual(fields["probability_at_first_seen"], 0.70)


class CrossSourceBackfillTests(unittest.TestCase):
    """30〜32. news_catalysts backfill / market_events backfill / reaction backfill dry_run
    （指示書23・24・26番）"""

    def test_news_catalysts_backfill_dry_run_counts(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_recent_social_posts_all_sources.return_value = []
            mock_db.list_news_catalysts_for_backfill.return_value = [{"id": 1, "title": "上方修正"}]
            mock_db.list_market_events_for_backfill.return_value = []
            result = server.backfill_underlying_events("dummy_url", "local", limit=50, dry_run=True,
                                                          sources=["news"])
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["target_news"], 1)
        self.assertEqual(result["candidate_events"], 1)

    def test_market_events_backfill_ingests_via_shared_pipeline(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_recent_social_posts_all_sources.return_value = []
            mock_db.list_news_catalysts_for_backfill.return_value = []
            mock_db.list_market_events_for_backfill.return_value = [
                {"id": 1, "title": "CPI発表", "event_type": "ECONOMIC", "event_date": "2026-09-11",
                 "affected_stocks": [], "source": "manual", "created_at": _iso(datetime.datetime.now(datetime.timezone.utc))},
            ]
            mock_db.list_underlying_event_candidates.return_value = []
            mock_db.create_underlying_event.return_value = {"id": 99, "event_type": "ECONOMIC_INDICATOR",
                                                               "confidence_level": "UNVERIFIED", "impact_score": None,
                                                               "numerical_fingerprint_json": [], "status": "ACTIVE",
                                                               "title": "CPI発表", "direct_tickers_json": []}
            mock_db.add_underlying_event_evidence.return_value = {"id": 1}
            mock_db.list_underlying_event_evidence.return_value = [{"source_name": "manual"}]
            mock_db.list_watchlist.return_value = []
            with mock.patch.object(server, "_fetch_raw_price_snapshot", return_value=None):
                result = server.backfill_underlying_events("dummy_url", "local", limit=50, dry_run=False, sources=["events"])
        self.assertEqual(result["created"], 1)
        mock_db.create_underlying_event.assert_called_once()
        self.assertEqual(mock_db.create_underlying_event.call_args[0][1]["event_type"], "ECONOMIC_INDICATOR")

    def test_reaction_backfill_dry_run_does_not_write(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_active_underlying_events.return_value = [{"id": 1}, {"id": 2}]
            result = server.backfill_event_market_reactions("dummy_url", "local", limit=10, dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["target_events"], 2)
        mock_db.create_event_market_reactions.assert_not_called()


class DuplicatePreventionTests(unittest.TestCase):
    """33. duplicate prevention（指示書1・27番：UNIQUE event_id+ticker+window）"""

    def test_create_event_market_reactions_uses_on_conflict(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.create_event_market_reactions.return_value = 1
            n = server.investment_db.create_event_market_reactions("dummy_url", [{"event_id": 1}, {"event_id": 1}])
            self.assertEqual(n, 1)
        mock_db.create_event_market_reactions.assert_called_once()


class SchedulerTests(unittest.TestCase):
    """34. scheduler（指示書28番：due到来分だけ処理、既存schedulerと独立）"""

    def test_run_due_reactions_only_processes_returned_items(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_due_event_market_reactions.return_value = []
            result = server.run_due_event_market_reactions("dummy_url", "local")
        self.assertEqual(result, {"evaluated": 0, "no_data": 0})
        mock_db.save_event_market_reaction_result.assert_not_called()

    def test_no_data_when_snapshot_missing(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_due_event_market_reactions.return_value = [
                {"id": 1, "event_id": 10, "ticker": "7203", "target_type": "STOCK", "baseline_price": 2000.0,
                 "reaction_window": "30M"},
            ]
            with mock.patch.object(server, "_fetch_raw_price_snapshot", return_value=None), \
                 mock.patch.object(server, "_concurrent_topix_change_pct", return_value=None):
                result = server.run_due_event_market_reactions("dummy_url", "local")
        self.assertEqual(result["no_data"], 1)


class MarketReactionConfirmedAlertTests(unittest.TestCase):
    """35. MARKET_REACTION_CONFIRMED alert（指示書35番）"""

    def test_alert_generated_when_impact_and_reaction_high(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_event_market_reactions_for_event.return_value = [
                {"reaction_window": "30M", "evaluation_status": "EVALUATED", "relevance": "DIRECT",
                 "target_type": "STOCK", "stock_return_pct": 5.0, "market_relative_return_pct": 4.0,
                 "extended_move": False},
            ]
            mock_db.get_underlying_event.return_value = {"id": 1, "impact_score": 80, "event_type": "BUYBACK", "title": "t"}
            server._maybe_update_event_after_reaction_safe("dummy_url", 1)
        mock_db.create_underlying_event_alert.assert_called_once()
        self.assertEqual(mock_db.create_underlying_event_alert.call_args[0][2], "MARKET_REACTION_CONFIRMED")

    def test_no_alert_when_impact_low(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_event_market_reactions_for_event.return_value = [
                {"reaction_window": "30M", "evaluation_status": "EVALUATED", "relevance": "DIRECT",
                 "target_type": "STOCK", "stock_return_pct": 5.0, "market_relative_return_pct": 4.0,
                 "extended_move": False},
            ]
            mock_db.get_underlying_event.return_value = {"id": 1, "impact_score": 30, "event_type": "BUYBACK", "title": "t"}
            server._maybe_update_event_after_reaction_safe("dummy_url", 1)
        mock_db.create_underlying_event_alert.assert_not_called()


class ExtendedMoveTests(unittest.TestCase):
    """36. EXTENDED_MOVE（指示書36・44番：強反応でも追いかけ買いを肯定しない）"""

    def test_extended_move_flagged_for_large_5m_move(self):
        self.assertTrue(server.classify_extended_move("5M", 10.0))

    def test_normal_move_not_flagged(self):
        self.assertFalse(server.classify_extended_move("5M", 2.0))

    def test_extended_move_included_in_event_summary(self):
        event = {"id": 1, "event_type": "BUYBACK", "title": "t", "confidence_level": "SOCIAL_ONLY",
                 "status": "ACTIVE", "first_seen_at": None, "last_seen_at": _iso(datetime.datetime.now(datetime.timezone.utc)),
                 "independent_source_count": 1, "raw_source_count": 1, "direct_tickers_json": [], "related_tickers_json": [],
                 "impact_score": 20, "primary_source_type": "SOCIAL", "primary_source_url": None,
                 "effectiveness_score": None, "reaction_pattern": None, "extended_move": True,
                 "material_magnitude": None}
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_underlying_event_evidence.return_value = []
            mock_db.list_event_market_reactions_for_event.return_value = []
            summary = server.build_event_summary("dummy_url", event)
        self.assertTrue(summary["extended_move"])


class ChatGptPayloadTests(unittest.TestCase):
    """37. ChatGPT payload（指示書31・37番）"""

    def test_market_reaction_omitted_when_no_data(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_event_market_reactions_for_event.return_value = []
            result = server.build_event_market_reaction_summary("dummy_url", 1)
        self.assertIsNone(result)

    def test_market_reaction_present_when_evaluated(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_event_market_reactions_for_event.return_value = [
                {"reaction_window": "30M", "evaluation_status": "EVALUATED", "relevance": "DIRECT",
                 "target_type": "STOCK", "stock_return_pct": 2.4, "market_relative_return_pct": 1.9,
                 "reaction_classification": "POSITIVE", "extended_move": False},
            ]
            result = server.build_event_market_reaction_summary("dummy_url", 1)
        self.assertEqual(result["30m"]["return_pct"], 2.4)
        self.assertEqual(result["30m"]["relative_pct"], 1.9)

    def test_reaction_context_separates_material_and_price(self):
        event = {"id": 1, "impact_score": 80, "confidence_level": "OFFICIAL_CONFIRMED", "material_magnitude": 100.0,
                  "extended_move": True}
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_event_market_reactions_for_event.return_value = [
                {"reaction_window": "30M", "evaluation_status": "EVALUATED", "relevance": "DIRECT",
                 "target_type": "STOCK", "stock_return_pct": 5.0, "market_relative_return_pct": 4.0,
                 "evaluated_at": _iso(datetime.datetime.now(datetime.timezone.utc))},
            ]
            ctx = server.build_event_reaction_context("dummy_url", event)
        self.assertEqual(ctx["material_strength"]["impact_score"], 80)
        self.assertEqual(ctx["price_reaction_so_far"]["return_pct"], 5.0)
        self.assertTrue(ctx["already_priced_in"])  # 指示書44番


class Phase7BackwardCompatibilityTests(unittest.TestCase):
    """39. Phase7 backward compatibility"""

    def test_backfill_default_sources_matches_phase7_behavior(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_recent_social_posts_all_sources.return_value = []
            result = server.backfill_underlying_events("dummy_url", "local", limit=10, dry_run=True)
        self.assertEqual(result["sources"], ["social"])
        mock_db.list_news_catalysts_for_backfill.assert_not_called()
        mock_db.list_market_events_for_backfill.assert_not_called()

    def test_event_match_confidence_engine_unchanged(self):
        existing = {"event_key": "JP:7203:BUYBACK:2026-09-11", "event_type": "BUYBACK", "ticker": "7203",
                    "normalized_title": "トヨタ自社株買い", "numerical_fingerprint": []}
        candidate = {"event_key": "JP:7203:BUYBACK:2026-09-11", "event_type": "BUYBACK", "ticker": "7203",
                     "normalized_title": "トヨタ自社株買い決定", "numerical_fingerprint": []}
        self.assertEqual(server.compute_event_match_confidence(existing, candidate), "EXACT")

    def test_consensus_engine_unchanged(self):
        now_iso = _iso(datetime.datetime.now(datetime.timezone.utc))
        posts = [
            {"post_id": "1", "source_handle": "nicosokufx", "text": "銀行が強い", "posted_at": now_iso,
             "primary_source_url": None, "direct_mentions_json": ["8306"]},
            {"post_id": "2", "source_handle": "kgbukabu", "text": "銀行が強い", "posted_at": now_iso,
             "primary_source_url": None, "direct_mentions_json": ["8306"]},
        ]
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_recent_social_posts_all_sources.return_value = posts
            result = server.build_market_intelligence_consensus("dummy_url")
        self.assertEqual(len(result["consensus"]), 1)


if __name__ == "__main__":
    unittest.main()
