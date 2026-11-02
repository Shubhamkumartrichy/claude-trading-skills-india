import contextlib
import io
import json
import math
import os
import tempfile
import unittest
from datetime import date, timedelta

from _load import FIXTURES, load

pt = load("pair-trading", "pairs")
STK = FIXTURES / "pairs_stocks"
MEMBERS = FIXTURES / "pairs_members.csv"
LOTS = FIXTURES / "pairs_lots.csv"
BAN = FIXTURES / "pairs_secban.csv"


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = pt.main(argv)
    return code, out.getvalue(), err.getvalue()


def strict_json(text):
    def bad(x):
        raise ValueError(f"non-standard JSON constant {x}")
    return json.loads(text, parse_constant=bad)


def logs(a, b, lookback=500):
    dates, pa, pb = pt.align(pt.load_prices(STK / f"{a}.csv"), pt.load_prices(STK / f"{b}.csv"), lookback)
    return dates, pa, pb, [math.log(x) for x in pa], [math.log(x) for x in pb]


class TestMaths(unittest.TestCase):
    def test_ols_hand_checked(self):
        self.assertEqual(pt.ols([1, 2, 3, 4, 5], [3, 5, 7, 9, 11]), (1.0, 2.0))
        # x mean 2.5, y mean 4: Sxy = 7, Sxx = 5 -> slope 1.4, intercept 4 - 1.4*2.5 = 0.5
        a, b = pt.ols([1, 2, 3, 4], [2, 3, 5, 6])
        self.assertAlmostEqual(b, 1.4)
        self.assertAlmostEqual(a, 0.5)
        with self.assertRaises(pt.PairError):
            pt.ols([2, 2, 2], [1, 2, 3])

    def test_tls_and_correlation(self):
        self.assertAlmostEqual(pt.tls_slope([1, 2, 3, 4, 5], [3, 5, 7, 9, 11]), 2.0)
        x, y = [1, 2, 3, 4, 5, 6], [2.1, 3.9, 6.2, 7.8, 10.3, 11.7]
        self.assertAlmostEqual(pt.tls_slope(x, y) * pt.tls_slope(y, x), 1.0)    # symmetric
        lo, hi = pt.ols(x, y)[1], 1 / pt.ols(y, x)[1]
        self.assertTrue(lo <= pt.tls_slope(x, y) <= hi)
        self.assertAlmostEqual(pt.pearson([1, 2, 3], [2, 4, 6]), 1.0)
        self.assertAlmostEqual(pt.pearson([1, 2, 3], [3, 2, 1]), -1.0)
        self.assertIsNone(pt.pearson([1, 2, 3], [5, 5, 5]))

    def test_half_life_exact_ar1(self):
        s = [10 * 0.9 ** t for t in range(50)]          # s_t = 0.9 s_(t-1) exactly
        hl = pt.half_life(s)
        self.assertAlmostEqual(hl["phi"], 0.9)
        self.assertAlmostEqual(hl["sessions"], math.log(0.5) / math.log(0.9))   # 6.5788
        self.assertAlmostEqual(hl["sessions"], 6.5788, places=3)
        self.assertIsNone(pt.half_life([1.05 ** t for t in range(30)])["sessions"])

    def test_adf_lag0_closed_form(self):
        e = [((t * 7) % 11 - 5) / 5 + 0.01 * t for t in range(40)]
        de = [e[t] - e[t - 1] for t in range(1, len(e))]
        lagged = e[:-1]
        sxx = sum(v * v for v in lagged)
        gamma = sum(a * b for a, b in zip(lagged, de)) / sxx
        ssr = sum((d - gamma * v) ** 2 for v, d in zip(lagged, de))
        t = gamma / math.sqrt(ssr / (len(de) - 1) / sxx)
        res = pt.adf(e, lag=0)
        self.assertAlmostEqual(res["gamma"], gamma)
        self.assertAlmostEqual(res["t_stat"], t)
        self.assertEqual((res["lag"], res["lag_method"], res["nobs"]), (0, "fixed", 39))

    def test_mackinnon_values(self):
        cv = pt.eg_critical(100)
        self.assertAlmostEqual(cv["5%"], -3.33613 - 6.1101 / 100 - 6.823 / 100 ** 2)
        self.assertAlmostEqual(cv["1%"], -4.0093, places=4)
        big = pt.eg_critical(10 ** 9)
        self.assertAlmostEqual(big["10%"], -3.04445, places=4)
        # the p-value surface reproduces the asymptotic critical values
        self.assertAlmostEqual(pt.eg_pvalue(-3.89644), 0.01, delta=0.001)
        self.assertAlmostEqual(pt.eg_pvalue(-3.33613), 0.05, delta=0.002)
        self.assertAlmostEqual(pt.eg_pvalue(-3.04445), 0.10, delta=0.003)
        self.assertAlmostEqual(pt.eg_pvalue(-2.62), pt.eg_pvalue(-2.6199), delta=0.002)   # continuous
        self.assertLess(pt.eg_pvalue(-5), pt.eg_pvalue(-3))
        self.assertEqual(pt.eg_pvalue(-25), 0.0)
        self.assertEqual(pt.eg_pvalue(2), 1.0)
        # Engle-Granger tables sit left of plain Dickey-Fuller (5% DF with constant is about -2.86)
        self.assertLess(big["5%"], -3.3)

    def test_rolling_z(self):
        z = pt.rolling_z([1, 2, 3, 1, 2, 6], 3)
        self.assertEqual(z[:2], [None, None])
        self.assertAlmostEqual(z[2], 1.0)                 # mean 2, sd 1
        self.assertAlmostEqual(z[5], 3 / math.sqrt(7))    # [1,2,6]: mean 3, sd sqrt(7)
        self.assertIsNone(pt.rolling_z([5, 5, 5], 3)[2])

    def test_json_safety(self):
        self.assertEqual(pt._safe({"x": float("nan"), "y": [float("inf")], "_hidden": 1, "d": date(2026, 1, 2)}),
                         {"x": None, "y": [None], "d": "2026-01-02"})


