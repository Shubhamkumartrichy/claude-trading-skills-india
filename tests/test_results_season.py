import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import date, datetime, time

from _load import FIXTURES, load

rs = load("results-season", "results")
ta = load("technical-analysis", "ta")

STRONG = str(FIXTURES / "results_strong.csv")      # post-market Fri 24-Jul-2026, gap-up, A
WEAK = str(FIXTURES / "results_weak.csv")          # pre-market Wed 05-Aug-2026, faded gap, fails
NEG = str(FIXTURES / "results_negative.csv")       # post-market Thu 13-Aug-2026, gap-down
MIXED = str(FIXTURES / "results_mixed.csv")        # market-hours Wed 22-Jul-2026, run-up into results
EVENTS = str(FIXTURES / "results_events.csv")
CAL = str(FIXTURES / "results_calendar.csv")


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = rs.main(argv)
    return rc, out.getvalue(), err.getvalue()


def strict_json(text):
    def bad(c):
        raise ValueError(f"non-standard JSON constant {c}")
    return json.loads(text, parse_constant=bad)


def rows(path):
    return ta.load_csv(path)


class TestDates(unittest.TestCase):
    def test_accepted_styles(self):
        want = date(2026, 10, 31)
        for s in ("2026-10-31", "31-Oct-2026", "31-OCT-2026", "31 Oct 2026", "31-October-2026",
                  "Oct 31, 2026", "20261031", "31/10/2026", "10/31/2026"):
            with self.subTest(s=s):
                self.assertEqual(rs.parse_date(s)[0], want)

    def test_time_suffix(self):
        d, t = rs.parse_date("31-Oct-2026 16:05")
        self.assertEqual((d, t), (date(2026, 10, 31), time(16, 5)))
        self.assertEqual(rs.parse_date("2026-10-31T09:05:00")[1], time(9, 5))
        self.assertEqual(rs.parse_date("2026-10-31 4:10 PM")[1], time(16, 10))

    def test_refuses_guessing(self):
        for s in ("03/04/2026", "11-12-2026", "31-Oct-26", "2026-02-30", "yesterday", ""):
            with self.subTest(s=s), self.assertRaises(ValueError):
                rs.parse_date(s)
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            rs.parse_date("03/04/2026")
        self.assertEqual(rs.parse_date("05/05/2026")[0], date(2026, 5, 5))     # same either way

    def test_timing(self):
        self.assertEqual(rs.parse_timing("post")[0], "post-market")
        self.assertEqual(rs.parse_timing("Market Hours")[0], "market-hours")
        self.assertEqual(rs.parse_timing("BMO")[0], "pre-market")
        self.assertEqual(rs.parse_timing("09:14")[0], "pre-market")
        self.assertEqual(rs.parse_timing("09:15")[0], "market-hours")
        self.assertEqual(rs.parse_timing("15:29")[0], "market-hours")
        self.assertEqual(rs.parse_timing("15:30")[0], "post-market")
        self.assertEqual(rs.parse_timing("unknown"), (None, None))
        with self.assertRaises(ValueError):
            rs.parse_timing("lunch")
        with self.assertRaisesRegex(ValueError, "contradicts"):
            rs.resolve_timing("post-market", time(10, 0), "x")


