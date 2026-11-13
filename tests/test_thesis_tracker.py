import contextlib
import datetime as dt
import io
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from _load import FIXTURES, ROOT, load

tt = load("thesis-tracker", "thesis")
dc = load("trading-discipline", "discipline")
rk = load("position-sizing", "risk")
tax = load("indian-tax-for-traders", "tax")

PRICES = str(FIXTURES / "thesis_prices.csv")


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = tt.main(argv)
        except SystemExit as e:  # argparse usage errors
            code = e.code
    return code, out.getvalue(), err.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.store = str(self.dir / "theses.json")

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args, as_of="2026-11-02", ok=True):
        code, out, err = run([*args, "--store", self.store, "--as-of", as_of])
        if ok:
            self.assertEqual(code, 0, err)
        return code, out, err

    def js(self, *args, as_of="2026-11-02"):
        code, out, err = run([*args, "--store", self.store, "--as-of", as_of, "--json"])
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def add_tcs(self, as_of="2026-11-02", **extra):
        args = ["add", "TCS", "--direction", "long", "--horizon", "swing", "--source", "breakout-planner",
                "--setup", "breakout", "--tag", "IT", "--statement", "Deal wins revive IT; base breakout holds",
                "--trigger", "close above 3400", "--trigger-price", "3400", "--kill-price", "3330",
                "--stop", "3340", "--target", "3520", "--risk-pct", "1"]
        for k, v in extra.items():
            args += [f"--{k.replace('_', '-')}", v]
        return self.js(*args, as_of=as_of)["thesis"]["id"]

    def trade(self, tid, entry, exit_, qty="100", entry_day="2026-11-03", exit_day="2026-11-06", reason="manual"):
        self.js("transition", tid, "entry-ready", as_of=entry_day)
        self.js("transition", tid, "active", "--entry-price", entry, "--qty", qty, as_of=entry_day)
        return self.js("transition", tid, "closed", "--exit-price", exit_, "--reason", reason, as_of=exit_day)

    def data(self):
        return json.loads(pathlib.Path(self.store).read_text(encoding="utf-8"))


class TestAddListShow(Base):
    def test_round_trip(self):
        tid = self.add_tcs()
        self.assertEqual(tid, "T0001")
        d = self.data()
        self.assertEqual(d["schema_version"], tt.SCHEMA_VERSION)
        th = d["theses"][0]
        self.assertEqual((th["state"], th["symbol"], th["tags"], th["review_due"]),
                         ("idea", "TCS", ["it"], "2026-11-09"))  # swing: 7-day review
        listed = self.js("list")["theses"]
        self.assertEqual([t["id"] for t in listed], ["T0001"])
        self.assertAlmostEqual(listed[0]["planned_rr"], 120 / 60)
        shown = self.js("show", "1")["thesis"]  # numeric id accepted
        self.assertEqual(shown["statement"], "Deal wins revive IT; base breakout holds")
        self.assertEqual(shown["history"][0]["event"], "created")
        _, out, _ = self.cli("show", "T0001")
        self.assertIn("₹3,400", out)

    def test_list_filters(self):
        self.add_tcs()
        self.js("add", "SBIN", "--direction", "short", "--horizon", "positional", "--segment", "futures",
                "--expiry", "2026-11-24", "--statement", "PSU banks lag as NIM compresses", "--tag", "psu")
        self.assertEqual(len(self.js("list", "--tag", "psu")["theses"]), 1)
        self.assertEqual(len(self.js("list", "--tag", "breakout")["theses"]), 1)  # setup matches too
        self.assertEqual(len(self.js("list", "--symbol", "sbin")["theses"]), 1)
        self.assertEqual(len(self.js("list", "--due", as_of="2026-11-09")["theses"]), 1)  # only TCS (7 d)
        self.js("transition", "T0002", "invalidated", "--note", "NIM held up")
        self.assertEqual(len(self.js("list")["theses"]), 1)
        self.assertEqual(len(self.js("list", "--all")["theses"]), 2)
        self.assertEqual(len(self.js("list", "--state", "invalidated")["theses"]), 1)

    def test_validation(self):
        bad = [["add", "TCS", "--direction", "long", "--horizon", "swing", "--statement", "too short"],
               ["add", "TCS", "--direction", "short", "--horizon", "swing", "--statement", "x" * 20],  # delivery short
               ["add", "NIFTY", "--direction", "long", "--horizon", "swing", "--segment", "options",
                "--statement", "x" * 20],  # no expiry
               ["add", "TCS", "--direction", "long", "--horizon", "swing", "--statement", "x" * 20,
                "--trigger-price", "3400", "--stop", "3450"]]  # stop above trigger for a long
        for argv in bad:
            with self.subTest(argv=argv):
                code, out, err = self.cli(*argv, ok=False)
                self.assertEqual(code, 2)
                self.assertTrue(err.startswith("error: "), err)
                self.assertEqual(len(err.strip().splitlines()), 1)


