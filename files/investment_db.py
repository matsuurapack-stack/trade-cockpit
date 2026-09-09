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
import math
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

-- v3-8（ポジション管理ロジック再設計 Step1）：取引タイプ（DAY/SWING）。既存ポジションは
-- NULL（未設定）を許容し、UI側で設定を促す。HARD STOP（-10%ルール）等はSWING限定のため
-- この列が必須の起点になる。
ALTER TABLE portfolio ADD COLUMN IF NOT EXISTS trade_style TEXT;

-- v3-9（監視銘柄自動登録エンジン）：manual_registeredは「手動登録された監視銘柄がAUTOタグの
-- 期限切れで誤って自動削除される事故」を絶対に起こさないための明示的な列。DEFAULT trueなので、
-- 既存の全行（=これまでは全て手動登録）は移行なしでそのまま「手動登録」として扱われる
-- （後方互換性）。自動登録エンジン（server.py側の専用関数のみ）が新規行を作る場合だけ
-- 明示的にfalseを指定する。_WATCHLIST_COLS（クライアントの汎用保存エンドポイントが使う
-- 書き込み許可列リスト）には意図的に含めない＝クライアント側の通常の保存・同期処理からは
-- 物理的に書き込めない設計にすることで、事故の経路そのものを塞ぐ。
ALTER TABLE watchlist ADD COLUMN IF NOT EXISTS manual_registered BOOLEAN NOT NULL DEFAULT true;
-- auto_tagsも同様の理由で_WATCHLIST_COLSに含めない。形式：
-- {"AUTO_MOMENTUM_DAY": {"score": 82, "addedAt": "2026-09-04T09:00:00+09:00",
--   "expiresAt": "2026-09-05T09:00:00+09:00"}, "AUTO_BREAK": {...}}
-- のように理由キーごとに独立したスコア・追加日時・有効期限を持つ。専用のマージ関数
-- （merge_auto_tag）でキー単位の追加・上書きのみを行い、他の理由キーには触れない。
ALTER TABLE watchlist ADD COLUMN IF NOT EXISTS auto_tags JSONB;

-- v3-9続き（2026-09-05・PHASE 1 AUTO SIGNAL LOG）：5つの自動登録エンジン（MOMENTUM DAY/
-- AUTO_BREAK/AUTO_RS/AUTO_SECTOR_LEADER/AUTO_PULLBACK、将来のAUTO_REVERSAL/AUTO_VOLUME/
-- AUTO_EARNINGSも含む）共通の恒久履歴テーブル。watchlist.auto_tags（CURRENT/SEEN・期限切れで
-- 消える「現在状態」専用）とは役割を分離し、こちらは状態遷移（ENTER_CURRENT/EXIT_CURRENT/
-- REENTER_CURRENT/EXPIRE）が起きた時だけ記録する。同じCURRENT銘柄を毎スキャンINSERTしない
-- （呼び出し側＝各run_*_scanが遷移検知時のみ呼ぶ設計）。将来「AUTO_BREAK 52点以上は有効か」
-- 等を翌営業日/3営業日後/5営業日後リターンで検証できるよう、発生時点の価格・指標をそのまま残す。
-- primary_status/action_statusは、サーバー側のスキャンがenrichWatchRow()（クライアント専用の
-- Single Source of Truth）の結果を持たないため、二重ロジックを避ける方針上、現時点ではNULLで
-- 記録する（2026-09-05ユーザー判断：事後補完APIは見送り）。
CREATE TABLE IF NOT EXISTS auto_signal_events (
    id              SERIAL PRIMARY KEY,
    user_id         TEXT NOT NULL,
    code            TEXT NOT NULL,
    market          TEXT NOT NULL DEFAULT 'JP',
    signal_type     TEXT NOT NULL,  -- MOMENTUM_DAY|BREAK|RS|SECTOR_LEADER|PULLBACK|REVERSAL|VOLUME|EARNINGS
    event_type      TEXT NOT NULL,  -- ENTER_CURRENT|EXIT_CURRENT|REENTER_CURRENT|EXPIRE
    detected_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    event_date      DATE NOT NULL,  -- JST基準の日付（将来の営業日ベースのリターン計算の起点）
    score           NUMERIC,
    current_price   NUMERIC,
    day_change_pct  NUMERIC,
    market_rs       NUMERIC,
    sector_rs       NUMERIC,
    turnover        NUMERIC,
    high_retention  NUMERIC,
    primary_status  TEXT,
    action_status   TEXT,
    metadata        JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_auto_signal_events_code ON auto_signal_events(user_id, code, signal_type);
CREATE INDEX IF NOT EXISTS idx_auto_signal_events_date ON auto_signal_events(user_id, event_date);

-- v3-9続き（2026-09-05・PHASE 3 EVENT/EARNINGS INTELLIGENCE）：経済指標・決算・中銀会合等の
-- イベント情報。ChatGPTで画像（経済カレンダー・決算スケジュール等）をJSON化してコピー貼り付けで
-- import する想定（有料AI APIは使わない）。画像由来の情報を確定情報として扱わないよう、
-- verification_status（IMAGE_DERIVED/UNVERIFIED/VERIFIED）を必ず持たせる。既存のnews関連
-- テーブル（news_feedback）とは役割が異なる（あちらは「不要」フィードバックのログのみで、
-- ニュース本体もイベント情報も保存していない）ため重複しない。
-- 同一イベントの重複importを避けるため(user_id, event_date, title)にUNIQUE制約を付ける
-- （既存のwatchlist等と違いupsertで「後から検証状態だけ更新」等を許容するため、
-- ON CONFLICT DO UPDATEで使う）。
CREATE TABLE IF NOT EXISTS market_events (
    id                    SERIAL PRIMARY KEY,
    user_id               TEXT NOT NULL,
    event_date            DATE NOT NULL,
    event_time            TEXT,     -- 自由書式（"21:30"等）。厳密なタイムゾーン計算はしない
    timezone              TEXT,
    title                 TEXT NOT NULL,
    country               TEXT,
    event_type            TEXT NOT NULL DEFAULT 'OTHER',  -- ECONOMIC|CENTRAL_BANK|EARNINGS|INDEX_REBALANCE|POLITICAL|GEOPOLITICAL|PRODUCT_EVENT|OTHER
    importance            TEXT,     -- HIGH|MEDIUM|LOW等、自由記述も許容
    affected_markets      JSONB,
    affected_sectors      JSONB,
    affected_stocks       JSONB,    -- ["7203","9984"]等、銘柄分析画面での紐付けに使う
    impact_channels       JSONB,
    source                TEXT,
    source_type           TEXT,     -- IMAGE|TEXT|MANUAL等
    verification_status   TEXT NOT NULL DEFAULT 'UNVERIFIED',  -- IMAGE_DERIVED|UNVERIFIED|VERIFIED
    notes                 TEXT,
    raw_payload           JSONB,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, event_date, title)
);
CREATE INDEX IF NOT EXISTS idx_market_events_user_date ON market_events(user_id, event_date);

-- v3-9続き（2026-09-05・PHASE 4 NEWS/CATALYST INTELLIGENCE）：ニュース本体（Google News RSS/
-- TDnet）は引き続きserver.pyが都度取得するだけで保存しない。このテーブルはそれとは別に、
-- ChatGPT等で構造化された「カタログ情報」（マクロ・セクター・銘柄材料や指数採用・資金フロー等）
-- を保存する専用テーブル。既存news_feedback（「不要」フィードバックのログのみ）とは役割が異なり
-- 重複しない。market_eventsと同じくJSON貼り付けimport・upsert・verification_status必須の設計を
-- 踏襲する。catalyst_date＝このカタリストが報じられた日（freshness＝TODAY/3D/7D/OLDの基準）、
-- event_date＝指数採用・決算等、効力が発生する日（あれば、この日まで重要性を維持する。任意）。
CREATE TABLE IF NOT EXISTS news_catalysts (
    id                    SERIAL PRIMARY KEY,
    user_id               TEXT NOT NULL,
    catalyst_date         DATE NOT NULL,
    event_date            DATE,
    title                 TEXT NOT NULL,
    category              TEXT NOT NULL DEFAULT 'OTHER',  -- MACRO|SECTOR_CATALYST|STOCK_CATALYST|INDEX_REBALANCE|FUND_FLOW|EARNINGS|CAPITAL_POLICY|REGULATION_POLICY|GEOPOLITICAL|PRODUCT_CATALYST|OTHER
    importance            TEXT,
    summary               TEXT,
    affected_markets      JSONB,
    affected_sectors      JSONB,
    affected_stocks       JSONB,
    source                TEXT,
    source_type           TEXT,     -- IMAGE|TEXT|MANUAL等
    verification_status   TEXT NOT NULL DEFAULT 'UNVERIFIED',  -- IMAGE_DERIVED|UNVERIFIED|VERIFIED
    notes                 TEXT,
    raw_payload           JSONB,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, catalyst_date, title)
);
CREATE INDEX IF NOT EXISTS idx_news_catalysts_user_date ON news_catalysts(user_id, catalyst_date);

-- v3-9続き（2026-09-05・PHASE 5 EXPERT INTELLIGENCE）：YouTube/インタビュー/セミナー/記事/SNS
-- での有識者見解を「見通し（outlook）／根拠（thesis）／確認条件（confirmations）／無効化条件
-- （invalidation_conditions）」に分けて保存する。ユーザーがChatGPTで要約→JSON化して貼り付ける
-- 想定（有料AI APIは使わない）。売買シグナルへ直接変換せず、Primary/Action Statusにも
-- AUTOロジックにも一切干渉しない、あくまで分析画面・ポジション画面の参考情報。
-- 重複判定キーは(expert_name, source_title, published_at)（ユーザー指定）。source_titleは
-- 省略可のため、無指定行同士はPostgresの仕様上「別物」として扱われる（NULLは重複判定されない）。
-- confidenceは有識者本人の自信度ではなく、Trade Cockpit側で見た「情報の明確さ・条件の具体性」
-- として扱う（ユーザー指定の考え方）。
CREATE TABLE IF NOT EXISTS expert_views (
    id                      SERIAL PRIMARY KEY,
    user_id                 TEXT NOT NULL,
    expert_name             TEXT NOT NULL,
    source_title            TEXT,
    source_url              TEXT,
    source_type             TEXT NOT NULL DEFAULT 'OTHER',  -- YOUTUBE|INTERVIEW|SEMINAR|ARTICLE|SNS|OTHER
    published_at            DATE NOT NULL,
    captured_at             DATE,
    topic                   TEXT,
    time_horizon            TEXT,     -- INTRADAY|SHORT_TERM|SWING|MEDIUM_TERM|LONG_TERM（自由記述も許容）
    market                  TEXT,     -- JP|US等。stocks/sectorが空の場合、この市場全体への言及として扱う
    sector                  TEXT,
    stocks                  JSONB,    -- ["7203","9984"]等
    outlook                 TEXT,
    confidence              TEXT,     -- LOW|MEDIUM|HIGH（本人の自信度ではなく情報の明確さ・条件の具体性）
    risk_window_start       DATE,
    risk_window_end         DATE,
    thesis                  TEXT,
    confirmations           JSONB,
    invalidation_conditions JSONB,
    key_points              JSONB,
    source_summary          TEXT,
    verification_status     TEXT NOT NULL DEFAULT 'UNVERIFIED',  -- USER_PROVIDED_TRANSCRIPT|TRANSCRIPT_DERIVED|SUMMARY_ONLY|VERIFIED|UNVERIFIED
    effective_from          DATE,
    effective_until         DATE,
    raw_payload             JSONB,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, expert_name, source_title, published_at)
);
CREATE INDEX IF NOT EXISTS idx_expert_views_user_date ON expert_views(user_id, published_at);

-- v3-9続き（2026-09-05・PHASE 6 DAILY CHATGPT JSON IMPORT）：既存chatgpt_imports（2026-09-02
-- 新設、投資ログ用のdate/market/watchlist/decisions/rule_updates形式＝kind='TRADING_LOG'）とは
-- 別の用途で、日々のChatGPT⇄Trade Cockpit相談の要約（date/summary/trading_observations/
-- app_changes/bugs_or_risks/new_rules_or_preferences/updates/claude_code_instruction形式）を
-- 保存する。ユーザー指定により「既存テーブルを確認し、流用可能なら新テーブルを作らず拡張」
-- した結果、新テーブルは作らずkind列で種別を分け、この形式用の列だけ追加する。
-- payload_hashのUNIQUE制約（既存）をそのまま重複防止に使う＝同じ日でも内容が違えば
-- 別レコードとして保存できる（ハッシュが変わるため）。
-- updates=[]ならapply_status='NO_UPDATES'で確定し、investment_rules等には一切書き込まない
-- （Import と Apply を明確に分離する設計）。
ALTER TABLE chatgpt_imports ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'TRADING_LOG';
ALTER TABLE chatgpt_imports ADD COLUMN IF NOT EXISTS summary JSONB;
ALTER TABLE chatgpt_imports ADD COLUMN IF NOT EXISTS trading_observations JSONB;
ALTER TABLE chatgpt_imports ADD COLUMN IF NOT EXISTS app_changes JSONB;
ALTER TABLE chatgpt_imports ADD COLUMN IF NOT EXISTS bugs_or_risks JSONB;
ALTER TABLE chatgpt_imports ADD COLUMN IF NOT EXISTS new_rules_or_preferences JSONB;
ALTER TABLE chatgpt_imports ADD COLUMN IF NOT EXISTS updates JSONB;
ALTER TABLE chatgpt_imports ADD COLUMN IF NOT EXISTS claude_code_instruction TEXT;
-- apply_status候補：IMPORTED|NO_UPDATES|PREVIEWED|APPLIED|PARTIAL|FAILED
ALTER TABLE chatgpt_imports ADD COLUMN IF NOT EXISTS apply_status TEXT;
ALTER TABLE chatgpt_imports ADD COLUMN IF NOT EXISTS apply_result JSONB;
ALTER TABLE chatgpt_imports ADD COLUMN IF NOT EXISTS applied_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_chatgpt_imports_kind ON chatgpt_imports(user_id, kind, imported_at DESC);

