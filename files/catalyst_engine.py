# Catalyst Confirmation Engine（Phase F・shadow）：価格の異変（Radar・EXPANDING等）があった銘柄だけに対して、
# 「今回の値動きに、確認できる材料がどれだけ伴っているか」を判定する純粋関数群（DB・ネットワーク非依存）。
#
#   ・材料の種類(catalyst_type)・方向(POSITIVE/NEGATIVE/MIXED/NEUTRAL/UNKNOWN)・情報源の信頼度・新しさを別々に持つ
#   ・catalyst_score 0-100 は「材料の大きさ×新しさ×信頼度」。方向とは別（下方修正+増資は大きいがNEGATIVE）
#   ・決算の近さ(earnings_state)・信用/取引規制(margin_restriction_state)は独立フィールド
#   ・「材料が強い」と「今買える」は別：catalystだけでENTRYさせない。チャート(Phase C/D)と組み合わせるentry_confidenceはshadow
#   ・タイトルだけでは方向が分からない開示（「業績予想の修正」「決算短信」等）はUNKNOWN（推測しない）
#
# しきい値・重みはすべて暫定値。本番のENTRY判定は変更しない（shadow記録のみ）。

import datetime
import re

# ---------------------------------------------------------------- 材料の分類（キーワード。上から順に最初に一致したものを採用）
# (type, direction, weight, patterns)。directionは "POSITIVE"/"NEGATIVE"/"NEUTRAL"/"UNKNOWN"（タイトルだけで判断できない）
RULES = (
    ("MARGIN_REGULATION", "NEGATIVE", 40, ("増担保", "増し担保", "日々公表", "信用規制", "貸株注意喚起", "売禁", "空売り規制", "信用取引規制")),
    ("MARGIN_RELEASE", "NEUTRAL", 20, ("規制解除", "増担保解除", "増し担保解除")),
    ("DOWNWARD_REVISION", "NEGATIVE", 78, ("下方修正", "業績予想の下方", "減額修正", "通期予想を下方")),
    ("UPWARD_REVISION", "POSITIVE", 82, ("上方修正", "業績予想の上方", "増額修正", "通期予想を上方")),
    ("DIVIDEND_CUT", "NEGATIVE", 60, ("減配", "無配", "配当予想の減額", "配当予想の修正（減")),
    ("DIVIDEND_UP", "POSITIVE", 62, ("増配", "記念配当", "特別配当", "配当予想の増額")),
    ("DILUTION", "NEGATIVE", 72, ("第三者割当", "公募増資", "公募による", "新株式発行", "新株予約権付社債", "転換社債", "行使価額修正条項付",
                                   "新株予約権の発行", "株式の売出し", "オーバーアロットメント", "ライツ・オファリング")),
    ("BUYBACK", "POSITIVE", 74, ("自己株式の取得", "自己株式取得", "自社株買い", "自己株式の公開買付け")),
    ("CANCELLATION", "POSITIVE", 55, ("自己株式の消却", "自己株式消却")),
    ("TOB", "POSITIVE", 90, ("公開買付", "TOB")),
    ("MNA", "POSITIVE", 60, ("子会社化", "完全子会社", "株式交換", "吸収合併", "経営統合", "事業譲受", "事業譲渡", "株式取得", "M&A")),
    ("CAPITAL_ALLIANCE", "POSITIVE", 66, ("資本業務提携", "資本提携")),
    ("BUSINESS_ALLIANCE", "POSITIVE", 42, ("業務提携", "業務連携", "提携")),
    ("LARGE_ORDER", "POSITIVE", 60, ("大型受注", "受注", "大口契約", "大型契約", "契約締結", "納入決定", "採用決定")),
    ("APPROVAL", "POSITIVE", 55, ("承認取得", "製造販売承認", "承認を取得", "認可取得", "薬事承認", "承認のお知らせ")),
    ("SPLIT", "POSITIVE", 45, ("株式分割",)),
    ("NEW_PRODUCT", "POSITIVE", 36, ("新製品", "新商品", "新サービス", "発売", "提供開始")),
    ("PATENT", "POSITIVE", 32, ("特許",)),
    ("EARNINGS", "UNKNOWN", 62, ("決算短信", "四半期決算", "決算発表", "決算補足", "決算説明", "四半期報告", "通期決算", "決算")),
    ("REVISION", "UNKNOWN", 66, ("業績予想の修正", "業績予想修正", "業績修正", "予想値と実績値の差異", "予想の修正")),
    ("THEME_AI_SEMI", "POSITIVE", 22, ("生成AI", "AI", "半導体", "データセンター", "量子", "ロボット", "宇宙", "防衛")),
)
_AI_RE = re.compile(r"(?<![A-Za-z])AI(?![A-Za-z])")     # "MAIN"等の英単語に含まれるAIは拾わない
# 「業績予想の修正」は方向がタイトルに無い（UNKNOWN）→ 上のREVISIONルールより前に、方向つきの語で判定済み。