class TestReactionSession(unittest.TestCase):
    def test_calendar(self):
        cases = [
            (date(2026, 7, 24), "pre-market", date(2026, 7, 24)),
            (date(2026, 7, 24), "market-hours", date(2026, 7, 24)),
            (date(2026, 7, 24), "post-market", date(2026, 7, 27)),      # Fri after close -> Mon
            (date(2026, 9, 11), "post-market", date(2026, 9, 15)),      # weekend + Mon 14-Sep holiday
            (date(2026, 10, 1), "post-market", date(2026, 10, 5)),      # Fri 2-Oct holiday + weekend
            (date(2026, 11, 9), "post-market", date(2026, 11, 11)),     # Tue 10-Nov holiday
            (date(2026, 11, 14), "pre-market", date(2026, 11, 16)),     # Saturday board meeting
            (date(2026, 10, 20), "market-hours", date(2026, 10, 21)),   # filed on a holiday
        ]
        for d, timing, want in cases:
            with self.subTest(d=d, timing=timing):
                self.assertEqual(rs.reaction_session(d, timing), want)

    def test_unknown_timing_counts_both(self):
        self.assertEqual(rs.exposure_sessions(date(2026, 11, 20), None), [date(2026, 11, 20), date(2026, 11, 23)])
        self.assertEqual(rs.exposure_sessions(date(2026, 11, 14), None), [date(2026, 11, 16)])

    def test_from_data(self):
        r = rows(STRONG)
        for d, timing, want in [(date(2026, 7, 24), "pre-market", "2026-07-24"),
                                (date(2026, 7, 24), "market-hours", "2026-07-24"),
                                (date(2026, 7, 24), "post-market", "2026-07-27"),
                                (date(2026, 9, 11), "post-market", "2026-09-15"),
                                (date(2026, 10, 1), "post-market", "2026-10-05")]:
            with self.subTest(d=d, timing=timing):
                self.assertEqual(rs.analyse_reaction(r, d, timing)["reaction_date"].isoformat(), want)

    def test_missing_bar_and_special_session_are_flagged(self):
        r = [x for x in rows(STRONG) if x["date"].date() != date(2026, 7, 27)]
        rx = rs.analyse_reaction(r, date(2026, 7, 24), "post-market")
        self.assertEqual(rx["reaction_date"], date(2026, 7, 28))
        self.assertTrue(any("No bar for the expected reaction session 2026-07-27" in n for n in rx["notes"]))
        r = rows(STRONG)
        extra = dict(r[0], date=datetime(2026, 7, 26))                  # a Sunday bar in the feed
        r = ta._clean(r + [extra])
        rx = rs.analyse_reaction(r, date(2026, 7, 24), "post-market")
        self.assertEqual(rx["reaction_date"], date(2026, 7, 27))
        self.assertTrue(any("Muhurat" in n for n in rx["notes"]))
        rx = rs.analyse_reaction(r, date(2026, 7, 24), "post-market", reaction_date=date(2026, 7, 26))
        self.assertEqual(rx["reaction_date"], date(2026, 7, 26))


class TestReactionMetrics(unittest.TestCase):
    def test_strong_hand_checked(self):
        r = rows(STRONG)
        rx = rs.analyse_reaction(r, date(2026, 7, 24), "post-market")
        self.assertAlmostEqual(rx["prev_close"], 200.0)
        self.assertAlmostEqual(rx["gap_pct"], 6.0)
        self.assertAlmostEqual(rx["reaction_pct"], 9.0)
        self.assertAlmostEqual(rx["close_location"], 7.5 / 8.5)
        self.assertAlmostEqual(rx["volume_ratio"], 4.0)
        i = rx["_idx"]
        atr = ta.atr([x["high"] for x in r], [x["low"] for x in r], [x["close"] for x in r], 14)[i - 1]
        self.assertAlmostEqual(rx["move_atr"], 18 / atr)
        self.assertAlmostEqual(rx["gap_atr"], 12 / atr)
        ft = {f["sessions"]: f for f in rx["follow_through"]}
        self.assertEqual(sorted(ft), [1, 3, 5, 10, 20])
        expect = {1: ("2026-07-28", 219.5), 3: ("2026-07-30", 221.0), 5: ("2026-08-03", 223.5),
                  10: ("2026-08-10", 229.0), 20: ("2026-08-24", 226.5)}
        for n, (d, c) in expect.items():
            with self.subTest(n=n):
                self.assertEqual(ft[n]["date"].isoformat(), d)
                self.assertAlmostEqual(ft[n]["from_reaction_close_pct"], (c / 218 - 1) * 100)
                self.assertAlmostEqual(ft[n]["from_pre_close_pct"], (c / 200 - 1) * 100)
                self.assertTrue(ft[n]["held_reaction_low"])
        self.assertEqual(rx["gap_fill"]["direction"], "up")
        self.assertFalse(rx["gap_fill"]["filled"])
        self.assertTrue(rx["pre"]["above_sma50"] and rx["pre"]["above_sma200"])
        self.assertAlmostEqual(rx["pre"]["high_52w"], 201.0)

    def test_faded_gap_fills(self):
        rx = rs.analyse_reaction(rows(WEAK), date(2026, 8, 5), "pre-market")
        self.assertEqual(rx["reaction_date"], date(2026, 8, 5))
        self.assertAlmostEqual(rx["gap_pct"], 5.0)
        self.assertAlmostEqual(rx["reaction_pct"], 1.0)
        self.assertAlmostEqual(rx["close_location"], 2 / 15.5)
        self.assertAlmostEqual(rx["volume_ratio"], 1.3)
        g = rx["gap_fill"]
        self.assertTrue(g["filled"])
        self.assertEqual((g["date"], g["sessions"]), (date(2026, 8, 7), 2))
        self.assertFalse(rx["follow_through"][1]["held_reaction_low"])   # +3 sessions: low 298 < 301

    def test_gap_down_and_market_hours(self):
        rx = rs.analyse_reaction(rows(NEG), date(2026, 8, 13), "post-market", clock=time(16, 10))
        self.assertEqual(rx["reaction_date"], date(2026, 8, 14))
        self.assertAlmostEqual(rx["gap_pct"], -7.6)
        self.assertAlmostEqual(rx["reaction_pct"], -10.4)
        self.assertAlmostEqual(rx["volume_ratio"], 5.0)
        self.assertEqual(rx["gap_fill"]["direction"], "down")
        self.assertFalse(rx["gap_fill"]["filled"])
        self.assertFalse(rx["pre"]["above_sma200"])
        mx = rs.analyse_reaction(rows(MIXED), date(2026, 7, 22), "market-hours", clock=time(15, 10))
        self.assertEqual(mx["gap_fill"]["direction"], "none")             # 0.25% gap is not a gap
        self.assertAlmostEqual(mx["reaction_pct"], 3.5)
        self.assertTrue(any("open came before the news" in n for n in mx["notes"]))
        self.assertTrue(any("15:00-15:30" in n for n in mx["notes"]))

    def test_too_little_history(self):
        with self.assertRaisesRegex(ValueError, "at least 20 sessions"):
            rs.analyse_reaction(rows(STRONG)[-45:], date(2026, 9, 11), "post-market")


