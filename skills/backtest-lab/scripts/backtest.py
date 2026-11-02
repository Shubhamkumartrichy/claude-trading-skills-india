#!/usr/bin/env python3
"""Backtest lab for rule-based swing ideas on NSE/BSE daily bars, with Indian costs built in.

Zero dependencies (Python 3.10+ stdlib). Optional: `pip install yfinance` for --symbol.
Re-uses technical-analysis/ta.py (loaders, indicators), trading-costs/charges.py (charges per
round trip) and indian-tax-for-traders/tax.py (capital-gains rates for the --tax estimate).

How orders are simulated (one instrument, one position at a time)
  * A strategy decides on a bar's CLOSE; the order fills at the NEXT bar's OPEN. Never the same close.
  * Stops and targets rest in the market. A gap beyond the level fills at the open; when one bar
    touches both, the stop is assumed to have filled first.
  * Every fill gives up --slippage-bps. Every round trip pays the exact charges for --segment and
    --broker. A delivery buy that is sold the same day is charged as intraday.
  * --segment intraday turns each signal into a next-session open-to-close trade (MIS square-off).
  * Shorts need --segment futures plus --allow-short: an Indian cash-market short has to be
    bought back the same session, so a multi-day short only exists as a futures position.
  * --circuit-band: an open sitting at the band limit gets no fill (the order would wait in the queue).

Sub-commands
  run          one backtest: metrics vs buy-and-hold, yearly table, trade list, red flags
  sweep        parameter grid plus a plateau-or-spike robustness read
  walkforward  rolling in-sample optimisation, out-of-sample test, stitched out-of-sample equity
  montecarlo   resample the trade sequence: drawdown and final-equity percentiles, risk of ruin
  strategies   list the built-in strategies and their parameters

Examples
  python3 backtest.py run --csv tcs.csv --strategy sma-cross --params fast=20 slow=50
  python3 backtest.py run --symbol POLYCAB --strategy 52w-high --risk-pct 1 --tax --trades-csv t.csv
  python3 backtest.py sweep --csv tcs.csv --strategy donchian-breakout --grid entry_n=20,40,55 exit_n=10,20
  python3 backtest.py walkforward --csv nifty.csv --strategy sma-cross --grid fast=10,20,30 \
      slow=50,100,200 --segment futures --lot-size 65 --allow-short
  python3 backtest.py montecarlo --csv tcs.csv --strategy ema-pullback --sims 2000 --seed 7
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import itertools
import json
import math
import random
import statistics
import sys
from datetime import date, datetime
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parents[2]


def _sibling(skill: str, script: str):
    name = f"_backtest_lab_dep_{script}"
    if name in sys.modules:
        return sys.modules[name]
    path = SKILLS_DIR / skill / "scripts" / f"{script}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


ta = _sibling("technical-analysis", "ta")
charges = _sibling("trading-costs", "charges")
taxmod = _sibling("indian-tax-for-traders", "tax")


class BacktestError(ValueError):
    """Bad input or an impossible set-up; the CLI prints it as one `error:` line and exits 2."""


SEGMENTS = ("delivery", "intraday", "futures")
CIRCUIT_BANDS = (2.0, 5.0, 10.0, 20.0)
ATR_N = 14
EPS = 0.005            # |net P&L| under half a paisa is a breakeven: neither a win nor a loss
CASH_BUFFER = 0.997    # a full-size cash buy leaves ~0.3% of equity for its own charges
MAX_CELLS = 400
MIN_BARS = 30
METRICS = ("sharpe", "sortino", "cagr", "calmar", "total_return", "profit_factor", "expectancy_r")
METRIC_KEY = {"sharpe": "sharpe", "sortino": "sortino", "cagr": "cagr_pct", "calmar": "calmar",
              "total_return": "total_return_pct", "profit_factor": "profit_factor",
              "expectancy_r": "expectancy_r"}

# ---------------------------------------------------------------------------
# Strategy library: fixed rules with typed, bounded parameters. Nothing is evaluated from input.
# ---------------------------------------------------------------------------
PARAM_TYPES = {
    "fast": (int, 2, 500), "slow": (int, 3, 800), "ema": (int, 2, 400), "trend": (int, 0, 800),
    "period": (int, 2, 200), "buy_below": (float, 1, 99), "sell_above": (float, 1, 99),
    "entry_n": (int, 2, 600), "exit_n": (int, 2, 600), "k": (float, 0.5, 5.0),
    "lookback": (int, 20, 800), "exit_ma": (int, 2, 400),
    "stop_atr": (float, 0, 20), "stop_pct": (float, 0, 50), "target_r": (float, 0, 50),
    "max_hold": (int, 0, 5000),
}
RISK = {"stop_atr": 0.0, "stop_pct": 0.0, "target_r": 0.0, "max_hold": 0}

STRATEGIES = {
    "sma-cross": {
        "idea": "Trend following. Long when the fast SMA crosses above the slow SMA; out once it is back below.",
        "defaults": {"fast": 20, "slow": 50, **RISK, "stop_atr": 3.0},
    },
    "ema-pullback": {
        "idea": "Buy a dip that tags the EMA and closes back above it, only while price is above a rising "
                "trend EMA. Exit at the R target, on the time limit, or on a close under the trend EMA.",
        "defaults": {"ema": 20, "trend": 100, **RISK, "stop_atr": 2.0, "target_r": 2.0, "max_hold": 20},
    },
    "rsi-reversion": {
        "idea": "Mean reversion. Buy when a short RSI is washed out (below buy_below) while price is above "
                "the trend SMA (trend=0 switches the filter off); sell when RSI recovers above sell_above.",
        "defaults": {"period": 2, "buy_below": 10.0, "sell_above": 70.0, "trend": 200, **RISK,
                     "stop_atr": 3.0, "max_hold": 10},
    },
    "donchian-breakout": {
        "idea": "Channel breakout. Buy a close above the prior entry_n-bar high; exit a close under the prior "
                "exit_n-bar low; ATR stop.",
        "defaults": {"entry_n": 20, "exit_n": 10, **RISK, "stop_atr": 2.0},
    },
    "bollinger-reversion": {
        "idea": "Buy a close below the lower Bollinger band (period, k), exit at the middle band. Optional "
                "SMA trend filter (trend=0 is off).",
        "defaults": {"period": 20, "k": 2.0, "trend": 0, **RISK, "stop_atr": 3.0, "max_hold": 20},
    },
    "52w-high": {
        "idea": "Momentum. Buy a close above the prior lookback-bar high (252 = one year); exit a close under "
                "the exit SMA.",
        "defaults": {"lookback": 252, "exit_ma": 50, **RISK, "stop_atr": 2.5},
    },
}


def coerce(key: str, raw) -> float | int:
    typ, lo, hi = PARAM_TYPES[key]
    try:
        v = float(raw)
    except (TypeError, ValueError):
        raise BacktestError(f"parameter {key}={raw!r} is not a number") from None
    if not math.isfinite(v):
        raise BacktestError(f"parameter {key} must be finite")
    if typ is int:
        if abs(v - round(v)) > 1e-9:
            raise BacktestError(f"parameter {key} must be a whole number, got {raw}")
        v = int(round(v))
    if not lo <= v <= hi:
        raise BacktestError(f"parameter {key} must be between {lo:g} and {hi:g}, got {raw}")
    return v


def check_params(strategy: str, p: dict) -> str | None:
    if strategy == "sma-cross" and p["fast"] >= p["slow"]:
        return "fast must be shorter than slow"
    if strategy == "ema-pullback" and p["ema"] >= p["trend"]:
        return "ema must be shorter than trend"
    if strategy == "rsi-reversion" and p["buy_below"] >= p["sell_above"]:
        return "buy_below must be below sell_above"
    if p["target_r"] > 0 and p["stop_atr"] <= 0 and p["stop_pct"] <= 0:
        return "target_r is measured in R, so it needs a stop (stop_atr or stop_pct > 0)"
    return None


def _strategy(name: str) -> dict:
    if name not in STRATEGIES:
        raise BacktestError(f"unknown strategy '{name}'. Built in: {', '.join(STRATEGIES)}")
    return STRATEGIES[name]


def make_params(strategy: str, items=None, check: bool = True) -> dict:
    """Defaults overridden by KEY=VALUE tokens ("fast=10", "slow=50" or "fast=10,slow=50").
    check=False skips the cross-parameter rules (a sweep validates each grid cell instead)."""
    p = dict(_strategy(strategy)["defaults"])
    for item in items or []:
        for part in str(item).split(","):
            if not part.strip():
                continue
            key, sep, raw = part.partition("=")
            key = key.strip().replace("-", "_")
            if not sep:
                raise BacktestError(f"--params takes KEY=VALUE, got {part!r}")
            if key not in p:
                raise BacktestError(f"{strategy} has no parameter '{key}' (it has: {', '.join(p)})")
            p[key] = coerce(key, raw.strip())
    err = check_params(strategy, p) if check else None
    if err:
        raise BacktestError(f"{strategy}: {err}")
    return p


def parse_grid(strategy: str, items) -> dict:
    """KEY=v1,v2,... or KEY=start:stop:step (inclusive). Values are sorted so grid neighbours are
    neighbours in parameter space."""
    if not items:
        raise BacktestError("give --grid KEY=V1,V2,... (e.g. --grid fast=10,20,30 slow=50,100)")
    defaults = _strategy(strategy)["defaults"]
    grid = {}
    for item in items:
        key, sep, raw = str(item).partition("=")
        key = key.strip().replace("-", "_")
        if not sep or not raw.strip():
            raise BacktestError(f"--grid takes KEY=V1,V2,... got {item!r}")
        if key not in defaults:
            raise BacktestError(f"{strategy} has no parameter '{key}' (it has: {', '.join(defaults)})")
        if key in grid:
            raise BacktestError(f"--grid lists '{key}' twice")
        vals = []
        for tok in raw.split(","):
            tok = tok.strip()
            if not tok:
                continue
            if ":" in tok:
                bits = tok.split(":")
                try:
                    a, b, step = (float(x) for x in bits)
                except ValueError:
                    raise BacktestError(f"--grid range must be start:stop:step, got {tok!r}") from None
                if len(bits) != 3 or step <= 0 or b < a or (b - a) / step > 200:
                    raise BacktestError(f"--grid range {tok!r} must be start:stop:step with step > 0, ≤ 200 values")
                x = a
                while x <= b + 1e-9:
                    vals.append(coerce(key, round(x, 10)))
                    x += step
            else:
                vals.append(coerce(key, tok))
        if not vals:
            raise BacktestError(f"--grid {key} has no values")
        grid[key] = sorted(set(vals))
    cells = math.prod(len(v) for v in grid.values())
    if cells > MAX_CELLS:
        raise BacktestError(f"grid has {cells} cells; keep it under {MAX_CELLS} (fewer cells = fewer lucky winners)")
    return grid


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------
class Series:
    """Daily bars plus an indicator / signal cache shared by every simulation on the same data."""

    def __init__(self, rows: list, name: str = "data"):
        self.name = name
        self.dates = [r["date"].date() if isinstance(r["date"], datetime) else r["date"] for r in rows]
        self.o = [float(r["open"]) for r in rows]
        self.h = [float(r["high"]) for r in rows]
        self.l = [float(r["low"]) for r in rows]
        self.c = [float(r["close"]) for r in rows]
        self.n = len(rows)
        self.regime = None          # [(long_ok, short_ok)] per bar when a regime filter is attached
        self.regime_label = None
        self.notes: list = []
        self._ind: dict = {}
        self._sig: dict = {}

    def ind(self, kind: str, n: int, k: float | None = None):
        key = (kind, n, k)
        if key not in self._ind:
            if kind == "sma":
                v = ta.sma(self.c, n)
            elif kind == "ema":
                v = ta.ema(self.c, n)
            elif kind == "rsi":
                v = ta.rsi(self.c, n)
            elif kind == "atr":
                v = ta.atr(self.h, self.l, self.c, n)
            elif kind == "boll":
                v = ta.bollinger(self.c, n, k)
            elif kind == "hh":          # highest high of the n bars BEFORE this one
                v = [None] * n + [max(self.h[i - n:i]) for i in range(n, self.n)]
            elif kind == "ll":
                v = [None] * n + [min(self.l[i - n:i]) for i in range(n, self.n)]
            else:
                raise KeyError(kind)
            self._ind[key] = v
        return self._ind[key]


def load_candles(path: str) -> tuple[list, int]:
    """Broker-MCP candle JSON ([[ts,o,h,l,c,v],...], [{...}], column arrays, or wrapped in
    candles/data/result/payload). Null candles are skipped and counted."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    for _ in range(4):
        if not isinstance(data, dict):
            break
        key = next((k for k in ("candles", "data", "result", "payload") if k in data), None)
        if key is None:
            break
        data = data[key]
    if isinstance(data, dict):
        low = {k.lower(): v for k, v in data.items()}
        tkey = next((k for k in ("timestamp", "time", "date", "datetime") if k in low), None)
        if not tkey or not isinstance(low.get("open"), list):
            raise BacktestError("unrecognised candle JSON: expected a list of candles or column arrays")
        vol = low.get("volume") or [0] * len(low["open"])
        data = [list(t) for t in zip(low[tkey], low["open"], low["high"], low["low"], low["close"], vol)]
    if not isinstance(data, list):
        raise BacktestError("unrecognised candle JSON: expected a list of candles")
    rows, skipped = [], 0
    for cnd in data:
        try:
            if isinstance(cnd, (list, tuple)):
                ts, o, h, lo, c = cnd[:5]
            elif isinstance(cnd, dict):
                d = {k.lower(): v for k, v in cnd.items()}
                ts = next((d[k] for k in ("date", "timestamp", "time", "datetime") if d.get(k) is not None), None)
                o, h, lo, c = d.get("open"), d.get("high"), d.get("low"), d.get("close")
            else:
                skipped += 1
                continue
            if ts is None or any(x is None for x in (o, h, lo, c)):
                skipped += 1
                continue
            rows.append({"date": ta.parse_ts(ts), "open": float(o), "high": float(h), "low": float(lo),
                         "close": float(c), "volume": 0.0})
        except (TypeError, ValueError):
            skipped += 1
    return ta._clean(rows), skipped