# 方向のヒント（決算・修正の見出しで使う。NQN/日経の見出しなどに含まれることがある）
POS_HINTS = ("増益", "最高益", "黒字転換", "黒字化", "上振れ", "増収", "大幅増", "好調", "上回", "過去最高", "増額", "上方")
NEG_HINTS = ("減益", "赤字", "下振れ", "減収", "大幅減", "不振", "下回", "特別損失", "減額", "下方", "最終赤字", "減損")

TYPE_LABEL = {
    "MARGIN_REGULATION": "信用・取引規制", "MARGIN_RELEASE": "規制解除", "DOWNWARD_REVISION": "下方修正", "UPWARD_REVISION": "上方修正",
    "DIVIDEND_CUT": "減配", "DIVIDEND_UP": "増配", "DILUTION": "希薄化（増資・CB等）", "BUYBACK": "自社株買い", "CANCELLATION": "自己株消却",
    "TOB": "TOB", "MNA": "M&A", "CAPITAL_ALLIANCE": "資本業務提携", "BUSINESS_ALLIANCE": "業務提携", "LARGE_ORDER": "受注・大型契約",
    "APPROVAL": "承認", "SPLIT": "株式分割", "NEW_PRODUCT": "新製品", "PATENT": "特許", "EARNINGS": "決算", "REVISION": "業績予想の修正",
    "THEME_AI_SEMI": "テーマ関連（AI/半導体等）", "OTHER": "その他", "DB_CATALYST": "登録済み材料",
}
CAPITAL_POLICY_POSITIVE = ("BUYBACK", "CANCELLATION", "SPLIT")
CAPITAL_POLICY_NEGATIVE = ("DILUTION",)

# ---------------------------------------------------------------- 情報源の信頼度
CONFIDENCE_ORDER = ("HIGH", "MEDIUM", "LOW", "UNVERIFIED")
CONF_FACTOR = {"HIGH": 1.0, "MEDIUM": 0.8, "LOW": 0.5, "UNVERIFIED": 0.3}
# 優先順位：1 TDnet/会社IR → 2 決算短信 → 3 取引所/制度情報 → 4 日経/Reuters等 → 5 その他ニュース → 6 SNS・噂（UNVERIFIED）
SOURCE_CONFIDENCE = {"TDNET": "HIGH", "IR": "HIGH", "EARNINGS_REPORT": "HIGH", "EXCHANGE": "HIGH", "TACHIBANA_DISCLOSURE": "HIGH",
                     "NQN": "MEDIUM", "NIKKEI": "MEDIUM", "REUTERS": "MEDIUM", "NEWS": "LOW", "DB_VERIFIED": "MEDIUM",
                     "DB_UNVERIFIED": "LOW", "SNS": "UNVERIFIED"}


def source_confidence(source):
    return SOURCE_CONFIDENCE.get(source, "LOW")


_PREFIX_RE = re.compile(r"^\s*(<[^>]{1,20}>\s*)+(AI\s*[:：]\s*)?(?:[^\s()（）]{0,30}\(\w{4}\)\s*)?")     # 「<TDnet>AI: アキッパ(627A) 」等の配信元ラベル・銘柄表記


def clean_title(title):
    """立花の開示速報などの配信元ラベル（<TDnet>AI: 社名(コード) ）を除いた本文。ラベルの「AI」をテーマ語と誤認しないため。"""
    return _PREFIX_RE.sub("", title or "", count=1).strip() or (title or "")


