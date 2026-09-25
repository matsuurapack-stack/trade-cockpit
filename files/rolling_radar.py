# Rolling Momentum Radar（Phase D.2・shadow）：寄り付き専用のEarly Radar（D.1、5分足2〜5本）とは別レイヤーで、
# 場中を通して「直近5本の窓」で“何か始まったかもしれない”を拾う警戒レーダー。
#   ・買い判定ではない（ENTRYには使わない）。dynamic watchのhot poolへ入れるだけ。
#   ・純粋関数（DB・ネットワーク非依存、未来データを参照しない＝渡されたbarsだけを使う）。
#   ・Phase D / D.1 の既存しきい値は変更しない。ここで使う急伸のしきい値はD.1のSURGE定数を再利用する
#     （新しい数値を627Aに合わせて作らない）。
#
# 状態: ROLLING_SURGE（連続する急な活性化）/ SINGLE_BAR_SURGE（静かな状態から1本だけ急伸）/
#       ROLLING_EXPANDING（連続拡大型）/ ROLLING_PRE_BREAKOUT / RADAR_WEAK（確認要素が足りない）/ NONE
# false positive抑制：VWAP上・直近高値更新・対市場RSプラス・セクターが弱すぎない・spread悪化なし
#   のうち、判定できた要素の2つ以上が欠けたら RADAR_WEAK（hot poolの優先度を下げ、通常のdynamic watchまで）。

import chart_context as cc
import early_radar as er

WINDOW = 5
MIN_BARS = 5
SURGE_VOL = er.SURGE_VOL_ACCEL            # 3.0（D.1と同じ）
SURGE_RANGE = er.SURGE_RANGE_ACCEL        # 2.0（D.1と同じ）
SUSTAINED_VOL = 2.0                       # ROLLING_SURGE：直近2本の出来高が直前3本平均の2倍以上
SUSTAINED_RANGE = 2.0
EXPANDING_VOL = er.EXPANDING_VOL_ACCEL    # 1.5
PRE_BREAKOUT_DIST = er.PRE_BREAKOUT_DIST
PRE_BREAKOUT_VOL = er.PRE_BREAKOUT_VOL_ACCEL
WEAK_MISSING = 2                          # 判定できた確認要素のうち、これ以上欠けたら RADAR_WEAK
WIDE_SPREAD_PCT = er.WIDE_SPREAD_PCT
HOT_STATES = ("ROLLING_SURGE", "SINGLE_BAR_SURGE", "ROLLING_PRE_BREAKOUT")     # dynamic hot pool へ即追加
WATCH_STATES = ("ROLLING_EXPANDING", "RADAR_WEAK")                             # 通常のdynamic watchまで
NOTABLE_STATES = HOT_STATES + WATCH_STATES


def _mean(v):
    v = [x for x in v if x is not None]
    return sum(v) / len(v) if v else None


def _ratio(a, b):
    return None if (a is None or not b or b <= 0) else a / b


def _scale(x, lo, hi, out):
    if x is None or hi == lo:
        return 0.0
    return max(0.0, min(1.0, (x - lo) / (hi - lo))) * out


