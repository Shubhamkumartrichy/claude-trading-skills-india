#!/usr/bin/env python3
"""Results-season toolkit for NSE/BSE stocks: how did the price react to the quarterly numbers?

Finds the right reaction session for the announcement time (same day, or the next NSE session for
after-hours and weekend/holiday announcements), measures the reaction (gap, close, range position,
volume, ATR-normalised move, follow-through, gap fill), grades it 0-100 with visible weights, watches
weekly bars for a post-results drift entry, ranks many events, and lists positions whose results
fall inside a holding window.

Zero dependencies (Python 3.10+ stdlib). Optional: `pip install yfinance` for --symbol fetches.
Loaders, SMA, ATR and weekly resampling come from ../../technical-analysis/scripts/ta.py; the NSE
holiday calendar comes from ../../fno-options-analysis/scripts/options.py.

Input
  --csv FILE | --candles FILE | --symbol NAME     daily OHLCV for one stock
  --date D --timing T                             results date and when it was announced
  --events FILE --csv-dir DIR [--prefix P]        many events (columns: symbol,date,timing)
  Dates: YYYY-MM-DD or NSE style 31-Oct-2026 (numeric DD/MM vs MM/DD is refused when ambiguous).
  Timing: pre-market | market-hours | post-market, or the announcement time HH:MM (IST).

Sub-commands
  reaction        gap, reaction-day return, close location, volume vs 50-day, ATR move, follow-through, gap fill
  score           0-100 reaction score with factor weights, grade A-D and a plain-English read
  drift           weekly post-results drift watch: pullback that holds the gap-day low, then a breakout
  scan            score and rank every event in an events file
  calendar-check  which positions have results inside a holding window (--from/--to)

Examples
  python3 results.py reaction --csv tcs.csv --date 2026-10-09 --timing post-market
  python3 results.py score --symbol HDFCBANK --date 18-Oct-2026 --timing 14:20
  python3 results.py drift --csv dixon.csv --date 2026-07-24 --timing post-market --json
  python3 results.py scan --events q2.csv --csv-dir data/ --top 10
  python3 results.py calendar-check --events q2.csv --from 2026-11-02 --to 2026-11-13 --symbols TCS,INFY
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
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parents[2]


def _sibling(skill: str, script: str):
    name = f"skill_{skill.replace('-', '_')}_{script}"
    if name in sys.modules:
        return sys.modules[name]
    path = SKILLS_DIR / skill / "scripts" / f"{script}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


ta = _sibling("technical-analysis", "ta")
nse_cal = _sibling("fno-options-analysis", "options")      # HOLIDAYS, is_trading_day

IST = timezone(timedelta(hours=5, minutes=30))
TIMINGS = ("pre-market", "market-hours", "post-market")
TIMING_ALIASES = {
    "pre-market": "pre-market", "premarket": "pre-market", "pre": "pre-market", "pre-open": "pre-market",
    "before-open": "pre-market", "before-market": "pre-market", "bmo": "pre-market",
    "market-hours": "market-hours", "during-market": "market-hours", "during": "market-hours",
    "intraday": "market-hours", "market": "market-hours", "live": "market-hours",
    "post-market": "post-market", "postmarket": "post-market", "post": "post-market",
    "after-market": "post-market", "after-hours": "post-market", "after-close": "post-market",
    "after": "post-market", "amc": "post-market", "evening": "post-market",
}
UNKNOWN_TIMING = {"", "unknown", "na", "n/a", "-", "tbd", "?"}
MARKET_OPEN, MARKET_CLOSE, CLOSE_VWAP_FROM = time(9, 15), time(15, 30), time(15, 0)

FOLLOW_SESSIONS = (1, 3, 5, 10, 20)
FLAT_GAP_PCT = 0.5          # |gap| below this is "no gap"
CIRCUIT_BANDS = (5.0, 10.0, 20.0)
MIN_BARS_BEFORE = 20

# Score weights (sum 100). See SKILL.md for the reasoning behind each factor.
WEIGHTS = {"reaction": 35, "volume": 20, "close": 15, "trend": 20, "pre_run": 10}
TREND_PARTS = {"above_200": 7, "above_50": 6, "near_high": 7}
REACTION_FULL_ATR = 3.0     # a close >= 3 ATR above the pre-results close earns full reaction points
GATE_FULL_ATR = 1.0         # context factors count in full once the reaction is >= +1 ATR (0 at <= 0)
VOLUME_FULL_X = 4.0         # 4x the 50-day average volume earns full volume points (1x earns none)
NEAR_HIGH_ZERO_PCT = 10.0   # 10% or more under the pre-results 52-week high earns no near-high points
PRE_RUN_FREE, PRE_RUN_ZERO = 10.0, 40.0   # 60-session run-up into results: <=10% full, >=40% none
GRADE_CUTS = ((80, "A"), (60, "B"), (40, "C"), (0, "D"))

MONTH_NAMES = ("january", "february", "march", "april", "may", "june", "july", "august", "september",
               "october", "november", "december")


class ResultsError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Dates and timing
# ---------------------------------------------------------------------------
_CLOCK = re.compile(r"^(\d{1,2})[:.](\d{2})(?::\d{2})?\s*([AaPp][Mm])?\s*(?:IST|ist|hrs|Hrs)?$")


def _clock(text: str) -> time | None:
    m = _CLOCK.match(text.strip())
    if not m:
        return None
    hh, mm, ampm = int(m.group(1)), int(m.group(2)), (m.group(3) or "").lower()
    if ampm:
        if not 1 <= hh <= 12:
            raise ResultsError(f"bad time {text!r}")
        hh = hh % 12 + (12 if ampm == "pm" else 0)
    if hh > 23 or mm > 59:
        raise ResultsError(f"bad time {text!r}")
    return time(hh, mm)


def _month(word: str) -> int | None:
    w = word.strip().lower().rstrip(".")
    for i, full in enumerate(MONTH_NAMES, 1):
        if w in (full, full[:3]) or (i == 9 and w == "sept"):
            return i
    return None


def parse_date(text, what: str = "--date") -> tuple[date, time | None]:
    """Parse a results date (optionally with a time). Refuses numeric dates that could be DD/MM or MM/DD."""
    raw = str(text or "").strip()
    if not raw:
        raise ResultsError(f"{what}: empty date")
    clock = None
    m = re.match(r"^(.*?)(?:[ T]+|,\s*)(\d{1,2}[:.]\d{2}(?::\d{2})?\s*(?:[AaPp][Mm])?\s*(?:IST|ist)?)$", raw)
    if m and _clock(m.group(2)) is not None:
        raw, clock = m.group(1).strip().rstrip(","), _clock(m.group(2))
    hint = "use YYYY-MM-DD or DD-Mon-YYYY, e.g. 2026-10-31 or 31-Oct-2026"
    y = mo = d = None
    if mm := re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", raw):
        y, mo, d = (int(g) for g in mm.groups())
    elif mm := re.fullmatch(r"(\d{4})(\d{2})(\d{2})", raw):
        y, mo, d = (int(g) for g in mm.groups())
    elif mm := re.fullmatch(r"(\d{1,2})[-\s/.]+([A-Za-z]{3,9})[-\s/.,]+(\d{2,4})", raw):
        mo = _month(mm.group(2))
        d, y = int(mm.group(1)), mm.group(3)
        if len(y) != 4:
            raise ResultsError(f"{what}: {text!r} has a 2-digit year; {hint}")
        y = int(y)
    elif mm := re.fullmatch(r"([A-Za-z]{3,9})[-\s/.]+(\d{1,2}),?[-\s/.]+(\d{4})", raw):
        mo, d, y = _month(mm.group(1)), int(mm.group(2)), int(mm.group(3))
    elif mm := re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})", raw):
        a, b, yy = int(mm.group(1)), int(mm.group(2)), mm.group(3)
        if len(yy) != 4:
            raise ResultsError(f"{what}: {text!r} has a 2-digit year; {hint}")
        y = int(yy)
        if a > 12 >= b:
            d, mo = a, b
        elif b > 12 >= a:
            mo, d = a, b
        elif a == b:
            d = mo = a
        else:
            raise ResultsError(f"{what}: {text!r} is ambiguous (day {a} of month {b}, or month {a} day {b}?); {hint}")
    if mo is None or y is None:
        raise ResultsError(f"{what}: unrecognised date {text!r}; {hint}")
    try:
        return date(y, mo, d), clock
    except ValueError:
        raise ResultsError(f"{what}: {text!r} is not a real calendar date")


def timing_from_clock(t: time) -> str:
    if t < MARKET_OPEN:
        return "pre-market"
    return "market-hours" if t < MARKET_CLOSE else "post-market"


def parse_timing(text) -> tuple[str | None, time | None]:
    """Return (timing, clock). Unknown/blank gives (None, None)."""
    s = str(text or "").strip()
    if s.lower() in UNKNOWN_TIMING:
        return None, None
    t = _clock(s)
    if t is not None:
        return timing_from_clock(t), t
    key = re.sub(r"[\s_]+", "-", s.lower())
    if key in TIMING_ALIASES:
        return TIMING_ALIASES[key], None
    raise ResultsError(f"unknown timing {text!r}: use pre-market, market-hours, post-market or the time HH:MM (IST)")


def resolve_timing(timing_text, clock_from_date: time | None, where: str) -> tuple[str | None, time | None]:
    timing, clock = parse_timing(timing_text) if timing_text is not None else (None, None)
    if clock_from_date is not None:
        derived = timing_from_clock(clock_from_date)
        if timing and timing != derived:
            raise ResultsError(f"{where}: timing {timing!r} contradicts the time {clock_from_date:%H:%M} "
                               f"({derived}) in the date")
        return derived, clock_from_date
    return timing, clock


# ---------------------------------------------------------------------------
# NSE session calendar
# ---------------------------------------------------------------------------
HOLIDAY_YEARS = (min(nse_cal.HOLIDAYS).year, nse_cal.HOLIDAYS_KNOWN_UNTIL.year)


def is_session(d: date) -> bool:
    return nse_cal.is_trading_day(d)


def next_session(d: date) -> date:
    """First NSE session on or after d."""
    while not is_session(d):
        d += timedelta(days=1)
    return d


def calendar_verified(d: date) -> bool:
    return HOLIDAY_YEARS[0] <= d.year <= HOLIDAY_YEARS[1]


def reaction_session(results_date: date, timing: str) -> date:
    """Session whose prices first reflect the results.

    Pre-market and market-hours announcements on a trading day react the same day; post-market
    announcements, and anything released on a weekend or exchange holiday, react on the next session.
    """
    if timing not in TIMINGS:
        raise ResultsError(f"timing must be one of {', '.join(TIMINGS)}")
    if timing == "post-market" or not is_session(results_date):
        return next_session(results_date + timedelta(days=1))
    return results_date


def exposure_sessions(results_date: date, timing: str | None) -> list:
    """Sessions that can carry the reaction. Unknown timing counts both candidates."""
    if timing:
        return [reaction_session(results_date, timing)]
    if is_session(results_date):
        return [results_date, next_session(results_date + timedelta(days=1))]
    return [next_session(results_date)]


def sessions_after(a: date, b: date) -> int:
    """Number of NSE sessions in (a, b]."""
    n, d = 0, a
    while d < b:
        d += timedelta(days=1)
        n += is_session(d)
    return n


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------
def _usable(rows: list) -> list:
    """Drop null/zero/broken candles (feeds emit them for holidays and suspended days)."""
    out = []
    for r in rows:
        vals = [r.get(k) for k in ("open", "high", "low", "close")]
        if any(v is None or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0 for v in vals):
            continue
        if r["high"] < r["low"]:
            continue
        v = r.get("volume")
        r["volume"] = float(v) if isinstance(v, (int, float)) and math.isfinite(v) and v > 0 else 0.0
        out.append(r)
    return out


def load_candles(path: str) -> list:
    """Broker-MCP candle JSON; candles with null fields are skipped instead of crashing."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict):
        for key in ("candles", "data", "result", "payload"):
            if key in data:
                data = data[key]
                if isinstance(data, dict) and "candles" in data:
                    data = data["candles"]
                break
    if isinstance(data, dict):            # column arrays: let ta.py handle the shape
        try:
            return _usable(ta.load_candles_json(path))
        except (TypeError, KeyError) as e:
            raise ResultsError(f"{path}: unreadable candle columns ({e})")
    if not isinstance(data, list):
        raise ResultsError(f"{path}: unrecognised candle JSON")
    rows = []
    for c in data:
        try:
            if isinstance(c, (list, tuple)):
                ts, o, h, lo, cl = c[:5]
                v = c[5] if len(c) > 5 else 0
            elif isinstance(c, dict):
                low = {str(k).lower(): val for k, val in c.items()}
                ts = next((low[k] for k in ("date", "timestamp", "time", "datetime") if low.get(k) is not None), None)
                o, h, lo, cl, v = (low.get(k) for k in ("open", "high", "low", "close", "volume"))
            else:
                continue
            if ts is None or None in (o, h, lo, cl):
                continue
            rows.append({"date": ta.parse_ts(ts), "open": float(o), "high": float(h), "low": float(lo),
                         "close": float(cl), "volume": float(v or 0)})
        except (ValueError, TypeError):
            continue
    return _usable(ta._clean(rows))


