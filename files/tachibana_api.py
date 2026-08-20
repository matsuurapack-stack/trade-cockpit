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
DEMO_LOGIN_URL = "https://demo-kabuka.e-shiten.jp/e_api_v4r9/auth/"
PROD_LOGIN_URL = "https://kabuka.e-shiten.jp/e_api_v4r9/auth/"

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
        # 診断用（一時）：鍵の中身は出さず、行数・先頭行・末尾行（本来固定の定型文なので
        # 秘密ではない）だけログに出す。クラウド環境でのSecret File設定ミスの切り分け用。
        lines = content.splitlines()
        print(f"  [診断] 秘密鍵の読み込み失敗: {e}")
        print(f"  [診断] 行数={len(lines)} 文字数={len(content)}")
        print(f"  [診断] 先頭行={lines[0]!r}" if lines else "  [診断] 空ファイル")
        print(f"  [診断] 末尾行={lines[-1]!r}" if lines else "")
        raise


def _now_p_sd_date():
    return datetime.datetime.now().strftime("%Y.%m.%d-%H:%M:%S.") + \
        f"{datetime.datetime.now().microsecond // 1000:03d}"


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

PRICE_COLUMNS = "pDPP,pPRP,pDYWP,pDYRP,pDV,pDHP,pDLP,pDOP,pQAP,pQBP"
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
                "high": _num(row.get("pDHP")),
                "low": _num(row.get("pDLP")),
                "change": _num(row.get("pDYWP")),
                "changePct": _num(row.get("pDYRP")),
                "volume": _num(row.get("pDV")),
                "ask": _num(row.get("pQAP")),  # 売気配（最良気配。板の全体深度は未取得）
                "bid": _num(row.get("pQBP")),  # 買気配
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


def get_news_headlines(categories, date_from, date_to, limit=100, use_prod=True):
    """ニュースヘッダー問合取得（CLMMfdsGetNewsHead）。日経QUICKニュース(NQN)等の速報見出し。
    2026-08-20 実測: リクエストはsUrlMaster宛。1回のリクエストでカテゴリは1つのみ指定可のため、
    categories（例: ["100","120","129"]）ごとに複数回呼んで連結する。
    カテゴリコード: 100=ニュース、110=AI市況状況速報、120=AI開示速報(決算関連)、129=AI開示速報(その他)。
    date_from/date_to は "YYYYMMDD"。見出し(p_HDL)はShiftJISをURLエンコードしてからBASE64化された
    値なので、BASE64復号→URLデコード(cp932)の順で元の日本語見出しに戻す。
    戻り値: [{date, time, category, codes:[...], headline}, ...]（新しい順ではない。呼び出し側で整列する）"""
    sess = _ensure_session(use_prod=use_prod)
    out = []
    for cg in categories:
        payload = {
            "sCLMID": "CLMMfdsGetNewsHead",
            "p_CG": cg,
            "p_DT_FROM": date_from,
            "p_DT_TO": date_to,
            "p_REC_OFST": "0",
            "p_REC_LIMT": str(limit),
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
        result = json.loads(resp.data.decode("shift_jis", errors="replace"))
        if result.get("p_errno") not in (None, "0"):
            continue
        for row in result.get("aCLMMfdsNewsHead", []):
            headline = _decode_headline(row.get("p_HDL", ""))
            if not headline:
                continue
            isl = row.get("p_ISL", "") or ""
            out.append({
                "date": row.get("p_DT", ""),
                "time": row.get("p_TM", ""),
                "category": cg,
                "codes": [c for c in isl.split("|") if c],
                "headline": headline,
            })
    return out


def _decode_headline(hdl):
    """p_HDL（ShiftJISをURLエンコード→BASE64化された見出し）を元の文字列に戻す。"""
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
