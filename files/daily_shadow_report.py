# Phase C shadow運用の日次レポート（読み取り専用。判定・サーバーには触れない）。
#   python daily_shadow_report.py [YYYY-MM-DD] [サーバーのstderrログパス]
# 事後価格(+5/+15/+30分)は集計専用で、判定ロジックへ戻さない。

import datetime
import re
import sys

import chart_signal_log as sl

JST = datetime.timezone(datetime.timedelta(hours=9))
ENTRY = sl.ENTRY_STATES


def _r(x):
    return "-" if x is None else f"{x:+.2f}%"


def _line(r):
    ft = r.get("features") or {}
    reasons = "; ".join((r.get("reasons") or [])[:3])
    pens = "; ".join((r.get("penalties") or [])[:2])
    return (f"  {str(r['logged_at'])[11:16]}(UTC) {r['code']} ¥{r['current_price']:.0f} ENTRY点={r.get('entry_timing')} "
            f"強さ={r.get('stock_strength')} {r['chart_pattern']} {r['legacy_entry_state']}→{r['chart_entry_state']} "
            f"+5/+15/+30={_r(r.get('ret_5m'))}/{_r(r.get('ret_15m'))}/{_r(r.get('ret_30m'))} "
            f"上ヒゲ={r.get('upper_wick_ratio')} VWAP乖離={r.get('vwap_distance')} | {reasons} | {pens}")


def _avg(rs, h):
    v = [r[f"ret_{h}m"] for r in rs if r.get(f"ret_{h}m") is not None]
    return (round(sum(v) / len(v), 3), len(v)) if v else (None, 0)