def load_rows(csv_path=None, candles=None, symbol=None, exchange="NSE", period="2y") -> tuple[list, str]:
    if csv_path:
        rows, name = ta.load_csv(csv_path), symbol or Path(csv_path).stem
    elif candles:
        rows, name = load_candles(candles), symbol or Path(candles).stem
    elif symbol:
        rows, name = ta.fetch_yf(symbol, exchange, period, "1d"), symbol.upper()
    else:
        raise ResultsError("give --csv, --candles or --symbol (or --events with --csv-dir)")
    rows = _usable(rows)
    if not rows:
        raise ResultsError(f"no usable OHLC rows in {csv_path or candles or symbol}")
    return rows, name


# ---------------------------------------------------------------------------
# Reaction
# ---------------------------------------------------------------------------
def locate_reaction(rows: list, results_date: date, timing: str, override: date | None = None):
    dates = [r["date"].date() for r in rows]
    notes = []
    expected = reaction_session(results_date, timing)
    if override is not None:
        if override not in dates:
            raise ResultsError(f"--reaction-date {override} is not a session in the data")
        if override < results_date:
            raise ResultsError("--reaction-date cannot be before the results date")
        notes.append(f"Reaction session set manually to {override} (calendar says {expected}).")
        return dates.index(override), expected, notes
    idx = bisect.bisect_left(dates, expected)
    if idx >= len(dates):
        raise ResultsError(f"the data ends {dates[-1]}, before the reaction session {expected}: "
                           "results not out yet, or refresh the data")
    if dates[idx] != expected:
        notes.append(f"No bar for the expected reaction session {expected} (data gap or an unlisted holiday); "
                     f"using the next bar, {dates[idx]}.")
    first_open = results_date if (timing != "post-market" and is_session(results_date)) else results_date + timedelta(days=1)
    extra = [x for x in dates if first_open <= x < expected]
    if extra:
        notes.append(f"The data has a session on {extra[0]} that the NSE calendar treats as closed (Muhurat or "
                     f"special session?). It was skipped; pass --reaction-date {extra[0]} to use it.")
    if not calendar_verified(expected):
        notes.append(f"The NSE holiday list covers {HOLIDAY_YEARS[0]}-{HOLIDAY_YEARS[1]} only; check that "
                     f"{dates[idx]} really was the first session after the announcement.")
    return idx, expected, notes


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def analyse_reaction(rows: list, results_date: date, timing: str, name: str = "", clock: time | None = None,
                     reaction_date: date | None = None) -> dict:
    idx, expected, notes = locate_reaction(rows, results_date, timing, reaction_date)
    if idx < MIN_BARS_BEFORE:
        raise ResultsError(f"need at least {MIN_BARS_BEFORE} sessions before the reaction day "
                           f"{rows[idx]['date'].date() if idx < len(rows) else expected}; got {idx}")
    o = [r["open"] for r in rows]
    h = [r["high"] for r in rows]
    lo = [r["low"] for r in rows]
    c = [r["close"] for r in rows]
    v = [r["volume"] for r in rows]
    r, prev_close = rows[idx], c[idx - 1]

    atr_pre = ta.atr(h, lo, c, 14)[idx - 1]
    if not atr_pre:
        raise ResultsError("ATR is zero before the reaction day (flat or broken data)")
    gap_pct = (o[idx] / prev_close - 1) * 100
    ret_pct = (c[idx] / prev_close - 1) * 100
    rng = h[idx] - lo[idx]
    if rng > 0:
        close_loc = (c[idx] - lo[idx]) / rng
    else:
        close_loc = 1.0 if c[idx] > prev_close else 0.0 if c[idx] < prev_close else 0.5
        notes.append("Single-price reaction day (high = low): typical of a circuit-locked stock.")

    prior_v = [x for x in v[max(0, idx - 50):idx] if x > 0]
    vol_ratio = vol_avg = None
    if v[idx] > 0 and len(prior_v) >= 10:
        vol_avg = sum(prior_v) / len(prior_v)
        vol_ratio = v[idx] / vol_avg
        if len(prior_v) < 50:
            notes.append(f"Volume average uses {len(prior_v)} sessions, not 50.")
    else:
        notes.append("No usable volume (index or patchy feed): the volume factor is left out of the score.")

    sma50 = ta.sma(c, 50)[idx - 1]
    sma200 = ta.sma(c, 200)[idx - 1]
    hi_window = h[max(0, idx - 250):idx]
    high_52w = max(hi_window)
    if len(hi_window) < 250:
        notes.append(f"52-week high taken from {len(hi_window)} sessions of history.")
    ret20 = (c[idx - 1] / c[idx - 21] - 1) * 100 if idx >= 21 else None
    ret60 = (c[idx - 1] / c[idx - 61] - 1) * 100 if idx >= 61 else None

    follow = []
    for n in FOLLOW_SESSIONS:
        j = idx + n
        if j >= len(rows):
            break
        follow.append({"sessions": n, "date": rows[j]["date"].date(), "close": c[j],
                       "from_reaction_close_pct": (c[j] / c[idx] - 1) * 100,
                       "from_pre_close_pct": (c[j] / prev_close - 1) * 100,
                       "held_reaction_low": min(lo[idx + 1:j + 1]) >= lo[idx]})

    direction = "up" if gap_pct >= FLAT_GAP_PCT else "down" if gap_pct <= -FLAT_GAP_PCT else "none"
    fill = {"direction": direction, "level": prev_close, "filled": None, "date": None, "sessions": None,
            "sessions_checked": len(rows) - idx - 1}
    if direction != "none":
        fill["filled"] = False
        for k in range(idx, len(rows)):
            if (direction == "up" and lo[k] <= prev_close) or (direction == "down" and h[k] >= prev_close):
                fill.update(filled=True, date=rows[k]["date"].date(), sessions=k - idx)
                break

    if timing == "market-hours":
        notes.append("Announced during market hours: the open came before the news, so judge the reaction by "
                     "the close, not the gap.")
        if clock and clock >= CLOSE_VWAP_FROM:
            notes.append("Announced in the last 30 minutes: NSE's close is the volume-weighted price of "
                         "15:00-15:30, so much of the reaction may show only on the next session. Compare with "
                         "--timing post-market.")
    if not is_session(results_date):
        notes.append(f"Announced on a non-trading day ({results_date:%a %d-%b-%Y}); the reaction is the next session.")
    for band in CIRCUIT_BANDS:
        if close_loc >= 0.98 and abs(ret_pct - band) <= 0.3:
            notes.append(f"Closed at the high, about +{band:g}%: possibly locked at a {band:g}% upper circuit "
                         "(non-F&O stock). Buyers may not have been filled; expect a gap next session.")
        if close_loc <= 0.02 and abs(ret_pct + band) <= 0.3:
            notes.append(f"Closed at the low, about -{band:g}%: possibly locked at a {band:g}% lower circuit. "
                         "Holders could not exit; stops do not fill in a locked circuit.")

    return {
        "name": name, "results_date": results_date, "timing": timing,
        "announced_at": clock.strftime("%H:%M") if clock else None,
        "reaction_date": rows[idx]["date"].date(), "expected_reaction_date": expected,
        "pre_results_date": rows[idx - 1]["date"].date(), "prev_close": prev_close,
        "bar": {k: r[k] for k in ("open", "high", "low", "close", "volume")},
        "gap_pct": gap_pct, "reaction_pct": ret_pct, "intraday_pct": (c[idx] / o[idx] - 1) * 100,
        "range_pct": rng / prev_close * 100, "close_location": close_loc,
        "volume_ratio": vol_ratio, "volume_avg_50d": vol_avg,
        "atr14_pre": atr_pre, "atr_pct_pre": atr_pre / prev_close * 100,
        "gap_atr": (o[idx] - prev_close) / atr_pre, "move_atr": (c[idx] - prev_close) / atr_pre,
        "pre": {"ret20_pct": ret20, "ret60_pct": ret60, "sma50": sma50, "sma200": sma200, "high_52w": high_52w,
                "above_sma50": None if sma50 is None else c[idx] > sma50,
                "above_sma200": None if sma200 is None else c[idx] > sma200,
                "from_52w_high_pct": (c[idx] / high_52w - 1) * 100},
        "follow_through": follow, "gap_fill": fill,
        "sessions_after": len(rows) - idx - 1, "last_date": rows[-1]["date"].date(), "last_close": c[-1],
        "notes": notes, "_idx": idx,
    }


