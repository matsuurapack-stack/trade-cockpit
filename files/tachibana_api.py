"""
立花証券 e支店API 接続モジュール（公開鍵認証）

- files/e_api_authid.txt … 認証ID（e支店・API利用設定画面からDL、base64文字列）
- files/e_api_private_key.pem … 秘密鍵（同画面で発行、PEM形式）
どちらも .gitignore 済み・絶対にコミットしない。

参考: https://www.e-shiten.jp/api/ （公式ドキュメント・サンプルの仕様に基づき、
このプロジェクト用に独自実装。公式サンプルコードの転載ではない）

現状はログイン疎通確認のみ。実運用（板・約定・発注の取り込み）はログインが
安定して通ってから着手する。
"""
import base64
import datetime
import json
import os
import threading
import urllib.parse

from Crypto.Cipher import PKCS1_OAEP
from Crypto.Hash import SHA256
from Crypto.PublicKey import RSA
import urllib3

HERE = os.path.dirname(os.path.abspath(__file__))
AUTHID_PATH = os.path.join(HERE, "e_api_authid.txt")
PRIVKEY_PATH = os.path.join(HERE, "e_api_private_key.pem")

# 2026-08-20 実測: デモ環境(demo-kabuka)は本番とは別発行の認証ID・秘密鍵が必要
# （通常のe支店サイトで発行したものは本番専用）。このプロジェクトでは板・発注などの
# 取引系エンドポイントには一切触れず、時価情報（読み取り専用）のみ本番環境を使う。
# 2026-09-26 v4r10へ移行（v4r9は2026-09-27廃止。公式「リリース＆改定情報」）。v4r9は恒久フォールバックとして残さない。
# 認証（公開鍵方式）・仮想URL・p_no・sJsonOfmtの仕様はv4r9から変更なし。廃止されたI/F（CLMMfdsGetMasterData・
# CLMMfdsGetNewsHead・CLMMfdsGetNewsBody・CLMEventDownload）は個別問合取得I/Fへ置換（下記の各get_*参照）。
API_VERSION = "v4r10"
DEMO_LOGIN_URL = "https://demo-kabuka.e-shiten.jp/e_api_v4r10/auth/"
PROD_LOGIN_URL = "https://kabuka.e-shiten.jp/e_api_v4r10/auth/"

_http = urllib3.PoolManager(timeout=urllib3.Timeout(connect=10, read=15))


