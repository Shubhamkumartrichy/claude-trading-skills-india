#!/usr/bin/env python3
"""Market-level valuation and froth gauges for Indian equities, from CSVs you download and numbers
you type in.

Zero dependencies (Python 3.10+ stdlib). Works fully offline; nothing is fetched.

Sub-commands
  pe        index valuation history (NSE Indices "P/E, P/B & Div Yield" CSV): current P/E, P/B and
            dividend yield, percentile over 5/10/all years, a cheap/fair/expensive/extreme band and
            the share of history that was more expensive than today. Handles the 31-Mar-2021 switch
            of the published P/E from standalone to consolidated earnings (--since)
  yieldgap  earnings yield (100 / P/E) minus the India 10-year G-sec yield, with a percentile
            against its own history when a valuation CSV and a yields CSV are both given
  mcap-gdp  listed market capitalisation ÷ nominal GDP from two numbers (₹ lakh crore each) or a
            small history CSV, placed in the bands usually quoted for India
  froth     0–100 froth / complacency score from whichever objective inputs you have (valuation,
            yield gap, mcap/GDP, small/midcap premium, India VIX, breadth, IPO and SME-IPO heat,
            SIP growth, new demat accounts, F&O activity, MTF leverage); weights are re-normalised
            over what is supplied and coverage is reported. Maps to a phase and a risk posture

Dates
  Accepted without question: 2026-10-30, 20261030, 30-Oct-2026, 30 Oct 2026, Oct 30, 2026 and
  30-10-2026 (dashes = day first, as NSE writes it). Slash or dot dates such as 05/10/2026 are used
  only when the column itself proves the order (some value has a day above 12); otherwise pass
  --date-format dmy|mdy. The script never guesses.

Examples
  python3 valuation.py pe --csv nifty50_pe.csv
  python3 valuation.py pe --csv nifty50_pe.csv --since 2021-03-31 --json
  python3 valuation.py yieldgap --pe 22.4 --gsec 6.55
  python3 valuation.py yieldgap --csv nifty50_pe.csv --yields gsec10y.csv
  python3 valuation.py mcap-gdp --mcap 470 --gdp 357
  python3 valuation.py froth --inputs froth_inputs.json --as-of 2026-11-12
  python3 valuation.py froth --valuation-pct 82 --vix 11.2 --mtf 148000 --mtf-year-ago 96000
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import re
import statistics
import sys
from datetime import date, datetime, timedelta
from pathlib import Path


class ValuationError(Exception):
    """Bad input the user can fix; printed as a single `error:` line with exit code 2."""


DISCLAIMER = "Educational analysis, not investment advice."

# NSE Indices press release of 23-Feb-2021: index P/E moves from trailing-four-quarter standalone
# earnings to consolidated earnings (standalone only where consolidated is unavailable), effective
# 31-Mar-2021. Values before this date are on a different earnings basis.
SWITCH_DATE = date(2021, 3, 31)

DEFAULTS = {
    "windows": (5, 10),        # look-back windows in years ("all" is always added)
    "bands": (25.0, 75.0, 90.0),  # richness percentile cuts: cheap < 25 ≤ fair < 75 ≤ expensive < 90 ≤ extreme
    "thin": 30,                # fewer observations than this in a window: percentile flagged as thin
    "max_gap_days": 45,        # a P/E date may use a G-sec yield at most this many days older
    "max_age_days": 45,        # froth inputs older than this are flagged stale (monthly data lags ~6 weeks)
}

# mcap/GDP bands for India (percent). The cuts are conventions built around figures brokerages
# publish: long-period averages of roughly 75–87%, about 55% at the Mar-2020 low, 149% at the
# Dec-2007 peak and 130–150% readings in 2024–25.
MCAP_GDP_BANDS = ((60.0, "cheap"), (100.0, "fair"), (120.0, "elevated"), (140.0, "expensive"),
                  (math.inf, "extreme"))
MCAP_GDP_LONG_RUN = (75.0, 87.0)
MCAP_GDP_REFERENCES = [
    {"period": "Mar-2020 (Covid low)", "ratio_pct": "54-57", "source": "Motilal Oswal via Business Standard"},
    {"period": "long-period average", "ratio_pct": "75-87",
     "source": "Motilal Oswal notes 2020-2025 (the average drifts up as years are added)"},
    {"period": "FY22", "ratio_pct": "112-116", "source": "Motilal Oswal via Business Standard"},
    {"period": "FY24 / FY25", "ratio_pct": "132 / 126", "source": "Motilal Oswal strategy notes"},
    {"period": "2024 highs", "ratio_pct": "140-150", "source": "press reports, May and Aug 2024"},
    {"period": "Dec-2007 peak", "ratio_pct": "149", "source": "Motilal Oswal"},
]


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------
def indian(x, d=2, sign=False) -> str:
    """Indian digit grouping: 12,34,567.89. None/NaN -> an em dash."""
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return "—"
    if round(abs(x), d) == 0:
        x = 0.0
    neg = x < 0
    whole, _, frac = f"{abs(x):.{d}f}".partition(".")
    if len(whole) > 3:
        head, tail, groups = whole[:-3], whole[-3:], []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        whole = ",".join(([head] if head else []) + groups + [tail])
    s = whole + ("." + frac if frac else "")
    return ("-" if neg else ("+" if sign and x > 0 else "")) + s


def num(x, d=2, sign=False) -> str:
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return "—"
    if round(abs(x), d) == 0:
        x = 0.0
    return f"{x:+.{d}f}" if sign else f"{x:.{d}f}"


def pct(x, d=1, sign=False) -> str:
    s = num(x, d, sign)
    return s if s == "—" else s + "%"


def ordinal(x) -> str:
    n = int(round(x))
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def dd(d: date | None, weekday=False) -> str:
    if d is None:
        return "—"
    s = d.strftime("%d-%b-%Y")
    return f"{d.strftime('%a')} {s}" if weekday else s


def clean_json(o):
    """Make output strict JSON: dates -> ISO strings, NaN/Infinity -> null, floats rounded."""
    if isinstance(o, dict):
        return {str(k): clean_json(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean_json(v) for v in o]
    if isinstance(o, float):
        return round(o, 4) if math.isfinite(o) else None
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    return o


def emit_json(res: dict) -> None:
    print(json.dumps(clean_json(res), indent=2, ensure_ascii=False, allow_nan=False))


# ---------------------------------------------------------------------------
# Dates: robust, but never guess day/month order
# ---------------------------------------------------------------------------
_YMD = re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$")
_DMY_NUM = re.compile(r"^(\d{1,2})([-/.])(\d{1,2})\2(\d{2}|\d{4})$")
_COMPACT = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
_TIME_TAIL = re.compile(r"(?:[ T]\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?\s*(?:[AP]M)?\s*(?:Z|[+-]\d{2}:?\d{2}|IST)?)$",
                        re.I)
_TEXT_FORMATS = ("%d-%b-%Y", "%d %b %Y", "%d-%B-%Y", "%d %B %Y", "%d %b, %Y", "%d %B, %Y", "%b %d, %Y",
                 "%B %d, %Y", "%b %d %Y", "%B %d %Y", "%d/%b/%Y", "%d.%b.%Y", "%b-%Y", "%b %Y", "%B %Y")
_MONTH_ONLY = ("%b-%Y", "%b %Y", "%B %Y")
DATE_FORMATS_HELP = "use YYYY-MM-DD, DD-Mon-YYYY (30-Oct-2026) or DD-MM-YYYY"


def _mkdate(y, m, d, raw):
    try:
        return date(int(y), int(m), int(d))
    except ValueError:
        raise ValuationError(f"invalid date {raw!r}")


def _month_end(d: date) -> date:
    nxt = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    return nxt - timedelta(days=1)


def _classify(raw):
    """("date", date) for unambiguous text, ("num", (a, b, year, sep, raw)) for numeric day/month
    forms, or (None, None) when the cell is not a date at all (a footnote or blank row)."""
    s = str(raw).strip().strip('"').strip()
    if not s:
        return None, None
    s = _TIME_TAIL.sub("", s).strip()
    m = _YMD.match(s)
    if m:
        return "date", _mkdate(m.group(1), m.group(2), m.group(3), raw)
    m = _COMPACT.match(s)
    if m and 1900 <= int(m.group(1)) <= 2200:
        return "date", _mkdate(m.group(1), m.group(2), m.group(3), raw)
    m = _DMY_NUM.match(s)
    if m:
        if len(m.group(4)) == 2:
            raise ValuationError(f"two-digit year in {raw!r} is ambiguous; {DATE_FORMATS_HELP}")
        return "num", (int(m.group(1)), int(m.group(3)), int(m.group(4)), m.group(2), str(raw).strip())
    for fmt in _TEXT_FORMATS:
        try:
            got = datetime.strptime(s, fmt).date()
        except ValueError:
            continue
        return "date", _month_end(got) if fmt in _MONTH_ONLY else got   # 'Sep-2026' = month end
    return None, None


def parse_dates(values, order: str | None = None, what: str = "date") -> list:
    """Parse a whole date column. Returns a list of date or None (cell is not a date).

    Dash-separated numbers follow NSE (DD-MM-YYYY). Slash/dot numbers need evidence from the column
    (a first part above 12 proves day-first, a second part above 12 proves month-first) or an
    explicit order ('dmy'/'mdy'); without that the column is rejected as ambiguous.
    """
    kinds = [_classify(v) for v in values]
    slash = [k[1] for k in kinds if k[0] == "num" and k[1][3] != "-"]
    slash_order = order
    if slash and not order:
        dayfirst = [x for x in slash if x[0] > 12]
        monthfirst = [x for x in slash if x[1] > 12]
        if dayfirst and monthfirst:
            raise ValuationError(f"{what}: column mixes day-first ({dayfirst[0][4]!r}) and month-first "
                                 f"({monthfirst[0][4]!r}) dates; fix the file or {DATE_FORMATS_HELP}")
        if dayfirst:
            slash_order = "dmy"
        elif monthfirst:
            slash_order = "mdy"
        else:
            raise ValuationError(f"{what}: ambiguous date {slash[0][4]!r} — could be day-first or month-first "
                                 f"and no value in the column settles it; pass --date-format dmy|mdy or "
                                 f"{DATE_FORMATS_HELP}")
    out = []
    for kind, val in kinds:
        if kind is None:
            out.append(None)
        elif kind == "date":
            out.append(val)
        else:
            a, b, y, sep, raw = val
            o = slash_order if sep != "-" else (order or "dmy")
            day, mon = (a, b) if o == "dmy" else (b, a)
            if mon > 12 and sep == "-" and not order:
                raise ValuationError(f"{what}: {raw!r} is not DD-MM-YYYY (NSE order); if the file is "
                                     "month-first pass --date-format mdy")
            out.append(_mkdate(y, mon, day, raw))
    return out


def parse_one(raw, flag: str, order: str | None = None) -> date:
    got = parse_dates([raw], order, flag)[0]
    if got is None:
        raise ValuationError(f"{flag}: cannot read date {raw!r}; {DATE_FORMATS_HELP}")
    return got


def years_back(d: date, n: int) -> date:
    try:
        return d.replace(year=d.year - n)
    except ValueError:                      # 29-Feb
        return d.replace(year=d.year - n, day=28)


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------
_BLANK = {"", "-", "--", "—", "na", "n/a", "nil", "null", "none", "nan"}


def norm(h) -> str:
    """'Div Yield %' -> 'div yield'; 'P/E' -> 'p e'; 'IndexName' -> 'indexname'."""
    return re.sub(r"[^a-z0-9]+", " ", str(h or "").lower()).strip()


def to_num(raw, where: str):
    """'1,23,456.70' / '(512.3)' / '45.10%' / '₹470' -> float; blank or dash -> None."""
    if raw is None:
        return None
    s = str(raw).strip().replace("−", "-").replace(",", "").replace("₹", "").replace("%", "").strip()
    if s.lower() in _BLANK:
        return None
    neg = s.startswith("(") and s.endswith(")")
    if neg:
        s = s[1:-1]
    try:
        x = float(s)
    except ValueError:
        raise ValuationError(f"{where}: not a number: {raw!r}")
    if not math.isfinite(x):
        raise ValuationError(f"{where}: not a finite number: {raw!r}")
    return -x if neg else x


def read_rows(path) -> list:
    p = Path(path)
    if not p.is_file():
        raise ValuationError(f"{path}: file not found")
    with open(p, newline="", encoding="utf-8-sig", errors="replace") as fh:
        rows = [r for r in csv.reader(fh)]
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        raise ValuationError(f"{p.name}: empty file")
    return rows


def _header(raw, name, need="date"):
    hi = next((i for i, r in enumerate(raw[:10]) if any(need in norm(c).split() for c in r)), None)
    if hi is None:
        raise ValuationError(f"{name}: no header row with a {need} column in the first 10 lines")
    return hi, [norm(c) for c in raw[hi]]


def _cell(r, i):
    return r[i] if i is not None and i < len(r) else ""


# ---------------------------------------------------------------------------
# Valuation history (P/E, P/B, dividend yield)
# ---------------------------------------------------------------------------
METRICS = (("pe", "P/E"), ("pb", "P/B"), ("div_yield", "Div yield"))


def _metric_col(h: str):
    t = h.split()
    if h in ("pe", "p e", "pe ratio", "p e ratio") or "earnings" in t or "earning" in t or t[:2] == ["p", "e"]:
        return "pe"
    if h in ("pb", "p b", "pb ratio", "p b ratio") or "book" in t or t[:2] == ["p", "b"]:
        return "pb"
    if "div" in t or "dividend" in t or h in ("dy", "yield", "yield pct"):
        return "div_yield"
    return None


def load_valuation(paths, index: str | None = None, order: str | None = None) -> tuple:
    """Read one or more valuation-history CSVs (niftyindices.com 'Index Name,Date,P/E,P/B,Div Yield %'
    or the older NSE archive 'Date,P/E,P/B,Div Yield'). Returns (rows, meta, notes); each row is
    {date, pe, pb, div_yield} with None where the cell is blank."""
    if isinstance(paths, (str, Path)):
        paths = [paths]
    by_date, notes, file_notes, names_seen, files = {}, [], [], {}, []
    for path in paths:
        raw = read_rows(path)
        name = Path(path).name
        files.append(name)
        hi, hn = _header(raw, name)
        dcol = next(i for i, h in enumerate(hn) if "date" in h.split())
        icol = next((i for i, h in enumerate(hn) if h in ("index name", "indexname", "index", "index_name")), None)
        cols = {}
        for i, h in enumerate(hn):
            if i in (dcol, icol):
                continue
            k = _metric_col(h)
            if k:
                cols.setdefault(k, i)
        if not cols:
            raise ValuationError(f"{name}: no P/E, P/B or dividend-yield column (header was "
                                 f"{[c.strip() for c in raw[hi] if c.strip()]})")
        body = raw[hi + 1:]
        dates = parse_dates([_cell(r, dcol) for r in body], order, f"{name} date column")
        skipped = 0
        for r, d in zip(body, dates):
            if d is None:
                text = " ".join(c.strip() for c in r if c.strip())
                if re.search(r"consolidated|standalone", text, re.I):
                    file_notes.append(f"{name}: {text}")
                else:
                    skipped += 1
                continue
            idx = _cell(r, icol).strip() if icol is not None else ""
            if idx:
                names_seen.setdefault(norm(idx), idx)
                if index and norm(idx) != norm(index):
                    continue
            where = f"{name} {dd(d)}"
            rec = {k: to_num(_cell(r, i), f"{where} {k}") for k, i in cols.items()}
            for k in ("pe", "pb"):
                if rec.get(k) is not None and rec[k] <= 0:
                    rec[k] = None                 # negative earnings: no meaningful ratio
            if rec.get("div_yield") is not None and rec["div_yield"] < 0:
                raise ValuationError(f"{where}: negative dividend yield {rec['div_yield']}")
            if all(v is None for v in rec.values()):
                skipped += 1
                continue
            key = (norm(idx), d)
            if key in by_date:
                if by_date[key] != rec:
                    raise ValuationError(f"{name}: {dd(d)} appears twice with different values")
                continue
            by_date[key] = rec
        if skipped:
            notes.append(f"{name}: skipped {skipped} row(s) without a date or any value")
    if index and names_seen and norm(index) not in names_seen:
        raise ValuationError(f"--index {index!r} not in the file; it has: {', '.join(sorted(names_seen.values()))}")
    if len(names_seen) > 1 and not index:
        raise ValuationError("the file holds several indices (" + ", ".join(sorted(names_seen.values()))
                             + "); pick one with --index")
    rows = [{"date": d, "pe": v.get("pe"), "pb": v.get("pb"), "div_yield": v.get("div_yield")}
            for (_, d), v in sorted(by_date.items(), key=lambda kv: kv[0][1])]
    if not rows:
        raise ValuationError(f"{', '.join(files)}: no usable rows ({DATE_FORMATS_HELP})")
    label = names_seen.get(norm(index)) if index and names_seen else (next(iter(names_seen.values()), None) or index)
    meta = {"index": label or "index", "files": files, "file_notes": file_notes}
    return rows, meta, notes


def rank_stats(values, cur, higher_is_richer=True) -> dict:
    """Where `cur` sits in `values`. percentile = share of observations at or below `cur` (so a
    record high is 100). richness_percentile turns that into 'more expensive = higher' (inverted for
    dividend yield and the yield gap). pct_time_richer = share of observations strictly more
    expensive than `cur`."""
    n = len(values)
    le = sum(1 for v in values if v <= cur)
    ge = sum(1 for v in values if v >= cur)
    raw = le / n * 100
    return {"percentile": raw, "richness_percentile": raw if higher_is_richer else ge / n * 100,
            "pct_time_above": (n - le) / n * 100,
            "pct_time_richer": ((n - le) if higher_is_richer else (n - ge)) / n * 100}


def band_for(richness: float, cuts=DEFAULTS["bands"]) -> str:
    cheap, rich, extreme = cuts
    if richness < cheap:
        return "cheap"
    if richness < rich:
        return "fair"
    if richness < extreme:
        return "expensive"
    return "extreme"


def build_windows(series, asof: date, years_list, higher_is_richer=True, cuts=DEFAULTS["bands"],
                  switch: date | None = None) -> list:
    """series: sorted [(date, value)], all on or before asof. One stats row per look-back window,
    plus 'all', plus a consolidated-only window when `switch` falls inside the history."""
    cur = series[-1][1]
    first = series[0][0]
    specs = [(f"{y}y", y, years_back(asof, y)) for y in years_list] + [("all", None, first)]
    if switch and first < switch <= series[-1][0]:
        specs.append((f"since {dd(switch)}", None, switch))
    out = []
    for label, y, start in specs:
        sel = [(d, v) for d, v in series if d >= start]
        if not sel:
            continue
        vals = [v for _, v in sel]
        st = rank_stats(vals, cur, higher_is_richer)
        out.append({"window": label, "years": y, "from": sel[0][0], "to": sel[-1][0], "n": len(sel),
                    "complete": y is None or first <= start + timedelta(days=31),
                    "mixed_basis": bool(switch and sel[0][0] < switch <= sel[-1][0]),
                    "thin": len(sel) < DEFAULTS["thin"], **st, "band": band_for(st["richness_percentile"], cuts),
                    "min": min(vals), "median": statistics.median(vals), "max": max(vals)})
    return out


def headline_window(windows: list) -> dict:
    """The longest window that sits on a single earnings basis (falls back to the longest window)."""
    pool = [w for w in windows if not w["mixed_basis"]] or windows
    return max(pool, key=lambda w: (w["n"], w["years"] is None))


def basis_break(rows) -> dict | None:
    """The P/E step across the 31-Mar-2021 switch, with P/B alongside as a price proxy."""
    before = next((r for r in reversed(rows) if r["date"] < SWITCH_DATE and r["pe"] is not None), None)
    after = next((r for r in rows if r["date"] >= SWITCH_DATE and r["pe"] is not None), None)
    if not before or not after or (after["date"] - before["date"]).days > 35:
        return None
    out = {"before": {"date": before["date"], "pe": before["pe"], "pb": before["pb"]},
           "after": {"date": after["date"], "pe": after["pe"], "pb": after["pb"]},
           "days_apart": (after["date"] - before["date"]).days,
           "pe_change_pct": (after["pe"] / before["pe"] - 1) * 100, "pb_change_pct": None}
    if before["pb"] and after["pb"]:
        out["pb_change_pct"] = (after["pb"] / before["pb"] - 1) * 100
    return out


def analyse_pe(rows, meta, years_list=DEFAULTS["windows"], cuts=DEFAULTS["bands"], since: date | None = None,
               asof: date | None = None) -> dict:
    full = rows
    if asof:
        rows = [r for r in rows if r["date"] <= asof]
        if not rows:
            raise ValuationError(f"no rows on or before {dd(asof)}")
    if since:
        rows = [r for r in rows if r["date"] >= since]
        if not rows:
            raise ValuationError(f"no rows on or after --since {dd(since)}")
    last = rows[-1]["date"]
    metrics, notes = {}, []
    for key, label in METRICS:
        series = [(r["date"], r[key]) for r in rows if r[key] is not None]
        if not series:
            continue
        wins = build_windows(series, last, years_list, higher_is_richer=(key != "div_yield"), cuts=cuts,
                             switch=SWITCH_DATE if key == "pe" else None)
        head = headline_window(wins) if key == "pe" else next(w for w in wins if w["window"] == "all")
        metrics[key] = {"label": label, "current": series[-1][1], "date": series[-1][0], "observations": len(series),
                        "windows": wins, "headline": {k: head[k] for k in ("window", "from", "n", "percentile",
                                                                           "richness_percentile", "band",
                                                                           "pct_time_richer", "thin")}}
        if series[-1][0] != last:
            notes.append(f"{label}: latest value is from {dd(series[-1][0])} (blank after that)")
    if not metrics:
        raise ValuationError("no P/E, P/B or dividend-yield values in the selected rows")
    pe_dates = [r["date"] for r in rows if r["pe"] is not None]
    spans = bool(pe_dates) and pe_dates[0] < SWITCH_DATE <= pe_dates[-1]
    basis = {"switch_date": SWITCH_DATE, "spans_switch": spans, "break": basis_break(full),
             "current_basis": None if not pe_dates else
             ("consolidated" if pe_dates[-1] >= SWITCH_DATE else "standalone"),
             "source": "NSE Indices press release, 23-Feb-2021 (effective 31-Mar-2021)"}
    res = {"command": "pe", "index": meta["index"], "as_of": last, "files": meta["files"],
           "observations": len(rows), "first_date": rows[0]["date"], "since": since,
           "bands": {"cheap_below": cuts[0], "expensive_from": cuts[1], "extreme_from": cuts[2],
                     "on": "richness percentile (dividend yield inverted: a low yield is expensive)"},
           "metrics": metrics, "basis": basis, "file_notes": meta["file_notes"], "notes": notes}
    res["read"] = pe_read(res)
    return res


def pe_read(res) -> list:
    out, m, b = [], res["metrics"], res["basis"]
    if "pe" in m:
        h = m["pe"]["headline"]
        out.append(f"{res['index']} P/E {num(m['pe']['current'])} is at the {ordinal(h['richness_percentile'])} "
                   f"percentile of its {h['window']} history ({h['n']} observations from {dd(h['from'])}): "
                   f"{h['band']}. It was more expensive than this {pct(h['pct_time_richer'], 0)} of the time.")
        if b["spans_switch"]:
            step = b["break"]
            seen = (f" In this file the P/E moved {pct(step['pe_change_pct'], 1, True)} between "
                    f"{dd(step['before']['date'])} and {dd(step['after']['date'])}"
                    + (f" while P/B moved {pct(step['pb_change_pct'], 1, True)}." if step["pb_change_pct"] is not None
                       else ".")) if step else ""
            out.append("Basis break: the published P/E used standalone earnings before 31-Mar-2021 and consolidated "
                       "earnings from that date. Consolidated profits are usually larger, so the P/E stepped down "
                       "with no change in price." + seen + " Windows marked † mix both bases and make today's "
                       "P/E look cheaper than it is; the headline uses consolidated-only history. Pass "
                       "--since 2021-03-31 to drop the older basis everywhere.")
        elif b["current_basis"] == "consolidated":
            out.append("All P/E values here are on consolidated earnings (from 31-Mar-2021). That is a short "
                       "history covering one market cycle, so treat the percentile as provisional and do not "
                       "compare it with pre-2021 P/E levels (standalone earnings).")
        else:
            out.append("All P/E values here are on standalone earnings (before 31-Mar-2021); they are not "
                       "comparable with P/E published since.")
        if h["thin"]:
            out.append(f"Only {h['n']} observations in the headline window: the percentile is rough.")
    for key in ("pb", "div_yield"):
        if key in m:
            h = m[key]["headline"]
            what = "P/B" if key == "pb" else "Dividend yield"
            unit = "" if key == "pb" else "%"
            tail = "" if key == "pb" else " (a low yield reads as expensive)"
            out.append(f"{what} {num(m[key]['current'])}{unit}: {ordinal(h['richness_percentile'])} richness "
                       f"percentile over {h['n']} observations from {dd(h['from'])} — {h['band']}{tail}.")
    order = ["cheap", "fair", "expensive", "extreme"]
    if "pe" in m and "pb" in m:
        gap = order.index(m["pb"]["headline"]["band"]) - order.index(m["pe"]["headline"]["band"])
        if abs(gap) >= 2:
            out.append("P/E and P/B disagree by two bands or more: " +
                       ("earnings are cyclically high (P/E flatters)" if gap > 0 else
                        "earnings are cyclically depressed (P/E overstates)") + " — weigh P/B more.")
    out.append("Valuation says little about the next few months; use it to set return expectations and "
               "position size, not entries and exits.")
    return out


def print_pe(res) -> None:
    print(f"{res['index']} valuation — {dd(res['as_of'], True)} | {res['observations']} rows "
          f"{dd(res['first_date'])} → {dd(res['as_of'])}" + (f" | --since {dd(res['since'])}" if res["since"] else ""))
    m = res["metrics"]
    print("Current: " + " | ".join(f"{v['label']} {num(v['current'])}{'%' if k == 'div_yield' else ''}"
                                   for k, v in m.items()))
    print(f"\n{'Metric':<10} {'Window':<18} {'Obs':>5} {'From':<12} {'Pctile':>6} {'Band':<10} {'Richer':>7} "
          f"{'Min':>7} {'Median':>7} {'Max':>7}")
    marks = set()
    for k, v in m.items():
        for w in v["windows"]:
            tag = ""
            if w["mixed_basis"]:
                tag += " †"
                marks.add("†")
            if not w["complete"]:
                tag += " *"
                marks.add("*")
            if w["window"] == v["headline"]["window"]:
                tag += " ◄"
            print(f"{v['label']:<10} {w['window'] + tag:<18} {w['n']:>5} {dd(w['from']):<12} "
                  f"{num(w['richness_percentile'], 0):>6} {w['band']:<10} {pct(w['pct_time_richer'], 0):>7} "
                  f"{num(w['min']):>7} {num(w['median']):>7} {num(w['max']):>7}")
    b = res["bands"]
    print(f"Pctile = richness percentile (share of observations as cheap as or cheaper than today; dividend "
          f"yield inverted). Bands: cheap < {b['cheap_below']:g} ≤ fair < {b['expensive_from']:g} ≤ expensive < "
          f"{b['extreme_from']:g} ≤ extreme. Richer = share of history more expensive than today. ◄ headline.")
    if "†" in marks:
        print("† window mixes standalone (pre 31-Mar-2021) and consolidated P/E.")
    if "*" in marks:
        print("* history is shorter than the window.")
    print("\nRead:")
    for line in res["read"]:
        print(f"  - {line}")
    for x in res["file_notes"]:
        print(f"File note: {x}")
    for x in res["notes"]:
        print(f"⚠ {x}")
    print(DISCLAIMER)


# ---------------------------------------------------------------------------
# yieldgap: earnings yield minus the 10-year G-sec yield
# ---------------------------------------------------------------------------
YIELDGAP_LIMITS = [
    "Earnings yield is a real, growing claim; the G-sec yield is a fixed nominal coupon. Subtracting one "
    "from the other mixes the two, so the level has no natural 'fair' value.",
    "In India the 10-year G-sec has yielded more than NIFTY's trailing earnings yield in most years, so a "
    "negative gap is the norm, not a sell signal. Read the gap against its own history.",
    "Trailing earnings are cyclical: the gap looks worst at an earnings trough and best at a peak.",
    "Published thresholds conflict (a 2018 press piece put the average bond-minus-earnings spread near "
    "2.6 points with 3+ as rich; a 2022 broker note used about 1 point as average and 2 as rich) because "
    "they differ on trailing vs forward earnings and on the window.",
    "The P/E basis changed on 31-Mar-2021 (standalone to consolidated), which shifts the gap history too.",
    "G-sec yields reflect RBI policy, SLR demand and government borrowing as well as inflation; and the "
    "gap has stayed stretched for years at a time. It is not a timing tool.",
]


def load_yields(path, order: str | None = None) -> list:
    """10-year G-sec yield history: [(date, yield %)]. Accepts 'Date,Yield', RBI/CCIL-style exports and
    Investing.com's 'Date,Price,Open,High,Low,Change %' (the Price column is the yield)."""
    raw = read_rows(path)
    name = Path(path).name
    hi, hn = _header(raw, name)
    dcol = next(i for i, h in enumerate(hn) if "date" in h.split())
    ycol = next((i for i, h in enumerate(hn) if i != dcol and ("yield" in h.split() or "ytm" in h.split())), None)
    if ycol is None:
        ycol = next((i for i, h in enumerate(hn) if h in ("price", "close", "last", "value", "rate", "10y",
                                                          "gsec", "g sec", "gsec 10y", "india 10y")), None)
    if ycol is None:
        raise ValuationError(f"{name}: no yield column (header was {[c.strip() for c in raw[hi] if c.strip()]})")
    body = raw[hi + 1:]
    dates = parse_dates([_cell(r, dcol) for r in body], order, f"{name} date column")
    out = {}
    for r, d in zip(body, dates):
        if d is None:
            continue
        y = to_num(_cell(r, ycol), f"{name} {dd(d)}")
        if y is None:
            continue
        check_gsec(y, f"{name} {dd(d)}")
        out[d] = y
    if not out:
        raise ValuationError(f"{name}: no usable yield rows")
    return sorted(out.items())


