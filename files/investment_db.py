"""
投資判断ログ用のDBアクセスモジュール（2026-09-02 新規、同日中にマルチユーザー化）

- 保存先はNeon（無料枠のPostgreSQL）。PC・Renderクラウドの両方から同じDBに接続することで
  データを一本化する（接続先の切り替えはserver.py側のDATABASE_URL定数で行う）。
- テーブル構成：
  - daily_log          … 日次の相場観（地合い・米国市場・金利・為替・原油・セクター強弱等）
  - stock_judgments    … 銘柄ごとの評価（daily_logに従属。監視/保有/買い候補/見送りの評価一式）
  - journal            … 売買記録（旧localStorage "journal" の移行先。列構成はほぼ同じ）
  - investment_rules   … マイルール（旧localStorage "myRules" の移行先）
  - investment_profile … 投資プロフィール（自己紹介的な情報。1ユーザー1行。将来の機能拡張向けに
    列だけ用意、現状読み書きするAPIは無い）
  - chatgpt_imports    … ChatGPTで作成した投資ログJSONの取り込み履歴（2026-09-02新規。
    「有料AI APIは使わず、ChatGPT⇄手動貼り付けで連携する」方針のため、アプリからAIを
    直接呼び出すことはしない）
- 2026-09-02 マルチユーザー化：daily_log・journal・investment_rules・investment_profileは
  すべてuser_id（ログインユーザー名、server.pyの_authorized()参照）で分離する。journal・
  investment_rulesは既存の主キーがidだけだった（マルチユーザー化前は暗黙的に単一ユーザー
  だったため）ので、(user_id, id)の複合主キーに移行する。stock_judgmentsは自身は
  user_idを持たず、daily_log経由でスコープする（親のdaily_log_idがそのユーザーの
  daily_logであることを各関数側で確認してから操作する）。
- psycopg（PostgreSQL用ドライバ）が未インストール・DATABASE_URL未設定の環境でも他機能に
  影響しないよう、未導入時は全関数が空データ/Noneを返すだけにする（他のAPI連携と同じ方針）。
"""
import re
import json
import uuid
import hashlib
import decimal
import datetime
import contextlib

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:
    psycopg = None


class _DirectConn:
    """psycopg_pool.ConnectionPoolと同じ`.connection()`呼び出し方を保ちつつ、実体は
    リクエストごとに接続を開いて閉じるだけの薄いラッパー。
    2026-09-02判明：Neonの接続文字列は"...-pooler..."（Neon側でPgBouncerによる
    コネクションプーリング済み）のため、こちら側でもpsycopg_poolを重ねて保持すると
    アイドル中にNeon側が接続を閉じてしまい"SSL connection has been closed unexpectedly"で
    落ちることがあった。個人利用規模の負荷ではリクエストごとに開閉しても十分軽いため、
    二重プーリングをやめてこちらに統一した。"""
    def __init__(self, database_url):
        self.database_url = database_url

    @contextlib.contextmanager
    def connection(self):
        conn = psycopg.connect(self.database_url)
        try:
            yield conn
        finally:
            conn.close()


def _get_pool(database_url):
    if psycopg is None or not database_url:
        return None
    return _DirectConn(database_url)


# マルチユーザー化前（2026-09-02当日の前半）に作成された既存データの移行先ユーザー名。
# 新規インストールでは無関係（該当行が無いのでUPDATE文は何もしない）。
_LEGACY_OWNER = "matsuura"

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS daily_log (
    id               SERIAL PRIMARY KEY,
    user_id          TEXT NOT NULL DEFAULT 'matsuura',
    date             TEXT NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    market_env       TEXT,
    us_market        TEXT,
    interest_rate    TEXT,
    fx               TEXT,
    oil              TEXT,
    sector_strength  TEXT,
    chatgpt_view     TEXT,
    my_view          TEXT,
    reflection       TEXT,
    strong_sectors   JSONB,  -- 2026-09-02追加（ChatGPT連携）：その日の強かったセクター（文字列配列）
    weak_sectors     JSONB,  -- 2026-09-02追加（ChatGPT連携）：その日の弱かったセクター（文字列配列）
    raw_payload      JSONB   -- 2026-09-02追加（ChatGPT連携）：取り込み元のJSONをそのまま保存（後日の再解析・救済用）。
                             -- どのchatgpt_importsから来たかはchatgpt_imports.daily_log_id側から辿る
);

CREATE TABLE IF NOT EXISTS stock_judgments (
    id                SERIAL PRIMARY KEY,
    daily_log_id      INTEGER NOT NULL REFERENCES daily_log(id) ON DELETE CASCADE,
    code              TEXT,
    name              TEXT,
    category          TEXT,  -- '監視' | '保有' | '買い候補' | '見送り'
    entry_reason      TEXT,
    skip_reason       TEXT,
    exit_judgment     TEXT,
    supply_demand     TEXT,
    earnings_eval     TEXT,
    valuation_eval    TEXT,
    theme_eval        TEXT,
    chart_eval        TEXT,
    chatgpt_judgment  TEXT,
    my_judgment       TEXT,
    actual_trade      TEXT,
    trade_result      TEXT,
    journal_id        TEXT,
    execution_status  TEXT,  -- 2026-09-02追加：BUY|WATCH|SKIP|MISSED|CANCELLED等（取引しなかった判断も記録）
    mental_state      TEXT,  -- 2026-09-02追加：fear|fomo|confident|uncertain|frustrated|revenge_trade|calm等
    user_decision     TEXT,  -- 2026-09-02追加（ChatGPT連携）：自分の判断（例: BUY|WAIT|SKIP）
    ai_decision       TEXT   -- 2026-09-02追加（ChatGPT連携）：ChatGPTの判断。user_decisionとの食い違いを後から分析できるようにするため分離
);
CREATE INDEX IF NOT EXISTS idx_stock_judgments_daily ON stock_judgments(daily_log_id);
CREATE INDEX IF NOT EXISTS idx_stock_judgments_code ON stock_judgments(code);

CREATE TABLE IF NOT EXISTS journal (
    user_id               TEXT NOT NULL DEFAULT 'matsuura',
    id                    TEXT NOT NULL,
    code                  TEXT,
    name                  TEXT,
    action                TEXT,
    price                 TEXT,
    shares                TEXT,
    entry_plan            JSONB,
    market_env_at_entry   TEXT,
    reason                TEXT,
    result                TEXT,
    lesson_note           TEXT,
    created_at            TEXT,
    PRIMARY KEY (user_id, id)
);

CREATE TABLE IF NOT EXISTS investment_rules (
    user_id     TEXT NOT NULL DEFAULT 'matsuura',
    id          TEXT NOT NULL,
    text        TEXT,
    active      BOOLEAN,
    created_at  TEXT,
    PRIMARY KEY (user_id, id)
);

