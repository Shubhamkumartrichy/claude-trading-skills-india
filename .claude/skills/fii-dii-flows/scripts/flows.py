#!/usr/bin/env python3
"""Institutional flow analysis for Indian equities: FII/FPI and DII money, F&O positioning,
shareholding changes and bulk/block deals, read from CSV files you download.

Zero dependencies (Python 3.10+ stdlib). Works fully offline; nothing is fetched.

Sub-commands
  cash          FII/FPI and DII cash-market flows (₹ crore): latest session, 5/10/20-session and
                month-to-date sums, buying/selling streaks, how much FII selling DIIs absorbed,
                largest days, optional correlation with NIFTY returns (--nifty CSV)
  participants  NSE participant-wise F&O open interest (fao_participant_oi_DDMMYYYY.csv, one file or
                a folder of them): FII index-futures long %, long/short ratio, day-over-day change,
                index-option call/put positioning, client positioning as contrarian context, flags
  shareholding  quarterly shareholding pattern (promoter, FII, DII, MF, public, pledged %):
                quarter-over-quarter change, institutional accumulation, promoter selling, pledge flags
  deals         bulk and block deals: net quantity and ₹ value by symbol and by client, repeat
                buyers/sellers, same-day round trips

Dates
  Accepted without question: 2026-10-30, 20261030, 30-Oct-2026, 30-OCT-2026, 30 Oct 2026,
  Oct 30, 2026 and 30-10-2026 (dashes = day first, as NSE writes it). Slash or dot dates such as
  05/10/2026 are used only when the column itself proves the order (some value has a day above 12);
  otherwise pass --date-format dmy|mdy. The script never guesses.

Examples
  python3 flows.py cash --csv fii_dii.csv
  python3 flows.py cash --csv fii_dii.csv --nifty nifty.csv --json
  python3 flows.py participants --oi participant_oi/ --days 10
  python3 flows.py shareholding --csv shareholding.csv --symbol HDFCBANK
  python3 flows.py deals --csv bulk.csv --csv block.csv --symbol XYZ
"""
from __future__ import annotations

import argparse
import calendar
import csv
import json
import math
import re
import statistics
import sys
from datetime import date, datetime, timedelta
from pathlib import Path


class FlowError(Exception):
    """Bad input the user can fix; printed as a single `error:` line with exit code 2."""


DEFAULTS = {
    "top": 5,             # largest days / deals listed
    "lookback": 60,       # sessions searched for largest days and record streaks (~3 months)
    "min_corr_days": 10,  # fewer matched sessions than this and correlation is not reported
    "long_low": 20.0,     # FII index-futures long % at or below this = extreme short
    "long_high": 70.0,    # ...at or above this = crowded long
    "shift_pp": 5.0,      # one-day swing in long % worth flagging (typical day: 1-2 points)
    "min_change": 1.0,    # QoQ change (percentage points) in FII/DII/MF holding worth flagging
    "promoter_drop": 0.5,  # QoQ fall in promoter holding worth flagging
    "pledge_watch": 10.0,  # pledged % of promoter holding: watch
    "pledge_red": 20.0,    # ...red flag (same line as fundamental-screening)
    "pledge_severe": 50.0,  # ...SEBI makes promoters disclose reasons above 50% encumbrance
}
DISCLAIMER = "Educational analysis, not investment advice."


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------
def indian(x, d=2, sign=False) -> str:
    """Indian digit grouping: 12,34,567.89. None/NaN -> an em dash."""
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return "—"
    if round(abs(x), d) == 0:
        x = 0.0                              # no "-0.00"
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


def cr(x, d=2) -> str:
    """Signed ₹ crore amount: +₹1,234.50 cr / -₹987.00 cr."""
    if x is None:
        return "—"
    if round(abs(x), d) == 0:
        return "₹0 cr"
    return ("-" if x < 0 else "+") + "₹" + indian(abs(x), d) + " cr"


def pct(x, d=1, sign=False) -> str:
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return "—"
    return (f"{x:+.{d}f}" if sign else f"{x:.{d}f}") + "%"


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
_TEXT_FORMATS = ("%d-%b-%Y", "%d %b %Y", "%d-%B-%Y", "%d %B %Y", "%d-%b-%y", "%d %b %y", "%d %b, %Y",
                 "%d %B, %Y", "%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y", "%d/%b/%Y", "%d.%b.%Y",
                 "%d-%b, %Y")
DATE_FORMATS_HELP = "use YYYY-MM-DD, DD-Mon-YYYY (30-Oct-2026) or DD-MM-YYYY"


def _mkdate(y, m, d, raw):
    try:
        return date(int(y), int(m), int(d))
    except ValueError:
        raise FlowError(f"invalid date {raw!r}")


def _classify(raw):
    """("date", date) for unambiguous text, ("num", (a, b, year, sep, raw)) for numeric day/month forms,
    or (None, None) when the cell is not a date at all (e.g. a 'Total' row)."""
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
            raise FlowError(f"two-digit year in {raw!r} is ambiguous; {DATE_FORMATS_HELP}")
        return "num", (int(m.group(1)), int(m.group(3)), int(m.group(4)), m.group(2), str(raw).strip())
    for fmt in _TEXT_FORMATS:
        try:
            return "date", datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None, None


def parse_dates(values, order: str | None = None, what: str = "date") -> list:
    """Parse a whole date column. Returns a list of date or None (cell is not a date).

    Dash-separated numbers follow NSE (DD-MM-YYYY). Slash/dot numbers need evidence from the column
    (a first part above 12 proves day-first, a second part above 12 proves month-first) or an explicit
    order ('dmy'/'mdy'); without that the column is rejected as ambiguous.
    """
    kinds = [_classify(v) for v in values]
    slash = [k[1] for k in kinds if k[0] == "num" and k[1][3] != "-"]
    slash_order = order
    if slash and not order:
        dayfirst = [x for x in slash if x[0] > 12]
        monthfirst = [x for x in slash if x[1] > 12]
        if dayfirst and monthfirst:
            raise FlowError(f"{what}: column mixes day-first ({dayfirst[0][4]!r}) and month-first "
                            f"({monthfirst[0][4]!r}) dates; fix the file or {DATE_FORMATS_HELP}")
        if dayfirst:
            slash_order = "dmy"
        elif monthfirst:
            slash_order = "mdy"
        else:
            raise FlowError(f"{what}: ambiguous date {slash[0][4]!r} — could be day-first or month-first and "
                            f"no value in the column settles it; pass --date-format dmy|mdy or {DATE_FORMATS_HELP}")
    out = []
    for kind, val in kinds:
        if kind is None:
            out.append(None)
        elif kind == "date":
            out.append(val)
        else:
            a, b, y, sep, raw = val
            o = (slash_order if sep != "-" else (order or "dmy"))
            day, mon = (a, b) if o == "dmy" else (b, a)
            if mon > 12 and sep == "-" and not order:
                raise FlowError(f"{what}: {raw!r} is not DD-MM-YYYY (NSE order); if the file is month-first "
                                "pass --date-format mdy")
            out.append(_mkdate(y, mon, day, raw))
    return out


def parse_one(raw: str, flag: str, order: str | None = None) -> date:
    got = parse_dates([raw], order, flag)[0]
    if got is None:
        raise FlowError(f"{flag}: cannot read date {raw!r}; {DATE_FORMATS_HELP}")
    return got


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------
def norm(h) -> str:
    """'BUY VALUE(₹ Crores)' -> 'buy value crores'; 'Future Stock Short   ' -> 'future stock short'."""
    return re.sub(r"[^a-z0-9]+", " ", str(h or "").lower()).strip()


def to_num(raw, where: str):
    """'1,23,456.70' / '-4,000.00' / '(512.3)' / '45.10%' -> float; blank or dash -> None."""
    if raw is None:
        return None
    s = str(raw).strip().replace("−", "-").replace(",", "").replace("₹", "").replace("%", "").strip()
    if s in ("", "-", "--", "—", "NA", "N/A", "na", "nil", "Nil", "NIL", "null"):
        return None
    neg = s.startswith("(") and s.endswith(")")
    if neg:
        s = s[1:-1]
    try:
        x = float(s)
    except ValueError:
        raise FlowError(f"{where}: not a number: {raw!r}")
    if not math.isfinite(x):
        raise FlowError(f"{where}: not a finite number: {raw!r}")
    return -x if neg else x


def read_rows(path) -> list:
    p = Path(path)
    if not p.is_file():
        raise FlowError(f"{path}: file not found")
    with open(p, newline="", encoding="utf-8-sig", errors="replace") as fh:
        rows = [r for r in csv.reader(fh)]
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        raise FlowError(f"{p.name}: empty file")
    return rows


# ---------------------------------------------------------------------------
# cash: FII/FPI and DII daily flows
# ---------------------------------------------------------------------------
def _party(h: str):
    t = set(h.split())
    if t & {"fii", "fpi", "fiis", "fpis", "foreign"} or h.startswith(("fii", "fpi")):
        return "fii"
    if t & {"dii", "diis", "domestic"} or h.startswith("dii"):
        return "dii"
    return None


def _field(h: str):
    # 'fii_net' / 'Net Purchase / Sales' / 'BUY VALUE(₹ Crores)' / 'Gross Sales'; also 'fiiBuy'-style keys
    t = {re.sub(r"^(fii|fpi|dii)", "", w) for w in h.split()}
    if "net" in t:
        return "net"
    if t & {"buy", "purchase", "purchases", "bought"}:
        return "buy"
    if t & {"sell", "sale", "sales", "sold"}:
        return "sell"
    return None


def _merge_header(r1: list, r2: list) -> list:
    """Two-row header (Moneycontrol style: 'FII Rs Crores' spanning Gross Purchase/Gross Sales/Net)."""
    out, cur = [], ""
    for i in range(max(len(r1), len(r2))):
        a = r1[i].strip() if i < len(r1) else ""
        b = r2[i].strip() if i < len(r2) else ""
        if a:
            cur = a
        out.append(f"{cur} {b}".strip() if b else (a or cur))
    return out