def compute_rolling_features(bars, cur=None, vwap=None, day_high=None):
    """直近5本の窓の特徴量（最新が末尾。形成中の足は最新quoteで更新する）。"""
    b = cc.normalize_bars(bars)
    if not b or len(b["closes"]) < MIN_BARS:
        return None
    o, h, l, c, v = (b[k][-WINDOW:] for k in ("opens", "highs", "lows", "closes", "volumes"))
    full_h = b["highs"]
    price = cur if cur is not None else c[-1]
    if price != c[-1]:
        c = c[:-1] + [price]
        h = h[:-1] + [max(h[-1], price)]
        l = l[:-1] + [min(l[-1], price)]
    n = len(c)
    rng = [((hh - ll) / cl * 100.0) if cl else 0.0 for hh, ll, cl in zip(h, l, c)]
    tv = [cl * vv for cl, vv in zip(c, v)]
    f = {"n": len(b["closes"]), "price": price}
    # 直近足 vs 過去4本
    f["rangeSurge"] = _r2(_ratio(rng[-1], _mean(rng[:-1])))
    f["volSurge"] = _r2(_ratio(v[-1], _mean(v[:-1])))
    f["turnoverSurge"] = _r2(_ratio(tv[-1], _mean(tv[:-1])))
    # 直前の足が既に急伸していたか（SINGLE_BAR_SURGEは「静かな状態から1本だけ」）
    f["prevRangeSurge"] = _r2(_ratio(rng[-2], _mean(rng[:-2]))) if n >= 5 else None
    f["prevVolSurge"] = _r2(_ratio(v[-2], _mean(v[:-2]))) if n >= 5 else None
    # 直近2本 vs その前3本（連続型）
    f["range2"] = _r2(_ratio(_mean(rng[-2:]), _mean(rng[:-2])))
    f["vol2"] = _r2(_ratio(_mean(v[-2:]), _mean(v[:-2])))
    f["rangeSteps"] = sum(1 for i in (-3, -2, -1) if rng[i] > rng[i - 1])
    f["upLast"] = bool(c[-1] > o[-1] and c[-1] >= c[-2])
    f["upLast2"] = bool(c[-1] > o[-1] and c[-2] > o[-2] and c[-1] >= c[-2])
    dh = max([x for x in (day_high, max(full_h)) if x])
    f["dayHigh"] = dh
    f["distFromHighPct"] = round((price / dh - 1) * 100, 3) if dh else None
    f["newHigh"] = bool(h[-1] >= max(h[:-1]) * 0.9999)                         # 直近足が窓内の高値を更新
    f["newHigh3"] = bool(max(h[-3:]) > max(h[:-3]) * 1.0)                      # 直近3本のどこかで高値更新
    tpv = sum(((hh + ll + cl) / 3.0) * vv for hh, ll, cl, vv in zip(b["highs"], b["lows"], b["closes"], b["volumes"]))
    tvv = sum(b["volumes"])
    vw = vwap if vwap else (tpv / tvv if tvv > 0 else None)
    f["aboveVwap"] = (price > vw) if vw else None
    f["vwapDistPct"] = round((price / vw - 1) * 100, 3) if vw else None
    return f


def _r2(x):
    return None if x is None else round(x, 2)


