// 保有中撤退判断支援アラート Phase A（2026-09-18新規）フロントロジックの純粋テスト。
//
// trade-cockpit.htmlに書いてあるderiveExitDecisionAlert()/evaluateThesisBreak()を
// 正規表現で抽出して評価する（DB/Browser不要、watchTargetsマージ式の検証と同じ手法）。
// 完成条件のE2Eシナリオ（最大含み益+1.67%→現在+0.10%、高値更新なし8分、VWAP割れ、
// 出来高低下、根拠5→2、過去反省に「利益消失後の粘り」あり → HIGH/CRITICAL）を再現する。
//
// 実行方法： cd files && node test_exit_decision_alert.js

const fs = require("fs");
const path = require("path");

const html = fs.readFileSync(path.join(__dirname, "trade-cockpit.html"), "utf-8");

function extractBlock(startMarker, endMarker) {
  const start = html.indexOf(startMarker);
  if (start === -1) throw new Error(`開始マーカーが見つかりません: ${startMarker}`);
  const end = html.indexOf(endMarker, start);
  if (end === -1) throw new Error(`終了マーカーが見つかりません: ${endMarker}`);
  return html.slice(start, end);
}

// THESIS_DIMENSION_LABELS〜EXIT_ALERT_TIER_META定義までを丸ごと抜き出してevalする
// （定数・関数定義だけのブロック、副作用なし）。
const block = extractBlock(
  "const THESIS_DIMENSION_LABELS=",
  "// current_stopが未設定ならOFF"
);
const sandbox = {};
new Function("sandbox", `${block}\nsandbox.evaluateCurrentThesis=evaluateCurrentThesis;\nsandbox.evaluateThesisBreak=evaluateThesisBreak;\nsandbox.deriveExitDecisionAlert=deriveExitDecisionAlert;`)(sandbox);
const { evaluateThesisBreak, deriveExitDecisionAlert } = sandbox;

let failures = 0;
function assertEqual(actual, expected, label) {
  if (JSON.stringify(actual) !== JSON.stringify(expected)) {
    failures++;
    console.error(`FAIL: ${label}\n  expected: ${JSON.stringify(expected)}\n  actual:   ${JSON.stringify(actual)}`);
  } else {
    console.log(`ok: ${label}`);
  }
}
function assertTrue(cond, label) {
  if (!cond) { failures++; console.error(`FAIL: ${label}`); } else { console.log(`ok: ${label}`); }
}

// --- evaluateThesisBreak: エントリー時trueだった項目だけを崩壊として数える ---
{
  const entryThesis = { trend_up: true, volume_expanding: true, above_vwap: true, market_supportive: true,
                          sector_supportive: true, breakout_detected: true, momentum_positive: true };
  const currentThesis = { trend_up: false, volume_expanding: false, above_vwap: false, market_supportive: true,
                            sector_supportive: true, breakout_detected: false, momentum_positive: true };
  const broken = evaluateThesisBreak(entryThesis, currentThesis);
  assertEqual(broken.sort(), ["above_vwap", "breakout_detected", "trend_up", "volume_expanding"].sort(),
    "evaluateThesisBreak: entry時trueで現在falseの項目のみ崩壊とみなす（5→2相当の実例）");
}
{
  // entry時点で既にfalse/nullだった項目は「崩壊」に数えない
  const entryThesis = { trend_up: null, above_vwap: true };
  const currentThesis = { trend_up: false, above_vwap: false };
  const broken = evaluateThesisBreak(entryThesis, currentThesis);
  assertEqual(broken, ["above_vwap"], "evaluateThesisBreak: entry時点でnull/falseだった項目は崩壊カウントしない");
}

// --- PHASE3: 利益消失アラート（0.4%未満のpeakでは評価しない） ---
{
  const r = deriveExitDecisionAlert({ pnlPct: 0.1, peakPnlPct: 0.3, peakAt: new Date(Date.now() - 10 * 60000).toISOString(),
    brokenConditions: [], eventRiskLevel: "LOW" });
  assertEqual(r.profitGivebackPct, null, "PHASE3: peakPnlPct<0.4%では利益消失率を評価しない");
}

// --- 完成条件E2Eシナリオ：最大含み益+1.67%→現在+0.10%、高値更新なし8分、根拠3件崩壊 ---
{
  const peakAt = new Date(Date.now() - 8 * 60000).toISOString();
  const alert = deriveExitDecisionAlert({
    pnlPct: 0.10, peakPnlPct: 1.67, peakAt,
    brokenConditions: ["above_vwap", "volume_expanding", "breakout_detected"], eventRiskLevel: "LOW",
  });
  assertTrue(alert.tier === "HIGH" || alert.tier === "CRITICAL",
    `完成条件E2E: tierがHIGH/CRITICAL（実際=${alert.tier}）`);
  assertTrue(Math.round(alert.profitGivebackPct) === 94 || Math.round(alert.profitGivebackPct) === 93,
    `完成条件E2E: 利益消失率が約93-94%（実際=${alert.profitGivebackPct}）`);
  assertTrue(alert.isHopeHolding === true || alert.isBreakevenExitCandidate === true,
    "完成条件E2E: HOPE_HOLDINGまたはBREAKEVEN_EXIT_CANDIDATEのいずれかを検出");
}

// --- PHASE13: イベント日補正（高値更新なしの閾値が短くなる） ---
{
  const peakAt = new Date(Date.now() - 3 * 60000).toISOString(); // 3分前
  const normal = deriveExitDecisionAlert({ pnlPct: 0.5, peakPnlPct: 1.0, peakAt, brokenConditions: ["a", "b"], eventRiskLevel: "LOW" });
  const eventDay = deriveExitDecisionAlert({ pnlPct: 0.5, peakPnlPct: 1.0, peakAt, brokenConditions: ["a", "b"], eventRiskLevel: "HIGH" });
  assertTrue(!normal.reasons.some(r => r.includes("高値更新なし")), "PHASE13: 通常日は3分では高値更新なし扱いにしない（閾値5分）");
  assertTrue(eventDay.reasons.some(r => r.includes("高値更新なし")), "PHASE13: イベントHIGH日は3分でも高値更新なし扱い（閾値2.5分）");
}

// --- PHASE4: 同値撤退候補の閾値未満では発火しない ---
{
  const r = deriveExitDecisionAlert({ pnlPct: 0.1, peakPnlPct: 0.5, peakAt: new Date(Date.now() - 10 * 60000).toISOString(),
    brokenConditions: ["a", "b"], eventRiskLevel: "LOW" });
  assertTrue(r.isBreakevenExitCandidate === false, "PHASE4: 最大含み益0.8%未満ではBREAKEVEN_EXIT_CANDIDATEを発火しない");
}

// --- 平穏な状態ではtier無し ---
{
  const r = deriveExitDecisionAlert({ pnlPct: 1.0, peakPnlPct: 1.1, peakAt: new Date().toISOString(),
    brokenConditions: [], eventRiskLevel: "LOW" });
  assertEqual(r.tier, null, "平穏な保有（根拠崩壊なし・高値更新直近）ではtierを発火しない");
}

if (failures > 0) {
  console.error(`\n${failures}件のシナリオが失敗しました。`);
  process.exit(1);
} else {
  console.log("\n全シナリオOK：保有中撤退判断支援アラートのロジックは期待どおり動作します。");
}