def check_gsec(y: float, where: str) -> None:
    if not 0.5 <= y <= 25:
        raise ValuationError(f"{where}: G-sec yield {y:g} is outside 0.5–25; give it in percent (6.55, not 0.0655)")


def gap_history(pe_series, yields, max_gap_days=DEFAULTS["max_gap_days"]) -> tuple:
    """Pair each P/E date with the latest yield on or before it (at most max_gap_days older)."""
    out, j, dropped = [], -1, 0
    for d, pe in pe_series:
        while j + 1 < len(yields) and yields[j + 1][0] <= d:
            j += 1
        if j < 0 or (d - yields[j][0]).days > max_gap_days:
            dropped += 1
            continue
        y = yields[j][1]
        out.append({"date": d, "pe": pe, "gsec": y, "yield_date": yields[j][0], "gap": 100 / pe - y})
    return out, dropped


def analyse_yieldgap(pe: float | None = None, gsec: float | None = None, pe_series=None, yields=None,
                     years_list=DEFAULTS["windows"], cuts=DEFAULTS["bands"], since: date | None = None,
                     asof: date | None = None, max_gap_days=DEFAULTS["max_gap_days"], index="index") -> dict:
    notes, hist, as_of, pe_date, gsec_date = [], [], None, None, None
    if asof:
        pe_series = [x for x in (pe_series or []) if x[0] <= asof] or None
        yields = [x for x in (yields or []) if x[0] <= asof] or None
    if pe_series and yields:
        hist, dropped = gap_history(pe_series, yields, max_gap_days)
        if since:
            hist = [h for h in hist if h["date"] >= since]
        if dropped:
            notes.append(f"{dropped} P/E date(s) had no G-sec yield within {max_gap_days} days before them and "
                         "were left out of the history")
        if not hist:
            raise ValuationError("the P/E and yield files do not overlap (after --since/--as-of); check both "
                                 "date ranges")
    if pe is None and pe_series:
        pe_date, pe = pe_series[-1]
    if gsec is None and yields:
        gsec_date, gsec = yields[-1]
        if pe_date and (pe_date - gsec_date).days > max_gap_days:
            notes.append(f"latest G-sec yield is from {dd(gsec_date)}, {(pe_date - gsec_date).days} days before "
                         "the latest P/E")
    if pe is None or gsec is None:
        raise ValuationError("need a P/E (--pe or --csv) and a 10-year G-sec yield (--gsec or --yields)")
    if pe <= 0:
        raise ValuationError("--pe must be positive")
    check_gsec(gsec, "--gsec")
    ey = 100 / pe
    gap = ey - gsec
    as_of = max([d for d in (pe_date, gsec_date) if d], default=None)
    res = {"command": "yieldgap", "index": index, "as_of": as_of, "pe": pe, "pe_date": pe_date, "gsec_10y": gsec,
           "gsec_date": gsec_date, "earnings_yield": ey, "yield_gap_pp": gap,
           "bond_to_earnings_yield_ratio": gsec / ey, "pe_where_gap_is_zero": 100 / gsec,
           "history": None, "limitations": YIELDGAP_LIMITS, "notes": notes}
    if hist:
        series = [(h["date"], h["gap"]) for h in hist]
        if series[-1][1] != gap:                      # current figure overridden by a flag
            series = series + [(max(series[-1][0], as_of or series[-1][0]), gap)]
        wins = build_windows(series, series[-1][0], years_list, higher_is_richer=False, cuts=cuts,
                             switch=SWITCH_DATE)
        head = headline_window(wins)
        med = head["median"]
        res["history"] = {"observations": len(series), "first_date": series[0][0], "last_date": series[-1][0],
                          "windows": wins,
                          "headline": {k: head[k] for k in ("window", "from", "n", "percentile",
                                                            "richness_percentile", "band", "pct_time_richer",
                                                            "median", "thin")},
                          "pe_at_median_gap": 100 / (gsec + med) if gsec + med > 0 else None,
                          "spans_switch": series[0][0] < SWITCH_DATE <= series[-1][0]}
    res["read"] = yieldgap_read(res)
    return res


