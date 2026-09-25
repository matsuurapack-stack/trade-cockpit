# Market-Wide Discovery（Phase E・shadow）：登録済み銘柄の外から「今日突然動き始めた銘柄」を自動発見し、
# dynamic watch → Rolling Radar → Chart Context へ流す二段階Discoveryの純粋ロジック（DB・ネットワーク非依存）。
#
#   Tier 1 Broad Discovery（低頻度・広く）：yfinance JP screener（20分遅延）＋IPO等のタグから最大300銘柄へ絞る
#   Tier 2 Real-time Discovery（絞った銘柄だけ）：最大120銘柄を立花quoteで確認し、価格加速・出来高加速・高値接近/更新・
#          spread許容・movement上昇 が揃えばdynamic watchへ昇格（明確な急変ならhot poolへ直接昇格）
#   ・yfinanceの値をリアルタイムENTRY判定へ直接渡さない。昇格は必ず立花quoteで再確認する。
#   ・ENTRYには直接接続しない（entry_allowed=False）。Radar/Chart Context/Movementは既存の純粋関数を再利用する。
#   ・鮮度重視：broad候補は45分、realtime確認済みは20分で失効。
#
# しきい値はすべて暫定値（実市場で30〜50件のDiscovery episodeが貯まるまで調整しない）。

import datetime

import chart_context as cc
import early_radar as er
import movement_potential as mp
import rolling_radar as rr

BROAD_MAX = 300
RT_MAX = 120
TTL_BROAD_MIN = 45              # broad候補：最後にscreenerで再確認されてからの有効時間（30〜60分）
TTL_RT_MIN = 20                 # realtime確認済み：最後に立花quoteで再確認されてからの有効時間（15〜30分）
MIN_FACTORS = 2                 # 複数要素を満たした銘柄だけをpoolへ入れる

# Broad factors（screener行に対する判定。yfinanceは20分遅延）
GAINER_PCT = 3.0
REL_VOLUME = 2.0                # 時間調整後の相対出来高
TURNOVER_SURGE_YEN = 1.0e9      # 売買代金がこれ以上かつ相対出来高1.5倍以上
RANGE_EXPANSION_PCT = 4.0       # 当日値幅（前日終値比）
NEAR_HIGH_RATIO = 0.99
MIN_TURNOVER_YEN = 3.0e8        # 流動性フィルタ（これ未満は候補にしない）
MAX_SPREAD_PCT = 1.0
MIN_PRICE = 50.0

# Real-time promotion（立花quote）
PRICE_WINDOW_SEC = 180
PRICE_ACCEL_PCT = 0.4           # 直近3分の上昇率
PRICE_ACCEL_RATIO = 1.5         # 直前3分の上昇率に対する加速
VOL_ACCEL_RATIO = 2.0           # 直近3分の出来高増分 / 直前3分の出来高増分
VOL_ACCEL_MIN_YEN = 2.0e7       # 直近3分の売買代金増分の下限（ノイズ除外）
NEAR_HIGH_PCT = -0.5
RT_SPREAD_MAX_PCT = 0.5
PROMOTE_MIN_SIGNALS = 3
HISTORY_MAX = 60

STATUS_PRIORITY = {"HOT": 4, "PROMOTED": 3, "REALTIME": 2, "BROAD": 1}


def _f(x):
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


def _scale(x, lo, hi, out):
    if x is None or hi == lo:
        return 0.0
    return max(0.0, min(1.0, (x - lo) / (hi - lo))) * out


def volume_fraction(minutes_since_open):
    """その時刻までに出るはずの1日出来高の割合（暫定：線形、下限10%。寄り直後の過大評価を避ける）。"""
    if minutes_since_open is None:
        return 1.0
    return max(0.10, min(1.0, minutes_since_open / 300.0))


def normalize_screener_row(q):
    """yfinance screenerのquote → 判定用の行（コードは '.T' を除く）。"""
    sym = str(q.get("symbol") or "")
    code = sym[:-2] if sym.endswith(".T") else sym
    price, prev = _f(q.get("regularMarketPrice")), _f(q.get("regularMarketPreviousClose"))
    hi, lo, op = _f(q.get("regularMarketDayHigh")), _f(q.get("regularMarketDayLow")), _f(q.get("regularMarketOpen"))
    vol, avg = _f(q.get("regularMarketVolume")), _f(q.get("averageDailyVolume3Month"))
    bid, ask = _f(q.get("bid")), _f(q.get("ask"))
    spread = ((ask - bid) / ((ask + bid) / 2) * 100) if (bid and ask and ask >= bid and (ask + bid) > 0) else None
    return {"code": code, "name": q.get("shortName") or q.get("longName"), "price": price, "prevClose": prev, "high": hi, "low": lo,
            "open": op, "volume": vol, "avgVolume": avg, "changePct": _f(q.get("regularMarketChangePercent")),
            "marketCap": _f(q.get("marketCap")), "spreadPct": spread, "marketState": q.get("marketState"),
            "quoteTime": q.get("regularMarketTime"), "delayMin": q.get("exchangeDataDelayedBy")}