-- 2026-09-07新規（監視銘柄→ポジション連携・売買損益管理）：既存portfolio（保有株、
-- 2026-09-03新設、既にuser_idスコープ済み）へ「買い増しの来歴」を追加するだけの列。
-- quantity/average_priceは引き続き「現在の合算値」のSSoTとして使い、entriesは監査用の
-- 追記専用ログ（買い増しのたびに1件追加、既存の値は書き換えない）。
ALTER TABLE portfolio ADD COLUMN IF NOT EXISTS entries JSONB NOT NULL DEFAULT '[]'::jsonb;

-- 売却確定時に1行だけ追加する取引履歴。既存journal（ユーザーが手動で振り返りを書く売買記録
-- タブ）とは役割が異なるため新設する：journalは自由記述の主観的な記録、trade_historyは
-- 「取得単価×売却単価×枚数」から機械的に確定する実現損益の恒久ログ（削除しない）。
-- user_idスコープのため他ユーザーへは一切共有されない（監視銘柄=watchlistも実は既に
-- user_idスコープ済みで現状共有されていない、PHASE8監査で確認済み）。
CREATE TABLE IF NOT EXISTS trade_history (
    id            SERIAL PRIMARY KEY,
    user_id       TEXT NOT NULL,
    code          TEXT NOT NULL,
    name          TEXT,
    market        TEXT NOT NULL DEFAULT 'JP',
    entry_price   NUMERIC NOT NULL,
    exit_price    NUMERIC NOT NULL,
    shares        NUMERIC NOT NULL,
    pnl           NUMERIC NOT NULL,
    closed_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_trade_history_user ON trade_history(user_id, closed_at DESC);

-- 通算実現損益の初期値（ユーザーが最初に手入力する既存の通算損益）。既存investment_profile
-- （1ユーザー1行、2026-09-02新設・未使用のまま残っていた）へ列を1つ足すだけで済ませる
-- （新規テーブルを増やさない）。表示時は毎回 initial_realized_pnl + trade_history.pnl合計
-- を再計算する（保存値をキャッシュせず、データ破損に強い構造にする＝ユーザー指示）。
ALTER TABLE investment_profile ADD COLUMN IF NOT EXISTS initial_realized_pnl NUMERIC NOT NULL DEFAULT 0;

-- 2026-09-07新規（通算実現損益を税引後ベースへ変更）：既存pnl列（税引前・gross）は意味を
-- 変えずそのまま残し（後方互換）、税引前/税額/税引後を明示的な列として追加する。
-- 既存レコード（本番では2026-09-07時点で0件）はgross_pnl/tax/net_pnlがNULLのままになるが、
-- get_investment_totals側でCOALESCE(net_pnl, pnl)により「税引後値が無い古い行はpnl
-- （税引前のまま）を暫定的にnetとして扱う」形で読み込み時に吸収し、過去データを書き換える
-- migrationは行わない（生データを勝手に改変しない、というユーザー方針に合わせた選択）。
ALTER TABLE trade_history ADD COLUMN IF NOT EXISTS gross_pnl NUMERIC;
ALTER TABLE trade_history ADD COLUMN IF NOT EXISTS tax NUMERIC;
ALTER TABLE trade_history ADD COLUMN IF NOT EXISTS net_pnl NUMERIC;

-- 2026-09-08新規（ニュース・材料連携の改善）：news_catalystsに好材料/悪材料/中立の方向性
-- （sentiment）を追加する。既存のcategory（分類）・importance（重要度）とは別軸で、
-- 「positive|negative|neutral」のいずれか。既存importで未指定の行はNULL（判定不能）のまま
-- 残す＝無理にpositive/negativeへ寄せない（ユーザー指定：確信が無ければneutral扱いにする
-- 判定ロジックはフロント/import時のヘルパー側が担う。ここではNULL可の列を追加するのみ）。
ALTER TABLE news_catalysts ADD COLUMN IF NOT EXISTS sentiment TEXT;
"""

# 2026-09-09新規（ルール学習システム）：投資判断ログ系の他テーブルより後に作成する必要は
# 無いが、既存の大きな_SCHEMA_SQL文字列を直接編集して差分を分かりにくくしないよう、
# 独立したブロックとして追加する（_MIGRATE_CHATGPT_IMPORT_SQL等と同じ方針）。
_SCHEMA_TRADE_RULES_SQL = """
CREATE TABLE IF NOT EXISTS trade_rules (
    id                 SERIAL PRIMARY KEY,
    user_id            TEXT NOT NULL,
    rule_key           TEXT NOT NULL,
    title              TEXT,
    rule_text          TEXT NOT NULL,
    category           TEXT,
    scope              TEXT,
    rule_type          TEXT NOT NULL DEFAULT 'TESTING',  -- PERMANENT|TESTING|TEMPORARY
    status             TEXT NOT NULL DEFAULT 'TESTING',  -- TESTING|ACTIVE|REVISED|RETIRED|EXPIRED
    confidence         TEXT NOT NULL DEFAULT 'LOW',       -- LOW|MEDIUM|HIGH
    evidence_count     INTEGER NOT NULL DEFAULT 0,
    success_count      INTEGER NOT NULL DEFAULT 0,
    failure_count      INTEGER NOT NULL DEFAULT 0,
    neutral_count      INTEGER NOT NULL DEFAULT 0,
    first_seen_date    TEXT,
    last_seen_date     TEXT,
    last_verified_date TEXT,
    last_failed_date   TEXT,
    action_text        TEXT,
    conditions_json    JSONB,
    exceptions_json    JSONB,
    source_json        JSONB,
    notes              TEXT,
    parent_rule_id     INTEGER REFERENCES trade_rules(id) ON DELETE SET NULL,
    revised_from       INTEGER,
    expires_date       TEXT,
    created_from       TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, rule_key)
);
CREATE INDEX IF NOT EXISTS idx_trade_rules_user_status ON trade_rules(user_id, status);
CREATE INDEX IF NOT EXISTS idx_trade_rules_user_category ON trade_rules(user_id, category);