def yieldgap_read(res) -> list:
    gap = res["yield_gap_pp"]
    side = "less" if gap < 0 else "more"
    out = [f"Earnings yield {pct(res['earnings_yield'], 2)} (100 ÷ P/E {num(res['pe'])}) vs 10-year G-sec "
           f"{pct(res['gsec_10y'], 2)}: gap {num(gap, 2, True)} points — equities' trailing earnings yield is "
           f"{abs(gap):.2f} points {side} than the bond yield "
           f"(bond ÷ earnings yield {res['bond_to_earnings_yield_ratio']:.2f}×).",
           f"Fed-model style read: the gap would be zero at a P/E of {num(res['pe_where_gap_is_zero'], 1)}. "
           "NIFTY has rarely traded there, so use the gap's own history, not zero, as the yardstick."]
    h = res["history"]
    if h:
        hd = h["headline"]
        out.append(f"Against its own history ({hd['window']}, {hd['n']} observations from {dd(hd['from'])}): "
                   f"{ordinal(hd['richness_percentile'])} richness percentile — equities are {hd['band']} relative "
                   f"to bonds; the gap was lower (richer) than this {pct(hd['pct_time_richer'], 0)} of the time. "
                   f"Median gap {num(hd['median'], 2, True)}"
                   + (f", which today's G-sec yield would pair with a P/E of {num(h['pe_at_median_gap'], 1)}."
                      if h["pe_at_median_gap"] else "."))
        if h["spans_switch"]:
            out.append("History mixes standalone (pre 31-Mar-2021) and consolidated P/E; the headline uses the "
                       "consolidated part only. Pass --since 2021-03-31 to restrict every window.")
    else:
        out.append("No history supplied: give --csv (valuation history) and --yields (G-sec history) for a "
                   "percentile. Without one, the level alone says little.")
    return out