def main():
    sys.stdout.reconfigure(encoding="utf-8")   # Windowsのcp932で¥等が出力できず落ちるのを防ぐ
    day = sys.argv[1] if len(sys.argv) > 1 else datetime.datetime.now(JST).date().isoformat()
    err_path = sys.argv[2] if len(sys.argv) > 2 else None
    import server
    import investment_db as db
    rows = db.list_chart_signals(server.DATABASE_URL, "matsuura", day, None, 20000)
    day_sum = sl.summarize_day(rows)
    rr = sl._with_ret(rows)
    print(f"=== Phase C shadow 日次レポート {day} ===")
    print(f"shadowログ総行数: {len(rows)}（目標 1,000〜2,000）/ 銘柄数 {len({r['code'] for r in rows})}")

    stop = [r for r in rr if r["legacy_entry_state"] in ENTRY and r["chart_entry_state"] not in ENTRY]
    promo = [r for r in rr if r["legacy_entry_state"] not in ENTRY and r["chart_entry_state"] in ENTRY]
    diff = [r for r in rr if r["legacy_entry_state"] != r["chart_entry_state"]]
    print(f"legacy≠chart 差分: {len(diff)}行 / Phase CがENTRYを止めた: {len(stop)}行 / ENTRYへ昇格させた: {len(promo)}行")
    for title, rs in (("legacy ENTRY_READY → chart WAIT/WATCH（止めた）", stop), ("legacy WAIT → chart ENTRY_READY（昇格）", promo)):
        print(f"\n[{title}] {len(rs)}行  平均リターン " + " / ".join(f"+{h}m {_avg(rs, h)[0]}%(n={_avg(rs, h)[1]})" for h in (5, 15, 30)))
        seen = set()
        for r in rs:
            k = (r["code"], r["chart_pattern"], r["chart_entry_state"])
            if k in seen:
                continue
            seen.add(k)
            print(_line(r))

    print("\n--- 件数 ---")
    for k in ("chase_stop_count", "failed_breakout_count", "pullback_ready_count", "entry_ready_count"):
        print(f"{k}: {day_sum[k]}")
    print("ENTRY_READY +15m/+30m プラス率:", day_sum["entry_ready_plus_rate_15m"], day_sum["entry_ready_plus_rate_30m"])
    print("CHASE見送り成功率/見逃し率:", day_sum["chase_stop_success_rate"], day_sum["chase_miss_rate"],
          "| PULLBACK成功率:", day_sum["pullback_success_rate"], "| FAILED_BREAKOUT誤判定率:", day_sum["failed_breakout_misjudge_rate"])

    print("\n--- 厳密なtransition_type ---")
    ts = day_sum["transition_types"]
    for t in ("ENTRY_READY_TO_CHASE", "ENTRY_READY_TO_FAILED_BREAKOUT", "CHASE_TO_PULLBACK_READY",
              "PULLBACK_READY_TO_ENTRY_READY", "FAILED_BREAKOUT_TO_RECOVERY"):
        d = ts.get(t)
        print(f"{t}: {d['count'] if d else 0}" + (f" origins={d['origins']} 平均+15m={d['avg_ret_15m']} +30m={d['avg_ret_30m']}" if d else ""))
        for r in rr:
            if t in (r.get("transition_type") or "").split(","):
                print(_line(r))

    mv = day_sum["movement"]
    print("\n--- Phase D shadow：既存の推奨 vs 値幅を見た推奨（movement-aware）---")
    print("活動状態の件数:", mv["activity_states"], "/ PRE_BREAKOUT:", mv["pre_breakout_count"], "/ TOO_LATE:", mv["too_late_count"])
    for name, g in mv["comparison"].items():
        print(f"{name}: n={g['n']} +5m={g['avg_ret_5m']} +15m={g['avg_ret_15m']}(n={g['n_15m']}) +30m={g['avg_ret_30m']} "
              f"+15mプラス率={g['plus_rate_15m']} MFE={g['avg_mfe_30m']} MAE={g['avg_mae_30m']}")
    print("\nモメンタムENTRY（shadow）:", len(mv["momentum_entries"]), "件 / 逆指値評価:", mv["stop_evaluation"])
    for m in mv["momentum_entries"]:
        print(f"  {str(m['at'])[11:16]}(UTC) {m['code']} ENTRY {m['entry_price']} STOP {m['stop']}(-{m['stop_distance_pct']}%) RR={m['rr']} "
              f"+5/+15/+30={_r(m['ret_5m'])}/{_r(m['ret_15m'])}/{_r(m['ret_30m'])} MFE={m['mfe_30m']} MAE={m['mae_30m']} "
              f"評価={m['stop_evaluation']} | {m['entry_reason']}")
    print("\nマイルストーン（いつ EXPANDING / PRE_BREAKOUT / EARLY_BREAKOUT / CHASE になったか, UTC）:")
    for t in mv["milestone_timeline"][:20]:
        print("  " + t["code"] + " " + " ".join(f"{k.replace('first_', '').replace('_at', '')}={str(t[k])[11:16]}"
                                                for k, _ in sl.MILESTONES if t.get(k)))

    rd = mv["radar"]
    print("\n--- Phase D.1 Early Momentum Radar（shadow・買い判定ではない）---")
    for name, g in rd["states"].items():
        print(f"{name}: n={g['n']} +5m={g['avg_ret_5m']} +15m={g['avg_ret_15m']}(n={g['n_15m']}) +30m={g['avg_ret_30m']} MFE={g['avg_mfe_30m']} MAE={g['avg_mae_30m']}")
    print("Radar先行時間（分。Radar初検出→各イベント）:")
    for t in rd["lead_times"]:
        print("  ", t)
    rl = mv["rolling"]
    print("\n--- Phase D.2 Rolling Momentum Radar（shadow・警戒レーダー。買い判定ではない）---")
    print("false positiveの定義:", rl["false_positive_definition"])
    for name, g in rl["states"].items():
        print(f"{name}: n={g['n']} +5m={g['avg_ret_5m']} +15m={g['avg_ret_15m']}(n={g['n_15m']}) +30m={g['avg_ret_30m']} MFE={g['avg_mfe_30m']} "
              f"MAE={g['avg_mae_30m']} 誤検出={g['false_positive']}/{g['judged']}（率 {g['false_positive_rate']}）")
    print("Rolling先行時間（分。Rolling初検出→各イベント）:")
    for t in rl["lead_times"]:
        print("  ", t)
    print("検出イベント（検出後の+5/+15/+30分・MFE/MAE）:")
    for e in rl["events"][:40]:
        print(f"  {str(e['at'])[11:16]}(UTC) {e['code']} {e['state']}({e['score']}) ¥{e['price']} +5/+15/+30={_r(e['ret_5m'])}/{_r(e['ret_15m'])}/{_r(e['ret_30m'])} "
              f"MFE={e['mfe_30m']} MAE={e['mae_30m']} 誤検出={e['false_positive']} 確認不足={e['confirmations_failed']}")
    ep = mv["radar_episodes"]
    print("\n--- Rolling Radar エピソード（Radar後の推移・寿命。観測専用）---")
    print(f"Radar総件数(エピソード) {ep['total']} / 状態別 {ep['by_start_state']} / hot状態で開始 {ep['hot_started']}")
    print(f"Radar→EXPANDING 平均先行 {ep['lead_to_expanding']} / Radar→PULLBACK_READYまたはPRE_BREAKOUT {ep['lead_to_pullback_or_pre']}")
    print(f"Radar→ENTRY_READY {ep['radar_to_entry_ready_count']}件 平均 {ep['lead_to_entry_ready']} ／ Radar→CHASE {ep['radar_to_chase_count']}件 平均 {ep['lead_to_chase']}")
    print(f"『Radar後に待って押し目ENTRYできた』件数: {ep['waited_then_entry_count']}")
    for e in ep["waited_then_entry"]:
        print("   ", e["code"], e["startedAt"][11:16], e["startState"], e["lead_minutes"])
    print("寿命:", ep["lifespan"], "/ radar age(分):", ep["age_minutes"])
    print("発生時の特徴量の平均（誤検出 vs 良いRadar）:")
    print("   誤検出:", ep["snapshot_compare"]["false_positive"])
    print("   良い  :", ep["snapshot_compare"]["good"])
    try:
        import datetime as _dt
        since = _dt.datetime.fromisoformat(day + "T00:00:00+09:00")
        hist = db.list_dynamic_watch_history(server.DATABASE_URL, "matsuura", since)
        by_src = {}
        for h in hist:
            by_src[h["source"]] = by_src.get(h["source"], 0) + 1
        print("dynamic watch 追加件数（理由別）:", by_src, "/ 合計", len(hist))
    except Exception as e:
        print("dynamic watch履歴を取得できません:", e)
    try:
        import datetime as _dt2
        md_rows = db.list_market_discovery(server.DATABASE_URL, "matsuura", day)
        print("\n--- Market Discovery（登録外から発見。shadow）---")
        by_status, by_src = {}, {}
        for r in md_rows:
            by_status[r["status"]] = by_status.get(r["status"], 0) + 1
            by_src[r["source"]] = by_src.get(r["source"], 0) + 1
        print(f"発見件数 {len(md_rows)} / 状態別 {by_status} / source別 {by_src}")
        def _m(a, b):
            ta, tb = sl._parse_dt(a), sl._parse_dt(b)
            return None if not (ta and tb) else round((tb - ta).total_seconds() / 60.0, 1)
        for label, key in (("発見→realtime初回", "rt_first_at"), ("発見→昇格", "promoted_at"), ("発見→hot", "hot_at"), ("発見→Radar", "radar_at"),
                           ("発見→EXPANDING", "expanding_at"), ("発見→ENTRY相当(上限)", "entry_at"), ("発見→CHASE", "chase_at")):
            v = [_m(r["discovered_at"], r.get(key)) for r in md_rows if r.get(key)]
            v = [x for x in v if x is not None]
            print(f"  {label}: n={len(v)} 平均 {round(sum(v) / len(v), 1) if v else None}分")
        for r in md_rows:
            if r.get("promoted_at"):
                print("   ", r["code"], r["status"], "発見", str(r["discovered_at"])[11:16], "昇格", str(r["promoted_at"])[11:16],
                      "Radar", str(r.get("radar_at") or "-")[11:16], "EXPANDING", str(r.get("expanding_at") or "-")[11:16],
                      "ENTRY", str(r.get("entry_at") or "-")[11:16], "CHASE", str(r.get("chase_at") or "-")[11:16], r.get("discovery_reason"))
    except Exception as e:
        print("Market Discovery集計を取得できません:", e)
    ct = day_sum["catalyst"]
    print("\n--- Phase F Catalyst Confirmation（shadow。材料×チャート）---")
    print("定義:", ct["definitions"])
    print("状態別:", ct["states"], "/ 決算:", ct["earnings_states"], "/ 規制:", ct["margin_states"])
    def _g(name, g):
        print(f"  {name}: n={g['n']} +5m={g['avg_ret_5m']} +15m={g['avg_ret_15m']}(n={g['n_15m']}) +30m={g['avg_ret_30m']} "
              f"+15mプラス率={g['plus_rate_15m']} MFE={g['avg_mfe_30m']} MAE={g['avg_mae_30m']}")
    print("組み合わせ別:")
    for name, g in ct["combinations"].items():
        _g(name, g)
    print("フラグ別:")
    for name, g in ct["flags"].items():
        _g(name, g)
    print("verdict別:")
    for name, g in ct["verdicts"].items():
        _g(name, g)
    try:
        snaps = db.list_catalyst_snapshots(server.DATABASE_URL, "matsuura", day)
        by_trig, by_type = {}, {}
        for r in snaps:
            by_trig[r["trigger"]] = by_trig.get(r["trigger"], 0) + 1
            by_type[r["catalyst_type"]] = by_type.get(r["catalyst_type"], 0) + 1
        dur = sorted(r["duration_ms"] for r in snaps if r.get("duration_ms") is not None)
        print(f"Catalyst Snapshot {len(snaps)}件 / 発火理由別 {by_trig} / 種別 {by_type} / 調査時間 中央値 {dur[len(dur) // 2] if dur else None}ms 最大 {dur[-1] if dur else None}ms")
        for r in snaps[:30]:
            print("   ", str(r["detected_at"])[11:16], r["code"], r["trigger"], r["state"], r["catalyst_type"], r["direction"], r["confidence"],
                  "score", r["catalyst_score"], "決算", r["earnings_state"], "規制", r["margin_restriction"], "材料不明" if r["unexplained_move"] else "")
    except Exception as e:
        print("Catalyst Snapshotを取得できません:", e)
    sq = mv["stop_quality"]
    print("推奨逆指値の品質（movement ENTRY_READY全イベント）:", sq)

    print("\n--- outcome delay（実取得時刻 − 目標時刻, 秒）---")
    for h, q in day_sum["outcome_quality"].items():
        print(h, q)

    if err_path:
        try:
            txt = open(err_path, encoding="utf-8", errors="replace").read()
            print(f"\nサーバー例外(Traceback)件数: {txt.count('Traceback')} / 立花関連エラー行: "
                  f"{len(re.findall(r'(?i)tachibana.*(error|失敗|例外)|立花.*(失敗|例外|エラー)', txt))}")
        except OSError as e:
            print("stderrログを読めません:", e)


if __name__ == "__main__":
    main()