# ---------------------------------------------------------------------------
# Score
# ---------------------------------------------------------------------------
def grade_for(score: float) -> str:
    return next(g for cut, g in GRADE_CUTS if score >= cut)


def score_factors(move_atr: float, volume_ratio: float | None, close_location: float,
                  above_sma50: bool | None, above_sma200: bool | None, from_high_pct: float | None,
                  ret60_pct: float | None) -> dict:
    """Pure scoring. Each factor yields a fraction in [0, 1] (or None when unknown) times its weight.

    Missing factors are dropped and the total is rescaled to 100 over the factors that remain.
    Volume, trend and run-up are context: they only count when the market rewarded the numbers, so
    they are multiplied by a gate that is 0 for a flat or negative reaction and 1 from +1 ATR up.
    """
    gate = _clamp(move_atr / GATE_FULL_ATR)
    fr = {"reaction": _clamp(move_atr / REACTION_FULL_ATR)}
    fr["volume"] = None if volume_ratio is None else _clamp((volume_ratio - 1) / (VOLUME_FULL_X - 1)) * gate
    fr["close"] = _clamp(close_location)
    parts = {"above_200": None if above_sma200 is None else float(above_sma200),
             "above_50": None if above_sma50 is None else float(above_sma50),
             "near_high": None if from_high_pct is None else _clamp(1 + from_high_pct / NEAR_HIGH_ZERO_PCT)}
    have = {k: x for k, x in parts.items() if x is not None}
    fr["trend"] = gate * sum(TREND_PARTS[k] * x for k, x in have.items()) / sum(TREND_PARTS[k] for k in have) \
        if have else None
    fr["pre_run"] = None if ret60_pct is None else \
        gate * _clamp((PRE_RUN_ZERO - ret60_pct) / (PRE_RUN_ZERO - PRE_RUN_FREE))
    avail = {k: x for k, x in fr.items() if x is not None}
    wsum = sum(WEIGHTS[k] for k in avail)
    total = 100 * sum(WEIGHTS[k] * x for k, x in avail.items()) / wsum
    score = int(round(total))
    return {"score": score, "grade": grade_for(score), "fractions": fr, "trend_parts": parts, "gate": gate,
            "dropped": [k for k, x in fr.items() if x is None], "rescaled": wsum != 100}


