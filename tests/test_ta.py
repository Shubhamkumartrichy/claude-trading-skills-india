import unittest

from _load import FIXTURES, load

ta = load("technical-analysis", "ta")


class TestTA(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = ta.load_csv(str(FIXTURES / "ohlcv_divergence.csv"))
        cls.ind = ta.compute(cls.rows)

    def test_basic_averages(self):
        self.assertEqual(ta.sma([1, 2, 3, 4], 2), [None, 1.5, 2.5, 3.5])
        e = ta.ema([1.0] * 5 + [2.0], 3)
        self.assertEqual(e[:2], [None, None])
        self.assertAlmostEqual(e[-1], 1.5)

    def test_rsi_extremes(self):
        up = list(range(1, 40))
        self.assertEqual(ta.rsi(up)[-1], 100.0)
        self.assertEqual(ta.rsi([5.0] * 30)[-1], 50.0)
        down = list(range(40, 1, -1))
        self.assertEqual(ta.rsi(down)[-1], 0.0)

    def test_planted_divergence_found(self):
        divs = ta.divergences(self.ind)
        self.assertTrue(any(d["type"].startswith("bullish regular") for d in divs))

    def test_weekly_resample(self):
        wk = ta.resample(self.rows, "W")
        self.assertEqual(len(wk), len({r["date"].isocalendar()[:2] for r in self.rows}))
        self.assertEqual(wk[-1]["close"], self.rows[-1]["close"])
        self.assertGreaterEqual(min(r["high"] for r in wk), 0)

    def test_fib_math(self):
        f = ta.fib_levels(2000, 1000, "up")
        self.assertAlmostEqual(f["retracements"]["61.8%"], 1382)
        self.assertAlmostEqual(f["extensions"]["161.8%"], 2618)
        d = ta.fib_levels(2000, 1000, "down", pivot=1500)
        self.assertAlmostEqual(d["retracements"]["38.2%"], 1382)
        self.assertAlmostEqual(d["extensions"]["100.0%"], 500)

    def test_summary_and_mtf(self):
        s = ta.summary(self.rows, "SAMPLE")
        self.assertIn("signals", s["dashboard"])
        self.assertTrue(s["stops"]["long"]["2.0x ATR"] < s["close"])
        w = ta.tf_view(ta.resample(self.rows, "W"), "W", 2)
        d = ta.tf_view(self.rows, "D", 5)
        c = ta.confluence(w, d, "long")
        self.assertTrue(0 <= c["score"] <= 6)

    def test_candles_json_epoch_ms(self):
        rows = ta.load_candles_json(str(FIXTURES / "candles.json"))
        self.assertEqual(len(rows), 40)
        self.assertEqual(rows[-1]["date"].date(), self.rows[-1]["date"].date())

    def test_yf_ticker_mapping(self):
        self.assertEqual(ta.yf_ticker("NIFTY"), "^NSEI")
        self.assertEqual(ta.yf_ticker("reliance"), "RELIANCE.NS")
        self.assertEqual(ta.yf_ticker("RELIANCE", "BSE"), "RELIANCE.BO")

    def test_cli(self):
        self.assertEqual(ta.main(["summary", "--csv", str(FIXTURES / "ohlcv_divergence.csv")]), 0)
        self.assertEqual(ta.main(["fib", "--high", "100", "--low", "80"]), 0)


if __name__ == "__main__":
    unittest.main()
