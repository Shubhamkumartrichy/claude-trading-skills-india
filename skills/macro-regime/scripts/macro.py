#!/usr/bin/env python3
"""Structural macro regime for Indian equities: the slow, 6-24 month cross-asset layer.

Ten monthly gauges are built from NSE index ratios, gold in rupees, G-sec yields, the rupee, Brent
crude, the US 10-year and India VIX. Every gauge is one line (a ratio or a level). Its state comes
from the crossover of its 6-month and 12-month simple averages, with the 6-month slope telling
whether the move is still running or fading. The states roll up into one of six regime labels,
together with the evidence, a count of how many gauges turned recently, the tilts each regime has
tended to favour, and the single flips that would change the label.

Not the same job as ../../market-regime/scripts/regime.py. That script reads distribution days and
follow-through on the daily index tape (a horizon of weeks; it sets the exposure ceiling). This one
reads leadership and macro pressure over quarters and changes only a few times per cycle.

Zero dependencies (Python 3.10+ stdlib). Optional: `pip install yfinance` to fetch the series
Yahoo carries. Moving averages and timestamp parsing come from ../../technical-analysis/scripts/ta.py.

Input
  --csv-dir DIR     one CSV per series; the file stem names the series ("NIFTY 50.csv",
                    "GOLDBEES.csv", "IN10Y.csv", "USDINR.csv" ...; run `series` for every accepted
                    name). A date column plus one close/price/yield column is enough. Daily, weekly
                    or monthly rows all work; everything is reduced to month-end values.
  --fetch           with --csv-dir: also fetch from Yahoo whatever the folder lacks
  (no --csv-dir)    fetch every series that has a Yahoo ticker (G-sec yields never do)
  --ticker K=T      override or add a Yahoo ticker, e.g. --ticker USDINR=USDINR=X

Sub-commands
  status            regime label, evidence, turn count, component table, tilts, what would change it
  history           the label month by month for the last N months, and the regime spells
  component NAME    one gauge in detail: monthly line, averages, gap, every turn in the sample
  series            the series list: accepted file names, Yahoo tickers and where to download

Examples
  python3 macro.py status --csv-dir macro_data/
  python3 macro.py status --csv-dir yields/ --fetch --json
  python3 macro.py history --csv-dir macro_data/ --months 36
  python3 macro.py component cyc_def --csv-dir macro_data/ --months 18
"""
from __future__ import annotations

import argparse
import bisect
import csv
import importlib.util
import json
import math
import re
import statistics
import sys
from datetime import date, datetime, timedelta
from pathlib import Path


def _load_ta():
    path = Path(__file__).resolve().parents[2] / "technical-analysis" / "scripts" / "ta.py"
    name = "skill_technical_analysis_ta"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


ta = _load_ta()


class MacroError(Exception):
    """Bad input the user can fix; reported as a one-line error with exit code 2."""


# ---------------------------------------------------------------------------
# Series registry
# ---------------------------------------------------------------------------
# Yahoo status
#   confirmed   - daily history was served when this skill pack probed the symbol on 2026-10-31
#                 (same list as ../../sector-rotation/scripts/sectors.py). Depth beyond a year or
#                 two was not checked; NSE strategy/size indices can be short on Yahoo.
#   unconfirmed - the usual Yahoo symbol, but NOT verified here: on 2026-11-12 Yahoo answered a
#                 plain scripted request with HTTP 429, and the probe was not pushed further.
#                 Expect it to work with yfinance; if it does not, use --ticker or a CSV.
#   csv-only    - no dependable Yahoo symbol. Download a CSV (see "where").
# kind: "pct" lines are compared in % terms; "level" lines (yields) in basis points.
SERIES = [
    # key, label, Yahoo ticker, status, kind, aliases (normalised file stems), where to download
    ("NIFTY", "NIFTY 50", "^NSEI", "confirmed", "pct", ("NIFTY50", "NSEI", "CNXNIFTY"),
     "niftyindices.com > Reports > Historical Data, or nseindia.com index history"),
    ("SMALLCAP", "NIFTY SMALLCAP 250", "NIFTYSMLCAP250.NS", "confirmed", "pct",
     ("NIFTYSMALLCAP250", "NIFTYSMLCAP250", "SMALLCAP250", "SMLCAP250", "NIFTYSMALLCAP100", "SMALLCAP100"),
     "niftyindices.com historical data (Smallcap 100 is an acceptable stand-in)"),
    ("MIDCAP", "NIFTY MIDCAP 150", "NIFTYMIDCAP150.NS", "confirmed", "pct",
     ("NIFTYMIDCAP150", "MIDCAP150", "NIFTYMIDCAP100", "MIDCAP100"), "niftyindices.com historical data"),
    ("BANK", "NIFTY BANK", "^NSEBANK", "confirmed", "pct", ("NIFTYBANK", "BANKNIFTY", "NSEBANK"),
     "niftyindices.com historical data"),
    ("EQUALWEIGHT", "NIFTY 50 EQUAL WEIGHT", None, "csv-only", "pct",
     ("NIFTY50EQUALWEIGHT", "NIFTYEQUALWEIGHT", "NIFTY50EQLWGT", "NIFTY50EQUALWT", "EQLWGT"),
     "niftyindices.com > Strategy indices > NIFTY50 Equal Weight"),
    ("FMCG", "NIFTY FMCG", "^CNXFMCG", "confirmed", "pct", ("NIFTYFMCG", "CNXFMCG"),
     "niftyindices.com historical data (defensive basket member)"),
    ("PHARMA", "NIFTY PHARMA", "^CNXPHARMA", "confirmed", "pct", ("NIFTYPHARMA", "CNXPHARMA"),
     "niftyindices.com historical data (defensive basket member)"),
    ("METAL", "NIFTY METAL", "^CNXMETAL", "confirmed", "pct", ("NIFTYMETAL", "CNXMETAL"),
     "niftyindices.com historical data (cyclical basket member)"),
    ("REALTY", "NIFTY REALTY", "^CNXREALTY", "confirmed", "pct", ("NIFTYREALTY", "CNXREALTY"),
     "niftyindices.com historical data (cyclical basket member)"),
    ("PSUBANK", "NIFTY PSU BANK", "^CNXPSUBANK", "confirmed", "pct", ("NIFTYPSUBANK", "CNXPSUBANK"),
     "niftyindices.com historical data (cyclical basket member)"),
    ("DEFENSIVE", "Defensive basket (own series)", None, "csv-only", "pct", ("DEFENSIVES",),
     "optional: your own defensive index; replaces the FMCG + PHARMA basket"),
    ("CYCLICAL", "Cyclical basket (own series)", None, "csv-only", "pct", ("CYCLICALS",),
     "optional: your own cyclical index; replaces the METAL + REALTY + PSU BANK basket"),
    ("GOLD", "Gold in rupees (GOLDBEES ETF)", "GOLDBEES.NS", "unconfirmed", "pct",
     ("GOLDBEES", "GOLDINR", "MCXGOLD", "GOLDBEESHISTORICALDATA"),
     "broker or NSE price history of GOLDBEES; MCX gold or IBJA rupee prices also work"),
    ("IN10Y", "India 10-year G-sec yield", None, "csv-only", "level",
     ("INDIA10Y", "IND10Y", "GSEC10Y", "GSEC", "INDIA10YEAR", "INDIA10YEARBONDYIELD"),
     "RBI DBIE (Financial Market > yields), CCIL / FBIL benchmark yields, or the Investing.com "
     "'India 10-Year Bond Yield' historical-data export"),
    ("SHORT", "Short rate (91-day T-bill or repo)", None, "csv-only", "level",
     ("SHORTRATE", "TBILL91", "TBILL91D", "TBILL", "IN91D", "IN3M", "INDIA3MONTHBONDYIELD",
      "REPO", "REPORATE", "POLICYRATE", "RBIREPO"),
     "RBI DBIE (91-day T-bill cut-off yield or policy repo rate), FBIL T-bill curve, or the "
     "Investing.com 'India 3-Month Bond Yield' export. A repo file needs only the change dates"),
    ("USDINR", "USD/INR", "INR=X", "unconfirmed", "pct", ("INR", "INRX", "USDINRX", "USDINRHISTORICALDATA"),
     "RBI / FBIL reference rate, or Yahoo INR=X (alternative symbol USDINR=X)"),
    ("BRENT", "Brent crude (USD)", "BZ=F", "unconfirmed", "pct",
     ("BRENTCRUDE", "BRENTOIL", "BRENTOILFUTURES", "BZ", "BZF", "CRUDE", "CRUDEOIL"),
     "Yahoo BZ=F, EIA Brent spot history, or the Investing.com Brent futures export"),
    ("US10Y", "US 10-year Treasury yield", "^TNX", "unconfirmed", "level",
     ("TNX", "UST10Y", "US10YEAR", "UNITEDSTATES10YEARBONDYIELD"),
     "Yahoo ^TNX (quoted in %), or FRED series DGS10"),
    ("VIX", "India VIX", "^INDIAVIX", "unconfirmed", "pct", ("INDIAVIX", "NIFTYVIX", "INDIAVIXHISTORICALDATA"),
     "nseindia.com > Historical VIX, or Yahoo ^INDIAVIX"),
]
INFO = {s[0]: {"key": s[0], "label": s[1], "ticker": s[2], "yahoo": s[3], "kind": s[4],
               "files": [s[1] + ".csv", s[0] + ".csv"], "where": s[6]} for s in SERIES}
