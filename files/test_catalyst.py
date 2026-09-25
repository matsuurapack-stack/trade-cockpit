# Catalyst Confirmation（Phase F）のエンジン・非同期lookupのテスト。 cd files && python -m unittest test_catalyst -v

import datetime
import time
import unittest

import catalyst_engine as ce
import catalyst_lookup as cl

JST = datetime.timezone(datetime.timedelta(hours=9))
NOW = datetime.datetime(2026, 9, 28, 10, 30, tzinfo=JST)            # 月曜の場中


def dt(days_ago=0, hour=9, minute=0):
    d = (NOW - datetime.timedelta(days=days_ago)).replace(hour=hour, minute=minute)
    return d


def item(title, source="TDNET", days_ago=0, **kw):
    return ce.build_item(title, source, dt(days_ago), NOW, **kw)


class ClassifyTests(unittest.TestCase):
    def test_types_and_directions(self):
        c = ce.classify_text
        self.assertEqual(c("2026年3月期 通期業績予想の上方修正に関するお知らせ")[:2], ("UPWARD_REVISION", "POSITIVE"))
        self.assertEqual(c("通期業績予想の下方修正に関するお知らせ")[:2], ("DOWNWARD_REVISION", "NEGATIVE"))
        self.assertEqual(c("業績予想の修正に関するお知らせ")[:2], ("REVISION", "UNKNOWN"))          # 方向はタイトルだけでは不明＝推測しない
        self.assertEqual(c("配当予想の修正（増配）に関するお知らせ")[:2], ("DIVIDEND_UP", "POSITIVE"))
        self.assertEqual(c("期末配当予想の修正（減配）")[:2], ("DIVIDEND_CUT", "NEGATIVE"))
        self.assertEqual(c("株式分割及び株式分割に伴う定款の一部変更")[:2], ("SPLIT", "POSITIVE"))
        self.assertEqual(c("資本業務提携に関するお知らせ")[:2], ("CAPITAL_ALLIANCE", "POSITIVE"))
        self.assertEqual(c("大型受注に関するお知らせ")[:2], ("LARGE_ORDER", "POSITIVE"))
        self.assertEqual(c("株式会社Aの子会社化に関するお知らせ")[:2], ("MNA", "POSITIVE"))
        self.assertEqual(c("製造販売承認取得のお知らせ")[:2], ("APPROVAL", "POSITIVE"))

    def test_buyback_and_dilution_are_separated(self):
        self.assertEqual(ce.classify_text("自己株式取得に係る事項の決定に関するお知らせ")[:2], ("BUYBACK", "POSITIVE"))
        self.assertEqual(ce.classify_text("自己株式の消却に関するお知らせ")[:2], ("CANCELLATION", "POSITIVE"))
        for t in ("第三者割当による新株式発行に関するお知らせ", "新株予約権付社債の発行に関するお知らせ", "公募増資及び株式の売出しに関するお知らせ",
                  "行使価額修正条項付新株予約権の発行"):
            self.assertEqual(ce.classify_text(t)[:2], ("DILUTION", "NEGATIVE"), t)

    def test_tob_direction_depends_on_who_is_acquiring(self):
        self.assertEqual(ce.classify_text("株式会社Xによる当社株式に対する公開買付けの開始に関するお知らせ")[:2], ("TOB", "POSITIVE"))
        self.assertEqual(ce.classify_text("当社による株式会社Yの公開買付けの開始")[:2], ("TOB", "UNKNOWN"))

    def test_earnings_direction_only_from_hints(self):
        self.assertEqual(ce.classify_text("2026年3月期 第1四半期決算短信〔日本基準〕(連結)")[:2], ("EARNINGS", "UNKNOWN"))
        self.assertEqual(ce.classify_text("〇〇、4-6月期決算 最終赤字に転落")[1], "NEGATIVE")
        self.assertEqual(ce.classify_text("〇〇、4-6月期決算 増益も通期は下振れ")[1], "MIXED")

    def test_delivery_labels_are_not_classified_as_themes(self):
        # 立花の開示速報「<TDnet>AI: 社名(コード) 本文」の「AI」は配信元ラベル。テーマ語と誤認しない
        self.assertEqual(ce.classify_text("<TDnet>AI: アキッパ(627A) 会社説明及び今後の戦略概要")[0], "OTHER")
        self.assertEqual(ce.clean_title("<TDnet>AI: アキッパ(627A) 主要株主の異動に関するお知らせ"), "主要株主の異動に関するお知らせ")
        self.assertEqual(ce.classify_text("<TDnet>AI: アキッパ(627A) 通期業績予想の上方修正に関するお知らせ")[:2], ("UPWARD_REVISION", "POSITIVE"))
        self.assertEqual(ce.classify_text("<TDnet>AI: アキッパ(627A) 東京証券取引所スタンダードへの上場に伴う当社決算情報等のお知らせ")[:2], ("EARNINGS", "UNKNOWN"))
        self.assertEqual(ce.classify_text("生成AI関連の展示会に出展")[0], "THEME_AI_SEMI")

    def test_margin_notices_and_theme_only(self):
        self.assertEqual(ce.classify_text("増担保規制の実施について")[:2], ("MARGIN_REGULATION", "NEGATIVE"))
        self.assertEqual(ce.classify_text("日々公表銘柄に指定")[0], "MARGIN_REGULATION")
        self.assertEqual(ce.classify_text("増担保規制解除のお知らせ")[0], "MARGIN_REGULATION")  # 見出しは先に規制語を拾う（解除は規制情報の状態で判定）
        self.assertEqual(ce.classify_text("生成AI関連の展示会に出展")[0], "THEME_AI_SEMI")
        self.assertEqual(ce.classify_text("MAIN会議の議事")[0], "OTHER")               # 英単語中のAIは拾わない
        self.assertEqual(ce.classify_text("お知らせ")[0], "OTHER")


