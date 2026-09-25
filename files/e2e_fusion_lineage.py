"""Technical Fusion のデータ系統E2E（実機・場中用）。立花へは一切ログインしない——起動中の本番サーバー
（既に立花セッションを持つ）のHTTP APIを読むだけなので、立花セッションの競合は起きない。

使い方（本番サーバーが起動していて、場中に実行）：
    cd files && python e2e_fusion_lineage.py 627A 8035 [samples] [interval_sec]
確認すること：quote / 内部5分足(barAt) / pVWAP / Chart Context / Technical Fusion が同じ時刻系で繋がっている
（quoteAt >= barAt、analyzedAt >= quoteAt、intraday_source が TACHIBANA系、stale_intraday=False、
VWAPがpVWAP由来）。認証は secrets.json の users から本プロセス内でヘッダーを作る（コマンド行に出さない）。
"""
import base64
import datetime
import json
import sys
import time
import urllib.request

import server

BASE = "http://127.0.0.1:8765"
USER, PW = next(iter(server.USERS.items()))
HDR = {"Authorization": "Basic " + base64.b64encode(f"{USER}:{PW}".encode()).decode(), "Content-Type": "application/json"}


def call(path, body=None):
    req = urllib.request.Request(BASE + path, data=None if body is None else json.dumps(body).encode(),
                                 headers=HDR, method="POST" if body is not None else "GET")
    return json.loads(urllib.request.urlopen(req, timeout=180).read().decode("utf-8"))


def hms(x):
    return (x or "--")[11:19] if x else "--"


def main():
    codes = [a for a in sys.argv[1:] if not a.isdigit() or len(a) == 4] or ["627A", "8035"]
    nums = [int(a) for a in sys.argv[1:] if a.isdigit() and len(a) != 4]
    samples, interval = (nums + [3, 30])[:2] if nums else (3, 30)
    for i in range(samples):
        live = call("/api/entry-candidates/live")
        pool = {}
        for key in ("entryReadyTop5", "actionableTop5", "analysisTop5", "watchCandidates", "reversalCandidates", "reversalWatchCandidates"):
            for c in live.get(key) or []:
                pool.setdefault(c["code"], c)
        quotes = call("/api/stock-quotes/fast", [{"code": c, "market": "JP"} for c in codes])["quotes"]
        print(f"\n=== sample {i + 1}/{samples} {datetime.datetime.now().strftime('%H:%M:%S')} (scan generatedAt={hms(live.get('generatedAt'))}Z) ===")
        for c in codes:
            q = quotes.get(c) or {}
            cand = pool.get(c)
            print(f"[{c}] quote: t={q.get('t')} pVWAP={q.get('vwap')} quoteAt={hms(q.get('quote_timestamp'))} src={q.get('source')} stale={q.get('is_stale')}")
            if not cand:
                print("    （今買い時の候補プール外：entry-candidates/liveに無い。プールに入る銘柄で確認してください）")
                continue
            tfz = cand.get("technicalFusion") or {}
            src = tfz.get("source") or {}
            lin = cand.get("chartLineage") or {}
            cc_ = cand.get("chartContext") or {}
            print(f"    candidate: current={cand.get('current')} quoteAt={hms(cand.get('quoteAt'))} scoredAt={hms(cand.get('scoredAt'))} vwap-source={'exchange pVWAP' if q.get('vwap') else 'n/a'}")
            print(f"    chart: pattern={cc_.get('pattern')} timing={cc_.get('entry_timing_score')} bars={cc_.get('barCount')} confidence={cc_.get('confidence')}")
            print(f"    lineage: {lin.get('intraday_source')} internal_bars={lin.get('internal_bars')} yf_bars_used={lin.get('yf_bars_used')} "
                  f"gap_bars={lin.get('gap_bars')} stale={lin.get('stale')} reasons={lin.get('reasons')}")
            print(f"    fusion: score={tfz.get('score')} level={tfz.get('level')} setup={tfz.get('setupType')} state={tfz.get('technicalState')} "
                  f"rec={tfz.get('recommendation')} flags={tfz.get('flags')} confidence={tfz.get('confidence')}")
            print(f"    source: intraday={src.get('intraday_source')} daily={src.get('daily_source')} quote_at={hms(src.get('quote_at'))} "
                  f"bar_at={hms(src.get('bar_at'))} analyzed_at={hms(src.get('analyzed_at'))} stale_intraday={src.get('stale_intraday')}")
            # 時刻系の整合チェック
            ok = []
            try:
                qa, ba, aa = (datetime.datetime.fromisoformat(src[k]) if src.get(k) else None for k in ("quote_at", "bar_at", "analyzed_at"))
                ok.append(("bar_at <= quote_at", (ba is None or qa is None or ba <= qa)))
                ok.append(("quote_at <= analyzed_at", (qa is None or aa is None or qa <= aa)))
                ok.append(("bar_at is recent (<=10min)", ba is None or aa is None or (aa - ba).total_seconds() <= 600))
            except Exception as e:
                ok.append(("time parse", False))
            ok.append(("intraday_source is Tachibana", (src.get("intraday_source") or "").find("TACHIBANA") >= 0))
            ok.append(("not stale", src.get("stale_intraday") is False))
            print("    checks:", {k: v for k, v in ok})
        if i < samples - 1:
            time.sleep(interval)


if __name__ == "__main__":
    main()