def sanitize(rows: list) -> tuple[list, int, int]:
    """Drop bars with missing/non-positive prices; widen high/low that do not contain open/close."""
    out, dropped, repaired = [], 0, 0
    for r in rows:
        vals = [r.get("open"), r.get("high"), r.get("low"), r.get("close")]
        if any(v is None or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0 for v in vals):
            dropped += 1
            continue
        hi, lo = max(vals), min(vals)
        if hi != r["high"] or lo != r["low"]:
            repaired += 1
            r = dict(r, high=hi, low=lo)
        out.append(r)
    return out, dropped, repaired


def jump_warnings(s: Series, threshold: float = 0.25, limit: int = 5) -> list:
    out = []
    for i in range(1, s.n):
        move = max(abs(s.o[i] / s.c[i - 1] - 1), abs(s.c[i] / s.c[i - 1] - 1))
        if move >= threshold:
            out.append(f"{s.dates[i]}: {move * 100:.0f}% overnight move. Check for an unadjusted split, bonus or "
                       "demerger; the backtest books it as a real gap.")
    return out[:limit]


def build_series(rows: list, name: str, end: str | None = None, skipped_nulls: int = 0) -> Series:
    rows, dropped, repaired = sanitize(rows)
    if end:
        cut = _date(end, "--end")
        rows = [r for r in rows if (r["date"].date() if isinstance(r["date"], datetime) else r["date"]) <= cut]
    days = {(r["date"].date() if isinstance(r["date"], datetime) else r["date"]) for r in rows}
    if len(days) < len(rows):
        raise BacktestError("several bars share a date: backtest-lab needs daily (or weekly) bars, not intraday")
    if len(rows) < MIN_BARS:
        raise BacktestError(f"only {len(rows)} usable bars; need at least {MIN_BARS} (several years is better)")
    s = Series(rows, name)
    if skipped_nulls or dropped:
        s.notes.append(f"Skipped {skipped_nulls + dropped} null/invalid candle(s).")
    if repaired:
        s.notes.append(f"Widened high/low on {repaired} bar(s) that did not contain their open/close.")
    s.notes += jump_warnings(s)
    return s


def attach_regime(s: Series, bench_rows: list, ma: int = 200, label: str = "benchmark") -> None:
    """Allow longs only while the benchmark closes above its SMA(ma) on the signal date (shorts only
    while below). Dates without benchmark history block entries."""
    if ma < 2:
        raise BacktestError("--regime-ma must be at least 2")
    bench_rows, _, _ = sanitize(bench_rows)
    if len(bench_rows) <= ma:
        raise BacktestError(f"regime benchmark has {len(bench_rows)} bars; need more than --regime-ma {ma}")
    bd = [r["date"].date() if isinstance(r["date"], datetime) else r["date"] for r in bench_rows]
    bc = [r["close"] for r in bench_rows]
    sm = ta.sma(bc, ma)
    out, j = [], -1
    for d in s.dates:
        while j + 1 < len(bd) and bd[j + 1] <= d:
            j += 1
        if j < 0 or sm[j] is None:
            out.append((False, False))
        else:
            out.append((bc[j] > sm[j], bc[j] < sm[j]))
    s.regime = out
    s.regime_label = f"{label} close vs its {ma}-day SMA"


def _date(text: str, flag: str) -> date:
    try:
        return datetime.strptime(text.strip(), "%Y-%m-%d").date()
    except ValueError:
        raise BacktestError(f"{flag} must be YYYY-MM-DD, got {text!r}") from None


def start_index(s: Series, start: str | None) -> int:
    if not start:
        return 0
    d = _date(start, "--start")
    i = next((k for k, x in enumerate(s.dates) if x >= d), None)
    if i is None or s.n - i < 2:
        raise BacktestError(f"--start {start} leaves no bars to test")
    return i


# ---------------------------------------------------------------------------
# Signals (decided on each bar's close using data up to and including that bar)
# ---------------------------------------------------------------------------
def needs_atr(p: dict) -> bool:
    return p["stop_pct"] <= 0 and p["stop_atr"] > 0


def build_signals(s: Series, strategy: str, p: dict) -> dict:
    key = (strategy, tuple(sorted(p.items())))
    if key in s._sig:
        return s._sig[key]
    n, c, h, lo = s.n, s.c, s.h, s.l
    el, xl, es, xs = ([False] * n for _ in range(4))

    def ok(*vals):
        return all(v is not None for v in vals)

    if strategy == "sma-cross":
        f, sl = s.ind("sma", p["fast"]), s.ind("sma", p["slow"])
        warm = p["slow"]
        for i in range(1, n):
            if ok(f[i], sl[i], f[i - 1], sl[i - 1]):
                el[i] = f[i] > sl[i] and f[i - 1] <= sl[i - 1]
                es[i] = f[i] < sl[i] and f[i - 1] >= sl[i - 1]
                xl[i], xs[i] = f[i] < sl[i], f[i] > sl[i]
    elif strategy == "ema-pullback":
        e, t, lag = s.ind("ema", p["ema"]), s.ind("ema", p["trend"]), 5
        warm = p["trend"] + lag
        for i in range(lag, n):
            if ok(e[i], t[i], t[i - lag]):
                up = c[i] > t[i] and t[i] > t[i - lag]
                down = c[i] < t[i] and t[i] < t[i - lag]
                el[i] = up and lo[i] <= e[i] < c[i]
                es[i] = down and h[i] >= e[i] > c[i]
                xl[i], xs[i] = c[i] < t[i], c[i] > t[i]
    elif strategy == "rsi-reversion":
        r = s.ind("rsi", p["period"])
        tr = s.ind("sma", p["trend"]) if p["trend"] else None
        warm = max(p["period"] + 1, p["trend"])
        for i in range(n):
            if r[i] is None or (tr is not None and tr[i] is None):
                continue
            above = tr is None or c[i] > tr[i]
            below = tr is None or c[i] < tr[i]
            el[i] = r[i] < p["buy_below"] and above
            es[i] = r[i] > 100 - p["buy_below"] and below
            xl[i], xs[i] = r[i] > p["sell_above"], r[i] < 100 - p["sell_above"]
    elif strategy == "donchian-breakout":
        he, le = s.ind("hh", p["entry_n"]), s.ind("ll", p["entry_n"])
        hx, lx = s.ind("hh", p["exit_n"]), s.ind("ll", p["exit_n"])
        warm = max(p["entry_n"], p["exit_n"])
        for i in range(n):
            if ok(he[i], le[i]):
                el[i], es[i] = c[i] > he[i], c[i] < le[i]
            if ok(hx[i], lx[i]):
                xl[i], xs[i] = c[i] < lx[i], c[i] > hx[i]
    elif strategy == "bollinger-reversion":
        mid, up, dn = s.ind("boll", p["period"], p["k"])
        tr = s.ind("sma", p["trend"]) if p["trend"] else None
        warm = max(p["period"], p["trend"])
        for i in range(n):
            if not ok(mid[i], up[i], dn[i]) or (tr is not None and tr[i] is None):
                continue
            above = tr is None or c[i] > tr[i]
            below = tr is None or c[i] < tr[i]
            el[i], es[i] = c[i] < dn[i] and above, c[i] > up[i] and below
            xl[i], xs[i] = c[i] >= mid[i], c[i] <= mid[i]
    elif strategy == "52w-high":
        hh, ll, ma = s.ind("hh", p["lookback"]), s.ind("ll", p["lookback"]), s.ind("sma", p["exit_ma"])
        warm = max(p["lookback"], p["exit_ma"])
        for i in range(n):
            if ok(hh[i], ll[i], ma[i]):
                el[i], es[i] = c[i] > hh[i], c[i] < ll[i]
            if ma[i] is not None:
                xl[i], xs[i] = c[i] < ma[i], c[i] > ma[i]
    else:
        raise BacktestError(f"unknown strategy '{strategy}'")
    atr = s.ind("atr", ATR_N) if needs_atr(p) else None
    if atr is not None:
        warm = max(warm, ATR_N)
    out = {"entry_long": el, "exit_long": xl, "entry_short": es, "exit_short": xs, "atr": atr, "warmup": warm}
    s._sig[key] = out
    return out


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SHORT_RULE = ("--allow-short needs --segment futures: an Indian cash-market short must be bought back in the "
              "same session (no overnight short without SLB), so a multi-day short exists only as a futures position")


