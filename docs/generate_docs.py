"""マニュアル生成ツール（2026-09-26 MU-Multi）。

1) docs/users/_template.md から manual_user1.md 〜 manual_user5.md を作る
   （利用者名が決まったら、_template.md 内の「利用者{{N}}さん」を実名へ置き換える手順は manual_admin.md 参照）
2) --html を付けると、docs 内の全 .md を印刷しやすい .html にも変換する
   （ブラウザで開いて「印刷 → PDFとして保存」でPDF版になります。追加ソフト不要）

使い方（プロジェクトルートで）：
  python docs/generate_docs.py            # 利用者マニュアル5人分を(再)生成
  python docs/generate_docs.py --html     # 併せて .html も作る
"""
import html
import os
import re
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
USERS = os.path.join(BASE, "users")


def gen_user_manuals(count=5):
    tpl = open(os.path.join(USERS, "_template.md"), encoding="utf-8").read()
    for n in range(1, count + 1):
        path = os.path.join(USERS, "manual_user%d.md" % n)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(tpl.replace("{{N}}", str(n)))
        print("作成:", path)


def md_to_html(md):
    """依存なしの簡易変換（見出し/箇条書き/番号/表/太字/区切り線）。印刷用途の最低限。"""
    out, in_ul, in_ol, in_table = [], False, False, False

    def close_lists():
        nonlocal in_ul, in_ol
        if in_ul:
            out.append("</ul>"); in_ul = False
        if in_ol:
            out.append("</ol>"); in_ol = False

    def inline(t):
        t = html.escape(t)
        t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
        return re.sub(r"`(.+?)`", r"<code>\1</code>", t)

    for raw in md.splitlines():
        line = raw.rstrip()
        if line.startswith("|") and line.endswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if all(re.fullmatch(r"-{3,}", c) for c in cells):
                continue
            close_lists()
            if not in_table:
                out.append("<table>"); in_table = True
                out.append("<tr>" + "".join("<th>%s</th>" % inline(c) for c in cells) + "</tr>")
            else:
                out.append("<tr>" + "".join("<td>%s</td>" % inline(c) for c in cells) + "</tr>")
            continue
        if in_table:
            out.append("</table>"); in_table = False
        m = re.match(r"^(#{1,4})\s+(.*)", line)
        if m:
            close_lists(); lvl = len(m.group(1)); out.append("<h%d>%s</h%d>" % (lvl, inline(m.group(2)), lvl)); continue
        if line.strip() == "---":
            close_lists(); out.append("<hr>"); continue
        m = re.match(r"^\s*[-*]\s+(.*)", line)
        if m:
            if in_ol:
                out.append("</ol>"); in_ol = False
            if not in_ul:
                out.append("<ul>"); in_ul = True
            out.append("<li>%s</li>" % inline(m.group(1))); continue
        m = re.match(r"^\s*\d+\.\s+(.*)", line)
        if m:
            if in_ul:
                out.append("</ul>"); in_ul = False
            if not in_ol:
                out.append("<ol>"); in_ol = True
            out.append("<li>%s</li>" % inline(m.group(1))); continue
        if not line.strip():
            close_lists(); continue
        if line.startswith("```"):
            continue
        close_lists()
        out.append("<p>%s</p>" % inline(line.strip()))
    close_lists()
    if in_table:
        out.append("</table>")
    return "\n".join(out)


PAGE = """<!doctype html><html lang="ja"><head><meta charset="utf-8"><title>{title}</title>
<style>body{{font-family:"Hiragino Sans","Yu Gothic",Meiryo,sans-serif;max-width:800px;margin:24px auto;padding:0 16px;line-height:1.8;color:#1c2330}}
h1{{font-size:24px}}h2{{font-size:20px;border-bottom:2px solid #dfe4ec;padding-bottom:4px;margin-top:28px}}h3{{font-size:17px}}
table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #c9d0db;padding:6px 10px;text-align:left;vertical-align:top}}th{{background:#f0f3f8}}
code{{background:#f0f3f8;padding:1px 5px;border-radius:4px}}@media print{{body{{margin:0}}h2{{page-break-after:avoid}}}}</style></head>
<body>{body}</body></html>"""


def build_html():
    for root, _dirs, files in os.walk(BASE):
        for fn in files:
            if fn.endswith(".md") and not fn.startswith("_"):
                src = os.path.join(root, fn)
                md = open(src, encoding="utf-8").read()
                title = (re.search(r"^#\s+(.*)", md, re.M) or [None, fn]).group(1) if re.search(r"^#\s+(.*)", md, re.M) else fn
                dst = src[:-3] + ".html"
                with open(dst, "w", encoding="utf-8") as f:
                    f.write(PAGE.format(title=html.escape(title), body=md_to_html(md)))
                print("作成:", dst)


if __name__ == "__main__":
    gen_user_manuals()
    if "--html" in sys.argv:
        build_html()
