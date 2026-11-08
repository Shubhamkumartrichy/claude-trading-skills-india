#!/usr/bin/env python3
"""Indian F&O toolkit: Greeks, IV, expected move, option-chain analytics, expiry calendar,
and multi-leg strategy payoffs for NSE/BSE index and stock derivatives.

Zero dependencies (Python 3.10+ stdlib only). Index and stock options in India are
European-style and cash/physically settled at expiry; Black-Scholes(-Merton) applies.

Sub-commands
------------
  greeks    Price + Greeks for one option          options.py greeks --spot 25000 --strike 25200 --type CE --iv 13 --days 6
  iv        Implied volatility from a premium      options.py iv --spot 25000 --strike 25200 --type CE --premium 95 --days 6
  move      Expected move from IV or India VIX     options.py move --spot 25000 --iv 13.5 --days 7
  chain     PCR, max pain, OI walls, straddle      options.py chain chain.csv --spot 25010
  buildup   Classify price/OI change               options.py buildup --price-change 1.2 --oi-change 8
  expiries  Upcoming expiries (holiday-adjusted)   options.py expiries NIFTY --count 6
  payoff    Multi-leg payoff, breakevens, POP      options.py payoff --underlying NIFTY --spot 25000 \
                                                       --leg S,CE,25500,60 --leg B,CE,25700,25 --iv 13 --days 6
  lots      Current lot sizes                      options.py lots

Lot sizes and holidays change (lot sizes are reviewed by exchanges periodically; holidays
can be added at short notice, e.g. 15 Jan 2026). DATA_AS_OF says when they were verified.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from datetime import date, datetime, timedelta, timezone

DATA_AS_OF = "2026-10-31"
IST = timezone(timedelta(hours=5, minutes=30))

# Index derivative lot sizes (NSE revision effective Dec-2025/Jan-2026 series; BSE as listed).
LOT_SIZES = {
    "NIFTY": 65, "BANKNIFTY": 30, "FINNIFTY": 60, "MIDCPNIFTY": 120, "NIFTYNXT50": 25,
    "SENSEX": 20, "BANKEX": 30,
}

# Weekday trading holidays for NSE/BSE equity & equity-derivatives segments, 2026.
HOLIDAYS = {
    date(2026, 1, 15),   # Maharashtra municipal elections (declared Jan 12)
    date(2026, 1, 26), date(2026, 3, 3), date(2026, 3, 26), date(2026, 3, 31),
    date(2026, 4, 3), date(2026, 4, 14), date(2026, 5, 1), date(2026, 5, 28),
    date(2026, 6, 26), date(2026, 9, 14), date(2026, 10, 2), date(2026, 10, 20),
    date(2026, 11, 10), date(2026, 11, 24), date(2026, 12, 25),
}
HOLIDAYS_KNOWN_UNTIL = date(2026, 12, 31)
# Fixed-date closures every year (used beyond HOLIDAYS_KNOWN_UNTIL; festival dates still unknown).
FIXED_ANNUAL_HOLIDAYS = {(1, 26), (5, 1), (8, 15), (10, 2), (12, 25)}

TUE, THU = 1, 3
# (exchange, weekly?, weekday). Since 1 Sep 2025: NSE expiries on Tuesday, BSE on Thursday.
# SEBI allows one weekly index expiry per exchange: NIFTY (NSE) and SENSEX (BSE).
EXPIRY_RULES = {
    "NIFTY": ("NSE", True, TUE), "BANKNIFTY": ("NSE", False, TUE), "FINNIFTY": ("NSE", False, TUE),
    "MIDCPNIFTY": ("NSE", False, TUE), "NIFTYNXT50": ("NSE", False, TUE), "STOCK": ("NSE", False, TUE),
    "SENSEX": ("BSE", True, THU), "BANKEX": ("BSE", False, THU), "SENSEX50": ("BSE", False, THU),
}

# ---------------------------------------------------------------------------
# Black-Scholes-Merton
# ---------------------------------------------------------------------------
SQRT2PI = math.sqrt(2 * math.pi)


def _N(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _n(x: float) -> float:
    return math.exp(-0.5 * x * x) / SQRT2PI


def bs(spot: float, strike: float, t: float, iv: float, r: float = 0.06, q: float = 0.0,
       kind: str = "CE") -> dict:
    """Price and Greeks. t in years, iv/r/q as decimals. theta per calendar day, vega/rho per 1%."""
    kind = kind.upper()
    if kind not in ("CE", "PE"):
        raise ValueError("kind must be CE or PE")
    if t <= 0 or iv <= 0:
        intrinsic = max(0.0, spot - strike) if kind == "CE" else max(0.0, strike - spot)
        itm = intrinsic > 0
        return {"price": intrinsic, "delta": (1.0 if kind == "CE" else -1.0) if itm else 0.0,
                "gamma": 0.0, "theta": 0.0, "vega": 0.0, "rho": 0.0}
    st = iv * math.sqrt(t)
    d1 = (math.log(spot / strike) + (r - q + 0.5 * iv * iv) * t) / st
    d2 = d1 - st
    dq, dr = math.exp(-q * t), math.exp(-r * t)
    gamma = dq * _n(d1) / (spot * st)
    vega = spot * dq * _n(d1) * math.sqrt(t) / 100
    common = -spot * dq * _n(d1) * iv / (2 * math.sqrt(t))
    if kind == "CE":
        price = spot * dq * _N(d1) - strike * dr * _N(d2)
        delta = dq * _N(d1)
        theta = (common - r * strike * dr * _N(d2) + q * spot * dq * _N(d1)) / 365
        rho = strike * t * dr * _N(d2) / 100
    else:
        price = strike * dr * _N(-d2) - spot * dq * _N(-d1)
        delta = -dq * _N(-d1)
        theta = (common + r * strike * dr * _N(-d2) - q * spot * dq * _N(-d1)) / 365
        rho = -strike * t * dr * _N(-d2) / 100
    return {"price": price, "delta": delta, "gamma": gamma, "theta": theta, "vega": vega, "rho": rho,
            "d1": d1, "d2": d2}


def implied_vol(premium: float, spot: float, strike: float, t: float, r: float = 0.06,
                q: float = 0.0, kind: str = "CE") -> float | None:
    """Bisection IV solver. Returns decimal IV, or None if premium is below intrinsic/out of range."""
    lo, hi = 1e-4, 5.0
    if premium < bs(spot, strike, t, lo, r, q, kind)["price"] - 1e-9:
        return None
    if premium > bs(spot, strike, t, hi, r, q, kind)["price"]:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        if bs(spot, strike, t, mid, r, q, kind)["price"] < premium:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def years_to_expiry(expiry: date, now: datetime | None = None) -> float:
    """Calendar time from `now` (IST) to 15:30 IST on expiry day, in years (min 1 minute)."""
    now = now or datetime.now(IST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)
    close = datetime(expiry.year, expiry.month, expiry.day, 15, 30, tzinfo=IST)
    minutes = max(1.0, (close - now).total_seconds() / 60)
    return minutes / (365 * 24 * 60)


def expected_move(spot: float, iv_pct: float, days: float) -> dict:
    """1-sigma move. India VIX is annualised on a calendar-day (365) basis."""
    sigma = iv_pct / 100 * math.sqrt(days / 365)
    one = spot * sigma
    return {"one_sigma_points": one, "one_sigma_pct": sigma * 100,
            "range_68": (spot - one, spot + one), "range_95": (spot - 2 * one, spot + 2 * one)}


# ---------------------------------------------------------------------------
# Option chain
# ---------------------------------------------------------------------------
def _num(v) -> float:
    if v is None:
        return 0.0
    s = str(v).strip().replace(",", "")
    if s in ("", "-", "--", "NA", "nan"):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


SIMPLE_COLS = {
    "strike": ("strike", "strike_price", "strikeprice"),
    "call_oi": ("call_oi", "ce_oi", "calls_oi"), "put_oi": ("put_oi", "pe_oi", "puts_oi"),
    "call_chg_oi": ("call_chg_oi", "ce_chg_oi", "call_change_oi", "ce_change_oi"),
    "put_chg_oi": ("put_chg_oi", "pe_chg_oi", "put_change_oi", "pe_change_oi"),
    "call_volume": ("call_volume", "ce_volume"), "put_volume": ("put_volume", "pe_volume"),
    "call_ltp": ("call_ltp", "ce_ltp"), "put_ltp": ("put_ltp", "pe_ltp"),
    "call_iv": ("call_iv", "ce_iv"), "put_iv": ("put_iv", "pe_iv"),
}
NSE_FIELD = {"OI": "oi", "CHNG IN OI": "chg_oi", "VOLUME": "volume", "IV": "iv", "LTP": "ltp"}


def load_chain(path: str) -> list:
    """Accepts a simple CSV (strike, call_oi, put_oi, ...) or NSE's option-chain CSV download,
    whose columns are mirrored around STRIKE (calls on the left, puts on the right)."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    strike_names = {"strike", "strike_price", "strikeprice"}
    norm_cell = lambda c: c.strip().lower().replace(" ", "_")
    header_idx = next((i for i, r in enumerate(rows) if any(norm_cell(c) in strike_names for c in r)), None)
    if header_idx is None:
        raise ValueError("no STRIKE column found")
    header = [c.strip() for c in rows[header_idx]]
    upper = [c.upper() for c in header]
    norm = [c.lower().replace(" ", "_") for c in header]
    out = []
    simple = any(n in sum(SIMPLE_COLS.values(), ()) and n != "strike" for n in norm)
    if simple:
        idx = {}
        for key, names in SIMPLE_COLS.items():
            for n in names:
                if n in norm:
                    idx[key] = norm.index(n)
                    break
        for r in rows[header_idx + 1:]:
            if len(r) <= idx["strike"] or not _num(r[idx["strike"]]):
                continue
            out.append({k: _num(r[i]) if i < len(r) else 0.0 for k, i in idx.items()})
    else:
        s_idx = next(i for i, c in enumerate(norm) if c in strike_names)
        for r in rows[header_idx + 1:]:
            if len(r) <= s_idx or not _num(r[s_idx]):
                continue
            row = {"strike": _num(r[s_idx])}
            for i, c in enumerate(upper):
                if c in NSE_FIELD and i < len(r):
                    side = "call" if i < s_idx else "put"
                    row[f"{side}_{NSE_FIELD[c]}"] = _num(r[i])
            out.append(row)
    out.sort(key=lambda x: x["strike"])
    if not out:
        raise ValueError("option chain has no strike rows")
    return out


