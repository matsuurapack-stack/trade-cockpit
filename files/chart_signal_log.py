# Phase C shadow運用：判定ログ・事後リターン・日次集計の純粋ロジック（DB/ネットワークに触れない）。
#
# 重要（hindsight bias禁止）：ここで扱う事後価格(+5/+15/+30分)は「記録と集計」専用。
# chart_context / _compute_price_dependent_entry_fields / TOP5選定は、このモジュールも
# chart_signal_log テーブルも一切読まない（一方向：判定 → ログ）。テストで固定している。
#
# しきい値の変更はしない（指示：実市場で30〜50シグナル溜まるまで凍結）。ここにあるのは
# 「結果の分類」用の集計定義だけで、判定ロジックには使わない。

import datetime

HORIZONS_MIN = (5, 15, 30)
OUTCOME_TOLERANCE_SEC = 240        # 目標時刻からこの秒数以内に取れた価格だけを事後価格として採用
# ログ件数抑制（Neon容量）。「状態変化・TOP5採用/脱落・legacyとchartの差分・優先パターン」は必ず残し、
# 同一状態の継続は特徴量が実質動いた時か、ハートビート時だけ残す。
PRIO_HEARTBEAT_SEC = 900           # 優先状態（ENTRY_READY/CHASE系/FAILED_BREAKOUT/PULLBACK_READY/VWAP_RECLAIM/TOP5/差分）
IDLE_HEARTBEAT_SEC = 3600          # それ以外のWATCH/WAIT系の継続
PATTERN_FLAP_MIN_SEC = 600         # 優先パターンの出入りが5分足ごとに揺れる場合の最小間隔（遷移タイプ・状態変化は別枠で即記録）
MATERIAL_MIN_GAP_SEC = 300         # 特徴量が動いた場合でも同一銘柄は5分以内に再記録しない（同一bar内の重複防止）
MATERIAL_TIMING = 20               # entry_timing の変化（点）
MATERIAL_VWAP_DIST = 1.0           # VWAP乖離の変化（%pt）
MATERIAL_CHG15 = 2.0               # 15分変化率の変化（%pt）
# 記録対象：TOP5系リスト、または旧/新どちらかが「注目状態」。WEAK/INVALID等の圏外は記録しない。
LOGGED_STATES = ("NOW_BUYABLE", "ENTRY_READY", "WATCH", "WAIT_PULLBACK", "WAIT_BREAKOUT", "CHASE_RISK")
PRIORITY_PATTERNS = ("CHASE", "EXTENDED", "EXHAUSTION", "FAILED_BREAKOUT", "PULLBACK_READY", "VWAP_RECLAIM")
RECOVERY_PATTERNS = ("PULLBACK_READY", "VWAP_RECLAIM", "EARLY_BREAKOUT", "BREAKOUT_CONFIRMED")
TRANSITION_WINDOW_SEC = 90 * 60    # CHASE/FAILED_BREAKOUT を起点とした遷移として数える最大経過時間（定義。判定には使わない）

# 結果分類（集計専用）：15分後リターンで 下落 / 横横 / 上昇 に分ける
DECLINE_PCT = -0.3
UP_PCT = 0.7                       # backtest_chart_context の QUICK_WIN と同じ
FAILED_BREAK_MISJUDGE_PCT = 4.0    # 「FAILED_BREAKOUT判定→30分後+4%以上」を誤判定とみなす（指示）

# Phase D（Movement Potential）：値幅を見た推奨（shadow）で「注目すべき」状態
MOVEMENT_NOTABLE_RECS = ("ENTRY_READY", "TOO_LATE", "PRE_BREAKOUT", "WATCH_EXPANDING", "BLOCKED_LOW_ACTIVITY")
MILESTONES = (("first_movement_at", "値幅拡大の始まり（movement>=65またはEXPANDING）"),
              ("first_expanding_at", "EXPANDING"), ("first_pre_breakout_at", "PRE_BREAKOUT"),
              ("first_early_breakout_at", "EARLY_BREAKOUT"), ("first_chase_at", "CHASE/EXTENDED/EXHAUSTION"),
              ("first_movement_entry_at", "movement-aware ENTRY_READY"),
              ("first_radar_at", "RADAR_SURGE/EXPANDING/PRE_BREAKOUT（初動監視）"), ("first_radar_surge_at", "RADAR_SURGE"),
              ("first_rolling_at", "Rolling Radar hot（SURGE/SINGLE_BAR_SURGE/PRE_BREAKOUT）"),
              ("first_single_bar_surge_at", "SINGLE_BAR_SURGE"), ("first_rolling_weak_at", "RADAR_WEAK"))
RADAR_NOTABLE = ("RADAR_SURGE", "RADAR_EXPANDING", "RADAR_PRE_BREAKOUT")
ROLLING_HOT = ("ROLLING_SURGE", "SINGLE_BAR_SURGE", "ROLLING_PRE_BREAKOUT")
ROLLING_NOTABLE = ROLLING_HOT + ("ROLLING_EXPANDING", "RADAR_WEAK")
# Rolling Radar検出の後始末（false positiveの定義。集計専用で判定には使わない）：30分後が+0.5%以下、かつ30分内の最大上昇(MFE)が+1.0%以下
FP_RET_30M = 0.5
FP_MFE_30M = 1.0