-- 2026-09-02新規：投資プロフィール（投資スタイル・リスク許容度等の自由記述サマリ。1ユーザー1行）。
-- まだ読み書きするAPIは無いが、schemaだけ先に用意しておく。
CREATE TABLE IF NOT EXISTS investment_profile (
    user_id         TEXT PRIMARY KEY,
    style_summary   TEXT,
    risk_tolerance  TEXT,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 2026-09-02新規（ChatGPT連携 Phase1）：ChatGPTで作成した投資ログJSONの取り込み履歴。
-- 同じ内容を誤って何度も貼り付け保存しないよう、payload_hash（生JSON文字列のSHA256）に
-- UNIQUE制約を付ける（9番：重複防止）。raw_payloadは取り込んだJSONをそのまま保存し、
-- 将来スキーマが変わっても再解析できるようにする（8番）。
CREATE TABLE IF NOT EXISTS chatgpt_imports (
    id            SERIAL PRIMARY KEY,
    user_id       TEXT NOT NULL,
    import_date   TEXT NOT NULL,
    payload_hash  TEXT NOT NULL,
    raw_payload   JSONB NOT NULL,
    imported_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    daily_log_id  INTEGER REFERENCES daily_log(id) ON DELETE SET NULL,
    UNIQUE (user_id, payload_hash)
);
CREATE INDEX IF NOT EXISTS idx_chatgpt_imports_user ON chatgpt_imports(user_id, imported_at DESC);

-- 2026-09-02新規（Trade Cockpit v3 Phase4）：「不要」判定したニュースのログ。AIは使わず、
-- 将来ニュースフィルタのキーワード辞書を人間が見直す際の材料として蓄積するだけ（設計案48番）。
CREATE TABLE IF NOT EXISTS news_feedback (
    id                  SERIAL PRIMARY KEY,
    user_id             TEXT NOT NULL,
    title               TEXT NOT NULL,
    source              TEXT,
    matched_stock_code  TEXT,
    matched_keyword     TEXT,
    category            TEXT,
    feedback            TEXT NOT NULL DEFAULT 'not_needed',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_news_feedback_user ON news_feedback(user_id, created_at DESC);

-- 2026-09-02新規（Trade Cockpit v3 Phase5）：ChatGPTスクリーンショット認識結果の監視銘柄一括
-- 取り込み履歴。実際のwatchlist本体はこれまで通りブラウザlocalStorageが正（Neonへは移していない）
-- ため、ここは「いつ・何件・どのモードで取り込んだか」の履歴と重複防止（payload_hash）専用。
CREATE TABLE IF NOT EXISTS watchlist_imports (
    id             SERIAL PRIMARY KEY,
    user_id        TEXT NOT NULL,
    payload_hash   TEXT NOT NULL,
    raw_payload    JSONB NOT NULL,
    applied_mode   TEXT NOT NULL,  -- add_only|diff|full_sync
    added_count    INTEGER,
    imported_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, payload_hash)
);
CREATE INDEX IF NOT EXISTS idx_watchlist_imports_user ON watchlist_imports(user_id, imported_at DESC);

-- 2026-09-03新規（Trade Cockpit v3-2）：watchlist本体をNeonへ移行（従来はブラウザlocalStorageが
-- 正データだった）。stock_code（code+market）をキーに管理し、複数端末で同じ監視リストを見られる
-- ようにする。sourceはChatGPT Watchlist Import・出来高ブレイクアウト自動追加等の由来を記録する。
CREATE TABLE IF NOT EXISTS watchlist (
    id            SERIAL PRIMARY KEY,
    user_id       TEXT NOT NULL,
    code          TEXT NOT NULL,
    name          TEXT,
    market        TEXT NOT NULL DEFAULT 'JP',
    sector        TEXT,
    kana          TEXT,
    tv_symbol     TEXT,
    theme         TEXT,
    watch         TEXT DEFAULT '通常',
    note          TEXT,
    source        TEXT,
    added_reason  TEXT,
    active        BOOLEAN NOT NULL DEFAULT true,
    added_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, code, market)
);
CREATE INDEX IF NOT EXISTS idx_watchlist_user ON watchlist(user_id, market);

-- 2026-09-03新規（Trade Cockpit v3-2）：portfolio（保有株）。localStorageにも従来存在しなかった
-- 新規機能のため、移行データは無い（最初からNeonが正）。
CREATE TABLE IF NOT EXISTS portfolio (
    id             SERIAL PRIMARY KEY,
    user_id        TEXT NOT NULL,
    code           TEXT NOT NULL,
    name           TEXT,
    market         TEXT NOT NULL DEFAULT 'JP',
    quantity       NUMERIC,
    average_price  NUMERIC,
    acquired_at    TIMESTAMPTZ,
    memo           TEXT,
    active         BOOLEAN NOT NULL DEFAULT true,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, code, market)
);
CREATE INDEX IF NOT EXISTS idx_portfolio_user ON portfolio(user_id);

-- 2026-09-02新規（Trade Cockpit v2 Phase1）：「今日の候補」。監視銘柄タブでStatus・RS Scoreを
-- 見ながら手動で拾った銘柄を保存する（自動売買や自動判定ではなく、あくまでユーザーが選んだ
-- ものを記録するテーブル）。仮想トレード追跡（v2 Phase7予定）用の列も先に用意しておく。
CREATE TABLE IF NOT EXISTS trade_candidates (
    id                   SERIAL PRIMARY KEY,
    user_id              TEXT NOT NULL,
    code                 TEXT NOT NULL,
    name                 TEXT,
    status               TEXT NOT NULL,  -- WATCH|WAIT|BUY_CANDIDATE|SKIP
    rs_score             NUMERIC,
    market_rs            NUMERIC,
    sector_rs            NUMERIC,
    note                 TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    virtual_entry_price  NUMERIC,
    virtual_entry_time   TIMESTAMPTZ,
    checkpoints          JSONB,
    sector               TEXT,     -- v3 Phase8（設計案83番）：セクター別統計用。保存時にフロントが持っている値をそのまま渡す
    margin_ratio         NUMERIC   -- v3 Phase8（設計案82番）：信用倍率別統計用。「分析」済み銘柄のみ取得できるため多くはNULL
);
CREATE INDEX IF NOT EXISTS idx_trade_candidates_user_date ON trade_candidates(user_id, created_at DESC);
"""

# 2026-09-02 マルチユーザー化の移行SQL。新規インストール（上のCREATE TABLEで最初から
# user_id列・複合主キーになっている）では実質no-op、既存データがある場合だけ意味を持つ。
# サーバー起動のたびに毎回実行しても副作用が無いよう、全行IF EXISTS/IF NOT EXISTS/
# WHERE ... IS NULLで冪等にしてある。
_MIGRATE_MULTIUSER_SQL = f"""
ALTER TABLE daily_log ADD COLUMN IF NOT EXISTS user_id TEXT;
UPDATE daily_log SET user_id = '{_LEGACY_OWNER}' WHERE user_id IS NULL;
ALTER TABLE daily_log ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE daily_log ALTER COLUMN user_id SET DEFAULT '{_LEGACY_OWNER}';
CREATE INDEX IF NOT EXISTS idx_daily_log_user_date ON daily_log(user_id, date);

ALTER TABLE stock_judgments ADD COLUMN IF NOT EXISTS execution_status TEXT;
ALTER TABLE stock_judgments ADD COLUMN IF NOT EXISTS mental_state TEXT;

ALTER TABLE journal ADD COLUMN IF NOT EXISTS user_id TEXT;
UPDATE journal SET user_id = '{_LEGACY_OWNER}' WHERE user_id IS NULL;
ALTER TABLE journal ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE journal ALTER COLUMN user_id SET DEFAULT '{_LEGACY_OWNER}';
ALTER TABLE journal DROP CONSTRAINT IF EXISTS journal_pkey;
ALTER TABLE journal ADD CONSTRAINT journal_pkey PRIMARY KEY (user_id, id);

ALTER TABLE investment_rules ADD COLUMN IF NOT EXISTS user_id TEXT;
UPDATE investment_rules SET user_id = '{_LEGACY_OWNER}' WHERE user_id IS NULL;
ALTER TABLE investment_rules ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE investment_rules ALTER COLUMN user_id SET DEFAULT '{_LEGACY_OWNER}';
ALTER TABLE investment_rules DROP CONSTRAINT IF EXISTS investment_rules_pkey;
ALTER TABLE investment_rules ADD CONSTRAINT investment_rules_pkey PRIMARY KEY (user_id, id);
"""

# 2026-09-02新規（ChatGPT連携 Phase1）：既存インストール向けの列追加。chatgpt_importsを
# 参照するimport_id列があるため、_SCHEMA_SQL（chatgpt_imports作成）より後に実行する必要がある。
_MIGRATE_CHATGPT_IMPORT_SQL = """
ALTER TABLE daily_log ADD COLUMN IF NOT EXISTS strong_sectors JSONB;
ALTER TABLE daily_log ADD COLUMN IF NOT EXISTS weak_sectors JSONB;
ALTER TABLE daily_log ADD COLUMN IF NOT EXISTS raw_payload JSONB;

