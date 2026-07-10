# -*- coding: utf-8 -*-
"""
トレード・コックピット ローカルサーバー
- ブラウザの「リアルタイムデータを反映」ボタンから呼ばれ、その場でデータを取得して返す
- 取得: 指数/為替(yfinance)、ニュース(Googleニュース RSS)
- 使い方: start.bat をダブルクリック。ブラウザが自動で開きます。
"""
import io
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

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

os.chdir(os.path.dirname(os.path.abspath(__file__)))

IS_CLOUD = bool(os.environ.get("RENDER") or os.environ.get("PORT"))
PORT = int(os.environ.get("PORT", 8765))
HOST = "0.0.0.0" if IS_CLOUD else "127.0.0.1"

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


def num_or_none(v):
    """フロントから渡ってくる可能性のある文字列/空文字/Noneをfloatか、変換不可ならNoneに正規化する。"""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


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


def _published_ts(entry):
    """ソート用のUNIX時刻。取得できない場合は0（末尾扱い）にする。"""
    pp = entry.get("published_parsed")
    if not pp:
        return 0
    try:
        return calendar.timegm(pp)
    except Exception:
        return 0


# キーワード一致度優先のGoogleニュース検索では、関連記事が少ないクエリだと数週間〜数ヶ月前の
# 古い記事まで拾ってしまうことがある（重大ニュースのキーワードに偶然一致した過去記事等）。
# 「重要ニュース」表示も含め、鮮度の低い記事が紛れ込まないよう取得時点でここまで絞り込む。
# 個別銘柄の決算・IRは四半期に一度など元々頻度が低いため、マクロニュースより長めの期間を許容する。
NEWS_MAX_AGE_DAYS = 14
STOCK_NEWS_MAX_AGE_DAYS = 45


def _is_recent(entry, max_age_days=NEWS_MAX_AGE_DAYS):
    ts = _published_ts(entry)
    if ts <= 0:
        return False
    return (time.time() - ts) <= max_age_days * 86400


