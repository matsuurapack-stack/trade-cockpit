# Movement Potential Engine（Phase D・shadow運用）：
#   「今日このあと値幅が出る可能性」を、5分足の値幅・出来高の“いま動いているか”から評価する純粋関数群
#   （DB・ネットワーク非依存、未来データを参照しない＝渡されたbarsだけを使う）。
#
#   movement_potential_score (0-100)  今日このあと値幅が出る可能性（stock_strength_scoreとは別軸）
#   recent_activity_score    (0-100)  直近30〜60分に今も動いているか（当日上昇率は見ない）
#   activity_state  EXPANDING / ACTIVE / COILING / LOW_ACTIVITY / FADING / UNKNOWN
#   pre_breakout    ブレイクの一段手前（高値0.3〜0.8%下・VWAP上・higher low・出来高増・値幅拡大…）
#   too_late        「強いが遅い」（15分急騰済み・連続陽線・VWAP乖離・上ヒゲ・出来高ピークアウト・RR不足）
#   momentum_mode   モメンタム候補（別ENTRYルール：逆指値必須）
#   recommended_stop / risk_reward
#
# しきい値はすべて暫定値（shadowで30〜50件以上集めてから調整する前提）。ここで得た値は、本番の既存
# ENTRY判定・TOP5選定を変えない（shadow比較用に並行保存する）。Chart Context Engine（Phase C）の
# 結果（pattern・features）は引数で受け取って再利用し、同じ特徴量を二重に計算しない。

import statistics

import chart_context as cc

MIN_BARS = 6                       # これ未満は UNKNOWN（推測しない）。6本=寄りから30分（9:30）で判定開始。信頼度は本数で下げる
EXPANDING_RANGE_RATIO = 1.5        # 直近3本の平均値幅 / 当日の通常値幅
EXPANDING_VOL_RATIO = 1.5
LOW_ACTIVITY_RANGE_RATIO = 0.6
PRE_BREAKOUT_DIST = (-0.8, -0.3)   # 当日高値まで0.3〜0.8%
HOT_SCORE = 75
ADD_SCORE = 65                     # dynamic watchへの自動追加
FADE_MIN_BARS_SINCE_HIGH = 9       # 高値から45分以上更新なし
FADE_DRAWDOWN_PCT = -1.5
RR_MIN = 1.5                       # 既存設定に固定のRR基準は無い（UIは表示のみ）。指示例の値を暫定採用
STOP_MIN_PCT = 0.3                 # これより浅い逆指値はノイズで刈られるため下限
STOP_ATR_MULT = 1.0                # 既存の損切り目安（ATR1倍）と同じ
MOMENTUM_FLAGS_NEEDED = 3


def _clamp(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, x))


def _scale(x, lo, hi, out):
    """xがlo→hiで 0→out に線形に増える（範囲外はクリップ）。xがNoneなら0。"""
    if x is None:
        return 0.0
    if hi == lo:
        return 0.0
    return _clamp((x - lo) / (hi - lo), 0.0, 1.0) * out


def _mean(v):
    v = [x for x in v if x is not None]
    return sum(v) / len(v) if v else None


def _bar_ranges_pct(h, l, c):
    return [((hi - lo) / cl * 100.0) if cl else 0.0 for hi, lo, cl in zip(h, l, c)]