CREATE TABLE IF NOT EXISTS trade_rule_history (
    id             SERIAL PRIMARY KEY,
    user_id        TEXT NOT NULL,
    rule_id        INTEGER NOT NULL REFERENCES trade_rules(id) ON DELETE CASCADE,
    event_type     TEXT NOT NULL,  -- CREATED|MENTION|EVALUATION|STATUS_CHANGE|CONFIDENCE_CHANGE|TEXT_EDIT|EXPIRED
    eval_result    TEXT,           -- SUPPORTED|FAILED|NEUTRAL|NOT_APPLICABLE（EVALUATIONのみ）
    eval_date      TEXT,
    old_status     TEXT,
    new_status     TEXT,
    old_confidence TEXT,
    new_confidence TEXT,
    old_text       TEXT,
    new_text       TEXT,
    reason         TEXT,
    source         TEXT,  -- auto|manual|chatgpt_import|legacy_rule_update|instruction_seed 等
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_trade_rule_history_rule ON trade_rule_history(rule_id, created_at DESC);
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
        conn.execute(_SCHEMA_TRADE_RULES_SQL)
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


# ============================================================
# ---- trade_rules（ルール学習システム。2026-09-09新規） ----
# 目的：rule_updates（ChatGPT取り込みJSONの一部）を「その日限りのメモ」として単純追記する
# だけだった従来のinvestment_rules運用を、「蓄積→照合→検証→昇格/修正/弱体化→次回分析へ反映」
# という循環に変える。既存のinvestment_rules・save_chatgpt_import・rule_updatesスキーマ
# （文字列配列 or {rule,status}配列）は一切変更せず、並行して動く新テーブルとして追加する
# （指示書「既存のChatGPT取り込み、daily_log、rule_updates、分析ロジックを壊さない」に対応）。
# ============================================================

# ---- ルール文の正規化・重複判定（指示書3番） ----
_RULE_ZEN_HAN_TABLE = str.maketrans(
    "０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ",
    "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz",
)
_RULE_DATE_RE1 = re.compile(r"\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?")
_RULE_DATE_RE2 = re.compile(r"\d{1,2}[/月]\d{1,2}日?")
_RULE_PUNCT_RE = re.compile(r"[、。・「」『』【】\[\]（）()｢｣!！?？,.:：;；\-—―~〜\"'’“”]")
_RULE_SPACE_RE = re.compile(r"[\s　]+")


def _normalize_rule_key(text):
    """ルール文の重複判定用キー。記号除去・全角半角吸収・空白除去・小文字化・日付表現の除去だけの
    軽量な正規化（形態素解析・銘柄マスタ照合等は使わない）。完全一致の重複防止が目的で、意味的な
    近さの判定は`_rule_similarity`（類似ルール候補の提示専用、自動統合はしない）に委ねる。"""
    if not text:
        return ""
    t = str(text).translate(_RULE_ZEN_HAN_TABLE).lower()
    t = _RULE_DATE_RE1.sub("", t)
    t = _RULE_DATE_RE2.sub("", t)
    t = _RULE_PUNCT_RE.sub("", t)
    t = _RULE_SPACE_RE.sub("", t)
    return t


def _rule_bigrams(s):
    return set(s[i:i + 2] for i in range(len(s) - 1)) if len(s) >= 2 else ({s} if s else set())


def _rule_similarity(key_a, key_b):
    """文字バイグラムのJaccard係数（0〜1）による簡易類似度。形態素解析なしでの近似実装。
    「200Aが後場に安値更新する日は半導体買いを慎重にする」と「日経半導体ETFが前場高値後に
    後場安値を更新した日は新規半導体買いを抑える」のような表記違いの同義ルールを拾うための
    緩い指標——自動統合はせず、あくまで「類似ルール候補」として提示するだけに使う（指示書3番）。"""
    ba, bb = _rule_bigrams(key_a), _rule_bigrams(key_b)
    if not ba or not bb:
        return 0.0
    inter = len(ba & bb)
    union = len(ba | bb)
    return inter / union if union else 0.0


_RULE_SIMILARITY_THRESHOLD = 0.55  # これ以上で「類似ルール候補」として提示する下限（定数化）

# ---- カテゴリ・スコープの簡易推定（キーワード方式、既存のIR_KEYWORDS等と同じ考え方） ----
_RULE_CATEGORY_KEYWORDS = [
    ("earnings", ["決算", "上方修正", "下方修正"]),
    ("semiconductor", ["半導体", "200A", "SOX", "ソックス"]),
    ("event", ["FOMC", "日銀会合", "雇用統計", "イベント", "決定会合"]),
    ("risk", ["損切り", "ロスカット", "撤退", "ストップ"]),
    ("exit", ["利確", "持ち越し", "手仕舞", "決済"]),
    ("entry", ["新規エントリー", "新規買い", "買い増し"]),
    ("position", ["ポジション", "建玉", "持ち越し"]),
    ("momentum", ["急騰", "モメンタム", "出来高急増"]),
    ("swing", ["スイング"]),
    ("daytrade", ["デイトレ", "日計り", "寄り天"]),
    ("sector", ["セクター", "業種", "ヒートマップ"]),
    ("market", ["地合い", "日経", "TOPIX", "プライム", "値下がり銘柄比率", "指数"]),
]


def _guess_rule_category(text):
    t = text or ""
    for cat, kws in _RULE_CATEGORY_KEYWORDS:
        if any(k in t for k in kws):
            return cat
    return "market"


def _guess_rule_scope(text):
    t = text or ""
    if re.search(r"[0-9]{4}[A-Z]?(?:[^0-9A-Za-z]|$)", t):  # 4桁銘柄コードらしき文字列
        return "stock"
    if any(k in t for k in ["セクター", "業種", "半導体", "銀行", "内需", "輸出"]):
        return "sector"
    if any(k in t for k in ["地合い", "相場全体", "市場全体", "指数"]):
        return "market_condition"
    return "global"


# ---- 昇格・弱体化の閾値（指示書7・8番。ハードコードせず定数化） ----
RULE_PROMOTION_MIN_EVIDENCE_MEDIUM = 2
RULE_PROMOTION_MIN_SUCCESS_MEDIUM = 2
RULE_PROMOTION_MIN_EVIDENCE_ACTIVE = 4
RULE_PROMOTION_MIN_SUCCESS_RATE_ACTIVE = 0.70
RULE_PROMOTION_MIN_EVIDENCE_HIGH = 6
RULE_PROMOTION_MIN_SUCCESS_RATE_HIGH = 0.75
# evidence_countはChatGPT取込での「再言及」だけでも増える（sync_rule_updates_to_trade_rules・
# upsert_trade_rule_from_text参照）ため、evidence_countだけを条件にするとほぼ未検証（評価0〜1回）
# のルールが「何度も話題に出ただけ」でACTIVE/HIGHまで昇格してしまう抜け道になる。success_rateは
# 実際の評価件数（total_eval=success+failure+neutral）だけから計算されるため、評価が少ないと
# 少数の結果だけで100%になりやすい点も合わせ、実際に検証された回数の下限を別途設ける。
RULE_PROMOTION_MIN_TOTAL_EVAL_ACTIVE = 2   # ACTIVE昇格に必要な実評価（SUPPORTED/FAILED/NEUTRAL）回数の下限
RULE_PROMOTION_MIN_TOTAL_EVAL_HIGH = 3     # HIGH昇格に必要な実評価回数の下限
RULE_DEMOTION_MIN_EVAL_FOR_CHECK = 3       # 失敗率を評価するのに必要な最低評価回数
RULE_DEMOTION_FAILURE_RATE_THRESHOLD = 0.4  # これ以上の失敗率で弱体化候補にする


def _evaluate_rule_promotion(row):
    """ルール1件の現在値（evidence_count/success_count/failure_count/neutral_count/status/
    confidence/rule_type）から、新しいstatus・confidenceと変更理由を返す（指示書7・8番）。
    TEMPORARYルールおよびRETIRED/EXPIRED済みは対象外（指示書15番：一時ルールは通常の昇格対象
    から除外）。DBへの書き込みはこの関数では行わない（呼び出し側の責務）。"""
    if row.get("rule_type") == "TEMPORARY" or row.get("status") in ("RETIRED", "EXPIRED"):
        return row.get("status"), row.get("confidence"), None
    evidence = row.get("evidence_count") or 0
    success = row.get("success_count") or 0
    failure = row.get("failure_count") or 0
    neutral = row.get("neutral_count") or 0
    total_eval = success + failure + neutral
    success_rate = (success / total_eval) if total_eval > 0 else 0.0
    failure_rate = (failure / total_eval) if total_eval > 0 else 0.0
    old_status, old_conf = row.get("status"), row.get("confidence")
    new_status, new_conf, reason = old_status, old_conf, None

    # 弱体化を先に判定（FAILEDが増えている場合は消さずに信頼度・ステータスを落とすだけ、指示書8番）
    if total_eval >= RULE_DEMOTION_MIN_EVAL_FOR_CHECK and failure_rate >= RULE_DEMOTION_FAILURE_RATE_THRESHOLD:
        if old_conf == "HIGH":
            new_conf = "MEDIUM"
            reason = f"失敗率{failure_rate:.0%}のためHIGH→MEDIUMへ弱体化"
        elif old_status == "ACTIVE":
            new_status = "REVISED"
            reason = f"失敗率{failure_rate:.0%}のためREVISED候補へ（修正版ルールの作成を検討してください）"
        return new_status, new_conf, reason

    # 昇格判定（evidence_count・success_count/success_rateの組み合わせ、指示書7番の目安）。
    # ACTIVE/HIGHへの昇格はtotal_eval（実評価回数）の下限も満たす必要がある（上記の抜け道対策）。
    if (evidence >= RULE_PROMOTION_MIN_EVIDENCE_HIGH and success_rate >= RULE_PROMOTION_MIN_SUCCESS_RATE_HIGH
            and total_eval >= RULE_PROMOTION_MIN_TOTAL_EVAL_HIGH):
        if old_conf != "HIGH":
            new_conf = "HIGH"
            reason = f"evidence{evidence}件・実評価{total_eval}件・成功率{success_rate:.0%}のためHIGHへ昇格"
        if old_status == "TESTING":
            new_status = "ACTIVE"
    elif (evidence >= RULE_PROMOTION_MIN_EVIDENCE_ACTIVE and success_rate >= RULE_PROMOTION_MIN_SUCCESS_RATE_ACTIVE
            and total_eval >= RULE_PROMOTION_MIN_TOTAL_EVAL_ACTIVE):
        if old_status == "TESTING":
            new_status = "ACTIVE"
            reason = f"evidence{evidence}件・実評価{total_eval}件・成功率{success_rate:.0%}のためACTIVEへ昇格"
        if old_conf == "LOW":
            new_conf = "MEDIUM"
    elif evidence >= RULE_PROMOTION_MIN_EVIDENCE_MEDIUM and success >= RULE_PROMOTION_MIN_SUCCESS_MEDIUM:
        if old_conf == "LOW":
            new_conf = "MEDIUM"
            reason = f"evidence{evidence}件・成功{success}件のためMEDIUMへ"
    return new_status, new_conf, reason


def _trade_rule_row_to_json(row):
    d = _row_to_json(row)
    s = d.get("success_count") or 0
    f = d.get("failure_count") or 0
    n = d.get("neutral_count") or 0
    total = s + f + n
    d["success_rate"] = round(s / total, 3) if total > 0 else None
    d["total_evaluations"] = total
    return d


def upsert_trade_rule_from_text(database_url, user_id, rule_text, source_info=None, category=None,
                                  scope=None, rule_type="TESTING", action_text=None,
                                  initial_status="TESTING", initial_confidence="LOW",
                                  expires_date=None, created_from="chatgpt_import", seen_date=None,
                                  exceptions=None, extra_fields=None):
    """ルール文1件をtrade_rulesへ照合・反映する中核関数（指示書4番）。rule_key（正規化済み
    テキスト）の完全一致で既存ルールを検索し、見つかれば「支持された」ものとしてevidence_count
    を加算・source_json（最大20件）に取り込み元を追記・昇格判定を再計算する。見つからなければ
    status=TESTING・confidence=LOWの新規ルールとして作成する（既定値。呼び出し側で上書き可）。
    戻り値: {"action":"matched"|"created", "id":..., "evidenceCount":..., "statusChanged":bool,
    "confidenceChanged":bool, "newStatus":..., "newConfidence":...} または pool未設定時None。"""
    pool = _get_pool(database_url)
    if pool is None:
        return None
    rule_text = (rule_text or "").strip()
    if not rule_text:
        return None
    rule_key = _normalize_rule_key(rule_text)
    if not rule_key:
        return None
    seen_date = seen_date or datetime.date.today().isoformat()
    category = category or _guess_rule_category(rule_text)
    scope = scope or _guess_rule_scope(rule_text)
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM trade_rules WHERE user_id=%s AND rule_key=%s", [user_id, rule_key])
            row = cur.fetchone()
            if row:
                sources = row.get("source_json") or []
                if source_info:
                    sources = (sources + [source_info])[-20:]
                new_evidence = (row["evidence_count"] or 0) + 1
                merged = {**row, "evidence_count": new_evidence}
                new_status, new_conf, reason = _evaluate_rule_promotion(merged)
                status_changed = new_status != row["status"]
                conf_changed = new_conf != row["confidence"]
                set_clauses = ["evidence_count=%s", "last_seen_date=%s", "source_json=%s::jsonb", "updated_at=now()"]
                params = [new_evidence, seen_date, json.dumps(sources, ensure_ascii=False)]
                if status_changed:
                    set_clauses.append("status=%s"); params.append(new_status)
                if conf_changed:
                    set_clauses.append("confidence=%s"); params.append(new_conf)
                params += [row["id"], user_id]
                cur.execute(f"UPDATE trade_rules SET {', '.join(set_clauses)} WHERE id=%s AND user_id=%s", params)
                cur.execute(
                    "INSERT INTO trade_rule_history (user_id,rule_id,event_type,reason,source) "
                    "VALUES (%s,%s,'MENTION',%s,%s)",
                    [user_id, row["id"], f"再言及によりevidence_count={new_evidence}", created_from])
                if status_changed:
                    cur.execute(
                        "INSERT INTO trade_rule_history (user_id,rule_id,event_type,old_status,new_status,reason,source) "
                        "VALUES (%s,%s,'STATUS_CHANGE',%s,%s,%s,'auto')",
                        [user_id, row["id"], row["status"], new_status, reason])
                if conf_changed:
                    cur.execute(
                        "INSERT INTO trade_rule_history (user_id,rule_id,event_type,old_confidence,new_confidence,reason,source) "
                        "VALUES (%s,%s,'CONFIDENCE_CHANGE',%s,%s,%s,'auto')",
                        [user_id, row["id"], row["confidence"], new_conf, reason])
                conn.commit()
                return {"action": "matched", "id": row["id"], "evidenceCount": new_evidence,
                        "statusChanged": status_changed, "confidenceChanged": conf_changed,
                        "newStatus": new_status, "newConfidence": new_conf}
            else:
                extra = extra_fields or {}
                sources = [source_info] if source_info else []
                cur.execute(
                    "INSERT INTO trade_rules (user_id, rule_key, title, rule_text, category, scope, rule_type, "
                    "status, confidence, evidence_count, first_seen_date, last_seen_date, action_text, "
                    "exceptions_json, source_json, created_from, parent_rule_id, revised_from, expires_date) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,1,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s,%s) RETURNING id",
                    [user_id, rule_key, rule_text[:80], rule_text, category, scope, rule_type,
                     initial_status, initial_confidence, seen_date, seen_date, action_text,
                     json.dumps(exceptions or [], ensure_ascii=False), json.dumps(sources, ensure_ascii=False),
                     created_from, extra.get("parent_rule_id"), extra.get("revised_from"), expires_date],
                )
                new_id = cur.fetchone()["id"]
                cur.execute(
                    "INSERT INTO trade_rule_history (user_id,rule_id,event_type,new_status,new_confidence,reason,source) "
                    "VALUES (%s,%s,'CREATED',%s,%s,%s,%s)",
                    [user_id, new_id, initial_status, initial_confidence, f"created_from={created_from}", created_from])
                conn.commit()
                return {"action": "created", "id": new_id, "evidenceCount": 1,
                        "statusChanged": False, "confidenceChanged": False,
                        "newStatus": initial_status, "newConfidence": initial_confidence}


def record_rule_evaluation(database_url, user_id, rule_id, eval_result, eval_date=None, note=None, source="manual"):
    """ルール1件を特定の日で評価する（指示書6番）。eval_result: SUPPORTED/FAILED/NEUTRAL/
    NOT_APPLICABLE。呼ぶたびに該当カウンタ（success/failure/neutral_count、NOT_APPLICABLE以外
    はevidence_countも）を加算し、_evaluate_rule_promotionで昇格/弱体化を再計算、
    trade_rule_historyへEVALUATIONイベントとして記録する。"""
    pool = _get_pool(database_url)
    if pool is None:
        return None
    if eval_result not in ("SUPPORTED", "FAILED", "NEUTRAL", "NOT_APPLICABLE"):
        return None
    eval_date = eval_date or datetime.date.today().isoformat()
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM trade_rules WHERE id=%s AND user_id=%s", [rule_id, user_id])
            row = cur.fetchone()
            if not row:
                return None
            col = {"SUPPORTED": "success_count", "FAILED": "failure_count", "NEUTRAL": "neutral_count"}.get(eval_result)
            updates = {}
            if col:
                updates[col] = (row[col] or 0) + 1
            if eval_result != "NOT_APPLICABLE":
                updates["evidence_count"] = (row["evidence_count"] or 0) + 1
            updates["last_verified_date"] = eval_date
            if eval_result == "FAILED":
                updates["last_failed_date"] = eval_date
            merged = {**row, **updates}
            new_status, new_conf, reason = _evaluate_rule_promotion(merged)
            set_clauses = [f"{k}=%s" for k in updates]
            params = list(updates.values())
            if new_status != row["status"]:
                set_clauses.append("status=%s"); params.append(new_status)
            if new_conf != row["confidence"]:
                set_clauses.append("confidence=%s"); params.append(new_conf)
            set_clauses.append("updated_at=now()")
            params += [rule_id, user_id]
            cur.execute(f"UPDATE trade_rules SET {', '.join(set_clauses)} WHERE id=%s AND user_id=%s", params)
            cur.execute(
                "INSERT INTO trade_rule_history (user_id,rule_id,event_type,eval_result,eval_date,"
                "old_status,new_status,old_confidence,new_confidence,reason,source) "
                "VALUES (%s,%s,'EVALUATION',%s,%s,%s,%s,%s,%s,%s,%s)",
                [user_id, rule_id, eval_result, eval_date, row["status"], new_status,
                 row["confidence"], new_conf, note or reason, source])
        conn.commit()
    return {"id": rule_id, "newStatus": new_status, "newConfidence": new_conf, "promotionReason": reason}


def update_trade_rule(database_url, user_id, rule_id, fields, reason=None, source="manual"):
    """手動操作（指示書16番）：status/confidence/rule_text/action_text/exceptions_json/
    conditions_json/category/scope/rule_type/expires_date/notesのいずれかを更新し、変更前後を
    trade_rule_historyへ記録する（指示書17番）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return None
    allowed = ["status", "confidence", "rule_text", "title", "action_text", "exceptions_json",
               "conditions_json", "category", "scope", "rule_type", "expires_date", "notes"]
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return None
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM trade_rules WHERE id=%s AND user_id=%s", [rule_id, user_id])
            row = cur.fetchone()
            if not row:
                return None
            set_clauses, params = [], []
            for k, v in fields.items():
                if k in ("exceptions_json", "conditions_json") and v is not None:
                    set_clauses.append(f"{k}=%s::jsonb"); params.append(json.dumps(v, ensure_ascii=False))
                else:
                    set_clauses.append(f"{k}=%s"); params.append(v)
            set_clauses.append("updated_at=now()")
            params += [rule_id, user_id]
            cur.execute(f"UPDATE trade_rules SET {', '.join(set_clauses)} WHERE id=%s AND user_id=%s", params)
            if "status" in fields and fields["status"] != row["status"]:
                cur.execute(
                    "INSERT INTO trade_rule_history (user_id,rule_id,event_type,old_status,new_status,reason,source) "
                    "VALUES (%s,%s,'STATUS_CHANGE',%s,%s,%s,%s)",
                    [user_id, rule_id, row["status"], fields["status"], reason, source])
            if "confidence" in fields and fields["confidence"] != row["confidence"]:
                cur.execute(
                    "INSERT INTO trade_rule_history (user_id,rule_id,event_type,old_confidence,new_confidence,reason,source) "
                    "VALUES (%s,%s,'CONFIDENCE_CHANGE',%s,%s,%s,%s)",
                    [user_id, rule_id, row["confidence"], fields["confidence"], reason, source])
            if "rule_text" in fields and fields["rule_text"] != row["rule_text"]:
                cur.execute(
                    "INSERT INTO trade_rule_history (user_id,rule_id,event_type,old_text,new_text,reason,source) "
                    "VALUES (%s,%s,'TEXT_EDIT',%s,%s,%s,%s)",
                    [user_id, rule_id, row["rule_text"], fields["rule_text"], reason, source])
        conn.commit()
    return True


def create_revised_trade_rule(database_url, user_id, parent_rule_id, new_rule_text, reason=None,
                                category=None, action_text=None):
    """既存ルールをREVISEDへ落とし、修正版を新規ルール（parent_rule_id/revised_from付き）として
    派生させる（指示書8番）。元ルールは削除しない。"""
    pool = _get_pool(database_url)
    if pool is None:
        return None
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM trade_rules WHERE id=%s AND user_id=%s", [parent_rule_id, user_id])
            parent = cur.fetchone()
            if not parent:
                return None
            cur.execute("UPDATE trade_rules SET status='REVISED', updated_at=now() WHERE id=%s AND user_id=%s",
                        [parent_rule_id, user_id])
            cur.execute(
                "INSERT INTO trade_rule_history (user_id,rule_id,event_type,old_status,new_status,reason,source) "
                "VALUES (%s,%s,'STATUS_CHANGE',%s,'REVISED',%s,'manual')",
                [user_id, parent_rule_id, parent["status"], reason])
        conn.commit()
    return upsert_trade_rule_from_text(
        database_url, user_id, new_rule_text,
        category=category or parent["category"], rule_type=parent["rule_type"],
        action_text=action_text or parent["action_text"],
        initial_status="TESTING", initial_confidence="LOW",
        created_from="revised_from_" + str(parent_rule_id),
        extra_fields={"parent_rule_id": parent_rule_id, "revised_from": parent_rule_id},
    )


def expire_temporary_trade_rules(database_url, user_id):
    """TEMPORARYルールでexpires_dateを過ぎたものをEXPIREDへ自動遷移する（指示書15番）。
    list_trade_rulesから毎回呼ばれる軽量チェック（対象0件ならクエリ1本のみ）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return 0
    today = datetime.date.today().isoformat()
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, status FROM trade_rules WHERE user_id=%s AND rule_type='TEMPORARY' "
                "AND status NOT IN ('EXPIRED','RETIRED') AND expires_date IS NOT NULL AND expires_date < %s",
                [user_id, today])
            rows = cur.fetchall()
            for r in rows:
                cur.execute("UPDATE trade_rules SET status='EXPIRED', updated_at=now() WHERE id=%s", [r["id"]])
                cur.execute(
                    "INSERT INTO trade_rule_history (user_id,rule_id,event_type,old_status,new_status,reason,source) "
                    "VALUES (%s,%s,'EXPIRED',%s,'EXPIRED','期限切れ（expires_date超過）','auto')",
                    [user_id, r["id"], r["status"]])
        conn.commit()
    return len(rows)


