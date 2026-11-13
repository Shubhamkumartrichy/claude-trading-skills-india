import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from _load import FIXTURES, load

sc = load("stock-screener", "screen")

EXPORT = str(FIXTURES / "screener_export.csv")
GENERIC = str(FIXTURES / "screener_generic.csv")
PRICES = str(FIXTURES / "screener_prices")


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = sc.main(argv)
    return code, out.getvalue(), err.getvalue()


def strict_json(text):
    def reject(c):
        raise ValueError(f"non-standard JSON constant {c}")
    return json.loads(text, parse_constant=reject)


def run_json(argv):
    code, out, err = run(argv + ["--json"])
    if code != 0:
        raise AssertionError(err)
    return strict_json(out)


def names(items):
    return [x["name"] for x in items]


def preset(name, *extra, csv=EXPORT):
    return run_json(["run", "--csv", csv, "--preset", name, "--top", "50", *extra])


class TestHeaderMapping(unittest.TestCase):
    def test_screener_headers(self):
        want = {"S.No.": None, "Name": "name", "Company": "name", "CMP Rs.": "cmp", "P/E": "pe",
                "Mar Cap Rs.Cr.": "mcap", "Div Yld %": "div_yield", "NP Qtr Rs.Cr.": "np_qtr",
                "Qtr Profit Var %": "qtr_profit_var", "Sales Qtr Rs.Cr.": "sales_qtr",
                "Qtr Sales Var %": "qtr_sales_var", "ROCE %": "roce", "ROE %": "roe", "Debt / Eq": "de",
                "CMP / BV": "pb", "EPS 12M Rs.": "eps", "Sales Var 3Yrs %": "sales_g3",
                "Profit Var 5Yrs %": "profit_g5", "Prom. Hold. %": "promoter", "Pledged %": "pledge",
                "Chg in FII Hold %": "fii_chg", "Industry": "industry", "52w High Rs.": "high52",
                "1Yr return %": "ret_1y", "QoQ Profits %": None, "Div Payout %": "payout"}
        for header, field in want.items():
            with self.subTest(header=header):
                self.assertEqual(sc.classify(header), field)

    def test_screener_query_names_as_headers(self):
        want = {"Price to Earning": "pe", "Market Capitalization": "mcap", "Return on capital employed": "roce",
                "Average return on capital employed 5Years": "roce_5y", "Profit growth 3Years": "profit_g3",
                "Sales growth": "sales_g1", "Pledged percentage": "pledge", "Promoter holding": "promoter",
                "Change in FII holding": "fii_chg", "Down from 52w high": "down_from_high",
                "Return over 1year": "ret_1y", "Return over 6months": "ret_6m", "PEG Ratio": "peg",
                "Price to book value": "pb", "Dividend Payout Ratio": "payout", "Book value": "book_value",
                "YOY Quarterly profit growth": "qtr_profit_var", "Industry PE": "industry_pe",
                "Free cash flow last year": "fcf", "Interest Coverage Ratio": "int_cov"}
        for header, field in want.items():
            with self.subTest(header=header):
                self.assertEqual(sc.classify(header), field)

    def test_number_cleaning(self):
        self.assertEqual(sc.to_num("1,23,456.70", "x"), 123456.7)
        self.assertEqual(sc.to_num("6.0%", "x"), 6.0)
        self.assertEqual(sc.to_num("(45)", "x"), -45.0)
        self.assertEqual(sc.to_num("₹ 1,480", "x"), 1480.0)
        for blank in ("", "-", "—", "NA", "n/a", None):
            self.assertIsNone(sc.to_num(blank, "x"))
        with self.assertRaises(sc.ScreenError):
            sc.to_num("abc", "x")

    def test_export_loaded_and_cleaned(self):
        data = sc.load_companies(EXPORT)
        self.assertEqual(len(data["companies"]), 21)
        by = {c["name"]: c for c in data["companies"]}
        self.assertAlmostEqual(by["Pragati Bank Ltd"]["v"]["mcap"], 123456.7)
        self.assertEqual(by["Coal Dividend Corp Ltd"]["v"]["div_yield"], 6.0)
        self.assertIsNone(by["Loss Maker Ltd"]["v"]["pe"])
        self.assertEqual(data["columns"]["de"], "Debt / Eq")

    def test_generic_snake_case_and_map(self):
        data = sc.load_companies(GENERIC)
        self.assertIn("Distribution Ratio", data["unmapped"])
        by = {c["symbol"]: c for c in data["companies"]}
        self.assertEqual(by["ALPHAQ"]["v"]["cmp"], 1250.5)
        self.assertEqual(by["ALPHAQ"]["v"]["roce"], 28.0)
        self.assertEqual(by["DELTAH"]["v"]["fcf"], -45.0)
        mapped = sc.load_companies(GENERIC, sc.parse_maps(["payout=Distribution Ratio"]))
        self.assertEqual(mapped["columns"]["payout"], "Distribution Ratio")
        code, _, err = run(["run", "--csv", GENERIC, "--where", "roce>1", "--map", "payout=Nope Column"])
        self.assertEqual(code, 2)
        self.assertIn("no column named", err)

    def test_derived_fields(self):
        data = sc.load_companies(EXPORT)
        sc.mark_financials(data)
        sc.derive(data)
        by = {c["name"]: c["v"] for c in data["companies"]}
        self.assertAlmostEqual(by["Leader Electronics Ltd"]["down_from_high"], 2.0)
        self.assertAlmostEqual(by["Leader Electronics Ltd"]["inst_chg"], 2.0)
        self.assertEqual(by["Tiny Tech SME Ltd"]["rs_pct"], 99.0)            # best 1-year return in the file
        self.assertGreaterEqual(by["Leader Electronics Ltd"]["rs_pct"], 90.0)
        self.assertEqual(by["Neg Worth Infra Ltd"]["rs_pct"], 1.0)


