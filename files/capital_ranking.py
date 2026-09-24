# 買付余力ベースの候補評価＋IPO再評価＋shadow_watchの純粋ロジック（DB・ネットワーク非依存）。
#
# 既存のTOP5選定（server._select_entry_ready_top5）・watchlist・trade learningを二重実装しない
# ため、ここには「余力から見た買える/買えない判定」「資金効率」「集中リスク」「IPO段階遷移」
# 「IPOスコア」「shadow_watch期間・トリガー判定」「analysis/actionableの二系統ランキング」だけを
# 置く。server.py側が既存関数から呼ぶ。
#
# 2系統ランキング（2026-09-24）：
#   analysis_top5   … 余力を考慮しない純粋な分析スコアTOP5（過去検証・精度評価・成績比較用）
#   actionable_top5 … 現在の買付余力で実際に買える最良候補（朝一・場中・今買い時TOP5の表示用）

import datetime
import re
import statistics

LOT_SIZE = 100  # SBI証券・日本株の原則単元
CONCENTRATION_CAUTION = 0.70
CONCENTRATION_STRONG = 0.85
MAX_CAPITAL_ADJUSTMENT = 5.0  # 資金効率補正の最大幅（±）。trade_qualityを逆転させ過ぎない

# ---------------------------------------------------------------- 余力・買える判定


def compute_buyability(price, cash_available, lot=LOT_SIZE):
    """minimum_purchase_amount = price × lot。cash_available未設定(None)なら判定不能として
    buyable=None（既存ランキングを変えない）。"""
    if price is None or price <= 0:
        return {"minimumPurchaseAmount": None, "cashAvailable": cash_available,
                "cashRemainingAfterBuy": None, "buyable": None, "cashUsageRatio": None,
                "concentrationRisk": None}
    minimum = round(price * lot)
    if cash_available is None:
        return {"minimumPurchaseAmount": minimum, "cashAvailable": None, "cashRemainingAfterBuy": None,
                "buyable": None, "cashUsageRatio": None, "concentrationRisk": None}
    buyable = minimum <= cash_available
    usage = (minimum / cash_available) if cash_available > 0 else None
    return {"minimumPurchaseAmount": minimum, "cashAvailable": cash_available,
            "cashRemainingAfterBuy": round(cash_available - minimum) if buyable else None,
            "buyable": buyable,
            "cashUsageRatio": round(usage, 4) if usage is not None else None,
            "concentrationRisk": concentration_risk_level(usage) if buyable else None}


def concentration_risk_level(usage_ratio):
    """70%以上=CAUTION、85%以上=STRONG。CAPITAL_CONCENTRATION_RISKの重さ。"""
    if usage_ratio is None:
        return None
    if usage_ratio >= CONCENTRATION_STRONG:
        return "STRONG"
    if usage_ratio >= CONCENTRATION_CAUTION:
        return "CAUTION"
    return None


_AVOID_STATES = ("CHASE_RISK", "INVALID", "WEAK")
_BUY_NOW_STATES = ("NOW_BUYABLE", "ENTRY_READY")
_WAIT_STATES = ("WAIT_BREAKOUT", "WAIT_PULLBACK", "WAIT_DATA_STALE")


def classify_capital_status(entry_state, buyable):
    """BUY_NOW / WATCH / WAIT / NOT_BUYABLE / AVOID。AVOID（危険状態）は余力に関わらずAVOIDを
    優先、次に余力不足。buyable=None（余力未設定）は余力判定をせずentry_stateだけで分類。"""
    if entry_state in _AVOID_STATES:
        return "AVOID"
    if buyable is False:
        return "NOT_BUYABLE"
    if entry_state in _BUY_NOW_STATES:
        return "BUY_NOW"
    if entry_state in _WAIT_STATES:
        return "WAIT"
    return "WATCH"


# ---------------------------------------------------------------- 資金効率（補正幅つき）

