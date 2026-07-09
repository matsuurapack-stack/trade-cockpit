# -*- coding: utf-8 -*-
"""
トレード・コックピット ローカルサーバー
- ブラウザの「リアルタイムデータを反映」ボタンから呼ばれ、その場でデータを取得して返す
- 取得: 指数/為替(yfinance)、ニュース(Googleニュース RSS)
- 使い方: start.bat をダブルクリック。ブラウザが自動で開きます。
"""
import os
import re
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


STOCK_QUOTES_CHUNK = 80  # 一括ダウンロード1回あたりの銘柄数（多すぎる一括取得も失敗しやすいため分割する）


def _download_chunk(symbols):
    """yf.downloadで複数銘柄をまとめて取得する。個別Ticker().history()を数百件連続で呼ぶと
    Yahoo側のレート制限に引っかかり、後半の銘柄ほど失敗しやすくなるため、まとめて取得することで
    速度・成功率の両方を改善する（実測：個別逐次は285件で数分＋失敗多発、一括は80件で約3秒・成功率100%）。
    group_by="ticker"指定時は銘柄が1件でも同じ階層構造（MultiIndex）で返るため、後続処理を共通化できる。"""
    try:
        return yf.download(symbols, period="7d", group_by="ticker", threads=True,
                            progress=False, auto_adjust=False)
    except Exception as e:
        print("  一括取得失敗", symbols[:3], "…", len(symbols), "件", e)
        return None


def get_stock_quotes(watchlist):
    """登録銘柄それぞれの現在値(t)・前日終値(p)・当日高値(high)・当日安値(low)・売買代金(turnover)を返す。
    売買代金は 終値×出来高 で概算（セクターの並び替え用。4章の時価総額ソートから変更）。
    価格履歴と同じ history() の出来高列から計算するため、追加のAPI呼び出しは不要。"""
    out = {}
    if yf is None:
        return out
    items = [(w.get("code", ""), _yf_symbol(w)) for w in watchlist if w.get("code")]
    if not items:
        return out
    symbols = [sym for _, sym in items]

    frames = {}
    for i in range(0, len(symbols), STOCK_QUOTES_CHUNK):
        chunk = symbols[i:i + STOCK_QUOTES_CHUNK]
        data = _download_chunk(chunk)
        if data is None:
            continue
        for sym in chunk:
            try:
                sub = data[sym]
                if sub is not None and not sub.empty:
                    frames[sym] = sub
            except Exception:
                pass  # このシンボルだけ結果に含まれなかった（上場廃止・シンボル誤り等）

    for code, sym in items:
        h = frames.get(sym)
        if h is None:
            continue
        try:
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


# 6-2章：SNSタブ。X公式の埋め込みタイムライン(widgets.js)はsyndication.twitter.comが
# 429(レート制限)を頻発して表示できないため、Nitter(Xの代替フロントエンド)のRSSフィードを
# サーバー側で取得する方式に切り替えた（ニュースタブのGoogleニュースRSSと同じ手法）。
# Nitterの公開インスタンスは不安定なため、複数インスタンスを順に試す。
NITTER_INSTANCES = ["nitter.net", "xcancel.com", "nitter.privacyredirect.com"]


def _nitter_tweet_url(handle, nitter_link):
    """NitterのURL(https://nitter.net/handle/status/12345#m)から実際のXの投稿URLを組み立てる。"""
    m = re.search(r"/status/(\d+)", nitter_link or "")
    if m:
        return f"https://x.com/{handle}/status/{m.group(1)}"
    return f"https://x.com/{handle}"


def get_sns_posts(handle, n=15):
    """指定アカウントの直近投稿をNitter RSS経由で取得する。全インスタンス失敗時は空配列を返し、
    フロント側で「Xで開く」フォールバック表示に切り替える。"""
    if feedparser is None:
        return []
    for host in NITTER_INSTANCES:
        url = f"https://{host}/{handle}/rss"
        try:
            feed = feedparser.parse(url, request_headers={"User-Agent": "Mozilla/5.0"})
            if feed.bozo or not feed.entries:
                continue
            out = []
            for e in feed.entries[:n]:
                out.append({
                    "title": e.get("title", ""),
                    "url": _nitter_tweet_url(handle, e.get("link", "")),
                    "published": _fmt_published(e),
                })
            if out:
                return out
        except Exception as ex:
            print("  SNS取得失敗", host, handle, ex)
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