def load_cash(path, order=None) -> tuple:
    """Return (rows, notes). Each row: date, fii_buy, fii_sell, fii_net, dii_buy, dii_sell, dii_net."""
    raw = read_rows(path)
    name = Path(path).name
    hi = next((i for i, r in enumerate(raw[:10]) if any("date" in norm(c) for c in r)), None)
    if hi is None:
        raise FlowError(f"{name}: no header row with a date column in the first 10 lines")
    header = raw[hi]
    hn = [norm(c) for c in header]
    if (any(_party(h) for h in hn) and not any(_party(h) and _field(h) for h in hn)
            and hi + 1 < len(raw) and sum(1 for c in raw[hi + 1] if _field(norm(c))) >= 2):
        header = _merge_header(header, raw[hi + 1])
        hn = [norm(c) for c in header]
        hi += 1
    body = raw[hi + 1:]
    dcol = next(i for i, h in enumerate(hn) if "date" in h)
    ccol = next((i for i, h in enumerate(hn) if h in ("category", "client type", "participant", "investor",
                                                         "investor type", "category of investor")), None)
    cols = {}
    for i, h in enumerate(hn):
        if i in (dcol, ccol):
            continue
        party, field = _party(h), _field(h)
        if field and (party or ccol is not None):
            cols.setdefault((party, field), i)
    if ccol is None and not any(p == "fii" for p, _ in cols):
        raise FlowError(f"{name}: no FII/FPI columns found; expected e.g. date,fii_buy,fii_sell,fii_net,"
                        f"dii_buy,dii_sell,dii_net (header was {[c.strip() for c in header if c.strip()]})")
    if ccol is not None and not any(f in ("net", "buy") for _, f in cols):
        raise FlowError(f"{name}: category layout needs Buy/Sell/Net value columns")
    notes = []

    def cell(r, i):
        return r[i] if i is not None and i < len(r) else ""

    dates = parse_dates([cell(r, dcol) for r in body], order, f"{name} date column")
    per_date: dict = {}
    skipped = 0
    for r, d in zip(body, dates):
        if d is None:
            skipped += 1
            continue
        where = f"{name} {dd(d)}"
        if ccol is not None:
            party = _party(norm(cell(r, ccol)))
            if party is None:
                skipped += 1
                continue
            vals = {f: to_num(cell(r, cols.get((None, f))), where) for f in ("buy", "sell", "net")}
            rec = per_date.setdefault(d, {})
            if party in rec:
                raise FlowError(f"{name}: {party.upper()} appears twice for {dd(d)}")
            rec[party] = vals
        else:
            rec = {}
            for party in ("fii", "dii"):
                vals = {f: to_num(cell(r, cols.get((party, f))), where) for f in ("buy", "sell", "net")}
                if any(v is not None for v in vals.values()):
                    rec[party] = vals
            if not rec:
                skipped += 1
                continue
            if d in per_date:
                if per_date[d] != rec:
                    raise FlowError(f"{name}: {dd(d)} appears twice with different values (mixing NSE-only "
                                    "and combined NSE+BSE+MSEI data?)")
                continue
            per_date[d] = rec
    if skipped:
        notes.append(f"{name}: skipped {skipped} row(s) without a session date or FII/DII category "
                     "(totals, blank or unrelated rows)")

    rows, mismatch = [], []
    for d in sorted(per_date):
        rec = {"date": d}
        for party in ("fii", "dii"):
            v = per_date[d].get(party, {})
            b, s, n = v.get("buy"), v.get("sell"), v.get("net")
            if n is None and b is not None and s is not None:
                n = round(b - s, 2)
            elif n is not None and b is not None and s is not None and abs(b - s - n) > max(1.0, 0.001 * (b + s)):
                mismatch.append(f"{party.upper()} {dd(d)}")
            rec.update({f"{party}_buy": b, f"{party}_sell": s, f"{party}_net": n})
        rows.append(rec)
    if mismatch:
        notes.append(f"buy − sell ≠ net for {', '.join(mismatch[:4])}{' …' if len(mismatch) > 4 else ''}; "
                     "using the file's net figure")
    missing_fii = [r for r in rows if r["fii_net"] is None]
    if missing_fii:
        notes.append(f"dropped {len(missing_fii)} session(s) with no FII net figure "
                     f"(first {dd(missing_fii[0]['date'])})")
        rows = [r for r in rows if r["fii_net"] is not None]
    has_dii = any(r["dii_net"] is not None for r in rows)
    if has_dii:
        gaps = [r for r in rows if r["dii_net"] is None]
        if gaps:
            notes.append(f"dropped {len(gaps)} session(s) with FII but no DII figure (first {dd(gaps[0]['date'])})")
            rows = [r for r in rows if r["dii_net"] is not None]
    else:
        notes.append("no DII columns: FII/FPI-only analysis (NSDL FPI data has no DII figures)")
    if not rows:
        raise FlowError(f"{name}: no usable sessions (dates must look like 2026-10-30 or 30-Oct-2026)")
    return rows, has_dii, notes


def load_closes(path, order=None) -> list:
    """NIFTY (or any index) closes: [(date, close)] from Yahoo, NSE or niftyindices.com CSV exports."""
    raw = read_rows(path)
    name = Path(path).name
    hi = next((i for i, r in enumerate(raw[:10]) if any("date" in norm(c) for c in r)), None)
    if hi is None:
        raise FlowError(f"{name}: no header row with a date column")
    hn = [norm(c) for c in raw[hi]]
    dcol = next(i for i, h in enumerate(hn) if "date" in h)
    ccol = next((i for i, h in enumerate(hn) if h in ("close", "closing index value", "close price",
                                                       "closing price", "ltp", "price")), None)
    if ccol is None:
        raise FlowError(f"{name}: no Close column (header was {[c.strip() for c in raw[hi]]})")
    body = raw[hi + 1:]
    dates = parse_dates([r[dcol] if dcol < len(r) else "" for r in body], order, f"{name} date column")
    out = {}
    for r, d in zip(body, dates):
        if d is None or ccol >= len(r):
            continue
        try:
            c = to_num(r[ccol], name)
        except FlowError:
            continue                      # Yahoo writes 'null' on holidays
        if c and c > 0:
            out[d] = c
    if len(out) < 2:
        raise FlowError(f"{name}: fewer than two usable closes")
    return sorted(out.items())


def pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / math.sqrt(sxx * syy)


def current_streak(rows, key) -> dict:
    last = rows[-1][key]
    if not last:
        return {"direction": "flat", "sessions": 0, "total": 0.0, "since": rows[-1]["date"]}
    up = last > 0
    n, total, since = 0, 0.0, rows[-1]["date"]
    for r in reversed(rows):
        v = r[key]
        if v is None or v == 0 or (v > 0) != up:
            break
        n, total, since = n + 1, total + v, r["date"]
    return {"direction": "buying" if up else "selling", "sessions": n, "total": total, "since": since}


def record_streaks(rows, key) -> dict:
    best = {"buying": None, "selling": None}
    run = None
    for r in rows + [{"date": None, key: 0}]:
        v = r[key]
        side = "buying" if v and v > 0 else "selling" if v and v < 0 else None
        if run and side == run["direction"]:
            run["sessions"] += 1
            run["total"] += v
            run["to"] = r["date"]
            continue
        if run:
            b = best[run["direction"]]
            if b is None or run["sessions"] >= b["sessions"]:
                best[run["direction"]] = dict(run)       # ties go to the most recent run
        run = {"direction": side, "sessions": 1, "total": v, "from": r["date"], "to": r["date"]} if side else None
    return best


def absorption(sel, has_dii) -> dict | None:
    """How much FII selling DIIs soaked up. net_pct: DII net / FII net outflow over the window.
    same_day_pct: share of each FII-selling day's outflow matched by DII buying that same day."""
    if not has_dii:
        return None
    fii = sum(r["fii_net"] for r in sel)
    dii = sum(r["dii_net"] for r in sel)
    sell_days = [r for r in sel if r["fii_net"] < 0]
    sold = -sum(r["fii_net"] for r in sell_days)
    matched = sum(min(max(r["dii_net"], 0.0), -r["fii_net"]) for r in sell_days)
    return {"net_pct": dii / -fii * 100 if fii < 0 else None,
            "same_day_pct": matched / sold * 100 if sold > 0 else None,
            "fii_selling_days": len(sell_days),
            "days_dii_bought_more": sum(1 for r in sell_days if r["dii_net"] >= -r["fii_net"])}


def window_stats(sel, label, size, has_dii) -> dict:
    fii = sum(r["fii_net"] for r in sel)
    dii = sum(r["dii_net"] for r in sel) if has_dii else None
    return {"window": label, "sessions": len(sel), "complete": size is None or len(sel) >= size,
            "from": sel[0]["date"] if sel else None, "to": sel[-1]["date"] if sel else None,
            "fii_net": fii, "dii_net": dii, "combined_net": fii + dii if has_dii else None,
            "absorption": absorption(sel, has_dii) if sel else None}


def nifty_link(rows, closes, has_dii, min_days) -> dict:
    idx = {d: i for i, (d, _) in enumerate(closes)}
    same, nxt, unmatched = [], [], 0
    for r in rows:
        i = idx.get(r["date"])
        if i is None or i == 0:
            unmatched += 1
            continue
        same.append((r, (closes[i][1] / closes[i - 1][1] - 1) * 100))
        if i + 1 < len(closes):
            nxt.append((r, (closes[i + 1][1] / closes[i][1] - 1) * 100))
    res = {"matched_sessions": len(same), "unmatched_flow_dates": unmatched,
           "from": same[0][0]["date"] if same else None, "to": same[-1][0]["date"] if same else None,
           "corr_fii_same_day": None, "corr_dii_same_day": None, "corr_combined_same_day": None,
           "corr_fii_next_day": None, "avg_nifty_ret_fii_selling_days": None,
           "avg_nifty_ret_fii_buying_days": None, "note": None}
    if len(same) < min_days:
        res["note"] = f"only {len(same)} sessions match the NIFTY file (need {min_days}) — correlation skipped"
        return res
    rets = [x for _, x in same]
    res["corr_fii_same_day"] = pearson([r["fii_net"] for r, _ in same], rets)
    if has_dii:
        res["corr_dii_same_day"] = pearson([r["dii_net"] for r, _ in same], rets)
        res["corr_combined_same_day"] = pearson([r["fii_net"] + r["dii_net"] for r, _ in same], rets)
    if len(nxt) >= min_days:
        res["corr_fii_next_day"] = pearson([r["fii_net"] for r, _ in nxt], [x for _, x in nxt])
    sell = [x for r, x in same if r["fii_net"] < 0]
    buy = [x for r, x in same if r["fii_net"] > 0]
    res["avg_nifty_ret_fii_selling_days"] = statistics.fmean(sell) if sell else None
    res["avg_nifty_ret_fii_buying_days"] = statistics.fmean(buy) if buy else None
    return res


