import contextlib
import io
import json
import math
import os
import pathlib
import tempfile
import unittest
import zipfile
from datetime import date, timedelta

from _load import FIXTURES, load

pf = load("portfolio-review", "portfolio")
tx = load("indian-tax-for-traders", "tax")
ch = load("trading-costs", "charges")

AS_OF = date(2026, 11, 1)
HOLDINGS = str(FIXTURES / "portfolio_holdings.csv")


def _lots(path=HOLDINGS):
    return pf.load_holdings(path)[0]


def _run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = pf.main(argv)
    return code, out.getvalue(), err.getvalue()


def _write(folder, name, text):
    path = pathlib.Path(folder) / name
    path.write_text(text, encoding="utf-8")
    return str(path)


class TestReview(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = pf.review(_lots(), cash=200000, as_of=AS_OF)
        cls.w = {h["symbol"]: h["weight_pct"] for h in cls.r["holdings"]}

    def test_totals(self):
        r = self.r
        self.assertAlmostEqual(r["equity_value"], 800000)
        self.assertAlmostEqual(r["total_capital"], 1000000)
        self.assertAlmostEqual(r["invested"], 710000)
        self.assertAlmostEqual(r["unrealised_pnl"], 90000)
        self.assertAlmostEqual(r["unrealised_pnl_pct"], 90000 / 710000 * 100)
        self.assertEqual((r["stocks"], r["lots"]), (6, 7))

    def test_weights_and_concentration(self):
        self.assertAlmostEqual(self.w["AAA"], 30.0)      # two lots aggregated: 150 x 2000
        self.assertAlmostEqual(self.w["TCS"], 15.0)
        self.assertAlmostEqual(self.w["BBB"], 10.0)
        self.assertAlmostEqual(self.r["top5_pct"], 76.0)
        self.assertAlmostEqual(self.r["top10_pct"], 80.0)
        msgs = [f["message"] for f in self.r["flags"]]
        breached = sorted(m.split()[0] for m in msgs if "single-stock limit" in m)
        self.assertEqual(breached, ["AAA", "DDD", "FFF", "TCS"])   # BBB at exactly 10% is not a breach
        aaa = next(f for f in self.r["flags"] if f["message"].startswith("AAA is 30.0%"))
        self.assertEqual(aaa["level"], "REVIEW")                   # more than 2x the limit

    def test_sectors_groups_and_builtin_map(self):
        s = self.r["sectors_pct"]
        self.assertAlmostEqual(s["Information Technology"], 55.0)   # AAA + BBB + TCS (built-in map)
        self.assertAlmostEqual(s["Financial Services"], 4.0)
        tcs = next(h for h in self.r["holdings"] if h["symbol"] == "TCS")
        self.assertEqual((tcs["sector_source"], tcs["mcap"]), ("built-in", "large"))
        self.assertTrue(any("Sector Information Technology is 55.0%" in f["message"] for f in self.r["flags"]))
        self.assertEqual(self.r["groups_pct"], {"Alpha": 40.0})     # single-company Tata is not a group risk

    def test_mcap_mix(self):
        mix = self.r["mcap"]["of_equity_pct"]
        self.assertAlmostEqual(mix["large"], 56.25)
        self.assertAlmostEqual(mix["mid"], 12.5)
        self.assertAlmostEqual(mix["small"], 31.25)     # FFF's "500" is ₹500 crore -> small
        self.assertAlmostEqual(self.r["mcap"]["gap_pct"]["mid"], -12.5)
        custom = pf.review(_lots(), 200000, AS_OF, mcap_target="50/30/20", mcap_cutoffs="400,100")
        self.assertAlmostEqual(custom["mcap"]["of_equity_pct"]["small"], 18.125)   # FFF now large

    def test_india_flags(self):
        msgs = "\n".join(f["message"] for f in self.r["flags"])
        self.assertIn("DDD is 30.0% below average cost", msgs)
        self.assertIn("DDD: 100 of 300 shares pledged", msgs)
        self.assertIn("FFF is an SME-platform stock", msgs)
        self.assertNotIn("TCS is 6", msgs)               # -6.25% is not below the 20% loser threshold

    def test_ltcg_clock_matches_tax_py(self):
        soon30 = [(x["symbol"], x["days_to_long_term"]) for x in self.r["turning_long_term_30d"]]
        soon60 = [(x["symbol"], x["days_to_long_term"]) for x in self.r["turning_long_term_31_60d"]]
        self.assertEqual(soon30, [("CCC", 20)])
        self.assertEqual(soon60, [("BBB", 40)])
        h = tx.harvest([{"symbol": "BBB", "buy_date": date(2025, 12, 10), "qty": 200, "buy_price": 400,
                         "ltp": 500}], AS_OF)
        self.assertEqual(h["lots"][0]["days_to_long_term"], 40)
        terms = {(x["symbol"], x["buy_date"]): x["term"] for x in self.r["tax_lots"]}
        self.assertEqual(terms[("AAA", "2024-01-10")], "LT")
        self.assertEqual(terms[("AAA", "2026-09-01")], "ST")
        self.assertTrue(any("Wait: CCC" in i for i in self.r["harvest_ideas"]))

    def test_unknown_sector_suggests_column(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "h.csv", "symbol,qty,buy_price,ltp\nZZZ,10,100,110\n")
            r = pf.review(pf.load_holdings(p)[0], as_of=AS_OF)
        self.assertEqual(r["sectors_pct"], {"Unknown": 100.0})
        self.assertTrue(any("add a `sector` column" in f["message"] for f in r["flags"]))
        self.assertEqual(r["tax_lots"][0]["term"], "unknown")


class TestBrokerHeaders(unittest.TestCase):
    def test_zerodha_console_layout(self):
        lots, notes = pf.load_holdings(str(FIXTURES / "portfolio_zerodha_console.csv"))
        infy = next(l for l in lots if l["symbol"] == "INFY")
        self.assertEqual((infy["qty"], infy["buy_price"], infy["ltp"]), (40, 1450.5, 1500))
        self.assertEqual((infy["pledged_qty"], infy["long_term_qty"], infy["isin"]), (10, 40, "INE009A01021"))
        self.assertEqual(infy["sector"], "IT - Software")
        hdfc = next(l for l in lots if l["symbol"] == "HDFCBANK")
        self.assertEqual(hdfc["pledged_qty"], 5)              # margin + loan pledge columns summed
        self.assertTrue(any("buy_date" in n for n in notes))
        self.assertTrue(any("Console" in n for n in notes))

    def test_kite_and_groww_layouts(self):
        with tempfile.TemporaryDirectory() as d:
            kite = _write(d, "kite.csv", 'Instrument,Qty.,Avg. cost,LTP,Cur. val,P&L,Net chg.,Day chg.\n'
                                         'RELIANCE,10,"2,400.00","2,800.00",28000,4000,16.67,0.5\n')
            groww = _write(d, "groww.csv", "Holdings statement\n\nStock Name,ISIN,Quantity,Average buy price,"
                                           "Buy value,Closing price,Closing value,Unrealised P&L\n"
                                           "ITC LTD,INE154A01025,100,400,40000,,42000,2000\n")
            k = pf.load_holdings(kite)[0][0]
            g = pf.load_holdings(groww)[0][0]
        self.assertEqual((k["symbol"], k["qty"], k["buy_price"], k["ltp"]), ("RELIANCE", 10, 2400, 2800))
        self.assertEqual((g["symbol"], g["qty"], g["buy_price"]), ("ITC LTD", 100, 400))
        self.assertAlmostEqual(g["ltp"], 420)          # derived from closing value / qty

    def test_xlsx_export(self):
        sheet = ('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
                 '<row r="1"><c r="A1" t="s"><v>0</v></c></row>'
                 '<row r="3"><c r="A3" t="s"><v>1</v></c><c r="B3" t="s"><v>2</v></c>'
                 '<c r="C3" t="s"><v>3</v></c><c r="D3" t="s"><v>4</v></c></row>'
                 '<row r="4"><c r="A4" t="inlineStr"><is><t>SBIN</t></is></c><c r="B4"><v>25</v></c>'
                 '<c r="C4"><v>600.5</v></c><c r="D4"><v>800</v></c></row></sheetData></worksheet>')
        strings = ["Client ID AB1234", "Symbol", "Quantity Available", "Average Price", "Previous Closing Price"]
        sst = ('<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
               + "".join(f"<si><t>{s}</t></si>" for s in strings) + "</sst>")
        wb = ('<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
              'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
              '<sheets><sheet name="Equity" sheetId="1" r:id="rId1"/></sheets></workbook>')
        rels = ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "holdings.xlsx")
            with zipfile.ZipFile(path, "w") as z:
                z.writestr("xl/workbook.xml", wb)
                z.writestr("xl/_rels/workbook.xml.rels", rels)
                z.writestr("xl/worksheets/sheet1.xml", sheet)
                z.writestr("xl/sharedStrings.xml", sst)
            lot = pf.load_holdings(path, "equity")[0][0]
        self.assertEqual((lot["symbol"], lot["qty"], lot["buy_price"], lot["ltp"]), ("SBIN", 25, 600.5, 800))


