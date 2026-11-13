import contextlib
import io
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from _load import FIXTURES, load

vz = load("market-valuation", "valuation")

PE = str(FIXTURES / "valuation_nifty50_pe.csv")
TINY = str(FIXTURES / "valuation_tiny.csv")
GSEC = str(FIXTURES / "valuation_gsec10y.csv")
MCAP = str(FIXTURES / "valuation_mcap_gdp.csv")
INPUTS_JSON = str(FIXTURES / "valuation_inputs.json")
INPUTS_CSV = str(FIXTURES / "valuation_inputs.csv")
BREADTH = str(FIXTURES / "valuation_breadth.json")
AMBIG = str(FIXTURES / "valuation_ambiguous.csv")
BADNUM = str(FIXTURES / "valuation_badnum.csv")


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = vz.main(argv)
    return code, out.getvalue(), err.getvalue()


def strict_json(text):
    def reject(c):
        raise ValueError(f"non-standard JSON constant {c}")
    return json.loads(text, parse_constant=reject)


def run_json(argv):
    code, out, err = run(argv + ["--json"])
    if code != 0:
        raise AssertionError(f"exit {code}: {err}")
    return strict_json(out)


class TestDates(unittest.TestCase):
    def test_unambiguous_formats(self):
        got = vz.parse_dates(["2026-10-30", "30-Oct-2026", "30 Oct 2026", "Oct 30, 2026", "30-10-2026",
                              "20261030", "Note: consolidated", ""])
        self.assertEqual(got[:6], [date(2026, 10, 30)] * 6)
        self.assertEqual(got[6:], [None, None])

    def test_month_only_is_month_end(self):
        self.assertEqual(vz.parse_dates(["Sep-2026"]), [date(2026, 9, 30)])

    def test_slash_dates_need_evidence(self):
        with self.assertRaisesRegex(vz.ValuationError, "ambiguous"):
            vz.parse_dates(["05/10/2026", "06/10/2026"])
        self.assertEqual(vz.parse_dates(["10/05/2026", "10/31/2026"]), [date(2026, 10, 5), date(2026, 10, 31)])
        self.assertEqual(vz.parse_dates(["05/10/2026"], "dmy"), [date(2026, 10, 5)])
        with self.assertRaisesRegex(vz.ValuationError, "mixes"):
            vz.parse_dates(["13/10/2026", "10/13/2026"])

    def test_two_digit_year_and_month_first_dashes(self):
        with self.assertRaisesRegex(vz.ValuationError, "two-digit year"):
            vz.parse_dates(["30-10-26"])
        with self.assertRaisesRegex(vz.ValuationError, "not DD-MM-YYYY"):
            vz.parse_dates(["10-31-2026"])


class TestPercentileMath(unittest.TestCase):
    """Tiny series checked by hand: P/E 10, 20, 30, 40, then 25 today."""

    def test_rank_stats_by_hand(self):
        st = vz.rank_stats([10, 20, 30, 40, 25], 25)
        self.assertAlmostEqual(st["percentile"], 60.0)          # 10, 20, 25 are ≤ 25 → 3/5
        self.assertAlmostEqual(st["richness_percentile"], 60.0)
        self.assertAlmostEqual(st["pct_time_richer"], 40.0)     # 30 and 40 were richer
        self.assertAlmostEqual(st["pct_time_above"], 40.0)

    def test_inverted_metric(self):
        # dividend yield 2.0, 1.5, 1.0, 0.8 then 1.2: yields ≥ 1.2 are 2.0, 1.5, 1.2 → 60% as cheap or cheaper
        st = vz.rank_stats([2.0, 1.5, 1.0, 0.8, 1.2], 1.2, higher_is_richer=False)
        self.assertAlmostEqual(st["percentile"], 60.0)          # raw: 1.0, 0.8, 1.2 are ≤ 1.2
        self.assertAlmostEqual(st["richness_percentile"], 60.0)
        self.assertAlmostEqual(st["pct_time_richer"], 40.0)     # 1.0 and 0.8 were richer (lower yield)

    def test_record_high_is_100(self):
        self.assertEqual(vz.rank_stats([1, 2, 3], 3)["percentile"], 100.0)

    def test_tiny_file(self):
        res = run_json(["pe", "--csv", TINY])
        pe, pb, dy = (res["metrics"][k] for k in ("pe", "pb", "div_yield"))
        self.assertEqual(pe["current"], 25)
        self.assertAlmostEqual(pe["headline"]["richness_percentile"], 60.0)
        self.assertEqual(pe["headline"]["window"], "all")
        self.assertTrue(pe["headline"]["thin"])
        self.assertEqual(pe["headline"]["band"], "fair")
        self.assertAlmostEqual(pb["headline"]["richness_percentile"], 100.0)
        self.assertEqual(pb["headline"]["band"], "extreme")
        self.assertAlmostEqual(dy["headline"]["richness_percentile"], 60.0)
        all_w = next(w for w in pe["windows"] if w["window"] == "all")
        self.assertEqual((all_w["min"], all_w["median"], all_w["max"]), (10, 25, 40))
        five = next(w for w in pe["windows"] if w["window"] == "5y")
        self.assertFalse(five["complete"])                       # 5 months of data for a 5-year window


