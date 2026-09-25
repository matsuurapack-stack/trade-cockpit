# Chart Context Engine の過去トレード検証（読み取り専用）。
#   python backtest_chart_context.py
#
# hindsight bias禁止：判定に使うのは「ENTRY時刻までに完成していた5分足」だけ（slice_bars_before）。
# ENTRY後の足は「結果ラベル（伸びた/横横/下落）」の算出にだけ使い、判定へは一切渡さない。
# 銘柄の強さ(stock_strength)は当時の値が保存されていないため固定値(BACKTEST_STRENGTH=60)で
# 「強い銘柄」と仮定し、ENTRYタイミング側（チャートパターン）の評価に絞る。

import datetime
import json
import sys

import chart_context as cc

JST = datetime.timezone(datetime.timedelta(hours=9))
BACKTEST_STRENGTH = 60
EXCLUDE_IDS = {46, 47}   # 同一価格・-10%・1分差の重複的な記録（テスト操作の疑い）


def slice_bars_before(bars, entry_dt):
    """bars: [{"start": datetime, open/high/low/close/volume}, ...]。ENTRY時刻までに完成していた足
    （start+5分 <= entry_dt）だけを返す。ENTRY時刻を含む足（形成中）と、それ以降の足は使わない。"""
    delta = datetime.timedelta(minutes=5)
    return [b for b in bars if b["start"] + delta <= entry_dt]


def bars_after(bars, entry_dt, exit_dt):
    """結果ラベル専用：ENTRY後〜決済までの足。判定には使わない。"""
    return [b for b in bars if b["start"] >= entry_dt.replace(second=0, microsecond=0) and b["start"] <= exit_dt]


def to_engine_bars(bars):
    return {"opens": [b["open"] for b in bars], "highs": [b["high"] for b in bars], "lows": [b["low"] for b in bars],
            "closes": [b["close"] for b in bars], "volumes": [b["volume"] for b in bars]}


def outcome_label(entry_price, pnl, post_bars):
    """結果ラベル（判定とは独立）：QUICK_WIN（30分以内に+0.7%以上）/ SIDEWAYS（値幅±0.5%以内で決済）/
    DECLINE（損失）/ WIN / FLAT。"""
    if entry_price and post_bars:
        first6 = post_bars[:6]
        mfe = (max(b["high"] for b in first6) / entry_price - 1) * 100
        rng = (max(b["high"] for b in post_bars) - min(b["low"] for b in post_bars)) / entry_price * 100
    else:
        mfe, rng = None, None
    if pnl is not None and pnl < 0:
        return "DECLINE", mfe
    if mfe is not None and mfe >= 0.7:
        return "QUICK_WIN", mfe
    if rng is not None and rng <= 0.5:
        return "SIDEWAYS", mfe
    return ("WIN" if (pnl or 0) > 0 else "FLAT"), mfe


def evaluate_trade(trade, day_bars):
    entry_dt = trade["entry_dt"]
    prior = slice_bars_before(day_bars, entry_dt)
    minutes = (entry_dt.hour * 60 + entry_dt.minute) - 540
    cc_res = cc.evaluate_chart_context(
        to_engine_bars(prior), quote={"t": trade["entry_price"]},
        day_high=max([b["high"] for b in prior] + [trade["entry_price"]]) if prior else None,
        day_low=min([b["low"] for b in prior] + [trade["entry_price"]]) if prior else None,
        minutes_since_open=minutes if 0 <= minutes <= 150 else None)
    decision = cc.entry_decision(BACKTEST_STRENGTH, cc_res)
    post = bars_after(day_bars, entry_dt, trade["exit_dt"])
    label, mfe = outcome_label(trade["entry_price"], trade["net_pnl"], post)
    return {"id": trade["id"], "code": trade["code"], "entry": entry_dt.isoformat(), "pattern": cc_res["pattern"],
            "timing": cc_res["entry_timing_score"], "confidence": cc_res["confidence"], "bars": cc_res["barCount"],
            "decision": decision, "net_pnl": trade["net_pnl"], "label": label, "mfe30m": None if mfe is None else round(mfe, 2),
            "reasons": cc_res["reasons"][:3], "penalties": cc_res["penalties"][:3]}


