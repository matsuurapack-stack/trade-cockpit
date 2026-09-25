# Chart Context Engine（Phase C）：直近の5分足OHLCVの時系列形状から、
#   ・チャートパターン状態（EARLY_BREAKOUT / BREAKOUT_CONFIRMED / PULLBACK_READY / VWAP_RECLAIM /
#     EXTENDED / CHASE / FAILED_BREAKOUT / EXHAUSTION / BASE_BUILDING / VWAP_LOSS）
#   ・entry_timing_score（0-100、「今この価格で入るタイミングが良いか」）
#   ・chart_context_confidence（バー本数・時間帯依存）
#   ・必ず理由（reasons / penalties）
# を返す純粋関数群（DB・ネットワーク非依存、未来データを参照しない＝渡されたbarsだけを使う）。
#
# 「銘柄の強さ（stock_strength_score）」と「今のENTRYタイミング（entry_timing_score）」は別軸。
# 強い銘柄でもタイミングが悪ければ STOCK STRONG / ENTRY WAIT になる。
#
# 既存のentry_score・BREAKOUT/PULLBACK判定・momentum_stateは削除せず、existing_signalsとして
# 受け取って併用する（既存signal＋chart contextの両方を見る）。

import re

PATTERNS = ("EARLY_BREAKOUT", "BREAKOUT_CONFIRMED", "PULLBACK_READY", "VWAP_RECLAIM", "EXTENDED", "CHASE",
            "FAILED_BREAKOUT", "EXHAUSTION", "BASE_BUILDING", "VWAP_LOSS", "NEUTRAL", "UNKNOWN")
BAD_PATTERNS = ("CHASE", "EXTENDED", "FAILED_BREAKOUT", "EXHAUSTION")
GOOD_PATTERNS = ("EARLY_BREAKOUT", "PULLBACK_READY", "VWAP_RECLAIM")

# 判定しきい値（暫定値。実トレードのバックテストを見て調整する前提）
FAST_15M_PCT = 1.2          # 15分上昇率がこれ以上＝急上昇
EXTENDED_15M_PCT = 2.0
VWAP_DIST_BIG_PCT = 1.2
VWAP_DIST_EXTENDED_PCT = 2.0
UPPER_WICK_LONG = 0.35
ENTRY_MIN_STRENGTH = 50
ENTRY_MIN_TIMING = 60
UPGRADE_MIN_TIMING = 70
EARLY_SESSION_MINUTES = 15   # 9:00〜9:15は判定の信頼度を下げ、昇格させない


def normalize_bars(bars):
    """dict-of-arrays（closes/highs/lows/volumes[/opens]）または list-of-dict（内部5分足の
    open/high/low/close/volume）を dict-of-arrays へそろえる。opensが無い場合は
    opens_approx=True（前の足の終値で代用、足の形の判定は控えめにする）。"""
    if not bars:
        return None
    if isinstance(bars, (list, tuple)):
        if not bars:
            return None
        return {"opens": [b.get("open") for b in bars], "highs": [b.get("high") for b in bars],
                "lows": [b.get("low") for b in bars], "closes": [b.get("close") for b in bars],
                "volumes": [b.get("volume") or 0.0 for b in bars], "opens_approx": False}
    closes = list(bars.get("closes") or [])
    n = len(closes)
    highs, lows = list(bars.get("highs") or []), list(bars.get("lows") or [])
    vols = list(bars.get("volumes") or [])
    if not (len(highs) == len(lows) == n):
        return None
    if len(vols) != n:
        vols = (vols + [0.0] * n)[:n]
    opens = bars.get("opens")
    approx = False
    if not opens or len(opens) != n:
        opens = [closes[0]] + closes[:-1]
        approx = True
    return {"opens": list(opens), "highs": highs, "lows": lows, "closes": closes, "volumes": vols,
            "opens_approx": approx}


