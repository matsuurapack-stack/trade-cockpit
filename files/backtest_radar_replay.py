# Early Momentum Radar と通常判定（Chart Context / Movement）を、5分足を1本ずつ追加する形で再生して
# 「初検出時刻」を比較する（読み取り専用）。
#   python backtest_radar_replay.py 627A 2026-09-24 2026-09-25
#
# hindsight bias禁止：各時点で使うのは、その時点までに完成した5分足だけ。しきい値は結果に合わせて調整しない。
# 時刻は「足の終了時刻」（＝その足が完成して判定できる最も早い時刻）で表す。
# ENTRY_READY相当は、通常判定が『既存のENTRY条件は満たしている』と仮定した上限（最も早い可能性）：
#   パターンが PULLBACK_READY / EARLY_BREAKOUT / VWAP_RECLAIM で、TOO_LATEでなく、動いている（LOW_ACTIVITY/FADINGでない）。

import datetime
import sys
import warnings

import chart_context as cc
import early_radar as er
import movement_potential as mp

JST = datetime.timezone(datetime.timedelta(hours=9))
RADAR_HOT_STATES = ("RADAR_SURGE", "RADAR_EXPANDING", "RADAR_PRE_BREAKOUT")


def load_day(code, day):
    import yfinance as yf
    h = yf.Ticker(f"{code}.T").history(period="5d", interval="5m")
    rows = []
    for ts, r in h.iterrows():
        t = ts.to_pydatetime().astimezone(JST)
        if t.date() == day:
            rows.append({"start": t, "open": float(r["Open"]), "high": float(r["High"]), "low": float(r["Low"]),
                         "close": float(r["Close"]), "volume": float(r["Volume"] or 0)})
    return rows


def replay(rows):
    """rows: 1日分の5分足（時刻順）。各時点の判定と、初検出時刻を返す。"""
    timeline, first = [], {}

    def mark(name, cond, t):
        if cond and name not in first:
            first[name] = t
    for i in range(2, len(rows) + 1):
        seg = rows[:i]
        end = seg[-1]["start"] + datetime.timedelta(minutes=5)
        bars = {k: [b[k2] for b in seg] for k, k2 in (("opens", "open"), ("highs", "high"), ("lows", "low"),
                                                       ("closes", "close"), ("volumes", "volume"))}
        price = bars["closes"][-1]
        dh, dl = max(bars["highs"]), min(bars["lows"])
        minutes = (end.hour * 60 + end.minute) - 540
        rd = er.evaluate_radar(bars, quote={"t": price}, day_high=dh)
        line = {"end": end.strftime("%H:%M"), "n": i, "price": price, "radar": rd["state"], "radar_score": rd["early_momentum_score"],
                "radar_conf": rd["confidence"], "activity": None, "movement": None, "pattern": None, "pre": False, "late": False,
                "entry_eligible": False}
        mark("radar", rd["state"] in RADAR_HOT_STATES, line["end"])
        mark("radar_any", rd["state"] not in ("RADAR_NONE",) and not rd["handoff"], line["end"])
        if i >= cc_min_bars():
            ch = cc.evaluate_chart_context(bars, quote={"t": price}, day_high=dh, day_low=dl, minutes_since_open=minutes)
            mv = mp.evaluate_movement(bars, quote={"t": price}, day_high=dh, day_low=dl, chart=ch, minutes_since_open=minutes)
            eligible = (mp.movement_recommendation("ENTRY_READY", "ENTRY_READY", mv, ch) == "ENTRY_READY"
                        and ch["pattern"] in ("PULLBACK_READY", "EARLY_BREAKOUT", "VWAP_RECLAIM"))
            line.update({"activity": mv["activity_state"], "movement": mv["movement_potential_score"], "pattern": ch["pattern"],
                         "pre": mv["pre_breakout"], "late": mv["too_late"], "entry_eligible": eligible})
            mark("expanding", mv["activity_state"] == "EXPANDING", line["end"])
            mark("pre_breakout", mv["pre_breakout"], line["end"])
            mark("entry_ready", eligible, line["end"])
            mark("chase", ch["pattern"] in ("CHASE", "EXTENDED", "EXHAUSTION"), line["end"])
        timeline.append(line)
    return timeline, first


def cc_min_bars():
    return mp.MIN_BARS


def minutes_between(a, b):
    if not a or not b:
        return None
    ha, ma = map(int, a.split(":"))
    hb, mb = map(int, b.split(":"))
    return (hb * 60 + mb) - (ha * 60 + ma)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    warnings.filterwarnings("ignore")
    code = sys.argv[1]
    for d in sys.argv[2:]:
        day = datetime.date.fromisoformat(d)
        rows = load_day(code, day)
        if not rows:
            print(code, d, "5分足なし")
            continue
        timeline, first = replay(rows)
        print(f"\n=== {code} {d}  始値{rows[0]['open']:.0f} → 終値{rows[-1]['close']:.0f} ({(rows[-1]['close'] / rows[0]['open'] - 1) * 100:+.1f}%) / 5分足{len(rows)}本 ===")
        for ln in timeline[:12]:
            print(f"  {ln['end']} n={ln['n']} px={ln['price']:.0f} radar={ln['radar']}({ln['radar_score']},{ln['radar_conf']}) "
                  f"activity={ln['activity']} mv={ln['movement']} pattern={ln['pattern']}")
        print("  初検出時刻（足の終了時刻）:")
        for k, label in (("radar", "Radar（SURGE/EXPANDING/PRE_BREAKOUT）"), ("radar_any", "Radar（ACTIVE以上）"), ("expanding", "EXPANDING"),
                         ("pre_breakout", "PRE_BREAKOUT"), ("entry_ready", "ENTRY_READY相当（上限）"), ("chase", "CHASE系")):
            print(f"    {label}: {first.get(k, '—')}")
        for k in ("expanding", "pre_breakout", "entry_ready", "chase"):
            m = minutes_between(first.get("radar"), first.get(k))
            print(f"    Radar → {k}: {'—' if m is None else str(m) + '分'}")


if __name__ == "__main__":
    main()