class TestLifecycle(Base):
    def test_illegal_transitions_refused(self):
        tid = self.add_tcs()
        for state in ("active", "closed"):
            code, _, err = self.cli("transition", tid, state, ok=False)
            self.assertEqual(code, 2)
            self.assertIn("cannot move to", err)
        self.assertEqual(self.data()["theses"][0]["state"], "idea")
        self.assertEqual(len(self.data()["theses"][0]["history"]), 1)  # nothing logged on refusal

    def test_entry_ready_needs_kill_and_stop(self):
        tid = self.js("add", "INFY", "--direction", "long", "--horizon", "swing",
                      "--statement", "Margin recovery after the deal ramp")["thesis"]["id"]
        code, _, err = self.cli("transition", tid, "entry-ready", ok=False)
        self.assertEqual(code, 2)
        self.assertIn("kill criteria", err)
        self.js("update", tid, "--trigger-price", "1500", "--kill", "ASM stage 2 entry", "--stop", "1460")
        self.js("transition", tid, "entry-ready")

    def test_full_lifecycle_history(self):
        tid = self.add_tcs(as_of="2026-10-01")
        self.js("transition", tid, "watching", "--note", "base forming", as_of="2026-10-05")
        self.js("note", tid, "volume drying up", "--reviewed", as_of="2026-10-08")
        self.js("transition", tid, "entry-ready", as_of="2026-10-20")
        res = self.js("transition", tid, "active", "--entry-price", "3405", "--qty", "100", as_of="2026-10-21")
        self.assertEqual(res["thesis"]["position"]["initial_stop"], 3340)
        self.js("update", tid, "--stop", "3410", "--note", "trail to breakeven", as_of="2026-10-28")
        res = self.js("transition", tid, "closed", "--exit-price", "3520", "--reason", "target",
                      "--charges", "120", as_of="2026-11-02")
        r = res["thesis"]["result"]
        self.assertAlmostEqual(r["pnl"], 115 * 100 - 120)
        self.assertAlmostEqual(r["r"], (115 * 100 - 120) / (65 * 100))  # R uses the stop at entry
        th = self.data()["theses"][0]
        self.assertEqual(th["state"], "closed")
        self.assertIsNone(th["review_due"])
        trans = [(h["from"], h["to"], h["date"]) for h in th["history"] if h["event"] == "transition"]
        self.assertEqual(trans, [("idea", "watching", "2026-10-05"), ("watching", "entry-ready", "2026-10-20"),
                                 ("entry-ready", "active", "2026-10-21"), ("active", "closed", "2026-11-02")])
        events = [h["event"] for h in th["history"]]
        self.assertEqual(events.count("review"), 1)
        self.assertEqual(events.count("update"), 1)
        upd = next(h for h in th["history"] if h["event"] == "update")
        self.assertEqual(upd["changes"]["stop"], {"from": 3340, "to": 3410})
        self.assertNotIn("stop_widened", upd)  # tightened, not widened
        # Closed is final; the plan is frozen
        code, _, err = self.cli("transition", tid, "active", "--entry-price", "1", "--qty", "1", ok=False)
        self.assertEqual(code, 2)
        code, _, err = self.cli("update", tid, "--stop", "3300", ok=False)
        self.assertIn("frozen", err)
        self.js("note", tid, "later: deal wins confirmed")  # notes still allowed

    def test_expired_can_be_revived(self):
        tid = self.add_tcs()
        self.js("transition", tid, "expired", "--note", "no trigger")
        self.js("transition", tid, "watching")
        self.assertEqual(self.data()["theses"][0]["state"], "watching")

    def test_widened_stop_flagged(self):
        tid = self.add_tcs()
        self.js("transition", tid, "entry-ready")
        self.js("transition", tid, "active", "--entry-price", "3402", "--qty", "10")
        res = self.js("update", tid, "--stop", "3300")
        self.assertTrue(res["warnings"])
        self.assertTrue(self.data()["theses"][0]["history"][-1]["stop_widened"])


