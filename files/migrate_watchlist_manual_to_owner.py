"""共有(_shared)watchlistのうち「ownerが手動登録しただけ」の行をowner個人のwatchlistへ移す。
2026-09-26 MU-Multi。既定はdry-run（何も変更しない）。--apply の時だけ、JSONバックアップ作成→移行→検証。

対象（audit_watchlist_shared.py の B 分類と同じ厳密な条件）：
  user_id='_shared' かつ manual_registered=true かつ auto_tags が空 かつ source が自動登録系でない
除外（勝手に移さない）：auto_tags を持つ行／source が自動登録系の行／ownerに同じ(code,market)が既にある行。
削除はしない（user_idの付け替えのみ）。

使い方（files/ で実行）：
  python migrate_watchlist_manual_to_owner.py --owner matsuura            # dry-run（件数・内訳の表示のみ）
  python migrate_watchlist_manual_to_owner.py --owner matsuura --apply    # バックアップ→移行→検証
"""
import argparse
import datetime
import json
import os
import sys

import investment_db
import server
from psycopg.rows import dict_row

BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backups")
AUTO_HINTS = ("auto", "momentum", "discovery", "radar", "engine", "system", "scan", "hot")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--owner", required=True, help="移行先のユーザー名（ownerアカウント）")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    db = server.DATABASE_URL
    pool = investment_db._get_pool(db)
    with pool.connection() as conn:
        if not conn.execute("SELECT 1 FROM app_users WHERE username = %s", [args.owner]).fetchone():
            print("移行先ユーザーがapp_usersに存在しません:", args.owner); return 2
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM watchlist WHERE user_id = %s ORDER BY code", [investment_db._SHARED_SCOPE])
            shared = cur.fetchall()
            cur.execute("SELECT code, market FROM watchlist WHERE user_id = %s", [args.owner])
            owner_keys = {(r["code"], r["market"]) for r in cur.fetchall()}
        movable, skipped_auto, skipped_dup = [], [], []
        for r in shared:
            src = (r.get("source") or "").lower()
            if r.get("auto_tags") or any(h in src for h in AUTO_HINTS):
                skipped_auto.append(r["id"]); continue
            if not r.get("manual_registered"):
                skipped_auto.append(r["id"]); continue
            if (r["code"], r["market"]) in owner_keys:
                skipped_dup.append(r["id"]); continue
            movable.append(r["id"])
        print("_shared 合計:", len(shared), "／移行対象:", len(movable), "／自動系のため除外:", len(skipped_auto),
              "／ownerに既にあるため除外:", len(skipped_dup))
        if not args.apply:
            print("[dry-run] DBは変更していません。--apply で実行します。"); return 0
        os.makedirs(BACKUP_DIR, exist_ok=True)
        path = os.path.join(BACKUP_DIR, "watchlist_shared_%s.json" % datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
        with open(path, "w", encoding="utf-8") as f:
            json.dump([investment_db._row_to_json(r) for r in shared], f, ensure_ascii=False, indent=2, default=str)
        print("バックアップ作成:", path, "(%d件)" % len(shared))
        if not movable:
            print("移行対象なし"); return 0
        n = conn.execute("UPDATE watchlist SET user_id = %s, updated_at = now() WHERE id = ANY(%s) AND user_id = %s",
                         [args.owner, movable, investment_db._SHARED_SCOPE]).rowcount
        conn.commit()
        left = conn.execute("SELECT count(*) FROM watchlist WHERE user_id = %s", [investment_db._SHARED_SCOPE]).fetchone()[0]
        mine = conn.execute("SELECT count(*) FROM watchlist WHERE user_id = %s", [args.owner]).fetchone()[0]
        print("移行件数:", n, "／_shared残り:", left, "／%s合計:" % args.owner, mine)
        return 0 if n == len(movable) else 2


if __name__ == "__main__":
    sys.exit(main())