def confidence_for(n_bars, minutes_since_open=None):
    """12本以上=HIGH / 6〜11本=MEDIUM / 3〜5本=LOW / 3本未満=UNKNOWN。寄り直後（9:00〜9:15）は
    値動きが激しくVWAPも不安定なので一段下げる。"""
    if n_bars < 3:
        return "UNKNOWN"
    level = 3 if n_bars >= 12 else (2 if n_bars >= 6 else 1)
    if minutes_since_open is not None and minutes_since_open < EARLY_SESSION_MINUTES:
        level = max(1, level - 1)
    return {3: "HIGH", 2: "MEDIUM", 1: "LOW"}[level]


def _pct(a, b):
    return None if (a is None or not b) else (a / b - 1.0) * 100.0


def _running_vwap(closes, highs, lows, vols):
    out, pv, tv = [], 0.0, 0.0
    for c, h, l, v in zip(closes, highs, lows, vols):
        tp = (c + h + l) / 3.0
        pv += tp * v
        tv += v
        out.append(pv / tv if tv > 0 else None)
    return out


def _candle(o, h, l, c):
    rng = max(h - l, 1e-9)
    body = abs(c - o)
    return {"body": body / rng, "upper": (h - max(o, c)) / rng, "lower": (min(o, c) - l) / rng,
            "bull": c > o, "bear": c < o, "doji": body / rng < 0.15}


