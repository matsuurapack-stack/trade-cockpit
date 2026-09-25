import base64
import datetime
import json
import unittest
import urllib.parse
from unittest import mock

import tachibana_api as t


def b64(text):
    """p_HDL/p_TX と同じ：ShiftJISをURLエンコード→BASE64。"""
    return base64.b64encode(urllib.parse.quote(text, encoding="cp932").encode("ascii")).decode("ascii")


class Resp:
    def __init__(self, obj):
        self.data = json.dumps(obj, ensure_ascii=False).encode("cp932", errors="replace")


class FakeHttp:
    def __init__(self, responder):
        self.calls = []
        self.responder = responder

    def request(self, method, url, body=None, **kw):
        payload = json.loads(body.decode("utf-8")) if body else {}
        self.calls.append((url, payload))
        return Resp(self.responder(url, payload))


def install(responder):
    fake = FakeHttp(responder)
    t._news_day_cache.clear()
    t._session, t._session_date = {"sUrlMaster": "https://m/", "sUrlPrice": "https://p/", "sUrlRequest": "https://r/"}, datetime.date.today().isoformat()
    return fake


class VersionTests(unittest.TestCase):
    def test_login_urls_are_v4r10_and_no_v4r9_fallback(self):
        self.assertIn("e_api_v4r10", t.PROD_LOGIN_URL)
        self.assertIn("e_api_v4r10", t.DEMO_LOGIN_URL)
        src = open(t.__file__, encoding="utf-8").read()
        self.assertNotIn("e_api_v4r9/", src)
        for gone in ('"CLMMfdsGetMasterData"', '"CLMMfdsGetNewsHead"', '"CLMMfdsGetNewsBody"', '"CLMEventDownload"'):
            self.assertNotIn(gone, src)                          # v4r10で廃止されたI/Fを呼ばない


class MasterTests(unittest.TestCase):
    def test_issue_master_uses_new_interface(self):
        fake = install(lambda u, p: {"p_errno": "0", "aCLMStkIssueMstKabu": [
            {"sIssueCode": "1301", "sIssueName": "極 洋", "sIssueNameKana": "キヨクヨウ", "sGyousyuCode": "0050"}]})
        with mock.patch.object(t, "_http", fake):
            out = t.get_issue_master_kabu()
        self.assertEqual(fake.calls[0][1]["sCLMID"], "CLMStkGetIssueMstKabu")
        self.assertEqual(fake.calls[0][0], "https://m/")
        self.assertEqual(out, [{"code": "1301", "name": "極洋", "kana": "キヨクヨウ", "gyoshuCode": "0050"}])

    def test_market_master(self):
        fake = install(lambda u, p: {"p_errno": "0", "aCLMStkIssueSizyouMstKabu": [
            {"sIssueCode": "1301", "sZyouzyouSizyou": "00", "sSinyouC": "1"}, {"sIssueCode": "9999", "sZyouzyouSizyou": "01", "sSinyouC": "2"}]})
        with mock.patch.object(t, "_http", fake):
            out = t.get_issue_market_master_kabu()
        self.assertEqual(fake.calls[0][1]["sCLMID"], "CLMStkGetIssueSizyouMstKabu")
        self.assertEqual(out, {"1301": {"sSinyouC": "1"}})

    def test_master_error_raises(self):
        fake = install(lambda u, p: {"p_errno": "-1", "p_err": "x"})
        with mock.patch.object(t, "_http", fake), self.assertRaises(RuntimeError):
            t.get_issue_master_kabu()


