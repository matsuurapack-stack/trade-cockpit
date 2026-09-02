"""
投資判断ログ用のDBアクセスモジュール（2026-09-02 新規、同日中にマルチユーザー化）

- 保存先はNeon（無料枠のPostgreSQL）。PC・Renderクラウドの両方から同じDBに接続することで
  データを一本化する（接続先の切り替えはserver.py側のDATABASE_URL定数で行う）。
- 5テーブル構成：
  - daily_log          … 日次の相場観（地合い・米国市場・金利・為替・原油・セクター強弱等）
  - stock_judgments    … 銘柄ごとの評価（daily_logに従属。監視/保有/買い候補/見送りの評価一式）
  - journal            … 売買記録（旧localStorage "journal" の移行先。列構成はほぼ同じ）
  - investment_rules   … マイルール（旧localStorage "myRules" の移行先）
  - investment_profile … 投資プロフィール（AI相談機能Phase1で使う自己紹介的な情報。1ユーザー1行）
- 2026-09-02 マルチユーザー化：daily_log・journal・investment_rules・investment_profileは
  すべてuser_id（ログインユーザー名、server.pyの_authorized()参照）で分離する。journal・
  investment_rulesは既存の主キーがidだけだった（マルチユーザー化前は暗黙的に単一ユーザー
  だったため）ので、(user_id, id)の複合主キーに移行する。stock_judgmentsは自身は
  user_idを持たず、daily_log経由でスコープする（親のdaily_log_idがそのユーザーの
  daily_logであることを各関数側で確認してから操作する）。
- psycopg（PostgreSQL用ドライバ）が未インストール・DATABASE_URL未設定の環境でも他機能に
  影響しないよう、未導入時は全関数が空データ/Noneを返すだけにする（他のAPI連携と同じ方針）。
"""
import json
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
    reflection       TEXT
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
    mental_state      TEXT   -- 2026-09-02追加：fear|fomo|confident|uncertain|frustrated|revenge_trade|calm等
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

-- 2026-09-02新規：AI相談機能Phase1で使う投資プロフィール（投資スタイル・リスク許容度等の
-- 自由記述サマリ。1ユーザー1行）。まだ読み書きするAPIは無いが、schemaだけ先に用意しておく。
CREATE TABLE IF NOT EXISTS investment_profile (
    user_id         TEXT PRIMARY KEY,
    style_summary   TEXT,
    risk_tolerance  TEXT,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
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


def init_schema(database_url):
    """テーブルを（無ければ）作成し、マルチユーザー化の移行SQLも実行する。サーバー起動時に
    1回呼ぶ想定。失敗時は例外を投げる（起動時ログで気づけるようにするため、ここでは
    握りつぶさない）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return
    with pool.connection() as conn:
        conn.execute(_SCHEMA_SQL)
        conn.execute(_MIGRATE_MULTIUSER_SQL)
        conn.commit()


# ---- daily_log / stock_judgments ----

_DAILY_LOG_COLS = ["date", "market_env", "us_market", "interest_rate", "fx", "oil",
                    "sector_strength", "chatgpt_view", "my_view", "reflection"]
_JUDGMENT_COLS = ["code", "name", "category", "entry_reason", "skip_reason", "exit_judgment",
                   "supply_demand", "earnings_eval", "valuation_eval", "theme_eval", "chart_eval",
                   "chatgpt_judgment", "my_judgment", "actual_trade", "trade_result", "journal_id",
                   "execution_status", "mental_state"]


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
    """psycopgのdict_rowはdatetime等をそのまま返すため、JSON化できる形に変換する。"""
    out = {}
    for k, v in row.items():
        if hasattr(v, "isoformat"):
            out[k] = v.isoformat()
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
    """ruleは旧localStorage形式{id,text,active,createdAt}。既存なら更新、無ければ新規作成。"""
    pool = _get_pool(database_url)
    if pool is None:
        return
    rule = {**rule, "created_at": rule.get("createdAt", rule.get("created_at"))}
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO investment_rules (user_id, id, text, active, created_at) VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (user_id, id) DO UPDATE SET text = EXCLUDED.text, active = EXCLUDED.active",
            [user_id, rule.get("id"), rule.get("text"), rule.get("active"), rule.get("created_at")],
        )
        conn.commit()


def delete_rule(database_url, user_id, rule_id):
    pool = _get_pool(database_url)
    if pool is None:
        return
    with pool.connection() as conn:
        conn.execute("DELETE FROM investment_rules WHERE id = %s AND user_id = %s", [rule_id, user_id])
        conn.commit()


# ---- investment_profile（投資プロフィール。AI相談機能Phase1向け。2026-09-02新規） ----

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
