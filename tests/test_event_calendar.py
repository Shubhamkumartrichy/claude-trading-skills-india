import contextlib
import io
import json
import os
import re
import tempfile
import unittest
from datetime import date, time

from _load import FIXTURES, load

ev = load("event-calendar", "events")
opt = load("fno-options-analysis", "options")

USER = str(FIXTURES / "calendar_events.csv")           # TCS post-mkt 20-Nov, INFY duplicate, HDFCBANK
MARKET = str(FIXTURES / "calendar_market.csv")         # date,category,title rows (lock-in, rbi dup, ex-date)
STALE_REF = str(FIXTURES / "calendar_reference_stale.csv")
AMBIGUOUS = str(FIXTURES / "calendar_ambiguous.csv")
REF_ROWS = ev.load_reference(ev.DEFAULT_REFERENCE)


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = ev.main(argv)
    return rc, out.getvalue(), err.getvalue()


def strict_json(text):
    def bad(c):
        raise ValueError(f"non-standard JSON constant {c}")
    return json.loads(text, parse_constant=bad)


def build(a, b, user=()):
    return ev.build(a, b, REF_ROWS, list(user))[0]


class TestExpiries(unittest.TestCase):
    def test_match_options_py(self):
        a, b = date(2026, 11, 1), date(2027, 1, 31)
        events = [e for e in ev.expiry_events(a, b) if e["category"] == "expiry"]
        for u in ("NIFTY", "BANKNIFTY", "SENSEX", "BANKEX"):
            want = {x["date"] for x in opt.expiries(u, a, 30) if date.fromisoformat(x["date"]) <= b}
            got = {e["date"].isoformat() for e in events if u in e["underlyings"]}
            with self.subTest(u=u):
                self.assertEqual(got, want)

    def test_holiday_shifted_monthly(self):
        # Tue 24-Nov-2026 is an exchange holiday: the NSE monthly expiry moves to Mon 23-Nov.
        events = ev.expiry_events(date(2026, 11, 16), date(2026, 11, 30))
        nse_m = [e for e in events if e["category"] == "expiry" and e["exchange"] == "NSE"
                 and e["expiry_type"] == "monthly"]
        self.assertEqual(len(nse_m), 1)
        self.assertEqual(nse_m[0]["date"], date(2026, 11, 23))
        self.assertTrue(nse_m[0]["shifted_for_holiday"])
        self.assertIn("Tue 24-Nov-2026", nse_m[0]["note"])
        self.assertEqual(nse_m[0]["impact"], "high")
        self.assertIn("BANKNIFTY", nse_m[0]["underlyings"])
        week = [e for e in events if e["category"] == "expiry-week"]
        self.assertEqual((week[0]["date"], week[0]["end"]), (date(2026, 11, 23), None))

    def test_beyond_holiday_list_flagged(self):
        events = ev.expiry_events(date(2027, 3, 1), date(2027, 3, 31))
        self.assertTrue(all(e["holiday_list_missing"] for e in events))


class TestRules(unittest.TestCase):
    def cpi(self, y, m):
        return [e["date"] for e in ev.rule_events(date(y, m, 1), date(y, m, 28)) if e["category"] == "cpi"]

    def test_cpi_12th_and_weekends(self):
        self.assertEqual(self.cpi(2026, 11), [date(2026, 11, 12)])     # Thursday
        self.assertEqual(self.cpi(2026, 9), [date(2026, 9, 14)])       # 12th is a Saturday -> Monday
        self.assertEqual(self.cpi(2026, 7), [date(2026, 7, 13)])       # 12th is a Sunday -> Monday
        e = [x for x in ev.rule_events(date(2026, 11, 1), date(2026, 11, 28)) if x["category"] == "cpi"][0]
        self.assertEqual(e["time_ist"], time(16, 0))
        self.assertIn("Oct 2026", e["title"])
        self.assertEqual(ev.finish(e)["status"], "expected by rule, confirm on the day")

    def test_wpi_and_gdp(self):
        evs = ev.rule_events(date(2026, 11, 1), date(2026, 11, 30))
        self.assertEqual([e["date"] for e in evs if e["category"] == "wpi"], [date(2026, 11, 16)])
        self.assertEqual([e["date"] for e in evs if e["category"] == "gdp"], [date(2026, 11, 30)])

    def test_uncertain_rules_labelled(self):
        pmi = [ev.finish(e) for e in ev.rule_events(date(2026, 12, 1), date(2026, 12, 31)) if e["category"] == "pmi"]
        self.assertEqual([e["date"] for e in pmi], [date(2026, 12, 1), date(2026, 12, 3)])
        self.assertTrue(all("uncertain" in e["status"] for e in pmi))

    def test_reference_replaces_rule(self):
        events = build(date(2027, 1, 1), date(2027, 2, 10))
        jobs = [e for e in events if e["category"] == "us-jobs"]
        self.assertEqual([(e["date"], e["kind"]) for e in jobs],
                         [(date(2027, 1, 8), "verified"), (date(2027, 2, 5), "verified")])
        budget = [e for e in events if e["category"] == "budget"]
        self.assertEqual(len(budget), 1)


