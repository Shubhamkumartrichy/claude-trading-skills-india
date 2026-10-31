import contextlib
import io
import json
import math
import unittest
from datetime import timedelta

from _load import FIXTURES, ROOT, load

bp = load("breakout-planner", "breakout")
ta = load("technical-analysis", "ta")

VCP = str(FIXTURES / "breakout_stk_vcp.csv")
FLAT = str(FIXTURES / "breakout_stk_flat.csv")
DOWN = str(FIXTURES / "breakout_stk_downtrend.csv")
ILLIQ = str(FIXTURES / "breakout_stk_illiquid.csv")
BENCH = str(FIXTURES / "breakout_bench_nifty.csv")


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = bp.main(argv)
    return rc, out.getvalue(), err.getvalue()


class TestTicks(unittest.TestCase):
    def test_nse_slabs(self):
        self.assertEqual(bp.tick_for(249.99), 0.01)
        self.assertEqual(bp.tick_for(250), 0.05)
        self.assertEqual(bp.tick_for(999.95), 0.05)
        self.assertEqual(bp.tick_for(1000), 0.10)
        self.assertEqual(bp.tick_for(7500), 0.50)
        self.assertEqual(bp.tick_for(15000), 1.00)
        self.assertEqual(bp.tick_for(30000), 5.00)

    def test_rounding_modes(self):
        self.assertEqual(bp.to_tick(1005.006, mode="up"), 1005.1)
        self.assertEqual(bp.to_tick(1005.006, 0.05, "up"), 1005.05)
        self.assertEqual(bp.to_tick(465.33, mode="down"), 465.3)
        self.assertEqual(bp.to_tick(473.8, mode="up"), 473.8)       # exact grid value is kept
        self.assertEqual(bp.to_tick(123.456, mode="nearest"), 123.46)


class TestTrendTemplate(unittest.TestCase):
    def test_vcp_stock_is_stage2_with_rs(self):
        tt = bp.trend_template(ta.load_csv(VCP), ta.load_csv(BENCH))
        self.assertTrue(tt["stage2"])
        self.assertEqual(tt["passed"], tt["total"])
        self.assertIn("rs", [c["key"] for c in tt["criteria"]])

    def test_downtrend_fails(self):
        tt = bp.trend_template(ta.load_csv(DOWN), ta.load_csv(BENCH))
        self.assertFalse(tt["stage2"])
        by = {c["key"]: c["pass"] for c in tt["criteria"]}
        self.assertFalse(by["above_mas"])
        self.assertFalse(by["sma200_rising"])
        self.assertFalse(by["near_high"])
        self.assertFalse(by["rs"])

    def test_short_history_marks_unknown(self):
        tt = bp.trend_template(ta.load_csv(VCP)[-120:])
        self.assertFalse(tt["stage2"])
        self.assertGreater(tt["unknown"], 0)

    def test_liquidity_filter(self):
        self.assertTrue(bp.liquidity(ta.load_csv(VCP), 5)["ok"])
        lq = bp.liquidity(ta.load_csv(ILLIQ), 5)
        self.assertFalse(lq["ok"])
        self.assertLess(lq["avg_traded_value_cr"], 0.1)
        self.assertIsNone(bp.liquidity(ta.load_csv(BENCH), 5)["ok"])     # index: no volume


class TestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = ta.load_csv(VCP)

    def test_planted_vcp(self):
        b = bp.detect_base(self.rows)
        self.assertEqual(b["type"], "VCP")
        depths = [round(c["depth_pct"]) for c in b["contractions"]]
        self.assertEqual(depths, [20, 10, 5])
        self.assertAlmostEqual(b["pivot"], 194.0)
        self.assertAlmostEqual(b["last_low"], 184.3)
        self.assertEqual(b["state"], "pre-breakout")
        self.assertEqual(b["grade"], "A")
        self.assertLess(b["volume_dryup_ratio"], 0.6)
        self.assertEqual(b["start_date"], "2026-06-25")

    def _extend(self, close, vol_mult):
        rows = [dict(r) for r in self.rows]
        last = rows[-1]
        rows.append({"date": last["date"] + timedelta(days=3), "open": last["close"], "high": close * 1.004,
                     "low": last["close"] * 0.998, "close": close, "volume": 1_000_000 * vol_mult})
        return rows

    def test_breakout_and_extended_states(self):
        b = bp.detect_base(self._extend(196.5, 2.5))
        self.assertEqual(b["state"], "breakout zone")
        self.assertAlmostEqual(b["pivot"], 194.0)
        self.assertGreater(b["volume_vs_50d"], 1.5)
        self.assertEqual(bp.detect_base(self._extend(205.0, 2.5))["state"], "extended")

    def test_flat_base(self):
        b = bp.detect_base(ta.load_csv(FLAT))
        self.assertEqual(b["type"], "flat base")
        self.assertAlmostEqual(b["pivot"], 181.74, places=2)
        self.assertLessEqual(b["depth_pct"], 15)

    def test_downtrend_is_not_a_setup(self):
        b = bp.detect_base(ta.load_csv(DOWN))
        self.assertNotIn(b["type"], ("VCP", "flat base"))
        self.assertEqual(b["grade"], "D")

    def test_too_few_bars(self):
        with self.assertRaises(ValueError):
            bp.detect_base(self.rows[:40])