ALTER TABLE stock_judgments ADD COLUMN IF NOT EXISTS user_decision TEXT;
ALTER TABLE stock_judgments ADD COLUMN IF NOT EXISTS ai_decision TEXT;

-- v3 Phase6（設計案56番）：investment_rulesの構造化。既存の自由テキスト行はrule_code等が
-- NULLのまま残り、従来通り動作する（後方互換）。
ALTER TABLE investment_rules ADD COLUMN IF NOT EXISTS rule_code TEXT;
ALTER TABLE investment_rules ADD COLUMN IF NOT EXISTS value NUMERIC;
ALTER TABLE investment_rules ADD COLUMN IF NOT EXISTS unit TEXT;
ALTER TABLE investment_rules ADD COLUMN IF NOT EXISTS priority TEXT;

-- v3 Phase8（設計案82-83番）：既存インストール向け列追加
ALTER TABLE trade_candidates ADD COLUMN IF NOT EXISTS sector TEXT;
ALTER TABLE trade_candidates ADD COLUMN IF NOT EXISTS margin_ratio NUMERIC;

-- Trade Cockpit v3-4（ポジションタブ拡張）：SL/TPをportfolioに保存できるようにする列追加。
-- トレーリング状態（OFF/初期SL/建値/利益保護/トレーリング）は現在値・平均取得単価・
-- initial_stop・current_stopから毎回導出できるため、専用列は追加しない（不要なスキーマ変更を避ける）。
ALTER TABLE portfolio ADD COLUMN IF NOT EXISTS initial_stop NUMERIC;
ALTER TABLE portfolio ADD COLUMN IF NOT EXISTS current_stop NUMERIC;
ALTER TABLE portfolio ADD COLUMN IF NOT EXISTS target_1 NUMERIC;
ALTER TABLE portfolio ADD COLUMN IF NOT EXISTS target_2 NUMERIC;
"""


def init_schema(database_url):
    """テーブルを（無ければ）作成し、マルチユーザー化・ChatGPT連携の移行SQLも実行する。
    サーバー起動時に1回呼ぶ想定。失敗時は例外を投げる（起動時ログで気づけるようにするため、
    ここでは握りつぶさない）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return
    with pool.connection() as conn:
        conn.execute(_SCHEMA_SQL)
        conn.execute(_MIGRATE_MULTIUSER_SQL)
        conn.execute(_MIGRATE_CHATGPT_IMPORT_SQL)
        conn.commit()


# ---- daily_log / stock_judgments ----

_DAILY_LOG_COLS = ["date", "market_env", "us_market", "interest_rate", "fx", "oil",
                    "sector_strength", "chatgpt_view", "my_view", "reflection"]
_JUDGMENT_COLS = ["code", "name", "category", "entry_reason", "skip_reason", "exit_judgment",
                   "supply_demand", "earnings_eval", "valuation_eval", "theme_eval", "chart_eval",
                   "chatgpt_judgment", "my_judgment", "actual_trade", "trade_result", "journal_id",
                   "execution_status", "mental_state", "user_decision", "ai_decision"]


def create_daily_log(database_url, user_id, data, judgments=None):
    """dataは_DAILY_LOG_COLSのキーを持つdict。judgmentsは_JUDGMENT_COLSを持つdictのリスト
    （任意）。戻り値: 作成したdaily_logのid。"""
    pool = _get_pool(database_url)
    if pool is None:
        return None
    cols = [c for c in _DAILY_LOG_COLS if c in data]
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO daily_log (user_id, {', '.join(cols)}) "
                f"VALUES (%s, {', '.join(['%s'] * len(cols))}) RETURNING id",
                [user_id] + [data.get(c) for c in cols],
            )
            log_id = cur.fetchone()[0]
            for j in (judgments or []):
                jcols = [c for c in _JUDGMENT_COLS if c in j]
                cur.execute(
                    f"INSERT INTO stock_judgments (daily_log_id, {', '.join(jcols)}) "
                    f"VALUES (%s, {', '.join(['%s'] * len(jcols))})",
                    [log_id] + [j.get(c) for c in jcols],
                )
        conn.commit()
    return log_id


def update_daily_log(database_url, user_id, log_id, data):
    pool = _get_pool(database_url)
    if pool is None:
        return
    cols = [c for c in _DAILY_LOG_COLS if c in data]
    if not cols:
        return
    with pool.connection() as conn:
        conn.execute(
            f"UPDATE daily_log SET {', '.join(c + ' = %s' for c in cols)} WHERE id = %s AND user_id = %s",
            [data.get(c) for c in cols] + [log_id, user_id],
        )
        conn.commit()


def _daily_log_belongs_to(conn, user_id, daily_log_id):
    row = conn.execute("SELECT 1 FROM daily_log WHERE id = %s AND user_id = %s", [daily_log_id, user_id]).fetchone()
    return row is not None


def add_stock_judgment(database_url, user_id, daily_log_id, data):
    """daily_log_idが呼び出しユーザーのものであることを確認してから追加する
    （他ユーザーのdaily_logへ書き込めないようにするため）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return None
    jcols = [c for c in _JUDGMENT_COLS if c in data]
    with pool.connection() as conn:
        if not _daily_log_belongs_to(conn, user_id, daily_log_id):
            return None
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO stock_judgments (daily_log_id, {', '.join(jcols)}) "
                f"VALUES (%s, {', '.join(['%s'] * len(jcols))}) RETURNING id",
                [daily_log_id] + [data.get(c) for c in jcols],
            )
            new_id = cur.fetchone()[0]
        conn.commit()
    return new_id


def update_stock_judgment(database_url, user_id, judgment_id, data):
    pool = _get_pool(database_url)
    if pool is None:
        return
    jcols = [c for c in _JUDGMENT_COLS if c in data]
    if not jcols:
        return
    with pool.connection() as conn:
        conn.execute(
            f"UPDATE stock_judgments SET {', '.join(c + ' = %s' for c in jcols)} "
            f"WHERE id = %s AND daily_log_id IN (SELECT id FROM daily_log WHERE user_id = %s)",
            [data.get(c) for c in jcols] + [judgment_id, user_id],
        )
        conn.commit()


def delete_stock_judgment(database_url, user_id, judgment_id):
    pool = _get_pool(database_url)
    if pool is None:
        return
    with pool.connection() as conn:
        conn.execute(
            "DELETE FROM stock_judgments WHERE id = %s "
            "AND daily_log_id IN (SELECT id FROM daily_log WHERE user_id = %s)",
            [judgment_id, user_id],
        )
        conn.commit()


def delete_daily_log(database_url, user_id, log_id):
    """daily_logを削除する（stock_judgmentsはON DELETE CASCADEで連動削除される）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return
    with pool.connection() as conn:
        conn.execute("DELETE FROM daily_log WHERE id = %s AND user_id = %s", [log_id, user_id])
        conn.commit()


