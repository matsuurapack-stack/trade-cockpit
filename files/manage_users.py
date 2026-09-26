"""利用者アカウント管理ツール（管理者専用・サーバーPC上で実行）。2026-09-26 MU-Multi。

パスワードはハッシュ化してDB(app_users)に保存する。平文はどこにも保存しない（画面に表示するのは
--generate で作った初期パスワードを「作成した瞬間の1回だけ」）。ソース・Gitにパスワードは書かない。
このツールに、他の利用者のポジション・トレード分析・振り返りを閲覧する機能は無い（アカウント管理のみ）。

使い方（files/ で実行。DBはsecrets.jsonのdatabase_url / 環境変数DATABASE_URLを使用）：
  python manage_users.py list
  python manage_users.py create user1                 # パスワードを対話入力（画面に出ません）
  python manage_users.py create user1 --generate      # ランダムな初期パスワードを1回だけ表示
  python manage_users.py create owner2 --role owner
  python manage_users.py set-password user1           # パスワード変更（ログイン中の端末は全てログアウト）
  python manage_users.py set-password user1 --generate
  python manage_users.py disable user1                # 利用停止（即時ログアウト、データは残る）
  python manage_users.py enable user1
  python manage_users.py migrate-legacy               # 旧secrets.jsonのusers(平文)をハッシュ化して取り込む
  python manage_users.py sessions user1               # ログイン中の端末数を確認
"""
import argparse
import getpass
import secrets
import string
import sys

import auth_core
import investment_db
import server


def _generate_password(n=12):
    alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 紛らわしい文字(0/O,1/l/I)を除く
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(n))
        if any(c.islower() for c in pw) and any(c.isupper() for c in pw) and any(c.isdigit() for c in pw):
            return pw


def _read_password(args):
    if getattr(args, "generate", False):
        return _generate_password(), True
    pw = getpass.getpass("パスワード（8文字以上、入力は表示されません）: ")
    if pw != getpass.getpass("もう一度: "):
        print("パスワードが一致しません。中止しました。")
        sys.exit(2)
    return pw, False


def main():
    ap = argparse.ArgumentParser(description="利用者アカウント管理")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    c = sub.add_parser("create"); c.add_argument("username"); c.add_argument("--role", default="user", choices=["user", "owner"])
    c.add_argument("--generate", action="store_true"); c.add_argument("--display-name")
    sp = sub.add_parser("set-password"); sp.add_argument("username"); sp.add_argument("--generate", action="store_true")
    for name in ("disable", "enable"):
        sub.add_parser(name).add_argument("username")
    sub.add_parser("migrate-legacy")
    ss = sub.add_parser("sessions"); ss.add_argument("username")
    args = ap.parse_args()

    db = server.DATABASE_URL
    if not db or investment_db is None:
        print("DBに接続できません（secrets.jsonのdatabase_urlを確認してください）"); return 1
    investment_db.init_schema(db)

    if args.cmd == "list":
        rows = investment_db.auth_list_users(db)
        if not rows:
            print("アカウントはまだありません。")
        for r in rows:
            print("%-16s role=%-5s %s  最終ログイン=%s" % (r["username"], r["role"], "有効" if r["enabled"] else "停止中",
                                                          r.get("last_login_at") or "-"))
        return 0
    if args.cmd == "migrate-legacy":
        migrated = server.migrate_legacy_users_to_db(db)
        print("移行したユーザー:", ", ".join(migrated) if migrated else "（新規なし）")
        if server.USERS:
            print("★ 移行後は secrets.json の \"users\"（平文パスワード）を必ず削除してください。")
        return 0
    if args.cmd == "create":
        ok, why = auth_core.validate_username(args.username)
        if not ok:
            print(why); return 2
        pw, generated = _read_password(args)
        ok, why = auth_core.validate_password_strength(pw)
        if not ok:
            print(why); return 2
        created = investment_db.auth_upsert_user(db, args.username, auth_core.hash_password(pw), role=args.role,
                                                 display_name=args.display_name, create_only=True)
        if not created:
            print("そのユーザー名は既にあります（パスワード変更は set-password を使ってください）"); return 2
        print("作成しました:", args.username)
        if generated:
            print("初期パスワード（この1回だけ表示。安全な方法で本人に渡してください）:", pw)
        return 0
    if args.cmd == "set-password":
        if not investment_db.auth_get_user(db, args.username):
            print("そのユーザーは存在しません"); return 2
        pw, generated = _read_password(args)
        ok, why = auth_core.validate_password_strength(pw)
        if not ok:
            print(why); return 2
        investment_db.auth_upsert_user(db, args.username, auth_core.hash_password(pw))
        print("パスワードを変更し、全端末をログアウトさせました:", args.username)
        if generated:
            print("新しいパスワード（この1回だけ表示）:", pw)
        return 0
    if args.cmd in ("disable", "enable"):
        found = investment_db.auth_set_enabled(db, args.username, args.cmd == "enable")
        print(("停止" if args.cmd == "disable" else "再開"), "しました:" if found else "対象が見つかりません:", args.username)
        return 0 if found else 2
    if args.cmd == "sessions":
        pool = investment_db._get_pool(db)
        with pool.connection() as conn:
            n = conn.execute("SELECT count(*) FROM app_sessions WHERE username=%s AND expires_at>now()", [args.username]).fetchone()[0]
        print("%s のログイン中の端末数: %d" % (args.username, n))
        return 0


if __name__ == "__main__":
    sys.exit(main())
