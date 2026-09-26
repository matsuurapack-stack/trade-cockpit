"""利用者マニュアル（PowerPoint→PDF）生成ツール。2026-09-26 MU-Multi（7人構成：user1〜user6を配布）。

PowerPoint（COM）で、user1〜user6の6人それぞれ専用の manual_userN.pptx / .pdf を作る。
各人専用のURL・ユーザー名・初期パスワードを1人分だけ記載する（他人のパスワードは載せない）。
初期パスワードはこのスクリプトには書かない：files/backups/initial_passwords*.txt（管理者専用・gitignore済み）から
実行時にメモリ上でだけ読む。生成物 docs/distribution/ は機密扱い（gitignore済み）。

使い方（プロジェクトルートで）：
  python docs/build_pptx_manuals.py                       # 既定URLで6人分を作成
  python docs/build_pptx_manuals.py --url https://xxxx/   # URLを指定（クラウド公開時など）
"""
import argparse
import glob
import os
import sys

import win32com.client

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "docs", "distribution")
ASSETS = os.path.join(ROOT, "docs", "assets")
PW_GLOB = os.path.join(ROOT, "files", "backups", "initial_passwords*.txt")
DEFAULT_URL = "http://192.168.188.166:8765/"


def rgb(r, g, b):
    return r + g * 256 + b * 65536


INK, GRAY = rgb(28, 35, 48), rgb(110, 120, 135)
BLUE, BLUE_BG = rgb(31, 102, 224), rgb(232, 241, 255)
GREEN, GREEN_BG = rgb(24, 148, 78), rgb(228, 246, 236)
RED, RED_BG = rgb(208, 40, 40), rgb(255, 234, 234)
WHITE, LINE = rgb(255, 255, 255), rgb(208, 214, 224)
FONT = "Yu Gothic UI"
TOTAL = 16


def load_passwords():
    pw = {}
    for path in sorted(glob.glob(PW_GLOB)):
        for line in open(path, encoding="utf-8"):
            if line.startswith("#") or "\t" not in line:
                continue
            name, secret = line.rstrip("\n").split("\t", 1)
            if name.startswith("user") and not secret.startswith("("):
                pw[name] = secret
    return pw


