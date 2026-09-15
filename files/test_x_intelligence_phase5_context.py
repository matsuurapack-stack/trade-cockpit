# X Intelligence Phase5（2026-09-15新規）：共通Intelligence Contextのテスト。
#
# build_external_intelligence_context()がsocial_market_posts/expert_views/
# market_events/build_market_intelligence_consensus()を横断して正しく構造化する
# こと、FACT/AUTHOR_VIEW/PREDICTION/MARKET_OBSERVATIONを混同しないこと、
# 鮮度（freshness）が種別ごとに適用されることを検証する。
#
# 実行方法： cd files & python -m unittest test_x_intelligence_phase5_context -v

import datetime
import unittest
from unittest import mock

import server


def _iso(hours_ago=0):
    return (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours_ago)).isoformat()


class IntelligenceRoleClassificationTests(unittest.TestCase):
    def test_news_media_is_fact_leaning(self):
        self.assertEqual(server._intelligence_role_for_handle("nikkei"), "FACT_LEANING")
        self.assertEqual(server._intelligence_role_for_handle("reutersjapan"), "FACT_LEANING")
        self.assertEqual(server._intelligence_role_for_handle("bloombergjapan"), "FACT_LEANING")

    def test_prediction_market_is_prediction(self):
        self.assertEqual(server._intelligence_role_for_handle("polymarketjapan"), "PREDICTION")

    def test_stock_breaking_accounts_are_stock_observation(self):
        self.assertEqual(server._intelligence_role_for_handle("kgbukabu"), "STOCK_OBSERVATION")
        self.assertEqual(server._intelligence_role_for_handle("aryarya"), "STOCK_OBSERVATION")

    def test_market_commentary_is_market_observation(self):
        self.assertEqual(server._intelligence_role_for_handle("nicosokufx"), "MARKET_OBSERVATION")

    def test_unknown_handle_defaults_to_market_observation_not_fact(self):
        # 監査の「unknown/manual_source」：事実として昇格させない安全側。
        self.assertEqual(server._intelligence_role_for_handle("random_unregistered_account"), "MARKET_OBSERVATION")


