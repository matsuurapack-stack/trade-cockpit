# Phase G：Technical Fusion Engine（shadow専用）。既存のテクニカル判定を7グループへ整理・統合し、
# 「何グループが同じ方向を支持しているか」（technical_confluence_score）で見る。本番ENTRY/TOP5の
# 判定は一切変更しない（結果は並行保存するだけ）。
#
# 再利用（再実装しない）：
#   chart_context   … 5分足の形（pattern/entry_timing_score/features: higherHighs・breakoutLevel・volRatio等）と
#                     `_candle`（body/wick比）・`normalize_bars`
#   movement_potential … activity_state・pre_breakout・too_late・recommended_stop・atr5
# このモジュールが新規に持つもの（既存に無いと棚卸しで確認済みのもの）：
#   ローソク足の組み合わせ（包み足・三兵等）、スイング点ベースのS/R、ブレイク種別（PROBE/リテスト/出来高別）、
#   チャートパターン（二重天井/底・三山・H&S・三角・フラッグ・ボックス）、日足MAコンテキスト
#   （GC/DC・LATE_GOLDEN_CROSS・ダマシ・グランビル）、一目均衡表、ダイバージェンス、価格×出来高マトリクス、
#   グループ集約のconfluence。日足/5分足の生データは呼び出し側（server.py）から渡される。
#
# 設計原則：同一グループの似た指標は「代表signal1つ」に集約し、根拠を重複加点しない。データが無い項目は
# UNKNOWN/未算出のままにし推測しない。遅行signal（GC・MA整列・RSI・一目）は「今買う」根拠にしない
# （すでにCHASE/EXTENDEDならTECHNICALLY_STRONG_BUT_LATE）。

import chart_context as cc

VERSION = "fusion-1"
GROUPS = ("trend", "price_action", "support_resistance", "pattern", "momentum", "volume", "market_context")
# Tier1（デイトレ最優先）を重く、Tier2/3を軽く。同一グループは代表signalの強さ（0〜1）だけを使う。
GROUP_WEIGHTS = {"trend": 1.0, "support_resistance": 1.0, "volume": 1.0, "price_action": 0.8, "pattern": 0.7,
                 "momentum": 0.6, "market_context": 0.6}
HIGH_CONFLUENCE, MID_CONFLUENCE = 70, 45
MIN_KNOWN_GROUPS = 4              # これ未満は信頼度LOW（スコアを上限で抑える）
UNSUPPORTED = ["SAUCER_TOP", "SAUCER_BOTTOM", "PENNANT", "三空", "三法", "MACD", "volume_by_price(日足)", "market_breadth",
               "seasonality(context_only)"]

BULL, BEAR, NEUTRAL, UNKNOWN = "BULLISH", "BEARISH", "NEUTRAL", "UNKNOWN"


# ------------------------------------------------------------------ 基本ヘルパー

def _pct(a, b):
    return None if (a is None or not b) else (a / b - 1.0) * 100.0


def _sma_list(vals, n):
    """日足MA用の最小ヘルパー（server._smaはserver.pyにあり循環importになるため本モジュールでは持たない）。"""
    if len(vals) < n:
        return []
    out, s = [], sum(vals[:n])
    out.append(s / n)
    for i in range(n, len(vals)):
        s += vals[i] - vals[i - n]
        out.append(s / n)
    return out


def swings(highs, lows, k=2):
    """フラクタルのスイング高値/安値のindex。確定に前後k本を要するため、末尾k本は未確定として含めない
    （未来データを使わない）。"""
    n = len(highs)
    sh, sl = [], []
    for i in range(k, n - k):
        if highs[i] > max(highs[i - k:i]) and highs[i] >= max(highs[i + 1:i + k + 1]):
            sh.append(i)
        if lows[i] < min(lows[i - k:i]) and lows[i] <= min(lows[i + 1:i + k + 1]):
            sl.append(i)
    return sh, sl


def _group(state=UNKNOWN, strength=0.0, signals=None, reasons=None, extra=None):
    g = {"state": state, "strength": round(max(0.0, min(1.0, strength)), 2), "signals": list(signals or []),
         "reasons": list(reasons or [])}
    if extra:
        g.update(extra)
    return g


# ------------------------------------------------------------------ 2. ローソク足（Price Action）

def candle_signals(b):
    """OHLC（normalize_bars済みdict）から最新足周辺のローソク足signalを返す。body/wick比は
    chart_context._candleを再利用する。opensが近似（opens_approx）の場合は形の判定を控える。"""
    if not b or len(b["closes"]) < 2 or b.get("opens_approx"):
        return []
    o, h, l, c = b["opens"], b["highs"], b["lows"], b["closes"]
    n = len(c)
    cs = [cc._candle(o[i], h[i], l[i], c[i]) for i in range(n)]
    last, prev = cs[-1], cs[-2]
    sig = []
    if last["bull"] and last["body"] >= 0.65:
        sig.append("LARGE_BULL_BODY")
    if last["bear"] and last["body"] >= 0.65:
        sig.append("LARGE_BEAR_BODY")
    if last["upper"] >= cc.UPPER_WICK_LONG:
        sig.append("LONG_UPPER_WICK")
    if last["lower"] >= 0.35:
        sig.append("LONG_LOWER_WICK")
    if last["doji"]:
        sig.append("DOJI")
    elif last["body"] < 0.3:
        sig.append("SMALL_BODY")
    # 包み足：前足と逆向きで、今足の実体が前足の実体を完全に包む
    if prev["bear"] and last["bull"] and c[-1] >= o[-2] and o[-1] <= c[-2] and abs(c[-1] - o[-1]) > abs(c[-2] - o[-2]):
        sig.append("BULLISH_ENGULFING")
    if prev["bull"] and last["bear"] and c[-1] <= o[-2] and o[-1] >= c[-2] and abs(c[-1] - o[-1]) > abs(c[-2] - o[-2]):
        sig.append("BEARISH_ENGULFING")
    # 酒田五法のうち既存パターンと重複しない三兵（陽線/陰線が3本連続で終値切り上げ/切り下げ）
    if n >= 3 and all(cs[i]["bull"] and cs[i]["body"] >= 0.5 for i in (-3, -2, -1)) and c[-3] < c[-2] < c[-1]:
        sig.append("THREE_WHITE_SOLDIERS")
    if n >= 3 and all(cs[i]["bear"] and cs[i]["body"] >= 0.5 for i in (-3, -2, -1)) and c[-3] > c[-2] > c[-1]:
        sig.append("THREE_BLACK_CROWS")
    return sig


# ------------------------------------------------------------------ 3. Trend

