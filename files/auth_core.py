"""ログイン認証の純粋ロジック（2026-09-26 MU-Multi）。DB・HTTPには依存しない（テスト容易性のため）。

- パスワード：PBKDF2-HMAC-SHA256 + ユーザーごとのランダムsalt。平文は保存しない。
    保存形式: pbkdf2_sha256$<反復回数>$<salt(b64)>$<hash(b64)>
- セッショントークン：secrets.token_urlsafe(32)。DBにはSHA-256のみ保存（DBが漏れてもCookie値は復元できない）。
- CSRFトークン：セッションごとのランダム値。書き込みAPIは X-CSRF-Token ヘッダ必須。
- ログイン試行制限：同一(ユーザー名, IP)で失敗が続いたら一定時間ロック（総当たり対策、メモリ内）。
"""
import base64
import hashlib
import hmac
import re
import secrets
import threading
import time

PBKDF2_ITERATIONS = 240_000
HASH_SCHEME = "pbkdf2_sha256"
MIN_PASSWORD_LENGTH = 8
USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{1,31}$")  # 先頭は英数字（"_shared"等の予約名を防ぐ）
RESERVED_USERNAMES = {"local", "_shared", "system", "admin_view"}

# セッション期限：無操作7日で失効（スライド更新）、ただし作成から最大30日で必ず失効。
SESSION_IDLE_SECONDS = 7 * 24 * 3600
SESSION_ABSOLUTE_SECONDS = 30 * 24 * 3600

LOGIN_MAX_FAILURES = 5
LOGIN_LOCK_SECONDS = 10 * 60
LOGIN_WINDOW_SECONDS = 10 * 60


def validate_username(username):
    """利用可能なユーザー名か。戻り値：(ok, 理由)。"""
    if not isinstance(username, str) or not USERNAME_RE.match(username):
        return False, "ユーザー名は英数字・._-のみ、2〜32文字（先頭は英数字）にしてください"
    if username.lower() in RESERVED_USERNAMES:
        return False, "このユーザー名は予約されており使えません"
    return True, None


def validate_password_strength(password):
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        return False, f"パスワードは{MIN_PASSWORD_LENGTH}文字以上にしてください"
    return True, None


def hash_password(password, iterations=PBKDF2_ITERATIONS, salt=None):
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "%s$%d$%s$%s" % (HASH_SCHEME, iterations, base64.b64encode(salt).decode(), base64.b64encode(dk).decode())


def verify_password(password, stored):
    """定数時間比較。形式不正・空はFalse（例外を投げない）。"""
    try:
        scheme, iters, salt_b64, hash_b64 = (stored or "").split("$")
        if scheme != HASH_SCHEME:
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        dk = hashlib.pbkdf2_hmac("sha256", (password or "").encode("utf-8"), salt, int(iters))
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


def new_session_token():
    return secrets.token_urlsafe(32)


def new_csrf_token():
    return secrets.token_urlsafe(24)


def token_digest(token):
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def constant_time_equal(a, b):
    return bool(a) and bool(b) and hmac.compare_digest(str(a), str(b))


class LoginThrottle:
    """(ユーザー名, IP)ごとの失敗回数を数え、上限を超えたら一定時間ロックする。"""

    def __init__(self, max_failures=LOGIN_MAX_FAILURES, lock_seconds=LOGIN_LOCK_SECONDS, window=LOGIN_WINDOW_SECONDS):
        self.max_failures, self.lock_seconds, self.window = max_failures, lock_seconds, window
        self._state = {}
        self._lock = threading.Lock()

    def _key(self, username, ip):
        return ((username or "").lower(), ip or "")

    def is_locked(self, username, ip, now=None):
        now = now or time.time()
        with self._lock:
            st = self._state.get(self._key(username, ip))
            return bool(st and st.get("locked_until", 0) > now)

    def record_failure(self, username, ip, now=None):
        now = now or time.time()
        with self._lock:
            key = self._key(username, ip)
            st = self._state.setdefault(key, {"fails": [], "locked_until": 0})
            st["fails"] = [t for t in st["fails"] if now - t < self.window] + [now]
            if len(st["fails"]) >= self.max_failures:
                st["locked_until"] = now + self.lock_seconds
                st["fails"] = []

    def record_success(self, username, ip):
        with self._lock:
            self._state.pop(self._key(username, ip), None)


def session_expiry(created_epoch, now_epoch):
    """スライド式：無操作7日、ただし作成から30日を超えない。戻り値：新しいexpires（epoch秒）。"""
    return min(now_epoch + SESSION_IDLE_SECONDS, created_epoch + SESSION_ABSOLUTE_SECONDS)


def same_origin(origin_header, host_header):
    """Originヘッダ（あれば）が、リクエスト先Hostと同一オリジンか。Originが無い場合は判定不能としてTrue
    （同一オリジンのfetchでもGETはOriginを付けないため。POSTは別途CSRFトークンで担保する）。"""
    if not origin_header or origin_header == "null":
        return origin_header != "null"
    try:
        origin_host = re.sub(r"^https?://", "", origin_header).split("/")[0].lower()
        return origin_host == (host_header or "").lower()
    except Exception:
        return False
