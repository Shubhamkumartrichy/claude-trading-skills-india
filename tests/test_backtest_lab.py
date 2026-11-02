import contextlib
import copy
import io
import json
import math
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from _load import FIXTURES, load

bt = load("backtest-lab", "backtest")
ta = load("technical-analysis", "ta")
charges = load("trading-costs", "charges")
discipline = load("trading-discipline", "discipline")

TINY = str(FIXTURES / "backtest_tiny.csv")
TREND = str(FIXTURES / "backtest_trend.csv")
BENCH = str(FIXTURES / "backtest_bench.csv")
NULLS = str(FIXTURES / "backtest_candles_nulls.json")
DONCHIAN = ["entry_n=5", "exit_n=3", "stop_pct=5"]


def cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = bt.main(argv)
    return rc, out.getvalue(), err.getvalue()


def strict_json(text):
    def bad(token):
        raise ValueError(f"non-finite number {token} in JSON")
    return json.loads(text, parse_constant=bad)


def bars(spec, start=date(2026, 1, 5)):
    """Rows from (open, high, low, close) tuples on consecutive weekdays."""
    rows, d = [], start
    for o, h, lo, c in spec:
        while d.weekday() >= 5:
            d += timedelta(days=1)
        rows.append({"date": d, "open": o, "high": h, "low": lo, "close": c, "volume": 1000.0})
        d += timedelta(days=1)
    return rows


FLAT = [(100, 101, 99, 100)] * 20
SIGNAL = (100, 105.5, 99.5, 105)          # close 105 clears the prior 5-bar high (101)


def backtest(rows, strategy="donchian-breakout", params=DONCHIAN, **cfg):
    kw = {"capital": 100_000, "slippage_bps": 0}
    kw.update(cfg)
    s = bt.Series(rows, "test")          # direct: the CLI's 30-bar minimum would reject these tiny series
    p = bt.make_params(strategy, list(params))
    res, m = bt.run_one(s, strategy, p, bt.Config(**kw))
    return res, m, s


class TestParams(unittest.TestCase):
    def test_defaults_and_overrides(self):
        p = bt.make_params("sma-cross", ["fast=10", "slow=40,stop_atr=0"])
        self.assertEqual((p["fast"], p["slow"], p["stop_atr"]), (10, 40, 0.0))
        self.assertIsInstance(p["fast"], int)
        self.assertEqual(bt.make_params("52w-high")["lookback"], 252)

    def test_bad_params(self):
        for items in (["foo=1"], ["fast=abc"], ["fast=10.5"], ["fast=60", "slow=50"], ["slow=99999"],
                      ["fast"], ["target_r=2", "stop_atr=0"]):
            with self.subTest(items=items), self.assertRaises(bt.BacktestError):
                bt.make_params("sma-cross", items)
        with self.assertRaises(bt.BacktestError):
            bt.make_params("no-such-strategy")

    def test_grid_parsing(self):
        g = bt.parse_grid("sma-cross", ["fast=30,10,20,10", "slow=50:100:25"])
        self.assertEqual(g, {"fast": [10, 20, 30], "slow": [50, 75, 100]})
        for items in ([], ["fast"], ["fast=10", "fast=20"], ["nope=1"], ["fast=1:400:1", "slow=3:800:2"],
                      ["fast=5:1:1"]):
            with self.subTest(items=items), self.assertRaises(bt.BacktestError):
                bt.parse_grid("sma-cross", items)


