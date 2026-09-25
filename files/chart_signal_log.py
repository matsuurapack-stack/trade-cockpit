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
HEARTBEAT_SEC = 300                # TOP5・ENTRY状態の銘柄は、状態が変わらなくても5分ごとに1行残す（追跡用）
HEARTBEAT_IDLE_SEC = 900           # それ以外のWATCH/WAIT系は15分ごと（Neon容量対策。状態変化は常に即記録）
# 記録対象：TOP5系リスト、または旧/新どちらかが「注目状態」。WEAK/INVALID等の圏外は記録しない。
LOGGED_STATES = ("NOW_BUYABLE", "ENTRY_READY", "WATCH", "WAIT_PULLBACK", "WAIT_BREAKOUT", "CHASE_RISK")

# 結果分類（集計専用）：15分後リターンで 下落 / 横横 / 上昇 に分ける
DECLINE_PCT = -0.3
UP_PCT = 0.7                       # backtest_chart_context の QUICK_WIN と同じ
FAILED_BREAK_MISJUDGE_PCT = 4.0    # 「FAILED_BREAKOUT判定→30分後+4%以上」を誤判定とみなす（指示）

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


def is_loggable(rec):
    if (rec.get("context") or {}).get("top5"):
        return True
    return rec.get("legacy_entry_state") in LOGGED_STATES or rec.get("chart_entry_state") in LOGGED_STATES


def should_log(last, rec, now, heartbeat_sec=None):
    """last: 直前に記録した {"pattern","chart","legacy","at"}。状態が変わった／ハートビート時のみTrue。"""
    if not last:
        return True
    if heartbeat_sec is None:
        hot = (rec.get("context") or {}).get("top5") or rec.get("chart_entry_state") in ENTRY_STATES             or rec.get("legacy_entry_state") in ENTRY_STATES
        heartbeat_sec = HEARTBEAT_SEC if hot else HEARTBEAT_IDLE_SEC
    if (last.get("pattern"), last.get("chart"), last.get("legacy")) != \
            (rec["chart_pattern"], rec["chart_entry_state"], rec["legacy_entry_state"]):
        return True
    return (now - last["at"]).total_seconds() >= heartbeat_sec


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
        "transitions": transition_chains(rows),
    }
