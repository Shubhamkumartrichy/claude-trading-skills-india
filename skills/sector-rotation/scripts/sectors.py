#!/usr/bin/env python3
"""Sector rotation for NSE sectoral and thematic indices, measured against NIFTY 50.

Zero dependencies (Python 3.10+ stdlib). Optional: `pip install yfinance` to fetch index history.
Reuses the loaders and moving averages from ../../technical-analysis/scripts/ta.py.

Input (pick one)
  --csv-dir DIR     one CSV per index, file stem = index name (e.g. "NIFTY IT.csv"), plus a
                    benchmark file ("NIFTY 50.csv" / "NIFTY.csv"). Needs a date column and a close
                    column; Open/High/Low/Volume are optional (niftyindices.com exports have no volume).
  (default)         fetch the built-in universe from Yahoo Finance via yfinance

Sub-commands
  rotation   ranked table: returns, relative strength, RS-Ratio/RS-Momentum quadrant, recent
             quadrant changes, distance from 50/200 DMA, trend, cyclical-vs-defensive tilt and
             size-factor read, plus a one-paragraph summary (default)
  rrg        the weekly RS-Ratio / RS-Momentum trail for each index (for plotting)
  breadth    % of each sector's member stocks above their 50/200 DMA (--members + --csv-dir)
  universe   list the built-in indices, Yahoo tickers and whether Yahoo serves their history

Examples
  python3 sectors.py rotation
  python3 sectors.py rotation --csv-dir nse_indices/ --weeks 12 --json
  python3 sectors.py rrg --sectors IT,BANK,METAL --tail 10
  python3 sectors.py breadth --members members.csv --csv-dir stocks/
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import math
import re
import statistics
import sys
from datetime import datetime
from pathlib import Path

_TA_PATH = Path(__file__).resolve().parents[2] / "technical-analysis" / "scripts" / "ta.py"
_spec = importlib.util.spec_from_file_location("sector_rotation_ta", _TA_PATH)
ta = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ta)


class SectorError(Exception):
    """Bad input the user can fix; reported as a one-line error with exit code 2."""


# ---------------------------------------------------------------------------
# Universe
# ---------------------------------------------------------------------------
# yahoo = "history": Yahoo's chart API returned a year of daily bars when probed on 2026-10-31.
# yahoo = "quote-only": the symbol resolves but Yahoo served only the latest quote (no history),
# so use --csv-dir with an NSE download, or override the ticker with --ticker/--map.
#
# group drives the rotation read:
#   cyclical  – domestic-growth / high-beta: earnings swing with credit, capex and commodity cycles
#   defensive – earnings less tied to the Indian cycle (export earners, staples, healthcare)
#   financial – rate- and FII-flow-driven heavyweights; reported, but kept out of the tilt
#   commodity – crude/gas linked; moves with oil more than with the domestic cycle
#   theme     – policy or thematic baskets (PSU, defence, consumption, capital markets, MNC, media)
#   size      – broad-market size factors, read separately
BENCHMARK = {"key": "NIFTY", "label": "NIFTY 50", "ticker": "^NSEI"}

UNIVERSE = [
    # key, label, Yahoo ticker, group, Yahoo status, extra aliases (normalised, NIFTY prefix stripped)
    ("BANK", "NIFTY BANK", "^NSEBANK", "financial", "history", ("BANKNIFTY", "NSEBANK")),
    ("PVTBANK", "NIFTY PRIVATE BANK", "NIFTY_PVT_BANK.NS", "financial", "quote-only", ("PRIVATEBANK",)),
    ("FINSERV", "NIFTY FINANCIAL SERVICES", "NIFTY_FIN_SERVICE.NS", "financial", "history",
     ("FINSERVICE", "FINANCIALSERVICES", "FINNIFTY")),
    ("PSUBANK", "NIFTY PSU BANK", "^CNXPSUBANK", "cyclical", "history", ("CNXPSUBANK",)),
    ("AUTO", "NIFTY AUTO", "^CNXAUTO", "cyclical", "history", ("CNXAUTO",)),
    ("METAL", "NIFTY METAL", "^CNXMETAL", "cyclical", "history", ("CNXMETAL",)),
    ("REALTY", "NIFTY REALTY", "^CNXREALTY", "cyclical", "history", ("CNXREALTY",)),
    ("INFRA", "NIFTY INFRASTRUCTURE", "^CNXINFRA", "cyclical", "history", ("INFRASTRUCTURE", "CNXINFRA")),
    ("IT", "NIFTY IT", "^CNXIT", "defensive", "history", ("CNXIT",)),
    ("PHARMA", "NIFTY PHARMA", "^CNXPHARMA", "defensive", "history", ("CNXPHARMA",)),
    ("HEALTHCARE", "NIFTY HEALTHCARE", "NIFTY_HEALTHCARE.NS", "defensive", "quote-only", ("HEALTHCAREINDEX",)),
    ("FMCG", "NIFTY FMCG", "^CNXFMCG", "defensive", "history", ("CNXFMCG",)),
    ("ENERGY", "NIFTY ENERGY", "^CNXENERGY", "commodity", "history", ("CNXENERGY",)),
    ("OILGAS", "NIFTY OIL & GAS", "NIFTY_OIL_AND_GAS.NS", "commodity", "quote-only", ("OILANDGAS",)),
    ("PSE", "NIFTY PSE", "^CNXPSE", "theme", "history", ("CNXPSE",)),
    ("CPSE", "NIFTY CPSE", "NIFTY_CPSE.NS", "theme", "quote-only", ()),
    ("DEFENCE", "NIFTY INDIA DEFENCE", "NIFTY_IND_DEFENCE.NS", "theme", "quote-only",
     ("INDIADEFENCE", "INDDEFENCE")),
    ("CAPMKT", "NIFTY CAPITAL MARKETS", "NIFTY_CAPITAL_MKT.NS", "theme", "quote-only",
     ("CAPITALMARKETS", "CAPITALMKT")),
    ("CONSUMPTION", "NIFTY INDIA CONSUMPTION", "^CNXCONSUM", "theme", "history",
     ("INDIACONSUMPTION", "CNXCONSUMPTION", "CONSUM")),
    ("MNC", "NIFTY MNC", "^CNXMNC", "theme", "history", ("CNXMNC",)),
    ("MEDIA", "NIFTY MEDIA", "^CNXMEDIA", "theme", "quote-only", ("CNXMEDIA",)),
    ("MIDCAP150", "NIFTY MIDCAP 150", "NIFTYMIDCAP150.NS", "size", "history", ()),
    ("SMALLCAP250", "NIFTY SMALLCAP 250", "NIFTYSMLCAP250.NS", "size", "history", ("SMLCAP250",)),
    ("MIDCAP100", "NIFTY MIDCAP 100", "NIFTY_MIDCAP_100.NS", "size", "quote-only", ("CNXMIDCAP",)),
    ("SMALLCAP100", "NIFTY SMALLCAP 100", "^CNXSC", "size", "quote-only", ("SMLCAP100", "CNXSC")),
]
INFO = {u[0]: {"key": u[0], "label": u[1], "ticker": u[2], "group": u[3], "yahoo": u[4]} for u in UNIVERSE}

QUADRANTS = ("Leading", "Weakening", "Lagging", "Improving")
MIN_DAILY_BARS = 30
MIN_WEEKLY_BARS = 12


def norm(name: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", name.upper())


def _alias_table() -> dict:
    table = {}
    for key, label, _t, _g, _y, extra in UNIVERSE:
        for a in (key, norm(label), *extra):
            a = norm(a)
            table[a] = key
            if a.startswith("NIFTY") and len(a) > 5:
                table[a[5:]] = key
    return table


ALIASES = _alias_table()


def is_benchmark(name: str) -> bool:
    return norm(name) in {"NIFTY", "NIFTY50", "NSEI", "CNXNIFTY"}


def resolve(name: str) -> str | None:
    """Map a file stem or user label ("NIFTY PSU BANK", "nifty_it", "FINNIFTY") to a universe key."""
    n = norm(name)
    if n in ALIASES:
        return ALIASES[n]
    if n.startswith("NIFTY") and n[5:] in ALIASES:
        return ALIASES[n[5:]]
    return None


def guess_group(name: str) -> str:
    n = norm(name)
    return "size" if any(t in n for t in ("MIDCAP", "SMLCAP", "SMALLCAP", "MICROCAP", "LARGEMID")) else "other"


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
DATE_COLS = ("date", "index date", "historicaldate", "timestamp", "datetime", "time")
CLOSE_COLS = ("close", "closing index value", "close price", "closing price", "ltp", "price", "value",
              "total returns index")


def load_closes(path: str | Path) -> list:
    """Read a CSV into [(date, close)]. Only a date and a close column are required."""
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        cols = {c.strip().lower(): c for c in reader.fieldnames or [] if c}
        cd = next((cols[c] for c in DATE_COLS if c in cols), None)
        cc = next((cols[c] for c in CLOSE_COLS if c in cols), None)
        if not cd or not cc:
            raise SectorError(f"{Path(path).name}: need a date column and a close column; found {list(cols)}")
        out = {}
        for r in reader:
            raw = (r.get(cd) or "").strip()
            if not raw:
                continue
            try:
                d = _parse_date(raw)
                c = ta._f(r.get(cc) or "")
            except ValueError:
                continue
            if not math.isnan(c) and c > 0:
                out[d] = c
    return sorted(out.items())


def _parse_date(raw: str):
    try:
        return ta.parse_ts(raw).date()
    except ValueError:
        for fmt in ("%b %d, %Y", "%d-%b-%y", "%d %b %y", "%d-%B-%Y", "%d %B %Y"):
            try:
                return datetime.strptime(raw, fmt).date()
            except ValueError:
                continue
    raise ValueError(raw)


def load_csv_dir(directory: str, benchmark: str = "NIFTY") -> tuple:
    d = Path(directory)
    if not d.is_dir():
        raise SectorError(f"--csv-dir {directory}: not a directory")
    files = sorted(p for p in d.iterdir() if p.suffix.lower() == ".csv")
    if not files:
        raise SectorError(f"--csv-dir {directory}: no .csv files")
    bench_file, series = None, {}
    for p in files:
        stem = p.stem
        if (benchmark.upper() == "NIFTY" and is_benchmark(stem)) or norm(stem) == norm(benchmark):
            bench_file = p
            continue
        series[stem] = p
    if bench_file is None:
        raise SectorError(f"--csv-dir {directory}: no benchmark file (expected e.g. 'NIFTY 50.csv' or "
                          f"'{benchmark}.csv'; set --benchmark to its file name)")
    bench = load_closes(bench_file)
    loaded = {}
    for stem, p in series.items():
        key = resolve(stem) or stem.upper()
        loaded[key] = {"key": key, "label": INFO[key]["label"] if key in INFO else stem,
                       "group": INFO[key]["group"] if key in INFO else guess_group(stem),
                       "source": p.name, "closes": load_closes(p)}
    return {"key": norm(bench_file.stem), "label": bench_file.stem, "source": bench_file.name,
            "closes": bench}, loaded


def fetch_universe(keys: list, overrides: dict, period: str, warnings: list) -> tuple:
    def get(ticker):
        try:
            rows = ta.fetch_yf(ticker, period=period)
        except SystemExit as e:
            msg = str(e)
            if "not installed" in msg:
                raise SectorError("yfinance not installed: pip install yfinance  (or pass --csv-dir)")
            return [], msg
        return [(r["date"].date(), r["close"]) for r in rows if r["close"] > 0], None

    bench_ticker = overrides.get("NIFTY", BENCHMARK["ticker"])
    bench, err = get(bench_ticker)
    if not bench:
        raise SectorError(f"could not fetch benchmark {bench_ticker}: {err}")
    loaded = {}
    for key in keys:
        info = INFO.get(key, {"label": key, "group": guess_group(key), "yahoo": "user"})
        ticker = overrides.get(key, info.get("ticker"))
        if not ticker:
            warnings.append(f"{key}: no ticker known; add one with --ticker {key}=TICKER")
            continue
        closes, err = get(ticker)
        if len(closes) < MIN_DAILY_BARS and key not in overrides and info.get("yahoo") == "quote-only":
            warnings.append(f"{key} ({ticker}): Yahoo serves no history for this index; "
                            "use --csv-dir with an NSE download or --ticker to override")
            continue
        loaded[key] = {"key": key, "label": info["label"], "group": info["group"],
                       "source": ticker, "closes": closes}
    label = BENCHMARK["label"] if bench_ticker == BENCHMARK["ticker"] else bench_ticker
    return {"key": "NIFTY", "label": label, "source": bench_ticker, "closes": bench}, loaded


def parse_overrides(pairs: list, map_file: str | None) -> dict:
    out = {}
    if map_file:
        p = Path(map_file)
        if not p.is_file():
            raise SectorError(f"--map {map_file}: file not found")
        text = p.read_text(encoding="utf-8-sig")
        if p.suffix.lower() == ".json":
            try:
                data = json.loads(text)
            except json.JSONDecodeError as e:
                raise SectorError(f"--map {map_file}: invalid JSON ({e.msg})")
            if not isinstance(data, dict):
                raise SectorError("--map JSON must be an object {\"NAME\": \"TICKER\"}")
            items = data.items()
        else:
            rows = list(csv.reader(text.splitlines()))
            items = [(r[0], r[1]) for r in rows if len(r) >= 2 and r[0].strip().lower() not in ("name", "index", "key")]
        for k, v in items:
            out[resolve(str(k)) or norm(str(k))] = str(v).strip()
    for pair in pairs or []:
        if "=" not in pair or not pair.split("=", 1)[1].strip():
            raise SectorError(f"--ticker expects NAME=TICKER, got {pair!r}")
        k, v = pair.split("=", 1)
        out[("NIFTY" if is_benchmark(k) else resolve(k) or norm(k))] = v.strip()
    return out


# ---------------------------------------------------------------------------
# Maths
# ---------------------------------------------------------------------------
def pct_change(vals: list, n: int):
    if n <= 0 or len(vals) <= n or not vals[-1 - n]:
        return None
    return (vals[-1] / vals[-1 - n] - 1) * 100


def relative_return(sector_ret, bench_ret):
    """Out/under-performance in % terms, consistent with the ratio line: (1+s)/(1+b) - 1."""
    if sector_ret is None or bench_ret is None:
        return None
    return ((1 + sector_ret / 100) / (1 + bench_ret / 100) - 1) * 100


def align(a: list, b: list) -> tuple:
    bm = dict(b)
    dates = [d for d, _ in a if d in bm]
    am = dict(a)
    return dates, [am[d] for d in dates], [bm[d] for d in dates]


def frequency(dates: list) -> str:
    if len(dates) < 3:
        return "daily"
    gap = statistics.median((dates[i] - dates[i - 1]).days for i in range(1, len(dates)))
    if gap <= 3:
        return "daily"
    if gap <= 8:
        return "weekly"
    raise SectorError("data looks monthly or sparser; use daily (preferred) or weekly closes")


def to_weekly(dates: list, *series) -> tuple:
    """Last observation of each ISO week, for every series in step."""
    idx = {}
    for i, d in enumerate(dates):
        idx[tuple(d.isocalendar()[:2])] = i
    keep = [idx[k] for k in sorted(idx)]
    return ([dates[i] for i in keep],) + tuple([s[i] for i in keep] for s in series)


def rrg_series(sector: list, bench: list, smooth: int = 3, window: int = 10, mom: int = 4) -> tuple:
    """RS-Ratio and RS-Momentum, both centred on 100 (weekly closes in, lists aligned to input out).

    rs       = 100 * sector / benchmark                     (ratio line)
    rs_s     = EMA(rs, smooth)                              (take the weekly noise out)
    RS-Ratio = 100 * rs_s / SMA(rs_s, window)               (> 100: relative strength above its own trend)
    RS-Mom   = 100 * RS-Ratio_t / RS-Ratio_(t - mom)        (> 100: that relative trend is improving)

    Both are plain percentages (101 = 1% above), not volatility-scaled, so a high-beta index such as
    REALTY swings wider than FMCG. Read quadrant and direction, not the raw distance from 100.
    """
    rs = [100 * s / b for s, b in zip(sector, bench)]
    rs_s = ta.ema(rs, smooth) if smooth > 1 else rs
    base = ta.sma(rs_s, window)
    ratio = [100 * x / m if x is not None and m else None for x, m in zip(rs_s, base)]
    momentum = [None] * len(ratio)
    for i in range(mom, len(ratio)):
        if ratio[i] is not None and ratio[i - mom]:
            momentum[i] = 100 * ratio[i] / ratio[i - mom]
    return ratio, momentum


def quadrant(ratio, momentum):
    if ratio is None or momentum is None:
        return None
    if ratio >= 100:
        return "Leading" if momentum >= 100 else "Weakening"
    return "Improving" if momentum >= 100 else "Lagging"


def transitions(dates: list, ratio: list, momentum: list, weeks: int) -> tuple:
    """Quadrant changes inside the last `weeks` weekly bars, and weeks spent in the current quadrant."""
    qs = [quadrant(r, m) for r, m in zip(ratio, momentum)]
    changes = []
    start = max(1, len(qs) - weeks)
    for i in range(start, len(qs)):
        if qs[i] and qs[i - 1] and qs[i] != qs[i - 1]:
            changes.append({"week": dates[i].isoformat(), "from": qs[i - 1], "to": qs[i]})
    held = 0
    for q in reversed(qs):
        if q is None or q != qs[-1]:
            break
        held += 1
    return changes, held


def trend_state(closes: list, fast: int, slow: int) -> dict:
    s_fast, s_slow = ta.sma(closes, fast), ta.sma(closes, slow)
    c, f, s = closes[-1], s_fast[-1], s_slow[-1]
    back = max(2, fast // 5)
    f_prev = s_fast[-1 - back] if len(s_fast) > back else None
    out = {"dist_fast_pct": None if f is None else (c / f - 1) * 100,
           "dist_slow_pct": None if s is None else (c / s - 1) * 100}
    if f is None:
        out["trend"] = "n/a"
    elif s is None:
        out["trend"] = "Above fast MA" if c > f else "Below fast MA"
    else:
        rising = f_prev is not None and f > f_prev
        if c > f > s and rising:
            out["trend"] = "Uptrend"
        elif c < f < s and not rising:
            out["trend"] = "Downtrend"
        elif c > f and f <= s:
            out["trend"] = "Recovering"
        elif c < f and f >= s:
            out["trend"] = "Pullback"
        else:
            out["trend"] = "Mixed"
    return out


def analyse_one(item: dict, bench: list, freq: str, p: dict) -> dict:
    dates, s, b = align(item["closes"], bench)
    res = {k: item[k] for k in ("key", "label", "group", "source")}
    min_bars = MIN_DAILY_BARS if freq == "daily" else MIN_WEEKLY_BARS
    if len(dates) < min_bars:
        res["skipped"] = f"only {len(dates)} bars overlap the benchmark (need {min_bars}+)"
        return res
    periods = {"1W": 5, "1M": 21, "3M": 63, "6M": 126, "12M": 252} if freq == "daily" else \
              {"1W": 1, "1M": 4, "3M": 13, "6M": 26, "12M": 52}
    res["last_date"] = dates[-1].isoformat()
    res["close"] = s[-1]
    res["returns"] = {k: pct_change(s, n) for k, n in periods.items()}
    res["relative"] = {k: relative_return(res["returns"][k], pct_change(b, n)) for k, n in periods.items()}
    own = [c for _, c in item["closes"]]
    fast, slow = (50, 200) if freq == "daily" else (10, 40)
    res.update(trend_state(own, fast, slow))
    wd, ws, wb = (dates, s, b) if freq == "weekly" else to_weekly(dates, s, b)
    ratio, momentum = rrg_series(ws, wb, p["smooth"], p["window"], p["momentum"])
    res["rs_ratio"], res["rs_momentum"] = ratio[-1], momentum[-1]
    res["quadrant"] = quadrant(ratio[-1], momentum[-1])
    res["transitions"], res["weeks_in_quadrant"] = transitions(wd, ratio, momentum, p["weeks"])
    res["score"] = None if res["quadrant"] is None else (ratio[-1] - 100) + (momentum[-1] - 100)
    res["tail"] = [{"week": wd[i].isoformat(), "rs_ratio": ratio[i], "rs_momentum": momentum[i],
                    "quadrant": quadrant(ratio[i], momentum[i])}
                   for i in range(max(0, len(wd) - p["tail"]), len(wd)) if ratio[i] is not None]
    if res["quadrant"] is None:
        res["note"] = f"RRG needs ~{p['smooth'] + p['window'] + p['momentum']} weekly bars"
    return res


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def rotation_read(rows: list) -> dict:
    """Cyclical-vs-defensive tilt from RS-Ratio and 3-month relative return."""
    cyc = [r for r in rows if r.get("group") == "cyclical" and r.get("rs_ratio") is not None]
    dfn = [r for r in rows if r.get("group") == "defensive" and r.get("rs_ratio") is not None]
    out = {"cyclicals": [r["key"] for r in cyc], "defensives": [r["key"] for r in dfn]}
    if not cyc or not dfn:
        out.update(tilt="n/a", reason="need at least one cyclical and one defensive index with RRG data")
        return out
    s_ratio = _mean(r["rs_ratio"] for r in cyc) - _mean(r["rs_ratio"] for r in dfn)
    c3, d3 = _mean(r["relative"]["3M"] for r in cyc), _mean(r["relative"]["3M"] for r in dfn)
    s_3m = None if c3 is None or d3 is None else c3 - d3
    out["rs_ratio_spread"], out["rel_3m_spread_pct"] = s_ratio, s_3m
    agree = s_3m is None or (s_3m > 0) == (s_ratio > 0)
    if s_ratio > 0.5 and agree:
        out["tilt"] = "Risk-on"
        out["reason"] = "cyclicals have stronger relative trends than defensives"
    elif s_ratio < -0.5 and agree:
        out["tilt"] = "Defensive"
        out["reason"] = "defensives have stronger relative trends than cyclicals"
    else:
        out["tilt"] = "Mixed"
        out["reason"] = "no clear cyclical or defensive leadership (spread small or signals disagree)"
    return out


def size_read(rows: list) -> dict:
    sz = [r for r in rows if r.get("group") == "size" and r.get("rs_ratio") is not None]
    if not sz:
        return {"read": "n/a", "reason": "no midcap/smallcap index loaded"}
    up = all(r["rs_ratio"] >= 100 and (r["relative"]["3M"] or 0) > 0 for r in sz)
    down = all(r["rs_ratio"] < 100 and (r["relative"]["3M"] or 0) < 0 for r in sz)
    read = "Broadening" if up else "Narrowing" if down else "Mixed"
    reason = {"Broadening": "mid/small caps outperforming NIFTY: risk appetite is broad",
              "Narrowing": "mid/small caps lagging NIFTY: money hiding in large caps",
              "Mixed": "size factors disagree; check which one is turning"}[read]
    return {"read": read, "reason": reason,
            "factors": [{"key": r["key"], "quadrant": r["quadrant"], "rel_3m_pct": r["relative"]["3M"]}
                        for r in sz]}


def narrative(sectors: list, tilt: dict, size: dict, weeks: int) -> str:
    by_q = {q: [r["key"] for r in sectors if r.get("quadrant") == q] for q in QUADRANTS}
    parts = []
    for q, verb in (("Leading", "Leading"), ("Improving", "Improving (watch for a move into Leading)"),
                    ("Weakening", "Weakening (losing momentum)"), ("Lagging", "Lagging")):
        if by_q[q]:
            parts.append(f"{verb}: {', '.join(by_q[q])}.")
    if tilt.get("tilt") not in (None, "n/a"):
        parts.append(f"Tilt: {tilt['tilt']} ({tilt['reason']}).")
    if size.get("read") not in (None, "n/a"):
        parts.append(f"Size: {size['read']} ({size['reason']}).")
    # Only the changes a trader acts on: entering Leading/Improving, or dropping out of Leading.
    moves = [f"{r['key']} {t['from']}→{t['to']} ({t['week']})" for r in sectors
             for t in r.get("transitions", [])[-1:]
             if t["to"] in ("Leading", "Improving") or t["from"] == "Leading"]
    if moves:
        parts.append(f"Key quadrant changes in the last {weeks} weeks: " + "; ".join(moves) + ".")
    return " ".join(parts) or "Not enough data for a rotation read."


def analyse(bench: dict, loaded: dict, params: dict, warnings: list) -> dict:
    if len(bench["closes"]) < MIN_WEEKLY_BARS:
        raise SectorError(f"benchmark has only {len(bench['closes'])} rows")
    freq = frequency([d for d, _ in bench["closes"]])
    last = bench["closes"][-1][0]
    stale = ta.freshness([{"date": datetime(last.year, last.month, last.day)}])
    if stale:
        warnings.append(stale)
    rows, skipped = [], []
    for item in loaded.values():
        r = analyse_one(item, bench["closes"], freq, params)
        if "skipped" in r:
            skipped.append({"key": r["key"], "reason": r["skipped"]})
            continue
        if (last - datetime.fromisoformat(r["last_date"]).date()).days > 3:
            warnings.append(f"{r['key']}: data ends {r['last_date']}, benchmark {last} (gap in source)")
        rows.append(r)
    if not rows:
        raise SectorError("no index had enough history overlapping the benchmark")
    rows.sort(key=lambda r: (r["score"] is None, -(r["score"] or 0)))
    sectors = [r for r in rows if r["group"] != "size"]
    size = [r for r in rows if r["group"] == "size"]
    tilt, sread = rotation_read(sectors), size_read(size)
    return {"benchmark": {"label": bench["label"], "source": bench["source"], "last_date": last.isoformat()},
            "frequency": freq, "params": params, "sectors": sectors, "size": size,
            "rotation": tilt, "size_read": sread,
            "summary": narrative(sectors, tilt, sread, params["weeks"]),
            "skipped": skipped, "warnings": warnings}


# ---------------------------------------------------------------------------
# Breadth
# ---------------------------------------------------------------------------
def load_members(path: str) -> dict:
    p = Path(path)
    if not p.is_file():
        raise SectorError(f"--members {path}: file not found")
    with open(p, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        cols = {c.strip().lower(): c for c in reader.fieldnames or [] if c}
        cs = next((cols[c] for c in ("sector", "index", "industry", "group") if c in cols), None)
        cy = next((cols[c] for c in ("symbol", "ticker", "stock", "tradingsymbol") if c in cols), None)
        if not cs or not cy:
            raise SectorError(f"--members needs 'sector' and 'symbol' columns; found {list(cols)}")
        out = {}
        for r in reader:
            sec, sym = (r.get(cs) or "").strip(), (r.get(cy) or "").strip().upper()
            if sec and sym:
                out.setdefault(resolve(sec) or sec.upper(), []).append(sym)
    if not out:
        raise SectorError(f"--members {path}: no sector/symbol rows")
    return out


def breadth(members: dict, directory: str) -> dict:
    d = Path(directory)
    if not d.is_dir():
        raise SectorError(f"--csv-dir {directory}: not a directory")
    files = {norm(p.stem.replace(".NS", "").replace(".BO", "")): p for p in d.iterdir()
             if p.suffix.lower() == ".csv"}
    out = []
    for sec, syms in members.items():
        n50 = a50 = n200 = a200 = 0
        missing = []
        for sym in dict.fromkeys(syms):
            p = files.get(norm(sym))
            closes = [c for _, c in load_closes(p)] if p else []
            if len(closes) < 50:
                missing.append(sym)
                continue
            n50 += 1
            a50 += closes[-1] > sum(closes[-50:]) / 50
            if len(closes) >= 200:
                n200 += 1
                a200 += closes[-1] > sum(closes[-200:]) / 200
        out.append({"sector": sec, "members": len(dict.fromkeys(syms)), "used": n50,
                    "pct_above_50dma": a50 / n50 * 100 if n50 else None,
                    "pct_above_200dma": a200 / n200 * 100 if n200 else None,
                    "missing": missing})
    out.sort(key=lambda r: -(r["pct_above_50dma"] or -1))
    return {"sectors": out}


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def f(x, d=1, sign=True):
    return "—" if x is None else (f"{x:+.{d}f}" if sign else f"{x:.{d}f}")


def print_rotation(res: dict) -> None:
    b, p = res["benchmark"], res["params"]
    print(f"Sector rotation vs {b['label']} — {res['frequency']} data to {b['last_date']} "
          f"({b['source']}); weekly RRG: smooth {p['smooth']}, window {p['window']}, momentum {p['momentum']}")
    for w in res["warnings"]:
        print(f"⚠ {w}")
    ma = "50/200DMA" if res["frequency"] == "daily" else "10/40WMA"
    hdr = (f"{'#':>2} {'Index':<12} {'1W%':>6} {'1M%':>6} {'3M%':>6} {'6M%':>6} {'12M%':>6} {'rel1M':>6} "
           f"{'rel3M':>6} {'RS-Rat':>7} {'RS-Mom':>7} {'Quadrant':<10} {'wks':>3} {'Last change':<22} "
           f"{'vs ' + ma:>15} {'Trend':<10}")

    def line(i, r):
        R, rel = r["returns"], r["relative"]
        t = r["transitions"][-1] if r["transitions"] else None
        chg = f"{t['from'][:4]}→{t['to'][:4]} {t['week'][5:]}" if t else "—"
        dist = f"{f(r['dist_fast_pct'])}/{f(r['dist_slow_pct'])}"
        return (f"{i:>2} {r['key'][:12]:<12} {f(R['1W']):>6} {f(R['1M']):>6} {f(R['3M']):>6} {f(R['6M']):>6} "
                f"{f(R['12M']):>6} {f(rel['1M']):>6} {f(rel['3M']):>6} {f(r['rs_ratio'], 2, False):>7} "
                f"{f(r['rs_momentum'], 2, False):>7} {r['quadrant'] or '—':<10} {r['weeks_in_quadrant']:>3} "
                f"{chg:<22} {dist:>15} {r['trend']:<10}")
    print(hdr)
    for i, r in enumerate(res["sectors"], 1):
        print(line(i, r))
    if res["size"]:
        print(f"Size factors (vs {b['label']}):")
        for i, r in enumerate(res["size"], 1):
            print(line(i, r))
    t, s = res["rotation"], res["size_read"]
    if t.get("tilt") != "n/a":
        print(f"\nTilt: {t['tilt']} — cyclicals {','.join(t['cyclicals'])} vs defensives "
              f"{','.join(t['defensives'])}: RS-Ratio spread {f(t['rs_ratio_spread'], 2)}, "
              f"rel 3M spread {f(t['rel_3m_spread_pct'])}%")
    else:
        print(f"\nTilt: n/a — {t['reason']}")
    print(f"Size: {s['read']} — {s['reason']}")
    for sk in res["skipped"]:
        print(f"Skipped {sk['key']}: {sk['reason']}")
    print(f"\nSummary: {res['summary']}")
    print("Rank = (RS-Ratio − 100) + (RS-Momentum − 100). Relative strength is a context filter, not a "
          "trade signal; confirm on the chart.")


def print_rrg(res: dict) -> None:
    print(f"RS-Ratio / RS-Momentum trail vs {res['benchmark']['label']} (weekly, oldest → newest)")
    for r in res["sectors"] + res["size"]:
        trail = "  ".join(f"{t['week'][5:]} {t['rs_ratio']:.1f}/{f(t['rs_momentum'], 1, False)}"
                          for t in r["tail"])
        print(f"{r['key']:<12} {r['quadrant'] or '—':<10} {trail}")


def print_universe() -> None:
    print(f"{'Key':<12} {'Index':<26} {'Yahoo ticker':<22} {'Group':<10} Yahoo history")
    print(f"{BENCHMARK['key']:<12} {BENCHMARK['label']:<26} {BENCHMARK['ticker']:<22} {'benchmark':<10} history")
    for u in UNIVERSE:
        print(f"{u[0]:<12} {u[1]:<26} {u[2]:<22} {u[3]:<10} {u[4]}")
    print("\nquote-only = Yahoo resolves the symbol but serves no daily history (checked 2026-10-31); "
          "use --csv-dir or --ticker NAME=TICKER.")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="NSE sector rotation vs NIFTY 50")
    p.add_argument("cmd", nargs="?", default="rotation", choices=["rotation", "rrg", "breadth", "universe"])
    p.add_argument("--csv-dir", help="folder with one CSV per index (or per stock for breadth)")
    p.add_argument("--benchmark", default="NIFTY", help="benchmark file stem in --csv-dir (default NIFTY / NIFTY 50)")
    p.add_argument("--sectors", help="comma list of index keys to include (default: all with Yahoo history)")
    p.add_argument("--ticker", action="append", metavar="NAME=TICKER", help="override/add a Yahoo ticker")
    p.add_argument("--map", help="JSON {NAME: TICKER} or CSV name,ticker of ticker overrides")
    p.add_argument("--period", default="2y", help="yfinance history period (default 2y)")
    p.add_argument("--weeks", type=int, default=8, help="look-back for quadrant changes (default 8)")
    p.add_argument("--smooth", type=int, default=3, help="EMA weeks applied to the RS line (default 3)")
    p.add_argument("--window", type=int, default=10, help="RS trend window in weeks (default 10)")
    p.add_argument("--momentum", type=int, default=4, help="RS-Momentum lag in weeks (default 4)")
    p.add_argument("--tail", type=int, default=8, help="weekly points kept in the RRG trail (default 8)")
    p.add_argument("--members", help="CSV of sector,symbol for the breadth sub-command")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    try:
        return _run(a)
    except SectorError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except (OSError, UnicodeDecodeError, csv.Error) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


def _run(a) -> int:
    if a.cmd == "universe":
        if a.json:
            print(json.dumps({"benchmark": BENCHMARK, "indices": list(INFO.values())}, indent=2))
        else:
            print_universe()
        return 0
    if a.cmd == "breadth":
        if not a.members or not a.csv_dir:
            raise SectorError("breadth needs --members FILE and --csv-dir DIR (one CSV per stock)")
        res = breadth(load_members(a.members), a.csv_dir)
        if a.json:
            print(json.dumps(res, indent=2))
        else:
            print(f"{'Sector':<14} {'used/members':>12} {'>50DMA':>7} {'>200DMA':>8}  missing")
            for r in res["sectors"]:
                print(f"{r['sector'][:14]:<14} {r['used']:>5}/{r['members']:<6} "
                      f"{f(r['pct_above_50dma'], 0, False):>6}% {f(r['pct_above_200dma'], 0, False):>7}%  "
                      f"{','.join(r['missing']) or '—'}")
        return 0

    for name in ("weeks", "smooth", "window", "momentum", "tail"):
        if getattr(a, name) < 1:
            raise SectorError(f"--{name} must be at least 1")
    params = {k: getattr(a, k) for k in ("weeks", "smooth", "window", "momentum", "tail")}
    wanted = None
    if a.sectors:
        wanted = [resolve(s) or norm(s) for s in a.sectors.split(",") if s.strip()]
    warnings: list = []
    overrides = parse_overrides(a.ticker, a.map)
    if a.csv_dir:
        bench, loaded = load_csv_dir(a.csv_dir, a.benchmark)
        if wanted:
            loaded = {k: v for k, v in loaded.items() if k in wanted or norm(k) in wanted}
    else:
        keys = wanted or [u[0] for u in UNIVERSE if u[4] == "history"]
        keys += [k for k in overrides if k not in keys and k != "NIFTY" and not wanted]
        bench, loaded = fetch_universe(keys, overrides, a.period, warnings)
    if not loaded:
        raise SectorError("no sector indices to analyse (check --sectors / --csv-dir contents)")
    res = analyse(bench, loaded, params, warnings)
    if a.json:
        print(json.dumps(res, indent=2, default=str))
    elif a.cmd == "rrg":
        print_rrg(res)
    else:
        print_rotation(res)
    return 0


if __name__ == "__main__":
    sys.exit(main())