ENTRY_STATES = ("NOW_BUYABLE", "ENTRY_READY")
CHASE_PATTERNS = ("CHASE", "EXTENDED", "EXHAUSTION")


def _num(x):
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


def build_signal_record(user_id, cand, now, source, top5_codes=None):
    """候補dict（スキャン/軽量再スコア後のpool要素）→ 保存用レコード。チャート判定が無い候補はNone。"""
    if not isinstance(cand, dict) or not cand.get("code"):
        return None
    cc = cand.get("chartContext")
    price = _num(cand.get("current"))
    if not cc or price is None:
        return None
    ft = cc.get("features") or {}
    vdist = _num(ft.get("vwapDistPct"))
    vwap = _num(cand.get("vwap"))
    if vwap is None and vdist is not None and price:
        vwap = round(price / (1 + vdist / 100.0), 2)
    dist_hi = _num(ft.get("distFromDayHighPct"))
    day_high = round(price / (1 + dist_hi / 100.0), 2) if (dist_hi is not None and price) else None
    top5 = top5_codes or {}
    return {
        "transition_type": None, "transition_origin": None,
        "rolling_state": cand.get("rollingState"), "rolling_score": cand.get("rollingScore"),
        "radar_state": cand.get("radarState"), "early_momentum_score": cand.get("earlyMomentumScore"),
        "spread_pct": cand.get("spreadPct"), "atr5_pct": cand.get("atr5Pct"),
        "movement_score": cand.get("movementScore"), "recent_activity": cand.get("recentActivityScore"),
        "activity_state": cand.get("activityState"), "pre_breakout": cand.get("preBreakout"),
        "too_late": cand.get("tooLate"), "momentum_mode": cand.get("momentumMode"),
        "recommended_stop": (cand.get("recommendedStop") or {}).get("price"),
        "stop_distance_pct": (cand.get("recommendedStop") or {}).get("distancePct"),
        "risk_reward": (cand.get("riskReward") or {}).get("rr"), "movement_recommendation": cand.get("movementRecommendation"),
        "movement": {"breakdown": cand.get("movementBreakdown"), "reasons": cand.get("movementReasons"),
                     "tooLateReasons": cand.get("tooLateReasons"), "momentumFlags": cand.get("momentumFlags"),
                     "stop": cand.get("recommendedStop"), "riskReward": cand.get("riskReward"),
                     "features": cand.get("movementFeatures"), "entryReason": cand.get("movementEntryReason"),
                     "radar": {"state": cand.get("radarState"), "score": cand.get("earlyMomentumScore"),
                               "confidence": cand.get("radarConfidence"), "reasons": cand.get("radarReasons"),
                               "features": cand.get("radarFeatures")},
                     "rolling": {"state": cand.get("rollingState"), "baseState": cand.get("rollingBaseState"),
                                 "score": cand.get("rollingScore"), "reasons": cand.get("rollingReasons"),
                                 "confirmations": cand.get("rollingConfirm"), "features": cand.get("rollingFeatures")}},
        "user_id": user_id, "logged_at": now, "code": str(cand["code"]), "name": cand.get("name"), "source": source,
        "current_price": price, "day_high": day_high,
        "stock_strength": cand.get("stockStrengthScore"), "entry_timing": cc.get("entry_timing_score"),
        "chart_pattern": cc.get("pattern"), "chart_confidence": cc.get("confidence"),
        "legacy_entry_state": cand.get("legacyEntryState") or cand.get("entryStatePreChart"),
        "chart_entry_state": cand.get("chartEntryState") or cand.get("entryState"),
        "entry_decision": cand.get("entryDecision"),
        "vwap": vwap, "vwap_distance": vdist, "change_15m": _num(ft.get("chg15m")),
        "consecutive_green": ft.get("consecGreen"),
        "upper_wick_ratio": _num(ft.get("upperWickAvg3") if ft.get("upperWickAvg3") is not None else ft.get("upperWick")),
        "breakout_volume_ratio": _num(ft.get("breakoutVolRatio")),
        "reasons": list(cc.get("reasons") or []), "penalties": list(cc.get("penalties") or []),
        "features": ft,
        "context": {"marketRS": cand.get("marketRS"), "changePct": cand.get("changePct"), "barCount": cc.get("barCount"),
                    "scoreBreakdown": cand.get("scoreBreakdown"), "entryScore": cand.get("entryScore"),
                    "top5": sorted(k for k, codes in top5.items() if cand["code"] in codes)},
    }


def movement_notable(rec):
    return bool(rec.get("movement_recommendation") in MOVEMENT_NOTABLE_RECS or rec.get("activity_state") == "EXPANDING"
                or rec.get("pre_breakout") or rec.get("momentum_mode") or rec.get("radar_state") in RADAR_NOTABLE
                or rec.get("rolling_state") in ROLLING_NOTABLE)