def list_daily_logs(database_url, user_id, date_from=None, date_to=None, code=None):
    """呼び出しユーザーの日次ログを、それぞれに紐づく銘柄評価（judgments配列）付きで返す。
    date_from/date_toで期間絞り込み、codeを指定すると該当銘柄の評価を含むログだけに絞り込む
    （後から分析する用途）。新しい日付順（降順）で返す。"""
    pool = _get_pool(database_url)
    if pool is None:
        return []
    where, params = ["user_id = %s"], [user_id]
    if date_from:
        where.append("date >= %s")
        params.append(date_from)
    if date_to:
        where.append("date <= %s")
        params.append(date_to)
    if code:
        where.append("id IN (SELECT daily_log_id FROM stock_judgments WHERE code = %s)")
        params.append(code)
    where_sql = "WHERE " + " AND ".join(where)
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(f"SELECT * FROM daily_log {where_sql} ORDER BY date DESC, id DESC", params)
            logs = cur.fetchall()
            if not logs:
                return []
            log_ids = [r["id"] for r in logs]
            cur.execute("SELECT * FROM stock_judgments WHERE daily_log_id = ANY(%s) ORDER BY id", [log_ids])
            judgments = cur.fetchall()
    by_log = {}
    for j in judgments:
        by_log.setdefault(j["daily_log_id"], []).append(_row_to_json(j))
    out = []
    for r in logs:
        d = _row_to_json(r)
        d["judgments"] = by_log.get(r["id"], [])
        out.append(d)
    return out


def _row_to_json(row):
    """psycopgのdict_rowはdatetime・Decimal等をそのまま返すため、JSON化できる形に変換する。
    2026-09-02判明：NUMERIC列（trade_candidatesのrs_score等）はDecimalで返り、標準の
    json.dumpsではシリアライズできず例外→レスポンス未送信のままクラッシュしていた
    （curlからは空レスポンスに見える）。float変換で対処する。"""
    out = {}
    for k, v in row.items():
        if hasattr(v, "isoformat"):
            out[k] = v.isoformat()
        elif isinstance(v, decimal.Decimal):
            out[k] = float(v)
        else:
            out[k] = v
    return out


# ---- journal（売買記録） ----
# 旧localStorage側のjournalはJSのcamelCaseキー（{id,createdAt,code,name,action,price,shares,
# entryPlan,marketEnvAtEntry,reason,result,lessonNote}、trade-cockpit.html参照）のまま。
# フロントのコード変更を最小限にするため、DB列（snake_case）との変換をここで吸収する。
_JOURNAL_COLS = ["id", "code", "name", "action", "price", "shares", "entry_plan",
                  "market_env_at_entry", "reason", "result", "lesson_note", "created_at"]
_JOURNAL_CAMEL_TO_SNAKE = {
    "createdAt": "created_at", "entryPlan": "entry_plan",
    "marketEnvAtEntry": "market_env_at_entry", "lessonNote": "lesson_note",
}
_JOURNAL_SNAKE_TO_CAMEL = {v: k for k, v in _JOURNAL_CAMEL_TO_SNAKE.items()}


def _journal_row_to_camel(row):
    d = _row_to_json(row)
    d.pop("user_id", None)
    return {_JOURNAL_SNAKE_TO_CAMEL.get(k, k): v for k, v in d.items()}


def list_journal(database_url, user_id):
    pool = _get_pool(database_url)
    if pool is None:
        return []
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM journal WHERE user_id = %s ORDER BY created_at DESC", [user_id])
            return [_journal_row_to_camel(r) for r in cur.fetchall()]