# 12-1章：分析タブのテクニカル指標計算。外部ライブラリ(ta-lib等)を追加せず、
# 既存のyfinance終値配列から素朴に計算する。
def _sma(values, n):
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def _sma_series(values, n):
    """values と同じ長さのリストを返す。各要素は直近n件の単純移動平均（不足時はNone）。
    技術分析ルール指示書のパンパカパン／ゴールデンクロス判定など、系列としての推移が必要な箇所で使う。"""
    out = []
    for i in range(len(values)):
        if i + 1 < n:
            out.append(None)
        else:
            out.append(sum(values[i + 1 - n:i + 1]) / n)
    return out


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


def _rci(values, n=26):
    """順位相関指数(RCI)。直近n日の「日付順位」と「価格順位」の相関を-100〜+100で返す。
    価格順位は安い順に1〜n（上昇トレンドで日付順位と一致し+100に近づく、一般的な定義）。"""
    if len(values) < n:
        return None
    window = values[-n:]
    date_rank = list(range(1, n + 1))  # 1=最も古い i.e. window[0] 〜 n=最新
    order = sorted(range(n), key=lambda i: window[i])  # 価格が安い順のインデックス列
    price_rank = [0] * n
    for r, idx in enumerate(order):
        price_rank[idx] = r + 1
    d2 = sum((date_rank[i] - price_rank[i]) ** 2 for i in range(n))
    return (1 - 6 * d2 / (n * (n * n - 1))) * 100


def _market_environment():
    """技術分析ルール指示書 1-7章・4-3章：相場環境（日経平均のトレンド）を判定し、
    地合いの良し悪しに応じたコメントを返す。分析対象銘柄ごとに毎回取得すると重いため、
    build_analysis() 内で1回だけ計算して全銘柄で使い回す。"""
    try:
        h = yf.Ticker("^N225").history(period="3mo")
        closes = h["Close"].dropna().tolist()
        if len(closes) < 25:
            return "相場環境：データ不足のため判定できません"
        ma25 = _sma(closes, 25)
        current = closes[-1]
        if current > ma25 * 1.01:
            return "地合い良好（日経平均が25日線より上で上昇トレンド）"
        if current < ma25 * 0.99:
            return "地合い不安定（日経平均が25日線より下で下落トレンド）→ デイトレード推奨"
        return "地合い中立（日経平均は25日線付近で横ばい）"
    except Exception:
        return "相場環境：取得できませんでした"


