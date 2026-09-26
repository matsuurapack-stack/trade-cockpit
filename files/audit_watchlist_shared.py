"""既存の共有(_shared)watchlistの棚卸し（読み取り専用・DBは一切変更しない）。2026-09-26 MU-Multi。

目的：これまで全員共有だった watchlist の各行が
  A. システム生成・共通分析用途（自動登録エンジンのタグ等）→ shared(system)のまま残す
  B. ownerが手動登録しただけの銘柄                        → owner個人watchlistへの移行「候補」
  C. 自動判別できないもの                                    → 勝手に移行せず一覧で報告
のどれかを機械的に仕分けし、結果を watchlist_audit_report.txt に保存する。
移行は行わない（このスクリプトは分類と報告のみ）。移行したい場合は結果を確認のうえ別途ご指示ください。

使い方（files/ で実行）：  python audit_watchlist_shared.py
"""
import json
import sys

import investment_db
import server
from psycopg.rows import dict_row

SYSTEM_SOURCE_HINTS = ("auto", "momentum", "discovery", "radar", "engine", "system", "scan", "hot")
MANUAL_SOURCE_HINTS = ("manual", "smart_import", "import", "migrate", "watch_stock", "master")


def classify(row):
    tags = row.get("auto_tags") or {}
    src = (row.get("source") or "").lower()
    if tags:
        return "A_SYSTEM", "auto_tags=%s" % ",".join(sorted(tags.keys()))[:60]
    if any(h in src for h in SYSTEM_SOURCE_HINTS) and not row.get("manual_registered"):
        return "A_SYSTEM", "source=%s" % src
    if row.get("manual_registered") or any(h in src for h in MANUAL_SOURCE_HINTS):
        return "B_OWNER_MANUAL_CANDIDATE", "manual_registered=%s source=%s" % (row.get("manual_registered"), src or "-")
    return "C_UNDETERMINED", "source=%s manual_registered=%s auto_tags=なし" % (src or "-", row.get("manual_registered"))


def main():
    pool = investment_db._get_pool(server.DATABASE_URL)
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT user_id, count(*) AS n FROM watchlist GROUP BY user_id ORDER BY user_id")
            by_user = {r["user_id"]: r["n"] for r in cur.fetchall()}
            cur.execute("SELECT * FROM watchlist WHERE user_id = %s ORDER BY code", [investment_db._SHARED_SCOPE])
            rows = cur.fetchall()
    groups = {"A_SYSTEM": [], "B_OWNER_MANUAL_CANDIDATE": [], "C_UNDETERMINED": []}
    for r in rows:
        g, why = classify(r)
        groups[g].append((r.get("code"), r.get("market"), r.get("name"), why))
    lines = ["watchlist 棚卸し（読み取り専用）", "user_id別件数: %s" % json.dumps(by_user, ensure_ascii=False),
             "_shared 合計: %d 件" % len(rows), ""]
    titles = {"A_SYSTEM": "A. システム生成・共通分析用途 → sharedのまま残す",
              "B_OWNER_MANUAL_CANDIDATE": "B. ownerが手動登録しただけ → owner個人watchlistへ移行候補（未移行）",
              "C_UNDETERMINED": "C. 自動判別できない → 勝手に移行せず一覧報告（要確認）"}
    for g in ("A_SYSTEM", "B_OWNER_MANUAL_CANDIDATE", "C_UNDETERMINED"):
        lines.append("%s（%d件）" % (titles[g], len(groups[g])))
        for code, market, name, why in groups[g]:
            lines.append("  %s/%s  %s  [%s]" % (code, market, name or "", why))
        lines.append("")
    text = "\n".join(lines)
    with open("watchlist_audit_report.txt", "w", encoding="utf-8") as f:
        f.write(text)
    print("A=%d B=%d C=%d 合計=%d  ユーザー別=%s" % (len(groups["A_SYSTEM"]), len(groups["B_OWNER_MANUAL_CANDIDATE"]),
                                                 len(groups["C_UNDETERMINED"]), len(rows), by_user))
    return 0


if __name__ == "__main__":
    sys.exit(main())