def print_yieldgap(res) -> None:
    print(f"Yield gap — {res['index']}" + (f" — {dd(res['as_of'], True)}" if res["as_of"] else ""))
    print(f"P/E {num(res['pe'])}{' (' + dd(res['pe_date']) + ')' if res['pe_date'] else ''} → earnings yield "
          f"{pct(res['earnings_yield'], 2)} | 10-year G-sec {pct(res['gsec_10y'], 2)}"
          f"{' (' + dd(res['gsec_date']) + ')' if res['gsec_date'] else ''} | gap {num(res['yield_gap_pp'], 2, True)} pp")
    h = res["history"]
    if h:
        print(f"\n{'Window':<20} {'Obs':>5} {'From':<12} {'Pctile':>6} {'Band':<10} {'Richer':>7} {'Min':>7} "
              f"{'Median':>7} {'Max':>7}")
        for w in h["windows"]:
            tag = (" †" if w["mixed_basis"] else "") + ("" if w["complete"] else " *") + \
                  (" ◄" if w["window"] == h["headline"]["window"] else "")
            print(f"{w['window'] + tag:<20} {w['n']:>5} {dd(w['from']):<12} {num(w['richness_percentile'], 0):>6} "
                  f"{w['band']:<10} {pct(w['pct_time_richer'], 0):>7} {num(w['min'], 2, True):>7} "
                  f"{num(w['median'], 2, True):>7} {num(w['max'], 2, True):>7}")
        print("Pctile = richness percentile (share of history with a gap as high as or higher than today's; a low "
              "gap is rich). † mixes P/E bases, * history shorter than the window, ◄ headline.")
    print("\nRead:")
    for line in res["read"]:
        print(f"  - {line}")
    print("Limits:")
    for line in res["limitations"]:
        print(f"  - {line}")
    for x in res["notes"]:
        print(f"⚠ {x}")
    print(DISCLAIMER)


# ---------------------------------------------------------------------------
# mcap-gdp
# ---------------------------------------------------------------------------
def mcap_gdp_band(ratio: float) -> str:
    return next(label for cut, label in MCAP_GDP_BANDS if ratio < cut)


def check_ratio(ratio: float, where: str) -> None:
    if not 10 <= ratio <= 500:
        raise ValuationError(f"{where}: market cap ÷ GDP works out to {ratio:.1f}% — outside 10–500%. Give both "
                             "in the same unit (₹ lakh crore; 1 lakh crore = ₹1 trillion)")


def load_mcap_gdp(path, order: str | None = None) -> list:
    """History CSV with a date column and either mcap + gdp (same unit) or a ratio column (%)."""
    raw = read_rows(path)
    name = Path(path).name
    hi, hn = _header(raw, name)
    dcol = next(i for i, h in enumerate(hn) if "date" in h.split())

    def find(*words):
        return next((i for i, h in enumerate(hn) if i != dcol and any(w in h.split() for w in words)), None)

    rcol = find("ratio")
    mcol = next((i for i, h in enumerate(hn) if i not in (dcol, rcol) and
                 ("mcap" in h.split() or "cap" in h.split() or "capitalisation" in h or "capitalization" in h)), None)
    gcol = next((i for i, h in enumerate(hn) if i not in (dcol, rcol, mcol) and "gdp" in h.split()), None)
    if rcol is None and (mcol is None or gcol is None):
        raise ValuationError(f"{name}: need date plus mcap and gdp columns, or a ratio column (header was "
                             f"{[c.strip() for c in raw[hi] if c.strip()]})")
    body = raw[hi + 1:]
    dates = parse_dates([_cell(r, dcol) for r in body], order, f"{name} date column")
    out = {}
    for r, d in zip(body, dates):
        if d is None:
            continue
        where = f"{name} {dd(d)}"
        mc, g = to_num(_cell(r, mcol), where), to_num(_cell(r, gcol), where)
        ratio = to_num(_cell(r, rcol), where)
        if mc is not None and g is not None:
            if mc <= 0 or g <= 0:
                raise ValuationError(f"{where}: mcap and gdp must be positive")
            ratio = mc / g * 100
        if ratio is None:
            continue
        check_ratio(ratio, where)
        out[d] = {"date": d, "mcap": mc, "gdp": g, "ratio": ratio}
    if not out:
        raise ValuationError(f"{name}: no usable rows")
    return [out[d] for d in sorted(out)]


def analyse_mcap_gdp(mcap: float | None = None, gdp: float | None = None, ratio: float | None = None,
                     history=None, asof: date | None = None, gdp_basis: str | None = None) -> dict:
    if asof and history:
        history = [h for h in history if h["date"] <= asof]
        if not history:
            raise ValuationError(f"no history rows on or before {dd(asof)}")
    cur_date = None
    if (mcap is None) != (gdp is None):
        raise ValuationError("give both --mcap and --gdp (₹ lakh crore each), or --ratio, or a --csv history")
    if mcap is not None:
        if mcap <= 0 or gdp <= 0:
            raise ValuationError("--mcap and --gdp must be positive")
        if ratio is not None:
            raise ValuationError("give --mcap/--gdp or --ratio, not both")
        ratio = mcap / gdp * 100
    elif ratio is None:
        if not history:
            raise ValuationError("give --mcap and --gdp (₹ lakh crore each), --ratio, or a --csv history")
        last = history[-1]
        mcap, gdp, ratio, cur_date = last["mcap"], last["gdp"], last["ratio"], last["date"]
    check_ratio(ratio, "mcap-gdp")
    lo, hi = MCAP_GDP_LONG_RUN
    res = {"command": "mcap-gdp", "as_of": cur_date, "mcap_lakh_crore": mcap, "gdp_lakh_crore": gdp,
           "gdp_basis": gdp_basis, "ratio_pct": ratio, "band": mcap_gdp_band(ratio),
           "bands": [{"below_pct": None if math.isinf(c) else c, "label": lab} for c, lab in MCAP_GDP_BANDS],
           "long_run_average_pct": [lo, hi],
           "premium_to_long_run_pct": [(ratio / hi - 1) * 100, (ratio / lo - 1) * 100],
           "mcap_at_long_run_average": [gdp * lo / 100, gdp * hi / 100] if gdp else None,
           "references": MCAP_GDP_REFERENCES, "history": None, "notes": []}
    if history:
        vals = [h["ratio"] for h in history]
        if cur_date is None:
            vals = vals + [ratio]
        st = rank_stats(vals, ratio)
        res["history"] = {"observations": len(vals), "first_date": history[0]["date"],
                          "last_date": history[-1]["date"], **st, "min": min(vals),
                          "median": statistics.median(vals), "max": max(vals), "thin": len(vals) < DEFAULTS["thin"]}
    res["read"] = mcap_gdp_read(res)
    return res