def compute_movement_features(bars, cur=None, vwap=None, day_high=None, day_low=None):
    """値幅・出来高・高値更新の生の特徴量。bars不足ならNone。"""
    b = cc.normalize_bars(bars)
    if not b or len(b["closes"]) < MIN_BARS:
        return None
    o, h, l, c, v = b["opens"], b["highs"], b["lows"], b["closes"], b["volumes"]
    n = len(c)
    price = cur if cur is not None else c[-1]
    if price != c[-1]:      # 最新quoteで最終足を更新（足の数は増やさない）
        c = c[:-1] + [price]
        h = h[:-1] + [max(h[-1], price)]
        l = l[:-1] + [min(l[-1], price)]
    rng = _bar_ranges_pct(h, l, c)
    normal = statistics.median(rng[:-3]) if n >= 6 else None       # 直近3本を除く当日の通常値幅
    recent3 = _mean(rng[-3:])
    f = {"n": n, "price": price, "normalRangePct": round(normal, 3) if normal else None,
         "recent3RangePct": round(recent3, 3) if recent3 is not None else None}
    f["rangeRatio"] = round(recent3 / normal, 2) if (normal and recent3 is not None) else None
    f["rangeSteps"] = sum(1 for i in (-3, -2, -1) if rng[i] > rng[i - 1])       # 直近3本の値幅の連続拡大数
    f["range6Pct"] = round((max(h[-6:]) - min(l[-6:])) / price * 100, 3) if price else None
    f["range12Pct"] = round((max(h[-12:]) - min(l[-12:])) / price * 100, 3) if (price and n >= 12) else None
    f["range4Pct"] = round((max(h[-4:]) - min(l[-4:])) / price * 100, 3) if price else None
    # 出来高：直近3本 / その前6本
    base = _mean(v[max(0, n - 9):-3]) if n >= 6 else None      # 直近3本の前（最大6本）
    f["volRatioRecent"] = round(_mean(v[-3:]) / base, 2) if (base and base > 0) else None
    # 売買代金の増加（直近3本 / その前3本）
    tv = [cl * vv for cl, vv in zip(c, v)]
    prev_tv = sum(tv[-6:-3]) if n >= 6 else 0
    f["turnoverAccel"] = round(sum(tv[-3:]) / prev_tv, 2) if prev_tv > 0 else None
    dh = max(day_high or 0, max(h)) if (day_high or h) else None
    dl = min(day_low, min(l)) if day_low else min(l)
    f["dayHigh"], f["dayLow"] = dh, dl
    f["distFromHighPct"] = round((price / dh - 1) * 100, 3) if dh else None
    f["dayRangePct"] = round((dh - dl) / price * 100, 3) if (dh and dl and price) else None
    # 高値更新（直近12本で当日高値を更新した本数）
    run, cnt = -1e18, 0
    last_high_idx = 0
    for i, hh in enumerate(h):
        if hh >= run:
            if i >= n - 12 and hh > run and run > -1e17:
                cnt += 1
            run, last_high_idx = hh, i
    f["newHighCount12"] = cnt
    f["barsSinceHigh"] = n - 1 - last_high_idx
    f["higherLow"] = bool(n >= 6 and min(l[-3:]) > min(l[-6:-3]))
    f["lowerHighs"] = bool(n >= 9 and max(h[-3:]) < max(h[-6:-3]) < max(h[-9:-6]))
    f["lowerLows"] = bool(n >= 6 and min(l[-3:]) < min(l[-6:-3]))
    f["slope12"] = round((c[-1] / c[-13] - 1) * 100, 3) if n >= 13 else None
    f["aboveVwap"] = (price > vwap) if vwap else None
    # 値動きのボラティリティ拡大（close-to-close標準偏差：直近6本 / その前12本）
    ret = [(c[i] / c[i - 1] - 1) * 100 for i in range(1, n) if c[i - 1]]
    f["volatilityRatio"] = None
    if len(ret) >= 14:
        recent_sd = statistics.pstdev(ret[-6:])
        prior_sd = statistics.pstdev(ret[-18:-6])
        if prior_sd > 1e-9:
            f["volatilityRatio"] = round(recent_sd / prior_sd, 2)
    # 直近足の上ヒゲ
    hi_, lo_, op_, cl_ = h[-1], l[-1], o[-1], c[-1]
    r_ = max(hi_ - lo_, 1e-9)
    f["lastUpperWick"] = round((hi_ - max(op_, cl_)) / r_, 2) if (hi_ - lo_) / max(price, 1e-9) * 100 >= 0.12 else 0.0
    # ATR（5分足、直近14本の平均TR）
    trs = [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, n)]
    f["atr5"] = round(sum(trs[-14:]) / len(trs[-14:]), 4) if trs else None
    f["lows"], f["highs"] = l, h
    return f


