import unittest

from _load import FIXTURES, load

rk = load("position-sizing", "risk")


class TestRisk(unittest.TestCase):
    def test_fixed_fractional_includes_costs(self):
        r = rk.position_size(1000000, 2, 1800, 1700, max_alloc_pct=100)
        self.assertEqual(r["units"], 191)              # 20000 / (100 + 0.25% of 1800)
        self.assertLessEqual(r["risk_amount"], 20000)

    def test_allocation_cap(self):
        r = rk.position_size(1000000, 2, 1800, 1700)
        self.assertTrue(r["capped_by_allocation"])
        self.assertLessEqual(r["position_value"], 200000)

    def test_atr_stop(self):
        r = rk.position_size(500000, 1, 1450, atr=28, atr_mult=2, max_alloc_pct=100)
        self.assertAlmostEqual(r["stop"], 1394)

    def test_lot_rounding(self):
        r = rk.position_size(3000000, 1, 24950, 24800, lot_size=65, cost_pct=0.02)
        self.assertEqual(r["units"] % 65, 0)
        self.assertEqual(r["lots"], 2)           # 3 lots would risk ₹30,223 > ₹30,000 budget
        tiny = rk.position_size(300000, 1, 24950, 24800, lot_size=65, cost_pct=0.02)
        self.assertEqual(tiny["lots"], 0)

    def test_leverage_shrinks_risk(self):
        base = rk.position_size(1000000, 2, 500, 480, max_alloc_pct=100)
        lev = rk.position_size(1000000, 2, 500, 480, leverage=4, max_alloc_pct=100)
        self.assertAlmostEqual(lev["risk_pct_of_capital"], base["risk_pct_of_capital"] / 4, places=1)

    def test_wrong_side_stop_rejected(self):
        with self.assertRaises(ValueError):
            rk.position_size(100000, 1, 100, 105)

    def test_rr_weighted(self):
        r = rk.rr_analysis(1800, 1700, [(1900, 50), (2000, 30), (2200, 20)], 45, cost_pct=0)
        self.assertAlmostEqual(r["weighted_R"], 1.9)
        self.assertAlmostEqual(r["expectancy_R"], 0.45 * 1.9 - 0.55)

    def test_trail_ladder(self):
        self.assertEqual(rk.trail(750, 720, 760)["recommended_stop"], 720)       # <1R
        self.assertEqual(rk.trail(750, 720, 785)["recommended_stop"], 750)       # 1R+
        self.assertAlmostEqual(rk.trail(750, 720, 815, atr=10)["recommended_stop"], 800)  # 2R+
        self.assertEqual(rk.trail(750, 720, 760, current_stop=752)["recommended_stop"], 752)  # ratchet

    def test_kelly(self):
        k = rk.kelly(40, 2, 1)
        self.assertAlmostEqual(k["kelly_pct"], 10)
        self.assertLessEqual(rk.kelly(30, 1, 1)["kelly_pct"], 0)

    def test_journal(self):
        j = rk.journal(str(FIXTURES / "journal.csv"))
        self.assertEqual(j["trades"], 5)
        self.assertAlmostEqual(j["win_rate_pct"], 60)
        self.assertAlmostEqual(j["net_pnl"], 970 - 530 + 570 - 330 + 770)
        self.assertTrue(j["sample_warning"])


if __name__ == "__main__":
    unittest.main()