def broad_score(row, minutes_since_open=None, tags=()):
    """Broad Discovery：複数要素（gainer / rel_volume / turnover_surge / range_expansion / near_high / news / ipo /
    prev_day_mover）のうち MIN_FACTORS 以上を満たした銘柄だけ (score, factors, reasons) を返す。満たさなければNone。"""
    price, vol = row.get("price"), row.get("volume")
    if not price or price < MIN_PRICE or not vol:
        return None
    turnover = price * vol
    if turnover < MIN_TURNOVER_YEN:
        return None                                         # 流動性不足
    if row.get("spreadPct") is not None and row["spreadPct"] > MAX_SPREAD_PCT:
        return None
    chg = row.get("changePct")
    frac = volume_fraction(minutes_since_open)
    relvol = (vol / row["avgVolume"] / frac) if row.get("avgVolume") else None
    prev, hi, lo = row.get("prevClose"), row.get("high"), row.get("low")
    range_pct = ((hi - lo) / prev * 100) if (hi and lo and prev) else None
    factors, reasons, score = [], [], 0.0
    if chg is not None and chg >= GAINER_PCT:
        factors.append("gainer")
        reasons.append(f"前日比{chg:+.1f}%")
        score += _scale(chg, GAINER_PCT, 10.0, 30)
    if relvol is not None and relvol >= REL_VOLUME:
        factors.append("rel_volume")
        reasons.append(f"相対出来高{relvol:.1f}倍")
        score += _scale(relvol, REL_VOLUME, 6.0, 25)
    if turnover >= TURNOVER_SURGE_YEN and relvol is not None and relvol >= 1.5:
        factors.append("turnover_surge")
        reasons.append(f"売買代金{turnover / 1e8:.0f}億円")
        score += 10
    if range_pct is not None and range_pct >= RANGE_EXPANSION_PCT:
        factors.append("range_expansion")
        reasons.append(f"当日値幅{range_pct:.1f}%")
        score += _scale(range_pct, RANGE_EXPANSION_PCT, 12.0, 15)
    if hi and price >= hi * NEAR_HIGH_RATIO and (chg or 0) >= 1.0:
        factors.append("near_high")
        reasons.append("高値圏")
        score += 10
    for t, label, pts in (("news", "ニュース/材料", 5), ("ipo", "IPO/直近IPO", 5), ("prev_day_mover", "前日の急騰銘柄", 5)):
        if t in tags:
            factors.append(t)
            reasons.append(label)
            score += pts
    if len(factors) < MIN_FACTORS:
        return None
    return round(min(100.0, score)), factors, reasons


def new_entry(code, name, source, score, reasons, now, factors=None, price=None):
    return {"code": code, "name": name, "sources": [source], "discovered_at": now, "discovery_reason": list(reasons),
            "factors": list(factors or []), "broad_score": score, "last_seen_at": now, "latest_quote_at": None,
            "rt_confirmed_at": None, "promoted_at": None, "hot_at": None, "promote_reasons": [], "expired_at": None,
            "expire_reason": None, "status": "BROAD", "radar_at": None, "expanding_at": None, "entry_at": None,
            "chase_at": None, "price_at_discovery": price, "eval": None, "entry_allowed": False}


def merge_broad(pool, cands, now, broad_max=BROAD_MAX):
    """cands: [{"code","name","source","score","reasons","factors","price"}]。既存は再確認（last_seen更新・理由を統合）、
    新規はBROADで追加。上限を超えたら、昇格済みでない低スコアから外す。戻り値: 新規追加したコードのリスト。"""
    added = []
    for c in cands:
        e = pool.get(c["code"])
        if e is not None and e["status"] != "EXPIRED":
            e["last_seen_at"] = now
            if c["source"] not in e["sources"]:
                e["sources"].append(c["source"])
            e["broad_score"] = max(e["broad_score"], c["score"])
            for r in c["reasons"]:
                if r not in e["discovery_reason"]:
                    e["discovery_reason"].append(r)
            continue
        pool[c["code"]] = new_entry(c["code"], c.get("name"), c["source"], c["score"], c["reasons"], now, c.get("factors"), c.get("price"))
        added.append(c["code"])
    live = [e for e in pool.values() if e["status"] != "EXPIRED"]
    if len(live) > broad_max:
        drop = sorted((e for e in live if e["status"] == "BROAD"), key=lambda e: e["broad_score"])[:len(live) - broad_max]
        for e in drop:
            expire_entry(e, now, "BROAD_CAP")
    return added