def classify_activity(f, rel_volume=None):
    """ACTIVE / EXPANDING / COILING / LOW_ACTIVITY / FADING / UNKNOWN と、その理由。"""
    if f is None:
        return "UNKNOWN", ["5分足不足（推測しない）"]
    rr, vr = f["rangeRatio"], f["volRatioRecent"]
    dd = f["distFromHighPct"]
    faded = (f["barsSinceHigh"] >= FADE_MIN_BARS_SINCE_HIGH and dd is not None and dd <= FADE_DRAWDOWN_PCT)
    vol_up = (vr is not None and vr >= EXPANDING_VOL_RATIO) or (rel_volume is not None and rel_volume >= EXPANDING_VOL_RATIO)
    # 今まさに値幅・出来高が拡大しているなら、朝の高値からの距離だけでFADING扱いしない（午後の再加速を拾う）。
    # FADINGは「今も動いていない」ことが条件（当日の高値からの位置だけでは判定しない）。
    if rr is not None and rr >= EXPANDING_RANGE_RATIO and f["rangeSteps"] >= 2 and vol_up:
        return "EXPANDING", [f"5分足値幅が通常の{rr}倍に拡大（{f['rangeSteps']}本連続）", f"出来高{vr or rel_volume}倍"]
    active_now = bool(rr is not None and rr >= 1.2 and vol_up)      # 値幅・出来高が今も通常より大きい
    if (faded and not active_now) or (f["lowerHighs"] and f["aboveVwap"] is False and (vr is not None and vr < 0.8)):
        return "FADING", [f"高値から{f['barsSinceHigh']}本更新なし・{dd}%下落" if faded else "高値切り下げ・VWAP下・出来高減"]
    if (rr is not None and rr < LOW_ACTIVITY_RANGE_RATIO) or (f["range6Pct"] is not None and f["range6Pct"] < 0.35):
        return "LOW_ACTIVITY", [f"直近値幅が通常の{rr}倍／30分レンジ{f['range6Pct']}%と小さい"]
    if rr is not None and rr < 0.9 and dd is not None and dd >= -1.0 and f["higherLow"]:
        return "COILING", ["高値圏で値幅が収束（higher low）"]
    return "ACTIVE", []