class TestScore(unittest.TestCase):
    BASE = dict(move_atr=2.0, volume_ratio=2.5, close_location=0.6, above_sma50=True, above_sma200=True,
                from_high_pct=-4.0, ret60_pct=15.0)

    def s(self, **kw):
        return rs.score_factors(**dict(self.BASE, **kw))["score"]

    def test_monotonic(self):
        for key, values in (("move_atr", [-3, -1, 0, 0.5, 1, 2, 3, 5]), ("volume_ratio", [0.5, 1, 1.5, 2.5, 4, 8]),
                            ("close_location", [0, 0.25, 0.5, 0.75, 1]),
                            ("from_high_pct", [-30, -10, -5, -2, 0, 5])):
            scores = [self.s(**{key: v}) for v in values]
            with self.subTest(key=key):
                self.assertEqual(scores, sorted(scores), scores)
                self.assertLess(scores[0], scores[-1])
        runs = [self.s(ret60_pct=v) for v in (-20, 0, 10, 20, 30, 40, 80)]
        self.assertEqual(runs, sorted(runs, reverse=True))
        self.assertGreater(self.s(above_sma200=True), self.s(above_sma200=False))
        self.assertGreater(self.s(above_sma50=True), self.s(above_sma50=False))

    def test_bounds_and_grades(self):
        best = rs.score_factors(5, 6, 1, True, True, 3, 0)
        worst = rs.score_factors(-5, 0.3, 0, False, False, -40, 90)
        self.assertEqual((best["score"], best["grade"]), (100, "A"))
        self.assertEqual((worst["score"], worst["grade"]), (0, "D"))
        for m in (-4, -1, 0, 1, 2, 4):
            for v in (None, 0.5, 3):
                for c in (0, 0.5, 1):
                    sc = rs.score_factors(m, v, c, None, False, None, None)
                    self.assertTrue(0 <= sc["score"] <= 100)
                    self.assertIn(sc["grade"], "ABCD")
        self.assertEqual([rs.grade_for(x) for x in (100, 80, 79, 60, 59, 40, 39, 0)],
                         ["A", "A", "B", "B", "C", "C", "D", "D"])

    def test_gate_and_rescale(self):
        # A down day earns nothing for volume, trend or run-up; only close strength remains.
        sc = rs.score_factors(-2, 5, 1.0, True, True, 0, 0)
        self.assertEqual(sc["score"], 15)
        nv = rs.score_factors(2, None, 0.6, True, True, -4, 15)
        self.assertIn("volume", nv["dropped"])
        self.assertTrue(nv["rescaled"])
        self.assertTrue(0 <= nv["score"] <= 100)

    def test_fixture_grades(self):
        got = {}
        for path, d, timing in ((STRONG, date(2026, 7, 24), "post-market"), (MIXED, date(2026, 7, 22), "market-hours"),
                                (WEAK, date(2026, 8, 5), "pre-market"), (NEG, date(2026, 8, 13), "post-market")):
            sc = rs.score_reaction(rs.analyse_reaction(rows(path), d, timing))
            got[path] = sc
            self.assertAlmostEqual(sum(f["points"] for f in sc["factors"] if f["points"] is not None),
                                   sc["score"], delta=0.5)
            self.assertEqual(sum(f["weight"] for f in sc["factors"]), 100)
            self.assertTrue(sc["read"])
        self.assertEqual(got[STRONG]["grade"], "A")
        self.assertEqual(got[MIXED]["grade"], "B")
        self.assertEqual(got[WEAK]["grade"], "D")
        self.assertEqual((got[NEG]["grade"], got[NEG]["bias"]), ("D", "bearish"))
        self.assertIn("faded", got[WEAK]["read"])
        self.assertIn("priced in", got[MIXED]["read"])