def classify_text(title):
    """見出し/開示タイトル → (type, direction, weight)。方向がタイトルだけで決まらない場合はUNKNOWN（推測しない）。"""
    t = clean_title(title)
    typ, direction, weight = "OTHER", "NEUTRAL", 10
    for rtype, rdir, rw, pats in RULES:
        if any((p in t) if p != "AI" else bool(_AI_RE.search(t)) for p in pats):
            typ, direction, weight = rtype, rdir, rw
            break
    # 決算・業績修正は見出しの語で方向を補う（無ければUNKNOWN）
    if typ in ("EARNINGS", "REVISION"):
        pos = any(h in t for h in POS_HINTS)
        neg = any(h in t for h in NEG_HINTS)
        if pos and not neg:
            direction = "POSITIVE"
        elif neg and not pos:
            direction = "NEGATIVE"
        elif pos and neg:
            direction = "MIXED"
    if typ == "TOB":
        direction = "POSITIVE" if "当社株式に対する" in t or "当社の株式に対する" in t else "UNKNOWN"   # 買い手側の見出しは方向不明
    if typ == "DILUTION" and ("取得" in t and "自己株式" in t):
        typ, direction, weight = "BUYBACK", "POSITIVE", 74
    # テーマ語だけ（AI等）の弱い材料は、他の語が無ければ「テーマ連想のみ」として弱く扱う
    return typ, direction, weight


# ---------------------------------------------------------------- 新しさ
def business_days_between(a, b):
    """aからbまでの営業日数（土日のみ除外。祝日は呼び出し側が渡すcalendar関数で補う想定の簡易版）。a<=b。"""
    if a > b:
        return 0
    n, d = 0, a
    while d < b:
        d += datetime.timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


FRESHNESS_FACTOR = {"TODAY": 1.0, "PREV_BUSINESS_DAY": 0.8, "2_3_DAYS": 0.55, "WITHIN_WEEK": 0.35, "OLD": 0.08}


def freshness(published_at, now, bdays_fn=business_days_between):
    """published_at/now: aware datetime。戻り値: (bucket, age_hours, age_business_days)。"""
    if published_at is None:
        return "OLD", None, None
    age_h = round((now - published_at).total_seconds() / 3600.0, 1)
    bd = bdays_fn(published_at.date(), now.date())
    if bd == 0:
        bucket = "TODAY"
    elif bd == 1:
        bucket = "PREV_BUSINESS_DAY"
    elif bd <= 3:
        bucket = "2_3_DAYS"
    elif bd <= 5:
        bucket = "WITHIN_WEEK"
    else:
        bucket = "OLD"
    return bucket, age_h, bd


# ---------------------------------------------------------------- 材料アイテム → スナップショット
def build_item(title, source, published_at, now, url=None, bdays_fn=business_days_between, verified=None):
    """1件の材料候補を評価する。sourceはSOURCE_CONFIDENCEのキー（TDNET/NQN/NEWS/SNS/DB_VERIFIED…）。"""
    typ, direction, weight = classify_text(title)
    conf = source_confidence(source)
    if source in ("DB_VERIFIED", "DB_UNVERIFIED") and verified is not None:
        conf = "MEDIUM" if verified else "LOW"
    bucket, age_h, bd = freshness(published_at, now, bdays_fn)
    score = weight * FRESHNESS_FACTOR[bucket] * CONF_FACTOR[conf]
    return {"title": title, "type": typ, "label": TYPE_LABEL.get(typ, typ), "direction": direction, "weight": weight, "source": source,
            "confidence": conf, "freshness": bucket, "age_hours": age_h, "age_business_days": bd, "score": round(score, 1),
            "published_at": published_at.isoformat() if published_at else None, "url": url}


def aggregate_direction(items):
    """方向の集約。POSITIVE / NEGATIVE / MIXED / NEUTRAL / UNKNOWN。新しさ・信頼度を掛けた寄与で判定。"""
    pos = sum(i["score"] for i in items if i["direction"] == "POSITIVE")
    neg = sum(i["score"] for i in items if i["direction"] == "NEGATIVE")
    mixed = sum(i["score"] for i in items if i["direction"] == "MIXED")
    unk = sum(i["score"] for i in items if i["direction"] == "UNKNOWN")
    neu = sum(i["score"] for i in items if i["direction"] == "NEUTRAL")
    if not items:
        return "UNKNOWN"
    if mixed > 0 or (pos >= 10 and neg >= 10 and min(pos, neg) >= 0.4 * max(pos, neg)):
        return "MIXED"
    if pos >= 10 and pos >= neg:
        return "POSITIVE"
    if neg >= 10 and neg > pos:
        return "NEGATIVE"
    if unk >= max(pos, neg, neu, 1) and unk >= 10:
        return "UNKNOWN"
    return "NEUTRAL"