def list_trade_rules(database_url, user_id, status=None, confidence=None, category=None, rule_type=None):
    pool = _get_pool(database_url)
    if pool is None:
        return []
    expire_temporary_trade_rules(database_url, user_id)
    where, params = ["user_id=%s"], [user_id]
    if status: where.append("status=%s"); params.append(status)
    if confidence: where.append("confidence=%s"); params.append(confidence)
    if category: where.append("category=%s"); params.append(category)
    if rule_type: where.append("rule_type=%s"); params.append(rule_type)
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"SELECT * FROM trade_rules WHERE {' AND '.join(where)} ORDER BY "
                f"CASE status WHEN 'ACTIVE' THEN 0 WHEN 'TESTING' THEN 1 WHEN 'REVISED' THEN 2 "
                f"WHEN 'RETIRED' THEN 3 ELSE 4 END, evidence_count DESC, updated_at DESC", params)
            return [_trade_rule_row_to_json(r) for r in cur.fetchall()]


def get_trade_rule(database_url, user_id, rule_id):
    """ルール1件を履歴付きで返す（指示書11番：ルール詳細画面用）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return None
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM trade_rules WHERE id=%s AND user_id=%s", [rule_id, user_id])
            row = cur.fetchone()
            if not row:
                return None
            cur.execute("SELECT * FROM trade_rule_history WHERE rule_id=%s AND user_id=%s ORDER BY created_at DESC",
                        [rule_id, user_id])
            history = [_row_to_json(h) for h in cur.fetchall()]
    result = _trade_rule_row_to_json(row)
    result["history"] = history
    return result


def find_similar_trade_rules(database_url, user_id, rule_text, exclude_id=None, limit=5):
    """完全一致（rule_key）ではないが意味的に近そうなルールを提示する（指示書3番、自動統合はしない）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return []
    key = _normalize_rule_key(rule_text)
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM trade_rules WHERE user_id=%s AND status NOT IN ('RETIRED','EXPIRED')", [user_id])
            rows = cur.fetchall()
    scored = []
    for r in rows:
        if exclude_id and r["id"] == exclude_id:
            continue
        if r["rule_key"] == key:
            continue
        sim = _rule_similarity(key, r["rule_key"])
        if sim >= _RULE_SIMILARITY_THRESHOLD:
            scored.append((sim, r))
    scored.sort(key=lambda x: -x[0])
    return [{"similarity": round(s, 3), **_trade_rule_row_to_json(r)} for s, r in scored[:limit]]


def trade_rules_debug_stats(database_url, user_id):
    """ルールタブのデバッグ折りたたみ表示用（指示書20番）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return {}
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT status, rule_type, COUNT(*) c FROM trade_rules WHERE user_id=%s GROUP BY status, rule_type",
                        [user_id])
            rows = cur.fetchall()
            cur.execute("SELECT id, rule_key FROM trade_rules WHERE user_id=%s AND status NOT IN ('RETIRED','EXPIRED')",
                        [user_id])
            all_rows = cur.fetchall()
            today = datetime.date.today().isoformat()
            cur.execute(
                "SELECT eval_result, COUNT(*) c FROM trade_rule_history WHERE user_id=%s AND event_type='EVALUATION' "
                "AND eval_date=%s GROUP BY eval_result", [user_id, today])
            today_evals = cur.fetchall()
    by_status, by_type = {}, {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + r["c"]
        by_type[r["rule_type"]] = by_type.get(r["rule_type"], 0) + r["c"]
    dup_pairs = 0
    for i in range(len(all_rows)):
        for j in range(i + 1, len(all_rows)):
            if _rule_similarity(all_rows[i]["rule_key"], all_rows[j]["rule_key"]) >= _RULE_SIMILARITY_THRESHOLD:
                dup_pairs += 1
    return {
        "byStatus": by_status, "byType": by_type, "totalRules": sum(by_status.values()),
        "todayEvaluations": {r["eval_result"]: r["c"] for r in today_evals},
        "duplicateCandidatePairs": dup_pairs,
    }


def relevant_trade_rules_for(database_url, user_id, categories=None, limit=8):
    """朝一分析・トレード分析・ポジション分析用（指示書12・13番）。ACTIVE＋関連度の高いTESTING
    ルールだけを返す（全ルールを毎回送らない）。categoriesが指定されればcategory一致または
    scope='global'のルールのみに絞る。"""
    pool = _get_pool(database_url)
    if pool is None:
        return []
    expire_temporary_trade_rules(database_url, user_id)
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            where = ["user_id=%s", "status IN ('ACTIVE','TESTING')", "rule_type != 'TEMPORARY'"]
            params = [user_id]
            if categories:
                where.append("(category = ANY(%s) OR scope='global')")
                params.append(list(categories))
            cur.execute(
                f"SELECT * FROM trade_rules WHERE {' AND '.join(where)} ORDER BY "
                f"CASE status WHEN 'ACTIVE' THEN 0 ELSE 1 END, "
                f"CASE confidence WHEN 'HIGH' THEN 0 WHEN 'MEDIUM' THEN 1 ELSE 2 END, evidence_count DESC "
                f"LIMIT %s", params + [limit])
            return [_trade_rule_row_to_json(r) for r in cur.fetchall()]


def sync_rule_updates_to_trade_rules(database_url, user_id, rule_updates, daily_log_id=None, date=None,
                                       market_condition=None, review_excerpt=None, decision_excerpt=None):
    """save_chatgpt_import()から呼ばれる（指示書4番）。rule_updates（文字列配列 or {rule,status}
    配列のどちらも既存互換のまま受け付ける）の各要素をtrade_rulesへ照合・反映する。
    戻り値: {"newRules":N,"existingSupported":N,"confidenceUp":N,"statusUp":N,"details":[...]}
    （指示書18番「本日のルール更新」表示用の集計もこの戻り値をそのまま使う）。"""
    if not rule_updates:
        return {"newRules": 0, "existingSupported": 0, "confidenceUp": 0, "statusUp": 0, "details": []}
    source_info = None
    if daily_log_id or date or market_condition or review_excerpt or decision_excerpt:
        source_info = {
            "date": date, "daily_log_id": daily_log_id, "market_condition": market_condition,
            "review_excerpt": (review_excerpt or "")[:200] or None,
            "decision_excerpt": (decision_excerpt or "")[:200] or None,
        }
    summary = {"newRules": 0, "existingSupported": 0, "confidenceUp": 0, "statusUp": 0, "details": []}
    for ru in rule_updates:
        text = ru if isinstance(ru, str) else (ru or {}).get("rule") or (ru or {}).get("text")
        if not text:
            continue
        result = upsert_trade_rule_from_text(
            database_url, user_id, text, source_info=source_info,
            initial_status="TESTING", initial_confidence="LOW",
            created_from="chatgpt_import", seen_date=date,
        )
        if not result:
            continue
        if result["action"] == "created":
            summary["newRules"] += 1
        else:
            summary["existingSupported"] += 1
            if result.get("confidenceChanged"):
                summary["confidenceUp"] += 1
            if result.get("statusChanged"):
                summary["statusUp"] += 1
        summary["details"].append({"rule": text[:60], "action": result["action"], "id": result["id"]})
    return summary


# ---- 既存データからの初期移行（指示書1・5番） ----
# 恒久ルール一覧のフォールバックseed。investment_rulesに同等の項目が既に無い場合のみ新規作成
# される（rule_keyのUNIQUE制約により重複は作られない）。
_PERMANENT_LONGTERM_RULE_SEEDS = [
    {"text": "決算をまたぐポジションは原則回避する", "category": "earnings",
     "action_text": "決算発表を跨ぐ保有は避ける、跨ぐ場合はポジションサイズを縮小する"},
    {"text": "スイングは-10%で損切りする", "category": "risk",
     "action_text": "SWING建玉の-10%絶対損切りルールを厳守する"},
    {"text": "デイトレードは逆指値を必ず入れる", "category": "risk",
     "action_text": "エントリー後は必ず逆指値注文を設定する"},
    {"text": "持ち越しは原則なし、その日のうちに手仕舞う", "category": "exit",
     "action_text": "デイトレ建玉はその日のうちに決済する"},
]
# 指示書5番で明示された新規ルール候補A〜C（Dは別途temporaryとして登録）。
_INSTRUCTION_SEED_RULES_ABC = [
    {"text": "日経・TOPIXが小幅変動でも東証プライムの値下がり銘柄比率が55%以上なら、体感地合いは弱いと評価する。",
     "category": "market", "confidence": "MEDIUM",
     "action_text": "新規エントリーのハードルを上げ、指数だけで強気判定しない"},
    {"text": "200Aが前場高値後に後場安値を更新する日は、個別材料が強くても半導体の新規買いを慎重にする。",
     "category": "semiconductor", "confidence": "MEDIUM",
     "action_text": "半導体の新規買いを抑制し、200Aの相対強度回復を待つ"},
    {"text": "指数寄与度の高い一部大型株だけで指数が支えられている場合、騰落数とセクターヒートマップを併用して判断する。",
     "category": "market", "confidence": "MEDIUM", "action_text": None},
]


def migrate_legacy_rules_to_trade_rules(database_url, user_id):
    """既存のdaily_log.raw_payload.rule_updates（全履歴）・investment_rules（既存の長期マイ
    ルール）をtrade_rulesへ移行し、指示書5番のA〜Dルール・既存の長期ルール（決算跨ぎ回避等）も
    合わせて登録する（指示書1・5番）。rule_keyのUNIQUE制約により、複数回実行しても重複登録され
    ない（初回以降は実質no-op、新しい過去データが増えていれば追加で拾う）。既存データは一切
    削除・上書きしない。戻り値: 移行/登録件数の内訳dict。"""
    pool = _get_pool(database_url)
    if pool is None:
        return {"migratedFromDailyLog": 0, "migratedFromInvestmentRules": 0, "seededExplicit": 0, "seededPermanent": 0}
    result = {"migratedFromDailyLog": 0, "migratedFromInvestmentRules": 0, "seededExplicit": 0, "seededPermanent": 0}

    # 2026-09-09修正：指示書5番のA〜D・既存の長期ルール（決算跨ぎ回避等）の明示seedを、
    # daily_log全履歴の走査より先に実行するよう順序を変更した。upsert_trade_rule_from_text()は
    # 「既存(rule_key一致)なら評価再計算のみ・confidenceは明示上書きしない」設計のため、
    # 先にdaily_logスキャンでconfidence=LOW（既定値）として作られてしまうと、後から実行する
    # 明示seedのconfidence=MEDIUM等の指定が反映されない不具合があった（実データ検証で発覚）。
    # 明示seedを先に確定させることで、A〜D・長期ルールの初期状態が指示書通りになる。

    # 1) 指示書5番のA〜C
    for e in _INSTRUCTION_SEED_RULES_ABC:
        res = upsert_trade_rule_from_text(
            database_url, user_id, e["text"], category=e["category"],
            initial_status="TESTING", initial_confidence=e["confidence"],
            action_text=e.get("action_text"), created_from="instruction_seed",
        )
        if res and res["action"] == "created":
            result["seededExplicit"] += 1

    # 2) 指示書5番のD（一回限りの例外、TEMPORARY・恒久ルール一覧には残るがACTIVE昇格対象外）
    res_d = upsert_trade_rule_from_text(
        database_url, user_id,
        "YE DIGITALの2026-09-09持ち越しは1日限定の例外。2026-09-10中に必ず決済し、持ち越し延長は禁止する。",
        category="position", scope="stock", rule_type="TEMPORARY",
        initial_status="TESTING", initial_confidence="MEDIUM",
        action_text="2026-09-10中にYE DIGITALを手仕舞う。延長禁止。",
        expires_date="2026-09-10", created_from="instruction_seed",
    )
    if res_d and res_d["action"] == "created":
        result["seededExplicit"] += 1

    # 3) 既存の長期ルール（決算跨ぎ回避等）がinvestment_rulesに存在しない場合のフォールバックseed
    for pr in _PERMANENT_LONGTERM_RULE_SEEDS:
        res_p = upsert_trade_rule_from_text(
            database_url, user_id, pr["text"], category=pr["category"], rule_type="PERMANENT",
            initial_status="ACTIVE", initial_confidence="HIGH",
            action_text=pr["action_text"], created_from="instruction_seed_permanent",
        )
        if res_p and res_p["action"] == "created":
            result["seededPermanent"] += 1

    # 4) investment_rules（既存の長期マイルール。ユーザーが既に運用してきた確立済みルールとして
    #    ACTIVE/HIGH・PERMANENTで登録する）
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM investment_rules WHERE user_id=%s", [user_id])
            old_rules = cur.fetchall()
    for r in old_rules:
        text = r.get("text")
        if not text:
            continue
        res = upsert_trade_rule_from_text(
            database_url, user_id, text, rule_type="PERMANENT",
            initial_status="ACTIVE", initial_confidence="HIGH",
            created_from="legacy_investment_rules",
        )
        if res and res["action"] == "created":
            result["migratedFromInvestmentRules"] += 1

    # 5) daily_log.raw_payload.rule_updates を全履歴分走査（最後に実行。既にA〜D・長期ルールと
    #    一致するものはevidence_countの加算のみで、confidenceは上書きされない）
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT id, date, raw_payload FROM daily_log WHERE user_id=%s AND raw_payload IS NOT NULL ORDER BY date",
                        [user_id])
            logs = cur.fetchall()
    for log in logs:
        payload = log.get("raw_payload") or {}
        rule_updates = payload.get("rule_updates")
        if not rule_updates:
            continue
        market = payload.get("market") or {}
        for ru in rule_updates:
            text = ru if isinstance(ru, str) else (ru or {}).get("rule") or (ru or {}).get("text")
            if not text:
                continue
            r = upsert_trade_rule_from_text(
                database_url, user_id, text,
                source_info={"date": log.get("date"), "daily_log_id": log.get("id"),
                              "market_condition": market.get("condition"),
                              "review_excerpt": (payload.get("review") or "")[:200] or None,
                              "decision_excerpt": None},
                initial_status="TESTING", initial_confidence="LOW",
                created_from="legacy_rule_update", seen_date=str(log.get("date")) if log.get("date") else None,
            )
            if r and r["action"] == "created":
                result["migratedFromDailyLog"] += 1

    return result


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
        # 2026-09-08更新（ChatGPT連携JSONスキーマ統一、指示書6番）：executionが既知の値
        # （ALLOWED_EXECUTION）でなくても、もう保存不可のエラーにはしない。execution_status
        # 列はTEXT型でDB側にCHECK制約は無く、共通スキーマのdecisions[].action（例：
        # "HOLD_WITH_CAUTION"）のような自由記述の値もそのまま記録できるほうが情報量を
        # 失わないため（本当に判定不能な場合＝codeが無い場合だけエラーにする、という方針）。
        # フロント側（validateChatGptPayload）も同様に緩和済み。
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
        # 2026-09-09追記（ChatGPT連携JSONスキーマ統一）：共通スキーマのrule_updatesは
        # {rule, status}形式（"text"ではなく"rule"キー）。旧形式の{"text":...}も
        # 引き続き読めるようフォールバックする（後方互換、指示書5番）。
        text = ru if isinstance(ru, str) else (ru or {}).get("rule") or (ru or {}).get("text")
        if not text:
            continue
        upsert_rule(database_url, user_id, {
            "id": "chatgpt-" + uuid.uuid4().hex[:8],
            "text": text,
            "active": True,
            "createdAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        })
        n_rules += 1

    # 2026-09-09新規（ルール学習システム）：上のinvestment_rules（旧・自由テキストの単純追記
    # 先）への書き込みは既存動作のまま完全に維持しつつ、同じrule_updatesをtrade_rules
    # （検証→信頼度更新→実戦利用のライフサイクルを持つ新テーブル）へも同期する。
    # 既存機能を壊さないための「並行稼働」方針（指示書「既存を壊さない」に対応）。
    rule_sync = sync_rule_updates_to_trade_rules(
        database_url, user_id, payload.get("rule_updates") or [],
        daily_log_id=log_id, date=payload.get("date"),
        market_condition=market.get("condition"),
        review_excerpt=payload.get("review"),
    )

    return {"dailyLogId": log_id, "importId": import_id, "judgments": n_judgments, "rulesAdded": n_rules,
            "ruleSync": rule_sync}


# ---- ChatGPT日次JSON（2026-09-05新規、PHASE 6 DAILY CHATGPT JSON IMPORT） ----
# 上のsave_chatgpt_import（投資ログ形式・kind='TRADING_LOG'）とは別の用途。ChatGPTとの日々の
# 投資・Trade Cockpit相談内容の要約（date/summary/trading_observations/app_changes/
# bugs_or_risks/new_rules_or_preferences/updates/claude_code_instruction）をchatgpt_imports
# テーブルにkind='DAILY_DIGEST'として保存する。「Import（履歴保存）」と「Apply（差分適用）」を
# 明確に分離し、updates=[]の場合はinvestment_rules等に一切書き込まない
# （ユーザー指定の最重要ルール）。

# SWING -10% hard stop等、日次JSONだけで簡単に弱体化・削除されては困る重要ルールのrule_code。
# 対象になった場合はWARNING_PROTECTEDとして扱い、Apply Updatesでは自動適用しない
# （マイルールタブから手動で変更してもらう）。CURRENT/SEEN・Stage1共有キャッシュ・
# enrichWatchRow() SSoT・Primary/Action分離等、investment_rulesの行として存在しない
# アーキテクチャ上の不変条件は、そもそもtargetが"investment_rules."で始まらない限り
# UNSUPPORTED_TARGETとして自動適用の対象外になる（コード側の保護は別途このリストに頼らない）。
PROTECTED_RULE_CODES = {"SWING_STOP_LOSS"}


def validate_daily_digest_payload(payload):
    """日次JSON貼り付けを解析したdictを検証し、エラーメッセージのリストを返す（空なら合格）。
    サーバー側検証が最終防衛線（validate_chatgpt_payloadと同じ位置付け）。"""
    errors = []
    if not isinstance(payload, dict):
        return ["JSONのトップレベルはオブジェクトである必要があります"]
    date = payload.get("date")
    if not date or not isinstance(date, str) or not _DATE_RE.match(date):
        errors.append("date は YYYY-MM-DD 形式の文字列で必須です")
    for key in ("summary", "trading_observations", "app_changes", "bugs_or_risks",
                "new_rules_or_preferences", "updates"):
        v = payload.get(key)
        if v is not None and not isinstance(v, list):
            errors.append(f"{key} は配列である必要があります")
    instruction = payload.get("claude_code_instruction")
    if instruction is not None and not isinstance(instruction, str):
        errors.append("claude_code_instruction は文字列である必要があります")
    updates = payload.get("updates")
    if isinstance(updates, list):
        for i, u in enumerate(updates):
            if not isinstance(u, dict):
                errors.append(f"updates[{i}] はオブジェクトである必要があります")
                continue
            if not u.get("target"):
                errors.append(f"updates[{i}] に target がありません")
            ct = u.get("change_type")
            if ct not in ("add", "update", "remove"):
                errors.append(f"updates[{i}].change_type は add|update|remove のいずれかである必要があります（値: {ct}）")
    return errors


def find_daily_digest_duplicate(database_url, user_id, payload):
    """同一内容（payload_hash一致）の取り込み済みDAILY_DIGESTレコードがあれば返す（無ければNone）。
    同じdateでも内容が違えばhashが変わるため別レコードとして保存できる（修正版JSONの許容）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return None
    h = _payload_hash(payload)
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, import_date, imported_at FROM chatgpt_imports "
                "WHERE user_id = %s AND kind = 'DAILY_DIGEST' AND payload_hash = %s",
                [user_id, h],
            )
            row = cur.fetchone()
            return _row_to_json(row) if row else None


