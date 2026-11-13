import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from datetime import date

from _load import FIXTURES, load

mr = load("macro-regime", "macro")
TURN = FIXTURES / "macro_turn"          # daily NIFTY + smallcap (V-shaped ratio, low 2026-01), monthly equal weight
SQUEEZE = FIXTURES / "macro_squeeze"    # monthly: crude, USD/INR, 10y, T-bill rising; T-bill ends above the 10y
EASING = FIXTURES / "macro_easing"      # monthly: 10y falling, repo cut in steps, crude and USD/INR falling


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = mr.main(argv)
    return code, out.getvalue(), err.getvalue()


def strict_json(text):
    def bad(c):
        raise ValueError(f"non-standard JSON constant {c}")
    return json.loads(text, parse_constant=bad)


def status_json(directory, *extra):
    code, out, err = run(["status", "--csv-dir", str(directory), "--json", *extra])
    assert code == 0, err
    return strict_json(out)


def by_key(res):
    return {r["key"]: r for r in res["components"]}


class TestMaths(unittest.TestCase):
    def test_change_and_slope_by_hand(self):
        self.assertAlmostEqual(mr.change([100, 110, 121], 1), 10.0)
        self.assertAlmostEqual(mr.change([100, 110, 121], 2), 21.0)
        self.assertIsNone(mr.change([100, 110, 121], 3))
        self.assertAlmostEqual(mr.change([7.10, 7.00, 6.85], 2, "level"), -25.0)       # basis points
        # [1, 2, 3]: fitted slope 1 per month on a mean of 2 -> 50% of the mean per month
        self.assertAlmostEqual(mr.slope([1, 2, 3], 2), 50.0)
        self.assertAlmostEqual(mr.slope([6.0, 6.1, 6.2, 6.3], 3, "level"), 10.0)       # 10bp a month
        self.assertAlmostEqual(mr.ols_slope([5, 5, 5, 5]), 0.0)

    def test_ratio_spread_and_basket(self):
        num = ([10, 11, 12], [200.0, 210.0, 240.0])
        den = ([11, 12, 13], [100.0, 120.0, 130.0])
        self.assertEqual(mr.ratio_line(num, den), ([11, 12], [2.1, 2.0]))
        months, vals = mr.spread_line(([1, 2], [7.0, 7.2]), ([2, 3], [6.5, 6.4]))
        self.assertEqual(months, [2])
        self.assertAlmostEqual(vals[0], 0.7)
        # each member rebased to 100 at the first shared month, then averaged
        months, vals = mr.basket([([1, 2], [50.0, 60.0]), ([1, 2], [200.0, 180.0])])
        self.assertEqual(months, [1, 2])
        self.assertAlmostEqual(vals[0], 100.0)
        self.assertAlmostEqual(vals[1], (120.0 + 90.0) / 2)

    def test_crossover_gap_on_tiny_series(self):
        p = {"fast": 2, "slow": 4, "band_pct": 0.25, "band_bp": 5}
        line = mr.make_line("x", [0, 1, 2, 3, 4, 5], [10, 9, 8, 7, 8, 10], "pct", p)
        # month 3: fast (8+7)/2 = 7.5, slow (10+9+8+7)/4 = 8.5 -> gap -11.76%
        self.assertAlmostEqual(line["gap"][3], (7.5 / 8.5 - 1) * 100)
        # month 5: fast 9, slow (8+7+8+10)/4 = 8.25 -> up; turn recorded at index 5
        self.assertAlmostEqual(line["gap"][5], (9 / 8.25 - 1) * 100)
        self.assertEqual(line["dir"], [None, None, None, "down", "down", "up"])
        self.assertEqual(line["turns"], [(5, "down", "up")])
        lvl = mr.make_line("y", [0, 1, 2, 3], [6.0, 6.1, 6.3, 6.6], "level", p)
        self.assertAlmostEqual(lvl["gap"][3], ((6.3 + 6.6) / 2 - 6.25) * 100)          # +20bp

    def test_dead_band_keeps_previous_direction(self):
        dirs, turns = mr.directions([None, -1.0, -0.1, 0.2, 0.3, -0.2], 0.25)
        self.assertEqual(dirs, [None, "down", "down", "down", "up", "up"])
        self.assertEqual(turns, [(4, "down", "up")])

    def test_monthly_and_gaps(self):
        obs = [(date(2026, 1, 5), 1.0), (date(2026, 1, 30), 2.0), (date(2026, 2, 27), 3.0)]
        self.assertEqual({k: v[0] for k, v in mr.to_monthly(obs).items()},
                         {mr.month_of(date(2026, 1, 1)): 2.0, mr.month_of(date(2026, 2, 1)): 3.0})
        months, vals, note = mr.contiguous({1: (1.0, None), 3: (3.0, None), 4: (4.0, None)})
        self.assertEqual((months, vals, note), ([1, 2, 3, 4], [1.0, 1.0, 3.0, 4.0], None))   # one hole bridged
        months, vals, note = mr.contiguous({1: (1.0, None), 5: (5.0, None), 6: (6.0, None)})
        self.assertEqual(months, [5, 6])
        self.assertIn("3 months missing", note)
        months, vals, _ = mr.contiguous({1: (6.5, None), 4: (6.25, None)}, step=True, upto=6)   # repo step series
        self.assertEqual(vals, [6.5, 6.5, 6.5, 6.25, 6.25, 6.25])

    def test_date_order_inference(self):
        self.assertEqual(mr.infer_day_first(["10/31/2026", "09/30/2026"]), (False, False))
        self.assertEqual(mr.infer_day_first(["31/10/2026", "30/09/2026"]), (True, False))
        self.assertEqual(mr.parse_date("2026-07"), date(2026, 7, 1))
        self.assertEqual(mr.resolve("India 10-Year Bond Yield Historical Data"), "IN10Y")
        self.assertEqual(mr.resolve("^NSEI"), "NIFTY")
        self.assertEqual(mr.resolve("GOLDBEES.NS"), "GOLD")
        self.assertEqual(mr.resolve("Repo Rate"), "SHORT")


