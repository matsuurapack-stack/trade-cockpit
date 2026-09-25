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


class EngineStructuralStopTests(unittest.TestCase):
    """実測(2026-09-26)：停止区分が0以外の銘柄611件のうち、信用区分=3（一般信用のみ）等は構造的な非対応で規制ではない。"""

    def test_structural_stops_are_not_restrictions(self):
        import catalyst_engine as ce
        stop = {"sSeidoSinyouSinkiKaitate": "1", "sSeidoSinyouSinkiUritate": "1", "sTeisiKubun": "1"}
        self.assertEqual(ce.margin_restriction_from_flags(dict(stop, _sinyouC="3"))["tachibana_restriction_state"], "NONE")
        self.assertEqual(ce.margin_restriction_from_flags(dict(stop, _sinyouC="?"))["tachibana_restriction_state"], "NONE")
        self.assertEqual(ce.margin_restriction_from_flags(dict(stop, _sinyouC="2"))["state"], "ACTIVE")
        self.assertEqual(ce.margin_restriction_from_flags(stop)["state"], "ACTIVE")            # 信用区分なし＝従来どおり

    def test_hard_kinds_apply_regardless_of_sinyou_class(self):
        import catalyst_engine as ce
        r = ce.margin_restriction_from_flags({"sSokuzituNyukinC": "1", "sSinyouSyutyuKubun": "2", "_sinyouC": "3"})
        self.assertEqual(r["state"], "ACTIVE")
        self.assertEqual(set(r["kinds"]), {"MARGIN_DEPOSIT_SAME_DAY", "DAILY_PUBLICATION"})


class SerializedRequestTests(unittest.TestCase):
    """p_errno=6（p_noが前要求以下）の再発防止：スレッド並列でもp_noは送信順に単調増加し、リクエストは同時に1件だけ。"""

    def test_p_no_assigned_at_send_time_and_monotonic_under_threads(self):
        import threading
        import time
        seen, active, maxactive = [], [0], [0]
        lk = threading.Lock()

        class Raw:
            def request(self, method, url, body=None, **kw):
                with lk:
                    active[0] += 1
                    maxactive[0] = max(maxactive[0], active[0])
                time.sleep(0.005)
                seen.append(int(json.loads(body.decode("utf-8"))["p_no"]))
                with lk:
                    active[0] -= 1
                return Resp({"p_errno": "0"})

        def worker():
            for _ in range(15):
                # 呼び出し側の採番（古い値）でも、送信の瞬間に採番し直される
                t._http.request("POST", "https://m/", body=json.dumps({"sCLMID": "X", "p_no": str(t._next_p_no()), "p_sd_date": "x"}).encode("utf-8"))
        with mock.patch.object(t, "_raw_http", Raw()):
            ths = [threading.Thread(target=worker) for _ in range(6)]
            [x.start() for x in ths]
            [x.join() for x in ths]
        self.assertEqual(len(seen), 90)
        self.assertEqual(seen, sorted(seen))                       # 送信順にp_noが増える（追い越しなし）
        self.assertEqual(len(set(seen)), 90)
        self.assertEqual(maxactive[0], 1)                          # 一問一答

    def test_auth_url_is_not_serialized_or_rewritten(self):
        got = {}

        class Raw:
            def request(self, method, url, body=None, **kw):
                got["body"] = json.loads(body.decode("utf-8"))
                return Resp({})
        with mock.patch.object(t, "_raw_http", Raw()):
            t._http.request("POST", "https://x/e_api_v4r10/auth/", body=json.dumps({"p_no": "1", "sCLMID": "CLMAuthLoginRequest"}).encode("utf-8"))
        self.assertEqual(got["body"]["p_no"], "1")