def movement_score(f, activity, rel_volume=None, market_rs=None, sector_lead=False):
    """movement_potential_score（0-100）と内訳。加点（値幅拡大・出来高・高値接近…）と減点（収縮・出来高減・失速…）。"""
    if f is None:
        return None, {}, ["5分足不足のため算出しない"]
    parts, why = {}, []

    def add(name, val, note=None):
        parts[name] = round(val, 1)
        if val and note:
            why.append(note)
    rr, vr = f["rangeRatio"], f["volRatioRecent"]
    dd = f["distFromHighPct"]
    add("rangeExpansion", _scale(rr, 1.0, 2.5, 15), f"5分足値幅が通常の{rr}倍" if (rr or 0) >= 1.3 else None)
    add("rangeSteps", {0: 0, 1: 1.5, 2: 3.5, 3: 5}[f["rangeSteps"]])
    add("volumeSurge", _scale(vr, 1.0, 3.0, 15), f"直近出来高{vr}倍" if (vr or 0) >= 1.5 else None)
    add("relativeVolume", _scale(rel_volume, 1.0, 3.0, 10), f"相対出来高{rel_volume}倍" if (rel_volume or 0) >= 1.5 else None)
    add("turnoverAccel", _scale(f["turnoverAccel"], 1.0, 3.0, 6))
    near = 0.0
    if dd is not None:
        near = 10.0 if dd >= -0.8 else _scale(-dd, 3.0, 0.8, 10)          # 0.8%以内=満点、3%で0点
    add("nearDayHigh", near, f"当日高値まで{abs(dd)}%" if (dd is not None and dd >= -0.8) else None)
    add("baseNearHigh", 5.0 if (dd is not None and dd >= -1.0 and f["range4Pct"] is not None and f["range4Pct"] <= 0.8) else 0.0,
        "高値圏でBASE形成" if (dd is not None and dd >= -1.0 and f["range4Pct"] is not None and f["range4Pct"] <= 0.8) else None)
    add("aboveVwap", 5.0 if f["aboveVwap"] else 0.0)
    add("sectorLeader", 5.0 if sector_lead else 0.0, "セクター先導" if sector_lead else None)
    add("marketRS", _scale(market_rs, 0.0, 3.0, 6))
    add("newHighs", _scale(f["newHighCount12"], 0, 3, 6), f"高値更新{f['newHighCount12']}回" if f["newHighCount12"] >= 2 else None)
    add("volatilityExpansion", _scale(f["volatilityRatio"], 1.0, 2.0, 8))
    pen = {}

    def sub(name, val, note):
        pen[name] = -val
        why.append(note)
    if rr is not None and rr < 0.6:
        sub("rangeContraction", 12, f"直近の値幅が通常の{rr}倍に縮小")
    if vr is not None and vr < 0.7:
        sub("volumeDecline", 8, f"出来高減少（{vr}倍）")
    if f["range6Pct"] is not None and f["range6Pct"] < 0.5:
        sub("flat", 10, f"30分レンジ{f['range6Pct']}%の横ばい")
    if f["lowerHighs"]:
        sub("lowerHighs", 8, "高値切り下げ")
    if f["aboveVwap"] is False:
        sub("belowVwap", 8, "VWAP下")
    if activity == "FADING":
        sub("fading", 15, "朝に動いた後に失速（高値から更新なし）")
    if f["slope12"] is not None and f["slope12"] <= -1.0 and f["lowerLows"]:
        sub("continuousDecline", 10, "高値から継続的に下落")
    if f["range12Pct"] is not None and f["range12Pct"] < 0.8 and f["barsSinceHigh"] >= 12:
        sub("longSideways", 8, "上昇後の長時間の横横")
    total = _clamp(sum(parts.values()) + sum(pen.values()))
    return round(total), {**parts, **pen}, why


def recent_activity_score(f, rel_volume=None):
    """直近30〜60分に今も動いているか（当日上昇率は一切見ない）。"""
    if f is None:
        return None
    s = _scale(f["rangeRatio"], 0.6, 2.0, 40) + _scale(f["volRatioRecent"] if f["volRatioRecent"] is not None else rel_volume, 0.7, 2.5, 30)
    s += _scale(f["newHighCount12"], 0, 2, 15)
    s += _scale(f["range6Pct"], 0.3, 1.5, 15)
    return round(_clamp(s))


def detect_pre_breakout(f, chart, too_late):
    """ブレイクの一段前。必須（高値0.3〜0.8%下・VWAP上・上ヒゲ失速なし）＋任意4条件のうち2つ以上。"""
    if f is None or too_late:
        return False, []
    dd = f["distFromHighPct"]
    need = [dd is not None and PRE_BREAKOUT_DIST[0] <= dd <= PRE_BREAKOUT_DIST[1],
            f["aboveVwap"] is True,
            f["lastUpperWick"] < cc.UPPER_WICK_LONG]
    if not all(need):
        return False, []
    opt, notes = 0, []
    for ok, note in ((f["higherLow"], "higher low"),
                     ((f["volRatioRecent"] or 0) >= 1.2, f"出来高増（{f['volRatioRecent']}倍）"),
                     ((f["rangeRatio"] or 0) >= 1.2 or f["rangeSteps"] >= 2, "値幅拡大"),
                     (f["range4Pct"] is not None and f["range4Pct"] <= 0.8, "高値圏でBASE形成")):
        if ok:
            opt += 1
            notes.append(note)
    if opt < 2:
        return False, []
    if chart and chart.get("pattern") in ("FAILED_BREAKOUT", "VWAP_LOSS"):
        return False, []
    return True, [f"当日高値まで{abs(dd)}%", "VWAP上", "上ヒゲ失速なし"] + notes