class TestStorage(Base):
    def test_atomic_write_and_backup(self):
        self.add_tcs()
        self.assertFalse(os.path.exists(self.store + ".bak"))  # first write: nothing to back up
        self.js("note", "T0001", "first look")
        bak = json.loads(pathlib.Path(self.store + ".bak").read_text(encoding="utf-8"))
        self.assertEqual(len(bak["theses"][0]["history"]), 1)
        self.assertEqual(len(self.data()["theses"][0]["history"]), 2)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["theses.json", "theses.json.bak"])

    def test_crash_mid_write_keeps_old_file(self):
        self.add_tcs()
        before = pathlib.Path(self.store).read_bytes()
        real_replace = os.replace

        def failing(src, dst):
            if str(dst) == str(pathlib.Path(self.store).resolve()):
                raise OSError("simulated crash")
            return real_replace(src, dst)

        with mock.patch.object(tt.os, "replace", side_effect=failing):
            code, _, err = self.cli("note", "T0001", "lost?", ok=False)
        self.assertEqual(code, 2)
        self.assertIn("simulated crash", err)
        self.assertEqual(pathlib.Path(self.store).read_bytes(), before)
        self.assertFalse([p for p in self.dir.iterdir() if p.name.endswith(".tmp")])

    def test_newer_schema_refused(self):
        pathlib.Path(self.store).write_text(json.dumps(
            {"schema_version": tt.SCHEMA_VERSION + 1, "tool": "thesis-tracker", "theses": []}), encoding="utf-8")
        before = pathlib.Path(self.store).read_text(encoding="utf-8")
        code, _, err = self.cli("list", ok=False)
        self.assertEqual(code, 2)
        self.assertIn("newer", err)
        code, _, _ = self.cli("add", "TCS", "--direction", "long", "--horizon", "swing",
                              "--statement", "x" * 20, ok=False)
        self.assertEqual(code, 2)
        self.assertEqual(pathlib.Path(self.store).read_text(encoding="utf-8"), before)

    def test_foreign_or_corrupt_file_refused(self):
        pathlib.Path(self.store).write_text('{"hello": 1}', encoding="utf-8")
        self.assertEqual(self.cli("list", ok=False)[0], 2)
        pathlib.Path(self.store).write_text('{"schema_version": 1, "tool": "thesis-tr', encoding="utf-8")
        code, _, err = self.cli("list", ok=False)
        self.assertEqual(code, 2)
        self.assertIn("not valid JSON", err)

    def test_store_never_in_skill_folder(self):
        inside = ROOT / "skills" / "thesis-tracker" / "theses.json"
        code, _, err = run(["add", "TCS", "--direction", "long", "--horizon", "swing",
                            "--statement", "x" * 20, "--store", str(inside)])
        self.assertEqual(code, 2)
        self.assertIn("inside the skill folder", err)
        self.assertFalse(inside.exists())
        old = os.getcwd()
        try:  # default store: ./theses.json in the current folder
            os.chdir(self.dir)
            code, _, err = run(["add", "TCS", "--direction", "long", "--horizon", "swing", "--statement", "x" * 20])
            self.assertEqual(code, 0, err)
        finally:
            os.chdir(old)
        self.assertTrue((self.dir / "theses.json").exists())
        self.assertFalse(list((ROOT / "skills" / "thesis-tracker").rglob("*.json")))

    def test_missing_store_is_clean_error(self):
        code, out, err = self.cli("list", ok=False)
        self.assertEqual(code, 2)
        self.assertIn("no thesis store", err)
        self.assertFalse(os.path.exists(self.store))