class TestExecution(unittest.TestCase):
    """Hand-built series in tests/fixtures/backtest_tiny.csv (see the comments in the fixture generator):
    signal on bar 20, fill at bar 21's open 107, gap through the 5% stop on bar 24 (open 96);
    second signal on bar 30, fill 100 at bar 31, exit signal on bar 34, exit at bar 35's open 98.5."""

    @classmethod
    def setUpClass(cls):
        cls.rows = ta.load_csv(TINY)
        cls.res, cls.m, cls.s = backtest(cls.rows)

    def test_fills_next_open_not_signal_close(self):
        t1, t2 = self.res["trades"]
        self.assertEqual(t1["signal_date"], self.s.dates[20].isoformat())
        self.assertEqual(t1["entry_index"], 21)
        self.assertEqual(t1["entry_price"], 107.0)               # bar 21 open, not bar 20's close (105)
        self.assertNotEqual(t1["entry_price"], self.s.c[20])
        self.assertEqual(t2["entry_index"], 31)
        self.assertEqual(t2["entry_price"], 100.0)
        self.assertEqual((t2["exit_index"], t2["exit_reason"]), (35, "exit signal"))
        self.assertEqual(t2["exit_price"], 98.5)                 # bar 35 open, not bar 34's close (99)

    def test_gap_through_stop_fills_at_open(self):
        t1 = self.res["trades"][0]
        self.assertAlmostEqual(t1["stop"], 101.65)
        self.assertEqual((t1["exit_index"], t1["exit_reason"]), (24, "stop (gap)"))
        self.assertEqual(t1["exit_price"], 96.0)                 # the open, not the stop price

    def test_touched_stop_fills_at_stop(self):
        spec = FLAT + [SIGNAL, (107, 109, 106, 108), (108, 110, 107, 109), (104, 105, 100, 101)] + [(101, 102, 100, 101)] * 3
        t = backtest(bars(spec))[0]["trades"][0]
        self.assertEqual(t["exit_reason"], "stop")
        self.assertAlmostEqual(t["exit_price"], 101.65)

    def test_target_and_stop_priority(self):
        base = FLAT + [SIGNAL, (107, 109, 106, 108)]
        tail = [(110, 111, 109, 110)] * 3
        hit = backtest(bars(base + [(108, 113, 107, 112)] + tail), params=DONCHIAN + ["target_r=1"])[0]["trades"][0]
        self.assertEqual(hit["exit_reason"], "target")
        self.assertAlmostEqual(hit["exit_price"], 107 + 5.35)    # 1R above the fill
        both = backtest(bars(base + [(108, 113, 101, 109)] + tail), params=DONCHIAN + ["target_r=1"])[0]["trades"][0]
        self.assertEqual(both["exit_reason"], "stop")              # one bar hits both: assume the stop
        gap = backtest(bars(base + [(108, 109, 107, 108), (115, 116, 114, 115)] + tail),
                       params=DONCHIAN + ["target_r=1"])[0]["trades"][0]
        self.assertEqual((gap["exit_reason"], gap["exit_price"]), ("target (gap)", 115))

    def test_slippage_applied_to_both_fills(self):
        t = backtest(self.rows, slippage_bps=10)[0]["trades"][0]
        self.assertAlmostEqual(t["entry_price"], 107 * 1.001)
        self.assertAlmostEqual(t["exit_price"], 96 * 0.999)
        self.assertAlmostEqual(t["slippage"], t["qty"] * (107 * 0.001 + 96 * 0.001))

    def test_charges_match_trading_costs(self):
        t = self.res["trades"][0]
        ref = charges.calculate("delivery", 107.0, 96.0, t["qty"], broker="zerodha")
        self.assertAlmostEqual(t["charges"], ref["total_charges"], places=9)
        self.assertAlmostEqual(t["gross_pnl"], ref["gross_pnl"], places=9)
        self.assertAlmostEqual(t["net_pnl"], ref["net_pnl"], places=9)
        groww = backtest(self.rows, broker="groww")[0]["trades"][0]
        self.assertAlmostEqual(groww["charges"],
                               charges.calculate("delivery", 107.0, 96.0, groww["qty"], broker="groww")["total_charges"])
        self.assertAlmostEqual(self.res["final_equity"], 100_000 + sum(x["net_pnl"] for x in self.res["trades"]))

    def test_same_day_delivery_exit_charged_as_intraday(self):
        spec = FLAT + [SIGNAL, (107, 109, 101, 108)] + [(108, 109, 107, 108)] * 3
        t = backtest(bars(spec))[0]["trades"][0]
        self.assertEqual((t["entry_index"], t["exit_index"], t["charges_segment"]), (21, 21, "intraday"))
        self.assertAlmostEqual(t["charges"],
                               charges.calculate("intraday", 107, 101.65, t["qty"], broker="zerodha")["total_charges"])

    def test_sizing_whole_shares_cash_capped(self):
        t1 = self.res["trades"][0]
        self.assertEqual(t1["qty"], math.floor(100_000 * bt.CASH_BUFFER / 107))
        res = backtest(self.rows, sizing="fixed", size_value=20_000)[0]
        self.assertEqual(res["trades"][0]["qty"], math.floor(20_000 / 105))     # sized on the signal close
        risk = backtest(self.rows, sizing="risk", size_value=1.0)[0]["trades"][0]
        self.assertEqual(risk["qty"], math.floor(1_000 / (105 * 0.05)))

    def test_lots_and_futures_short(self):
        spec = FLAT + [(100, 100.5, 94.5, 95), (93, 94, 91, 92), (92, 92.5, 85, 86), (86, 86.5, 80, 81)]
        rows = bars(spec)
        self.assertEqual(backtest(rows, segment="futures", lot_size=50)[0]["trades"], [])   # longs only
        res = backtest(rows, segment="futures", lot_size=50, allow_short=True)[0]
        t = res["trades"][0]
        self.assertEqual((t["side"], t["entry_price"], t["qty"], t["lots"]), ("short", 93, 1050, 21))
        self.assertEqual((t["exit_reason"], t["exit_price"]), ("end of test", 81))
        self.assertAlmostEqual(t["gross_pnl"], (93 - 81) * 1050)
        self.assertAlmostEqual(t["charges"], charges.calculate("futures", 81, 93, 1050)["total_charges"])
        self.assertAlmostEqual(t["stop"], 93 * 1.05)

    def test_short_refused_outside_futures(self):
        for seg in ("delivery", "intraday"):
            with self.subTest(segment=seg), self.assertRaises(bt.BacktestError) as cm:
                bt.Config(segment=seg, allow_short=True)
            self.assertIn("futures", str(cm.exception))
        rc, _, err = cli(["run", "--csv", TINY, "--strategy", "sma-cross", "--allow-short"])
        self.assertEqual(rc, 2)
        self.assertIn("same session", err)

    def test_circuit_band_blocks_fill(self):
        spec = FLAT + [SIGNAL, (110.25, 111, 109, 110)] + [(110, 111, 109, 110)] * 3
        res, _, s = backtest(bars(spec), circuit_band=5.0)
        skip = [e for e in res["events"] if e["event"] == "entry skipped"]
        self.assertEqual(len(skip), 1)
        self.assertEqual(skip[0]["date"], s.dates[21].isoformat())
        self.assertIn("upper circuit", skip[0]["reason"])
        self.assertEqual(res["trades"][0]["entry_index"], 22)       # bar 21's close re-signals; bar 22 is unlocked
        self.assertEqual(backtest(bars(spec))[0]["trades"][0]["entry_price"], 110.25)   # no band: fills

    def test_circuit_locked_stop_waits(self):
        spec = FLAT + [SIGNAL, (107, 109, 106, 108), (108, 110, 107, 109), (98.1, 98.1, 98.1, 98.1),
                       (95, 97, 94, 96)] + [(96, 97, 95, 96)] * 3
        res = backtest(bars(spec), circuit_band=10.0)[0]
        t = res["trades"][0]
        self.assertEqual((t["exit_index"], t["exit_price"], t["exit_reason"]), (24, 95, "stop (after circuit)"))
        self.assertTrue(any(e["event"] == "stop delayed" for e in res["events"]))
        free = backtest(bars(spec))[0]["trades"][0]
        self.assertEqual((free["exit_index"], free["exit_price"]), (23, 98.1))

    def test_intraday_squares_off_same_bar(self):
        res = backtest(self.rows, segment="intraday")[0]
        for t in res["trades"]:
            self.assertEqual(t["entry_index"], t["exit_index"])
        self.assertEqual(res["trades"][0]["exit_reason"], "square-off (MIS)")
        self.assertEqual(res["trades"][0]["exit_price"], 108.0)

    def test_signals_and_trades_are_causal(self):
        rows = ta.load_csv(TREND)
        full = bt.Series(rows, "full")
        params = {"sma-cross": [], "ema-pullback": ["trend=50"], "rsi-reversion": ["trend=50"],
                  "donchian-breakout": [], "bollinger-reversion": [], "52w-high": ["lookback=100"]}
        for strat, items in params.items():
            p = bt.make_params(strat, items)
            sig_full = bt.build_signals(full, strat, p)
            for k in (260, 455):
                part = bt.Series(rows[:k], "part")
                sig_part = bt.build_signals(part, strat, p)
                for key in ("entry_long", "exit_long", "entry_short", "exit_short"):
                    with self.subTest(strategy=strat, k=k, key=key):
                        self.assertEqual(sig_part[key], sig_full[key][:k])
                cfg = bt.Config(segment="futures", allow_short=True)
                done = [t for t in bt.simulate(part, sig_part, p, cfg)["trades"] if t["exit_index"] < k - 1]
                all_t = bt.simulate(full, sig_full, p, cfg)["trades"][:len(done)]
                with self.subTest(strategy=strat, k=k, check="trades"):
                    self.assertEqual(done, all_t)

    def test_regime_filter_blocks_entries(self):
        rows = ta.load_csv(TREND)
        s = bt.build_series(rows, "trend")
        bench = ta.load_csv(BENCH)
        bt.attach_regime(s, bench, 50, "bench")
        p = bt.make_params("donchian-breakout")
        res = bt.simulate(s, bt.build_signals(s, "donchian-breakout", p), p, bt.Config())
        self.assertGreater(res["blocked_by_regime"], 0)
        closes = {r["date"].date(): r["close"] for r in bench}
        sma = dict(zip([r["date"].date() for r in bench], ta.sma([r["close"] for r in bench], 50)))
        for t in res["trades"]:
            d = date.fromisoformat(t["signal_date"])
            self.assertGreater(closes[d], sma[d])

    def test_null_candles_skipped(self):
        rc, out, _ = cli(["run", "--candles", NULLS, "--strategy", "sma-cross", "--params", "fast=5", "slow=10", "--json"])
        self.assertEqual(rc, 0)
        d = strict_json(out)
        self.assertEqual(d["metrics"]["bars"], 57)             # 61 entries: 3 null OHLC + 1 null candle
        self.assertTrue(any("Skipped 4 null" in n for n in d["notes"]))

    def test_split_warning(self):
        spec = [(100, 101, 99, 100)] * 30 + [(50, 51, 49, 50)] * 5
        s = bt.build_series(bars(spec), "split")
        self.assertTrue(any("split" in n for n in s.notes))


