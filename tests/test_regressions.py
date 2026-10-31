"""Regression tests for defects found in independent review (one test per finding)."""
import csv
import json
import os
import tempfile
import unittest

from _load import FIXTURES, load

ch = load("trading-costs", "charges")
tx = load("indian-tax-for-traders", "tax")
op = load("fno-options-analysis", "options")
ta = load("technical-analysis", "ta")
rk = load("position-sizing", "risk")


def _tmp(text: str, suffix=".csv") -> str:
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    return path


class TestChargesRegressions(unittest.TestCase):
    def test_breakeven_beyond_initial_bracket(self):
        for seg, buy, qty in (("options", 1.0, 20), ("options", 2.0, 20), ("options", 1.0, 30), ("delivery", 10, 1)):
            be = ch.breakeven_sell(seg, buy, qty, broker="zerodha")
            self.assertIsNotNone(be)
            self.assertAlmostEqual(ch.calculate(seg, buy, be, qty, broker="zerodha")["net_pnl"], 0, places=2)
        self.assertAlmostEqual(ch.breakeven_sell("options", 1.0, 20, broker="zerodha"), 3.367, places=2)

    def test_breakeven_is_the_lowest_root(self):
        # Groww charges no DP fee below ₹100 of sell value, so net P&L jumps at a sell price of 50.
        be = ch.breakeven_sell("delivery", 40, 2, broker="groww")
        self.assertLess(be, 50)
        self.assertAlmostEqual(ch.calculate("delivery", 40, be, 2, broker="groww")["net_pnl"], 0, places=2)

    def test_short_breakeven_unreachable_is_none(self):
        self.assertIsNone(ch.breakeven_buy_for_short("options", 1.0, 20, broker="zerodha"))
        be = ch.breakeven_buy_for_short("options", 150, 130, broker="zerodha")
        self.assertAlmostEqual(ch.calculate("options", be, 150, 130, broker="zerodha")["net_pnl"], 0, places=2)


class TestTaxRegressions(unittest.TestCase):
    def test_capital_loss_netted_before_business_loss(self):
        r = tx.estimate(salary=1000000, fno=-200000, stcg=-100000, ltcg=200000)
        self.assertAlmostEqual(r["carry_forward"]["stcl_8y"], 0)
        self.assertAlmostEqual(r["carry_forward"]["business_loss_8y"], 100000)
        self.assertAlmostEqual(r["ltcg_after_setoff"], 0)

    def test_missing_symbol_column_is_an_error(self):
        path = _tmp("Date,Company,Side,Quantity,Price\n2026-05-04,RELIANCE,BUY,10,1300\n"
                    "2026-05-04,TCS,BUY,10,3500\n2026-06-04,TCS,SELL,10,3600\n")
        res = tx.process_tradebook(tx.load_trades(path))     # 'Company' is a recognised alias
        self.assertAlmostEqual(res["years"]["2026-27"]["stcg"], 1000)
        bad = _tmp("Date,Name,Side,Quantity,Price\n2026-05-04,RELIANCE,BUY,10,1300\n")
        with self.assertRaises(ValueError):
            tx.load_trades(bad)

    def test_same_day_fifo_keeps_delivery_price(self):
        path = _tmp("date,symbol,side,qty,price\n2026-05-04,ABC,BUY,100,100\n"
                    "2026-05-04,ABC,SELL,100,101\n2026-05-04,ABC,BUY,100,200\n")
        res = tx.process_tradebook(tx.load_trades(path))
        self.assertAlmostEqual(res["years"]["2026-27"]["intraday"], 100)
        self.assertEqual(res["open_lots"]["ABC"], [["2026-05-04", 100.0, 200.0]])

    def test_negative_sell_quantity(self):
        path = _tmp("date,symbol,side,qty,price\n2026-05-04,ABC,BUY,50,1500\n2026-06-10,ABC,SELL,-50,1600\n")
        res = tx.process_tradebook(tx.load_trades(path))
        self.assertAlmostEqual(res["years"]["2026-27"]["stcg"], 5000)
        self.assertAlmostEqual(res["years"]["2026-27"]["intraday"], 0)
        signed = _tmp("date,symbol,qty,price\n2026-05-04,ABC,50,1500\n2026-06-10,ABC,-50,1600\n")
        self.assertAlmostEqual(tx.process_tradebook(tx.load_trades(signed))["years"]["2026-27"]["stcg"], 5000)

    def test_dividend_surcharge_capped_at_15pct(self):
        r = tx.estimate(salary=30000000, dividends=5000000)
        normal = r["normal_income"]
        div_tax = tx.slab_tax(normal, "new") - tx.slab_tax(normal - 5000000, "new")
        expected = (r["tax_on_normal_income"] - div_tax) * 0.25 + div_tax * 0.15
        self.assertAlmostEqual(r["surcharge"], expected, places=2)