class TestDrift(unittest.TestCase):
    def drift(self, r, d=date(2026, 7, 24), timing="post-market", **kw):
        return rs.drift_watch(r, rs.analyse_reaction(r, d, timing), **kw)

    def test_pullback_then_breakout(self):
        dr = self.drift(rows(STRONG))
        self.assertEqual(dr["status"], "triggered")
        self.assertEqual(dr["pullback"]["week_ending"], date(2026, 8, 21))
        self.assertAlmostEqual(dr["trigger"], 232.0)
        self.assertAlmostEqual(dr["stop"], 222.0 * 0.995)
        self.assertEqual(dr["entry"]["date"], date(2026, 8, 26))
        self.assertAlmostEqual(dr["entry"]["price"], 232.0)
        self.assertAlmostEqual(dr["gap_day_low"], 210.5)
        self.assertGreater(dr["r_multiple_now"], 2)

    def test_states_over_time(self):
        r = rows(STRONG)
        cut = lambda d: [x for x in r if x["date"].date() <= d]
        self.assertEqual(self.drift(cut(date(2026, 8, 14)))["status"], "watching")
        pb = self.drift(cut(date(2026, 8, 25)))
        self.assertEqual(pb["status"], "pullback-set")
        self.assertAlmostEqual(pb["trigger"], 232.0)
        self.assertAlmostEqual(pb["risk_pct"], (232 - 220.89) / 232 * 100)
        self.assertEqual(self.drift(r, max_weeks=2)["status"], "expired")

    def test_stopped_and_gap_entry(self):
        r = [dict(x) for x in rows(STRONG)]
        k = next(i for i, x in enumerate(r) if x["date"].date() == date(2026, 9, 3))
        r[k].update(low=219.0)
        dr = self.drift(r)
        self.assertEqual(dr["status"], "stopped")
        self.assertEqual(dr["exit"]["date"], date(2026, 9, 3))
        self.assertAlmostEqual(dr["r_multiple_now"], -1.0)
        r = [dict(x) for x in rows(STRONG)]
        k = next(i for i, x in enumerate(r) if x["date"].date() == date(2026, 8, 26))
        r[k].update(open=233.0)
        dr = self.drift(r)
        self.assertTrue(dr["entry"]["gapped_above"])
        self.assertAlmostEqual(dr["entry"]["price"], 233.0)

    def test_failed_reaction(self):
        dr = self.drift(rows(WEAK), date(2026, 8, 5), "pre-market")
        self.assertEqual(dr["status"], "failed")
        self.assertEqual(dr["failed"]["date"], date(2026, 8, 10))
        self.assertAlmostEqual(dr["failed"]["close"], 299.5)
        self.assertIn("Failed reaction", dr["plan"][0])

    def test_not_applicable(self):
        self.assertEqual(self.drift(rows(NEG), date(2026, 8, 13))["status"], "not-applicable")
        self.assertEqual(self.drift(rows(MIXED), date(2026, 7, 22), "market-hours")["status"], "not-applicable")
        lowered = self.drift(rows(MIXED), date(2026, 7, 22), "market-hours", min_gap_pct=3)   # +3.5% now qualifies
        self.assertNotEqual(lowered["status"], "not-applicable")
        self.assertAlmostEqual(lowered["gap_day_low"], 399.0)