class TestComputed(unittest.TestCase):
    def test_results_seasons(self):
        evs = ev.season_events(date(2026, 4, 1), date(2027, 5, 31))
        deadlines = {e["quarter"]: e["deadline"] for e in evs}
        self.assertEqual(deadlines["Q4 FY26"], date(2026, 5, 30))
        self.assertEqual(deadlines["Q1 FY27"], date(2026, 8, 14))
        self.assertEqual(deadlines["Q2 FY27"], date(2026, 11, 14))
        self.assertEqual(deadlines["Q3 FY27"], date(2027, 2, 14))
        self.assertEqual(deadlines["Q4 FY27"], date(2027, 5, 30))
        span = [e for e in evs if e["quarter"] == "Q2 FY27" and e["end"]][0]
        self.assertEqual((span["date"], span["end"]), (date(2026, 10, 1), date(2026, 11, 14)))
        self.assertIn("45 days", span["note"])
        q4 = [e for e in evs if e["quarter"] == "Q4 FY27" and e["end"]][0]
        self.assertIn("60 days", q4["note"])

    def test_advance_tax(self):
        evs = [e for e in ev.tax_events(date(2026, 4, 1), date(2027, 3, 31)) if "Advance tax" in e["title"]]
        got = {e["date"] for e in evs if date(2026, 4, 1) <= e["date"] <= date(2027, 3, 31)}
        self.assertEqual(got, {date(2026, 6, 15), date(2026, 9, 15), date(2026, 12, 15), date(2027, 3, 15)})
        mar = [e for e in evs if e["date"] == date(2027, 3, 15)][0]
        self.assertIn("100%", mar["title"])
        self.assertIn("FY27", mar["title"])

    def test_fy_end_session_and_holidays(self):
        evs = ev.tax_events(date(2026, 3, 1), date(2026, 3, 31))
        last = [e for e in evs if e["title"].startswith("Last trading session of FY25-26")][0]
        self.assertEqual(last["date"], date(2026, 3, 30))                  # 31-Mar-2026 is a holiday
        hol = [e for e in ev.holiday_events(date(2026, 11, 1), date(2026, 11, 30)) if e["category"] == "holiday"]
        self.assertEqual([e["date"] for e in hol], [date(2026, 11, 10), date(2026, 11, 24)])

    def test_long_weekend(self):
        lw = [e for e in ev.holiday_events(date(2026, 1, 20), date(2026, 4, 6)) if e["category"] == "long-weekend"]
        spans = [(e["date"], e["end"]) for e in lw]
        self.assertIn((date(2026, 1, 24), date(2026, 1, 26)), spans)      # Sat-Sun + Republic Day
        self.assertIn((date(2026, 4, 3), date(2026, 4, 5)), spans)        # Good Friday + weekend
        self.assertNotIn(date(2026, 3, 28), [a for a, _ in spans])        # Mon 30-Mar trades

    def test_fomc_converted_to_ist(self):
        events = build(date(2026, 6, 1), date(2026, 12, 31))
        fomc = {e["date"]: e["time_ist"] for e in events if e["category"] == "fomc"}
        self.assertEqual(fomc[date(2026, 6, 17)], time(23, 30))           # daylight time
        self.assertEqual(fomc[date(2026, 12, 10)], time(0, 30))           # standard time, next IST day
        dec = [e for e in events if e["category"] == "fomc" and e["date"] == date(2026, 12, 10)][0]
        self.assertEqual(dec["reacts"], [date(2026, 12, 10)])