class TestClassify(unittest.TestCase):
    def test_rules(self):
        c = mr.classify
        self.assertEqual(c({"crude": "up", "rupee": "up", "rates": None})["key"], "squeeze")
        self.assertEqual(c({"crude": "up", "rupee": "up", "rates": "down"})["key"], "mixed")   # a pressure leg falling
        self.assertEqual(c({"concentration": "down", "size": "down", "cyc_def": "down",
                            "equity_gold": "down"})["key"], "risk_off")
        two_down = {"concentration": "up", "cyc_def": "down", "equity_gold": "down", "volatility": "up"}
        self.assertNotEqual(c(two_down)["key"], "risk_off")
        self.assertEqual(c(two_down, {"level": 24.0, "average": 18.0})["key"], "risk_off")   # stressed VIX
        self.assertEqual(c({"concentration": "up", "size": "up", "financials": "down"})["key"], "broadening")
        self.assertEqual(c({"concentration": "down", "financials": "up"})["key"], "narrow")
        self.assertEqual(c({"rates": "down", "curve": "up", "crude": "up"})["key"], "easing")
        self.assertEqual(c({"rates": "down", "crude": "up", "rupee": "up"})["key"], "mixed")  # crude + rupee offset
        self.assertEqual(c({})["key"], "mixed")

    def test_priority_and_also_present(self):
        r = mr.classify({"crude": "up", "rupee": "up", "rates": "up", "concentration": "down"})
        self.assertEqual(r["label"], "Inflationary squeeze")
        self.assertEqual(r["also_present"], ["Narrow / concentrated"])


