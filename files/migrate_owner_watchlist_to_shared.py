"""ownerの個人watchlistを「全員が最初から見る共通(system/shared)監視銘柄」にする。2026-09-26 MU-Multi(7人化)。

方針（個人メモを共有に漏らさない・削除しない）：
  - 個人メモ列（note / added_reason / tags / priority）が空の行 → user_id を '_shared' へ付け替え（移動。削除ではない）。
  - 個人メモ列のどれかが入っている行 → メモ列を除いた複製を '_shared' に作り、owner側の個人行はそのまま残す
    （ownerには個人行が優先して見えるため、ownerは今までと同じ内容が見える）。
  - 共有側に同じ(code, market)が既にある行は触らない（重複は報告のみ）。
  - 個人watchlistの仕組みは残す（今後の手動追加は本人だけに見える）。共通銘柄は利用者が削除しても全体から消えない。

使い方（files/ で実行）：
  python migrate_owner_watchlist_to_shared.py --owner matsuura            # dry-run（何も変更しない）
  python migrate_owner_watchlist_to_shared.py --owner matsuura --apply    # バックアップ→移行→検証
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
MEMO_COLS = ("note", "added_reason", "tags", "priority")
COPY_EXCLUDE = {"id", "user_id", "added_at", "updated_at"}


def has_memo(row):
    for c in MEMO_COLS:
        v = row.get(c)
        if v not in (None, "", [], {}, "[]", "{}", "null"):
            return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--owner", required=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    db = server.DATABASE_URL
    pool = investment_db._get_pool(db)
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM watchlist WHERE user_id = %s ORDER BY code", [args.owner])
            mine = cur.fetchall()
            cur.execute("SELECT code, market FROM watchlist WHERE user_id = %s", [investment_db._SHARED_SCOPE])
            shared_keys = {(r["code"], r["market"]) for r in cur.fetchall()}
        move, copy, dup = [], [], []
        for r in mine:
            if (r["code"], r["market"]) in shared_keys:
                dup.append(r["id"]); continue
            (copy if has_memo(r) else move).append(r)
        print("owner個人watchlist:", len(mine), "件")
        print("  → sharedへ移動(削除ではない):", len(move), "件")
        print("  → メモ列を除いた複製をsharedへ(owner個人行は残す):", len(copy), "件")
        print("  → sharedに既にあり重複(触らない):", len(dup), "件")
        print("  削除:", 0, "件")
        print("  移行後の見え方: owner=%d件(個人行が優先)／他ユーザー=共通%d件" % (len(mine), len(move) + len(copy) + len(shared_keys)))
        if not args.apply:
            print("[dry-run] DBは変更していません。--apply で実行します。"); return 0
        os.makedirs(BACKUP_DIR, exist_ok=True)
        path = os.path.join(BACKUP_DIR, "watchlist_owner_before_share_%s.json" % datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
        with open(path, "w", encoding="utf-8") as f:
            json.dump([investment_db._row_to_json(r) for r in mine], f, ensure_ascii=False, indent=2, default=str)
        print("バックアップ作成:", path, "(%d件)" % len(mine))
        moved = conn.execute("UPDATE watchlist SET user_id = %s, updated_at = now() WHERE id = ANY(%s) AND user_id = %s",
                             [investment_db._SHARED_SCOPE, [r["id"] for r in move], args.owner]).rowcount if move else 0
        copied = 0
        for r in copy:
            cols = [c for c in r.keys() if c not in COPY_EXCLUDE and c not in MEMO_COLS]
            vals = [r[c] if not isinstance(r[c], (dict, list)) else json.dumps(r[c], ensure_ascii=False) for c in cols]
            ph = ["%s::jsonb" if isinstance(r[c], (dict, list)) else "%s" for c in cols]
            copied += conn.execute(
                f"INSERT INTO watchlist (user_id, {', '.join(cols)}) VALUES (%s, {', '.join(ph)}) "
                f"ON CONFLICT (user_id, code, market) DO NOTHING", [investment_db._SHARED_SCOPE] + vals).rowcount
        conn.commit()
        sh = conn.execute("SELECT count(*) FROM watchlist WHERE user_id = %s", [investment_db._SHARED_SCOPE]).fetchone()[0]
        ow = conn.execute("SELECT count(*) FROM watchlist WHERE user_id = %s", [args.owner]).fetchone()[0]
        leaked = conn.execute("SELECT count(*) FROM watchlist WHERE user_id = %s AND (coalesce(note,'')<>'' OR coalesce(added_reason,'')<>'')",
                              [investment_db._SHARED_SCOPE]).fetchone()[0]
        print("移動:", moved, "／複製:", copied, "／shared合計:", sh, "／owner個人行:", ow, "／shared内の個人メモ:", leaked)
        ok = moved == len(move) and copied == len(copy) and leaked == 0
        print("検証:", "OK" if ok else "NG")
        return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