def _pct(x, d=1):
    return "—" if x is None else f"{x:+.{d}f}%"


def score_reaction(rx: dict) -> dict:
    pre = rx["pre"]
    s = score_factors(rx["move_atr"], rx["volume_ratio"], rx["close_location"], pre["above_sma50"],
                      pre["above_sma200"], pre["from_52w_high_pct"], pre["ret60_pct"])
    fr = s["fractions"]
    yes = {True: "yes", False: "no", None: "n/a"}
    detail = {
        "reaction": (f"{rx['move_atr']:+.2f} ATR ({rx['reaction_pct']:+.1f}%)",
                     f"close vs pre-results close in ATR(14); full at +{REACTION_FULL_ATR:g} ATR"),
        "volume": ("n/a" if rx["volume_ratio"] is None else f"{rx['volume_ratio']:.2f}x 50-day avg",
                   f"1x earns 0, {VOLUME_FULL_X:g}x earns full; x gate"),
        "close": (f"{rx['close_location']:.2f} of the day's range", "0 = closed at the low, 1 = at the high"),
        "trend": (f">200DMA {yes[pre['above_sma200']]}, >50DMA {yes[pre['above_sma50']]}, "
                  f"{_pct(pre['from_52w_high_pct'])} vs 52w high",
                  f"200DMA {TREND_PARTS['above_200']}, 50DMA {TREND_PARTS['above_50']}, "
                  f"near high {TREND_PARTS['near_high']} (0 at -{NEAR_HIGH_ZERO_PCT:g}%); x gate"),
        "pre_run": (f"{_pct(pre['ret60_pct'])} over 60 sessions",
                    f"<= +{PRE_RUN_FREE:g}% full, >= +{PRE_RUN_ZERO:g}% none (priced in); x gate"),
    }
    labels = {"reaction": "Reaction size", "volume": "Volume expansion", "close": "Close strength",
              "trend": "Trend position", "pre_run": "Fresh surprise (no big run-up)"}
    wsum = sum(WEIGHTS[k] for k in fr if fr[k] is not None)
    factors = []
    for k in WEIGHTS:
        pts = None if fr[k] is None else WEIGHTS[k] * fr[k] * 100 / wsum
        factors.append({"key": k, "label": labels[k], "weight": WEIGHTS[k], "value": detail[k][0],
                        "rule": detail[k][1], "fraction": fr[k], "points": pts})
    bias = "bullish" if rx["move_atr"] >= 1 else "bearish" if rx["move_atr"] <= -1 else "neutral"
    return {"score": s["score"], "grade": s["grade"], "bias": bias, "gate": s["gate"], "factors": factors,
            "dropped": s["dropped"],
            "rescaled": s["rescaled"], "read": plain_read(rx, s["grade"], bias)}


def plain_read(rx: dict, grade: str, bias: str) -> str:
    pre, m = rx["pre"], rx["move_atr"]
    size = "a large" if abs(m) >= 2.5 else "a solid" if abs(m) >= 1.5 else "a modest" if abs(m) >= 0.75 else "a muted"
    if rx["timing"] != "market-hours" and rx["gap_fill"]["direction"] != "none":
        s = (f"Gapped {rx['gap_fill']['direction']} {rx['gap_pct']:+.1f}% and closed {rx['reaction_pct']:+.1f}% "
             f"on {rx['reaction_date']} ({m:+.1f} ATR, {size} move)")
    else:
        s = f"Closed {rx['reaction_pct']:+.1f}% on {rx['reaction_date']} ({m:+.1f} ATR, {size} move)"
    vr = rx["volume_ratio"]
    if vr is not None:
        s += (f" on {vr:.1f}x normal volume" if vr >= 1.5 else f" on ordinary volume ({vr:.1f}x)")
    cl = rx["close_location"]
    if cl >= 0.75:
        s += ", finishing near the day's high."
    elif cl <= 0.25:
        s += ", finishing near the day's low."
    else:
        s += ", finishing mid-range."
    out = [s]
    if rx["gap_pct"] >= 2 and cl <= 0.35:
        out.append("The gap faded: sellers used the news to exit.")
    if rx["gap_pct"] <= -2 and cl >= 0.65:
        out.append("Buyers absorbed the gap-down into the close.")
    if vr is not None and vr < 1.5 and abs(m) >= 1:
        out.append("Light volume for a results move: little sign of institutional repositioning.")
    fh = pre["from_52w_high_pct"]
    if pre["above_sma200"] and pre["above_sma50"] and fh is not None and fh >= -3:
        out.append("It now trades above the 50- and 200-DMA, at or near a 52-week high (little overhead supply).")
    elif pre["above_sma200"] is False:
        out.append("It is still below the 200-DMA, so the longer trend has not turned.")
    if pre["ret60_pct"] is not None and pre["ret60_pct"] >= 25:
        out.append(f"It ran {pre['ret60_pct']:+.0f}% in the 60 sessions before results; much may have been priced in.")
    verdict = {
        "A": "Leader-type reaction: watch for a weekly pullback that holds the reaction-day low (run drift) "
             "instead of chasing the gap.",
        "B": "Constructive reaction: worth a drift watch; size normally only after a pullback entry triggers.",
        "C": "Mixed reaction: no edge from the results alone; let the chart decide.",
        "D": "Weak or negative reaction: avoid fresh longs until a new base forms; holders should respect stops."}
    if bias == "bearish":
        verdict["D"] = ("Clearly negative reaction on the numbers: avoid fresh longs. High-volume results moves "
                        "often keep drifting the same way for weeks; holders should respect stops.")
    out.append(verdict[grade])
    return " ".join(out)