def compute_features(bars, quote=None, vwap=None, day_high=None, day_low=None, lookback=24):
    """時系列形状の生の特徴量。barsは最新が末尾。未来データは使わない。"""
    b = normalize_bars(bars)
    if not b or len(b["closes"]) < 2:
        return None
    sl = slice(-lookback, None)
    o, h, l, c, v = (b[k][sl] for k in ("opens", "highs", "lows", "closes", "volumes"))
    n = len(c)
    cur = (quote or {}).get("t") if (quote or {}).get("t") is not None else c[-1]
    if cur != c[-1]:  # 最新quoteで最終足を更新（軽量再スコア用）。足の数は増やさない
        c = c[:-1] + [cur]
        h = h[:-1] + [max(h[-1], cur)]
        l = l[:-1] + [min(l[-1], cur)]
    vwaps = _running_vwap(c, h, l, v)
    vw = vwap if vwap else vwaps[-1]
    f = {"n": n, "cur": cur, "vwap": vw, "opensApprox": b["opens_approx"]}
    for k in (3, 6, 12):
        f[f"slope{k}"] = _pct(c[-1], c[-1 - k]) if n > k else None
    f["chg5m"] = _pct(c[-1], c[-2]) if n >= 2 else None
    f["chg15m"] = _pct(c[-1], c[-4]) if n >= 4 else None
    f["chg30m"] = _pct(c[-1], c[-7]) if n >= 7 else None
    # 高値/安値の切り上げ・切り下げ（直近3本 vs その前3本）
    if n >= 6:
        f["higherHighs"] = max(h[-3:]) > max(h[-6:-3])
        f["higherLows"] = min(l[-3:]) > min(l[-6:-3])
        f["lowerHighs"] = max(h[-3:]) < max(h[-6:-3])
        f["lowerLows"] = min(l[-3:]) < min(l[-6:-3])
    else:
        f["higherHighs"] = f["higherLows"] = f["lowerHighs"] = f["lowerLows"] = None
    # 足の形
    cs = [_candle(o[i], h[i], l[i], c[i]) for i in range(n)]
    for i, x in enumerate(cs):   # 値幅が極小（<0.12%）の足のヒゲ比率はノイズなので0扱い
        if c[i] and (h[i] - l[i]) / c[i] * 100 < 0.12:
            x["upper"] = x["lower"] = 0.0
    last = cs[-1]
    f.update({"bodyRatio": round(last["body"], 2), "upperWick": round(last["upper"], 2),
              "lowerWick": round(last["lower"], 2), "bull": last["bull"], "bear": last["bear"], "doji": last["doji"]})
    g = 0
    for x in reversed(cs):
        if x["bull"]:
            g += 1
        else:
            break
    r = 0
    for x in reversed(cs):
        if x["bear"]:
            r += 1
        else:
            break
    f["consecGreen"], f["consecRed"] = g, r
    f["greenRunGainPct"] = _pct(c[-1], c[-1 - g]) if 0 < g < n else None   # 連続陽線の合計上昇率
    f["swingPct"] = _pct(max(h[-12:]), min(l[-12:]))                       # 直近12本の値幅（安値→高値）
    f["upperWickAvg3"] = round(sum(x["upper"] for x in cs[-3:]) / min(3, n), 2)
    f["upperWickRising"] = n >= 3 and cs[-1]["upper"] > cs[-2]["upper"] >= cs[-3]["upper"] and cs[-1]["upper"] >= 0.25
    # VWAP
    f["vwapDistPct"] = _pct(cur, vw) if vw else None
    f["aboveVwap"] = (cur > vw) if vw else None
    f["vwapReclaim"] = f["vwapLoss"] = f["vwapBounce"] = f["vwapCross"] = False
    if n >= 4 and all(x is not None for x in vwaps[-6:]):
        prior = list(zip(c[-6:-1], vwaps[-6:-1]))
        was_below = any(cc < vv for cc, vv in prior)
        was_above = any(cc > vv for cc, vv in prior)
        f["vwapReclaim"] = bool(was_below and c[-1] > vwaps[-1] and c[-2] >= vwaps[-2] * 0.999)
        f["vwapLoss"] = bool(was_above and c[-1] < vwaps[-1] and c[-2] <= vwaps[-2] * 1.001) or \
            bool(was_above and c[-1] < vwaps[-1] and c[-2] >= vwaps[-2] and cs[-1]["bear"])
        f["vwapCross"] = f["vwapReclaim"] or f["vwapLoss"]
        f["vwapBounce"] = bool(any(l[i] <= vwaps[i] * 1.0015 and c[i] > vwaps[i] for i in range(-3, 0)) and c[-1] > vwaps[-1])
    # 高値
    dh = day_high if day_high else max(h)
    dl = day_low if day_low else min(l)
    f["dayHigh"], f["dayLow"] = dh, dl
    f["distFromDayHighPct"] = _pct(cur, dh)
    pre = max(h[:-2]) if n >= 4 else None    # 直近2本より前の高値＝ブレイク対象水準
    f["breakoutLevel"] = pre
    f["newHighNow"] = bool(n >= 3 and h[-1] >= max(h[:-1]) * 0.9999)
    broke_idx = None
    if pre:
        for i in (n - 2, n - 1):
            if c[i] > pre or h[i] > pre * 1.0003:
                broke_idx = i
                break
    f["brokeRecently"] = broke_idx is not None
    f["breakoutHeld"] = bool(pre and broke_idx is not None and c[-1] >= pre * 0.999)
    f["failedBreakout"] = False
    if pre and broke_idx is not None:
        bc = cs[broke_idx]
        after_weak = (broke_idx == n - 2 and h[-1] < h[broke_idx] and c[-1] < pre) or (c[-1] < pre * 0.998)
        f["failedBreakout"] = bool((after_weak and (bc["upper"] >= 0.3 or c[-1] < pre)) and c[-1] < h[broke_idx] * 0.997)
    f["retake"] = bool(pre and n >= 5 and max(h[-5:-1]) > pre and min(c[-4:-1]) < pre and c[-1] > pre)
    # 出来高
    base_v = [x for x in v[-7:-1] if x is not None]
    avg_v = (sum(base_v) / len(base_v)) if base_v else None
    f["volRatioLast"] = round(v[-1] / avg_v, 2) if avg_v else None
    bo_i = broke_idx if broke_idx is not None else None
    f["breakoutVolRatio"] = None
    if bo_i is not None and avg_v:
        f["breakoutVolRatio"] = round(v[bo_i] / avg_v, 2)
    pb_bars = [i for i in range(max(0, n - 3), n) if cs[i]["bear"] or c[i] < c[i - 1]] if n >= 4 else []
    up_bars = [i for i in range(max(0, n - 8), n - 3) if cs[i]["bull"]] if n >= 8 else []
    if pb_bars and up_bars:
        f["pullbackVolDown"] = (sum(v[i] for i in pb_bars) / len(pb_bars)) < (sum(v[i] for i in up_bars) / len(up_bars))
    else:
        f["pullbackVolDown"] = None
    f["volPeakout"] = False
    if n >= 6:
        tail = v[-6:]
        pk = max(tail)
        f["volPeakout"] = bool(pk > 0 and tail.index(pk) < 5 and v[-1] < pk * 0.6)
    # 値幅の縮小・平均足幅
    rng = [(h[i] - l[i]) / cur * 100 for i in range(n)] if cur else [0] * n
    f["avgBarRangePct"] = round(sum(rng[-6:]) / min(6, n), 3)
    f["rangeContracting"] = bool(n >= 6 and sum(rng[-3:]) / 3 < 0.6 * (sum(rng[-6:-3]) / 3 or 1e9))
    f["range6Pct"] = round((max(h[-6:]) - min(l[-6:])) / cur * 100, 2) if (n >= 6 and cur) else None
    return f


