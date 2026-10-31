#!/usr/bin/env python3
"""Market-regime reader for Indian indices (NIFTY, BANKNIFTY, SENSEX) or any index/ETF series.

Answers "should I be buying, holding or sitting in cash?" from the index's own tape:
distribution-day clusters, the follow-through-day cycle after a correction, trend and drawdown
context, optional India VIX, rolled into a 0-100 risk score, a regime label and an exposure ceiling.

Zero dependencies (Python 3.10+ stdlib). Optional: `pip install yfinance` for --symbol.
Loaders and moving averages are shared with ../../technical-analysis/scripts/ta.py.

Input (pick one)
  --csv FILE          daily OHLCV CSV of the index (Volume column optional)
  --candles FILE      broker-MCP JSON candles
  --symbol NAME       fetch via yfinance (NIFTY, BANKNIFTY, SENSEX, or an ETF such as NIFTYBEES)

Volume (Yahoo's ^NSEI / ^NSEBANK volume is usually zero or patchy)
  --volume-csv FILE   take volume from another series, matched by date (NIFTYBEES, NIFTY futures)
  --volume-symbol S   same, fetched via yfinance (e.g. NIFTYBEES)
  With --symbol NIFTY/BANKNIFTY and unusable index volume, NIFTYBEES/BANKBEES volume is fetched
  automatically (disable with --no-proxy). With no usable volume the script runs price-only.

Sub-commands
  status         regime label, risk score, exposure ceiling, what would change it (default view)
  distribution   distribution (and optional stalling) days: active, expired, cleared
  ftd            correction / rally-attempt / follow-through state and its event log
  trend          50/200 DMA, 52-week drawdown, sessions since high/low, depth class

Examples
  python3 regime.py status --symbol NIFTY --vix 14.2
  python3 regime.py status --csv nifty.csv --volume-csv niftybees.csv --json
  python3 regime.py distribution --csv nifty.csv --volume-csv niftybees.csv --ignore 2026-02-01
  python3 regime.py ftd --csv banknifty.csv --price-only --ftd-pct 1.5
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import sys
from datetime import date
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

# ETFs whose traded volume stands in for an index that Yahoo reports without volume.
PROXY_VOLUME = {"NIFTY": "NIFTYBEES", "NIFTY50": "NIFTYBEES", "^NSEI": "NIFTYBEES",
                "BANKNIFTY": "BANKBEES", "NIFTYBANK": "BANKBEES", "^NSEBANK": "BANKBEES"}
WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4}

DEFAULTS = {
    "dd_pct": 0.2,            # decline that counts as a distribution day (with higher volume)
    "dd_window": 25,          # a distribution day stays in the count for 25 sessions incl. itself
    "dd_clear_pct": 5.0,      # ...or until a close this far above its close
    "price_only_dd_pct": 0.75,  # stricter decline used when volume is unavailable (~1 NIFTY sigma)
    "stall_max_gain": 0.4,    # stalling: tiny gain on higher volume near the highs, weak close
    "correction_pct": 5.0,    # drop from the cycle peak that starts a correction
    "ftd_min_day": 4,         # earliest rally-attempt day that can be a follow-through
    "ftd_floor": 1.0, "ftd_cap": 2.0, "ftd_sigma_mult": 1.5,   # auto FTD threshold bounds
    "fresh_ftd_sessions": 10,  # an FTD this recent gets starter-size exposure only
    "ftd_watch_sessions": 25,  # after this many sessions an FTD can no longer "fail"
}


# ---------------------------------------------------------------------------
# Data and volume handling
# ---------------------------------------------------------------------------
def num(x, d=2) -> str:
    """Indian digit grouping (12,34,567.89); index levels are points, not rupees."""
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    neg = x < 0
    whole, _, frac = f"{abs(x):.{d}f}".partition(".")
    if len(whole) > 3:
        head, tail, groups = whole[:-3], whole[-3:], []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        whole = ",".join(([head] if head else []) + groups + [tail])
    return ("-" if neg else "") + whole + ("." + frac if frac else "")


def volume_quality(rows: list, window: int = 250) -> dict:
    tail = rows[-window:]
    nonzero = sum(1 for r in tail if r["volume"] and r["volume"] > 0)
    distinct = len({r["volume"] for r in tail if r["volume"] > 0})
    pct = 100.0 * nonzero / len(tail) if tail else 0.0
    # a constant non-zero value is a placeholder some feeds use, not real volume
    return {"nonzero_pct": round(pct, 1), "usable": pct >= 80.0 and distinct > 1}


def attach_volume(rows: list, vol_rows: list) -> list:
    """Replace each bar's volume with the proxy's volume for the same calendar date (0 if absent)."""
    by_day = {r["date"].date(): r["volume"] for r in vol_rows}
    return [{**r, "volume": float(by_day.get(r["date"].date(), 0.0) or 0.0)} for r in rows]


def choose_volume(rows: list, name: str, volume_rows: list | None = None, volume_label: str | None = None,
                  force_price_only: bool = False, fetcher=None) -> tuple:
    """Return (rows, mode, source, notes). mode is 'volume' or 'price-only'."""
    notes = []
    if force_price_only:
        return rows, "price-only", "none (forced)", ["Price-only mode requested: volume conditions skipped."]
    if volume_rows is not None:
        merged = attach_volume(rows, volume_rows)
        q = volume_quality(merged)
        if q["usable"]:
            return merged, "volume", f"proxy {volume_label}", notes
        notes.append(f"Proxy volume from {volume_label} is unusable ({q['nonzero_pct']}% of recent sessions "
                     "have volume, or it never changes); ignoring it.")
    q = volume_quality(rows)
    if q["usable"]:
        return rows, "volume", "own series", notes
    if volume_rows is None:
        notes.append(f"Index volume is missing, zero or constant ({q['nonzero_pct']:.0f}% of recent sessions "
                     "have any); normal for Yahoo's ^NSEI/^NSEBANK.")
        if fetcher is not None:
            proxy = PROXY_VOLUME.get(name.upper().replace(" ", ""))
            if proxy:
                try:
                    merged = attach_volume(rows, fetcher(proxy))
                    if volume_quality(merged)["usable"]:
                        notes.append(f"Using {proxy}.NS ETF volume as the participation proxy.")
                        return merged, "volume", f"proxy {proxy}.NS", notes
                except (SystemExit, Exception) as e:   # network/yfinance failures must not abort
                    notes.append(f"Could not fetch {proxy} volume ({e}).")
    notes.append("PRICE-ONLY MODE: the 'higher volume' tests for distribution and follow-through days were "
                 "skipped and stricter price rules used instead. Pass --volume-csv (NIFTYBEES or NIFTY "
                 "futures) for the full read.")
    return rows, "price-only", "none", notes


def thin_sessions(rows: list, frac: float = 0.4, n: int = 20) -> set:
    """Sessions whose volume is under 40% of the trailing 20-session median: Muhurat trading,
    special Saturday/DR-drill sessions, half-days. Beating such a session's volume proves nothing."""
    out = set()
    for i in range(n, len(rows)):
        w = sorted(r["volume"] for r in rows[i - n:i] if r["volume"] > 0)
        if w and 0 < rows[i]["volume"] < frac * w[len(w) // 2]:
            out.add(i)
    return out


def vol_up(rows: list, i: int, thin: set | None = None):
    """True/False if volume rose versus the prior session; None if either session lacks volume
    or the prior session was abnormally thin."""
    a, b = rows[i - 1]["volume"], rows[i]["volume"]
    if not a or not b or a <= 0 or b <= 0 or (thin and (i - 1) in thin):
        return None
    return b > a


def close_position(r: dict) -> float:
    rng = r["high"] - r["low"]
    return 0.5 if rng <= 0 else (r["close"] - r["low"]) / rng


def auto_ftd_pct(rows: list, lookback: int = 250) -> float:
    """1.5x the daily-return standard deviation, clamped to 1.0-2.0%. NIFTY usually lands near 1.2%."""
    c = [r["close"] for r in rows[-(lookback + 1):]]
    rets = [(c[i] / c[i - 1] - 1) * 100 for i in range(1, len(c))]
    if len(rets) < 20:
        return 1.2
    m = sum(rets) / len(rets)
    sd = math.sqrt(sum((x - m) ** 2 for x in rets) / (len(rets) - 1))
    return round(min(DEFAULTS["ftd_cap"], max(DEFAULTS["ftd_floor"], DEFAULTS["ftd_sigma_mult"] * sd)), 2)


# ---------------------------------------------------------------------------
# Distribution and stalling days
# ---------------------------------------------------------------------------
def distribution_days(rows: list, mode: str = "volume", dd_pct: float = 0.2, window: int = 25,
                      clear_pct: float = 5.0, price_only_pct: float = 0.75, stalling: bool = False,
                      ignore: set | None = None, expiry_weekday: int = 1) -> dict:
    """Scan every session; status is judged as of the last bar."""
    ignore = ignore or set()
    c = [r["close"] for r in rows]
    t = len(rows) - 1
    thin = thin_sessions(rows) if mode == "volume" else set()
    recs, skipped = [], 0
    for i in range(1, len(rows)):
        day = rows[i]["date"].date()
        if day in ignore:
            continue
        chg = (c[i] / c[i - 1] - 1) * 100
        kind = None
        if mode == "price-only":
            if chg <= -price_only_pct and close_position(rows[i]) <= 0.5:
                kind = "distribution"
        else:
            up = vol_up(rows, i, thin)
            if chg <= -dd_pct:
                if up is None:
                    skipped += 1
                elif up:
                    kind = "distribution"
            elif stalling and 0 <= chg <= DEFAULTS["stall_max_gain"] and up and close_position(rows[i]) < 0.5 \
                    and c[i] >= 0.98 * max(c[max(0, i - 25):i + 1]):
                kind = "stalling"
        if not kind:
            continue
        clear_level = c[i] * (1 + clear_pct / 100)
        cleared = next((j for j in range(i + 1, t + 1) if c[j] >= clear_level), None)
        age = t - i
        if cleared is not None:
            status = "cleared"
        elif age >= window:
            status = "expired"
        else:
            status = "active"
        tags = []
        if rows[i]["date"].weekday() == expiry_weekday:
            tags.append("expiry day")
        if abs(chg) >= 10:
            tags.append("circuit-breaker move")
        recs.append({"date": day.isoformat(), "kind": kind, "change_pct": round(chg, 2),
                     "close": round(c[i], 2), "age_sessions": age, "status": status,
                     "sessions_left": max(0, window - age) if status == "active" else 0,
                     "clear_level": round(clear_level, 2),
                     "cleared_on": rows[cleared]["date"].date().isoformat() if cleared is not None else None,
                     "tags": tags})
    active = [r for r in recs if r["status"] == "active"]
    weight = lambda rs: sum(1.0 if r["kind"] == "distribution" else 0.5 for r in rs)
    counts = {f"d{n}": weight([r for r in active if r["age_sessions"] < n]) for n in (5, 15, window)}
    counts = {k: (int(v) if float(v).is_integer() else v) for k, v in counts.items()}
    return {"counts": counts, "window": window, "clear_pct": clear_pct, "active": active,
            "recent": [r for r in recs if r["age_sessions"] < 3 * window],
            "skipped_no_volume": skipped, "mode": mode}


# ---------------------------------------------------------------------------
# Correction -> rally attempt -> follow-through state machine
# ---------------------------------------------------------------------------
def follow_through(rows: list, mode: str = "volume", ftd_pct: float = 1.2, min_day: int = 4,
                   correction_pct: float = 5.0, ignore: set | None = None) -> dict:
    """Walk the series once. States: uptrend, correction, rally_attempt, follow_through."""
    ignore = ignore or set()
    c = [r["close"] for r in rows]
    lo = [r["low"] for r in rows]
    ds = lambda i: rows[i]["date"].date().isoformat()
    thin = thin_sessions(rows) if mode == "volume" else set()
    state, peak_i, low_i, day1_i, ftd_i = "uptrend", 0, None, None, None
    events = []
    for i in range(1, len(rows)):
        chg = (c[i] / c[i - 1] - 1) * 100
        if state == "follow_through":
            if c[i] < lo[low_i]:
                events.append({"date": ds(i), "event": "follow-through failed",
                               "detail": f"closed {num(c[i])} below rally low {num(lo[low_i])}"})
                state, low_i = "correction", i
                continue
            if i - ftd_i > DEFAULTS["ftd_watch_sessions"]:
                state = "uptrend"
        if state == "uptrend" or state == "follow_through":
            if c[i] > c[peak_i]:
                peak_i = i
            if c[i] <= c[peak_i] * (1 - correction_pct / 100):
                state, low_i = "correction", i
                events.append({"date": ds(i), "event": "correction",
                               "detail": f"{(c[i] / c[peak_i] - 1) * 100:.1f}% below {num(c[peak_i])} "
                                         f"({ds(peak_i)})"})
            continue
        if c[i] > c[peak_i]:      # V-shaped recovery to a new cycle high without a formal FTD
            events.append({"date": ds(i), "event": "recovered", "detail": "new cycle high without follow-through"})
            state, peak_i = "uptrend", i
            continue
        if state == "correction":
            if lo[i] < lo[low_i]:
                low_i = i
            if chg > 0:
                state, day1_i = "rally_attempt", i
                events.append({"date": ds(i), "event": "rally attempt day 1",
                               "detail": f"low {num(lo[low_i])} on {ds(low_i)}"})
            continue
        # rally_attempt
        if lo[i] < lo[low_i]:
            events.append({"date": ds(i), "event": "rally attempt failed",
                           "detail": f"low {num(lo[i])} undercut {num(lo[low_i])}"})
            state, low_i = "correction", i
            continue
        day = i - day1_i + 1
        if day < min_day or chg < ftd_pct or rows[i]["date"].date() in ignore:
            continue
        if mode == "price-only":
            ok, how = close_position(rows[i]) >= 0.5, "volume not checked (price-only)"
        else:
            up = vol_up(rows, i, thin)
            ok, how = up is True, "on higher volume"
        if ok:
            state, ftd_i, peak_i = "follow_through", i, i
            events.append({"date": ds(i), "event": "follow-through day",
                           "detail": f"day {day}, +{chg:.2f}% {how}"})
    t = len(rows) - 1
    cur = {"state": state, "ftd_pct": ftd_pct, "min_day": min_day, "correction_pct": correction_pct,
           "cycle_peak": round(c[peak_i], 2), "cycle_peak_date": ds(peak_i),
           "correction_trigger": round(c[peak_i] * (1 - correction_pct / 100), 2)}
    if low_i is not None and state != "uptrend":
        cur.update(low=round(lo[low_i], 2), low_date=ds(low_i))
    if state == "rally_attempt":
        day = t - day1_i + 1
        cur.update(rally_day=day, day1_date=ds(day1_i),
                   sessions_until_window=max(0, min_day - day - 1),
                   ftd_close_needed=round(c[t] * (1 + ftd_pct / 100), 2))
    if state == "follow_through":
        cur.update(ftd_date=ds(ftd_i), ftd_close=round(c[ftd_i], 2), sessions_since_ftd=t - ftd_i)
    return {"current": cur, "events": events}


# ---------------------------------------------------------------------------
# Trend and drawdown context
# ---------------------------------------------------------------------------
def depth_class(dd_pct: float) -> str:
    d = -dd_pct
    if d < 3:
        return "at or near highs"
    if d < 5:
        return "pullback (3-5%)"
    if d < 10:
        return "correction (5-10%)"
    if d < 20:
        return "deep correction (10-20%)"
    return "bear market (20%+)"


def trend_context(rows: list) -> dict:
    c = [r["close"] for r in rows]
    s50, s200 = ta.sma(c, 50), ta.sma(c, 200)
    t = len(rows) - 1
    yr = min(len(rows), 252)
    start = len(rows) - yr
    hi_i = max(range(start, t + 1), key=lambda i: rows[i]["high"])
    lo_i = min(range(start, t + 1), key=lambda i: rows[i]["low"])
    pct = lambda a, b: None if a is None or b is None else round((a / b - 1) * 100, 2)
    slope = None
    if s50[t] is not None and t >= 10 and s50[t - 10] is not None:
        slope = "rising" if s50[t] > s50[t - 10] else "falling"
    dd = pct(c[t], rows[hi_i]["high"])
    rnd = lambda x: None if x is None else round(x, 2)
    return {"close": round(c[t], 2), "sma50": rnd(s50[t]), "sma200": rnd(s200[t]),
            "vs_sma50_pct": pct(c[t], s50[t]), "vs_sma200_pct": pct(c[t], s200[t]), "sma50_slope": slope,
            "golden_alignment": None if s50[t] is None or s200[t] is None else s50[t] > s200[t],
            "range_label": "52w" if yr >= 252 else f"{yr}-session",
            "high_52w": round(rows[hi_i]["high"], 2), "high_date": rows[hi_i]["date"].date().isoformat(),
            "low_52w": round(rows[lo_i]["low"], 2), "low_date": rows[lo_i]["date"].date().isoformat(),
            "drawdown_pct": dd, "above_low_pct": pct(c[t], rows[lo_i]["low"]),
            "sessions_since_high": t - hi_i, "sessions_since_low": t - lo_i, "depth": depth_class(dd)}


# ---------------------------------------------------------------------------
# Score, label, exposure
# ---------------------------------------------------------------------------
def vix_points(vix: float) -> int:
    for lim, pts in ((13, 0), (17, 4), (22, 9), (30, 13)):
        if vix < lim:
            return pts
    return 15


def risk_score(dist: dict, trend: dict, ftd: dict, vix: float | None = None) -> dict:
    k = dist["counts"]
    parts = {"distribution": min(35, round(5 * k[f"d{dist['window']}"] + (5 if k["d5"] >= 2 else 0)))}
    tp = 0
    if trend["vs_sma50_pct"] is not None and trend["vs_sma50_pct"] < 0:
        tp += 10
    if trend["vs_sma200_pct"] is not None and trend["vs_sma200_pct"] < 0:
        tp += 12
    if trend["sma50_slope"] == "falling":
        tp += 4
    if trend["golden_alignment"] is False:
        tp += 4
    parts["trend"] = tp
    d = -(trend["drawdown_pct"] or 0)
    parts["drawdown"] = 0 if d < 3 else 5 if d < 5 else 10 if d < 10 else 15 if d < 20 else 20
    maxes = {"distribution": 35, "trend": 30, "drawdown": 20}
    if vix is not None:
        parts["vix"], maxes["vix"] = vix_points(vix), 15
    raw = sum(parts.values())
    score = raw / sum(maxes.values()) * 100
    cur = ftd["current"]
    if cur["state"] == "follow_through" and cur["sessions_since_ftd"] <= DEFAULTS["ftd_watch_sessions"]:
        score -= 10
        parts["fresh_follow_through"] = -10
    return {"score": int(round(max(0, min(100, score)))), "components": parts, "max": maxes}


def regime(dist: dict, trend: dict, ftd: dict, score: int, vix: float | None = None) -> dict:
    cur, k = ftd["current"], dist["counts"]
    w = dist["window"]
    state = cur["state"]
    below50 = trend["vs_sma50_pct"] is not None and trend["vs_sma50_pct"] < 0
    pressure = k[f"d{w}"] >= 5 or k["d15"] >= 4 or below50 or score >= 50
    fresh = state == "follow_through" and cur["sessions_since_ftd"] <= DEFAULTS["fresh_ftd_sessions"]
    if state == "correction":
        label = "Correction"
        deep = -(trend["drawdown_pct"] or 0) >= 10
        ceiling, new = (10, 0) if deep else (20, 0)
        stance = "Protect capital: no new swing longs, keep stops tight, build a watchlist of relative-strength leaders."
    elif state == "rally_attempt":
        label = "Rally attempt"
        ceiling, new = 25, 1
        stance = "Wait for the follow-through. At most one pilot position at half size in a leader."
    elif fresh:
        # A follow-through is the confirmation itself; the index is usually still under its 50 DMA,
        # so the pressure tests are not applied yet. Many follow-throughs fail: size up gradually.
        if score < 60:
            label, ceiling, new = "Confirmed uptrend", 50, 3
            stance = "Fresh follow-through: start with starter positions and add only as they work."
        else:
            label, ceiling, new = "Uptrend under pressure", 30, 2
            stance = ("Follow-through fired, but the damage (trend, drawdown, distribution) is heavy: "
                      "pilot positions only until the index reclaims its 50 DMA.")
    elif pressure:
        label = "Uptrend under pressure"
        ceiling, new = (60, 2) if score < 50 else (40, 1)
        stance = "Hold winners with trailed stops; be selective with new buys and book weak positions."
    else:
        label = "Confirmed uptrend"
        ceiling, new = (100, 5) if score < 25 else (80, 4)
        stance = "Normal risk: take valid breakouts and pullback entries with full position sizes."
    if vix is not None and vix >= 22 and ceiling > 50:
        ceiling, new = 50, min(new, 2)
        stance += " India VIX is elevated: half-size entries and expect wider swings."
    return {"label": label, "exposure_ceiling_pct": ceiling, "max_new_positions": new, "stance": stance,
            "fresh_follow_through": fresh}


def what_changes(reg: dict, dist: dict, trend: dict, ftd: dict) -> list:
    cur, k, w = ftd["current"], dist["counts"], dist["window"]
    label, out = reg["label"], []
    if label in ("Confirmed uptrend", "Uptrend under pressure"):
        if k[f"d{w}"] < 5:
            n = 5 - k[f"d{w}"]
            out.append(f"{n:g} more distribution day(s) inside {w} sessions → Uptrend under pressure")
        else:
            oldest = min(dist["active"], key=lambda r: r["sessions_left"])
            out.append(f"Count eases as the {oldest['date']} distribution day drops out in "
                       f"{oldest['sessions_left']} session(s), or on any close ≥ {num(oldest['clear_level'])}")
        if trend["sma50"] is not None:
            side = "below" if (trend["vs_sma50_pct"] or 0) >= 0 else "back above"
            out.append(f"A close {side} the 50 DMA ({num(trend['sma50'])}) changes the pressure read")
        out.append(f"A close ≤ {num(cur['correction_trigger'])} ({cur['correction_pct']:g}% off the "
                   f"{num(cur['cycle_peak'])} cycle peak) → Correction")
        if cur["state"] == "follow_through" and "low" in cur:
            out.append(f"A close below the rally low {num(cur['low'])} fails the follow-through → Correction")
    elif label == "Correction":
        out.append("First up close → Rally attempt day 1")
        out.append(f"From day {cur['min_day']}: a gain ≥ {cur['ftd_pct']:g}% "
                   + ("on higher volume" if dist["mode"] == "volume" else "(volume unchecked)")
                   + " → follow-through → Confirmed uptrend")
        if "low" in cur:
            out.append(f"Lows under {num(cur['low'])} extend the correction")
    elif label == "Rally attempt":
        wait = cur["sessions_until_window"]
        when = "next session" if wait == 0 else f"in {wait} session(s)"
        out.append(f"Follow-through window opens {when}: close ≥ {num(cur['ftd_close_needed'])} "
                   f"(+{cur['ftd_pct']:g}% from today) "
                   + ("on higher volume" if dist["mode"] == "volume" else "(volume unchecked)"))
        out.append(f"Undercutting the rally low {num(cur['low'])} → back to Correction")
    return out


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def analyse(rows: list, name: str, mode: str = "volume", volume_source: str = "own series",
            notes: list | None = None, vix: float | None = None, ftd_pct: float | None = None,
            dd_pct: float = DEFAULTS["dd_pct"], window: int = DEFAULTS["dd_window"],
            clear_pct: float = DEFAULTS["dd_clear_pct"], price_only_pct: float = DEFAULTS["price_only_dd_pct"],
            correction_pct: float = DEFAULTS["correction_pct"], min_day: int = DEFAULTS["ftd_min_day"],
            stalling: bool = False, ignore: set | None = None, expiry_weekday: int = 1) -> dict:
    if len(rows) < 30:
        raise ValueError(f"only {len(rows)} sessions; need at least 30 (250+ for the 52-week and 200 DMA read)")
    notes = list(notes or [])
    auto = ftd_pct is None
    ftd_pct = auto_ftd_pct(rows) if auto else ftd_pct
    dist = distribution_days(rows, mode, dd_pct, window, clear_pct, price_only_pct, stalling, ignore,
                             expiry_weekday)
    ftd = follow_through(rows, mode, ftd_pct, min_day, correction_pct, ignore)
    trend = trend_context(rows)
    sc = risk_score(dist, trend, ftd, vix)
    reg = regime(dist, trend, ftd, sc["score"], vix)
    if len(rows) < 200:
        notes.append(f"Only {len(rows)} sessions: 200 DMA unavailable; trend score is partial.")
    if dist["skipped_no_volume"]:
        notes.append(f"{dist['skipped_no_volume']} down session(s) could not be judged: missing volume, or the "
                     "prior session was abnormally thin (Muhurat, special session).")
    if any(r["tags"] and "expiry day" in r["tags"] for r in dist["active"]):
        notes.append("Some active distribution days fell on expiry day: weigh them lighter if volume came "
                     "from futures or the ETF arbitrage desk.")
    stale = ta.freshness(rows)
    if stale:
        notes.append(stale.lstrip("⚠ "))
    return {"name": name, "as_of": rows[-1]["date"].date().isoformat(), "sessions": len(rows),
            "volume_mode": mode, "volume_source": volume_source,
            "params": {"dd_pct": dd_pct if mode == "volume" else price_only_pct, "dd_window": window,
                       "dd_clear_pct": clear_pct, "ftd_pct": ftd_pct, "ftd_pct_auto": auto,
                       "ftd_min_day": min_day, "correction_pct": correction_pct, "stalling": stalling},
            "regime": reg, "risk": sc, "distribution": dist, "follow_through": ftd, "trend": trend,
            "vix": vix, "what_would_change": what_changes(reg, dist, trend, ftd), "notes": notes}


# ---------------------------------------------------------------------------
# Printing
# ---------------------------------------------------------------------------
def _header(res: dict) -> None:
    print(f"\n{res['name']} — market regime as of {res['as_of']} (daily close, IST) | {res['sessions']} sessions")
    vm = "price-only (volume tests skipped)" if res["volume_mode"] == "price-only" else res["volume_source"]
    print(f"Volume: {vm}")


def print_trend(tr: dict) -> None:
    sig = lambda v: "—" if v is None else f"{v:+.2f}%"
    print(f"Trend: close {num(tr['close'])} | 50 DMA {num(tr['sma50'])} ({sig(tr['vs_sma50_pct'])}"
          + (f", {tr['sma50_slope']}" if tr["sma50_slope"] else "") + f") | 200 DMA {num(tr['sma200'])} "
          f"({sig(tr['vs_sma200_pct'])})")
    print(f"{tr['range_label']} high {num(tr['high_52w'])} on {tr['high_date']} ({sig(tr['drawdown_pct'])}, "
          f"{tr['sessions_since_high']} sessions ago) | low {num(tr['low_52w'])} on {tr['low_date']} "
          f"({tr['sessions_since_low']} sessions ago) → {tr['depth']}")


def print_distribution(dist: dict, full: bool = False) -> None:
    k = dist["counts"]
    label = "Distribution" if dist["mode"] == "volume" else "Heavy-selling days (price-only proxy)"
    wk = f"d{dist['window']}"
    print(f"{label}: d5 {k['d5']} | d15 {k['d15']} | {wk} {k[wk]}")
    rows = dist["recent"] if full else dist["active"]
    for r in sorted(rows, key=lambda r: r["date"]):
        extra = (f"drops out in {r['sessions_left']}, clears ≥ {num(r['clear_level'])}" if r["status"] == "active"
                 else f"{dist['clear_pct']:g}% rally on {r['cleared_on']}" if r["status"] == "cleared"
                 else f"aged out after {dist['window']} sessions")
        tag = f" [{', '.join(r['tags'])}]" if r["tags"] else ""
        kind = "" if r["kind"] == "distribution" else " (stalling)"
        print(f"  {r['date']}{kind} {r['change_pct']:+.2f}% close {num(r['close'])} — {r['status']}: {extra}{tag}")


def print_ftd(ftd: dict, events: int = 0) -> None:
    cur = ftd["current"]
    s = cur["state"]
    if s == "uptrend":
        line = (f"no active correction (cycle peak {num(cur['cycle_peak'])} on {cur['cycle_peak_date']}; "
                f"correction below {num(cur['correction_trigger'])})")
    elif s == "correction":
        line = f"correction — low {num(cur['low'])} on {cur['low_date']}; waiting for a rally attempt"
    elif s == "rally_attempt":
        line = (f"rally attempt day {cur['rally_day']} (day 1 {cur['day1_date']}, low {num(cur['low'])}); "
                f"FTD needs close ≥ {num(cur['ftd_close_needed'])}")
    else:
        line = (f"follow-through on {cur['ftd_date']} ({cur['sessions_since_ftd']} session(s) ago); "
                f"fails below {num(cur.get('low'))}")
    print(f"Follow-through ({cur['ftd_pct']:g}% threshold): {line}")
    for e in ftd["events"][-events:] if events else []:
        print(f"  {e['date']}  {e['event']:<24} {e['detail']}")


def print_status(res: dict) -> None:
    _header(res)
    reg, sc = res["regime"], res["risk"]
    print(f"\nRegime: {reg['label'].upper()}   risk score {sc['score']}/100")
    print(f"Exposure ceiling: {reg['exposure_ceiling_pct']}% of trading capital | "
          f"max new positions: {reg['max_new_positions']}")
    print(f"Stance: {reg['stance']}\n")
    print_trend(res["trend"])
    print_distribution(res["distribution"])
    print_ftd(res["follow_through"])
    if res["vix"] is not None:
        print(f"India VIX: {res['vix']:g} (+{sc['components']['vix']} risk points)")
    print("Score parts: " + " | ".join(f"{k} {v}" + (f"/{sc['max'][k]}" if k in sc["max"] else "")
                                       for k, v in sc["components"].items()))
    print("\nWhat would change the label:")
    for w in res["what_would_change"]:
        print("  •", w)
    for n in res["notes"]:
        print("⚠", n)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _dates(spec: str | None) -> set:
    out = set()
    for part in (spec or "").split(","):
        part = part.strip()
        if part:
            try:
                out.add(date.fromisoformat(part))
            except ValueError:
                raise ValueError(f"bad date {part!r} in --ignore (use YYYY-MM-DD)")
    return out


def _fetch(symbol: str, exchange: str, period: str) -> list:
    return ta.fetch_yf(symbol, exchange, period, "1d")


def main(argv=None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    g = common.add_argument_group("input")
    g.add_argument("--csv")
    g.add_argument("--candles")
    g.add_argument("--symbol", help="NIFTY, BANKNIFTY, SENSEX or any yfinance symbol (needs yfinance)")
    g.add_argument("--name", help="label for the report")
    g.add_argument("--exchange", default="NSE")
    g.add_argument("--period", default="2y")
    g.add_argument("--as-of", help="evaluate as of this date (YYYY-MM-DD), ignoring later bars")
    v = common.add_argument_group("volume")
    v.add_argument("--volume-csv", help="CSV whose Volume column replaces the index volume (matched by date)")
    v.add_argument("--volume-symbol", help="yfinance symbol to borrow volume from, e.g. NIFTYBEES")
    v.add_argument("--no-proxy", action="store_true", help="never auto-fetch ETF volume for --symbol")
    v.add_argument("--price-only", action="store_true", help="ignore volume entirely")
    r = common.add_argument_group("rules")
    r.add_argument("--dd-pct", type=float, default=DEFAULTS["dd_pct"], help="distribution-day decline %% (0.2)")
    r.add_argument("--dd-window", type=int, default=DEFAULTS["dd_window"], help="sessions a DD counts (25)")
    r.add_argument("--dd-clear-pct", type=float, default=DEFAULTS["dd_clear_pct"],
                   help="close this %% above a DD's close removes it (5)")
    r.add_argument("--price-only-dd-pct", type=float, default=DEFAULTS["price_only_dd_pct"])
    r.add_argument("--ftd-pct", type=float, help="follow-through gain %% (default: auto, 1.5x daily sigma, 1-2%%)")
    r.add_argument("--ftd-min-day", type=int, default=DEFAULTS["ftd_min_day"])
    r.add_argument("--correction-pct", type=float, default=DEFAULTS["correction_pct"])
    r.add_argument("--stalling", action="store_true", help="also count stalling days (half weight)")
    r.add_argument("--ignore", help="comma-separated YYYY-MM-DD sessions to skip (Budget, MSCI rebalance)")
    r.add_argument("--expiry-day", choices=list(WEEKDAYS), help="weekly-expiry weekday to tag (tue NIFTY, thu SENSEX)")
    r.add_argument("--vix", type=float, help="current India VIX level")
    common.add_argument("--json", action="store_true")

    p = argparse.ArgumentParser(description="Market regime for Indian indices")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", parents=[common], help="label, score, exposure ceiling")
    d = sub.add_parser("distribution", parents=[common], help="distribution/stalling day detail")
    d.add_argument("--all", action="store_true", help="also show expired and cleared days")
    f = sub.add_parser("ftd", parents=[common], help="follow-through state machine")
    f.add_argument("--events", type=int, default=12, help="how many recent events to list")
    sub.add_parser("trend", parents=[common], help="DMA, drawdown and duration context")
    a = p.parse_args(argv)

    try:
        for nm, val in (("--dd-pct", a.dd_pct), ("--dd-clear-pct", a.dd_clear_pct),
                        ("--correction-pct", a.correction_pct), ("--price-only-dd-pct", a.price_only_dd_pct)):
            if val <= 0:
                raise ValueError(f"{nm} must be positive")
        if a.ftd_pct is not None and a.ftd_pct <= 0:
            raise ValueError("--ftd-pct must be positive")
        if a.dd_window < 5 or a.ftd_min_day < 2:
            raise ValueError("--dd-window must be ≥ 5 and --ftd-min-day ≥ 2")
        if a.vix is not None and not 0 < a.vix < 150:
            raise ValueError("--vix should be the India VIX level, e.g. 14.5")
        ignore = _dates(a.ignore)

        fetcher = None
        if a.csv:
            rows, name = ta.load_csv(a.csv), a.name or Path(a.csv).stem
        elif a.candles:
            rows, name = ta.load_candles_json(a.candles), a.name or Path(a.candles).stem
        elif a.symbol:
            rows, name = _fetch(a.symbol, a.exchange, a.period), a.name or a.symbol.upper()
            if not a.no_proxy:
                fetcher = lambda s: _fetch(s, "NSE", a.period)
        else:
            raise ValueError("give --csv, --candles or --symbol")
        if a.as_of:
            try:
                cutoff = date.fromisoformat(a.as_of)
            except ValueError:
                raise ValueError(f"bad --as-of {a.as_of!r} (use YYYY-MM-DD)")
            rows = [x for x in rows if x["date"].date() <= cutoff]
        if not rows:
            raise ValueError("no usable OHLC rows in the input")

        vol_rows, vol_label = None, None
        if a.volume_csv:
            vol_rows, vol_label = ta.load_csv(a.volume_csv), Path(a.volume_csv).name
        elif a.volume_symbol:
            vol_rows, vol_label = _fetch(a.volume_symbol, a.exchange, a.period), ta.yf_ticker(a.volume_symbol)
        rows, mode, source, notes = choose_volume(rows, a.symbol or name, vol_rows, vol_label, a.price_only,
                                                  fetcher)

        if a.expiry_day:
            wd = WEEKDAYS[a.expiry_day]
        else:
            wd = WEEKDAYS["thu"] if any(x in name.upper() for x in ("SENSEX", "BANKEX", "BSESN")) else WEEKDAYS["tue"]
        res = analyse(rows, name, mode, source, notes, a.vix, a.ftd_pct, a.dd_pct, a.dd_window, a.dd_clear_pct,
                      a.price_only_dd_pct, a.correction_pct, a.ftd_min_day, a.stalling, ignore, wd)
    except (ValueError, OSError, KeyError, json.JSONDecodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except SystemExit as e:          # fetch_yf reports missing yfinance / bad symbols this way
        if isinstance(e.code, str):
            print(f"error: {e.code}", file=sys.stderr)
            return 2
        raise

    if a.json:
        view = {"status": res, "distribution": {k: res[k] for k in ("name", "as_of", "volume_mode", "volume_source",
                                                                      "params", "distribution", "notes")},
                "ftd": {k: res[k] for k in ("name", "as_of", "volume_mode", "volume_source", "params",
                                            "follow_through", "notes")},
                "trend": {k: res[k] for k in ("name", "as_of", "trend", "notes")}}[a.cmd]
        print(json.dumps(view, indent=2, default=str, ensure_ascii=False))
        return 0
    if a.cmd == "status":
        print_status(res)
    elif a.cmd == "distribution":
        _header(res)
        print_distribution(res["distribution"], full=a.all)
        for n in res["notes"]:
            print("⚠", n)
    elif a.cmd == "ftd":
        _header(res)
        print_ftd(res["follow_through"], a.events)
        for n in res["notes"]:
            print("⚠", n)
    else:
        _header(res)
        print_trend(res["trend"])
        for n in res["notes"]:
            print("⚠", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