class FreshnessAndConfidenceTests(unittest.TestCase):
    def test_business_day_freshness_buckets(self):
        f = lambda d: ce.freshness(dt(d), NOW)[0]
        self.assertEqual(f(0), "TODAY")
        # 月曜(9/28)基準：前営業日=金曜(9/25)＝3暦日前
        self.assertEqual(ce.freshness(dt(3), NOW)[0], "PREV_BUSINESS_DAY")
        self.assertEqual(ce.freshness(dt(4), NOW)[0], "2_3_DAYS")
        self.assertEqual(ce.freshness(dt(7), NOW)[0], "WITHIN_WEEK")
        self.assertEqual(ce.freshness(dt(30), NOW)[0], "OLD")
        b, age_h, bd = ce.freshness(dt(0, hour=7, minute=30), NOW)
        self.assertEqual((b, age_h, bd), ("TODAY", 3.0, 0))

    def test_same_type_is_weaker_when_old(self):
        fresh, old = item("資本業務提携に関するお知らせ", days_ago=0), item("資本業務提携に関するお知らせ", days_ago=30)
        self.assertGreater(fresh["score"], 55)
        self.assertLess(old["score"], 10)                                   # 1か月前の提携は今日の急騰理由として弱い
        self.assertEqual(old["freshness"], "OLD")
        self.assertIsNotNone(old["age_hours"])

    def test_source_confidence_order_and_sns_unverified(self):
        t = "通期業績予想の上方修正に関するお知らせ"
        tdnet, nqn, news, sns = item(t, "TDNET"), item(t, "NQN"), item(t, "NEWS"), item(t, "SNS")
        self.assertEqual([tdnet["confidence"], nqn["confidence"], news["confidence"], sns["confidence"]], ["HIGH", "MEDIUM", "LOW", "UNVERIFIED"])
        self.assertGreater(tdnet["score"], nqn["score"])
        self.assertGreater(nqn["score"], news["score"])
        self.assertGreater(news["score"], sns["score"])
        self.assertEqual(item(t, "DB_UNVERIFIED", verified=False)["confidence"], "LOW")
        self.assertEqual(item(t, "DB_VERIFIED", verified=True)["confidence"], "MEDIUM")