class TestPlanMath(unittest.TestCase):
    def plan(self, **kw):
        base = dict(contraction_low=470, prev_close=495, band=5)
        base.update(kw)
        return bp.build_plan(500, 1_000_000, 1.0, **base)

    def test_trigger_limit_stop_cap(self):
        p = self.plan()
        self.assertEqual(p["trigger"], 501.00)        # 500 x 1.002, tick 0.05
        self.assertEqual(p["limit"], 515.00)          # 500 x 1.03
        self.assertEqual(p["stop_candidates"]["structure"], 465.30)   # 470 x 0.99
        self.assertTrue(p["stop_capped"])             # 465.30 is 9.65% under 515
        self.assertEqual(p["stop"], 473.80)           # 515 x 0.92
        self.assertAlmostEqual(p["loss_pct_from_limit"], 8.0, places=6)
        self.assertAlmostEqual(p["R_per_share"], 41.2, places=6)
        self.assertEqual([t["price"] for t in p["targets"]], [597.40, 638.60])

    def test_size_matches_risk_formula(self):
        p = self.plan()
        per_share = 41.2 + 515 * p["cost_pct_used"] / 100
        self.assertEqual(p["qty"], math.floor(10_000 / per_share))
        self.assertTrue(230 <= p["qty"] <= 242, p["qty"])
        self.assertLessEqual(p["risk_if_stopped"], 10_000)
        self.assertTrue(0.15 < p["cost_pct_used"] < 0.35)          # delivery round trip ≈ 0.2%
        self.assertAlmostEqual(p["position_value"], p["qty"] * 515)

    def test_uncapped_stop(self):
        p = self.plan(contraction_low=490)
        self.assertFalse(p["stop_capped"])
        self.assertEqual(p["stop"], 485.10)

    def test_chase_limit_and_warning(self):
        p = self.plan(max_chase_pct=5)
        self.assertEqual(p["limit"], 525.00)
        self.assertFalse(any("above 5%" in w for w in p["warnings"]))
        p = self.plan(max_chase_pct=7)
        self.assertTrue(any("above 5%" in w for w in p["warnings"]))
        p = self.plan(last_close=530)
        self.assertTrue(any("missed" in w for w in p["warnings"]))

    def test_band_warning(self):
        ok = self.plan()                               # upper 495 x 1.05 = 519.75 > 515
        self.assertEqual(ok["band"]["upper"], 519.75)
        self.assertFalse(any("above the upper circuit" in w or "cannot trigger" in w for w in ok["warnings"]))
        bad = self.plan(prev_close=480)                # upper 504.00 < limit 515
        self.assertEqual(bad["band"]["upper"], 504.00)
        self.assertTrue(any("above the upper circuit" in w for w in bad["warnings"]))
        locked = self.plan(prev_close=470)             # upper 493.50 < trigger 501
        self.assertTrue(any("cannot trigger this session" in w for w in locked["warnings"]))
        fno = self.plan(band=0, prev_close=495)
        self.assertTrue(any("dynamic price band" in c for c in fno["checklist"]))

    def test_tick_rounding_in_plan(self):
        p = bp.build_plan(1003, 1_000_000, 1.0, stop=960)
        self.assertEqual(p["trigger"], 1005.10)         # 1005.006 up to the ₹0.10 slab
        q = bp.build_plan(1003, 1_000_000, 1.0, stop=960, tick=0.05)
        self.assertEqual(q["trigger"], 1005.05)
        self.assertEqual(bp.build_plan(120, 1_000_000, 1.0, stop=112)["trigger"], 120.24)   # ₹0.01 slab

    def test_mtf_halves_risk_budget(self):
        p = self.plan(segment="mtf")
        self.assertEqual(p["leverage"], 2.0)
        self.assertLessEqual(p["risk_if_stopped"], 5_000)
        self.assertTrue(any("MTF" in o or "mtf" in o for o in [p["orders"]["gtt_entry"]]))

    def test_surveillance_warning(self):
        p = self.plan(surveillance="t2t")
        self.assertTrue(any("Trade-for-trade" in w for w in p["warnings"]))

    def test_invalid_inputs(self):
        with self.assertRaises(ValueError):
            bp.build_plan(500, 1_000_000, 1.0)                     # no stop source
        with self.assertRaises(ValueError):
            bp.build_plan(500, 1_000_000, 1.0, stop=505)           # stop above trigger
        with self.assertRaises(ValueError):
            bp.build_plan(500, 1_000_000, 1.0, stop=480, leverage=3)   # leverage without MTF