def detect_too_late(chart, rr_ok=True, rr=None):
    """「強いが遅い」。Phase Cの結果（pattern・features）を再利用する。
    悪いパターン（CHASE/EXTENDED/EXHAUSTION/FAILED_BREAKOUT）、または遅れのフラグが2つ以上、またはRR不足。"""
    if not chart:
        return False, []
    ft = chart.get("features") or {}
    flags = []
    if (ft.get("chg15m") or 0) >= cc.FAST_15M_PCT:
        flags.append(f"15分{ft['chg15m']:+.1f}%")
    if (ft.get("consecGreen") or 0) >= 3:
        flags.append(f"{ft['consecGreen']}本連続陽線")
    if (ft.get("vwapDistPct") or 0) >= cc.VWAP_DIST_BIG_PCT:
        flags.append(f"VWAP{ft['vwapDistPct']:+.1f}%")
    dh = ft.get("distFromDayHighPct")
    if dh is not None and dh >= -0.15 and (ft.get("chg15m") or 0) >= 0.8:
        flags.append("高値からほぼ乖離なし")
    if (ft.get("upperWick") or 0) >= cc.UPPER_WICK_LONG:
        flags.append("上ヒゲ出現")
    if ft.get("volPeakout"):
        flags.append("出来高ピークアウト")
    reasons = list(flags)
    late = len(flags) >= 2 or chart.get("pattern") in ("CHASE", "EXTENDED", "EXHAUSTION")
    if chart.get("pattern") in ("CHASE", "EXTENDED", "EXHAUSTION"):
        reasons.insert(0, f"チャート{chart['pattern']}")
    if not rr_ok:
        late = True
        reasons.append(f"RR不足（{rr}<{RR_MIN}）")
    return late, reasons


def momentum_flags(f, rel_volume=None, market_rs=None, chg15m=None):
    if f is None:
        return []
    out = []
    if (f["volRatioRecent"] or 0) >= 2.0 or (rel_volume or 0) >= 2.0:
        out.append("出来高急増")
    if (f["rangeRatio"] or 0) >= EXPANDING_RANGE_RATIO:
        out.append("range expansion")
    if (market_rs or 0) >= 2.0:
        out.append("high RS")
    if f["newHighCount12"] >= 2:
        out.append("高値更新頻度高")
    if abs(chg15m or 0) >= 1.0:
        out.append("値動きが速い")
    return out


def recommended_stop(f, chart, pattern_hint=None):
    """デイトレENTRY用の短い逆指値（固定-8%ではない）。チャート構造に合う候補を選ぶ：
    ブレイク系→ブレイク水準の下 / 押し目・VWAP系→直近安値かVWAPの下 / いずれも取れなければATR。
    最低幅（STOP_MIN_PCT）と、ATR倍数を超える深さの制限を掛ける。戻り値は根拠と代替候補つき。"""
    if f is None:
        return None
    price = f["price"]
    ft = (chart or {}).get("features") or {}
    pattern = pattern_hint or (chart or {}).get("pattern")
    lows = f["lows"]
    cands = {}
    bl = ft.get("breakoutLevel")
    if bl and bl < price:
        cands["BREAKOUT_LEVEL"] = bl * 0.999
    cands["RECENT_5M_LOW"] = min(lows[-2:]) * 0.999
    swing = min(lows[-4:]) * 0.999
    cands["SWING_LOW"] = swing
    if ft.get("vwapDistPct") is not None:
        vw = price / (1 + ft["vwapDistPct"] / 100.0)
        if vw < price:
            cands["VWAP"] = vw * 0.998
    atr = f.get("atr5")
    if atr:
        cands["ATR"] = price - atr * STOP_ATR_MULT * 1.5
    order = {"EARLY_BREAKOUT": ["BREAKOUT_LEVEL", "RECENT_5M_LOW"], "BREAKOUT_CONFIRMED": ["BREAKOUT_LEVEL", "RECENT_5M_LOW"],
             "PRE_BREAKOUT": ["RECENT_5M_LOW", "BREAKOUT_LEVEL"], "PULLBACK_READY": ["SWING_LOW", "VWAP"],
             "VWAP_RECLAIM": ["VWAP", "RECENT_5M_LOW"]}.get(pattern, ["RECENT_5M_LOW", "VWAP"]) + ["ATR"]
    chosen = next((k for k in order if k in cands and 0 < (price - cands[k]) / price * 100), None)
    if not chosen:
        return None
    stop = cands[chosen]
    dist = (price - stop) / price * 100.0
    # 浅すぎる逆指値は5分足1本分のノイズで刈られる。下限＝固定の最小幅と「5分足ATR×1（既存の損切り目安と同じ倍率）」の大きい方。
    min_pct = max(STOP_MIN_PCT, (atr / price * 100.0 * STOP_ATR_MULT) if (atr and price) else 0.0)
    if dist < min_pct:
        stop, chosen = price * (1 - min_pct / 100.0), chosen + "+MIN"
        dist = min_pct
    if atr and (price - stop) > atr * 4:    # 深すぎ→ATRベースへ
        stop, chosen = price - atr * 1.5, "ATR"
        dist = (price - stop) / price * 100.0
    return {"price": round(stop, 1), "distancePct": round(dist, 2), "method": chosen,
            "alternatives": {k: round(v, 1) for k, v in cands.items() if v < price}}