def _uptrend(f):
    s12, s6 = f.get("slope12"), f.get("slope6")
    return bool((s12 is not None and s12 >= 0.5) or (s6 is not None and s6 >= 0.4 and f.get("higherLows")))


def classify_pattern(f):
    """優先順位：FAILED_BREAKOUT > EXHAUSTION > CHASE > EXTENDED > VWAP_LOSS > PULLBACK_READY >
    VWAP_RECLAIM > EARLY_BREAKOUT > BREAKOUT_CONFIRMED > BASE_BUILDING > NEUTRAL。
    戻り値: (pattern, flags)。flagsは判定に使った真偽の内訳（explainability用）。"""
    fl = {}
    dh = f.get("distFromDayHighPct")
    near_high = dh is not None and dh >= -0.6
    fast15 = (f.get("chg15m") or 0) >= FAST_15M_PCT
    vwap_big = (f.get("vwapDistPct") or 0) >= VWAP_DIST_BIG_PCT
    green3 = f.get("consecGreen", 0) >= 3 and (f.get("greenRunGainPct") or 0) >= 0.6   # 小幅の陽線連続はCHASE扱いにしない
    long_upper = f.get("upperWick", 0) >= UPPER_WICK_LONG
    fl.update({"near_high": near_high, "fast15": fast15, "vwap_dist_big": vwap_big, "green3": green3,
               "long_upper_wick": long_upper, "vol_peakout": f.get("volPeakout")})
    chase_flags = [green3, fast15, vwap_big, near_high, long_upper, bool(f.get("volPeakout"))]
    chase_count = sum(1 for x in chase_flags if x)
    fl["chase_count"] = chase_count
    extended = ((f.get("chg15m") or 0) >= EXTENDED_15M_PCT or (f.get("vwapDistPct") or 0) >= VWAP_DIST_EXTENDED_PCT
                or ((f.get("chg30m") or 0) >= 3.0 and near_high))
    fl["extended"] = extended
    big_prior = (f.get("chg30m") or 0) >= 2.5 or (f.get("slope12") or 0) >= 3.0 or (f.get("swingPct") or 0) >= 2.5
    exhaustion = bool(big_prior and near_high and sum([green3, bool(f.get("upperWickRising")) or f.get("upperWickAvg3", 0) >= 0.3,
                                                       bool(f.get("volPeakout")), bool(f.get("rangeContracting"))]) >= 2)
    fl["exhaustion"] = exhaustion
    if f.get("failedBreakout"):
        return "FAILED_BREAKOUT", fl
    if exhaustion:
        return "EXHAUSTION", fl
    # CHASE：強いが追いかけ買いになっている（急上昇/連続陽線/VWAP乖離/高値圏/上ヒゲ/出来高ピークアウトのうち3つ以上、
    # かつ急上昇か連続陽線を含む）
    if chase_count >= 3 and (fast15 or green3):
        return "CHASE", fl
    if extended:
        return "EXTENDED", fl
    if f.get("vwapLoss"):
        return "VWAP_LOSS", fl
    up = _uptrend(f)
    pb_conditions = {
        "uptrend": up,
        "higher_low": bool(f.get("higherLows")),
        "near_support": bool(f.get("vwapBounce") or (f.get("aboveVwap") and (f.get("vwapDistPct") or 9) <= 0.8)),
        "pullback_volume_down": bool(f.get("pullbackVolDown")),
        "reversal_candle": bool(f.get("lowerWick", 0) >= 0.3 or (f.get("bull") and (f.get("chg5m") or 0) > 0)),
        "shallow": dh is not None and -2.0 <= dh <= -0.15,
    }
    fl["pullback_conditions"] = pb_conditions
    if up and sum(1 for k, x in pb_conditions.items() if x) >= 5 and f.get("aboveVwap") is not False and not fast15:
        return "PULLBACK_READY", fl
    if f.get("vwapReclaim") and (f.get("bull") or (f.get("volRatioLast") or 0) >= 1.0) and not fast15:
        return "VWAP_RECLAIM", fl
    pre = f.get("breakoutLevel")
    early = bool(f.get("aboveVwap") is not False and not vwap_big and not fast15 and not green3
                 and (f.get("higherLows") or (f.get("slope3") or 0) > 0)
                 and (f.get("volRatioLast") or 0) >= 1.1
                 and pre and (-0.6 <= ((f["cur"] / pre - 1) * 100) <= 0.4))
    fl["early_breakout"] = early
    if early and not (f.get("brokeRecently") and f.get("breakoutHeld") and (f["cur"] / pre - 1) * 100 > 0.15):
        return "EARLY_BREAKOUT", fl
    if f.get("brokeRecently") and f.get("breakoutHeld") and not vwap_big:
        return "BREAKOUT_CONFIRMED", fl
    if f.get("range6Pct") is not None and f["range6Pct"] <= 0.9 and near_high is False and (dh or -9) >= -1.2 \
            and f.get("aboveVwap") is not False:
        return "BASE_BUILDING", fl
    if f.get("range6Pct") is not None and f["range6Pct"] <= 0.9 and near_high and f.get("aboveVwap") is not False:
        return "BASE_BUILDING", fl
    return "NEUTRAL", fl


