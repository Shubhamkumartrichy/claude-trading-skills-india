import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from _load import FIXTURES, load

ex = load("exhaustion-reversal", "exhaustion")

UNI = str(FIXTURES / "exhaustion_universe")
PARA = str(FIXTURES / "exhaustion_universe" / "PARABOLIC.csv")
UCS = str(FIXTURES / "exhaustion_universe" / "UCSTREAK.csv")
FNO = str(FIXTURES / "exhaustion_fno.csv")
BAN = str(FIXTURES / "exhaustion_ban.csv")
BANDS = str(FIXTURES / "exhaustion_bands.csv")
NEWS_FAIL = str(FIXTURES / "exhaustion_news_fail.csv")
NEWS_OK = str(FIXTURES / "exhaustion_news_ok.csv")


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = ex.main(argv)
    return rc, out.getvalue(), err.getvalue()


def _no_nan(token):
    raise ValueError(f"non-standard JSON constant {token}")


def js(argv):
    rc, out, err = run(argv + ["--json"])
    if rc != 0:
        raise AssertionError(f"rc={rc} err={err}")
    return json.loads(out, parse_constant=_no_nan)


def by_symbol(rows):
    return {r["symbol"]: r for r in rows}


class TestClimax(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.res = by_symbol(js(["climax", "--csv-dir", UNI, "--bands", BANDS])["results"])

    def test_parabolic_scores_high_steady_low(self):
        p, s = self.res["PARABOLIC"], self.res["STEADY"]
        self.assertGreaterEqual(p["score"], ex.CLIMAX)
        self.assertLess(s["score"], ex.EXTENDED)
        self.assertEqual(s["stage"], "not extended")
        self.assertEqual(p["peak"]["date"], "2026-11-09")
        self.assertEqual({f["key"] for f in p["factors"]}, set(ex.WEIGHTS))
        self.assertEqual(sum(ex.WEIGHTS.values()), 100)
        self.assertGreater(p["metrics"]["ext20_atr"], 6)
        self.assertGreaterEqual(p["metrics"]["late_gaps"], 2)
        self.assertLess(p["metrics"]["peak_close_pos_pct"], 30)       # reversal bar

    def test_reversal_confirmed_on_right_date(self):
        p = self.res["PARABOLIC"]
        self.assertEqual(p["stage"], "reversal confirmed")
        cf = p["confirmation"]
        self.assertEqual(cf["confirmed_on"], "2026-11-11")
        self.assertEqual(cf["close_below_prior_low"]["date"], "2026-11-11")
        self.assertIn("close_below_prior_low", cf["by"])

    def test_not_confirmed_on_the_peak_day(self):
        r = js(["climax", "--csv", PARA, "--as-of", "2026-11-10"])["results"][0]
        self.assertEqual(r["stage"], "climax")
        self.assertIsNone(r["confirmation"]["confirmed_on"])
        self.assertTrue(r["watch"])

    def test_upper_circuit_streak_counted(self):
        u = self.res["UCSTREAK"]
        self.assertEqual(u["circuit"]["streak"], 6)
        self.assertEqual(u["circuit"]["kind"], "locked")
        self.assertIn("UC", u["flags"])
        self.assertIn("band-cut", u["flags"])
        self.assertEqual(u["tightening"]["from_pct"], 10.0)
        self.assertEqual(u["tightening"]["to_pct"], 5.0)
        climax = next(f for f in u["factors"] if f["key"] == "climax")
        self.assertEqual(climax["points"], 30)
        self.assertGreaterEqual(u["score"], ex.CLIMAX)

    def test_inferred_streak_without_bands(self):
        r = js(["climax", "--csv", UCS])["results"][0]
        self.assertEqual(r["circuit"]["streak"], 6)
        self.assertIn("UC?", r["flags"])

    def test_text_output(self):
        rc, out, _ = run(["climax", "--csv", PARA, "--as-of", "2026-11-10"])
        self.assertEqual(rc, 0)
        self.assertIn("CLIMAX", out)
        self.assertIn("previous session's low", out)


class TestPlan(unittest.TestCase):
    def test_holder_plan(self):
        r = js(["plan", "--csv", PARA, "--as-of", "2026-11-10", "--qty", "300", "--avg-price", "600"])
        h = r["hold"]
        self.assertEqual(h["stage"], "climax")
        self.assertEqual(h["sell_pct"], 40)
        self.assertEqual((h["sell_now"], h["trail_qty"]), (120, 180))
        self.assertEqual(h["trail"], "prior-low")
        self.assertLess(h["stop_trigger"], 983.10)                      # one tick under the prior low
        self.assertAlmostEqual(h["stop_trigger"], 983.05, places=2)
        self.assertLess(h["stop_limit"], h["stop_trigger"])
        self.assertTrue(any("GTT" in s for s in h["steps"]))
        self.assertNotIn("short", r)

    def test_holder_at_upper_circuit_sells_into_queue(self):
        r = js(["plan", "--csv", UCS, "--bands", BANDS, "--qty", "1000"])
        self.assertIn("into the circuit", r["hold"]["steps"][0])
        self.assertTrue(any("lower circuit" in w for w in r["hold"]["warnings"]))

    def test_refuses_overnight_short_for_non_fno(self):
        r = js(["plan", "--csv", UCS, "--bands", BANDS, "--side", "short", "--fno-list", FNO])
        s = r["short"]
        self.assertFalse(s["allowed"])
        self.assertFalse(s["fno"])
        self.assertIn("no overnight short", s["reason"])
        self.assertIn("intraday", s["intraday_note"])

    def test_unknown_fno_status_refused(self):
        s = js(["plan", "--csv", PARA, "--side", "short"])["short"]
        self.assertFalse(s["allowed"])
        self.assertIn("--fno", s["reason"])

    def test_fno_short_sized_in_lots_stop_above_high(self):
        r = js(["plan", "--csv-dir", UNI, "--symbol", "PARABOLIC", "--side", "short", "--fno-list", FNO,
                "--capital", "5000000", "--risk-pct", "1", "--margin-per-lot", "60000"])
        s = r["short"]
        self.assertTrue(s["allowed"])
        self.assertEqual(s["status"], "triggered")
        self.assertGreater(s["stop"], s["climax_high"])
        self.assertLess(s["entry"], s["climax_low"])
        sz = s["sizing"]
        self.assertEqual(sz["lot"], 250)                              # read from the F&O list
        per_lot = 250 * (s["stop"] - s["entry"] + s["entry"] * ex.FUT_COST_PCT / 100)
        self.assertEqual(sz["lots"], int(50000 // per_lot))
        self.assertGreaterEqual(sz["lots"], 1)
        self.assertEqual(sz["qty"], sz["lots"] * 250)
        self.assertLessEqual(sz["risk_if_stopped"], 50000)
        self.assertEqual(sz["margin_total"], sz["lots"] * 60000)
        self.assertEqual([t["label"] for t in s["targets"]][:1], ["10 EMA"])
        self.assertTrue(any("no maximum loss" in w for w in s["warnings"]))
        self.assertTrue(any("Squeeze" in w for w in s["warnings"]))

    def test_short_waits_for_trigger_before_break(self):
        s = js(["plan", "--csv", PARA, "--as-of", "2026-11-10", "--side", "short", "--fno", "--lot", "250",
                "--capital", "5000000"])["short"]
        self.assertEqual(s["status"], "waiting")
        self.assertLess(s["trigger"], s["climax_low"])
        self.assertEqual(s["entry"], s["trigger"])

    def test_ban_list_blocks_short(self):
        s = js(["plan", "--csv-dir", UNI, "--symbol", "STEADY", "--side", "short", "--fno-list", FNO,
                "--ban-list", BAN])["short"]
        self.assertFalse(s["allowed"])
        self.assertTrue(s["banned"])
        self.assertIn("ban", s["reason"])

    def test_put_alternative(self):
        s = js(["plan", "--csv", PARA, "--side", "short", "--fno", "--lot", "250", "--capital", "2000000",
                "--put-premium", "40", "--put-strike", "1000"])["short"]
        self.assertEqual(s["put"]["lots"], 2)                          # ₹20,000 budget / (40 × 250)
        self.assertEqual(s["put"]["breakeven"], 960)


class TestWashout(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.res = js(["washout", "--csv-dir", UNI, "--capital", "1000000"])
        cls.hits = by_symbol(cls.res["washouts"])

    def test_planted_hammer_found(self):
        h = self.hits["HAMMER"]
        self.assertEqual(h["date"], "2026-11-11")
        self.assertTrue(h["undercut"]["reclaimed"])
        self.assertGreaterEqual(h["wick_pct"], 50)
        self.assertGreaterEqual(h["close_pos_pct"], 60)
        self.assertGreater(h["vol_vs_50d"], 1.5)
        self.assertGreaterEqual(h["score"], 70)
        self.assertEqual(h["status"], "triggered")
        self.assertEqual(h["status_date"], "2026-11-12")
        self.assertGreater(h["entry"], h["stop"])
        for x in (h["entry"], h["stop"]):                              # NSE tick 0.05 for ₹250-1,000
            self.assertAlmostEqual(round(x / 0.05), x / 0.05, places=6)
        self.assertGreater(h["sizing"]["qty"], 0)

    def test_weak_close_rejected(self):
        self.assertNotIn("WEAKCLOSE", self.hits)
        rej = [e for e in self.res["rejected"] if e["symbol"] == "WEAKCLOSE" and e["date"] == "2026-11-11"]
        self.assertEqual(len(rej), 1)
        self.assertTrue(any("closed at" in r for r in rej[0]["reasons"]), rej[0]["reasons"])

    def test_parabolic_not_a_washout(self):
        self.assertNotIn("PARABOLIC", self.hits)
        self.assertNotIn("STEADY", self.hits)


class TestReaction(unittest.TestCase):
    def test_bullish_news_failure(self):
        r = js(["reaction", "--csv", NEWS_FAIL, "--date", "2026-10-27", "--news", "bullish", "--crowding", "long"])
        self.assertEqual(r["verdict"], "failed")
        self.assertTrue(r["rule"].startswith("rejection day"))
        self.assertTrue(r["window"]["news_day_extreme_broken"])
        self.assertTrue(r["gate"]["price_confirmed"])
        self.assertTrue(r["gate"]["status"].startswith("contrarian setup complete"))

    def test_bullish_news_confirmed(self):
        r = js(["reaction", "--csv", NEWS_OK, "--date", "2026-10-27", "--news", "bullish"])
        self.assertEqual(r["verdict"], "confirmed")
        self.assertTrue(r["rule"].startswith("response held"))
        self.assertFalse(r["gate"]["reaction_failed"])

    def test_bearish_reading_of_a_rally_fails(self):
        r = js(["reaction", "--csv", NEWS_OK, "--date", "2026-10-27", "--news", "bearish"])
        self.assertEqual(r["verdict"], "failed")

    def test_inconclusive_and_one_session(self):
        r = js(["reaction", "--csv", NEWS_OK, "--date", "2026-10-27", "--news", "bullish", "--min-move-atr", "50"])
        self.assertEqual(r["verdict"], "inconclusive")
        r = js(["reaction", "--csv", NEWS_FAIL, "--date", "2026-10-27", "--news", "bullish", "--sessions", "1"])
        self.assertEqual(r["sessions_used"], 1)
        self.assertEqual(r["window"]["end_date"], "2026-10-27")

    def test_after_close_shifts_day_one(self):
        r = js(["reaction", "--csv", NEWS_FAIL, "--date", "2026-10-26", "--news", "bullish", "--after-close"])
        self.assertEqual(r["day1"]["date"], "2026-10-27")
        self.assertEqual(r["verdict"], "failed")


class TestScan(unittest.TestCase):
    def test_ranks_and_tags(self):
        r = js(["scan", "--csv-dir", UNI, "--fno-list", FNO, "--ban-list", BAN, "--bands", BANDS])
        ex_ = r["exhausted"]
        self.assertEqual([e["symbol"] for e in ex_], ["UCSTREAK", "PARABOLIC"])
        self.assertEqual([e["score"] for e in ex_], sorted((e["score"] for e in ex_), reverse=True))
        tags = by_symbol(ex_)
        self.assertTrue(tags["PARABOLIC"]["fno"])
        self.assertIn("futures", tags["PARABOLIC"]["short_route"])
        self.assertFalse(tags["UCSTREAK"]["fno"])
        self.assertIn("not in F&O", tags["UCSTREAK"]["short_route"])
        self.assertEqual([e["symbol"] for e in r["washouts"]], ["HAMMER"])
        self.assertTrue(r["washouts"][0]["fno"])
        filt = by_symbol(r["filtered"])
        self.assertIn("illiquid", filt["PUMPILLIQ"]["reason"])

    def test_text_scan(self):
        rc, out, _ = run(["scan", "--csv-dir", UNI, "--fno-list", FNO])
        self.assertEqual(rc, 0)
        self.assertIn("Exhausted on the upside", out)
        self.assertIn("Washed out on the downside", out)


class TestInputs(unittest.TestCase):
    def test_symbol_list_formats(self):
        lots = ex.load_symbol_list(str(FIXTURES / "pairs_lots.csv"), "--fno-list")
        self.assertEqual(lots["BANKB"], 250)
        ban = ex.load_symbol_list(str(FIXTURES / "pairs_secban.csv"), "--ban-list")
        self.assertIn("METALA", ban)

    def test_null_candles_skipped(self):
        rows = Path(PARA).read_text().splitlines()
        rows.insert(100, "2026-01-01,,,,,")
        rows.insert(120, "2026-01-02,0,0,0,0,0")
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "PARABOLIC.csv"
            f.write_text("\n".join(rows) + "\n")
            r = js(["climax", "--csv", str(f)])["results"][0]
        self.assertEqual(r["stage"], "reversal confirmed")

    def test_bad_input_exits_2(self):
        cases = [
            ["climax", "--csv", "/no/such/file.csv"],
            ["plan", "--csv-dir", UNI],                                   # needs one stock
            ["plan", "--csv-dir", UNI, "--symbol", "NOPE"],
            ["reaction", "--csv", NEWS_OK, "--date", "27-10-2026", "--news", "bullish"],
            ["reaction", "--csv", NEWS_OK, "--date", "2026-10-27", "--news", "bullish", "--sessions", "5"],
            ["washout", "--csv-dir", UNI, "--min-wick", "1.5"],
            ["scan", "--csv-dir", UNI, "--fno-list", "/no/such/list.csv"],
            ["plan", "--csv", PARA, "--side", "short", "--fno", "--lot", "1"],
            ["climax", "--csv", PARA, "--as-of", "2026/11/01"],
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                rc, out, err = run(argv)
                self.assertEqual(rc, 2)
                self.assertTrue(err.startswith("error: "), err)
                self.assertEqual(len(err.strip().splitlines()), 1)
                self.assertNotIn("Traceback", err)

    def test_short_history(self):
        rows = Path(PARA).read_text().splitlines()[:20]
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "TINY.csv"
            f.write_text("\n".join(rows) + "\n")
            rc, _, err = run(["climax", "--csv", str(f)])
        self.assertEqual(rc, 2)
        self.assertIn("30+", err)


if __name__ == "__main__":
    unittest.main()
