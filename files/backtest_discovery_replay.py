# Market Discovery の再現検証（読み取り専用）：ある銘柄が「登録されていなかった」場合に、
#   Broad Discovery（screener）→ realtime promotion（立花quote）→ Rolling Radar → EXPANDING / ENTRY相当 / CHASE
# の各段階を何時に通過できたかを、過去の1分足で再生する。
#   python backtest_discovery_replay.py 627A 2026-09-24 2026-09-25
#
# 近似（結果の読み方に注意）：
#  ・screenerはyfinanceの20分遅延を再現：時刻Tの判定に使うのは T-20分 までに完成した1分足だけ（10分ごとに実行）。
#    寄り前(08:50)の実行は前営業日のデータ（前日の急騰銘柄タグ）。相対出来高の平均は日足の直近3か月平均（IPO直後は取得できた日数のみ）。
#  ・立花quoteの30秒ポーリングは1分足で代用（価格＝1分足終値、出来高＝当日累計）。bid/askが無いのでspreadは未判定（数えない）。
#  ・Radar/Chart Context/Movementは、発見以後にquoteから作った内部5分足だけで評価（発見前の足は使えない＝本番と同じ）。
#  ・ENTRY相当は旧判定がENTRY可だったと仮定した上限（実際に買えるという意味ではない）。
#  ・未来データは判定に使わない。しきい値は結果に合わせて調整しない。

import datetime
import sys
import warnings

import market_discovery as md

JST = datetime.timezone(datetime.timedelta(hours=9))
DELAY_MIN = 20
STEP_MIN = 10


def load_1m(code):
    import yfinance as yf
    h = yf.Ticker(f"{code}.T").history(period="7d", interval="1m")
    rows = []
    for ts, r in h.iterrows():
        rows.append({"t": ts.to_pydatetime().astimezone(JST), "open": float(r["Open"]), "high": float(r["High"]), "low": float(r["Low"]),
                     "close": float(r["Close"]), "volume": float(r["Volume"] or 0)})
    return rows


def load_daily(code):
    import yfinance as yf
    h = yf.Ticker(f"{code}.T").history(period="6mo", interval="1d")
    return [{"d": ts.to_pydatetime().date(), "close": float(r["Close"]), "volume": float(r["Volume"] or 0),
             "high": float(r["High"]), "low": float(r["Low"]), "open": float(r["Open"])} for ts, r in h.iterrows()]


def minutes_of(t):
    m = t.hour * 60 + t.minute
    if m < 540:
        return None
    if m <= 690:
        return m - 540
    if m < 750:
        return 150
    return min(300, 150 + (m - 750))


def screener_view(day_rows, upto, prev_close, avg_vol):
    """upto（時刻）までに完成した1分足だけから作る、yfinance screener相当の行。"""
    seg = [r for r in day_rows if r["t"] + datetime.timedelta(minutes=1) <= upto]
    if not seg:
        return None
    price = seg[-1]["close"]
    hi, lo, op = max(r["high"] for r in seg), min(r["low"] for r in seg), seg[0]["open"]
    vol = sum(r["volume"] for r in seg)
    return {"code": "X", "name": "X", "price": price, "prevClose": prev_close, "high": hi, "low": lo, "open": op, "volume": vol,
            "avgVolume": avg_vol, "changePct": (price / prev_close - 1) * 100 if prev_close else None, "spreadPct": None}


def to_5m(seg):
    """1分足→5分足（list-of-dict）。発見以後のquoteから作る内部5分足に相当（最後の足は形成中）。"""
    bars, cur, slot = [], None, None
    for r in seg:
        s = (r["t"].hour * 60 + r["t"].minute) // 5
        if s != slot:
            cur = {"open": r["open"], "high": r["high"], "low": r["low"], "close": r["close"], "volume": r["volume"]}
            bars.append(cur)
            slot = s
        else:
            cur["high"], cur["low"], cur["close"] = max(cur["high"], r["high"]), min(cur["low"], r["low"]), r["close"]
            cur["volume"] += r["volume"]
    return bars