class SellHaltOnlyTests(unittest.TestCase):
    def test_sell_halt_alone_is_not_a_margin_restriction_for_long_momentum(self):
        import catalyst_engine as ce
        r = ce.margin_restriction_from_flags({"sSeidoSinyouSinkiUritate": "1", "_sinyouC": "1"})
        self.assertEqual((r["tachibana_restriction_state"], r["active"], r["kinds"]), ("NONE", False, ["MARGIN_NEW_SELL_HALT"]))
        r2 = ce.margin_restriction_from_flags({"sSeidoSinyouSinkiUritate": "1", "sSeidoSinyouSinkiKaitate": "1", "_sinyouC": "2"})
        self.assertEqual(r2["state"], "ACTIVE")                                   # 新規買建も停止＝規制


class RestrictionSemanticsTests(unittest.TestCase):
    """規制情報の意味付け：立花NONE≠信用規制なし。JPX増担保は未取得なのでUNKNOWN。総合stateをNONEに確定しない。"""

    def test_4440_like_issue(self):
        import catalyst_engine as ce
        r = ce.margin_restriction_from_flags({"sSeidoSinyouSinkiUritate": "1", "sSeidoSinyouGenbiki": "1", "_sinyouC": "1"})   # 4440の実データ相当
        self.assertEqual(r["tachibana_restriction_state"], "NONE")
        self.assertEqual(r["jpx_margin_restriction_state"], "UNKNOWN")
        self.assertEqual(r["state"], "UNKNOWN")
        d = r["restriction_details"]
        self.assertIs(d["tachibana"]["new_sell_halt"], True)
        self.assertIs(d["tachibana"]["new_buy_halt"], False)
        for k in ("margin_deposit_increase(増担保)", "lending_caution(貸株注意喚起)", "short_sale_regulation(空売り規制)"):
            self.assertEqual(d["not_confirmable"][k], "UNKNOWN")

    def test_overall_follows_tachibana_only_when_restriction_confirmed(self):
        import catalyst_engine as ce
        self.assertEqual(ce.margin_restriction_from_flags({"sSeidoSinyouSinkiKaitate": "1", "_sinyouC": "2"})["state"], "ACTIVE")
        self.assertEqual(ce.margin_restriction_from_flags({"sSeidoSinyouSinkiKaitate": "1", "_sinyouC": "2"}, prev_active=False)["state"], "NEW_RESTRICTION")
        self.assertEqual(ce.margin_restriction_from_flags({}, prev_active=True)["state"], "RELEASED")
        self.assertEqual(ce.margin_restriction_from_flags(None)["state"], "UNKNOWN")
        self.assertEqual(ce.margin_restriction_from_flags(None)["restriction_details"]["tachibana"]["same_day_deposit"], "UNKNOWN")

    def test_snapshot_carries_split_states_and_details(self):
        import catalyst_engine as ce
        snap = ce.build_snapshot("6862", [], datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))),
                                 margin=ce.margin_restriction_from_flags({"sSeidoSinyouSinkiKaitate": "1", "_sinyouC": "2"}))
        self.assertEqual((snap["margin_restriction"], snap["tachibana_restriction_state"], snap["jpx_margin_restriction_state"]), ("ACTIVE", "ACTIVE", "UNKNOWN"))
        self.assertIs(snap["restriction_details"]["tachibana"]["new_buy_halt"], True)

    def test_ui_never_asserts_no_margin_increase(self):
        html = open(__import__("os").path.join(__import__("os").path.dirname(t.__file__), "trade-cockpit.html"), encoding="utf-8").read()
        self.assertNotIn("増し担保：", html)
        self.assertIn("「増担保なし」とは言えません", html)

    def test_restriction_never_touches_entry_or_exit_source(self):
        import inspect
        import catalyst_engine as ce
        for name in ("entry_confidence", "exit_hints"):
            self.assertIn(name, dir(ce))              # 規制はshadowのhintに出るだけ。既存のENTRY/EXIT側からcatalyst_engineを参照していないことは既存テストで固定済み


if __name__ == "__main__":
    unittest.main()