def analyze_stock(w, market_env=""):
    """12-1章・technical_analysis_rules.md：ローソク足パターン・移動平均線の並び／クロス・
    ボリンジャーバンド・RCI・複合底打ち条件などから買い/売りシグナルを判定し、その中から
    最有力のシグナルに基づいて購入・損切り・利確の目安単価と算出根拠を返す。
    値はあくまで目安であり断定的な推奨ではない(12-2章の方針)。"""
    sym = _yf_symbol(w)
    tk = yf.Ticker(sym)
    h = tk.history(period="1y")
    closes = h["Close"].dropna().tolist()
    opens = h["Open"].dropna().tolist()
    highs = h["High"].dropna().tolist()
    lows = h["Low"].dropna().tolist()
    volumes = h["Volume"].dropna().tolist()
    if len(closes) < 20:
        return None
    n = len(closes)
    current = closes[-1]
    prev = closes[-2] if n >= 2 else current
    change_pct = (current - prev) / prev * 100 if prev else 0

    ma25_s, ma75_s, ma100_s = _sma_series(closes, 25), _sma_series(closes, 75), _sma_series(closes, 100)
    ma25, ma75, ma100 = ma25_s[-1], ma75_s[-1], ma100_s[-1]
    rsi = _rsi(closes, 14)
    bb_mid, bb_upper2, bb_lower2 = _bollinger(closes, 20, 2)
    _, bb_upper3, bb_lower3 = _bollinger(closes, 20, 3)
    rci26 = _rci(closes, 26)
    lookback = min(60, n)
    support = min(closes[-lookback:])
    resistance = max(closes[-lookback:])
    high52w = max(highs[-min(252, len(highs)):]) if highs else current
    low52w = min(lows[-min(252, len(lows)):]) if lows else current
    vol_avg5 = sum(volumes[-6:-1]) / 5 if len(volumes) >= 6 else None
    vol_surge = vol_avg5 is not None and volumes[-1] > vol_avg5 * 1.5

    low_zone = current <= high52w * 0.8   # 条件C等：52週高値の-20%以下＝安値圏
    high_zone = current >= high52w * 0.95  # 条件B等：52週高値の-5%以内＝高値圏

    # ---- ローソク足の形（直近1本）----
    o, c, hi, lo = opens[-1], closes[-1], highs[-1], lows[-1]
    body = abs(c - o)
    rng = max(hi - lo, 0.0001)
    is_bull, is_bear = c > o, c < o
    lower_shadow, upper_shadow = min(o, c) - lo, hi - max(o, c)
    is_doji = body <= rng * 0.1
    is_long_lower_shadow = lower_shadow >= body * 1.5 and body > 0
    is_hanging_man = high_zone and is_long_lower_shadow and (hi - max(o, c)) <= body * 0.5

    # はらみ線：直近足の実体が前の足の実体に完全に収まる
    is_harami = False
    if n >= 2:
        o2, c2 = opens[-2], closes[-2]
        body1_hi, body1_lo = max(o2, c2), min(o2, c2)
        body2_hi, body2_lo = max(o, c), min(o, c)
        is_harami = body1_hi - body1_lo > 0 and body2_hi <= body1_hi and body2_lo >= body1_lo

    buy_signals, sell_signals = [], []

    # 条件A/B：下落トレンド中の出来高急増＋陽線／下ヒゲ
    downtrend = ma25 is not None and current < ma25
    if downtrend and vol_surge and is_bull:
        buy_signals.append({"key": "volBull", "label": "出来高急増を伴う陽線（下落局面の反転）", "price": c})
    if downtrend and vol_surge and is_long_lower_shadow:
        buy_signals.append({"key": "volShadow", "label": "出来高急増を伴う下ヒゲ（売り圧力の底打ち）", "price": lo})

    # 条件C/D：安値圏での十字線／はらみ線
    if low_zone and is_doji:
        buy_signals.append({"key": "dojiLow", "label": "安値圏での十字線（上昇転換の前触れ）", "price": c})
    if low_zone and is_harami:
        buy_signals.append({"key": "haramiLow", "label": "安値圏でのはらみ線", "price": c})

    # 条件G：パンパカパン（25>75>100が全て右肩上がり）
    panpakapan = False
    if ma25 and ma75 and ma100 and ma25 > ma75 > ma100:
        rising = all(s[-1] is not None and s[-6] is not None and s[-1] > s[-6]
                     for s in (ma25_s, ma75_s, ma100_s)) if n >= 6 else False
        if rising:
            panpakapan = True
            buy_signals.append({
                "key": "panpakapan",
                "label": f"パンパカパン形成（25日線{ma25:.1f}＞75日線{ma75:.1f}＞100日線{ma100:.1f}が全て上昇）",
                "price": ma25,
            })

    # 条件H／2-4条件G：ゴールデンクロス／デッドクロス（直近5日以内）
    golden_cross = dead_cross = False
    if n >= 6:
        for i in range(-5, 0):
            a0, b0, a1, b1 = ma25_s[i - 1], ma75_s[i - 1], ma25_s[i], ma75_s[i]
            if None in (a0, b0, a1, b1):
                continue
            if a0 <= b0 and a1 > b1:
                golden_cross = True
            if a0 >= b0 and a1 < b1:
                dead_cross = True
    if golden_cross:
        buy_signals.append({"key": "goldenCross", "label": "ゴールデンクロス（25日線が75日線を上抜け）", "price": current})
    if dead_cross:
        sell_signals.append({"key": "deadCross", "label": "デッドクロス（25日線が75日線を下抜け）", "price": current})

    # 条件I：ボリンジャーバンド-3σタッチ
    bb3_touch = bb_lower3 is not None and current <= bb_lower3
    if bb3_touch:
        buy_signals.append({"key": "bb3", "label": f"ボリンジャーバンド-3σ（{bb_lower3:.1f}）にタッチ", "price": bb_lower3})

    # 条件（複合底打ち）：4条件のうち2つ以上
    bottom_conditions = [
        change_pct <= -2.5,
        ma25 is not None and current <= ma25 * 0.97,
        bb3_touch,
        rci26 is not None and rci26 <= -90,
    ]
    bottom_count = sum(1 for x in bottom_conditions if x)
    if bottom_count >= 2:
        buy_signals.append({"key": "compoundBottom", "label": f"複合底打ちシグナル（4条件中{bottom_count}件が該当）", "price": current})

    # ---- 売りシグナル ----
    if high_zone and is_doji:
        sell_signals.append({"key": "dojiHigh", "label": "高値圏での十字線（下落転換の前触れ）", "price": c})
    if is_hanging_man:
        sell_signals.append({"key": "hangingMan", "label": "高値圏での首吊り線", "price": c})
    if high_zone and is_harami:
        sell_signals.append({"key": "haramiHigh", "label": "高値圏でのはらみ線", "price": c})
    if ma100 is not None and current < ma100:
        sell_signals.append({"key": "ma100Break", "label": f"100日移動平均線（{ma100:.1f}）を割り込み（最終防衛線突破）", "price": ma100})
    # 上値抵抗線での売り：直近レジスタンス付近で複数回跳ね返されている
    near_resistance_count = sum(1 for x in closes[-20:] if resistance > 0 and x >= resistance * 0.98)
    if near_resistance_count >= 3 and current < resistance * 0.98:
        sell_signals.append({"key": "resistanceReject", "label": f"上値抵抗線（{resistance:.1f}）に複数回はね返される", "price": resistance})

    # ---- 強度（★1〜5）：最有力シグナルの種類で判定 ----
    signal_keys = {s["key"] for s in buy_signals}
    if "panpakapan" in signal_keys:
        strength = 5
    elif "compoundBottom" in signal_keys and bottom_count >= 3:
        strength = 4
    elif {"goldenCross", "bb3", "compoundBottom"} & signal_keys:
        strength = 3
    elif buy_signals:
        strength = 2
    else:
        strength = 1

    # ---- ファンダメンタル ----
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

    # ---- 購入単価(目安)：買いシグナルの優先度順に採用。該当なしなら20日線基準にフォールバック ----
    priority = ["panpakapan", "compoundBottom", "goldenCross", "bb3", "volBull", "volShadow", "dojiLow", "haramiLow"]
    primary = None
    for key in priority:
        primary = next((s for s in buy_signals if s["key"] == key), None)
        if primary:
            break

    if primary:
        entry = primary["price"]
        entry_reasons = [primary["label"] + f"（{entry:.1f}）"]
    else:
        entry = ma25 if ma25 else current
        entry_reasons = [f"該当する買いシグナルなし。25日移動平均線({entry:.1f})付近への押し目を暫定的に採用" if ma25 else "データ不足のため現在値を採用"]
    if rsi is not None and rsi >= 70:
        entry = min(entry, current * 0.98)
        entry_reasons.append(f"RSI({rsi:.0f})が買われすぎ水準のためやや低めに調整")

    # ---- 損切り単価(目安)：4-2章の優先順位（100日線割れ+3% → -3σ-2% → 購入値-5% → サポート-2%）----
    stop, stop_reasons = None, []
    if ma100 is not None:
        stop = ma100 * 0.97
        stop_reasons.append(f"100日移動平均線（{ma100:.1f}）を明確に割り込んだ水準（-3%）を最終防衛ラインに設定")
    elif bb_lower3 is not None:
        stop = bb_lower3 * 0.98
        stop_reasons.append(f"ボリンジャーバンド-3σ（{bb_lower3:.1f}）をさらに下回った水準（-2%）を設定")
    elif entry:
        stop = entry * 0.95
        stop_reasons.append("移動平均線データ不足のため、購入単価から-5%をデフォルトの損切り水準に設定")
    else:
        stop = support * 0.98
        stop_reasons.append(f"直近{lookback}営業日の安値({support:.1f})の-2%を下限目安に設定")

    # ---- 利確単価(目安)：買い理由の種類に応じたルール(3章)を適用 ----
    if primary and primary["key"] == "compoundBottom":
        target = entry * 1.025
        target_reasons = ["急落・底打ちからの買いのため、+2.5%（下落分を取り戻す水準）を利確目安に設定"]
    elif primary and primary["key"] == "bb3":
        target = bb_mid if bb_mid else entry * 1.05
        target_reasons = [f"ボリンジャーバンド中央線（{target:.1f}）までの戻りを利確目安に設定"]
    elif primary and primary["key"] == "panpakapan":
        risk = max(entry - stop, 0.01)
        target = max(resistance, entry + risk * 2)
        target_reasons = [f"トレンド継続中のため、直近高値（{resistance:.1f}）またはリスクリワード2倍（{entry + risk * 2:.1f}）を暫定目標に設定。"
                           "短期線・中期線の接近が3回発生、または100日線割れが出たタイミングで見直してください"]
    else:
        risk = max(entry - stop, 0.01)
        rr_target = entry + risk * 2
        target = max(resistance, rr_target)
        target_reasons = [f"直近{lookback}営業日の高値({resistance:.1f})、またはリスクリワード2倍の目標({rr_target:.1f})のうち高い方を採用"]

    fund_notes = []
    per, pbr, div = fundamentals.get("per"), fundamentals.get("pbr"), fundamentals.get("dividendYield")
    if per:
        fund_notes.append(f"PER {per:.1f}倍" + ("（60倍超のため成長期待の織り込み過ぎに注意）" if per > 60 else ""))
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
        "strength": strength,
        "marketEnv": market_env,
        "signals": {
            "buy": [{"key": s["key"], "label": s["label"]} for s in buy_signals],
            "sell": [{"key": s["key"], "label": s["label"]} for s in sell_signals],
        },
        "indicators": {
            "ma25": round(ma25, 2) if ma25 else None,
            "ma75": round(ma75, 2) if ma75 else None,
            "ma100": round(ma100, 2) if ma100 else None,
            "rsi": round(rsi, 1) if rsi is not None else None,
            "rci26": round(rci26, 1) if rci26 is not None else None,
            "bbUpper2": round(bb_upper2, 2) if bb_upper2 else None,
            "bbLower2": round(bb_lower2, 2) if bb_lower2 else None,
            "bbLower3": round(bb_lower3, 2) if bb_lower3 else None,
            "support": round(support, 2), "resistance": round(resistance, 2),
            "high52w": round(high52w, 2), "low52w": round(low52w, 2),
        },
        "fundamentalNote": "・".join(fund_notes) if fund_notes else "取得できるファンダメンタルデータがありません",
    }


def build_analysis(watchlist):
    """12章：分析タブ対象銘柄それぞれの購入/損切り/利確の目安を返す。
    相場環境（日経平均のトレンド）は全銘柄共通のため1回だけ計算する。"""
    out = {}
    if yf is None:
        return out
    market_env = _market_environment()
    for w in watchlist:
        code = w.get("code", "")
        if not code:
            continue
        try:
            r = analyze_stock(w, market_env)
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
        elif self.path.startswith("/api/sns"):
            qs = urllib.parse.urlparse(self.path).query
            handle = urllib.parse.parse_qs(qs).get("handle", [""])[0]
            print(f"[取得] SNS（@{handle}）…")
            posts = get_sns_posts(handle) if handle else []
            self._send_json({"posts": posts})
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
