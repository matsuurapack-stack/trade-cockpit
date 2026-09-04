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
import base64
import secrets
import calendar
import datetime
import threading
import webbrowser
import unicodedata
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
# スマホ・他PCから同じWi-Fiで開けるように、ローカル実行時も0.0.0.0（全ネットワークIF）で
# 待ち受ける（127.0.0.1固定だとPC自身からしかアクセスできなかった）。
# 自宅Wi-Fi内での利用はパスワード等のアクセス制限を付けない方針だったが、2026-09-02の
# マルチユーザー化以降はUSERS（下記）が1件でも設定されていれば常にログインが必要になる
# （[[trade-cockpit-multi-pc-access]]）。同じWi-Fi内の他端末からは誰でも見えるため、
# 公衆Wi-Fi等では使わないこと。
# 旧APP_PASSWORD（単一共有パスワード）は廃止し、USERSベースの認証に統一した
# （_authorized()参照）。
HOST = "0.0.0.0"


def _lan_ip():
    """このPCのLAN内IPアドレスを推定する（実際に通信はしない。失敗時はNone）。"""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        return None


# APIキー類はfiles/secrets.json（gitignore済み・未コミット）に置く。ファイルが無い/キー未設定
# でも動くようにし、その場合は該当機能だけ空データで諦める。
def _load_secrets():
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "secrets.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


_SECRETS = _load_secrets()
JQUANTS_API_KEY = _SECRETS.get("jquants_refresh_token", "")  # V2はAPIキー方式（x-api-keyヘッダー）
EDINET_API_KEY = _SECRETS.get("edinet_api_key", "")
# 2026-09-02 ユーザー要望「投資判断ログ」機能：日次の相場観・銘柄評価・売買記録・マイルールを
# サーバー側DBに永続化し、将来MCP/API経由でChatGPT/Claudeから読み書きできるようにする。
# DBはNeon（無料枠のPostgreSQL、スリープはしても削除されない方式）を使用。PC・Render両方から
# 同じDBに接続することでデータを一本化する。接続先はローカルはsecrets.jsonの"database_url"、
# Renderは環境変数DATABASE_URL（Renderの規約に合わせた名前）のどちらでも読めるようにする。
DATABASE_URL = os.environ.get("DATABASE_URL") or _SECRETS.get("database_url", "")
# 2026-09-02 ユーザー要望「マルチユーザー化」：従来の単一共有パスワード(APP_PASSWORD)から、
# ユーザー名ごとの個別パスワードに切り替える。{"ユーザー名": "パスワード"} の形。ローカルは
# secrets.jsonの"users"、Renderは環境変数APP_USERS（JSON文字列）のどちらでも読める。
# 空のままなら（ローカル/LAN利用時と同じく）認証なしで動作する＝後方互換。認証成功時は
# ログイン名がそのままNeon側の各テーブルのuser_id（TEXT列）になる。
try:
    USERS = json.loads(os.environ.get("APP_USERS", "")) or _SECRETS.get("users", {})
except Exception:
    USERS = _SECRETS.get("users", {})

try:
    import yfinance as yf
except ImportError:
    yf = None
try:
    import feedparser
except ImportError:
    feedparser = None
try:
    # 立花証券e支店API（登録銘柄の日本株リアルタイム時価取得用）。
    # 認証ファイル(files/e_api_authid.txt・e_api_private_key.pem)が無いPC（他PC/共有先等）
    # でも他機能に影響しないよう、未導入・未設定時は静かにNoneのまま動作させる。
    import tachibana_api
except ImportError:
    tachibana_api = None
try:
    # 投資判断ログ用DBアクセス（2026-09-02新規）。psycopg未インストール・DATABASE_URL未設定の
    # 環境でも他機能に影響しないよう、未導入時はimportだけ通してinvestment_db側の各関数が
    # 空データ/Noneを返す（investment_db.py参照）。
    import investment_db
except ImportError:
    investment_db = None

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

    _overlay_tachibana_prices(out, watchlist)
    return out


def _overlay_tachibana_prices(out, watchlist):
    """日本株について、yfinance（遅延）の現在値・高値・安値・前日終値を立花証券APIの
    実測値で上書きする。turnoverはpDV（出来高）×現在値で再計算。spark（履歴）はyfinance
    データのまま維持する（立花のスナップショットには日足履歴が無いため）。
    未接続（認証ファイル無し・ログイン失敗・通信エラー等）の場合は何もせず、
    既存のyfinance値をそのまま使う（機能低下のみで停止しない）。"""
    if tachibana_api is None:
        return
    jp_codes = [w.get("code", "") for w in watchlist if w.get("market", "JP") == "JP" and w.get("code")]
    if not jp_codes:
        return
    try:
        live = tachibana_api.get_market_price(jp_codes)
    except Exception as e:
        print("  立花証券API 時価取得失敗（yfinanceの値を継続使用）", e)
        return
    for code, v in live.items():
        if code not in out or v.get("t") is None:
            continue
        out[code]["t"] = v["t"]
        if v.get("p") is not None:
            out[code]["p"] = v["p"]
        if v.get("high") is not None:
            out[code]["high"] = v["high"]
        if v.get("low") is not None:
            out[code]["low"] = v["low"]
        if v.get("volume") is not None:
            out[code]["turnover"] = v["t"] * v["volume"]
            out[code]["volume"] = v["volume"]  # フロント側の出来高ブレイクアウト判定用
        if v.get("ask") is not None:
            out[code]["ask"] = v["ask"]
        if v.get("bid") is not None:
            out[code]["bid"] = v["bid"]
        out[code]["liveSource"] = "tachibana"


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


#  9章：登録銘柄ニュースは「決算を含むIR・適時開示」のみに絞る。GoogleニュースRSSには構造化
# カテゴリがないため、クエリ自体をIR寄りにした上で、タイトルに以下キーワードを含むものだけに
# 絞り込む代替策を取っている（完全なIR/適時開示フィードではなく、あくまでキーワードベースの近似）。
IR_KEYWORDS = [
    "決算", "上方修正", "下方修正", "業績予想", "業績修正", "自己株式", "自社株買い", "配当",
    "株式分割", "適時開示", "増資", "決算短信", "通期", "四半期", "特別損失", "特別利益",
    "新株予約権", "有価証券報告書", "開示", "IR", "本決算", "決算発表",
    "月次売上高", "月次業績", "月次",  # TSMC等が発表する月次売上高のような月次開示も拾う
]


def _is_ir_news(title):
    return any(k in title for k in IR_KEYWORDS)


def _title_mentions_name(name, title):
    """Googleニュースの検索結果はクエリ語の一部だけに一致した無関係記事（noteの個人ブログ等）も
    紛れ込むため、IRキーワードだけでなく銘柄名自体が見出しに含まれているかも確認する。
    「三菱重工業」→見出しは「三菱重工」のように、末尾の「業」を省いた略称で報じられることが
    多いため、その形も許容する（ユーザー要望：三菱重工の提携記事が拾えていなかったため
    2026-07-14追加。「重工」等の実在する短い略称に絞るため、name自体が4文字超の場合のみ対象）。"""
    t = title.lower()
    if name.lower() in t:
        return True
    if name.endswith("業") and len(name) > 4 and name[:-1].lower() in t:
        return True
    return False


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


