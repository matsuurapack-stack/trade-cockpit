# 買付余力ベースの候補評価＋IPO再評価＋shadow_watchの純粋ロジック（DB・ネットワーク非依存）。
#
# 既存のTOP5選定（server._select_entry_ready_top5）・watchlist・trade learningを二重実装しない
# ため、ここには「余力から見た買える/買えない判定」「資金効率」「集中リスク」「IPO段階遷移」
# 「IPOスコア」「shadow_watch期間・トリガー判定」だけを置く。server.py側が既存関数から呼ぶ。

import datetime

LOT_SIZE = 100  # SBI証券・日本株の原則単元
CONCENTRATION_CAUTION = 0.70
CONCENTRATION_STRONG = 0.85

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


def capital_efficiency_score(candidate, buyability):
    """capital_efficiency_score = trade_score × liquidity × momentum ÷ capital_required_factor。
    低位株が単純に有利にならないよう、必要資金は「余力使用率」の緩やかなペナルティ
    （1+0.3×使用率）としてのみ効かせ、株価の逆数は使わない。流動性・急騰・イベントリスクは
    係数で減点する。"""
    score = float(candidate.get("entryScore") or 0)
    liquidity = candidate.get("liquidityFactor")
    liquidity = 1.0 if liquidity is None else max(0.5, min(1.1, float(liquidity)))
    change = candidate.get("changePct")
    if change is not None and change >= 15:
        liquidity *= 0.8  # 急騰リスク（高値掴み・値動き過熱）
    if candidate.get("eventRiskLevel") == "HIGH":
        liquidity *= 0.85
    momentum = {"MOMENTUM_REACCELERATING": 1.05, "MOMENTUM_DECAY": 0.9}.get(candidate.get("momentumState"), 1.0)
    usage = (buyability or {}).get("cashUsageRatio")
    required_factor = 1.0 + 0.3 * min(max(usage or 0.0, 0.0), 1.0)
    return round(score * liquidity * momentum / required_factor, 2)


def annotate_capital(candidate, cash_available):
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
    out["capitalEfficiencyScore"] = capital_efficiency_score(candidate, b) if cash_available is not None else None
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
            # 複数銘柄に分散すると1銘柄集中は解消される。単一銘柄の使用率が高い組は減点。
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
      ①各候補へ余力情報を付与 ②買える候補（buyable != False）だけでselect_fn（既存TOP5選定）を実行
      ③買えない候補のうち注目に値するもの（Tier1〜3相当の状態）を別枠notBuyableNotableへ。
    select_fnは既存の_select_entry_ready_top5（5要素タプル）を想定。戻り値は
    (select_fnの結果, annotated全候補, notBuyableNotable, combinations)。"""
    if cash_available is None:
        return select_fn(candidates), list(candidates), [], []
    annotated = [annotate_capital(c, cash_available) for c in candidates]
    buyable_pool = [c for c in annotated if c["buyable"] is not False]
    result = select_fn(buyable_pool)
    notable_states = _BUY_NOW_STATES + ("WAIT_BREAKOUT", "WAIT_PULLBACK", "WATCH")
    notable = [c for c in annotated if c["buyable"] is False and c.get("entryState") in notable_states
               and (c.get("entryScore") or 0) >= 45]
    notable.sort(key=lambda c: -(c.get("entryScore") or 0))
    combos = suggest_combinations(annotated, cash_available)
    return result, annotated, notable[:5], combos


def rerank_by_capital_efficiency(top5):
    """同じTier内だけ資金効率で並べ替える（Tier間の順序＝今すぐ入れる優先は変えない）。"""
    if not top5 or top5[0].get("capitalEfficiencyScore") is None:
        return top5
    return [{**c, "rank": i + 1} for i, c in enumerate(
        sorted(top5, key=lambda c: (c.get("candidateTier") or 9, -(c.get("capitalEfficiencyScore") or 0))))]


# ---------------------------------------------------------------- IPO

IPO_WATCH_BUSINESS_DAYS = 20
SHADOW_WATCH_DAYS = {"IPO": 20, "MATERIAL": 3, "SURGE": 2}
IPO_STAGES = ("WATCH_LOW", "WATCH", "BUY_CANDIDATE")

_IPO_STRONG_SIGNALS = ("volume_surge", "prev_high_break", "day_high_update", "five_min_high_update")
_IPO_SUPPORT_SIGNALS = ("vwap_recovered", "higher_low", "outperform_index", "outperform_peers",
                        "bid_increase", "ask_absorb")


def _stage_rank(stage):
    return IPO_STAGES.index(stage) if stage in IPO_STAGES else 0


def evaluate_ipo_stage(current_stage, signals, entry_score=None):
    """朝に弱くても再評価で昇格できる。降格しても下限はWATCH_LOW（DELETEにはしない）。
      BUY_CANDIDATE: 出来高急増 かつ 高値更新系（当日高値/前日高値ブレイク/5分足高値）、
                     もしくはentry_score>=85
      WATCH        : 何らかの回復/強さシグナル（VWAP回復・安値切上げ・逆行高・板・高値更新等）
                     もしくはentry_score>=60
      WATCH_LOW    : それ以外（朝の下落のみでもここに残す）
    戻り値: (stage, reasons)"""
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
    朝の下落だけ(morning_drop_only)では絶対に削除しない。IPO監視期間内は3条件以上を要求、
    それ以外は3条件以上（同じ基準）。"""
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