def capital_adjustment(candidate, buyability, median_flex=None):
    """final_action_score = trade_quality(entryScore) + capital_efficiency_adjustment。
    補正は最大±MAX_CAPITAL_ADJUSTMENT点。
      ・購入後の自由度（1-余力使用率）を、同じ候補群の中央値と比べた差で±4点程度
        （trade_qualityが十分離れていれば逆転しない＝「余力を残すために弱い銘柄を選ぶ」はNG）
      ・急騰（+15%以上）・高イベントリスク・流動性不足は減点、モメンタム再加速/減衰は±1
    低位株が価格の逆数で有利になる設計ではない（価格自体は使わず、使用率の群内相対差のみ）。"""
    usage = (buyability or {}).get("cashUsageRatio")
    flex = 1.0 - min(max(usage if usage is not None else 0.0, 0.0), 1.0)
    ref = 0.5 if median_flex is None else median_flex
    adj = (flex - ref) * 8.0
    change = candidate.get("changePct")
    if change is not None and change >= 15:
        adj -= 2.0  # 急騰リスク
    if candidate.get("eventRiskLevel") == "HIGH":
        adj -= 1.5
    liquidity = candidate.get("liquidityFactor")
    if liquidity is not None:
        adj += max(-2.0, min(1.0, (float(liquidity) - 1.0) * 10.0))
    adj += {"MOMENTUM_REACCELERATING": 1.0, "MOMENTUM_DECAY": -1.0}.get(candidate.get("momentumState"), 0.0)
    return round(max(-MAX_CAPITAL_ADJUSTMENT, min(MAX_CAPITAL_ADJUSTMENT, adj)), 2)


def capital_efficiency_score(candidate, buyability, median_flex=None):
    """final_action_score（entryScore＋補正）。"""
    return round(float(candidate.get("entryScore") or 0) + capital_adjustment(candidate, buyability, median_flex), 2)


def annotate_capital(candidate, cash_available, median_flex=None):
    """candidate（dict）へ余力情報を非破壊で付与したコピーを返す。"""
    b = compute_buyability(candidate.get("current"), cash_available)
    out = {**candidate,
           "minimumPurchaseAmount": b["minimumPurchaseAmount"], "cashAvailable": b["cashAvailable"],
           "cashRemainingAfterBuy": b["cashRemainingAfterBuy"], "buyable": b["buyable"],
           "cashUsageRatio": b["cashUsageRatio"],
           "capitalConcentrationRisk": b["concentrationRisk"],
           "capitalStatus": classify_capital_status(candidate.get("entryState"), b["buyable"])}
    if b["concentrationRisk"]:
        out["capitalFlags"] = ["CAPITAL_CONCENTRATION_RISK"]
    if cash_available is not None:
        adj = capital_adjustment(candidate, b, median_flex)
        out["tradeQuality"] = candidate.get("entryScore")
        out["capitalEfficiencyAdjustment"] = adj
        out["capitalEfficiencyScore"] = round(float(candidate.get("entryScore") or 0) + adj, 2)
    else:
        out["capitalEfficiencyScore"] = None
    return out


def suggest_combinations(candidates, cash_available, max_size=3, pool=8, top_n=3):
    """余力内で同時に持てる組み合わせ（Phase17）。買える候補の上位pool件から2〜max_size銘柄の
    組を列挙し、資金効率合計－集中リスクペナルティで並べる。"""
    if not cash_available or cash_available <= 0:
        return []
    buyable = [c for c in candidates if c.get("buyable") and c.get("minimumPurchaseAmount")]
    buyable = sorted(buyable, key=lambda c: -(c.get("capitalEfficiencyScore") or 0))[:pool]
    from itertools import combinations
    out = []
    for size in range(2, max_size + 1):
        for combo in combinations(buyable, size):
            total = sum(c["minimumPurchaseAmount"] for c in combo)
            if total > cash_available:
                continue
            eff = sum(c.get("capitalEfficiencyScore") or 0 for c in combo)
            top_share = max(c["minimumPurchaseAmount"] for c in combo) / cash_available
            penalty = 0.0
            if top_share >= CONCENTRATION_STRONG:
                penalty = 0.15
            elif top_share >= CONCENTRATION_CAUTION:
                penalty = 0.07
            out.append({"codes": [c["code"] for c in combo], "count": size, "totalAmount": total,
                        "cashRemaining": round(cash_available - total),
                        "usageRatio": round(total / cash_available, 4),
                        "score": round(eff * (1 - penalty), 2),
                        "concentrationRisk": concentration_risk_level(top_share)})
    out.sort(key=lambda x: -x["score"])
    return out[:top_n]