def google_news(query, n=2, max_age_days=NEWS_MAX_AGE_DAYS):
    """GoogleニュースRSSは検索クエリ単位では関連度寄りの順序で返り、必ずしも新しい順ではないため、
    ここで公開日時の降順（新しい記事が先頭）に並べ替えてから返す。古い記事（max_age_days超）は
    ここで除外する。複数クエリの結果を連結して使う呼び出し元（build_stock_news/build_macro_news）でも、
    連結後に改めて全体を日時順に並べ替えている。"""
    if feedparser is None:
        return []
    url = ("https://news.google.com/rss/search?q="
           + urllib.parse.quote(query) + "&hl=ja&gl=JP&ceid=JP:ja")
    try:
        feed = feedparser.parse(url)
        recent_entries = [e for e in feed.entries if _is_recent(e, max_age_days)]
        entries = sorted(recent_entries, key=_published_ts, reverse=True)
        out = []
        for e in entries[:n]:
            raw = e.get("title", "")
            out.append({
                "title": _clean_title(raw),
                "url": e.get("link", ""),
                "source": _source_of(e, raw),
                "published": _fmt_published(e),
                "_ts": _published_ts(e),
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
    フロント側で「Xで開く」フォールバック表示に切り替える。投稿文だけのシンプル表示のため、
    画像等の付加情報は取得しない（安定性優先）。"""
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


def _title_mentions_name(name, title):
    """Googleニュースの検索結果はクエリ語の一部だけに一致した無関係記事（noteの個人ブログ等）も
    紛れ込むため、IRキーワードだけでなく銘柄名自体が見出しに含まれているかも確認する。"""
    return name.lower() in title.lower()


def _sort_and_strip(items):
    """複数クエリの結果を連結したリストを公開日時の降順に並べ替え、ソート用の内部フィールドを除く。"""
    items = sorted(items, key=lambda it: it.get("_ts", 0), reverse=True)
    return [{k: v for k, v in it.items() if k != "_ts"} for it in items]


# 適時開示は本来Googleニュースの近似ではなく、TDnet（適時開示情報閲覧サービス）の公開一覧ページ
# （https://www.release.tdnet.info/inbs/I_list_XXX_YYYYMMDD.html）を直接スクレイピングして「本日
# 発表された決算・業績関連の適時開示」を取得する。1回のページ取得（複数ページに分割されている
# 場合は全ページ）でその日の全上場企業分がまとまっているため、登録銘柄が何社あっても銘柄ごとに
# 検索する必要がなく、上位12社だけに絞る必要もない（kabutan同様、公式APIはないため公開HTMLの
# スクレイピング。ユーザー承認済み方針）。アメリカ株はTDnet対象外のため、従来通りGoogleニュース
# RSSを優先度順の上位12社まで検索する方式を維持する。
TDNET_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
_TDNET_ROW_RE = re.compile(
    r'<td class="\w+new-L kjTime" noWrap>([\d:]+)</td>\s*'
    r'<td class="\w+new-M kjCode" noWrap>([0-9A-Z]+)</td>\s*'
    r'<td class="\w+new-M kjName" noWrap>[^<]*</td>\s*'
    r'<td class="\w+new-M kjTitle" align="left"><a href="([^"]+)"[^>]*>([^<]*)</a>'
)
_TDNET_TOTAL_RE = re.compile(r"全(\d+)件")
EARNINGS_TITLE_KEYWORDS = ["決算", "業績予想", "業績の修正", "業績修正", "上方修正", "下方修正"]


def _is_earnings_title(title):
    return any(k in title for k in EARNINGS_TITLE_KEYWORDS)


def _tdnet_fetch_page(date_str, page):
    url = f"https://www.release.tdnet.info/inbs/I_list_{page:03d}_{date_str}.html"
    req = urllib.request.Request(url, headers={"User-Agent": TDNET_UA})
    with urllib.request.urlopen(req, timeout=8) as res:
        return res.read().decode("utf-8", errors="replace")


def _tdnet_sort_key(time_str):
    """開示時刻(HH:MM、本日分)をソート用のUNIX時刻に変換する。"""
    try:
        jst = datetime.timezone(datetime.timedelta(hours=9))
        hh, mm = time_str.split(":")
        return datetime.datetime.now(jst).replace(hour=int(hh), minute=int(mm), second=0, microsecond=0).timestamp()
    except Exception:
        return 0


def _tdnet_today_disclosures():
    """本日TDnetに開示された情報を、証券コード(4桁)をキーにした辞書（値はリスト、1コードに
    複数開示があることもある）で返す。取得失敗時は空辞書（分析全体は失敗させない方針）。"""
    date_str = datetime.date.today().strftime("%Y%m%d")
    by_code = {}
    try:
        html = _tdnet_fetch_page(date_str, 1)
    except Exception as e:
        print("  TDnet取得失敗", e)
        return by_code
    m = _TDNET_TOTAL_RE.search(html)
    total = int(m.group(1)) if m else 0
    pages = min(max((total + 99) // 100, 1), 10)  # 安全のため最大10ページ(1000件)まで
    htmls = [html]
    for p in range(2, pages + 1):
        try:
            htmls.append(_tdnet_fetch_page(date_str, p))
        except Exception as e:
            print("  TDnet取得失敗", p, e)
            break
    for page_html in htmls:
        for time_s, code5, href, title in _TDNET_ROW_RE.findall(page_html):
            code = code5[:-1] if len(code5) == 5 else code5
            by_code.setdefault(code, []).append({
                "time": time_s,
                "title": title.strip(),
                "url": "https://www.release.tdnet.info/inbs/" + href,
            })
    return by_code


# 決算短信の1〜2ページ目は東証が指定する統一フォーマット（サマリー情報）のため、会社が変わっても
# 「経営成績（実績）」「通期の連結業績予想」の表はほぼ同じ並びで出てくる。ここではLLMを使わず、
# その表を正規表現で抜き出して「前年同期比」「会社計画に対する進捗率」「予想修正の有無」を
# 機械的に要約する（自由記述の定性コメントの要約にはLLMが必要なため対象外）。
_EARNINGS_ROW_RE = re.compile(
    r"(\S+?年\S+?月期\S*)\s+([\d,]+)\s+(△?[\d.]+)\s+([\d,]+)\s+(△?[\d.]+)\s+"
    r"([\d,]+)\s+(△?[\d.]+)\s+([\d,]+)\s+(△?[\d.]+)"
)
_EARNINGS_FORECAST_RE = re.compile(
    r"通期\s+([\d,]+)\s+(△?[\d.]+)\s+([\d,]+)\s+(△?[\d.]+)\s+"
    r"([\d,]+)\s+(△?[\d.]+)\s+([\d,]+)\s+(△?[\d.]+)"
)
_GUIDANCE_REVISED_RE = re.compile(r"業績予想からの修正の有無[：:]\s*([無有])")


def _num(s):
    return float(s.replace(",", ""))


def _pct(s):
    return -_num(s[1:]) if s.startswith("△") else _num(s)


def _fetch_pdf_text(url, max_pages=2):
    """決算短信PDFの先頭ページ群のテキストを返す。取得・解析失敗時は空文字（呼び出し元で
    要約をスキップする＝ニュース自体は表示され、要約だけが付かない形に落とす）。"""
    if PdfReader is None:
        return ""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": TDNET_UA})
        with urllib.request.urlopen(req, timeout=10) as res:
            data = res.read()
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((reader.pages[i].extract_text() or "") for i in range(min(max_pages, len(reader.pages))))
    except Exception as e:
        print("  決算短信PDF取得失敗", url, e)
        return ""


def summarize_earnings_pdf(url):
    """決算短信PDFのサマリー表から「前年同期比」「通期会社計画比の進捗率」「予想修正の有無」を
    抜き出したテンプレート文を返す。表のレイアウトが想定と異なる銘柄では抽出できずNoneになる
    （数値ベースの決定的な処理のみで、定性コメントの要約は行わない＝1段階目の実装）。"""
    text = _fetch_pdf_text(url)
    m = _EARNINGS_ROW_RE.search(text)
    if not m:
        return None
    period = m.group(1)
    revenue, revenue_yoy = _num(m.group(2)), _pct(m.group(3))
    op, op_yoy = _num(m.group(4)), _pct(m.group(5))
    net, net_yoy = _num(m.group(8)), _pct(m.group(9))

    parts = [
        f"{period}実績：売上高{revenue:,.0f}百万円({revenue_yoy:+.1f}%)・"
        f"営業利益{op:,.0f}百万円({op_yoy:+.1f}%)・純利益{net:,.0f}百万円({net_yoy:+.1f}%)"
    ]

    fm = _EARNINGS_FORECAST_RE.search(text)
    if fm:
        f_rev, f_rev_yoy = _num(fm.group(1)), _pct(fm.group(2))
        f_op, f_op_yoy = _num(fm.group(3)), _pct(fm.group(4))
        progress = f"（今回までの進捗率{revenue / f_rev * 100:.1f}%）" if f_rev else ""
        parts.append(
            f"通期会社計画：売上高{f_rev:,.0f}百万円({f_rev_yoy:+.1f}%)・"
            f"営業利益{f_op:,.0f}百万円({f_op_yoy:+.1f}%){progress}"
        )

    gm = _GUIDANCE_REVISED_RE.search(text)
    if gm:
        parts.append("業績予想は今回修正あり（要確認）" if gm.group(1) == "有" else "業績予想は据え置き")

    return "。".join(parts) + "。"


# 決算分析タブ（「決算」ボタン）用：TDnetの日付一覧ページは銘柄横断検索ができないため、
# yanoshin氏が公開している非公式のTDnetミラーAPI（銘柄コード単位で開示履歴を返す）を使い、
# 「本日」に限らず直近の決算短信を探す。公式APIではないため取得失敗時は空リストで諦める。
TDNET_HISTORY_API = "https://webapi.yanoshin.jp/webapi/tdnet/list/{code}.json?limit=30"


def _tdnet_company_history(code):
    url = TDNET_HISTORY_API.format(code=code)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": TDNET_UA})
        with urllib.request.urlopen(req, timeout=8) as res:
            data = json.loads(res.read().decode("utf-8", errors="replace"))
    except Exception as e:
        print("  TDnet銘柄別履歴取得失敗", code, e)
        return []
    out = []
    for item in data.get("items", []):
        t = item.get("Tdnet", {})
        title = t.get("title", "")
        raw_url = t.get("document_url", "") or ""
        # yanoshinのdocument_urlは "https://webapi.yanoshin.jp/rd.php?<実URL>" のリダイレクト形式。
        pdf_url = raw_url.split("rd.php?", 1)[-1] if "rd.php?" in raw_url else raw_url
        out.append({"title": title, "pubdate": t.get("pubdate", ""), "url": pdf_url})
    return out


def latest_earnings_detail(code, name):
    """指定銘柄の直近の決算短信を探し、summarize_earnings_pdf()の数値要約を添えて返す。
    見つからない・要約できない場合はtitle/summaryがNoneのまま返す（フロント側で
    「見つかりません」「要約できません」の文言に分岐させる）。"""
    result = {"code": code, "name": name, "title": None, "url": None, "published": None, "summary": None}
    latest = next((h for h in _tdnet_company_history(code) if "決算短信" in h["title"]), None)
    if not latest:
        return result
    result["title"] = latest["title"]
    result["url"] = latest["url"]
    result["published"] = latest["pubdate"]
    if latest["url"]:
        result["summary"] = summarize_earnings_pdf(latest["url"])
    return result


def build_stock_news(watchlist):
    """登録銘柄ニュースを配列で返す（各要素 code/name/title/url/source/published）。
    9章の仕様により、決算・IR・適時開示に関連するもののみに絞り込む。
    日本株はTDnetの本日開示一覧（登録銘柄数によらず1回のページ取得でカバー）から本日発表済みの
    決算・業績関連開示のみを抽出する。アメリカ株はTDnet対象外のため、Googleニュースを優先度順の
    上位12社まで検索する（銘柄ごとに検索する方式のため件数を絞っている）。"""
    items = []

    jp_items = [w for w in watchlist if w.get("market", "JP") != "US"]
    us_items = [w for w in watchlist if w.get("market", "JP") == "US"]

    if jp_items:
        jst = datetime.timezone(datetime.timedelta(hours=9))
        today_str = datetime.datetime.now(jst).strftime("%m/%d")
        tdnet = _tdnet_today_disclosures()
        for w in jp_items:
            code, name = w.get("code", ""), w.get("name", "")
            if not code or not name:
                continue
            for e in tdnet.get(code, []):
                if not _is_earnings_title(e["title"]):
                    continue
                entry = {
                    "code": code, "name": name, "title": e["title"], "url": e["url"],
                    "source": "TDnet", "published": f"{today_str} {e['time']}",
                    "_ts": _tdnet_sort_key(e["time"]),
                }
                # 決算短信（東証統一フォーマットのサマリー表を含む）のみ数値要約を試みる。
                # 決算説明資料など別フォーマットのPDFは対象外（無理に解析すると誤読のリスクがあるため）。
                if "決算短信" in e["title"]:
                    summary = summarize_earnings_pdf(e["url"])
                    if summary:
                        entry["summary"] = summary
                items.append(entry)

    order = {"優先": 0, "通常": 1, "様子見": 2}
    wl = sorted(us_items, key=lambda w: order.get(w.get("watch", "通常"), 1))[:12]
    for w in wl:
        name = w.get("name", "")
        code = w.get("code", "")
        if not name:
            continue
        candidates = google_news(name + " 決算 適時開示 業績", 4, max_age_days=STOCK_NEWS_MAX_AGE_DAYS)
        ir_only = [it for it in candidates if _is_ir_news(it["title"]) and _title_mentions_name(name, it["title"])]
        for it in ir_only[:2]:
            items.append({**it, "code": code, "name": name})

    return _sort_and_strip(items)


# 9-1章：国内市況・海外市況のサブタブ用にクエリを分けて取得する
MACRO_QUERIES_DOMESTIC = ["日経平均 見通し", "日銀 金融政策 決定", "ドル円 相場"]
MACRO_QUERIES_OVERSEAS = ["FRB 利上げ 金利", "米国株式市場 ダウ"]


def build_macro_news():
    """マクロニュースを国内・海外に分けて返す（国内タプル, 海外タプル）。
    複数クエリの結果を連結後、公開日時の降順に並べ替えてから返す。"""
    domestic = []
    for q in MACRO_QUERIES_DOMESTIC:
        domestic.extend(google_news(q, 3))
    overseas = []
    for q in MACRO_QUERIES_OVERSEAS:
        overseas.extend(google_news(q, 3))
    return _sort_and_strip(domestic), _sort_and_strip(overseas)


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


def _atr(highs, lows, closes, period=14):
    """ATR（Average True Range）。前日終値を考慮したTrue Rangeの移動平均で、
    「その日のうちに現実的に動きうる値幅」の目安として使う。"""
    n = len(closes)
    if n < period + 1:
        return None
    trs = []
    for i in range(1, n):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        trs.append(tr)
    if len(trs) < period:
        return None
    return sum(trs[-period:]) / period


# 12-1章：分析の購入/損切り/利確単価は「その日の売買時間内」で現実的な値である必要がある。
# 東証には前日終値を基準にした値幅制限（ストップ高・ストップ安）があり、以前はMA100や60営業日高値
# など複数日単位の水準をそのまま目安に使っていたため、この制限を超える非現実的な価格になることが
# あった。日本株についてはこの値幅制限テーブルで上限・下限を算出し、必ずその範囲内に収める。
TSE_PRICE_LIMIT_TABLE = [
    (100, 30), (200, 50), (500, 80), (700, 100), (1000, 150), (1500, 300), (2000, 400),
    (3000, 500), (5000, 700), (7000, 1000), (10000, 1500), (15000, 3000), (20000, 4000),
    (30000, 5000), (50000, 7000), (70000, 10000), (100000, 15000), (150000, 30000),
    (200000, 40000), (300000, 50000), (500000, 70000), (700000, 100000), (1000000, 150000),
    (1500000, 300000), (2000000, 400000), (3000000, 500000), (5000000, 700000),
    (7000000, 1000000), (10000000, 1500000),
]


def tse_price_limit(prev_close):
    """前日終値からその日の値幅制限（ストップ安値, ストップ高値）を返す。"""
    if prev_close is None or prev_close <= 0:
        return None, None
    for threshold, width in TSE_PRICE_LIMIT_TABLE:
        if prev_close < threshold:
            return max(prev_close - width, 1), prev_close + width
    width = TSE_PRICE_LIMIT_TABLE[-1][1]
    return prev_close - width, prev_close + width


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


def _fetch_intraday(tk, interval):
    """当日（直近の取引セッション）の分足を取得する。市場時間外・取得失敗時はNoneを返す。"""
    try:
        h = tk.history(period="1d", interval=interval)
        closes = h["Close"].dropna().tolist()
        highs = h["High"].dropna().tolist()
        lows = h["Low"].dropna().tolist()
        volumes = h["Volume"].dropna().tolist()
        if len(closes) < 2:
            return None
        return {"closes": closes, "highs": highs, "lows": lows, "volumes": volumes}
    except Exception:
        return None


def _vwap(closes, volumes):
    """出来高加重平均価格（当日の分足から算出する、ザラ場でよく見る節目の一つ）。"""
    total_vol = sum(volumes)
    if total_vol <= 0:
        return None
    return sum(c * v for c, v in zip(closes, volumes)) / total_vol


EARNINGS_RATINGS_FILE = "earnings_ratings.json"


def _load_earnings_ratings():
    """決算またぎ期待値の星評価（銘柄コード→{stars, updatedAt}）。JSONファイルはfiles/直下に置き、
    OneDrive共有フォルダ経由でアプリ自体と一緒に共有されるようにする（1章の仕様）。"""
    try:
        with open(EARNINGS_RATINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_earnings_ratings(data):
    with open(EARNINGS_RATINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _days_to_earnings(tk):
    """trading_rules.mdの決算またぎルール判定に使う、次回決算発表までの日数。
    取得できない場合はNoneを返す（銘柄によっては非開示・データなしのことがある）。"""
    try:
        cal = tk.calendar or {}
        dates = cal.get("Earnings Date")
        if not dates:
            return None
        future = [d for d in dates if d >= datetime.date.today()]
        target = min(future) if future else min(dates)
        return (target - datetime.date.today()).days
    except Exception:
        return None


# trading_rules_追加分（信用倍率編）ルール②：貸借倍率(信用倍率=買残÷売残)。yfinanceは日本株の
# 信用残高を取得できないため、kabutanの銘柄ページをスクレイピングして代用する（ユーザー承認済み方針）。
KABUTAN_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
_MARGIN_RATIO_RE = re.compile(
    r"<td>[\d.]+<span class=\"fs9\">倍</span></td>\s*"
    r"<td>[\d.]+<span class=\"fs9\">倍</span></td>\s*"
    r"<td>[\d.]+<span class=\"fs9\">％</span></td>\s*"
    r"<td>([\d.]+)<span class=\"fs9\">倍</span></td>"
)


def _kabutan_margin_ratio(code):
    """銘柄コードから信用倍率(貸借倍率)を取得する。kabutanはUser-Agent未指定のリクエストを
    403で拒否するため、ブラウザ相当のUAを付与する。ページ構造の変化・アクセス失敗時はNoneを返し、
    分析全体は失敗させない（ニュース/SNS取得の他の外部データ取得と同じ、失敗を許容する方針）。"""
    try:
        req = urllib.request.Request(f"https://kabutan.jp/stock/?code={code}", headers={"User-Agent": KABUTAN_UA})
        with urllib.request.urlopen(req, timeout=5) as res:
            html = res.read().decode("utf-8", errors="replace")
        m = _MARGIN_RATIO_RE.search(html)
        return float(m.group(1)) if m else None
    except Exception as e:
        print("  信用倍率取得失敗", code, e)
        return None


def _margin_badge(ratio):
    """trading_rules_追加分ルール②の3段階判定：10倍未満=通常／10倍以上30倍以下=慎重／30倍超=厳重注意。"""
    if ratio is None:
        return None, None
    if ratio > 30:
        return "danger", f"貸借倍率{ratio:.2f}倍のため戻り売り警戒（上値に含み損玉が多い可能性、厳重注意）"
    if ratio >= 10:
        return "caution", f"貸借倍率{ratio:.2f}倍のため戻り売りに慎重"
    return "normal", f"貸借倍率{ratio:.2f}倍（通常水準）"


def analyze_stock(w, market_env=""):
    """12-1章・technical_analysis_rules.md：ローソク足パターン・移動平均線の並び／クロス・
    ボリンジャーバンド・RCI・複合底打ち条件などから買い/売りシグナルを判定し、その中から
    最有力のシグナルに基づいて購入・損切り・利確の目安単価と算出根拠を返す。
    値はあくまで目安であり断定的な推奨ではない(12-2章の方針)。"""
    sym = _yf_symbol(w)
    tk = yf.Ticker(sym)
    # Yahoo側の一時的なレート制限で日足が空/不足で返ってくることがあり、その場合そのまま
    # 「データを取得できませんでした」になってしまっていたため、1回だけ間を置いて再試行する。
    h = tk.history(period="1y")
    if len(h.get("Close", [])) < 20:
        time.sleep(1.5)
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
    # フロント側が直前の「リアルタイムデータを反映」で取得済みの現在値(w["current"])があれば、
    # そちらを優先して使う。analyze_stock()はここで別途yfinanceに問い合わせるため、タイミング次第で
    # 登録銘柄テーブルの現在値とズレることがあり、「反映直後に分析してもテーブルの最新値と
    # 一致しない」という不整合の原因になっていた。現在値を上書きすることで、直近の終値を使う
    # 移動平均線・RSI等の指標にも最新値が反映されるようにする。
    current_override = num_or_none(w.get("current"))
    current = current_override if current_override is not None else closes[-1]
    if current_override is not None:
        closes[-1] = current_override
    prev = closes[-2] if n >= 2 else current
    change_pct = (current - prev) / prev * 100 if prev else 0

    # ---- 当日の分足（1分足・5分足・15分足）をリアルタイムに取得し、日足だけでは分からない
    # 「その日ここまでの実際の値動き」を購入/損切り/利確単価に反映する。市場時間外・取得失敗時は
    # Noneのままとし、日足ベースの計算にフォールバックする。----
    m1 = _fetch_intraday(tk, "1m")
    m5 = _fetch_intraday(tk, "5m")
    m15 = _fetch_intraday(tk, "15m")
    intraday_high = max(m1["highs"]) if m1 else None
    intraday_low = min(m1["lows"]) if m1 else None
    intraday_range = (intraday_high - intraday_low) if (intraday_high is not None and intraday_low is not None) else None
    vwap = _vwap(m1["closes"], m1["volumes"]) if m1 else None
    rsi_5m = _rsi(m5["closes"], 14) if m5 and len(m5["closes"]) >= 15 else None
    rsi_15m = _rsi(m15["closes"], 14) if m15 and len(m15["closes"]) >= 15 else None

    # ---- trading_rules_追加分ルール①③：GU（ギャップアップ）率と、寄り付き高値からの押し目形成有無。
    # 「様子見(初押し待ち)」判定と「寄り付き高値を追わない」警告の両方でこの2つを使う。----
    today_open = opens[-1] if opens else None
    gu_pct = (today_open - prev) / prev * 100 if (today_open is not None and prev) else None
    elapsed_minutes = len(m1["closes"]) if m1 else None  # 1分足の本数を経過分数の目安として使う
    pullback_formed = False
    if m1 and len(m1["closes"]) >= 2:
        peak = max(m1["closes"])
        pullback_formed = m1["closes"][-1] <= peak * 0.997  # 高値から0.3%以上の押し目

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
    vol_thin = vol_avg5 is not None and volumes[-1] < vol_avg5 * 0.7
    change_1w = (current - closes[-6]) / closes[-6] * 100 if n >= 6 and closes[-6] else None

    # ---- trading_rules_追加分ルール①：好決算日等の様子見ルール。5%以上のGU or 出来高急増で、
    # 寄り付きから60分未満・かつ押し目がまだ形成されていなければ「様子見中」とする。----
    watch_status = None
    if m1 and elapsed_minutes is not None and elapsed_minutes < 60 and not pullback_formed:
        if (gu_pct is not None and gu_pct >= 5) or vol_surge:
            watch_status = "様子見中(初押し待ち)"

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

    # ---- 購入単価(目安)：ATR(当日の現実的な値幅)を基準に、現在値からの押し目水準を設定する。
    # 60営業日高値やMA100等の複数日単位の水準をそのまま使うと、値幅制限（ストップ高安）を
    # 超える非現実的な価格になるため、シグナルは「どの根拠で買いと判定したか」の説明にのみ使い、
    # 実際の価格はその日のATRから逆算する。当日の1分足から算出した実際の値幅（intraday_range）が
    # 日次ATRを上回っている場合は、そちらを優先する（リアルタイムの値動きをより直接反映するため）。
    atr14 = _atr(highs, lows, closes, 14)
    atr_ref = atr14 if atr14 else current * 0.02  # ATRが計算できない場合は現在値の2%を代用
    used_intraday_range = False
    if intraday_range and intraday_range > atr_ref:
        atr_ref = intraday_range
        used_intraday_range = True

    # trading_rules.mdのチャート確認優先順位（出来高→移動平均線→VWAP→ボリンジャーバンド→RSI）に合わせ、
    # 複数シグナルが同時点灯した場合はこの順で「最有力の根拠」を選ぶ（VWAP/RSIは単独の買いシグナルを
    # 持たず、entry調整の理由として別途entry_reasonsに追記される）。
    priority = ["volBull", "volShadow", "panpakapan", "goldenCross", "bb3", "compoundBottom", "dojiLow", "haramiLow"]
    primary = None
    for key in priority:
        primary = next((s for s in buy_signals if s["key"] == key), None)
        if primary:
            break

    atr_label = "当日1分足の実測値幅" if used_intraday_range else "当日のATR"
    if primary:
        entry = current - atr_ref * 0.3
        entry_reasons = [f"{primary['label']}が点灯。{atr_label}({atr_ref:.1f})から見た現実的な押し目水準として{entry:.1f}を採用"]
    else:
        entry = current - atr_ref * 0.2
        entry_reasons = [f"該当する買いシグナルなし。{atr_label}から見た現在値近辺のわずかな押し目を暫定的に採用"]
    if rsi is not None and rsi >= 70:
        entry -= atr_ref * 0.2
        entry_reasons.append(f"RSI({rsi:.0f})が買われすぎ水準のためやや低めに調整")
    if rsi_5m is not None and rsi_5m >= 75:
        entry -= atr_ref * 0.1
        entry_reasons.append(f"5分足RSI({rsi_5m:.0f})も過熱気味のため、ザラ場の短期的な買われすぎを加味してやや低めに調整")
    if rsi_15m is not None and rsi_15m >= 75:
        entry -= atr_ref * 0.1
        entry_reasons.append(f"15分足RSI({rsi_15m:.0f})も過熱気味のため、やや低めに調整")
    if vwap is not None and current > vwap * 1.01:
        entry_reasons.append(f"現在値はVWAP({vwap:.1f})より上（当日の平均的な出来高加重コストより高め）")

    # ---- entryは「今日、実際にその価格で約定し得たか」を保証するため、当日の実測値幅
    # （intraday_low〜intraday_high）の中に必ず収める。ATRから逆算した押し目が当日の実際の安値
    # より深い場合、一度も付いていない非現実的な価格になってしまうため、当日安値を下限とする。----
    if intraday_low is not None and entry < intraday_low:
        entry = intraday_low
        entry_reasons.append(f"当日安値({intraday_low:.1f})を下限として調整（未達水準は避ける）")
    if intraday_high is not None and entry > intraday_high:
        entry = intraday_high
        entry_reasons.append(f"当日高値({intraday_high:.1f})を上限として調整")

    # ---- 損切り単価(目安)：ATR相当(当日実測 or 日次ATR)の1倍を損切り幅の目安とする（ザラ場内で許容できる下振れ）。
    # entry確定後に算出するため、当日安値による下限調整をentryにも先に反映済み（旧実装は
    # entry未調整のままstopだけ当日安値でかさ上げしていたため、entryより高いstopが出る不具合があった）。----
    stop = entry - atr_ref
    stop_reasons = [f"{atr_label}({atr_ref:.1f})の1倍を損切り幅の目安に設定"]
    if intraday_low is not None and stop < intraday_low < entry:
        stop = intraday_low
        stop_reasons.append(f"当日安値({intraday_low:.1f})を下限目安として調整")
    # entryより低いことを必ず保証する（浅めの最小値幅を最低ラインとして確保）
    min_gap = max(entry * 0.002, 1)
    if stop >= entry:
        stop = entry - min_gap
        stop_reasons.append("損切りが購入水準を下回るよう調整")

    # ---- 利確単価(目安)：ATR相当の1.5倍（リスクリワード概ね1:1.5）を利確目安とする ----
    target = entry + atr_ref * 1.5
    target_reasons = [f"{atr_label}({atr_ref:.1f})の1.5倍（リスクリワード概ね1:1.5）を利確目安に設定"]
    if target <= entry:
        target = entry + min_gap
        target_reasons.append("利確が購入水準を上回るよう調整")
    # trading_rules.mdの利確ルール（+3〜5%、欲張らない）：ATR基準の利確目安がそれを超える場合は
    # +5%水準を上限としてキャップする（値幅の大きい銘柄でリスクリワード優先の目標が膨らみ過ぎるのを防ぐ）。
    target_cap = entry * 1.05
    if target > target_cap:
        target = target_cap
        target_reasons.append("trading_rules.mdの利確目安(+3〜5%、欲張らない)に基づき+5%水準を上限にキャップ")

    # ---- 東証の値幅制限（ストップ高・ストップ安）を必ず超えないようにする（日本株のみ）----
    if w.get("market", "JP") != "US":
        day_lo, day_hi = tse_price_limit(prev)
        if day_lo is not None and day_hi is not None:
            if entry > day_hi:
                entry = day_hi
                entry_reasons.append(f"ストップ高({day_hi:.1f})を上限として調整")
            if entry < day_lo:
                entry = day_lo
                entry_reasons.append(f"ストップ安({day_lo:.1f})を下限として調整")
            if stop < day_lo:
                stop = day_lo
                stop_reasons.append(f"ストップ安({day_lo:.1f})を下限として調整")
            if target > day_hi:
                target = day_hi
                target_reasons.append(f"ストップ高({day_hi:.1f})を上限として調整")

    # ---- 最終安全確認：ここまでの調整後も stop < entry < target の順序を必ず保証する ----
    min_gap = max(entry * 0.002, 1)
    if stop >= entry:
        stop = entry - min_gap
    if target <= entry:
        target = entry + min_gap

    # ---- trading_rules.md：エントリー適性・見送り条件のチェックリスト（自動判定できる範囲のみ）----
    is_uptrend = ma25 is not None and current > ma25
    is_pullback = is_uptrend and not high_zone  # 上昇トレンド中で直近高値からは離れている＝押し目
    above_vwap = vwap is not None and current > vwap
    market_env_bad = "不安定" in (market_env or "")
    entry_checklist = [
        {"key": "uptrend", "label": "上昇トレンド（現在値が25日線より上）", "pass": bool(is_uptrend)},
        {"key": "volume", "label": "出来高増加", "pass": bool(vol_surge)},
        {"key": "pullback", "label": "押し目（高値圏で買い急いでいない）", "pass": bool(is_pullback)},
        {"key": "vwap", "label": "VWAPより上", "pass": bool(above_vwap) if vwap is not None else None},
    ]
    avoid_checklist = [
        {"key": "thinVolume", "label": "出来高が少ない", "hit": bool(vol_thin)},
        {"key": "badMarket", "label": "地合いが悪い", "hit": bool(market_env_bad)},
        {"key": "weakSector", "label": "セクターが弱い（日次モニターのセクター順で要確認）", "hit": None},
    ]

    # ---- trading_rules_追加分ルール③：GU日に寄り付き高値を追いかけていないか ----
    chasing_gu_high = bool(gu_pct is not None and gu_pct >= 5 and intraday_high is not None
                            and current >= intraday_high * 0.995 and not pullback_formed)
    if chasing_gu_high:
        avoid_checklist.append({"key": "chasingGuHigh", "label": "GU日の寄り付き高値を追いかけている（押し目待ち推奨）", "hit": True})

    # ---- trading_rules_追加分ルール④：エントリー位置がVWAP・移動平均線から離れすぎていないか ----
    entry_ref = vwap if vwap is not None else ma25
    entry_dev_pct = (current - entry_ref) / entry_ref * 100 if entry_ref else None
    if entry_dev_pct is not None and entry_dev_pct > 2:
        avoid_checklist.append({"key": "awayFromMaVwap",
                                 "label": f"現在値がVWAP/移動平均線から+{entry_dev_pct:.1f}%乖離（高値掴みリスク、待てないなら見送り）",
                                 "hit": True})

    # ---- trading_rules_追加分ルール②：貸借倍率（kabutanスクレイピング、日本株のみ）----
    margin_ratio = _kabutan_margin_ratio(w.get("code", "")) if w.get("market", "JP") == "JP" else None
    margin_badge, margin_note = _margin_badge(margin_ratio)
    if margin_badge in ("caution", "danger"):
        avoid_checklist.append({"key": "marginRatio", "label": margin_note, "hit": True})
    if margin_badge == "danger":
        strength = max(1, strength - 1)
        entry_reasons.append(f"{margin_note}のためエントリー推奨を格下げ")

    # ---- 決算またぎルール：決算発表が近い場合、直近1週間の値動きから「またぐ／またがない」の目安を出す ----
    days_to_earnings = _days_to_earnings(tk)
    earnings_note = None
    if days_to_earnings is not None and 0 <= days_to_earnings <= 7:
        if change_1w is not None and change_1w >= 5:
            earnings_note = (f"決算まで{days_to_earnings}日。直近1週間で{change_1w:+.1f}%上昇しており、"
                              f"期待が既に織り込み済みの可能性→またがない候補")
            avoid_checklist.append({"key": "earningsPriced", "label": "決算直前で期待が織り込み済み", "hit": True})
        elif change_1w is not None and change_1w <= 0:
            earnings_note = (f"決算まで{days_to_earnings}日。直近1週間{change_1w:+.1f}%で市場の期待は低め→"
                              f"またぐ候補（自身の確信度・業界の追い風・受注等の先行指標と合わせて判断）")
        else:
            earnings_note = f"決算まで{days_to_earnings}日。またぐかどうかは方針の基準に照らして判断してください"
    elif days_to_earnings is not None and 7 < days_to_earnings <= 30:
        earnings_note = f"次回決算まで{days_to_earnings}日"

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
            "atr14": round(atr14, 2) if atr14 is not None else None,
            # 当日の分足（1分/5分/15分）からリアルタイムに算出した指標。市場時間外は取得できずNoneになる。
            "intradayHigh": round(intraday_high, 2) if intraday_high is not None else None,
            "intradayLow": round(intraday_low, 2) if intraday_low is not None else None,
            "vwap": round(vwap, 2) if vwap is not None else None,
            "rsi5m": round(rsi_5m, 1) if rsi_5m is not None else None,
            "rsi15m": round(rsi_15m, 1) if rsi_15m is not None else None,
        },
        "fundamentalNote": "・".join(fund_notes) if fund_notes else "取得できるファンダメンタルデータがありません",
        "daysToEarnings": days_to_earnings,  # 決算またぎ期待値機能：10日前からのカウントダウン表示に使う
        "tradeRules": {
            "entryChecklist": entry_checklist,
            "avoidChecklist": avoid_checklist,
            "earningsNote": earnings_note,
            "watchStatus": watch_status,
            "marginRatio": round(margin_ratio, 2) if margin_ratio is not None else None,
            "marginBadge": margin_badge,
        },
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

    def end_headers(self):
        # trade-cockpit.html等の静的配信はブラウザ側のキャッシュにより、コード修正後に
        # リロードしても古い見た目のままになることがあったため、常にキャッシュさせない。
        self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()

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
        elif self.path.startswith("/api/earnings-rating"):
            self._send_json({"ratings": _load_earnings_ratings()})
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
        elif self.path.startswith("/api/earnings-detail"):
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"[]"
            try:
                targets = json.loads(raw.decode("utf-8") or "[]")
            except Exception:
                targets = []
            print(f"[取得] 決算分析（対象 {len(targets)} 銘柄）…")
            results = [latest_earnings_detail(t.get("code", ""), t.get("name", ""))
                       for t in targets if t.get("code")]
            self._send_json({"results": results})
        elif self.path.startswith("/api/earnings-rating"):
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                body = {}
            code = str(body.get("code", "")).strip()
            stars = body.get("stars")
            if code and isinstance(stars, (int, float)) and 0 <= stars <= 5:
                ratings = _load_earnings_ratings()
                ratings[code] = {"stars": stars, "updatedAt": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")}
                _save_earnings_ratings(ratings)
                self._send_json({"ok": True, "ratings": ratings})
            else:
                self._send_json({"ok": False})
        else:
            self.send_response(404)
            self.end_headers()


def open_browser():
    time.sleep(1.5)
    webbrowser.open(f"http://localhost:{PORT}/trade-cockpit.html")


def main():
    if yf is None or feedparser is None:
        print("必要なライブラリが見つかりません。setup.bat を先に実行してください。")
        if not IS_CLOUD:
            input("Enterで終了します。")
        return
    try:
        httpd = ThreadingTCPServer((HOST, PORT), Handler)
    except OSError:
        # ポート使用中（前回のサーバーが残っている等）。親切に案内して終了。
        print("=" * 52)
        print(f" ポート {PORT} が既に使用中のため、起動できませんでした。")
        print(" すでにサーバーが起動している可能性があります。")
        print(" 前回の黒いウィンドウ（サーバー）を閉じてから、")
        print(" もう一度 start.bat を実行してください。")
        print("=" * 52)
        if not IS_CLOUD:
            input("Enterで終了します。")
        return
    if not IS_CLOUD:
        threading.Thread(target=open_browser, daemon=True).start()
    print("=" * 52)
    print(" トレード・コックピット サーバー起動中")
    if IS_CLOUD:
        print(f"  ポート {PORT} で待受中（クラウド環境）")
    else:
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