def compute_ipo_fundamental_score(data):
    """data（全て任意、未知はNone→除外して重みを再正規化しcoverageで信頼度を示す）:
    revenue_growth_pct / profit_growth_pct / margin_change_pt / guidance（'UPWARD'/'IN_LINE'/
    'DOWNWARD'/None）/ kpi_growth_pct / business_quality（0-100）/ market_size_score（0-100）/
    per（バリュエーション。低いほど高得点）/ lockup_risk（0=なし〜100=大）/
    vc_holding_pct / float_ratio_pct。"""
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
    supply_parts = [comp[k] for k in ("lockup_risk", "vc_overhang", "float_size") if comp[k] is not None]
    supply = (sum(supply_parts) / len(supply_parts)) if supply_parts else None
    r = lambda x: None if x is None else round(x)
    return {"ipo_fundamental_score": r(total), "revenue_growth_score": r(comp["revenue_growth"]),
            "profit_growth_score": r(comp["profit_growth"]), "kpi_score": r(comp["kpi_growth"]),
            "valuation_score": r(comp["valuation"]), "supply_demand_score": r(supply),
            "coverage": round(wsum, 2)}


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


IPO_LEARNING_RULE = {
    "rule_name": "直近IPOの早期監視解除禁止",
    "category": "ENTRY",
    "priority": "HIGH",
    "rule_text": ("直近IPOは朝の下落だけで監視対象から外さない。最低でもWATCH_LOWとして残す。"
                  "IPO後20営業日以内は、出来高・VWAP・高値更新・材料・需給を継続評価する。"
                  "ユーザーが手動で監視解除してもshadow_watchを続ける。"
                  "再上昇条件を満たした場合はWATCHまたはBUY_CANDIDATEへ復帰させる。"),
}


def build_ipo_info(ipo_row, candidate, signals, today, is_trading_day=None, weights=None):
    """IPO銘柄1件分の再評価結果（段階・シグナル・IPOスコア群）を作る純粋関数。
    ipo_row: ipo_stocksの行（listing_date='YYYY-MM-DD'|date|None、fundamentals_json、watch_stage、
    offer_price）。candidate: _score_entry_candidatesの候補dict（entryScore/current/reasons等）。"""
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
    fundamentals = compute_ipo_fundamental_score(ipo_row.get("fundamentals_json") or {})
    tavr = (candidate.get("scoreBreakdown") or {}).get("volumeRatio") or candidate.get("volumeRatio")
    volume_score = None if tavr is None else round(_lin(tavr, 0.5, 3.0))
    material_score = 70 if any("好材料" in r for r in (candidate.get("reasons") or [])) else 40
    total = compute_ipo_total_score({
        "fundamental": fundamentals["ipo_fundamental_score"],
        "momentum": candidate.get("entryScore"),
        "supply_demand": fundamentals["supply_demand_score"],
        "volume": volume_score, "material": material_score}, weights)
    offer = ipo_row.get("offer_price")
    price = candidate.get("current")
    return {"stage": stage if in_window else (ipo_row.get("watch_stage") or "WATCH_LOW"),
            "inWindow": in_window, "signals": on,
            "listingDate": listing.isoformat() if listing else None,
            "businessDaysSinceListing": business_days_between(listing, today, is_trading_day) if listing else None,
            "offerPrice": offer,
            "priceVsOfferPct": round((price - offer) / offer * 100, 1) if (offer and price) else None,
            "fundamental": fundamentals, "ipoTotalScore": total}