def risk_reward(f, stop):
    """RR = ターゲットまで / 逆指値まで。ターゲット＝ 当日値幅の60% と 通常5分足値幅の3本分 の大きい方（暫定）。"""
    if f is None or not stop:
        return None
    price = f["price"]
    risk = price - stop["price"]
    if risk <= 0:
        return None
    reward_pct = max((f["dayRangePct"] or 0) * 0.6, (f["normalRangePct"] or 0) * 3)
    reward = price * reward_pct / 100.0
    return {"rr": round(reward / risk, 2), "targetPrice": round(price + reward, 1), "targetPct": round(reward_pct, 2)}


def evaluate_movement(bars, quote=None, vwap=None, day_high=None, day_low=None, rel_volume=None, market_rs=None,
                      sector_lead=False, chart=None, minutes_since_open=None):
    """Movement Potentialの全体評価。chartはPhase Cのevaluate_chart_context結果（必須ではないが渡すと再利用する）。"""
    cur = (quote or {}).get("t")
    f = compute_movement_features(bars, cur=cur, vwap=vwap, day_high=day_high, day_low=day_low)
    if f is None:
        return {"movement_potential_score": None, "recent_activity_score": None, "activity_state": "UNKNOWN",
                "pre_breakout": False, "too_late": False, "too_late_reasons": [], "momentum_mode": False,
                "momentum_flags": [], "recommended_stop": None, "risk_reward": None, "reasons": ["5分足不足（推測しない）"],
                "breakdown": {}, "confidence": "UNKNOWN", "features": None}
    activity, act_why = classify_activity(f, rel_volume)
    score, breakdown, why = movement_score(f, activity, rel_volume, market_rs, sector_lead)
    ft = (chart or {}).get("features") or {}
    flags = momentum_flags(f, rel_volume, market_rs, ft.get("chg15m"))
    mode = len(flags) >= MOMENTUM_FLAGS_NEEDED
    late0, late_why = detect_too_late(chart)
    pre, pre_why = detect_pre_breakout(f, chart, late0)
    hint = "PRE_BREAKOUT" if pre else None
    stop = recommended_stop(f, chart, hint)
    rr = risk_reward(f, stop)
    rr_ok = True if (rr is None or not mode) else rr["rr"] >= RR_MIN     # RR基準はモメンタム銘柄に必須（他は表示のみ）
    late, late_why = detect_too_late(chart, rr_ok, rr["rr"] if rr else None)
    if late and pre:
        pre, pre_why = False, []
    conf = cc.confidence_for(f["n"], minutes_since_open)
    return {"movement_potential_score": score, "recent_activity_score": recent_activity_score(f, rel_volume),
            "activity_state": activity, "activity_reasons": act_why, "pre_breakout": pre, "pre_breakout_reasons": pre_why,
            "too_late": late, "too_late_reasons": late_why, "momentum_mode": mode, "momentum_flags": flags,
            "recommended_stop": stop, "risk_reward": rr, "reasons": why, "breakdown": breakdown, "confidence": conf,
            "features": {k: v for k, v in f.items() if k not in ("lows", "highs")}}