REPO_STEMS = {"REPO", "REPORATE", "POLICYRATE", "RBIREPO"}
DEFENSIVE_MEMBERS = ("FMCG", "PHARMA")
CYCLICAL_MEMBERS = ("METAL", "REALTY", "PSUBANK")


def norm(name: str) -> str:
    stem = re.sub(r"\.(NS|BO)$", "", name.strip(), flags=re.I)
    n = re.sub(r"[^A-Z0-9]", "", stem.upper())
    return n[:-14] if n.endswith("HISTORICALDATA") and len(n) > 14 else n


def _alias_table() -> dict:
    table = {}
    for key, label, _t, _s, _k, extra, _w in SERIES:
        for a in (key, label, *extra):
            table[norm(a)] = key
    return table


ALIASES = _alias_table()


def resolve(name: str) -> str | None:
    """Map a file stem or user label ("NIFTY 50", "^NSEI", "India 10-Year Bond Yield Historical Data")
    to a series key."""
    return ALIASES.get(norm(name))


# ---------------------------------------------------------------------------
# Months are integers (year * 12 + month - 1) so that month arithmetic is plain subtraction.
# ---------------------------------------------------------------------------
def month_of(d: date) -> int:
    return d.year * 12 + d.month - 1


def mlabel(m: int) -> str:
    return f"{m // 12:04d}-{m % 12 + 1:02d}"


def weekdays_left(d: date) -> int:
    n, x = 0, d + timedelta(days=1)
    while x.month == d.month:
        n += x.weekday() < 5
        x += timedelta(days=1)
    return n


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
DATE_COLS = ("date", "index date", "historicaldate", "timestamp", "datetime", "time", "month", "period")
VALUE_COLS = ("close", "price", "yield", "yield (%)", "rate", "value", "last", "ltp", "closing index value",
              "close price", "closing price", "index value", "total returns index")
VALUE_HINTS = ("close", "yield", "rate", "price")
_NUMERIC_DATE = re.compile(r"^(\d{1,2})[/.-](\d{1,2})[/.-](\d{4}|\d{2})$")
_MONTH_FORMATS = ("%Y-%m", "%Y/%m", "%b-%Y", "%b %Y", "%B %Y", "%B-%Y", "%b-%y", "%Y%m")
_EXTRA_FORMATS = ("%b %d, %Y", "%B %d, %Y", "%d-%b-%y", "%d %b %y", "%d-%B-%Y", "%d %B %Y")


def infer_day_first(raws: list) -> tuple:
    """Decide dd/mm vs mm/dd for all-numeric dates from the whole file, not row by row.

    A first field above 12 proves day-first; a second field above 12 proves month-first. If neither
    occurs (monthly exports dated on the 1st), take the reading that yields more distinct months.
    Returns (day_first, ambiguous)."""
    parts = [(int(m.group(1)), int(m.group(2)), m.group(3)) for m in map(_NUMERIC_DATE.match, raws) if m]
    if not parts:
        return True, False
    if any(a > 12 for a, _, _ in parts):
        return True, False
    if any(b > 12 for _, b, _ in parts):
        return False, False
    as_day_first = len({(b, y) for _, b, y in parts})
    as_month_first = len({(a, y) for a, _, y in parts})
    if as_day_first == as_month_first:
        return True, len(parts) > 1
    return as_day_first > as_month_first, False


def parse_date(raw: str, day_first: bool = True) -> date:
    raw = raw.strip()
    m = _NUMERIC_DATE.match(raw)
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        y += 2000 if y < 100 else 0
        return date(y, b, a) if day_first else date(y, a, b)
    for fmt in _MONTH_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    try:
        return ta.parse_ts(raw).date()
    except (ValueError, OverflowError, OSError):
        pass
    for fmt in _EXTRA_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    raise ValueError(raw)


def _num(x) -> float:
    s = str(x if x is not None else "").strip().replace(",", "").rstrip("%").strip()
    if s.lower() in ("", "-", "--", "null", "none", "nan", "n/a", "na"):
        return math.nan
    return float(s)


def load_series(path: str | Path, warnings: list | None = None) -> list:
    """Read one CSV into a sorted [(date, value)]. Rows with a blank, null or non-positive value
    are skipped (broker exports pad holidays with nulls)."""
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        cols = {c.strip().lower(): c for c in reader.fieldnames or [] if c}
        cd = next((cols[c] for c in DATE_COLS if c in cols), None)
        cv = next((cols[c] for c in VALUE_COLS if c in cols), None)
        if cv is None:
            cv = next((orig for low, orig in cols.items() if orig != cd and any(h in low for h in VALUE_HINTS)), None)
        if not cd or not cv:
            raise MacroError(f"{Path(path).name}: need a date column and a close/price/yield column; "
                             f"found {list(cols)}")
        raw_rows = [((r.get(cd) or "").strip(), r.get(cv)) for r in reader]
    day_first, ambiguous = infer_day_first([d for d, _ in raw_rows])
    if ambiguous and warnings is not None:
        warnings.append(f"{Path(path).name}: dd/mm vs mm/dd cannot be told apart; read as day-first. "
                        "Use YYYY-MM-DD dates to be safe")
    out = {}
    for raw, val in raw_rows:
        if not raw:
            continue
        try:
            d, v = parse_date(raw, day_first), _num(val)
        except ValueError:
            continue
        if math.isfinite(v) and v > 0:
            out[d] = v
    return sorted(out.items())


def fetch_yahoo(ticker: str, period: str) -> tuple:
    """Daily closes from Yahoo via yfinance: ([(date, close)], error-or-None). NaN bars are dropped."""
    try:
        import yfinance as yf  # type: ignore
    except ImportError:
        raise MacroError("yfinance not installed: pip install yfinance  (or pass --csv-dir without --fetch)")
    try:
        df = yf.Ticker(ticker).history(period=period, interval="1d", auto_adjust=False)
    except Exception as e:  # yfinance raises its own, version-dependent exception types
        return [], f"{type(e).__name__}: {e}"
    if df is None or df.empty:
        return [], "no data returned"
    out = {}
    for idx, row in df.iterrows():
        try:
            c = float(row["Close"])
        except (TypeError, ValueError, KeyError):
            continue
        if math.isfinite(c) and c > 0:
            out[idx.date()] = c
    return sorted(out.items()), None


