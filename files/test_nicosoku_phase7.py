# Market Intelligence Phase7 テスト（指示書32番）。
#
# Phase2〜6同様、実DBを必要としない形で26項目をカバーする。
#
# 実行方法： cd files && python -m unittest test_nicosoku_phase7 -v

import datetime
import unittest
from unittest import mock

import server


def _iso(dt):
    return dt.astimezone(datetime.timezone.utc).isoformat()


class EventKeyMatchTests(unittest.TestCase):
    """1〜2. exact event_key match / same ticker different event（指示書6・9・31番）"""

    def test_exact_event_key_match(self):
        existing = {"event_key": "JP:7203:BUYBACK:2026-09-11", "event_type": "BUYBACK", "ticker": "7203",
                    "normalized_title": "トヨタ自社株買い", "numerical_fingerprint": []}
        candidate = {"event_key": "JP:7203:BUYBACK:2026-09-11", "event_type": "BUYBACK", "ticker": "7203",
                     "normalized_title": "トヨタ自社株買い決定", "numerical_fingerprint": []}
        self.assertEqual(server.compute_event_match_confidence(existing, candidate), "EXACT")

    def test_same_ticker_different_event_type_is_new_event(self):
        # 指示書31番：同日同社でも自社株買い/決算/配当は別event。tickerだけで統合しない。
        existing = {"event_key": None, "event_type": "BUYBACK", "ticker": "7203",
                    "normalized_title": "トヨタ自社株買い", "numerical_fingerprint": []}
        candidate = {"event_key": None, "event_type": "EARNINGS", "ticker": "7203",
                     "normalized_title": "トヨタ決算発表", "numerical_fingerprint": []}
        self.assertEqual(server.compute_event_match_confidence(existing, candidate), "NEW_EVENT")


class MergeScenarioTests(unittest.TestCase):
    """3〜4. same event social+TDnet / same Reuters relay duplicated（指示書3・5番）"""

    def test_social_and_tdnet_merge_via_find_matching(self):
        now_iso = _iso(datetime.datetime.now(datetime.timezone.utc))
        existing_event = {"id": 1, "event_key": None, "event_type": "BUYBACK", "ticker": "7203",
                           "normalized_title": "トヨタ自社株買い100億円", "numerical_fingerprint_json": ["AMOUNT:100億円"]}
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_underlying_event_candidates.return_value = [existing_event]
            candidate = server.build_event_candidate_descriptor("BUYBACK", "トヨタが自社株買い100億円を発表",
                                                                  ticker="7203", event_at=now_iso)
            matched, level = server.find_matching_underlying_event("dummy_url", candidate)
        self.assertIsNotNone(matched)
        self.assertIn(level, ("HIGH", "MEDIUM", "EXACT"))

    def test_duplicate_reuters_relay_not_double_counted(self):
        evidences = [
            {"source_name": "kgbukabu", "source_url": "https://reuters.example.com/a", "dependency_group": None, "upstream_source": None},
            {"source_name": "aryarya", "source_url": "https://reuters.example.com/a", "dependency_group": None, "upstream_source": None},
        ]
        self.assertEqual(server.compute_independent_source_count(evidences), 1)


class IndependentSourceCountTests(unittest.TestCase):
    """5. independent source count（指示書10・11番）"""

    def test_distinct_urls_count_as_distinct_independent_sources(self):
        evidences = [
            {"source_name": "aryarya", "source_url": "https://a.example.com/1"},
            {"source_name": "kgbukabu", "source_url": "https://b.example.com/2"},
        ]
        self.assertEqual(server.compute_independent_source_count(evidences), 2)

    def test_dependency_group_dedupes_even_with_different_urls(self):
        evidences = [
            {"source_name": "aryarya", "source_url": "https://a.example.com/1", "dependency_group": "tdnet-123"},
            {"source_name": "kgbukabu", "source_url": "https://b.example.com/2", "dependency_group": "tdnet-123"},
        ]
        self.assertEqual(server.compute_independent_source_count(evidences), 1)


class PrimarySourcePromotionTests(unittest.TestCase):
    """6. primary source promotion（指示書4番）"""

    def test_social_promoted_to_tdnet(self):
        event = {"primary_source_type": "SOCIAL"}
        promotion = server.maybe_promote_primary_source(event, "TDNET", "https://release.tdnet.info/x")
        self.assertIsNotNone(promotion)
        self.assertEqual(promotion["primary_source_type"], "TDNET")

    def test_tdnet_not_demoted_by_social(self):
        event = {"primary_source_type": "TDNET"}
        promotion = server.maybe_promote_primary_source(event, "SOCIAL", "https://x.com/1")
        self.assertIsNone(promotion)