_PATTERN_TIMING = {"EARLY_BREAKOUT": 25, "BREAKOUT_CONFIRMED": 12, "PULLBACK_READY": 30, "VWAP_RECLAIM": 22,
                   "BASE_BUILDING": 5, "NEUTRAL": 0, "CHASE": -35, "EXTENDED": -22, "FAILED_BREAKOUT": -40,
                   "EXHAUSTION": -35, "VWAP_LOSS": -25, "UNKNOWN": 0}

# Learning Rule（既存DBのtrade_rules）から拾う「エントリー失敗パターン」→ チャート特徴のタグ。
RULE_KEYWORDS = (("CHASE", r"高値追い|高値掴み|追いかけ|飛びつき"),
                 ("SIDEWAYS_AFTER_RISE", r"上昇後.{0,4}横ばい|横横|横ばいで撤退|上昇後の横"),
                 ("SMALL_DECLINE_EXIT", r"微下落|微損|じわじわ下"),
                 ("FAILED_BREAKOUT", r"ブレイク失敗|ダマシ|failed\s*break|高値更新後.{0,6}失速"),
                 ("LATE_ENTRY", r"エントリー遅れ|entry遅れ|遅いエントリー|上昇確認後に入"))
_RULE_TAG_FEATURES = {"CHASE": ("CHASE", "EXTENDED"), "SIDEWAYS_AFTER_RISE": ("EXTENDED", "EXHAUSTION", "CHASE"),
                      "SMALL_DECLINE_EXIT": ("EXTENDED", "EXHAUSTION"), "FAILED_BREAKOUT": ("FAILED_BREAKOUT",),
                      "LATE_ENTRY": ("EXTENDED", "CHASE")}