def analyse_cash(rows, has_dii, closes=None, lookback=60, top=5, min_corr_days=10) -> dict:
    last = rows[-1]
    windows = [window_stats(rows[-k:], f"{k} sessions", k, has_dii) for k in (5, 10, 20)]
    mtd = [r for r in rows if (r["date"].year, r["date"].month) == (last["date"].year, last["date"].month)]
    windows.append(window_stats(mtd, f"MTD {last['date'].strftime('%b-%Y')}", None, has_dii))
    first_of_month = last["date"].replace(day=1)
    prev_end = first_of_month - timedelta(days=1)
    prev = [r for r in rows if (r["date"].year, r["date"].month) == (prev_end.year, prev_end.month)]
    if prev:
        w = window_stats(prev, prev_end.strftime("%b-%Y"), None, has_dii)
        w["complete"] = rows[0]["date"] <= prev_end.replace(day=1) + timedelta(days=3)
        windows.append(w)
    recent = rows[-lookback:]
    tail20 = rows[-20:]
    avg_abs = statistics.fmean(abs(r["fii_net"]) for r in tail20)
    streaks = {"fii": current_streak(rows, "fii_net")}
    records = {"fii": record_streaks(recent, "fii_net")}
    if has_dii:
        streaks["dii"] = current_streak(rows, "dii_net")
        records["dii"] = record_streaks(recent, "dii_net")

    def largest(key, sign):
        cand = [r for r in recent if r[key] is not None and r[key] * sign > 0]
        cand.sort(key=lambda r: (-abs(r[key]), r["date"].toordinal() * -1))
        return [{"date": r["date"], "net": r[key]} for r in cand[:top]]

    big = {"fii_selling": largest("fii_net", -1), "fii_buying": largest("fii_net", 1)}
    if has_dii:
        big.update({"dii_buying": largest("dii_net", 1), "dii_selling": largest("dii_net", -1)})
    res = {
        "command": "cash", "as_of": last["date"], "dates_tz": "IST (NSE session dates)", "units": "₹ crore",
        "sessions_loaded": len(rows), "first_date": rows[0]["date"], "has_dii": has_dii,
        "latest": {**last, "combined_net": last["fii_net"] + last["dii_net"] if has_dii else None,
                   "fii_size_vs_20d_avg_abs": abs(last["fii_net"]) / avg_abs if avg_abs else None,
                   "fii_20d_avg_abs": avg_abs},
        "windows": windows, "streaks": streaks, "record_streaks": records, "lookback_sessions": len(recent),
        "largest_days": big, "nifty": nifty_link(rows, closes, has_dii, min_corr_days) if closes else None,
    }
    res["read"] = cash_read(res)
    return res


def cash_read(res) -> list:
    out = []
    lat, has_dii = res["latest"], res["has_dii"]
    w20 = next(w for w in res["windows"] if w["window"] == "20 sessions")
    fs = res["streaks"]["fii"]
    if fs["sessions"] >= 3:
        rec = res["record_streaks"]["fii"][fs["direction"]]
        longest = rec and rec["sessions"] == fs["sessions"] and rec["to"] == res["as_of"]
        out.append(f"FIIs have been net {fs['direction']} for {fs['sessions']} sessions ({cr(fs['total'])} since "
                   f"{dd(fs['since'])})" + (f" — the longest {fs['direction']} run in the last "
                                             f"{res['lookback_sessions']} sessions." if longest else "."))
    if has_dii and w20["fii_net"] < 0 and w20["absorption"]["net_pct"] is not None:
        a = w20["absorption"]["net_pct"]
        if a >= 100:
            out.append(f"DIIs more than absorbed FII selling over {w20['sessions']} sessions ({a:.0f}%): the "
                       "SIP-funded domestic bid; FII outflows alone have not been a crash signal while this holds.")
        elif a >= 50:
            out.append(f"DIIs absorbed {a:.0f}% of FII selling over {w20['sessions']} sessions — partial support; "
                       "the uncovered part shows up as pressure on large caps.")
        else:
            out.append(f"DIIs absorbed only {a:.0f}% of FII selling over {w20['sessions']} sessions — thin "
                       "domestic support; expect selling to show in prices.")
    elif w20["fii_net"] > 0:
        out.append(f"FIIs are net buyers over {w20['sessions']} sessions ({cr(w20['fii_net'])}); foreign buying "
                   "tends to lift index heavyweights (banks, IT, large private names) first.")
    ratio = lat["fii_size_vs_20d_avg_abs"]
    if ratio is not None and ratio >= 2:
        out.append(f"Latest FII flow ({cr(lat['fii_net'])}) is {ratio:.1f}× the 20-session average size — check "
                   "for block deals, MSCI/FTSE rebalancing or a large IPO/QIP allotment on that date.")
    if has_dii and lat["fii_net"] < 0 and lat["dii_net"] < 0:
        out.append("FIIs and DIIs were both net sellers in the latest session — uncommon; usually broad risk-off.")
    n = res.get("nifty")
    if n and n["corr_fii_same_day"] is not None:
        c = n["corr_fii_same_day"]
        out.append(f"Same-day correlation of FII net with NIFTY return is {c:+.2f} over {n['matched_sessions']} "
                   "sessions; flows and prices move together, which says little about tomorrow"
                   + (f" (next-day correlation {n['corr_fii_next_day']:+.2f})." if n["corr_fii_next_day"] is not None
                      else "."))
    return out


def print_cash(res, notes) -> None:
    lat = res["latest"]
    print(f"FII/DII cash flows — {dd(res['as_of'], True)} (IST session) | ₹ crore | {res['sessions_loaded']} sessions "
          f"{dd(res['first_date'])} → {dd(res['as_of'])}")
    print("Provisional exchange data unless your file says otherwise; NSDL's final FPI figures can differ.")
    print(f"\nLatest  {'Buy':>12} {'Sell':>12} {'Net':>12}")
    for p, lab in (("fii", "FII/FPI"), ("dii", "DII")):
        if p == "dii" and not res["has_dii"]:
            continue
        print(f"{lab:<7} {indian(lat[p + '_buy']):>12} {indian(lat[p + '_sell']):>12} {indian(lat[p + '_net'], sign=True):>12}")
    if res["has_dii"]:
        print(f"{'Combined':<33} {indian(lat['combined_net'], sign=True):>12}")
    if lat["fii_size_vs_20d_avg_abs"] is not None:
        print(f"FII size vs 20-session average |flow| ({indian(lat['fii_20d_avg_abs'])}): "
              f"{lat['fii_size_vs_20d_avg_abs']:.1f}×")
    print(f"\n{'Window':<16} {'Sessions':>8} {'FII net':>12} {'DII net':>12} {'Combined':>12} {'DII absorbed':>13} "
          f"{'same-day':>9}")
    for w in res["windows"]:
        a = w["absorption"] or {}
        sess = f"{w['sessions']}{'' if w['complete'] else '*'}"
        print(f"{w['window']:<16} {sess:>8} {indian(w['fii_net'], sign=True):>12} "
              f"{indian(w['dii_net'], sign=True):>12} {indian(w['combined_net'], sign=True):>12} "
              f"{pct(a.get('net_pct'), 0):>13} {pct(a.get('same_day_pct'), 0):>9}")
    if any(not w["complete"] for w in res["windows"]):
        print("* fewer sessions than the window (file too short or month not fully covered)")
    print("DII absorbed = DII net ÷ FII net outflow over the window (— when FIIs were net buyers); "
          "same-day = FII selling matched by DII buying on the same day.")
    print("\nStreaks:")
    for p, lab in (("fii", "FII/FPI"), ("dii", "DII")):
        if p not in res["streaks"]:
            continue
        s = res["streaks"][p]
        rec = res["record_streaks"][p]
        recs = "; ".join(f"longest {k} {v['sessions']} ({dd(v['from'])} → {dd(v['to'])}, {cr(v['total'], 0)})"
                         for k, v in rec.items() if v)
        print(f"  {lab:<7} {s['direction']} {s['sessions']} session(s), {cr(s['total'])} since {dd(s['since'])} | "
              f"last {res['lookback_sessions']}: {recs or '—'}")
    print(f"\nLargest days (last {res['lookback_sessions']} sessions):")
    for k, lab in (("fii_selling", "FII selling"), ("fii_buying", "FII buying"), ("dii_buying", "DII buying"),
                   ("dii_selling", "DII selling")):
        if k in res["largest_days"]:
            items = ", ".join(f"{indian(x['net'], 0, sign=True)} ({x['date'].strftime('%d-%b')})"
                              for x in res["largest_days"][k])
            print(f"  {lab:<12} {items or '—'}")
    n = res["nifty"]
    if n:
        print(f"\nNIFTY link ({n['matched_sessions']} matched sessions):", end=" ")
        if n["note"]:
            print(n["note"])
        else:
            def c(x):
                return "—" if x is None else f"{x:+.2f}"
            print(f"corr FII/same-day {c(n['corr_fii_same_day'])}, DII/same-day {c(n['corr_dii_same_day'])}, "
                  f"combined {c(n['corr_combined_same_day'])}, FII/next-day {c(n['corr_fii_next_day'])}")
            print(f"  avg NIFTY return on FII selling days {pct(n['avg_nifty_ret_fii_selling_days'], 2, True)}, "
                  f"on FII buying days {pct(n['avg_nifty_ret_fii_buying_days'], 2, True)}")
        if n["unmatched_flow_dates"]:
            print(f"  {n['unmatched_flow_dates']} flow date(s) had no NIFTY close or prior close")
    if res["read"]:
        print("\nRead:")
        for line in res["read"]:
            print(f"  - {line}")
    for x in notes:
        print(f"⚠ {x}")
    print(DISCLAIMER)