def update_milestones(mem, key, rec, now):
    """銘柄×日ごとに「最初にその状態になった時刻」を記録し、今回新しく付いたマイルストーンのリストを返す。
    アキッパ型（後でストップ高まで行った銘柄）が、いつEXPANDING/PRE_BREAKOUT/EARLY_BREAKOUT/CHASEになったかを
    後から検証するための記録（未来データは使わない）。"""
    ms = mem.get(key)
    if ms is None or ms.get("_date") != now.date():
        ms = {"_date": now.date()}
        mem[key] = ms
    new = []

    def mark(name, cond):
        if cond and name not in ms:
            ms[name] = now.isoformat()
            new.append(name)
    mark("first_movement_at", (rec.get("movement_score") or 0) >= 65 or rec.get("activity_state") == "EXPANDING")
    mark("first_expanding_at", rec.get("activity_state") == "EXPANDING")
    mark("first_pre_breakout_at", bool(rec.get("pre_breakout")))
    mark("first_early_breakout_at", rec.get("chart_pattern") == "EARLY_BREAKOUT")
    mark("first_chase_at", rec.get("chart_pattern") in CHASE_PATTERNS)
    mark("first_movement_entry_at", rec.get("movement_recommendation") == "ENTRY_READY")
    mark("first_radar_at", rec.get("radar_state") in RADAR_NOTABLE)
    mark("first_radar_surge_at", rec.get("radar_state") == "RADAR_SURGE")
    mark("first_rolling_at", rec.get("rolling_state") in ROLLING_HOT)
    mark("first_single_bar_surge_at", rec.get("rolling_state") == "SINGLE_BAR_SURGE")
    mark("first_rolling_weak_at", rec.get("rolling_state") == "RADAR_WEAK")
    rec.setdefault("context", {})["milestones"] = {k: v for k, v in ms.items() if not k.startswith("_")}
    rec["context"]["newMilestones"] = new
    return new


def is_priority(rec):
    ctx = rec.get("context") or {}
    return bool(ctx.get("top5") or movement_notable(rec) or rec.get("legacy_entry_state") != rec.get("chart_entry_state")
                or rec.get("chart_entry_state") in ENTRY_STATES or rec.get("legacy_entry_state") in ENTRY_STATES
                or "CHASE_RISK" in (rec.get("chart_entry_state"), rec.get("legacy_entry_state"))
                or rec.get("chart_pattern") in PRIORITY_PATTERNS)


def is_loggable(rec):
    if (rec.get("context") or {}).get("top5") or movement_notable(rec) or (rec.get("context") or {}).get("newMilestones"):
        return True
    return rec.get("legacy_entry_state") in LOGGED_STATES or rec.get("chart_entry_state") in LOGGED_STATES


def last_state(rec, now):
    """直前に記録した状態のスナップショット（should_logの比較用）。"""
    ft = rec.get("features") or {}
    return {"pattern": rec["chart_pattern"], "chart": rec["chart_entry_state"], "legacy": rec["legacy_entry_state"],
            "at": now, "timing": rec.get("entry_timing"), "vdist": rec.get("vwap_distance"), "chg15": rec.get("change_15m"),
            "prio": is_priority(rec), "top5": tuple((rec.get("context") or {}).get("top5") or ()),
            "mrec": rec.get("movement_recommendation"), "act": rec.get("activity_state"), "pre": bool(rec.get("pre_breakout")),
            "radar": rec.get("radar_state"), "rolling": rec.get("rolling_state")}


def _moved(a, b, th):
    return a is not None and b is not None and abs(a - b) >= th


def should_log(last, rec, now):
    """last: last_state()の戻り値。状態変化・TOP5採用/脱落・差分発生は即記録。同一状態の継続は
    特徴量が実質動いた時（5分以上空いて）かハートビート時のみ。"""
    if not last:
        return True
    prio = is_priority(rec)
    if (last["chart"], last["legacy"]) != (rec["chart_entry_state"], rec["legacy_entry_state"]) and (prio or last.get("prio")):
        return True       # 優先状態へ/からの変化。WATCH↔WAIT_PULLBACK等の非優先間の揺れは継続扱い（特徴量が動けば記録）
    top5 = tuple((rec.get("context") or {}).get("top5") or ())
    if top5 != last.get("top5"):
        return True
    if (rec.get("context") or {}).get("newMilestones"):
        return True       # 最初にEXPANDING/PRE_BREAKOUT/EARLY_BREAKOUT/CHASEになった瞬間は必ず残す
    if (last.get("mrec"), last.get("act"), last.get("pre"), last.get("radar"), last.get("rolling")) != (
            rec.get("movement_recommendation"), rec.get("activity_state"), bool(rec.get("pre_breakout")), rec.get("radar_state"),
            rec.get("rolling_state")) \
            and (prio or last.get("prio")):
        return True       # 値幅を見た推奨・活動状態・PRE_BREAKOUTの変化
    if last["pattern"] != rec["chart_pattern"] and (prio or last.get("prio"))             and (now - last["at"]).total_seconds() >= PATTERN_FLAP_MIN_SEC:
        return True       # 優先パターンへ/から変わった瞬間（BASE_BUILDING↔NEUTRAL等の非優先間の揺れは継続扱い）
    age = (now - last["at"]).total_seconds()
    if age < MATERIAL_MIN_GAP_SEC:
        return False
    material = (_moved(last.get("timing"), rec.get("entry_timing"), MATERIAL_TIMING)
                or _moved(last.get("vdist"), rec.get("vwap_distance"), MATERIAL_VWAP_DIST)
                or _moved(last.get("chg15"), rec.get("change_15m"), MATERIAL_CHG15))
    if prio:
        return material or age >= PRIO_HEARTBEAT_SEC
    return material or age >= IDLE_HEARTBEAT_SEC