def derive_rule_penalties(rules):
    """既存のLearning Rule（trade_rules行のリスト：rule_text/status/confidence）から、チャート
    パターン別のentry_penaltyを導く。ハードコードした固定ルールではなく、DBのルール本文に
    含まれる失敗パターン（高値追い・上昇後横ばい・微下落撤退・breakout失敗・エントリー遅れ）を
    キーワードで検出する。ACTIVE/HIGHは5点、それ以外(TESTING/MEDIUM等)は3点、合計上限8点。
    戻り値: {"byPattern": {pattern: points}, "sources": [{ruleId, tag, points}]}"""
    by_pat, sources = {}, []
    for r in rules or []:
        text = str(r.get("rule_text") or "")
        pts = 5 if (r.get("status") == "ACTIVE" or r.get("confidence") == "HIGH") else 3
        for tag, rx in RULE_KEYWORDS:
            if re.search(rx, text):
                sources.append({"ruleId": r.get("id"), "tag": tag, "points": pts})
                for pat in _RULE_TAG_FEATURES[tag]:
                    by_pat[pat] = min(8, max(by_pat.get(pat, 0), pts))
    return {"byPattern": by_pat, "sources": sources[:10]}


def evaluate_chart_context(bars, quote=None, vwap=None, day_high=None, day_low=None, existing_signals=None,
                           minutes_since_open=None, rule_penalties=None):
    """Chart Context Engine本体（純粋関数）。
    existing_signals: 既存signalの辞書（structure='higher_highs'/'mixed'…, aboveVwap, overheat(bool),
    momentumState, recentHighBreak(bool)）。併用するが、CHASE等のチャート判定を覆さない。
    戻り値: dict(pattern, entry_timing_score, confidence, reasons, penalties, features, decision_hint)。"""
    existing_signals = existing_signals or {}
    nb = normalize_bars(bars)
    n = len(nb["closes"]) if nb else 0
    conf = confidence_for(n, minutes_since_open)
    if conf == "UNKNOWN":
        return {"pattern": "UNKNOWN", "entry_timing_score": None, "confidence": "UNKNOWN", "barCount": n,
                "reasons": ["5分足が3本未満のためチャート判定不能（強いENTRY判定は出さない）"], "penalties": [],
                "features": None, "earlySession": minutes_since_open is not None and minutes_since_open < EARLY_SESSION_MINUTES}
    f = compute_features(bars, quote, vwap, day_high, day_low)
    if f is None:
        return {"pattern": "UNKNOWN", "entry_timing_score": None, "confidence": "UNKNOWN", "barCount": n,
                "reasons": ["5分足データ不正"], "penalties": [], "features": None, "earlySession": False}
    pattern, flags = classify_pattern(f)
    score = 50.0
    reasons, penalties = [], []
    base = _PATTERN_TIMING.get(pattern, 0)
    score += base
    (reasons if base >= 0 else penalties).append(f"パターン {pattern}（{base:+d}）")
    def add(cond, pts, text):
        nonlocal score
        if cond:
            score += pts
            (reasons if pts >= 0 else penalties).append(f"{text}（{pts:+d}）")
    add(f.get("higherLows") and pattern not in BAD_PATTERNS, 6, "直近の安値切り上げ")
    add((f.get("breakoutVolRatio") or 0) >= 1.5 and pattern in ("BREAKOUT_CONFIRMED", "EARLY_BREAKOUT"), 6, f"ブレイク時の出来高増加x{f.get('breakoutVolRatio')}")
    add(f.get("vwapBounce") and pattern not in BAD_PATTERNS, 6, "VWAP反発")
    add(f.get("lowerWick", 0) >= 0.3 and (f.get("aboveVwap") is not False) and pattern not in BAD_PATTERNS, 5, "下ヒゲ反発")
    add(f.get("upperWick", 0) >= UPPER_WICK_LONG, -8, f"直近足に長い上ヒゲ（{f['upperWick']:.2f}）")
    add(f.get("consecGreen", 0) >= 3 and (f.get("greenRunGainPct") or 0) >= 0.6, -8,
        f"{f.get('consecGreen')}本連続陽線後（+{(f.get('greenRunGainPct') or 0):.1f}%）")
    add((f.get("vwapDistPct") or 0) >= VWAP_DIST_EXTENDED_PCT, -8, f"VWAP乖離+{(f.get('vwapDistPct') or 0):.1f}%")
    add(f.get("volPeakout") and pattern != "PULLBACK_READY", -6, "出来高ピークアウト")
    # 既存signalとの併用（既存BREAKOUTがtrueでもCHASE等のチャート判定が優先される）
    add(existing_signals.get("structure") == "higher_highs" and pattern not in BAD_PATTERNS, 4, "既存：5分足高値切り上げ")
    add(existing_signals.get("overheat"), -6, "既存：高値からの乖離（過熱）")
    add(existing_signals.get("momentumState") == "MOMENTUM_DECAY", -6, "既存：MOMENTUM_DECAY")
    # Learning Rule由来のentry_penalty（DBのルール本文から導出）
    rp = (rule_penalties or {}).get("byPattern", {})
    if rp.get(pattern):
        score -= rp[pattern]
        penalties.append(f"Learning Rule（過去のエントリー失敗パターン）（-{rp[pattern]}）")
    # 数値の説明
    if f.get("chg15m") is not None and pattern in ("CHASE", "EXTENDED", "EXHAUSTION"):
        reasons.insert(0, f"15分で{f['chg15m']:+.1f}%")
    if f.get("vwapDistPct") is not None and pattern in ("CHASE", "EXTENDED"):
        reasons.append(f"VWAP乖離{f['vwapDistPct']:+.1f}%")
    # 信頼度が低いときは強い判断に寄せない（50から中立側へ縮める）
    if conf == "LOW":
        score = 50 + (score - 50) * 0.5
        penalties.append("5分足が3〜5本のみ：信頼度LOW（スコアを中立側へ縮小）")
    early = minutes_since_open is not None and minutes_since_open < EARLY_SESSION_MINUTES
    if early:
        penalties.append("寄り直後（〜9:15）：値動き・VWAPが不安定なため信頼度を下げて判定")
    return {"pattern": pattern, "entry_timing_score": int(round(max(0.0, min(100.0, score)))), "confidence": conf,
            "barCount": n, "reasons": reasons, "penalties": penalties, "flags": flags, "earlySession": early,
            "features": {k: (round(v, 3) if isinstance(v, float) else v) for k, v in f.items()
                         if k in ("chg5m", "chg15m", "chg30m", "vwapDistPct", "consecGreen", "consecRed", "upperWick",
                                  "lowerWick", "bodyRatio", "breakoutVolRatio", "volRatioLast", "distFromDayHighPct",
                                  "slope3", "slope6", "slope12", "higherHighs", "higherLows", "aboveVwap", "vwapReclaim",
                                  "vwapLoss", "opensApprox")}}


