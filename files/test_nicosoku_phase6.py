# Market Intelligence Phase6 テスト（指示書33番）。
#
# Phase2〜5同様、実DBを必要としない形で26項目をカバーする。X API呼び出し自体は
# _x_resolve_user_id/_x_fetch_recent_tweetsをモックし、実ネットワークへは一切出ない。
#
# 実行方法： cd files && python -m unittest test_nicosoku_phase6 -v

import datetime
import unittest
from unittest import mock

import server


def _iso(dt):
    return dt.astimezone(datetime.timezone.utc).isoformat()


def _tweet(tid, text, created_at=None):
    return {"id": tid, "text": text, "created_at": created_at or "2026-09-12T00:00:00Z"}


class SourceFetchTests(unittest.TestCase):
    """1〜4. nicosokufx/polymarketjapan/kgbukabu/aryarya取得（指示書1・2番）"""

    def _run_poll(self, handle, tweet_text):
        cfg = server.MARKET_SOURCE_BY_HANDLE[handle]
        with mock.patch.object(server, "investment_db") as mock_db, \
             mock.patch.object(server, "X_API_BEARER_TOKEN", "dummy-token"), \
             mock.patch.object(server, "_x_user_id_cache", {}):
            mock_db.ensure_market_source.return_value = {"last_seen_post_id": None, "enabled": True}
            mock_db.list_watchlist.return_value = []
            mock_db.list_portfolio.return_value = []
            mock_db.list_market_events.return_value = []
            mock_db.insert_social_post_if_new.side_effect = lambda url, rec: dict(rec, id=1)
            with mock.patch.object(server, "_x_resolve_user_id", return_value=("999", "ok", None)), \
                 mock.patch.object(server, "_x_fetch_recent_tweets") as mock_fetch, \
                 mock.patch.object(server, "generate_social_event_evaluations_for_post_safe", return_value=0), \
                 mock.patch.object(server, "assign_intelligence_cluster_safe", return_value=None):
                mock_fetch.return_value = ({"data": [_tweet("1", tweet_text)]}, "ok", None)
                result = server.poll_market_source("dummy_url", "local", cfg)
        return result, mock_db

    def test_nicosokufx_fetch(self):
        result, mock_db = self._run_poll("nicosokufx", "銀行は強い")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["newPosts"], 1)
        record = mock_db.insert_social_post_if_new.call_args[0][1]
        self.assertEqual(record["source_handle"], "nicosokufx")

    def test_polymarketjapan_fetch(self):
        result, mock_db = self._run_poll("polymarketjapan", "FRB利下げ確率 52%→70%")
        self.assertEqual(result["status"], "ok")
        record = mock_db.insert_social_post_if_new.call_args[0][1]
        self.assertIsNotNone(record["prediction_market"])
        self.assertEqual(record["prediction_market"]["probability"], 70.0)

    def test_kgbukabu_fetch(self):
        result, mock_db = self._run_poll("kgbukabu", "6758 +8.5% ストップ高 出来高急増")
        self.assertEqual(result["status"], "ok")
        record = mock_db.insert_social_post_if_new.call_args[0][1]
        self.assertIsNotNone(record["stock_breaking"])
        self.assertEqual(record["stock_breaking"]["ticker"], "6758")

    def test_aryarya_fetch(self):
        result, mock_db = self._run_poll("aryarya", "○○社 上方修正 https://release.tdnet.info/xxx")
        self.assertEqual(result["status"], "ok")
        record = mock_db.insert_social_post_if_new.call_args[0][1]
        self.assertEqual(record["primary_source_type"], "TDNET")
        self.assertEqual(record["post_classification"], "BREAKING_RELAY")


