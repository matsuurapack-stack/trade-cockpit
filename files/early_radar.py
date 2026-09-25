# Early Momentum Radar（Phase D.1・shadow）：寄り付き直後（5分足が2〜5本）の“異変”を早期に見つけるだけの
# 監視用レーダー。買い判定ではない（ENTRYには一切使わない）。純粋関数（DB・ネットワーク非依存、
# 未来データを参照しない＝渡されたbarsだけを使う）。
#
#   RADAR_SURGE / RADAR_EXPANDING / RADAR_PRE_BREAKOUT / RADAR_ACTIVE / RADAR_NONE
#   early_momentum_score 0-100
#
# 重要なのは絶対出来高ではなく「数分前より急に活発になったか」（直前足に対する加速度）。
# 通常のChart Context（Phase C）とMovement（Phase D）は6本目から従来どおり。Radarは2〜5本だけ担当し、
# 6本以上は通常Phase Dへ引き継ぐ（この関数はhandoff=Trueを返してRADAR_NONEにする）。
# Radarからは直接ENTRY_READYにしない：流れは RADAR → dynamic watch / 注目候補 →
# PRE_BREAKOUT / EXPANDING → ENTRY_READY（買う場所は既存のChart Context/Movementが決める）。
#
# しきい値はすべて暫定値。627A等の検証結果に合わせて調整しない（shadowで30〜50件貯めてから）。

import chart_context as cc

MIN_BARS = 2
HANDOFF_BARS = 6                  # 6本以上は通常のChart Context / Movement Potentialへ引き継ぐ
CONFIDENCE = {2: "LOW", 3: "MEDIUM_LOW", 4: "MEDIUM_LOW", 5: "MEDIUM"}
CONF_FACTOR = {"LOW": 0.7, "MEDIUM_LOW": 0.85, "MEDIUM": 1.0}    # 順位付け用（stateの判定には使わない）

SURGE_VOL_ACCEL = 3.0
SURGE_RANGE_ACCEL = 2.0
EXPANDING_VOL_ACCEL = 1.5
EXPANDING_RANGE_ACCEL = 1.5
PRE_BREAKOUT_DIST = (-0.8, -0.2)  # 当日高値の0.2〜0.8%下（本数が少ないので通常より下限を緩める）
PRE_BREAKOUT_VOL_ACCEL = 1.3
ACTIVE_SCORE = 40
HOT_SCORE = 60                    # dynamic watchのhot poolへ即追加する目安（RADAR_SURGE/RADAR_PRE_BREAKOUTは無条件）
WIDE_SPREAD_PCT = 0.5             # スプレッドがこれを超えると減点（約定コストが大きい）


def _scale(x, lo, hi, out):
    if x is None or hi == lo:
        return 0.0
    return max(0.0, min(1.0, (x - lo) / (hi - lo))) * out


def _ratio(a, b):
    return None if (a is None or not b or b <= 0) else a / b


def _mean(v):
    v = [x for x in v if x is not None]
    return sum(v) / len(v) if v else None


