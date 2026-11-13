#!/usr/bin/env python3
"""Exhaustion and reversal reads for NSE/BSE stocks: when a move has run out of buyers or sellers.

The other side of momentum-scanner. That skill finds stocks starting to move; this one asks whether a
move that is already old and steep is ending, and what an Indian retail trader can actually do about it.

  climax    0-100 parabolic score for an up-move (stretch above the 20/50/200 DMA, acceleration, length
            and size of the run, a range/volume blow-off or an upper-circuit streak, a price-band cut),
            a stage (extended -> climax -> reversal confirmed) and the levels that confirm a top
  plan      for one stock: a holder's scale-out and tightened GTT trail (the usual case), or a short
            that is only offered through stock futures / puts on F&O names, after a trigger, in lots
  washout   the mirror for long entries: a selling-exhaustion bar (undercut of a prior low, reclaim,
            long lower wick, strong close, volume) after a real pullback in a stock that was trending
  reaction  did price respond to news? confirmed / failed / inconclusive over 1-3 sessions, with the
            rule that fired and an optional crowding gate
  scan      climax + washout across a universe, liquidity-filtered, with F&O tags for the short route

Zero dependencies (Python 3.10+ stdlib). Optional: `pip install yfinance` for --symbols.
Re-uses momentum-scanner/momentum.py (universe loader, circuit and band detection, JSON output),
breakout-planner/breakout.py (NSE ticks, ₹-crore liquidity), technical-analysis/ta.py (EMA, ATR) and
position-sizing/risk.py (lot sizing).

Examples
  python3 exhaustion.py climax --csv data/KAYNES.csv
  python3 exhaustion.py plan --csv data/KAYNES.csv --qty 120 --avg-price 4100
  python3 exhaustion.py plan --csv data/KAYNES.csv --side short --fno --lot 100 --margin-per-lot 210000 \
      --capital 1500000 --risk-pct 1
  python3 exhaustion.py washout --csv-dir data/ --days 3 --show-rejects
  python3 exhaustion.py reaction --csv data/INFY.csv --date 2026-10-15 --news bullish --crowding long
  python3 exhaustion.py scan --csv-dir data/ --fno-list fo_mktlots.csv --bands bands.csv --json
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import math
import re
import sys
from datetime import datetime
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parents[2]


def _sibling(skill: str, script: str):
    name = f"_exhaustion_reversal_dep_{script}"
    if name in sys.modules:
        return sys.modules[name]
    path = SKILLS_DIR / skill / "scripts" / f"{script}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


ms = _sibling("momentum-scanner", "momentum")
ta, bp = ms.ta, ms.bp                      # the copies momentum.py already loaded
risk = bp.risk

EXTENDED, CLIMAX = 40, 65                  # stage thresholds on the 0-100 score
WEIGHTS = {"extension": 25, "acceleration": 15, "run": 25, "climax": 30, "band": 5}
FUT_COST_PCT = 0.10                        # futures round trip incl. STT, charges and a little slippage
SYM_RE = re.compile(r"[A-Z0-9&_-]*[A-Z][A-Z0-9&_-]*")
SELL_PCT = {"not extended": 0, "extended": 25, "climax": 40, "reversal confirmed": 50}
SHORT_WARNINGS = (
    "A short future has no maximum loss. A parabolic stock can add another 20-30% before it turns, and "
    "F&O stocks have no fixed circuit to stop it.",
    "Squeeze risk: late shorts covering, a stock near its MWPL (ban above 95%) and expiry-week rollover "
    "all push price up fastest right at the top. That is why the entry waits for a trigger.",
    "A stop is an order, not a guarantee: a gap up through it fills at the open. Futures are marked to "
    "market every day, so the loss leaves your account in cash before you decide anything.",
    "Stock futures are physically settled. A short carried into expiry must deliver shares; brokers "
    "raise margin in the last week. Close or roll before that.",
    "If the stock enters the F&O ban you can only reduce: no adding, no fresh roll.",
)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def ramp(x, lo: float, hi: float) -> float:
    """0 at lo, 1 at hi, linear in between. None counts as 0."""
    if x is None:
        return 0.0
    return max(0.0, min(1.0, (x - lo) / (hi - lo)))


def _r(x, d=2):
    return ms._r(x, d)


def _iso(d) -> str:
    return ms._iso(d)


def _pct(a, b):
    return (a / b - 1) * 100 if a is not None and b else None


def _tick(cfg, price):
    return getattr(cfg, "tick", None) or bp.tick_for(price)


def prep(st: dict) -> dict:
    c, h, l = st["c"], st["h"], st["l"]
    st["sma20"], st["ema10"], st["ema20"] = ta.sma(c, 20), ta.ema(c, 10), ta.ema(c, 20)
    st["atr"], st["tr"] = ta.atr(h, l, c, 14), ta.true_range(h, l, c)
    st["has_vol"] = any(x > 0 for x in st["v"])
    return st


def atr_before(st: dict, i: int):
    """ATR as it stood before bar i, so a blow-off bar is not measured against itself."""
    for j in (i - 1, i):
        if 0 <= j < len(st["atr"]) and st["atr"][j]:
            return st["atr"][j]
    w = st["tr"][max(0, i - 14):i]
    return sum(w) / len(w) if w and sum(w) > 0 else None


def vol_ratio(st: dict, i: int):
    """Volume of bar i against the 50 sessions before it (at least 10 if history is short)."""
    avg = st["vavg50"][i]
    if avg is None:
        w = st["v"][max(0, i - 50):i]
        avg = sum(w) / len(w) if len(w) >= 10 else None
    return st["v"][i] / avg if avg and avg > 0 and st["v"][i] > 0 else None


def close_pos(st: dict, i: int):
    rng = st["h"][i] - st["l"][i]
    return None if rng <= 0 else (st["c"][i] - st["l"][i]) / rng * 100


def load_symbol_list(path: str, flag: str) -> dict:
    """{SYMBOL: lot or None} from a one-per-line list, a SYMBOL[,LOT] CSV, NSE fo_mktlots.csv or fo_secban.csv."""
    p = Path(path)
    if not p.is_file():
        raise ValueError(f"{flag} {path}: file not found")
    rows = [[c.strip() for c in r] for r in csv.reader(p.read_text(encoding="utf-8-sig").splitlines())]
    header = next((r for r in rows if any(c.upper() == "SYMBOL" for c in r)), None)
    si = [c.upper() for c in header].index("SYMBOL") if header else None
    out = {}
    for r in rows:
        k = si if si is not None else next((i for i, c in enumerate(r) if SYM_RE.fullmatch(c.upper())), None)
        if k is None or k >= len(r):
            continue
        sym = r[k].upper()
        if sym == "SYMBOL" or not SYM_RE.fullmatch(sym):
            continue
        lot = next((int(float(x)) for x in r[k + 1:] if re.fullmatch(r"\d+(\.0+)?", x)), None)
        out[ms._norm_sym(sym)] = lot
    if not out:
        raise ValueError(f"{flag} {path}: no symbols found")
    return out


# ---------------------------------------------------------------------------
# India: limit moves, circuit streaks, band cuts
# ---------------------------------------------------------------------------
def limit_close(st: dict, i: int):
    """('up'|'down', band %) when bar i closed at its extreme on a band-sized move (2/5/10/20%)."""
    prev, c = st["c"][i - 1], st["c"][i]
    half = bp.tick_for(prev) / 2 + 1e-9
    move = (c / prev - 1) * 100
    if move > 0 and st["h"][i] - c <= half:
        side = "up"
    elif move < 0 and c - st["l"][i] <= half:
        side = "down"
    else:
        return None
    for b in ms.BANDS:
        if abs(abs(move) - b) <= ms.BAND_TOL:
            return side, b
    return None


def lower_circuit_at(st: dict, i: int, band: dict):
    """Mirror of momentum.circuit_at for the lower circuit: 'locked' with a known band, else 'possible'."""
    prev, c = st["c"][i - 1], st["c"][i]
    tick = bp.tick_for(prev)
    if c - st["l"][i] > tick / 2 + 1e-9:
        return None
    b = band["band_pct"]
    if b == 0:
        return None
    if b:
        lower = bp.to_tick(prev * (1 - b / 100), None, "up")
        return {"status": "locked", "band_pct": b, "lower": lower} if c <= lower + tick / 2 + 1e-9 else None
    lc = limit_close(st, i)
    return {"status": "possible", "band_pct": lc[1], "lower": c} if lc and lc[0] == "down" else None


def circuit_streak(st: dict, end: int, band: dict, fn) -> dict:
    """Consecutive circuit closes ending at bar `end`, and how many of the last 10 sessions locked."""
    hits = []
    j = end
    while j >= 1:
        x = fn(st, j, band)
        if not x:
            break
        hits.append(x)
        j -= 1
    in10 = sum(1 for k in range(max(1, end - 9), end + 1) if fn(st, k, band))
    kind = None
    if hits or in10:
        kind = "locked" if band["band_pct"] else "possible"
    return {"streak": len(hits), "in_10": in10, "kind": kind,
            "band_pct": hits[0]["band_pct"] if hits else band["band_pct"]}


def band_tightening(st: dict, i: int, band: dict, lookback: int = 60):
    """Hint that the exchange cut the price band during the run (20 -> 10 -> 5 is the usual ladder).

    With a known band: any daily move in the window that the current band would not have allowed.
    Without one: limit closes at a wide band, later followed by two or more at a narrower one.
    """
    lo = max(1, i - lookback + 1)
    known = band["band_pct"]
    if known == 0:
        return None
    moves = [(j, abs(st["c"][j] / st["c"][j - 1] - 1) * 100) for j in range(lo, i + 1)]
    if known:
        wide = [(j, m) for j, m in moves if m > known + 0.5]
        if not wide:
            return None
        j, m = wide[-1]
        was = next((b for b in ms.BANDS if b >= m - ms.BAND_TOL), ms.BANDS[-1])
        return {"from_pct": was, "to_pct": known, "after": st["date"][j], "source": "--bands vs the tape"}
    lims = [(j, x[1]) for j, x in ((j, limit_close(st, j)) for j in range(lo, i + 1)) if x]
    if len(lims) < 3:
        return None
    now = lims[-1][1]
    wider = [(j, b) for j, b in lims if b > now]
    if not wider:
        return None
    j0 = wider[-1][0]
    since = [b for j, b in lims if j > j0]
    if len(since) < 2 or any(m > now + 0.25 for j, m in moves if j > j0):
        return None
    return {"from_pct": wider[-1][1], "to_pct": now, "after": st["date"][j0], "source": "inferred from limit closes"}


# ---------------------------------------------------------------------------
# Climax (upside exhaustion)
# ---------------------------------------------------------------------------
def up_streak(c: list, q: int) -> int:
    k = 0
    while q - k >= 1 and c[q - k] > c[q - k - 1]:
        k += 1
    return k


def up_weeks(st: dict, q: int) -> int:
    weeks = {}
    for i in range(max(0, q - 90), q + 1):
        weeks[tuple(st["date"][i].isocalendar()[:2])] = st["c"][i]
    closes = list(weeks.values())
    k = 0
    while k < len(closes) - 1 and closes[-1 - k] > closes[-2 - k]:
        k += 1
    return k


def confirmations(st: dict, p: int) -> dict:
    """What has happened since the peak bar p that says the run is over."""
    c, h, l, e10, dt = st["c"], st["h"], st["l"], st["ema10"], st["date"]
    last = len(c) - 1
    out = {"close_below_prior_low": None, "close_below_ema10": None, "lower_high": None}
    for j in range(p, last + 1):
        if out["close_below_prior_low"] is None and c[j] < l[j - 1]:
            out["close_below_prior_low"] = {"date": dt[j], "close": c[j], "level": l[j - 1]}
        if out["close_below_ema10"] is None and j > p and e10[j] is not None and c[j] < e10[j]:
            out["close_below_ema10"] = {"date": dt[j], "close": c[j], "level": _r(e10[j])}
    # lower high: a pullback, a bounce that stops short of the peak, then a close under the pullback low
    k = next((j for j in range(p + 2, last + 1) if h[j] > h[j - 1]), None)
    if k is not None:
        pull_low, bh, bi = min(l[p + 1:k]), h[k], k
        for j in range(k, last + 1):
            if h[j] > bh:
                bh, bi = h[j], j
            if j > k and c[j] < pull_low:
                out["lower_high"] = {"date": dt[j], "bounce_high": bh, "bounce_date": dt[bi], "broke": pull_low}
                break
    first = [(v["date"], k_) for k_, v in out.items() if v and k_ != "lower_high"]
    out["confirmed_on"] = min(first)[0] if first else None
    out["by"] = [k_ for k_, v in out.items() if k_ in ("close_below_prior_low", "close_below_ema10", "lower_high")
                 and v]
    return out


def climax_read(st: dict, cfg) -> dict:
    c, o, h, l, v, dt = st["c"], st["o"], st["h"], st["l"], st["v"], st["date"]
    n = len(c)
    if n < 30:
        raise ValueError(f"only {n} bars (the climax read needs 30+, 200+ for every factor)")
    last = n - 1
    p = max(range(max(21, n - cfg.lookback), n), key=lambda i: (h[i], i))     # the high of the move
    q = p if c[p] >= c[p - 1] else p - 1                                       # last up-close of the run
    atr = atr_before(st, p)
    band = ms.band_for(st, p)
    factors, notes, flags = [], [], []

    def add(key, label, pts, detail):
        factors.append({"key": key, "label": label, "points": round(pts, 1), "max": WEIGHTS[key], "detail": detail})

    # 1. extension: how far the high stands above the averages
    s20, s50, s200 = st["sma20"][p], st["sma50"][p], st["sma200"][p]
    ext20 = (h[p] - s20) / atr if s20 and atr else None
    ext50, ext200 = _pct(h[p], s50), _pct(h[p], s200)
    pts = 10 * ramp(ext20, 2, 6) + 10 * ramp(ext50, 15, 50) + 5 * ramp(ext200, 40, 120)
    add("extension", "Stretch above the averages", pts,
        ", ".join(x for x in (f"{ext20:.1f} ATR above the 20 DMA" if ext20 is not None else None,
                              f"{ext50:+.0f}% vs 50 DMA" if ext50 is not None else "50 DMA needs 50 bars",
                              f"{ext200:+.0f}% vs 200 DMA" if ext200 is not None else "200 DMA needs 200 bars")
                  if x))
    # 2. acceleration: daily pace of the last 5 sessions against the last 20
    roc5, roc20 = _pct(c[q], c[q - 5]), _pct(c[q], c[q - 20])
    accel = (roc5 / 5) / (roc20 / 20) if roc20 and roc20 > 0 else None
    add("acceleration", "Acceleration (5-day vs 20-day pace)", 15 * ramp(accel, 1.2, 3.0) * ramp(roc5, 3, 10),
        f"5 sessions {roc5:+.1f}%, 20 sessions {roc20:+.1f}%"
        + (f": the last week ran {accel:.1f}x the 20-day pace" if accel is not None else ": no 20-day advance"))
    # 3. the run: size over 10/20/60 sessions, and unbroken up days / up weeks
    g = {k: _pct(c[q], c[q - k]) if q >= k else None for k in (10, 20, 60)}
    streak, weeks = up_streak(c, q), up_weeks(st, q)
    pts = 15 * max(ramp(g[10], 15, 50), ramp(g[20], 25, 80), ramp(g[60], 50, 150))
    pts += 6 * ramp(streak, 2, 8) + 4 * ramp(weeks, 2, 6)
    add("run", "Size and length of the run", pts,
        " / ".join(f"{k}d {g[k]:+.0f}%" for k in (10, 20, 60) if g[k] is not None)
        + f"; {streak} up day(s) and {weeks} up week(s) in a row")
    # 4. climax evidence: a blow-off on the tape, or (for banded stocks) a streak of upper-circuit closes
    win = range(max(1, p - 2), p + 1)
    trx = max((st["tr"][j] / a for j in win if (a := atr_before(st, j))), default=None)
    tr60 = max(st["tr"][max(1, p - 59):p + 1])
    range_top = max(st["tr"][j] for j in win) >= tr60
    vrs = [x for x in (vol_ratio(st, j) for j in win) if x is not None]
    volx = max(vrs) if vrs else None
    vol_top = st["has_vol"] and max(v[j] for j in win) >= max(v[max(0, p - 59):p + 1])
    gaps = sum(1 for j in range(max(1, p - 4), p + 1) if o[j] > h[j - 1] and o[j] >= c[j - 1] * 1.01)
    pos = close_pos(st, p)
    big_bar = atr is not None and st["tr"][p] >= atr
    rev = 8 * ramp(1 - pos / 100, 0.5, 0.8) if pos is not None and big_bar else 0.0
    tape = (5 * ramp(trx, 1.5, 3.0) + (3 if range_top and trx and trx >= 1.5 else 0)
            + 5 * ramp(volx, 1.5, 4.0) + (3 if vol_top and volx and volx >= 1.5 else 0) + min(6, 3 * gaps) + rev)
    end = p if ms.circuit_at(st, p, band) else p - 1
    uc = circuit_streak(st, end, band, ms.circuit_at)
    uc["lock_broke"] = end != p and uc["streak"] > 0
    circ_pts = min(30, 6 * uc["streak"])
    detail = (f"widest bar {trx:.1f}x ATR" + (" (largest in 60 sessions)" if range_top else "") if trx else "range n/a")
    detail += ", " + (f"volume {volx:.1f}x the 50-day average" + (" (highest in 60 sessions)" if vol_top else "")
                      if volx else "no volume data")
    detail += f", {gaps} gap-up(s) in the last 5 sessions"
    if pos is not None:
        detail += f", peak day closed at {pos:.0f}% of its range" + (" (reversal bar)" if rev >= 4 else "")
    if uc["streak"]:
        detail += f"; {uc['streak']} upper-circuit close(s) in a row" + (", then the lock broke" if uc["lock_broke"] else "")
    add("climax", "Blow-off bar or circuit streak", max(tape, circ_pts), detail)
    # 5. price band cut by the exchange, or a stock already in a surveillance-sized band
    tight = band_tightening(st, p, band)
    small = band["band_pct"] in (2.0, 5.0)
    running = factors[2]["points"] > 0
    add("band", "Price-band tightening", 5 if tight else 3 if small and running else 0,
        f"band cut from {tight['from_pct']:g}% to {tight['to_pct']:g}% after {_iso(tight['after'])} ({tight['source']})"
        if tight else f"{band['band_pct']:g}% band ({band['source']})" if small else "no band cut seen")

    score = int(round(min(100, sum(f["points"] for f in factors))))
    conf = confirmations(st, p)
    if score < EXTENDED:
        stage = "not extended"
    elif conf["confirmed_on"]:
        stage = "reversal confirmed"
    else:
        stage = "climax" if score >= CLIMAX else "extended"

    if uc["streak"] or uc["in_10"]:
        flags.append("UC" if uc["kind"] == "locked" else "UC?")
        notes.append(f"{uc['streak']} upper-circuit close(s) in a row, {uc['in_10']} in the last 10 sessions"
                     + (" (band inferred from the tape: confirm on NSE/BSE)" if uc["kind"] != "locked" else "")
                     + ". Locked closes are not prices you could have bought; when the lock breaks the same "
                       "queue turns into sellers, often at the lower circuit.")
    if small:
        flags.append(f"band{band['band_pct']:g}")
    if tight:
        flags.append("band-cut")
        notes.append(f"The price band looks cut from {tight['from_pct']:g}% to {tight['to_pct']:g}%. Exchanges "
                     "narrow the band and add ASM/GSM margins after sharp run-ups; that takes leverage out and "
                     "often marks the last leg. Check the NSE/BSE surveillance list and the series (EQ vs BE).")
    elif small:
        notes.append(f"{band['band_pct']:g}% band: surveillance, trade-for-trade or SME. Exits can lock at the "
                     "lower circuit, so sell into strength rather than trusting a stop.")
    liq = bp.liquidity(st["rows"][:p + 1], cfg.min_turnover_cr)
    if liq["ok"] is False:
        flags.append("illiquid")
        notes.append(f"Only ₹{liq['avg_traded_value_cr']:.2f} cr/day traded: operator-driven moves live here.")
    if ms.monthly_expiry_week(dt[p].date()) and band["band_pct"] in (None, 0.0) and score >= EXTENDED:
        flags.append("expiry-wk")
        notes.append("The high came in F&O monthly-expiry week: short covering and rollover can exaggerate "
                     "the last leg, and unwind it just as fast.")
    watch = []
    if stage in ("extended", "climax"):
        watch.append(f"a close below the previous session's low (tomorrow that is {bp.px(l[last])})")
        if st["ema10"][last]:
            watch.append(f"a close below the 10 EMA ({bp.px(st['ema10'][last])})")
        watch.append(f"a bounce that stops short of {bp.px(h[p])} and then closes under the pullback low (lower high)")
    return {"symbol": st["name"], "date": dt[last], "close": c[last], "score": score, "stage": stage,
            "peak": {"date": dt[p], "high": h[p], "low": l[p], "close": c[p], "sessions_ago": last - p,
                     "off_high_pct": _r(_pct(c[last], h[p]))},
            "factors": factors,
            "metrics": {"ext20_atr": _r(ext20), "ext50_pct": _r(ext50), "ext200_pct": _r(ext200),
                        "roc5_pct": _r(roc5), "roc20_pct": _r(roc20), "accel": _r(accel),
                        "gain_10d_pct": _r(g[10]), "gain_20d_pct": _r(g[20]), "gain_60d_pct": _r(g[60]),
                        "up_days": streak, "up_weeks": weeks, "range_vs_atr": _r(trx), "vol_vs_50d": _r(volx),
                        "late_gaps": gaps, "peak_close_pos_pct": _r(pos, 0)},
            "circuit": uc, "band": band, "tightening": tight, "confirmation": conf, "watch": watch,
            "levels": {"prior_day_low": l[last], "ema10": _r(st["ema10"][last]), "ema20": _r(st["ema20"][last]),
                       "sma50": _r(st["sma50"][last]), "climax_high": h[p], "climax_low": l[p]},
            "liquidity": liq, "flags": flags, "notes": notes, "_p": p, "_st": st}


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------
def hold_plan(cl: dict, cfg) -> dict:
    st, stage = cl["_st"], cl["stage"]
    c, l = st["c"], st["l"]
    last = len(c) - 1
    tick = _tick(cfg, c[last])
    atr = atr_before(st, last + 1)
    band = cl["band"]["band_pct"]
    pct = cfg.sell_pct if cfg.sell_pct is not None else SELL_PCT[stage]
    trail = cfg.trail
    if trail == "auto":
        trail = "prior-low" if stage in ("climax", "reversal confirmed") or cl["circuit"]["streak"] >= 2 else "ema10"
    e10 = st["ema10"][last]
    if trail == "ema10" and e10 is None:
        trail = "prior-low"
    if trail == "prior-low":
        trigger = bp.to_tick(l[last] - tick, tick, "down")
        rule = (f"one tick under the last session's low {bp.px(l[last])}. After every close move it up to one "
                "tick under that day's low; never down.")
    else:
        trigger = bp.to_tick(e10 - 0.5 * (atr or 0), tick, "down")
        rule = (f"the exit rule is a daily close below the 10 EMA ({bp.px(e10)}), checked by hand at 15:20-15:25 "
                "(a GTT cannot wait for a close). The GTT sits half an ATR under it as the hard backstop.")
    slip = 2.0 if band in (2.0, 5.0) or (atr and atr / c[last] * 100 > 5) else 1.0
    limit = bp.to_tick(trigger * (1 - slip / 100), tick, "down")
    warnings = []
    lower = None
    if band:
        lower = bp.to_tick(c[last] * (1 - band / 100), tick, "up")
        if trigger <= lower:
            warnings.append(f"The stop {bp.px(trigger)} is at or below the next session's lower circuit "
                            f"{bp.px(lower)} ({band:g}% band): it cannot fire that day. Re-set it daily.")
        if limit < lower:
            limit = lower
        warnings.append("Banded stock: if it locks at the lower circuit no sell order fills, GTT or not. The "
                        "shares sold into strength are the only exit you fully control.")
    qty = cfg.qty
    sell_now = rest = None
    if qty:
        sell_now = int(qty * pct / 100)
        if pct > 0 and sell_now == 0 and qty >= 2:
            sell_now = 1
        rest = qty - sell_now
    locked_uc = bool(ms.circuit_at(st, last, cl["band"]))
    conf = cl["confirmation"]
    steps = []
    if pct > 0:
        n = f"{sell_now} share(s)" if qty else f"{pct:g}% of the position"
        if stage == "reversal confirmed":
            how = (f"the top is confirmed ({', '.join(x.replace('_', ' ') for x in conf['by'])}, "
                   f"{_iso(conf['confirmed_on'])}): sell now, do not wait for a bounce")
        elif locked_uc:
            how = ("into the circuit: a sell order at the upper-circuit price fills at once against the buy "
                   "queue. That liquidity disappears the day the lock opens")
        else:
            how = "into strength: a limit order near the highs today or into the next session's first push"
        steps.append(f"Sell {n} {how}.")
    else:
        steps.append("Score is below the extended zone: nothing to sell into. Keep your normal trail "
                     "(see trailing-stops).")
    n = f"{rest} " if qty else ""
    steps.append(f"Trail the rest: GTT single SELL {n}CNC, trigger {bp.px(trigger)}, limit {bp.px(limit)}. Stop is {rule}")
    if trail == "prior-low" and e10:
        steps.append(f"Anything still open exits on a daily close below the 10 EMA ({bp.px(e10)}); if the stock "
                     f"bases and reclaims its highs, that is a new trade for momentum-scanner.")
    mechanics = [
        "A GTT fires on the last traded price and sends a limit order. A gap below the limit stays unfilled: "
        "check the open on results days and after any circuit.",
        "Plain SL orders expire at 15:30 every day; the GTT lives until it triggers (up to a year). Shares "
        "bought today settle T+1, so some brokers accept the sell GTT only from the next morning.",
        "Gains on shares held under 12 months are short-term (20%); see indian-tax-for-traders before selling "
        "a holding that is close to turning long-term.",
    ]
    out = {"stage": stage, "score": cl["score"], "sell_pct": pct, "qty": qty, "sell_now": sell_now,
           "trail_qty": rest, "trail": trail, "stop_trigger": trigger, "stop_limit": limit,
           "stop_pct_below_close": _r((1 - trigger / c[last]) * 100), "lower_circuit_next": lower,
           "ema10": _r(e10), "steps": steps, "mechanics": mechanics, "warnings": warnings}
    if cfg.avg_price:
        out["avg_price"] = cfg.avg_price
        out["gain_now_pct"] = _r(_pct(c[last], cfg.avg_price))
        out["gain_locked_pct"] = _r(_pct(trigger, cfg.avg_price))
    return out


def short_plan(cl: dict, cfg, fno, ban) -> dict:
    st, p, sym = cl["_st"], cl["_p"], cl["symbol"]
    c, h, l = st["c"], st["h"], st["l"]
    last = len(c) - 1
    in_fno = True if cfg.fno else (sym in fno) if fno is not None else None
    banned = sym in ban if ban is not None else None
    band = cl["band"]["band_pct"]
    locked = bool(ms.circuit_at(st, last, cl["band"]))
    out = {"allowed": False, "fno": in_fno, "banned": banned, "reason": None, "warnings": list(SHORT_WARNINGS)}
    intraday = ("Cash-market shorts are intraday only (MIS): cover before the broker's ~15:15 square-off. A "
                "short left open is short delivery: the exchange buys it in at auction and charges the "
                "difference plus a penalty.")
    if locked:
        intraday += (" Today it closed locked at the upper circuit: brokers generally block fresh intraday "
                     "shorts there, and while the lock holds there are no sellers to cover from, so even the "
                     "intraday route is shut.")
    elif band in (2.0, 5.0):
        intraday += " Stocks in 2%/5% bands, ASM/GSM or trade-for-trade usually cannot be shorted intraday at all."
    out["intraday_note"] = intraday
    if not in_fno:
        why = "is not in F&O" if in_fno is False else "has no F&O status given (pass --fno or --fno-list)"
        out["reason"] = (f"{sym} {why}: no overnight short is possible. There are no stock futures or puts, and "
                         "a cash short must be covered the same day. If you hold it, use the holder plan; if "
                         "you don't, the trade is simply not to buy it.")
        return out
    if band:
        out["warnings"].append(f"--bands lists a {band:g}% fixed band, but F&O stocks have none. Re-check that "
                               f"{sym} is still in the F&O list.")
    if banned:
        out["reason"] = (f"{sym} is in the F&O ban (open interest above 95% of MWPL): only position-reducing "
                         "trades are allowed, so no new short future and no new put until it leaves the ban "
                         "(below 80%).")
        return out
    if cl["score"] < EXTENDED and not cfg.force:
        out["reason"] = (f"Score {cl['score']} is below {EXTENDED}: the move is not extended, so there is nothing "
                         "to fade. Shorting an ordinary uptrend is how accounts get squeezed (--force overrides).")
        return out
    tick = _tick(cfg, c[last])
    trig = bp.to_tick(l[p] - tick, tick, "down")
    hit = next((j for j in range(p + 1, last + 1) if l[j] < l[p]), None)
    if hit is None:
        status, entry = "waiting", trig
        trig_note = (f"No entry yet. Sell only if price trades below the climax-day low {bp.px(l[p])} "
                     f"(sell-stop trigger {bp.px(trig)}), or after a bounce fails under the high and turns "
                     "down (first lower high). Until then a short is a bet against the strongest stock on the board.")
    elif c[last] >= trig:
        status, entry = "triggered, then recovered", trig
        trig_note = (f"The climax-day low broke on {_iso(st['date'][hit])} but price is back above it. A "
                     f"fresh break of {bp.px(trig)} re-arms the entry.")
    else:
        status, entry = "triggered", bp.to_tick(c[last], tick, "nearest")
        trig_note = (f"Triggered on {_iso(st['date'][hit])} (traded below the climax-day low {bp.px(l[p])}). "
                     f"Entry is priced at the last close {bp.px(entry)}; the stop is further away now, so "
                     "waiting for a bounce that fails (lower high) gives a tighter one.")
    stop = bp.to_tick(h[p] * (1 + cfg.stop_buffer_pct / 100), _tick(cfg, h[p]), "up")
    if stop <= h[p]:
        stop = round(stop + _tick(cfg, h[p]), 2)
    r_unit = stop - entry
    targets = []
    for label, lvl in (("10 EMA", st["ema10"][last]), ("20 EMA", st["ema20"][last]), ("50 DMA", st["sma50"][last])):
        if lvl and lvl < entry:
            px_ = bp.to_tick(lvl, tick, "nearest")
            targets.append({"label": label, "price": px_, "R": _r((entry - px_) / r_unit),
                            "pct_from_entry": _r(_pct(px_, entry))})
    if not targets:
        out["warnings"].append("Price is already at or below the 10/20 EMA and 50 DMA: the easy part of the "
                               "fall is done.")
    elif targets[0]["R"] < 1:
        out["warnings"].append(f"The first target ({targets[0]['label']}) pays only {targets[0]['R']:.1f}R: "
                               "take partial there or skip.")
    lot = cfg.lot or (fno or {}).get(sym)
    sizing = None
    budget = cfg.capital * cfg.risk_pct / 100 if cfg.capital else None
    if lot and cfg.capital:
        margin = cfg.margin_per_lot or entry * lot * cfg.margin_pct / 100
        sz = risk.position_size(cfg.capital, cfg.risk_pct, entry, stop, lot_size=lot, margin_per_lot=margin,
                                side="short", cost_pct=FUT_COST_PCT, max_margin_pct=cfg.max_margin_pct)
        sizing = {"lot": lot, "lots": sz["lots"], "qty": sz["units"], "contract_value": lot * entry,
                  "risk_budget": budget, "risk_per_lot": lot * (r_unit + sz["cost_per_unit"]),
                  "risk_if_stopped": sz["risk_amount"], "margin_per_lot": margin,
                  "margin_estimated": not cfg.margin_per_lot, "margin_total": sz["lots"] * margin,
                  "notes": sz["notes"]}
        if not cfg.margin_per_lot:
            sizing["notes"].append(f"Margin estimated at {cfg.margin_pct:g}% of contract value; volatile "
                                   "names carry more. Pass --margin-per-lot from your broker's calculator.")
    elif not lot:
        out["warnings"].append("No lot size: pass --lot (or an --fno-list with lots, e.g. NSE fo_mktlots.csv) "
                               "and --capital to size the trade.")
    put = None
    if cfg.put_premium:
        lots = int(budget // (cfg.put_premium * lot)) if budget and lot else None
        put = {"premium": cfg.put_premium, "strike": cfg.put_strike,
               "breakeven": cfg.put_strike - cfg.put_premium if cfg.put_strike else None,
               "cost_per_lot": cfg.put_premium * lot if lot else None, "lots": lots,
               "max_loss": lots * lot * cfg.put_premium if lots is not None else None,
               "note": "Buying a put caps the loss at the premium paid. After a vertical run implied "
                       "volatility is high, so the put is expensive and loses value if the stock just "
                       "stalls: a bear put spread cuts the cost (see options-strategies)."}
    if ms.monthly_expiry_week(st["date"][last].date()):
        out["warnings"].append("This is monthly-expiry week (last Tuesday): use the next-month contract, and "
                               "expect physical-settlement margin on the near month.")
    out.update({"allowed": True, "instrument": "stock futures (short) or long put; never an overnight cash short",
                "status": status, "trigger_note": trig_note, "entry": entry, "trigger": trig,
                "climax_high": h[p], "climax_low": l[p], "stop": stop,
                "stop_pct_from_entry": _r(_pct(stop, entry)), "R_per_share": _r(r_unit), "targets": targets,
                "sizing": sizing, "put": put})
    return out


# ---------------------------------------------------------------------------
# Washout (downside exhaustion, long entry)
# ---------------------------------------------------------------------------
def undercut(st: dict, i: int, lookback: int = 60, wing: int = 2):
    """A prior pivot low that held until bar i (or i-1) took it out. Prefers a reclaimed level, then the oldest."""
    l, c = st["l"], st["c"]
    best = None
    for j in range(max(wing, i - lookback), i - wing):
        if l[j] > min(l[j - wing:j + wing + 1]):
            continue
        if j + 1 < i - 1 and min(l[j + 1:i - 1]) < l[j]:
            continue                                   # already broken earlier
        if min(l[i - 1], l[i]) >= l[j]:
            continue
        cand = {"level": l[j], "date": st["date"][j], "age": i - j, "reclaimed": c[i] > l[j]}
        if best is None or (cand["reclaimed"], cand["age"]) > (best["reclaimed"], best["age"]):
            best = cand
    return best


def washout_at(st: dict, i: int, cfg):
    c, o, h, l, v, dt = st["c"], st["o"], st["h"], st["l"], st["v"], st["date"]
    if i < 30:
        return None
    rng = h[i] - l[i]
    if rng <= 0 or l[i] > min(l[i - 4:i]):
        return None                                    # not the low of the last five sessions
    hi = max(range(max(0, i - cfg.decline_lookback), i), key=lambda j: (h[j], j))
    depth = (h[hi] - l[i]) / h[hi] * 100
    wick = (min(o[i], c[i]) - l[i]) / rng
    uc = undercut(st, i)
    if depth < 0.6 * cfg.min_depth_pct or (wick < 0.3 and not uc):
        return None
    reasons, notes, flags = [], [], []
    bars_down = i - hi
    if depth < cfg.min_depth_pct:
        reasons.append(f"pullback only {depth:.1f}% from the high (needs {cfg.min_depth_pct:g}%+): no real decline")
    elif depth > cfg.max_depth_pct:
        reasons.append(f"down {depth:.1f}% from the high (max {cfg.max_depth_pct:g}%): a broken stock, not a pullback")
    if bars_down < 3:
        reasons.append(f"only {bars_down} session(s) off the high: a one-day drop is not selling exhaustion")
    # context: was this a stock worth buying before it fell?
    base = min(l[max(0, hi - 120):hi]) if hi > 0 else l[0]
    prior_run = (h[hi] / base - 1) * 100
    s200 = st["sma200"]
    above200 = bool(s200[i] and i >= 20 and s200[i - 20] and c[i] > s200[i] > s200[i - 20])
    quality = st["name"] in cfg.quality
    if not (prior_run >= cfg.min_prior_run_pct or above200 or quality):
        reasons.append(f"no prior uptrend (+{prior_run:.0f}% into the high, not above a rising 200 DMA): a "
                       "hammer in a downtrend is a bounce, not a pullback entry")
    pos = (c[i] - l[i]) / rng * 100
    if wick < cfg.min_wick:
        reasons.append(f"lower wick is {wick * 100:.0f}% of the bar (needs {cfg.min_wick * 100:g}%+)")
    if pos < cfg.min_close_pos:
        reasons.append(f"closed at {pos:.0f}% of the day's range (needs {cfg.min_close_pos:g}%+): sellers "
                       "still had it at the bell")
    atr = atr_before(st, i)
    if atr and st["tr"][i] < 0.75 * atr:
        reasons.append(f"narrow bar ({st['tr'][i] / atr:.1f}x ATR): no flush to reverse")
    vr = vol_ratio(st, i)
    if st["has_vol"] and (vr is None or vr < cfg.min_vol_mult):
        reasons.append(f"volume {vr or 0:.1f}x the 50-day average (needs {cfg.min_vol_mult:g}x): nobody capitulated")
    vol_top = st["has_vol"] and v[i] >= max(v[hi:i + 1])
    if uc and not uc["reclaimed"] and uc["age"] >= 5:
        reasons.append(f"closed below the {_iso(uc['date'])} low {bp.px(uc['level'])} it undercut: a breakdown, "
                       "not a reclaim")
    band = ms.band_for(st, i)
    if lower_circuit_at(st, i, band):
        reasons.append("closed at the lower circuit: still locked, sellers unfilled")
    lc = circuit_streak(st, i - 1, band, lower_circuit_at)
    liq = bp.liquidity(st["rows"][max(0, i - 20):i], cfg.min_turnover_cr)
    if liq["ok"] is False:
        reasons.append(f"illiquid: ₹{liq['avg_traded_value_cr']:.2f} cr/day traded (min ₹{cfg.min_turnover_cr:g} cr)")
    # score: context 20, depth 15, undercut-and-reclaim 25, wick 15, close 10, volume 15 (+5 lower-circuit streak ending)
    parts = {"context": 5 + 5 * ramp(prior_run, cfg.min_prior_run_pct, 2 * cfg.min_prior_run_pct)
             if prior_run >= cfg.min_prior_run_pct else 0.0}
    parts["context"] += 10 if above200 or quality else 0
    parts["depth"] = (15.0 if 10 <= depth <= 25 else 10 + 5 * ramp(depth, cfg.min_depth_pct, 10) if depth < 10
                      else 15 - 10 * ramp(depth, 25, cfg.max_depth_pct))
    parts["undercut"] = (25.0 if uc["age"] >= 5 else 15.0) if uc and uc["reclaimed"] else 0.0
    parts["wick"] = 15 * ramp(wick, 0.4, 0.7)
    parts["close"] = 10 * ramp(pos, 50, 90)
    parts["volume"] = 10 * ramp(vr, 1.0, 2.5) + (5 if vol_top and vr and vr >= 1 else 0)
    bonus = 5 if lc["streak"] >= 2 else 0
    score = int(round(min(100, sum(parts.values()) + bonus)))
    if lc["streak"] >= 2:
        flags.append("LC-end")
        notes.append(f"{lc['streak']} lower-circuit close(s) before this bar and the lock opened: trapped "
                     "sellers finally got out. Size small; the band can lock again tomorrow.")
    if band["band_pct"] in (2.0, 5.0):
        flags.append(f"band{band['band_pct']:g}")
    tick = _tick(cfg, h[i])
    entry = bp.to_tick(h[i] + tick, tick, "up")
    stop = bp.to_tick(l[i] - _tick(cfg, l[i]), _tick(cfg, l[i]), "down")
    risk_pct = (entry - stop) / entry * 100
    if risk_pct > 8:
        flags.append("wide-stop")
        notes.append(f"The bar is {risk_pct:.1f}% tall: a stop under its low is wide, so the position must be small.")
    last = len(c) - 1
    status, when = "pending", None
    for j in range(i + 1, last + 1):
        if l[j] < l[i]:
            status, when = "failed (low broken)", dt[j]
            break
        if h[j] > h[i]:
            status, when = "triggered", dt[j]
            break
    targets = [{"label": lab, "price": bp.to_tick(x, None, "nearest"), "R": _r((x - entry) / (entry - stop))}
               for lab, x in (("20 EMA", st["ema20"][i]), ("prior high", h[hi])) if x and x > entry]
    ev = {"symbol": st["name"], "date": dt[i], "close": c[i], "score": score, "passed": not reasons,
          "decline": {"high": h[hi], "high_date": dt[hi], "depth_pct": _r(depth), "sessions": bars_down,
                      "depth_atr": _r((h[hi] - l[i]) / atr) if atr else None},
          "context": {"prior_run_pct": _r(prior_run), "above_rising_200dma": above200, "quality_list": quality},
          "undercut": uc, "wick_pct": _r(wick * 100, 0), "close_pos_pct": _r(pos, 0), "vol_vs_50d": _r(vr),
          "highest_volume_of_decline": bool(vol_top), "lower_circuit_streak": lc["streak"],
          "points": {k: round(x, 1) for k, x in parts.items()}, "band": band,
          "turnover_cr": _r(liq["avg_traded_value_cr"]), "entry": entry, "stop": stop, "risk_pct": _r(risk_pct),
          "targets": targets, "status": status, "status_date": when, "flags": flags, "notes": notes,
          "reasons": reasons, "sizing": None}
    if cfg.capital and not reasons:
        sz = risk.position_size(cfg.capital, cfg.risk_pct, entry, stop)
        ev["sizing"] = {"qty": sz["units"], "position_value": sz["position_value"],
                        "risk_if_stopped": sz["risk_amount"], "notes": sz["notes"]}
    return ev


def find_washouts(stocks: list, cfg, days: int):
    start = ms.session_window(stocks, days)
    hits, rejects = [], []
    for st in stocks:
        for i in range(30, len(st["c"])):
            if st["date"][i].date() < start:
                continue
            ev = washout_at(st, i, cfg)
            if ev:
                (hits if ev["passed"] else rejects).append(ev)
    hits.sort(key=lambda e: (-e["score"], e["symbol"]))
    rejects.sort(key=lambda e: (e["symbol"], e["date"]))
    return start, hits, rejects


# ---------------------------------------------------------------------------
# Reaction (did price respond to the news?)
# ---------------------------------------------------------------------------
def reaction(st: dict, cfg, bench: dict | None) -> dict:
    c, o, h, l, dt = st["c"], st["o"], st["h"], st["l"], st["date"]
    last = len(c) - 1
    sign = 1 if cfg.news == "bullish" else -1
    d1 = next((i for i, d in enumerate(dt) if (d.date() > cfg.date if cfg.after_close else d.date() >= cfg.date)),
              None)
    base = {"symbol": st["name"], "news": cfg.news, "news_date": cfg.date, "after_close": cfg.after_close,
            "crowding": cfg.crowding}
    if d1 is None:
        return {**base, "verdict": "inconclusive", "rule": "No session has traded since the news: nothing to judge yet.",
                "sessions_used": 0, "tests": [], "gate": _gate("inconclusive", cfg, False)}
    if d1 < 15:
        raise ValueError(f"{st['name']}: only {d1} bars before {cfg.date} (needs 15+ to measure a normal day)")
    end = min(d1 + cfg.sessions - 1, last)
    n = end - d1 + 1
    ref = c[d1 - 1]
    atr_pct = atr_before(st, d1) / ref * 100
    b1 = bn = 0.0
    if bench:
        try:
            b0 = bench[dt[d1 - 1].date()]
            b1, bn = _pct(bench[dt[d1].date()], b0), _pct(bench[dt[end].date()], b0)
        except KeyError:
            raise ValueError("benchmark has no bar on one of the reaction dates")
    chg1, net = _pct(c[d1], ref), _pct(c[end], ref)
    m1, mn = sign * (chg1 - b1) / atr_pct, sign * (net - bn) / atr_pct      # ATRs moved WITH the news
    pos = close_pos(st, d1)
    fav = None if pos is None else pos if sign > 0 else 100 - pos            # 100 = closed at the news-friendly end
    vr = vol_ratio(st, d1)
    mid = (h[d1] + l[d1]) / 2
    broke = c[end] < l[d1] if sign > 0 else c[end] > h[d1]
    held_mid = sign * (c[end] - mid) >= 0
    thr = cfg.min_move_atr
    good, bad = ("up", "down") if sign > 0 else ("down", "up")
    end_word = "low" if sign > 0 else "high"
    rel = " (net of the benchmark)" if bench else ""
    vol_ok = vr is None or vr >= cfg.fail_vol_mult
    t1 = m1 < 0 and fav is not None and fav <= 35 and vol_ok
    t2 = n >= 2 and mn <= -thr and broke
    t3 = mn >= thr and held_mid
    tests = [
        {"rule": "rejection day", "fired": t1,
         "text": f"day 1 closes {bad} against the pre-news close{rel}, in the worst 35% of its range, on volume "
                 f"≥ {cfg.fail_vol_mult:g}x the 50-day average",
         "values": f"day 1 {chg1:+.2f}% ({m1:+.2f} ATR with the news), close at {fav:.0f}% toward the "
                   f"news-friendly end" + (f", volume {vr:.1f}x" if vr else ", no volume data") if fav is not None
         else f"day 1 {chg1:+.2f}%, no range"},
        {"rule": "no follow-through", "fired": t2,
         "text": f"after 2-3 sessions price is ≥ {thr:g} ATR the wrong way{rel} and has closed beyond the "
                 f"news-day {end_word}",
         "values": f"{n} session(s): {net:+.2f}% ({mn:+.2f} ATR with the news), news-day {end_word} "
                   f"{'broken' if broke else 'intact'}"},
        {"rule": "response held", "fired": t3,
         "text": f"price is ≥ {thr:g} ATR {good}{rel} and the last close is on the right side of the news-day midpoint",
         "values": f"{mn:+.2f} ATR with the news, last close {bp.px(c[end])} vs midpoint {bp.px(mid)}"},
    ]
    if t1 or t2:
        verdict = "failed"
        fired = tests[0] if t1 else tests[1]
    elif t3:
        verdict, fired = "confirmed", tests[2]
    else:
        verdict, fired = "inconclusive", None
    if fired:
        rule = f"{fired['rule']}: {fired['text']}. Here: {fired['values']}."
    else:
        rule = (f"No rule fired: the move is {mn:+.2f} ATR with the news{rel}, inside ±{thr:g} ATR of noise or "
                "mixed" + ("" if vol_ok or not (m1 < 0 and fav is not None and fav <= 35)
                           else f"; day 1 looked like a rejection but volume was only {vr:.1f}x") + ".")
    gap = _pct(o[d1], ref)
    notes = []
    if sign * gap >= 0.5 * atr_pct and sign * (c[d1] - o[d1]) < 0:
        notes.append(f"Opened {gap:+.1f}% on the news and closed back {'below' if sign > 0 else 'above'} the "
                     "open: the first print was the best price of the day.")
    if n < cfg.sessions:
        notes.append(f"Only {n} of {cfg.sessions} session(s) have traded: re-run after the next close.")
    if bench and abs(bn) >= 1.5:
        notes.append(f"The benchmark moved {bn:+.1f}% over the same sessions; the verdict is on the stock's "
                     "move net of that.")
    out = {**base, "verdict": verdict, "rule": rule, "sessions_used": n,
           "day1": {"date": dt[d1], "pre_news_close": ref, "gap_pct": _r(gap), "chg_pct": _r(chg1),
                    "close_pos_pct": _r(pos, 0), "vol_vs_50d": _r(vr), "high": h[d1], "low": l[d1]},
           "window": {"end_date": dt[end], "close": c[end], "net_pct": _r(net), "atr_pct": _r(atr_pct),
                      "move_atr_with_news": _r(mn), "bench_pct": _r(bn) if bench else None,
                      "news_day_extreme_broken": broke},
           "tests": tests, "notes": notes, "gate": _gate(verdict, cfg, broke)}
    if verdict == "failed":
        out["next"] = (f"A close {'below' if sign > 0 else 'above'} the news-day {end_word} "
                       f"{bp.px(l[d1] if sign > 0 else h[d1])} is the price confirmation. "
                       + ("Holders: tighten with `plan`; do not add." if sign > 0 else
                          "Sellers ran out on bad news: look for a `washout` bar to define the entry."))
    return out


def _gate(verdict: str, cfg, price_confirmed: bool) -> dict:
    """Three checks in order: crowded positioning, failure to respond, price confirmation."""
    want = "long" if cfg.news == "bullish" else "short"
    crowded = None if cfg.crowding is None else cfg.crowding == want
    failed = verdict == "failed"
    if not failed:
        status = "no setup: the news was not rejected"
    elif crowded is None:
        status = ("reaction failure only: check positioning (fii-dii-flows participant OI, the stock's OI "
                  "against MWPL, delivery and MTF build-up) before calling it a contrarian setup")
    elif not crowded:
        status = f"reaction failure without a crowded {want} side: weaker, treat as a warning only"
    elif price_confirmed:
        status = f"contrarian setup complete: crowded {want}s, news they needed, no response, level broken"
    else:
        status = f"watch: crowded {want}s were handed their news and price refused; needs the level to break"
    return {"crowded": crowded, "reaction_failed": failed, "price_confirmed": bool(failed and price_confirmed),
            "status": status}


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------
def _route(sym: str, fno, ban) -> dict:
    if fno is None:
        return {"fno": None, "banned": None, "short_route": "F&O status unknown (pass --fno-list)"}
    banned = sym in ban if ban is not None else None
    if sym not in fno:
        return {"fno": False, "banned": banned, "short_route": "none overnight (not in F&O): holders scale out"}
    if banned:
        return {"fno": True, "banned": True, "short_route": "F&O ban: no new futures or puts"}
    return {"fno": True, "banned": banned, "short_route": "stock futures or puts, after a trigger"}


def run_scan(stocks: list, cfg, fno, ban) -> dict:
    exhausted, filtered, errors = [], [], []
    for st in stocks:
        try:
            r = climax_read(st, cfg)
        except ValueError as e:
            errors.append({"name": st["name"], "error": str(e)})
            continue
        if r["score"] < cfg.min_score:
            continue
        if r["liquidity"]["ok"] is False:
            filtered.append({"symbol": r["symbol"], "list": "exhausted", "score": r["score"],
                             "reason": f"illiquid: ₹{r['liquidity']['avg_traded_value_cr']:.2f} cr/day "
                                       f"(min ₹{cfg.min_turnover_cr:g} cr)"})
            continue
        exhausted.append({**{k: r[k] for k in ("symbol", "close", "score", "stage", "peak", "circuit", "flags",
                                               "watch")},
                          "confirmed_on": r["confirmation"]["confirmed_on"],
                          "why": "; ".join(f["detail"] for f in sorted(r["factors"], key=lambda f: -f["points"])[:2]),
                          **_route(r["symbol"], fno, ban)})
    start, hits, rejects = find_washouts(stocks, cfg, cfg.days)
    for e in rejects:
        if any(x.startswith("illiquid") for x in e["reasons"]):
            filtered.append({"symbol": e["symbol"], "list": "washouts", "score": e["score"],
                             "reason": "; ".join(e["reasons"])})
    exhausted.sort(key=lambda e: (-e["score"], e["symbol"]))
    washouts = [{**{k: e[k] for k in ("symbol", "date", "close", "score", "entry", "stop", "risk_pct", "status",
                                      "flags", "undercut", "decline")}, **_route(e["symbol"], fno, ban)}
                for e in hits]
    if cfg.top:
        exhausted, washouts = exhausted[:cfg.top], washouts[:cfg.top]
    return {"exhausted": exhausted, "washouts": washouts, "washout_window_start": start,
            "washout_rejects": len(rejects), "filtered": filtered, "errors": errors}


# ---------------------------------------------------------------------------
# Printing
# ---------------------------------------------------------------------------
def _fresh(stocks):
    last = max(st["date"][-1] for st in stocks).date()
    fr = ta.freshness([{"date": datetime.combine(last, datetime.min.time())}])
    if fr:
        print(fr)


def _skips(skipped):
    for s in skipped:
        print(f"   skipped {s['name']}: {s['error']}")


def print_climax_one(r: dict) -> None:
    pk = r["peak"]
    print(f"\n{r['symbol']} — parabolic score {r['score']}/100 → {r['stage'].upper()} "
          f"(as of {_iso(r['date'])}, close {bp.px(r['close'])})")
    ago = "today" if pk["sessions_ago"] == 0 else f"{pk['sessions_ago']} session(s) ago"
    print(f"High of the move: {bp.px(pk['high'])} on {_iso(pk['date'])} ({ago}); close is "
          f"{pk['off_high_pct']:+.1f}% from it" + (f" | {' '.join(r['flags'])}" if r["flags"] else ""))
    print("Factors")
    for f in r["factors"]:
        print(f"  {f['label']:<36}{f['points']:>5.1f}/{f['max']:<3} {f['detail']}")
    cf = r["confirmation"]
    if r["stage"] == "not extended":
        print(f"Below {EXTENDED}: an ordinary trend, not an exhaustion candidate. Trail it normally.")
    elif cf["confirmed_on"]:
        print(f"Top confirmed on {_iso(cf['confirmed_on'])}:")
        for k in ("close_below_prior_low", "close_below_ema10"):
            if cf[k]:
                print(f"  • {k.replace('_', ' ')}: closed {bp.px(cf[k]['close'])} vs {bp.px(cf[k]['level'])} "
                      f"on {_iso(cf[k]['date'])}")
        lh = cf["lower_high"]
        print(f"  • lower high: bounce to {bp.px(lh['bounce_high'])} failed, closed under {bp.px(lh['broke'])} on "
              f"{_iso(lh['date'])}" if lh else "  • lower high: not yet (a failed bounce would add to it)")
    else:
        print("No confirmation yet. A top is confirmed by the first of:")
        for w in r["watch"]:
            print(f"  • {w}")
    for n in r["notes"]:
        print(f"  ⚠ {n}")
    if r["stage"] != "not extended":
        print("Next: `plan` for the holder's scale-out and GTT trail (add --side short for an F&O name).")


def print_climax_table(rows: list) -> None:
    print(f"{'#':>3} {'Symbol':<14}{'Close':>10} {'Score':>6}  {'Stage':<19}{'High on':<11} {'Off high':>8} "
          f"{'UC':>3}  Flags")
    for k, r in enumerate(rows, 1):
        print(f"{k:>3} {r['symbol'][:13]:<14}{r['close']:>10,.2f} {r['score']:>6}  {r['stage']:<19}"
              f"{_iso(r['peak']['date']):<11} {r['peak']['off_high_pct']:>+7.1f}% {r['circuit']['streak']:>3}  "
              + " ".join(r["flags"]))


def print_plan(res: dict) -> None:
    cl = res["climax"]
    print(f"\n{cl['symbol']} — score {cl['score']}/100, {cl['stage']} | close {bp.px(cl['close'])}, high "
          f"{bp.px(cl['peak']['high'])} on {_iso(cl['peak']['date'])}")
    hp = res.get("hold")
    if hp:
        print("\nIf you hold it")
        for k, s in enumerate(hp["steps"], 1):
            print(f"  {k}. {s}")
        if hp.get("avg_price"):
            print(f"  Open gain {hp['gain_now_pct']:+.1f}% on {bp.px(hp['avg_price'])}; the stop locks "
                  f"{hp['gain_locked_pct']:+.1f}%.")
        for w in hp["warnings"]:
            print(f"  ⚠ {w}")
        for m in hp["mechanics"]:
            print(f"  - {m}")
    sp = res.get("short")
    if sp:
        print("\nIf you want to short it")
        if not sp["allowed"]:
            print(f"  ✗ {sp['reason']}")
            print(f"  - {sp['intraday_note']}")
        else:
            print(f"  Route  : {sp['instrument']}")
            print(f"  Trigger: {sp['trigger_note']}")
            print(f"  Entry {bp.px(sp['entry'])} | stop {bp.px(sp['stop'])} (above the climax high "
                  f"{bp.px(sp['climax_high'])}, {sp['stop_pct_from_entry']:+.1f}%) | risk ₹{sp['R_per_share']:,.2f}/share")
            for t in sp["targets"]:
                print(f"  Target {t['label']:<7} {bp.px(t['price'])}  {t['pct_from_entry']:+.1f}%  {t['R']:.1f}R")
            sz = sp["sizing"]
            if sz:
                print(f"  Size   : {sz['lots']} lot(s) × {sz['lot']} = {sz['qty']} shares | risk if stopped "
                      f"{risk.inr(sz['risk_if_stopped'])} of {risk.inr(sz['risk_budget'])} budget | margin "
                      f"{risk.inr(sz['margin_total'])}" + (" (estimated)" if sz["margin_estimated"] else ""))
                for n in sz["notes"]:
                    print(f"      {n}")
            pt = sp["put"]
            if pt:
                be = f", breakeven {bp.px(pt['breakeven'])}" if pt["breakeven"] else ""
                lots = f"{pt['lots']} lot(s), max loss {risk.inr(pt['max_loss'])}" if pt["lots"] is not None \
                    else "give --lot and --capital to size"
                print(f"  Put    : premium {bp.px(pt['premium'])}{be}: {lots}. {pt['note']}")
            for w in sp["warnings"]:
                print(f"  ⚠ {w}")


def print_washouts(res: dict, cfg) -> None:
    if not res["washouts"]:
        print("No selling-exhaustion bar passed the filters.")
    for e in res["washouts"]:
        d = e["decline"]
        print(f"\n{e['symbol']} — {_iso(e['date'])}: score {e['score']}/100 | {d['depth_pct']:.1f}% off "
              f"{bp.px(d['high'])} ({_iso(d['high_date'])}) in {d['sessions']} sessions"
              + (f" | {' '.join(e['flags'])}" if e["flags"] else ""))
        u = e["undercut"]
        print("  Undercut: " + (f"took out the {_iso(u['date'])} low {bp.px(u['level'])} and closed back above it"
                                if u and u["reclaimed"] else "no prior low reclaimed"))
        vol = f"{e['vol_vs_50d']:.1f}x volume" if e["vol_vs_50d"] else "no volume data"
        print(f"  Bar     : lower wick {e['wick_pct']:.0f}% of range, close at {e['close_pos_pct']:.0f}%, {vol}"
              + (" (highest of the decline)" if e["highest_volume_of_decline"] else ""))
        print(f"  Plan    : buy above {bp.px(e['entry'])}, stop {bp.px(e['stop'])} ({e['risk_pct']:.1f}% risk) "
              f"— {e['status']}" + (f" on {_iso(e['status_date'])}" if e["status_date"] else ""))
        for t in e["targets"]:
            print(f"  Target  : {t['label']} {bp.px(t['price'])} ({t['R']:.1f}R)")
        if e["sizing"]:
            print(f"  Size    : {e['sizing']['qty']} shares, {risk.inr(e['sizing']['risk_if_stopped'])} at risk")
        for n in e["notes"]:
            print(f"  ⚠ {n}")
    rej = res["rejected"]
    if rej and cfg.show_rejects:
        print(f"\nRejected ({len(rej)}):")
        for e in rej:
            print(f"  {_iso(e['date'])}  {e['symbol']:<14} " + "; ".join(e["reasons"]))
    elif rej:
        print(f"\n{len(rej)} near-miss(es) rejected: " + ", ".join(sorted({e['symbol'] for e in rej}))
              + ". Use --show-rejects for reasons.")


def print_reaction(r: dict) -> None:
    print(f"\n{r['symbol']} — {r['news']} news on {_iso(r['news_date'])}"
          + (" (after the close)" if r["after_close"] else "") + f" → {r['verdict'].upper()}")
    print(f"Rule: {r['rule']}")
    if r.get("day1"):
        d, w = r["day1"], r["window"]
        vol = f"{d['vol_vs_50d']:.1f}x volume" if d["vol_vs_50d"] else "no volume data"
        cp = f"{d['close_pos_pct']:.0f}%" if d["close_pos_pct"] is not None else "n/a"
        print(f"Day 1 ({_iso(d['date'])}): gap {d['gap_pct']:+.2f}%, close {d['chg_pct']:+.2f}% vs "
              f"{bp.px(d['pre_news_close'])}, at {cp} of the range, {vol}")
        print(f"Through {_iso(w['end_date'])} ({r['sessions_used']} session(s)): {w['net_pct']:+.2f}% = "
              f"{w['move_atr_with_news']:+.2f} ATR with the news (1 ATR = {w['atr_pct']:.2f}%)"
              + (f"; benchmark {w['bench_pct']:+.2f}%" if w["bench_pct"] is not None else ""))
        for t in r["tests"]:
            print(f"  [{'x' if t['fired'] else ' '}] {t['rule']}: {t['values']}")
    for n in r.get("notes", []):
        print(f"  ⚠ {n}")
    print(f"Gate: {r['gate']['status']}")
    if r.get("next"):
        print(f"Next: {r['next']}")


def print_scan(res: dict) -> None:
    print("\nExhausted on the upside (score ≥ {}):".format(res["rules"]["min_score"]))
    if not res["exhausted"]:
        print("  none")
    else:
        print(f"{'#':>3} {'Symbol':<14}{'Close':>10} {'Score':>6}  {'Stage':<19}{'F&O':<5} Short route")
        for k, e in enumerate(res["exhausted"], 1):
            fno = "yes" if e["fno"] else "no" if e["fno"] is False else "?"
            print(f"{k:>3} {e['symbol'][:13]:<14}{e['close']:>10,.2f} {e['score']:>6}  {e['stage']:<19}{fno:<5} "
                  f"{e['short_route']}" + (f"  [{' '.join(e['flags'])}]" if e["flags"] else ""))
        for k, e in enumerate(res["exhausted"], 1):
            print(f"{k:>3}. {e['symbol']}: {e['why']}.")
    print(f"\nWashed out on the downside (reversal bars since {res['washout_window_start']}):")
    if not res["washouts"]:
        print("  none")
    for k, e in enumerate(res["washouts"], 1):
        print(f"{k:>3} {e['symbol'][:13]:<14}{_iso(e['date'])}  score {e['score']:>3}  buy above {bp.px(e['entry'])}, "
              f"stop {bp.px(e['stop'])} ({e['risk_pct']:.1f}%) — {e['status']}"
              + (f"  [{' '.join(e['flags'])}]" if e["flags"] else ""))
    for f in res["filtered"]:
        print(f"   filtered {f['symbol']} ({f['list']}, score {f['score']}): {f['reason']}")
    print("\nNext: `plan` on an exhausted name you hold; size washout entries with position-sizing and run "
          "the trading-discipline gate first.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _parents():
    u = argparse.ArgumentParser(add_help=False)
    g = u.add_argument_group("universe (same flags as momentum.py)")
    g.add_argument("--csv-dir", help="folder with one OHLCV CSV per symbol (file name = symbol)")
    g.add_argument("--pattern", default="*.csv", help="glob inside --csv-dir (default *.csv)")
    g.add_argument("--csv", action="append", help="one stock CSV (repeatable)")
    g.add_argument("--symbols", help="comma list fetched via yfinance")
    g.add_argument("--exchange", default="NSE", choices=["NSE", "BSE"])
    g.add_argument("--period", default="2y", help="yfinance history period")
    g.add_argument("--as-of", help="evaluate as of YYYY-MM-DD, ignoring later bars")
    g.add_argument("--min-turnover-cr", type=float, default=5.0, help="min avg daily traded value, ₹ crore (5)")
    g.add_argument("--bands", help="CSV SYMBOL,band or JSON {SYMBOL: band}; band 2/5/10/20, 0 = no fixed band (F&O)")
    g.add_argument("--tick", type=float, help="force a tick size (default: NSE slab for the price)")
    g.add_argument("--json", action="store_true")
    one = argparse.ArgumentParser(add_help=False)
    one.add_argument("--symbol", help="pick one stock out of --csv-dir (or fetch it via yfinance)")
    cx = argparse.ArgumentParser(add_help=False)
    cx.add_argument("--lookback", type=int, default=15, help="sessions in which to look for the high of the move (15)")
    wo = argparse.ArgumentParser(add_help=False)
    g = wo.add_argument_group("washout rules")
    g.add_argument("--days", type=int, default=3, help="sessions to look back for a reversal bar (3)")
    g.add_argument("--min-depth-pct", type=float, default=8.0, help="min pullback from the high, %% (8)")
    g.add_argument("--max-depth-pct", type=float, default=40.0, help="deeper than this is a broken stock, %% (40)")
    g.add_argument("--decline-lookback", type=int, default=40, help="sessions back to find the high (40)")
    g.add_argument("--min-prior-run-pct", type=float, default=25.0,
                   help="advance into the high that counts as a prior uptrend, %% (25)")
    g.add_argument("--min-wick", type=float, default=0.5, help="lower wick as a fraction of the bar (0.5)")
    g.add_argument("--min-close-pos", type=float, default=60.0, help="close in the top part of the range, %% (60)")
    g.add_argument("--min-vol-mult", type=float, default=1.0, help="min volume vs the 50-day average (1.0)")
    g.add_argument("--quality", help="file of symbols you rate as quality (counts as context)")
    g.add_argument("--capital", type=float, help="trading capital, ₹ (sizes the entry)")
    g.add_argument("--risk-pct", type=float, default=1.0, help="risk per trade, %% of capital (1)")
    g.add_argument("--show-rejects", action="store_true", help="list near-misses and why they failed")
    fl = argparse.ArgumentParser(add_help=False)
    fl.add_argument("--fno-list", help="F&O symbols: one per line, SYMBOL[,LOT] CSV or NSE fo_mktlots.csv")
    fl.add_argument("--ban-list", help="F&O ban list (NSE fo_secban.csv or one symbol per line)")
    return u, one, cx, wo, fl


def build_parser() -> argparse.ArgumentParser:
    u, one, cx, wo, fl = _parents()
    p = argparse.ArgumentParser(description="Exhaustion and reversal reads for NSE/BSE stocks")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("climax", parents=[u, cx], help="parabolic score, stage and top confirmation")
    s.add_argument("--min-score", type=int, default=0, help="hide names scoring below this")
    s = sub.add_parser("plan", parents=[u, one, cx, fl], help="holder scale-out or F&O short plan for one stock")
    s.add_argument("--side", choices=["hold", "short", "both"], default="hold")
    g = s.add_argument_group("holder")
    g.add_argument("--qty", type=int, help="shares you hold")
    g.add_argument("--avg-price", type=float, help="your average buy price")
    g.add_argument("--sell-pct", type=float, help="share to sell into strength, %% (default by stage: 25/40/50)")
    g.add_argument("--trail", choices=["auto", "prior-low", "ema10"], default="auto")
    g = s.add_argument_group("short (F&O only)")
    g.add_argument("--fno", action="store_true", help="the stock is in the F&O segment")
    g.add_argument("--lot", type=int, help="futures lot size (shares)")
    g.add_argument("--margin-per-lot", type=float, help="₹ margin per lot from your broker")
    g.add_argument("--margin-pct", type=float, default=25.0, help="margin estimate, %% of contract value (25)")
    g.add_argument("--max-margin-pct", type=float, default=50.0, help="cap margin at this %% of capital (50)")
    g.add_argument("--capital", type=float, help="trading capital, ₹")
    g.add_argument("--risk-pct", type=float, default=1.0, help="risk per trade, %% of capital (1)")
    g.add_argument("--stop-buffer-pct", type=float, default=0.5, help="stop this far above the climax high, %% (0.5)")
    g.add_argument("--put-premium", type=float, help="premium of the put you would buy instead, ₹/share")
    g.add_argument("--put-strike", type=float, help="its strike")
    g.add_argument("--force", action="store_true", help="plan a short even when the score is below the extended zone")
    sub.add_parser("washout", parents=[u, wo], help="selling-exhaustion reversal bars for long entries")
    s = sub.add_parser("reaction", parents=[u, one], help="did price respond to the news?")
    s.add_argument("--date", required=True, help="news date YYYY-MM-DD")
    s.add_argument("--news", required=True, choices=["bullish", "bearish"])
    s.add_argument("--after-close", action="store_true", help="news came after 15:30: judge from the next session")
    s.add_argument("--sessions", type=int, default=3, help="sessions to judge, 1-3 (3)")
    s.add_argument("--crowding", choices=["long", "short", "none"], help="positioning before the news, if you know it")
    s.add_argument("--benchmark-csv", help="index CSV: judge the move net of the market")
    s.add_argument("--min-move-atr", type=float, default=0.5, help="move that counts as a response, in ATRs (0.5)")
    s.add_argument("--fail-vol-mult", type=float, default=1.5, help="volume a rejection day needs, x 50-day avg (1.5)")
    s = sub.add_parser("scan", parents=[u, cx, wo, fl], help="climax + washout over a universe, ranked")
    s.add_argument("--min-score", type=int, default=EXTENDED, help=f"min parabolic score to list ({EXTENDED})")
    s.add_argument("--top", type=int, default=10, help="rows per list (10; 0 = all)")
    return p


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    try:
        return _run(a)
    except SystemExit as e:                 # fetch_yf reports missing yfinance / bad symbols this way
        if isinstance(e.code, str):
            print(f"error: {e.code}", file=sys.stderr)
            return 2
        raise
    except (ValueError, OSError, KeyError, ZeroDivisionError, UnicodeDecodeError, csv.Error) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


DEFAULTS = {"lookback": 15, "days": 3, "min_depth_pct": 8.0, "max_depth_pct": 40.0, "decline_lookback": 40,
            "min_prior_run_pct": 25.0, "min_wick": 0.5, "min_close_pos": 60.0, "min_vol_mult": 1.0,
            "quality": None, "capital": None, "risk_pct": 1.0, "show_rejects": False, "benchmark_csv": None,
            "fno_list": None, "ban_list": None, "symbol": None, "min_score": 0, "top": 0}


def _validate(a) -> None:
    for k in ("min_turnover_cr", "min_depth_pct", "min_vol_mult", "stop_buffer_pct", "min_prior_run_pct"):
        if getattr(a, k, None) is not None and getattr(a, k) < 0:
            raise ValueError(f"--{k.replace('_', '-')} must be ≥ 0")
    for k in ("capital", "risk_pct", "tick", "avg_price", "margin_per_lot", "margin_pct", "max_margin_pct",
              "put_premium", "put_strike", "min_move_atr", "fail_vol_mult", "qty"):
        if getattr(a, k, None) is not None and getattr(a, k) <= 0:
            raise ValueError(f"--{k.replace('_', '-')} must be positive")
    if a.lookback < 3:
        raise ValueError("--lookback must be ≥ 3")
    if a.days < 1:
        raise ValueError("--days must be ≥ 1")
    if a.top < 0:
        raise ValueError("--top must be ≥ 0")
    if a.max_depth_pct <= a.min_depth_pct:
        raise ValueError("--max-depth-pct must be above --min-depth-pct")
    if not 0 < a.min_wick < 1:
        raise ValueError("--min-wick is a fraction between 0 and 1")
    if not 0 <= a.min_close_pos <= 100:
        raise ValueError("--min-close-pos is a percentage 0-100")
    if getattr(a, "sell_pct", None) is not None and not 0 <= a.sell_pct <= 100:
        raise ValueError("--sell-pct is a percentage 0-100")
    if getattr(a, "lot", None) is not None and a.lot < 2:
        raise ValueError("--lot is the futures lot size in shares (2 or more)")
    if a.cmd == "reaction" and not 1 <= a.sessions <= 3:
        raise ValueError("--sessions must be 1, 2 or 3")


def _one_stock(a) -> None:
    """Narrow the universe flags to a single stock for plan / reaction."""
    if not a.symbol:
        return
    if a.csv_dir:
        d = Path(a.csv_dir)
        if not d.is_dir():
            raise ValueError(f"--csv-dir {a.csv_dir} is not a folder")
        want = ms._norm_sym(a.symbol)
        hits = [str(f) for f in sorted(d.glob(a.pattern)) if f.is_file() and ms._norm_sym(f.stem) == want]
        if not hits:
            raise ValueError(f"--symbol {a.symbol}: no matching CSV in {a.csv_dir}")
        a.csv_dir, a.csv = None, hits[:1]
    elif not a.csv and not a.symbols:
        a.symbols = a.symbol


def _run(a) -> int:
    for k, v in DEFAULTS.items():
        if not hasattr(a, k):
            setattr(a, k, v)
    _validate(a)
    a.as_of = ms.parse_as_of(a.as_of)
    if a.cmd == "reaction":
        try:
            a.date = datetime.strptime(a.date, "%Y-%m-%d").date()
        except ValueError:
            raise ValueError(f"--date {a.date!r} is not YYYY-MM-DD")
    bands = ms.load_bands(a.bands) if a.bands else {}
    fno = load_symbol_list(a.fno_list, "--fno-list") if a.fno_list else None
    ban = set(load_symbol_list(a.ban_list, "--ban-list")) if a.ban_list else None
    a.quality = set(load_symbol_list(a.quality, "--quality")) if a.quality else set()
    if a.cmd in ("plan", "reaction"):
        _one_stock(a)
    stocks, skipped = ms.load_universe(a, bands)
    stocks = [prep(st) for st in stocks]
    if a.cmd in ("plan", "reaction") and len(stocks) != 1:
        raise ValueError(f"{a.cmd} works on one stock: give --csv FILE, or --csv-dir DIR --symbol NAME "
                         f"({len(stocks)} loaded)")
    meta = {"as_of": max(st["date"][-1] for st in stocks), "universe": [st["name"] for st in stocks],
            "skipped": skipped}

    if a.cmd == "climax":
        rows = []
        for st in stocks:
            try:
                rows.append(climax_read(st, a))
            except ValueError as e:
                skipped.append({"name": st["name"], "error": str(e)})
        if not rows:
            raise ValueError("; ".join(f"{s['name']}: {s['error']}" for s in skipped[:3]))
        rows = sorted((r for r in rows if r["score"] >= a.min_score), key=lambda r: (-r["score"], r["symbol"]))
        res = {**meta, "thresholds": {"extended": EXTENDED, "climax": CLIMAX}, "weights": WEIGHTS, "results": rows}
        if a.json:
            print(ms.dump(res))
            return 0
        if len(stocks) == 1 and rows:
            print_climax_one(rows[0])
        else:
            last = max(st["date"][-1] for st in stocks).date()
            print(f"\nParabolic scores — as of {last} | {len(stocks)} stocks | extended ≥ {EXTENDED}, climax ≥ {CLIMAX}")
            print_climax_table(rows) if rows else print("No stock at or above --min-score.")
            for r in rows:
                if r["stage"] != "not extended":
                    print(f"  • {r['symbol']}: " + max(r["factors"], key=lambda f: f["points"])["detail"]
                          + (f"; confirmed {_iso(r['confirmation']['confirmed_on'])}"
                             if r["confirmation"]["confirmed_on"] else "; not confirmed"))
        _fresh(stocks)
        _skips(skipped)
        return 0

    if a.cmd == "plan":
        cl = climax_read(stocks[0], a)
        res = {**meta, "climax": cl}
        if a.side in ("hold", "both"):
            res["hold"] = hold_plan(cl, a)
        if a.side in ("short", "both"):
            res["short"] = short_plan(cl, a, fno, ban)
        if a.json:
            print(ms.dump(res))
        else:
            print_plan(res)
            _fresh(stocks)
        return 0

    if a.cmd == "washout":
        start, hits, rejects = find_washouts(stocks, a, a.days)
        res = {**meta, "window_start": start, "washouts": hits, "rejected": rejects,
               "rules": {k: getattr(a, k) for k in ("min_depth_pct", "max_depth_pct", "min_prior_run_pct",
                                                    "min_wick", "min_close_pos", "min_vol_mult", "min_turnover_cr")}}
        if a.json:
            print(ms.dump(res))
            return 0
        last = max(st["date"][-1] for st in stocks).date()
        print(f"\nSelling-exhaustion bars — {start} to {last} | {len(stocks)} stocks | pullback "
              f"{a.min_depth_pct:g}-{a.max_depth_pct:g}%, wick ≥ {a.min_wick * 100:g}%, close ≥ {a.min_close_pos:g}% "
              f"of range, ≥ ₹{a.min_turnover_cr:g} cr/day")
        print_washouts(res, a)
        _fresh(stocks)
        _skips(skipped)
        return 0

    if a.cmd == "reaction":
        bench = None
        if a.benchmark_csv:
            bench = {r["date"].date(): r["close"] for r in ta.load_csv(a.benchmark_csv) if r["close"] > 0}
            if not bench:
                raise ValueError(f"--benchmark-csv {a.benchmark_csv}: no usable rows")
        res = {**meta, **reaction(stocks[0], a, bench)}
        print(ms.dump(res)) if a.json else print_reaction(res)
        return 0

    # scan
    res = {**meta, **run_scan(stocks, a, fno, ban),
           "rules": {"min_score": a.min_score, "min_turnover_cr": a.min_turnover_cr, "washout_days": a.days}}
    if a.json:
        print(ms.dump(res))
    else:
        last = max(st["date"][-1] for st in stocks).date()
        print(f"\nExhaustion scan — as of {last} | {len(stocks)} stocks"
              + (f" | F&O list: {len(fno)} symbols" if fno is not None else " | no --fno-list"))
        print_scan(res)
        _fresh(stocks)
        _skips(skipped + res["errors"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