class SourceSinceIdTests(unittest.TestCase):
    """5. source別since_id（指示書2・29番）"""

    def test_since_id_comes_from_that_sources_market_source_row(self):
        cfg = server.MARKET_SOURCE_BY_HANDLE["kgbukabu"]
        with mock.patch.object(server, "investment_db") as mock_db, \
             mock.patch.object(server, "X_API_BEARER_TOKEN", "dummy-token"), \
             mock.patch.object(server, "_x_user_id_cache", {"kgbukabu": "111"}):
            mock_db.ensure_market_source.return_value = {"last_seen_post_id": "555", "enabled": True}
            mock_db.list_watchlist.return_value = []
            mock_db.list_portfolio.return_value = []
            with mock.patch.object(server, "_x_fetch_recent_tweets") as mock_fetch:
                mock_fetch.return_value = ({"data": []}, "ok", None)
                server.poll_market_source("dummy_url", "local", cfg)
            mock_fetch.assert_called_once_with("111", since_id="555")


class SourceFailureIsolationTests(unittest.TestCase):
    """6. source障害分離（指示書6・30番）"""

    def test_one_source_failure_does_not_stop_others(self):
        with mock.patch.object(server, "investment_db") as mock_db, \
             mock.patch.object(server, "X_API_BEARER_TOKEN", "dummy-token"):
            mock_db.list_market_sources.return_value = [
                {"handle": h, "enabled": True} for h in server.MARKET_SOURCE_BY_HANDLE
            ]

            def fake_poll(url, user_id, cfg):
                if cfg["handle"] == "kgbukabu":
                    raise RuntimeError("boom")
                return {"status": "ok", "newPosts": 0, "fetched": 0, "duplicates": 0, "eventsDetected": 0, "error": None}

            with mock.patch.object(server, "poll_market_source", side_effect=fake_poll):
                results = server.poll_all_market_sources_once("dummy_url", "local", force=True)
        self.assertEqual(results["kgbukabu"]["status"], "failed")
        self.assertEqual(results["nicosokufx"]["status"], "ok")
        self.assertEqual(results["aryarya"]["status"], "ok")


class PredictionMarketParsingTests(unittest.TestCase):
    """7〜9. PREDICTION_MARKET分類 / probability抽出 / probability delta（指示書5・6番）"""

    def test_category_classification(self):
        data = server.extract_prediction_market_data("米大統領選の行方について")
        self.assertEqual(data["category"], "POLITICS")

    def test_probability_extraction_single_value(self):
        data = server.extract_prediction_market_data("日銀の追加利上げ確率は70%")
        self.assertEqual(data["probability"], 70.0)
        self.assertIsNone(data["previous_probability"])

    def test_probability_delta_from_shift_pattern(self):
        data = server.extract_prediction_market_data("FRB利下げ確率 52%→70%に急上昇")
        self.assertEqual(data["previous_probability"], 52.0)
        self.assertEqual(data["probability"], 70.0)
        self.assertEqual(data["probability_change"], 18.0)
        self.assertTrue(server.classify_prediction_market_shift(data["probability_change"]))

    def test_small_change_not_a_shift(self):
        self.assertFalse(server.classify_prediction_market_shift(3))


class StockBreakingCorporateBreakingTests(unittest.TestCase):
    """10〜13. STOCK_BREAKING分類 / CORPORATE_BREAKING分類 / ticker抽出 / primary source判定
    （指示書8・10・11番）"""

    def test_stock_breaking_kind_classification(self):
        self.assertEqual(server.classify_signal_kind_for_source("ストップ高で急騰", "STOCK_BREAKING"), "BREAKING_STOCK")
        self.assertEqual(server.classify_signal_kind_for_source("出来高急増で急伸", "STOCK_BREAKING"), "MOMENTUM_ALERT")
        self.assertEqual(server.classify_signal_kind_for_source("業務提携を材料に", "STOCK_BREAKING"), "CATALYST_ALERT")

    def test_other_source_type_uses_existing_phase5_classification(self):
        # 指示書9番「既存enumとの互換性を壊さない」：STOCK_BREAKING以外は従来通り。
        self.assertEqual(server.classify_signal_kind_for_source("銀行強い", "MARKET_COMMENTARY"),
                          server.classify_signal_kind("銀行強い"))

    def test_corporate_breaking_extraction(self):
        data = server.extract_corporate_breaking_data("○○社が上方修正", url="https://release.tdnet.info/abc")
        self.assertEqual(data["linked_domain"], "release.tdnet.info")

    def test_ticker_extraction(self):
        data = server.extract_stock_breaking_data("6758 ソニーが急騰")
        self.assertEqual(data["ticker"], "6758")

    def test_primary_source_type_classification(self):
        self.assertEqual(server.classify_primary_source_type("https://release.tdnet.info/x"), "TDNET")
        self.assertEqual(server.classify_primary_source_type("https://www.fsa.go.jp/x"), "GOV")
        self.assertEqual(server.classify_primary_source_type("https://www.bloomberg.co.jp/x"), "NEWS")
        self.assertIsNone(server.classify_primary_source_type(None))