class DirectionAndScoreTests(unittest.TestCase):
    def snap(self, items, **kw):
        return ce.build_snapshot("6270", items, NOW, lookup={"tdnet": True, "news": True, "db": True}, **kw)

    def test_positive_negative_mixed_neutral_unknown(self):
        pos = self.snap([item("通期業績予想の上方修正"), item("自己株式取得に係る事項の決定")])
        self.assertEqual((pos["direction"], pos["buyback"], pos["dilution"]), ("POSITIVE", True, False))
        neg = self.snap([item("通期業績予想の下方修正"), item("第三者割当による新株式発行")])
        self.assertEqual((neg["direction"], neg["dilution"]), ("NEGATIVE", True))
        mixed = self.snap([item("4-6月期決算 増益", "NQN"), item("通期業績予想の下方修正")])
        self.assertEqual(mixed["direction"], "MIXED")
        pr = self.snap([item("お知らせ", "NEWS")])
        self.assertIn(pr["direction"], ("NEUTRAL", "UNKNOWN"))
        self.assertLess(pr["catalyst_score"], 10)
        unk = self.snap([item("業績予想の修正に関するお知らせ")])
        self.assertEqual(unk["direction"], "UNKNOWN")                       # 方向不明の修正は推測しない
        self.assertEqual(self.snap([])["direction"], "UNKNOWN")

    def test_strong_and_weak_catalyst_scores(self):
        strong = self.snap([item("通期業績予想の上方修正"), item("自己株式取得に係る事項の決定")])
        weak = self.snap([item("生成AI関連の展示会に出展", "NEWS")])
        old = self.snap([item("業務提携に関するお知らせ", days_ago=30)])
        self.assertGreaterEqual(strong["catalyst_score"], 85)
        self.assertLess(weak["catalyst_score"], 15)
        self.assertLess(old["catalyst_score"], 10)
        self.assertTrue(strong["strong_confirmed"])
        self.assertFalse(weak["strong_confirmed"])

    def test_snapshot_has_the_specified_fields(self):
        s = self.snap([item("大型受注に関するお知らせ")], margin={"state": "NONE", "kinds": []})
        for k in ("code", "detected_at", "catalyst_type", "direction", "confidence", "age_hours", "source", "earnings_state",
                  "margin_restriction", "catalyst_score"):
            self.assertIn(k, s)
        self.assertEqual((s["catalyst_type"], s["source"], s["margin_restriction"]), ("LARGE_ORDER", "TDNET", "NONE"))

    def test_combined_label(self):
        s = self.snap([item("通期業績予想の上方修正"), item("自己株式取得に係る事項の決定"), item("お知らせ", "NEWS")])
        self.assertEqual(ce.combined_label(s), "上方修正 + 自社株買い")


class EarningsStateTests(unittest.TestCase):
    def test_states(self):
        today = NOW.date()
        e0 = item("2026年3月期 第1四半期決算短信", days_ago=0)
        self.assertEqual(ce.earnings_state([e0], None, today)[0], "EARNINGS_TODAY")
        e2 = item("第1四半期決算短信", days_ago=4)          # 月曜基準で2営業日前
        self.assertEqual(ce.earnings_state([e2], None, today)[0], "POST_EARNINGS")
        self.assertEqual(ce.earnings_state([], today + datetime.timedelta(days=2), today), ("PRE_EARNINGS", 2))
        self.assertEqual(ce.earnings_state([], today, today), ("EARNINGS_TODAY", 0))
        self.assertEqual(ce.earnings_state([], today + datetime.timedelta(days=30), today)[0], "NO_NEAR_EARNINGS")
        self.assertEqual(ce.earnings_state([], None, today, calendar_known=False)[0], "UNKNOWN")     # カレンダー不明は推測しない
        self.assertEqual(ce.earnings_state([], None, today, calendar_known=True)[0], "NO_NEAR_EARNINGS")


