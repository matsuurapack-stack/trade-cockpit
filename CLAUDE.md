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
  - `GET /api/quotes` … 指数・為替の「今日値(t)/前日値(p)」を返す。
    銘柄マップは `INDEX`（usdjpy=JPY=X, nikkei=^N225, dow=^DJI, nasdaq=^IXIC,
    sox=^SOX, us10y=^TNX）。`_two_closes()` が直近7日分から終値2つを取る。
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
- **ポートは 8765 固定。** 使用中エラー時は `server.py` の `PORT` と HTML 側の案内文言を合わせて変更する。
- file:// で開いた旧版の保存データは localhost 版へ自動移行されない。
  画面の「バックアップ↓」でJSON保存 →「復元↑」で読み込む手動移行が必要。

## 撤去済み機能（履歴・現行では非対応）

以下はかつて存在したが**コードから撤去済み**。現行の機能・制約ではない。
再要望が来たら「新規実装」として扱う（既存機能の復旧ではない）。

- **Fear & Greed（CNN公開データ）の自動取得** — 撤去済み。現行の取得対象は
  ドル円・日経平均・ダウ・ナスダック・SOX・米10年債のみ（`server.py` の `INDEX`）。
- **日経VI の自動取得** — 撤去済み。
- **マクロ買いシグナルのアラート（F&G < 20 かつ 日経VI > 50）** — 撤去済み。
  現行 HTML に残る「底打ちシグナル」等の手動シグナル項目とは別物なので混同しないこと。

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