def trend_structure(b, features, daily_bias=None):
    """5分足のトレンド構造。既存Chart Contextのfeatures（higherHighs/higherLows/slope）を優先し、
    スイング点は補助（TREND_WEAKENING/TRANSITIONの検出）にだけ使う。"""
    ft = features or {}
    hh, hl, lh, ll = ft.get("higherHighs"), ft.get("higherLows"), ft.get("lowerHighs"), ft.get("lowerLows")
    slope = ft.get("slope6") if ft.get("slope6") is not None else ft.get("slope3")
    aboveV = ft.get("aboveVwap")
    state, why = "UNKNOWN", []
    if hh is not None:
        if hh and hl:
            state = "UPTREND"
        elif lh and ll:
            state = "DOWNTREND"
        elif (hh and ll) or (lh and hl):
            state = "TREND_TRANSITION"
        else:
            state = "RANGE"
    weak = False
    if b and len(b["closes"]) >= 10:
        sh, sl = swings(b["highs"], b["lows"], 2)
        if len(sh) >= 2 and len(sl) >= 2:
            new_h, old_h = b["highs"][sh[-1]], b["highs"][sh[-2]]
            new_l, old_l = b["lows"][sl[-1]], b["lows"][sl[-2]]
            if new_l > old_l and new_h > old_h:
                if state in ("RANGE", "UNKNOWN"):
                    state = "UPTREND"
            elif new_l < old_l and new_h < old_h:
                if state in ("RANGE", "UNKNOWN"):
                    state = "DOWNTREND"
            elif new_l > old_l and new_h <= old_h and state == "UPTREND":
                weak = True
                why.append("高値を更新できず（安値は切り上げ）")
            if state == "UPTREND" and new_l <= old_l:
                weak = True
                why.append("直近安値を切り下げ")
    if state == "UPTREND" and (weak or (slope is not None and slope < 0)):
        state = "TREND_WEAKENING"
    d = {"UPTREND": (BULL, 0.5 + (0.2 if aboveV else 0) + (0.2 if (slope or 0) >= 0.5 else 0)),
         "DOWNTREND": (BEAR, 0.7), "TREND_TRANSITION": (NEUTRAL, 0.3), "TREND_WEAKENING": (NEUTRAL, 0.4),
         "RANGE": (NEUTRAL, 0.2), "UNKNOWN": (UNKNOWN, 0.0)}
    gs, st = d[state]
    if daily_bias == BULL and gs == BULL:
        st += 0.1                    # 日足の追い風は微加点まで（5分足のENTRYを日足で決めない）
    if daily_bias == BEAR and gs == BULL:
        st -= 0.2
        why.append("日足は下向き（大局に逆行）")
    if state == "UPTREND":
        why.append("高値・安値の切り上げ" + ("（VWAP上）" if aboveV else ""))
    elif state == "DOWNTREND":
        why.append("高値・安値の切り下げ")
    return _group(gs, st, [state], why, {"trendState": state})


# ------------------------------------------------------------------ 4. Support / Resistance

def sr_levels(b, cur, vwap=None, day_high=None, day_low=None, daily=None, tol=0.0015):
    """S/R候補をtouch_count・last_touch付きで返す。preview: 前日高安・日中高安・VWAP・直近スイング・日足MA。"""
    if not b or cur is None:
        return []
    h, l, c = b["highs"], b["lows"], b["closes"]
    n = len(c)
    cands = []

    def add(kind, price):
        if price:
            cands.append((kind, float(price)))
    if daily and daily.get("highs") and len(daily["highs"]) >= 2:
        add("prev_high", daily["highs"][-2])
        add("prev_low", daily["lows"][-2])
    add("day_high", day_high if day_high else max(h))
    add("day_low", day_low if day_low else min(l))
    add("vwap", vwap)
    sh, sl = swings(h, l, 2)
    for i in sh[-2:]:
        add("swing_high", h[i])
    for i in sl[-2:]:
        add("swing_low", l[i])
    if daily and daily.get("closes") and len(daily["closes"]) >= 26:
        m25 = _sma_list(daily["closes"], 25)
        if m25:
            add("ma25", m25[-1])
    out = []
    for kind, p in cands:
        if abs(p - cur) / cur < 0.0005:       # 現在値そのものは支持/抵抗として扱わない
            continue
        typ = "support" if p < cur else "resistance"
        touches, last_ago, prev_i = 0, None, -10
        for i in range(n):
            ext = l[i] if typ == "support" else h[i]
            if abs(ext - p) / p <= tol:
                if i - prev_i >= 2:          # 連続する足が同じ線に張り付いていても1回のtouchとして数える
                    touches += 1
                prev_i = i
                last_ago = n - 1 - i
        base = {"prev_high": 0.35, "prev_low": 0.35, "day_high": 0.3, "day_low": 0.3, "vwap": 0.3, "swing_high": 0.25,
                "swing_low": 0.25, "ma25": 0.25}[kind]
        strength = round(min(1.0, base + 0.2 * touches), 2)
        out.append({"kind": kind, "price": round(p, 2), "type": typ, "touch_count": touches,
                    "last_touch_bars_ago": last_ago, "support_strength": strength if typ == "support" else None,
                    "resistance_strength": strength if typ == "resistance" else None,
                    "distancePct": round((p - cur) / cur * 100, 2)})
    return sorted(out, key=lambda x: abs(x["distancePct"]))


def breakout_state(b, features):
    """線を抜けただけで判断しない。close確定・出来高・次足維持・リテスト（旧抵抗が支持へ）で分類する。"""
    ft = features or {}
    lvl = ft.get("breakoutLevel")
    if not b or not lvl or len(b["closes"]) < 4:
        return None, []
    c, h, l = b["closes"], b["highs"], b["lows"]
    vr = ft.get("breakoutVolRatio")
    if ft.get("failedBreakout"):
        return "FAILED_BREAKOUT", ["抜けたが戻された"]
    if ft.get("brokeRecently") and ft.get("breakoutHeld"):
        # 抜けた後の足で旧抵抗まで押し、支持として機能して引けた＝リテスト
        ago = ft.get("brokeBarsAgo") or 0
        if ago >= 2 and min(l[-3:]) <= lvl * 1.002 and c[-1] > lvl:
            return "BREAKOUT_RETEST", [f"旧抵抗{lvl:,.1f}が支持に転換（リテスト維持）"]
        if vr is not None and vr >= 1.5:
            return "STRONG_VOLUME_BREAKOUT", [f"出来高{vr:.1f}倍を伴う抜け＋次足維持"]
        if vr is not None and vr < 1.2:
            return "WEAK_BREAKOUT", [f"抜けたが出来高{vr:.1f}倍で弱い"]
        return "BREAKOUT_HELD", ["抜けて維持（出来高は判定材料不足）"]
    if h[-1] > lvl * 1.0003 and c[-1] <= lvl:
        return "BREAKOUT_PROBE", ["一瞬だけ抜けたが終値は線の下（確定していない）"]
    return None, []