class MarginTests(unittest.TestCase):
    def test_kinds_and_state_transitions(self):
        active = {"sSokuzituNyukinC": "1", "sSinyouSyutyuKubun": "0"}
        m = ce.margin_restriction_from_flags(active, prev_active=True)
        self.assertEqual((m["state"], m["kinds"]), ("ACTIVE", ["MARGIN_DEPOSIT_SAME_DAY"]))
        self.assertEqual(ce.margin_restriction_from_flags(active, prev_active=False)["state"], "NEW_RESTRICTION")
        self.assertEqual(ce.margin_restriction_from_flags({"sSokuzituNyukinC": "0"}, prev_active=True)["state"], "RELEASED")
        self.assertEqual(ce.margin_restriction_from_flags({"sSokuzituNyukinC": "0"}, prev_active=False)["tachibana_restriction_state"], "NONE")
        self.assertEqual(ce.margin_restriction_from_flags({"sSokuzituNyukinC": "0"}, prev_active=None)["tachibana_restriction_state"], "NONE")
        self.assertEqual(ce.margin_restriction_from_flags(None)["state"], "UNKNOWN")
        daily = ce.margin_restriction_from_flags({"sSinyouSyutyuKubun": "2"})
        self.assertIn("DAILY_PUBLICATION", daily["kinds"])
        halt = ce.margin_restriction_from_flags({"sSeidoSinyouSinkiKaitate": "1", "sIppanSinyouSinkiUritate": "2", "sTeisiKubun": "0"})
        self.assertEqual(set(halt["kinds"]), {"MARGIN_NEW_BUY_HALT", "MARGIN_NEW_SELL_HALT"})
        self.assertIn("空売り規制", m["unavailable_kinds"])                                # このAPIで判定できない種類を明示


class FlagTests(unittest.TestCase):
    def snap(self, items, earn=None, margin=None, anomaly=None, complete=True):
        s = ce.build_snapshot("X", items, NOW, lookup={"tdnet": complete, "news": complete, "db": True},
                              margin=margin, anomaly=anomaly)
        if earn:
            s["earnings_state"] = earn
        return s

    def test_unexplained_move_needs_complete_lookup_and_no_recent_explanation(self):
        a = {"chgPct": 10.0, "volSurge": 4.0}
        self.assertTrue(self.snap([], anomaly=a)["unexplained_move"])
        self.assertFalse(self.snap([], anomaly=a, complete=False)["unexplained_move"])         # 取得不完全なら断定しない
        self.assertTrue(self.snap([item("業務提携に関するお知らせ", days_ago=30)], anomaly=a)["unexplained_move"])   # 古い材料は説明にならない
        self.assertFalse(self.snap([item("通期業績予想の上方修正")], anomaly=a)["unexplained_move"])
        self.assertFalse(self.snap([], anomaly={"chgPct": 1.0, "volSurge": 1.0})["unexplained_move"])

    def test_good_news_bad_reaction_and_bad_news_strong_price(self):
        pos = self.snap([item("通期業績予想の上方修正")])
        self.assertIn("GOOD_NEWS_BAD_REACTION", ce.reaction_flags(pos, {"chgPct": 1.0, "aboveVwap": False}))
        self.assertIn("GOOD_NEWS_BAD_REACTION", ce.reaction_flags(pos, {"chgPct": 1.0, "aboveVwap": True, "volPeakout": True}))
        self.assertIn("GOOD_NEWS_BAD_REACTION", ce.reaction_flags(pos, {"chgPct": 1.0, "aboveVwap": True, "lowerHighs": True}))
        self.assertNotIn("GOOD_NEWS_BAD_REACTION", ce.reaction_flags(pos, {"chgPct": 3.0, "aboveVwap": True}))
        self.assertIn("CATALYST_FAILURE", ce.reaction_flags(pos, {"chgPct": 0.5, "aboveVwap": False, "holding": True}))
        neg = self.snap([item("通期業績予想の下方修正")])
        self.assertIn("BAD_NEWS_STRONG_PRICE", ce.reaction_flags(neg, {"chgPct": 4.0, "aboveVwap": True}))
        self.assertNotIn("BAD_NEWS_STRONG_PRICE", ce.reaction_flags(neg, {"chgPct": -2.0, "aboveVwap": False}))

    def test_post_earnings_reaction_classes(self):
        pos = self.snap([item("4-6月期決算 最高益 増益", "NQN")], earn="POST_EARNINGS")
        neg = self.snap([item("4-6月期決算 最終赤字", "NQN")], earn="POST_EARNINGS")
        self.assertIn("CATALYST_CONFIRMED", ce.reaction_flags(pos, {"chgPct": 5.0}))
        self.assertIn("SELL_THE_NEWS_EXPECTATION_MISS", ce.reaction_flags(pos, {"chgPct": -3.0}))
        self.assertIn("NEGATIVE_NEWS_BUT_PRICE_STRONG", ce.reaction_flags(neg, {"chgPct": 4.0}))
        self.assertIn("NEGATIVE_CONFIRMED", ce.reaction_flags(neg, {"chgPct": -4.0}))
        unk = self.snap([item("第1四半期決算短信")], earn="POST_EARNINGS")
        self.assertFalse({"CATALYST_CONFIRMED", "SELL_THE_NEWS_EXPECTATION_MISS"} & set(ce.reaction_flags(unk, {"chgPct": 5.0})))   # 内容不明は判定しない

    def test_pre_earnings_runup_and_margin_momentum_risk(self):
        s = self.snap([], earn="PRE_EARNINGS")
        self.assertIn("PRE_EARNINGS_RUNUP", ce.reaction_flags(s, {"chgPct": 4.0}))
        m = self.snap([], margin=ce.margin_restriction_from_flags({"sSokuzituNyukinC": "1"}, prev_active=True))
        self.assertIn("MOMENTUM_RISK_HIGH", ce.reaction_flags(m, {"chgPct": 6.0, "momentum_mode": True}))
        self.assertNotIn("MOMENTUM_RISK_HIGH", ce.reaction_flags(m, {"chgPct": 6.0, "momentum_mode": False}))