class TestMetrics(unittest.TestCase):
    def test_cagr(self):
        self.assertAlmostEqual(bt.cagr(100, 121, 2), 0.10)
        self.assertAlmostEqual(bt.cagr(100, 50, 1), -0.5)
        self.assertEqual(bt.cagr(100, 0, 1), -1.0)
        self.assertIsNone(bt.cagr(100, 110, 0))

    def test_drawdown_depth_and_duration(self):
        dd = bt.drawdown([100, 120, 90, 100, 130, 117, 140])
        self.assertAlmostEqual(dd["max_dd_pct"], -25.0)
        self.assertEqual((dd["peak"], dd["trough"], dd["recovery"]), (1, 2, 4))
        self.assertEqual(dd["longest_underwater"], 3)
        still = bt.drawdown([100, 110, 99, 105])
        self.assertIsNone(still["recovery"])
        self.assertEqual(still["longest_underwater"], 2)
        self.assertEqual(bt.drawdown([100, 101, 102])["max_dd_pct"], 0.0)

    def test_return_stats(self):
        r = bt.return_stats([100, 110, 99, 108.9], 1, 0.0)      # returns +10%, -10%, +10%
        self.assertAlmostEqual(r["sharpe"], (0.1 / 3) / math.sqrt(0.04 / 3), places=9)
        self.assertAlmostEqual(r["sortino"], (0.1 / 3) / math.sqrt(0.01 / 3), places=9)
        flat = bt.return_stats([100, 100, 100, 100], 252, 6.5)
        self.assertEqual((flat["sharpe"], flat["sortino"], flat["volatility_pct"]), (None, None, 0.0))

    def test_trade_stats_breakevens_and_profit_factor(self):
        pnl = [100.0, -50.0, 0.0, 0.004, -25.0, 75.0]
        trades = [{"net_pnl": x, "gross_pnl": x + 1, "charges": 1.0, "slippage": 0.0, "bars_held": 2,
                   "r_multiple": x / 50} for x in pnl]
        st = bt.trade_stats(trades)
        self.assertEqual((st["wins"], st["losses"], st["breakevens"]), (2, 2, 2))
        self.assertAlmostEqual(st["win_rate_pct"], 100 * 2 / 6)
        self.assertAlmostEqual(st["profit_factor"], 175 / 75)
        self.assertAlmostEqual(st["avg_win"], 87.5)
        self.assertAlmostEqual(st["avg_loss"], -37.5)
        self.assertAlmostEqual(st["expectancy"], sum(pnl) / 6)
        self.assertEqual(st["longest_losing_streak"], 2)          # breakevens do not break the streak
        self.assertAlmostEqual(st["charges_pct_of_gross_profit"], 6 / (101 + 1 + 1.004 + 76) * 100)
        self.assertIsNone(bt.trade_stats(trades[:1])["profit_factor"])            # no losses: undefined

    def test_run_metrics_consistent(self):
        res, m, s = backtest(ta.load_csv(TINY))
        years = (s.dates[-1] - s.dates[0]).days / 365.25
        self.assertAlmostEqual(m["cagr_pct"], bt.cagr(100_000, m["final_equity"], years) * 100)
        self.assertAlmostEqual(m["total_return_pct"], (m["final_equity"] / 100_000 - 1) * 100)
        self.assertEqual(m["trades"], 2)
        self.assertEqual(m["losses"], 2)
        self.assertAlmostEqual(m["exposure_pct"], (4 + 5) / 40 * 100)          # bars 21-24 and 31-35

    def test_percentile_and_luck_hurdle(self):
        xs = [1, 2, 3, 4, 5]
        self.assertEqual(bt.percentile(xs, 50), 3)
        self.assertEqual(bt.percentile(xs, 25), 2)
        self.assertAlmostEqual(bt.percentile(xs, 90), 4.6)
        self.assertEqual(bt.expected_max_z(1), 0.0)
        self.assertLess(bt.expected_max_z(10), bt.expected_max_z(100))

    def test_tax_estimate(self):
        tr = [{"entry_date": "2026-05-01", "exit_date": "2026-06-01", "net_pnl": 100_000.0},     # STCG
              {"entry_date": "2025-04-01", "exit_date": "2026-06-15", "net_pnl": 200_000.0},     # LTCG
              {"entry_date": "2026-07-01", "exit_date": "2026-07-01", "net_pnl": 10_000.0},      # same day
              {"entry_date": "2027-05-01", "exit_date": "2027-06-01", "net_pnl": -50_000.0},
              {"entry_date": "2026-01-01", "exit_date": "2027-07-01", "net_pnl": 300_000.0}]
        t = bt.tax_estimate(tr, "delivery", 30, 1_000_000, 2.0)
        by = {r["fy"]: r["tax"] for r in t["by_fy"]}
        self.assertAlmostEqual(by["2026-27"], (20_000 + 75_000 * 0.125 + 3_000) * 1.04)
        self.assertAlmostEqual(by["2027-28"], 125_000 * 0.125 * 1.04)   # ST loss set off against LTCG first
        fut = bt.tax_estimate(tr[:1], "futures", 30, 1_000_000, 1.0)
        self.assertAlmostEqual(fut["total_tax"], 100_000 * 0.30 * 1.04)