def apply_capital_selection(candidates, cash_available, select_fn):
    """cash未設定ならselect_fn(candidates)をそのまま返す（既存挙動不変）。設定済みなら：
      ①各候補へ余力情報を付与（補正の基準＝買える候補の余力自由度の中央値）
      ②買える候補（buyable != False）だけでselect_fn（既存TOP5選定）を実行
      ③買えない候補のうち注目に値するもの（Tier1〜3相当の状態）を別枠notBuyableNotableへ。
    select_fnは既存の_select_entry_ready_top5（5要素タプル）を想定。戻り値は
    (select_fnの結果, annotated全候補, notBuyableNotable, combinations)。"""
    if cash_available is None:
        return select_fn(candidates), list(candidates), [], []
    flexes = []
    for c in candidates:
        b = compute_buyability(c.get("current"), cash_available)
        if b["buyable"]:
            flexes.append(1.0 - min(max(b["cashUsageRatio"] or 0.0, 0.0), 1.0))
    median_flex = statistics.median(flexes) if flexes else None
    annotated = [annotate_capital(c, cash_available, median_flex) for c in candidates]
    buyable_pool = [c for c in annotated if c["buyable"] is not False]
    result = select_fn(buyable_pool)
    notable_states = _BUY_NOW_STATES + ("WAIT_BREAKOUT", "WAIT_PULLBACK", "WATCH")
    notable = [c for c in annotated if c["buyable"] is False and c.get("entryState") in notable_states
               and (c.get("entryScore") or 0) >= 45]
    notable.sort(key=lambda c: -(c.get("entryScore") or 0))
    combos = suggest_combinations(annotated, cash_available)
    return result, annotated, notable[:5], combos


def rerank_by_capital_efficiency(top5):
    """同じTier内だけ資金効率(final_action_score)で並べ替える（Tier間の順序＝今すぐ入れる優先は
    変えない）。補正は±5点以内なので、trade_qualityが大きく離れた銘柄同士は逆転しない。"""
    if not top5 or top5[0].get("capitalEfficiencyScore") is None:
        return top5
    return [{**c, "rank": i + 1} for i, c in enumerate(
        sorted(top5, key=lambda c: (c.get("candidateTier") or 9, -(c.get("capitalEfficiencyScore") or 0))))]


# ---------------------------------------------------------------- ポジション数（新規ENTRY余地）

def compute_new_entry_capacity(open_position_count):
    """0-1銘柄=NORMAL、2銘柄=CAUTION、3銘柄以上=ENTRY_LIMIT。補助判断であり候補の除外や
    既存ポジションの売却は行わない。"""
    n = open_position_count or 0
    if n >= 3:
        return "ENTRY_LIMIT"
    if n == 2:
        return "CAUTION"
    return "NORMAL"


def summarize_positions(portfolio_rows):
    """portfolio行（active・quantity>0）から open_position_count / portfolio_exposure（取得原価合計）
    を作る。複雑な資産評価はしない。"""
    open_rows = [r for r in (portfolio_rows or [])
                 if r.get("active") is not False and (r.get("quantity") or 0) > 0]
    exposure = sum((r.get("quantity") or 0) * (r.get("average_price") or 0) for r in open_rows)
    count = len(open_rows)
    capacity = compute_new_entry_capacity(count)
    warning = {"CAUTION": f"保有{count}銘柄：新規ENTRYは慎重に",
               "ENTRY_LIMIT": f"保有{count}銘柄：新規ENTRY上限（強く推奨しない）"}.get(capacity)
    return {"openPositionCount": count, "portfolioExposure": round(exposure),
            "newEntryCapacity": capacity, "entryCapacityWarning": warning,
            "positionCodes": [r.get("code") for r in open_rows]}


# ---------------------------------------------------------------- analysis / actionable 二系統