def movement_recommendation(legacy_state, chart_state, mv, chart):
    """shadow：既存の推奨（chart_state）に対する『値幅を見た推奨』。既存判定は変えず並行保存する。
      ENTRY_READY   既存もENTRY可で、遅くない・動いている・（モメンタムなら）RR成立
      TOO_LATE      既存はENTRY可でも遅い／RR不足 → 押し目待ち
      BLOCKED_LOW_ACTIVITY 既存はENTRY可でも動いていない（LOW_ACTIVITY/FADING）
      PRE_BREAKOUT / WATCH_EXPANDING  まだENTRYではない最重要監視
      NONE"""
    if not mv or mv.get("activity_state") == "UNKNOWN":
        return "NONE"
    chart_ok = chart_state in ("NOW_BUYABLE", "ENTRY_READY")
    legacy_ok = legacy_state in ("NOW_BUYABLE", "ENTRY_READY")     # Phase C補正前＝旧判定でENTRY可だったもの
    bad_pattern = (chart or {}).get("pattern") in ("CHASE", "EXTENDED", "FAILED_BREAKOUT", "EXHAUSTION")
    promo = bool(mv.get("pre_breakout") and (chart or {}).get("pattern") == "EARLY_BREAKOUT"
                 and (mv.get("recent_activity_score") or 0) >= 50)   # PRE_BREAKOUT→出来高付きブレイク→ENTRY
    if chart_ok or legacy_ok or promo:
        if mv.get("too_late") or bad_pattern:
            return "TOO_LATE"
        if chart_ok or promo:
            if mv.get("activity_state") in ("LOW_ACTIVITY", "FADING"):
                return "BLOCKED_LOW_ACTIVITY"
            return "ENTRY_READY"
    if mv.get("pre_breakout"):
        return "PRE_BREAKOUT"
    if mv.get("activity_state") == "EXPANDING":
        return "WATCH_EXPANDING"
    return "NONE"


def attention_rank_score(mv):
    """注目（今日これから値幅が出そう）用の並べ替えスコア。EXPANDING / PRE_BREAKOUT を優遇し、
    LOW_ACTIVITY / FADING は落とす。当日上昇率は使わない。"""
    if not mv or mv.get("movement_potential_score") is None:
        return None
    s = 0.7 * mv["movement_potential_score"] + 0.3 * (mv.get("recent_activity_score") or 0)
    if mv["activity_state"] == "EXPANDING":
        s += 25        # 値幅が拡大し始めた銘柄を最重要視（すでに急騰した銘柄より上に置く）
    if mv.get("pre_breakout"):
        s += 30        # ブレイクの一段手前は最重要監視
    if mv["activity_state"] in ("LOW_ACTIVITY", "FADING"):
        s -= 30
    return round(s, 1)


# ---------------------------------------------------------------- shadow lists（既存TOP5は変えず並行保存）
def entry_reason_line(c):
    """買い時カード用の「なぜ今なのか」1行。候補dict（server側の候補）から、成立している事実だけをつなぐ。"""
    mf = c.get("movementFeatures") or {}
    pattern = c.get("chartPattern")
    vol = mf.get("volRatioRecent")
    parts = []
    if pattern == "PULLBACK_READY":
        parts = ["VWAP押し目" if mf.get("aboveVwap") else "押し目", "higher low" if mf.get("higherLow") else None, "再上昇"]
    elif pattern in ("EARLY_BREAKOUT", "BREAKOUT_CONFIRMED"):
        parts = ["ブレイク接近" if c.get("preBreakout") else None, "高値ブレイク",
                 f"出来高{vol}倍" if (vol or 0) >= 1.3 else None, "高値更新" if (mf.get("newHighCount12") or 0) >= 1 else None]
    elif pattern == "VWAP_RECLAIM":
        parts = ["VWAP奪回", f"出来高{vol}倍" if (vol or 0) >= 1.3 else None, "higher low" if mf.get("higherLow") else None]
    line = " → ".join(p for p in parts if p)
    if not line:
        rs = (c.get("chartContext") or {}).get("reasons") or c.get("movementReasons") or []
        line = " → ".join(rs[:2])
    return line