class TestSweep(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s = bt.build_series(ta.load_csv(TREND), "trend")
        cls.grid = bt.parse_grid("sma-cross", ["fast=5,10,20", "slow=30,50"])
        cls.res = bt.sweep(cls.s, "sma-cross", bt.make_params("sma-cross"), cls.grid, bt.Config(), "sharpe",
                           min_trades=3)

    def test_grid_is_complete(self):
        cells = self.res["cells"]
        self.assertEqual(len(cells), 6)
        self.assertEqual(sorted((c["params"]["fast"], c["params"]["slow"]) for c in cells),
                         [(f, sl) for f in (5, 10, 20) for sl in (30, 50)])
        self.assertTrue(all(c["valid"] for c in cells))

    def test_best_and_plateau_stats(self):
        rb = self.res["robustness"]
        elig = [c for c in self.res["cells"] if c["score"] is not None and c["trades"] >= 3]
        self.assertEqual(rb["best"]["score"], max(c["score"] for c in elig))
        for key in ("neighbours", "neighbour_median", "grid_median", "verdict", "luck_hurdle_sharpe"):
            self.assertIn(key, rb)
        self.assertGreater(rb["neighbours"], 0)

    def test_plateau_vs_spike(self):
        def cells(scores):
            return [{"idx": [i, j], "params": {"a": i, "b": j}, "valid": True, "score": scores[i][j], "trades": 20,
                     "total_return_pct": scores[i][j], "years": 4.0} for i in range(3) for j in range(3)]
        flat = bt.robustness(cells([[1.0, 1.0, 0.9], [1.0, 1.1, 1.0], [0.9, 1.0, 1.0]]), "sharpe", 5)
        self.assertEqual(flat["best"]["params"], {"a": 1, "b": 1})
        self.assertEqual(flat["neighbours"], 8)
        self.assertAlmostEqual(flat["neighbour_median"], 1.0)
        self.assertTrue(flat["verdict"].startswith("plateau"))
        spike = bt.robustness(cells([[0.1, 0.2, 0.1], [0.2, 2.0, 0.1], [0.1, 0.2, 0.1]]), "sharpe", 5)
        self.assertTrue(spike["verdict"].startswith("spike"))
        corner = bt.robustness(cells([[3.0, 1.0, 0.0], [1.0, 1.0, 0.0], [0, 0, 0]]), "sharpe", 5)
        self.assertEqual(corner["neighbours"], 3)
        self.assertTrue(bt.robustness(cells([[-1.0] * 3] * 3), "sharpe", 5)["verdict"].startswith("no edge"))
        lone = bt.robustness([cells([[1.0] * 3] * 3)[0]], "sharpe", 5)
        self.assertTrue(lone["verdict"].startswith("isolated"))

    def test_cli_sweep_json(self):
        rc, out, _ = cli(["sweep", "--csv", TREND, "--strategy", "sma-cross", "--grid", "fast=5,10,20",
                          "slow=30,50", "--min-trades", "3", "--json"])
        self.assertEqual(rc, 0)
        d = strict_json(out)
        self.assertEqual(d["grid"], {"fast": [5, 10, 20], "slow": [30, 50]})
        self.assertEqual(len(d["cells"]), 6)
        self.assertEqual(d["robustness"]["best"]["params"], self.res["robustness"]["best"]["params"])
        rc, out, _ = cli(["sweep", "--csv", TREND, "--strategy", "sma-cross", "--grid", "fast=5,10", "slow=30,50"])
        self.assertEqual(rc, 0)
        self.assertIn("verdict:", out)
        # base fast=60 clashes with the default slow=50, but every grid cell overrides slow: allowed
        rc, out, _ = cli(["sweep", "--csv", TREND, "--strategy", "sma-cross", "--params", "fast=60",
                          "--grid", "slow=100,150", "--min-trades", "1", "--json"])
        self.assertEqual(rc, 0)
        self.assertTrue(all(c["valid"] for c in strict_json(out)["cells"]))


class TestWalkForward(unittest.TestCase):
    GRID = ["fast=5,10,20", "slow=30,50"]

    def wf(self, rows):
        s = bt.build_series(rows, "trend")
        grid = bt.parse_grid("sma-cross", self.GRID)
        return s, grid, bt.walkforward(s, "sma-cross", bt.make_params("sma-cross"), grid, bt.Config(), "sharpe",
                                       is_bars=250, oos_bars=100, min_trades=2)

    @classmethod
    def setUpClass(cls):
        cls.rows = ta.load_csv(TREND)

    def test_windows_do_not_overlap(self):
        _, _, r = self.wf(self.rows)
        ws = r["windows"]
        self.assertEqual([w["oos_range"] for w in ws], [[250, 350], [350, 450], [450, 550], [550, 640]])
        for w in ws:
            self.assertEqual(w["is_range"][1], w["oos_range"][0])
            self.assertEqual(w["is_range"][1] - w["is_range"][0], 250)
        for a, b in zip(ws, ws[1:]):
            self.assertEqual(a["oos_range"][1], b["oos_range"][0])
        self.assertEqual(r["oos"]["bars"], 390)
        self.assertEqual(r["oos"]["start_equity"], 1_000_000)

    def test_oos_uses_in_sample_choice_only(self):
        s, grid, r = self.wf(self.rows)
        for w in r["windows"]:
            a, b = w["is_range"]
            sw = bt.sweep(s, "sma-cross", bt.make_params("sma-cross"), grid, bt.Config(), "sharpe", a, b, 2)
            best = sw["robustness"]["best"]
            self.assertEqual(w["params"], best["params"] if best else None)
        # Rewrite everything after the first OOS start: the first window's choice must not move.
        cut = r["windows"][0]["oos_range"][0]
        rows2 = copy.deepcopy(self.rows)
        for i in range(cut, len(rows2)):
            f = 1 + 0.2 * math.sin(i)
            for k in ("open", "high", "low", "close"):
                rows2[i][k] *= f
        _, _, r2 = self.wf(rows2)
        self.assertEqual(r2["windows"][0]["params"], r["windows"][0]["params"])
        self.assertEqual(r2["windows"][0]["is_score"], r["windows"][0]["is_score"])

    def test_cli_walkforward(self):
        rc, out, _ = cli(["walkforward", "--csv", TREND, "--strategy", "sma-cross", "--grid"] + self.GRID +
                         ["--is-bars", "250", "--oos-bars", "100", "--min-trades", "2", "--json"])
        self.assertEqual(rc, 0)
        d = strict_json(out)
        self.assertEqual(len(d["windows"]), 4)
        self.assertIn("efficiency", d["is_vs_oos"])
        rc, _, err = cli(["walkforward", "--csv", TREND, "--strategy", "sma-cross", "--grid", "fast=5,10",
                          "--is-bars", "700"])
        self.assertEqual(rc, 2)
        self.assertIn("error:", err)


class TestMonteCarlo(unittest.TestCase):
    RETS = [0.05, -0.02, 0.03, -0.04, 0.06, 0.01, -0.01, 0.02, -0.03, 0.04]

    def test_deterministic_with_seed_and_ordered(self):
        a = bt.monte_carlo(self.RETS, 100_000, 500, seed=11, years=2.0)
        b = bt.monte_carlo(self.RETS, 100_000, 500, seed=11, years=2.0)
        self.assertEqual(a, b)
        c = bt.monte_carlo(self.RETS, 100_000, 500, seed=12, years=2.0)
        self.assertNotEqual(a["final_equity"], c["final_equity"])
        for key in ("max_drawdown_pct", "final_equity", "cagr_pct"):
            v = a[key]
            self.assertLessEqual(v["p5"], v["p50"])
            self.assertLessEqual(v["p50"], v["p95"])
        self.assertTrue(0 <= a["risk_of_ruin_pct"] <= 100)
        self.assertAlmostEqual(a["actual"]["final_equity"], 100_000 * math.prod(1 + r for r in self.RETS))

    def test_shuffle_keeps_final_equity(self):
        r = bt.monte_carlo(self.RETS, 100_000, 200, seed=3, method="shuffle")
        self.assertAlmostEqual(r["final_equity"]["p5"], r["final_equity"]["p95"], places=6)
        self.assertGreaterEqual(r["max_drawdown_pct"]["p95"], r["actual"]["max_drawdown_pct"] - 1e-9)

    def test_ruin_and_guards(self):
        r = bt.monte_carlo([-0.2] * 6, 100_000, 50, seed=1, ruin_dd=30)
        self.assertEqual(r["risk_of_ruin_pct"], 100.0)
        with self.assertRaises(bt.BacktestError):
            bt.monte_carlo(self.RETS[:4], 100_000)
        with self.assertRaises(bt.BacktestError):
            bt.monte_carlo(self.RETS, 100_000, method="magic")

    def test_cli_from_backtest_and_trades_csv(self):
        argv = ["montecarlo", "--csv", TREND, "--strategy", "rsi-reversion", "--params", "trend=50",
                "--sims", "300", "--seed", "5", "--json"]
        rc, out, _ = cli(argv)
        self.assertEqual(rc, 0)
        d = strict_json(out)
        self.assertEqual(d["montecarlo"]["sims"], 300)
        self.assertEqual(cli(argv)[1], out)                              # same seed, same output
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "trades.csv")
            rc, _, _ = cli(["run", "--csv", TREND, "--strategy", "rsi-reversion", "--params", "trend=50",
                            "--trades-csv", path, "--json"])
            self.assertEqual(rc, 0)
            rc, out, _ = cli(["montecarlo", "--from-trades", path, "--seed", "5", "--json"])
            self.assertEqual(rc, 0)
            self.assertEqual(strict_json(out)["montecarlo"]["trades"], d["montecarlo"]["trades"])