class TestPlantedPair(unittest.TestCase):
    def test_planted_pair_is_cointegrated(self):
        # fixtures: ln BANKB = c + 1.5 ln BANKA + u, u AR(1) with phi 0.9
        _, _, _, lb_, la_ = logs("BANKB", "BANKA")
        eg = pt.engle_granger(lb_, la_)
        self.assertAlmostEqual(eg["beta"], 1.5, delta=0.05)
        self.assertLess(eg["adf"]["t_stat"], eg["crit"]["5%"])
        self.assertIn(eg["reject_at"], ("1%", "5%"))
        self.assertLess(eg["p_value"], 0.01)
        rev = pt.engle_granger(la_, lb_)
        self.assertIn(rev["reject_at"], ("1%", "5%"))
        hl = pt.half_life(eg["resid"])
        self.assertAlmostEqual(hl["sessions"], math.log(0.5) / math.log(0.9), delta=1.5)

    def test_independent_walks_not_cointegrated(self):
        for a, b in (("METALA", "METALB"), ("BANKA", "BANKC")):
            _, _, _, la_, lb_ = logs(a, b)
            for y, x in ((la_, lb_), (lb_, la_)):
                eg = pt.engle_granger(y, x)
                with self.subTest(pair=f"{a}/{b}"):
                    self.assertNotIn(eg["reject_at"], ("1%", "5%"))
                    self.assertGreater(eg["p_value"], 0.10)

    def test_null_candle_skipped(self):
        rows = pt.load_prices(STK / "BANKA.csv")
        self.assertEqual(len(rows), 599)
        self.assertNotIn(date(2024, 12, 2), {d for d, _, _ in rows})
        dates, _, _ = pt.align(rows, pt.load_prices(STK / "BANKB.csv"), 0)
        self.assertEqual(len(dates), 599)

    def test_full_report(self):
        code, out, _ = run(["test", "BANKB", "BANKA", "--csv-dir", str(STK), "--json"])
        self.assertEqual(code, 0)
        r = strict_json(out)
        self.assertEqual(r["sessions"], 500)
        self.assertAlmostEqual(r["hedge"]["beta_ols"], 1.5, delta=0.05)
        self.assertGreater(r["correlation_returns"], 0.8)
        self.assertEqual(r["stability"]["label"], "stable")
        self.assertEqual(len(r["stability"]["periods"]), 3)
        self.assertTrue(r["verdict"].startswith("Cointegrated at 1%"))
        self.assertEqual(set(r["engle_granger"]["critical_values"]), {"1%", "5%", "10%"})
        code, out, _ = run(["test", "BANKB", "BANKA", "--csv-dir", str(STK), "--end", "2026-03-31",
                            "--lookback", "250", "--json"])
        r = strict_json(out)
        self.assertEqual((r["to"], r["sessions"]), ("2026-03-31", 250))
        code, out, _ = run(["test", "METALA", "METALB", "--csv-dir", str(STK)])
        self.assertIn("unit root NOT rejected", out)
        self.assertIn("MacKinnon", out)


