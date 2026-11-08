#!/usr/bin/env python3
"""Breakout planner for NSE/BSE cash-market stocks.

Finds Stage-2 leaders, reads the base they are building (volatility-contraction or flat base),
locates the pivot, and turns it into an order-ready plan priced in NSE ticks with Indian
charges, circuit bands and GTT/SL order templates.

Zero dependencies (Python 3.10+ stdlib). Optional: `pip install yfinance` for --symbol/--symbols.
Re-uses technical-analysis/ta.py (loaders, SMA, ATR), position-sizing/risk.py (sizing) and
trading-costs/charges.py (round-trip charges).

Sub-commands
  check   Stage-2 trend template + liquidity + base/pivot read for one stock
  plan    entry trigger, chase limit, stop, size, charges, R targets and order templates
  scan    check many stocks (a folder of CSVs or yfinance symbols) and rank them

Examples
  python3 breakout.py check --csv tatamotors.csv --benchmark-csv nifty.csv
  python3 breakout.py check --symbol POLYCAB --benchmark NIFTY
  python3 breakout.py plan --csv polycab.csv --capital 1000000 --risk-pct 1 --band 0
  python3 breakout.py plan --pivot 1520 --contraction-low 1462 --prev-close 1498 --capital 500000 --band 10
  python3 breakout.py scan --csv-dir data/ --benchmark-csv data/nifty.csv --json
  python3 breakout.py scan --symbols DIXON,KAYNES,POLYCAB,TRENT --benchmark NIFTY
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import sys
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parents[2]


def _sibling(skill: str, script: str):
    name = f"_breakout_planner_dep_{script}"
    if name in sys.modules:
        return sys.modules[name]
    path = SKILLS_DIR / skill / "scripts" / f"{script}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


ta = _sibling("technical-analysis", "ta")
risk = _sibling("position-sizing", "risk")
charges = _sibling("trading-costs", "charges")

# NSE equity tick sizes by price (circular NSE/CMTR/67133, effective 15-Apr-2025). The exchange
# re-assigns a stock's slab monthly from a reference closing price, so a stock trading right at a
# boundary can carry the neighbouring slab's tick. Use --tick to force a value (BSE may differ).
TICK_SLABS = ((250.0, 0.01), (1000.0, 0.05), (5000.0, 0.10), (10000.0, 0.50), (20000.0, 1.00),
              (math.inf, 5.00))
TICK_SOURCE = "NSE tick slabs effective 15-Apr-2025 (circular CMTR/67133)"
CRORE = 1e7
CIRCUIT_BANDS = (2.0, 5.0, 10.0, 20.0)
SURVEILLANCE = ("unknown", "none", "asm", "gsm", "t2t")


# ---------------------------------------------------------------------------
# Ticks
# ---------------------------------------------------------------------------
def tick_for(price: float) -> float:
    for limit, tick in TICK_SLABS:
        if price < limit:
            return tick
    return TICK_SLABS[-1][1]


def to_tick(price: float, tick: float | None = None, mode: str = "nearest") -> float:
    """Round a price to the tick grid. mode: up (buy triggers), down (stops/limits), nearest."""
    t = tick or tick_for(price)
    n = price / t
    if mode == "up":
        n = math.ceil(n - 1e-7)
    elif mode == "down":
        n = math.floor(n + 1e-7)
    else:
        n = round(n)
    return round(n * t, 2)


def px(x: float | None) -> str:
    return "—" if x is None else charges.inr(x, 2)


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------
def load_rows(csv_path=None, candles=None, symbol=None, exchange="NSE", period="2y"):
    if csv_path:
        rows, name = ta.load_csv(csv_path), symbol or Path(csv_path).stem
    elif candles:
        rows, name = ta.load_candles_json(candles), symbol or Path(candles).stem
    elif symbol:
        rows, name = ta.fetch_yf(symbol, exchange, period, "1d"), ta.yf_ticker(symbol, exchange)
    else:
        raise ValueError("give --csv, --candles or --symbol")
    if not rows:
        raise ValueError(f"no usable OHLC rows in {csv_path or candles or symbol}")
    return rows, name


def load_benchmark(bench_csv=None, bench_symbol=None, exchange="NSE", period="2y"):
    if bench_csv:
        return ta.load_csv(bench_csv)
    if bench_symbol:
        return ta.fetch_yf(bench_symbol, exchange, period, "1d")
    return None


# ---------------------------------------------------------------------------
# Trend template (Stage 2)
# ---------------------------------------------------------------------------
def relative_strength(rows: list, bench: list) -> dict | None:
    bmap = {r["date"].date(): r["close"] for r in bench}
    pairs = [(r["close"], bmap[r["date"].date()]) for r in rows if r["date"].date() in bmap]
    if len(pairs) < 64:
        return None
    out = {"matched_bars": len(pairs)}
    for label, k in (("3m", 63), ("6m", 126), ("12m", 252)):
        if len(pairs) > k:
            s = pairs[-1][0] / pairs[-1 - k][0] - 1
            b = pairs[-1][1] / pairs[-1 - k][1] - 1
            out[label] = {"stock_pct": s * 100, "bench_pct": b * 100, "excess_pct": (s - b) * 100}
    line = [a / b for a, b in pairs if b]
    window = line[-252:]
    out["rs_line_from_high_pct"] = (line[-1] / max(window) - 1) * 100
    return out


def trend_template(rows: list, bench: list | None = None, max_from_high: float = 25.0,
                   min_above_low: float = 30.0, slope_bars: int = 22) -> dict:
    c = [r["close"] for r in rows]
    h = [r["high"] for r in rows]
    lo = [r["low"] for r in rows]
    n = len(c)
    s50, s150, s200 = ta.sma(c, 50)[-1], ta.sma(c, 150)[-1], ta.sma(c, 200)
    s200_now = s200[-1]
    s200_then = s200[-1 - slope_bars] if n > slope_bars else None
    yr = min(n, 252)
    hi52, lo52 = max(h[-yr:]), min(lo[-yr:])
    last = c[-1]
    crit = []

    def add(key, label, ok, detail):
        crit.append({"key": key, "label": label, "pass": ok, "detail": detail})

    if None in (s50, s150, s200_now):
        add("above_mas", "Close above SMA50, SMA150 and SMA200", None, f"need 200+ bars, have {n}")
        add("ma_stack", "SMA50 > SMA150 > SMA200", None, "insufficient data")
    else:
        add("above_mas", "Close above SMA50, SMA150 and SMA200", last > s50 and last > s150 and last > s200_now,
            f"close {last:,.2f} vs {s50:,.2f} / {s150:,.2f} / {s200_now:,.2f}")
        add("ma_stack", "SMA50 > SMA150 > SMA200", s50 > s150 > s200_now,
            f"{s50:,.2f} / {s150:,.2f} / {s200_now:,.2f}")
    if s200_now is None or s200_then is None:
        add("sma200_rising", f"SMA200 rising over {slope_bars} sessions", None, f"need {200 + slope_bars}+ bars")
    else:
        add("sma200_rising", f"SMA200 rising over {slope_bars} sessions", s200_now > s200_then,
            f"{s200_then:,.2f} → {s200_now:,.2f} ({(s200_now / s200_then - 1) * 100:+.2f}%)")
    from_high = (last / hi52 - 1) * 100
    add("near_high", f"Within {max_from_high:g}% of 52-week high", from_high >= -max_from_high,
        f"52w high {hi52:,.2f}, close {from_high:+.1f}%")
    above_low = (last / lo52 - 1) * 100
    add("above_low", f"At least {min_above_low:g}% above 52-week low", above_low >= min_above_low,
        f"52w low {lo52:,.2f}, close {above_low:+.1f}%")
    rs = None
    if bench:
        rs = relative_strength(rows, bench)
        if rs is None:
            add("rs", "Outperforming benchmark (3m and 6m)", None, "fewer than 64 matching dates")
        else:
            legs = [rs[k]["excess_pct"] for k in ("3m", "6m") if k in rs]
            add("rs", "Outperforming benchmark (3m and 6m)", all(x > 0 for x in legs),
                ", ".join(f"{k} {rs[k]['excess_pct']:+.1f} pts" for k in ("3m", "6m", "12m") if k in rs)
                + f"; RS line {rs['rs_line_from_high_pct']:+.1f}% from its high")
    passed = sum(1 for x in crit if x["pass"] is True)
    unknown = sum(1 for x in crit if x["pass"] is None)
    notes = []
    if yr < 252:
        notes.append(f"52-week levels use only {yr} bars.")
    if not bench:
        notes.append("No benchmark given: relative strength not checked (add --benchmark-csv or --benchmark NIFTY).")
    return {"criteria": crit, "passed": passed, "total": len(crit), "unknown": unknown,
            "stage2": passed == len(crit), "high_52w": hi52, "low_52w": lo52,
            "sma50": s50, "sma150": s150, "sma200": s200_now, "rs": rs, "notes": notes}


def liquidity(rows: list, min_cr: float = 5.0, bars: int = 20) -> dict:
    w = rows[-bars:]
    if all(r["volume"] <= 0 for r in w):
        return {"avg_traded_value_cr": None, "min_cr": min_cr, "ok": None,
                "note": "no volume in data (index or incomplete export) — liquidity not checked"}
    avg = sum(r["close"] * r["volume"] for r in w) / len(w)
    return {"avg_traded_value_cr": avg / CRORE, "min_cr": min_cr, "ok": avg >= min_cr * CRORE,
            "bars": len(w)}


# ---------------------------------------------------------------------------
# Base detection
# ---------------------------------------------------------------------------
def zigzag(h: list, lo: list, start: int, thr_pct: float):
    """Alternating swing points beginning with the swing high at `start`.

    A low is confirmed once price rallies thr_pct off it; a high once price falls thr_pct.
    Returns (points, pending) where pending is the unconfirmed extreme still forming.
    """
    up, dn = 1 + thr_pct / 100, 1 - thr_pct / 100
    pts = [["H", start, h[start]]]
    cand = None
    for i in range(start + 1, len(h)):
        if pts[-1][0] == "H":
            if h[i] > pts[-1][2]:
                pts[-1] = ["H", i, h[i]]
                cand = None
                continue
            if cand is None or lo[i] < cand[2]:
                cand = ["L", i, lo[i]]
            if cand[1] < i and h[i] >= cand[2] * up:
                pts.append(cand)
                cand = ["H", i, h[i]]
        else:
            if lo[i] < pts[-1][2]:
                pts[-1] = ["L", i, lo[i]]
                cand = None
                continue
            if cand is None or h[i] > cand[2]:
                cand = ["H", i, h[i]]
            if cand[1] < i and lo[i] <= cand[2] * dn:
                pts.append(cand)
                cand = ["L", i, lo[i]]
    return pts, cand


def contractions(pts: list, pending, min_pending_depth: float = 1.5, pause_tol: float = 0.01) -> list:
    """Group swing points into pullbacks from a high to the lowest low before a higher low forms.

    A bounce that fails and makes a lower low stays inside the same pullback. A shallow dip
    during the rally that is followed by a clearly higher high is a pause, not a contraction.
    """
    seq = [tuple(p) for p in pts]
    in_progress = False
    if pending and pending[0] == "L" and seq[-1][0] == "H":
        if (seq[-1][2] - pending[2]) / seq[-1][2] * 100 >= min_pending_depth:
            seq.append(tuple(pending))
            in_progress = True
    out, last_high = [], seq[0]
    for k, (kind, i, p) in enumerate(seq):
        if kind == "H":
            last_high = (kind, i, p)
            continue
        if not out:
            out.append({"high_i": last_high[1], "high": last_high[2], "low_i": i, "low": p})
            continue
        cur = out[-1]
        if p <= cur["low"] and last_high[2] <= cur["high"]:
            cur["low_i"], cur["low"] = i, p
            continue
        while len(out) > 1 and out[-1]["high"] < last_high[2] * (1 - pause_tol):
            out.pop()
        out.append({"high_i": last_high[1], "high": last_high[2], "low_i": i, "low": p})
    for c in out:
        c["depth_pct"] = (c["high"] - c["low"]) / c["high"] * 100
        c["bars"] = c["low_i"] - c["high_i"]
        c["in_progress"] = False
    if out and in_progress and out[-1]["low_i"] == seq[-1][1]:
        out[-1]["in_progress"] = True
    return out


def _avg(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def detect_base(rows: list, lookback: int = 150, swing_pct: float | None = None, tighten: float = 0.85,
                max_chase_pct: float = 3.0, min_base_bars: int = 15) -> dict:
    """Find the current base: VCP (successively tighter pullbacks) or flat base, plus pivot and state."""
    h = [r["high"] for r in rows]
    lo = [r["low"] for r in rows]
    c = [r["close"] for r in rows]
    v = [r["volume"] for r in rows]
    n = len(rows)
    if n < 60:
        raise ValueError(f"only {n} bars — need 60+ for base detection (250+ for the trend template)")
    a = ta.atr(h, lo, c)[-1]
    atr_pct = a / c[-1] * 100 if a else None
    thr = swing_pct if swing_pct else min(8.0, max(3.0, 1.25 * (atr_pct or 0)))
    w0 = max(0, n - lookback)
    search_end = max(w0 + 1, n - 3)       # a breakout in the last 3 bars must not become the base start
    start = max(range(w0, search_end), key=lambda i: (h[i], i))
    base_bars = n - 1 - start
    res = {"type": "none", "grade": None, "score": None, "start_date": rows[start]["date"].date().isoformat(),
           "start_high": h[start], "bars": base_bars, "weeks": round(base_bars / 5, 1), "swing_pct": thr,
           "atr": a, "atr_pct": atr_pct, "contractions": [], "pivot": None, "last_low": None,
           "state": None, "notes": []}
    if base_bars < min_base_bars:
        res["state"] = "no base"
        res["notes"].append(f"Highest high was {base_bars} bars ago — no base yet. Wait for a 3+ week consolidation.")
        return res

    pts, pending = zigzag(h, lo, start, thr)
    cons = contractions(pts, pending)
    for k, ct in enumerate(cons):
        ct["high_date"] = rows[ct["high_i"]]["date"].date().isoformat()
        ct["low_date"] = rows[ct["low_i"]]["date"].date().isoformat()
        ct["ratio_to_prev"] = ct["depth_pct"] / cons[k - 1]["depth_pct"] if k and cons[k - 1]["depth_pct"] else None
    res["contractions"] = cons
    last = cons[-1] if cons else None
    pivot = last["high"] if last else h[start]
    # The base ends where price first closes above the pivot after the last low (if it has).
    base_end = n - 1
    if last:
        for i in range(last["low_i"] + 1, n):
            if c[i] > pivot:
                base_end = i - 1
                break
    base_low = min(lo[start:base_end + 1])
    depth = (h[start] - base_low) / h[start] * 100
    res["depth_pct"] = depth

    # volume signature: last contraction vs the 50 bars before it
    vol_ok = any(x > 0 for x in v[-60:])
    dry = None
    if vol_ok and last:
        seg = v[last["high_i"]:base_end + 1]
        ref = v[max(0, last["high_i"] - 50):last["high_i"]]
        if seg and ref and _avg(ref):
            dry = _avg(seg) / _avg(ref)
    res["volume_dryup_ratio"] = dry
    avg50 = _avg(v[-51:-1]) if vol_ok else None
    res["volume_vs_50d"] = v[-1] / avg50 if avg50 else None

    # VCP rules
    depths = [x["depth_pct"] for x in cons]
    violations = sum(1 for k in range(1, len(depths)) if depths[k] > depths[k - 1] * tighten)
    vcp_ok = (len(depths) >= 2 and depths[-1] < depths[0] and depths[-1] <= 15 and depths[0] <= 50
              and violations <= (1 if len(depths) >= 3 else 0))
    res["tightening_violations"] = violations
    flat_ok = depth <= 15 and (base_end - start) >= 25
    pts_ = 0
    if vcp_ok:
        res["type"] = "VCP"
        pts_ += 2 if 3 <= len(depths) <= 4 else 1
        pts_ += 2 if violations == 0 else 0
        pts_ += 2 if depths[-1] <= 6 else 1 if depths[-1] <= 10 else 0
        pts_ += 1 if depths[0] <= 35 else 0
        pts_ += 2 if dry is not None and dry <= 0.6 else 1 if dry is not None and dry <= 0.85 else 0
        pts_ += 1 if base_bars >= 20 else 0
        score = pts_ * 10
    elif flat_ok:
        res["type"] = "flat base"
        pivot = max(h[start:base_end + 1])
        tail = c[max(start, base_end - 9):base_end + 1]
        tight = (max(tail) - min(tail)) / max(tail) * 100 if tail else 99
        pts_ += 3 if depth <= 10 else 2
        pts_ += 2 if dry is not None and dry <= 0.6 else 1 if dry is not None and dry <= 0.85 else 0
        pts_ += 1 if base_end - start >= 35 else 0
        pts_ += 2 if tight <= 5 else 1 if tight <= 8 else 0
        score = round(pts_ * 100 / 8)
        res["notes"].append(f"Flat base {depth:.1f}% deep; last-10-close range {tight:.1f}%.")
    else:
        res["type"] = "loose/unclassified"
        score = 20 if depth <= 35 else 0
        why = []
        if len(depths) < 2:
            why.append("fewer than 2 pullbacks")
        if violations:
            why.append(f"{violations} pullback(s) not tighter than the one before")
        if depths and depths[-1] > 15:
            why.append(f"last pullback {depths[-1]:.1f}% is too wide")
        if depth > 50:
            why.append(f"base {depth:.0f}% deep (damaged)")
        res["notes"].append("Not a VCP or flat base: " + ("; ".join(why) or "irregular structure") + ".")
    res["score"] = score
    res["grade"] = "A" if score >= 80 else "B" if score >= 60 else "C" if score >= 40 else "D"
    res["pivot"] = pivot
    if res["type"] == "flat base":           # stop reference: low of the final 10 bars of the base
        res["last_low"] = min(lo[max(start, base_end - 9):base_end + 1])
    else:
        res["last_low"] = last["low"] if last else base_low
    close = c[-1]
    dist = (close / pivot - 1) * 100
    res["close"], res["from_pivot_pct"] = close, dist
    if close < res["last_low"]:
        res["state"] = "failed (undercut the last low)"
    elif dist < -15:
        res["state"] = "deep below pivot"
        res["notes"].append("More than 15% under the pivot: no setup yet, the right side of the base is unbuilt.")
    elif close < pivot:
        res["state"] = "pre-breakout"
    elif dist <= max_chase_pct:
        res["state"] = "breakout zone"
    else:
        res["state"] = "extended"
    if res["state"] in ("breakout zone", "extended") and res["volume_vs_50d"] is not None:
        if res["volume_vs_50d"] < 1.4:
            res["notes"].append(f"Above pivot on only {res['volume_vs_50d']:.1f}x average volume — weak confirmation.")
    if dry is not None and dry > 1.0:
        res["notes"].append("Volume rose in the last pullback — supply, not dry-up.")
    if not vol_ok:
        res["notes"].append("No volume in data: dry-up and breakout-volume checks skipped.")
    return res


def overnight_gap_down(rows: list, bars: int = 60) -> float:
    worst = 0.0
    for i in range(max(1, len(rows) - bars), len(rows)):
        pc = rows[i - 1]["close"]
        worst = max(worst, (pc - rows[i]["open"]) / pc * 100)
    return worst


def check(rows: list, name: str, bench: list | None = None, min_turnover_cr: float = 5.0,
          lookback: int = 150, swing_pct: float | None = None, max_chase_pct: float = 3.0) -> dict:
    tt = trend_template(rows, bench)
    liq = liquidity(rows, min_turnover_cr)
    base = detect_base(rows, lookback, swing_pct, max_chase_pct=max_chase_pct)
    score = 40 * tt["passed"] / tt["total"]
    score += {"A": 40, "B": 30, "C": 20, "D": 10}.get(base["grade"], 0)
    if base["state"] == "breakout zone" or (base["state"] == "pre-breakout" and base["from_pivot_pct"] >= -5):
        score += 20
    elif base["state"] == "pre-breakout" and base["from_pivot_pct"] >= -10:
        score += 10
    if liq["ok"] is False:
        score -= 50
    verdict = ("actionable" if tt["stage2"] and base["type"] in ("VCP", "flat base") and liq["ok"] is not False
               and base["state"] in ("pre-breakout", "breakout zone") else "watch" if tt["passed"] >= tt["total"] - 1
               and base["type"] != "none" else "skip")
    return {"name": name, "last_date": rows[-1]["date"].date().isoformat(), "bars": len(rows),
            "close": rows[-1]["close"], "freshness": ta.freshness(rows), "trend_template": tt,
            "liquidity": liq, "base": base, "gap_down_60d_pct": overnight_gap_down(rows),
            "rank_score": round(score, 1), "verdict": verdict}


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------
def build_plan(pivot: float, capital: float, risk_pct: float = 1.0, *, contraction_low: float | None = None,
               stop: float | None = None, atr: float | None = None, prev_close: float | None = None,
               last_close: float | None = None, band: float | None = None, tick: float | None = None,
               trigger_buffer_pct: float = 0.2, max_chase_pct: float = 3.0, stop_buffer_pct: float = 1.0,
               atr_mult: float = 2.0, stop_method: str = "structure", max_loss_pct: float = 8.0,
               segment: str = "delivery", leverage: float | None = None, broker: str = "zerodha",
               exchange: str = "NSE", hold_days: int = 20, gap_pct: float | None = None,
               max_alloc_pct: float = 20.0, surveillance: str = "unknown", targets_r=(2.0, 3.0)) -> dict:
    if pivot <= 0 or capital <= 0 or risk_pct <= 0:
        raise ValueError("pivot, capital and risk-pct must be positive")
    if segment not in ("delivery", "mtf"):
        raise ValueError("segment must be delivery or mtf (breakouts are positional trades)")
    if surveillance not in SURVEILLANCE:
        raise ValueError(f"surveillance must be one of {SURVEILLANCE}")
    lev = leverage if leverage else (2.0 if segment == "mtf" else 1.0)
    if segment == "delivery" and lev != 1.0:
        raise ValueError("leverage applies only to --segment mtf")
    warnings, checklist = [], []

    def tk(p, mode):
        return to_tick(p, tick, mode)

    trigger = tk(pivot * (1 + trigger_buffer_pct / 100), "up")
    limit = max(trigger, tk(pivot * (1 + max_chase_pct / 100), "down"))
    if max_chase_pct > 5:
        warnings.append(f"Max chase {max_chase_pct:g}% is above 5% — breakouts bought that far up fail more often "
                        "and the stop gets wide.")
    cands = {}
    if contraction_low:
        cands["structure"] = tk(contraction_low * (1 - stop_buffer_pct / 100), "down")
    if atr and atr > 0:
        cands["atr"] = tk(trigger - atr_mult * atr, "down")
    if stop is not None:
        chosen, method = tk(stop, "down"), "manual"
    elif stop_method in cands:
        chosen, method = cands[stop_method], stop_method
    elif cands:
        method = next(iter(cands))
        chosen = cands[method]
        warnings.append(f"No {stop_method} stop available; used the {method} stop.")
    else:
        raise ValueError("give --contraction-low, --atr or --stop (or use --csv so they are read from the chart)")
    raw_loss = (limit - chosen) / limit * 100
    capped = False
    if raw_loss > max_loss_pct:
        chosen = tk(limit * (1 - max_loss_pct / 100), "up")
        capped = True
        warnings.append(f"{method} stop was {raw_loss:.1f}% below the worst fill; capped at {max_loss_pct:g}% → "
                        f"{px(chosen)}. The stop now sits inside the base — a loose base is a reason to skip.")
    if chosen >= trigger:
        raise ValueError(f"stop {chosen:,.2f} is not below the entry trigger {trigger:,.2f}")
    if atr and limit - chosen < atr:
        warnings.append(f"Stop is closer than 1 ATR ({atr:,.2f}) — ordinary noise can hit it.")

    # Size on the worst acceptable fill (the limit), iterating costs from the real charge schedule.
    cost_pct, gap = 0.25, gap_pct if gap_pct and gap_pct > 0 else None
    for _ in range(3):
        sz = risk.position_size(capital, risk_pct, limit, chosen, leverage=lev, cost_pct=cost_pct, gap_pct=gap,
                                max_alloc_pct=max_alloc_pct)
        qty = sz["units"]
        if qty <= 0:
            break
        funded = qty * limit * (1 - 1 / lev) if segment == "mtf" else 0.0
        ch = charges.calculate(segment, limit, chosen, qty, broker, exchange,
                               mtf_days=hold_days if segment == "mtf" else 0, mtf_funded=funded)
        new = ch["total_charges"] / (qty * limit) * 100
        if abs(new - cost_pct) < 1e-4:
            break
        cost_pct = new
    sz = risk.position_size(capital, risk_pct, limit, chosen, leverage=lev, cost_pct=cost_pct, gap_pct=gap,
                            max_alloc_pct=max_alloc_pct)
    qty = sz["units"]
    if qty <= 0:
        raise ValueError(f"risk budget {risk.inr(capital * risk_pct / 100)} buys 0 shares at {limit:,.2f} with "
                         f"stop {chosen:,.2f}; raise capital or tighten the setup")
    funded = qty * limit * (1 - 1 / lev) if segment == "mtf" else 0.0
    stop_ch = charges.calculate(segment, limit, chosen, qty, broker, exchange,
                                mtf_days=hold_days if segment == "mtf" else 0, mtf_funded=funded)
    r_unit = limit - chosen
    targets = []
    for r in targets_r:
        tp = to_tick(limit + r * r_unit, tick, "nearest")
        tch = charges.calculate(segment, limit, tp, qty, broker, exchange,
                                mtf_days=hold_days if segment == "mtf" else 0, mtf_funded=funded)
        targets.append({"R": r, "price": tp, "net_pnl": tch["net_pnl"],
                        "pct_from_entry": (tp / limit - 1) * 100})

    # Circuit band from the previous close
    band_info = None
    if band is not None and prev_close:
        if band == 0:
            band_info = {"band_pct": 0, "upper": None, "lower": None}
            checklist.append("No fixed circuit (dynamic price band, typical of F&O stocks): the exchange widens "
                             "the band in steps, so a breakout can run — and gap — further than in banded stocks.")
        else:
            upper = to_tick(prev_close * (1 + band / 100), tick, "down")
            lower = to_tick(prev_close * (1 - band / 100), tick, "up")
            band_info = {"band_pct": band, "upper": upper, "lower": lower}
            if trigger > upper:
                warnings.append(f"Trigger {px(trigger)} is above today's upper circuit {px(upper)} ({band:g}% band "
                                f"from {px(prev_close)}): it cannot trigger this session. Re-check the band next day.")
            elif limit > upper:
                warnings.append(f"Chase limit {px(limit)} is above the upper circuit {px(upper)}: orders beyond the "
                                f"band are rejected. Cap the limit at {px(upper)}; at the circuit you may sit in a "
                                "buy queue with no sellers.")
            if band <= 5:
                warnings.append(f"{band:g}% band: usually a surveillance, SME or thinly traded stock. Breakouts "
                                "tend to lock at upper circuit (no fill) and lower circuit (no exit). Avoid or size down.")
            if chosen < lower:
                checklist.append(f"Stop {px(chosen)} is below today's lower circuit {px(lower)}: a one-day exit "
                                 "at your stop is not guaranteed if the stock locks lower.")
    elif band is not None:
        warnings.append("--band given without --prev-close (or data): band check skipped.")
    if last_close is not None:
        if last_close > limit:
            warnings.append(f"Last close {px(last_close)} is already above the chase limit {px(limit)}: missed — "
                            "wait for a new base or an orderly pullback toward the pivot.")
        elif last_close >= trigger:
            warnings.append(f"Last close {px(last_close)} is already inside the buy zone ({px(trigger)}–{px(limit)}): "
                            "a GTT would fire at once — use a plain limit buy at or below the chase limit instead.")
        elif contraction_low and last_close < contraction_low:
            warnings.append("Last close is below the last contraction low: the base has failed.")

    if surveillance in ("asm", "gsm", "t2t"):
        warnings.append({"asm": "ASM stock: higher/100% upfront margin, often trade-for-trade at later stages; "
                                "MTF usually unavailable. Size down or skip.",
                         "gsm": "GSM stock: graded surveillance (trade-for-trade, restricted trading at higher "
                                "stages). Skip breakouts here.",
                         "t2t": "Trade-for-trade (T2T/BE series): every trade is delivery, no intraday or BTST, full "
                                "payment upfront, no MTF."}[surveillance])
        if segment == "mtf":
            warnings.append("MTF funding is generally not available for surveillance stocks — check your broker's list.")
    elif surveillance == "unknown":
        checklist.append("Check NSE ASM/GSM lists and the series (EQ vs BE/T2T, SME): surveillance stocks need 100% "
                         "upfront margin, no intraday/BTST, often no MTF.")

    tick_note = (f"tick {tick:g} (manual)" if tick else
                 f"tick {tick_for(trigger):g} at {px(trigger)} — {TICK_SOURCE}")
    orders = {
        "gtt_entry": (f"GTT single, BUY {qty} CNC{' (MTF)' if segment == 'mtf' else ''}: trigger {px(trigger)}, "
                      f"limit {px(limit)}. Fires on LTP and sends a limit order; a gap above {px(limit)} goes "
                      "unfilled — that is the chase rule working."),
        "sl_entry_day": (f"Alternative (one session only): SL BUY {qty} trigger {px(trigger)} limit {px(limit)}, "
                         "validity DAY. Exchange stop orders expire at 15:30 — re-place daily or use the GTT."),
        "gtt_exit": (f"After the fill, GTT OCO SELL {qty}: stop trigger {px(chosen)} limit "
                     f"{px(to_tick(chosen * 0.995, tick, 'down'))} | target trigger {px(targets[-1]['price'])} "
                     f"limit {px(targets[-1]['price'])}."),
        "day0": ("Shares settle T+1. Until they show in holdings, some brokers won't accept a sell GTT: place a DAY "
                 f"SL SELL (trigger {px(chosen)}) on the buy day, then the OCO from the next morning."),
    }
    checklist += [
        "Market regime allows new longs (see market-regime); skip breakouts in a falling NIFTY / weak breadth tape.",
        "Breakout-day volume ≥1.5x 50-day average, and delivery % at or above its recent average.",
        "No quarterly results, AGM/record date or block deal overhang within ~5 sessions (results gaps of "
        "5–15% jump any stop).",
        "Liquidity: ≥₹5 crore average daily traded value; avoid SME/operator-driven names.",
        "Pre-open (09:00–09:15) accepts no stop orders; a gap through the pivot fills only if it opens below the limit.",
    ]
    return {"pivot": pivot, "trigger": trigger, "limit": limit, "max_chase_pct": max_chase_pct,
            "stop": chosen, "stop_method": method, "stop_candidates": cands, "stop_capped": capped,
            "raw_stop_loss_pct": raw_loss, "loss_pct_from_limit": (limit - chosen) / limit * 100,
            "loss_pct_from_trigger": (trigger - chosen) / trigger * 100, "R_per_share": r_unit,
            "qty": qty, "position_value": qty * limit, "allocation_pct": qty * limit / capital * 100,
            "capital_needed": sz["capital_needed"], "risk_budget": capital * risk_pct / 100,
            "risk_if_stopped": sz["risk_amount"], "cost_pct_used": cost_pct,
            "charges_stop_out": stop_ch["total_charges"], "net_loss_at_stop": stop_ch["net_pnl"],
            "segment": segment, "leverage": lev, "broker": broker, "targets": targets, "band": band_info,
            "tick_note": tick_note, "sizing_notes": sz["notes"], "orders": orders, "warnings": warnings,
            "checklist": checklist}


# ---------------------------------------------------------------------------
# Printing
# ---------------------------------------------------------------------------
def mark(ok):
    return "x" if ok is True else " " if ok is False else "?"


def print_check(r: dict) -> None:
    print(f"\n{r['name']} — last bar {r['last_date']} ({r['bars']} bars), close {px(r['close'])}")
    if r["freshness"]:
        print(r["freshness"])
    tt = r["trend_template"]
    print(f"Trend template: {tt['passed']}/{tt['total']} → {'STAGE 2' if tt['stage2'] else 'not Stage 2'}")
    for c in tt["criteria"]:
        print(f"  [{mark(c['pass'])}] {c['label']:<40} {c['detail']}")
    for n in tt["notes"]:
        print("  •", n)
    lq = r["liquidity"]
    if lq["ok"] is None:
        print(f"Liquidity: {lq['note']}")
    else:
        print(f"Liquidity: ₹{lq['avg_traded_value_cr']:,.2f} cr/day avg traded value (20d) — "
              f"{'OK' if lq['ok'] else 'BELOW MINIMUM'} (min ₹{lq['min_cr']:g} cr)")
    b = r["base"]
    print(f"\nBase: {b['type']}" + (f", grade {b['grade']} ({b['score']}/100)" if b["grade"] else "")
          + f", {b['bars']} bars (~{b['weeks']} weeks) from the {px(b['start_high'])} high on {b['start_date']}")
    for k, c in enumerate(b["contractions"], 1):
        ratio = f", {c['ratio_to_prev']:.2f}x prev" if c["ratio_to_prev"] else ""
        prog = " (in progress)" if c["in_progress"] else ""
        print(f"  T{k}: {c['depth_pct']:.1f}%  {px(c['high'])} ({c['high_date']}) → {px(c['low'])} "
              f"({c['low_date']}), {c['bars']} bars{ratio}{prog}")
    if b.get("volume_dryup_ratio") is not None:
        print(f"  Volume in last contraction: {b['volume_dryup_ratio']:.2f}x the prior 50 bars")
    if b["pivot"]:
        print(f"  Pivot {px(b['pivot'])} | last low {px(b['last_low'])} | close {b['from_pivot_pct']:+.1f}% "
              f"vs pivot → {b['state'].upper()}")
    else:
        print(f"  State: {b['state']}")
    for n in b["notes"]:
        print("  •", n)
    print(f"Worst overnight gap-down (60 bars): {r['gap_down_60d_pct']:.1f}%")
    print(f"\nVerdict: {r['verdict'].upper()} (rank score {r['rank_score']})")


def print_plan(p: dict, header: str | None = None) -> None:
    if header:
        print(header)
    print(f"\nBreakout plan — pivot {px(p['pivot'])}, {p['segment']}"
          + (f" at {p['leverage']:g}x" if p["segment"] == "mtf" else ""))
    print(f"  Entry trigger  : {px(p['trigger'])}   (pivot + buffer, rounded up to tick)")
    print(f"  Chase limit    : {px(p['limit'])}   (≤{p['max_chase_pct']:g}% over pivot; size uses this worst fill)")
    print(f"  Stop           : {px(p['stop'])}   ({p['stop_method']}{', CAPPED' if p['stop_capped'] else ''}; "
          f"{p['loss_pct_from_limit']:.2f}% below limit, {p['loss_pct_from_trigger']:.2f}% below trigger)")
    if p["stop_candidates"]:
        print("  Stop candidates: " + " | ".join(f"{k} {px(v)}" for k, v in p["stop_candidates"].items()))
    print(f"  Quantity       : {p['qty']} shares = {risk.inr(p['position_value'])} "
          f"({p['allocation_pct']:.1f}% of capital"
          + (f"; own funds {risk.inr(p['capital_needed'])}" if p["segment"] == "mtf" else "") + ")")
    print(f"  Risk if stopped: {risk.inr(p['risk_if_stopped'])} of {risk.inr(p['risk_budget'])} budget "
          f"(charges on a stop-out ≈ {charges.inr(p['charges_stop_out'])})")
    print("  Targets        : " + " | ".join(
        f"{t['R']:g}R {px(t['price'])} ({t['pct_from_entry']:+.1f}%, net {risk.inr(t['net_pnl'])})" for t in p["targets"]))
    if p["band"] and p["band"]["upper"]:
        print(f"  Circuit band   : {p['band']['band_pct']:g}% → {px(p['band']['lower'])} – {px(p['band']['upper'])}")
    print(f"  Tick           : {p['tick_note']}")
    print("\nOrders:")
    for k in ("gtt_entry", "sl_entry_day", "gtt_exit", "day0"):
        print("  •", p["orders"][k])
    if p["warnings"] or p["sizing_notes"]:
        print("\nWarnings:")
        for w in p["warnings"] + p["sizing_notes"]:
            print("  ⚠", w)
    print("\nChecklist:")
    for c in p["checklist"]:
        print("  [ ]", c)


def print_scan(results: list, errors: list) -> None:
    print(f"\n{'#':>2} {'Symbol':<22}{'Close':>11} {'TT':>5} {'Base':<19}{'Gr':>3} {'Pivot':>11} {'vs piv':>7} "
          f"{'₹cr/d':>7}  {'State':<16}{'Score':>6}  Verdict")
    for k, r in enumerate(results, 1):
        b, tt, lq = r["base"], r["trend_template"], r["liquidity"]
        cr = "—" if lq["avg_traded_value_cr"] is None else f"{lq['avg_traded_value_cr']:.1f}"
        piv = f"{b['pivot']:,.2f}" if b["pivot"] else "—"
        dist = f"{b['from_pivot_pct']:+.1f}%" if b["pivot"] else "—"
        print(f"{k:>2} {r['name'][:21]:<22}{r['close']:>11,.2f} {tt['passed']}/{tt['total']:<3} {b['type'][:18]:<19}"
              f"{b['grade'] or '—':>3} {piv:>11} {dist:>7} {cr:>7}  {(b['state'] or '')[:15]:<16}"
              f"{r['rank_score']:>6}  {r['verdict']}" + ("  (illiquid)" if lq["ok"] is False else ""))
    for e in errors:
        print(f"   skipped {e['name']}: {e['error']}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _common(p, data=True):
    if data:
        p.add_argument("--csv")
        p.add_argument("--candles", help="broker-MCP candle JSON")
        p.add_argument("--symbol", help="name, or fetch via yfinance when no file is given")
    p.add_argument("--exchange", default="NSE", choices=["NSE", "BSE"])
    p.add_argument("--period", default="2y", help="yfinance history period")
    p.add_argument("--benchmark-csv", help="NIFTY/sector index CSV for relative strength")
    p.add_argument("--benchmark", help="benchmark via yfinance, e.g. NIFTY, NIFTYIT")
    p.add_argument("--min-turnover-cr", type=float, default=5.0, help="min avg daily traded value, ₹ crore")
    p.add_argument("--lookback", type=int, default=150, help="bars searched for the base start")
    p.add_argument("--swing-pct", type=float, help="swing threshold %% (default: max(3, 1.25×ATR%%), cap 8)")
    p.add_argument("--max-chase-pct", type=float, default=3.0, help="max %% above pivot you will pay")
    p.add_argument("--json", action="store_true")


def _num_or_auto(x: str):
    if x.lower() == "auto":
        return None
    v = float(x)
    if v <= 0:
        raise argparse.ArgumentTypeError("tick must be positive")
    return v


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Stage-2 breakout planner for NSE/BSE stocks")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="trend template + base + pivot for one stock")
    _common(c)
    pl = sub.add_parser("plan", help="order-ready breakout plan")
    _common(pl)
    pl.add_argument("--capital", type=float, required=True)
    pl.add_argument("--risk-pct", type=float, default=1.0)
    pl.add_argument("--pivot", type=float, help="override/give the pivot (no data needed)")
    pl.add_argument("--contraction-low", type=float, help="low of the last contraction")
    pl.add_argument("--stop", type=float, help="manual stop (still capped by --max-loss-pct)")
    pl.add_argument("--atr", type=float)
    pl.add_argument("--atr-mult", type=float, default=2.0)
    pl.add_argument("--stop-method", choices=["structure", "atr"], default="structure")
    pl.add_argument("--stop-buffer-pct", type=float, default=1.0)
    pl.add_argument("--max-loss-pct", type=float, default=8.0)
    pl.add_argument("--trigger-buffer-pct", type=float, default=0.2)
    pl.add_argument("--prev-close", type=float, help="previous close for the circuit-band check")
    pl.add_argument("--band", type=float, help="price band %%: 2, 5, 10, 20, or 0 for no fixed band (F&O)")
    pl.add_argument("--tick", type=_num_or_auto, default=None, help="tick size or 'auto' (NSE slabs)")
    pl.add_argument("--segment", choices=["delivery", "mtf"], default="delivery")
    pl.add_argument("--leverage", type=float, help="MTF leverage (default 2 for mtf)")
    pl.add_argument("--hold-days", type=int, default=20, help="expected holding days (MTF interest)")
    pl.add_argument("--broker", default="zerodha", choices=list(charges.BROKERS))
    pl.add_argument("--gap-pct", type=float, help="gap-through-stop %% (default: worst 60-bar gap-down from data)")
    pl.add_argument("--max-alloc-pct", type=float, default=20.0)
    pl.add_argument("--surveillance", choices=SURVEILLANCE, default="unknown")
    pl.add_argument("--targets", default="2,3", help="R multiples, e.g. 2,3,5")
    sc = sub.add_parser("scan", help="rank many stocks")
    _common(sc, data=False)
    sc.add_argument("--csv-dir", help="folder with one OHLCV CSV per symbol (file name = symbol)")
    sc.add_argument("--pattern", default="*.csv", help="glob inside --csv-dir")
    sc.add_argument("--symbols", help="comma list fetched via yfinance")
    sc.add_argument("--top", type=int, default=0, help="show only the top N")
    a = p.parse_args(argv)

    try:
        return _run(a)
    except SystemExit as e:              # fetch_yf reports missing yfinance / bad symbols this way
        if isinstance(e.code, str):
            print(f"error: {e.code}", file=sys.stderr)
            return 2
        raise
    except (ValueError, OSError, KeyError, ZeroDivisionError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


def _run(a) -> int:
    if a.max_chase_pct < 0:
        raise ValueError("--max-chase-pct must be ≥ 0")
    bench = load_benchmark(a.benchmark_csv, a.benchmark, a.exchange, a.period)

    if a.cmd == "check":
        rows, name = load_rows(a.csv, a.candles, a.symbol, a.exchange, a.period)
        res = check(rows, name, bench, a.min_turnover_cr, a.lookback, a.swing_pct, a.max_chase_pct)
        print(json.dumps(res, indent=2, default=str)) if a.json else print_check(res)
        return 0

    if a.cmd == "plan":
        if a.band is not None and a.band not in (0.0,) + CIRCUIT_BANDS:
            raise ValueError("--band must be 0, 2, 5, 10 or 20")
        try:
            targets = tuple(float(x) for x in a.targets.split(",") if x.strip())
        except ValueError:
            raise ValueError("--targets takes R multiples like 2,3,5")
        if not targets or any(t <= 0 for t in targets):
            raise ValueError("--targets must be positive R multiples")
        rep, last_close, gap = None, None, a.gap_pct
        pivot, low, atr, prev = a.pivot, a.contraction_low, a.atr, a.prev_close
        if a.csv or a.candles or (a.symbol and a.pivot is None):
            rows, name = load_rows(a.csv, a.candles, a.symbol, a.exchange, a.period)
            rep = check(rows, name, bench, a.min_turnover_cr, a.lookback, a.swing_pct, a.max_chase_pct)
            b = rep["base"]
            pivot = pivot or b["pivot"]
            low = low or b["last_low"]
            atr = atr or b["atr"]
            prev = prev or rows[-1]["close"]
            last_close = rows[-1]["close"]
            gap = gap if gap is not None else rep["gap_down_60d_pct"]
            if pivot is None:
                raise ValueError(f"no base found ({b['state']}); pass --pivot to plan anyway")
        if pivot is None:
            raise ValueError("give --pivot (with --contraction-low/--atr/--stop) or --csv/--candles/--symbol")
        plan = build_plan(pivot, a.capital, a.risk_pct, contraction_low=low, stop=a.stop, atr=atr,
                          prev_close=prev, last_close=last_close, band=a.band, tick=a.tick,
                          trigger_buffer_pct=a.trigger_buffer_pct, max_chase_pct=a.max_chase_pct,
                          stop_buffer_pct=a.stop_buffer_pct, atr_mult=a.atr_mult, stop_method=a.stop_method,
                          max_loss_pct=a.max_loss_pct, segment=a.segment, leverage=a.leverage, broker=a.broker,
                          exchange=a.exchange, hold_days=a.hold_days, gap_pct=gap,
                          max_alloc_pct=a.max_alloc_pct, surveillance=a.surveillance, targets_r=targets)
        if rep:
            if not rep["trend_template"]["stage2"]:
                plan["warnings"].insert(0, f"Trend template {rep['trend_template']['passed']}/"
                                           f"{rep['trend_template']['total']}: not a Stage-2 stock — skip or paper-trade.")
            b = rep["base"]
            if b["type"] not in ("VCP", "flat base") or b["state"] not in ("pre-breakout", "breakout zone"):
                plan["warnings"].insert(0, f"Base is {b['type']} / {b['state']}: no actionable setup on this chart.")
            if rep["liquidity"]["ok"] is False:
                plan["warnings"].insert(0, f"Avg traded value ₹{rep['liquidity']['avg_traded_value_cr']:.2f} cr/day "
                                           f"is below ₹{a.min_turnover_cr:g} cr — slippage and exit risk.")
            if a.band is None:
                plan["checklist"].insert(0, "Band unknown: pass --band (2/5/10/20, or 0 for F&O stocks) to check the "
                                            "upper circuit against the chase limit.")
        if a.json:
            print(json.dumps({"check": rep, "plan": plan}, indent=2, default=str))
        else:
            if rep:
                b = rep["base"]
                print(f"\n{rep['name']} — {rep['last_date']}, close {px(rep['close'])} | trend "
                      f"{rep['trend_template']['passed']}/{rep['trend_template']['total']} | base {b['type']} "
                      f"{b['grade'] or ''} | {b['state']}")
                if rep["freshness"]:
                    print(rep["freshness"])
            print_plan(plan)
        return 0

    # scan
    jobs = []
    if a.csv_dir:
        d = Path(a.csv_dir)
        if not d.is_dir():
            raise ValueError(f"--csv-dir {a.csv_dir} is not a folder")
        jobs = [("csv", f) for f in sorted(d.glob(a.pattern)) if f.is_file()]
        if bench is not None and a.benchmark_csv:
            jobs = [j for j in jobs if j[1].resolve() != Path(a.benchmark_csv).resolve()]
    elif a.symbols:
        jobs = [("yf", s.strip()) for s in a.symbols.split(",") if s.strip()]
    else:
        raise ValueError("give --csv-dir DIR or --symbols A,B,C")
    if not jobs:
        raise ValueError("nothing to scan (no matching files or symbols)")
    results, errors = [], []
    for kind, item in jobs:
        label = item.stem if kind == "csv" else item.upper()
        try:
            rows = ta.load_csv(str(item)) if kind == "csv" else ta.fetch_yf(item, a.exchange, a.period, "1d")
            if not rows:
                raise ValueError("no usable rows")
            results.append(check(rows, label, bench, a.min_turnover_cr, a.lookback, a.swing_pct, a.max_chase_pct))
        except SystemExit as e:
            if not isinstance(e.code, str):
                raise
            errors.append({"name": label, "error": e.code})
        except (ValueError, OSError, KeyError, ZeroDivisionError) as e:
            errors.append({"name": label, "error": str(e)})
    order = {"actionable": 0, "watch": 1, "skip": 2}
    results.sort(key=lambda r: (order[r["verdict"]], -r["rank_score"]))
    if a.top:
        results = results[:a.top]
    if a.json:
        print(json.dumps({"results": results, "errors": errors}, indent=2, default=str))
    else:
        print_scan(results, errors)
    return 0 if results else 2


if __name__ == "__main__":
    sys.exit(main())