class TestScanAndCalendar(unittest.TestCase):
    def test_scan_ranks(self):
        rc, out, _ = run(["scan", "--events", EVENTS, "--csv-dir", str(FIXTURES), "--prefix", "results_", "--json"])
        self.assertEqual(rc, 0)
        d = strict_json(out)
        self.assertEqual([x["reaction"]["name"] for x in d["results"]], ["STRONG", "MIXED", "WEAK", "NEGATIVE"])
        scores = [x["score"]["score"] for x in d["results"]]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertEqual([x["drift"]["status"] for x in d["results"]],
                         ["triggered", "not-applicable", "failed", "not-applicable"])
        self.assertEqual([e["symbol"] for e in d["errors"]], ["PENDING"])
        self.assertEqual(d["results"][2]["reaction"]["results_date"], "2026-08-05")      # 05-Aug-2026 parsed
        self.assertEqual(d["results"][3]["reaction"]["announced_at"], "16:10")
        rc, out, _ = run(["scan", "--events", EVENTS, "--csv-dir", str(FIXTURES), "--prefix", "results_",
                          "--top", "2"])
        self.assertEqual(rc, 0)
        self.assertIn("STRONG", out)
        self.assertNotIn("NEGATIVE ", out)

    def test_calendar_window(self):
        events, _ = rs.load_events(CAL)
        res = rs.calendar_check(events, date(2026, 11, 2), date(2026, 11, 13))
        status = {e["symbol"]: e["status"] for e in res["events"]}
        self.assertEqual(status, {"ALPHA": "inside", "BETA": "inside", "GAMMA": "near", "ZETA": "near",
                                  "DELTA": "outside", "EPSILON": "outside"})
        beta = next(e for e in res["events"] if e["symbol"] == "BETA")
        self.assertEqual(beta["reaction_sessions"], [date(2026, 11, 11)])               # 10-Nov holiday
        gamma = next(e for e in res["events"] if e["symbol"] == "GAMMA")
        self.assertIn("exit by 15:30", gamma["note"])
        self.assertTrue(res["any_inside"])
        self.assertEqual(res["discipline_flag"], "--event yes")
        self.assertEqual([s["quarter"] for s in res["seasons"]], ["Q2 FY27"])
        wide = rs.calendar_check(events, date(2026, 11, 2), date(2026, 11, 20))
        self.assertEqual({e["symbol"] for e in wide["events"] if e["status"] == "inside"},
                         {"ALPHA", "BETA", "GAMMA", "ZETA", "DELTA"})                  # unknown timing counted
        quiet = rs.calendar_check(events, date(2026, 12, 1), date(2026, 12, 4))
        self.assertFalse(quiet["any_inside"])
        self.assertEqual(quiet["discipline_flag"], "--event no")

    def test_calendar_cli_symbols(self):
        rc, out, _ = run(["calendar-check", "--events", CAL, "--from", "02-Nov-2026", "--to", "2026-11-13",
                          "--symbols", "alpha,GAMMA,OMEGA", "--json"])
        self.assertEqual(rc, 0)
        d = strict_json(out)
        self.assertEqual([e["symbol"] for e in d["events"]], ["ALPHA", "GAMMA"])
        self.assertEqual(d["missing"][0]["symbol"], "OMEGA")
        self.assertIn("14-Nov-2026", d["missing"][0]["note"])
        rc, out, _ = run(["calendar-check", "--events", CAL, "--from", "2026-11-02", "--to", "2026-11-13"])
        self.assertEqual(rc, 0)
        self.assertIn("[INSIDE] ALPHA", out)
        self.assertIn("--event yes", out)