# ---------------------------------------------------------------------------
# participants: NSE participant-wise F&O open interest
# ---------------------------------------------------------------------------
OI_COLS = {
    "future index long": "fut_idx_long", "future index short": "fut_idx_short",
    "future stock long": "fut_stk_long", "future stock short": "fut_stk_short",
    "option index call long": "idx_call_long", "option index put long": "idx_put_long",
    "option index call short": "idx_call_short", "option index put short": "idx_put_short",
    "option stock call long": "stk_call_long", "option stock put long": "stk_put_long",
    "option stock call short": "stk_call_short", "option stock put short": "stk_put_short",
    "total long contracts": "total_long", "total short contracts": "total_short",
}
PARTICIPANTS = {"client": "Client", "dii": "DII", "fii": "FII", "fpi": "FII", "fii fpi": "FII", "pro": "Pro",
                "total": "TOTAL"}
_TITLE_DATE = re.compile(r"as\s+on\s+([A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4}|\d{1,2}[- ][A-Za-z]{3,9}[- ,]+\d{4}|"
                         r"\d{4}-\d{2}-\d{2}|\d{1,2}-\d{1,2}-\d{4})", re.I)
_FILE_DATE = re.compile(r"(\d{2})(\d{2})(\d{4})")


def last_weekday(y, m, wd) -> date:
    d = date(y, m, calendar.monthrange(y, m)[1])
    while d.weekday() != wd:
        d -= timedelta(days=1)
    return d


def expiry_weekday(d: date) -> int:
    """NSE index derivatives expire on Tuesday since 1-Sep-2025 (Thursday before)."""
    return 1 if d >= date(2025, 9, 1) else 3


def monthly_expiry(d: date) -> date:
    """Last Tuesday (Thursday before Sep-2025) of d's month. Holiday shifts are not modelled."""
    return last_weekday(d.year, d.month, expiry_weekday(d))


def load_participant_file(path: Path) -> dict:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise FlowError(f"{path.name}: empty file")
    first = lines[0]
    if "volume" in first.lower() and "open interest" not in first.lower():
        raise FlowError(f"{path.name}: this is the participant-wise trading VOLUME file, not open interest "
                        "(download fao_participant_oi_DDMMYYYY.csv)")
    rows = list(csv.reader(lines))
    hi = next((i for i, r in enumerate(rows[:6]) if r and norm(r[0]) == "client type"), None)
    if hi is None:
        raise FlowError(f"{path.name}: no 'Client Type' header row — not an NSE participant-wise OI file")
    keys = [OI_COLS.get(norm(c)) for c in rows[hi]]
    if "fut_idx_long" not in keys or "fut_idx_short" not in keys:
        raise FlowError(f"{path.name}: missing Future Index Long/Short columns")
    data = {}
    for r in rows[hi + 1:]:
        if not r or not r[0].strip():
            continue
        who = PARTICIPANTS.get(norm(r[0]))
        if who is None:
            continue
        vals = {}
        for k, c in zip(keys[1:], r[1:]):
            if k:
                v = to_num(c, f"{path.name} {who} {k}")
                vals[k] = int(v) if v is not None else None
        data[who] = vals
    if "FII" not in data:
        raise FlowError(f"{path.name}: no FII row")
    title_date = None
    m = _TITLE_DATE.search(first) if hi > 0 else None
    if m:
        title_date = parse_dates([m.group(1).replace(".", "")], None, f"{path.name} title")[0]
    fm = _FILE_DATE.search(path.stem)
    file_date = None
    if fm:
        try:
            file_date = date(int(fm.group(3)), int(fm.group(2)), int(fm.group(1)))   # NSE names files DDMMYYYY
        except ValueError:
            file_date = None
    d = title_date or file_date
    if d is None:
        raise FlowError(f"{path.name}: cannot tell the date — no 'as on <date>' title line and the name is not "
                        "fao_participant_oi_DDMMYYYY.csv")
    note = None
    if title_date and file_date and title_date != file_date:
        note = f"{path.name}: title says {dd(title_date)} but file name says {dd(file_date)}; using the title"
    return {"date": d, "file": path.name, "data": data, "note": note}


def load_participants(paths: list) -> tuple:
    files, notes = [], []
    for p in paths:
        pp = Path(p)
        if pp.is_dir():
            found = sorted(x for x in pp.iterdir() if x.is_file() and x.suffix.lower() == ".csv")
            if not found:
                raise FlowError(f"{p}: no .csv files in the folder")
            for x in found:
                try:
                    files.append(load_participant_file(x))
                except FlowError as e:
                    notes.append(f"skipped {e}")
        elif pp.is_file():
            files.append(load_participant_file(pp))
        else:
            raise FlowError(f"{p}: file or folder not found")
    if not files:
        raise FlowError("no participant-wise OI files could be read" + (f" ({notes[0]})" if notes else ""))
    by_date = {}
    for f in files:
        if f["note"]:
            notes.append(f["note"])
        if f["date"] in by_date:
            if by_date[f["date"]]["data"] != f["data"]:
                raise FlowError(f"two different files for {dd(f['date'])}: {by_date[f['date']]['file']} and {f['file']}")
            continue
        by_date[f["date"]] = f
    return [by_date[d] for d in sorted(by_date)], notes


def _side(v, lk, sk):
    L, S = v.get(lk), v.get(sk)
    if L is None or S is None:
        return None
    t = L + S
    return {"long": L, "short": S, "net": L - S, "long_pct": L / t * 100 if t else None,
            "ls_ratio": L / S if S else None}


def _leg(v, lk, sk):
    L, S = v.get(lk), v.get(sk)
    if L is None or S is None:
        return None
    return {"long": L, "short": S, "net": L - S}


def positions(v: dict) -> dict:
    calls, puts = _leg(v, "idx_call_long", "idx_call_short"), _leg(v, "idx_put_long", "idx_put_short")
    return {"index_futures": _side(v, "fut_idx_long", "fut_idx_short"),
            "stock_futures": _side(v, "fut_stk_long", "fut_stk_short"),
            "index_calls": calls, "index_puts": puts,
            "index_options_bias": calls["net"] - puts["net"] if calls and puts else None,
            "stock_calls": _leg(v, "stk_call_long", "stk_call_short"),
            "stock_puts": _leg(v, "stk_put_long", "stk_put_short"),
            "total": _leg(v, "total_long", "total_short")}


def activity(dl, ds) -> str:
    """Name the dominant leg of a day's change in long and short contracts."""
    if not dl and not ds:
        return "unchanged"
    lab_l = "long buildup" if dl > 0 else "long unwinding" if dl < 0 else None
    lab_s = "short buildup" if ds > 0 else "short covering" if ds < 0 else None
    major, minor = (lab_l, lab_s) if abs(dl) >= abs(ds) else (lab_s, lab_l)
    if minor and min(abs(dl), abs(ds)) >= 0.5 * max(abs(dl), abs(ds)):
        return f"{major} + {minor}"             # both legs moved materially
    return major


def check_totals(f) -> list:
    data, out = f["data"], []
    tot = data.get("TOTAL")
    if not tot:
        return out
    for k in ("fut_idx_long", "fut_idx_short"):
        parts = [data[p].get(k) for p in ("Client", "DII", "FII", "Pro") if p in data]
        if tot.get(k) is not None and None not in parts and len(parts) == 4 and sum(parts) != tot[k]:
            out.append(f"{f['file']}: participant {k} sums to {sum(parts)}, TOTAL row says {tot[k]}")
    if tot.get("fut_idx_long") is not None and tot.get("fut_idx_long") != tot.get("fut_idx_short"):
        out.append(f"{f['file']}: TOTAL index futures long ≠ short — file may be truncated")
    return out