# ---------------------------------------------------------------- 決算の近さ
EARNINGS_TITLE = ("決算短信", "四半期決算", "決算発表", "通期決算", "四半期報告")


def earnings_state(items, next_earnings_date, today, bdays_fn=business_days_between, calendar_known=True):
    """EARNINGS_TODAY / POST_EARNINGS / PRE_EARNINGS / NO_NEAR_EARNINGS / UNKNOWN（カレンダーが無い場合は推測しない）。
    ・今日または直近3営業日に決算短信等の開示がある → EARNINGS_TODAY / POST_EARNINGS
    ・次回決算予定日が今日〜5営業日以内 → EARNINGS_TODAY / PRE_EARNINGS
    戻り値: (state, days_to_next)"""
    for i in items:
        if i["type"] == "EARNINGS" and any(k in (i["title"] or "") for k in EARNINGS_TITLE):
            if i["age_business_days"] == 0:
                return "EARNINGS_TODAY", 0
            if i["age_business_days"] is not None and i["age_business_days"] <= 3:
                return "POST_EARNINGS", None
    if next_earnings_date is not None:
        d = business_days_between(today, next_earnings_date) if next_earnings_date >= today else None
        if d is not None:
            if d == 0:
                return "EARNINGS_TODAY", 0
            if d <= 5:
                return "PRE_EARNINGS", d
            return "NO_NEAR_EARNINGS", d
        return "NO_NEAR_EARNINGS", None
    return ("NO_NEAR_EARNINGS" if calendar_known else "UNKNOWN"), None


# ---------------------------------------------------------------- 信用・取引規制
def margin_restriction_from_flags(flags, prev_active=None):
    """立花 CLMStkGetIssueSizyouKiseiKabu の1銘柄分 → 規制の有無と種類。
      増し担保：即日入金規制(sSokuzituNyukinC=1) ／ 日々公表：信用一極集中区分=2 ／ 事前調整：sZizenCyouseiC=1 ／
      信用新規停止：制度/一般の新規買建・売建の停止区分が0以外 ／ 取引停止：sTeisiKubun!=0
    貸株注意喚起・空売り規制（値幅制限型の空売り規制）はこのAPIには無いため判定しない（unavailable_kinds）。
    prev_active: 前営業日に規制があったか（True/False/None）。戻り値のstateは NONE / NEW_RESTRICTION / ACTIVE / RELEASED / UNKNOWN。"""
    if flags is None:
        return {"state": "UNKNOWN", "kinds": [], "active": None, "unavailable_kinds": ["貸株注意喚起", "空売り規制"]}
    kinds = []

    def nz(k):
        return str(flags.get(k) or "0") not in ("0", "", "None")
    if str(flags.get("sSokuzituNyukinC") or "0") == "1" or nz("sSokuzituNyukinCYoku") and str(flags.get("sSokuzituNyukinCYoku")) == "1":
        kinds.append("MARGIN_DEPOSIT_SAME_DAY")            # 増し担保（即日入金規制）
    if str(flags.get("sSinyouSyutyuKubun") or "0") == "2":
        kinds.append("DAILY_PUBLICATION")                    # 日々公表
    elif str(flags.get("sSinyouSyutyuKubun") or "0") == "1":
        kinds.append("MARGIN_CONCENTRATION")
    if str(flags.get("sZizenCyouseiC") or "0") == "1":
        kinds.append("PRE_ADJUSTMENT")
    if any(nz(k) for k in ("sSeidoSinyouSinkiKaitate", "sIppanSinyouSinkiKaitate")):
        kinds.append("MARGIN_NEW_BUY_HALT")
    if any(nz(k) for k in ("sSeidoSinyouSinkiUritate", "sIppanSinyouSinkiUritate")):
        kinds.append("MARGIN_NEW_SELL_HALT")               # 売禁（新規売建停止）
    if nz("sTeisiKubun"):
        kinds.append("TRADING_HALT")
    active = bool(kinds)
    if active and prev_active is False:
        state = "NEW_RESTRICTION"
    elif active:
        state = "ACTIVE"
    elif prev_active:
        state = "RELEASED"
    else:
        state = "NONE"
    return {"state": state, "kinds": kinds, "active": active, "unavailable_kinds": ["貸株注意喚起", "空売り規制"]}