# ---------------------------------------------------------------------------
# Drift watch (weekly)
# ---------------------------------------------------------------------------
def drift_watch(rows: list, rx: dict, min_gap_pct: float = 4.0, max_weeks: int = 12,
                stop_buffer_pct: float = 0.5) -> dict:
    idx = rx["_idx"]
    gap_low = rows[idx]["low"]
    gain = rx["reaction_pct"] if rx["timing"] == "market-hours" else max(rx["gap_pct"], rx["reaction_pct"])
    out = {"status": None, "reaction_date": rx["reaction_date"], "reaction_gain_pct": gain,
           "min_gap_pct": min_gap_pct, "gap_day_low": gap_low, "gap_day_high": rows[idx]["high"],
           "pullback": None, "trigger": None, "stop": None, "risk_pct": None, "entry": None, "exit": None,
           "failed": None, "weeks_after": 0, "max_weeks": max_weeks, "last_date": rows[-1]["date"].date(),
           "last_close": rows[-1]["close"], "r_multiple_now": None, "notes": []}
    if not (gain >= min_gap_pct and rx["reaction_pct"] > 0):
        out["status"] = "not-applicable"
        out["notes"].append(f"No strong gap-up: reaction gain {gain:+.1f}% (needs >= +{min_gap_pct:g}% and a "
                            "green close vs the pre-results close).")
        return _drift_finish(out, rows)

    week_key = lambda i: tuple(rows[i]["date"].isocalendar()[:2])
    rkey = week_key(idx)
    j = idx + 1
    tail = []                                   # rest of the reaction week (daily failure check only)
    while j < len(rows) and week_key(j) == rkey:
        tail.append(j)
        j += 1
    weeks, keys = [], []
    for i in range(j, len(rows)):
        k = week_key(i)
        if not keys or keys[-1] != k:
            keys.append(k)
            weeks.append([])
        weeks[-1].append(i)
    bars = ta.resample(rows[j:], "W") if j < len(rows) else []
    out["weeks_after"] = len(weeks)

    def fail(i):
        out["failed"] = {"date": rows[i]["date"].date(), "close": rows[i]["close"]}
        out["status"] = "failed"

    for i in tail:
        if rows[i]["close"] < gap_low:
            fail(i)
            return _drift_finish(out, rows)

    state, pb = "watching", None
    for k, (days, wb) in enumerate(zip(weeks, bars), start=1):
        if state in ("watching", "pullback-set") and k > max_weeks:
            state = "expired"
            break
        if state == "watching":
            hit = next((i for i in days if rows[i]["close"] < gap_low), None)
            if hit is not None:
                fail(hit)
                return _drift_finish(out, rows)
            if wb["close"] < wb["open"]:
                if wb["low"] > gap_low:
                    pb = {"week_ending": wb["date"].date(), "open": wb["open"], "high": wb["high"],
                          "low": wb["low"], "close": wb["close"], "weeks": 1, "week_number": k}
                    state = "pullback-set"
                else:
                    out["notes"].append(f"Week ending {wb['date'].date()} dipped under the gap-day low intraday but "
                                        "closed above it: a shakeout, not a valid pullback.")
            continue
        # pullback-set: a day trading above the pullback high triggers; a close under the gap-day low fails
        trigger = pb["high"]
        for i in days:
            if rows[i]["high"] > trigger:
                fill_px = max(trigger, rows[i]["open"])
                out["entry"] = {"date": rows[i]["date"].date(), "price": fill_px, "gapped_above": fill_px > trigger}
                state, entry_i = "triggered", i
                break
            if rows[i]["close"] < gap_low:
                fail(i)
                return _drift_finish(out, rows, pb, stop_buffer_pct)
        if state == "triggered":
            break
        if wb["close"] < wb["open"] and wb["low"] > gap_low:      # pullback extends: tighter trigger
            pb.update(high=wb["high"], low=min(pb["low"], wb["low"]), close=wb["close"],
                      week_ending=wb["date"].date(), weeks=pb["weeks"] + 1)
    if state == "triggered":
        stop = pb["low"] * (1 - stop_buffer_pct / 100)
        for i in range(entry_i + 1, len(rows)):
            if rows[i]["low"] <= stop:
                out["exit"] = {"date": rows[i]["date"].date(), "price": min(stop, rows[i]["open"]),
                               "reason": "stop"}
                state = "stopped"
                break
    out["status"] = state
    return _drift_finish(out, rows, pb, stop_buffer_pct)


def _drift_finish(out: dict, rows: list, pb: dict | None = None, stop_buffer_pct: float = 0.5) -> dict:
    if pb:
        out["pullback"] = pb
        out["trigger"] = pb["high"]
        out["stop"] = pb["low"] * (1 - stop_buffer_pct / 100)
        out["risk_pct"] = (out["trigger"] - out["stop"]) / out["trigger"] * 100
        if out["entry"]:
            risk = out["entry"]["price"] - out["stop"]
            ref = out["exit"]["price"] if out["exit"] else rows[-1]["close"]
            out["r_multiple_now"] = (ref - out["entry"]["price"]) / risk if risk > 0 else None
    fresh = ta.freshness(rows)
    if fresh and out["status"] in ("watching", "pullback-set", "triggered"):
        out["notes"].append(fresh)
    out["plan"] = drift_plan(out)
    return out


def drift_plan(d: dict) -> list:
    inr = fmt_inr
    s = d["status"]
    if s == "not-applicable":
        return ["Drift watch applies only after a strong gap-up; nothing to do."]
    if s == "failed":
        return [f"Failed reaction: closed {inr(d['failed']['close'])} below the gap-day low {inr(d['gap_day_low'])} "
                f"on {d['failed']['date']}. The market rejected the results move; drop it from the watch."]
    if s == "watching":
        return [f"Waiting for the first red weekly candle that holds above the gap-day low {inr(d['gap_day_low'])} "
                f"({d['weeks_after']} of {d['max_weeks']} weeks used).",
                f"Invalidated by a daily close below {inr(d['gap_day_low'])}."]
    if s == "expired":
        return [f"No pullback entry within {d['max_weeks']} weeks of the reaction; the drift edge has faded."]
    lines = [f"Pullback week ending {d['pullback']['week_ending']} ({d['pullback']['weeks']} red week(s)): "
             f"high {inr(d['pullback']['high'])}, low {inr(d['pullback']['low'])}."]
    if s == "pullback-set":
        lines += [f"Entry: buy-stop / GTT just above {inr(d['trigger'])}; stop {inr(d['stop'])} "
                  f"(pullback low minus buffer); risk {d['risk_pct']:.1f}% per share.",
                  f"Invalidated by a daily close below the gap-day low {inr(d['gap_day_low'])}. "
                  "Size it with position-sizing or breakout.py plan --pivot/--stop."]
    elif s == "triggered":
        lines += [f"Triggered {d['entry']['date']} at {inr(d['entry']['price'])}"
                  + (" (gapped above the trigger)" if d["entry"]["gapped_above"] else "")
                  + f"; stop {inr(d['stop'])}; last close {inr(d['last_close'])} = {d['r_multiple_now']:+.1f}R.",
                  "Manage the open trade with trailing stops (breakeven at 1R)."]
    elif s == "stopped":
        lines += [f"Triggered {d['entry']['date']} at {inr(d['entry']['price'])}, stopped "
                  f"{d['exit']['date']} at {inr(d['exit']['price'])} ({d['r_multiple_now']:+.1f}R)."]
    return lines


# ---------------------------------------------------------------------------
# Events files, scan, calendar check
# ---------------------------------------------------------------------------
SYMBOL_COLS = ("symbol", "ticker", "tradingsymbol", "trading symbol", "scrip", "nse symbol", "stock")
DATE_COLS = ("date", "results date", "result date", "results_date", "board meeting date", "meeting date",
             "bm date", "event date", "announcement date")
TIMING_COLS = ("timing", "time", "session", "when", "announced")
FILE_COLS = ("file", "csv", "path")
PURPOSE_COLS = ("purpose", "agenda", "subject")


