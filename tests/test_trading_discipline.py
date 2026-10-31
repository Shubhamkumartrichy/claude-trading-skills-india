import contextlib
import csv
import datetime as dt
import io
import json
import os
import tempfile
import unittest

from _load import FIXTURES, load

dc = load("trading-discipline", "discipline")

CAP = 500000  # ₹5 lakh: daily limit ₹10,000, weekly ₹25,000, monthly ₹50,000, base risk ₹5,000
D = dt.date


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = dc.main(argv)
        except SystemExit as e:  # argparse usage errors
            code = e.code
    return code, out.getvalue(), err.getvalue()


def journal(name):
    return dc.load_journal(str(FIXTURES / name))


class TestJournal(unittest.TestCase):
    def test_breakeven_and_open(self):
        t = journal("discipline_journal.csv")
        self.assertEqual(len(t), 9)
        flat = next(x for x in t if x["symbol"] == "HDFCBANK")
        self.assertEqual(flat["outcome"], "breakeven")
        lt = next(x for x in t if x["symbol"] == "LT")
        self.assertEqual(lt["status"], "open")
        self.assertIsNone(lt["pnl"])
        self.assertEqual(lt["risk"], 1200)

    def test_pnl_column_and_charges(self):
        t = {x["symbol"]: x for x in journal("discipline_journal.csv")}
        self.assertEqual(t["TCS"]["pnl"], 80 * 50 - 150)
        self.assertEqual(t["NIFTY OPT"]["pnl"], -2600)          # pnl column wins
        self.assertEqual(t["SBIN"]["pnl"], 3000)                 # short side
        self.assertEqual(t["TCS"]["holding_days"], 2)

    def test_risk_py_format_still_reads(self):
        t = journal("journal.csv")
        self.assertEqual(len(dc.closed_trades(t)), 5)
        self.assertEqual(dc.stats(t)["net_pnl"], 970 - 530 + 570 - 330 + 770)