def max_pain(chain: list) -> float:
    """Strike at which option writers' total payout (buyers' intrinsic value) is minimal."""
    strikes = [c["strike"] for c in chain]
    best, best_pain = None, None
    for s in strikes:
        pain = sum(c.get("call_oi", 0) * max(0.0, s - c["strike"]) +
                   c.get("put_oi", 0) * max(0.0, c["strike"] - s) for c in chain)
        if best_pain is None or pain < best_pain:
            best, best_pain = s, pain
    return best


def analyse_chain(chain: list, spot: float | None = None, top: int = 3) -> dict:
    call_oi = sum(c.get("call_oi", 0) for c in chain)
    put_oi = sum(c.get("put_oi", 0) for c in chain)
    call_chg = sum(c.get("call_chg_oi", 0) for c in chain)
    put_chg = sum(c.get("put_chg_oi", 0) for c in chain)
    res = {
        "pcr_oi": put_oi / call_oi if call_oi else None,
        "pcr_change_oi": (put_chg / call_chg) if call_chg > 0 else None,
        "max_pain": max_pain(chain),
        "call_walls": [(c["strike"], c.get("call_oi", 0)) for c in
                       sorted(chain, key=lambda c: -c.get("call_oi", 0))[:top]],
        "put_walls": [(c["strike"], c.get("put_oi", 0)) for c in
                      sorted(chain, key=lambda c: -c.get("put_oi", 0))[:top]],
        "fresh_call_writing": [(c["strike"], c.get("call_chg_oi", 0)) for c in
                               sorted(chain, key=lambda c: -c.get("call_chg_oi", 0))[:top]
                               if c.get("call_chg_oi", 0) > 0],
        "fresh_put_writing": [(c["strike"], c.get("put_chg_oi", 0)) for c in
                              sorted(chain, key=lambda c: -c.get("put_chg_oi", 0))[:top]
                              if c.get("put_chg_oi", 0) > 0],
    }
    if spot:
        atm = min(chain, key=lambda c: abs(c["strike"] - spot))
        res["atm_strike"] = atm["strike"]
        ce, pe = atm.get("call_ltp", 0), atm.get("put_ltp", 0)
        if ce and pe:
            straddle = ce + pe
            res["atm_straddle"] = straddle
            res["straddle_implied_range"] = (atm["strike"] - straddle, atm["strike"] + straddle)
        if atm.get("call_iv") and atm.get("put_iv"):
            res["atm_iv"] = (atm["call_iv"] + atm["put_iv"]) / 2
        # 25-delta-ish skew proxy: ~3% OTM put IV vs ~3% OTM call IV
        otm_put = min(chain, key=lambda c: abs(c["strike"] - spot * 0.97))
        otm_call = min(chain, key=lambda c: abs(c["strike"] - spot * 1.03))
        if otm_put.get("put_iv") and otm_call.get("call_iv"):
            res["skew_put_minus_call_iv"] = otm_put["put_iv"] - otm_call["call_iv"]
    pcr = res["pcr_oi"]
    if pcr is not None:
        res["pcr_read"] = ("very high (>1.5): heavy put writing, supportive but crowded — contrarian caution"
                           if pcr > 1.5 else "bullish-leaning (1.0-1.5): puts written more than calls"
                           if pcr >= 1.0 else "neutral (0.7-1.0)" if pcr >= 0.7 else
                           "bearish-leaning (<0.7): calls written more than puts")
    return res


