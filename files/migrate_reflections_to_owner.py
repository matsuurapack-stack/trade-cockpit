"""trade_reflections の `_shared` 行を owner の個人データへ移行する（2026-09-26 MU-Multi）。

背景：これまで trade_reflections は全員共有(_shared固定)だったが、反省本文・教訓・trade_idを含む
個人データのため本人専用へ変更した。配布前は実質owner一人で使っていたため、既存の共有行は
すべてownerの履歴として扱う。削除はしない（user_id列の付け替えのみ）。

使い方（files/ で実行）：
  python migrate_reflections_to_owner.py --dry-run          # 対象件数の表示のみ（DB変更なし）
  python migrate_reflections_to_owner.py --backup           # JSONバックアップのみ作成
  python migrate_reflections_to_owner.py --apply            # バックアップ作成→移行→結果確認
  python migrate_reflections_to_owner.py --apply --owner matsuura

--apply は必ず先にバックアップを書き出し、書き出せなければ中断する。
"""
import argparse
import datetime
import json
import os
import sys

import investment_db
import server

BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backups")


def count_by_user(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT user_id, count(*) FROM trade_reflections GROUP BY user_id ORDER BY user_id")
        return {r[0]: r[1] for r in cur.fetchall()}


def write_backup(conn):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    with conn.cursor(row_factory=investment_db.dict_row) as cur:
        cur.execute("SELECT * FROM trade_reflections ORDER BY id")
        rows = [investment_db._row_to_json(r) for r in cur.fetchall()]
    path = os.path.join(BACKUP_DIR, "trade_reflections_%s.json" % datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2, default=str)
    return path, len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--backup", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--owner", default=investment_db._LEGACY_OWNER)
    args = ap.parse_args()
    if not (args.dry_run or args.backup or args.apply):
        ap.error("--dry-run / --backup / --apply のいずれかを指定してください")
    pool = investment_db._get_pool(server.DATABASE_URL)
    if pool is None:
        print("DB未接続"); return 1
    with pool.connection() as conn:
        before = count_by_user(conn)
        shared_n = before.get(investment_db._SHARED_SCOPE, 0)
        print("移行前 user_id別件数:", before)
        print("移行対象(_shared)件数:", shared_n, "→ owner:", args.owner)
        if args.dry_run:
            print("[dry-run] DBは変更していません"); return 0
        path, n = write_backup(conn)
        print("バックアップ作成:", path, "(%d件)" % n)
        if args.backup:
            return 0
        if n and not os.path.exists(path):
            print("バックアップ失敗のため中断"); return 1
        with conn.cursor() as cur:
            cur.execute("UPDATE trade_reflections SET user_id = %s WHERE user_id = %s",
                        [args.owner, investment_db._SHARED_SCOPE])
            moved = cur.rowcount
        conn.commit()
        after = count_by_user(conn)
        print("移行件数:", moved)
        print("移行後 user_id別件数:", after)
        ok = after.get(investment_db._SHARED_SCOPE, 0) == 0 and after.get(args.owner, 0) == before.get(args.owner, 0) + shared_n
        print("検証:", "OK（_shared=0件、ownerへ全件移行、総数不変）" if ok else "NG")
        return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