# ---------------------------------------------------------------- 銘柄の強さ（タイミングと分離）

STRENGTH_COMPONENTS = ("momentum", "marketRelative", "volume", "autoRs", "autoSector", "catalyst")
_STRENGTH_MAX = 20 + 15 + 10 + 10 + 5 + 5


def stock_strength_score(comp):
    """既存entry_scoreの内訳(comp)のうち、ENTRYタイミングと無関係な「銘柄自体の強さ」だけを0-100へ
    正規化する（当日上昇率・対市場RS・出来高・AUTO_RS/セクター先導・材料、＋過去経験の加減点と
    イベントリスク減点）。VWAP位置・5分足構造・過熱・値幅余地はタイミング側に属するため含めない。"""
    if not comp:
        return None
    s = sum(comp.get(k) or 0 for k in STRENGTH_COMPONENTS)
    s += (comp.get("experienceBonus") or 0) + (comp.get("experiencePenalty") or 0) + (comp.get("riskEvent") or 0)
    return int(round(max(0.0, min(100.0, s / _STRENGTH_MAX * 100.0))))


def entry_decision(strength, cc):
    """強さ×タイミング→ENTRY判定。強い銘柄でもタイミングが悪ければ STOCK STRONG / ENTRY WAIT。
    ENTRY_READY / WATCH / WAIT_PULLBACK / WAIT_BREAKOUT / NO_ENTRY_CHASE / NO_ENTRY_FAILED_BREAK"""
    pattern, timing, conf = cc.get("pattern"), cc.get("entry_timing_score"), cc.get("confidence")
    if pattern == "FAILED_BREAKOUT":
        return "NO_ENTRY_FAILED_BREAK"
    if pattern in ("CHASE", "EXTENDED", "EXHAUSTION"):
        return "NO_ENTRY_CHASE"
    if pattern in ("UNKNOWN",) or timing is None:
        return "WATCH"
    if pattern == "VWAP_LOSS":
        return "WATCH"
    if (strength or 0) >= ENTRY_MIN_STRENGTH and timing >= ENTRY_MIN_TIMING and conf in ("HIGH", "MEDIUM"):
        return "ENTRY_READY"
    if (strength or 0) >= ENTRY_MIN_STRENGTH:
        dh = ((cc.get("features") or {}).get("distFromDayHighPct"))
        return "WAIT_BREAKOUT" if (dh is not None and dh >= -0.8) else "WAIT_PULLBACK"
    return "WATCH"


