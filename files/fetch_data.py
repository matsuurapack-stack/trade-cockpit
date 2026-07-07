# -*- coding: utf-8 -*-
"""
トレード・コックピット用 データ取得スクリプト
- 株価/指数（ドル円・日経・ダウ・ナスダック・SOX・米10年債）を yfinance で取得
- 登録銘柄とマクロのニュースを Google ニュース RSS で取得
- 結果を cockpit-data.js に書き出し、コックピットHTMLが読み込む

毎朝 run.bat をダブルクリックするだけでOKです。
"""
import os
import sys
import json
import glob
import datetime
import urllib.parse

# このファイルがある場所を作業フォルダにする（ダブルクリック実行でも確実に動くように）
os.chdir(os.path.dirname(os.path.abspath(__file__)))

# ---- ライブラリの読み込み（未インストールなら親切に案内） ----
missing = []
try:
    import yfinance as yf
except ImportError:
    yf = None
    missing.append("yfinance")
try:
    import feedparser
except ImportError:
    feedparser = None
    missing.append("feedparser")

if missing:
    print("必要なライブラリが見つかりません:", ", ".join(missing))
    print("→ setup.bat をダブルクリックしてインストールしてください。")
    input("Enterキーで終了します。")
    sys.exit(1)

# ---- 取得する指数・為替（キー名はコックピット側と一致させる） ----
INDEX = {
    "usdjpy": "JPY=X",   # ドル円
    "nikkei": "^N225",   # 日経平均
    "dow":    "^DJI",    # NYダウ
    "nasdaq": "^IXIC",   # ナスダック総合
    "sox":    "^SOX",    # フィラデルフィア半導体指数
    "us10y":  "^TNX",    # 米10年債利回り
}


def get_quotes():
    """各指数の最新終値を取得して辞書で返す"""
    quotes = {}
    for key, sym in INDEX.items():
        try:
            hist = yf.Ticker(sym).history(period="5d")
            if len(hist) > 0:
                val = float(hist["Close"].iloc[-1])
                quotes[key] = round(val, 2)
                print(f"  {key:8s} {sym:8s} = {quotes[key]}")
            else:
                print(f"  {key:8s} {sym:8s} = データなし")
        except Exception as e:
            print(f"  {key:8s} {sym:8s} = 取得失敗 ({e})")
    return quotes


def load_watchlist():
    """同じフォルダにあるコックピットのバックアップJSONから登録銘柄を読む"""
    files = sorted(glob.glob("trade-cockpit-*.json"))
    if not files:
        print("  登録銘柄のバックアップ(trade-cockpit-*.json)が見つかりません。")
        print("  → コックピットの『バックアップ↓』で保存し、このフォルダに置いてください。")
        return []
    latest = files[-1]
    print(f"  登録銘柄を読み込み: {latest}")
    try:
        with open(latest, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("watchlist", [])
    except Exception as e:
        print("  読み込み失敗:", e)
        return []


def google_news(query, n=3):
    """Google ニュース RSS を検索して記事リストを返す"""
    url = ("https://news.google.com/rss/search?q="
           + urllib.parse.quote(query)
           + "&hl=ja&gl=JP&ceid=JP:ja")
    try:
        feed = feedparser.parse(url)
        out = []
        for e in feed.entries[:n]:
            out.append({"title": e.get("title", ""), "url": e.get("link", ""),
                        "date": e.get("published", "")})
        return out
    except Exception as e:
        print("  ニュース取得失敗:", query, e)
        return []


def build_stock_news(watchlist):
    """注目度の高い順に、登録銘柄のニュースを集めて整形テキストにする"""
    order = {"優先": 0, "通常": 1, "様子見": 2}
    wl = sorted(watchlist, key=lambda w: order.get(w.get("watch", "通常"), 1))
    targets = wl[:12]  # リクエストが増えすぎないよう上位12銘柄まで
    lines = []
    for w in targets:
        name = w.get("name", "")
        code = w.get("code", "")
        if not name:
            continue
        for it in google_news(name + " 株価 決算", 2):
            lines.append(f"・[{code} {name}] {it['title']}\n  {it['url']}")
    return "\n".join(lines)


def build_macro_news():
    """相場全体に効くマクロニュースを集めて整形テキストにする"""
    queries = ["日経平均 見通し", "日銀 金融政策 決定", "FRB 利上げ 金利",
               "ドル円 相場", "米国株式市場 ダウ"]
    lines = []
    for q in queries:
        for it in google_news(q, 2):
            lines.append(f"・{it['title']}\n  {it['url']}")
    return "\n".join(lines)


def main():
    print("=" * 48)
    print(" トレード・コックピット データ取得")
    print("=" * 48)

    print("\n[1/3] 指数・為替を取得中 …")
    quotes = get_quotes()

    print("\n[2/3] 登録銘柄のニュースを取得中 …")
    watchlist = load_watchlist()
    stock_news = build_stock_news(watchlist)

    print("\n[3/3] マクロのニュースを取得中 …")
    macro_news = build_macro_news()

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    payload = {
        "fetchedAt": now,
        "quotes": quotes,
        "stockNewsText": stock_news,
        "macroNewsText": macro_news,
    }
    with open("cockpit-data.js", "w", encoding="utf-8") as f:
        f.write("window.PYDATA = " + json.dumps(payload, ensure_ascii=False, indent=2) + ";\n")

    print("\n" + "=" * 48)
    print(f" 完了！ {now} 時点のデータを cockpit-data.js に保存しました。")
    print(" コックピットHTMLを開き（開いていればリロードし）、")
    print(" 『Pythonデータを反映』ボタンを押してください。")
    print("=" * 48)
    input("\nEnterキーで終了します。")


if __name__ == "__main__":
    main()