class TestPresets(unittest.TestCase):
    def test_quality_compounder(self):
        res = preset("quality-compounder")
        passed = names(res["passed"])
        self.assertEqual(passed[0], "Compounder Labs Ltd")
        self.assertLess(passed.index("Compounder Labs Ltd"), passed.index("Steady Pharma Ltd"))
        near = {j["name"]: j["failed"] for j in res["near_misses"]}
        self.assertEqual(near["Near Miss Cement Ltd"], ["Promoter"])
        gated = names(res["gated"])
        self.assertIn("Pledge Motors Ltd", gated)
        self.assertNotIn("Pledge Motors Ltd", passed)
        scores = [j["score"] for j in res["passed"]]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_value_dividend_and_gsec_bar(self):
        res = preset("value-dividend")
        self.assertEqual(names(res["passed"]), ["Coal Dividend Corp Ltd"])
        near = {j["name"]: j["failed"] for j in res["near_misses"]}
        self.assertEqual(near["Near Value Textiles Ltd"], ["Yield"])
        trap = next(j for j in res["failed"] if j["name"] == "Yield Trap Ltd")
        self.assertTrue({"Payout", "Growth", "ROE"} <= set(trap["failed"]))
        self.assertEqual(res["gates"]["dividend_yield_bar_pct"], 2.8)
        lower = preset("value-dividend", "--gsec", "5")                     # bar 2.0%
        self.assertIn("Near Value Textiles Ltd", names(lower["passed"]))

    def test_garp(self):
        res = preset("garp")
        self.assertEqual(names(res["passed"]), ["Garp Auto Parts Ltd"])
        pricey = next(j for j in res["failed"] if j["name"] == "Pricey Platform Ltd")
        self.assertEqual(set(pricey["failed"]), {"PEG", "PE"})
        self.assertIn("Loss Maker Ltd", names(res["incomplete"]))

    def test_canslim_india(self):
        res = preset("canslim-india")
        passed = names(res["passed"])
        self.assertEqual(passed[0], "Leader Electronics Ltd")
        near = {j["name"]: j["failed"] for j in res["near_misses"]}
        self.assertEqual(near["Almost Leader Tech Ltd"], ["I"])
        lag = next(j for j in res["failed"] if j["name"] == "Laggard Sugar Ltd")
        self.assertEqual(set(lag["failed"]), {"N", "L"})
        turn = next(j for j in res["passed"] if j["name"] == "Turnaround Metals Ltd")
        self.assertIn("low-base?", turn["flags"])
        self.assertTrue(any("market-regime" in n for n in res["notes"]))

    def test_dividend_growth_pullback_with_prices(self):
        res = preset("dividend-growth-pullback", "--prices-dir", PRICES)
        self.assertEqual(names(res["passed"]), ["Grower Consumer Ltd"])
        hot = next(j for j in res["near_misses"] if j["name"] == "Grower Hot Ltd")
        self.assertEqual(hot["failed"], ["Pullback"])
        rsi = next(c["value"] for c in hot["checks"] if c["field"] == "rsi")
        self.assertGreater(rsi, 60)
        self.assertTrue(any("RSI(14) from daily closes" in n for n in res["notes"]))

    def test_dividend_growth_pullback_without_prices(self):
        res = preset("dividend-growth-pullback")
        self.assertEqual(set(names(res["passed"])), {"Grower Consumer Ltd", "Grower Hot Ltd"})
        pull = next(r for r in res["rules"] if r["rule"] == "Pullback")
        self.assertFalse(pull["checked"])
        self.assertTrue(any("NOT CHECKED" in n for n in res["notes"]))

    def test_presets_listing(self):
        code, out, _ = run(["presets"])
        self.assertEqual(code, 0)
        for name in ("canslim-india", "value-dividend", "dividend-growth-pullback", "garp", "quality-compounder"):
            self.assertIn(name, out)
        self.assertIn("₹10,000", out)
        data = run_json(["presets"])
        self.assertEqual(len(data["presets"]), 5)


