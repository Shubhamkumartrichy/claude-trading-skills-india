import contextlib
import io
import json
import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path

from _load import FIXTURES, load

fl = load("fii-dii-flows", "flows")

CASH = str(FIXTURES / "flows_cash.csv")
CASH_NSE = str(FIXTURES / "flows_cash_nse.csv")
CASH_MC = str(FIXTURES / "flows_cash_moneycontrol.csv")
CASH_AMBIG = str(FIXTURES / "flows_cash_ambiguous.csv")
NIFTY = str(FIXTURES / "flows_nifty.csv")
OI_DIR = FIXTURES / "flows_participant_oi"
OI_30 = OI_DIR / "fao_participant_oi_30102026.csv"
SHP = str(FIXTURES / "flows_shareholding.csv")
SHP_SCREENER = str(FIXTURES / "flows_shareholding_screener.csv")
BULK = str(FIXTURES / "flows_bulk_deals.csv")
BLOCK = str(FIXTURES / "flows_block_deals.csv")


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = fl.main(argv)
    return code, out.getvalue(), err.getvalue()


def strict_json(text):
    def reject(c):
        raise ValueError(f"non-standard JSON constant {c}")
    return json.loads(text, parse_constant=reject)


class TestDates(unittest.TestCase):
    def test_unambiguous_formats(self):
        got = fl.parse_dates(["2026-10-30", "30-Oct-2026", "30-OCT-2026", "30 Oct 2026", "Oct 30, 2026",
                              "30-10-2026", "20261030", "2026-10-30 00:00:00", "Total", ""])
        self.assertEqual(got[:8], [date(2026, 10, 30)] * 8)
        self.assertEqual(got[8:], [None, None])

    def test_slash_dates_need_evidence(self):
        with self.assertRaisesRegex(fl.FlowError, "ambiguous"):
            fl.parse_dates(["05/10/2026", "06/10/2026"])
        self.assertEqual(fl.parse_dates(["05/10/2026", "13/10/2026"]), [date(2026, 10, 5), date(2026, 10, 13)])
        self.assertEqual(fl.parse_dates(["10/05/2026", "10/13/2026"]), [date(2026, 10, 5), date(2026, 10, 13)])
        self.assertEqual(fl.parse_dates(["05/10/2026"], "mdy"), [date(2026, 5, 10)])
        with self.assertRaisesRegex(fl.FlowError, "mixes"):
            fl.parse_dates(["13/10/2026", "10/13/2026"])

    def test_dash_dates_are_nse_day_first(self):
        self.assertEqual(fl.parse_dates(["05-10-2026"]), [date(2026, 10, 5)])
        with self.assertRaisesRegex(fl.FlowError, "not DD-MM-YYYY"):
            fl.parse_dates(["10-31-2026"])
        with self.assertRaisesRegex(fl.FlowError, "two-digit year"):
            fl.parse_dates(["30-10-26"])


