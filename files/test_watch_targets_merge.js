// watchTargets merge収束テスト（2026-09-18新規）。
//
// 「iPhoneだけ監視銘柄が3件のまま」調査で、フロントのマージ計算式そのものにバグが
// 無いことをNode.jsで直接検証する（DBもBrowserも使わない最小のE2E的検証。本ファイルは
// python -m unittest discover の対象外——このリポジトリにJSテストランナーは無いため、
// 手動で `node test_watch_targets_merge.js` を実行して確認する）。
//
// trade-cockpit.htmlに書いてある実際のマージ式を正規表現で抽出して評価する（コピー＆
// ペーストで別の式を用意すると、本体側だけ直しても気づかずテストが陳腐化するため、
// 常に実ファイルの現在の中身をテストする）。
//
// 実行方法： cd files && node test_watch_targets_merge.js

const fs = require("fs");
const path = require("path");

const html = fs.readFileSync(path.join(__dirname, "trade-cockpit.html"), "utf-8");

function extractLine(marker) {
  const idx = html.indexOf(marker);
  if (idx === -1) throw new Error(`マーカーが見つかりません（trade-cockpit.htmlの実装が変わった可能性）: ${marker}`);
  const lineEnd = html.indexOf("\n", idx);
  return html.slice(idx, lineEnd).trim();
}

// 実装から4行を抽出する（server取得→ローカル差分抽出→和集合、の3ステップ＋直前の
// localBefore代入）。抽出できなければ実装が変わってテストが追随できていないという
// シグナルとして例外で落とす（沈黙でパスしない）。
const lineLocalBefore = extractLine("const localBefore=sRef.current.watchTargets;");
const lineServer = extractLine("const serverWatchTargets=jd.items.filter(w=>w.isWatchTarget).map(w=>w.code);");
const lineLocalOnly = extractLine("const localOnly=localBefore.filter(c=>!serverWatchTargets.includes(c));");
const lineMerged = extractLine("const merged=[...new Set([...serverWatchTargets,...localOnly])];");

function runMerge({ serverCodes, localCodes }) {
  const jd = { items: serverCodes.map((code) => ({ code, isWatchTarget: true })) };
  const sRef = { current: { watchTargets: localCodes } };
  // eslint的な安全性は問わない（テスト専用のミニeval、実行対象はtrade-cockpit.html本体の
  // 純粋な配列操作4行のみ）。
  const fn = new Function("jd", "sRef", `${lineLocalBefore}\n${lineServer}\n${lineLocalOnly}\n${lineMerged}\nreturn merged;`);
  return fn(jd, sRef);
}

let failures = 0;
function assertEqual(actual, expected, label) {
  const a = JSON.stringify([...actual].sort());
  const b = JSON.stringify([...expected].sort());
  if (a !== b) {
    failures++;
    console.error(`FAIL: ${label}\n  expected: ${b}\n  actual:   ${a}`);
  } else {
    console.log(`ok: ${label}`);
  }
}

// シナリオ1（今回の実例）：iPhoneのローカル3件がサーバーのN件の部分集合。
// 期待結果：merge後はサーバーのN件（ローカルの3件で水増しされない、かつ3件のままにも
// ならない）——ユーザー要求「UI=N, state=N, localStorage=N」に対応する中核ケース。
{
  const serverCodes = ["1000", "1001", "1002", "1003", "1004", "1005", "1006", "1007", "1008", "1332"];
  const localCodes = ["1332", "1001", "1008"]; // iPhoneの古い3件、いずれもサーバー側に実在する
  const merged = runMerge({ serverCodes, localCodes });
  assertEqual(merged, serverCodes, "シナリオ1: local(3、server部分集合) + server(10) => server(10)に収束");
}

// シナリオ2：サーバーが0件（今回の運用前確認で実施した一時的なクリーンアップ直後の状態）
// のとき、ローカルの3件がそのまま生き残り、かつサーバーへ書き戻す対象（localOnly）として
// 正しく検出されること。
{
  const serverCodes = [];
  const localCodes = ["1332", "1812", "9983"];
  const merged = runMerge({ serverCodes, localCodes });
  assertEqual(merged, localCodes, "シナリオ2: server(0) + local(3) => local(3)がそのまま採用される（書き戻し対象として検出可能）");
}

// シナリオ3：ローカルにサーバーへ無い独自の追加が1件だけ混ざっている場合、その1件だけが
// 上乗せされ、サーバー側の他の件数は失われない（「サーバー値を消さない」ことの確認）。
{
  const serverCodes = ["2001", "2002", "2003"];
  const localCodes = ["2001", "9999"]; // 9999だけローカル固有
  const merged = runMerge({ serverCodes, localCodes });
  assertEqual(merged, ["2001", "2002", "2003", "9999"], "シナリオ3: server(3) + local固有1件 => server全件+固有1件（欠落なし）");
}

// シナリオ4：ローカルが空（新規端末）でサーバーがN件 => そのままN件を採用する
// （端末側に何も無くてもサーバー値だけで正しく揃うことの確認、まさに「iPhone初回起動」相当）。
{
  const serverCodes = ["3001", "3002", "3003", "3004"];
  const localCodes = [];
  const merged = runMerge({ serverCodes, localCodes });
  assertEqual(merged, serverCodes, "シナリオ4（iPhone初回相当）: server(N) + local(0) => server(N)に収束");
}

if (failures > 0) {
  console.error(`\n${failures}件のシナリオが失敗しました。`);
  process.exit(1);
} else {
  console.log("\n全シナリオOK：watchTargetsマージ式は要求どおりserver値に収束します。");
}