class TestDue(Base):
    def setUp(self):
        super().setUp()
        self.add_tcs(as_of="2026-10-20")                                     # T0001 idea, review 10-27
        self.js("add", "SBIN", "--direction", "short", "--horizon", "swing", "--segment", "futures",
                "--expiry", "2026-11-24", "--statement", "PSU banks lag as NIM compresses",
                "--trigger-price", "800", "--kill-price", "815", "--stop", "812", "--target", "770",
                as_of="2026-10-20")                                         # T0002
        self.js("add", "INFY", "--direction", "long", "--horizon", "investment",
                "--statement", "Deal ramp lifts margins over FY27", "--trigger-price", "1450",
                "--kill-price", "1300", "--stop", "1320", "--target", "1900", as_of="2025-11-25")  # T0003
        self.js("transition", "T0003", "entry-ready", as_of="2025-11-30")
        self.js("transition", "T0003", "active", "--entry-price", "1455", "--qty", "50", as_of="2025-12-01")
        self.js("note", "T0003", "entered", "--next-review", "2026-12-31", as_of="2025-12-01")
        self.js("add", "HDFCBANK", "--direction", "long", "--horizon", "positional",
                "--statement", "Deposit growth beats loan growth again", "--review-due", "2026-12-31",
                "--catalyst", "2026-11-18:Q2 results", "--catalyst", "2027-01-20:Q3 results",
                as_of="2026-11-01")                                         # T0004

    def test_reviews_kills_catalysts(self):
        res = self.js("due", "--prices", PRICES, "--days", "7", as_of="2026-11-15")
        self.assertEqual([r["id"] for r in res["reviews_due"]], ["T0001", "T0002"])
        self.assertEqual(res["reviews_due"][0]["days_overdue"], 19)
        kills = {k["id"]: k for k in res["kill_hits"]}
        self.assertEqual(list(kills), ["T0001"])                          # TCS 3320.5 <= kill 3330
        self.assertEqual(kills["T0001"]["level"], "kill")
        self.assertIn("invalidated", kills["T0001"]["action"])            # not entered yet
        cats = [(c["id"], c["date"], c["kind"]) for c in res["catalysts"]]
        self.assertEqual(cats, [("T0004", "2026-11-18", "catalyst")])     # expiry 11-24 is 9 days out
        res = self.js("due", "--prices", PRICES, "--days", "10", as_of="2026-11-15")
        self.assertIn(("T0002", "2026-11-24", "expiry"), [(c["id"], c["date"], c["kind"]) for c in res["catalysts"]])

    def test_short_kill_direction(self):
        p = self.dir / "p.csv"
        p.write_text("symbol,ltp\nSBIN,816\nTCS,3500\n", encoding="utf-8")
        res = self.js("due", "--prices", str(p), as_of="2026-11-15")
        self.assertEqual([k["id"] for k in res["kill_hits"]], ["T0002"])
        self.assertTrue(any("INFY" in n for n in res["notes"]))  # missing price is reported

    def test_ltcg_countdown_matches_tax_py(self):
        as_of = dt.date(2026, 11, 15)
        res = self.js("due", "--prices", PRICES, as_of=as_of.isoformat())
        lt = res["turning_long_term"]
        self.assertEqual([x["id"] for x in lt], ["T0003"])
        h = tax.harvest([{"symbol": "INFY", "buy_date": dt.date(2025, 12, 1), "buy_price": 1455, "qty": 50,
                          "ltp": 1580}], as_of)
        self.assertEqual(lt[0]["days_to_long_term"], h["lots"][0]["days_to_long_term"])
        self.assertEqual(lt[0]["days_to_long_term"], 17)
        first = dt.date.fromisoformat(lt[0]["long_term_from"])
        self.assertTrue(tax.is_long_term(dt.date(2025, 12, 1), first))
        self.assertFalse(tax.is_long_term(dt.date(2025, 12, 1), first - dt.timedelta(days=1)))
        self.assertIn("wait 17 days", lt[0]["hint"])
        self.assertIn("STCG 20% into LTCG 12.5%", lt[0]["hint"])
        self.assertAlmostEqual(lt[0]["tax_saved_estimate"], (1580 - 1455) * 50 * 0.075)
        # Outside the window, and once long-term, it drops off
        self.assertEqual(self.js("due", "--ltcg-days", "10", as_of="2026-11-15")["turning_long_term"], [])
        self.assertEqual(self.js("due", as_of=first.isoformat())["turning_long_term"], [])

    def test_stale(self):
        self.assertEqual(self.js("stale", "--days", "21", as_of="2026-11-05")["stale"], [])
        res = self.js("stale", "--days", "21", as_of="2026-11-15")
        self.assertEqual(sorted(s["id"] for s in res["stale"]), ["T0001", "T0002"])  # active T0003 never stale
        self.js("note", "T0001", "still basing", as_of="2026-11-14")
        res = self.js("stale", "--days", "21", as_of="2026-11-15")
        self.assertEqual([s["id"] for s in res["stale"]], ["T0002"])  # a note resets the clock

    def test_text_output(self):
        _, out, _ = self.cli("due", "--prices", PRICES, as_of="2026-11-15")
        self.assertIn("Kill level hit", out)
        self.assertIn("₹3,320.50", out)
        self.assertIn("Sun 15 Nov 2026 IST", out)