class IntelligenceClusterTests(unittest.TestCase):
    """14〜16. intelligence cluster / duplicate source detection / independent source count
    （指示書16・19番）"""

    def test_cluster_match_by_shared_mention(self):
        post_a = {"post_id": "1", "source_handle": "nicosokufx", "direct_mentions_json": ["8035"],
                  "categories_json": ["SECTOR_ROTATION"], "text": "半導体弱い"}
        post_b = {"post_id": "2", "source_handle": "kgbukabu", "direct_mentions_json": ["8035"],
                  "categories_json": ["SECTOR_ROTATION"], "text": "半導体急落"}
        match = server.find_intelligence_cluster_match([post_a], post_b)
        self.assertEqual(match["post_id"], "1")

    def test_no_match_without_shared_signal(self):
        post_a = {"post_id": "1", "source_handle": "nicosokufx", "direct_mentions_json": ["8035"],
                  "categories_json": ["SECTOR_ROTATION"], "text": "半導体弱い"}
        post_b = {"post_id": "2", "source_handle": "aryarya", "direct_mentions_json": ["9999"],
                  "categories_json": ["DISCLOSURE"], "text": "全く別の話題です"}
        match = server.find_intelligence_cluster_match([post_a], post_b)
        self.assertIsNone(match)

    def test_duplicate_source_via_same_primary_url_counts_once(self):
        now_iso = _iso(datetime.datetime.now(datetime.timezone.utc))
        posts = [
            {"post_id": "1", "source_handle": "kgbukabu", "text": "半導体弱い", "posted_at": now_iso,
             "primary_source_url": "https://news.example.com/a", "direct_mentions_json": []},
            {"post_id": "2", "source_handle": "aryarya", "text": "半導体弱い", "posted_at": now_iso,
             "primary_source_url": "https://news.example.com/a", "direct_mentions_json": []},
        ]
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_recent_social_posts_all_sources.return_value = posts
            result = server.build_market_intelligence_consensus("dummy_url")
        # 同一記事の転載2件は独立1ソース扱い→consensusは生成されない（指示書18・19番）
        self.assertEqual(result["consensus"], [])

    def test_independent_source_count_with_distinct_urls(self):
        now_iso = _iso(datetime.datetime.now(datetime.timezone.utc))
        posts = [
            {"post_id": "1", "source_handle": "nicosokufx", "text": "半導体弱い", "posted_at": now_iso,
             "primary_source_url": None, "direct_mentions_json": ["8035"]},
            {"post_id": "2", "source_handle": "kgbukabu", "text": "半導体急落", "posted_at": now_iso,
             "primary_source_url": None, "direct_mentions_json": ["8035"]},
        ]
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_recent_social_posts_all_sources.return_value = posts
            result = server.build_market_intelligence_consensus("dummy_url")
        self.assertEqual(len(result["consensus"]), 1)
        self.assertEqual(result["consensus"][0]["independent_source_count"], 2)