def build_dual_ranking(candidates, cash_available, select_fn, position_summary=None):
    """analysis_top5（余力を考慮しない純粋な分析順位）とactionable_top5（現在余力で実際に買える
    候補）を同じ既存選定関数(select_fn)から作る。どちらも同じ判定基準・同じ関数で、違いは
    「余力による買える/買えないの絞り込み」だけ。

    各候補には analysisRank（分析上の順位、TOP5外はNone）・actionableRank（実戦順位）・
    actionableReason（分析上位なのに実戦TOP5に無い理由）を付ける＝ブラックボックス化しない。
    戻り値: dict(analysis=(top5,watch,rev,revw,debug), actionable=同, annotated, notable, combos,
    capacity)。"""
    analysis_input = [dict(c) for c in candidates]
    analysis = select_fn(analysis_input)  # 余力を一切見ない
    actionable, annotated, notable, combos = apply_capital_selection(
        [dict(c) for c in candidates], cash_available, select_fn)
    a_top5 = analysis[0]
    act_top5 = actionable[0]
    if cash_available is not None:
        # analysis側にも必要資金・買える/買えないを見せる（余力はランキングには影響させない）
        by_code = {c["code"]: c for c in annotated}
        a_top5[:] = [{**by_code[c["code"]], **c} if c["code"] in by_code else c for c in a_top5]
    a_rank = {c["code"]: i + 1 for i, c in enumerate(a_top5)}
    act_rank = {c["code"]: i + 1 for i, c in enumerate(act_top5)}
    capacity = position_summary or {}
    limit_state = capacity.get("newEntryCapacity")
    for i, c in enumerate(a_top5):
        c["analysisRank"] = i + 1
        c["actionableRank"] = act_rank.get(c["code"])
        if c["actionableRank"] is None:
            if c.get("buyable") is False:
                c["actionableReason"] = (f"余力不足（必要資金{c.get('minimumPurchaseAmount'):,}円 > 余力"
                                         f"{int(c.get('cashAvailable') or 0):,}円）")
            else:
                c["actionableReason"] = "実戦TOP5の枠外（他の買える候補が上位）"
        c["rank"] = i + 1
    for i, c in enumerate(act_top5):
        c["analysisRank"] = a_rank.get(c["code"])
        c["actionableRank"] = i + 1
        c["rank"] = i + 1
        if limit_state in ("CAUTION", "ENTRY_LIMIT"):
            c["entryCapacity"] = limit_state
            c["entryCapacityWarning"] = capacity.get("entryCapacityWarning")
    return {"analysis": analysis, "actionable": actionable, "annotated": annotated,
            "notable": notable, "combos": combos, "capacity": capacity}


# ---------------------------------------------------------------- IPO / 復活

IPO_WATCH_BUSINESS_DAYS = 20
SHADOW_WATCH_DAYS = {"IPO": 20, "MATERIAL": 3, "SURGE": 2}
IPO_STAGES = ("WATCH_LOW", "WATCH", "BUY_CANDIDATE")

_IPO_STRONG_SIGNALS = ("volume_surge", "prev_high_break", "day_high_update", "five_min_high_update")
_IPO_SUPPORT_SIGNALS = ("vwap_recovered", "higher_low", "outperform_index", "outperform_peers",
                        "bid_increase", "ask_absorb")


def evaluate_ipo_stage(current_stage, signals, entry_score=None):
    """朝に弱くても再評価で昇格できる。降格しても下限はWATCH_LOW（DELETEにはしない）。
      BUY_CANDIDATE: 出来高急増 かつ 高値更新系（当日高値/前日高値ブレイク/5分足高値）、
                     もしくはentry_score>=85
      WATCH        : 何らかの回復/強さシグナル（VWAP回復・安値切上げ・逆行高・板・高値更新等）
                     もしくはentry_score>=60
      WATCH_LOW    : それ以外（朝の下落のみでもここに残す）
    IPO以外の保護対象（材料株・決算直後等）にも同じ段階遷移を使う。戻り値: (stage, reasons)"""
    signals = signals or {}
    on = [k for k in _IPO_STRONG_SIGNALS + _IPO_SUPPORT_SIGNALS if signals.get(k)]
    high_update = any(signals.get(k) for k in ("day_high_update", "prev_high_break", "five_min_high_update"))
    if (signals.get("volume_surge") and high_update) or (entry_score is not None and entry_score >= 85):
        return "BUY_CANDIDATE", on
    if on or (entry_score is not None and entry_score >= 60):
        return "WATCH", on
    return "WATCH_LOW", on


def add_business_days(start_date, n, is_trading_day=None):
    is_td = is_trading_day or (lambda d: d.weekday() < 5)
    d = start_date
    count = 0
    while count < n:
        d += datetime.timedelta(days=1)
        if is_td(d):
            count += 1
    return d


def business_days_between(start_date, end_date, is_trading_day=None):
    is_td = is_trading_day or (lambda d: d.weekday() < 5)
    if end_date <= start_date:
        return 0
    d, count = start_date, 0
    while d < end_date:
        d += datetime.timedelta(days=1)
        if is_td(d):
            count += 1
    return count


def is_within_ipo_watch_window(listing_date, today, is_trading_day=None):
    if listing_date is None:
        return False
    return business_days_between(listing_date, today, is_trading_day) <= IPO_WATCH_BUSINESS_DAYS