def summarize(results):
    def stats(rows):
        n = len(rows)
        wins = sum(1 for r in rows if (r["net_pnl"] or 0) > 0)
        pnl = sum(r["net_pnl"] or 0 for r in rows)
        loss_rows = [r for r in rows if (r["net_pnl"] or 0) < 0]
        return {"n": n, "wins": wins, "win_rate": round(wins / n, 2) if n else None, "net_pnl_sum": round(pnl),
                "loss_count": len(loss_rows), "loss_sum": round(sum(r["net_pnl"] for r in loss_rows))}
    out = {"all": stats(results)}
    out["decision_ENTRY_READY"] = stats([r for r in results if r["decision"] == "ENTRY_READY"])
    out["decision_NO_ENTRY_CHASE"] = stats([r for r in results if r["decision"] == "NO_ENTRY_CHASE"])
    out["decision_NO_ENTRY_FAILED_BREAK"] = stats([r for r in results if r["decision"] == "NO_ENTRY_FAILED_BREAK"])
    out["decision_other(WAIT/WATCH)"] = stats([r for r in results if r["decision"] in ("WATCH", "WAIT_PULLBACK", "WAIT_BREAKOUT")])
    for pat in ("PULLBACK_READY", "EARLY_BREAKOUT", "VWAP_RECLAIM", "BREAKOUT_CONFIRMED"):
        out[f"pattern_{pat}"] = stats([r for r in results if r["pattern"] == pat])
    out["pattern_histogram"] = {}
    for r in results:
        out["pattern_histogram"][r["pattern"]] = out["pattern_histogram"].get(r["pattern"], 0) + 1
    avoided = [r for r in results if r["decision"] in ("NO_ENTRY_CHASE", "NO_ENTRY_FAILED_BREAK")]
    out["avoidance"] = {"blocked_trades": len(avoided), "blocked_losers": sum(1 for r in avoided if (r["net_pnl"] or 0) < 0),
                        "loss_avoided_yen": -round(sum(r["net_pnl"] for r in avoided if (r["net_pnl"] or 0) < 0)),
                        "profit_forgone_yen": round(sum(r["net_pnl"] for r in avoided if (r["net_pnl"] or 0) > 0))}
    return out


def load_day_bars(code, day):
    """yfinanceの5分足（当日分）→ [{"start": aware datetime(JST), ...}]。取得不可ならNone。"""
    import yfinance as yf
    h = yf.Ticker(f"{code}.T").history(period="60d", interval="5m")
    if h is None or h.empty:
        return None
    rows = []
    for ts, r in h.iterrows():
        t = ts.to_pydatetime().astimezone(JST)
        if t.date() == day:
            rows.append({"start": t, "open": float(r["Open"]), "high": float(r["High"]), "low": float(r["Low"]),
                         "close": float(r["Close"]), "volume": float(r["Volume"] or 0)})
    return rows or None


def main():
    import server
    import investment_db as db
    pool = db._get_pool(server.DATABASE_URL)
    with pool.connection() as c:
        rows = c.execute("select id, code, entry_price, net_pnl, acquired_at, closed_at from trade_history "
                         "where user_id='matsuura' and acquired_at is not null order by id").fetchall()
    trades, skipped = [], []
    for r in rows:
        if r[0] in EXCLUDE_IDS:
            skipped.append((r[0], "excluded(suspected test record)"))
            continue
        trades.append({"id": r[0], "code": r[1], "entry_price": float(r[2]), "net_pnl": float(r[3]) if r[3] is not None else None,
                       "entry_dt": r[4].astimezone(JST), "exit_dt": r[5].astimezone(JST)})
    results, cache = [], {}
    for t in trades:
        key = (t["code"], t["entry_dt"].date())
        if key not in cache:
            try:
                cache[key] = load_day_bars(*key)
            except Exception as e:
                cache[key] = None
                print("bars取得失敗", key, e, file=sys.stderr)
        day_bars = cache[key]
        if not day_bars:
            skipped.append((t["id"], "5m bars unavailable"))
            continue
        results.append(evaluate_trade(t, day_bars))
    print(json.dumps({"summary": summarize(results), "skipped": skipped, "trades": results}, ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    main()