def upsert_journal_entry(database_url, user_id, entry):
    """entryは旧localStorage形式のcamelCaseキーを持つdict（idは必須）。既存なら更新、
    無ければ新規作成。entryPlanはdict想定（JSONB列にそのまま渡す）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return
    entry = {_JOURNAL_CAMEL_TO_SNAKE.get(k, k): v for k, v in entry.items()}
    cols = [c for c in _JOURNAL_COLS if c in entry]
    values = []
    for c in cols:
        v = entry.get(c)
        values.append(json.dumps(v) if c == "entry_plan" and v is not None else v)
    placeholders = []
    for c in cols:
        placeholders.append("%s::jsonb" if c == "entry_plan" else "%s")
    update_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c != "id")
    with pool.connection() as conn:
        conn.execute(
            f"INSERT INTO journal (user_id, {', '.join(cols)}) VALUES (%s, {', '.join(placeholders)}) "
            f"ON CONFLICT (user_id, id) DO UPDATE SET {update_clause}",
            [user_id] + values,
        )
        conn.commit()


def delete_journal_entry(database_url, user_id, entry_id):
    pool = _get_pool(database_url)
    if pool is None:
        return
    with pool.connection() as conn:
        conn.execute("DELETE FROM journal WHERE id = %s AND user_id = %s", [entry_id, user_id])
        conn.commit()


# ---- investment_rules（マイルール） ----
# 旧localStorage側のmyRulesもJSのcamelCaseキー（{id,text,active,createdAt}）。journalと同様、
# createdAtだけDB列（created_at）と変換する。

def _rule_row_to_camel(row):
    d = _row_to_json(row)
    d.pop("user_id", None)
    d["createdAt"] = d.pop("created_at", None)
    return d


def list_rules(database_url, user_id):
    pool = _get_pool(database_url)
    if pool is None:
        return []
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM investment_rules WHERE user_id = %s ORDER BY created_at DESC NULLS LAST", [user_id])
            return [_rule_row_to_camel(r) for r in cur.fetchall()]


def upsert_rule(database_url, user_id, rule):
    """ruleは旧localStorage形式{id,text,active,createdAt}に加え、v3 Phase6（設計案56番）で
    rule_code/value/unit/priorityを任意で持てるようにした（無ければNULLのまま＝旧来の
    自由テキストルールと同じ挙動）。既存なら更新、無ければ新規作成。"""
    pool = _get_pool(database_url)
    if pool is None:
        return
    rule = {**rule, "created_at": rule.get("createdAt", rule.get("created_at"))}
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO investment_rules (user_id, id, text, active, created_at, rule_code, value, unit, priority) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (user_id, id) DO UPDATE SET text = EXCLUDED.text, active = EXCLUDED.active, "
            "rule_code = EXCLUDED.rule_code, value = EXCLUDED.value, unit = EXCLUDED.unit, priority = EXCLUDED.priority",
            [user_id, rule.get("id"), rule.get("text"), rule.get("active"), rule.get("created_at"),
             rule.get("rule_code"), rule.get("value"), rule.get("unit"), rule.get("priority")],
        )
        conn.commit()


# v3 Phase6（設計案57番）：初期ルール候補。有効化はユーザーの明示操作（「初期ルールを追加」
# ボタン）のみで行い、勝手に既存のマイルールへ割り込ませない。rule_codeを固定idにしているため
# 複数回押しても増殖しない（upsertで上書きになるだけ）。
DEFAULT_STRUCTURED_RULES = [
    {"rule_code": "SWING_STOP_LOSS", "text": "スイングは-10%で損切り", "value": -10, "unit": "percent", "priority": "CRITICAL"},
    {"rule_code": "NO_FALLING_KNIFE", "text": "落ちるナイフは掴まない（下げ止まり未確認では入らない）", "value": None, "unit": "boolean", "priority": "CRITICAL"},
    {"rule_code": "NO_FOMO_CHASE", "text": "急騰を追わない", "value": None, "unit": "boolean", "priority": "HIGH"},
    {"rule_code": "USE_STOP_ORDER_DAYTRADE", "text": "デイトレードは逆指値を必ず入れる", "value": None, "unit": "boolean", "priority": "HIGH"},
    {"rule_code": "AVOID_EARNINGS_CROSS", "text": "決算をまたぐポジションは避ける", "value": None, "unit": "boolean", "priority": "NORMAL"},
    {"rule_code": "RELATIVE_STRENGTH_PRIORITY", "text": "相対的に強い銘柄を優先する", "value": None, "unit": "text", "priority": "NORMAL"},
]


def seed_default_structured_rules(database_url, user_id):
    """DEFAULT_STRUCTURED_RULESをまとめてupsertする。戻り値: 追加/更新した件数。"""
    n = 0
    for r in DEFAULT_STRUCTURED_RULES:
        upsert_rule(database_url, user_id, {
            "id": "structured-" + r["rule_code"].lower(), "text": r["text"], "active": True,
            "createdAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "rule_code": r["rule_code"], "value": r["value"], "unit": r["unit"], "priority": r["priority"],
        })
        n += 1
    return n


def delete_rule(database_url, user_id, rule_id):
    pool = _get_pool(database_url)
    if pool is None:
        return
    with pool.connection() as conn:
        conn.execute("DELETE FROM investment_rules WHERE id = %s AND user_id = %s", [rule_id, user_id])
        conn.commit()


# ---- investment_profile（投資プロフィール。2026-09-02新規） ----

def get_profile(database_url, user_id):
    pool = _get_pool(database_url)
    if pool is None:
        return None
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM investment_profile WHERE user_id = %s", [user_id])
            row = cur.fetchone()
            return _row_to_json(row) if row else None


def upsert_profile(database_url, user_id, data):
    """dataは{style_summary, risk_tolerance}のいずれか/両方を含むdict。"""
    pool = _get_pool(database_url)
    if pool is None:
        return
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO investment_profile (user_id, style_summary, risk_tolerance, updated_at) "
            "VALUES (%s, %s, %s, now()) "
            "ON CONFLICT (user_id) DO UPDATE SET "
            "style_summary = COALESCE(EXCLUDED.style_summary, investment_profile.style_summary), "
            "risk_tolerance = COALESCE(EXCLUDED.risk_tolerance, investment_profile.risk_tolerance), "
            "updated_at = now()",
            [user_id, data.get("style_summary"), data.get("risk_tolerance")],
        )
        conn.commit()


# ---- ChatGPT連携（2026-09-02新規、Phase1）：ChatGPTが出力した投資ログJSONを取り込む ----
# 有料AI APIは使わず、「ChatGPTで相談→JSON出力→ここへ手動貼り付け」という半自動フローの
# 保存先。JSONのwatchlist/decisionsはcodeで突き合わせてstock_judgments 1行にマージする
# （どちらか片方にしか無い項目も許容する）。

ALLOWED_EXECUTION = ["BUY", "SELL", "WAIT", "WATCH", "NO_TRADE", "MISSED", "CANCELLED",
                      "STOP_LOSS", "TAKE_PROFIT"]
_CODE_RE_LOOSE = re.compile(r"^[0-9A-Za-z.\-]{1,12}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def validate_chatgpt_payload(payload):
    """貼り付けJSONを解析したdictを検証し、エラーメッセージのリストを返す（空なら合格）。
    フロント側でも同じ内容の検証をJSで行うが（即時フィードバック用）、ここでのサーバー側検証が
    最終防衛線（フロントを経由しない直接APIコールや改変に備える）。"""
    errors = []
    if not isinstance(payload, dict):
        return ["JSONのトップレベルはオブジェクトである必要があります"]
    date = payload.get("date")
    if not date or not isinstance(date, str) or not _DATE_RE.match(date):
        errors.append("date は YYYY-MM-DD 形式の文字列で必須です")
    watchlist = payload.get("watchlist")
    if watchlist is not None and not isinstance(watchlist, list):
        errors.append("watchlist は配列である必要があります")
    for i, w in enumerate(watchlist or []):
        if not isinstance(w, dict) or not w.get("code"):
            errors.append(f"watchlist[{i}] に code がありません")
        elif not _CODE_RE_LOOSE.match(str(w.get("code"))):
            errors.append(f"watchlist[{i}].code の形式が不正です: {w.get('code')}")
    decisions = payload.get("decisions")
    if decisions is not None and not isinstance(decisions, list):
        errors.append("decisions は配列である必要があります")
    for i, d in enumerate(decisions or []):
        if not isinstance(d, dict) or not d.get("code"):
            errors.append(f"decisions[{i}] に code がありません")
            continue
        if not _CODE_RE_LOOSE.match(str(d.get("code"))):
            errors.append(f"decisions[{i}].code の形式が不正です: {d.get('code')}")
        execution = d.get("execution")
        if execution and execution not in ALLOWED_EXECUTION:
            errors.append(f"decisions[{i}].execution \"{execution}\" は許可された値ではありません"
                           f"（許可: {', '.join(ALLOWED_EXECUTION)}）")
    rule_updates = payload.get("rule_updates")
    if rule_updates is not None and not isinstance(rule_updates, list):
        errors.append("rule_updates は配列である必要があります")
    return errors


def _payload_hash(raw_payload):
    raw_str = json.dumps(raw_payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw_str.encode("utf-8")).hexdigest()


def find_chatgpt_import_duplicate(database_url, user_id, raw_payload):
    """同一内容（payload_hash一致）の取り込み済みレコードがあれば返す（無ければNone）。
    保存前のプレビュー段階で警告表示するために使う（10番の重複防止のプレビュー版）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return None
    h = _payload_hash(raw_payload)
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, import_date, imported_at FROM chatgpt_imports WHERE user_id = %s AND payload_hash = %s",
                [user_id, h],
            )
            row = cur.fetchone()
            return _row_to_json(row) if row else None


def list_chatgpt_imports(database_url, user_id, limit=30):
    pool = _get_pool(database_url)
    if pool is None:
        return []
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, import_date, imported_at, daily_log_id FROM chatgpt_imports "
                "WHERE user_id = %s ORDER BY imported_at DESC LIMIT %s",
                [user_id, limit],
            )
            return [_row_to_json(r) for r in cur.fetchall()]