class TestPostmortemStats(Base):
    def closed(self, exit_, verdict, symbol="TCS", reason="manual", **pm):
        tid = self.js("add", symbol, "--direction", "long", "--horizon", "swing", "--source", "results-season",
                      "--setup", "drift", "--statement", "Post-results drift continues",
                      "--trigger-price", "100", "--kill-price", "94", "--stop", "95", "--target", "115")["thesis"]["id"]
        self.trade(tid, "100", exit_, reason=reason)
        args = ["postmortem", tid, "--thesis", verdict]
        for k, v in pm.items():
            args += [f"--{k.replace('_', '-')}", v]
        return self.js(*args)["postmortem"]

    def test_quadrants(self):
        self.assertEqual(self.closed("110", "right")["quadrant"], "right-good")
        self.assertEqual(self.closed("95", "right")["quadrant"], "right-bad")
        self.assertEqual(self.closed("110", "wrong")["quadrant"], "wrong-good")
        self.assertEqual(self.closed("95", "wrong")["quadrant"], "wrong-bad")
        self.assertEqual(self.closed("100", "right")["quadrant"], "flat")
        tid = self.add_tcs()
        self.js("transition", tid, "invalidated", "--note", "results missed")
        pm = self.js("postmortem", tid, "--thesis", "wrong")["postmortem"]
        self.assertEqual(pm["quadrant"], "no-trade")

    def test_plan_checks(self):
        pm = self.closed("115", "right", reason="target", mistake="none")
        self.assertEqual((pm["plan"]["entry_at_trigger"], pm["plan"]["stop_honoured"], pm["plan"]["exited_early"]),
                         (True, True, False))
        self.assertTrue(pm["plan"]["followed"])
        self.assertEqual(pm["mistakes"], [])
        pm = self.closed("110", "right")  # manual exit in profit, short of the 115 target
        self.assertTrue(pm["plan"]["exited_early"])
        self.assertFalse(pm["plan"]["followed"])

    def test_plan_checks_stop_blown(self):
        pm = self.closed("90", "right", mistake="Ignored Stop")
        self.assertFalse(pm["plan"]["stop_honoured"])
        self.assertFalse(pm["plan"]["followed"])
        self.assertEqual(pm["mistakes"], ["ignored-stop"])
        pm = self.closed("90", "right", stop_honoured="yes")  # trader's answer overrides the record
        self.assertTrue(pm["plan"]["stop_honoured"])
        self.assertEqual(pm["plan"]["answered_by"]["stop_honoured"], "user")

    def test_postmortem_guards(self):
        tid = self.add_tcs()
        self.assertEqual(self.cli("postmortem", tid, "--thesis", "right", ok=False)[0], 2)  # still open
        self.closed("110", "right")
        code, _, err = self.cli("postmortem", "T0002", "--thesis", "wrong", ok=False)
        self.assertIn("--revise", err)
        self.js("postmortem", "T0002", "--thesis", "wrong", "--revise")
        hist = [h for h in self.data()["theses"][1]["history"] if h["event"] == "postmortem"]
        self.assertEqual(len(hist), 2)
        self.assertEqual(hist[1]["replaced"]["thesis"], "right")  # the old review is kept
        self.assertEqual(self.js("postmortem", "T0002")["action"], "postmortem-saved")  # read-only view

    def test_stats_with_breakeven(self):
        self.closed("110", "right", mistake="none")
        self.closed("95", "wrong", mistake="chased entry", entry_at_trigger="no")
        self.closed("100", "right")
        s = self.js("stats")
        sm = s["summary"]
        self.assertEqual((sm["trades"], sm["wins"], sm["losses"], sm["breakevens"]), (3, 1, 1, 1))
        self.assertAlmostEqual(sm["win_rate_pct"], 100 / 3)
        self.assertAlmostEqual(sm["net_pnl"], 1000 - 500 + 0)
        self.assertAlmostEqual(sm["expectancy"], 500 / 3)
        self.assertAlmostEqual(sm["avg_R"], (2 - 1 + 0) / 3)
        self.assertEqual(s["quadrants"], {"right-good": 1, "right-bad": 0, "wrong-good": 0, "wrong-bad": 1, "flat": 1})
        self.assertEqual(s["by_source"]["results-season"]["trades"], 3)
        self.assertEqual(s["by_direction"]["long"]["breakevens"], 1)
        self.assertEqual(list(s["mistakes"]), ["chased-entry"])
        pa = s["plan_adherence"]
        self.assertEqual((pa["judged"], pa["broken"]), (3, 2))  # early exit + off-trigger entry
        _, out, _ = self.cli("stats")
        self.assertIn("W1/L1/BE1", out)