def parse_overrides(pairs: list | None) -> dict:
    out = {}
    for pair in pairs or []:
        name, _, ticker = pair.partition("=")
        if not name.strip() or not ticker.strip():
            raise MacroError(f"--ticker expects NAME=TICKER, got {pair!r}")
        key = resolve(name)
        if key is None:
            raise MacroError(f"--ticker {pair!r}: unknown series {name!r}; run `series` for the list")
        out[key] = ticker.strip()
    return out


def gather(csv_dir: str | None, fetch: bool, overrides: dict, period: str, warnings: list) -> tuple:
    """Collect raw observations per series key from a folder and/or Yahoo."""
    data, ignored = {}, []
    if csv_dir:
        d = Path(csv_dir)
        if not d.is_dir():
            raise MacroError(f"--csv-dir {csv_dir}: not a directory")
        files = sorted(p for p in d.iterdir() if p.suffix.lower() == ".csv")
        if not files:
            raise MacroError(f"--csv-dir {csv_dir}: no .csv files")
        for p in files:
            key = resolve(p.stem)
            if key is None:
                ignored.append(p.name)
                continue
            if key in data:
                raise MacroError(f"--csv-dir {csv_dir}: both {data[key]['source']} and {p.name} map to "
                                 f"{key}; keep one")
            obs = load_series(p, warnings)
            if not obs:
                warnings.append(f"{p.name}: no usable rows")
                continue
            data[key] = {"obs": obs, "source": p.name, "repo": norm(p.stem) in REPO_STEMS}
    if fetch or not csv_dir:
        for key, info in INFO.items():
            if key in data:
                continue
            ticker = overrides.get(key, info["ticker"])
            if not ticker:
                continue
            obs, err = fetch_yahoo(ticker, period)
            if not obs:
                warnings.append(f"{key} ({ticker}): Yahoo fetch failed ({err}); supply a CSV or --ticker {key}=...")
                continue
            data[key] = {"obs": obs, "source": ticker, "repo": False}
    if not data:
        raise MacroError("no series loaded: name the files after the series (run `series` to see the names)")
    return data, ignored


# ---------------------------------------------------------------------------
# Maths
# ---------------------------------------------------------------------------
def frequency(dates: list) -> str:
    if len(dates) < 3:
        return "monthly"
    gap = statistics.median((dates[i] - dates[i - 1]).days for i in range(1, len(dates)))
    return "daily" if gap <= 3 else "weekly" if gap <= 8 else "monthly"


def to_monthly(obs: list) -> dict:
    """Last observation of each calendar month: {month: (value, date)}."""
    out = {}
    for d, v in obs:
        out[month_of(d)] = (v, d)
    return out


def contiguous(monthly: dict, step: bool = False, upto: int | None = None) -> tuple:
    """(months, values, note) for the latest unbroken run of months.

    One missing month is bridged with the previous value. A longer hole discards everything before
    it, so a 6-month average never silently spans a year. With step=True (a policy rate that holds
    until changed) every hole is filled, and the last value is carried up to `upto`."""
    months, vals, note = [], [], None
    for m in sorted(monthly):
        if months:
            hole = m - months[-1] - 1
            if hole > 1 and not step:
                note = f"{hole} months missing before {mlabel(m)}; earlier data ignored"
                months, vals = [], []
            else:
                for k in range(hole):
                    months.append(months[-1] + 1)
                    vals.append(vals[-1])
        months.append(m)
        vals.append(monthly[m][0])
    if step and months and upto is not None:
        while months[-1] < upto:
            months.append(months[-1] + 1)
            vals.append(vals[-1])
    return months, vals, note


def ratio_line(num: tuple, den: tuple) -> tuple:
    """Month-by-month ratio of two (months, values) series over the months both have."""
    dm = dict(zip(*den))
    months = [m for m in num[0] if m in dm]
    nm = dict(zip(*num))
    return months, [nm[m] / dm[m] for m in months]


def spread_line(a: tuple, b: tuple) -> tuple:
    bm = dict(zip(*b))
    months = [m for m in a[0] if m in bm]
    am = dict(zip(*a))
    return months, [am[m] - bm[m] for m in months]


def basket(members: list) -> tuple:
    """Equal-weight basket: rebase each member to 100 at the first month they all share, then average."""
    common = sorted(set.intersection(*(set(m[0]) for m in members)))
    if not common:
        return [], []
    maps = [dict(zip(*m)) for m in members]
    return common, [sum(100 * mp[m] / mp[common[0]] for mp in maps) / len(maps) for m in common]


def change(vals: list, n: int, kind: str = "pct"):
    """Change over n months: % for price/ratio lines, basis points for yield lines."""
    if n <= 0 or len(vals) <= n:
        return None
    if kind == "level":
        return (vals[-1] - vals[-1 - n]) * 100
    return (vals[-1] / vals[-1 - n] - 1) * 100 if vals[-1 - n] else None


def ols_slope(vals: list):
    """Least-squares slope per step of evenly spaced values."""
    n = len(vals)
    if n < 2:
        return None
    mx, my = (n - 1) / 2, sum(vals) / n
    return sum((i - mx) * (v - my) for i, v in enumerate(vals)) / sum((i - mx) ** 2 for i in range(n))


def slope(vals: list, n: int, kind: str = "pct"):
    """Fitted slope over the last n months (n + 1 points): % of the window mean per month for
    price/ratio lines, basis points per month for yield lines. Less endpoint-sensitive than change()."""
    if n <= 0 or len(vals) <= n:
        return None
    win = vals[-n - 1:]
    s = ols_slope(win)
    if kind == "level":
        return s * 100
    mean = sum(win) / len(win)
    return s / mean * 100 if mean else None


def directions(gaps: list, band: float) -> tuple:
    """Crossover state with a dead band. Direction becomes "up" once the gap exceeds +band, "down"
    once it is below -band, and otherwise keeps its previous value. Returns (direction per bar,
    turns as [(index, from, to)])."""
    out, turns, cur = [], [], None
    for i, g in enumerate(gaps):
        if g is not None:
            new = "up" if g > band else "down" if g < -band else cur
            if new != cur:
                if cur is not None:
                    turns.append((i, cur, new))
                cur = new
        out.append(cur)
    return out, turns


def make_line(label: str, months: list, values: list, kind: str, p: dict) -> dict:
    fast, slow = ta.sma(values, p["fast"]), ta.sma(values, p["slow"])
    gaps = []
    for f_, s_ in zip(fast, slow):
        if f_ is None or s_ is None:
            gaps.append(None)
        elif kind == "level":
            gaps.append((f_ - s_) * 100)
        else:
            gaps.append((f_ / s_ - 1) * 100 if s_ else None)
    dirs, turns = directions(gaps, p["band_bp"] if kind == "level" else p["band_pct"])
    return {"label": label, "months": months, "values": values, "kind": kind, "fast": fast, "slow": slow,
            "gap": gaps, "dir": dirs, "turns": turns}


def snapshot(line: dict, j: int, recent: int) -> dict:
    vals, kind = line["values"][:j + 1], line["kind"]
    d = line["dir"][j]
    turn = next((t for t in reversed(line["turns"]) if t[0] <= j), None)
    last_turn, turned = None, False
    if turn:
        ago = line["months"][j] - line["months"][turn[0]]
        last_turn = {"month": mlabel(line["months"][turn[0]]), "from": turn[1], "to": turn[2], "months_ago": ago}
        turned = ago < recent
    sl = slope(vals, 6, kind)
    if d is None:
        state = "flat"
    elif turned:
        state = f"turned {d}"
    elif d == "up":
        state = "rising" if sl is None or sl >= 0 else "rising, fading"
    else:
        state = "falling" if sl is None or sl <= 0 else "falling, basing"
    return {"month": mlabel(line["months"][j]), "value": vals[-1], "ma_fast": line["fast"][j],
            "ma_slow": line["slow"][j], "gap": line["gap"][j], "unit": "bp" if kind == "level" else "%",
            "chg_6m": change(vals, 6, kind), "chg_12m": change(vals, 12, kind), "slope_6m": sl,
            "direction": d, "state": state, "last_turn": last_turn, "turned_recently": turned}


