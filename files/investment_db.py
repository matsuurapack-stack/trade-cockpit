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

# Phase MU-S1（2026-09-14・SHARED/PRIVATE再分類）：市場分析系の一部テーブル
# （watchlist / market_events / news_catalysts / expert_views / stock_theses /
# market_intelligence_reports）は全ユーザー共有にする。2026-09-02の一律user_id分離で
# これらも個人ごとに分断されてしまっていたための修正（指示書：ポジション/トレード等の
# 個人情報は現状のuser_idスコープを維持し、市場分析だけを共有に戻す）。
# 呼び出し元（server.py）から渡されるuser_idは意図的に無視し、この固定値を使う——
# 関数シグネチャ・呼び出し側は変更しない。将来ユーザーが増えても同じ値を参照する。
# user_id列自体は削除しない（将来のscope再設計・ロールバックに備える）。
_SHARED_SCOPE = "_shared"

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

-- Smart Import「監視銘柄更新」（type: watchlist_master_update）対応：SBI証券等の監視銘柄
-- マスターを一括反映する際、update_mode="sync"でマスター側に存在しなくなった既存銘柄を
-- 即削除せず区別して残すための列。active（表示/非表示の絶対フラグ）とは別軸——
-- inactive_candidateはあくまで「次回同期でも見当たらなかった」という注記であり、
-- ユーザーが手動で判断するまで監視銘柄一覧からは消さない。
ALTER TABLE watchlist ADD COLUMN IF NOT EXISTS inactive_candidate BOOLEAN NOT NULL DEFAULT false;

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

-- 2026-09-14新規（日次振り返りの持ち越し誤判定バグ対応）：全株売却でportfolioの行自体が
-- 削除される（上のコメント参照）ため、これまではtrade_historyに「いつ取得したポジション
-- だったか」「デイトレ想定だったか」が一切残らず、generate_daily_review()が review_date
-- 大引け時点の保有状態を過去に遡って再構成できなかった（=list_portfolio()の「今この瞬間」
-- のactiveな行だけしか見られず、その日のうちに手仕舞って既に削除済みの建玉は「持ち越して
-- いなかった」ことを証明する手段が無かった）。売却確定時点でportfolio行からacquired_at・
-- trade_styleを複製して残すことで、後からでも「その建玉は review_date の大引けより前に
-- 手仕舞われていたか」を機械的に判定できるようにする。
ALTER TABLE trade_history ADD COLUMN IF NOT EXISTS acquired_at TIMESTAMPTZ;
ALTER TABLE trade_history ADD COLUMN IF NOT EXISTS trade_style TEXT;

-- 2026-09-08新規（ニュース・材料連携の改善）：news_catalystsに好材料/悪材料/中立の方向性
-- （sentiment）を追加する。既存のcategory（分類）・importance（重要度）とは別軸で、
-- 「positive|negative|neutral」のいずれか。既存importで未指定の行はNULL（判定不能）のまま
-- 残す＝無理にpositive/negativeへ寄せない（ユーザー指定：確信が無ければneutral扱いにする
-- 判定ロジックはフロント/import時のヘルパー側が担う。ここではNULL可の列を追加するのみ）。
ALTER TABLE news_catalysts ADD COLUMN IF NOT EXISTS sentiment TEXT;

-- 2026-09-16新規（Trade Learning Phase B：STOP LOSS記録のデータ欠損修正）：ポジション決済
-- （add_position_exit）時、portfolio.initial_stop/current_stopは既にSELECTで取得済みなのに
-- trade_historyへのINSERT列に含まれておらず、決済と同時にSTOP情報が消えていた（6227
-- AIメカテック実例の調査で判明）。ここではUI変更なしでデータパスのみ塞ぐ：
--   initial_stop_price = 決済直前のportfolio.initial_stop（登録時に決めた当初の損切り値）
--   final_stop_price   = 決済直前のportfolio.current_stop（トレーリング等で更新された最新の損切り値）
-- stop_reason_category/textはportfolio側（登録・編集時にanalyze_stock().stopReasonCategoryを
-- 由来として保存、無ければMANUAL/UNKNOWN）から同様に引き継ぐ。stop_quality_evidenceは
-- 決済時にinitial_stop_priceの有無から機械的に判定する（ACTUAL_STOP|UNKNOWN）。
-- 過去トレード（6227含む）は遡って埋め戻さない＝NULL（UNKNOWN扱い）のまま残す（推測禁止、ユーザー指示）。
-- stop_history（変更履歴の時系列保存）はPhase Bでは実装しない（INITIAL→FINALの2点保存まで、Phase B2で検討）。
ALTER TABLE trade_history ADD COLUMN IF NOT EXISTS initial_stop_price NUMERIC;
ALTER TABLE trade_history ADD COLUMN IF NOT EXISTS final_stop_price NUMERIC;
ALTER TABLE trade_history ADD COLUMN IF NOT EXISTS stop_reason_category TEXT;
ALTER TABLE trade_history ADD COLUMN IF NOT EXISTS stop_reason_text TEXT;
ALTER TABLE trade_history ADD COLUMN IF NOT EXISTS stop_quality_evidence TEXT;

-- 同じくPhase B：portfolio側にも登録・編集時点のSTOP根拠を保持する列を足す（決済時に
-- trade_historyへ複製して引き継ぐための発生源。既存initial_stop/current_stopの値そのものは
-- 変更しない、根拠の分類・自由文のみ追加）。
ALTER TABLE portfolio ADD COLUMN IF NOT EXISTS stop_reason_category TEXT;
ALTER TABLE portfolio ADD COLUMN IF NOT EXISTS stop_reason_text TEXT;

-- 2026-09-16新規（Trade Learning Phase C：ENTRY/STOP/EXIT/REENTRY QUALITY 4軸独立評価）：
-- 6227 AIメカテック実例を基準ケースとし、「損切り後に上がった＝STOPが悪かった」という
-- 結果論の一括学習を避けるため、ENTRY判断のタイミング・STOP幅の構造的妥当性（実STOP記録
-- ありのトレードのみ、無ければUNKNOWN）・EXIT執行の計画遵守・EXIT後のTRIGGER検出可否を
-- 完全に独立した4つの軸として保存する（server.py evaluate_trade_quality_axes()）。
-- 既存execution_score/entry_avoidability/loss_reason_tags等は無変更、別枠のJSONB列として追加
-- するのみ。本Phaseは観測・保存までで、ENTRY SCOREのweight変更・自動ルール昇格・STOP幅の
-- 自動変更には一切接続しない。
ALTER TABLE trade_experiences ADD COLUMN IF NOT EXISTS quality_axes_json JSONB;
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

# Phase MU-S2（2026-09-14・GLOBAL/USER可視性分離）：trade_rules / trade_playbooksに
# 「誰から見えるか」の可視性列を追加する。既存の`scope`列（trade_rulesのみ）は
# _guess_rule_scope()が判定する「対象範囲」（stock|sector|market_condition|global）という
# 全く別の意味の列であり、意味が食い違うため絶対に再利用しない（列名は別にする）。
#   visibility='USER'   … current_userでスコープ（デフォルト。既存行は全てこのまま）
#   visibility='GLOBAL' … _SHARED_SCOPEでスコープ（全ユーザー共通）
# 既存データは安全側に倒し、このmigrationでは自動でGLOBALへ移行しない（新規列追加のみ、
# 既存23件のtrade_rulesは全てUSERのまま）。GLOBAL移行は人間が確認した候補一覧を見てから
# 別途手動で実施する（指示書：個別銘柄・具体的な売買失敗・個人インシデント由来のルールは
# 一般化処理を通るまでGLOBALにしない）。
# trade_rule_historyは自身に可視性列を持たず、親trade_rules.visibility（rule_id経由）に
# 従属する（現状の列にはPnL等の個人財務情報が無いため、この方針で十分。将来PRIVATE評価情報を
# 追加する場合はGLOBAL定義履歴とUSER評価履歴のテーブル分離を優先すること）。
_MIGRATE_RULE_VISIBILITY_SQL = """
ALTER TABLE trade_rules ADD COLUMN IF NOT EXISTS visibility TEXT NOT NULL DEFAULT 'USER';
ALTER TABLE trade_playbooks ADD COLUMN IF NOT EXISTS visibility TEXT NOT NULL DEFAULT 'USER';
CREATE INDEX IF NOT EXISTS idx_trade_rules_visibility ON trade_rules(visibility);
CREATE INDEX IF NOT EXISTS idx_trade_playbooks_visibility ON trade_playbooks(visibility);
"""

# ============================================================
# Trade Experience Learning（2026-09-11新規）：日々の実トレードから、ユーザー固有の
# 「勝ちパターン・負けパターン・WAIT条件・利確条件」を蓄積し、ENTRY TOP5・トレード分析・
# ルール評価・日中レポートへ反映するための学習基盤。指示書20番：既存機能（stock_theses・
# ENTRY TOP5・applied rules・expert views・intraday reports・daily review・smart import・
# ChatGPT共有JSON）の上に追加する補助レイヤーであり、既存スコアへは介入しない。
# ACTIVEルールへの自動昇格はしない——RULE_CANDIDATE状態はtrade_rules.statusの新しい値として
# 追加するのみで、新規テーブルは作らない（既存の昇格フロー・trade_rule_historyをそのまま
# 再利用する、指示書9・20番）。
# ============================================================

_SCHEMA_TRADE_EXPERIENCES_SQL = """
CREATE TABLE IF NOT EXISTS trade_experiences (
    id                            SERIAL PRIMARY KEY,
    user_id                       TEXT NOT NULL,
    trade_date                    DATE NOT NULL,
    symbol                        TEXT NOT NULL,
    stock_name                    TEXT,
    side                          TEXT NOT NULL DEFAULT 'BUY',   -- BUY|SELL
    trade_style                   TEXT,                           -- DAY|SWING
    quantity                      NUMERIC,
    entry_price                   NUMERIC,
    exit_price                    NUMERIC,
    entry_time                    TIMESTAMPTZ,
    exit_time                     TIMESTAMPTZ,
    gross_pnl                     NUMERIC,
    gross_pnl_pct                  NUMERIC,
    holding_minutes                 INTEGER,
    pre_entry_state                  TEXT,   -- WAIT/ENTRY_READY/BREAKOUT/REVERSAL等
    wait_reason_json                   JSONB,
    entry_reason_json                    JSONB,
    exit_reason_json                       JSONB,
    invalidation_reason_json                 JSONB,
    market_condition                           TEXT,
    sector_condition                             TEXT,
    nikkei_change_pct                             NUMERIC,
    relative_strength                              NUMERIC,
    rsi_at_entry                                    NUMERIC,
    rsi_at_exit                                      NUMERIC,
    short_ma                                          NUMERIC,
    mid_ma                                             NUMERIC,
    long_ma                                             NUMERIC,
    volume_ratio                                         NUMERIC,
    intraday_low                                          NUMERIC,
    intraday_high                                          NUMERIC,
    distance_from_low_pct                                   NUMERIC,
    distance_from_high_pct                                   NUMERIC,
    volatility_score                                          NUMERIC,
    pattern_tags_json                                          JSONB,
    execution_score                                             NUMERIC,   -- 1-100
    rule_compliance_score                                        NUMERIC,   -- 1-100
    result_class                                                  TEXT,      -- WIN|LOSS|BREAKEVEN
    max_favorable_excursion_pct                                    NUMERIC,
    max_adverse_excursion_pct                                       NUMERIC,
    post_exit_max_price                                              NUMERIC,
    post_exit_min_price                                               NUMERIC,
    profit_capture_ratio                                               NUMERIC,  -- 指示書10番：参考値、評価点には強く効かせない
    learning_status                                                     TEXT NOT NULL DEFAULT 'PENDING', -- PENDING|VALIDATED|EXCLUDED
    learning_weight                                                      NUMERIC NOT NULL DEFAULT 1.0,    -- 0.0〜1.0
    score_breakdown_json                                                  JSONB,  -- 指示書15番：採点根拠（恣意的な点数を禁止）
    decision_snapshot_json                                                 JSONB, -- 指示書16番：ENTRY時点で分かっていたことだけ
    post_trade_analysis_json                                                JSONB, -- 指示書16番：EXIT後に判明した情報はここのみ
    notes                                                                    TEXT,
    created_at                                                               TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                                                                TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_trade_experiences_user_date ON trade_experiences(user_id, trade_date DESC);
CREATE INDEX IF NOT EXISTS idx_trade_experiences_symbol ON trade_experiences(user_id, symbol);

CREATE TABLE IF NOT EXISTS trade_decision_events (
    id                          SERIAL PRIMARY KEY,
    user_id                     TEXT NOT NULL,
    trade_experience_id         INTEGER REFERENCES trade_experiences(id) ON DELETE SET NULL,
    event_time                  TIMESTAMPTZ NOT NULL,
    symbol                      TEXT NOT NULL,
    decision_type               TEXT NOT NULL,  -- WAIT|ENTRY_READY|ENTRY|HOLD|EXIT_READY|EXIT|INVALIDATED
    price                       NUMERIC,
    reason_json                 JSONB,
    technical_snapshot_json     JSONB,
    market_snapshot_json        JSONB,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_trade_decision_events_trade ON trade_decision_events(trade_experience_id, event_time);
CREATE INDEX IF NOT EXISTS idx_trade_decision_events_symbol ON trade_decision_events(user_id, symbol, event_time DESC);
"""

# 「今日の振り返り」独立タブ化 + 15:30自動評価 + トレード経験/銘柄クセ学習（2026-09-12新規）。
# ・sync_key：15:30スケジューラ・手動再生成・再起動・retryが重なっても同一トレード/WAITを
#   重複登録しないための冪等キー（指示書29番）。実トレードは'trade:<trade_history.id>'、
#   WAIT成功/失敗の記録は'wait:<symbol>:<trade_date>:<event_time>'の形にする（呼び出し側で
#   組み立てる、ここではUNIQUE制約を持つ列を追加するだけ）。
# ・decision_quality_score/trade_result_score：「判断品質」と「結果」を分けて保存する
#   （指示書6番）。既存のexecution_score（0-100、既存のトレード実行そのものの評価）とは
#   別軸——decision_quality_scoreは主に日次振り返りの5軸スコアから、trade_result_scoreは
#   純粋なP/Lの符号・比率だけから機械的に算出する（結果の良し悪しで判断の評価を歪めない）。
_SCHEMA_TRADE_EXPERIENCES_LEARNING_SQL = """
ALTER TABLE trade_experiences ADD COLUMN IF NOT EXISTS sync_key TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS uq_trade_experiences_sync_key
    ON trade_experiences(user_id, sync_key) WHERE sync_key IS NOT NULL;
ALTER TABLE trade_experiences ADD COLUMN IF NOT EXISTS decision_quality_score NUMERIC;
ALTER TABLE trade_experiences ADD COLUMN IF NOT EXISTS trade_result_score NUMERIC;

-- 銘柄ごとの「クセ」を統計的に学習するプロフィール（指示書14・15番）。文章ではなく統計値から
-- 作る。sample_count<5はLOW、5-14はMEDIUM、15+はHIGH（既存Trade Experience Learningの
-- classify_pattern_confidence()と同じ閾値・同じ考え方を流用する、二重の基準を作らない）。
CREATE TABLE IF NOT EXISTS stock_behavior_profiles (
    id                          SERIAL PRIMARY KEY,
    user_id                     TEXT NOT NULL,
    symbol                      TEXT NOT NULL,
    stock_name                  TEXT,
    sample_count                INTEGER NOT NULL DEFAULT 0,
    avg_intraday_range_pct      NUMERIC,
    gap_up_frequency            NUMERIC,
    gap_down_frequency          NUMERIC,
    opening_30m_strength_rate   NUMERIC,
    morning_high_break_rate     NUMERIC,
    afternoon_high_break_rate   NUMERIC,
    afternoon_reversal_rate     NUMERIC,
    vwap_reclaim_success_rate   NUMERIC,
    vwap_loss_failure_rate      NUMERIC,
    oversold_reversal_rate      NUMERIC,
    breakout_followthrough_rate NUMERIC,
    breakout_failure_rate       NUMERIC,
    pullback_success_rate       NUMERIC,
    late_day_momentum_rate      NUMERIC,
    late_day_fade_rate          NUMERIC,
    overnight_win_rate          NUMERIC,
    overnight_gap_down_rate     NUMERIC,
    avg_mfe_pct                 NUMERIC,
    avg_mae_pct                 NUMERIC,
    best_entry_time_bucket      TEXT,
    worst_entry_time_bucket     TEXT,
    time_bucket_stats_json      JSONB,  -- 指示書16番：09:00-09:30等、時間帯別の勝率・反転率
    preferred_setup_json        JSONB,  -- 指示書17番：BREAKOUT/PULLBACK/VWAP_RECLAIM等セットアップ別の件数・勝率
    danger_patterns_json        JSONB,
    confidence_level            TEXT NOT NULL DEFAULT 'LOW',  -- LOW|MEDIUM|HIGH
    last_updated                TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, symbol)
);
CREATE INDEX IF NOT EXISTS idx_stock_behavior_profiles_user ON stock_behavior_profiles(user_id, symbol);

-- daily_reviews側にも同じ「判断品質」と「結果」の分離を追加する（指示書6・7番）。
-- 既存score_total（process quality寄り、既存の各score_*の合計）は無変更のまま残す。
ALTER TABLE daily_reviews ADD COLUMN IF NOT EXISTS decision_quality_score NUMERIC;
ALTER TABLE daily_reviews ADD COLUMN IF NOT EXISTS trade_result_score NUMERIC;
-- 15:30自動評価スケジューラ（指示書4・11・28番）の状態管理。1日1回の確定生成を記録し、
-- 冪等性の最終防衛線（daily_reviews.(user_id,review_date)のUNIQUE制約）に加えて、
-- 「まだ確定していない（リトライ中）」と「確定済み」を区別できるようにする。
ALTER TABLE daily_reviews ADD COLUMN IF NOT EXISTS is_finalized BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE daily_reviews ADD COLUMN IF NOT EXISTS generation_attempts INTEGER NOT NULL DEFAULT 0;
"""

# 2026-09-09新規（日次投資レビュー・投資スコア）：daily_reviews。
_SCHEMA_DAILY_REVIEWS_SQL = """
CREATE TABLE IF NOT EXISTS daily_reviews (
    id                    SERIAL PRIMARY KEY,
    user_id               TEXT NOT NULL,
    review_date           TEXT NOT NULL,
    score_total           INTEGER,
    score_rule_adherence  INTEGER,
    score_entry_quality   INTEGER,
    score_exit_quality    INTEGER,
    score_market_fit      INTEGER,
    score_risk_mgmt       INTEGER,
    score_reflection      INTEGER,
    good_points           JSONB,
    improvement_points    JSONB,
    tomorrow_notes        JSONB,
    auto_summary          TEXT,
    user_feedback         TEXT,
    reflection_tags       JSONB,
    generated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, review_date)
);
CREATE INDEX IF NOT EXISTS idx_daily_reviews_user_date ON daily_reviews(user_id, review_date DESC);
"""

# 2026-09-09新規（判断エンジン強化：知識を実際の分析へ使う、指示書1・2・6・24番）。
_SCHEMA_KNOWLEDGE_ENGINE_SQL = """
-- 指示書1・2番：有識者見解の自動評価・信頼度学習用の列を追加（既存expert_viewsを拡張、
-- 新規テーブルは作らない）。
ALTER TABLE expert_views ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'ACTIVE';
ALTER TABLE expert_views ADD COLUMN IF NOT EXISTS supported_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE expert_views ADD COLUMN IF NOT EXISTS failed_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE expert_views ADD COLUMN IF NOT EXISTS neutral_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE expert_views ADD COLUMN IF NOT EXISTS last_evaluated_at TIMESTAMPTZ;
ALTER TABLE expert_views ADD COLUMN IF NOT EXISTS accuracy_score NUMERIC;
-- 指示書2番「分野別精度」：expert_name+topicの組み合わせ単位で正答率を積み上げられるよう、
-- topic列（既存）をそのまま分野キーとして使う（新規列は増やさない）。

CREATE TABLE IF NOT EXISTS trade_playbooks (
    id                          SERIAL PRIMARY KEY,
    user_id                     TEXT NOT NULL,
    playbook_key                TEXT NOT NULL,  -- 正規化キー（trade_rulesのrule_keyと同じ考え方、重複防止用）
    name                        TEXT NOT NULL,
    source_trader                TEXT,
    source_title                TEXT,
    source_type                  TEXT,
    market_type                  TEXT,   -- JP|US等
    timeframe                    TEXT,   -- DAYTRADE|SWING|POSITION等
    applicable_sectors_json      JSONB,
    applicable_stocks_json       JSONB,
    entry_conditions_json        JSONB,
    confirmation_conditions_json JSONB,
    exit_conditions_json         JSONB,
    stop_conditions_json         JSONB,
    avoid_conditions_json        JSONB,
    position_management_json     JSONB,
    status                      TEXT NOT NULL DEFAULT 'TESTING',  -- TESTING|ACTIVE|REVISED|RETIRED
    confidence                  TEXT NOT NULL DEFAULT 'LOW',
    evidence_count               INTEGER NOT NULL DEFAULT 0,
    success_count                INTEGER NOT NULL DEFAULT 0,
    failure_count                INTEGER NOT NULL DEFAULT 0,
    neutral_count                INTEGER NOT NULL DEFAULT 0,
    user_attempt_count            INTEGER NOT NULL DEFAULT 0,
    user_success_count            INTEGER NOT NULL DEFAULT 0,
    user_failure_count            INTEGER NOT NULL DEFAULT 0,
    user_avg_return               NUMERIC,
    user_compatibility_score      NUMERIC,
    created_from                 TEXT,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, playbook_key)
);
CREATE INDEX IF NOT EXISTS idx_trade_playbooks_user_status ON trade_playbooks(user_id, status);

-- 指示書24・26番：各分析実行時に「何を参照したか」を残し、実際の売買・日次レビューで
-- 「知っていたのに無視した」を検出できるようにする。
CREATE TABLE IF NOT EXISTS analysis_context_log (
    id                SERIAL PRIMARY KEY,
    user_id           TEXT NOT NULL,
    code              TEXT,
    analysis_type     TEXT,   -- morning|stock|top5|position|exit_consult等
    analysis_date     TEXT NOT NULL,
    judgment          TEXT,   -- BUY_CANDIDATE|WAIT|TAKE_PROFIT|RAISE_STOP|REDUCE|EXIT|NO_OVERNIGHT等
    confidence_score  INTEGER,
    reasons_json      JSONB,
    used_context_json JSONB,  -- {"rules":[...],"expert_views":[...],"events":[...],"news":[...],
                               --  "playbooks":[...],"similar_trades":[...],"reflections":[...]}
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_analysis_context_log_user_date ON analysis_context_log(user_id, analysis_date DESC);
CREATE INDEX IF NOT EXISTS idx_analysis_context_log_user_code ON analysis_context_log(user_id, code, analysis_date DESC);

-- 指示書26番：日次レビューへ「知っていたのに無視した」件数を残す列を追加。
ALTER TABLE daily_reviews ADD COLUMN IF NOT EXISTS known_risk_ignored_json JSONB;
"""

# 2026-09-10新規（朝一マーケット自動分析システム、MorningMarketCheck）。
_SCHEMA_MORNING_CHECK_SQL = """
CREATE TABLE IF NOT EXISTS morning_market_checks (
    id                   SERIAL PRIMARY KEY,
    user_id              TEXT NOT NULL,
    check_date           TEXT NOT NULL,
    snapshot_time        TEXT NOT NULL,  -- T0530|T0700|T0800|T0830|T0850|MANUAL
    generated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    market_regime        TEXT,
    volatility_regime    TEXT,
    trend_type           TEXT,
    market_risk_score    INTEGER,
    volatility_score     INTEGER,
    trend_score          INTEGER,
    macro_pressure_score INTEGER,
    indices_json         JSONB,
    fx_json              JSONB,
    commodities_json     JSONB,
    adr_json             JSONB,
    data_quality_json    JSONB,
    strong_sectors_json  JSONB,
    weak_sectors_json    JSONB,
    watchlist_top5_json  JSONB,
    avoid_stocks_json    JSONB,
    resilience_json      JSONB,
    risk_warnings_json   JSONB,
    event_risk_json      JSONB,
    position_risk_json   JSONB,
    strategy_json        JSONB,
    strategy_text        TEXT,
    raw_payload_json     JSONB,
    is_read              BOOLEAN NOT NULL DEFAULT false,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, check_date, snapshot_time)
);
CREATE INDEX IF NOT EXISTS idx_morning_checks_user_date ON morning_market_checks(user_id, check_date DESC, generated_at DESC);
"""

# 2026-09-10新規（損切りルール是正・最優先修正）：ポジション損切りルールを1箇所で一元管理する
# 共通設定。旧SWING限定の-8%接近警告/-10%強制ハードストップ（trade-cockpit.htmlの
# calcHardStop/hardStopState、server.pyのHARD_STOP_APPROACHING_PCT/HARD_STOP_TRIGGER_PCT）は
# ユーザーの実際の運用ルールと一致していなかったため、ここを唯一の真実（single source of
# truth）として全トレードスタイル共通の-6%WATCH/-7%WARNING/-8%EXITへ是正する。
# 重要：既存trade_rulesの"SWING_STOP_LOSS"（"スイングは-10%で損切り"、CRITICAL・保護対象
# ルール）は削除・書き換えしない（過去の学習履歴・保護機構への影響を避けるため）。
# あくまで「実際の判定に使う値」をこの新しいテーブルへ一本化するだけで、trade_rulesの
# 学習系（evidence/confidence等）とは別物として扱う——ハードな業務ルール（閾値固定・
# 行動固定）と、証拠を積み重ねて信頼度が変化する学習系ルールは性質が違うため、あえて
# 混在させない設計にした。
DEFAULT_POSITION_RISK_RULES = {
    "watch_pct": -6.0, "warning_pct": -7.0, "max_loss_pct": -8.0,
    "action": "EXIT", "allow_reentry": True, "reentry_requires_new_decision": True,
}
_SCHEMA_POSITION_RISK_RULES_SQL = """
CREATE TABLE IF NOT EXISTS position_risk_rules (
    user_id                        TEXT PRIMARY KEY,
    watch_pct                      NUMERIC NOT NULL DEFAULT -6.0,
    warning_pct                    NUMERIC NOT NULL DEFAULT -7.0,
    max_loss_pct                   NUMERIC NOT NULL DEFAULT -8.0,
    action                         TEXT NOT NULL DEFAULT 'EXIT',
    allow_reentry                  BOOLEAN NOT NULL DEFAULT true,
    reentry_requires_new_decision  BOOLEAN NOT NULL DEFAULT true,
    updated_at                     TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

# 2026-09-10新規（Market Intelligence Timeline、Phase2-A）：MorningMarketCheckを起点に
# 場中の定時レポート（寄り30分/前場終了/後場30分/大引け）を1本のタイムラインとして
# 蓄積する。Phase2-Aでは09:30 OPENING_30Mのみ実装し、テーブル自体は他report_typeも
# 受け入れられる汎用スキーマにしておく（Phase2-Bで11:30/13:00/15:30を追加する際に
# テーブル変更が不要なようにするため）。
_SCHEMA_MARKET_INTELLIGENCE_SQL = """
CREATE TABLE IF NOT EXISTS market_intelligence_reports (
    id                          SERIAL PRIMARY KEY,
    user_id                     TEXT NOT NULL,
    trade_date                  TEXT NOT NULL,
    report_type                 TEXT NOT NULL,  -- OPENING_30M|MORNING_CLOSE|AFTERNOON_30M|MARKET_CLOSE
    scheduled_time              TEXT,
    generated_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    morning_check_id            INTEGER,
    market_regime               TEXT,
    volatility_regime           TEXT,
    market_summary              TEXT,
    nikkei_change_pct           NUMERIC,
    topix_change_pct            NUMERIC,
    growth250_change_pct        NUMERIC,
    nikkei_vi                   NUMERIC,
    usdjpy                      NUMERIC,
    sector_snapshot_json        JSONB,
    strong_sectors_json         JSONB,
    weak_sectors_json           JSONB,
    top_stocks_json             JSONB,
    resilience_stocks_json      JSONB,
    momentum_stocks_json        JSONB,
    missed_opportunities_json   JSONB,
    morning_thesis_evaluation_json JSONB,
    risk_alerts_json            JSONB,
    position_alerts_json        JSONB,
    news_changes_json           JSONB,
    event_risk_json             JSONB,
    strategy_update_json        JSONB,
    data_health_json            JSONB,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, trade_date, report_type)
);
CREATE INDEX IF NOT EXISTS idx_market_intel_user_date ON market_intelligence_reports(user_id, trade_date DESC, generated_at ASC);
"""

# 2026-09-10新規（Market Intelligence Timeline Phase2-C「今買い時TOP5＋Thesis永続化」）：
# entry_ready_top5（ENTRY_SCOREで選ばれた「今エントリー条件が整っている」候補）1件ごとに、
# 選出時点の仮説（thesis）と、その後の答え合わせ（Phase2-A/Bの朝TOP5答え合わせと同じ
# evaluate_morning_thesis/_thesis_transition_status/_final_top5_resultをそのまま再利用、
# 別ロジックは作らない）を永続化する専用テーブル。market_intelligence_reportsは日付単位の
# レポートJSONブロブで銘柄横断の履歴クエリに向かないため、新規テーブルとした。
# thesis_status は ACTIVE（選出直後）→CONFIRMED/PARTIAL/INVALIDATED/NOT_TRIGGERED/
# DATA_INSUFFICIENT（初回の答え合わせ）→STRENGTHENED/MAINTAINED/WEAKENED/FAILED（2回目以降の
# 答え合わせ、前回からの変化）→SUCCESS/PARTIAL_SUCCESS/FAIL/NO_ENTRY/DATA_INSUFFICIENT
# （大引け時点の最終結果）という1本のライフサイクルを順番に上書きしていく（全履歴は
# status_history_jsonに保持、成績評価集計はfinal_resultで行う）。
# 重複キーは(user_id, code, market, entry_date)＝同じ銘柄が同じ日に何度TOP5入りしても
# 1つの仮説として扱う（既に今日のACTIVEな仮説がある銘柄を再度TOP5に選んでも新規作成しない）。
_SCHEMA_STOCK_THESES_SQL = """
CREATE TABLE IF NOT EXISTS stock_theses (
    id                       SERIAL PRIMARY KEY,
    user_id                  TEXT NOT NULL,
    code                     TEXT NOT NULL,
    market                   TEXT NOT NULL DEFAULT 'JP',
    entry_date               TEXT NOT NULL,
    name                     TEXT,
    initial_entry_score      INTEGER,
    initial_entry_state      TEXT,
    initial_reasons_json     JSONB,
    latest_entry_score       INTEGER,
    latest_thesis_result     TEXT,     -- 直近のevaluate_morning_thesis()生の結果（次回transition計算の入力）
    analysis_confidence      TEXT,     -- HIGH|MEDIUM|LOW（data_qualityから決定、独自に推測しない）
    thesis_status            TEXT NOT NULL DEFAULT 'ACTIVE',
    final_result             TEXT,     -- 大引け確定後のみ設定：SUCCESS|PARTIAL_SUCCESS|FAIL|NO_ENTRY|DATA_INSUFFICIENT
    entered_position         BOOLEAN NOT NULL DEFAULT false,
    status_history_json      JSONB NOT NULL DEFAULT '[]'::jsonb,
    raw_payload_json         JSONB,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, code, market, entry_date)
);
CREATE INDEX IF NOT EXISTS idx_stock_theses_user_date ON stock_theses(user_id, entry_date DESC);
CREATE INDEX IF NOT EXISTS idx_stock_theses_user_status ON stock_theses(user_id, thesis_status);
"""

# 2026-09-10追加（Phase2-C「朝TOP5成績評価＋TOP5選考基準の全面見直し」指示書11・12番）：
# 「朝TOP5」（08:50 MorningMarketCheck生成時点で確定・以後書き換えない、成績評価専用）と
# 「Current TOP5」（09:30以降いつでも再計算できる、その場限りの表示用）を明確に分離する。
# stock_thesesは今後「朝TOP5」だけが行を作る（source='MORNING'固定）——Current TOP5の
# 都度更新では行を作らない・書き換えない設計に変更した（既存重複テーブルを増やさず、
# 最低限のALTER COLUMNで対応、指示書27番「最小変更を優先」）。
_MIGRATE_STOCK_THESES_MORNING_COLUMNS_SQL = """
ALTER TABLE stock_theses ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'MORNING';
ALTER TABLE stock_theses ADD COLUMN IF NOT EXISTS morning_check_id INTEGER;
ALTER TABLE stock_theses ADD COLUMN IF NOT EXISTS morning_rank INTEGER;
ALTER TABLE stock_theses ADD COLUMN IF NOT EXISTS vwap_state TEXT;
ALTER TABLE stock_theses ADD COLUMN IF NOT EXISTS auto_rs BOOLEAN;
ALTER TABLE stock_theses ADD COLUMN IF NOT EXISTS auto_sector BOOLEAN;
ALTER TABLE stock_theses ADD COLUMN IF NOT EXISTS resilience TEXT;
ALTER TABLE stock_theses ADD COLUMN IF NOT EXISTS trigger_text TEXT;
ALTER TABLE stock_theses ADD COLUMN IF NOT EXISTS avoid_condition TEXT;
ALTER TABLE stock_theses ADD COLUMN IF NOT EXISTS morning_price NUMERIC;
"""

# 2026-09-10新規（にこそく@nicosokufx X投稿 自動取得・市場分析連携、指示書3・20番）：
# market_sourcesは将来の複数アカウント拡張用の設定テーブル（今回はnicosokufx 1件のみ
# seedする）。social_market_postsは取得したX投稿本体＋分類＋事実/見解分離＋画像解析
# （手動注釈、下記参照）の保存先。既存news_catalysts/news_feedback等とは完全に分離した
# 専用テーブル（指示書3番「既存ニュースDBとは完全に混ぜない」）。
# 画像の構造化解析（ヒートマップ読み取り等）はOCR/画像認識APIを新規導入せず（CLAUDE.md
# 「有料AI APIは使わない」方針を踏襲）、image_analysis_jsonへユーザーがChatGPT等で解析した
# 結果をJSON貼り付けで保存する設計とした（既存のSmart Import・ChatGPT連携と同じ
# 「解析はユーザー側で行い、アプリは構造化データの保存/表示に徹する」パターン）。
# postはユーザー非依存の公開市場情報として扱う（既存の大半のテーブルと異なりuser_id列を
# 持たない——にこそく氏の投稿は全ユーザー共通の同一データのため、per-userに複製しない）。
_SCHEMA_SOCIAL_MARKET_SQL = """
CREATE TABLE IF NOT EXISTS market_sources (
    id                 SERIAL PRIMARY KEY,
    platform           TEXT NOT NULL DEFAULT 'X',
    handle             TEXT NOT NULL,
    display_name       TEXT,
    enabled            BOOLEAN NOT NULL DEFAULT true,
    priority           TEXT NOT NULL DEFAULT 'HIGH',
    categories_json    JSONB,
    last_seen_post_id  TEXT,
    last_success_at    TIMESTAMPTZ,
    last_error         TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (platform, handle)
);

CREATE TABLE IF NOT EXISTS social_market_posts (
    id                     SERIAL PRIMARY KEY,
    source_type            TEXT NOT NULL DEFAULT 'X_MARKET_SOURCE',
    source_name            TEXT NOT NULL,
    source_handle          TEXT NOT NULL,
    post_id                TEXT NOT NULL,
    posted_at              TIMESTAMPTZ,
    text                   TEXT,
    url                    TEXT,
    media_json             JSONB,
    quoted_post_json       JSONB,
    public_metrics_json    JSONB,
    categories_json        JSONB,   -- ["MARKET_HEATMAP","SECTOR_ROTATION",...]
    importance             TEXT,    -- LOW|MEDIUM|HIGH|CRITICAL
    facts_json             JSONB,
    author_opinion_json    JSONB,
    system_inference_json  JSONB,
    direct_mentions_json   JSONB,   -- 本文に直接登場した登録銘柄コード
    theme_related_json     JSONB,   -- テーマ経由の間接関連銘柄コード
    image_analysis_json    JSONB,   -- ユーザーが後から貼り付ける構造化画像解析結果
    verification_status    TEXT NOT NULL DEFAULT 'UNVERIFIED',  -- CONFIRMED|PARTIALLY_CONFIRMED|UNVERIFIED|CONTRADICTED
    underlying_event_id    INTEGER, -- market_events.idへの紐付け（重複防止用）
    fetched_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_handle, post_id)
);
CREATE INDEX IF NOT EXISTS idx_social_posts_posted_at ON social_market_posts(posted_at DESC);
CREATE INDEX IF NOT EXISTS idx_social_posts_importance ON social_market_posts(importance);
"""

# Market Intelligence Phase6（2026-09-12新規）：複数X情報源（@nicosokufx/@polymarketjapan/
# @kgbukabu/@aryarya）への拡張。market_sourcesを本格利用する設定駆動型へ拡張し（指示書1番）、
# social_market_postsにはsource_type別の構造化抽出結果・一次情報リンク・
# intelligence_cluster_idを追加専用で持たせる（指示書5・8・10・11・16番）。既存Phase1〜5の
# 列・ロジックは一切変更しない（指示書「Phase5後方互換」）。
_MIGRATE_MARKET_SOURCES_PROFILE_SQL = """
ALTER TABLE market_sources ADD COLUMN IF NOT EXISTS source_type TEXT;
ALTER TABLE market_sources ADD COLUMN IF NOT EXISTS poll_interval_market_sec INTEGER;
ALTER TABLE market_sources ADD COLUMN IF NOT EXISTS poll_interval_off_sec INTEGER;
ALTER TABLE market_sources ADD COLUMN IF NOT EXISTS strengths_json JSONB;
ALTER TABLE market_sources ADD COLUMN IF NOT EXISTS evaluation_modes_json JSONB;
"""

_MIGRATE_SOCIAL_POSTS_INTELLIGENCE_SQL = """
ALTER TABLE social_market_posts ADD COLUMN IF NOT EXISTS post_classification TEXT;
ALTER TABLE social_market_posts ADD COLUMN IF NOT EXISTS prediction_market_json JSONB;
ALTER TABLE social_market_posts ADD COLUMN IF NOT EXISTS stock_breaking_json JSONB;
ALTER TABLE social_market_posts ADD COLUMN IF NOT EXISTS corporate_breaking_json JSONB;
ALTER TABLE social_market_posts ADD COLUMN IF NOT EXISTS primary_source_url TEXT;
ALTER TABLE social_market_posts ADD COLUMN IF NOT EXISTS primary_source_type TEXT;
ALTER TABLE social_market_posts ADD COLUMN IF NOT EXISTS discovered_via_social BOOLEAN;
ALTER TABLE social_market_posts ADD COLUMN IF NOT EXISTS intelligence_cluster_id TEXT;
CREATE INDEX IF NOT EXISTS idx_social_posts_cluster ON social_market_posts(intelligence_cluster_id);
"""

# 場中レポート（market_intelligence_reports）から直近のにこそく投稿を参照できるように、
# 追加専用の列を1つ増やすだけ（既存列・既存レポート生成ロジックには一切影響しない、
# 指示書11・21番「既存機能を壊さない」）。
_MIGRATE_MARKET_INTEL_SOCIAL_SQL = """
ALTER TABLE market_intelligence_reports ADD COLUMN IF NOT EXISTS social_signals_json JSONB;
"""

# 2026-09-10追加（にこそく画像解析待ちキュー）：画像付き投稿を取得した時点でPENDINGにし、
# ユーザーがChatGPT等で解析した結果を貼り付けて保存した時点でANALYZEDへ変わる2値の状態列。
# 画像なし投稿はNULLのまま（「解析待ち」ではなく「そもそも対象外」）。
_MIGRATE_SOCIAL_POSTS_IMAGE_STATUS_SQL = """
ALTER TABLE social_market_posts ADD COLUMN IF NOT EXISTS image_analysis_status TEXT;
"""

# にこそくX連携 Phase2（2026-09-10）：image_analysis_statusをNONE/PENDING/ANALYZED/SKIPPED/
# FAILEDの5値へ拡張する。既存DBは壊さず追加カラム＋バックフィルで対応（指示書1番）。
# 旧仕様では画像なし投稿はNULLのままだったが、新仕様ではNONEを明示する（「そもそも対象外」を
# 積極的に表す値へ変更、指示書1番の定義に合わせる）。既存のPENDING/ANALYZEDの値はそのまま
# 意味が変わらないため触らない。
_MIGRATE_SOCIAL_POSTS_IMAGE_STATUS_V2_SQL = """
ALTER TABLE social_market_posts ADD COLUMN IF NOT EXISTS image_analysis_error TEXT;
ALTER TABLE social_market_posts ADD COLUMN IF NOT EXISTS image_analysis_updated_at TIMESTAMPTZ;
UPDATE social_market_posts SET image_analysis_status='NONE'
    WHERE image_analysis_status IS NULL AND (media_json IS NULL OR media_json::text = '[]');
UPDATE social_market_posts SET image_analysis_status='PENDING'
    WHERE image_analysis_status IS NULL AND media_json IS NOT NULL AND media_json::text <> '[]';
"""

# にこそくX連携 Phase2・診断API（指示書11・17番）：1サイクル分のfetch統計をDBへも残す
# （プロセス再起動をまたいでも直近の取得結果がdiagnosticsから見えるように）。Bearer Token
# 等の秘密情報は一切保存しない列のみ。
_MIGRATE_MARKET_SOURCES_DIAGNOSTICS_SQL = """
ALTER TABLE market_sources ADD COLUMN IF NOT EXISTS last_fetch_started_at TIMESTAMPTZ;
ALTER TABLE market_sources ADD COLUMN IF NOT EXISTS last_fetch_finished_at TIMESTAMPTZ;
ALTER TABLE market_sources ADD COLUMN IF NOT EXISTS last_error_at TIMESTAMPTZ;
ALTER TABLE market_sources ADD COLUMN IF NOT EXISTS last_http_status TEXT;
ALTER TABLE market_sources ADD COLUMN IF NOT EXISTS last_fetched_count INTEGER;
ALTER TABLE market_sources ADD COLUMN IF NOT EXISTS last_inserted_count INTEGER;
ALTER TABLE market_sources ADD COLUMN IF NOT EXISTS last_duplicate_count INTEGER;
"""


# Phase MU-S1（2026-09-14・SHARED/PRIVATE再分類）：watchlist / market_events /
# news_catalysts / expert_views / stock_theses / market_intelligence_reportsの
# 既存データのuser_idを固定共有scope（_SHARED_SCOPE、"_shared"）へ一括移行する。
# 2026-09-02の一律user_id分離でこれらも個人ごとに分断されてしまっていたための修正
# （ポジション/トレード等の個人情報は現状のuser_idスコープを維持し、市場分析だけを
# 共有に戻す。指示書：既存データは壊さず、user_id列自体も削除しない）。
# サーバー起動のたびに実行しても副作用が無いよう冪等にしてある：
#   ①同じ自然キー（例：watchlistならcode+market）で既に"_shared"の行がある場合は
#     旧user_idの行を削除（重複を残さない）
#   ②まだ"_shared"化されていない行同士で自然キーが重複する場合（本番matsuuraと
#     過去の手動テスト残骸user_id等）は、matsuura優先→idが小さい方優先で1件だけ残す
#   ③残った行のuser_idを"_shared"へ更新
# 各テーブルの自然キーは元のUNIQUE制約と同じ（NULL同士を重複扱いしないPostgresの
# 仕様も含めてそのまま踏襲）。定数_SHARED_SCOPEの値を変更した場合はここも合わせて
# 変更すること（あえて直接リテラル'_shared'で書いている）。
_MIGRATE_SHARED_SCOPE_SQL = """
-- watchlist（自然キー: code, market）
DELETE FROM watchlist a USING watchlist b
WHERE a.user_id <> '_shared' AND b.user_id = '_shared'
  AND a.code = b.code AND a.market = b.market;
DELETE FROM watchlist a USING watchlist b
WHERE a.user_id <> '_shared' AND b.user_id <> '_shared' AND a.id <> b.id
  AND a.code = b.code AND a.market = b.market
  AND ((b.user_id = 'matsuura' AND a.user_id <> 'matsuura')
       OR (a.user_id <> 'matsuura' AND b.user_id <> 'matsuura' AND a.id > b.id));
UPDATE watchlist SET user_id = '_shared' WHERE user_id <> '_shared';

-- market_events（自然キー: event_date, title）
DELETE FROM market_events a USING market_events b
WHERE a.user_id <> '_shared' AND b.user_id = '_shared'
  AND a.event_date = b.event_date AND a.title = b.title;
DELETE FROM market_events a USING market_events b
WHERE a.user_id <> '_shared' AND b.user_id <> '_shared' AND a.id <> b.id
  AND a.event_date = b.event_date AND a.title = b.title
  AND ((b.user_id = 'matsuura' AND a.user_id <> 'matsuura')
       OR (a.user_id <> 'matsuura' AND b.user_id <> 'matsuura' AND a.id > b.id));
UPDATE market_events SET user_id = '_shared' WHERE user_id <> '_shared';

-- news_catalysts（自然キー: catalyst_date, title）
DELETE FROM news_catalysts a USING news_catalysts b
WHERE a.user_id <> '_shared' AND b.user_id = '_shared'
  AND a.catalyst_date = b.catalyst_date AND a.title = b.title;
DELETE FROM news_catalysts a USING news_catalysts b
WHERE a.user_id <> '_shared' AND b.user_id <> '_shared' AND a.id <> b.id
  AND a.catalyst_date = b.catalyst_date AND a.title = b.title
  AND ((b.user_id = 'matsuura' AND a.user_id <> 'matsuura')
       OR (a.user_id <> 'matsuura' AND b.user_id <> 'matsuura' AND a.id > b.id));
UPDATE news_catalysts SET user_id = '_shared' WHERE user_id <> '_shared';

-- expert_views（自然キー: expert_name, published_at, source_title。source_titleが
-- NULL同士は元のUNIQUE制約と同じくPostgresの仕様上「別物」として扱い重複扱いしない）
DELETE FROM expert_views a USING expert_views b
WHERE a.user_id <> '_shared' AND b.user_id = '_shared'
  AND a.expert_name = b.expert_name AND a.published_at = b.published_at
  AND a.source_title = b.source_title;
DELETE FROM expert_views a USING expert_views b
WHERE a.user_id <> '_shared' AND b.user_id <> '_shared' AND a.id <> b.id
  AND a.expert_name = b.expert_name AND a.published_at = b.published_at
  AND a.source_title = b.source_title
  AND ((b.user_id = 'matsuura' AND a.user_id <> 'matsuura')
       OR (a.user_id <> 'matsuura' AND b.user_id <> 'matsuura' AND a.id > b.id));
UPDATE expert_views SET user_id = '_shared' WHERE user_id <> '_shared';

-- stock_theses（自然キー: code, market, entry_date）
DELETE FROM stock_theses a USING stock_theses b
WHERE a.user_id <> '_shared' AND b.user_id = '_shared'
  AND a.code = b.code AND a.market = b.market AND a.entry_date = b.entry_date;
DELETE FROM stock_theses a USING stock_theses b
WHERE a.user_id <> '_shared' AND b.user_id <> '_shared' AND a.id <> b.id
  AND a.code = b.code AND a.market = b.market AND a.entry_date = b.entry_date
  AND ((b.user_id = 'matsuura' AND a.user_id <> 'matsuura')
       OR (a.user_id <> 'matsuura' AND b.user_id <> 'matsuura' AND a.id > b.id));
UPDATE stock_theses SET user_id = '_shared' WHERE user_id <> '_shared';

-- market_intelligence_reports（自然キー: trade_date, report_type）
DELETE FROM market_intelligence_reports a USING market_intelligence_reports b
WHERE a.user_id <> '_shared' AND b.user_id = '_shared'
  AND a.trade_date = b.trade_date AND a.report_type = b.report_type;
DELETE FROM market_intelligence_reports a USING market_intelligence_reports b
WHERE a.user_id <> '_shared' AND b.user_id <> '_shared' AND a.id <> b.id
  AND a.trade_date = b.trade_date AND a.report_type = b.report_type
  AND ((b.user_id = 'matsuura' AND a.user_id <> 'matsuura')
       OR (a.user_id <> 'matsuura' AND b.user_id <> 'matsuura' AND a.id > b.id));
UPDATE market_intelligence_reports SET user_id = '_shared' WHERE user_id <> '_shared';
"""

# Phase MU-S2（2026-09-14・PRIVATE情報漏洩の修正）：MU-S1でSHARED化する前のmarket_intelligence_
# reportsは、生成したユーザー自身の保有ポジション情報（銘柄コード・銘柄名・average_price由来の
# 個人PnL%・position risk warning）をposition_alerts_json・risk_alerts_json・
# strategy_update_json.major_changes/overnight_notesに書き込んでいた。SHARED化後はこれが
# 他ユーザーにも見える状態になってしまうため、既存データからこれらを取り除く（今後の保存は
# server.py側の修正で既に個人情報を含まない。このSQLは過去に保存済みの行の後始末のみ）。
# 冪等：既に取り除かれていれば何も変化しない。
_MIGRATE_CLEAR_PRIVATE_FROM_SHARED_REPORTS_SQL = """
UPDATE market_intelligence_reports
SET position_alerts_json = '[]'::jsonb
WHERE position_alerts_json IS NOT NULL AND position_alerts_json <> '[]'::jsonb;

UPDATE market_intelligence_reports
SET risk_alerts_json = COALESCE((
    SELECT jsonb_agg(elem) FROM jsonb_array_elements(risk_alerts_json) elem
    WHERE elem->>'message' <> '保有銘柄が損切りルール（EXIT RULE）に到達'
), '[]'::jsonb)
WHERE risk_alerts_json @> '[{"message": "保有銘柄が損切りルール（EXIT RULE）に到達"}]'::jsonb;

UPDATE market_intelligence_reports
SET strategy_update_json = jsonb_set(
    jsonb_set(
        strategy_update_json,
        '{major_changes}',
        COALESCE((SELECT jsonb_agg(v) FROM jsonb_array_elements_text(strategy_update_json->'major_changes') v
                   WHERE v <> 'EXIT_RULE_HIT'), '[]'::jsonb)
    ),
    '{overnight_notes}',
    COALESCE((SELECT jsonb_agg(v) FROM jsonb_array_elements_text(strategy_update_json->'overnight_notes') v
               WHERE v <> '損切りライン接近/到達中の保有銘柄あり。持ち越し判断は個別に再確認'), '[]'::jsonb)
)
WHERE strategy_update_json ? 'major_changes' AND strategy_update_json ? 'overnight_notes'
  AND (strategy_update_json->'major_changes' @> '"EXIT_RULE_HIT"'::jsonb
       OR strategy_update_json->'overnight_notes' @> '"損切りライン接近/到達中の保有銘柄あり。持ち越し判断は個別に再確認"'::jsonb);
"""

# Phase MU-S3B（2026-09-14・SHARED_SAFE 5テーブルの共有化）：MU-S3Aの調査で個人情報混入が
# 無いと確認できた5テーブル（auto_signal_events / limit_up_events / theme_momentum_history /
# next_day_theme_candidates / entry_candidate_snapshots）を、MU-S1と同じ_SHARED_SCOPEへ
# 一括移行する。既存データのuser_id列は削除しない（将来のscope再設計・ロールバックに備える）。
# 冪等：①同じ自然キーで既に"_shared"の行がある場合は旧user_idの行を削除（重複を残さない）
# ②まだ"_shared"化されていない行同士で自然キーが重複する場合はmatsuura優先→id最小優先で
# 1件だけ残す ③残った行のuser_idを"_shared"へ更新。
# auto_signal_eventsとentry_candidate_snapshots(dedupe_keyがNULLの行)は自然キー（user_id込み）
# のUNIQUE制約が無いログ/スナップショットのため、単純UPDATEのみで良い（重複が起きようがない）。
_MIGRATE_MUS3B_SHARED_SCOPE_SQL = """
-- auto_signal_events（自然キー無し・履歴ログのため単純UPDATEのみ）
UPDATE auto_signal_events SET user_id = '_shared' WHERE user_id <> '_shared';

-- limit_up_events（自然キー: event_date, symbol）
DELETE FROM limit_up_events a USING limit_up_events b
WHERE a.user_id <> '_shared' AND b.user_id = '_shared'
  AND a.event_date = b.event_date AND a.symbol = b.symbol;
DELETE FROM limit_up_events a USING limit_up_events b
WHERE a.user_id <> '_shared' AND b.user_id <> '_shared' AND a.id <> b.id
  AND a.event_date = b.event_date AND a.symbol = b.symbol
  AND ((b.user_id = 'matsuura' AND a.user_id <> 'matsuura')
       OR (a.user_id <> 'matsuura' AND b.user_id <> 'matsuura' AND a.id > b.id));
UPDATE limit_up_events SET user_id = '_shared' WHERE user_id <> '_shared';

-- theme_momentum_history（自然キー: theme, event_date）
DELETE FROM theme_momentum_history a USING theme_momentum_history b
WHERE a.user_id <> '_shared' AND b.user_id = '_shared'
  AND a.theme = b.theme AND a.event_date = b.event_date;
DELETE FROM theme_momentum_history a USING theme_momentum_history b
WHERE a.user_id <> '_shared' AND b.user_id <> '_shared' AND a.id <> b.id
  AND a.theme = b.theme AND a.event_date = b.event_date
  AND ((b.user_id = 'matsuura' AND a.user_id <> 'matsuura')
       OR (a.user_id <> 'matsuura' AND b.user_id <> 'matsuura' AND a.id > b.id));
UPDATE theme_momentum_history SET user_id = '_shared' WHERE user_id <> '_shared';

-- next_day_theme_candidates（自然キー: theme, generated_date）
DELETE FROM next_day_theme_candidates a USING next_day_theme_candidates b
WHERE a.user_id <> '_shared' AND b.user_id = '_shared'
  AND a.theme = b.theme AND a.generated_date = b.generated_date;
DELETE FROM next_day_theme_candidates a USING next_day_theme_candidates b
WHERE a.user_id <> '_shared' AND b.user_id <> '_shared' AND a.id <> b.id
  AND a.theme = b.theme AND a.generated_date = b.generated_date
  AND ((b.user_id = 'matsuura' AND a.user_id <> 'matsuura')
       OR (a.user_id <> 'matsuura' AND b.user_id <> 'matsuura' AND a.id > b.id));
UPDATE next_day_theme_candidates SET user_id = '_shared' WHERE user_id <> '_shared';

-- entry_candidate_snapshots（実質的な自然キーはdedupe_key、NULLはUNIQUE制約対象外）
DELETE FROM entry_candidate_snapshots a USING entry_candidate_snapshots b
WHERE a.user_id <> '_shared' AND b.user_id = '_shared'
  AND a.dedupe_key IS NOT NULL AND a.dedupe_key = b.dedupe_key;
DELETE FROM entry_candidate_snapshots a USING entry_candidate_snapshots b
WHERE a.user_id <> '_shared' AND b.user_id <> '_shared' AND a.id <> b.id
  AND a.dedupe_key IS NOT NULL AND a.dedupe_key = b.dedupe_key
  AND ((b.user_id = 'matsuura' AND a.user_id <> 'matsuura')
       OR (a.user_id <> 'matsuura' AND b.user_id <> 'matsuura' AND a.id > b.id));
UPDATE entry_candidate_snapshots SET user_id = '_shared' WHERE user_id <> '_shared';
"""

# Phase MU-S3C（2026-09-14・MIXEDテーブル分離 その1：trade_playbooks）：GLOBAL定義（setup・
# entry/exit/stop/avoid条件・一般的evidence）と個人実践成績（user_attempt_count等、
# record_trade_outcome_for_playbooksが個人のnet_pnl/entry_price/sharesから算出して同一行に
# 書き込んでいた）を別テーブルへ分離する。trade_playbooks自体はtrade_rulesと同じ
# visibility='USER'|'GLOBAL'方式のまま（MU-S2の_MIGRATE_RULE_VISIBILITY_SQLで既にvisibility列
# 追加済み）——プレイブックも個人が自分用に作成し得るため、MU-S1のような単純な強制_shared化は
# しない。実データ0件のタイミングで、破壊的変更（列削除）を安全に行う。
_SCHEMA_TRADE_PLAYBOOK_USER_STATS_SQL = """
CREATE TABLE IF NOT EXISTS trade_playbook_user_stats (
    id                    SERIAL PRIMARY KEY,
    user_id               TEXT NOT NULL,
    playbook_id           INTEGER NOT NULL REFERENCES trade_playbooks(id) ON DELETE CASCADE,
    attempt_count         INTEGER NOT NULL DEFAULT 0,
    success_count         INTEGER NOT NULL DEFAULT 0,
    failure_count         INTEGER NOT NULL DEFAULT 0,
    avg_return            NUMERIC,
    compatibility_score   NUMERIC,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, playbook_id)
);
CREATE INDEX IF NOT EXISTS idx_trade_playbook_user_stats_user ON trade_playbook_user_stats(user_id, playbook_id);
"""

# 既存trade_playbooks行に非デフォルトの個人成績が残っていれば新テーブルへ退避してから列を削除する
# （現状0件のため実質no-opだが、将来この関数が別環境で実行されてもデータを失わないよう防御的に書く）。
# 列が既に削除済み（2回目以降の起動）でも安全に再実行できるよう、SELECT文自体をDO $$ ... $$内の
# IF分岐に閉じ込める（PL/pgSQL内の埋め込みSQLは実際にそのステートメントへ到達した時点で初めて
# 解析されるため、列が存在しない環境ではIF条件がfalseになりparseされず落ちない）。
_MIGRATE_TRADE_PLAYBOOK_USER_STATS_SQL = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='trade_playbooks' AND column_name='user_attempt_count') THEN
        INSERT INTO trade_playbook_user_stats (user_id, playbook_id, attempt_count, success_count,
                                                 failure_count, avg_return, compatibility_score)
        SELECT user_id, id, user_attempt_count, user_success_count, user_failure_count,
               user_avg_return, user_compatibility_score
        FROM trade_playbooks
        WHERE (user_attempt_count > 0 OR user_success_count > 0 OR user_failure_count > 0
               OR user_avg_return IS NOT NULL OR user_compatibility_score IS NOT NULL)
        ON CONFLICT (user_id, playbook_id) DO NOTHING;
    END IF;
END $$;

ALTER TABLE trade_playbooks DROP COLUMN IF EXISTS user_attempt_count;
ALTER TABLE trade_playbooks DROP COLUMN IF EXISTS user_success_count;
ALTER TABLE trade_playbooks DROP COLUMN IF EXISTS user_failure_count;
ALTER TABLE trade_playbooks DROP COLUMN IF EXISTS user_avg_return;
ALTER TABLE trade_playbooks DROP COLUMN IF EXISTS user_compatibility_score;
"""

# Phase MU-S3C（2026-09-14・MIXEDテーブル分離 その2：morning_market_checks）：SHARED MARKET CORE
# （指数・為替・商品・ADR・データ品質・強弱セクター・朝TOP5候補・見送り銘柄・地合い耐性・
# 市場由来のrisk警告・イベント・戦略）と、PRIVATE USER OVERLAY（個人の保有ポジション由来の
# position_risk_json・そこから生成されるCRITICAL警告・既読状態）へ分離する。以前は
# risk_warnings_jsonに「保有銘柄が損切りルールに到達」という個人ポジション由来の文言が
# 混入し得ていたため、SHARED側はmarket_risk_warnings_json（市場要因のみ）に責務を絞り、
# 個人由来の警告はPRIVATE overlay側のposition_critical_warnings_jsonへ分離する。
_SCHEMA_MORNING_CHECK_PRIVATE_OVERLAY_SQL = """
CREATE TABLE IF NOT EXISTS morning_market_check_private_overlay (
    id                              SERIAL PRIMARY KEY,
    user_id                         TEXT NOT NULL,
    check_id                        INTEGER NOT NULL REFERENCES morning_market_checks(id) ON DELETE CASCADE,
    check_date                      TEXT NOT NULL,
    snapshot_time                   TEXT NOT NULL,
    position_risk_json              JSONB,
    position_critical_warnings_json JSONB,
    is_read                         BOOLEAN NOT NULL DEFAULT false,
    created_at                      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, check_id)
);
CREATE INDEX IF NOT EXISTS idx_morning_check_overlay_user_date
    ON morning_market_check_private_overlay(user_id, check_date DESC);
"""

# 既存6行（実データ確認済み：position_risk_jsonは全行[]、is_readは2行True）を退避してから
# SHARED側の列を整理する。列が既に整理済み（2回目以降の起動）でも安全に再実行できるよう、
# 列存在チェックをDO $$ ... $$のIF内に閉じ込める（trade_playbook_user_statsと同じ手法）。
_MIGRATE_MORNING_CHECK_PRIVATE_OVERLAY_SQL = """
ALTER TABLE morning_market_checks ADD COLUMN IF NOT EXISTS market_risk_warnings_json JSONB;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='morning_market_checks' AND column_name='position_risk_json') THEN
        INSERT INTO morning_market_check_private_overlay
            (user_id, check_id, check_date, snapshot_time, position_risk_json,
             position_critical_warnings_json, is_read)
        SELECT user_id, id, check_date, snapshot_time,
               COALESCE(position_risk_json, '[]'::jsonb),
               COALESCE((SELECT jsonb_agg(elem) FROM jsonb_array_elements(risk_warnings_json) elem
                          WHERE elem->>'message' = '保有銘柄が損切りルールに到達'), '[]'::jsonb),
               is_read
        FROM morning_market_checks
        ON CONFLICT (user_id, check_id) DO NOTHING;

        UPDATE morning_market_checks
        SET market_risk_warnings_json = COALESCE((
            SELECT jsonb_agg(elem) FROM jsonb_array_elements(risk_warnings_json) elem
            WHERE elem->>'message' <> '保有銘柄が損切りルールに到達'
        ), '[]'::jsonb)
        WHERE market_risk_warnings_json IS NULL AND risk_warnings_json IS NOT NULL;
    END IF;
END $$;

ALTER TABLE morning_market_checks DROP COLUMN IF EXISTS risk_warnings_json;
ALTER TABLE morning_market_checks DROP COLUMN IF EXISTS position_risk_json;
ALTER TABLE morning_market_checks DROP COLUMN IF EXISTS is_read;
"""

# PRIVATE overlayへ退避完了後にSHARED本体のuser_idを_sharedへ一括移行する（MU-S1と同じ
# 冪等パターン。自然キー：check_date, snapshot_time）。overlay側は退避時点のuser_idを
# そのまま保持しているため、本体のuser_id変更はoverlayの正しさに影響しない。
_MIGRATE_MORNING_CHECK_SHARED_SCOPE_SQL = """
DELETE FROM morning_market_checks a USING morning_market_checks b
WHERE a.user_id <> '_shared' AND b.user_id = '_shared'
  AND a.check_date = b.check_date AND a.snapshot_time = b.snapshot_time;
DELETE FROM morning_market_checks a USING morning_market_checks b
WHERE a.user_id <> '_shared' AND b.user_id <> '_shared' AND a.id <> b.id
  AND a.check_date = b.check_date AND a.snapshot_time = b.snapshot_time
  AND ((b.user_id = 'matsuura' AND a.user_id <> 'matsuura')
       OR (a.user_id <> 'matsuura' AND b.user_id <> 'matsuura' AND a.id > b.id));
UPDATE morning_market_checks SET user_id = '_shared' WHERE user_id <> '_shared';
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
        conn.execute(_SCHEMA_TRADE_EXPERIENCES_SQL)
        conn.execute(_SCHEMA_TRADE_EXPERIENCES_LEARNING_SQL)
        conn.execute(_SCHEMA_DAILY_REVIEWS_SQL)
        conn.execute(_SCHEMA_KNOWLEDGE_ENGINE_SQL)
        conn.execute(_MIGRATE_RULE_VISIBILITY_SQL)
        conn.execute(_SCHEMA_MORNING_CHECK_SQL)
        conn.execute(_SCHEMA_POSITION_RISK_RULES_SQL)
        conn.execute(_SCHEMA_MARKET_INTELLIGENCE_SQL)
        conn.execute(_SCHEMA_STOCK_THESES_SQL)
        conn.execute(_MIGRATE_STOCK_THESES_MORNING_COLUMNS_SQL)
        conn.execute(_SCHEMA_SOCIAL_MARKET_SQL)
        conn.execute(_MIGRATE_MARKET_SOURCES_PROFILE_SQL)
        conn.execute(_MIGRATE_SOCIAL_POSTS_INTELLIGENCE_SQL)
        conn.execute(_MIGRATE_MARKET_INTEL_SOCIAL_SQL)
        conn.execute(_MIGRATE_SOCIAL_POSTS_IMAGE_STATUS_SQL)
        conn.execute(_MIGRATE_SOCIAL_POSTS_IMAGE_STATUS_V2_SQL)
        conn.execute(_MIGRATE_MARKET_SOURCES_DIAGNOSTICS_SQL)
        conn.execute(_SCHEMA_SOCIAL_SIGNAL_EVALUATIONS_SQL)
        conn.execute(_MIGRATE_SOCIAL_SIGNAL_EVALUATIONS_V2_SQL)
        conn.execute(_SCHEMA_SOCIAL_EVENT_EVALUATIONS_SQL)
        conn.execute(_MIGRATE_SOCIAL_SIGNAL_EVALUATIONS_V3_SQL)
        conn.execute(_SCHEMA_SOCIAL_SIGNAL_ALERTS_SQL)
        conn.execute(_SCHEMA_UNDERLYING_EVENTS_SQL)
        conn.execute(_MIGRATE_UNDERLYING_EVENTS_V2_SQL)
        conn.execute(_SCHEMA_EVENT_MARKET_REACTIONS_SQL)
        conn.execute(_SCHEMA_EVENT_DECISION_SUPPORT_SQL)
        conn.execute(_SCHEMA_TRADE_DECISION_CONTEXT_SQL)
        conn.execute(_MIGRATE_ENTRY_CANDIDATE_SNAPSHOTS_V2_SQL)
        conn.execute(_MIGRATE_VALIDATION_SESSIONS_V2_SQL)
        conn.execute(_SCHEMA_PARSER_FAILURE_QUEUE_SQL)
        conn.execute(_SCHEMA_CONFIG_CHANGE_LOG_SQL)
        conn.execute(_SCHEMA_CHORUCO_STYLE_SQL)
        conn.execute(_SCHEMA_CROSS_MARKET_LINK_SQL)
        conn.execute(_SCHEMA_SECTOR_ROTATION_SQL)
        conn.execute(_SCHEMA_YAAMAN_THEME_SQL)
        conn.execute(_MIGRATE_SHARED_SCOPE_SQL)
        conn.execute(_MIGRATE_CLEAR_PRIVATE_FROM_SHARED_REPORTS_SQL)
        conn.execute(_MIGRATE_MUS3B_SHARED_SCOPE_SQL)
        conn.execute(_SCHEMA_TRADE_PLAYBOOK_USER_STATS_SQL)
        conn.execute(_MIGRATE_TRADE_PLAYBOOK_USER_STATS_SQL)
        conn.execute(_SCHEMA_MORNING_CHECK_PRIVATE_OVERLAY_SQL)
        conn.execute(_MIGRATE_MORNING_CHECK_PRIVATE_OVERLAY_SQL)
        conn.execute(_MIGRATE_MORNING_CHECK_SHARED_SCOPE_SQL)
        conn.execute(_SCHEMA_NEWS_NOTIFICATION_LOG_SQL)
        conn.execute(_MIGRATE_MARKET_NEWS_CONTEXT_SQL)
        conn.execute(_MIGRATE_MARKET_EVENT_SOURCE_TRACKING_SQL)
        conn.execute(_MIGRATE_EXTERNAL_INTELLIGENCE_CONTEXT_SQL)
        conn.execute(_MIGRATE_CENTRAL_BANK_EVENT_SYNC_SQL)
        conn.commit()


def get_position_risk_rules(database_url, user_id):
    """ポジション損切りルールの共通設定（唯一の真実）を返す。保存済み行が無ければ
    DEFAULT_POSITION_RISK_RULESをそのまま返す（未設定でも即座に正しい既定値で動く）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return dict(DEFAULT_POSITION_RISK_RULES)
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM position_risk_rules WHERE user_id=%s", [user_id])
            row = cur.fetchone()
    if not row:
        return dict(DEFAULT_POSITION_RISK_RULES)
    return {
        "watch_pct": float(row["watch_pct"]), "warning_pct": float(row["warning_pct"]),
        "max_loss_pct": float(row["max_loss_pct"]), "action": row["action"],
        "allow_reentry": row["allow_reentry"], "reentry_requires_new_decision": row["reentry_requires_new_decision"],
    }


def save_position_risk_rules(database_url, user_id, data):
    """将来のユーザー設定変更に備えたupsert（今回のUIからは呼ばないが、共通設定を1箇所に
    まとめる設計のため保存経路も用意しておく）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return None
    merged = {**DEFAULT_POSITION_RISK_RULES, **data}
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "INSERT INTO position_risk_rules (user_id, watch_pct, warning_pct, max_loss_pct, action, "
                "allow_reentry, reentry_requires_new_decision, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,now()) "
                "ON CONFLICT (user_id) DO UPDATE SET watch_pct=EXCLUDED.watch_pct, warning_pct=EXCLUDED.warning_pct, "
                "max_loss_pct=EXCLUDED.max_loss_pct, action=EXCLUDED.action, allow_reentry=EXCLUDED.allow_reentry, "
                "reentry_requires_new_decision=EXCLUDED.reentry_requires_new_decision, updated_at=now() "
                "RETURNING *",
                [user_id, merged["watch_pct"], merged["warning_pct"], merged["max_loss_pct"], merged["action"],
                 merged["allow_reentry"], merged["reentry_requires_new_decision"]])
            saved = cur.fetchone()
        conn.commit()
    return _row_to_json(saved)


def evaluate_position_risk_tier(pnl_pct, rules=None):
    """pnl_pct（取得単価比%）から現在の階層（None|WATCH|WARNING|EXIT）を判定する共通関数。
    Morning Check・Position画面・通知・利確損切り相談・日次レビューが全てこの関数を通す
    ことで、閾値のズレ（例：Morning CheckだけAPPROACHING/STOPの旧表現のまま、といった
    食い違い）を構造的に防ぐ。"""
    rules = rules or DEFAULT_POSITION_RISK_RULES
    if pnl_pct is None:
        return None
    if pnl_pct <= rules["max_loss_pct"]:
        return "EXIT"
    if pnl_pct <= rules["warning_pct"]:
        return "WARNING"
    if pnl_pct <= rules["watch_pct"]:
        return "WATCH"
    return None


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
    """Phase MU-S2：本人のUSERルールに加え、GLOBAL（visibility='GLOBAL'、user_id=_SHARED_SCOPE）
    ルールも合わせて返す（全ユーザー共通で見えるべきもののため）。GLOBAL行はuser_id列自体が
    _SHARED_SCOPEなので、user_id IN (本人, _SHARED_SCOPE)だけで両方を安全に絞り込める
    （visibility列を独立に見なくても、この2値の組み合わせでしか発生しない設計）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return []
    expire_temporary_trade_rules(database_url, user_id)
    where, params = ["user_id IN (%s, %s)"], [user_id, _SHARED_SCOPE]
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
    """ルール1件を履歴付きで返す（指示書11番：ルール詳細画面用）。Phase MU-S2：本人の
    USERルールに加え、GLOBALルール（list_trade_rulesと同じ判定）も取得できる。"""
    pool = _get_pool(database_url)
    if pool is None:
        return None
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM trade_rules WHERE id=%s AND user_id IN (%s, %s)", [rule_id, user_id, _SHARED_SCOPE])
            row = cur.fetchone()
            if not row:
                return None
            cur.execute("SELECT * FROM trade_rule_history WHERE rule_id=%s AND user_id IN (%s, %s) ORDER BY created_at DESC",
                        [rule_id, user_id, _SHARED_SCOPE])
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
            # Phase MU-S2：GLOBALルールとの重複も検出対象にする（list_trade_rulesと同じ判定）。
            cur.execute("SELECT * FROM trade_rules WHERE user_id IN (%s, %s) AND status NOT IN ('RETIRED','EXPIRED')",
                        [user_id, _SHARED_SCOPE])
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
            # Phase MU-S2：GLOBALルール（user_id=_SHARED_SCOPE）も対象にする。
            where = ["user_id IN (%s, %s)", "status IN ('ACTIVE','TESTING')", "rule_type != 'TEMPORARY'"]
            params = [user_id, _SHARED_SCOPE]
            if categories:
                where.append("(category = ANY(%s) OR scope='global')")
                params.append(list(categories))
            cur.execute(
                f"SELECT * FROM trade_rules WHERE {' AND '.join(where)} ORDER BY "
                f"CASE status WHEN 'ACTIVE' THEN 0 ELSE 1 END, "
                f"CASE confidence WHEN 'HIGH' THEN 0 WHEN 'MEDIUM' THEN 1 ELSE 2 END, evidence_count DESC "
                f"LIMIT %s", params + [limit])
            return [_trade_rule_row_to_json(r) for r in cur.fetchall()]


# ---- Trade Experience Learning（2026-09-11新規）：CRUD ----

_TRADE_EXPERIENCE_JSON_COLS = ("wait_reason_json", "entry_reason_json", "exit_reason_json",
                                 "invalidation_reason_json", "pattern_tags_json", "score_breakdown_json",
                                 "decision_snapshot_json", "post_trade_analysis_json", "rotation_context_json",
                                 "quality_axes_json")
_TRADE_EXPERIENCE_COLS = (
    "trade_date", "symbol", "stock_name", "side", "trade_style", "quantity", "entry_price", "exit_price",
    "entry_time", "exit_time", "gross_pnl", "gross_pnl_pct", "holding_minutes", "pre_entry_state",
    "wait_reason_json", "entry_reason_json", "exit_reason_json", "invalidation_reason_json",
    "market_condition", "sector_condition", "nikkei_change_pct", "relative_strength", "rsi_at_entry",
    "rsi_at_exit", "short_ma", "mid_ma", "long_ma", "volume_ratio", "intraday_low", "intraday_high",
    "distance_from_low_pct", "distance_from_high_pct", "volatility_score", "pattern_tags_json",
    "execution_score", "rule_compliance_score", "result_class", "max_favorable_excursion_pct",
    "max_adverse_excursion_pct", "post_exit_max_price", "post_exit_min_price", "profit_capture_ratio",
    "learning_status", "learning_weight", "score_breakdown_json", "decision_snapshot_json",
    "post_trade_analysis_json", "notes", "decision_quality_score", "trade_result_score",
    "market_mode_at_entry", "market_mode_at_exit", "event_risk_at_entry", "position_multiplier",
    "recommended_multiplier", "story_score_at_entry", "story_break_status", "story_break_reason",
    "primary_driver", "driver_corr_at_entry", "driver_lag_at_entry", "driver_state_at_entry",
    "driver_state_at_exit", "cross_market_score_at_entry",
    "sector_at_entry", "sector_state_at_entry", "sector_flow_score_at_entry",
    "sector_state_at_exit", "sector_flow_score_at_exit", "rotation_context_json",
    "quality_axes_json",
)


def create_trade_experience(database_url, user_id, fields):
    """指示書1・3番：trade_experiences 1件をINSERTする。symbolが無ければNoneを返す。"""
    pool = _get_pool(database_url)
    if pool is None or not (fields or {}).get("symbol"):
        return None
    cols = [c for c in _TRADE_EXPERIENCE_COLS if c in fields]
    values = [fields.get(c) for c in cols]
    wrapped = [json.dumps(v, ensure_ascii=False) if (c in _TRADE_EXPERIENCE_JSON_COLS and v is not None) else v
               for c, v in zip(cols, values)]
    placeholders = ["%s::jsonb" if c in _TRADE_EXPERIENCE_JSON_COLS else "%s" for c in cols]
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"INSERT INTO trade_experiences (user_id, {', '.join(cols)}) "
                f"VALUES (%s, {', '.join(placeholders)}) RETURNING *", [user_id] + wrapped)
            row = cur.fetchone()
        conn.commit()
    return _row_to_json(row) if row else None


def update_trade_experience(database_url, user_id, experience_id, fields):
    """指示書6・9・15番：evaluate_trade_experienceの採点結果等、後から追記する場合に使う。"""
    pool = _get_pool(database_url)
    if pool is None:
        return None
    cols = [c for c in _TRADE_EXPERIENCE_COLS if c in fields]
    if not cols:
        return None
    values = [fields.get(c) for c in cols]
    wrapped = [json.dumps(v, ensure_ascii=False) if (c in _TRADE_EXPERIENCE_JSON_COLS and v is not None) else v
               for c, v in zip(cols, values)]
    set_clauses = [f"{c}=%s::jsonb" if c in _TRADE_EXPERIENCE_JSON_COLS else f"{c}=%s" for c in cols]
    set_clauses.append("updated_at=now()")
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"UPDATE trade_experiences SET {', '.join(set_clauses)} WHERE id=%s AND user_id=%s RETURNING *",
                wrapped + [experience_id, user_id])
            row = cur.fetchone()
        conn.commit()
    return _row_to_json(row) if row else None


def get_trade_experience(database_url, user_id, experience_id):
    pool = _get_pool(database_url)
    if pool is None:
        return None
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM trade_experiences WHERE id=%s AND user_id=%s", [experience_id, user_id])
            row = cur.fetchone()
    return _row_to_json(row) if row else None


def list_trade_experiences(database_url, user_id, symbol=None, trade_date=None, limit=200):
    """指示書14番：GET /api/trade-experiences。symbol/trade_date（YYYY-MM-DD）で絞り込み可能。"""
    pool = _get_pool(database_url)
    if pool is None:
        return []
    where, params = ["user_id=%s"], [user_id]
    if symbol:
        where.append("symbol=%s")
        params.append(symbol)
    if trade_date:
        where.append("trade_date=%s")
        params.append(trade_date)
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"SELECT * FROM trade_experiences WHERE {' AND '.join(where)} "
                f"ORDER BY trade_date DESC, entry_time DESC NULLS LAST LIMIT %s", params + [limit])
            return [_row_to_json(r) for r in cur.fetchall()]


def create_trade_decision_event(database_url, user_id, fields):
    """指示書2番：WAIT/ENTRY_READY/ENTRY/HOLD/EXIT_READY/EXIT/INVALIDATEDの各時点を保存する。"""
    pool = _get_pool(database_url)
    if pool is None or not (fields or {}).get("symbol") or not (fields or {}).get("decision_type"):
        return None
    cols = ["trade_experience_id", "event_time", "symbol", "decision_type", "price", "reason_json",
            "technical_snapshot_json", "market_snapshot_json"]
    json_cols = {"reason_json", "technical_snapshot_json", "market_snapshot_json"}
    values = [fields.get(c) for c in cols]
    wrapped = [json.dumps(v, ensure_ascii=False) if (c in json_cols and v is not None) else v
               for c, v in zip(cols, values)]
    placeholders = ["%s::jsonb" if c in json_cols else "%s" for c in cols]
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"INSERT INTO trade_decision_events (user_id, {', '.join(cols)}) "
                f"VALUES (%s, {', '.join(placeholders)}) RETURNING *", [user_id] + wrapped)
            row = cur.fetchone()
        conn.commit()
    return _row_to_json(row) if row else None


def list_trade_decision_events(database_url, user_id, trade_experience_id=None, symbol=None, limit=100):
    """指示書2・21番：時系列順（event_time ASC）で返す——WAIT→ENTRY_READY→ENTRY→…の順序を
    確認できるようにする。"""
    pool = _get_pool(database_url)
    if pool is None:
        return []
    where, params = ["user_id=%s"], [user_id]
    if trade_experience_id:
        where.append("trade_experience_id=%s")
        params.append(trade_experience_id)
    if symbol:
        where.append("symbol=%s")
        params.append(symbol)
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"SELECT * FROM trade_decision_events WHERE {' AND '.join(where)} "
                f"ORDER BY event_time ASC LIMIT %s", params + [limit])
            return [_row_to_json(r) for r in cur.fetchall()]


def create_trade_experience_rule_candidate(database_url, user_id, rule_text, category=None, action_text=None,
                                              evidence=None, extra_fields=None):
    """指示書9番：経験学習から抽出したルール候補をtrade_rules.status='RULE_CANDIDATE'として
    保存する（既存のupsert_trade_rule_from_text/_evaluate_rule_promotionは一切経由しない、
    自動昇格ロジックに巻き込まれないようにするため）。ACTIVEはおろかTESTINGへも自動では
    昇格しない——promote_trade_experience_rule_candidate()を明示的に呼んだ場合のみ。"""
    pool = _get_pool(database_url)
    if pool is None:
        return None
    rule_text = (rule_text or "").strip()
    if not rule_text:
        return None
    rule_key = _normalize_rule_key(rule_text)
    extra = extra_fields or {}
    today = datetime.date.today().isoformat()
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT id FROM trade_rules WHERE user_id=%s AND rule_key=%s", [user_id, rule_key])
            existing = cur.fetchone()
            if existing:
                return get_trade_rule(database_url, user_id, existing["id"])
            cur.execute(
                "INSERT INTO trade_rules (user_id, rule_key, title, rule_text, category, scope, rule_type, "
                "status, confidence, evidence_count, first_seen_date, last_seen_date, action_text, "
                "source_json, notes, created_from) "
                "VALUES (%s,%s,%s,%s,%s,%s,'TESTING','RULE_CANDIDATE',%s,%s,%s,%s,%s,%s::jsonb,%s,%s) RETURNING id",
                [user_id, rule_key, rule_text[:80], rule_text, category or "market", extra.get("scope") or "global",
                 extra.get("confidence") or "MEDIUM", extra.get("evidence_count") or 1, today, today, action_text,
                 json.dumps([evidence] if evidence else [], ensure_ascii=False), extra.get("notes"),
                 "trade_experience_learning"])
            new_id = cur.fetchone()["id"]
            cur.execute(
                "INSERT INTO trade_rule_history (user_id,rule_id,event_type,new_status,new_confidence,reason,source) "
                "VALUES (%s,%s,'CREATED',%s,%s,%s,%s)",
                [user_id, new_id, "RULE_CANDIDATE", extra.get("confidence") or "MEDIUM",
                 extra.get("reason") or "trade experience learningからの自動候補", "trade_experience_learning"])
        conn.commit()
    return get_trade_rule(database_url, user_id, new_id)


def promote_trade_experience_rule_candidate(database_url, user_id, rule_id, reason=None):
    """指示書9番：RULE_CANDIDATE→TESTINGはユーザー承認制。既存のupdate_trade_rule()（履歴記録
    込み）をそのまま再利用する——新しい昇格経路を別途作らない。RULE_CANDIDATE以外の状態の
    ルールには使わない（誤って他ステータスを巻き戻さないため）。"""
    rule = get_trade_rule(database_url, user_id, rule_id)
    if not rule or rule.get("status") != "RULE_CANDIDATE":
        return None
    ok = update_trade_rule(database_url, user_id, rule_id, {"status": "TESTING"},
                             reason=reason or "ユーザーがTrade Experience Learningの候補を承認",
                             source="trade_experience_learning_promote")
    return get_trade_rule(database_url, user_id, rule_id) if ok else None


def list_trade_experience_rule_candidates(database_url, user_id):
    """指示書9・14番：GET /api/trade-experience-patterns向けのRULE_CANDIDATE一覧。"""
    return list_trade_rules(database_url, user_id, status="RULE_CANDIDATE")


def upsert_trade_experience_by_sync_key(database_url, user_id, sync_key, fields):
    """「今日の振り返り」独立タブ化+15:30自動評価（2026-09-12新規、指示書29番）：sync_keyで
    冪等にupsertする。15:30スケジューラ・手動再生成・サーバー再起動・リトライが重なっても
    同一トレード/WAIT判断を重複登録しない（UNIQUE(user_id,sync_key)を利用したON CONFLICT）。"""
    pool = _get_pool(database_url)
    if pool is None or not sync_key or not (fields or {}).get("symbol"):
        return None
    cols = [c for c in _TRADE_EXPERIENCE_COLS if c in fields]
    values = [fields.get(c) for c in cols]
    wrapped = [json.dumps(v, ensure_ascii=False) if (c in _TRADE_EXPERIENCE_JSON_COLS and v is not None) else v
               for c, v in zip(cols, values)]
    insert_placeholders = ["%s::jsonb" if c in _TRADE_EXPERIENCE_JSON_COLS else "%s" for c in cols]
    update_clauses = [f"{c}=EXCLUDED.{c}" for c in cols] + ["updated_at=now()"]
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"INSERT INTO trade_experiences (user_id, sync_key, {', '.join(cols)}) "
                f"VALUES (%s, %s, {', '.join(insert_placeholders)}) "
                # 2026-09-14修正（不具合対応）：UNIQUE(user_id,sync_key)は
                # `WHERE sync_key IS NOT NULL`の部分インデックスのため、ON CONFLICT句にも
                # 同じWHERE述語を付けないとPostgresの制約推論が一致せず
                # 「no unique or exclusion constraint matching」で常に失敗していた
                # （この関数はsync_key必須＝呼び出し時点で常にNOT NULLのため実害はこれのみ）。
                f"ON CONFLICT (user_id, sync_key) WHERE sync_key IS NOT NULL "
                f"DO UPDATE SET {', '.join(update_clauses)} "
                f"RETURNING *", [user_id, sync_key] + wrapped)
            row = cur.fetchone()
        conn.commit()
    return _row_to_json(row) if row else None


def upsert_stock_behavior_profile(database_url, user_id, symbol, fields):
    """銘柄クセ学習（2026-09-12新規、指示書14・15番）：symbol単位で冪等にupsertする
    （UNIQUE(user_id,symbol)）。"""
    pool = _get_pool(database_url)
    if pool is None or not symbol:
        return None
    cols = ["stock_name", "sample_count", "avg_intraday_range_pct", "gap_up_frequency", "gap_down_frequency",
            "opening_30m_strength_rate", "morning_high_break_rate", "afternoon_high_break_rate",
            "afternoon_reversal_rate", "vwap_reclaim_success_rate", "vwap_loss_failure_rate",
            "oversold_reversal_rate", "breakout_followthrough_rate", "breakout_failure_rate",
            "pullback_success_rate", "late_day_momentum_rate", "late_day_fade_rate", "overnight_win_rate",
            "overnight_gap_down_rate", "avg_mfe_pct", "avg_mae_pct", "best_entry_time_bucket",
            "worst_entry_time_bucket", "time_bucket_stats_json", "preferred_setup_json",
            "danger_patterns_json", "confidence_level",
            "primary_driver", "primary_driver_corr", "primary_driver_lag", "cross_market_reliability",
            "best_sector_state_for_entry", "sector_leading_win_rate", "sector_weakening_loss_rate",
            "rotation_sensitivity"]
    json_cols = {"time_bucket_stats_json", "preferred_setup_json", "danger_patterns_json"}
    present = [c for c in cols if c in (fields or {})]
    if not present:
        return get_stock_behavior_profile(database_url, user_id, symbol)
    values = [fields.get(c) for c in present]
    wrapped = [json.dumps(v, ensure_ascii=False) if (c in json_cols and v is not None) else v
               for c, v in zip(present, values)]
    insert_placeholders = ["%s::jsonb" if c in json_cols else "%s" for c in present]
    update_clauses = [f"{c}=EXCLUDED.{c}" for c in present] + ["last_updated=now()"]
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"INSERT INTO stock_behavior_profiles (user_id, symbol, {', '.join(present)}) "
                f"VALUES (%s, %s, {', '.join(insert_placeholders)}) "
                f"ON CONFLICT (user_id, symbol) DO UPDATE SET {', '.join(update_clauses)} "
                f"RETURNING *", [user_id, symbol] + wrapped)
            row = cur.fetchone()
        conn.commit()
    return _row_to_json(row) if row else None


def get_stock_behavior_profile(database_url, user_id, symbol):
    pool = _get_pool(database_url)
    if pool is None:
        return None
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM stock_behavior_profiles WHERE user_id=%s AND symbol=%s", [user_id, symbol])
            row = cur.fetchone()
    return _row_to_json(row) if row else None


def update_daily_review_learning_scores(database_url, user_id, review_date, decision_quality_score, trade_result_score):
    """指示書6・7番：「判断品質」と「結果」を分けて保存する。既存score_total（既存5軸の合計、
    process quality寄り）には一切触れない追加専用の列。"""
    pool = _get_pool(database_url)
    if pool is None:
        return None
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "UPDATE daily_reviews SET decision_quality_score=%s, trade_result_score=%s, updated_at=now() "
                "WHERE user_id=%s AND review_date=%s RETURNING *",
                [decision_quality_score, trade_result_score, user_id, review_date])
            row = cur.fetchone()
        conn.commit()
    return _row_to_json(row) if row else None


def update_daily_review_choruco_score(database_url, user_id, review_date, choruco_score, choruco_breakdown):
    """Choruco Style（2026-09-12新規、指示書39・40番）：ちょる子式評価（100点、既存
    score_total等5軸とは別軸）を保存する。"""
    pool = _get_pool(database_url)
    if pool is None:
        return None
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "UPDATE daily_reviews SET choruco_score=%s, choruco_breakdown_json=%s::jsonb, updated_at=now() "
                "WHERE user_id=%s AND review_date=%s RETURNING *",
                [choruco_score, json.dumps(choruco_breakdown, ensure_ascii=False), user_id, review_date])
            row = cur.fetchone()
        conn.commit()
    return _row_to_json(row) if row else None


def mark_daily_review_finalized(database_url, user_id, review_date):
    """15:30自動評価スケジューラ（指示書4・11・28・29番）：確定生成の完了を記録する
    （is_finalized・generation_attempts）。daily_reviewsの行自体はgenerate_daily_review()が
    ON CONFLICTで作成済みという前提。"""
    pool = _get_pool(database_url)
    if pool is None:
        return None
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "UPDATE daily_reviews SET is_finalized=true, generation_attempts=generation_attempts+1, "
                "updated_at=now() WHERE user_id=%s AND review_date=%s RETURNING *", [user_id, review_date])
            row = cur.fetchone()
        conn.commit()
    return _row_to_json(row) if row else None


def reset_daily_review_finalized(database_url, user_id, review_date):
    """緊急修正（2026-09-16）：is_finalizedはmark_daily_review_finalized()でしかtrueに
    ならない一方向のラチェットになっており、一度（誤って）FINAL化されると通常の再生成
    （PROVISIONAL）だけでは戻らないバグがあった。session_closed=False（大引け前）で
    generate_daily_review_with_learning()が呼ばれた場合、この関数でis_finalizedを
    強制的にfalseへ戻す——「場中に生成されたスコアをFINALな学習結果として残さない」
    （指示書8番）を、過去に誤って確定されたレビューに対しても自己修復する形で満たす。
    行が存在しない場合は何もしない（generate_daily_review()が先に行を作成済みの前提）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return None
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "UPDATE daily_reviews SET is_finalized=false, updated_at=now() "
                "WHERE user_id=%s AND review_date=%s AND is_finalized=true RETURNING *", [user_id, review_date])
            row = cur.fetchone()
        conn.commit()
    return _row_to_json(row) if row else None


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


# ============================================================
# ---- ChatGPT統合連携（2026-09-09新規、指示書Phase1）----
# 「投資ログ取り込み」から「ChatGPT統合インポート」へ役割拡張。1つのJSONに複数種別の情報
# （market/watchlist/decisions/rule_updates/review＝既存必須項目、events/news/
# expert_opinions/catalysts/user_feedback/position_review＝今回追加の任意項目）が
# 混在していても、アプリ側で内容を判定して既存の各保存先（save_chatgpt_import・
# import_market_events・import_news_catalysts・import_expert_views・daily_reviews）へ
# 自動振り分けする。新しい保存ロジック・重複排除ロジックは作らず、既存の各関数（イベント・
# カタリスト・有識者は既にPHASE3〜5で構築済み）をそのまま呼ぶだけ（指示書「既存の重複排除
# ロジックは可能な限り再利用する」）。
# ============================================================

def _classify_single_item(item):
    """1件のオブジェクトを内容（キー形状）から分類する（指示書4番）。優先順位付き
    ヒューリスティックのみ（AI不使用）。判定できなければNoneを返す（無理に分類しない）。"""
    if not isinstance(item, dict):
        return None
    keys = set(item.keys())
    if "expert_name" in keys and (keys & {"thesis", "confirmations", "invalidation_conditions", "outlook"}):
        return "expert_opinions"
    if (keys & {"event_date", "event_type"}) or ({"event", "importance"} <= keys):
        return "events"
    if "headline" in keys or ("title" in keys and (keys & {"source", "published_at"})):
        return "news"
    if "rule" in keys and (keys & {"action", "confidence", "status"}):
        return "rule_updates"
    if "code" in keys and "action" in keys and (keys & {"reason", "risk", "time_horizon"}):
        return "decisions"
    if {"code", "name"} <= keys and (keys & {"stance", "view", "theme", "priority"}):
        return "watchlist"
    return None


# 統合payloadの既知トップレベルキー（標準envelope＋今回追加の任意キー）。この集合に無い
# キーで値が配列のものだけ、_classify_single_item()による内容判定の対象にする（指示書4番）。
_UNIFIED_KNOWN_KEYS = {
    "schema_version", "type", "date", "market", "watchlist", "decisions", "review",
    "rule_updates", "events", "news", "expert_opinions", "catalysts",
    "user_feedback", "position_review", "notes", "trade_playbooks",
}


def classify_chatgpt_unified_payload(payload):
    """統合ChatGPT連携の中核（指示書2〜5番）。DBへの書き込みは行わない純粋関数——
    プレビュー表示（指示書5番）と実保存（save_chatgpt_unified_import）の両方から呼ぶ。
    既存の必須キー（date/market/watchlist/decisions/review/rule_updates）が無くても
    正常動作し（指示書3番）、今回追加の任意キー（events/news/expert_opinions/catalysts/
    user_feedback/position_review）が無くても正常動作する。
    戻り値: {"buckets": {...}, "counts": {...}}"""
    if not isinstance(payload, dict):
        payload = {}
    market = payload.get("market") if isinstance(payload.get("market"), dict) else {}
    watchlist = list(payload.get("watchlist")) if isinstance(payload.get("watchlist"), list) else []
    decisions = list(payload.get("decisions")) if isinstance(payload.get("decisions"), list) else []
    rule_updates = list(payload.get("rule_updates")) if isinstance(payload.get("rule_updates"), list) else []
    events = list(payload.get("events")) if isinstance(payload.get("events"), list) else []
    news = list(payload.get("news")) if isinstance(payload.get("news"), list) else []
    expert_opinions = list(payload.get("expert_opinions")) if isinstance(payload.get("expert_opinions"), list) else []
    catalysts = list(payload.get("catalysts")) if isinstance(payload.get("catalysts"), list) else []
    user_feedback = payload.get("user_feedback") or ""
    position_review = payload.get("position_review") if isinstance(payload.get("position_review"), dict) else {}
    review = payload.get("review") or ""
    # 2026-09-09追加（判断エンジン強化、指示書8番）：trade_playbooksも任意キーとして受け付ける。
    trade_playbooks = list(payload.get("trade_playbooks")) if isinstance(payload.get("trade_playbooks"), list) else []

    unclassified = 0
    for k, v in payload.items():
        if k in _UNIFIED_KNOWN_KEYS or not isinstance(v, list):
            continue
        for item in v:
            bucket = _classify_single_item(item)
            if bucket == "watchlist":
                watchlist.append(item)
            elif bucket == "decisions":
                decisions.append(item)
            elif bucket == "rule_updates":
                rule_updates.append(item)
            elif bucket == "events":
                events.append(item)
            elif bucket == "news":
                news.append(item)
            elif bucket == "expert_opinions":
                expert_opinions.append(item)
            else:
                unclassified += 1

    buckets = {
        "market_summary": market or None, "watchlist": watchlist, "decisions": decisions,
        "rule_updates": rule_updates, "events": events, "news": news,
        "expert_opinions": expert_opinions, "catalysts": catalysts,
        "user_feedback": user_feedback, "review": review, "position_review": position_review,
        "trade_playbooks": trade_playbooks,
    }
    counts = {
        "market_summary": 1 if market else 0, "watchlist": len(watchlist), "decisions": len(decisions),
        "rule_updates": len(rule_updates), "events": len(events), "news": len(news),
        "expert_opinions": len(expert_opinions), "catalysts": len(catalysts),
        "user_feedback": 1 if user_feedback else 0, "review": 1 if review else 0,
        "trade_playbooks": len(trade_playbooks), "unclassified": unclassified,
    }
    return {"buckets": buckets, "counts": counts}


def _map_news_item_to_catalyst(item):
    """統合連携の"news"バケット（headline/title/source/published_at/impact/sentiment/
    affected_codes等、指示書8番）を、既存news_catalystsテーブルが期待する形
    （catalyst_date/title必須）へ変換する。ニュースと材料（カタリスト）は「株価に影響しうる
    情報」という意味で同じ土台のため、新しいテーブルは作らず既存news_catalystsへ統合する。"""
    date = item.get("date") or item.get("catalyst_date") or (item.get("published_at") or "")[:10] or None
    codes = item.get("affected_codes") or item.get("codes") or ([item["code"]] if item.get("code") else [])
    return {
        "catalyst_date": date,
        "title": item.get("headline") or item.get("title"),
        "summary": item.get("summary") or item.get("detail"),
        "sentiment": item.get("sentiment"),
        "importance": item.get("importance"),
        "affected_stocks": codes,
        "affected_sectors": item.get("affected_sectors") or ([item["sector"]] if item.get("sector") else []),
        "source": item.get("source") or "ChatGPT統合連携",
    }


def save_chatgpt_unified_import(database_url, user_id, payload, force=False):
    """統合ChatGPT連携の保存処理（指示書1・2番、ユーザー向けの唯一の取り込み口）。
    既存save_chatgpt_import()（daily_log/stock_judgments/investment_rules/trade_rules
    保存、無変更）をそのまま呼び、追加でevents/news/catalysts/expert_opinionsが
    あれば既存のimport_market_events/import_news_catalysts/import_expert_viewsへ
    振り分け、user_feedbackがあればdaily_reviewsへ保存する。戻り値は既存
    save_chatgpt_import()の戻り値に"unified"（各バケットのimport結果）と
    "classification"（分類件数）を追加したもの。"""
    base_result = save_chatgpt_import(database_url, user_id, payload, force=force)
    if "error" in base_result:
        return base_result
    classified = classify_chatgpt_unified_payload(payload)
    buckets = classified["buckets"]
    unified = {}
    if buckets["events"]:
        unified["events"] = import_market_events(database_url, user_id, buckets["events"])
    if buckets["news"]:
        unified["news"] = import_news_catalysts(database_url, user_id, [_map_news_item_to_catalyst(n) for n in buckets["news"]])
    if buckets["catalysts"]:
        unified["catalysts"] = import_news_catalysts(database_url, user_id, buckets["catalysts"])
    if buckets["expert_opinions"]:
        unified["expert_opinions"] = import_expert_views(database_url, user_id, buckets["expert_opinions"])
    if buckets["user_feedback"]:
        review_date = payload.get("date") or datetime.date.today().isoformat()
        save_review_user_feedback(database_url, user_id, review_date, buckets["user_feedback"], source="chatgpt_import")
        unified["user_feedback"] = {"saved": True, "date": review_date}
    if buckets.get("trade_playbooks"):
        unified["trade_playbooks"] = import_trade_playbooks(database_url, user_id, buckets["trade_playbooks"])
    base_result["unified"] = unified
    base_result["classification"] = classified["counts"]
    return base_result


# ============================================================
# ---- daily_reviews（日次投資レビュー・投資スコア。2026-09-09新規、指示書Phase4・5）----
# ポジション・売買履歴・持ち越し・ルール遵守から1日単位で自動評価する。「儲かった＝高得点」
# にしない（指示書14番）ため、生のPnLはスコアの直接入力にせず、実データ（portfolio/
# trade_history/trade_rules）から検証できる具体的なチェック結果だけを積み上げる設計にした。
# ============================================================

RULE_ADHERENCE_MAX = 25
ENTRY_QUALITY_MAX = 20
EXIT_QUALITY_MAX = 20
MARKET_FIT_MAX = 15
RISK_MGMT_MAX = 10
REFLECTION_MAX = 10
DEDUCTION_RULE_VIOLATION = 8
DEDUCTION_TEMPORARY_RULE_VIOLATION = 15  # 指示書17番：例外ルールの期限超過持ち越しは強く減点

_REFLECTION_TAG_KEYWORDS = [
    ("FOMO", ["飛びつき", "焦って", "乗り遅れ", "FOMO", "fomo"]),
    ("高値追い", ["高値追い", "高値掴み", "追いかけて買"]),
    ("損切り遅れ", ["損切りが遅れ", "損切り遅れ", "塩漬け", "ロスカットが遅"]),
    ("利確遅れ", ["利確を逃", "利確が遅れ", "欲張っ", "もっと上がると思っ"]),
    ("ナンピン", ["ナンピン"]),
    ("過信", ["自信があったので", "過信", "大丈夫だと思っ"]),
    ("地合い無視", ["地合いを無視", "地合いが悪い中", "地合いに逆行"]),
    ("ルール違反", ["ルールに反", "ルール違反", "原則から外れ", "例外扱いにして"]),
    ("良い判断", ["良い判断", "冷静に判断", "計画通り"]),
    ("冷静な見送り", ["見送った", "様子見にした", "無理せず"]),
]
# 上のフレーズ完全一致だけでは「損切りも少し遅れた気がする」のように助詞・修飾語が挟まる
# 自然な言い回しを取りこぼすため、タグごとに「全て含まれていれば良い」語のANDパターンも
# 併用する（形態素解析はしない軽量な補完、指示書20番のキーワード方式の範囲内）。
_REFLECTION_TAG_AND_KEYWORDS = [
    ("損切り遅れ", [["損切り", "遅れ"], ["ロスカット", "遅"]]),
    ("利確遅れ", [["利確", "遅れ"], ["利確", "逃し"]]),
    ("高値追い", [["高値", "追"]]),
    ("地合い無視", [["地合い", "無視"], ["地合い", "逆行"]]),
]


def extract_reflection_tags(text):
    """ユーザー感想からタグを抽出する（指示書20番、キーワード方式・AI不使用）。"""
    if not text:
        return []
    tags = {tag for tag, kws in _REFLECTION_TAG_KEYWORDS if any(k in text for k in kws)}
    for tag, patterns in _REFLECTION_TAG_AND_KEYWORDS:
        if any(all(k in text for k in pat) for pat in patterns):
            tags.add(tag)
    # 元のリスト順を維持して返す（表示の安定性のため）
    order = [t for t, _ in _REFLECTION_TAG_KEYWORDS]
    return [t for t in order if t in tags] + [t for t in tags if t not in order]


def _check_rule_adherence(database_url, user_id, review_date, positions, rules, session_closed=True):
    """ルール遵守（25点満点）。実データ（保有中ポジション・trade_rules）から機械的に
    チェックできるものだけを対象にする（指示書14番：単純な損益判定はしない）。
    緊急修正（2026-09-16）：session_closed=False（review_dateがまだ大引け前）の間は
    持ち越し（day_positions）判定を完全にスキップする——
    `09:30 ENTRY → 13:00保有中 → 14:30 EXIT`のような通常のデイトレを、場中に生成した
    レビューが「持ち越し原則ルール違反」と誤判定しないため（指示書1・3・6番）。
    逆指値未設定チェック（3番）はENTRY時点で判断可能な項目のため対象外——持ち越し概念とは
    無関係、場中でも従来通り評価する。"""
    good, bad = [], []
    score = RULE_ADHERENCE_MAX
    active_rules = [r for r in rules if r["status"] == "ACTIVE"]
    temp_rules = [r for r in rules if r["rule_type"] == "TEMPORARY"]

    # 1) 持ち越し原則なし系ルール：trade_style=DAY（デイトレ）の建玉が保有中＝持ち越し発生。
    #    その銘柄コードを名指しした有効なTEMPORARY例外ルールが無ければ違反とみなす。
    #    session_closed=Falseの間（review_dateがまだ大引け前）はこのブロック自体を丸ごと
    #    スキップする——「保有中＝持ち越し」ではなく「大引けを越えてなお保有中＝持ち越し」
    #    が正しい定義（指示書3番）。
    no_carry_active = session_closed and any("持ち越し" in r["rule_text"] and "原則" in r["rule_text"] for r in active_rules)
    if no_carry_active:
        day_positions = [p for p in positions if (p.get("trade_style") or "").upper() == "DAY"]
        for p in day_positions:
            covered = any(t["scope"] == "stock" and p["code"] in (t.get("rule_text") or "")
                          and t["status"] not in ("EXPIRED", "RETIRED") for t in temp_rules)
            label = p.get("name") or p["code"]
            if covered:
                good.append(f"{label}：デイトレ持ち越しだが例外ルールとして明示登録済み")
            else:
                score -= DEDUCTION_RULE_VIOLATION
                bad.append(f"{label}：デイトレ想定の建玉を例外登録なしで持ち越し（持ち越し原則ルール違反）")

    # 2) TEMPORARYルールの期限切れ後も対象銘柄を持ち越している場合（指示書17番の例そのもの）
    held_codes = {p["code"] for p in positions}
    for t in temp_rules:
        if t["status"] == "EXPIRED" and t.get("scope") == "stock":
            mentioned = [c for c in held_codes if c in (t.get("rule_text") or "")]
            if mentioned:
                score -= DEDUCTION_TEMPORARY_RULE_VIOLATION
                bad.append(f"例外ルール『{t['rule_text'][:40]}…』の期限超過後も持ち越しを継続（例外違反）")

    # 3) 逆指値必須ルールがACTIVEなのに、保有銘柄に損切りライン未設定のものがある
    stop_required = any("逆指値" in r["rule_text"] for r in active_rules)
    if stop_required and positions:
        no_stop = [p for p in positions if p.get("current_stop") is None and p.get("initial_stop") is None]
        if no_stop:
            deduct = min(DEDUCTION_RULE_VIOLATION, len(no_stop) * 3)
            score -= deduct
            bad.append(f"{len(no_stop)}銘柄で損切りライン（逆指値）が未設定")
        else:
            good.append("保有銘柄は全て損切りラインを設定済み")

    return max(0, min(RULE_ADHERENCE_MAX, score)), good, bad


def _check_entry_quality(new_positions):
    """エントリー品質（20点満点）：その日新規に持ったポジションが、初期損切り・利確目標を
    決めた上で入っているか（計画性）を見る。現在値との比較等の厳密な「高値掴みだったか」判定は
    リアルタイムスナップショットが無いと不可能なため、v1では計画性チェックに限定する。"""
    if not new_positions:
        return ENTRY_QUALITY_MAX, ["本日の新規エントリーなし（判定対象外、満点扱い）"], []
    good, bad = [], []
    score = ENTRY_QUALITY_MAX
    per_item = ENTRY_QUALITY_MAX / max(1, len(new_positions))
    for p in new_positions:
        label = p.get("name") or p["code"]
        missing = [n for n, v in [("初期損切り", p.get("initial_stop")), ("利確目標", p.get("target_1"))] if v is None]
        if missing:
            score -= per_item * (len(missing) / 2)
            bad.append(f"{label}：エントリー時に{('・'.join(missing))}が未設定のまま建玉化")
        else:
            good.append(f"{label}：損切り・利確目標を決めてからエントリー")
    return max(0, round(score)), good, bad


def _check_exit_quality(exits_today, reflection_tags):
    """利確・損切り（20点満点）：当日の決済（trade_history）を評価する。損失決済＝悪い、では
    なく、損切り遅れ等の反省タグが無ければ「計画通りの損切り」として扱う（指示書15番の
    「損失でも正しい損切りなら高評価可能」の実装）。"""
    if not exits_today:
        return EXIT_QUALITY_MAX, ["本日の決済なし（判定対象外、満点扱い）"], []
    good, bad = [], []
    score = EXIT_QUALITY_MAX
    per_item = EXIT_QUALITY_MAX / max(1, len(exits_today))
    late_exit_flagged = "損切り遅れ" in reflection_tags
    greedy_flagged = "利確遅れ" in reflection_tags
    for t in exits_today:
        label = t.get("name") or t["code"]
        pnl = t.get("net_pnl") if t.get("net_pnl") is not None else t.get("pnl")
        if pnl is not None and pnl < 0:
            if late_exit_flagged:
                score -= per_item
                bad.append(f"{label}：損失決済かつ本人の振り返りで「損切り遅れ」を自己申告")
            else:
                good.append(f"{label}：損失決済だが計画的な損切りとして処理（自己申告の遅れ報告なし）")
        else:
            if greedy_flagged:
                score -= per_item * 0.5
                bad.append(f"{label}：利益確定だが本人の振り返りで「利確遅れ」を自己申告")
            else:
                good.append(f"{label}：利益確定")
    return max(0, round(score)), good, bad


def _check_market_fit(new_positions, market_condition, is_business_day=True):
    """地合い適応（15点満点）：地合いが軟調（リスクオフ等）な日に新規エントリーを増やして
    いないかを見る。market_conditionはdaily_log（PRIVATE）またはSHARED market_intelligence_
    reportsから解決済みの自由記述テキスト（_resolve_market_condition_for_review参照）で、
    キーワードで軽く判定する（厳密な数値判定はしない）。
    2026-09-14修正（不具合対応）：データ不足を満点扱いしていた旧仕様（「地合い情報が未記録の
    ため判定不能（満点扱い）」）を廃止。市場休場日・データ不足はともに満点を与えず、戻り値の
    4つ目（applicable_max）を0にして生成元（generate_daily_review）の分母から除外させる。
    戻り値: (score, good_points, improvement_points, applicable_max)"""
    if not is_business_day:
        return 0, [], ["市場休場日のため評価対象外"], 0
    if not market_condition:
        return 0, [], ["地合い評価：データ不足のため未評価"], 0
    # 2026-09-14修正（SHARED market_intelligence_reportsフォールバック対応）：daily_log自由記述の
    # 日本語表現に加え、market_intelligence_reports.market_regime（RISK_OFF|MILD_RISK_OFF|
    # HIGH_VOLATILITY等の英語enum、server.pyのgenerate_morning_strategy参照）もリスクオフ判定
    # できるようにする（旧実装は日本語キーワードしか見ておらず、SHARED経由の地合いを
    # 常に「良好」と誤判定していた）。
    risk_off = any(k in market_condition for k in
                    ["リスクオフ", "軟調", "弱い", "急落", "下落", "RISK_OFF", "HIGH_VOLATILITY"])
    if not risk_off:
        return MARKET_FIT_MAX, [f"地合い評価：良好（{market_condition}の下で通常運用）"], [], MARKET_FIT_MAX
    if not new_positions:
        return MARKET_FIT_MAX, [f"地合い評価：警戒（{market_condition}軟調の中、新規エントリーを抑制）"], [], MARKET_FIT_MAX
    deduct = min(MARKET_FIT_MAX, len(new_positions) * 5)
    return (max(0, MARKET_FIT_MAX - deduct), [],
            [f"地合い評価：不一致（{market_condition}軟調にも関わらず新規{len(new_positions)}件エントリー）"], MARKET_FIT_MAX)


# Phase MU-S2続き（2026-09-14・不具合対応）：market_intelligence_reportsはMU-S1でSHARED化
# 済みのため、report_type優先順位で「その日の最終的な地合い」を1件だけ選ぶ。daily_review
# （PRIVATE）とはuser_idでJOINしない——date（trade_date=review_date）だけで関連付ける
# （指示書：PRIVATEとSHAREDはuser_idでJOINしてはいけない。list_market_intelligence_reports
# 自体がMU-S1でuser_id引数を無視し常に_SHARED_SCOPEを見る設計のため、ここで渡すuser_idの
# 値は実質無視される＝安全）。
_MARKET_CONDITION_REPORT_PRIORITY = ("MARKET_CLOSE", "AFTERNOON_30M", "MORNING_CLOSE", "OPENING_30M")


def _resolve_market_condition_for_review(database_url, user_id, review_date):
    """その日の地合い判定材料を1つに解決する。優先順位：
    1) daily_log.market_env（PRIVATE、ユーザーが自分で書いた自由記述）
    2) market_intelligence_reports（SHARED）を優先度の高いreport_type順に見る
    見つからなければNone（=データ不足、呼び出し側で「データ不足のため未評価」扱い）。
    戻り値: market_condition文字列 または None。"""
    pool = _get_pool(database_url)
    if pool is None:
        return None
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT market_env FROM daily_log WHERE user_id=%s AND date=%s ORDER BY id DESC LIMIT 1",
                        [user_id, review_date])
            row = cur.fetchone()
            if row and row.get("market_env"):
                return row["market_env"]
    reports = list_market_intelligence_reports(database_url, user_id, trade_date=review_date)
    if not reports:
        return None
    by_type = {r.get("report_type"): r for r in reports}
    chosen = None
    for rt in _MARKET_CONDITION_REPORT_PRIORITY:
        if rt in by_type:
            chosen = by_type[rt]
            break
    if chosen is None:
        chosen = reports[-1]  # 優先順位に無いreport_typeでも、当日分があれば無いよりまし
    regime = chosen.get("market_regime")
    summary = chosen.get("market_summary")
    if not regime and not summary:
        return None
    if regime and summary:
        return f"{regime}：{summary}"
    return regime or summary


def _check_risk_management(database_url, user_id, review_date):
    """リスク管理（10点満点）：その日にFAILED評価されたriskカテゴリのルールがあれば減点する
    （既存のルール学習システムと接続、指示書10番）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return RISK_MGMT_MAX, [], []
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT h.reason, r.rule_text FROM trade_rule_history h JOIN trade_rules r ON r.id=h.rule_id "
                "WHERE h.user_id=%s AND h.event_type='EVALUATION' AND h.eval_result='FAILED' "
                "AND h.eval_date=%s AND r.category='risk'", [user_id, review_date])
            fails = cur.fetchall()
    if not fails:
        return RISK_MGMT_MAX, [], []
    deduct = min(RISK_MGMT_MAX, len(fails) * 5)
    return max(0, RISK_MGMT_MAX - deduct), [], [f"リスク関連ルール『{f['rule_text'][:30]}…』がFAILED評価" for f in fails]


_JST = datetime.timezone(datetime.timedelta(hours=9))


def _to_jst_date_str(ts):
    """timestamp文字列（TIMESTAMPTZ由来、UTC想定）をJSTの日付（YYYY-MM-DD）文字列にして返す。
    2026-09-14修正（不具合対応）：単純に文字列の先頭10文字を取るとUTC日付になってしまい、
    JST 00:00〜08:59（=UTC前日15:00〜23:59）に約定した取引がJSTでの実際の日付より1日
    前の日として扱われてしまう（例：JST 2026-09-14 00:55の取得がUTCでは2026-09-13 15:55と
    なり、[:10]切り出しだとreview_date="2026-09-13"の持ち越し判定に誤って含まれ得る）。
    パース失敗時・空文字時は空文字を返す（呼び出し側で「日付不明」として扱われる）。"""
    if not ts:
        return ""
    try:
        dt = datetime.datetime.fromisoformat(str(ts))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(_JST).date().isoformat()
    except (ValueError, TypeError):
        return str(ts)[:10]


def generate_daily_review(database_url, user_id, review_date, user_feedback=None, is_business_day=None,
                           session_closed=True):
    """指示書13〜17番：1日の投資振り返りを自動生成し、1〜100点で評価する。既存の
    ChatGPT取込・trade_rules・portfolio・trade_historyのデータだけを使い、新しい判定
    ロジックを勝手に「賢く」しすぎない（機械的に検証できる項目だけを積み上げる設計）。
    is_business_day：呼び出し元（server.py）がJP祝日カレンダー込みのis_jp_trading_day()で
    正確に判定できるならその結果を渡す。省略時（None）はinvestment_db側で土日だけの簡易判定
    にフォールバックする（祝日カレンダーはserver.py側にしか無いため）。
    緊急修正（2026-09-16）：session_closed（review_dateの大引けを過ぎたか、server.py
    can_finalize_daily_review()と同じ基準）。デフォルトTrue＝過去日の再生成等の既存呼び出し
    パターンとの後方互換のため。当日の場中に呼ばれた場合のみFalseが渡り、
    _check_rule_adherence()の持ち越し（day_positions）判定を一時的に無効化する
    （「position exists at review generation time」だけで持ち越しにしない、指示書3番）。
    戻り値: 保存済みdaily_reviewsの1行（camelCase変換済み）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return None
    if is_business_day is None:
        try:
            is_business_day = datetime.date.fromisoformat(review_date).weekday() < 5
        except (ValueError, TypeError):
            is_business_day = True
    positions = list_portfolio(database_url, user_id)
    history = list_trade_history(database_url, user_id, limit=500)
    rules = list_trade_rules(database_url, user_id)

    # 2026-09-14修正（不具合対応）：list_portfolio()は「今この瞬間」のポジションを返すため、
    # 過去日のreview_dateを生成する際にそのまま使うと、今日買った銘柄が過去日のレビューへ
    # 逆流してしまう（例：本日新規で買った銘柄が「9/11から持ち越していた」と誤判定される）。
    # acquired_at（無ければcreated_at）がreview_date以前のものだけを「その日時点で保有して
    # いた可能性がある」ポジションとして扱う。
    #
    # 2026-09-14再修正（Sansan持ち越し誤判定バグ対応）：上記だけでは不十分だった。
    # 「今この瞬間」activeな行を使う限り、review_dateの大引け後にまだ売却がこのアプリへ
    # 記録されていない（=約定はしたが手動記録がまだの）間にレビューが生成されると、
    # 当日中に引け成売りで手仕舞った建玉が「持ち越し」と誤判定されてしまう
    # （15:30〜15:35に自動生成する_daily_review_scheduler_loopと、引け成売りの手動記録
    # タイミングとの間に競合が起きるため）。正しい判定条件は
    #   acquired_at <= review_date終了時点 AND (closed_at is NULL OR closed_at > review_date終了時点)
    # ＝「その日の終わりまで未決済だったか」であり、「その日に取得済みだったか」だけでは
    # 足りない。trade_history（決済済み）側にも acquired_at <= review_date かつ
    # closed_at > review_date（＝review_date当日には手仕舞わず、後日に持ち越して決済した）の
    # 行があれば「review_date時点で保有していた」ものとして合流させ、反対にreview_date当日
    # またはそれ以前に決済済み（closed_at <= review_date）の行は保有扱いから除外する
    # （同日決済＝持ち越しではない、が最優先）。
    # 既知の制約：この再構成はtrade_history.acquired_at/trade_style（2026-09-14新設列）に
    # 依存するため、それ以前に決済済みで列がNULLのまま残っている古いtrade_history行は
    # 再構成できない（従来通り「追跡不可」のまま）。また、activeなportfolio行自体が
    # 「review_dateには保有していたが今日までに手仕舞われ削除済み」のケースは、
    # 既にtrade_history側から拾えるためlist_portfolio()の現在値と二重計上しないよう
    # コード単位でtrade_history側を優先除外する。
    exited_by_review_end = {
        t["code"] for t in history
        if _to_jst_date_str(t.get("closed_at")) and _to_jst_date_str(t.get("closed_at")) <= review_date
    }
    active_as_of_review = [p for p in positions
                            if _to_jst_date_str(p.get("acquired_at") or p.get("created_at")) <= review_date
                            and p["code"] not in exited_by_review_end]
    carried_from_history = [
        {**t, "acquired_at": t.get("acquired_at"), "current_stop": None, "initial_stop": None, "target_1": None}
        for t in history
        if t.get("acquired_at")
        and _to_jst_date_str(t.get("acquired_at")) <= review_date
        and _to_jst_date_str(t.get("closed_at")) > review_date
    ]
    positions_as_of_review = active_as_of_review + carried_from_history

    exits_today = [t for t in history if _to_jst_date_str(t.get("closed_at")) == review_date]
    new_positions = [p for p in positions_as_of_review
                      if _to_jst_date_str(p.get("acquired_at") or p.get("created_at")) == review_date]

    # 2026-09-14修正（不具合対応）：market_conditionはPRIVATE daily_logで見つからなければ
    # SHARED market_intelligence_reportsへフォールバックする（_resolve_market_condition_for_review
    # 参照。date一致だけで関連付け、user_idではJOINしない）。
    market_condition = _resolve_market_condition_for_review(database_url, user_id, review_date)
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            # 既存のuser_feedback（同日、まだ無ければNone）
            cur.execute("SELECT user_feedback FROM daily_reviews WHERE user_id=%s AND review_date=%s",
                        [user_id, review_date])
            existing = cur.fetchone()
    effective_feedback = user_feedback if user_feedback is not None else (existing.get("user_feedback") if existing else None)
    reflection_tags = extract_reflection_tags(effective_feedback)

    score_rule, good_rule, bad_rule = _check_rule_adherence(database_url, user_id, review_date, positions_as_of_review,
                                                              rules, session_closed=session_closed)
    score_entry, good_entry, bad_entry = _check_entry_quality(new_positions)
    score_exit, good_exit, bad_exit = _check_exit_quality(exits_today, reflection_tags)
    score_market, good_market, bad_market, market_applicable_max = _check_market_fit(
        new_positions, market_condition, is_business_day=is_business_day)
    score_risk, good_risk, bad_risk = _check_risk_management(database_url, user_id, review_date)
    score_reflection = REFLECTION_MAX if (effective_feedback or "").strip() else 4

    # 2026-09-09新規（判断エンジン強化、指示書26番）：その日のanalysis_context_logで
    # NO_OVERNIGHT等の警告が出ていたのに実際に持ち越した銘柄を検出し、「知っていたのに
    # 無視した」としてルール遵守点をさらに減点する（結果論ではなく、その時点で警告が
    # 出ていたかどうかで判定、指示書27番）。
    known_risk_ignored = _check_known_risk_ignored(database_url, user_id, review_date, positions_as_of_review, new_positions=new_positions)
    if known_risk_ignored:
        score_rule = max(0, score_rule - DEDUCTION_RULE_VIOLATION * len(known_risk_ignored))
        bad_rule = bad_rule + [f"{k['code']}：{k['reason']}（知っていたのに無視）" for k in known_risk_ignored]

    # 2026-09-09新規（判断エンジン全画面統合、指示書18番）：その日の決済がplaybookの
    # 回避条件発動を無視していなかったか／playbookを参照して判断していたかを反映する。
    good_pb, bad_pb = _check_playbook_discipline(database_url, user_id, exits_today)
    good_entry = good_entry + good_pb
    bad_rule = bad_rule + bad_pb

    # 2026-09-14修正（不具合対応）：地合い適応がデータ不足/休場日で評価対象外
    # （market_applicable_max=0）の場合、100点満点の分母からも除外し、残りの項目だけで
    # 100点相当に比例配分する（「データ不足を満点扱いしない」＝分子にも分母にも入れない）。
    raw_total = score_rule + score_entry + score_exit + score_market + score_risk + score_reflection
    applicable_max = (RULE_ADHERENCE_MAX + ENTRY_QUALITY_MAX + EXIT_QUALITY_MAX + market_applicable_max
                       + RISK_MGMT_MAX + REFLECTION_MAX)
    score_total = round(raw_total / applicable_max * 100) if applicable_max > 0 else 0
    good_points = good_rule + good_entry + good_exit + good_market + good_risk
    improvement_points = bad_rule + bad_entry + bad_exit + bad_market + bad_risk

    tomorrow_notes = []
    for t in temp_rules_expiring_soon(rules, review_date):
        tomorrow_notes.append(f"{t['rule_text'][:40]}…（期限{t.get('expires_date')}）を必ず順守")
    if "損切り遅れ" in reflection_tags:
        tomorrow_notes.append("含み損ポジションは早めの損切り判断を意識する")
    if "利確遅れ" in reflection_tags:
        tomorrow_notes.append("含み益ポジションは目標到達で機械的に利確する")
    if "高値追い" in reflection_tags or "FOMO" in reflection_tags:
        tomorrow_notes.append("急騰銘柄への飛び乗りエントリーを控える")

    market_fit_label = f"{score_market}/{market_applicable_max}" if market_applicable_max > 0 else "評価対象外"
    auto_summary = f"今日の投資スコア：{score_total}/100（ルール遵守{score_rule}/{RULE_ADHERENCE_MAX}・" \
        f"エントリー{score_entry}/{ENTRY_QUALITY_MAX}・利確損切り{score_exit}/{EXIT_QUALITY_MAX}・" \
        f"地合い適応{market_fit_label}・リスク管理{score_risk}/{RISK_MGMT_MAX}・" \
        f"振り返り{score_reflection}/{REFLECTION_MAX}）"

    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "INSERT INTO daily_reviews (user_id, review_date, score_total, score_rule_adherence, "
                "score_entry_quality, score_exit_quality, score_market_fit, score_risk_mgmt, "
                "score_reflection, good_points, improvement_points, tomorrow_notes, auto_summary, "
                "user_feedback, reflection_tags, known_risk_ignored_json, updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s,%s::jsonb,%s::jsonb,now()) "
                "ON CONFLICT (user_id, review_date) DO UPDATE SET "
                "score_total=EXCLUDED.score_total, score_rule_adherence=EXCLUDED.score_rule_adherence, "
                "score_entry_quality=EXCLUDED.score_entry_quality, score_exit_quality=EXCLUDED.score_exit_quality, "
                "score_market_fit=EXCLUDED.score_market_fit, score_risk_mgmt=EXCLUDED.score_risk_mgmt, "
                "score_reflection=EXCLUDED.score_reflection, good_points=EXCLUDED.good_points, "
                "improvement_points=EXCLUDED.improvement_points, tomorrow_notes=EXCLUDED.tomorrow_notes, "
                "auto_summary=EXCLUDED.auto_summary, "
                "user_feedback=COALESCE(daily_reviews.user_feedback, EXCLUDED.user_feedback), "
                "reflection_tags=EXCLUDED.reflection_tags, known_risk_ignored_json=EXCLUDED.known_risk_ignored_json, "
                "updated_at=now() "
                "RETURNING *",
                [user_id, review_date, score_total, score_rule, score_entry, score_exit, score_market,
                 score_risk, score_reflection, json.dumps(good_points, ensure_ascii=False),
                 json.dumps(improvement_points, ensure_ascii=False), json.dumps(tomorrow_notes, ensure_ascii=False),
                 auto_summary, effective_feedback, json.dumps(reflection_tags, ensure_ascii=False),
                 json.dumps(known_risk_ignored, ensure_ascii=False)])
            saved = cur.fetchone()
        conn.commit()
    return _row_to_json(saved)


def temp_rules_expiring_soon(rules, review_date, days=1):
    """TEMPORARYルールのうちreview_date基準でdays日以内に期限が来るものを返す（指示書21番
    「明日の注意」用）。"""
    try:
        base = datetime.date.fromisoformat(review_date)
    except (ValueError, TypeError):
        return []
    out = []
    for r in rules:
        if r["rule_type"] != "TEMPORARY" or r["status"] in ("EXPIRED", "RETIRED") or not r.get("expires_date"):
            continue
        try:
            exp = datetime.date.fromisoformat(r["expires_date"])
        except ValueError:
            continue
        if 0 <= (exp - base).days <= days:
            out.append(r)
    return out


def save_review_user_feedback(database_url, user_id, review_date, feedback, source="manual", session_closed=True):
    """日次レビューへユーザー感想を保存する（指示書18番）。該当日のdaily_reviewsが無ければ
    先に生成してから感想を上書きする（感想入力だけ先に行われるケースに対応）。
    緊急修正（2026-09-16）：session_closed（呼び出し元server.pyがcan_finalize_daily_review()で
    計算した値）をgenerate_daily_review()へそのまま引き継ぐ——場中にユーザーが感想を入力して
    このパスがdaily_reviewを再生成しても、持ち越し（day_positions）判定を誤って有効化しない。"""
    pool = _get_pool(database_url)
    if pool is None:
        return None
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT id FROM daily_reviews WHERE user_id=%s AND review_date=%s", [user_id, review_date])
            exists = cur.fetchone()
    if not exists:
        generate_daily_review(database_url, user_id, review_date, user_feedback=feedback, session_closed=session_closed)
    tags = extract_reflection_tags(feedback)
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "UPDATE daily_reviews SET user_feedback=%s, reflection_tags=%s::jsonb, updated_at=now() "
                "WHERE user_id=%s AND review_date=%s RETURNING *",
                [feedback, json.dumps(tags, ensure_ascii=False), user_id, review_date])
            saved = cur.fetchone()
        conn.commit()
    # 感想保存後はルール遵守以外の軸（振り返り点・利確損切り点の反省タグ反映）も再計算する
    return generate_daily_review(database_url, user_id, review_date, user_feedback=feedback, session_closed=session_closed)


def get_daily_review(database_url, user_id, review_date):
    pool = _get_pool(database_url)
    if pool is None:
        return None
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM daily_reviews WHERE user_id=%s AND review_date=%s", [user_id, review_date])
            row = cur.fetchone()
    return _row_to_json(row) if row else None


def list_daily_reviews(database_url, user_id, limit=60):
    pool = _get_pool(database_url)
    if pool is None:
        return []
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM daily_reviews WHERE user_id=%s ORDER BY review_date DESC LIMIT %s",
                        [user_id, limit])
            return [_row_to_json(r) for r in cur.fetchall()]


def recent_reflections_for(database_url, user_id, days=3):
    """朝一チェック・トレード分析の「昨日の反省」表示用（指示書21番）。直近days日分の
    improvement_points/tomorrow_notes/reflection_tagsをまとめて返す。"""
    reviews = list_daily_reviews(database_url, user_id, limit=days)
    out = []
    for r in reviews:
        if not (r.get("improvement_points") or r.get("tomorrow_notes")):
            continue
        out.append({
            "date": r.get("review_date"), "score": r.get("score_total"),
            "improvementPoints": r.get("improvement_points") or [],
            "tomorrowNotes": r.get("tomorrow_notes") or [],
            "reflectionTags": r.get("reflection_tags") or [],
        })
    return out


# ============================================================
# ---- 判断エンジン強化（知識の実利用）。2026-09-09新規 ----
# 「情報を保存するだけ」から「現在の分析へ実際に使う」への拡張。ChatGPT統合連携・
# trade_rules・daily_reviews・recent_reflections（前回・前々回コミット）を土台に、
# 有識者見解の自動評価・イベント/ニュースの分析接続・プレイブック・統合コンテキスト
# ビルダー・used_context記録までを実装する。表示専用の機能は増やさず、既存の分析
# payload（詳細分析を相談・朝一分析・ポジション相談等）へ実際に注入することを優先する。
# ============================================================

# ---- 情報鮮度フレームワーク（指示書5番） ----
FRESHNESS_LIVE_HOURS = 2
FRESHNESS_CURRENT_HOURS = 24
FRESHNESS_RECENT_DAYS = 3
FRESHNESS_STALE_DAYS = 14


def compute_freshness(date_or_datetime_str, now=None):
    """日付・日時文字列からLIVE/CURRENT/RECENT/STALE/EXPIREDを判定する（指示書5番）。
    news/expert_views/events/reflectionsで共通利用する。未来日付（予定されているイベント等）
    はLIVE扱い。パースできない・値が無い場合はEXPIRED（無理に鮮度を高く見せない）。"""
    if not date_or_datetime_str:
        return "EXPIRED"
    now = now or datetime.datetime.now(datetime.timezone.utc)
    try:
        s = str(date_or_datetime_str)
        if len(s) <= 10:
            dt = datetime.datetime.fromisoformat(s).replace(tzinfo=datetime.timezone.utc)
        else:
            dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
    except (ValueError, TypeError):
        return "STALE"
    delta_hours = (now - dt).total_seconds() / 3600
    if delta_hours < 0:
        return "LIVE"
    if delta_hours <= FRESHNESS_LIVE_HOURS:
        return "LIVE"
    if delta_hours <= FRESHNESS_CURRENT_HOURS:
        return "CURRENT"
    if delta_hours <= FRESHNESS_RECENT_DAYS * 24:
        return "RECENT"
    if delta_hours <= FRESHNESS_STALE_DAYS * 24:
        return "STALE"
    return "EXPIRED"


_FRESHNESS_ANALYSIS_ALLOWED = {"LIVE", "CURRENT", "RECENT"}  # 指示書5番：分析payloadは原則ここまで


# ---- 有識者見解の自動評価（指示書1・2番） ----

def evaluate_expert_view_status(view, today=None):
    """有識者見解1件の現在statusを判定する（指示書1番：ACTIVE/CONFIRMED/
    PARTIALLY_CONFIRMED/WEAKENED/INVALIDATED/EXPIRED）。risk_window_end/
    effective_untilが過去日付ならEXPIRED。手動評価（record_expert_view_evaluation）が
    蓄積していればsupported/failed比率でCONFIRMED〜WEAKENED/INVALIDATEDを判定する
    （AI不使用、既存のconfirmations/invalidation_conditionsは自由記述のため、内容の
    自動突合はせずrecord_expert_view_evaluationでのユーザー評価に委ねる保守的な設計）。"""
    today = today or datetime.date.today()
    end = view.get("risk_window_end") or view.get("effective_until")
    if end:
        try:
            if today > datetime.date.fromisoformat(str(end)[:10]):
                return "EXPIRED"
        except ValueError:
            pass
    supported = view.get("supported_count") or 0
    failed = view.get("failed_count") or 0
    total = supported + failed + (view.get("neutral_count") or 0)
    if total == 0:
        return view.get("status") or "ACTIVE"
    rate = supported / total
    if failed >= 2 and supported == 0:
        return "INVALIDATED"
    if rate >= 0.7 and total >= 2:
        return "CONFIRMED"
    if rate >= 0.4:
        return "PARTIALLY_CONFIRMED"
    return "WEAKENED"


def refresh_expert_view_statuses(database_url, user_id):
    """全有識者見解のstatusを毎回の分析時に再評価する（指示書1番「現在データと毎日照合」）。
    期限切れ判定が主目的の軽量チェックで、新しいAPI呼び出しは発生しない。戻り値: 更新件数。
    Phase MU-S1：expert_viewsはSHARED化済み。"""
    user_id = _SHARED_SCOPE
    pool = _get_pool(database_url)
    if pool is None:
        return 0
    views = list_expert_views(database_url, user_id, limit=500)
    n = 0
    with pool.connection() as conn:
        for v in views:
            new_status = evaluate_expert_view_status(v)
            if new_status != v.get("status"):
                conn.execute("UPDATE expert_views SET status=%s, updated_at=now() WHERE id=%s AND user_id=%s",
                             [new_status, v["id"], user_id])
                n += 1
        conn.commit()
    return n


def record_expert_view_evaluation(database_url, user_id, view_id, result):
    """有識者見解1件をSUPPORTED/FAILED/NEUTRALで評価する（指示書2・28番）。
    supported_count/failed_count/neutral_count・accuracy_score・statusを更新する。
    Phase MU-S1：expert_viewsはSHARED化済み。"""
    user_id = _SHARED_SCOPE
    pool = _get_pool(database_url)
    if pool is None or result not in ("SUPPORTED", "FAILED", "NEUTRAL"):
        return None
    col = {"SUPPORTED": "supported_count", "FAILED": "failed_count", "NEUTRAL": "neutral_count"}[result]
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM expert_views WHERE id=%s AND user_id=%s", [view_id, user_id])
            row = cur.fetchone()
            if not row:
                return None
            new_val = (row[col] or 0) + 1
            merged = {**row, col: new_val}
            total = (merged.get("supported_count") or 0) + (merged.get("failed_count") or 0) + (merged.get("neutral_count") or 0)
            accuracy = round((merged.get("supported_count") or 0) / total, 3) if total > 0 else None
            new_status = evaluate_expert_view_status(merged)
            cur.execute(
                f"UPDATE expert_views SET {col}=%s, accuracy_score=%s, status=%s, last_evaluated_at=now(), updated_at=now() "
                f"WHERE id=%s AND user_id=%s RETURNING *",
                [new_val, accuracy, new_status, view_id, user_id])
            saved = cur.fetchone()
        conn.commit()
    return _row_to_json(saved)


def _expert_view_matches(v, code, sector, market):
    """既存フロント側expertViewMatches()と同じ関連度判定（stocks一致／sector一致／
    stocks・sector両方空でmarket一致＝市場全体言及）をサーバー側でも再利用する。"""
    stocks = v.get("stocks") or []
    if code and code in stocks:
        return True
    if sector and v.get("sector") and v["sector"] == sector:
        return True
    if market and v.get("market") == market and not stocks and not v.get("sector"):
        return True
    return False


def relevant_expert_views_for(database_url, user_id, code=None, sector=None, market=None, limit=5):
    """分析対象に関連する有識者見解だけを返す（指示書1番：古い・無関係な見解は入れない）。
    INVALIDATED/EXPIREDおよびfreshnessがEXPIREDのものは除外。"""
    views = list_expert_views(database_url, user_id, market=market, limit=200)
    out = []
    for v in views:
        if v.get("status") in ("INVALIDATED", "EXPIRED"):
            continue
        if not _expert_view_matches(v, code, sector, market):
            continue
        fresh_basis = v.get("risk_window_end") or v.get("effective_until") or v.get("published_at")
        if compute_freshness(fresh_basis) == "EXPIRED":
            continue
        out.append(v)
    out.sort(key=lambda v: v.get("published_at") or "", reverse=True)
    return out[:limit]


# ---- イベントを実際の売買判断へ接続（指示書3番） ----

EVENT_SIGNAL_META = {
    "EVENT_RISK_HIGH": "重要イベントが目前（当日〜1営業日以内）",
    "NO_OVERNIGHT": "重要イベント前は持ち越しを避ける",
    "ENTRY_CAUTION": "重要イベント直前は新規エントリーに注意",
    "REDUCE_SIZE": "イベント接近のためポジションサイズを抑える",
    "TAKE_PROFIT_PRIORITY": "イベント接近のため利益確定を優先",
}


def _business_days_between(base, target):
    """base（date）からtarget（date）までの営業日数（土日のみ除外、祝日は考慮しない簡易版、
    既存のbusinessDaysUntil()と同じ考え方）。target<baseなら負値。"""
    if target == base:
        return 0
    step = 1 if target > base else -1
    d, n = base, 0
    while d != target:
        d += datetime.timedelta(days=step)
        if d.weekday() < 5:
            n += step
    return n


def upcoming_event_signals(database_url, user_id, code=None, sector=None, position=None, today=None, days_ahead=7,
                             _preloaded_events=None):
    """当日/1営業日前/2営業日前/3営業日前/1週間以内の重要イベントを判定し、補助判断フラグを
    生成する（指示書3番）。positionは{"trade_style":"DAY"|"SWING", ...}等（省略可、無ければ
    NO_OVERNIGHT系の判定はスキップ）。戻り値: {"events":[...], "signals":[...]}。
    2026-09-15追加（Market Data Phase 2、DB N+1解消）：_preloaded_eventsを渡すと
    list_market_events()への問い合わせを省略し、渡されたリスト（list_market_eventsと同じ形式）
    をそのままフィルタ対象にする。ENTRY TOP5のバッチ経路専用のオプション引数で、省略時の
    動作（毎回list_market_eventsを呼ぶ）は完全に維持される——同一のfrom_date/to_date・
    同一user_idのeventsであれば、単体呼び出しを何度実行しても同じ結果を1回のDB取得で
    再現できる（events自体はcode/sectorに依存しないクエリのため）。"""
    today = today or datetime.date.today()
    events = _preloaded_events if _preloaded_events is not None else list_market_events(
        database_url, user_id, from_date=today.isoformat(), to_date=(today + datetime.timedelta(days=days_ahead)).isoformat())
    relevant = []
    for e in events:
        affected_codes = e.get("affected_stocks") or []
        affected_sectors = e.get("affected_sectors") or []
        if code and affected_codes and code not in affected_codes:
            if sector and affected_sectors and sector not in affected_sectors:
                continue
            elif not affected_sectors and affected_codes:
                continue
        try:
            ev_date = datetime.date.fromisoformat(str(e.get("event_date"))[:10])
        except (ValueError, TypeError):
            continue
        bd = _business_days_between(today, ev_date)
        if bd < 0 or bd > 5:
            continue
        relevant.append({**e, "business_days_until": bd})

    signals = []
    high_soon = [e for e in relevant if (e.get("importance") or "").lower() in ("high", "critical") and e["business_days_until"] <= 1]
    if high_soon:
        signals.append("EVENT_RISK_HIGH")
        signals.append("ENTRY_CAUTION")
        if position and (position.get("trade_style") or "").upper() in ("DAY", "SWING"):
            signals.append("NO_OVERNIGHT")
        signals.append("REDUCE_SIZE")
        if position:
            signals.append("TAKE_PROFIT_PRIORITY")
    relevant.sort(key=lambda e: e["business_days_until"])
    return {"events": relevant, "signals": list(dict.fromkeys(signals))}


# ---- ニュース・カタリストの分析接続（指示書4番） ----

def relevant_catalysts_for(database_url, user_id, code=None, sector=None, limit=5, max_freshness_days=FRESHNESS_STALE_DAYS,
                             _preloaded_catalysts=None):
    """news_catalystsを分析へ使う（指示書4番）。affected_stocks/affected_sectors一致で
    絞り込み、freshness（catalyst_date基準）がEXPIRED（STALE_DAYSを超過）のものは除外。
    重要度＞鮮度の順でソートする（直近重要ネガティブを優先的に前へ）。
    2026-09-15追加（Market Data Phase 2、DB N+1解消）：_preloaded_catalystsを渡すと
    list_news_catalysts()への問い合わせを省略し、渡されたリストをそのままフィルタ対象にする
    （catalysts自体はcode/sectorに依存しないクエリのため、同一user_idなら1回の取得を全銘柄で
    使い回せる）。ENTRY TOP5のバッチ経路専用のオプション引数で、省略時は従来通り動作する。"""
    catalysts = _preloaded_catalysts if _preloaded_catalysts is not None else list_news_catalysts(database_url, user_id, limit=200)
    _importance_rank = {"high": 0, "medium_high": 1, "medium": 2, "low": 3}
    out = []
    for c in catalysts:
        codes = c.get("affected_stocks") or []
        sectors = c.get("affected_sectors") or []
        if code and codes and code not in codes:
            if not (sector and sectors and sector in sectors):
                continue
        elif not codes and sector and sectors and sector not in sectors:
            continue
        fresh = compute_freshness(c.get("catalyst_date"))
        if fresh == "EXPIRED":
            continue
        out.append({**c, "freshness": fresh})
    out.sort(key=lambda c: (_importance_rank.get((c.get("importance") or "").lower(), 9),
                              0 if c.get("sentiment") == "negative" else 1,
                              {"LIVE": 0, "CURRENT": 1, "RECENT": 2, "STALE": 3}.get(c["freshness"], 4)))
    return out[:limit]


# ---- reflectionを行動警告へ変換（指示書15・16番） ----

BEHAVIORAL_WARNING_META = {
    "CHASE_RISK_HIGH": "最近、高値追い・FOMOでの失敗が続いています。急騰銘柄への飛び乗りに注意",
    "TAKE_PROFIT_DISCIPLINE_WARNING": "最近、利確判断が遅れがちです。目標到達で機械的に利確する意識を",
    "STOP_DISCIPLINE_WARNING": "最近、損切り判断が遅れがちです。計画した損切りラインを守る意識を",
}
_BEHAVIORAL_WARNING_TRIGGER_TAGS = {
    "CHASE_RISK_HIGH": {"高値追い", "FOMO"},
    "TAKE_PROFIT_DISCIPLINE_WARNING": {"利確遅れ"},
    "STOP_DISCIPLINE_WARNING": {"損切り遅れ"},
}
BEHAVIORAL_WARNING_MIN_OCCURRENCES = 2  # 直近N日でこの回数以上繰り返されたら警告（定数化）


def behavioral_warnings_from_reflections(database_url, user_id, days=5):
    """直近days日のreflection_tagsから行動警告を生成する（指示書15番）。表示だけでなく
    build_relevant_trading_context経由で分析へ投入する想定。"""
    reviews = list_daily_reviews(database_url, user_id, limit=days)
    tag_counts = {}
    for r in reviews:
        for t in (r.get("reflection_tags") or []):
            tag_counts[t] = tag_counts.get(t, 0) + 1
    warnings = []
    for code, trigger_tags in _BEHAVIORAL_WARNING_TRIGGER_TAGS.items():
        occurrences = sum(tag_counts.get(t, 0) for t in trigger_tags)
        if occurrences >= BEHAVIORAL_WARNING_MIN_OCCURRENCES:
            warnings.append({"code": code, "message": BEHAVIORAL_WARNING_META[code], "occurrences": occurrences})
    return warnings


RULE_CANDIDATE_FROM_REFLECTION_MIN_OCCURRENCES = 3  # 定数化（指示書16番の「一定回数」）
_REFLECTION_TAG_RULE_CANDIDATE_TEXT = {
    "利確遅れ": "含み益が一定以上になったら逆指値（トレーリングストップ）を引き上げる",
    "損切り遅れ": "含み損が拡大する前に、決めた損切りラインで機械的に手仕舞う",
    "高値追い": "当日大幅上昇・押し目未形成の銘柄には新規で飛び乗らない",
    "FOMO": "焦って追いかけ買いをせず、押し目形成を待つ",
    "ナンピン": "含み損ポジションへの無計画なナンピンをしない",
}


def generate_rule_candidates_from_reflections(database_url, user_id, days=14):
    """同じreflection_tagが繰り返された場合にTESTINGルール候補を生成する（指示書16番）。
    自動ACTIVEにはしない。既存upsert_trade_rule_from_text（rule_keyでの重複防止）を
    再利用するため、同じ候補を複数回実行しても増殖しない。戻り値: 生成/一致した候補の一覧。"""
    reviews = list_daily_reviews(database_url, user_id, limit=days)
    tag_counts = {}
    for r in reviews:
        for t in (r.get("reflection_tags") or []):
            tag_counts[t] = tag_counts.get(t, 0) + 1
    created = []
    for tag, text in _REFLECTION_TAG_RULE_CANDIDATE_TEXT.items():
        if tag_counts.get(tag, 0) >= RULE_CANDIDATE_FROM_REFLECTION_MIN_OCCURRENCES:
            res = upsert_trade_rule_from_text(
                database_url, user_id, text, category="risk",
                initial_status="TESTING", initial_confidence="LOW",
                created_from=f"reflection_pattern:{tag}",
            )
            if res:
                created.append({"tag": tag, "occurrences": tag_counts[tag], "ruleId": res["id"], "action": res["action"]})
    return created


# ---- trade_playbooks（指示書6〜10番） ----

def _normalize_playbook_key(name, source_trader=None):
    """trade_rulesと同じ正規化方式（_normalize_rule_key）を流用し、名前+出典者で
    重複判定キーを作る。"""
    return _normalize_rule_key(f"{name}|{source_trader or ''}")


_PLAYBOOK_JSON_COLS = ["applicable_sectors_json", "applicable_stocks_json", "entry_conditions_json",
                        "confirmation_conditions_json", "exit_conditions_json", "stop_conditions_json",
                        "avoid_conditions_json", "position_management_json"]


def upsert_trade_playbook(database_url, user_id, pb, created_from="manual"):
    """1件のplaybook dictをupsertする（指示書7番：新規は必ずTESTING/LOWまたはMEDIUM）。
    playbook_keyの完全一致で重複判定（指示書「自動ACTIVEにしない」を厳守、statusを
    手動以外で上書きしない）。"""
    pool = _get_pool(database_url)
    if pool is None or not pb.get("name"):
        return None
    key = _normalize_playbook_key(pb["name"], pb.get("source_trader"))
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT id FROM trade_playbooks WHERE user_id=%s AND playbook_key=%s", [user_id, key])
            existing = cur.fetchone()
            if existing:
                cur.execute("UPDATE trade_playbooks SET evidence_count=evidence_count+1, updated_at=now() "
                            "WHERE id=%s RETURNING *", [existing["id"]])
                saved = cur.fetchone()
                conn.commit()
                return {"action": "matched", **_row_to_json(saved)}
            cols = ["name", "source_trader", "source_title", "source_type", "market_type", "timeframe"] + _PLAYBOOK_JSON_COLS
            src_map = {
                "applicable_sectors_json": pb.get("applicable_sectors") or pb.get("applicable_sectors_json"),
                "applicable_stocks_json": pb.get("applicable_stocks") or pb.get("applicable_stocks_json"),
                "entry_conditions_json": pb.get("entry_conditions") or pb.get("entry_conditions_json"),
                "confirmation_conditions_json": pb.get("confirmation_conditions") or pb.get("confirmation_conditions_json"),
                "exit_conditions_json": pb.get("exit_conditions") or pb.get("exit_conditions_json"),
                "stop_conditions_json": pb.get("stop_conditions") or pb.get("stop_conditions_json"),
                "avoid_conditions_json": pb.get("avoid_conditions") or pb.get("avoid_conditions_json"),
                "position_management_json": pb.get("position_management") or pb.get("position_management_json"),
            }
            values = []
            for c in cols:
                if c in _PLAYBOOK_JSON_COLS:
                    values.append(json.dumps(src_map.get(c) or [], ensure_ascii=False))
                else:
                    values.append(pb.get(c))
            placeholders = ", ".join(["%s::jsonb" if c in _PLAYBOOK_JSON_COLS else "%s" for c in cols])
            cur.execute(
                f"INSERT INTO trade_playbooks (user_id, playbook_key, {', '.join(cols)}, status, confidence, "
                f"evidence_count, created_from) VALUES (%s,%s,{placeholders},'TESTING','LOW',1,%s) RETURNING *",
                [user_id, key] + values + [created_from],
            )
            saved = cur.fetchone()
        conn.commit()
    return {"action": "created", **_row_to_json(saved)}


def import_trade_playbooks(database_url, user_id, playbooks, created_from="chatgpt_import"):
    """playbooks（dictのリスト）を1件ずつupsertする（指示書8番）。戻り値:
    {"imported":N,"matched":M,"skipped":K}。"""
    imported = matched = skipped = 0
    for pb in playbooks:
        if not isinstance(pb, dict) or not pb.get("name"):
            skipped += 1
            continue
        res = upsert_trade_playbook(database_url, user_id, pb, created_from=created_from)
        if not res:
            skipped += 1
        elif res["action"] == "created":
            imported += 1
        else:
            matched += 1
    return {"imported": imported, "matched": matched, "skipped": skipped}


def list_trade_playbooks(database_url, user_id, status=None):
    """Phase MU-S3C：trade_rulesと同じGLOBAL/USER可視性方式。本人が作成したUSERプレイブックに
    加え、GLOBAL（visibility='GLOBAL'、user_id=_SHARED_SCOPE）も合わせて返す。個人実践成績
    （旧user_attempt_count等）はtrade_playbook_user_statsへ分離済みのためここには含まれない
    ——必要な呼び出し元はget_trade_playbook_user_statsで本人分だけ別途取得すること。"""
    pool = _get_pool(database_url)
    if pool is None:
        return []
    where, params = ["user_id IN (%s, %s)"], [user_id, _SHARED_SCOPE]
    if status:
        where.append("status=%s")
        params.append(status)
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(f"SELECT * FROM trade_playbooks WHERE {' AND '.join(where)} ORDER BY updated_at DESC", params)
            return [_row_to_json(r) for r in cur.fetchall()]


def get_trade_playbook_user_stats(database_url, user_id, playbook_id):
    """Phase MU-S3C：呼び出しユーザー自身のplaybook実践成績（PRIVATE、trade_playbook_user_stats）
    を1件返す。無ければNone（＝まだ実践経験なし）。GLOBAL/USER定義本体とは別テーブルのため、
    他ユーザーの実践成績が混ざることは無い。"""
    pool = _get_pool(database_url)
    if pool is None:
        return None
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM trade_playbook_user_stats WHERE user_id=%s AND playbook_id=%s",
                [user_id, playbook_id])
            row = cur.fetchone()
    return _row_to_json(row) if row else None


def _condition_matches(cond, signals):
    """1つの条件（文字列 or {"key":...,"op":...,"value":...}）が、現在のsignals dict
    （出来高倍率・VWAP位置・セクターRS等、呼び出し側が用意する軽量な数値/真偽値の集合）に
    合致するかを判定する。文字列条件はsignalsのキーとして真偽値をそのまま見る簡易実装
    （例："volume_surge"というキーがTrueならヒット）。厳密なDSLは作らず、既存の
    analyze_stock()の出力キー名をそのままcondition文字列として書けるようにする設計。"""
    if isinstance(cond, str):
        return bool(signals.get(cond))
    if isinstance(cond, dict):
        key, op, val = cond.get("key"), cond.get("op", "eq"), cond.get("value")
        cur = signals.get(key)
        if cur is None:
            return False
        try:
            if op == "gte":
                return cur >= val
            if op == "lte":
                return cur <= val
            if op == "gt":
                return cur > val
            if op == "lt":
                return cur < val
            if op == "eq":
                return cur == val
        except TypeError:
            return False
    return False


PLAYBOOK_AVOID_PENALTY = 40  # 定数化（指示書9番：avoid_conditions発動時は大きく減点）


def playbook_match_score(playbook, signals):
    """対象銘柄の現在シグナル（signals、呼び出し側でVWAP位置・出来高倍率・セクターRS・
    モメンタム・イベント距離等を用意する）と1件のplaybookを照合し、0〜100の適合度を返す
    （指示書9番）。条件一致率をベースに、avoid_conditionsが1つでも発動していれば大きく
    減点する。戻り値: {"score":0-100, "matched_entry":[...], "matched_avoid":[...]}。"""
    entry_conds = playbook.get("entry_conditions_json") or []
    confirm_conds = playbook.get("confirmation_conditions_json") or []
    avoid_conds = playbook.get("avoid_conditions_json") or []
    all_conds = entry_conds + confirm_conds
    if not all_conds:
        base_score = 50  # 条件が定義されていないplaybookは判定不能寄りの中立値
    else:
        matched = [c for c in all_conds if _condition_matches(c, signals)]
        base_score = round(100 * len(matched) / len(all_conds))
    matched_avoid = [c for c in avoid_conds if _condition_matches(c, signals)]
    score = max(0, base_score - PLAYBOOK_AVOID_PENALTY * len(matched_avoid))
    return {"score": score, "matchedEntryCount": len([c for c in all_conds if _condition_matches(c, signals)]),
            "totalConditions": len(all_conds), "avoidTriggered": len(matched_avoid) > 0}


def matched_playbooks_for(database_url, user_id, signals, code=None, sector=None, timeframe=None, limit=3):
    """対象銘柄に適合するplaybookを照合し、スコア順で上位を返す（指示書9番）。
    RETIREDは対象外。"""
    playbooks = list_trade_playbooks(database_url, user_id)
    out = []
    for pb in playbooks:
        if pb.get("status") == "RETIRED":
            continue
        stocks = pb.get("applicable_stocks_json") or []
        sectors = pb.get("applicable_sectors_json") or []
        if stocks and code and code not in stocks:
            continue
        if sectors and sector and sector not in sectors:
            continue
        if timeframe and pb.get("timeframe") and pb["timeframe"] != timeframe:
            continue
        m = playbook_match_score(pb, signals)
        out.append({**pb, "matchScore": m["score"], "matchDetail": m})
    out.sort(key=lambda p: -p["matchScore"])
    return out[:limit]


# ---- 類似相場日検索（指示書12番） ----

def similar_market_days_for(database_url, user_id, current_snapshot, limit=5, exclude_date=None):
    """daily_log.raw_payload.marketから、現在の相場スナップショット（current_snapshot:
    {"nikkei_pct":float,"decliner_ratio":float,...}のうち利用可能なものだけでよい）に
    近い過去日を抽出する（指示書12番）。数値項目のユークリッド距離が近い順。項目が
    無ければ照合対象から除外する（捏造しない）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return []
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT date, raw_payload FROM daily_log WHERE user_id=%s AND raw_payload IS NOT NULL ORDER BY date DESC LIMIT 200",
                        [user_id])
            logs = cur.fetchall()
    candidates = []
    keys = list(current_snapshot.keys())
    if not keys:
        return []
    for log in logs:
        if exclude_date and str(log.get("date")) == str(exclude_date):
            continue
        payload = log.get("raw_payload") or {}
        market = payload.get("market") or {}
        snap = market.get("snapshot") or {}
        matched_keys = [k for k in keys if k in snap and isinstance(snap.get(k), (int, float))]
        if not matched_keys:
            continue
        dist = sum((snap[k] - current_snapshot[k]) ** 2 for k in matched_keys) ** 0.5
        candidates.append({"date": log.get("date"), "distance": round(dist, 3),
                            "matchedFields": matched_keys, "condition": market.get("condition")})
    candidates.sort(key=lambda c: c["distance"])
    return candidates[:limit]


# ---- 統合ナレッジコンテキストビルダー（指示書18〜20番） ----

CONTEXT_CATEGORY_PRIORITY = [  # 指示書20番の優先順位（表示・送信時のソート基準として使う）
    "current_position", "recent_news", "upcoming_events", "relevant_rules",
    "matched_playbooks", "expert_views", "behavioral_warnings", "similar_trades", "similar_market_days",
]


def build_relevant_trading_context(database_url, user_id, stock_code=None, sector=None, market=None,
                                     position=None, analysis_type="stock", signals=None,
                                     rule_categories=None):
    """分析直前に呼ぶ統合コンテキストビルダー（指示書18・19番）。朝一分析・個別銘柄分析・
    TOP5相談・ポジション相談・利確損切り相談・日次レビューから共通で使う。全ての情報源を
    毎回総なめにするのではなく、関連度・鮮度であらかじめ絞った少量だけを返す（指示書26番
    「古い一般論は低優先」）。signalsはplaybook照合用の軽量な数値/真偽値dict（省略可、
    無ければmatched_playbooksは空になる）。
    戻り値: {"recent_news":[], "upcoming_events":[], "relevant_rules":[], "expert_views":[],
    "matched_playbooks":[], "similar_trades":[], "similar_market_days":[],
    "behavioral_warnings":[]}"""
    signals = signals or {}
    rule_categories = rule_categories or ([sector] if sector else None)

    recent_news = relevant_catalysts_for(database_url, user_id, code=stock_code, sector=sector, limit=5)
    event_info = upcoming_event_signals(database_url, user_id, code=stock_code, sector=sector, position=position)
    relevant_rules = relevant_trade_rules_for(database_url, user_id, categories=rule_categories, limit=6)
    expert_views = relevant_expert_views_for(database_url, user_id, code=stock_code, sector=sector, market=market, limit=5)
    matched_playbooks = matched_playbooks_for(database_url, user_id, signals, code=stock_code, sector=sector,
                                                timeframe=(position or {}).get("trade_style"), limit=3)
    behavioral_warnings = behavioral_warnings_from_reflections(database_url, user_id)
    # 2026-09-09拡張（指示書14番）：similar_tradesはmatched_playbooksのIDも照合材料に使う
    # （同銘柄＋同じプレイブックで判断していた過去トレードを優先表示）。
    similar_trades = find_similar_trades_for(database_url, user_id, code=stock_code, sector=sector, limit=5,
                                               matched_playbook_ids=[p["id"] for p in matched_playbooks])

    return {
        "recent_news": recent_news,
        "upcoming_events": event_info["events"],
        "event_signals": event_info["signals"],
        "relevant_rules": relevant_rules,
        "expert_views": expert_views,
        "matched_playbooks": matched_playbooks,
        "similar_trades": similar_trades,
        "similar_market_days": [],  # current_snapshotが呼び出し側にしか無いため、必要な場合は個別にsimilar_market_days_forを呼ぶ
        "behavioral_warnings": behavioral_warnings,
    }


# ---- 過去売買の類似検索（指示書11・14番） ----

def find_similar_trades_for(database_url, user_id, code=None, sector=None, limit=5, matched_playbook_ids=None):
    """過去のtrade_historyから類似トレードを抽出する（指示書14番）。指示書が挙げる項目
    （sector/time_of_day/momentum_state/volatility等）のうち、実際にDBへ永続化されて
    いて検証可能なものだけをスコア化する：同銘柄（基礎点）＋同じプレイブックを参照して
    判断していたか（analysis_context_log.used_context.playbooksとの突合）。
    sector/時間帯/ボラティリティ・モメンタム状態は現状trade_historyに記録が無く、
    捏造を避けるため今回はスコアに含めない（今後の課題）。"""
    if not code:
        return []
    history = list_trade_history(database_url, user_id, limit=200)
    same_code = [t for t in history if t.get("code") == code]
    if not same_code:
        return []
    matched_playbook_ids = set(matched_playbook_ids or [])
    pool = _get_pool(database_url) if matched_playbook_ids else None
    scored = []
    for t in same_code:
        score = 3  # 同銘柄一致の基礎点
        if pool:
            with pool.connection() as conn:
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute(
                        "SELECT used_context_json FROM analysis_context_log WHERE user_id=%s AND code=%s "
                        "AND analysis_date <= %s ORDER BY analysis_date DESC LIMIT 3",
                        [user_id, code, str(t.get("closed_at") or "")[:10]])
                    logs = cur.fetchall()
            used_pbs = set()
            for log in logs:
                used_pbs |= set((log.get("used_context_json") or {}).get("playbooks") or [])
            if used_pbs & matched_playbook_ids:
                score += 2  # 同じプレイブックで判断していた過去トレードを優先
        scored.append((score, t.get("closed_at") or "", t))
    scored.sort(key=lambda x: (-x[0], x[1]), reverse=False)
    scored.sort(key=lambda x: -x[0])
    return [t for _, _, t in scored[:limit]]


# ---- 総合判断の生成（指示書21〜23番） ----

TRADE_JUDGMENTS = ["BUY_CANDIDATE", "WAIT", "TAKE_PROFIT", "RAISE_STOP", "REDUCE", "EXIT", "NO_OVERNIGHT"]


def synthesize_trade_judgment(context, base_signal=None):
    """統合コンテキスト（build_relevant_trading_context()の戻り値）から、単一材料だけに
    頼らない総合判断・確信度・理由を生成する（指示書21〜23番）。base_signalは呼び出し側の
    既存ロジック（enrichWatchRow由来のactionStatus等）から渡す「今のところの一次判断」
    （例："ENTRY_READY"|"WAIT"|"RISK"）——このエンジンはそれを置き換えるのではなく、
    知識コンテキストで補強・警告を上乗せする。
    2026-09-09更新（指示書16・17番）：各材料をbullish（強気＝supporting）／bearish
    （弱気・警戒＝opposing）に分類し、単純な材料数ではなく方向の一致度でconfidenceを
    調整する（賛否が割れているほど確信度を下げる、指示書16番の「rules bearish／events
    bearish／expert bearish／playbook bullish なら判断は慎重」を一般化した実装）。
    戻り値: {"judgment":..., "confidence":0-100, "reasons":[...], "risk_flags":[...],
    "supporting":[...], "opposing":[...]}（supporting/opposingはcontradicting_context
    用、指示書17番）"""
    reasons = []
    risk_flags = []
    supporting = []  # bullish寄りの材料（テキストラベル）
    opposing = []    # bearish/警戒寄りの材料
    judgment = {"ENTRY_READY": "BUY_CANDIDATE", "RISK": "WAIT", "WAIT": "WAIT"}.get(base_signal, "WAIT")
    if base_signal == "ENTRY_READY":
        supporting.append("既存の一次判断（Action Status）がENTRY_READY")

    if "EVENT_RISK_HIGH" in (context.get("event_signals") or []):
        risk_flags.append("EVENT_RISK_HIGH")
        reasons.append("重要イベントが目前")
        opposing.append("重要イベントが目前")
        if judgment == "BUY_CANDIDATE":
            judgment = "WAIT"
    if "NO_OVERNIGHT" in (context.get("event_signals") or []):
        risk_flags.append("NO_OVERNIGHT")
        reasons.append("イベント前のため持ち越し非推奨")
        opposing.append("イベント前のため持ち越し非推奨")
        judgment = "NO_OVERNIGHT" if judgment not in ("EXIT", "TAKE_PROFIT") else judgment

    neg_news = [n for n in (context.get("recent_news") or []) if n.get("sentiment") == "negative" and n.get("freshness") in ("LIVE", "CURRENT")]
    pos_news = [n for n in (context.get("recent_news") or []) if n.get("sentiment") == "positive" and n.get("freshness") in ("LIVE", "CURRENT")]
    if neg_news:
        reasons.append(f"直近ネガティブ材料あり（{neg_news[0].get('title','')[:20]}）")
        risk_flags.append("NEGATIVE_NEWS")
        opposing.append("直近ネガティブニュース")
        if judgment == "BUY_CANDIDATE":
            judgment = "WAIT"
    if pos_news:
        supporting.append("直近ポジティブニュース")

    top_playbook = (context.get("matched_playbooks") or [None])[0]
    if top_playbook:
        if top_playbook["matchDetail"]["avoidTriggered"]:
            reasons.append(f"プレイブック『{top_playbook['name']}』の回避条件に該当")
            risk_flags.append("PLAYBOOK_AVOID")
            opposing.append(f"プレイブック『{top_playbook['name']}』の回避条件")
            if judgment == "BUY_CANDIDATE":
                judgment = "WAIT"
        elif top_playbook["matchScore"] >= 70:
            reasons.append(f"プレイブック『{top_playbook['name']}』適合度{top_playbook['matchScore']}%")
            supporting.append(f"プレイブック『{top_playbook['name']}』高適合")

    high_conf_rules = [r for r in (context.get("relevant_rules") or []) if r.get("confidence") == "HIGH" and r.get("status") == "ACTIVE"]
    risk_rules = [r for r in high_conf_rules if r.get("category") == "risk"]
    if risk_rules:
        reasons.append(f"ACTIVEルール『{risk_rules[0]['rule_text'][:20]}…』が該当")
        opposing.append(f"リスク系ACTIVEルール『{risk_rules[0]['rule_text'][:16]}…』")
    elif high_conf_rules:
        reasons.append(f"ACTIVEルール『{high_conf_rules[0]['rule_text'][:20]}…』が該当")
        supporting.append(f"ACTIVEルール『{high_conf_rules[0]['rule_text'][:16]}…』")

    for w in (context.get("behavioral_warnings") or []):
        reasons.append(w["message"][:24] + "…")
        risk_flags.append(w["code"])
        opposing.append(w["code"])
        if judgment == "BUY_CANDIDATE" and w["code"] == "CHASE_RISK_HIGH":
            judgment = "WAIT"

    weakened_experts = [e for e in (context.get("expert_views") or []) if e.get("status") == "WEAKENED"]
    confirmed_experts = [e for e in (context.get("expert_views") or []) if e.get("status") == "CONFIRMED"]
    if confirmed_experts:
        reasons.append(f"{confirmed_experts[0]['expert_name']}氏の見解が支持されている")
        supporting.append(f"{confirmed_experts[0]['expert_name']}氏の見解（CONFIRMED）")
    if weakened_experts:
        opposing.append(f"{weakened_experts[0]['expert_name']}氏の見解（WEAKENED）")

    # 指示書16番：材料数ではなく方向の一致度でconfidenceを決める。全会一致（片方が0件）に
    # 近いほど高く、賛否が割れる（両方に材料がある）ほど低くする。基準confidence=60から、
    # 一致率（多数派側の割合）に応じて増減させる簡易実装。
    total_factors = len(supporting) + len(opposing)
    if total_factors == 0:
        confidence = 55  # 材料が全く無い＝判断根拠が薄いため中立よりやや低め
    else:
        majority = max(len(supporting), len(opposing))
        agreement_rate = majority / total_factors  # 1.0=全会一致, 0.5=真っ二つ
        confidence = round(40 + agreement_rate * 40)  # 40(五分五分)〜80(全会一致)のレンジ
        if len(opposing) > len(supporting):
            confidence -= 10  # 警戒材料が優勢な場合はさらに慎重寄りに

    confidence = max(0, min(100, confidence))
    return {
        "judgment": judgment, "confidence": confidence,
        "reasons": reasons[:5],  # 指示書23番：3〜5件へ要約
        "risk_flags": list(dict.fromkeys(risk_flags)),
        "supporting": supporting, "opposing": opposing,
    }


def save_analysis_context_log(database_url, user_id, code, analysis_type, judgment_result, context, analysis_date=None):
    """指示書24番：各分析実行時にused_contextを保存する。日次レビューのknown_risk_ignored
    検出（指示書19・26番）で再利用する。2026-09-09拡張：risk_flags・matched_playbooksの
    avoid発動有無・supporting/opposing区分（指示書17番のcontradicting_context）も保存する。"""
    pool = _get_pool(database_url)
    if pool is None:
        return None
    analysis_date = analysis_date or datetime.date.today().isoformat()
    playbooks = context.get("matched_playbooks") or []
    used_context = {
        "rules": [r.get("id") for r in (context.get("relevant_rules") or [])],
        "expert_views": [e.get("id") for e in (context.get("expert_views") or [])],
        "events": [e.get("id") for e in (context.get("upcoming_events") or [])],
        "news": [n.get("id") for n in (context.get("recent_news") or [])],
        "playbooks": [p.get("id") for p in playbooks],
        "similar_trades": [t.get("id") for t in (context.get("similar_trades") or [])],
        "event_signals": context.get("event_signals") or [],
        "risk_flags": judgment_result.get("risk_flags") or [],
        "avoid_playbook_ids": [p.get("id") for p in playbooks if (p.get("matchDetail") or {}).get("avoidTriggered")],
        "active_risk_rule_ids": [r.get("id") for r in (context.get("relevant_rules") or [])
                                   if r.get("category") == "risk" and r.get("status") == "ACTIVE"],
        "supporting": judgment_result.get("supporting") or [],
        "opposing": judgment_result.get("opposing") or [],
    }
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "INSERT INTO analysis_context_log (user_id, code, analysis_type, analysis_date, judgment, "
                "confidence_score, reasons_json, used_context_json) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb) "
                "RETURNING *",
                [user_id, code, analysis_type, analysis_date, judgment_result.get("judgment"),
                 judgment_result.get("confidence"), json.dumps(judgment_result.get("reasons") or [], ensure_ascii=False),
                 json.dumps(used_context, ensure_ascii=False)])
            saved = cur.fetchone()
        conn.commit()
    return _row_to_json(saved)


def _check_known_risk_ignored(database_url, user_id, review_date, positions, new_positions=None):
    """指示書19・26番：その日のanalysis_context_logで警告が出ていたのに実際の売買行動が
    それを無視したケースを機械的に照合する（「結果論」ではなく「その時点で警告が出ていたか」
    で判定、指示書27番）。2026-09-09拡張：NO_OVERNIGHTに加え、EVENT_RISK_HIGH（重要イベント
    直前の持ち越し）・PLAYBOOK_AVOID（回避条件発動下での新規建玉）・ACTIVE_RISK_RULE
    （riskカテゴリのACTIVEルールが文脈にあった中での新規建玉）も検出対象にする。"""
    pool = _get_pool(database_url)
    if pool is None:
        return []
    new_positions = new_positions if new_positions is not None else []
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT code, judgment, used_context_json FROM analysis_context_log "
                "WHERE user_id=%s AND analysis_date=%s AND code IS NOT NULL", [user_id, review_date])
            logs = cur.fetchall()
    held_codes = {p["code"] for p in positions}
    new_codes = {p["code"] for p in new_positions}
    seen = set()  # (code, reason)単位で重複排除
    ignored = []

    def _add(code, reason):
        key = (code, reason)
        if key not in seen:
            seen.add(key)
            ignored.append({"code": code, "reason": reason})

    for log in logs:
        code = log.get("code")
        uc = log.get("used_context_json") or {}
        signals = uc.get("event_signals") or []
        risk_flags = uc.get("risk_flags") or []
        if (log.get("judgment") == "NO_OVERNIGHT" or "NO_OVERNIGHT" in signals) and code in held_codes:
            _add(code, "NO_OVERNIGHT警告があったにも関わらず持ち越し")
        if "EVENT_RISK_HIGH" in signals and code in held_codes:
            _add(code, "重要イベント直前の警告があったにも関わらず持ち越し")
        if (uc.get("avoid_playbook_ids") or "PLAYBOOK_AVOID" in risk_flags) and code in new_codes:
            _add(code, "プレイブックの回避条件が発動していたにも関わらず新規建玉")
        if uc.get("active_risk_rule_ids") and code in new_codes:
            _add(code, "リスク関連のACTIVEルールが該当していたにも関わらず新規建玉")
        # 2026-09-10新規（損切りルール是正）：EXIT RULE（-8%到達）が判定済みなのに
        # 保有継続（その日のうちに売却していない）していれば最重要の違反として検出する。
        if (log.get("judgment") == "EXIT" or "POSITION_RISK_EXIT" in risk_flags) and code in held_codes:
            _add(code, "EXIT RULE（-8%到達）が判定されていたにも関わらず保有継続")
    return ignored


# ============================================================
# ---- 判断エンジンの全画面統合＋自己適応。2026-09-09新規 ----
# 個別銘柄分析に接続済みの統合コンテキストを、朝一・TOP5・ポジション・利確損切り・
# 持ち越し判断へ接続し、playbookの自分との相性学習・過去知識のplaybook候補化を追加する。
# ============================================================

# ---- ポジション相談・利確損切り相談：EXIT側の総合判断（指示書5・6番） ----

def evaluate_exit_judgment(context, position=None, risk_rules=None):
    """保有銘柄向けの総合判断（HOLD/RAISE_STOP/TAKE_PROFIT/REDUCE/EXIT/NO_OVERNIGHT）を
    生成する。新規買い判断（synthesize_trade_judgment）とは別に、matched_playbooksの
    exit_conditions_json/stop_conditions_jsonとtrade_rulesのexit/riskカテゴリを強く
    参照する（指示書5・6番）。positionは{"unrealized_pnl_pct":float,...}（省略可）。
    2026-09-10更新（損切りルール是正・最優先修正）：get_position_risk_rules()由来の
    共通設定（既定-8%）に到達した場合はEXITを絶対最優先で確定し、以降のいかなる材料
    （プレイブック・行動警告・イベント等）でも上書きしない——自信度・材料・ファンダ・
    ニュース・AI分析結果に関係なく一旦売却を促す、というユーザー指定の最新ルール。
    旧実装は判定の途中（他の材料の後）でjudgment=="HOLD"の場合のみEXITにしていたため、
    先に他の材料でjudgmentが決まっているとEXITへ上書きされない不具合があった。"""
    position = position or {}
    reasons, risk_flags, supporting, opposing = [], [], [], []
    pnl_pct = position.get("unrealized_pnl_pct")
    rules = risk_rules or DEFAULT_POSITION_RISK_RULES
    risk_tier = evaluate_position_risk_tier(pnl_pct, rules)
    if risk_tier == "EXIT":
        return {
            "judgment": "EXIT", "confidence": 100,
            "reasons": [f"EXIT RULE：買値から{pnl_pct:.1f}%（{rules['max_loss_pct']:.1f}%到達）。"
                        f"一旦売却してください。再エントリーは新しいトレードとして判断します。"],
            "risk_flags": ["POSITION_RISK_EXIT"], "supporting": [], "opposing": ["含み損がEXIT RULEに到達"],
        }
    judgment = "HOLD"

    if "NO_OVERNIGHT" in (context.get("event_signals") or []):
        judgment = "NO_OVERNIGHT"
        reasons.append("イベント前のため持ち越し非推奨")
        risk_flags.append("NO_OVERNIGHT")
        opposing.append("イベント前の持ち越しリスク")

    exit_rules = [r for r in (context.get("relevant_rules") or [])
                  if r.get("status") == "ACTIVE" and r.get("category") in ("exit", "risk")]
    for r in exit_rules[:2]:
        reasons.append(f"ACTIVEルール『{r['rule_text'][:20]}…』")
        opposing.append(f"ルール『{r['rule_text'][:16]}…』")

    top_playbook = (context.get("matched_playbooks") or [None])[0]
    if top_playbook:
        exit_conds = top_playbook.get("exit_conditions_json") or []
        stop_conds = top_playbook.get("stop_conditions_json") or []
        if top_playbook["matchDetail"]["avoidTriggered"]:
            reasons.append(f"プレイブック『{top_playbook['name']}』の回避条件が発動中")
            if judgment == "HOLD":
                judgment = "REDUCE"
            opposing.append("プレイブック回避条件")
        elif exit_conds or stop_conds:
            reasons.append(f"プレイブック『{top_playbook['name']}』のexit/stop条件を参照")

    for w in (context.get("behavioral_warnings") or []):
        if w["code"] == "TAKE_PROFIT_DISCIPLINE_WARNING" and pnl_pct is not None and pnl_pct > 0:
            reasons.append("最近、利確判断が遅れがち（過去の反省）")
            if judgment == "HOLD":
                judgment = "TAKE_PROFIT"
            opposing.append("利確遅れの反省履歴")
        if w["code"] == "STOP_DISCIPLINE_WARNING" and pnl_pct is not None and pnl_pct < 0:
            reasons.append("最近、損切り判断が遅れがち（過去の反省）")
            if judgment == "HOLD":
                judgment = "RAISE_STOP"
            opposing.append("損切り遅れの反省履歴")

    if pnl_pct is not None:
        if risk_tier == "WARNING":
            reasons.append(f"損切りライン接近（含み損{pnl_pct:.1f}%、EXIT RULE {rules['max_loss_pct']:.1f}%まであと僅か）")
            if judgment == "HOLD":
                judgment = "RAISE_STOP"
            opposing.append("損切りラインに接近")
        elif risk_tier == "WATCH":
            reasons.append(f"含み損がやや拡大（{pnl_pct:.1f}%、注視）")
            opposing.append("含み損やや拡大")
        elif pnl_pct >= 8:
            reasons.append(f"含み益{pnl_pct:.1f}%")
            if judgment == "HOLD":
                judgment = "TAKE_PROFIT"
            supporting.append("含み益十分")

    neg_news = [n for n in (context.get("recent_news") or []) if n.get("sentiment") == "negative" and n.get("freshness") in ("LIVE", "CURRENT")]
    if neg_news:
        reasons.append(f"直近ネガティブ材料あり（{neg_news[0].get('title','')[:20]}）")
        opposing.append("直近ネガティブニュース")
        if judgment == "HOLD":
            judgment = "REDUCE"

    total = len(supporting) + len(opposing)
    confidence = 55 if total == 0 else round(40 + (max(len(supporting), len(opposing)) / total) * 40)
    if opposing and not supporting:
        confidence = min(100, confidence + 10)  # 警戒材料しか無い場合はむしろ判断がはっきりする
    confidence = max(0, min(100, confidence))
    return {"judgment": judgment, "confidence": confidence, "reasons": reasons[:5],
            "risk_flags": list(dict.fromkeys(risk_flags)), "supporting": supporting, "opposing": opposing}


# ---- 持ち越し判断専用ロジック（指示書7番） ----

OVERNIGHT_DECISIONS = ["OVERNIGHT_OK", "OVERNIGHT_CAUTION", "NO_OVERNIGHT"]


def _count_past_known_risk_ignored_for_code(database_url, user_id, code, days=30):
    """指示書7番「known_risk_ignored履歴」：過去days日でこの銘柄が何回known_risk_ignoredに
    載ったかを数える。"""
    pool = _get_pool(database_url)
    if pool is None:
        return 0
    since = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT known_risk_ignored_json FROM daily_reviews WHERE user_id=%s AND review_date>=%s",
                        [user_id, since])
            rows = cur.fetchall()
    return sum(1 for r in rows for item in (r.get("known_risk_ignored_json") or []) if item.get("code") == code)


def evaluate_overnight_decision(database_url, user_id, code, context, unrealized_pnl_pct=None,
                                  market_condition=None, sector_rs=None):
    """持ち越し判断専用（指示書7番）。翌日重要イベント・決算・FOMC/日銀・overnight禁止
    ルール・含み益/含み損・地合い・セクター相対強度・直近反省・known_risk_ignored履歴を
    明示的にチェックする。戻り値: {"decision":OVERNIGHT_OK|OVERNIGHT_CAUTION|
    NO_OVERNIGHT, "score":int, "reasons":[...]}"""
    reasons = []
    score = 0
    # 2026-09-10新規（損切りルール是正・最優先修正）：EXIT RULE（-8%到達）に既に達している
    # ポジションは、そもそも持ち越し以前に売却すべきなので無条件でNO_OVERNIGHTにする。
    risk_tier = evaluate_position_risk_tier(unrealized_pnl_pct) if unrealized_pnl_pct is not None else None
    if risk_tier == "EXIT":
        reasons.append("EXIT RULE（損切りライン）に到達済み。持ち越し以前に売却してください")
        score += 5
    if "NO_OVERNIGHT" in (context.get("event_signals") or []):
        reasons.append("重要イベント直前のためNO_OVERNIGHTルールに該当")
        score += 3
    high_events = [e for e in (context.get("upcoming_events") or [])
                   if (e.get("importance") or "").lower() in ("high", "critical") and e.get("business_days_until", 9) <= 1]
    if high_events:
        title = high_events[0].get("title") or high_events[0].get("event") or "重要イベント"
        reasons.append(f"{title}が翌営業日までに予定されている")
        score += 2
    no_overnight_rules = [r for r in (context.get("relevant_rules") or [])
                            if r.get("status") == "ACTIVE" and "持ち越し" in (r.get("rule_text") or "")]
    if no_overnight_rules:
        reasons.append(f"ACTIVEルール『{no_overnight_rules[0]['rule_text'][:20]}…』")
        score += 2
    if unrealized_pnl_pct is not None and unrealized_pnl_pct < 0:
        reasons.append(f"含み損{unrealized_pnl_pct:.1f}%の状態")
        score += 1
    if market_condition and any(k in market_condition for k in ("リスクオフ", "軟調", "弱い")):
        reasons.append(f"地合いが軟調（{market_condition}）")
        score += 1
    if sector_rs is not None and sector_rs < -2:
        reasons.append(f"セクター相対強度が弱い（{sector_rs:+.1f}pt）")
        score += 1
    for w in (context.get("behavioral_warnings") or []):
        reasons.append(w["message"][:24] + "…")
        score += 1
    past_ignored = _count_past_known_risk_ignored_for_code(database_url, user_id, code)
    if past_ignored > 0:
        reasons.append(f"過去{past_ignored}回、この銘柄でリスク警告を無視した記録あり")
        score += 1

    decision = "NO_OVERNIGHT" if score >= 4 else "OVERNIGHT_CAUTION" if score >= 2 else "OVERNIGHT_OK"
    return {"decision": decision, "score": score, "reasons": reasons[:5]}


# ---- playbook実績の自動更新・自分との相性（指示書8〜10番） ----

USER_COMPAT_PRIOR_N = 4       # ベイズ的縮小の疑似試行回数（指示書9番「最低試行回数」対応）
USER_COMPAT_PRIOR_RATE = 0.5  # 事前分布の勝率（サンプルが少ない間は五分五分寄りに縮小する）


def compute_user_compatibility_score(success, failure, neutral, avg_return):
    """user_compatibility_score計算式（指示書9・10番）。単純勝率にはしない：
    ①ベイズ的縮小（疑似試行4回・勝率50%を事前分布とし、サンプルが少ないほど50点側へ
    寄せる、極端な値を防ぐ）を掛けた勝率（重み70%）と、②平均リターン（±20%にクリップ、
    重み30%）を合成する。playbook自体のconfidence（一般的な再現性、trade_rulesと同じ
    evidence/success_count方式）とは完全に別軸として保存するため、「一般再現性は高いが
    本人適合度は低い」という指示書10番の状態を表現できる。"""
    n = (success or 0) + (failure or 0) + (neutral or 0)
    shrunk_rate = ((success or 0) + USER_COMPAT_PRIOR_N * USER_COMPAT_PRIOR_RATE) / (n + USER_COMPAT_PRIOR_N)
    return_component = max(-20, min(20, avg_return)) if avg_return is not None else 0
    score = shrunk_rate * 100 * 0.7 + (50 + return_component) * 0.3
    return round(max(0, min(100, score)), 1)


def record_trade_outcome_for_playbooks(database_url, user_id, code, trade):
    """指示書8番：売却確定後に呼ぶ（server.py側でadd_position_exit成功後にベストエフォート
    で呼ぶ想定、失敗しても売却本体には影響させない）。直近のanalysis_context_log
    （この銘柄・保有期間中に記録されたもの）からused_context.playbooksを集め、実際の
    売買結果と紐づけてtrade_playbook_user_stats（PRIVATE、本人の実践成績専用テーブル）の
    attempt/success/failure_count・avg_return・compatibility_scoreを更新する。
    Phase MU-S3C（不具合是正）：以前はtrade_playbooks本体（GLOBAL定義と同一行）へ
    user_attempt_count等を書き込み、さらに個人の勝敗をevidence_count/success_count/
    failure_countという「一般的な再現性」の指標にまで混入させていた。GLOBAL化した際に
    他ユーザーへ個人の実績が透けて見える設計だったため、個人成績は完全に別テーブルへ分離し、
    playbook本体の一般的evidence/success/failure_countは一切更新しない（個人の売買結果は
    「一般的な再現性の証拠」には使わない方針に変更）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return {"updated": 0}
    net_pnl = trade.get("net_pnl") if trade.get("net_pnl") is not None else trade.get("pnl")
    entry_price, shares = trade.get("entry_price"), trade.get("shares")
    return_pct = None
    if entry_price and shares and net_pnl is not None:
        cost = entry_price * shares
        if cost:
            return_pct = (net_pnl / cost) * 100
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT used_context_json FROM analysis_context_log WHERE user_id=%s AND code=%s "
                "ORDER BY created_at DESC LIMIT 20", [user_id, code])
            logs = cur.fetchall()
    playbook_ids = set()
    for log in logs:
        playbook_ids |= {pid for pid in ((log.get("used_context_json") or {}).get("playbooks") or []) if pid is not None}
    if not playbook_ids:
        return {"updated": 0}
    is_success = (net_pnl or 0) > 0
    updated = 0
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            for pid in playbook_ids:
                # GLOBAL（visibility='GLOBAL'、user_id=_SHARED_SCOPE）のplaybookで練習した場合も
                # 対象にする（本人所有のUSERプレイブックに限定しない）。
                cur.execute("SELECT id FROM trade_playbooks WHERE id=%s AND user_id IN (%s, %s)",
                            [pid, user_id, _SHARED_SCOPE])
                if cur.fetchone() is None:
                    continue
                cur.execute(
                    "SELECT * FROM trade_playbook_user_stats WHERE user_id=%s AND playbook_id=%s",
                    [user_id, pid])
                stats = cur.fetchone()
                prev_n = (stats["attempt_count"] if stats else 0) or 0
                prev_success = (stats["success_count"] if stats else 0) or 0
                prev_failure = (stats["failure_count"] if stats else 0) or 0
                prev_avg = stats.get("avg_return") if stats else None
                attempt = prev_n + 1
                success = prev_success + (1 if is_success else 0)
                failure = prev_failure + (0 if is_success else 1)
                if return_pct is not None:
                    new_avg = ((prev_avg or 0) * prev_n + return_pct) / attempt
                else:
                    new_avg = prev_avg
                compat = compute_user_compatibility_score(success, failure, attempt - success - failure, new_avg)
                cur.execute(
                    "INSERT INTO trade_playbook_user_stats (user_id, playbook_id, attempt_count, "
                    "success_count, failure_count, avg_return, compatibility_score, updated_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,now()) "
                    "ON CONFLICT (user_id, playbook_id) DO UPDATE SET "
                    "attempt_count=EXCLUDED.attempt_count, success_count=EXCLUDED.success_count, "
                    "failure_count=EXCLUDED.failure_count, avg_return=EXCLUDED.avg_return, "
                    "compatibility_score=EXCLUDED.compatibility_score, updated_at=now()",
                    [user_id, pid, attempt, success, failure, new_avg, compat])
                updated += 1
        conn.commit()
    return {"updated": updated, "returnPct": return_pct}


# ---- 過去文章からのplaybook候補化（指示書11・12番） ----

def backfill_playbook_candidates_from_expert_views(database_url, user_id):
    """expert_viewsのうちconfirmations/invalidation_conditionsが明確に構造化されている
    （短いフレーズの配列として既に保存済み）ものだけをplaybook候補化する（指示書11番）。
    thesisのような自由文はDSL条件として技術的シグナルと照合できないため含めない
    （「曖昧な一般論はplaybook化しない」＝マッチング可能な構造化データが無ければ
    作らない、という実務的な解釈）。自動ACTIVE禁止（created_from=legacy_text、
    source_type=BACKFILL、常にTESTING/LOW、指示書12番）。戻り値: 新規作成件数。"""
    views = list_expert_views(database_url, user_id, limit=300)
    created = 0
    for v in views:
        confirmations = v.get("confirmations") or []
        invalidations = v.get("invalidation_conditions") or []
        if not confirmations and not invalidations:
            continue
        pb = {
            "name": f"{v['expert_name']}見解由来（{v.get('published_at')}）",
            "source_trader": v["expert_name"], "source_title": v.get("source_title"),
            "source_type": "BACKFILL", "timeframe": v.get("time_horizon"),
            "applicable_sectors": [v["sector"]] if v.get("sector") else [],
            "applicable_stocks": v.get("stocks") or [],
            "confirmation_conditions": confirmations,
            "avoid_conditions": invalidations,
        }
        res = upsert_trade_playbook(database_url, user_id, pb, created_from="legacy_text")
        if res and res["action"] == "created":
            created += 1
    return created


# ---- playbook重複防止：条件の正規化類似度（指示書13番） ----

def _serialize_playbook_conditions(pb):
    parts = []
    for key in ("entry_conditions_json", "confirmation_conditions_json", "exit_conditions_json",
                "stop_conditions_json", "avoid_conditions_json"):
        for c in (pb.get(key) or []):
            parts.append(c if isinstance(c, str) else json.dumps(c, ensure_ascii=False, sort_keys=True))
    return _normalize_rule_key(" ".join(parts))


def find_similar_trade_playbooks(database_url, user_id, pb, exclude_id=None, limit=5):
    """指示書13番：名前だけでなくentry/exit/avoid条件の正規化類似度（trade_rulesと同じ
    _rule_similarity・バイグラムJaccard）で重複候補を検出する。自動統合・削除はしない。"""
    key = _serialize_playbook_conditions(pb)
    if not key:
        return []
    existing = list_trade_playbooks(database_url, user_id)
    scored = []
    for e in existing:
        if exclude_id and e["id"] == exclude_id:
            continue
        ekey = _serialize_playbook_conditions(e)
        if not ekey:
            continue
        sim = _rule_similarity(key, ekey)
        if sim >= _RULE_SIMILARITY_THRESHOLD:
            scored.append({"similarity": round(sim, 3), **e})
    scored.sort(key=lambda x: -x["similarity"])
    return scored[:limit]


# ---- TOP5相談：軽量な候補別フラグ（指示書4番） ----

def lightweight_context_flags_for_codes(database_url, user_id, codes, sector_map=None):
    """TOP5相談用の軽量版。各候補についてフルコンテキストを取得するのではなく、
    event_risk・直近ネガティブニュース有無・行動警告有無だけを安く返す（指示書4番
    「TOP5の順位自体を大きく自動変更しすぎず、まずは補助判断・注意フラグとして利用」）。"""
    sector_map = sector_map or {}
    behavioral = behavioral_warnings_from_reflections(database_url, user_id)
    has_behavioral_warning = len(behavioral) > 0
    out = {}
    for code in codes:
        sector = sector_map.get(code)
        event_info = upcoming_event_signals(database_url, user_id, code=code, sector=sector)
        news = relevant_catalysts_for(database_url, user_id, code=code, sector=sector, limit=3)
        out[code] = {
            "eventRisk": "EVENT_RISK_HIGH" in event_info["signals"],
            "negativeNews": any(n.get("sentiment") == "negative" for n in news),
            "behavioralWarning": has_behavioral_warning,
        }
    return out


# ---- 日次レビューとplaybook評価の接続（指示書18番） ----

def _check_playbook_discipline(database_url, user_id, exits_today):
    """その日の決済（trade_history）がplaybookのavoid条件発動を無視していなかったか、
    playbookを参照して判断していたかを日次レビューへ反映する（指示書18番）。"""
    good, bad = [], []
    if not exits_today:
        return good, bad
    pool = _get_pool(database_url)
    if pool is None:
        return good, bad
    for t in exits_today:
        code = t.get("code")
        with pool.connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT used_context_json FROM analysis_context_log WHERE user_id=%s AND code=%s "
                    "ORDER BY created_at DESC LIMIT 5", [user_id, code])
                logs = cur.fetchall()
        label = t.get("name") or code
        avoid_seen = any((log.get("used_context_json") or {}).get("avoid_playbook_ids") for log in logs)
        pb_ids = set()
        for log in logs:
            pb_ids |= set((log.get("used_context_json") or {}).get("playbooks") or [])
        if avoid_seen:
            bad.append(f"{label}：プレイブック回避条件が発動していた履歴あり")
        elif pb_ids:
            good.append(f"{label}：プレイブックを参照して決済判断")
    return good, bad


# ============================================================
# ---- 朝一マーケット自動分析システム（MorningMarketCheck）。2026-09-10新規 ----
# 実際の市場データ取得・分析はserver.py側（market_data_service/morning_analysis_engine/
# morning_report_service）が担う。ここではNeonへの保存・取得（morning_report_serviceの
# 永続化部分）だけを持つ。
# ============================================================

_MORNING_CHECK_JSON_COLS = [
    "indices_json", "fx_json", "commodities_json", "adr_json", "data_quality_json",
    "strong_sectors_json", "weak_sectors_json", "watchlist_top5_json", "avoid_stocks_json",
    "resilience_json", "market_risk_warnings_json", "event_risk_json",
    "strategy_json", "raw_payload_json",
    "market_news_context_json",  # News Intelligence Phase 2（指示書12）
    "external_intelligence_json",  # X Intelligence Phase5（2026-09-15）
]
_MORNING_CHECK_SCALAR_COLS = [
    "market_regime", "volatility_regime", "trend_type", "market_risk_score", "volatility_score",
    "trend_score", "macro_pressure_score", "strategy_text",
]
# Phase MU-S3C：個人の保有ポジション由来のためPRIVATE overlay専用（morning_market_checks本体
# には保存しない）。
_MORNING_CHECK_PRIVATE_JSON_COLS = ["position_risk_json", "position_critical_warnings_json"]


def _merge_morning_check_overlay(shared_row, overlay_row):
    """SHARED本体1行 + PRIVATE overlay1行（無ければNone）を、呼び出し元向けに旧来と同じ
    形（risk_warnings_json＝market_risk_warnings_json+position_critical_warnings_json、
    position_risk_json）へ合成する。DBには保存しない、レスポンス組み立て専用。"""
    if shared_row is None:
        return None
    out = dict(shared_row)
    market_warnings = out.pop("market_risk_warnings_json", None) or []
    if overlay_row:
        position_risk = overlay_row.get("position_risk_json") or []
        critical_warnings = overlay_row.get("position_critical_warnings_json") or []
        out["is_read"] = overlay_row.get("is_read", False)
    else:
        position_risk, critical_warnings = [], []
        out["is_read"] = False
    out["position_risk_json"] = position_risk
    out["risk_warnings_json"] = (critical_warnings + market_warnings)[:3]
    out["market_risk_warnings_json"] = market_warnings
    return out


def save_morning_check(database_url, user_id, check_date, snapshot_time, data):
    """1回分のMorningMarketCheckを保存する（(check_date, snapshot_time)でUNIQUE、同一時間帯の
    再生成＝手動再分析はON CONFLICTで上書き更新）。dataは_MORNING_CHECK_SCALAR_COLS/
    _MORNING_CHECK_JSON_COLS/_MORNING_CHECK_PRIVATE_JSON_COLSのキーを持つdict
    （data["risk_warnings_json"]が渡された場合はmarket_risk_warnings_jsonとして扱う
    後方互換フォールバック付き）。
    Phase MU-S3C：SHARED MARKET CORE（morning_market_checks、_shared固定）とPRIVATE USER
    OVERLAY（morning_market_check_private_overlay、呼び出しユーザー自身のposition_risk等）に
    分けて保存する。戻り値は両方を合成した従来と同じ形のdict（DBには合成しない）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return None
    shared_data = dict(data)
    if "market_risk_warnings_json" not in shared_data and "risk_warnings_json" in shared_data:
        shared_data["market_risk_warnings_json"] = shared_data["risk_warnings_json"]
    cols = _MORNING_CHECK_SCALAR_COLS + _MORNING_CHECK_JSON_COLS
    values = []
    for c in cols:
        if c in _MORNING_CHECK_JSON_COLS:
            values.append(json.dumps(shared_data.get(c), ensure_ascii=False))
        else:
            values.append(shared_data.get(c))
    placeholders = ", ".join(["%s::jsonb" if c in _MORNING_CHECK_JSON_COLS else "%s" for c in cols])
    update_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols)
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"INSERT INTO morning_market_checks (user_id, check_date, snapshot_time, {', '.join(cols)}) "
                f"VALUES (%s, %s, %s, {placeholders}) "
                f"ON CONFLICT (user_id, check_date, snapshot_time) DO UPDATE SET "
                f"{update_clause}, generated_at = now() "
                f"RETURNING *",
                [_SHARED_SCOPE, check_date, snapshot_time] + values,
            )
            shared_row = cur.fetchone()
            cur.execute(
                "INSERT INTO morning_market_check_private_overlay "
                "(user_id, check_id, check_date, snapshot_time, position_risk_json, "
                "position_critical_warnings_json, is_read) "
                "VALUES (%s,%s,%s,%s,%s::jsonb,%s::jsonb,false) "
                "ON CONFLICT (user_id, check_id) DO UPDATE SET "
                "position_risk_json=EXCLUDED.position_risk_json, "
                "position_critical_warnings_json=EXCLUDED.position_critical_warnings_json, "
                "is_read=false, updated_at=now() RETURNING *",
                [user_id, shared_row["id"], check_date, snapshot_time,
                 json.dumps(data.get("position_risk_json") or [], ensure_ascii=False),
                 json.dumps(data.get("position_critical_warnings_json") or [], ensure_ascii=False)])
            overlay_row = cur.fetchone()
        conn.commit()
    return _merge_morning_check_overlay(_row_to_json(shared_row), _row_to_json(overlay_row))


def get_latest_morning_check(database_url, user_id, check_date=None):
    """当日（省略時は今日）分の最新MorningMarketCheckを1件返す（無ければNone）。
    Phase MU-S3C：SHARED本体は全ユーザー共通、呼び出しユーザー自身のPRIVATE overlay
    （position_risk等）をその場で合成する（他ユーザーのoverlayが混ざることは無い）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return None
    check_date = check_date or datetime.date.today().isoformat()
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM morning_market_checks WHERE user_id=%s AND check_date=%s "
                "ORDER BY generated_at DESC LIMIT 1", [_SHARED_SCOPE, check_date])
            row = cur.fetchone()
            if row is None:
                return None
            cur.execute(
                "SELECT * FROM morning_market_check_private_overlay WHERE user_id=%s AND check_id=%s",
                [user_id, row["id"]])
            overlay = cur.fetchone()
    return _merge_morning_check_overlay(_row_to_json(row), _row_to_json(overlay) if overlay else None)


def list_morning_checks(database_url, user_id, check_date=None, limit=10):
    """当日（省略時は今日）分のMorningMarketCheckを時系列（古い→新しい）で返す。
    Phase MU-S3C：呼び出しユーザー自身のPRIVATE overlayをその場で合成する。"""
    pool = _get_pool(database_url)
    if pool is None:
        return []
    check_date = check_date or datetime.date.today().isoformat()
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM morning_market_checks WHERE user_id=%s AND check_date=%s "
                "ORDER BY generated_at ASC LIMIT %s", [_SHARED_SCOPE, check_date, limit])
            rows = cur.fetchall()
            if not rows:
                return []
            ids = [r["id"] for r in rows]
            cur.execute(
                "SELECT * FROM morning_market_check_private_overlay WHERE user_id=%s AND check_id = ANY(%s)",
                [user_id, ids])
            overlays = {o["check_id"]: o for o in cur.fetchall()}
    return [_merge_morning_check_overlay(_row_to_json(r), _row_to_json(overlays[r["id"]]) if r["id"] in overlays else None)
            for r in rows]


def mark_morning_check_read(database_url, user_id, check_id):
    """Phase MU-S3C：is_readは個人のUI既読状態のためPRIVATE overlay側で管理する
    （overlay行が無ければ作成、他ユーザーの既読状態には一切影響しない）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return False
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO morning_market_check_private_overlay "
            "(user_id, check_id, check_date, snapshot_time, position_risk_json, "
            "position_critical_warnings_json, is_read) "
            "SELECT %s, id, check_date, snapshot_time, '[]'::jsonb, '[]'::jsonb, true "
            "FROM morning_market_checks WHERE id=%s "
            "ON CONFLICT (user_id, check_id) DO UPDATE SET is_read=true, updated_at=now()",
            [user_id, check_id])
        conn.commit()
    return True


# ============================================================
# ---- Market Intelligence Timeline（場中定時レポート）。2026-09-10新規、Phase2-A ----
# 実際の市場データ取得・分析はserver.py側（intraday_analysis_engine/market_report_service）
# が担う。ここではNeonへの保存・取得だけを持つ（MorningMarketCheckと同じ役割分担）。
# ============================================================

_MARKET_INTEL_JSON_COLS = [
    "sector_snapshot_json", "strong_sectors_json", "weak_sectors_json", "top_stocks_json",
    "resilience_stocks_json", "momentum_stocks_json", "missed_opportunities_json",
    "morning_thesis_evaluation_json", "risk_alerts_json", "position_alerts_json",
    "news_changes_json", "event_risk_json", "strategy_update_json", "data_health_json",
    "social_signals_json",
    "market_news_context_json",  # News Intelligence Phase 2（指示書12）
    "external_intelligence_json",  # X Intelligence Phase5（2026-09-15）
]
_MARKET_INTEL_SCALAR_COLS = [
    "scheduled_time", "morning_check_id", "market_regime", "volatility_regime", "market_summary",
    "nikkei_change_pct", "topix_change_pct", "growth250_change_pct", "nikkei_vi", "usdjpy",
]


def save_market_intelligence_report(database_url, user_id, trade_date, report_type, data):
    """1回分のMarket Intelligence Timelineレポートを保存する（(user_id, trade_date,
    report_type)でUNIQUE、手動再生成「今すぐ分析」はON CONFLICTで上書き＝重複レコードを
    作らない、指示書4番）。Phase MU-S1：market_intelligence_reportsはSHARED化済み。"""
    user_id = _SHARED_SCOPE
    pool = _get_pool(database_url)
    if pool is None:
        return None
    cols = _MARKET_INTEL_SCALAR_COLS + _MARKET_INTEL_JSON_COLS
    values = []
    for c in cols:
        if c in _MARKET_INTEL_JSON_COLS:
            values.append(json.dumps(data.get(c), ensure_ascii=False))
        else:
            values.append(data.get(c))
    placeholders = ", ".join(["%s::jsonb" if c in _MARKET_INTEL_JSON_COLS else "%s" for c in cols])
    update_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols)
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"INSERT INTO market_intelligence_reports (user_id, trade_date, report_type, {', '.join(cols)}) "
                f"VALUES (%s, %s, %s, {placeholders}) "
                f"ON CONFLICT (user_id, trade_date, report_type) DO UPDATE SET "
                f"{update_clause}, generated_at = now(), updated_at = now() "
                f"RETURNING *",
                [user_id, trade_date, report_type] + values,
            )
            saved = cur.fetchone()
        conn.commit()
    return _row_to_json(saved)


def list_market_intelligence_reports(database_url, user_id, trade_date=None):
    """当日（省略時は今日）分のMarket Intelligence Timelineを時系列（古い→新しい）で返す。
    Phase MU-S1：market_intelligence_reportsはSHARED化済み。"""
    user_id = _SHARED_SCOPE
    pool = _get_pool(database_url)
    if pool is None:
        return []
    trade_date = trade_date or datetime.date.today().isoformat()
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM market_intelligence_reports WHERE user_id=%s AND trade_date=%s "
                "ORDER BY generated_at ASC", [user_id, trade_date])
            rows = cur.fetchall()
    return [_row_to_json(r) for r in rows]


def get_market_intelligence_report(database_url, user_id, trade_date, report_type):
    # Phase MU-S1：market_intelligence_reportsはSHARED化済み。
    user_id = _SHARED_SCOPE
    pool = _get_pool(database_url)
    if pool is None:
        return None
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM market_intelligence_reports WHERE user_id=%s AND trade_date=%s AND report_type=%s",
                [user_id, trade_date, report_type])
            row = cur.fetchone()
    return _row_to_json(row) if row else None


# ---- stock_theses（Market Intelligence Timeline Phase2-C「今買い時TOP5＋Thesis永続化」） ----

def ensure_stock_thesis(database_url, user_id, code, market, entry_date, name, entry_score, entry_state, reasons, analysis_confidence,
                          source="MORNING", morning_check_id=None, morning_rank=None, vwap_state=None,
                          auto_rs=None, auto_sector=None, resilience=None, trigger_text=None, avoid_condition=None, morning_price=None):
    """朝TOP5（08:50 MorningMarketCheck生成時点で確定するentry_ready_top5）に選ばれた銘柄の
    仮説を新規作成する。2026-09-10更新（Phase2-C「朝TOP5成績評価」）：以後はMorningMarketCheck
    経由（source='MORNING'）だけが仮説を作る——Current TOP5（日中いつでも再計算できる表示用の
    一覧）は仮説を作らない設計に変更した（「朝TOP5は成績評価用として固定し、後から書き換えない」
    「Current TOP5は朝TOP5とは別物」という指示書22・23番の要件）。同じ(user_id, code, market,
    entry_date)が既にあれば何もしない（1日1仮説）。戻り値：作成したら新規行(dict)、既存行が
    あればNone。Phase MU-S1：stock_thesesはSHARED化済み。"""
    user_id = _SHARED_SCOPE
    pool = _get_pool(database_url)
    if pool is None:
        return None
    history = [{"at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "status": "ACTIVE", "entry_score": entry_score, "note": "朝TOP5選出"}]
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "INSERT INTO stock_theses (user_id, code, market, entry_date, name, initial_entry_score, "
                "initial_entry_state, initial_reasons_json, latest_entry_score, analysis_confidence, "
                "thesis_status, status_history_json, source, morning_check_id, morning_rank, vwap_state, "
                "auto_rs, auto_sector, resilience, trigger_text, avoid_condition, morning_price) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,'ACTIVE',%s::jsonb,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (user_id, code, market, entry_date) DO NOTHING "
                "RETURNING *",
                [user_id, code, market, entry_date, name, entry_score, entry_state,
                 json.dumps(reasons or [], ensure_ascii=False), entry_score, analysis_confidence,
                 json.dumps(history, ensure_ascii=False), source, morning_check_id, morning_rank, vwap_state,
                 auto_rs, auto_sector, resilience, trigger_text, avoid_condition, morning_price])
            row = cur.fetchone()
        conn.commit()
    return _row_to_json(row) if row else None


def list_active_stock_theses(database_url, user_id, entry_date):
    """当日分の、まだ最終確定（final_result未設定）していない仮説を返す（答え合わせ対象）。
    Phase MU-S1：stock_thesesはSHARED化済み。"""
    user_id = _SHARED_SCOPE
    pool = _get_pool(database_url)
    if pool is None:
        return []
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM stock_theses WHERE user_id=%s AND entry_date=%s AND final_result IS NULL",
                [user_id, entry_date])
            rows = cur.fetchall()
    return [_row_to_json(r) for r in rows]


def update_stock_thesis_evaluation(database_url, user_id, code, market, entry_date, thesis_result, transition_status, entry_score, note):
    """答え合わせ1回分を反映する。thesis_result＝evaluate_morning_thesis()の生の結果
    （次回のtransition計算の入力として保存）、transition_status＝前回からの変化
    （STRENGTHENED/MAINTAINED/WEAKENED/FAILED、初回はNone）。表示用thesis_statusは
    transition_statusがあればそれ、無ければthesis_resultをそのまま使う（指示書の
    ACTIVE→結果→変化、というライフサイクルを1列に反映）。Phase MU-S1：stock_thesesはSHARED化済み。"""
    user_id = _SHARED_SCOPE
    pool = _get_pool(database_url)
    if pool is None:
        return None
    visible_status = transition_status or thesis_result
    entry = {"at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "status": visible_status, "thesis_result": thesis_result,
              "entry_score": entry_score, "note": note}
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "UPDATE stock_theses SET latest_thesis_result=%s, thesis_status=%s, latest_entry_score=%s, "
                "status_history_json = status_history_json || %s::jsonb, updated_at=now() "
                "WHERE user_id=%s AND code=%s AND market=%s AND entry_date=%s AND final_result IS NULL "
                "RETURNING *",
                [thesis_result, visible_status, entry_score, json.dumps([entry], ensure_ascii=False),
                 user_id, code, market, entry_date])
            row = cur.fetchone()
        conn.commit()
    return _row_to_json(row) if row else None


def finalize_stock_thesis(database_url, user_id, code, market, entry_date, final_result):
    """大引け時点の最終結果を確定する（以後、答え合わせ対象から外れる）。
    Phase MU-S1：stock_thesesはSHARED化済み。"""
    user_id = _SHARED_SCOPE
    pool = _get_pool(database_url)
    if pool is None:
        return None
    entry = {"at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "status": final_result, "note": "大引け確定"}
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "UPDATE stock_theses SET final_result=%s, thesis_status=%s, "
                "status_history_json = status_history_json || %s::jsonb, updated_at=now() "
                "WHERE user_id=%s AND code=%s AND market=%s AND entry_date=%s AND final_result IS NULL "
                "RETURNING *",
                [final_result, final_result, json.dumps([entry], ensure_ascii=False),
                 user_id, code, market, entry_date])
            row = cur.fetchone()
        conn.commit()
    return _row_to_json(row) if row else None


def list_stock_theses(database_url, user_id, from_date=None, to_date=None, limit=200):
    """成績評価画面向け：期間内の仮説を新しい順で返す。Phase MU-S1：stock_thesesはSHARED化済み。"""
    user_id = _SHARED_SCOPE
    pool = _get_pool(database_url)
    if pool is None:
        return []
    where = ["user_id=%s"]
    params = [user_id]
    if from_date:
        where.append("entry_date >= %s")
        params.append(from_date)
    if to_date:
        where.append("entry_date <= %s")
        params.append(to_date)
    params.append(limit)
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"SELECT * FROM stock_theses WHERE {' AND '.join(where)} "
                f"ORDER BY entry_date DESC, id DESC LIMIT %s", params)
            rows = cur.fetchall()
    return [_row_to_json(r) for r in rows]


def get_stock_thesis_stats(database_url, user_id, days=30):
    """直近days日分のfinal_result内訳（成績評価）を集計する。final_result未確定（当日進行中）
    の仮説は集計に含めない。2026-09-10更新（Phase2-C指示書19番）：hit_rateはSUCCESS=1点・
    PARTIAL_SUCCESS=0.5点・FAIL=0点のsuccess_equivalentを(SUCCESS+PARTIAL+FAIL)で割る
    （PARTIAL_SUCCESSを満点扱いしていた旧実装から修正）。NO_ENTRY/DATA_INSUFFICIENTは
    「そもそもエントリー機会が無かった/判定不能」であり狙いが外れたわけではないため、
    分母から除外する（指示書「NO_ENTRYは分母から除外」）。Phase MU-S1：stock_thesesはSHARED化済み。"""
    user_id = _SHARED_SCOPE
    pool = _get_pool(database_url)
    if pool is None:
        return {"totalFinalized": 0, "byResult": {}, "winRate": None}
    since = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT final_result, COUNT(*) AS n FROM stock_theses "
                "WHERE user_id=%s AND entry_date >= %s AND final_result IS NOT NULL "
                "GROUP BY final_result", [user_id, since])
            rows = cur.fetchall()
    by_result = {r["final_result"]: r["n"] for r in rows}
    total = sum(by_result.values())
    success_n = by_result.get("SUCCESS", 0)
    partial_n = by_result.get("PARTIAL_SUCCESS", 0)
    fail_n = by_result.get("FAIL", 0)
    decided = success_n + partial_n + fail_n
    success_equivalent = success_n * 1.0 + partial_n * 0.5 + fail_n * 0.0
    win_rate = round(success_equivalent / decided * 100, 1) if decided else None
    return {"totalFinalized": total, "byResult": by_result, "winRate": win_rate, "sinceDate": since}


# ---- market_sources / social_market_posts（にこそく@nicosokufx X投稿連携。2026-09-10新規） ----

def ensure_market_source(database_url, platform, handle, display_name=None, priority="HIGH", categories=None,
                          source_type=None, poll_interval_market_sec=None, poll_interval_off_sec=None,
                          strengths=None, evaluation_modes=None):
    """(platform, handle)の設定行を作る（無ければ）。既存があれば何もしない・既存の
    enabled/priority設定は上書きしない（ユーザーが後で無効化した場合に自動復活させない
    ため）。Phase6（指示書1・13番）：source_type・ポーリング間隔・capability profile
    （strengths/evaluation_modes）を初期登録できるよう拡張。これらも既存行があれば上書き
    しない（設定はDBが唯一の真実、コード側の初期値は「初回のみ」の種に過ぎない）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return None
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "INSERT INTO market_sources (platform, handle, display_name, priority, categories_json, "
                "source_type, poll_interval_market_sec, poll_interval_off_sec, strengths_json, evaluation_modes_json) "
                "VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s::jsonb,%s::jsonb) "
                "ON CONFLICT (platform, handle) DO NOTHING RETURNING *",
                [platform, handle, display_name, priority, json.dumps(categories or [], ensure_ascii=False),
                 source_type, poll_interval_market_sec, poll_interval_off_sec,
                 json.dumps(strengths or [], ensure_ascii=False), json.dumps(evaluation_modes or [], ensure_ascii=False)])
            row = cur.fetchone()
        conn.commit()
    return _row_to_json(row) if row else get_market_source(database_url, handle, platform)


def get_market_source(database_url, handle, platform="X"):
    pool = _get_pool(database_url)
    if pool is None:
        return None
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM market_sources WHERE platform=%s AND handle=%s", [platform, handle])
            row = cur.fetchone()
    return _row_to_json(row) if row else None


def list_market_sources(database_url, enabled_only=False, platform=None):
    """Phase6新規（指示書1・29・31番）：GET /api/market-sources・複数source対応pollerの
    走査対象取得用。"""
    pool = _get_pool(database_url)
    if pool is None:
        return []
    where, params = [], []
    if enabled_only:
        where.append("enabled = true")
    if platform:
        where.append("platform = %s")
        params.append(platform)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(f"SELECT * FROM market_sources {clause} ORDER BY handle", params)
            rows = cur.fetchall()
    return [_row_to_json(r) for r in rows]


def update_market_source_status(database_url, handle, platform="X", last_seen_post_id=None,
                                  mark_success=False, last_error=None, resolved_user_id_note=None):
    """ポーリング1サイクル分の結果を反映する。last_seen_post_id指定時のみ更新（Noneのままなら
    既存値を保持）。mark_success=Trueでlast_success_at=now()・last_error=NULLにする
    （成功したのにエラーが残ったままにならないように）。last_error指定時はエラー内容を記録
    （X_SOURCE_STATUS=DEGRADED表示に使う）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return None
    sets, params = [], []
    if last_seen_post_id is not None:
        sets.append("last_seen_post_id=%s")
        params.append(last_seen_post_id)
    if mark_success:
        sets.append("last_success_at=now()")
        sets.append("last_error=NULL")
    if last_error is not None:
        sets.append("last_error=%s")
        params.append(last_error)
    if not sets:
        return get_market_source(database_url, handle, platform)
    sets.append("updated_at=now()")
    params += [platform, handle]
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"UPDATE market_sources SET {', '.join(sets)} WHERE platform=%s AND handle=%s RETURNING *",
                params)
            row = cur.fetchone()
        conn.commit()
    return _row_to_json(row) if row else None


_SOCIAL_POST_JSON_COLS = ["media_json", "quoted_post_json", "public_metrics_json", "categories_json",
                           "facts_json", "author_opinion_json", "system_inference_json",
                           "direct_mentions_json", "theme_related_json", "image_analysis_json",
                           "prediction_market_json", "stock_breaking_json", "corporate_breaking_json"]


def insert_social_post_if_new(database_url, post):
    """postは{source_type,source_name,source_handle,post_id,posted_at,text,url,media,
    quoted_post,public_metrics,categories,importance,facts,author_opinion,system_inference,
    direct_mentions,theme_related,verification_status}を含むdict。(source_handle,post_id)の
    UNIQUE制約により、既に取り込み済みの投稿は何もしない（指示書2・17番「同一投稿を何度も
    処理しない」）。Phase6（指示書5・8・10・11・12・16番）：post_classification・
    prediction_market/stock_breaking/corporate_breaking_json・primary_source_url/type・
    discovered_via_social・intelligence_cluster_idを追加専用で受け取る（無ければNULLのまま、
    既存のにこそく投稿には一切影響しない）。戻り値：新規なら作成行(dict)、既存ならNone。"""
    pool = _get_pool(database_url)
    if pool is None:
        return None
    key_to_col = {"media": "media_json", "quoted_post": "quoted_post_json",
                  "public_metrics": "public_metrics_json", "categories": "categories_json",
                  "facts": "facts_json", "author_opinion": "author_opinion_json",
                  "system_inference": "system_inference_json", "direct_mentions": "direct_mentions_json",
                  "theme_related": "theme_related_json", "prediction_market": "prediction_market_json",
                  "stock_breaking": "stock_breaking_json", "corporate_breaking": "corporate_breaking_json"}
    cols = ["source_type", "source_name", "source_handle", "post_id", "posted_at", "text", "url",
            "media_json", "quoted_post_json", "public_metrics_json", "categories_json", "importance",
            "facts_json", "author_opinion_json", "system_inference_json", "direct_mentions_json",
            "theme_related_json", "verification_status", "image_analysis_status",
            "post_classification", "prediction_market_json", "stock_breaking_json", "corporate_breaking_json",
            "primary_source_url", "primary_source_type", "discovered_via_social", "intelligence_cluster_id"]
    values = []
    for c in cols:
        if c == "image_analysis_status":
            # Phase2（指示書1番）：画像付き投稿はPENDING（解析待ち）、画像なしはNONE
            # （そもそも対象外を明示、旧NULLから変更）。
            values.append("PENDING" if post.get("media") else "NONE")
            continue
        src_key = next((k for k, v in key_to_col.items() if v == c), c)
        v = post.get(src_key)
        values.append(json.dumps(v, ensure_ascii=False) if c in _SOCIAL_POST_JSON_COLS else v)
    placeholders = ", ".join(["%s::jsonb" if c in _SOCIAL_POST_JSON_COLS else "%s" for c in cols])
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"INSERT INTO social_market_posts ({', '.join(cols)}) VALUES ({placeholders}) "
                f"ON CONFLICT (source_handle, post_id) DO NOTHING RETURNING *",
                values)
            row = cur.fetchone()
        conn.commit()
    return _row_to_json(row) if row else None


def save_social_post_image_analysis(database_url, source_handle, post_id, image_analysis):
    """指示書4番・画像解析待ちキュー指示書：画像の構造化解析結果（ユーザーがChatGPT等で解析
    した結果のJSON貼り付け）を既存投稿へ追記する。post自体は再取得しない（analysisだけの
    更新）。保存に成功したらimage_analysis_status を PENDING→ANALYZED へ進める。Phase2
    （指示書2番）：正常保存なのでimage_analysis_errorはNULLへ戻す。戻り値がNoneなのは
    対象投稿が見つからない場合（呼び出し側で「見つからない」エラーとして扱う）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return None
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "UPDATE social_market_posts SET image_analysis_json=%s::jsonb, "
                "image_analysis_status='ANALYZED', image_analysis_error=NULL, "
                "image_analysis_updated_at=now(), updated_at=now() "
                "WHERE source_handle=%s AND post_id=%s RETURNING *",
                [json.dumps(image_analysis or [], ensure_ascii=False), source_handle, post_id])
            row = cur.fetchone()
        conn.commit()
    return _row_to_json(row) if row else None


def mark_social_post_image_analysis_failed(database_url, source_handle, post_id, error_message):
    """Phase2（指示書2番）：Smart Importでの画像解析保存に失敗した場合に呼ぶ。
    image_analysis_status=FAILED・image_analysis_errorへ理由を記録する。対象投稿が無ければ
    Noneを返す（呼び出し側でpost_id不明エラーとして扱う）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return None
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "UPDATE social_market_posts SET image_analysis_status='FAILED', "
                "image_analysis_error=%s, image_analysis_updated_at=now(), updated_at=now() "
                "WHERE source_handle=%s AND post_id=%s RETURNING *",
                [str(error_message or "")[:2000], source_handle, post_id])
            row = cur.fetchone()
        conn.commit()
    return _row_to_json(row) if row else None


_SOCIAL_IMAGE_ANALYSIS_STATUSES = {"NONE", "PENDING", "ANALYZED", "SKIPPED", "FAILED"}


def set_social_post_image_analysis_status(database_url, source_handle, post_id, status):
    """Phase2（指示書3・4番）：SKIPPED化（「解析不要」ボタン）と、ANALYZED/FAILED/SKIPPEDから
    PENDINGへ戻す再解析操作、の両方に使う汎用の状態遷移関数。image_analysis_json自体は
    削除しない（指示書4番「即削除せず、再解析結果保存時に上書きする」）。不正なstatus値は
    何もせずNoneを返す。"""
    if status not in _SOCIAL_IMAGE_ANALYSIS_STATUSES:
        return None
    pool = _get_pool(database_url)
    if pool is None:
        return None
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "UPDATE social_market_posts SET image_analysis_status=%s, "
                "image_analysis_error=CASE WHEN %s='FAILED' THEN image_analysis_error ELSE NULL END, "
                "image_analysis_updated_at=now(), updated_at=now() "
                "WHERE source_handle=%s AND post_id=%s RETURNING *",
                [status, status, source_handle, post_id])
            row = cur.fetchone()
        conn.commit()
    return _row_to_json(row) if row else None


def update_market_source_fetch_stats(database_url, handle, platform="X", **fields):
    """Phase2（指示書11・17番）：1回のfetch-now/ポーリングサイクルの統計を記録する。
    fieldsはlast_fetch_started_at/last_fetch_finished_at/last_error_at/last_http_status/
    last_fetched_count/last_inserted_count/last_duplicate_countのいずれか（未指定のキーは
    更新しない）。Bearer Token等の秘密情報はここでは一切扱わない。"""
    allowed = {"last_fetch_started_at", "last_fetch_finished_at", "last_error_at", "last_http_status",
               "last_fetched_count", "last_inserted_count", "last_duplicate_count"}
    sets, params = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if v == "NOW()":
            sets.append(f"{k}=now()")
        else:
            sets.append(f"{k}=%s")
            params.append(v)
    if not sets:
        return None
    pool = _get_pool(database_url)
    if pool is None:
        return None
    sets.append("updated_at=now()")
    params += [platform, handle]
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"UPDATE market_sources SET {', '.join(sets)} WHERE platform=%s AND handle=%s RETURNING *",
                params)
            row = cur.fetchone()
        conn.commit()
    return _row_to_json(row) if row else None


def merge_social_post_mentions(database_url, source_handle, post_id, extra_direct_mentions):
    """画像解析結果から新たに判明した銘柄関連付けを、既存direct_mentions_jsonへ重複無く
    追記する（画像解析待ちキュー指示書：解析結果を関連銘柄へ反映）。"""
    if not extra_direct_mentions:
        return None
    pool = _get_pool(database_url)
    if pool is None:
        return None
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT direct_mentions_json FROM social_market_posts WHERE source_handle=%s AND post_id=%s",
                        [source_handle, post_id])
            row = cur.fetchone()
            if not row:
                return None
            merged = sorted(set(row["direct_mentions_json"] or []) | set(extra_direct_mentions))
            cur.execute(
                "UPDATE social_market_posts SET direct_mentions_json=%s::jsonb, updated_at=now() "
                "WHERE source_handle=%s AND post_id=%s RETURNING *",
                [json.dumps(merged, ensure_ascii=False), source_handle, post_id])
            updated = cur.fetchone()
        conn.commit()
    return _row_to_json(updated) if updated else None


def set_post_intelligence_cluster(database_url, source_handle, post_id, cluster_id):
    """Market Intelligence Phase6新規（指示書16・19番）：cross-source entity matchingで
    同一クラスタに属すると判定した投稿へintelligence_cluster_idを設定する。"""
    pool = _get_pool(database_url)
    if pool is None:
        return None
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "UPDATE social_market_posts SET intelligence_cluster_id=%s, updated_at=now() "
                "WHERE source_handle=%s AND post_id=%s RETURNING *",
                [cluster_id, source_handle, post_id])
            row = cur.fetchone()
        conn.commit()
    return _row_to_json(row) if row else None


def list_recent_social_posts_all_sources(database_url, since_iso, limit=200):
    """Market Intelligence Phase6新規（指示書16・17番）：クラスタリング・コンセンサス計算用に
    全source横断で直近投稿を取得する。"""
    pool = _get_pool(database_url)
    if pool is None:
        return []
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM social_market_posts WHERE posted_at >= %s ORDER BY posted_at DESC LIMIT %s",
                [since_iso, limit])
            rows = cur.fetchall()
    return [_row_to_json(r) for r in rows]


def list_recent_social_posts(database_url, source_handle=None, since_iso=None, min_importance=None, limit=50,
                              image_analysis_status=None):
    """UI表示・にこそくカード向け。新しい順。min_importanceはLOW未満を除外する簡易フィルタ
    ではなく、指定レベル以上のみ返す（LOW<MEDIUM<HIGH<CRITICAL）。image_analysis_statusは
    Phase2追加：NONE/PENDING/ANALYZED/SKIPPED/FAILEDのいずれかで完全一致絞り込み
    （画像解析待ちキューのサマリー・並び替え計算に使う）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return []
    rank = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}
    where, params = [], []
    if source_handle:
        where.append("source_handle=%s")
        params.append(source_handle)
    if since_iso:
        where.append("posted_at >= %s")
        params.append(since_iso)
    if image_analysis_status:
        where.append("image_analysis_status=%s")
        params.append(image_analysis_status)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    params.append(limit if not min_importance else max(limit * 3, 100))  # min_importanceはPython側で絞るため多めに取る
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"SELECT * FROM social_market_posts {clause} ORDER BY posted_at DESC NULLS LAST LIMIT %s",
                params)
            rows = [_row_to_json(r) for r in cur.fetchall()]
    if min_importance:
        min_rank = rank.get(min_importance, 0)
        rows = [r for r in rows if rank.get(r.get("importance"), -1) >= min_rank]
    return rows[:limit]


def list_social_signals(database_url, since_iso, min_importance="MEDIUM", limit=20):
    """recent_social_market_signals生成用（指示書9番）。since_iso以降・min_importance以上の
    投稿を重要度→新しさの順で返す。"""
    return list_recent_social_posts(database_url, since_iso=since_iso, min_importance=min_importance, limit=limit)


# ============================================================
# にこそくX連携 Phase3（2026-09-10新規）：投稿の市場的中率・先行性・テーマ別信頼度を
# 定量評価するsocial_signal_evaluationsテーブル。集計・スコア計算のロジック自体はserver.py側
# （純粋関数、テスト容易性のため）に置き、ここではDB永続化とシンプルな取得・保存のみを担う
# （指示書21番「売買ロジックには直接繋げない」——このテーブルはsource reliability contextの
# 元データであり、entry_score等には一切関与しない）。
# ============================================================
_SCHEMA_SOCIAL_SIGNAL_EVALUATIONS_SQL = """
CREATE TABLE IF NOT EXISTS social_signal_evaluations (
    id                  SERIAL PRIMARY KEY,
    source_handle       TEXT NOT NULL,
    post_id             TEXT NOT NULL,
    signal_type         TEXT NOT NULL,
    signal_direction    TEXT NOT NULL,   -- BULLISH|BEARISH|MIXED|NEUTRAL
    target_type         TEXT NOT NULL,   -- MARKET|SECTOR|STOCK|EVENT
    target_key          TEXT NOT NULL,
    evaluation_window    TEXT NOT NULL,   -- 30M|1H|MARKET_CLOSE|NEXT_OPEN|NEXT_CLOSE
    baseline_at          TIMESTAMPTZ,
    due_at               TIMESTAMPTZ,     -- schedulerが「評価してよい時刻」を判定する追加列
    evaluated_at         TIMESTAMPTZ,
    baseline_value        NUMERIC,
    result_value          NUMERIC,
    change_value           NUMERIC,        -- %（baseline比）
    confirmed              BOOLEAN,          -- NEUTRAL/MIXEDや未評価の間はNULL
    contradicted            BOOLEAN NOT NULL DEFAULT false,
    confirmation_score       NUMERIC,        -- 0〜100
    evaluation_status         TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING|EVALUATED|NO_DATA
    notes                     TEXT,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_handle, post_id, signal_type, target_type, target_key, evaluation_window)
);
CREATE INDEX IF NOT EXISTS idx_social_eval_due ON social_signal_evaluations(evaluation_status, due_at);
CREATE INDEX IF NOT EXISTS idx_social_eval_handle ON social_signal_evaluations(source_handle, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_social_eval_post ON social_signal_evaluations(source_handle, post_id);
"""

# にこそくX連携 Phase4（2026-09-10新規）：baseline/resultをsignal生成時点・評価時点それぞれの
# 実スナップショットから取得するよう精度改善する（指示書1・2・3・4番）。既存Phase3列は
# 無変更（変更後も後方互換、指示書19番「既存Phase3互換性」）。
_MIGRATE_SOCIAL_SIGNAL_EVALUATIONS_V2_SQL = """
ALTER TABLE social_signal_evaluations ADD COLUMN IF NOT EXISTS baseline_source TEXT;
ALTER TABLE social_signal_evaluations ADD COLUMN IF NOT EXISTS baseline_status TEXT;
ALTER TABLE social_signal_evaluations ADD COLUMN IF NOT EXISTS baseline_detail_json JSONB;
ALTER TABLE social_signal_evaluations ADD COLUMN IF NOT EXISTS result_at TIMESTAMPTZ;
ALTER TABLE social_signal_evaluations ADD COLUMN IF NOT EXISTS evaluation_quality TEXT;
"""

# にこそくX連携 Phase4（指示書9番）：economic_events系の投稿はBULLISH/BEARISH方向性評価
# （social_signal_evaluations）とは分離し、専用テーブルで「イベント情報の一致率・先行時間」
# だけを評価する（指示書7番「投稿単独では既存の売買スコアへ一切加点しない」方針を踏襲、
# source reliability contextの一部）。
_SCHEMA_SOCIAL_EVENT_EVALUATIONS_SQL = """
CREATE TABLE IF NOT EXISTS social_event_evaluations (
    id                  SERIAL PRIMARY KEY,
    source_handle       TEXT NOT NULL,
    post_id             TEXT NOT NULL,
    event_name          TEXT NOT NULL,
    event_type          TEXT,
    event_start_at      TIMESTAMPTZ,
    post_created_at     TIMESTAMPTZ,
    lead_time_minutes   NUMERIC,
    match_status        TEXT,   -- EXACT_MATCH|DATE_MATCH|PARTIAL_MATCH|NO_MATCH
    timeliness          TEXT,   -- EARLY|GOOD|SHORT_NOTICE|LAST_MINUTE
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_handle, post_id, event_name, event_start_at)
);
CREATE INDEX IF NOT EXISTS idx_social_event_eval_handle ON social_event_evaluations(source_handle, created_at DESC);
"""


def create_social_signal_evaluations(database_url, rows):
    """指示書1・9番：評価対象の候補行を一括INSERTする（PENDING状態、まだ評価しない）。
    UNIQUE制約によりON CONFLICT DO NOTHINGで重複を防ぐ（指示書16番「同一評価を再計算し
    続けない」・テスト9番「重複評価防止」）。戻り値：実際に新規作成された行数。
    Phase4（指示書1番）：baseline_source/baseline_status/baseline_detail_json/
    evaluation_quality（baseline取得時点の品質、resultとcombineされる前の初期値）を追加。
    baseline_detail_jsonはSECTOR評価用の構成銘柄スナップショット（[{"code","price"},...]）。"""
    if not rows:
        return 0
    pool = _get_pool(database_url)
    if pool is None:
        return 0
    plain_cols = ["source_handle", "post_id", "signal_type", "signal_direction", "target_type", "target_key",
                  "evaluation_window", "baseline_at", "due_at", "baseline_value", "baseline_source",
                  "baseline_status", "evaluation_status", "evaluation_quality", "notes",
                  # Phase5（指示書1・3・4・5・6・7番）：追加専用列。
                  "signal_kind", "signal_confidence", "author_certainty", "observed_at_post",
                  "signal_group_id", "group_role", "relative_return_pct", "breadth", "decision_relevance_score"]
    json_cols = ["baseline_detail_json", "market_state_json", "market_regime_json"]
    cols = plain_cols + json_cols
    inserted = 0
    with pool.connection() as conn:
        with conn.cursor() as cur:
            for r in rows:
                values = [r.get(c) for c in plain_cols]
                values += [json.dumps(r.get(c), ensure_ascii=False) if r.get(c) is not None else None for c in json_cols]
                placeholders = ", ".join(["%s"] * len(plain_cols) + ["%s::jsonb"] * len(json_cols))
                cur.execute(
                    f"INSERT INTO social_signal_evaluations ({', '.join(cols)}) "
                    f"VALUES ({placeholders}) "
                    f"ON CONFLICT (source_handle, post_id, signal_type, target_type, target_key, evaluation_window) "
                    f"DO NOTHING",
                    values)
                inserted += cur.rowcount
        conn.commit()
    return inserted


def list_due_social_signal_evaluations(database_url, now_iso, limit=50):
    """指示書16番：scheduler向け。PENDING状態でdue_atが到来した評価だけを返す。"""
    pool = _get_pool(database_url)
    if pool is None:
        return []
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM social_signal_evaluations "
                "WHERE evaluation_status='PENDING' AND due_at IS NOT NULL AND due_at <= %s "
                "ORDER BY due_at ASC LIMIT %s",
                [now_iso, limit])
            rows = cur.fetchall()
    return [_row_to_json(r) for r in rows]


def save_social_signal_evaluation_result(database_url, evaluation_id, result_value=None, change_value=None,
                                          confirmed=None, contradicted=False, confirmation_score=None,
                                          evaluation_status="EVALUATED", notes=None, evaluation_quality=None,
                                          result_at=None, relative_return_pct=None, breadth=None,
                                          confirmed_v2=None, confirmation_score_v2=None):
    """1件分の評価結果を保存する（指示書3・4・7・8・18・19番）。evaluation_status="NO_DATA"の
    場合はresult_value等はNULLのまま記録し、的中率の分母から除外する（指示書18番、集計側
    （server.py）がevaluation_status=='EVALUATED'のみを対象にすることで担保する）。
    evaluation_qualityはbaseline取得時点の品質とresult取得時点の品質を合わせた最終値
    （呼び出し側で決定済みのものを渡す、指示書4番）。Phase5（指示書10・11・12番）：
    relative_return_pct/breadth/confirmed_v2/confirmation_score_v2を追加——v1列
    （confirmed/confirmation_score等）はそのまま残す（指示書12番「既存v1は残す」）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return None
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "UPDATE social_signal_evaluations SET result_value=%s, change_value=%s, confirmed=%s, "
                "contradicted=%s, confirmation_score=%s, evaluation_status=%s, notes=%s, "
                "evaluation_quality=COALESCE(%s, evaluation_quality), result_at=%s, "
                "relative_return_pct=%s, breadth=%s, confirmed_v2=%s, confirmation_score_v2=%s, "
                "evaluated_at=now() "
                "WHERE id=%s RETURNING *",
                [result_value, change_value, confirmed, contradicted, confirmation_score, evaluation_status,
                 notes, evaluation_quality, result_at, relative_return_pct, breadth, confirmed_v2,
                 confirmation_score_v2, evaluation_id])
            row = cur.fetchone()
        conn.commit()
    return _row_to_json(row) if row else None


def list_social_signal_evaluations_for_post(database_url, source_handle, post_id):
    """指示書15・22番：投稿単位の評価一覧（GET /api/social-posts/:post_id/evaluations）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return []
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM social_signal_evaluations WHERE source_handle=%s AND post_id=%s "
                "ORDER BY evaluation_window",
                [source_handle, post_id])
            rows = cur.fetchall()
    return [_row_to_json(r) for r in rows]


def list_social_signal_evaluations_since(database_url, source_handle, since_iso, limit=500):
    """指示書9・10・20番：集計用の生データ取得。集計ロジック自体はPython側（server.py、
    テスト容易性のため）が担う——ここは単純な期間絞り込みの取得のみ。"""
    pool = _get_pool(database_url)
    if pool is None:
        return []
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM social_signal_evaluations WHERE source_handle=%s AND created_at >= %s "
                "ORDER BY created_at DESC LIMIT %s",
                [source_handle, since_iso, limit])
            rows = cur.fetchall()
    return [_row_to_json(r) for r in rows]


def list_analyzed_social_posts_without_evaluations(database_url, source_handle, limit=50):
    """指示書17番：バックフィル用。ANALYZED済みだが評価行が1件も無い投稿を返す（自動で
    大量処理しないよう、呼び出し側でlimitを必須にする設計。ここは単なる候補取得）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return []
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT p.* FROM social_market_posts p "
                "WHERE p.source_handle=%s AND p.image_analysis_status='ANALYZED' "
                "AND NOT EXISTS (SELECT 1 FROM social_signal_evaluations e "
                "WHERE e.source_handle=p.source_handle AND e.post_id=p.post_id) "
                "ORDER BY p.posted_at DESC LIMIT %s",
                [source_handle, limit])
            rows = cur.fetchall()
    return [_row_to_json(r) for r in rows]


def list_analyzed_social_posts(database_url, source_handle, limit=50):
    """Phase4（指示書14番）：recompute=true専用。既存評価の有無を問わずANALYZED投稿を返す
    （list_analyzed_social_posts_without_evaluationsとは異なり除外条件が無い）。勝手な
    一括再計算を避けるため、呼び出し側（backfill、recompute=true時のみ）以外からは使わない
    想定。"""
    pool = _get_pool(database_url)
    if pool is None:
        return []
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM social_market_posts WHERE source_handle=%s AND image_analysis_status='ANALYZED' "
                "ORDER BY posted_at DESC LIMIT %s",
                [source_handle, limit])
            rows = cur.fetchall()
    return [_row_to_json(r) for r in rows]


def delete_social_signal_evaluations_for_post(database_url, source_handle, post_id):
    """Phase4（指示書14番）：recompute=true時のみ呼ぶ、既存評価行の削除。デフォルト経路
    （recompute省略）からは一切呼ばれない——既存Phase3評価を勝手に上書きしないため
    （指示書14番「勝手に既存評価を上書きしない」）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return 0
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM social_signal_evaluations WHERE source_handle=%s AND post_id=%s",
                        [source_handle, post_id])
            n = cur.rowcount
        conn.commit()
    return n


def count_social_signal_evaluations(database_url, source_handle, evaluation_status=None):
    """Phase4（指示書15番）：diagnostics向けの件数カウント（pending_evaluations/
    no_data_evaluations）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return 0
    where = ["source_handle=%s"]
    params = [source_handle]
    if evaluation_status:
        where.append("evaluation_status=%s")
        params.append(evaluation_status)
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM social_signal_evaluations WHERE {' AND '.join(where)}", params)
            n = cur.fetchone()[0]
    return n


# ---- social_event_evaluations（にこそくX連携 Phase4：event_accuracy/event_timeliness。
# 2026-09-10新規） ----

def create_social_event_evaluations(database_url, rows):
    """指示書9番：economic_events系の候補を一括INSERTする。UNIQUE制約
    （source_handle,post_id,event_name,event_start_at）によりON CONFLICT DO NOTHINGで
    重複を防ぐ。戻り値：実際に新規作成された行数。"""
    if not rows:
        return 0
    pool = _get_pool(database_url)
    if pool is None:
        return 0
    cols = ["source_handle", "post_id", "event_name", "event_type", "event_start_at", "post_created_at",
            "lead_time_minutes", "match_status", "timeliness"]
    inserted = 0
    with pool.connection() as conn:
        with conn.cursor() as cur:
            for r in rows:
                values = [r.get(c) for c in cols]
                cur.execute(
                    f"INSERT INTO social_event_evaluations ({', '.join(cols)}) "
                    f"VALUES ({', '.join(['%s'] * len(cols))}) "
                    f"ON CONFLICT (source_handle, post_id, event_name, event_start_at) DO NOTHING",
                    values)
                inserted += cur.rowcount
        conn.commit()
    return inserted


def list_social_event_evaluations_since(database_url, source_handle, since_iso, limit=500):
    """指示書10番：performance集計（event_performance）用の生データ取得。"""
    pool = _get_pool(database_url)
    if pool is None:
        return []
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM social_event_evaluations WHERE source_handle=%s AND created_at >= %s "
                "ORDER BY created_at DESC LIMIT %s",
                [source_handle, since_iso, limit])
            rows = cur.fetchall()
    return [_row_to_json(r) for r in rows]


# ============================================================
# にこそくX連携 Phase5（2026-09-11新規）：実運用校正・シグナル品質改善。既存Phase3/4の列は
# 一切変更しない（追加専用のALTER、指示書28番「Phase4互換性」）。集計・分類ロジック自体は
# server.py側（純粋関数、テスト容易性のため）に置く方針を継続する。
# ============================================================
_MIGRATE_SOCIAL_SIGNAL_EVALUATIONS_V3_SQL = """
ALTER TABLE social_signal_evaluations ADD COLUMN IF NOT EXISTS signal_kind TEXT;
ALTER TABLE social_signal_evaluations ADD COLUMN IF NOT EXISTS signal_confidence NUMERIC;
ALTER TABLE social_signal_evaluations ADD COLUMN IF NOT EXISTS author_certainty TEXT;
ALTER TABLE social_signal_evaluations ADD COLUMN IF NOT EXISTS observed_at_post BOOLEAN;
ALTER TABLE social_signal_evaluations ADD COLUMN IF NOT EXISTS signal_group_id TEXT;
ALTER TABLE social_signal_evaluations ADD COLUMN IF NOT EXISTS group_role TEXT;
ALTER TABLE social_signal_evaluations ADD COLUMN IF NOT EXISTS market_state_json JSONB;
ALTER TABLE social_signal_evaluations ADD COLUMN IF NOT EXISTS market_regime_json JSONB;
ALTER TABLE social_signal_evaluations ADD COLUMN IF NOT EXISTS relative_return_pct NUMERIC;
ALTER TABLE social_signal_evaluations ADD COLUMN IF NOT EXISTS breadth NUMERIC;
ALTER TABLE social_signal_evaluations ADD COLUMN IF NOT EXISTS confirmed_v2 BOOLEAN;
ALTER TABLE social_signal_evaluations ADD COLUMN IF NOT EXISTS confirmation_score_v2 NUMERIC;
ALTER TABLE social_signal_evaluations ADD COLUMN IF NOT EXISTS decision_relevance_score NUMERIC;
CREATE INDEX IF NOT EXISTS idx_social_eval_group ON social_signal_evaluations(source_handle, signal_group_id);
"""

# 指示書19・20番：SOCIAL_SIGNAL_ALERT生成ログ（cooldown判定・diagnostics集計用）。売買指示では
# なく、あくまで「注目に値する投稿があった」という記録。
_SCHEMA_SOCIAL_SIGNAL_ALERTS_SQL = """
CREATE TABLE IF NOT EXISTS social_signal_alerts (
    id                  SERIAL PRIMARY KEY,
    source_handle       TEXT NOT NULL,
    post_id             TEXT NOT NULL,
    signal_group_id     TEXT,
    alert_type          TEXT NOT NULL DEFAULT 'SOCIAL_SIGNAL_ALERT',
    payload_json        JSONB,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_social_alerts_group ON social_signal_alerts(source_handle, signal_group_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_social_alerts_handle ON social_signal_alerts(source_handle, created_at DESC);
"""


def list_recent_signal_group_candidates(database_url, source_handle, target_type, target_key, signal_kind, since_iso):
    """指示書5・6番：signal deduplication/follow-up判定用。同一(target_type,target_key,
    signal_kind)についてsince_iso以降に生成された評価行を新しい順で返す（signal_group_id・
    group_role・signal_directionの解決に使う。1件のpostが複数windowを持つため
    DISTINCT ON (post_id)で投稿単位にまとめる）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return []
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT DISTINCT ON (post_id) * FROM social_signal_evaluations "
                "WHERE source_handle=%s AND target_type=%s AND target_key=%s AND signal_kind=%s "
                "AND created_at >= %s ORDER BY post_id, created_at DESC",
                [source_handle, target_type, target_key, signal_kind, since_iso])
            rows = cur.fetchall()
    rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return [_row_to_json(r) for r in rows]


def create_social_signal_alert(database_url, row):
    """指示書19番：SOCIAL_SIGNAL_ALERTを1件記録する。"""
    pool = _get_pool(database_url)
    if pool is None:
        return None
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "INSERT INTO social_signal_alerts (source_handle, post_id, signal_group_id, alert_type, payload_json) "
                "VALUES (%s,%s,%s,%s,%s::jsonb) RETURNING *",
                [row.get("source_handle"), row.get("post_id"), row.get("signal_group_id"),
                 row.get("alert_type") or "SOCIAL_SIGNAL_ALERT",
                 json.dumps(row.get("payload") or {}, ensure_ascii=False)])
            saved = cur.fetchone()
        conn.commit()
    return _row_to_json(saved)


def list_recent_alerts_for_group(database_url, source_handle, signal_group_id, since_iso):
    """指示書20番：alert spam防止（cooldown判定）。指定グループへ直近since_iso以降に
    出したalertがあるかどうかを調べるために使う。"""
    pool = _get_pool(database_url)
    if pool is None:
        return []
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM social_signal_alerts WHERE source_handle=%s AND signal_group_id=%s "
                "AND created_at >= %s ORDER BY created_at DESC",
                [source_handle, signal_group_id, since_iso])
            rows = cur.fetchall()
    return [_row_to_json(r) for r in rows]


def list_social_signal_alerts_since(database_url, source_handle, since_iso, limit=50):
    """指示書22番：UI表示・診断用の直近alert一覧。"""
    pool = _get_pool(database_url)
    if pool is None:
        return []
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM social_signal_alerts WHERE source_handle=%s AND created_at >= %s "
                "ORDER BY created_at DESC LIMIT %s",
                [source_handle, since_iso, limit])
            rows = cur.fetchall()
    return [_row_to_json(r) for r in rows]


def count_social_signal_alerts_since(database_url, source_handle, since_iso):
    """指示書24番：diagnostics向け（alerts_generated_today）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return 0
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM social_signal_alerts WHERE source_handle=%s AND created_at >= %s",
                        [source_handle, since_iso])
            return cur.fetchone()[0]


def count_high_confidence_signals_since(database_url, source_handle, since_iso, min_confidence=0.75):
    """指示書24番：diagnostics向け（high_confidence_signals_today）。post単位で数える
    （同一postの複数window行を二重に数えない）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return 0
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(DISTINCT post_id) FROM social_signal_evaluations "
                "WHERE source_handle=%s AND created_at >= %s AND signal_confidence >= %s",
                [source_handle, since_iso, min_confidence])
            return cur.fetchone()[0]


def count_duplicate_signal_groups_since(database_url, source_handle, since_iso):
    """指示書24番：diagnostics向け（duplicate_signal_groups）。同一signal_group_idが複数
    post_idにまたがっている（＝CONFIRMATION等でグルーピングされた）グループ数。"""
    pool = _get_pool(database_url)
    if pool is None:
        return 0
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM ("
                "  SELECT signal_group_id FROM social_signal_evaluations "
                "  WHERE source_handle=%s AND created_at >= %s AND signal_group_id IS NOT NULL "
                "  GROUP BY signal_group_id HAVING COUNT(DISTINCT post_id) > 1"
                ") sub",
                [source_handle, since_iso])
            return cur.fetchone()[0]


# ============================================================
# Market Intelligence Phase7（2026-09-13新規）：Underlying Event Engine。social posts/
# news catalysts/corporate disclosures/economic events/prediction market shiftsを、同一の
# 出来事であれば1つのunderlying_eventへ統合する（指示書1・3番）。既存Phase1〜6のテーブル・
# ロジックは一切変更しない（指示書26番「Phase6後方互換」・intelligence_cluster_idは廃止せず
# 併存）。売買ロジックへの直接加点はまだ行わない（指示書冒頭）。
# ============================================================

_SCHEMA_UNDERLYING_EVENTS_SQL = """
CREATE TABLE IF NOT EXISTS underlying_events (
    id                      SERIAL PRIMARY KEY,
    event_key               TEXT,                     -- 決定的key（例: JP:7203:BUYBACK:2026-09-11）。作れない場合はNULL
    event_type              TEXT NOT NULL,
    title                   TEXT,
    normalized_title        TEXT,
    ticker                  TEXT,
    company_name            TEXT,
    sector                  TEXT,
    country                 TEXT DEFAULT 'JP',
    event_at                TIMESTAMPTZ,
    first_seen_at           TIMESTAMPTZ NOT NULL DEFAULT now(),  -- 一次情報昇格でも変更しない（指示書4番）
    last_seen_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    primary_source_type     TEXT,                     -- TDNET|COMPANY_IR|GOVERNMENT|CENTRAL_BANK_OFFICIAL|
                                                        -- EXCHANGE_OFFICIAL|NEWS|SOCIAL
    primary_source_url      TEXT,
    confidence              NUMERIC,                  -- 0.0〜1.0（内部計算用の生の確度）
    confidence_level        TEXT,                     -- OFFICIAL_CONFIRMED|MULTI_SOURCE_CONFIRMED|
                                                        -- SINGLE_RELIABLE_SOURCE|SOCIAL_ONLY|UNVERIFIED
    importance              TEXT DEFAULT 'MEDIUM',
    status                  TEXT NOT NULL DEFAULT 'ACTIVE',  -- ACTIVE|CONFIRMED|UPDATED|RESOLVED|INVALIDATED
    direct_tickers_json     JSONB,
    related_tickers_json    JSONB,
    related_sectors_json    JSONB,
    numerical_fingerprint_json JSONB,   -- 金額・%・株数等（指示書8番、重複判定・material update検出用）
    raw_source_count        INTEGER NOT NULL DEFAULT 0,
    independent_source_count INTEGER NOT NULL DEFAULT 0,
    primary_source_confirmed BOOLEAN NOT NULL DEFAULT false,
    impact_score            NUMERIC,
    intelligence_cluster_id TEXT,        -- Phase6のcluster機構を廃止せず併存（指示書27番）
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_underlying_events_key ON underlying_events(event_key);
CREATE INDEX IF NOT EXISTS idx_underlying_events_ticker ON underlying_events(ticker);
CREATE INDEX IF NOT EXISTS idx_underlying_events_status ON underlying_events(status, last_seen_at DESC);

CREATE TABLE IF NOT EXISTS underlying_event_evidence (
    id                  SERIAL PRIMARY KEY,
    event_id            INTEGER NOT NULL REFERENCES underlying_events(id) ON DELETE CASCADE,
    source_kind         TEXT NOT NULL,   -- SOCIAL|NEWS|IR|TDNET|EVENT|PREDICTION
    source_name         TEXT NOT NULL,   -- 例: aryarya, TDnet, Reuters
    source_record_id    TEXT NOT NULL,
    source_url          TEXT,
    posted_at           TIMESTAMPTZ,
    is_primary          BOOLEAN NOT NULL DEFAULT false,
    is_independent      BOOLEAN NOT NULL DEFAULT true,
    upstream_source      TEXT,           -- 転載元（例: aryarya/kgbukabuが共にTDnet由来ならTDnet）
    dependency_group      TEXT,          -- 同一upstreamのevidenceをまとめる識別子
    raw_text_summary      TEXT,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (event_id, source_kind, source_record_id)
);
CREATE INDEX IF NOT EXISTS idx_event_evidence_event ON underlying_event_evidence(event_id, posted_at);

CREATE TABLE IF NOT EXISTS underlying_event_alerts (
    id          SERIAL PRIMARY KEY,
    event_id    INTEGER NOT NULL REFERENCES underlying_events(id) ON DELETE CASCADE,
    alert_type  TEXT NOT NULL,   -- NEW_EVENT|MATERIAL_UPDATE|CONFIDENCE_UPGRADE|IMPACT_UPGRADE
    payload_json JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_event_alerts_event ON underlying_event_alerts(event_id, created_at DESC);
"""


def create_underlying_event(database_url, data):
    """指示書1番：新規underlying_eventを1件作成する。戻り値：作成行（dict）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return None
    plain_cols = ["event_key", "event_type", "title", "normalized_title", "ticker", "company_name",
                  "sector", "country", "event_at", "primary_source_type", "primary_source_url",
                  "confidence", "confidence_level", "importance", "status", "raw_source_count",
                  "independent_source_count", "primary_source_confirmed", "impact_score",
                  "intelligence_cluster_id",
                  # Phase8（指示書2・3・4・6・12・15番）：追加専用列。
                  "event_timing", "market_relevant_at", "system_observed_at", "opening_gap_pct",
                  "material_magnitude", "effectiveness_score", "reaction_pattern", "resolution_date",
                  "extended_move", "backfill_source"]
    json_cols = ["direct_tickers_json", "related_tickers_json", "related_sectors_json", "numerical_fingerprint_json",
                 "event_type_details_json"]
    cols = plain_cols + json_cols
    values = [data.get(c) for c in plain_cols]
    values += [json.dumps(data.get(c), ensure_ascii=False) if data.get(c) is not None else None for c in json_cols]
    placeholders = ", ".join(["%s"] * len(plain_cols) + ["%s::jsonb"] * len(json_cols))
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"INSERT INTO underlying_events ({', '.join(cols)}) VALUES ({placeholders}) RETURNING *",
                values)
            row = cur.fetchone()
        conn.commit()
    return _row_to_json(row) if row else None


def get_underlying_event(database_url, event_id):
    pool = _get_pool(database_url)
    if pool is None:
        return None
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM underlying_events WHERE id=%s", [event_id])
            row = cur.fetchone()
    return _row_to_json(row) if row else None


def list_underlying_event_candidates(database_url, event_type=None, ticker=None, since_iso=None, limit=50):
    """指示書5番：matcher向けの候補取得。event_type・ticker・期間で絞り込む
    （指示書31番「tickerだけで統合しない」を担保するため、呼び出し側は必ずevent_typeも
    条件に含める設計を推奨するが、この関数自体はticker単独指定も許容する——安全弁は
    matcher側のロジックに置く）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return []
    where, params = ["status != 'INVALIDATED'"], []
    if event_type:
        where.append("event_type=%s")
        params.append(event_type)
    if ticker:
        where.append("ticker=%s")
        params.append(ticker)
    if since_iso:
        where.append("last_seen_at >= %s")
        params.append(since_iso)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    params.append(limit)
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(f"SELECT * FROM underlying_events {clause} ORDER BY last_seen_at DESC LIMIT %s", params)
            rows = cur.fetchall()
    return [_row_to_json(r) for r in rows]


def list_active_underlying_events(database_url, since_iso=None, limit=50):
    """指示書23・29・30番：UI「重要」タブ・GET /api/market-intelligence/events向け。"""
    pool = _get_pool(database_url)
    if pool is None:
        return []
    where, params = ["status != 'INVALIDATED'"], []
    if since_iso:
        where.append("last_seen_at >= %s")
        params.append(since_iso)
    clause = f"WHERE {' AND '.join(where)}"
    params.append(limit)
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(f"SELECT * FROM underlying_events {clause} ORDER BY last_seen_at DESC LIMIT %s", params)
            rows = cur.fetchall()
    return [_row_to_json(r) for r in rows]


def update_underlying_event(database_url, event_id, fields):
    """汎用UPDATE。fieldsのキーはunderlying_eventsの列名（JSON列はdict/listのまま渡せる）。
    first_seen_atは呼び出し側が絶対に渡さない設計にする（指示書4番、一次情報昇格でも
    変更しない）。"""
    if not fields:
        return get_underlying_event(database_url, event_id)
    pool = _get_pool(database_url)
    if pool is None:
        return None
    json_cols = {"direct_tickers_json", "related_tickers_json", "related_sectors_json",
                 "numerical_fingerprint_json", "event_type_details_json"}
    sets, params = [], []
    for k, v in fields.items():
        if k == "first_seen_at":
            continue  # 指示書4番のガード
        if k in json_cols:
            sets.append(f"{k}=%s::jsonb")
            params.append(json.dumps(v, ensure_ascii=False) if v is not None else None)
        elif v == "NOW()":
            sets.append(f"{k}=now()")
        else:
            sets.append(f"{k}=%s")
            params.append(v)
    sets.append("updated_at=now()")
    params.append(event_id)
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(f"UPDATE underlying_events SET {', '.join(sets)} WHERE id=%s RETURNING *", params)
            row = cur.fetchone()
        conn.commit()
    return _row_to_json(row) if row else None


def add_underlying_event_evidence(database_url, evidence):
    """指示書3番：evidenceを1件追加する。UNIQUE(event_id,source_kind,source_record_id)により
    同一投稿を二重にevidence化しない（指示書「raw_source_count/independent_source_countの
    正確化」の前提）。戻り値：新規追加ならdict、既存（重複）ならNone。"""
    pool = _get_pool(database_url)
    if pool is None:
        return None
    cols = ["event_id", "source_kind", "source_name", "source_record_id", "source_url", "posted_at",
            "is_primary", "is_independent", "upstream_source", "dependency_group", "raw_text_summary"]
    values = [evidence.get(c) for c in cols]
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"INSERT INTO underlying_event_evidence ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) "
                f"ON CONFLICT (event_id, source_kind, source_record_id) DO NOTHING RETURNING *",
                values)
            row = cur.fetchone()
        conn.commit()
    return _row_to_json(row) if row else None


def list_underlying_event_evidence(database_url, event_id):
    """指示書25番：event詳細のtimeline表示用（posted_at昇順＝時系列）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return []
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM underlying_event_evidence WHERE event_id=%s ORDER BY posted_at ASC NULLS LAST",
                [event_id])
            rows = cur.fetchall()
    return [_row_to_json(r) for r in rows]


def create_underlying_event_alert(database_url, event_id, alert_type, payload=None):
    """指示書20・21番：NEW_EVENT/MATERIAL_UPDATE/CONFIDENCE_UPGRADE/IMPACT_UPGRADEのみ記録する
    （呼び出し側=server.pyが「単なる転載」では呼ばない設計、指示書20番「多重発火防止」）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return None
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "INSERT INTO underlying_event_alerts (event_id, alert_type, payload_json) "
                "VALUES (%s,%s,%s::jsonb) RETURNING *",
                [event_id, alert_type, json.dumps(payload or {}, ensure_ascii=False)])
            row = cur.fetchone()
        conn.commit()
    return _row_to_json(row) if row else None


def count_underlying_event_alerts_since(database_url, since_iso, alert_type=None):
    """指示書30番：diagnostics向け（duplicate_alerts_suppressedは呼び出し側でsuppress回数を
    別途カウントするため、ここはalert_type別の「実際に出したalert数」を返す）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return 0
    where, params = ["created_at >= %s"], [since_iso]
    if alert_type:
        where.append("alert_type=%s")
        params.append(alert_type)
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM underlying_event_alerts WHERE {' AND '.join(where)}", params)
            return cur.fetchone()[0]


def count_underlying_events_since(database_url, since_iso, status=None):
    """指示書30番：diagnostics向け（active_underlying_events/events_created_today）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return 0
    where, params = ["created_at >= %s"], [since_iso]
    if status:
        where.append("status=%s")
        params.append(status)
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM underlying_events WHERE {' AND '.join(where)}", params)
            return cur.fetchone()[0]


def count_active_underlying_events(database_url):
    pool = _get_pool(database_url)
    if pool is None:
        return 0
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM underlying_events WHERE status NOT IN ('RESOLVED','INVALIDATED')")
            return cur.fetchone()[0]


def count_underlying_event_evidence_since(database_url, since_iso):
    """指示書30番：diagnostics向け（merged_evidence_today）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return 0
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM underlying_event_evidence WHERE created_at >= %s", [since_iso])
            return cur.fetchone()[0]


# ============================================================
# Market Intelligence Phase8（2026-09-14新規）：Event Reaction Engine / Cross-Source
# Backfill / Resolution Tracking。underlying_eventsを中心に「材料の強さ（impact）」と
# 「実際の市場反応（effectiveness）」を分離して記録する（指示書12・44番）。既存Phase1〜7の
# テーブル・ロジックは一切変更しない。売買スコアへの直接加点は今回も行わない。
# ============================================================

_MIGRATE_UNDERLYING_EVENTS_V2_SQL = """
ALTER TABLE underlying_events ADD COLUMN IF NOT EXISTS event_timing TEXT;
ALTER TABLE underlying_events ADD COLUMN IF NOT EXISTS market_relevant_at TIMESTAMPTZ;
ALTER TABLE underlying_events ADD COLUMN IF NOT EXISTS system_observed_at TIMESTAMPTZ;
ALTER TABLE underlying_events ADD COLUMN IF NOT EXISTS opening_gap_pct NUMERIC;
ALTER TABLE underlying_events ADD COLUMN IF NOT EXISTS material_magnitude NUMERIC;
ALTER TABLE underlying_events ADD COLUMN IF NOT EXISTS event_type_details_json JSONB;
ALTER TABLE underlying_events ADD COLUMN IF NOT EXISTS effectiveness_score NUMERIC;
ALTER TABLE underlying_events ADD COLUMN IF NOT EXISTS reaction_pattern TEXT;
ALTER TABLE underlying_events ADD COLUMN IF NOT EXISTS resolution_date DATE;
ALTER TABLE underlying_events ADD COLUMN IF NOT EXISTS extended_move BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE underlying_events ADD COLUMN IF NOT EXISTS backfill_source TEXT;
"""

_SCHEMA_EVENT_MARKET_REACTIONS_SQL = """
CREATE TABLE IF NOT EXISTS event_market_reactions (
    id                          SERIAL PRIMARY KEY,
    event_id                    INTEGER NOT NULL REFERENCES underlying_events(id) ON DELETE CASCADE,
    ticker                      TEXT NOT NULL,   -- 銘柄ticker、または市場全体反応なら'MARKET'、セクターなら'SECTOR:BANK'等
    target_type                 TEXT NOT NULL DEFAULT 'STOCK',  -- STOCK|SECTOR|MARKET
    relevance                   TEXT NOT NULL DEFAULT 'DIRECT', -- DIRECT|RELATED（指示書7・8番、direct/relatedは混ぜない）
    reaction_window             TEXT NOT NULL,   -- 5M|30M|1H|CLOSE|NEXT_OPEN|NEXT_CLOSE
    baseline_at                 TIMESTAMPTZ,
    baseline_price               NUMERIC,
    due_at                       TIMESTAMPTZ,
    result_at                    TIMESTAMPTZ,
    result_price                  NUMERIC,
    stock_return_pct              NUMERIC,
    sector_return_pct              NUMERIC,
    market_return_pct              NUMERIC,
    sector_relative_return_pct      NUMERIC,
    market_relative_return_pct      NUMERIC,
    volume_ratio                    NUMERIC,
    breadth                          NUMERIC,
    evaluation_quality               TEXT,     -- EXACT|NEAR_EXACT|ESTIMATED|NO_DATA（Phase4と同じ語彙を再利用）
    evaluation_status                TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING|EVALUATED|NO_DATA
    reaction_classification           TEXT,    -- STRONG_POSITIVE|POSITIVE|NEUTRAL|NEGATIVE|STRONG_NEGATIVE
    extended_move                     BOOLEAN NOT NULL DEFAULT false,
    notes                             TEXT,
    created_at                        TIMESTAMPTZ NOT NULL DEFAULT now(),
    evaluated_at                      TIMESTAMPTZ,
    UNIQUE (event_id, ticker, reaction_window)
);
CREATE INDEX IF NOT EXISTS idx_event_reactions_due ON event_market_reactions(evaluation_status, due_at);
CREATE INDEX IF NOT EXISTS idx_event_reactions_event ON event_market_reactions(event_id);

CREATE TABLE IF NOT EXISTS prediction_resolutions (
    id                          SERIAL PRIMARY KEY,
    event_id                    INTEGER NOT NULL REFERENCES underlying_events(id) ON DELETE CASCADE,
    topic                        TEXT,
    probability_at_first_seen     NUMERIC,
    peak_probability               NUMERIC,
    final_probability               NUMERIC,
    resolved_outcome                 BOOLEAN,
    resolved_at                       TIMESTAMPTZ,
    created_at                        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (event_id)
);
"""


def create_event_market_reactions(database_url, rows):
    """指示書1・27番：reaction候補行を一括INSERTする（PENDING状態）。UNIQUE
    (event_id,ticker,reaction_window)によりON CONFLICT DO NOTHINGで重複を防ぐ
    （scheduler再起動でも二重生成しない）。戻り値：新規作成行数。"""
    if not rows:
        return 0
    pool = _get_pool(database_url)
    if pool is None:
        return 0
    cols = ["event_id", "ticker", "target_type", "relevance", "reaction_window", "baseline_at",
            "baseline_price", "due_at", "evaluation_status", "notes"]
    inserted = 0
    with pool.connection() as conn:
        with conn.cursor() as cur:
            for r in rows:
                values = [r.get(c) for c in cols]
                cur.execute(
                    f"INSERT INTO event_market_reactions ({', '.join(cols)}) "
                    f"VALUES ({', '.join(['%s'] * len(cols))}) "
                    f"ON CONFLICT (event_id, ticker, reaction_window) DO NOTHING",
                    values)
                inserted += cur.rowcount
        conn.commit()
    return inserted


def list_due_event_market_reactions(database_url, now_iso, limit=50):
    """指示書28番：due_at到来分・PENDINGのみ返す（独立scheduler向け）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return []
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM event_market_reactions "
                "WHERE evaluation_status='PENDING' AND due_at IS NOT NULL AND due_at <= %s "
                "ORDER BY due_at ASC LIMIT %s",
                [now_iso, limit])
            rows = cur.fetchall()
    return [_row_to_json(r) for r in rows]


def save_event_market_reaction_result(database_url, reaction_id, **fields):
    """reaction結果を保存する。fieldsはevent_market_reactionsの列名（result_price等）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return None
    allowed = {"result_at", "result_price", "stock_return_pct", "sector_return_pct", "market_return_pct",
               "sector_relative_return_pct", "market_relative_return_pct", "volume_ratio", "breadth",
               "evaluation_quality", "evaluation_status", "reaction_classification", "extended_move", "notes"}
    sets, params = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        sets.append(f"{k}=%s")
        params.append(v)
    if not sets:
        return None
    sets.append("evaluated_at=now()")
    params.append(reaction_id)
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(f"UPDATE event_market_reactions SET {', '.join(sets)} WHERE id=%s RETURNING *", params)
            row = cur.fetchone()
        conn.commit()
    return _row_to_json(row) if row else None


def list_event_market_reactions_for_event(database_url, event_id):
    """指示書32・40番：event詳細のreaction一覧（UIカード展開・GET /events/:id/reactions）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return []
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM event_market_reactions WHERE event_id=%s ORDER BY reaction_window",
                [event_id])
            rows = cur.fetchall()
    return [_row_to_json(r) for r in rows]


def list_evaluated_event_market_reactions_since(database_url, since_iso, limit=1000):
    """指示書14・24番：event_type別集計（aggregate_event_type_performance）用の生データ取得。"""
    pool = _get_pool(database_url)
    if pool is None:
        return []
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT r.*, e.event_type, e.material_magnitude FROM event_market_reactions r "
                "JOIN underlying_events e ON e.id = r.event_id "
                "WHERE r.evaluation_status='EVALUATED' AND r.created_at >= %s "
                "ORDER BY r.created_at DESC LIMIT %s",
                [since_iso, limit])
            rows = cur.fetchall()
    return [_row_to_json(r) for r in rows]


def count_event_market_reactions(database_url, evaluation_status=None, since_iso=None):
    """指示書39番：diagnostics向け（pending_event_reactions/reactions_evaluated_today/
    reaction_no_data_count等）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return 0
    where, params = [], []
    if evaluation_status:
        where.append("evaluation_status=%s")
        params.append(evaluation_status)
    if since_iso:
        where.append("created_at >= %s" if evaluation_status != "EVALUATED" else "evaluated_at >= %s")
        params.append(since_iso)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM event_market_reactions {clause}", params)
            return cur.fetchone()[0]


def count_event_market_reactions_by_quality(database_url, evaluation_quality, since_iso):
    pool = _get_pool(database_url)
    if pool is None:
        return 0
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM event_market_reactions WHERE evaluation_quality=%s AND evaluated_at >= %s",
                [evaluation_quality, since_iso])
            return cur.fetchone()[0]


def upsert_prediction_resolution(database_url, event_id, fields):
    """指示書21・22番：prediction_resolutions（peak/final probability・resolved_outcome）。
    INSERT ON CONFLICT (event_id) DO UPDATEでpeak_probabilityは大きい方を保持する。"""
    pool = _get_pool(database_url)
    if pool is None:
        return None
    cols = ["event_id", "topic", "probability_at_first_seen", "peak_probability", "final_probability",
            "resolved_outcome", "resolved_at"]
    values = [event_id] + [fields.get(c) for c in cols[1:]]
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"INSERT INTO prediction_resolutions ({', '.join(cols)}) "
                f"VALUES ({', '.join(['%s'] * len(cols))}) "
                f"ON CONFLICT (event_id) DO UPDATE SET "
                f"topic=COALESCE(EXCLUDED.topic, prediction_resolutions.topic), "
                f"peak_probability=GREATEST(COALESCE(EXCLUDED.peak_probability,0), COALESCE(prediction_resolutions.peak_probability,0)), "
                f"final_probability=COALESCE(EXCLUDED.final_probability, prediction_resolutions.final_probability), "
                f"resolved_outcome=COALESCE(EXCLUDED.resolved_outcome, prediction_resolutions.resolved_outcome), "
                f"resolved_at=COALESCE(EXCLUDED.resolved_at, prediction_resolutions.resolved_at), "
                f"updated_at=now() RETURNING *",
                values)
            row = cur.fetchone()
        conn.commit()
    return _row_to_json(row) if row else None


def list_pending_prediction_resolutions(database_url, limit=50):
    """指示書39番：diagnostics向け（prediction_resolutions_pending）。resolved_outcomeが
    未確定のものを返す。"""
    pool = _get_pool(database_url)
    if pool is None:
        return []
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM prediction_resolutions WHERE resolved_outcome IS NULL ORDER BY updated_at DESC LIMIT %s",
                [limit])
            rows = cur.fetchall()
    return [_row_to_json(r) for r in rows]


def count_pending_prediction_resolutions(database_url):
    pool = _get_pool(database_url)
    if pool is None:
        return 0
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM prediction_resolutions WHERE resolved_outcome IS NULL")
            return cur.fetchone()[0]


def list_news_catalysts_for_backfill(database_url, user_id, since_date, limit=100):
    """指示書23番：news_catalysts backfill用。既存list_news_catalystsをそのまま使い回す
    （別実装を作らない）。"""
    return list_news_catalysts(database_url, user_id, from_date=since_date, limit=limit)


def list_market_events_for_backfill(database_url, user_id, since_date, limit=100):
    """指示書24番：market_events backfill用。既存list_market_eventsをそのまま使い回す。"""
    return list_market_events(database_url, user_id, from_date=since_date, limit=limit)


# ============================================================
# Market Intelligence Phase9（2026-09-11新規）：Event Quality Calibration /
# Decision Support Bridge。underlying_event＋event_market_reaction＋
# event_type_performanceを組み合わせ「材料は強いか→実際に反応しているか→
# 既に織り込まれていないか→今のポジションから見て入る価値があるか」を判定する
# 補助レイヤー。既存のBUY/WAIT/SELL・ENTRY TOP5のentry_score・thesis score・
# AUTO_RS・AUTO_SECTOR・損切り・ポジションサイジングは一切直接変更しない
# （指示書21・43番、常に「隣に並べるだけ」）。
# 履歴のあるテーブルとして設計する（UNIQUE制約は付けず評価の度にINSERTする）——
# 「09:35 STRONG_SUPPORT→10:00 AVOID_CHASE→10:45 SUPPORTIVE」のような遷移を後から
# 追える必要があるため（指示書28・29番）。最新1件はevaluated_at DESCで取得する。
# ============================================================

_SCHEMA_EVENT_DECISION_SUPPORT_SQL = """
CREATE TABLE IF NOT EXISTS event_decision_support (
    id                          SERIAL PRIMARY KEY,
    event_id                    INTEGER NOT NULL REFERENCES underlying_events(id) ON DELETE CASCADE,
    ticker                      TEXT NOT NULL,
    evaluated_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    available_data_at           TIMESTAMPTZ,   -- 指示書31番：no hindsight（事後データを混ぜていないことの記録）
    material_quality_score      NUMERIC,
    reaction_quality_score      NUMERIC,
    extension_score             NUMERIC,
    freshness_score             NUMERIC,
    historical_edge_score       NUMERIC,
    source_confidence_score     NUMERIC,
    decision_support_score      NUMERIC,
    decision_support_state      TEXT,     -- STRONG_SUPPORT|SUPPORTIVE|NEUTRAL|CAUTION|AVOID|AVOID_CHASE
    event_direction              TEXT,     -- POSITIVE|NEGATIVE|MIXED|NEUTRAL
    event_direction_confidence    NUMERIC,
    persistence_class             TEXT,     -- INTRADAY|SHORT_TERM|SWING|STRUCTURAL
    avoid_chase                   BOOLEAN NOT NULL DEFAULT false,
    pullback_candidate            BOOLEAN NOT NULL DEFAULT false,
    preferred_pullback_zone_json  JSONB,
    failed_reaction               BOOLEAN NOT NULL DEFAULT false,
    sell_the_news                 BOOLEAN NOT NULL DEFAULT false,
    event_conflict                BOOLEAN NOT NULL DEFAULT false,
    contradiction_flags_json      JSONB,
    market_regime                 TEXT,     -- 指示書33番：Phase5 classify_market_regimeを再利用
    sector_strength_at_event       NUMERIC,  -- 指示書34番
    reasons_json                   JSONB,
    warnings_json                   JSONB,
    created_at                       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_event_decision_support_event ON event_decision_support(event_id, ticker, evaluated_at DESC);
CREATE INDEX IF NOT EXISTS idx_event_decision_support_ticker ON event_decision_support(ticker, evaluated_at DESC);
CREATE INDEX IF NOT EXISTS idx_event_decision_support_created ON event_decision_support(created_at);
"""


def create_event_decision_support(database_url, fields):
    """指示書1番：event_decision_support行を1件INSERTする（履歴として積む、UPDATEしない）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return None
    cols = ["event_id", "ticker", "available_data_at", "material_quality_score", "reaction_quality_score",
            "extension_score", "freshness_score", "historical_edge_score", "source_confidence_score",
            "decision_support_score", "decision_support_state", "event_direction", "event_direction_confidence",
            "persistence_class", "avoid_chase", "pullback_candidate", "preferred_pullback_zone_json",
            "failed_reaction", "sell_the_news", "event_conflict", "contradiction_flags_json",
            "market_regime", "sector_strength_at_event", "reasons_json", "warnings_json"]
    json_cols = {"preferred_pullback_zone_json", "contradiction_flags_json", "reasons_json", "warnings_json"}
    values = [fields.get(c) for c in cols]
    wrapped = [json.dumps(v, ensure_ascii=False) if (c in json_cols and v is not None) else v
               for c, v in zip(cols, values)]
    placeholders = [f"%s::jsonb" if c in json_cols else "%s" for c in cols]
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"INSERT INTO event_decision_support ({', '.join(cols)}) "
                f"VALUES ({', '.join(placeholders)}) RETURNING *",
                wrapped)
            row = cur.fetchone()
        conn.commit()
    return _row_to_json(row) if row else None


def get_latest_event_decision_support(database_url, event_id, ticker=None):
    """指示書1・27番：最新1件（on-demand再計算のキャッシュ判定・API表示用）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return None
    where = ["event_id = %s"]
    params = [event_id]
    if ticker:
        where.append("ticker = %s")
        params.append(ticker)
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"SELECT * FROM event_decision_support WHERE {' AND '.join(where)} "
                f"ORDER BY evaluated_at DESC LIMIT 1", params)
            row = cur.fetchone()
    return _row_to_json(row) if row else None


def list_event_decision_support_history(database_url, event_id, ticker=None, limit=50):
    """指示書28・29番：decision_supportの遷移履歴（STRONG_SUPPORT→AVOID_CHASE→…）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return []
    where = ["event_id = %s"]
    params = [event_id]
    if ticker:
        where.append("ticker = %s")
        params.append(ticker)
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"SELECT * FROM event_decision_support WHERE {' AND '.join(where)} "
                f"ORDER BY evaluated_at ASC LIMIT %s", params + [limit])
            rows = cur.fetchall()
    return [_row_to_json(r) for r in rows]


def list_recent_event_decision_support(database_url, since_iso, limit=200, state=None):
    """指示書18・40番：直近の最新スナップショット一覧（GET /decision-support・
    build_ticker_intelligence_summary向け）。ticker毎に最新1件のみを返す（DISTINCT ON）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return []
    where = ["evaluated_at >= %s"]
    params = [since_iso]
    if state:
        where.append("decision_support_state = %s")
        params.append(state)
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"SELECT DISTINCT ON (event_id, ticker) * FROM event_decision_support "
                f"WHERE {' AND '.join(where)} ORDER BY event_id, ticker, evaluated_at DESC LIMIT %s",
                params + [limit])
            rows = cur.fetchall()
    return [_row_to_json(r) for r in rows]


def list_event_decision_support_for_ticker(database_url, ticker, since_iso, limit=50):
    """指示書18番：build_ticker_intelligence_summary(ticker)向け、直近event毎の最新1件。"""
    pool = _get_pool(database_url)
    if pool is None:
        return []
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT DISTINCT ON (event_id) * FROM event_decision_support "
                "WHERE ticker = %s AND evaluated_at >= %s ORDER BY event_id, evaluated_at DESC LIMIT %s",
                [ticker, since_iso, limit])
            rows = cur.fetchall()
    return [_row_to_json(r) for r in rows]


def list_event_decision_support_for_tickers(database_url, tickers, since_iso, limit=50):
    """Market Data Phase 2（2026-09-15新規）：list_event_decision_support_for_ticker()の
    バッチ版。ENTRY TOP5がwatchlist件数だけ個別にこの関数を呼んでいたDB N+1を解消するため、
    複数tickerを1回のSQL（WHERE ticker = ANY(%s)）でまとめて取得し、{ticker: [rows]}で返す。
    各tickerの中身・並び順・件数上限は単体版と完全に同一になるようDISTINCT ON (ticker,
    event_id)で同じ集約をSQL側に任せたうえ、limitでの打ち切りだけPython側で行う——単体版は
    "ORDER BY event_id, evaluated_at DESC LIMIT %s"だが、DISTINCT ONで(event_id)ごとに
    既に1行へ収束済みのためevaluated_at DESCは同点タイブレークとしてしか働かず、実質
    event_id昇順にLIMIT件を切り出しているだけ——本関数もticker, event_id昇順でグループ化
    してから同じ件数だけ切り出すことで同じ結果になる（ゴールデン比較テストで確認）。
    tickersが空、またはpool取得失敗時は{ticker: []}を返す（存在しないtickerも同様）。"""
    out = {t: [] for t in tickers}
    pool = _get_pool(database_url)
    if pool is None or not tickers:
        return out
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT DISTINCT ON (ticker, event_id) * FROM event_decision_support "
                "WHERE ticker = ANY(%s) AND evaluated_at >= %s "
                "ORDER BY ticker, event_id, evaluated_at DESC",
                [list(tickers), since_iso])
            rows = cur.fetchall()
    grouped = {}
    for r in rows:
        grouped.setdefault(r["ticker"], []).append(_row_to_json(r))
    for t, rs in grouped.items():
        if t in out:
            out[t] = rs[:limit]
    return out


def count_event_decision_support_since(database_url, since_iso, flag_col=None, state=None):
    """指示書41番：diagnostics向け（decision_support_generated_today・avoid_chase_count等）。
    flag_colはavoid_chase/pullback_candidate/failed_reaction/event_conflict等のBOOLEAN列名。"""
    pool = _get_pool(database_url)
    if pool is None:
        return 0
    _allowed_flags = {"avoid_chase", "pullback_candidate", "failed_reaction", "event_conflict", "sell_the_news"}
    where = ["created_at >= %s"]
    params = [since_iso]
    if flag_col and flag_col in _allowed_flags:
        where.append(f"{flag_col} = true")
    if state:
        where.append("decision_support_state = %s")
        params.append(state)
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM event_decision_support WHERE {' AND '.join(where)}", params)
            return cur.fetchone()[0]


# ============================================================
# Market Intelligence Phase10（2026-09-11新規）：Real-Time Calibration / Decision
# Support Replay / Trade Review Integration。売買判断（ENTRY/ADD/HOLD/EXIT/STOP）の前後で
# decision_supportのスナップショットを保存し、後から「結果」と「判断の質」を分離して
# 評価できるようにする。既存のBUY/WAIT/SELL・ENTRY TOP5のentry_score・AUTO_RS・
# AUTO_SECTOR・損切り・ポジションサイジングは一切直接変更しない（指示書42番）。
# trade_decision_contextは immutable（一切UPDATEしない、INSERTのみ）——指示書2番の
# no hindsight保証をテーブル設計レベルで担保する。
# ============================================================

_SCHEMA_TRADE_DECISION_CONTEXT_SQL = """
CREATE TABLE IF NOT EXISTS trade_decision_context (
    id                          SERIAL PRIMARY KEY,
    user_id                     TEXT NOT NULL,
    trade_id                    TEXT NOT NULL,
    ticker                      TEXT NOT NULL,
    captured_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
    action                      TEXT NOT NULL,   -- ENTRY|ADD|HOLD|EXIT|STOP
    price                       NUMERIC,
    entry_score                 NUMERIC,
    event_support_state         TEXT,
    material_quality_score      NUMERIC,
    reaction_quality_score      NUMERIC,
    extension_score             NUMERIC,
    decision_support_score      NUMERIC,
    pullback_candidate          BOOLEAN NOT NULL DEFAULT false,
    avoid_chase                 BOOLEAN NOT NULL DEFAULT false,
    active_event_ids_json       JSONB,
    market_regime_json          JSONB,
    sector_strength_json        JSONB,
    available_data_at           TIMESTAMPTZ,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_trade_decision_context_trade ON trade_decision_context(trade_id, captured_at);
CREATE INDEX IF NOT EXISTS idx_trade_decision_context_ticker ON trade_decision_context(ticker, captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_trade_decision_context_user ON trade_decision_context(user_id, created_at);

CREATE TABLE IF NOT EXISTS trade_outcome_evaluations (
    id                            SERIAL PRIMARY KEY,
    user_id                       TEXT NOT NULL,
    trade_id                      TEXT NOT NULL,
    ticker                        TEXT NOT NULL,
    entry_context_id              INTEGER REFERENCES trade_decision_context(id),
    exit_context_id                INTEGER REFERENCES trade_decision_context(id),
    pnl_pct                        NUMERIC,
    max_favorable_excursion_pct     NUMERIC,
    max_adverse_excursion_pct       NUMERIC,
    decision_quality                 TEXT,  -- GOOD/BAD_DECISION_GOOD/BAD_RESULT
    timing_quality                    TEXT,  -- EARLY_GOOD/GOOD_ENTRY/LATE_ENTRY/CHASE_ENTRY/...
    rule_compliance                    TEXT,  -- COMPLIANT|RULE_OVERRIDES_EVENT|VIOLATION等
    event_support_accuracy              TEXT,  -- FALSE_POSITIVE|FALSE_NEGATIVE|ACCURATE|NULL
    wait_outcome                         TEXT,  -- CORRECT_WAIT|GOOD_PULLBACK|MISSED_BREAKOUT|NO_EDGE
    opportunity_cost_pct                  NUMERIC,
    avoided_loss_pct                       NUMERIC,
    review_notes_json                       JSONB,
    created_at                               TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                               TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (trade_id)
);
CREATE INDEX IF NOT EXISTS idx_trade_outcome_evaluations_user ON trade_outcome_evaluations(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS event_decision_transitions (
    id                SERIAL PRIMARY KEY,
    event_id          INTEGER NOT NULL REFERENCES underlying_events(id) ON DELETE CASCADE,
    ticker            TEXT NOT NULL,
    from_state        TEXT,
    to_state          TEXT NOT NULL,
    transition_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    price             NUMERIC,
    reason            TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_event_decision_transitions_event ON event_decision_transitions(event_id, ticker, transition_at);

CREATE TABLE IF NOT EXISTS entry_candidate_snapshots (
    id                  SERIAL PRIMARY KEY,
    user_id             TEXT NOT NULL,
    code                TEXT NOT NULL,
    candidate_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    entry_score         NUMERIC,
    event_support       TEXT,
    price_at_candidate  NUMERIC,
    subsequent_30m_pct  NUMERIC,
    subsequent_close_pct NUMERIC,
    was_taken           BOOLEAN NOT NULL DEFAULT false,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_entry_candidate_snapshots_code ON entry_candidate_snapshots(code, candidate_at DESC);
CREATE INDEX IF NOT EXISTS idx_entry_candidate_snapshots_user ON entry_candidate_snapshots(user_id, created_at);
"""

# ============================================================
# Market Intelligence Phase11（2026-09-11新規）：Candidate Outcome Engine / Historical
# Replay Accuracy / Live Validation。entry_candidate_snapshotsを拡張し、ENTRY/WAIT/
# PULLBACK/AVOID_CHASE候補（買わなかったものも含む）を自動追跡・評価できるようにする
# （指示書1〜11・29・30番）。既存列（price_at_candidate等）と重複する列は追加しない
# （指示書2番）。
# ============================================================

_MIGRATE_ENTRY_CANDIDATE_SNAPSHOTS_V2_SQL = """
ALTER TABLE entry_candidate_snapshots ADD COLUMN IF NOT EXISTS candidate_type TEXT NOT NULL DEFAULT 'ENTRY';
ALTER TABLE entry_candidate_snapshots ADD COLUMN IF NOT EXISTS candidate_state TEXT;
ALTER TABLE entry_candidate_snapshots ADD COLUMN IF NOT EXISTS candidate_rank INTEGER;
ALTER TABLE entry_candidate_snapshots ADD COLUMN IF NOT EXISTS extension_score NUMERIC;
ALTER TABLE entry_candidate_snapshots ADD COLUMN IF NOT EXISTS wait_reason TEXT;
ALTER TABLE entry_candidate_snapshots ADD COLUMN IF NOT EXISTS subsequent_5m_pct NUMERIC;
ALTER TABLE entry_candidate_snapshots ADD COLUMN IF NOT EXISTS subsequent_1h_pct NUMERIC;
ALTER TABLE entry_candidate_snapshots ADD COLUMN IF NOT EXISTS subsequent_next_close_pct NUMERIC;
ALTER TABLE entry_candidate_snapshots ADD COLUMN IF NOT EXISTS mfe_pct NUMERIC;
ALTER TABLE entry_candidate_snapshots ADD COLUMN IF NOT EXISTS mae_pct NUMERIC;
ALTER TABLE entry_candidate_snapshots ADD COLUMN IF NOT EXISTS outcome_status TEXT;
ALTER TABLE entry_candidate_snapshots ADD COLUMN IF NOT EXISTS outcome_evaluated_at TIMESTAMPTZ;
ALTER TABLE entry_candidate_snapshots ADD COLUMN IF NOT EXISTS evaluation_quality TEXT;
ALTER TABLE entry_candidate_snapshots ADD COLUMN IF NOT EXISTS data_quality_score NUMERIC;
ALTER TABLE entry_candidate_snapshots ADD COLUMN IF NOT EXISTS dedupe_key TEXT;
ALTER TABLE entry_candidate_snapshots ADD COLUMN IF NOT EXISTS config_version TEXT NOT NULL DEFAULT 'DS_V1';
CREATE UNIQUE INDEX IF NOT EXISTS idx_entry_candidate_snapshots_dedupe
    ON entry_candidate_snapshots(dedupe_key) WHERE dedupe_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_entry_candidate_snapshots_outcome_due
    ON entry_candidate_snapshots(outcome_status, candidate_at);
CREATE INDEX IF NOT EXISTS idx_entry_candidate_snapshots_type ON entry_candidate_snapshots(candidate_type);

CREATE TABLE IF NOT EXISTS validation_sessions (
    id                 SERIAL PRIMARY KEY,
    session_date       DATE NOT NULL,
    app_version        TEXT,
    commit_hash        TEXT,
    config_version     TEXT,
    validation_version TEXT,
    mode               TEXT,
    environment        TEXT,
    started_at         TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (session_date)
);
"""


def create_trade_decision_context(database_url, user_id, fields):
    """指示書1・2・3番：売買判断snapshotを1件INSERTする（immutable、UPDATEは一切行わない
    ——no hindsight保証をテーブル設計レベルで担保する）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return None
    cols = ["trade_id", "ticker", "action", "price", "entry_score", "event_support_state",
            "material_quality_score", "reaction_quality_score", "extension_score", "decision_support_score",
            "pullback_candidate", "avoid_chase", "active_event_ids_json", "market_regime_json",
            "sector_strength_json", "available_data_at"]
    json_cols = {"active_event_ids_json", "market_regime_json", "sector_strength_json"}
    values = [fields.get(c) for c in cols]
    wrapped = [json.dumps(v, ensure_ascii=False) if (c in json_cols and v is not None) else v
               for c, v in zip(cols, values)]
    placeholders = ["%s::jsonb" if c in json_cols else "%s" for c in cols]
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"INSERT INTO trade_decision_context (user_id, {', '.join(cols)}) "
                f"VALUES (%s, {', '.join(placeholders)}) RETURNING *",
                [user_id] + wrapped)
            row = cur.fetchone()
        conn.commit()
    return _row_to_json(row) if row else None


def get_trade_decision_context(database_url, context_id):
    pool = _get_pool(database_url)
    if pool is None:
        return None
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM trade_decision_context WHERE id = %s", [context_id])
            row = cur.fetchone()
    return _row_to_json(row) if row else None


def list_trade_decision_contexts_for_trade(database_url, trade_id):
    """指示書3・35番：1つのtrade_idに紐づくENTRY〜EXITの全snapshot（時系列順）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return []
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM trade_decision_context WHERE trade_id = %s ORDER BY captured_at ASC",
                [trade_id])
            rows = cur.fetchall()
    return [_row_to_json(r) for r in rows]


def list_recent_trade_decision_contexts(database_url, user_id, since_iso, limit=500):
    """指示書16・39番：calibration dataset・diagnostics向け。"""
    pool = _get_pool(database_url)
    if pool is None:
        return []
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM trade_decision_context WHERE user_id = %s AND created_at >= %s "
                "ORDER BY created_at DESC LIMIT %s", [user_id, since_iso, limit])
            rows = cur.fetchall()
    return [_row_to_json(r) for r in rows]


def count_trade_decision_contexts_since(database_url, user_id, since_iso, action=None):
    pool = _get_pool(database_url)
    if pool is None:
        return 0
    where = ["user_id = %s", "created_at >= %s"]
    params = [user_id, since_iso]
    if action:
        where.append("action = %s")
        params.append(action)
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM trade_decision_context WHERE {' AND '.join(where)}", params)
            return cur.fetchone()[0]


def upsert_trade_outcome_evaluation(database_url, user_id, trade_id, fields):
    """指示書6・7・8番：1trade_id=1行。INSERT ON CONFLICT(trade_id) DO UPDATEで再評価にも
    対応する（decision_quality等は再計算可能な派生値であり、trade_decision_context自体は
    不変のまま——immutableなのはsnapshotのみ、という設計）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return None
    cols = ["ticker", "entry_context_id", "exit_context_id", "pnl_pct", "max_favorable_excursion_pct",
            "max_adverse_excursion_pct", "decision_quality", "timing_quality", "rule_compliance",
            "event_support_accuracy", "wait_outcome", "opportunity_cost_pct", "avoided_loss_pct",
            "review_notes_json"]
    json_cols = {"review_notes_json"}
    values = [fields.get(c) for c in cols]
    wrapped = [json.dumps(v, ensure_ascii=False) if (c in json_cols and v is not None) else v
               for c, v in zip(cols, values)]
    set_clauses = [f"{c}=COALESCE(EXCLUDED.{c}, trade_outcome_evaluations.{c})" for c in cols]
    placeholders = ["%s::jsonb" if c in json_cols else "%s" for c in cols]
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"INSERT INTO trade_outcome_evaluations (user_id, trade_id, {', '.join(cols)}) "
                f"VALUES (%s, %s, {', '.join(placeholders)}) "
                f"ON CONFLICT (trade_id) DO UPDATE SET {', '.join(set_clauses)}, updated_at=now() "
                f"RETURNING *",
                [user_id, trade_id] + wrapped)
            row = cur.fetchone()
        conn.commit()
    return _row_to_json(row) if row else None


def get_trade_outcome_evaluation(database_url, trade_id):
    pool = _get_pool(database_url)
    if pool is None:
        return None
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM trade_outcome_evaluations WHERE trade_id = %s", [trade_id])
            row = cur.fetchone()
    return _row_to_json(row) if row else None


def list_trade_outcome_evaluations(database_url, user_id, since_iso=None, limit=500):
    """指示書16・17・18・19・20番：calibration・daily review集計向け。"""
    pool = _get_pool(database_url)
    if pool is None:
        return []
    where = ["user_id = %s"]
    params = [user_id]
    if since_iso:
        where.append("created_at >= %s")
        params.append(since_iso)
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"SELECT * FROM trade_outcome_evaluations WHERE {' AND '.join(where)} "
                f"ORDER BY created_at DESC LIMIT %s", params + [limit])
            rows = cur.fetchall()
    return [_row_to_json(r) for r in rows]


def count_trade_outcome_evaluations_since(database_url, user_id, since_iso, decision_quality=None):
    pool = _get_pool(database_url)
    if pool is None:
        return 0
    where = ["user_id = %s", "created_at >= %s"]
    params = [user_id, since_iso]
    if decision_quality:
        where.append("decision_quality = %s")
        params.append(decision_quality)
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM trade_outcome_evaluations WHERE {' AND '.join(where)}", params)
            return cur.fetchone()[0]


def create_event_decision_transition(database_url, fields):
    """指示書12・13番：decision_support_stateの遷移記録（transition理由付き）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return None
    cols = ["event_id", "ticker", "from_state", "to_state", "price", "reason"]
    values = [fields.get(c) for c in cols]
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"INSERT INTO event_decision_transitions ({', '.join(cols)}) "
                f"VALUES ({', '.join(['%s'] * len(cols))}) RETURNING *", values)
            row = cur.fetchone()
        conn.commit()
    return _row_to_json(row) if row else None


def list_event_decision_transitions_for_event(database_url, event_id, ticker=None):
    pool = _get_pool(database_url)
    if pool is None:
        return []
    where = ["event_id = %s"]
    params = [event_id]
    if ticker:
        where.append("ticker = %s")
        params.append(ticker)
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"SELECT * FROM event_decision_transitions WHERE {' AND '.join(where)} ORDER BY transition_at ASC",
                params)
            rows = cur.fetchall()
    return [_row_to_json(r) for r in rows]


def create_entry_candidate_snapshot(database_url, user_id, fields):
    """指示書3・4・29・30番：ENTRY TOP5/WAIT/PULLBACK/AVOID_CHASE候補（買わなかったものも
    含む）のsnapshot。dedupe_keyにUNIQUE partial indexがあるためON CONFLICT DO NOTHINGで
    1日・銘柄・候補タイプ・状態ごとの重複保存を防ぐ（状態が変われば別dedupe_keyになり新規
    snapshotとして保存される、指示書4番の「状態変化した場合は新snapshot可」に対応）。
    Phase MU-S3B：entry_candidate_snapshotsはSHARED化済み（市場・銘柄条件から自動算出される
    候補のみを保存し、個人の執行実績は保存しない設計。fields["was_taken"]は現状常にFalseで
    未配線——将来「実際に買ったか」等の個人執行情報をここに追加してはならない。追加する場合は
    private_execution_status等の別PRIVATEテーブルへ分離すること）。"""
    user_id = _SHARED_SCOPE
    pool = _get_pool(database_url)
    if pool is None:
        return None
    cols = ["code", "entry_score", "event_support", "price_at_candidate", "was_taken",
            "candidate_type", "candidate_state", "candidate_rank", "extension_score", "wait_reason",
            "data_quality_score", "dedupe_key", "config_version"]
    values = [fields.get(c) for c in cols]
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"INSERT INTO entry_candidate_snapshots (user_id, {', '.join(cols)}) "
                f"VALUES (%s, {', '.join(['%s'] * len(cols))}) "
                f"ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING RETURNING *",
                [user_id] + values)
            row = cur.fetchone()
        conn.commit()
    return _row_to_json(row) if row else None


def list_due_entry_candidate_snapshots_for_backfill(database_url, older_than_iso, limit=100):
    """指示書10・11番：outcome_statusが未確定・candidate_atからある程度時間が経過した
    snapshotをscheduler向けに返す（due AND not evaluated）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return []
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM entry_candidate_snapshots WHERE outcome_status IS NULL "
                "AND candidate_at <= %s ORDER BY candidate_at ASC LIMIT %s", [older_than_iso, limit])
            rows = cur.fetchall()
    return [_row_to_json(r) for r in rows]


def save_entry_candidate_snapshot_result(database_url, snapshot_id, **fields):
    """指示書2・6・7・11番：subsequent_*/mfe_pct/mae_pct/outcome_status等をUPDATEする。
    candidate自体（candidate_price・candidate_state等）はimmutable、outcome系フィールドの
    みここで更新可能にする。"""
    allowed = {"subsequent_5m_pct", "subsequent_30m_pct", "subsequent_1h_pct", "subsequent_close_pct",
               "subsequent_next_close_pct", "mfe_pct", "mae_pct", "outcome_status", "outcome_evaluated_at",
               "evaluation_quality", "data_quality_score"}
    pool = _get_pool(database_url)
    if pool is None:
        return None
    sets, params = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        sets.append(f"{k}=COALESCE(%s, {k})")
        params.append(v)
    if not sets:
        return None
    params.append(snapshot_id)
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(f"UPDATE entry_candidate_snapshots SET {', '.join(sets)} WHERE id=%s RETURNING *", params)
            row = cur.fetchone()
        conn.commit()
    return _row_to_json(row) if row else None


def list_entry_candidate_snapshots_for_code(database_url, code, since_iso, limit=100):
    pool = _get_pool(database_url)
    if pool is None:
        return []
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM entry_candidate_snapshots WHERE code = %s AND candidate_at >= %s "
                "ORDER BY candidate_at DESC LIMIT %s", [code, since_iso, limit])
            rows = cur.fetchall()
    return [_row_to_json(r) for r in rows]


def list_entry_candidate_snapshots(database_url, user_id, since_iso, candidate_type=None, limit=1000):
    """指示書17〜21・27・29・31番：performance集計・coverage rate算出向けの全件取得。
    Phase MU-S3B：entry_candidate_snapshotsはSHARED化済み。"""
    user_id = _SHARED_SCOPE
    pool = _get_pool(database_url)
    if pool is None:
        return []
    where = ["user_id = %s", "candidate_at >= %s"]
    params = [user_id, since_iso]
    if candidate_type:
        where.append("candidate_type = %s")
        params.append(candidate_type)
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"SELECT * FROM entry_candidate_snapshots WHERE {' AND '.join(where)} "
                f"ORDER BY candidate_at DESC LIMIT %s", params + [limit])
            rows = cur.fetchall()
    return [_row_to_json(r) for r in rows]


def count_entry_candidate_snapshots_since(database_url, user_id, since_iso):
    # Phase MU-S3B：entry_candidate_snapshotsはSHARED化済み。
    user_id = _SHARED_SCOPE
    pool = _get_pool(database_url)
    if pool is None:
        return 0
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM entry_candidate_snapshots WHERE user_id=%s AND created_at >= %s",
                [user_id, since_iso])
            return cur.fetchone()[0]


def count_entry_candidate_snapshots_pending(database_url, user_id):
    # Phase MU-S3B：entry_candidate_snapshotsはSHARED化済み。
    user_id = _SHARED_SCOPE
    pool = _get_pool(database_url)
    if pool is None:
        return 0
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM entry_candidate_snapshots WHERE user_id=%s AND outcome_status IS NULL",
                [user_id])
            return cur.fetchone()[0]


def count_entry_candidate_snapshots_evaluated_since(database_url, user_id, since_iso):
    # Phase MU-S3B：entry_candidate_snapshotsはSHARED化済み。
    user_id = _SHARED_SCOPE
    pool = _get_pool(database_url)
    if pool is None:
        return 0
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM entry_candidate_snapshots WHERE user_id=%s "
                "AND outcome_evaluated_at >= %s", [user_id, since_iso])
            return cur.fetchone()[0]


# ============================================================
# Market Intelligence Phase12（2026-09-11新規）：Live Validation / Production Hardening。
# 新しい売買判断ロジックは追加しない（指示書冒頭）——ここはDB整合性監査・parser失敗キュー・
# validation metadataのみ。
# ============================================================

_MIGRATE_VALIDATION_SESSIONS_V2_SQL = """
ALTER TABLE validation_sessions ADD COLUMN IF NOT EXISTS validation_version TEXT;
ALTER TABLE validation_sessions ADD COLUMN IF NOT EXISTS mode TEXT;
ALTER TABLE validation_sessions ADD COLUMN IF NOT EXISTS environment TEXT;
ALTER TABLE validation_sessions ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ;
"""

_SCHEMA_PARSER_FAILURE_QUEUE_SQL = """
CREATE TABLE IF NOT EXISTS parser_failure_queue (
    id            SERIAL PRIMARY KEY,
    source        TEXT NOT NULL,
    post_id       TEXT,
    parser        TEXT,
    error         TEXT,
    retryable     BOOLEAN NOT NULL DEFAULT true,
    retry_count   INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'PENDING',
    failed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_parser_failure_queue_status ON parser_failure_queue(status, failed_at);
"""

# 指示書8・27・28番：同じレコード（source+post_id+parser）が繰り返し失敗した場合に1行へ集約する
# （retry_countを積み上げる、無限行を作らない）。
PARSER_FAILURE_DEAD_LETTER_RETRY_THRESHOLD = 5


def record_parser_failure(database_url, source, post_id, parser, error, retryable=True):
    """指示書27・28番：parser失敗を黙って捨てない。同一source+post_id+parserの既存行があれば
    retry_countを増やし、閾値超過でDEAD_LETTERへ隔離する（無限retry禁止）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return None
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM parser_failure_queue WHERE source=%s AND post_id=%s AND parser=%s "
                "AND status != 'DEAD_LETTER'", [source, post_id, parser])
            existing = cur.fetchone()
            if existing:
                new_count = (existing.get("retry_count") or 0) + 1
                status = "DEAD_LETTER" if new_count >= PARSER_FAILURE_DEAD_LETTER_RETRY_THRESHOLD else "PENDING"
                cur.execute(
                    "UPDATE parser_failure_queue SET retry_count=%s, status=%s, error=%s, updated_at=now() "
                    "WHERE id=%s RETURNING *", [new_count, status, error, existing["id"]])
            else:
                cur.execute(
                    "INSERT INTO parser_failure_queue (source, post_id, parser, error, retryable) "
                    "VALUES (%s, %s, %s, %s, %s) RETURNING *", [source, post_id, parser, error, retryable])
            row = cur.fetchone()
        conn.commit()
    return _row_to_json(row) if row else None


def list_parser_failures(database_url, status=None, limit=100):
    pool = _get_pool(database_url)
    if pool is None:
        return []
    where, params = [], []
    if status:
        where.append("status = %s")
        params.append(status)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(f"SELECT * FROM parser_failure_queue {clause} ORDER BY failed_at DESC LIMIT %s",
                        params + [limit])
            rows = cur.fetchall()
    return [_row_to_json(r) for r in rows]


def count_parser_failures(database_url, status=None):
    pool = _get_pool(database_url)
    if pool is None:
        return 0
    where, params = [], []
    if status:
        where.append("status = %s")
        params.append(status)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM parser_failure_queue {clause}", params)
            return cur.fetchone()[0]


# ============================================================
# 2026-09-14新規（ニュース機能修正指示書F：再通知禁止）：同じ重要ニュースを二度通知しない
# ためのnotification_log。title文字列だけではなくnews.pyのdedupeキー（TDnet document ID／
# normalized URL／stock_code+normalized_title+published_at）由来のstable keyで判定する
# （notification_key）。アプリ（サーバープロセス）再起動後も再通知しないよう、DBへ永続化する
# （メモリ上のSetでは再起動で消えてしまうため）。
# ============================================================
_SCHEMA_NEWS_NOTIFICATION_LOG_SQL = """
CREATE TABLE IF NOT EXISTS notification_log (
    id                  SERIAL PRIMARY KEY,
    notification_key    TEXT NOT NULL UNIQUE,
    news_id             TEXT,
    notification_type   TEXT,
    importance_score     INTEGER,
    notified_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_notification_log_notified_at ON notification_log(notified_at DESC);
"""


def was_already_notified(database_url, notification_key):
    """指示書F：このnotification_keyが過去に一度でも通知済みならTrue。"""
    pool = _get_pool(database_url)
    if pool is None or not notification_key:
        return False
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM notification_log WHERE notification_key=%s", [notification_key])
            return cur.fetchone() is not None


def record_notification(database_url, notification_key, news_id=None, notification_type=None, importance_score=None):
    """指示書F：通知済みとして記録する。ON CONFLICT DO NOTHINGで冪等
    （同時実行・リトライで二重挿入しない、notification_keyはUNIQUE制約）。
    戻り値：True=新規記録、False=既に記録済み（＝二重通知の可能性があったことを示す）。"""
    pool = _get_pool(database_url)
    if pool is None or not notification_key:
        return False
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO notification_log (notification_key, news_id, notification_type, importance_score) "
                "VALUES (%s,%s,%s,%s) ON CONFLICT (notification_key) DO NOTHING RETURNING id",
                [notification_key, news_id, notification_type, importance_score])
            row = cur.fetchone()
        conn.commit()
    return row is not None


def list_recent_notifications(database_url, limit=100):
    pool = _get_pool(database_url)
    if pool is None:
        return []
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM notification_log ORDER BY notified_at DESC LIMIT %s", [limit])
            rows = cur.fetchall()
    return [_row_to_json(r) for r in rows]


# ============================================================
# News Intelligence Phase 2（2026-09-14新規・指示書12）：朝一チェック／場中4レポートへ
# market_news_context（構造化された「今日の市場を動かしている材料」要約）を持たせるための
# 列追加。記事本文の長文保存・転載はしない（title/URL/短いsummary相当の構造化情報のみ）。
# ============================================================
_MIGRATE_MARKET_NEWS_CONTEXT_SQL = """
ALTER TABLE morning_market_checks ADD COLUMN IF NOT EXISTS market_news_context_json JSONB;
ALTER TABLE market_intelligence_reports ADD COLUMN IF NOT EXISTS market_news_context_json JSONB;
"""

# ============================================================
# X Intelligence Phase5（2026-09-15新規）：朝一チェック／場中4レポートへ、共通Intelligence
# Context（build_external_intelligence_context()の戻り値、facts/expert_views/consensus/
# disagreements/event_signals/warnings/stock_signals/sector_signals/freshness/diagnostics）
# を持たせるための列追加。既存のexternal_market_commentary（にこそく専用、
# raw_payload_json内）とは別枠——既存フィールドは削除・変更しない。
# ============================================================
_MIGRATE_EXTERNAL_INTELLIGENCE_CONTEXT_SQL = """
ALTER TABLE morning_market_checks ADD COLUMN IF NOT EXISTS external_intelligence_json JSONB;
ALTER TABLE market_intelligence_reports ADD COLUMN IF NOT EXISTS external_intelligence_json JSONB;
"""


# ---- 指示書15・16番：duplicate audit / orphan audit（自動削除はしない、報告のみ） ----

def count_duplicate_underlying_events(database_url):
    """event_keyが同一で複数行存在する（本来マージされるべきもの）。event_key未生成の素材は
    対象外（generate_event_keyがNoneを返すケース、既知の制約）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return 0
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM (SELECT event_key FROM underlying_events WHERE event_key IS NOT NULL "
                "GROUP BY event_key HAVING COUNT(*) > 1) t")
            return cur.fetchone()[0]


def count_duplicate_candidate_snapshots(database_url):
    """dedupe_keyにUNIQUE partial indexがあるため通常0のはず——0でなければindex破損等の
    異常を示す。"""
    pool = _get_pool(database_url)
    if pool is None:
        return 0
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM (SELECT dedupe_key FROM entry_candidate_snapshots "
                "WHERE dedupe_key IS NOT NULL GROUP BY dedupe_key HAVING COUNT(*) > 1) t")
            return cur.fetchone()[0]


def count_duplicate_event_market_reactions(database_url):
    """UNIQUE(event_id,ticker,reaction_window)があるため通常0のはず。"""
    pool = _get_pool(database_url)
    if pool is None:
        return 0
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM (SELECT event_id, ticker, reaction_window FROM event_market_reactions "
                "GROUP BY event_id, ticker, reaction_window HAVING COUNT(*) > 1) t")
            return cur.fetchone()[0]


def count_orphan_evidence(database_url):
    """event_idにFK（ON DELETE CASCADE）があるため構造的には発生しないはずだが、監査として
    明示的に確認する（指示書16番「自動削除しない、報告のみ」）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return 0
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM underlying_event_evidence e "
                "WHERE NOT EXISTS (SELECT 1 FROM underlying_events u WHERE u.id = e.event_id)")
            return cur.fetchone()[0]


def count_orphan_event_market_reactions(database_url):
    pool = _get_pool(database_url)
    if pool is None:
        return 0
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM event_market_reactions r "
                "WHERE NOT EXISTS (SELECT 1 FROM underlying_events u WHERE u.id = r.event_id)")
            return cur.fetchone()[0]


def count_orphan_event_decision_support(database_url):
    pool = _get_pool(database_url)
    if pool is None:
        return 0
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM event_decision_support d "
                "WHERE NOT EXISTS (SELECT 1 FROM underlying_events u WHERE u.id = d.event_id)")
            return cur.fetchone()[0]


def count_orphan_event_decision_transitions(database_url):
    pool = _get_pool(database_url)
    if pool is None:
        return 0
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM event_decision_transitions t "
                "WHERE NOT EXISTS (SELECT 1 FROM underlying_events u WHERE u.id = t.event_id)")
            return cur.fetchone()[0]


# 指示書11番：起動時・diagnosticsでの簡易スキーマ整合性チェック（存在確認のみ、DBを勝手に
# 修復しない）。
EXPECTED_MARKET_INTELLIGENCE_TABLES = [
    "underlying_events", "underlying_event_evidence", "event_market_reactions", "prediction_resolutions",
    "event_decision_support", "trade_decision_context", "trade_outcome_evaluations",
    "event_decision_transitions", "entry_candidate_snapshots", "validation_sessions", "parser_failure_queue",
]


def check_schema_integrity(database_url):
    """指示書11番：expected tableの存在確認。列単位までは踏み込まない（軽量チェック、
    既知の制約）。不整合は報告するのみで自動修復はしない。"""
    pool = _get_pool(database_url)
    if pool is None:
        return {"ok": False, "tables": {t: False for t in EXPECTED_MARKET_INTELLIGENCE_TABLES},
                 "missing": list(EXPECTED_MARKET_INTELLIGENCE_TABLES)}
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='public' "
                "AND table_name = ANY(%s)", [EXPECTED_MARKET_INTELLIGENCE_TABLES])
            existing = {r[0] for r in cur.fetchall()}
    tables = {t: (t in existing) for t in EXPECTED_MARKET_INTELLIGENCE_TABLES}
    missing = [t for t, ok in tables.items() if not ok]
    return {"ok": len(missing) == 0, "tables": tables, "missing": missing}


# ============================================================
# Market Intelligence Phase12.5（2026-09-11新規）：Live Activation / Shadow Data
# Accumulation / Operational Runbook。判断閾値・重み（DS_V1）は原則変更しない
# （指示書34番）——変更した場合のみここへ記録する（指示書35番）。
# ============================================================

_SCHEMA_CONFIG_CHANGE_LOG_SQL = """
CREATE TABLE IF NOT EXISTS config_change_log (
    id           SERIAL PRIMARY KEY,
    changed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    reason       TEXT NOT NULL,
    before_json  JSONB,
    after_json   JSONB,
    commit_hash  TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_config_change_log_changed_at ON config_change_log(changed_at DESC);
"""


def record_config_change(database_url, reason, before, after, commit_hash=None):
    """指示書35番：判断閾値・重みをどうしても変更した場合の監査ログ。Phase12.5期間中は
    原則呼ばれない想定（指示書34番）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return None
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "INSERT INTO config_change_log (reason, before_json, after_json, commit_hash) "
                "VALUES (%s, %s::jsonb, %s::jsonb, %s) RETURNING *",
                [reason, json.dumps(before, ensure_ascii=False), json.dumps(after, ensure_ascii=False), commit_hash])
            row = cur.fetchone()
        conn.commit()
    return _row_to_json(row) if row else None


def list_config_changes(database_url, limit=50):
    pool = _get_pool(database_url)
    if pool is None:
        return []
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM config_change_log ORDER BY changed_at DESC LIMIT %s", [limit])
            rows = cur.fetchall()
    return [_row_to_json(r) for r in rows]


# Choruco Style / ちょる子式（2026-09-12新規）。既存ENTRY SCORE/Rule Engine/Trade Experience
# Learning/daily_reviewsには一切変更を加えず、その上に積む補助判断レイヤー用の追加列・
# テーブルのみ。
_SCHEMA_CHORUCO_STYLE_SQL = """
-- 指示書38番：トレード終了後にちょる子式の文脈を保存する（既存のTrade Experience
-- Learningスキーマへの追加列。decision_snapshot_json/post_trade_analysis_jsonの
-- no-hindsight分離方針はそのまま踏襲——市場モード・イベントリスクは"at_entry"/"at_exit"を
-- 分けて保存し、事後情報を事前フィールドに混ぜない）。
ALTER TABLE trade_experiences ADD COLUMN IF NOT EXISTS market_mode_at_entry TEXT;
ALTER TABLE trade_experiences ADD COLUMN IF NOT EXISTS market_mode_at_exit TEXT;
ALTER TABLE trade_experiences ADD COLUMN IF NOT EXISTS event_risk_at_entry TEXT;
ALTER TABLE trade_experiences ADD COLUMN IF NOT EXISTS position_multiplier NUMERIC;
ALTER TABLE trade_experiences ADD COLUMN IF NOT EXISTS recommended_multiplier NUMERIC;
ALTER TABLE trade_experiences ADD COLUMN IF NOT EXISTS story_score_at_entry NUMERIC;
ALTER TABLE trade_experiences ADD COLUMN IF NOT EXISTS story_break_status TEXT;
ALTER TABLE trade_experiences ADD COLUMN IF NOT EXISTS story_break_reason TEXT;

-- 指示書39・40番：daily_reviewsへちょる子式評価（100点、既存score_totalとは別軸）を追加。
ALTER TABLE daily_reviews ADD COLUMN IF NOT EXISTS choruco_score NUMERIC;
ALTER TABLE daily_reviews ADD COLUMN IF NOT EXISTS choruco_breakdown_json JSONB;

-- 指示書50番：stock_behavior_profilesへちょる子式関連の集計列を追加候補として反映する。
ALTER TABLE stock_behavior_profiles ADD COLUMN IF NOT EXISTS attack_mode_win_rate NUMERIC;
ALTER TABLE stock_behavior_profiles ADD COLUMN IF NOT EXISTS normal_mode_win_rate NUMERIC;
ALTER TABLE stock_behavior_profiles ADD COLUMN IF NOT EXISTS defense_mode_win_rate NUMERIC;
ALTER TABLE stock_behavior_profiles ADD COLUMN IF NOT EXISTS story_break_frequency NUMERIC;
ALTER TABLE stock_behavior_profiles ADD COLUMN IF NOT EXISTS good_news_weak_price_rate NUMERIC;
ALTER TABLE stock_behavior_profiles ADD COLUMN IF NOT EXISTS event_sensitive_score NUMERIC;

-- 指示書13・58番：TRADE_STORY（ENTRY前から作成可能な、まだ約定していない銘柄の
-- ストーリー定義も持てるよう、trade_experiencesとは独立したテーブルにする）。
CREATE TABLE IF NOT EXISTS choruco_stories (
    id                          SERIAL PRIMARY KEY,
    user_id                     TEXT NOT NULL,
    symbol                      TEXT NOT NULL,
    story_json                  JSONB NOT NULL,   -- market/sector/setup/trigger/support/target/invalid_if
    story_score                 NUMERIC,
    story_score_breakdown_json  JSONB,
    status                      TEXT NOT NULL DEFAULT 'ACTIVE',  -- ACTIVE|WEAKENING|BROKEN
    break_reasons_json          JSONB,
    market_mode_at_entry        TEXT,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_choruco_stories_user_symbol ON choruco_stories(user_id, symbol, created_at DESC);
"""

# Cross-Market Link Phase 2（2026-09-12新規）。既存trade_experiences/stock_behavior_profilesへの
# 追加列のみ（新規テーブルは作らない）。
_SCHEMA_CROSS_MARKET_LINK_SQL = """
ALTER TABLE trade_experiences ADD COLUMN IF NOT EXISTS primary_driver TEXT;
ALTER TABLE trade_experiences ADD COLUMN IF NOT EXISTS driver_corr_at_entry NUMERIC;
ALTER TABLE trade_experiences ADD COLUMN IF NOT EXISTS driver_lag_at_entry INTEGER;
ALTER TABLE trade_experiences ADD COLUMN IF NOT EXISTS driver_state_at_entry TEXT;
ALTER TABLE trade_experiences ADD COLUMN IF NOT EXISTS driver_state_at_exit TEXT;
ALTER TABLE trade_experiences ADD COLUMN IF NOT EXISTS cross_market_score_at_entry NUMERIC;

ALTER TABLE stock_behavior_profiles ADD COLUMN IF NOT EXISTS primary_driver TEXT;
ALTER TABLE stock_behavior_profiles ADD COLUMN IF NOT EXISTS primary_driver_corr NUMERIC;
ALTER TABLE stock_behavior_profiles ADD COLUMN IF NOT EXISTS primary_driver_lag INTEGER;
ALTER TABLE stock_behavior_profiles ADD COLUMN IF NOT EXISTS cross_market_reliability NUMERIC;
"""

# Sector Rotation / Capital Flow Engine（2026-09-12新規、前提commit 9f396a1）。
_SCHEMA_SECTOR_ROTATION_SQL = """
ALTER TABLE trade_experiences ADD COLUMN IF NOT EXISTS sector_at_entry TEXT;
ALTER TABLE trade_experiences ADD COLUMN IF NOT EXISTS sector_state_at_entry TEXT;
ALTER TABLE trade_experiences ADD COLUMN IF NOT EXISTS sector_flow_score_at_entry NUMERIC;
ALTER TABLE trade_experiences ADD COLUMN IF NOT EXISTS sector_state_at_exit TEXT;
ALTER TABLE trade_experiences ADD COLUMN IF NOT EXISTS sector_flow_score_at_exit NUMERIC;
ALTER TABLE trade_experiences ADD COLUMN IF NOT EXISTS rotation_context_json JSONB;

ALTER TABLE stock_behavior_profiles ADD COLUMN IF NOT EXISTS best_sector_state_for_entry TEXT;
ALTER TABLE stock_behavior_profiles ADD COLUMN IF NOT EXISTS sector_leading_win_rate NUMERIC;
ALTER TABLE stock_behavior_profiles ADD COLUMN IF NOT EXISTS sector_weakening_loss_rate NUMERIC;
ALTER TABLE stock_behavior_profiles ADD COLUMN IF NOT EXISTS rotation_sensitivity NUMERIC;

-- 指示書38番：sector_behavior_profiles新設。
CREATE TABLE IF NOT EXISTS sector_behavior_profiles (
    id                        SERIAL PRIMARY KEY,
    user_id                   TEXT NOT NULL,
    sector_name               TEXT NOT NULL,
    sample_count              INTEGER NOT NULL DEFAULT 0,
    avg_leading_duration      NUMERIC,
    avg_flow_score            NUMERIC,
    breakout_success_rate     NUMERIC,
    exhaustion_failure_rate   NUMERIC,
    best_time_bucket          TEXT,
    time_bucket_stats_json    JSONB,
    confidence_level          TEXT NOT NULL DEFAULT 'LOW',
    last_updated              TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, sector_name)
);
"""


def create_choruco_story(database_url, user_id, symbol, story_json, story_score=None,
                            story_score_breakdown_json=None, market_mode_at_entry=None):
    """指示書13・58番：POST /api/choruco/story/{symbol}/create。"""
    pool = _get_pool(database_url)
    if pool is None or not symbol:
        return None
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "INSERT INTO choruco_stories (user_id, symbol, story_json, story_score, "
                "story_score_breakdown_json, market_mode_at_entry) VALUES (%s,%s,%s::jsonb,%s,%s::jsonb,%s) "
                "RETURNING *",
                [user_id, symbol, json.dumps(story_json, ensure_ascii=False), story_score,
                 json.dumps(story_score_breakdown_json, ensure_ascii=False) if story_score_breakdown_json else None,
                 market_mode_at_entry])
            row = cur.fetchone()
        conn.commit()
    return _row_to_json(row) if row else None


def update_choruco_story_evaluation(database_url, user_id, story_id, status, story_score=None,
                                        story_score_breakdown_json=None, break_reasons=None):
    """指示書58番：POST /api/choruco/story/{symbol}/evaluate。detect_story_break()の結果を
    保存する。"""
    pool = _get_pool(database_url)
    if pool is None:
        return None
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "UPDATE choruco_stories SET status=%s, "
                "story_score=COALESCE(%s, story_score), "
                "story_score_breakdown_json=COALESCE(%s::jsonb, story_score_breakdown_json), "
                "break_reasons_json=%s::jsonb, updated_at=now() "
                "WHERE id=%s AND user_id=%s RETURNING *",
                [status, story_score,
                 json.dumps(story_score_breakdown_json, ensure_ascii=False) if story_score_breakdown_json else None,
                 json.dumps(break_reasons or [], ensure_ascii=False), story_id, user_id])
            row = cur.fetchone()
        conn.commit()
    return _row_to_json(row) if row else None


def get_latest_choruco_story(database_url, user_id, symbol):
    """指示書58番：GET /api/choruco/story/{symbol}。最新1件を返す。"""
    pool = _get_pool(database_url)
    if pool is None:
        return None
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM choruco_stories WHERE user_id=%s AND symbol=%s "
                "ORDER BY created_at DESC LIMIT 1", [user_id, symbol])
            row = cur.fetchone()
    return _row_to_json(row) if row else None


def upsert_sector_behavior_profile(database_url, user_id, sector_name, fields):
    """Sector Rotation学習（2026-09-12新規、指示書38番）：sector単位で冪等にupsertする
    （UNIQUE(user_id,sector_name)）。"""
    pool = _get_pool(database_url)
    if pool is None or not sector_name:
        return None
    cols = ["sample_count", "avg_leading_duration", "avg_flow_score", "breakout_success_rate",
            "exhaustion_failure_rate", "best_time_bucket", "time_bucket_stats_json", "confidence_level"]
    json_cols = {"time_bucket_stats_json"}
    present = [c for c in cols if c in (fields or {})]
    if not present:
        return get_sector_behavior_profile(database_url, user_id, sector_name)
    values = [fields.get(c) for c in present]
    wrapped = [json.dumps(v, ensure_ascii=False) if (c in json_cols and v is not None) else v
               for c, v in zip(present, values)]
    insert_placeholders = ["%s::jsonb" if c in json_cols else "%s" for c in present]
    update_clauses = [f"{c}=EXCLUDED.{c}" for c in present] + ["last_updated=now()"]
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"INSERT INTO sector_behavior_profiles (user_id, sector_name, {', '.join(present)}) "
                f"VALUES (%s, %s, {', '.join(insert_placeholders)}) "
                f"ON CONFLICT (user_id, sector_name) DO UPDATE SET {', '.join(update_clauses)} "
                f"RETURNING *", [user_id, sector_name] + wrapped)
            row = cur.fetchone()
        conn.commit()
    return _row_to_json(row) if row else None


def get_sector_behavior_profile(database_url, user_id, sector_name):
    pool = _get_pool(database_url)
    if pool is None:
        return None
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM sector_behavior_profiles WHERE user_id=%s AND sector_name=%s",
                        [user_id, sector_name])
            row = cur.fetchone()
    return _row_to_json(row) if row else None


def list_sector_behavior_profiles(database_url, user_id):
    pool = _get_pool(database_url)
    if pool is None:
        return []
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM sector_behavior_profiles WHERE user_id=%s ORDER BY sector_name", [user_id])
            rows = cur.fetchall()
    return [_row_to_json(r) for r in rows]


# Yaaman Style / ヤーマン式 Theme Discovery（2026-09-12新規）。
_SCHEMA_YAAMAN_THEME_SQL = """
-- 指示書26番：ストップ高・急騰銘柄の履歴。
CREATE TABLE IF NOT EXISTS limit_up_events (
    id                SERIAL PRIMARY KEY,
    user_id           TEXT NOT NULL,
    event_date        DATE NOT NULL,
    symbol            TEXT NOT NULL,
    stock_name        TEXT,
    pct               NUMERIC,
    volume            NUMERIC,
    volume_ratio      NUMERIC,
    reason            TEXT,
    catalyst_type     TEXT,
    theme             TEXT,
    sector            TEXT,
    pts_change        NUMERIC,
    next_day_gap      NUMERIC,
    next_day_high     NUMERIC,
    next_day_close    NUMERIC,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, event_date, symbol)
);
CREATE INDEX IF NOT EXISTS idx_limit_up_events_user_date ON limit_up_events(user_id, event_date DESC);

-- 指示書27番：テーマの日次推移。
CREATE TABLE IF NOT EXISTS theme_momentum_history (
    id                     SERIAL PRIMARY KEY,
    user_id                TEXT NOT NULL,
    theme                  TEXT NOT NULL,
    event_date             DATE NOT NULL,
    stage                  TEXT,
    theme_score            NUMERIC,
    breadth                NUMERIC,
    leader_count           INTEGER,
    related_movers         INTEGER,
    next_day_confirmation  TEXT,
    day2_performance       NUMERIC,
    day3_performance       NUMERIC,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, theme, event_date)
);
CREATE INDEX IF NOT EXISTS idx_theme_momentum_history_user_theme ON theme_momentum_history(user_id, theme, event_date DESC);

-- 「明日の注目テーマ」（LEVEL1）を翌朝の答え合わせまで永続化する。指示書3・8・10・11番。
CREATE TABLE IF NOT EXISTS next_day_theme_candidates (
    id                     SERIAL PRIMARY KEY,
    user_id                TEXT NOT NULL,
    theme                  TEXT NOT NULL,
    generated_date         DATE NOT NULL,   -- 生成日（引け後、15:35想定）
    theme_score            NUMERIC,
    stage                  TEXT,
    trigger_stocks_json    JSONB,
    related_stocks_json    JSONB,
    catalyst               TEXT,
    breadth                NUMERIC,
    volume_expansion       NUMERIC,
    pts_confirmation       BOOLEAN,
    confirmation_status    TEXT,            -- NULL（未確認）|CONFIRMED|PARTIAL|FADED|INVALIDATED
    confirmed_at           TIMESTAMPTZ,
    confirmation_detail_json JSONB,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, theme, generated_date)
);
CREATE INDEX IF NOT EXISTS idx_next_day_theme_candidates_user_date ON next_day_theme_candidates(user_id, generated_date DESC);
"""


def upsert_limit_up_event(database_url, user_id, event_date, symbol, fields):
    """指示書26番：limit_up_events。冪等（UNIQUE(user_id,event_date,symbol)）。
    Phase MU-S3B：limit_up_eventsはSHARED化済み（市場の事実のみ、個人の売買情報は含まない）。"""
    user_id = _SHARED_SCOPE
    pool = _get_pool(database_url)
    if pool is None or not symbol:
        return None
    cols = ["stock_name", "pct", "volume", "volume_ratio", "reason", "catalyst_type", "theme",
            "sector", "pts_change", "next_day_gap", "next_day_high", "next_day_close"]
    present = [c for c in cols if c in (fields or {})]
    if not present:
        return None
    values = [fields.get(c) for c in present]
    insert_placeholders = ["%s"] * len(present)
    update_clauses = [f"{c}=EXCLUDED.{c}" for c in present]
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"INSERT INTO limit_up_events (user_id, event_date, symbol, {', '.join(present)}) "
                f"VALUES (%s, %s, %s, {', '.join(insert_placeholders)}) "
                f"ON CONFLICT (user_id, event_date, symbol) DO UPDATE SET {', '.join(update_clauses)} "
                f"RETURNING *", [user_id, event_date, symbol] + values)
            row = cur.fetchone()
        conn.commit()
    return _row_to_json(row) if row else None


def list_limit_up_events(database_url, user_id, event_date=None, limit=200):
    # Phase MU-S3B：limit_up_eventsはSHARED化済み。
    user_id = _SHARED_SCOPE
    pool = _get_pool(database_url)
    if pool is None:
        return []
    where, params = ["user_id=%s"], [user_id]
    if event_date:
        where.append("event_date=%s")
        params.append(event_date)
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(f"SELECT * FROM limit_up_events WHERE {' AND '.join(where)} "
                        f"ORDER BY event_date DESC, pct DESC LIMIT %s", params + [limit])
            rows = cur.fetchall()
    return [_row_to_json(r) for r in rows]


def upsert_next_day_theme_candidate(database_url, user_id, theme, generated_date, fields):
    """指示書3・8番：next_day_theme_candidates。冪等（UNIQUE(user_id,theme,generated_date)）。
    Phase MU-S3B：next_day_theme_candidatesはSHARED化済み（「市場として明日注目されるテーマ
    候補」のみを保存し、個人の余力・ポジションは含めない）。"""
    user_id = _SHARED_SCOPE
    pool = _get_pool(database_url)
    if pool is None or not theme:
        return None
    cols = ["theme_score", "stage", "trigger_stocks_json", "related_stocks_json", "catalyst",
            "breadth", "volume_expansion", "pts_confirmation", "confirmation_status",
            "confirmed_at", "confirmation_detail_json"]
    json_cols = {"trigger_stocks_json", "related_stocks_json", "confirmation_detail_json"}
    present = [c for c in cols if c in (fields or {})]
    if not present:
        return get_next_day_theme_candidate(database_url, user_id, theme, generated_date)
    values = [fields.get(c) for c in present]
    wrapped = [json.dumps(v, ensure_ascii=False) if (c in json_cols and v is not None) else v
               for c, v in zip(present, values)]
    insert_placeholders = ["%s::jsonb" if c in json_cols else "%s" for c in present]
    update_clauses = [f"{c}=EXCLUDED.{c}" for c in present] + ["updated_at=now()"]
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"INSERT INTO next_day_theme_candidates (user_id, theme, generated_date, {', '.join(present)}) "
                f"VALUES (%s, %s, %s, {', '.join(insert_placeholders)}) "
                f"ON CONFLICT (user_id, theme, generated_date) DO UPDATE SET {', '.join(update_clauses)} "
                f"RETURNING *", [user_id, theme, generated_date] + wrapped)
            row = cur.fetchone()
        conn.commit()
    return _row_to_json(row) if row else None


def get_next_day_theme_candidate(database_url, user_id, theme, generated_date):
    # Phase MU-S3B：next_day_theme_candidatesはSHARED化済み。
    user_id = _SHARED_SCOPE
    pool = _get_pool(database_url)
    if pool is None:
        return None
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM next_day_theme_candidates WHERE user_id=%s AND theme=%s AND generated_date=%s",
                        [user_id, theme, generated_date])
            row = cur.fetchone()
    return _row_to_json(row) if row else None


def list_next_day_theme_candidates(database_url, user_id, generated_date=None, limit=50):
    # Phase MU-S3B：next_day_theme_candidatesはSHARED化済み。
    user_id = _SHARED_SCOPE
    pool = _get_pool(database_url)
    if pool is None:
        return []
    where, params = ["user_id=%s"], [user_id]
    if generated_date:
        where.append("generated_date=%s")
        params.append(generated_date)
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(f"SELECT * FROM next_day_theme_candidates WHERE {' AND '.join(where)} "
                        f"ORDER BY generated_date DESC, theme_score DESC NULLS LAST LIMIT %s", params + [limit])
            rows = cur.fetchall()
    return [_row_to_json(r) for r in rows]


def upsert_theme_momentum_history(database_url, user_id, theme, event_date, fields):
    """指示書27・28番：theme_momentum_history。冪等（UNIQUE(user_id,theme,event_date)）。
    Phase MU-S3B：theme_momentum_historyはSHARED化済み（テーマ強度・資金流入・構成銘柄等の
    市場データのみ、個人の売買有無は含まない）。"""
    user_id = _SHARED_SCOPE
    pool = _get_pool(database_url)
    if pool is None or not theme:
        return None
    cols = ["stage", "theme_score", "breadth", "leader_count", "related_movers",
            "next_day_confirmation", "day2_performance", "day3_performance"]
    present = [c for c in cols if c in (fields or {})]
    if not present:
        return None
    values = [fields.get(c) for c in present]
    insert_placeholders = ["%s"] * len(present)
    update_clauses = [f"{c}=EXCLUDED.{c}" for c in present]
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"INSERT INTO theme_momentum_history (user_id, theme, event_date, {', '.join(present)}) "
                f"VALUES (%s, %s, %s, {', '.join(insert_placeholders)}) "
                f"ON CONFLICT (user_id, theme, event_date) DO UPDATE SET {', '.join(update_clauses)} "
                f"RETURNING *", [user_id, theme, event_date] + values)
            row = cur.fetchone()
        conn.commit()
    return _row_to_json(row) if row else None


def list_theme_momentum_history(database_url, user_id, theme=None, limit=100):
    # Phase MU-S3B：theme_momentum_historyはSHARED化済み。
    user_id = _SHARED_SCOPE
    pool = _get_pool(database_url)
    if pool is None:
        return []
    where, params = ["user_id=%s"], [user_id]
    if theme:
        where.append("theme=%s")
        params.append(theme)
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(f"SELECT * FROM theme_momentum_history WHERE {' AND '.join(where)} "
                        f"ORDER BY event_date DESC LIMIT %s", params + [limit])
            rows = cur.fetchall()
    return [_row_to_json(r) for r in rows]


def count_underlying_events_total(database_url):
    """指示書26・41番：累計underlying_events件数（Phase13検討前サンプル目標との比較用）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return 0
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM underlying_events")
            return cur.fetchone()[0]


def count_entry_candidate_snapshots_total(database_url, user_id, candidate_type=None):
    """指示書26・41番：累計candidate snapshot件数（type別も可）。
    Phase MU-S3B：entry_candidate_snapshotsはSHARED化済み。"""
    user_id = _SHARED_SCOPE
    pool = _get_pool(database_url)
    if pool is None:
        return 0
    where = ["user_id = %s"]
    params = [user_id]
    if candidate_type:
        where.append("candidate_type = %s")
        params.append(candidate_type)
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM entry_candidate_snapshots WHERE {' AND '.join(where)}", params)
            return cur.fetchone()[0]


def count_validation_sessions(database_url):
    """指示書15・27番：Shadow modeで起動した日数（validation_sessionsは1日1行、
    session_dateにUNIQUE制約があるため件数=稼働日数の近似値）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return 0
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM validation_sessions")
            return cur.fetchone()[0]


def count_entry_candidate_snapshots_evaluated_total(database_url, user_id):
    """指示書26・41番：累計evaluated outcome件数（outcome_status確定済み）。
    Phase MU-S3B：entry_candidate_snapshotsはSHARED化済み。"""
    user_id = _SHARED_SCOPE
    pool = _get_pool(database_url)
    if pool is None:
        return 0
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM entry_candidate_snapshots WHERE user_id=%s AND outcome_status IS NOT NULL",
                [user_id])
            return cur.fetchone()[0]


def get_or_create_validation_session(database_url, session_date, app_version=None, commit_hash=None,
                                        config_version=None, validation_version=None, mode=None, environment=None):
    """指示書2・3・24・25番：日ごとのvalidation_session（app_version・commit_hash・
    config_version・validation_version・mode・environment）。既にあればそのまま返す
    （同じロジック版での結果比較のため上書きしない——Phase12期間中は固定、指示書1・2番）。"""
    pool = _get_pool(database_url)
    if pool is None:
        return None
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "INSERT INTO validation_sessions (session_date, app_version, commit_hash, config_version, "
                "validation_version, mode, environment, started_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, now()) "
                "ON CONFLICT (session_date) DO UPDATE SET session_date=EXCLUDED.session_date "
                "RETURNING *", [session_date, app_version, commit_hash, config_version, validation_version,
                                  mode, environment])
            row = cur.fetchone()
        conn.commit()
    return _row_to_json(row) if row else None


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
    JP/USで絞り込む。フロントのwatchlist配列とほぼ同じ形（tvSymbol等camelCase）で返す。
    Phase MU-S1：watchlistはSHARED化済みのため、呼び出し元のuser_idは無視し全員共通のscopeを見る。"""
    user_id = _SHARED_SCOPE
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
    いずれかを含むdict（code必須、marketは省略時JP）。既存なら更新、無ければ新規作成。
    Phase MU-S1：watchlistはSHARED化済み。"""
    user_id = _SHARED_SCOPE
    pool = _get_pool(database_url)
    if pool is None:
        return False
    with pool.connection() as conn:
        ok = _upsert_watchlist_item_conn(conn, user_id, item)
        conn.commit()
    return ok


def upsert_watchlist_master_stocks(database_url, user_id, stocks, update_mode="add", source="smart_import_master"):
    """Smart Import『監視銘柄更新』（type: watchlist_master_update）専用の一括upsert。
    stocksは[{"code":str,"name":str|None}, ...]。codeは"417A"/"593A"のような英字入りの
    銘柄コードも文字列としてそのまま保持し、数値変換は一切行わない。
    update_mode:
      "add"  … stocksに含まれる銘柄をadd/updateするのみ（他の既存銘柄には触れない）
      "sync" … 上記に加え、マスターに存在しなくなった既存銘柄（同一user_id・market='JP'）を
               即削除せずinactive_candidate=trueにする（指示書：即削除しない）
    戻り値：{"added":N,"updated":N,"invalid":N,"inactive_candidates":N}。
    Phase MU-S1：watchlistはSHARED化済み。"""
    user_id = _SHARED_SCOPE
    pool = _get_pool(database_url)
    if pool is None:
        return {"added": 0, "updated": 0, "invalid": 0, "inactive_candidates": 0}
    valid, seen_codes, invalid = [], set(), 0
    for s in stocks or []:
        code = str((s or {}).get("code") or "").strip()
        if not code:
            invalid += 1
            continue
        if code in seen_codes:
            continue  # 同一バッチ内の重複は先勝ち・invalidにはしない
        seen_codes.add(code)
        valid.append({"code": code, "name": (s or {}).get("name")})
    added = updated = inactive_marked = 0
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT code FROM watchlist WHERE user_id=%s AND market='JP'", [user_id])
            existing_codes = {r["code"] for r in cur.fetchall()}
        for item in valid:
            is_new = item["code"] not in existing_codes
            cols, values = ["source"], [source]
            if item.get("name"):
                cols.append("name")
                values.append(item["name"])
            placeholders = ", ".join(["%s"] * len(cols))
            conn.execute(
                f"INSERT INTO watchlist (user_id, code, market, {', '.join(cols)}, inactive_candidate) "
                f"VALUES (%s, %s, 'JP', {placeholders}, false) "
                f"ON CONFLICT (user_id, code, market) DO UPDATE SET "
                f"{', '.join(c + '=EXCLUDED.' + c for c in cols)}, active=true, inactive_candidate=false, "
                f"updated_at=now()",
                [user_id, item["code"]] + values)
            if is_new:
                added += 1
            else:
                updated += 1
        if update_mode == "sync":
            stale_codes = list(existing_codes - {item["code"] for item in valid})
            if stale_codes:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE watchlist SET inactive_candidate=true, updated_at=now() "
                        "WHERE user_id=%s AND market='JP' AND code = ANY(%s)",
                        [user_id, stale_codes])
                    inactive_marked = cur.rowcount
        conn.commit()
    return {"added": added, "updated": updated, "invalid": invalid, "inactive_candidates": inactive_marked}


# v3-9（監視銘柄自動登録エンジン）：手動登録との事故防止のため、auto_tags/manual_registeredは
# _WATCHLIST_COLS（クライアントの汎用保存エンドポイントの書き込み許可列）に含めず、以下の専用
# 関数だけが触る。upsert_watchlist_item()側の一般的な部分更新の仕組みとは完全に独立させている。
def auto_register_or_tag_watchlist_item(database_url, user_id, code, market, reason_key, tag_value, item_fields=None):
    """自動登録エンジン専用。銘柄が未登録なら manual_registered=false で新規作成し、既に存在する
    銘柄（手動登録・他の自動登録いずれでも）なら auto_tags の reason_key キーだけをマージする
    （他のキー・manual_registered・name/sector等の既存値には一切触れない）。
    tag_value例: {"score": 82, "addedAt": "...", "expiresAt": "..."}
    item_fields: 新規作成時のみ使うname/sector/kana等の初期値（dict、省略可）。既存行の更新には使わない。
    戻り値: True=成功。
    Phase MU-S1：watchlistはSHARED化済み。"""
    user_id = _SHARED_SCOPE
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
    既存値には一切触れない）。行が無い、またはそのキーを持たない場合は何もしない。
    Phase MU-S1：watchlistはSHARED化済み。"""
    user_id = _SHARED_SCOPE
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
    「前回CURRENTだったが今回は外れた」銘柄を特定するために使う。
    Phase MU-S1：watchlistはSHARED化済み。"""
    user_id = _SHARED_SCOPE
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
    軽量に毎回呼べるよう、対象行が無ければ何もしない設計。
    Phase MU-S1：watchlistはSHARED化済みだがportfolio（保有ポジション）はPRIVATEのまま
    （個人のuser_idで判定する）ため、この関数だけはテーブルごとにscopeを使い分ける。"""
    pool = _get_pool(database_url)
    if pool is None:
        return {"expired": 0, "deleted": 0}
    shared_id = _SHARED_SCOPE
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    expired_count = 0
    deleted_count = 0
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, code, market, manual_registered, auto_tags FROM watchlist "
                "WHERE user_id = %s AND auto_tags IS NOT NULL AND auto_tags != '{}'::jsonb",
                [shared_id],
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
    まま記録する（2026-09-05ユーザー判断：事後補完APIは見送り）。戻り値: True=成功。
    Phase MU-S3B：auto_signal_eventsはSHARED化済み（自動登録エンジンの市場シグナル遷移のみ、
    MY_STOP_HIT等の個人イベントは記録しない設計）。"""
    user_id = _SHARED_SCOPE
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
    """auto_signal_eventsの履歴を新しい順に返す（検証・確認用）。codeやsignal_typeで絞り込み可能。
    Phase MU-S3B：auto_signal_eventsはSHARED化済み。"""
    user_id = _SHARED_SCOPE
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
                       "source", "source_type", "verification_status", "notes", "raw_payload",
                       "source_handle", "source_post_id", "source_post_url", "source_published_at",
                       "canonical_event_key", "event_time_jst", "time_precision", "timezone_source"]
# 金融政策イベント統合（2026-09-16新規）：CATALYST→EVENT同期（server.py
# promote_central_bank_catalysts_to_market_events）専用の追加列。
#   canonical_event_key … "FOMC_POLICY_DECISION"等、表記揺れに依らない実イベント識別子
#     （同一イベントが複数カタリストとして別titleで取り込まれても、この値で同一性判定する）。
#   event_time_jst       … JSTへ正規化した時刻（"03:00"等）。既存event_time（原タイムゾーンでの
#     時刻）・timezone（原タイムゾーン名、例"America/New_York"）とは役割を分ける。
#   time_precision        … "EXACT"|"APPROXIMATE"|"DATE_ONLY"。時刻が不明・曖昧な場合は
#     推測せずAPPROXIMATE/DATE_ONLYのままにする（time_to_event等の精密計算に誤用させない）。
#   timezone_source        … event_time_jstの信頼度provenance（例："SMART_IMPORT_ASSUMED_JST"＝
#     Smart Import由来の値が既にJSTである一貫した経験則はあるが、仕様として保証されたものではない
#     ことを明示するラベル）。
_MARKET_EVENT_CENTRAL_BANK_SYNC_COLS = ("canonical_event_key", "event_time_jst", "time_precision", "timezone_source")
_MARKET_EVENT_JSONB_COLS = {"affected_markets", "affected_sectors", "affected_stocks", "impact_channels", "raw_payload"}
# X Intelligence Phase3（2026-09-15新規）：X由来イベントのsource追跡専用列。既存の
# source/source_type（自由記述、"IMAGE|TEXT|MANUAL等"）とは別に、構造化されたX投稿の
# 出典を追跡する。全てNULL許容——X由来でない既存イベント・一般テキストSmart Import・
# 手動イベント登録には一切影響しない（指示書4・5番）。
_MARKET_EVENT_SOURCE_TRACKING_COLS = ("source_handle", "source_post_id", "source_post_url", "source_published_at")
_MIGRATE_MARKET_EVENT_SOURCE_TRACKING_SQL = """
ALTER TABLE market_events ADD COLUMN IF NOT EXISTS source_handle TEXT;
ALTER TABLE market_events ADD COLUMN IF NOT EXISTS source_post_id TEXT;
ALTER TABLE market_events ADD COLUMN IF NOT EXISTS source_post_url TEXT;
ALTER TABLE market_events ADD COLUMN IF NOT EXISTS source_published_at TIMESTAMPTZ;
"""

# 金融政策イベント統合（2026-09-16新規、CATALYST→EVENT同期）：全てNULL許容——このsync機能を
# 使わない既存イベント（手動登録・social由来等）には一切影響しない（既存の他マイグレーションと
# 同じ後方互換方針）。
_MIGRATE_CENTRAL_BANK_EVENT_SYNC_SQL = """
ALTER TABLE market_events ADD COLUMN IF NOT EXISTS canonical_event_key TEXT;
ALTER TABLE market_events ADD COLUMN IF NOT EXISTS event_time_jst TEXT;
ALTER TABLE market_events ADD COLUMN IF NOT EXISTS time_precision TEXT;
ALTER TABLE market_events ADD COLUMN IF NOT EXISTS timezone_source TEXT;
CREATE INDEX IF NOT EXISTS idx_market_events_canonical_key
    ON market_events(user_id, canonical_event_key, event_date) WHERE canonical_event_key IS NOT NULL;
"""


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

    # X Intelligence Phase3（2026-09-15新規）：X由来イベント（_detect_events_from_social_text/
    # _normalize_image_economic_eventsが生成するraw_payload={"source_handle":...,
    # "source_post_id":...,"source_post_url":...,"published_at":...}）から、専用列へ
    # 自動的に昇格させる。既にトップレベルキーで明示されていればそちらを優先し上書きしない
    # （標準形式のキーが既にある場合は変えない、という既存方針を踏襲）。X由来でない
    # 既存イベント・一般テキストSmart Import・手動イベント登録のraw_payloadにはこれらの
    # キーが無いため、一切影響しない（列はNULLのまま）。
    raw_payload_in = ev.get("raw_payload") if isinstance(ev.get("raw_payload"), dict) else {}
    if not ev.get("source_handle") and raw_payload_in.get("source_handle"):
        ev["source_handle"] = raw_payload_in["source_handle"]
    if not ev.get("source_post_id") and raw_payload_in.get("source_post_id"):
        ev["source_post_id"] = raw_payload_in["source_post_id"]
    if not ev.get("source_post_url") and raw_payload_in.get("source_post_url"):
        ev["source_post_url"] = raw_payload_in["source_post_url"]
    if not ev.get("source_published_at") and raw_payload_in.get("published_at"):
        ev["source_published_at"] = raw_payload_in["published_at"]

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
    if not ev.get("importance"):
        # 2026-09-14追加：スキーマ上はimportance TEXT DEFAULT 'MEDIUM'だが、本番テーブルは
        # このDEFAULT定義より前から存在しており、INSERT文からimportance列自体を省略した
        # 場合にDEFAULTが適用されずNULLのまま入る実態が確認された（イベント画面の
        # HIGH+MEDIUMフィルタで何も表示されない一因）。DBのDEFAULTに依存せず、
        # ここで明示的にMEDIUMへフォールバックする（値の無いイベントを重要度不明のまま
        # 埋もれさせない、既存の列DEFAULT意図と同じ既定値）。
        ev["importance"] = "MEDIUM"

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


def _merge_market_event_additional_sources(existing_raw_payload, incoming_source):
    """X Intelligence Phase3（2026-09-15新規）：同一イベント（同一user_id・event_date・title）
    を別のX投稿source（別アカウントの別post_id）が指しているとき、専用列（source_handle等）
    は「最初に確定した1件」を保持したまま上書きしない（指示書6番「既存dedupeロジックとの
    競合を確認する」＝自然キーUNIQUE(user_id,event_date,title)による重複防止と、複数source
    保持は両立させる必要がある）。2件目以降のsourceはraw_payload.additional_sourcesへ
    追記し、情報を失わない。同一source_post_idの再取り込み（同じ投稿の再Smart Import）は
    重複追加しない。戻り値：マージ後のraw_payload dict。"""
    payload = dict(existing_raw_payload or {})
    additional = list(payload.get("additional_sources") or [])
    incoming_post_id = incoming_source.get("source_post_id")
    if incoming_post_id and any(a.get("source_post_id") == incoming_post_id for a in additional):
        return payload  # 同一投稿の再取り込み：重複追加しない
    additional.append(incoming_source)
    payload["additional_sources"] = additional
    return payload


def _upsert_market_event_conn(conn, user_id, ev):
    """正規化済み（_normalize_market_event適用後）のイベント1件をupsertする。
    event_date・titleが無い場合はNoneを返す（呼び出し側でスキップ扱い）。
    verification_statusは未指定ならUNVERIFIED（画像由来等を確定情報として扱わない、
    既定の安全側）。戻り値：新規作成ならTrue、既存行の更新ならFalse、保存不可ならNone。
    2026-09-07追加（STEP4：新規/更新の件数を分けて報告できるようにする）：
    `RETURNING (xmax = 0) AS is_insert`は、そのUPSERTが実際にINSERTだったか
    （ON CONFLICTでのUPDATEではなかったか）をPostgreSQL内部列xmaxから判定する定石。
    2026-09-15更新（X Intelligence Phase3）：source_handle/source_post_id/source_post_url/
    source_published_atは、既存行に既に値がある場合はEXCLUDEDで上書きしない
    （COALESCE、最初に確定したsourceを保持）。既存行のsource_post_idと今回のsource_post_id
    が両方あり異なる場合だけ、raw_payload.additional_sourcesへ2件目以降のsourceとして
    追記する（指示書「同一イベントとして関連付けつつsourceは複数保持可能」）。
    X由来でない既存イベント（source_post_idが無い）は、この特別扱いに一切該当せず、
    従来通りraw_payload等がEXCLUDEDで単純上書きされる（後方互換）。"""
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

    incoming_post_id = ev.get("source_post_id")
    with conn.cursor(row_factory=dict_row) as cur:
        # source_post_idを持つ（＝X由来の）イベントだけ、複数source統合のため既存行を
        # 事前にロックして読む（FOR UPDATEで同時実行時の競合を防ぐ）。X由来でないイベントは
        # 従来通り単純なINSERT ... ON CONFLICT DO UPDATEのみで済ませ、余計なSELECTを増やさない
        # （既存の挙動・性能特性を変えない）。
        existing = None
        if incoming_post_id:
            cur.execute(
                "SELECT source_handle, source_post_id, source_post_url, source_type, "
                "source_published_at, raw_payload FROM market_events "
                "WHERE user_id=%s AND event_date=%s AND title=%s FOR UPDATE",
                [user_id, event_date, title])
            existing = cur.fetchone()

        # "source"は旧来の自由記述列（例："x:nicosokufx"）で、専用列source_handleと同じ
        # 「主たる発信者」を指す。複数source統合時にsource_handleだけ既存を維持してsourceが
        # 新しいEXCLUDEDへ上書きされると、両者が食い違って見える（実データE2Eで発見）ため
        # 一緒に既存維持する。
        _first_source_wins_cols = _MARKET_EVENT_SOURCE_TRACKING_COLS + ("source_type", "source")
        set_clauses = [f"{c} = EXCLUDED.{c}" for c in update_cols if c not in _first_source_wins_cols and c != "raw_payload"]
        params_extra = []
        if existing and existing.get("source_post_id") and existing["source_post_id"] != incoming_post_id:
            # 既に別sourceが確定済み：専用列（source_type含む）は既存を維持し、raw_payloadへ
            # 今回のsourceを追記する。
            for c in _first_source_wins_cols:
                if c in update_cols:
                    set_clauses.append(f"{c} = market_events.{c}")
            if "raw_payload" in update_cols:
                merged_payload = _merge_market_event_additional_sources(
                    existing.get("raw_payload"),
                    {"source_handle": ev.get("source_handle"), "source_post_id": ev.get("source_post_id"),
                     "source_post_url": ev.get("source_post_url"), "source_type": ev.get("source_type"),
                     "source_published_at": ev.get("source_published_at")})
                set_clauses.append("raw_payload = %s::jsonb")
                params_extra.append(json.dumps(merged_payload, ensure_ascii=False))
        else:
            # 初回、またはX由来でない、または同一投稿の再取り込み：従来通りEXCLUDEDで上書き。
            for c in _first_source_wins_cols:
                if c in update_cols:
                    set_clauses.append(f"{c} = EXCLUDED.{c}")
            if "raw_payload" in update_cols:
                set_clauses.append("raw_payload = EXCLUDED.raw_payload")

        cur.execute(
            f"INSERT INTO market_events (user_id, {', '.join(cols)}) "
            f"VALUES (%s, {', '.join(['%s::jsonb' if c in _MARKET_EVENT_JSONB_COLS else '%s' for c in cols])}) "
            f"ON CONFLICT (user_id, event_date, title) DO UPDATE SET "
            f"{', '.join(set_clauses)}, updated_at = now() "
            f"RETURNING (xmax = 0) AS is_insert",
            [user_id] + values + params_extra,
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
    1件ずつtry/exceptする。
    Phase MU-S1：market_eventsはSHARED化済み。"""
    user_id = _SHARED_SCOPE
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


def update_market_event_central_bank_sync(database_url, user_id, event_id, canonical_event_key,
                                            event_time_jst, time_precision, timezone_source,
                                            provenance_update, linked_catalyst_ids):
    """金融政策イベント統合（2026-09-16新規）：CATALYST→EVENT同期で「同一実イベントが既に
    market_eventsに存在する」と判定した場合に使う部分更新専用関数。title/event_date/
    event_type等の既存表示用フィールドは変更しない——手動登録・social由来を問わず既存行の
    見た目を壊さず、canonical_event_key等のメタデータだけを後付けする（指示書「既存のOTHER行
    へ新規CENTRAL_BANK行を追加して二重化しない、link/updateする」）。
    raw_payload.linked_catalyst_idsは既存値とunion（重複排除・昇順）してから書き込む——1つの
    canonical eventに複数catalystが紐づく構造を維持する（source_catalyst_idを単数前提にしない）。
    戻り値：対象行があり更新できればTrue、無ければFalse。"""
    # Phase MU-S1：market_eventsはSHARED化済み（import_market_events/list_market_events等と
    # 同じ扱い）。ここを忘れるとuser_id不一致でSELECTが0件になり、常にFalseを返してしまう。
    user_id = _SHARED_SCOPE
    pool = _get_pool(database_url)
    if pool is None:
        return False
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT raw_payload FROM market_events WHERE user_id=%s AND id=%s FOR UPDATE",
                        [user_id, event_id])
            row = cur.fetchone()
            if row is None:
                return False
            raw_payload = dict(row.get("raw_payload") or {})
            existing_ids = raw_payload.get("linked_catalyst_ids") or []
            raw_payload["linked_catalyst_ids"] = sorted(set(existing_ids) | set(linked_catalyst_ids or []))
            raw_payload.update(provenance_update or {})
            cur.execute(
                "UPDATE market_events SET canonical_event_key=%s, event_time_jst=%s, "
                "time_precision=%s, timezone_source=%s, raw_payload=%s::jsonb, updated_at=now() "
                "WHERE user_id=%s AND id=%s",
                [canonical_event_key, event_time_jst, time_precision, timezone_source,
                 json.dumps(raw_payload, ensure_ascii=False), user_id, event_id])
        conn.commit()
    return True


def list_market_events(database_url, user_id, from_date=None, to_date=None, limit=200):
    """イベントをevent_date昇順で返す。from_date/to_dateはISO日付文字列（両端含む）。
    省略時は全期間（limit件まで）。Phase MU-S1：market_eventsはSHARED化済み。"""
    user_id = _SHARED_SCOPE
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
    # Phase MU-S1：market_eventsはSHARED化済み。
    user_id = _SHARED_SCOPE
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
    戻り値: {"imported": N, "skipped": M}（catalyst_date/titleが無い行はskip）。
    Phase MU-S1：news_catalystsはSHARED化済み。"""
    user_id = _SHARED_SCOPE
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
    """カタリストをcatalyst_date降順（新しいもの優先）で返す。from_date/to_dateは
    catalyst_dateへのISO日付フィルタ（両端含む）。categoryで絞り込み可能。
    Phase MU-S1：news_catalystsはSHARED化済み。"""
    user_id = _SHARED_SCOPE
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
    # Phase MU-S1：news_catalystsはSHARED化済み。
    user_id = _SHARED_SCOPE
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
    戻り値: {"imported": N, "skipped": M}（expert_name/published_atが無い行はskip）。
    Phase MU-S1：expert_viewsはSHARED化済み。"""
    user_id = _SHARED_SCOPE
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
    """有識者見解をpublished_at降順（新しいもの優先）で返す。expert=有識者名、
    from_date/to_date=published_atのISO日付フィルタ（両端含む）、market=市場、
    stock=stocks配列に含まれる銘柄コードで絞り込み可能。Phase MU-S1：expert_viewsはSHARED化済み。"""
    user_id = _SHARED_SCOPE
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
    # Phase MU-S1：expert_viewsはSHARED化済み。
    user_id = _SHARED_SCOPE
    pool = _get_pool(database_url)
    if pool is None:
        return
    with pool.connection() as conn:
        conn.execute("DELETE FROM expert_views WHERE user_id = %s AND id = %s", [user_id, view_id])
        conn.commit()


def delete_watchlist_item(database_url, user_id, code, market=None):
    # Phase MU-S1：watchlistはSHARED化済み。
    user_id = _SHARED_SCOPE
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
    コストが件数分かかる）ため、1本の接続を使い回して処理する。
    Phase MU-S1：watchlistはSHARED化済み。"""
    user_id = _SHARED_SCOPE
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
                   "trade_style", "stop_reason_category", "stop_reason_text"]
                   # marketはINSERT文で別途固定列として扱うためここには含めない


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
        # Trade Learning Phase B：決済直前にportfolioから取得済みのSTOP値をtrade_historyへ引き継ぐ
        # （従来はここで捨てられていた＝6227実例で判明したデータ欠損）。過去トレードは埋め戻さない
        # 方針のため、ここではrowに実際に値がある場合のみ引き継ぎ、無ければNULL（UNKNOWN扱い）のまま。
        initial_stop_price = row.get("initial_stop")
        final_stop_price = row.get("current_stop")
        stop_reason_category = row.get("stop_reason_category")
        stop_reason_text = row.get("stop_reason_text")
        stop_quality_evidence = "ACTUAL_STOP" if initial_stop_price is not None else "UNKNOWN"
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "INSERT INTO trade_history (user_id, code, name, market, entry_price, exit_price, shares, "
                "pnl, gross_pnl, tax, net_pnl, acquired_at, trade_style, initial_stop_price, final_stop_price, "
                "stop_reason_category, stop_reason_text, stop_quality_evidence) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
                [user_id, code, row.get("name"), market, avg_price, exit_price, shares, pnl, pnl, tax, net_pnl,
                 row.get("acquired_at"), row.get("trade_style"), initial_stop_price, final_stop_price,
                 stop_reason_category, stop_reason_text, stop_quality_evidence],
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
