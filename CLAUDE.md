# CLAUDE.md

このファイルは、このリポジトリで作業する Claude Code（および開発者）への引き継ぎメモです。

## プロジェクト概要

**トレード・コックピット（サーバー版・Windows）** — 株価・指数・為替・ニュースを、
画面の「リアルタイムデータを反映」ボタンで都度取得して表示する個人用ダッシュボード。
ローカルで小さな Python サーバーを立て、ブラウザから使う。

判断材料の整理用ツールであり、売買推奨・発注機能はない（板・約定・発注は将来の
立花証券API連携の担当範囲）。

## 実行方法

すべて `files/` 内で完結する。

- **初回準備:** `setup.bat` をダブルクリック → `yfinance` と `feedparser` を pip install。
- **毎回の起動:** `start.bat` をダブルクリック → `python server.py` が起動し、
  1.5秒後にブラウザで `http://localhost:8765/trade-cockpit.html` を自動で開く。
- **停止:** 黒いコンソールウィンドウを閉じる（サーバーが止まる）。使用中は閉じない。
- Python は PATH 通し済み前提（`python` コマンドで起動）。

## アーキテクチャ

```
ブラウザ (trade-cockpit.html)  ──GET /api/quotes──▶  server.py (yfinance)
   React 18 + Babel standalone   ──POST /api/news──▶  server.py (Googleニュース RSS)
   localStorage: "trade-cockpit-v1"
```

- **files/server.py** — `http.server` ベースの自作サーバー。ポート **8765** 固定。
  - `GET /api/quotes` … 指数・為替・コモディティの「今日値(t)/前日値(p)」を返す。
    銘柄マップは `INDEX`（usdjpy=JPY=X, nikkei=^N225, dow=^DJI, nasdaq=^IXIC,
    sox=^SOX, us10y=^TNX, sp500=^GSPC, kospi=^KS11, nikkei_fut=NIY=F,
    dow_fut=YM=F, wti=CL=F, gold=GC=F, copper=HG=F）。
    `_two_closes()` が直近7日分から終値2つを取る。
  - `POST /api/stock-quotes` … body の watchlist（登録銘柄）を受け、銘柄ごとの
    現在値(t)/前日終値(p)/当日高値(high)/当日安値(low) を返す。シンボル解決は
    `_yf_symbol()`（JP: code+".T"、US: codeそのまま、IDX: `IDX_YF_OVERRIDE` で上書き）。
  - `POST /api/news` … body の watchlist を受け、登録銘柄ニュース＋マクロニュースを返す。
    優先/通常/様子見でソートし上位12件。マクロは固定クエリ5種。
  - CORS 全許可。静的ファイル（HTML）も同サーバーが配信。
- **files/trade-cockpit.html** — 本体（約500行）。CDN の React/ReactDOM/Babel を
  `type="text/babel"` でその場コンパイル。ビルド工程なし。状態は localStorage キー
  `trade-cockpit-v1` に自動保存。フロントは相対パス `/api/quotes`・`/api/news` を叩く。
- **files/cockpit-data.js** — `window.PYDATA` にデータをベタ書きした静的スナップショット
  （旧 file:// 方式の名残。サーバー版では未使用に近い）。

## 重要な制約・注意

- 数値は**遅延あり**（前営業日終値〜数十分遅れ）。リアルタイムの板ではない。
- 市場休場日はデータが出ないことがある。
- **上昇/下落の配色は日本の相場慣習（赤=上昇・緑=下落）で統一。** CSS変数 `--up`/`--down`/`--upBg`/`--downBg`
  を使用（日次モニターの指標カード・登録銘柄テーブルの両方）。既存の `--gain`(緑)/`--loss`(赤) は
  買いサイン/売りサイン等の別の意味で使っているため、意味的に独立させてある。混同しないこと。
- **ポートは 8765 固定。** 使用中エラー時は `server.py` の `PORT` と HTML 側の案内文言を合わせて変更する。
  なおポート使用中（前回サーバー閉じ忘れ等）の場合、`server.py` は案内を出して正常終了する。
- **チャート（TradingView 無料埋め込み）で表示できるシンボルには制限がある。** 実測（2026-07）で確認：
  - 表示可: 指数は再配信可能なもの（日経=`INDEX:NKY`）／米国株（`NASDAQ:`・`NYSE:` 遅延）／為替（`FX:USDJPY`）／
    OANDA・CAPITALCOM 等の CFD。SOX/VIX は連動ETF（`NASDAQ:SOXX`・`BATS:VIXY`）で代用。
  - 表示**不可**（「このシンボルは TradingView 上でのみ利用可能」）: `TVC:` 系指数（`TVC:NI225` 等）、
    `NASDAQ:SOX`・`SP:SPX`・`CBOE:VIX`、そして **日本の個別株 `TSE:xxxx` 全般**（東証データの再配信制限）。
  - HTML 側は `embeddable()`（`TSE:` を弾く）で判定し、日本株は埋め込みの代わりに
    「↗ TradingViewで開く」パネルを表示する。分岐 div には `key` を付けること（付けないと
    命令的注入した旧ウィジェットの iframe が React 管理外で残る）。
- file:// で開いた旧版の保存データは localhost 版へ自動移行されない。
  画面の「バックアップ↓」でJSON保存 →「復元↑」で読み込む手動移行が必要。

## 撤去済み機能（履歴・現行では非対応）

以下はかつて存在したが**コードから撤去済み**。現行の機能・制約ではない。
再要望が来たら「新規実装」として扱う（既存機能の復旧ではない）。

- **Fear & Greed（CNN公開データ）の自動取得** — 撤去済み。現行の取得対象は
  `server.py` の `INDEX`（日次モニター用）参照。
- **日経VI の自動取得** — 撤去済み。
- **マクロ買いシグナルのアラート（F&G < 20 かつ 日経VI > 50）** — 撤去済み。
  現行 HTML に残る「底打ちシグナル」等の手動シグナル項目とは別物なので混同しないこと。
- **決算期待値の星の手動クリック評価**（`files/earnings_ratings.json` への保存・
  `/api/earnings-rating` API・`EarningsStars` のクリック編集）— 撤去済み（2026-07）。
  現行は `server.py` の `_auto_earnings_stars()` が過去の上方/下方修正比率・直近決算の良し悪し・
  現在の株価の過熱度（RSI・52週高値/安値からの位置）から自動算出し、`analyze_stock()` の
  戻り値 `autoEarningsStars` としてそのまま表示するのみ（編集不可）。

## レガシー（もう使わない）

README に「もう使いません」と明記。触らない・復活させない。

- `files/run.bat`, `files/fetch_data.py`, `files/cockpit-data.js`（バックアップJSON配置方式）

## 外部依存・データソース

- Python: `yfinance`（指数/為替）, `feedparser`（RSS）
- フロント: React 18.2.0 / ReactDOM 18.2.0 / babel-standalone 7.23.6（すべて cdnjs 経由）
- ニュース: Google ニュース RSS / Fear&Greed: CNN 公開データ

## リポジトリ構成メモ

- ソース一式は `files/` サブフォルダにある（リポジトリルート直下ではない）。
- `.gitignore` で `*.zip`（ソースと重複するアーカイブ）と `.claude/settings.local.json` を除外済み。
- 利用者向け手順書は `files/README.md`。