class TestSignals(unittest.TestCase):
    D = [date(2026, 1, 1) + timedelta(days=i) for i in range(12)]

    def sim(self, z, exit_=0.0, max_hold=None, pa=None, pb=None):
        pa = pa or [100.0] * len(z)
        pb = pb or [100.0] * len(z)
        return pt.simulate(self.D[:len(z)], pa, pb, 1.0, z, 2.0, exit_, 3.5, max_hold)

    def test_entries_exits_and_stop(self):
        z = [None, 0.0, 1.0, 2.1, 1.5, 0.3, -0.1, -2.5, -3.6, -1.0, 0.2]
        res = self.sim(z)
        got = [(t["side"], t["entry_date"], t["exit_date"], t["exit_reason"]) for t in res["trades"]]
        self.assertEqual(got, [("short spread", "2026-01-04", "2026-01-07", "target"),
                               ("long spread", "2026-01-08", "2026-01-09", "stop")])
        self.assertIsNone(res["open_trade"])
        self.assertEqual(res["summary"]["exits"], {"target": 1, "stop": 1})

    def test_exit_half_and_time_stop(self):
        z = [None, 0.0, 1.0, 2.1, 1.5, 0.3, -0.1]
        self.assertEqual(self.sim(z, exit_=0.5)["trades"][0]["exit_date"], "2026-01-06")      # 0.3 <= 0.5
        t = self.sim(z, max_hold=2)["trades"][0]
        self.assertEqual((t["exit_date"], t["exit_reason"], t["bars"]), ("2026-01-06", "time", 2))

    def test_no_reentry_until_back_inside_band(self):
        # stop at 3.6, z stays above 2: no new short until |z| has dipped below 2
        z = [0.0, 2.2, 3.6, 2.5, 2.4, 1.0, 2.3, 2.0]
        res = self.sim(z)
        self.assertEqual([(t["entry_date"], t["exit_reason"]) for t in res["trades"]],
                         [("2026-01-02", "stop")])
        self.assertEqual(res["open_trade"]["entry_date"], "2026-01-07")
        # first valid z already beyond entry is not a fresh cross; a jump straight past the stop is skipped
        self.assertEqual(self.sim([2.5, 2.4, 0.0])["trades"], [])
        self.assertIsNone(self.sim([0.0, 3.8, 1.0])["open_trade"])
        self.assertEqual(self.sim([0.0, 3.8, 3.0, 1.0, 2.2])["open_trade"]["entry_date"], "2026-01-05")

    def test_trade_return(self):
        # short spread (z high): A falls 2%, B flat, beta 1 -> +2% / (1 + 1) = +1% of gross
        z = [0.0, 2.2, 0.0]
        res = self.sim(z, pa=[100, 100, 98], pb=[50, 50, 50])
        self.assertAlmostEqual(res["trades"][0]["return_pct"], 1.0)
        self.assertEqual(res["summary"]["hit_rate_pct"], 100.0)

    def test_cli_signal_on_fixture(self):
        code, out, _ = run(["signal", "BANKB", "BANKA", "--csv-dir", str(STK), "--json"])
        self.assertEqual(code, 0)
        r = strict_json(out)
        self.assertGreater(r["summary"]["closed_trades"], 3)
        self.assertEqual(r["rules"]["max_hold_bars"], math.ceil(2 * r["half_life_sessions"]))
        for t in r["trades"]:
            self.assertGreaterEqual(abs(t["z_entry"]), 2.0)
            self.assertEqual(t["side"] == "short spread", t["z_entry"] > 0)
            self.assertIn(t["exit_reason"], ("target", "stop", "time"))
        s = r["summary"]
        self.assertAlmostEqual(s["hit_rate_pct"], s["wins"] / s["closed_trades"] * 100)
        self.assertTrue(any("IN-SAMPLE" in n for n in r["notes"]))
        code, out, _ = run(["signal", "BANKB", "BANKA", "--csv-dir", str(STK), "--exit", "0.5", "--max-hold", "5"])
        self.assertIn("time stop 5 bars", out)
        self.assertIn("IN-SAMPLE", out)


