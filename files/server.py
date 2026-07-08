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
    """最新終値(t)・1営業日前(p)・直近終値の推移(spark, 10-1章のミニスパークライン用)を返す"""
    h = yf.Ticker(sym).history(period="1mo")
    closes = h["Close"].dropna()
    if len(closes) == 0:
        return None
    t = round(float(closes.iloc[-1]), 2)
    p = round(float(closes.iloc[-2]), 2) if len(closes) >= 2 else None
    spark = [round(float(x), 2) for x in closes.tolist()[-20:]]
    return {"t": t, "p": p, "spark": spark}


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
    """登録銘柄それぞれの現在値(t)・前日終値(p)・当日高値(high)・当日安値(low)・売買代金(turnover)を返す。
    売買代金は 終値×出来高 で概算（セクターの並び替え用。4章の時価総額ソートから変更）。
    価格履歴と同じ history() の出来高列から計算するため、追加のAPI呼び出しは不要。"""
    out = {}
    if yf is None:
        return out
    for w in watchlist:
        code = w.get("code", "")
        if not code:
            continue
        sym = _yf_symbol(w)
        try:
            tk = yf.Ticker(sym)
            h = tk.history(period="7d")
            closes = h["Close"].dropna()
            if len(closes) == 0:
                continue
            t = round(float(closes.iloc[-1]), 2)
            p = round(float(closes.iloc[-2]), 2) if len(closes) >= 2 else None
            highs = h["High"].dropna()
            lows = h["Low"].dropna()
            volumes = h["Volume"].dropna()
            turnover = float(t) * float(volumes.iloc[-1]) if len(volumes) and t is not None else None
            spark = [round(float(x), 2) for x in closes.tolist()[-20:]]  # 10-1章：カードUIのミニスパークライン用
            out[code] = {
                "t": t, "p": p,
                "high": round(float(highs.iloc[-1]), 2) if len(highs) else None,
                "low": round(float(lows.iloc[-1]), 2) if len(lows) else None,
                "turnover": turnover,
                "spark": spark,
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


#  9章：登録銘柄ニュースは「決算を含むIR・適時開示」のみに絞る。GoogleニュースRSSには構造化
# カテゴリがないため、クエリ自体をIR寄りにした上で、タイトルに以下キーワードを含むものだけに
# 絞り込む代替策を取っている（完全なIR/適時開示フィードではなく、あくまでキーワードベースの近似）。
IR_KEYWORDS = [
    "決算", "上方修正", "下方修正", "業績予想", "業績修正", "自己株式", "自社株買い", "配当",
    "株式分割", "適時開示", "増資", "決算短信", "通期", "四半期", "特別損失", "特別利益",
    "新株予約権", "有価証券報告書", "開示", "IR", "本決算", "決算発表",
]


def _is_ir_news(title):
    return any(k in title for k in IR_KEYWORDS)


def build_stock_news(watchlist):
    """登録銘柄ニュースを配列で返す（各要素 code/name/title/url/source/published）。
    9章の仕様により、決算・IR・適時開示に関連するもののみに絞り込む。"""
    order = {"優先": 0, "通常": 1, "様子見": 2}
    wl = sorted(watchlist, key=lambda w: order.get(w.get("watch", "通常"), 1))[:12]
    items = []
    for w in wl:
        name = w.get("name", "")
        code = w.get("code", "")
        if not name:
            continue
        candidates = google_news(name + " 決算 適時開示 業績", 4)
        ir_only = [it for it in candidates if _is_ir_news(it["title"])]
        for it in ir_only[:2]:
            items.append({**it, "code": code, "name": name})
    return items


# 9-1章：国内市況・海外市況のサブタブ用にクエリを分けて取得する
MACRO_QUERIES_DOMESTIC = ["日経平均 見通し", "日銀 金融政策 決定", "ドル円 相場"]
MACRO_QUERIES_OVERSEAS = ["FRB 利上げ 金利", "米国株式市場 ダウ"]


def build_macro_news():
    """マクロニュースを国内・海外に分けて返す（国内タプル, 海外タプル）。"""
    domestic = []
    for q in MACRO_QUERIES_DOMESTIC:
        domestic.extend(google_news(q, 3))
    overseas = []
    for q in MACRO_QUERIES_OVERSEAS:
        overseas.extend(google_news(q, 3))
    return domestic, overseas


# 12-1章：分析タブのテクニカル指標（移動平均・RSI・ボリンジャーバンド）計算。
# 外部ライブラリ(ta-lib等)を追加せず、既存のyfinance終値配列から素朴に計算する。
def _sma(values, n):
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def _rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _bollinger(closes, n=20, k=2):
    if len(closes) < n:
        return None, None, None
    window = closes[-n:]
    mean = sum(window) / n
    var = sum((x - mean) ** 2 for x in window) / n
    std = var ** 0.5
    return mean, mean + k * std, mean - k * std  # mid, upper, lower


def analyze_stock(w):
    """12-1章：テクニカル(20日/50日移動平均・RSI14・ボリンジャーバンド・直近サポート/レジスタンス)と
    ファンダメンタル(PER・PBR・配当利回り・予想EPSの実績比乖離)を組み合わせて、購入・損切り・利確の
    目安単価と、それぞれの算出根拠を返す。値は目安であり断定的な推奨ではない(12-2章の方針)。"""
    sym = _yf_symbol(w)
    tk = yf.Ticker(sym)
    h = tk.history(period="6mo")
    closes = h["Close"].dropna().tolist()
    if len(closes) < 20:
        return None
    current = closes[-1]
    ma20 = _sma(closes, 20)
    ma50 = _sma(closes, 50)
    rsi = _rsi(closes, 14)
    bb_mid, bb_upper, bb_lower = _bollinger(closes, 20, 2)
    lookback = min(60, len(closes))
    support = min(closes[-lookback:])
    resistance = max(closes[-lookback:])

    fundamentals = {}
    try:
        info = tk.info or {}
        fundamentals = {
            "per": info.get("trailingPE"),
            "pbr": info.get("priceToBook"),
            "dividendYield": info.get("dividendYield"),
            "forwardEps": info.get("forwardEps"),
            "trailingEps": info.get("trailingEps"),
        }
    except Exception:
        pass

    # 購入単価(目安): 20日移動平均線付近への押し目を基準に、ボリンジャー下限を下回らないよう調整。
    # RSIが70以上(買われすぎ)の場合はやや低めに調整する。
    entry_reasons = []
    if ma20:
        entry = ma20
        entry_reasons.append(f"20日移動平均線({ma20:.1f})付近への押し目を想定")
    else:
        entry = current
        entry_reasons.append("移動平均線データ不足のため現在値を採用")
    if bb_lower and entry < bb_lower:
        entry = bb_lower
        entry_reasons.append(f"ボリンジャーバンド下限({bb_lower:.1f})を下回らない水準に調整")
    if rsi is not None and rsi >= 70:
        entry = min(entry, current * 0.98)
        entry_reasons.append(f"RSI({rsi:.0f})が買われすぎ水準のためやや低めに調整")

    # 損切り単価(目安): 直近安値(サポート)の-2%。ボリンジャー下限がさらに下ならそちらを採用。
    stop = support * 0.98
    stop_reasons = [f"直近{lookback}営業日の安値({support:.1f})の-2%を下限目安に設定"]
    if bb_lower and bb_lower < stop:
        stop = bb_lower * 0.99
        stop_reasons.append(f"ボリンジャーバンド下限({bb_lower:.1f})がさらに下のため、その-1%を採用")

    # 利確単価(目安): 直近高値(レジスタンス)と、損切り幅の2倍のリスクリワード目標のうち高い方。
    risk = max(entry - stop, 0.01)
    rr_target = entry + risk * 2
    target = max(resistance, rr_target)
    target_reasons = [f"直近{lookback}営業日の高値({resistance:.1f})、または損切り幅の2倍のリスクリワード目標({rr_target:.1f})のうち高い方を採用"]

    fund_notes = []
    per, pbr, div = fundamentals.get("per"), fundamentals.get("pbr"), fundamentals.get("dividendYield")
    if per:
        fund_notes.append(f"PER {per:.1f}倍")
    if pbr:
        fund_notes.append(f"PBR {pbr:.2f}倍")
    if div:
        # yfinanceのdividendYieldはバージョンにより小数(0.024=2.4%)と%換算済み数値(2.4)が混在するため、
        # 1未満なら小数とみなして100倍、それ以外はそのまま%として扱う。
        div_pct = div * 100 if div < 1 else div
        fund_notes.append(f"配当利回り {div_pct:.2f}%")
    fwd, trl = fundamentals.get("forwardEps"), fundamentals.get("trailingEps")
    if fwd and trl:
        dev = (fwd - trl) / abs(trl) * 100
        fund_notes.append(f"予想EPSは実績比{dev:+.1f}%")

    return {
        "current": round(current, 2),
        "entry": round(entry, 2), "entryReason": "・".join(entry_reasons),
        "stop": round(stop, 2), "stopReason": "・".join(stop_reasons),
        "target": round(target, 2), "targetReason": "・".join(target_reasons),
        "indicators": {
            "ma20": round(ma20, 2) if ma20 else None,
            "ma50": round(ma50, 2) if ma50 else None,
            "rsi": round(rsi, 1) if rsi is not None else None,
            "bbUpper": round(bb_upper, 2) if bb_upper else None,
            "bbLower": round(bb_lower, 2) if bb_lower else None,
            "support": round(support, 2), "resistance": round(resistance, 2),
        },
        "fundamentalNote": "・".join(fund_notes) if fund_notes else "取得できるファンダメンタルデータがありません",
    }


def build_analysis(watchlist):
    """12章：分析タブ対象銘柄それぞれの購入/損切り/利確の目安を返す。"""
    out = {}
    if yf is None:
        return out
    for w in watchlist:
        code = w.get("code", "")
        if not code:
            continue
        try:
            r = analyze_stock(w)
            if r:
                out[code] = r
        except Exception as e:
            print("  分析失敗", code, e)
    return out


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
            macro_domestic, macro_overseas = build_macro_news()
            macro_all = macro_domestic + macro_overseas
            self._send_json({
                "stockNews": stock,  # 9章：決算・IR・適時開示のみに絞り込み済み
                "macroNews": macro_all,
                "macroNewsDomestic": macro_domestic,  # 9-1章：国内市況サブタブ
                "macroNewsOverseas": macro_overseas,  # 9-1章：海外市況サブタブ
                # 旧フロント・バックアップ互換のためテキスト版も返す
                "stockNewsText": _stock_text(stock),
                "macroNewsText": _macro_text(macro_all),
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
        elif self.path.startswith("/api/analysis"):
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"[]"
            try:
                targets = json.loads(raw.decode("utf-8") or "[]")
            except Exception:
                targets = []
            print(f"[取得] 分析（対象 {len(targets)} 銘柄）…")
            analysis = build_analysis(targets)
            self._send_json({"analysis": analysis})
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