class TestBands(unittest.TestCase):
    def test_default_thresholds(self):
        cases = {0: "cheap", 24.9: "cheap", 25: "fair", 74.9: "fair", 75: "expensive", 89.9: "expensive",
                 90: "extreme", 100: "extreme"}
        for p, want in cases.items():
            self.assertEqual(vz.band_for(p), want, p)

    def test_custom_bands_flag(self):
        res = run_json(["pe", "--csv", TINY, "--bands", "10,50,95"])
        self.assertEqual(res["metrics"]["pe"]["headline"]["band"], "expensive")     # 60 ≥ 50
        self.assertEqual(res["metrics"]["pb"]["headline"]["band"], "extreme")
        for bad in ("75,25,90", "10,20", "0,50,90"):
            code, _, err = run(["pe", "--csv", TINY, "--bands", bad])
            self.assertEqual(code, 2, bad)
            self.assertTrue(err.startswith("error:"))

    def test_mcap_gdp_bands(self):
        cases = {55: "cheap", 60: "fair", 99.9: "fair", 100: "elevated", 120: "expensive", 139.9: "expensive",
                 140: "extreme", 160: "extreme"}
        for r, want in cases.items():
            self.assertEqual(vz.mcap_gdp_band(r), want, r)


class TestPeHistory(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows, cls.meta, cls.notes = vz.load_valuation(PE)
        cls.res = run_json(["pe", "--csv", PE])

    def test_loaded_and_note_row_kept(self):
        self.assertEqual(len(self.rows), 190)
        self.assertEqual(self.meta["index"], "NIFTY 50")
        self.assertEqual(self.rows[-1], {"date": date(2026, 10, 30), "pe": 22.4, "pb": 3.7, "div_yield": 1.25})
        self.assertTrue(any("consolidated" in n for n in self.meta["file_notes"]))

    def test_switch_detected_and_headline_consolidated_only(self):
        b = self.res["basis"]
        self.assertTrue(b["spans_switch"])
        self.assertEqual(b["switch_date"], "2021-03-31")
        self.assertEqual(b["current_basis"], "consolidated")
        self.assertLess(b["break"]["pe_change_pct"], -10)                 # planted ~18% step down
        self.assertLess(abs(b["break"]["pb_change_pct"]), 5)              # P/B barely moved
        pe = self.res["metrics"]["pe"]
        self.assertEqual(pe["headline"]["window"], "since 31-Mar-2021")
        self.assertEqual(pe["headline"]["from"], "2021-03-31")
        mixed = {w["window"]: w["mixed_basis"] for w in pe["windows"]}
        self.assertTrue(mixed["all"])
        self.assertTrue(mixed["10y"])
        self.assertFalse(mixed["5y"])
        # mixing in the higher standalone P/Es makes today look cheaper than the consolidated-only view
        all_w = next(w for w in pe["windows"] if w["window"] == "all")
        self.assertLess(all_w["richness_percentile"], pe["headline"]["richness_percentile"])
        self.assertTrue(any("Basis break" in r for r in self.res["read"]))

    def test_percentile_matches_manual_count(self):
        post = [r["pe"] for r in self.rows if r["date"] >= date(2021, 3, 31)]
        want = sum(1 for v in post if v <= 22.4) / len(post) * 100
        self.assertAlmostEqual(self.res["metrics"]["pe"]["headline"]["percentile"], want, places=3)

    def test_since_filters_everything(self):
        res = run_json(["pe", "--csv", PE, "--since", "2021-03-31"])
        self.assertFalse(res["basis"]["spans_switch"])
        self.assertEqual(res["first_date"], "2021-03-31")
        n_post = sum(1 for r in self.rows if r["date"] >= date(2021, 3, 31))
        self.assertEqual(res["observations"], n_post)
        for k in ("pe", "pb", "div_yield"):
            for w in res["metrics"][k]["windows"]:
                self.assertGreaterEqual(w["from"], "2021-03-31")
                self.assertFalse(w["mixed_basis"])
        self.assertEqual(res["metrics"]["pe"]["headline"]["window"], "all")
        self.assertAlmostEqual(res["metrics"]["pe"]["headline"]["percentile"],
                               self.res["metrics"]["pe"]["headline"]["percentile"], places=6)

    def test_since_before_switch_keeps_both(self):
        res = run_json(["pe", "--csv", PE, "--since", "2019-01-01"])
        self.assertTrue(res["basis"]["spans_switch"])
        self.assertEqual(res["first_date"], "2019-01-31")

    def test_as_of_replay(self):
        res = run_json(["pe", "--csv", PE, "--as-of", "2020-12-31"])
        self.assertEqual(res["as_of"], "2020-12-31")
        self.assertEqual(res["basis"]["current_basis"], "standalone")
        self.assertFalse(res["basis"]["spans_switch"])

    def test_windows_flag(self):
        res = run_json(["pe", "--csv", PE, "--windows", "3"])
        names = [w["window"] for w in res["metrics"]["pb"]["windows"]]
        self.assertEqual(names, ["3y", "all"])
        three = res["metrics"]["pb"]["windows"][0]
        self.assertEqual(three["from"], "2023-10-31")

    def test_index_filter(self):
        self.assertEqual(run_json(["pe", "--csv", PE, "--index", "nifty 50"])["index"], "NIFTY 50")
        code, _, err = run(["pe", "--csv", PE, "--index", "NIFTY BANK"])
        self.assertEqual(code, 2)
        self.assertIn("NIFTY 50", err)

    def test_text_output(self):
        code, out, _ = run(["pe", "--csv", PE])
        self.assertEqual(code, 0)
        self.assertIn("† window mixes standalone", out)
        self.assertIn("Educational analysis, not investment advice.", out)


class TestYieldGap(unittest.TestCase):
    def test_arithmetic(self):
        res = run_json(["yieldgap", "--pe", "20", "--gsec", "6.5"])
        self.assertAlmostEqual(res["earnings_yield"], 5.0)
        self.assertAlmostEqual(res["yield_gap_pp"], -1.5)
        self.assertAlmostEqual(res["bond_to_earnings_yield_ratio"], 1.3)
        self.assertAlmostEqual(res["pe_where_gap_is_zero"], 100 / 6.5, places=4)
        self.assertIsNone(res["history"])
        self.assertTrue(res["limitations"])

    def test_history_with_both_files(self):
        res = run_json(["yieldgap", "--csv", PE, "--yields", GSEC])
        self.assertAlmostEqual(res["pe"], 22.4)
        self.assertAlmostEqual(res["gsec_10y"], 6.55)
        self.assertAlmostEqual(res["yield_gap_pp"], 100 / 22.4 - 6.55, places=3)
        h = res["history"]
        self.assertEqual(h["observations"], 190)
        self.assertTrue(h["spans_switch"])
        self.assertEqual(h["headline"]["window"], "since 31-Mar-2021")
        # manual percentile of the consolidated-only gap history
        rows, _, _ = vz.load_valuation(PE)
        ys = dict(vz.load_yields(GSEC))
        gaps = [100 / r["pe"] - ys[r["date"]] for r in rows if r["date"] >= date(2021, 3, 31)]
        cur = gaps[-1]
        want = sum(1 for g in gaps if g >= cur) / len(gaps) * 100
        self.assertAlmostEqual(h["headline"]["richness_percentile"], want, places=3)

    def test_yields_slash_dates_resolved_by_evidence(self):
        ys = vz.load_yields(GSEC)                     # MM/DD/YYYY, proven by days above 12
        self.assertEqual(ys[-1], (date(2026, 10, 30), 6.55))

    def test_since_on_gap(self):
        res = run_json(["yieldgap", "--csv", PE, "--yields", GSEC, "--since", "2021-03-31"])
        self.assertFalse(res["history"]["spans_switch"])
        self.assertEqual(res["history"]["first_date"], "2021-03-31")

    def test_bad_inputs(self):
        for argv in (["yieldgap", "--pe", "20"], ["yieldgap", "--pe", "20", "--gsec", "0.065"],
                     ["yieldgap", "--pe", "-5", "--gsec", "6.5"], ["yieldgap", "--pe", "abc", "--gsec", "6.5"],
                     ["yieldgap", "--pe", "nan", "--gsec", "6.5"]):
            code, out, err = run(argv)
            self.assertEqual(code, 2, argv)
            self.assertEqual(out, "")
            self.assertTrue(err.startswith("error:"), err)
            self.assertNotIn("Traceback", err)


class TestMcapGdp(unittest.TestCase):
    def test_flags(self):
        res = run_json(["mcap-gdp", "--mcap", "470", "--gdp", "357"])
        self.assertAlmostEqual(res["ratio_pct"], 470 / 357 * 100, places=3)
        self.assertEqual(res["band"], "expensive")
        self.assertAlmostEqual(res["mcap_at_long_run_average"][0], 357 * 0.75)

    def test_ratio_only_and_bands(self):
        self.assertEqual(run_json(["mcap-gdp", "--ratio", "55"])["band"], "cheap")
        self.assertEqual(run_json(["mcap-gdp", "--ratio", "149"])["band"], "extreme")

    def test_history_csv(self):
        res = run_json(["mcap-gdp", "--csv", MCAP])
        self.assertEqual(res["as_of"], "2026-03-31")
        self.assertAlmostEqual(res["ratio_pct"], 470 / 357 * 100, places=3)
        h = res["history"]
        self.assertEqual(h["observations"], 11)
        ratios = sorted([94 / 137, 121 / 153, 142 / 171, 151 / 189, 113 / 201, 204 / 198, 264 / 236, 258 / 269,
                         387 / 295, 412 / 331, 470 / 357])
        want = sum(1 for r in ratios if r <= 470 / 357) / 11 * 100
        self.assertAlmostEqual(h["percentile"], want, places=3)
        self.assertTrue(h["thin"])

    def test_unit_slip_and_missing(self):
        for argv in (["mcap-gdp", "--mcap", "470"], ["mcap-gdp", "--mcap", "470", "--gdp", "35700"],
                     ["mcap-gdp", "--mcap", "470", "--gdp", "357", "--ratio", "130"], ["mcap-gdp"]):
            code, _, err = run(argv)
            self.assertEqual(code, 2, argv)
            self.assertTrue(err.startswith("error:"))


class TestFroth(unittest.TestCase):
    ASOF = "2026-11-12"

    @classmethod
    def setUpClass(cls):
        cls.full = run_json(["froth", "--inputs", INPUTS_JSON, "--as-of", cls.ASOF])
        cls.inputs = vz.load_inputs(INPUTS_JSON)

    def test_full_coverage(self):
        r = self.full
        self.assertEqual(r["components_scored"], 12)
        self.assertAlmostEqual(r["coverage_pct"], 100.0)
        self.assertEqual(r["confidence"], "high")
        self.assertEqual(r["missing"], [])
        self.assertAlmostEqual(sum(c["points"] for c in r["components"]), r["score"], places=2)
        self.assertAlmostEqual(sum(c["effective_weight"] for c in r["components"]), 100.0, places=2)
        self.assertEqual(r["phase"], vz.phase_for(r["score"]))
        self.assertEqual(r["posture"]["framing"], "risk management, not a market call")
        self.assertFalse(any(c["stale"] for c in r["components"]))
        for c in r["components"]:
            self.assertTrue(c["input"] and c["where"], c["key"])
            self.assertTrue(0 <= c["sub_score"] <= 100)

    def test_component_arithmetic(self):
        comp = {c["key"]: c for c in self.full["components"]}
        self.assertAlmostEqual(comp["yield_gap"]["sub_score"], vz.pwl(100 / 22.4 - 6.55, vz.ANCHORS["yield_gap"]), places=3)
        self.assertAlmostEqual(comp["mcap_gdp"]["sub_score"], vz.pwl(470 / 357 * 100, vz.ANCHORS["mcap_gdp"]), places=3)
        prem = ((33.0 + 30.0) / 2 / 22.4 - 1) * 100
        self.assertAlmostEqual(comp["smid_premium"]["sub_score"], vz.pwl(prem, vz.ANCHORS["smid_premium"]), places=3)
        self.assertAlmostEqual(comp["mtf"]["sub_score"], vz.pwl((148000 / 96000 - 1) * 100, vz.ANCHORS["mtf_yoy"]), places=3)
        ipo = (vz.pwl(5.0, vz.ANCHORS["ipo_per_month"]) + vz.pwl(18, vz.ANCHORS["ipo_gain"])) / 2
        self.assertAlmostEqual(comp["ipo"]["sub_score"], ipo, places=3)
        self.assertEqual(comp["mtf"]["as_of"], "2026-10-31")     # year-ago base does not set the date
        self.assertEqual(comp["mtf"]["source"], "NSE+BSE MTF")

    def test_pwl(self):
        pts = [(0, 0), (10, 50), (20, 100)]
        self.assertEqual(vz.pwl(-5, pts), 0)
        self.assertEqual(vz.pwl(5, pts), 25)
        self.assertEqual(vz.pwl(15, pts), 75)
        self.assertEqual(vz.pwl(99, pts), 100)

    def test_phases(self):
        cases = {0: "Depressed", 19.9: "Depressed", 20: "Normal", 40: "Warm", 60: "Frothy", 79.9: "Frothy",
                 80: "Euphoric", 100: "Euphoric"}
        for s, want in cases.items():
            self.assertEqual(vz.phase_for(s), want, s)

    def test_partial_renormalises(self):
        res = vz.analyse_froth({"vix": 11.0, "mtf_yoy": 40.0}, date(2026, 11, 12))
        self.assertAlmostEqual(res["coverage_pct"], 20.0)               # 8 + 12 of 100
        self.assertEqual(res["confidence"], "low")
        comp = {c["key"]: c for c in res["components"]}
        self.assertAlmostEqual(comp["vix"]["effective_weight"], 40.0)
        self.assertAlmostEqual(comp["mtf"]["effective_weight"], 60.0)
        self.assertAlmostEqual(res["score"], 0.4 * 80 + 0.6 * 60)       # anchors: VIX 11 → 80, MTF +40% → 60
        self.assertEqual(len(res["missing"]), 10)
        self.assertTrue(any("coverage is only 20%" in w for w in res["warnings"]))
        self.assertTrue(any("staleness cannot be checked" in w for w in res["warnings"]))

    def test_single_input_scores_its_subscore(self):
        res = vz.analyse_froth({"valuation_percentile": 90}, date(2026, 11, 12))
        self.assertAlmostEqual(res["score"], 90.0)
        self.assertEqual(res["phase"], "Euphoric")

    def test_monotonic_in_each_component(self):
        base = {"valuation_percentile": 50, "yield_gap": -2.0, "mcap_gdp": 110, "smid_premium": 20, "vix": 14,
                "breadth_score": 70, "ipo_count": 9, "ipo_months": 3, "ipo_listing_gain": 10,
                "sme_subscription": 50, "sme_over_100x_pct": 20, "sip_yoy": 20, "demat_new": 25,
                "demat_avg_12m": 25, "fno_yoy": 20, "mtf_yoy": 30}
        hotter = {"valuation_percentile": 95, "yield_gap": -3.5, "mcap_gdp": 145, "smid_premium": 60, "vix": 10,
                  "breadth_score": 92, "ipo_count": 24, "ipo_listing_gain": 35, "sme_subscription": 250,
                  "sme_over_100x_pct": 40, "sip_yoy": 50, "demat_new": 45, "fno_yoy": 80, "mtf_yoy": 70}
        d = date(2026, 11, 12)
        s0 = vz.analyse_froth(base, d)["score"]
        for k, v in hotter.items():
            with self.subTest(input=k):
                s1 = vz.analyse_froth({**base, k: v}, d)["score"]
                self.assertGreater(s1, s0)
        # and never decreasing over a sweep of each input's range
        sweeps = {"valuation_percentile": range(0, 101, 5), "mcap_gdp": range(40, 200, 10),
                  "mtf_yoy": range(-20, 120, 10), "sip_yoy": range(-10, 80, 5)}
        for k, xs in sweeps.items():
            scores = [vz.analyse_froth({**base, k: x}, d)["score"] for x in xs]
            self.assertEqual(scores, sorted(scores), k)
        vix = [vz.analyse_froth({**base, "vix": x}, d)["score"] for x in (8, 10, 12, 14, 18, 25)]
        self.assertEqual(vix, sorted(vix, reverse=True))               # higher VIX = less complacent

    def test_yield_gap_percentile_inverts(self):
        lo = vz.analyse_froth({"yield_gap_percentile": 10}, date(2026, 11, 12))["score"]
        hi = vz.analyse_froth({"yield_gap_percentile": 90}, date(2026, 11, 12))["score"]
        self.assertAlmostEqual(lo, 90)
        self.assertAlmostEqual(hi, 10)

    def test_stale_warning(self):
        res = run_json(["froth", "--inputs", INPUTS_CSV, "--as-of", self.ASOF])
        comp = {c["key"]: c for c in res["components"]}
        self.assertTrue(comp["demat"]["stale"])
        self.assertEqual(comp["demat"]["age_days"], 73)
        self.assertFalse(comp["vix"]["stale"])
        self.assertTrue(any("stale input" in w and "New demat accounts" in w for w in res["warnings"]))
        dropped = run_json(["froth", "--inputs", INPUTS_CSV, "--as-of", self.ASOF, "--drop-stale"])
        self.assertNotIn("demat", {c["key"] for c in dropped["components"]})
        self.assertLess(dropped["coverage_pct"], res["coverage_pct"])
        tight = run_json(["froth", "--inputs", INPUTS_JSON, "--as-of", self.ASOF, "--max-age-days", "1"])
        self.assertTrue(all(c["stale"] for c in tight["components"]))

    def test_future_dated_input_rejected(self):
        code, _, err = run(["froth", "--inputs", INPUTS_JSON, "--as-of", "2026-11-01"])
        self.assertEqual(code, 2)
        self.assertIn("after the evaluation date", err)

    def test_flags_override_file_and_breadth_json(self):
        res = run_json(["froth", "--inputs", INPUTS_JSON, "--as-of", self.ASOF, "--vix", "20",
                        "--breadth-json", BREADTH, "--inputs-as-of", "2026-11-11"])
        comp = {c["key"]: c for c in res["components"]}
        self.assertEqual(comp["vix"]["sub_score"], 0)
        self.assertEqual(comp["vix"]["as_of"], "2026-11-11")
        self.assertAlmostEqual(comp["breadth"]["sub_score"], vz.pwl(72.5, vz.ANCHORS["breadth"]), places=3)
        self.assertIn("market-breadth", comp["breadth"]["source"])
        self.assertLess(res["score"], self.full["score"])

    def test_valuation_csv_fills_percentile(self):
        res = run_json(["froth", "--csv", PE, "--as-of", self.ASOF])
        comp = {c["key"]: c for c in res["components"]}
        pe = run_json(["pe", "--csv", PE])["metrics"]["pe"]["headline"]["richness_percentile"]
        self.assertAlmostEqual(comp["valuation"]["sub_score"], pe, places=3)
        self.assertEqual(comp["valuation"]["as_of"], "2026-10-30")

    def test_bad_inputs_files(self):
        with tempfile.TemporaryDirectory() as t:
            cases = {
                "unknown.json": '{"inputs": {"vixx": 12}}',
                "range.json": '{"inputs": {"vix": 0.5}}',
                "text.json": '{"inputs": {"vix": "twelve"}}',
                "notjson.json": '{"inputs": ',
                "baddate.json": '{"inputs": {"vix": {"value": 12, "as_of": "05/10/2026"}}}',
                "dup.csv": "input,value,as_of\nvix,12,2026-11-10\nvix,13,2026-11-10\n",
                "badnum.csv": "input,value,as_of\nmtf_yoy,4O,2026-11-10\n",
            }
            for name, text in cases.items():
                p = Path(t) / name
                p.write_text(text)
                with self.subTest(file=name):
                    code, out, err = run(["froth", "--inputs", str(p), "--as-of", self.ASOF])
                    self.assertEqual(code, 2)
                    self.assertEqual(out, "")
                    self.assertEqual(len(err.strip().splitlines()), 1)
                    self.assertTrue(err.startswith("error:"), err)

    def test_text_output(self):
        code, out, _ = run(["froth", "--inputs", INPUTS_CSV, "--as-of", self.ASOF])
        self.assertEqual(code, 0)
        self.assertIn("Missing (not scored):", out)
        self.assertIn("Posture for", out)
        self.assertIn("⚠ stale input", out)
        self.assertIn("Educational analysis, not investment advice.", out)


class TestErrorsAndJson(unittest.TestCase):
    def test_json_is_strict_everywhere(self):
        for argv in (["pe", "--csv", PE], ["pe", "--csv", TINY], ["yieldgap", "--csv", PE, "--yields", GSEC],
                     ["mcap-gdp", "--csv", MCAP], ["froth", "--inputs", INPUTS_JSON, "--as-of", "2026-11-12"]):
            with self.subTest(cmd=argv[0]):
                res = run_json(argv)
                self.assertEqual(res["command"], argv[0])

    def test_ambiguous_dates_exit_2(self):
        code, out, err = run(["pe", "--csv", AMBIG])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("ambiguous", err)
        self.assertEqual(run(["pe", "--csv", AMBIG, "--date-format", "dmy"])[0], 0)

    def test_bad_number_exit_2(self):
        code, _, err = run(["pe", "--csv", BADNUM])
        self.assertEqual(code, 2)
        self.assertIn("not a number", err)
        self.assertIn("'abc'", err)

    def test_misc_errors(self):
        cases = [["pe", "--csv", "missing.csv"], ["pe", "--csv", PE, "--since", "2030-01-01"],
                 ["pe", "--csv", PE, "--as-of", "13/13/2026"], ["pe", "--csv", PE, "--windows", "x"],
                 ["froth"], ["froth", "--vix", "abc"], ["nope"], []]
        for argv in cases:
            with self.subTest(argv=argv):
                code, out, err = run(argv)
                self.assertEqual(code, 2)
                self.assertTrue(err.startswith("error:"), err)
                self.assertEqual(len(err.strip().splitlines()), 1, err)
                self.assertNotIn("Traceback", err)


if __name__ == "__main__":
    unittest.main()
