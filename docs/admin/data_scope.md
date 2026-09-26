# データ範囲一覧（個人 / 共有）

コード上の台帳は `files/investment_db.py` の `PRIVATE_TABLES` / `SHARED_TABLES` / `MIXED_TABLES`。
`files/test_mu_multi_privacy.py` が「user_id列を持つ全テーブルが分類済み」「個人テーブルを共有固定で扱う関数が無い」ことを毎回検査します
（新しいテーブルを足して分類を忘れるとテストが落ちます）。

## 個人データ（本人以外に返さない。DB・APIの両方で `user_id` により分離）

| 区分 | テーブル |
|---|---|
| ポジション・損益 | `portfolio`（保有・買値・株数・stop）、`portfolio_cash_balance`（買付余力）、`position_risk_rules`、`trade_history`（PTS取引含む）、`trade_candidates` |
| トレード分析・学習 | `trade_experiences`、`trade_outcome_evaluations`、`trade_decision_context`、`trade_decision_events`、`trade_rules`（個人Learning Rule）、`trade_rule_history`、`trade_playbook_user_stats`、`stock_behavior_profiles`、`sector_behavior_profiles`、`choruco_stories`、`analysis_context_log` |
| 反省・振り返り・メモ | `trade_reflections`（← 今回、共有から個人へ修正）、`daily_reviews`（今日の振り返り）、`daily_log`、`journal`、`investment_profile`、`investment_rules`、`news_feedback` |
| 取り込み・個人の追跡状態 | `chatgpt_imports`、`watchlist_imports`、`smart_import_sources`、`shadow_watch`、`ipo_stocks`、`dynamic_watchlist`、`catalyst_snapshot_log`、`chart_signal_log` |
| ポジション由来のアラート | `morning_market_check_private_overlay`（朝一チェックの個人警告） |
| 個人の監視銘柄 | `watchlist`（`user_id`＝本人の行） |

## 共有データ（`_shared` で1回だけ計算・保存。全員が同じものを見る）

`market_events`（イベント）、`expert_views`（有識者見解）、`news_catalysts`、`morning_market_checks`（朝一チェックの共通部分）、
`market_intelligence_reports`（場中4レポート）、`entry_candidate_snapshots`、`auto_signal_events`、`limit_up_events`、`theme_momentum_history`、
`next_day_theme_candidates`、`trade_playbooks`（共通の売買定義。個人の成績は `trade_playbook_user_stats`）、
`market_discovery_pool`（Market Discovery。今回、全員で1回だけ計算するよう変更）、
`watchlist`（`_shared` の行＝システム自動登録・共通分析用）。
ユーザー列を持たないX/ニュース系（`social_market_posts` など）も全員共通です。

## 混合

- `watchlist`：見える範囲は「システム(`_shared`)＋自分の行」。同じ銘柄が両方にあれば自分の設定が優先。
  手動追加・削除・表示対象ON/OFFは本人の行だけを変更し、他の利用者には影響しません。
- `stock_theses`：朝TOP5の仮説（共通）と個人の記録。

## 今回修正した危険箇所

1. `trade_reflections`（反省メモ）が全員共有になっていた → 個人専用へ。既存1件はownerへ移行（バックアップ済み・削除なし）。
2. `watchlist` が全員共有だった → system と user に分離（手動追加は本人だけ）。
3. Basic認証（パスワード平文・ログアウト不可・CORS `*`）→ ログイン画面＋ハッシュ＋Cookieセッション＋CSRF。
4. `market_discovery_pool` をユーザーごとに6回計算していた → `_shared` で1回だけ。

## 既存の共有watchlistの棚卸し結果（2026-09-26時点）

`_shared` 319件はすべて「ownerが手動登録」（`manual_registered=true`、自動登録タグなし）でした。
配布前に owner 個人へ移すことを推奨します（`migrate_watchlist_manual_to_owner.py`、手順は管理者マニュアル1-2）。