class TestGatesAndFinancials(unittest.TestCase):
    def test_gates(self):
        res = preset("quality-compounder")
        gated = {j["name"]: " ".join(j["reasons"]) for j in res["gated"]}
        self.assertIn("pledge 35.0%", gated["Pledge Motors Ltd"])
        self.assertIn("₹500 cr floor", gated["Tiny Tech SME Ltd"])
        self.assertIn("negative net worth", gated["Neg Worth Infra Ltd"])
        self.assertIn("Blank Data Ltd", names(res["incomplete"]))

    def test_pledge_threshold_and_no_gates(self):
        res = preset("quality-compounder", "--max-pledge", "40")
        self.assertNotIn("Pledge Motors Ltd", names(res["gated"]))
        res = preset("quality-compounder", "--no-gates")
        self.assertEqual(res["gated"], [])
        self.assertEqual(names(res["incomplete"]), ["Blank Data Ltd"])   # no rule could be judged at all
        self.assertIn("Tiny Tech SME Ltd", names(res["passed"]))
        self.assertFalse(res["gates"]["enabled"])

    def test_bank_skips_debt_and_roce(self):
        res = preset("quality-compounder")
        bank = next(j for j in res["passed"] if j["name"] == "Pragati Bank Ltd")
        self.assertTrue(bank["financial"])
        status = {c["rule"]: c["status"] for c in bank["checks"]}
        self.assertEqual((status["ROCE"], status["Debt"], status["FCF"]), ("skipped",) * 3)
        res = preset("quality-compounder", "--financials", "none")
        self.assertNotIn("Pragati Bank Ltd", names(res["passed"]) + names(res["near_misses"]))

    def test_nbfc_detected_from_industry(self):
        data = sc.load_companies(EXPORT)
        sc.mark_financials(data)
        fins = {c["name"] for c in data["companies"] if c["financial"]}
        self.assertEqual(fins, {"Pragati Bank Ltd", "Vikas Finance Ltd"})

    def test_name_guess_without_industry_and_user_filters_apply(self):
        res = run_json(["run", "--csv", GENERIC, "--where", "pe<25"])
        self.assertTrue(any("no Industry column" in n for n in res["notes"]))
        sahyog = next(j for j in res["passed"] if j["name"] == "Sahyog Finance Ltd")
        self.assertTrue(sahyog["financial"])
        res = run_json(["run", "--csv", GENERIC, "--where", "roce>20"])
        self.assertNotIn("Sahyog Finance Ltd", names(res["passed"]))   # --where is applied to lenders too