class TestUserEvents(unittest.TestCase):
    def test_merge_and_dedupe(self):
        rc, out, err = run(["range", "--from", "2026-11-16", "--to", "2026-12-10", "--events", USER,
                            "--events", MARKET, "--category", "company,rbi", "--json"])
        self.assertEqual(rc, 0, err)
        data = strict_json(out)
        evs = data["events"]
        infy = [e for e in evs if e["symbol"] == "INFY"]
        self.assertEqual(len(infy), 1)
        self.assertEqual(infy[0]["timing"], "pre-market")                 # row with timing wins
        self.assertEqual(infy[0]["reacts"], ["2026-11-25"])               # 24-Nov holiday -> next session
        rbi = [e for e in evs if e["category"] == "rbi"]
        self.assertEqual(len(rbi), 1)
        self.assertEqual(rbi[0]["kind"], "verified")                      # reference beats user duplicate
        self.assertGreaterEqual(data["duplicates_merged"], 2)
        cats = {e["category"] for e in evs}
        self.assertEqual(cats, {"results", "lock-in", "ex-date", "rbi"})

    def test_category_filter(self):
        rc, out, _ = run(["range", "--from", "2026-11-01", "--to", "2027-02-28", "--category", "rbi", "--json"])
        self.assertEqual(rc, 0)
        evs = strict_json(out)["events"]
        self.assertEqual([e["date"] for e in evs], ["2026-12-04", "2027-02-05"])
        rc, out, _ = run(["next", "--category", "fed", "--count", "2", "--today", "2026-11-13", "--json"])
        self.assertEqual([e["date"] for e in strict_json(out)["events"]], ["2026-12-10", "2027-01-28"])
        rc, _, err = run(["range", "--from", "2026-11-01", "--to", "2026-11-30", "--category", "weather"])
        self.assertEqual(rc, 2)
        self.assertTrue(err.startswith("error:"))

    def test_min_impact(self):
        rc, out, _ = run(["month", "--month", "2026-12", "--min-impact", "high", "--json"])
        evs = strict_json(out)["events"]
        self.assertTrue(evs)
        self.assertTrue(all(e["impact"] == "high" for e in evs))


class TestWindow(unittest.TestCase):
    def test_inside_yes(self):
        rc, out, err = run(["window", "--from", "2026-11-16", "--to", "2026-11-27", "--symbols", "TCS,INFY,SBIN",
                            "--events", USER, "--json"])
        self.assertEqual(rc, 0, err)
        res = strict_json(out)
        self.assertEqual(res["event_risk"], "yes")
        self.assertEqual(res["discipline_flag"], "--event yes")
        syms = {e["symbol"] for e in res["events"] if e["symbol"]}
        self.assertEqual(syms, {"TCS", "INFY"})                           # HDFCBANK not held
        self.assertEqual([m["symbol"] for m in res["missing"]], ["SBIN"])
        self.assertTrue(any(e["category"] == "expiry" for e in res["events"]))

    def test_post_market_on_exit_day_is_near(self):
        rc, out, _ = run(["window", "--from", "2026-11-16", "--to", "2026-11-20", "--symbols", "TCS",
                          "--events", USER, "--json"])
        res = strict_json(out)
        self.assertEqual(res["event_risk"], "no")
        self.assertEqual([e["symbol"] for e in res["after_exit"]], ["TCS"])

    def test_macro_risk_and_expiry_flag(self):
        start, end = date(2026, 12, 1), date(2026, 12, 4)
        evs = build(start, end + date.resolution * 10)
        res = ev.window(evs, start, end, None)
        self.assertEqual(res["event_risk"], "yes")
        self.assertTrue(any("RBI" in t for t in res["risk_events"]))
        start, end = date(2026, 11, 16), date(2026, 11, 19)
        evs = build(start, end + date.resolution * 10)
        self.assertEqual(ev.window(evs, start, end, ["TCS"])["event_risk"], "no")
        self.assertEqual(ev.window(evs, start, end, ["TCS"], include_expiry=True)["event_risk"], "yes")

    def test_text_output(self):
        rc, out, _ = run(["window", "--from", "2026-12-01", "--to", "2026-12-04"])
        self.assertEqual(rc, 0)
        self.assertIn("Event risk: YES", out)
        self.assertIn("pass --event yes", out)