class EntryConfidenceTests(unittest.TestCase):
    def snap(self, items, **kw):
        return ce.build_snapshot("X", items, NOW, lookup={"tdnet": True, "news": True, "db": True}, **kw)

    STRONG = [("通期業績予想の上方修正",), ("自己株式取得に係る事項の決定",), ("資本業務提携に関するお知らせ",)]

    def strong(self):
        return self.snap([item(t[0]) for t in self.STRONG])

    def test_high_confidence_combination(self):
        ec = ce.entry_confidence(self.strong(), {"entry_timing": 79, "movement": 82, "pattern": "EARLY_BREAKOUT", "liquidity": 80})
        self.assertEqual(ec["verdict"], "HIGH_CONFIDENCE")
        self.assertGreaterEqual(ec["score"], 75)

    def test_strong_catalyst_but_chase_is_not_now(self):
        s = self.strong()
        self.assertGreaterEqual(s["catalyst_score"], 88)
        ec = ce.entry_confidence(s, {"entry_timing": 24, "movement": 70, "pattern": "CHASE", "liquidity": 80})
        self.assertEqual(ec["verdict"], "CATALYST_STRONG_BUT_NOT_NOW")             # 材料は強いが今は買わない
        self.assertLess(ec["score"], 70)

    def test_catalyst_alone_does_not_make_high_confidence(self):
        ec = ce.entry_confidence(self.strong(), {"entry_timing": 35, "movement": 30, "pattern": "BASE_BUILDING", "liquidity": 50})
        self.assertNotEqual(ec["verdict"], "HIGH_CONFIDENCE")

    def test_pending_unexplained_negative_and_hints(self):
        self.assertEqual(ce.entry_confidence({"state": "PENDING"}, {})["verdict"], "CATALYST_PENDING")
        self.assertEqual(ce.entry_confidence(None, {})["verdict"], "CATALYST_PENDING")
        un = self.snap([], anomaly={"chgPct": 10.0, "volSurge": 4.0})
        ec = ce.entry_confidence(un, {"entry_timing": 70, "movement": 70, "pattern": "EARLY_BREAKOUT", "liquidity": 70})
        self.assertEqual(ec["verdict"], "UNEXPLAINED_CAUTION")
        self.assertEqual((ec["size_hint"], ec["stop_hint"], ec["chase_strictness"]), ("REDUCE", "TIGHTER", "STRICT"))
        neg = self.snap([item("通期業績予想の下方修正"), item("第三者割当による新株式発行")])
        self.assertEqual(ce.entry_confidence(neg, {"entry_timing": 80, "movement": 80})["verdict"], "NEGATIVE_CATALYST_AVOID")
        mar = self.snap([item("通期業績予想の上方修正")], margin=ce.margin_restriction_from_flags({"sSokuzituNyukinC": "1"}, prev_active=True))
        ec2 = ce.entry_confidence(mar, {"entry_timing": 70, "movement": 70, "pattern": "EARLY_BREAKOUT"})
        self.assertEqual(ec2["stop_hint"], "RISK_HIGH")                             # 規制ありはリスク表示を強める（stop幅自体は変えない）

    def test_exit_hints(self):
        s = self.strong()
        self.assertIn("HOLD_LONGER_OK", ce.exit_hints(s, {"healthyPullback": True}))
        un = self.snap([], anomaly={"chgPct": 10.0, "volSurge": 4.0})
        self.assertIn("TAKE_PROFIT_EARLY", ce.exit_hints(un, {"surge": True, "upperWick": True}))
        neg = self.snap([item("通期業績予想の下方修正")])
        self.assertIn("EXIT_URGENCY_UP", ce.exit_hints(neg, {}))
        self.assertIn("CATALYST_FAILURE_WARNING", ce.exit_hints(s, {"flags": ["GOOD_NEWS_BAD_REACTION"]}))