def expire_entry(e, now, reason):
    e["status"], e["expired_at"], e["expire_reason"] = "EXPIRED", now, reason


def expire_pool(pool, now, ttl_broad_min=TTL_BROAD_MIN, ttl_rt_min=TTL_RT_MIN):
    """鮮度重視：古い候補は捨てる。BROADは最後のscreener再確認から45分、realtime確認済み（REALTIME/PROMOTED/HOT）は
    最後の立花quote再確認から20分。Radar発火中（radar_at済みで直近も動いている）はRadar ageへ引き継ぐため失効させない。"""
    expired = []
    for e in pool.values():
        if e["status"] == "EXPIRED":
            continue
        if e["status"] == "BROAD":
            if (now - e["last_seen_at"]).total_seconds() > ttl_broad_min * 60:
                expire_entry(e, now, "BROAD_STALE")
                expired.append(e["code"])
        else:
            ref = e.get("rt_confirmed_at") or e.get("promoted_at") or e["last_seen_at"]
            if e.get("radar_active"):
                continue
            if (now - ref).total_seconds() > ttl_rt_min * 60:
                expire_entry(e, now, "RT_STALE")
                expired.append(e["code"])
    return expired


def select_realtime(pool, rt_max=RT_MAX):
    """立花quoteで見る銘柄（最大120）。昇格済みを優先し、次にbroad_score順。"""
    live = [e for e in pool.values() if e["status"] != "EXPIRED"]
    live.sort(key=lambda e: (-STATUS_PRIORITY.get(e["status"], 0), -e["broad_score"]))
    return [e["code"] for e in live[:rt_max]]


# ---------------------------------------------------------------- real-time（立花quote）
def update_history(hist, code, quote, now):
    """hist: {code: [(epoch, price, volume)]}。同一quote時刻の重複は追加しない。"""
    t, v = _f(quote.get("t")), _f(quote.get("volume"))
    if t is None:
        return
    h = hist.setdefault(code, [])
    ts = now.timestamp()
    if h and abs(h[-1][0] - ts) < 1.0:
        return
    h.append((ts, t, v))
    if len(h) > HISTORY_MAX:
        del h[:len(h) - HISTORY_MAX]


def _at_or_before(h, ts):
    prev = None
    for s in h:
        if s[0] <= ts:
            prev = s
        else:
            break
    return prev


def realtime_signals(h, quote, now):
    """立花quoteの履歴から、価格加速・出来高加速・高値接近/更新・spread を判定する。
    履歴が短い場合は判定できない項目をNone（＝数えない）にし、推測しない。"""
    sig = {"price_accel": None, "vol_accel": None, "near_high": None, "new_high": None, "spread_ok": None, "detail": {}}
    t = _f(quote.get("t"))
    if t is None:
        return sig
    ts = now.timestamp()
    s1 = _at_or_before(h, ts - PRICE_WINDOW_SEC)
    s2 = _at_or_before(h, ts - 2 * PRICE_WINDOW_SEC)
    if s1 and s1[1]:
        recent = (t / s1[1] - 1) * 100
        prior = ((s1[1] / s2[1] - 1) * 100) if (s2 and s2[1]) else None
        sig["detail"].update({"chg_recent_pct": round(recent, 3), "chg_prior_pct": None if prior is None else round(prior, 3)})
        if prior is None:
            sig["price_accel"] = bool(recent >= PRICE_ACCEL_PCT * 1.5)          # 比較対象が無い間は厳しめ
        else:
            sig["price_accel"] = bool(recent >= PRICE_ACCEL_PCT and recent >= PRICE_ACCEL_RATIO * max(prior, 0.05))
    vol = _f(quote.get("volume"))
    if s1 and s1[2] is not None and vol is not None:
        dv_recent = vol - s1[2]
        dv_prior = (s1[2] - s2[2]) if (s2 and s2[2] is not None) else None
        sig["detail"].update({"dv_recent": dv_recent, "dv_prior": dv_prior})
        yen = dv_recent * t
        if dv_prior is not None and dv_prior > 0:
            sig["vol_accel"] = bool(dv_recent / dv_prior >= VOL_ACCEL_RATIO and yen >= VOL_ACCEL_MIN_YEN)
        elif dv_prior is not None:
            sig["vol_accel"] = bool(dv_recent > 0 and yen >= VOL_ACCEL_MIN_YEN)
    hi = _f(quote.get("high"))
    if hi:
        dist = (t / hi - 1) * 100
        sig["detail"]["dist_from_high_pct"] = round(dist, 3)
        sig["near_high"] = bool(dist >= NEAR_HIGH_PCT)
        known_max = max([x[1] for x in h] or [t])
        sig["new_high"] = bool(t >= hi * 0.9995 and (len(h) < 2 or t >= known_max))
    ask, bid = _f(quote.get("ask")), _f(quote.get("bid"))
    if ask and bid and ask >= bid and (ask + bid) > 0:
        sp = (ask - bid) / ((ask + bid) / 2) * 100
        sig["detail"]["spread_pct"] = round(sp, 3)
        sig["spread_ok"] = bool(sp <= RT_SPREAD_MAX_PCT)
    return sig