class Deck:
    def __init__(self, ppt, n, username, password, url):
        self.n, self.username, self.password, self.url = n, username, password, url
        self.pres = ppt.Presentations.Add()
        self.pres.PageSetup.SlideWidth, self.pres.PageSetup.SlideHeight = 960, 540
        self.idx = 0

    # ---- 部品 ----
    def text(self, s, txt, l, t, w, h, size=28, bold=False, color=INK, align=1, font=FONT, anchor=1):
        shp = s.Shapes.AddTextbox(1, l, t, w, h)
        tf = shp.TextFrame
        tf.WordWrap = -1
        tf.AutoSize = 0
        tf.MarginLeft = tf.MarginRight = 4
        tf.VerticalAnchor = anchor
        tr = tf.TextRange
        tr.Text = txt
        tr.Font.Name = font
        tr.Font.NameFarEast = font
        tr.Font.Size = size
        tr.Font.Bold = -1 if bold else 0
        tr.Font.Color.RGB = color
        tr.ParagraphFormat.Alignment = align
        shp.Height = h
        return shp

    def box(self, s, l, t, w, h, fill, line=None, radius=0.12):
        shp = s.Shapes.AddShape(5, l, t, w, h)
        try:
            shp.Adjustments[1] = radius
        except Exception:
            pass
        shp.Fill.ForeColor.RGB = fill
        if line is None:
            shp.Line.Visible = 0
        else:
            shp.Line.ForeColor.RGB = line
            shp.Line.Weight = 2.25
        shp.Shadow.Visible = 0
        return shp

    def new_slide(self, icon, title, accent=BLUE):
        self.idx += 1
        s = self.pres.Slides.Add(self.idx, 12)
        s.FollowMasterBackground = 0
        s.Background.Fill.ForeColor.RGB = WHITE
        self.text(s, icon, 40, 24, 90, 80, size=48, font="Segoe UI Emoji", align=2, anchor=3)
        self.text(s, title, 128, 24, 780, 80, size=40, bold=True, anchor=3)
        bar = self.box(s, 48, 108, 140, 8, accent, radius=0.5)
        self.text(s, "Trade Cockpit 利用マニュアル　利用者%d" % self.n, 40, 500, 600, 28, size=13, color=GRAY)
        self.text(s, "%d / %d" % (self.idx, TOTAL), 800, 500, 120, 28, size=13, color=GRAY, align=3)
        return s

    def steps(self, s, items, top=140, gap=74, color=BLUE, left=56, width=560):
        for i, txt in enumerate(items, 1):
            y = top + (i - 1) * gap
            c = s.Shapes.AddShape(9, left, y, 54, 54)
            c.Fill.ForeColor.RGB = color
            c.Line.Visible = 0
            c.TextFrame.TextRange.Text = str(i)
            c.TextFrame.TextRange.Font.Size = 28
            c.TextFrame.TextRange.Font.Bold = -1
            c.TextFrame.TextRange.Font.Name = FONT
            c.TextFrame.TextRange.Font.Color.RGB = WHITE
            c.TextFrame.TextRange.ParagraphFormat.Alignment = 2
            c.TextFrame.VerticalAnchor = 3
            self.text(s, txt, left + 72, y - 4, width, 62, size=30, bold=True, anchor=3)

    def note(self, s, txt, l, t, w, h, kind="red", size=24):
        fill, col = {"red": (RED_BG, RED), "green": (GREEN_BG, GREEN), "blue": (BLUE_BG, BLUE)}[kind]
        self.box(s, l, t, w, h, fill, line=col)
        self.text(s, txt, l + 14, t, w - 28, h, size=size, bold=True, color=col, anchor=3, align=1)

    def save(self, path_pptx, path_pdf):
        self.pres.SaveAs(path_pptx)
        self.pres.SaveAs(path_pdf, 32)  # ppSaveAsPDF
        self.pres.Close()

    # ---- スライド ----
    def build(self):
        n, u, p, url = self.n, self.username, self.password, self.url
        # 1 表紙
        self.idx += 1
        s = self.pres.Slides.Add(self.idx, 12)
        s.FollowMasterBackground = 0
        s.Background.Fill.ForeColor.RGB = WHITE
        self.box(s, 0, 0, 960, 22, BLUE, radius=0)
        self.text(s, "📈", 380, 50, 200, 110, size=72, font="Segoe UI Emoji", align=2, anchor=3)
        self.text(s, "Trade Cockpit\n利用マニュアル", 80, 165, 800, 150, size=52, bold=True, align=2, anchor=3)
        self.text(s, "利用者%dさん専用" % n, 80, 320, 800, 50, size=30, bold=True, color=BLUE, align=2)
        self.box(s, 170, 385, 620, 92, BLUE_BG, line=BLUE)
        self.text(s, "ユーザー名： %s\nURL： %s" % (u, url), 190, 388, 590, 86, size=24, bold=True, color=INK, anchor=3)
        self.text(s, "このマニュアルはあなた専用です。他の人に見せないでください。", 80, 490, 800, 30, size=15, color=RED, align=2, bold=True)
        # 2 ログインに必要なもの
        s = self.new_slide("🔑", "ログインに必要なもの")
        for i, (lab, val, mono) in enumerate([("① アプリのURL", url, False), ("② ユーザー名", u, False), ("③ 初期パスワード", p, True)]):
            y = 135 + i * 108
            self.box(s, 60, y, 840, 96, BLUE_BG if i < 2 else GREEN_BG, line=BLUE if i < 2 else GREEN)
            self.text(s, lab, 80, y + 4, 300, 34, size=20, color=GRAY, bold=True)
            self.text(s, val, 80, y + 34, 800, 58, size=34, bold=True, color=INK, font="Consolas" if mono else FONT, anchor=3)
        self.note(s, "⚠ パスワードは他の人に教えないでください", 60, 452, 840, 44, "red", 22)
        # 3 iPhone
        s = self.new_slide("📱", "iPhoneで開く方法")
        self.steps(s, ["Safariを開く", "URLを入力する", "ユーザー名を入力", "パスワードを入力", "「ログイン」を押す"], top=132, gap=70, width=520)
        if os.path.exists(os.path.join(ASSETS, "login.png")):
            s.Shapes.AddPicture(os.path.join(ASSETS, "login.png"), 0, -1, 660, 124, 250, 350)
            self.text(s, "↑ ログイン画面", 660, 474, 250, 22, size=13, color=GRAY, align=2)
        # 4 ホーム画面
        s = self.new_slide("➕", "iPhoneのホーム画面に追加")
        self.steps(s, ["Safariでアプリを開く", "画面の下の「共有ボタン」（□に↑）を押す", "「ホーム画面に追加」を押す", "「追加」を押す"], top=140, gap=80, width=780)
        self.note(s, "✓ 次からは、ホーム画面のアイコンを押すだけで開けます", 60, 465, 840, 40, "green", 22)
        # 5 PC
        s = self.new_slide("💻", "PCで開く方法")
        self.steps(s, ["ChromeまたはEdgeを開く", "URLを開く", "ユーザー名とパスワードを入れて「ログイン」"], top=150, gap=100, width=780)
        self.note(s, "URL： %s" % url, 60, 455, 840, 44, "blue", 24)
        # 6-10 共通画面
        feat = [
            ("🎯", "今日の注目TOP5", "今日これから大きく動く可能性がある銘柄を見る画面です。", "みんなで同じものを見ます", "green",
             "「監視銘柄」画面の上のほうにあります。"),
            ("⏰", "今買い時TOP5", "いまのチャート・出来高・材料などから、買う条件（ENTRY条件）が整ってきた銘柄を見る画面です。", "みんなで同じものを見ます", "green",
             "「監視銘柄」画面の上のほうにあります。"),
        ]
        for icon, title, desc, tag, kind, where in feat:
            s = self.new_slide(icon, title, GREEN)
            self.text(s, desc, 60, 150, 840, 150, size=32, bold=True, anchor=1)
            self.note(s, "👥 " + tag, 60, 330, 840, 60, kind, 26)
            self.text(s, where, 60, 410, 840, 50, size=22, color=GRAY)
        # 8 監視銘柄
        s = self.new_slide("👀", "監視銘柄", GREEN)
        self.box(s, 60, 140, 410, 230, GREEN_BG, line=GREEN)
        self.text(s, "共通監視銘柄", 76, 148, 380, 44, size=28, bold=True, color=GREEN)
        self.text(s, "全員に見えます。\n利用者が削除しても\n全体からは消えません。", 76, 196, 380, 170, size=24, bold=True)
        self.box(s, 490, 140, 410, 230, BLUE_BG, line=BLUE)
        self.text(s, "自分で追加した銘柄", 506, 148, 380, 44, size=28, bold=True, color=BLUE)
        self.text(s, "あなただけに見えます。\n他の人には見えません。", 506, 196, 380, 170, size=24, bold=True)
        self.text(s, "※ 監視銘柄の画面は、共通の銘柄と自分の銘柄が一つのリストで表示されます。", 60, 395, 840, 60, size=20, color=GRAY)
        # 9 ニュース・イベント
        s = self.new_slide("📰", "ニュース・イベント", GREEN)
        self.text(s, "株価に影響しそうなニュースや、\n決算・日銀・FOMC（アメリカの金利を決める会議）\nなどの予定を確認できます。", 60, 150, 840, 200, size=32, bold=True)
        self.note(s, "画面の上の「ニュース」「イベント」ボタンから開きます", 60, 380, 840, 60, "blue", 24)
        # 10 Radar
        s = self.new_slide("📡", "Radar（レーダー）", GREEN)
        self.text(s, "急に株価や出来高が動き始めた銘柄を\n見つけて知らせます。", 60, 150, 840, 150, size=34, bold=True)
        self.note(s, "👥 みんなで同じものを見ます", 60, 330, 840, 60, "green", 26)
        self.text(s, "「監視銘柄」画面の中に表示されます。", 60, 410, 840, 50, size=22, color=GRAY)
        # 11-13 個人画面
        priv = [("💼", "ポジション", "自分が保有している銘柄を見る画面です。", "画面の上の「ポジション」ボタン"),
                ("📝", "トレード分析", "自分の売買の、良かった点・改善点を見る画面です。", "画面の上の「記録」「トレード分析」ボタン"),
                ("🗓", "今日の振り返り", "今日の自分の売買を振り返る画面です。", "画面の上の「今日の振り返り」ボタン")]
        for icon, title, desc, where in priv:
            s = self.new_slide(icon, title, RED)
            self.text(s, desc, 60, 150, 840, 130, size=34, bold=True)
            self.note(s, "🔒 重要：他の人には見えません", 60, 300, 840, 70, "red", 30)
            self.text(s, where, 60, 400, 840, 50, size=22, color=GRAY)
        # 14 ログアウト
        s = self.new_slide("🚪", "ログアウト")
        self.text(s, "使い終わったら、画面の一番上にある", 60, 140, 840, 50, size=30, bold=True)
        self.box(s, 220, 210, 520, 70, WHITE, line=LINE)
        self.text(s, "ログイン中：%s" % u, 236, 214, 300, 62, size=20, bold=True, anchor=3)
        b = self.box(s, 560, 222, 160, 46, BLUE, radius=0.3)
        b.TextFrame.TextRange.Text = "ログアウト"
        b.TextFrame.TextRange.Font.Size = 20
        b.TextFrame.TextRange.Font.Bold = -1
        b.TextFrame.TextRange.Font.Name = FONT
        b.TextFrame.TextRange.Font.Color.RGB = WHITE
        b.TextFrame.VerticalAnchor = 3
        self.text(s, "ボタンを押してください。", 60, 305, 840, 50, size=30, bold=True)
        self.note(s, "共用のスマホ・パソコンでは、必ずログアウトしてください", 60, 395, 840, 60, "red", 24)
        # 15 困ったとき
        s = self.new_slide("🆘", "困ったとき")
        rows = [("開かない", "SafariやChromeを一度閉じて、もう一度開く"), ("更新されない", "画面の「リアルタイムデータを反映」ボタンを押す"),
                ("ログインできない", "ユーザー名とパスワードをもう一度確認する"), ("分からない", "管理者へ連絡する")]
        for i, (a, b2) in enumerate(rows):
            y = 135 + i * 88
            self.box(s, 60, y, 250, 76, BLUE_BG, line=BLUE)
            self.text(s, a, 66, y, 240, 76, size=24, bold=True, color=BLUE, align=2, anchor=3)
            self.text(s, "→ " + b2, 326, y, 590, 76, size=24, bold=True, anchor=3)
        # 16 大事な注意
        s = self.new_slide("⚠", "大事な注意", RED)
        items = ["TOP5に出ても、必ず上がるわけではありません", "最終的な売買判断は、あなた自身が行います", "パスワードは他人に教えないでください",
                 "他の人のポジションや振り返りは、あなたには見えません"]
        for i, t in enumerate(items):
            y = 138 + i * 86
            self.box(s, 60, y, 840, 74, RED_BG if i < 3 else BLUE_BG, line=RED if i < 3 else BLUE)
            self.text(s, ("⚠ " if i < 3 else "🔒 ") + t, 76, y, 810, 74, size=24, bold=True, color=RED if i < 3 else BLUE, anchor=3)
        assert self.idx == TOTAL, self.idx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=DEFAULT_URL)
    args = ap.parse_args()
    passwords = load_passwords()
    missing = [f"user{i}" for i in range(1, 7) if f"user{i}" not in passwords]
    if missing:
        print("初期パスワードが見つからないユーザー:", missing); return 2
    ppt = win32com.client.Dispatch("PowerPoint.Application")
    try:
        for i in range(1, 7):
            name = f"user{i}"
            folder = os.path.join(OUT, name)
            os.makedirs(folder, exist_ok=True)
            deck = Deck(ppt, i, name, passwords[name], args.url)
            deck.build()
            deck.save(os.path.join(folder, f"manual_{name}.pptx"), os.path.join(folder, f"manual_{name}.pdf"))
            print("作成:", name)
    finally:
        ppt.Quit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