class TestScreen(unittest.TestCase):
    def test_planted_pair_ranks_first(self):
        code, out, _ = run(["screen", "--members", str(MEMBERS), "--csv-dir", str(STK), "--json"])
        self.assertEqual(code, 0)
        r = strict_json(out)
        self.assertEqual(r["pairs_tested"], 4)                   # BANKS: A,B,C -> 3; METALS: A,B -> 1
        top = r["results"][0]
        self.assertEqual({top["a"], top["b"]}, {"BANKA", "BANKB"})
        self.assertEqual(top["status"], "candidate")
        self.assertTrue(top["passes_bonferroni"])
        self.assertAlmostEqual(r["bonferroni_threshold"], 0.05 / 4)
        self.assertTrue(all(x["status"] == "reject" for x in r["results"][1:]))
        reasons = {x["symbol"]: x["reason"] for x in r["filtered"]}
        self.assertIn("min-turnover", reasons["BANKD"])
        self.assertIn("min-bars", reasons["METALC"])
        self.assertIn("NOSUCH", reasons)

    def test_filters_are_configurable(self):
        code, out, _ = run(["screen", "--members", str(MEMBERS), "--csv-dir", str(STK), "--min-turnover", "0",
                            "--json"])
        self.assertEqual(strict_json(out)["pairs_tested"], 7)    # BANKD joins: 6 bank pairs + 1 metal
        code, out, _ = run(["screen", "--members", str(MEMBERS), "--csv-dir", str(STK), "--top", "2"])
        self.assertIn("Bonferroni", out)
        self.assertIn("2 more", out)

    def test_degenerate_pair_is_skipped_not_fatal(self):
        tmp = tempfile.mkdtemp()
        for name, src in (("BANKA", "BANKA"), ("BANKB", "BANKB"), ("TWIN", "BANKA")):
            with open(STK / f"{src}.csv") as fh, open(os.path.join(tmp, f"{name}.csv"), "w") as out:
                out.write(fh.read())
        members = os.path.join(tmp, "members.csv")
        with open(members, "w") as fh:
            fh.write("symbol,sector\nBANKA,X\nBANKB,X\nTWIN,X\n")
        code, out, _ = run(["screen", "--members", members, "--csv-dir", tmp, "--json"])
        self.assertEqual(code, 0)
        r = strict_json(out)
        self.assertEqual([x["pair"] for x in r["skipped_pairs"]], ["BANKA/TWIN"])
        self.assertEqual(r["pairs_tested"], 2)