def save_chatgpt_import(database_url, user_id, payload, force=False):
    """検証済み（validate_chatgpt_payloadでエラー0件確認済み）のpayloadをNeonへ保存する。
    - daily_log 1行（市場環境・強弱セクター・raw_payload）
    - stock_judgments N行（watchlist・decisionsをcodeで突き合わせてマージ）
    - investment_rules（rule_updatesがあれば追加。既存ルールの上書きはしない＝新規追加のみ）
    - chatgpt_imports 1行（取り込み履歴。payload_hashで重複検出）
    同一内容が取り込み済みならforce=Trueでない限り保存せずエラーを返す。
    戻り値: {"error": ...} または {"dailyLogId":.., "importId":.., "judgments":N, "rulesAdded":N}。"""
    pool = _get_pool(database_url)
    if pool is None:
        return {"error": "DB未設定（DATABASE_URLが未設定、またはpsycopg未インストール）"}

    dup = find_chatgpt_import_duplicate(database_url, user_id, payload)
    if dup and not force:
        return {"error": f"同じ内容のログは既に取り込み済みです（{dup['import_date']}に取り込み、import_id {dup['id']}）"}

    market = payload.get("market") or {}
    h = _payload_hash(payload)
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO daily_log (user_id, date, market_env, chatgpt_view, reflection, "
                "strong_sectors, weak_sectors, raw_payload) "
                "VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb) RETURNING id",
                [
                    user_id, payload.get("date"), market.get("condition"), market.get("summary"),
                    payload.get("review"),
                    json.dumps(market.get("strong_sectors") or [], ensure_ascii=False),
                    json.dumps(market.get("weak_sectors") or [], ensure_ascii=False),
                    json.dumps(payload, ensure_ascii=False),
                ],
            )
            log_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO chatgpt_imports (user_id, import_date, payload_hash, raw_payload, daily_log_id) "
                "VALUES (%s, %s, %s, %s::jsonb, %s) RETURNING id",
                [user_id, payload.get("date"), h, json.dumps(payload, ensure_ascii=False), log_id],
            )
            import_id = cur.fetchone()[0]
        conn.commit()

    # watchlist・decisionsをcodeで突き合わせてstock_judgments 1行にマージ
    by_code = {}
    for w in payload.get("watchlist") or []:
        code = w.get("code")
        if not code:
            continue
        row = by_code.setdefault(code, {"code": code})
        if w.get("name"):
            row["name"] = w["name"]
        if w.get("status"):
            row["category"] = w["status"]
        if w.get("reason"):
            row["entry_reason"] = w["reason"]
    for d in payload.get("decisions") or []:
        code = d.get("code")
        if not code:
            continue
        row = by_code.setdefault(code, {"code": code})
        if d.get("user_decision"):
            row["user_decision"] = d["user_decision"]
        if d.get("ai_decision"):
            row["ai_decision"] = d["ai_decision"]
        if d.get("execution"):
            row["execution_status"] = d["execution"]
        if d.get("mental_state"):
            row["mental_state"] = d["mental_state"]
        if d.get("reason"):
            # watchlist由来のentry_reasonが既にあれば上書きしない（見送り理由はskip_reasonへ）
            key = "skip_reason" if d.get("execution") == "NO_TRADE" else "entry_reason"
            row.setdefault(key, d["reason"])

    n_judgments = 0
    for fields in by_code.values():
        jid = add_stock_judgment(database_url, user_id, log_id, fields)
        if jid is not None:
            n_judgments += 1

    n_rules = 0
    for ru in payload.get("rule_updates") or []:
        text = ru if isinstance(ru, str) else (ru or {}).get("text")
        if not text:
            continue
        upsert_rule(database_url, user_id, {
            "id": "chatgpt-" + uuid.uuid4().hex[:8],
            "text": text,
            "active": True,
            "createdAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        })
        n_rules += 1

    return {"dailyLogId": log_id, "importId": import_id, "judgments": n_judgments, "rulesAdded": n_rules}


# ---- 一括移行（旧localStorageのjournal・myRulesをまとめて取り込む） ----

def migrate_legacy(database_url, user_id, journal_list, rules_list):
    """フロントのlocalStorageに残っている旧journal・myRulesをまとめてDBへ取り込む
    （「記録」タブの「サーバーDBへ移行」ボタンから1回だけ呼ばれる想定。IDが同じものは
    上書きになるため、複数回押しても壊れない＝冪等）。戻り値: {journal: 件数, rules: 件数}。"""
    n_j = n_r = 0
    for entry in (journal_list or []):
        entry = dict(entry)
        entry["id"] = str(entry.get("id"))
        upsert_journal_entry(database_url, user_id, entry)
        n_j += 1
    for rule in (rules_list or []):
        rule = dict(rule)
        rule["id"] = str(rule.get("id"))
        upsert_rule(database_url, user_id, rule)
        n_r += 1
    return {"journal": n_j, "rules": n_r}


# ---- trade_candidates（「今日の候補」。2026-09-02新規、Trade Cockpit v2 Phase1） ----
# 監視銘柄タブでStatus・RS Scoreを見ながらユーザーが手動で拾った銘柄を保存する。
# 自動判定結果ではなく「ユーザーがその時点でその状態だと判断した」記録として扱う。

TRADE_CANDIDATE_STATUSES = ["WATCH", "WAIT", "BUY_CANDIDATE", "SKIP"]


def create_trade_candidate(database_url, user_id, data):
    """dataは{code,name,status,rs_score,market_rs,sector_rs,note,virtual_entry_price,
    virtual_entry_time}のいずれかを含むdict（code・statusは必須）。virtual_entry_price/
    virtual_entry_timeは「仮想IN」（v2 Phase7・設計案12・40番：監視銘柄一覧から見送り銘柄を
    仮想的にエントリーしたことにして後から結果を追跡する）で使う。戻り値: 作成したidまたはNone。"""
    pool = _get_pool(database_url)
    if pool is None:
        return None
    if not data.get("code") or data.get("status") not in TRADE_CANDIDATE_STATUSES:
        return None
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO trade_candidates (user_id, code, name, status, rs_score, market_rs, sector_rs, note, "
                "virtual_entry_price, virtual_entry_time, sector, margin_ratio) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                [user_id, data.get("code"), data.get("name"), data.get("status"),
                 data.get("rs_score"), data.get("market_rs"), data.get("sector_rs"), data.get("note"),
                 data.get("virtual_entry_price"), data.get("virtual_entry_time"),
                 data.get("sector"), data.get("margin_ratio")],
            )
            new_id = cur.fetchone()[0]
        conn.commit()
    return new_id


# ---- 仮想トレード追跡（trade_candidates.checkpoints。2026-09-02新規、Trade Cockpit v2 Phase7） ----
# 見送り銘柄の「仮想IN」後、30分後/1時間後/大引け/翌営業日/3営業日後/5営業日後の価格を手動で
# 記録できるようにする（自動追跡には定期実行の仕組みが必要になり複雑化するため、Phase7は
# ユーザーが見た時に記録する手動方式にとどめる。設計案21番「システムを複雑にしない」に沿う）。
TRADE_CANDIDATE_CHECKPOINT_LABELS = ["30m", "1h", "close", "next_day", "3d", "5d"]


def add_trade_candidate_checkpoint(database_url, user_id, candidate_id, label, price):
    """checkpoints(JSONB)に{label: {"price":.., "at":ISO8601}}を1件マージする。既存の同じlabelは
    上書きする（記録し直したい場合のため）。呼び出しユーザーの候補であることを確認してから更新する。"""
    pool = _get_pool(database_url)
    if pool is None:
        return False
    if label not in TRADE_CANDIDATE_CHECKPOINT_LABELS:
        return False
    entry = json.dumps({label: {"price": price, "at": datetime.datetime.now(datetime.timezone.utc).isoformat()}})
    with pool.connection() as conn:
        cur = conn.execute(
            "UPDATE trade_candidates SET checkpoints = COALESCE(checkpoints, '{}'::jsonb) || %s::jsonb "
            "WHERE id = %s AND user_id = %s",
            [entry, candidate_id, user_id],
        )
        conn.commit()
        return cur.rowcount > 0


# ---- 監視銘柄→投資判断ログへのワンクリック記録（2026-09-02新規、Trade Cockpit v2 Phase5） ----
# 「監視→分析→判断→記録」の導線接続（設計案5・26・39番）。監視銘柄タブの行から直接、
# 当日のdaily_log（無ければ自動作成）にstock_judgmentを1件追加する。

def get_or_create_daily_log(database_url, user_id, date):
    """(user_id, date)のdaily_logがあればそのidを返し、無ければ空のdaily_logを作成して返す。
    「記録」ボタン用：投資判断ログタブで手動作成済みの当日ログがあればそれに相乗りし、
    無ければ自動で作る（ユーザーに「新規ログ」フォームへの入力を強制しない）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return None
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM daily_log WHERE user_id = %s AND date = %s ORDER BY id LIMIT 1", [user_id, date])
            row = cur.fetchone()
            if row:
                return row[0]
            cur.execute("INSERT INTO daily_log (user_id, date) VALUES (%s, %s) RETURNING id", [user_id, date])
            new_id = cur.fetchone()[0]
        conn.commit()
    return new_id


def list_trade_candidates(database_url, user_id, since_date=None):
    """呼び出しユーザーの「今日の候補」を新しい順に返す。since_dateを指定すると
    その日付（YYYY-MM-DD、JST想定はフロント側で計算）以降に絞り込む（未指定時は当日分のみ）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return []
    if since_date is None:
        since_date = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).strftime("%Y-%m-%d")
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM trade_candidates WHERE user_id = %s AND created_at >= %s::date "
                "ORDER BY created_at DESC",
                [user_id, since_date],
            )
            return [_row_to_json(r) for r in cur.fetchall()]