# ---------------------------------------------------------------- 総合スナップショット
def build_snapshot(code, items, now, earnings_next=None, calendar_known=True, margin=None, lookup=None, anomaly=None,
                   bdays_fn=business_days_between):
    """材料アイテム群 → スナップショット。lookup: 取得状況 {"tdnet":True/False, "news":..., "db":..., "calendar":..., "regulation":...}
    anomaly: 価格の異変 {"chgPct":..,"volSurge":..}（UNEXPLAINED_MOVE判定用）。"""
    items = sorted(items, key=lambda i: -i["score"])
    real = [i for i in items if i["type"] != "OTHER" or i["score"] >= 4]
    top = real[0] if real else None
    direction = aggregate_direction(real)
    mag = 0.0
    if real:
        mag = real[0]["score"] + 0.35 * sum(i["score"] for i in real[1:3])
    score = int(round(min(100.0, mag * 1.15)))          # 重み82×新しさ1.0×HIGH1.0 = 82 → 94。弱い/古い材料は低く出る
    earn, days_to = earnings_state(items, earnings_next, now.date(), bdays_fn, calendar_known)
    m = margin or {"state": "UNKNOWN", "kinds": [], "active": None}
    complete = bool(lookup) and all(lookup.get(k) for k in ("tdnet", "news"))
    unexplained = bool(anomaly and (abs(anomaly.get("chgPct") or 0) >= 5 or (anomaly.get("volSurge") or 0) >= 3)
                       and complete and not [i for i in real if i["freshness"] in ("TODAY", "PREV_BUSINESS_DAY", "2_3_DAYS") and i["score"] >= 12])
    strong = [i for i in real if i["score"] >= 40 and i["confidence"] in ("HIGH", "MEDIUM")]
    cap_pos = any(i["type"] in CAPITAL_POLICY_POSITIVE and i["score"] >= 20 for i in real)
    cap_neg = any(i["type"] in CAPITAL_POLICY_NEGATIVE and i["score"] >= 20 for i in real)
    state = "CONFIRMED" if real else ("NONE_FOUND" if complete else "INCOMPLETE")
    return {"code": code, "detected_at": now.isoformat(), "state": state, "catalyst_type": top["type"] if top else None,
            "catalyst_label": top["label"] if top else None, "direction": direction,
            "confidence": (top["confidence"] if top else None), "age_hours": (top["age_hours"] if top else None),
            "age_business_days": (top["age_business_days"] if top else None), "freshness": (top["freshness"] if top else None),
            "source": (top["source"] if top else None), "catalyst_score": score, "earnings_state": earn, "days_to_earnings": days_to,
            "margin_restriction": m["state"], "margin_kinds": m.get("kinds", []), "strong_confirmed": bool(strong),
            "buyback": cap_pos and any(i["type"] == "BUYBACK" for i in real), "dilution": cap_neg,
            "unexplained_move": unexplained, "items": items[:8], "lookup": lookup or {}}


def combined_label(snap, limit=2):
    """UI用：材料: 上方修正 + 自社株買い"""
    parts = []
    for i in snap.get("items", []):
        if i["type"] != "OTHER" and i["label"] not in parts and i["score"] >= 10:
            parts.append(i["label"])
        if len(parts) >= limit:
            break
    return " + ".join(parts)


# ---------------------------------------------------------------- 値動きとの整合・フラグ・entry_confidence（shadow）
STRONG_CATALYST = 70
BAD_CHART_PATTERNS = ("CHASE", "EXTENDED", "EXHAUSTION", "FAILED_BREAKOUT")