def analyse_participants(files, long_low=20.0, long_high=70.0, shift_pp=5.0, days=10) -> dict:
    hist = []
    prev = None
    notes = []
    for f in files:
        notes.extend(check_totals(f))
        pos = {who: positions(v) for who, v in f["data"].items() if who != "TOTAL"}
        d = f["date"]
        exp = monthly_expiry(d)
        tags = []
        if d == exp:
            tags.append("monthly expiry (approx.)")
        elif d.weekday() == expiry_weekday(d):
            tags.append("weekly options expiry")
        rec = {"date": d, "file": f["file"], "tags": tags, "participants": pos, "change": None}
        if prev:
            gap = (d - prev["date"]).days
            change = {}
            for who, p in pos.items():
                q = prev["participants"].get(who)
                if not q or not p["index_futures"] or not q["index_futures"]:
                    continue
                a, b = p["index_futures"], q["index_futures"]
                c = {"long": a["long"] - b["long"], "short": a["short"] - b["short"], "net": a["net"] - b["net"],
                     "long_pct_pp": (a["long_pct"] - b["long_pct"]) if a["long_pct"] is not None
                     and b["long_pct"] is not None else None}
                c["activity"] = activity(c["long"], c["short"])
                if p["index_calls"] and q["index_calls"]:
                    c["index_calls_net"] = p["index_calls"]["net"] - q["index_calls"]["net"]
                if p["index_puts"] and q["index_puts"]:
                    c["index_puts_net"] = p["index_puts"]["net"] - q["index_puts"]["net"]
                change[who] = c
            span = []
            pexp = monthly_expiry(prev["date"])
            if prev["date"] < exp <= d or prev["date"] < pexp <= d:
                span.append("spans a monthly expiry: expiring futures dropped out, read it as rollover")
            if gap > 4:
                span.append(f"compares with {dd(prev['date'])} ({gap} days earlier) — missing files?")
            rec["change"] = {"vs": prev["date"], "by_participant": change, "caveats": span}
        hist.append(rec)
        prev = rec
    latest = hist[-1]
    fii = latest["participants"]["FII"]
    fut = fii["index_futures"]
    flags = []
    if fut and fut["long_pct"] is not None:
        lp = fut["long_pct"]
        if lp <= long_low:
            flags.append(f"FII index futures only {lp:.1f}% long (≤ {long_low:g}%): extreme short positioning — fuel "
                         "for a short-covering rally on a positive trigger, but it has persisted for weeks before "
                         "(2025), so it is not a timing signal")
        elif lp >= long_high:
            flags.append(f"FII index futures {lp:.1f}% long (≥ {long_high:g}%): crowded long — vulnerable to long "
                         "unwinding on bad news")
    cl = latest["participants"].get("Client", {}).get("index_futures")
    if cl and cl["long_pct"] is not None:
        if cl["long_pct"] >= long_high:
            flags.append(f"Clients (mostly retail) {cl['long_pct']:.1f}% long index futures (≥ {long_high:g}%)"
                         + (" while FIIs are heavily short — the classic retail-vs-FII divergence (clients are "
                            "usually the other side of FII futures bets)" if fut and fut["long_pct"] is not None
                            and fut["long_pct"] <= 100 - long_high else ""))
        elif cl["long_pct"] <= long_low:
            flags.append(f"Clients only {cl['long_pct']:.1f}% long index futures — retail capitulation, a contrarian "
                         "positive")
    ch = (latest["change"] or {}).get("by_participant", {}).get("FII")
    if ch and ch["long_pct_pp"] is not None and abs(ch["long_pct_pp"]) >= shift_pp:
        flags.append(f"FII long % moved {ch['long_pct_pp']:+.1f} points in one session (≥ {shift_pp:g}) — "
                     f"a sharp {ch['activity']}")
    calls, puts = fii["index_calls"], fii["index_puts"]
    if calls and puts:
        if calls["net"] < 0 < puts["net"]:
            flags.append("FIIs are net short index calls and net long index puts — bearish or hedged options book")
        elif puts["net"] < 0 < calls["net"]:
            flags.append("FIIs are net long index calls and net short index puts — bullish options book")
    series = [h["participants"]["FII"]["index_futures"]["long_pct"] for h in hist
              if h["participants"].get("FII", {}).get("index_futures")
              and h["participants"]["FII"]["index_futures"]["long_pct"] is not None]
    pctile = None
    if len(series) >= 20 and fut and fut["long_pct"] is not None:
        pctile = sum(1 for x in series if x <= fut["long_pct"]) / len(series) * 100
        if pctile <= 10 or pctile >= 90:
            flags.append(f"FII long % is at the {pctile:.0f}th percentile of the {len(series)} loaded sessions")
    if latest["change"]:
        notes.extend(f"Δ vs {dd(latest['change']['vs'])}: {c}" for c in latest["change"]["caveats"])
    if latest["tags"]:
        notes.append(f"{dd(latest['date'])} is a {', '.join(latest['tags'])} day: options OI resets, so day-over-day "
                     "option changes are mostly expiry, not fresh bets")
    return {"command": "participants", "as_of": latest["date"], "dates_tz": "IST (NSE session dates)",
            "units": "contracts", "files_loaded": len(hist), "first_date": hist[0]["date"],
            "thresholds": {"long_low": long_low, "long_high": long_high, "shift_pp": shift_pp},
            "latest": latest, "fii_long_pct_percentile": pctile, "flags": flags,
            "history": [{"date": h["date"], "tags": h["tags"],
                         "fii_index_futures": h["participants"]["FII"]["index_futures"],
                         "client_index_futures": h["participants"].get("Client", {}).get("index_futures"),
                         "fii_index_calls_net": (h["participants"]["FII"]["index_calls"] or {}).get("net"),
                         "fii_index_puts_net": (h["participants"]["FII"]["index_puts"] or {}).get("net"),
                         "fii_change": ((h["change"] or {}).get("by_participant") or {}).get("FII"),
                         "change_caveats": (h["change"] or {}).get("caveats", [])}
                        for h in hist[-days:]],
            "notes": notes}


def print_participants(res, notes) -> None:
    lat = res["latest"]
    print(f"Participant-wise F&O open interest (NSE, contracts) — {dd(res['as_of'], True)} | {res['files_loaded']} "
          f"file(s) {dd(res['first_date'])} → {dd(res['as_of'])}" + (f" | {', '.join(lat['tags'])}" if lat['tags'] else ""))
    ch = (lat["change"] or {}).get("by_participant", {})
    vs = f" vs {lat['change']['vs'].strftime('%d-%b')}" if lat["change"] else ""
    print(f"\n{'Index futures':<13} {'Long':>11} {'Short':>11} {'Net':>12} {'Long%':>6} {'L/S':>5}  "
          f"{'ΔLong':>9} {'ΔShort':>9} {'ΔNet' + vs:>15}  Activity")
    for who in ("FII", "Client", "Pro", "DII"):
        p = lat["participants"].get(who)
        if not p or not p["index_futures"]:
            continue
        f, c = p["index_futures"], ch.get(who)
        ls = "—" if f["ls_ratio"] is None else f"{f['ls_ratio']:.2f}"
        delta = (f"{indian(c['long'], 0, True):>9} {indian(c['short'], 0, True):>9} {indian(c['net'], 0, True):>15}  "
                 f"{c['activity']}") if c else f"{'—':>9} {'—':>9} {'—':>15}"
        print(f"{who:<13} {indian(f['long'], 0):>11} {indian(f['short'], 0):>11} {indian(f['net'], 0, True):>12} "
              f"{pct(f['long_pct']):>6} {ls:>5}  {delta}")
    fii = lat["participants"]["FII"]
    if fii["index_calls"] and fii["index_puts"]:
        c, p = fii["index_calls"], fii["index_puts"]
        dc, dp = (ch.get("FII") or {}).get("index_calls_net"), (ch.get("FII") or {}).get("index_puts_net")
        print(f"\nFII index options: calls long {indian(c['long'], 0)} / short {indian(c['short'], 0)} = net "
              f"{indian(c['net'], 0, True)}" + (f" (Δ {indian(dc, 0, True)})" if dc is not None else "")
              + f" | puts long {indian(p['long'], 0)} / short {indian(p['short'], 0)} = net {indian(p['net'], 0, True)}"
              + (f" (Δ {indian(dp, 0, True)})" if dp is not None else ""))
        print(f"  calls net − puts net = {indian(fii['index_options_bias'], 0, True)} (positive = bullish tilt; "
              "contract counts, not delta-adjusted)")
    if fii["stock_futures"]:
        s = fii["stock_futures"]
        print(f"FII stock futures: long {indian(s['long'], 0)} / short {indian(s['short'], 0)} = net "
              f"{indian(s['net'], 0, True)} ({pct(s['long_pct'])} long)")
    if len(res["history"]) > 1:
        print("\nFII index futures history:")
        print(f"  {'Date':<16} {'Long%':>6} {'Net':>12} {'ΔNet':>10}  {'Client long%':>12}  Tags")
        for h in res["history"]:
            f, c, cl = h["fii_index_futures"], h["fii_change"], h["client_index_futures"]
            print(f"  {dd(h['date'], True):<16} {pct(f['long_pct'] if f else None):>6} "
                  f"{indian(f['net'] if f else None, 0, True):>12} {indian(c['net'] if c else None, 0, True):>10}  "
                  f"{pct(cl['long_pct'] if cl else None):>12}  "
                  f"{', '.join(h['tags'] + (['Δ spans expiry/gap'] if h['change_caveats'] else []))}")
    if res["fii_long_pct_percentile"] is not None:
        print(f"FII long % percentile within loaded files: {res['fii_long_pct_percentile']:.0f}")
    print(f"\nFlags (long % thresholds {res['thresholds']['long_low']:g} / {res['thresholds']['long_high']:g}):")
    for fl in res["flags"] or ["none"]:
        print(f"  - {fl}")
    print("Long% = long ÷ (long + short): what Indian market commentary calls the FII 'long-short ratio' "
          "(0.19 = 19% long). L/S = long ÷ short.")
    for x in notes + res["notes"]:
        print(f"⚠ {x}")
    print(DISCLAIMER)


# ---------------------------------------------------------------------------
# shareholding: quarterly pattern
# ---------------------------------------------------------------------------
_MON = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
_MON.update({m.lower(): i for i, m in enumerate(calendar.month_name) if m})
_MON["sept"] = 9
SHP_FIELDS = ("promoter", "fii", "dii", "mf", "govt", "public", "pledged", "shareholders")


def parse_quarter(raw, where="quarter") -> tuple:
    """Return (year, month) of the quarter end. Accepts Sep-2026, Sep 2026, Sep-26, Q2FY27, Q2 FY2027,
    FY27Q2, Q2 2026-27, 2026-09-30, 30-Sep-2026. Rejects 'Q3 2026' (calendar or fiscal?)."""
    s = str(raw).strip().strip('"')
    t = s.upper().replace(" ", "").replace("'", "")
    m = (re.fullmatch(r"Q([1-4])[-_]?FY(\d{2}|\d{4})", t) or re.fullmatch(r"FY(\d{2}|\d{4})[-_]?Q([1-4])", t))
    if m:
        q, fy = (m.group(1), m.group(2)) if t.startswith("Q") else (m.group(2), m.group(1))
        fy = int(fy) + (2000 if len(fy) == 2 else 0)
        mon = {1: 6, 2: 9, 3: 12, 4: 3}[int(q)]
        return (fy if mon == 3 else fy - 1, mon)
    m = re.fullmatch(r"Q([1-4])[-_]?(\d{4})[-/](\d{2}|\d{4})", t)
    if m:
        start = int(m.group(2))
        mon = {1: 6, 2: 9, 3: 12, 4: 3}[int(m.group(1))]
        return (start + 1 if mon == 3 else start, mon)
    if re.fullmatch(r"Q[1-4][-_]?\d{4}|\d{4}[-_]?Q[1-4]|Q[1-4]", t):
        raise FlowError(f"{where}: {s!r} is ambiguous (calendar or Indian fiscal quarter?); write Q2FY27 or Sep-2026")
    m = re.fullmatch(r"([A-Za-z]{3,9})[-' ,/]*(\d{2}|\d{4})", s)
    if m and m.group(1).lower() in _MON:
        mon = _MON[m.group(1).lower()]
        yr = int(m.group(2)) + (2000 if len(m.group(2)) == 2 else 0)
    else:
        d = parse_dates([s], None, where)[0]
        if d is None:
            raise FlowError(f"{where}: cannot read quarter {s!r}; use Sep-2026, Q2FY27 or 2026-09-30")
        yr, mon = d.year, d.month
    if mon not in (3, 6, 9, 12):
        raise FlowError(f"{where}: {s!r} is not a quarter end (Mar/Jun/Sep/Dec)")
    return (yr, mon)


def qlabel(q) -> str:
    """(2026, 9) -> 'Sep-2026 (Q2FY27)'; Indian fiscal year runs April to March."""
    y, m = q
    fq = {6: 1, 9: 2, 12: 3, 3: 4}[m]
    fy = y if m == 3 else y + 1
    return f"{calendar.month_abbr[m]}-{y} (Q{fq}FY{fy % 100:02d})"