def mcap_gdp_read(res) -> list:
    r, (p_hi, p_lo) = res["ratio_pct"], res["premium_to_long_run_pct"]
    lo, hi = res["long_run_average_pct"]
    out = [f"Market cap ÷ GDP is {pct(r)} — {res['band']} on the India bands (cheap < 60, fair 60–100, elevated "
           f"100–120, expensive 120–140, extreme 140+).",
           f"That is {pct(p_hi, 0, True)} to {pct(p_lo, 0, True)} against the {lo:g}–{hi:g}% long-period averages "
           "brokerages have quoted; reference points: about 55% in Mar-2020, 149% at the Dec-2007 peak, "
           "130–150% in 2024–25."]
    h = res["history"]
    if h:
        out.append(f"In your history ({h['observations']} observations from {dd(h['first_date'])}) this is the "
                   f"{ordinal(h['percentile'])} percentile; the ratio was higher {pct(h['pct_time_above'], 0)} of the "
                   f"time" + (" (few observations: rough)." if h["thin"] else "."))
    out.append("The ratio drifts up as more of the economy lists (large IPOs, PSU listings), so compare with "
               "recent years as well as the long average. State which GDP you used (trailing four quarters, the "
               "last full fiscal year or a current-year estimate): the choice moves the ratio by 5–10 points.")
    return out


def print_mcap_gdp(res) -> None:
    print("Market cap to GDP — India" + (f" — {dd(res['as_of'])}" if res["as_of"] else ""))
    if res["mcap_lakh_crore"] is not None:
        basis = f" ({res['gdp_basis']})" if res["gdp_basis"] else ""
        print(f"Listed market cap ₹{indian(res['mcap_lakh_crore'], 1)} lakh crore ÷ nominal GDP "
              f"₹{indian(res['gdp_lakh_crore'], 1)} lakh crore{basis} = {pct(res['ratio_pct'])} → {res['band']}")
        a = res["mcap_at_long_run_average"]
        print(f"Market cap at the long-period average ratio would be ₹{indian(a[0], 0)}–{indian(a[1], 0)} lakh crore "
              "(arithmetic, not a target).")
    else:
        print(f"Ratio {pct(res['ratio_pct'])} → {res['band']}")
    print("\nReference points (published figures; GDP basis differs by source):")
    for ref in res["references"]:
        print(f"  {ref['period']:<22} {ref['ratio_pct']:>9}%   {ref['source']}")
    print("\nRead:")
    for line in res["read"]:
        print(f"  - {line}")
    for x in res["notes"]:
        print(f"⚠ {x}")
    print(DISCLAIMER)


# ---------------------------------------------------------------------------
# froth: 0–100 froth / complacency score
# ---------------------------------------------------------------------------
def pwl(x: float, pts) -> float:
    """Piecewise-linear score through (input, score) anchors sorted by input; flat beyond the ends."""
    if x <= pts[0][0]:
        return float(pts[0][1])
    if x >= pts[-1][0]:
        return float(pts[-1][1])
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return float(pts[-1][1])


ANCHORS = {
    "yield_gap": [(-4.0, 100), (-1.0, 0)],                       # earnings yield − G-sec, points
    "mcap_gdp": [(60, 0), (85, 25), (110, 50), (130, 75), (150, 100)],
    "smid_premium": [(-10, 0), (0, 20), (20, 50), (40, 75), (70, 100)],   # mid/small P/E premium over large, %
    "vix": [(9.5, 100), (11, 80), (13, 50), (16, 20), (20, 0)],  # complacency: low VIX scores high
    "breadth": [(50, 0), (70, 40), (85, 80), (95, 100)],         # market-breadth health score
    "ipo_per_month": [(1, 0), (3, 35), (5, 65), (8, 100)],       # mainboard listings per month
    "ipo_gain": [(0, 0), (10, 30), (25, 65), (40, 100)],         # average listing-day gain, %
    "sme_subscription": [(5, 0), (25, 30), (100, 70), (300, 100)],   # median SME IPO subscription, times
    "sme_over_100x": [(0, 0), (10, 30), (25, 60), (45, 100)],    # % of SME IPOs subscribed 100×+
    "sip_yoy": [(0, 0), (10, 20), (25, 50), (40, 80), (60, 100)],
    "demat_ratio": [(0.6, 0), (1.0, 35), (1.5, 75), (2.0, 100)],  # new accounts ÷ own 12-month average
    "demat_lakh": [(10, 0), (20, 30), (30, 60), (40, 85), (50, 100)],   # new accounts per month, lakh
    "fno_yoy": [(0, 0), (25, 40), (50, 70), (100, 100)],
    "mtf_yoy": [(0, 0), (20, 30), (40, 60), (60, 85), (80, 100)],
}

# key: (label, min, max, unit hint). Anything outside the range is rejected as a typo or a unit slip.
INPUTS = {
    "valuation_percentile": ("NIFTY P/E richness percentile from `pe`", 0, 100, "0–100"),
    "pe": ("NIFTY 50 trailing P/E", 1, 200, "times"),
    "gsec": ("India 10-year G-sec yield", 0.5, 25, "%"),
    "yield_gap": ("earnings yield − G-sec yield", -25, 25, "percentage points"),
    "yield_gap_percentile": ("percentile of the yield gap in its own history (low = rich)", 0, 100, "0–100"),
    "mcap_gdp": ("market cap ÷ GDP", 10, 500, "%"),
    "mcap": ("listed market cap", 1, 100000, "₹ lakh crore"),
    "gdp": ("nominal GDP", 1, 100000, "₹ lakh crore"),
    "largecap_pe": ("NIFTY 50 / NIFTY 100 P/E", 1, 200, "times"),
    "midcap_pe": ("NIFTY Midcap 150/100 P/E", 1, 500, "times"),
    "smallcap_pe": ("NIFTY Smallcap 250/100 P/E", 1, 500, "times"),
    "smid_premium": ("mid/small-cap P/E premium over large caps", -90, 500, "%"),
    "vix": ("India VIX close", 3, 100, "index points"),
    "breadth_score": ("market-breadth health score", 0, 100, "0–100"),
    "ipo_count": ("mainboard IPOs listed in the last ipo_months", 0, 500, "count"),
    "ipo_months": ("months covered by ipo_count", 1, 24, "months"),
    "ipo_listing_gain": ("average listing-day gain of those IPOs", -100, 500, "%"),
    "sme_subscription": ("median SME IPO subscription", 0, 5000, "times"),
    "sme_over_100x_pct": ("share of SME IPOs subscribed 100× or more", 0, 100, "%"),
    "sip_inflow": ("monthly SIP contribution", 1, 10000000, "₹ crore"),
    "sip_inflow_year_ago": ("monthly SIP contribution, same month last year", 1, 10000000, "₹ crore"),
    "sip_yoy": ("SIP contribution growth", -100, 1000, "% YoY"),
    "demat_new": ("new demat accounts in the latest month", 0, 100000, "lakh"),
    "demat_avg_12m": ("average new demat accounts per month, last 12 months", 0.01, 100000, "lakh"),
    "fno_yoy": ("growth in individual-investor F&O activity", -100, 1000, "% YoY"),
    "mtf_book": ("MTF funded book outstanding", 1, 100000000, "₹ crore"),
    "mtf_book_year_ago": ("MTF funded book a year earlier", 1, 100000000, "₹ crore"),
    "mtf_yoy": ("MTF book growth", -100, 1000, "% YoY"),
}
PARAM_KEYS = {"ipo_months"}        # settings, not dated observations
# Comparison bases that are old by design (a year-ago value, a trailing average, annual GDP): they do not
# make a component stale. Staleness is judged on the current observation(s).
REFERENCE_KEYS = {"sip_inflow_year_ago", "mtf_book_year_ago", "demat_avg_12m", "gdp"}


def _take(inp, *keys):
    got = [inp[k] for k in keys if k in inp]
    return got if len(got) == len(keys) else None


def c_valuation(inp):
    if "valuation_percentile" not in inp:
        return None
    v = inp["valuation_percentile"]["value"]
    return v, f"{ordinal(v)} percentile", ["valuation_percentile"]


def c_yield_gap(inp):
    if "yield_gap_percentile" in inp:
        p = inp["yield_gap_percentile"]["value"]
        return 100 - p, f"gap at its {ordinal(p)} percentile (low = rich)", ["yield_gap_percentile"]
    if "yield_gap" in inp:
        g, used = inp["yield_gap"]["value"], ["yield_gap"]
        text = f"gap {num(g, 2, True)} pp"
    elif _take(inp, "pe", "gsec"):
        pe, y = inp["pe"]["value"], inp["gsec"]["value"]
        g, used = 100 / pe - y, ["pe", "gsec"]
        text = f"gap {num(g, 2, True)} pp (P/E {num(pe, 1)}, G-sec {pct(y, 2)})"
    else:
        return None
    return pwl(g, ANCHORS["yield_gap"]), text, used


def c_mcap_gdp(inp):
    if "mcap_gdp" in inp:
        r, used = inp["mcap_gdp"]["value"], ["mcap_gdp"]
    elif _take(inp, "mcap", "gdp"):
        r, used = inp["mcap"]["value"] / inp["gdp"]["value"] * 100, ["mcap", "gdp"]
        check_ratio(r, "froth mcap/gdp")
    else:
        return None
    return pwl(r, ANCHORS["mcap_gdp"]), f"{pct(r, 0)} of GDP ({mcap_gdp_band(r)})", used


def c_smid(inp):
    if "smid_premium" in inp:
        p, used = inp["smid_premium"]["value"], ["smid_premium"]
        text = f"{pct(p, 0, True)} premium"
    else:
        base = "largecap_pe" if "largecap_pe" in inp else "pe" if "pe" in inp else None
        legs = [k for k in ("midcap_pe", "smallcap_pe") if k in inp]
        if not base or not legs:
            return None
        avg = statistics.fmean(inp[k]["value"] for k in legs)
        p, used = (avg / inp[base]["value"] - 1) * 100, [base] + legs
        text = (f"{pct(p, 0, True)} premium (" + ", ".join(f"{k.split('_')[0]} {num(inp[k]['value'], 1)}"
                                                           for k in legs)
                + f" vs large {num(inp[base]['value'], 1)})")
    return pwl(p, ANCHORS["smid_premium"]), text, used