def buildup(price_change: float, oi_change: float) -> str:
    if price_change > 0 and oi_change > 0:
        return "Long buildup — fresh longs; bullish"
    if price_change < 0 and oi_change > 0:
        return "Short buildup — fresh shorts; bearish"
    if price_change > 0 and oi_change < 0:
        return "Short covering — shorts exiting; bullish but often short-lived"
    if price_change < 0 and oi_change < 0:
        return "Long unwinding — longs exiting; bearish but often short-lived"
    return "No clear signal (flat price or OI)"


# ---------------------------------------------------------------------------
# Expiry calendar
# ---------------------------------------------------------------------------
def is_holiday(d: date) -> bool:
    if d in HOLIDAYS:
        return True
    return d > HOLIDAYS_KNOWN_UNTIL and (d.month, d.day) in FIXED_ANNUAL_HOLIDAYS


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and not is_holiday(d)


def prev_trading_day(d: date) -> date:
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def last_weekday(year: int, month: int, weekday: int) -> date:
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    d = nxt - timedelta(days=1)
    while d.weekday() != weekday:
        d -= timedelta(days=1)
    return d


def expiries(underlying: str, start: date | None = None, count: int = 6) -> list:
    u = underlying.upper()
    exch, weekly, wd = EXPIRY_RULES.get(u, EXPIRY_RULES["STOCK"])
    start = start or datetime.now(IST).date()
    out, seen = [], set()
    if weekly:
        d = start + timedelta(days=(wd - start.weekday()) % 7)
        while len(out) < count:
            e = prev_trading_day(d)
            monthly = prev_trading_day(last_weekday(d.year, d.month, wd))
            if e >= start and e not in seen:
                out.append({"date": e.isoformat(), "weekday": e.strftime("%a"),
                            "type": "monthly" if e == monthly else "weekly",
                            "shifted_for_holiday": e != d})
                seen.add(e)
            d += timedelta(days=7)
    else:
        y, m = start.year, start.month
        while len(out) < count:
            nominal = last_weekday(y, m, wd)
            e = prev_trading_day(nominal)
            if e >= start:
                out.append({"date": e.isoformat(), "weekday": e.strftime("%a"), "type": "monthly",
                            "shifted_for_holiday": e != nominal})
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    warn = any(date.fromisoformat(x["date"]) > HOLIDAYS_KNOWN_UNTIL for x in out)
    return [dict(x, holiday_list_missing=warn and date.fromisoformat(x["date"]) > HOLIDAYS_KNOWN_UNTIL)
            for x in out]


