# X Intelligence Phase5（2026-09-15新規）：分析連携の統合テスト。
#
# 「DBに存在するだけ」「専用APIで見られるだけ」では完了扱いにしない、という方針に沿って、
# 各最終payloadへ実際にexternal intelligenceが注記されること、既存スコア計算式（ENTRY
# TOP5・Sector Rotation state・analyze_stockのentry/stop/target）には一切混入しない
# ことを検証する。generate_morning_market_check/generate_intraday_reportは実市場データ
# 取得を伴う重い関数のため、ここではpayload構築部分のソース検証＋関連ヘルパー関数の
# 単体テストで担保し、実データE2Eは別途スクリプトで実施する。
#
# 実行方法： cd files & python -m unittest test_x_intelligence_phase5_integration -v

import inspect
import unittest
from unittest import mock

import server


class ScoreNonInterferenceSignatureTests(unittest.TestCase):
    """指示書14番（最重要）：X/Expert情報の有無でスコア計算式が変化しないことの
    構造的証明——スコア関数自体がexternal intelligenceを引数として受け取らないこと。"""

    def test_score_entry_candidates_signature_unchanged(self):
        sig = list(inspect.signature(server._score_entry_candidates).parameters.keys())
        self.assertEqual(sig, ["database_url", "user_id"])

    def test_classify_sector_state_signature_unchanged(self):
        sig = list(inspect.signature(server.classify_sector_state).parameters.keys())
        self.assertEqual(sig, ["score"])

    def test_analyze_stock_external_intelligence_is_optional_and_last(self):
        sig = inspect.signature(server.analyze_stock)
        params = list(sig.parameters.keys())
        self.assertEqual(params, ["w", "market_env", "external_intelligence"])
        self.assertIsNone(sig.parameters["external_intelligence"].default)
        self.assertIsNone(sig.parameters["market_env"].default)  # 既存デフォルトも不変


class MorningCheckAndIntradayReportWiringTests(unittest.TestCase):
    """朝一チェック・4レポートのpayload構築に external_intelligence_json が
    実際に組み込まれていることをソースレベルで確認する（指示書4・5番）。"""

    def test_morning_check_payload_includes_external_intelligence(self):
        src = inspect.getsource(server.generate_morning_market_check)
        self.assertIn('"external_intelligence_json": external_intel_ctx', src)
        # 既存フィールドが削除されていないこと（後方互換）。
        self.assertIn('"market_news_context_json"', src)
        self.assertIn("_nicosoku_morning_commentary_safe", src)

    def test_intraday_report_payload_includes_external_intelligence(self):
        src = inspect.getsource(server.generate_intraday_report)
        self.assertIn('"external_intelligence_json": build_external_intelligence_context_safe', src)
        self.assertIn('"market_news_context_json"', src)  # 既存フィールド無変更

    def test_watchlist_top5_json_annotated_with_external_intelligence(self):
        src = inspect.getsource(server.generate_morning_market_check)
        self.assertIn('"externalIntelligence": (', src)
        self.assertIn('"newsCatalyst": news_catalyst_map.get(c["code"])', src)  # 既存注記も無変更


class SectorRotationExternalIntelligenceTests(unittest.TestCase):
    """Sector Rotation：external_intelligenceは別フィールドであり、既存のscore/state
    判定式（classify_sector_state・build_sector_strength_score等）は無変更（指示書6番）。"""

    def test_build_sector_rotation_snapshot_source_keeps_external_intelligence_separate(self):
        src = inspect.getsource(server.build_sector_rotation_snapshot)
        self.assertIn('"external_intelligence": _sector_intel_by_theme.get(theme)', src)
        # scoreの算出行そのものは変更していないことを確認（既存の呼び出しが残っている）。
        self.assertIn("classify_sector_state(score)", src)

    def test_sector_signal_target_key_mapping_has_no_ambiguous_entries(self):
        # 対応表の値（テーマ名）が全てSECTOR_ROTATION_THEMESに実在すること。
        for theme in server.SECTOR_SIGNAL_TARGET_KEY_TO_ROTATION_THEME.values():
            self.assertIn(theme, server.SECTOR_ROTATION_THEMES)


class StockIntelligenceAnnotationTests(unittest.TestCase):
    """build_stock_intelligence_annotation()：analyze_stock()へ渡す銘柄別注記の
    組み立てロジック（指示書8番）。BUY/SELL判定には一切使わない読み取り専用データ。"""

    def test_market_observation_matched_by_direct_mention(self):
        ctx = {"stock_signals": [{"code": "7203", "direction": "BULLISH",
                                    "bullish_sources": ["kgbukabu"], "bearish_sources": []}],
               "sector_signals": [], "disagreements": [], "expert_views": []}
        result = server.build_stock_intelligence_annotation("7203", ctx)
        self.assertEqual(result["marketObservation"]["direction"], "BULLISH")

    def test_sector_consensus_matched_via_theme_code_lookup(self):
        # 6963はSECTOR_ROTATION_THEME_CODESの"半導体"に属する。
        ctx = {"stock_signals": [], "expert_views": [],
               "sector_signals": [{"topic": "SEMICONDUCTOR", "direction": "BULLISH",
                                     "independent_source_count": 2, "confidence": 0.6}],
               "disagreements": []}
        result = server.build_stock_intelligence_annotation("6963", ctx)
        self.assertEqual(result["sectorConsensus"]["sector"], "半導体")
        self.assertEqual(result["sectorConsensus"]["level"], "SECTOR")  # 銘柄固有ではないことを明示

    def test_no_related_information_returns_none(self):
        ctx = {"stock_signals": [], "sector_signals": [], "disagreements": [], "expert_views": []}
        self.assertIsNone(server.build_stock_intelligence_annotation("9999", ctx))

    def test_none_context_returns_none(self):
        self.assertIsNone(server.build_stock_intelligence_annotation("7203", None))

    def test_analyze_stock_accepts_annotation_without_affecting_signature_defaults(self):
        # external_intelligenceを渡しても既存の2引数呼び出しと同じ挙動である
        # （実際の判定はmarket_env/wのみに依存、この引数はpass-throughのみ）ことを
        # ソースの最終return文にのみ現れることで確認する。
        src = inspect.getsource(server.analyze_stock)
        self.assertIn('"externalIntelligence": external_intelligence,', src)
        # entry/stop/target等の算出行にexternal_intelligenceという語が現れないこと
        # （判定ロジックへ混入していないことの簡易確認）。
        calc_lines = [l for l in src.splitlines() if any(k in l for k in ("entry =", "stop =", "target ="))]
        for l in calc_lines:
            self.assertNotIn("external_intelligence", l)


if __name__ == "__main__":
    unittest.main()