class TestFilterLanguage(unittest.TestCase):
    def test_parse(self):
        rules = sc.parse_where(["roce>20", "P/E < 25".replace("/", ""), "pledge<=5 and de == 0", "mcap=1000"])
        got = [(r.field, r.op, r.value) for r in rules]
        self.assertEqual(got, [("roce", ">", 20.0), ("pe", "<", 25.0), ("pledge", "<=", 5.0), ("de", "==", 0.0),
                               ("mcap", "==", 1000.0)])
        self.assertEqual(sc.parse_where(["Debt_to_Equity<0.5"])[0].field, "de")

    def test_rejects_unsafe_and_malformed(self):
        for bad in ("__import__('os').system('ls')", "roce>20;rm -rf /", "len(name)>2", "roce>pe",
                    "roce >> 2", "roce>", "exec('x')", "roce>2 or 1", "eval>1", "roce>1e3"):
            with self.subTest(bad=bad):
                code, out, err = run(["run", "--csv", EXPORT, "--where", bad])
                self.assertEqual(code, 2)
                self.assertTrue(err.startswith("error:"), err)
                self.assertEqual(len(err.strip().splitlines()), 1)
                self.assertNotIn("Traceback", err)
                self.assertEqual(out, "")

    def test_custom_run_sort_and_near_miss(self):
        res = run_json(["run", "--csv", GENERIC, "--where", "roce>20", "--where", "pe<25", "--where", "pledge<=5",
                        "--sort", "roce", "--financials", "none"])
        self.assertEqual(res["sort"], "roce")
        near = {j["name"]: j["failed"] for j in res["near_misses"]}
        self.assertEqual(near, {"Alpha Quality Ltd": ["pe"], "Beta Value Ltd": ["roce"],
                                "Sahyog Finance Ltd": ["roce"]})
        self.assertIn("Gamma Pledged Ltd", names(res["gated"]))
        res = run_json(["run", "--csv", EXPORT, "--where", "roce>=20", "--sort", "roce", "--no-gates"])
        vals = [next(c["value"] for c in j["checks"] if c["field"] == "roce") for j in res["passed"]]
        self.assertEqual(vals, sorted(vals, reverse=True))
        res = run_json(["run", "--csv", EXPORT, "--where", "pe<20", "--sort", "pe", "--asc", "--no-gates"])
        vals = [next(c["value"] for c in j["checks"] if c["field"] == "pe") for j in res["passed"]]
        self.assertEqual(vals, sorted(vals))

    def test_text_output_lists_near_misses(self):
        code, out, _ = run(["run", "--csv", EXPORT, "--preset", "quality-compounder"])
        self.assertEqual(code, 0)
        self.assertIn("NEAR MISSES", out)
        self.assertIn("Near Miss Cement Ltd", out)
        self.assertIn("Promoter holding 35.0% (needs ≥ 40%)", out)
        self.assertIn("1,23,457", out)                                  # ₹ crore in Indian grouping
        self.assertIn("Educational analysis, not investment advice.", out)