def _compact(c, rank=None):
    st = c.get("recommendedStop")
    return {"code": c.get("code"), "name": c.get("name"), "current": c.get("current"), "changePct": c.get("changePct"),
            "movementScore": c.get("movementScore"), "recentActivityScore": c.get("recentActivityScore"),
            "activityState": c.get("activityState"), "preBreakout": c.get("preBreakout"), "tooLate": c.get("tooLate"),
            "tooLateReasons": c.get("tooLateReasons"), "momentumMode": c.get("momentumMode"),
            "recommendedStop": st, "riskReward": c.get("riskReward"), "movementRecommendation": c.get("movementRecommendation"),
            "existingRecommendation": c.get("chartEntryState") or c.get("entryState"),
            "legacyEntryState": c.get("legacyEntryState"), "chartPattern": c.get("chartPattern"),
            "entryTimingScore": c.get("entryTimingScore"), "rank": rank, "entryReason": entry_reason_line(c)}


def build_shadow_lists(cands, existing_analysis_codes=(), hot_codes=(), top_n=5):
    """既存の推奨（analysisTop5/actionableTop5）とは別に、movement-awareな並びを作る（shadow）。
      attentionTop5  今日これから値幅が出そうな順（LOW_ACTIVITY/FADING/UNKNOWNは除外）
      entryBoard     既存もENTRY可で、値幅を見ても成立（遅くない・動いている・RR成立）
      tooLate        既存はENTRY可でも「強いが遅い／RR不足／動いていない」で外したもの
      preBreakout / expanding  最重要監視（まだENTRYではない）"""
    scored = []
    for c in cands:
        mv = {"movement_potential_score": c.get("movementScore"), "recent_activity_score": c.get("recentActivityScore"),
              "activity_state": c.get("activityState"), "pre_breakout": c.get("preBreakout")}
        r = attention_rank_score(mv)
        if r is not None:
            scored.append((r, c))
    scored.sort(key=lambda x: -x[0])
    attention = [(r, c) for r, c in scored if c.get("activityState") not in ("LOW_ACTIVITY", "FADING", "UNKNOWN")][:top_n]
    existing = list(existing_analysis_codes)
    out = {"attentionTop5": [], "entryBoard": [], "tooLate": [], "preBreakout": [], "expanding": [],
           "hotPool": list(hot_codes)}
    for r, c in attention:
        d = _compact(c, r)
        d["existingRank"] = (existing.index(c["code"]) + 1) if c.get("code") in existing else None
        out["attentionTop5"].append(d)
    board = [c for c in cands if c.get("movementRecommendation") == "ENTRY_READY"]
    board.sort(key=lambda c: (-(c.get("entryTimingScore") or 0), -(c.get("movementScore") or 0)))
    out["entryBoard"] = [_compact(c, i + 1) for i, c in enumerate(board[:top_n])]
    out["tooLate"] = [_compact(c) for c in cands if c.get("movementRecommendation") in ("TOO_LATE", "BLOCKED_LOW_ACTIVITY")][:10]
    out["preBreakout"] = [_compact(c) for c in cands if c.get("movementRecommendation") == "PRE_BREAKOUT"][:top_n]
    out["expanding"] = [_compact(c) for c in cands if c.get("movementRecommendation") == "WATCH_EXPANDING"][:top_n]
    return out