class TestBreaker(unittest.TestCase):
    def test_daily_limit_trips(self):
        r = dc.breaker(journal("discipline_daily.csv"), CAP, D(2026, 10, 28))
        self.assertEqual(r["status"], "RED")
        self.assertEqual(r["allowed_risk"], 0)
        day = r["periods"][0]
        self.assertAlmostEqual(day["realised_pnl"], -12200)
        self.assertTrue(day["tripped"])
        self.assertEqual(day["resumes"], D(2026, 10, 29))

    def test_green_next_day_and_as_of(self):
        t = journal("discipline_daily.csv")
        nxt = dc.breaker(t, CAP, D(2026, 10, 29))
        self.assertEqual(nxt["status"], "GREEN")
        self.assertEqual(nxt["allowed_risk"], 5000)
        self.assertAlmostEqual(nxt["periods"][1]["realised_pnl"], 3900 - 12200)   # week
        before = dc.breaker(t, CAP, D(2026, 10, 27))
        self.assertEqual(before["status"], "GREEN")
        self.assertEqual(before["ignored_after_as_of"], 1)
        self.assertEqual(before["closed_trades_counted"], 1)

    def test_weekly_limit_configurable(self):
        r = dc.breaker(journal("discipline_daily.csv"), CAP, D(2026, 10, 29), weekly_pct=1.5)
        self.assertEqual(r["status"], "RED")
        self.assertEqual(r["periods"][1]["resumes"], D(2026, 11, 2))

    def test_streak_stops_the_day(self):
        t = journal("discipline_streak.csv")
        today = dc.breaker(t, CAP, D(2026, 10, 29))
        self.assertEqual(today["losing_streak"], 3)     # the breakeven neither breaks nor extends it
        self.assertEqual(today["status"], "RED")
        self.assertAlmostEqual(today["periods"][0]["realised_pnl"], -3000)   # daily limit not hit
        self.assertFalse(today["periods"][0]["tripped"])
        tomorrow = dc.breaker(t, CAP, D(2026, 10, 30))
        self.assertEqual(tomorrow["status"], "AMBER")
        self.assertEqual(tomorrow["allowed_risk"], 5000)

    def test_five_streak_halves_size_for_n_trades(self):
        t = journal("discipline_streak5.csv")
        self.assertEqual(dc.breaker(t, CAP, D(2026, 10, 23))["status"], "RED")
        r = dc.breaker(t, CAP, D(2026, 10, 27))
        self.assertEqual(r["status"], "AMBER")
        self.assertEqual(r["reduced_trades_left"], 3)
        self.assertEqual(r["allowed_risk"], 2500)
        done = dc.breaker(t, CAP, D(2026, 10, 27), reduce_trades=2)
        self.assertEqual(done["status"], "GREEN")

    def test_cli_breaker_json(self):
        code, out, _ = run(["breaker", str(FIXTURES / "discipline_daily.csv"), "--capital", str(CAP),
                            "--as-of", "2026-10-28", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["status"], "RED")


class TestGate(unittest.TestCase):
    CLEAN = dict(entry=1000, stop=980, target=1060, qty=200, capital=CAP, plan_written=True,
                 minutes_since_last_loss=120, regime="risk-on", fno_ban=False, surveillance=False,
                 event_in_window=False, circuit_locked=False, now=dt.time(10, 30), as_of=D(2026, 10, 29))

    def gate(self, **over):
        kw = dict(self.CLEAN)
        kw.update(over)
        return dc.gate(**kw)

    def status(self, res, check):
        return next(i["status"] for i in res["items"] if i["check"] == check)

    def test_clean_trade_passes(self):
        r = self.gate()
        self.assertEqual(r["verdict"], "PASS")
        self.assertEqual(r["risk"], 4000)
        self.assertAlmostEqual(r["risk_pct"], 0.8)

    def test_missing_stop_blocks(self):
        r = self.gate(stop=None)
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertEqual(self.status(r, "stop-loss"), "BLOCK")

    def test_wrong_side_stop_blocks(self):
        self.assertEqual(self.status(self.gate(stop=1010, side="long"), "stop-loss"), "BLOCK")

    def test_oversize_blocks(self):
        r = self.gate(stop=950)                         # ₹10,000 = 2% > 1%
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertIn("risk-size", r["blocks"])

    def test_revenge_window_blocks(self):
        r = self.gate(minutes_since_last_loss=10)
        self.assertIn("revenge-window", r["blocks"])
        self.assertEqual(self.gate(minutes_since_last_loss=30)["verdict"], "PASS")

    def test_fno_ban_blocks_fno_only(self):
        self.assertIn("fno-ban", self.gate(segment="futures", fno_ban=True)["blocks"])
        cash = self.gate(fno_ban=True)
        self.assertEqual(cash["verdict"], "PASS-WITH-WARNINGS")

    def test_warnings(self):
        self.assertEqual(self.gate(plan_written=None)["verdict"], "PASS-WITH-WARNINGS")
        self.assertEqual(self.gate(regime="risk-off")["verdict"], "PASS-WITH-WARNINGS")
        self.assertIn("written-plan", self.gate(plan_written=False)["blocks"])
        self.assertIn("target", self.gate(target=1010)["blocks"])          # 0.5R

    def test_market_regime_labels(self):
        # Labels emitted by market-regime's regime.py
        self.assertEqual(self.gate(regime="Confirmed uptrend")["verdict"], "PASS")
        for lab in ("Uptrend under pressure", "Rally attempt", "Correction"):
            with self.subTest(regime=lab):
                self.assertEqual(self.gate(regime=lab)["verdict"], "PASS-WITH-WARNINGS")

    def test_india_checks(self):
        self.assertIn("intraday-cutoff", self.gate(segment="intraday", now=dt.time(15, 1))["blocks"])
        self.assertEqual(self.gate(segment="intraday", now=dt.time(14, 59))["verdict"], "PASS")
        self.assertIn("circuit", self.gate(circuit_locked=True)["blocks"])
        self.assertIn("asm-gsm-t2t", self.gate(segment="intraday", surveillance=True)["blocks"])
        tue = self.gate(segment="options-buy", entry=100, stop=60, target=180, qty=75, fno_ban=False,
                        as_of=D(2026, 11, 3))
        self.assertIn("expiry-day", tue["warnings"])

    def test_breaker_integration(self):
        t = journal("discipline_streak.csv")
        br = dc.breaker(t, CAP, D(2026, 10, 29))
        r = self.gate(breaker_result=br, journal_trades=t)
        self.assertIn("circuit-breaker", r["blocks"])
        half = dc.breaker(journal("discipline_streak5.csv"), CAP, D(2026, 10, 27))
        r2 = self.gate(breaker_result=half)                     # risk ₹4,000 > ₹2,500 allowed
        self.assertIn("circuit-breaker", r2["blocks"])

    def test_cli_gate_json(self):
        code, out, _ = run(["gate", "--capital", str(CAP), "--entry", "1000", "--stop", "980", "--target",
                            "1060", "--qty", "200", "--plan-written", "yes", "--as-of", "2026-10-29",
                            "--journal", str(FIXTURES / "discipline_streak.csv"), "--json"])
        self.assertEqual(code, 0)
        res = json.loads(out)
        self.assertEqual(res["verdict"], "BLOCK")
        self.assertIn("circuit-breaker", res["blocks"])


class TestReview(unittest.TestCase):
    def setUp(self):
        self.rv = dc.review(journal("discipline_journal.csv"), recent=4)

    def test_summary_by_hand(self):
        s = self.rv["summary"]
        self.assertEqual((s["trades"], s["wins"], s["losses"], s["breakevens"]), (8, 3, 4, 1))
        self.assertAlmostEqual(s["win_rate_pct"], 37.5)
        self.assertAlmostEqual(s["net_pnl"], 1250)
        self.assertAlmostEqual(s["avg_loss"], 11600 / 4)        # breakeven not averaged in as a loss
        self.assertAlmostEqual(s["avg_win"], 12850 / 3)
        self.assertAlmostEqual(s["profit_factor"], 12850 / 11600)
        self.assertAlmostEqual(s["expectancy"], 1250 / 8)
        self.assertAlmostEqual(s["avg_R"], (1.925 - 1 - 1 + 2 + 0 - 2 + 3 - 1) / 8)
        self.assertAlmostEqual(s["max_drawdown"], -6000)
        self.assertEqual(s["longest_losing_streak"], 2)

    def test_breakdowns(self):
        rv = self.rv
        self.assertAlmostEqual(rv["by_setup"]["breakout"]["net_pnl"], 12850)
        self.assertAlmostEqual(rv["by_setup"]["pullback"]["net_pnl"], -6000)
        self.assertAlmostEqual(rv["by_weekday"]["Tue"]["net_pnl"], -5600)
        self.assertAlmostEqual(rv["by_hour"]["09:00"]["net_pnl"], 7250)
        self.assertAlmostEqual(rv["fno_vs_cash"]["fno"]["net_pnl"], -5600)
        self.assertAlmostEqual(rv["fno_vs_cash"]["cash"]["net_pnl"], 6850)
        self.assertEqual(next(iter(rv["mistakes"])), "revenge")
        self.assertAlmostEqual(rv["mistakes"]["revenge"]["loss_cost"], 5600)
        self.assertAlmostEqual(rv["clean_vs_mistake"]["clean"]["net_pnl"], 9850)
        self.assertAlmostEqual(rv["holding_days"]["winners"], 2.0)
        self.assertAlmostEqual(rv["holding_days"]["losers"], 2.25)
        self.assertAlmostEqual(rv["trend"]["recent"]["net_pnl"], 0)
        self.assertAlmostEqual(rv["trend"]["prior"]["net_pnl"], 1250)
        self.assertEqual([o["symbol"] for o in rv["open_trades"]], ["LT"])

    def test_week_digest(self):
        t = journal("discipline_journal.csv")
        latest = dc.week_digest(t)
        self.assertEqual(latest["week"], "2026-W44")
        self.assertAlmostEqual(latest["summary"]["net_pnl"], -3000)
        w43 = dc.week_digest(t, "2026-W43")
        self.assertEqual((w43["summary"]["trades"], w43["summary"]["breakevens"]), (2, 1))
        self.assertAlmostEqual(w43["summary"]["win_rate_pct"], 50)
        self.assertAlmostEqual(w43["previous_week_net"], -3000)

    def test_cli_review_json(self):
        code, out, _ = run(["review", str(FIXTURES / "discipline_journal.csv"), "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["summary"]["breakevens"], 1)
        code, out, _ = run(["review", str(FIXTURES / "discipline_journal.csv"), "--week", "--json"])
        self.assertEqual(json.loads(out)["week"], "2026-W44")


class TestLogAndErrors(unittest.TestCase):
    ARGS = ["--capital", str(CAP), "--entry", "1000", "--stop", "980", "--qty", "200", "--as-of", "2026-10-29"]

    def test_log_appends_exactly_one_row(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "decisions.csv")
            self.assertEqual(run(["log", "--log", path, "--symbol", "INFY", *self.ARGS])[0], 0)
            self.assertEqual(run(["log", "--log", path, "--symbol", "TCS", "--action", "skipped",
                                  *self.ARGS])[0], 0)
            with open(path, newline="", encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[1]["symbol"], "TCS")
            self.assertEqual(rows[1]["action"], "skipped")
            self.assertEqual(rows[0]["risk_inr"], "4000")

    def test_gate_never_writes(self):
        with tempfile.TemporaryDirectory() as d:
            cwd = os.getcwd()
            os.chdir(d)
            try:
                run(["gate", *self.ARGS])
                self.assertEqual(os.listdir(d), [])
            finally:
                os.chdir(cwd)

    def test_log_refuses_foreign_csv(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "other.csv")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("a,b\n1,2\n")
            code, _, err = run(["log", "--log", path, "--symbol", "X", *self.ARGS])
            self.assertEqual(code, 2)
            self.assertIn("error:", err)
            with open(path, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "a,b\n1,2\n")

    def test_bad_input_exits_cleanly(self):
        for argv in (["review", str(FIXTURES / "discipline_bad.csv")],
                     ["review", "no-such-file.csv"],
                     ["breaker", str(FIXTURES / "discipline_daily.csv"), "--capital", "0", "--as-of", "2026-10-28"],
                     ["breaker", str(FIXTURES / "discipline_daily.csv"), "--capital", "1", "--as-of", "28/13/2026"],
                     ["gate", "--entry", "100", "--plan-written", "maybe"],
                     ["gate", "--entry", "-5"]):
            with self.subTest(argv=argv):
                code, out, err = run(argv)
                self.assertEqual(code, 2)
                self.assertNotIn("Traceback", err)

    def test_inr_grouping(self):
        self.assertEqual(dc.inr(1234567), "₹12,34,567")
        self.assertEqual(dc.inr(-12200, True), "-₹12,200")
        self.assertEqual(dc.inr(500, True), "+₹500")


if __name__ == "__main__":
    unittest.main()