# ---------------------------------------------------------------------------
# Strategy payoff
# ---------------------------------------------------------------------------
def parse_leg(spec: str) -> dict:
    """'B,CE,25000,120[,lots]'  side B/S, type CE/PE/FUT, strike (or futures price), premium, lots."""
    parts = [p.strip() for p in spec.split(",")]
    if len(parts) < 3:
        raise ValueError(f"bad leg {spec!r}; use SIDE,TYPE,STRIKE,PREMIUM[,LOTS]")
    side, kind = parts[0].upper()[0], parts[1].upper()
    if side not in "BS" or kind not in ("CE", "PE", "FUT"):
        raise ValueError(f"bad leg {spec!r}")
    strike = float(parts[2])
    premium = float(parts[3]) if len(parts) > 3 and parts[3] else (strike if kind == "FUT" else 0.0)
    lots = float(parts[4]) if len(parts) > 4 and parts[4] else 1.0
    return {"side": side, "kind": kind, "strike": strike, "premium": premium, "lots": lots}


def leg_expiry_value(leg: dict, s: float, lot_size: float) -> float:
    sign = 1 if leg["side"] == "B" else -1
    if leg["kind"] == "CE":
        v = max(0.0, s - leg["strike"]) - leg["premium"]
    elif leg["kind"] == "PE":
        v = max(0.0, leg["strike"] - s) - leg["premium"]
    else:
        v = s - leg["premium"]
    return sign * v * leg["lots"] * lot_size