def _col(cols: dict, names) -> str | None:
    return next((cols[n] for n in names if n in cols), None)


def load_events(path: str) -> tuple[list, int]:
    """Events CSV -> list of {symbol, date, timing|None, clock, file, line}. Bad dates are fatal."""
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        cols = {c.strip().lower(): c for c in (reader.fieldnames or []) if c}
        sc, dc = _col(cols, SYMBOL_COLS), _col(cols, DATE_COLS)
        if not sc or not dc:
            raise ResultsError(f"{path}: events file needs symbol and date columns (symbol,date,timing); "
                               f"found {list(cols)}")
        tc, fc, pc = _col(cols, TIMING_COLS), _col(cols, FILE_COLS), _col(cols, PURPOSE_COLS)
        events, skipped = [], 0
        for n, r in enumerate(reader, start=2):
            sym = (r.get(sc) or "").strip().upper()
            raw = (r.get(dc) or "").strip()
            if not sym and not raw:
                continue
            if pc and (r.get(pc) or "").strip() and not re.search(r"result|financ", r.get(pc) or "", re.I):
                skipped += 1
                continue
            where = f"{Path(path).name} line {n}"
            if not sym or not raw:
                raise ResultsError(f"{where}: needs both a symbol and a date")
            d, clock = parse_date(raw, where)
            try:
                timing, clock = resolve_timing((r.get(tc) or "") if tc else None, clock, where)
            except ResultsError as e:
                raise ResultsError(f"{where}: {e}" if not str(e).startswith(where) else str(e))
            events.append({"symbol": sym, "date": d, "timing": timing, "clock": clock,
                           "file": (r.get(fc) or "").strip() if fc else "", "line": n})
    if not events:
        raise ResultsError(f"{path}: no events found")
    return events, skipped


def find_csv(csv_dir: Path, symbol: str, prefix: str = "", file: str = "") -> Path:
    if file:
        p = (csv_dir / file) if not Path(file).is_absolute() else Path(file)
        if p.is_file():
            return p
        raise ResultsError(f"file {file} for {symbol} not found")
    want = f"{prefix}{symbol}.csv".lower()
    for p in csv_dir.iterdir():
        if p.is_file() and p.name.lower() == want:
            return p
    raise ResultsError(f"no {prefix}{symbol}.csv in {csv_dir}")


def run_events(events: list, csv_dir: str | None, prefix: str, exchange: str, period: str,
               want_drift: bool, min_gap_pct: float, max_weeks: int, stop_buffer_pct: float) -> tuple[list, list]:
    if csv_dir and not Path(csv_dir).is_dir():
        raise ResultsError(f"--csv-dir {csv_dir} is not a folder")
    cache, results, errors = {}, [], []
    for ev in events:
        label = ev["symbol"]
        try:
            if not ev["timing"]:
                raise ResultsError("timing unknown: add pre-market, market-hours, post-market or HH:MM")
            key = (ev["symbol"], ev["file"])
            if key not in cache:
                if csv_dir:
                    path = find_csv(Path(csv_dir), ev["symbol"], prefix, ev["file"])
                    cache[key] = _usable(ta.load_csv(str(path)))
                else:
                    cache[key] = _usable(ta.fetch_yf(ev["symbol"], exchange, period, "1d"))
            rows = cache[key]
            if not rows:
                raise ResultsError("no usable rows")
            rx = analyse_reaction(rows, ev["date"], ev["timing"], label, ev["clock"])
            item = {"reaction": rx, "score": score_reaction(rx)}
            if want_drift:
                item["drift"] = drift_watch(rows, rx, min_gap_pct, max_weeks, stop_buffer_pct)
            results.append(item)
        except SystemExit as e:
            if not isinstance(e.code, str):
                raise
            errors.append({"symbol": label, "date": ev["date"], "error": e.code})
        except (ValueError, OSError, KeyError, ZeroDivisionError) as e:
            errors.append({"symbol": label, "date": ev["date"], "error": str(e)})
    return results, errors


def scan_rank(results: list) -> list:
    return sorted(results, key=lambda x: (-x["score"]["score"], -x["reaction"]["move_atr"]))


def results_seasons(start: date, end: date) -> list:
    """SEBI LODR Reg 33(3): Q1-Q3 within 45 days of quarter end; Q4/annual within 60 days of FY end."""
    out = []
    for y in range(start.year - 1, end.year + 1):
        for m, d, days in ((3, 31, 60), (6, 30, 45), (9, 30, 45), (12, 31, 45)):
            qe = date(y, m, d)
            deadline = qe + timedelta(days=days)
            if qe < end and deadline >= start:
                fy = y + 1 if m >= 4 else y
                q = {6: 1, 9: 2, 12: 3, 3: 4}[m]
                out.append({"quarter": f"Q{q} FY{fy % 100:02d}", "quarter_end": qe, "deadline": deadline})
    return out


def calendar_check(events: list, start: date, end: date, symbols: list | None = None, near: int = 2) -> dict:
    if end < start:
        raise ResultsError("--to is before --from")
    rows, notes = [], []
    want = set(symbols) if symbols else None
    for ev in events:
        if want is not None and ev["symbol"] not in want:
            continue
        exp = exposure_sessions(ev["date"], ev["timing"])
        inside = any(start <= e <= end for e in exp)
        after = [e for e in exp if e > end]
        status = "inside" if inside else "outside"
        if not inside and after and all(e > end for e in exp):
            n = sessions_after(end, min(after))
            if 1 <= n <= near:
                status = "near"
        item = {"symbol": ev["symbol"], "results_date": ev["date"], "timing": ev["timing"] or "unknown",
                "announced_at": ev["clock"].strftime("%H:%M") if ev["clock"] else None,
                "reaction_sessions": exp, "status": status, "note": ""}
        if status == "inside":
            item["note"] = ("results inside the holding window: gap risk past the stop; for options, IV usually "
                            "collapses after the event. Halve size, exit before, or treat it as an event trade.")
            if not ev["timing"]:
                item["note"] += " Timing unknown, so both the results day and the next session are counted."
        elif status == "near":
            item["note"] = (f"reaction {min(after)} is {sessions_after(end, min(after))} session(s) after your exit "
                            "date: if the exit slips, you carry the results gap.")
            if ev["date"] == end and ev["timing"] == "post-market":
                item["note"] = "results after the close on your exit day: exit by 15:30 or you carry the gap."
        if not is_session(ev["date"]) and status != "outside":
            item["note"] += f" Announced on a non-trading day ({ev['date']:%a}); it reacts on the next session."
        if any(not calendar_verified(e) for e in exp):
            item["note"] = (item["note"] + " " if item["note"] else "") + \
                "Holiday list not verified for this year; check the session."
        rows.append(item)
    rows.sort(key=lambda x: ({"inside": 0, "near": 1, "outside": 2}[x["status"]], x["reaction_sessions"][0]))
    missing = []
    if want:
        have = {r["symbol"] for r in rows}
        seasons = results_seasons(start, end)
        for s in sorted(want - have):
            item = {"symbol": s, "note": "no results date in the events file"}
            if seasons:
                q = seasons[-1]
                item["note"] += (f"; {q['quarter']} results are due by {q['deadline']:%d-%b-%Y} (SEBI LODR), "
                                 "so check the exchange's board-meeting announcements")
            missing.append(item)
    if not calendar_verified(end):
        notes.append(f"NSE holidays are loaded for {HOLIDAY_YEARS[0]}-{HOLIDAY_YEARS[1]} only.")
    inside = [r for r in rows if r["status"] == "inside"]
    return {"from": start, "to": end, "near_sessions": near, "events": rows, "missing": missing,
            "any_inside": bool(inside), "inside_count": len(inside),
            "seasons": results_seasons(start, end), "notes": notes,
            "discipline_flag": "--event yes" if inside else "--event no"}