class NewsTests(unittest.TestCase):
    def rows(self):
        return [
            {"p_ID": "20260925090000_A1", "p_TM": "0900", "p_CGL": "120", "p_GNL": "1", "p_ISL": "627A", "p_HDL": b64("<TDnet>AI: アキッパ(627A) 業績予想の修正"), "p_TX": b64("本文です")},
            {"p_ID": "20260925150000_M1", "p_TM": "1500", "p_CGL": "110", "p_GNL": "2", "p_ISL": "|".join(["627A"] + [str(1000 + i) for i in range(45)]),
             "p_HDL": b64("<AI市況>【騰落率】値上がり上位"), "p_TX": b64("市況")},
            {"p_ID": "20260925100000_N1", "p_TM": "1000", "p_CGL": "100", "p_GNL": "3", "p_ISL": "", "p_HDL": b64("<NQN>◇日経平均"), "p_TX": b64("nqn")},
        ]

    def make(self):
        return install(lambda u, p: {"p_errno": "0", "sCLMID": "CLMMfdsGetNews", "aCLMMfdsNews": self.rows()} if p["sCLMID"] == "CLMMfdsGetNews" else {"p_errno": "-1"})

    def test_stock_news_filters_by_code_and_excludes_market_roundups(self):
        fake = self.make()
        with mock.patch.object(t, "_http", fake):
            out = t.get_stock_news("627A", "20260925", "20260925")
        self.assertEqual([h["id"] for h in out], ["20260925090000_A1"])       # 関連46銘柄の市況まとめは個別銘柄のニュースではない
        self.assertTrue(out[0]["headline"].startswith("<TDnet>"))
        self.assertEqual(fake.calls[0][1]["sCLMID"], "CLMMfdsGetNews")
        self.assertEqual(fake.calls[0][1]["p_DT"], "20260925")
        with mock.patch.object(t, "_http", fake):
            allrel = t.get_stock_news("627A", "20260925", "20260925", max_related=None)
        self.assertEqual(len(allrel), 2)

    def test_headlines_by_category(self):
        fake = self.make()
        with mock.patch.object(t, "_http", fake):
            out = t.get_news_headlines(["100", "120"], "20260925", "20260925")
        self.assertEqual({(h["category"], h["id"]) for h in out}, {("100", "20260925100000_N1"), ("120", "20260925090000_A1")})

    def test_body_comes_from_day_list_and_day_is_cached(self):
        fake = self.make()
        with mock.patch.object(t, "_http", fake):
            self.assertEqual(t.get_news_body("20260925090000_A1"), "本文です")
            self.assertEqual(t.get_news_body("20260925150000_M1"), "市況")
            self.assertEqual(t.get_news_body("20260925000000_none"), "")
            self.assertEqual(t.get_news_body(""), "")
        self.assertEqual(len(fake.calls), 1)                                     # 過去日は1回だけ取得（更新されない）

    def test_today_uses_short_ttl_and_past_days_are_permanent(self):
        fake = self.make()
        today = datetime.datetime.now(t._JST).strftime("%Y%m%d")
        with mock.patch.object(t, "_http", fake):
            t._news_day(today)
            t._news_day(today)
            self.assertEqual(len(fake.calls), 1)
            t._news_day_cache[today] = (t._news_day_cache[today][0] - t.NEWS_TODAY_TTL_SEC - 1, t._news_day_cache[today][1])
            t._news_day(today)
            self.assertEqual(len(fake.calls), 2)

    def test_failure_raises_only_if_every_day_fails(self):
        def responder(u, p):
            return {"p_errno": "-1", "p_err": "boom"} if p["p_DT"] == "20260925" else {"p_errno": "0", "aCLMMfdsNews": []}
        fake = install(responder)
        with mock.patch.object(t, "_http", fake):
            self.assertEqual(t.get_stock_news("627A", "20260924", "20260925"), [])      # 一部の日だけ失敗→取れた日で続行
            with self.assertRaises(RuntimeError):
                t.get_stock_news("627A", "20260925", "20260925")                           # 全日失敗→例外（材料なしと誤認しない）


class RegulationTests(unittest.TestCase):
    def test_request_and_parse(self):
        fake = install(lambda u, p: {"p_errno": "0", "aCLMStkIssueSizyouKiseiKabu": [
            {"sIssueCode": "4440", "sZyouzyouSizyou": "00", "sSeidoSinyouSinkiUritate": "1", "sSokuzituNyukinC": "0"},
            {"sIssueCode": "4440", "sZyouzyouSizyou": "01", "sSokuzituNyukinC": "1"}]})
        with mock.patch.object(t, "_http", fake):
            out = t.get_issue_regulation_kabu()
        self.assertEqual(fake.calls[0][1]["sCLMID"], "CLMStkGetIssueSizyouKiseiKabu")
        self.assertEqual(out["4440"]["sSeidoSinyouSinkiUritate"], "1")            # 東証(00)のみ

    def test_error_raises_with_reason(self):
        fake = install(lambda u, p: {"p_errno": "-1", "p_err": "引数エラー"})
        with mock.patch.object(t, "_http", fake), self.assertRaisesRegex(RuntimeError, "p_errno=-1"):
            t.get_issue_regulation_kabu()


class LogoutTests(unittest.TestCase):
    def test_logout_clears_session(self):
        fake = install(lambda u, p: {"p_errno": "0", "sResultCode": "0"})
        with mock.patch.object(t, "_http", fake):
            self.assertTrue(t.logout())
        self.assertEqual(fake.calls[0][1]["sCLMID"], "CLMAuthLogoutRequest")
        self.assertEqual(fake.calls[0][0], "https://r/")
        self.assertIsNone(t._session)


if __name__ == "__main__":
    unittest.main()