def delete_trade_candidate(database_url, user_id, candidate_id):
    pool = _get_pool(database_url)
    if pool is None:
        return
    with pool.connection() as conn:
        conn.execute("DELETE FROM trade_candidates WHERE id = %s AND user_id = %s", [candidate_id, user_id])
        conn.commit()


# ---- 統計ダッシュボード（2026-09-02新規、Trade Cockpit v2 Phase8） ----
# AI APIは使わずSQLの集計のみ（設計案52-60番）。journal.resultは{"成功","失敗","引分","未定"}の
# 固定値のため勝率は計算できるが、金額の損益（平均利益・平均損失・Profit Factor）は journal に
# 数値P/L列が無く計算できない。無理に推測せず、「未対応（journalに金額列が無いため）」を
# 明記して返す（83番：データ欠損はUNKNOWNとして扱う方針）。

def get_stats(database_url, user_id):
    """統計ダッシュボード用の集計をまとめて返す。DB未設定時は全項目0/空で返す。"""
    pool = _get_pool(database_url)
    empty = {
        "journal": {"total": 0, "byResult": {}, "winRate": None},
        "judgments": {"total": 0, "byExecutionStatus": {}, "byMentalState": {},
                       "decisionAgreement": {"agree": 0, "disagree": 0, "rate": None}},
        "candidates": {"total": 0, "byStatus": {}, "avgRsScore": None, "byRsBucket": {}, "bySector": {}},
        "note": "平均利益・平均損失・Profit Factorはjournalに金額の損益列が無いため未対応です。",
    }
    if pool is None:
        return empty
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT result, COUNT(*) AS n FROM journal WHERE user_id = %s GROUP BY result", [user_id])
            by_result = {r["result"] or "未定": r["n"] for r in cur.fetchall()}
            j_total = sum(by_result.values())
            wins, losses, draws = by_result.get("成功", 0), by_result.get("失敗", 0), by_result.get("引分", 0)
            decided = wins + losses + draws
            win_rate = round(wins / decided * 100, 1) if decided else None

            cur.execute(
                "SELECT sj.execution_status, COUNT(*) AS n FROM stock_judgments sj "
                "JOIN daily_log dl ON dl.id = sj.daily_log_id WHERE dl.user_id = %s "
                "GROUP BY sj.execution_status", [user_id],
            )
            by_exec = {(r["execution_status"] or "未設定"): r["n"] for r in cur.fetchall()}
            j_judg_total = sum(by_exec.values())

            cur.execute(
                "SELECT sj.mental_state, COUNT(*) AS n FROM stock_judgments sj "
                "JOIN daily_log dl ON dl.id = sj.daily_log_id WHERE dl.user_id = %s AND sj.mental_state IS NOT NULL "
                "GROUP BY sj.mental_state", [user_id],
            )
            by_mental = {r["mental_state"]: r["n"] for r in cur.fetchall()}

            cur.execute(
                "SELECT COUNT(*) FILTER (WHERE sj.user_decision = sj.ai_decision) AS agree, "
                "COUNT(*) FILTER (WHERE sj.user_decision IS NOT NULL AND sj.ai_decision IS NOT NULL "
                "AND sj.user_decision != sj.ai_decision) AS disagree "
                "FROM stock_judgments sj JOIN daily_log dl ON dl.id = sj.daily_log_id "
                "WHERE dl.user_id = %s AND sj.user_decision IS NOT NULL AND sj.ai_decision IS NOT NULL",
                [user_id],
            )
            agree_row = cur.fetchone() or {"agree": 0, "disagree": 0}
            agree, disagree = agree_row["agree"] or 0, agree_row["disagree"] or 0
            agree_total = agree + disagree
            agree_rate = round(agree / agree_total * 100, 1) if agree_total else None

            cur.execute("SELECT status, COUNT(*) AS n, AVG(rs_score) AS avg_rs FROM trade_candidates "
                        "WHERE user_id = %s GROUP BY status", [user_id])
            cand_rows = cur.fetchall()
            by_status = {r["status"]: r["n"] for r in cand_rows}
            cand_total = sum(by_status.values())
            cur.execute("SELECT AVG(rs_score) AS avg_rs FROM trade_candidates WHERE user_id = %s", [user_id])
            avg_rs_row = cur.fetchone()
            avg_rs = float(avg_rs_row["avg_rs"]) if avg_rs_row and avg_rs_row["avg_rs"] is not None else None

            # v3 Phase8（設計案81番）：RS Score帯別の件数。「全面安で強い銘柄を狙う」戦略が
            # 実際に機能しているかを後から検証できるようにする（AI不使用、SQL集計のみ）。
            cur.execute("SELECT rs_score FROM trade_candidates WHERE user_id = %s AND rs_score IS NOT NULL", [user_id])
            rs_bucket_defs = [("90+", 90, 999), ("75-89", 75, 90), ("50-74", 50, 75), ("25-49", 25, 50), ("<25", -999, 25)]
            by_rs_bucket = {label: 0 for label, _, _ in rs_bucket_defs}
            for r in cur.fetchall():
                v = float(r["rs_score"])
                for label, lo, hi in rs_bucket_defs:
                    if lo <= v < hi:
                        by_rs_bucket[label] += 1
                        break

            # v3 Phase8（設計案83番）：セクター別件数。sector列は2026-09-02以降に保存された
            # candidatesのみ持つため、それ以前のデータはUNKNOWNとして扱う（0として誤魔化さない）。
            cur.execute("SELECT COALESCE(sector, 'UNKNOWN') AS sector, COUNT(*) AS n FROM trade_candidates "
                        "WHERE user_id = %s GROUP BY sector", [user_id])
            by_sector = {r["sector"]: r["n"] for r in cur.fetchall()}

    return {
        "journal": {"total": j_total, "byResult": by_result, "winRate": win_rate},
        "judgments": {"total": j_judg_total, "byExecutionStatus": by_exec, "byMentalState": by_mental,
                       "decisionAgreement": {"agree": agree, "disagree": disagree, "rate": agree_rate}},
        "candidates": {"total": cand_total, "byStatus": by_status,
                        "avgRsScore": round(avg_rs, 1) if avg_rs is not None else None,
                        "byRsBucket": by_rs_bucket, "bySector": by_sector},
        "note": "平均利益・平均損失・Profit Factorはjournalに金額の損益列が無いため未対応です。",
    }


# ---- news_feedback（2026-09-02新規、Trade Cockpit v3 Phase4） ----

def create_news_feedback(database_url, user_id, data):
    """dataは{title,source,matched_stock_code,matched_keyword,category}のいずれかを含むdict
    （titleは必須）。戻り値: 作成したidまたはNone。"""
    pool = _get_pool(database_url)
    if pool is None:
        return None
    if not data.get("title"):
        return None
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO news_feedback (user_id, title, source, matched_stock_code, matched_keyword, category) "
                "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                [user_id, data.get("title"), data.get("source"), data.get("matched_stock_code"),
                 data.get("matched_keyword"), data.get("category")],
            )
            new_id = cur.fetchone()[0]
        conn.commit()
    return new_id


# ---- watchlist_imports（2026-09-02新規、Trade Cockpit v3 Phase5） ----
# ChatGPTスクリーンショット認識結果の監視銘柄一括取り込み履歴。実watchlistはブラウザ側で
# 管理するため（71-76番：既存アーキテクチャを維持）、ここは履歴・重複防止専用。

def find_watchlist_import_duplicate(database_url, user_id, raw_payload):
    pool = _get_pool(database_url)
    if pool is None:
        return None
    h = _payload_hash(raw_payload)
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, imported_at, applied_mode, added_count FROM watchlist_imports "
                "WHERE user_id = %s AND payload_hash = %s", [user_id, h],
            )
            row = cur.fetchone()
            return _row_to_json(row) if row else None