class TestCLI(unittest.TestCase):
    def test_json_is_strict(self):
        for cmd in ("reaction", "score", "drift"):
            for path, d, t in ((STRONG, "2026-07-24", "post-market"), (NEG, "13-Aug-2026", "16:10")):
                with self.subTest(cmd=cmd, path=path):
                    rc, out, _ = run([cmd, "--csv", path, "--date", d, "--timing", t, "--json"])
                    self.assertEqual(rc, 0)
                    doc = strict_json(out)
                    self.assertNotIn("_idx", json.dumps(doc))
        rc, out, _ = run(["drift", "--csv", STRONG, "--date", "2026-07-24", "--timing", "post-market", "--json"])
        self.assertEqual(strict_json(out)["drift"]["entry"]["date"], "2026-08-26")

    def test_text_output(self):
        rc, out, _ = run(["score", "--csv", STRONG, "--date", "2026-07-24", "--timing", "post-market"])
        self.assertEqual(rc, 0)
        for s in ("reaction session Mon 27-Jul-2026", "grade A", "Reaction size", "Read:", "₹218.00"):
            self.assertIn(s, out)
        rc, out, _ = run(["drift", "--events", EVENTS, "--csv-dir", str(FIXTURES), "--prefix", "results_"])
        self.assertEqual(rc, 0)
        self.assertIn("TRIGGERED", out)
        self.assertIn("FAILED", out)
        self.assertIn("skipped PENDING", out)

    def test_null_candles_are_skipped(self):
        r = rows(STRONG)
        candles = [[x["date"].strftime("%Y-%m-%dT00:00:00+05:30"), x["open"], x["high"], x["low"], x["close"],
                    x["volume"]] for x in r]
        candles.insert(200, ["2026-03-28T00:00:00+05:30", None, None, None, None, None])
        candles.insert(100, {"date": "2025-10-25", "open": None, "high": 1, "low": 1, "close": 1})
        candles.insert(50, ["2025-08-16T00:00:00+05:30", 0, 0, 0, 0, 0])
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "c.json")
            with open(p, "w") as fh:
                json.dump(candles, fh)
            self.assertEqual(len(rs.load_candles(p)), len(r))
            rc, out, _ = run(["reaction", "--candles", p, "--date", "2026-07-24", "--timing", "post-market", "--json"])
            self.assertEqual(rc, 0)
            self.assertAlmostEqual(strict_json(out)["volume_ratio"], 4.0)

    def test_bad_input_exits_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            amb = os.path.join(tmp, "amb.csv")
            with open(amb, "w") as fh:
                fh.write("symbol,date,timing\nAAA,2026-11-04,post-market\nBBB,03/04/2026,pre-market\n")
            nocol = os.path.join(tmp, "nocol.csv")
            with open(nocol, "w") as fh:
                fh.write("name,when\nAAA,2026-11-04\n")
            cases = [
                ["reaction", "--csv", STRONG, "--date", "03/04/2026", "--timing", "pre-market"],
                ["reaction", "--csv", STRONG, "--date", "2026-02-30", "--timing", "pre-market"],
                ["reaction", "--csv", STRONG, "--date", "31-Oct-26", "--timing", "pre-market"],
                ["score", "--csv", STRONG, "--date", "2026-07-24", "--timing", "lunch"],
                ["score", "--csv", STRONG, "--date", "2026-07-24"],
                ["score", "--csv", STRONG, "--date", "2026-07-24 10:00", "--timing", "post-market"],
                ["drift", "--csv", STRONG, "--date", "2026-11-20", "--timing", "pre-market"],
                ["drift", "--csv", STRONG, "--date", "2026-07-24", "--timing", "post-market", "--max-weeks", "0"],
                ["reaction", "--csv", STRONG, "--date", "2026-07-24", "--timing", "pre-market",
                 "--reaction-date", "2026-07-25"],
                ["reaction", "--csv", "/nonexistent/x.csv", "--date", "2026-07-24", "--timing", "pre-market"],
                ["reaction", "--date", "2026-07-24", "--timing", "pre-market"],
                ["scan", "--events", amb, "--csv-dir", str(FIXTURES)],
                ["scan", "--events", nocol, "--csv-dir", str(FIXTURES)],
                ["scan", "--events", EVENTS, "--csv-dir", "/nonexistent"],
                ["scan"],
                ["calendar-check", "--events", amb, "--to", "2026-11-13"],
                ["calendar-check", "--events", CAL, "--from", "2026-11-13", "--to", "2026-11-02"],
                ["calendar-check", "--events", CAL, "--to", "13/11/26"],
            ]
            for argv in cases:
                with self.subTest(argv=argv):
                    rc, out, err = run(argv)
                    self.assertEqual(rc, 2)
                    self.assertTrue(err.startswith("error:"), err)
                    self.assertEqual(len(err.strip().splitlines()), 1)
                    self.assertNotIn("Traceback", err)
            rc, _, err = run(["calendar-check", "--events", amb, "--to", "2026-11-13"])
            self.assertIn("ambiguous", err)
            self.assertIn("line 3", err)
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(io.StringIO()):
            rs.main(["calendar-check", "--events", CAL])                      # argparse: --to missing
        self.assertEqual(cm.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
