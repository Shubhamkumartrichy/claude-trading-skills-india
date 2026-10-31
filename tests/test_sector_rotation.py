import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import date, timedelta

from _load import FIXTURES, load

sr = load("sector-rotation", "sectors")
IDX = FIXTURES / "sector_indices"
STK = FIXTURES / "sector_stocks"
MEMBERS = FIXTURES / "sector_members.csv"


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = sr.main(argv)
    return code, out.getvalue(), err.getvalue()


class TestMaths(unittest.TestCase):
    def test_returns(self):
        vals = [100, 110, 121]
        self.assertAlmostEqual(sr.pct_change(vals, 1), 10.0)
        self.assertAlmostEqual(sr.pct_change(vals, 2), 21.0)
        self.assertIsNone(sr.pct_change(vals, 3))
        # sector +21% while NIFTY +10% → outperformed by 10%, not 11 points
        self.assertAlmostEqual(sr.relative_return(21.0, 10.0), 10.0)
        self.assertIsNone(sr.relative_return(None, 5.0))

    def test_quadrants(self):
        self.assertEqual(sr.quadrant(101, 101), "Leading")
        self.assertEqual(sr.quadrant(101, 99), "Weakening")
        self.assertEqual(sr.quadrant(99, 99), "Lagging")
        self.assertEqual(sr.quadrant(99, 101), "Improving")
        self.assertIsNone(sr.quadrant(None, 100))

    def test_rrg_flat_and_accelerating(self):
        bench = [100.0] * 40
        ratio, mom = sr.rrg_series([50.0] * 40, bench)
        self.assertAlmostEqual(ratio[-1], 100.0)
        self.assertAlmostEqual(mom[-1], 100.0)
        accel = [50.0 * 1.001 ** (i * i) for i in range(40)]
        ratio, mom = sr.rrg_series(accel, bench)
        self.assertGreater(ratio[-1], 100)
        self.assertGreater(mom[-1], 100)

    def test_weekly_resample_keeps_last_close(self):
        d0 = date(2026, 10, 5)           # Monday
        dates = [d0 + timedelta(days=i) for i in range(12) if (d0 + timedelta(days=i)).weekday() < 5]
        wd, closes = sr.to_weekly(dates, list(range(len(dates))))
        self.assertEqual(len(wd), 2)
        self.assertEqual(closes, [4, len(dates) - 1])

    def test_aliases(self):
        self.assertEqual(sr.resolve("NIFTY PSU BANK"), "PSUBANK")
        self.assertEqual(sr.resolve("nifty_fin_service"), "FINSERV")
        self.assertEqual(sr.resolve("NIFTY SMLCAP 250"), "SMALLCAP250")
        self.assertEqual(sr.resolve("BANKNIFTY"), "BANK")
        self.assertIsNone(sr.resolve("NIFTY 50"))
        self.assertTrue(sr.is_benchmark("NIFTY 50"))


class TestFixtures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bench, loaded = sr.load_csv_dir(str(IDX))
        params = {"weeks": 8, "smooth": 3, "window": 10, "momentum": 4, "tail": 8}
        cls.res = sr.analyse(bench, loaded, params, [])
        cls.by = {r["key"]: r for r in cls.res["sectors"] + cls.res["size"]}

    def test_files_resolved_to_universe(self):
        self.assertEqual(set(self.by), {"METAL", "FMCG", "PHARMA", "SMALLCAP250"})
        self.assertEqual(self.by["SMALLCAP250"]["group"], "size")
        self.assertEqual(self.res["frequency"], "daily")

    def test_close_only_csv_without_volume(self):
        rows = sr.load_closes(IDX / "NIFTY FMCG.csv")        # Date,Close only
        self.assertEqual(len(rows), 420)
        self.assertIn("returns", self.by["FMCG"])

    def test_returns_match_csv(self):
        closes = [c for _, c in sr.load_closes(IDX / "NIFTY METAL.csv")]
        bench = [c for _, c in sr.load_closes(IDX / "NIFTY 50.csv")]
        r = self.by["METAL"]
        self.assertAlmostEqual(r["returns"]["1W"], (closes[-1] / closes[-6] - 1) * 100)
        self.assertAlmostEqual(r["returns"]["3M"], (closes[-1] / closes[-64] - 1) * 100)
        b3 = (bench[-1] / bench[-64] - 1) * 100
        self.assertAlmostEqual(r["relative"]["3M"], ((1 + r["returns"]["3M"] / 100) / (1 + b3 / 100) - 1) * 100)

    def test_planted_leader_and_lagger(self):
        self.assertEqual(self.by["METAL"]["quadrant"], "Leading")
        self.assertEqual(self.by["FMCG"]["quadrant"], "Lagging")
        self.assertEqual(self.res["sectors"][0]["key"], "METAL")
        self.assertEqual(self.res["sectors"][-1]["key"], "FMCG")
        self.assertEqual(self.by["METAL"]["trend"], "Uptrend")
        self.assertEqual(self.by["FMCG"]["trend"], "Downtrend")

    def test_planted_transition(self):
        ph = self.by["PHARMA"]
        self.assertEqual(ph["quadrant"], "Improving")
        self.assertIn(("Lagging", "Improving"), [(t["from"], t["to"]) for t in ph["transitions"]])
        self.assertLessEqual(ph["weeks_in_quadrant"], 3)
        self.assertIn("PHARMA Lagging→Improving", self.res["summary"])

    def test_rotation_and_size_read(self):
        self.assertEqual(self.res["rotation"]["tilt"], "Risk-on")
        self.assertEqual(self.res["rotation"]["cyclicals"], ["METAL"])
        self.assertEqual(self.res["size_read"]["read"], "Broadening")