class TestExportHypothesis(Base):
    def test_export_reads_in_discipline_and_risk(self):
        a = self.add_tcs()
        self.trade(a, "3405", "3520", reason="target")
        self.js("postmortem", a, "--thesis", "right", "--mistake", "moved stop")
        b = self.add_tcs()
        self.trade(b, "3401", "3401")                                   # breakeven
        c = self.add_tcs()
        self.js("transition", c, "entry-ready")
        self.js("transition", c, "active", "--entry-price", "3402", "--qty", "20")  # open
        out = str(self.dir / "journal.csv")
        res = self.js("export", "--journal", out)
        self.assertEqual((res["rows"], res["closed"], res["open"]), (3, 2, 1))
        trades = dc.load_journal(out)
        by = {t["notes"].split(" | ")[0]: t for t in trades}
        self.assertEqual(by[a]["pnl"], 115 * 100)
        self.assertEqual(by[a]["risk"], 65 * 100)
        self.assertEqual(by[a]["mistakes"], ["moved-stop"])
        self.assertEqual(by[a]["setup"], "breakout")
        self.assertEqual(by[a]["segment"], "equity")
        self.assertEqual(by[a]["holding_days"], 3)
        self.assertEqual(by[b]["outcome"], "breakeven")
        self.assertEqual(by[c]["status"], "open")
        self.assertEqual(dc.review(trades)["summary"]["breakevens"], 1)
        # Existing file is not overwritten silently; --closed-only feeds risk.py journal
        self.assertEqual(self.cli("export", "--journal", out, ok=False)[0], 2)
        self.js("export", "--journal", out, "--closed-only", "--overwrite")
        self.assertEqual(rk.journal(out)["trades"], 2)
        self.assertEqual(rk.journal(out)["net_pnl"], 11500)

    def test_export_refuses_store_path(self):
        self.add_tcs()
        self.assertEqual(self.cli("export", "--journal", self.store, ok=False)[0], 2)

    def test_hypothesis_card(self):
        res = self.js("hypothesis", "--symbol", "dixon", "--expected-win-rate", "45", "--margin", "10")
        self.assertEqual(res["symbol"], "DIXON")
        for key in ("hypothesis", "evidence_for", "evidence_against", "experiment", "kill_criteria", "sample_size"):
            self.assertIn(key, res["card"])
        self.assertEqual(res["sample_size"]["trades_needed"], 96)
        self.assertFalse(os.path.exists(self.store))  # writes nothing
        _, out, _ = self.cli("hypothesis")
        self.assertIn("KILL CRITERIA", out)