class TestPlantedTurn(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.res = status_json(TURN)
        cls.by = by_key(cls.res)

    def test_turn_month(self):
        # ratio = 1 + 0.02*|i - 14| with the low in 2026-01. For a symmetric V, mean |distance| over the
        # last 6 months first beats the 12-month one 6 months after the low (3.5 vs 3.0): 2026-07.
        size = self.by["size"]
        self.assertEqual(size["line"], "NIFTY SMALLCAP 250 / NIFTY 50")
        self.assertEqual(size["last_turn"], {"month": "2026-07", "from": "down", "to": "up", "months_ago": 1})
        self.assertEqual(size["state"], "turned up")
        self.assertAlmostEqual(size["value"], 1.14, places=6)                         # 2026-08 = 1 + 0.02*7
        self.assertAlmostEqual(size["gap"], (1.09 / ((1.14 + 1.12 + 1.10 + 1.08 + 1.06 + 1.04 + 1.02 + 1.00 +
                                                      1.02 + 1.04 + 1.06 + 1.08) / 12) - 1) * 100, places=6)

    def test_daily_resampled_and_nulls_skipped(self):
        self.assertEqual(self.res["as_of_month"], "2026-08")
        self.assertEqual(self.res["data_to"], "2026-08-31")
        self.assertFalse(self.res["month_in_progress"])
        rows = mr.load_series(TURN / "NIFTY SMALLCAP 250.csv")
        self.assertNotIn(date(2026, 2, 11), dict(rows))                           # null row dropped

    def test_transition_and_label(self):
        tr = self.res["transition"]
        self.assertEqual((tr["turned"], tr["available"]), (1, 2))
        self.assertEqual(tr["text"], "1 of 2 components turned in the last 3 months")
        self.assertEqual(self.res["regime"]["label"], "Broadening risk-on")
        self.assertEqual(self.by["concentration"]["line"], "NIFTY 50 EQUAL WEIGHT / NIFTY 50")

    def test_as_of_before_turn(self):
        res = status_json(TURN, "--as-of", "2026-06")
        self.assertEqual(res["as_of_month"], "2026-06")
        self.assertEqual(by_key(res)["size"]["direction"], "down")
        self.assertEqual(res["regime"]["label"], "Transitional / mixed")

    def test_history(self):
        code, out, _ = run(["history", "--csv-dir", str(TURN), "--months", "6", "--json"])
        self.assertEqual(code, 0)
        h = strict_json(out)
        self.assertEqual([m["month"] for m in h["months"]],
                         ["2026-03", "2026-04", "2026-05", "2026-06", "2026-07", "2026-08"])
        self.assertEqual([s["label"] for s in h["spells"]], ["Transitional / mixed", "Broadening risk-on"])
        self.assertEqual(h["spells"][1]["start"], "2026-07")
        self.assertEqual(h["changes"], 1)
        self.assertEqual(h["months"][-1]["directions"]["size"], "up")
        self.assertIsNone(h["months"][-1]["directions"]["crude"])

    def test_month_in_progress(self):
        tmp = tempfile.mkdtemp()
        for f in TURN.iterdir():
            shutil.copy(f, tmp)
        with open(os.path.join(tmp, "NIFTY 50.csv"), "a") as fh:
            fh.write("2026-09-01,24000,24100,23900,24000,0\n2026-09-02,24050,24100,23900,24050,0\n")
        with open(os.path.join(tmp, "NIFTY SMALLCAP 250.csv"), "a") as fh:
            fh.write("2026-09-01,27400\n2026-09-02,27500\n")
        res = status_json(tmp)
        self.assertTrue(res["month_in_progress"])
        self.assertTrue(any("still in progress" in w for w in res["warnings"]))
        res = status_json(tmp, "--closed-months")
        self.assertEqual(res["as_of_month"], "2026-08")
        self.assertFalse(res["month_in_progress"])


class TestSqueeze(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.res = status_json(SQUEEZE)
        cls.by = by_key(cls.res)

    def test_label_and_evidence(self):
        r = self.res["regime"]
        self.assertEqual(r["label"], "Inflationary squeeze")
        self.assertEqual(sorted(r["pressure"]["up"]), ["crude", "rates", "rupee"])
        self.assertTrue(any("crude rising" in e for e in r["evidence_for"]))
        self.assertIn("Narrow / concentrated", r["also_present"])
        self.assertTrue(any("OMCs" in x for x in self.res["tilts"]["pressured"]))
        self.assertTrue(self.res["what_would_change"])

    def test_yield_curve_inversion(self):
        # spread = (7.0 + 0.03(i-13)) - (6.6 + 0.08(i-15)) = 1.21 - 0.05i, negative from i = 25 (2026-06)
        cv = self.by["rates"]["curve"]
        self.assertTrue(cv["available"])
        self.assertTrue(cv["inverted"])
        self.assertEqual(cv["inverted_since"], "2026-06")
        self.assertEqual(cv["inverted_months"], 5)
        self.assertAlmostEqual(cv["value"], 1.21 - 0.05 * 29, places=6)
        self.assertEqual(cv["reads_as"], "bear flattening, inverted")
        code, out, _ = run(["status", "--csv-dir", str(SQUEEZE)])
        self.assertIn("inverted since 2026-06", out)

    def test_component_levels(self):
        self.assertEqual(self.by["rates"]["unit"], "bp")
        self.assertAlmostEqual(self.by["rates"]["chg_6m"], 18.0, places=6)      # 0.03 a month for 6 months
        self.assertEqual(self.by["volatility"]["level_band"], "normal")
        self.assertEqual(self.by["equity_gold"]["reads_as"], "gold beating equities")

    def test_missing_series_still_labelled(self):
        tmp = tempfile.mkdtemp()
        for f in SQUEEZE.iterdir():
            if f.stem not in ("BRENT", "USDINR"):
                shutil.copy(f, tmp)
        with open(os.path.join(tmp, "notes.csv"), "w") as fh:
            fh.write("a,b\n1,2\n")
        res = status_json(tmp)
        cov = res["coverage"]
        self.assertEqual(cov["available"], 5)
        self.assertEqual({m["key"] for m in cov["missing"]}, {"size", "financials", "cyc_def", "rupee", "crude"})
        self.assertEqual(res["regime"]["label"], "Narrow / concentrated")     # one pressure leg cannot make a squeeze
        self.assertEqual(res["ignored_files"], ["notes.csv"])
        code, out, _ = run(["status", "--csv-dir", tmp])
        self.assertIn("Missing crude: needs BRENT", out)


class TestEasing(unittest.TestCase):
    def test_label_curve_and_coverage(self):
        res = status_json(EASING)
        by = by_key(res)
        self.assertEqual(res["regime"]["label"], "Easing tailwind")
        cv = by["rates"]["curve"]
        self.assertEqual(cv["line"], "10y minus repo rate")
        self.assertFalse(cv["inverted"])
        self.assertEqual(cv["reads_as"], "bull steepening")
        self.assertAlmostEqual(cv["value"], (7.2 - 0.02 * 21) - 4.75, places=6)   # repo carried forward from Aug
        self.assertEqual(by["crude"]["reads_as"], "crude falling")
        self.assertEqual(res["coverage"]["available"], 4)
        self.assertEqual(res["coverage"]["grade"], "thin")
        self.assertTrue(any("rate-sensitives" in x for x in res["tilts"]["favoured"]))

    def test_component_detail(self):
        code, out, _ = run(["component", "curve", "--csv-dir", str(EASING), "--months", "3", "--json"])
        self.assertEqual(code, 0)
        d = strict_json(out)
        self.assertEqual(d["component"]["key"], "rates")
        self.assertEqual(len(d["series"]["rows"]), 3)
        self.assertEqual(d["curve_series"]["rows"][-1]["month"], "2026-10")
        code, out, _ = run(["component", "rupee", "--csv-dir", str(EASING)])
        self.assertIn("rupee strengthening", out)


class TestCLI(unittest.TestCase):
    def test_text_and_series(self):
        code, out, _ = run(["status", "--csv-dir", str(EASING)])
        self.assertEqual(code, 0)
        self.assertIn("Regime: Easing tailwind", out)
        self.assertIn("components turned in the last 3 months", out)
        code, out, _ = run(["history", "--csv-dir", str(SQUEEZE), "--months", "4"])
        self.assertIn("Inflationary squeeze", out)
        code, out, _ = run(["series", "--json"])
        s = {x["key"]: x for x in strict_json(out)["series"]}
        self.assertEqual(s["NIFTY"]["yahoo"], "confirmed")
        self.assertEqual(s["IN10Y"]["yahoo"], "csv-only")
        self.assertEqual(s["BRENT"]["yahoo"], "unconfirmed")

    def test_json_is_strict_everywhere(self):
        for d in (TURN, SQUEEZE, EASING):
            for argv in (["status"], ["history", "--months", "30"], ["component", "rates"]):
                with self.subTest(d=d.name, argv=argv):
                    code, out, err = run(argv + ["--csv-dir", str(d), "--json"])
                    self.assertEqual(code, 0, err)
                    strict_json(out)

    def test_ticker_overrides(self):
        self.assertEqual(mr.parse_overrides(["USDINR=USDINR=X", "nifty 50=^NSEI"]),
                         {"USDINR": "USDINR=X", "NIFTY": "^NSEI"})

    def test_bad_inputs_exit_cleanly(self):
        tmp = tempfile.mkdtemp()
        empty = os.path.join(tmp, "empty")
        os.mkdir(empty)
        nocol = os.path.join(tmp, "nocol")
        os.mkdir(nocol)
        with open(os.path.join(nocol, "NIFTY 50.csv"), "w") as fh:
            fh.write("when,what\n2026-01-01,x\n")
        only = os.path.join(tmp, "only")
        os.mkdir(only)
        shutil.copy(SQUEEZE / "NIFTY 50.csv", only)
        dup = os.path.join(tmp, "dup")
        shutil.copytree(EASING, dup)
        shutil.copy(EASING / "BRENT.csv", os.path.join(dup, "BZ=F.csv"))
        for argv in (["status", "--csv-dir", os.path.join(tmp, "missing")],
                     ["status", "--csv-dir", empty],
                     ["status", "--csv-dir", nocol],
                     ["status", "--csv-dir", only],                               # no component computable
                     ["status", "--csv-dir", dup],                                # two files for one series
                     ["status", "--csv-dir", str(EASING), "--fast", "12", "--slow", "6"],
                     ["status", "--csv-dir", str(EASING), "--as-of", "someday"],
                     ["status", "--csv-dir", str(EASING), "--as-of", "1990-01-01"],
                     ["status", "--csv-dir", str(EASING), "--ticker", "BRENT"],
                     ["status", "--csv-dir", str(EASING), "--ticker", "COPPER=HG=F"],
                     ["history", "--csv-dir", str(EASING), "--months", "0"],
                     ["component", "nonsense", "--csv-dir", str(EASING)]):
            with self.subTest(argv=argv):
                code, out, err = run(argv)
                self.assertEqual(code, 2)
                self.assertTrue(err.startswith("error:"), err)
                self.assertEqual(len(err.strip().splitlines()), 1)
                self.assertNotIn("Traceback", err)


if __name__ == "__main__":
    unittest.main()