class TestTARegressions(unittest.TestCase):
    def test_separate_date_and_time_columns(self):
        lines = ["Date,Time,Open,High,Low,Close,Volume"]
        for day in ("2026-10-28", "2026-10-29"):
            for i in range(25):
                hh, mm = divmod(9 * 60 + 15 + 15 * i, 60)
                lines.append(f"{day},{hh:02d}:{mm:02d},{100 + i},{101 + i},{99 + i},{100.5 + i},1000")
        rows = ta.load_csv(_tmp("\n".join(lines) + "\n"))
        self.assertEqual(len(rows), 50)
        self.assertEqual(rows[1]["date"].minute, 30)

    def test_yyyymmdd_dates(self):
        self.assertEqual(ta.parse_ts("20260105").date().isoformat(), "2026-01-05")
        self.assertEqual(ta.parse_ts("2026-10-30T09:15:00+0530").hour, 9)

    def test_column_array_candles(self):
        cols = {"open": [1, 2], "high": [2, 3], "low": [0.5, 1.5], "close": [1.5, 2.5], "volume": [10, 20],
                "timestamp": [1761796500, 1761882900]}
        rows = ta.load_candles_json(_tmp(json.dumps({"data": cols}), ".json"))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["close"], 2.5)

    def test_daily_candles_with_market_open_timestamp_are_daily(self):
        rows = ta.load_candles_json(str(FIXTURES / "candles.json"))   # one bar per day, stamped 09:15 IST
        self.assertEqual(ta.summary(rows, "X")["range_label"], "52w H/L")

    def test_rows_larger_than_data(self):
        self.assertEqual(ta.main(["indicators", "--candles", str(FIXTURES / "candles.json"), "--rows", "60",
                                  "--json"]), 0)


class TestOptionsRegressions(unittest.TestCase):
    def test_strike_price_alias(self):
        text = (FIXTURES / "chain_simple.csv").read_text().replace("strike,", "strike_price,", 1)
        chain = op.load_chain(_tmp(text))
        self.assertEqual(len(chain), 7)

    def test_empty_chain(self):
        path = _tmp("strike,call_oi,put_oi\n")
        with self.assertRaises(ValueError):
            op.load_chain(path)
        self.assertEqual(op.main(["chain", path]), 2)


class TestRiskRegressions(unittest.TestCase):
    def test_margin_caps_lots(self):
        r = rk.position_size(800000, 1, 24950, 24940, lot_size=65, margin_per_lot=165000, cost_pct=0.02)
        self.assertEqual(r["lots"], 2)            # 50% of ₹8L ÷ ₹1.65L
        r = rk.position_size(800000, 1, 24950, 24940, lot_size=65, margin_per_lot=165000, cost_pct=0.02,
                             max_margin_pct=100)
        self.assertEqual(r["lots"], 4)

    def test_degenerate_inputs_raise_value_error(self):
        with self.assertRaises(ValueError):
            rk.rr_analysis(100, 100, [(110, 100)])
        with self.assertRaises(ValueError):
            rk.rr_analysis(100, 95, [(90, 100)])           # target on the stop side
        with self.assertRaises(ValueError):
            rk.trail(100, 100, 120)
        with self.assertRaises(ValueError):
            rk.kelly(50, 1, 0)
        self.assertEqual(rk.main(["rr", "--entry", "100", "--stop", "100", "--target", "110"]), 2)
        self.assertEqual(rk.main(["kelly", "--win-rate", "50", "--avg-win", "1", "--avg-loss", "0"]), 2)

    def test_journal_prints_averages_without_losses(self):
        path = _tmp("date,symbol,pnl\n2026-01-01,A,100\n2026-01-02,B,50\n")
        self.assertEqual(rk.main(["journal", path]), 0)


if __name__ == "__main__":
    unittest.main()
