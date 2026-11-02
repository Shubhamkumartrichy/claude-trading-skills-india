import contextlib
import csv
import io
import json
import os
import tempfile
import unittest
from datetime import date, timedelta

from _load import FIXTURES, load

br = load("market-breadth", "breadth")
UNI = FIXTURES / "breadth_universe"
NIFTY = FIXTURES / "breadth_nifty.csv"
MEMBERS = FIXTURES / "breadth_members.csv"
AD = FIXTURES / "breadth_ad.csv"


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = br.main(argv)
    return code, out.getvalue(), err.getvalue()


def strict_json(text):
    def bad(c):
        raise ValueError(f"non-standard JSON constant {c}")
    return json.loads(text, parse_constant=bad)


def bdays(n, start=date(2026, 9, 1)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def bars(closes, dates):
    return [{"date": d, "high": c, "low": c, "close": c} for d, c in zip(dates, closes)]


def read_rows(path):
    """Independent CSV reader: rows with four numeric OHLC fields only."""
    out = []
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            try:
                o, h, lo, c = (float(r[k]) for k in ("Open", "High", "Low", "Close"))
            except ValueError:
                continue
            out.append((r["Date"], h, lo, c))
    return out


def fixture_series(members=True, bench=True):
    stocks, errors, _ = br.universe_from_dir(str(UNI), "*.csv")
    S = br.universe_series(stocks, br.load_members(str(MEMBERS)) if members else None,
                           br.load_closes(NIFTY) if bench else None, br.HL_WINDOW)
    return br.derive(S, br.DIV_WINDOW), stocks, errors


class TestTinyUniverse(unittest.TestCase):
    """Five stocks, 25 sessions, flat high = low = close bars: every number checked by hand."""

    @classmethod
    def setUpClass(cls):
        d = bdays(25)
        cls.stocks = {
            "UP": bars([100 + i for i in range(25)], d),          # SMA20 114.5 < 124
            "DOWN": bars([124 - i for i in range(25)], d),        # SMA20 109.5 > 100
            "JUMP": bars([100] * 24 + [101], d),                  # SMA20 100.05 < 101
            "FLAT": bars([100] * 25, d),                          # close = SMA20: not above; unchanged
            "LOCK": bars([100] * 24 + [105], d),                  # +5% on a zero-range bar: circuit suspect
        }
        cls.S = br.derive(br.universe_series(cls.stocks, None, None, hl_window=5), 20)

    def test_first_session_dropped(self):
        self.assertEqual(len(self.S["date"]), 24)

    def test_pct_above_dma(self):
        self.assertAlmostEqual(self.S["pct20"][-1], 60.0)     # UP, JUMP, LOCK of 5
        self.assertEqual(self.S["n20"][-1], 5)
        self.assertIsNone(self.S["pct50"][-1])                # not enough bars yet
        self.assertIsNone(self.S["uptrend"][-1])

    def test_advance_decline_and_line(self):
        self.assertEqual((self.S["adv"][-1], self.S["dec"][-1], self.S["unch"][-1]), (3, 1, 1))
        # sessions 1..23: UP advances, DOWN declines, the rest unchanged → net 0 each day; last day +2
        self.assertEqual(self.S["ad_line"][-2], 0)
        self.assertEqual(self.S["ad_line"][-1], 2)

    def test_new_highs_lows_hand_count(self):
        self.assertEqual((self.S["nh"][-1], self.S["nl"][-1], self.S["n_hl"][-1]), (3, 1, 5))
        self.assertEqual(self.S["nh"][2], None)               # index 3 of a stock: still 4 bars of 5
        self.assertEqual(self.S["nh"][3], 1)                  # 5th bar: only UP sets a new high

    def test_circuit_suspect(self):
        self.assertEqual(self.S["circuit"][-1], ["LOCK"])
        self.assertEqual(self.S["circuit"][-2], [])

    def test_stale_stock_left_out_of_today(self):
        d = bdays(25)
        stocks = dict(self.stocks, OLD=bars([100 + i for i in range(23)], d[:23]))
        S = br.derive(br.universe_series(stocks, None, None, hl_window=5), 20)
        self.assertEqual(S["adv"][-1] + S["dec"][-1] + S["unch"][-1], 5)
        res = br.snapshot(S, {"mode": "universe", "input": "x", "stocks_used": 6}, 20, 1.0, 5, None)
        self.assertEqual([s["symbol"] for s in res["stale"]], ["OLD"])
        self.assertTrue(any("no bar on" in n for n in res["notes"]))

    def test_stray_date_dropped_from_calendar(self):
        d = bdays(10)
        stocks = {s: bars([100 + i for i in range(10)], d) for s in ("A", "B", "C")}
        stocks["A"] = stocks["A"] + [{"date": date(2026, 9, 19), "high": 120, "low": 120, "close": 120}]  # Saturday
        prepped = {s: br._prepare(r, 5) for s, r in stocks.items()}
        cal = br.session_calendar(prepped)
        self.assertNotIn(date(2026, 9, 19), cal)
        self.assertEqual(len(cal), 10)


class TestIndicators(unittest.TestCase):
    def test_mcclellan_hand_checked(self):
        # RANA: 200, -400, 600, 0. EMA2 (k=2/3) seeds at -100 → 366.667 → 122.222.
        # EMA3 (k=1/2) seeds at 133.333 → 66.667. Osc: 233.333, 55.556. Summation: 233.333, 288.889.
        rana, osc, summ = br.mcclellan([60, 30, 80, 50], [40, 70, 20, 50], fast=2, slow=3)
        self.assertEqual(rana, [200.0, -400.0, 600.0, 0.0])
        self.assertIsNone(osc[1])
        self.assertAlmostEqual(osc[2], 233.3333, places=3)
        self.assertAlmostEqual(osc[3], 55.5556, places=3)
        self.assertAlmostEqual(summ[3], 288.8889, places=3)

    def test_mcclellan_defaults(self):
        rana, osc, summ = br.mcclellan([70] * 45, [30] * 45)
        self.assertIsNone(osc[37])
        self.assertAlmostEqual(osc[38], 0.0)                  # first value after 39 sessions
        self.assertAlmostEqual(rana[0], 400.0)
        self.assertAlmostEqual(summ[-1], 0.0)
        _, osc, _ = br.mcclellan([50] * 39 + [90] * 3, [50] * 39 + [10] * 3)
        self.assertGreater(osc[-1], 0)                        # breadth improving → positive oscillator

    def test_thrust_rule(self):
        self.assertEqual(br.thrust_events([0.5, 0.45, 0.38, 0.5, 0.62, 0.7]), [4])
        slow = [0.38] + [0.45] * 10 + [0.62]                   # low is 11 sessions before the cross
        self.assertEqual(br.thrust_events(slow), [])
        self.assertEqual(br.thrust_events([0.39, 0.7, 0.6, 0.7]), [1, 3])
        ema = br.thrust_ema([0] * 9 + [5], [5] * 9 + [0])
        self.assertAlmostEqual(ema[9], 0.1)                    # SMA seed of A/(A+D)

    def test_score_reweights_missing_components(self):
        comp = {"ad_line": (40.0, ""), "mcclellan": (90.0, "")}
        score, label, cov = br.score_from(comp)
        self.assertAlmostEqual(score, 60.0)                   # (15×40 + 10×90) / 25: lands on the boundary
        self.assertEqual((label, cov), ("Healthy", 25))
        self.assertEqual(br.score_from({"mcclellan": (59.9, "")})[1], "Mixed")
        self.assertEqual(br.score_from({}), (None, None, 0))


class TestUniverseFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.S, cls.stocks, cls.errors = fixture_series()
        cls.res = br.snapshot(cls.S, {"mode": "universe", "input": "fixture", "stocks_used": 10},
                              br.DIV_WINDOW, br.DIV_MIN_PCT, br.HL_WINDOW, br.load_members(str(MEMBERS)))

    def test_null_candles_skipped(self):
        self.assertEqual(self.errors, [])
        self.assertEqual(len(self.stocks), 10)
        self.assertEqual(len(self.stocks["ITC"]), 256)        # 4 blank/null rows dropped
        self.assertEqual(len(self.S["date"]), 259)            # sessions survive with 9 of 10 reporting
        self.assertEqual(self.S["active"][59], 9)

    def test_pct_above_50dma_matches_csvs(self):
        above = 0
        for f in UNI.glob("*.csv"):
            closes = [r[3] for r in read_rows(f)]
            above += closes[-1] > sum(closes[-50:]) / 50
        self.assertAlmostEqual(self.S["pct50"][-1], above * 10.0)

    def test_new_highs_lows_match_csvs(self):
        nh = nl = 0
        for f in UNI.glob("*.csv"):
            rows = read_rows(f)
            highs, lows = [r[1] for r in rows], [r[2] for r in rows]
            if len(rows) >= 252:
                nh += highs[-1] > max(highs[-252:-1])
                nl += lows[-1] < min(lows[-252:-1])
        self.assertEqual((self.S["nh"][-1], self.S["nl"][-1]), (nh, nl))
        self.assertGreaterEqual(nl, 1)                        # SUNPHARMA's planted 52-week low

    def test_advances_declines_match_csvs(self):
        adv = sum(r[-1][3] > r[-2][3] for r in (read_rows(f) for f in UNI.glob("*.csv")))
        self.assertEqual(self.S["adv"][-1], adv)
        self.assertEqual(self.S["ad_line"][-1], sum(self.S["net"]))

    def test_bearish_divergence_planted(self):
        dv = self.res["advance_decline"]["divergence"]
        self.assertEqual(dv["flag"], "bearish")
        self.assertGreater(dv["index_return_pct"], 1.0)
        self.assertLess(dv["ad_line_change"], 0)
        self.assertIn("rally carried by a few stocks", self.res["read"])
        self.assertTrue(any("heavyweight-led" in n for n in self.res["notes"]))

    def test_no_benchmark_no_divergence(self):
        S, _, _ = fixture_series(members=False, bench=False)
        self.assertIsNone(br.divergence_at(S, len(S["date"]) - 1, 20, 1.0))
        self.assertNotIn("sectors", S["components"][-1])

    def test_planted_thrust(self):
        self.assertEqual([self.S["date"][i] for i in self.S["thrusts"]], [date(2026, 6, 5)])

    def test_sectors(self):
        sec = self.res["sectors"]
        by = {r["sector"]: r for r in sec["table"]}
        self.assertEqual(sec["graded"], 7)
        self.assertEqual(sec["missing_members"], ["NOSUCH"])
        self.assertEqual(by["IT"]["stocks"], 2)
        self.assertAlmostEqual(sec["pct"], sec["participating"] / 7 * 100)

    def test_score_complete(self):
        sc = self.res["score"]
        self.assertEqual(sc["coverage_pct"], 100)
        self.assertEqual(sc["missing"], [])
        self.assertTrue(0 <= sc["value"] <= 100)
        self.assertLess(sc["value"], 40)                      # 8 of 10 stocks sliding: weak breadth


class TestAdCsv(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rows, cls.found = br.load_ad_csv(str(AD))
        cls.S = br.derive(br.ad_series(rows, None), br.DIV_WINDOW)

    def test_blank_row_skipped_and_columns_found(self):
        self.assertEqual(len(self.S["date"]), 159)
        self.assertEqual(set(self.found), {"adv", "dec", "unch", "nh", "nl", "index"})

    def test_thrust_and_divergence(self):
        self.assertEqual([self.S["date"][i] for i in self.S["thrusts"]], [date(2026, 8, 3)])
        dv = br.divergence_at(self.S, len(self.S["date"]) - 1, 20, 1.0)
        self.assertEqual(dv["flag"], "bearish")

    def test_cli_status(self):
        code, out, _ = run(["status", "--ad-csv", str(AD), "--json"])
        self.assertEqual(code, 0)
        data = strict_json(out)
        self.assertEqual(data["mode"], "ad-csv")
        self.assertEqual(data["score"]["coverage_pct"], 40)
        self.assertIn("participation", data["score"]["missing"])
        self.assertEqual(data["highs_lows"]["new_highs"], 27)
        self.assertIsNone(data["participation"]["pct_above_dma"]["50"]["pct"])
        code, out, _ = run(["status", "--ad-csv", str(AD), "--as-of", "2026-08-05"])
        self.assertIn("thrust fired 2 session(s) ago", out)

    def test_minimal_columns(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "ad.csv")
        with open(path, "w") as fh:
            fh.write("DATE,ADVANCES,DECLINES\n02-Nov-2026,1500,800\n03-Nov-2026,null,700\n04-Nov-2026,600,1700\n")
        code, out, _ = run(["status", "--ad-csv", path, "--json"])
        data = strict_json(out)
        self.assertEqual(code, 0)
        self.assertEqual(data["sessions"], 2)
        self.assertIsNone(data["highs_lows"])
        self.assertIsNone(data["advance_decline"]["divergence"])


class TestCLI(unittest.TestCase):
    def test_status_json_and_text(self):
        base = ["--csv-dir", str(UNI), "--benchmark-csv", str(NIFTY), "--members", str(MEMBERS)]
        code, out, _ = run(["status", *base, "--json"])
        self.assertEqual(code, 0)
        data = strict_json(out)
        self.assertEqual(data["stocks_used"], 10)
        self.assertEqual(data["advance_decline"]["divergence"]["flag"], "bearish")
        self.assertEqual(data["thrust"]["events"], ["2026-06-05"])
        code, out, _ = run(["status", *base])
        self.assertEqual(code, 0)
        for s in ("Health score", "BEARISH DIVERGENCE", "Sectors participating", "Read:"):
            self.assertIn(s, out)

    def test_history_json_and_csv(self):
        tmp = tempfile.mkdtemp()
        out_csv = os.path.join(tmp, "h.csv")
        code, out, _ = run(["history", "--csv-dir", str(UNI), "--sessions", "15", "--out", out_csv, "--json"])
        self.assertEqual(code, 0)
        rows = strict_json(out)["rows"]
        self.assertEqual(len(rows), 15)
        self.assertEqual(rows[-1]["date"], "2026-10-30")
        with open(out_csv) as fh:
            self.assertEqual(len(list(csv.DictReader(fh))), 15)
        code, out, _ = run(["history", "--csv-dir", str(UNI), "--as-of", "2026-06-05", "--sessions", "3"])
        self.assertIn("2026-06-05", out.splitlines()[-2])
        self.assertIn("*", out.splitlines()[-2])              # thrust session marked

    def test_universe_file_filters_folder(self):
        tmp = tempfile.mkdtemp()
        uf = os.path.join(tmp, "ind_list.csv")
        with open(uf, "w") as fh:
            fh.write("Company Name,Industry,Symbol,Series,ISIN Code\n"
                     "HDFC Bank Ltd.,Financial Services,HDFCBANK,EQ,X1\n"
                     "Infosys Ltd.,Information Technology,INFY,EQ,X2\n"
                     "Missing Ltd.,Chemicals,ABSENT,EQ,X3\n")
        code, out, _ = run(["status", "--csv-dir", str(UNI), "--universe-file", uf, "--json"])
        self.assertEqual(code, 0)
        data = strict_json(out)
        self.assertEqual(data["stocks_used"], 2)
        self.assertEqual(data["not_in_dir"], ["ABSENT"])
        self.assertEqual({r["sector"] for r in data["sectors"]["table"]},
                         {"FINANCIAL SERVICES", "INFORMATION TECHNOLOGY"})

    def test_all_null_stock_is_skipped_not_fatal(self):
        tmp = tempfile.mkdtemp()
        for f in ("TCS.csv", "INFY.csv"):
            with open(UNI / f) as src, open(os.path.join(tmp, f), "w") as dst:
                dst.write(src.read())
        with open(os.path.join(tmp, "JUNK.csv"), "w") as fh:
            fh.write("Date,Open,High,Low,Close,Volume\n2026-10-29,,,,,\n2026-10-30,null,null,null,null,0\n")
        code, out, _ = run(["status", "--csv-dir", tmp, "--json"])
        self.assertEqual(code, 0)
        data = strict_json(out)
        self.assertEqual(data["stocks_used"], 2)
        self.assertEqual(data["skipped"][0]["symbol"], "JUNK")

    def test_bad_inputs_exit_cleanly(self):
        tmp = tempfile.mkdtemp()
        bad_members = os.path.join(tmp, "members.csv")
        with open(bad_members, "w") as fh:
            fh.write("a,b\nx,y\n")
        empty_dir = os.path.join(tmp, "empty")
        os.mkdir(empty_dir)
        cases = (["status"],
                 ["status", "--csv-dir", os.path.join(tmp, "missing")],
                 ["status", "--csv-dir", empty_dir],
                 ["status", "--csv-dir", str(UNI), "--members", bad_members],
                 ["status", "--csv-dir", str(UNI), "--as-of", "not-a-date"],
                 ["status", "--csv-dir", str(UNI), "--as-of", "2020-01-01"],
                 ["status", "--csv-dir", str(UNI), "--hl-window", "1"],
                 ["status", "--ad-csv", str(MEMBERS)],
                 ["status", "--ad-csv", str(AD), "--csv-dir", str(UNI)],
                 ["history", "--csv-dir", str(UNI), "--sessions", "0"],
                 ["status", "--csv-dir", str(UNI), "--benchmark-csv", os.path.join(tmp, "nope.csv")])
        for argv in cases:
            with self.subTest(argv=argv):
                code, out, err = run(argv)
                self.assertEqual(code, 2)
                self.assertTrue(err.startswith("error:"))
                self.assertEqual(len(err.strip().splitlines()), 1)
                self.assertNotIn("Traceback", err)


if __name__ == "__main__":
    unittest.main()