def c_vix(inp):
    if "vix" not in inp:
        return None
    v = inp["vix"]["value"]
    return pwl(v, ANCHORS["vix"]), f"India VIX {num(v, 1)}", ["vix"]


def c_breadth(inp):
    if "breadth_score" not in inp:
        return None
    v = inp["breadth_score"]["value"]
    return pwl(v, ANCHORS["breadth"]), f"breadth score {num(v, 0)}/100", ["breadth_score"]


def _mean_of(parts):
    return statistics.fmean(s for s, _ in parts), "; ".join(t for _, t in parts)


def c_ipo(inp):
    parts, used = [], []
    if "ipo_count" in inp:
        months = inp["ipo_months"]["value"] if "ipo_months" in inp else 3.0
        rate = inp["ipo_count"]["value"] / months
        parts.append((pwl(rate, ANCHORS["ipo_per_month"]),
                      f"{num(inp['ipo_count']['value'], 0)} mainboard IPOs in {months:g} months ({rate:.1f}/month)"))
        used.append("ipo_count")
    if "ipo_listing_gain" in inp:
        g = inp["ipo_listing_gain"]["value"]
        parts.append((pwl(g, ANCHORS["ipo_gain"]), f"average listing-day gain {pct(g, 0, True)}"))
        used.append("ipo_listing_gain")
    if not parts:
        return None
    return (*_mean_of(parts), used)


def c_sme(inp):
    parts, used = [], []
    if "sme_subscription" in inp:
        x = inp["sme_subscription"]["value"]
        parts.append((pwl(x, ANCHORS["sme_subscription"]), f"median SME IPO subscribed {num(x, 0)}×"))
        used.append("sme_subscription")
    if "sme_over_100x_pct" in inp:
        x = inp["sme_over_100x_pct"]["value"]
        parts.append((pwl(x, ANCHORS["sme_over_100x"]), f"{pct(x, 0)} of SME IPOs subscribed 100×+"))
        used.append("sme_over_100x_pct")
    if not parts:
        return None
    return (*_mean_of(parts), used)


def _yoy(inp, direct, now, then, unit="₹", suffix=" cr"):
    if direct in inp:
        g = inp[direct]["value"]
        return g, f"{pct(g, 0, True)} YoY", [direct]
    if _take(inp, now, then):
        a, b = inp[now]["value"], inp[then]["value"]
        g = (a / b - 1) * 100
        return g, f"{pct(g, 0, True)} YoY ({unit}{indian(b, 0)} → {unit}{indian(a, 0)}{suffix})", [now, then]
    return None


def c_sip(inp):
    got = _yoy(inp, "sip_yoy", "sip_inflow", "sip_inflow_year_ago")
    if not got:
        return None
    g, text, used = got
    return pwl(g, ANCHORS["sip_yoy"]), "SIP " + text, used


def c_demat(inp):
    if "demat_new" not in inp:
        return None
    n = inp["demat_new"]["value"]
    if "demat_avg_12m" in inp:
        avg = inp["demat_avg_12m"]["value"]
        r = n / avg
        return (pwl(r, ANCHORS["demat_ratio"]),
                f"{num(n, 1)} lakh new accounts = {r:.2f}× the 12-month average ({num(avg, 1)} lakh)",
                ["demat_new", "demat_avg_12m"])
    return pwl(n, ANCHORS["demat_lakh"]), f"{num(n, 1)} lakh new accounts in the month", ["demat_new"]


def c_fno(inp):
    if "fno_yoy" not in inp:
        return None
    g = inp["fno_yoy"]["value"]
    return pwl(g, ANCHORS["fno_yoy"]), f"individual F&O activity {pct(g, 0, True)} YoY", ["fno_yoy"]


def c_mtf(inp):
    got = _yoy(inp, "mtf_yoy", "mtf_book", "mtf_book_year_ago")
    if not got:
        return None
    g, text, used = got
    return pwl(g, ANCHORS["mtf_yoy"]), "MTF book " + text, used


# key, group, label, weight, scorer, where to find the number, what would fill it
COMPONENTS = [
    ("valuation", "Valuation", "NIFTY P/E percentile", 16, c_valuation,
     "niftyindices.com → Reports → Historical Data → P/E, P/B & Div Yield, then `pe` (or --csv here)",
     "valuation_percentile"),
    ("yield_gap", "Valuation", "Earnings yield vs G-sec", 8, c_yield_gap,
     "10-year G-sec: RBI (DBIE) or CCIL/FBIL benchmark yield; P/E from the valuation file; or run `yieldgap`",
     "pe + gsec, yield_gap or yield_gap_percentile"),
    ("mcap_gdp", "Valuation", "Market cap ÷ GDP", 8, c_mcap_gdp,
     "BSE all-listed market capitalisation (bseindia.com) and MoSPI nominal GDP; or run `mcap-gdp`",
     "mcap_gdp, or mcap + gdp"),
    ("smid_premium", "Valuation", "Mid/small-cap P/E premium", 8, c_smid,
     "niftyindices.com P/E for NIFTY Midcap 150 and Smallcap 250 vs NIFTY 50 on the same date",
     "midcap_pe and/or smallcap_pe with largecap_pe (or pe), or smid_premium"),
    ("vix", "Complacency", "India VIX (low = complacent)", 8, c_vix,
     "NSE India VIX closing value", "vix"),
    ("breadth", "Complacency", "Breadth extreme", 5, c_breadth,
     "market-breadth skill: breadth.py status --json (score.value); pass the file to --breadth-json",
     "breadth_score"),
    ("ipo", "Issuance", "Mainboard IPO heat", 10, c_ipo,
     "exchange listing pages or an IPO tracker: mainboard listings in the last 3 months and each one's "
     "listing-day close vs issue price", "ipo_count (+ ipo_months) and/or ipo_listing_gain"),
    ("sme_ipo", "Issuance", "SME IPO subscription", 7, c_sme,
     "BSE SME / NSE Emerge subscription tables or an IPO tracker", "sme_subscription and/or sme_over_100x_pct"),
    ("sip", "Retail", "SIP inflow growth", 6, c_sip,
     "AMFI monthly note (amfiindia.com), out around the 10th: SIP contribution for the month",
     "sip_inflow + sip_inflow_year_ago, or sip_yoy"),
    ("demat", "Retail", "New demat accounts", 6, c_demat,
     "CDSL + NSDL monthly statistics, SEBI Bulletin", "demat_new (+ demat_avg_12m)"),
    ("fno", "Retail", "Retail F&O activity", 6, c_fno,
     "NSE Market Pulse / SEBI bulletin: individual investors' index-options premium turnover or the count "
     "of unique F&O traders, year over year", "fno_yoy"),
    ("mtf", "Leverage", "Margin-trading (MTF) book", 12, c_mtf,
     "NSE and BSE daily margin-trading reports (funded amount outstanding); monthly brokerage/rating-agency "
     "notes compile them", "mtf_book + mtf_book_year_ago, or mtf_yoy"),
]
assert sum(c[3] for c in COMPONENTS) == 100

PHASES = ((20, "Depressed"), (40, "Normal"), (60, "Warm"), (80, "Frothy"), (math.inf, "Euphoric"))
POSTURE = {
    "Depressed": {
        "size_multiplier": 1.0,
        "new_buying": "Full planned size, staggered over weeks. Fear and apathy price in a lot; the market "
                      "regime still decides when to add.",
        "stops": "Normal stops. Expect volatility, so size for wider swings rather than tightening.",
        "ipo": "Few issues come to market; the ones that do are usually priced to sell.",
        "leverage": "Avoid MTF until the regime confirms an uptrend."},
    "Normal": {
        "size_multiplier": 1.0,
        "new_buying": "Normal sizing inside the market-regime exposure ceiling.",
        "stops": "Standard stops and trails.",
        "ipo": "Judge each issue on business and price.",
        "leverage": "Modest and planned, if used at all."},
    "Warm": {
        "size_multiplier": 0.8,
        "new_buying": "Keep buying the best setups at about 80% of normal size; stop adding to laggards and "
                      "to anything bought only because it was rising.",
        "stops": "Start trailing winners; move stops to breakeven on extended small and mid caps.",
        "ipo": "Be selective: skip issues priced above the best listed peer; do not chase listing-day pops.",
        "leverage": "Do not increase MTF exposure."},
    "Frothy": {
        "size_multiplier": 0.5,
        "new_buying": "Half size, highest-quality setups only, and no averaging up. Let cash build as stops "
                      "are hit rather than redeploying at once.",
        "stops": "Tighten trails (around 1.5 × ATR) and book partial profits into strength, small and mid "
                 "caps first.",
        "ipo": "Avoid SME issues and OFS-heavy issues at peak multiples; treat subscription multiples as a "
               "crowding signal, not as proof of quality.",
        "leverage": "Cut MTF; never pledge holdings to fund new buys."},
    "Euphoric": {
        "size_multiplier": 0.25,
        "new_buying": "Pilot positions only (about a quarter of normal size). Protecting capital matters more "
                      "than the last leg; euphoria can still run for months, so this is sizing, not a call.",
        "stops": "Tight trails on everything; honour every stop; take profits on vertical moves.",
        "ipo": "Do not apply for listing gains; assume allotments are tiny and reversals fast.",
        "leverage": "None."},
}


def phase_for(score: float) -> str:
    return next(label for cut, label in PHASES if score < cut)


def check_input(key: str, value: float, where: str) -> float:
    if key not in INPUTS:
        close = [k for k in INPUTS if k.replace("_", "") == key.replace("_", "").replace("-", "")]
        raise ValuationError(f"{where}: unknown input {key!r}" + (f" (did you mean {close[0]}?)" if close else
                                                                 f"; valid inputs: {', '.join(INPUTS)}"))
    label, lo, hi, unit = INPUTS[key]
    if not lo <= value <= hi:
        raise ValuationError(f"{where}: {key} = {value:g} is outside {lo:g}–{hi:g} ({label}, {unit})")
    return value


def _coerce(raw, where):
    if isinstance(raw, bool):
        raise ValuationError(f"{where}: not a number: {raw!r}")
    if isinstance(raw, (int, float)):
        if not math.isfinite(raw):
            raise ValuationError(f"{where}: not a finite number")
        return float(raw)
    return to_num(raw, where)