class EventConfidenceLevelTests(unittest.TestCase):
    """7〜9. SOCIAL_ONLY / OFFICIAL_CONFIRMED / MULTI_SOURCE_CONFIRMED（指示書12番）"""

    def test_social_only(self):
        self.assertEqual(server.classify_event_confidence_level("SOCIAL", 1), "SOCIAL_ONLY")

    def test_official_confirmed(self):
        self.assertEqual(server.classify_event_confidence_level("TDNET", 1), "OFFICIAL_CONFIRMED")

    def test_multi_source_confirmed(self):
        self.assertEqual(server.classify_event_confidence_level("SOCIAL", 2), "MULTI_SOURCE_CONFIRMED")

    def test_single_reliable_source(self):
        self.assertEqual(server.classify_event_confidence_level("NEWS", 1), "SINGLE_RELIABLE_SOURCE")


class HeadlineNormalizationTests(unittest.TestCase):
    """10〜11. normalized headline match / numerical fingerprint（指示書7・8番）"""

    def test_normalization_strips_noise_but_keeps_numbers(self):
        normalized = server.normalize_headline("🚀ABC株式会社、自社株買い100億円を実施 09:31 https://example.com/x")
        self.assertNotIn("株式会社", normalized)
        self.assertNotIn("http", normalized)
        self.assertIn("100億円", normalized)

    def test_numerical_fingerprint_extraction(self):
        fp = server.extract_numerical_fingerprint("自社株買い100億円、上限2.5%")
        self.assertIn("AMOUNT:100億円", fp)
        self.assertIn("PCT:2.5", fp)

    def test_fingerprint_overlap_boosts_confidence(self):
        existing = {"event_key": None, "event_type": "BUYBACK", "ticker": "7203",
                    "normalized_title": "自社株買い100億円決定", "numerical_fingerprint": ["AMOUNT:100億円"]}
        candidate = {"event_key": None, "event_type": "BUYBACK", "ticker": "7203",
                     "normalized_title": "自社株買い100億円を発表", "numerical_fingerprint": ["AMOUNT:100億円"]}
        self.assertEqual(server.compute_event_match_confidence(existing, candidate), "HIGH")


class LowConfidenceNoMergeTests(unittest.TestCase):
    """12. low confidence no merge（指示書9番：誤統合より重複の方が安全）"""

    def test_low_confidence_returns_no_match(self):
        existing_event = {"id": 1, "event_key": None, "event_type": "BUYBACK", "ticker": "7203",
                           "normalized_title": "全く違う話", "numerical_fingerprint_json": []}
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_underlying_event_candidates.return_value = [existing_event]
            candidate = server.build_event_candidate_descriptor("BUYBACK", "別の内容の投稿です", ticker="7203")
            matched, level = server.find_matching_underlying_event("dummy_url", candidate)
        self.assertIsNone(matched)