def evaluate_watch_removal(conditions, ipo_in_window=False):
    """完全削除は複数条件が揃った時のみ（Phase12）。conditionsのキー:
    volume_dried_up / catalyst_gone / trend_broken / period_elapsed / liquidity_low。
    朝の下落だけ(morning_drop_only)では絶対に削除しない。"""
    if conditions.get("morning_drop_only"):
        return False
    keys = ("volume_dried_up", "catalyst_gone", "trend_broken", "period_elapsed", "liquidity_low")
    hit = sum(1 for k in keys if conditions.get(k))
    return hit >= 3 and (not ipo_in_window or conditions.get("period_elapsed") or hit >= 4)


def shadow_watch_until(kind, start_date, is_trading_day=None):
    return add_business_days(start_date, SHADOW_WATCH_DAYS.get(kind, 3), is_trading_day)


def detect_shadow_trigger(signals):
    """shadow_watch中の再注目条件：出来高急増／高値更新／材料発生のいずれか。"""
    signals = signals or {}
    reasons = []
    if signals.get("volume_surge"):
        reasons.append("出来高急増")
    if signals.get("day_high_update") or signals.get("prev_high_break") or signals.get("five_min_high_update"):
        reasons.append("高値更新")
    if signals.get("new_catalyst"):
        reasons.append("材料発生")
    return reasons


def derive_signals(row, stage2, snapshot, entry_breakdown=None):
    """既存のscan結果（row/stage2/snapshot）からIPO・shadow用シグナルを導出する。欠損はFalse。"""
    row, stage2, snapshot = row or {}, stage2 or {}, snapshot or {}
    tavr = stage2.get("timeAdjustedVolumeRatio")
    current, high = row.get("current"), row.get("high")
    breakdown = entry_breakdown or {}
    return {
        "volume_surge": bool(tavr is not None and tavr >= 2.0),
        "vwap_recovered": bool(snapshot.get("aboveVwap")),
        "day_high_update": bool(current and high and current >= high * 0.999),
        "prev_high_break": bool(stage2.get("aboveRecentHigh")),
        "five_min_high_update": bool((breakdown.get("fiveMinStructure") or 0) >= 15
                                     or snapshot.get("fiveMinStructure") == "higher_highs"),
        "outperform_index": bool((row.get("marketRS") or 0) > 1.0),
    }


# ---------------------------------------------------------------- 朝の弱さだけで除外しない

def detect_protective_context(row, daily_arrays, stage2, positive_catalysts, is_ipo=False,
                              recent_limit_up=False, recent_earnings=False):
    """朝9:00〜9:30の値動きだけで完全除外しない対象かを判定する。対象:
    直近IPO / 決算直後 / 材料発生 / 前日出来高急増 / ストップ高経験 / 前日大幅高 / 寄り前GU・GD大。
    戻り値: 理由のリスト（空なら保護対象外）。daily_arrays=(closes,opens,highs,lows,volumes)、
    末尾が当日（liveバー）。"""
    reasons = []
    if is_ipo:
        reasons.append("直近IPO")
    if recent_earnings:
        reasons.append("決算直後")
    if positive_catalysts:
        reasons.append("材料発生")
    if recent_limit_up:
        reasons.append("ストップ高経験")
    if daily_arrays:
        closes, opens, highs, lows, volumes = daily_arrays
        if len(closes) >= 23 and len(volumes) >= 23:
            base = sum(volumes[-22:-2]) / 20.0
            if base and volumes[-2] >= base * 2.0:
                reasons.append("前日出来高急増")
            if closes[-3] and closes[-2] >= closes[-3] * 1.07:
                reasons.append("前日大幅高")
        if len(closes) >= 2 and len(opens) >= 1 and closes[-2]:
            gap = (opens[-1] - closes[-2]) / closes[-2] * 100.0
            if abs(gap) >= 3.0:
                reasons.append("寄り前GU大" if gap > 0 else "寄り前GD大")
    return reasons


# ---------------------------------------------------------------- IPO Fundamental / 総合

def _lin(v, lo, hi):
    """lo→0点、hi→100点の線形（範囲外は丸め）。"""
    if v is None:
        return None
    if hi == lo:
        return None
    return max(0.0, min(100.0, (v - lo) / (hi - lo) * 100.0))


IPO_FUNDAMENTAL_WEIGHTS = {"revenue_growth": 0.15, "profit_growth": 0.15, "margin_trend": 0.08,
                           "guidance_strength": 0.10, "kpi_growth": 0.10, "business_quality": 0.10,
                           "market_size": 0.05, "valuation": 0.10, "lockup_risk": 0.07,
                           "vc_overhang": 0.05, "float_size": 0.05}