def save_daily_digest_import(database_url, user_id, payload, force=False):
    """検証済み（validate_daily_digest_payloadでエラー0件確認済み）のpayloadをchatgpt_importsへ
    kind='DAILY_DIGEST'として保存するだけ（investment_rules等への書き込みは一切行わない＝
    Applyは別関数で明示的に呼ぶまで実行しない）。
    戻り値: {"error": ...} または {"importId":.., "applyStatus":.., "updatesCount":N}。"""
    pool = _get_pool(database_url)
    if pool is None:
        return {"error": "DB未設定（DATABASE_URLが未設定、またはpsycopg未インストール）"}
    dup = find_daily_digest_duplicate(database_url, user_id, payload)
    if dup and not force:
        return {"error": f"同じ内容のログは既に取り込み済みです（{dup['import_date']}に取り込み、import_id {dup['id']}）"}
    updates = payload.get("updates") or []
    apply_status = "NO_UPDATES" if not updates else "IMPORTED"
    h = _payload_hash(payload)
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO chatgpt_imports (user_id, import_date, payload_hash, raw_payload, kind, "
                "summary, trading_observations, app_changes, bugs_or_risks, new_rules_or_preferences, "
                "updates, claude_code_instruction, apply_status) "
                "VALUES (%s, %s, %s, %s::jsonb, 'DAILY_DIGEST', %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, "
                "%s::jsonb, %s::jsonb, %s, %s) RETURNING id",
                [
                    user_id, payload.get("date"), h, json.dumps(payload, ensure_ascii=False),
                    json.dumps(payload.get("summary") or [], ensure_ascii=False),
                    json.dumps(payload.get("trading_observations") or [], ensure_ascii=False),
                    json.dumps(payload.get("app_changes") or [], ensure_ascii=False),
                    json.dumps(payload.get("bugs_or_risks") or [], ensure_ascii=False),
                    json.dumps(payload.get("new_rules_or_preferences") or [], ensure_ascii=False),
                    json.dumps(updates, ensure_ascii=False),
                    payload.get("claude_code_instruction") or "",
                    apply_status,
                ],
            )
            import_id = cur.fetchone()[0]
        conn.commit()
    return {"importId": import_id, "applyStatus": apply_status, "updatesCount": len(updates)}


def list_daily_digest_imports(database_url, user_id, limit=30):
    pool = _get_pool(database_url)
    if pool is None:
        return []
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, import_date, imported_at, apply_status, applied_at, "
                "summary, trading_observations, app_changes, bugs_or_risks, new_rules_or_preferences, "
                "updates, claude_code_instruction FROM chatgpt_imports "
                "WHERE user_id = %s AND kind = 'DAILY_DIGEST' ORDER BY imported_at DESC LIMIT %s",
                [user_id, limit],
            )
            return [_row_to_json(r) for r in cur.fetchall()]


def get_daily_digest_import(database_url, user_id, import_id):
    pool = _get_pool(database_url)
    if pool is None:
        return None
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM chatgpt_imports WHERE user_id = %s AND id = %s AND kind = 'DAILY_DIGEST'",
                [user_id, import_id],
            )
            row = cur.fetchone()
            return _row_to_json(row) if row else None


def _rules_by_code(database_url, user_id):
    by_code = {}
    for r in list_rules(database_url, user_id):
        rc = r.get("rule_code")
        if rc:
            by_code[rc.upper()] = r
    return by_code


def _diff_one_update(rules_by_code, update):
    """updates[]の1件を、現在のinvestment_rulesと比較して差分ステータスを判定する（DBへの
    書き込みは一切行わない）。status候補：NO_CHANGE（既存値と同じ）／ADD_CANDIDATE（targetが
    存在しない）／DIFF（実際に差分があり適用可能）／WARNING_PROTECTED（保護ルールへの
    update/remove、自動適用しない）／UNSUPPORTED_TARGET（investment_rules.*以外は自動適用
    できない＝Claude Codeへの変更指示として手動対応）。"""
    update = update or {}
    target = update.get("target") or ""
    change_type = update.get("change_type")
    result = {"target": target, "change_type": change_type, "reason": update.get("reason"),
              "instruction": update.get("instruction")}
    if not target.startswith("investment_rules."):
        result["status"] = "UNSUPPORTED_TARGET"
        result["note"] = "investment_rules.<rule_code> 形式以外は自動適用できません（claude_code_instructionとして手動対応してください）"
        return result
    rule_code = target.split(".", 1)[1].strip().upper()
    result["rule_code"] = rule_code
    current = rules_by_code.get(rule_code)
    result["current"] = ({"text": current.get("text"), "value": current.get("value"),
                           "unit": current.get("unit"), "priority": current.get("priority")}
                          if current else None)
    proposed_text = update.get("text", update.get("instruction"))
    proposed_value = update.get("value")
    result["proposed"] = {
        "text": proposed_text,
        "value": proposed_value if proposed_value is not None else (current.get("value") if current else None),
        "unit": update.get("unit", current.get("unit") if current else None),
        "priority": update.get("priority", current.get("priority") if current else None),
    }
    protected = rule_code in PROTECTED_RULE_CODES

    if change_type == "remove":
        if not current:
            result["status"] = "NO_CHANGE"
            result["note"] = "対象が存在しないため削除不要"
        elif protected:
            result["status"] = "WARNING_PROTECTED"
            result["note"] = "保護ルールのため自動適用しません。必要ならマイルールタブから手動で削除してください"
        else:
            result["status"] = "DIFF"
        return result

    if not current:
        result["status"] = "ADD_CANDIDATE"
        return result

    # 実際の現在値との比較：valueが指定されていればvalueで比較（数値ルールの「本当の現在値」）、
    # 無指定ならtext比較（自由記述ルール）。「ChatGPT JSONにupdateがある」だけでは更新しない。
    if proposed_value is not None and current.get("value") is not None:
        same = float(current["value"]) == float(proposed_value)
    elif proposed_value is not None:
        same = False
    else:
        same = (current.get("text") or "").strip() == (proposed_text or "").strip()

    if same:
        result["status"] = "NO_CHANGE"
    elif protected:
        result["status"] = "WARNING_PROTECTED"
        result["note"] = "保護ルールのため自動適用しません。必要ならマイルールタブから手動で変更してください"
    else:
        result["status"] = "DIFF"
    return result


def preview_daily_digest_updates(database_url, user_id, import_id):
    """指定importのupdates配列について、現在のinvestment_rulesと比較した差分プレビューを返す
    （DBへの書き込みは一切行わない）。import_idが見つからなければNone。"""
    row = get_daily_digest_import(database_url, user_id, import_id)
    if row is None:
        return None
    updates = row.get("updates") or []
    rules_by_code = _rules_by_code(database_url, user_id)
    return [_diff_one_update(rules_by_code, u) for u in updates]