class MaterialUpdateAlertTests(unittest.TestCase):
    """13〜16. event revision / material update / duplicate alert suppression /
    confidence upgrade alert（指示書14・20・21番）"""

    def test_new_event_alert(self):
        self.assertEqual(server.classify_underlying_event_alert_type(True), "NEW_EVENT")

    def test_no_alert_for_pure_duplicate(self):
        before = {"confidence_level": "SOCIAL_ONLY", "impact_score": 20, "numerical_fingerprint": ["AMOUNT:100億円"]}
        after = {"confidence_level": "SOCIAL_ONLY", "impact_score": 20, "numerical_fingerprint": ["AMOUNT:100億円"]}
        self.assertIsNone(server.classify_underlying_event_alert_type(False, before, after))

    def test_material_update_on_new_numerical_fact(self):
        before = {"confidence_level": "SOCIAL_ONLY", "impact_score": 20, "numerical_fingerprint": ["AMOUNT:100億円"]}
        after = {"confidence_level": "SOCIAL_ONLY", "impact_score": 20, "numerical_fingerprint": ["AMOUNT:100億円", "PCT:2.5"]}
        self.assertEqual(server.classify_underlying_event_alert_type(False, before, after), "MATERIAL_UPDATE")

    def test_confidence_upgrade_alert(self):
        before = {"confidence_level": "SOCIAL_ONLY", "impact_score": 20, "numerical_fingerprint": []}
        after = {"confidence_level": "OFFICIAL_CONFIRMED", "impact_score": 20, "numerical_fingerprint": []}
        self.assertEqual(server.classify_underlying_event_alert_type(False, before, after), "CONFIDENCE_UPGRADE")

    def test_duplicate_evidence_does_not_create_alert_via_ingest(self):
        event = {"id": 1, "event_type": "BUYBACK", "ticker": "7203", "confidence_level": "SOCIAL_ONLY",
                 "impact_score": 20, "numerical_fingerprint_json": [], "primary_source_type": "SOCIAL",
                 "status": "ACTIVE", "title": "t", "normalized_title": "自社株買い", "event_key": None}
        with mock.patch.object(server, "investment_db") as mock_db:
            # 1回目：既存イベント無し→新規作成。2回目：既に作成済みのeventが見つかる状態を再現。
            mock_db.list_underlying_event_candidates.side_effect = [[], [event]]
            mock_db.create_underlying_event.return_value = event
            mock_db.add_underlying_event_evidence.side_effect = [{"id": 1}, None]  # 2回目は重複でNone
            mock_db.list_underlying_event_evidence.return_value = [{"source_name": "aryarya"}]
            candidate = server.build_event_candidate_descriptor("BUYBACK", "自社株買い", ticker="7203")
            evidence = {"source_kind": "SOCIAL", "source_name": "aryarya", "source_record_id": "1",
                        "source_url": "https://x.com/1", "posted_at": _iso(datetime.datetime.now(datetime.timezone.utc))}
            server.ingest_market_intelligence_item("dummy_url", "local", candidate, evidence)
            result2 = server.ingest_market_intelligence_item("dummy_url", "local", candidate, evidence)
        self.assertIsNone(result2["alert_type"])
        mock_db.create_underlying_event_alert.assert_called_once()  # NEW_EVENTの1回だけ


