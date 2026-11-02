import contextlib
import io
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from _load import FIXTURES, load

ms = load("momentum-scanner", "momentum")
ta = load("technical-analysis", "ta")

UNI = str(FIXTURES / "momentum_universe")
BENCH = str(FIXTURES / "momentum_nifty.csv")
BANDS = str(FIXTURES / "momentum_bands.csv")


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = ms.main(argv)
    return rc, out.getvalue(), err.getvalue()


def _no_nan(token):
    raise ValueError(f"non-standard JSON constant {token}")


def js(argv):
    rc, out, err = run(argv + ["--json"])
    if rc != 0:
        raise AssertionError(f"rc={rc} err={err}")
    return json.loads(out, parse_constant=_no_nan)


def by_symbol(events):
    return {e["symbol"]: e for e in events}


class TestBurst(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.res = js(["burst", "--csv-dir", UNI])
        cls.hits = by_symbol(cls.res["bursts"])
        cls.rej = by_symbol(cls.res["rejected"])

    def test_planted_burst_on_right_date(self):
        b = self.hits["BURSTCO"]
        self.assertEqual(b["date"], "2026-10-29")
        self.assertAlmostEqual(b["chg_pct"], 6.0, places=1)
        self.assertGreater(b["vol_vs_50d"], 2.0)
        self.assertGreater(b["vol_vs_prev"], 1.0)
        self.assertGreaterEqual(b["base_days"], 3)
        self.assertLess(b["range_ratio"], 0.6)
        self.assertGreaterEqual(b["close_pos_pct"], 90)
        self.assertTrue(b["above_rising_50dma"])
        self.assertEqual(b["flags"], [])
        self.assertEqual(b["delivery"]["pct"], 62.5)          # optional Deliv % column is read
        self.assertFalse(b["delivery"]["below_avg"])
        self.assertLess(b["stop_ref"], b["close"])
        self.assertTrue(b["low_held"])
        self.assertEqual(self.res["window_start"], "2026-10-26")

    def test_not_found_before_it_happens(self):
        res = js(["burst", "--csv-dir", UNI, "--as-of", "2026-10-28"])
        self.assertNotIn("BURSTCO", by_symbol(res["bursts"]))

    def test_extended_move_rejected(self):
        self.assertNotIn("EXTENDED", self.hits)
        e = self.rej["EXTENDED"]
        self.assertEqual(e["date"], "2026-10-29")
        self.assertTrue(any(r.startswith("extended") for r in e["reasons"]), e["reasons"])
        self.assertGreater(e["run_3d_pct"], 6)

    def test_liquidity_filter(self):
        self.assertNotIn("ILLIQ", self.hits)
        e = self.rej["ILLIQ"]
        self.assertEqual(len(e["reasons"]), 1)
        self.assertIn("illiquid", e["reasons"][0])
        self.assertLess(e["turnover_cr"], 0.5)
        loose = by_symbol(js(["burst", "--csv-dir", UNI, "--min-turnover-cr", "0"])["bursts"])
        self.assertEqual(loose["ILLIQ"]["date"], "2026-10-29")      # same shape passes once the floor is off

    def test_upper_circuit_flagged(self):
        u = self.hits["UCLOCK"]                                     # no --bands: inferred from the tape
        self.assertIn("UC?", u["flags"])
        self.assertEqual(u["circuit"]["status"], "possible")
        self.assertEqual(u["circuit"]["band_pct"], 10)
        self.assertLess(u["score"], self.hits["BURSTCO"]["score"])
        known = by_symbol(js(["burst", "--csv-dir", UNI, "--bands", BANDS])["bursts"])["UCLOCK"]
        self.assertIn("UC", known["flags"])
        self.assertEqual(known["circuit"]["status"], "locked")
        self.assertAlmostEqual(known["circuit"]["upper"], known["close"], places=2)
        self.assertEqual(known["band"]["source"], "--bands")
        res = js(["burst", "--csv-dir", UNI, "--exclude-locked"])
        self.assertNotIn("UCLOCK", by_symbol(res["bursts"]))
        self.assertIn("upper-circuit", " ".join(by_symbol(res["rejected"])["UCLOCK"]["reasons"]))

    def test_small_band_flagged(self):
        s = self.hits["SMEPUMP"]
        self.assertIn("band5", s["flags"])
        self.assertEqual(s["band"]["source"], "inferred")
        self.assertIn("UC", s["flags"])
        self.assertTrue(any("surveillance" in n for n in s["notes"]))
        self.assertLess(s["score"], 50)
        self.assertNotIn("band5", self.hits["BURSTCO"]["flags"])

    def test_text_output(self):
        rc, out, _ = run(["burst", "--csv-dir", UNI, "--show-rejects"])
        self.assertEqual(rc, 0)
        self.assertIn("BURSTCO", out)
        self.assertIn("burst high", out)
        self.assertIn("Rejected (", out)
        self.assertIn("extended", out)


class TestPivot(unittest.TestCase):
    def test_planted_pivot_found(self):
        res = js(["pivot", "--csv-dir", UNI])
        hits = by_symbol(res["pivots"])
        self.assertEqual(list(hits), ["EPGAP"])
        p = hits["EPGAP"]
        self.assertEqual(p["date"], "2026-10-26")
        self.assertAlmostEqual(p["gap_pct"], 8.0, places=1)
        self.assertGreaterEqual(p["vol_vs_50d"], 3)
        self.assertTrue(p["holding"])
        self.assertFalse(p["gap_filled"])
        self.assertTrue(p["day1"].startswith("strong"))
        self.assertTrue(p["day2"].startswith("follow-through"))
        self.assertEqual(p["catalyst"]["supplied"], [])
        self.assertIsNotNone(p["catalyst"]["note"])
        self.assertIn("09:08", res["preopen_note"])

    def test_low_volume_gap_rejected(self):
        res = js(["pivot", "--csv-dir", UNI])
        self.assertNotIn("LOWVOLGAP", by_symbol(res["pivots"]))
        r = by_symbol(res["rejected"])["LOWVOLGAP"]
        self.assertEqual(r["date"], "2026-10-28")
        self.assertGreaterEqual(r["gap_pct"], 5)
        self.assertTrue(any("volume only" in x for x in r["reasons"]), r["reasons"])

    def test_day2_pending_and_catalyst(self):
        p = js(["pivot", "--csv-dir", UNI, "--as-of", "2026-10-26"])["pivots"][0]
        self.assertTrue(p["day2"].startswith("pending"))
        plain = by_symbol(js(["pivot", "--csv-dir", UNI])["pivots"])["EPGAP"]
        cat = by_symbol(js(["pivot", "--csv-dir", UNI, "--catalyst", "EPGAP=earnings,block"])["pivots"])["EPGAP"]
        self.assertEqual(cat["catalyst"]["supplied"], ["results", "block-deal"])
        confirmed = [x["kind"] for x in cat["catalyst"]["checklist"] if x["confirmed"]]
        self.assertEqual(confirmed, ["results", "block-deal"])
        self.assertEqual(cat["score"], plain["score"] + 10)

    def test_text_output(self):
        rc, out, _ = run(["pivot", "--csv-dir", UNI, "--catalyst", "results"])
        self.assertEqual(rc, 0)
        self.assertIn("[x] results", out)
        self.assertIn("pre-open", out)


class TestRS(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.res = js(["rs", "--csv-dir", UNI, "--benchmark-csv", BENCH])
        cls.rows = by_symbol(cls.res["ratings"])

    def test_ratings_in_range_and_ranked(self):
        ratings = [r["rating"] for r in self.res["ratings"]]
        self.assertEqual(len(ratings), 10)
        self.assertTrue(all(1 <= x <= 99 for x in ratings))
        self.assertEqual(self.rows["LEADER"]["rating"], 99)
        self.assertEqual(self.rows["LAGGARD"]["rating"], 1)
        by_ret = sorted(self.res["ratings"], key=lambda r: -r["weighted_return"])
        self.assertEqual([r["symbol"] for r in by_ret], [r["symbol"] for r in self.res["ratings"]])
        self.assertEqual(ratings, sorted(ratings, reverse=True))
        self.assertGreater(self.rows["BURSTCO"]["rating"], self.rows["FLATCO"]["rating"])

    def test_weighting_documented(self):
        r = self.rows["LEADER"]["returns"]
        expect = 0.4 * r["3m"] + 0.2 * r["6m"] + 0.2 * r["9m"] + 0.2 * r["12m"]
        self.assertAlmostEqual(self.rows["LEADER"]["weighted_return"], expect, delta=0.05)
        self.assertEqual(self.res["weights"], {"3m": 0.4, "6m": 0.2, "9m": 0.2, "12m": 0.2})
        score, rets, partial = ms.weighted_return([100 + k for k in range(130)])
        self.assertTrue(partial)
        self.assertEqual(sorted(rets), ["3m", "6m"])

    def test_rating_percentiles(self):
        self.assertEqual(ms._rating(10, [1, 2, 3]), 99)
        self.assertEqual(ms._rating(0, [1, 2, 3]), 1)
        self.assertEqual(ms._rating(2, [1, 3]), 50)

    def test_rs_line_leads_price(self):
        rl = self.rows["LEADER"]["rs_line"]
        self.assertTrue(rl["leading"])
        self.assertEqual(rl["state"], "new high ahead of price")
        self.assertLess(self.rows["LEADER"]["from_high_pct"], 0)
        self.assertFalse(self.rows["LAGGARD"]["rs_line"]["rs_new_high"])
        self.assertTrue(1 <= self.res["bench_rating"] <= 99)
        self.assertLess(self.rows["LAGGARD"]["from_high_pct"], -30)

    def test_blank_candles_skipped(self):
        raw = ta.load_csv(str(Path(UNI) / "FLATCO.csv"))
        st = ms.build_stock(raw, "FLATCO")
        self.assertEqual(len(st["c"]), 297)                         # 2 blank + 1 zero candle dropped
        self.assertIn("FLATCO", self.rows)

    def test_text_and_filters(self):
        rc, out, _ = run(["rs", "--csv-dir", UNI, "--top", "3"])
        self.assertEqual(rc, 0)
        self.assertIn("LEADER", out)
        self.assertNotIn("LAGGARD", out)
        top = js(["rs", "--csv-dir", UNI, "--min-rs", "50"])["ratings"]
        self.assertTrue(all(r["rating"] >= 50 for r in top))


class TestScan(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.res = js(["scan", "--csv-dir", UNI, "--benchmark-csv", BENCH, "--bands", BANDS])
        cls.wl = by_symbol(cls.res["watchlist"])

    def test_watchlist_contents(self):
        names = [e["symbol"] for e in self.res["watchlist"]]
        self.assertEqual(names[:2], ["BURSTCO", "EPGAP"])
        for bad in ("ILLIQ", "EXTENDED", "LAGGARD", "FLATCO"):
            self.assertNotIn(bad, self.wl)
        self.assertIn("RS leader", self.wl["LEADER"]["signals"])
        self.assertTrue(all(e["reason"] for e in self.res["watchlist"]))
        self.assertIn("episodic pivot 2026-10-26", self.wl["EPGAP"]["reason"])
        self.assertEqual(self.res["rejected"], {"bursts": 2, "pivots": 1})

    def test_handoff_skips_locked(self):
        h = self.wl["BURSTCO"]["handoff"]
        self.assertIn("breakout.py check --csv", h)
        self.assertIn("BURSTCO.csv", h)
        self.assertIn("--benchmark-csv", h)
        self.assertIsNone(self.wl["UCLOCK"].get("handoff"))
        self.assertIsNone(self.wl["SMEPUMP"].get("handoff"))
        self.assertIn("UC", self.wl["UCLOCK"]["flags"])

    def test_regime_gate_and_text(self):
        res = js(["scan", "--csv-dir", UNI, "--regime", "Correction"])
        self.assertEqual(res["regime"]["max_new"], 0)
        self.assertIn("no new momentum trades", res["regime"]["gate"])
        rc, out, _ = run(["scan", "--csv-dir", UNI, "--benchmark-csv", BENCH, "--top", "3"])
        self.assertEqual(rc, 0)
        self.assertIn("Regime not given", out)
        self.assertIn("Next:", out)
        self.assertIn("breakout.py check", out)
        self.assertIn("Why:", out)

    def test_expiry_week_helper(self):
        self.assertTrue(ms.monthly_expiry_week(date(2026, 10, 26)))     # Monday before the last Tuesday
        self.assertTrue(ms.monthly_expiry_week(date(2026, 10, 27)))
        self.assertFalse(ms.monthly_expiry_week(date(2026, 10, 28)))
        self.assertFalse(ms.monthly_expiry_week(date(2026, 10, 20)))


class TestCLI(unittest.TestCase):
    def test_json_valid_for_every_command(self):
        for cmd in ("burst", "pivot", "rs", "scan"):
            with self.subTest(cmd=cmd):
                d = js([cmd, "--csv-dir", UNI, "--benchmark-csv", BENCH, "--bands", BANDS])
                self.assertIn("as_of", d)

    def test_bad_input_exits_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad_band = Path(tmp) / "bands.csv"
            bad_band.write_text("symbol,band\nUCLOCK,7\n")
            junk = Path(tmp) / "JUNK.csv"
            junk.write_text("Date,Open,High,Low,Close,Volume\n2026-10-01,,,,,\nxx,1,2,3,4,5\n")
            cases = [
                ["burst"],                                                     # no universe
                ["burst", "--csv-dir", "/nonexistent"],
                ["burst", "--csv", "/nonexistent/x.csv"],
                ["burst", "--csv", str(junk)],                                 # only blank/garbage rows
                ["burst", "--csv-dir", UNI, "--pattern", "*.txt"],
                ["pivot", "--csv-dir", UNI, "--catalyst", "EPGAP=rumour"],
                ["rs", "--csv-dir", UNI, "--as-of", "2026-13-40"],
                ["rs", "--csv-dir", UNI, "--bands", str(bad_band)],
                ["scan", "--csv-dir", UNI, "--regime", "bullish"],
                ["burst", "--csv-dir", UNI, "--days", "0"],
                ["rs", "--csv-dir", UNI, "--benchmark-csv", str(junk)],
            ]
            for argv in cases:
                with self.subTest(argv=argv):
                    rc, out, err = run(argv)
                    self.assertEqual(rc, 2)
                    self.assertTrue(err.startswith("error:"), err)
                    self.assertEqual(len(err.strip().splitlines()), 1)
                    self.assertNotIn("Traceback", err)
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(io.StringIO()):
            ms.main(["nosuchcommand"])
        self.assertEqual(cm.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