# ---------------------------------------------------------------------------
# Components
# ---------------------------------------------------------------------------
# key, title, reading when the line is up, reading when it is down, effect of "up" on Indian equities
COMPONENTS = [
    ("concentration", "Concentration vs broadening", "broadening", "concentrating", 1),
    ("size", "Size factor", "smaller caps leading", "large caps leading", 1),
    ("financials", "Financials leadership", "banks leading", "banks lagging", 1),
    ("cyc_def", "Cyclicals vs defensives", "cyclicals leading", "defensives leading", 1),
    ("equity_gold", "Equity vs gold", "equities beating gold", "gold beating equities", 1),
    ("rates", "India rates", "yields rising", "yields falling", -1),
    ("rupee", "Rupee", "rupee weakening", "rupee strengthening", -1),
    ("crude", "Crude oil", "crude rising", "crude falling", -1),
    ("global_rates", "Global rates", "US yields rising", "US yields falling", -1),
    ("volatility", "Volatility regime", "volatility rising", "volatility falling", -1),
]
CINFO = {c[0]: {"key": c[0], "title": c[1], "up": c[2], "down": c[3], "sign": c[4]} for c in COMPONENTS}
COMPONENT_ALIASES = {"curve": "rates", "yields": "rates", "yield": "rates", "breadth": "concentration",
                     "broadening": "concentration", "smallcap": "size", "bank": "financials",
                     "banks": "financials", "cyclicals": "cyc_def", "cycdef": "cyc_def", "gold": "equity_gold",
                     "equitygold": "equity_gold", "usdinr": "rupee", "inr": "rupee", "brent": "crude",
                     "oil": "crude", "us10y": "global_rates", "globalrates": "global_rates", "vix": "volatility"}
SHORT_TAGS = {"concentration": "conc", "size": "size", "financials": "fin", "cyc_def": "cyc/def",
              "equity_gold": "eq/gold", "rates": "10y", "rupee": "usdinr", "crude": "brent",
              "global_rates": "us10y", "volatility": "vix"}
INTERNALS = ("concentration", "size", "financials", "cyc_def", "equity_gold")
BREADTH = ("concentration", "size")
PRESSURE = ("crude", "rupee", "rates")
STALE_MAX = 2                      # months a lagging series may trail the evaluation month
VIX_BANDS = ((13, "calm"), (17, "normal"), (22, "elevated"), (30, "stressed"))   # same cut-offs as market-regime


def vix_band(level) -> str | None:
    if level is None:
        return None
    return next((name for cut, name in VIX_BANDS if level < cut), "panic")


def _sane_yield(key: str, mv: tuple, warnings: list) -> bool:
    if mv[1] and statistics.median(mv[1]) > 25:
        warnings.append(f"{key}: values look like a price or index (median {statistics.median(mv[1]):.1f}), "
                        "not a yield in %; series ignored")
        return False
    return True


def build_components(data: dict, end: int, p: dict, warnings: list) -> list:
    """Turn raw series into the ten component lines (each either a line or a `missing` reason)."""
    S = {}
    for key, item in data.items():
        monthly = {m: v for m, v in to_monthly(item["obs"]).items() if m <= end}
        if not monthly:
            continue
        mv = contiguous(monthly, step=item["repo"], upto=end if item["repo"] else None)
        if mv[2]:
            warnings.append(f"{key}: {mv[2]}")
        if INFO[key]["kind"] == "level" and not _sane_yield(key, mv, warnings):
            continue
        S[key] = (mv[0], mv[1])

    def need(*keys):
        miss = [k for k in keys if k not in S]
        return "needs " + " and ".join(keys) + f" (missing: {', '.join(miss)})" if miss else None

    def comp(key, line=None, inputs=(), missing=None, **extra):
        return {**CINFO[key], "line": line, "inputs": list(inputs), "missing": missing, **extra}

    def ratio(key, num, den, label=None):
        miss = need(num, den)
        if miss:
            return comp(key, missing=miss)
        months, vals = ratio_line(S[num], S[den])
        if not months:
            return comp(key, missing=f"{num} and {den} share no months")
        return comp(key, make_line(label or f"{INFO[num]['label']} / {INFO[den]['label']}", months, vals, "pct", p),
                    (num, den))

    def level(key, src):
        if src not in S:
            return comp(key, missing=f"needs {src} ({INFO[src]['label']})")
        return comp(key, make_line(INFO[src]["label"], S[src][0], S[src][1], INFO[src]["kind"], p), (src,))

    def side(own, members):
        if own in S:
            return S[own], [own]
        have = [k for k in members if k in S]
        return (basket([S[k] for k in have]), have) if have else (None, [])

    out = []
    # 1 and 2: the breadth pair. Equal weight is the cleanest concentration read; without it the
    # small-cap ratio stands in and the mid-cap ratio becomes the size factor, so no line is used twice.
    breadth_num = next((k for k in ("EQUALWEIGHT", "SMALLCAP", "MIDCAP") if k in S), None)
    if "NIFTY" not in S or breadth_num is None:
        out.append(comp("concentration", missing=need("NIFTY") or "needs EQUALWEIGHT, SMALLCAP or MIDCAP"))
    else:
        out.append(ratio("concentration", breadth_num, "NIFTY"))
    size_num = next((k for k in ("SMALLCAP", "MIDCAP") if k in S and k != breadth_num), None)
    if "NIFTY" not in S or breadth_num is None:
        out.append(comp("size", missing=need("NIFTY") or "needs SMALLCAP or MIDCAP"))
    elif size_num is None:
        out.append(comp("size", missing=f"{breadth_num} / NIFTY already serves as the concentration line; "
                                        "add EQUALWEIGHT or a second size index to split the two"))
    else:
        out.append(ratio("size", size_num, "NIFTY"))
    out.append(ratio("financials", "BANK", "NIFTY"))
    # 4: cyclical basket over defensive basket
    (cyc, cyc_in), (dfn, dfn_in) = side("CYCLICAL", CYCLICAL_MEMBERS), side("DEFENSIVE", DEFENSIVE_MEMBERS)
    if cyc is None or dfn is None:
        lacking = [n for n, s in (("a cyclical index (METAL, REALTY, PSU BANK)", cyc),
                                  ("a defensive index (FMCG, PHARMA)", dfn)) if s is None]
        out.append(comp("cyc_def", missing="needs " + " and ".join(lacking)))
    else:
        months, vals = ratio_line(cyc, dfn)
        if months:
            label = f"cyclicals ({'+'.join(cyc_in)}) / defensives ({'+'.join(dfn_in)})"
            out.append(comp("cyc_def", make_line(label, months, vals, "pct", p), cyc_in + dfn_in))
        else:
            out.append(comp("cyc_def", missing="cyclical and defensive series share no months"))
    out.append(ratio("equity_gold", "NIFTY", "GOLD", "NIFTY 50 / gold in rupees"))
    # 6: the 10-year yield, with the curve (10y minus short rate) as a sub-line
    rates = level("rates", "IN10Y")
    if not rates["missing"]:
        if "SHORT" in S:
            months, vals = spread_line(S["IN10Y"], S["SHORT"])
            kind = "repo rate" if data["SHORT"]["repo"] else "short rate"
            rates["curve"] = make_line(f"10y minus {kind}", months, vals, "level", p) if months else None
            rates["curve_missing"] = None if months else "IN10Y and SHORT share no months"
            rates["inputs"].append("SHORT")
        else:
            rates["curve"], rates["curve_missing"] = None, "needs SHORT (91-day T-bill or repo rate)"
    out.append(rates)
    out.append(level("rupee", "USDINR"))
    out.append(level("crude", "BRENT"))
    out.append(level("global_rates", "US10Y"))
    out.append(level("volatility", "VIX"))
    return out