def detect_transition(mem, key, rec, now):
    """状態が変わった「その瞬間」に厳密な遷移タイプを返す（履歴列からの後付け推測はしない）。
    mem: 呼び出し側が保持するdict（key=(user,code)ごとの観測メモリ。ここで更新する）。
    戻り値: (遷移タイプのリスト, origin)。origin＝押し目/エントリー復帰の起点（CHASE/FAILED_BREAKOUT/None）。
      CHASE_TO_PULLBACK_READY        直近90分内にCHASE/EXTENDED/EXHAUSTIONが出た後、PULLBACK_READYになった
      PULLBACK_READY_TO_ENTRY_READY  PULLBACK_READYのままENTRY系stateへ入った（originに起点）
      FAILED_BREAKOUT_TO_RECOVERY    直近90分内のFAILED_BREAKOUT後、回復系パターン（押し目/VWAP奪回/ブレイク）になった
      ENTRY_READY_TO_CHASE           ENTRY系stateだった銘柄がCHASE/EXTENDED/EXHAUSTIONになった
      ENTRY_READY_TO_FAILED_BREAKOUT ENTRY系stateだった銘柄がFAILED_BREAKOUTになった"""
    pat, chart = rec["chart_pattern"], rec["chart_entry_state"]
    m = mem.get(key)
    if m is None or m.get("date") != now.date():
        m = {"date": now.date(), "pattern": None, "chart": None, "chase_at": None, "chase_pb": False, "fb_at": None}
        mem[key] = m

    def within(ts):
        return ts is not None and (now - ts).total_seconds() <= TRANSITION_WINDOW_SEC

    out, origin = [], None
    prev_entry = m["chart"] in ENTRY_STATES
    is_entry = chart in ENTRY_STATES
    if m["pattern"] is not None:
        if prev_entry and pat in CHASE_PATTERNS and m["pattern"] not in CHASE_PATTERNS:
            out.append("ENTRY_READY_TO_CHASE")
        if prev_entry and pat == "FAILED_BREAKOUT" and m["pattern"] != "FAILED_BREAKOUT":
            out.append("ENTRY_READY_TO_FAILED_BREAKOUT")
    if pat == "PULLBACK_READY" and m["pattern"] != "PULLBACK_READY":
        if within(m["chase_at"]) and not m["chase_pb"]:
            out.append("CHASE_TO_PULLBACK_READY")
            m["chase_pb"] = True
    if pat == "PULLBACK_READY" and is_entry and not prev_entry:
        out.append("PULLBACK_READY_TO_ENTRY_READY")
        origin = "CHASE" if within(m["chase_at"]) else ("FAILED_BREAKOUT" if within(m["fb_at"]) else None)
        m["chase_at"] = None
    if within(m["fb_at"]) and pat in RECOVERY_PATTERNS and m["pattern"] not in RECOVERY_PATTERNS:
        out.append("FAILED_BREAKOUT_TO_RECOVERY")
        origin = origin or "FAILED_BREAKOUT"
        m["fb_at"] = None
    if pat in CHASE_PATTERNS:
        if not within(m["chase_at"]) or m["pattern"] not in CHASE_PATTERNS:
            m["chase_pb"] = False
        m["chase_at"] = now
    if pat == "FAILED_BREAKOUT":
        m["fb_at"] = now
    m["pattern"], m["chart"] = pat, chart
    return out, origin


def return_pct(entry, later):
    e, l = _num(entry), _num(later)
    if not e or l is None:
        return None
    return round((l / e - 1) * 100, 3)


def classify_return(ret):
    if ret is None:
        return None
    if ret >= UP_PCT:
        return "UP"
    if ret <= DECLINE_PCT:
        return "DECLINE"
    return "FLAT"


def due_horizons(logged_at, now, have):
    """今回価格を取るべき水準(分)。have=既に取得済みの水準集合。目標時刻+許容秒を過ぎたものは取り逃し扱い。"""
    out, missed = [], []
    age = (now - logged_at).total_seconds()
    for h in HORIZONS_MIN:
        if h in have:
            continue
        if age < h * 60:
            continue
        (out if age <= h * 60 + OUTCOME_TOLERANCE_SEC else missed).append(h)
    return out, missed


def outcome_finished(logged_at, now):
    return (now - logged_at).total_seconds() > max(HORIZONS_MIN) * 60 + OUTCOME_TOLERANCE_SEC


def _with_ret(rows):
    out = []
    for r in rows:
        d = dict(r)
        for h in HORIZONS_MIN:
            d[f"ret_{h}m"] = return_pct(r.get("current_price"), r.get(f"price_{h}m"))
        out.append(d)
    return out


def _is_entry(state):
    return state in ENTRY_STATES