def apply_chart_gate(entry_state, strength, entry_score, cc):
    """既存のENTRY_STATEにチャート判定を重ねる。既存signalは削除せず、安全側の格下げと、条件が
    揃った時だけの昇格を行う。戻り値: (新state, 理由リスト)。
      ・NOW_BUYABLE/ENTRY_READYでも FAILED_BREAKOUT→WATCH、CHASE/EXTENDED/EXHAUSTION→WAIT_PULLBACK、
        VWAP_LOSS→WATCH、タイミング低(<40、信頼度MEDIUM以上)→WAIT_PULLBACK
      ・チャート不明(UNKNOWN)のNOW_BUYABLEはENTRY_READYまで（データ不足時に強い判定を出さない）
      ・WAIT_PULLBACK/WAIT_BREAKOUT→ENTRY_READY：PULLBACK_READY/EARLY_BREAKOUT/VWAP_RECLAIM、
        タイミング>=70、信頼度MEDIUM以上、強さ>=50、entry_score>=45、寄り直後でない場合のみ。"""
    pattern, timing, conf = cc.get("pattern"), cc.get("entry_timing_score"), cc.get("confidence")
    why = []
    if entry_state in ("NOW_BUYABLE", "ENTRY_READY"):
        if pattern == "FAILED_BREAKOUT":
            return "WATCH", ["チャート：高値更新後に失速（FAILED_BREAKOUT）→ENTRY禁止"]
        if pattern in ("CHASE", "EXTENDED", "EXHAUSTION"):
            return "WAIT_PULLBACK", [f"チャート：{pattern}（走りすぎ）→押し目待ち"]
        if pattern == "VWAP_LOSS":
            return "WATCH", ["チャート：VWAP割れ→ENTRY見送り"]
        if timing is not None and conf in ("HIGH", "MEDIUM") and timing < 40:
            return "WAIT_PULLBACK", [f"チャート：ENTRYタイミング低（{timing}）→押し目待ち"]
        if conf == "UNKNOWN" and entry_state == "NOW_BUYABLE":
            return "ENTRY_READY", ["5分足不足：強いENTRY判定を出さない"]
        return entry_state, why
    if entry_state in ("WAIT_PULLBACK", "WAIT_BREAKOUT"):
        if (pattern in GOOD_PATTERNS and timing is not None and timing >= UPGRADE_MIN_TIMING
                and conf in ("HIGH", "MEDIUM") and (strength or 0) >= ENTRY_MIN_STRENGTH
                and (entry_score or 0) >= 45 and not cc.get("earlySession")):
            return "ENTRY_READY", [f"チャート：{pattern}・ENTRYタイミング{timing}でENTRY候補へ"]
    return entry_state, why
