import math
import unittest
from datetime import date

from _load import FIXTURES, load

op = load("fno-options-analysis", "options")


class TestOptions(unittest.TestCase):
    def test_black_scholes_textbook(self):
        # Hull: S=42, K=40, r=10%, sigma=20%, T=0.5 -> call 4.76, put 0.81
        self.assertAlmostEqual(op.bs(42, 40, 0.5, 0.2, 0.1, 0, "CE")["price"], 4.7594, places=3)
        self.assertAlmostEqual(op.bs(42, 40, 0.5, 0.2, 0.1, 0, "PE")["price"], 0.8086, places=3)

    def test_put_call_parity_and_greeks(self):
        s, k, t, v, r = 25000, 25200, 6 / 365, 0.13, 0.06
        c, p = op.bs(s, k, t, v, r, 0, "CE"), op.bs(s, k, t, v, r, 0, "PE")
        self.assertAlmostEqual(c["price"] - p["price"], s - k * math.exp(-r * t), places=6)
        self.assertAlmostEqual(c["delta"] - p["delta"], 1.0, places=9)
        self.assertAlmostEqual(c["gamma"], p["gamma"], places=12)
        self.assertLess(c["theta"], 0)

    def test_iv_round_trip(self):
        price = op.bs(25000, 25500, 10 / 365, 0.145, 0.06, 0, "CE")["price"]
        self.assertAlmostEqual(op.implied_vol(price, 25000, 25500, 10 / 365, 0.06, 0, "CE"), 0.145, places=5)
        self.assertIsNone(op.implied_vol(1.0, 25000, 24000, 10 / 365, 0.06, 0, "CE"))  # below intrinsic

    def test_expected_move(self):
        m = op.expected_move(25000, 13.5, 7)
        self.assertAlmostEqual(m["one_sigma_points"], 25000 * 0.135 * math.sqrt(7 / 365))

    def test_chain_formats_agree(self):
        simple = op.load_chain(str(FIXTURES / "chain_simple.csv"))
        nse = op.load_chain(str(FIXTURES / "chain_nse.csv"))
        self.assertEqual(len(simple), len(nse))
        for a, b in zip(simple, nse):
            for key in ("strike", "call_oi", "put_oi", "call_chg_oi", "put_chg_oi", "call_ltp", "put_ltp"):
                self.assertAlmostEqual(a[key], b[key], msg=key)

    def test_chain_analytics(self):
        chain = op.load_chain(str(FIXTURES / "chain_simple.csv"))
        res = op.analyse_chain(chain, spot=25010)
        self.assertAlmostEqual(res["pcr_oi"], 60500 / 57000)
        self.assertEqual(res["call_walls"][0][0], 25200)
        self.assertEqual(res["put_walls"][0][0], 24900)
        self.assertEqual(res["atm_strike"], 25000)
        self.assertAlmostEqual(res["atm_straddle"], 208)
        pains = {s: sum(c["call_oi"] * max(0, s - c["strike"]) + c["put_oi"] * max(0, c["strike"] - s)
                        for c in chain) for s in (c["strike"] for c in chain)}
        self.assertEqual(res["max_pain"], min(pains, key=pains.get))

    def test_buildup(self):
        self.assertTrue(op.buildup(1, 5).startswith("Long buildup"))
        self.assertTrue(op.buildup(-1, 5).startswith("Short buildup"))
        self.assertTrue(op.buildup(1, -5).startswith("Short covering"))
        self.assertTrue(op.buildup(-1, -5).startswith("Long unwinding"))

    def test_expiry_holiday_shifts_2026(self):
        nifty = {x["date"]: x for x in op.expiries("NIFTY", date(2026, 2, 25), 6)}
        self.assertIn("2026-03-02", nifty)            # Holi Tue 3 Mar -> Mon 2 Mar
        self.assertEqual(nifty["2026-03-30"]["type"], "monthly")   # Mahavir Jayanti Tue 31 Mar
        sensex = [x["date"] for x in op.expiries("SENSEX", date(2026, 1, 9), 1)]
        self.assertEqual(sensex, ["2026-01-14"])      # Jan 15 election closure
        bank = [x["date"] for x in op.expiries("BANKNIFTY", date(2026, 11, 1), 1)]
        self.assertEqual(bank, ["2026-11-23"])        # Guru Nanak Jayanti Tue 24 Nov
        self.assertEqual(op.expiries("BANKNIFTY", date(2027, 1, 1), 1)[0]["date"], "2027-01-25")  # Republic Day

    def test_iron_condor(self):
        legs = [op.parse_leg(s) for s in ("S,CE,25500,60", "B,CE,25700,25", "S,PE,24500,55", "B,PE,24300,22")]
        r = op.analyse_strategy(legs, 65, 25000, 0.13, 6 / 365)
        self.assertAlmostEqual(r["max_profit"], 68 * 65)
        self.assertAlmostEqual(r["max_loss"], -(200 - 68) * 65)
        self.assertAlmostEqual(r["breakevens"][0], 24432)
        self.assertAlmostEqual(r["breakevens"][1], 25568)
        self.assertTrue(0.5 < r["probability_of_profit"] < 1)

    def test_unlimited_detection(self):
        r = op.analyse_strategy([op.parse_leg("S,CE,25000,180")], 65, 25000)
        self.assertTrue(math.isinf(r["max_loss"]))
        r = op.analyse_strategy([op.parse_leg("B,CE,25000,180")], 65, 25000)
        self.assertTrue(math.isinf(r["max_profit"]))
        self.assertAlmostEqual(r["max_loss"], -180 * 65)

    def test_cli(self):
        self.assertEqual(op.main(["lots"]), 0)
        self.assertEqual(op.main(["chain", str(FIXTURES / "chain_nse.csv"), "--spot", "25010"]), 0)


if __name__ == "__main__":
    unittest.main()