def _shp_field(h: str):
    if not h:
        return None
    if "pledge" in h or "encumb" in h:
        return "pledged"
    if "shareholder" in h or h.startswith("no of"):
        return "shareholders"
    if "promoter" in h:
        return "promoter"
    if h in ("mf", "mfs") or "mutual fund" in h:
        return "mf"
    t = set(h.split())
    if t & {"fii", "fiis", "fpi", "fpis"} or "foreign" in h:
        return "fii"
    if t & {"dii", "diis"} or "domestic institution" in h:
        return "dii"
    if "government" in h or t & {"govt", "gov"}:
        return "govt"
    if "public" in h or "retail" in h or "non institution" in h:
        return "public"
    return None


def load_shareholding(path, symbol_arg=None) -> tuple:
    raw = read_rows(path)
    name = Path(path).name
    header = raw[0]
    quarter_cells = []
    for c in header[1:]:
        try:
            quarter_cells.append(parse_quarter(c) if c.strip() else None)
        except FlowError:
            quarter_cells.append(None)
    data: dict = {}
    notes = []
    if sum(1 for q in quarter_cells if q) >= 2:            # transposed: rows = categories, columns = quarters
        sym = (symbol_arg.split(",")[0].strip().upper() if symbol_arg else Path(path).stem.upper())
        for r in raw[1:]:
            fld = _shp_field(norm(r[0].replace("+", "")))
            if not fld:
                continue
            for q, c in zip(quarter_cells, r[1:]):
                if q:
                    data.setdefault(sym, {}).setdefault(q, {})[fld] = to_num(c, f"{name} {r[0].strip()}")
        notes.append(f"{name}: read as one stock ({sym}) with quarters across the columns")
    else:
        hn = [norm(c) for c in header]
        scol = next((i for i, h in enumerate(hn) if h in ("symbol", "ticker", "stock", "scrip", "nse symbol",
                                                             "company", "name", "security", "security name")), None)
        qcol = next((i for i, h in enumerate(hn) if h in ("quarter", "period", "date", "qtr", "as on", "as of",
                                                             "quarter ended", "quarter end")), None)
        if qcol is None:
            raise FlowError(f"{name}: need a quarter column (header was {[c.strip() for c in header]})")
        fcols = {}
        for i, h in enumerate(hn):
            if i in (scol, qcol):
                continue
            f = _shp_field(h)
            if f and f not in fcols:
                fcols[f] = i
        if not {"promoter", "fii", "dii"} & set(fcols):
            raise FlowError(f"{name}: no promoter/FII/DII columns found (header was {[c.strip() for c in header]})")
        for n, r in enumerate(raw[1:], 2):
            if qcol >= len(r) or not r[qcol].strip():
                continue
            sym = r[scol].strip().upper() if scol is not None and scol < len(r) and r[scol].strip() else \
                (symbol_arg.split(",")[0].strip().upper() if symbol_arg else Path(path).stem.upper())
            q = parse_quarter(r[qcol], f"{name} line {n}")
            vals = {f: to_num(r[i] if i < len(r) else "", f"{name} line {n} {f}") for f, i in fcols.items()}
            prev = data.setdefault(sym, {}).get(q)
            if prev is not None and prev != vals:
                raise FlowError(f"{name}: {sym} has two different rows for {qlabel(q)}")
            data[sym][q] = vals
    if not data:
        raise FlowError(f"{name}: no shareholding rows")
    return data, notes


def _streak(seq, sign) -> int:
    n = 0
    for a, b in zip(reversed(seq[:-1]), reversed(seq[1:])):
        if a is None or b is None or (b - a) * sign <= 0:
            break
        n += 1
    return n


def analyse_stock(sym, quarters: dict, p) -> dict:
    qs = sorted(quarters)
    rows = []
    for q in qs:
        v = dict(quarters[q])
        fii, dii = v.get("fii"), v.get("dii")
        v["institutional"] = fii + dii if fii is not None and dii is not None else None
        rows.append({"quarter": q, "label": qlabel(q), **v})
    last = rows[-1]
    prev = rows[-2] if len(rows) > 1 else None
    yoy = next((r for r in rows if r["quarter"] == (last["quarter"][0] - 1, last["quarter"][1])), None)
    fields = ("promoter", "fii", "dii", "mf", "public", "pledged", "institutional", "govt")

    def delta(a, b, f):
        return a[f] - b[f] if b is not None and a.get(f) is not None and b.get(f) is not None else None

    qoq = {f: delta(last, prev, f) for f in fields}
    yoyd = {f: delta(last, yoy, f) for f in fields}
    inst_seq = [r["institutional"] for r in rows]
    prom_seq = [r.get("promoter") for r in rows]
    flags, red = [], []
    mc = p["min_change"]
    if qoq["institutional"] is not None and qoq["institutional"] >= mc:
        flags.append(f"rising institutional: FII+DII {qoq['institutional']:+.2f} pp QoQ"
                     + (f", up {_streak(inst_seq, 1)} quarters running" if _streak(inst_seq, 1) > 1 else ""))
    elif qoq["institutional"] is not None and qoq["institutional"] <= -mc:
        flags.append(f"institutional exit: FII+DII {qoq['institutional']:+.2f} pp QoQ")
    if qoq["fii"] is not None and qoq["dii"] is not None and qoq["fii"] <= -mc and qoq["dii"] >= mc:
        flags.append(f"FII→DII handover: FII {qoq['fii']:+.2f} pp, DII {qoq['dii']:+.2f} pp")
    else:
        if qoq["fii"] is not None and abs(qoq["fii"]) >= mc:
            flags.append(f"FII {'accumulation' if qoq['fii'] > 0 else 'selling'}: {qoq['fii']:+.2f} pp QoQ")
        if qoq["dii"] is not None and abs(qoq["dii"]) >= mc:
            flags.append(f"DII {'accumulation' if qoq['dii'] > 0 else 'selling'}: {qoq['dii']:+.2f} pp QoQ")
    if qoq["mf"] is not None and qoq["mf"] >= mc:
        flags.append(f"mutual funds adding: MF {qoq['mf']:+.2f} pp QoQ")
    prom_fall = qoq["promoter"] is not None and qoq["promoter"] <= -p["promoter_drop"]
    if prom_fall:
        n = _streak(prom_seq, -1)
        flags.append(f"falling promoter: {qoq['promoter']:+.2f} pp QoQ" + (f", down {n} quarters running" if n > 1 else "")
                     + " — check for dilution (QIP, ESOPs, merger) vs open-market sale/OFS")
    pl, dpl = last.get("pledged"), qoq["pledged"]
    if pl is not None:
        if pl >= p["pledge_severe"]:
            red.append(f"pledge severe: {pl:.2f}% of promoter shares pledged (≥ {p['pledge_severe']:g}%)")
        elif pl >= p["pledge_red"]:
            red.append(f"pledge red flag: {pl:.2f}% of promoter shares pledged (≥ {p['pledge_red']:g}%)")
        elif pl >= p["pledge_watch"]:
            flags.append(f"pledge watch: {pl:.2f}% of promoter shares pledged (≥ {p['pledge_watch']:g}%)")
    if dpl is not None and dpl >= 1:
        red.append(f"pledge rising: {dpl:+.2f} pp QoQ")
        if prom_fall:
            red.append("promoter stake falling while pledge rises — possible pledge invocation / margin-call selling")
    total = sum(last.get(f) or 0 for f in ("promoter", "fii", "dii", "govt", "public"))
    notes = []
    if total > 101:
        notes.append(f"{sym} {last['label']}: promoter+FII+DII+govt+public = {total:.1f}% (> 100): categories overlap? "
                     "MF is part of DII; don't add it to the total")
    return {"symbol": sym, "quarters": len(rows), "latest": last, "previous": prev, "year_ago": yoy,
            "qoq": qoq, "yoy": yoyd, "flags": flags, "red_flags": red, "history": rows, "notes": notes}


def analyse_shareholding(data, symbols=None, **p) -> dict:
    p = {**{k: DEFAULTS[k] for k in ("min_change", "promoter_drop", "pledge_watch", "pledge_red", "pledge_severe")},
         **p}
    if symbols:
        missing = [s for s in symbols if s not in data]
        if missing and len(missing) == len(symbols):
            raise FlowError(f"symbol(s) {', '.join(missing)} not in the file (it has {', '.join(sorted(data)[:10])})")
        data = {s: data[s] for s in symbols if s in data}
    absent = [s for s in symbols or [] if s not in data]
    stocks = [analyse_stock(s, q, p) for s, q in data.items()]
    stocks.sort(key=lambda s: -(s["qoq"]["institutional"] if s["qoq"]["institutional"] is not None else -1e9))
    latest_q = max(s["latest"]["quarter"] for s in stocks)
    return {"command": "shareholding", "as_of": qlabel(latest_q), "units": "% of equity (pledged: % of promoter holding)",
            "params": p, "stocks": stocks,
            "rising_institutional": [s["symbol"] for s in stocks if any(f.startswith("rising institutional")
                                                                        for f in s["flags"])],
            "falling_promoter": [s["symbol"] for s in stocks if any(f.startswith("falling promoter") for f in s["flags"])],
            "pledge_red_flags": [s["symbol"] for s in stocks if s["red_flags"]],
            "stale": [s["symbol"] for s in stocks if s["latest"]["quarter"] != latest_q],
            "notes": [n for s in stocks for n in s["notes"]]
            + ([f"not in the file: {', '.join(absent)}"] if absent else [])}


def _d(x):
    return "—" if x is None else f"{x:+.2f}"


def _v(x):
    return "—" if x is None else f"{x:.2f}"