def apply_daily_digest_updates(database_url, user_id, import_id):
    """importに保存されたupdatesのうち、DIFF／ADD_CANDIDATEのものだけをinvestment_rulesへ
    適用する。NO_CHANGE・WARNING_PROTECTED・UNSUPPORTED_TARGETは自動適用しない（安全側）。
    クライアント側の古いプレビューを信用せず、適用直前に必ず差分判定を再計算する。
    戻り値: {"error": ...} または {"results":[...], "applyStatus":..}。"""
    row = get_daily_digest_import(database_url, user_id, import_id)
    if row is None:
        return {"error": "指定されたimportが見つかりません"}
    updates = row.get("updates") or []
    if not updates:
        return {"results": [], "applyStatus": "NO_UPDATES"}

    rules_by_code = _rules_by_code(database_url, user_id)
    diffs = [_diff_one_update(rules_by_code, u) for u in updates]
    applied = failed = 0
    for d in diffs:
        status = d["status"]
        if status not in ("DIFF", "ADD_CANDIDATE"):
            d["outcome"] = "SKIPPED"
            continue
        try:
            rc = d["rule_code"]
            current = rules_by_code.get(rc)
            if d["change_type"] == "remove" and current:
                delete_rule(database_url, user_id, current["id"])
            else:
                rid = current["id"] if current else ("structured-" + rc.lower())
                upsert_rule(database_url, user_id, {
                    "id": rid, "text": d["proposed"]["text"], "active": True,
                    "createdAt": (current or {}).get("createdAt") or datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "rule_code": rc, "value": d["proposed"]["value"], "unit": d["proposed"]["unit"],
                    "priority": d["proposed"]["priority"],
                })
            d["outcome"] = "APPLIED"
            applied += 1
        except Exception as e:
            d["outcome"] = "FAILED"
            d["error"] = str(e)
            failed += 1

    no_change_only = all(d["status"] == "NO_CHANGE" for d in diffs)
    if failed and applied:
        overall = "PARTIAL"
    elif failed:
        overall = "FAILED"
    elif applied:
        overall = "APPLIED"
    elif no_change_only:
        overall = "NO_UPDATES"
    else:
        # 全件WARNING_PROTECTED／UNSUPPORTED_TARGETで意図的にSKIPした場合。「何も変更する
        # 必要が無かった」わけではなく「要対応だが自動適用しなかった」ことを区別するため
        # NO_UPDATESではなくPARTIALとして報告する。
        overall = "PARTIAL"

    pool = _get_pool(database_url)
    if pool is not None:
        with pool.connection() as conn:
            conn.execute(
                "UPDATE chatgpt_imports SET apply_status = %s, apply_result = %s::jsonb, applied_at = now() "
                "WHERE user_id = %s AND id = %s",
                [overall, json.dumps(diffs, ensure_ascii=False), user_id, import_id],
            )
            conn.commit()
    return {"results": diffs, "applyStatus": overall}


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


# v3-9（監視銘柄自動登録エンジン）：手動登録との事故防止のため、auto_tags/manual_registeredは
# _WATCHLIST_COLS（クライアントの汎用保存エンドポイントの書き込み許可列）に含めず、以下の専用
# 関数だけが触る。upsert_watchlist_item()側の一般的な部分更新の仕組みとは完全に独立させている。
def auto_register_or_tag_watchlist_item(database_url, user_id, code, market, reason_key, tag_value, item_fields=None):
    """自動登録エンジン専用。銘柄が未登録なら manual_registered=false で新規作成し、既に存在する
    銘柄（手動登録・他の自動登録いずれでも）なら auto_tags の reason_key キーだけをマージする
    （他のキー・manual_registered・name/sector等の既存値には一切触れない）。
    tag_value例: {"score": 82, "addedAt": "...", "expiresAt": "..."}
    item_fields: 新規作成時のみ使うname/sector/kana等の初期値（dict、省略可）。既存行の更新には使わない。
    戻り値: True=成功。"""
    pool = _get_pool(database_url)
    if pool is None:
        return False
    item_fields = item_fields or {}
    tag_json = json.dumps({reason_key: tag_value}, ensure_ascii=False)
    with pool.connection() as conn:
        # ①未登録の場合だけ新規作成（ON CONFLICT DO NOTHING＝既存行があれば何もしない＝
        # 既存のmanual_registeredや他の列を一切上書きしない）。
        extra_cols = [c for c in _WATCHLIST_COLS if c in item_fields]
        conn.execute(
            f"INSERT INTO watchlist (user_id, code, market, manual_registered, auto_tags"
            + ("".join(f", {c}" for c in extra_cols)) + ") "
            f"VALUES (%s, %s, %s, false, %s::jsonb" + ("".join(", %s" for _ in extra_cols)) + ") "
            f"ON CONFLICT (user_id, code, market) DO NOTHING",
            [user_id, code, market, tag_json] + [item_fields.get(c) for c in extra_cols],
        )
        # ②reason_keyのタグをマージ（新規作成された行にも、既存行にも同じ処理で適用される＝
        # 二重に書く必要がない）。COALESCEでauto_tagsが元々NULLの既存行にも対応する。
        conn.execute(
            "UPDATE watchlist SET auto_tags = COALESCE(auto_tags, '{}'::jsonb) || %s::jsonb, "
            "updated_at = now(), active = true "
            "WHERE user_id = %s AND code = %s AND market = %s",
            [tag_json, user_id, code, market],
        )
        conn.commit()
    return True


# v3-9改訂（MOMENTUM DAY：CURRENT/SEENの分離）：同日に複数回スキャンした際、その都度のTOP15が
# 無限に累積しないようにするため、「最新TOP15（CURRENT）」から外れた銘柄はタグを消さずに
# 「本日圏内だった履歴（SEEN）」へ降格させる。この2関数は上のauto_register_or_tag_watchlist_item
# と組み合わせて使う（降格＝SEENへの昇格はauto_register_or_tag_watchlist_item、CURRENTタグの
# 除去はremove_auto_tag_keyで行う）。
def remove_auto_tag_key(database_url, user_id, code, market, reason_key):
    """auto_tagsから指定のreason_keyだけを取り除く（他のキー・manual_registered・name等の
    既存値には一切触れない）。行が無い、またはそのキーを持たない場合は何もしない。"""
    pool = _get_pool(database_url)
    if pool is None:
        return False
    with pool.connection() as conn:
        conn.execute(
            "UPDATE watchlist SET auto_tags = auto_tags - %s, updated_at = now() "
            "WHERE user_id = %s AND code = %s AND market = %s AND auto_tags ? %s",
            [reason_key, user_id, code, market, reason_key],
        )
        conn.commit()
    return True


def get_codes_with_auto_tag(database_url, user_id, reason_key, market=None):
    """指定のauto_tagsキーを持つ銘柄コードの集合を返す。新しいスキャン結果と比較して
    「前回CURRENTだったが今回は外れた」銘柄を特定するために使う。"""
    pool = _get_pool(database_url)
    if pool is None:
        return set()
    where = ["user_id = %s", "auto_tags ? %s"]
    params = [user_id, reason_key]
    if market:
        where.append("market = %s")
        params.append(market)
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT code FROM watchlist WHERE {' AND '.join(where)}", params)
            return {r[0] for r in cur.fetchall()}


def cleanup_expired_auto_tags(database_url, user_id):
    """auto_tagsの中で有効期限切れの理由キーだけを取り除く。結果としてauto_tagsが空になり、
    かつ manual_registered=false（＝自動登録のみで維持されていた銘柄）かつ保有ポジションでも
    ない銘柄は、監視銘柄から削除する。手動登録銘柄（manual_registered=true）は auto_tags が
    空になっても絶対に削除しない（タグを空にするだけ）。呼び出し側（/api/watchlist等）から
    軽量に毎回呼べるよう、対象行が無ければ何もしない設計。"""
    pool = _get_pool(database_url)
    if pool is None:
        return {"expired": 0, "deleted": 0}
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    expired_count = 0
    deleted_count = 0
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, code, market, manual_registered, auto_tags FROM watchlist "
                "WHERE user_id = %s AND auto_tags IS NOT NULL AND auto_tags != '{}'::jsonb",
                [user_id],
            )
            rows = cur.fetchall()
        if not rows:
            return {"expired": 0, "deleted": 0}
        # 保有ポジション（portfolio.active=true）のコードは自動削除対象から除外する。
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT code, market FROM portfolio WHERE user_id = %s AND active = true", [user_id])
            held = {(r["code"], r["market"]) for r in cur.fetchall()}
        for row in rows:
            tags = row["auto_tags"] or {}
            kept = {k: v for k, v in tags.items() if not (isinstance(v, dict) and v.get("expiresAt") and v["expiresAt"] < now_iso)}
            if len(kept) == len(tags):
                continue  # 期限切れなし
            expired_count += len(tags) - len(kept)
            if not kept and not row["manual_registered"] and (row["code"], row["market"]) not in held:
                # 自動登録のみで維持されていた銘柄が、有効なauto_tagsを1件も持たなくなった
                # ＝手動登録でも保有中でもない → 削除して良い唯一のケース。
                conn.execute("DELETE FROM watchlist WHERE id = %s", [row["id"]])
                deleted_count += 1
            else:
                conn.execute(
                    "UPDATE watchlist SET auto_tags = %s::jsonb, updated_at = now() WHERE id = %s",
                    [json.dumps(kept, ensure_ascii=False), row["id"]],
                )
        conn.commit()
    return {"expired": expired_count, "deleted": deleted_count}


# v3-9続き（2026-09-05・PHASE 1 AUTO SIGNAL LOG）：5つの自動登録エンジン共通の履歴ログ。
# watchlist.auto_tags（現在状態、CURRENT/SEEN・期限切れで消える）とは役割を分離し、こちらは
# 状態遷移（ENTER_CURRENT/EXIT_CURRENT/REENTER_CURRENT/EXPIRE）が起きた時だけ1行追加する
# 恒久履歴（削除・上書きは行わない）。呼び出し側（server.py）が「同じCURRENTの毎スキャン
# 再INSERT」を避ける判定を行った上でこの関数を呼ぶ設計＝この関数自体は単純なINSERTのみ。
def log_auto_signal_event(database_url, user_id, code, market, signal_type, event_type, event_date,
                           score=None, current_price=None, day_change_pct=None, market_rs=None,
                           sector_rs=None, turnover=None, high_retention=None,
                           primary_status=None, action_status=None, metadata=None):
    """auto_signal_eventsへ1件記録する。primary_status/action_statusは、サーバー側のスキャンが
    enrichWatchRow()（クライアント専用のSSoT）の結果を持たないため、現時点では意図的にNULLの
    まま記録する（2026-09-05ユーザー判断：事後補完APIは見送り）。戻り値: True=成功。"""
    pool = _get_pool(database_url)
    if pool is None:
        return False
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO auto_signal_events (user_id, code, market, signal_type, event_type, event_date, "
            "score, current_price, day_change_pct, market_rs, sector_rs, turnover, high_retention, "
            "primary_status, action_status, metadata) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)",
            [user_id, code, market, signal_type, event_type, event_date,
             score, current_price, day_change_pct, market_rs, sector_rs, turnover, high_retention,
             primary_status, action_status, json.dumps(metadata or {}, ensure_ascii=False)],
        )
        conn.commit()
    return True


def list_auto_signal_events(database_url, user_id, code=None, signal_type=None, limit=200):
    """auto_signal_eventsの履歴を新しい順に返す（検証・確認用）。codeやsignal_typeで絞り込み可能。"""
    pool = _get_pool(database_url)
    if pool is None:
        return []
    where, params = ["user_id = %s"], [user_id]
    if code:
        where.append("code = %s")
        params.append(code)
    if signal_type:
        where.append("signal_type = %s")
        params.append(signal_type)
    params.append(limit)
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"SELECT * FROM auto_signal_events WHERE {' AND '.join(where)} "
                f"ORDER BY detected_at DESC LIMIT %s",
                params,
            )
            return [_row_to_json(r) for r in cur.fetchall()]


# v3-9続き（2026-09-05・PHASE 3 EVENT/EARNINGS INTELLIGENCE）：market_events。
# 既存のwatchlist_imports/chatgpt_importsと同じ「validation→duplicate check→保存」の考え方を
# 踏襲しつつ、イベントは1件ずつ個別に検索・参照したい（銘柄分析画面での「次回決算まで何営業日」
# 等の計算に使うため）ため、JSON全体を1行で保存するのではなく、パース後に1イベント1行で
# upsertする設計にする。
_MARKET_EVENT_COLS = ["event_time", "timezone", "country", "event_type", "importance",
                       "affected_markets", "affected_sectors", "affected_stocks", "impact_channels",
                       "source", "source_type", "verification_status", "notes", "raw_payload"]
_MARKET_EVENT_JSONB_COLS = {"affected_markets", "affected_sectors", "affected_stocks", "impact_channels", "raw_payload"}


_MARKET_EVENT_IMPORTANCE_LEGACY = {5: "HIGH", 4: "HIGH", 3: "MEDIUM", 2: "LOW", 1: "LOW"}


def _normalize_market_event(ev):
    """2026-09-07追加：ChatGPT等から従来受け取っていたlegacy形式（date/event/type/note、
    importanceが1〜5の整数、time_jstが"26:00"等の24時超表現）を、market_events標準形式
    （event_date/title/event_type/notes、importanceがHIGH/MEDIUM/LOW、event_timeが
    00:00〜23:59）に変換する。標準形式のキーが既に入っている場合はそちらを優先し、
    値を上書きしない（既存の標準JSON形式を壊さないため、あくまで「無い場合の補完」）。
    event_typeは値をそのまま通す（許可リストによる制限はしない。未知のevent_typeでも
    Dashboardの「すべて」フィルタで表示できることを優先する）。
    24時超のevent_time（例："26:00"）はevent_dateを+1日し00:00〜23:59に収めて正規化する。
    正規化できない場合はValueErrorを送出し、呼び出し側（import_market_events）で
    この1件だけをエラー扱いにする（他の行を巻き添えにしない設計、SAVEPOINTと対）。"""
    ev = dict(ev)  # 呼び出し元の元dictは書き換えない

    if not ev.get("event_date") and ev.get("date"):
        ev["event_date"] = ev["date"]
    if not ev.get("title") and ev.get("event"):
        ev["title"] = ev["event"]
    if not ev.get("event_type") and ev.get("type"):
        ev["event_type"] = ev["type"]
    if not ev.get("notes") and ev.get("note"):
        ev["notes"] = ev["note"]
    if not ev.get("event_time"):
        legacy_time = ev.get("time_jst") or ev.get("event_time_jst")
        if legacy_time:
            ev["event_time"] = legacy_time

    imp = ev.get("importance")
    imp_int = None
    if isinstance(imp, bool):
        imp_int = None
    elif isinstance(imp, (int, float)):
        imp_int = int(imp)
    elif isinstance(imp, str) and imp.strip().isdigit():
        imp_int = int(imp.strip())
    if imp_int is not None and imp_int in _MARKET_EVENT_IMPORTANCE_LEGACY:
        ev["importance"] = _MARKET_EVENT_IMPORTANCE_LEGACY[imp_int]

    et = ev.get("event_time")
    if et:
        m = re.match(r"^(\d{1,3}):(\d{2})$", str(et).strip())
        if not m:
            raise ValueError(f"event_time（{et}）の形式が不正です（HH:MM形式で指定してください）")
        hh, mm = int(m.group(1)), int(m.group(2))
        if mm > 59:
            raise ValueError(f"event_time（{et}）の形式が不正です（分が59を超えています）")
        if hh >= 24:
            extra_days, hh = divmod(hh, 24)
            d = ev.get("event_date")
            if not d:
                raise ValueError(f"event_time（{et}）が24時超ですがevent_dateが無いため正規化できません")
            try:
                base = datetime.date.fromisoformat(str(d)[:10])
            except ValueError:
                raise ValueError(f"event_date（{d}）の形式が不正なためevent_time（{et}）を正規化できません")
            ev["event_date"] = (base + datetime.timedelta(days=extra_days)).isoformat()
            ev["event_time"] = f"{hh:02d}:{mm:02d}"
    return ev