def _locate(line: dict, month: int):
    """Index of the line's latest bar at or before `month`, or a reason it cannot be used."""
    j = bisect.bisect_right(line["months"], month) - 1
    if j < 0:
        return None, f"no data up to {mlabel(month)}"
    if month - line["months"][j] > STALE_MAX:
        return None, f"stale: data ends {mlabel(line['months'][j])}"
    if line["gap"][j] is None:
        return None, f"only {j + 1} monthly points to {mlabel(line['months'][j])}; the slow average needs more"
    return j, None


def curve_read(rates_dir, curve: dict) -> str:
    shape = {"up": "steepening", "down": "flattening"}.get(curve["direction"], "flat")
    if curve["direction"] and rates_dir:
        shape = ("bull " if rates_dir == "down" else "bear ") + shape
    return shape + (", inverted" if curve["inverted"] else "")


def evaluate(comps: list, month: int, p: dict) -> dict:
    """State of every component at `month`, and the regime read that follows from them."""
    rows, snap, vix = [], {}, {}
    for c in comps:
        row = {"key": c["key"], "title": c["title"], "line": c["line"]["label"] if c["line"] else None,
               "inputs": c["inputs"], "available": False}
        rows.append(row)
        if c["missing"]:
            row["reason"] = c["missing"]
            continue
        j, why = _locate(c["line"], month)
        if j is None:
            row["reason"] = why
            continue
        row.update(available=True, **snapshot(c["line"], j, p["recent"]))
        row["stale_months"] = month - c["line"]["months"][j]
        d = row["direction"]
        row["reads_as"] = c[d] if d else "no clear trend"
        row["effect"] = "neutral" if d is None else "supportive" if (d == "up") == (c["sign"] > 0) else "adverse"
        snap[c["key"]] = d
        if c["key"] == "rates":
            cl = c.get("curve")
            jc = _locate(cl, month)[0] if cl else None
            if jc is None:
                row["curve"] = {"available": False,
                                "reason": c.get("curve_missing") or "curve has too little history or is stale"}
            else:
                cv = {"available": True, "line": cl["label"], **snapshot(cl, jc, p["recent"])}
                spreads = cl["values"][:jc + 1]
                cv["inverted"] = spreads[-1] < 0
                run = 0
                for s in reversed(spreads):
                    if s >= 0:
                        break
                    run += 1
                cv["inverted_months"] = run
                cv["inverted_since"] = mlabel(cl["months"][jc - run + 1]) if run else None
                last_inv = next((i for i in range(jc, -1, -1) if spreads[i] < 0), None)
                cv["last_inverted"] = None if last_inv is None else mlabel(cl["months"][last_inv])
                cv["reads_as"] = curve_read(d, cv)
                row["curve"] = cv
                snap["curve"] = cv["direction"]
        if c["key"] == "volatility":
            row["level_band"], row["average_band"] = vix_band(row["value"]), vix_band(row["ma_fast"])
            vix = {"level": row["value"], "average": row["ma_fast"]}
    regime = classify(snap, vix)
    avail = [r for r in rows if r["available"]]
    turned = [r for r in avail if r["turned_recently"]]
    n, m = len(turned), len(avail)
    share = n / m if m else 0
    read = ("no component has turned: the regime is stable" if n == 0 else
            "isolated turns: watch, not yet a shift" if share < 0.25 else
            "rotation building: several gauges have turned" if share < 0.5 else
            "broad transition under way: at least half the gauges have turned")
    adverse = sum(r["effect"] == "adverse" for r in turned)
    transition = {"turned": n, "available": m, "window_months": p["recent"], "adverse": adverse,
                  "supportive": n - adverse, "read": read,
                  "text": f"{n} of {m} components turned in the last {p['recent']} months",
                  "components": [{"key": r["key"], "to": r["last_turn"]["to"], "month": r["last_turn"]["month"],
                                  "reads_as": r["reads_as"]} for r in turned]}
    grade = "full" if m >= 8 else "partial" if m >= 5 else "thin"
    coverage = {"available": m, "total": len(COMPONENTS), "grade": grade,
                "missing": [{"key": r["key"], "reason": r["reason"]} for r in rows if not r["available"]],
                "stale": [{"key": r["key"], "data_month": r["month"]} for r in avail if r["stale_months"]]}
    return {"month": mlabel(month), "regime": regime, "transition": transition, "coverage": coverage,
            "components": rows, "snap": snap, "vix": vix}


# ---------------------------------------------------------------------------
# Regime rules
# ---------------------------------------------------------------------------
LABELS = {"squeeze": "Inflationary squeeze", "risk_off": "Risk-off / contraction",
          "broadening": "Broadening risk-on", "narrow": "Narrow / concentrated",
          "easing": "Easing tailwind", "mixed": "Transitional / mixed"}
PRIORITY = ("squeeze", "risk_off", "broadening", "narrow", "easing")     # first match is the headline
RULES = {
    "squeeze": "at least two of Brent, USD/INR and the India 10-year rising, and none of the three falling",
    "risk_off": "three or more of the five equity-internal ratios falling, gold beating equities among them "
                "(defensives beating cyclicals if gold is missing); two are enough when India VIX is stressed",
    "broadening": "the breadth pair (concentration, size) rising with none falling, and at least 60% of the "
                  "available equity-internal ratios rising",
    "narrow": "the breadth pair falling with none rising, without the wider damage that makes it risk-off",
    "easing": "India 10-year falling, backed by a steepening curve, falling crude, falling US yields or a "
              "firmer rupee, and not offset by crude and USD/INR both rising",
    "mixed": "no rule above is met: the gauges disagree or too few are available",
}
# The legs quoted as evidence for (matching) or against (opposite) each label.
LEGS = {
    "squeeze": (("crude", "up"), ("rupee", "up"), ("rates", "up"), ("global_rates", "up")),
    "risk_off": tuple((k, "down") for k in INTERNALS) + (("volatility", "up"),),
    "broadening": tuple((k, "up") for k in INTERNALS) + (("volatility", "down"),),
    "narrow": (("concentration", "down"), ("size", "down"), ("financials", "down"), ("cyc_def", "down")),
    "easing": (("rates", "down"), ("curve", "up"), ("crude", "down"), ("global_rates", "down"), ("rupee", "down")),
}
TILTS = {
    "broadening": {
        "favoured": ["mid and small caps over the NIFTY 50", "domestic cyclicals: capital goods, PSU banks, "
                     "metals, realty, industrials", "the capex and PSU themes when order books are growing"],
        "pressured": ["defensives (FMCG, pharma) in relative terms", "gold as a share of the portfolio"],
        "note": "Late in a broad phase smallcap valuations stretch; the breadth pair usually rolls over first."},
    "narrow": {
        "favoured": ["index heavyweights and large-cap quality (private banks, large IT, leading consumer names)",
                     "the NIFTY 50 itself over broader indices"],
        "pressured": ["low-quality small and micro caps", "breakout trades outside the top 100, which fail more often"],
        "note": "The index can keep rising for many months on a handful of stocks; narrow is not the same as bearish."},
    "squeeze": {
        "favoured": ["dollar earners: IT services, pharma exporters", "upstream energy (ONGC, Oil India)",
                     "gold", "short-duration debt over long gilts"],
        "pressured": ["oil users: OMCs, paints, tyres, aviation", "rate-sensitives: NBFCs, realty, autos",
                      "import-heavy and high-debt balance sheets", "long-duration bonds"],
        "note": "Crude feeds the current account, the rupee and CPI at once, so RBI usually has to stay tight."},
    "easing": {
        "favoured": ["rate-sensitives: banks, NBFCs, realty, autos", "long-duration gilt funds while yields fall",
                     "leveraged cyclicals as borrowing costs drop"],
        "pressured": ["pure defensives in relative terms once growth responds"],
        "note": "Easing has often arrived before breadth improved; watch the breadth pair for the hand-over."},
    "risk_off": {
        "favoured": ["FMCG, pharma and other steady earners", "gold", "liquid and short-duration debt, cash"],
        "pressured": ["small and mid caps (deepest drawdowns)", "PSU banks, realty, metals", "leverage of any kind"],
        "note": "Capital protection first. Wait for the equity/gold and cyclical/defensive lines to turn before adding risk."},
    "mixed": {
        "favoured": ["no structural tilt: stay balanced and lean on the shorter-horizon skills"],
        "pressured": ["large one-way bets on a single factor"],
        "note": "Mixed phases resolve in either direction; the turn count shows which side is gaining."},
}


