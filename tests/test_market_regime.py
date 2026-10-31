import contextlib
import io
import json
import unittest

from _load import FIXTURES, load

rg = load("market-regime", "regime")
ta = rg.ta

DIST = str(FIXTURES / "regime_distribution.csv")
FTD = str(FIXTURES / "regime_ftd.csv")
NOVOL = str(FIXTURES / "regime_novolume.csv")
PROXY = str(FIXTURES / "regime_proxy_volume.csv")

# Bar indices planted by the fixture generator.
DD_CLUSTER = (250, 254, 258, 262, 266)
DD_CLEARED, CLEAR_BAR = 280, 285
FAILED_ATTEMPT_BAR, PRICE_ONLY_FTD_BAR, FTD_BAR = 233, 238, 239


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = rg.main(argv)
    return code, out.getvalue(), err.getvalue()


class TestDistribution(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = ta.load_csv(DIST)

    def dd(self, upto, **kw):
        return rg.distribution_days(self.rows[:upto + 1], "volume", **kw)

    def day(self, i):
        return self.rows[i]["date"].date().isoformat()

    def test_cluster_counts(self):
        res = self.dd(266)
        self.assertEqual(res["counts"], {"d5": 2, "d15": 4, "d25": 5})
        self.assertEqual([r["date"] for r in res["active"]], [self.day(i) for i in DD_CLUSTER])

    def test_controls_are_not_distribution(self):
        dates = {r["date"] for r in self.dd(299)["recent"]}
        self.assertNotIn(self.day(252), dates)   # decline on lower volume
        self.assertNotIn(self.day(260), dates)   # higher volume but only -0.15%

    def test_expires_after_25_sessions(self):
        at24 = self.dd(DD_CLUSTER[0] + 24)
        self.assertEqual(at24["counts"]["d25"], 5)
        oldest = at24["active"][0]
        self.assertEqual((oldest["date"], oldest["sessions_left"]), (self.day(DD_CLUSTER[0]), 1))
        at25 = self.dd(DD_CLUSTER[0] + 25)
        self.assertEqual(at25["counts"]["d25"], 4)
        first = next(r for r in at25["recent"] if r["date"] == self.day(DD_CLUSTER[0]))
        self.assertEqual(first["status"], "expired")

    def test_cleared_by_five_percent_rally(self):
        before = self.dd(CLEAR_BAR - 1)
        self.assertIn(self.day(DD_CLEARED), [r["date"] for r in before["active"]])
        after = self.dd(CLEAR_BAR)
        rec = next(r for r in after["recent"] if r["date"] == self.day(DD_CLEARED))
        self.assertEqual((rec["status"], rec["cleared_on"]), ("cleared", self.day(CLEAR_BAR)))
        self.assertEqual(after["counts"]["d25"], 0)

    def test_ignore_dates(self):
        res = self.dd(266, ignore={self.rows[266]["date"].date()})
        self.assertEqual(res["counts"]["d25"], 4)

    def test_expiry_tag(self):
        res = self.dd(266, expiry_weekday=self.rows[258]["date"].weekday())
        tagged = [r["date"] for r in res["active"] if "expiry day" in r["tags"]]
        self.assertIn(self.day(258), tagged)


class TestFollowThrough(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = ta.load_csv(FTD)

    def day(self, i):
        return self.rows[i]["date"].date().isoformat()

    def test_ftd_on_right_day_after_failed_attempt(self):
        res = rg.follow_through(self.rows, "volume", ftd_pct=1.2)
        ev = [(e["event"], e["date"]) for e in res["events"]]
        self.assertIn(("rally attempt failed", self.day(FAILED_ATTEMPT_BAR)), ev)
        ftds = [d for name, d in ev if name == "follow-through day"]
        self.assertEqual(ftds, [self.day(FTD_BAR)])   # not day 3 (too early) nor day 4 (lower volume)

    def test_state_progression(self):
        st = lambda i: rg.follow_through(self.rows[:i + 1], "volume", ftd_pct=1.2)["current"]
        self.assertEqual(st(FAILED_ATTEMPT_BAR - 1)["state"], "rally_attempt")
        self.assertEqual(st(FAILED_ATTEMPT_BAR)["state"], "correction")
        cur = st(FTD_BAR - 3)
        self.assertEqual((cur["state"], cur["rally_day"]), ("rally_attempt", 2))
        self.assertEqual(cur["sessions_until_window"], 1)
        cur = st(FTD_BAR)
        self.assertEqual((cur["state"], cur["ftd_date"]), ("follow_through", self.day(FTD_BAR)))
        self.assertEqual(st(len(self.rows) - 1)["state"], "uptrend")   # >25 sessions later

    def test_regime_labels_through_the_cycle(self):
        label = lambda i: rg.analyse(self.rows[:i + 1], "T", ftd_pct=1.2)["regime"]
        self.assertEqual(label(FAILED_ATTEMPT_BAR)["label"], "Correction")
        self.assertEqual(label(FAILED_ATTEMPT_BAR)["max_new_positions"], 0)
        self.assertEqual(label(FTD_BAR - 1)["label"], "Rally attempt")
        fresh = label(FTD_BAR)
        self.assertTrue(fresh["fresh_follow_through"])
        self.assertLessEqual(fresh["exposure_ceiling_pct"], 50)


class TestVolumeFallback(unittest.TestCase):
    def test_price_only_when_volume_zero(self):
        rows = ta.load_csv(NOVOL)
        rows2, mode, source, notes = rg.choose_volume(rows, "NIFTY")
        self.assertEqual(mode, "price-only")
        self.assertTrue(any("PRICE-ONLY" in n for n in notes))
        res = rg.analyse(rows2, "NIFTY", mode, source, notes, ftd_pct=1.2)
        self.assertEqual(res["volume_mode"], "price-only")
        self.assertEqual(res["distribution"]["skipped_no_volume"], 0)
        ftd = [e["date"] for e in res["follow_through"]["events"] if e["event"] == "follow-through day"]
        self.assertEqual(ftd, [rows[PRICE_ONLY_FTD_BAR]["date"].date().isoformat()])   # volume veto skipped

    def test_proxy_volume_csv_restores_volume_rules(self):
        rows, proxy = ta.load_csv(NOVOL), ta.load_csv(PROXY)
        rows2, mode, source, _ = rg.choose_volume(rows, "NIFTY", proxy, "niftybees.csv")
        self.assertEqual((mode, source), ("volume", "proxy niftybees.csv"))
        res = rg.follow_through(rows2, mode, ftd_pct=1.2)
        self.assertEqual(res["events"][-1]["date"], rows[FTD_BAR]["date"].date().isoformat())

    def test_auto_proxy_fetch_for_nifty(self):
        rows, proxy = ta.load_csv(NOVOL), ta.load_csv(PROXY)
        asked = []
        fetch = lambda s: asked.append(s) or proxy
        _, mode, source, _ = rg.choose_volume(rows, "NIFTY", fetcher=fetch)
        self.assertEqual((asked, mode, source), (["NIFTYBEES"], "volume", "proxy NIFTYBEES.NS"))

        def broken(_):
            raise SystemExit("no network")
        _, mode, _, notes = rg.choose_volume(rows, "NIFTY", fetcher=broken)
        self.assertEqual(mode, "price-only")
        self.assertTrue(any("Could not fetch" in n for n in notes))

    def test_thin_prior_session_not_judged(self):
        # A Muhurat-style thin session before a planted DD: "higher volume" is meaningless, so skip it.
        rows = [dict(r) for r in ta.load_csv(DIST)[:267]]
        rows[265]["volume"] = 100_000
        res = rg.distribution_days(rows, "volume")
        self.assertEqual(res["counts"]["d25"], 4)
        self.assertEqual(res["skipped_no_volume"], 1)

    def test_constant_volume_is_a_placeholder(self):
        rows = [{**r, "volume": 5.0} for r in ta.load_csv(FTD)]
        self.assertFalse(rg.volume_quality(rows)["usable"])


class TestHelpers(unittest.TestCase):
    def test_num_indian_grouping(self):
        self.assertEqual(rg.num(8123456.5), "81,23,456.50")
        self.assertEqual(rg.num(24850), "24,850.00")
        self.assertEqual(rg.num(None), "—")

    def test_auto_ftd_threshold_clamped(self):
        rows = ta.load_csv(DIST)
        self.assertEqual(rg.auto_ftd_pct(rows), 1.0)   # calm synthetic series hits the floor
        self.assertTrue(1.0 <= rg.auto_ftd_pct(ta.load_csv(FTD)) <= 2.0)

    def test_score_and_vix(self):
        rows = ta.load_csv(DIST)[:267]
        calm = rg.analyse(rows, "T", vix=11)
        hot = rg.analyse(rows, "T", vix=32)
        self.assertLess(calm["risk"]["score"], hot["risk"]["score"])
        self.assertTrue(0 <= calm["risk"]["score"] <= 100)
        self.assertEqual(calm["regime"]["label"], "Uptrend under pressure")
        self.assertTrue(calm["what_would_change"])

    def test_stalling_optional(self):
        rows = ta.load_csv(DIST)
        base = rg.distribution_days(rows, "volume")
        self.assertTrue(all(r["kind"] == "distribution" for r in base["recent"]))


class TestCLI(unittest.TestCase):
    def test_json_outputs(self):
        for cmd in ("status", "distribution", "ftd", "trend"):
            code, out, _ = run([cmd, "--csv", FTD, "--ftd-pct", "1.2", "--vix", "14", "--json"])
            self.assertEqual(code, 0, cmd)
            data = json.loads(out)
            self.assertIn("as_of", data)
        code, out, _ = run(["status", "--csv", NOVOL, "--volume-csv", PROXY, "--json"])
        data = json.loads(out)
        self.assertEqual(data["volume_mode"], "volume")
        self.assertIn(data["regime"]["label"], ("Confirmed uptrend", "Uptrend under pressure",
                                                "Correction", "Rally attempt"))

    def test_text_outputs(self):
        code, out, _ = run(["status", "--csv", NOVOL])
        self.assertEqual(code, 0)
        self.assertIn("price-only", out)
        self.assertIn("Exposure ceiling", out)
        code, out, _ = run(["distribution", "--csv", DIST, "--all", "--stalling"])
        self.assertEqual(code, 0)
        code, out, _ = run(["ftd", "--csv", FTD, "--ftd-pct", "1.2", "--as-of",
                            ta.load_csv(FTD)[FTD_BAR]["date"].date().isoformat()])
        self.assertIn("follow-through", out)

    def test_bad_input_exits_cleanly(self):
        cases = [["status"], ["status", "--csv", "missing.csv"], ["trend", "--csv", DIST, "--as-of", "nope"],
                 ["status", "--csv", DIST, "--as-of", "2025-01-10"], ["status", "--csv", DIST, "--ignore", "x"],
                 ["status", "--csv", DIST, "--dd-pct", "-1"], ["status", "--csv", DIST, "--vix", "0"]]
        for argv in cases:
            code, out, err = run(argv)
            self.assertEqual(code, 2, argv)
            self.assertTrue(err.startswith("error:"), argv)
            self.assertNotIn("Traceback", err)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            rg.main(["nonsense"])
        self.assertEqual(cm.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
