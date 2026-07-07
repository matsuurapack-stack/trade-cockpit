# -*- coding: utf-8 -*-
"""
トレード・コックピット ローカルサーバー
- ブラウザの「リアルタイムデータを反映」ボタンから呼ばれ、その場でデータを取得して返す
- 取得: 指数/為替(yfinance)、ニュース(Googleニュース RSS)
- 使い方: start.bat をダブルクリック。ブラウザが自動で開きます。
"""
import os
import json
import time
import calendar
import datetime
import threading
import webbrowser
import urllib.parse
import urllib.request
from http.server import SimpleHTTPRequestHandler
from socketserver import ThreadingTCPServer

os.chdir(os.path.dirname(os.path.abspath(__file__)))

PORT = 8765

try:
    import yfinance as yf
except ImportError:
    yf = None
try:
    import feedparser
except ImportError:
    feedparser = None

INDEX = {
    "usdjpy": "JPY=X", "nikkei": "^N225", "dow": "^DJI",
    "nasdaq": "^IXIC", "sox": "^SOX", "us10y": "^TNX",
    "sp500": "^GSPC", "kospi": "^KS11",
    "nikkei_fut": "NIY=F", "dow_fut": "YM=F",
    "wti": "CL=F", "gold": "GC=F", "copper": "HG=F",
}


def _two_closes(sym):
    """最新終値(t)と1営業日前(p)を返す"""
    h = yf.Ticker(sym).history(period="7d")
    closes = h["Close"].dropna()
    if len(closes) == 0:
        return None
    t = round(float(closes.iloc[-1]), 2)
    p = round(float(closes.iloc[-2]), 2) if len(closes) >= 2 else None
    return {"t": t, "p": p}


def get_index_quotes():
    out = {}
    if yf is None:
        return out
    for key, sym in INDEX.items():
        try:
            r = _two_closes(sym)
            if r:
                out[key] = r
        except Exception as e:
            print("  index失敗", key, e)
    return out


# 登録銘柄（watchlist）の market===IDX 用シンボル上書き（TradingViewシンボルはyfinance非対応のため）
IDX_YF_OVERRIDE = {"NI225": "^N225", "USDJPY": "JPY=X", "SOX": "SOXX", "VIX": "VIXY"}


def _yf_symbol(w):
    code = w.get("code", "")
    market = w.get("market", "JP")
    if market == "US":
        return code
    if market == "IDX":
        return IDX_YF_OVERRIDE.get(code, code)
    return code + ".T"  # 日本株


def get_stock_quotes(watchlist):
    """登録銘柄それぞれの現在値(t)・前日終値(p)・当日高値(high)・当日安値(low)を返す。"""
    out = {}
    if yf is None:
        return out
    for w in watchlist:
        code = w.get("code", "")
        if not code:
            continue
        sym = _yf_symbol(w)
        try:
            h = yf.Ticker(sym).history(period="7d")
            closes = h["Close"].dropna()
            if len(closes) == 0:
                continue
            t = round(float(closes.iloc[-1]), 2)
            p = round(float(closes.iloc[-2]), 2) if len(closes) >= 2 else None
            highs = h["High"].dropna()
            lows = h["Low"].dropna()
            out[code] = {
                "t": t, "p": p,
                "high": round(float(highs.iloc[-1]), 2) if len(highs) else None,
                "low": round(float(lows.iloc[-1]), 2) if len(lows) else None,
            }
        except Exception as e:
            print("  個別銘柄失敗", code, sym, e)
    return out


def _fmt_published(entry):
    """RSS の pubDate(GMT) を日本時間 'MM/DD HH:MM' に整形。無ければ空。"""
    pp = entry.get("published_parsed")
    if not pp:
        return ""
    try:
        jst = datetime.timezone(datetime.timedelta(hours=9))
        dt = datetime.datetime.fromtimestamp(calendar.timegm(pp), datetime.timezone.utc).astimezone(jst)
        return dt.strftime("%m/%d %H:%M")
    except Exception:
        return ""


def _source_of(entry, title):
    """出典（媒体名）を取り出す。Google ニュースの見出しは末尾が ' - 媒体名'。"""
    src = entry.get("source")
    if isinstance(src, dict):
        s = src.get("title") or src.get("value")
        if s:
            return s
    if " - " in title:
        return title.rsplit(" - ", 1)[1].strip()
    return ""


def _clean_title(title):
    """見出し末尾の ' - 媒体名' を除いた本文だけを返す。"""
    if " - " in title:
        return title.rsplit(" - ", 1)[0].strip()
    return title