def classify(snap: dict, vix: dict | None = None) -> dict:
    """Regime from component directions. `snap` maps component key (plus "curve") to "up", "down"
    or None (available but trendless); a missing key means the component is unavailable."""
    vix = vix or {}

    def ups(keys):
        return [k for k in keys if snap.get(k) == "up"]

    def downs(keys):
        return [k for k in keys if snap.get(k) == "down"]

    matches = []
    if len(ups(PRESSURE)) >= 2 and not downs(PRESSURE):
        matches.append("squeeze")
    i_up, i_dn = ups(INTERNALS), downs(INTERNALS)
    n_int = len(i_up) + len(i_dn)
    anchor = "equity_gold" if "equity_gold" in snap else "cyc_def"
    stressed = (vix.get("level") or 0) >= 22 or (snap.get("volatility") == "up" and (vix.get("average") or 0) >= 17)
    if snap.get(anchor) == "down" and (len(i_dn) >= 3 or (stressed and len(i_dn) >= 2)):
        matches.append("risk_off")
    b_up, b_dn = ups(BREADTH), downs(BREADTH)
    if b_up and not b_dn and n_int >= 2 and len(i_up) >= math.ceil(0.6 * n_int):
        matches.append("broadening")
    if b_dn and not b_up:
        matches.append("narrow")
    support = [("curve", "up"), ("crude", "down"), ("global_rates", "down"), ("rupee", "down")]
    present = [(k, d) for k, d in support if k in snap]
    backed = not present or any(snap[k] == d for k, d in present)
    if snap.get("rates") == "down" and backed and not (snap.get("crude") == "up" and snap.get("rupee") == "up"):
        matches.append("easing")
    key = next((k for k in PRIORITY if k in matches), "mixed")
    return {"key": key, "label": LABELS[key], "rule": RULES[key],
            "also_present": [LABELS[k] for k in matches if k != key], "matches": matches,
            "internals": {"up": i_up, "down": i_dn}, "pressure": {"up": ups(PRESSURE), "down": downs(PRESSURE)},
            "vix_stressed": bool(stressed)}


def _gap_text(row: dict) -> str:
    return f"{row['gap']:+.0f}bp" if row["unit"] == "bp" else f"{row['gap']:+.1f}%"


def evidence(ev: dict) -> tuple:
    by = {r["key"]: r for r in ev["components"] if r["available"]}
    curve = by.get("rates", {}).get("curve") or {}
    if curve.get("available"):
        by["curve"] = {**curve, "reads_as": "curve " + curve["reads_as"]}
    key = ev["regime"]["key"]
    if key == "mixed":
        r = ev["regime"]
        return ([f"equity internals: {len(r['internals']['up'])} rising, {len(r['internals']['down'])} falling",
                 f"pressure gauges (crude, USD/INR, 10y): {len(r['pressure']['up'])} rising, "
                 f"{len(r['pressure']['down'])} falling"], [])
    pro, con = [], []
    for k, want in LEGS[key]:
        row = by.get(k)
        if row is None or row["direction"] is None:
            continue
        (pro if row["direction"] == want else con).append(f"{row['reads_as']} (6m/12m gap {_gap_text(row)})")
    return pro, con


def what_would_change(ev: dict, p: dict) -> list:
    """Flip one component at a time and re-run the rules; report the flips that move the headline,
    nearest crossover first."""
    by = {r["key"]: r for r in ev["components"] if r["available"]}
    curve = by.get("rates", {}).get("curve") or {}
    if curve.get("available"):
        by["curve"] = curve
    base, out = ev["regime"]["key"], []
    for k, row in by.items():
        for alt in ("up", "down"):
            if alt == row["direction"]:
                continue
            new = classify({**ev["snap"], k: alt}, ev["vix"])
            if new["key"] == base:
                continue
            band = p["band_bp"] if row["unit"] == "bp" else p["band_pct"]
            if k == "curve":
                what = "yield curve " + ("steepening" if alt == "up" else "flattening")
            else:
                what = f"{CINFO[k]['title']}: {CINFO[k][alt]}"
            out.append({"component": k, "to": alt, "new_label": new["label"], "gap": row["gap"], "unit": row["unit"],
                        "distance": abs(row["gap"]) / band if band else abs(row["gap"]),
                        "text": f"{what} (6m/12m gap now {_gap_text(row)}) -> {new['label']}"})
    out.sort(key=lambda x: x["distance"])
    return out