class Config:
    def __init__(self, capital: float = 1_000_000.0, segment: str = "delivery", broker: str = "zerodha",
                 exchange: str = "NSE", slippage_bps: float = 5.0, sizing: str = "pct",
                 size_value: float = 100.0, lot_size: int = 1, leverage: float = 1.0,
                 allow_short: bool = False, circuit_band: float | None = None, rf_pct: float = 6.5):
        if not (isinstance(capital, (int, float)) and math.isfinite(capital) and capital > 0):
            raise BacktestError("--capital must be a positive number")
        if segment not in SEGMENTS:
            raise BacktestError(f"--segment must be one of {', '.join(SEGMENTS)} (options are not backtested here)")
        if broker not in charges.BROKERS:
            raise BacktestError(f"unknown broker '{broker}'. Known: {', '.join(charges.BROKERS)}")
        exchange = exchange.upper()
        if exchange not in ("NSE", "BSE"):
            raise BacktestError("--exchange must be NSE or BSE")
        if not (math.isfinite(slippage_bps) and 0 <= slippage_bps <= 500):
            raise BacktestError("--slippage-bps must be between 0 and 500")
        if lot_size is None or int(lot_size) != lot_size or lot_size < 1:
            raise BacktestError("--lot-size must be a whole number ≥ 1")
        if not (math.isfinite(leverage) and 1 <= leverage <= 10):
            raise BacktestError("--leverage must be between 1 and 10")
        if leverage > 1 and segment == "delivery":
            raise BacktestError("--leverage applies to intraday or futures; delivery is fully paid (MTF is not modelled)")
        if allow_short and segment != "futures":
            raise BacktestError(SHORT_RULE)
        if sizing not in ("pct", "risk", "fixed"):
            raise BacktestError("sizing must be pct, risk or fixed")
        if not (math.isfinite(size_value) and size_value > 0):
            raise BacktestError("position size must be positive")
        if sizing == "pct" and size_value > 100 * leverage:
            raise BacktestError(f"--size-pct above {100 * leverage:g}% needs more --leverage")
        if sizing == "risk" and size_value > 10:
            raise BacktestError("--risk-pct above 10% per trade is not a sizing rule, it is a coin flip")
        if circuit_band is not None and circuit_band not in CIRCUIT_BANDS:
            raise BacktestError("--circuit-band must be 2, 5, 10 or 20")
        if not (math.isfinite(rf_pct) and -5 <= rf_pct <= 30):
            raise BacktestError("--rf must be an annual % between -5 and 30")
        self.capital, self.segment, self.broker, self.exchange = float(capital), segment, broker, exchange
        self.slippage_bps, self.sizing, self.size_value = float(slippage_bps), sizing, float(size_value)
        self.lot_size, self.leverage, self.allow_short = int(lot_size), float(leverage), bool(allow_short)
        self.circuit_band, self.rf_pct = circuit_band, float(rf_pct)

    def describe(self) -> dict:
        size = {"pct": f"{self.size_value:g}% of equity", "risk": f"risk {self.size_value:g}% of equity per trade",
                "fixed": f"{charges.inr(self.size_value, 0)} per trade"}[self.sizing]
        return {"capital": self.capital, "segment": self.segment, "broker": self.broker,
                "broker_name": charges.BROKERS[self.broker]["name"], "exchange": self.exchange,
                "slippage_bps": self.slippage_bps, "sizing": self.sizing, "size_value": self.size_value,
                "size_rule": size, "lot_size": self.lot_size, "leverage": self.leverage,
                "allow_short": self.allow_short, "circuit_band_pct": self.circuit_band,
                "risk_free_pct": self.rf_pct, "charges_rates_as_of": charges.RATES_AS_OF}


def combo_error(cfg: Config, p: dict) -> str | None:
    if cfg.sizing == "risk" and p["stop_atr"] <= 0 and p["stop_pct"] <= 0:
        return "--risk-pct sizing needs a stop (stop_atr or stop_pct > 0)"
    return None


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------
def outcome(pnl: float) -> str:
    return "win" if pnl > EPS else "loss" if pnl < -EPS else "breakeven"


def _stop_distance(p: dict, atr, i: int, price: float) -> float | None:
    if p["stop_pct"] > 0:
        return price * p["stop_pct"] / 100
    if p["stop_atr"] > 0 and atr is not None and atr[i] is not None:
        return p["stop_atr"] * atr[i]
    return None


def _band_locks(cfg: Config, prev_close: float | None, open_px: float) -> tuple[bool, bool]:
    """(open at the upper band limit, open at the lower band limit)."""
    if not cfg.circuit_band or not prev_close:
        return False, False
    tol = max(0.01, prev_close * 0.0002)
    upper = prev_close * (1 + cfg.circuit_band / 100)
    lower = prev_close * (1 - cfg.circuit_band / 100)
    return open_px >= upper - tol, open_px <= lower + tol


def plan_qty(cfg: Config, p: dict, sig: dict, i: int, price: float, equity: float) -> tuple[int, str | None]:
    """Size an order on the signal bar's close (known when the order is placed)."""
    if equity <= 0:
        return 0, "no equity left"
    lot = cfg.lot_size
    if cfg.sizing == "pct":
        units = equity * cfg.size_value / 100 / price
    elif cfg.sizing == "fixed":
        units = cfg.size_value / price
    else:
        dist = _stop_distance(p, sig["atr"], i, price)
        if not dist:
            return 0, "risk sizing needs a stop distance"
        units = equity * cfg.size_value / 100 / dist
    cap = equity * cfg.leverage * (1.0 if cfg.segment == "futures" else CASH_BUFFER) / price
    units = min(units, cap)
    qty = int(math.floor(units / lot + 1e-9)) * lot
    if qty <= 0:
        what = f"1 lot of {lot}" if lot > 1 else "1 share"
        return 0, f"size rounds to zero: {what} at {price:,.2f} does not fit the sizing rule"
    return qty, None