def payoff_at(legs: list, s: float, lot_size: float) -> float:
    return sum(leg_expiry_value(l, s, lot_size) for l in legs)


def lognormal_prob_between(spot, lo, hi, iv, t, r=0.06):
    """Risk-neutral probability that S_T ends in (lo, hi)."""
    mu = math.log(spot) + (r - 0.5 * iv * iv) * t
    sd = iv * math.sqrt(t)
    cdf = lambda x: 0.0 if x <= 0 else (1.0 if x == math.inf else _N((math.log(x) - mu) / sd))
    return cdf(hi) - cdf(lo)


def analyse_strategy(legs: list, lot_size: float, spot: float, iv: float | None = None,
                     t: float | None = None, r: float = 0.06) -> dict:
    kinks = sorted({0.0} | {l["strike"] for l in legs if l["kind"] != "FUT"} | {spot})
    far = max(kinks) * 3 + 1
    pts = kinks + [far]
    vals = [payoff_at(legs, x, lot_size) for x in pts]
    slope_right = (payoff_at(legs, far + 1, lot_size) - vals[-1])
    breakevens = []
    for (x0, v0), (x1, v1) in zip(zip(pts, vals), zip(pts[1:], vals[1:])):
        if v0 == 0 and x0 not in breakevens and x0 > 0:
            breakevens.append(x0)
        if v0 * v1 < 0:
            breakevens.append(x0 + (x1 - x0) * (-v0) / (v1 - v0))
    max_profit = math.inf if slope_right > 1e-9 else max(vals)
    max_loss = -math.inf if slope_right < -1e-9 else min(vals)
    net_premium = sum((-1 if l["side"] == "B" else 1) * l["premium"] * l["lots"] * lot_size
                      for l in legs if l["kind"] != "FUT")
    res = {"lot_size": lot_size, "net_premium": net_premium,
           "net": "credit" if net_premium > 0 else "debit" if net_premium < 0 else "zero",
           "max_profit": max_profit, "max_loss": max_loss, "breakevens": sorted(breakevens),
           "payoff_at_spot": payoff_at(legs, spot, lot_size)}
    if iv and t:
        edges = [0.0] + sorted(breakevens) + [math.inf]
        pop = 0.0
        for lo, hi in zip(edges, edges[1:]):
            if hi == math.inf:
                probe = max(lo, spot) * 1.1 + 1
            elif lo == 0:
                probe = hi / 2
            else:
                probe = (lo + hi) / 2
            if payoff_at(legs, probe, lot_size) > 0:
                pop += lognormal_prob_between(spot, lo, hi, iv, t, r)
        res["probability_of_profit"] = pop
        greeks = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
        value_now = 0.0
        for l in legs:
            sign = 1 if l["side"] == "B" else -1
            mult = sign * l["lots"] * lot_size
            if l["kind"] == "FUT":
                greeks["delta"] += mult
                value_now += mult * (spot - l["premium"])
                continue
            g = bs(spot, l["strike"], t, iv, r, 0.0, l["kind"])
            for k in greeks:
                greeks[k] += mult * g[k]
            value_now += mult * (g["price"] - l["premium"])
        res["position_greeks"] = greeks
        res["mark_to_model_pnl_now"] = value_now
    return res


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _t_from_args(a) -> float:
    if getattr(a, "expiry", None):
        return years_to_expiry(date.fromisoformat(a.expiry))
    return max(a.days, 1 / (24 * 60)) / 365


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Indian F&O toolkit")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_time(sp):
        sp.add_argument("--days", type=float, default=7.0, help="calendar days to expiry")
        sp.add_argument("--expiry", help="expiry date YYYY-MM-DD (overrides --days; 15:30 IST)")
        sp.add_argument("--rate", type=float, default=6.0, help="risk-free %% (use 91-day T-bill)")
        sp.add_argument("--div-yield", type=float, default=0.0, help="dividend yield %%")

    g = sub.add_parser("greeks")
    g.add_argument("--spot", type=float, required=True)
    g.add_argument("--strike", type=float, required=True)
    g.add_argument("--type", choices=["CE", "PE", "ce", "pe"], required=True)
    g.add_argument("--iv", type=float, required=True, help="IV in %%")
    g.add_argument("--lot-size", type=float, default=1)
    add_time(g)

    v = sub.add_parser("iv")
    v.add_argument("--spot", type=float, required=True)
    v.add_argument("--strike", type=float, required=True)
    v.add_argument("--type", choices=["CE", "PE", "ce", "pe"], required=True)
    v.add_argument("--premium", type=float, required=True)
    add_time(v)

    m = sub.add_parser("move")
    m.add_argument("--spot", type=float, required=True)
    m.add_argument("--iv", type=float, required=True, help="IV %% or India VIX value")
    m.add_argument("--days", type=float, default=1.0)

    c = sub.add_parser("chain")
    c.add_argument("csv")
    c.add_argument("--spot", type=float)
    c.add_argument("--json", action="store_true")

    b = sub.add_parser("buildup")
    b.add_argument("--price-change", type=float, required=True)
    b.add_argument("--oi-change", type=float, required=True)

    e = sub.add_parser("expiries")
    e.add_argument("underlying", help="NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, NIFTYNXT50, SENSEX, BANKEX, STOCK")
    e.add_argument("--count", type=int, default=6)
    e.add_argument("--from", dest="start", help="YYYY-MM-DD (default today IST)")

    pf = sub.add_parser("payoff")
    pf.add_argument("--leg", action="append", required=True, help="SIDE,TYPE,STRIKE,PREMIUM[,LOTS]")
    pf.add_argument("--spot", type=float, required=True)
    pf.add_argument("--underlying", help="sets lot size from the table")
    pf.add_argument("--lot-size", type=float)
    pf.add_argument("--iv", type=float, help="IV %% for POP and Greeks")
    pf.add_argument("--json", action="store_true")
    add_time(pf)

    sub.add_parser("lots")
    a = p.parse_args(argv)

    if a.cmd == "lots":
        print(f"Index lot sizes (verified {DATA_AS_OF}; stock F&O lots vary — check the exchange contract file):")
        for k, val in LOT_SIZES.items():
            print(f"  {k:<11} {val}")
        return 0

    if a.cmd == "greeks":
        t = _t_from_args(a)
        r = bs(a.spot, a.strike, t, a.iv / 100, a.rate / 100, a.div_yield / 100, a.type)
        print(f"{a.type.upper()} {a.strike:g} | spot {a.spot:g} | IV {a.iv}% | {t * 365:.2f} days")
        for k in ("price", "delta", "gamma", "theta", "vega", "rho"):
            per = {"theta": " /day", "vega": " /1% IV", "rho": " /1% rate"}.get(k, "")
            extra = f"   (x lot {a.lot_size:g} = {r[k] * a.lot_size:,.2f})" if a.lot_size != 1 else ""
            print(f"  {k:<6} {r[k]:>12.4f}{per}{extra}")
        return 0

    if a.cmd == "iv":
        t = _t_from_args(a)
        val = implied_vol(a.premium, a.spot, a.strike, t, a.rate / 100, a.div_yield / 100, a.type)
        print("IV: below intrinsic / unsolvable" if val is None else f"Implied volatility: {val * 100:.2f}%")
        return 0

    if a.cmd == "move":
        r = expected_move(a.spot, a.iv, a.days)
        print(f"Spot {a.spot:g}, IV {a.iv}% over {a.days:g} day(s):")
        print(f"  1σ move ≈ ±{r['one_sigma_points']:.1f} pts (±{r['one_sigma_pct']:.2f}%)")
        print(f"  ~68% range {r['range_68'][0]:.0f} – {r['range_68'][1]:.0f}")
        print(f"  ~95% range {r['range_95'][0]:.0f} – {r['range_95'][1]:.0f}")
        return 0

    if a.cmd == "chain":
        try:
            res = analyse_chain(load_chain(a.csv), a.spot)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        if a.json:
            print(json.dumps(res, indent=2))
            return 0
        pcr = res["pcr_oi"]
        print(f"PCR (OI): {pcr:.2f} — {res.get('pcr_read', '')}" if pcr else "PCR (OI): n/a")
        if res["pcr_change_oi"] is not None:
            print(f"PCR (change in OI): {res['pcr_change_oi']:.2f}")
        print(f"Max pain: {res['max_pain']:g}")
        print("Call OI walls (resistance):", ", ".join(f"{s:g} ({o:,.0f})" for s, o in res["call_walls"]))
        print("Put OI walls (support):   ", ", ".join(f"{s:g} ({o:,.0f})" for s, o in res["put_walls"]))
        if res["fresh_call_writing"]:
            print("Fresh call OI added:", ", ".join(f"{s:g} (+{o:,.0f})" for s, o in res["fresh_call_writing"]))
        if res["fresh_put_writing"]:
            print("Fresh put OI added: ", ", ".join(f"{s:g} (+{o:,.0f})" for s, o in res["fresh_put_writing"]))
        if "atm_straddle" in res:
            lo, hi = res["straddle_implied_range"]
            print(f"ATM {res['atm_strike']:g} straddle {res['atm_straddle']:.2f} → expiry range ≈ {lo:.0f} – {hi:.0f}")
        if "atm_iv" in res:
            print(f"ATM IV ≈ {res['atm_iv']:.2f}%")
        if "skew_put_minus_call_iv" in res:
            print(f"Skew (3% OTM put IV − 3% OTM call IV): {res['skew_put_minus_call_iv']:+.2f} vol pts")
        print("OI shows where writers sit, not where price must go; walls shift intraday.")
        return 0

    if a.cmd == "buildup":
        print(buildup(a.price_change, a.oi_change))
        return 0

    if a.cmd == "expiries":
        start = date.fromisoformat(a.start) if a.start else None
        rows = expiries(a.underlying, start, a.count)
        print(f"{a.underlying.upper()} expiries (holidays verified to {HOLIDAYS_KNOWN_UNTIL}):")
        for x in rows:
            flag = " (moved: holiday)" if x["shifted_for_holiday"] else ""
            flag += " ⚠ holiday list not loaded for this year" if x["holiday_list_missing"] else ""
            print(f"  {x['date']} {x['weekday']}  {x['type']}{flag}")
        return 0

    if a.cmd == "payoff":
        legs = [parse_leg(s) for s in a.leg]
        lot = a.lot_size or (LOT_SIZES.get(a.underlying.upper()) if a.underlying else None)
        if not lot:
            print("error: give --lot-size or a known --underlying", file=sys.stderr)
            return 2
        t = _t_from_args(a) if a.iv else None
        res = analyse_strategy(legs, lot, a.spot, a.iv / 100 if a.iv else None, t, a.rate / 100)
        if a.json:
            print(json.dumps(res, indent=2, default=str))
            return 0
        fmt = lambda x: "unlimited" if math.isinf(x) else ("-" if x < 0 else "") + f"₹{abs(x):,.0f}"
        print(f"Legs (lot size {lot:g}):")
        for l in legs:
            print(f"  {'BUY ' if l['side'] == 'B' else 'SELL'} {l['lots']:g} x {l['kind']} {l['strike']:g} @ {l['premium']:g}")
        print(f"Net premium: ₹{abs(res['net_premium']):,.0f} {res['net']}")
        print(f"Max profit: {fmt(res['max_profit'])}   Max loss: {fmt(res['max_loss'])}")
        print("Breakevens at expiry:", ", ".join(f"{x:.1f}" for x in res["breakevens"]) or "none")
        if "probability_of_profit" in res:
            print(f"Probability of profit (lognormal, IV {a.iv}%): {res['probability_of_profit'] * 100:.1f}%")
            gk = res["position_greeks"]
            print(f"Position Greeks: Δ {gk['delta']:+.1f}  Γ {gk['gamma']:+.4f}  Θ {gk['theta']:+.0f}/day  "
                  f"Vega {gk['vega']:+.0f}/1%")
        print("Margin is not modelled — use the broker's basket margin calculator (hedged legs get SPAN benefit).")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
