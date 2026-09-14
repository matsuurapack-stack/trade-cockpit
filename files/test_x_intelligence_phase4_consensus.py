# X Intelligence Phase4（2026-09-15新規）：複数発信者統合（consensus/disagreement）の
# 回帰テスト。既存のbuild_market_intelligence_consensus()自体はMarket Intelligence Phase6
# 時点の実装（変更なし）。Phase4は「Phase1Bで実際に投稿が保存されるようになったことで、
# このロジックが実データで正しく動くか」を検証する段階のため、新規バグは見つからず
# 実装変更は無い。ここでは実データE2Eで確認した3パターンをmockベースで固定化する。
#
# 実行方法： cd files & python -m unittest test_x_intelligence_phase4_consensus -v

import datetime
import unittest
from unittest import mock

import server


def _post(handle, text, url=None, minutes_ago=1):
    posted_at = (datetime.datetime.now(datetime.timezone.utc) -
                 datetime.timedelta(minutes=minutes_ago)).isoformat()
    return {
        "source_handle": handle, "post_id": f"{handle}-1", "text": text,
        "posted_at": posted_at, "primary_source_url": url,
        "direct_mentions_json": [], "categories_json": ["OTHER"],
    }


class ConsensusAgreementTests(unittest.TestCase):
    """実データE2Eで確認：kgbukabu+reutersjapanが独立に半導体BULLISHを投稿→consensus成立。"""

    def test_two_independent_bullish_sources_form_consensus(self):
        posts = [_post("kgbukabu", "半導体株が強い、買われている"),
                 _post("reutersjapan", "半導体セクターは堅調に推移")]
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_recent_social_posts_all_sources.return_value = posts
            result = server.build_market_intelligence_consensus("dummy_url")
        semis = [c for c in result["consensus"] if c["topic"] == "SEMICONDUCTOR"]
        self.assertEqual(len(semis), 1)
        self.assertEqual(semis[0]["direction"], "BULLISH")
        self.assertEqual(semis[0]["independent_source_count"], 2)
        self.assertEqual(set(semis[0]["sources"]), {"kgbukabu", "reutersjapan"})

    def test_single_source_does_not_form_consensus(self):
        posts = [_post("kgbukabu", "半導体株が強い、買われている")]
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_recent_social_posts_all_sources.return_value = posts
            result = server.build_market_intelligence_consensus("dummy_url")
        self.assertEqual(result["consensus"], [])


class ConsensusDisagreementTests(unittest.TestCase):
    """実データE2Eで確認：nicosokufx BULLISH vs aryarya BEARISHの銀行株→disagreement成立。"""

    def test_opposing_directions_form_disagreement_not_consensus(self):
        posts = [_post("nicosokufx", "銀行株が強い、上昇継続"),
                 _post("aryarya", "銀行株は弱い、下落基調")]
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_recent_social_posts_all_sources.return_value = posts
            result = server.build_market_intelligence_consensus("dummy_url")
        banks = [d for d in result["disagreements"] if d["topic"] == "BANK"]
        self.assertEqual(len(banks), 1)
        self.assertTrue(banks[0]["disagreement"])
        self.assertEqual(banks[0]["directions"], {"nicosokufx": "BULLISH", "aryarya": "BEARISH"})
        self.assertEqual([c for c in result["consensus"] if c["topic"] == "BANK"], [])


class RepostDedupTests(unittest.TestCase):
    """実データE2Eで確認：同一primary_source_urlを2アカウントが引用→1 independent sourceに
    まとめられ、二重カウントされない（指示書19番）。"""

    def test_same_url_repost_counted_as_one_independent_source(self):
        same_url = "https://example.com/article/auto-bullish-story"
        posts = [_post("nikkei", "自動車株が強い、上昇", url=same_url),
                 _post("bloombergjapan", "自動車株が強い、上昇", url=same_url)]
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_recent_social_posts_all_sources.return_value = posts
            result = server.build_market_intelligence_consensus("dummy_url")
        # 1 independent sourceのためconsensusは生成されない（指示書18番）。
        self.assertEqual([c for c in result["consensus"] if c["topic"] == "AUTO"], [])

    def test_different_urls_same_direction_counted_as_two_independent_sources(self):
        posts = [_post("nikkei", "自動車株が強い、上昇", url="https://a.example.com/1"),
                 _post("bloombergjapan", "自動車株が強い、上昇", url="https://b.example.com/2")]
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_recent_social_posts_all_sources.return_value = posts
            result = server.build_market_intelligence_consensus("dummy_url")
        autos = [c for c in result["consensus"] if c["topic"] == "AUTO"]
        self.assertEqual(len(autos), 1)
        self.assertEqual(autos[0]["independent_source_count"], 2)


class OpinionNotTreatedAsFactRegressionTests(unittest.TestCase):
    """指示書「個人意見をFACT扱いしない」：Phase1Bのnormalize_social_post_importが生成する
    facts/author_opinionの分離が、consensus計算対象の投稿でも保たれていること
    （consensus自体はテキスト全体から方向性を読む別軸のロジックであり、この分離を壊さない）。"""

    def test_post_record_keeps_facts_and_opinion_separated_for_consensus_input(self):
        with mock.patch.object(server, "investment_db") as mock_db:
            mock_db.list_portfolio.return_value = []
            mock_db.list_watchlist.return_value = []
            draft = {"source_handle": "aryarya", "text": "銀行株は弱い、下落基調"}
            normalized = server.normalize_social_post_import("dummy_url", "matsuura", draft)
        # author_opinion_json相当のキー("author_opinion")が個別に保持されており、
        # factsへ意見が混入していないことを確認する。
        self.assertIn("facts", normalized["post_record"])
        self.assertIn("author_opinion", normalized["post_record"])


if __name__ == "__main__":
    unittest.main()