def compute_radar_features(bars, cur=None, vwap=None, day_high=None):
    """少ない足でも取れる特徴量。barsは最新が末尾（形成中の足は最新quoteで更新する）。"""
    b = cc.normalize_bars(bars)
    if not b or len(b["closes"]) < MIN_BARS:
        return None
    o, h, l, c, v = b["opens"], b["highs"], b["lows"], b["closes"], b["volumes"]
    n = len(c)
    price = cur if cur is not None else c[-1]
    if price != c[-1]:
        c = c[:-1] + [price]
        h = h[:-1] + [max(h[-1], price)]
        l = l[:-1] + [min(l[-1], price)]
    rng = [((hh - ll) / cl * 100.0) if cl else 0.0 for hh, ll, cl in zip(h, l, c)]
    tv = [cl * vv for cl, vv in zip(c, v)]
    f = {"n": n, "price": price}
    f["rangeLastPct"] = round(rng[-1], 3)
    f["rangeAccel"] = round(_ratio(rng[-1], rng[-2]), 2) if _ratio(rng[-1], rng[-2]) is not None else None
    prior_v = _mean(v[:-1])
    f["volAccelPrev"] = round(_ratio(v[-1], v[-2]), 2) if _ratio(v[-1], v[-2]) is not None else None     # 直前の足との比
    f["volAccel"] = round(_ratio(v[-1], prior_v), 2) if _ratio(v[-1], prior_v) is not None else None      # それ以前の平均との比
    prior_tv = _mean(tv[:-1])
    f["turnoverAccel"] = round(_ratio(tv[-1], prior_tv), 2) if _ratio(tv[-1], prior_tv) is not None else None
    dh = max([x for x in (day_high, max(h)) if x])
    f["dayHigh"] = dh
    f["distFromHighPct"] = round((price / dh - 1) * 100, 3) if dh else None
    f["newHigh"] = bool(n >= 2 and h[-1] >= max(h[:-1]) * 0.9999)
    tpv = sum(((hh + ll + cl) / 3.0) * vv for hh, ll, cl, vv in zip(h, l, c, v))
    tvv = sum(v)
    vw = vwap if vwap else (tpv / tvv if tvv > 0 else None)
    f["vwap"] = vw
    f["aboveVwap"] = (price > vw) if vw else None
    f["vwapDistPct"] = round((price / vw - 1) * 100, 3) if vw else None
    f["openChangePct"] = round((price / o[0] - 1) * 100, 3) if o[0] else None                            # 寄値からの変化率
    f["lastBarChgPct"] = round((c[-1] / c[-2] - 1) * 100, 3) if c[-2] else None                          # 価格変化速度（5分あたり）
    f["upLast"] = bool(c[-1] >= c[-2])
    return f


def evaluate_radar(bars, quote=None, vwap=None, day_high=None, market_rs=None, sector_lead=False, spread_pct=None,
                   quote_speed_pct_per_min=None):
    """Radar評価。quote_speed_pct_per_min（quote更新から取れた価格変化速度、無ければNone）は加点のみに使う。"""
    b = cc.normalize_bars(bars)
    n = len(b["closes"]) if b else 0
    base = {"state": "RADAR_NONE", "early_momentum_score": None, "rank_score": None, "confidence": "UNKNOWN", "handoff": False,
            "reasons": [], "features": None, "entry_allowed": False, "bars": n}
    if n >= HANDOFF_BARS:
        return dict(base, handoff=True, confidence="HANDOFF", reasons=["6本以上：通常のChart Context/Movementへ引き継ぎ"])
    f = compute_radar_features(bars, cur=(quote or {}).get("t"), vwap=vwap, day_high=day_high)
    if f is None:
        return dict(base, reasons=["5分足が2本未満（推測しない）"])
    conf = CONFIDENCE[n]
    ra, va, ta = f["rangeAccel"], f["volAccel"], f["turnoverAccel"]
    dd = f["distFromHighPct"]
    parts, why = {}, []

    def add(name, val, note=None):
        parts[name] = round(val, 1)
        if val > 0 and note:
            why.append(note)
    add("rangeAccel", _scale(ra, 1.0, 3.0, 20), f"値幅拡大（直前の{ra}倍）" if (ra or 0) >= 1.5 else None)
    add("volAccel", _scale(va, 1.0, 4.0, 25), f"出来高加速（それ以前の平均の{va}倍）" if (va or 0) >= 1.5 else None)
    add("turnoverAccel", _scale(ta, 1.0, 4.0, 10), "売買代金の増加" if (ta or 0) >= 1.5 else None)
    near = 0.0
    if dd is not None:
        near = 12.0 if dd >= -0.8 else _scale(-dd, 3.0, 0.8, 12)
    add("nearHigh", near, f"高値接近（{abs(dd)}%下）" if (dd is not None and dd >= -0.8) else None)
    add("newHigh", 8.0 if f["newHigh"] else 0.0, "高値更新" if f["newHigh"] else None)
    add("aboveVwap", 6.0 if f["aboveVwap"] else 0.0, "VWAP上" if f["aboveVwap"] else None)
    add("openChange", _scale(f["openChangePct"], 0.3, 3.0, 8), f"寄値から{f['openChangePct']:+.1f}%" if (f["openChangePct"] or 0) >= 0.5 else None)
    add("priceSpeed", _scale(f["lastBarChgPct"], 0.1, 1.0, 6) + _scale(quote_speed_pct_per_min, 0.05, 0.3, 3), None)
    add("marketRS", _scale(market_rs, 0.0, 3.0, 3))
    add("sector", 2.0 if sector_lead else 0.0, "セクター先導" if sector_lead else None)
    pen = 0.0
    if f["aboveVwap"] is False:
        pen -= 6
        why.append("VWAP下")
    if not f["upLast"]:
        pen -= 5
    if spread_pct is not None and spread_pct > WIDE_SPREAD_PCT:
        pen -= 5
        why.append(f"スプレッド広い（{spread_pct:.2f}%）")
    score = max(0.0, min(100.0, sum(parts.values()) + pen))

    up = f["upLast"]
    if (va or 0) >= SURGE_VOL_ACCEL and (ra or 0) >= SURGE_RANGE_ACCEL and up:
        state = "RADAR_SURGE"
    elif (dd is not None and PRE_BREAKOUT_DIST[0] <= dd <= PRE_BREAKOUT_DIST[1] and f["aboveVwap"] is True
          and (va or 0) >= PRE_BREAKOUT_VOL_ACCEL and (ra is None or ra >= 0.8) and up):
        state = "RADAR_PRE_BREAKOUT"
    elif (ra or 0) >= EXPANDING_RANGE_ACCEL and (va or 0) >= EXPANDING_VOL_ACCEL and up:
        state = "RADAR_EXPANDING"
    elif score >= ACTIVE_SCORE:
        state = "RADAR_ACTIVE"
    else:
        state = "RADAR_NONE"
    return {"state": state, "early_momentum_score": round(score), "rank_score": round(score * CONF_FACTOR[conf], 1),
            "confidence": conf, "handoff": False, "reasons": why, "breakdown": {**parts, "penalty": pen},
            "features": f, "entry_allowed": False, "bars": n}