class TestCLI(unittest.TestCase):
    def test_run_json_is_strict(self):
        rc, out, _ = cli(["run", "--csv", TINY, "--strategy", "donchian-breakout", "--params"] + DONCHIAN +
                         ["--slippage-bps", "0", "--capital", "100000", "--tax", "--json"])
        self.assertEqual(rc, 0)
        d = strict_json(out)
        self.assertEqual([t["entry_price"] for t in d["trades"]], [107.0, 100.0])
        self.assertEqual(d["metrics"]["trades"], 2)
        self.assertIn("buy_hold", d)
        self.assertIn("tax", d)
        self.assertIsNone(d["metrics"]["profit_factor"] if d["metrics"]["losses"] == 0 else None)

    def test_text_run_and_strategies(self):
        rc, out, _ = cli(["run", "--csv", TREND, "--strategy", "52w-high", "--params", "lookback=100"])
        self.assertEqual(rc, 0)
        for s in ("Buy & hold", "Profit factor", "Exposure", "Costs: charges"):
            self.assertIn(s, out)
        rc, out, _ = cli(["strategies", "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(set(strict_json(out)), set(bt.STRATEGIES))

    def test_trades_csv_reads_as_discipline_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "t.csv")
            rc, out, _ = cli(["run", "--csv", TREND, "--strategy", "sma-cross", "--params", "fast=10", "slow=40",
                              "--trades-csv", path, "--json"])
            self.assertEqual(rc, 0)
            n = len(strict_json(out)["trades"])
            journal = discipline.load_journal(path)
            self.assertEqual(len(journal), n)
            self.assertTrue(all(t["status"] == "closed" and t["setup"] == "sma-cross" for t in journal))

    def test_bad_input_exits_cleanly(self):
        cases = [
            ["run", "--csv", "/nonexistent/x.csv", "--strategy", "sma-cross"],
            ["run", "--csv", TREND, "--strategy", "sma-cross", "--params", "fast=50", "slow=20"],
            ["run", "--csv", TREND, "--strategy", "sma-cross", "--params", "bogus=1"],
            ["run", "--csv", TREND, "--strategy", "sma-cross", "--leverage", "2"],
            ["run", "--csv", TREND, "--strategy", "sma-cross", "--risk-pct", "1", "--params", "stop_atr=0"],
            ["run", "--csv", TREND, "--strategy", "sma-cross", "--circuit-band", "7"],
            ["run", "--csv", TREND, "--strategy", "52w-high", "--params", "lookback=700"],
            ["run", "--csv", TINY, "--strategy", "sma-cross", "--start", "2031-01-01"],
            ["run", "--strategy", "sma-cross"],
            ["sweep", "--csv", TREND, "--strategy", "sma-cross", "--grid", "fast"],
            ["montecarlo", "--csv", TINY, "--strategy", "donchian-breakout", "--params"] + DONCHIAN,
            ["montecarlo", "--from-trades", "/nonexistent/t.csv"],
            ["montecarlo", "--csv", TREND],
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                rc, out, err = cli(argv)
                self.assertEqual(rc, 2)
                self.assertTrue(err.startswith("error:"), err)
                self.assertEqual(len(err.strip().splitlines()), 1)
                self.assertNotIn("Traceback", err)
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(io.StringIO()):
            bt.main(["run", "--csv", TREND])                               # argparse: --strategy missing
        self.assertEqual(cm.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