class TestCliContract(Base):
    def test_every_command_json(self):
        tid = self.add_tcs()
        self.trade(tid, "3405", "3380")
        self.js("postmortem", tid, "--thesis", "wrong")
        out = str(self.dir / "j.csv")
        for argv in (["list", "--all"], ["show", tid], ["due", "--prices", PRICES], ["stale"], ["stats"],
                     ["postmortem", tid], ["export", "--journal", out], ["hypothesis"],
                     ["note", tid, "json check"]):
            with self.subTest(argv=argv):
                self.assertIsInstance(self.js(*argv), dict)

    def test_bad_input_exits_2(self):
        self.add_tcs()
        cases = [["show", "T0099"], ["transition", "T0001", "nonsense"], ["add", "TCS", "--direction", "up"],
                 ["add", "TCS", "--direction", "long", "--horizon", "swing", "--statement", "x" * 20,
                  "--stop", "-5"],
                 ["note", "T0001", "x", "--date", "31/31/2026"], ["due", "--prices", str(self.dir / "nope.csv")],
                 ["add", "TCS", "--direction", "long", "--horizon", "swing", "--statement", "x" * 20,
                  "--catalyst", "results soon"],
                 ["transition", "T0001", "invalidated"],                      # needs --note
                 ["update", "T0001"]]                                         # nothing to change
        for argv in cases:
            with self.subTest(argv=argv):
                code, out, err = self.cli(*argv, ok=False)
                self.assertEqual(code, 2)
                self.assertTrue(err.startswith("error: "), err)
                self.assertNotIn("Traceback", err)
                self.assertEqual(len(err.strip().splitlines()), 1, err)

    def test_bad_prices_file(self):
        self.add_tcs()
        p = self.dir / "p.csv"
        p.write_text("name,value\nTCS,1\n", encoding="utf-8")
        self.assertEqual(self.cli("due", "--prices", str(p), ok=False)[0], 2)
        p.write_text("symbol,ltp\nTCS,abc\n", encoding="utf-8")
        self.assertEqual(self.cli("due", "--prices", str(p), ok=False)[0], 2)


if __name__ == "__main__":
    unittest.main()
