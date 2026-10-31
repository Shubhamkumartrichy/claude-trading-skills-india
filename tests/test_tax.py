import unittest
from datetime import date

from _load import FIXTURES, load

tx = load("indian-tax-for-traders", "tax")


class TestTax(unittest.TestCase):
    def test_slab_tax_new_regime(self):
        self.assertAlmostEqual(tx.slab_tax(1725000, "new"), 145000)
        self.assertAlmostEqual(tx.slab_tax(400000, "new"), 0)
        self.assertAlmostEqual(tx.slab_tax(3000000, "new"), 300000 + 600000 * 0.30)

    def test_mixed_income_example(self):
        r = tx.estimate(salary=1800000, stcg=120000, ltcg=300000, fno=-80000)
        self.assertAlmostEqual(r["stcg_after_setoff"], 40000)      # F&O loss offsets STCG, not salary
        self.assertAlmostEqual(r["ltcg_taxable"], 175000)
        self.assertAlmostEqual(r["total_tax"], 181870, places=0)

    def test_zero_tax_up_to_12_lakh(self):
        self.assertAlmostEqual(tx.estimate(salary=1275000)["total_tax"], 0)

    def test_marginal_relief_above_12_lakh(self):
        r = tx.estimate(salary=1300000)           # normal income 12.25L
        self.assertAlmostEqual(r["total_tax"], 25000 * 1.04, places=0)

    def test_rebate_not_on_special_rate_gains(self):
        r = tx.estimate(salary=1075000, stcg=150000)   # normal 10L, total 11.5L
        self.assertAlmostEqual(r["tax_on_normal_income"] - r["rebate_87a"], 0)
        self.assertAlmostEqual(r["tax_on_stcg"], 30000)

    def test_fno_loss_never_offsets_salary(self):
        r = tx.estimate(salary=1500000, fno=-200000)
        self.assertAlmostEqual(r["carry_forward"]["business_loss_8y"], 200000)
        self.assertAlmostEqual(r["normal_income"], 1425000)

    def test_intraday_loss_isolated(self):
        r = tx.estimate(other_income=500000, intraday=-50000, fno=30000)
        self.assertAlmostEqual(r["carry_forward"]["speculative_loss_4y"], 50000)
        self.assertAlmostEqual(r["normal_income"], 530000)

    def test_stcl_offsets_ltcg_but_ltcl_only_carries(self):
        r = tx.estimate(salary=2000000, stcg=-50000, ltcg=200000)
        self.assertAlmostEqual(r["ltcg_after_setoff"], 150000)
        r2 = tx.estimate(salary=2000000, stcg=60000, ltcg=-40000)
        self.assertAlmostEqual(r2["stcg_after_setoff"], 60000)
        self.assertAlmostEqual(r2["carry_forward"]["ltcl_8y"], 40000)

    def test_basic_exemption_shortfall_used_against_gains(self):
        r = tx.estimate(stcg=300000)      # no other income: 4L exemption absorbs STCG
        self.assertAlmostEqual(r["total_tax"], 0)
        nri = tx.estimate(stcg=300000, resident=False)
        self.assertAlmostEqual(nri["tax_on_stcg"], 60000)

    def test_surcharge_capped_at_15pct_on_gains(self):
        r = tx.estimate(salary=30000000, stcg=1000000)
        self.assertAlmostEqual(r["surcharge"], r["tax_on_normal_income"] * 0.25 + r["tax_on_stcg"] * 0.15)

    def test_long_term_boundary(self):
        self.assertFalse(tx.is_long_term(date(2025, 1, 15), date(2026, 1, 15)))
        self.assertTrue(tx.is_long_term(date(2025, 1, 15), date(2026, 1, 16)))
        self.assertTrue(tx.is_long_term(date(2024, 2, 29), date(2025, 3, 1)))

    def test_tradebook_fifo(self):
        res = tx.process_tradebook(tx.load_trades(str(FIXTURES / "tradebook.csv")))
        y25, y26 = res["years"]["2025-26"], res["years"]["2026-27"]
        self.assertAlmostEqual(y25["ltcg"], 4960)    # TCS, charges deducted both sides
        self.assertAlmostEqual(y25["stcg"], 3000)    # ITC held < 12 months
        self.assertAlmostEqual(y26["ltcg"], 4500 - 1000)  # INFY grandfathered @ FMV 1150, HDFCBANK loss
        self.assertAlmostEqual(y26["intraday"], 500)
        self.assertEqual(res["warnings"], [])

    def test_harvest(self):
        r = tx.harvest(tx.load_holdings(str(FIXTURES / "holdings.csv")), date(2027, 3, 10),
                       realized_stcg=10000, realized_ltcg=40000)
        ideas = "\n".join(r["ideas"])
        self.assertIn("sell & rebuy 85 LTGAIN", ideas)
        self.assertIn("Loss harvest: booking STLOSS", ideas)
        self.assertIn("NEARLT turns long-term in 23 days", ideas)
        self.assertAlmostEqual(r["ltcg_exemption_room_left"], 0)

    def test_cli(self):
        self.assertEqual(tx.main(["estimate", "--salary", "1000000"]), 0)
        self.assertEqual(tx.main(["turnover", "--pnl", "100", "-50"]), 0)
        self.assertEqual(tx.main(["tradebook", str(FIXTURES / "tradebook.csv"), "--fy", "2026-27"]), 0)


if __name__ == "__main__":
    unittest.main()