def simulate(s: Series, sig: dict, p: dict, cfg: Config, start: int = 0, end: int | None = None,
             capital: float | None = None) -> dict:
    end = s.n if end is None else min(end, s.n)
    capital = cfg.capital if capital is None else float(capital)
    slip = cfg.slippage_bps / 1e4
    atr = sig["atr"]
    realized = capital
    pos = None
    pend_entry = None
    pend_exit = None
    trades, events, equity = [], [], []
    exposed = blocked = 0
    ruined = False

    def close(i: int, ref: float, reason: str) -> None:
        nonlocal realized, pos
        sd, qty = pos["side"], pos["qty"]
        px = ref * (1 - sd * slip)
        buy_px, sell_px = (pos["fill"], px) if sd == 1 else (px, pos["fill"])
        seg = "intraday" if (cfg.segment == "delivery" and pos["i"] == i) else cfg.segment
        total = charges.calculate(seg, buy_px, sell_px, qty, broker=cfg.broker, exchange=cfg.exchange)["total_charges"]
        gross = sd * (px - pos["fill"]) * qty
        net = gross - total
        realized += gross - (total - pos["entry_leg"])
        risk = pos["risk_unit"] * qty if pos["risk_unit"] else None
        trades.append({
            "n": len(trades) + 1, "side": "long" if sd == 1 else "short",
            "entry_date": s.dates[pos["i"]].isoformat(), "entry_price": pos["fill"], "entry_open": pos["ref"],
            "signal_date": s.dates[pos["si"]].isoformat(),
            "exit_date": s.dates[i].isoformat(), "exit_price": px, "exit_ref": ref, "exit_reason": reason,
            "qty": qty, "lots": qty // cfg.lot_size if cfg.lot_size > 1 else None,
            "stop": pos["stop"], "target": pos["target"], "bars_held": i - pos["i"] + 1,
            "gross_pnl": gross, "charges": total, "charges_segment": seg,
            "slippage": qty * (abs(pos["fill"] - pos["ref"]) + abs(px - ref)),
            "net_pnl": net, "return_pct": net / pos["equity_before"] * 100 if pos["equity_before"] > 0 else None,
            "r_multiple": net / risk if risk else None, "outcome": outcome(net),
            "entry_index": pos["i"], "exit_index": i,
        })
        pos = None

    for i in range(start, end):
        o, hi, lo, cl = s.o[i], s.h[i], s.l[i], s.c[i]
        d = s.dates[i].isoformat()
        up_lock, dn_lock = _band_locks(cfg, s.c[i - 1] if i > 0 else None, o)
        had_pos, entered = pos is not None, False
        # 1) exit ordered at the previous close
        if pos is not None and pend_exit:
            if dn_lock if pos["side"] == 1 else up_lock:
                events.append({"date": d, "event": "exit delayed",
                               "reason": f"open at the {'lower' if pos['side'] == 1 else 'upper'} circuit: no fill"})
            else:
                close(i, o, pend_exit)
                pend_exit = None
        # 2) entry ordered at the previous close
        if pend_entry is not None:
            side, si, qty_plan = pend_entry
            pend_entry = None
            if pos is not None:
                events.append({"date": d, "event": "entry skipped", "reason": "previous position still open"})
            elif (side == 1 and up_lock) or (side == -1 and dn_lock):
                events.append({"date": d, "event": "entry skipped",
                               "reason": f"open at the {'upper' if side == 1 else 'lower'} circuit: order would queue unfilled"})
            else:
                fill = o * (1 + side * slip)
                cap_units = realized * cfg.leverage * (1.0 if cfg.segment == "futures" else CASH_BUFFER) / fill
                qty = min(qty_plan, int(math.floor(cap_units / cfg.lot_size + 1e-9)) * cfg.lot_size)
                if qty <= 0:
                    events.append({"date": d, "event": "entry skipped", "reason": "not enough equity at the fill price"})
                else:
                    dist = _stop_distance(p, atr, si, fill)       # % of the fill, or k x ATR at the signal bar
                    stop = fill - side * dist if dist else None
                    target = fill + side * p["target_r"] * dist if (dist and p["target_r"] > 0) else None
                    leg = charges.calculate(cfg.segment, fill if side == 1 else 0.0, 0.0 if side == 1 else fill, qty,
                                            broker=cfg.broker, exchange=cfg.exchange)["total_charges"]
                    pos = {"side": side, "qty": qty, "fill": fill, "ref": o, "i": i, "si": si, "stop": stop,
                           "target": target, "entry_leg": leg, "equity_before": realized, "risk_unit": dist}
                    realized -= leg
                    entered = True
        # 3) resting stop / target inside this bar
        if pos is not None:
            sd, st, tg = pos["side"], pos["stop"], pos["target"]
            carried = pos["i"] < i          # an entry at this open cannot have gapped through its own stop
            delayed = False
            if st is not None and ((sd == 1 and lo <= st) or (sd == -1 and hi >= st)):
                gapped = carried and (o <= st if sd == 1 else o >= st)
                if gapped and (dn_lock if sd == 1 else up_lock):
                    delayed = True
                    pend_exit = "stop (after circuit)"
                    events.append({"date": d, "event": "stop delayed",
                                   "reason": "gapped through the stop into a circuit limit: no fill this session"})
                else:
                    close(i, o if gapped else st, "stop (gap)" if gapped else "stop")
            if pos is not None and not delayed and tg is not None and ((sd == 1 and hi >= tg) or (sd == -1 and lo <= tg)):
                gapped = carried and (o >= tg if sd == 1 else o <= tg)
                close(i, o if gapped else tg, "target (gap)" if gapped else "target")
        # 4) forced exits at this close
        if pos is not None and cfg.segment == "intraday":
            close(i, cl, "square-off (MIS)")
        elif pos is not None and i == end - 1:
            close(i, cl, "end of test")
        if had_pos or entered:
            exposed += 1
        eq = realized + (pos["side"] * pos["qty"] * (cl - pos["fill"]) if pos else 0.0)
        if eq <= 0 and not ruined:
            ruined = True
            if pos is not None:
                close(i, cl, "equity wiped out")
            eq = realized
            pend_entry = pend_exit = None
        equity.append((s.dates[i], eq))
        # 5) decisions on this close -> orders for the next open
        if i >= end - 1 or ruined:
            continue
        if pos is not None and pend_exit is None:
            if sig["exit_long" if pos["side"] == 1 else "exit_short"][i]:
                pend_exit = "exit signal"
            elif p["max_hold"] and i - pos["i"] + 1 >= p["max_hold"]:
                pend_exit = f"time exit ({p['max_hold']} bars)"
        if pos is None or pend_exit is not None:
            cur = pos["side"] if pos is not None else 0
            side = 0
            if sig["entry_long"][i] and cur != 1:
                side = 1
            elif cfg.allow_short and sig["entry_short"][i] and cur != -1:
                side = -1
            if side and s.regime is not None and not s.regime[i][0 if side == 1 else 1]:
                blocked += 1
                side = 0
            if side:
                qty, why = plan_qty(cfg, p, sig, i, cl, eq)
                if qty:
                    pend_entry = (side, i, qty)
                else:
                    events.append({"date": d, "event": "signal skipped", "reason": why})
    return {"capital": capital, "final_equity": equity[-1][1] if equity else capital, "equity": equity,
            "trades": trades, "events": events, "exposed_bars": exposed, "bars": end - start,
            "blocked_by_regime": blocked, "ruined": ruined, "start": start, "end": end}


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
def periods_per_year(dates: list) -> int:
    gaps = sorted((b - a).days for a, b in zip(dates, dates[1:]))
    if not gaps:
        return 252
    med = gaps[len(gaps) // 2]
    return 252 if med <= 2 else 52 if med <= 8 else 12


def cagr(start_value: float, end_value: float, years: float) -> float | None:
    """Compound annual growth as a fraction (0.10 = 10%)."""
    if years <= 0 or start_value <= 0:
        return None
    if end_value <= 0:
        return -1.0
    return (end_value / start_value) ** (1 / years) - 1


def drawdown(values: list) -> dict:
    """Deepest peak-to-trough fall (as a negative %) and the longest stretch below a prior high."""
    if not values:
        return {"max_dd_pct": 0.0, "peak": None, "trough": None, "recovery": None, "longest_underwater": 0}
    peak, peak_i = values[0], 0
    mdd, m_peak, m_trough = 0.0, 0, 0
    under_from, longest = None, 0
    for i, v in enumerate(values):
        if v >= peak:
            if under_from is not None:
                longest = max(longest, i - under_from)
                under_from = None
            peak, peak_i = v, i
        else:
            if under_from is None:
                under_from = peak_i
            dd = v / peak - 1 if peak > 0 else -1.0
            if dd < mdd:
                mdd, m_peak, m_trough = dd, peak_i, i
    if under_from is not None:
        longest = max(longest, len(values) - 1 - under_from)
    recovery = None
    if mdd < 0:
        recovery = next((k for k in range(m_trough + 1, len(values)) if values[k] >= values[m_peak]), None)
    return {"max_dd_pct": mdd * 100, "peak": m_peak if mdd < 0 else None, "trough": m_trough if mdd < 0 else None,
            "recovery": recovery, "longest_underwater": longest}


def return_stats(values: list, ppy: int, rf_pct: float) -> dict:
    rets = [b / a - 1 for a, b in zip(values, values[1:]) if a > 0]
    if len(rets) < 2:
        return {"volatility_pct": None, "sharpe": None, "sortino": None}
    rf = (1 + rf_pct / 100) ** (1 / ppy) - 1
    mean, sd = statistics.fmean(rets), statistics.stdev(rets)
    downside = math.sqrt(sum(min(0.0, r - rf) ** 2 for r in rets) / len(rets))
    root = math.sqrt(ppy)
    if sd <= 1e-12:                     # flat curve (e.g. no trades): ratios are undefined, not huge
        return {"volatility_pct": 0.0, "sharpe": None, "sortino": None}
    return {"volatility_pct": sd * root * 100,
            "sharpe": (mean - rf) / sd * root,
            "sortino": (mean - rf) / downside * root if downside > 1e-12 else None}


def curve_metrics(values: list, dates: list, rf_pct: float) -> dict:
    """values[0] is the starting capital (before dates[0]); values[k] is the close of dates[k-1]."""
    years = (dates[-1] - dates[0]).days / 365.25 if len(dates) > 1 else 0.0
    ppy = periods_per_year(dates)
    dd = drawdown(values)
    g = cagr(values[0], values[-1], years)
    when = lambda k: None if k is None else dates[max(0, k - 1)].isoformat()
    out = {"start_date": dates[0].isoformat(), "end_date": dates[-1].isoformat(), "bars": len(dates),
           "years": years, "periods_per_year": ppy, "start_equity": values[0], "final_equity": values[-1],
           "total_return_pct": (values[-1] / values[0] - 1) * 100,
           "cagr_pct": g * 100 if g is not None else None,
           "max_drawdown_pct": dd["max_dd_pct"], "max_dd_peak": when(dd["peak"]),
           "max_dd_trough": when(dd["trough"]), "max_dd_recovery": when(dd["recovery"]),
           "longest_underwater_bars": dd["longest_underwater"]}
    out.update(return_stats(values, ppy, rf_pct))
    out["calmar"] = (g / abs(dd["max_dd_pct"] / 100)) if (g is not None and dd["max_dd_pct"] < 0) else None
    out["risk_free_pct"] = rf_pct
    return out


def trade_stats(trades: list) -> dict:
    n = len(trades)
    pnl = [t["net_pnl"] for t in trades]
    wins = [x for x in pnl if outcome(x) == "win"]
    losses = [x for x in pnl if outcome(x) == "loss"]
    gp, gl = sum(wins), -sum(losses)
    rs = [t["r_multiple"] for t in trades if t.get("r_multiple") is not None]
    run = longest = 0
    for x in pnl:
        o = outcome(x)
        if o == "loss":
            run += 1
            longest = max(longest, run)
        elif o == "win":
            run = 0                      # a breakeven neither extends nor resets a losing streak
    gross_profit = sum(max(0.0, t["gross_pnl"]) for t in trades)
    total_ch = sum(t["charges"] for t in trades)
    aw = gp / len(wins) if wins else None
    al = -gl / len(losses) if losses else None
    best = max(pnl) if pnl else None
    net = sum(pnl)
    return {
        "trades": n, "wins": len(wins), "losses": len(losses), "breakevens": n - len(wins) - len(losses),
        "win_rate_pct": len(wins) / n * 100 if n else None,
        "avg_win": aw, "avg_loss": al, "payoff_ratio": aw / -al if (aw and al) else None,
        "profit_factor": gp / gl if gl > 0 else None, "net_pnl": net,
        "expectancy": net / n if n else None,
        "expectancy_r": statistics.fmean(rs) if rs else None, "trades_with_r": len(rs),
        "longest_losing_streak": longest,
        "avg_bars_held": statistics.fmean(t["bars_held"] for t in trades) if trades else None,
        "total_charges": total_ch, "total_slippage": sum(t["slippage"] for t in trades),
        "gross_profit_before_costs": gross_profit,
        "charges_pct_of_gross_profit": total_ch / gross_profit * 100 if gross_profit > 0 else None,
        "best_trade_pct_of_net": best / net * 100 if (best is not None and net > 0 and best > 0) else None,
    }


def summarize(res: dict, cfg: Config) -> dict:
    dates = [d for d, _ in res["equity"]]
    values = [res["capital"]] + [e for _, e in res["equity"]]
    m = curve_metrics(values, dates, cfg.rf_pct)
    m.update(trade_stats(res["trades"]))
    m["exposure_pct"] = res["exposed_bars"] / res["bars"] * 100 if res["bars"] else 0.0
    m["blocked_by_regime"] = res["blocked_by_regime"]
    return m


def bh_curve(s: Series, cfg: Config, start: int, end: int, capital: float) -> tuple[list, float] | None:
    """Delivery buy at the first open, marked at each close, sold at the last close: one round trip of
    charges and slippage. Returns ([capital, value at each close], total charges)."""
    slip = cfg.slippage_bps / 1e4
    fill = s.o[start] * (1 + slip)
    qty = math.floor(capital * CASH_BUFFER / fill)
    if qty <= 0 or end - start < 2:
        return None
    out_px = s.c[end - 1] * (1 - slip)
    kw = {"broker": cfg.broker, "exchange": cfg.exchange}
    leg = charges.calculate("delivery", fill, 0.0, qty, **kw)["total_charges"]
    total = charges.calculate("delivery", fill, out_px, qty, **kw)["total_charges"]
    cash = capital - qty * fill - leg
    values = [capital] + [cash + qty * s.c[i] for i in range(start, end)]
    values[-1] = cash + qty * out_px - (total - leg)
    return values, total


def buy_and_hold(s: Series, cfg: Config, start: int, end: int, capital: float) -> dict | None:
    curve = bh_curve(s, cfg, start, end, capital)
    if curve is None:
        return None
    m = curve_metrics(curve[0], s.dates[start:end], cfg.rf_pct)
    m["charges"] = curve[1]
    return m


def yearly(res: dict, bh_values: list | None = None) -> list:
    rows, prev, prev_bh = [], res["capital"], (bh_values[0] if bh_values else None)
    by_year: dict = {}
    for k, (d, e) in enumerate(res["equity"]):
        by_year.setdefault(d.year, []).append((e, bh_values[k + 1] if bh_values else None))
    exits: dict = {}
    for t in res["trades"]:
        y = int(t["exit_date"][:4])
        exits[y] = exits.get(y, 0) + 1
    for y in sorted(by_year):
        e_end, b_end = by_year[y][-1]
        rows.append({"year": y, "strategy_pct": (e_end / prev - 1) * 100 if prev > 0 else None,
                     "buy_hold_pct": (b_end / prev_bh - 1) * 100 if (b_end is not None and prev_bh) else None,
                     "trades_closed": exits.get(y, 0)})
        prev, prev_bh = e_end, b_end
    return rows


def red_flags(m: dict, bh: dict | None, cfg: Config) -> list:
    out = []
    n = m["trades"]
    if n == 0:
        return ["No trades: the rules never fired on this data. Check the parameters, the warm-up and --start."]
    if n < 30:
        out.append(f"Only {n} trades: too few to tell skill from luck. Aim for 30+, ideally 100+ "
                   "(more years or more symbols).")
    cp = m.get("charges_pct_of_gross_profit")
    if cp is not None and cp > 25:
        out.append(f"Charges take {cp:.0f}% of gross profit. Indian STT, stamp duty and brokerage are eating the edge; "
                   "trade less often or hold longer.")
    pf = m.get("profit_factor")
    if pf is not None and pf < 1.2:
        out.append(f"Profit factor {pf:.2f}: little cushion for worse fills or a regime change.")
    bt = m.get("best_trade_pct_of_net")
    if bt is not None and bt > 100:
        out.append("The best trade alone made more than the whole net profit: without it the rules lose money.")
    elif bt is not None and bt > 50:
        out.append(f"The single best trade is {bt:.0f}% of net profit. Remove it mentally: is anything left?")
    if bh and m.get("cagr_pct") is not None and bh.get("cagr_pct") is not None:
        if m["cagr_pct"] < bh["cagr_pct"] and m["max_drawdown_pct"] < bh["max_drawdown_pct"]:
            out.append("Lower return AND a deeper drawdown than buy-and-hold: the rules add nothing here.")
    if m["years"] < 5 and m["periods_per_year"] == 252:
        out.append(f"Only {m['years']:.1f} years tested. Include at least one bad phase (2008, 2015-16, the 2018 "
                   "small/midcap fall, March 2020, the late-2024 to early-2025 correction).")
    if cfg.segment == "futures":
        out.append("Futures on a spot/continuous series: rollover cost, basis and margin calls are not modelled.")
    return out


# ---------------------------------------------------------------------------
# Tax (approximate; rates from indian-tax-for-traders)
# ---------------------------------------------------------------------------
def tax_estimate(trades: list, segment: str, slab_pct: float, capital: float, years: float) -> dict:
    buckets: dict = {}
    for t in trades:
        d_in, d_out = date.fromisoformat(t["entry_date"]), date.fromisoformat(t["exit_date"])
        if segment == "futures":
            kind = "business"
        elif segment == "intraday" or d_in == d_out:
            kind = "speculative"
        else:
            kind = "ltcg" if taxmod.is_long_term(d_in, d_out) else "stcg"
        fy = buckets.setdefault(taxmod.fy_of(d_out), {"stcg": 0.0, "ltcg": 0.0, "speculative": 0.0, "business": 0.0})
        fy[kind] += t["net_pnl"]
    slab = slab_pct / 100
    rows, total = [], 0.0
    for fy in sorted(buckets):
        b = buckets[fy]
        st, lt = b["stcg"], b["ltcg"]
        if st < 0:                      # a short-term loss may be set off against long-term gains
            lt, st = lt + st, 0.0
        lt_taxable = max(0.0, lt - taxmod.LTCG_EXEMPTION)
        tax = (max(0.0, st) * taxmod.STCG_RATE + lt_taxable * taxmod.LTCG_RATE
               + (max(0.0, b["speculative"]) + max(0.0, b["business"])) * slab) * (1 + taxmod.CESS)
        total += tax
        rows.append({"fy": fy, **b, "tax": tax})
    net = sum(t["net_pnl"] for t in trades)
    post = cagr(capital, capital + net - total, years)
    return {"by_fy": rows, "total_tax": total, "pre_tax_net_pnl": net, "post_tax_net_pnl": net - total,
            "post_tax_cagr_pct": post * 100 if post is not None else None,
            "rates": {"stcg_pct": taxmod.STCG_RATE * 100, "ltcg_pct": taxmod.LTCG_RATE * 100,
                      "ltcg_exemption": taxmod.LTCG_EXEMPTION, "slab_pct": slab_pct, "cess_pct": taxmod.CESS * 100},
            "assumptions": "Approximate. Delivery held ≤12 months = STCG 20%, >12 months = LTCG 12.5% above "
                           "₹1.25 lakh per FY; same-day/intraday = speculative and futures = business income, both "
                           f"at a flat {slab_pct:g}% slab; 4% cess. No carry-forward of losses, surcharge, rebate or "
                           "basic exemption. Charges are netted before tax, but STT is not deductible against "
                           "capital gains, so the real bill is slightly higher. Confirm with a CA."}


# ---------------------------------------------------------------------------
# Sweep, walk-forward, Monte Carlo
# ---------------------------------------------------------------------------
def run_one(s: Series, strategy: str, p: dict, cfg: Config, start: int = 0, end: int | None = None,
            capital: float | None = None) -> tuple[dict, dict]:
    end = s.n if end is None else end
    sig = build_signals(s, strategy, p)
    if end - max(start, sig["warmup"]) < 2:
        raise BacktestError(f"{strategy} with these parameters needs about {sig['warmup']} bars of history before its "
                            f"first signal; the test window has only {end - start} bars (data {s.n}).")
    res = simulate(s, sig, p, cfg, start, end, capital)
    return res, summarize(res, cfg)


def expected_max_z(n: int) -> float:
    """Approximate expected maximum of n independent standard normal draws."""
    if n < 2:
        return 0.0
    gamma = 0.5772156649015329
    nd = statistics.NormalDist()
    return (1 - gamma) * nd.inv_cdf(1 - 1 / n) + gamma * nd.inv_cdf(1 - 1 / (n * math.e))


def sweep(s: Series, strategy: str, base: dict, grid: dict, cfg: Config, metric: str = "sharpe",
          start: int = 0, end: int | None = None, min_trades: int = 5) -> dict:
    if metric not in METRICS:
        raise BacktestError(f"--metric must be one of {', '.join(METRICS)}")
    keys = list(grid)
    cells = []
    for idx in itertools.product(*(range(len(grid[k])) for k in keys)):
        p = dict(base)
        p.update({k: grid[k][j] for k, j in zip(keys, idx)})
        cell = {"idx": list(idx), "params": {k: p[k] for k in keys}, "valid": False, "reason": None,
                "trades": 0, "score": None}
        err = check_params(strategy, p) or combo_error(cfg, p)
        if err:
            cell["reason"] = err
            cells.append(cell)
            continue
        try:
            _, m = run_one(s, strategy, p, cfg, start, end)
        except BacktestError:
            cell["reason"] = "not enough bars for these parameters"
            cells.append(cell)
            continue
        cell.update({"valid": True, "trades": m["trades"], "score": m[METRIC_KEY[metric]],
                     "total_return_pct": m["total_return_pct"], "cagr_pct": m["cagr_pct"],
                     "max_drawdown_pct": m["max_drawdown_pct"], "sharpe": m["sharpe"],
                     "profit_factor": m["profit_factor"], "win_rate_pct": m["win_rate_pct"],
                     "exposure_pct": m["exposure_pct"], "years": m["years"]})
        cells.append(cell)
    rob = robustness(cells, metric, min_trades)
    return {"metric": metric, "grid": grid, "base_params": base, "cells": cells, "robustness": rob}


def robustness(cells: list, metric: str, min_trades: int) -> dict:
    """Is the best cell a lone spike or the top of a plateau? Neighbours = cells one step away in
    every grid dimension (diagonals included)."""
    elig = [c for c in cells if c["valid"] and c["score"] is not None and c["trades"] >= min_trades]
    valid = [c for c in cells if c["valid"]]
    out = {"metric": metric, "cells": len(cells), "valid_cells": len(valid), "eligible_cells": len(elig),
           "min_trades": min_trades, "best": None, "neighbours": 0, "neighbours_scored": 0,
           "neighbour_median": None, "neighbour_min": None, "neighbour_ratio": None,
           "grid_median": statistics.median([c["score"] for c in elig]) if elig else None,
           "profitable_cells_pct": (sum(1 for c in valid if c["total_return_pct"] > 0) / len(valid) * 100) if valid else None,
           "luck_hurdle_sharpe": None, "verdict": None}
    if not elig:
        out["verdict"] = f"no usable cell: none has a {metric} score with at least {min_trades} trades"
        return out
    best = max(elig, key=lambda c: c["score"])
    ids = {id(c) for c in elig}
    nb = [c for c in cells if c is not best and max(abs(a - b) for a, b in zip(c["idx"], best["idx"])) == 1]
    scored = [c["score"] for c in nb if id(c) in ids]
    out["best"] = {"params": best["params"], "score": best["score"], "trades": best["trades"], "idx": best["idx"],
                   "years": best.get("years")}
    out["neighbours"], out["neighbours_scored"] = len(nb), len(scored)
    if scored:
        out["neighbour_median"], out["neighbour_min"] = statistics.median(scored), min(scored)
        if best["score"] > 0:
            out["neighbour_ratio"] = out["neighbour_median"] / best["score"]
    if metric == "sharpe" and len(elig) >= 2 and best.get("years"):
        out["luck_hurdle_sharpe"] = expected_max_z(len(elig)) / math.sqrt(best["years"])
    r = out["neighbour_ratio"]
    if best["score"] <= 0:
        out["verdict"] = f"no edge: even the best cell's {metric} is ≤ 0"
    elif not scored:
        out["verdict"] = "isolated: no scored neighbour to compare with; widen the grid around the best cell"
    elif r >= 0.75 and out["neighbour_min"] > 0:
        out["verdict"] = "plateau: neighbours keep ≥75% of the best score, so the result is not knife-edge"
    elif r >= 0.5:
        out["verdict"] = "slope: neighbours keep 50-75% of the best; pick the middle of the good zone, not the peak"
    else:
        out["verdict"] = "spike: neighbours keep under half of the best score; likely curve-fit"
    return out


def walkforward(s: Series, strategy: str, base: dict, grid: dict, cfg: Config, metric: str = "sharpe",
                is_bars: int = 756, oos_bars: int = 252, min_trades: int = 5, start: int = 0) -> dict:
    if is_bars < 20 or oos_bars < 10:
        raise BacktestError("--is-bars must be ≥ 20 and --oos-bars ≥ 10")
    windows, oos_curve = [], []
    equity = cfg.capital
    a = start
    while True:
        is_end = a + is_bars
        oos_end = min(is_end + oos_bars, s.n)
        if is_end >= s.n or oos_end - is_end < max(10, oos_bars // 4):
            break
        sw = sweep(s, strategy, base, grid, cfg, metric, a, is_end, min_trades)
        best = sw["robustness"]["best"]
        w = {"is_range": [a, is_end], "oos_range": [is_end, oos_end],
             "is_start": s.dates[a].isoformat(), "is_end": s.dates[is_end - 1].isoformat(),
             "oos_start": s.dates[is_end].isoformat(), "oos_end": s.dates[oos_end - 1].isoformat(),
             "params": None, "is_score": None, "is_cagr_pct": None, "is_verdict": sw["robustness"]["verdict"],
             "oos_score": None, "oos_return_pct": 0.0, "oos_trades": 0, "start_equity": equity}
        if best is None:
            w["note"] = "no in-sample cell qualified: stayed in cash"
            oos_curve += [(s.dates[i], equity) for i in range(is_end, oos_end)]
        else:
            p = dict(base)
            p.update(best["params"])
            cell = next(c for c in sw["cells"] if c["idx"] == best["idx"])
            res, m = run_one(s, strategy, p, cfg, is_end, oos_end, equity)
            w.update({"params": best["params"], "is_score": best["score"], "is_cagr_pct": cell["cagr_pct"],
                      "oos_score": m[METRIC_KEY[metric]], "oos_return_pct": m["total_return_pct"],
                      "oos_trades": m["trades"], "oos_max_drawdown_pct": m["max_drawdown_pct"]})
            oos_curve += res["equity"]
            equity = res["final_equity"]
        w["end_equity"] = equity
        windows.append(w)
        a += oos_bars
    if not windows:
        raise BacktestError(f"data has {s.n - start} bars; walk-forward needs more than --is-bars {is_bars} plus "
                            f"at least {max(10, oos_bars // 4)} out-of-sample bars")
    dates = [d for d, _ in oos_curve]
    oos = curve_metrics([cfg.capital] + [e for _, e in oos_curve], dates, cfg.rf_pct)
    oos["trades"] = sum(w["oos_trades"] for w in windows)
    used = [w for w in windows if w["params"] is not None]
    is_scores = [w["is_score"] for w in used if w["is_score"] is not None]
    oos_scores = [w["oos_score"] for w in used if w["oos_score"] is not None]
    is_cagr = [w["is_cagr_pct"] for w in used if w["is_cagr_pct"] is not None]
    mean_is_cagr = statistics.fmean(is_cagr) if is_cagr else None
    eff = (oos["cagr_pct"] / mean_is_cagr) if (mean_is_cagr and mean_is_cagr > 0 and oos["cagr_pct"] is not None) else None
    changes = sum(1 for x, y in zip(used, used[1:]) if x["params"] != y["params"])
    if eff is None:
        verdict = "no in-sample edge to carry forward" if not mean_is_cagr or mean_is_cagr <= 0 else "not enough data"
    elif eff >= 0.5:
        verdict = "holds up: out-of-sample keeps at least half of the in-sample return"
    elif eff > 0:
        verdict = "degrades: out-of-sample keeps under half of the in-sample return; expect less live"
    else:
        verdict = "fails out of sample: the optimised edge did not survive unseen data"
    bh = buy_and_hold(s, cfg, windows[0]["oos_range"][0], windows[-1]["oos_range"][1], cfg.capital)
    return {"metric": metric, "grid": grid, "base_params": base, "is_bars": is_bars, "oos_bars": oos_bars,
            "windows": windows,
            "oos": oos, "buy_hold_oos": bh,
            "is_vs_oos": {"mean_is_score": statistics.fmean(is_scores) if is_scores else None,
                          "mean_oos_score": statistics.fmean(oos_scores) if oos_scores else None,
                          "mean_is_cagr_pct": mean_is_cagr, "oos_cagr_pct": oos["cagr_pct"],
                          "efficiency": eff, "param_changes": changes,
                          "profitable_oos_windows": sum(1 for w in windows if w["oos_return_pct"] > 0),
                          "verdict": verdict}}


def percentile(sorted_vals: list, q: float) -> float:
    if not sorted_vals:
        raise ValueError("empty sample")
    pos = (len(sorted_vals) - 1) * q / 100
    lo = math.floor(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def path_drawdown(capital: float, rets: list) -> tuple[float, float]:
    eq = peak = capital
    mdd = 0.0
    for r in rets:
        eq *= 1 + r
        if eq <= 0:
            return 0.0, 100.0
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
    return eq, -mdd * 100


def monte_carlo(returns: list, capital: float, sims: int = 1000, seed: int = 42, method: str = "bootstrap",
                ruin_dd: float = 30.0, years: float | None = None) -> dict:
    """Resample per-trade returns (fractions of equity). bootstrap draws with replacement (final equity
    varies); shuffle only reorders, so final equity is fixed and only the path/drawdown changes."""
    if len(returns) < 5:
        raise BacktestError(f"Monte Carlo needs at least 5 trades; got {len(returns)}")
    if not 1 <= sims <= 100_000:
        raise BacktestError("--sims must be between 1 and 100000")
    if method not in ("bootstrap", "shuffle"):
        raise BacktestError("--method must be bootstrap or shuffle")
    if not 0 < ruin_dd < 100:
        raise BacktestError("--ruin-dd must be a drawdown % between 0 and 100")
    rng = random.Random(seed)
    k = len(returns)
    finals, dds = [], []
    for _ in range(sims):
        seq = [returns[rng.randrange(k)] for _ in range(k)] if method == "bootstrap" else rng.sample(returns, k)
        f, d = path_drawdown(capital, seq)
        finals.append(f)
        dds.append(d)
    finals.sort()
    dds.sort()
    pcts = (5, 50, 95)
    fin = {f"p{q}": percentile(finals, q) for q in pcts}
    act_final, act_dd = path_drawdown(capital, returns)
    out = {"method": method, "sims": sims, "seed": seed, "trades": k, "capital": capital,
           "max_drawdown_pct": {f"p{q}": percentile(dds, q) for q in pcts},
           "final_equity": fin,
           "cagr_pct": None,
           "risk_of_ruin_pct": sum(1 for d in dds if d >= ruin_dd) / sims * 100, "ruin_dd_pct": ruin_dd,
           "prob_loss_pct": sum(1 for f in finals if f < capital) / sims * 100,
           "actual": {"final_equity": act_final, "max_drawdown_pct": act_dd}}
    if years and years > 0:
        out["cagr_pct"] = {key: (cagr(capital, v, years) or 0.0) * 100 for key, v in fin.items()}
        out["years"] = years
    return out


def returns_from_csv(path: str, capital: float) -> tuple[list, float | None]:
    if not Path(path).is_file():
        raise BacktestError(f"trades file not found: {path}")
    rets, days = [], []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        cols = {c.strip().lower(): c for c in reader.fieldnames or [] if c}
        if "return_pct" not in cols and "pnl" not in cols:
            raise BacktestError(f"{path}: need a return_pct column (from run --trades-csv) or a pnl column")
        for k, row in enumerate(reader, start=2):
            raw = (row.get(cols.get("return_pct", ""), "") or "").strip()
            try:
                if raw:
                    rets.append(float(raw) / 100)
                else:
                    pnl = (row.get(cols.get("pnl", ""), "") or "").strip().replace(",", "")
                    if not pnl:
                        continue
                    rets.append(float(pnl) / capital)
            except ValueError:
                raise BacktestError(f"{path} row {k}: not a number") from None
            for col in ("date", "exit_date"):
                v = (row.get(cols.get(col, ""), "") or "").strip()
                if v:
                    try:
                        days.append(ta.parse_ts(v).date())
                    except ValueError:
                        pass
    if any(not math.isfinite(r) or r <= -1 for r in rets):
        raise BacktestError(f"{path}: a trade return of -100% or worse cannot be resampled")
    years = (max(days) - min(days)).days / 365.25 if len(days) > 1 else None
    return rets, years


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------
def clean(x):
    """JSON-safe copy: NaN/Infinity -> null, dates -> ISO strings, floats rounded."""
    if isinstance(x, float):
        return round(x, 6) if math.isfinite(x) else None
    if isinstance(x, (date, datetime)):
        return x.isoformat()
    if isinstance(x, dict):
        return {str(k): clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [clean(v) for v in x]
    return x


def emit(obj) -> None:
    print(json.dumps(clean(obj), indent=2, allow_nan=False))


def fpct(x, signed: bool = True, d: int = 1) -> str:
    return "—" if x is None else (f"{x:+.{d}f}%" if signed else f"{x:.{d}f}%")


def fnum(x, d: int = 2) -> str:
    return "—" if x is None else f"{x:.{d}f}"


def rupees(x) -> str:
    return "—" if x is None else charges.inr(x, 0)


def pstr(p: dict) -> str:
    return " ".join(f"{k}={v:g}" if isinstance(v, (int, float)) else f"{k}={v}" for k, v in p.items())


def header(name: str, strategy: str, p: dict, cfg: Config, grid: dict | None = None) -> None:
    d = cfg.describe()
    fixed = {k: v for k, v in p.items() if not grid or k not in grid}
    print(f"\n{name}: {strategy} ({pstr(fixed)}" + (f"; grid over {', '.join(grid)})" if grid else ")"))
    lots = f" | lot {cfg.lot_size}" if cfg.lot_size > 1 else ""
    lev = f" | leverage {cfg.leverage:g}x" if cfg.leverage > 1 else ""
    print(f"{cfg.segment} on {d['exchange']} via {d['broker_name']} | capital {rupees(cfg.capital)} | {d['size_rule']}"
          f"{lots}{lev} | slippage {cfg.slippage_bps:g} bps per fill" + (" | shorts on" if cfg.allow_short else ""))


def print_run(r: dict, cfg: Config, show: int) -> None:
    m, bh = r["metrics"], r["buy_hold"]
    header(r["name"], r["strategy"], r["params"], cfg)
    print(f"{m['start_date']} → {m['end_date']} | {m['bars']} bars ({m['years']:.2f} y) | signal on close, fill next "
          f"open | charges as of {charges.RATES_AS_OF}")
    print(f"\n{'':<20}{'Strategy':>12}{'Buy & hold':>14}")
    rows = [("Total return", "total_return_pct", fpct), ("CAGR", "cagr_pct", fpct),
            ("Max drawdown", "max_drawdown_pct", fpct), ("Volatility", "volatility_pct", lambda x: fpct(x, False)),
            (f"Sharpe (rf {cfg.rf_pct:g}%)", "sharpe", fnum), ("Sortino", "sortino", fnum), ("Calmar", "calmar", fnum)]
    for label, key, f in rows:
        print(f"{label:<20}{f(m[key]):>12}{(f(bh[key]) if bh else '—'):>14}")
    print(f"\nTrades {m['trades']}: {m['wins']} win / {m['losses']} loss / {m['breakevens']} breakeven | win rate "
          f"{fpct(m['win_rate_pct'], False)} | avg win {rupees(m['avg_win'])} | avg loss {rupees(m['avg_loss'])}")
    pf = fnum(m["profit_factor"]) if m["profit_factor"] is not None else ("n/a (no losses)" if m["wins"] else "—")
    er = f" ({m['expectancy_r']:+.2f}R)" if m["expectancy_r"] is not None else ""
    print(f"Profit factor {pf} | expectancy {rupees(m['expectancy'])}/trade{er} | payoff {fnum(m['payoff_ratio'])} | "
          f"longest losing streak {m['longest_losing_streak']}")
    dd = (f"max DD {m['max_dd_peak']} → {m['max_dd_trough']}, "
          + (f"recovered {m['max_dd_recovery']}" if m["max_dd_recovery"] else "not recovered")) if m["max_dd_peak"] else "no drawdown"
    print(f"Exposure {fpct(m['exposure_pct'], False)} of bars | avg hold {fnum(m['avg_bars_held'], 1)} bars | {dd} | "
          f"longest underwater {m['longest_underwater_bars']} bars")
    cp = m["charges_pct_of_gross_profit"]
    print(f"Costs: charges {rupees(m['total_charges'])}" + (f" ({cp:.1f}% of gross profit)" if cp is not None else "")
          + f" | slippage {rupees(m['total_slippage'])}" + (f" | buy & hold paid {rupees(bh['charges'])}" if bh else ""))
    if r.get("regime"):
        print(f"Regime filter: {r['regime']} | entries blocked {m['blocked_by_regime']}")
    if r.get("tax"):
        t = r["tax"]
        print(f"Tax (approx.): {rupees(t['total_tax'])} → post-tax net {rupees(t['post_tax_net_pnl'])} "
              f"(pre-tax {rupees(t['pre_tax_net_pnl'])}), post-tax CAGR {fpct(t['post_tax_cagr_pct'])}. "
              "See tax.assumptions in --json.")
    if r["yearly"]:
        print(f"\n{'Year':<6}{'Strategy':>10}{'B&H':>10}{'Trades':>8}")
        for y in r["yearly"]:
            print(f"{y['year']:<6}{fpct(y['strategy_pct']):>10}{fpct(y['buy_hold_pct']):>10}{y['trades_closed']:>8}")
    trades = r["trades"][-show:] if show else []
    if trades:
        print(f"\nLast {len(trades)} of {len(r['trades'])} trades:")
        print(f"{'#':>3} {'side':<5} {'entry':<10} {'price':>10} {'exit':<10} {'price':>10} {'reason':<18}"
              f"{'qty':>7} {'net P&L':>12} {'R':>6}")
        for t in trades:
            rm = f"{t['r_multiple']:+.2f}" if t["r_multiple"] is not None else "—"
            print(f"{t['n']:>3} {t['side']:<5} {t['entry_date']:<10} {t['entry_price']:>10,.2f} {t['exit_date']:<10} "
                  f"{t['exit_price']:>10,.2f} {t['exit_reason'][:18]:<18}{t['qty']:>7} {rupees(t['net_pnl']):>12} {rm:>6}")
    if r["events"]:
        print(f"\nOrder events ({len(r['events'])}): " + "; ".join(f"{e['date']} {e['event']}: {e['reason']}"
                                                              for e in r["events"][:5]))
    for title, items in (("Red flags", r["red_flags"]), ("Data notes", r["notes"])):
        if items:
            print(f"\n{title}:")
            for x in items:
                print("  •", x)


def print_sweep(r: dict, cfg: Config, top: int) -> None:
    rb = r["robustness"]
    header(r["name"], r["strategy"], r["base_params"], cfg, r["grid"])
    keys = list(r["grid"])
    span = ", ".join(f"{k} {r['grid'][k]}" for k in keys)
    print(f"Sweep over {span} | metric {r['metric']} | "
          f"{rb['eligible_cells']}/{rb['cells']} cells with ≥{rb['min_trades']} trades")
    best_idx = rb["best"]["idx"] if rb["best"] else None
    if len(keys) == 2:
        k1, k2 = keys
        print(f"\n{r['metric']} ({k1} down, {k2} across; * best, x invalid, · too few trades)")
        print(f"{k1:>10} " + "".join(f"{v:>10g}" for v in r["grid"][k2]))
        by = {tuple(c["idx"]): c for c in r["cells"]}
        for i, v1 in enumerate(r["grid"][k1]):
            line = f"{v1:>10g} "
            for j in range(len(r["grid"][k2])):
                c = by[(i, j)]
                if not c["valid"]:
                    cellt = "x"
                elif c["score"] is None or c["trades"] < rb["min_trades"]:
                    cellt = "·"
                else:
                    cellt = f"{c['score']:.2f}" + ("*" if c["idx"] == best_idx else "")
                line += f"{cellt:>10}"
            print(line)
    ranked = sorted((c for c in r["cells"] if c["valid"] and c["score"] is not None),
                    key=lambda c: -c["score"])[:top]
    if ranked:
        print(f"\nTop {len(ranked)} cells:")
        print(f"  {'params':<28}{'trades':>7}{'CAGR':>9}{'maxDD':>9}{'Sharpe':>8}{'PF':>7}{r['metric']:>14}")
        for c in ranked:
            few = "  (too few trades)" if c["trades"] < rb["min_trades"] else ""
            print(f"  {pstr(c['params'])[:28]:<28}{c['trades']:>7}{fpct(c['cagr_pct']):>9}"
                  f"{fpct(c['max_drawdown_pct']):>9}{fnum(c['sharpe']):>8}{fnum(c['profit_factor']):>7}"
                  f"{fnum(c['score'], 3):>14}{few}")
    print("\nRobustness:")
    if rb["best"]:
        b = rb["best"]
        print(f"  best {pstr(b['params'])}: {r['metric']} {b['score']:.3f} on {b['trades']} trades")
        print(f"  neighbours {rb['neighbours_scored']}/{rb['neighbours']} scored, median {fnum(rb['neighbour_median'], 3)}, "
              f"min {fnum(rb['neighbour_min'], 3)}, ratio to best {fnum(rb['neighbour_ratio'])} | grid median "
              f"{fnum(rb['grid_median'], 3)} | profitable cells {fpct(rb['profitable_cells_pct'], False, 0)}")
        if rb["luck_hurdle_sharpe"] is not None:
            print(f"  luck hurdle: the best of {rb['eligible_cells']} zero-edge variants would show Sharpe ≈ "
                  f"{rb['luck_hurdle_sharpe']:.2f} over {b['years']:.1f} y by chance alone")
    print(f"  verdict: {rb['verdict']}")


def print_wf(r: dict, cfg: Config) -> None:
    header(r["name"], r["strategy"], r["base_params"], cfg, r["grid"])
    print(f"Walk-forward: {r['is_bars']} in-sample bars → {r['oos_bars']} out-of-sample bars, re-optimised on "
          f"{r['metric']} over {', '.join(r['grid'])}")
    print(f"\n{'IS window':<24}{'OOS window':<24}{'chosen':<26}{'IS':>8}{'OOS':>8}{'OOS ret':>9}{'trades':>7}")
    for w in r["windows"]:
        chosen = pstr(w["params"]) if w["params"] else "cash (none qualified)"
        print(f"{w['is_start'][2:] + '→' + w['is_end'][2:]:<24}{w['oos_start'][2:] + '→' + w['oos_end'][2:]:<24}"
              f"{chosen[:25]:<26}{fnum(w['is_score']):>8}{fnum(w['oos_score']):>8}{fpct(w['oos_return_pct']):>9}"
              f"{w['oos_trades']:>7}")
    o, bh, iv = r["oos"], r["buy_hold_oos"], r["is_vs_oos"]
    print(f"\nStitched out-of-sample {o['start_date']} → {o['end_date']}: return {fpct(o['total_return_pct'])}, "
          f"CAGR {fpct(o['cagr_pct'])}, max DD {fpct(o['max_drawdown_pct'])}, Sharpe {fnum(o['sharpe'])}, "
          f"{o['trades']} trades")
    if bh:
        print(f"Buy & hold over the same span: return {fpct(bh['total_return_pct'])}, CAGR {fpct(bh['cagr_pct'])}, "
              f"max DD {fpct(bh['max_drawdown_pct'])}")
    print(f"IS vs OOS: mean {r['metric']} {fnum(iv['mean_is_score'])} → {fnum(iv['mean_oos_score'])} | mean IS CAGR "
          f"{fpct(iv['mean_is_cagr_pct'])} → OOS {fpct(iv['oos_cagr_pct'])} | efficiency {fnum(iv['efficiency'])} | "
          f"param changes {iv['param_changes']} | profitable OOS windows {iv['profitable_oos_windows']}/{len(r['windows'])}")
    print(f"Verdict: {iv['verdict']}")


def print_mc(r: dict) -> None:
    mc = r["montecarlo"]
    src = r.get("source", "")
    print(f"\nMonte Carlo: {mc['sims']} {mc['method']} resamples of {mc['trades']} trades (seed {mc['seed']}){src}")
    print(f"{'':<22}{'5th':>14}{'50th':>14}{'95th':>14}")
    dd = mc["max_drawdown_pct"]
    print(f"{'Max drawdown':<22}" + "".join(f"{fpct(dd[k], False):>14}" for k in ("p5", "p50", "p95")))
    fe = mc["final_equity"]
    print(f"{'Final equity':<22}" + "".join(f"{rupees(fe[k]):>14}" for k in ("p5", "p50", "p95")))
    if mc["cagr_pct"]:
        print(f"{'CAGR':<22}" + "".join(f"{fpct(mc['cagr_pct'][k]):>14}" for k in ("p5", "p50", "p95")))
    print(f"Risk of ruin (drawdown ≥ {mc['ruin_dd_pct']:g}%): {mc['risk_of_ruin_pct']:.1f}% of paths | ending below "
          f"start: {mc['prob_loss_pct']:.1f}%")
    a = mc["actual"]
    extra = f"; bar-by-bar backtest max DD {fpct(r['backtest_max_dd_pct'])}" if r.get("backtest_max_dd_pct") is not None else ""
    print(f"Actual sequence: final {rupees(a['final_equity'])}, trade-to-trade max DD {fpct(a['max_drawdown_pct'], False)}{extra}")
    print("Drawdown percentiles: 95th is the bad tail to size for. Trade-level paths understate open-trade swings.")
    if mc["method"] == "shuffle":
        print("Shuffle only reorders trades, so final equity is the same on every path; use bootstrap for its spread.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _data_args(p, required: bool = True) -> None:
    p.add_argument("--csv", help="OHLCV CSV (Date, Open, High, Low, Close[, Volume])")
    p.add_argument("--candles", help="broker-MCP candle JSON")
    p.add_argument("--symbol", help="name, or fetch via yfinance when no file is given")
    p.add_argument("--exchange", default="NSE", choices=["NSE", "BSE"])
    p.add_argument("--period", default="10y", help="yfinance history period (default 10y)")
    p.add_argument("--start", help="first trading date YYYY-MM-DD (earlier bars still warm up indicators)")
    p.add_argument("--end", help="last date YYYY-MM-DD")
    p.add_argument("--strategy", required=required, choices=list(STRATEGIES))
    p.add_argument("--params", nargs="+", default=[], metavar="KEY=VALUE", help="override strategy defaults")


def _exec_args(p) -> None:
    p.add_argument("--capital", type=float, default=1_000_000.0, help="starting capital ₹ (default 10,00,000)")
    p.add_argument("--segment", choices=SEGMENTS, default="delivery")
    p.add_argument("--broker", choices=list(charges.BROKERS), default="zerodha")
    p.add_argument("--slippage-bps", type=float, default=5.0, help="adverse slippage per fill, basis points")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--size-pct", type=float, help="position = this %% of equity (default 100)")
    g.add_argument("--risk-pct", type=float, help="risk this %% of equity to the initial stop")
    g.add_argument("--fixed-inr", type=float, help="fixed ₹ position value per trade")
    p.add_argument("--lot-size", type=int, default=1, help="trade whole lots of this many units (futures, SME)")
    p.add_argument("--leverage", type=float, default=1.0, help="max notional / equity (intraday, futures)")
    p.add_argument("--allow-short", action="store_true", help="take short signals (futures only)")
    p.add_argument("--circuit-band", type=float, help="price band %% (2/5/10/20): no fill when the open is at the limit")
    p.add_argument("--regime-csv", help="benchmark CSV (e.g. NIFTY): longs only above its SMA, shorts only below")
    p.add_argument("--regime-symbol", help="benchmark via yfinance, e.g. NIFTY")
    p.add_argument("--regime-ma", type=int, default=200, help="benchmark SMA length for --regime-* (default 200)")
    p.add_argument("--rf", type=float, default=6.5, help="risk-free rate, %% a year, for Sharpe/Sortino (default 6.5)")
    p.add_argument("--json", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Backtest lab for NSE/BSE daily-bar strategies with Indian costs")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="one backtest with metrics and the trade list")
    _data_args(r)
    _exec_args(r)
    r.add_argument("--trades-csv", metavar="OUT", help="write the trade list (trading-discipline journal columns)")
    r.add_argument("--show-trades", type=int, default=10, help="trades to print (default 10, 0 = none)")
    r.add_argument("--tax", action="store_true", help="add an approximate post-tax view")
    r.add_argument("--slab-pct", type=float, default=30.0, help="slab rate for intraday/futures income (default 30)")
    s = sub.add_parser("sweep", help="parameter grid with a plateau-or-spike read")
    _data_args(s)
    _exec_args(s)
    s.add_argument("--grid", nargs="+", required=True, metavar="KEY=V1,V2")
    s.add_argument("--metric", choices=METRICS, default="sharpe")
    s.add_argument("--min-trades", type=int, default=5, help="cells with fewer trades cannot be 'best'")
    s.add_argument("--top", type=int, default=10)
    w = sub.add_parser("walkforward", help="rolling in-sample optimisation, out-of-sample test")
    _data_args(w)
    _exec_args(w)
    w.add_argument("--grid", nargs="+", required=True, metavar="KEY=V1,V2")
    w.add_argument("--metric", choices=METRICS, default="sharpe")
    w.add_argument("--min-trades", type=int, default=5)
    w.add_argument("--is-bars", type=int, default=756, help="in-sample bars per window (default 756 ≈ 3 y)")
    w.add_argument("--oos-bars", type=int, default=252, help="out-of-sample bars per window (default 252 ≈ 1 y)")
    m = sub.add_parser("montecarlo", help="resample the trade sequence")
    _data_args(m, required=False)
    _exec_args(m)
    m.add_argument("--from-trades", metavar="CSV", help="resample a trades CSV (return_pct or pnl column) instead")
    m.add_argument("--sims", type=int, default=1000)
    m.add_argument("--seed", type=int, default=42)
    m.add_argument("--method", choices=["bootstrap", "shuffle"], default="bootstrap")
    m.add_argument("--ruin-dd", type=float, default=30.0, help="drawdown %% that counts as ruin (default 30)")
    ls = sub.add_parser("strategies", help="list built-in strategies")
    ls.add_argument("--json", action="store_true")
    return p


def config_from_args(a) -> Config:
    if a.risk_pct is not None:
        sizing, val = "risk", a.risk_pct
    elif a.fixed_inr is not None:
        sizing, val = "fixed", a.fixed_inr
    else:
        sizing, val = "pct", 100.0 if a.size_pct is None else a.size_pct
    return Config(capital=a.capital, segment=a.segment, broker=a.broker, exchange=a.exchange,
                  slippage_bps=a.slippage_bps, sizing=sizing, size_value=val, lot_size=a.lot_size,
                  leverage=a.leverage, allow_short=a.allow_short, circuit_band=a.circuit_band, rf_pct=a.rf)


def load_from_args(a) -> Series:
    skipped = 0
    if a.csv:
        if not Path(a.csv).is_file():
            raise BacktestError(f"file not found: {a.csv}")
        rows, name = ta.load_csv(a.csv), a.symbol or Path(a.csv).stem
    elif a.candles:
        if not Path(a.candles).is_file():
            raise BacktestError(f"file not found: {a.candles}")
        rows, skipped = load_candles(a.candles)
        name = a.symbol or Path(a.candles).stem
    elif a.symbol:
        rows, name = ta.fetch_yf(a.symbol, a.exchange, a.period, "1d"), ta.yf_ticker(a.symbol, a.exchange)
    else:
        raise BacktestError("give --csv, --candles or --symbol")
    s = build_series(rows, name, a.end, skipped)
    if a.regime_csv or a.regime_symbol:
        if a.regime_csv:
            if not Path(a.regime_csv).is_file():
                raise BacktestError(f"file not found: {a.regime_csv}")
            bench, label = ta.load_csv(a.regime_csv), Path(a.regime_csv).stem
        else:
            bench, label = ta.fetch_yf(a.regime_symbol, a.exchange, a.period, "1d"), a.regime_symbol.upper()
        attach_regime(s, bench, a.regime_ma, label)
    return s


def write_trades(path: str, trades: list, symbol: str, strategy: str, segment: str) -> None:
    cols = ["date", "exit_date", "symbol", "side", "entry", "exit", "stop", "qty", "charges", "pnl", "setup",
            "segment", "exit_reason", "bars_held", "gross_pnl", "slippage", "return_pct", "r_multiple"]
    r2 = lambda x, d=2: "" if x is None else round(x, d)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for t in trades:
            w.writerow([t["entry_date"], t["exit_date"], symbol, t["side"], r2(t["entry_price"], 4),
                        r2(t["exit_price"], 4), r2(t["stop"], 4), t["qty"], r2(t["charges"]), r2(t["net_pnl"]),
                        strategy, segment, t["exit_reason"], t["bars_held"], r2(t["gross_pnl"]),
                        r2(t["slippage"]), r2(t["return_pct"], 6), r2(t["r_multiple"], 4)])


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    try:
        return _run(a)
    except SystemExit as e:              # fetch_yf reports a missing yfinance / bad symbol this way
        if isinstance(e.code, str):
            print(f"error: {e.code}", file=sys.stderr)
            return 2
        raise
    except (ValueError, OSError, KeyError, ZeroDivisionError, OverflowError) as e:
        msg = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
        print(f"error: {msg}", file=sys.stderr)
        return 2


def _run(a) -> int:
    if a.cmd == "strategies":
        if a.json:
            emit({k: {"idea": v["idea"], "defaults": v["defaults"]} for k, v in STRATEGIES.items()})
            return 0
        for k, v in STRATEGIES.items():
            print(f"\n{k}\n  {v['idea']}\n  defaults: {pstr(v['defaults'])}")
        print("\nRisk keys on every strategy: stop_atr (k × ATR14), stop_pct (overrides stop_atr), target_r, "
              "max_hold (bars; 0 = off).")
        return 0

    cfg = config_from_args(a)
    if a.cmd == "montecarlo" and a.from_trades:
        rets, years = returns_from_csv(a.from_trades, cfg.capital)
        mc = monte_carlo(rets, cfg.capital, a.sims, a.seed, a.method, a.ruin_dd, years)
        out = {"command": "montecarlo", "source": f" from {Path(a.from_trades).name}", "montecarlo": mc}
        emit(out) if a.json else print_mc(out)
        return 0
    if not a.strategy:
        raise BacktestError("give --strategy (or --from-trades CSV for montecarlo)")

    s = load_from_args(a)
    start = start_index(s, a.start)
    p = make_params(a.strategy, a.params, check=a.cmd in ("run", "montecarlo"))
    err = combo_error(cfg, p)
    if a.cmd in ("run", "montecarlo") and err:
        raise BacktestError(err)

    if a.cmd == "run":
        if not (math.isfinite(a.slab_pct) and 0 <= a.slab_pct <= 50):
            raise BacktestError("--slab-pct must be between 0 and 50")
        res, m = run_one(s, a.strategy, p, cfg, start)
        bh = buy_and_hold(s, cfg, start, s.n, cfg.capital)
        out = {"command": "run", "name": s.name, "strategy": a.strategy, "params": p, "config": cfg.describe(),
               "metrics": m, "buy_hold": bh, "yearly": yearly(res, (bh_curve(s, cfg, start, s.n, cfg.capital) or (None,))[0]),
               "trades": res["trades"], "events": res["events"], "regime": s.regime_label,
               "red_flags": red_flags(m, bh, cfg), "notes": s.notes,
               "assumptions": ["signal on bar close, fill at the next bar's open", "stops/targets: gap fills at the open; "
                               "stop wins when one bar hits both", "open position closed at the last close",
                               "idle cash earns nothing (Sharpe subtracts the risk-free rate)"]}
        if a.tax:
            out["tax"] = tax_estimate(res["trades"], cfg.segment, a.slab_pct, cfg.capital, m["years"])
        if a.trades_csv:
            write_trades(a.trades_csv, res["trades"], s.name, a.strategy, cfg.segment)
            out["trades_csv"] = a.trades_csv
        if a.json:
            emit(out)
        else:
            print_run(out, cfg, max(0, a.show_trades))
            if a.trades_csv:
                print(f"\nWrote {len(res['trades'])} trades to {a.trades_csv}")
        return 0

    if a.cmd in ("sweep", "walkforward"):
        if a.min_trades < 0:
            raise BacktestError("--min-trades must be ≥ 0")
        grid = parse_grid(a.strategy, a.grid)
        if a.cmd == "sweep":
            r = sweep(s, a.strategy, p, grid, cfg, a.metric, start, s.n, a.min_trades)
        else:
            r = walkforward(s, a.strategy, p, grid, cfg, a.metric, a.is_bars, a.oos_bars, a.min_trades, start)
        if a.cmd == "sweep" and not any(c["valid"] for c in r["cells"]):
            raise BacktestError("no valid cell in the grid: " + "; ".join(sorted({c["reason"] for c in r["cells"]})))
        r.update({"command": a.cmd, "name": s.name, "strategy": a.strategy, "config": cfg.describe(),
                  "notes": s.notes})
        if a.json:
            emit(r)
        elif a.cmd == "sweep":
            print_sweep(r, cfg, max(1, a.top))
        else:
            print_wf(r, cfg)
        return 0

    # montecarlo on a fresh backtest
    res, m = run_one(s, a.strategy, p, cfg, start)
    rets = [t["return_pct"] / 100 for t in res["trades"] if t["return_pct"] is not None]
    mc = monte_carlo(rets, cfg.capital, a.sims, a.seed, a.method, a.ruin_dd, m["years"])
    out = {"command": "montecarlo", "name": s.name, "strategy": a.strategy, "params": p, "config": cfg.describe(),
           "source": f" from {s.name} {a.strategy}", "backtest_max_dd_pct": m["max_drawdown_pct"],
           "backtest_final_equity": m["final_equity"], "montecarlo": mc, "notes": s.notes}
    emit(out) if a.json else print_mc(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