def _num(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _load_authid():
    with open(AUTHID_PATH, "r", encoding="utf-8") as f:
        return f.read().strip()


def _load_private_key():
    with open(PRIVKEY_PATH, "r", encoding="utf-8") as f:
        content = f.read()
    try:
        return RSA.import_key(content)
    except Exception as e:
        # 鍵の中身は出さず、行数・先頭行・末尾行（本来固定の定型文なので秘密ではない）だけ
        # サーバーログに出す。クラウド環境でのSecret File設定ミス（BEGIN/END行の欠落等、
        # 2026-08-20に実際に発生）の切り分け用。
        lines = content.splitlines()
        print(f"  [診断] 秘密鍵の読み込み失敗: {e}")
        print(f"  [診断] 行数={len(lines)} 文字数={len(content)}")
        print(f"  [診断] 先頭行={lines[0]!r}" if lines else "  [診断] 空ファイル")
        print(f"  [診断] 末尾行={lines[-1]!r}" if lines else "")
        raise


_JST = datetime.timezone(datetime.timedelta(hours=9))


def _now_p_sd_date():
    # p_sd_dateはサーバー側で日本時間との時刻差をチェックされるため、実行環境のOSタイムゾーンに
    # 依存せず必ず日本時間(JST)で送る（2026-08-20判明：Render等UTC環境のPCではdatetime.now()が
    # UTCを返し、9時間分ズレて「p_sd_date is exceed time limit」エラーになっていた）。
    now = datetime.datetime.now(_JST)
    return now.strftime("%Y.%m.%d-%H:%M:%S.") + f"{now.microsecond // 1000:03d}"


def _decrypt_field(value, priv_key):
    """暗号化されたURLフィールド(base64)をRSA-OAEP(SHA-256)で復号して文字列で返す。
    平文でそのまま返ってくる場合はそのまま返す。"""
    if not value:
        return value
    try:
        cipher_bytes = base64.b64decode(value)
        cipher = PKCS1_OAEP.new(priv_key, hashAlgo=SHA256)
        return cipher.decrypt(cipher_bytes).decode("utf-8")
    except Exception:
        return value


def login(use_prod=False):
    """ログインして仮想URL群を取得する。戻り値は生レスポンスのdict。
    成功時は sUrlRequest / sUrlMaster / sUrlPrice / sUrlEvent 等が入っている想定。"""
    authid = _load_authid()
    priv_key = _load_private_key()

    payload = {
        "sCLMID": "CLMAuthLoginRequest",
        "sAuthId": authid,
        "p_no": "1",
        "p_sd_date": _now_p_sd_date(),
        "sJsonOfmt": "5",
    }
    url = PROD_LOGIN_URL if use_prod else DEMO_LOGIN_URL
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    resp = _http.request(
        "POST", url,
        body=body,
        headers={"Content-Type": "application/json"},
        retries=urllib3.Retry(total=2, backoff_factor=1.0),
    )
    text = resp.data.decode("shift_jis", errors="replace")
    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        return {"_raw": text, "_status": resp.status, "_parse_error": True}

    for key in list(result.keys()):
        if key.lower().startswith("surl"):
            result[key] = _decrypt_field(result[key], priv_key)

    result["_status"] = resp.status
    return result


# ---- 時価情報（読み取り専用）のセッション管理 ----
# ログイン応答の仮想URLは「1日券」なので、当日中はログインを使い回し、p_no（要求番号）だけ
# リクエストのたびに増やす。p_noは前回より大きい必要がある（同じ値だとp_errno=6で拒否される）。
_session_lock = threading.Lock()
_session = None
_session_date = None
_p_no = 1

PRICE_COLUMNS = "pDPP,pPRP,pDYWP,pDYRP,pDV,pDHP,pDLP,pDOP,pQAP,pQBP,pVWAP"  # pVWAP：取引所算出の当日VWAP（2026-09-25実測で取得可を確認）
PRICE_CHUNK = 40  # 一括問い合わせの銘柄数上限（未検証の上限に余裕を持たせた保守的な値）


def _ensure_session(use_prod=True, force=False):
    global _session, _session_date
    today = datetime.date.today().isoformat()
    with _session_lock:
        if not force and _session is not None and _session_date == today:
            return _session
        result = login(use_prod=use_prod)
        if result.get("p_errno") not in (None, "0"):
            raise RuntimeError(f"立花証券APIログイン失敗: {result.get('p_err')}")
        _session = result
        _session_date = today
        return _session


def _next_p_no():
    global _p_no
    with _session_lock:
        _p_no += 1
        return _p_no


def _request_price(price_url, codes):
    payload = {
        "sCLMID": "CLMMfdsGetMarketPrice",
        "sTargetIssueCode": ",".join(codes),
        "sTargetColumn": PRICE_COLUMNS,
        "p_no": str(_next_p_no()),
        "p_sd_date": _now_p_sd_date(),
        "sJsonOfmt": "5",
    }
    resp = _http.request(
        "POST", price_url,
        body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        retries=urllib3.Retry(total=2, backoff_factor=1.0),
    )
    text = resp.data.decode("shift_jis", errors="replace")
    return json.loads(text)


def get_market_price(codes, use_prod=True):
    """日本株コードのリストから現在値・前日終値・高値・安値・出来高・気配値等を取得する。
    戻り値は {code: {t, p, high, low, change, changePct, volume, ask, bid}}。
    ask/bidは最良気配（板の全体の深さは未取得。歩み値・発注機能とあわせて必要になれば拡張する）。
    銘柄コードが無効等の理由で応答に含まれないコードは戻り値にも含まれない。"""
    out = {}
    codes = [c for c in dict.fromkeys(codes) if c]  # 重複除去・順序保持
    if not codes:
        return out

    sess = _ensure_session(use_prod=use_prod)
    for i in range(0, len(codes), PRICE_CHUNK):
        chunk = codes[i:i + PRICE_CHUNK]
        try:
            result = _request_price(sess["sUrlPrice"], chunk)
        except (json.JSONDecodeError, KeyError):
            continue

        if result.get("p_errno") not in (None, "0"):
            # セッション切れ・要求番号ずれ等 → 1回だけ再ログインして再試行
            sess = _ensure_session(use_prod=use_prod, force=True)
            try:
                result = _request_price(sess["sUrlPrice"], chunk)
            except (json.JSONDecodeError, KeyError):
                continue
            if result.get("p_errno") not in (None, "0"):
                continue

        for row in result.get("aCLMMfdsMarketPrice", []):
            code = row.get("sIssueCode")
            if not code:
                continue
            out[code] = {
                "t": _num(row.get("pDPP")),
                "p": _num(row.get("pPRP")),
                "open": _num(row.get("pDOP")),  # 当日始値（日足履歴に当日分を合成する用途にも使う）
                "high": _num(row.get("pDHP")),
                "low": _num(row.get("pDLP")),
                "change": _num(row.get("pDYWP")),
                "changePct": _num(row.get("pDYRP")),
                "volume": _num(row.get("pDV")),
                "ask": _num(row.get("pQAP")),  # 売気配（最良気配。板の全体深度は未取得）
                "bid": _num(row.get("pQBP")),  # 買気配
                "vwap": _num(row.get("pVWAP")),  # 当日VWAP（取引所算出）。未約定・取得不能ならNone
            }
    return out


def get_daily_history(code, sizyou_c="00", use_prod=True):
    """個別株の日足データ（分割調整済み・上場来〜20年分）を古い順のリストで返す。
    各要素: {date:"YYYY-MM-DD", open, high, low, close, volume}
    TradingView無料埋め込みが東証再配信制限で使えないJP個別株チャート用。
    2026-08-20 実測: リクエストはsUrlPrice宛、sIssueCode+sSizyouC（1銘柄のみ・期間指定不可）。
    分割調整後の pDOPxK/pDHPxK/pDLPxK/pDPPxK/pDVxK を使う（xK無しは未調整の生値）。"""
    sess = _ensure_session(use_prod=use_prod)
    payload = {
        "sCLMID": "CLMMfdsGetMarketPriceHistory",
        "sIssueCode": code,
        "sSizyouC": sizyou_c,
        "p_no": str(_next_p_no()),
        "p_sd_date": _now_p_sd_date(),
        "sJsonOfmt": "5",
    }
    resp = _http.request(
        "POST", sess["sUrlPrice"],
        body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        retries=urllib3.Retry(total=2, backoff_factor=1.0),
    )
    result = json.loads(resp.data.decode("shift_jis", errors="replace"))

    if result.get("p_errno") not in (None, "0"):
        sess = _ensure_session(use_prod=use_prod, force=True)
        payload["p_no"] = str(_next_p_no())
        resp = _http.request(
            "POST", sess["sUrlPrice"],
            body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        result = json.loads(resp.data.decode("shift_jis", errors="replace"))
        if result.get("p_errno") not in (None, "0"):
            raise RuntimeError(f"立花証券API 日足取得失敗: {result.get('p_err')}")

    out = []
    for row in result.get("aCLMMfdsMarketPriceHistory", []):
        d = row.get("sDate", "")
        if len(d) != 8:
            continue
        o, h, l, c = _num(row.get("pDOPxK")), _num(row.get("pDHPxK")), _num(row.get("pDLPxK")), _num(row.get("pDPPxK"))
        if None in (o, h, l, c):
            continue
        out.append({
            "date": f"{d[0:4]}-{d[4:6]}-{d[6:8]}",
            "open": o, "high": h, "low": l, "close": c,
            "volume": _num(row.get("pDVxK")),
        })
    out.sort(key=lambda r: r["date"])
    return out


# ---- ニュース（v4r10：ニュース問合取得 CLMMfdsGetNews は「日付指定で1日分の全ニュース」のみ。銘柄・カテゴリ絞り込みや
# 本文問合せ（旧 CLMMfdsGetNewsHead / CLMMfdsGetNewsBody）は廃止された）。1日分をキャッシュし、銘柄・カテゴリの絞り込みと
# 本文（p_TX）の参照はクライアント側で行う。マニュアル：過去日の情報は更新されない→過去日は永続キャッシュ、当日は毎分更新→短TTL。
NEWS_TODAY_TTL_SEC = 60
NEWS_MAX_DAYS = 90            # 取得可能な範囲（過去90日）
_news_day_cache = {}          # "YYYYMMDD" -> (fetched_epoch, rows)
_news_lock = threading.Lock()


def _news_day(date_str, use_prod=True):
    """指定日（YYYYMMDD）の全ニュース行のリスト。行: {id,date,time,categories,genres,codes,headline,body_raw}。
    取得失敗（p_errno≠0）は例外。休日など配信なしは正常応答で空リスト。"""
    import time as _time
    today_str = datetime.datetime.now(_JST).strftime("%Y%m%d")
    with _news_lock:
        c = _news_day_cache.get(date_str)
        if c is not None and (date_str != today_str or _time.time() - c[0] <= NEWS_TODAY_TTL_SEC):
            return c[1]
        sess = _ensure_session(use_prod=use_prod)
        payload = {
            "sCLMID": "CLMMfdsGetNews",
            "p_DT": date_str,
            "p_no": str(_next_p_no()),
            "p_sd_date": _now_p_sd_date(),
            "sJsonOfmt": "5",
        }
        resp = _http.request(
            "POST", sess["sUrlMaster"],
            body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            retries=urllib3.Retry(total=2, backoff_factor=1.0),
            timeout=urllib3.Timeout(connect=10, read=60),      # 1日分の本文つきで応答が大きい
        )
        result = json.loads(resp.data.decode("shift_jis", errors="replace"))
        if result.get("p_errno") not in (None, "0"):
            raise RuntimeError(f"ニュース取得失敗({date_str}): p_errno={result.get('p_errno')} {result.get('p_err')}")
        rows = []
        for r in result.get("aCLMMfdsNews", []) or []:
            headline = _decode_headline(r.get("p_HDL", ""))
            if not headline:
                continue
            rows.append({
                "id": r.get("p_ID", ""),
                "date": date_str,
                "time": r.get("p_TM", ""),
                "categories": [x for x in (r.get("p_CGL", "") or "").split("|") if x],
                "genres": [x for x in (r.get("p_GNL", "") or "").split("|") if x],
                "codes": [x for x in (r.get("p_ISL", "") or "").split("|") if x],
                "headline": headline,
                "body_raw": r.get("p_TX", "") or "",
            })
        # 過去日は確定（更新されない）。当日はTTLで再取得。空の当日（配信前）も同じTTL
        _news_day_cache[date_str] = (_time.time(), rows)
        for k in [k for k in _news_day_cache if k < (datetime.datetime.now(_JST) - datetime.timedelta(days=NEWS_MAX_DAYS)).strftime("%Y%m%d")]:
            _news_day_cache.pop(k, None)
        return rows


def _news_range(date_from, date_to, use_prod=True):
    """date_from〜date_to（YYYYMMDD）の各日のニュース行（新しい日から）。全日失敗なら例外（部分失敗は取れた日だけ返す）。"""
    d0 = datetime.datetime.strptime(date_from, "%Y%m%d")
    d1 = datetime.datetime.strptime(date_to, "%Y%m%d")
    out, errors, ok = [], [], 0
    d = d1
    while d >= d0:
        try:
            out.extend(sorted(_news_day(d.strftime("%Y%m%d"), use_prod=use_prod), key=lambda r: (r["time"], r["id"]), reverse=True))
            ok += 1
        except Exception as e:
            errors.append(str(e)[:120])
        d -= datetime.timedelta(days=1)
    if ok == 0 and errors:
        raise RuntimeError(errors[0])
    return out


def get_news_headlines(categories, date_from, date_to, limit=100, use_prod=True):
    """ニュース見出し（v4r10：CLMMfdsGetNews＝日付指定の1日分から、クライアント側でカテゴリ絞り込み）。
    categories（例: ["100","120","129"]）は p_CGL に含まれるかで判定（カテゴリごとに新しい順でlimit件）。
    カテゴリコード: 100=ニュース、110=AI市況状況速報、120=AI開示速報(決算関連)、129=AI開示速報(その他)。
    戻り値: [{id,date,time,category,codes:[...],headline}, ...]（旧v4r9版と同じ形）"""
    rows = _news_range(date_from, date_to, use_prod=use_prod)
    out = []
    for cg in categories:
        n = 0
        for r in rows:
            if cg in r["categories"]:
                out.append({"id": r["id"], "date": r["date"], "time": r["time"], "category": cg, "codes": r["codes"], "headline": r["headline"]})
                n += 1
                if n >= limit:
                    break
    return out


MAX_RELATED_CODES_STOCK_NEWS = 5   # 関連銘柄がこれを超える記事（<AI市況>騰落率・新高値・売買代金上位などの市況まとめ）は個別銘柄のニュースではない


def get_stock_news(code, date_from, date_to, limit=20, use_prod=True, max_related=MAX_RELATED_CODES_STOCK_NEWS):
    """個別銘柄のニュース見出し（v4r10：1日分のニュースから p_ISL（関連銘柄コード）で絞り込み）。
    実測(2026-09-26)：<TDnet>/<EDINET>の開示速報は関連銘柄1件、<AI市況>の市況まとめは40〜140銘柄が関連づく。
    市況まとめまで個別ニュースに含めないよう、関連銘柄が max_related を超える記事は除く（None で無効）。
    戻り値: [{id,date,time,codes:[...],headline}, ...]（新しい順で最大limit件）。取得失敗は例外（旧版は握りつぶして空を返していた）。"""
    out = []
    for r in _news_range(date_from, date_to, use_prod=use_prod):
        if max_related is not None and len(r["codes"]) > max_related:
            continue
        if code in r["codes"]:
            out.append({"id": r["id"], "date": r["date"], "time": r["time"], "codes": r["codes"], "headline": r["headline"]})
            if len(out) >= limit:
                break
    return out


def get_news_body(news_id, use_prod=True):
    """ニュースID本文（v4r10：本文専用I/Fは廃止。1日分のニュース応答に含まれる p_TX を使う。IDの先頭8桁が配信日）。
    取得失敗・該当なしは空文字。"""
    if not news_id or len(news_id) < 8 or not news_id[:8].isdigit():
        return ""
    for r in _news_day(news_id[:8], use_prod=use_prod):
        if r["id"] == news_id:
            return _decode_headline(r["body_raw"])
    return ""


MFDS_ISSUE_CHUNK = 120  # sTargetIssueCodeは最大120銘柄まで（超過分は取引所側で無視される）


def _mfds_issue_query(clmid, codes, list_key, use_prod=True):
    """CLMMfdsGetIssueDetail・CLMMfdsGetSyoukinZan・CLMMfdsGetShinyouZan・CLMMfdsGetHibuInfo共通の
    「銘柄コードをカンマ区切りで渡し、配列で返ってくる」問合せ処理。応答を銘柄コードキーの辞書にして返す。"""
    sess = _ensure_session(use_prod=use_prod)

    def _do(sess):
        payload = {
            "sCLMID": clmid,
            "sTargetIssueCode": ",".join(codes),
            "p_no": str(_next_p_no()),
            "p_sd_date": _now_p_sd_date(),
            "sJsonOfmt": "5",
        }
        resp = _http.request(
            "POST", sess["sUrlMaster"],
            body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            retries=urllib3.Retry(total=2, backoff_factor=1.0),
        )
        return json.loads(resp.data.decode("shift_jis", errors="replace"))

    result = _do(sess)
    if result.get("p_errno") not in (None, "0"):
        sess = _ensure_session(use_prod=use_prod, force=True)
        result = _do(sess)
        if result.get("p_errno") not in (None, "0"):
            return {}
    out = {}
    for row in result.get(list_key, []):
        code = row.get("sIssueCode")
        if code:
            out[code] = row
    return out


def _mfds_issue_query_chunked(clmid, codes, list_key, use_prod=True):
    out = {}
    codes = [c for c in dict.fromkeys(codes) if c]
    for i in range(0, len(codes), MFDS_ISSUE_CHUNK):
        try:
            out.update(_mfds_issue_query(clmid, codes[i:i + MFDS_ISSUE_CHUNK], list_key, use_prod=use_prod))
        except Exception as e:
            print(f"  {clmid} 取得失敗", e)
    return out


def get_issue_detail(codes, use_prod=True):
    """銘柄詳細情報（CLMMfdsGetIssueDetail）：PER・PBR・EPS・BPS・ROE・配当利回り・年初来高値安値等。
    戻り値: {code: {sIssueCode, pBPSB, pCLOE, pEPSF, pEXRD, pIDVE, pROEL, pRPER, pSPBR, pSPRO, pSYIE,
    pYHPD, pYHPR, pYLPD, pYLPR}}（項目の意味はPDF仕様書参照。空文字は値なしの意味）。"""
    return _mfds_issue_query_chunked("CLMMfdsGetIssueDetail", codes, "aCLMMfdsIssueDetail", use_prod=use_prod)


def get_syoukin_zan(codes, use_prod=True):
    """証金残情報（CLMMfdsGetSyoukinZan）：日証金の融資残・貸株残・回転日数・貸借倍率等。
    戻り値: {code: {sIssueCode, pSFC6, pSFD, pSFD6, pSFF6, pSFG6, pSFKS, pSFL6, pSFN6, pSFP6, pSFR6,
    pSFS6, pSSG6, pSSL6, pSSP6}}。"""
    return _mfds_issue_query_chunked("CLMMfdsGetSyoukinZan", codes, "aCLMMfdsSyoukinZan", use_prod=use_prod)


def get_shinyou_zan(codes, use_prod=True):
    """信用残情報（CLMMfdsGetShinyouZan）：信用買残・売残（一般/制度/合算）・信用倍率等。
    戻り値: {code: {sIssueCode, pMBB3, pMBB6, pMBBQ, pMBC3, pMBC6, pMBCQ, pMBD, pMBN3, pMBN6, pMBNQ,
    pMBR3, pMBR6, pMBRQ, pMBS3, pMBS6, pMBSQ}}。"""
    return _mfds_issue_query_chunked("CLMMfdsGetShinyouZan", codes, "aCLMMfdsShinyouZan", use_prod=use_prod)


def get_issue_regulation_kabu(use_prod=True):
    """株式銘柄別・市場別規制情報問合取得（CLMStkGetIssueSizyouKiseiKabu）。公式マニュアル
    「マスタ機能（REQUEST I/F）」に記載の機能で、引数なし・全銘柄分を1回のリクエストで返す（sUrlMaster宛）。
    使う項目：sSokuzituNyukinC（即日入金規制＝増し担保）・sSinyouSyutyuKubun（0なし/1あり/2日々公表）・sZizenCyouseiC（事前調整）・
    制度/一般信用の新規買建・売建の停止区分・sTeisiKubun（取引停止）と、それぞれの翌営業日分（〜Yoku）。
    システム稼働中は更新されないため、営業日の朝に1回だけ取得して使い回す（マニュアルの指示）。
    貸株注意喚起・空売り規制（値幅制限型）はこのAPIには含まれない。
    戻り値: {code: {項目名: 値}}（東証 sZyouzyouSizyou=="00" のみ）。失敗時は例外（呼び出し側で握りつぶさず「規制情報UNKNOWN」にする）。"""
    sess = _ensure_session(use_prod=use_prod)
    payload = {
        "sCLMID": "CLMStkGetIssueSizyouKiseiKabu",
        "p_no": str(_next_p_no()),
        "p_sd_date": _now_p_sd_date(),
        "sJsonOfmt": "5",
    }
    resp = _http.request(
        "POST", sess["sUrlMaster"],
        body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        retries=urllib3.Retry(total=2, backoff_factor=1.0),
        timeout=urllib3.Timeout(connect=10, read=60),
    )
    result = json.loads(resp.data.decode("shift_jis", errors="replace"))
    if result.get("p_errno") not in (None, "0"):
        raise RuntimeError(f"規制情報取得失敗: p_errno={result.get('p_errno')} {result.get('p_err')}")
    if result.get("sResultCode") not in (None, "", "0"):
        raise RuntimeError(f"規制情報取得失敗: sResultCode={result.get('sResultCode')} {result.get('sResultText')}")
    out = {}
    for row in result.get("aCLMStkIssueSizyouKiseiKabu", []):
        code = row.get("sIssueCode")
        if code and (row.get("sZyouzyouSizyou") in (None, "", "00")):
            out[code] = {k: v for k, v in row.items() if k not in ("sIssueCode", "sZyouzyouSizyou")}
    return out


def get_hibu_info(codes, use_prod=True):
    """逆日歩情報（CLMMfdsGetHibuInfo）。戻り値: {code: {sIssueCode, pBWRQ}}（pBWRQ=逆日歩）。"""
    return _mfds_issue_query_chunked("CLMMfdsGetHibuInfo", codes, "aCLMMfdsHibuInfo", use_prod=use_prod)


def get_issue_master_kabu(use_prod=True):
    """東証上場の株式銘柄マスタ全件（v4r10：株式銘柄マスタ問合取得 CLMStkGetIssueMstKabu、sUrlMaster宛・引数なし）。
    旧 CLMMfdsGetMasterData(sTargetCLMID=CLMIssueMstKabu) は廃止。マスタはシステム稼働中は更新されない（朝1回取得して使い回す）。
    （2026-08-22 ユーザー要望：日本市場タブの検索候補を上場銘柄全体に広げる）。戻り値: [{code, name, kana, gyoshuCode}, ...]。
    sIssueNameは全角スペース区切りの正式名称のまま返す（例:"極 洋"）。空白除去や業種コード→東証33業種名は呼び出し側（server.py）。"""
    sess = _ensure_session(use_prod=use_prod)
    payload = {
        "sCLMID": "CLMStkGetIssueMstKabu",
        "p_no": str(_next_p_no()),
        "p_sd_date": _now_p_sd_date(),
        "sJsonOfmt": "5",
    }
    resp = _http.request(
        "POST", sess["sUrlMaster"],
        body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        retries=urllib3.Retry(total=2, backoff_factor=1.0),
        timeout=urllib3.Timeout(connect=10, read=60),  # 全銘柄分のため応答が大きく、通常より長めに待つ
    )
    result = json.loads(resp.data.decode("shift_jis", errors="replace"))
    if result.get("p_errno") not in (None, "0"):
        raise RuntimeError(f"銘柄マスタ取得失敗: p_errno={result.get('p_errno')} {result.get('p_err')}")
    out = []
    for row in result.get("aCLMStkIssueMstKabu", []):
        code = row.get("sIssueCode")
        if not code:
            continue
        out.append({
            "code": code,
            "name": (row.get("sIssueName") or "").replace("　", "").replace(" ", ""),
            "kana": row.get("sIssueNameKana") or "",
            "gyoshuCode": row.get("sGyousyuCode") or "",
        })
    return out


def get_issue_market_master_kabu(use_prod=True):
    """株式銘柄市場マスタ問合取得（v4r10：CLMStkGetIssueSizyouMstKabu、sUrlMaster宛・引数なし）。
    値幅（sNehabaMin/Max）・信用区分（sSinyouC: 1貸借/2制度/3一般信用）・前日終値・上場区分など。
    戻り値: {code: {項目名: 値}}（東証 sZyouzyouSizyou=="00" のみ）。失敗時は例外。"""
    sess = _ensure_session(use_prod=use_prod)
    payload = {
        "sCLMID": "CLMStkGetIssueSizyouMstKabu",
        "p_no": str(_next_p_no()),
        "p_sd_date": _now_p_sd_date(),
        "sJsonOfmt": "5",
    }
    resp = _http.request(
        "POST", sess["sUrlMaster"],
        body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        retries=urllib3.Retry(total=2, backoff_factor=1.0),
        timeout=urllib3.Timeout(connect=10, read=60),
    )
    result = json.loads(resp.data.decode("shift_jis", errors="replace"))
    if result.get("p_errno") not in (None, "0"):
        raise RuntimeError(f"市場マスタ取得失敗: p_errno={result.get('p_errno')} {result.get('p_err')}")
    out = {}
    for row in result.get("aCLMStkIssueSizyouMstKabu", []):
        code = row.get("sIssueCode")
        if code and (row.get("sZyouzyouSizyou") in (None, "", "00")):
            out[code] = {k: v for k, v in row.items() if k not in ("sIssueCode", "sZyouzyouSizyou")}
    return out


def logout(use_prod=True):
    """ログアウト（CLMAuthLogoutRequest、sUrlRequest宛）。通常運用では呼ばない（仮想URLは1日券で当日使い回す）。
    E2E後のセッション後始末用。成功後は保持中のセッションを破棄する（次回リクエストで再ログイン）。"""
    global _session, _session_date
    with _session_lock:
        sess = _session
    if not sess:
        return True
    payload = {"sCLMID": "CLMAuthLogoutRequest", "p_no": str(_next_p_no()), "p_sd_date": _now_p_sd_date(), "sJsonOfmt": "5"}
    resp = _http.request(
        "POST", sess["sUrlRequest"],
        body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        retries=urllib3.Retry(total=1, backoff_factor=1.0),
    )
    result = json.loads(resp.data.decode("shift_jis", errors="replace"))
    with _session_lock:
        _session, _session_date = None, None
    if result.get("p_errno") not in (None, "0") or result.get("sResultCode") not in (None, "", "0"):
        raise RuntimeError(f"ログアウト失敗: {result.get('p_err') or result.get('sResultText')}")
    return True


def _decode_headline(hdl):
    """p_HDL・p_TX（ShiftJISをURLエンコード→BASE64化された文字列）を元の文字列に戻す。
    ニュース見出し・本文どちらも同じエンコード方式のため共用する。"""
    if not hdl:
        return ""
    try:
        padded = hdl + "=" * (-len(hdl) % 4)
        raw = base64.b64decode(padded).decode("ascii")
        return urllib.parse.unquote(raw, encoding="cp932", errors="replace")
    except Exception:
        return ""


if __name__ == "__main__":
    print("立花証券e支店API デモ環境へログイン試行中…")
    try:
        result = login(use_prod=False)
    except FileNotFoundError as e:
        print(f"認証ファイルが見つかりません: {e}")
        raise SystemExit(1)

    errno = result.get("p_errno")
    if errno not in (None, "0"):
        print(f"ログイン失敗（p_errno={errno}）: {result.get('p_err', result)}")
    elif result.get("_parse_error"):
        print("応答をJSONとして解釈できませんでした。フォーマットを見直します。")
        print("応答の先頭300文字:", result["_raw"][:300])
    else:
        got = [k for k in result if k.lower().startswith("surl")]
        print(f"ログイン成功。取得できた仮想URLキー: {got}")
        print("全キー:", sorted(result.keys()))