# ------------------------------------------------------------------ 6/7. Chart Pattern

def detect_patterns(b, breakout_vol_ratio=None, min_bars=12):
    """スイング点ベースの形状認識（shadow）。無理に認識せず、条件が揃わなければ何も返さない。各パターンに
    pattern_confidence（0〜1）を持たせ、出来高とセットで評価する（単体では高評価しない）。"""
    if not b or len(b["closes"]) < min_bars:
        return []
    W = 36                                   # 直近36本（3時間）に限定：1日全体のスイングを混ぜて誤検出しない
    h, l, c, v = b["highs"][-W:], b["lows"][-W:], b["closes"][-W:], b["volumes"][-W:]
    n = len(c)
    sh, sl = swings(h, l, 2)
    FRESH = 15                               # パターンの最後のスイングが直近15本以内のものだけ有効
    sh_f = lambda idx: (n - 1 - idx) <= FRESH
    sl_f = sh_f
    out = []
    conf_scale = min(1.0, n / 24.0)

    def near(a, b_, tol):
        return abs(a - b_) / max(a, b_) <= tol
    # 二重/三重天井・底
    if len(sh) >= 2 and sh_f(sh[-1]):
        a, bb = sh[-2], sh[-1]
        valley = min(l[a:bb + 1])
        triple = (len(sh) >= 3 and near(h[sh[-3]], h[bb], 0.003) and near(h[a], h[bb], 0.003) and bb - sh[-3] >= 9
                  and min(l[sh[-3]:a + 1]) <= h[a] * 0.996 and min(l[a:bb + 1]) <= h[bb] * 0.996)   # 谷が十分深い三山だけ
        if not triple and bb - a >= 3 and near(h[a], h[bb], 0.004) and (h[a] - valley) / h[a] >= 0.005:
            confirmed = c[-1] < valley
            out.append({"name": "DOUBLE_TOP", "direction": BEAR, "neckline": round(valley, 2), "confirmed": confirmed,
                        "pattern_confidence": round((0.6 if confirmed else 0.4) * conf_scale + 0.2, 2)})
        if triple:
            out.append({"name": "TRIPLE_TOP", "direction": BEAR, "neckline": round(min(l[sh[-3]:bb + 1]), 2),
                        "confirmed": c[-1] < min(l[sh[-3]:bb + 1]) * 0.999, "pattern_confidence": round(0.5 * conf_scale + 0.2, 2),
                        "note": "三山≒triple top（H&Sと統合）"})
    if len(sl) >= 2 and sl_f(sl[-1]):
        a, bb = sl[-2], sl[-1]
        peak = max(h[a:bb + 1])
        if bb - a >= 3 and near(l[a], l[bb], 0.004) and (peak - l[a]) / peak >= 0.005:
            confirmed = c[-1] > peak
            out.append({"name": "DOUBLE_BOTTOM", "direction": BULL, "neckline": round(peak, 2), "confirmed": confirmed,
                        "pattern_confidence": round((0.6 if confirmed else 0.4) * conf_scale + 0.2, 2)})
    # 三尊/逆三尊
    if len(sh) >= 3 and sh_f(sh[-1]):
        p1, p2, p3 = sh[-3], sh[-2], sh[-1]
        if h[p2] > h[p1] * 1.004 and h[p2] > h[p3] * 1.004 and near(h[p1], h[p3], 0.006):
            neck = (min(l[p1:p2 + 1]) + min(l[p2:p3 + 1])) / 2
            out.append({"name": "HEAD_AND_SHOULDERS", "direction": BEAR, "neckline": round(neck, 2),
                        "confirmed": c[-1] < neck, "pattern_confidence": round(0.55 * conf_scale + 0.2, 2)})
    if len(sl) >= 3 and sl_f(sl[-1]):
        p1, p2, p3 = sl[-3], sl[-2], sl[-1]
        if l[p2] < l[p1] * 0.996 and l[p2] < l[p3] * 0.996 and near(l[p1], l[p3], 0.006):
            neck = (max(h[p1:p2 + 1]) + max(h[p2:p3 + 1])) / 2
            out.append({"name": "INVERSE_HEAD_AND_SHOULDERS", "direction": BULL, "neckline": round(neck, 2),
                        "confirmed": c[-1] > neck, "pattern_confidence": round(0.55 * conf_scale + 0.2, 2)})
    # 三角保ち合い（高値/安値の傾きと出来高収縮）
    if len(sh) >= 2 and len(sl) >= 2 and sh_f(sh[-1]) and sl_f(sl[-1]):
        hs, ls = [h[i] for i in sh[-2:]], [l[i] for i in sl[-2:]]
        half = max(1, n // 2)
        vol_contract = (sum(v[half:]) / max(1, n - half)) < (sum(v[:half]) / half) * 0.9 if sum(v[:half]) > 0 else False
        highs_flat, lows_flat = near(hs[0], hs[1], 0.003), near(ls[0], ls[1], 0.0025)
        lows_up, highs_down = ls[1] > ls[0] * 1.001, hs[1] < hs[0] * 0.997
        res = max(hs)
        if highs_flat and lows_up:
            wk = breakout_vol_ratio is not None and c[-1] > res and breakout_vol_ratio < 1.2
            out.append({"name": "ASCENDING_TRIANGLE", "direction": BULL, "resistance": round(res, 2),
                        "volume": "CONTRACTING" if vol_contract else "NOT_CONTRACTING",
                        "breakout": ("WEAK_BREAKOUT" if wk else ("STRONG_BREAKOUT" if (c[-1] > res and (breakout_vol_ratio or 0) >= 1.5) else None)),
                        "pattern_confidence": round((0.5 + (0.15 if vol_contract else 0)) * conf_scale + 0.15, 2)})
        elif lows_flat and highs_down:
            out.append({"name": "DESCENDING_TRIANGLE", "direction": BEAR, "support": round(min(ls), 2),
                        "pattern_confidence": round((0.5 + (0.1 if vol_contract else 0)) * conf_scale + 0.15, 2)})
        elif highs_down and lows_up:
            out.append({"name": "TRIANGLE", "direction": NEUTRAL, "pattern_confidence": round(0.4 * conf_scale + 0.15, 2)})
    # フラッグ：直近の急騰（ポール）→ 浅い調整＋出来高減
    for pole_len in (3, 4, 5, 6):
        if n < pole_len + 4:
            continue
        end = n - 4                                   # ポールの終点（その後の調整が最低3本）
        start = end - pole_len
        if start < 0:
            continue
        pole_gain = _pct(c[end], c[start])
        if pole_gain is None or pole_gain < 1.2:
            continue
        flag = c[end + 1:]
        drift = _pct(flag[-1], c[end])
        pole_vol = sum(v[start:end + 1]) / (pole_len + 1)
        flag_vol = sum(v[end + 1:]) / len(flag)
        retrace = (c[end] - min(l[end + 1:])) / max(c[end] - c[start], 1e-9)
        if drift is not None and -0.8 <= drift <= 0.1 and retrace <= 0.5 and pole_vol > 0 and flag_vol < pole_vol:
            out.append({"name": "FLAG", "direction": BULL, "pole_gain_pct": round(pole_gain, 2), "flag_high": round(max(h[end + 1:]), 2),
                        "pattern_confidence": round(0.5 * conf_scale + 0.2, 2)})
            break
    # ボックス
    if n >= 8:
        seg_h, seg_l = h[-8:], l[-8:]
        mid = (max(seg_h) + min(seg_l)) / 2
        if (max(seg_h) - min(seg_l)) / mid <= 0.008:
            top_t = sum(1 for x in seg_h if near(x, max(seg_h), 0.0015))
            bot_t = sum(1 for x in seg_l if near(x, min(seg_l), 0.0015))
            if top_t >= 2 and bot_t >= 2:
                out.append({"name": "BOX_RANGE", "direction": NEUTRAL, "top": round(max(seg_h), 2), "bottom": round(min(seg_l), 2),
                            "height_pct": round((max(seg_h) - min(seg_l)) / mid * 100, 2), "pattern_confidence": round(0.5 * conf_scale + 0.1, 2)})
    return out


# ------------------------------------------------------------------ 8-11. MA / Granville / GC-DC / 一目

def ma_context(daily, cur_intraday_ext=False):
    """日足MAコンテキスト。クロスは遅行するため単独ENTRY根拠にしない（LATE/WHIPSAW判定つき）。"""
    if not daily or not daily.get("closes") or len(daily["closes"]) < 80:
        return None
    c = daily["closes"]
    m5, m25, m75 = _sma_list(c, 5), _sma_list(c, 25), _sma_list(c, 75)
    if not (m25 and m75):
        return None
    cur = c[-1]
    slope25 = _pct(m25[-1], m25[-6]) if len(m25) >= 6 else None
    dist25 = _pct(cur, m25[-1])
    a25, a75 = m25[-len(m75):], m75      # 期間を揃える
    cross = None
    for k in range(1, 6):
        if len(a25) > k and (a25[-k - 1] <= a75[-k - 1]) != (a25[-k] <= a75[-k]):
            cross = "GOLDEN_CROSS_RECENT" if a25[-k] > a75[-k] else "DEAD_CROSS_RECENT"
            break
    bull_align = cur > m25[-1] > m75[-1] and (slope25 or 0) > 0
    bear_align = cur < m25[-1] < m75[-1] and (slope25 or 0) < 0
    crossings = sum(1 for i in range(-10, -1) if (c[i] - m25[i]) * (c[i + 1] - m25[i + 1]) < 0)
    flat = slope25 is not None and abs(slope25) < 0.3
    whipsaw = bool(cross and (flat or crossings >= 3))
    extended = (dist25 is not None and dist25 >= 10.0) or cur_intraday_ext
    late_gc = bool(cross == "GOLDEN_CROSS_RECENT" and extended)
    gran = None
    lows = daily.get("lows") or c
    if (slope25 or 0) > 0 and dist25 is not None:
        if -1.0 <= _pct(lows[-1], m25[-1]) <= 1.5 and cur > m25[-1]:
            gran = "MA_PULLBACK"
        elif c[-2] < m25[-2] and cur > m25[-1]:
            gran = "MA_RECLAIM"
        elif extended:
            gran = "MA_EXTENDED"
    return {"slope25Pct": round(slope25, 2) if slope25 is not None else None, "dist25Pct": round(dist25, 2) if dist25 is not None else None,
            "cross": cross, "bullAlignment": bull_align, "bearAlignment": bear_align, "whipsaw": whipsaw,
            "lateGoldenCross": late_gc, "granville": gran, "extended": extended,
            "signals": ([cross] if cross else []) + (["MA_BULL_ALIGNMENT"] if bull_align else []) + (["MA_BEAR_ALIGNMENT"] if bear_align else [])
            + (["LATE_GOLDEN_CROSS"] if late_gc else []) + (["MA_CROSS_WHIPSAW"] if whipsaw else []) + ([gran] if gran else [])}


def ichimoku_context(daily):
    """一目均衡表（日足のcontextのみ。5分足ENTRYの主トリガーにはしない）。"""
    if not daily or not daily.get("closes") or len(daily["closes"]) < 80:
        return None
    hh, ll, cc_ = daily["highs"], daily["lows"], daily["closes"]

    def mid(i, n):
        return (max(hh[i - n + 1:i + 1]) + min(ll[i - n + 1:i + 1])) / 2
    i = len(cc_) - 1
    tenkan, kijun = mid(i, 9), mid(i, 26)
    j = i - 26                                     # 雲は26本先行 → 今日の雲は26本前に計算した値
    span_a = (mid(j, 9) + mid(j, 26)) / 2
    span_b = mid(j, 52)
    top, bot = max(span_a, span_b), min(span_a, span_b)
    j2 = j - 5
    span_a_prev = (mid(j2, 9) + mid(j2, 26)) / 2
    cur = cc_[-1]
    pos = "ABOVE_CLOUD" if cur > top else ("BELOW_CLOUD" if cur < bot else "IN_CLOUD")
    three_role = cur > top and tenkan > kijun and cur > cc_[i - 26]
    return {"position": pos, "cloudDirection": "UP" if span_a > span_a_prev else "DOWN", "tenkanAboveKijun": tenkan > kijun,
            "threeRoleBullish": three_role, "tenkan": round(tenkan, 2), "kijun": round(kijun, 2),
            "cloudTop": round(top, 2), "cloudBottom": round(bot, 2), "role": "context_only"}


# ------------------------------------------------------------------ 13/14. RSI・ダイバージェンス

def rsi_series(closes, period=14):
    """server._rsi（単純平均版）と同じ定義。系列が必要なためここで持つ。"""
    out = [None] * len(closes)
    for i in range(period, len(closes)):
        gains = losses = 0.0
        for k in range(i - period + 1, i + 1):
            d = closes[k] - closes[k - 1]
            gains += max(d, 0)
            losses += max(-d, 0)
        if losses == 0:
            out[i] = 100.0 if gains > 0 else 50.0
        else:
            rs = (gains / period) / (losses / period)
            out[i] = 100.0 - 100.0 / (1.0 + rs)
    return out


def stochastic_k(b, period=14):
    if not b or len(b["closes"]) < period:
        return None
    hh, ll = max(b["highs"][-period:]), min(b["lows"][-period:])
    return None if hh == ll else round((b["closes"][-1] - ll) / (hh - ll) * 100, 1)


def divergence(b):
    """price高値更新×RSI高値切り下げ（弱気）／price安値更新×RSI安値切り上げ（強気）。単独ではENTRY/EXITにしない。"""
    if not b or len(b["closes"]) < 20:
        return None
    rs = rsi_series(b["closes"])
    sh, sl = swings(b["highs"], b["lows"], 2)
    sh = [i for i in sh if rs[i] is not None]
    sl = [i for i in sl if rs[i] is not None]
    if len(sh) >= 2 and b["highs"][sh[-1]] > b["highs"][sh[-2]] and rs[sh[-1]] < rs[sh[-2]] - 2:
        return "BEARISH_DIVERGENCE"
    if len(sl) >= 2 and b["lows"][sl[-1]] < b["lows"][sl[-2]] and rs[sl[-1]] > rs[sl[-2]] + 2:
        return "BULLISH_DIVERGENCE"
    return None


# ------------------------------------------------------------------ 15/16. Volume

def price_volume_matrix(b, n=3):
    """直近n本と直前n本の（価格方向 × 出来高増減）。"""
    if not b or len(b["closes"]) < 2 * n + 1:
        return None
    c, v = b["closes"], b["volumes"]
    chg = _pct(c[-1], c[-1 - n])
    v_now, v_prev = sum(v[-n:]), sum(v[-2 * n:-n])
    if chg is None or v_prev <= 0:
        return None
    up, vol_up = chg > 0.05, v_now > v_prev * 1.1
    vol_down = v_now < v_prev * 0.9
    if chg > 0.05 and vol_up:
        return "HEALTHY_EXPANSION"
    if chg > 0.05 and vol_down:
        return "WEAK_RALLY"
    if chg < -0.05 and vol_up:
        return "DISTRIBUTION"
    if chg < -0.05 and vol_down:
        return "QUIET_PULLBACK"
    return None


def volume_profile(b, bins=8):
    """5分足の出来高を典型価格で価格帯に集計（実バーの出来高のみ使用。足が12本未満・出来高0なら作らない）。"""
    if not b or len(b["closes"]) < 12 or sum(b["volumes"]) <= 0:
        return None
    tp = [(b["highs"][i] + b["lows"][i] + b["closes"][i]) / 3.0 for i in range(len(b["closes"]))]
    lo, hi = min(tp), max(tp)
    if hi <= lo:
        return None
    step = (hi - lo) / bins
    acc = [0.0] * bins
    for p, vv in zip(tp, b["volumes"]):
        acc[min(bins - 1, int((p - lo) / step))] += vv
    k = max(range(bins), key=lambda i: acc[i])
    node = lo + step * (k + 0.5)
    cur = b["closes"][-1]
    return {"pocPrice": round(node, 2), "share": round(acc[k] / sum(acc), 2), "role": "support" if node <= cur else "resistance",
            "source": "INTRADAY_5M_TYPICAL_PRICE"}


# ------------------------------------------------------------------ 集約

def _late_flags(chart, movement, ma):
    ft = (chart or {}).get("features") or {}
    flags = []
    if (chart or {}).get("pattern") in cc.BAD_PATTERNS:
        flags.append(f"チャート{chart['pattern']}")
    if (ft.get("vwapDistPct") or 0) >= cc.VWAP_DIST_EXTENDED_PCT:
        flags.append("VWAP大幅乖離")
    if (ft.get("consecGreen") or 0) >= 3:
        flags.append("連続陽線")
    if (ft.get("upperWick") or 0) >= cc.UPPER_WICK_LONG:
        flags.append("長い上ヒゲ")
    if (movement or {}).get("too_late"):
        flags.append("値幅消化済み(too_late)")
    if ma and ma.get("lateGoldenCross"):
        flags.append("LATE_GOLDEN_CROSS")
    return flags


def _setup_type(chart, movement, brk, candles, pv, sr_support_near, ma):
    pat = (chart or {}).get("pattern")
    ft = (chart or {}).get("features") or {}
    if pat == "PULLBACK_READY" or (("LONG_LOWER_WICK" in candles) and sr_support_near and pv in ("QUIET_PULLBACK", None) and ft.get("higherLows")) \
            or (ma and ma.get("granville") == "MA_PULLBACK" and ft.get("aboveVwap")):
        return "PULLBACK"
    if (movement or {}).get("pre_breakout") or pat == "EARLY_BREAKOUT":
        return "PRE_BREAKOUT"
    if brk in ("BREAKOUT_RETEST", "STRONG_VOLUME_BREAKOUT"):
        return "BREAKOUT_RETEST" if brk == "BREAKOUT_RETEST" else "BREAKOUT_VOLUME"
    if pat == "VWAP_RECLAIM" and ft.get("higherLows"):
        return "VWAP_RECLAIM"
    return None


def evaluate_fusion(bars, chart=None, movement=None, quote=None, vwap=None, day_high=None, day_low=None, daily=None,
                    market=None, entry_decision=None, in_profit=None):
    """7グループ統合の評価。chart/movementは既存エンジンの出力（必須ではないが渡すと再利用する）。
    戻り値は常にdict（データ不足でも例外を出さない）。ENTRY/TOP5判定には使わない（shadow）。"""
    b = cc.normalize_bars(bars)
    n = len(b["closes"]) if b else 0
    cur = (quote or {}).get("t") if (quote or {}).get("t") is not None else (b["closes"][-1] if b else None)
    ft = (chart or {}).get("features") or {}
    conf = cc.confidence_for(n) if n else "UNKNOWN"
    res = {"version": VERSION, "barCount": n, "confidence": conf, "unsupported": UNSUPPORTED, "role": "shadow"}
    if not b or n < 3:
        res.update({"groups": {g: _group() for g in GROUPS}, "confluence": {"score": None, "level": "UNKNOWN", "knownGroups": 0},
                    "setupType": None, "warnings": ["5分足不足（推測しない）"], "technicalState": "UNKNOWN",
                    "recommendation": "UNKNOWN", "late": False})
        return res

    candles = candle_signals(b)
    ma = ma_context(daily, cur_intraday_ext=(chart or {}).get("pattern") in cc.BAD_PATTERNS)
    ichi = ichimoku_context(daily)
    daily_bias = BULL if (ma and ma["bullAlignment"]) else (BEAR if (ma and ma["bearAlignment"]) else None)
    levels = sr_levels(b, cur, vwap or ft.get("vwap"), day_high, day_low, daily)
    brk, brk_why = breakout_state(b, ft)
    bv = ft.get("breakoutVolRatio")
    patterns = detect_patterns(b, bv)
    pv = price_volume_matrix(b)
    rs = rsi_series(b["closes"])
    rsi = rs[-1]
    stoch = stochastic_k(b)
    div = divergence(b)
    prof = volume_profile(b)
    warnings, reasons = [], []

    # --- A. Trend
    g_trend = trend_structure(b, ft, daily_bias)

    # --- B. Price Action（同一グループは代表signal1つ）
    near_res = next((x for x in levels if x["type"] == "resistance" and abs(x["distancePct"]) <= 0.4), None)
    near_sup = next((x for x in levels if x["type"] == "support" and abs(x["distancePct"]) <= 0.4), None)
    pa_state, pa_str, pa_why = NEUTRAL, 0.2, []
    if "LONG_UPPER_WICK" in candles and (near_res or (ft.get("distFromDayHighPct") is not None and ft["distFromDayHighPct"] >= -0.5)):
        pa_state, pa_str = BEAR, 0.7
        pa_why.append("高値圏で長い上ヒゲ（CHASE/FAILED_BREAKOUT警戒）")
    elif "BEARISH_ENGULFING" in candles or "THREE_BLACK_CROWS" in candles or "LARGE_BEAR_BODY" in candles:
        pa_state, pa_str = BEAR, 0.6
        pa_why.append("弱気のローソク足")
    elif "LONG_LOWER_WICK" in candles and near_sup and (pv in ("QUIET_PULLBACK", "HEALTHY_EXPANSION") or (ft.get("volRatioLast") or 0) >= 1.0):
        pa_state, pa_str = BULL, 0.8
        pa_why.append("支持線付近の長い下ヒゲ＋出来高（PULLBACK加点）")
    elif "BULLISH_ENGULFING" in candles or "THREE_WHITE_SOLDIERS" in candles:
        pa_state, pa_str = BULL, 0.6
        pa_why.append("強気のローソク足")
    elif "LARGE_BULL_BODY" in candles:
        # 大陽線は単独で加点しすぎない。すでに急騰後ならむしろTOO_LATE候補
        late_now = (chart or {}).get("pattern") in cc.BAD_PATTERNS or (ft.get("chg15m") or 0) >= cc.FAST_15M_PCT
        pa_state, pa_str = (NEUTRAL, 0.2) if late_now else (BULL, 0.35)
        pa_why.append("大陽線" + ("（急騰後＝TOO_LATE候補）" if late_now else ""))
    g_pa = _group(pa_state if candles or n >= 6 else UNKNOWN, pa_str, candles, pa_why)

    # --- C. Support / Resistance
    sr_state, sr_str, sr_why = NEUTRAL, 0.2, []
    if brk == "BREAKOUT_RETEST":
        sr_state, sr_str = BULL, 0.9
        sr_why += brk_why
    elif brk == "STRONG_VOLUME_BREAKOUT":
        sr_state, sr_str = BULL, 0.8
        sr_why += brk_why
    elif brk in ("FAILED_BREAKOUT",):
        sr_state, sr_str = BEAR, 0.9
        sr_why += brk_why
    elif brk == "WEAK_BREAKOUT":
        sr_state, sr_str = NEUTRAL, 0.3
        sr_why += brk_why
    elif brk == "BREAKOUT_PROBE":
        sr_state, sr_str = NEUTRAL, 0.2
        sr_why += brk_why
    elif near_sup and near_sup["touch_count"] >= 2 and (ft.get("higherLows")):
        sr_state, sr_str = BULL, min(0.85, near_sup["support_strength"] or 0.4)
        sr_why.append(f"{near_sup['kind']} {near_sup['price']:,.1f}が{near_sup['touch_count']}回支持")
    elif near_res and near_res["touch_count"] >= 2 and "LONG_UPPER_WICK" in candles:
        sr_state, sr_str = BEAR, min(0.85, near_res["resistance_strength"] or 0.4)
        sr_why.append(f"{near_res['kind']} {near_res['price']:,.1f}で{near_res['touch_count']}回抵抗（反落）")
    elif ft.get("vwapLoss"):
        sr_state, sr_str = BEAR, 0.6
        sr_why.append("VWAPを割り込み（支持喪失）")
    g_sr = _group(sr_state if levels else UNKNOWN, sr_str, [x["kind"] for x in levels[:3]], sr_why,
                  {"levels": levels[:6], "breakout": brk})

    # --- D. Pattern（パターン単体は高評価しない：出来高とセット）
    pat_state, pat_str, pat_why, pat_sig = NEUTRAL, 0.0, [], [p["name"] for p in patterns]
    for p in sorted(patterns, key=lambda x: -x["pattern_confidence"]):
        if p["direction"] == BEAR and (p.get("confirmed") or p["name"] in ("DESCENDING_TRIANGLE",)):
            pat_state, pat_str = BEAR, p["pattern_confidence"]
            pat_why.append(f"{p['name']}（確認済み/信頼度{p['pattern_confidence']}）")
            break
        if p["direction"] == BULL:
            weak = p.get("breakout") == "WEAK_BREAKOUT" or (p["name"] in ("ASCENDING_TRIANGLE",) and p.get("volume") == "NOT_CONTRACTING")
            if weak:
                pat_state, pat_str = NEUTRAL, 0.25
                pat_why.append(f"{p['name']}だが出来高が伴わない（WEAK_BREAKOUT）")
            else:
                pat_state, pat_str = BULL, p["pattern_confidence"] * (0.7 if p["name"] != "FLAG" else 0.8)
                pat_why.append(f"{p['name']}（信頼度{p['pattern_confidence']}）")
            break
    g_pat = _group(pat_state if (patterns or n >= 12) else UNKNOWN, pat_str, pat_sig, pat_why, {"patterns": patterns})

    # --- E. Momentum（RSI/Stoch/乖離/VWAP乖離は「過熱」1つの根拠へ集約）
    overheated_src = []
    if rsi is not None and rsi >= 75:
        overheated_src.append(f"RSI{rsi:.0f}")
    if stoch is not None and stoch >= 90:
        overheated_src.append(f"Stoch{stoch:.0f}")
    if (ft.get("vwapDistPct") or 0) >= cc.VWAP_DIST_EXTENDED_PCT:
        overheated_src.append("VWAP乖離大")
    mo_state, mo_str, mo_sig, mo_why = NEUTRAL, 0.2, [], []
    if overheated_src:
        healthy = ((movement or {}).get("activity_state") == "EXPANDING" and pv == "HEALTHY_EXPANSION"
                   and "LONG_UPPER_WICK" not in candles and not ft.get("volPeakout"))
        if healthy:
            mo_state, mo_str, mo_sig = BULL, 0.6, ["MOMENTUM_HEALTHY_STRONG"]
            mo_why.append("過熱圏だが値幅・出来高が拡大中（強いトレンド）")
        elif "LONG_UPPER_WICK" in candles or ft.get("volPeakout"):
            mo_state, mo_str, mo_sig = BEAR, 0.7, ["MOMENTUM_EXHAUSTION"]
            mo_why.append("過熱＋上ヒゲ/出来高ピークアウト（EXHAUSTION警戒）")
        else:
            mo_state, mo_str, mo_sig = NEUTRAL, 0.4, ["MOMENTUM_OVERHEATED"]
            mo_why.append("過熱（" + "・".join(overheated_src) + "は同一根拠として1件扱い）")
    elif rsi is not None:
        if 50 <= rsi < 75:
            mo_state, mo_str = BULL, 0.5
            mo_why.append(f"RSI{rsi:.0f}（健全な上昇圏）")
        elif rsi < 35:
            mo_state, mo_str = BEAR, 0.5
            mo_why.append(f"RSI{rsi:.0f}（弱い）")
    if div:
        mo_sig.append("DIVERGENCE_WARNING")
        warnings.append({"BEARISH_DIVERGENCE": "弱気ダイバージェンス（高値更新×RSI低下）", "BULLISH_DIVERGENCE": "強気ダイバージェンス候補"}[div])
        if div == "BEARISH_DIVERGENCE" and mo_state == BULL:
            mo_state, mo_str = NEUTRAL, min(mo_str, 0.3)
    g_mo = _group(mo_state if (rsi is not None or overheated_src) else UNKNOWN, mo_str, mo_sig, mo_why,
                  {"rsi": None if rsi is None else round(rsi, 1), "stochK": stoch, "divergence": div})

    # --- F. Volume / Market Energy
    v_state, v_str, v_why = UNKNOWN, 0.0, []
    if pv:
        v_state = {"HEALTHY_EXPANSION": BULL, "QUIET_PULLBACK": BULL, "WEAK_RALLY": NEUTRAL, "DISTRIBUTION": BEAR}[pv]
        v_str = {"HEALTHY_EXPANSION": 0.7, "QUIET_PULLBACK": 0.55, "WEAK_RALLY": 0.3, "DISTRIBUTION": 0.7}[pv]
        v_why.append({"HEALTHY_EXPANSION": "価格↑×出来高↑（健全な拡大）", "WEAK_RALLY": "価格↑×出来高↓（上昇が弱い）",
                      "DISTRIBUTION": "価格↓×出来高↑（売り圧力/分配）", "QUIET_PULLBACK": "価格↓×出来高↓（静かな押し）"}[pv])
    if bv is not None and brk in ("STRONG_VOLUME_BREAKOUT", "WEAK_BREAKOUT", "BREAKOUT_RETEST"):
        v_why.append(f"ブレイク出来高{bv:.1f}倍")
        if bv >= 1.5:
            v_state, v_str = BULL, max(v_str, 0.8)
        elif bv < 1.2 and v_state != BEAR:
            v_state, v_str = NEUTRAL, min(max(v_str, 0.2), 0.3)
    if ft.get("volPeakout") and v_state != BEAR:
        v_state, v_str = NEUTRAL if v_state == BULL else v_state, min(v_str, 0.3) if v_str else 0.3
        v_why.append("出来高ピークアウト")
    g_vol = _group(v_state, v_str, [pv] if pv else [], v_why, {"matrix": pv, "profile": prof})

    # --- G. Market Context（取得可能なものだけ。breadthは未取得）
    m = market or {}
    mrs, nk = m.get("marketRS"), m.get("nikkeiChg")
    mc_state, mc_str, mc_why = UNKNOWN, 0.0, []
    if mrs is not None or nk is not None or m.get("sectorLead"):
        mc_state, mc_str = NEUTRAL, 0.2
        if (mrs is not None and mrs >= 1.0) or m.get("sectorLead"):
            mc_state, mc_str = BULL, 0.5 + (0.2 if (mrs or 0) >= 3 else 0)
            mc_why.append("対市場で優位" + ("・セクター内リード" if m.get("sectorLead") else ""))
        if (nk is not None and nk <= -1.0) or (mrs is not None and mrs <= -1.0):
            mc_state, mc_str = BEAR, 0.6
            mc_why = ["地合い逆風または対市場で劣位"]
    g_mc = _group(mc_state, mc_str, [], mc_why, {"breadth": "UNAVAILABLE"})

    groups = {"trend": g_trend, "price_action": g_pa, "support_resistance": g_sr, "pattern": g_pat, "momentum": g_mo,
              "volume": g_vol, "market_context": g_mc}

    # --- Confluence（グループ単位。同じ方向のグループ数×代表strength）
    known = {k: g for k, g in groups.items() if g["state"] != UNKNOWN}
    wsum = sum(GROUP_WEIGHTS[k] for k in known)
    bull = sum(GROUP_WEIGHTS[k] * g["strength"] for k, g in known.items() if g["state"] == BULL)
    bear = sum(GROUP_WEIGHTS[k] * g["strength"] for k, g in known.items() if g["state"] == BEAR)
    score = None
    if wsum > 0:
        score = 100.0 * bull / wsum
        if bear > 0:                                           # 逆方向のグループがあれば割り引く（競合）
            score *= max(0.0, 1.0 - 1.5 * bear / wsum)
        if len(known) < MIN_KNOWN_GROUPS:
            score = min(score, 50.0)
        score = round(score, 1)
    bull_groups = [k for k, g in known.items() if g["state"] == BULL]
    bear_groups = [k for k, g in known.items() if g["state"] == BEAR]
    level = "UNKNOWN" if score is None else ("HIGH" if score >= HIGH_CONFLUENCE else ("MEDIUM" if score >= MID_CONFLUENCE else "LOW"))
    conflict = bool(bull_groups and bear_groups and min(len(bull_groups), len(bear_groups)) >= 2)
    if conflict:
        warnings.append("グループ間でsignalが競合（強気" + "/".join(bull_groups) + "×弱気" + "/".join(bear_groups) + "）")

    late_flags = _late_flags(chart, movement, ma)
    late = bool(late_flags)
    tech_state = "TECHNICALLY_STRONG_BUT_LATE" if (late and score is not None and score >= MID_CONFLUENCE) else (
        "STRONG" if level == "HIGH" else ("MIXED" if conflict else ("WEAK" if level == "LOW" else "MODERATE")))
    if tech_state == "TECHNICALLY_STRONG_BUT_LATE":
        warnings.append("テクニカルは強いが遅い：" + "・".join(late_flags))
    if ma and ma.get("lateGoldenCross"):
        warnings.append("LATE_GOLDEN_CROSS（クロス後にすでに急騰）")
    if ma and ma.get("whipsaw"):
        warnings.append("MA_CROSS_WHIPSAWの可能性（傾きが緩い/クロス多発）")

    setup = _setup_type(chart, movement, brk, candles, pv, bool(near_sup), ma)
    timing = (chart or {}).get("entry_timing_score")
    # ENTRYタイミングが悪ければ買わない（テクニカルが強くてもWAIT）
    if late or (timing is not None and timing < 40):
        rec = "WAIT"
    elif level == "HIGH" and setup and (timing is None or timing >= 60):
        rec = "ENTRY_SUPPORTED"
    elif level == "LOW":
        rec = "WEAK_TECHNICAL"
    else:
        rec = "NEUTRAL"

    # 目標/ストップ候補（数値は変更せずshadow比較。既存Momentum stopは movement.recommended_stop）
    targets, stops = [], []
    for p in patterns:
        if p["name"] == "BOX_RANGE" and cur:
            targets.append({"kind": "BOX_HEIGHT_PROJECTION", "price": round(p["top"] + (p["top"] - p["bottom"]), 2)})
        if p["name"] == "FLAG" and cur:
            targets.append({"kind": "FLAG_POLE_PROJECTION", "price": round(p["flag_high"] * (1 + p["pole_gain_pct"] / 100.0), 2)})
    if brk in ("STRONG_VOLUME_BREAKOUT", "BREAKOUT_RETEST") and ft.get("breakoutLevel") and day_low:
        targets.append({"kind": "BREAKOUT_MEASURED_MOVE", "price": round(ft["breakoutLevel"] + (ft["breakoutLevel"] - day_low), 2)})
    for x in levels:
        if x["type"] == "support" and x["kind"] in ("swing_low", "vwap", "ma25"):
            stops.append({"kind": x["kind"], "price": x["price"]})
    rs_stop = (movement or {}).get("recommended_stop")
    if rs_stop:
        stops.append({"kind": "movement_recommended", "price": (rs_stop or {}).get("price")})

    exit_press = exit_pressure(candles, ft, div, near_sup is None and bool(ft.get("vwapLoss")), in_profit)
    reasons = [r for k in ("trend", "support_resistance", "volume", "price_action", "pattern", "momentum", "market_context")
               for r in groups[k]["reasons"]]
    res.update({
        "groups": groups, "confluence": {"score": score, "level": level, "knownGroups": len(known), "bullGroups": bull_groups,
                                         "bearGroups": bear_groups, "conflict": conflict},
        "setupType": setup, "technicalState": tech_state, "late": late, "lateFlags": late_flags,
        "recommendation": rec, "entryDecisionExisting": entry_decision, "warnings": warnings, "reasons": reasons[:8],
        "maContext": ma, "ichimoku": ichi, "candles": candles, "patterns": patterns, "breakout": brk,
        "targetCandidates": targets, "stopCandidates": stops, "exitPressure": exit_press,
        "timeframes": {"5m": g_trend["trendState"], "15m": _trend_15m(b), "daily": (ma or {}).get("bullAlignment") and "BULL_ALIGN"
                       or ((ma or {}).get("bearAlignment") and "BEAR_ALIGN") or ("N/A" if not ma else "MIXED")},
    })
    return res


def _trend_15m(b):
    """5分足3本→15分足へ集約したトレンド（短期トレンド確認用）。8本（24分）未満なら判定しない。"""
    if not b or len(b["closes"]) < 12:
        return "UNKNOWN"
    m = len(b["closes"]) // 3 * 3
    off = len(b["closes"]) - m
    hs = [max(b["highs"][off + i:off + i + 3]) for i in range(0, m, 3)]
    ls = [min(b["lows"][off + i:off + i + 3]) for i in range(0, m, 3)]
    if len(hs) < 3:
        return "UNKNOWN"
    if hs[-1] > hs[-2] > hs[-3] and ls[-1] > ls[-2]:
        return "UPTREND"
    if hs[-1] < hs[-2] and ls[-1] < ls[-2] < ls[-3]:
        return "DOWNTREND"
    return "RANGE"


def exit_pressure(candles, features, div, support_lost, in_profit=None):
    """EXITへのTechnical Fusion（shadow）。利益中に上ヒゲ＋出来高ピーク＋ダイバージェンス＋支持喪失が
    重なるほどconfidenceが上がる。同種の根拠は重複加算しない（signal種別ごとに1回）。"""
    pts, why = 0.0, []
    if "LONG_UPPER_WICK" in candles or "BEARISH_ENGULFING" in candles:
        pts += 0.25
        why.append("上ヒゲ/弱気の包み足")
    if (features or {}).get("volPeakout"):
        pts += 0.25
        why.append("出来高ピークアウト")
    if div == "BEARISH_DIVERGENCE":
        pts += 0.25
        why.append("弱気ダイバージェンス")
    if support_lost:
        pts += 0.25
        why.append("支持（VWAP）喪失")
    if in_profit is False:
        pts *= 0.7        # 含み損側は既存EXIT RULEが主導のため補助扱い
    return {"confidence": round(min(1.0, pts), 2), "reasons": why}


def compact(fusion):
    """ログ/候補dict用の軽量版（DB context_jsonに載せる）。"""
    if not fusion:
        return None
    g = fusion.get("groups") or {}
    return {"version": fusion.get("version"), "score": (fusion.get("confluence") or {}).get("score"),
            "level": (fusion.get("confluence") or {}).get("level"), "setupType": fusion.get("setupType"),
            "technicalState": fusion.get("technicalState"), "recommendation": fusion.get("recommendation"),
            "late": fusion.get("late"), "lateFlags": fusion.get("lateFlags"),
            "groups": {k: {"state": v["state"], "strength": v["strength"], "signals": v["signals"][:4]} for k, v in g.items()},
            "bullGroups": (fusion.get("confluence") or {}).get("bullGroups"),
            "bearGroups": (fusion.get("confluence") or {}).get("bearGroups"),
            "warnings": fusion.get("warnings"), "breakout": fusion.get("breakout"),
            "patterns": [p["name"] for p in (fusion.get("patterns") or [])], "candles": fusion.get("candles"),
            "ma": (fusion.get("maContext") or {}).get("signals"), "exitPressure": fusion.get("exitPressure"),
            "entryDecisionExisting": fusion.get("entryDecisionExisting")}