def load_inputs(path, order: str | None = None) -> dict:
    """Froth inputs from JSON ({"as_of": .., "inputs": {key: number | {value, as_of, source}}}) or CSV
    (input,value,as_of,source). Returns {key: {value, as_of, source, origin}}."""
    p = Path(path)
    if not p.is_file():
        raise ValuationError(f"{path}: file not found")
    name, out = p.name, {}
    if p.suffix.lower() == ".json":
        try:
            data = json.loads(p.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as e:
            raise ValuationError(f"{name}: not valid JSON ({e.msg}, line {e.lineno})")
        if not isinstance(data, dict):
            raise ValuationError(f"{name}: expected a JSON object")
        default = parse_one(data["as_of"], f"{name} as_of", order) if data.get("as_of") else None
        items = data.get("inputs")
        if items is None:
            items = {k: v for k, v in data.items() if k not in ("as_of", "notes", "source", "comment")}
        if not isinstance(items, dict):
            raise ValuationError(f"{name}: 'inputs' must be an object of input: value")
        for key, raw in items.items():
            if str(key).startswith("_"):
                continue
            where = f"{name} {key}"
            as_of, source = default, None
            if isinstance(raw, dict):
                if raw.get("as_of"):
                    as_of = parse_one(raw["as_of"], f"{where} as_of", order)
                source, raw = raw.get("source"), raw.get("value")
            value = _coerce(raw, where)
            if value is None:
                continue
            out[key] = {"value": check_input(key, value, name), "as_of": as_of,
                        "source": str(source) if source else None, "origin": name}
        return out
    raw = read_rows(p)
    hi = next((i for i, r in enumerate(raw[:10]) if any(norm(c) == "value" for c in r)), None)
    if hi is None:
        raise ValuationError(f"{name}: no header row with a value column (expected input,value,as_of,source)")
    hn = [norm(c) for c in raw[hi]]
    kcol = next((i for i, h in enumerate(hn) if h in ("input", "key", "name", "metric", "indicator")), None)
    vcol = hn.index("value")
    dcol = next((i for i, h in enumerate(hn) if h in ("as of", "asof", "date")), None)
    scol = next((i for i, h in enumerate(hn) if h in ("source", "src")), None)
    if kcol is None:
        raise ValuationError(f"{name}: no input/key column (expected input,value,as_of,source)")
    body = raw[hi + 1:]
    dates = parse_dates([_cell(r, dcol) for r in body], order, f"{name} as_of column") if dcol is not None \
        else [None] * len(body)
    for r, d in zip(body, dates):
        key = _cell(r, kcol).strip()
        if not key or key.startswith(("#", "_")):
            continue
        where = f"{name} {key}"
        if dcol is not None and _cell(r, dcol).strip() and d is None:
            raise ValuationError(f"{where}: cannot read as_of date {_cell(r, dcol)!r}; {DATE_FORMATS_HELP}")
        value = to_num(_cell(r, vcol), where)
        if value is None:
            continue
        if key in out:
            raise ValuationError(f"{name}: input {key!r} appears twice")
        src = _cell(r, scol).strip()
        out[key] = {"value": check_input(key, value, name), "as_of": d, "source": src or None, "origin": name}
    return out


def load_breadth_json(path) -> dict:
    """Health score written by the market-breadth skill (breadth.py status --json)."""
    p = Path(path)
    if not p.is_file():
        raise ValuationError(f"{path}: file not found")
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
        value = data["score"]["value"]
    except (json.JSONDecodeError, KeyError, TypeError):
        raise ValuationError(f"{p.name}: not a market-breadth status JSON (no score.value)")
    value = _coerce(value, f"{p.name} score.value")
    if value is None:
        raise ValuationError(f"{p.name}: score.value is empty (breadth history too short?)")
    as_of = parse_one(data["as_of"], f"{p.name} as_of") if data.get("as_of") else None
    label = (data.get("score") or {}).get("label")
    return {"value": check_input("breadth_score", value, p.name), "as_of": as_of,
            "source": f"market-breadth {p.name}" + (f" ({label})" if label else ""), "origin": p.name}


def analyse_froth(inputs: dict, asof: date, max_age_days: int = DEFAULTS["max_age_days"],
                  drop_stale: bool = False) -> dict:
    """inputs: {key: {value, as_of, source}} (bare numbers are accepted too)."""
    inp = {}
    for k, v in inputs.items():
        rec = dict(v) if isinstance(v, dict) else {"value": v}
        rec.setdefault("as_of", None)
        rec.setdefault("source", None)
        rec["value"] = check_input(k, float(rec["value"]), "froth")
        if rec["as_of"] and rec["as_of"] > asof:
            raise ValuationError(f"input {k} is dated {dd(rec['as_of'])}, after the evaluation date {dd(asof)}; "
                                 "fix the date or pass --as-of")
        inp[k] = rec
    comps, missing, warnings = [], [], []
    for key, group, label, weight, fn, hint, needs in COMPONENTS:
        got = fn(inp)
        if got is None:
            missing.append({"key": key, "group": group, "label": label, "weight": weight, "needs": needs,
                            "where": hint})
            continue
        sub, text, used = got
        live = [k for k in used if k not in PARAM_KEYS and k not in REFERENCE_KEYS]
        dated = [inp[k]["as_of"] for k in live if inp[k]["as_of"]]
        undated = [k for k in live if not inp[k]["as_of"]]
        as_of = min(dated) if dated else None
        age = (asof - as_of).days if as_of else None
        sources = sorted({inp[k]["source"] for k in used if inp[k]["source"]})
        comps.append({"key": key, "group": group, "label": label, "input": text, "inputs_used": used,
                      "sub_score": max(0.0, min(100.0, sub)), "weight": weight, "as_of": as_of, "age_days": age,
                      "stale": age is not None and age > max_age_days, "undated": undated,
                      "source": "; ".join(sources) or None, "where": hint})
    stale = [c for c in comps if c["stale"]]
    if stale:
        warnings.append(f"stale input(s), older than {max_age_days} days at {dd(asof)}: "
                        + ", ".join(f"{c['label']} ({dd(c['as_of'])}, {c['age_days']} days)" for c in stale)
                        + (" — dropped from the score" if drop_stale else " — still scored; refresh them or "
                           "pass --drop-stale"))
        if drop_stale:
            for c in stale:
                missing.append({"key": c["key"], "group": c["group"], "label": c["label"], "weight": c["weight"],
                                "needs": "a fresh value (dropped as stale)", "where": c["where"]})
            comps = [c for c in comps if not c["stale"]]
    undated = [c["label"] for c in comps if c["undated"]]
    if undated:
        warnings.append("no as_of date recorded for: " + ", ".join(undated)
                        + " — staleness cannot be checked (use an inputs file or --inputs-as-of)")
    if not comps:
        raise ValuationError("no froth inputs to score; supply at least one (for example --vix, "
                             "--valuation-pct, --mtf-yoy or an --inputs file)")
    covered = sum(c["weight"] for c in comps)
    score = 0.0
    for c in comps:
        c["effective_weight"] = c["weight"] / covered * 100
        c["points"] = c["effective_weight"] * c["sub_score"] / 100
        score += c["points"]
    groups = {}
    for c in comps:
        g = groups.setdefault(c["group"], {"weight": 0.0, "points": 0.0})
        g["weight"] += c["weight"]
        g["points"] += c["weight"] * c["sub_score"]
    groups = {k: {"weight_scored": v["weight"], "score": v["points"] / v["weight"]} for k, v in groups.items()}
    confidence = "high" if covered >= 75 else "medium" if covered >= 50 else "low"
    if covered < 50:
        warnings.append(f"coverage is only {covered:.0f}% of the weight ({len(comps)} of {len(COMPONENTS)} "
                        "components): treat the phase as indicative and fill the missing inputs")
    if len(groups) == 1 and len(comps) < len(COMPONENTS):
        warnings.append(f"every scored input is from the {next(iter(groups))} group; froth needs issuance, "
                        "leverage and participation evidence too")
    phase = phase_for(score)
    res = {"command": "froth", "as_of": asof, "score": score, "phase": phase, "coverage_pct": float(covered),
           "confidence": confidence, "components_scored": len(comps), "components_total": len(COMPONENTS),
           "max_age_days": max_age_days, "components": comps, "groups": groups, "missing": missing,
           "posture": {"framing": "risk management, not a market call", **POSTURE[phase]},
           "phases": [{"below": None if math.isinf(c) else c, "label": lab} for c, lab in PHASES],
           "warnings": warnings}
    res["read"] = froth_read(res)
    return res


def froth_read(res) -> list:
    comps = sorted(res["components"], key=lambda c: -c["points"])
    hot = [c for c in comps if c["sub_score"] >= 70]
    cool = [c for c in comps if c["sub_score"] <= 25]
    out = [f"Froth score {res['score']:.0f}/100 → {res['phase']} ({res['components_scored']} of "
           f"{res['components_total']} components, {res['coverage_pct']:.0f}% of the weight; "
           f"{res['confidence']} confidence)."]
    if hot:
        out.append("Running hot: " + "; ".join(f"{c['label']} ({c['input']})" for c in hot[:4]) + ".")
    if cool:
        out.append("Not frothy: " + "; ".join(f"{c['label']} ({c['input']})" for c in cool[:4]) + ".")
    g = res["groups"]
    if "Valuation" in g and len(g) > 1:
        others = [v["score"] for k, v in g.items() if k != "Valuation"]
        rest = statistics.fmean(others)
        if g["Valuation"]["score"] >= 60 and rest < 40:
            out.append("Valuations are rich but behaviour (issuance, leverage, retail activity) is calm: "
                       "expensive without euphoria, which can persist while domestic flows hold.")
        elif g["Valuation"]["score"] < 40 and rest >= 60:
            out.append("Behaviour is hot while index valuation is moderate: froth is likely concentrated in "
                       "small caps, SME issues or leveraged retail positions rather than the index.")
    out.append("The score ranks conditions against rough historical anchors; it does not say when a frothy "
               "market turns. Combine it with the market-regime label for timing.")
    return out


def print_froth(res) -> None:
    print(f"Froth / complacency score — as of {dd(res['as_of'], True)}")
    print(f"Score {res['score']:.0f}/100 → {res['phase']} | coverage {res['coverage_pct']:.0f}% "
          f"({res['components_scored']} of {res['components_total']} components) | confidence {res['confidence']}")
    print(f"\n{'Component':<30} {'Sub':>4} {'Wt':>3} {'Eff wt':>6} {'Pts':>5}  {'As of':<11}  Input")
    group = None
    for c in res["components"]:
        if c["group"] != group:
            group = c["group"]
            g = res["groups"][group]
            print(f"{group} (group score {g['score']:.0f})")
        flag = " ⚠stale" if c["stale"] else ""
        print(f"  {c['label']:<28} {c['sub_score']:>4.0f} {c['weight']:>3} {c['effective_weight']:>6.1f} "
              f"{c['points']:>5.1f}  {dd(c['as_of']):<11}  {c['input']}{flag}"
              + (f"  [{c['source']}]" if c["source"] else ""))
    print("Sub = 0–100 sub-score; Wt = base weight; Eff wt = weight after re-normalising over supplied inputs; "
          "Pts add up to the score.")
    print("Phases: <20 Depressed, 20–39 Normal, 40–59 Warm, 60–79 Frothy, 80+ Euphoric.")
    if res["missing"]:
        print("\nMissing (not scored):")
        for m in res["missing"]:
            print(f"  {m['label']} (weight {m['weight']}): needs {m['needs']} — {m['where']}")
    p = res["posture"]
    print(f"\nPosture for {res['phase']} ({p['framing']}):")
    print(f"  New buying : {p['new_buying']} (size × {p['size_multiplier']:g} of the market-regime allowance)")
    print(f"  Stops      : {p['stops']}")
    print(f"  IPOs       : {p['ipo']}")
    print(f"  Leverage   : {p['leverage']}")
    print("\nRead:")
    for line in res["read"]:
        print(f"  - {line}")
    for x in res["warnings"]:
        print(f"⚠ {x}")
    print(DISCLAIMER)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValuationError(message)


def _number(text: str) -> float:
    try:
        x = float(str(text).replace(",", "").replace("%", "").replace("₹", "").strip())
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}")
    if not math.isfinite(x):
        raise argparse.ArgumentTypeError(f"not a finite number: {text!r}")
    return x