class TestRisk(unittest.TestCase):
    N = 60      # daily returns: benchmark alternates +1% / -1%

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = cls.tmp.name
        rb = [0.01 if i % 2 == 0 else -0.01 for i in range(cls.N)]
        start = date(2026, 1, 1)

        def series(name, first, mult):
            px, lines = first, ["date,open,high,low,close,volume"]
            for i in range(cls.N + 1):
                if i:
                    px *= 1 + mult * rb[i - 1]
                lines.append(f"{(start + timedelta(days=i)).isoformat()},{px},{px * 1.01},{px * 0.99},{px},1000")
            _write(d, f"{name}.csv", "\n".join(lines) + "\n")
        series("NIFTY", 100.0, 1)
        series("XHI", 200.0, 2)      # twice the benchmark move -> beta 2
        series("YLO", 50.0, 1)       # same as benchmark -> beta 1, correlation 1 with XHI
        cls.holdings = _write(d, "h.csv", "symbol,qty,buy_price,ltp,stop\nXHI,100,200,200,180\nYLO,400,50,50,\n")
        lots = pf.load_holdings(cls.holdings)[0]
        prices, missing = pf.load_price_dir(d, ["XHI", "YLO", "NIFTY"])
        cls.r = pf.risk(lots, cash=40000, prices=prices, missing=missing)
        cls.rb = rb

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_beta_vol_drawdown(self):
        s = self.r["stats"]
        n = self.N
        bench_vol = 0.01 * math.sqrt(n / (n - 1)) * math.sqrt(252) * 100
        self.assertAlmostEqual(s["benchmark_vol_ann_pct"], bench_vol, places=6)
        self.assertAlmostEqual(s["beta"], 1.5, places=6)                 # 50/50 by value
        self.assertAlmostEqual(s["vol_ann_pct"], 1.5 * bench_vol, places=6)
        self.assertAlmostEqual(s["beta_incl_cash"], 1.5 * 40000 / 80000, places=6)
        expected_dd = ((1.015 * 0.985) ** (n // 2) / 1.015 - 1) * 100    # peak after the first up-day
        self.assertAlmostEqual(s["max_drawdown_pct"], expected_dd, places=6)
        per = {x["symbol"]: x["beta"] for x in s["per_stock"]}
        self.assertAlmostEqual(per["XHI"], 2.0, places=6)
        self.assertAlmostEqual(per["YLO"], 1.0, places=6)

    def test_correlation_cluster(self):
        pairs = self.r["stats"]["correlated_pairs"]
        self.assertEqual(len(pairs), 1)
        self.assertAlmostEqual(pairs[0]["corr"], 1.0, places=6)
        self.assertEqual(self.r["stats"]["clusters"], [["XHI", "YLO"]])

    def test_heat_and_atr(self):
        h = self.r["heat"]
        self.assertAlmostEqual(h["at_risk"], 2000)                        # (200-180) x 100
        self.assertAlmostEqual(h["heat_pct"], 2000 / 80000 * 100)
        xhi = next(p for p in h["positions"] if p["symbol"] == "XHI")
        self.assertGreater(xhi["atr"], 0)
        self.assertAlmostEqual(xhi["stop_in_atr"], 20 / xhi["atr"])

    def test_max_drawdown_helper(self):
        rets = [110 / 100 - 1, 99 / 110 - 1, 120 / 99 - 1]
        self.assertAlmostEqual(pf.max_drawdown(rets)[0], -0.10)

    def test_heat_without_prices(self):
        r = pf.risk(_lots(), cash=200000)
        self.assertAlmostEqual(r["heat"]["heat_pct"], 4.0)               # AAA 30,000 + BBB 10,000
        self.assertIsNone(r["stats"])
        self.assertTrue(any("DDD" in f["message"] and f["level"] == "REVIEW" for f in r["flags"]))

    def test_cli_json(self):
        code, out, _ = _run(["risk", self.holdings, "--prices-dir", self.tmp.name, "--cash", "40000", "--json"])
        self.assertEqual(code, 0)
        self.assertAlmostEqual(json.loads(out)["stats"]["beta"], 1.5, places=6)


class TestRebalance(unittest.TestCase):
    def test_equal_weight_trades(self):
        r = pf.rebalance(_lots(), cash=200000, as_of=AS_OF)
        t = {x["symbol"]: (x["side"], x["qty"]) for x in r["trades"]}
        self.assertEqual(t["AAA"], ("SELL", 66))        # (3,00,000 - 1,66,667) / 2000
        self.assertEqual(t["CCC"], ("BUY", 316))
        self.assertEqual(t["BBB"], ("BUY", 133))
        self.assertEqual(t["TCS"], ("BUY", 5))
        self.assertAlmostEqual(r["tax"]["ltcg"], 66000)  # FIFO: oldest AAA lot (2024) sold first
        self.assertAlmostEqual(r["tax"]["estimated_tax"], 0)
        aaa = next(x for x in r["trades"] if x["symbol"] == "AAA")
        self.assertAlmostEqual(aaa["charges"], ch.calculate("delivery", 0, 2000, 66, broker="zerodha")["total_charges"])
        self.assertGreaterEqual(r["cash_after"], 0)

    def test_targets_with_tax_flags(self):
        kind, targets, px = pf.load_targets(str(FIXTURES / "portfolio_targets.csv"))
        self.assertEqual(kind, "symbol")
        r = pf.rebalance(_lots(), 200000, AS_OF, targets, kind, px)
        t = {x["symbol"]: (x["side"], x["qty"]) for x in r["trades"]}
        self.assertEqual(t, {"AAA": ("SELL", 50), "BBB": ("SELL", 200), "CCC": ("SELL", 100)})
        self.assertAlmostEqual(r["tax"]["stcg"], 30000)
        self.assertAlmostEqual(r["tax"]["ltcg"], 50000)
        self.assertAlmostEqual(r["tax"]["estimated_tax"], 30000 * tx.STCG_RATE)
        flags = "\n".join(r["tax_flags"])
        self.assertIn("WAIT? BBB", flags)
        self.assertIn("long-term in 40 days", flags)
        self.assertIn("WAIT? CCC", flags)
        self.assertIn("inside the remaining ₹1.25L exemption", flags)
        self.assertEqual({h["symbol"] for h in r["holds"]}, {"DDD", "TCS", "FFF"})

    def test_ltcg_above_exemption_and_wait_window(self):
        r = pf.rebalance(_lots(), 200000, AS_OF, {"AAA": 20, "BBB": 0}, "symbol", realized_ltcg=100000, wait_days=30)
        self.assertAlmostEqual(r["tax"]["estimated_tax"], 20000 * tx.STCG_RATE + 25000 * tx.LTCG_RATE)
        self.assertNotIn("WAIT? BBB", "\n".join(r["tax_flags"]))          # 40 days > 30-day window

    def test_sector_targets(self):
        r = pf.rebalance(_lots(), 200000, AS_OF, {"Information Technology": 25}, "sector")
        aaa = next(x for x in r["trades"] if x["symbol"] == "AAA")
        self.assertEqual((aaa["side"], aaa["qty"]), ("SELL", 81))     # target 25% x 300/550 of ₹10L

    def test_no_sell_scales_to_cash(self):
        r = pf.rebalance(_lots(), 50000, AS_OF, no_sell=True)
        self.assertTrue(all(x["side"] == "BUY" for x in r["trades"]))
        self.assertGreaterEqual(r["cash_after"], 0)
        self.assertTrue(any("scaled" in n for n in r["notes"]))

    def test_targets_over_100_rejected(self):
        with self.assertRaises(ValueError):
            pf.rebalance(_lots(), 0, AS_OF, {"AAA": 80, "TCS": 50}, "symbol")


class TestCli(unittest.TestCase):
    def test_review_json_is_valid(self):
        code, out, _ = _run(["review", HOLDINGS, "--cash", "200000", "--as-of", "2026-11-01", "--json"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertAlmostEqual(data["top5_pct"], 76.0)

    def test_text_outputs(self):
        for argv in (["review", HOLDINGS, "--as-of", "2026-11-01"],
                     ["rebalance", HOLDINGS, "--cash", "200000", "--as-of", "2026-11-01", "--broker", "groww"],
                     ["risk", HOLDINGS]):
            code, out, _ = _run(argv)
            self.assertEqual(code, 0, argv)
            self.assertIn("not", out.splitlines()[-1].lower())

    def test_bad_inputs_exit_cleanly(self):
        with tempfile.TemporaryDirectory() as d:
            no_header = _write(d, "x.csv", "foo,bar\n1,2\n")
            bad_price = _write(d, "y.csv", "symbol,qty,buy_price,ltp\nAAA,10,100,abc\n")
            cases = [["review", os.path.join(d, "missing.csv")],
                     ["review", no_header],
                     ["review", bad_price],
                     ["review", HOLDINGS, "--cash", "-5"],
                     ["review", HOLDINGS, "--mcap-target", "50/50/50"],
                     ["rebalance", HOLDINGS, "--broker", "nobroker"],
                     ["risk", HOLDINGS, "--prices-dir", d]]
            for argv in cases:
                code, out, err = _run(argv)
                self.assertEqual(code, 2, argv)
                self.assertTrue(err.startswith("error:"), (argv, err))
                self.assertNotIn("Traceback", err)


if __name__ == "__main__":
    unittest.main()