def evaluate_promotion(sig, movement_up=None):
    """dynamic watchへの昇格判定。判定できた確認要素のうち PROMOTE_MIN_SIGNALS 以上が真で、かつ加速系（価格/出来高）を
    1つ以上含むこと。spreadが許容外なら昇格しない。明確な急変（価格加速＋出来高加速＋高値更新＋spread許容）はhotへ直接昇格。
    戻り値: (level, reasons)  level ∈ {None, "PROMOTED", "HOT"}"""
    if sig.get("spread_ok") is False:
        return None, ["spread許容外"]
    hi_flag = bool(sig.get("near_high") or sig.get("new_high"))
    flags = [("価格加速", sig.get("price_accel")), ("出来高加速", sig.get("vol_accel")), ("当日高値接近/更新", hi_flag if sig.get("near_high") is not None else None),
             ("spread許容", sig.get("spread_ok")), ("movement上昇", movement_up)]
    reasons = [name for name, v in flags if v]
    accel = bool(sig.get("price_accel") or sig.get("vol_accel"))
    if sig.get("price_accel") and sig.get("vol_accel") and sig.get("new_high") and sig.get("spread_ok") is not False:
        return "HOT", reasons
    if accel and len(reasons) >= PROMOTE_MIN_SIGNALS:
        return "PROMOTED", reasons
    return None, reasons


def evaluate_entry_state(bars, quote, minutes_since_open=None, market_rs=None):
    """昇格した銘柄を既存の Rolling Radar → Chart Context / Movement に流す（新しいRadarロジックは作らない）。
    barsは立花quoteから作った内部5分足（list-of-dict）。entry_allowed=Falseの観測用。"""
    out = {"n": 0, "radar": None, "rolling": None, "pattern": None, "activity": None, "movement": None, "entry_eligible": False,
           "chase": False, "expanding": False, "movement_recommendation": None, "confidence": "UNKNOWN"}
    b = cc.normalize_bars(bars)
    n = len(b["closes"]) if b else 0
    out["n"] = n
    if n < er.MIN_BARS:
        return out
    t = _f(quote.get("t"))
    vwap, hi, lo = _f(quote.get("vwap")), _f(quote.get("high")), _f(quote.get("low"))
    spread = None
    ask, bid = _f(quote.get("ask")), _f(quote.get("bid"))
    if ask and bid and ask >= bid and (ask + bid) > 0:
        spread = (ask - bid) / ((ask + bid) / 2) * 100
    radar = er.evaluate_radar(bars, quote={"t": t}, vwap=vwap, day_high=hi, market_rs=market_rs, spread_pct=spread)
    rolling = rr.evaluate_rolling(bars, quote={"t": t}, vwap=vwap, day_high=hi, market_rs=market_rs, spread_pct=spread)
    out["radar"], out["rolling"] = radar["state"], rolling["state"]
    out["radar_hot"], out["rolling_hot"] = er.radar_hot(radar), bool(rolling["hot"])
    out["radar_watch"], out["rolling_watch"] = radar["state"] in ("RADAR_ACTIVE",), bool(rolling["watch"])
    if n >= mp.MIN_BARS:
        chart = cc.evaluate_chart_context(bars, quote={"t": t}, vwap=vwap, day_high=hi, day_low=lo, minutes_since_open=minutes_since_open)
        mv = mp.evaluate_movement(bars, quote={"t": t}, vwap=vwap, day_high=hi, day_low=lo, market_rs=market_rs, chart=chart,
                                  minutes_since_open=minutes_since_open)
        rec = mp.movement_recommendation("ENTRY_READY", "ENTRY_READY", mv, chart)     # 旧判定はENTRY可と仮定した上限（観測用）
        out.update({"pattern": chart["pattern"], "activity": mv["activity_state"], "movement": mv["movement_potential_score"],
                    "movement_recommendation": rec, "confidence": chart["confidence"], "chase": chart["pattern"] in ("CHASE", "EXTENDED", "EXHAUSTION"),
                    "expanding": mv["activity_state"] == "EXPANDING", "too_late": mv["too_late"],
                    "entry_eligible": bool(rec == "ENTRY_READY" and chart["pattern"] in ("PULLBACK_READY", "EARLY_BREAKOUT", "VWAP_RECLAIM"))})
    return out


