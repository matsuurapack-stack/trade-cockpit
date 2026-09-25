# dynamic_watchlist（Phase D・shadow）：手動の監視銘柄（watchlist / manual_watchlist）とは別の
# 「動いている銘柄」だけのオーバーレイ。純粋ロジック（DB・ネットワーク非依存）。
#
#   ・自動追加：movement_potential >= ADD_SCORE、または EXPANDING / PRE_BREAKOUT / HOT
#   ・自動削除：LOW_ACTIVITY / FADING / VWAP下で回復なし / モメンタム減衰 が REMOVE_GRACE_SEC 継続
#   ・上限：active MAX_ACTIVE（30〜50）、hot pool MAX_HOT（10〜20）。超えたら弱い順（非手動から）に外す
#   ・手動登録銘柄（is_manual）は、dynamic側から外れても manual watchlist からは一切削除されない。
#     ここは watchlist テーブルには触れず、dynamic_watchlist（オーバーレイ）だけを更新する。
#     手動銘柄もオーバーレイ上では上限・自動削除の対象（上限が効かず全銘柄がhot扱いになるのを防ぐ）。
#
# 現状の候補源は「全登録銘柄のうちスキャンでスコアリングされた銘柄」。全市場の値上がり率上位などの
# 外部ランキングは取得経路が無いため未対応（データ源を追加していない）。

import movement_potential as mp

MAX_ACTIVE = 50
MAX_HOT = 20
REMOVE_GRACE_SEC = 20 * 60         # 弱い状態がこの秒数続いたら外す（一時的な休みでは外さない）
ADD_SCORE = mp.ADD_SCORE
HOT_SCORE = mp.HOT_SCORE
HOT_RECENT_ACTIVITY = 60


def is_hot(c):
    return bool(c.get("radar_hot") or c.get("rolling_hot") or c.get("pre_breakout") or c.get("activity_state") == "EXPANDING"
                or ((c.get("movement") or 0) >= HOT_SCORE and (c.get("recent_activity") or 0) >= HOT_RECENT_ACTIVITY))


def add_reason(c):
    if c.get("rolling_hot"):
        return f"ROLLING:{c.get('rolling_state')}"           # Rolling Radar（場中の警戒レーダー）：hot poolへ即追加
    if c.get("radar_hot"):
        return f"RADAR:{c.get('radar_state') or 'SCORE'}"      # 通常のMovement Scoreがまだ低くても初動として即追加
    if c.get("pre_breakout"):
        return "PRE_BREAKOUT"
    if c.get("activity_state") == "EXPANDING":
        return "EXPANDING"
    if is_hot(c):
        return "HOT"
    if (c.get("movement") or 0) >= ADD_SCORE:
        return f"MOVEMENT>={ADD_SCORE}"
    if c.get("rolling_watch"):
        return f"ROLLING:{c.get('rolling_state')}"           # RADAR_WEAK / ROLLING_EXPANDING：通常のdynamic watchまで（hotにはしない）
    return None


def weak_reason(c):
    """外す候補にする弱い状態。回復（movementが再び高い・EXPANDING・PRE_BREAKOUT）していれば弱くない。"""
    if (c.get("radar_hot") or c.get("rolling_hot") or c.get("rolling_watch") or c.get("pre_breakout")
            or c.get("activity_state") == "EXPANDING" or (c.get("movement") or 0) >= ADD_SCORE):
        return None
    if c.get("activity_state") == "LOW_ACTIVITY":
        return "LOW_ACTIVITY"
    if c.get("activity_state") == "FADING":
        return "FADING"
    if c.get("momentum_state") == "MOMENTUM_DECAY":
        return "MOMENTUM_DECAY"
    if c.get("above_vwap") is False and (c.get("movement") or 0) < 50:
        return "BELOW_VWAP_NO_RECOVERY"
    return None


def update_dynamic_watch(existing, cands, now, max_active=MAX_ACTIVE, max_hot=MAX_HOT, grace_sec=REMOVE_GRACE_SEC):
    """existing: {code: {"added_at","weak_since","pool","is_manual",...}}（現在ACTIVEの行）。
    cands: 今回評価した銘柄 [{"code","name","movement","recent_activity","activity_state","pre_breakout",
                              "above_vwap","momentum_state","is_manual","rank"}]。
    戻り値: {"adds":[...], "removes":[...], "state":{code:{...}}, "hot":[code...]}（stateが新しいACTIVE集合）。"""
    by_code = {c["code"]: c for c in cands}
    state = {k: dict(v) for k, v in existing.items()}
    adds, removes = [], []
    for code, st in list(state.items()):
        c = by_code.get(code)
        if c is None:
            continue                       # 今回評価されなかった銘柄は状態を変えない
        st["last_seen_at"] = now
        st["is_manual"] = bool(c.get("is_manual", st.get("is_manual")))
        w = weak_reason(c)
        if w is None:
            st["weak_since"], st["weak_reason"] = None, None
            continue
        st["weak_reason"] = w
        if st.get("weak_since") is None:
            st["weak_since"] = now
        if (now - st["weak_since"]).total_seconds() >= grace_sec:
            removes.append({"code": code, "reason": w})
            del state[code]
    for c in cands:
        code = c["code"]
        if code in state:
            continue
        reason = add_reason(c)
        if reason:
            row = {"code": code, "name": c.get("name"), "added_at": now, "last_seen_at": now, "weak_since": None,
                   "is_manual": bool(c.get("is_manual")), "source": reason, "pool": "ACTIVE",
                   "movement_at_add": c.get("movement")}
            state[code] = row
            adds.append(row)
    # 上限：activeが多すぎる場合は、rankの低い順に外す（オーバーレイからのみ。手動のwatchlistは無変更）
    if len(state) > max_active:
        order = sorted(state, key=lambda k: (by_code.get(k) or {}).get("rank") or -1e9)
        for code in order:
            if len(state) <= max_active:
                break
            removes.append({"code": code, "reason": "CAP"})
            del state[code]
        adds = [a for a in adds if a["code"] in state]
    # hot pool：activeのうちhotで、rankの高い順に上限まで
    hot_sorted = sorted((k for k in state if k in by_code and is_hot(by_code[k])),
                        key=lambda k: -((by_code[k].get("rank")) or 0))
    hot = hot_sorted[:max_hot]
    for k, st in state.items():
        st["pool"] = "HOT" if k in hot else "ACTIVE"
    return {"adds": adds, "removes": removes, "state": state, "hot": hot}