def fundamental_data_quality(coverage):
    """スコア算出に使えた指標の重み合計(coverage)→データ品質。スコアが高くてもINSUFFICIENT/LOWの
    場合は画面で「参考値」と明示する。"""
    if coverage is None or coverage < 0.25:
        return "INSUFFICIENT"
    if coverage < 0.5:
        return "LOW"
    if coverage < 0.75:
        return "MEDIUM"
    return "HIGH"


DERIVED_FIELD_TO_COMPONENT = {"float_ratio_pct": "float_size"}


def compute_ipo_fundamental_score(data, derived_fields=()):
    """data（全て任意、未知はNone→除外して重みを再正規化しcoverageで信頼度を示す）:
    revenue_growth_pct / profit_growth_pct / margin_change_pt / guidance（'UPWARD'/'IN_LINE'/
    'DOWNWARD'/None）/ kpi_growth_pct / business_quality（0-100）/ market_size_score（0-100）/
    per（バリュエーション。低いほど高得点）/ lockup_risk（0=なし〜100=大）/
    vc_holding_pct / float_ratio_pct。
    derived_fields: 開示値ではなく他の値から推定した項目（例: float_ratio_pct＝100−大株主等保有比率）。
    スコアには使うが、データ品質(coverage)の根拠には数えない（推定値でHIGH/MEDIUMに見えないように）。"""
    d = data or {}
    comp = {
        "revenue_growth": _lin(d.get("revenue_growth_pct"), 0, 40),
        "profit_growth": _lin(d.get("profit_growth_pct"), 0, 60),
        "margin_trend": _lin(d.get("margin_change_pt"), -2, 5),
        "guidance_strength": {"UPWARD": 100.0, "IN_LINE": 60.0, "DOWNWARD": 15.0}.get(d.get("guidance")),
        "kpi_growth": _lin(d.get("kpi_growth_pct"), 0, 40),
        "business_quality": d.get("business_quality"),
        "market_size": d.get("market_size_score"),
        "valuation": None if d.get("per") is None else (100.0 - _lin(d["per"], 10, 60)),
        "lockup_risk": None if d.get("lockup_risk") is None else 100.0 - max(0.0, min(100.0, d["lockup_risk"])),
        "vc_overhang": None if d.get("vc_holding_pct") is None else 100.0 - _lin(d["vc_holding_pct"], 0, 50),
        "float_size": _lin(d.get("float_ratio_pct"), 10, 40),
    }
    known = {k: v for k, v in comp.items() if v is not None}
    wsum = sum(IPO_FUNDAMENTAL_WEIGHTS[k] for k in known)
    total = (sum(IPO_FUNDAMENTAL_WEIGHTS[k] * v for k, v in known.items()) / wsum) if wsum else None
    derived_components = sorted({DERIVED_FIELD_TO_COMPONENT[f] for f in (derived_fields or ())
                                 if f in DERIVED_FIELD_TO_COMPONENT and DERIVED_FIELD_TO_COMPONENT[f] in known})
    quality_wsum = sum(IPO_FUNDAMENTAL_WEIGHTS[k] for k in known if k not in derived_components)
    supply_parts = [comp[k] for k in ("lockup_risk", "vc_overhang", "float_size") if comp[k] is not None]
    supply = (sum(supply_parts) / len(supply_parts)) if supply_parts else None
    r = lambda x: None if x is None else round(x)
    coverage = round(quality_wsum, 2)
    return {"ipo_fundamental_score": r(total), "revenue_growth_score": r(comp["revenue_growth"]),
            "profit_growth_score": r(comp["profit_growth"]), "kpi_score": r(comp["kpi_growth"]),
            "valuation_score": r(comp["valuation"]), "supply_demand_score": r(supply),
            "coverage": coverage, "fundamental_data_quality": fundamental_data_quality(coverage),
            "known_components": sorted(known.keys()), "derived_components": derived_components,
            "missing_components": sorted(k for k in comp if k not in known)}


IPO_TOTAL_DEFAULT_WEIGHTS = {"fundamental": 0.30, "momentum": 0.25, "supply_demand": 0.20,
                             "volume": 0.15, "material": 0.10}


def compute_ipo_total_score(scores, weights=None):
    """scores: {fundamental, momentum, supply_demand, volume, material}（0-100、未知None）。
    weightsで市場状況に応じて重み変更可。未知成分は除外して再正規化。"""
    w = weights or IPO_TOTAL_DEFAULT_WEIGHTS
    known = {k: scores.get(k) for k in w if scores.get(k) is not None}
    wsum = sum(w[k] for k in known)
    if not wsum:
        return None
    return round(sum(w[k] * v for k, v in known.items()) / wsum)