def transition_chains(rows):
    """コード別に、パターン／chart状態の時系列を（連続重複を畳んで）返す。
    CHASE → WAIT_PULLBACK → PULLBACK_READY → ENTRY_READY の完走を検出する。"""
    by = {}
    for r in sorted(rows, key=lambda x: x["logged_at"]):
        by.setdefault(r["code"], []).append(r)
    chains, full = {}, []
    for code, rs in by.items():
        seq = []
        for r in rs:
            step = (r["chart_pattern"], r["chart_entry_state"])
            if not seq or seq[-1] != step:
                seq.append(step)
        chains[code] = seq
        pats = [p for p, _ in seq]
        states = [s for _, s in seq]
        try:
            i = next(i for i, p in enumerate(pats) if p in CHASE_PATTERNS)
            j = next(j for j in range(i + 1, len(seq)) if states[j] == "WAIT_PULLBACK" or pats[j] == "PULLBACK_READY")
            k = next(k for k in range(j, len(seq)) if pats[k] == "PULLBACK_READY")
            m = next(m for m in range(k, len(seq)) if states[m] in ENTRY_STATES)
            full.append({"code": code, "steps": seq[i:m + 1]})
        except StopIteration:
            pass
    edges = {}
    for seq in chains.values():
        for a, b in zip(seq, seq[1:]):
            key = f"{a[0]}→{b[0]}"
            edges[key] = edges.get(key, 0) + 1
    return {"edges": edges, "completed_chase_to_entry": full, "codes": len(chains)}


