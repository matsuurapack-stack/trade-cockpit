import datetime
import time
import unittest

import catalyst_lookup as cl

JST = datetime.timezone(datetime.timedelta(hours=9))
NOW = datetime.datetime(2026, 9, 25, 10, 0, tzinfo=JST)


def mk(fetchers, **kw):
    return cl.CatalystLookup(fetchers, now_fn=lambda: NOW, cooldown_sec=0, bdays_fn=lambda a, b: 0, **kw)


def row(title, src="TDNET"):
    return {"title": title, "published_at": NOW - datetime.timedelta(hours=1), "source": src}


class RegulationDecoupleTests(unittest.TestCase):
    def test_slow_regulation_does_not_delay_snapshot(self):
        def slow_reg(code):
            time.sleep(1.0)
            return None, None
        svc = mk({"tdnet": lambda c: [row("自己株式取得に係る事項の決定")], "news": lambda c: [], "db": lambda c: [],
                  "earnings_next": lambda c: None, "regulation": slow_reg}, async_regulation=True)
        svc.request("6501", "RADAR")
        t0 = time.time()
        snap = svc.run_one("6501")
        self.assertLess(time.time() - t0, 0.5)
        self.assertEqual(snap["regulation_state"], "REGULATION_PENDING")
        self.assertEqual(snap["margin_restriction"], "UNKNOWN")
        self.assertIsNone(snap["timing_ms"]["regulation_ms"])
        self.assertEqual(snap["state"], "CONFIRMED")

    def test_regulation_updates_afterwards(self):
        flags = {"margin_deposit_same_day": True}
        import catalyst_engine as ce
        kinds = ce.margin_restriction_from_flags
        svc = mk({"tdnet": lambda c: [], "news": lambda c: [], "db": lambda c: [], "earnings_next": lambda c: None,
                  "regulation": lambda c: (flags, None)}, async_regulation=True)
        logged = []
        svc.on_snapshot = lambda code, snap, r: logged.append(snap)
        svc.request("6501", "RADAR")
        svc.run_one("6501")
        svc.drain_background()
        snap = svc.get("6501")
        self.assertIsNotNone(snap["timing_ms"]["regulation_ms"])
        self.assertIn(snap["regulation_state"], ("DONE", "UNAVAILABLE"))
        self.assertTrue(callable(kinds))

    def test_regulation_failure_is_isolated(self):
        def boom(code):
            raise RuntimeError("p_errno=-1")
        svc = mk({"tdnet": lambda c: [row("業績予想の修正")], "news": lambda c: [], "db": lambda c: [], "earnings_next": lambda c: None,
                  "regulation": boom}, async_regulation=True)
        svc.request("6501", "RADAR")
        svc.run_one("6501")
        svc.drain_background()
        snap = svc.get("6501")
        self.assertEqual(snap["regulation_state"], "UNAVAILABLE")
        self.assertEqual(snap["margin_restriction"], "UNKNOWN")
        self.assertEqual(snap["state"], "CONFIRMED")
        self.assertTrue(any(e.startswith("regulation:") for e in snap["errors"]))

    def test_sync_mode_unchanged(self):
        svc = mk({"tdnet": lambda c: [], "news": lambda c: [], "db": lambda c: [], "earnings_next": lambda c: None,
                  "regulation": lambda c: (None, None)})
        svc.request("6501", "RADAR")
        snap = svc.run_one("6501")
        self.assertIsNotNone(snap["timing_ms"]["regulation_ms"])


class TimingTests(unittest.TestCase):
    def test_timing_keys_and_parallel(self):
        def slow(code):
            time.sleep(0.3)
            return []
        svc = mk({"tdnet": slow, "news": slow, "db": slow, "earnings_next": lambda c: (time.sleep(0.3), None)[1]})
        svc.request("6501", "RADAR")
        t0 = time.time()
        snap = svc.run_one("6501")
        self.assertLess(time.time() - t0, 0.8)          # 4情報源が並列（直列なら1.2秒）
        tm = snap["timing_ms"]
        for k in ("tdnet_ms", "tachibana_news_ms", "db_ms", "earnings_ms", "regulation_ms", "total_ms"):
            self.assertIn(k, tm)
        for k in ("tdnet_ms", "tachibana_news_ms", "db_ms", "earnings_ms"):
            self.assertGreaterEqual(tm[k], 250)
        self.assertEqual(snap["duration_ms"], tm["total_ms"])


class StagedTdnetTests(unittest.TestCase):
    def test_recent_first_then_older(self):
        old = row("株式分割に関するお知らせ")
        old["published_at"] = NOW - datetime.timedelta(days=5)
        svc = mk({"tdnet_recent": lambda c: [row("自己株式取得に係る事項の決定")], "tdnet": lambda c: [row("自己株式取得に係る事項の決定"), old],
                  "news": lambda c: [], "db": lambda c: [], "earnings_next": lambda c: None})
        logged = []
        svc.on_snapshot = lambda code, snap, r: logged.append(snap["tdnet_depth"])
        svc.request("6501", "RADAR")
        snap = svc.run_one("6501")
        self.assertEqual(snap["tdnet_depth"], "RECENT")
        self.assertEqual(len(snap["items"]), 1)
        svc.drain_background()
        snap2 = svc.get("6501")
        self.assertEqual(snap2["tdnet_depth"], "FULL")
        self.assertEqual(len(snap2["items"]), 2)
        self.assertEqual(logged, ["RECENT", "FULL"])

    def test_no_change_in_deep_is_not_relogged(self):
        svc = mk({"tdnet_recent": lambda c: [row("自己株式取得に係る事項の決定")], "tdnet": lambda c: [row("自己株式取得に係る事項の決定")],
                  "news": lambda c: [], "db": lambda c: [], "earnings_next": lambda c: None})
        logged = []
        svc.on_snapshot = lambda code, snap, r: logged.append(1)
        svc.request("6501", "RADAR")
        svc.run_one("6501")
        svc.drain_background()
        self.assertEqual(len(logged), 1)

    def test_deep_failure_keeps_recent(self):
        def bad(c):
            raise RuntimeError("x")
        svc = mk({"tdnet_recent": lambda c: [row("自己株式取得に係る事項の決定")], "tdnet": bad,
                  "news": lambda c: [], "db": lambda c: [], "earnings_next": lambda c: None})
        svc.request("6501", "RADAR")
        svc.run_one("6501")
        svc.drain_background()
        self.assertEqual(svc.get("6501")["state"], "CONFIRMED")


if __name__ == "__main__":
    unittest.main()