# ---------------------------------------------------------------- 業績データの自動結合

def _num(s):
    if s is None:
        return None
    s = str(s).replace(",", "").replace("△", "-").replace("▲", "-").strip()
    try:
        return float(s)
    except ValueError:
        return None


def parse_ipo_forecast_table(text):
    """上場時に開示される「当社決算情報等のお知らせ」（TDnet）の業績予想表から、
    売上高・営業利益・純利益の予想／前期実績を取り出す。表の行の形（単位：百万円）:
      売上高 4,300 100 12.3 1,915 100 3,828 100
      営業利益 340 7.9 69.1 119 6.2 201 5.2
    成長率は表示された対前期増減率ではなく、予想値÷前期実績から自分で計算する。
    抽出できない場合はNone（推測しない）。"""
    if not text:
        return None
    t = text.replace("　", " ")
    num = r"(-?[\d,]+(?:\.\d+)?)"
    m_s = re.search(r"売上高\s+" + num + r"\s+100\s+" + num + r"\s+" + num + r"\s+100\s+" + num + r"\s+100", t)
    m_o = re.search(r"営業利益\s+" + num + r"\s+" + num + r"\s+" + num + r"\s+" + num + r"\s+" + num
                    + r"\s+" + num + r"\s+" + num, t)
    m_n = re.search(r"当期（中間）純利益\s+" + num + r"\s+" + num + r"\s+" + num + r"\s+" + num + r"\s+" + num
                    + r"\s+" + num + r"\s+" + num, t)
    if not m_s:
        return None
    out = {"sales_forecast_mn": _num(m_s.group(1)), "sales_prev_mn": _num(m_s.group(4))}
    if out["sales_prev_mn"]:
        out["revenue_growth_pct"] = round((out["sales_forecast_mn"] / out["sales_prev_mn"] - 1) * 100, 1)
    if m_o:
        out.update({"op_forecast_mn": _num(m_o.group(1)), "op_prev_mn": _num(m_o.group(6)),
                    "op_margin_pct": _num(m_o.group(2)), "op_margin_prev_pct": _num(m_o.group(7))})
        if out["op_prev_mn"]:
            out["profit_growth_pct"] = round((out["op_forecast_mn"] / out["op_prev_mn"] - 1) * 100, 1)
        if out["op_margin_pct"] is not None and out["op_margin_prev_pct"] is not None:
            out["margin_change_pt"] = round(out["op_margin_pct"] - out["op_margin_prev_pct"], 1)
    if m_n:
        out["net_forecast_mn"] = _num(m_n.group(1))
    return out


FUNDAMENTAL_FIELDS = ("revenue_growth_pct", "profit_growth_pct", "margin_change_pt", "guidance",
                      "kpi_growth_pct", "business_quality", "market_size_score", "per", "lockup_risk",
                      "vc_holding_pct", "float_ratio_pct")


def merge_fundamentals(existing=None, smart_import=None, explicit=None):
    """業績データの結合。優先順位：①既存の企業業績データ（TDnet決算短信/jQuants/上場時開示）
    ②Smart Importで保存された決算・企業情報 ③ipo_stocksの明示入力。上位が持つ項目は上位を採用し、
    無い項目だけ下位で補う。戻り値: (merged, sources)  sources={field: 'existing'|'smart_import'|'explicit'}。"""
    merged, sources = {}, {}
    for label, src in (("existing", existing), ("smart_import", smart_import), ("explicit", explicit)):
        for k, v in (src or {}).items():
            if v is None or k in merged:
                continue
            merged[k] = v
            sources[k] = label
    return merged, sources


def derive_guidance_from_titles(titles):
    """適時開示の見出しから会社予想の修正方向を判定（上方修正/下方修正の明示がある場合のみ）。"""
    up = any("上方修正" in t for t in titles or [])
    down = any("下方修正" in t for t in titles or [])
    if up and not down:
        return "UPWARD"
    if down and not up:
        return "DOWNWARD"
    return None