def history(comps: list, end: int, n: int, p: dict) -> dict:
    rows = []
    for m in range(end - n + 1, end + 1):
        ev = evaluate(comps, m, p)
        if not ev["coverage"]["available"]:
            continue
        dirs = {r["key"]: (r["direction"] or "flat") if r["available"] else None for r in ev["components"]}
        rows.append({"month": ev["month"], "label": ev["regime"]["label"], "key": ev["regime"]["key"],
                     "also_present": ev["regime"]["also_present"], "turned": ev["transition"]["turned"],
                     "available": ev["coverage"]["available"], "directions": dirs})
    spells = []
    for r in rows:
        if spells and spells[-1]["label"] == r["label"]:
            spells[-1]["end"], spells[-1]["months"] = r["month"], spells[-1]["months"] + 1
        else:
            spells.append({"label": r["label"], "start": r["month"], "end": r["month"], "months": 1})
    return {"months": rows, "spells": spells, "changes": max(0, len(spells) - 1)}


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------
def prepare(a, today: date | None = None) -> dict:
    today = today or date.today()
    for name, low in (("fast", 1), ("slow", 2), ("recent", 1)):
        if getattr(a, name) < low:
            raise MacroError(f"--{name} must be at least {low}")
    if a.fast >= a.slow:
        raise MacroError("--fast must be shorter than --slow")
    if a.band_pct < 0 or a.band_bp < 0:
        raise MacroError("--band-pct and --band-bp cannot be negative")
    p = {"fast": a.fast, "slow": a.slow, "recent": a.recent, "band_pct": a.band_pct, "band_bp": a.band_bp}
    warnings: list = []
    data, ignored = gather(a.csv_dir, a.fetch, parse_overrides(a.ticker), a.period, warnings)
    if a.as_of:
        try:
            cut = parse_date(a.as_of)
        except ValueError:
            raise MacroError(f"--as-of {a.as_of!r}: use YYYY-MM-DD")
        if re.fullmatch(r"\d{4}-\d{2}", a.as_of.strip()):          # YYYY-MM means the whole month
            cut = date(cut.year + cut.month // 12, cut.month % 12 + 1, 1) - timedelta(days=1)
        for item in data.values():
            item["obs"] = [(d, v) for d, v in item["obs"] if d <= cut]
        data = {k: v for k, v in data.items() if v["obs"]}
        if not data:
            raise MacroError(f"--as-of {a.as_of}: no data on or before that date")
    # Evaluate at the month most series end in (the later one on a tie), so a single series that
    # runs ahead does not make every other one look stale.
    lasts = [month_of(v["obs"][-1][0]) for v in data.values() if not v["repo"]] or \
            [month_of(v["obs"][-1][0]) for v in data.values()]
    end = max(set(lasts), key=lambda m: (lasts.count(m), m))

    def in_progress(month):
        daily = [v["obs"][-1][0] for v in data.values()
                 if month_of(v["obs"][-1][0]) == month and frequency([d for d, _ in v["obs"][-30:]]) == "daily"]
        if daily:
            return weekdays_left(max(daily)) >= 3
        return month == month_of(today)

    for item in data.values():
        item["obs"] = [(d, v) for d, v in item["obs"] if month_of(d) <= end]
    data = {k: v for k, v in data.items() if v["obs"]}
    partial = in_progress(end)
    if partial and a.closed_months:
        for item in data.values():
            item["obs"] = [(d, v) for d, v in item["obs"] if month_of(d) < end]
        data = {k: v for k, v in data.items() if v["obs"]}
        if not data:
            raise MacroError("--closed-months left no data: every series has only the current month")
        end, partial = end - 1, False
    if partial:
        warnings.append(f"{mlabel(end)} is still in progress: its states and any turn dated {mlabel(end)} "
                        "are provisional until month-end (--closed-months drops it)")
    data_to = max(v["obs"][-1][0] for v in data.values() if month_of(v["obs"][-1][0]) <= end)
    comps = build_components(data, end, p, warnings)
    sources = {k: {"source": v["source"], "rows": len(v["obs"]), "first": v["obs"][0][0].isoformat(),
                   "last": v["obs"][-1][0].isoformat()} for k, v in data.items()}
    return {"comps": comps, "month": end, "data_to": data_to.isoformat(), "partial": partial, "params": p,
            "warnings": warnings, "sources": sources, "ignored": ignored}


def status(ctx: dict) -> dict:
    ev = evaluate(ctx["comps"], ctx["month"], ctx["params"])
    if not ev["coverage"]["available"]:
        reasons = "; ".join(f"{m['key']}: {m['reason']}" for m in ev["coverage"]["missing"][:3])
        raise MacroError(f"no component has enough data for a {ctx['params']['slow']}-month average ({reasons} ...)")
    pro, con = evidence(ev)
    regime = {k: v for k, v in ev["regime"].items() if k != "matches"}
    regime.update(evidence_for=pro, evidence_against=con)
    changes = what_would_change(ev, ctx["params"])
    return {"as_of_month": ev["month"], "data_to": ctx["data_to"], "month_in_progress": ctx["partial"],
            "regime": regime, "transition": ev["transition"], "coverage": ev["coverage"],
            "components": ev["components"], "tilts": TILTS[ev["regime"]["key"]],
            "what_would_change": changes[:6], "params": ctx["params"], "sources": ctx["sources"],
            "ignored_files": ctx["ignored"], "warnings": ctx["warnings"]}


def component_detail(ctx: dict, name: str, months: int) -> dict:
    n = re.sub(r"[^a-z0-9_]", "", name.lower().replace("-", "_"))
    key = n if n in CINFO else COMPONENT_ALIASES.get(n.replace("_", ""))
    if key is None:
        raise MacroError(f"unknown component {name!r}; choose from: {', '.join(CINFO)}")
    c = next(c for c in ctx["comps"] if c["key"] == key)
    ev = evaluate(ctx["comps"], ctx["month"], ctx["params"])
    row = next(r for r in ev["components"] if r["key"] == key)
    out = {"as_of_month": ev["month"], "component": row, "reads": {"up": c["up"], "down": c["down"]},
           "up_is": "supportive" if c["sign"] > 0 else "adverse", "params": ctx["params"],
           "warnings": ctx["warnings"]}

    def trail(line):
        j = bisect.bisect_right(line["months"], ctx["month"]) - 1
        rows = [{"month": mlabel(line["months"][i]), "value": line["values"][i], "ma_fast": line["fast"][i],
                 "ma_slow": line["slow"][i], "gap": line["gap"][i], "direction": line["dir"][i]}
                for i in range(max(0, j - months + 1), j + 1)]
        turns = [{"month": mlabel(line["months"][i]), "from": f_, "to": t} for i, f_, t in line["turns"] if i <= j]
        return {"line": line["label"], "unit": "bp" if line["kind"] == "level" else "%", "rows": rows, "turns": turns}

    if c["line"]:
        out["series"] = trail(c["line"])
    if c.get("curve"):
        out["curve_series"] = trail(c["curve"])
    return out


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
ARROW = {"up": "↑", "down": "↓", "flat": "→", None: "·"}


def clean(x):
    """Make a structure strict-JSON safe: non-finite floats become null."""
    if isinstance(x, float):
        return x if math.isfinite(x) else None
    if isinstance(x, dict):
        return {str(k): clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [clean(v) for v in x]
    return x


def emit(obj) -> None:
    print(json.dumps(clean(obj), indent=2, allow_nan=False, default=str))


def fv(x, unit: str = "%") -> str:
    if x is None:
        return "—"
    if unit == "bp" or abs(x) < 100 and abs(x) >= 1:
        return f"{x:.2f}"
    return f"{x:,.1f}" if abs(x) >= 100 else f"{x:.4f}"


def fc(x, unit: str = "%") -> str:
    return "—" if x is None else f"{x:+.0f}bp" if unit == "bp" else f"{x:+.1f}%"


def _turn(row: dict) -> str:
    t = row.get("last_turn")
    return f"{ARROW[t['to']]} {t['month']} ({t['months_ago']}m ago)" if t else "none in sample"


def _table_row(name: str, row: dict, reads: str) -> str:
    u = row["unit"]
    return (f"{name:<14} {(row.get('line') or '')[:34]:<34} {fv(row['value'], u):>9} {fc(row['chg_6m'], u):>8} "
            f"{fc(row['chg_12m'], u):>8} {fc(row['gap'], u):>8}  {row['state']:<16} {_turn(row):<22} {reads}")


def print_status(res: dict) -> None:
    r, cov, tr, p = res["regime"], res["coverage"], res["transition"], res["params"]
    print(f"Macro regime (India, structural 6-24 month layer) | month {res['as_of_month']}"
          f"{' (in progress)' if res['month_in_progress'] else ''} | data to {res['data_to']} | "
          f"{p['fast']}m vs {p['slow']}m averages")
    print(f"Regime: {r['label']} | coverage {cov['available']}/{cov['total']} components ({cov['grade']})")
    print(f"  rule: {r['rule']}")
    if r["also_present"]:
        print(f"  also matching: {', '.join(r['also_present'])}")
    print(f"  for:     {'; '.join(r['evidence_for']) or '—'}")
    print(f"  against: {'; '.join(r['evidence_against']) or '—'}")
    print(f"Transition: {tr['text']} ({tr['adverse']} adverse, {tr['supportive']} supportive) - {tr['read']}")
    for t in tr["components"]:
        print(f"  {t['key']} {ARROW[t['to']]} {t['month']}: {t['reads_as']}")
    print(f"\n{'Component':<14} {'Line':<34} {'Last':>9} {'6m chg':>8} {'12m chg':>8} {'Gap':>8}  "
          f"{'State':<16} {'Last turn':<22} Reads as")
    for row in res["components"]:
        if not row["available"]:
            continue
        stale = f" [data {row['month']}]" if row["stale_months"] else ""
        reads = row["reads_as"] + stale
        if row["key"] == "volatility":
            reads += f"; level {row['level_band']}, {p['fast']}m average {row['average_band']}"
        print(_table_row(row["key"], row, reads))
        cv = row.get("curve")
        if cv and cv["available"]:
            extra = f"; last inverted {cv['last_inverted']}" if cv["last_inverted"] and not cv["inverted"] else \
                    f" since {cv['inverted_since']} ({cv['inverted_months']} month(s))" if cv["inverted"] else ""
            print(_table_row("  curve", cv, cv["reads_as"] + extra))
        elif cv:
            print(f"  curve        not available: {cv['reason']}")
    for m in cov["missing"]:
        print(f"Missing {m['key']}: {m['reason']}")
    t = res["tilts"]
    print(f"\nTends to favour: {'; '.join(t['favoured'])}")
    print(f"Tends to hurt:   {'; '.join(t['pressured'])}")
    print(f"Note: {t['note']}")
    print("What would change the label (one flip at a time, nearest crossover first):")
    for w in res["what_would_change"]:
        print(f"  - {w['text']}")
    if not res["what_would_change"]:
        print("  - no single component flip changes it; two or more gauges would have to turn")
    if res["ignored_files"]:
        print(f"Ignored files (name not recognised): {', '.join(res['ignored_files'])}")
    for w in res["warnings"]:
        print(f"⚠ {w}")
    print("Tilts are historical tendencies, not predictions. The turn count is a tally, not a probability.")


def print_history(res: dict) -> None:
    print(f"Regime by month ({len(res['months'])} months). Arrows: " +
          " ".join(SHORT_TAGS[c[0]] for c in COMPONENTS) + "   (· = no data, → = no trend)")
    print(f"{'Month':<8} {'Regime':<24} {'Turned':>7}  Directions")
    for r in res["months"]:
        arrows = " ".join(ARROW[r["directions"][c[0]]] for c in COMPONENTS)
        also = f"   also: {', '.join(r['also_present'])}" if r["also_present"] else ""
        print(f"{r['month']:<8} {r['label']:<24} {r['turned']:>2} of {r['available']:<2}  {arrows}{also}")
    print("\nSpells: " + "  |  ".join(f"{s['label']} {s['start']}..{s['end']} ({s['months']}m)" for s in res["spells"]))
    print(f"Label changes in the window: {res['changes']}. Few shifts per cycle is normal; treat each as one data point.")
    for w in res.get("warnings", []):
        print(f"⚠ {w}")


def print_component(res: dict) -> None:
    row = res["component"]
    print(f"{row['title']} ({row['key']}) | month {res['as_of_month']}")
    if not row["available"]:
        print(f"Not available: {row['reason']}")
        return
    u = row["unit"]
    print(f"Line: {row['line']}   up = {res['reads']['up']} ({res['up_is']} for equities), down = {res['reads']['down']}")
    print(f"State: {row['state']} -> {row['reads_as']} | last {fv(row['value'], u)} | 6m {fc(row['chg_6m'], u)} | "
          f"12m {fc(row['chg_12m'], u)} | fitted 6m slope {fc(row['slope_6m'], u)}/month | gap {fc(row['gap'], u)} | "
          f"last turn {_turn(row)}")
    for name in ("series", "curve_series"):
        s = res.get(name)
        if not s:
            continue
        print(f"\n{s['line']}")
        print(f"{'Month':<8} {'Value':>10} {'Fast avg':>10} {'Slow avg':>10} {'Gap':>8}  Dir")
        for r in s["rows"]:
            print(f"{r['month']:<8} {fv(r['value'], s['unit']):>10} {fv(r['ma_fast'], s['unit']):>10} "
                  f"{fv(r['ma_slow'], s['unit']):>10} {fc(r['gap'], s['unit']):>8}  {ARROW[r['direction'] or 'flat']}")
        turns = ", ".join(f"{t['month']} {ARROW[t['to']]}" for t in s["turns"]) or "none"
        print(f"Turns in sample: {turns}")
    cv = row.get("curve")
    if cv and cv["available"]:
        inv = f"inverted since {cv['inverted_since']}" if cv["inverted"] else \
            f"last inverted {cv['last_inverted'] or 'never in sample'}"
        print(f"\nCurve: {cv['reads_as']} | spread {cv['value']:+.2f} pts | 6m {fc(cv['chg_6m'], 'bp')} | {inv}")
    for w in res["warnings"]:
        print(f"⚠ {w}")


def print_series() -> None:
    print(f"{'Key':<12} {'Series':<34} {'Yahoo ticker':<18} {'Yahoo':<12} Where to get it")
    for key, info in INFO.items():
        print(f"{key:<12} {info['label']:<34} {info['ticker'] or '—':<18} {info['yahoo']:<12} {info['where']}")
    print("\nFile names: the key or the series name (\"NIFTY 50.csv\", \"IN10Y.csv\"), a Yahoo symbol "
          "(\"^NSEI.csv\"), or an Investing.com export name. confirmed = history served in this pack's "
          "2026-10-31 probe; unconfirmed = standard symbol, not verified here; csv-only = download it.")


def main(argv=None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    g = common.add_argument_group("input")
    g.add_argument("--csv-dir", help="folder with one CSV per series (file stem names the series)")
    g.add_argument("--fetch", action="store_true", help="with --csv-dir: fetch series the folder lacks from Yahoo")
    g.add_argument("--ticker", action="append", metavar="NAME=TICKER", help="override or add a Yahoo ticker")
    g.add_argument("--period", default="10y", help="yfinance history period (default 10y)")
    g.add_argument("--as-of", help="evaluate as of this date (YYYY-MM-DD or YYYY-MM), ignoring later rows")
    g.add_argument("--closed-months", action="store_true", help="drop the month still in progress")
    r = common.add_argument_group("method")
    r.add_argument("--fast", type=int, default=6, help="fast average in months (default 6)")
    r.add_argument("--slow", type=int, default=12, help="slow average in months (default 12)")
    r.add_argument("--recent", type=int, default=3, help="months within which a turn counts as recent (default 3)")
    r.add_argument("--band-pct", type=float, default=0.25, help="dead band around a crossover, %% (default 0.25)")
    r.add_argument("--band-bp", type=float, default=5.0, help="dead band for yield lines, bp (default 5)")
    common.add_argument("--json", action="store_true")

    p = argparse.ArgumentParser(description="Structural macro regime for Indian equities (monthly cross-asset ratios)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", parents=[common], help="regime label, evidence, component table")
    h = sub.add_parser("history", parents=[common], help="regime label month by month")
    h.add_argument("--months", type=int, default=24, help="months of history (default 24)")
    c = sub.add_parser("component", parents=[common], help="one component in detail")
    c.add_argument("name", help="one of: " + ", ".join(CINFO))
    c.add_argument("--months", type=int, default=12, help="monthly rows to show (default 12)")
    s = sub.add_parser("series", help="series list, file names, Yahoo tickers, sources")
    s.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    try:
        return _run(a)
    except MacroError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except (OSError, UnicodeDecodeError, csv.Error) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


def _run(a) -> int:
    if a.cmd == "series":
        if a.json:
            emit({"series": list(INFO.values()), "components": list(CINFO.values())})
        else:
            print_series()
        return 0
    if a.cmd in ("history", "component") and a.months < 1:
        raise MacroError("--months must be at least 1")
    ctx = prepare(a)
    if a.cmd == "status":
        res = status(ctx)
        emit(res) if a.json else print_status(res)
    elif a.cmd == "history":
        res = history(ctx["comps"], ctx["month"], a.months, ctx["params"])
        if not res["months"]:
            raise MacroError(f"no month in the window has a component with a {ctx['params']['slow']}-month average")
        res.update(as_of_month=mlabel(ctx["month"]), params=ctx["params"], warnings=ctx["warnings"])
        emit(res) if a.json else print_history(res)
    else:
        res = component_detail(ctx, a.name, a.months)
        emit(res) if a.json else print_component(res)
    return 0


if __name__ == "__main__":
    sys.exit(main())