def reaction_flags(snap, ctx):
    """材料と値動きの整合。ctx: {"chgPct","aboveVwap","lowerHighs","volPeakout","pattern","momentum_mode","holding"}。
      GOOD_NEWS_BAD_REACTION / BAD_NEWS_STRONG_PRICE / POST_EARNINGS反応（CATALYST_CONFIRMED / SELL_THE_NEWS_EXPECTATION_MISS /
      NEGATIVE_NEWS_BUT_PRICE_STRONG / NEGATIVE_CONFIRMED）/ PRE_EARNINGS_RUNUP / UNEXPLAINED_MOVE / MOMENTUM_RISK_HIGH / CATALYST_FAILURE"""
    flags = []
    chg = ctx.get("chgPct")
    d, score = snap.get("direction"), snap.get("catalyst_score") or 0
    weak_price = bool(ctx.get("aboveVwap") is False or ctx.get("lowerHighs") or ctx.get("volPeakout")
                      or ctx.get("pattern") in ("FAILED_BREAKOUT", "VWAP_LOSS"))
    strong_price = bool((chg or 0) >= 2.0 and ctx.get("aboveVwap") is not False)
    if d == "POSITIVE" and score >= 50 and weak_price:
        flags.append("GOOD_NEWS_BAD_REACTION")
        if ctx.get("holding"):
            flags.append("CATALYST_FAILURE")
    if d == "NEGATIVE" and score >= 40 and strong_price:
        flags.append("BAD_NEWS_STRONG_PRICE")
    if snap.get("earnings_state") == "POST_EARNINGS" or snap.get("earnings_state") == "EARNINGS_TODAY":
        if d == "POSITIVE" and chg is not None:
            flags.append("CATALYST_CONFIRMED" if chg > 0 else "SELL_THE_NEWS_EXPECTATION_MISS")
        elif d == "NEGATIVE" and chg is not None:
            flags.append("NEGATIVE_NEWS_BUT_PRICE_STRONG" if chg > 0 else "NEGATIVE_CONFIRMED")
    if snap.get("earnings_state") == "PRE_EARNINGS" and (chg or 0) >= 3.0:
        flags.append("PRE_EARNINGS_RUNUP")
    if snap.get("unexplained_move"):
        flags.append("UNEXPLAINED_MOVE")
    if snap.get("margin_restriction") in ("ACTIVE", "NEW_RESTRICTION") and ctx.get("momentum_mode"):
        flags.append("MOMENTUM_RISK_HIGH")
    if snap.get("dilution") or snap.get("catalyst_type") == "DOWNWARD_REVISION":
        if d in ("NEGATIVE", "MIXED"):
            flags.append("NEGATIVE_CATALYST_ACTIVE")
    return flags


def entry_confidence(snap, chart):
    """ENTRY confidence（0-100）とverdict（shadow。既存のscore・ENTRY判定は置き換えない）。
    chart: {"entry_timing","movement","pattern","liquidity"(0-100),"event_risk"(0-100,高いほど危険)}。
    材料が強くても、チャートが悪ければ買わない（CATALYST_STRONG_BUT_NOT_NOW）。材料だけでENTRYさせない。"""
    if snap is None or snap.get("state") == "PENDING":
        return {"score": None, "verdict": "CATALYST_PENDING", "size_hint": None, "stop_hint": None, "chase_strictness": None}
    timing, mv = chart.get("entry_timing"), chart.get("movement")
    pattern = chart.get("pattern")
    d, cs = snap.get("direction"), snap.get("catalyst_score") or 0
    cat = cs if d == "POSITIVE" else (cs * 0.5 if d in ("MIXED",) else 0)
    liq = chart.get("liquidity")
    parts = [(0.35, timing), (0.25, mv), (0.25, cat), (0.15, liq)]
    known = [(w, v) for w, v in parts if v is not None]
    base = (sum(w * v for w, v in known) / sum(w for w, _ in known)) if known else None
    penalty = 0
    if snap.get("margin_restriction") in ("ACTIVE", "NEW_RESTRICTION"):
        penalty += 10
    if snap.get("earnings_state") in ("PRE_EARNINGS", "EARNINGS_TODAY"):
        penalty += 8
    if snap.get("unexplained_move"):
        penalty += 8
    if d == "NEGATIVE" and cs >= 40:
        penalty += 20
    penalty += (chart.get("event_risk") or 0) * 0.1
    score = None if base is None else int(round(max(0.0, min(100.0, base - penalty))))
    bad_chart = pattern in BAD_CHART_PATTERNS or (timing is not None and timing < 40)
    if d == "POSITIVE" and cs >= STRONG_CATALYST and bad_chart:
        verdict = "CATALYST_STRONG_BUT_NOT_NOW"            # 材料は強いが今は買わない（押し目待ち）
    elif d == "POSITIVE" and cs >= STRONG_CATALYST and (mv or 0) >= 60 and (timing or 0) >= 60:
        verdict = "HIGH_CONFIDENCE"
    elif d == "NEGATIVE" and cs >= 40:
        verdict = "NEGATIVE_CATALYST_AVOID"
    elif snap.get("unexplained_move"):
        verdict = "UNEXPLAINED_CAUTION"
    elif snap.get("state") == "INCOMPLETE":
        verdict = "CATALYST_PENDING"
    else:
        verdict = "NORMAL"
    caution = verdict in ("UNEXPLAINED_CAUTION", "NEGATIVE_CATALYST_AVOID") or snap.get("margin_restriction") in ("ACTIVE", "NEW_RESTRICTION")
    return {"score": score, "verdict": verdict,
            "size_hint": "REDUCE" if caution else ("NORMAL" if verdict != "HIGH_CONFIDENCE" else "NORMAL_OR_UP"),
            "stop_hint": ("RISK_HIGH" if snap.get("margin_restriction") in ("ACTIVE", "NEW_RESTRICTION")
                          else ("TIGHTER" if snap.get("unexplained_move") or d in ("UNKNOWN",) and cs < 10 else "NORMAL")),
            "chase_strictness": "STRICT" if verdict in ("UNEXPLAINED_CAUTION",) or snap.get("unexplained_move") else "NORMAL"}


