# X Intelligence Phase1（2026-09-15新規）：正式指定7アカウントのmarket_sources登録。
#
# 監査（2026-09-14実施）で判明した実害：
#  - MARKET_SOURCE_CONFIGSには当初4source（nicosokufx/polymarketjapan/kgbukabu/aryarya）
#    しか無く、正式リストの7アカウント中3つ（nikkei/reutersjapan/bloombergjapan）が
#    コードにすら存在しなかった。
#  - _nicosoku_poll_scheduler_loop()はX_API_BEARER_TOKEN未設定時に即returnしており、
#    ensure_market_source()が一度も呼ばれないため、本番DBのmarket_sourcesには
#    実際には1行（nicosokufx、旧経路由来）しか登録されていなかった（実データ確認済み）。
#
# 本ファイルはこの2点の回帰を防ぐ。実際のDB接続・X API呼び出しは一切行わない
# （investment_dbをモックする）。
#
# 実行方法： cd files && python -m unittest test_x_intelligence_phase1_sources -v

import unittest
from unittest import mock

import server

EXPECTED_HANDLES = {
    "nicosokufx": "にこそく",
    "nikkei": "日本経済新聞",
    "polymarketjapan": "Polymarket Japan",
    "aryarya": "ありゃりゃ",
    "kgbukabu": "KGB",
    "reutersjapan": "Reuters Japan",
    "bloombergjapan": "Bloomberg Japan",
}


class MarketSourceConfigShapeTests(unittest.TestCase):
    """正式指定7アカウントが全てMARKET_SOURCE_CONFIGSに存在すること。"""

    def test_all_seven_handles_present(self):
        handles = {c["handle"] for c in server.MARKET_SOURCE_CONFIGS}
        self.assertEqual(handles, set(EXPECTED_HANDLES.keys()))

    def test_display_names_match_official_list(self):
        for handle, expected_name in EXPECTED_HANDLES.items():
            cfg = server.MARKET_SOURCE_BY_HANDLE[handle]
            self.assertEqual(cfg["display_name"], expected_name)

    def test_news_media_accounts_use_news_media_source_type(self):
        for handle in ("nikkei", "reutersjapan", "bloombergjapan"):
            self.assertEqual(server.MARKET_SOURCE_BY_HANDLE[handle]["source_type"], "NEWS_MEDIA")

    def test_existing_four_sources_unchanged_source_type(self):
        # 既存4source（Phase6時点）のsource_typeは変更しない（後方互換）。
        self.assertEqual(server.MARKET_SOURCE_BY_HANDLE["nicosokufx"]["source_type"], "MARKET_COMMENTARY")
        self.assertEqual(server.MARKET_SOURCE_BY_HANDLE["polymarketjapan"]["source_type"], "PREDICTION_MARKET")
        self.assertEqual(server.MARKET_SOURCE_BY_HANDLE["kgbukabu"]["source_type"], "STOCK_BREAKING")
        self.assertEqual(server.MARKET_SOURCE_BY_HANDLE["aryarya"]["source_type"], "CORPORATE_BREAKING")

    def test_news_media_in_source_types_tuple(self):
        self.assertIn("NEWS_MEDIA", server.SOURCE_TYPES)

    def test_trust_weight_ranks_news_media_above_personal_accounts(self):
        # 指示書の重み付け：一次情報/公式 > Reuters/Bloomberg/日経 > 個人発信。
        w = server.MARKET_SOURCE_TYPE_TRUST_WEIGHT
        self.assertGreater(w["NEWS_MEDIA"], w["MARKET_COMMENTARY"])
        self.assertGreater(w["NEWS_MEDIA"], w["STOCK_BREAKING"])
        self.assertGreater(w["NEWS_MEDIA"], w["CORPORATE_BREAKING"])


class EnsureAllMarketSourcesRegisteredTests(unittest.TestCase):
    """ensure_all_market_sources_registered()が7件全てにensure_market_sourceを呼ぶこと。"""

    def test_calls_ensure_market_source_for_all_seven(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.ensure_market_source.return_value = {"handle": "x"}
            result = server.ensure_all_market_sources_registered("dummy_url")
            self.assertEqual(mock_db.ensure_market_source.call_count, 7)
            self.assertEqual(set(result.keys()), set(EXPECTED_HANDLES.keys()))

    def test_one_source_failure_does_not_block_others(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            def side_effect(database_url, platform, handle, **kwargs):
                if handle == "nikkei":
                    raise Exception("DB error")
                return {"handle": handle}
            mock_db.ensure_market_source.side_effect = side_effect
            result = server.ensure_all_market_sources_registered("dummy_url")
            self.assertIsNone(result["nikkei"])
            self.assertEqual(result["kgbukabu"], {"handle": "kgbukabu"})

    def test_no_database_url_returns_empty(self):
        result = server.ensure_all_market_sources_registered(None)
        self.assertEqual(result, {})


class SchedulerRegistersSourcesEvenWithoutTokenTests(unittest.TestCase):
    """回帰テスト：X_API_BEARER_TOKEN未設定でも、スケジューラのエントリポイントで
    ensure_all_market_sources_registered()が呼ばれてから安全にreturnすること
    （以前はtoken未設定時に即returnし、登録処理自体が一度も走らなかった）。"""

    def test_scheduler_registers_sources_before_bailing_without_token(self):
        with mock.patch.object(server, "X_API_BEARER_TOKEN", ""), \
             mock.patch.object(server, "ensure_all_market_sources_registered") as mock_ensure, \
             mock.patch.object(server, "DATABASE_URL", "dummy_url"):
            server._nicosoku_poll_scheduler_loop()
            mock_ensure.assert_called_once_with("dummy_url")


class NewsMediaAccountDoesNotCrashPostBuilderTests(unittest.TestCase):
    """NEWS_MEDIA種別のsource_configが_build_social_post_record()を安全に通ること
    （STOCK_BREAKING/CORPORATE_BREAKING/PREDICTION_MARKET専用抽出のいずれにも該当せず、
    Noneのまま素通りする＝クラッシュしない）。"""

    def test_news_media_tweet_builds_without_specialized_extraction(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_portfolio.return_value = []
            mock_db.list_watchlist.return_value = []
            tweet = {"id": "1", "text": "日銀、金融政策決定会合で追加利上げを検討との観測",
                     "created_at": "2026-09-15T00:00:00Z"}
            record, posted_date = server._build_social_post_record(
                "dummy_url", "matsuura", tweet, {}, "nikkei", "日本経済新聞",
                source_config=server.MARKET_SOURCE_BY_HANDLE["nikkei"])
            self.assertIsNone(record["prediction_market"])
            self.assertIsNone(record["stock_breaking"])
            self.assertIsNone(record["corporate_breaking"])
            self.assertIn("text", record)
            self.assertEqual(record["source_handle"], "nikkei")


if __name__ == "__main__":
    unittest.main()