class TestIcs(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "x.ics")
            rc, out, err = run(["ics", "--out", path, "--from", "2026-11-16", "--to", "2026-12-15",
                                "--events", MARKET, "--json"])
            self.assertEqual(rc, 0, err)
            n = strict_json(out)["events"]
            with open(path, "rb") as fh:
                raw = fh.read().decode("utf-8")
        self.assertTrue(raw.startswith("BEGIN:VCALENDAR\r\n"))
        self.assertTrue(raw.rstrip().endswith("END:VCALENDAR"))
        unfolded = raw.replace("\r\n ", "")
        lines = unfolded.split("\r\n")
        self.assertEqual(sum(1 for l in lines if l == "BEGIN:VEVENT"), n)
        self.assertEqual(sum(1 for l in lines if l == "END:VEVENT"), n)
        starts = [l for l in lines if l.startswith("DTSTART")]
        self.assertEqual(len(starts), n)
        for s in starts:
            self.assertRegex(s, r"^DTSTART(:\d{8}T\d{6}Z|;VALUE=DATE:\d{8})$")
        self.assertIn("DTSTART:20261204T043000Z", lines)                  # RBI 10:00 IST
        self.assertTrue(all(len(l.encode()) <= 75 for l in raw.split("\r\n")))
        uids = [l for l in lines if l.startswith("UID:")]
        self.assertEqual(len(uids), len(set(uids)))


class TestStaleness(unittest.TestCase):
    def test_fresh_today(self):
        res = ev.staleness(REF_ROWS, date(2026, 11, 13))
        self.assertEqual(res["per_category_last_date"]["rbi"], date(2027, 2, 5))
        self.assertFalse(any(w.startswith("rbi:") for w in res["warnings"]))
        self.assertTrue(any("unverified" in w and "Budget" in w for w in res["warnings"]))

    def test_faked_today(self):
        res = ev.staleness(REF_ROWS, date(2027, 1, 1))
        self.assertTrue(any(w.startswith("rbi:") for w in res["warnings"]))
        rc, out, _ = run(["staleness", "--reference", STALE_REF, "--today", "2026-12-01", "--json", "--strict"])
        self.assertEqual(rc, 1)
        res = strict_json(out)
        text = "\n".join(res["warnings"])
        self.assertIn("rbi: reference dates end", text)
        self.assertIn("unverified", text)
        self.assertIn("last verified 2026-01-10", text)
        self.assertIn("holiday list", text)
        rc, out, _ = run(["staleness", "--reference", STALE_REF, "--today", "2026-12-01"])
        self.assertEqual(rc, 0)
        self.assertIn("STALE:", out)


class TestLabelsAndErrors(unittest.TestCase):
    def test_unverified_labelled(self):
        rc, out, _ = run(["next", "--category", "budget", "--today", "2026-11-13", "--count", "1"])
        self.assertEqual(rc, 0)
        self.assertIn("UNVERIFIED", out)
        evs = build(date(2027, 2, 1), date(2027, 2, 1))
        b = [e for e in evs if e["category"] == "budget"][0]
        self.assertEqual(b["kind"], "unverified")

    def test_json_valid_everywhere(self):
        for argv in (["week", "--today", "2026-11-13"], ["week", "--from", "2026-11-16", "--next"],
                     ["month", "--month", "2027-03"], ["range", "--from", "2026-11-01", "--to", "2026-11-30"],
                     ["next", "--category", "expiry", "--today", "2026-11-13"],
                     ["window", "--from", "2026-11-16", "--to", "2026-11-27"], ["staleness"]):
            with self.subTest(argv=argv):
                rc, out, err = run(argv + ["--json"])
                self.assertEqual(rc, 0, err)
                strict_json(out)

    def test_week_bounds_and_text(self):
        rc, out, _ = run(["week", "--from", "2026-11-12"])
        self.assertIn("Mon 09-Nov-2026 → Sun 15-Nov-2026", out)
        self.assertIn("moved from Tue 10-Nov-2026", out)
        self.assertIn("Q2 FY27 results season", out)

    def test_errors_exit_2(self):
        cases = [["range", "--from", "03/04/2027", "--to", "2027-04-30"],
                 ["range", "--from", "2026-12-01", "--to", "2026-11-01"],
                 ["week", "--events", AMBIGUOUS],
                 ["month", "--month", "2026-13"],
                 ["next"],
                 ["week", "--events", str(FIXTURES / "nope.csv")],
                 ["staleness", "--reference", USER]]
        for argv in cases:
            with self.subTest(argv=argv):
                rc, out, err = run(argv)
                self.assertEqual(rc, 2)
                self.assertEqual(len(err.strip().splitlines()), 1)
                self.assertTrue(err.startswith("error:"))
                self.assertNotIn("Traceback", err)
        rc, _, err = run(["range", "--from", "03/04/2027", "--to", "2027-04-30"])
        self.assertIn("ambiguous", err)


if __name__ == "__main__":
    unittest.main()
