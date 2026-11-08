#!/usr/bin/env python3
"""Market-wide breadth for NSE/BSE: how many stocks are actually taking part in the index's move.

Zero dependencies (Python 3.10+ stdlib). Optional: `pip install yfinance` for --symbols/--universe-file.
Reuses the loaders and moving averages from ../../technical-analysis/scripts/ta.py.

Input (pick one)
  --csv-dir DIR         one daily OHLCV CSV per stock, file stem = symbol (--pattern to filter;
                        --universe-file restricts the folder to a constituent list)
  --symbols A,B,...     fetch daily bars from Yahoo (.NS / .BO) via yfinance
  --universe-file FILE  constituent list (niftyindices.com CSV with a Symbol column, or one
                        symbol per line); fetched via yfinance unless --csv-dir is also given
  --ad-csv FILE         exchange-wide daily advances/declines (+ optional unchanged, new 52-week
                        highs/lows and an index close column): breadth without per-stock data

Optional: --members FILE (symbol,sector) for sector participation, --benchmark-csv FILE or
--benchmark NAME (yfinance) for the A/D-vs-index divergence check.

Sub-commands
  status    latest session: % above 20/50/200 DMA, uptrend ratio, advance/decline and A/D line,
            divergence vs the index, new 52-week highs/lows, McClellan oscillator and summation
            index, breadth thrust, sector participation and a 0-100 composite health score
  history   the same metrics for the last N sessions (table, --json or --out CSV for plotting)

Examples
  python3 breadth.py status --csv-dir nifty500/ --benchmark-csv "NIFTY 50.csv" --members ind_nifty500list.csv
  python3 breadth.py status --csv-dir nifty500/ --universe-file ind_nifty50list.csv --benchmark-csv nifty.csv
  python3 breadth.py status --ad-csv nse_adv_dec.csv --json
  python3 breadth.py history --csv-dir stocks/ --sessions 60 --out breadth.csv
"""
from __future__ import annotations

import argparse
import bisect
import csv
import importlib.util
import json
import math
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_TA_PATH = Path(__file__).resolve().parents[2] / "technical-analysis" / "scripts" / "ta.py"
_spec = importlib.util.spec_from_file_location("market_breadth_ta", _TA_PATH)
ta = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ta)

IST = timezone(timedelta(hours=5, minutes=30))
DMAS = (20, 50, 200)
DMA_WEIGHTS = {20: 0.2, 50: 0.4, 200: 0.4}
HL_WINDOW = 252            # sessions in a "52-week" high/low look-back (today included)
HL_SUM = 10                # sessions summed for the new-high/new-low component
MCC_FAST, MCC_SLOW = 19, 39   # EMA smoothing constants 0.10 and 0.05
THRUST_EMA, THRUST_LOW, THRUST_HIGH, THRUST_SPAN = 10, 0.40, 0.615, 10
DIV_WINDOW, DIV_MIN_PCT = 20, 1.0
SECTOR_MIN_PCT = 50.0      # a sector "participates" when at least this % of members are above 50 DMA
CIRCUIT_MIN_MOVE = 1.9     # % move on a zero-range bar that suggests a circuit-locked stock
UNMAPPED = "UNMAPPED"
NO_YF = "yfinance not installed: pip install yfinance (or work offline with --csv-dir / --ad-csv)"

# Composite health score: component -> weight (points out of 100 when every input is available)
WEIGHTS = {"participation": 25, "uptrend_ratio": 15, "participation_trend": 10, "ad_line": 15,
           "mcclellan": 10, "highs_lows": 15, "sectors": 10}
LABELS = ((75, "Broad participation"), (60, "Healthy"), (40, "Mixed"), (25, "Narrow / weak"),
          (0, "Very weak"))


class BreadthError(Exception):
    """Bad input the user can fix; reported as one `error:` line with exit code 2."""


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
DATE_COLS = ("date", "indexdate", "tradedate", "timestamp", "datetime", "day")
CLOSE_COLS = ("close", "closingindexvalue", "closeprice", "closingprice", "ltp", "price", "value")
SYMBOL_COLS = ("symbol", "ticker", "tradingsymbol", "stock", "nsesymbol", "nsecode", "scrip")
SECTOR_COLS = ("sector", "industry", "basicindustry", "index", "group")
AD_COLS = {
    "adv": ("advances", "advance", "adv", "advancing", "advancers", "up", "gainers"),
    "dec": ("declines", "decline", "dec", "declining", "decliners", "down", "losers"),
    "unch": ("unchanged", "unch", "unchg", "nochange", "flat"),
    "nh": ("new52whighs", "new52weekhighs", "newhighs", "52whighs", "52weekhighs", "52whigh",
           "52weekhigh", "highs", "nh"),
    "nl": ("new52wlows", "new52weeklows", "newlows", "52wlows", "52weeklows", "52wlow",
           "52weeklow", "lows", "nl"),
    "index": ("nifty50", "nifty", "nifty500", "niftyclose", "index", "indexclose", "closingindexvalue",
              "sensex", "benchmark", "close"),
}