def print_shareholding(res, notes) -> None:
    stocks = res["stocks"]
    print(f"Shareholding pattern — latest quarter {res['as_of']} | {len(stocks)} stock(s) | % of equity; "
          "pledged = % of promoter holding")
    if len(stocks) == 1:
        s = stocks[0]
        print(f"\n{s['symbol']}")
        print(f"  {'Quarter':<22} {'Promoter':>8} {'FII':>6} {'DII':>6} {'MF':>6} {'Public':>7} {'Pledged':>8} {'FII+DII':>8}")
        for r in s["history"]:
            print(f"  {r['label']:<22} {_v(r.get('promoter')):>8} {_v(r.get('fii')):>6} {_v(r.get('dii')):>6} "
                  f"{_v(r.get('mf')):>6} {_v(r.get('public')):>7} {_v(r.get('pledged')):>8} {_v(r['institutional']):>8}")
        q, y = s["qoq"], s["yoy"]
        print(f"  {'QoQ change (pp)':<22} {_d(q['promoter']):>8} {_d(q['fii']):>6} {_d(q['dii']):>6} {_d(q['mf']):>6} "
              f"{_d(q['public']):>7} {_d(q['pledged']):>8} {_d(q['institutional']):>8}")
        print(f"  {'YoY change (pp)':<22} {_d(y['promoter']):>8} {_d(y['fii']):>6} {_d(y['dii']):>6} {_d(y['mf']):>6} "
              f"{_d(y['public']):>7} {_d(y['pledged']):>8} {_d(y['institutional']):>8}")
        if s["latest"].get("shareholders") and s["previous"] and s["previous"].get("shareholders"):
            a, b = s["latest"]["shareholders"], s["previous"]["shareholders"]
            hint = (" — fewer holders while institutions add: stock moving from retail to institutions"
                    if a < b and (s["qoq"]["institutional"] or 0) > 0 else
                    " — more holders while institutions trim: retail is absorbing institutional supply"
                    if a > b and (s["qoq"]["institutional"] or 0) < 0 else "")
            print(f"  Shareholders {indian(a, 0)} ({(a / b - 1) * 100:+.1f}% QoQ){hint}")
    else:
        print(f"\n{'Symbol':<12} {'Quarter':<10} {'Prom':>6} {'ΔProm':>6} {'FII':>6} {'ΔFII':>6} {'DII':>6} {'ΔDII':>6} "
              f"{'ΔMF':>6} {'ΔInst':>6} {'Pledge':>7}  Flags")
        for s in stocks:
            L, q = s["latest"], s["qoq"]
            fl = "; ".join(s["red_flags"] + s["flags"]) or "—"
            print(f"{s['symbol'][:12]:<12} {L['label'][:8]:<10} {_v(L.get('promoter')):>6} {_d(q['promoter']):>6} "
                  f"{_v(L.get('fii')):>6} {_d(q['fii']):>6} {_v(L.get('dii')):>6} {_d(q['dii']):>6} {_d(q['mf']):>6} "
                  f"{_d(q['institutional']):>6} {_v(L.get('pledged')):>7}  {fl}")
    if len(stocks) == 1:
        s = stocks[0]
        print("\nFlags:")
        for f in s["red_flags"]:
            print(f"  RED  {f}")
        for f in s["flags"]:
            print(f"  -    {f}")
        if not s["flags"] and not s["red_flags"]:
            print("  none")
    else:
        print(f"\nRising institutional: {', '.join(res['rising_institutional']) or '—'}")
        print(f"Falling promoter:     {', '.join(res['falling_promoter']) or '—'}")
        print(f"Pledge red flags:     {', '.join(res['pledge_red_flags']) or '—'}")
    if res["stale"]:
        print(f"Not yet filed for {res['as_of']}: {', '.join(res['stale'])}")
    p = res["params"]
    print(f"Thresholds: FII/DII/MF ±{p['min_change']:g} pp, promoter −{p['promoter_drop']:g} pp, pledge "
          f"{p['pledge_watch']:g}/{p['pledge_red']:g}/{p['pledge_severe']:g}% of promoter holding.")
    for x in notes + res["notes"]:
        print(f"⚠ {x}")
    print(DISCLAIMER)


# ---------------------------------------------------------------------------
# deals: bulk and block deals
# ---------------------------------------------------------------------------
BUY_WORDS = {"BUY", "B", "P", "PURCHASE", "BOUGHT"}
SELL_WORDS = {"SELL", "S", "SALE", "SOLD"}


def load_deals(path, order=None) -> tuple:
    raw = read_rows(path)
    name = Path(path).name
    hi = next((i for i, r in enumerate(raw[:5]) if any("client" in norm(c) for c in r)), None)
    if hi is None:
        raise FlowError(f"{name}: no header row with a Client Name column")
    hn = [norm(c) for c in raw[hi]]

    def find(pred):
        return next((i for i, h in enumerate(hn) if pred(h)), None)

    cols = {
        "date": find(lambda h: "date" in h),
        "symbol": find(lambda h: h in ("symbol", "nse symbol", "scrip code", "security code", "ticker")),
        "name": find(lambda h: h in ("security name", "scrip name", "company", "company name", "security")),
        "client": find(lambda h: "client" in h),
        "side": find(lambda h: h in ("buy sell", "buy or sell", "deal type", "transaction type", "action", "side",
                                     "trade type", "type")),
        "qty": find(lambda h: "quantity" in h or h in ("qty", "shares")),
        "price": find(lambda h: "price" in h or h in ("watp", "rate", "avg price")),
        "kind": find(lambda h: h in ("kind", "deal", "deal category", "bulk block")),
    }
    missing = [k for k in ("date", "client", "side", "qty", "price") if cols[k] is None]
    if cols["symbol"] is None and cols["name"] is None:
        missing.append("symbol")
    if missing:
        raise FlowError(f"{name}: missing column(s) {', '.join(missing)} (header was {[c.strip() for c in raw[hi]]})")
    need = max(v for v in cols.values() if v is not None)
    body = [r for r in raw[hi + 1:] if len(r) > need]
    dates = parse_dates([r[cols["date"]] for r in body], order, f"{name} date column")
    stem = Path(path).stem.lower()
    default_kind = "block" if "block" in stem else "bulk" if "bulk" in stem else "deal"
    out, skipped = [], 0
    for n, (r, d) in enumerate(zip(body, dates), 1):
        if d is None:
            skipped += 1
            continue
        where = f"{name} data row {n}"
        side_raw = r[cols["side"]].strip().upper()
        side = "buy" if side_raw in BUY_WORDS else "sell" if side_raw in SELL_WORDS else None
        if side is None:
            raise FlowError(f"{where}: buy/sell value {r[cols['side']]!r} not understood")
        qty, price = to_num(r[cols["qty"]], where), to_num(r[cols["price"]], where)
        if qty is None or price is None or qty <= 0 or price <= 0:
            raise FlowError(f"{where}: quantity and price must be positive numbers")
        sym = r[cols["symbol"]].strip().upper() if cols["symbol"] is not None else r[cols["name"]].strip().upper()
        kind = r[cols["kind"]].strip().lower() if cols["kind"] is not None and r[cols["kind"]].strip() else default_kind
        out.append({"date": d, "symbol": sym, "security": r[cols["name"]].strip() if cols["name"] is not None else sym,
                    "client": re.sub(r"\s+", " ", r[cols["client"]]).strip().upper().rstrip(".,"),
                    "side": side, "qty": qty, "price": price, "value_cr": qty * price / 1e7, "kind": kind,
                    "source": name})
    notes = [f"{name}: skipped {skipped} row(s) without a date"] if skipped else []
    return out, notes


def _agg():
    return {"buy_qty": 0.0, "sell_qty": 0.0, "buy_value_cr": 0.0, "sell_value_cr": 0.0, "deals": 0}


def _add(a, d):
    a[d["side"] + "_qty"] += d["qty"]
    a[d["side"] + "_value_cr"] += d["value_cr"]
    a["deals"] += 1


def _finish(a):
    a["net_qty"] = a["buy_qty"] - a["sell_qty"]
    a["net_value_cr"] = a["buy_value_cr"] - a["sell_value_cr"]
    a["avg_buy_price"] = a["buy_value_cr"] * 1e7 / a["buy_qty"] if a["buy_qty"] else None
    a["avg_sell_price"] = a["sell_value_cr"] * 1e7 / a["sell_qty"] if a["sell_qty"] else None
    return a


def analyse_deals(deals, top=10) -> dict:
    deals = sorted(deals, key=lambda d: (d["date"], d["symbol"], d["client"]))
    by_sym, by_client, pair = {}, {}, {}
    for d in deals:
        s = by_sym.setdefault(d["symbol"], {"symbol": d["symbol"], "security": d["security"], **_agg(),
                                            "clients": set(), "kinds": set(), "first": d["date"], "last": d["date"]})
        _add(s, d)
        s["clients"].add(d["client"])
        s["kinds"].add(d["kind"])
        s["last"] = d["date"]
        c = by_client.setdefault(d["client"], {"client": d["client"], **_agg(), "symbols": set()})
        _add(c, d)
        c["symbols"].add(d["symbol"])
        k = (d["client"], d["symbol"])
        pr = pair.setdefault(k, {"client": d["client"], "symbol": d["symbol"], **_agg(),
                                 "buy_dates": set(), "sell_dates": set()})
        _add(pr, d)
        pr[d["side"] + "_dates"].add(d["date"])
    symbols = [_finish(s) | {"clients": len(s["clients"]), "kinds": sorted(s["kinds"])} for s in by_sym.values()]
    symbols.sort(key=lambda s: -s["net_value_cr"])
    clients = [_finish(c) | {"symbols": sorted(c["symbols"])} for c in by_client.values()]
    clients.sort(key=lambda c: -abs(c["net_value_cr"]))
    pairs = [_finish(p) for p in pair.values()]
    repeat_buyers, repeat_sellers, round_trips = [], [], []
    for p in pairs:
        both = p["buy_dates"] & p["sell_dates"]
        if both:
            round_trips.append({"client": p["client"], "symbol": p["symbol"], "dates": sorted(both),
                                "net_qty": p["net_qty"], "net_value_cr": p["net_value_cr"]})
        if len(p["buy_dates"]) >= 2 and p["net_qty"] > 0:
            repeat_buyers.append({"client": p["client"], "symbol": p["symbol"], "dates": sorted(p["buy_dates"]),
                                  "net_qty": p["net_qty"], "net_value_cr": p["net_value_cr"],
                                  "avg_buy_price": p["avg_buy_price"]})
        if len(p["sell_dates"]) >= 2 and p["net_qty"] < 0:
            repeat_sellers.append({"client": p["client"], "symbol": p["symbol"], "dates": sorted(p["sell_dates"]),
                                   "net_qty": p["net_qty"], "net_value_cr": p["net_value_cr"],
                                   "avg_sell_price": p["avg_sell_price"]})
    repeat_buyers.sort(key=lambda r: -r["net_value_cr"])
    repeat_sellers.sort(key=lambda r: r["net_value_cr"])
    for p in pairs:
        p.pop("buy_dates")
        p.pop("sell_dates")
    return {"command": "deals", "as_of": deals[-1]["date"], "from": deals[0]["date"], "dates_tz": "IST",
            "units": "quantity in shares, value in ₹ crore", "deals": len(deals),
            "by_symbol": symbols, "by_client": clients[:top * 3], "client_symbol": pairs,
            "repeat_buyers": repeat_buyers, "repeat_sellers": repeat_sellers, "same_day_round_trips": round_trips,
            "top": top}