def _event_display_title(ev):
    """スキップ/エラー理由をユーザーに表示する際、どの行の話か分かるようにするための
    参考タイトル。正規化前後どちらのキーでも拾えるようにevent/titleの両方を見る
    （normalize自体が失敗した行でも表示できるようにするため）。"""
    if not isinstance(ev, dict):
        return None
    return ev.get("title") or ev.get("event") or None


def _upsert_market_event_conn(conn, user_id, ev):
    """正規化済み（_normalize_market_event適用後）のイベント1件をupsertする。
    event_date・titleが無い場合はNoneを返す（呼び出し側でスキップ扱い）。
    verification_statusは未指定ならUNVERIFIED（画像由来等を確定情報として扱わない、
    既定の安全側）。戻り値：新規作成ならTrue、既存行の更新ならFalse、保存不可ならNone。
    2026-09-07追加（STEP4：新規/更新の件数を分けて報告できるようにする）：
    `RETURNING (xmax = 0) AS is_insert`は、そのUPSERTが実際にINSERTだったか
    （ON CONFLICTでのUPDATEではなかったか）をPostgreSQL内部列xmaxから判定する定石。"""
    event_date = ev.get("event_date")
    title = ev.get("title")
    if not event_date or not title:
        return None
    cols = ["event_date", "title"] + [c for c in _MARKET_EVENT_COLS if c in ev or c == "verification_status"]
    values = []
    for c in cols:
        if c == "event_date":
            values.append(event_date)
        elif c == "title":
            values.append(title)
        elif c == "verification_status":
            values.append(ev.get("verification_status") or "UNVERIFIED")
        elif c in _MARKET_EVENT_JSONB_COLS:
            values.append(json.dumps(ev.get(c), ensure_ascii=False) if ev.get(c) is not None else None)
        else:
            values.append(ev.get(c))
    update_cols = [c for c in cols if c not in ("event_date", "title")]
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"INSERT INTO market_events (user_id, {', '.join(cols)}) "
            f"VALUES (%s, {', '.join(['%s::jsonb' if c in _MARKET_EVENT_JSONB_COLS else '%s' for c in cols])}) "
            f"ON CONFLICT (user_id, event_date, title) DO UPDATE SET "
            f"{', '.join(c + ' = EXCLUDED.' + c for c in update_cols)}, updated_at = now() "
            f"RETURNING (xmax = 0) AS is_insert",
            [user_id] + values,
        )
        row = cur.fetchone()
    return bool(row and row.get("is_insert"))


def import_market_events(database_url, user_id, events):
    """events（dictのリスト、JSON貼り付けのimport想定）を1件ずつupsertする。
    2026-09-07更新（legacy JSON互換）：各行について
    「抽出（呼び出し側のextractEventsArray）→正規化（_normalize_market_event）→
    validation（event_date/title必須チェック）→DB upsert」の順で処理する
    （以前はupsert直前で正規化していたため、正規化結果を見てvalidationするという
    順序になっていなかった）。
    戻り値: {"imported": N（新規作成）, "updated": M（既存行の上書き）,
    "skipped": K（event_date/titleが無い等で保存不可）, "errors": E（正規化/DBの例外）,
    "skipped_details"/"error_details": [{"index","title","reason"}, ...]（最大10件、
    UIで「原因が分からない」を防ぐため）}。
    2026-09-07更新（STEP4）：新規/更新/スキップ/エラーを分けて報告できるようにした
    （以前はimported=新規+更新の合計だった）。1件の例外で全体を失敗させないよう
    1件ずつtry/exceptする。"""
    _DETAIL_LIMIT = 10
    pool = _get_pool(database_url)
    if pool is None:
        details = [{"index": i, "title": _event_display_title(e), "reason": "DB_NOT_CONFIGURED"}
                   for i, e in enumerate(events)][:_DETAIL_LIMIT]
        return {"imported": 0, "updated": 0, "skipped": len(events), "errors": 0,
                "skipped_details": details, "error_details": []}
    imported = updated = skipped = errors = 0
    skipped_details = []
    error_details = []
    with pool.connection() as conn:
        for i, ev in enumerate(events):
            if not isinstance(ev, dict):
                skipped += 1
                if len(skipped_details) < _DETAIL_LIMIT:
                    skipped_details.append({"index": i, "title": None, "reason": "NOT_AN_OBJECT"})
                continue
            raw_title = _event_display_title(ev)
            try:
                norm = _normalize_market_event(ev)
            except ValueError as e:
                errors += 1
                if len(error_details) < _DETAIL_LIMIT:
                    error_details.append({"index": i, "title": raw_title, "reason": str(e)})
                continue
            if not norm.get("event_date") or not norm.get("title"):
                skipped += 1
                if len(skipped_details) < _DETAIL_LIMIT:
                    reason = "MISSING_EVENT_DATE" if not norm.get("event_date") else "MISSING_TITLE"
                    skipped_details.append({"index": i, "title": raw_title, "reason": reason})
                continue
            try:
                # 2026-09-07追加：1行のエラーで残り全件を巻き添えにしないためのSAVEPOINT。
                # psycopg3のconn.transaction()は既存トランザクション内ではSAVEPOINTとして
                # 動作し、例外時はこの1行分だけロールバックする（PostgreSQLは1文でも失敗すると
                # トランザクション全体がabortedになるため、素のtry/exceptだけでは以降の行も
                # 全て失敗してしまう＝この対策が無いと確認できた実際の落とし穴）。
                with conn.transaction():
                    is_insert = _upsert_market_event_conn(conn, user_id, norm)
            except Exception as e:
                errors += 1
                if len(error_details) < _DETAIL_LIMIT:
                    error_details.append({"index": i, "title": raw_title, "reason": str(e)})
                continue
            if is_insert is None:
                skipped += 1
                if len(skipped_details) < _DETAIL_LIMIT:
                    skipped_details.append({"index": i, "title": raw_title, "reason": "DB_UPSERT_SKIPPED"})
            elif is_insert:
                imported += 1
            else:
                updated += 1
        conn.commit()
    return {"imported": imported, "updated": updated, "skipped": skipped, "errors": errors,
            "skipped_details": skipped_details, "error_details": error_details}


def list_market_events(database_url, user_id, from_date=None, to_date=None, limit=200):
    """user_idのイベントをevent_date昇順で返す。from_date/to_dateはISO日付文字列（両端含む）。
    省略時は全期間（limit件まで）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return []
    where, params = ["user_id = %s"], [user_id]
    if from_date:
        where.append("event_date >= %s")
        params.append(from_date)
    if to_date:
        where.append("event_date <= %s")
        params.append(to_date)
    params.append(limit)
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"SELECT * FROM market_events WHERE {' AND '.join(where)} "
                f"ORDER BY event_date ASC LIMIT %s",
                params,
            )
            return [_row_to_json(r) for r in cur.fetchall()]


def delete_market_event(database_url, user_id, event_id):
    pool = _get_pool(database_url)
    if pool is None:
        return
    with pool.connection() as conn:
        conn.execute("DELETE FROM market_events WHERE user_id = %s AND id = %s", [user_id, event_id])
        conn.commit()


# v3-9続き（2026-09-05・PHASE 4 NEWS/CATALYST INTELLIGENCE）：news_catalysts。market_eventsと
# 全く同じ「JSON貼り付け→1件ずつupsert」の考え方を踏襲する。catalyst_date・titleが必須キー
# （UNIQUE制約もこの2つ）、event_date（発効日）は任意。
_NEWS_CATALYST_COLS = ["event_date", "category", "importance", "sentiment", "summary", "affected_markets",
                        "affected_sectors", "affected_stocks", "source", "source_type",
                        "verification_status", "notes", "raw_payload"]
_NEWS_CATALYST_JSONB_COLS = {"affected_markets", "affected_sectors", "affected_stocks", "raw_payload"}

# 2026-09-08新規（ニュース・材料連携の改善）：指示書1番の好材料/悪材料キーワードリストに基づく
# 簡易分類。importがsentimentを明示していない場合のみのフォールバックとして使う（ChatGPT等が
# 生成した構造化JSONに既にsentimentが入っていればそれを優先し、ここでは上書きしない）。
# タイトル・本文（summary）の両方を対象に、好材料語・悪材料語のどちらか一方だけがヒットすれば
# その方向、両方または片方もヒットしなければneutral（指示書9番：確信が無ければneutralとし、
# 無理にpositive/negativeを決めない）。あくまで簡易ヒューリスティックであり、Primary/Action
# Status等の売買判定ロジックには一切使わない（参考情報の分類のみ）。
_CATALYST_POSITIVE_KEYWORDS = [
    "新製品", "新サービス", "大型受注", "提携", "業務提携", "資本提携", "協業",
    "事業化", "量産開始", "量産化", "採用決定", "上方修正", "自社株買い", "増配",
]
_CATALYST_NEGATIVE_KEYWORDS = [
    "下方修正", "減配", "不祥事", "事故", "訴訟", "公募増資", "希薄化", "大型売出し", "売出し",
]


def _classify_catalyst_sentiment(title, summary=None):
    """タイトル・本文からsentiment（positive/negative/neutral）を推定する。判定できない・
    どちらとも取れる場合はNone（呼び出し側でneutral扱い、または未設定のまま）を返す。"""
    text = f"{title or ''} {summary or ''}"
    has_pos = any(kw in text for kw in _CATALYST_POSITIVE_KEYWORDS)
    has_neg = any(kw in text for kw in _CATALYST_NEGATIVE_KEYWORDS)
    if has_pos and not has_neg:
        return "positive"
    if has_neg and not has_pos:
        return "negative"
    return None


def _upsert_news_catalyst_conn(conn, user_id, cat):
    """1件のカタリストdictをupsertする。catalyst_date・titleは必須（無ければFalseを返し
    呼び出し側でカウントしない）。categoryは未指定ならOTHER、verification_statusは未指定なら
    UNVERIFIED（画像由来等を確定情報として扱わない、既定の安全側）。sentimentは未指定なら
    キーワードベースで推定を試み、判定できなければneutralとして保存する（指示書3・9番）。"""
    catalyst_date = cat.get("catalyst_date") or cat.get("date")
    title = cat.get("title")
    if not catalyst_date or not title:
        return False
    cols = ["catalyst_date", "title", "sentiment"] + [c for c in _NEWS_CATALYST_COLS if (c in cat or c in ("category", "verification_status")) and c != "sentiment"]
    values = []
    for c in cols:
        if c == "catalyst_date":
            values.append(catalyst_date)
        elif c == "title":
            values.append(title)
        elif c == "sentiment":
            values.append(cat.get("sentiment") or _classify_catalyst_sentiment(title, cat.get("summary")) or "neutral")
        elif c == "category":
            values.append(cat.get("category") or "OTHER")
        elif c == "verification_status":
            values.append(cat.get("verification_status") or "UNVERIFIED")
        elif c in _NEWS_CATALYST_JSONB_COLS:
            values.append(json.dumps(cat.get(c), ensure_ascii=False) if cat.get(c) is not None else None)
        else:
            values.append(cat.get(c))
    update_cols = [c for c in cols if c not in ("catalyst_date", "title")]
    conn.execute(
        f"INSERT INTO news_catalysts (user_id, {', '.join(cols)}) "
        f"VALUES (%s, {', '.join(['%s::jsonb' if c in _NEWS_CATALYST_JSONB_COLS else '%s' for c in cols])}) "
        f"ON CONFLICT (user_id, catalyst_date, title) DO UPDATE SET "
        f"{', '.join(c + ' = EXCLUDED.' + c for c in update_cols)}, updated_at = now()",
        [user_id] + values,
    )
    return True


def import_news_catalysts(database_url, user_id, catalysts):
    """catalysts（dictのリスト、JSON貼り付けのimport想定）を1件ずつupsertする。
    戻り値: {"imported": N, "skipped": M}（catalyst_date/titleが無い行はskip）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return {"imported": 0, "skipped": len(catalysts)}
    imported = skipped = 0
    with pool.connection() as conn:
        for cat in catalysts:
            if not isinstance(cat, dict):
                skipped += 1
                continue
            ok = _upsert_news_catalyst_conn(conn, user_id, cat)
            if ok:
                imported += 1
            else:
                skipped += 1
        conn.commit()
    return {"imported": imported, "skipped": skipped}


def list_news_catalysts(database_url, user_id, from_date=None, to_date=None, category=None, limit=300):
    """user_idのカタリストをcatalyst_date降順（新しいもの優先）で返す。from_date/to_dateは
    catalyst_dateへのISO日付フィルタ（両端含む）。categoryで絞り込み可能。"""
    pool = _get_pool(database_url)
    if pool is None:
        return []
    where, params = ["user_id = %s"], [user_id]
    if from_date:
        where.append("catalyst_date >= %s")
        params.append(from_date)
    if to_date:
        where.append("catalyst_date <= %s")
        params.append(to_date)
    if category:
        where.append("category = %s")
        params.append(category)
    params.append(limit)
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"SELECT * FROM news_catalysts WHERE {' AND '.join(where)} "
                f"ORDER BY catalyst_date DESC LIMIT %s",
                params,
            )
            return [_row_to_json(r) for r in cur.fetchall()]