def _key(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def norm_symbol(s: str) -> str:
    s = (s or "").strip().upper()
    for suffix in (".NS", ".BO", "-EQ"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    return s


def _columns(fieldnames) -> dict:
    return {_key(c): c for c in fieldnames or [] if c and _key(c)}


def _pick(cols: dict, names) -> str | None:
    return next((cols[n] for n in names if n in cols), None)


def _parse_date(raw: str) -> date:
    try:
        return ta.parse_ts(raw).date()
    except ValueError:
        for fmt in ("%b %d, %Y", "%d-%b-%y", "%d %b %y", "%d-%B-%Y", "%d %B %Y"):
            try:
                return datetime.strptime(raw.strip(), fmt).date()
            except ValueError:
                continue
    raise ValueError(raw)


def _num(x) -> float:
    v = ta._f(x if x is not None else "")
    return v if math.isfinite(v) else math.nan


def load_closes(path) -> list:
    """[(date, close)] from any CSV with a date and a close column (index exports, close-only files)."""
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        cols = _columns(reader.fieldnames)
        cd, cc = _pick(cols, DATE_COLS), _pick(cols, CLOSE_COLS)
        if not cd or not cc:
            raise BreadthError(f"{Path(path).name}: need a date and a close column; found {list(cols)}")
        out = {}
        for r in reader:
            raw = (r.get(cd) or "").strip()
            if not raw:
                continue
            try:
                d, c = _parse_date(raw), _num(r.get(cc))
            except ValueError:
                continue
            if math.isfinite(c) and c > 0:
                out[d] = c
    return sorted(out.items())


def _daily(rows: list, name: str) -> list:
    """Keep finite, positive OHLC bars as {date, high, low, close}; reject intraday files."""
    out = []
    for r in rows:
        vals = [r.get(k) for k in ("open", "high", "low", "close")]
        if not all(isinstance(v, (int, float)) and math.isfinite(v) and v > 0 for v in vals):
            continue
        d = r["date"].date() if isinstance(r["date"], datetime) else r["date"]
        out.append({"date": d, "high": max(vals), "low": min(vals), "close": float(r["close"])})
    if len({r["date"] for r in out}) < len(out):
        raise BreadthError(f"{name}: several bars per day; breadth needs daily bars")
    return out


def load_stock_csv(path: Path) -> list:
    try:
        rows = ta.load_csv(str(path))
    except ValueError as e:
        if "CSV needs" not in str(e):
            raise BreadthError(f"{path.name}: {e}")
        rows = [{"date": d, "open": c, "high": c, "low": c, "close": c} for d, c in load_closes(path)]
    return _daily(rows, path.name)


def load_members(path: str) -> dict:
    """symbol -> sector from a CSV with symbol and sector/industry columns (any order)."""
    p = Path(path)
    if not p.is_file():
        raise BreadthError(f"--members {path}: file not found")
    with open(p, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        cols = _columns(reader.fieldnames)
        cy, cs = _pick(cols, SYMBOL_COLS), _pick(cols, SECTOR_COLS)
        if not cy or not cs:
            raise BreadthError(f"--members needs 'symbol' and 'sector' (or 'industry') columns; found {list(cols)}")
        out = {}
        for r in reader:
            sym, sec = norm_symbol(r.get(cy)), (r.get(cs) or "").strip().upper()
            if sym and sec:
                out[sym] = sec
    if not out:
        raise BreadthError(f"--members {path}: no symbol/sector rows")
    return out


def load_universe_file(path: str) -> tuple:
    """(symbols, sectors) from a constituent CSV with a Symbol column, or one symbol per line."""
    p = Path(path)
    if not p.is_file():
        raise BreadthError(f"--universe-file {path}: file not found")
    text = p.read_text(encoding="utf-8-sig")
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    if not lines:
        raise BreadthError(f"--universe-file {path}: empty")
    header = _columns(next(csv.reader([lines[0]])))
    symbols, sectors = [], {}
    if _pick(header, SYMBOL_COLS):
        reader = csv.DictReader(lines)
        cols = _columns(reader.fieldnames)
        cy, cs = _pick(cols, SYMBOL_COLS), _pick(cols, SECTOR_COLS)
        for r in reader:
            sym = norm_symbol(r.get(cy))
            if sym:
                symbols.append(sym)
                if cs and (r.get(cs) or "").strip():
                    sectors[sym] = r[cs].strip().upper()
    else:
        for row in csv.reader(lines):
            if row and norm_symbol(row[0]):
                symbols.append(norm_symbol(row[0]))
    symbols = list(dict.fromkeys(symbols))
    if not symbols:
        raise BreadthError(f"--universe-file {path}: no symbols")
    return symbols, sectors


def universe_from_dir(directory: str, pattern: str, exclude=None, only=None) -> tuple:
    d = Path(directory)
    if not d.is_dir():
        raise BreadthError(f"--csv-dir {directory}: not a folder")
    files = {}
    for f in sorted(d.glob(pattern)):
        if f.is_file() and f.suffix.lower() == ".csv" and not (exclude and f.resolve() == exclude):
            files.setdefault(norm_symbol(f.stem), f)
    if not files:
        raise BreadthError(f"--csv-dir {directory}: no CSV files match {pattern!r}")
    missing = []
    if only is not None:
        missing = [s for s in only if s not in files]
        files = {s: files[s] for s in only if s in files}
        if not files:
            raise BreadthError(f"--csv-dir {directory}: none of the --universe-file symbols has a CSV")
    stocks, errors = {}, []
    for sym, f in files.items():
        try:
            rows = load_stock_csv(f)
        except (BreadthError, OSError, UnicodeDecodeError, csv.Error) as e:
            errors.append({"symbol": sym, "error": str(e)})
            continue
        if len(rows) < 2:
            errors.append({"symbol": sym, "error": "fewer than 2 usable bars"})
            continue
        stocks[sym] = rows
    return stocks, errors, missing


def universe_from_yf(symbols: list, exchange: str, period: str, save_dir: str | None) -> tuple:
    stocks, errors = {}, []
    out_dir = Path(save_dir) if save_dir else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
    for sym in symbols:
        try:
            raw = ta.fetch_yf(sym, exchange, period, "1d")
            rows = _daily(raw, sym)
        except SystemExit as e:
            if not isinstance(e.code, str):
                raise
            if "not installed" in e.code:
                raise BreadthError(NO_YF)
            errors.append({"symbol": sym, "error": e.code})
            continue
        except (BreadthError, ValueError, KeyError) as e:
            errors.append({"symbol": sym, "error": str(e)})
            continue
        if len(rows) < 2:
            errors.append({"symbol": sym, "error": "fewer than 2 usable bars"})
            continue
        stocks[sym] = rows
        if out_dir:
            with open(out_dir / f"{sym}.csv", "w", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(["Date", "Open", "High", "Low", "Close", "Volume"])
                for r in raw:
                    w.writerow([r["date"].date().isoformat(), r["open"], r["high"], r["low"], r["close"],
                                int(r["volume"]) if math.isfinite(r["volume"]) else 0])
    return stocks, errors


def load_ad_csv(path: str) -> tuple:
    """Rows of {date, adv, dec, unch, nh, nl, index} and the optional columns found."""
    p = Path(path)
    if not p.is_file():
        raise BreadthError(f"--ad-csv {path}: file not found")
    with open(p, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        cols = _columns(reader.fieldnames)
        cd = _pick(cols, DATE_COLS)
        found = {k: _pick(cols, names) for k, names in AD_COLS.items()}
        if not cd or not found["adv"] or not found["dec"]:
            raise BreadthError(f"--ad-csv needs date, advances and declines columns; found {list(cols)}")
        out = {}
        for r in reader:
            raw = (r.get(cd) or "").strip()
            if not raw:
                continue
            try:
                d = _parse_date(raw)
            except ValueError:
                continue
            a, dc = _num(r.get(found["adv"])), _num(r.get(found["dec"]))
            if not (math.isfinite(a) and math.isfinite(dc)) or a < 0 or dc < 0:
                continue
            row = {"date": d, "adv": int(a), "dec": int(dc)}
            for k in ("unch", "nh", "nl", "index"):
                v = _num(r.get(found[k])) if found[k] else math.nan
                ok = math.isfinite(v) and v >= 0
                row[k] = (v if k == "index" else int(round(v))) if ok else None
            out[d] = row
    rows = [out[d] for d in sorted(out)]
    if len(rows) < 2:
        raise BreadthError(f"--ad-csv {path}: fewer than 2 usable rows")
    return rows, {k: v for k, v in found.items() if v}


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------
def mcclellan(adv: list, dec: list, fast: int = MCC_FAST, slow: int = MCC_SLOW) -> tuple:
    """Ratio-adjusted McClellan oscillator and summation index.

    RANA = (A − D) / (A + D) × 1000; oscillator = EMA_fast(RANA) − EMA_slow(RANA) (EMAs seeded with
    an SMA, so 19/39 give smoothing constants 0.10/0.05); summation = running sum of the oscillator."""
    rana = [(a - d) / (a + d) * 1000 if a + d else 0.0 for a, d in zip(adv, dec)]
    ef, es = ta.ema(rana, fast), ta.ema(rana, slow)
    osc = [f - s if f is not None and s is not None else None for f, s in zip(ef, es)]
    summ, run = [], None
    for o in osc:
        if o is not None:
            run = (run or 0.0) + o
        summ.append(run)
    return rana, osc, summ


def thrust_ema(adv: list, dec: list, n: int = THRUST_EMA) -> list:
    """n-session EMA of A / (A + D); a session with no advances or declines counts as 0.5."""
    return ta.ema([a / (a + d) if a + d else 0.5 for a, d in zip(adv, dec)], n)


def thrust_events(ema_vals: list, low: float = THRUST_LOW, high: float = THRUST_HIGH,
                  span: int = THRUST_SPAN) -> list:
    """Indices where the EMA crosses above `high` having been below `low` within the prior `span` sessions."""
    out = []
    for t in range(1, len(ema_vals)):
        e, prev = ema_vals[t], ema_vals[t - 1]
        if e is None or prev is None or not (e > high >= prev):
            continue
        window = [x for x in ema_vals[max(0, t - span):t] if x is not None]
        if window and min(window) < low:
            out.append(t)
    return out


def _prepare(rows: list, hl_window: int) -> dict:
    c = [r["close"] for r in rows]
    h = [r["high"] for r in rows]
    lo = [r["low"] for r in rows]
    nh, nl = [None] * len(c), [None] * len(c)
    for i in range(hl_window - 1, len(c)):
        nh[i] = h[i] > max(h[i - hl_window + 1:i])
        nl[i] = lo[i] < min(lo[i - hl_window + 1:i])
    return {"idx": {r["date"]: i for i, r in enumerate(rows)}, "first": rows[0]["date"],
            "last": rows[-1]["date"], "c": c, "h": h, "l": lo,
            "sma": {n: ta.sma(c, n) for n in DMAS}, "nh": nh, "nl": nl}


def session_calendar(prepped: dict) -> list:
    """Dates on which at least half of the stocks listed by then have a bar. Drops stray rows and a
    trailing date that only a few freshly refreshed files reach."""
    counts = {}
    for p in prepped.values():
        for d in p["idx"]:
            counts[d] = counts.get(d, 0) + 1
    firsts = sorted(p["first"] for p in prepped.values())
    return [d for d in sorted(counts) if counts[d] * 2 >= bisect.bisect_right(firsts, d)]


def _pct(above: int, n: int):
    return above / n * 100 if n else None


def _align(bench: list | None, dates: list) -> list:
    """Benchmark closes aligned to the sessions, carrying the last close over gaps."""
    if not bench:
        return [None] * len(dates)
    bmap = dict(bench)
    keys = sorted(bmap)
    out = []
    for d in dates:
        j = bisect.bisect_right(keys, d) - 1
        out.append(bmap[keys[j]] if j >= 0 else None)
    return out


def universe_series(stocks: dict, members: dict | None, bench: list | None, hl_window: int) -> dict:
    prepped = {s: _prepare(rows, hl_window) for s, rows in stocks.items()}
    dates = session_calendar(prepped)
    keys = ("adv", "dec", "unch", "active", "nh", "nl", "n_hl", "uptrend", "n_up", "circuit", "sectors",
            "sector_pct") + tuple(f"pct{n}" for n in DMAS) + tuple(f"n{n}" for n in DMAS)
    S = {k: [] for k in keys}
    for d in dates:
        adv = dec = unch = active = nh = nl = n_hl = up = n_up = 0
        dma = {n: [0, 0] for n in DMAS}
        circuit, sec = [], {}
        for sym, p in prepped.items():
            i = p["idx"].get(d)
            if i is None:
                continue
            active += 1
            c = p["c"]
            if i > 0:
                ch = c[i] - c[i - 1]
                if abs(ch) <= 1e-9 * c[i - 1]:
                    unch += 1
                elif ch > 0:
                    adv += 1
                else:
                    dec += 1
                if p["h"][i] == p["l"][i] and abs(ch / c[i - 1]) * 100 >= CIRCUIT_MIN_MOVE:
                    circuit.append(sym)
            for n in DMAS:
                s = p["sma"][n][i]
                if s is not None:
                    dma[n][0] += 1
                    dma[n][1] += c[i] > s
            s50, s200 = p["sma"][50][i], p["sma"][200][i]
            if s50 is not None and s200 is not None:
                n_up += 1
                up += c[i] > s50 > s200
            if p["nh"][i] is not None:
                n_hl += 1
                nh += p["nh"][i]
                nl += p["nl"][i]
            if members is not None:
                g = sec.setdefault(members.get(sym, UNMAPPED), {"n50": 0, "a50": 0, "n200": 0, "a200": 0,
                                                                "adv": 0, "dec": 0})
                if s50 is not None:
                    g["n50"] += 1
                    g["a50"] += c[i] > s50
                if s200 is not None:
                    g["n200"] += 1
                    g["a200"] += c[i] > s200
                if i > 0:
                    g["adv"] += c[i] > c[i - 1]
                    g["dec"] += c[i] < c[i - 1]
        for n in DMAS:
            S[f"pct{n}"].append(_pct(dma[n][1], dma[n][0]))
            S[f"n{n}"].append(dma[n][0])
        for k, v in (("adv", adv), ("dec", dec), ("unch", unch), ("active", active), ("n_hl", n_hl),
                     ("n_up", n_up), ("circuit", circuit)):
            S[k].append(v)
        S["nh"].append(nh if n_hl else None)
        S["nl"].append(nl if n_hl else None)
        S["uptrend"].append(_pct(up, n_up))
        if members is None:
            S["sectors"].append(None)
            S["sector_pct"].append(None)
        else:
            S["sectors"].append(sec)
            graded = [g for name, g in sec.items() if name != UNMAPPED and g["n50"]]
            part = sum(g["a50"] / g["n50"] * 100 >= SECTOR_MIN_PCT for g in graded)
            S["sector_pct"].append(_pct(part, len(graded)))
    S["date"] = dates
    # The first session has no previous close, so no advance/decline reading: drop it.
    while S["date"] and S["adv"][0] + S["dec"][0] + S["unch"][0] == 0:
        for k in S:
            S[k].pop(0)
    S["bench"] = _align(bench, S["date"])
    S["bench_last"] = bench[-1][0] if bench else None
    S["last_bar"] = {s: p["last"] for s, p in prepped.items()}
    return S


def ad_series(rows: list, bench: list | None) -> dict:
    n = len(rows)
    S = {"date": [r["date"] for r in rows], "adv": [r["adv"] for r in rows],
         "dec": [r["dec"] for r in rows], "unch": [r["unch"] or 0 for r in rows],
         "nh": [r["nh"] for r in rows], "nl": [r["nl"] for r in rows],
         "uptrend": [None] * n, "n_up": [0] * n, "circuit": [[] for _ in rows],
         "sectors": [None] * n, "sector_pct": [None] * n, "last_bar": {}}
    S["active"] = [a + d + u for a, d, u in zip(S["adv"], S["dec"], S["unch"])]
    S["n_hl"] = [None] * n          # exchange files give counts, not the number of eligible stocks
    for t, r in enumerate(rows):
        if r["nh"] is None or r["nl"] is None:
            S["nh"][t] = S["nl"][t] = None
    for k in DMAS:
        S[f"pct{k}"] = [None] * n
        S[f"n{k}"] = [0] * n
    S["bench"] = _align(bench, S["date"]) if bench else [r["index"] for r in rows]
    S["bench_last"] = bench[-1][0] if bench else None
    return S


# ---------------------------------------------------------------------------
# Derived series, score and snapshot
# ---------------------------------------------------------------------------
def _lin(x: float, lo: float, hi: float) -> float:
    return max(0.0, min(100.0, (x - lo) / (hi - lo) * 100))


def _chg(series: list, t: int, back: int):
    if t - back < 0 or series[t] is None or series[t - back] is None:
        return None
    return series[t] - series[t - back]


def divergence_at(S: dict, t: int, window: int, min_pct: float) -> dict | None:
    b = S["bench"]
    if t - window < 0 or b[t] is None or b[t - window] is None:
        return None
    ret = (b[t] / b[t - window] - 1) * 100
    ad_chg = S["ad_line"][t] - S["ad_line"][t - window]
    if ret >= min_pct and ad_chg < 0:
        flag = "bearish"
    elif ret <= -min_pct and ad_chg > 0:
        flag = "bullish"
    else:
        flag = "none"
    return {"window": window, "index_return_pct": ret, "ad_line_change": ad_chg, "flag": flag}


def components_at(S: dict, t: int, window: int) -> dict:
    comp = {}
    parts = [(DMA_WEIGHTS[n], S[f"pct{n}"][t]) for n in DMAS if S[f"pct{n}"][t] is not None]
    if parts:
        val = sum(w * v for w, v in parts) / sum(w for w, _ in parts)
        comp["participation"] = (val, "weighted % above 20/50/200 DMA (0.2/0.4/0.4)")
    if S["uptrend"][t] is not None:
        comp["uptrend_ratio"] = (_lin(S["uptrend"][t], 10, 60), f"uptrend ratio {S['uptrend'][t]:.1f}% (10%→0, 60%→100)")
    d20 = _chg(S["pct50"], t, 20)
    if d20 is not None:
        comp["participation_trend"] = (_lin(d20, -25, 25), f"% above 50 DMA {d20:+.1f} pts over 20 sessions")
    if t + 1 >= window:
        a = sum(S["adv"][t - window + 1:t + 1])
        d = sum(S["dec"][t - window + 1:t + 1])
        if a + d:
            r = (a - d) / (a + d)
            comp["ad_line"] = (_lin(r, -0.25, 0.25), f"{window}-session net advances {r:+.2f} of issues")
    if S["osc"][t] is not None:
        comp["mcclellan"] = (_lin(S["osc"][t], -100, 100), f"oscillator {S['osc'][t]:+.1f} (−100→0, +100→100)")
    lo = max(0, t - HL_SUM + 1)
    hs = [x for x in S["nh"][lo:t + 1] if x is not None]
    ls = [x for x in S["nl"][lo:t + 1] if x is not None]
    if hs and ls:
        tot = sum(hs) + sum(ls)
        comp["highs_lows"] = (sum(hs) / tot * 100 if tot else 50.0,
                              f"{HL_SUM}-session new highs {sum(hs)} vs lows {sum(ls)}")
    if S["sector_pct"][t] is not None:
        comp["sectors"] = (S["sector_pct"][t], f"sectors with ≥{SECTOR_MIN_PCT:.0f}% of members above 50 DMA")
    return comp


def score_from(comp: dict) -> tuple:
    if not comp:
        return None, None, 0
    w = sum(WEIGHTS[k] for k in comp)
    score = sum(WEIGHTS[k] * v for k, (v, _) in comp.items()) / w
    label = next(lbl for cut, lbl in LABELS if score >= cut)
    return score, label, w


def derive(S: dict, window: int) -> dict:
    S["net"] = [a - d for a, d in zip(S["adv"], S["dec"])]
    run, line = 0, []
    for x in S["net"]:
        run += x
        line.append(run)
    S["ad_line"] = line
    S["rana"], S["osc"], S["summation"] = mcclellan(S["adv"], S["dec"])
    S["thrust_ema"] = thrust_ema(S["adv"], S["dec"])
    S["thrusts"] = thrust_events(S["thrust_ema"])
    S["score"], S["label"], S["coverage"], S["components"] = [], [], [], []
    for t in range(len(S["date"])):
        comp = components_at(S, t, window)
        sc, lbl, cov = score_from(comp)
        S["score"].append(sc)
        S["label"].append(lbl)
        S["coverage"].append(cov)
        S["components"].append(comp)
    return S


def thrust_state(S: dict, t: int) -> dict:
    ema = S["thrust_ema"]
    past = [i for i in S["thrusts"] if i <= t]
    last = past[-1] if past else None
    out = {"ema10": ema[t], "low": THRUST_LOW, "high": THRUST_HIGH, "span": THRUST_SPAN,
           "events": [S["date"][i].isoformat() for i in past],
           "last": S["date"][last].isoformat() if last is not None else None,
           "sessions_ago": t - last if last is not None else None}
    lows = [i for i in range(max(0, t - THRUST_SPAN + 1), t + 1) if ema[i] is not None and ema[i] < THRUST_LOW]
    if last is not None and t - last <= 20:
        out["state"] = f"thrust fired {t - last} session(s) ago ({out['last']})"
    elif lows and (ema[t] is None or ema[t] <= THRUST_HIGH):
        left = lows[-1] + THRUST_SPAN - t
        out["state"] = (f"armed: EMA was below {THRUST_LOW} within the last {THRUST_SPAN} sessions; "
                        f"a rise above {THRUST_HIGH} in the next {left} session(s) would be a thrust")
    elif ema[t] is None:
        out["state"] = f"n/a (needs {THRUST_EMA} sessions)"
    else:
        out["state"] = "none"
    return out


def snapshot(S: dict, meta: dict, window: int, min_pct: float, hl_window: int, members: dict | None) -> dict:
    t = len(S["date"]) - 1
    asof = S["date"][t]
    comp = S["components"][t]
    score, label, cov = S["score"][t], S["label"][t], S["coverage"][t]
    dmas = {}
    for n in DMAS:
        dmas[str(n)] = {"pct": S[f"pct{n}"][t], "n": S[f"n{n}"][t], "chg_5": _chg(S[f"pct{n}"], t, 5),
                        "chg_20": _chg(S[f"pct{n}"], t, 20)}
    a, d = S["adv"][t], S["dec"][t]
    div = divergence_at(S, t, window, min_pct)
    lo = max(0, t - HL_SUM + 1)
    hl = None
    if S["nh"][t] is not None:
        hl = {"new_highs": S["nh"][t], "new_lows": S["nl"][t], "net": S["nh"][t] - S["nl"][t],
              "eligible": S["n_hl"][t], "window": hl_window if meta["mode"] == "universe" else "as published",
              f"highs_{HL_SUM}": sum(x for x in S["nh"][lo:t + 1] if x is not None),
              f"lows_{HL_SUM}": sum(x for x in S["nl"][lo:t + 1] if x is not None)}
    sectors = None
    if S["sectors"][t] is not None:
        table = []
        for name, g in S["sectors"][t].items():
            p50 = _pct(g["a50"], g["n50"])
            table.append({"sector": name, "stocks": g["n50"], "pct_above_50dma": p50,
                          "pct_above_200dma": _pct(g["a200"], g["n200"]), "adv": g["adv"], "dec": g["dec"],
                          "participating": None if name == UNMAPPED or p50 is None else p50 >= SECTOR_MIN_PCT})
        table.sort(key=lambda r: (r["sector"] == UNMAPPED, -(r["pct_above_50dma"] if r["pct_above_50dma"] is not None else -1)))
        graded = [r for r in table if r["participating"] is not None]
        sectors = {"participating": sum(r["participating"] for r in graded), "graded": len(graded),
                   "pct": S["sector_pct"][t], "table": table,
                   "missing_members": sorted(s for s in (members or {}) if s not in S["last_bar"])}
    stale = sorted((s for s, last in S["last_bar"].items() if last < asof), key=lambda s: S["last_bar"][s])
    res = {
        "as_of": asof.isoformat(), "sessions": len(S["date"]), **meta,
        "score": {"value": score, "label": label, "coverage_pct": cov,
                  "components": {k: {"score": v, "weight": WEIGHTS[k], "input": why} for k, (v, why) in comp.items()},
                  "missing": [k for k in WEIGHTS if k not in comp]},
        "participation": {"pct_above_dma": dmas,
                          "uptrend_ratio": {"pct": S["uptrend"][t], "n": S["n_up"][t],
                                            "chg_5": _chg(S["uptrend"], t, 5), "chg_20": _chg(S["uptrend"], t, 20),
                                            "definition": "close > 50 DMA > 200 DMA"}},
        "advance_decline": {"advances": a, "declines": d, "unchanged": S["unch"][t],
                            "ratio": a / d if d else None, "net": a - d, "ad_line": S["ad_line"][t],
                            f"net_{window}": S["ad_line"][t] - S["ad_line"][t - window] if t >= window else None,
                            "divergence": div},
        "highs_lows": hl,
        "mcclellan": {"rana": S["rana"][t], "oscillator": S["osc"][t], "summation": S["summation"][t],
                      "summation_chg_5": _chg(S["summation"], t, 5)},
        "thrust": thrust_state(S, t),
        "sectors": sectors,
        "circuit_suspects": S["circuit"][t],
        "stale": [{"symbol": s, "last_bar": S["last_bar"][s].isoformat()} for s in stale],
    }
    res["notes"] = notes(S, t, res)
    res["read"] = read_line(res)
    return res


def notes(S: dict, t: int, res: dict) -> list:
    out = []
    n = res.get("stocks_used") or 0
    if res["mode"] == "universe" and n < 50:
        out.append(f"Small universe ({n} stocks): each stock moves a percentage by {100 / max(n, 1):.0f} points, "
                   "and the thrust/McClellan readings were designed for thousands of issues. Use 100+ stocks "
                   "or --ad-csv exchange counts for A/D-based signals.")
    b = S["bench"]
    s50 = ta.sma(b, 50)[t]
    p50 = S["pct50"][t]
    if s50 is not None and p50 is not None and b[t] > s50 and p50 < 40:
        out.append(f"Index above its 50 DMA while only {p50:.0f}% of stocks are: a narrow, heavyweight-led "
                   "market. Check midcap/smallcap breadth before trusting index breakouts.")
    if s50 is not None and p50 is not None and b[t] < s50 and p50 > 60:
        out.append(f"Index below its 50 DMA while {p50:.0f}% of stocks are above theirs: heavyweights are "
                   "dragging the index; the broad market is holding up.")
    bl = S.get("bench_last")
    if bl is not None and (S["date"][t] - bl).days > 4:
        out.append(f"Index data ends {bl}, before the breadth data ({S['date'][t]}): the divergence check "
                   "is using a stale index close.")
    if res["circuit_suspects"]:
        out.append(f"{len(res['circuit_suspects'])} possible circuit-locked stock(s) (zero-range bar, move ≥ "
                   f"{CIRCUIT_MIN_MOVE}%): {', '.join(res['circuit_suspects'][:10])}. They count in A/D but "
                   "could not be traded freely.")
    if res["stale"]:
        out.append(f"{len(res['stale'])} stock(s) have no bar on {res['as_of']} (suspended, delisted or stale "
                   f"file) and are left out of today's counts: "
                   + ", ".join(f"{s['symbol']} ({s['last_bar']})" for s in res["stale"][:10]))
    last = S["date"][t]
    gap = (datetime.now(IST).date() - last).days
    if gap > 4:
        out.append(f"Data ends {last} ({gap} days ago): refresh before acting.")
    if res["score"]["coverage_pct"] and res["score"]["coverage_pct"] < 50:
        out.append(f"Score uses only {res['score']['coverage_pct']}% of the component weight "
                   f"(missing: {', '.join(res['score']['missing'])}); treat it as an A/D-only read.")
    return out


def read_line(res: dict) -> str:
    sc = res["score"]
    if sc["value"] is None:
        return "Not enough history for a breadth score yet."
    bits = [f"Breadth {sc['label'].lower()} ({sc['value']:.0f}/100)"]
    div = res["advance_decline"]["divergence"]
    if div and div["flag"] == "bearish":
        bits.append(f"index {div['index_return_pct']:+.1f}% over {div['window']} sessions while the A/D line "
                    f"fell {abs(div['ad_line_change']):,}: rally carried by a few stocks")
    elif div and div["flag"] == "bullish":
        bits.append(f"index {div['index_return_pct']:+.1f}% over {div['window']} sessions while the A/D line "
                    f"rose {div['ad_line_change']:,}: selling concentrated in heavyweights")
    p50 = res["participation"]["pct_above_dma"]["50"]["pct"]
    if p50 is not None:
        bits.append(f"{p50:.0f}% of stocks above their 50 DMA")
    th = res["thrust"]
    if th["state"].startswith(("thrust", "armed")):
        bits.append("breadth thrust " + th["state"].split(":")[0])
    return "; ".join(bits) + "."


def history_rows(S: dict, sessions: int) -> list:
    out = []
    for t in range(max(0, len(S["date"]) - sessions), len(S["date"])):
        out.append({"date": S["date"][t].isoformat(), "advances": S["adv"][t], "declines": S["dec"][t],
                    "unchanged": S["unch"][t], "net": S["net"][t], "ad_line": S["ad_line"][t],
                    "pct_above_20dma": S["pct20"][t], "pct_above_50dma": S["pct50"][t],
                    "pct_above_200dma": S["pct200"][t], "uptrend_ratio": S["uptrend"][t],
                    "new_highs": S["nh"][t], "new_lows": S["nl"][t], "mcclellan_osc": S["osc"][t],
                    "summation": S["summation"][t], "thrust_ema": S["thrust_ema"][t],
                    "thrust": t in S["thrusts"], "sector_participation": S["sector_pct"][t],
                    "index": S["bench"][t], "score": S["score"][t], "label": S["label"][t]})
    return out


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def _clean(x, nd=4):
    """JSON-safe copy: non-finite floats become null, floats rounded, dates as ISO strings."""
    if isinstance(x, float):
        return round(x, nd) if math.isfinite(x) else None
    if isinstance(x, dict):
        return {str(k): _clean(v, nd) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_clean(v, nd) for v in x]
    if isinstance(x, (date, datetime)):
        return x.isoformat()
    return x


def f(x, d=1, sign=False, suffix=""):
    if x is None:
        return "—"
    return (f"{x:+,.{d}f}" if sign else f"{x:,.{d}f}") + suffix


def print_status(r: dict) -> None:
    src = f"{r['stocks_used']} stocks from {r['input']}" if r["mode"] == "universe" else f"exchange A/D counts from {r['input']}"
    print(f"Market breadth: {src}; data to {r['as_of']} ({r['sessions']} sessions)"
          + (f"; index: {r['benchmark']}" if r.get("benchmark") else ""))
    sc = r["score"]
    if sc["value"] is not None:
        print(f"Health score {sc['value']:.0f}/100: {sc['label']} (coverage {sc['coverage_pct']}% of weight)")
        print("  " + " | ".join(f"{k} {v['score']:.0f}×{v['weight']}" for k, v in sc["components"].items()))
        if sc["missing"]:
            print("  not scored: " + ", ".join(sc["missing"]))
    else:
        print("Health score: n/a (not enough history)")
    pa = r["participation"]
    if r["mode"] == "universe":
        print("% above DMA: " + " | ".join(
            f"{n}d {f(v['pct'], suffix='%')} of {v['n']} (5s {f(v['chg_5'], sign=True)}, 20s {f(v['chg_20'], sign=True)})"
            for n, v in pa["pct_above_dma"].items()))
        u = pa["uptrend_ratio"]
        print(f"Uptrend ratio (close > 50 DMA > 200 DMA): {f(u['pct'], suffix='%')} of {u['n']} "
              f"(5s {f(u['chg_5'], sign=True)}, 20s {f(u['chg_20'], sign=True)})")
    ad = r["advance_decline"]
    win = ad["divergence"]["window"] if ad["divergence"] else DIV_WINDOW
    print(f"Advances/declines: {ad['advances']} / {ad['declines']} / {ad['unchanged']} unchanged "
          f"(ratio {f(ad['ratio'], 2)}) | A/D line {ad['ad_line']:+,} | net {win}s "
          f"{f(ad.get(f'net_{win}'), 0, True)}")
    dv = ad["divergence"]
    if dv:
        tag = {"bearish": "BEARISH DIVERGENCE", "bullish": "BULLISH DIVERGENCE", "none": "no divergence"}[dv["flag"]]
        print(f"A/D line vs index ({dv['window']}s): index {dv['index_return_pct']:+.1f}%, A/D line "
              f"{dv['ad_line_change']:+,} → {tag}")
    else:
        print("A/D line vs index: n/a (give --benchmark-csv / --benchmark, or an index column in --ad-csv)")
    hl = r["highs_lows"]
    if hl:
        print(f"New 52w highs/lows: {hl['new_highs']} / {hl['new_lows']} (net {hl['net']:+})"
              + (f" of {hl['eligible']} with {hl['window']} sessions of history" if hl["eligible"] else "") + "; "
              f"last {HL_SUM}s {hl[f'highs_{HL_SUM}']} / {hl[f'lows_{HL_SUM}']}")
    else:
        print("New 52w highs/lows: n/a (needs a full look-back of bars, or NH/NL columns in --ad-csv)")
    m = r["mcclellan"]
    print(f"McClellan oscillator {f(m['oscillator'], 1, True)} | summation {f(m['summation'], 0, True)} "
          f"(5s {f(m['summation_chg_5'], 0, True)}) | today's ratio-adjusted net {f(m['rana'], 0, True)}")
    th = r["thrust"]
    print(f"Breadth thrust: {th['state']}; 10d EMA of A/(A+D) {f(th['ema10'], 3)}"
          + (f"; past thrusts {', '.join(th['events'][-3:])}" if th["events"] else ""))
    s = r["sectors"]
    if s:
        print(f"Sectors participating (≥{SECTOR_MIN_PCT:.0f}% above 50 DMA): {s['participating']}/{s['graded']}")
        for row in s["table"]:
            mark = "—" if row["participating"] is None else ("yes" if row["participating"] else "no")
            print(f"  {row['sector'][:18]:<18} n={row['stocks']:<4} >50DMA {f(row['pct_above_50dma'], 0, suffix='%'):>5}"
                  f"  >200DMA {f(row['pct_above_200dma'], 0, suffix='%'):>5}  A/D {row['adv']}/{row['dec']}  {mark}")
        if s["missing_members"]:
            print(f"  members without data: {', '.join(s['missing_members'][:15])}"
                  + (" …" if len(s["missing_members"]) > 15 else ""))
    for e in r.get("skipped", []):
        print(f"skipped {e['symbol']}: {e['error']}")
    if r.get("not_in_dir"):
        print(f"universe symbols with no CSV: {', '.join(r['not_in_dir'][:15])}" + (" …" if len(r["not_in_dir"]) > 15 else ""))
    for n in r["notes"]:
        print(f"• {n}")
    print(f"\nRead: {r['read']}")


def print_history(rows: list) -> None:
    hdr = (f"{'Date':<10} {'Adv':>5} {'Dec':>5} {'A/D line':>9} {'>20%':>5} {'>50%':>5} {'>200%':>5} "
           f"{'Upt%':>5} {'NH':>4} {'NL':>4} {'McCl':>7} {'Summ':>8} {'Thr':>5} {'Score':>5}")
    print(hdr)
    for r in rows:
        print(f"{r['date']:<10} {r['advances']:>5} {r['declines']:>5} {r['ad_line']:>9,} "
              f"{f(r['pct_above_20dma'], 0):>5} {f(r['pct_above_50dma'], 0):>5} {f(r['pct_above_200dma'], 0):>5} "
              f"{f(r['uptrend_ratio'], 0):>5} {f(r['new_highs'], 0):>4} {f(r['new_lows'], 0):>4} "
              f"{f(r['mcclellan_osc'], 1, True):>7} {f(r['summation'], 0, True):>8} {f(r['thrust_ema'], 2):>5}"
              f"{'*' if r['thrust'] else ' '}{f(r['score'], 0):>5}")
    print("* = breadth thrust session. Percentages are of stocks with enough history for that average.")


def write_csv(rows: list, path: str) -> None:
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if v is None else round(v, 4) if isinstance(v, float) else v) for k, v in r.items()})


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _common(p) -> None:
    p.add_argument("--csv-dir", help="folder with one daily OHLCV CSV per stock (file stem = symbol)")
    p.add_argument("--pattern", default="*.csv", help="glob inside --csv-dir (default *.csv)")
    p.add_argument("--symbols", help="comma list fetched via yfinance")
    p.add_argument("--universe-file", help="constituent list (CSV with Symbol column, or one per line)")
    p.add_argument("--ad-csv", help="daily advances/declines CSV (exchange-wide counts)")
    p.add_argument("--members", help="CSV of symbol,sector for sector participation")
    p.add_argument("--benchmark-csv", help="index CSV (date + close) for the divergence check")
    p.add_argument("--benchmark", help="index via yfinance, e.g. NIFTY (default NIFTY when fetching stocks)")
    p.add_argument("--exchange", default="NSE", choices=["NSE", "BSE"])
    p.add_argument("--period", default="2y", help="yfinance history period (default 2y)")
    p.add_argument("--save-dir", help="also write fetched stock CSVs here for offline re-runs")
    p.add_argument("--as-of", help="replay: ignore data after YYYY-MM-DD")
    p.add_argument("--hl-window", type=int, default=HL_WINDOW, help="sessions in the new-high/low look-back (252)")
    p.add_argument("--div-window", type=int, default=DIV_WINDOW, help="sessions for A/D-vs-index divergence (20)")
    p.add_argument("--div-min-pct", type=float, default=DIV_MIN_PCT,
                   help="index move %% needed before a divergence counts (1.0)")
    p.add_argument("--json", action="store_true")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Market-wide breadth for NSE/BSE stock universes")
    sub = p.add_subparsers(dest="cmd", required=True)
    _common(sub.add_parser("status", help="latest breadth snapshot and health score"))
    h = sub.add_parser("history", help="breadth metrics for the last N sessions")
    _common(h)
    h.add_argument("--sessions", type=int, default=20, help="sessions to show (default 20)")
    h.add_argument("--out", help="write the history to this CSV file")
    a = p.parse_args(argv)
    try:
        return _run(a)
    except BreadthError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except SystemExit as e:            # ta.fetch_yf reports a missing yfinance this way
        if isinstance(e.code, str):
            print(f"error: {e.code}", file=sys.stderr)
            return 2
        raise
    except (OSError, UnicodeDecodeError, csv.Error, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


def _cut(rows: list, asof: date | None, key=lambda r: r["date"]) -> list:
    return rows if asof is None else [r for r in rows if key(r) <= asof]


def _run(a) -> int:
    if a.hl_window < 2:
        raise BreadthError("--hl-window must be at least 2")
    if a.div_window < 1:
        raise BreadthError("--div-window must be at least 1")
    if a.div_min_pct < 0:
        raise BreadthError("--div-min-pct must be ≥ 0")
    if getattr(a, "sessions", 1) < 1:
        raise BreadthError("--sessions must be at least 1")
    asof = None
    if a.as_of:
        try:
            asof = _parse_date(a.as_of)
        except ValueError:
            raise BreadthError(f"--as-of {a.as_of!r}: not a date (use YYYY-MM-DD)")
    online = bool(a.symbols) or (bool(a.universe_file) and not a.csv_dir)
    if a.ad_csv and (a.csv_dir or a.symbols or a.universe_file):
        raise BreadthError("--ad-csv is its own input: drop --csv-dir/--symbols/--universe-file")
    if a.csv_dir and a.symbols:
        raise BreadthError("give --csv-dir or --symbols, not both")
    if not (a.ad_csv or a.csv_dir or online):
        raise BreadthError("give --csv-dir DIR, --symbols A,B,C / --universe-file FILE, or --ad-csv FILE")

    bench, bench_label = None, None
    if a.benchmark_csv:
        if not Path(a.benchmark_csv).is_file():
            raise BreadthError(f"--benchmark-csv {a.benchmark_csv}: file not found")
        bench, bench_label = load_closes(a.benchmark_csv), Path(a.benchmark_csv).stem
    elif a.benchmark or online:
        name = a.benchmark or "NIFTY"
        try:
            bench = [(r["date"].date(), r["close"]) for r in ta.fetch_yf(name, a.exchange, a.period, "1d")
                     if math.isfinite(r["close"]) and r["close"] > 0]
            bench_label = ta.yf_ticker(name, a.exchange)
        except SystemExit as e:
            if not isinstance(e.code, str):
                raise
            raise BreadthError(NO_YF if "not installed" in e.code else f"benchmark {name}: {e.code}")
    if bench is not None:
        bench = _cut(bench, asof, key=lambda r: r[0])
        if not bench:
            raise BreadthError("benchmark has no usable closes" + (f" on or before {asof}" if asof else ""))

    members = load_members(a.members) if a.members else None
    meta = {"benchmark": bench_label}
    if a.ad_csv:
        rows, found = load_ad_csv(a.ad_csv)
        rows = _cut(rows, asof)
        if len(rows) < 2:
            raise BreadthError(f"--ad-csv: fewer than 2 rows on or before {asof}")
        S = ad_series(rows, bench)
        if not bench and "index" in found:
            meta["benchmark"] = f"{found['index']} column"
        meta.update({"mode": "ad-csv", "input": Path(a.ad_csv).name, "stocks_used": None,
                     "columns": found})
        members = None
    else:
        symbols, uni_sectors = [], {}
        if a.universe_file:
            symbols, uni_sectors = load_universe_file(a.universe_file)
        if a.symbols:
            symbols += [norm_symbol(s) for s in a.symbols.split(",") if norm_symbol(s)]
            symbols = list(dict.fromkeys(symbols))
        if members is None and uni_sectors:
            members = uni_sectors
        missing = []
        if a.csv_dir:
            exclude = Path(a.benchmark_csv).resolve() if a.benchmark_csv else None
            stocks, errors, missing = universe_from_dir(a.csv_dir, a.pattern, exclude, symbols or None)
            src = a.csv_dir
        else:
            if not symbols:
                raise BreadthError("no symbols to fetch")
            stocks, errors = universe_from_yf(symbols, a.exchange, a.period, a.save_dir)
            src = f"yfinance ({a.exchange})"
        if asof:
            stocks = {s: r for s, r in ((s, _cut(r, asof)) for s, r in stocks.items()) if len(r) >= 2}
        if not stocks:
            raise BreadthError("no stock has usable daily bars" + (f" on or before {asof}" if asof else "")
                               + (f"; first problem: {errors[0]['symbol']}: {errors[0]['error']}" if errors else ""))
        S = universe_series(stocks, members, bench, a.hl_window)
        if len(S["date"]) < 2:
            raise BreadthError("need at least 3 common sessions across the universe")
        meta.update({"mode": "universe", "input": src, "stocks_used": len(stocks), "skipped": errors,
                     "not_in_dir": missing})
    derive(S, a.div_window)

    if a.cmd == "history":
        rows = history_rows(S, a.sessions)
        if a.out:
            write_csv(rows, a.out)
        if a.json:
            print(json.dumps(_clean({"mode": meta["mode"], "input": meta["input"], "rows": rows}),
                             indent=2, allow_nan=False))
        else:
            print_history(rows)
            if a.out:
                print(f"wrote {len(rows)} rows to {a.out}")
        return 0

    res = snapshot(S, meta, a.div_window, a.div_min_pct, a.hl_window, members)
    if a.json:
        print(json.dumps(_clean(res), indent=2, allow_nan=False))
    else:
        print_status(res)
    return 0


if __name__ == "__main__":
    sys.exit(main())