def _pctile(vals, q):
    v = sorted(vals)
    if not v:
        return None
    k = (len(v) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return round(v[lo] + (v[hi] - v[lo]) * (k - lo), 1)


def _parse_dt(x):
    if isinstance(x, datetime.datetime):
        return x
    try:
        return datetime.datetime.fromisoformat(x) if x else None
    except (TypeError, ValueError):
        return None


def outcome_quality(rows):
    """+5/+15/+30分の due_at / fetched_at(at_Xm) / actual_delay_sec の分布。at_Xm列が無い行
    （列追加前の行）は遅延を測れないため対象外にして、その件数をmeasurable/priceで示す。"""
    out = {}
    for h in HORIZONS_MIN:
        delays, priced, missed = [], 0, 0
        for r in rows:
            logged, fetched = _parse_dt(r.get("logged_at")), _parse_dt(r.get(f"at_{h}m"))
            if r.get(f"price_{h}m") is not None:
                priced += 1
            elif r.get("outcome_done"):
                missed += 1
            if logged and fetched:
                delays.append((fetched - logged).total_seconds() - h * 60)   # due_at = logged_at + h分
        out[f"{h}m"] = {"priced": priced, "missed(done but no price)": missed, "measurable": len(delays),
                        "delay_sec_median": _pctile(delays, 0.5), "delay_sec_p95": _pctile(delays, 0.95),
                        "delay_sec_max": round(max(delays), 1) if delays else None}
    return out


def transition_summary(rows):
    """状態が変わった瞬間に記録したtransition_type別の件数・origin・15/30分後の平均リターン。"""
    by = {}
    for r in rows:
        for t in [x for x in (r.get("transition_type") or "").split(",") if x]:
            d = by.setdefault(t, {"count": 0, "origins": {}, "_r15": [], "_r30": []})
            d["count"] += 1
            o = r.get("transition_origin") or "NONE"
            d["origins"][o] = d["origins"].get(o, 0) + 1
            for h, key in ((15, "_r15"), (30, "_r30")):
                if r.get(f"ret_{h}m") is not None:
                    d[key].append(r[f"ret_{h}m"])
    for d in by.values():
        for key, name in (("_r15", "avg_ret_15m"), ("_r30", "avg_ret_30m")):
            v = d.pop(key)
            d[name] = round(sum(v) / len(v), 3) if v else None
            d["n_" + name[-3:]] = len(v)
    return by


def _rate(num, den):
    return None if not den else round(num / den, 3)


def summarize_day(rows):
    """日次集計。rowsはchart_signal_logの行（dict）。単位：シグナル行（状態変化/ハートビート）ではなく
    「コード×連続状態」で重複を減らすため、件数は (code, pattern, chart_state, legacy_state) の初出のみ数える。"""
    rows = _with_ret(rows)
    seen, events = set(), []
    for r in sorted(rows, key=lambda x: x["logged_at"]):
        key = (r["code"], r["chart_pattern"], r["chart_entry_state"], r["legacy_entry_state"])
        if key in seen:
            continue
        seen.add(key)
        events.append(r)

    entry_ev = [r for r in events if _is_entry(r["chart_entry_state"])]
    chase_stop = [r for r in events if _is_entry(r["legacy_entry_state"]) and not _is_entry(r["chart_entry_state"])
                  and (r["chart_pattern"] in CHASE_PATTERNS or r.get("entry_decision") == "NO_ENTRY_CHASE")]
    pb_ev = [r for r in events if r["chart_pattern"] == "PULLBACK_READY"]
    fb_ev = [r for r in events if r["chart_pattern"] == "FAILED_BREAKOUT"]
    pb_picked = [r for r in events if not _is_entry(r["legacy_entry_state"]) and r["chart_pattern"] == "PULLBACK_READY"
                 and _is_entry(r["chart_entry_state"])]

    def with_(rs, h):
        return [r for r in rs if r.get(f"ret_{h}m") is not None]

    def plus_rate(rs, h):
        k = with_(rs, h)
        return {"n": len(k), "rate": _rate(sum(1 for r in k if r[f"ret_{h}m"] > 0), len(k))}

    def dist(rs, h=15):
        k = with_(rs, h)
        c = {"UP": 0, "FLAT": 0, "DECLINE": 0}
        for r in k:
            c[classify_return(r[f"ret_{h}m"])] += 1
        return {"n": len(k), **c}

    cs15 = with_(chase_stop, 15)
    pb15 = with_(pb_ev, 15)
    fb30 = with_(fb_ev, 30)
    diffs = {}
    for r in events:
        if r["legacy_entry_state"] != r["chart_entry_state"]:
            key = f"{r['legacy_entry_state']}→{r['chart_entry_state']}"
            d = diffs.setdefault(key, {"count": 0, "_r15": []})
            d["count"] += 1
            if r.get("ret_15m") is not None:
                d["_r15"].append(r["ret_15m"])
    for d in diffs.values():
        r15 = d.pop("_r15")
        d["avg_ret_15m"] = round(sum(r15) / len(r15), 3) if r15 else None
        d["n_with_outcome"] = len(r15)

    return {
        "signal_rows": len(rows), "events": len(events),
        "definitions": {"UP": f"15分後 >= +{UP_PCT}%", "DECLINE": f"15分後 <= {DECLINE_PCT}%",
                        "chase_stop_success": "legacy=ENTRY で chart が止めた後、15分後が UP でない（横横/下落）",
                        "chase_miss": "同上で15分後が UP", "pullback_success": "PULLBACK_READY の15分後が UP",
                        "failed_breakout_misjudge": f"FAILED_BREAKOUT 判定の30分後 >= +{FAILED_BREAK_MISJUDGE_PCT}%"},
        "entry_ready_count": len(entry_ev), "chase_stop_count": len(chase_stop),
        "failed_breakout_count": len(fb_ev), "pullback_ready_count": len(pb_ev),
        "entry_ready_plus_rate_15m": plus_rate(entry_ev, 15), "entry_ready_plus_rate_30m": plus_rate(entry_ev, 30),
        "entry_ready_outcome_15m": dist(entry_ev, 15),
        "chase_stop_success_rate": _rate(sum(1 for r in cs15 if classify_return(r["ret_15m"]) != "UP"), len(cs15)),
        "chase_miss_rate": _rate(sum(1 for r in cs15 if classify_return(r["ret_15m"]) == "UP"), len(cs15)),
        "chase_stop_outcome_15m": dist(chase_stop, 15), "chase_stop_outcome_30m": dist(chase_stop, 30),
        "pullback_success_rate": _rate(sum(1 for r in pb15 if classify_return(r["ret_15m"]) == "UP"), len(pb15)),
        "pullback_picked_by_chart": {"count": len(pb_picked), "outcome_15m": dist(pb_picked, 15)},
        "failed_breakout_misjudge_rate": _rate(sum(1 for r in fb30 if r["ret_30m"] >= FAILED_BREAK_MISJUDGE_PCT), len(fb30)),
        "failed_breakout_cases": [
            {"code": r["code"], "at": str(r["logged_at"]), "ret_30m": r.get("ret_30m"), "upper_wick": r.get("upper_wick_ratio"),
             "breakout_vol": r.get("breakout_volume_ratio"), "vwap_distance": r.get("vwap_distance"),
             "brokeBarsAgo": (r.get("features") or {}).get("brokeBarsAgo"),
             "new_high_after_sec": r.get("new_high_after_sec"), "max_ret_30m": return_pct(r.get("current_price"), r.get("max_30m")),
             "marketRS": (r.get("context") or {}).get("marketRS"),
             "sector": ((r.get("context") or {}).get("scoreBreakdown") or {}).get("autoSector")}
            for r in fb_ev],
        "legacy_vs_chart_diff": diffs,
        "transition_types": transition_summary(rows),          # 厳密：状態が変わった瞬間に記録した遷移タイプ
        "transitions_loose": transition_chains(rows),          # 参考：履歴列からの緩い推測（厳密な集計には使わない）
        "outcome_quality": outcome_quality(rows),
        "movement": summarize_movement(rows),                  # Phase D（shadow）：existing vs movement-aware
    }


# ---------------------------------------------------------------- Phase D：existing vs movement-aware の比較（shadow）
STOP_DEEP_RATIO = 0.4    # 逆指値に掛からず、最大逆行(MAE)が逆指値幅のこの割合未満 → 「深すぎた」（集計上の定義）


def stop_evaluation(row):
    """推奨逆指値の事後評価（集計専用。ENTRYの判定には戻さない）。
      TOO_SHALLOW  30分内に逆指値へ届いたが、30分後は建値以上に戻った（浅すぎて刈られた）
      APPROPRIATE  届いて30分後も建値未満（適切）／届かず、逆行が逆指値幅の40%以上（適切に機能しうる幅）
      TOO_DEEP     届かず、逆行が逆指値幅の40%未満（必要以上に深かった）
    max/min_30mは約20秒間隔のquote標本なので、瞬間的なヒゲは拾えない。"""
    entry, stop = _num(row.get("current_price")), _num(row.get("recommended_stop"))
    lo, p30 = _num(row.get("min_30m")), _num(row.get("price_30m"))
    if not entry or not stop or stop >= entry or lo is None:
        return None
    if lo <= stop:
        if p30 is None:
            return None
        return "TOO_SHALLOW" if p30 >= entry else "APPROPRIATE"
    mae, dist = entry - lo, entry - stop
    return "TOO_DEEP" if mae < dist * STOP_DEEP_RATIO else "APPROPRIATE"


def _mfe_mae(r):
    e = _num(r.get("current_price"))
    if not e:
        return None, None
    hi, lo = _num(r.get("max_30m")), _num(r.get("min_30m"))
    return (round((hi / e - 1) * 100, 3) if hi is not None else None), (round((lo / e - 1) * 100, 3) if lo is not None else None)


def _group_stats(rows):
    def avg(vals):
        v = [x for x in vals if x is not None]
        return (round(sum(v) / len(v), 3), len(v)) if v else (None, 0)
    out = {"n": len(rows)}
    for h in HORIZONS_MIN:
        a, n = avg([r.get(f"ret_{h}m") for r in rows])
        out[f"avg_ret_{h}m"], out[f"n_{h}m"] = a, n
    out["plus_rate_15m"] = _rate(sum(1 for r in rows if (r.get("ret_15m") or 0) > 0), sum(1 for r in rows if r.get("ret_15m") is not None))
    out["avg_mfe_30m"] = avg([_mfe_mae(r)[0] for r in rows])[0]
    out["avg_mae_30m"] = avg([_mfe_mae(r)[1] for r in rows])[0]
    return out


def summarize_movement(rows):
    """existing recommendation（chart_entry_state）と movement-aware recommendation の並行比較、
    モメンタムENTRYのshadow記録（逆指値・MFE/MAE・逆指値評価）、マイルストーン（いつEXPANDING/PRE_BREAKOUT/…になったか）。"""
    rr = _with_ret(rows)
    seen, events = set(), []
    for r in sorted(rr, key=lambda x: str(x["logged_at"])):     # 同一状態の重複行は初出のみ
        k = (r["code"], r.get("chart_entry_state"), r.get("movement_recommendation"), r.get("activity_state"), bool(r.get("pre_breakout")))
        if k in seen:
            continue
        seen.add(k)
        events.append(r)
    ex_entry = [r for r in events if _is_entry(r.get("chart_entry_state"))]
    groups = {
        "existing_ENTRY_and_movement_ENTRY": [r for r in ex_entry if r.get("movement_recommendation") == "ENTRY_READY"],
        "existing_ENTRY_but_movement_TOO_LATE": [r for r in ex_entry if r.get("movement_recommendation") == "TOO_LATE"],
        "existing_ENTRY_but_movement_LOW_ACTIVITY": [r for r in ex_entry if r.get("movement_recommendation") == "BLOCKED_LOW_ACTIVITY"],
        "movement_ENTRY_but_existing_not_ENTRY": [r for r in events if r.get("movement_recommendation") == "ENTRY_READY"
                                                  and not _is_entry(r.get("chart_entry_state"))],
        "movement_PRE_BREAKOUT": [r for r in events if r.get("movement_recommendation") == "PRE_BREAKOUT"],
        "movement_WATCH_EXPANDING": [r for r in events if r.get("movement_recommendation") == "WATCH_EXPANDING"],
    }
    mom = [r for r in events if r.get("momentum_mode") and r.get("movement_recommendation") == "ENTRY_READY"]
    momentum = []
    for r in mom:
        mfe, mae = _mfe_mae(r)
        momentum.append({"code": r["code"], "at": str(r["logged_at"]), "entry_price": r.get("current_price"),
                         "stop": r.get("recommended_stop"), "stop_distance_pct": r.get("stop_distance_pct"),
                         "rr": r.get("risk_reward"), "ret_5m": r.get("ret_5m"), "ret_15m": r.get("ret_15m"),
                         "ret_30m": r.get("ret_30m"), "mfe_30m": mfe, "mae_30m": mae, "stop_evaluation": stop_evaluation(r),
                         "entry_reason": (r.get("movement") or {}).get("entryReason")})
    for m, r in zip(momentum, mom):
        m["spread_pct"], m["atr5_pct"] = r.get("spread_pct"), r.get("atr5_pct")
    ev = [m["stop_evaluation"] for m in momentum if m["stop_evaluation"]]
    stop_eval = {k: ev.count(k) for k in ("TOO_SHALLOW", "APPROPRIATE", "TOO_DEEP")}
    last_ms = {}
    for r in sorted(rr, key=lambda x: str(x["logged_at"])):
        ms = (r.get("context") or {}).get("milestones")
        if ms:
            last_ms[r["code"]] = ms
    timeline = [dict(code=c, **ms) for c, ms in last_ms.items()]
    timeline.sort(key=lambda d: d.get("first_movement_at") or "9999")
    # 推奨逆指値の品質（momentum以外も含む、movement-aware ENTRY_READYの全イベント）：ノイズで刈られていないかを見る材料
    sq = [r for r in events if r.get("movement_recommendation") == "ENTRY_READY" and r.get("recommended_stop")]
    hit = [r for r in sq if _num(r.get("min_30m")) is not None and _num(r["min_30m"]) <= _num(r["recommended_stop"])]
    known = [r for r in sq if _num(r.get("min_30m")) is not None]
    rec_after = [r for r in hit if _num(r.get("price_30m")) is not None and _num(r["price_30m"]) >= _num(r["current_price"])]
    stop_quality = {"n": len(sq), "n_with_outcome": len(known), "stop_hit": len(hit),
                    "stop_hit_rate": _rate(len(hit), len(known)),
                    "recovered_to_entry_after_stop": len(rec_after), "recovered_rate": _rate(len(rec_after), len(hit)),
                    "avg_mae_30m": _group_stats(sq)["avg_mae_30m"],
                    "avg_stop_distance_pct": (round(sum(_num(r["stop_distance_pct"]) for r in sq if r.get("stop_distance_pct") is not None)
                                                    / max(1, sum(1 for r in sq if r.get("stop_distance_pct") is not None)), 3) if sq else None),
                    "avg_atr5_pct": (round(sum(_num(r["atr5_pct"]) for r in sq if r.get("atr5_pct") is not None)
                                           / max(1, sum(1 for r in sq if r.get("atr5_pct") is not None)), 3) if sq else None),
                    "avg_spread_pct": (round(sum(_num(r["spread_pct"]) for r in sq if r.get("spread_pct") is not None)
                                             / max(1, sum(1 for r in sq if r.get("spread_pct") is not None)), 3) if sq else None)}
    # Radar（初動監視）：状態別の件数・その後の値動き、Radarが通常判定より何分早かったか
    radar_groups = {s: [r for r in events if r.get("radar_state") == s] for s in
                    ("RADAR_SURGE", "RADAR_EXPANDING", "RADAR_PRE_BREAKOUT", "RADAR_ACTIVE")}
    lead = []
    for c, ms in last_ms.items():
        t0 = ms.get("first_radar_at")
        if not t0:
            continue
        def mins(k, t0=t0, ms=ms):
            return round((_parse_dt(ms[k]) - _parse_dt(t0)).total_seconds() / 60, 1) if ms.get(k) and _parse_dt(t0) and _parse_dt(ms[k]) else None
        lead.append({"code": c, "first_radar_at": t0, "minutes_before_expanding": mins("first_expanding_at"),
                     "minutes_before_pre_breakout": mins("first_pre_breakout_at"),
                     "minutes_before_early_breakout": mins("first_early_breakout_at"),
                     "minutes_before_movement_entry": mins("first_movement_entry_at"),
                     "minutes_before_chase": mins("first_chase_at")})
    # Rolling Radar（D.2）：状態別の成績・false positive・先行時間・個別の検出イベント（検出後の+5/+15/+30分・MFE/MAE）
    rolling_groups = {s: [r for r in events if r.get("rolling_state") == s] for s in
                      ("SINGLE_BAR_SURGE", "ROLLING_SURGE", "ROLLING_PRE_BREAKOUT", "ROLLING_EXPANDING", "RADAR_WEAK")}

    def is_fp(r):
        mfe, _ = _mfe_mae(r)
        r30 = r.get("ret_30m")
        return None if (r30 is None or mfe is None) else bool(r30 <= FP_RET_30M and mfe <= FP_MFE_30M)

    rolling_states = {}
    for s, rs_ in rolling_groups.items():
        g = _group_stats(rs_)
        judged = [is_fp(r) for r in rs_ if is_fp(r) is not None]
        g["judged"], g["false_positive"] = len(judged), sum(1 for x in judged if x)
        g["false_positive_rate"] = _rate(g["false_positive"], len(judged))
        rolling_states[s] = g
    rolling_events = []
    for r in events:
        if r.get("rolling_state") in ROLLING_NOTABLE:
            mfe, mae = _mfe_mae(r)
            rolling_events.append({"code": r["code"], "at": str(r["logged_at"]), "state": r["rolling_state"], "score": r.get("rolling_score"),
                                   "price": r.get("current_price"), "ret_5m": r.get("ret_5m"), "ret_15m": r.get("ret_15m"),
                                   "ret_30m": r.get("ret_30m"), "mfe_30m": mfe, "mae_30m": mae, "false_positive": is_fp(r),
                                   "confirmations_failed": ((r.get("movement") or {}).get("rolling") or {}).get("confirmations", {}).get("failed")})
    rolling_lead = []
    for c, ms in last_ms.items():
        t0 = ms.get("first_rolling_at")
        if not t0:
            continue
        def mins2(k, t0=t0, ms=ms):
            return round((_parse_dt(ms[k]) - _parse_dt(t0)).total_seconds() / 60, 1) if ms.get(k) and _parse_dt(t0) and _parse_dt(ms[k]) else None
        rolling_lead.append({"code": c, "first_rolling_at": t0, "minutes_before_expanding": mins2("first_expanding_at"),
                             "minutes_before_pre_breakout": mins2("first_pre_breakout_at"),
                             "minutes_before_early_breakout": mins2("first_early_breakout_at"),
                             "minutes_before_movement_entry": mins2("first_movement_entry_at"), "minutes_before_chase": mins2("first_chase_at")})
    return {"rolling": {"states": rolling_states, "events": rolling_events[:60], "lead_times": rolling_lead,
                        "false_positive_definition": f"30分後が+{FP_RET_30M}%以下 かつ 30分内の最大上昇が+{FP_MFE_30M}%以下"},
            "radar": {"states": {s: _group_stats(v) for s, v in radar_groups.items()}, "lead_times": lead},
            "stop_quality": stop_quality,
            "comparison": {k: _group_stats(v) for k, v in groups.items()},
            "activity_states": {s: sum(1 for r in events if r.get("activity_state") == s)
                                for s in ("EXPANDING", "ACTIVE", "COILING", "LOW_ACTIVITY", "FADING", "UNKNOWN")},
            "pre_breakout_count": sum(1 for r in events if r.get("pre_breakout")),
            "too_late_count": sum(1 for r in events if r.get("too_late")),
            "momentum_entries": momentum, "stop_evaluation": stop_eval, "milestone_timeline": timeline}