def delete_news_catalyst(database_url, user_id, catalyst_id):
    pool = _get_pool(database_url)
    if pool is None:
        return
    with pool.connection() as conn:
        conn.execute("DELETE FROM news_catalysts WHERE user_id = %s AND id = %s", [user_id, catalyst_id])
        conn.commit()


# v3-9続き（2026-09-05・PHASE 5 EXPERT INTELLIGENCE）：expert_views。market_events/news_catalysts
# と同じ「JSON貼り付け→1件ずつupsert」の考え方を踏襲する。重複判定キーはexpert_name・
# source_title・published_at（ユーザー指定）。expert_name・published_atは必須（無ければFalseを
# 返し呼び出し側でカウントしない）。
_EXPERT_VIEW_OPTIONAL_COLS = ["source_url", "source_type", "captured_at", "topic", "time_horizon",
                               "market", "sector", "stocks", "outlook", "confidence",
                               "risk_window_start", "risk_window_end", "thesis", "confirmations",
                               "invalidation_conditions", "key_points", "source_summary",
                               "verification_status", "effective_from", "effective_until", "raw_payload"]
_EXPERT_VIEW_JSONB_COLS = {"stocks", "confirmations", "invalidation_conditions", "key_points", "raw_payload"}


def _upsert_expert_view_conn(conn, user_id, ev):
    """1件の有識者見解dictをupsertする。expert_name・published_atは必須。source_type未指定なら
    OTHER、verification_status未指定ならUNVERIFIED（字幕全文や検証済みでない情報を確定情報
    として扱わない、既定の安全側）。"""
    expert_name = ev.get("expert_name")
    published_at = ev.get("published_at") or ev.get("date")
    if not expert_name or not published_at:
        return False
    source_title = ev.get("source_title")
    cols = ["expert_name", "published_at", "source_title"] + \
        [c for c in _EXPERT_VIEW_OPTIONAL_COLS if c in ev or c in ("source_type", "verification_status")]
    values = []
    for c in cols:
        if c == "expert_name":
            values.append(expert_name)
        elif c == "published_at":
            values.append(published_at)
        elif c == "source_title":
            values.append(source_title)
        elif c == "source_type":
            values.append(ev.get("source_type") or "OTHER")
        elif c == "verification_status":
            values.append(ev.get("verification_status") or "UNVERIFIED")
        elif c in _EXPERT_VIEW_JSONB_COLS:
            values.append(json.dumps(ev.get(c), ensure_ascii=False) if ev.get(c) is not None else None)
        else:
            values.append(ev.get(c))
    update_cols = [c for c in cols if c not in ("expert_name", "published_at", "source_title")]
    conn.execute(
        f"INSERT INTO expert_views (user_id, {', '.join(cols)}) "
        f"VALUES (%s, {', '.join(['%s::jsonb' if c in _EXPERT_VIEW_JSONB_COLS else '%s' for c in cols])}) "
        f"ON CONFLICT (user_id, expert_name, source_title, published_at) DO UPDATE SET "
        f"{', '.join(c + ' = EXCLUDED.' + c for c in update_cols)}, updated_at = now()",
        [user_id] + values,
    )
    return True


def import_expert_views(database_url, user_id, views):
    """views（dictのリスト、JSON貼り付けのimport想定）を1件ずつupsertする。
    戻り値: {"imported": N, "skipped": M}（expert_name/published_atが無い行はskip）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return {"imported": 0, "skipped": len(views)}
    imported = skipped = 0
    with pool.connection() as conn:
        for ev in views:
            if not isinstance(ev, dict):
                skipped += 1
                continue
            ok = _upsert_expert_view_conn(conn, user_id, ev)
            if ok:
                imported += 1
            else:
                skipped += 1
        conn.commit()
    return {"imported": imported, "skipped": skipped}


def list_expert_views(database_url, user_id, expert=None, from_date=None, to_date=None, market=None, stock=None, limit=200):
    """user_idの有識者見解をpublished_at降順（新しいもの優先）で返す。expert=有識者名、
    from_date/to_date=published_atのISO日付フィルタ（両端含む）、market=市場、
    stock=stocks配列に含まれる銘柄コードで絞り込み可能。"""
    pool = _get_pool(database_url)
    if pool is None:
        return []
    where, params = ["user_id = %s"], [user_id]
    if expert:
        where.append("expert_name = %s")
        params.append(expert)
    if from_date:
        where.append("published_at >= %s")
        params.append(from_date)
    if to_date:
        where.append("published_at <= %s")
        params.append(to_date)
    if market:
        where.append("market = %s")
        params.append(market)
    if stock:
        where.append("stocks ? %s")
        params.append(stock)
    params.append(limit)
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"SELECT * FROM expert_views WHERE {' AND '.join(where)} "
                f"ORDER BY published_at DESC LIMIT %s",
                params,
            )
            return [_row_to_json(r) for r in cur.fetchall()]


def delete_expert_view(database_url, user_id, view_id):
    pool = _get_pool(database_url)
    if pool is None:
        return
    with pool.connection() as conn:
        conn.execute("DELETE FROM expert_views WHERE user_id = %s AND id = %s", [user_id, view_id])
        conn.commit()


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
                   "initial_stop", "current_stop", "target_1", "target_2",
                   "trade_style"]  # marketはINSERT文で別途固定列として扱うためここには含めない


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


# ---- 監視銘柄→ポジション連携・売買損益管理（2026-09-07新規） ----
# 既存のportfolio（保有株、user_idスコープ済み）をそのまま使い、quantity/average_priceを
# 「現在の合算値」のSSoTとして維持しつつ、買い増しのたびにentries（追記専用の来歴）を積む。
# 売却はtrade_history（新設）へ1行記録し、全株売却でportfolioの行自体を削除する
# （trade_historyは削除しない＝取引履歴は消さない、というユーザー指示）。

def add_position_entry(database_url, user_id, code, name, market, price, shares, trade_style=None):
    """買い/買い増し。既存ポジション（同一user_id・code・market）があれば加重平均で合算し、
    無ければ新規作成する。price/sharesは正の数であることをここでも確認する（不正な値は保存
    しない）。戻り値：更新後のportfolio 1行（dict）、または失敗時None。"""
    pool = _get_pool(database_url)
    if pool is None:
        return None
    try:
        price = float(price)
        shares = float(shares)
    except (TypeError, ValueError):
        return None
    if not code or price <= 0 or shares <= 0:
        return None
    market = market or "JP"
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    entry = {"price": price, "shares": shares, "timestamp": now_iso}
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM portfolio WHERE user_id = %s AND code = %s AND market = %s AND active = true",
                [user_id, code, market],
            )
            row = cur.fetchone()
        if row:
            old_qty = float(row["quantity"] or 0)
            old_avg = float(row["average_price"] or 0)
            new_qty = old_qty + shares
            new_avg = ((old_avg * old_qty) + (price * shares)) / new_qty if new_qty else price
            entries = list(row.get("entries") or []) + [entry]
            conn.execute(
                "UPDATE portfolio SET quantity = %s, average_price = %s, entries = %s::jsonb, updated_at = now() "
                "WHERE user_id = %s AND code = %s AND market = %s",
                [new_qty, new_avg, json.dumps(entries, ensure_ascii=False), user_id, code, market],
            )
        else:
            conn.execute(
                "INSERT INTO portfolio (user_id, code, name, market, quantity, average_price, "
                "trade_style, entries, active, acquired_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, true, now())",
                [user_id, code, name, market, shares, price, trade_style, json.dumps([entry], ensure_ascii=False)],
            )
        conn.commit()
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM portfolio WHERE user_id = %s AND code = %s AND market = %s", [user_id, code, market])
            updated = cur.fetchone()
    if not updated:
        return None
    d = _row_to_json(updated)
    d.pop("user_id", None)
    return d


# 2026-09-07新規（通算実現損益を税引後ベースへ変更）：上場株式の譲渡益にかかる税率
# （所得税・復興特別所得税15.315% + 住民税5.000% = 20.315%）。バックエンド（ここ、実際の
# trade_history保存時の権威ある計算）とフロントエンド（trade-cockpit.htmlの売却確認画面での
# プレビュー表示）で別々にベタ書きせず、値としてはこの1か所をSSoTとする。フロント側は
# 別言語のため同じ変数を共有できないが、同名・同値の定数として複製し、コメントで
# 「ここと同期させること」と明記している（trade-cockpit.html内STOCK_CAPITAL_GAINS_TAX_RATE
# 参照）。今回は年間の損益通算等を再現する複雑な税務エンジンにはせず、1トレードごとに
# 「利益なら20.315%控除・損失ならそのまま」という単純な近似計算のみ行う（ユーザー指示）。
STOCK_CAPITAL_GAINS_TAX_RATE = 0.20315


def _calc_trade_tax(gross_pnl):
    """1トレードの税引前実現損益(gross_pnl)から、概算の税額・税引後損益を返す。
    利益の場合のみ税率を掛け、円未満は切り捨てる（他の金額表示との丸め方針との整合を優先。
    このアプリの他の金額表示は円未満を四捨五入/切り捨てで概算表示しており、明確な統一ルールは
    無かったため、税額は「実際に源泉徴収される額を上回って表示しない」安全側に倒し切り捨てを
    採用した）。損失の場合は追加で税を引かない（0円）。"""
    if gross_pnl > 0:
        tax = math.floor(gross_pnl * STOCK_CAPITAL_GAINS_TAX_RATE)
    else:
        tax = 0
    return tax, gross_pnl - tax


def add_position_exit(database_url, user_id, code, market, exit_price, shares):
    """売却確定。(exit_price - average_price) * sharesを実現損益としてtrade_historyへ1行記録
    する。一部売却の場合、残った建玉のaverage_priceは変更しない（ユーザー指示）。残り枚数が
    0以下ならportfolioの行を削除する（trade_historyは削除しない）。保有枚数を超える売却・
    0以下の売値/枚数は拒否する。戻り値：{"trade":{...},"remainingShares":..,"closed":bool}
    または{"error":...}。"""
    pool = _get_pool(database_url)
    if pool is None:
        return {"error": "DB未設定（DATABASE_URLが未設定、またはpsycopg未インストール）"}
    try:
        exit_price = float(exit_price)
        shares = float(shares)
    except (TypeError, ValueError):
        return {"error": "売値・売却枚数は数値で指定してください"}
    if exit_price <= 0 or shares <= 0:
        return {"error": "売値・売却枚数は正の数で指定してください"}
    market = market or "JP"
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM portfolio WHERE user_id = %s AND code = %s AND market = %s AND active = true",
                [user_id, code, market],
            )
            row = cur.fetchone()
        if not row:
            return {"error": "保有ポジションが見つかりません"}
        remaining = float(row["quantity"] or 0)
        if shares > remaining + 1e-9:
            return {"error": f"保有枚数（{remaining:g}株）を超える売却はできません"}
        avg_price = float(row["average_price"] or 0)
        pnl = (exit_price - avg_price) * shares  # 税引前（gross）。既存pnl列は意味を変えず維持する。
        tax, net_pnl = _calc_trade_tax(pnl)
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "INSERT INTO trade_history (user_id, code, name, market, entry_price, exit_price, shares, "
                "pnl, gross_pnl, tax, net_pnl) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
                [user_id, code, row.get("name"), market, avg_price, exit_price, shares, pnl, pnl, tax, net_pnl],
            )
            trade = cur.fetchone()
        new_remaining = remaining - shares
        closed = new_remaining <= 1e-9
        if closed:
            conn.execute("DELETE FROM portfolio WHERE user_id = %s AND code = %s AND market = %s", [user_id, code, market])
        else:
            conn.execute(
                "UPDATE portfolio SET quantity = %s, updated_at = now() WHERE user_id = %s AND code = %s AND market = %s",
                [new_remaining, user_id, code, market],
            )
        conn.commit()
    trade_json = _row_to_json(trade)
    trade_json.pop("user_id", None)
    return {"trade": trade_json, "remainingShares": 0 if closed else new_remaining, "closed": closed}


def list_trade_history(database_url, user_id, limit=200):
    pool = _get_pool(database_url)
    if pool is None:
        return []
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM trade_history WHERE user_id = %s ORDER BY closed_at DESC LIMIT %s",
                [user_id, limit],
            )
            out = []
            for r in cur.fetchall():
                d = _row_to_json(r)
                d.pop("user_id", None)
                out.append(d)
            return out


def get_investment_totals(database_url, user_id):
    """通算実現損益（税引後） = investment_profile.initial_realized_pnl（ユーザーが税引後の
    値として入力している前提、ここでは追加の税計算を一切かけない） + trade_history各行の
    税引後損益(net_pnl)の合計。net_pnlが無い行（税引後対応前の旧レコード、2026-09-07時点の
    本番では0件）はCOALESCEで既存pnl（税引前のまま）を暫定的に使う（過去データを勝手に
    書き換えるmigrationは行わない方針のため）。保存済みの合計値をキャッシュせず毎回
    再計算する（データ破損防止、ユーザー指示）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return {"initialRealizedPnl": 0.0, "totalRealizedPnl": 0.0}
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT initial_realized_pnl FROM investment_profile WHERE user_id = %s", [user_id])
            row = cur.fetchone()
            initial = float(row["initial_realized_pnl"]) if row and row.get("initial_realized_pnl") is not None else 0.0
            cur.execute("SELECT COALESCE(SUM(COALESCE(net_pnl, pnl)), 0) AS total FROM trade_history WHERE user_id = %s", [user_id])
            trades_sum = float(cur.fetchone()["total"] or 0)
    return {"initialRealizedPnl": initial, "totalRealizedPnl": initial + trades_sum}


def set_initial_realized_pnl(database_url, user_id, value):
    pool = _get_pool(database_url)
    if pool is None:
        return
    try:
        value = float(value)
    except (TypeError, ValueError):
        return
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO investment_profile (user_id, initial_realized_pnl, updated_at) VALUES (%s, %s, now()) "
            "ON CONFLICT (user_id) DO UPDATE SET initial_realized_pnl = EXCLUDED.initial_realized_pnl, updated_at = now()",
            [user_id, value],
        )
        conn.commit()