class TriggerTests(unittest.TestCase):
    def test_only_anomalies_trigger_lookup(self):
        self.assertEqual(ce.trigger_reasons({"code": "A", "activityState": "ACTIVE", "chartPattern": "BASE_BUILDING", "changePct": 1.0}), [])
        self.assertIn("RADAR", ce.trigger_reasons({"code": "A", "rollingState": "SINGLE_BAR_SURGE"}))
        self.assertIn("EXPANDING", ce.trigger_reasons({"code": "A", "activityState": "EXPANDING"}))
        self.assertIn("PRE_BREAKOUT", ce.trigger_reasons({"code": "A", "preBreakout": True}))
        self.assertIn("EARLY_BREAKOUT", ce.trigger_reasons({"code": "A", "chartPattern": "EARLY_BREAKOUT"}))
        self.assertIn("ENTRY_READY", ce.trigger_reasons({"code": "A", "chartEntryState": "ENTRY_READY"}))
        self.assertIn("VOLUME_SURGE", ce.trigger_reasons({"code": "A", "movementFeatures": {"volRatioRecent": 3.5}}))
        self.assertIn("PRICE_MOVE", ce.trigger_reasons({"code": "A", "changePct": 6.0, "activityState": "ACTIVE"}))
        self.assertIn("HOLDING_MOVE", ce.trigger_reasons({"code": "A", "changePct": -4.0, "holding": True}))
        prev = {"new_top5": {"A"}, "new_dynamic": {"B"}}
        self.assertIn("TOP5_NEW", ce.trigger_reasons({"code": "A"}, prev))
        self.assertIn("DYNAMIC_PROMOTED", ce.trigger_reasons({"code": "B"}, prev))


def fetchers(**over):
    base = {
        "tdnet": lambda code: [{"title": "通期業績予想の上方修正に関するお知らせ", "published_at": dt(0), "url": "u", "source": "TDNET"}],
        "news": lambda code: [{"title": "通期業績予想の上方修正に関するお知らせ", "published_at": dt(0), "source": "NQN"}],
        "db": lambda code: [],
        "earnings_next": lambda code: NOW.date() + datetime.timedelta(days=30),
        "regulation": lambda code: ({"sSokuzituNyukinC": "0"}, False),
    }
    base.update(over)
    return base