class PredictionMacroLinkageTests(unittest.TestCase):
    """17〜18. prediction market linkage / macro event linkage（指示書15・16番）"""

    def test_prediction_links_to_existing_macro_event_only(self):
        existing_event = {"id": 5, "event_key": None, "event_type": "CENTRAL_BANK", "ticker": None,
                           "normalized_title": "FOMC 9月会合", "numerical_fingerprint_json": []}
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_underlying_event_candidates.return_value = [existing_event]
            mock_db.add_underlying_event_evidence.return_value = {"id": 10}
            saved_post = {"post_id": "1", "source_handle": "polymarketjapan", "url": "https://x.com/1",
                          "posted_at": _iso(datetime.datetime.now(datetime.timezone.utc)),
                          "prediction_market_json": {"category": "MACRO", "prediction_topic": "FOMC 9月会合の利下げ確率"}}
            event_id = server.maybe_link_prediction_to_macro_event("dummy_url", "local", saved_post)
        self.assertEqual(event_id, 5)
        mock_db.add_underlying_event_evidence.assert_called_once()
        call_kwargs = mock_db.add_underlying_event_evidence.call_args[0][1]
        self.assertEqual(call_kwargs["source_kind"], "PREDICTION")

    def test_prediction_does_not_create_new_event_when_no_match(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_underlying_event_candidates.return_value = []
            saved_post = {"post_id": "1", "source_handle": "polymarketjapan", "url": "https://x.com/1",
                          "posted_at": _iso(datetime.datetime.now(datetime.timezone.utc)),
                          "prediction_market_json": {"category": "MACRO", "prediction_topic": "全く新しい話題"}}
            event_id = server.maybe_link_prediction_to_macro_event("dummy_url", "local", saved_post)
        self.assertIsNone(event_id)
        mock_db.create_underlying_event.assert_not_called()


class RelevanceTests(unittest.TestCase):
    """19〜20. ticker relevance / sector relevance（指示書17番）"""

    def test_direct_ticker_captured_in_candidate(self):
        candidate = server.build_event_candidate_descriptor("BUYBACK", "自社株買い", ticker="7203")
        self.assertEqual(candidate["ticker"], "7203")

    def test_sector_captured_when_provided(self):
        candidate = server.build_event_candidate_descriptor("BUYBACK", "自社株買い", ticker="7203", sector="自動車")
        self.assertEqual(candidate["sector"], "自動車")


class ImpactScoreTests(unittest.TestCase):
    """21. impact score（指示書18番）"""

    def test_impact_score_reflects_position_and_confidence(self):
        score_position = server.compute_event_impact_score(direct_position_hit=True, confidence_level="OFFICIAL_CONFIRMED",
                                                              event_type="BUYBACK", freshness_minutes=10)
        score_baseline = server.compute_event_impact_score(confidence_level="SOCIAL_ONLY", event_type="OTHER")
        self.assertGreater(score_position, score_baseline)
        self.assertLessEqual(score_position, 100)


class EventTimelineDiscoverySpeedTests(unittest.TestCase):
    """22〜23. event timeline / discovery speed（指示書25・26番）"""

    def test_timeline_order_is_chronological(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_underlying_event_evidence.return_value = [
                {"posted_at": "2026-09-11T00:31:00Z", "source_name": "aryarya", "source_kind": "SOCIAL"},
                {"posted_at": "2026-09-11T00:35:00Z", "source_name": "TDnet", "source_kind": "TDNET"},
            ]
            evidence = server.investment_db.list_underlying_event_evidence("dummy_url", 1)
        self.assertEqual(evidence[0]["source_name"], "aryarya")

    def test_discovery_lead_seconds(self):
        evidence = [
            {"source_kind": "SOCIAL", "posted_at": "2026-09-11T00:31:00Z"},
            {"source_kind": "TDNET", "posted_at": "2026-09-11T00:35:00Z"},
        ]
        lead = server.compute_discovery_lead_seconds(evidence)
        self.assertEqual(lead, 240)

    def test_discovery_lead_none_when_only_social(self):
        evidence = [{"source_kind": "SOCIAL", "posted_at": "2026-09-11T00:31:00Z"}]
        self.assertIsNone(server.compute_discovery_lead_seconds(evidence))


class RecentMarketIntelligenceEventFormatTests(unittest.TestCase):
    """24. recent_market_intelligence event format（指示書22番）"""

    def test_events_included_in_recent_market_intelligence(self):
        now_iso = _iso(datetime.datetime.now(datetime.timezone.utc))
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_recent_social_posts_all_sources.return_value = []
            mock_db.list_social_signals.return_value = []
            mock_db.list_watchlist.return_value = []
            mock_db.list_portfolio.return_value = []
            mock_db.get_market_source.return_value = None
            mock_db.list_active_underlying_events.return_value = [
                {"id": 123, "event_type": "BUYBACK", "title": "トヨタ自社株買い", "confidence_level": "OFFICIAL_CONFIRMED",
                 "status": "CONFIRMED", "first_seen_at": now_iso, "last_seen_at": now_iso,
                 "independent_source_count": 2, "raw_source_count": 3, "direct_tickers_json": ["7203"],
                 "related_tickers_json": [], "impact_score": 84, "primary_source_type": "TDNET",
                 "primary_source_url": "https://release.tdnet.info/x"},
            ]
            mock_db.list_underlying_event_evidence.return_value = [
                {"source_name": "aryarya", "source_kind": "SOCIAL", "posted_at": now_iso},
                {"source_name": "TDnet", "source_kind": "TDNET", "posted_at": now_iso},
            ]
            intel = server.get_recent_market_intelligence("dummy_url", "local")
        self.assertEqual(len(intel["events"]), 1)
        ev = intel["events"][0]
        self.assertEqual(ev["event_id"], 123)
        self.assertEqual(ev["confidence"], "OFFICIAL_CONFIRMED")
        self.assertEqual(ev["direct_tickers"], ["7203"])
        self.assertEqual(ev["impact_score"], 84)
        self.assertIn("aryarya", ev["sources"])
        self.assertIn("TDnet", ev["sources"])


class Phase6BackwardCompatibilityTests(unittest.TestCase):
    """26. Phase6 backward compatibility"""

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

    def test_intelligence_cluster_id_field_still_supported(self):
        # Phase6のintelligence_cluster_idは廃止せず、underlying_eventのdescriptorとは
        # 独立して動く（指示書27番）。
        post_a = {"post_id": "1", "source_handle": "nicosokufx", "direct_mentions_json": ["8035"],
                  "categories_json": ["SECTOR_ROTATION"], "text": "半導体弱い"}
        post_b = {"post_id": "2", "source_handle": "kgbukabu", "direct_mentions_json": ["8035"],
                  "categories_json": ["SECTOR_ROTATION"], "text": "半導体急落"}
        match = server.find_intelligence_cluster_match([post_a], post_b)
        self.assertIsNotNone(match)

    def test_nicosoku_poll_once_still_works(self):
        with mock.patch.object(server, "poll_market_source") as mock_poll:
            mock_poll.return_value = {"status": "ok", "newPosts": 0, "fetched": 0, "duplicates": 0,
                                       "eventsDetected": 0, "error": None}
            result = server.nicosoku_poll_once("dummy_url", "local")
        self.assertEqual(result["status"], "ok")


if __name__ == "__main__":
    unittest.main()