def print_deals(res, notes) -> None:
    print(f"Bulk/block deals — {res['deals']} deal(s) {dd(res['from'])} → {dd(res['as_of'])} | qty in shares, value ₹ crore")
    top = res["top"]
    print(f"\n{'Symbol':<14} {'Deals':>5} {'Clients':>7} {'Buy qty':>12} {'Sell qty':>12} {'Net qty':>13} "
          f"{'Net ₹ cr':>10}  Kinds")
    syms = res["by_symbol"]
    shown = syms if len(syms) <= 2 * top else syms[:top] + syms[-top:]
    for s in shown:
        print(f"{s['symbol'][:14]:<14} {s['deals']:>5} {s['clients']:>7} {indian(s['buy_qty'], 0):>12} "
              f"{indian(s['sell_qty'], 0):>12} {indian(s['net_qty'], 0, True):>13} {indian(s['net_value_cr'], 2, True):>10}  "
              f"{'/'.join(s['kinds'])}")
    if len(shown) < len(syms):
        print(f"({len(syms) - len(shown)} more symbols in --json)")
    print(f"\nLargest clients by |net value|:")
    for c in res["by_client"][:top]:
        print(f"  {c['client'][:40]:<40} {indian(c['net_value_cr'], 2, True):>10} cr  net {indian(c['net_qty'], 0, True)} "
              f"in {', '.join(c['symbols'][:5])}")
    print("\nRepeat buyers (bought on 2+ days, net long):")
    for r in res["repeat_buyers"][:top] or [None]:
        print("  —" if r is None else f"  {r['client'][:40]:<40} {r['symbol']:<12} {len(r['dates'])} days, net "
              f"{indian(r['net_qty'], 0, True)} sh ({cr(r['net_value_cr'])}), avg buy {indian(r['avg_buy_price'])}")
    print("Repeat sellers (sold on 2+ days, net short):")
    for r in res["repeat_sellers"][:top] or [None]:
        print("  —" if r is None else f"  {r['client'][:40]:<40} {r['symbol']:<12} {len(r['dates'])} days, net "
              f"{indian(r['net_qty'], 0, True)} sh ({cr(r['net_value_cr'])}), avg sell {indian(r['avg_sell_price'])}")
    if res["same_day_round_trips"]:
        print("Same-day round trips (bought and sold the same day — usually market makers/arbitrage, not conviction):")
        for r in res["same_day_round_trips"][:top]:
            print(f"  {r['client'][:40]:<40} {r['symbol']:<12} {', '.join(x.strftime('%d-%b') for x in r['dates'])} "
                  f"net {indian(r['net_qty'], 0, True)} sh")
    for x in notes:
        print(f"⚠ {x}")
    print(DISCLAIMER)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="machine-readable output")
    common.add_argument("--date-format", choices=["dmy", "mdy"],
                        help="order for slash/dot dates such as 05/10/2026 when the file cannot prove it")
    p = argparse.ArgumentParser(description="FII/DII flows, F&O participant positioning, shareholding and deals (India)")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("cash", parents=[common], help="FII/FPI and DII daily cash-market flows")
    c.add_argument("--csv", required=True, help="FII/DII CSV (wide fii_buy..dii_net, NSE category rows, or "
                                                 "Moneycontrol two-row header)")
    c.add_argument("--nifty", help="NIFTY closes CSV for the flow/return correlation")
    c.add_argument("--as-of", help="ignore sessions after this date")
    c.add_argument("--lookback", type=int, default=DEFAULTS["lookback"],
                   help="sessions searched for largest days and record streaks (60)")
    c.add_argument("--top", type=int, default=DEFAULTS["top"], help="largest days listed per side (5)")

    q = sub.add_parser("participants", parents=[common], help="NSE participant-wise F&O open interest")
    q.add_argument("--oi", action="append", required=True,
                   help="fao_participant_oi_DDMMYYYY.csv or a folder of them (repeatable)")
    q.add_argument("--as-of", help="ignore files after this date")
    q.add_argument("--days", type=int, default=10, help="history rows shown (10)")
    q.add_argument("--long-low", type=float, default=DEFAULTS["long_low"], help="FII long %% extreme-short flag (20)")
    q.add_argument("--long-high", type=float, default=DEFAULTS["long_high"], help="FII long %% crowded-long flag (70)")
    q.add_argument("--shift-pp", type=float, default=DEFAULTS["shift_pp"],
                   help="one-day change in FII long %% worth flagging (5)")

    s = sub.add_parser("shareholding", parents=[common], help="quarterly shareholding-pattern changes")
    s.add_argument("--csv", required=True, help="symbol,quarter,promoter,fii,dii,mf,public,pledged (or a "
                                                 "Screener-style table with quarters across)")
    s.add_argument("--symbol", help="comma list of symbols to keep (or the name for a single-stock file)")
    s.add_argument("--min-change", type=float, default=DEFAULTS["min_change"],
                   help="QoQ pp change in FII/DII/MF worth flagging (1.0)")
    s.add_argument("--promoter-drop", type=float, default=DEFAULTS["promoter_drop"],
                   help="QoQ pp fall in promoter holding worth flagging (0.5)")
    s.add_argument("--pledge-watch", type=float, default=DEFAULTS["pledge_watch"])
    s.add_argument("--pledge-red", type=float, default=DEFAULTS["pledge_red"])
    s.add_argument("--pledge-severe", type=float, default=DEFAULTS["pledge_severe"])

    d = sub.add_parser("deals", parents=[common], help="bulk and block deals by symbol and client")
    d.add_argument("--csv", action="append", required=True, help="NSE/BSE bulk or block deals CSV (repeatable)")
    d.add_argument("--symbol", help="comma list of symbols to keep")
    d.add_argument("--client", help="keep clients whose name contains this text")
    d.add_argument("--kind", choices=["bulk", "block"], help="keep only bulk or block deals")
    d.add_argument("--from", dest="date_from", help="first date to keep")
    d.add_argument("--to", dest="date_to", help="last date to keep")
    d.add_argument("--top", type=int, default=10, help="rows per list (10)")

    a = p.parse_args(argv)
    try:
        return _run(a)
    except FlowError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except (OSError, UnicodeDecodeError, csv.Error) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


def _run(a) -> int:
    order = a.date_format
    if a.cmd == "cash":
        if a.lookback < 5 or a.top < 1:
            raise FlowError("--lookback must be ≥ 5 and --top ≥ 1")
        rows, has_dii, notes = load_cash(a.csv, order)
        if a.as_of:
            cut = parse_one(a.as_of, "--as-of", order)
            rows = [r for r in rows if r["date"] <= cut]
            if not rows:
                raise FlowError(f"no sessions on or before {dd(cut)}")
        closes = load_closes(a.nifty, order) if a.nifty else None
        res = analyse_cash(rows, has_dii, closes, a.lookback, a.top, DEFAULTS["min_corr_days"])
        res["notes"] = notes
        if a.json:
            emit_json(res)
        else:
            print_cash(res, notes)
        return 0
    if a.cmd == "participants":
        if not 0 < a.long_low < a.long_high < 100:
            raise FlowError("need 0 < --long-low < --long-high < 100")
        if a.days < 1 or a.shift_pp <= 0:
            raise FlowError("--days must be ≥ 1 and --shift-pp positive")
        files, notes = load_participants(a.oi)
        if a.as_of:
            cut = parse_one(a.as_of, "--as-of", order)
            files = [f for f in files if f["date"] <= cut]
            if not files:
                raise FlowError(f"no participant files on or before {dd(cut)}")
        res = analyse_participants(files, a.long_low, a.long_high, a.shift_pp, a.days)
        res["notes"] = notes + res["notes"]
        if a.json:
            emit_json(res)
        else:
            print_participants(res, [])
        return 0
    if a.cmd == "shareholding":
        for nm in ("min_change", "promoter_drop", "pledge_watch", "pledge_red", "pledge_severe"):
            if getattr(a, nm) <= 0:
                raise FlowError(f"--{nm.replace('_', '-')} must be positive")
        if not a.pledge_watch <= a.pledge_red <= a.pledge_severe:
            raise FlowError("need --pledge-watch ≤ --pledge-red ≤ --pledge-severe")
        data, notes = load_shareholding(a.csv, a.symbol)
        syms = [x.strip().upper() for x in a.symbol.split(",") if x.strip()] if a.symbol else None
        res = analyse_shareholding(data, syms, min_change=a.min_change, promoter_drop=a.promoter_drop,
                                   pledge_watch=a.pledge_watch, pledge_red=a.pledge_red,
                                   pledge_severe=a.pledge_severe)
        res["notes"] = notes + res["notes"]
        if a.json:
            emit_json(res)
        else:
            print_shareholding(res, [])
        return 0
    # deals
    if a.top < 1:
        raise FlowError("--top must be ≥ 1")
    deals, notes = [], []
    for path in a.csv:
        got, n = load_deals(path, order)
        deals += got
        notes += n
    if a.symbol:
        want = {x.strip().upper() for x in a.symbol.split(",") if x.strip()}
        deals = [x for x in deals if x["symbol"] in want]
    if a.client:
        deals = [x for x in deals if a.client.strip().upper() in x["client"]]
    if a.kind:
        deals = [x for x in deals if x["kind"] == a.kind]
    if a.date_from:
        lo = parse_one(a.date_from, "--from", order)
        deals = [x for x in deals if x["date"] >= lo]
    if a.date_to:
        hi = parse_one(a.date_to, "--to", order)
        deals = [x for x in deals if x["date"] <= hi]
    if not deals:
        raise FlowError("no deals left after filters")
    res = analyse_deals(deals, a.top)
    res["notes"] = notes
    if a.json:
        emit_json(res)
    else:
        print_deals(res, notes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