class TestCash(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows, cls.has_dii, cls.notes = fl.load_cash(CASH)
        cls.res = fl.analyse_cash(cls.rows, cls.has_dii, fl.load_closes(NIFTY))
        cls.win = {w["window"]: w for w in cls.res["windows"]}

    def test_loaded(self):
        self.assertEqual(len(self.rows), 32)
        self.assertTrue(self.has_dii)
        self.assertEqual(self.res["as_of"], date(2026, 10, 30))
        self.assertEqual(self.rows[-1]["fii_net"], -4000)

    def test_window_sums(self):
        expect = {"5 sessions": (-12500, 11200), "10 sessions": (-16100, 15900), "20 sessions": (-20500, 22500),
                  "MTD Oct-2026": (-21000, 23200), "Sep-2026": (6700, 2400)}
        for name, (fii, dii) in expect.items():
            w = self.win[name]
            self.assertAlmostEqual(w["fii_net"], fii, places=6, msg=name)
            self.assertAlmostEqual(w["dii_net"], dii, places=6, msg=name)
            self.assertAlmostEqual(w["combined_net"], fii + dii, places=6, msg=name)
        self.assertEqual(self.win["MTD Oct-2026"]["sessions"], 21)
        self.assertFalse(self.win["Sep-2026"]["complete"])      # file starts mid-September

    def test_absorption(self):
        a5 = self.win["5 sessions"]["absorption"]
        self.assertAlmostEqual(a5["net_pct"], 11200 / 12500 * 100)          # 89.6%
        self.assertAlmostEqual(a5["same_day_pct"], 10900 / 12500 * 100)     # 87.2%
        self.assertEqual((a5["fii_selling_days"], a5["days_dii_bought_more"]), (5, 1))
        self.assertAlmostEqual(self.win["20 sessions"]["absorption"]["net_pct"], 22500 / 20500 * 100)
        self.assertIsNone(self.win["Sep-2026"]["absorption"]["net_pct"])    # FIIs were net buyers

    def test_streaks(self):
        s = self.res["streaks"]
        self.assertEqual((s["fii"]["direction"], s["fii"]["sessions"]), ("selling", 6))
        self.assertAlmostEqual(s["fii"]["total"], -14000)
        self.assertEqual(s["fii"]["since"], date(2026, 10, 23))
        self.assertEqual((s["dii"]["direction"], s["dii"]["sessions"], s["dii"]["since"]),
                         ("buying", 10, date(2026, 10, 19)))
        self.assertAlmostEqual(s["dii"]["total"], 15900)
        rec = self.res["record_streaks"]["fii"]["buying"]
        self.assertEqual((rec["sessions"], rec["from"], rec["to"]), (4, date(2026, 9, 25), date(2026, 9, 30)))
        self.assertAlmostEqual(rec["total"], 2700)

    def test_largest_and_size(self):
        sell = [(x["date"].day, x["net"]) for x in self.res["largest_days"]["fii_selling"][:3]]
        self.assertEqual(sell, [(30, -4000), (29, -3000), (27, -2500)])
        self.assertEqual(self.res["largest_days"]["fii_buying"][0]["date"], date(2026, 10, 16))
        self.assertAlmostEqual(self.res["latest"]["fii_size_vs_20d_avg_abs"], 4000 / 1405)

    def test_nifty_correlation(self):
        n = self.res["nifty"]
        self.assertEqual((n["matched_sessions"], n["unmatched_flow_dates"]), (32, 0))
        self.assertAlmostEqual(n["corr_fii_same_day"], 1.0, places=3)   # fixture: return % = FII net / 10,000
        self.assertIsNotNone(n["corr_fii_next_day"])
        self.assertLess(n["avg_nifty_ret_fii_selling_days"], 0)
        self.assertGreater(n["avg_nifty_ret_fii_buying_days"], 0)

    def test_nse_category_layout_matches(self):
        rows, has_dii, _ = fl.load_cash(CASH_NSE)
        self.assertTrue(has_dii)
        self.assertEqual([r["date"] for r in rows], [r["date"] for r in self.rows[-3:]])
        for a, b in zip(rows, self.rows[-3:]):
            self.assertAlmostEqual(a["fii_net"], b["fii_net"], places=2)    # derived from buy − sell
            self.assertAlmostEqual(a["dii_net"], b["dii_net"], places=2)
            self.assertAlmostEqual(a["fii_buy"], b["fii_buy"], places=2)

    def test_moneycontrol_two_row_header(self):
        rows, _, notes = fl.load_cash(CASH_MC)
        self.assertEqual([(r["date"].day, r["fii_net"], r["dii_net"]) for r in rows],
                         [(28, -1000, 900), (29, -3000, 2500), (30, -4000, 3500)])
        self.assertTrue(any("skipped 1 row" in n for n in notes))

    def test_fii_only_and_duplicates(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "nsdl.csv"
            p.write_text("Reporting Date,FPI Net Investment\n29-Oct-2026,-1200\n30-Oct-2026,800\n")
            rows, has_dii, notes = fl.load_cash(p)
            self.assertFalse(has_dii)
            res = fl.analyse_cash(rows, has_dii)
            self.assertIsNone(res["windows"][0]["absorption"])
            self.assertEqual(res["streaks"]["fii"]["direction"], "buying")
            strict_json(json.dumps(fl.clean_json(res), allow_nan=False))
            p.write_text("date,fii_net,dii_net\n30-Oct-2026,-100,50\n2026-10-30,-200,50\n")
            with self.assertRaisesRegex(fl.FlowError, "appears twice"):
                fl.load_cash(p)


class TestParticipants(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        files, cls.notes = fl.load_participants([str(OI_DIR)])
        cls.files = files
        cls.res = fl.analyse_participants(files)

    def test_parse_real_layout_with_title_row(self):
        f = fl.load_participant_file(OI_30)
        self.assertEqual(f["date"], date(2026, 10, 30))                 # from the "as on Oct 30, 2026" title
        fii = f["data"]["FII"]
        self.assertEqual((fii["fut_idx_long"], fii["fut_idx_short"]), (30000, 130000))
        self.assertEqual(fii["fut_stk_short"], 2600000)                 # header cell has trailing spaces
        self.assertEqual(fii["total_long"], 30000 + 3500000 + 600000 + 900000 + 150000 + 220000)
        self.assertEqual(f["data"]["TOTAL"]["fut_idx_long"], f["data"]["TOTAL"]["fut_idx_short"])
        self.assertEqual(fl.check_totals(f), [])

    def test_long_short_ratio(self):
        fut = self.res["latest"]["participants"]["FII"]["index_futures"]
        self.assertAlmostEqual(fut["long_pct"], 18.75)                 # 30,000 / 1,60,000
        self.assertAlmostEqual(fut["ls_ratio"], 30000 / 130000)
        self.assertEqual(fut["net"], -100000)
        cl = self.res["latest"]["participants"]["Client"]["index_futures"]
        self.assertAlmostEqual(cl["long_pct"], 75.0)

    def test_day_over_day_change(self):
        self.assertEqual(len(self.files), 5)
        ch = self.res["latest"]["change"]
        self.assertEqual(ch["vs"], date(2026, 10, 29))
        c = ch["by_participant"]["FII"]
        self.assertEqual((c["long"], c["short"], c["net"]), (-2000, 12000, -14000))
        self.assertEqual(c["activity"], "short buildup")
        self.assertAlmostEqual(c["long_pct_pp"], 18.75 - 32000 / 150000 * 100)
        self.assertEqual((c["index_calls_net"], c["index_puts_net"]), (-30000, 70000))
        hist = {h["date"]: h for h in self.res["history"]}
        self.assertEqual(hist[date(2026, 10, 29)]["fii_change"]["net"], -12000)

    def test_options_positioning(self):
        fii = self.res["latest"]["participants"]["FII"]
        self.assertEqual(fii["index_calls"]["net"], -200000)
        self.assertEqual(fii["index_puts"]["net"], 400000)
        self.assertEqual(fii["index_options_bias"], -600000)

    def test_flags_and_thresholds(self):
        flags = " | ".join(self.res["flags"])
        self.assertIn("extreme short", flags)
        self.assertIn("Clients", flags)
        self.assertIn("net short index calls", flags)
        quiet = fl.analyse_participants(self.files, long_low=15, long_high=80)
        self.assertFalse(any("extreme short" in f for f in quiet["flags"]))
        self.assertFalse(any("Clients" in f for f in quiet["flags"]))

    def test_expiry_tags(self):
        res = fl.analyse_participants([f for f in self.files if f["date"] <= date(2026, 10, 27)])
        self.assertIn("monthly expiry (approx.)", res["latest"]["tags"])    # last Tuesday of Oct-2026
        self.assertTrue(any("monthly expiry" in c for c in res["latest"]["change"]["caveats"]))
        self.assertEqual(fl.monthly_expiry(date(2025, 3, 10)), date(2025, 3, 27))   # Thursday era

    def test_filename_date_fallback_and_bad_files(self):
        with tempfile.TemporaryDirectory() as td:
            lines = OI_30.read_bytes().decode().split("\r\n")
            p = Path(td) / "fao_participant_oi_30102026.csv"
            p.write_text("\r\n".join(lines[1:]))                         # no title row
            self.assertEqual(fl.load_participant_file(p)["date"], date(2026, 10, 30))
            q = Path(td) / "positions.csv"
            q.write_text("\r\n".join(lines[1:]))
            with self.assertRaisesRegex(fl.FlowError, "cannot tell the date"):
                fl.load_participant_file(q)
            v = Path(td) / "fao_participant_vol_30102026.csv"
            v.write_text('"Participant wise Trading Volumes in Equity Derivatives as on Oct 30, 2026"\n'
                         + "\n".join(lines[1:]))
            with self.assertRaisesRegex(fl.FlowError, "VOLUME"):
                fl.load_participant_file(v)
            shutil.copy(OI_30, Path(td) / "copy.csv")                     # same date twice, same data: fine
            files, notes = fl.load_participants([td])
            self.assertEqual([f["date"] for f in files], [date(2026, 10, 30)])
            self.assertTrue(any("VOLUME" in n for n in notes))


class TestShareholding(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        data, _ = fl.load_shareholding(SHP)
        cls.res = fl.analyse_shareholding(data)
        cls.by = {s["symbol"]: s for s in cls.res["stocks"]}

    def test_quarter_parsing(self):
        self.assertEqual(fl.parse_quarter("Q2FY27"), (2026, 9))
        self.assertEqual(fl.parse_quarter("Q4 FY26"), (2026, 3))
        self.assertEqual(fl.parse_quarter("FY27Q1"), (2026, 6))
        self.assertEqual(fl.parse_quarter("Q2 2026-27"), (2026, 9))
        self.assertEqual(fl.parse_quarter("Sep-2026"), (2026, 9))
        self.assertEqual(fl.parse_quarter("Dec 2025"), (2025, 12))
        self.assertEqual(fl.parse_quarter("2026-09-30"), (2026, 9))
        with self.assertRaisesRegex(fl.FlowError, "ambiguous"):
            fl.parse_quarter("Q3 2026")
        with self.assertRaisesRegex(fl.FlowError, "quarter end"):
            fl.parse_quarter("Oct-2026")

    def test_qoq_deltas_and_rising_institutional(self):
        a = self.by["ALPHA"]
        self.assertAlmostEqual(a["qoq"]["fii"], 1.3)
        self.assertAlmostEqual(a["qoq"]["dii"], 0.7)
        self.assertAlmostEqual(a["qoq"]["institutional"], 2.0)
        self.assertAlmostEqual(a["yoy"]["institutional"], 5.0)
        self.assertIn("ALPHA", self.res["rising_institutional"])
        self.assertTrue(any("4 quarters running" in f for f in a["flags"]))
        self.assertEqual(self.res["stocks"][0]["symbol"], "ALPHA")          # ranked by FII+DII change

    def test_promoter_and_pledge_flags(self):
        b = self.by["BETA"]
        self.assertAlmostEqual(b["qoq"]["promoter"], -2.5)
        self.assertAlmostEqual(b["qoq"]["pledged"], 12.0)
        self.assertEqual(b["latest"]["label"], "Sep-2026 (Q2FY27)")
        self.assertIn("BETA", self.res["falling_promoter"])
        self.assertIn("BETA", self.res["pledge_red_flags"])
        red = " | ".join(b["red_flags"])
        self.assertIn("pledge red flag", red)
        self.assertIn("pledge invocation", red)
        self.assertEqual(self.by["ALPHA"]["red_flags"], [])

    def test_handover_flag(self):
        g = self.by["GAMMA"]
        self.assertTrue(any(f.startswith("FII→DII handover") for f in g["flags"]))
        self.assertIsNone(g["latest"]["pledged"])

    def test_screener_layout(self):
        data, notes = fl.load_shareholding(SHP_SCREENER, "INFY")
        res = fl.analyse_shareholding(data)
        s = res["stocks"][0]
        self.assertEqual((s["symbol"], s["quarters"]), ("INFY", 4))
        self.assertAlmostEqual(s["qoq"]["fii"], 1.3)
        self.assertEqual(s["latest"]["shareholders"], 195001)
        self.assertIn("INFY", res["rising_institutional"])


class TestDeals(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        deals = fl.load_deals(BULK)[0] + fl.load_deals(BLOCK)[0]
        cls.res = fl.analyse_deals(deals)
        cls.sym = {s["symbol"]: s for s in cls.res["by_symbol"]}

    def test_aggregate_by_symbol(self):
        x = self.sym["XYZ"]
        self.assertEqual((x["buy_qty"], x["sell_qty"], x["net_qty"]), (900000, 300000, 600000))
        self.assertAlmostEqual(x["buy_value_cr"], 11.0)
        self.assertAlmostEqual(x["net_value_cr"], 11.0 - 3.615)
        a = self.sym["ABC"]
        self.assertEqual(a["net_qty"], -900000)
        self.assertAlmostEqual(a["net_value_cr"], 29.86 - 56.61)
        self.assertEqual(a["kinds"], ["block", "bulk"])
        self.assertEqual(self.res["by_symbol"][0]["symbol"], "XYZ")

    def test_clients_repeat_and_round_trips(self):
        buyers = {(r["client"], r["symbol"]): r for r in self.res["repeat_buyers"]}
        self.assertIn(("ALPHA GROWTH FUND", "XYZ"), buyers)              # "Alpha Growth Fund " merged
        self.assertAlmostEqual(buyers[("ALPHA GROWTH FUND", "XYZ")]["avg_buy_price"], 11.0e7 / 900000)
        self.assertIn(("BETA MUTUAL FUND", "ABC"), buyers)
        self.assertEqual([(r["client"], len(r["dates"])) for r in self.res["repeat_sellers"]],
                         [("PROMOTER HOLDINGS PVT LTD", 2)])
        self.assertEqual([(r["client"], r["symbol"], r["net_qty"]) for r in self.res["same_day_round_trips"]],
                         [("GRAVITON RESEARCH CAPITAL LLP", "SMEX", 0)])
        top = self.res["by_client"][0]
        self.assertEqual(top["client"], "PROMOTER HOLDINGS PVT LTD")
        self.assertAlmostEqual(top["net_value_cr"], -56.61)


class TestCLI(unittest.TestCase):
    def test_json_outputs_are_strict(self):
        cases = [["cash", "--csv", CASH, "--nifty", NIFTY], ["cash", "--csv", CASH_NSE],
                 ["participants", "--oi", str(OI_DIR)], ["participants", "--oi", str(OI_30)],
                 ["shareholding", "--csv", SHP], ["shareholding", "--csv", SHP_SCREENER, "--symbol", "INFY"],
                 ["deals", "--csv", BULK, "--csv", BLOCK]]
        for argv in cases:
            code, out, err = run(argv + ["--json"])
            self.assertEqual(code, 0, (argv, err))
            data = strict_json(out)
            self.assertIn("as_of", data, argv)
        code, out, _ = run(["deals", "--csv", BULK, "--csv", BLOCK, "--symbol", "xyz", "--json"])
        data = strict_json(out)
        self.assertEqual([s["symbol"] for s in data["by_symbol"]], ["XYZ"])
        code, out, _ = run(["cash", "--csv", CASH, "--as-of", "2026-10-29", "--json"])
        self.assertEqual(strict_json(out)["as_of"], "2026-10-29")

    def test_text_outputs(self):
        code, out, _ = run(["cash", "--csv", CASH, "--nifty", NIFTY])
        self.assertEqual(code, 0)
        for s in ("FII/FPI", "DII absorbed", "-₹14,000.00 cr", "-12,500.00", "Read:"):
            self.assertIn(s, out)
        code, out, _ = run(["participants", "--oi", str(OI_DIR)])
        self.assertIn("1,30,000", out)
        self.assertIn("18.8%", out)
        self.assertIn("short buildup", out)
        code, out, _ = run(["shareholding", "--csv", SHP, "--symbol", "BETA"])
        self.assertIn("RED", out)
        code, out, _ = run(["deals", "--csv", BULK, "--csv", BLOCK])
        self.assertIn("Repeat buyers", out)
        self.assertNotIn("-0.00", out)

    def test_ambiguous_dates_exit_cleanly(self):
        for argv in (["cash", "--csv", CASH_AMBIG], ["cash", "--csv", CASH, "--as-of", "05/10/2026"],
                     ["deals", "--csv", BULK, "--from", "03/04/2026"]):
            code, out, err = run(argv)
            self.assertEqual(code, 2, argv)
            self.assertTrue(err.startswith("error:"), argv)
            self.assertIn("ambiguous", err)
            self.assertEqual(err.count("\n"), 1)
            self.assertNotIn("Traceback", err)
        code, out, _ = run(["cash", "--csv", CASH_AMBIG, "--date-format", "dmy", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(strict_json(out)["as_of"], "2026-10-07")

    def test_bad_input_exits_cleanly(self):
        with tempfile.TemporaryDirectory() as td:
            q = Path(td) / "q.csv"
            q.write_text("symbol,quarter,promoter,fii,dii\nX,Q3 2026,50,20,10\n")
            side = Path(td) / "d.csv"
            side.write_text("Date,Symbol,Client Name,Buy/Sell,Quantity Traded,Trade Price\n30-Oct-2026,X,A,HOLD,1,1\n")
            cases = [["cash", "--csv", "missing.csv"], ["cash", "--csv", SHP],
                     ["participants", "--oi", td], ["participants", "--oi", str(OI_DIR), "--long-low", "80"],
                     ["participants", "--oi", str(OI_DIR), "--as-of", "2026-01-01"],
                     ["shareholding", "--csv", str(q)], ["shareholding", "--csv", SHP, "--symbol", "NOPE"],
                     ["deals", "--csv", str(side)], ["deals", "--csv", BULK, "--symbol", "NOPE"]]
            for argv in cases:
                code, out, err = run(argv)
                self.assertEqual(code, 2, argv)
                self.assertTrue(err.startswith("error:"), (argv, err))
                self.assertNotIn("Traceback", err)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            fl.main(["nonsense"])
        self.assertEqual(cm.exception.code, 2)

    def test_formatting(self):
        self.assertEqual(fl.indian(-1234567.891), "-12,34,567.89")
        self.assertEqual(fl.cr(-14000), "-₹14,000.00 cr")
        self.assertEqual(fl.cr(-0.004), "₹0 cr")
        self.assertEqual(fl.indian(-0.001), "0.00")


if __name__ == "__main__":
    unittest.main()