class TestCLI(unittest.TestCase):
    def test_check_json(self):
        rc, out, _ = run(["check", "--csv", VCP, "--benchmark-csv", BENCH, "--json"])
        self.assertEqual(rc, 0)
        d = json.loads(out)
        self.assertEqual(d["verdict"], "actionable")
        self.assertAlmostEqual(d["base"]["pivot"], 194.0)

    def test_plan_from_csv_json(self):
        rc, out, _ = run(["plan", "--csv", VCP, "--capital", "500000", "--band", "10", "--json"])
        self.assertEqual(rc, 0)
        d = json.loads(out)
        self.assertAlmostEqual(d["plan"]["pivot"], 194.0)
        self.assertEqual(d["plan"]["trigger"], 194.39)               # 194 x 1.002, tick 0.01
        self.assertLess(d["plan"]["stop"], d["plan"]["trigger"])
        self.assertGreater(d["plan"]["qty"], 0)

    def test_plan_manual_text(self):
        rc, out, _ = run(["plan", "--pivot", "500", "--contraction-low", "470", "--prev-close", "480",
                          "--band", "5", "--capital", "1000000"])
        self.assertEqual(rc, 0)
        self.assertIn("GTT", out)
        self.assertIn("₹515.00", out)

    def test_scan_ranks(self):
        rc, out, _ = run(["scan", "--csv-dir", str(FIXTURES), "--pattern", "breakout_stk_*.csv",
                          "--benchmark-csv", BENCH, "--json"])
        self.assertEqual(rc, 0)
        names = [r["name"] for r in json.loads(out)["results"]]
        self.assertEqual(names, ["breakout_stk_vcp", "breakout_stk_flat", "breakout_stk_illiquid",
                                 "breakout_stk_downtrend"])
        rc, out, _ = run(["scan", "--csv-dir", str(FIXTURES), "--pattern", "breakout_*.csv",
                          "--benchmark-csv", BENCH, "--top", "2"])
        self.assertEqual(rc, 0)
        self.assertIn("breakout_stk_vcp", out)
        self.assertNotIn("breakout_bench_nifty", out)                # benchmark excluded from its own scan

    def test_sample_csv(self):
        rc, out, _ = run(["check", "--csv", str(ROOT / "examples" / "sample_ohlcv.csv")])
        self.assertEqual(rc, 0)
        self.assertIn("Trend template", out)

    def test_bad_input_exits_cleanly(self):
        cases = [
            ["check", "--csv", "/nonexistent/x.csv"],
            ["check", "--candles", str(FIXTURES / "candles.json")],          # 40 bars: too short
            ["plan", "--capital", "100000"],                                 # nothing to plan from
            ["plan", "--pivot", "500", "--stop", "480", "--capital", "100000", "--band", "7"],
            ["plan", "--pivot", "500", "--stop", "520", "--capital", "100000"],
            ["scan", "--csv-dir", "/nonexistent"],
            ["scan"],
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                rc, out, err = run(argv)
                self.assertEqual(rc, 2)
                self.assertIn("error:", err)
                self.assertNotIn("Traceback", err)
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(io.StringIO()):
            bp.main(["plan", "--pivot", "500"])                              # argparse: --capital missing
        self.assertEqual(cm.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