def save_watchlist_import(database_url, user_id, raw_payload, applied_mode, added_count, force=False):
    pool = _get_pool(database_url)
    if pool is None:
        return {"error": "DB未設定"}
    dup = find_watchlist_import_duplicate(database_url, user_id, raw_payload)
    if dup and not force:
        return {"error": f"同じ内容は既に取り込み済みです（{dup['imported_at']}）"}
    h = _payload_hash(raw_payload)
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO watchlist_imports (user_id, payload_hash, raw_payload, applied_mode, added_count) "
                "VALUES (%s, %s, %s::jsonb, %s, %s) RETURNING id",
                [user_id, h, json.dumps(raw_payload, ensure_ascii=False), applied_mode, added_count],
            )
            new_id = cur.fetchone()[0]
        conn.commit()
    return {"id": new_id}


def list_watchlist_imports(database_url, user_id, limit=30):
    pool = _get_pool(database_url)
    if pool is None:
        return []
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, imported_at, applied_mode, added_count FROM watchlist_imports "
                "WHERE user_id = %s ORDER BY imported_at DESC LIMIT %s", [user_id, limit],
            )
            return [_row_to_json(r) for r in cur.fetchall()]


# ---- watchlist（2026-09-03新規、Trade Cockpit v3-2：Neonをwatchlist本体のSingle Source of
# Truthにする） ----
# フロントのcamelCaseキー（tvSymbol）とDB列（tv_symbol）の変換のみここで吸収する。

_WATCHLIST_CAMEL_TO_SNAKE = {"tvSymbol": "tv_symbol"}
_WATCHLIST_SNAKE_TO_CAMEL = {v: k for k, v in _WATCHLIST_CAMEL_TO_SNAKE.items()}


def _watchlist_row_to_camel(row):
    d = _row_to_json(row)
    d.pop("user_id", None)
    return {_WATCHLIST_SNAKE_TO_CAMEL.get(k, k): v for k, v in d.items()}


def list_watchlist(database_url, user_id, market=None):
    """呼び出しユーザーのwatchlist（active=trueのみ）をadded_at昇順で返す。marketを指定すると
    JP/USで絞り込む。フロントのwatchlist配列とほぼ同じ形（tvSymbol等camelCase）で返す。"""
    pool = _get_pool(database_url)
    if pool is None:
        return []
    where, params = ["user_id = %s", "active = true"], [user_id]
    if market:
        where.append("market = %s")
        params.append(market)
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(f"SELECT * FROM watchlist WHERE {' AND '.join(where)} ORDER BY added_at", params)
            return [_watchlist_row_to_camel(r) for r in cur.fetchall()]


_WATCHLIST_COLS = ["name", "sector", "kana", "tv_symbol", "theme", "watch", "note", "source", "added_reason"]


def _upsert_watchlist_item_conn(conn, user_id, item):
    """upsert_watchlist_itemの実処理。既に開いているconnを使う（migrate_watchlist_from_clientが
    309件规模を1本の接続で処理できるようにするため、_get_pool()を介した毎回の新規接続を避ける）。
    呼び出し側でconn.commit()すること。"""
    if not item.get("code"):
        return False
    item = {_WATCHLIST_CAMEL_TO_SNAKE.get(k, k): v for k, v in item.items()}
    market = item.get("market") or "JP"
    cols = [c for c in _WATCHLIST_COLS if c in item]
    conn.execute(
        f"INSERT INTO watchlist (user_id, code, market, {', '.join(cols)}) "
        f"VALUES (%s, %s, %s, {', '.join(['%s'] * len(cols))}) "
        f"ON CONFLICT (user_id, code, market) DO UPDATE SET "
        f"{', '.join(c + ' = EXCLUDED.' + c for c in cols)}, updated_at = now(), active = true",
        [user_id, item.get("code"), market] + [item.get(c) for c in cols],
    )
    return True


def upsert_watchlist_item(database_url, user_id, item):
    """itemは{code,market,name,sector,kana,tvSymbol,theme,watch,note,source,added_reason}の
    いずれかを含むdict（code必須、marketは省略時JP）。既存なら更新、無ければ新規作成。"""
    pool = _get_pool(database_url)
    if pool is None:
        return False
    with pool.connection() as conn:
        ok = _upsert_watchlist_item_conn(conn, user_id, item)
        conn.commit()
    return ok


def delete_watchlist_item(database_url, user_id, code, market=None):
    pool = _get_pool(database_url)
    if pool is None:
        return
    with pool.connection() as conn:
        if market:
            conn.execute("DELETE FROM watchlist WHERE user_id = %s AND code = %s AND market = %s", [user_id, code, market])
        else:
            conn.execute("DELETE FROM watchlist WHERE user_id = %s AND code = %s", [user_id, code])
        conn.commit()


def migrate_watchlist_from_client(database_url, user_id, items):
    """ブラウザに残っている旧localStorage watchlistをまとめてNeonへ取り込む（1回だけ呼ばれる
    想定。既存コードは上書きになるため、複数回押しても壊れない＝冪等）。戻り値: 件数。
    2026-09-03判明：300件規模だと1件ごとに新規接続していては非常に遅い（Neonへの接続確立
    コストが件数分かかる）ため、1本の接続を使い回して処理する。"""
    pool = _get_pool(database_url)
    if pool is None:
        return 0
    n = 0
    with pool.connection() as conn:
        for item in (items or []):
            if _upsert_watchlist_item_conn(conn, user_id, {**item, "source": item.get("source") or "manual"}):
                n += 1
        conn.commit()
    return n


# ---- portfolio（2026-09-03新規、Trade Cockpit v3-2。localStorageに存在しなかった新規機能のため
# 移行データは無く、最初からNeonが正） ----

def list_portfolio(database_url, user_id):
    pool = _get_pool(database_url)
    if pool is None:
        return []
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM portfolio WHERE user_id = %s AND active = true ORDER BY created_at", [user_id])
            rows = cur.fetchall()
            out = []
            for r in rows:
                d = _row_to_json(r)
                d.pop("user_id", None)
                out.append(d)
            return out


_PORTFOLIO_COLS = ["name", "quantity", "average_price", "acquired_at", "memo",
                   "initial_stop", "current_stop", "target_1", "target_2"]  # marketはINSERT文で別途固定列として扱うためここには含めない


def upsert_portfolio_item(database_url, user_id, item):
    """itemは{code,market,name,quantity,average_price,acquired_at,memo}のいずれかを含むdict
    （code必須、marketは省略時JP）。既存なら更新、無ければ新規作成。"""
    pool = _get_pool(database_url)
    if pool is None:
        return False
    if not item.get("code"):
        return False
    market = item.get("market") or "JP"
    if item.get("acquired_at") == "":
        item = {**item, "acquired_at": None}  # 空文字はTIMESTAMPTZ列に直接入らないためnullに変換
    cols = [c for c in _PORTFOLIO_COLS if c in item]
    with pool.connection() as conn:
        conn.execute(
            f"INSERT INTO portfolio (user_id, code, market, {', '.join(cols)}) "
            f"VALUES (%s, %s, %s, {', '.join(['%s'] * len(cols))}) "
            f"ON CONFLICT (user_id, code, market) DO UPDATE SET "
            f"{', '.join(c + ' = EXCLUDED.' + c for c in cols)}, updated_at = now(), active = true",
            [user_id, item.get("code"), market] + [item.get(c) for c in cols],
        )
        conn.commit()
    return True


def delete_portfolio_item(database_url, user_id, code, market=None):
    pool = _get_pool(database_url)
    if pool is None:
        return
    with pool.connection() as conn:
        if market:
            conn.execute("DELETE FROM portfolio WHERE user_id = %s AND code = %s AND market = %s", [user_id, code, market])
        else:
            conn.execute("DELETE FROM portfolio WHERE user_id = %s AND code = %s", [user_id, code])
        conn.commit()
