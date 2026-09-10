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
import concurrent.futures
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


# 2026-09-07新規（Tailscale経由の外出先アクセス）：Tailscaleがインストール・接続済みなら
# `tailscale ip -4`でこのPCのTailscale IP（100.x.x.x、CGNATレンジ）とMagicDNS名を取得できる。
# サーバー自体は元々HOST="0.0.0.0"で全ネットワークIFに待ち受けているため、Tailscale IPが
# わかればコード変更なしにそのまま`http://<Tailscale IP>:{PORT}/trade-cockpit.html`でアクセス
# できる。Tailscale未インストール・未接続・コマンド不在の場合は起動時バナー表示を諦めるだけで
# サーバー起動自体は妨げない（既存のLAN案内と同じ「機能低下のみで停止しない」方針）。
def _tailscale_status():
    """Tailscaleが使える場合は{"ip":"100.x.x.x","dnsName":"pc名.tailnet-xxxx.ts.net"}を返す。
    使えない・未接続の場合はNone（実際の通信は行わず、ローカルのtailscaleコマンドに問い合わせるのみ）。"""
    import subprocess
    try:
        r = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True,
                            timeout=3, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode != 0 or not r.stdout:
            return None
        data = json.loads(r.stdout)
        self_info = data.get("Self") or {}
        ips = self_info.get("TailscaleIPs") or []
        ipv4 = next((ip for ip in ips if "." in ip), None)
        if not ipv4:
            return None
        dns_name = (self_info.get("DNSName") or "").rstrip(".")
        return {"ip": ipv4, "dnsName": dns_name or None}
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
# 2026-09-10新規（にこそく@nicosokufx X投稿 自動取得・市場分析連携）：X公式API v2のBearer
# Token。未設定でもアプリ全体は正常動作し、この機能だけがX_SOURCE_STATUS=DEGRADEDになる
# （指示書19番）。スクレイピング・ログイン回避等は実装しない（指示書1番、公式APIのみ使用）。
X_API_BEARER_TOKEN = os.environ.get("X_API_BEARER_TOKEN") or _SECRETS.get("x_api_bearer_token", "")
NICOSOKU_X_USERNAME = os.environ.get("NICOSOKU_X_USERNAME") or _SECRETS.get("nicosoku_x_username", "nicosokufx")
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
    # 2026-09-08新規（ニュース・材料連携改善の続き：ユーザーの後場レビューJSON、
    # app_improvement_requests「SECTOR_RELATIVE_STRENGTH」対応）：日経平均・TOPIXだけでなく
    # 半導体セクターの実勢を測る代表ETF（200A＝NEXT FUNDS 日経半導体35 ETF、東証上場）を
    # 既存のINDEX/get_index_quotes()パイプラインにそのまま追加するだけで、新しい取得経路は
    # 作らない（/api/quotesが自動的にこの値も返すようになる）。
    "nikkei_semi": "200A.T",
    # 2026-09-10新規（朝一マーケット自動分析システム）：TOPIX・グロース250は無料で取れる
    # 生の指数値ティッカーがYahoo Financeに存在しないため、連動ETF（1306＝野村TOPIX連動型
    # 上場投信、2516＝NEXT FUNDS 東証グロース市場250 ETF）の価格を「変化率の代理指標」
    # として使う（絶対値をTOPIXの実際のポイント数であるかのように見せない、UI側で
    # 「ETF代理」であることを明示する）。日経平均VI（2036）は出来高が薄く取得できない日も
    # ある（過去にFear&Greed同様の理由で撤去した経緯があるためsource_statusで正直に
    # failed/staleを返す設計にする、CLAUDE.md「撤去済み機能」参照）。
    "topix_etf": "1306.T", "growth250_etf": "2516.T", "vix": "^VIX", "nikkei_vi_etn": "2036.T",
    "eurjpy": "EURJPY=X", "dxy": "DX-Y.NYB", "nasdaq_fut": "NQ=F", "brent": "BZ=F",
}
# 2026-09-10新規：登録銘柄のうちADR（米国預託証券）が存在する主要銘柄のみのYahoo Finance
# ティッカー対応表（網羅的ではない、無ければ「ADRなし」として扱うだけで失敗にはしない）。
ADR_TICKER_MAP = {
    "7203": "TM", "6758": "SONY", "7267": "HMC", "8306": "MUFG", "8316": "SMFG",
    "8591": "IX", "8035": "TOELY", "6501": "HTHIY", "4568": "DSNKY",
    "8031": "MITSY", "8001": "ITOCY", "9984": "SFTBY", "6861": "KYCCF",
}  # 動作確認済みティッカーのみ（NTT・三菱商事はYahoo Finance上でADR銘柄が見つからず対象外）


# ============================================================
# 外部データ取得レート制限耐性（2026-09-10新規、Market Intelligence Timeline専用）。
# 既存の/api/quotes（メインダッシュボード、get_index_quotes）・登録銘柄の通常リアルタイム
# 取得等には一切影響を与えない設計にする——ここで追加するキャッシュ/リトライは、
# _fetch_index_snapshot・fetch_adr_snapshot・get_stock_quotes(cache_ttl指定時のみ)・
# _intraday_regime(cache_ttl指定時のみ)経由でMorningMarketCheck・Market Intelligence
# Timelineの呼び出しにのみ適用される（既存呼び出し元はcache_ttl省略＝0のままなので
# 挙動が変わらない）。「レポートが出ない」より「一部データ不足と明示してレポートを出す」を
# 優先する（ユーザー指示）。
# ============================================================
CACHE_TTL = {  # 用途別キャッシュTTL（秒）。指示書2番の目安レンジの中央値付近を採用。
    "index": 45,        # 指数/為替/先物/KOSPI：30〜60秒
    "stock5m": 90,       # 個別株5分足（VWAP・構造判定）：60〜120秒
    "stock_quote": 90,   # 個別株現在値（get_stock_quotes）：60〜120秒
    "news": 420,         # ニュース/カタリスト：5〜10分（中央値7分）
    "sector": 90,        # セクター集計：60〜120秒（indices由来のため実質indexキャッシュに従属）
}
_CACHE_LOCK = threading.Lock()
_CACHE_STORE = {}  # key -> {"value": ..., "ts": epoch秒}


def _cache_get(key):
    with _CACHE_LOCK:
        return _CACHE_STORE.get(key)


def _cache_set(key, value):
    with _CACHE_LOCK:
        _CACHE_STORE[key] = {"value": value, "ts": time.time()}


def _cache_fresh(entry, ttl):
    return entry is not None and (time.time() - entry["ts"]) <= ttl


def _is_rate_limit_error(e):
    """yfinance/curl_cffi等が投げる429系エラーをメッセージから判定する（構造化された
    例外型が無いため文字列判定、指示書3・4番）。"""
    msg = str(e)
    return ("Too Many Requests" in msg or "429" in msg or "Rate limited" in msg
            or "rate limit" in msg.lower())


def _retry_on_rate_limit(fetch_fn, max_retries=2, backoff_base=1.0):
    """429/レート制限系のエラーだけexponential backoffで最大max_retries回まで再試行する
    （指示書4番：無限リトライ禁止、レート制限以外は即座に諦める）。Retry-Afterヘッダー相当の
    情報はyfinance例外に構造化されて含まれないため、指数バックオフ（1秒→2秒→4秒、上限5秒）
    で代用する。戻り値：(値, None)成功時 / (None, 例外)全滅時。"""
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            return fetch_fn(), None
        except Exception as e:
            last_exc = e
            if _is_rate_limit_error(e) and attempt < max_retries:
                time.sleep(min(backoff_base * (2 ** attempt), 5))
                continue
            break
    return None, last_exc


def _cached_two_closes(cache_key, sym, ttl):
    """_two_closes()をキャッシュ+リトライ+stale fallbackでラップする（指数/為替/先物/KOSPI/
    ADR共通、指示書1・2・3・4番）。戻り値：(value_dict_or_None, cache_status)。
    cache_status："ok"（新規取得 or TTL内キャッシュ）|"stale_cache"（期限切れキャッシュを代用）|
    "rate_limited"（レート制限で取得不能・キャッシュも無し）|"failed"（レート制限以外の失敗）。"""
    entry = _cache_get(cache_key)
    if _cache_fresh(entry, ttl):
        return entry["value"], "ok"
    value, exc = _retry_on_rate_limit(lambda: _two_closes(sym))
    if value is not None:
        _cache_set(cache_key, value)
        return value, "ok"
    if exc is None:
        # 取得自体は成功したが対象データが無い（上場廃止・薄商い等）＝レート制限とは無関係の
        # 通常のデータ欠如。data_qualityのmissing/rate_limited扱いにはしない（"status"側の
        # failed/staleで既に表現されるため、cache_statusとしては"ok"のまま返す）。
        return None, "ok"
    if entry is not None:
        print(f"  [RateLimitGuard] {cache_key}：取得失敗のため期限切れキャッシュ（stale）を使用")
        return entry["value"], "stale_cache"
    if _is_rate_limit_error(exc):
        return None, "rate_limited"
    return None, "failed"


def _quality_summary(source_statuses):
    """data_health_json内に追加する指示書7番のdata_quality要約。source_statusesは
    {source_name: cache_status}（cache_statusは"ok"|"stale_cache"|"rate_limited"|"failed"|
    それ以外のstatus文字列）。stale_cacheのみならPARTIAL、rate_limited/failed（＝代替データも
    無く完全欠損）が1つでもあればDEGRADED、全て健全ならFULL。"""
    missing = [s for s, st in source_statuses.items() if st in ("failed", "missing", "no_data")]
    rate_limited = [s for s, st in source_statuses.items() if st == "rate_limited"]
    stale = [s for s, st in source_statuses.items() if st == "stale_cache"]
    if missing or rate_limited:
        quality = "DEGRADED"
    elif stale:
        quality = "PARTIAL"
    else:
        quality = "FULL"
    return {"quality": quality, "missing_sources": missing, "stale_sources": stale, "rate_limited_sources": rate_limited}


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


def _parse_stock_quote_frame(h):
    """yf.downloadが返す1銘柄分のDataFrameから、get_stock_quotesの戻り値1件分を作る
    （キャッシュ有無に関わらず共通で使うパース処理を切り出しただけ、ロジックは変更なし）。
    データが無ければNoneを返す。"""
    closes = h["Close"].dropna()
    if len(closes) == 0:
        return None
    t = round(float(closes.iloc[-1]), 2)
    p = round(float(closes.iloc[-2]), 2) if len(closes) >= 2 else None
    highs = h["High"].dropna()
    lows = h["Low"].dropna()
    opens = h["Open"].dropna()  # 2026-09-07新規（ポジション・リアルタイム売却判断）：当日始値。
    volumes = h["Volume"].dropna()
    turnover = float(t) * float(volumes.iloc[-1]) if len(volumes) and t is not None else None
    spark = [round(float(x), 2) for x in closes.tolist()[-20:]]  # 10-1章：カードUIのミニスパークライン用
    return {
        "t": t, "p": p,
        "open": round(float(opens.iloc[-1]), 2) if len(opens) else None,
        "high": round(float(highs.iloc[-1]), 2) if len(highs) else None,
        "low": round(float(lows.iloc[-1]), 2) if len(lows) else None,
        "volume": float(volumes.iloc[-1]) if len(volumes) else None,
        "turnover": turnover,
        "spark": spark,
    }


def get_stock_quotes(watchlist, cache_ttl=0, status_out=None):
    """登録銘柄それぞれの現在値(t)・前日終値(p)・当日高値(high)・当日安値(low)・売買代金(turnover)を返す。
    売買代金は 終値×出来高 で概算（セクターの並び替え用。4章の時価総額ソートから変更）。
    価格履歴と同じ history() の出来高列から計算するため、追加のAPI呼び出しは不要。
    2026-09-10新規（レート制限耐性）：cache_ttl>0を指定した呼び出し元（Market Intelligence
    Timeline・Morning Check）だけ、銘柄単位のキャッシュ+バッチ全滅時のリトライ+stale
    fallbackが有効になる。既存呼び出し元（メインダッシュボードの/api/stock-quotes等）は
    cache_ttl省略＝0のままなので、常に無条件で最新値を取りに行く既存動作を完全維持する
    （指示書「既存機能を壊さない」）。status_outを渡すとcode→cache_status（"ok"|
    "stale_cache"|"rate_limited"|"failed"）を書き込む（呼び出し側のdata_quality集計用）。"""
    out = {}
    if yf is None:
        return out
    items = [(w.get("code", ""), _yf_symbol(w)) for w in watchlist if w.get("code")]
    if not items:
        return out

    to_fetch = items
    if cache_ttl > 0:
        to_fetch = []
        for code, sym in items:
            entry = _cache_get(f"stockquote:{sym}")
            if _cache_fresh(entry, cache_ttl):
                out[code] = entry["value"]
                if status_out is not None:
                    status_out[code] = "ok"
            else:
                to_fetch.append((code, sym))

    if to_fetch:
        symbols = [sym for _, sym in to_fetch]

        def _download_all():
            frames = {}
            for i in range(0, len(symbols), STOCK_QUOTES_CHUNK):
                chunk = symbols[i:i + STOCK_QUOTES_CHUNK]
                data = _download_chunk(chunk)
                if data is None:
                    raise RuntimeError("stock quotes chunk download failed")
                for sym in chunk:
                    try:
                        sub = data[sym]
                        if sub is not None and not sub.empty:
                            frames[sym] = sub
                    except Exception:
                        pass  # このシンボルだけ結果に含まれなかった（上場廃止・シンボル誤り等）
            return frames

        if cache_ttl > 0:
            frames, exc = _retry_on_rate_limit(_download_all)
            frames = frames or {}
        else:
            # 既存動作を完全維持：リトライせず1回だけ（失敗時は握りつぶして空扱い、従来通り）
            try:
                frames = _download_all()
            except Exception as e:
                print("  一括取得失敗（リトライ無効経路）", e)
                frames = {}
            exc = None

        for code, sym in to_fetch:
            h = frames.get(sym)
            quote = None
            if h is not None:
                try:
                    quote = _parse_stock_quote_frame(h)
                except Exception as e:
                    print("  個別銘柄失敗", code, sym, e)
            if quote is not None:
                out[code] = quote
                if cache_ttl > 0:
                    _cache_set(f"stockquote:{sym}", quote)
                if status_out is not None:
                    status_out[code] = "ok"
            elif cache_ttl > 0 and exc is not None:
                # バッチ取得自体が例外で全滅した場合のみキャッシュへフォールバックする。
                # 個別銘柄がデータ無し（上場廃止等、バッチ自体は成功）の場合はレート制限とは
                # 無関係の通常のデータ欠如のため、ここには来ない（statusは既存通りokのまま扱う）。
                stale_entry = _cache_get(f"stockquote:{sym}")
                if stale_entry is not None:
                    out[code] = stale_entry["value"]
                    print(f"  [RateLimitGuard] stockquote:{sym}：取得失敗のため期限切れキャッシュ（stale）を使用")
                    if status_out is not None:
                        status_out[code] = "stale_cache"
                elif status_out is not None:
                    status_out[code] = "rate_limited" if _is_rate_limit_error(exc) else "failed"
            elif status_out is not None:
                status_out[code] = "ok"  # バッチは成功したがこの銘柄だけデータ無し（従来通り欠損扱い、quality影響なし）

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
        if v.get("open") is not None:  # 2026-09-07新規（ポジション・リアルタイム売却判断）
            out[code]["open"] = v["open"]
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
    # 2026-09-08追加（ニュース優先順位改善、指示書ir_expansion対応）：決算・配当・自社株買い
    # 以外の重要IRカテゴリ（M&A・業務提携・大型受注・新製品・設備投資・工場新設・KPI・
    # 中期経営計画・経営方針・訴訟/行政処分・人事）を追加し、企業公式情報の取得件数を増やす。
    "M&A", "買収", "合併", "業務提携", "資本提携", "資本業務提携", "TOB", "公開買付",
    "大量保有", "受注", "大型受注", "新製品", "新サービス", "設備投資", "工場新設", "増産",
    "生産能力", "KPI", "中期経営計画", "中計", "経営方針", "経営計画", "訴訟", "提訴",
    "行政処分", "業務改善命令", "人事異動", "役員人事", "代表取締役",
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
# 2026-09-08時点：build_stock_news()の絞り込みは_is_ir_news()（IR_KEYWORDS全体）に切り替えた
# ため、この決算限定の判定は現在どこからも呼ばれていない（削除はせず残置。決算のみに絞りたい
# 用途が将来出た場合のため）。
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
                # 2026-09-08更新（ニュース優先順位改善、指示書ir_expansion対応）：決算関連のみ
                # （_is_earnings_title、5キーワード）から、IR_KEYWORDS全体（決算・配当・
                # 自社株買い・M&A・業務提携・大型受注・新製品・設備投資・中期経営計画等）へ
                # 判定を広げ、登録銘柄の企業公式情報の取得件数を増やす。build_disclosure_news
                # （適時開示タブ）は元々種類を問わず全件のため変更不要。
                if not _is_ir_news(e["title"]):
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
# 2026-09-08更新：広告系とスポーツ系を別リストに分離した（スポーツ系だけ、経営文脈があれば
# 除外しない「ビジネスoverride」を適用できるようにするため。指示書4番：球団事業の売却・再編・
# スポンサー契約・球場関連投資・業績への影響等は除外しない）。
STOCK_NAME_NEWS_EXCLUDE_KEYWORDS_PROMO = [
    # 広告・セール・商品レビュー・お買い得情報のまとめ記事
    "セール", "プライムデー", "タイムセール", "クーポン", "割引", "％オフ", "%オフ", "ポイント還元",
    "PR", "広告", "キャンペーン", "送料無料", "福袋", "初売り", "ブラックフライデー", "サイバーマンデー",
    "レビュー", "開封", "おすすめ", "ランキング", "まとめ買い", "本日限定", "特価", "お買い得", "特別価格",
    "ベストセラー",
]
# プロ野球・スポーツ結果（楽天イーグルス・ソフトバンクホークス・日本ハムファイターズ・
# 鹿島アントラーズ等、社名とチーム名が重なるため。2026-08-21 ユーザー指摘：日本ハム(2282)・
# 鹿島(1812)のスポーツニュース混入が残っていたため追加調査のうえ拡充。試合結果記事は見出しに
# 「野球」「サッカー」等の一般語を含まないことが多く（例：「日本ハム・清宮虎、移籍後初登板も
# サヨナラ負け」）、実際にヒットした見出しから頻出語を拾って追加した。
# 2026-09-08further追加（ユーザー指摘：「9月8日 ソフトバンク―日本ハム23回戦 写真特集」が
# 2282日本ハムに混入。既存語（野球・ファイターズ・ホークス等）はこの見出しに含まれておらず
# すり抜けていたため、実際の見出しから頻出語を追加）。
STOCK_NAME_NEWS_EXCLUDE_KEYWORDS_SPORTS = [
    "プロ野球", "野球", "イーグルス", "ホークス", "ファイターズ", "甲子園", "高校野球",
    "Jリーグ", "J1リーグ", "J2リーグ", "明治安田", "パ・リーグ", "セ・リーグ", "サッカー", "アントラーズ",
    "サヨナラ", "1軍", "2軍", "登板", "先発", "被安打", "ユース", "サンケイスポーツ", "FOOTBALL ZONE",
    "高校サッカードットコム",
    "試合", "回戦", "写真特集", "投手", "打者", "本塁打", "ホームラン", "勝利", "敗戦", "球場",
]
# 2026-09-08新規（指示書4番）：スポーツキーワード・対戦カード形式に該当しても、経営に関係する
# 文脈があれば除外しない（球団事業の売却・再編、スポンサー契約、球場関連投資、業績への影響、
# IR・決算でのスポーツ事業言及等）。あくまでSTOCK_NAME_NEWS_EXCLUDE_KEYWORDS_SPORTS／
# 対戦カード形式で「スポーツ記事らしい」と判定された場合にだけ働く安全弁（広告系の除外には
# 適用しない＝セール記事等がこのリストの語を含むだけでは復活しない）。
STOCK_NAME_NEWS_SPORTS_BUSINESS_OVERRIDE = [
    "決算", "業績", "IR", "適時開示", "売却", "譲渡", "再編", "スポンサー", "契約", "投資",
    "子会社", "事業", "黒字", "赤字", "買収", "出資", "上方修正", "下方修正", "説明会",
]


def _is_sports_matchup_title(title, name):
    """「ソフトバンク―日本ハム」のような対戦カード形式（自社名がダッシュで別の語と直接
    連結されている）を検出する（指示書3番）。自社名そのものをスポーツ判定の根拠にすると
    本業ニュースまで誤って除外しかねないため、あくまで「ダッシュで直結されている」という
    構造だけを見る（相手側の固有名詞は問わない）。通常の企業ニュース見出しで社名の直前・
    直後にいきなりダッシュが来ることは稀なため、単独では誤検出リスクが低い
    （STOCK_NAME_NEWS_SPORTS_BUSINESS_OVERRIDEとの併用が前提）。"""
    if not name or not title:
        return False
    norm_title = unicodedata.normalize("NFKC", title)
    esc = re.escape(unicodedata.normalize("NFKC", name))
    pattern = rf"(\S[―－ー\-]\s*{esc})|({esc}\s*[―－ー\-]\s*\S)"
    return re.search(pattern, norm_title) is not None

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


def _is_promo_news(title, source="", name=""):
    # 2026-08-22 ユーザー指摘対応：「Ｊリーグ」のように全角英字で書かれた見出しは、半角の
    # "Jリーグ"キーワードでは一致しないまま素通りしていた。NFKC正規化（全角英数→半角）で
    # 比較してから判定することで、全角/半角どちらの表記でも確実に弾けるようにする。
    norm_title = unicodedata.normalize("NFKC", title)
    if any(unicodedata.normalize("NFKC", k) in norm_title for k in STOCK_NAME_NEWS_EXCLUDE_KEYWORDS_PROMO):
        return True
    if any(s == source for s in STOCK_NAME_NEWS_EXCLUDE_SOURCES):
        return True
    # 2026-08-21 ユーザー指摘対応：Yahoo!ニュース等の集約媒体はsourceが「Yahoo!ニュース」に
    # なり、実際の配信元（東スポWEB・サンケイスポーツ等）は見出し末尾に「（〇〇）」として
    # 埋め込まれるだけのため、上のsource完全一致だけでは弾けない。見出し中にスポーツ媒体名が
    # 含まれていないかも追加でチェックする。
    if any(unicodedata.normalize("NFKC", s) in norm_title for s in STOCK_NAME_NEWS_EXCLUDE_SOURCES):
        return True
    # 2026-09-08新規（登録銘柄ニュースの誤紐付け対策）：スポーツキーワード一致、または
    # 「ソフトバンク―日本ハム」のような対戦カード形式（_is_sports_matchup_title）のいずれかで
    # 「スポーツ記事らしい」と判定された場合のみ、経営文脈（STOCK_NAME_NEWS_SPORTS_BUSINESS_
    # OVERRIDE）が無いか確認したうえで除外する。経営文脈があれば除外しない（指示書4番：球団
    # 事業の売却・再編・スポンサー契約・球場関連投資・業績への影響等）。
    sports_hit = any(unicodedata.normalize("NFKC", k) in norm_title for k in STOCK_NAME_NEWS_EXCLUDE_KEYWORDS_SPORTS)
    matchup_hit = _is_sports_matchup_title(title, name)
    if not (sports_hit or matchup_hit):
        return False
    if any(k in norm_title for k in STOCK_NAME_NEWS_SPORTS_BUSINESS_OVERRIDE):
        return False
    return True


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
    セール告知等のPR記事・スポーツ結果記事（経営文脈が無いもの）は_is_promo_news()で除外する。
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
                   if _title_mentions_name(name, it["title"]) and not _is_promo_news(it["title"], it.get("source", ""), name)]
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


# ============================================================
# 朝一マーケット自動分析システム（MorningMarketCheck）。2026-09-10新規。
# 指示書の方針通り、取得（market_data_service）と分析（morning_analysis_engine）を分離する。
# 既存のINDEX/get_index_quotes・_market_risk_score・get_stock_quotes・投資判断エンジン
# （investment_db.relevant_catalysts_for等）を可能な限り再利用し、新しい取得経路・
# 判定基準を無闇に増やさない。
# ============================================================

MORNING_CHECK_SNAPSHOT_TIMES = {  # 指示書2番：将来変更しやすいよう定数化（JST、24h表記）
    "T0530": "05:30", "T0700": "07:00", "T0800": "08:00", "T0830": "08:30", "T0850": "08:50",
}
MORNING_CHECK_FINAL_SNAPSHOT = "T0850"


# ---- market_data_service：取得のみ、分析ロジックを含まない ----

def fetch_adr_snapshot(codes, status_out=None):
    """登録銘柄コードのうちADR_TICKER_MAPに存在するものだけADR変動率を取得する（指示書3G番）。
    存在しない銘柄はスキップするだけで失敗として扱わない。2026-09-10更新（レート制限耐性）：
    キャッシュ+リトライ+stale fallbackを適用（Morning Check・Market Intelligence Timeline
    共通、指示書1・2・3・4番）。status_outを渡すとcode→cache_statusを書き込む（呼び出し側の
    data_quality集計用、省略時は何もしない）。"""
    out = {}
    if yf is None:
        return out
    for code in codes:
        adr_ticker = ADR_TICKER_MAP.get(code)
        if not adr_ticker:
            continue
        r, cache_status = _cached_two_closes(f"adr:{code}", adr_ticker, CACHE_TTL["index"])
        if status_out is not None:
            status_out[code] = cache_status
        if r and r.get("t") is not None and r.get("p"):
            pct = (r["t"] - r["p"]) / r["p"] * 100
            out[code] = {"adrTicker": adr_ticker, "adrPct": round(pct, 2), "adrClose": r["t"], "status": "ok",
                         "cacheStatus": cache_status}
    return out


def fetch_fear_greed():
    """Fear & Greed Index（CNN公開データ）。CLAUDE.md記載の通り、過去に不安定なスクレイピング
    処理のため撤去した経緯がある。無料の公式APIが存在しないため、ここでは推測値を作らず
    常にNone/failedを返す設計にする（指示書15番「取得不能はnull、推測禁止」を厳守）。
    将来ユーザーが信頼できる取得先を用意できた場合にこの関数だけ差し替えれば良い設計
    （market_data_service/morning_analysis_engineの分離により、分析ロジック側の変更は
    不要）。"""
    return {"value": None, "label": None, "status": "failed",
            "note": "Fear&Greedは無料の公式APIが無いため未取得（推測値は作らない）"}


def _fetch_index_snapshot(keys):
    """INDEX辞書のうち指定キーだけを取得し、timestamp・status付きで返す（指示書15番：
    データ品質の追跡）。既存get_index_quotes()を全キー分呼ぶと無駄なので、必要な分だけ
    個別に_two_closes()する。2026-09-10更新（レート制限耐性）：キャッシュ+リトライ+
    stale fallbackを適用（指示書1・2・3・4番）。既存の"status"（ok/stale/failed、値の中身に
    基づく判定）は変えず、新たに"cache_status"（ok/stale_cache/rate_limited/failed、取得経路
    自体の健全性）を追加する——両者は意味が異なるため混同しないこと。"""
    out = {}
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    for key in keys:
        sym = INDEX.get(key)
        if not sym:
            continue
        r, cache_status = _cached_two_closes(f"index:{key}", sym, CACHE_TTL["index"])
        if r and r.get("t") is not None:
            pct = ((r["t"] - r["p"]) / r["p"] * 100) if r.get("p") else None
            # nikkei_vi_etn等、前日値が取れない（薄商い）場合は値はあってもchange不明＝stale扱い
            status = "ok" if r.get("p") is not None else "stale"
            out[key] = {"value": r["t"], "prevClose": r.get("p"), "changePct": round(pct, 2) if pct is not None else None,
                        "timestamp": now_iso, "status": status, "symbol": sym, "cache_status": cache_status}
        else:
            out[key] = {"value": None, "prevClose": None, "changePct": None, "timestamp": now_iso,
                        "status": "failed", "symbol": sym, "cache_status": cache_status}
    return out


# ---- morning_analysis_engine：既に取得済みのデータから判断を組み立てる（新規APIは呼ばない）----

def _morning_volatility_score(indices):
    """VIX・日経VI（取得できれば）からボラティリティスコア（0〜100、高いほど高ボラ）を作る。
    指示書6番のvolatility_scoreに対応。VIXが主指標、日経VIは補助（取得不能な日が多いため）。"""
    vix = indices.get("vix", {})
    score, missing = 30, []
    v = vix.get("value")
    if v is not None:
        # VIX目安：<15落ち着き、15-20平常、20-25やや高い、25-35高い、35+極端
        if v >= 35: score = 95
        elif v >= 25: score = 75
        elif v >= 20: score = 55
        elif v >= 15: score = 35
        else: score = 15
    else:
        missing.append("VIX")
    nvi = indices.get("nikkei_vi_etn", {})
    if nvi.get("status") == "ok" and nvi.get("changePct") is not None and nvi["changePct"] > 5:
        score = min(100, score + 10)
    return max(0, min(100, score)), missing


def _morning_macro_pressure_score(indices, commodities):
    """金利・為替・原油から「マクロ的な逆風」の強さ（0〜100）を作る（指示書6番の
    macro_pressure_score）。米金利上昇＋ドル円急変動＋原油急騰を加点する単純ルールベース。"""
    score = 30
    us10y = indices.get("us10y", {})
    if us10y.get("changePct") is not None and us10y["changePct"] >= 2:
        score += 25
    usdjpy = indices.get("usdjpy", {})
    if usdjpy.get("changePct") is not None and abs(usdjpy["changePct"]) >= 1.0:
        score += 15
    wti = commodities.get("wti", {})
    if wti.get("changePct") is not None and wti["changePct"] >= 3:
        score += 20
    return max(0, min(100, score))


def _morning_trend_score(indices):
    """米指数・日本先物のトレンド方向を単純平均してtrend_score（0〜100、高いほど上昇トレンド
    優勢）にする。"""
    pcts = []
    for k in ("dow", "nasdaq", "sp500", "sox", "nikkei_fut"):
        v = indices.get(k, {}).get("changePct")
        if v is not None:
            pcts.append(v)
    if not pcts:
        return 50, ["米指数/日経先物"]
    avg = sum(pcts) / len(pcts)
    # ±2%を振り切りとみなして0-100へ線形マップ（中心50）
    score = max(0, min(100, round(50 + avg * 25)))
    return score, []


def classify_morning_regime(risk_score, volatility_score, indices):
    """market_regime・volatility_regime・trend_typeを分けて判定する（指示書5番：1つに
    無理やり固定しない）。risk_scoreは既存_market_risk_score()の0-100（高いほどRISK OFF）を
    そのまま使う。"""
    if risk_score is None:
        market_regime = "NEUTRAL"
    elif risk_score >= 76: market_regime = "RISK_OFF"
    elif risk_score >= 60: market_regime = "MILD_RISK_OFF"
    elif risk_score <= 20: market_regime = "RISK_ON"
    elif risk_score <= 40: market_regime = "MILD_RISK_ON"
    else: market_regime = "NEUTRAL"

    if volatility_score is not None and volatility_score >= 70:
        volatility_regime = "HIGH"
        if market_regime in ("NEUTRAL", "MILD_RISK_ON", "MILD_RISK_OFF"):
            market_regime = "HIGH_VOLATILITY"
    elif volatility_score is not None and volatility_score >= 45:
        volatility_regime = "ELEVATED"
    else:
        volatility_regime = "LOW"

    sox = indices.get("sox", {}).get("changePct")
    dow = indices.get("dow", {}).get("changePct")
    if sox is not None and dow is not None and abs(sox - dow) >= 1.5:
        trend_type = "selective_strength"  # 指数間で強弱がバラつく＝選別相場
    elif market_regime in ("RISK_ON", "MILD_RISK_ON"):
        trend_type = "broad_strength"
    elif market_regime in ("RISK_OFF", "MILD_RISK_OFF"):
        trend_type = "broad_weakness"
    else:
        trend_type = "mixed"
    return market_regime, volatility_regime, trend_type


_SECTOR_PROXY_HINTS = {
    "energy": ["INPEX", "石油", "商社"], "trading_companies": ["商社"],
    "semiconductor": ["半導体"], "semiconductor_equipment": ["半導体製造装置"],
    "high_per_growth": ["グロース", "成長株"], "defense": ["防衛"],
}


def compute_macro_sector_strength(indices, commodities):
    """マクロ指標（原油・金利・SOX・ADR等）から当日の強い/弱いセクターを推定する（指示書7番）。
    個別銘柄の強弱と混同しないよう、あくまでマクロ要因からの推定に限定し、既存ADR/ニュース等の
    個別材料は別枠（generate_morning_watchlist_focus側）で扱う。"""
    strong, weak = [], []
    wti = commodities.get("wti", {}).get("changePct")
    brent = commodities.get("brent", {}).get("changePct")
    oil_up = (wti is not None and wti >= 2) or (brent is not None and brent >= 2)
    oil_down = (wti is not None and wti <= -2) or (brent is not None and brent <= -2)
    if oil_up:
        strong += ["energy", "trading_companies"]
    if oil_down:
        weak.append("energy")
    us10y = indices.get("us10y", {}).get("changePct")
    if us10y is not None and us10y >= 2:
        weak.append("high_per_growth")
        strong.append("bank")
    elif us10y is not None and us10y <= -2:
        strong.append("high_per_growth")
    sox = indices.get("sox", {}).get("changePct")
    if sox is not None and sox >= 1.5:
        strong.append("semiconductor")
    elif sox is not None and sox <= -1.5:
        weak.append("semiconductor")
    return list(dict.fromkeys(strong)), list(dict.fromkeys(weak))


# 2026-09-10更新（損切りルール是正・最優先修正）：旧HARD_STOP_APPROACHING_PCT(-8%警告)/
# HARD_STOP_TRIGGER_PCT(-10%強制、SWING限定)はユーザーの実際の運用ルールと不一致だった
# ため撤去。以後は investment_db.get_position_risk_rules()/evaluate_position_risk_tier()
# （唯一の共通設定、trade-cockpit.html側にも同じ値をミラーしてある）を全トレードスタイル
# 共通で参照する。


def evaluate_position_risk_warnings(database_url, user_id, stock_quotes):
    """保有ポジションの損切りルール接近・到達を判定する（指示書11・16番、2026-09-10是正）。
    -6%WATCH/-7%WARNING/-8%EXITを全トレードスタイル共通で適用する（自信度・材料・
    ファンダ・AI分析結果に関わらず、-8%到達時点では一旦売却を促す＝EXIT RULE）。
    投資判断がまだ有効なら、さらに下落した後の再エントリーは「別トレード」として
    検討してよい（allow_reentry・reentry_requires_new_decision、rules内に保持）。"""
    if investment_db is None or not database_url:
        return []
    rules = investment_db.get_position_risk_rules(database_url, user_id)
    positions = investment_db.list_portfolio(database_url, user_id)
    warnings = []
    for p in positions:
        code = p.get("code")
        avg = p.get("average_price")
        current = (stock_quotes.get(code) or {}).get("t")
        if avg is None or current is None or not avg:
            continue
        pnl_pct = (current - avg) / avg * 100
        tier = investment_db.evaluate_position_risk_tier(pnl_pct, rules)
        if not tier:
            continue
        if tier == "EXIT":
            message = (f"🚨 EXIT RULE\n\n買値から{rules['max_loss_pct']:.1f}%に到達しました。\n\n"
                       f"「売却」\n\n投資判断がまだ有効でも一旦撤退してください。"
                       f"再エントリーは新しいトレードとして判断します。")
            level = "CRITICAL"
        elif tier == "WARNING":
            message = f"損切りライン（{rules['max_loss_pct']:.1f}%）に接近しています（現在{pnl_pct:.1f}%）。"
            level = "WARNING"
        else:  # WATCH
            message = f"含み損がやや拡大しています（現在{pnl_pct:.1f}%）。"
            level = "WATCH"
        warnings.append({"code": code, "name": p.get("name"), "pnlPct": round(pnl_pct, 2),
                          "level": level, "tier": tier, "message": message})
    return warnings


def generate_morning_watchlist_focus(database_url, user_id, market_data, cache_ttl=0, status_out=None):
    """登録銘柄の朝の注目TOP5・追わない銘柄・地合い耐性銘柄を生成する（指示書4・8・10番）。
    重い日足履歴取得（analyze_stock等）は使わず、get_stock_quotes（既存の軽量な当日値取得）＋
    ADR＋既存のカタリスト/イベント関連度判定（investment_db.relevant_catalysts_for・
    upcoming_event_signals、判断エンジンから再利用）だけでスコアリングする。
    2026-09-10新規（レート制限耐性）：cache_ttl>0を指定した呼び出し元（Market Intelligence
    Timeline）だけget_stock_quotes/fetch_adr_snapshotのキャッシュが有効になる。既存呼び出し元
    （Morning Check）はcache_ttl省略＝0のままなので既存動作を完全維持する（指示書「既存機能を
    壊さない」）。status_outを渡すと{"watchlist_quotes":cache_status,"adr":cache_status}を
    書き込む（呼び出し側のdata_quality集計用）。"""
    if investment_db is None or not database_url:
        return {"top5": [], "avoid": [], "resilience": []}
    watchlist = investment_db.list_watchlist(database_url, user_id, market="JP")
    if not watchlist:
        return {"top5": [], "avoid": [], "resilience": []}
    quote_status = {}
    quotes = get_stock_quotes(watchlist, cache_ttl=cache_ttl, status_out=quote_status if cache_ttl > 0 else None)
    nikkei_chg = market_data.get("indices", {}).get("nikkei", {}).get("changePct")
    adr_status = {}
    adr = fetch_adr_snapshot([w["code"] for w in watchlist], status_out=adr_status if cache_ttl > 0 else None)
    if status_out is not None and cache_ttl > 0:
        # 個別銘柄単位のcache_statusのうち最も悪いもの（rate_limited>failed>stale_cache>ok）を代表値にする
        _rank = {"rate_limited": 3, "failed": 2, "stale_cache": 1, "ok": 0}
        worst = lambda d: max(d.values(), key=lambda v: _rank.get(v, 0)) if d else "ok"
        status_out["watchlist_quotes"] = worst(quote_status)
        status_out["adr"] = worst(adr_status)

    # セクター平均（対セクター計算用、既存run_momentum_stage1のsector_avgと同じ考え方の軽量版）
    sector_sum, sector_count = {}, {}
    rows = []
    for w in watchlist:
        code = w.get("code")
        q = quotes.get(code)
        if not q or q.get("t") is None or not q.get("p"):
            continue
        chg = (q["t"] - q["p"]) / q["p"] * 100
        rows.append({"code": code, "name": w.get("name"), "sector": w.get("sector"), "changePct": chg,
                      "turnover": q.get("turnover"), "high": q.get("high"), "low": q.get("low"),
                      "current": q.get("t")})
        if w.get("sector"):
            sector_sum[w["sector"]] = sector_sum.get(w["sector"], 0.0) + chg
            sector_count[w["sector"]] = sector_count.get(w["sector"], 0) + 1
    sector_avg = {s: sector_sum[s] / sector_count[s] for s in sector_sum}

    scored = []
    for row in rows:
        code = row["code"]
        market_rs = (row["changePct"] - nikkei_chg) if nikkei_chg is not None else None
        sector_rs = (row["changePct"] - sector_avg.get(row["sector"], row["changePct"])) if row.get("sector") else None
        row["marketRS"] = round(market_rs, 2) if market_rs is not None else None
        row["sectorRS"] = round(sector_rs, 2) if sector_rs is not None else None
        row["resilience"] = _rs_resilience_tier(
            {"changePct": row["changePct"], "marketRS": market_rs}, nikkei_chg)
        adr_row = adr.get(code)
        row["adrPct"] = adr_row["adrPct"] if adr_row else None
        catalysts = investment_db.relevant_catalysts_for(database_url, user_id, code=code, sector=row.get("sector"), limit=2)
        row["catalysts"] = catalysts
        events = investment_db.upcoming_event_signals(database_url, user_id, code=code, sector=row.get("sector"))
        row["eventSignals"] = events["signals"]

        score, reasons, risks = 0.0, [], []
        if market_rs is not None:
            score += max(-20, min(30, market_rs * 4))
            if market_rs >= 3:
                reasons.append(f"対市場+{market_rs:.1f}pt")
        if row["resilience"] == "STRONG":
            score += 25
            reasons.append("地合い逆行の強い相対強度（🛡地合い耐性）")
        elif row["resilience"] == "NORMAL":
            score += 12
        if adr_row:
            score += max(-15, min(20, adr_row["adrPct"] * 3))
            if adr_row["adrPct"] >= 2:
                reasons.append(f"ADR+{adr_row['adrPct']:.1f}%（先回り買い材料）")
        pos_cat = [c for c in catalysts if c.get("sentiment") == "positive" and c.get("freshness") in ("LIVE", "CURRENT")]
        neg_cat = [c for c in catalysts if c.get("sentiment") == "negative" and c.get("freshness") in ("LIVE", "CURRENT")]
        if pos_cat:
            score += 15
            reasons.append(f"好材料：{pos_cat[0].get('title','')[:20]}")
        if neg_cat:
            score -= 20
            risks.append(f"悪材料：{neg_cat[0].get('title','')[:20]}")
        if "EVENT_RISK_HIGH" in row["eventSignals"]:
            risks.append("重要イベント接近")
            score -= 10
        if row["changePct"] <= -3:
            risks.append("当日大幅安")
            score -= 15
        row["score"] = round(score, 1)
        row["reasons"] = reasons
        row["risks"] = risks
        scored.append(row)

    scored.sort(key=lambda r: -r["score"])
    top5_pool = [r for r in scored if r["score"] > 0][:5]
    top5 = []
    for i, r in enumerate(top5_pool):
        top5.append({
            "rank": i + 1, "code": r["code"], "name": r["name"], "score": round(r["score"]),
            "reason": r["reasons"] or ["総合スコア上位"], "risks": r["risks"],
            "current": r["current"], "changePct": round(r["changePct"], 2),
            "marketRS": r["marketRS"], "sectorRS": r["sectorRS"], "adrPct": r["adrPct"],
            "resilience": r["resilience"],
            "trigger": "寄り後VWAP維持＋5分足安値切り上げを確認してからのエントリーを推奨",
            "avoidCondition": "寄り天・出来高を伴わない上昇・悪材料の追加",
        })
    avoid = [{"code": r["code"], "name": r["name"], "reasons": r["risks"] or ["セクター/地合い逆風"]}
             for r in scored if r["score"] <= -15][:5]
    resilience_watch = [{"code": r["code"], "name": r["name"], "changePct": round(r["changePct"], 2),
                          "marketRS": r["marketRS"]} for r in scored if r["resilience"] == "STRONG"][:10]
    return {"top5": top5, "avoid": avoid, "resilience": resilience_watch}


def generate_morning_strategy(market_regime, volatility_regime, risk_score, event_signals):
    """今日の戦略を自動生成する（指示書13番）。構造化フィールド＋自然文の両方を返す。"""
    if market_regime in ("RISK_OFF", "HIGH_VOLATILITY"):
        primary, swing, entry_style, size = "daytrade", "avoid", "wait_for_confirmation", "reduced"
    elif market_regime in ("MILD_RISK_OFF",):
        primary, swing, entry_style, size = "daytrade", "selective", "wait_for_confirmation", "normal"
    elif market_regime in ("RISK_ON", "MILD_RISK_ON"):
        primary, swing, entry_style, size = "daytrade_and_swing", "allowed", "normal", "normal"
    else:
        primary, swing, entry_style, size = "daytrade", "selective", "normal", "normal"
    if "EVENT_RISK_HIGH" in (event_signals or []):
        swing = "avoid"
        size = "reduced"
    strategy = {"primary": primary, "swing": swing, "entry_style": entry_style, "position_size": size,
                "focus": "relative_strength", "avoid": "blind_dip_buying"}
    regime_text = {"RISK_ON": "リスクオン", "MILD_RISK_ON": "やや強気", "NEUTRAL": "中立",
                   "MILD_RISK_OFF": "やや弱気", "RISK_OFF": "リスクオフ", "HIGH_VOLATILITY": "高ボラティリティ"}.get(market_regime, market_regime)
    text = f"指数は{regime_text}。"
    if swing == "avoid":
        text += "スイングの新規は避け、デイトレード中心に。"
    elif swing == "selective":
        text += "スイングは厳選のみ、デイトレード中心に。"
    else:
        text += "デイトレード・スイングともに通常運用。"
    text += "指数の逆張りは避け、地合いに逆行して強い銘柄（相対強度）を優先する。"
    return strategy, text


def generate_morning_market_check(database_url, user_id, snapshot_time):
    """MorningMarketCheck1回分を生成・保存する（morning_report_serviceの中核）。データ単位で
    取得失敗しても全体を落とさない（指示書20番）。戻り値: 保存済みレコード（dict）。"""
    data_quality = {}
    now = datetime.datetime.now(datetime.timezone.utc)
    check_date = _jst_today_date_str() if "_jst_today_date_str" in globals() else datetime.date.today().isoformat()

    index_keys = ["dow", "nasdaq", "sp500", "sox", "nasdaq_fut", "nikkei", "nikkei_fut", "topix_etf",
                  "growth250_etf", "vix", "nikkei_vi_etn", "us10y", "usdjpy", "eurjpy", "dxy", "kospi"]
    try:
        indices = _fetch_index_snapshot(index_keys)
    except Exception as e:
        print("  MorningCheck: 指数取得で例外", e)
        indices = {}
    for k in index_keys:
        data_quality[k] = indices.get(k, {}).get("status", "failed")

    commodities = {k: indices[k] for k in ("wti", "gold", "brent") if k in indices}
    try:
        commodities.update(_fetch_index_snapshot(["wti", "gold", "brent"]))
    except Exception:
        pass
    for k in ("wti", "gold", "brent"):
        data_quality[k] = commodities.get(k, {}).get("status", "failed")

    fear_greed = fetch_fear_greed()
    data_quality["fear_greed"] = fear_greed["status"]

    try:
        watchlist_all = investment_db.list_watchlist(database_url, user_id, market="JP") if investment_db else []
        adr = fetch_adr_snapshot([w["code"] for w in watchlist_all])
        data_quality["adr"] = "ok" if adr else "no_data"
    except Exception as e:
        print("  MorningCheck: ADR取得で例外", e)
        adr, watchlist_all = {}, []
        data_quality["adr"] = "failed"

    volatility_score, vol_missing = _morning_volatility_score(indices)
    macro_pressure_score = _morning_macro_pressure_score(indices, commodities)
    trend_score, trend_missing = _morning_trend_score(indices)
    n225_trend = _index_trend(INDEX["nikkei"])
    nasdaq_trend = _index_trend(INDEX["nasdaq"])
    sox_trend = _index_trend(INDEX["sox"])
    us10y_trend = _index_trend(INDEX["us10y"])
    usdjpy_trend = _index_trend(INDEX["usdjpy"])
    risk = _market_risk_score(n225_trend, nasdaq_trend, sox_trend, us10y_trend, usdjpy_trend)
    market_risk_score = risk["score"]

    market_regime, volatility_regime, trend_type = classify_morning_regime(market_risk_score, volatility_score, indices)
    strong_sectors, weak_sectors = compute_macro_sector_strength(indices, commodities)

    try:
        event_info = investment_db.upcoming_event_signals(database_url, user_id) if investment_db else {"events": [], "signals": []}
    except Exception as e:
        print("  MorningCheck: イベント取得で例外", e)
        event_info = {"events": [], "signals": []}

    try:
        focus = generate_morning_watchlist_focus(database_url, user_id, {"indices": indices})
        data_quality["watchlist"] = "ok"
    except Exception as e:
        print("  MorningCheck: 銘柄分析で例外", e)
        focus = {"top5": [], "avoid": [], "resilience": []}
        data_quality["watchlist"] = "failed"

    # 2026-09-10更新（Phase2-C「TOP5選考基準の全面見直し」）：watchlist_top5_jsonは、
    # ①当日騰落率がプラス（または逆行耐性例外）②VWAP/5分足構造/対市場RS等の根拠がある
    # ③ENTRY_READY/NOW_BUYABLE状態、の3条件を満たすentry_score版TOP5に置き換える（旧
    # generate_morning_watchlist_focus()の単純スコアではマイナス銘柄が混入し得たため）。
    # avoid/resilience（地合い耐性ランキング）は既存のfocusをそのまま使い続ける（この2つは
    # 今回の指示書の対象外、既存の別用途の判定のため無変更）。
    try:
        morning_entry_result = generate_morning_entry_top5(database_url, user_id)
        entry_ready_top5 = morning_entry_result["entryReadyTop5"]
        data_quality["entry_top5"] = "ok"
    except Exception as e:
        print("  MorningCheck: entry_score版TOP5算出で例外", e)
        entry_ready_top5 = []
        data_quality["entry_top5"] = "failed"
    watchlist_top5_json = [{
        "code": c["code"], "name": c["name"], "rank": c["rank"],
        "entryScore": c["entryScore"], "score": c["entryScore"], "entryState": c["entryState"],
        "reason": c["reasons"], "risks": c["risks"], "current": c["current"],
        "changePct": c["changePct"], "marketRS": c["marketRS"], "resilience": c.get("resilience"),
        "analysisConfidence": c["analysisConfidence"], "dataQuality": c["dataQuality"],
        "trigger": "寄り後VWAP維持＋5分足安値切り上げを確認してからのエントリーを推奨",
        "avoidCondition": "寄り天・出来高を伴わない上昇・悪材料の追加",
    } for c in entry_ready_top5]

    try:
        stock_quotes_for_positions = get_stock_quotes(watchlist_all) if watchlist_all else {}
        # ポジション銘柄がwatchlist外の場合も拾えるよう、保有銘柄も追加取得する
        try:
            positions_all = investment_db.list_portfolio(database_url, user_id) if investment_db else []
            extra = [p for p in positions_all if p.get("code") not in stock_quotes_for_positions]
            if extra:
                stock_quotes_for_positions.update(get_stock_quotes(extra))
        except Exception:
            pass
        position_risk = evaluate_position_risk_warnings(database_url, user_id, stock_quotes_for_positions)
        data_quality["positions"] = "ok"
    except Exception as e:
        print("  MorningCheck: ポジションリスク判定で例外", e)
        position_risk = []
        data_quality["positions"] = "failed"

    strategy, strategy_text = generate_morning_strategy(market_regime, volatility_regime, market_risk_score, event_info["signals"])

    risk_warnings = []
    vix_val = indices.get("vix", {}).get("value")
    if vix_val is not None and vix_val >= 25:
        risk_warnings.append({"level": "WARNING" if vix_val < 35 else "CRITICAL", "message": f"VIX {vix_val}"})
    us10y_val = indices.get("us10y", {}).get("value")
    if us10y_val is not None and us10y_val >= 4.5:
        risk_warnings.append({"level": "WATCH", "message": f"米10年債 {us10y_val}%"})
    brent_val = commodities.get("brent", {}).get("value")
    if brent_val is not None and brent_val >= 100:
        risk_warnings.append({"level": "WATCH", "message": f"Brent {brent_val}ドル超"})
    if "EVENT_RISK_HIGH" in event_info["signals"]:
        risk_warnings.append({"level": "WARNING", "message": "重要イベントが目前"})
    if any(w["level"] == "CRITICAL" for w in position_risk):
        risk_warnings.insert(0, {"level": "CRITICAL", "message": "保有銘柄が損切りルールに到達"})

    payload = {
        "market_regime": market_regime, "volatility_regime": volatility_regime, "trend_type": trend_type,
        "market_risk_score": market_risk_score, "volatility_score": volatility_score,
        "trend_score": trend_score, "macro_pressure_score": macro_pressure_score,
        "indices_json": indices, "fx_json": {k: indices[k] for k in ("usdjpy", "eurjpy", "dxy") if k in indices},
        "commodities_json": commodities, "adr_json": adr, "data_quality_json": data_quality,
        "strong_sectors_json": strong_sectors, "weak_sectors_json": weak_sectors,
        "watchlist_top5_json": watchlist_top5_json, "avoid_stocks_json": focus["avoid"],
        "resilience_json": focus["resilience"], "risk_warnings_json": risk_warnings[:3],
        "event_risk_json": event_info["events"][:5], "position_risk_json": position_risk,
        "strategy_json": strategy, "strategy_text": strategy_text,
        "raw_payload_json": {"feargreed": fear_greed, "generatedAt": now.isoformat(), "missing": vol_missing + trend_missing,
                              "external_market_commentary": _nicosoku_morning_commentary_safe(database_url, user_id)},
    }
    saved = investment_db.save_morning_check(database_url, user_id, check_date, snapshot_time, payload) if investment_db else None
    # 朝TOP5をstock_thesesへ永続化（source='MORNING'固定、以後書き換えない成績評価用スナップ
    # ショット）。保存済みMorningCheckのidをmorning_check_idとして使う（指示書11・12番）。
    if saved and entry_ready_top5:
        try:
            persist_morning_theses(database_url, user_id, check_date, saved.get("id"), entry_ready_top5)
        except Exception as e:
            print("  MorningCheck: 朝TOP5 thesis永続化で例外", e)
    return saved


def _morning_check_scheduler_users():
    """定時生成の対象ユーザー一覧。マルチユーザー設定（USERS）があればその全員、
    無ければ既存の後方互換ユーザー名（"matsuura"）1人だけ（既存の_LEGACY_OWNERと同じ値）。"""
    if USERS:
        return list(USERS.keys())
    return ["matsuura"]


def _is_jp_market_business_day(d):
    """土日はスキップする（指示書1番「祝日・休場日は生成しない」の最低限の実装）。
    日本の祝日カレンダーライブラリには依存していないため、平日だが東証休場の祝日
    （振替休日等）は判定できない点が既知の制約——実行時に指数取得が全滅した場合は
    データ品質欄（data_quality）に反映されるだけで、レポート自体は空値のまま保存される
    （捏造はしない）。"""
    return d.weekday() < 5


def _morning_check_scheduler_loop():
    """指示書2番の定時（05:30/07:00/08:00/08:30/08:50 JST）にMorningMarketCheckを自動生成する
    デーモンスレッド。60秒間隔でJST時刻をチェックし、対象時刻の分に一度だけ発火する
    （プロセス内メモリの発火済みセットで同一プロセス内の二重発火を防ぎ、DBのUNIQUE制約
    （user_id, check_date, snapshot_time）が最終防衛線としてさらに二重生成を防ぐ）。"""
    fired = set()  # {(check_date, snapshot_time, user_id)}
    JST = datetime.timezone(datetime.timedelta(hours=9))
    while True:
        try:
            now_jst = datetime.datetime.now(JST)
            hhmm = now_jst.strftime("%H:%M")
            if _is_jp_market_business_day(now_jst):
                for snapshot_time, target_hhmm in MORNING_CHECK_SNAPSHOT_TIMES.items():
                    if hhmm == target_hhmm:
                        check_date = now_jst.date().isoformat()
                        for user_id in _morning_check_scheduler_users():
                            key = (check_date, snapshot_time, user_id)
                            if key in fired:
                                continue
                            fired.add(key)
                            try:
                                generate_morning_market_check(DATABASE_URL, user_id, snapshot_time)
                                print(f"  [MorningCheck] {user_id} {snapshot_time}（{target_hhmm}）生成完了")
                            except Exception as e:
                                print(f"  [MorningCheck] {user_id} {snapshot_time} 生成失敗", e)
                # 日付が変わったら発火済みセットをクリアして無限に肥大化しないようにする
                if len(fired) > 200:
                    fired = {k for k in fired if k[0] == now_jst.date().isoformat()}
        except Exception as e:
            print("  [MorningCheck] スケジューラループで例外", e)
        time.sleep(60)


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


# 2026-09-08新規（ユーザーの後場レビューJSON、app_improvement_requests「SECTOR_REGIME」対応）：
# セクター代表ETF（現状は半導体＝200A.Tのみ）の当日値動きから、RISK_ON/MIXED/RISK_OFFの
# 簡易レジームを判定する。既存の_fetch_intraday()（5分足チャート等で既に使っている取得関数）を
# そのまま再利用し、新しい分足取得経路は作らない。VWAP（出来高加重平均価格）に対する現在値の
# 位置と、直近半分・前半分の高値/安値比較（切り上げ/切り下げ）だけで判定する単純なルールベース
# （AI不使用、他のAUTO系エンジンと同じ方針）。データ不足・取得失敗時はNoneを返す（推測値は
# 作らない）。
def _intraday_regime_uncached(symbol, interval="5m"):
    if yf is None:
        return None
    tk = yf.Ticker(symbol)
    bars = _fetch_intraday(tk, interval)
    if not bars or len(bars["closes"]) < 6:
        return None
    closes, highs, lows, volumes = bars["closes"], bars["highs"], bars["lows"], bars["volumes"]
    total_vol = sum(volumes)
    vwap = (sum(c * v for c, v in zip(closes, volumes)) / total_vol) if total_vol > 0 else (sum(closes) / len(closes))
    current = closes[-1]
    above_vwap = current >= vwap
    mid = len(highs) // 2
    first_half_high = max(highs[:mid]) if mid > 0 else highs[0]
    second_half_high = max(highs[mid:])
    first_half_low = min(lows[:mid]) if mid > 0 else lows[0]
    second_half_low = min(lows[mid:])
    higher_highs = second_half_high > first_half_high
    lower_lows = second_half_low < first_half_low
    if above_vwap and higher_highs and not lower_lows:
        regime, pattern = "RISK_ON", "higher_highs"
    elif (not above_vwap) and lower_lows:
        regime, pattern = "RISK_OFF", "lower_lows"
    else:
        regime, pattern = "MIXED", "mixed"
    return {"current": round(current, 2), "vwap": round(vwap, 2), "aboveVwap": above_vwap,
            "regime": regime, "pattern": pattern}


def _intraday_regime_cached(symbol, interval, ttl):
    """_intraday_regime_uncached()をキャッシュ+リトライ+stale fallbackでラップする
    （Market Intelligence Timelineの個別銘柄5分足判定専用、指示書1・2・3・4番）。
    戻り値：(value_or_None, cache_status)。cache_status："ok"|"stale_cache"|
    "rate_limited"|"failed"。"""
    cache_key = f"intraday5m:{symbol}:{interval}"
    entry = _cache_get(cache_key)
    if _cache_fresh(entry, ttl):
        return entry["value"], "ok"
    value, exc = _retry_on_rate_limit(lambda: _intraday_regime_uncached(symbol, interval))
    if value is not None:
        _cache_set(cache_key, value)
        return value, "ok"
    if exc is None:
        # 取得自体は成功したが分足データが無い（データ不足）＝レート制限とは無関係
        return None, "ok"
    if entry is not None:
        print(f"  [RateLimitGuard] {cache_key}：取得失敗のため期限切れキャッシュ（stale）を使用")
        return entry["value"], "stale_cache"
    if exc is not None:
        print("  5分足レジーム判定失敗（キャッシュ経路）", symbol, exc)
        if _is_rate_limit_error(exc):
            return None, "rate_limited"
    return None, "failed"


def _intraday_regime(symbol, interval="5m", cache_ttl=0):
    """2026-09-10更新（レート制限耐性）：cache_ttl>0を指定した呼び出し元（Market Intelligence
    Timelineの個別銘柄5分足判定）だけキャッシュ+リトライ+stale fallbackが有効になる。
    既存呼び出し元（/api/sector-regime等）はcache_ttl省略＝0のままなので、常に無条件で
    最新値を取りに行く既存動作を完全維持する。"""
    if cache_ttl <= 0:
        try:
            return _intraday_regime_uncached(symbol, interval)
        except Exception as e:
            print("  セクターレジーム判定失敗", symbol, e)
            return None
    value, _status = _intraday_regime_cached(symbol, interval, cache_ttl)
    return value


# セクター代表ETF・関連するAUTO_RS拡張（テーマ相対強弱）で使う一覧。半導体のみ先行実装
# （ユーザーの後場レビューJSONで実際に検証された組み合わせ）。今後テーマが増えたらここに
# 追加するだけで、GET /api/sector-regime・フロント側THEME_PROXY_METRICの両方に反映される。
SECTOR_PROXY_METRICS = {"nikkei_semi": "200A.T"}


# 2026-09-07新規（ポジション→リアルタイム売却判断画面 Phase2）：5分足チャート専用の取得関数。
# 既存の_fetch_intraday()はanalyze_stock()のエントリー判定ロジックが「closes/highs/lows/volumes」
# の配列だけを前提に使っており、そちらを変更するとentry_pattern等の既存判断が壊れるリスクが
# あるため、Open値・時刻（LightweightChartsが必要とするUNIXタイムスタンプ秒）が必要な
# チャート用途向けに別関数として新設した（既存呼び出し元・既存ロジックへの影響ゼロ）。
# 2026-09-09更新（監視銘柄/市場チャートの時間足切替）：periodを省略時"1d"固定だったのを、
# 呼び出し側が明示的に指定できるよう拡張した（省略時は従来通り"1d"＝ポジション画面の
# 既存「当日の5分足」動作を完全に維持）。共通チャートモーダル側は、時間足ごとに
# 「初期表示範囲＋スクロール用の余裕」を確保できるperiodを明示的に渡す
# （例：5分足→"5d"で直近数営業日分を取得しておき、表示は当日〜直近2営業日にズームする）。
def _fetch_intraday_bars(symbol, interval="5m", period=None):
    """指定期間・時間足のOHLCVをローソク足チャート用の形式（古い順のリスト、要素は
    {time, open, high, low, close, volume}、timeはUNIX秒）で返す。市場時間外・取得失敗時は
    空リストを返す（推測値・補完値は作らない）。periodを省略すると"1d"（当日のみ、既存動作）。"""
    try:
        tk = yf.Ticker(symbol)
        h = tk.history(period=period or "1d", interval=interval)
        if h is None or h.empty:
            return []
        h = h.dropna(subset=["Open", "High", "Low", "Close"])
        bars = []
        for idx, row in h.iterrows():
            bars.append({
                "time": int(idx.timestamp()),
                "open": round(float(row["Open"]), 2),
                "high": round(float(row["High"]), 2),
                "low": round(float(row["Low"]), 2),
                "close": round(float(row["Close"]), 2),
                "volume": float(row["Volume"]) if row["Volume"] == row["Volume"] else 0,  # NaN!=NaNを利用（pandas未importのため）
            })
        return bars
    except Exception as e:
        print("  短期足チャート取得失敗", symbol, interval, period, e)
        return []


def _vwap(closes, volumes):
    """出来高加重平均価格（当日の分足から算出する、ザラ場でよく見る節目の一つ）。"""
    total_vol = sum(volumes)
    if total_vol <= 0:
        return None
    return sum(c * v for c, v in zip(closes, volumes)) / total_vol


# ============================================================
# Market Intelligence Timeline（場中定時レポート）。2026-09-10新規、Phase2-A→Phase2-Bで拡張。
# 08:50 MorningMarketCheckを起点に、09:30寄り30分/11:30前場終了/13:00後場30分/15:30大引けを
# 「1本のタイムライン」として積み上げる。Phase2-Aで作った共通レポートエンジン
# （generate_opening_30m_report）をPhase2-Bでgenerate_intraday_report()へ一般化し、
# report_typeによる分岐だけで4つの時間帯すべてに対応する（09:30専用ロジックを他の時間帯へ
# コピーしない、指示書「実装方針」）。見逃し銘柄本格抽出・momentum_score・セクターローテー
# ション詳細統計・予想成績DBはPhase2-C/D/Eへ引き続き先送り。既存のmorning_analysis_engine・
# position_risk_rules・投資判断エンジン（catalysts/events）を最大限再利用し、判定基準を
# 重複実装しない（指示書30番）。
# ============================================================

INTRADAY_REPORT_SNAPSHOT_TIMES = {  # 指示書6番：JST、24h表記
    "OPENING_30M": "09:30", "MORNING_CLOSE": "11:30", "AFTERNOON_30M": "13:00", "MARKET_CLOSE": "15:30",
}
INTRADAY_REPORT_ORDER = ["OPENING_30M", "MORNING_CLOSE", "AFTERNOON_30M", "MARKET_CLOSE"]

# 朝TOP5の答え合わせ結果の強さ順（指示書1番：STRENGTHENED/MAINTAINED/WEAKENED/FAILEDの判定に使う）
_THESIS_RESULT_RANK = {"INVALIDATED": 0, "NOT_TRIGGERED": 1, "PARTIAL": 2, "CONFIRMED": 3}


def _thesis_transition_status(prev_result, new_result):
    """前回レポート時点のthesis_resultから今回への変化を判定する（指示書1番）。データ不足時は
    無理に強弱を判定せずDATA_INSUFFICIENTとする（指示書29番）。INVALIDATEDへ転落した場合は、
    強弱の方向に関わらず明確な失敗として一律FAILEDにする（「何が外れたか」を明確にするため）。"""
    if not prev_result or prev_result == "DATA_INSUFFICIENT" or new_result == "DATA_INSUFFICIENT":
        return "DATA_INSUFFICIENT"
    if new_result == "INVALIDATED":
        return "FAILED"
    prev_rank, new_rank = _THESIS_RESULT_RANK.get(prev_result), _THESIS_RESULT_RANK.get(new_result)
    if prev_rank is None or new_rank is None:
        return "DATA_INSUFFICIENT"
    if new_rank > prev_rank:
        return "STRENGTHENED"
    if new_rank < prev_rank:
        return "WEAKENED"
    return "MAINTAINED"


def _final_top5_result(thesis_history):
    """1銘柄分の当日全時間帯のthesis_result履歴（古い→新しい、現在の評価を含む）から、
    大引け時点の最終結果を判定する（指示書3番）。重要：triggerが一度も発動していない
    （常にNOT_TRIGGERED/DATA_INSUFFICIENTのみ）場合は、株価が下落していてもFAILにせず
    NO_ENTRYとする——朝の仮説と実際のエントリー機会を分けて評価する（ユーザー指示）。"""
    triggered = any(r in ("CONFIRMED", "PARTIAL", "INVALIDATED") for r in thesis_history)
    if not triggered:
        return "NO_ENTRY" if any(r == "NOT_TRIGGERED" for r in thesis_history) else "DATA_INSUFFICIENT"
    final = thesis_history[-1]
    if final == "CONFIRMED":
        return "SUCCESS"
    if final == "PARTIAL":
        return "PARTIAL_SUCCESS"
    if final == "INVALIDATED":
        return "FAIL"
    # 一度は発動したのに最後がNOT_TRIGGERED/DATA_INSUFFICIENTに戻る想定外パターン→安全側でPARTIAL_SUCCESS扱い
    return "PARTIAL_SUCCESS"


def _previous_intraday_reference(database_url, user_id, trade_date, report_type, morning_check):
    """指示書4番「全レポートでprevious_reportを参照する」。直前の時間帯のmarket_intelligence_
    reportsがあればそれを、無ければMorningMarketCheckまで遡る（順序：MorningCheck→09:30→
    11:30→13:00→15:30）。戻り値：(前回レポートdict_or_None, "morning_check"|report_type|None)。"""
    idx = INTRADAY_REPORT_ORDER.index(report_type)
    if investment_db is not None and database_url:
        for prior_type in reversed(INTRADAY_REPORT_ORDER[:idx]):
            rep = investment_db.get_market_intelligence_report(database_url, user_id, trade_date, prior_type)
            if rep:
                return rep, prior_type
    return (morning_check, "morning_check") if morning_check else (None, None)


def _intraday_stock_snapshot(watchlist_item):
    """TOP5銘柄1件分の「今」の状態（現在値・当日騰落率・VWAP位置・5分足構造）を取得する。
    既存の_intraday_regime()（セクターETFの当日レジーム判定で既に使っている5分足取得＋VWAP計算）
    をそのまま個別銘柄に転用するだけで、新しい分足取得経路は作らない（指示書30番）。
    2026-09-10更新（レート制限耐性）：現在値はget_stock_quotes(cache_ttl指定)、VWAP/5分足構造は
    _intraday_regime_cached()経由でキャッシュ+リトライ+stale fallbackを適用する（指示書1・2・
    3・4番）。cacheStatusも返す（呼び出し側のdata_quality集計用）。"""
    sym = _yf_symbol(watchlist_item)
    quote_status = {}
    quote = get_stock_quotes([watchlist_item], cache_ttl=CACHE_TTL["stock_quote"], status_out=quote_status).get(
        watchlist_item.get("code"))
    regime, regime_status = _intraday_regime_cached(sym, "5m", CACHE_TTL["stock5m"])
    current_change_pct = None
    if quote and quote.get("t") is not None and quote.get("p"):
        current_change_pct = round((quote["t"] - quote["p"]) / quote["p"] * 100, 2)
    cache_status = quote_status.get(watchlist_item.get("code"), "failed") if quote else regime_status
    # 両方stale/rate_limitedならその中でより深刻な方（missing相当）を優先して報告する
    if regime_status in ("rate_limited", "failed") and cache_status == "ok":
        cache_status = regime_status
    elif regime_status == "stale_cache" and cache_status == "ok":
        cache_status = "stale_cache"
    return {
        "current": quote.get("t") if quote else None,
        "currentChangePct": current_change_pct,
        "aboveVwap": regime["aboveVwap"] if regime else None,
        "fiveMinStructure": regime["pattern"] if regime else None,
        "dataStatus": "ok" if (quote and regime) else ("partial" if (quote or regime) else "failed"),
        "cacheStatus": cache_status,
    }


def evaluate_morning_thesis(morning_top5_item, snapshot, nikkei_chg):
    """朝の仮説（Morning CheckのTOP5候補1件）が実市場で成立したかを判定する（指示書5・6番）。
    「株価が上がった＝当たり」にはせず、VWAP位置・5分足構造・対市場相対強度（地合いに対する
    相対強度）を合わせて見る。データ不足時はDATA_INSUFFICIENTとし、無理に当たり外れを判定しない
    （指示書29番）。"""
    if snapshot["dataStatus"] == "failed" or snapshot["currentChangePct"] is None:
        return "DATA_INSUFFICIENT"
    chg = snapshot["currentChangePct"]
    above_vwap = snapshot["aboveVwap"]
    structure = snapshot["fiveMinStructure"]
    market_rs = (chg - nikkei_chg) if nikkei_chg is not None else None
    relatively_strong = market_rs is not None and market_rs >= 1.0
    if chg <= 0 and not relatively_strong:
        # トリガー（VWAP上・出来高増）未発動のまま下落＝まだ検証材料が無いのか、外れたのかを分ける
        if above_vwap is False and structure == "lower_lows":
            return "INVALIDATED"
        return "NOT_TRIGGERED"
    if above_vwap and structure == "higher_highs" and (chg > 0 or relatively_strong):
        return "CONFIRMED"
    if above_vwap is None:
        return "DATA_INSUFFICIENT"
    if above_vwap or relatively_strong:
        return "PARTIAL"
    return "INVALIDATED"


# ============================================================
# Market Intelligence Timeline Phase2-C（今買い時TOP5＋Thesis永続化）。2026-09-10新規。
# 「今日の注目TOP5」（trade-cockpit.htmlのenrichWatchRow()だけを使うフロント側の既存軽量
# ロジック、v3-4）とは別物。こちらは「現在最も条件が整っているENTRY候補」（entry_ready_top5）を
# ENTRY SCORE（0-100、重み付け）でサーバー側から算出し、Watch候補とは明確に分離する。既存の
# 5エンジン（run_momentum_stage1・AUTO_RS・AUTO_SECTOR_LEADER・AUTO_VOLUME）・Market
# Intelligence Timelineの個別銘柄5分足判定（_intraday_stock_snapshot・evaluate_morning_thesis・
# _thesis_transition_status・_final_top5_result）をそのまま再利用し、新しい全市場スキャン・
# 新しい判定ロジックの重複実装はしない。missed_opportunities本格分析・sector_rotation統計・
# 5/20/60営業日統計・自動ルール生成・自動売買はPhase2-Cの範囲外（ユーザー指示）。
# ============================================================

ENTRY_STATE_META = {
    "NOW_BUYABLE":   {"label": "今すぐ買える", "tier": 5},
    "ENTRY_READY":   {"label": "エントリー準備完了", "tier": 4},
    "WAIT_PULLBACK": {"label": "押し目待ち", "tier": 3},
    "WAIT_BREAKOUT": {"label": "ブレイク待ち", "tier": 3},
    "WATCH":         {"label": "監視継続", "tier": 2},
    "CHASE_RISK":    {"label": "高値掴みリスク", "tier": 1},
    "WEAK":          {"label": "弱い", "tier": 0},
    "INVALID":       {"label": "根拠崩れ", "tier": 0},
    "PROVISIONAL":   {"label": "データ不足（暫定）", "tier": 0},
}


def _entry_score_components(row, stage2, snapshot, auto_rs_current, auto_sector_current, catalysts, event_signals):
    """entry_score（0-100、内訳の合計をclampしたもの）を配点ごとに算出する。既存の各エンジンが
    既に計算済みの値だけを使い、新しい取得経路は増やさない。データが無い項目は0点（無理に
    加点も減点もしない、「取得できない値は推測しない」の踏襲）。
    配点：Momentum20/VWAP15/5分足構造15/MarketRelative15/Volume10/AUTO_RS10/AUTO_SECTOR5/
    Catalyst5/RiskEvent-5〜0/Overheat-10〜0。"""
    day_change = row.get("changePct")
    market_rs = row.get("marketRS")
    momentum = _scale_score(day_change, 0, 5, 20) if day_change is not None else 0.0

    above_vwap = snapshot.get("aboveVwap") if snapshot else None
    vwap_score = 15.0 if above_vwap else 0.0

    structure = snapshot.get("fiveMinStructure") if snapshot else None
    structure_score = 15.0 if structure == "higher_highs" else (7.0 if structure == "mixed" else 0.0)

    market_rel_score = _scale_score(market_rs, 0, 5, 15) if market_rs is not None else 0.0

    volume_type = _volume_type(stage2, row) if stage2 else "NEUTRAL_VOLUME"
    tavr = stage2.get("timeAdjustedVolumeRatio") if stage2 else None
    volume_score = 0.0 if volume_type in ("NEGATIVE_VOLUME", "CLIMAX_DOWN") else _scale_score(tavr, 1.0, 3.0, 10)

    auto_rs_score = 10.0 if row.get("code") in auto_rs_current else 0.0
    auto_sector_score = 5.0 if row.get("code") in auto_sector_current else 0.0

    pos_cat = [c for c in (catalysts or []) if c.get("sentiment") == "positive" and c.get("freshness") in ("LIVE", "CURRENT")]
    neg_cat = [c for c in (catalysts or []) if c.get("sentiment") == "negative" and c.get("freshness") in ("LIVE", "CURRENT")]
    catalyst_score = 5.0 if pos_cat else 0.0

    risk_event_penalty = 0.0
    if "EVENT_RISK_HIGH" in (event_signals or []):
        risk_event_penalty = -5.0
    if neg_cat:
        risk_event_penalty = -5.0

    overheat_penalty = 0.0
    if volume_type == "CLIMAX_UP":
        overheat_penalty = -10.0
    elif stage2 and stage2.get("aboveRecentHigh") and stage2.get("distanceFromHighPct") is not None:
        d = stage2["distanceFromHighPct"]
        if d >= 5:
            overheat_penalty = -10.0
        elif d >= 2:
            overheat_penalty = -5.0

    total = (momentum + vwap_score + structure_score + market_rel_score + volume_score
             + auto_rs_score + auto_sector_score + catalyst_score + risk_event_penalty + overheat_penalty)
    total = max(0.0, min(100.0, total))
    return {
        "total": round(total, 1),
        "momentum": round(momentum, 1), "vwap": round(vwap_score, 1), "fiveMinStructure": round(structure_score, 1),
        "marketRelative": round(market_rel_score, 1), "volume": round(volume_score, 1),
        "autoRs": round(auto_rs_score, 1), "autoSector": round(auto_sector_score, 1),
        "catalyst": round(catalyst_score, 1), "riskEvent": round(risk_event_penalty, 1),
        "overheat": round(overheat_penalty, 1),
        "volumeType": volume_type, "positiveCatalysts": pos_cat[:1], "negativeCatalysts": neg_cat[:1],
    }


def _classify_entry_state(entry_score, row, stage2, snapshot, data_quality, event_signals, neg_cat_present, nikkei_chg=None):
    """ENTRY_STATE（8種＋PROVISIONAL）をルールベースで決定する（AI不使用、既存AUTO系エンジンと
    同じ方針）。2026-09-10更新（Phase2-C「TOP5選考基準の全面見直し」指示書2・3・24番）：
    「原則プラス銘柄からしか選ばない」ゲートを追加した。当日騰落率がマイナスの銘柄は、以下を
    "すべて"満たす例外（逆行耐性例外）でない限りNOW_BUYABLE/ENTRY_READYにはならない
    （WATCH/WEAKへ回り、Watchlist側で「反転待ち」等として扱われる想定）：
      ①市場全体が大幅安（nikkei_chg<=-1.0%）②銘柄は小幅マイナス（changePct>=-1.0%）
      ③対市場RSが極めて強い（marketRS>=+2.0pt）④VWAP回復済み（aboveVwap）
      ⑤5分足で明確な反転（fiveMinStructure=="higher_highs"）⑥出来高増加
      （POSITIVE_VOLUME、または時間帯補正済み出来高倍率>=1.3）
    戻り値：(entry_state, exception_applied)。exception_applied=Trueの時、呼び出し側は
    reasonsに「逆行耐性例外」を明示する（指示書3番「その場合UIで理由を明示」）。
    data_quality=DEGRADED（Stage2・5分足スナップショットの両方が欠落）の場合はPROVISIONAL固定
    ＝confidence=LOWの銘柄をNOW_BUYABLEにしない（指示書26番）。"""
    if data_quality == "DEGRADED":
        return "PROVISIONAL", False
    above_vwap = snapshot.get("aboveVwap") if snapshot else None
    structure = snapshot.get("fiveMinStructure") if snapshot else None
    market_rs = row.get("marketRS")
    day_change = row.get("changePct")
    volume_type = _volume_type(stage2, row) if stage2 else "NEUTRAL_VOLUME"
    tavr = stage2.get("timeAdjustedVolumeRatio") if stage2 else None
    making_new_low = bool(stage2 and stage2.get("makingNewLowToday"))
    overheated = volume_type == "CLIMAX_UP" or bool(
        stage2 and stage2.get("aboveRecentHigh") and (stage2.get("distanceFromHighPct") or 0) >= 5)

    exception_applied = False
    if day_change is not None and day_change <= 0:
        market_selloff = nikkei_chg is not None and nikkei_chg <= -1.0
        small_decline = day_change >= -1.0
        strong_market_rs = market_rs is not None and market_rs >= 2.0
        vwap_recovered = above_vwap is True
        reversal_structure = structure == "higher_highs"
        volume_up = volume_type == "POSITIVE_VOLUME" or (tavr is not None and tavr >= 1.3)
        exception_applied = all([market_selloff, small_decline, strong_market_rs,
                                  vwap_recovered, reversal_structure, volume_up])
        if not exception_applied:
            # プラス転換の根拠が無いマイナス銘柄はNOW_BUYABLE/ENTRY_READYの対象から除外
            # （後段のCHASE_RISK/INVALID/WEAKの判定はこの後も通常どおり行う）。
            if neg_cat_present and (making_new_low or (market_rs is not None and market_rs < 0)):
                return "INVALID", False
            return ("WATCH" if entry_score >= 30 else "WEAK"), False

    if neg_cat_present and (making_new_low or (market_rs is not None and market_rs < 0)):
        return "INVALID", False
    if overheated:
        return "CHASE_RISK", exception_applied
    if entry_score < 30:
        return "WEAK", exception_applied
    if "EVENT_RISK_HIGH" in (event_signals or []) and entry_score < 60:
        return "WATCH", exception_applied
    if above_vwap and structure == "higher_highs" and (market_rs is not None and market_rs > 0) and entry_score >= 70:
        return "NOW_BUYABLE", exception_applied
    if above_vwap and structure in ("higher_highs", "mixed") and entry_score >= 55:
        return "ENTRY_READY", exception_applied
    if entry_score >= 40 and above_vwap is False:
        return "WAIT_BREAKOUT", exception_applied
    if entry_score >= 40 and above_vwap:
        return "WAIT_PULLBACK", exception_applied
    if entry_score >= 30:
        return "WATCH", exception_applied
    return "WEAK", exception_applied


def _score_entry_candidates(database_url, user_id):
    """今買い時TOP5（entry_ready_top5）とWatch候補を算出する純粋関数（DB書き込みなし）。
    既存の共有Stage1（run_momentum_stage1、複数AUTOエンジンとキャッシュ共有）・
    AUTO_RS/AUTO_SECTOR_LEADERのCURRENT登録・AUTO_VOLUMEのStage2（_volume_stage2_detail）・
    Market Intelligence Timelineの個別銘柄5分足判定（_intraday_stock_snapshot）をそのまま
    再利用する（新規の全市場スキャン・新規の分足取得経路は追加しない）。対象は監視銘柄
    （watchlist）のみ（全市場4000銘柄には広げない。「Watch候補との分離」は監視銘柄内での話）。
    2026-09-10更新（Phase2-C）：「Current TOP5」（/api/entry-candidates、いつでも再計算）と
    「朝TOP5」（MorningMarketCheck、08:50固定・成績評価用）の両方がこの同じ関数を使う——
    永続化（stock_thesesへの書き込み）はしない副作用フリーな関数にし、呼び出し側
    （generate_morning_market_check）だけが朝TOP5として結果を保存する設計にした（指示書
    22・23番「Current TOP5とMorning TOP5は別物、Morning TOP5は後から書き換えない」）。"""
    empty = {"entryReadyTop5": [], "watchCandidates": [], "dataQuality": "DEGRADED", "generatedAt": None}
    if investment_db is None or not database_url:
        return empty
    watchlist = investment_db.list_watchlist(database_url, user_id, market="JP")
    if not watchlist:
        return empty
    stage1 = run_momentum_stage1()
    stage1_rows = stage1.get("rows", {})
    nikkei_chg = stage1.get("nikkeiChangePct")
    auto_rs_current = investment_db.get_codes_with_auto_tag(database_url, user_id, "AUTO_RS_CURRENT", market="JP")
    auto_sector_current = investment_db.get_codes_with_auto_tag(database_url, user_id, "AUTO_SECTOR_LEADER_CURRENT", market="JP")

    candidates = []
    quality_counts = {"FULL": 0, "PARTIAL": 0, "DEGRADED": 0}
    for w in watchlist:
        code = w.get("code")
        row = stage1_rows.get(code)
        if not row or row.get("current") is None:
            continue
        stage2 = None
        try:
            stage2 = _volume_stage2_detail(code, row)
        except Exception as e:
            print("  entry-candidates: Stage2取得失敗", code, e)
        snapshot = None
        try:
            snap = _intraday_stock_snapshot(w)
            if snap.get("dataStatus") != "failed":
                snapshot = snap
        except Exception as e:
            print("  entry-candidates: 5分足スナップショット失敗", code, e)

        if stage2 is not None and snapshot is not None:
            data_quality = "FULL"
        elif stage2 is not None or snapshot is not None:
            data_quality = "PARTIAL"
        else:
            data_quality = "DEGRADED"
        quality_counts[data_quality] += 1
        analysis_confidence = {"FULL": "HIGH", "PARTIAL": "MEDIUM", "DEGRADED": "LOW"}[data_quality]

        catalysts = investment_db.relevant_catalysts_for(database_url, user_id, code=code, sector=row.get("sector"), limit=3)
        events = investment_db.upcoming_event_signals(database_url, user_id, code=code, sector=row.get("sector"))
        event_signals = events["signals"]

        comp = _entry_score_components(row, stage2, snapshot or {}, auto_rs_current, auto_sector_current, catalysts, event_signals)
        neg_cat_present = bool(comp["negativeCatalysts"])
        entry_score = comp["total"]
        entry_state, exception_applied = _classify_entry_state(
            entry_score, row, stage2, snapshot, data_quality, event_signals, neg_cat_present, nikkei_chg)
        resilience = _rs_resilience_tier({"changePct": row.get("changePct"), "marketRS": row.get("marketRS")}, nikkei_chg)

        reasons = []
        if comp["momentum"] > 0:
            reasons.append(f"当日+{row.get('changePct'):.1f}%の勢い")
        if comp["vwap"] > 0:
            reasons.append("VWAP上を維持")
        if comp["fiveMinStructure"] >= 15:
            reasons.append("5分足で高値切り上げ")
        if comp["marketRelative"] > 0:
            reasons.append(f"対市場+{row.get('marketRS'):.1f}pt")
        if comp["autoRs"] > 0:
            reasons.append("AUTO_RS選出中")
        if comp["autoSector"] > 0:
            reasons.append("セクター内優位（AUTO_SECTOR_LEADER）")
        if comp["positiveCatalysts"]:
            reasons.append(f"好材料：{comp['positiveCatalysts'][0].get('title','')[:20]}")
        if exception_applied:
            reasons.append("⚠逆行耐性例外（地合い逆風下でも対市場優位・VWAP回復・反転構造・出来高増を確認）")
        risks = []
        if comp["negativeCatalysts"]:
            risks.append(f"悪材料：{comp['negativeCatalysts'][0].get('title','')[:20]}")
        if comp["riskEvent"] < 0 and not comp["negativeCatalysts"]:
            risks.append("重要イベント接近")
        if comp["overheat"] < 0:
            risks.append("直近高値からの乖離が大きい（高値掴み注意）")

        candidates.append({
            "code": code, "name": w.get("name"), "sector": w.get("sector"),
            "current": row.get("current"),
            "changePct": round(row.get("changePct"), 2) if row.get("changePct") is not None else None,
            "marketRS": round(row.get("marketRS"), 2) if row.get("marketRS") is not None else None,
            "entryScore": round(entry_score), "entryState": entry_state, "resilience": resilience,
            "analysisConfidence": analysis_confidence, "dataQuality": data_quality,
            "scoreBreakdown": comp, "reasons": reasons or ["総合スコア上位"], "risks": risks,
        })

    # 指示書6番「値上がり率だけでは選ばない」：ソート基準はentry_score（既に過熱ペナルティ・
    # VWAP/構造/RS等を織り込み済み）であり、changePct単純降順ではない。
    candidates.sort(key=lambda c: -c["entryScore"])
    entry_ready_top5 = [c for c in candidates if c["entryState"] in ("NOW_BUYABLE", "ENTRY_READY")][:5]
    top5_codes = {c["code"] for c in entry_ready_top5}
    watch_candidates = [c for c in candidates
                         if c["entryState"] in ("WAIT_PULLBACK", "WAIT_BREAKOUT", "WATCH")
                         and c["code"] not in top5_codes][:15]
    overall_quality = "FULL" if quality_counts["DEGRADED"] == 0 and quality_counts["PARTIAL"] == 0 else (
        "DEGRADED" if quality_counts["FULL"] == 0 else "PARTIAL")

    return {
        "entryReadyTop5": [{**c, "rank": i + 1} for i, c in enumerate(entry_ready_top5)],
        "watchCandidates": watch_candidates,
        "dataQuality": overall_quality,
        "generatedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }


def compute_entry_ready_candidates(database_url, user_id):
    """Current TOP5（/api/entry-candidates、いつでも再計算できる表示用）。_score_entry_
    candidates()をそのまま返すだけで、stock_thesesへの永続化は行わない（永続化は朝TOP5＝
    generate_morning_entry_top5+persist_morning_thesesの専任、指示書22・23番）。"""
    return _score_entry_candidates(database_url, user_id)


def generate_morning_entry_top5(database_url, user_id):
    """朝TOP5（entry_ready_top5、08:50MorningMarketCheck生成時点のスナップショット）を算出
    する。Current TOP5と全く同じ_score_entry_candidates()を使う（指示書「同じ選考基準」）。
    永続化はしない（呼び出し側でsave_morning_check後にpersist_morning_thesesを呼ぶ）。"""
    return _score_entry_candidates(database_url, user_id)


def persist_morning_theses(database_url, user_id, trade_date, morning_check_id, entry_ready_top5):
    """朝TOP5（generate_morning_entry_top5の結果のentryReadyTop5）をstock_thesesへ永続化する
    （source='MORNING'固定、指示書11・12番）。同じ(user_id, code, market, entry_date)が既に
    あれば何もしない＝1日1仮説・後から書き換えない。"""
    if investment_db is None or not database_url or not entry_ready_top5:
        return
    for c in entry_ready_top5:
        bd = c.get("scoreBreakdown", {})
        try:
            investment_db.ensure_stock_thesis(
                database_url, user_id, c["code"], "JP", trade_date, c["name"],
                c["entryScore"], c["entryState"], c["reasons"], c["analysisConfidence"],
                source="MORNING", morning_check_id=morning_check_id, morning_rank=c.get("rank"),
                vwap_state=("ABOVE" if bd.get("vwap", 0) > 0 else "BELOW_OR_UNKNOWN"),
                auto_rs=bd.get("autoRs", 0) > 0, auto_sector=bd.get("autoSector", 0) > 0,
                resilience=c.get("resilience"),
                trigger_text="寄り後VWAP維持＋5分足安値切り上げを確認してからのエントリーを推奨",
                avoid_condition="寄り天・出来高を伴わない上昇・悪材料の追加",
                morning_price=c.get("current"))
        except Exception as e:
            print("  朝TOP5 thesis永続化失敗", c.get("code"), e)


def _reevaluate_active_stock_theses(database_url, user_id, trade_date, report_type, nikkei_chg):
    """entry_ready_top5から作られたstock_thesesを答え合わせする。朝TOP5の答え合わせと全く
    同じevaluate_morning_thesis/_thesis_transition_status/_final_top5_resultを再利用する
    （別ロジックは作らない）。report_type=MARKET_CLOSEの回だけ、当日の履歴から最終結果
    （SUCCESS/PARTIAL_SUCCESS/FAIL/NO_ENTRY/DATA_INSUFFICIENT）を確定する。"""
    if investment_db is None or not database_url:
        return
    active = investment_db.list_active_stock_theses(database_url, user_id, trade_date)
    for thesis in active:
        code, market = thesis.get("code"), thesis.get("market", "JP")
        w = {"code": code, "market": market, "name": thesis.get("name")}
        try:
            snap = _intraday_stock_snapshot(w)
        except Exception as e:
            print("  Thesis答え合わせ: スナップショット失敗", code, e)
            continue
        result = evaluate_morning_thesis({"code": code}, snap, nikkei_chg)
        prev_result = thesis.get("latest_thesis_result")
        transition = _thesis_transition_status(prev_result, result) if prev_result else None
        try:
            investment_db.update_stock_thesis_evaluation(
                database_url, user_id, code, market, trade_date, result, transition,
                thesis.get("latest_entry_score"), report_type)
        except Exception as e:
            print("  Thesis答え合わせ: 更新失敗", code, e)

    if report_type == "MARKET_CLOSE":
        for thesis in investment_db.list_active_stock_theses(database_url, user_id, trade_date):
            history = [h.get("thesis_result") for h in (thesis.get("status_history_json") or []) if h.get("thesis_result")]
            if not history:
                continue
            final = _final_top5_result(history)
            try:
                investment_db.finalize_stock_thesis(
                    database_url, user_id, thesis["code"], thesis.get("market", "JP"), trade_date, final)
            except Exception as e:
                print("  Thesis最終確定失敗", thesis.get("code"), e)


# ============================================================
# にこそく（@nicosokufx）X投稿 自動取得・市場分析連携。2026-09-10新規。
# 第一選択かつ唯一の取得方式はX公式API v2（指示書1番「スクレイピング・ログイン回避・
# CAPTCHA回避は実装しない」）。X_API_BEARER_TOKEN未設定でもアプリ全体は正常動作し、この
# 機能だけがX_SOURCE_STATUS=DEGRADEDになる（指示書19番）。投稿は全ユーザー共通の公開市場
# 情報として扱い、他の大半のテーブルと異なりuser_id列を持たない（social_market_posts）
# ——ただし関連銘柄（direct_mentions/theme_related）の判定は監視銘柄/ポジションに依存する
# ため、ユーザー単位で計算する関数も用意する。画像そのものの構造化解析（ヒートマップの
# 読み取り等）はOCR/画像認識APIを新規導入せず（CLAUDE.md「有料AI APIは使わない」方針を
# 踏襲）、image_analysis_jsonへユーザーがChatGPT等で解析した結果をJSON貼り付けで保存する
# 設計とした（既存のSmart Import・ChatGPT連携と同じ「解析はユーザー側、アプリは保存/表示に
# 徹する」パターン）。既存の朝一チェック・INTRADAY_REPORT・イベント・カタリスト・AUTO_RS等の
# 判定ロジックは一切変更しない（指示書12・21番）——追加専用フィールドとしてのみ統合する。
# ============================================================

X_API_BASE = "https://api.twitter.com/2"
X_SOCIAL_SOURCE_PLATFORM = "X"

# 指示書5番：投稿の自動分類（複数カテゴリ付与可）。AI不使用のキーワードベース分類
# （他のSmart Import/AUTO系エンジンと同じ方針）。
X_POST_CATEGORY_KEYWORDS = {
    "MARKET_HEATMAP":    ["ヒートマップ"],
    "INDEX_TECHNICAL":   ["日経平均", "日経先物", "TOPIX", "グロース250", "225先物"],
    "STOCK_TECHNICAL":   ["日足", "移動平均", "出来高", "ローソク足", "チャート"],
    "ECONOMIC_EVENT":    ["CPI", "PPI", "雇用統計", "小売売上高", "経済指標", "GDP"],
    "CENTRAL_BANK":      ["FOMC", "FRB", "日銀", "ECB", "利上げ", "利下げ", "金融政策"],
    "FX":                ["ドル円", "為替", "円安", "円高", "ユーロ円"],
    "US_MARKET":         ["NYダウ", "ダウ平均", "ナスダック", "S&P", "米株", "米国株"],
    "INTEREST_RATE":     ["金利", "国債利回り", "10年債"],
    "SEMICONDUCTOR":     ["半導体", "SOX", "NVIDIA", "エヌビディア"],
    "COMMODITY":         ["原油", "金相場", "商品市況", "WTI"],
    "SHIPPING":          ["海運", "バルチック", "運賃指数"],
    "SECTOR_ROTATION":   ["セクターローテーション", "資金循環", "物色"],
    "MARKET_SENTIMENT":  ["センチメント", "投資家心理", "リスクオン", "リスクオフ"],
    "MARKET_OVERVIEW":   ["相場全体", "本日の相場", "地合い", "全体観"],
}
X_POST_SQ_KEYWORDS = ["SQ", "メジャーSQ"]
X_POST_HIGH_KEYWORDS = (["CPI", "PPI", "雇用統計", "FOMC", "FRB", "日銀", "ECB", "為替急変",
                          "米金利急変", "SOX", "NVIDIA", "エヌビディア", "セクターローテーション"]
                         + X_POST_SQ_KEYWORDS)
X_POST_CRITICAL_KEYWORDS = ["急落", "急騰", "暴落", "暴騰", "サーキットブレーカー", "ストップ安", "ストップ高"]
# 見解（author_opinion）を示す表現。事実（facts）との混同を防ぐための最重要ロジック（指示書7番）。
X_POST_OPINION_MARKERS = ["だろう", "と思う", "と見ている", "べきではない", "べき", "山場",
                           "かもしれない", "警戒したい", "期待したい", "注意したい", "懸念",
                           "強気", "弱気", "個人的に", "想定", "様子見でいい", "お勧め", "推奨"]
X_POST_FACT_TIME_RE = re.compile(r"\d{1,2}[:：]\d{2}|\d{1,2}/\d{1,2}|\d{4}年\d{1,2}月\d{1,2}日")
# 指示書15番：直接銘柄名が無くてもテーマ経由で間接関連付けする対象キーワード。ここでは
# キーワード検出だけ行い、実際の銘柄マッピングは監視銘柄の既存theme欄（2026-09-08
# SECTOR_RELATIVE_STRENGTH機能で使われているもの）と突き合わせる——独自の銘柄マップを
# ハードコードで作らない（不正確な固定マップの方が実害が大きいため、既存の登録データだけを
# 根拠にする）。
X_POST_THEME_KEYWORDS = ["AI", "半導体", "GPU", "データセンター", "電線", "銅", "SaaS",
                          "海運", "銀行", "保険", "自動運転", "原油"]
# 指示書8番：投稿からの将来イベント自動検出。日付（M/D）＋イベント種別キーワードの組み合わせ
# だけを拾う軽量な正規表現ベース検出（既存Smart Import EVENTカテゴリの判定とは別の専用ロジック
# ——にこそく投稿はイベントカレンダー画像＋短い日付列挙が多く、Smart Importの自然文パターンと
# 形が異なるため）。
X_POST_EVENT_DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})\s*([^\d/\n、。]{0,12})")
X_POST_EVENT_TYPE_KEYWORDS = {
    "ECONOMIC": ["CPI", "PPI", "雇用統計", "小売売上高", "GDP", "経済指標"],
    "CENTRAL_BANK": ["FOMC", "日銀会合", "日銀", "ECB", "金融政策"],
    "INDEX_REBALANCE": ["メジャーSQ", "SQ"],
}


def _classify_social_post_categories(text):
    """指示書5番：キーワード一致で複数カテゴリを付与する（1つも一致しなければOTHER）。"""
    if not text:
        return ["OTHER"]
    cats = [cat for cat, kws in X_POST_CATEGORY_KEYWORDS.items() if any(kw in text for kw in kws)]
    return cats or ["OTHER"]


def _classify_social_post_importance(text, categories, direct_mentions=None, position_codes=None):
    """指示書6番：LOW/MEDIUM/HIGH/CRITICALを判定する。CRITICALは「市場を即時に動かしている・
    現在保有銘柄へ直接影響・当日のトレード判断を変更する可能性が高い」場合だけに限定
    （指示書の明示的な限定条件）——緊急性を示すキーワードがあり、かつ直接言及銘柄が実際の
    保有銘柄と重なる場合のみCRITICALとする（無関係な急騰急落報告を無条件にCRITICAL化しない）。"""
    if not text:
        return "LOW"
    direct_mentions = direct_mentions or []
    position_codes = position_codes or set()
    has_critical_kw = any(kw in text for kw in X_POST_CRITICAL_KEYWORDS)
    affects_position = bool(set(direct_mentions) & set(position_codes))
    if has_critical_kw and affects_position:
        return "CRITICAL"
    if any(kw in text for kw in X_POST_HIGH_KEYWORDS) or has_critical_kw:
        return "HIGH"
    if categories and categories != ["OTHER"]:
        return "MEDIUM"
    return "LOW"


def _split_facts_opinions(text):
    """指示書7番：本文を「事実」と「投稿者の見解」に分離する（system_inferenceはPhase1では
    生成しない——実際の売買判断ロジックと結び付けない段階のため、AIによる断定的な推論を
    捏造しない。指示書12番「投稿だけで売買判断しない」の精神に合わせた保守的な実装）。
    文分割は句点・改行の単純な区切り。見解マーカーが1つでも含まれる文はauthor_opinionへ、
    それ以外はfactsへ（事実の断定はしすぎず、単純な列挙文もfactsとして保持する）。"""
    if not text:
        return [], []
    sentences = re.split(r"[。\n]", text)
    facts, opinions = [], []
    for s in sentences:
        s = s.strip()
        if not s:
            continue
        if any(m in s for m in X_POST_OPINION_MARKERS):
            opinions.append(s)
        else:
            facts.append(s)
    return facts, opinions


def _detect_social_post_mentions(database_url, user_id, text):
    """指示書15番：登録銘柄の直接言及（銘柄名が本文に文字列として含まれるか）と、テーマ
    キーワード経由の間接関連（監視銘柄の既存theme欄に同じキーワードが含まれる銘柄）を分離
    して返す。独自の固定銘柄マップは使わない（既存の登録データのみを根拠にする）。
    戻り値：{"direct_mentions": [code,...], "theme_related": [code,...]}"""
    result = {"direct_mentions": [], "theme_related": []}
    if investment_db is None or not database_url or not text:
        return result
    try:
        watchlist = investment_db.list_watchlist(database_url, user_id, market="JP")
    except Exception:
        return result
    direct, theme = set(), set()
    matched_themes = [kw for kw in X_POST_THEME_KEYWORDS if kw in text]
    for w in watchlist:
        name = (w.get("name") or "").strip()
        if name and name in text:
            direct.add(w.get("code"))
        if matched_themes:
            item_theme = (w.get("theme") or "")
            if any(kw in item_theme for kw in matched_themes):
                theme.add(w.get("code"))
    theme -= direct  # 直接言及と間接関連は排他（指示書15番「直接言及と間接関連は区別する」）
    result["direct_mentions"] = sorted(direct)
    result["theme_related"] = sorted(theme)
    return result


def _detect_events_from_social_text(text, posted_at_date):
    """指示書8番：投稿本文から将来イベント候補を検出する（M/D＋イベント種別キーワード）。
    確定登録はせずdraft（source='nicosoku_x', verification_status='UNVERIFIED'）として返す。
    呼び出し側でinvestment_db.import_market_eventsへ渡す前に重複チェックを行う。"""
    if not text:
        return []
    drafts = []
    for m in X_POST_EVENT_DATE_RE.finditer(text):
        month, day, tail = int(m.group(1)), int(m.group(2)), m.group(3)
        event_type = next((et for et, kws in X_POST_EVENT_TYPE_KEYWORDS.items() if any(kw in tail for kw in kws)), None)
        title = tail.strip()
        if not event_type or not title:
            continue
        year = posted_at_date.year
        try:
            event_date = datetime.date(year, month, day)
        except ValueError:
            continue
        if event_date < posted_at_date - datetime.timedelta(days=3):
            event_date = datetime.date(year + 1, month, day)  # 年またぎ（12月の投稿で1月のイベント等）
        drafts.append({
            "event_date": event_date.isoformat(), "title": f"{title}（にこそく投稿より検出）",
            "event_type": event_type, "importance": "MEDIUM",
            "source": "nicosoku_x", "source_type": "X_POST", "verification_status": "UNVERIFIED",
            "raw_payload": {"confidence": "LOW", "detected_text": tail.strip()},
        })
    return drafts


# ============================================================
# にこそく画像解析待ちキュー。2026-09-10新規。
# 画像付き投稿はimage_analysis_status=PENDINGで保存され、ユーザーがChatGPT等で解析した
# 結果を既存の「ChatGPT連携」窓口（Smart Import）へ貼り付けるとSOCIAL_IMAGE_ANALYSISとして
# 自動分類され、POST /api/social-posts/image-analysisと同じ保存経路へ振り分けられる
# （smart_import_confirm()のSOCIAL_IMAGE_ANALYSIS分岐）。解析結果はrecent_social_market_
# signals・朝一チェック・INTRADAY_REPORT・イベント検出・関連銘柄付けに使うが、売買スコアへは
# 一切加点しない（既存方針の継続）。
# ============================================================

def normalize_social_image_analysis(draft, raw_text=None, import_source="unknown"):
    """{"type":"social_market_image_analysis","post_id":...,"analysis":{...}}形式のJSONを
    (post_id, analysis_dict)へ正規化する。post_idが無ければ保存できないためNoneを返す
    （呼び出し側でconfidence=LOWとして自動保存対象外になる想定と一致させる）。analysisの
    中身はユーザー/ChatGPT側の自由記述を許容し、厳密なスキーマ検証はしない（他のSmart
    Import正規化関数と同じ「形を強制しすぎない」方針）。"""
    if not isinstance(draft, dict):
        return None
    post_id = draft.get("post_id")
    if not post_id:
        return None
    analysis = draft.get("analysis")
    if not isinstance(analysis, dict):
        # analysisキーが無い場合、post_id・type以外の残りのキー全体をanalysisとして扱う
        # （ユーザーがrequestで例示した8項目をトップレベルに直接貼り付けるケースにも対応）。
        analysis = {k: v for k, v in draft.items() if k not in ("type", "post_id", "posted_at", "text", "image_urls", "request")}
    return post_id, analysis


def _extract_stock_mentions_from_analysis(analysis):
    """analysis中のstock_mentions（または旧仕様のstocks）から銘柄名/コード候補の文字列一覧を
    取り出す（{"name":...}/{"stock":...}/{"code":...}/文字列、いずれの形も許容）。"""
    raw = (analysis or {}).get("stock_mentions") or (analysis or {}).get("stocks") or []
    if not isinstance(raw, list):
        return []
    names = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            names.append(item.strip())
        elif isinstance(item, dict):
            n = item.get("name") or item.get("stock") or item.get("code")
            if n:
                names.append(str(n).strip())
    return names


def _resolve_stock_mentions_to_codes(database_url, user_id, mention_names):
    """画像解析が返した銘柄名候補を、既存監視銘柄の名前と突き合わせてコードへ解決する
    （指示書15番と同じ「既存の登録データのみを根拠にする」方針、独自の固定マップは使わない）。"""
    if investment_db is None or not database_url or not mention_names:
        return []
    try:
        watchlist = investment_db.list_watchlist(database_url, user_id, market="JP")
    except Exception:
        return []
    codes = set()
    for name in mention_names:
        for w in watchlist:
            wname = (w.get("name") or "").strip()
            if wname and (wname in name or name in wname):
                codes.add(w.get("code"))
    return sorted(codes)


def _normalize_image_economic_events(analysis, posted_date):
    """analysis中のeconomic_eventsを market_events importable な draft へ変換する
    （文字列「9/10 PPI」形式は既存の_detect_events_from_social_textを再利用、
    {"date":...,"title":...}形式は直接構築——別ロジックの重複実装を避ける）。"""
    raw = (analysis or {}).get("economic_events") or []
    if not isinstance(raw, list):
        return []
    drafts = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            drafts.extend(_detect_events_from_social_text(item, posted_date))
        elif isinstance(item, dict) and item.get("title"):
            event_date = posted_date
            date_str = item.get("date")
            if date_str:
                m = re.match(r"(\d{1,2})/(\d{1,2})", str(date_str))
                if m:
                    try:
                        event_date = datetime.date(posted_date.year, int(m.group(1)), int(m.group(2)))
                    except ValueError:
                        event_date = posted_date
            drafts.append({
                "event_date": event_date.isoformat(), "title": f"{item['title']}（にこそく画像解析より検出）",
                "event_type": item.get("event_type") or "OTHER", "importance": item.get("importance") or "MEDIUM",
                "source": "nicosoku_x_image", "source_type": "X_POST_IMAGE", "verification_status": "UNVERIFIED",
                "raw_payload": {"confidence": "MEDIUM", "detected_from": "image_analysis"},
            })
    return drafts


def _x_api_request(path, params=None):
    """X API v2への共通リクエスト。戻り値：(json_or_None, status, detail)。
    status："ok"|"no_key"|"rate_limited"|"timeout"|"failed"。指示書1番により実装方式は
    公式APIのみ（スクレイピング等は一切実装しない）。指示書2番「レート制限を検出したら
    exponential backoff」は呼び出し側（_nicosoku_poll_scheduler_loop）が担当する
    （このレイヤーは1回の試行結果を正しく分類して返すだけ）。"""
    if not X_API_BEARER_TOKEN:
        return None, "no_key", "X_API_BEARER_TOKEN未設定"
    url = f"{X_API_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {X_API_BEARER_TOKEN}",
                                                 "User-Agent": "trade-cockpit/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            return json.loads(res.read().decode("utf-8")), "ok", None
    except urllib.error.HTTPError as e:
        if e.code == 429:
            return None, "rate_limited", "429 Too Many Requests"
        try:
            body = e.read().decode("utf-8")
        except Exception:
            body = str(e)
        return None, "failed", f"HTTP {e.code}: {body[:200]}"
    except TimeoutError:
        return None, "timeout", "タイムアウト"
    except Exception as e:
        return None, "failed", str(e)


def _x_resolve_user_id(username):
    """GET /2/users/by/username/:username 相当（指示書1番）。"""
    data, status, detail = _x_api_request(f"/users/by/username/{username}")
    if status != "ok" or not data or not data.get("data"):
        return None, status, detail
    return data["data"].get("id"), "ok", None


def _x_fetch_recent_tweets(user_id, since_id=None):
    """GET /2/users/:id/tweets 相当（指示書1番）。リプライ・リポストは除外
    （exclude=replies,retweets、引用投稿は本人のオリジナル投稿として残る）。"""
    params = {
        "max_results": "20",
        "exclude": "replies,retweets",
        "tweet.fields": "created_at,public_metrics,entities,attachments,referenced_tweets",
        "expansions": "attachments.media_keys,referenced_tweets.id",
        "media.fields": "url,preview_image_url,type,width,height,alt_text",
    }
    if since_id:
        params["since_id"] = since_id
    return _x_api_request(f"/users/{user_id}/tweets", params)


def _build_social_post_record(database_url, user_id, tweet, media_by_key, source_handle, source_name):
    """1件のtweet dict（X API v2形式）から、DB保存用のsocial_market_posts行を組み立てる
    （指示書3・4・5・6・7・15番）。"""
    text = tweet.get("text") or ""
    post_id = tweet.get("id")
    media_keys = (tweet.get("attachments") or {}).get("media_keys") or []
    media = [media_by_key[k] for k in media_keys if k in media_by_key]
    quoted = None
    for ref in (tweet.get("referenced_tweets") or []):
        if ref.get("type") == "quoted":
            quoted = {"id": ref.get("id")}
    posted_at_str = tweet.get("created_at")
    try:
        posted_at = datetime.datetime.fromisoformat((posted_at_str or "").replace("Z", "+00:00"))
    except Exception:
        posted_at = datetime.datetime.now(datetime.timezone.utc)
    categories = _classify_social_post_categories(text)
    mentions = _detect_social_post_mentions(database_url, user_id, text)
    try:
        positions = investment_db.list_portfolio(database_url, user_id) if investment_db else []
        position_codes = {p.get("code") for p in positions}
    except Exception:
        position_codes = set()
    importance = _classify_social_post_importance(text, categories, mentions["direct_mentions"], position_codes)
    facts, opinions = _split_facts_opinions(text)
    return {
        "source_type": "X_MARKET_SOURCE", "source_name": source_name, "source_handle": source_handle,
        "post_id": post_id, "posted_at": posted_at.isoformat(),
        "text": text, "url": f"https://x.com/{source_handle}/status/{post_id}",
        "media": media, "quoted_post": quoted, "public_metrics": tweet.get("public_metrics") or {},
        "categories": categories, "importance": importance,
        "facts": facts, "author_opinion": opinions, "system_inference": [],
        "direct_mentions": mentions["direct_mentions"], "theme_related": mentions["theme_related"],
        "verification_status": "UNVERIFIED",
    }, posted_at.date()


# ============================================================
# にこそくX連携 Phase2（2026-09-10）：診断機能・優先度スコア・画像解析待ちキューの状態管理強化。
# 既存の売買スコア・AUTO_RS・ENTRY TOP5等には一切加点しない（指示書冒頭・20番）。
# ============================================================

# プロセス内メモリの診断情報（指示書17番）。トークン等の秘密情報は絶対に入れない。
# last_success_at・last_errorはmarket_sources側（DB永続化済み）を正とし、ここは
# 「直近1回の呼び出し」の詳細（プロセス再起動で消えても実害が無い情報）だけを持つ。
_nicosoku_diag = {
    "last_fetch_started_at": None, "last_fetch_finished_at": None,
    "last_error_at": None, "last_error_message": None,
    "fetched_count": 0, "inserted_count": 0, "duplicate_count": 0,
    "last_http_status": None, "poller_running": False,
}

SOCIAL_IMAGE_ANALYSIS_STATUSES = ("NONE", "PENDING", "ANALYZED", "SKIPPED", "FAILED")


def _initial_image_analysis_status(media):
    """指示書1番：新規X投稿保存時のimage_analysis_status初期値。image_urls（media）が
    1件以上あればPENDING、無ければNONE。"""
    return "PENDING" if media else "NONE"


# 指示書5番：解析優先度スコアの配点。同じ理由を重複加点しないよう、「言及」区分（保有/監視/
# テーマ）と「カテゴリ」区分はそれぞれ最も高い1件のみを加点する。
_SOCIAL_PRIORITY_IMPORTANCE = {"CRITICAL": 35, "HIGH": 25, "MEDIUM": 10}
_SOCIAL_PRIORITY_CATEGORY = {
    "ECONOMIC_EVENT": 15, "CENTRAL_BANK": 15, "MARKET_HEATMAP": 12,
    "INDEX_TECHNICAL": 10, "STOCK_TECHNICAL": 10, "MARKET_SENTIMENT": 8,
}


def _post_age_minutes(posted_at, now=None):
    """posted_at（ISO文字列）から現在までの経過分。解析不能ならNone。"""
    if not posted_at:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(posted_at).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
    except Exception:
        return None
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return (now - dt).total_seconds() / 60


def compute_social_post_priority_score(post, watch_codes=None, position_codes=None, now=None):
    """指示書5番：投稿1件のanalysis_priority_score（0〜100）を動的計算する。DBへ新規列を
    足さず、APIレスポンス生成時に都度計算する方針（指示書「必須保存する必要はない」）。"""
    watch_codes = watch_codes or set()
    position_codes = position_codes or set()
    score = 0
    score += _SOCIAL_PRIORITY_IMPORTANCE.get(post.get("importance"), 0)
    age_min = _post_age_minutes(post.get("posted_at"), now=now)
    if age_min is not None:
        if age_min <= 30:
            score += 20
        elif age_min <= 60:
            score += 10
    direct = set(post.get("direct_mentions_json") or [])
    theme = set(post.get("theme_related_json") or [])
    if direct & position_codes:
        score += 20
    elif direct & watch_codes:
        score += 15
    elif theme:
        score += 8
    cats = set(post.get("categories_json") or [])
    cat_bonus = max((_SOCIAL_PRIORITY_CATEGORY[c] for c in cats if c in _SOCIAL_PRIORITY_CATEGORY), default=0)
    score += cat_bonus
    if len(post.get("media_json") or []) > 1:
        score += 5
    return min(score, 100)


def social_post_priority_label(score):
    """指示書6番：スコアからURGENT/HIGH/NORMAL/LOWラベルを生成する。"""
    if score >= 80:
        return "URGENT"
    if score >= 60:
        return "HIGH"
    if score >= 30:
        return "NORMAL"
    return "LOW"


def _enrich_social_post_priority(post, watch_codes=None, position_codes=None, now=None):
    """指示書5・6・9番：投稿dictにanalysis_priority_score・priority_label・
    related_positions・related_watchlist を追加専用フィールドとして付与する（既存キーは
    一切変更しない）。"""
    watch_codes = watch_codes or set()
    position_codes = position_codes or set()
    score = compute_social_post_priority_score(post, watch_codes, position_codes, now=now)
    direct = set(post.get("direct_mentions_json") or [])
    theme = set(post.get("theme_related_json") or [])
    mentions = direct | theme
    post = dict(post)
    post["analysis_priority_score"] = score
    post["priority_label"] = social_post_priority_label(score)
    post["related_positions"] = sorted(mentions & position_codes)
    post["related_watchlist"] = sorted(mentions & watch_codes)
    return post


def sort_pending_posts_by_priority(posts):
    """指示書7番：PENDING投稿をanalysis_priority_score DESC・posted_at DESCで並べる。
    posts中の各要素は事前にanalysis_priority_scoreが付与済みである前提（未付与は0扱い）。
    PENDING以外の投稿は並び替えの対象にせずそのままの順序で末尾に残す（安定ソート）。"""
    def key(p):
        return (p.get("image_analysis_status") != "PENDING",
                -(p.get("analysis_priority_score") or 0),
                "" if not p.get("posted_at") else "")
    pending = [p for p in posts if p.get("image_analysis_status") == "PENDING"]
    others = [p for p in posts if p.get("image_analysis_status") != "PENDING"]
    pending.sort(key=lambda p: (p.get("posted_at") or ""), reverse=True)
    pending.sort(key=lambda p: p.get("analysis_priority_score") or 0, reverse=True)
    return pending + others


def _normalize_confidence(value):
    """指示書10番：画像解析結果のconfidenceを0.0〜1.0へ正規化する。値が無い/変換不能なら
    None（無理に0埋めしない）。"""
    if value is None or value == "":
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v != v:  # NaN
        return None
    return max(0.0, min(1.0, v))


def _nicosoku_market_hours_jst(now_jst=None):
    jst = datetime.timezone(datetime.timedelta(hours=9))
    now_jst = now_jst or datetime.datetime.now(jst)
    hhmm = now_jst.strftime("%H:%M")
    return "08:00" <= hhmm <= "15:40"


def _nicosoku_source_is_stale(source, now=None):
    """指示書14番：last_success_atをもとにSTALE判定する。市場時間中(08:00-15:40 JST)は
    15分、それ以外は30分を閾値とする。last_success_atが無ければ（一度も成功していない）
    STALE扱い。"""
    last_success_at = (source or {}).get("last_success_at")
    if not last_success_at:
        return True
    try:
        dt = datetime.datetime.fromisoformat(str(last_success_at).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
    except Exception:
        return True
    now = now or datetime.datetime.now(datetime.timezone.utc)
    age_min = (now - dt).total_seconds() / 60
    jst = datetime.timezone(datetime.timedelta(hours=9))
    threshold = 15 if _nicosoku_market_hours_jst(now.astimezone(jst)) else 30
    return age_min > threshold


def build_social_posts_response(database_url, user_id, limit=10, min_importance=None):
    """GET /api/social-posts の実体（指示書5・6・7・8番）。各投稿へanalysis_priority_score等を
    付与し、PENDING投稿はスコア降順→新しさ降順で並べ替える。pendingSummaryは表示件数
    （limit）に関わらず、SKIPPEDを除く全PENDING件数を対象にする（指示書8番「解析待ち件数」・
    3番「SKIPPED投稿は通常の解析待ち件数から除外」）。"""
    if investment_db is None or not database_url:
        return {"posts": [], "pendingSummary": {"pending": 0, "urgent": 0}}
    posts = investment_db.list_recent_social_posts(
        database_url, source_handle=NICOSOKU_X_USERNAME, min_importance=min_importance, limit=limit)
    try:
        watch_codes = {w.get("code") for w in investment_db.list_watchlist(database_url, user_id, market="JP")}
        position_codes = {p.get("code") for p in investment_db.list_portfolio(database_url, user_id)}
    except Exception:
        watch_codes, position_codes = set(), set()
    enriched = [_enrich_social_post_priority(p, watch_codes, position_codes) for p in posts]
    ordered = sort_pending_posts_by_priority(enriched)
    all_pending = investment_db.list_recent_social_posts(
        database_url, source_handle=NICOSOKU_X_USERNAME, limit=500, image_analysis_status="PENDING")
    pending_scores = [compute_social_post_priority_score(p, watch_codes, position_codes) for p in all_pending]
    pending_summary = {"pending": len(all_pending), "urgent": sum(1 for s in pending_scores if s >= 80)}
    return {"posts": ordered, "pendingSummary": pending_summary}


def nicosoku_diagnostics(database_url, user_id):
    """指示書11番：GET /api/social-sources/nicosoku/diagnostics の実体。Bearer Tokenそのもの
    は絶対に返さない（token_configuredの真偽値のみ）。"""
    source = investment_db.get_market_source(database_url, NICOSOKU_X_USERNAME) \
        if (investment_db is not None and database_url) else None
    latest = investment_db.list_recent_social_posts(database_url, source_handle=NICOSOKU_X_USERNAME, limit=1) \
        if (investment_db is not None and database_url) else []
    latest_post = latest[0] if latest else None
    rate_limit_status = "RATE_LIMITED" if _nicosoku_diag.get("last_http_status") == "429" else "OK"
    return {
        "token_configured": bool(X_API_BEARER_TOKEN),
        "username": NICOSOKU_X_USERNAME,
        "user_id_resolved": bool(_nicosoku_x_user_id_cache),
        "last_success_at": (source or {}).get("last_success_at"),
        "last_error": (source or {}).get("last_error"),
        "rate_limit_status": rate_limit_status,
        "latest_post_id": (latest_post or {}).get("post_id"),
        "latest_post_at": (latest_post or {}).get("posted_at"),
        "latest_post_has_media": bool((latest_post or {}).get("media_json")),
        "poller_running": _nicosoku_diag["poller_running"],
        "stale": _nicosoku_source_is_stale(source),
        "last_fetch_started_at": _nicosoku_diag["last_fetch_started_at"],
        "last_fetch_finished_at": _nicosoku_diag["last_fetch_finished_at"],
        "last_fetched_count": _nicosoku_diag["fetched_count"],
        "last_inserted_count": _nicosoku_diag["inserted_count"],
        "last_duplicate_count": _nicosoku_diag["duplicate_count"],
    }


def nicosoku_poll_once(database_url, user_id):
    """1サイクル分のポーリング（指示書1・2・3・8番）。ユーザーID解決→未取得分の投稿取得→
    分類・保存→イベント検出→market_sourcesの状態更新、までを1回実行する。戻り値：
    {"status","newPosts","fetched","duplicates","eventsDetected","error"}。
    Phase2（指示書11・12・17番）：診断API・手動「今すぐ取得」の両方がこの関数をそのまま
    再利用する（別実装を作らない）。_nicosoku_diag（プロセス内メモリ）への記録もここで行う。"""
    result = {"status": "ok", "newPosts": 0, "fetched": 0, "duplicates": 0, "eventsDetected": 0, "error": None}
    _nicosoku_diag["last_fetch_started_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    if investment_db is None or not database_url:
        result["status"] = "no_db"
        return result
    source = investment_db.ensure_market_source(database_url, X_SOCIAL_SOURCE_PLATFORM, NICOSOKU_X_USERNAME,
                                                  display_name="にこそく", priority="HIGH",
                                                  categories=["JP_MARKET", "MACRO"])
    if not X_API_BEARER_TOKEN:
        result["status"] = "no_key"
        return result

    # X APIのuser_id（数値ID）はmarket_sources.last_seen_post_idとは別物。DB列を1つ増やす
    # ほどのものではないため、プロセス内メモリのキャッシュで十分（再起動時は再解決するだけで
    # 実害はない）。
    global _nicosoku_x_user_id_cache
    if not _nicosoku_x_user_id_cache:
        uid, status, detail = _x_resolve_user_id(NICOSOKU_X_USERNAME)
        if status != "ok" or not uid:
            investment_db.update_market_source_status(database_url, NICOSOKU_X_USERNAME, last_error=f"ユーザーID解決失敗: {detail}")
            _nicosoku_diag["last_error_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            _nicosoku_diag["last_error_message"] = detail
            _nicosoku_diag["last_fetch_finished_at"] = _nicosoku_diag["last_error_at"]
            result["status"] = status
            result["error"] = detail
            return result
        _nicosoku_x_user_id_cache = uid
    x_user_id = _nicosoku_x_user_id_cache

    since_id = (source or {}).get("last_seen_post_id")
    data, status, detail = _x_fetch_recent_tweets(x_user_id, since_id=since_id)
    _nicosoku_diag["last_http_status"] = "429" if status == "rate_limited" else ("200" if status == "ok" else status)
    if status != "ok":
        investment_db.update_market_source_status(database_url, NICOSOKU_X_USERNAME, last_error=f"投稿取得失敗: {detail}")
        _nicosoku_diag["last_error_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        _nicosoku_diag["last_error_message"] = detail
        _nicosoku_diag["last_fetch_finished_at"] = _nicosoku_diag["last_error_at"]
        investment_db.update_market_source_fetch_stats(database_url, NICOSOKU_X_USERNAME,
            last_fetch_started_at=_nicosoku_diag["last_fetch_started_at"], last_fetch_finished_at="NOW()",
            last_error_at="NOW()", last_http_status=_nicosoku_diag["last_http_status"])
        result["status"] = status
        result["error"] = detail
        return result

    tweets = (data or {}).get("data") or []
    result["fetched"] = len(tweets)
    media_list = ((data or {}).get("includes") or {}).get("media") or []
    media_by_key = {m.get("media_key"): {"url": m.get("url") or m.get("preview_image_url"),
                                           "type": m.get("type"), "width": m.get("width"),
                                           "height": m.get("height"), "alt_text": m.get("alt_text")}
                    for m in media_list}
    max_id = since_id
    for tweet in tweets:
        record, posted_date = _build_social_post_record(database_url, user_id, tweet, media_by_key,
                                                           NICOSOKU_X_USERNAME, "にこそく")
        saved = investment_db.insert_social_post_if_new(database_url, record)
        if saved:
            result["newPosts"] += 1
            event_drafts = _detect_events_from_social_text(record["text"], posted_date)
            if event_drafts:
                try:
                    existing = investment_db.list_market_events(
                        database_url, user_id, from_date=posted_date.isoformat(),
                        to_date=(posted_date + datetime.timedelta(days=120)).isoformat())
                    existing_titles = {(e.get("event_date"), _event_title_key(e.get("title"))) for e in existing}
                    fresh = [d for d in event_drafts
                             if (d["event_date"], _event_title_key(d["title"])) not in existing_titles
                             and not any(_event_title_key(d["title"])[:6] in _event_title_key(t) for _, t in existing_titles)]
                    if fresh:
                        imp_result = investment_db.import_market_events(database_url, user_id, fresh)
                        result["eventsDetected"] += imp_result.get("imported", 0)
                except Exception as e:
                    print("  にこそく投稿からのイベント検出で例外", e)
        else:
            result["duplicates"] += 1
        if tweet.get("id") and (max_id is None or int(tweet["id"]) > int(max_id)):
            max_id = tweet["id"]

    investment_db.update_market_source_status(database_url, NICOSOKU_X_USERNAME,
                                                last_seen_post_id=max_id, mark_success=True)
    finished_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    _nicosoku_diag["last_fetch_finished_at"] = finished_at
    _nicosoku_diag["fetched_count"] = result["fetched"]
    _nicosoku_diag["inserted_count"] = result["newPosts"]
    _nicosoku_diag["duplicate_count"] = result["duplicates"]
    investment_db.update_market_source_fetch_stats(
        database_url, NICOSOKU_X_USERNAME,
        last_fetch_started_at=_nicosoku_diag["last_fetch_started_at"], last_fetch_finished_at=finished_at,
        last_http_status=_nicosoku_diag["last_http_status"], last_fetched_count=result["fetched"],
        last_inserted_count=result["newPosts"], last_duplicate_count=result["duplicates"])
    return result


_nicosoku_x_user_id_cache = None


def _event_title_key(title):
    """タイトルの簡易正規化（空白除去・小文字化）。厳密な重複判定ではなく、投稿から検出した
    イベントが既存の公式イベントとおおよそ同じかを見る軽量チェック用（指示書17番
    「重複チェック必須」、完全一致でなくても明らかに同じイベントの二重登録を避ける）。"""
    return re.sub(r"\s+", "", (title or "")).lower()


def get_recent_social_market_signals(database_url, user_id, lookback_minutes=180, min_importance="MEDIUM", limit=8):
    """指示書9番：recent_social_market_signals。ChatGPT相談JSONへ含める、絞り込み済みの
    投稿一覧を返す。全投稿ではなく、時間・重要度・現在の監視銘柄/ポジションとの関連性で
    絞り込む。"""
    if investment_db is None or not database_url:
        return []
    since_iso = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=lookback_minutes)).isoformat()
    posts = investment_db.list_social_signals(database_url, since_iso, min_importance=min_importance, limit=limit * 3)
    try:
        watch_codes = {w.get("code") for w in investment_db.list_watchlist(database_url, user_id, market="JP")}
        positions = investment_db.list_portfolio(database_url, user_id)
        position_codes = {p.get("code") for p in positions}
    except Exception:
        watch_codes, position_codes = set(), set()
    relevant_codes = watch_codes | position_codes
    rank = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}
    # 指示書15・16番：X APIソースがSTALE（更新停止）の場合、この関数が返す投稿は「参考情報
    # として残す」が「NEW MARKET SIGNAL判定には使わない」ことを呼び出し側が判別できるよう、
    # 各項目へdata_statusを追加専用フィールドとして付与する（既存キーには触れない）。
    try:
        source = investment_db.get_market_source(database_url, NICOSOKU_X_USERNAME)
        data_status = "STALE" if _nicosoku_source_is_stale(source) else "OK"
    except Exception:
        data_status = "STALE"
    scored = []
    for p in posts:
        mentions = set(p.get("direct_mentions_json") or []) | set(p.get("theme_related_json") or [])
        relevant = bool(mentions & relevant_codes)
        if rank.get(p.get("importance"), 0) < rank.get("HIGH", 2) and not relevant:
            continue  # MEDIUM以下は関連銘柄が無ければ落とす（指示書「全投稿を送らない」）
        summary = (p.get("text") or "")[:80]
        facts = list(p.get("facts_json") or [])
        # 画像解析待ちキュー：ANALYZED済みの画像があれば、その構造化結果（market_implications/
        # observations等）もfactsへ添える（指示書「解析結果をrecent_social_market_signals…
        # へ利用する」）。売買スコアへは一切加点しない（この関数はChatGPT相談用の参考情報を
        # 返すだけで、entry_score等の既存スコアには触れない）。
        if p.get("image_analysis_status") == "ANALYZED":
            for a in (p.get("image_analysis_json") or []):
                if not isinstance(a, dict):
                    continue
                for key in ("market_implications", "observations"):
                    v = a.get(key)
                    if isinstance(v, str) and v:
                        facts.append(f"[画像解析] {v}")
                    elif isinstance(v, list):
                        facts.extend(f"[画像解析] {x}" for x in v if isinstance(x, str))
        scored.append({
            "source": "nicosoku", "posted_at": p.get("posted_at"), "importance": p.get("importance"),
            "categories": p.get("categories_json") or [], "summary": summary,
            "facts": facts, "author_opinion": p.get("author_opinion_json") or [],
            "relevance": sorted(mentions), "url": p.get("url"), "data_status": data_status,
        })
    scored.sort(key=lambda s: (-rank.get(s["importance"], 0), s["posted_at"] or ""), reverse=False)
    scored.sort(key=lambda s: -rank.get(s["importance"], 0))
    return scored[:limit]


def _nicosoku_morning_commentary(database_url, user_id):
    """指示書10番：朝一チェックの補助材料。前日15:30〜当日08:30(JST)程度の投稿から要点を
    抽出する。既存のmorning_market_check本体ロジックには一切干渉しない、追加専用フィールド。"""
    if investment_db is None or not database_url:
        return None
    jst = datetime.timezone(datetime.timedelta(hours=9))
    now_jst = datetime.datetime.now(jst)
    since_jst = (now_jst - datetime.timedelta(hours=17))  # 前日15:30頃〜のおおよそのカバー
    since_iso = since_jst.astimezone(datetime.timezone.utc).isoformat()
    posts = investment_db.list_recent_social_posts(database_url, source_handle=NICOSOKU_X_USERNAME,
                                                      since_iso=since_iso, min_importance="MEDIUM", limit=10)
    if not posts:
        return None
    key_points, related = [], set()
    for p in posts[:5]:
        text = (p.get("text") or "")[:60]
        if text:
            key_points.append(text)
        related |= set(p.get("direct_mentions_json") or []) | set(p.get("theme_related_json") or [])
    # 指示書15番：source status != STALE の場合だけ latest_signal として扱う安全ガード。
    # STALEでも内容自体は参考情報として残す（data_statusで明示するだけで、生成自体は行う）。
    try:
        source = investment_db.get_market_source(database_url, NICOSOKU_X_USERNAME)
        data_status = "STALE" if _nicosoku_source_is_stale(source) else "OK"
    except Exception:
        data_status = "STALE"
    return {
        "latest_post_at": posts[0].get("posted_at"), "key_points": key_points,
        "market_implication": "、".join(key_points[:2]) if key_points else "",
        "related_stocks": sorted(related), "data_status": data_status,
    }


def _nicosoku_morning_commentary_safe(database_url, user_id):
    """指示書10・21番：MorningMarketCheck本体を絶対に壊さないよう、例外は握りつぶしNoneを
    返す（この機能単体の不具合で朝一チェック生成自体が失敗することを防ぐ）。"""
    try:
        return _nicosoku_morning_commentary(database_url, user_id)
    except Exception as e:
        print("  にこそく朝一コメンタリー生成で例外（無視して続行）", e)
        return None


def _nicosoku_intraday_signals(database_url, user_id, lookback_minutes=60):
    """指示書11番：INTRADAY_REPORT生成時に直近60分以内・HIGH/CRITICALの投稿だけを参照する
    （優先度の高い投稿に限定、指示書の明示的な条件）。既存のレポート生成・エントリー判定
    ロジックには一切影響しない、追加専用フィールド。"""
    return get_recent_social_market_signals(database_url, user_id, lookback_minutes=lookback_minutes,
                                               min_importance="HIGH", limit=5)


def _nicosoku_intraday_signals_safe(database_url, user_id, lookback_minutes=60):
    """指示書11・21番：generate_intraday_report本体を絶対に壊さないよう例外を握りつぶす。"""
    try:
        return _nicosoku_intraday_signals(database_url, user_id, lookback_minutes)
    except Exception as e:
        print("  にこそく場中シグナル参照で例外（無視して続行）", e)
        return []


def _nicosoku_poll_interval_seconds():
    """指示書2番：市場時間中(08:00-15:40 JST)は3-5分間隔、それ以外は10-15分間隔（固定値では
    なくレンジの中間値を採用）。X APIのプラン・レート制限は環境変数側の契約に委ねる
    （ハードコードしているのはあくまでポーリング頻度の目安であり、レート制限自体はAPI応答
    （429）で検出しbackoffする、別のレイヤー）。"""
    jst = datetime.timezone(datetime.timedelta(hours=9))
    now_jst = datetime.datetime.now(jst)
    hhmm = now_jst.strftime("%H:%M")
    return 240 if "08:00" <= hhmm <= "15:40" else 750  # 4分 / 12.5分


def _nicosoku_poll_scheduler_loop():
    """バックグラウンドポーリングのデーモンスレッド。X_API_BEARER_TOKEN未設定なら何もせず
    終了する（アプリ本体の動作には影響しない、指示書19番）。429検出時はexponential backoff
    （指示書2番）。"""
    if not X_API_BEARER_TOKEN:
        print("  [にこそくX連携] X_API_BEARER_TOKEN未設定のためポーリングは無効（X_SOURCE_STATUS=DEGRADED）")
        return
    _nicosoku_diag["poller_running"] = True
    consecutive_failures = 0
    while True:
        try:
            user_id = _morning_check_scheduler_users()[0]
            result = nicosoku_poll_once(DATABASE_URL, user_id)
            if result["status"] == "ok":
                consecutive_failures = 0
                if result["newPosts"] > 0:
                    print(f"  [にこそくX連携] fetched={result['fetched']}件・新規{result['newPosts']}件・"
                          f"重複{result['duplicates']}件・イベント検出{result['eventsDetected']}件")
            elif result["status"] == "rate_limited":
                consecutive_failures += 1
            else:
                consecutive_failures += 1
                print(f"  [にこそくX連携] 取得失敗（{result['status']}）：{result.get('error')}")
        except Exception as e:
            consecutive_failures += 1
            print("  [にこそくX連携] ポーリングループで例外", e)
        backoff_multiplier = min(2 ** consecutive_failures, 16) if consecutive_failures > 0 else 1
        time.sleep(_nicosoku_poll_interval_seconds() * backoff_multiplier)


def generate_opening_30m_report(database_url, user_id, trade_date=None):
    """後方互換の薄いラッパー（Phase2-A時点の呼び出し名をそのまま維持）。実体は
    generate_intraday_report()に一般化した（指示書「実装方針」：09:30専用ロジックを
    11:30/13:00/15:30へコピーせず、report_typeによる分岐で共通処理する）。"""
    return generate_intraday_report(database_url, user_id, "OPENING_30M", trade_date)


def generate_intraday_report(database_url, user_id, report_type, trade_date=None):
    """Market Intelligence Timelineの共通レポートエンジン（market_report_serviceの中核）。
    report_typeはOPENING_30M/MORNING_CLOSE/AFTERNOON_30M/MARKET_CLOSEのいずれか。
    朝一予想（MorningMarketCheckのT0850）が無くても、その日の実市場スナップショットだけは
    残す（DATA_INSUFFICIENTを使い、レポート自体は落とさない方針、指示書29番）。"""
    if report_type not in INTRADAY_REPORT_SNAPSHOT_TIMES:
        raise ValueError(f"未対応のreport_type: {report_type}")
    data_health = {}
    trade_date = trade_date or _jst_today_date_str()
    morning_check = investment_db.get_latest_morning_check(database_url, user_id, check_date=trade_date) if investment_db else None
    data_health["morning_check"] = "ok" if morning_check else "missing"
    previous_report, previous_kind = _previous_intraday_reference(database_url, user_id, trade_date, report_type, morning_check)
    data_health["previous_report"] = previous_kind or "missing"

    # 2026-09-10更新（レート制限耐性）：kospiも共通キャッシュ対象に加える（指示書1番）。
    # 実際の取得はすべて_fetch_index_snapshot→_cached_two_closes経由でキャッシュ+リトライ+
    # stale fallbackが効くため、09:30/11:30/13:00/15:30が短時間に連続実行されても
    # Yahoo Financeへの重複リクエストにならない。
    index_keys = ["nikkei", "topix_etf", "growth250_etf", "nikkei_vi_etn", "usdjpy", "nasdaq", "sox", "us10y", "kospi"]
    try:
        indices = _fetch_index_snapshot(index_keys)
    except Exception as e:
        print("  IntradayReport: 指数取得で例外", e)
        indices = {}
    source_statuses = {}  # data_quality集計用（指示書7番）。key=ソース名、value=cache_status
    for k in index_keys:
        data_health[k] = indices.get(k, {}).get("status", "failed")
        source_statuses[k] = indices.get(k, {}).get("cache_status", "failed")

    # 指示書6番：指数取得が完全に失敗した（値もキャッシュも無い）場合、前回レポート/MorningCheckに
    # 保存済みの値へ最終フォールバックする（DBを「前回snapshot」として再利用）。あくまで最終手段
    # であり、フォールバックした値はsource_statusesで"stale_cache"として明示する。
    # 直前のintraday reportはpayload列（例：nikkei_change_pct）にスカラーで持つが、MorningCheckは
    # 列を持たずindices_json内にネストされているため、両方の形を吸収する。
    def _fallback_scalar(index_key, index_field, payload_field):
        v = indices.get(index_key, {}).get(index_field)
        if v is not None:
            return v
        if previous_report is not None and previous_kind != "morning_check" and previous_report.get(payload_field) is not None:
            source_statuses[index_key] = "stale_cache"
            return previous_report[payload_field]
        if morning_check is not None:
            mc_val = (morning_check.get("indices_json") or {}).get(index_key, {}).get(index_field)
            if mc_val is not None:
                source_statuses[index_key] = "stale_cache"
                return mc_val
        return None

    nikkei_chg = _fallback_scalar("nikkei", "changePct", "nikkei_change_pct")
    n225_trend = _index_trend(INDEX["nikkei"])
    nasdaq_trend = _index_trend(INDEX["nasdaq"])
    sox_trend = _index_trend(INDEX["sox"])
    us10y_trend = _index_trend(INDEX["us10y"])
    usdjpy_trend = _index_trend(INDEX["usdjpy"])
    risk = _market_risk_score(n225_trend, nasdaq_trend, sox_trend, us10y_trend, usdjpy_trend)
    volatility_score, _ = _morning_volatility_score(indices)
    market_regime, volatility_regime, _trend_type = classify_morning_regime(risk["score"], volatility_score, indices)
    strong_sectors, weak_sectors = compute_macro_sector_strength(indices, {})

    regime_transition = None
    if morning_check:
        regime_transition = {"from": morning_check.get("market_regime"), "to": market_regime}
    # 指示書4番：前回レポート（直前の時間帯）との地合い比較。MorningCheckとの差分（regime_transition）
    # とは別に、「直前の時間帯から何が変わったか」も見る（例：09:30 MILD_RISK_OFF→11:30 NEUTRAL）。
    previous_regime = previous_report.get("market_regime") if previous_report else None
    regime_changed_since_previous = previous_kind not in (None, "morning_check") and previous_regime is not None and previous_regime != market_regime

    # 指示書4番：セクターの簡易ローテーション差分（本格的な順位統計はPhase2-D）。前回レポートの
    # 強い/弱いセクターと比べ、新規に強く/弱くなったセクターだけを検出する。
    previous_strong = set((previous_report.get("strong_sectors_json") or [])) if previous_report and previous_kind != "morning_check" else set()
    previous_weak = set((previous_report.get("weak_sectors_json") or [])) if previous_report and previous_kind != "morning_check" else set()
    newly_strong_sectors = [s for s in strong_sectors if s not in previous_strong]
    newly_weak_sectors = [s for s in weak_sectors if s not in previous_weak]

    # ---- 朝TOP5の答え合わせ＋前回レポートからの変化（指示書1・5・6番） ----
    thesis_stocks = []
    morning_top5 = (morning_check.get("watchlist_top5_json") or []) if morning_check else []
    prev_thesis_by_code = {}
    if previous_report and previous_kind != "morning_check":
        for s in (previous_report.get("morning_thesis_evaluation_json") or {}).get("stocks", []):
            prev_thesis_by_code[s.get("code")] = s.get("thesis_result")
    for item in morning_top5:
        w_item = {"code": item.get("code"), "name": item.get("name"), "market": "JP"}
        try:
            snap = _intraday_stock_snapshot(w_item)
        except Exception as e:
            print("  IntradayReport: 朝TOP5答え合わせで例外", item.get("code"), e)
            snap = {"current": None, "currentChangePct": None, "aboveVwap": None,
                    "fiveMinStructure": None, "dataStatus": "failed", "cacheStatus": "failed"}
        _rank = {"rate_limited": 3, "failed": 2, "stale_cache": 1, "ok": 0}
        if _rank.get(snap.get("cacheStatus", "ok"), 0) > _rank.get(source_statuses.get("watchlist_top5", "ok"), 0):
            source_statuses["watchlist_top5"] = snap.get("cacheStatus", "ok")
        result = evaluate_morning_thesis(item, snap, nikkei_chg)
        entry = {
            "code": item.get("code"), "name": item.get("name"), "morning_rank": item.get("rank"),
            "morning_stance": "WATCH_LONG", "current_change_pct": snap["currentChangePct"],
            "above_vwap": snap["aboveVwap"], "five_min_structure": snap["fiveMinStructure"],
            "thesis_result": result,
        }
        if report_type != "OPENING_30M":
            # 09:30は「初回評価」のため変化なし。11:30以降のみSTRENGTHENED/MAINTAINED/WEAKENED/FAILEDを付与
            prev_result = prev_thesis_by_code.get(item.get("code"))
            entry["prior_result"] = prev_result
            entry["status"] = _thesis_transition_status(prev_result, result)
        thesis_stocks.append(entry)

    # ---- 15:30大引けのみ：当日全時間帯の履歴からTOP5の最終結果を判定（指示書3番） ----
    if report_type == "MARKET_CLOSE" and morning_top5:
        try:
            day_reports = investment_db.list_market_intelligence_reports(database_url, user_id, trade_date) if investment_db else []
        except Exception as e:
            print("  IntradayReport: 当日レポート履歴取得で例外", e)
            day_reports = []
        history_by_code = {}
        for rep in day_reports:
            for s in (rep.get("morning_thesis_evaluation_json") or {}).get("stocks", []):
                history_by_code.setdefault(s.get("code"), []).append(s.get("thesis_result"))
        for entry in thesis_stocks:
            hist = history_by_code.get(entry["code"], []) + [entry["thesis_result"]]
            entry["final_result"] = _final_top5_result(hist)

    if morning_top5:
        data_health["thesis_evaluation"] = "ok" if any(s["thesis_result"] != "DATA_INSUFFICIENT" for s in thesis_stocks) else "failed"
    else:
        data_health["thesis_evaluation"] = "no_morning_check"

    # ---- Phase2-C：entry_ready_top5から作ったstock_thesesの答え合わせ（朝TOP5とは別の仮説群、
    # 同じevaluate_morning_thesis/_thesis_transition_status/_final_top5_resultを再利用） ----
    try:
        _reevaluate_active_stock_theses(database_url, user_id, trade_date, report_type, nikkei_chg)
        data_health["entry_theses"] = "ok"
    except Exception as e:
        print("  IntradayReport: entry_ready_top5仮説の答え合わせで例外", e)
        data_health["entry_theses"] = "failed"

    # ---- 地合い耐性ランキング（既存ロジック再利用、指示書7・30番） ----
    # 2026-09-10更新（レート制限耐性）：cache_ttlを指定してget_stock_quotes/fetch_adr_snapshotの
    # キャッシュを有効化する（Morning Checkはcache_ttl省略のまま呼ぶので影響を受けない）。
    focus_status = {}
    try:
        focus = generate_morning_watchlist_focus(database_url, user_id, {"indices": indices},
                                                   cache_ttl=CACHE_TTL["stock_quote"], status_out=focus_status)
        data_health["resilience"] = "ok"
        source_statuses.update(focus_status)
    except Exception as e:
        print("  IntradayReport: 地合い耐性ランキングで例外", e)
        focus = {"top5": [], "avoid": [], "resilience": []}
        data_health["resilience"] = "failed"
        source_statuses["watchlist_quotes"] = "failed"

    # ---- 13:00後場30分のみ：資金移動の簡易分類（指示書2番）。根拠が弱い場合は推測せずUNKNOWN
    # （REVERSAL/SHORT_COVER/WEAKENINGは信用残高等のデータが無いと判定できないためPhase2-C以降）。
    afternoon_flow = []
    if report_type == "AFTERNOON_30M":
        previous_resilience_codes = {r.get("code") for r in (previous_report.get("resilience_stocks_json") or [])} \
            if previous_report and previous_kind != "morning_check" else set()
        for r in focus["resilience"]:
            code = r.get("code")
            try:
                cats = investment_db.relevant_catalysts_for(database_url, user_id, code=code, limit=1) if investment_db else []
            except Exception:
                cats = []
            has_fresh_catalyst = bool(cats) and cats[0].get("freshness") in ("LIVE", "CURRENT")
            if code in previous_resilience_codes:
                classification, reason = "CONTINUATION", None
            elif has_fresh_catalyst:
                classification, reason = "NEWS_DRIVEN", cats[0].get("title", "")[:30]
            elif previous_kind not in (None,):
                classification, reason = "NEW_FLOW", None
            else:
                classification, reason = "UNKNOWN", "根拠不十分なため推測しない"
            afternoon_flow.append({"code": code, "name": r.get("name"), "classification": classification, "reason": reason})

    # ---- 保有ポジションのリスク（既存position_risk_rulesをそのまま使用、指示書10番） ----
    # 重要：EXIT RULE(-8%)の判定ロジック自体（evaluate_position_risk_warnings）は一切変更しない
    # （損切りルール是正の意図を壊さないため）。変更するのは現在値の取得経路のキャッシュ利用のみ。
    positions_all = []  # try節で例外が起きても後段のルール違反検出（15:30のみ）がNameErrorにならないよう初期化
    try:
        watchlist_all = investment_db.list_watchlist(database_url, user_id, market="JP") if investment_db else []
        positions_all = investment_db.list_portfolio(database_url, user_id) if investment_db else []
        quote_targets = {w["code"]: w for w in watchlist_all}
        for p in positions_all:
            quote_targets.setdefault(p.get("code"), {"code": p.get("code"), "market": "JP"})
        position_quote_status = {}
        stock_quotes = get_stock_quotes(list(quote_targets.values()), cache_ttl=CACHE_TTL["stock_quote"],
                                          status_out=position_quote_status) if quote_targets else {}
        position_alerts = evaluate_position_risk_warnings(database_url, user_id, stock_quotes)
        data_health["positions"] = "ok"
        _rank = {"rate_limited": 3, "failed": 2, "stale_cache": 1, "ok": 0}
        if position_quote_status:
            source_statuses["position_quotes"] = max(position_quote_status.values(), key=lambda v: _rank.get(v, 0))
    except Exception as e:
        print("  IntradayReport: ポジションリスク判定で例外", e)
        position_alerts = []
        data_health["positions"] = "failed"
        source_statuses["position_quotes"] = "failed"

    # ---- イベント・ニュース（既存判断エンジンをそのまま再利用、指示書5・17番） ----
    try:
        event_info = investment_db.upcoming_event_signals(database_url, user_id) if investment_db else {"events": [], "signals": []}
    except Exception as e:
        print("  IntradayReport: イベント取得で例外", e)
        event_info = {"events": [], "signals": []}
    try:
        # 指示書5番：厳密な「前回レポート生成時刻以降」の差分は現状のcatalyst_date（日付粒度）
        # では断定できないため、直近のLIVE/CURRENT鮮度のものだけに絞り、断定表現は避ける
        # （possible_driver程度に留める、指示書5番）。ニュースはDB問い合わせ（Yahoo Financeの
        # レート制限対象ではない）だが、指示書2番のTTL方針に合わせ短時間の重複問い合わせだけ
        # キャッシュで避ける（5〜10分、DB負荷軽減目的）。
        news_cache_key = f"news:{user_id}"
        news_entry = _cache_get(news_cache_key)
        if _cache_fresh(news_entry, CACHE_TTL["news"]):
            news_changes_raw = news_entry["value"]
        else:
            news_changes_raw = investment_db.relevant_catalysts_for(database_url, user_id, limit=8) if investment_db else []
            _cache_set(news_cache_key, news_changes_raw)
        news_changes = [{**c, "possible_driver": True} if c.get("freshness") in ("LIVE", "CURRENT") else c for c in news_changes_raw]
        data_health["news"] = "ok"
    except Exception as e:
        print("  IntradayReport: ニュース差分取得で例外", e)
        news_changes = []
        data_health["news"] = "failed"
        source_statuses["news"] = "failed"

    # ---- 15:30大引けのみ：ルール違反検出（既存_check_known_risk_ignoredを再利用、指示書3番） ----
    rule_violations = []
    if report_type == "MARKET_CLOSE":
        try:
            rule_violations = investment_db._check_known_risk_ignored(
                database_url, user_id, trade_date, positions_all, new_positions=[]) if investment_db else []
        except Exception as e:
            print("  IntradayReport: ルール違反検出で例外", e)
            rule_violations = []

    risk_alerts = []
    major_changes = []
    if any(a["level"] == "CRITICAL" for a in position_alerts):
        risk_alerts.append({"level": "CRITICAL", "message": "保有銘柄が損切りルール（EXIT RULE）に到達"})
        major_changes.append("EXIT_RULE_HIT")
    if "EVENT_RISK_HIGH" in event_info.get("signals", []):
        risk_alerts.append({"level": "WARNING", "message": "重要イベントが目前"})
    for v in rule_violations:
        risk_alerts.append({"level": "WARNING", "message": f"ルール違反：{v.get('reason')}", "type": "RULE_VIOLATION"})
    confirmed_n = sum(1 for s in thesis_stocks if s["thesis_result"] == "CONFIRMED")
    invalidated_n = sum(1 for s in thesis_stocks if s["thesis_result"] == "INVALIDATED")
    failed_n = sum(1 for s in thesis_stocks if s.get("status") == "FAILED")
    if morning_top5 and invalidated_n > confirmed_n:
        risk_alerts.append({"level": "WARNING", "message": "朝の仮説が実市場で崩れている銘柄が優勢"})
    if report_type != "OPENING_30M" and regime_changed_since_previous:
        risk_alerts.append({"level": "WATCH", "message": f"前回レポートから地合いが変化（{previous_regime}→{market_regime}）"})
        major_changes.append("REGIME_SHIFTED")
    if newly_strong_sectors:
        risk_alerts.append({"level": "WATCH", "message": f"新しい強いセクターを検出：{'・'.join(newly_strong_sectors)}"})
        major_changes.append("NEW_STRONG_SECTOR")
    if report_type != "OPENING_30M" and failed_n > 0:
        major_changes.append("THESIS_FAILED")
    # 15:30のみ：翌営業日イベントリスク・持ち越し注意（指示書3番）
    overnight_notes = []
    if report_type == "MARKET_CLOSE":
        if "NO_OVERNIGHT" in event_info.get("signals", []) or "EVENT_RISK_HIGH" in event_info.get("signals", []):
            overnight_notes.append("翌営業日に重要イベントがあるため持ち越しに注意")
        if any(a["tier"] in ("WARNING", "EXIT") for a in position_alerts):
            overnight_notes.append("損切りライン接近/到達中の保有銘柄あり。持ち越し判断は個別に再確認")

    # 指数の他のスカラーもnikkei_chgと同じフォールバック経路を通す（指示書6番）
    topix_chg = _fallback_scalar("topix_etf", "changePct", "topix_change_pct")
    growth250_chg = _fallback_scalar("growth250_etf", "changePct", "growth250_change_pct")
    nikkei_vi_val = _fallback_scalar("nikkei_vi_etn", "value", "nikkei_vi")
    usdjpy_val = _fallback_scalar("usdjpy", "value", "usdjpy")

    # ---- data_quality要約（指示書7番）。UI表示・ChatGPT共有どちらにも使う ----
    quality_summary = _quality_summary(source_statuses)
    data_health["quality_summary"] = quality_summary

    strategy, _ = generate_morning_strategy(market_regime, volatility_regime, risk["score"], event_info.get("signals", []))
    _report_label = {"OPENING_30M": "寄り30分", "MORNING_CLOSE": "前場終了", "AFTERNOON_30M": "後場30分", "MARKET_CLOSE": "大引け"}[report_type]
    if morning_top5:
        summary_text = (f"{_report_label}：朝の想定TOP5のうち{confirmed_n}/{len(thesis_stocks)}件が現時点で成立(CONFIRMED)。"
                         f"地合いは{market_regime}"
                         + ("（朝の想定から変化）" if regime_transition and regime_transition["from"] != market_regime else "")
                         + ("（直前レポートから地合い変化）" if regime_changed_since_previous else "")
                         + "。指数への逆張りは避け、地合い耐性銘柄を優先。")
    else:
        summary_text = f"{_report_label}：MorningMarketCheck未生成のため答え合わせ対象なし。地合いは{market_regime}。"
    if report_type == "MARKET_CLOSE" and morning_top5:
        success_n = sum(1 for s in thesis_stocks if s.get("final_result") == "SUCCESS")
        summary_text += f"　本日の朝TOP5最終結果：SUCCESS {success_n}/{len(thesis_stocks)}件。"
    if quality_summary["quality"] != "FULL":
        summary_text += f"　⚠ データ品質：{quality_summary['quality']}（一部データはキャッシュ/取得制限の影響を受けています）"

    payload = {
        "scheduled_time": INTRADAY_REPORT_SNAPSHOT_TIMES[report_type],
        "morning_check_id": morning_check.get("id") if morning_check else None,
        "market_regime": market_regime, "volatility_regime": volatility_regime, "market_summary": summary_text,
        "nikkei_change_pct": nikkei_chg,
        "topix_change_pct": topix_chg,
        "growth250_change_pct": growth250_chg,
        "nikkei_vi": nikkei_vi_val,
        "usdjpy": usdjpy_val,
        "sector_snapshot_json": {"strong": strong_sectors, "weak": weak_sectors,
                                  "newly_strong": newly_strong_sectors, "newly_weak": newly_weak_sectors},
        "strong_sectors_json": strong_sectors, "weak_sectors_json": weak_sectors,
        "top_stocks_json": focus["top5"], "resilience_stocks_json": focus["resilience"],
        # momentum_stocks_json：本格的な0-100 momentum_scoreはPhase2-C予定。Phase2-Bでは13:00の
        # 簡易資金流入分類（afternoon_flow）だけを載せ、15:30は13:00の分類をそのまま引き継ぐ。
        "momentum_stocks_json": (afternoon_flow if report_type == "AFTERNOON_30M"
                                  else ((previous_report.get("momentum_stocks_json") or [])
                                        if report_type == "MARKET_CLOSE" and previous_kind == "AFTERNOON_30M" else [])),
        "missed_opportunities_json": [],  # Phase2-Dで実装予定（指示書13番）
        "morning_thesis_evaluation_json": {
            "regime_transition": regime_transition,
            "previous_report_regime": previous_regime, "regime_changed_since_previous": regime_changed_since_previous,
            "stocks": thesis_stocks,
        },
        "risk_alerts_json": risk_alerts, "position_alerts_json": position_alerts,
        "news_changes_json": news_changes, "event_risk_json": event_info.get("events", [])[:5],
        "strategy_update_json": {"strategy": strategy, "text": summary_text, "major_changes": major_changes,
                                  "overnight_notes": overnight_notes, "previous_report_kind": previous_kind},
        "data_health_json": data_health,
        # 2026-09-10新規（にこそく@nicosokufx X投稿連携、指示書11番）：直近60分以内・HIGH/
        # CRITICALの投稿のみ参照する追加専用フィールド。既存のレポート生成・エントリー判定
        # ロジックには一切影響しない（例外は握りつぶし、失敗しても空配列のまま）。
        "social_signals_json": _nicosoku_intraday_signals_safe(database_url, user_id),
    }
    saved = investment_db.save_market_intelligence_report(database_url, user_id, trade_date, report_type, payload) if investment_db else None
    return saved


def _intraday_report_scheduler_users():
    """定時生成の対象ユーザー一覧（MorningMarketCheckと同じ考え方）。"""
    return _morning_check_scheduler_users()


def _intraday_report_scheduler_loop():
    """指示書6番の定時（09:30/11:30/13:00/15:30、Phase2-Bで全4本に拡張）に各report_typeを
    自動生成するデーモンスレッド。MorningMarketCheckと同じ発火済みセット方式＋DBのUNIQUE制約
    による二重防止。前の時間帯のレポートが未生成でも（例：11:30時点で09:30が無い）
    generate_intraday_report内部でMorningCheckまで遡るためエラーにはならない。"""
    fired = set()  # {(trade_date, report_type, user_id)}
    JST = datetime.timezone(datetime.timedelta(hours=9))
    while True:
        try:
            now_jst = datetime.datetime.now(JST)
            hhmm = now_jst.strftime("%H:%M")
            if _is_jp_market_business_day(now_jst):
                for report_type, target_hhmm in INTRADAY_REPORT_SNAPSHOT_TIMES.items():
                    if hhmm == target_hhmm:
                        trade_date = now_jst.date().isoformat()
                        for user_id in _intraday_report_scheduler_users():
                            key = (trade_date, report_type, user_id)
                            if key in fired:
                                continue
                            fired.add(key)
                            try:
                                generate_intraday_report(DATABASE_URL, user_id, report_type, trade_date)
                                print(f"  [IntradayReport] {user_id} {report_type}（{target_hhmm}）生成完了")
                            except Exception as e:
                                print(f"  [IntradayReport] {user_id} {report_type} 生成失敗", e)
                if len(fired) > 200:
                    fired = {k for k in fired if k[0] == now_jst.date().isoformat()}
        except Exception as e:
            print("  [IntradayReport] スケジューラループで例外", e)
        time.sleep(60)


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


# ============================================================
# v3-9（監視銘柄自動登録エンジン）：🐒 MOMENTUM DAY
# まずMOMENTUM DAYだけを完成させ、Stage1市場全体スキャナー・auto_tags・期限管理を共通基盤として
# 後続のAUTO_BREAK/AUTO_RS/AUTO_SECTOR_LEADER等に展開する方針（ユーザー指示）。
# ============================================================

# Stage1（市場全体スキャン）のキャッシュ有効期間。相場データはユーザー間で共通のため、
# プロセス内メモリにグローバルキャッシュ1本だけを持ち、全ユーザー・全タブで共有する
# （4000銘柄スキャンをアクセスのたびに繰り返さない）。初期値10分。実測のスキャン所要時間・
# API負荷を見て調整する前提（run_momentum_day_scanの戻り値で毎回実測値を報告する）。
# 2026-09-04（ユーザーフィードバック：立花証券APIのRemoteDisconnected対策）：MOMENTUM DAYと
# AUTO_BREAKの両方がこの同じrun_momentum_stage1()を呼ぶため、市場全体スキャンは常に1つの
# キャッシュ・1つのロックを共有する（今後AUTO_RS/AUTO_SECTOR_LEADER等を追加してもここを
# 再利用するだけでよく、各エンジンが個別に3900銘柄を取得する設計にはしない）。
MOMENTUM_STAGE1_TTL_SEC = 600
# force=Trueでの強制再スキャンでも、この秒数を下回る間隔では実際のAPI呼び出しをしない
# （スキャンボタン連打・複数エンジンの立て続けの呼び出しでAPIに連続大量リクエストを送らない
# ための下限、ユーザー指示：最低90〜120秒）。TTLより短いのでforceの意味は「TTL内でも、
# クールダウンさえ空いていれば早めに更新できる」ようにするため。
MOMENTUM_STAGE1_MIN_COOLDOWN_SEC = 100
MOMENTUM_STAGE1_FETCH_MAX_ATTEMPTS = 3  # RemoteDisconnected等のAPIエラー時の再試行上限（無限リトライ禁止）
_momentum_stage1_cache = {"builtAt": 0, "rows": {}, "durationSec": None, "requestCount": None,
                           "codesScanned": 0, "pricesReturned": 0, "nikkeiChangePct": None,
                           "scanFailed": False, "usedStaleCache": False}
_momentum_stage1_lock = threading.Lock()
_momentum_stage1_scan_in_progress = False  # ユーザー指示の「scan_in_progress」相当。UI表示・
                                            # ログ用途で明示的に持つ（実際の排他制御はロック本体が担う）。


def _fetch_market_price_with_retry(codes):
    """立花証券APIへの市場全体一括取得。RemoteDisconnected等の接続エラー時は、即座に大量
    リクエストを再送するのではなく段階的に待機してから再試行する（3秒→6秒、最大
    MOMENTUM_STAGE1_FETCH_MAX_ATTEMPTS回、無限リトライはしない）。全て失敗したらNoneを返し、
    呼び出し側で直近の正常キャッシュへのフォールバックを判断する。"""
    for attempt in range(1, MOMENTUM_STAGE1_FETCH_MAX_ATTEMPTS + 1):
        try:
            return tachibana_api.get_market_price(codes)
        except Exception as e:
            print(f"  市場全体スキャン失敗（{attempt}/{MOMENTUM_STAGE1_FETCH_MAX_ATTEMPTS}回目）", e)
            if attempt < MOMENTUM_STAGE1_FETCH_MAX_ATTEMPTS:
                time.sleep(attempt * 3)
    return None

MOMENTUM_LITE_THRESHOLD = 40    # Stage1のみでMOMENTUM_LITE_SCOREがこの点以上をStage2候補にする
MOMENTUM_LITE_MAX_CANDIDATES = 100  # Stage2（銘柄ごとに日足履歴を追加取得）に回す上限件数
# 2026-09-04実測：初期値70では、全面高の日に東証全体3899銘柄中84銘柄が登録される事態になった
# （「今日だけ何かがおかしいくらい強い銘柄」という趣旨に対して明らかに多すぎる）。ユーザー
# フィードバックにより、単純な閾値だけでなく「閾値以上の中から上位N件だけ」という相対順位方式
# を併用する設計に変更（下のrun_momentum_day_scan参照）。スコアが低い日に無理にN件埋めることは
# しない。
MOMENTUM_SCORE_THRESHOLD = 90    # Stage2まで終えたMOMENTUM_SCOREの最低ライン
MOMENTUM_FINAL_MAX_REGISTER = 15  # 閾値を満たした銘柄の中から、実際に自動登録するのは上位この件数まで
MOMENTUM_TAG_EXPIRE_HOURS = 18  # AUTO_MOMENTUM_DAYタグの有効期間の目安（当日限定。大引け後〜
                                 # 翌営業日の朝には失効させる想定で、厳密な「翌営業日9時」計算は
                                 # せず、当日中は確実に有効な時間で単純化する）
# 2026-09-04実測：Stage2を直列実行すると100件で約340秒かかった（1件あたりget_daily_history()の
# ネットワーク待ちが支配的）。ThreadPoolExecutorで並列化し、立花証券APIへの同時負荷を抑える
# ため上限を設ける（無制限並列は避ける）。
MOMENTUM_STAGE2_WORKERS = 10


def _jst_time_str(epoch_sec):
    """UNIX時刻(秒)をJSTのHH:MM文字列にする。UIの「直近スキャン：15:21」表示用。"""
    if not epoch_sec:
        return None
    jst = datetime.timezone(datetime.timedelta(hours=9))
    return datetime.datetime.fromtimestamp(epoch_sec, jst).strftime("%H:%M")


def _stage1_needs_refresh(force):
    """キャッシュ更新が必要かどうかの判定。force=Trueでも、直近スキャンから
    MOMENTUM_STAGE1_MIN_COOLDOWN_SEC秒未満なら更新不要（クールダウン優先）とすることで、
    スキャンボタン連打や複数エンジンの立て続けの呼び出しで市場全体スキャンを繰り返さない。"""
    has_cache = bool(_momentum_stage1_cache["rows"])
    if not has_cache:
        return True
    age = time.time() - _momentum_stage1_cache["builtAt"]
    cooldown_elapsed = age >= MOMENTUM_STAGE1_MIN_COOLDOWN_SEC
    if force:
        return cooldown_elapsed
    return age >= MOMENTUM_STAGE1_TTL_SEC


def run_momentum_stage1(force=False):
    """東証全銘柄（get_jp_issue_master、約4000件）を対象に、立花証券APIのget_market_price()
    （内部でPRICE_CHUNK＝40件ずつに自動分割される、既存の仕組みをそのまま利用）で当日値を
    一括取得し、Stage1（MOMENTUM LITE）で使う指標だけを算出する：
      changePct（当日騰落率）／turnover（売買代金、price×volume）／highRetention（高値維持率、
      current/dayHigh）／marketRS（対市場＝changePct−日経平均changePct）／
      sectorRS（対セクター＝changePct−セクター平均changePct、セクターはget_jp_issue_masterの分類）
    出来高倍率・ブレイク判定など「銘柄ごとに追加の日足履歴取得が要る」指標はここでは扱わない
    （Stage2の責務。4000銘柄全部に日足取得をかけると非現実的な負荷になるため）。
    結果はMOMENTUM_STAGE1_TTL_SEC秒プロセス内キャッシュし、force=Trueでも
    MOMENTUM_STAGE1_MIN_COOLDOWN_SEC秒未満の間隔では実際のAPI取得をしない（クールダウン）。
    同時アクセスによる二重スキャンは_momentum_stage1_lockで防ぐ（ロック取得を待っていた
    呼び出しは、ロック解放後にキャッシュが新しくなっていればそれをそのまま使う＝
    二重チェックロッキング。MOMENTUM DAY・AUTO_BREAK・今後のAUTO_RS等、複数エンジンが
    同時にこの関数を呼んでも、実際の3900銘柄取得は1回で済む）。
    API取得が全て失敗した場合（RemoteDisconnected等）は直近の正常キャッシュがあればそれを
    scanFailed=Trueと共に返す（呼び出し側はUIに「市場スキャン更新失敗・直近データ使用」を
    表示できる）。"""
    global _momentum_stage1_cache, _momentum_stage1_scan_in_progress
    if not _stage1_needs_refresh(force):
        return _momentum_stage1_cache
    with _momentum_stage1_lock:
        if not _stage1_needs_refresh(force):
            return _momentum_stage1_cache
        _momentum_stage1_scan_in_progress = True
        t0 = time.time()
        master = get_jp_issue_master()
        codes = list(master.keys())
        if tachibana_api is None or not codes:
            _momentum_stage1_scan_in_progress = False
            _momentum_stage1_cache = {"builtAt": time.time(), "rows": {}, "durationSec": 0,
                                       "requestCount": 0, "codesScanned": 0, "pricesReturned": 0,
                                       "nikkeiChangePct": None, "scanFailed": True, "usedStaleCache": False}
            return _momentum_stage1_cache
        prices = _fetch_market_price_with_retry(codes)
        if prices is None:
            # 全リトライ失敗：直近の正常キャッシュがあればそれを使い続ける（無限リトライしない）。
            _momentum_stage1_scan_in_progress = False
            if _momentum_stage1_cache["rows"]:
                _momentum_stage1_cache = {**_momentum_stage1_cache, "scanFailed": True, "usedStaleCache": True}
            else:
                _momentum_stage1_cache = {"builtAt": time.time(), "rows": {}, "durationSec": round(time.time() - t0, 1),
                                           "requestCount": 0, "codesScanned": len(codes), "pricesReturned": 0,
                                           "nikkeiChangePct": None, "scanFailed": True, "usedStaleCache": False}
            return _momentum_stage1_cache
        request_count = -(-len(codes) // tachibana_api.PRICE_CHUNK)  # 切り上げ除算

        market_env = _market_environment()
        nikkei_chg = market_env.get("nikkeiChangePct")

        # セクター別の当日平均騰落率（対セクターの基準）。get_jp_issue_masterのsector分類を使う。
        sector_sum, sector_count = {}, {}
        for code, p in prices.items():
            chg = p.get("changePct")
            sector = master.get(code, {}).get("sector")
            if chg is not None and sector:
                sector_sum[sector] = sector_sum.get(sector, 0.0) + chg
                sector_count[sector] = sector_count.get(sector, 0) + 1
        sector_avg = {s: sector_sum[s] / sector_count[s] for s in sector_sum}

        rows = {}
        for code, p in prices.items():
            chg = p.get("changePct")
            t, high, volume = p.get("t"), p.get("high"), p.get("volume")
            sector = master.get(code, {}).get("sector")
            turnover = (t * volume) if (t is not None and volume is not None) else None
            high_retention = (t / high) if (t is not None and high) else None
            market_rs = (chg - nikkei_chg) if (chg is not None and nikkei_chg is not None) else None
            sector_rs = (chg - sector_avg[sector]) if (chg is not None and sector in sector_avg) else None
            rows[code] = {
                "code": code, "name": master.get(code, {}).get("name"), "sector": sector,
                "changePct": chg, "current": t, "high": high, "low": p.get("low"),
                "open": p.get("open"), "volume": volume, "turnover": turnover,
                "highRetention": high_retention, "marketRS": market_rs, "sectorRS": sector_rs,
                "ask": p.get("ask"), "bid": p.get("bid"),
            }
        _momentum_stage1_cache = {
            "builtAt": time.time(), "rows": rows, "durationSec": round(time.time() - t0, 1),
            "requestCount": request_count, "codesScanned": len(codes), "pricesReturned": len(prices),
            "nikkeiChangePct": nikkei_chg, "scanFailed": False, "usedStaleCache": False,
        }
        _momentum_stage1_scan_in_progress = False
    return _momentum_stage1_cache


def _scale_score(v, lo, hi, points):
    """v が lo 以下なら0点、hi 以上なら満点(points)、間は線形補間。"""
    if v is None:
        return 0.0
    if v <= lo:
        return 0.0
    if v >= hi:
        return points
    return (v - lo) / (hi - lo) * points


def _liquidity_multiplier(turnover):
    """デイトレ対象として売買できる厚みがあるかの掛け目（0.2〜1.0）。ユーザー指示：
    売買代金10億円未満は原則低評価、30億円以上で加点、100億円以上で高評価。単純な加点ではなく
    スコア全体に掛ける乗数にすることで、他の指標がどれだけ良くても薄商いの銘柄が上位に来ないよう
    強く抑制する（実データ分布を見て調整する前提の暫定しきい値）。"""
    if turnover is None:
        return 0.5  # 不明時は過度な優遇も冷遇もしない中間値
    if turnover >= 1e10:   # 100億円以上
        return 1.0
    if turnover >= 3e9:    # 30億円以上
        return 0.85
    if turnover >= 1e9:    # 10億円以上
        return 0.55
    return 0.2              # 10億円未満＝原則低評価


def _momentum_lite_score(row):
    """MOMENTUM_LITE_SCORE：Stage1だけで市場全体約3900銘柄について算出できる指標のみ使う
    （出来高倍率は使わない＝Stage2の責務）。2026-09-04ユーザーフィードバックにより、単に
    「+8〜10%上がっただけ」で高得点になり過ぎないよう当日騰落率の配点比重を下げ、売買代金・
    高値維持率を重視する配点に変更。さらに_liquidity_multiplier()を掛けて薄商い銘柄を
    全体的に抑制する（優先順位：1.売買代金 2.高値維持率 3.当日騰落率 4.対市場 5.対セクター）。
      売買代金        最大30点（10億円未満で0点、100億円以上で満点）
      高値維持率      最大25点（92%未満で0点、99.5%以上で満点。「高値からどれだけ離れていないか」を重視）
      当日騰落率      最大20点（0%で0点、+10%以上で満点。以前は+15%で満点・配点40だったのを
                      大幅に抑え、「上がっただけ」の銘柄が単独で上位に来にくくする）
      対市場          最大15点（0pt以下で0点、+5pt以上で満点）
      対セクター      最大10点（0pt以下で0点、+3pt以上で満点）"""
    score = 0.0
    score += _scale_score(row.get("turnover"), 1e9, 1e10, 30)
    score += _scale_score(row.get("highRetention"), 0.92, 0.995, 25)
    score += _scale_score(row.get("changePct"), 0, 10, 20)
    score += _scale_score(row.get("marketRS"), 0, 5, 15)
    score += _scale_score(row.get("sectorRS"), 0, 3, 10)
    score *= _liquidity_multiplier(row.get("turnover"))
    return round(score, 1)


def select_momentum_stage1_candidates(stage1):
    """Stage1結果からMOMENTUM_LITE_SCORE降順で並べ、閾値以上・上位MOMENTUM_LITE_MAX_CANDIDATES件
    だけをStage2に回す候補として返す（[(liteScore, code, row), ...]）。"""
    scored = []
    for code, row in stage1["rows"].items():
        s = _momentum_lite_score(row)
        if s >= MOMENTUM_LITE_THRESHOLD:
            scored.append((s, code, row))
    scored.sort(key=lambda x: -x[0])
    return scored[:MOMENTUM_LITE_MAX_CANDIDATES]


def _momentum_stage2_detail(code, stage1_row):
    """Stage1通過銘柄だけに、日足履歴が要る指標（出来高倍率・3か月ブレイク・年初来高値更新）を
    追加する。1銘柄につき_tachibana_daily_arrays()（get_daily_history）を1回だけ呼ぶ
    （既存のBREAKOUT_LOOKBACK_DAYS・_tachibana_daily_arraysをそのまま再利用、新規API種別の
    追加なし）。取得失敗・データ不足時はNoneを返し、呼び出し側はStage1情報だけで暫定スコアを使う。"""
    arrays = _tachibana_daily_arrays(code)
    if not arrays:
        return None
    closes, opens, highs, lows, volumes = arrays
    if len(volumes) < 6:
        return None
    current = stage1_row.get("current") if stage1_row.get("current") is not None else closes[-1]
    vol_avg5 = sum(volumes[-6:-1]) / 5
    vol_ratio = (volumes[-1] / vol_avg5) if vol_avg5 else None
    breakout_lookback_high = (max(highs[-(BREAKOUT_LOOKBACK_DAYS + 1):-1])
                               if len(highs) >= BREAKOUT_LOOKBACK_DAYS + 1 else None)
    active_break = bool(breakout_lookback_high is not None and current > breakout_lookback_high)
    high52w = max(highs[-min(252, len(highs)):]) if highs else current
    new_year_high = bool(current >= high52w * 0.999)
    return {"volRatio": round(vol_ratio, 2) if vol_ratio is not None else None,
            "activeBreak": active_break, "newYearHigh": new_year_high,
            "breakoutLookbackHigh": round(breakout_lookback_high, 1) if breakout_lookback_high is not None else None,
            "high52w": round(high52w, 1)}


def _momentum_final_score(lite_score, stage2):
    """MOMENTUM_SCORE最終値：Stage1のMOMENTUM_LITE_SCORE（既に売買代金の掛け目を反映済み）を
    土台に、Stage2で分かった出来高倍率・ACTIVE_BREAK・年初来高値更新を加点する。
      出来高倍率：1倍あたり4点（5倍で頭打ち＝最大20点。優先順位4番目として厚めに配点）
      ACTIVE_BREAK（3か月高値ブレイク）：+8点
      年初来高値更新：+4点
    2026-09-04ユーザーフィードバックにより、100点で丸め込む（クランプする）のをやめた。
    以前は「+12%だが高値から8%崩れている」銘柄と「+6%だが高値維持率がほぼ100%・出来高5倍」
    銘柄が両方100点に張り付いて区別できない事態が起きていたため、上位ほど差が付くよう
    そのままのスコアを返す（自動登録の閾値判定・上位N件の相対順位付けの両方に使うだけで、
    ちょうど0〜100に収まる保証は元々していない）。
    Stage2データが取得できなかった銘柄はStage1スコアをそのまま最終スコアとする
    （＝日足履歴が取れない銘柄を一律減点はしない。ただし加点も無いため相対的に順位は下がる）。"""
    if not stage2:
        return lite_score
    score = lite_score
    if stage2.get("volRatio") is not None:
        score += min(stage2["volRatio"], 5) * 4
    if stage2.get("activeBreak"):
        score += 8
    if stage2.get("newYearHigh"):
        score += 4
    return round(score, 1)


def _jst_today_date_str():
    jst = datetime.timezone(datetime.timedelta(hours=9))
    return datetime.datetime.now(jst).date().isoformat()


# v3-9続き（2026-09-05・PHASE 1 AUTO SIGNAL LOG）：5つの自動登録エンジン共通のシグナル履歴記録。
# watchlist.auto_tags（現在状態、CURRENT/SEEN・期限切れで消える）とは役割を分離し、
# auto_signal_eventsには状態遷移（ENTER_CURRENT/EXIT_CURRENT/REENTER_CURRENT）が起きた時だけ
# 1行追加する（同じCURRENT銘柄を毎スキャンINSERTしない＝重複防止の要）。各run_*_scanの
# CURRENT/SEEN更新ブロックの直後から1回呼ぶだけで済む共通ヘルパー。既存のauto_tags/CURRENT/SEEN/
# manual_registered保護/ポジション保護/Stage1共有キャッシュには一切手を加えない（追加のみ）。
def _record_signal_transitions(database_url, user_id, signal_type, market,
                                to_register, old_current, old_seen, demoted, metadata_fn):
    """to_register: このスキャンで登録対象になった詳細dictのリスト（code/finalScore/changePct/
    marketRS/sectorRS/turnover/current・highRetentionは任意のキーを持つ想定）。
    old_current/old_seen: このスキャン開始前のCURRENT/SEENコード集合。
    demoted: このスキャンでCURRENT→SEENへ降格したコードのリスト。
    metadata_fn(d): エンジン固有のmetadata dictを組み立てるcallable。"""
    if not (database_url and investment_db is not None):
        return
    event_date = _jst_today_date_str()
    for d in to_register:
        code = d["code"]
        if code in old_current:
            continue  # 既にCURRENT中＝状態遷移なし、記録しない（重複防止）
        event_type = "REENTER_CURRENT" if code in old_seen else "ENTER_CURRENT"
        try:
            investment_db.log_auto_signal_event(
                database_url, user_id, code, market, signal_type, event_type, event_date,
                score=d.get("finalScore"), current_price=d.get("current"),
                day_change_pct=d.get("changePct"), market_rs=d.get("marketRS"),
                sector_rs=d.get("sectorRS"), turnover=d.get("turnover"),
                high_retention=d.get("highRetention"), metadata=metadata_fn(d))
        except Exception as e:
            print(f"  {signal_type} signal_event記録失敗（{event_type}）", code, e)
    for code in demoted:
        try:
            investment_db.log_auto_signal_event(
                database_url, user_id, code, market, signal_type, "EXIT_CURRENT", event_date, metadata={})
        except Exception as e:
            print(f"  {signal_type} signal_event記録失敗（EXIT_CURRENT）", code, e)


def run_momentum_day_scan(database_url, user_id, force=False):
    """Stage1（市場全体スキャン）→Stage1候補選定→Stage2（絞り込み後の詳細）→MOMENTUM_SCORE
    確定→閾値を満たした上位MOMENTUM_FINAL_MAX_REGISTER件を「AUTO_MOMENTUM_DAY_CURRENT」として
    監視銘柄へ登録、旧CURRENTで新TOP15から外れた銘柄は「AUTO_MOMENTUM_DAY_SEEN」へ降格、
    までの一連の処理。同日に何度スキャンしてもCURRENTは常に最新TOP15だけを指し、無限に累積
    しない（SEENは当日の履歴として残るが、監視銘柄タブの主フィルターには出さない設計、
    フロント側で対応）。戻り値はUI表示・実データでの閾値調整レポートの両方に使うサマリー。
    S高張り付き（値幅制限の上限に貼り付いて実質売買できない状態）は専用の気配情報APIが無いため、
    「現在値が当日高値とほぼ一致し、かつ売気配(ask)が立っていない（＝買い一色で売り注文が
    尽きている）」という間接的な近似で判定する（stuckLimitUp、あくまで簡易推定）。"""
    stage1 = run_momentum_stage1(force=force)
    candidates = select_momentum_stage1_candidates(stage1)
    t_stage2_start = time.time()

    # Stage2はcandidates 1件につきget_daily_history()1回＝ネットワークI/O待ちが支配的なため、
    # ThreadPoolExecutorで並列化する（初回実測：直列だと100件で約340秒。データの取得内容自体は
    # 変えず、待ち時間だけ重ねる）。MOMENTUM_STAGE2_WORKERSは立花証券APIへの同時負荷を抑える
    # ための上限（無制限に並列化しない）。
    stage2_results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=MOMENTUM_STAGE2_WORKERS) as ex:
        futures = {ex.submit(_momentum_stage2_detail, code, row): code for _, code, row in candidates}
        for fut in concurrent.futures.as_completed(futures):
            code = futures[fut]
            try:
                stage2_results[code] = fut.result()
            except Exception as e:
                print("  Stage2詳細取得失敗", code, e)
                stage2_results[code] = None
    stage2_duration = round(time.time() - t_stage2_start, 1)

    # v3-9改訂（2026-09-04ユーザーフィードバック）：単純な閾値判定だけでなく、閾値を満たした
    # 銘柄の中から上位MOMENTUM_FINAL_MAX_REGISTER件だけを実際に登録する「相対順位方式」を併用する。
    # スコアが低い日に無理に件数を埋めることはしない（該当が3件しかなければ3件だけ登録する）。
    details = []
    for lite_score, code, row in candidates:
        stage2 = stage2_results.get(code)
        final_score = _momentum_final_score(lite_score, stage2)
        stuck_limit_up = bool(row.get("current") is not None and row.get("high")
                               and row["current"] >= row["high"] * 0.999 and not row.get("ask"))
        details.append({"code": code, "name": row.get("name"), "sector": row.get("sector"),
                         "changePct": row.get("changePct"), "highRetention": row.get("highRetention"),
                         "marketRS": row.get("marketRS"), "sectorRS": row.get("sectorRS"),
                         "turnover": row.get("turnover"), "volRatio": (stage2 or {}).get("volRatio"),
                         "current": row.get("current"),
                         "liteScore": lite_score, "finalScore": final_score, "stage2": stage2,
                         "stuckLimitUp": stuck_limit_up, "row": row})
    details.sort(key=lambda d: -d["finalScore"])
    to_register = [d for d in details if d["finalScore"] >= MOMENTUM_SCORE_THRESHOLD][:MOMENTUM_FINAL_MAX_REGISTER]

    # v3-9再改訂（2026-09-04ユーザーフィードバック：同日複数回スキャン時の累積を防ぐ）：
    # 「最新TOP15（AUTO_MOMENTUM_DAY_CURRENT）」と「本日中に一度でもTOP15入りしたが現在は
    # 外れている履歴（AUTO_MOMENTUM_DAY_SEEN）」を分離する。スキャンのたびに、
    #   ①新しいTOP15に入った銘柄 → CURRENTタグを付与（SEENタグが付いていれば剥がす＝復帰）
    #   ②旧CURRENTだったが新しいTOP15から外れた銘柄 → CURRENTタグを剥がし、SEENタグに切り替える
    # これにより監視銘柄タブの「🐒 MOMENTUM DAY」フィルターは常に最新TOP15だけを指し、
    # 過去に強かったが今は外れた銘柄は「🐒 本日履歴」側でのみ確認できる。
    registered, demoted = [], []
    if database_url and investment_db is not None:
        old_current = investment_db.get_codes_with_auto_tag(database_url, user_id, "AUTO_MOMENTUM_DAY_CURRENT", market="JP")
        old_seen = investment_db.get_codes_with_auto_tag(database_url, user_id, "AUTO_MOMENTUM_DAY_SEEN", market="JP")
        new_current_codes = {d["code"] for d in to_register}
        now_dt = datetime.datetime.now(datetime.timezone.utc)
        expires_at = (now_dt + datetime.timedelta(hours=MOMENTUM_TAG_EXPIRE_HOURS)).isoformat()

        for d in to_register:
            tag_value = {"score": d["finalScore"], "addedAt": now_dt.isoformat(),
                         "expiresAt": expires_at, "stuckLimitUp": d["stuckLimitUp"]}
            try:
                ok = investment_db.auto_register_or_tag_watchlist_item(
                    database_url, user_id, d["code"], "JP", "AUTO_MOMENTUM_DAY_CURRENT", tag_value,
                    item_fields={"name": d["name"], "sector": d["sector"], "source": "auto_momentum_day"})
                investment_db.remove_auto_tag_key(database_url, user_id, d["code"], "JP", "AUTO_MOMENTUM_DAY_SEEN")
            except Exception as e:
                print("  MOMENTUM DAY自動登録（CURRENT）失敗", d["code"], e)
                ok = False
            if ok:
                registered.append(d)

        for code in old_current - new_current_codes:
            try:
                investment_db.auto_register_or_tag_watchlist_item(
                    database_url, user_id, code, "JP", "AUTO_MOMENTUM_DAY_SEEN",
                    {"addedAt": now_dt.isoformat(), "expiresAt": expires_at})
                investment_db.remove_auto_tag_key(database_url, user_id, code, "JP", "AUTO_MOMENTUM_DAY_CURRENT")
                demoted.append(code)
            except Exception as e:
                print("  MOMENTUM DAY降格（SEEN化）失敗", code, e)

        _record_signal_transitions(database_url, user_id, "MOMENTUM_DAY", "JP",
            to_register, old_current, old_seen, demoted,
            metadata_fn=lambda d: {"stuckLimitUp": d.get("stuckLimitUp"), "volRatio": d.get("volRatio")})
    for d in details:
        d.pop("row", None)  # rowは選定計算専用の内部情報。レスポンスには含めない
    return {
        "stage1CodesScanned": stage1["codesScanned"], "stage1PricesReturned": stage1["pricesReturned"],
        "stage1DurationSec": stage1["durationSec"], "stage1RequestCount": stage1["requestCount"],
        "stage1CacheAgeSec": round(time.time() - stage1["builtAt"], 1) if stage1["builtAt"] else None,
        "stage1BuiltAtJst": _jst_time_str(stage1["builtAt"]),
        "stage1ScanFailed": stage1.get("scanFailed", False), "stage1UsedStaleCache": stage1.get("usedStaleCache", False),
        "stage1CandidateCount": len(candidates),
        "demotedToSeenCount": len(demoted), "demotedToSeen": demoted,
        "stage2EvaluatedCount": len(details),
        "stage2DurationSec": stage2_duration,
        "registeredCount": len(registered),
        "registered": registered,
        "allCandidates": details,  # 閾値調整の判断材料として、上位20件だけでなく全件返す
    }


# ============================================================
# v3-9続き：🚀 AUTO_BREAK
# MOMENTUM DAYで作ったStage1市場全体スキャナー（run_momentum_stage1、同じキャッシュをそのまま
# 共有）・auto_tags・CURRENT/SEEN構造を再利用して展開する（ユーザー指示）。
# 【設計原則】Primary/Action Status（ACTIVE_BREAK/WEAK_BREAK/FAILED_BREAK等）のSingle Source
# of Truthは引き続きフロントのenrichWatchRow()。この自動登録エンジンは「監視に値するブレイク
# 銘柄を見つけて登録する」だけの役割で、breakType/breakQualityの正式な判定はenrichWatchRow()に
# 委ねる。ここで使うbreak_tier（年初来高値／3か月高値／前日高値のどれを満たすか）は、
# 市場全体約3900銘柄をスキャンするための軽量な一次判定であり、analyze_stock()の
# breakout_confirmed/breakout_lookback_highと同じ考え方を流用しただけの近似（二重の「正式」
# 判定を作らない、あくまで自動登録の入口用）。
# ============================================================
BREAK_LITE_THRESHOLD = 30
BREAK_LITE_MAX_CANDIDATES = 100
# 2026-09-04実測：初期値65では東証3899銘柄中1件しか登録されなかった（目標5〜20件を下回る）。
# 実データの分布（登録候補のfinalScoreが上位でも45〜73点程度に収まっていた）を見て52へ変更
# （ユーザー指示：まず52で数営業日運用し、50までは下げない。配点自体は変更しない）。
BREAK_SCORE_THRESHOLD = 52
BREAK_FINAL_MAX_REGISTER = 20     # 1回のスキャンで登録する上限（無理に埋めない）
BREAK_TAG_EXPIRE_DAYS = 3         # AUTO_BREAKはMOMENTUM DAYと違い数日有効（短期スイング候補）
BREAK_STAGE2_WORKERS = 10


def _break_lite_score(row):
    """BREAK候補の一次選定（Stage1のみ、履歴取得なし）。売買代金・高値維持率を重視し、
    出来高倍率・ブレイク種別はStage2で確定する。"""
    score = 0.0
    score += _scale_score(row.get("turnover"), 1e9, 1e10, 30)
    score += _scale_score(row.get("highRetention"), 0.92, 0.995, 30)
    score += _scale_score(row.get("marketRS"), 0, 5, 20)
    score += _scale_score(row.get("sectorRS"), 0, 3, 10)
    score += _scale_score(row.get("changePct"), 0, 8, 10)
    score *= _liquidity_multiplier(row.get("turnover"))
    return round(score, 1)


def select_break_stage1_candidates(stage1):
    scored = []
    for code, row in stage1["rows"].items():
        s = _break_lite_score(row)
        if s >= BREAK_LITE_THRESHOLD:
            scored.append((s, code, row))
    scored.sort(key=lambda x: -x[0])
    return scored[:BREAK_LITE_MAX_CANDIDATES]


def _break_stage2_detail(code, stage1_row):
    """Stage1通過銘柄だけに、日足履歴が要るブレイク種別判定を追加する。
    年初来高値＞3か月高値（＝直近レジスタンス突破）＞前日高値突破、の順で重要度が高い
    （同時に複数満たす場合は最上位のtierだけを採用、加算しない）。
    breakout_lookback_high・high52wはanalyze_stock()と同じ計算（BREAKOUT_LOOKBACK_DAYS=63・
    直近252営業日）をそのまま流用し、判定基準を二重に定義しない。"""
    arrays = _tachibana_daily_arrays(code)
    if not arrays:
        return None
    closes, opens, highs, lows, volumes = arrays
    if len(volumes) < 6 or len(highs) < 2:
        return None
    current = stage1_row.get("current") if stage1_row.get("current") is not None else closes[-1]
    vol_avg5 = sum(volumes[-6:-1]) / 5
    vol_ratio = (volumes[-1] / vol_avg5) if vol_avg5 else None
    breakout_lookback_high = (max(highs[-(BREAKOUT_LOOKBACK_DAYS + 1):-1])
                               if len(highs) >= BREAKOUT_LOOKBACK_DAYS + 1 else None)
    high52w = max(highs[-min(252, len(highs)):]) if highs else current
    prev_day_high = highs[-2]
    year_high = bool(current >= high52w * 0.999)
    three_month_high = bool(breakout_lookback_high is not None and current > breakout_lookback_high)
    prev_day_break = bool(current > prev_day_high)
    # analyze_stock()のbreakout_confirmedと同じ基準（3か月高値を出来高1.5倍以上で上抜け）。
    active_break_confirmed = bool(three_month_high and vol_ratio is not None and vol_ratio >= 1.5)
    if year_high:
        break_tier, break_tier_points = "YEAR_HIGH", 30
    elif three_month_high:
        break_tier, break_tier_points = "3M_HIGH", 25
    elif prev_day_break:
        break_tier, break_tier_points = "PREV_DAY_HIGH", 15
    else:
        break_tier, break_tier_points = None, 0
    return {"volRatio": round(vol_ratio, 2) if vol_ratio is not None else None,
            "breakoutLookbackHigh": round(breakout_lookback_high, 1) if breakout_lookback_high is not None else None,
            "high52w": round(high52w, 1), "prevDayHigh": round(prev_day_high, 1),
            "yearHigh": year_high, "threeMonthHigh": three_month_high, "prevDayHighBreak": prev_day_break,
            "activeBreakConfirmed": active_break_confirmed,
            "breakTier": break_tier, "breakTierPoints": break_tier_points}


def _break_final_score(row, stage2):
    """BREAK_SCORE：ブレイク条件を一つも満たさない銘柄・高値から5%以上崩れている銘柄は
    対象外（None）とし、そもそも候補にしない（ユーザー指示：FAILED_BREAK相当は新規登録しない）。
      ブレイク重要度：年初来高値30点 ＞ 3か月高値（レジスタンス突破）25点 ＞ 前日高値突破15点
      出来高倍率：最大20点（1倍あたり4点、5倍で頭打ち）
      高値維持率：最大20点（95%未満0点＝対象外基準と同じ境界、99.9%以上で満点）
      対市場：最大10点／対セクター：最大5点
      最後に売買代金の掛け目（_liquidity_multiplier、MOMENTUM DAYと共通）を掛け、
      「年初来高値でも流動性が低い銘柄は過大評価しない」を担保する。"""
    if not stage2 or not stage2["breakTier"]:
        return None
    hr = row.get("highRetention")
    if hr is not None and hr < 0.95:
        return None
    score = stage2["breakTierPoints"]
    if stage2.get("volRatio") is not None:
        score += min(stage2["volRatio"], 5) * 4
    score += _scale_score(hr, 0.95, 0.999, 20)
    score += _scale_score(row.get("marketRS"), 0, 5, 10)
    score += _scale_score(row.get("sectorRS"), 0, 3, 5)
    score *= _liquidity_multiplier(row.get("turnover"))
    return round(score, 1)


def run_break_scan(database_url, user_id, force=False):
    """MOMENTUM DAYと同じ2段階構成＋CURRENT/SEEN構造。Stage1（run_momentum_stage1、キャッシュ
    共有）→BREAK候補選定→Stage2（ブレイク種別確定）→BREAK_SCORE確定→閾値を満たした上位
    BREAK_FINAL_MAX_REGISTER件を「AUTO_BREAK_CURRENT」として登録、新TOP外に落ちた旧CURRENTは
    「AUTO_BREAK_SEEN」へ降格。FAILED_BREAKへの転落（＝Primary Statusの変化）はフロント側の
    enrichWatchRow()が検知して表示するため、ここでは扱わない（Primary/Action Statusの
    Single Source of Truthを保つ）。"""
    stage1 = run_momentum_stage1(force=force)
    candidates = select_break_stage1_candidates(stage1)
    t_stage2_start = time.time()

    stage2_results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=BREAK_STAGE2_WORKERS) as ex:
        futures = {ex.submit(_break_stage2_detail, code, row): code for _, code, row in candidates}
        for fut in concurrent.futures.as_completed(futures):
            code = futures[fut]
            try:
                stage2_results[code] = fut.result()
            except Exception as e:
                print("  BREAK Stage2詳細取得失敗", code, e)
                stage2_results[code] = None
    stage2_duration = round(time.time() - t_stage2_start, 1)

    details = []
    for lite_score, code, row in candidates:
        stage2 = stage2_results.get(code)
        final_score = _break_final_score(row, stage2)
        if final_score is None:
            continue  # ブレイク条件なし、または高値から5%以上崩れている＝候補にしない
        details.append({"code": code, "name": row.get("name"), "sector": row.get("sector"),
                         "changePct": row.get("changePct"), "highRetention": row.get("highRetention"),
                         "marketRS": row.get("marketRS"), "sectorRS": row.get("sectorRS"),
                         "turnover": row.get("turnover"), "liteScore": lite_score,
                         "current": row.get("current"),
                         "finalScore": final_score, "stage2": stage2})
    details.sort(key=lambda d: -d["finalScore"])
    to_register = [d for d in details if d["finalScore"] >= BREAK_SCORE_THRESHOLD][:BREAK_FINAL_MAX_REGISTER]

    registered, demoted = [], []
    if database_url and investment_db is not None:
        old_current = investment_db.get_codes_with_auto_tag(database_url, user_id, "AUTO_BREAK_CURRENT", market="JP")
        old_seen = investment_db.get_codes_with_auto_tag(database_url, user_id, "AUTO_BREAK_SEEN", market="JP")
        new_current_codes = {d["code"] for d in to_register}
        now_dt = datetime.datetime.now(datetime.timezone.utc)
        expires_at = (now_dt + datetime.timedelta(days=BREAK_TAG_EXPIRE_DAYS)).isoformat()

        for d in to_register:
            tag_value = {"score": d["finalScore"], "breakTier": d["stage2"]["breakTier"],
                         "addedAt": now_dt.isoformat(), "expiresAt": expires_at}
            try:
                ok = investment_db.auto_register_or_tag_watchlist_item(
                    database_url, user_id, d["code"], "JP", "AUTO_BREAK_CURRENT", tag_value,
                    item_fields={"name": d["name"], "sector": d["sector"], "source": "auto_break"})
                investment_db.remove_auto_tag_key(database_url, user_id, d["code"], "JP", "AUTO_BREAK_SEEN")
            except Exception as e:
                print("  AUTO_BREAK登録（CURRENT）失敗", d["code"], e)
                ok = False
            if ok:
                registered.append(d)

        for code in old_current - new_current_codes:
            try:
                investment_db.auto_register_or_tag_watchlist_item(
                    database_url, user_id, code, "JP", "AUTO_BREAK_SEEN",
                    {"addedAt": now_dt.isoformat(), "expiresAt": expires_at})
                investment_db.remove_auto_tag_key(database_url, user_id, code, "JP", "AUTO_BREAK_CURRENT")
                demoted.append(code)
            except Exception as e:
                print("  AUTO_BREAK降格（SEEN化）失敗", code, e)

        _record_signal_transitions(database_url, user_id, "BREAK", "JP",
            to_register, old_current, old_seen, demoted,
            metadata_fn=lambda d: {"breakTier": d["stage2"]["breakTier"], "volumeRatio": d["stage2"].get("volRatio")})
    return {
        "stage1CodesScanned": stage1["codesScanned"], "stage1PricesReturned": stage1["pricesReturned"],
        "stage1DurationSec": stage1["durationSec"], "stage1RequestCount": stage1["requestCount"],
        "stage1CacheAgeSec": round(time.time() - stage1["builtAt"], 1) if stage1["builtAt"] else None,
        "stage1BuiltAtJst": _jst_time_str(stage1["builtAt"]),
        "stage1ScanFailed": stage1.get("scanFailed", False), "stage1UsedStaleCache": stage1.get("usedStaleCache", False),
        "stage1CandidateCount": len(candidates),
        "stage2ValidCount": len(details),  # ブレイク条件を満たし候補となった件数（対象外は除外済み）
        "stage2DurationSec": stage2_duration,
        "demotedToSeenCount": len(demoted), "demotedToSeen": demoted,
        "registeredCount": len(registered),
        "registered": registered,
        "allCandidates": details,
    }


# ============================================================
# v3-9続き：📈 AUTO_RS
# MOMENTUM DAY・AUTO_BREAKと同じStage1（run_momentum_stage1、キャッシュ共有）・auto_tags・
# CURRENT/SEEN構造を再利用する。AUTO_RSは対市場／対セクターというStage1だけで既に算出済みの
# 指標が主軸のため、【Stage2（銘柄ごとの日足履歴取得）を持たない】設計にした＝3エンジンの中で
# 最も軽量（追加のAPI呼び出しがゼロ、Stage1スキャンの結果だけで完結する）。
# 【設計原則】ここでも「総合RS>=75」等はenrichWatchRow()の正式なRS Scoreとは別物（市場全体を
# スキャンするための近似で、対市場・対セクターの生の値を直接使う）。登録後の実際のPrimary/
# Action StatusはenrichWatchRow()がSSoTのまま。
# ============================================================
AUTO_RS_SCORE_THRESHOLD = 55  # 実データを見て調整する前提の初期値（MOMENTUM DAY/AUTO_BREAKと同じ運用）
AUTO_RS_MAX_REGISTER = 15
AUTO_RS_TAG_EXPIRE_DAYS = 2   # 元の設計案どおり2〜3営業日の短めの目安


def _auto_rs_score(row, nikkei_chg):
    """AUTO_RS_SCORE：対市場マイナスの銘柄はそもそも候補外（None）。大幅下落中（前日比-3%以下）
    または高値から10%以上崩れている銘柄も除外＝Falling Knifeの簡易ガード（日足履歴を使わない
    軽量版のため、MOMENTUM DAY/AUTO_BREAKほど厳密ではない点に留意）。
      対市場          最大40点（0pt以下で0点、+6pt以上で満点）
      対セクター      最大25点（0pt以下で0点、+4pt以上で満点）
      売買代金        最大20点（10億円未満で0点、100億円以上で満点、_liquidity_multiplierも別途乗算）
      高値維持率      最大15点（90%未満で0点、99%以上で満点）
      地合い耐性ボーナス：日経平均が-0.3%以下の軟調日に、当該銘柄が-0.5%以上（プラス〜小幅安）を
      維持している場合＋15点（「地合いが悪いのに強い」を明示的に評価）。"""
    market_rs = row.get("marketRS")
    if market_rs is None or market_rs < 0:
        return None
    chg = row.get("changePct")
    hr = row.get("highRetention")
    if chg is not None and chg <= -3:
        return None
    if hr is not None and hr < 0.90:
        return None
    score = 0.0
    score += _scale_score(market_rs, 0, 6, 40)
    score += _scale_score(row.get("sectorRS"), 0, 4, 25)
    score += _scale_score(row.get("turnover"), 1e9, 1e10, 20)
    score += _scale_score(hr, 0.90, 0.99, 15)
    if nikkei_chg is not None and nikkei_chg <= -0.3 and chg is not None and chg >= -0.5:
        score += 15
    score *= _liquidity_multiplier(row.get("turnover"))
    return round(score, 1)


# 2026-09-05（ユーザー指示：RS RESILIENCEフラグ）：「通常の強い銘柄」と「地合いが悪い日に特に
# 強い銘柄」を区別するための内部フラグ。AUTO_RS_SCOREの計算・配点は一切変更せず、既に
# _auto_rs_score()内にある地合い耐性ボーナスの判定条件（日経<=-0.3%かつ銘柄が-0.5%以上を
# 維持）をそのまま流用して真偽値化するだけ（新しい判定基準は増やさない）。STRONGはより厳しい
# 条件（日経<=-1%でも銘柄自体がプラス）。将来の「地合い悪化日の候補抽出」「翌日継続率」等の
# 検証に使えるよう、タグにそのまま保存する（今回はバックテスト機能自体は作らない）。
def _rs_resilience_tier(row, nikkei_chg):
    chg = row.get("changePct")
    market_rs = row.get("marketRS")
    if nikkei_chg is None or chg is None:
        return None
    if nikkei_chg <= -1.0 and chg > 0:
        return "STRONG"
    if nikkei_chg <= -0.3 and chg >= -0.5 and market_rs is not None and market_rs >= 2:
        return "NORMAL"
    return None


def select_auto_rs_candidates(stage1):
    nikkei_chg = stage1.get("nikkeiChangePct")
    scored = []
    for code, row in stage1["rows"].items():
        s = _auto_rs_score(row, nikkei_chg)
        if s is not None:
            scored.append((s, code, row))
    scored.sort(key=lambda x: -x[0])
    return scored


def run_auto_rs_scan(database_url, user_id, force=False):
    """Stage1（run_momentum_stage1、キャッシュ共有）→AUTO_RS_SCORE算出→閾値を満たした上位
    AUTO_RS_MAX_REGISTER件を「AUTO_RS_CURRENT」として登録、新TOP外に落ちた旧CURRENTは
    「AUTO_RS_SEEN」へ降格。Stage2（追加の日足履歴取得）を持たないため、他の2エンジンより
    高速・低負荷。"""
    stage1 = run_momentum_stage1(force=force)
    nikkei_chg = stage1.get("nikkeiChangePct")
    scored = select_auto_rs_candidates(stage1)
    details = [{"code": code, "name": row.get("name"), "sector": row.get("sector"),
                "changePct": row.get("changePct"), "highRetention": row.get("highRetention"),
                "marketRS": row.get("marketRS"), "sectorRS": row.get("sectorRS"),
                "turnover": row.get("turnover"), "finalScore": s, "current": row.get("current"),
                "resilience": _rs_resilience_tier(row, nikkei_chg)}
               for s, code, row in scored]
    to_register = [d for d in details if d["finalScore"] >= AUTO_RS_SCORE_THRESHOLD][:AUTO_RS_MAX_REGISTER]

    registered, demoted = [], []
    if database_url and investment_db is not None:
        old_current = investment_db.get_codes_with_auto_tag(database_url, user_id, "AUTO_RS_CURRENT", market="JP")
        old_seen = investment_db.get_codes_with_auto_tag(database_url, user_id, "AUTO_RS_SEEN", market="JP")
        new_current_codes = {d["code"] for d in to_register}
        now_dt = datetime.datetime.now(datetime.timezone.utc)
        expires_at = (now_dt + datetime.timedelta(days=AUTO_RS_TAG_EXPIRE_DAYS)).isoformat()

        for d in to_register:
            tag_value = {"score": d["finalScore"], "addedAt": now_dt.isoformat(), "expiresAt": expires_at,
                         "resilience": d["resilience"]}
            try:
                ok = investment_db.auto_register_or_tag_watchlist_item(
                    database_url, user_id, d["code"], "JP", "AUTO_RS_CURRENT", tag_value,
                    item_fields={"name": d["name"], "sector": d["sector"], "source": "auto_rs"})
                investment_db.remove_auto_tag_key(database_url, user_id, d["code"], "JP", "AUTO_RS_SEEN")
            except Exception as e:
                print("  AUTO_RS登録（CURRENT）失敗", d["code"], e)
                ok = False
            if ok:
                registered.append(d)

        for code in old_current - new_current_codes:
            try:
                investment_db.auto_register_or_tag_watchlist_item(
                    database_url, user_id, code, "JP", "AUTO_RS_SEEN",
                    {"addedAt": now_dt.isoformat(), "expiresAt": expires_at})
                investment_db.remove_auto_tag_key(database_url, user_id, code, "JP", "AUTO_RS_CURRENT")
                demoted.append(code)
            except Exception as e:
                print("  AUTO_RS降格（SEEN化）失敗", code, e)

        _record_signal_transitions(database_url, user_id, "RS", "JP",
            to_register, old_current, old_seen, demoted,
            metadata_fn=lambda d: {"resilience": d.get("resilience")})
    return {
        "stage1CodesScanned": stage1["codesScanned"], "stage1PricesReturned": stage1["pricesReturned"],
        "stage1DurationSec": stage1["durationSec"], "stage1RequestCount": stage1["requestCount"],
        "stage1CacheAgeSec": round(time.time() - stage1["builtAt"], 1) if stage1["builtAt"] else None,
        "stage1BuiltAtJst": _jst_time_str(stage1["builtAt"]),
        "stage1ScanFailed": stage1.get("scanFailed", False), "stage1UsedStaleCache": stage1.get("usedStaleCache", False),
        "candidateCount": len(details),
        "demotedToSeenCount": len(demoted), "demotedToSeen": demoted,
        "registeredCount": len(registered),
        "registered": registered,
        "allCandidates": details,
    }


# ============================================================
# v3-9続き：🏆 AUTO_SECTOR_LEADER
# 同じStage1（run_momentum_stage1、キャッシュ共有）を再利用。Stage2なし＝AUTO_RSと同じく
# 軽量版（セクター集計はStage1の結果を再集計するだけで、新規API呼び出しは発生しない）。
# 【設計原則】「強いセクターにいる」だけでは高評価にしない。セクター順位・セクター対市場は
# あくまで舞台の強さで、主役はrow["sectorRS"]（銘柄自身のセクター内での強さ＝「セクターの中で
# さらに強い」）。売買代金の配点をわざと小さくし、「大型株で売買代金が大きいだけ」では
# 高評価にならないようにする（ユーザー指示）。AUTO_RSとの重複は許容（むしろ高評価に値する
# 組み合わせ、UIで両方のバッジが並ぶ設計）。
# ============================================================
SECTOR_LEADER_SCORE_THRESHOLD = 55  # 実データを見て調整する前提の初期値（他エンジンと同じ運用）
SECTOR_LEADER_MAX_REGISTER = 15
SECTOR_LEADER_TAG_EXPIRE_DAYS = 2


def _compute_sector_stats(stage1, nikkei_chg):
    """Stage1の全銘柄行からセクター別の当日平均騰落率を再集計し、順位付けする
    （run_momentum_stage1内部のsector_avg計算と同じ考え方だが、キャッシュ構造は変更せず
    このエンジン内で独立して再計算する＝他エンジンへの影響を避ける）。新規API呼び出しなし。"""
    sums, counts = {}, {}
    for row in stage1["rows"].values():
        chg = row.get("changePct")
        sector = row.get("sector")
        if chg is not None and sector:
            sums[sector] = sums.get(sector, 0.0) + chg
            counts[sector] = counts.get(sector, 0) + 1
    avg = {s: sums[s] / counts[s] for s in sums}
    ranked = sorted(avg.items(), key=lambda x: -x[1])
    rank = {sector: i + 1 for i, (sector, _) in enumerate(ranked)}
    vs_market = {s: (avg[s] - nikkei_chg) if nikkei_chg is not None else None for s in avg}
    return {"avg": avg, "rank": rank, "total": len(ranked), "vsMarket": vs_market}


def _sector_rank_points(rank, total_sectors):
    """セクター順位を得点化（上位3セクターで満点25点、15位以降は0点、間は線形）。"""
    if rank is None:
        return 0.0
    if rank <= 3:
        return 25.0
    if rank >= 15:
        return 0.0
    return 25.0 * (15 - rank) / (15 - 3)


def _sector_leader_score(row, sector_stats):
    """SECTOR_LEADER_SCORE：セクター内で既に劣後している銘柄（sectorRS<0）はそもそも候補外。
    大幅下落中・高値から10%以上崩れている銘柄も除外（他エンジンと同じFalling Knife簡易ガード）。
      銘柄の対セクター（sectorRS）最大35点：主役。「セクターの中でさらに強い」を最重視
      セクター順位          最大25点：強いセクターにいることの評価（ただし主役ではない）
      セクター対市場        最大20点：セクター全体が市場よりどれだけ強いか
      高値維持率            最大12点：一過性の急騰でないことの確認
      売買代金              最大8点のみ：意図的に小さく配点（大型株優遇を避ける）。
      最後に_liquidity_multiplierを乗算（極端に薄い銘柄は除外方向へ）。"""
    sector = row.get("sector")
    if not sector or sector not in sector_stats["avg"]:
        return None
    sector_rs = row.get("sectorRS")
    if sector_rs is None or sector_rs < 0:
        return None
    chg = row.get("changePct")
    hr = row.get("highRetention")
    if chg is not None and chg <= -3:
        return None
    if hr is not None and hr < 0.90:
        return None
    rank = sector_stats["rank"].get(sector)
    sector_vs_market = sector_stats["vsMarket"].get(sector)
    score = 0.0
    score += _scale_score(sector_rs, 0, 4, 35)
    score += _sector_rank_points(rank, sector_stats["total"])
    score += _scale_score(sector_vs_market, 0, 2, 20)
    score += _scale_score(hr, 0.90, 0.99, 12)
    score += _scale_score(row.get("turnover"), 1e9, 1e10, 8)
    score *= _liquidity_multiplier(row.get("turnover"))
    return round(score, 1), rank, sector_stats["total"], sector_vs_market, sector_stats["avg"].get(sector)


# 2026-09-05（ユーザー指示：SECTOR STRENGTH補助フラグ）：「強いセクターの主役」と「弱いセクター
# の中で一人だけ強い銘柄」を区別するための内部フラグ。SECTOR_LEADER_SCOREの計算・配点は一切
# 変更せず、既に算出済みのsector_vs_market（セクター対市場）を見て分類するだけ（新しい判定基準・
# 追加のAPI呼び出しは無し）。将来の「SECTOR_LEADER件数」「STRONG_SECTOR比率」等の検証に
# 使えるよう、タグにそのまま保存する。
def _sector_strength_flag(sector_vs_market):
    if sector_vs_market is None:
        return None
    return "STRONG_SECTOR" if sector_vs_market > 0 else "WEAK_SECTOR_LEADER"


def select_sector_leader_candidates(stage1, sector_stats):
    scored = []
    for code, row in stage1["rows"].items():
        result = _sector_leader_score(row, sector_stats)
        if result is not None:
            s, rank, total, sector_vs_market, sector_avg = result
            scored.append((s, code, row, rank, total, sector_vs_market, sector_avg))
    scored.sort(key=lambda x: -x[0])
    return scored


def run_sector_leader_scan(database_url, user_id, force=False):
    """Stage1（キャッシュ共有）→セクター別集計→SECTOR_LEADER_SCORE算出→閾値を満たした上位
    SECTOR_LEADER_MAX_REGISTER件を「AUTO_SECTOR_LEADER_CURRENT」として登録、新TOP外に落ちた
    旧CURRENTは「AUTO_SECTOR_LEADER_SEEN」へ降格。AUTO_RSと同じくStage2を持たない軽量版。"""
    stage1 = run_momentum_stage1(force=force)
    nikkei_chg = stage1.get("nikkeiChangePct")
    sector_stats = _compute_sector_stats(stage1, nikkei_chg)
    scored = select_sector_leader_candidates(stage1, sector_stats)
    details = [{"code": code, "name": row.get("name"), "sector": row.get("sector"),
                "changePct": row.get("changePct"), "highRetention": row.get("highRetention"),
                "marketRS": row.get("marketRS"), "sectorRS": row.get("sectorRS"),
                "turnover": row.get("turnover"), "finalScore": s, "current": row.get("current"),
                "sectorRank": rank, "sectorTotal": total, "sectorVsMarket": sector_vs_market,
                "sectorAvgPct": sector_avg, "sectorStrength": _sector_strength_flag(sector_vs_market)}
               for s, code, row, rank, total, sector_vs_market, sector_avg in scored]
    to_register = [d for d in details if d["finalScore"] >= SECTOR_LEADER_SCORE_THRESHOLD][:SECTOR_LEADER_MAX_REGISTER]

    registered, demoted = [], []
    if database_url and investment_db is not None:
        old_current = investment_db.get_codes_with_auto_tag(database_url, user_id, "AUTO_SECTOR_LEADER_CURRENT", market="JP")
        old_seen = investment_db.get_codes_with_auto_tag(database_url, user_id, "AUTO_SECTOR_LEADER_SEEN", market="JP")
        new_current_codes = {d["code"] for d in to_register}
        now_dt = datetime.datetime.now(datetime.timezone.utc)
        expires_at = (now_dt + datetime.timedelta(days=SECTOR_LEADER_TAG_EXPIRE_DAYS)).isoformat()

        for d in to_register:
            tag_value = {"score": d["finalScore"], "sectorRank": d["sectorRank"], "sectorTotal": d["sectorTotal"],
                         "sectorStrength": d["sectorStrength"],
                         "addedAt": now_dt.isoformat(), "expiresAt": expires_at}
            try:
                ok = investment_db.auto_register_or_tag_watchlist_item(
                    database_url, user_id, d["code"], "JP", "AUTO_SECTOR_LEADER_CURRENT", tag_value,
                    item_fields={"name": d["name"], "sector": d["sector"], "source": "auto_sector_leader"})
                investment_db.remove_auto_tag_key(database_url, user_id, d["code"], "JP", "AUTO_SECTOR_LEADER_SEEN")
            except Exception as e:
                print("  AUTO_SECTOR_LEADER登録（CURRENT）失敗", d["code"], e)
                ok = False
            if ok:
                registered.append(d)

        for code in old_current - new_current_codes:
            try:
                investment_db.auto_register_or_tag_watchlist_item(
                    database_url, user_id, code, "JP", "AUTO_SECTOR_LEADER_SEEN",
                    {"addedAt": now_dt.isoformat(), "expiresAt": expires_at})
                investment_db.remove_auto_tag_key(database_url, user_id, code, "JP", "AUTO_SECTOR_LEADER_CURRENT")
                demoted.append(code)
            except Exception as e:
                print("  AUTO_SECTOR_LEADER降格（SEEN化）失敗", code, e)

        _record_signal_transitions(database_url, user_id, "SECTOR_LEADER", "JP",
            to_register, old_current, old_seen, demoted,
            metadata_fn=lambda d: {"sectorStrength": d.get("sectorStrength"), "sectorRank": d.get("sectorRank"),
                                    "sectorVsMarket": d.get("sectorVsMarket")})
    return {
        "stage1CodesScanned": stage1["codesScanned"], "stage1PricesReturned": stage1["pricesReturned"],
        "stage1DurationSec": stage1["durationSec"], "stage1RequestCount": stage1["requestCount"],
        "stage1CacheAgeSec": round(time.time() - stage1["builtAt"], 1) if stage1["builtAt"] else None,
        "stage1BuiltAtJst": _jst_time_str(stage1["builtAt"]),
        "stage1ScanFailed": stage1.get("scanFailed", False), "stage1UsedStaleCache": stage1.get("usedStaleCache", False),
        "candidateCount": len(details),
        "demotedToSeenCount": len(demoted), "demotedToSeen": demoted,
        "registeredCount": len(registered),
        "registered": registered,
        "allCandidates": details,
    }


# ============================================================
# v3-9続き：🌊 AUTO_PULLBACK
# 同じStage1（run_momentum_stage1、キャッシュ共有）を利用し、AUTO_BREAKと同様にStage2
# （銘柄ごとの日足履歴、_tachibana_daily_arraysをそのまま再利用）を持つ構成。
# 【設計原則】「新高値」ではなく「強い銘柄が浅く押して、まだ上昇トレンドを崩していない」状態を
# 拾う。VWAP付近までの押し・VWAP再奪回は、市場全体スキャンで全銘柄に分足（イントラデイ）を
# 追加取得するのは負荷が大きすぎるため（yfinanceのレート制限リスク・立花証券APIの連続大量
# リクエスト双方を避ける、2026-09-04の教訓）採用せず、日足だけで近似できる代替指標（MA25からの
# 上方乖離・前日高値／3か月高値からの浅い乖離・安値切り上げ・出来高健全性）で代用する。
# 「単に大きく下落しただけ」は複数のガード（上昇トレンド判定・乖離レンジ・出来高下限）で除外。
# ============================================================
AUTO_PULLBACK_LITE_MAX_CANDIDATES = 100
AUTO_PULLBACK_SCORE_THRESHOLD = 50  # 実データを見て調整する前提の初期値（他エンジンと同じ運用）
AUTO_PULLBACK_MAX_REGISTER = 15
AUTO_PULLBACK_TAG_EXPIRE_DAYS = 2
AUTO_PULLBACK_STAGE2_WORKERS = 10


def _pullback_lite_score(row):
    """Stage1だけでの一次選定：対市場・対セクターで強さを保っている銘柄を広く拾う。
    2026-09-05実測で発覚した不具合の修正：当初は「当日-4%〜+1%」という絶対的なchg上限も
    課していたが、日経平均自体が+1%を超えて上昇した日には marketRS(=chg−nikkei_chg)>=0 と
    chg<=+1% が数学的に両立しえず候補が0件になってしまっていた（marketRS>=0の条件だけで
    「その日の勢いだけで跳ねた銘柄」は既に十分絞り込めるため、絶対的なchg上限は不要と判断し
    撤去）。「新高値ではなく押し目」という判定自体は、この後のStage2（pullbackDevPct＝
    直近高値からの乖離率が0.3〜6%の範囲）が正式に担当する。"""
    chg = row.get("changePct")
    if chg is None or chg < -4:
        return None
    market_rs = row.get("marketRS")
    if market_rs is None or market_rs < 0:
        return None
    score = 0.0
    score += _scale_score(market_rs, 0, 5, 30)
    score += _scale_score(row.get("sectorRS"), 0, 3, 20)
    score += _scale_score(row.get("turnover"), 1e9, 1e10, 20)
    score *= _liquidity_multiplier(row.get("turnover"))
    return round(score, 1)


def select_pullback_stage1_candidates(stage1):
    scored = []
    for code, row in stage1["rows"].items():
        s = _pullback_lite_score(row)
        if s is not None:
            scored.append((s, code, row))
    scored.sort(key=lambda x: -x[0])
    return scored[:AUTO_PULLBACK_LITE_MAX_CANDIDATES]


def _pullback_stage2_detail(code, stage1_row):
    """Stage1通過銘柄だけに、日足履歴が要る指標（MA25・押し目の乖離率・安値切り上げ・出来高
    倍率）を追加する。VWAPの代わりにMA25からの上方乖離を「トレンドの強さ」の代理指標として使う。
    2026-09-05 PHASE2追加：「今日/前日高値からの1〜2%の押し」を「本当の押し目」と誤認しない
    ように、①直近高値が何営業日前に形成されたか（daysSinceHigh、recentHighDateの代わりに
    使える同等情報。日足配列のインデックスだけで算出でき、新規API不要）、②MA25自体が上向きか
    （ma25Rising、5営業日前のMA25と比較）を追加する。"""
    arrays = _tachibana_daily_arrays(code)
    if not arrays:
        return None
    closes, opens, highs, lows, volumes = arrays
    if len(closes) < 30 or len(volumes) < 6 or len(lows) < 2:
        return None
    current = stage1_row.get("current") if stage1_row.get("current") is not None else closes[-1]
    ma25 = sum(closes[-25:]) / 25
    uptrend = current > ma25
    # MA25の傾き：5営業日前時点のMA25と比較して上向きかどうか（既存の日足配列だけで算出、追加API不要）。
    ma25_prev = sum(closes[-30:-5]) / 25
    ma25_slope_pct = ((ma25 - ma25_prev) / ma25_prev * 100) if ma25_prev else None
    ma25_rising = bool(ma25_slope_pct is not None and ma25_slope_pct > 0)
    vol_avg5 = sum(volumes[-6:-1]) / 5
    vol_ratio = (volumes[-1] / vol_avg5) if vol_avg5 else None
    # 直近高値（3か月高値）の探索。単なるmax()ではなく、何営業日前がピークだったかも同時に求める
    # （argmax、当日を除く既存のhighs配列のスライスだけで完結、追加API不要）。
    lookback_window = highs[-(BREAKOUT_LOOKBACK_DAYS + 1):-1] if len(highs) >= BREAKOUT_LOOKBACK_DAYS + 1 else []
    if lookback_window:
        max_idx = max(range(len(lookback_window)), key=lambda i: lookback_window[i])
        breakout_lookback_high = lookback_window[max_idx]
        days_since_breakout_high = len(lookback_window) - max_idx  # 末尾（=前営業日）がピークなら1
    else:
        breakout_lookback_high = None
        days_since_breakout_high = None
    prev_day_high = highs[-2]
    # 押し目の基準水準：直近ブレイク水準（3か月高値）があればそちらを優先、無ければ前日高値
    # （この場合は定義上「1営業日前の高値」となる）。
    if breakout_lookback_high is not None:
        ref_high, days_since_high = breakout_lookback_high, days_since_breakout_high
    else:
        ref_high, days_since_high = prev_day_high, 1
    pullback_dev_pct = ((ref_high - current) / ref_high * 100) if (ref_high and ref_high > 0) else None
    higher_low = bool(lows[-1] >= lows[-2] * 0.995)  # 安値切り上げ（0.5%の許容誤差）
    return {"ma25": round(ma25, 1), "uptrend": uptrend, "ma25SlopePct": round(ma25_slope_pct, 2) if ma25_slope_pct is not None else None,
            "ma25Rising": ma25_rising,
            "volRatio": round(vol_ratio, 2) if vol_ratio is not None else None,
            "higherLow": higher_low, "refHigh": round(ref_high, 1) if ref_high is not None else None,
            "daysSinceHigh": days_since_high,
            "pullbackDevPct": round(pullback_dev_pct, 2) if pullback_dev_pct is not None else None}


def _pullback_final_score(row, stage2):
    """PULLBACK_SCORE：上昇トレンドが崩れている（現在値がMA25未満）銘柄はそもそも対象外。
    押し目の乖離が浅すぎる（まだ高値圏＝押し目になっていない）または深すぎる（6%超＝
    ただの下落）銘柄も対象外。出来高が薄すぎる（0.7倍未満）銘柄も「見放された押し目」として除外。
    （既存ゲート・配点は維持）
      対市場（marketRS）        最大30点
      押し目の浅さ              最大25点（0.3%程度で満点、6%で0点）
      安値切り上げ              +20点（binary）
      出来高健全性              最大15点（0.7倍未満は対象外、1.5倍以上で満点）
      MA25からの上方乖離        最大10点（トレンドの強さの補強）
    2026-09-05 PHASE2追加：「本当の押し目」（数営業日前に高値形成→調整→MA25上向き）を
    より高く評価する。
      直近高値の形成タイミング  最大15点（1営業日前で0点、10営業日以上前で満点）
      MA25の傾き（上向き）      +10点
    さらに、直近高値がまさに1営業日前（＝実質「前日高値から1〜2%押しただけ」）の場合は、
    合計スコアを0.5倍に減点する（対象外にはしないが、「本当の押し目」として高評価しすぎない、
    というユーザー方針をスコア面で反映する）。"""
    if not stage2 or not stage2["uptrend"]:
        return None
    dev = stage2.get("pullbackDevPct")
    if dev is None or dev < 0.3 or dev > 6:
        return None
    vol_ratio = stage2.get("volRatio")
    if vol_ratio is not None and vol_ratio < 0.7:
        return None
    score = 0.0
    score += _scale_score(row.get("marketRS"), 0, 5, 30)
    score += _scale_score(6 - dev, 0, 5.7, 25)
    if stage2.get("higherLow"):
        score += 20
    if vol_ratio is not None:
        score += _scale_score(vol_ratio, 0.7, 1.5, 15)
    current, ma25 = row.get("current"), stage2.get("ma25")
    if current is not None and ma25:
        score += _scale_score((current - ma25) / ma25 * 100, 0, 5, 10)
    days_since_high = stage2.get("daysSinceHigh")
    if days_since_high is not None:
        score += _scale_score(days_since_high, 1, 10, 15)
    if stage2.get("ma25Rising"):
        score += 10
    score *= _liquidity_multiplier(row.get("turnover"))
    if days_since_high is not None and days_since_high < 2:
        score *= 0.5  # 高値形成から1営業日以内の押しは「本当の押し目」として過大評価しない
    return round(score, 1)


def run_pullback_scan(database_url, user_id, force=False):
    """Stage1（キャッシュ共有）→PULLBACK候補選定→Stage2（並列、AUTO_BREAKと同じ構成）→
    PULLBACK_SCORE確定→閾値を満たした上位AUTO_PULLBACK_MAX_REGISTER件を
    「AUTO_PULLBACK_CURRENT」として登録、新TOP外に落ちた旧CURRENTは「AUTO_PULLBACK_SEEN」へ降格。"""
    stage1 = run_momentum_stage1(force=force)
    candidates = select_pullback_stage1_candidates(stage1)
    t_stage2_start = time.time()

    stage2_results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=AUTO_PULLBACK_STAGE2_WORKERS) as ex:
        futures = {ex.submit(_pullback_stage2_detail, code, row): code for _, code, row in candidates}
        for fut in concurrent.futures.as_completed(futures):
            code = futures[fut]
            try:
                stage2_results[code] = fut.result()
            except Exception as e:
                print("  PULLBACK Stage2詳細取得失敗", code, e)
                stage2_results[code] = None
    stage2_duration = round(time.time() - t_stage2_start, 1)

    details = []
    for lite_score, code, row in candidates:
        stage2 = stage2_results.get(code)
        final_score = _pullback_final_score(row, stage2)
        if final_score is None:
            continue
        details.append({"code": code, "name": row.get("name"), "sector": row.get("sector"),
                         "changePct": row.get("changePct"), "highRetention": row.get("highRetention"),
                         "marketRS": row.get("marketRS"), "sectorRS": row.get("sectorRS"),
                         "turnover": row.get("turnover"), "liteScore": lite_score,
                         "current": row.get("current"),
                         "finalScore": final_score, "stage2": stage2})
    details.sort(key=lambda d: -d["finalScore"])
    to_register = [d for d in details if d["finalScore"] >= AUTO_PULLBACK_SCORE_THRESHOLD][:AUTO_PULLBACK_MAX_REGISTER]

    registered, demoted = [], []
    if database_url and investment_db is not None:
        old_current = investment_db.get_codes_with_auto_tag(database_url, user_id, "AUTO_PULLBACK_CURRENT", market="JP")
        old_seen = investment_db.get_codes_with_auto_tag(database_url, user_id, "AUTO_PULLBACK_SEEN", market="JP")
        new_current_codes = {d["code"] for d in to_register}
        now_dt = datetime.datetime.now(datetime.timezone.utc)
        expires_at = (now_dt + datetime.timedelta(days=AUTO_PULLBACK_TAG_EXPIRE_DAYS)).isoformat()

        for d in to_register:
            tag_value = {"score": d["finalScore"], "pullbackDevPct": d["stage2"]["pullbackDevPct"],
                         "higherLow": d["stage2"]["higherLow"],
                         "daysSinceHigh": d["stage2"].get("daysSinceHigh"), "ma25Rising": d["stage2"].get("ma25Rising"),
                         "addedAt": now_dt.isoformat(), "expiresAt": expires_at}
            try:
                ok = investment_db.auto_register_or_tag_watchlist_item(
                    database_url, user_id, d["code"], "JP", "AUTO_PULLBACK_CURRENT", tag_value,
                    item_fields={"name": d["name"], "sector": d["sector"], "source": "auto_pullback"})
                investment_db.remove_auto_tag_key(database_url, user_id, d["code"], "JP", "AUTO_PULLBACK_SEEN")
            except Exception as e:
                print("  AUTO_PULLBACK登録（CURRENT）失敗", d["code"], e)
                ok = False
            if ok:
                registered.append(d)

        for code in old_current - new_current_codes:
            try:
                investment_db.auto_register_or_tag_watchlist_item(
                    database_url, user_id, code, "JP", "AUTO_PULLBACK_SEEN",
                    {"addedAt": now_dt.isoformat(), "expiresAt": expires_at})
                investment_db.remove_auto_tag_key(database_url, user_id, code, "JP", "AUTO_PULLBACK_CURRENT")
                demoted.append(code)
            except Exception as e:
                print("  AUTO_PULLBACK降格（SEEN化）失敗", code, e)

        def _pullback_metadata(d):
            # 2026-09-05実測で発覚した不具合の修正：ma25Deviationにはma25そのもの（株価）ではなく
            # 現在値からの乖離率(%)を保存する（フィールド名の意味と一致させる）。
            ma25 = d["stage2"].get("ma25")
            current = d.get("current")
            ma25_dev_pct = round((current - ma25) / ma25 * 100, 2) if (current and ma25) else None
            # recentHighDateは実際のカレンダー日付を取得するには_tachibana_daily_arraysの
            # 戻り値に日付配列を追加する必要があり非該当（新規のデータ取得は増やさない方針）。
            # 「同等情報」としてdaysSinceHigh（直近高値が何営業日前か）を代わりに保存する
            # （ユーザー承認済み：recentHighDate「または同等情報」）。
            return {"recentHighDeviation": d["stage2"].get("pullbackDevPct"),
                    "daysSinceHigh": d["stage2"].get("daysSinceHigh"),
                    "ma25Deviation": ma25_dev_pct,
                    "ma25Slope": d["stage2"].get("ma25SlopePct"), "ma25Rising": d["stage2"].get("ma25Rising"),
                    "higherLow": d["stage2"].get("higherLow"),
                    "volumeRatio": d["stage2"].get("volRatio")}
        _record_signal_transitions(database_url, user_id, "PULLBACK", "JP",
            to_register, old_current, old_seen, demoted, metadata_fn=_pullback_metadata)
    return {
        "stage1CodesScanned": stage1["codesScanned"], "stage1PricesReturned": stage1["pricesReturned"],
        "stage1DurationSec": stage1["durationSec"], "stage1RequestCount": stage1["requestCount"],
        "stage1CacheAgeSec": round(time.time() - stage1["builtAt"], 1) if stage1["builtAt"] else None,
        "stage1BuiltAtJst": _jst_time_str(stage1["builtAt"]),
        "stage1ScanFailed": stage1.get("scanFailed", False), "stage1UsedStaleCache": stage1.get("usedStaleCache", False),
        "stage1CandidateCount": len(candidates),
        "stage2ValidCount": len(details),
        "stage2DurationSec": stage2_duration,
        "demotedToSeenCount": len(demoted), "demotedToSeen": demoted,
        "registeredCount": len(registered),
        "registered": registered,
        "allCandidates": details,
    }


# ============================================================
# v3-9続き（2026-09-07・AUTO_REVERSAL）：「下落中の銘柄を拾う」のではなく「下落後に反転が
# 確認できた銘柄」を発見するエンジン。既存のStage1共有キャッシュ（run_momentum_stage1）・
# CURRENT/SEEN・manual_registered保護・auto_signal_events・_record_signal_transitionsを
# そのまま再利用し、新しい全市場スキャンは追加しない（Stage2は他エンジンと同様、候補銘柄
# ごとに日足履歴を取得するだけ）。
# AUTO_PULLBACKとの違い：PULLBACKは「上昇トレンド中の浅い調整」（current>MA25が前提）、
# REVERSALは「下落・弱含みからの反転」（MA25割れ状態からの回復途上も対象）。両者は排他にせず、
# 重複登録があっても禁止しない（ユーザー指示）が、判定の入口（トレンド継続 vs 下落後の反転）が
# 異なるため通常は別銘柄になる想定。
# 最重要原則：「落ちるナイフは掴まない」。_reversal_falling_knife()に該当する銘柄は
# スコア計算そのものを行わずNoneを返し、CURRENT登録対象から完全に除外する。
# ============================================================
REVERSAL_LOW_LOOKBACK_DAYS = 20  # 「直近安値」を探す範囲（直近5〜20営業日、ユーザー指示の上限）
REVERSAL_LITE_MAX_CANDIDATES = 100
REVERSAL_SCORE_THRESHOLD = 50  # 実データを見て調整する前提の初期値（他エンジンと同じ運用）
REVERSAL_MAX_REGISTER = 15
REVERSAL_TAG_EXPIRE_DAYS = 2
REVERSAL_STAGE2_WORKERS = 10


def _reversal_lite_score(row):
    """Stage1だけでの一次選定：まだ大幅下落中の当日（chg<-8%）はStage2に回さない（反転どころか
    パニック売りの最中である可能性が高い）。当日安値付近に張り付いたまま（day_bounce_pct<25%）
    の銘柄も「戻りが無い」として除外する。それ以外は対市場・対セクター・売買代金で広く拾い、
    反転構造の確定判定（higherLow・MA reclaim・抵抗突破等）はStage2（日足履歴）に委ねる。"""
    chg = row.get("changePct")
    if chg is None or chg < -8:
        return None
    current, low, high = row.get("current"), row.get("low"), row.get("high")
    if current is not None and low is not None and high is not None and high > low:
        day_bounce_pct = (current - low) / (high - low) * 100
        if day_bounce_pct < 25:
            return None
    score = 0.0
    score += _scale_score(row.get("marketRS"), -2, 5, 25)
    score += _scale_score(row.get("sectorRS"), -2, 3, 15)
    score += _scale_score(row.get("turnover"), 1e9, 1e10, 20)
    score *= _liquidity_multiplier(row.get("turnover"))
    return round(score, 1)


def select_reversal_stage1_candidates(stage1):
    scored = []
    for code, row in stage1["rows"].items():
        s = _reversal_lite_score(row)
        if s is not None:
            scored.append((s, code, row))
    scored.sort(key=lambda x: -x[0])
    return scored[:REVERSAL_LITE_MAX_CANDIDATES]


def _tachibana_daily_history_with_dates(code):
    """_tachibana_daily_arrays()と同じ日足取得・当日分合成ロジックだが、AUTO_REVERSAL専用に
    date配列とopens配列も返す（recentLowDate算出・gap down判定に使う）。既存5エンジンが使う
    _tachibana_daily_arrays()は返り値のタプル数が異なる既存呼び出し元を壊さないよう変更せず、
    この専用関数を別途新設する（新しいAPI呼び出しは増やさない＝同じget_daily_history()を
    1回呼ぶだけ、他エンジンのStage2と同じコスト）。"""
    if tachibana_api is None or not code:
        return None
    try:
        hist = tachibana_api.get_daily_history(code)
    except Exception as e:
        print(f"  立花証券API 日足取得失敗（AUTO_REVERSAL・{code}）", e)
        return None
    if len(hist) < 30:
        return None
    hist = hist[-400:]
    dates = [r["date"] for r in hist]
    opens = [r["open"] for r in hist]
    highs = [r["high"] for r in hist]
    lows = [r["low"] for r in hist]
    closes = [r["close"] for r in hist]
    volumes = [r["volume"] for r in hist]

    jst = datetime.timezone(datetime.timedelta(hours=9))
    today_str = datetime.datetime.now(jst).strftime("%Y-%m-%d")
    if dates[-1] != today_str:
        try:
            live = tachibana_api.get_market_price([code]).get(code)
        except Exception:
            live = None
        if live and live.get("t") is not None and live.get("open") is not None:
            dates.append(today_str)
            opens.append(live["open"])
            highs.append(live.get("high") if live.get("high") is not None else live["t"])
            lows.append(live.get("low") if live.get("low") is not None else live["t"])
            closes.append(live["t"])
            volumes.append(live.get("volume") if live.get("volume") is not None else 0)
    return dates, opens, highs, lows, closes, volumes


def _reversal_stage2_detail(code, stage1_row):
    """日足履歴を使って反転構造を確認する。最低確認項目：recentLow（直近安値の日付・価格）・
    higherLow（安値切り上げ）・ma5/10/25 reclaim・ma25の傾き・直近戻り高値の突破・出来高倍率。"""
    data = _tachibana_daily_history_with_dates(code)
    if not data:
        return None
    dates, opens, highs, lows, closes, volumes = data
    if len(closes) < 30 or len(volumes) < 6:
        return None
    current = stage1_row.get("current") if stage1_row.get("current") is not None else closes[-1]

    # 直近安値の探索（当日を除く、直近REVERSAL_LOW_LOOKBACK_DAYS営業日の最安値）。
    window_lows = lows[-(REVERSAL_LOW_LOOKBACK_DAYS + 1):-1]
    window_dates = dates[-(REVERSAL_LOW_LOOKBACK_DAYS + 1):-1]
    window_highs = highs[-(REVERSAL_LOW_LOOKBACK_DAYS + 1):-1]
    if not window_lows:
        return None
    min_idx = min(range(len(window_lows)), key=lambda i: window_lows[i])
    recent_low_price = window_lows[min_idx]
    recent_low_date = window_dates[min_idx]
    recent_low_days_ago = len(window_lows) - min_idx  # 末尾（前営業日）がrecentLowなら1

    today_low = lows[-1]
    making_new_low_today = today_low <= recent_low_price

    # 安値切り上げ：recentLow形成"後"、当日より前の営業日でrecentLowを再度割っていないか
    # （0.5%の許容誤差。既存AUTO_PULLBACKのhigherLow判定と同じ考え方）。
    after_low = window_lows[min_idx + 1:]
    broke_recent_low_again = any(l < recent_low_price * 0.995 for l in after_low)
    higher_low = (not broke_recent_low_again) and (not making_new_low_today) and current >= recent_low_price * 0.995

    current_from_low_pct = ((current - recent_low_price) / recent_low_price * 100) if recent_low_price else None

    ma5 = sum(closes[-5:]) / 5
    ma10 = sum(closes[-10:]) / 10
    ma25 = sum(closes[-25:]) / 25
    ma25_prev = sum(closes[-30:-5]) / 25
    ma25_slope_pct = ((ma25 - ma25_prev) / ma25_prev * 100) if ma25_prev else None
    ma25_rising = bool(ma25_slope_pct is not None and ma25_slope_pct > 0)
    ma5_reclaim = bool(current > ma5)
    ma10_reclaim = bool(current > ma10)
    ma25_reclaim = bool(current > ma25)

    # 抵抗線＝recentLow形成後の戻り高値（無ければ前日高値で代用）。この水準を上抜けたことを
    # 「抵抗突破」とする（大局の3か月高値＝AUTO_PULLBACKのrefHighとは別物、あくまで下落からの
    # 戻りの過程でできた直近の壁）。
    resistance_window = window_highs[min_idx + 1:]
    resistance = max(resistance_window) if resistance_window else highs[-2]
    resistance_break = bool(resistance and current > resistance)

    vol_avg5 = sum(volumes[-6:-1]) / 5
    vol_ratio = (volumes[-1] / vol_avg5) if vol_avg5 else None

    # 強いgap down継続：当日始値が前日終値から3%以上下に窓を開け、なお前日終値を回復できていない。
    gap_down = bool(len(closes) >= 2 and closes[-2] and opens[-1] < closes[-2] * 0.97 and current < closes[-2])

    return {"recentLowDate": recent_low_date, "recentLowPrice": round(recent_low_price, 1),
            "recentLowDaysAgo": recent_low_days_ago,
            "currentFromRecentLowPct": round(current_from_low_pct, 2) if current_from_low_pct is not None else None,
            "higherLow": higher_low, "makingNewLowToday": making_new_low_today,
            "brokeRecentLowAgain": broke_recent_low_again,
            "ma5": round(ma5, 1), "ma10": round(ma10, 1), "ma25": round(ma25, 1),
            "ma5Reclaim": ma5_reclaim, "ma10Reclaim": ma10_reclaim, "ma25Reclaim": ma25_reclaim,
            "ma25SlopePct": round(ma25_slope_pct, 2) if ma25_slope_pct is not None else None,
            "ma25Rising": ma25_rising,
            "resistance": round(resistance, 1) if resistance else None, "resistanceBreak": resistance_break,
            "volRatio": round(vol_ratio, 2) if vol_ratio is not None else None,
            "gapDown": gap_down, "dayChangePct": stage1_row.get("changePct")}


def _reversal_falling_knife(stage2, row):
    """最重要原則：「落ちるナイフは掴まない」。以下のいずれかに該当すればAUTO_REVERSAL不可
    （スコア計算自体を行わずNoneを返す＝CURRENT登録対象から完全除外）。
      1. 当日安値更新中（makingNewLowToday）
      2. 現在値が直近安値を割っている（＝安値切り下げ）
      3. MA5/10/25すべて未回復かつMA25も下向き（反転の兆候が一つも無い）
      4. 直近安値を形成した後、再度その安値を割っている（brokeRecentLowAgain）
      5. 出来高急増を伴う大幅下落（当日騰落率<-5%かつ出来高1.5倍超）
      6. 強いgap downが継続（前日終値未回復）"""
    if stage2 is None:
        return True
    if stage2["makingNewLowToday"]:
        return True
    current = row.get("current")
    if current is not None and stage2.get("recentLowPrice") and current < stage2["recentLowPrice"]:
        return True
    if not stage2["ma5Reclaim"] and not stage2["ma10Reclaim"] and not stage2["ma25Reclaim"] and not stage2["ma25Rising"]:
        return True
    if stage2["brokeRecentLowAgain"]:
        return True
    day_change = row.get("changePct")
    vol_ratio = stage2.get("volRatio")
    if day_change is not None and day_change < -5 and vol_ratio is not None and vol_ratio > 1.5:
        return True
    if stage2.get("gapDown"):
        return True
    return False


def _reversal_types(stage2):
    """metadata.reversalTypes：複数の反転根拠を持つ場合はMULTI_CONFIRMATIONも併記する。"""
    types = []
    if stage2.get("higherLow"):
        types.append("HIGHER_LOW")
    if stage2.get("ma5Reclaim") or stage2.get("ma10Reclaim") or stage2.get("ma25Reclaim"):
        types.append("MA_RECLAIM")
    if stage2.get("resistanceBreak"):
        types.append("RESISTANCE_BREAK")
    if stage2.get("volRatio") is not None and stage2["volRatio"] >= 1.3:
        types.append("VOLUME_REVERSAL")
    if len(types) >= 3:
        types.append("MULTI_CONFIRMATION")
    return types


def _reversal_final_score(row, stage2):
    """REVERSAL_SCORE（100点満点、初期配点。実データを見て微調整する前提）：
      Higher Low                25点（binary）
      短期MA reclaim            最大20点（MA5回復+10・MA10回復+10）
      直近戻り高値の抵抗突破     20点（binary）
      出来高改善                最大15点（1.0倍で0点、1.8倍以上で満点）
      Market RS                 最大10点
      Sector RS                 最大10点
    Falling Knife判定に該当する場合はNone（スコア計算自体を行わない＝AUTO_REVERSAL不可）。"""
    if stage2 is None:
        return None
    if _reversal_falling_knife(stage2, row):
        return None
    score = 0.0
    if stage2["higherLow"]:
        score += 25
    ma_reclaim_pts = (10 if stage2["ma5Reclaim"] else 0) + (10 if stage2["ma10Reclaim"] else 0)
    score += min(ma_reclaim_pts, 20)
    if stage2["resistanceBreak"]:
        score += 20
    if stage2.get("volRatio") is not None:
        score += _scale_score(stage2["volRatio"], 1.0, 1.8, 15)
    score += _scale_score(row.get("marketRS"), 0, 5, 10)
    score += _scale_score(row.get("sectorRS"), 0, 3, 10)
    score *= _liquidity_multiplier(row.get("turnover"))
    return round(score, 1)


def run_reversal_scan(database_url, user_id, force=False):
    """Stage1（キャッシュ共有）→REVERSAL候補選定→Stage2（並列、他エンジンと同じ構成）→
    REVERSAL_SCORE確定（Falling Knife該当は除外）→閾値を満たした上位REVERSAL_MAX_REGISTER件を
    「AUTO_REVERSAL_CURRENT」として登録、新TOP外に落ちた旧CURRENTは「AUTO_REVERSAL_SEEN」へ降格。"""
    stage1 = run_momentum_stage1(force=force)
    candidates = select_reversal_stage1_candidates(stage1)
    t_stage2_start = time.time()

    stage2_results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=REVERSAL_STAGE2_WORKERS) as ex:
        futures = {ex.submit(_reversal_stage2_detail, code, row): code for _, code, row in candidates}
        for fut in concurrent.futures.as_completed(futures):
            code = futures[fut]
            try:
                stage2_results[code] = fut.result()
            except Exception as e:
                print("  REVERSAL Stage2詳細取得失敗", code, e)
                stage2_results[code] = None
    stage2_duration = round(time.time() - t_stage2_start, 1)

    details = []
    for lite_score, code, row in candidates:
        stage2 = stage2_results.get(code)
        final_score = _reversal_final_score(row, stage2)
        if final_score is None:
            continue
        details.append({"code": code, "name": row.get("name"), "sector": row.get("sector"),
                         "changePct": row.get("changePct"), "highRetention": row.get("highRetention"),
                         "marketRS": row.get("marketRS"), "sectorRS": row.get("sectorRS"),
                         "turnover": row.get("turnover"), "liteScore": lite_score,
                         "current": row.get("current"),
                         "finalScore": final_score, "stage2": stage2})
    details.sort(key=lambda d: -d["finalScore"])
    to_register = [d for d in details if d["finalScore"] >= REVERSAL_SCORE_THRESHOLD][:REVERSAL_MAX_REGISTER]

    registered, demoted = [], []
    if database_url and investment_db is not None:
        old_current = investment_db.get_codes_with_auto_tag(database_url, user_id, "AUTO_REVERSAL_CURRENT", market="JP")
        old_seen = investment_db.get_codes_with_auto_tag(database_url, user_id, "AUTO_REVERSAL_SEEN", market="JP")
        new_current_codes = {d["code"] for d in to_register}
        now_dt = datetime.datetime.now(datetime.timezone.utc)
        expires_at = (now_dt + datetime.timedelta(days=REVERSAL_TAG_EXPIRE_DAYS)).isoformat()

        for d in to_register:
            tag_value = {"score": d["finalScore"], "higherLow": d["stage2"]["higherLow"],
                         "currentFromRecentLowPct": d["stage2"].get("currentFromRecentLowPct"),
                         "reversalTypes": _reversal_types(d["stage2"]),
                         "addedAt": now_dt.isoformat(), "expiresAt": expires_at}
            try:
                ok = investment_db.auto_register_or_tag_watchlist_item(
                    database_url, user_id, d["code"], "JP", "AUTO_REVERSAL_CURRENT", tag_value,
                    item_fields={"name": d["name"], "sector": d["sector"], "source": "auto_reversal"})
                investment_db.remove_auto_tag_key(database_url, user_id, d["code"], "JP", "AUTO_REVERSAL_SEEN")
            except Exception as e:
                print("  AUTO_REVERSAL登録（CURRENT）失敗", d["code"], e)
                ok = False
            if ok:
                registered.append(d)

        for code in old_current - new_current_codes:
            try:
                investment_db.auto_register_or_tag_watchlist_item(
                    database_url, user_id, code, "JP", "AUTO_REVERSAL_SEEN",
                    {"addedAt": now_dt.isoformat(), "expiresAt": expires_at})
                investment_db.remove_auto_tag_key(database_url, user_id, code, "JP", "AUTO_REVERSAL_CURRENT")
                demoted.append(code)
            except Exception as e:
                print("  AUTO_REVERSAL降格（SEEN化）失敗", code, e)

        def _reversal_metadata(d):
            s2 = d["stage2"]
            return {"recentLowDate": s2.get("recentLowDate"), "recentLowPrice": s2.get("recentLowPrice"),
                    "recentLowDaysAgo": s2.get("recentLowDaysAgo"),
                    "currentFromRecentLowPct": s2.get("currentFromRecentLowPct"),
                    "higherLow": s2.get("higherLow"),
                    "ma5Reclaim": s2.get("ma5Reclaim"), "ma10Reclaim": s2.get("ma10Reclaim"),
                    "ma25Reclaim": s2.get("ma25Reclaim"), "ma25Slope": s2.get("ma25SlopePct"),
                    "resistanceBreak": s2.get("resistanceBreak"), "volumeRatio": s2.get("volRatio"),
                    "reversalTypes": _reversal_types(s2)}
        _record_signal_transitions(database_url, user_id, "REVERSAL", "JP",
            to_register, old_current, old_seen, demoted, metadata_fn=_reversal_metadata)
    return {
        "stage1CodesScanned": stage1["codesScanned"], "stage1PricesReturned": stage1["pricesReturned"],
        "stage1DurationSec": stage1["durationSec"], "stage1RequestCount": stage1["requestCount"],
        "stage1CacheAgeSec": round(time.time() - stage1["builtAt"], 1) if stage1["builtAt"] else None,
        "stage1BuiltAtJst": _jst_time_str(stage1["builtAt"]),
        "stage1ScanFailed": stage1.get("scanFailed", False), "stage1UsedStaleCache": stage1.get("usedStaleCache", False),
        "stage1CandidateCount": len(candidates),
        "stage2ValidCount": len(details),
        "stage2DurationSec": stage2_duration,
        "demotedToSeenCount": len(demoted), "demotedToSeen": demoted,
        "registeredCount": len(registered),
        "registered": registered,
        "allCandidates": details,
    }


# ============================================================
# v3-9続き（2026-09-07・AUTO_VOLUME）：「出来高が多い銘柄を拾う」のではなく「通常時と比べて
# 異常に資金が流入している銘柄」を、価格方向・売買代金・RS・高値維持と合わせて検出するエンジン。
# 既存Stage1共有キャッシュ・CURRENT/SEEN・manual_registered保護・auto_signal_eventsをそのまま
# 再利用し、新しい全市場スキャンは追加しない。
# 最重要課題：日中出来高の時間帯依存。取引時間中の途中出来高を過去の1日平均出来高と単純比較
# すると朝ほどvolRatioが低くなる（AUTO_REVERSAL実データ検証で0.05〜0.1という値が出た問題と
# 同根）。_market_time_progress_ratio()で「今の時刻までに通常1日の何%程度出来高が進むはずか」
# を東証の前場(09:00-11:30)・昼休み(11:30-12:30、出来高進行を止める)・後場(12:30-15:30)を
# 区別して算出し、timeAdjustedVolumeRatio（時間帯補正後）とrawVolumeRatio（補正前の生の倍率、
# 大引け後の最終確定値として使う）を両方保持する。
# ============================================================
VOLUME_LITE_MAX_CANDIDATES = 150
VOLUME_SCORE_THRESHOLD = 50  # 実データを見て調整する前提の初期値（他エンジンと同じ運用）
VOLUME_MAX_REGISTER = 15
VOLUME_TAG_EXPIRE_DAYS = 2
VOLUME_STAGE2_WORKERS = 10
VOLUME_HIGH_LOOKBACK_DAYS = 63  # distanceFromHigh・break statusの参照期間（AUTO_PULLBACKの
                                 # BREAKOUT_LOOKBACK_DAYSと同じ3か月＝既存の考え方を踏襲）


def _market_time_progress_ratio(now_jst=None):
    """東京市場の取引時間（前場09:00-11:30・昼休み11:30-12:30は進行停止・後場12:30-15:30、
    合計330分）を基準に、「現在時刻までに通常1日の出来高の何%程度が形成されるはずか」を返す
    （0.0〜1.0）。09:00より前は0.0（未寄り付き）、15:30以降は1.0（大引け後＝rawVolumeRatioが
    そのまま最終確定値として使える）。前場・後場をそれぞれ線形補間し、昼休みは前場終了時点の
    値のまま据え置く（単純な一日全体の線形補間にしない、ユーザー指示）。"""
    if now_jst is None:
        jst = datetime.timezone(datetime.timedelta(hours=9))
        now_jst = datetime.datetime.now(jst)
    t = now_jst.time()
    morning_start, morning_end = datetime.time(9, 0), datetime.time(11, 30)
    lunch_end, afternoon_end = datetime.time(12, 30), datetime.time(15, 30)
    morning_minutes, afternoon_minutes = 150, 180
    total_minutes = morning_minutes + afternoon_minutes
    if t < morning_start:
        return 0.0
    if t <= morning_end:
        elapsed = (t.hour * 60 + t.minute) - (morning_start.hour * 60 + morning_start.minute)
        return max(0.0, min(1.0, elapsed / total_minutes))
    if t <= lunch_end:
        return morning_minutes / total_minutes  # 昼休み中は前場終了時点の進行度で据え置く
    if t <= afternoon_end:
        elapsed_pm = (t.hour * 60 + t.minute) - (lunch_end.hour * 60 + lunch_end.minute)
        return min(1.0, (morning_minutes + elapsed_pm) / total_minutes)
    return 1.0  # 大引け後


def _volume_lite_score(row):
    """Stage1だけでの一次選定：売買代金・当日騰落率の絶対値・高値維持率・対市場/対セクターで
    広く拾う（精密な出来高倍率はStage2の責務、ユーザー指示：Stage1では必須にしない）。"""
    turnover = row.get("turnover")
    if turnover is None or turnover < 3e8:
        return None  # 売買代金3億円未満は資金流入の議論に値しないレベルとして除外
    chg = row.get("changePct")
    score = 0.0
    score += _scale_score(turnover, 3e8, 1e10, 30)
    score += _scale_score(abs(chg) if chg is not None else 0, 0, 8, 25)
    score += _scale_score(row.get("highRetention"), 0.7, 0.99, 15)
    score += _scale_score(abs(row.get("marketRS")) if row.get("marketRS") is not None else 0, 0, 5, 15)
    score += _scale_score(abs(row.get("sectorRS")) if row.get("sectorRS") is not None else 0, 0, 3, 15)
    score *= _liquidity_multiplier(turnover)
    return round(score, 1)


def select_volume_stage1_candidates(stage1):
    scored = []
    for code, row in stage1["rows"].items():
        s = _volume_lite_score(row)
        if s is not None:
            scored.append((s, code, row))
    scored.sort(key=lambda x: -x[0])
    return scored[:VOLUME_LITE_MAX_CANDIDATES]


def _volume_stage2_detail(code, stage1_row):
    """日足履歴から過去20営業日平均出来高（avgVolume20）・当日出来高との倍率（生・時間帯補正後
    の両方）・売買代金倍率・直近高値からの乖離（distanceFromHigh）・break status等を算出する。"""
    arrays = _tachibana_daily_arrays(code)
    if not arrays:
        return None
    closes, opens, highs, lows, volumes = arrays
    if len(closes) < 25 or len(volumes) < 21:
        return None
    current = stage1_row.get("current") if stage1_row.get("current") is not None else closes[-1]

    avg_volume20 = sum(volumes[-21:-1]) / 20
    current_volume = volumes[-1]
    raw_volume_ratio = (current_volume / avg_volume20) if avg_volume20 else None

    progress = _market_time_progress_ratio()
    is_intraday = progress < 1.0
    expected_volume_so_far = avg_volume20 * progress if avg_volume20 else None
    time_adjusted_volume_ratio = (current_volume / expected_volume_so_far) if expected_volume_so_far else raw_volume_ratio

    # 売買代金倍率（可能なら）：過去20営業日平均売買代金との比較。当日売買代金はStage1のturnover
    # （current×当日出来高）をそのまま使う（新規API不要）。
    avg_turnover20 = None
    turnover_ratio = None
    if avg_volume20:
        # 過去20日分の終値×出来高の平均で近似（日次の高値/安値までは使わず、既存データだけで
        # 完結させる）。
        past_turnovers = [closes[i] * volumes[i] for i in range(-21, -1)]
        avg_turnover20 = sum(past_turnovers) / len(past_turnovers) if past_turnovers else None
        turnover = stage1_row.get("turnover")
        turnover_ratio = (turnover / avg_turnover20) if (turnover and avg_turnover20) else None

    # distanceFromHigh・break status：AUTO_PULLBACKと同じ3か月（VOLUME_HIGH_LOOKBACK_DAYS）の
    # 戻り高値を参照する。
    lookback_window = highs[-(VOLUME_HIGH_LOOKBACK_DAYS + 1):-1] if len(highs) >= VOLUME_HIGH_LOOKBACK_DAYS + 1 else highs[:-1]
    recent_high = max(lookback_window) if lookback_window else highs[-2]
    distance_from_high_pct = ((current - recent_high) / recent_high * 100) if recent_high else None
    above_recent_high = bool(recent_high and current > recent_high)

    # Falling Knife/REVERSAL同様の「当日安値更新中」「強いgap down」判定（NEGATIVE_VOLUME判定に使う）。
    recent_low_window = lows[-21:-1]
    making_new_low_today = bool(recent_low_window and lows[-1] <= min(recent_low_window))
    gap_down = bool(len(closes) >= 2 and closes[-2] and opens[-1] < closes[-2] * 0.97 and current < closes[-2])

    return {"avgVolume20": round(avg_volume20, 0) if avg_volume20 else None,
            "currentVolume": current_volume,
            "rawVolumeRatio": round(raw_volume_ratio, 2) if raw_volume_ratio is not None else None,
            "timeAdjustedVolumeRatio": round(time_adjusted_volume_ratio, 2) if time_adjusted_volume_ratio is not None else None,
            "isIntradayVolume": is_intraday, "timeProgressRatio": round(progress, 3),
            "avgTurnover20": round(avg_turnover20, 0) if avg_turnover20 else None,
            "turnoverRatio": round(turnover_ratio, 2) if turnover_ratio is not None else None,
            "distanceFromHighPct": round(distance_from_high_pct, 2) if distance_from_high_pct is not None else None,
            "aboveRecentHigh": above_recent_high,
            "makingNewLowToday": making_new_low_today, "gapDown": gap_down}


def _volume_type(stage2, row):
    """出来高急増を伴う資金の向きを分類する。POSITIVE_VOLUME（資金流入を伴う上昇）／
    NEGATIVE_VOLUME（出来高急増を伴う下落）／CLIMAX_UP・CLIMAX_DOWN（極端な出来高急増＋
    急騰/急落）／NEUTRAL_VOLUME（出来高は増えているが方向不明）。出来高増加＝買い、ではない
    というユーザー方針を反映する。"""
    day_change = row.get("changePct")
    tavr = stage2.get("timeAdjustedVolumeRatio")
    if day_change is None or tavr is None:
        return "NEUTRAL_VOLUME"
    if day_change >= 10 and tavr >= 5:
        return "CLIMAX_UP"
    if day_change <= -8 and tavr >= 3:
        return "CLIMAX_DOWN"
    if (day_change < -3 and tavr >= 1.5) or (stage2.get("makingNewLowToday") and tavr >= 1.5) or (stage2.get("gapDown") and tavr >= 1.5):
        return "NEGATIVE_VOLUME"
    if day_change > 1 and tavr >= 1.3:
        return "POSITIVE_VOLUME"
    return "NEUTRAL_VOLUME"


def _volume_final_score(row, stage2):
    """VOLUME_SCORE（100点満点、初期配点）：
      Time-adjusted Volume Ratio   最大30点（1.0倍で0点、3.0倍以上で満点）
      Turnover/Liquidity           最大20点
      Price Direction              最大15点（上昇のみ加点、下落は0点＝下のNEGATIVE_VOLUME
                                    判定と合わせて二重に評価する）
      High Retention               最大15点（0.7未満で0点、0.99以上で満点）
      Market RS                    最大10点
      Sector RS                    最大10点
    NEGATIVE_VOLUME／CLIMAX_DOWNは「出来高急増を伴う下落」であり買い候補ではないため、
    CURRENT登録対象から除外する（Noneを返す。ただしStage1候補には残るためallCandidatesには
    出ない＝ユーザー指示の「異常出来高としての警戒タグ表示」は将来拡張として見送り、今回は
    除外のみ実装）。"""
    if stage2 is None:
        return None
    volume_type = _volume_type(stage2, row)
    if volume_type in ("NEGATIVE_VOLUME", "CLIMAX_DOWN"):
        return None
    tavr = stage2.get("timeAdjustedVolumeRatio")
    score = 0.0
    score += _scale_score(tavr, 1.0, 3.0, 30)
    score += _scale_score(row.get("turnover"), 3e8, 1e10, 20)
    day_change = row.get("changePct")
    if day_change is not None and day_change > 0:
        score += _scale_score(day_change, 0, 8, 15)
    score += _scale_score(row.get("highRetention"), 0.7, 0.99, 15)
    score += _scale_score(row.get("marketRS"), 0, 5, 10)
    score += _scale_score(row.get("sectorRS"), 0, 3, 10)
    score *= _liquidity_multiplier(row.get("turnover"))
    return round(score, 1)


def run_volume_scan(database_url, user_id, force=False):
    """Stage1（キャッシュ共有）→VOLUME候補選定→Stage2（並列、他エンジンと同じ構成）→
    VOLUME_SCORE確定（NEGATIVE_VOLUME/CLIMAX_DOWNは除外）→閾値を満たした上位
    VOLUME_MAX_REGISTER件を「AUTO_VOLUME_CURRENT」として登録、新TOP外に落ちた旧CURRENTは
    「AUTO_VOLUME_SEEN」へ降格。"""
    stage1 = run_momentum_stage1(force=force)
    candidates = select_volume_stage1_candidates(stage1)
    t_stage2_start = time.time()

    stage2_results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=VOLUME_STAGE2_WORKERS) as ex:
        futures = {ex.submit(_volume_stage2_detail, code, row): code for _, code, row in candidates}
        for fut in concurrent.futures.as_completed(futures):
            code = futures[fut]
            try:
                stage2_results[code] = fut.result()
            except Exception as e:
                print("  VOLUME Stage2詳細取得失敗", code, e)
                stage2_results[code] = None
    stage2_duration = round(time.time() - t_stage2_start, 1)

    details = []
    for lite_score, code, row in candidates:
        stage2 = stage2_results.get(code)
        final_score = _volume_final_score(row, stage2)
        if final_score is None:
            continue
        details.append({"code": code, "name": row.get("name"), "sector": row.get("sector"),
                         "changePct": row.get("changePct"), "highRetention": row.get("highRetention"),
                         "marketRS": row.get("marketRS"), "sectorRS": row.get("sectorRS"),
                         "turnover": row.get("turnover"), "liteScore": lite_score,
                         "current": row.get("current"),
                         "finalScore": final_score, "stage2": stage2,
                         "volumeType": _volume_type(stage2, row)})
    details.sort(key=lambda d: -d["finalScore"])
    to_register = [d for d in details if d["finalScore"] >= VOLUME_SCORE_THRESHOLD][:VOLUME_MAX_REGISTER]

    registered, demoted = [], []
    if database_url and investment_db is not None:
        old_current = investment_db.get_codes_with_auto_tag(database_url, user_id, "AUTO_VOLUME_CURRENT", market="JP")
        old_seen = investment_db.get_codes_with_auto_tag(database_url, user_id, "AUTO_VOLUME_SEEN", market="JP")
        new_current_codes = {d["code"] for d in to_register}
        now_dt = datetime.datetime.now(datetime.timezone.utc)
        expires_at = (now_dt + datetime.timedelta(days=VOLUME_TAG_EXPIRE_DAYS)).isoformat()

        for d in to_register:
            tag_value = {"score": d["finalScore"], "volumeType": d["volumeType"],
                         "rawVolumeRatio": d["stage2"].get("rawVolumeRatio"),
                         "timeAdjustedVolumeRatio": d["stage2"].get("timeAdjustedVolumeRatio"),
                         "isIntradayVolume": d["stage2"].get("isIntradayVolume"),
                         "addedAt": now_dt.isoformat(), "expiresAt": expires_at}
            try:
                ok = investment_db.auto_register_or_tag_watchlist_item(
                    database_url, user_id, d["code"], "JP", "AUTO_VOLUME_CURRENT", tag_value,
                    item_fields={"name": d["name"], "sector": d["sector"], "source": "auto_volume"})
                investment_db.remove_auto_tag_key(database_url, user_id, d["code"], "JP", "AUTO_VOLUME_SEEN")
            except Exception as e:
                print("  AUTO_VOLUME登録（CURRENT）失敗", d["code"], e)
                ok = False
            if ok:
                registered.append(d)

        for code in old_current - new_current_codes:
            try:
                investment_db.auto_register_or_tag_watchlist_item(
                    database_url, user_id, code, "JP", "AUTO_VOLUME_SEEN",
                    {"addedAt": now_dt.isoformat(), "expiresAt": expires_at})
                investment_db.remove_auto_tag_key(database_url, user_id, code, "JP", "AUTO_VOLUME_CURRENT")
                demoted.append(code)
            except Exception as e:
                print("  AUTO_VOLUME降格（SEEN化）失敗", code, e)

        def _volume_metadata(d):
            s2 = d["stage2"]
            return {"rawVolumeRatio": s2.get("rawVolumeRatio"), "timeAdjustedVolumeRatio": s2.get("timeAdjustedVolumeRatio"),
                    "avgVolume20": s2.get("avgVolume20"), "currentVolume": s2.get("currentVolume"),
                    "turnover": d.get("turnover"), "turnoverRatio": s2.get("turnoverRatio"),
                    "highRetention": d.get("highRetention"), "dayChangePct": d.get("changePct"),
                    "distanceFromHighPct": s2.get("distanceFromHighPct"), "aboveRecentHigh": s2.get("aboveRecentHigh"),
                    "volumeType": d["volumeType"], "isIntradayVolume": s2.get("isIntradayVolume"),
                    "timeProgressRatio": s2.get("timeProgressRatio")}
        _record_signal_transitions(database_url, user_id, "VOLUME", "JP",
            to_register, old_current, old_seen, demoted, metadata_fn=_volume_metadata)
    return {
        "stage1CodesScanned": stage1["codesScanned"], "stage1PricesReturned": stage1["pricesReturned"],
        "stage1DurationSec": stage1["durationSec"], "stage1RequestCount": stage1["requestCount"],
        "stage1CacheAgeSec": round(time.time() - stage1["builtAt"], 1) if stage1["builtAt"] else None,
        "stage1BuiltAtJst": _jst_time_str(stage1["builtAt"]),
        "stage1ScanFailed": stage1.get("scanFailed", False), "stage1UsedStaleCache": stage1.get("usedStaleCache", False),
        "stage1CandidateCount": len(candidates),
        "stage2ValidCount": len(details),
        "stage2DurationSec": stage2_duration,
        "demotedToSeenCount": len(demoted), "demotedToSeen": demoted,
        "registeredCount": len(registered),
        "registered": registered,
        "allCandidates": details,
        "marketTimeProgressRatio": round(_market_time_progress_ratio(), 3),
    }


# 2026-09-07新規（ポジション→リアルタイム売却判断画面 Phase1）：ポジション詳細エリアを開いた
# 1銘柄だけをオンデマンドで取得する専用API。既存のget_stock_quotes（yfinance＋立花証券API
# オーバーレイ、新規取得ロジックなし）と、AUTO_VOLUMEの_volume_stage2_detail/_volume_type
# （出来高の方向判定、新規スキャンなし・Stage1キャッシュも使わず単体銘柄のみ計算）をそのまま
# 再利用する。ポジション本体（entries/trade_history/実現損益）には一切書き込まない、
# 読み取り専用の参考情報API。失敗しても売買機能自体には影響しない設計にする。
def get_position_live_detail(code, market="JP"):
    quotes = get_stock_quotes([{"code": code, "market": market}])
    quote = quotes.get(code)
    if not quote:
        return {"error": "現在値を取得できませんでした（データ取得中か、対象外銘柄の可能性があります）"}
    result = dict(quote)
    result["code"] = code
    result["market"] = market
    jst = datetime.timezone(datetime.timedelta(hours=9))
    result["fetchedAtJst"] = datetime.datetime.now(jst).strftime("%H:%M:%S")
    if market == "JP":
        try:
            t, p = quote.get("t"), quote.get("p")
            change_pct = ((t - p) / p * 100) if (t is not None and p) else None
            row = {"changePct": change_pct, "turnover": quote.get("turnover"),
                   "highRetention": None, "marketRS": None, "sectorRS": None}
            stage2 = _volume_stage2_detail(code, row)
            if stage2:
                result["volumeDetail"] = {
                    "avgVolume20": stage2.get("avgVolume20"),
                    "rawVolumeRatio": stage2.get("rawVolumeRatio"),
                    "timeAdjustedVolumeRatio": stage2.get("timeAdjustedVolumeRatio"),
                    "volumeType": _volume_type(stage2, row),
                }
        except Exception as e:
            print("  ポジション・リアルタイム：出来高詳細の取得に失敗（現在値表示は継続）", code, e)
    return result


# 2026-09-07新規（ポジション→リアルタイム売却判断画面 Phase2）：ポジション詳細エリアの
# 5分足チャート専用API。get_position_live_detailとは別関数にし、価格・出来高判定（Phase1）と
# チャート取得（Phase2）を分離したまま呼び出せるようにする（フロント側も個別にpollingできる）。
# 2026-09-09更新（監視銘柄/市場チャートの時間足切替）：periodを追加。省略時は従来通り
# "1d"（ポジション画面の既存呼び出しは無変更のまま動く）。監視銘柄/日本市場一覧等の共通
# チャートモーダルは、この同じAPI・同じ関数をperiod指定付きで再利用する（新しい取得経路を
# 作らない、指示書「共通チャート取得関数を利用」対応）。
INTRADAY_CHART_ALLOWED_PERIODS = {"1d", "5d", "1mo", "3mo"}


def get_position_intraday_chart(code, market="JP", interval="5m", period=None):
    symbol = _yf_symbol({"code": code, "market": market})
    if period is not None and period not in INTRADAY_CHART_ALLOWED_PERIODS:
        period = None  # 未知のperiodは無視して既定値(1d)にフォールバック（不正値を素通ししない）
    bars = _fetch_intraday_bars(symbol, interval, period)
    if not bars:
        return {"bars": [], "interval": interval,
                "error": "指定期間の短期足データを取得できませんでした（市場時間外、または対象外銘柄の可能性があります）"}
    return {"bars": bars, "interval": interval}


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
    # 2026-09-08新規（分析カードUI改善、指示書SHORT_TERM_BREAKOUT_PRICE対応）：上のresistance
    # （3か月＝60営業日の終値高値）は詳細情報向けの中期参照値としてそのまま維持しつつ、
    # 短期トレード判断向けに別途「短期ブレイク価格」を算出する。直近5営業日高値から順に
    # 10営業日・20営業日高値を試し、現在値超〜+10%以内に収まる最も近い候補を採用する
    # （前日高値・当日高値は必ずこの5〜20営業日の窓に含まれるため別枠で拾う必要はない）。
    # どの窓でも現在値超の候補が無い、または最も近い候補すら+10%を超える場合はNone
    # （無理にブレイク価格を作らない、というユーザー指定の方針）。
    short_term_breakout = None
    if current and highs:
        for lb_days in (5, 10, 20):
            window = highs[-min(lb_days, len(highs)):]
            if len(window) < 2:
                continue
            candidate = max(window)
            if candidate <= current:
                continue  # 既にその窓の高値を上抜け済み＝この窓ではブレイク待ちの水準がない
            deviation_pct = (candidate - current) / current * 100
            if deviation_pct <= 10:
                short_term_breakout = candidate
                break  # 5→10→20の順に試しているため、最初に条件を満たした時点が最も近い候補
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

    # ---- v3-8 Step5（ポジション管理：DAY/SWING別チャート崩れ判定の材料）：直近10営業日
    # （当日を除く）の安値＝「直近スイング安値」の簡易版。新規API呼び出しはせず、この関数内で
    # 既に取得済みのlows配列を再利用する。----
    swing_low_recent = min(lows[-11:-1]) if len(lows) >= 11 else (min(lows[:-1]) if len(lows) >= 2 else None)

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

    # ---- v3-8 Step6（ポジション管理：出来高複合判定）：出来高単独では売買判断の根拠にせず、
    # 必ず価格の動き（下落／高値圏からの反落／ブレイク失敗）と組み合わせた複合条件でのみフラグを
    # 立てる。新規API呼び出しなし（既存のvol_surge/high_zone/breakout_lookback_high/change_pct/
    # hi・c・rngを再利用）。ポジション管理側（derivePositionStatus）でもCAUTION/REDUCE/EXIT等の
    # 判断材料の一つとして合算するのみで、この複合フラグ単独で撤退等を決めることはしない。
    # ①出来高増加＋下落：出来高急増を伴って当日下落＝売り優勢（分配の可能性）。
    volume_up_with_decline = bool(vol_surge and change_pct is not None and change_pct < 0)
    # ②高値圏＋大出来高＋反落：52週高値圏を維持しつつ出来高急増があったのに、終値が当日高値から
    # 大きく戻された＝高値圏での天井圏売り抜けの可能性。
    high_zone_volume_reversal = bool(high_zone and vol_surge and c <= hi - rng * 0.3)
    # ③ブレイク失敗＋出来高増加：直近ブレイク水準（breakout_lookback_high、既存のブレイクアウト
    # entry採用ロジックと同一の値）に当日高値でタッチ／上抜けしたが、出来高急増を伴いながら
    # 終値はその水準を割り込んで引けた＝ブレイクの失敗（だまし）。
    failed_breakout_with_volume = bool(breakout_lookback_high is not None and vol_surge
                                        and hi >= breakout_lookback_high and c < breakout_lookback_high)

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
        # 2026-09-08新規（分析カードUI改善）：短期（5〜20営業日）ブレイク価格。indicators.resistance
        # （3か月＝60営業日高値、詳細情報向け）とは別の、意思決定カード表示専用の値。
        "shortTermBreakout": round(short_term_breakout, 1) if short_term_breakout is not None else None,
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
        # v3-8 Step3・Step5：ポジション管理（チャート崩れ判定）向けの軽量フラグ・水準。
        # 新規API呼び出しなし（この関数内で既に計算済みの値をそのまま返すだけ）。
        "positionFlags": {
            "longBearishCandle": long_bearish_candle,
            "gapUpFailure": gap_up_failure,
            # Step5：DAY重視＝直近ブレイク水準・5分足直近安値、SWING重視＝直近スイング安値・
            # 5日線・25日線（5日線・25日線はindicators.ma5Daily/ma25を流用、ここでは重複させない）。
            "recentLow5m": round(recent_low_5m, 2) if recent_low_5m is not None else None,
            "breakoutLookbackHigh": round(breakout_lookback_high, 2) if breakout_lookback_high is not None else None,
            "swingLowRecent": round(swing_low_recent, 2) if swing_low_recent is not None else None,
            # Step6：出来高複合判定（出来高単独では立てない、価格の動きとの組み合わせのみ）。
            "volumeUpWithDecline": volume_up_with_decline,
            "highZoneVolumeReversal": high_zone_volume_reversal,
            "failedBreakoutWithVolume": failed_breakout_with_volume,
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


# ============================================================
# Unified Smart Import（SmartImportEngine）。2026-09-10新規、Phase SI-A。
# 目的：カタリスト・有識者意見・イベント等、入力経路ごとに異なるJSON schemaをユーザーが
# 意識しなくても、「文章・JSON・箇条書き・記事本文・ChatGPT出力」をそのまま貼るだけで
# 自動判別・分類・プレビュー・保存できるようにする。指示書の実装順どおりPhase SI-Aでは
# 共通classify/normalize基盤＋CATALYST／EVENT／EXPERT_OPINIONの3カテゴリのみ実装する
# （MARKET_ANALYSIS・MORNING_MARKET_CHECK・INTRADAY_REPORT・TRADE_RULE・WATCHLIST_UPDATE・
# POSITION_UPDATE・NEWSはPhase SI-B/C予定、現時点ではUNKNOWNに分類され保存されない）。
# 既存のimport_news_catalysts()・import_market_events()・import_expert_views()
# （investment_db.py）をそのまま呼び出し、保存ロジックを重複実装しない（指示書33番）。
# ============================================================

SMART_IMPORT_CATEGORIES = [  # 指示書2番。将来カテゴリ追加時はここへ追加するだけでよい構造
    "CATALYST", "EXPERT_OPINION", "EVENT", "MARKET_ANALYSIS", "MORNING_MARKET_CHECK",
    "INTRADAY_REPORT", "TRADE_RULE", "NEWS", "WATCHLIST_UPDATE", "POSITION_UPDATE", "UNKNOWN",
]
# Phase SI-B（2026-09-10）で MARKET_ANALYSIS・MORNING_MARKET_CHECK・INTRADAY_REPORT・
# TRADE_RULEを追加。CHATGPT_LEGACYはSMART_IMPORT_CATEGORIESには含めない内部専用カテゴリ
# （指示書1番：既存ChatGPT統合連携のtype=trading_log的な旧schemaを検出した場合に、既存の
# save_chatgpt_unified_import()へそのまま委譲するための経路。UIのカテゴリ選択肢にも出さない）。
# Phase SI-C（2026-09-10）でWATCHLIST_UPDATE・POSITION_UPDATEを追加。NEWSは引き続き未実装
# （既存ニュースタブのRSS取得と役割が重複するため、指示書のスコープではPhase SI-C対象外）。
SMART_IMPORT_IMPLEMENTED_CATEGORIES = {
    "CATALYST", "EVENT", "EXPERT_OPINION", "MARKET_ANALYSIS", "MORNING_MARKET_CHECK",
    "INTRADAY_REPORT", "TRADE_RULE", "CHATGPT_LEGACY", "WATCHLIST_UPDATE", "POSITION_UPDATE",
    "SOCIAL_IMAGE_ANALYSIS",
}
# 指示書16番（Phase SI-C）：重要操作（TRADE_RULE・WATCHLIST_UPDATEのREMOVE・POSITION_UPDATE
# 全般）は「安全な項目のみ選択」の対象外とする。POSITION_UPDATEはカテゴリ全体が対象外
# （指示書6番：HIGH confidenceでも自動保存禁止、常に確認）。SOCIAL_IMAGE_ANALYSISは既存投稿
# への追記のみ（売買・ルールに直接影響しない）のため安全側に含める。
SMART_IMPORT_SAFE_CATEGORIES_BACKEND = {"CATALYST", "EVENT", "EXPERT_OPINION", "MARKET_ANALYSIS",
                                          "MORNING_MARKET_CHECK", "INTRADAY_REPORT", "SOCIAL_IMAGE_ANALYSIS"}

_JSON_CODEBLOCK_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


def _extract_json_candidates(raw_text):
    """raw_textから解析可能なJSON（正規JSON全体・```json コードブロック内・文中に埋め込まれた
    最初のbalanced {...}/[...]断片）を取り出す（指示書3・4番STEP1）。パースできなければ
    Noneを返すだけで例外は投げない（指示書30番：JSON parse失敗をエラー扱いにしない）。"""
    text = (raw_text or "").strip()
    if not text:
        return None
    candidates_text = [text]
    for m in _JSON_CODEBLOCK_RE.finditer(text):
        candidates_text.append(m.group(1).strip())
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = text.find(open_ch)
        if start != -1:
            end = text.rfind(close_ch)
            if end > start:
                candidates_text.append(text[start:end + 1])
    for t in candidates_text:
        if not t:
            continue
        try:
            return json.loads(t)
        except Exception:
            continue
    return None


# INTRADAY_REPORT・EVENT等の時刻文字列（"09:30"等）からreport_typeを推定するための対応表
# （指示書6番：INTRADAY_REPORT_SNAPSHOT_TIMESの逆引き。時刻の断定表記が無ければ推定しない）。
_SMART_IMPORT_TIME_TO_REPORT_TYPE = {v: k for k, v in INTRADAY_REPORT_SNAPSHOT_TIMES.items()}


def _looks_like_legacy_chatgpt_payload(item):
    """既存ChatGPT統合連携（date/market/watchlist/decisions/rule_updates、
    classify_chatgpt_unified_payload()が前提とするTRADING_LOG形式）のJSONかどうかを
    フィールド形状で判定する（指示書1番：優先順位1＝既知JSON schema、2＝既存ChatGPT
    parserを再利用する経路）。"""
    if not isinstance(item, dict):
        return False
    list_keys = ("watchlist", "decisions", "rule_updates", "events", "news", "expert_opinions", "catalysts")
    return "date" in item and any(isinstance(item.get(k), list) for k in list_keys)


def _classify_json_item(item):
    """dict1件をカテゴリへ分類する（フィールド形状ベース、指示書4番STEP2・14番：既知typeを
    最優先）。既存の各schemaのキー名をそのまま使い、新しいschemaは増やさない。"""
    if not isinstance(item, dict):
        return "UNKNOWN", "LOW", {}
    t = str(item.get("type") or "").strip().lower()

    # 指示書14番：既知typeがあれば自然文解析より最優先。type=trading_log等の旧ChatGPT形式は
    # 既存パーサーへ委譲する（指示書1番の優先順位1・2）。
    if t == "morning_market_check" or ("watchlist_top5" in item and "market_regime" in item and t != "market_intraday_report"):
        return "MORNING_MARKET_CHECK", "HIGH", item
    if t in ("market_intraday_report", "daily_market_review"):
        draft = dict(item)
        if t == "daily_market_review" and not draft.get("report_type"):
            draft["report_type"] = "MARKET_CLOSE"  # 指示書5番：daily_market_reviewは大引け相当
        rt = draft.get("report_type")
        confidence = "HIGH" if rt in INTRADAY_REPORT_SNAPSHOT_TIMES else "MEDIUM"
        return "INTRADAY_REPORT", confidence, draft
    if t == "market_analysis":
        return "MARKET_ANALYSIS", "HIGH", item
    if t == "trade_rule":
        return "TRADE_RULE", "HIGH", item
    if t == "watchlist_update":
        # 指示書2番（Phase SI-C）：REMOVEは重要操作のためconfidenceを上げすぎない（UIの既定
        # 未選択はaction自体で別途判定するが、ここでも情報として下げておく）。
        return "WATCHLIST_UPDATE", ("MEDIUM" if str(item.get("action", "")).upper() == "REMOVE" else "HIGH"), item
    if t == "position_update":
        return "POSITION_UPDATE", "HIGH", item
    if t == "social_market_image_analysis":
        # 2026-09-10新規（にこそく画像解析待ちキュー）：post_idが無い場合は保存できないため
        # confidenceを下げる（他のtype判定と同じ「必須項目が無ければ下げる」方針）。
        return "SOCIAL_IMAGE_ANALYSIS", ("HIGH" if item.get("post_id") else "LOW"), item
    if _looks_like_legacy_chatgpt_payload(item):
        return "CHATGPT_LEGACY", "HIGH", item

    if t in ("catalyst", "news_catalyst"):
        return "CATALYST", "HIGH", item
    if t in ("event", "market_event"):
        return "EVENT", "HIGH", item
    if t in ("expert_opinion", "expert_view"):
        return "EXPERT_OPINION", "HIGH", item
    if "expert_name" in item and ("published_at" in item or "date" in item):
        return "EXPERT_OPINION", ("HIGH" if item.get("published_at") else "MEDIUM"), item
    if "title" in item and ("catalyst_date" in item or "category" in item):
        return "CATALYST", ("HIGH" if item.get("catalyst_date") else "MEDIUM"), item
    if ("event_date" in item or "event" in item) and ("title" in item or "event" in item):
        return "EVENT", ("HIGH" if item.get("event_date") else "MEDIUM"), item
    return "UNKNOWN", "LOW", item


# 指示書9番のイベント種別を中心とした簡易キーワード分類（AI不使用、他のAUTO系エンジンと
# 同じ方針）。日付が併記されているかどうかでconfidenceを分ける（指示書21番：勝手に日付を
# 補完しない、無ければMEDIUM/LOWに留める）。
_SMART_IMPORT_EVENT_KEYWORDS = ["FOMC", "日銀", "日銀会合", "金融政策決定会合", "CPI", "PPI",
    "雇用統計", "GDP", "SQ", "MSQ", "決算発表", "決算", "要人発言", "製品発表", "ロックアップ解除"]
_SMART_IMPORT_DATE_PATTERNS = [
    re.compile(r"\d{4}年\d{1,2}月\d{1,2}日"), re.compile(r"\d{1,2}月\d{1,2}日"),
    re.compile(r"\d{1,2}/\d{1,2}"), re.compile(r"今日|明日|明後日|今週|来週|今月|来月"),
]
# 指示書8番：話者名＋意見動詞の組み合わせで有識者意見を検出する。事実の羅列（指示書22番）と
# 区別するため、名前だけ・動詞だけでは判定しない（両方揃って初めてEXPERT_OPINION）。
_SMART_IMPORT_EXPERT_NAME_RE = re.compile(r"([一-龠ぁ-んァ-ヶー]{2,8}(?:氏|さん|アナリスト))")
_SMART_IMPORT_OPINION_VERBS = ["との見方", "との見解", "と述べ", "と分析", "とコメント", "予想",
    "慎重", "強気", "弱気", "有効", "注目", "示唆", "と語った", "とみる"]
_SMART_IMPORT_CATALYST_KEYWORDS = [
    "新製品", "新サービス", "大型受注", "提携", "業務提携", "資本提携", "協業", "事業化",
    "量産開始", "量産化", "採用決定", "上方修正", "自社株買い", "増配", "下方修正", "減配",
    "不祥事", "事故", "訴訟", "公募増資", "希薄化", "売出し", "出荷", "受注", "サンプル出荷",
    "材料",  # 「全固体電池材料で強い」等、日本の相場でよく使われる「好材料/悪材料」の略称
]
# 2026-09-10修正：\bは日本語文字を\w扱いする（Unicode既定）ため「7203を」のような直後に
# 日本語が続く場合に境界と判定されず一致しない不具合があった。桁の前後に別の数字が
# 続かないことだけを見るnegative lookaround（(?<!\d)/(?!\d)）に変更する。
_SMART_IMPORT_STOCK_CODE_RE = re.compile(r"(?<!\d)(\d{4})(?!\d)")

# 指示書7番（Phase SI-B）：売買ルール変更・追加の検出。単なる「〜しない」等の一般的な否定文
# まで拾うと誤検出が多いため、明確なルール宣言語＋トレード行動語の組み合わせでのみ検出する
# （指示書8・9番：TRADE_RULEは自動保存禁止・position_risk_rulesは直接上書きしない前提）。
_SMART_IMPORT_RULE_DECLARATION_KEYWORDS = ["禁止", "必ず", "例外なく", "例外なし", "一旦売却",
    "厳守", "徹底する", "ルールとする", "ルール化", "変更する", "へ変更"]
_SMART_IMPORT_RULE_ACTION_KEYWORDS = ["売却", "エントリー", "スイング", "建てる", "損切り",
    "利確", "持ち越し", "デイトレ", "ポジション", "EXIT"]
_SMART_IMPORT_RULE_PCT_RE = re.compile(r"([-+]?\d+(?:\.\d+)?)\s*%")
# 指示書9番：position_risk_rules（-6 WATCH/-7 WARNING/-8 EXIT）に関わる変更提案かどうかの
# 簡易判定。数値そのものの意味は判断せず「損切り/EXIT関連の語＋%」があれば警告フラグを立てる
# だけに留める（閾値の意味解釈や自動比較はしない、安全側）。
_SMART_IMPORT_RISK_RULE_KEYWORDS = ["EXIT", "損切り", "-8%", "-8％"]

# ============================================================
# Phase SI-C（2026-09-10）：WATCHLIST_UPDATE・POSITION_UPDATEの検出。日常的な短文
# （「JX3810で100株買い」「逆指値3820に上げた」「半分利確」等）を理解できるようにする
# （指示書8番）。ただし対象銘柄・数量・価格に確信が持てない場合はnull/LOW confidenceに
# 倒し、勝手に確定しない（指示書3・10・8・9番）。
# ============================================================
_SMART_IMPORT_WATCHLIST_ADD_KEYWORDS = ["監視銘柄に追加", "監視に追加", "ウォッチに追加", "監視銘柄へ追加", "監視リストに追加"]
_SMART_IMPORT_WATCHLIST_REMOVE_KEYWORDS = ["監視銘柄から削除", "監視から外す", "監視解除", "監視銘柄から外す", "監視リストから削除"]
_SMART_IMPORT_THEME_RE = re.compile(r"テーマは(.+?)(?:。|$)")
_SMART_IMPORT_NAME_HINT_RE = re.compile(r"^([一-龠ぁ-んァ-ヶーA-Za-z0-9]{2,20}?)を?(?:監視銘柄|監視|ウォッチ)")

_SMART_IMPORT_BUY_RE = re.compile(r"(\d{2,6}(?:\.\d+)?)\s*円\s*で\s*(\d{1,6}(?:\.\d+)?)\s*株\s*(?:買|購入)")
_SMART_IMPORT_SELL_WITH_QTY_RE = re.compile(r"(\d{2,6}(?:\.\d+)?)\s*円\s*で\s*(\d{1,6}(?:\.\d+)?)\s*株\s*(?:売却|売り|売った)")
# 指示書8番の例文「JX3850で売れた」は「円」が省略されるため、円を任意とする。
_SMART_IMPORT_SELL_PRICE_ONLY_RE = re.compile(r"(\d{2,6}(?:\.\d+)?)\s*円?\s*で\s*(?:売れた|売却|売り)")
_SMART_IMPORT_ADD_SHARES_RE = re.compile(r"(\d{1,6}(?:\.\d+)?)\s*株\s*(?:追加|買い増し|買い足し)")
# 「3900円で全部売却」のように「円で」と決済キーワードの間に「全部」等が挟まり上記の厳密な
# パターンにマッチしない場合の汎用フォールバック（半分利確/全部売却と価格の組み合わせ用）。
_SMART_IMPORT_ANY_PRICE_RE = re.compile(r"(\d{2,6}(?:\.\d+)?)\s*円")
_SMART_IMPORT_STOP_RE = re.compile(r"逆指値\s*(\d{2,6}(?:\.\d+)?)")
_SMART_IMPORT_HALF_KEYWORDS = ["半分利確", "半分売却", "半分だけ売却", "半分手仕舞い", "半分だけ利確"]
_SMART_IMPORT_SAME_PRICE_EXIT_KEYWORDS = ["同値撤退", "同値で撤退", "同値決済", "同値で手仕舞い"]
_SMART_IMPORT_FULL_EXIT_KEYWORDS = ["全部売却", "全株売却", "全部売った", "全株売った", "全て売却", "すべて売却"]
_SMART_IMPORT_STOPLOSS_KEYWORDS = ["損切り"]
_SMART_IMPORT_HOLD_KEYWORDS = ["持ち越し"]
_SMART_IMPORT_SKIP_KEYWORDS = ["見送り"]
_SMART_IMPORT_BUY_WORD = "買"
_SMART_IMPORT_SELL_WORD_RE = re.compile(r"売")


def _name_key(text):
    """銘柄名の表記揺れを軽減するための正規化キー（指示書3番：完全なエイリアス辞書は
    持たないため、あくまで簡易な語尾統一・空白除去のみのベストエフォート）。
    「パナソニックホールディングス」「パナソニックHD」は一致させられるが、
    「パナHD」のような大幅な略称までは解決できない既知の制約。"""
    if not text:
        return ""
    key = text.strip()
    for a, b in (("ホールディングス", "HD"), ("株式会社", ""), ("（株）", ""), (" ", ""), ("　", "")):
        key = key.replace(a, b)
    return key.upper()


def _resolve_ticker_from_text(database_url, user_id, text, hint_code=None):
    """テキストから銘柄コードを解決する（指示書3番の優先順位：①明示コード②現在Watchlist
    ③Positions④銘柄マスター、news_catalysts・全文検索まではPhase SI-Cでは実装しない
    既知の制約）。確信が持てなければ(None, "LOW")を返し、勝手に推定しない。
    hint_code：同一raw_text内の直前チャンクで既に解決済みのコード（指示書9番：
    「逆指値3820に上げた」等、名前が省略された文への継承に使う）。
    2026-09-10修正：POSITION_UPDATE/WATCHLIST_UPDATEの短文では「3810円」「3850で」
    「100株」「逆指値3820」等の価格・株数・逆指値の4桁数字が銘柄コードと誤認されやすい
    （「円」「株」を伴わない裸の数字表現もあるため、記号での除外だけでは不十分）。そのため
    優先順位を「①現在Watchlist②Positions③明示コード④銘柄マスター」に変更し、まず社名
    ベースで解決を試みてから、社名が見つからない場合のみ裸の4桁数字をコードとして扱う。"""
    if investment_db is not None and database_url:
        text_key = _name_key(text)
        try:
            for w in investment_db.list_watchlist(database_url, user_id, market="JP") or []:
                name = w.get("name")
                if name and _name_key(name) and _name_key(name) in text_key:
                    return w.get("code"), "HIGH"
        except Exception:
            pass
        try:
            for p in investment_db.list_portfolio(database_url, user_id) or []:
                name = p.get("name")
                if name and _name_key(name) and _name_key(name) in text_key:
                    return p.get("code"), "HIGH"
        except Exception:
            pass
    # 社名で解決できなかった場合のみ、裸の4桁数字をコードとして扱う（「3810円」「100株」
    # 「逆指値3820」等の価格・株数・逆指値表現はスキップする）。
    for m in _SMART_IMPORT_STOCK_CODE_RE.finditer(text):
        if text[m.end():m.end() + 1] in ("円", "株"):
            continue
        if "逆指値" in text[max(0, m.start() - 4):m.start()]:
            continue
        return m.group(1), "HIGH"
    if investment_db is not None and database_url:
        try:
            master = get_jp_issue_master() or {}
            for code, info in master.items():
                name = (info or {}).get("name")
                if name and _name_key(name) and _name_key(name) in text_key:
                    return code, "MEDIUM"
        except Exception:
            pass
    if hint_code:
        return hint_code, "MEDIUM"  # 直前チャンクからの継承（名前省略文への対応、指示書9番）
    return None, "LOW"


def _classify_watchlist_or_position_chunk(chunk, database_url, user_id, hint_code=None, next_chunk=""):
    """自然文の1塊をWATCHLIST_UPDATE/POSITION_UPDATEへ分類する（指示書1・5番）。
    該当しなければNoneを返す（他のカテゴリの判定に回す）。next_chunkは「テーマは○○。」が
    別文として続く場合の補完に使う（指示書1番の例文どおり文が分かれるため、直後の1文だけ
    先読みする）。"""
    has_watch_add = any(kw in chunk for kw in _SMART_IMPORT_WATCHLIST_ADD_KEYWORDS)
    has_watch_remove = any(kw in chunk for kw in _SMART_IMPORT_WATCHLIST_REMOVE_KEYWORDS)
    if has_watch_add or has_watch_remove:
        ticker, conf = _resolve_ticker_from_text(database_url, user_id, chunk)
        name_match = _SMART_IMPORT_NAME_HINT_RE.match(chunk.strip())
        theme_match = _SMART_IMPORT_THEME_RE.search(chunk) or (next_chunk and _SMART_IMPORT_THEME_RE.search(next_chunk))
        action = "REMOVE" if has_watch_remove else "ADD"
        # 指示書2番：REMOVEは重要操作のため、tickerが解決できてもconfidenceをMEDIUM止まりにする
        confidence = "LOW" if ticker is None else ("MEDIUM" if action == "REMOVE" else conf)
        return "WATCHLIST_UPDATE", confidence, {
            "ticker": ticker, "name_hint": name_match.group(1) if name_match else None,
            "action": action, "themes": theme_match.group(1) if theme_match else None, "notes": chunk}

    buy_m = _SMART_IMPORT_BUY_RE.search(chunk)
    sell_qty_m = _SMART_IMPORT_SELL_WITH_QTY_RE.search(chunk)
    sell_price_only_m = _SMART_IMPORT_SELL_PRICE_ONLY_RE.search(chunk) if not sell_qty_m else None
    add_shares_m = _SMART_IMPORT_ADD_SHARES_RE.search(chunk)
    stop_m = _SMART_IMPORT_STOP_RE.search(chunk)
    has_half = any(kw in chunk for kw in _SMART_IMPORT_HALF_KEYWORDS)
    has_same_price_exit = any(kw in chunk for kw in _SMART_IMPORT_SAME_PRICE_EXIT_KEYWORDS)
    has_full_exit = any(kw in chunk for kw in _SMART_IMPORT_FULL_EXIT_KEYWORDS)
    has_stoploss = any(kw in chunk for kw in _SMART_IMPORT_STOPLOSS_KEYWORDS)
    # 「持ち越し」「見送り」はTRADE_RULEの行動語（_SMART_IMPORT_RULE_ACTION_KEYWORDS）とも
    # 重複するため、ルール宣言語（禁止/必ず等）が同じ文に含まれる場合はPOSITION_UPDATEの
    # HOLD_NOTEとして奪わず、TRADE_RULE側の判定に委ねる（例：「FOMCまでは持ち越し禁止」）。
    has_rule_decl_in_chunk = any(kw in chunk for kw in _SMART_IMPORT_RULE_DECLARATION_KEYWORDS)
    has_hold = any(kw in chunk for kw in _SMART_IMPORT_HOLD_KEYWORDS) and not has_rule_decl_in_chunk
    has_skip = any(kw in chunk for kw in _SMART_IMPORT_SKIP_KEYWORDS) and not has_rule_decl_in_chunk

    # 数値を伴う強いシグナル（買い/売り/株数/逆指値）は単独でも判定してよいが、キーワードのみの
    # 弱いシグナル（半分/同値撤退/全部売却/損切り/持ち越し/見送り）は、一般的な売買ルール文
    # （TRADE_RULE）等と紛れやすいため、対象銘柄が解決できる場合に限って採用する
    # （解決できなければNoneを返し、他の分類器に委ねる）。
    strong_signal = any([buy_m, sell_qty_m, sell_price_only_m, add_shares_m, stop_m])
    soft_signal = any([has_half, has_same_price_exit, has_full_exit, has_stoploss, has_hold, has_skip])
    if not strong_signal and not soft_signal:
        return None
    ticker, tconf = _resolve_ticker_from_text(database_url, user_id, chunk, hint_code=hint_code)
    if not strong_signal and soft_signal and ticker is None:
        return None
    draft = {"ticker": ticker, "notes": chunk}
    if buy_m:
        draft.update({"action": "OPEN", "entry_price": float(buy_m.group(1)), "quantity": float(buy_m.group(2))})
    elif sell_qty_m:
        draft.update({"action": "REDUCE", "exit_price": float(sell_qty_m.group(1)), "quantity": float(sell_qty_m.group(2))})
    elif sell_price_only_m:
        # 指示書10番：売却数量が特定できない場合はnullのまま（勝手にCLOSE確定しない）
        draft.update({"action": "REDUCE", "exit_price": float(sell_price_only_m.group(1)), "quantity": None,
                      "quantity_unresolved": True})
    elif add_shares_m:
        draft.update({"action": "ADD", "quantity": float(add_shares_m.group(1)), "entry_price": None})
    elif has_full_exit:
        price_m = _SMART_IMPORT_ANY_PRICE_RE.search(chunk)
        draft.update({"action": "CLOSE", "quantity_hint": "ALL",
                      "exit_price": float(price_m.group(1)) if price_m else None})
    elif has_half:
        price_m = _SMART_IMPORT_ANY_PRICE_RE.search(chunk)
        draft.update({"action": "REDUCE", "quantity_hint": "HALF",
                      "exit_price": float(price_m.group(1)) if price_m else None})
    elif has_same_price_exit:
        draft.update({"action": "CLOSE", "same_price_exit": True, "exit_price": None})
    elif has_stoploss:
        draft.update({"action": "CLOSE", "quantity_hint": "ALL", "exit_price": None, "is_stoploss": True})
    elif has_hold:
        draft.update({"action": "HOLD_NOTE"})
    elif has_skip:
        draft.update({"action": "HOLD_NOTE"})
    # UPDATE_STOPは他アクションと併記され得る（例：買い＋逆指値）ため、別チャンクとして
    # 追加検出はせずここではstop_priceを併記するだけに留める（複数銘柄操作の1文1候補の
    # 原則に合わせ、同一文中の買い+逆指値は1つのOPEN候補としてstop_priceも持たせる）。
    if stop_m and draft.get("action") not in ("OPEN", "ADD"):
        draft["action"] = "UPDATE_STOP"
    if stop_m:
        draft["stop_price"] = float(stop_m.group(1))
    if "action" not in draft:
        return None
    # confidence：ticker解決できないとLOW（指示書8番：対象特定不能ならLOW止まり）。
    # 数量不明の売却もLOW寄りに倒す（指示書10番）。
    if ticker is None:
        confidence = "LOW"
    elif draft.get("quantity_unresolved") or draft.get("quantity_hint") or draft.get("same_price_exit"):
        confidence = "MEDIUM"
    else:
        confidence = tconf if tconf != "HIGH" else "HIGH"
    return "POSITION_UPDATE", confidence, draft


def _classify_text_chunk(chunk, database_url=None, user_id=None, hint_code=None, next_chunk=""):
    """自然文の1塊をEVENT/EXPERT_OPINION/CATALYST/TRADE_RULE/UNKNOWNへ分類する簡易
    ヒューリスティック（指示書4番STEP3・4、指示書22・23番：事実と意見の区別を試みるが、
    あくまで簡易判定であり確信が持てなければLOW・UNKNOWNに倒す）。AIモデルは使わず、
    キーワード・正規表現のみで判定する（他のAUTO系エンジンと同じ設計方針）。"""
    chunk = chunk.strip()
    if not chunk:
        return "UNKNOWN", "LOW", {}
    # 指示書1・5番（Phase SI-C）：WATCHLIST_UPDATE/POSITION_UPDATEは価格・株数・「監視銘柄に
    # 追加」等の非常に具体的なパターンのため、他カテゴリより先に判定して取りこぼさない。
    wp = _classify_watchlist_or_position_chunk(chunk, database_url, user_id, hint_code=hint_code, next_chunk=next_chunk)
    if wp:
        return wp
    has_date = any(p.search(chunk) for p in _SMART_IMPORT_DATE_PATTERNS)
    has_event_kw = any(kw in chunk for kw in _SMART_IMPORT_EVENT_KEYWORDS)
    expert_match = _SMART_IMPORT_EXPERT_NAME_RE.search(chunk)
    has_opinion_verb = any(v in chunk for v in _SMART_IMPORT_OPINION_VERBS)
    has_catalyst_kw = any(kw in chunk for kw in _SMART_IMPORT_CATALYST_KEYWORDS)
    stock_code = _SMART_IMPORT_STOCK_CODE_RE.search(chunk)
    has_rule_decl = any(kw in chunk for kw in _SMART_IMPORT_RULE_DECLARATION_KEYWORDS)
    has_rule_action = any(kw in chunk for kw in _SMART_IMPORT_RULE_ACTION_KEYWORDS)
    pct_match = _SMART_IMPORT_RULE_PCT_RE.search(chunk)

    # 話者名＋意見動詞（より具体的なシグナル）を、単なるイベントキーワード一致より優先する。
    # 例：「木野内氏はFOMCまでは慎重との見方」はFOMCという語を含むが、これはイベント告知では
    # なく有識者の見解についての文であるため、EXPERT_OPINIONを優先する。
    if expert_match and has_opinion_verb:
        return "EXPERT_OPINION", "MEDIUM", {
            "expert_name": expert_match.group(1), "opinion_summary": chunk, "outlook": chunk}
    # ルール宣言語＋トレード行動語の組み合わせも、単なるイベントキーワード一致より優先する
    # （例：「FOMCまでスイング禁止」はFOMCを含むがイベント告知ではなくルール宣言）。
    if has_rule_decl and has_rule_action:
        is_risk_rule_change = bool(pct_match) and any(kw in chunk for kw in _SMART_IMPORT_RISK_RULE_KEYWORDS)
        return "TRADE_RULE", ("MEDIUM" if pct_match else "HIGH"), {
            "title": chunk[:60], "description": chunk, "threshold": pct_match.group(1) if pct_match else None,
            "risk_rule_change_candidate": is_risk_rule_change}
    if has_event_kw:
        return "EVENT", ("MEDIUM" if has_date else "LOW"), {
            "title": chunk[:80], "event_date": None, "notes": chunk}
    if has_catalyst_kw:
        return "CATALYST", ("MEDIUM" if stock_code else "LOW"), {
            "title": chunk[:80], "summary": chunk, "ticker": stock_code.group(1) if stock_code else None}
    return "UNKNOWN", "LOW", {}


# 指示書2・3番（Phase SI-B）：文単位ではなく文章全体レベルで市場地合い・朝一・場中の
# 「語り（narrative）」を検出する。MARKET_ANALYSIS/MORNING_MARKET_CHECK/INTRADAY_REPORTは
# 1文に収まらない段落・複数文であることが多いため、_classify_text_chunk（1文単位）とは
# 別枠で全体を1つの候補として扱う。
_SMART_IMPORT_REGIME_KEYWORDS = ["RISK_OFF", "RISK_ON", "リスクオフ", "リスクオン", "様子見",
    "地合い", "強気相場", "弱気相場"]
_SMART_IMPORT_INDEX_MOVE_RE = re.compile(r"(日経|NASDAQ|ナスダック|VIX|米10年|TOPIX|ダウ|SOX)[^\d]{0,6}[-+]?\d+(\.\d+)?")
_SMART_IMPORT_MORNING_MARKERS = ["今朝", "朝一", "寄り前", "本日の戦略", "本日の注目", "朝の想定"]
_SMART_IMPORT_INTRADAY_MARKERS = ["時点", "VWAP", "地合い耐性", "朝仮説", "寄り30分", "前場終了", "後場"]
_SMART_IMPORT_INTRADAY_TIME_RE = re.compile(r"(09:30|9:30|11:30|13:00|15:30)")


def _guess_market_regime_from_text(text):
    """テキスト中の地合い表現からmarket_regime文字列を推定する（数値の推測はしないが、
    明示的に書かれている地合い語の言い換え正規化程度は許容する）。判別できなければNone。"""
    if "RISK_OFF" in text or "リスクオフ" in text or "RISK OFF" in text:
        return "RISK_OFF"
    if "RISK_ON" in text or "リスクオン" in text or "RISK ON" in text:
        return "RISK_ON"
    if "様子見" in text:
        return "NEUTRAL"
    return None


def _classify_market_narrative(raw_text):
    """raw_text全体からMARKET_ANALYSIS/MORNING_MARKET_CHECK/INTRADAY_REPORTのいずれかを
    検出する（指示書2・3・5番）。地合いキーワードまたは指数変動の言及が無ければNoneを返す
    （誤検出防止、指示書30番の「判別不能」に倒れる）。"""
    text = raw_text or ""
    has_regime_kw = any(kw in text for kw in _SMART_IMPORT_REGIME_KEYWORDS)
    has_index_move = bool(_SMART_IMPORT_INDEX_MOVE_RE.search(text))
    if not (has_regime_kw or has_index_move):
        return None
    signal_count = sum([has_regime_kw, has_index_move])
    is_morning = any(kw in text for kw in _SMART_IMPORT_MORNING_MARKERS)
    is_intraday = any(kw in text for kw in _SMART_IMPORT_INTRADAY_MARKERS)
    time_match = _SMART_IMPORT_INTRADAY_TIME_RE.search(text)
    market_regime = _guess_market_regime_from_text(text)

    if is_intraday:
        report_type = _SMART_IMPORT_TIME_TO_REPORT_TYPE.get(time_match.group(1).replace("9:30", "09:30")) if time_match else None
        confidence = "HIGH" if (report_type and signal_count >= 2) else ("MEDIUM" if report_type else "LOW")
        return {"category": "INTRADAY_REPORT", "confidence": confidence, "source_kind": "natural_text",
                "raw_text": text[:800], "draft": {"report_type": report_type, "market_summary": text,
                                                    "market_regime": market_regime, "trade_date": None}}
    if is_morning:
        confidence = "HIGH" if signal_count >= 2 else "MEDIUM"
        return {"category": "MORNING_MARKET_CHECK", "confidence": confidence, "source_kind": "natural_text",
                "raw_text": text[:800], "draft": {"summary": text, "market_regime": market_regime}}
    confidence = "MEDIUM" if signal_count >= 2 else "LOW"
    return {"category": "MARKET_ANALYSIS", "confidence": confidence, "source_kind": "natural_text",
            "raw_text": text[:800], "draft": {"summary": text, "market_regime": market_regime}}


def classify_content(raw_text, database_url=None, user_id=None):
    """SmartImportEngineの入口（指示書4番の全STEP）。1回の貼り付けに複数種類の情報が
    あっても対応し、1入力＝1レコードに固定しない（指示書5番）。database_url/user_idは
    Phase SI-C（WATCHLIST_UPDATE/POSITION_UPDATE）の銘柄コード解決に使う（省略時は
    コード直接記載以外の名前解決ができずticker=Noneになるだけで、他カテゴリの判定には
    影響しない）。
    戻り値：[{category, confidence, source_kind, raw_text, draft}, ...]。
    draftはnormalize_*()にそのまま渡せる形（JSON由来ならそのdict、自然文由来なら
    抽出したフィールドのdict）。"""
    raw_text = (raw_text or "").strip()
    if not raw_text:
        return []
    parsed = _extract_json_candidates(raw_text)
    if parsed is not None:
        items = parsed if isinstance(parsed, list) else [parsed]
        # {"catalysts":[...]}/{"events":[...]}/{"expert_views":[...]}等の既存ラッパー形式も吸収する
        if len(items) == 1 and isinstance(items[0], dict):
            wrapper = items[0]
            for key in ("catalysts", "events", "market_events", "expert_views", "views"):
                if isinstance(wrapper.get(key), list) and wrapper[key]:
                    items = wrapper[key]
                    break
        candidates = []
        for item in items:
            category, confidence, draft = _classify_json_item(item)
            candidates.append({"category": category, "confidence": confidence, "source_kind": "json",
                                "raw_text": json.dumps(item, ensure_ascii=False)[:500], "draft": draft})
        if candidates:
            return candidates
    # JSONとして解釈できない、または要素0件→自然文解析（指示書3・30番：エラーにしない）
    candidates = []
    # 指示書2・3・5番（Phase SI-B）：文章全体レベルでMARKET_ANALYSIS/MORNING_MARKET_CHECK/
    # INTRADAY_REPORTを先に検出する（1文単位では拾えない段落全体の「語り」のため）。
    narrative = _classify_market_narrative(raw_text)
    if narrative:
        candidates.append(narrative)
    # 続けて文単位でEVENT/EXPERT_OPINION/CATALYST/TRADE_RULEを検出する（指示書10番：
    # 複数カテゴリ混在文を独立候補へ分割、narrativeとは重複しても構わない＝両方提示する）。
    chunks = [c.strip() for c in re.split(r"[。\n]", raw_text) if c.strip()]
    if not chunks:
        chunks = [raw_text]
    last_ticker = None  # 指示書9番：「逆指値3820に上げた」等、名前が省略された文への継承用
    for idx, chunk in enumerate(chunks):
        next_chunk = chunks[idx + 1] if idx + 1 < len(chunks) else ""
        category, confidence, draft = _classify_text_chunk(chunk, database_url, user_id, hint_code=last_ticker, next_chunk=next_chunk)
        if category != "UNKNOWN":
            candidates.append({"category": category, "confidence": confidence, "source_kind": "natural_text",
                                "raw_text": chunk, "draft": draft})
            if category in ("WATCHLIST_UPDATE", "POSITION_UPDATE") and draft.get("ticker"):
                last_ticker = draft["ticker"]
    if not candidates:
        # 完全に判別不能な場合のみ（指示書30番の「登録候補を特定できませんでした」に対応）
        candidates.append({"category": "UNKNOWN", "confidence": "LOW", "source_kind": "natural_text",
                            "raw_text": raw_text[:500], "draft": {}})
    return candidates


def normalize_catalyst(draft, raw_text=None, import_source="unknown"):
    """draft（JSON由来 or 自然文由来）をinvestment_db.import_news_catalysts()が受け付ける
    形へ正規化する（指示書6・7・19番）。titleが無ければNoneを返す（呼び出し側でスキップ）。"""
    draft = draft or {}
    title = draft.get("title") or draft.get("summary")
    if not title:
        return None
    out = dict(draft)
    out["title"] = title
    out.setdefault("catalyst_date", draft.get("catalyst_date") or draft.get("published_at")
                    or datetime.date.today().isoformat())
    if draft.get("ticker") and not out.get("affected_stocks"):
        out["affected_stocks"] = [draft["ticker"]]
    out.setdefault("category", draft.get("catalyst_type") or "OTHER")
    payload = dict(out.get("raw_payload") or {})
    payload.update({"raw_text": raw_text, "import_source": import_source, "smart_import": True})
    out["raw_payload"] = payload
    return out


def normalize_event(draft, raw_text=None, import_source="unknown"):
    """draftをinvestment_db.import_market_events()が受け付ける形へ正規化する（指示書6・9・
    19番）。既存の_normalize_market_event()（legacy JSON互換の日付/importance変換）を
    そのまま再利用し、変換ロジックを重複実装しない。titleが無ければNoneを返す。
    event_dateが不明確な場合も勝手に補完せずNoneのまま返す（呼び出し側・プレビューで警告）。"""
    draft = draft or {}
    title = draft.get("title") or draft.get("event")
    if not title:
        return None
    out = investment_db._normalize_market_event(draft) if investment_db else dict(draft)
    out["title"] = title
    payload = dict(out.get("raw_payload") or {})
    payload.update({"raw_text": raw_text, "import_source": import_source, "smart_import": True})
    out["raw_payload"] = payload
    return out


def normalize_expert_opinion(draft, raw_text=None, import_source="unknown"):
    """draftをinvestment_db.import_expert_views()が受け付ける形へ正規化する（指示書6・8・
    19番）。expert_nameが無ければNoneを返す。指示書22番：opinion系フィールド（outlook等）に
    入れるだけで、事実データのテーブル（news_catalysts/market_events）とは混同しない。"""
    draft = draft or {}
    expert_name = draft.get("expert_name") or draft.get("speaker")
    if not expert_name:
        return None
    out = dict(draft)
    out["expert_name"] = expert_name
    out.setdefault("published_at", draft.get("published_at") or draft.get("date")
                    or datetime.date.today().isoformat())
    out.setdefault("outlook", draft.get("opinion_summary") or draft.get("outlook"))
    payload = dict(out.get("raw_payload") or {})
    payload.update({"raw_text": raw_text, "import_source": import_source, "smart_import": True})
    out["raw_payload"] = payload
    return out


def normalize_market_analysis(draft, raw_text=None, import_source="unknown"):
    """draftをinvestment_db.import_news_catalysts()が受け付ける形へ正規化する（指示書2番）。
    MARKET_ANALYSIS専用のテーブルは新設せず、既存news_catalystsのcategory="MACRO"
    （市場全体に関わるカタリスト、既存enumをそのまま再利用）として保存する——市場分析の
    詳細構造（market_regime/strong_sectors/weak_sectors/watch_tickers/avoid_tickers/
    risk_factors/strategy等）はraw_payloadへそのまま保持し、失われないようにする
    （指示書19番）。指示書22番：あくまでANALYSIS（意見）であってFACTではないことを
    raw_payload.information_kindで明示する。summaryが無ければNoneを返す。"""
    draft = draft or {}
    summary = draft.get("summary") or draft.get("market_summary")
    if not summary:
        return None
    title = draft.get("title") or summary[:60]
    out = {
        "title": title, "summary": summary,
        "catalyst_date": draft.get("analysis_date") or draft.get("published_at") or datetime.date.today().isoformat(),
        "category": "MACRO",
        "affected_stocks": (draft.get("watch_tickers") or []) + (draft.get("avoid_tickers") or []),
        "affected_sectors": (draft.get("strong_sectors") or []) + (draft.get("weak_sectors") or []),
        "source": draft.get("source") or import_source,
    }
    payload = dict(draft.get("raw_payload") or {})
    payload.update({
        "raw_text": raw_text, "import_source": import_source, "smart_import": True,
        "information_kind": "ANALYSIS", "market_regime": draft.get("market_regime"),
        "volatility_regime": draft.get("volatility_regime"), "strong_sectors": draft.get("strong_sectors"),
        "weak_sectors": draft.get("weak_sectors"), "watch_tickers": draft.get("watch_tickers"),
        "avoid_tickers": draft.get("avoid_tickers"), "risk_factors": draft.get("risk_factors"),
        "strategy": draft.get("strategy"), "key_points": draft.get("key_points"),
    })
    out["raw_payload"] = payload
    return out


# Morning Checkの自然文解析結果をsave_morning_check()の必須列（_MORNING_CHECK_SCALAR_COLS/
# _MORNING_CHECK_JSON_COLS）へ写像する。数値スコア系（market_risk_score・volatility_score等）
# は自然文から確信を持って抽出できないためNoneのまま保持する（指示書3・20番：不足値を
# 推測しない）。
def normalize_morning_check(draft, raw_text=None, import_source="unknown"):
    """draftをinvestment_db.save_morning_check()が受け付けるdata形へ正規化する
    （指示書3・4番）。summaryもmarket_regimeも取れない場合はNoneを返す（保存不可）。
    戻り値：(check_date, data) のタプル、またはNone。"""
    draft = draft or {}
    if not (draft.get("summary") or draft.get("market_regime") or draft.get("global_market")):
        return None
    check_date = draft.get("date") or datetime.date.today().isoformat()
    data = {
        "market_regime": draft.get("market_regime"), "volatility_regime": draft.get("volatility_regime"),
        "trend_type": draft.get("trend_type"), "market_risk_score": draft.get("market_risk_score"),
        "volatility_score": draft.get("volatility_score"), "trend_score": draft.get("trend_score"),
        "macro_pressure_score": draft.get("macro_pressure_score"),
        "strategy_text": draft.get("summary") or draft.get("morning_conclusion") or draft.get("strategy"),
        "indices_json": {"global_market": draft.get("global_market"), "japan_market": draft.get("japan_market"),
                          "rates_fx": draft.get("rates_fx"), "commodities": draft.get("commodities")},
        "strong_sectors_json": draft.get("strong_sectors") or [],
        "weak_sectors_json": draft.get("weak_sectors") or [],
        # daytrade_watchlist/watch_tickers等、呼称のブレを吸収する（指示書13番：既存WARNINGの
        # 改善＝別キーにある同等情報を自動マッピングし、ユーザーにschemaの違いを意識させない）。
        "watchlist_top5_json": [{"code": t.get("code") if isinstance(t, dict) else None,
                                   "name": t.get("name") if isinstance(t, dict) else t, "rank": i + 1}
                                  for i, t in enumerate(draft.get("daytrade_watchlist") or draft.get("watch_tickers") or [])],
        "avoid_stocks_json": [{"code": t.get("code") if isinstance(t, dict) else None,
                                 "name": t.get("name") if isinstance(t, dict) else t}
                                for t in (draft.get("avoid_watch") or draft.get("avoid_tickers") or [])],
        "risk_warnings_json": draft.get("risk_alerts") or [],
        "event_risk_json": [],
        "position_risk_json": [],
        "strategy_json": {"execution_rules": draft.get("execution_rules"), "scenario_plan": draft.get("scenario_plan")},
        "data_quality_json": {"smart_import": True,
                               "quality": "PARTIAL" if draft.get("market_risk_score") is None else "FULL"},
        "raw_payload_json": {"raw_text": raw_text, "import_source": import_source, "smart_import": True,
                              "source_mode": "smart_import"},
    }
    return check_date, data


def normalize_intraday_report(draft, raw_text=None, import_source="unknown"):
    """draftをinvestment_db.save_market_intelligence_report()が受け付けるdata形へ正規化する
    （指示書5・6番）。report_typeが確定できない場合は勝手に推定せずNoneを返す（保存不可、
    プレビューでユーザーに時間帯の指定を促す）。戻り値：(trade_date, report_type, data) の
    タプル、またはNone。"""
    draft = draft or {}
    report_type = draft.get("report_type")
    if report_type not in INTRADAY_REPORT_SNAPSHOT_TIMES:
        return None
    if not (draft.get("market_summary") or draft.get("market_regime")):
        return None
    trade_date = draft.get("trade_date") or datetime.date.today().isoformat()
    data = {
        "scheduled_time": INTRADAY_REPORT_SNAPSHOT_TIMES[report_type],
        "morning_check_id": None,
        "market_regime": draft.get("market_regime"), "volatility_regime": draft.get("volatility_regime"),
        "market_summary": draft.get("market_summary"),
        "nikkei_change_pct": draft.get("nikkei_change_pct"), "topix_change_pct": draft.get("topix_change_pct"),
        "growth250_change_pct": draft.get("growth250_change_pct"), "nikkei_vi": draft.get("nikkei_vi"),
        "usdjpy": draft.get("usdjpy"),
        "sector_snapshot_json": {"strong": draft.get("strong_sectors") or [], "weak": draft.get("weak_sectors") or []},
        "strong_sectors_json": draft.get("strong_sectors") or [], "weak_sectors_json": draft.get("weak_sectors") or [],
        "top_stocks_json": draft.get("top_stocks") or [], "resilience_stocks_json": draft.get("resilience_stocks") or [],
        "momentum_stocks_json": [], "missed_opportunities_json": [],
        "morning_thesis_evaluation_json": draft.get("morning_thesis_evaluation") or {"stocks": []},
        "risk_alerts_json": draft.get("risk_alerts") or [], "position_alerts_json": [],
        "news_changes_json": [], "event_risk_json": [],
        "strategy_update_json": {"text": draft.get("strategy_update") or draft.get("market_summary")},
        "data_health_json": {"smart_import": True,
                              "quality": "PARTIAL" if draft.get("nikkei_change_pct") is None else "FULL",
                              "raw_text": raw_text, "import_source": import_source},
    }
    return trade_date, report_type, data


def normalize_trade_rule(draft, raw_text=None, import_source="unknown"):
    """draftをinvestment_db.upsert_trade_rule_from_text()が受け付ける引数へ正規化する
    （指示書7・8・9番：TRADE_RULEは既存のルール学習システム（trade_rules、常にTESTING/LOW
    始まり）へ候補として記録するだけで、position_risk_rules等の実運用ルールを直接
    書き換えることは絶対にしない）。description/titleが無ければNoneを返す。
    戻り値：(rule_text, source_info, risk_rule_change_candidate) のタプル、またはNone。"""
    draft = draft or {}
    rule_text = draft.get("description") or draft.get("title")
    if not rule_text:
        return None
    source_info = {"raw_text": raw_text, "import_source": import_source, "smart_import": True,
                    "threshold": draft.get("threshold")}
    return rule_text, source_info, bool(draft.get("risk_rule_change_candidate"))


def normalize_watchlist_update(draft, raw_text=None, import_source="unknown"):
    """draftをinvestment_db.upsert_watchlist_item()/delete_watchlist_item()が受け付ける
    形へ正規化する（指示書1・4番（Phase SI-C））。tickerが解決できていなければNoneを返す
    （指示書3番：銘柄コードに自信が無い場合は保存不可、勝手に推定しない）。"""
    draft = draft or {}
    ticker = draft.get("ticker")
    if not ticker:
        return None
    action = str(draft.get("action") or "ADD").upper()
    if action not in ("ADD", "UPDATE", "REMOVE"):
        action = "ADD"
    return {"code": ticker, "name": draft.get("name") or draft.get("name_hint"), "action": action,
            "themes": draft.get("themes"), "reason": draft.get("reason") or draft.get("notes"),
            "raw_text": raw_text, "import_source": import_source}


def normalize_position_update(database_url, user_id, draft, raw_text=None, import_source="unknown"):
    """draftを既存のadd_position_entry/add_position_exit/upsert_portfolio_item（すべて
    investment_db.py既存関数、平均単価・実現損益の計算ロジックはそこに委譲し重複実装しない、
    指示書11・12番）が受け付ける形へ正規化する（指示書5〜10番）。ticker・価格・数量等が
    確定できない場合はNoneを返す（指示書7・8・10番：勝手に確定しない）。
    戻り値：正規化済みdict、またはNone。"""
    draft = draft or {}
    ticker = draft.get("ticker")
    action = str(draft.get("action") or "").upper()
    if not ticker or not action:
        return None
    existing = None
    if investment_db is not None and database_url:
        try:
            existing = next((p for p in investment_db.list_portfolio(database_url, user_id) if p.get("code") == ticker), None)
        except Exception:
            existing = None

    resolved = {"code": ticker, "action": action, "name": draft.get("name") or (existing or {}).get("name"),
                "trade_style": draft.get("trade_style") or (existing or {}).get("trade_style"),
                "raw_text": raw_text, "import_source": import_source}

    if action in ("OPEN", "ADD"):
        # 指示書11番：既存add_position_entry()が加重平均計算を担うため、ここでは正規化のみ。
        if draft.get("entry_price") is None or draft.get("quantity") is None:
            return None
        resolved["entry_price"] = draft["entry_price"]
        resolved["quantity"] = draft["quantity"]
        resolved["stop_price"] = draft.get("stop_price")  # 同一文の逆指値併記（指示書14番）
        return resolved

    if action in ("REDUCE", "CLOSE"):
        quantity = draft.get("quantity")
        exit_price = draft.get("exit_price")
        quantity_hint = draft.get("quantity_hint")
        if quantity_hint == "HALF":
            # 指示書8番：「半分利確」は既存保有数量の半分。保有が特定できなければ保存不可。
            if not existing or not existing.get("quantity"):
                return None
            quantity = float(existing["quantity"]) / 2
        elif quantity_hint == "ALL" or (action == "CLOSE" and quantity is None):
            if not existing or not existing.get("quantity"):
                return None
            quantity = float(existing["quantity"])
        if draft.get("same_price_exit"):
            # 指示書8番：「同値撤退」は取得単価と同値での決済。
            if not existing or existing.get("average_price") is None:
                return None
            exit_price = float(existing["average_price"])
        if quantity is None or exit_price is None:
            return None  # 指示書10番：売却数量・価格が未解決のまま勝手にCLOSE確定しない
        resolved["quantity"] = quantity
        resolved["exit_price"] = exit_price
        return resolved

    if action == "UPDATE_STOP":
        if draft.get("stop_price") is None or not existing:
            return None  # 対象ポジションが無ければ逆指値だけ更新できない
        resolved["stop_price"] = draft["stop_price"]
        return resolved

    if action == "HOLD_NOTE":
        resolved["note"] = draft.get("notes")
        return resolved

    return None


def smart_import_check_duplicates(database_url, user_id, candidates):
    """CATALYST/MARKET_ANALYSIS/EVENTの候補について、既存データとタイトルが一致する可能性が
    あるものにpossible_duplicate=Trueを付与する（指示書19番（Phase SI-B）・Phase SI-Aの
    指示書20番。厳密なcontent hash照合ではなく簡易なタイトル一致判定——自動で二重登録は
    しない、あくまでプレビュー警告用）。MORNING_MARKET_CHECK/INTRADAY_REPORTは
    (user_id,date,snapshot_time)/(user_id,trade_date,report_type)の既存UNIQUE制約＋
    UPSERTで重複防止されるため、ここでは対象外（指示書19番）。"""
    if investment_db is None or not database_url:
        return candidates
    existing_catalyst_titles, existing_event_titles = None, None
    for c in candidates:
        title = (c.get("draft") or {}).get("title")
        category = c.get("category")
        if category in ("CATALYST", "MARKET_ANALYSIS"):
            if not title:
                title = (c.get("draft") or {}).get("summary", "")[:60] or None
            if not title:
                continue
            if existing_catalyst_titles is None:
                try:
                    existing_catalyst_titles = {x.get("title") for x in investment_db.list_news_catalysts(database_url, user_id, limit=300)}
                except Exception:
                    existing_catalyst_titles = set()
            if title in existing_catalyst_titles:
                c["possible_duplicate"] = True
        elif category == "EVENT":
            if not title:
                continue
            if existing_event_titles is None:
                try:
                    existing_event_titles = {x.get("title") for x in investment_db.list_market_events(database_url, user_id, limit=200)}
                except Exception:
                    existing_event_titles = set()
            if title in existing_event_titles:
                c["possible_duplicate"] = True
        elif category == "WATCHLIST_UPDATE":
            # 指示書4番（Phase SI-C）：既にWatchlist登録済みのtickerへのADDは、新規ADDではなく
            # 「テーマ追加として処理しますか？」の確認候補として扱う（自動で二重登録しない）。
            ticker = (c.get("draft") or {}).get("ticker")
            action = str((c.get("draft") or {}).get("action") or "ADD").upper()
            if ticker and action == "ADD":
                try:
                    existing_codes = {w.get("code") for w in investment_db.list_watchlist(database_url, user_id, market="JP")}
                except Exception:
                    existing_codes = set()
                if ticker in existing_codes:
                    c["possible_duplicate"] = True
        elif category == "POSITION_UPDATE":
            # 指示書18番：同一ticker+action+price+quantityの候補が直近のtrade_history/
            # portfolioと一致する場合に警告する簡易判定（時刻厳密照合はしない）。
            draft = c.get("draft") or {}
            ticker, action = draft.get("ticker"), str(draft.get("action") or "").upper()
            if ticker and action in ("OPEN",):
                try:
                    existing_codes = {p.get("code") for p in investment_db.list_portfolio(database_url, user_id)}
                except Exception:
                    existing_codes = set()
                if ticker in existing_codes:
                    c["possible_duplicate"] = True  # 既に保有中→ADDの意図の可能性を警告
    return candidates


def smart_import_confirm(database_url, user_id, candidates, import_source="unknown"):
    """プレビュー画面でユーザーが確定した候補群を、カテゴリごとに既存のimport_*/save_*関数へ
    振り分けて保存する（指示書18番（Phase SI-A）・24番（Phase SI-B）：SmartImportは入口・
    変換・振り分けのみを担当し、独自の巨大DBは作らない・カテゴリごとに別エンジンを作らない）。
    LOW confidenceの候補はforce指定が無い限り保存しない（指示書16番：LOWは自動保存禁止）。
    1件の失敗が他候補の保存を巻き戻さない（指示書18番（Phase SI-B）：CATALYST/EVENT/
    EXPERT_OPINION/MARKET_ANALYSISはバッチ処理のため個別例外はimport_*内部で吸収されるが、
    MORNING_MARKET_CHECK/INTRADAY_REPORT/TRADE_RULE/CHATGPT_LEGACYは1件ずつtry/exceptする）。
    戻り値：{"results": {category: {...}}, "rejected_low_confidence": N,
    "skipped_unimplemented": M, "skipped_existing_report": K, "trade_rule_results": [...]}"""
    if investment_db is None or not database_url:
        return {"results": {}, "rejected_low_confidence": 0, "skipped_unimplemented": 0}
    buckets = {"CATALYST": [], "EVENT": [], "EXPERT_OPINION": [], "MARKET_ANALYSIS": []}
    rejected = 0
    skipped_unimplemented = 0
    skipped_existing_report = 0
    morning_check_results = []
    intraday_report_results = []
    trade_rule_results = []
    chatgpt_legacy_results = []
    watchlist_update_results = []
    position_update_results = []
    social_image_analysis_results = []

    for c in candidates or []:
        category = c.get("category")
        if category not in SMART_IMPORT_IMPLEMENTED_CATEGORIES:
            skipped_unimplemented += 1
            continue
        if c.get("confidence") == "LOW" and not c.get("force"):
            rejected += 1
            continue
        draft = c.get("draft") or {}
        raw_text = c.get("raw_text")

        if category in ("CATALYST", "EVENT", "EXPERT_OPINION", "MARKET_ANALYSIS"):
            normalized = None
            try:
                if category == "CATALYST":
                    normalized = normalize_catalyst(draft, raw_text, import_source)
                elif category == "EVENT":
                    normalized = normalize_event(draft, raw_text, import_source)
                elif category == "EXPERT_OPINION":
                    normalized = normalize_expert_opinion(draft, raw_text, import_source)
                elif category == "MARKET_ANALYSIS":
                    normalized = normalize_market_analysis(draft, raw_text, import_source)
            except Exception as e:
                print("  SmartImport: 正規化失敗", category, e)
                normalized = None
            if normalized is not None:
                buckets[category].append(normalized)
            continue

        if category == "MORNING_MARKET_CHECK":
            try:
                normalized = normalize_morning_check(draft, raw_text, import_source)
                if normalized is not None:
                    check_date, data = normalized
                    # snapshot_time="SMART_IMPORT"専用枠を使う（指示書4番：既存の自動生成
                    # 05:30/07:00/08:00/08:30/08:50/MANUALとは別枠のため、上書きの心配なし）。
                    saved = investment_db.save_morning_check(database_url, user_id, check_date, "SMART_IMPORT", data)
                    morning_check_results.append({"ok": saved is not None, "check_date": check_date})
                else:
                    morning_check_results.append({"ok": False, "reason": "必須項目（summary/market_regime）が不足"})
            except Exception as e:
                print("  SmartImport: MorningCheck保存失敗", e)
                morning_check_results.append({"ok": False, "reason": str(e)})
            continue

        if category == "INTRADAY_REPORT":
            try:
                normalized = normalize_intraday_report(draft, raw_text, import_source)
                if normalized is None:
                    intraday_report_results.append({"ok": False, "reason": "report_type未確定またはmarket_summary不足のため保存できません"})
                else:
                    trade_date, report_type, data = normalized
                    # 指示書「既存機能を壊さない」を最優先：自動生成された既存レポート
                    # （data_health_json.smart_importが無いもの）を、貼り付けたテキストで
                    # 無言のまま上書きしない。既存の自動レポートがある場合はforce指定が
                    # 無い限りスキップする（Smart Import同士の再取り込みはUPSERTでよい）。
                    existing = investment_db.get_market_intelligence_report(database_url, user_id, trade_date, report_type)
                    if existing and not (existing.get("data_health_json") or {}).get("smart_import") and not c.get("force"):
                        skipped_existing_report += 1
                        intraday_report_results.append({"ok": False, "reason": f"{report_type}は既に自動生成レポートが存在するため上書きしません（forceで上書き可）"})
                    else:
                        saved = investment_db.save_market_intelligence_report(database_url, user_id, trade_date, report_type, data)
                        intraday_report_results.append({"ok": saved is not None, "trade_date": trade_date, "report_type": report_type})
            except Exception as e:
                print("  SmartImport: IntradayReport保存失敗", e)
                intraday_report_results.append({"ok": False, "reason": str(e)})
            continue

        if category == "TRADE_RULE":
            # 指示書8・9番（最重要）：TRADE_RULEはconfidenceに関わらず既存のルール学習
            # システム（trade_rules、常にTESTING/LOW始まり）へ候補記録するのみ。
            # position_risk_rules等の実運用ルールを直接書き換える経路はここには一切無い。
            try:
                normalized = normalize_trade_rule(draft, raw_text, import_source)
                if normalized is None:
                    trade_rule_results.append({"ok": False, "reason": "ルール文が空のため保存できません"})
                else:
                    rule_text, source_info, is_risk_rule_change = normalized
                    result = investment_db.upsert_trade_rule_from_text(
                        database_url, user_id, rule_text, source_info=source_info, created_from="smart_import")
                    trade_rule_results.append({"ok": result is not None, "rule_text": rule_text,
                                                "risk_rule_change_candidate": is_risk_rule_change, "detail": result})
            except Exception as e:
                print("  SmartImport: TradeRule保存失敗", e)
                trade_rule_results.append({"ok": False, "reason": str(e)})
            continue

        if category == "CHATGPT_LEGACY":
            # 指示書1番：既知の旧ChatGPT統合連携schema（date/watchlist/decisions/rule_updates
            # 等）は、既存のsave_chatgpt_unified_import()（内部でclassify_chatgpt_unified_
            # payload()を使い、watchlist/decisions/rule_updates/events/news/catalysts/
            # expert_opinions/user_feedback/trade_playbooksを一括で振り分ける）へそのまま委譲
            # する。SmartImport側で個別に再実装しない（指示書33番）。
            try:
                result = investment_db.save_chatgpt_unified_import(database_url, user_id, draft)
                chatgpt_legacy_results.append(result)
            except Exception as e:
                print("  SmartImport: ChatGPT legacy import失敗", e)
                chatgpt_legacy_results.append({"error": str(e)})
            continue

        if category == "WATCHLIST_UPDATE":
            # 指示書1〜4番（Phase SI-C）：既存upsert_watchlist_item/delete_watchlist_itemへ
            # 振り分けるのみ。REMOVEは重要操作だが、ここに来る時点でユーザーが明示的に選択・
            # 確定した候補のみ（UIで既定未選択、指示書2・16番）。
            try:
                normalized = normalize_watchlist_update(draft, raw_text, import_source)
                if normalized is None:
                    watchlist_update_results.append({"ok": False, "reason": "銘柄コードを解決できないため保存できません"})
                elif normalized["action"] == "REMOVE":
                    investment_db.delete_watchlist_item(database_url, user_id, normalized["code"], market="JP")
                    watchlist_update_results.append({"ok": True, "code": normalized["code"], "action": "REMOVE"})
                else:
                    item = {"code": normalized["code"], "market": "JP", "source": "smart_import"}
                    if normalized.get("name"):
                        item["name"] = normalized["name"]
                    if normalized.get("reason"):
                        item["note"] = normalized["reason"]
                    if normalized.get("themes"):
                        # 指示書4番：既存themeは明示削除指示が無い限り残す（置換ではなく追記マージ）。
                        existing_theme = None
                        try:
                            existing_w = next((w for w in investment_db.list_watchlist(database_url, user_id, market="JP")
                                                if w.get("code") == normalized["code"]), None)
                            existing_theme = (existing_w or {}).get("theme")
                        except Exception:
                            pass
                        new_parts = [t.strip() for t in re.split(r"[、,]", normalized["themes"]) if t.strip()]
                        old_parts = [t.strip() for t in re.split(r"[、,]", existing_theme or "") if t.strip()]
                        merged = old_parts + [t for t in new_parts if t not in old_parts]
                        item["theme"] = "、".join(merged) if merged else None
                    ok = investment_db.upsert_watchlist_item(database_url, user_id, item)
                    watchlist_update_results.append({"ok": ok, "code": normalized["code"], "action": normalized["action"]})
            except Exception as e:
                print("  SmartImport: WatchlistUpdate保存失敗", e)
                watchlist_update_results.append({"ok": False, "reason": str(e)})
            continue

        if category == "POSITION_UPDATE":
            # 指示書5〜13番（最重要）：POSITION_UPDATEはconfidenceに関わらず既に「ユーザーが
            # 明示的に選択・確定した」候補のみここへ来る（UIで既定未選択、指示書6・16番）。
            # 平均単価計算・実現損益計算は既存add_position_entry/add_position_exitへ完全に
            # 委譲し、別ロジックを作らない（指示書11・12番）。position_risk_rules自体は
            # 変更しない（次回評価時に既存関数が最新のportfolioを見るだけで自動的に
            # 再評価される、指示書13番）。
            try:
                normalized = normalize_position_update(database_url, user_id, draft, raw_text, import_source)
                if normalized is None:
                    position_update_results.append({"ok": False, "reason": "対象銘柄・価格・数量のいずれかが未解決のため保存できません"})
                else:
                    code, action = normalized["code"], normalized["action"]
                    if action in ("OPEN", "ADD"):
                        updated = investment_db.add_position_entry(
                            database_url, user_id, code, normalized.get("name"), "JP",
                            normalized["entry_price"], normalized["quantity"], normalized.get("trade_style"))
                        if updated is not None and normalized.get("stop_price") is not None:
                            investment_db.upsert_portfolio_item(database_url, user_id,
                                {"code": code, "market": "JP", "current_stop": normalized["stop_price"]})
                        position_update_results.append({"ok": updated is not None, "code": code, "action": action})
                    elif action in ("REDUCE", "CLOSE"):
                        result = investment_db.add_position_exit(database_url, user_id, code, "JP",
                                                                    normalized["exit_price"], normalized["quantity"])
                        position_update_results.append({"ok": isinstance(result, dict) and "error" not in result,
                                                          "code": code, "action": action, "detail": result})
                    elif action == "UPDATE_STOP":
                        ok = investment_db.upsert_portfolio_item(database_url, user_id,
                            {"code": code, "market": "JP", "current_stop": normalized["stop_price"]})
                        position_update_results.append({"ok": ok, "code": code, "action": action})
                    elif action == "HOLD_NOTE":
                        # DBを変更しない情報メモ（指示書14番の「持ち越し/見送り」）。履歴には残すが
                        # ポジション自体には触れない。
                        position_update_results.append({"ok": True, "code": code, "action": action,
                                                          "note": normalized.get("note")})
            except Exception as e:
                print("  SmartImport: PositionUpdate保存失敗", e)
                position_update_results.append({"ok": False, "reason": str(e)})
            continue

        if category == "SOCIAL_IMAGE_ANALYSIS":
            # にこそく画像解析待ちキュー：ChatGPT等で解析した画像の構造化結果を既存投稿へ
            # 追記保存する（POST /api/social-posts/image-analysisと同じ保存経路）。保存後は
            # PENDING→ANALYZEDになり、economic_events/stock_mentionsがあれば既存の
            # イベント検出・監視銘柄照合ロジックへ流す（新しい判定ロジックは作らない）。
            try:
                normalized = normalize_social_image_analysis(draft, raw_text, import_source)
                if normalized is None:
                    social_image_analysis_results.append({"ok": False, "reason": "post_idが無いため保存できません"})
                else:
                    post_id, analysis = normalized
                    # Phase2（指示書10番）：confidenceは0.0〜1.0へ正規化する（値が無ければnullのまま）。
                    if isinstance(analysis, dict) and "confidence" in analysis:
                        analysis["confidence"] = _normalize_confidence(analysis.get("confidence"))
                    saved = investment_db.save_social_post_image_analysis(database_url, NICOSOKU_X_USERNAME, post_id, [analysis])
                    if saved is None:
                        # Phase2（指示書2番）：保存失敗（対象投稿が見つからない）もFAILEDとして記録する。
                        investment_db.mark_social_post_image_analysis_failed(
                            database_url, NICOSOKU_X_USERNAME, post_id, "対象の投稿が見つかりません")
                        social_image_analysis_results.append({"ok": False, "reason": "対象の投稿が見つかりません", "post_id": post_id})
                    else:
                        events_imported = 0
                        try:
                            posted_at = saved.get("posted_at")
                            posted_date = datetime.datetime.fromisoformat(posted_at).date() if posted_at else datetime.date.today()
                            event_drafts = _normalize_image_economic_events(analysis, posted_date)
                            if event_drafts:
                                existing = investment_db.list_market_events(
                                    database_url, user_id, from_date=posted_date.isoformat(),
                                    to_date=(posted_date + datetime.timedelta(days=120)).isoformat())
                                existing_keys = {(e.get("event_date"), _event_title_key(e.get("title"))) for e in existing}
                                fresh = [d for d in event_drafts if (d["event_date"], _event_title_key(d["title"])) not in existing_keys]
                                if fresh:
                                    imp = investment_db.import_market_events(database_url, user_id, fresh)
                                    events_imported = imp.get("imported", 0)
                        except Exception as e:
                            print("  SmartImport: 画像解析からのイベント検出失敗", e)
                        mentions_added = 0
                        try:
                            mention_names = _extract_stock_mentions_from_analysis(analysis)
                            extra_codes = _resolve_stock_mentions_to_codes(database_url, user_id, mention_names)
                            if extra_codes:
                                investment_db.merge_social_post_mentions(database_url, NICOSOKU_X_USERNAME, post_id, extra_codes)
                                mentions_added = len(extra_codes)
                        except Exception as e:
                            print("  SmartImport: 画像解析からの銘柄関連付け失敗", e)
                        social_image_analysis_results.append({"ok": True, "post_id": post_id,
                                                                "events_imported": events_imported, "mentions_added": mentions_added})
            except Exception as e:
                print("  SmartImport: 画像解析保存失敗", e)
                # Phase2（指示書2番）：解析JSON不正・保存失敗はFAILED＋理由として記録する
                # （post_idが分かる場合のみ。DB側の状態更新自体が失敗しても握りつぶし、
                # SmartImport全体の応答は落とさない）。
                fallback_post_id = draft.get("post_id") if isinstance(draft, dict) else None
                if fallback_post_id:
                    try:
                        investment_db.mark_social_post_image_analysis_failed(
                            database_url, NICOSOKU_X_USERNAME, fallback_post_id, str(e))
                    except Exception:
                        pass
                social_image_analysis_results.append({"ok": False, "reason": str(e), "post_id": fallback_post_id})
            continue

    results = {}
    if buckets["CATALYST"]:
        results["CATALYST"] = investment_db.import_news_catalysts(database_url, user_id, buckets["CATALYST"])
    if buckets["EVENT"]:
        results["EVENT"] = investment_db.import_market_events(database_url, user_id, buckets["EVENT"])
    if buckets["EXPERT_OPINION"]:
        results["EXPERT_OPINION"] = investment_db.import_expert_views(database_url, user_id, buckets["EXPERT_OPINION"])
    if buckets["MARKET_ANALYSIS"]:
        results["MARKET_ANALYSIS"] = investment_db.import_news_catalysts(database_url, user_id, buckets["MARKET_ANALYSIS"])
    if morning_check_results:
        results["MORNING_MARKET_CHECK"] = {"imported": sum(1 for r in morning_check_results if r["ok"]),
                                            "skipped": sum(1 for r in morning_check_results if not r["ok"]),
                                            "details": morning_check_results}
    if intraday_report_results:
        results["INTRADAY_REPORT"] = {"imported": sum(1 for r in intraday_report_results if r["ok"]),
                                       "skipped": sum(1 for r in intraday_report_results if not r["ok"]),
                                       "details": intraday_report_results}
    if trade_rule_results:
        results["TRADE_RULE"] = {"imported": sum(1 for r in trade_rule_results if r["ok"]),
                                  "skipped": sum(1 for r in trade_rule_results if not r["ok"]),
                                  "details": trade_rule_results}
    if chatgpt_legacy_results:
        results["CHATGPT_LEGACY"] = {"count": len(chatgpt_legacy_results), "details": chatgpt_legacy_results}
    if watchlist_update_results:
        results["WATCHLIST_UPDATE"] = {"imported": sum(1 for r in watchlist_update_results if r["ok"]),
                                        "skipped": sum(1 for r in watchlist_update_results if not r["ok"]),
                                        "details": watchlist_update_results}
    if position_update_results:
        results["POSITION_UPDATE"] = {"imported": sum(1 for r in position_update_results if r["ok"]),
                                       "skipped": sum(1 for r in position_update_results if not r["ok"]),
                                       "details": position_update_results}
    if social_image_analysis_results:
        results["SOCIAL_IMAGE_ANALYSIS"] = {"imported": sum(1 for r in social_image_analysis_results if r["ok"]),
                                             "skipped": sum(1 for r in social_image_analysis_results if not r["ok"]),
                                             "details": social_image_analysis_results}
    return {"results": results, "rejected_low_confidence": rejected, "skipped_unimplemented": skipped_unimplemented,
            "skipped_existing_report": skipped_existing_report}


def smart_import_recent_activity(database_url, user_id, limit=15):
    """Smart Import経由で登録された最近の項目一覧を返す（指示書14番（Phase SI-D）：
    「可能なら既存DB情報から構成し、SmartImport専用巨大ログDBは作らない」を厳守——
    新しいテーブルは一切増やさず、既存の各一覧関数からraw_payload.smart_import==Trueの
    行だけを抜き出して合成する。MorningCheck/IntradayReportは日付をまたぐ一覧関数を
    持たないため、直近数日分だけ確認する簡易版（完全な履歴ではない、既知の制約）。"""
    if investment_db is None or not database_url:
        return []
    items = []
    try:
        for c in investment_db.list_news_catalysts(database_url, user_id, limit=300):
            if (c.get("raw_payload") or {}).get("smart_import"):
                is_analysis = (c.get("raw_payload") or {}).get("information_kind") == "ANALYSIS"
                items.append({"time": c.get("created_at"), "category": "MARKET_ANALYSIS" if is_analysis else "CATALYST",
                              "title": c.get("title"), "status": "SUCCESS"})
    except Exception as e:
        print("  SmartImport履歴: catalyst取得失敗", e)
    try:
        for e in investment_db.list_market_events(database_url, user_id, limit=200):
            if (e.get("raw_payload") or {}).get("smart_import"):
                items.append({"time": e.get("created_at"), "category": "EVENT", "title": e.get("title"), "status": "SUCCESS"})
    except Exception as e:
        print("  SmartImport履歴: event取得失敗", e)
    try:
        for v in investment_db.list_expert_views(database_url, user_id, limit=200):
            if (v.get("raw_payload") or {}).get("smart_import"):
                items.append({"time": v.get("created_at"), "category": "EXPERT_OPINION", "title": v.get("expert_name"), "status": "SUCCESS"})
    except Exception as e:
        print("  SmartImport履歴: expert_view取得失敗", e)
    try:
        today = datetime.date.today()
        for d in (today, today - datetime.timedelta(days=1)):
            check = investment_db.get_latest_morning_check(database_url, user_id, check_date=d.isoformat())
            if check and check.get("snapshot_time") == "SMART_IMPORT":
                items.append({"time": check.get("generated_at"), "category": "MORNING_MARKET_CHECK",
                              "title": check.get("strategy_text", "")[:40], "status": "SUCCESS"})
            for rt in INTRADAY_REPORT_SNAPSHOT_TIMES:
                rep = investment_db.get_market_intelligence_report(database_url, user_id, d.isoformat(), rt)
                if rep and (rep.get("data_health_json") or {}).get("smart_import"):
                    items.append({"time": rep.get("generated_at"), "category": "INTRADAY_REPORT",
                                  "title": f"{rt}（{d.isoformat()}）", "status": "SUCCESS"})
    except Exception as e:
        print("  SmartImport履歴: morning/intraday取得失敗", e)
    try:
        for r in investment_db.list_trade_rules(database_url, user_id):
            sources = r.get("source_json") or []
            if any((s or {}).get("smart_import") for s in sources if isinstance(s, dict)):
                items.append({"time": r.get("updated_at") or r.get("created_at"), "category": "TRADE_RULE",
                              "title": r.get("rule_text") or r.get("description"), "status": "CONFIRM_REQUIRED" if r.get("status") == "TESTING" else "SUCCESS"})
    except Exception as e:
        print("  SmartImport履歴: trade_rule取得失敗", e)
    items.sort(key=lambda x: str(x.get("time") or ""), reverse=True)
    return items[:limit]


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
        # ---- 2026-09-09新規（ルール学習システム）：既存/api/rules（investment_rules、単純な
        # 自由テキストルール）とは別の新テーブルtrade_rules用。既存ルートは無変更。 ----
        elif self.path.startswith("/api/trade-rules/detail"):
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            rule_id = params.get("id", [None])[0]
            if not rule_id or not (investment_db is not None and DATABASE_URL):
                self._send_json({"error": "idが必要です"}); return
            rule = investment_db.get_trade_rule(DATABASE_URL, self.current_user, int(rule_id))
            if rule is None:
                self._send_json({"error": "指定されたルールが見つかりません"}); return
            self._send_json({"rule": rule})
        elif self.path.startswith("/api/trade-rules/similar"):
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            text = params.get("text", [None])[0]
            exclude_id = params.get("excludeId", [None])[0]
            candidates = investment_db.find_similar_trade_rules(
                DATABASE_URL, self.current_user, text or "",
                exclude_id=int(exclude_id) if exclude_id else None,
            ) if (investment_db is not None and DATABASE_URL and text) else []
            self._send_json({"candidates": candidates})
        elif self.path.startswith("/api/trade-rules/debug-stats"):
            stats = investment_db.trade_rules_debug_stats(DATABASE_URL, self.current_user) if (investment_db is not None and DATABASE_URL) else {}
            self._send_json({"stats": stats})
        elif self.path.startswith("/api/trade-rules/relevant"):
            # 朝一分析・トレード分析・ポジション分析のChatGPT相談payload・分析カード表示用
            # （指示書12・13番）。?categories=semiconductor,market のようにカンマ区切りで渡す。
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            cats_raw = params.get("categories", [None])[0]
            categories = [c for c in (cats_raw or "").split(",") if c] or None
            rules = investment_db.relevant_trade_rules_for(
                DATABASE_URL, self.current_user, categories=categories,
            ) if (investment_db is not None and DATABASE_URL) else []
            self._send_json({"rules": rules})
        elif self.path.startswith("/api/trade-rules"):
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            rules = investment_db.list_trade_rules(
                DATABASE_URL, self.current_user,
                status=params.get("status", [None])[0], confidence=params.get("confidence", [None])[0],
                category=params.get("category", [None])[0], rule_type=params.get("ruleType", [None])[0],
            ) if (investment_db is not None and DATABASE_URL) else []
            self._send_json({"rules": rules})
        # ---- 2026-09-09新規（日次投資レビュー・投資スコア、指示書Phase4・5） ----
        elif self.path.startswith("/api/daily-review/recent"):
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            days = int(params.get("days", [3])[0])
            reflections = investment_db.recent_reflections_for(DATABASE_URL, self.current_user, days=days) \
                if (investment_db is not None and DATABASE_URL) else []
            self._send_json({"reflections": reflections})
        elif self.path.startswith("/api/daily-review/list"):
            reviews = investment_db.list_daily_reviews(DATABASE_URL, self.current_user) \
                if (investment_db is not None and DATABASE_URL) else []
            self._send_json({"reviews": reviews})
        elif self.path.startswith("/api/daily-review"):
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            date = params.get("date", [None])[0] or datetime.date.today().isoformat()
            review = investment_db.get_daily_review(DATABASE_URL, self.current_user, date) \
                if (investment_db is not None and DATABASE_URL) else None
            self._send_json({"review": review})
        # ---- 2026-09-10新規（朝一マーケット自動分析システム） ----
        elif self.path.startswith("/api/morning-check/list"):
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            checks = investment_db.list_morning_checks(DATABASE_URL, self.current_user, check_date=params.get("date", [None])[0]) \
                if (investment_db is not None and DATABASE_URL) else []
            self._send_json({"checks": checks})
        elif self.path.startswith("/api/morning-check"):
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            check = investment_db.get_latest_morning_check(DATABASE_URL, self.current_user, check_date=params.get("date", [None])[0]) \
                if (investment_db is not None and DATABASE_URL) else None
            self._send_json({"check": check})
        elif self.path.startswith("/api/position-risk-rules"):
            # 2026-09-10新規（損切りルール是正・最優先修正）：Morning Check・Positions・
            # 通知・利確損切り相談・日次レビューが全て同じこの設定を参照する唯一の真実。
            if investment_db is not None and DATABASE_URL:
                rules = investment_db.get_position_risk_rules(DATABASE_URL, self.current_user)
            elif investment_db is not None:
                rules = investment_db.DEFAULT_POSITION_RISK_RULES
            else:
                rules = {"watch_pct": -6.0, "warning_pct": -7.0, "max_loss_pct": -8.0,
                          "action": "EXIT", "allow_reentry": True, "reentry_requires_new_decision": True}
            self._send_json({"rules": rules})
        # ---- 2026-09-10新規（Market Intelligence Timeline、Phase2-A） ----
        elif self.path.startswith("/api/market-intelligence"):
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            reports = investment_db.list_market_intelligence_reports(DATABASE_URL, self.current_user, trade_date=params.get("date", [None])[0]) \
                if (investment_db is not None and DATABASE_URL) else []
            self._send_json({"reports": reports})
        # ---- 2026-09-09新規（判断エンジン強化：知識の実利用） ----
        elif self.path.startswith("/api/trade-playbooks"):
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            playbooks = investment_db.list_trade_playbooks(DATABASE_URL, self.current_user, status=params.get("status", [None])[0]) \
                if (investment_db is not None and DATABASE_URL) else []
            self._send_json({"playbooks": playbooks})
        elif self.path.startswith("/api/expert-views/relevant"):
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            views = investment_db.relevant_expert_views_for(
                DATABASE_URL, self.current_user, code=params.get("code", [None])[0],
                sector=params.get("sector", [None])[0], market=params.get("market", [None])[0],
            ) if (investment_db is not None and DATABASE_URL) else []
            self._send_json({"views": views})
        elif self.path.startswith("/api/chatgpt-import/list"):
            imports = investment_db.list_chatgpt_imports(DATABASE_URL, self.current_user) if (investment_db is not None and DATABASE_URL) else []
            self._send_json({"imports": imports})
        elif self.path.startswith("/api/chatgpt-daily/list"):
            # v3-9続き（PHASE 6 DAILY CHATGPT JSON IMPORT）：日次JSON取り込み履歴の一覧
            # （既存の投資ログ取り込み=/api/chatgpt-import/listとは別系統・kind='DAILY_DIGEST'のみ）。
            imports = investment_db.list_daily_digest_imports(DATABASE_URL, self.current_user) if (investment_db is not None and DATABASE_URL) else []
            self._send_json({"imports": imports})
        elif self.path.startswith("/api/chatgpt-daily/preview"):
            # ?id=<import_id>。現在のinvestment_rulesと比較した差分プレビューを返すだけで、
            # DBへの書き込みは一切行わない（Diff PreviewとApply Updatesを分離する設計）。
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            import_id = params.get("id", [None])[0]
            if not import_id or not (investment_db is not None and DATABASE_URL):
                self._send_json({"error": "idが必要です"})
                return
            preview = investment_db.preview_daily_digest_updates(DATABASE_URL, self.current_user, int(import_id))
            if preview is None:
                self._send_json({"error": "指定されたimportが見つかりません"})
                return
            self._send_json({"preview": preview})
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
        elif self.path.startswith("/api/sector-regime"):
            # 2026-09-08新規（後場レビューJSON対応：SECTOR_REGIME）。SECTOR_PROXY_METRICSの
            # 各ティッカーについて_intraday_regime()を実行するだけ（新規の重い全市場スキャンは
            # 増やしていない、対象はセクター代表ETF数件のみ）。フロント側は市場時間中に
            # 数分おき、または手動更新で呼ぶ想定。
            regimes = {}
            for key, sym in SECTOR_PROXY_METRICS.items():
                r = _intraday_regime(sym)
                if r:
                    regimes[key] = r
            self._send_json({"regimes": regimes})
        elif self.path.startswith("/api/momentum-scan"):
            # v3-9：🐒 MOMENTUM DAY。「リアルタイムデータを反映」ボタンと同じ設計思想で、
            # ユーザーが明示的にクリックしたときだけ実行する（ページ表示のたびに自動実行はしない。
            # 4000銘柄スキャンは重いため）。force=1でStage1キャッシュを無視して強制再スキャン。
            qs = urllib.parse.urlparse(self.path).query
            force = urllib.parse.parse_qs(qs).get("force", ["0"])[0] == "1"
            print(f"[取得] MOMENTUM DAYスキャン開始（force={force}）…")
            result = run_momentum_day_scan(DATABASE_URL, self.current_user, force=force)
            print(f"  Stage1 {result['stage1CodesScanned']}銘柄スキャン（{result['stage1DurationSec']}秒・"
                  f"リクエスト{result['stage1RequestCount']}回）→ Stage2候補{result['stage1CandidateCount']}件"
                  f"→ 自動登録{result['registeredCount']}件")
            self._send_json(result)
        elif self.path.startswith("/api/break-scan"):
            # v3-9続き：🚀 AUTO_BREAK。MOMENTUM DAYと同じ設計思想（明示クリック時のみ実行）。
            # Stage1（市場全体スキャン）はrun_momentum_stage1のキャッシュをそのまま共有するため、
            # 直近でMOMENTUM DAYスキャン済みなら追加の市場全体取得は発生しない。
            qs = urllib.parse.urlparse(self.path).query
            force = urllib.parse.parse_qs(qs).get("force", ["0"])[0] == "1"
            print(f"[取得] AUTO_BREAKスキャン開始（force={force}）…")
            result = run_break_scan(DATABASE_URL, self.current_user, force=force)
            print(f"  Stage1 {result['stage1CodesScanned']}銘柄スキャン（{result['stage1DurationSec']}秒）→ "
                  f"ブレイク候補{result['stage2ValidCount']}件→自動登録{result['registeredCount']}件"
                  f"（降格{result['demotedToSeenCount']}件）")
            self._send_json(result)
        elif self.path.startswith("/api/rs-scan"):
            # v3-9続き：📈 AUTO_RS。Stage1はMOMENTUM DAY/AUTO_BREAKと共有キャッシュ。
            # Stage2（追加の日足履歴取得）を持たないため3エンジンの中で最も軽量・高速。
            qs = urllib.parse.urlparse(self.path).query
            force = urllib.parse.parse_qs(qs).get("force", ["0"])[0] == "1"
            print(f"[取得] AUTO_RSスキャン開始（force={force}）…")
            result = run_auto_rs_scan(DATABASE_URL, self.current_user, force=force)
            print(f"  Stage1 {result['stage1CodesScanned']}銘柄スキャン（{result['stage1DurationSec']}秒）→ "
                  f"RS候補{result['candidateCount']}件→自動登録{result['registeredCount']}件"
                  f"（降格{result['demotedToSeenCount']}件）")
            self._send_json(result)
        elif self.path.startswith("/api/sector-leader-scan"):
            # v3-9続き：🏆 AUTO_SECTOR_LEADER。Stage1共有・Stage2なし（AUTO_RSと同じく軽量）。
            qs = urllib.parse.urlparse(self.path).query
            force = urllib.parse.parse_qs(qs).get("force", ["0"])[0] == "1"
            print(f"[取得] AUTO_SECTOR_LEADERスキャン開始（force={force}）…")
            result = run_sector_leader_scan(DATABASE_URL, self.current_user, force=force)
            print(f"  Stage1 {result['stage1CodesScanned']}銘柄スキャン（{result['stage1DurationSec']}秒）→ "
                  f"候補{result['candidateCount']}件→自動登録{result['registeredCount']}件"
                  f"（降格{result['demotedToSeenCount']}件）")
            self._send_json(result)
        elif self.path.startswith("/api/pullback-scan"):
            # v3-9続き：🌊 AUTO_PULLBACK。Stage1共有・AUTO_BREAKと同じくStage2あり（日足履歴）。
            qs = urllib.parse.urlparse(self.path).query
            force = urllib.parse.parse_qs(qs).get("force", ["0"])[0] == "1"
            print(f"[取得] AUTO_PULLBACKスキャン開始（force={force}）…")
            result = run_pullback_scan(DATABASE_URL, self.current_user, force=force)
            print(f"  Stage1 {result['stage1CodesScanned']}銘柄スキャン（{result['stage1DurationSec']}秒）→ "
                  f"押し目候補{result['stage2ValidCount']}件→自動登録{result['registeredCount']}件"
                  f"（降格{result['demotedToSeenCount']}件）")
            self._send_json(result)
        elif self.path.startswith("/api/reversal-scan"):
            # v3-9続き（2026-09-07・AUTO_REVERSAL）：🔄 下落後の反転確認。Stage1共有・
            # AUTO_PULLBACKと同じくStage2あり（日足履歴）。
            qs = urllib.parse.urlparse(self.path).query
            force = urllib.parse.parse_qs(qs).get("force", ["0"])[0] == "1"
            print(f"[取得] AUTO_REVERSALスキャン開始（force={force}）…")
            result = run_reversal_scan(DATABASE_URL, self.current_user, force=force)
            print(f"  Stage1 {result['stage1CodesScanned']}銘柄スキャン（{result['stage1DurationSec']}秒）→ "
                  f"反転候補{result['stage2ValidCount']}件→自動登録{result['registeredCount']}件"
                  f"（降格{result['demotedToSeenCount']}件）")
            self._send_json(result)
        elif self.path.startswith("/api/volume-scan"):
            # v3-9続き（2026-09-07・AUTO_VOLUME）：🔥 異常な資金流入の検出。Stage1共有・
            # 他Stage2エンジンと同じ並列構成。時間帯補正（timeAdjustedVolumeRatio）を含む。
            qs = urllib.parse.urlparse(self.path).query
            force = urllib.parse.parse_qs(qs).get("force", ["0"])[0] == "1"
            print(f"[取得] AUTO_VOLUMEスキャン開始（force={force}）…")
            result = run_volume_scan(DATABASE_URL, self.current_user, force=force)
            print(f"  Stage1 {result['stage1CodesScanned']}銘柄スキャン（{result['stage1DurationSec']}秒）→ "
                  f"出来高候補{result['stage2ValidCount']}件→自動登録{result['registeredCount']}件"
                  f"（降格{result['demotedToSeenCount']}件・時間進行度{result['marketTimeProgressRatio']}）")
            self._send_json(result)
        elif self.path.startswith("/api/entry-candidates"):
            # 2026-09-10新規（Market Intelligence Timeline Phase2-C「今買い時TOP5＋Thesis永続化」）：
            # entry_ready_top5（ENTRY_SCOREで選ばれた「今エントリー条件が整っている」候補）と
            # Watch候補を返す。既存の共有Stage1・AUTO_RS/AUTO_SECTOR_LEADER・AUTO_VOLUME Stage2・
            # 個別銘柄5分足判定を再利用（新規の全市場スキャンではない、監視銘柄のみ対象）。
            print("[取得] ENTRY TOP5候補スキャン開始…")
            result = compute_entry_ready_candidates(DATABASE_URL, self.current_user) \
                if (investment_db is not None and DATABASE_URL) else \
                {"entryReadyTop5": [], "watchCandidates": [], "dataQuality": "DEGRADED", "generatedAt": None}
            print(f"  ENTRY TOP5：{len(result['entryReadyTop5'])}件、Watch候補：{len(result['watchCandidates'])}件"
                  f"（dataQuality={result['dataQuality']}）")
            self._send_json(result)
        elif self.path.startswith("/api/stock-theses"):
            # 2026-09-10新規（Phase2-C）：成績評価画面向け。?days=（既定30）で集計期間指定、
            # 一覧は?from=&to=で絞り込み可能。
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            from_date = params.get("from", [None])[0]
            to_date = params.get("to", [None])[0]
            days = int(params.get("days", ["30"])[0])
            if investment_db is not None and DATABASE_URL:
                theses = investment_db.list_stock_theses(DATABASE_URL, self.current_user, from_date=from_date, to_date=to_date)
                stats = investment_db.get_stock_thesis_stats(DATABASE_URL, self.current_user, days=days)
            else:
                theses, stats = [], {"totalFinalized": 0, "byResult": {}, "winRate": None}
            self._send_json({"theses": theses, "stats": stats})
        elif self.path.startswith("/api/social-posts/status"):
            # 2026-09-10新規（にこそく@nicosokufx X投稿連携、指示書19番）：X_SOURCE_STATUS表示用。
            src = investment_db.get_market_source(DATABASE_URL, NICOSOKU_X_USERNAME) \
                if (investment_db is not None and DATABASE_URL) else None
            if not X_API_BEARER_TOKEN:
                status_label = "DEGRADED"
                reason = "X_API_BEARER_TOKEN未設定"
            elif src and src.get("last_error"):
                status_label = "DEGRADED"
                reason = src.get("last_error")
            else:
                status_label = "OK"
                reason = None
            self._send_json({"status": status_label, "reason": reason,
                              "lastSuccessAt": (src or {}).get("last_success_at"),
                              "lastSeenPostId": (src or {}).get("last_seen_post_id")})
        elif self.path.startswith("/api/social-posts") and "/image-analysis" not in self.path:
            # 2026-09-10新規（にこそく@nicosokufx X投稿連携、指示書14番）：日本市場画面の
            # 「X 市場情報」カード向け。?limit=&min_importance=。Phase2（指示書5・6・7・8番）：
            # analysis_priority_score・priority_label・並び替え・pendingSummaryを追加。
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            limit = int(params.get("limit", ["10"])[0])
            min_importance = params.get("min_importance", [None])[0]
            resp = build_social_posts_response(DATABASE_URL, self.current_user, limit=limit, min_importance=min_importance)
            self._send_json(resp)
        elif self.path.startswith("/api/social-sources/nicosoku/diagnostics"):
            # Phase2新規（指示書11番）：実X API疎通確認用の診断エンドポイント。
            diag = nicosoku_diagnostics(DATABASE_URL, self.current_user) \
                if (investment_db is not None and DATABASE_URL) else {"token_configured": bool(X_API_BEARER_TOKEN)}
            self._send_json(diag)
        elif self.path.startswith("/api/social-signals"):
            # 2026-09-10新規：recent_social_market_signals（指示書9番、ChatGPT相談JSON補助情報）。
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            lookback = int(params.get("lookback_minutes", ["180"])[0])
            min_importance = params.get("min_importance", ["MEDIUM"])[0]
            signals = get_recent_social_market_signals(DATABASE_URL, self.current_user,
                                                         lookback_minutes=lookback, min_importance=min_importance) \
                if (investment_db is not None and DATABASE_URL) else []
            self._send_json({"recent_social_market_signals": signals})
        elif self.path.startswith("/api/auto-signal-events"):
            # v3-9続き（PHASE 1 AUTO SIGNAL LOG）：検証・確認用の閲覧API。?code=・?signal_type=で絞り込み可能。
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            code = params.get("code", [None])[0]
            signal_type = params.get("signal_type", [None])[0]
            events = investment_db.list_auto_signal_events(DATABASE_URL, self.current_user, code=code, signal_type=signal_type) if (investment_db is not None and DATABASE_URL) else []
            self._send_json({"events": events})
        elif self.path.startswith("/api/market-events"):
            # v3-9続き（PHASE 3 EVENT/EARNINGS INTELLIGENCE）：?from=&to=（ISO日付）で絞り込み可能。
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            from_date = params.get("from", [None])[0]
            to_date = params.get("to", [None])[0]
            events = investment_db.list_market_events(DATABASE_URL, self.current_user, from_date=from_date, to_date=to_date) if (investment_db is not None and DATABASE_URL) else []
            self._send_json({"events": events})
        elif self.path.startswith("/api/smart-import/history"):
            # 2026-09-10新規（Unified Smart Import、Phase SI-D、指示書14番）：Smart Import経由
            # で登録された最近の項目一覧。専用テーブルは持たず既存一覧関数から合成するだけ。
            history = smart_import_recent_activity(DATABASE_URL, self.current_user) \
                if (investment_db is not None and DATABASE_URL) else []
            self._send_json({"history": history})
        elif self.path.startswith("/api/news-catalysts"):
            # v3-9続き（PHASE 4 NEWS/CATALYST INTELLIGENCE）：?from=&to=（catalyst_dateのISO日付）・
            # ?category=で絞り込み可能。
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            from_date = params.get("from", [None])[0]
            to_date = params.get("to", [None])[0]
            category = params.get("category", [None])[0]
            catalysts = investment_db.list_news_catalysts(DATABASE_URL, self.current_user, from_date=from_date, to_date=to_date, category=category) if (investment_db is not None and DATABASE_URL) else []
            self._send_json({"catalysts": catalysts})
        elif self.path.startswith("/api/expert-views"):
            # v3-9続き（PHASE 5 EXPERT INTELLIGENCE）：?expert=&from=&to=（published_atのISO日付）・
            # ?market=&stock=で絞り込み可能。
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            expert = params.get("expert", [None])[0]
            from_date = params.get("from", [None])[0]
            to_date = params.get("to", [None])[0]
            market = params.get("market", [None])[0]
            stock = params.get("stock", [None])[0]
            views = investment_db.list_expert_views(DATABASE_URL, self.current_user, expert=expert, from_date=from_date, to_date=to_date, market=market, stock=stock) if (investment_db is not None and DATABASE_URL) else []
            self._send_json({"views": views})
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
            # v3-9：一覧を返す前に期限切れauto_tagsを掃除する（遅延評価。専用のcronは持たないため、
            # アクセスされたタイミングで軽くチェックする方式。手動登録銘柄・保有ポジションは
            # cleanup_expired_auto_tags側の判定で絶対に削除されない）。
            if investment_db is not None and DATABASE_URL:
                try:
                    investment_db.cleanup_expired_auto_tags(DATABASE_URL, self.current_user)
                except Exception as e:
                    print("  auto_tags期限切れ掃除に失敗（一覧取得は続行）", e)
            qs = urllib.parse.urlparse(self.path).query
            market = urllib.parse.parse_qs(qs).get("market", [None])[0]
            items = investment_db.list_watchlist(DATABASE_URL, self.current_user, market=market) if (investment_db is not None and DATABASE_URL) else []
            self._send_json({"items": items})
        elif self.path.startswith("/api/portfolio"):
            # v3-2新規：portfolio（保有株、localStorageに無かった新規機能）
            items = investment_db.list_portfolio(DATABASE_URL, self.current_user) if (investment_db is not None and DATABASE_URL) else []
            self._send_json({"items": items})
        elif self.path.startswith("/api/trade-history"):
            # 2026-09-07新規：売却確定で自動記録される取引履歴（journalとは別、機械的な実現損益ログ）。
            trades = investment_db.list_trade_history(DATABASE_URL, self.current_user) if (investment_db is not None and DATABASE_URL) else []
            self._send_json({"trades": trades})
        elif self.path.startswith("/api/investment-totals"):
            # 2026-09-07新規：通算実現損益（初期値＋trade_history合計、毎回再計算）。
            totals = investment_db.get_investment_totals(DATABASE_URL, self.current_user) if (investment_db is not None and DATABASE_URL) else {"initialRealizedPnl":0,"totalRealizedPnl":0}
            self._send_json(totals)
        elif self.path.startswith("/api/position-live"):
            # 2026-09-07新規（ポジション→リアルタイム売却判断画面 Phase1）：ポジションカードの
            # 「リアルタイム」展開エリアを開いている間だけ、その1銘柄だけをオンデマンドで取得する。
            # 既存の全銘柄一括ポーリング（stockQuotes）とは完全に別経路にすることで、展開していない
            # ポジションや監視銘柄まで巻き込んだ高頻度リクエストにならないようにする。
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            code = (params.get("code", [""])[0] or "").strip()
            market = params.get("market", ["JP"])[0] or "JP"
            if not code:
                self._send_json({"error": "codeは必須です"})
            else:
                # 2026-09-07追加（実機バグ修正）：関数内の想定外の例外で空bodyのまま接続が切れると、
                # フロント側のres.json()が「Unexpected end of JSON input」で落ちる不具合が実機で
                # 発生したため、想定外の例外もここで捕まえて必ずJSONを返す（get_position_live_detail
                # 自体は既に個別のtry/exceptを持つが、その外側の想定外エラーに対する保険）。
                try:
                    self._send_json(get_position_live_detail(code, market))
                except Exception as e:
                    print("  /api/position-live 想定外のエラー", code, e)
                    self._send_json({"error": f"サーバー内部エラー: {e}"})
        elif self.path.startswith("/api/position-intraday-chart"):
            # 2026-09-07新規（ポジション→リアルタイム売却判断画面 Phase2）：展開中の1銘柄だけの
            # 当日5分足チャート。/api/position-liveとは別経路・別ポーリング間隔にする
            # （チャートはyfinance側のレート制限がより厳しいため、価格ティッカーより低頻度で
            # フロント側がポーリングする設計）。
            # 2026-09-09更新（監視銘柄/市場チャートの時間足切替）：periodを追加受付。省略時は
            # 従来通り"1d"（ポジション画面の呼び出しは変更していないため挙動は完全に不変）。
            # 監視銘柄・日本市場一覧等の共通チャートモーダルも、名称は"position-intraday-chart"
            # のままだがcode/market/interval/periodだけのポジション非依存API（実データはget_
            # position_intraday_chart内でも銘柄コード・市場からシンボル解決するだけ）のため、
            # 新しいエンドポイントを増やさずそのまま再利用する（指示書「共通チャート取得関数を
            # 利用」対応）。intervalは既知の値のみ許可する。
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            code = (params.get("code", [""])[0] or "").strip()
            market = params.get("market", ["JP"])[0] or "JP"
            interval = params.get("interval", ["5m"])[0] or "5m"
            if interval not in ("1m", "5m", "15m", "60m"):
                interval = "5m"
            period = params.get("period", [None])[0] or None
            if not code:
                self._send_json({"error": "codeは必須です"})
            else:
                try:
                    self._send_json(get_position_intraday_chart(code, market, interval, period))
                except Exception as e:
                    print("  /api/position-intraday-chart 想定外のエラー", code, e)
                    self._send_json({"error": f"サーバー内部エラー: {e}", "bars": []})
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
        elif self.path.split("?", 1)[0] in ("/manifest.json", "/trade_icon_512.png"):
            # v3-9続き（2026-09-05・PHASE 7 MOBILE/PWA）：manifest.json・アイコンだけ追加で
            # ホワイトリスト許可する（上と同じ「必要なファイルだけ明示許可」方針を維持）。
            super().do_GET()
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
        # ---- 2026-09-09新規（ルール学習システム） ----
        elif self.path == "/api/trade-rules/evaluate":
            # 指示書6番：ルール1件を特定の日でSUPPORTED/FAILED/NEUTRAL/NOT_APPLICABLE評価する。
            if not self._investment_db_ready():
                return
            body = self._read_json_body()
            result = investment_db.record_rule_evaluation(
                DATABASE_URL, self.current_user, body.get("id"), body.get("evalResult"),
                eval_date=body.get("evalDate"), note=body.get("note"), source="manual",
            )
            if result is None:
                self._send_json({"error": "評価に失敗しました（idまたはevalResultを確認してください）"}); return
            self._send_json(result)
        elif self.path == "/api/trade-rules/update":
            # 指示書16番：手動操作（昇格/差し戻し/REVISED/RETIRED・信頼度変更・文章修正・
            # 例外追加・アクション修正）。指示書17番：変更履歴はrecord_rule_evaluation/
            # update_trade_rule内部でtrade_rule_historyへ自動記録される。
            if not self._investment_db_ready():
                return
            body = self._read_json_body()
            ok = investment_db.update_trade_rule(
                DATABASE_URL, self.current_user, body.get("id"), body.get("fields") or {},
                reason=body.get("reason"), source="manual",
            )
            if not ok:
                self._send_json({"error": "更新に失敗しました（idを確認してください）"}); return
            self._send_json({"ok": True})
        elif self.path == "/api/trade-rules/revise":
            # 指示書8番：既存ルールをREVISEDへ落とし、修正版ルールを新規に派生させる。
            if not self._investment_db_ready():
                return
            body = self._read_json_body()
            result = investment_db.create_revised_trade_rule(
                DATABASE_URL, self.current_user, body.get("parentId"), body.get("newRuleText"),
                reason=body.get("reason"), category=body.get("category"), action_text=body.get("actionText"),
            )
            if result is None:
                self._send_json({"error": "修正版ルールの作成に失敗しました（parentIdを確認してください）"}); return
            self._send_json(result)
        elif self.path == "/api/trade-rules/migrate":
            # 指示書1・5番：過去のrule_updates・investment_rulesからの初期移行、およびA〜D
            # ルール・既存の長期ルールの登録。rule_keyのUNIQUE制約により何度実行しても安全
            # （新しい過去データが増えていれば追加で拾うだけの冪等処理）。
            if not self._investment_db_ready():
                return
            result = investment_db.migrate_legacy_rules_to_trade_rules(DATABASE_URL, self.current_user)
            self._send_json(result)
        # ---- 2026-09-09新規（日次投資レビュー・投資スコア、指示書Phase4・5） ----
        elif self.path == "/api/daily-review/generate":
            if not self._investment_db_ready():
                return
            body = self._read_json_body()
            date = body.get("date") or datetime.date.today().isoformat()
            review = investment_db.generate_daily_review(DATABASE_URL, self.current_user, date)
            if review is None:
                self._send_json({"error": "レビュー生成に失敗しました"}); return
            self._send_json({"review": review})
        elif self.path == "/api/daily-review/feedback":
            # 指示書18・19番：ユーザー感想を保存し、翌日以降の分析（recent_reflections_for）へ
            # 使えるようにする。保存と同時にreflection_tagsを抽出し、その日のスコアも再計算する
            # （損切り遅れ・利確遅れ等の自己申告がスコアの利確損切り軸に反映されるため）。
            if not self._investment_db_ready():
                return
            body = self._read_json_body()
            date = body.get("date") or datetime.date.today().isoformat()
            feedback = body.get("feedback") or ""
            review = investment_db.save_review_user_feedback(DATABASE_URL, self.current_user, date, feedback)
            if review is None:
                self._send_json({"error": "感想の保存に失敗しました"}); return
            self._send_json({"review": review})
        # ---- 2026-09-09新規（判断エンジン強化：知識の実利用） ----
        elif self.path == "/api/knowledge-context":
            # 指示書18・19・24番：分析直前に呼ぶ統合コンテキストビルダー。総合判断・確信度・
            # 理由も同時に生成し、used_context_jsonをanalysis_context_logへ記録する
            # （日次レビューのknown_risk_ignored検出で再利用するため、副作用のあるPOSTにした）。
            # 2026-09-09更新（判断エンジン全画面統合、指示書1・5・6・7番）：scope
            # （"entry"省略時デフォルト|"exit"|"overnight"）で朝一・個別銘柄分析向けの
            # 新規買い判断（synthesize_trade_judgment）と、ポジション/利確損切り相談向けの
            # EXIT判断（evaluate_exit_judgment）、持ち越し判断専用（evaluate_overnight_decision）
            # を同じエンドポイント・同じcontext builderから出し分ける（画面ごとの独自簡易
            # ルール抽出を廃止し、共通context builderへ寄せる）。
            if not self._investment_db_ready():
                return
            body = self._read_json_body()
            code = body.get("code")
            scope = body.get("scope") or "entry"
            context = investment_db.build_relevant_trading_context(
                DATABASE_URL, self.current_user, stock_code=code, sector=body.get("sector"),
                market=body.get("market"), position=body.get("position"),
                analysis_type=body.get("analysisType") or "stock", signals=body.get("signals") or {},
                rule_categories=body.get("ruleCategories"),
            )
            if scope == "exit":
                risk_rules = investment_db.get_position_risk_rules(DATABASE_URL, self.current_user)
                judgment = investment_db.evaluate_exit_judgment(context, position=body.get("position"), risk_rules=risk_rules)
            elif scope == "overnight":
                pos = body.get("position") or {}
                overnight = investment_db.evaluate_overnight_decision(
                    DATABASE_URL, self.current_user, code, context,
                    unrealized_pnl_pct=pos.get("unrealized_pnl_pct"), market_condition=body.get("marketCondition"),
                    sector_rs=body.get("sectorRs"))
                judgment = {"judgment": overnight["decision"], "confidence": None,
                            "reasons": overnight["reasons"], "risk_flags": [], "score": overnight["score"]}
            else:
                judgment = investment_db.synthesize_trade_judgment(context, base_signal=body.get("baseSignal"))
            log = None
            if code:
                log = investment_db.save_analysis_context_log(
                    DATABASE_URL, self.current_user, code, body.get("analysisType") or scope,
                    judgment, context, analysis_date=body.get("date"))
            self._send_json({"context": context, "judgment": judgment, "logId": (log or {}).get("id")})
        elif self.path == "/api/knowledge-context/top5-flags":
            # 指示書4番：TOP5相談向けの軽量な候補別フラグ（フルコンテキストは取得しない）。
            if not self._investment_db_ready():
                return
            body = self._read_json_body()
            codes = body.get("codes") or []
            sector_map = body.get("sectorMap") or {}
            flags = investment_db.lightweight_context_flags_for_codes(DATABASE_URL, self.current_user, codes, sector_map=sector_map)
            self._send_json({"flags": flags})
        elif self.path == "/api/expert-views/evaluate":
            # 指示書2・28番：有識者見解1件をSUPPORTED/FAILED/NEUTRALで評価する。
            if not self._investment_db_ready():
                return
            body = self._read_json_body()
            view = investment_db.record_expert_view_evaluation(DATABASE_URL, self.current_user, body.get("id"), body.get("result"))
            if view is None:
                self._send_json({"error": "評価に失敗しました"}); return
            self._send_json({"view": view})
        elif self.path == "/api/expert-views/refresh-status":
            # 指示書1番：全有識者見解のstatusを現在日付・評価実績で再判定する。
            if not self._investment_db_ready():
                return
            n = investment_db.refresh_expert_view_statuses(DATABASE_URL, self.current_user)
            self._send_json({"updated": n})
        elif self.path == "/api/position-risk-rules/save":
            # 2026-09-10新規：将来ユーザーが閾値を変更できるようにするための保存経路
            # （今回のUIからは呼ばないが、共通設定を1箇所にまとめる設計のため用意する）。
            if not self._investment_db_ready():
                return
            body = self._read_json_body()
            saved = investment_db.save_position_risk_rules(DATABASE_URL, self.current_user, body)
            self._send_json({"rules": saved})
        elif self.path == "/api/trade-rules/generate-from-reflections":
            # 指示書16番：直近の反省の繰り返しからTESTINGルール候補を生成する。
            if not self._investment_db_ready():
                return
            created = investment_db.generate_rule_candidates_from_reflections(DATABASE_URL, self.current_user)
            self._send_json({"candidates": created})
        elif self.path == "/api/morning-check/generate":
            # 指示書21番：定時以外でも現在時点の臨時レポートを作成する手動更新（MANUAL）。
            # スケジューラが呼ぶ定時生成もsnapshot_time（T0530等）を指定してこの同じ関数を
            # 呼ぶだけで、生成ロジックの二重実装はしない。
            if not self._investment_db_ready():
                return
            body = self._read_json_body()
            snapshot_time = body.get("snapshotTime") or "MANUAL"
            try:
                check = generate_morning_market_check(DATABASE_URL, self.current_user, snapshot_time)
            except Exception as e:
                import traceback
                print("  /api/morning-check/generate 想定外のエラー")
                traceback.print_exc()
                self._send_json({"fatalError": f"{type(e).__name__}: {e}"})
                return
            self._send_json({"check": check})
        elif self.path == "/api/morning-check/mark-read":
            if not self._investment_db_ready():
                return
            body = self._read_json_body()
            ok = investment_db.mark_morning_check_read(DATABASE_URL, self.current_user, body.get("id"))
            self._send_json({"ok": ok})
        elif self.path == "/api/market-intelligence/generate":
            # 2026-09-10新規（Market Intelligence Timeline、Phase2-Bで4時間帯すべてに対応）：
            # 指示書6番「今すぐ分析」手動再生成。UPSERTなので同一report_typeの重複レコードは
            # 作らない。
            if not self._investment_db_ready():
                return
            body = self._read_json_body()
            report_type = body.get("reportType") or "OPENING_30M"
            if report_type not in INTRADAY_REPORT_SNAPSHOT_TIMES:
                self._send_json({"fatalError": f"未対応のreport_type: {report_type}"})
                return
            try:
                report = generate_intraday_report(DATABASE_URL, self.current_user, report_type)
            except Exception as e:
                import traceback
                print("  /api/market-intelligence/generate 想定外のエラー")
                traceback.print_exc()
                self._send_json({"fatalError": f"{type(e).__name__}: {e}"})
                return
            self._send_json({"report": report})
        elif self.path == "/api/trade-playbooks/backfill":
            # 指示書11・12番：expert_viewsの構造化データからplaybook候補を生成する
            # （常にTESTING/LOW、自動ACTIVE禁止）。
            if not self._investment_db_ready():
                return
            created = investment_db.backfill_playbook_candidates_from_expert_views(DATABASE_URL, self.current_user)
            self._send_json({"created": created})
        elif self.path == "/api/trade-playbooks/similar":
            # 指示書13番：条件の正規化類似度で重複候補を検出する（自動統合・削除はしない）。
            if not self._investment_db_ready():
                return
            body = self._read_json_body()
            candidates = investment_db.find_similar_trade_playbooks(
                DATABASE_URL, self.current_user, body.get("playbook") or {}, exclude_id=body.get("excludeId"))
            self._send_json({"candidates": candidates})
        # ---- ChatGPT連携（2026-09-02新規、Phase1）：有料AI APIは使わず、ChatGPTが出力した
        # 投資ログJSONを手動貼り付けで取り込む。 ----
        elif self.path == "/api/chatgpt-import/save":
            # 2026-09-08追加（「NEONへ保存」失敗の調査）：想定外の例外がこのルート内で発生すると、
            # 従来はレスポンスを一切送らないまま接続が切れ、フロント側は「サーバーが起動して
            # いるか確認してください」という誤解を招く一律のメッセージしか出せなかった
            # （実際にはサーバーは動いていて、保存処理の途中で例外が起きていただけのケースを
            # 区別できなかった）。想定外の例外を捕まえて必ずJSONで実際のエラー内容を返すように
            # した（fatalErrorキーで既存のerrors/error（バリデーション・重複）とは区別する）。
            if not self._investment_db_ready():
                return
            try:
                body = self._read_json_body()
                payload = body.get("payload")
                force = bool(body.get("force"))
                errors = investment_db.validate_chatgpt_payload(payload)
                if errors:
                    self._send_json({"errors": errors})
                    return
                # 2026-09-09更新（ChatGPT統合連携、指示書1・2番）：唯一の取り込み口として、
                # 既存のsave_chatgpt_import()（無変更）を内部で呼びつつevents/news/catalysts/
                # expert_opinions/user_feedbackがあれば自動振り分けするsave_chatgpt_unified_
                # import()へ切り替えた。従来の必須キーだけのJSONでも戻り値の形は完全互換
                # （unified/classificationキーが追加されるだけ）。
                result = investment_db.save_chatgpt_unified_import(DATABASE_URL, self.current_user, payload, force=force)
                self._send_json(result)
            except Exception as e:
                import traceback
                print("  /api/chatgpt-import/save 想定外のエラー")
                traceback.print_exc()
                self._send_json({"fatalError": f"{type(e).__name__}: {e}"})
        elif self.path == "/api/chatgpt-import/preview":
            # 2026-09-09新規（ChatGPT統合連携、指示書5番）：保存前に分類結果だけをプレビュー
            # する（DBへの書き込みは一切しない）。既存validate_chatgpt_payloadと同じ
            # payloadを渡す。
            if not self._investment_db_ready():
                return
            body = self._read_json_body()
            payload = body.get("payload")
            classified = investment_db.classify_chatgpt_unified_payload(payload)
            self._send_json({"counts": classified["counts"]})
        elif self.path == "/api/chatgpt-daily/import":
            # v3-9続き（PHASE 6 DAILY CHATGPT JSON IMPORT）：STEP1「Import（履歴保存）」のみ。
            # updates=[]でもinvestment_rules等には一切書き込まない（apply_status='NO_UPDATES'
            # を保存するだけ）。updatesがあってもここでは保存するだけで適用はしない
            # （STEP3の/api/chatgpt-daily/applyを明示的に呼ぶまで反映しない）。
            if not self._investment_db_ready():
                return
            body = self._read_json_body()
            payload = body.get("payload")
            force = bool(body.get("force"))
            errors = investment_db.validate_daily_digest_payload(payload)
            if errors:
                self._send_json({"errors": errors})
                return
            result = investment_db.save_daily_digest_import(DATABASE_URL, self.current_user, payload, force=force)
            self._send_json(result)
        elif self.path == "/api/chatgpt-daily/apply":
            # STEP3「Apply Updates（明示的に差分だけ適用）」。適用直前に必ずinvestment_rulesの
            # 現在値を再取得して差分判定をやり直す（クライアント側の古いプレビューは信用しない）。
            # NO_CHANGE・WARNING_PROTECTED（SWING -10%等の保護ルール）・UNSUPPORTED_TARGET
            # （investment_rules.*以外）は自動適用しない。
            if not self._investment_db_ready():
                return
            body = self._read_json_body()
            import_id = body.get("importId")
            if not import_id:
                self._send_json({"error": "importIdが必要です"})
                return
            result = investment_db.apply_daily_digest_updates(DATABASE_URL, self.current_user, int(import_id))
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
        elif self.path == "/api/market-events/import":
            # v3-9続き（PHASE 3 EVENT/EARNINGS INTELLIGENCE）：ChatGPTで画像→JSON化したイベント
            # 一覧を貼り付けてimportする。body: {"events": [{event_date,title,...}, ...]}。
            # 画像由来の情報を確定情報として扱わないため、verification_status未指定は
            # UNVERIFIEDになる（investment_db.import_market_events側の既定）。
            if not self._investment_db_ready():
                return
            body = self._read_json_body()
            events = body.get("events")
            if not isinstance(events, list):
                self._send_json({"error": "eventsは配列で指定してください"})
                return
            result = investment_db.import_market_events(DATABASE_URL, self.current_user, events)
            self._send_json(result)
        elif self.path == "/api/market-events/delete":
            if not self._investment_db_ready():
                return
            body = self._read_json_body()
            investment_db.delete_market_event(DATABASE_URL, self.current_user, body.get("id"))
            self._send_json({"ok": True})
        elif self.path == "/api/news-catalysts/import":
            # v3-9続き（PHASE 4 NEWS/CATALYST INTELLIGENCE）：ChatGPT等で構造化したカタリスト
            # （マクロ・セクター・銘柄材料や指数採用・資金フロー等）一覧を貼り付けてimportする。
            # body: {"catalysts": [{catalyst_date,title,...}, ...]}。ニュース本体（RSS/TDnet）は
            # 引き続きこのテーブルに保存しない（都度取得のまま）。画像由来の情報を確定情報として
            # 扱わないため、verification_status未指定はUNVERIFIEDになる
            # （investment_db.import_news_catalysts側の既定）。
            if not self._investment_db_ready():
                return
            body = self._read_json_body()
            catalysts = body.get("catalysts")
            if not isinstance(catalysts, list):
                self._send_json({"error": "catalystsは配列で指定してください"})
                return
            result = investment_db.import_news_catalysts(DATABASE_URL, self.current_user, catalysts)
            self._send_json(result)
        elif self.path == "/api/news-catalysts/delete":
            if not self._investment_db_ready():
                return
            body = self._read_json_body()
            investment_db.delete_news_catalyst(DATABASE_URL, self.current_user, body.get("id"))
            self._send_json({"ok": True})
        elif self.path == "/api/expert-views/import":
            # v3-9続き（PHASE 5 EXPERT INTELLIGENCE）：ChatGPTでYouTube/インタビュー等を要約した
            # 有識者見解一覧を貼り付けてimportする。body: {"views": [{expert_name,published_at,...}, ...]}。
            # 売買シグナルへ直接変換しない・AUTOロジックを書き換えない設計のため、importしても
            # Primary/Action Statusやスコア計算には一切影響しない。verification_status未指定は
            # UNVERIFIEDになる（investment_db.import_expert_views側の既定）。
            if not self._investment_db_ready():
                return
            body = self._read_json_body()
            views = body.get("views")
            if not isinstance(views, list):
                self._send_json({"error": "viewsは配列で指定してください"})
                return
            result = investment_db.import_expert_views(DATABASE_URL, self.current_user, views)
            self._send_json(result)
        elif self.path == "/api/expert-views/delete":
            if not self._investment_db_ready():
                return
            body = self._read_json_body()
            investment_db.delete_expert_view(DATABASE_URL, self.current_user, body.get("id"))
            self._send_json({"ok": True})
        elif self.path == "/api/smart-import/preview":
            # 2026-09-10新規（Unified Smart Import、Phase SI-A）：文章・JSON・箇条書き・
            # ChatGPT出力等をそのまま貼り付け、自動判別した候補一覧を返す（DB保存はしない、
            # 指示書4番STEP1〜6のプレビュー段階）。
            body = self._read_json_body()
            raw_text = body.get("text") or ""
            try:
                candidates = classify_content(raw_text, DATABASE_URL, self.current_user)
                if investment_db is not None and DATABASE_URL:
                    candidates = smart_import_check_duplicates(DATABASE_URL, self.current_user, candidates)
            except Exception as e:
                import traceback
                print("  /api/smart-import/preview 想定外のエラー")
                traceback.print_exc()
                self._send_json({"fatalError": f"{type(e).__name__}: {e}"})
                return
            self._send_json({"candidates": candidates})
        elif self.path == "/api/smart-import/confirm":
            # 2026-09-10新規（Unified Smart Import、Phase SI-A）：プレビューでユーザーが選択・
            # 編集した候補を確定保存する（指示書4番STEP7）。カテゴリごとに既存のimport_*へ
            # 振り分けるだけで、SmartImport独自のテーブルは持たない（指示書18番）。
            if not self._investment_db_ready():
                return
            body = self._read_json_body()
            candidates = body.get("candidates")
            if not isinstance(candidates, list):
                self._send_json({"error": "candidatesは配列で指定してください"})
                return
            import_source = body.get("importSource") or "manual"
            result = smart_import_confirm(DATABASE_URL, self.current_user, candidates, import_source)
            self._send_json(result)
        elif self.path == "/api/social-posts/image-analysis":
            # 2026-09-10新規（にこそく@nicosokufx X投稿連携、指示書4番）：画像の構造化解析は
            # OCR/画像認識APIを新規導入せず、ユーザーがChatGPT等で解析した結果をJSON貼り付けで
            # 保存する（既存のSmart Import/ChatGPT連携と同じパターン）。body: {postId, imageAnalysis}
            # （imageAnalysisは[{image_type,market,observations,stocks},...]形式の配列）。
            if not self._investment_db_ready():
                return
            body = self._read_json_body()
            post_id = body.get("postId")
            image_analysis = body.get("imageAnalysis")
            if not post_id or not isinstance(image_analysis, list):
                self._send_json({"error": "postId・imageAnalysis（配列）は必須です"})
                return
            saved = investment_db.save_social_post_image_analysis(DATABASE_URL, NICOSOKU_X_USERNAME, post_id, image_analysis)
            if saved is None:
                self._send_json({"error": "対象の投稿が見つかりません"})
            else:
                self._send_json({"post": saved})
        elif self.path == "/api/social-posts/skip":
            # Phase2新規（指示書3番）：PENDING投稿を「解析不要」としてSKIPPEDへ変更する。
            # body: {postId}
            if not self._investment_db_ready():
                return
            body = self._read_json_body()
            post_id = body.get("postId")
            if not post_id:
                self._send_json({"error": "postIdは必須です"})
                return
            saved = investment_db.set_social_post_image_analysis_status(DATABASE_URL, NICOSOKU_X_USERNAME, post_id, "SKIPPED")
            if saved is None:
                self._send_json({"error": "対象の投稿が見つかりません"})
            else:
                self._send_json({"post": saved})
        elif self.path == "/api/social-posts/reanalyze":
            # Phase2新規（指示書4番）：ANALYZED/FAILED/SKIPPEDの投稿をPENDINGへ戻す
            # （image_analysis_json自体は削除しない、再解析結果保存時に上書きされるだけ）。
            # body: {postId}
            if not self._investment_db_ready():
                return
            body = self._read_json_body()
            post_id = body.get("postId")
            if not post_id:
                self._send_json({"error": "postIdは必須です"})
                return
            saved = investment_db.set_social_post_image_analysis_status(DATABASE_URL, NICOSOKU_X_USERNAME, post_id, "PENDING")
            if saved is None:
                self._send_json({"error": "対象の投稿が見つかりません"})
            else:
                self._send_json({"post": saved})
        elif self.path == "/api/social-sources/nicosoku/fetch-now":
            # Phase2新規（指示書12番）：診断用の手動「今すぐ取得」。既存pollerロジック
            # （nicosoku_poll_once）をそのまま1回呼ぶだけで、別実装は作らない。
            if not self._investment_db_ready():
                return
            result = nicosoku_poll_once(DATABASE_URL, self.current_user)
            self._send_json({"ok": result.get("status") == "ok", "status": result.get("status"),
                              "fetched": result.get("fetched", 0), "inserted": result.get("newPosts", 0),
                              "duplicates": result.get("duplicates", 0), "error": result.get("error")})
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
        elif self.path == "/api/portfolio/add-entry":
            # 2026-09-07新規（監視銘柄→ポジション連携）：監視銘柄カードの「ポジション追加」/
            # 保有カードの「買い増し」から呼ぶ。既存ポジションがあれば加重平均で合算する。
            if not self._investment_db_ready():
                return
            body = self._read_json_body()
            updated = investment_db.add_position_entry(
                DATABASE_URL, self.current_user, body.get("code"), body.get("name"),
                body.get("market") or "JP", body.get("price"), body.get("shares"), body.get("trade_style"))
            if updated is None:
                self._send_json({"error": "買値・枚数は正の数で指定してください"})
                return
            self._send_json({"position": updated})
        elif self.path == "/api/portfolio/exit":
            # 2026-09-07新規：保有カードの「売却」確定から呼ぶ。実現損益を計算しtrade_historyへ
            # 記録、全株売却ならportfolioの行を削除する（取引履歴は削除しない）。
            if not self._investment_db_ready():
                return
            body = self._read_json_body()
            result = investment_db.add_position_exit(
                DATABASE_URL, self.current_user, body.get("code"), body.get("market") or "JP",
                body.get("exitPrice"), body.get("shares"))
            # 2026-09-09追加（判断エンジン全画面統合、指示書8番）：売却が成功した場合のみ、
            # ベストエフォートでplaybook実績を更新する（add_position_exit自体は無変更、
            # 失敗してもここで例外を握りつぶし売却結果のレスポンスには影響させない）。
            if result and "error" not in result and result.get("trade"):
                try:
                    investment_db.record_trade_outcome_for_playbooks(
                        DATABASE_URL, self.current_user, body.get("code"), result["trade"])
                except Exception:
                    pass
            self._send_json(result)
        elif self.path == "/api/investment-totals/set-initial":
            # 2026-09-07新規：通算実現損益の初期値をユーザーが最初に手入力するためのAPI。
            if not self._investment_db_ready():
                return
            body = self._read_json_body()
            investment_db.set_initial_realized_pnl(DATABASE_URL, self.current_user, body.get("value"))
            self._send_json(investment_db.get_investment_totals(DATABASE_URL, self.current_user))
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
        # 2026-09-10新規（朝一マーケット自動分析システム、指示書2番）：定時スケジューラを
        # デーモンスレッドで起動する。サーバーが起動している間だけ機能する
        # （start.bat/サーバー常駐が前提、CLAUDE.md「使用中は閉じない」と整合）。
        threading.Thread(target=_morning_check_scheduler_loop, daemon=True).start()
        # 2026-09-10新規（Market Intelligence Timeline、指示書4番）：09:30 OPENING_30Mの
        # 定時スケジューラ（Phase2-A範囲）。別スレッドに分離し、Morning Checkのスケジューラが
        # 万一詰まってもこちらは独立して動く（指示書31番のサービス分離方針）。
        threading.Thread(target=_intraday_report_scheduler_loop, daemon=True).start()
        # 2026-09-10新規（にこそく@nicosokufx X投稿連携、指示書2番）：X_API_BEARER_TOKEN
        # 未設定なら_nicosoku_poll_scheduler_loop内で即returnする（アプリ本体には影響しない）。
        threading.Thread(target=_nicosoku_poll_scheduler_loop, daemon=True).start()
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
        # 2026-09-07新規：Tailscaleが接続済みならTailscale IP（外出先・別Wi-Fi・4G/5Gからでも
        # アクセス可能）も案内する。未インストール・未接続なら何も表示しない（起動は妨げない）。
        ts = _tailscale_status()
        if ts:
            print("  --- 外出先（別Wi-Fi・4G/5G）から使う場合（Tailscale接続が必要） ---")
            print(f"  http://{ts['ip']}:{PORT}/trade-cockpit.html")
            if ts.get("dnsName"):
                print(f"  http://{ts['dnsName']}:{PORT}/trade-cockpit.html （MagicDNS）")
        print("  使い終わったら、このウィンドウを閉じてください。")
    print("=" * 52)
    with httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