def _windows(text: str) -> tuple:
    try:
        ys = tuple(sorted({int(x) for x in str(text).split(",") if x.strip()}))
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected years like 5,10: {text!r}")
    if not ys or min(ys) < 1 or max(ys) > 100:
        raise argparse.ArgumentTypeError(f"expected years between 1 and 100: {text!r}")
    return ys


def _bands(text: str) -> tuple:
    try:
        cuts = tuple(float(x) for x in str(text).split(","))
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected three percentiles like 25,75,90: {text!r}")
    if len(cuts) != 3 or not 0 < cuts[0] < cuts[1] < cuts[2] < 100:
        raise argparse.ArgumentTypeError(f"expected three rising percentiles between 0 and 100: {text!r}")
    return cuts


# froth flag -> input key
FROTH_FLAGS = [
    ("--valuation-pct", "valuation_percentile"), ("--pe", "pe"), ("--gsec", "gsec"), ("--yield-gap", "yield_gap"),
    ("--yield-gap-pct", "yield_gap_percentile"), ("--mcap-gdp", "mcap_gdp"), ("--mcap", "mcap"), ("--gdp", "gdp"),
    ("--largecap-pe", "largecap_pe"), ("--midcap-pe", "midcap_pe"), ("--smallcap-pe", "smallcap_pe"),
    ("--smid-premium", "smid_premium"), ("--vix", "vix"), ("--breadth", "breadth_score"),
    ("--ipo-count", "ipo_count"), ("--ipo-months", "ipo_months"), ("--ipo-gain", "ipo_listing_gain"),
    ("--sme-sub", "sme_subscription"), ("--sme-100x-pct", "sme_over_100x_pct"), ("--sip", "sip_inflow"),
    ("--sip-year-ago", "sip_inflow_year_ago"), ("--sip-yoy", "sip_yoy"), ("--demat-new", "demat_new"),
    ("--demat-avg", "demat_avg_12m"), ("--fno-yoy", "fno_yoy"), ("--mtf", "mtf_book"),
    ("--mtf-year-ago", "mtf_book_year_ago"), ("--mtf-yoy", "mtf_yoy"),
]


def build_parser() -> argparse.ArgumentParser:
    p = _Parser(prog="valuation.py", description="Indian market valuation and froth gauges (offline, stdlib).",
                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__.split("Sub-commands")[1])
    common = _Parser(add_help=False)
    common.add_argument("--json", action="store_true", help="strict JSON output")
    common.add_argument("--date-format", choices=["dmy", "mdy"], help="order of slash/dot dates in the files")
    common.add_argument("--as-of", help="evaluate as of this date (ignore later rows)")
    sub = p.add_subparsers(dest="cmd", required=True)

    hist = _Parser(add_help=False)
    hist.add_argument("--since", help="use history from this date only (2021-03-31 = consolidated P/E only)")
    hist.add_argument("--windows", type=_windows, default=DEFAULTS["windows"], help="look-back years (5,10)")
    hist.add_argument("--bands", type=_bands, default=DEFAULTS["bands"],
                      help="richness-percentile cuts for cheap/fair/expensive/extreme (25,75,90)")
    hist.add_argument("--index", help="index name when the CSV holds several (e.g. 'NIFTY 50')")

    c = sub.add_parser("pe", parents=[common, hist], help="P/E, P/B, dividend-yield percentiles and bands")
    c.add_argument("--csv", action="append", required=True,
                   help="valuation history CSV: Date, P/E, P/B, Div Yield (repeatable; files are merged)")

    y = sub.add_parser("yieldgap", parents=[common, hist], help="earnings yield minus 10-year G-sec yield")
    y.add_argument("--pe", type=_number, help="trailing P/E (overrides the latest value in --csv)")
    y.add_argument("--gsec", type=_number, help="India 10-year G-sec yield in percent, e.g. 6.55")
    y.add_argument("--csv", action="append", help="valuation history CSV (for the current P/E and gap history)")
    y.add_argument("--yields", help="G-sec yield history CSV: Date, Yield")
    y.add_argument("--max-gap-days", type=int, default=DEFAULTS["max_gap_days"],
                   help="how old a yield may be relative to a P/E date (45)")

    m = sub.add_parser("mcap-gdp", parents=[common], help="market cap to GDP with India bands")
    m.add_argument("--mcap", type=_number, help="total listed market cap, ₹ lakh crore (BSE or NSE)")
    m.add_argument("--gdp", type=_number, help="nominal GDP, ₹ lakh crore")
    m.add_argument("--ratio", type=_number, help="the ratio itself in percent, if that is all you have")
    m.add_argument("--gdp-basis", help="label for the GDP figure, e.g. 'FY26 provisional' or 'trailing 4 quarters'")
    m.add_argument("--csv", help="history CSV: date, mcap, gdp (or date, ratio)")

    f = sub.add_parser("froth", parents=[common], help="0–100 froth / complacency score")
    f.add_argument("--inputs", help="inputs file: JSON {as_of, inputs:{key: number|{value,as_of,source}}} or CSV "
                                    "input,value,as_of,source")
    f.add_argument("--inputs-as-of", help="as_of date for values typed as flags")
    f.add_argument("--max-age-days", type=int, default=DEFAULTS["max_age_days"],
                   help="flag inputs older than this many days (45)")
    f.add_argument("--drop-stale", action="store_true", help="leave stale inputs out of the score")
    f.add_argument("--csv", action="append", help="valuation history CSV: fills the P/E percentile and P/E")
    f.add_argument("--index", help="index name when --csv holds several")
    f.add_argument("--since", help="history start for the --csv percentile")
    f.add_argument("--breadth-json", help="market-breadth `status --json` output (reads score.value)")
    for flag, key in FROTH_FLAGS:
        label, _, _, unit = INPUTS[key]
        f.add_argument(flag, dest="in_" + key, type=_number, metavar="N", help=f"{label} ({unit})".replace("%", "%%"))
    return p


def main(argv=None) -> int:
    try:
        a = build_parser().parse_args(argv)
        return _run(a)
    except ValuationError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except (OSError, UnicodeDecodeError, csv.Error) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


def _out(a, res, printer) -> int:
    if a.json:
        emit_json(res)
    else:
        printer(res)
    return 0


def _run(a) -> int:
    order = a.date_format
    asof = parse_one(a.as_of, "--as-of", order) if a.as_of else None
    since = parse_one(a.since, "--since", order) if getattr(a, "since", None) else None
    if a.cmd == "pe":
        rows, meta, notes = load_valuation(a.csv, a.index, order)
        res = analyse_pe(rows, meta, a.windows, a.bands, since, asof)
        res["notes"] = notes + res["notes"]
        return _out(a, res, print_pe)
    if a.cmd == "yieldgap":
        if a.max_gap_days < 0:
            raise ValuationError("--max-gap-days must be 0 or more")
        pe_series, index, notes = None, "index", []
        if a.csv:
            rows, meta, notes = load_valuation(a.csv, a.index, order)
            pe_series = [(r["date"], r["pe"]) for r in rows if r["pe"] is not None]
            index = meta["index"]
            if not pe_series:
                raise ValuationError("the valuation file has no P/E values")
        yields = load_yields(a.yields, order) if a.yields else None
        res = analyse_yieldgap(a.pe, a.gsec, pe_series, yields, a.windows, a.bands, since, asof, a.max_gap_days,
                               index)
        res["notes"] = notes + res["notes"]
        return _out(a, res, print_yieldgap)
    if a.cmd == "mcap-gdp":
        history = load_mcap_gdp(a.csv, order) if a.csv else None
        return _out(a, analyse_mcap_gdp(a.mcap, a.gdp, a.ratio, history, asof, a.gdp_basis), print_mcap_gdp)
    # froth
    if a.max_age_days < 0:
        raise ValuationError("--max-age-days must be 0 or more")
    asof = asof or date.today()
    inputs = load_inputs(a.inputs, order) if a.inputs else {}
    flag_date = parse_one(a.inputs_as_of, "--inputs-as-of", order) if a.inputs_as_of else None
    extra = []
    if a.csv:
        rows, meta, notes = load_valuation(a.csv, a.index, order)
        pe_res = analyse_pe(rows, meta, since=since, asof=asof)
        extra += notes
        if "pe" not in pe_res["metrics"]:
            raise ValuationError("the valuation file has no P/E values")
        mp = pe_res["metrics"]["pe"]
        src = f"{', '.join(meta['files'])} ({mp['headline']['window']} window)"
        inputs.setdefault("valuation_percentile", {"value": mp["headline"]["richness_percentile"],
                                                   "as_of": mp["date"], "source": src, "origin": "csv"})
        inputs.setdefault("pe", {"value": mp["current"], "as_of": mp["date"], "source": src, "origin": "csv"})
        if pe_res["basis"]["spans_switch"]:
            extra.append("valuation percentile uses consolidated-basis P/E history only (from 31-Mar-2021)")
    if a.breadth_json:
        inputs["breadth_score"] = load_breadth_json(a.breadth_json)
    for flag, key in FROTH_FLAGS:
        v = getattr(a, "in_" + key)
        if v is not None:
            inputs[key] = {"value": check_input(key, v, flag), "as_of": flag_date, "source": None, "origin": "flag"}
    res = analyse_froth(inputs, asof, a.max_age_days, a.drop_stale)
    res["notes"] = extra
    if a.json:
        emit_json(res)
    else:
        print_froth(res)
        for x in extra:
            print(f"Note: {x}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