def exit_hints(snap, ctx):
    """EXIT判断への接続（shadow・ヒントのみ）。ctx: {"upperWick","surge","healthyPullback","holding"}。"""
    hints = []
    d, cs = snap.get("direction"), snap.get("catalyst_score") or 0
    if d == "POSITIVE" and cs >= STRONG_CATALYST and ctx.get("healthyPullback"):
        hints.append("HOLD_LONGER_OK")                       # 強材料＋健全な押し目
    if snap.get("unexplained_move") and ctx.get("surge") and ctx.get("upperWick"):
        hints.append("TAKE_PROFIT_EARLY")                    # 材料不明＋急騰＋上ヒゲ
    if d in ("NEGATIVE", "MIXED") and (snap.get("dilution") or snap.get("catalyst_type") == "DOWNWARD_REVISION"):
        hints.append("EXIT_URGENCY_UP")                      # 下方修正/増資
    if "GOOD_NEWS_BAD_REACTION" in (ctx.get("flags") or []):
        hints.append("CATALYST_FAILURE_WARNING")
    return hints


# ---------------------------------------------------------------- 発火条件（price anomaly → catalyst lookup）
def trigger_reasons(c, prev=None):
    """候補dict（サーバー側のpool候補）から、Catalyst確認を走らせる理由を返す。空なら確認しない（重い調査を常時走らせない）。
    prev: 前回の状態 {"codes_top5": set, "dynamic": set}（新規採用・新規昇格の検出用）。"""
    r = []
    if c.get("rollingState") in ("ROLLING_SURGE", "SINGLE_BAR_SURGE", "ROLLING_PRE_BREAKOUT") or c.get("radarState") in ("RADAR_SURGE", "RADAR_PRE_BREAKOUT"):
        r.append("RADAR")
    if c.get("activityState") == "EXPANDING":
        r.append("EXPANDING")
    if c.get("preBreakout"):
        r.append("PRE_BREAKOUT")
    if c.get("chartPattern") == "EARLY_BREAKOUT":
        r.append("EARLY_BREAKOUT")
    if c.get("chartEntryState") in ("ENTRY_READY", "NOW_BUYABLE") or c.get("movementRecommendation") == "ENTRY_READY":
        r.append("ENTRY_READY")
    f = c.get("movementFeatures") or {}
    if (f.get("volRatioRecent") or 0) >= 3.0:
        r.append("VOLUME_SURGE")
    if abs(c.get("changePct") or 0) >= 5.0 and (c.get("activityState") in ("EXPANDING", "ACTIVE")):
        r.append("PRICE_MOVE")
    if prev:
        if c.get("code") in (prev.get("new_top5") or ()):
            r.append("TOP5_NEW")
        if c.get("code") in (prev.get("new_dynamic") or ()):
            r.append("DYNAMIC_PROMOTED")
    if c.get("holding") and abs(c.get("changePct") or 0) >= 3.0:
        r.append("HOLDING_MOVE")
    return r