IPO_LEARNING_RULE = {
    "rule_name": "直近IPOの早期監視解除禁止",
    "category": "ENTRY",
    "priority": "HIGH",
    "rule_text": ("直近IPOは朝の下落だけで監視対象から外さない。最低でもWATCH_LOWとして残す。"
                  "IPO後20営業日以内は、出来高・VWAP・高値更新・材料・需給を継続評価する。"
                  "ユーザーが手動で監視解除してもshadow_watchを続ける。"
                  "再上昇条件を満たした場合はWATCHまたはBUY_CANDIDATEへ復帰させる。"),
}

# 上記IPOルールの拡張版（IPOだけでなく、決算直後・材料・出来高急増・ストップ高経験等にも適用）。
# 既存ルールと重複するため新規作成せず、既存のIPOルールを拡張する（investment_db側で判定）。
MORNING_WEAKNESS_RULE = {
    "rule_name": "朝の弱さだけで候補除外しない",
    "category": "ENTRY",
    "priority": "HIGH",
    "rule_text": ("直近IPOは朝の下落だけで監視対象から外さない。最低でもWATCH_LOWとして残す。"
                  "IPO後20営業日以内は、出来高・VWAP・高値更新・材料・需給を継続評価する。"
                  "ユーザーが手動で監視解除してもshadow_watchを続ける。"
                  "再上昇条件を満たした場合はWATCHまたはBUY_CANDIDATEへ復帰させる。"
                  "IPOに限らず、決算直後・材料発生・前日出来高急増・ストップ高経験・前日大幅高・"
                  "寄り前GU/GD大の銘柄も、朝9:00〜9:30の値動きだけで完全除外せず、最低WATCH_LOWで維持する。"
                  "特に寄りGD→売り一巡→VWAP回復はデイトレ候補として再評価する。"),
}


def build_ipo_info(ipo_row, candidate, signals, today, is_trading_day=None, weights=None,
                   fundamentals=None, derived_fields=()):
    """IPO銘柄1件分の再評価結果（段階・シグナル・4種のIPOスコア・データ品質）を作る純粋関数。
    ipo_row: ipo_stocksの行。fundamentals: 結合済みの業績データ(merge_fundamentals後)。省略時は
    ipo_row['fundamentals_json']のみを使う。candidate: _score_entry_candidatesの候補dict。"""
    listing = ipo_row.get("listing_date")
    if isinstance(listing, str):
        try:
            listing = datetime.date.fromisoformat(listing[:10])
        except ValueError:
            listing = None
    elif isinstance(listing, datetime.datetime):
        listing = listing.date()
    in_window = True if listing is None else is_within_ipo_watch_window(listing, today, is_trading_day)
    stage, on = evaluate_ipo_stage(ipo_row.get("watch_stage"), signals, candidate.get("entryScore"))
    data = fundamentals if fundamentals is not None else (ipo_row.get("fundamentals_json") or {})
    fund = compute_ipo_fundamental_score(data, derived_fields)
    tavr = (candidate.get("scoreBreakdown") or {}).get("volumeRatio") or candidate.get("volumeRatio")
    volume_score = None if tavr is None else round(_lin(tavr, 0.5, 3.0))
    material_score = 70 if any("好材料" in r for r in (candidate.get("reasons") or [])) else 40
    entry = candidate.get("entryScore")
    sig_score = min(100, 25 * len(on))
    momentum_score = None if entry is None else round(0.6 * entry + 0.4 * sig_score)
    supply_parts = [x for x in (fund["supply_demand_score"], volume_score) if x is not None]
    supply_score = round(sum(supply_parts) / len(supply_parts)) if supply_parts else None
    total = compute_ipo_total_score({
        "fundamental": fund["ipo_fundamental_score"], "momentum": momentum_score,
        "supply_demand": supply_score, "volume": volume_score, "material": material_score}, weights)
    offer = ipo_row.get("offer_price")
    price = candidate.get("current")
    return {"stage": stage if in_window else (ipo_row.get("watch_stage") or "WATCH_LOW"),
            "inWindow": in_window, "signals": on,
            "listingDate": listing.isoformat() if listing else None,
            "businessDaysSinceListing": business_days_between(listing, today, is_trading_day) if listing else None,
            "offerPrice": offer, "firstPrice": ipo_row.get("first_price"),
            "priceVsOfferPct": round((price - offer) / offer * 100, 1) if (offer and price) else None,
            "fundamental": fund,
            "ipo_fundamental_score": fund["ipo_fundamental_score"],
            "ipo_supply_demand_score": supply_score,
            "ipo_momentum_score": momentum_score,
            "ipo_total_score": total, "ipoTotalScore": total,
            "fundamental_data_quality": fund["fundamental_data_quality"]}