def _tdnet_disclosures_for_date(date_str):
    """指定日(YYYYMMDD)にTDnetに開示された情報を、証券コード(4桁)をキーにした辞書（値はリスト、
    1コードに複数開示があることもある）で返す。取得失敗時は空辞書（分析全体は失敗させない方針）。"""
    by_code = {}
    try:
        html = _tdnet_fetch_page(date_str, 1)
    except Exception as e:
        print("  TDnet取得失敗", date_str, e)
        return by_code
    m = _TDNET_TOTAL_RE.search(html)
    total = int(m.group(1)) if m else 0
    pages = min(max((total + 99) // 100, 1), 10)  # 安全のため最大10ページ(1000件)まで
    htmls = [html]
    for p in range(2, pages + 1):
        try:
            htmls.append(_tdnet_fetch_page(date_str, p))
        except Exception as e:
            print("  TDnet取得失敗", date_str, p, e)
            break
    for page_html in htmls:
        for time_s, code5, href, title in _TDNET_ROW_RE.findall(page_html):
            code = code5[:-1] if len(code5) == 5 else code5
            by_code.setdefault(code, []).append({
                "time": time_s, "date": date_str,
                "title": title.strip(),
                "url": "https://www.release.tdnet.info/inbs/" + href,
            })
    return by_code


def _tdnet_today_disclosures():
    return _tdnet_disclosures_for_date(datetime.date.today().strftime("%Y%m%d"))


# 「適時開示」サブタブ用：決算・上方修正・下方修正・新株予約権発行など種類を問わず、当日を含む
# 直近10日分のTDnet開示を対象にする（本日分だけの_tdnet_today_disclosuresより広い期間）。
TDNET_DISCLOSURE_RANGE_DAYS = 10


def _tdnet_recent_disclosures(days=TDNET_DISCLOSURE_RANGE_DAYS):
    """当日を含む直近days日分のTDnet開示を証券コードごとにまとめて返す（日付混在、新しい順ではない
    ため呼び出し元でソートする）。"""
    by_code = {}
    today = datetime.date.today()
    for i in range(days):
        d = today - datetime.timedelta(days=i)
        for code, day_items in _tdnet_disclosures_for_date(d.strftime("%Y%m%d")).items():
            by_code.setdefault(code, []).extend(day_items)
    return by_code


def _tdnet_date_sort_key(date_str, time_str):
    """複数日にまたがる開示のソート用に、日付(YYYYMMDD)＋時刻(HH:MM)をUNIX時刻に変換する。"""
    try:
        jst = datetime.timezone(datetime.timedelta(hours=9))
        hh, mm = time_str.split(":")
        dt = datetime.datetime.strptime(date_str, "%Y%m%d").replace(
            hour=int(hh), minute=int(mm), tzinfo=jst)
        return dt.timestamp()
    except Exception:
        return 0


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


def _parse_earnings_numbers(text):
    """サマリー表から売上高・営業利益・純利益とその前年同期比を抜き出す（百万円ベース）。
    表のレイアウトが想定と異なる銘柄では抽出できずNoneになる。"""
    m = _EARNINGS_ROW_RE.search(text)
    if not m:
        return None
    return {
        "period": m.group(1),
        "revenue": _num(m.group(2)), "revenue_yoy": _pct(m.group(3)),
        "op": _num(m.group(4)), "op_yoy": _pct(m.group(5)),
        "net": _num(m.group(8)), "net_yoy": _pct(m.group(9)),
    }


def _summarize_earnings_text(text, nums):
    parts = [
        f"{nums['period']}実績：売上高{nums['revenue']:,.0f}百万円({nums['revenue_yoy']:+.1f}%)・"
        f"営業利益{nums['op']:,.0f}百万円({nums['op_yoy']:+.1f}%)・純利益{nums['net']:,.0f}百万円({nums['net_yoy']:+.1f}%)"
    ]

    fm = _EARNINGS_FORECAST_RE.search(text)
    if fm:
        f_rev, f_rev_yoy = _num(fm.group(1)), _pct(fm.group(2))
        f_op, f_op_yoy = _num(fm.group(3)), _pct(fm.group(4))
        progress = f"（今回までの進捗率{nums['revenue'] / f_rev * 100:.1f}%）" if f_rev else ""
        parts.append(
            f"通期会社計画：売上高{f_rev:,.0f}百万円({f_rev_yoy:+.1f}%)・"
            f"営業利益{f_op:,.0f}百万円({f_op_yoy:+.1f}%){progress}"
        )

    gm = _GUIDANCE_REVISED_RE.search(text)
    if gm:
        parts.append("業績予想は今回修正あり（要確認）" if gm.group(1) == "有" else "業績予想は据え置き")

    return "。".join(parts) + "。"


def summarize_earnings_pdf(url):
    """決算短信PDFのサマリー表から「前年同期比」「通期会社計画比の進捗率」「予想修正の有無」を
    抜き出したテンプレート文を返す。表のレイアウトが想定と異なる銘柄では抽出できずNoneになる
    （数値ベースの決定的な処理のみで、定性コメントの要約は行わない＝1段階目の実装）。"""
    text = _fetch_pdf_text(url)
    nums = _parse_earnings_numbers(text)
    return _summarize_earnings_text(text, nums) if nums else None


def _parse_earnings_detail(text, nums):
    """実績(nums)に加え、通期会社計画・進捗率・予想修正の有無を構造化して返す（百万円ベース）。
    フロント側で「通期会社計画」パネルを表として表示するための構造化データ
    （文章要約はここでは作らない＝latest_earnings_detailの画面はテーブル表示に一本化）。"""
    detail = {
        "period": nums["period"],
        "revenue": nums["revenue"], "revenueYoy": nums["revenue_yoy"],
        "op": nums["op"], "opYoy": nums["op_yoy"],
        "net": nums["net"], "netYoy": nums["net_yoy"],
        "forecastRevenue": None, "forecastRevenueYoy": None,
        "forecastOp": None, "forecastOpYoy": None,
        "forecastNet": None, "forecastNetYoy": None,
        "progressPct": None,
        "guidanceRevised": None,  # True=修正あり／False=据え置き／None=PDFから判定できず
    }
    fm = _EARNINGS_FORECAST_RE.search(text)
    if fm:
        f_rev, f_rev_yoy = _num(fm.group(1)), _pct(fm.group(2))
        f_op, f_op_yoy = _num(fm.group(3)), _pct(fm.group(4))
        # 通期予想の4項目は実績表(_EARNINGS_ROW_RE)と同じ並び（売上高・営業利益・経常利益・純利益）
        # のため、純利益はグループ7・8（経常利益の次）。
        f_net, f_net_yoy = _num(fm.group(7)), _pct(fm.group(8))
        detail["forecastRevenue"] = f_rev
        detail["forecastRevenueYoy"] = f_rev_yoy
        detail["forecastOp"] = f_op
        detail["forecastOpYoy"] = f_op_yoy
        detail["forecastNet"] = f_net
        detail["forecastNetYoy"] = f_net_yoy
        detail["progressPct"] = (nums["revenue"] / f_rev * 100) if f_rev else None
    gm = _GUIDANCE_REVISED_RE.search(text)
    if gm:
        detail["guidanceRevised"] = (gm.group(1) == "有")
    return detail


def analyze_earnings_pdf(url):
    """latest_earnings_detail用：PDFを1回だけ取得し、構造化された実績・通期会社計画・進捗率・
    予想修正の有無を返す（Noneはサマリー表のレイアウトが想定と異なり抽出できなかった場合）。"""
    text = _fetch_pdf_text(url)
    nums = _parse_earnings_numbers(text)
    if not nums:
        return None
    return _parse_earnings_detail(text, nums)


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


# 12章・銘柄分析タブ用：決算内容の悪化・増資（希薄化）の適時開示を購入判断の格下げ材料に反映する
# （ユーザー要望）。PTS（夜間取引）は無料で安定したAPIがないため自動取得はせず、注記のみ行う。
BAD_EARNINGS_LOOKBACK_DAYS = 30
DILUTION_KEYWORDS = ["第三者割当", "公募増資", "新株式発行", "株式の発行", "新株予約権付社債", "行使価額修正条項付新株予約権"]
DILUTION_DISCOUNT_PCT = 1.5  # 希薄化リスクの適時開示があった場合の単価割引の目安(%)


def _within_lookback(pubdate, days=BAD_EARNINGS_LOOKBACK_DAYS):
    try:
        pub_date = datetime.datetime.strptime(pubdate[:10], "%Y-%m-%d").date()
    except Exception:
        return False
    return (datetime.date.today() - pub_date).days <= days


EARNINGS_DISCOUNT_PER_POINT = 1.5  # 悪材料1件あたりの単価割引幅(%)の目安
EARNINGS_DISCOUNT_MAX = 4.0


def _earnings_risk(code):
    """直近BAD_EARNINGS_LOOKBACK_DAYS日以内に決算短信があれば、analyze_earnings_pdf()（決算分析
    タブと同じPDF解析）の実績・通期予想の前年比、および「下方修正」の明示的な適時開示の有無から
    「悪かった」と言えるかを判定し、(注記文, 単価割引の目安%) を返す。決算短信が無い・期間外・
    良好な内容だった場合は (None, 0)。
    （PDFの「修正の有無」フラグは上方/下方を区別しないため誤検知を避けるためあえて使わない。
    方向は見出しに「下方修正」と明示された開示があるかどうかで確認する）"""
    history = _tdnet_company_history(code)
    latest = next((h for h in history if "決算短信" in h["title"]), None)
    if not latest or not latest.get("url") or not _within_lookback(latest.get("pubdate", "")):
        return None, 0
    detail = analyze_earnings_pdf(latest["url"])
    if not detail:
        return None, 0
    bad_points = []
    if detail.get("opYoy") is not None and detail["opYoy"] < 0:
        bad_points.append(f"営業利益{detail['opYoy']:+.1f}%")
    if detail.get("netYoy") is not None and detail["netYoy"] < 0:
        bad_points.append(f"純利益{detail['netYoy']:+.1f}%")
    if detail.get("forecastOpYoy") is not None and detail["forecastOpYoy"] < 0:
        bad_points.append(f"通期営業利益予想{detail['forecastOpYoy']:+.1f}%")
    if any("下方修正" in h["title"] and _within_lookback(h.get("pubdate", "")) for h in history):
        bad_points.append("業績予想を下方修正")
    if not bad_points:
        return None, 0
    note = f"直近決算（{detail.get('period', '')}）が軟調：" + "・".join(bad_points)
    discount = min(EARNINGS_DISCOUNT_PER_POINT * len(bad_points), EARNINGS_DISCOUNT_MAX)
    return note, discount


def _dilution_flag(code):
    """直近BAD_EARNINGS_LOOKBACK_DAYS日以内に、増資・新株予約権付社債等の希薄化につながりうる
    適時開示があれば、その見出しを注記文として返す。無ければNone。"""
    for h in _tdnet_company_history(code):
        if any(k in h["title"] for k in DILUTION_KEYWORDS) and _within_lookback(h.get("pubdate", "")):
            return f"希薄化リスクのある適時開示あり：{h['title']}"
    return None


def _auto_earnings_stars(code, rsi, high_zone, low_zone, bad_earnings_note, dilution_note):
    """「決算期待値」の星（0〜5・0.5刻み）を自動算出する（旧・手動クリック評価を置き換え）。
    ①過去の上方修正/下方修正の開示回数比率（会社が自社予想をどれだけ上振れさせてきたか＝
    予想達成率の代理指標。TDnet銘柄別履歴の直近30件分が対象）②直近決算が軟調でないか
    （このモジュール内の_earnings_risk/_dilution_flagの結果を流用）③現在の株価の過熱度
    （RSI・52週高値/安値からの位置）の3点を合成する。"""
    history = _tdnet_company_history(code)
    up = sum(1 for h in history if "上方修正" in h["title"])
    down = sum(1 for h in history if "下方修正" in h["title"])
    score = (up / (up + down) * 5) if (up + down) > 0 else 2.5
    if rsi is not None:
        if rsi >= 70:
            score -= 1  # 過熱＝短期的な期待の織り込み過ぎに注意
        elif rsi <= 30:
            score += 0.5  # 売られ過ぎ＝出直りの余地
    if high_zone:
        score -= 0.5
    if low_zone:
        score += 0.5
    if bad_earnings_note:
        score -= 1
    if dilution_note:
        score -= 0.5
    score = max(0, min(5, score))
    return round(score * 2) / 2


def latest_earnings_detail(code, name):
    """指定銘柄の直近の決算短信を探し、analyze_earnings_pdf()の構造化詳細（実績・通期会社計画・
    進捗率・予想修正の有無）を添えて返す。見つからない・抽出できない場合はtitle/latestDetailが
    Noneのまま返す（フロント側で「見つかりません」「抽出できません」の文言に分岐させる）。"""
    result = {"code": code, "name": name, "title": None, "url": None, "published": None, "latestDetail": None}
    latest = next((h for h in _tdnet_company_history(code) if "決算短信" in h["title"]), None)
    if not latest:
        result["trend"] = build_earnings_trend(code)
        result["edinetReport"] = find_edinet_annual_report(code)
        return result
    result["title"] = latest["title"]
    result["url"] = latest["url"]
    result["published"] = latest["pubdate"]
    trend = build_earnings_trend(code)
    if latest["url"]:
        detail = analyze_earnings_pdf(latest["url"])
        result["latestDetail"] = detail
        # jQuantsは無料プランの遅延で直近1件が欠けやすいため、TDnetから取れた最新の実績値を
        # 「速報」として推移テーブルの末尾に合流させる（jQuants側が既にこの期を含んでいれば
        # 発表日の新しい方＝TDnet側だけ残るよう、重複時は追加しない）。
        if detail and (not trend or trend[-1].get("discDate", "") < (latest["pubdate"] or "")):
            trend.append({
                "periodType": None, "periodEnd": None, "periodLabel": detail["period"],
                "discDate": latest["pubdate"], "isLatest": True,
                "sales": detail["revenue"] * 1_000_000, "op": detail["op"] * 1_000_000, "net": detail["net"] * 1_000_000,
                "eps": None,
                "salesYoy": detail["revenueYoy"], "opYoy": detail["opYoy"], "netYoy": detail["netYoy"],
            })
            trend = trend[-8:]
    result["trend"] = trend
    result["edinetReport"] = find_edinet_annual_report(code)
    return result


# jQuants（無料プラン）の財務データサマリーで、TDnet決算短信PDFの正規表現抽出に頼らず
# 過去の開示（四半期累計・通期）の売上高/営業利益/純利益/EPSを構造化データで取得する。
# ただし無料プランは直近約12週間分が遅延で欠けるため、「直近の決算」はTDnet側が担当し、
# ここでは「その手前までの推移」の補助表示に限定する。
JQUANTS_API_BASE = "https://api.jquants.com/v2"


def fetch_jquants_summary(code):
    if not JQUANTS_API_KEY:
        return []
    try:
        req = urllib.request.Request(
            f"{JQUANTS_API_BASE}/fins/summary?code={code}",
            headers={"x-api-key": JQUANTS_API_KEY},
        )
        with urllib.request.urlopen(req, timeout=10) as res:
            data = json.loads(res.read().decode("utf-8"))
        return data.get("data", [])
    except Exception as e:
        print("  jQuants取得失敗", code, e)
        return []


def _jq_num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _yoy_pct(cur, prev):
    if cur is None or not prev:
        return None
    return (cur / prev - 1) * 100


def build_earnings_trend(code):
    """直近8件分の開示（四半期累計・通期）から、売上高・営業利益・純利益・EPSと
    前年同期比（同じ決算期区分の1年前データとの比較）を計算して返す。
    キー未設定・取得失敗時は空リスト（フロント側は「非表示」扱いにする）。"""
    rows = fetch_jquants_summary(code)
    if not rows:
        return []
    rows.sort(key=lambda r: r.get("DiscDate", ""))
    by_period = {}
    trend = []
    for r in rows:
        per_type = r.get("CurPerType", "")
        fy_end = r.get("CurFYEn", "")
        key = (per_type, fy_end)
        sales, op, net, eps = (_jq_num(r.get(k)) for k in ("Sales", "OP", "NP", "EPS"))
        prev = None
        if len(fy_end) >= 4 and fy_end[:4].isdigit():
            prev = by_period.get((per_type, str(int(fy_end[:4]) - 1) + fy_end[4:]))
        entry = {
            "periodType": per_type,
            "periodEnd": r.get("CurPerEn"),
            "discDate": r.get("DiscDate"),
            "sales": sales, "op": op, "net": net, "eps": eps,
            "salesYoy": _yoy_pct(sales, prev and prev["sales"]),
            "opYoy": _yoy_pct(op, prev and prev["op"]),
            "netYoy": _yoy_pct(net, prev and prev["net"]),
        }
        by_period[key] = entry
        trend.append(entry)
    return trend[-8:]


# EDINET（無料・プラン制限なし）から有価証券報告書（決算短信より詳しいが提出は数週間〜1ヶ月ほど
# 遅い）を探す。EDINETには銘柄コード横断の履歴検索APIが無く、日付ごとの全件一覧を返す
# documents.json を1日ずつ叩いてsecCodeで絞り込むしかないため、直近120日分（多くの企業の
# 3月期決算→6月提出に対応できる範囲）を一度だけスキャンしてsecCode→最新の有価証券報告書の
# 対応表をメモリ上にキャッシュする（半日ごとに再構築。サーバー再起動でも再構築）。
EDINET_API_BASE = "https://api.edinet-fsa.go.jp/api/v2"
EDINET_INDEX_SCAN_DAYS = 120
EDINET_INDEX_TTL_SEC = 12 * 3600
_edinet_index_cache = {"built_at": 0, "by_sec_code": {}}


def _edinet_day_documents(date_str):
    if not EDINET_API_KEY:
        return []
    try:
        req = urllib.request.Request(
            f"{EDINET_API_BASE}/documents.json?date={date_str}&type=2&Subscription-Key={EDINET_API_KEY}"
        )
        with urllib.request.urlopen(req, timeout=10) as res:
            data = json.loads(res.read().decode("utf-8"))
        return data.get("results", []) or []
    except Exception as e:
        print("  EDINET取得失敗", date_str, e)
        return []


def _build_edinet_index():
    by_sec_code = {}
    today = datetime.date.today()
    for i in range(EDINET_INDEX_SCAN_DAYS):
        d = today - datetime.timedelta(days=i)
        for r in _edinet_day_documents(d.isoformat()):
            sec_code = r.get("secCode")
            # 今日から過去へ向かって走査するため、同じsecCodeで最初に見つかったものが最新。
            if r.get("docTypeCode") == "120" and sec_code and sec_code not in by_sec_code:
                by_sec_code[sec_code] = {
                    "docID": r.get("docID"),
                    "title": r.get("docDescription"),
                    "published": r.get("submitDateTime"),
                }
    return by_sec_code


def _get_edinet_index():
    if time.time() - _edinet_index_cache["built_at"] > EDINET_INDEX_TTL_SEC:
        print(f"[取得] EDINET有価証券報告書インデックス構築中（過去{EDINET_INDEX_SCAN_DAYS}日分）…")
        _edinet_index_cache["by_sec_code"] = _build_edinet_index()
        _edinet_index_cache["built_at"] = time.time()
    return _edinet_index_cache["by_sec_code"]


def find_edinet_annual_report(code):
    """指定銘柄（4桁コード）の直近の有価証券報告書を探す。見つからない場合はNone。"""
    if not EDINET_API_KEY:
        return None
    return _get_edinet_index().get(code + "0")


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
        # TSMC等、月次売上高を発表する銘柄向けに専用クエリも足す。「決算 適時開示 業績」に
        # 「月次売上高」まで一緒に混ぜるとGoogleニュースの関連度検索が広がりすぎて無関係な
        # 記事が増えてしまうため、別クエリとして分けて結果だけ合流させる。
        candidates = (google_news(name + " 決算 適時開示 業績", 4, max_age_days=STOCK_NEWS_MAX_AGE_DAYS)
                      + google_news(name + " 月次売上高", 2, max_age_days=STOCK_NEWS_MAX_AGE_DAYS))
        ir_only = [it for it in candidates if _is_ir_news(it["title"]) and _title_mentions_name(name, it["title"])]
        # 2クエリ分を連結しているため、後半（月次売上高クエリ）の記事が新しくても件数上限で
        # 弾かれないよう、上限を適用する前に公開日時の新しい順へ並べ替える。
        ir_only.sort(key=lambda it: it.get("_ts", 0), reverse=True)
        for it in ir_only[:2]:
            items.append({**it, "code": code, "name": name})

    return _sort_and_strip(items)


def build_disclosure_news(watchlist):
    """ニュースタブ「適時開示」サブタブ用：決算・上方修正・下方修正・新株予約権発行など種類を
    問わず、登録銘柄（日本株）の当日を含む直近TDNET_DISCLOSURE_RANGE_DAYS日分の開示を全件返す
    （build_stock_newsは当日分・決算関連キーワードのみに絞っているため別関数にしている）。
    決算短信のみ東証統一フォーマットのサマリー表からの数値要約を試みる。アメリカ株はTDnet対象外
    のため、build_stock_newsと同じGoogleニュースのIRキーワード絞り込みを流用する。"""
    items = []

    jp_items = [w for w in watchlist if w.get("market", "JP") != "US"]
    us_items = [w for w in watchlist if w.get("market", "JP") == "US"]

    if jp_items:
        tdnet = _tdnet_recent_disclosures()
        for w in jp_items:
            code, name = w.get("code", ""), w.get("name", "")
            if not code or not name:
                continue
            for e in tdnet.get(code, []):
                date_str = e.get("date", "")
                published = f"{date_str[4:6]}/{date_str[6:8]} {e['time']}" if len(date_str) == 8 else e["time"]
                entry = {
                    "code": code, "name": name, "title": e["title"], "url": e["url"],
                    "source": "TDnet", "published": published,
                    "_ts": _tdnet_date_sort_key(date_str, e["time"]),
                }
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
        candidates = (google_news(name + " 決算 適時開示 業績", 4, max_age_days=STOCK_NEWS_MAX_AGE_DAYS)
                      + google_news(name + " 月次売上高", 2, max_age_days=STOCK_NEWS_MAX_AGE_DAYS))
        ir_only = [it for it in candidates if _is_ir_news(it["title"]) and _title_mentions_name(name, it["title"])]
        # 2クエリ分を連結しているため、後半（月次売上高クエリ）の記事が新しくても件数上限で
        # 弾かれないよう、上限を適用する前に公開日時の新しい順へ並べ替える。
        ir_only.sort(key=lambda it: it.get("_ts", 0), reverse=True)
        for it in ir_only[:2]:
            items.append({**it, "code": code, "name": name})

    return _sort_and_strip(items)


# ニュースタブ「登録銘柄」サブタブ用：適時開示（決算・IR関連キーワードのみ）とは別に、社名が
# そのままニュース見出しに出てくる一般ニュースを拾う（決算・IR以外の材料も見たいというニーズに
# 対応）。登録銘柄数が多い場合に検索回数が膨らむため、優先度順の上位に限定する。
STOCK_NAME_NEWS_LIMIT = 25  # 2026-07-14 ユーザー要望により15→25へ増加（表示数を増やす）
# 日本株1銘柄あたり、この件数以上NQNが取れていればGoogleニュースでの補完はしない
# （2026-08-20 ユーザー要望：広告・野球結果混入を避けるため、まず立花証券APIを優先する）。
STOCK_NAME_NEWS_NQN_MIN = 2

# 社名一致は広く拾う分、無関係な記事が紛れ込みやすい（判断材料としての価値が薄いため除外する）。
# ①Amazon「プライムデー」等のセール告知・広告・商品レビュー記事（Amazon/Microsoft/Appleのような
# 一般名詞に近い社名で特に多い）②楽天グループ（楽天イーグルス）・ソフトバンクグループ
# （ソフトバンクホークス）のように社名がプロ野球チーム名と重なる銘柄のスポーツ結果記事。
# いずれもユーザー要望（2026-07-14「プロ野球の結果やAmazon/Microsoft/Appleの広告が多い」）で追加。
STOCK_NAME_NEWS_EXCLUDE_KEYWORDS = [
    # 広告・セール・商品レビュー・お買い得情報のまとめ記事
    "セール", "プライムデー", "タイムセール", "クーポン", "割引", "％オフ", "%オフ", "ポイント還元",
    "PR", "広告", "キャンペーン", "送料無料", "福袋", "初売り", "ブラックフライデー", "サイバーマンデー",
    "レビュー", "開封", "おすすめ", "ランキング", "まとめ買い", "本日限定", "特価", "お買い得", "特別価格",
    "ベストセラー",
    # プロ野球・スポーツ結果（楽天イーグルス・ソフトバンクホークス・日本ハムファイターズ・
    # 鹿島アントラーズ等、社名とチーム名が重なるため。2026-08-21 ユーザー指摘：日本ハム(2282)・
    # 鹿島(1812)のスポーツニュース混入が残っていたため追加調査のうえ拡充。試合結果記事は見出しに
    # 「野球」「サッカー」等の一般語を含まないことが多く（例：「日本ハム・清宮虎、移籍後初登板も
    # サヨナラ負け」）、実際にヒットした見出しから頻出語を拾って追加した。
    "プロ野球", "野球", "イーグルス", "ホークス", "ファイターズ", "甲子園", "高校野球",
    "Jリーグ", "J1リーグ", "J2リーグ", "明治安田", "パ・リーグ", "セ・リーグ", "サッカー", "アントラーズ",
    "サヨナラ", "1軍", "2軍", "登板", "先発", "被安打", "ユース", "サンケイスポーツ", "FOOTBALL ZONE",
    "高校サッカードットコム",
]

# 上記キーワードでは拾いきれない、商品お買い得情報まとめを主とするアフィリエイト/SEOブログ媒体・
# スポーツ専門媒体は出典（媒体名）そのものを除外する（判断材料としての価値が薄いニュースが多いため）。
# スポーツ媒体は2026-08-21 ユーザー指摘（日本ハム・鹿島の混入）を受けて実際にヒットした
# 見出しの出典から追加（BASEBALL KING・道新スポーツ・スポニチ・サンスポ等）。
STOCK_NAME_NEWS_EXCLUDE_SOURCES = [
    "All About ニュース", "電撃ホビーウェブ", "uzurea.net", "PUNKLOID",
    "BASEBALL KING", "道新スポーツ", "スポニチ Sponichi Annex", "サンスポ", "Goal.com",
    "スポーツブル", "サッカー批評Web", "sportingnews.com", "targma.jp", "デイリースポーツ",
    "日刊スポーツ", "スポーツ報知", "東スポWEB", "Full-Count", "THE ANSWER", "SOCCER DIGEST Web",
]


def _is_promo_news(title, source=""):
    # 2026-08-22 ユーザー指摘対応：「Ｊリーグ」のように全角英字で書かれた見出しは、半角の
    # "Jリーグ"キーワードでは一致しないまま素通りしていた。NFKC正規化（全角英数→半角）で
    # 比較してから判定することで、全角/半角どちらの表記でも確実に弾けるようにする。
    norm_title = unicodedata.normalize("NFKC", title)
    if any(unicodedata.normalize("NFKC", k) in norm_title for k in STOCK_NAME_NEWS_EXCLUDE_KEYWORDS):
        return True
    if any(s == source for s in STOCK_NAME_NEWS_EXCLUDE_SOURCES):
        return True
    # 2026-08-21 ユーザー指摘対応：Yahoo!ニュース等の集約媒体はsourceが「Yahoo!ニュース」に
    # なり、実際の配信元（東スポWEB・サンケイスポーツ等）は見出し末尾に「（〇〇）」として
    # 埋め込まれるだけのため、上のsource完全一致だけでは弾けない。見出し中にスポーツ媒体名が
    # 含まれていないかも追加でチェックする。
    return any(unicodedata.normalize("NFKC", s) in norm_title for s in STOCK_NAME_NEWS_EXCLUDE_SOURCES)


# 立花証券APIのニュースヘッダー機能（NQN＝日経QUICKニュース等の実況速報）。銘柄コードでの
# 関連付けのため、社名の文字列一致に頼るGoogleニュース検索より誤ヒットが無く速報性も高い。
# カテゴリ: 100=ニュース、120=AI開示速報(決算関連)、129=AI開示速報(その他)。
# 110(AI市況状況速報)は個別銘柄との紐付けが薄いため対象外。
TACHIBANA_NEWS_CATEGORIES = ["100", "120", "129"]
TACHIBANA_NEWS_DAYS = 2  # 直近何日分を見るか（当日中心。株価同様に毎回取り直すため長すぎる範囲は不要）


def _tachibana_stock_news(jp_items):
    """登録銘柄（日本株）に銘柄コードで関連付いたNQN等の見出しを返す（build_stock_name_newsと
    同じ形式：code/name/title/url/source/published/_ts）。urlは元記事が無い速報のため空文字。
    未接続・エラー時は空リスト（Googleニュースの結果はそのまま生きる＝機能低下のみで停止しない）。"""
    if tachibana_api is None or not jp_items:
        return []
    code_to_name = {w.get("code"): w.get("name") for w in jp_items if w.get("code") and w.get("name")}
    if not code_to_name:
        return []
    jst = datetime.timezone(datetime.timedelta(hours=9))
    now = datetime.datetime.now(jst)
    today_str = now.strftime("%Y%m%d")
    date_from = (now - datetime.timedelta(days=TACHIBANA_NEWS_DAYS)).strftime("%Y%m%d")
    try:
        headlines = tachibana_api.get_news_headlines(TACHIBANA_NEWS_CATEGORIES, date_from, today_str, limit=100)
    except Exception as e:
        print("  立花証券API ニュース取得失敗（Googleニュースの結果のみ使用）", e)
        return []
    items = []
    for h in headlines:
        matched = [c for c in h["codes"] if c in code_to_name]
        if not matched:
            continue
        d, tm = h.get("date", ""), h.get("time", "")
        hhmm = f"{tm[:2]}:{tm[2:]}" if len(tm) == 4 else ""
        published = f"{d[4:6]}/{d[6:8]} {hhmm}" if len(d) == 8 and hhmm else ""
        ts = _tdnet_date_sort_key(d, hhmm) if len(d) == 8 and hhmm else 0
        for code in matched:
            items.append({"code": code, "name": code_to_name[code], "title": h["headline"], "url": "",
                           "source": "NQN", "published": published, "_ts": ts})
    return items


def build_stock_name_news(watchlist):
    """優先度順（優先→通常→様子見）に上位STOCK_NAME_NEWS_LIMIT銘柄まで、社名そのもので
    Googleニュースを検索し、見出しに社名を含むものだけを返す（IRキーワードでの絞り込みはしない）。
    セール告知等のPR記事はSTOCK_NAME_NEWS_EXCLUDE_KEYWORDSで除外する。
    社名単体の検索に加えて "site:nikkei.com" を明示的に組み合わせたクエリも実行し、結果を合流させる。
    日経新聞の記事はGoogleニュースの関連度順検索だけだと他の媒体に埋もれやすいため、業務提携等の
    一般ニュース（三菱重工の協業・フジクラ等）でも日経の記事を積極的に拾えるようにする
    （ユーザー要望：「日経のニュースは積極的に表示してほしい」2026-07-14）。

    日本株は、銘柄コードで確実に関連付けられ広告・野球結果等のノイズも混じらない立花証券API
    のNQN等を優先する。NQNの件数が少ない銘柄（小型株など報道量が少ない場合）だけ、不足分を
    Googleニュースで補う（ユーザー要望：「Yahoo!ファイナンス由来だと広告や野球結果が混じる」
    2026-08-20。完全にNQNのみにすると報道の少ない銘柄でニュース欄が空になるため、ハイブリッド
    方式を選択）。米国株はNQN対象外のため従来通りGoogleニュースのみ。"""
    order = {"優先": 0, "通常": 1, "様子見": 2}
    wl = sorted(watchlist, key=lambda w: order.get(w.get("watch", "通常"), 1))[:STOCK_NAME_NEWS_LIMIT]
    items = []

    # 先にNQNを銘柄コードごとに集計しておき、Googleニュースで補う必要があるか判定する。
    tachibana_items = _tachibana_stock_news([w for w in wl if w.get("market", "JP") != "US"])
    items += tachibana_items
    tachibana_count = {}
    for it in tachibana_items:
        tachibana_count[it["code"]] = tachibana_count.get(it["code"], 0) + 1

    for w in wl:
        name, code = w.get("name", ""), w.get("code", "")
        market = w.get("market", "JP")
        if not name:
            continue
        nqn_n = tachibana_count.get(code, 0)
        if market != "US" and nqn_n >= STOCK_NAME_NEWS_NQN_MIN:
            continue  # NQNで十分な件数が取れている銘柄はGoogleニュースを使わない（ノイズ回避）
        quota = 3 if market == "US" else max(0, STOCK_NAME_NEWS_NQN_MIN - nqn_n)
        if quota == 0:
            continue
        candidates = (google_news(name, 4, max_age_days=STOCK_NEWS_MAX_AGE_DAYS)
                      + google_news(name + " site:nikkei.com", 5, max_age_days=STOCK_NEWS_MAX_AGE_DAYS))
        matched = [it for it in candidates
                   if _title_mentions_name(name, it["title"]) and not _is_promo_news(it["title"], it.get("source", ""))]
        # 2クエリにまたがって同じ記事がヒットすることがあるため、URLで重複除去してから新しい順に整える。
        seen_urls = set()
        deduped = []
        for it in matched:
            if it["url"] in seen_urls:
                continue
            seen_urls.add(it["url"])
            deduped.append(it)
        deduped.sort(key=lambda it: it.get("_ts", 0), reverse=True)
        for it in deduped[:quota]:
            items.append({**it, "code": code, "name": name})

    return _sort_and_strip(items)


# 9-1章：国内市況・海外市況のサブタブ用にクエリを分けて取得する
# "site:nikkei.com" は日本経済新聞（nikkei.com）の記事に絞り込むGoogleニュースRSS検索クエリ。
# 本文は会員限定でも見出しはGoogleニュース経由で無料表示できるため、見出しだけでも拾えるようにする
# （ユーザー要望：「日経新聞のニュースは見出しだけでもあげれない？」「日経のニュースは積極的に
# 表示してほしい、ニュースの表示数を増やして」2026-07-14）。
MACRO_QUERIES_DOMESTIC = ["日経平均 見通し", "日銀 金融政策 決定", "ドル円 相場", "site:nikkei.com 株式市場"]
MACRO_QUERIES_OVERSEAS = ["FRB 利上げ 金利", "米国株式市場 ダウ"]


def build_macro_news():
    """マクロニュースを国内・海外に分けて返す（国内タプル, 海外タプル）。
    複数クエリの結果を連結後、公開日時の降順に並べ替えてから返す。"""
    domestic = []
    for q in MACRO_QUERIES_DOMESTIC:
        domestic.extend(google_news(q, 6))
    overseas = []
    for q in MACRO_QUERIES_OVERSEAS:
        overseas.extend(google_news(q, 6))
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


def _index_trend(symbol):
    """指数1本のトレンド（25日線に対する位置）と前日比を返す。取得失敗時はNone。"""
    try:
        h = yf.Ticker(symbol).history(period="3mo")
        closes = h["Close"].dropna().tolist()
        if len(closes) < 25:
            return None
        ma25 = _sma(closes, 25)
        current = closes[-1]
        prev = closes[-2] if len(closes) >= 2 else current
        change_pct = (current - prev) / prev * 100 if prev else None
        if current > ma25 * 1.01:
            trend = "up"
        elif current < ma25 * 0.99:
            trend = "down"
        else:
            trend = "flat"
        return {"current": current, "changePct": change_pct, "trend": trend}
    except Exception:
        return None


_TREND_LABEL = {"up": "上昇", "down": "下落", "flat": "横ばい"}


def _market_risk_score(n225, nasdaq, sox, us10y, usdjpy):
    """Trade Cockpit v2 Phase2（設計案10番）：0〜100のMarket Risk Score。日経・NASDAQ/SOX・
    US10Y・USDJPYの各トレンドに加点していく単純なルールベース（AI不使用）。0-25=LOW RISK,
    26-50=NORMAL, 51-75=HIGH RISK, 76-100=RISK OFF。取得できなかった指数は加点対象外にする
    （データ欠損を0点＝安全側に丸めない。83番：欠損はUNKNOWNとして扱う設計方針に合わせ、
    scoreはあくまで取得できた指数のみで計算する旨をnoteに残す）。"""
    score = 25
    missing = []
    if n225:
        if n225["trend"] == "down":
            score += 20
    else:
        missing.append("日経平均")
    if (nasdaq and nasdaq["trend"] == "down") or (sox and sox["trend"] == "down"):
        score += 20
    if not nasdaq and not sox:
        missing.append("NASDAQ/SOX")
    if us10y:
        if us10y["trend"] == "up":
            score += 15
    else:
        missing.append("米10年債")
    if usdjpy and usdjpy["changePct"] is not None and abs(usdjpy["changePct"]) >= 1:
        score += 10
    score = max(0, min(100, score))
    if score <= 25:
        label = "LOW RISK"
    elif score <= 50:
        label = "NORMAL"
    elif score <= 75:
        label = "HIGH RISK"
    else:
        label = "RISK OFF"
    return {"score": score, "label": label, "missing": missing}


def _market_environment():
    """動画「スマホで2億円を稼いだ天才ママ」の教え①②：個別株より先に日経平均・NASDAQ・SOX指数の
    方向を確認する。日経平均のトレンドで地合い良好/不安定/中立を判定し、NASDAQ・SOXの状況も
    一言添える。日経平均の前日比（nikkeiChangePct）は、個別銘柄との相対的な強さ（ルール⑪：
    市場全体が下がっても下がらない銘柄は強い銘柄）の判定にも使う。分析対象銘柄ごとに毎回
    取得すると重いため、build_analysis() 内で1回だけ計算して全銘柄で使い回す。
    2026-09-02（Trade Cockpit v2 Phase2）：Market Risk Score（0〜100）とMarket Condition
    （RISK ON/NEUTRAL/RISK OFF）も同じタイミングでまとめて計算する（設計案11・12番）。"""
    n225 = _index_trend("^N225")
    if not n225:
        return {"text": "相場環境：データ不足のため判定できません", "nikkeiChangePct": None, "bad": False,
                "marketRiskScore": None, "marketRiskLabel": None, "marketCondition": None}

    if n225["trend"] == "up":
        text = "地合い良好（日経平均が25日線より上で上昇トレンド）"
    elif n225["trend"] == "down":
        text = "地合い不安定（日経平均が25日線より下で下落トレンド）→ デイトレード推奨"
    else:
        text = "地合い中立（日経平均は25日線付近で横ばい）"

    nasdaq = _index_trend("^IXIC")
    sox = _index_trend("^SOX")
    us10y = _index_trend("^TNX")
    usdjpy = _index_trend("JPY=X")

    support_bits = []
    for label, idx in (("NASDAQ", nasdaq), ("SOX", sox)):
        if idx:
            support_bits.append(f"{label} {_TREND_LABEL[idx['trend']]}")
    if support_bits:
        text += "／" + "・".join(support_bits)

    risk = _market_risk_score(n225, nasdaq, sox, us10y, usdjpy)
    market_condition = "RISK OFF" if risk["score"] >= 76 else ("RISK ON" if risk["score"] <= 25 else "NEUTRAL")

    return {"text": text, "nikkeiChangePct": n225["changePct"], "bad": n225["trend"] == "down",
            "marketRiskScore": risk["score"], "marketRiskLabel": risk["label"], "marketCondition": market_condition}


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


def _volume_profile_poc(closes, volumes, lookback=60, bins=20):
    """動画「テスタさん」の教え⑧：価格帯別出来高。直近lookback日の値幅をbins分割し、
    最も出来高が集中した価格帯（POC=Point of Control）の中心値を返す。現在値がPOCより下なら
    戻り待ちの売り（上値抵抗）、上なら押し目買い（支持線）が出やすいと解釈する。"""
    n = min(len(closes), len(volumes), lookback)
    if n < 20:
        return None
    window_closes = closes[-n:]
    window_volumes = volumes[-n:]
    lo, hi = min(window_closes), max(window_closes)
    if hi <= lo:
        return None
    bin_width = (hi - lo) / bins
    bucket_vol = [0.0] * bins
    for c, v in zip(window_closes, window_volumes):
        idx = min(int((c - lo) / bin_width), bins - 1)
        bucket_vol[idx] += v
    max_idx = max(range(bins), key=lambda i: bucket_vol[i])
    return lo + bin_width * (max_idx + 0.5)


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
    分析全体は失敗させない（ニュース取得等の他の外部データ取得と同じ、失敗を許容する方針）。"""
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


def _tachibana_daily_arrays(code):
    """立花証券APIの日足履歴（分割調整済み・上場来）から closes/opens/highs/lows/volumes を作る。
    日足履歴は前営業日までの確定値のみのため、当日分は時価情報（ライブ気配）から合成して
    末尾に追加する（yfinanceのhistory()が当日分もリアルタイムに含めて返す挙動に合わせるため。
    これをしないと当日のopens[-1]/highs[-1]/lows[-1]が前営業日のままなのに終値だけ
    current_overrideで当日値に差し替わり、ギャップアップ判定・ローソク足形状の判定がずれる）。
    直近400営業日に絞る（52週高値等の計算には十分で、6000件超をそのまま扱うより軽い）。
    失敗・データ不足時はNoneを返し、呼び出し側でyfinanceにフォールバックする。"""
    if tachibana_api is None or not code:
        return None
    try:
        hist = tachibana_api.get_daily_history(code)
    except Exception as e:
        print(f"  立花証券API 日足取得失敗（{code}）。yfinanceにフォールバック", e)
        return None
    if len(hist) < 20:
        return None
    hist = hist[-400:]
    closes = [r["close"] for r in hist]
    opens = [r["open"] for r in hist]
    highs = [r["high"] for r in hist]
    lows = [r["low"] for r in hist]
    volumes = [r["volume"] for r in hist]

    jst = datetime.timezone(datetime.timedelta(hours=9))
    today_str = datetime.datetime.now(jst).strftime("%Y-%m-%d")
    if hist[-1]["date"] != today_str:
        try:
            live = tachibana_api.get_market_price([code]).get(code)
        except Exception:
            live = None
        if live and live.get("t") is not None and live.get("open") is not None:
            closes.append(live["t"])
            opens.append(live["open"])
            highs.append(live.get("high") if live.get("high") is not None else live["t"])
            lows.append(live.get("low") if live.get("low") is not None else live["t"])
            volumes.append(live.get("volume") if live.get("volume") is not None else 0)
    return closes, opens, highs, lows, volumes


# 2026-08-21 ユーザー要望：出来高ブレイクアウト判定の高値の参照期間を「直近20営業日」から
# 「直近3か月」に変更。1か月≒21営業日として3か月分=63営業日とする。
BREAKOUT_LOOKBACK_DAYS = 63


def get_breakout_levels(watchlist):
    """登録銘柄（日本株）ごとに、出来高ブレイクアウト判定に使う基準値（直近3か月高値・
    直近5日平均出来高）だけを軽量に返す。analyze_stock()と同じ定義（3か月高値、5日平均
    出来高の1.5倍）だが、RSI・ボリンジャー・PDF解析等は一切行わないため大幅に軽い。
    この基準値は日中変わらないため、フロント側は1日1回だけ呼べばよい（30秒おきの現在値更新の
    たびにここを叩く必要はない。現在値との比較はフロント側で行う）。
    戻り値：{code: {highLookback, volAvg5}}（取得失敗した銘柄は含まれない＝機械的にスキップ）。"""
    out = {}
    for w in watchlist:
        if w.get("market", "JP") == "US":
            continue  # 立花証券APIの日足はJP専用のため対象外
        code = w.get("code", "")
        if not code:
            continue
        arrays = _tachibana_daily_arrays(code)
        if not arrays:
            continue
        _closes, _opens, highs, _lows, volumes = arrays
        # 当日分は_tachibana_daily_arrays()が末尾に合成しているため、直近3か月高値・5日平均
        # 出来高は「当日を含まない」直近の確定済み日から数える（[-(N+1):-1]は当日を除いたN日分）。
        if len(highs) < BREAKOUT_LOOKBACK_DAYS + 1 or len(volumes) < 6:
            continue
        high_lookback = max(highs[-(BREAKOUT_LOOKBACK_DAYS + 1):-1])
        vol_avg5 = sum(volumes[-6:-1]) / 5
        out[code] = {"highLookback": round(high_lookback, 2), "volAvg5": round(vol_avg5, 0)}
    return out


# 2026-08-21 ユーザー要望：マスターリスト（MASTER・約277社）に無い銘柄を「手動登録」する際、
# 東証33業種のどれに当たるかをyfinanceのsector/industry（GICS準拠・英語）から推定する。
# yfinanceは業種名を東証33業種とは異なる分類・英語で返すため、キーワードで簡易マッピングする。
# 完全一致は保証できない前提の「たたき台」であり、フロント側では引き続き手動で選び直せる
# （2026-08-21 ユーザー確認済み：yfinance推定で進める。多少不正確・取得が遅い場合がある前提）。
_YF_INDUSTRY_TO_SECTOR33 = [
    # (industryに含まれていれば優先的にマッチさせるキーワード, 東証33業種)
    ("semiconductor", "電気機器"), ("consumer electronics", "電気機器"), ("computer hardware", "電気機器"),
    ("electronic", "電気機器"),
    ("software", "情報・通信業"), ("internet", "情報・通信業"), ("telecom", "情報・通信業"), ("media", "情報・通信業"),
    ("bank", "銀行業"),
    ("insurance", "保険業"),
    ("capital markets", "証券、商品先物取引業"), ("asset management", "証券、商品先物取引業"), ("securities", "証券、商品先物取引業"),
    ("credit services", "その他金融業"),
    ("auto ", "輸送用機器"), ("automobile", "輸送用機器"), ("aerospace", "輸送用機器"),
    ("apparel", "繊維製品"), ("textile", "繊維製品"),
    ("retail", "小売業"), ("department store", "小売業"), ("grocery", "小売業"), ("restaurant", "小売業"),
    ("beverage", "食料品"), ("packaged food", "食料品"), ("food", "食料品"),
    ("household", "化学"), ("chemical", "化学"),
    ("steel", "鉄鋼"),
    ("copper", "非鉄金属"), ("aluminum", "非鉄金属"), ("industrial metals", "非鉄金属"), ("mining", "鉱業"),
    ("paper", "パルプ・紙"),
    ("machinery", "機械"), ("industrial machinery", "機械"),
    ("railroad", "陸運業"), ("trucking", "陸運業"), ("freight", "陸運業"), ("logistics", "陸運業"),
    ("marine shipping", "海運業"), ("shipping", "海運業"),
    ("airline", "空運業"), ("airport", "空運業"),
    ("engineering & construction", "建設業"), ("construction", "建設業"),
    ("real estate", "不動産業"), ("reit", "不動産業"),
    ("utilities—regulated electric", "電気・ガス業"), ("utilities—regulated gas", "電気・ガス業"), ("utilit", "電気・ガス業"),
    ("oil", "石油・石炭製品"), ("gas ", "石油・石炭製品"), ("energy", "石油・石炭製品"),
    ("medical", "精密機器"), ("diagnostics", "精密機器"), ("biotechnology", "医薬品"), ("drug", "医薬品"), ("pharma", "医薬品"),
    ("rubber", "ゴム製品"), ("tire", "ゴム製品"),
    ("glass", "ガラス・土石製品"), ("cement", "ガラス・土石製品"),
]
# yfinanceのsector（大分類）だけで判定する場合のフォールバック（industryでマッチしなかった場合用）
_YF_SECTOR_TO_SECTOR33 = {
    "technology": "情報・通信業", "communication services": "情報・通信業",
    "financial services": "その他金融業", "financial": "その他金融業",
    "healthcare": "医薬品",
    "consumer cyclical": "小売業", "consumer defensive": "食料品",
    "industrials": "機械", "basic materials": "化学",
    "energy": "石油・石炭製品", "utilities": "電気・ガス業", "real estate": "不動産業",
}


def guess_sector(code, market):
    """MASTERに無い銘柄の東証33業種をyfinanceから推定する（失敗時はNone）。"""
    if yf is None or not code:
        return None
    try:
        sym = code if market == "US" else code + ".T"
        info = yf.Ticker(sym).info or {}
        industry = str(info.get("industry") or "").lower()
        sector = str(info.get("sector") or "").lower()
        for kw, s33 in _YF_INDUSTRY_TO_SECTOR33:
            if kw in industry:
                return s33
        if sector in _YF_SECTOR_TO_SECTOR33:
            return _YF_SECTOR_TO_SECTOR33[sector]
    except Exception as e:
        print(f"  業種推定失敗（{code}）", e)
    return None


# 2026-08-22 ユーザー要望：「日本市場」タブの銘柄検索を、日経225中心の手動キュレーションリスト
# （MASTER、約277社）だけでなく東証上場銘柄全体（約4400銘柄。ETF・投資信託等を含む）に広げる。
# 立花証券APIの株式銘柄マスタ（sGyousyuCode）は東証33業種を使っているが、名称の区切り記号や
# 表記が本アプリのSECTOR_CHOICES（trade-cockpit.html）とわずかに異なる（例:「石油石炭製品」
# vs「石油・石炭製品」）ため、コードで確実にマッピングする。9999（ETF・投資信託等、個別企業
# ではない）は対応先が無いためマッピングしない＝呼び出し側で除外する。
GYOSHU_CODE_TO_SECTOR33 = {
    "0050": "水産・農林業", "1050": "鉱業", "2050": "建設業", "3050": "食料品",
    "3100": "繊維製品", "3150": "パルプ・紙", "3200": "化学", "3250": "医薬品",
    "3300": "石油・石炭製品", "3350": "ゴム製品", "3400": "ガラス・土石製品", "3450": "鉄鋼",
    "3500": "非鉄金属", "3550": "金属製品", "3600": "機械", "3650": "電気機器",
    "3700": "輸送用機器", "3750": "精密機器", "3800": "その他製品", "4050": "電気・ガス業",
    "5050": "陸運業", "5100": "海運業", "5150": "空運業", "5200": "倉庫・運輸関連業",
    "5250": "情報・通信業", "6050": "卸売業", "6100": "小売業", "7050": "銀行業",
    "7100": "証券、商品先物取引業", "7150": "保険業", "7200": "その他金融業",
    "8050": "不動産業", "9050": "サービス業",
}

JP_ISSUE_MASTER_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jp_issue_master_cache.json")
_jp_issue_master_cache = None  # プロセス内メモリキャッシュ（起動後1回取得すればよい）


def get_jp_issue_master():
    """東証上場の株式銘柄マスタ全件を{code:{name,kana,sector}}で返す（個別企業のみ。ETF・投資信託
    等はsGyousyuCode=9999でマッピング先が無いため除外）。プロセス内メモリに載ったらそれを使い回し、
    無ければ当日分のファイルキャッシュ（JP_ISSUE_MASTER_CACHE_PATH）を見る。それも無い/日付が
    古い場合のみ立花証券APIに問い合わせる（4000件超のため数秒かかる。ユーザー確認済み：
    サーバー起動時に1回取得してファイルにキャッシュする方針）。取得失敗時は空dict。"""
    global _jp_issue_master_cache
    if _jp_issue_master_cache is not None:
        return _jp_issue_master_cache
    today = datetime.date.today().isoformat()
    try:
        with open(JP_ISSUE_MASTER_CACHE_PATH, encoding="utf-8") as f:
            cached = json.load(f)
        if cached.get("date") == today and cached.get("items"):
            _jp_issue_master_cache = cached["items"]
            print(f"[取得] 銘柄マスタ（ファイルキャッシュ・{len(_jp_issue_master_cache)}件）")
            return _jp_issue_master_cache
    except Exception:
        pass
    out = {}
    if tachibana_api is not None:
        try:
            print("[取得] 銘柄マスタ（立花証券API・全銘柄）…")
            raw = tachibana_api.get_issue_master_kabu()
            for r in raw:
                sector = GYOSHU_CODE_TO_SECTOR33.get(r["gyoshuCode"])
                if not sector:
                    continue  # ETF・投資信託等（9999）は対象外
                out[r["code"]] = {"name": r["name"], "kana": r["kana"], "sector": sector}
        except Exception as e:
            print("  銘柄マスタ取得失敗", e)
    _jp_issue_master_cache = out
    if out:
        try:
            with open(JP_ISSUE_MASTER_CACHE_PATH, "w", encoding="utf-8") as f:
                json.dump({"date": today, "items": out}, f, ensure_ascii=False)
        except Exception as e:
            print("  銘柄マスタキャッシュ保存失敗", e)
    return out


def analyze_stock(w, market_env=None):
    """12-1章・technical_analysis_rules.md：ローソク足パターン・移動平均線の並び／クロス・
    ボリンジャーバンド・RCI・複合底打ち条件などから買い/売りシグナルを判定し、その中から
    最有力のシグナルに基づいて購入・損切り・利確の目安単価と算出根拠を返す。
    値はあくまで目安であり断定的な推奨ではない(12-2章の方針)。"""
    if not isinstance(market_env, dict):
        market_env = {"text": market_env or "", "nikkeiChangePct": None, "bad": False}
    code = w.get("code", "")
    # v3-7（押し目エントリー価格帯の見直し）：フロント（enrichWatchRow）が既に算出済みのPrimary
    # Status／Action Status。サーバー側で同じ判定を再実装しない（二重ロジックを避ける）ため、
    # フロントから渡された値をそのまま受け取るだけ（未送信時はNoneのまま＝通常銘柄扱い）。
    client_primary_status = w.get("primaryStatus")
    client_action_status = w.get("actionStatus")
    sym = _yf_symbol(w)
    tk = yf.Ticker(sym)  # 分足・決算カレンダー・ファンダメンタルは相当データが無いためyfinanceのまま使用

    # 日足（移動平均・RSI・ボリンジャー等、分析の中核部分）は日本株なら立花証券APIを優先する
    # （yfinanceのレート制限リスクを避けるため。2026-08-20ユーザー要望）。取得できない場合のみ
    # yfinanceにフォールバックする。
    arrays = _tachibana_daily_arrays(code) if w.get("market", "JP") != "US" else None
    if arrays:
        closes, opens, highs, lows, volumes = arrays
    else:
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

    # ---- エントリーのタイミングルール（最新版）：判断の基準線として使う「5分足短期線」（5本SMA）と
    # 「5分足直近安値」。損切り位置を先に決めるルール・初押し判定の両方で使い回す。----
    ma5_short = _sma(m5["closes"], 5) if m5 and len(m5["closes"]) >= 5 else None
    recent_low_5m = min(m5["lows"][-3:]) if m5 and len(m5["lows"]) >= 3 else None

    # ---- trading_rules_追加分ルール①③：GU（ギャップアップ）率と、寄り付き高値からの押し目形成有無。
    # 「様子見(初押し待ち)」判定と「寄り付き高値を追わない」警告の両方でこの2つを使う。----
    today_open = opens[-1] if opens else None
    gu_pct = (today_open - prev) / prev * 100 if (today_open is not None and prev) else None
    elapsed_minutes = len(m1["closes"]) if m1 else None  # 1分足の本数を経過分数の目安として使う
    pullback_formed = False
    if m1 and len(m1["closes"]) >= 2:
        peak = max(m1["closes"])
        pullback_formed = m1["closes"][-1] <= peak * 0.997  # 高値から0.3%以上の押し目

    # ---- 動画「テスタさん」の教え①②：移動平均線は「その期間に買った投資家の平均取得価格」として
    # 見る。5日線（数日〜1週間の短期）・25日線（約1カ月、短期・スイングで最重視）・75日線
    # （数カ月の大きなトレンド）の3本を日足ベースで使う。5日線はma25_s等と同じ日足の並びで、
    # 5分足の「ma5_short」（ザラ場のエントリー判定用）とは別物なので混同しないこと。----
    ma5_s = _sma_series(closes, 5)
    ma25_s, ma75_s, ma100_s = _sma_series(closes, 25), _sma_series(closes, 75), _sma_series(closes, 100)
    ma5_daily, ma25, ma75, ma100 = ma5_s[-1], ma25_s[-1], ma75_s[-1], ma100_s[-1]
    rsi = _rsi(closes, 14)
    bb_mid, bb_upper2, bb_lower2 = _bollinger(closes, 20, 2)
    _, bb_upper3, bb_lower3 = _bollinger(closes, 20, 3)
    rci26 = _rci(closes, 26)
    lookback = min(60, n)
    support = min(closes[-lookback:])
    resistance = max(closes[-lookback:])
    volume_profile_poc = _volume_profile_poc(closes, volumes)
    high52w = max(highs[-min(252, len(highs)):]) if highs else current
    low52w = min(lows[-min(252, len(lows)):]) if lows else current
    vol_avg5 = sum(volumes[-6:-1]) / 5 if len(volumes) >= 6 else None
    vol_surge = vol_avg5 is not None and volumes[-1] > vol_avg5 * 1.5
    vol_thin = vol_avg5 is not None and volumes[-1] < vol_avg5 * 0.7
    change_1w = (current - closes[-6]) / closes[-6] * 100 if n >= 6 and closes[-6] else None

    # ---- ユーザー要望(2026-08-20)：「購入単価（目安）」は下で拾う（押し目）のではなく、上昇
    # トレンドで出来高を伴って直近高値を上抜けた水準を採用する。直近3か月高値（当日を含まない。
    # 2026-08-21：参照期間を20営業日→3か月＝BREAKOUT_LOOKBACK_DAYS営業日に変更）を、直近5日
    # 平均の1.5倍以上の出来高（既存vol_surgeと同一基準）を伴って上抜けていればブレイクアウト成立
    # とし、その高値をentryに採用する（後段で最終的に上書き。既存の初押し等の細かいエントリー
    # 判定はそのまま残し、ブレイクアウト成立時だけ優先表示する）。----
    breakout_lookback_high = max(highs[-(BREAKOUT_LOOKBACK_DAYS + 1):-1]) if len(highs) >= BREAKOUT_LOOKBACK_DAYS + 1 else None
    breakout_confirmed = bool(breakout_lookback_high is not None and vol_surge and current > breakout_lookback_high)
    vol_ratio_for_breakout = (volumes[-1] / vol_avg5) if (vol_avg5 and breakout_confirmed) else None

    # ---- trading_rules_追加分ルール①：好決算日等の様子見ルール。5%以上のGU or 出来高急増で、
    # 寄り付きから60分未満・かつ押し目がまだ形成されていなければ「様子見中」とする。----
    watch_status = None
    if m1 and elapsed_minutes is not None and elapsed_minutes < 60 and not pullback_formed:
        if (gu_pct is not None and gu_pct >= 5) or vol_surge:
            watch_status = "様子見中(初押し待ち)"

    low_zone = current <= high52w * 0.8   # 条件C等：52週高値の-20%以下＝安値圏
    high_zone = current >= high52w * 0.95  # 条件B等：52週高値の-5%以内＝高値圏

    # ---- エントリーのタイミングルール（最新版）：①銘柄の強さ（上昇トレンド・出来高急増・高値圏維持、
    # 地合い）が揃っていればAランクとし、「押し目を待つ」のではなく「押しても崩れないことを確認して
    # 買う」方針に切り替える。セクター全体の強さ・板の厚みはyfinanceで自動取得できないため、
    # ここでは自動判定できる範囲（トレンド・出来高・高値圏・地合い）のみでAランクを判定し、
    # セクター・板については後述のentryTimingChecklistで手動確認を促す。----
    is_uptrend_early = ma25 is not None and current > ma25
    market_env_bad = bool(market_env.get("bad"))
    a_rank_setup = bool(is_uptrend_early and vol_surge and high_zone and not market_env_bad)

    # ---- 動画「スマホで2億円を稼いだ天才ママ」の教え⑪：市場全体が下がっても下がらない銘柄は
    # 「強い銘柄」、市場が上がっているのに売られている銘柄は「弱い銘柄」と判断する。日経平均の
    # 前日比（market_env、build_analysis()内で1回だけ取得）と当銘柄の前日比を比較する。----
    nikkei_chg = market_env.get("nikkeiChangePct")
    relative_strength_note = None
    if nikkei_chg is not None:
        if nikkei_chg <= -0.3 and change_pct >= 0:
            relative_strength_note = (f"日経平均{nikkei_chg:+.1f}%に対し当銘柄は{change_pct:+.1f}%＝"
                                       f"地合いが悪い中でも下がらない強い銘柄")
        elif nikkei_chg >= 0.3 and change_pct <= -1:
            relative_strength_note = (f"日経平均{nikkei_chg:+.1f}%に対し当銘柄は{change_pct:+.1f}%＝"
                                       f"地合いが良い中で売られている弱い銘柄")

    # ---- ②③：Aランクの銘柄では「初押し」（上昇開始後、初めて5分足短期線付近まで押し・
    # 出来高が減らない）と「高値ブレイク」（高値更新・出来高増加・ブレイク後もすぐ戻されない）
    # の2パターンだけを最有力エントリーとして狙う（★5）。「押し目待ち症候群」対策として、
    # これらが揃っていれば「もっと安く」を待たず100点を待たない。----
    entry_pattern = None
    if a_rank_setup:
        if (pullback_formed and not vol_thin and ma5_short is not None
                and current >= ma5_short * 0.995):
            entry_pattern = {"key": "hatsuoshi", "label": "初押し（上昇開始後、初めて5分足短期線付近まで押し・出来高減らず）"}
        elif intraday_high is not None and current >= intraday_high * 0.998:
            entry_pattern = {"key": "takaneBreak", "label": "高値ブレイク（高値更新・出来高増加・ブレイク後もすぐ戻されない）"}

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

    # ---- 動画「1_UnOn0ayww」の教え⑤：上放れ並び赤（窓を開けて上昇・陽線が並ぶ・さらに上放れる、
    # 強い買い資金が継続して入っているサイン）。直近3本が陽線で終値が切り上がり、いずれかの日に
    # 窓（ギャップアップ）を伴い、出来高も増えていることを条件とする。----
    uwabanare_narabe_aka = False
    if n >= 4:
        last3_bull = all(closes[i] > opens[i] for i in range(-3, 0))
        rising_closes = closes[-1] > closes[-2] > closes[-3]
        gapped_up = opens[-1] > closes[-2] or opens[-2] > closes[-3]
        if last3_bull and rising_closes and gapped_up and vol_surge:
            uwabanare_narabe_aka = True
            buy_signals.append({"key": "uwabanareNarabeAka",
                                 "label": "上放れ並び赤（陽線が並び窓を開けて上放れ、強い買い資金が継続して入っている）",
                                 "price": current})

    # ---- 動画「1_UnOn0ayww」の教え⑩：下落途中ではなく、売りが一巡してから買う。当日大きく
    # 下げた銘柄が、直近の1分足で安値を更新しなくなった＝売り圧力が一段落した兆候として捉える。----
    dip_stabilized = False
    if m1 and len(m1["lows"]) >= 4 and change_pct is not None and change_pct <= -1.5:
        recent_lows = m1["lows"][-4:]
        session_low = min(m1["lows"])
        if recent_lows[-1] >= min(recent_lows[:-1]) and recent_lows[-1] > session_low * 1.001:
            dip_stabilized = True
            buy_signals.append({"key": "dipStabilized",
                                 "label": "下落が一巡し、直近の1分足で安値を更新していない（売り一巡・戻り狙いの目安）",
                                 "price": current})

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
            # ---- 動画「1_UnOn0ayww」の教え⑥：株価が25日線に何度も接近するとトレンド転換しやすく、
            # 特に3回目の接近は要警戒。直近20日で終値が25日線の±1%以内に入った回数を数える。----
            approach_count = sum(
                1 for i in range(-min(20, n), 0)
                if ma25_s[i] is not None and abs(closes[i] - ma25_s[i]) / ma25_s[i] <= 0.01
            )
            if approach_count >= 3:
                panpakapan_third_touch = approach_count
            else:
                panpakapan_third_touch = None
        else:
            panpakapan_third_touch = None
    else:
        panpakapan_third_touch = None

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

    # ---- 動画「スマホで2億円を稼いだ天才ママ」の教え⑥⑦（キーエンス型の反発）：陰線が続き
    # -2σを下回っていた銘柄が、-2σを上に抜け返し、出来高も伴う＝反転の兆候を確認してからの
    # 逆張り。前足がバンド内（現在の-2σ基準の近似）に沈んでいて、直近足で上に抜け返した形を見る。----
    bb2_rebound = bool(bb_lower2 is not None and n >= 2
                        and closes[-2] <= bb_lower2 and current > bb_lower2 and vol_surge)
    if bb2_rebound:
        buy_signals.append({"key": "bb2Rebound",
                             "label": f"ボリンジャーバンド-2σ（{bb_lower2:.1f}）を上に抜け返し、出来高も伴う反発（反転確認後の打診買い候補）",
                             "price": bb_lower2})

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

    # ---- 動画「テスタさん」の教え⑦⑨：過去に例のない大商いを伴って急落し、5日・25日・75日線を
    # 一気に割った場合は「戻りを期待しない」水準として最も警戒する。その価格帯で買った投資家の
    # 大部分が含み損になり、戻っても売りが出やすいため。ニュースの有無は問わず、株価と出来高の
    # 変化自体を根拠にする。----
    volume_extreme = bool(len(volumes) >= 61 and volumes[-1] > max(volumes[-61:-1]) * 1.2)
    broke_5d = bool(ma5_daily is not None and current < ma5_daily)
    broke_25d = bool(ma25 is not None and current < ma25)
    broke_75d = bool(ma75 is not None and current < ma75)
    catastrophic_volume_crash = bool(volume_extreme and change_pct is not None and change_pct <= -5
                                      and broke_5d and broke_25d and broke_75d)
    if catastrophic_volume_crash:
        sell_signals.append({"key": "catastrophicVolumeCrash",
                              "label": "過去に例のない大商いを伴う急落で5日・25日・75日線を一気に割った（戻りを期待しない水準）",
                              "price": current})

    # ---- 動画「テスタさん」の教え⑥：出来高を伴わない上昇は一時的な可能性を疑う ----
    thin_volume_rise = bool(is_bull and vol_thin)

    # ---- 強度（★1〜5）：最有力シグナルの種類で判定 ----
    signal_keys = {s["key"] for s in buy_signals}
    if {"panpakapan", "uwabanareNarabeAka"} & signal_keys:
        strength = 5
    elif "compoundBottom" in signal_keys and bottom_count >= 3:
        strength = 4
    elif {"goldenCross", "bb3", "bb2Rebound", "dipStabilized", "compoundBottom"} & signal_keys:
        strength = 3
    elif buy_signals:
        strength = 2
    else:
        strength = 1
    # ---- エントリーのタイミングルール（最新版）②③：「初押し」「高値ブレイク」は最も期待値が
    # 高いポイント（★5）として、他のシグナル判定より優先して星評価に反映する。----
    if entry_pattern:
        strength = 5
    # ---- 動画「1_UnOn0ayww」の教え⑥：パンパカパン中に25日線への接近が3回目以降なら、
    # トレンド転換の警戒サインとして星評価を1段階格下げする。----
    if panpakapan_third_touch:
        strength = max(1, strength - 1)
    # ---- 動画「テスタさん」の教え⑦⑨：過去に例のない大商いを伴う急落で主要移動平均線を
    # 一気に割った銘柄は、他のシグナルの強さに関わらず最も低い★1まで格下げする（戻りを期待しない）。----
    if catastrophic_volume_crash:
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
            "marketCap": info.get("marketCap"),
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

    # ---- v3-8 Step3（ポジション管理ロジック再設計）：「大陰線」「ギャップ失敗」の軽量フラグ。
    # 新規のAPI呼び出しは行わず、この関数内で既に取得済みの当日始値(o)/前日終値(prev)/高値(hi)/
    # 安値(lo)/現在値(current)/ATR(atr_ref)/前日比(change_pct)のみで判定する。ポジション管理
    # （Step5でDAY/SWING別のチャート崩れ判定の補助材料として使用予定）専用の指標で、
    # 銘柄分析タブのBUYシグナル判定（long_bull_top等）には影響しない独立フィールド。
    # 大陰線：実体(body)が値幅(rng)の6割以上を占める陰線で、終値が安値付近（下位25%）まで
    # 押し込まれ、ATR比でも十分な大きさがあり、前日終値比でも明確なマイナス（-3%以下）。
    long_bearish_candle = bool(is_bear and body >= rng * 0.6 and c <= lo + rng * 0.25
                                and body >= atr_ref * 0.5 and change_pct is not None and change_pct <= -3)
    # ギャップ失敗：寄り付きで2%以上の上放れ（gu_pct、既存のルール①と同じ算出値）があったにも
    # かかわらず、その後失速して前日終値を割り込むまで戻された＝始値の強さが最後まで持たなかった状態。
    gap_up_failure = bool(gu_pct is not None and gu_pct >= 2 and prev is not None and current <= prev)

    # trading_rules.mdのチャート確認優先順位（出来高→移動平均線→VWAP→ボリンジャーバンド→RSI）に合わせ、
    # 複数シグナルが同時点灯した場合はこの順で「最有力の根拠」を選ぶ（VWAP/RSIは単独の買いシグナルを
    # 持たず、entry調整の理由として別途entry_reasonsに追記される）。
    priority = ["volBull", "volShadow", "uwabanareNarabeAka", "panpakapan", "goldenCross", "bb3", "bb2Rebound",
                "dipStabilized", "compoundBottom", "dojiLow", "haramiLow"]
    primary = None
    for key in priority:
        primary = next((s for s in buy_signals if s["key"] == key), None)
        if primary:
            break

    atr_label = "当日1分足の実測値幅" if used_intraday_range else "当日のATR"
    # 現在値が既に当日VWAPより下＝ザラ場内で一定の押し目が入っている状態。この状態からさらに
    # 当日値幅ベースの押し目を満額差し引くと、ボラティリティの大きい銘柄ほど二重に保守的な
    # （現在値からかけ離れた）水準になってしまうため、その場合は割引係数を半分に弱める。
    already_pulled_back = vwap is not None and current < vwap
    pullback_factor = 0.5 if already_pulled_back else 1.0
    # ---- エントリーのタイミングルール（最新版）②⑤：「初押し」「高値ブレイク」に該当するAランクの
    # 好機では「もっと安く」を待たず、押し目の深追いをやめて現在値に近い水準（80点のタイミング）を
    # 採用する（100点を待って置いていかれることを避けるルール）。----
    if entry_pattern:
        pullback_factor *= 0.4
    if primary:
        entry = current - atr_ref * 0.3 * pullback_factor
        entry_reasons = [f"{primary['label']}が点灯。{atr_label}({atr_ref:.1f})から見た現実的な押し目水準として{entry:.1f}を採用"]
    else:
        entry = current - atr_ref * 0.2 * pullback_factor
        entry_reasons = [f"該当する買いシグナルなし。{atr_label}から見た現在値近辺のわずかな押し目を暫定的に採用"]
    if already_pulled_back:
        entry_reasons.append(f"現在値が当日VWAP({vwap:.1f})より下＝ザラ場内で既に押し目が入っているため、追加の押し目調整を弱めて算出")
    if entry_pattern:
        entry_reasons.append(f"{entry_pattern['label']}のAランクの好機のため、押し目を深追いせず現在値に近い水準を採用（100株から）")
    if rsi is not None and rsi >= 70:
        entry -= atr_ref * 0.2 * pullback_factor
        entry_reasons.append(f"RSI({rsi:.0f})が買われすぎ水準のためやや低めに調整")
    if rsi_5m is not None and rsi_5m >= 75:
        entry -= atr_ref * 0.1 * pullback_factor
        entry_reasons.append(f"5分足RSI({rsi_5m:.0f})も過熱気味のため、ザラ場の短期的な買われすぎを加味してやや低めに調整")
    if rsi_15m is not None and rsi_15m >= 75:
        entry -= atr_ref * 0.1 * pullback_factor
        entry_reasons.append(f"15分足RSI({rsi_15m:.0f})も過熱気味のため、やや低めに調整")
    if vwap is not None and current > vwap * 1.01:
        entry_reasons.append(f"現在値はVWAP({vwap:.1f})より上（当日の平均的な出来高加重コストより高め）")

    # ---- ボラティリティの大きい銘柄では当日値幅ベースの押し目が現在値から離れすぎることがあるため、
    # 現在値の-3%を下限として、押し目調整が行き過ぎないようキャップする（決算・増資による追加の
    # 割引は、これとは別の理由に基づく調整のためこのキャップの対象外＝後段で別途適用）。----
    entry_floor = current * 0.97
    if entry < entry_floor:
        entry = entry_floor
        entry_reasons.append("現在値からの押し目幅が大きくなりすぎないよう、現在値の-3%を下限として調整")

    # ---- entryは「今日、実際にその価格で約定し得たか」を保証するため、当日の実測値幅
    # （intraday_low〜intraday_high）の中に必ず収める。ATRから逆算した押し目が当日の実際の安値
    # より深い場合、一度も付いていない非現実的な価格になってしまうため、当日安値を下限とする。----
    if intraday_low is not None and entry < intraday_low:
        entry = intraday_low
        entry_reasons.append(f"当日安値({intraday_low:.1f})を下限として調整（未達水準は避ける）")
    if intraday_high is not None and entry > intraday_high:
        entry = intraday_high
        entry_reasons.append(f"当日高値({intraday_high:.1f})を上限として調整")

    # ---- 決算内容の悪化・増資（希薄化）を単価にも反映する（ユーザー要望）。ここで下げたentryを
    # 元にstop/targetも算出されるため、以降の計算すべてに反映される。市場が開いている間（当日の
    # 分足m1が取得できている間）はチャート・出来高で悪材料がどれだけ株価に織り込まれたかを見て
    # 割引幅を調整し（既に大きく下げていれば二重に織り込まない）、開いていない間は直近終値・
    # 決算内容だけに基づいて機械的に割引く。PTS（夜間取引）は自動取得非対応のため対象外。----
    bad_earnings_note, earnings_discount = (None, 0)
    dilution_note = None
    if w.get("market", "JP") != "US" and code:
        bad_earnings_note, earnings_discount = _earnings_risk(code)
        dilution_note = _dilution_flag(code)
    dilution_discount = DILUTION_DISCOUNT_PCT if dilution_note else 0
    total_discount = min(earnings_discount + dilution_discount, 6.0)
    if total_discount > 0:
        market_open = bool(m1)
        if market_open:
            already_priced_in = change_pct is not None and change_pct <= -3
            effective_discount = total_discount * (0.4 if already_priced_in else 1.0)
            basis_note = "当日の下落で概ね織り込み済みのため圧縮" if already_priced_in else "当日の値動きにまだ十分反映されていない可能性を考慮"
        else:
            effective_discount = total_discount
            basis_note = "市場時間外のため直近終値・決算内容に基づき算出"
        entry = entry * (1 - effective_discount / 100)
        entry_reasons.append(f"決算・増資の材料を反映し目安を-{effective_discount:.1f}%引き下げ（{basis_note}）")

    # ---- ブレイクアウト成立時は、上記の押し目ベースの算出を上書きし、直近高値の上抜け水準を
    # 目安値として採用する（ユーザー要望2026-08-20：「下で拾う単価はダメ。上昇トレンドで入る」）。
    # stop/targetはこのentryを基準に後段で算出されるため、以降の計算すべてに一貫して反映される。----
    if breakout_confirmed:
        entry = breakout_lookback_high
        entry_reasons = [
            f"直近3か月高値({breakout_lookback_high:.1f})を、5日平均出来高の{vol_ratio_for_breakout:.1f}倍の"
            f"出来高を伴って上抜け。押し目を待たず、上昇トレンドのブレイクアウト水準を目安値に採用。"
        ]
        entry_pattern = {
            "key": "breakoutLookbackHigh",
            "label": f"出来高ブレイクアウト（直近3か月高値{breakout_lookback_high:.1f}を出来高{vol_ratio_for_breakout:.1f}倍で上抜け）",
        }

    # ---- v3-7（押し目エントリー価格帯の見直し）：「押し目＝大きく落ちた価格」ではなく「上昇
    # トレンドを壊さない浅い押し目」と定義し直す。優先順位（①VWAP付近 ②直近ブレイク水準
    # ③前日高値 ④当日押し安値 ⑤短期支持線 ⑥ATR補正）で、現在値未満・かつ乖離が大きすぎない
    # 候補を順に探す（ATRは他に根拠がない時の最終手段に格下げ）。
    # ACTIVE_BREAK/HOT×ENTRY_READYの銘柄（client_primary_status/client_action_status、フロントの
    # enrichWatchRowと同じ判定をそのまま受け取るだけ＝二重ロジックにしない）は0.5〜2.5%の浅い
    # ゾーンのみを候補として許容し、それより深い候補しかなければ「押し目候補なし」とする。
    # 通常銘柄は0〜4%を許容範囲とし、-4%を超える場合は「ここまで落ちたらもう入れない」価格を
    # 押し目として出さず、深い調整待ち／トレンド再確認ゾーンという定性的な表示にする。
    # 既存のentry（stop/target/株数目安等、多数の計算がこれに依存）には影響させず、
    # 表示専用の新フィールドpullbackEntryとして別途返す。
    prev_day_high = highs[-2] if len(highs) >= 2 else None
    short_support = ma25  # 「その期間に買った投資家の平均取得価格」という既存解釈を短期支持線として流用（新規計算なし）
    pullback_candidates = []
    if vwap is not None and current > vwap:
        pullback_candidates.append(("VWAP付近", vwap))
    if breakout_lookback_high is not None and current > breakout_lookback_high:
        pullback_candidates.append(("直近ブレイク水準", breakout_lookback_high))
    if prev_day_high is not None and current > prev_day_high:
        pullback_candidates.append(("前日高値", prev_day_high))
    if intraday_low is not None and current > intraday_low:
        pullback_candidates.append(("当日押し安値", intraday_low))
    if short_support is not None and current > short_support:
        pullback_candidates.append(("短期支持線(25日線)", short_support))

    is_strong_ready = client_primary_status in ("ACTIVE_BREAK", "HOT") and client_action_status == "ENTRY_READY"
    min_dev, max_dev = (0.5, 2.5) if is_strong_ready else (0.0, 4.0)

    pb_basis, pb_price = None, None
    for basis, price in pullback_candidates:
        dev = (current - price) / current * 100 if current else 0
        if min_dev <= dev <= max_dev:
            pb_basis, pb_price = basis, price
            break
    if pb_price is None and not is_strong_ready:
        # ⑥ATR補正：優先度①〜⑤に使える候補が無い通常銘柄だけの最終手段（強い銘柄では使わず
        # 「候補なし」を優先＝ATRで無理に浅い数字を作らない）。
        atr_dev_price = current - atr_ref * 0.3
        dev = (current - atr_dev_price) / current * 100 if current else 0
        if min_dev <= dev <= max_dev:
            pb_basis, pb_price = "ATR補正", atr_dev_price

    if pb_price is not None:
        pb_dev_pct = (current - pb_price) / current * 100
        if pb_dev_pct <= 1:
            pb_zone_label = "浅い押し目"
        elif pb_dev_pct <= 2.5:
            pb_zone_label = "標準的な押し目"
        else:
            pb_zone_label = "深い押し"
        pullback_entry = {
            "status": "candidate",
            "zoneLow": round(pb_price * 0.997, 2), "zoneHigh": round(pb_price * 1.003, 2),
            "basis": pb_basis, "zoneLabel": pb_zone_label, "deviationPct": round(pb_dev_pct, 2),
        }
    elif is_strong_ready:
        pullback_entry = {"status": "no_candidate"}
    else:
        pullback_entry = {"status": "deep_adjustment" if is_uptrend_early else "trend_recheck"}

    # ---- 損切り単価(目安)：ATR相当(当日実測 or 日次ATR)の1倍を損切り幅の目安とする（ザラ場内で許容できる下振れ）。
    # entry確定後に算出するため、当日安値による下限調整をentryにも先に反映済み（旧実装は
    # entry未調整のままstopだけ当日安値でかさ上げしていたため、entryより高いstopが出る不具合があった）。----
    stop = entry - atr_ref
    stop_reasons = [f"{atr_label}({atr_ref:.1f})の1倍を損切り幅の目安に設定"]
    if intraday_low is not None and stop < intraday_low < entry:
        stop = intraday_low
        stop_reasons.append(f"当日安値({intraday_low:.1f})を下限目安として調整")

    # ---- エントリーのタイミングルール（最新版）⑦：損切り位置を「買う前に」決めるルール。候補は
    # 5分足直近安値割れ／VWAP割れ／5分足短期線の明確な割れの3つ。このうちentryより下でATR基準の
    # stopより浅い（＝entryに近い）ものがあれば、より早く「間違いだった」と判断できる基準として
    # 採用する（ATR基準より深くする方向へは動かさない＝安全側のみ）。----
    stop_candidates = []
    if recent_low_5m is not None and recent_low_5m < entry:
        stop_candidates.append(("5分足直近安値", recent_low_5m))
    if vwap is not None and vwap < entry:
        stop_candidates.append(("VWAP", vwap))
    if ma5_short is not None and ma5_short < entry:
        stop_candidates.append(("5分足短期線", ma5_short))
    if stop_candidates:
        tightest_label, tightest_price = max(stop_candidates, key=lambda x: x[1])
        if tightest_price > stop:
            stop = tightest_price
            stop_reasons.append(f"{tightest_label}({tightest_price:.1f})を割ったら損切りと判断（買う前に損切り位置を決めるルール）")

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

    # ---- 利確ルール（最新版）：利益目標+5〜8%に達したら100株すべての利確を検討する全部利確ライン。
    # 上のtarget(+3〜5%が目安)は一部利確・様子見の目安、こちらは「そこまで伸びたら欲張らず全部閉じる」
    # という上限ラインとして別に返す（買う前に決めるルールのため、ここもentry確定時点で計算する）。----
    full_exit_target = entry * 1.08

    # ---- 動画「1_UnOn0ayww」の教え⑪：逆張り（下落からの戻り狙い）で買った場合の目標は基本的に
    # 25日移動平均線への戻り、地合いが強ければボリンジャーバンド+2σまで引っ張ることも検討する。
    # 実際のtarget/full_exit_target（当日ATRベースで値幅制限内に収まるよう算出）は変更せず、
    # 参考情報としてのみ返す（複数日単位の水準をそのまま単価にすると値幅制限を超える非現実的な
    # 価格になる教訓があるため、既存のATRベース計算を上書きしない）。----
    rebound_target_note = None
    reversal_signal_keys = {"volShadow", "bb3", "bb2Rebound", "dipStabilized", "compoundBottom"}
    if primary and primary["key"] in reversal_signal_keys:
        if ma25 is not None and ma25 > entry:
            rebound_target_note = f"逆張りの場合の戻り目安は25日線（{ma25:.1f}）"
            if not market_env_bad and bb_upper2 is not None and bb_upper2 > ma25:
                rebound_target_note += f"。地合いが強ければボリンジャーバンド+2σ（{bb_upper2:.1f}）まで引っ張ることも検討"

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
            if full_exit_target > day_hi:
                full_exit_target = day_hi

    # ---- 最終安全確認：ここまでの調整後も stop < entry < target の順序を必ず保証する ----
    min_gap = max(entry * 0.002, 1)
    if stop >= entry:
        stop = entry - min_gap
    if target <= entry:
        target = entry + min_gap

    # ---- trading_rules.md：エントリー適性・見送り条件のチェックリスト（自動判定できる範囲のみ）----
    is_uptrend = is_uptrend_early
    is_pullback = is_uptrend and not high_zone  # 上昇トレンド中で直近高値からは離れている＝押し目
    above_vwap = vwap is not None and current > vwap
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

    # ---- 動画「1_UnOn0ayww」の教え⑥：パンパカパン中の25日線への3回目以降の接近はトレンド転換に警戒 ----
    if panpakapan_third_touch:
        avoid_checklist.append({"key": "panpakapanThirdTouch",
                                 "label": f"パンパカパン中に25日線へ{panpakapan_third_touch}回目の接近＝トレンド転換に警戒",
                                 "hit": True})

    # ---- 動画「テスタさん」の教え⑦⑨：過去に例のない大商いを伴う急落で主要移動平均線を一気に割った ----
    if catastrophic_volume_crash:
        avoid_checklist.append({"key": "catastrophicVolumeCrash",
                                 "label": "過去に例のない大商いを伴う急落で5日・25日・75日線を一気に割っている（戻りを期待しない）",
                                 "hit": True})

    # ---- 動画「テスタさん」の教え⑥：出来高を伴わない上昇は一時的な可能性を疑う ----
    if thin_volume_rise:
        avoid_checklist.append({"key": "thinVolumeRise", "label": "出来高を伴わない上昇（一時的な値動きの可能性）", "hit": True})

    # ---- 動画「テスタさん」の教え⑧：価格帯別出来高（POC）が現在値の上か下かで支持線/抵抗線を判断 ----
    if volume_profile_poc is not None:
        if current < volume_profile_poc * 0.995:
            avoid_checklist.append({"key": "poIsResistance",
                                     "label": f"価格帯別出来高の厚い価格帯（{volume_profile_poc:.1f}）が上に控えており上値抵抗になりやすい",
                                     "hit": True})
        elif current > volume_profile_poc * 1.005:
            entry_checklist.append({"key": "poIsSupport",
                                     "label": f"価格帯別出来高の厚い価格帯（{volume_profile_poc:.1f}）が下に控えており支持線になりやすい",
                                     "pass": True})

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

    # ---- エントリーのタイミングルール（最新版）⑥：飛び付き買い禁止の4条件。長い陽線の天井・
    # RSIだけが高い（出来高等の他の裏付けがない）・5分足短期線からの大きな乖離・利益確定売りが
    # 出そうな上値抵抗線付近、のいずれかに該当すれば見送りチップとして警告する。----
    long_bull_top = bool(high_zone and is_bull and body >= rng * 0.7)
    if long_bull_top:
        avoid_checklist.append({"key": "longBullTop", "label": "高値圏での長い陽線の天井（飛び付き買い注意）", "hit": True})

    rsi_only_high = bool(rsi is not None and rsi >= 75 and not vol_surge and not buy_signals)
    if rsi_only_high:
        avoid_checklist.append({"key": "rsiOnlyHigh", "label": f"RSI({rsi:.0f})だけが高く出来高等の裏付けがない（飛び付き買い注意）", "hit": True})

    away_from_5m_ma = bool(ma5_short is not None and ma5_short > 0 and current > ma5_short * 1.03)
    if away_from_5m_ma:
        dev5m = (current - ma5_short) / ma5_short * 100
        avoid_checklist.append({"key": "awayFrom5mMa", "label": f"5分足短期線から+{dev5m:.1f}%大きく乖離（飛び付き買い注意）", "hit": True})

    near_resistance_now = bool(near_resistance_count >= 3 and resistance > 0 and current >= resistance * 0.98)
    if near_resistance_now:
        avoid_checklist.append({"key": "profitTakingZone", "label": f"上値抵抗線（{resistance:.1f}）付近＝利益確定売りが出そうな位置（飛び付き買い注意）", "hit": True})

    # ---- 動画「スマホで2億円を稼いだ天才ママ」の教え⑪：地合いが良いのに売られている「弱い銘柄」は
    # 見送り警告、地合いが悪いのに下がらない「強い銘柄」はエントリー適性チップとして加点表示する。----
    if relative_strength_note:
        if nikkei_chg is not None and nikkei_chg >= 0.3 and change_pct <= -1:
            avoid_checklist.append({"key": "weakerThanMarket", "label": relative_strength_note, "hit": True})
        else:
            entry_checklist.append({"key": "strongerThanMarket", "label": relative_strength_note, "pass": True})

    # ---- trading_rules_追加分ルール②：貸借倍率（kabutanスクレイピング、日本株のみ）----
    margin_ratio = _kabutan_margin_ratio(w.get("code", "")) if w.get("market", "JP") == "JP" else None
    margin_badge, margin_note = _margin_badge(margin_ratio)
    if margin_badge in ("caution", "danger"):
        avoid_checklist.append({"key": "marginRatio", "label": margin_note, "hit": True})
    if margin_badge == "danger":
        strength = max(1, strength - 1)
        entry_reasons.append(f"{margin_note}のためエントリー推奨を格下げ")

    # ---- 決算内容の悪化・増資（希薄化）をチェックリスト・推奨度（星）にも反映する（ユーザー要望）。
    # 単価への反映は上のentry算出時点で済んでいるため、ここではbad_earnings_note/dilution_note
    # （entry算出時に計算済み）を使い回し、PDF・TDnetの再取得はしない。----
    if bad_earnings_note:
        avoid_checklist.append({"key": "badEarnings", "label": bad_earnings_note, "hit": True})
        # 決算悪化はテクニカルの強気シグナルより重い材料のため、他の警告(-1)より大きく
        # 「星2つ以下」まで一気に格下げする（テクニカルが強気でも鵜呑みにしない）。
        strength = min(strength, 2)
        entry_reasons.append("直近決算が軟調のため推奨度（星）を大きく格下げ")
    if dilution_note:
        avoid_checklist.append({"key": "dilution", "label": dilution_note, "hit": True})
        strength = max(1, strength - 1)
        entry_reasons.append("増資関連の適時開示があるためエントリー推奨度（星）を格下げ")
    # PTS（夜間取引）は無料で安定したAPIが無く自動取得できないため、警告扱いにはせず中立的な
    # 注記（ptsNote）としてのみ返す（avoidChecklistに混ぜると「検出された危険」に見えてしまうため）。
    pts_note = "PTS（夜間取引）の値動きは自動取得非対応です。気配は証券会社アプリ等でご自身でご確認ください。"

    # ---- 決算期待値の星（旧・手動クリック評価を自動計算に置き換え）----
    auto_earnings_stars = (_auto_earnings_stars(code, rsi, high_zone, low_zone, bad_earnings_note, dilution_note)
                            if w.get("market", "JP") != "US" and code else None)

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

    # ---- 動画「スマホで2億円を稼いだ天才ママ」の教え⑫：成否が二択のイベント（決算等）に大金を
    # 賭けない。決算が目前(2日以内)の場合は結果次第で大きく振れるため、ポジションを抑える注意を出す。----
    if days_to_earnings is not None and 0 <= days_to_earnings <= 2:
        avoid_checklist.append({"key": "binaryEventNear",
                                 "label": f"決算発表まで{days_to_earnings}日＝結果次第で大きく振れる二択イベント目前（大きく張らない）",
                                 "hit": True})

    # ---- エントリーのタイミングルール（最新版）：毎回確認するチェックリスト。セクターの強さ・
    # 板の厚みはyfinanceで自動取得できないため手動確認の注記（pass:None）にとどめ、それ以外は
    # ここまでに計算済みの値を再利用する。損切り位置・100株スタートは常に「決まっている」方針。----
    entry_timing_checklist = [
        {"key": "sectorStrong", "label": "セクターは強いか（日次モニターのセクター売買代金順で要確認）", "pass": None},
        {"key": "marketGood", "label": "地合いは良いか", "pass": (not market_env_bad) if market_env.get("text") else None},
        {"key": "volumeUp", "label": "出来高は増えているか", "pass": bool(vol_surge)},
        {"key": "highUpdate", "label": "高値更新・高値圏を維持しているか", "pass": bool(high_zone)},
        {"key": "above5mMa", "label": "5分足短期線の上か", "pass": bool(current >= ma5_short) if ma5_short is not None else None},
        {"key": "bidAbsorb", "label": "板は売りを吸収しているか（自動取得非対応、ご自身でご確認ください）", "pass": None},
        {"key": "stopDecided", "label": "損切り位置は決めたか（下の損切り単価を参照）", "pass": True},
        {"key": "start100", "label": "100株から入るか（迷うなら100株。最初から全力はしない）", "pass": True},
    ]
    position_size_note = ("最初は100株の打診買い。迷うなら100株だけ。値動きを確認してから計画的に追加、"
                           "ダメなら損切り。最初から全力はしない・無計画なナンピンはしない。")

    # ---- 利確ルール（最新版）：値幅の目標到達だけでなく、①利益目標+5〜8%到達 ②5分足短期線を
    # 終値で明確に割った ③急騰後に高値更新できず陰線が続いた、のいずれかを「利確を検討すべき」
    # シグナルとして返す。「もっと上がるかも」でルールを変えないよう、条件が揃えば機械的に示す。----
    profit_pct_from_entry = (current - entry) / entry * 100 if entry else None
    ma5_short_break = bool(ma5_short is not None and ma5_short > 0 and current < ma5_short * 0.997)
    last2_bearish = bool(n >= 2 and closes[-1] < opens[-1] and closes[-2] < opens[-2])
    no_new_high_recent = bool(n >= 6 and current < max(highs[-6:-1]))
    surged_recently = bool(change_1w is not None and change_1w >= 8)
    stall_after_surge = bool(surged_recently and no_new_high_recent and last2_bearish)
    # ---- 動画「1_UnOn0ayww」の教え⑫：ボリンジャーバンド+3σを超えるような過熱状態は利益確定を検討 ----
    bb3_upper_touch = bool(bb_upper3 is not None and current >= bb_upper3)
    exit_checklist = [
        {"key": "profitTargetHit",
         "label": f"利益目標+5〜8%に到達（現在値は目安買値から{profit_pct_from_entry:+.1f}%）→100株すべての利確を検討",
         "hit": bool(profit_pct_from_entry is not None and profit_pct_from_entry >= 5)},
        {"key": "ma5ShortBreakExit", "label": "5分足短期線を終値で明確に割った→利確", "hit": ma5_short_break},
        {"key": "stallAfterSurge", "label": "急騰後に高値更新できず陰線が続いている→利確", "hit": stall_after_surge},
        {"key": "bb3UpperTouch", "label": f"ボリンジャーバンド+3σ（{bb_upper3:.1f}）到達＝過熱感が高く利益確定を検討" if bb_upper3 is not None else "",
         "hit": bb3_upper_touch},
    ]
    profit_taking_note = "「もっと上がるかも」でルールを変えない。利確シグナルが出たら機械的に実行する。"

    # ---- 動画「テスタさん」の教え③④⑮：移動平均線は「その期間に買った投資家の平均取得価格」。
    # 時間軸ごとに見る線・損切り基準を最初に決め、短期と長期の売却基準を混ぜない。----
    time_horizon_note = ("移動平均線は投資家の平均取得価格。時間軸は買う前に決める："
                          "数日＝5日線／数週間〜数カ月のスイング＝25日線（最重視）／大きなトレンド＝75日線。"
                          "短期と長期の売却基準を混ぜない。")
    # ---- 動画「テスタさん」の教え⑩⑪⑫⑬：自分の取得価格は市場にとって意味がない。損切りは
    # 失敗ではなく利益確定の一部。勝率100%を目指さず、小さい損失と大きい利益の合計で残す。----
    loss_cut_philosophy_note = ("自分の取得価格は市場にとって意味がない。損切りは失敗ではなく利益確定の一部。"
                                 "損切り後に回復したら、売値より高くても買い直してよい。勝率100%は目指さない。")

    # ---- 動画「スマホで2億円を稼いだ天才ママ」の教え⑨⑩：テクニカルで入った銘柄はテクニカルで出る、
    # ファンダで入った銘柄はファンダで出る（買った根拠と売る根拠を一致させる）。この分析はテクニカル
    # シグナル・当日値幅を根拠に単価を算出しているため、原則テクニカル基準（このカードのシグナル・
    # 利確/損切りチェックリスト）で出口を判断する旨を明記する。ファンダメンタルズを主な根拠に買う
    # 場合は、この単価をそのまま使わずファンダの前提が崩れるまで保有する、という別基準になる点に注意。----
    entry_basis_note = ("この目安はテクニカル根拠（チャート・出来高）で算出しています。買った根拠と"
                         "売る根拠を一致させるため、利確・損切りもテクニカル基準（本カードのシグナルや"
                         "チェックリスト）に従ってください。事業内容・業績等のファンダメンタルズを主な"
                         "根拠に買う場合は、この単価は使わずファンダの前提が崩れない限り保有する、という"
                         "別の基準になります。")

    fund_notes = []
    # ---- 動画「1_UnOn0ayww」の教え④：海外投資家が買いやすい大型株を優先する。時価総額の目安を
    # fundamentalNoteに表示するだけでなく、entryChecklist/avoidChecklistにも反映し、実際の
    # エントリー判断（星評価につながるチェック項目）に使えるようにする（日本株のみ。米国株は
    # yfinanceのmarketCapがUSD建てで単位が異なるため対象外）。----
    mcap = fundamentals.get("marketCap")
    if mcap and w.get("market", "JP") != "US":
        mcap_oku = mcap / 1e8
        if mcap_oku >= 1000:
            size_label = "大型株"
            entry_checklist.append({"key": "largeCap", "label": f"時価総額 約{mcap_oku:,.0f}億円の大型株（海外投資家の資金が入りやすい）", "pass": True})
        elif mcap_oku >= 300:
            size_label = "中型株"
        else:
            size_label = "小型株"
            avoid_checklist.append({"key": "smallCap", "label": f"時価総額 約{mcap_oku:,.0f}億円の小型株（海外投資家の資金が入りにくい可能性）", "hit": True})
        fund_notes.append(f"時価総額 約{mcap_oku:,.0f}億円（{size_label}、海外投資家の資金が入りやすいのは大型株）")
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

    # ---- Trade Cockpit v2 Phase2（設計案12〜14・21・24・25番）：Falling Knife判定・Chase Risk判定・
    # Entry Condition集計・8軸評価もどきの総合判断・ルールベースの判断文。新規の重い計算は増やさず、
    # ここまでに既に計算済みの値（catastrophic_volume_crash・chasing_gu_high・rsi_only_high・
    # away_from_5m_ma・near_resistance_now・entry_checklist等）を組み合わせるだけにする。----
    falling_knife_reasons = []
    if catastrophic_volume_crash:
        falling_knife_reasons.append("大商いを伴う急落で主要移動平均線を一気に割っている")
    if (change_pct is not None and change_pct <= -4 and intraday_low is not None
            and current <= intraday_low * 1.01 and not already_pulled_back):
        falling_knife_reasons.append(f"本日{change_pct:+.1f}%の急落で、現在値がまだ当日安値付近＝下げ止まりを確認できていない")
    falling_knife = bool(falling_knife_reasons)

    chase_risk_reasons = []
    if long_bull_top:
        chase_risk_reasons.append("高値圏での長い陽線の天井")
    if rsi_only_high:
        chase_risk_reasons.append(f"RSI({rsi:.0f})だけが高く出来高等の裏付けがない")
    if away_from_5m_ma:
        chase_risk_reasons.append("5分足短期線から大きく乖離")
    if near_resistance_now:
        chase_risk_reasons.append("上値抵抗線付近＝利益確定売りが出そうな位置")
    if chasing_gu_high:
        chase_risk_reasons.append("GU日の寄り付き高値を追いかけている")
    chase_risk = bool(chase_risk_reasons)

    entry_met = sum(1 for c in entry_checklist if c.get("pass") is True)
    entry_total = len(entry_checklist)

    market_rs = (change_pct - nikkei_chg) if (change_pct is not None and nikkei_chg is not None) else None

    # ---- 8軸評価もどき（設計案24番）。有料AI APIは使わずルールベースのラベルのみ。----
    axis_market = market_env.get("marketCondition")
    axis_supplyDemand = "RISK" if margin_badge in ("caution", "danger") else ("NEUTRAL" if margin_badge else None)
    if entry_total:
        axis_technical = "STRONG" if entry_met / entry_total >= 0.6 else ("NEUTRAL" if entry_met / entry_total >= 0.4 else "WEAK")
    else:
        axis_technical = None
    axis_catalyst = None
    if days_to_earnings is not None and 0 <= days_to_earnings <= 10:
        axis_catalyst = "POSITIVE" if (auto_earnings_stars or 0) >= 4 else "NEUTRAL"

    # ---- 総合判断・ルールベースの判断文（設計案25番）。テンプレート＋条件判定のみ、AI不使用。----
    judgment_parts = []
    if falling_knife:
        overall_status = "WAIT"
        judgment_parts.append("急落中で下げ止まりが未確認のため、現時点では様子見（Falling Knife）")
    elif chase_risk:
        overall_status = "WAIT"
        judgment_parts.append("勢いは強いが現在位置からの追いかけはリスクが高い（Chase Risk）。押し目を待つ")
    elif entry_total and entry_met == entry_total:
        overall_status = "HOT"
        judgment_parts.append(f"エントリー条件が{entry_total}/{entry_total}すべて成立")
    elif entry_total and entry_met > 0:
        overall_status = "WATCH"
        judgment_parts.append(f"エントリー条件{entry_met}/{entry_total}成立、残りの条件待ち")
    else:
        overall_status = "WEAK"
        judgment_parts.append("明確な優位性は確認できず、様子見が妥当")
    if relative_strength_note:
        judgment_parts.append(relative_strength_note)
    judgment_text = "。".join(judgment_parts) + "。"

    assessment = {
        "overallStatus": overall_status,
        "judgmentText": judgment_text,
        "entryConditionsMet": entry_met, "entryConditionsTotal": entry_total,
        "fallingKnife": falling_knife, "fallingKnifeReasons": falling_knife_reasons,
        "chaseRisk": chase_risk, "chaseRiskReasons": chase_risk_reasons,
        "marketRS": round(market_rs, 2) if market_rs is not None else None,
        "marketRiskScore": market_env.get("marketRiskScore"),
        "marketRiskLabel": market_env.get("marketRiskLabel"),
        "axes": {
            "market": axis_market, "supplyDemand": axis_supplyDemand,
            "technical": axis_technical, "catalyst": axis_catalyst,
        },
    }

    return {
        "current": round(current, 2),
        "entry": round(entry, 2), "entryReason": "・".join(entry_reasons),
        "pullbackEntry": pullback_entry,  # v3-7：表示用の押し目価格帯（ゾーン・根拠・分類）。entryとは独立
        "stop": round(stop, 2), "stopReason": "・".join(stop_reasons),
        "target": round(target, 2), "targetReason": "・".join(target_reasons),
        "fullExitTarget": round(full_exit_target, 2),
        "strength": strength,
        "marketEnv": market_env.get("text"),
        "signals": {
            "buy": [{"key": s["key"], "label": s["label"]} for s in buy_signals],
            "sell": [{"key": s["key"], "label": s["label"]} for s in sell_signals],
        },
        "indicators": {
            "ma5Daily": round(ma5_daily, 2) if ma5_daily is not None else None,
            "ma25": round(ma25, 2) if ma25 else None,
            "ma75": round(ma75, 2) if ma75 else None,
            "ma100": round(ma100, 2) if ma100 else None,
            "rsi": round(rsi, 1) if rsi is not None else None,
            "rci26": round(rci26, 1) if rci26 is not None else None,
            "bbUpper2": round(bb_upper2, 2) if bb_upper2 else None,
            "bbLower2": round(bb_lower2, 2) if bb_lower2 else None,
            "bbLower3": round(bb_lower3, 2) if bb_lower3 else None,
            "volumeProfilePOC": round(volume_profile_poc, 2) if volume_profile_poc is not None else None,
            "support": round(support, 2), "resistance": round(resistance, 2),
            "high52w": round(high52w, 2), "low52w": round(low52w, 2),
            "atr14": round(atr14, 2) if atr14 is not None else None,
            # 当日の分足（1分/5分/15分）からリアルタイムに算出した指標。市場時間外は取得できずNoneになる。
            "intradayHigh": round(intraday_high, 2) if intraday_high is not None else None,
            "intradayLow": round(intraday_low, 2) if intraday_low is not None else None,
            "vwap": round(vwap, 2) if vwap is not None else None,
            "rsi5m": round(rsi_5m, 1) if rsi_5m is not None else None,
            "rsi15m": round(rsi_15m, 1) if rsi_15m is not None else None,
            "ma5Short": round(ma5_short, 2) if ma5_short is not None else None,
        },
        "fundamentalNote": "・".join(fund_notes) if fund_notes else "取得できるファンダメンタルデータがありません",
        "daysToEarnings": days_to_earnings,  # 決算またぎ期待値機能：10日前からのカウントダウン表示に使う
        "autoEarningsStars": auto_earnings_stars,  # 決算期待値の星（過去の上方/下方修正比率・直近決算・過熱度から自動算出）
        "assessment": assessment,  # Trade Cockpit v2 Phase2：Falling Knife/Chase Risk・Entry Conditions集計・8軸ラベル・ルールベース判断文
        # v3-8 Step3：ポジション管理（Step5のチャート崩れ判定）向けの軽量フラグ。新規API呼び出しなし。
        "positionFlags": {
            "longBearishCandle": long_bearish_candle,
            "gapUpFailure": gap_up_failure,
        },
        "tradeRules": {
            "entryChecklist": entry_checklist,
            "avoidChecklist": avoid_checklist,
            "earningsNote": earnings_note,
            "ptsNote": pts_note,
            "watchStatus": watch_status,
            "marginRatio": round(margin_ratio, 2) if margin_ratio is not None else None,
            "marginBadge": margin_badge,
            "entryPattern": entry_pattern,
            "entryTimingChecklist": entry_timing_checklist,
            "positionSizeNote": position_size_note,
            "exitChecklist": exit_checklist,
            "profitTakingNote": profit_taking_note,
            "entryBasisNote": entry_basis_note,
            "relativeStrengthNote": relative_strength_note,
            "reboundTargetNote": rebound_target_note,
            "timeHorizonNote": time_horizon_note,
            "lossCutPhilosophyNote": loss_cut_philosophy_note,
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

    # 2026-08-21 ユーザー要望：銘柄分析カードに銘柄詳細情報・信用残情報・証金残情報・逆日歩情報・
    # ニュース（見出し＋本文）を追加。立花証券APIの仕様書（e_api_web_access添付のCLMMfdsGetIssueDetail
    # 等）に基づく。日本株のみ対象（これらは東証個別株向けの情報のため）。4種の残高情報はコード最大120件
    # まで一括取得できるAPI仕様のため、対象銘柄をまとめて1回ずつ問い合わせる（銘柄ごとに個別リクエスト
    # しない）。ニュースの本文は1件ずつ別リクエストが必要なため、銘柄ごとに件数を絞って取得する。
    jp_codes = [w.get("code", "") for w in watchlist if w.get("market", "JP") != "US" and w.get("code")]
    if tachibana_api is not None and jp_codes:
        try:
            issue_detail = tachibana_api.get_issue_detail(jp_codes)
        except Exception as e:
            print("  銘柄詳細情報取得失敗", e)
            issue_detail = {}
        try:
            syoukin_zan = tachibana_api.get_syoukin_zan(jp_codes)
        except Exception as e:
            print("  証金残情報取得失敗", e)
            syoukin_zan = {}
        try:
            shinyou_zan = tachibana_api.get_shinyou_zan(jp_codes)
        except Exception as e:
            print("  信用残情報取得失敗", e)
            shinyou_zan = {}
        try:
            hibu_info = tachibana_api.get_hibu_info(jp_codes)
        except Exception as e:
            print("  逆日歩情報取得失敗", e)
            hibu_info = {}
        for code in jp_codes:
            if code not in out:
                continue
            out[code]["issueDetail"] = issue_detail.get(code) or None
            out[code]["syoukinZan"] = syoukin_zan.get(code) or None
            out[code]["shinyouZan"] = shinyou_zan.get(code) or None
            out[code]["hibuInfo"] = hibu_info.get(code) or None
            out[code]["stockNews"] = _stock_news_for(code)
    return out


def _stock_news_for(code, limit=3):
    """個別銘柄のニュースを見出し＋本文つきで返す（銘柄分析カード用）。build_stock_name_news・
    _tachibana_stock_newsは登録銘柄一覧をまとめて処理する用途で見出しのみだが、こちらは1銘柄分を
    p_IS指定で絞り込み取得し、本文（get_news_body、1件ごとに別リクエスト）も付ける。件数は
    呼び出し回数を抑えるため直近limit件に絞る。取得失敗時は空リスト（カード全体は表示を継続）。"""
    if tachibana_api is None or not code:
        return []
    jst = datetime.timezone(datetime.timedelta(hours=9))
    now = datetime.datetime.now(jst)
    today_str = now.strftime("%Y%m%d")
    date_from = (now - datetime.timedelta(days=30)).strftime("%Y%m%d")
    try:
        heads = tachibana_api.get_stock_news(code, date_from, today_str, limit=20)
    except Exception as e:
        print(f"  銘柄別ニュース取得失敗（{code}）", e)
        return []
    heads.sort(key=lambda h: _tdnet_date_sort_key(
        h.get("date", ""), f"{h['time'][:2]}:{h['time'][2:]}" if len(h.get("time", "")) == 4 else "00:00"
    ), reverse=True)
    out = []
    for h in heads[:limit]:
        try:
            body = tachibana_api.get_news_body(h.get("id", ""))
        except Exception as e:
            print(f"  ニュース本文取得失敗（{h.get('id')}）", e)
            body = ""
        d, tm = h.get("date", ""), h.get("time", "")
        hhmm = f"{tm[:2]}:{tm[2:]}" if len(tm) == 4 else ""
        published = f"{d[4:6]}/{d[6:8]} {hhmm}" if len(d) == 8 and hhmm else ""
        out.append({"headline": h.get("headline", ""), "body": body, "published": published})
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

    def _read_json_body(self):
        """POST本文をJSONとして読む（投資判断ログAPI群で使用。既存の各APIは個別に読んでいるが、
        新設分はここに揃える）。パース失敗時は{}を返す。"""
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return {}

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _authorized(self):
        """複数ユーザー対応のBasic認証（2026-09-02 マルチユーザー化）。USERS
        （{"ユーザー名":"パスワード"}、secrets.jsonの"users"またはRenderの環境変数APP_USERS）が
        空なら、従来通り認証なしで動作する（ローカル/LAN限定利用向け。この場合self.current_userは
        "local"固定＝Neon側のuser_id）。USERSが1件でも設定されていれば、必ずユーザー名・
        パスワードでのログインが必要になる（マルチユーザー化以降は「誰が使っているか」を
        user_idとしてNeon側の各テーブルに記録するため、ローカル/LANかどうかを問わず必須）。"""
        if not USERS:
            self.current_user = "local"
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                decoded = base64.b64decode(header[6:]).decode("utf-8", errors="replace")
                username, _, pw = decoded.partition(":")
                expected = USERS.get(username)
                if expected is not None and secrets.compare_digest(pw, expected):
                    self.current_user = username
                    return True
            except Exception:
                pass
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Trade Cockpit"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("ユーザー名とパスワードが必要です。".encode("utf-8"))
        return False

    def end_headers(self):
        # trade-cockpit.html等の静的配信はブラウザ側のキャッシュにより、コード修正後に
        # リロードしても古い見た目のままになることがあったため、常にキャッシュさせない。
        self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()

    def do_GET(self):
        if not self._authorized():
            return
        if self.path.startswith("/api/quotes"):
            print("[取得] 指数・為替 …")
            quotes = get_index_quotes()
            now = datetime.datetime.now().strftime("%H:%M:%S")
            self._send_json({"quotes": quotes, "fetchedAt": now})
        elif self.path.startswith("/api/edinet-doc"):
            self._proxy_edinet_doc()
        elif self.path.startswith("/api/stock-history"):
            self._stock_history()
        elif self.path.startswith("/api/jp-issue-master"):
            items = get_jp_issue_master()
            self._send_json({"items": items})
        elif self.path.startswith("/api/investment-log"):
            qs = urllib.parse.urlparse(self.path).query
            q = urllib.parse.parse_qs(qs)
            logs = investment_db.list_daily_logs(
                DATABASE_URL, self.current_user,
                date_from=q.get("from", [None])[0],
                date_to=q.get("to", [None])[0],
                code=q.get("code", [None])[0],
            ) if (investment_db is not None and DATABASE_URL) else []
            self._send_json({"logs": logs})
        elif self.path.startswith("/api/journal"):
            entries = investment_db.list_journal(DATABASE_URL, self.current_user) if (investment_db is not None and DATABASE_URL) else []
            self._send_json({"journal": entries})
        elif self.path.startswith("/api/rules"):
            rules = investment_db.list_rules(DATABASE_URL, self.current_user) if (investment_db is not None and DATABASE_URL) else []
            self._send_json({"rules": rules})
        elif self.path.startswith("/api/chatgpt-import/list"):
            imports = investment_db.list_chatgpt_imports(DATABASE_URL, self.current_user) if (investment_db is not None and DATABASE_URL) else []
            self._send_json({"imports": imports})
        elif self.path.startswith("/api/market-risk"):
            # Trade Cockpit v2 Phase3（設計案27番）：日次モニター最上部のTODAY'S MARKET用。
            # _market_environment()は指数のトレンド判定に3か月分の日足を毎回取得するため軽くは
            # ないが、analyze_stock()と同じ処理を再利用するだけで新規の重い計算は増やしていない。
            # フロント側は1日1回だけ呼ぶ想定（breakoutLevelsと同じキャッシュパターン）。
            env = _market_environment()
            self._send_json({
                "text": env.get("text"), "nikkeiChangePct": env.get("nikkeiChangePct"),
                "marketRiskScore": env.get("marketRiskScore"), "marketRiskLabel": env.get("marketRiskLabel"),
                "marketCondition": env.get("marketCondition"),
            })
        elif self.path.startswith("/api/trade-candidates"):
            candidates = investment_db.list_trade_candidates(DATABASE_URL, self.current_user) if (investment_db is not None and DATABASE_URL) else []
            self._send_json({"candidates": candidates})
        elif self.path.startswith("/api/stats"):
            # 統計ダッシュボード（2026-09-02新規、Trade Cockpit v2 Phase8）
            stats = investment_db.get_stats(DATABASE_URL, self.current_user) if (investment_db is not None and DATABASE_URL) else None
            self._send_json(stats or {"error": "投資判断ログDB未設定"})
        elif self.path.startswith("/api/watchlist-import/list"):
            # Trade Cockpit v3 Phase5：ChatGPTスクリーンショット監視銘柄取り込み履歴
            imports = investment_db.list_watchlist_imports(DATABASE_URL, self.current_user) if (investment_db is not None and DATABASE_URL) else []
            self._send_json({"imports": imports})
        elif self.path.startswith("/api/watchlist"):
            # v3-2：watchlist本体（Neonが正）。?market=JP|USで絞り込み。
            qs = urllib.parse.urlparse(self.path).query
            market = urllib.parse.parse_qs(qs).get("market", [None])[0]
            items = investment_db.list_watchlist(DATABASE_URL, self.current_user, market=market) if (investment_db is not None and DATABASE_URL) else []
            self._send_json({"items": items})
        elif self.path.startswith("/api/portfolio"):
            # v3-2新規：portfolio（保有株、localStorageに無かった新規機能）
            items = investment_db.list_portfolio(DATABASE_URL, self.current_user) if (investment_db is not None and DATABASE_URL) else []
            self._send_json({"items": items})
        elif self.path == "/" or self.path == "":
            self.send_response(302)
            self.send_header("Location", "/trade-cockpit.html")
            self.end_headers()
        elif self.path.split("?", 1)[0] == "/trade-cockpit.html":
            super().do_GET()  # 本体HTMLのみ静的配信。それ以外のファイル一覧・個別ファイルは
            # 一切配信しない（ディレクトリ一覧表示や、認証情報ファイル(e_api_authid.txt・
            # e_api_private_key.pem・secrets.json)への直接アクセスを防ぐため。2026-08-20
            # 発覚：SimpleHTTPRequestHandlerはデフォルトでフォルダ内の全ファイルを静的配信・
            # 一覧表示してしまうため、必要なファイル1つだけを明示的に許可するホワイトリスト方式にした）。
        else:
            self.send_response(404)
            self.end_headers()

    _CODE_RE = re.compile(r"^[0-9A-Za-z]{1,10}$")

    def _stock_history(self):
        """日本の個別株チャート用：立花証券APIの日足履歴（分割調整済み・上場来）を返す。
        TradingView無料埋め込みが東証再配信制限で使えないJP個別株の代替表示用。
        認証未設定・API側エラー時は空配列を返す（フロント側でTradingView本体誘導にフォールバック）。"""
        qs = urllib.parse.urlparse(self.path).query
        code = urllib.parse.parse_qs(qs).get("code", [""])[0]
        if not code or not self._CODE_RE.match(code) or tachibana_api is None:
            self._send_json({"history": []})
            return
        try:
            print(f"[取得] 日足履歴（立花証券API） {code} …")
            history = tachibana_api.get_daily_history(code)
        except Exception as e:
            print("  日足履歴取得失敗", code, e)
            history = []
        # 2026-08-22 ユーザー要望：日足履歴は前営業日までの確定値のみのため、当日分がチャートに
        # 反映されないまま（_tachibana_daily_arraysと同じ理由）。当日の値がまだ含まれていなければ、
        # 時価情報（ライブ気配）から当日分の仮の日足を合成して末尾に追加する。
        jst = datetime.timezone(datetime.timedelta(hours=9))
        today_str = datetime.datetime.now(jst).strftime("%Y-%m-%d")
        if history and history[-1].get("date") != today_str:
            try:
                live = tachibana_api.get_market_price([code]).get(code)
            except Exception:
                live = None
            if live and live.get("t") is not None and live.get("open") is not None:
                history = history + [{
                    "date": today_str,
                    "open": live["open"],
                    "high": live.get("high") if live.get("high") is not None else live["t"],
                    "low": live.get("low") if live.get("low") is not None else live["t"],
                    "close": live["t"],
                    "volume": live.get("volume") if live.get("volume") is not None else 0,
                }]
        self._send_json({"history": history})

    _DOC_ID_RE = re.compile(r"^[A-Za-z0-9]+$")

    def _proxy_edinet_doc(self):
        # EDINETのPDF取得APIはAPIキーが必須のため、キーをブラウザに渡さずサーバー側で
        # 中継する（フロントはこのエンドポイントへのリンクを開くだけでよい）。
        qs = urllib.parse.urlparse(self.path).query
        doc_id = urllib.parse.parse_qs(qs).get("docID", [""])[0]
        if not doc_id or not self._DOC_ID_RE.match(doc_id) or not EDINET_API_KEY:
            self.send_response(404)
            self.end_headers()
            return
        try:
            req = urllib.request.Request(
                f"{EDINET_API_BASE}/documents/{doc_id}?type=2&Subscription-Key={EDINET_API_KEY}"
            )
            with urllib.request.urlopen(req, timeout=15) as res:
                pdf = res.read()
        except Exception as e:
            print("  EDINET PDF取得失敗", doc_id, e)
            self.send_response(502)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(pdf)

    def _investment_db_ready(self):
        if investment_db is None or not DATABASE_URL:
            self._send_json({"error": "投資判断ログDB未設定（DATABASE_URLが未設定、またはpsycopg未インストール）"})
            return False
        return True

    def do_POST(self):
        if not self._authorized():
            return
        # ---- 投資判断ログ（2026-09-02新規、同日中にマルチユーザー化）：daily_log・stock_judgments ----
        # 全てself.current_user（_authorized()がBasic認証のユーザー名から設定。USERS未設定時は
        # "local"固定）をuser_idとして渡し、他ユーザーのデータに触れないようDB側でもスコープする。
        if self.path == "/api/investment-log/create":
            if not self._investment_db_ready():
                return
            body = self._read_json_body()
            log_id = investment_db.create_daily_log(DATABASE_URL, self.current_user, body.get("dailyLog", {}), body.get("judgments", []))
            self._send_json({"id": log_id})
        elif self.path == "/api/investment-log/update":
            if not self._investment_db_ready():
                return
            body = self._read_json_body()
            investment_db.update_daily_log(DATABASE_URL, self.current_user, body.get("id"), body.get("dailyLog", {}))
            self._send_json({"ok": True})
        elif self.path == "/api/investment-log/delete":
            if not self._investment_db_ready():
                return
            body = self._read_json_body()
            investment_db.delete_daily_log(DATABASE_URL, self.current_user, body.get("id"))
            self._send_json({"ok": True})
        elif self.path == "/api/investment-log/judgment/add":
            if not self._investment_db_ready():
                return
            body = self._read_json_body()
            jid = investment_db.add_stock_judgment(DATABASE_URL, self.current_user, body.get("dailyLogId"), body.get("judgment", {}))
            self._send_json({"id": jid})
        elif self.path == "/api/investment-log/judgment/update":
            if not self._investment_db_ready():
                return
            body = self._read_json_body()
            investment_db.update_stock_judgment(DATABASE_URL, self.current_user, body.get("id"), body.get("judgment", {}))
            self._send_json({"ok": True})
        elif self.path == "/api/investment-log/judgment/delete":
            if not self._investment_db_ready():
                return
            body = self._read_json_body()
            investment_db.delete_stock_judgment(DATABASE_URL, self.current_user, body.get("id"))
            self._send_json({"ok": True})
        # ---- 売買記録（journal）・マイルール（rules）：旧localStorageからDBへ移行済みの保存先 ----
        elif self.path == "/api/journal/save":
            if not self._investment_db_ready():
                return
            body = self._read_json_body()
            investment_db.upsert_journal_entry(DATABASE_URL, self.current_user, body)
            self._send_json({"ok": True})
        elif self.path == "/api/journal/delete":
            if not self._investment_db_ready():
                return
            body = self._read_json_body()
            investment_db.delete_journal_entry(DATABASE_URL, self.current_user, body.get("id"))
            self._send_json({"ok": True})
        elif self.path == "/api/rules/save":
            if not self._investment_db_ready():
                return
            body = self._read_json_body()
            investment_db.upsert_rule(DATABASE_URL, self.current_user, body)
            self._send_json({"ok": True})
        elif self.path == "/api/rules/seed-defaults":
            # v3 Phase6（設計案57番）：初期ルール候補をまとめて追加。ユーザーの明示操作でのみ呼ばれる。
            if not self._investment_db_ready():
                return
            n = investment_db.seed_default_structured_rules(DATABASE_URL, self.current_user)
            self._send_json({"count": n})
        elif self.path == "/api/rules/delete":
            if not self._investment_db_ready():
                return
            body = self._read_json_body()
            investment_db.delete_rule(DATABASE_URL, self.current_user, body.get("id"))
            self._send_json({"ok": True})
        # ---- ChatGPT連携（2026-09-02新規、Phase1）：有料AI APIは使わず、ChatGPTが出力した
        # 投資ログJSONを手動貼り付けで取り込む。 ----
        elif self.path == "/api/chatgpt-import/save":
            if not self._investment_db_ready():
                return
            body = self._read_json_body()
            payload = body.get("payload")
            force = bool(body.get("force"))
            errors = investment_db.validate_chatgpt_payload(payload)
            if errors:
                self._send_json({"errors": errors})
                return
            result = investment_db.save_chatgpt_import(DATABASE_URL, self.current_user, payload, force=force)
            self._send_json(result)
        # ---- 今日の候補（trade_candidates。2026-09-02新規、Trade Cockpit v2 Phase1） ----
        elif self.path == "/api/trade-candidates/save":
            if not self._investment_db_ready():
                return
            body = self._read_json_body()
            cid = investment_db.create_trade_candidate(DATABASE_URL, self.current_user, body)
            self._send_json({"id": cid} if cid is not None else {"error": "codeまたはstatusが不正です"})
        elif self.path == "/api/trade-candidates/delete":
            if not self._investment_db_ready():
                return
            body = self._read_json_body()
            investment_db.delete_trade_candidate(DATABASE_URL, self.current_user, body.get("id"))
            self._send_json({"ok": True})
        elif self.path == "/api/trade-candidates/checkpoint":
            # 仮想トレード追跡（2026-09-02新規、Trade Cockpit v2 Phase7）
            if not self._investment_db_ready():
                return
            body = self._read_json_body()
            ok = investment_db.add_trade_candidate_checkpoint(
                DATABASE_URL, self.current_user, body.get("id"), body.get("label"), body.get("price"))
            self._send_json({"ok": ok})
        elif self.path == "/api/news-feedback/save":
            # 「不要」ニュースのフィードバックログ（2026-09-02新規、Trade Cockpit v3 Phase4）
            if not self._investment_db_ready():
                return
            body = self._read_json_body()
            fid = investment_db.create_news_feedback(DATABASE_URL, self.current_user, body)
            self._send_json({"id": fid} if fid is not None else {"error": "titleが必要です"})
        elif self.path == "/api/watchlist-import/save":
            # ChatGPTスクリーンショット監視銘柄取り込み履歴（2026-09-02新規、Trade Cockpit v3 Phase5）。
            # v3-2以降、watchlist本体もNeonが正になったが、ここは引き続き取り込み履歴・重複防止専用。
            if not self._investment_db_ready():
                return
            body = self._read_json_body()
            result = investment_db.save_watchlist_import(
                DATABASE_URL, self.current_user, body.get("payload"),
                body.get("mode", "add_only"), body.get("addedCount", 0), force=bool(body.get("force")))
            self._send_json(result)
        # ---- watchlist本体（2026-09-03新規、Trade Cockpit v3-2：NeonをSingle Source of Truthに） ----
        elif self.path == "/api/watchlist/save":
            if not self._investment_db_ready():
                return
            body = self._read_json_body()
            ok = investment_db.upsert_watchlist_item(DATABASE_URL, self.current_user, body)
            self._send_json({"ok": ok})
        elif self.path == "/api/watchlist/delete":
            if not self._investment_db_ready():
                return
            body = self._read_json_body()
            investment_db.delete_watchlist_item(DATABASE_URL, self.current_user, body.get("code"), body.get("market"))
            self._send_json({"ok": True})
        elif self.path == "/api/watchlist/migrate":
            # 既存ユーザーのlocalStorage watchlistを1回だけNeonへ取り込む（investmentLogMigratedと
            # 同じパターン）。冪等（同じcode+marketは上書きになるだけ）。
            if not self._investment_db_ready():
                return
            body = self._read_json_body()
            n = investment_db.migrate_watchlist_from_client(DATABASE_URL, self.current_user, body.get("items", []))
            self._send_json({"count": n})
        # ---- portfolio（2026-09-03新規、Trade Cockpit v3-2） ----
        elif self.path == "/api/portfolio/save":
            if not self._investment_db_ready():
                return
            body = self._read_json_body()
            ok = investment_db.upsert_portfolio_item(DATABASE_URL, self.current_user, body)
            self._send_json({"ok": ok})
        elif self.path == "/api/portfolio/delete":
            if not self._investment_db_ready():
                return
            body = self._read_json_body()
            investment_db.delete_portfolio_item(DATABASE_URL, self.current_user, body.get("code"), body.get("market"))
            self._send_json({"ok": True})
        elif self.path == "/api/investment-log/quick-judgment":
            # 監視銘柄タブからのワンクリック記録（2026-09-02新規、Trade Cockpit v2 Phase5・設計案39番）。
            # 当日のdaily_logが無ければ自動作成し、stock_judgmentを1件追加する。
            if not self._investment_db_ready():
                return
            body = self._read_json_body()
            jst = datetime.timezone(datetime.timedelta(hours=9))
            date = body.get("date") or datetime.datetime.now(jst).strftime("%Y-%m-%d")
            log_id = investment_db.get_or_create_daily_log(DATABASE_URL, self.current_user, date)
            jid = investment_db.add_stock_judgment(DATABASE_URL, self.current_user, log_id, body.get("judgment", {}))
            self._send_json({"dailyLogId": log_id, "judgmentId": jid})
        elif self.path == "/api/migrate-legacy":
            if not self._investment_db_ready():
                return
            body = self._read_json_body()
            print(f"[投資判断ログ] 旧データ移行（{self.current_user}・journal {len(body.get('journal', []))}件・rules {len(body.get('rules', []))}件）…")
            result = investment_db.migrate_legacy(DATABASE_URL, self.current_user, body.get("journal", []), body.get("rules", []))
            self._send_json(result)
        elif self.path.startswith("/api/news"):
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"[]"
            try:
                watchlist = json.loads(raw.decode("utf-8") or "[]")
            except Exception:
                watchlist = []
            print(f"[取得] ニュース（登録銘柄 {len(watchlist)} 件 ＋ マクロ）…")
            stock = build_stock_news(watchlist)
            stock_name_news = build_stock_name_news(watchlist)
            disclosure_news = build_disclosure_news(watchlist)
            macro_domestic, macro_overseas = build_macro_news()
            macro_all = macro_domestic + macro_overseas
            self._send_json({
                "stockNews": stock,  # 9章：決算・IR・適時開示のみに絞り込み済み
                "stockNameNews": stock_name_news,  # 「登録銘柄」サブタブ：IR以外も含む社名一致ニュース
                "disclosureNews": disclosure_news,  # 「適時開示」サブタブ：当日含む直近10日分・種類問わず全件
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
        elif self.path.startswith("/api/breakout-levels"):
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"[]"
            try:
                watchlist = json.loads(raw.decode("utf-8") or "[]")
            except Exception:
                watchlist = []
            print(f"[取得] 出来高ブレイクアウト基準値（対象 {len(watchlist)} 銘柄）…")
            levels = get_breakout_levels(watchlist)
            self._send_json({"levels": levels})
        elif self.path.startswith("/api/guess-sector"):
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                body = {}
            code = str(body.get("code") or "").strip()
            market = body.get("market") or "JP"
            print(f"[取得] 業種推定（{code}／{market}）…")
            sector = guess_sector(code, market)
            self._send_json({"sector": sector})
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
    if investment_db is not None and DATABASE_URL:
        try:
            investment_db.init_schema(DATABASE_URL)
            print("[投資判断ログ] DBスキーマ確認OK")
        except Exception as e:
            print("[投資判断ログ] DB接続・スキーマ作成に失敗（この機能のみ利用不可。他機能には影響しません）", e)
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
        lan_ip = _lan_ip()
        if lan_ip:
            print("  --- スマホ・他PCから使う場合（同じWi-Fiに接続してください） ---")
            print(f"  http://{lan_ip}:{PORT}/trade-cockpit.html")
            print("  ※初回、Windowsのファイアウォール確認画面が出たら「アクセスを許可する」を選んでください。")
        print("  使い終わったら、このウィンドウを閉じてください。")
    print("=" * 52)
    with httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