class LookupTests(unittest.TestCase):
    def svc(self, f=None, **kw):
        return cl.CatalystLookup(f or fetchers(), now_fn=lambda: NOW, **kw)

    def test_get_is_nonblocking_and_returns_pending_until_done(self):
        gate = []
        def slow_tdnet(code):
            time.sleep(0.4)
            return []
        s = self.svc(fetchers(tdnet=slow_tdnet))
        s.start()
        try:
            t0 = time.time()
            self.assertEqual(s.request("6270", "RADAR"), "QUEUED")
            self.assertEqual(s.get("6270")["state"], "PENDING")             # チャート判定側は待たない（CATALYST_PENDING）
            self.assertLess(time.time() - t0, 0.2)
            for _ in range(40):
                time.sleep(0.05)
                if s.get("6270")["state"] != "PENDING":
                    break
            self.assertNotEqual(s.get("6270")["state"], "PENDING")
        finally:
            s.stop()

    def test_snapshot_uses_all_sources_and_dedupes_by_confidence(self):
        s = self.svc()
        s.request("6270", "RADAR")
        s.drain()
        snap = s.get("6270")
        self.assertEqual(snap["state"], "CONFIRMED")
        self.assertEqual(snap["catalyst_type"], "UPWARD_REVISION")
        self.assertEqual(snap["source"], "TDNET")                            # 同じ見出しはTDnet(HIGH)を採用
        self.assertEqual(len([i for i in snap["items"] if i["type"] == "UPWARD_REVISION"]), 1)
        self.assertEqual(snap["earnings_state"], "NO_NEAR_EARNINGS")
        self.assertEqual(snap["margin_restriction"], "UNKNOWN")                       # 立花NONE・JPX未確認＝総合はNONEに確定しない
        self.assertEqual((snap["tachibana_restriction_state"], snap["jpx_margin_restriction_state"]), ("NONE", "UNKNOWN"))
        self.assertEqual(snap["trigger"], "RADAR")
        self.assertTrue(all(snap["lookup"][k] for k in ("tdnet", "news", "db", "calendar", "regulation")))

    def test_source_failures_are_isolated(self):
        def boom(code):
            raise RuntimeError("tdnet down")
        s = self.svc(fetchers(tdnet=boom))
        s.request("6270", "EXPANDING", anomaly={"chgPct": 9.0, "volSurge": 4.0})
        s.drain()
        snap = s.get("6270")
        self.assertFalse(snap["lookup"]["tdnet"])
        self.assertTrue(snap["lookup"]["news"])
        self.assertFalse(snap["unexplained_move"])                           # tdnetが取れていないので「材料なし」とは断定しない
        self.assertEqual(s.summary()["source_failures"]["tdnet"], 1)
        self.assertTrue(any("tdnet" in e for e in snap["errors"]))

    def test_unexplained_when_all_sources_ok_and_nothing_found(self):
        s = self.svc(fetchers(tdnet=lambda c: [], news=lambda c: []))
        s.request("6270", "RADAR", anomaly={"chgPct": 10.0, "volSurge": 5.0})
        s.drain()
        snap = s.get("6270")
        self.assertEqual(snap["state"], "NONE_FOUND")
        self.assertTrue(snap["unexplained_move"])

    def test_cooldown_ttl_queue_cap_and_dedupe(self):
        s = self.svc(cooldown_sec=60, max_queue=2)
        self.assertEqual(s.request("A", "RADAR"), "QUEUED")
        self.assertEqual(s.request("A", "RADAR"), "INFLIGHT")
        self.assertEqual(s.request("B", "RADAR"), "QUEUED")
        self.assertEqual(s.request("C", "RADAR"), "DROPPED")                # 溢れたら捨てる（常時重く調べない）
        s.drain()
        self.assertEqual(s.request("A", "RADAR"), "CACHED")                 # TTL内はキャッシュ
        s2 = self.svc(ttl_sec=0, cooldown_sec=60)
        s2.request("A", "RADAR")
        s2.drain()
        self.assertEqual(s2.request("A", "RADAR"), "COOLDOWN")              # 直後の再調査はクールダウン
        self.assertTrue(s2.get("A")["stale"])

    def test_on_snapshot_callback_and_summary(self):
        seen = []
        s = self.svc(on_snapshot=lambda code, snap, reason: seen.append((code, snap["catalyst_type"], reason)))
        s.request("6270", "TOP5_NEW")
        s.drain()
        self.assertEqual(seen, [("6270", "UPWARD_REVISION", "TOP5_NEW")])
        sm = s.summary()
        self.assertEqual((sm["requested"], sm["queued"], sm["done"], sm["pending"]), (1, 1, 1, 0))
        self.assertIsNotNone(sm["duration_ms_median"])

    def test_unknown_code_returns_none(self):
        self.assertIsNone(self.svc().get("0000"))


if __name__ == "__main__":
    unittest.main()