class TestPlan(unittest.TestCase):
    def test_lot_rounding_and_mismatch(self):
        c = pt.lot_combos(1.5, 1000, 500, 100, 300, 1)[0]
        self.assertEqual((c["lots_a"], c["lots_b"]), (1, 1))     # Rs 1L of A needs Rs 1.5L of B = 1 lot
        self.assertAlmostEqual(c["mismatch_pct"], 0.0)
        combos = pt.lot_combos(1.5, 1000, 500, 100, 250, 5)
        self.assertEqual(combos[0]["lots_b"], 1)                 # 1.2 lots rounds to 1
        self.assertAlmostEqual(combos[0]["mismatch_pct"], -100 / 6)
        self.assertEqual((combos[4]["lots_a"], combos[4]["lots_b"]), (5, 6))
        self.assertAlmostEqual(combos[4]["mismatch_pct"], 0.0)
        legs = pt.size_legs(1.5, 1000, 500, 100, 250, 1, "futures", -1)
        self.assertAlmostEqual(legs["unhedged_b_rs"], 125000 - 150000)
        cash = pt.size_legs(1.5, 1000, 500, 100, 250, 1, "cash-long", -1)   # A short futures, B bought in cash
        self.assertEqual((cash["lots_a"], cash["qty_b"]), (1, 300))
        self.assertAlmostEqual(cash["mismatch_pct"], 0.0)

    def test_nse_lot_file_and_ban_list(self):
        lots = pt.load_lots(str(LOTS))
        self.assertEqual(lots["BANKA"]["lot"], 500)
        self.assertEqual([v for _, v in lots["BANKB"]["months"]], [250, 250, 300])
        self.assertEqual(pt.load_ban_list(str(BAN), "xyz"), {"METALA", "SOMEOTHER", "XYZ"})

    def test_plan_from_fixture(self):
        code, out, _ = run(["plan", "BANKB", "BANKA", "--csv-dir", str(STK), "--lots-csv", str(LOTS),
                            "--as-of", "2026-11-02", "--side", "short", "--ban-list", str(BAN),
                            "--mwpl-b", "85", "--json"])
        self.assertEqual(code, 0)
        r = strict_json(out)
        a, b = r["legs"]
        self.assertEqual((a["symbol"], a["action"], a["lots"], a["qty"]), ("BANKB", "SELL", 1, 250))
        self.assertEqual((b["symbol"], b["action"]), ("BANKA", "BUY"))
        ideal = r["beta"] * a["notional"] / (500 * b["price"])
        self.assertEqual(b["lots"], max(1, round(ideal)))
        self.assertAlmostEqual(r["hedge"]["mismatch_pct"], (b["notional"] / (r["beta"] * a["notional"]) - 1) * 100)
        # 24 Nov 2026 is an NSE holiday, so the last-Tuesday expiry moves to Monday 23 Nov
        self.assertEqual(r["rollover"]["contract_expiry"], "2026-11-23")
        self.assertEqual(r["rollover"]["rolls"][0]["roll_by"], "2026-11-18")
        rt = pt.charges.calculate("futures", a["price"], a["price"], a["qty"])["total_charges"]
        self.assertAlmostEqual(r["charges"]["round_trip"]["BANKB"], rt)
        self.assertAlmostEqual(r["charges"]["total"], sum(r["charges"]["round_trip"].values())
                               + r["charges"]["rolls"] * r["charges"]["per_roll"])
        self.assertAlmostEqual(r["margin"]["total"], 0.25 * (a["notional"] + b["notional"]))
        self.assertIn("not guaranteed", r["margin"]["note"])
        self.assertIn("not in the supplied ban list", r["fo_ban"]["BANKB"])
        self.assertIn("close to the 95% ban", r["fo_ban"]["BANKA"])
        self.assertTrue(any(x.startswith("BANKB: lot size") for x in r["lot_revision"]))
        self.assertTrue(any("auction" in x for x in r["india_notes"]))

    def test_plan_offline_inputs_and_ban(self):
        code, out, _ = run(["plan", "METALA", "BANKA", "--beta", "1.2", "--price-a", "150", "--price-b", "1000",
                            "--lot-b", "500", "--lots-csv", str(LOTS), "--side", "short", "--banned", "metala",
                            "--as-of", "2026-12-20"])
        self.assertEqual(code, 0)
        self.assertIn("IN F&O BAN", out)
        self.assertIn("cannot open this pair", out)
        self.assertIn("roll by 2026-12-23", out)                 # 25 Dec holiday skipped
        self.assertIn("reward/risk not estimated", out)

    def test_cash_long_mode(self):
        code, out, _ = run(["plan", "BANKB", "BANKA", "--csv-dir", str(STK), "--lot-a", "250", "--lot-b", "500",
                            "--side", "long", "--legs", "cash-long", "--as-of", "2026-11-02", "--json"])
        r = strict_json(out)
        self.assertEqual(r["legs"][0]["instrument"], "cash (CNC delivery)")
        self.assertIsNone(r["legs"][0]["lots"])
        self.assertLess(abs(r["hedge"]["mismatch_pct"]), 1.0)
        self.assertEqual(r["lot_combos"], [])