def radar_hot(r):
    """dynamic watchのhot poolへ即追加する条件（通常のMovement Scoreがまだ低くても）。"""
    return bool(r and not r.get("handoff") and (r.get("state") in ("RADAR_SURGE", "RADAR_PRE_BREAKOUT")
                                                or (r.get("early_momentum_score") or 0) >= HOT_SCORE))


RADAR_LIST_STATES = ("RADAR_SURGE", "RADAR_EXPANDING", "RADAR_PRE_BREAKOUT", "RADAR_ACTIVE")


def build_early_radar_list(cands, top_n=5):
    """shadowMovement.earlyRadar：初動監視（🚨）の最大5銘柄。並びは rank_score（本数が少ないほど割り引く）。
    既存のTOP5・attentionTop5とは別枠。ここに載っても買い判定ではない（entry_allowed=False）。"""
    rows = [c for c in cands if not c.get("radarHandoff") and c.get("radarState") in RADAR_LIST_STATES
            and c.get("radarRankScore") is not None]
    rows.sort(key=lambda c: -c["radarRankScore"])
    return [{"code": c.get("code"), "name": c.get("name"), "current": c.get("current"), "changePct": c.get("changePct"),
             "radarState": c["radarState"], "earlyMomentumScore": c.get("earlyMomentumScore"),
             "rank": i + 1, "rankScore": c["radarRankScore"], "confidence": c.get("radarConfidence"),
             "text": c.get("radarText") or "", "reasons": (c.get("radarReasons") or [])[:4],
             "movementScore": c.get("movementScore"), "activityState": c.get("activityState"),
             "existingEntryState": c.get("chartEntryState") or c.get("entryState"), "entryAllowed": False,
             "label": "🚨 初動監視"}
            for i, c in enumerate(rows[:top_n])]


def radar_reason_text(r):
    """UI用：🚨 初動監視 の1行（出来高加速 / 値幅拡大 / 高値接近）。"""
    if not r or not r.get("reasons"):
        return ""
    keys = [("出来高加速", "出来高加速"), ("値幅拡大", "値幅拡大"), ("高値接近", "高値接近"), ("高値更新", "高値更新"), ("VWAP上", "VWAP上")]
    out = [label for k, label in keys if any(k in x for x in r["reasons"])]
    return " / ".join(out[:3])