def google_news(query, n=2):
    if feedparser is None:
        return []
    url = ("https://news.google.com/rss/search?q="
           + urllib.parse.quote(query) + "&hl=ja&gl=JP&ceid=JP:ja")
    try:
        feed = feedparser.parse(url)
        out = []
        for e in feed.entries[:n]:
            raw = e.get("title", "")
            out.append({
                "title": _clean_title(raw),
                "url": e.get("link", ""),
                "source": _source_of(e, raw),
                "published": _fmt_published(e),
            })
        return out
    except Exception:
        return []


def build_stock_news(watchlist):
    """登録銘柄ニュースを配列で返す（各要素 code/name/title/url/source/published）。"""
    order = {"優先": 0, "通常": 1, "様子見": 2}
    wl = sorted(watchlist, key=lambda w: order.get(w.get("watch", "通常"), 1))[:12]
    items = []
    for w in wl:
        name = w.get("name", "")
        code = w.get("code", "")
        if not name:
            continue
        for it in google_news(name + " 株価 決算", 2):
            items.append({**it, "code": code, "name": name})
    return items


def build_macro_news():
    """マクロニュースを配列で返す。"""
    queries = ["日経平均 見通し", "日銀 金融政策 決定", "FRB 利上げ 金利",
               "ドル円 相場", "米国株式市場 ダウ"]
    items = []
    for q in queries:
        for it in google_news(q, 2):
            items.append(it)
    return items


def _stock_text(items):
    return "\n".join(f"・[{it['code']} {it['name']}] {it['title']}\n  {it['url']}" for it in items)


def _macro_text(items):
    return "\n".join(f"・{it['title']}\n  {it['url']}" for it in items)


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass  # アクセスログは静かに

    def _send_json(self, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        if self.path.startswith("/api/quotes"):
            print("[取得] 指数・為替 …")
            quotes = get_index_quotes()
            now = datetime.datetime.now().strftime("%H:%M:%S")
            self._send_json({"quotes": quotes, "fetchedAt": now})
        else:
            super().do_GET()  # HTMLなどの静的配信

    def do_POST(self):
        if self.path.startswith("/api/news"):
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"[]"
            try:
                watchlist = json.loads(raw.decode("utf-8") or "[]")
            except Exception:
                watchlist = []
            print(f"[取得] ニュース（登録銘柄 {len(watchlist)} 件 ＋ マクロ）…")
            stock = build_stock_news(watchlist)
            macro = build_macro_news()
            self._send_json({
                "stockNews": stock,
                "macroNews": macro,
                # 旧フロント・バックアップ互換のためテキスト版も返す
                "stockNewsText": _stock_text(stock),
                "macroNewsText": _macro_text(macro),
            })
        elif self.path.startswith("/api/stock-quotes"):
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"[]"
            try:
                watchlist = json.loads(raw.decode("utf-8") or "[]")
            except Exception:
                watchlist = []
            print(f"[取得] 登録銘柄の現在値（{len(watchlist)} 件）…")
            quotes = get_stock_quotes(watchlist)
            now = datetime.datetime.now().strftime("%H:%M:%S")
            self._send_json({"quotes": quotes, "fetchedAt": now})
        else:
            self.send_response(404)
            self.end_headers()


def open_browser():
    time.sleep(1.5)
    webbrowser.open(f"http://localhost:{PORT}/trade-cockpit.html")


def main():
    if yf is None or feedparser is None:
        print("必要なライブラリが見つかりません。setup.bat を先に実行してください。")
        input("Enterで終了します。")
        return
    try:
        httpd = ThreadingTCPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        # ポート使用中（前回のサーバーが残っている等）。親切に案内して終了。
        print("=" * 52)
        print(f" ポート {PORT} が既に使用中のため、起動できませんでした。")
        print(" すでにサーバーが起動している可能性があります。")
        print(" 前回の黒いウィンドウ（サーバー）を閉じてから、")
        print(" もう一度 start.bat を実行してください。")
        print("=" * 52)
        input("Enterで終了します。")
        return
    threading.Thread(target=open_browser, daemon=True).start()
    print("=" * 52)
    print(" トレード・コックピット サーバー起動中")
    print("  ブラウザが自動で開きます。開かない場合は下記を開いてください：")
    print(f"  http://localhost:{PORT}/trade-cockpit.html")
    print("  使い終わったら、このウィンドウを閉じてください。")
    print("=" * 52)
    with httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