class TestInputs(unittest.TestCase):
    def test_weekly_csvs(self):
        tmp = tempfile.mkdtemp()
        for name in ("NIFTY 50", "NIFTY METAL", "NIFTY FMCG"):
            rows = sr.load_closes(IDX / f"{name}.csv")
            wd, wc = sr.to_weekly([d for d, _ in rows], [c for _, c in rows])
            with open(os.path.join(tmp, f"{name}.csv"), "w") as fh:
                fh.write("Date,Close\n" + "".join(f"{d:%d-%b-%Y},{c}\n" for d, c in zip(wd, wc)))
        bench, loaded = sr.load_csv_dir(tmp)
        params = {"weeks": 8, "smooth": 3, "window": 10, "momentum": 4, "tail": 8}
        res = sr.analyse(bench, loaded, params, [])
        by = {r["key"]: r for r in res["sectors"]}
        self.assertEqual(res["frequency"], "weekly")
        self.assertEqual(by["METAL"]["quadrant"], "Leading")
        self.assertEqual(by["FMCG"]["quadrant"], "Lagging")

    def test_ticker_overrides(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "map.json")
        with open(path, "w") as fh:
            json.dump({"NIFTY MEDIA": "MYMEDIA.NS"}, fh)
        o = sr.parse_overrides(["NIFTY 50=^CRSLDX", "nifty it=^CNXIT"], path)
        self.assertEqual(o, {"MEDIA": "MYMEDIA.NS", "NIFTY": "^CRSLDX", "IT": "^CNXIT"})


class TestBreadth(unittest.TestCase):
    def test_percent_above_dma(self):
        res = sr.breadth(sr.load_members(str(MEMBERS)), str(STK))
        by = {r["sector"]: r for r in res["sectors"]}
        self.assertAlmostEqual(by["IT"]["pct_above_50dma"], 75.0)
        self.assertAlmostEqual(by["IT"]["pct_above_200dma"], 75.0)
        self.assertAlmostEqual(by["METAL"]["pct_above_50dma"], 50.0)
        self.assertEqual(by["METAL"]["missing"], ["NOSUCH"])


class TestCLI(unittest.TestCase):
    def test_json_outputs(self):
        code, out, _ = run(["rotation", "--csv-dir", str(IDX), "--json"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertIn("summary", data)
        self.assertEqual(data["sectors"][0]["key"], "METAL")
        code, out, _ = run(["rrg", "--csv-dir", str(IDX), "--tail", "4", "--json"])
        self.assertEqual(len(json.loads(out)["sectors"][0]["tail"]), 4)
        code, out, _ = run(["breadth", "--members", str(MEMBERS), "--csv-dir", str(STK), "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(out)["sectors"]), 2)
        self.assertEqual(json.loads(run(["universe", "--json"])[1])["benchmark"]["ticker"], "^NSEI")

    def test_text_outputs(self):
        code, out, _ = run(["rotation", "--csv-dir", str(IDX), "--sectors", "METAL,FMCG"])
        self.assertEqual(code, 0)
        self.assertIn("Tilt: Risk-on", out)
        self.assertIn("Summary:", out)
        self.assertNotIn("PHARMA", out)

    def test_bad_inputs_exit_cleanly(self):
        tmp = tempfile.mkdtemp()
        with open(os.path.join(tmp, "NIFTY IT.csv"), "w") as fh:
            fh.write("Date,Close\n2026-10-30,100\n")
        bad_cols = os.path.join(tmp, "members.csv")
        with open(bad_cols, "w") as fh:
            fh.write("a,b\nx,y\n")
        for argv in (["rotation", "--csv-dir", os.path.join(tmp, "missing")],
                     ["rotation", "--csv-dir", tmp],                       # no benchmark file
                     ["rotation", "--csv-dir", str(IDX), "--ticker", "IT"],
                     ["rotation", "--csv-dir", str(IDX), "--window", "0"],
                     ["breadth", "--members", bad_cols, "--csv-dir", str(STK)],
                     ["breadth", "--csv-dir", str(STK)]):
            with self.subTest(argv=argv):
                code, out, err = run(argv)
                self.assertEqual(code, 2)
                self.assertTrue(err.startswith("error:"))
                self.assertNotIn("Traceback", err)


if __name__ == "__main__":
    unittest.main()