class FreshnessTests(unittest.TestCase):
    def test_fresh_market_observation_post_included(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_recent_social_posts_all_sources.return_value = [
                {"source_handle": "nicosokufx", "post_id": "1", "text": "日経が強い",
                 "posted_at": _iso(hours_ago=1), "facts_json": ["日経平均が上昇"],
                 "direct_mentions_json": []},
            ]
            mock_db.list_expert_views.return_value = []
            mock_db.list_market_events.return_value = []
            ctx = server.build_external_intelligence_context("dummy_url", "matsuura")
        self.assertEqual(len(ctx["facts"]), 1)
        self.assertIn("nicosokufx", ctx["sources_used"])

    def test_stale_market_observation_post_excluded(self):
        # 市場実況は3時間で失効（INTELLIGENCE_MARKET_OBSERVATION_STALE_HOURS）。
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_recent_social_posts_all_sources.return_value = [
                {"source_handle": "nicosokufx", "post_id": "1", "text": "古い実況",
                 "posted_at": _iso(hours_ago=10), "facts_json": ["古い事実"],
                 "direct_mentions_json": []},
            ]
            mock_db.list_expert_views.return_value = []
            mock_db.list_market_events.return_value = []
            ctx = server.build_external_intelligence_context("dummy_url", "matsuura")
        self.assertEqual(ctx["facts"], [])
        self.assertEqual(ctx["diagnostics"]["stale_skipped"], 1)

    def test_news_media_survives_longer_than_market_observation(self):
        # 同じ経過時間(10時間)でも、NEWS_MEDIA（nikkei）はまだ有効（20時間閾値）、
        # MARKET_COMMENTARY（nicosokufx）は失効済み（3時間閾値）。
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_recent_social_posts_all_sources.return_value = [
                {"source_handle": "nikkei", "post_id": "1", "text": "ニュース",
                 "posted_at": _iso(hours_ago=10), "facts_json": ["日銀が会合を開催"],
                 "direct_mentions_json": []},
                {"source_handle": "nicosokufx", "post_id": "2", "text": "実況",
                 "posted_at": _iso(hours_ago=10), "facts_json": ["古い実況事実"],
                 "direct_mentions_json": []},
            ]
            mock_db.list_expert_views.return_value = []
            mock_db.list_market_events.return_value = []
            ctx = server.build_external_intelligence_context("dummy_url", "matsuura")
        fact_sources = {f["source_handle"] for f in ctx["facts"]}
        self.assertIn("nikkei", fact_sources)
        self.assertNotIn("nicosokufx", fact_sources)

    def test_expert_view_active_within_risk_window(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_recent_social_posts_all_sources.return_value = []
            mock_db.list_expert_views.return_value = [{
                "expert_name": "木野内栄治", "published_at": "2026-09-04",
                "topic": None, "thesis": "半導体調整", "outlook": "10月上旬に底入れ",
                "key_points": ["AI・半導体は調整局面"], "confirmations": [], "invalidation_conditions": [],
                "confidence": "MEDIUM", "risk_window_end": "2099-01-01",
            }]
            mock_db.list_market_events.return_value = []
            ctx = server.build_external_intelligence_context("dummy_url", "matsuura")
        self.assertEqual(len(ctx["expert_views"]), 1)
        self.assertIn("半導体", ctx["expert_views"][0]["inferred_sectors"])

    def test_expert_view_expired_excluded(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_recent_social_posts_all_sources.return_value = []
            mock_db.list_expert_views.return_value = [{
                "expert_name": "期限切れ氏", "published_at": "2020-01-01",
                "topic": None, "thesis": "", "outlook": "", "key_points": [],
                "risk_window_end": "2020-02-01",
            }]
            mock_db.list_market_events.return_value = []
            ctx = server.build_external_intelligence_context("dummy_url", "matsuura")
        self.assertEqual(ctx["expert_views"], [])
        self.assertEqual(ctx["diagnostics"]["stale_skipped"], 1)


class FactVsOpinionSeparationTests(unittest.TestCase):
    def test_only_facts_json_enters_facts_list(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_recent_social_posts_all_sources.return_value = [
                {"source_handle": "aryarya", "post_id": "1", "text": "銀行株は弱い、下落基調",
                 "posted_at": _iso(hours_ago=1), "facts_json": ["需給が悪化"],
                 "author_opinion_json": ["下落継続すると予想する"], "direct_mentions_json": []},
            ]
            mock_db.list_expert_views.return_value = []
            mock_db.list_market_events.return_value = []
            ctx = server.build_external_intelligence_context("dummy_url", "matsuura")
        fact_texts = [f["text"] for f in ctx["facts"]]
        self.assertIn("需給が悪化", fact_texts)
        self.assertNotIn("下落継続すると予想する", fact_texts)

    def test_prediction_market_probability_not_treated_as_fact(self):
        # Polymarketの確率はFACTではない：facts_jsonに入っていない限りfactsへは入らない。
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_recent_social_posts_all_sources.return_value = [
                {"source_handle": "polymarketjapan", "post_id": "1", "text": "利上げ確率70%",
                 "posted_at": _iso(hours_ago=1), "facts_json": [], "direct_mentions_json": []},
            ]
            mock_db.list_expert_views.return_value = []
            mock_db.list_market_events.return_value = []
            ctx = server.build_external_intelligence_context("dummy_url", "matsuura")
        self.assertEqual(ctx["facts"], [])


class StockAndSectorSignalTests(unittest.TestCase):
    def test_stock_signal_aggregated_from_direct_mentions(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_recent_social_posts_all_sources.return_value = [
                {"source_handle": "kgbukabu", "post_id": "1", "text": "急騰、強い動き",
                 "posted_at": _iso(hours_ago=1), "facts_json": [], "direct_mentions_json": ["7203"]},
            ]
            mock_db.list_expert_views.return_value = []
            mock_db.list_market_events.return_value = []
            ctx = server.build_external_intelligence_context("dummy_url", "matsuura")
        signals = {s["code"]: s for s in ctx["stock_signals"]}
        self.assertIn("7203", signals)
        self.assertEqual(signals["7203"]["direction"], "BULLISH")

    def test_sector_signals_come_from_consensus_sector_entries(self):
        with mock.patch.object(server, "investment_db") as mock_db, \
             mock.patch.object(server, "build_market_intelligence_consensus") as mock_consensus:
            mock_db.list_recent_social_posts_all_sources.return_value = []
            mock_db.list_expert_views.return_value = []
            mock_db.list_market_events.return_value = []
            mock_consensus.return_value = {
                "consensus": [{"topic": "SEMICONDUCTOR", "target_type": "SECTOR", "direction": "BULLISH",
                                "sources": ["kgbukabu", "reutersjapan"], "independent_source_count": 2,
                                "consensus_strength": "MODERATE", "confidence": 0.6, "related_stocks": []}],
                "disagreements": [],
            }
            ctx = server.build_external_intelligence_context("dummy_url", "matsuura")
        self.assertEqual(len(ctx["sector_signals"]), 1)
        self.assertEqual(ctx["sector_signals"][0]["topic"], "SEMICONDUCTOR")


class DisagreementWarningTests(unittest.TestCase):
    def test_disagreement_becomes_explicit_warning_not_averaged(self):
        with mock.patch.object(server, "investment_db") as mock_db, \
             mock.patch.object(server, "build_market_intelligence_consensus") as mock_consensus:
            mock_db.list_recent_social_posts_all_sources.return_value = []
            mock_db.list_expert_views.return_value = []
            mock_db.list_market_events.return_value = []
            mock_consensus.return_value = {
                "consensus": [],
                "disagreements": [{"topic": "BANK", "target_type": "SECTOR",
                                    "sources": ["nicosokufx", "aryarya"],
                                    "directions": {"nicosokufx": "BULLISH", "aryarya": "BEARISH"},
                                    "disagreement": True, "consensus_strength": "NONE"}],
            }
            ctx = server.build_external_intelligence_context("dummy_url", "matsuura")
        self.assertEqual(len(ctx["disagreements"]), 1)
        self.assertTrue(any("BANK" in w and "見解不一致" in w for w in ctx["warnings"]))


class EventSignalTests(unittest.TestCase):
    def test_high_medium_importance_events_included(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_recent_social_posts_all_sources.return_value = []
            mock_db.list_expert_views.return_value = []
            mock_db.list_market_events.return_value = [
                {"title": "FOMC", "event_date": "2026-09-17", "event_type": "CENTRAL_BANK",
                 "importance": "HIGH", "source_handle": "nicosokufx", "source_type": "X_POST"},
                {"title": "軽微なイベント", "event_date": "2026-09-18", "event_type": "OTHER",
                 "importance": "LOW", "source_handle": None, "source_type": None},
            ]
            ctx = server.build_external_intelligence_context("dummy_url", "matsuura")
        titles = [e["title"] for e in ctx["event_signals"]]
        self.assertIn("FOMC", titles)
        self.assertNotIn("軽微なイベント", titles)


class DiagnosticsTests(unittest.TestCase):
    def test_diagnostics_reports_errors_not_silently(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_recent_social_posts_all_sources.side_effect = Exception("db down")
            mock_db.list_expert_views.return_value = []
            mock_db.list_market_events.return_value = []
            ctx = server.build_external_intelligence_context("dummy_url", "matsuura")
        self.assertTrue(any("db down" in e for e in ctx["diagnostics"]["errors"]))

    def test_no_database_url_returns_empty_context_not_none(self):
        ctx = server.build_external_intelligence_context(None, "matsuura")
        self.assertEqual(ctx["facts"], [])
        self.assertIn("as_of", ctx)


class SafeWrapperTests(unittest.TestCase):
    def test_safe_wrapper_returns_none_on_exception(self):
        with mock.patch.object(server, "build_external_intelligence_context", side_effect=Exception("boom")):
            result = server.build_external_intelligence_context_safe("dummy_url", "matsuura")
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