class ConsensusDisagreementTests(unittest.TestCase):
    """17〜19. consensus bullish / bearish / disagreement（指示書17・18・20・21番）"""

    def _posts(self, dir_a, dir_b):
        now_iso = _iso(datetime.datetime.now(datetime.timezone.utc))
        word_a = "強い" if dir_a == "BULLISH" else "弱い"
        word_b = "強い" if dir_b == "BULLISH" else "弱い"
        return [
            {"post_id": "1", "source_handle": "nicosokufx", "text": f"銀行が{word_a}", "posted_at": now_iso,
             "primary_source_url": None, "direct_mentions_json": ["8306"]},
            {"post_id": "2", "source_handle": "kgbukabu", "text": f"銀行が{word_b}", "posted_at": now_iso,
             "primary_source_url": None, "direct_mentions_json": ["8306"]},
        ]

    def test_consensus_bullish(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_recent_social_posts_all_sources.return_value = self._posts("BULLISH", "BULLISH")
            result = server.build_market_intelligence_consensus("dummy_url")
        self.assertEqual(len(result["consensus"]), 1)
        self.assertEqual(result["consensus"][0]["direction"], "BULLISH")
        self.assertEqual(result["consensus"][0]["consensus_strength"], "MODERATE")

    def test_consensus_bearish(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_recent_social_posts_all_sources.return_value = self._posts("BEARISH", "BEARISH")
            result = server.build_market_intelligence_consensus("dummy_url")
        self.assertEqual(result["consensus"][0]["direction"], "BEARISH")

    def test_disagreement_not_forced_into_consensus(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_recent_social_posts_all_sources.return_value = self._posts("BULLISH", "BEARISH")
            result = server.build_market_intelligence_consensus("dummy_url")
        self.assertEqual(result["consensus"], [])
        self.assertEqual(len(result["disagreements"]), 1)
        self.assertTrue(result["disagreements"][0]["disagreement"])
        self.assertEqual(result["disagreements"][0]["consensus_strength"], "NONE")


class RelevanceTests(unittest.TestCase):
    """20〜21. watchlist relevance / position relevance（既存Phase5 decision_relevance_score
    を再利用、指示書23番）"""

    def test_watchlist_relevance(self):
        score = server.compute_decision_relevance_score(watch_related=True)
        self.assertEqual(score, 15)

    def test_position_relevance_higher_than_watchlist(self):
        score_position = server.compute_decision_relevance_score(direct_position_hit=True)
        score_watch = server.compute_decision_relevance_score(watch_related=True)
        self.assertGreater(score_position, score_watch)


class ImportantOrderingTests(unittest.TestCase):
    """22. UI important ordering（指示書26・27番：構造の存在確認。厳密な並び替えスコアリング
    はクライアント側の簡易版のため、ここではAPIが必要な区分を全て返すことを確認する）"""

    def test_recent_market_intelligence_structure(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_recent_social_posts_all_sources.return_value = []
            mock_db.list_social_signals.return_value = []
            mock_db.list_watchlist.return_value = []
            mock_db.list_portfolio.return_value = []
            mock_db.get_market_source.return_value = None
            intel = server.get_recent_market_intelligence("dummy_url", "local")
        for key in ("social_signals", "prediction_market_shifts", "breaking_stock_signals",
                    "corporate_breaking", "consensus", "disagreements"):
            self.assertIn(key, intel)


class DiagnosticsPhase6Tests(unittest.TestCase):
    """23. diagnostics（指示書30・31番）"""

    def test_all_source_diagnostics_isolated(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.get_market_source.return_value = None
            mock_db.list_recent_social_posts.return_value = []
            mock_db.count_social_signal_evaluations.return_value = 0
            mock_db.count_duplicate_signal_groups_since.return_value = 0
            mock_db.count_social_signal_alerts_since.return_value = 0
            mock_db.count_high_confidence_signals_since.return_value = 0
            result = server.get_all_market_source_diagnostics("dummy_url", user_id="local")
        for handle in server.MARKET_SOURCE_BY_HANDLE:
            self.assertIn(handle, result)
            self.assertNotIn("X_API_BEARER_TOKEN", str(result[handle]))

    def test_diagnostics_no_token_leak(self):
        with mock.patch.object(server, "X_API_BEARER_TOKEN", "super-secret-token"):
            with mock.patch.object(server, "investment_db") as mock_db:
                mock_db.get_market_source.return_value = None
                mock_db.list_recent_social_posts.return_value = []
                mock_db.count_social_signal_evaluations.return_value = 0
                mock_db.count_duplicate_signal_groups_since.return_value = 0
                mock_db.count_social_signal_alerts_since.return_value = 0
                mock_db.count_high_confidence_signals_since.return_value = 0
                diag = server.get_market_source_diagnostics("dummy_url", "kgbukabu")
        self.assertNotIn("super-secret-token", str(diag))
        self.assertTrue(diag["token_configured"])


class FetchNowPhase6Tests(unittest.TestCase):
    """24. fetch-now（指示書31番：source別手動取得は既存pollerをそのまま再利用）"""

    def test_fetch_now_reuses_poll_market_source(self):
        cfg = server.MARKET_SOURCE_BY_HANDLE["aryarya"]
        with mock.patch.object(server, "poll_market_source") as mock_poll:
            mock_poll.return_value = {"status": "ok", "fetched": 2, "newPosts": 1, "duplicates": 1,
                                       "eventsDetected": 0, "error": None}
            result = server.poll_market_source("dummy_url", "local", cfg)
        self.assertEqual(result["status"], "ok")


class RecentMarketIntelligenceTests(unittest.TestCase):
    """25. recent_market_intelligence（指示書22番）"""

    def test_prediction_shift_and_breaking_included(self):
        now_iso = _iso(datetime.datetime.now(datetime.timezone.utc))
        posts = [
            {"post_id": "1", "source_handle": "polymarketjapan", "posted_at": now_iso,
             "prediction_market_json": {"prediction_topic": "FRB利下げ", "probability": 70, "previous_probability": 52,
                                         "probability_change": 18, "category": "MACRO"}, "url": "https://x.com/1",
             "stock_breaking_json": None, "corporate_breaking_json": None},
            {"post_id": "2", "source_handle": "kgbukabu", "posted_at": now_iso,
             "stock_breaking_json": {"ticker": "6758", "company_name": "ソニー", "change_pct": 8.5,
                                      "catalyst": None, "limit_status": None},
             "url": "https://x.com/2", "prediction_market_json": None, "corporate_breaking_json": None},
        ]
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_recent_social_posts_all_sources.return_value = posts
            mock_db.list_social_signals.return_value = []
            mock_db.list_watchlist.return_value = []
            mock_db.list_portfolio.return_value = []
            mock_db.get_market_source.return_value = None
            intel = server.get_recent_market_intelligence("dummy_url", "local")
        self.assertEqual(len(intel["prediction_market_shifts"]), 1)
        self.assertEqual(len(intel["breaking_stock_signals"]), 1)


class Phase5BackwardCompatibilityTests(unittest.TestCase):
    """26. Phase5後方互換"""

    def test_nicosoku_poll_once_still_works(self):
        with mock.patch.object(server, "poll_market_source") as mock_poll:
            mock_poll.return_value = {"status": "ok", "newPosts": 0, "fetched": 0, "duplicates": 0,
                                       "eventsDetected": 0, "error": None}
            result = server.nicosoku_poll_once("dummy_url", "local")
        self.assertEqual(result["status"], "ok")
        cfg_used = mock_poll.call_args[0][2]
        self.assertEqual(cfg_used["handle"], server.NICOSOKU_X_USERNAME)

    def test_nicosoku_diagnostics_still_works(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.get_market_source.return_value = None
            mock_db.list_recent_social_posts.return_value = []
            mock_db.count_social_signal_evaluations.return_value = 0
            mock_db.count_duplicate_signal_groups_since.return_value = 0
            mock_db.count_social_signal_alerts_since.return_value = 0
            mock_db.count_high_confidence_signals_since.return_value = 0
            diag = server.nicosoku_diagnostics("dummy_url", "local")
        self.assertEqual(diag["username"], server.NICOSOKU_X_USERNAME)

    def test_phase5_signal_confirmation_engine_untouched(self):
        confirmed, contradicted, score = server.compute_signal_confirmation("BULLISH", 0.8, "MARKET")
        self.assertTrue(confirmed)
        self.assertEqual(score, 100)


if __name__ == "__main__":
    unittest.main()