class TestCLIErrors(unittest.TestCase):
    def test_bad_inputs_exit_cleanly(self):
        tmp = tempfile.mkdtemp()
        bad_members = os.path.join(tmp, "m.csv")
        with open(bad_members, "w") as fh:
            fh.write("a,b\nx,y\n")
        with open(os.path.join(tmp, "TINY.csv"), "w") as fh:
            fh.write("Date,Close\n2026-10-29,100\n2026-10-30,null\n")
        with open(os.path.join(tmp, "NOCLOSE.csv"), "w") as fh:
            fh.write("Date,Open\n2026-10-29,100\n")
        s = str(STK)
        for argv in (["test", "BANKB"],
                     ["test", "BANKB", "BANKA", "--csv-dir", os.path.join(tmp, "missing")],
                     ["test", "BANKB", "NOSUCH", "--csv-dir", s],
                     ["test", "BANKB", "BANKB", "--csv-dir", s],
                     ["test", "BANKB", "TINY", "--csv-dir", tmp],
                     ["test", "NOCLOSE", "TINY", "--csv-dir", tmp],
                     ["test", "BANKB", "BANKA", "--csv-dir", s, "--window", "3"],
                     ["test", "BANKB", "BANKA", "--csv-dir", s, "--end", "31-03-2026"],
                     ["test", "BANKB", "BANKA", "--csv-dir", s, "--end", "2023-01-01"],
                     ["signal", "BANKB", "BANKA", "--csv-dir", s, "--entry", "4"],
                     ["signal", "BANKB", "BANKA", "--csv-dir", s, "--lookback", "abc"],
                     ["screen", "--csv-dir", s],
                     ["screen", "--members", bad_members, "--csv-dir", s],
                     ["plan", "BANKB", "BANKC", "--csv-dir", s],
                     ["plan", "BANKB", "BANKA", "--csv-dir", s, "--lots-csv", str(LOTS), "--broker", "nope"],
                     ["plan", "BANKB", "BANKA", "--csv-dir", s, "--lot-a", "-5", "--lot-b", "10"],
                     ["plan", "X", "Y", "--beta", "-1", "--price-a", "1", "--price-b", "1", "--lot-a", "1",
                      "--lot-b", "1"],
                     ["nonsense"]):
            with self.subTest(argv=argv):
                code, out, err = run(argv)
                self.assertEqual(code, 2)
                self.assertTrue(err.startswith("error:"), err)
                self.assertEqual(len(err.strip().splitlines()), 1)
                self.assertNotIn("Traceback", err)


if __name__ == "__main__":
    unittest.main()
