import unittest

from _load import load

ch = load("trading-costs", "charges")


class TestCharges(unittest.TestCase):
    def test_zerodha_delivery_breakdown(self):
        r = ch.calculate("delivery", 1800, 1900, 100, broker="zerodha")
        self.assertAlmostEqual(r["brokerage"], 0.0)
        self.assertAlmostEqual(r["stt"], 370.0)              # 0.1% both sides
        self.assertAlmostEqual(r["exchange_txn"], 370000 * 0.0000307, places=4)
        self.assertAlmostEqual(r["stamp_duty"], 27.0)        # 0.015% on buy
        self.assertAlmostEqual(r["dp_charges"], 15.34, places=2)
        self.assertAlmostEqual(r["total_charges"], 426.18, places=1)

    def test_options_stt_on_sell_premium(self):
        r = ch.calculate("options", 120, 150, 130, broker="zerodha")
        self.assertAlmostEqual(r["stt"], 150 * 130 * 0.0015)  # 0.15% from 1 Apr 2026
        self.assertAlmostEqual(r["brokerage"], 40.0)
        self.assertAlmostEqual(r["total_charges"], 91.68, places=1)

    def test_futures_stt(self):
        r = ch.calculate("futures", 25000, 25100, 65, broker="dhan")
        self.assertAlmostEqual(r["stt"], 25100 * 65 * 0.0005)

    def test_intraday_pct_cap_and_minimum(self):
        small = ch.calculate("intraday", 100, 101, 10, broker="groww")      # 0.1% of 1000 = 1 -> min 5
        self.assertAlmostEqual(small["brokerage"], 10.0)
        big = ch.calculate("intraday", 2000, 2010, 500, broker="zerodha")   # 0.03% of 10L = 300 -> cap 20
        self.assertAlmostEqual(big["brokerage"], 40.0)

    def test_cash_brokerage_never_exceeds_2_5_pct(self):
        r = ch.calculate("delivery", 50, 0, 2, broker="upstox")              # flat 20 on a ₹100 order
        self.assertAlmostEqual(r["brokerage"], 2.5)

    def test_bse_futures_have_no_txn_charge(self):
        r = ch.calculate("futures", 80000, 80100, 20, broker="zerodha", exchange="BSE")
        self.assertEqual(r["exchange_txn"], 0.0)
        self.assertEqual(r["ipft"], 0.0)

    def test_breakeven_nets_to_zero(self):
        be = ch.breakeven_sell("delivery", 1800, 100, broker="zerodha")
        self.assertAlmostEqual(ch.calculate("delivery", 1800, be, 100, broker="zerodha")["net_pnl"], 0, places=2)
        self.assertGreater(be, 1800 * 1.002)  # delivery round trip costs > 0.2%

    def test_mtf_interest(self):
        r = ch.calculate("mtf", 500, 540, 400, broker="zerodha", mtf_days=20, mtf_funded=100000)
        self.assertAlmostEqual(r["mtf_interest"], 100000 * 0.0004 * 20)

    def test_override(self):
        r = ch.calculate("intraday", 1000, 1010, 100, broker="custom", brokerage_override={"flat": 10})
        self.assertAlmostEqual(r["brokerage"], 20.0)

    def test_inr_grouping(self):
        self.assertEqual(ch.inr(1234567.5), "₹12,34,567.50")
        self.assertEqual(ch.inr(-999), "-₹999.00")
        self.assertEqual(ch.inr(100000, 0), "₹1,00,000")

    def test_cli_runs(self):
        self.assertEqual(ch.main(["--segment", "intraday", "--buy", "100", "--sell", "101", "--qty", "10",
                                  "--broker", "all"]), 0)


if __name__ == "__main__":
    unittest.main()
