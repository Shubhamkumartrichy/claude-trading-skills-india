#!/usr/bin/env python3
"""Momentum scanner for NSE/BSE stocks: short bursts, news gaps and relative-strength ranks.

Works across a universe (a folder of daily OHLCV CSVs, a list of files, or yfinance symbols) and
looks for three kinds of strength, then combines them into a short watchlist:

  burst   a big up-close (default +4%) out of a few quiet sessions, on volume above both the
          previous session and the 50-day average, closing near the high, in a rising 50 DMA
  pivot   an opening gap (default +5%) on at least 3x the 50-day volume that keeps holding the
          gap-day low, with a day-1/day-2 read and a catalyst checklist you fill in
  rs      a 1-99 relative-strength rating ranked inside the universe from weighted 3/6/9/12-month
          returns (40/20/20/20), distance from the 52-week high, and the RS line vs NIFTY
  scan    all three ranked into a watchlist with the reason for each name and a hand-off to
          breakout-planner (`breakout.py check`) for the base, pivot and order plan

India specifics: circuit bands (2/5/10/20%) from --bands or inferred from the tape, upper-circuit
closes flagged (you cannot buy a locked stock), ₹-crore traded-value floor, delivery % when the
CSV carries it, NSE pre-open notes, F&O monthly-expiry-week tags.

Zero dependencies (Python 3.10+ stdlib). Optional: `pip install yfinance` for --symbols/--benchmark.
Re-uses technical-analysis/ta.py (loaders, SMA) and breakout-planner/breakout.py (liquidity, NSE
ticks, circuit bands).

Examples
  python3 momentum.py burst --csv-dir data/ --days 5
  python3 momentum.py pivot --csv-dir data/ --catalyst DIXON=results --catalyst KAYNES=order-win
  python3 momentum.py rs --csv-dir data/ --benchmark-csv nifty.csv --top 20
  python3 momentum.py scan --csv-dir data/ --benchmark-csv nifty.csv --bands bands.csv --regime confirmed
  python3 momentum.py scan --symbols DIXON,KAYNES,TRENT,POLYCAB --benchmark NIFTY --json
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import math
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parents[2]


def _sibling(skill: str, script: str):
    name = f"_momentum_scanner_dep_{script}"
    if name in sys.modules:
        return sys.modules[name]
    path = SKILLS_DIR / skill / "scripts" / f"{script}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


ta = _sibling("technical-analysis", "ta")
bp = _sibling("breakout-planner", "breakout")

CRORE = bp.CRORE
BANDS = tuple(float(b) for b in bp.CIRCUIT_BANDS)       # 2, 5, 10, 20
BAND_TOL = 0.15                                         # % slack for tick rounding at the circuit
# (sessions back, weight, label). Cumulative returns, so the latest quarter sits inside all four.
RS_HORIZONS = ((63, 0.40, "3m"), (126, 0.20, "6m"), (189, 0.20, "9m"), (252, 0.20, "12m"))
YEAR = 252

CATALYSTS = {
    "results": "Quarterly results: a beat on revenue and operating margin, not just PAT (other income "
               "and one-offs inflate PAT). Guidance raised on the call is better still.",
    "order-win": "Order win: size it against trailing revenue. Above ~10-15% of annual sales moves "
                 "estimates; routine PSU defence/rail/infra orders often don't.",
    "block-deal": "Block/bulk deal: check who bought and who sold. A long-only fund taking a promoter or "
                  "PE stake can re-rate the stock; a promoter selling into strength is supply.",
    "approval": "Regulatory approval: USFDA approval or a clean inspection for pharma, a licence, tariff "
                "order or PLI award. Ask whether it changes earnings, not just headlines.",
    "rating-upgrade": "Credit or broker rating upgrade: weak on its own. It counts when estimates rise with it.",
    "other": "Other news (policy change, merger/demerger, fund-raise, index inclusion): confirm it on the "
             "NSE/BSE corporate announcement page.",
}
CATALYST_ALIASES = {"earnings": "results", "result": "results", "order": "order-win", "orders": "order-win",
                    "block": "block-deal", "bulk-deal": "block-deal", "usfda": "approval",
                    "regulatory": "approval", "upgrade": "rating-upgrade", "rating": "rating-upgrade"}
PREOPEN_NOTE = ("NSE pre-open: orders are collected 09:00-09:08 and a call auction fixes the open; normal "
                "trading starts at 09:15. The gap here is that auction price. Don't chase the first minutes: "
                "buy above the gap-day high or the opening-range high, stop under the gap-day low.")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _avg(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _r(x, d=2):
    return None if x is None or not math.isfinite(x) else round(x, d)


def _iso(d) -> str:
    return d.date().isoformat() if isinstance(d, datetime) else d.isoformat()


def clean(o):
    """Make an object JSON-safe: no NaN/Infinity, dates as ISO strings."""
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items() if not k.startswith("_")}
    if isinstance(o, (list, tuple)):
        return [clean(x) for x in o]
    if isinstance(o, (datetime, date)):
        return _iso(o)
    if isinstance(o, Path):
        return str(o)
    return o


def dump(obj) -> str:
    return json.dumps(clean(obj), indent=2, allow_nan=False, ensure_ascii=False)


def monthly_expiry_week(d: date) -> bool:
    """True from Monday up to the last Tuesday of the month (NSE stock F&O monthly expiry).

    Approximate: a holiday on that Tuesday moves expiry to the previous trading day.
    """
    nxt = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    last = nxt - timedelta(days=1)
    last_tue = last - timedelta(days=(last.weekday() - 1) % 7)
    return d.isocalendar()[:2] == last_tue.isocalendar()[:2] and d <= last_tue


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------
def _good(r: dict) -> bool:
    vals = (r["open"], r["high"], r["low"], r["close"])
    if not all(isinstance(x, float) and math.isfinite(x) and x > 0 for x in vals):
        return False
    return r["high"] >= r["low"]


def _delivery_map(path: str) -> dict:
    """date -> delivery % from an optional column (NSE DELIV_PER, '% Dly Qt to Traded Qty', ...)."""
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        fields = reader.fieldnames or []
        norm = {f: "".join(ch for ch in f.lower() if ch.isalnum()) for f in fields}
        dcol = next((f for f in fields if norm[f] in ("date", "datetime", "timestamp", "time")), None)
        pct = next((f for f in fields if ("deliv" in norm[f] or "dly" in norm[f])
                    and ("per" in norm[f] or "pct" in norm[f] or "%" in f or norm[f] in ("delivery", "deliv"))),
                   None)
        qty = next((f for f in fields if ("deliv" in norm[f] or "dly" in norm[f]) and "qty" in norm[f]
                    and "traded" not in norm[f] and "%" not in f), None)
        vcol = next((f for f in fields if norm[f] in ("volume", "vol", "totaltradedquantity", "qty")), None)
        if not dcol or not (pct or (qty and vcol)):
            return {}
        out = {}
        for r in reader:
            try:
                d = ta.parse_ts(r[dcol]).date()
                if pct:
                    x = ta._f(r[pct])
                else:
                    q, v = ta._f(r[qty]), ta._f(r[vcol])
                    x = q / v * 100 if v and v > 0 else math.nan
            except (ValueError, KeyError, TypeError):
                continue
            if math.isfinite(x) and 0 <= x <= 100:
                out[d] = x
    return out


def build_stock(rows: list, name: str, source=None, kind="csv", deliv: dict | None = None) -> dict:
    rows = [r for r in rows if _good(r)]
    for r in rows:
        if not math.isfinite(r["volume"]) or r["volume"] < 0:
            r["volume"] = 0.0
    if len(rows) < 2:
        raise ValueError("fewer than 2 usable candles")
    c = [r["close"] for r in rows]
    v = [r["volume"] for r in rows]
    pre = [0.0]
    for x in v:
        pre.append(pre[-1] + x)
    vavg50 = [None] * len(v)                     # mean of the 50 sessions BEFORE bar i
    for i in range(50, len(v)):
        vavg50[i] = (pre[i] - pre[i - 50]) / 50
    dl = None
    if deliv:
        dl = [deliv.get(r["date"].date()) for r in rows]
        if all(x is None for x in dl):
            dl = None
    return {"name": name, "source": source, "kind": kind, "rows": rows, "date": [r["date"] for r in rows],
            "o": [r["open"] for r in rows], "h": [r["high"] for r in rows], "l": [r["low"] for r in rows],
            "c": c, "v": v, "vavg50": vavg50, "sma50": ta.sma(c, 50), "sma200": ta.sma(c, 200), "deliv": dl,
            "band_known": None}


def _norm_sym(s: str) -> str:
    s = s.strip().upper()
    for suf in (".NS", ".BO"):
        if s.endswith(suf):
            s = s[: -len(suf)]
    return s


def load_bands(path: str) -> dict:
    p = Path(path)
    text = p.read_text(encoding="utf-8-sig")
    if p.suffix.lower() == ".json":
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("--bands JSON must be an object {SYMBOL: band}")
        items = list(data.items())
    else:
        items = []
        for k, row in enumerate(csv.reader(text.splitlines())):
            row = [x.strip() for x in row if x.strip()]
            if len(row) < 2:
                continue
            if k == 0 and row[0].lower() in ("symbol", "name", "ticker"):
                continue
            items.append((row[0], row[1]))
    out = {}
    for sym, raw in items:
        s = str(raw).strip().lower().rstrip("%")
        if s in ("", "0", "none", "no band", "nil", "fno", "f&o", "dynamic"):
            val = 0.0
        else:
            try:
                val = float(s)
            except ValueError:
                raise ValueError(f"--bands: {sym} has band {raw!r}; use 2, 5, 10, 20 or 0 (no fixed band, F&O)")
        if val not in (0.0,) + BANDS:
            raise ValueError(f"--bands: {sym} has band {raw!r}; use 2, 5, 10, 20 or 0 (no fixed band, F&O)")
        out[_norm_sym(str(sym))] = val
    if not out:
        raise ValueError(f"--bands {path}: no SYMBOL,band rows found")
    return out


def parse_as_of(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"--as-of {s!r} is not YYYY-MM-DD")


def _cut(rows: list, as_of):
    return [r for r in rows if r["date"].date() <= as_of] if as_of else rows


def load_benchmark(a):
    rows, name = None, None
    if a.benchmark_csv:
        rows, name = ta.load_csv(a.benchmark_csv), Path(a.benchmark_csv).stem
    elif a.benchmark:
        rows, name = ta.fetch_yf(a.benchmark, a.exchange, a.period, "1d"), a.benchmark.upper()
    if rows is None:
        return None
    rows = [r for r in _cut(rows, a.as_of) if _good(r)]
    if len(rows) < 64:
        raise ValueError(f"benchmark {name}: only {len(rows)} usable bars (need 64+)")
    return {"name": name, "rows": rows}


def load_universe(a, bands: dict):
    jobs = []
    if a.csv_dir:
        d = Path(a.csv_dir)
        if not d.is_dir():
            raise ValueError(f"--csv-dir {a.csv_dir} is not a folder")
        jobs += [("csv", f) for f in sorted(d.glob(a.pattern)) if f.is_file()]
    for f in a.csv or []:
        jobs.append(("csv", Path(f)))
    if a.symbols:
        jobs += [("yf", s.strip()) for s in a.symbols.split(",") if s.strip()]
    if not (a.csv_dir or a.csv or a.symbols):
        raise ValueError("give --csv-dir DIR, --csv FILE or --symbols A,B,C")
    if a.benchmark_csv:
        b = Path(a.benchmark_csv).resolve()
        jobs = [j for j in jobs if j[0] != "csv" or j[1].resolve() != b]
    if not jobs:
        raise ValueError("nothing to scan (no matching files or symbols)")
    stocks, skipped, seen = [], [], set()
    for kind, item in jobs:
        label = _norm_sym(item.stem if kind == "csv" else item)
        if label in seen:
            skipped.append({"name": label, "error": "duplicate symbol, first file kept"})
            continue
        try:
            if kind == "csv":
                rows, deliv = ta.load_csv(str(item)), _delivery_map(str(item))
            else:
                rows, deliv = ta.fetch_yf(item, a.exchange, a.period, "1d"), None
            rows = _cut(rows, a.as_of)
            if not rows:
                raise ValueError("no usable rows" + (f" on or before {a.as_of}" if a.as_of else ""))
            st = build_stock(rows, label, str(item) if kind == "csv" else item.upper(), kind, deliv)
        except SystemExit as e:
            if not isinstance(e.code, str):
                raise
            skipped.append({"name": label, "error": e.code})
            continue
        except (ValueError, OSError, KeyError, ZeroDivisionError, UnicodeDecodeError, csv.Error) as e:
            skipped.append({"name": label, "error": str(e)})
            continue
        st["band_known"] = bands.get(label)
        seen.add(label)
        stocks.append(st)
    if not stocks:
        why = "; ".join(f"{s['name']}: {s['error']}" for s in skipped[:3])
        raise ValueError(f"no usable stocks ({why})")
    return stocks, skipped


def session_window(stocks: list, days: int):
    """Earliest date among the universe's last `days` distinct sessions (so stale files don't leak in)."""
    dates = sorted({d.date() for st in stocks for d in st["date"][-days * 3 - 5:]})
    return dates[-days] if len(dates) >= days else (dates[0] if dates else None)


# ---------------------------------------------------------------------------
# India: bands, circuits, delivery
# ---------------------------------------------------------------------------
def infer_small_band(st: dict, i: int, lookback: int = 120):
    """2% or 5% if the tape keeps closing exactly at ±band and never moves beyond it."""
    c, h, l = st["c"], st["h"], st["l"]
    lo = max(1, i - lookback + 1)
    if i - lo < 20:
        return None
    chg = {j: (c[j] / c[j - 1] - 1) * 100 for j in range(lo, i + 1)}
    biggest = max(abs(x) for x in chg.values())
    for b in (2.0, 5.0):
        hits = 0
        for j, x in chg.items():
            tick = bp.tick_for(c[j - 1])
            if abs(abs(x) - b) <= BAND_TOL and ((x > 0 and h[j] - c[j] <= tick / 2 + 1e-9) or
                                                (x < 0 and c[j] - l[j] <= tick / 2 + 1e-9)):
                hits += 1
        if hits >= 2 and biggest <= b + 0.25:
            return b
    return None


def band_for(st: dict, i: int) -> dict:
    if st["band_known"] is not None:
        return {"band_pct": st["band_known"], "source": "--bands"}
    b = infer_small_band(st, i)
    if b:
        return {"band_pct": b, "source": "inferred"}
    return {"band_pct": None, "source": "unknown"}


def circuit_at(st: dict, i: int, band: dict, price_key: str = "c"):
    """Did bar i close (or open, price_key='o') at the upper circuit? None if not."""
    prev = st["c"][i - 1]
    px_ = st[price_key][i]
    h = st["h"][i]
    tick = bp.tick_for(prev)
    at_high = h - px_ <= tick / 2 + 1e-9
    if not at_high:
        return None
    b = band["band_pct"]
    frozen = abs(st["h"][i] - st["l"][i]) <= tick / 2 + 1e-9
    if b == 0:
        return None
    if b:
        upper = bp.to_tick(prev * (1 + b / 100), None, "down")
        if px_ >= upper - tick / 2 - 1e-9:
            return {"status": "locked", "band_pct": b, "upper": upper, "frozen_all_day": frozen,
                    "source": band["source"]}
        return None
    move = (px_ / prev - 1) * 100
    for cand in BANDS:
        if abs(move - cand) <= BAND_TOL:
            return {"status": "possible", "band_pct": cand, "upper": px_, "frozen_all_day": frozen,
                    "source": "inferred from a band-sized move closing at the high"}
    return None


def delivery_at(st: dict, i: int):
    dl = st["deliv"]
    if not dl or dl[i] is None:
        return None
    avg = _avg(dl[max(0, i - 20):i])
    return {"pct": _r(dl[i], 1), "avg20": _r(avg, 1), "below_avg": avg is not None and dl[i] < avg}


def _india_flags(st, i, band, circ, cfg, ev):
    flags, notes = [], []
    if circ:
        if circ["status"] == "locked":
            flags.append("UC")
            notes.append(f"Closed at the upper circuit ₹{circ['upper']:,.2f} ({circ['band_pct']:g}% band"
                         + (", no trade below it all day" if circ["frozen_all_day"] else "")
                         + "): buyers were stuck in the queue, so the close is not a price you could pay. "
                           "Expect a gap next open; don't chase it.")
        else:
            flags.append("UC?")
            notes.append(f"Possible upper circuit: +{circ['band_pct']:g}% close at the day's high. Confirm the "
                         "band on NSE; a locked stock can't be bought and often gaps next day.")
    b = band["band_pct"]
    if b in (2.0, 5.0):
        flags.append(f"band{b:g}")
        notes.append(f"{b:g}% price band ({band['source']}): usually surveillance (ASM/GSM), trade-for-trade or "
                     "SME. Moves lock at the circuits, exits can be impossible, often no intraday/MTF. Skip or "
                     "size down hard.")
    dl = ev.get("delivery")
    if dl and dl["below_avg"]:
        flags.append("low-deliv")
        notes.append(f"Delivery {dl['pct']:g}% vs {dl['avg20']:g}% 20-day average: mostly intraday churn.")
    d = st["date"][i].date()
    if monthly_expiry_week(d) and b in (None, 0.0):
        flags.append("expiry-wk")
        notes.append("F&O monthly-expiry week (last Tuesday): rollover and short covering can fake a burst in "
                     "F&O names. Want a second day of strength.")
    return flags, notes


def _penalty(flags: list) -> int:
    p = 0
    if "UC" in flags:
        p += 30
    elif "UC?" in flags:
        p += 20
    if any(f.startswith("band") for f in flags):
        p += 15
    return p


# ---------------------------------------------------------------------------
# Burst
# ---------------------------------------------------------------------------
def base_quality(h, l, c, i, max_days=10, max_width=8.0):
    """Sessions of quiet trading right before bar i: (days, width %, range ratio vs the 20 before)."""
    days, width = 0, None
    for k in range(1, max_days + 1):
        j = i - k
        if j < 1:
            break
        w = (max(h[j:i]) / min(l[j:i]) - 1) * 100
        if w > max_width:
            break
        days, width = k, w
    look = max(days, 3)

    def rng(j):
        return (h[j] - l[j]) / c[j - 1] * 100

    seg = range(i - look, i)
    pre = range(max(1, i - look - 20), i - look)
    ratio = None
    if len(pre) >= 10 and seg.start >= 1:
        a, b = _avg([rng(j) for j in seg]), _avg([rng(j) for j in pre])
        ratio = a / b if b else None
    return days, width, ratio


def burst_at(st: dict, i: int, cfg) -> dict | None:
    c, h, l, v = st["c"], st["h"], st["l"], st["v"]
    if i < 60:
        return None
    prev = c[i - 1]
    chg = (c[i] / prev - 1) * 100
    if chg < cfg.min_move_pct:
        return None
    reasons = []
    avg = st["vavg50"][i]
    vol_ratio = v[i] / avg if avg else None
    if not avg or v[i] <= 0:
        reasons.append("no volume data")
    else:
        if v[i - 1] > 0 and v[i] <= v[i - 1]:
            reasons.append(f"volume {v[i] / v[i - 1]:.2f}x the previous session (must rise)")
        if v[i] <= avg:
            reasons.append(f"volume {vol_ratio:.2f}x the 50-day average (must be above it)")
    rng = h[i] - l[i]
    pos = 100.0 if rng <= 0 else (c[i] - l[i]) / rng * 100
    if pos < cfg.min_close_pos:
        reasons.append(f"closed at {pos:.0f}% of the day's range (needs {cfg.min_close_pos:g}%+)")
    days, width, ratio = base_quality(h, l, c, i, 10, cfg.max_base_pct)
    if days < cfg.min_base_days:
        reasons.append(f"no quiet base: {days} tight session(s) before the move (needs {cfg.min_base_days}+ "
                       f"within a {cfg.max_base_pct:g}% range)")
    elif ratio is not None and ratio > cfg.max_range_ratio:
        reasons.append(f"ranges were widening before the move ({ratio:.2f}x the prior 20 sessions, "
                       f"max {cfg.max_range_ratio:g}x)")
    run3 = (prev / c[i - 4] - 1) * 100
    if run3 > cfg.max_run_pct:
        reasons.append(f"extended: already {run3:+.1f}% in the 3 sessions before (max {cfg.max_run_pct:g}%)")
    s50, s50p, s200 = st["sma50"][i], st["sma50"][i - 10], st["sma200"][i]
    if s50 is None or s50p is None:
        reasons.append("need 60+ bars for the 50 DMA")
    else:
        if c[i] <= s50:
            reasons.append(f"closed below the 50 DMA ({s50:,.2f})")
        if s50 <= s50p:
            reasons.append("50 DMA not rising")
    liq = bp.liquidity(st["rows"][max(0, i - 20):i], cfg.min_turnover_cr)
    if liq["ok"] is False:
        reasons.append(f"illiquid: ₹{liq['avg_traded_value_cr']:.2f} cr/day traded (min ₹{cfg.min_turnover_cr:g} cr)")
    dl = delivery_at(st, i)
    if cfg.min_delivery_pct is not None and dl and dl["pct"] < cfg.min_delivery_pct:
        reasons.append(f"delivery {dl['pct']:g}% below --min-delivery-pct {cfg.min_delivery_pct:g}")
    band = band_for(st, i)
    circ = circuit_at(st, i, band)
    if cfg.exclude_locked and circ:
        reasons.append("upper-circuit close (--exclude-locked)")
    last = len(c) - 1
    risk_pct = (c[i] - l[i]) / c[i] * 100
    ev = {"symbol": st["name"], "date": st["date"][i], "close": c[i], "prev_close": prev, "chg_pct": _r(chg),
          "volume": v[i], "vol_vs_prev": _r(v[i] / v[i - 1]) if v[i - 1] > 0 else None,
          "vol_vs_50d": _r(vol_ratio), "close_pos_pct": _r(pos, 0), "base_days": days,
          "base_width_pct": _r(width), "range_ratio": _r(ratio), "run_3d_pct": _r(run3),
          "sma50": _r(s50), "above_rising_50dma": bool(s50 and s50p and c[i] > s50 and s50 > s50p),
          "turnover_cr": _r(liq["avg_traded_value_cr"]), "delivery": dl, "band": band, "circuit": circ,
          "entry_ref": h[i], "stop_ref": l[i], "risk_pct": _r(risk_pct),
          "sessions_since": last - i, "since_pct": _r((c[last] / c[i] - 1) * 100),
          "low_held": all(x >= l[i] for x in l[i + 1:]) if i < last else None}
    flags, notes = _india_flags(st, i, band, circ, cfg, ev)
    if risk_pct > 8:
        flags.append("wide-stop")
        notes.append(f"Burst-day low is {risk_pct:.1f}% below the close: a stop there is wide for a 3-5 day trade.")
    if ev["low_held"] is False:
        notes.append("Price has since traded below the burst-day low: the burst failed.")
    # score (documented in SKILL.md): move 25, volume 20, close 15, base 10, contraction 15, trend 10, delivery 5
    pts = min(25.0, 15 + (chg - 4) * 2.5) if chg >= 4 else 10.0
    vr = vol_ratio or 0
    pts += 20 if vr >= 3 else 15 if vr >= 2 else 10 if vr >= 1.5 else 5 if vr > 1 else 0
    pts += 15 if pos >= 90 else 10 if pos >= 80 else 5 if pos >= 70 else 0
    pts += 10 if days >= 5 else 5 if days >= 3 else 0
    pts += (15 if ratio <= 0.6 else 10 if ratio <= 0.8 else 5 if ratio <= 1.0 else 0) if ratio is not None else 0
    pts += 10 if s50 and s200 and s50 > s200 else 0
    pts += 5 if dl and not dl["below_avg"] else 0
    ev["score"] = max(0, round(pts - _penalty(flags)))
    ev["flags"], ev["notes"], ev["reasons"] = flags, notes, reasons
    ev["passed"] = not reasons
    return ev


def find_events(stocks, fn, cfg, start):
    hits, rejects = [], []
    for st in stocks:
        for i in range(1, len(st["c"])):
            if st["date"][i].date() < start:
                continue
            ev = fn(st, i, cfg)
            if ev is None:
                continue
            (hits if ev["passed"] else rejects).append(ev)
    hits.sort(key=lambda e: (-e["score"], e["symbol"]))
    rejects.sort(key=lambda e: (e["symbol"], e["date"]))
    return hits, rejects


# ---------------------------------------------------------------------------
# Episodic pivot
# ---------------------------------------------------------------------------
def parse_catalysts(items) -> dict:
    out = {}
    for it in items or []:
        if "=" in it:
            sym, kinds = it.split("=", 1)
            key = _norm_sym(sym)
        else:
            key, kinds = "*", it
        if not key or not kinds.strip():
            raise ValueError(f"--catalyst {it!r}: give SYMBOL=kind or just kind")
        for k in (x.strip().lower() for x in kinds.split(",") if x.strip()):
            k = CATALYST_ALIASES.get(k, k)
            if k not in CATALYSTS:
                raise ValueError(f"--catalyst kind {k!r} unknown; use {', '.join(CATALYSTS)}")
            if k not in out.setdefault(key, []):
                out[key].append(k)
    return out


def catalyst_check(symbol: str, cats: dict) -> dict:
    given = list(dict.fromkeys(cats.get(symbol, []) + cats.get("*", [])))
    checklist = [{"kind": k, "confirmed": k in given, "what_to_check": CATALYSTS[k]}
                 for k in CATALYSTS if k != "other" or k in given]
    return {"supplied": given, "checklist": checklist,
            "note": None if given else "No catalyst supplied. Check NSE/BSE announcements, results, order and "
                                       "block-deal filings. An unexplained gap is a burst, not an episodic pivot, "
                                       "and in small caps can be an operator move."}


def pivot_at(st: dict, i: int, cfg) -> dict | None:
    c, o, h, l, v = st["c"], st["o"], st["h"], st["l"], st["v"]
    if i < 51:
        return None
    prev = c[i - 1]
    gap = (o[i] / prev - 1) * 100
    if gap < cfg.min_gap_pct:
        return None
    reasons = []
    avg = st["vavg50"][i]
    mult = v[i] / avg if avg else None
    if not avg or v[i] <= 0:
        reasons.append("no volume data")
    elif mult < cfg.min_vol_mult:
        reasons.append(f"volume only {mult:.1f}x the 50-day average (needs {cfg.min_vol_mult:g}x)")
    last = len(c) - 1
    gap_low = l[i]
    broke = next((j for j in range(i + 1, last + 1) if c[j] < gap_low), None)
    if broke is not None:
        reasons.append(f"closed below the gap-day low ₹{gap_low:,.2f} on {_iso(st['date'][broke])}")
    filled = any(x <= prev for x in l[i:])
    liq = bp.liquidity(st["rows"][max(0, i - 20):i], cfg.min_turnover_cr)
    if liq["ok"] is False:
        reasons.append(f"illiquid: ₹{liq['avg_traded_value_cr']:.2f} cr/day traded (min ₹{cfg.min_turnover_cr:g} cr)")
    rng = h[i] - l[i]
    pos = 100.0 if rng <= 0 else (c[i] - l[i]) / rng * 100
    green = c[i] >= o[i]
    day1 = ("strong: closed above the open, near the high" if green and pos >= 60 else
            "fair: held the middle of the range" if pos >= 40 else
            "weak: sold into the open, closed near the low")
    if i < last:
        j = i + 1
        day2 = ("follow-through: closed above day 1" if c[j] > c[i] else
                "holding: no new high but above the gap-day low" if l[j] >= gap_low else
                "tested the gap-day low and closed back above it" if c[j] >= gap_low else
                "failed: closed below the gap-day low")
    else:
        day2 = "pending: day 2 not traded yet"
    prior60 = (prev / c[i - 61] - 1) * 100 if i >= 61 else None
    band = band_for(st, i)
    circ = circuit_at(st, i, band)
    circ_open = circuit_at(st, i, band, "o")
    if cfg.exclude_locked and circ:
        reasons.append("upper-circuit close (--exclude-locked)")
    cat = catalyst_check(st["name"], cfg.catalysts)
    ev = {"symbol": st["name"], "date": st["date"][i], "prev_close": prev, "open": o[i], "high": h[i],
          "low": gap_low, "close": c[i], "gap_pct": _r(gap), "chg_pct": _r((c[i] / prev - 1) * 100),
          "volume": v[i], "vol_vs_50d": _r(mult), "close_pos_pct": _r(pos, 0), "day1": day1, "day2": day2,
          "holding": broke is None, "gap_filled": filled, "prior_60d_pct": _r(prior60),
          "turnover_cr": _r(liq["avg_traded_value_cr"]), "delivery": delivery_at(st, i), "band": band,
          "circuit": circ, "opened_at_circuit": bool(circ_open), "catalyst": cat,
          "entry_ref": max(h[i:i + 2]), "stop_ref": gap_low, "risk_pct": _r((c[last] - gap_low) / c[last] * 100),
          "sessions_since": last - i, "since_pct": _r((c[last] / c[i] - 1) * 100)}
    flags, notes = _india_flags(st, i, band, circ, cfg, ev)
    if circ_open and not circ:
        notes.append("Opened at the upper circuit in the pre-open auction, then the lock broke.")
    if filled:
        notes.append(f"The gap has been filled (traded back to ₹{prev:,.2f}): weaker, the news is being sold.")
    if prior60 is not None and prior60 > 40:
        notes.append(f"Already {prior60:+.0f}% in the 60 sessions before the gap: less of a surprise, more of a "
                     "climax risk.")
    # score: gap 25, volume 25, day-1 close 15, neglect 10, follow-through 15, catalyst 10
    pts = min(25.0, 15 + (gap - 5) * 2) if gap >= 5 else 10.0
    m = mult or 0
    pts += 25 if m >= 8 else 20 if m >= 5 else 15 if m >= 3 else 0
    pts += 15 if green and pos >= 60 else 8 if pos >= 40 else 0
    pts += (10 if prior60 <= 10 else 5 if prior60 <= 25 else 0) if prior60 is not None else 5
    pts += 15 if day2.startswith("follow") else 8 if day2.startswith(("holding", "tested")) else \
        5 if day2.startswith("pending") else 0
    pts += 10 if cat["supplied"] else 0
    ev["score"] = max(0, round(pts - _penalty(flags) - (10 if filled else 0)))
    ev["flags"], ev["notes"], ev["reasons"] = flags, notes, reasons
    ev["passed"] = not reasons
    return ev


# ---------------------------------------------------------------------------
# Relative strength
# ---------------------------------------------------------------------------
def weighted_return(c: list):
    rets, used = {}, []
    for k, w, lab in RS_HORIZONS:
        if len(c) > k:
            rets[lab] = (c[-1] / c[-1 - k] - 1) * 100
            used.append((w, rets[lab]))
    if not used:
        return None, rets, True
    ws = sum(w for w, _ in used)
    return sum(w * r for w, r in used) / ws, rets, len(used) < len(RS_HORIZONS)


def rs_line(st: dict, bench: dict, days: int = 5):
    bmap = {r["date"].date(): r["close"] for r in bench["rows"]}
    pairs = [(st["c"][i], bmap[d.date()]) for i, d in enumerate(st["date"]) if d.date() in bmap]
    if len(pairs) < 64:
        return None
    line = [a / b for a, b in pairs][-YEAR:]
    price = [a for a, _ in pairs][-YEAR:]
    k = min(days, len(line) - 1)
    rs_new = max(line[-k:]) >= max(line)
    px_new = max(price[-k:]) >= max(price)
    state = ("new high ahead of price" if rs_new and not px_new else
             "new high with price" if rs_new else
             "lagging: price at a high, RS line not" if px_new else "below its high")
    return {"state": state, "rs_new_high": rs_new, "price_new_high": px_new, "leading": rs_new and not px_new,
            "from_high_pct": _r((line[-1] / max(line) - 1) * 100), "matched_bars": len(pairs),
            "change_63d_pct": _r((line[-1] / line[-64] - 1) * 100) if len(line) > 63 else None}


def _rating(score: float, others: list) -> int:
    below = sum(1 for x in others if x < score)
    eq = sum(1 for x in others if x == score)
    pct = (below + 0.5 * eq) / len(others)
    return int(max(1, min(99, round(1 + 98 * pct))))


def rs_table(stocks: list, bench: dict | None, days: int = 5, min_turnover_cr: float = 5.0):
    rows, skipped = [], []
    for st in stocks:
        c, h = st["c"], st["h"]
        if len(c) < 64:
            skipped.append({"name": st["name"], "error": f"only {len(c)} bars (RS needs 64+)"})
            continue
        score, rets, partial = weighted_return(c)
        hi = max(h[-YEAR:])
        s50 = st["sma50"][-1]
        rows.append({"symbol": st["name"], "close": c[-1], "date": st["date"][-1], "weighted_return": _r(score),
                     "returns": {k: _r(x) for k, x in rets.items()}, "partial_history": partial,
                     "high_52w": hi, "from_high_pct": _r((c[-1] / hi - 1) * 100), "above_50dma": bool(s50 and c[-1] > s50),
                     "liquidity": bp.liquidity(st["rows"], min_turnover_cr),
                     "rs_line": rs_line(st, bench, days) if bench else None, "rating": None})
    scores = [r["weighted_return"] for r in rows]
    if len(rows) >= 2:
        for r in rows:
            others = list(scores)
            others.remove(r["weighted_return"])
            r["rating"] = _rating(r["weighted_return"], others)
    rows.sort(key=lambda r: (-(r["rating"] or 0), -r["weighted_return"], r["symbol"]))
    bench_rating = None
    if bench and len(rows) >= 2:
        bs, _, _ = weighted_return([r["close"] for r in bench["rows"]])
        bench_rating = _rating(bs, scores) if bs is not None else None
    return rows, skipped, bench_rating


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------
REGIME_GATES = (
    ("correction", "Correction: no new momentum trades. Keep the list for the next follow-through day.", 0),
    ("rally", "Rally attempt: one pilot at half size at most; bursts fail often before a follow-through.", 1),
    ("pressure", "Uptrend under pressure: only the top 1-2 names, reduced risk (0.5-0.75% per trade).", 2),
    ("confirmed", "Confirmed uptrend: bursts and pivots allowed within the exposure ceiling.", 5),
)


def regime_gate(text):
    if not text:
        return {"label": None, "gate": "Regime not given: run market-regime first (regime.py status) and only "
                                       "trade bursts when it allows new longs.", "max_new": None}
    t = text.lower()
    for key, msg, n in REGIME_GATES:
        if key in t:
            return {"label": text, "gate": msg, "max_new": n}
    raise ValueError(f"--regime {text!r} not recognised; use confirmed, pressure, rally or correction "
                     "(the market-regime labels)")


def _handoff(entry: dict, bench_arg: str | None) -> str:
    script = SKILLS_DIR / "breakout-planner" / "scripts" / "breakout.py"
    try:
        script_s = os.path.relpath(script)
    except ValueError:                      # different drive on Windows
        script_s = str(script)
    if script_s.startswith(".."):
        script_s = str(script)
    if entry["kind"] == "csv":
        src = f"--csv {_q(entry['source'])}"
    else:
        src = f"--symbol {entry['symbol']}"
    return f"python3 {_q(script_s)} check {src}" + (f" {bench_arg}" if bench_arg else "")


def _q(s: str) -> str:
    return f'"{s}"' if " " in s else s


def scan(stocks, bench, cfg):
    by = {st["name"]: st for st in stocks}
    rs_rows, rs_skipped, bench_rating = rs_table(stocks, bench, cfg.rs_high_days, cfg.min_turnover_cr)
    rs = {r["symbol"]: r for r in rs_rows}
    bursts, b_rej = find_events(stocks, burst_at, cfg, session_window(stocks, cfg.burst_days))
    pivots, p_rej = find_events(stocks, pivot_at, cfg, session_window(stocks, cfg.pivot_days))
    entries = {}

    def entry(name):
        st = by[name]
        return entries.setdefault(name, {"symbol": name, "close": st["c"][-1], "kind": st["kind"],
                                         "source": st["source"], "bursts": [], "pivots": [], "flags": [],
                                         "signals": []})
    for e in pivots:
        entry(e["symbol"])["pivots"].append(e)
    for e in bursts:
        entry(e["symbol"])["bursts"].append(e)
    for r in rs_rows:
        rl = r["rs_line"] or {}
        if (r["symbol"] not in entries and (r["rating"] or 0) >= cfg.leader_rs and r["from_high_pct"] >= -10
                and r["above_50dma"] and r["liquidity"]["ok"] is not False
                and (rl.get("rs_new_high") or not bench)):
            entry(r["symbol"])["leader"] = True
    out = []
    for name, e in entries.items():
        r = rs.get(name)
        rating = r["rating"] if r else None
        rl = (r or {}).get("rs_line") or {}
        sig, parts = 0, []
        for p in e["pivots"][:1]:
            sig = max(sig, p["score"] + 5)
            cat = ",".join(p["catalyst"]["supplied"]) or "catalyst not supplied"
            parts.append(f"episodic pivot {_iso(p['date'])}: gap {p['gap_pct']:+.1f}% on {p['vol_vs_50d']:.1f}x "
                         f"volume, {p['day2'].split(':')[0]}, holding above ₹{p['stop_ref']:,.2f} ({cat})")
            e["signals"].append(f"EP {p['gap_pct']:+.1f}% {p['vol_vs_50d']:.1f}x")
            e["flags"] += p["flags"]
        pivot_days = {p["date"] for p in e["pivots"][:1]}
        for b in [x for x in e["bursts"] if x["date"] not in pivot_days][:1]:
            sig = max(sig, b["score"])
            parts.append(f"burst {_iso(b['date'])}: {b['chg_pct']:+.1f}% on {b['vol_vs_50d']:.1f}x volume out of a "
                         f"{b['base_days']}-day base, closed at {b['close_pos_pct']:.0f}% of range")
            e["signals"].append(f"burst {b['chg_pct']:+.1f}%")
            e["flags"] += b["flags"]
        if e.get("leader"):
            parts.append("RS leader near its high, no trigger yet: wait for a burst or a base")
            e["signals"].append("RS leader")
        if rating is not None:
            parts.append(f"RS {rating}")
        if rl.get("leading"):
            parts.append("RS line at a new high ahead of price")
        elif rl.get("rs_new_high"):
            parts.append("RS line at a new high")
        elif rl.get("state", "").startswith("lagging"):
            parts.append("RS line lagging price")
        score = 0.6 * sig + 0.3 * (rating if rating is not None else 50)
        score += 10 if rl.get("leading") else 5 if rl.get("rs_new_high") else 0
        score += 5 if r and r["from_high_pct"] >= -10 else 0
        e["flags"] = list(dict.fromkeys(e["flags"]))
        score -= _penalty(e["flags"]) / 2           # on top of the event penalty: locked/banded names sink
        if any(f in ("UC", "UC?") or f.startswith("band") for f in e["flags"]):
            parts.append("circuit/band flag: can't be bought at the close, keep off the order list")
        e.update({"rs_rating": rating, "from_high_pct": r["from_high_pct"] if r else None,
                  "rs_line": rl.get("state"), "score": round(score, 1), "reason": "; ".join(parts)})
        if cfg.min_rs and (rating or 0) < cfg.min_rs:
            continue
        out.append(e)
    out.sort(key=lambda e: (-e["score"], e["symbol"]))
    shown = out[:cfg.top] if cfg.top else out
    bench_arg = (f"--benchmark-csv {_q(cfg.benchmark_csv)}" if cfg.benchmark_csv else
                 f"--benchmark {cfg.benchmark}" if cfg.benchmark else None)
    tradable = [e for e in shown if not any(f in ("UC", "UC?") or f.startswith("band") for f in e["flags"])]
    for e in tradable[:3]:
        e["handoff"] = _handoff(e, bench_arg)
    for e in shown:
        for k in ("bursts", "pivots"):
            e[k] = [{"date": x["date"], "score": x["score"], "entry_ref": x["entry_ref"], "stop_ref": x["stop_ref"],
                     "risk_pct": x["risk_pct"], "notes": x["notes"]} for x in e[k]]
    return {"watchlist": shown, "candidates": len(out), "bench_rating": bench_rating,
            "rejected": {"bursts": len(b_rej), "pivots": len(p_rej)}, "rs_skipped": rs_skipped}


# ---------------------------------------------------------------------------
# Printing
# ---------------------------------------------------------------------------
def _head(title, stocks, skipped, start, extra=""):
    last = max(st["date"][-1] for st in stocks).date()
    s = f"\n{title} — {start} to {last}" if start else f"\n{title} — as of {last}"
    s += f" | {len(stocks)} stocks" + (f" ({len(skipped)} skipped)" if skipped else "") + extra
    print(s)
    fr = ta.freshness([{"date": datetime.combine(last, datetime.min.time())}])
    if fr:
        print(fr)


def _skips(skipped):
    for s in skipped:
        print(f"   skipped {s['name']}: {s['error']}")


def _rejects(rej, show):
    if not rej:
        return
    if show:
        print(f"\nRejected ({len(rej)}):")
        for e in rej:
            print(f"  {_iso(e['date'])}  {e['symbol']:<14} " + "; ".join(e["reasons"]))
    else:
        print(f"\n{len(rej)} near-miss(es) rejected: " + ", ".join(sorted({e['symbol'] for e in rej}))
              + ". Use --show-rejects for reasons.")


def print_bursts(res, stocks, skipped, cfg):
    _head("Momentum bursts", stocks, skipped, res["window_start"],
          f" | ≥{cfg.min_move_pct:g}% close, rising volume, quiet base, rising 50 DMA, ≥₹{cfg.min_turnover_cr:g} cr/day")
    if not res["bursts"]:
        print("No bursts passed the filters.")
    else:
        print(f"{'Date':<11} {'Symbol':<14}{'Close':>10} {'Chg':>7} {'Vol/50d':>8} {'Close@':>7} {'Base':>9} "
              f"{'Run3d':>7} {'₹cr/d':>7} {'Score':>6}  Flags")
        for e in res["bursts"]:
            base = f"{e['base_days']}d/{e['base_width_pct']:.1f}%" if e["base_width_pct"] is not None else "—"
            cr = "—" if e["turnover_cr"] is None else f"{e['turnover_cr']:.1f}"
            print(f"{_iso(e['date']):<11} {e['symbol'][:13]:<14}{e['close']:>10,.2f} {e['chg_pct']:>+6.1f}% "
                  f"{e['vol_vs_50d'] or 0:>7.1f}x {e['close_pos_pct']:>6.0f}% {base:>9} {e['run_3d_pct']:>+6.1f}% "
                  f"{cr:>7} {e['score']:>6}  {' '.join(e['flags'])}")
        print()
        for e in res["bursts"]:
            since = (f" {e['sessions_since']} session(s) since: {e['since_pct']:+.1f}%, burst low "
                     f"{'held' if e['low_held'] else 'BROKEN'}." if e["sessions_since"] else " Burst was today.")
            dl = f" Delivery {e['delivery']['pct']:g}%." if e["delivery"] else ""
            print(f"  • {e['symbol']}: buy above {bp.px(e['entry_ref'])} (burst high), stop {bp.px(e['stop_ref'])} "
                  f"(burst low, {e['risk_pct']:.1f}% risk).{since}{dl}")
            for n in e["notes"]:
                print(f"      ⚠ {n}")
    _rejects(res["rejected"], cfg.show_rejects)
    _skips(skipped)


def print_pivots(res, stocks, skipped, cfg):
    _head("Episodic pivots", stocks, skipped, res["window_start"],
          f" | gap ≥{cfg.min_gap_pct:g}% on ≥{cfg.min_vol_mult:g}x 50-day volume, holding the gap-day low")
    if not res["pivots"]:
        print("No episodic pivots passed the filters.")
    for e in res["pivots"]:
        print(f"\n{e['symbol']} — {_iso(e['date'])}: gap {e['gap_pct']:+.1f}% (open {bp.px(e['open'])} vs prev close "
              f"{bp.px(e['prev_close'])}), day {e['chg_pct']:+.1f}%, volume {e['vol_vs_50d']:.1f}x 50-day | "
              f"score {e['score']}" + (f" | {' '.join(e['flags'])}" if e["flags"] else ""))
        print(f"  Day 1: {e['day1']} ({e['close_pos_pct']:.0f}% of range)")
        print(f"  Day 2: {e['day2']}")
        print(f"  Now  : {e['sessions_since']} session(s) later, {e['since_pct']:+.1f}% from the gap-day close; "
              f"{'holding' if e['holding'] else 'BROKEN'} above the gap-day low {bp.px(e['stop_ref'])}"
              + (f"; 60 sessions before the gap {e['prior_60d_pct']:+.1f}%" if e["prior_60d_pct"] is not None else ""))
        print(f"  Plan : buy above {bp.px(e['entry_ref'])}, stop under {bp.px(e['stop_ref'])} "
              f"({e['risk_pct']:.1f}% from the last close)")
        cat = e["catalyst"]
        print("  Catalyst: " + " ".join(f"[{'x' if x['confirmed'] else ' '}] {x['kind']}" for x in cat["checklist"]))
        for x in cat["checklist"]:
            if x["confirmed"]:
                print(f"      {x['kind']}: {x['what_to_check']}")
        if cat["note"]:
            print(f"      {cat['note']}")
        for n in e["notes"]:
            print(f"  ⚠ {n}")
    print(f"\n{PREOPEN_NOTE}")
    _rejects(res["rejected"], cfg.show_rejects)
    _skips(skipped)


def print_rs(res, stocks, skipped, bench, cfg):
    _head("RS ratings", stocks, skipped, None,
          " | weighted 3/6/9/12-month returns 40/20/20/20" + (f" | RS line vs {bench['name']}" if bench else ""))
    print(f"{'#':>3} {'Symbol':<14}{'RS':>4} {'3m':>8} {'6m':>8} {'9m':>8} {'12m':>8} {'vs 52wH':>8}  RS line")
    for k, r in enumerate(res["ratings"], 1):
        rt = "—" if r["rating"] is None else str(r["rating"])
        rets = " ".join(f"{r['returns'][lab]:>+7.1f}%" if lab in r["returns"] else f"{'—':>8}"
                        for _, _, lab in RS_HORIZONS)
        rl = r["rs_line"]["state"] if r["rs_line"] else "—"
        print(f"{k:>3} {r['symbol'][:13]:<14}{rt:>4} {rets} {r['from_high_pct']:>+7.1f}%  {rl}"
              + ("  (partial history)" if r["partial_history"] else "")
              + ("  (illiquid)" if r["liquidity"]["ok"] is False else ""))
    if res["bench_rating"] is not None:
        print(f"\n{bench['name']} itself would rate {res['bench_rating']} in this universe.")
    if len(res["ratings"]) < 50:
        print(f"Ratings rank only these {len(res['ratings'])} names. Use a broad list (NIFTY 500 or your full "
              "watchlist) for a market-wide reading.")
    _skips(skipped + res["skipped"])


def print_scan(res, stocks, skipped, cfg):
    _head("Momentum watchlist", stocks, skipped, None,
          f" | {res['candidates']} candidate(s), top {len(res['watchlist'])}")
    print(res["regime"]["gate"])
    if not res["watchlist"]:
        print("Nothing on the watchlist: no bursts, pivots or RS leaders passed the filters.")
    else:
        print(f"{'#':>3} {'Symbol':<14}{'Close':>10} {'RS':>4} {'vs 52wH':>8} {'Score':>6}  Signals")
        for k, e in enumerate(res["watchlist"], 1):
            rt = "—" if e["rs_rating"] is None else str(e["rs_rating"])
            fh = "—" if e["from_high_pct"] is None else f"{e['from_high_pct']:+.1f}%"
            print(f"{k:>3} {e['symbol'][:13]:<14}{e['close']:>10,.2f} {rt:>4} {fh:>8} {e['score']:>6}  "
                  + ", ".join(e["signals"]) + (f"  [{' '.join(e['flags'])}]" if e["flags"] else ""))
        print("\nWhy:")
        for k, e in enumerate(res["watchlist"], 1):
            print(f"{k:>3}. {e['symbol']}: {e['reason']}.")
        print("\nNext: find the base and plan the order with breakout-planner for the top names:")
        for e in [x for x in res["watchlist"] if x.get("handoff")]:
            print(f"  {e['handoff']}")
        print("Then size with position-sizing (risk.py) and run the trading-discipline gate before any order.")
    rj = res["rejected"]
    if rj["bursts"] or rj["pivots"]:
        print(f"\nRejected near-misses: {rj['bursts']} burst(s), {rj['pivots']} pivot(s). Run burst/pivot "
              "--show-rejects for reasons.")
    _skips(skipped)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _parents():
    u = argparse.ArgumentParser(add_help=False)
    g = u.add_argument_group("universe")
    g.add_argument("--csv-dir", help="folder with one OHLCV CSV per symbol (file name = symbol)")
    g.add_argument("--pattern", default="*.csv", help="glob inside --csv-dir (default *.csv)")
    g.add_argument("--csv", action="append", help="one stock CSV (repeatable)")
    g.add_argument("--symbols", help="comma list fetched via yfinance")
    g.add_argument("--exchange", default="NSE", choices=["NSE", "BSE"])
    g.add_argument("--period", default="2y", help="yfinance history period")
    g.add_argument("--benchmark-csv", help="NIFTY (or sector index) CSV for the RS line")
    g.add_argument("--benchmark", help="benchmark via yfinance, e.g. NIFTY, NIFTYMIDCAP150")
    g.add_argument("--as-of", help="evaluate as of YYYY-MM-DD, ignoring later bars")
    g.add_argument("--min-turnover-cr", type=float, default=5.0, help="min avg daily traded value, ₹ crore (5)")
    g.add_argument("--bands", help="CSV SYMBOL,band or JSON {SYMBOL: band}; band 2/5/10/20, 0 = no fixed band (F&O)")
    g.add_argument("--json", action="store_true")
    ev = argparse.ArgumentParser(add_help=False)
    ev.add_argument("--days", type=int, help="sessions to look back (burst 5, pivot 10)")
    ev.add_argument("--show-rejects", action="store_true", help="list near-misses and why they failed")
    ev.add_argument("--exclude-locked", action="store_true", help="reject upper-circuit closes instead of flagging")
    bu = argparse.ArgumentParser(add_help=False)
    g = bu.add_argument_group("burst rules")
    g.add_argument("--min-move-pct", type=float, default=4.0, help="min close-to-close gain %% (4)")
    g.add_argument("--min-close-pos", type=float, default=70.0, help="close in the top part of the range, %% (70)")
    g.add_argument("--max-run-pct", type=float, default=6.0, help="max gain in the 3 sessions before, %% (6)")
    g.add_argument("--max-base-pct", type=float, default=8.0, help="max high-low width of the quiet base, %% (8)")
    g.add_argument("--min-base-days", type=int, default=3, help="min quiet sessions before the burst (3)")
    g.add_argument("--max-range-ratio", type=float, default=1.0,
                   help="base daily range vs the 20 sessions before it (1.0 = no wider)")
    g.add_argument("--min-delivery-pct", type=float, help="reject bursts below this delivery %% (needs the column)")
    pv = argparse.ArgumentParser(add_help=False)
    g = pv.add_argument_group("pivot rules")
    g.add_argument("--min-gap-pct", type=float, default=5.0, help="min opening gap vs previous close, %% (5)")
    g.add_argument("--min-vol-mult", type=float, default=3.0, help="min gap-day volume vs 50-day average (3)")
    g.add_argument("--catalyst", action="append", metavar="[SYMBOL=]KIND[,KIND]",
                   help=f"news you have verified: {', '.join(CATALYSTS)} (repeatable)")
    rs = argparse.ArgumentParser(add_help=False)
    g = rs.add_argument_group("relative strength")
    g.add_argument("--rs-high-days", type=int, default=5, help="sessions in which a new RS-line high counts (5)")
    g.add_argument("--min-rs", type=int, default=0, help="hide names rated below this")
    return u, ev, bu, pv, rs


def main(argv=None) -> int:
    u, ev, bu, pv, rs = _parents()
    p = argparse.ArgumentParser(description="Momentum bursts, episodic pivots and RS ratings for NSE/BSE stocks")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("burst", parents=[u, ev, bu], help="momentum-burst days across the universe")
    sub.add_parser("pivot", parents=[u, ev, pv], help="episodic-pivot gaps on heavy volume")
    r = sub.add_parser("rs", parents=[u, rs], help="1-99 relative-strength ratings")
    r.add_argument("--top", type=int, default=0, help="show only the top N")
    s = sub.add_parser("scan", parents=[u, ev, bu, pv, rs], help="ranked watchlist from all three")
    s.add_argument("--top", type=int, default=10, help="watchlist length (10; 0 = all)")
    s.add_argument("--leader-rs", type=int, default=85, help="RS rating for a no-trigger leader to list (85)")
    s.add_argument("--regime", help="market-regime label: confirmed, pressure, rally or correction")
    a = p.parse_args(argv)
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


def _validate(a):
    for k in ("min_turnover_cr", "min_move_pct", "min_gap_pct", "min_vol_mult", "max_base_pct", "max_range_ratio"):
        if getattr(a, k, None) is not None and getattr(a, k) < 0:
            raise ValueError(f"--{k.replace('_', '-')} must be ≥ 0")
    if getattr(a, "days", None) is not None and a.days < 1:
        raise ValueError("--days must be ≥ 1")
    if getattr(a, "top", 0) is not None and getattr(a, "top", 0) < 0:
        raise ValueError("--top must be ≥ 0")
    if getattr(a, "min_close_pos", None) is not None and not 0 <= a.min_close_pos <= 100:
        raise ValueError("--min-close-pos is a percentage 0-100")
    if getattr(a, "rs_high_days", 1) < 1:
        raise ValueError("--rs-high-days must be ≥ 1")


def _run(a) -> int:
    _validate(a)
    a.as_of = parse_as_of(a.as_of)
    a.catalysts = parse_catalysts(getattr(a, "catalyst", None))
    bands = load_bands(a.bands) if a.bands else {}
    gate = regime_gate(a.regime) if a.cmd == "scan" else None
    bench = load_benchmark(a)
    stocks, skipped = load_universe(a, bands)
    for k, v in (("min_move_pct", 4.0), ("min_close_pos", 70.0), ("max_run_pct", 6.0), ("max_base_pct", 8.0),
                 ("min_base_days", 3), ("max_range_ratio", 1.0), ("min_delivery_pct", None), ("min_gap_pct", 5.0),
                 ("min_vol_mult", 3.0), ("rs_high_days", 5), ("min_rs", 0), ("show_rejects", False),
                 ("exclude_locked", False)):
        if not hasattr(a, k):
            setattr(a, k, v)
    meta = {"as_of": max(st["date"][-1] for st in stocks), "universe": [st["name"] for st in stocks],
            "skipped": skipped, "benchmark": bench["name"] if bench else None}

    if a.cmd == "burst":
        start = session_window(stocks, a.days or 5)
        hits, rej = find_events(stocks, burst_at, a, start)
        res = {**meta, "window_start": start, "bursts": hits, "rejected": rej,
               "rules": {"min_move_pct": a.min_move_pct, "min_close_pos": a.min_close_pos,
                         "max_run_pct": a.max_run_pct, "max_base_pct": a.max_base_pct,
                         "min_base_days": a.min_base_days, "max_range_ratio": a.max_range_ratio,
                         "min_turnover_cr": a.min_turnover_cr}}
        print(dump(res)) if a.json else print_bursts(res, stocks, skipped, a)
        return 0

    if a.cmd == "pivot":
        start = session_window(stocks, a.days or 10)
        hits, rej = find_events(stocks, pivot_at, a, start)
        res = {**meta, "window_start": start, "pivots": hits, "rejected": rej, "preopen_note": PREOPEN_NOTE,
               "rules": {"min_gap_pct": a.min_gap_pct, "min_vol_mult": a.min_vol_mult,
                         "min_turnover_cr": a.min_turnover_cr}}
        print(dump(res)) if a.json else print_pivots(res, stocks, skipped, a)
        return 0

    if a.cmd == "rs":
        rows, rs_skipped, bench_rating = rs_table(stocks, bench, a.rs_high_days, a.min_turnover_cr)
        if not rows:
            raise ValueError("no stock has the 64+ bars an RS rating needs")
        rows = [r for r in rows if not a.min_rs or (r["rating"] or 0) >= a.min_rs]
        res = {**meta, "weights": {lab: w for _, w, lab in RS_HORIZONS}, "ratings": rows[:a.top] if a.top else rows,
               "bench_rating": bench_rating, "skipped": skipped + rs_skipped}
        if a.json:
            print(dump(res))
        else:
            res["skipped"] = rs_skipped
            print_rs(res, stocks, skipped, bench, a)
        return 0

    # scan
    a.burst_days = a.days or 5
    a.pivot_days = a.days or 10
    res = {**meta, **scan(stocks, bench, a), "regime": gate}
    print(dump(res)) if a.json else print_scan(res, stocks, skipped, a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