def load_symbols(spec: str | None, holdings: str | None) -> list | None:
    out = []
    if spec:
        out += [s.strip().upper() for s in spec.split(",") if s.strip()]
    if holdings:
        with open(holdings, newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            cols = {c.strip().lower(): c for c in (reader.fieldnames or []) if c}
            sc = _col(cols, SYMBOL_COLS + ("instrument",))
            if not sc:
                raise ResultsError(f"{holdings}: no symbol/tradingsymbol/instrument column")
            out += [(r.get(sc) or "").strip().upper() for r in reader if (r.get(sc) or "").strip()]
    return sorted(set(out)) if out else None


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def fmt_inr(x, d: int = 2) -> str:
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return "—"
    whole, frac = f"{abs(x):.{d}f}".split(".") if d else (f"{abs(x):.0f}", "")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        whole = ",".join(([head] if head else []) + groups + [tail])
    return ("-" if x < 0 else "") + "₹" + whole + ("." + frac if d else "")


def jsonable(x):
    if isinstance(x, float):
        return round(x, 4) if math.isfinite(x) else None
    if isinstance(x, dict):
        return {str(k): jsonable(v) for k, v in x.items() if not str(k).startswith("_")}
    if isinstance(x, (list, tuple)):
        return [jsonable(v) for v in x]
    if isinstance(x, datetime):
        return x.isoformat()
    if isinstance(x, (date, time)):
        return x.isoformat()
    return x


def dump(obj) -> None:
    print(json.dumps(jsonable(obj), indent=2, ensure_ascii=False, allow_nan=False))


def print_reaction(rx: dict) -> None:
    b = rx["bar"]
    when = rx["timing"] + (f" ({rx['announced_at']} IST)" if rx["announced_at"] else "")
    print(f"\n{rx['name']} — results {rx['results_date']:%d-%b-%Y} {when} → reaction session "
          f"{rx['reaction_date']:%a %d-%b-%Y}")
    print(f"  Pre-results close {fmt_inr(rx['prev_close'])} ({rx['pre_results_date']}) | reaction bar O "
          f"{fmt_inr(b['open'])} H {fmt_inr(b['high'])} L {fmt_inr(b['low'])} C {fmt_inr(b['close'])}")
    print(f"  Gap {rx['gap_pct']:+.2f}% ({rx['gap_atr']:+.2f} ATR) | day {rx['reaction_pct']:+.2f}% "
          f"({rx['move_atr']:+.2f} ATR; ATR14 {fmt_inr(rx['atr14_pre'])} = {rx['atr_pct_pre']:.1f}%) | "
          f"open→close {rx['intraday_pct']:+.2f}%")
    vr = "n/a" if rx["volume_ratio"] is None else f"{rx['volume_ratio']:.2f}x"
    print(f"  Close location {rx['close_location']:.2f} of a {rx['range_pct']:.1f}% range | volume {vr} the 50-day avg")
    p = rx["pre"]
    yes = {True: "above", False: "below", None: "n/a"}
    print(f"  Into results: 20d {_pct(p['ret20_pct'])}, 60d {_pct(p['ret60_pct'])} | close {yes[p['above_sma50']]} "
          f"50DMA, {yes[p['above_sma200']]} 200DMA | {_pct(p['from_52w_high_pct'])} vs 52w high {fmt_inr(p['high_52w'])}")
    if rx["follow_through"]:
        print("  Follow-through (close vs reaction close / vs pre-results close, reaction low held?):")
        for f in rx["follow_through"]:
            print(f"    +{f['sessions']:<2} {f['date']}  {fmt_inr(f['close']):>12}  {f['from_reaction_close_pct']:+6.2f}%  "
                  f"{f['from_pre_close_pct']:+6.2f}%  {'held' if f['held_reaction_low'] else 'LOST'}")
    else:
        print("  Follow-through: no sessions after the reaction yet.")
    g = rx["gap_fill"]
    if g["direction"] == "none":
        print(f"  Gap fill: no meaningful gap (|gap| < {FLAT_GAP_PCT:g}%).")
    elif g["filled"]:
        print(f"  Gap {g['direction']} FILLED back to {fmt_inr(g['level'])} on {g['date']} "
              f"({'same day' if g['sessions'] == 0 else str(g['sessions']) + ' session(s) later'}).")
    else:
        print(f"  Gap {g['direction']} unfilled after {g['sessions_checked']} session(s) (level {fmt_inr(g['level'])}).")
    for n in rx["notes"]:
        print("  ⚠", n)


def print_score(rx: dict, sc: dict) -> None:
    print(f"\n{rx['name']} — results reaction score {sc['score']}/100, grade {sc['grade']} ({sc['bias']})"
          f" | reaction {rx['reaction_date']}")
    print(f"  {'Factor':<32}{'Wt':>4}  {'Pts':>5}  Value  [rule]")
    for f in sc["factors"]:
        pts = "  —  " if f["points"] is None else f"{f['points']:5.1f}"
        print(f"  {f['label']:<32}{f['weight']:>4}  {pts}  {f['value']}  [{f['rule']}]")
    print(f"  Gate {sc['gate']:.2f} (reaction ATR, 0 at <= 0, 1 at >= +{GATE_FULL_ATR:g}) scales volume, trend and run-up.")
    if sc["dropped"]:
        print(f"  Dropped (no data): {', '.join(sc['dropped'])}; total rescaled to 100.")
    print("  Grades: A ≥80, B ≥60, C ≥40, D below.")
    print(f"\n  Read: {sc['read']}")


def print_drift(rx: dict, d: dict) -> None:
    print(f"\n{rx['name']} — post-results drift watch (weekly) | reaction {d['reaction_date']} "
          f"{d['reaction_gain_pct']:+.1f}% | gap-day low {fmt_inr(d['gap_day_low'])}")
    print(f"  Status: {d['status'].upper()} | data to {d['last_date']}, last close {fmt_inr(d['last_close'])}")
    for line in d["plan"]:
        print("  •", line)
    for n in d["notes"]:
        print("  ⚠", n)


def print_scan(results: list, errors: list) -> None:
    print(f"\n{'#':>2} {'Symbol':<16}{'Results':<12}{'Timing':<13}{'Reaction':<12}{'Gap%':>7}{'Day%':>7}"
          f"{'ATRx':>6}{'Vol x':>6}{'CL':>5}{'Score':>6} Gr  Drift")
    for k, it in enumerate(results, 1):
        rx, sc = it["reaction"], it["score"]
        vr = "—" if rx["volume_ratio"] is None else f"{rx['volume_ratio']:.1f}"
        dr = it.get("drift", {}).get("status", "")
        print(f"{k:>2} {rx['name'][:15]:<16}{rx['results_date'].isoformat():<12}{rx['timing']:<13}"
              f"{rx['reaction_date'].isoformat():<12}{rx['gap_pct']:>7.1f}{rx['reaction_pct']:>7.1f}"
              f"{rx['move_atr']:>6.1f}{vr:>6}{rx['close_location']:>5.2f}{sc['score']:>6} {sc['grade']:<3} {dr}")
    for e in errors:
        print(f"   skipped {e['symbol']} ({e['date']}): {e['error']}")


def print_calendar(res: dict) -> None:
    print(f"\nResults inside the holding window {res['from']:%d-%b-%Y} → {res['to']:%d-%b-%Y} "
          f"(plus {res['near_sessions']} session(s) after):")
    shown = [r for r in res["events"] if r["status"] != "outside"]
    if not shown:
        print("  none in the events file.")
    for r in shown:
        sess = " / ".join(f"{d:%a %d-%b}" for d in r["reaction_sessions"])
        at = f" {r['announced_at']}" if r["announced_at"] else ""
        print(f"  [{r['status'].upper():<6}] {r['symbol']:<14} results {r['results_date']:%d-%b-%Y} "
              f"{r['timing']}{at} → reacts {sess}")
        print(f"           {r['note']}")
    for m in res["missing"]:
        print(f"  [?]      {m['symbol']:<14} {m['note']}")
    others = sum(1 for r in res["events"] if r["status"] == "outside")
    if others:
        print(f"  ({others} other event(s) outside the window)")
    for s in res["seasons"]:
        print(f"  Season: {s['quarter']} (quarter ended {s['quarter_end']:%d-%b-%Y}) results due by "
              f"{s['deadline']:%d-%b-%Y}")
    print(f"\n  trading-discipline gate: pass {res['discipline_flag']}"
          + (" for the positions marked INSIDE." if res["any_inside"] else "."))
    for n in res["notes"]:
        print("  ⚠", n)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _today() -> date:
    return datetime.now(IST).date()


def main(argv=None) -> int:
    data = argparse.ArgumentParser(add_help=False)
    g = data.add_argument_group("one stock")
    g.add_argument("--csv", help="daily OHLCV CSV")
    g.add_argument("--candles", help="broker-MCP candle JSON")
    g.add_argument("--symbol", help="label, or fetch via yfinance when no file is given")
    g.add_argument("--date", help="results date: YYYY-MM-DD or 31-Oct-2026 (optionally with HH:MM)")
    g.add_argument("--timing", help="pre-market | market-hours | post-market | HH:MM (IST)")
    g.add_argument("--reaction-date", help="override the reaction session (YYYY-MM-DD)")
    m = data.add_argument_group("many stocks")
    m.add_argument("--events", help="CSV with symbol,date,timing (optional file column)")
    m.add_argument("--csv-dir", help="folder with one OHLCV CSV per symbol")
    m.add_argument("--prefix", default="", help="file-name prefix in --csv-dir, e.g. 'nse_' for nse_TCS.csv")
    data.add_argument("--exchange", default="NSE", choices=["NSE", "BSE"])
    data.add_argument("--period", default="2y", help="yfinance history period")
    data.add_argument("--json", action="store_true")
    dr = argparse.ArgumentParser(add_help=False)
    dr.add_argument("--min-gap-pct", type=float, default=4.0, help="gap/reaction %% that starts a drift watch (4)")
    dr.add_argument("--max-weeks", type=int, default=12, help="weeks to wait for a pullback entry (12)")
    dr.add_argument("--stop-buffer-pct", type=float, default=0.5, help="stop this %% under the pullback low (0.5)")

    p = argparse.ArgumentParser(description="Quarterly-results price reaction toolkit for NSE/BSE stocks")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("reaction", parents=[data], help="measure the results-day reaction")
    sub.add_parser("score", parents=[data], help="0-100 reaction score, grade and read")
    sub.add_parser("drift", parents=[data, dr], help="weekly post-results drift watch")
    s = sub.add_parser("scan", parents=[data, dr], help="score and rank every event in --events")
    s.add_argument("--top", type=int, default=0, help="show only the top N")
    c = sub.add_parser("calendar-check", help="positions with results inside a holding window")
    c.add_argument("--events", required=True, help="CSV with symbol,date[,timing]")
    c.add_argument("--from", dest="start", help="window start (default: today IST)")
    c.add_argument("--to", dest="end", required=True, help="window end (planned exit date)")
    c.add_argument("--symbols", help="only these positions, comma-separated")
    c.add_argument("--holdings", help="holdings CSV with a symbol/tradingsymbol column")
    c.add_argument("--near", type=int, default=2, help="also flag reactions this many sessions after --to (2)")
    c.add_argument("--json", action="store_true")
    a = p.parse_args(argv)

    try:
        return _run(a)
    except SystemExit as e:              # fetch_yf reports missing yfinance / bad symbols this way
        if isinstance(e.code, str):
            print(f"error: {e.code}", file=sys.stderr)
            return 2
        raise
    except (ValueError, OSError, KeyError, ZeroDivisionError, json.JSONDecodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


def _run(a) -> int:
    if a.cmd == "calendar-check":
        if a.near < 0:
            raise ResultsError("--near must be >= 0")
        start = parse_date(a.start, "--from")[0] if a.start else _today()
        end = parse_date(a.end, "--to")[0]
        events, _ = load_events(a.events)
        res = calendar_check(events, start, end, load_symbols(a.symbols, a.holdings), a.near)
        dump(res) if a.json else print_calendar(res)
        return 0

    min_gap = getattr(a, "min_gap_pct", 4.0)
    max_weeks = getattr(a, "max_weeks", 12)
    buf = getattr(a, "stop_buffer_pct", 0.5)
    if min_gap <= 0 or max_weeks < 1 or buf < 0:
        raise ResultsError("--min-gap-pct must be > 0, --max-weeks >= 1 and --stop-buffer-pct >= 0")

    if a.cmd == "scan" or a.events:
        if not a.events:
            raise ResultsError("scan needs --events FILE (symbol,date,timing) and --csv-dir DIR or yfinance")
        events, skipped = load_events(a.events)
        results, errors = run_events(events, a.csv_dir, a.prefix, a.exchange, a.period,
                                     a.cmd in ("drift", "scan"), min_gap, max_weeks, buf)
        if a.cmd == "scan":
            results = scan_rank(results)
            if a.top > 0:
                results = results[:a.top]
        if a.json:
            dump({"results": results, "errors": errors, "skipped_non_results_rows": skipped})
        elif a.cmd == "scan":
            print_scan(results, errors)
        else:
            for it in results:
                {"reaction": lambda: print_reaction(it["reaction"]),
                 "score": lambda: print_score(it["reaction"], it["score"]),
                 "drift": lambda: print_drift(it["reaction"], it["drift"])}[a.cmd]()
            for e in errors:
                print(f"skipped {e['symbol']} ({e['date']}): {e['error']}")
        return 0 if results else 2

    if not a.date:
        raise ResultsError("give --date (results date) and --timing, or --events FILE")
    d, clock = parse_date(a.date, "--date")
    timing, clock = resolve_timing(a.timing, clock, "--date/--timing")
    if not timing:
        raise ResultsError("give --timing pre-market|market-hours|post-market (or the announcement time HH:MM IST)")
    override = parse_date(a.reaction_date, "--reaction-date")[0] if a.reaction_date else None
    rows, name = load_rows(a.csv, a.candles, a.symbol, a.exchange, a.period)
    rx = analyse_reaction(rows, d, timing, name, clock, override)
    if a.cmd == "reaction":
        dump(rx) if a.json else print_reaction(rx)
    elif a.cmd == "score":
        sc = score_reaction(rx)
        dump({"reaction": rx, "score": sc}) if a.json else (print_reaction(rx), print_score(rx, sc))
    else:
        dft = drift_watch(rows, rx, min_gap, max_weeks, buf)
        dump({"reaction": rx, "drift": dft}) if a.json else print_drift(rx, dft)
    return 0


if __name__ == "__main__":
    sys.exit(main())