def evaluate_rolling(bars, quote=None, vwap=None, day_high=None, market_rs=None, sector_lead=False, sector_weak=None,
                     spread_pct=None, quote_speed_pct_per_min=None):
    """Rolling Radar評価。sector_weak=Noneはセクターの弱さが不明（確認要素としては数えない）。"""
    base = {"state": "NONE", "base_state": "NONE", "rolling_score": None, "reasons": [], "features": None,
            "confirmations": {"known": 0, "passed": 0, "failed": [], "detail": {}}, "hot": False, "watch": False,
            "entry_allowed": False}
    f = compute_rolling_features((bars), cur=(quote or {}).get("t"), vwap=vwap, day_high=day_high)
    if f is None:
        return dict(base, reasons=["5分足が5本未満（推測しない）"])
    rs, vs = f["rangeSurge"], f["volSurge"]
    up = f["upLast"]
    near_high = f["newHigh"] or (f["distFromHighPct"] is not None and f["distFromHighPct"] >= -0.5)
    last_surge = bool(rs is not None and vs is not None and rs >= SURGE_RANGE and vs >= SURGE_VOL and up and near_high)
    prev_quiet = not ((f["prevRangeSurge"] or 0) >= 1.5 and (f["prevVolSurge"] or 0) >= 1.5)
    dd = f["distFromHighPct"]
    if last_surge and prev_quiet:
        state = "SINGLE_BAR_SURGE"                                           # 静かな状態から1本だけ急伸
    elif ((f["range2"] or 0) >= SUSTAINED_RANGE and (f["vol2"] or 0) >= SUSTAINED_VOL and f["upLast2"]) or last_surge:
        state = "ROLLING_SURGE"                                              # 連続する急な活性化
    elif f["rangeSteps"] >= 2 and (f["vol2"] or 0) >= EXPANDING_VOL and up:
        state = "ROLLING_EXPANDING"                                          # 連続拡大型
    elif (dd is not None and PRE_BREAKOUT_DIST[0] <= dd <= PRE_BREAKOUT_DIST[1] and f["aboveVwap"] is True
          and (f["vol2"] or 0) >= PRE_BREAKOUT_VOL and up):
        state = "ROLLING_PRE_BREAKOUT"
    else:
        state = "NONE"

    # false positive抑制：確認要素（判定できたものだけを数える）
    detail = {"vwap": f["aboveVwap"], "recentHigh": bool(f["newHigh3"]),
              "marketRS": None if market_rs is None else market_rs > 0,
              "sector": None if sector_weak is None else (not sector_weak),
              "spread": None if spread_pct is None else spread_pct <= WIDE_SPREAD_PCT}
    known = {k: v for k, v in detail.items() if v is not None}
    failed = [k for k, v in known.items() if not v]
    conf = {"known": len(known), "passed": len(known) - len(failed), "failed": failed, "detail": detail}
    out_state = state
    if state != "NONE" and len(failed) >= WEAK_MISSING:
        out_state = "RADAR_WEAK"                                             # 確認要素が足りない：hot poolの優先度を下げる

    # スコア（0-100）：窓内の活性化の強さ＋確認要素
    parts = {"rangeSurge": _scale(rs, 1.0, 4.0, 22), "volSurge": _scale(vs, 1.0, 5.0, 28),
             "turnoverSurge": _scale(f["turnoverSurge"], 1.0, 5.0, 10), "newHigh": 10.0 if f["newHigh"] else 0.0,
             "nearHigh": 10.0 if (dd is not None and dd >= -0.8) else 0.0, "vwap": 5.0 if f["aboveVwap"] else 0.0,
             "confirmations": conf["passed"] * 3.0,
             "quoteSpeed": _scale(quote_speed_pct_per_min, 0.05, 0.3, 4)}
    score = max(0.0, min(100.0, sum(parts.values())))
    why = []
    if (vs or 0) >= 2:
        why.append(f"直近足の出来高が過去4本平均の{vs}倍")
    if (rs or 0) >= 1.5:
        why.append(f"直近足の値幅が過去4本平均の{rs}倍")
    if f["newHigh"]:
        why.append("直近足で高値更新")
    if f["aboveVwap"]:
        why.append("VWAP上")
    if failed:
        why.append("確認不足：" + "・".join(failed))
    return {"state": out_state, "base_state": state, "rolling_score": round(score) if state != "NONE" else round(score),
            "reasons": why, "breakdown": {k: round(v, 1) for k, v in parts.items()}, "features": f, "confirmations": conf,
            "hot": out_state in HOT_STATES, "watch": out_state in WATCH_STATES, "entry_allowed": False}


STATE_PRIORITY = {"SINGLE_BAR_SURGE": 5, "ROLLING_SURGE": 4, "ROLLING_PRE_BREAKOUT": 3, "ROLLING_EXPANDING": 2, "RADAR_WEAK": 1}


def build_rolling_radar_list(cands, top_n=5):
    """shadowMovement.rollingRadar：警戒レーダー（最大5銘柄）。hot状態を優先し、次にスコア順。買い判定ではない。"""
    rows = [c for c in cands if c.get("rollingState") in NOTABLE_STATES]
    rows.sort(key=lambda c: (-STATE_PRIORITY.get(c["rollingState"], 0), -(c.get("rollingScore") or 0)))
    return [{"code": c.get("code"), "name": c.get("name"), "current": c.get("current"), "changePct": c.get("changePct"),
             "rollingState": c["rollingState"], "baseState": c.get("rollingBaseState"), "rollingScore": c.get("rollingScore"),
             "reasons": (c.get("rollingReasons") or [])[:4], "confirmations": c.get("rollingConfirm"),
             "existingEntryState": c.get("chartEntryState") or c.get("entryState"), "movementScore": c.get("movementScore"),
             "activityState": c.get("activityState"), "entryAllowed": False, "rank": i + 1, "label": "📡 警戒レーダー"}
            for i, c in enumerate(rows[:top_n])]