class TestExplainAndQuery(unittest.TestCase):
    def test_explain(self):
        code, out, _ = run(["explain", "Near Miss Cement", "--csv", EXPORT, "--preset", "quality-compounder"])
        self.assertEqual(code, 0)
        self.assertIn("NEAR MISS", out)
        self.assertIn("FAIL", out)
        self.assertIn("35.0%", out)
        data = run_json(["explain", "pragati bank ltd", "--csv", EXPORT, "--preset", "quality-compounder"])
        self.assertEqual(data["company"]["bucket"], "passed")
        self.assertIn("rank", data["company"])

    def test_explain_errors(self):
        code, _, err = run(["explain", "Zzz", "--csv", EXPORT, "--preset", "garp"])
        self.assertEqual(code, 2)
        self.assertIn("not found", err)
        code, _, err = run(["explain", "Grower", "--csv", EXPORT, "--preset", "garp"])
        self.assertEqual(code, 2)
        self.assertIn("several", err)

    def test_query_text(self):
        code, out, _ = run(["query", "--preset", "quality-compounder"])
        self.assertEqual(code, 0)
        self.assertIn("Return on capital employed > 20 AND", out)
        self.assertIn("Debt to equity < 0.3 AND", out)
        self.assertIn("Pledged percentage < 1 AND", out)
        self.assertIn("Market Capitalization > 500", out)
        q = run_json(["query", "--preset", "canslim-india"])
        self.assertIn("Return over 1year", q["unverified_fields"])
        self.assertIn("YOY Quarterly profit growth > 25", q["one_line"])
        q = run_json(["query", "--where", "roce>25", "--where", "pe<=30", "--no-gates"])
        self.assertEqual(q["one_line"], "Return on capital employed > 25 AND Price to Earning < 30")
        q = run_json(["query", "--preset", "dividend-growth-pullback"])
        self.assertNotIn("Dividend growth", q["one_line"])
        self.assertTrue(any("dividend growth" in n for n in q["notes"]))


class TestJsonAndErrors(unittest.TestCase):
    def test_json_nulls(self):
        data = run_json(["explain", "Blank Data", "--csv", EXPORT, "--preset", "garp"])
        values = {c["field"]: c["value"] for c in data["company"]["checks"]}
        self.assertIsNone(values["pe"])
        self.assertIsNone(data["company"]["score"])
        self.assertEqual(data["company"]["bucket"], "incomplete")
        data = run_json(["fields", "--csv", EXPORT])
        self.assertEqual(data["columns"]["mcap"], "Mar Cap Rs.Cr.")
        self.assertTrue(data["fields"]["roce"]["query_verified"])

    def test_bad_inputs_exit_2(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        (tmp / "empty.csv").write_text("")
        (tmp / "noname.csv").write_text("foo,bar\n1,2\n")
        (tmp / "badnum.csv").write_text("Name,ROCE %\nAcme,high\n")
        (tmp / "book.csv").write_bytes(b"PK\x03\x04junk")
        cases = [
            ["run", "--csv", str(tmp / "missing.csv"), "--preset", "garp"],
            ["run", "--csv", str(tmp / "empty.csv"), "--preset", "garp"],
            ["run", "--csv", str(tmp / "noname.csv"), "--preset", "garp"],
            ["run", "--csv", str(tmp / "badnum.csv"), "--preset", "garp"],
            ["run", "--csv", str(tmp / "book.csv"), "--preset", "garp"],
            ["run", "--csv", EXPORT],
            ["run", "--csv", EXPORT, "--preset", "nosuch"],
            ["run", "--csv", EXPORT, "--preset", "garp", "--sort", "nosuch"],
            ["run", "--csv", EXPORT, "--preset", "garp", "--top", "0"],
            ["run", "--csv", EXPORT, "--preset", "garp", "--gsec", "-1"],
            ["run", "--csv", EXPORT, "--preset", "garp", "--financials", "Nobody Ltd"],
            ["run", "--csv", EXPORT, "--preset", "garp", "--prices-dir", str(tmp / "nodir")],
            ["run", "--csv", EXPORT, "--where", "roce>1", "--map", "bogus=ROCE %"],
        ]
        for argv in cases:
            with self.subTest(argv=argv[-2:]):
                code, out, err = run(argv)
                self.assertEqual(code, 2, err)
                self.assertTrue(err.startswith("error:"), err)
                self.assertNotIn("Traceback", err)


if __name__ == "__main__":
    unittest.main()