def replay_day(all_1m, daily, day):
    day_rows = [r for r in all_1m if r["t"].date() == day]
    if not day_rows:
        return None
    prev_days = [d for d in daily if d["d"] < day]
    prev_close = prev_days[-1]["close"] if prev_days else None
    avg_vol = (sum(d["volume"] for d in prev_days[-60:]) / len(prev_days[-60:])) if prev_days else None
    prior_rows = [r for r in all_1m if r["t"].date() == (prev_days[-1]["d"] if prev_days else None)]
    out = {"day": day, "prev_close": prev_close, "avg_vol": avg_vol, "days_for_avg": len(prev_days[-60:])}
    # ---- Tier 1: Broad（寄り前は前営業日データ、以降は10分ごとに20分遅延のビュー）
    discovered = None
    runs = []
    pre = datetime.datetime.combine(day, datetime.time(8, 50), tzinfo=JST)
    if prior_rows and len(prev_days) >= 2:
        pc = prev_days[-2]["close"]
        v = screener_view(prior_rows, datetime.datetime.combine(prev_days[-1]["d"], datetime.time(15, 31), tzinfo=JST), pc, avg_vol)
        runs.append((pre, v, None, {"prev_day_mover"}))
    t = datetime.datetime.combine(day, datetime.time(9, 10), tzinfo=JST)
    end = datetime.datetime.combine(day, datetime.time(15, 30), tzinfo=JST)
    while t <= end:
        if not (datetime.time(11, 30) < t.time() < datetime.time(12, 30)):
            runs.append((t, screener_view(day_rows, t - datetime.timedelta(minutes=DELAY_MIN), prev_close, avg_vol),
                         minutes_of(t - datetime.timedelta(minutes=DELAY_MIN)), set()))
        t += datetime.timedelta(minutes=STEP_MIN)
    scan_log = []
    for t, view, mins, tags in runs:
        if not view:
            continue
        res = md.broad_score(view, minutes_since_open=mins if mins is not None else 300, tags=tags)
        scan_log.append((t, res))
        if res and discovered is None:
            discovered = {"at": t, "score": res[0], "factors": res[1], "reasons": res[2], "seen_price": view["price"]}
    out["discovered"] = discovered
    out["scan_log"] = scan_log
    if not discovered:
        return out
    # ---- Tier 2: 発見以後を1分ごとにポーリング（立花quote相当）
    entry = md.new_entry("X", "X", "YF", discovered["score"], discovered["reasons"], discovered["at"], discovered["factors"])
    hist = {}
    poll_rows = [r for r in day_rows if r["t"] + datetime.timedelta(minutes=1) > discovered["at"]]
    cum_vol = sum(r["volume"] for r in day_rows if r["t"] + datetime.timedelta(minutes=1) <= discovered["at"])
    run_hi = max([r["high"] for r in day_rows if r["t"] + datetime.timedelta(minutes=1) <= discovered["at"]] or [0])
    seg = []
    events = {}
    for r in poll_rows:
        now = r["t"] + datetime.timedelta(minutes=1)
        cum_vol += r["volume"]
        run_hi = max(run_hi, r["high"])
        seg.append(r)
        quote = {"t": r["close"], "volume": cum_vol, "high": run_hi, "low": min(x["low"] for x in day_rows if x["t"] <= r["t"]),
                 "vwap": None, "ask": None, "bid": None}
        md.update_history(hist, "X", quote, now)
        sig = md.realtime_signals(hist["X"], quote, now)
        bars = to_5m(seg)
        ev = md.evaluate_entry_state(bars, quote, minutes_since_open=minutes_of(now))
        level, reasons = md.evaluate_promotion(sig)
        for name in md.apply_realtime(entry, sig, level, reasons, ev, now):
            events.setdefault(name, (now, r["close"], reasons if name in ("promoted", "hot") else None))
    out["events"] = events
    out["entry"] = entry
    return out


def fmt(t):
    return t.strftime("%H:%M") if t else "—"


def mins(a, b):
    return None if not (a and b) else round((b - a).total_seconds() / 60.0)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    warnings.filterwarnings("ignore")
    import backtest_radar_replay as br
    code = sys.argv[1]
    all_1m, daily = load_1m(code), load_daily(code)
    for d in sys.argv[2:]:
        day = datetime.date.fromisoformat(d)
        res = replay_day(all_1m, daily, day)
        print(f"\n=== {code} {d}（未登録だったと仮定） 前日終値 {res and res['prev_close']} / 相対出来高の平均は日足{res and res['days_for_avg']}日分 ===")
        if not res:
            print("  1分足なし")
            continue
        for t, r in res["scan_log"][:8]:
            print(f"  screener {fmt(t)}: {'発見 ' + str(r[1]) + ' ' + '/'.join(r[2]) if r else '該当なし'}")
        disc = res["discovered"]
        if not disc:
            print("  → Discovery Poolに入らなかった")
            continue
        ev = res["events"]
        get = lambda k: ev.get(k, (None,))[0]
        print(f"  Broad Discovery（Poolへ）: {fmt(disc['at'])}（スコア{disc['score']}・{'/'.join(disc['factors'])}・{'/'.join(disc['reasons'])}）※screener値は約20分前の状態")
        for k, label in (("realtime_first", "立花quote監視の開始"), ("promoted", "realtime昇格（dynamic watch）"), ("hot", "hot pool直接昇格"),
                         ("radar_at", "Rolling/Early Radar（発見後の内部5分足）"), ("expanding_at", "EXPANDING"), ("entry_at", "ENTRY相当（上限）"),
                         ("chase_at", "CHASE系")):
            t = get(k)
            extra = f"  価格{ev[k][1]:.0f}" if k in ev else ""
            why = f"  理由={ev[k][2]}" if (k in ev and ev[k][2]) else ""
            print(f"    {label}: {fmt(t)}{extra}{why}   （発見から{mins(disc['at'], t)}分）" if t else f"    {label}: —")
        # 参考：登録済み（寄りから全足を見られる）場合の時刻
        rows5 = br.load_day("627A" if code == "627A" else code, day)
        _tl, first, _det = br.replay(rows5)
        print(f"  [参考] 登録済みだった場合（寄りからの5分足）: Rolling hot {first.get('rolling_hot', '—')} / EXPANDING {first.get('expanding', '—')} / "
              f"ENTRY相当 {first.get('entry_ready', '—')} / CHASE {first.get('chase', '—')}（足の終了時刻）")


if __name__ == "__main__":
    main()