def apply_realtime(entry, sig, level, reasons, ev, now):
    """1回のrealtime確認結果をpoolのエントリへ反映（状態遷移とマイルストーン時刻）。戻り値: 新しく付いたイベント名のリスト。"""
    new = []
    entry["latest_quote_at"] = now
    if entry["status"] == "BROAD":
        entry["status"] = "REALTIME"
        entry["rt_first_at"] = now
        new.append("realtime_first")
    if level in ("PROMOTED", "HOT"):
        entry["rt_confirmed_at"] = now
        entry["promote_reasons"] = reasons
        if entry["promoted_at"] is None:
            entry["promoted_at"] = now
            new.append("promoted")
        if entry["status"] in ("REALTIME", "BROAD"):
            entry["status"] = "PROMOTED"
        if level == "HOT" and entry["hot_at"] is None:
            entry["hot_at"] = now
            new.append("hot")
        if level == "HOT":
            entry["status"] = "HOT"
    elif entry["status"] in ("PROMOTED", "HOT") and entry.get("rt_confirmed_at") is None:
        entry["rt_confirmed_at"] = now
    if ev:
        entry["eval"] = {k: ev.get(k) for k in ("n", "radar", "rolling", "pattern", "activity", "movement", "movement_recommendation",
                                                 "entry_eligible", "chase", "expanding", "confidence")}
        entry["radar_active"] = bool(ev.get("rolling_hot") or ev.get("radar_hot") or ev.get("rolling_watch"))
        for name, cond in (("radar_at", ev.get("radar_hot") or ev.get("rolling_hot") or ev.get("rolling_watch")),
                           ("expanding_at", ev.get("expanding")), ("entry_at", ev.get("entry_eligible")), ("chase_at", ev.get("chase"))):
            if cond and entry.get(name) is None:
                entry[name] = now
                new.append(name)
    return new


def build_shadow_list(pool, now, top_n=15):
    """shadowMovement.marketDiscovery：🌐 市場発見（銘柄・発見理由・source・discovery age・movement・Radar状態）。
    昇格済みを優先。買い判定ではない（entryAllowed=False）。"""
    live = [e for e in pool.values() if e["status"] != "EXPIRED"]
    live.sort(key=lambda e: (-STATUS_PRIORITY.get(e["status"], 0), -e["broad_score"]))
    out = []
    for e in live[:top_n]:
        ev = e.get("eval") or {}
        out.append({"code": e["code"], "name": e.get("name"), "status": e["status"], "sources": e["sources"],
                    "reasons": e["discovery_reason"][:4], "promoteReasons": e.get("promote_reasons") or [],
                    "broadScore": e["broad_score"], "discoveryAgeMinutes": round((now - e["discovered_at"]).total_seconds() / 60.0, 1),
                    "movementScore": ev.get("movement"), "radarState": ev.get("rolling") if ev.get("rolling") not in (None, "NONE") else ev.get("radar"),
                    "pattern": ev.get("pattern"), "promotedAt": e["promoted_at"].isoformat() if e.get("promoted_at") else None,
                    "entryAllowed": False, "label": "🌐 市場発見"})
    return out


def summarize_pool(pool):
    live = [e for e in pool.values() if e["status"] != "EXPIRED"]
    by = {}
    for e in live:
        by[e["status"]] = by.get(e["status"], 0) + 1
    return {"live": len(live), "by_status": by, "expired": sum(1 for e in pool.values() if e["status"] == "EXPIRED"), "total_seen": len(pool)}
