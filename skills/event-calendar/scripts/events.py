#!/usr/bin/env python3
"""Offline calendar of the dates that move Indian markets.

Three kinds of event, always labelled so nobody mistakes an estimate for a fact:
  computed    exchange calendar (F&O expiries, holidays, long weekends) from
              ../../fno-options-analysis/scripts/options.py, SEBI LODR results deadlines,
              income-tax dates
  rule        recurring releases placed by their publishing rule (CPI on the 12th, PMI on the
              first working day, ...): "expected, confirm on the day"
  verified    dated rows in ../references/events-2026-27.csv (RBI MPC, FOMC, BLS, MSCI, MoSPI GDP),
              each with a source and a verified_on date; a blank verified_on is shown as UNVERIFIED
plus your own events (--events FILE): results, board meetings, ex-dates, AGMs, lock-in expiries.

Zero dependencies (Python 3.10+ stdlib). Date parsing comes from
../../results-season/scripts/results.py (numeric DD/MM vs MM/DD is refused, never guessed).

Sub-commands
  week       this week (Mon-Sun) or --next week          events.py week [--from 2026-11-16]
  month      one calendar month                           events.py month --month 2026-12
  range      any span                                     events.py range --from 2026-11-16 --to 2026-12-31
  next       next events of a category                    events.py next --category rbi --count 2
  window     events inside a holding window, yes/no risk  events.py window --from 2026-11-16 --to 2026-11-27 --symbols TCS
  ics        export an .ics calendar file                 events.py ics --out india.ics --from 2026-11-16 --to 2027-03-31
  staleness  is the reference file running out / old?     events.py staleness [--days 60 --max-age 120]

Common flags: --events FILE (repeatable), --category rbi,expiry,..., --min-impact low|medium|high,
--today YYYY-MM-DD (pretend date), --reference FILE, --json.
"""
from __future__ import annotations

import argparse
import calendar
import csv
import hashlib
import importlib.util
import re
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parents[2]
DEFAULT_REFERENCE = Path(__file__).resolve().parents[1] / "references" / "events-2026-27.csv"


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


nse = _sibling("fno-options-analysis", "options")     # HOLIDAYS, is_trading_day, expiries, EXPIRY_RULES
rs = _sibling("results-season", "results")           # parse_date, resolve_timing, exposure_sessions, seasons

IST = nse.IST
UTC = timezone.utc
IMPACTS = ("low", "medium", "high")
CLOSE = time(15, 30)

# category -> (default impact, one line on why traders care)
CATEGORIES = {
    "expiry": ("high", "Option premiums decay to zero and pinning/unwinding flows can whip the index into the close."),
    "expiry-week": ("low", "Roll-over, OI unwinding and higher expiry-day margins cluster in the last week of the series."),
    "holiday": ("low", "No trading or settlement; expiries move to the previous session and T+1 payouts slip a day."),
    "long-weekend": ("medium", "Three or more closed days: global news piles up into one gap, and option theta for the break is charged anyway."),
    "special-session": ("medium", "An unusual session (weekend/Budget/Muhurat): thin or event-driven liquidity, odd timings."),
    "tax": ("low", "Deadlines that change what a trade costs after tax (advance tax, ITR, FY-end harvesting)."),
    "results-season": ("medium", "Most companies report in this window; stock-specific gaps and IV build-ups are everywhere."),
    "cpi": ("medium", "Retail inflation steers RBI rate expectations; banks, NBFCs and rate-sensitives react next session."),
    "iip": ("low", "Factory output; usually a footnote for markets unless it surprises badly."),
    "wpi": ("low", "Wholesale inflation; input-cost signal for manufacturers' margins."),
    "gdp": ("medium", "Growth print released after the close; frames the next session and the RBI's growth view."),
    "pmi": ("low", "Early read on activity; moves cyclicals only on a big miss or beat."),
    "auto-sales": ("low", "Monthly dispatch numbers move auto stocks (high impact for those names, low for the index)."),
    "gst": ("low", "Tax collections as a consumption proxy; headline-driven, rarely a market mover."),
    "amfi": ("low", "Mutual-fund flows and SIP totals: the domestic bid that absorbs FII selling."),
    "rbi": ("high", "Repo rate and stance at 10:00 IST: banks, NBFCs, realty, autos and the rupee swing intraday."),
    "budget": ("high", "Tax, STT and spending changes hit whole sectors in one speech; IV is bid up before and crushed after."),
    "fomc": ("high", "US rate decision lands after Indian hours; GIFT Nifty, IT stocks, the rupee and FII flows react next session."),
    "us-cpi": ("medium", "US inflation moves US yields and the dollar, which feed FII flows and IT/pharma exporters."),
    "us-jobs": ("medium", "US payrolls shift Fed expectations; India reacts the next session via GIFT Nifty."),
    "index-rebalance": ("medium", "Passive funds trade at the close: heavy volume and odd closing prints in added/removed stocks."),
    "msci": ("medium", "Global passive money follows MSCI changes; India weight shifts can mean large FII buying or selling at one close."),
    "results": ("high", "Company results: overnight gap risk past any stop, and option IV collapses after the event."),
    "board-meeting": ("high", "Board outcome (results, fund-raise, buyback, dividend) can gap the stock."),
    "ex-date": ("medium", "Price adjusts for the dividend/bonus/split: stops and charts need adjusting."),
    "agm": ("low", "Management commentary and approvals; occasionally moves the stock."),
    "lock-in": ("high", "Pre-IPO or anchor shares become free to sell: supply overhang around the date."),
    "other": ("medium", "User-supplied event."),
}
COMPANY = {"results", "board-meeting", "ex-date", "agm", "lock-in"}
ALIASES = {
    "results": {"results", "results-season", "board-meeting"},
    "expiries": {"expiry", "expiry-week"}, "fno": {"expiry", "expiry-week"},
    "holidays": {"holiday", "long-weekend", "special-session"},
    "macro": {"cpi", "iip", "wpi", "gdp", "pmi", "auto-sales", "gst", "amfi"},
    "policy": {"rbi", "budget", "fomc"}, "fed": {"fomc"},
    "us": {"fomc", "us-cpi", "us-jobs"}, "index": {"index-rebalance", "msci"},
    "company": set(COMPANY), "india-cpi": {"cpi"},
}
# Categories that create gap risk for a held position (what trading-discipline's --event asks about).
RISK_CATEGORIES = {"rbi", "budget", "fomc", "results", "board-meeting", "lock-in"}
# One event per date for these, so duplicates across files collapse.
SINGLE = {"rbi", "budget", "fomc", "cpi", "iip", "wpi", "gdp", "us-cpi", "us-jobs", "gst", "amfi"}
# Release clock (IST) by category; US releases are converted from New York time.
IST_TIMES = {"rbi": time(10, 0), "budget": time(11, 0), "cpi": time(16, 0), "iip": time(16, 0),
             "gdp": time(16, 0), "wpi": time(12, 0), "pmi": time(10, 30)}
ET_TIMES = {"fomc": (14, 0), "us-cpi": (8, 30), "us-jobs": (8, 30)}
# Reference categories the staleness check watches, with who publishes the next batch.
TRACKED = {
    "rbi": "RBI publishes the next financial year's MPC schedule (usually in late March)",
    "fomc": "the Fed publishes the next year's FOMC calendar (usually in summer)",
    "us-cpi": "BLS publishes its release schedule on bls.gov/schedule",
    "us-jobs": "BLS publishes its release schedule on bls.gov/schedule",
    "msci": "MSCI announces the next eight review dates at each quarterly review",
    "gdp": "MoSPI's Advance Release Calendar on mospi.gov.in",
}
FIXED_NAMES = {(1, 26): "Republic Day", (5, 1): "Maharashtra Day", (8, 15): "Independence Day",
               (10, 2): "Mahatma Gandhi Jayanti", (12, 25): "Christmas"}
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September",
          "October", "November", "December")


class EventError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Small date helpers
# ---------------------------------------------------------------------------
def fmt(d: date) -> str:
    return f"{d:%a %d-%b-%Y}"


def parse_day(text, what: str) -> date:
    return rs.parse_date(text, what)[0]


def weekday_on_or_after(d: date) -> date:
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def last_weekday_of_month(y: int, m: int) -> date:
    d = date(y, m, calendar.monthrange(y, m)[1])
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def nth_working_day(y: int, m: int, n: int) -> date:
    d, k = date(y, m, 1), 0
    while True:
        if nse.is_trading_day(d):
            k += 1
            if k == n:
                return d
        d += timedelta(days=1)


def next_session(d: date) -> date:
    while not nse.is_trading_day(d):
        d += timedelta(days=1)
    return d


def months_between(a: date, b: date):
    y, m = a.year, a.month
    while (y, m) <= (b.year, b.month):
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def prev_month_name(y: int, m: int, back: int = 1) -> str:
    m0 = (m - 1 - back) % 12
    return f"{MONTHS[m0][:3]} {y - (1 if m - back < 1 else 0)}"


def fy_label(d: date) -> str:
    return f"FY{(d.year + 1 if d.month >= 4 else d.year) % 100:02d}"


def holiday_list_missing(d: date) -> bool:
    return d > nse.HOLIDAYS_KNOWN_UNTIL


def _nth_sunday(y: int, m: int, n: int) -> date:
    first = date(y, m, 1)
    return first + timedelta(days=(6 - first.weekday()) % 7 + 7 * (n - 1))


def et_to_ist(d: date, hh: int, mm: int) -> datetime:
    """New York wall-clock time to IST (US daylight time: 2nd Sunday of March to 1st Sunday of November)."""
    dst = _nth_sunday(d.year, 3, 2) <= d < _nth_sunday(d.year, 11, 1)
    tz = timezone(timedelta(hours=-4 if dst else -5))
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=tz).astimezone(IST)


def make(d: date, category: str, title: str, kind: str, impact: str | None = None, end: date | None = None,
         at: time | None = None, source: str = "", verified_on: date | None = None, note: str = "",
         symbol: str = "", uncertain: bool = False, **extra) -> dict:
    e = {"date": d, "end": end if end and end != d else None, "category": category, "title": title,
         "impact": impact or CATEGORIES[category][0], "kind": kind, "time_ist": at, "source": source,
         "verified_on": verified_on, "note": note, "symbol": symbol, "uncertain": uncertain}
    e.update(extra)
    return e


# ---------------------------------------------------------------------------
# Reference file (dated, verified events)
# ---------------------------------------------------------------------------
REF_COLS = ("date", "category", "title", "impact", "source", "verified_on")


def load_reference(path) -> list:
    path = Path(path)
    if not path.is_file():
        raise EventError(f"reference file {path} not found")
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        cols = [c.strip().lower() for c in (reader.fieldnames or [])]
        missing = [c for c in REF_COLS if c not in cols]
        if missing:
            raise EventError(f"{path.name}: missing column(s) {', '.join(missing)}; need {','.join(REF_COLS)}")
        rows = []
        for n, raw in enumerate(reader, start=2):
            r = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
            if not any(r.values()):
                continue
            where = f"{path.name} line {n}"
            cat = r["category"].lower()
            if cat not in CATEGORIES:
                raise EventError(f"{where}: unknown category {r['category']!r}")
            imp = (r["impact"] or CATEGORIES[cat][0]).lower()
            if imp not in IMPACTS:
                raise EventError(f"{where}: impact must be low, medium or high")
            vo = parse_day(r["verified_on"], f"{where} verified_on") if r["verified_on"] else None
            rows.append({"date": parse_day(r["date"], where), "category": cat, "title": r["title"],
                         "impact": imp, "source": r["source"], "verified_on": vo})
    return rows


def reference_events(rows: list) -> list:
    out = []
    for r in rows:
        d, at, title = r["date"], IST_TIMES.get(r["category"]), r["title"]
        if r["category"] in ET_TIMES:
            dt = et_to_ist(d, *ET_TIMES[r["category"]])
            if dt.date() != d:
                title += f" [US date {d:%a %d-%b}]"
            d, at = dt.date(), dt.time()
        out.append(make(d, r["category"], title, "verified" if r["verified_on"] else "unverified", r["impact"],
                        at=at, source=r["source"], verified_on=r["verified_on"]))
    return out


def coverage(rows: list) -> dict:
    cov = {}
    for r in rows:
        lo, hi = cov.get(r["category"], (r["date"], r["date"]))
        cov[r["category"]] = (min(lo, r["date"]), max(hi, r["date"]))
    return cov


# ---------------------------------------------------------------------------
# Computed events: exchange calendar, SEBI deadlines, tax dates
# ---------------------------------------------------------------------------
def _holiday_names() -> dict:
    names = {}
    ref = SKILLS_DIR / "indian-market-structure" / "references"
    for f in sorted(ref.glob("holidays-*.md")) if ref.is_dir() else []:
        for m in re.finditer(r"^\|\s*\d+\s*\|\s*(\d{1,2} [A-Za-z]{3} \d{4})\s*\|\s*\w+\s*\|\s*(.+?)\s*\|\s*$",
                             f.read_text(encoding="utf-8"), re.M):
            try:
                names[datetime.strptime(m.group(1), "%d %b %Y").date()] = re.sub(r"\s*\(.*", "", m.group(2))
            except ValueError:
                continue
    return names


HOLIDAY_NAMES = _holiday_names()
NSE_UNDERLYINGS = ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50", "STOCK")
BSE_UNDERLYINGS = ("SENSEX", "BANKEX")


def expiry_events(a: date, b: date) -> list:
    groups = {}
    for u in NSE_UNDERLYINGS + BSE_UNDERLYINGS:
        exch, weekly, wd = nse.EXPIRY_RULES[u]
        count = (b - a).days // 7 + 3 if weekly else (b - a).days // 28 + 3
        for x in nse.expiries(u, a, count):
            d = date.fromisoformat(x["date"])
            if d > b:
                continue
            g = groups.setdefault((exch, d, x["type"]), {"names": [], "shifted": False, "missing": False, "wd": wd})
            g["names"].append("stock F&O" if u == "STOCK" else u)
            g["shifted"] |= x["shifted_for_holiday"]
            g["missing"] |= x["holiday_list_missing"]
    out = []
    for (exch, d, typ), g in sorted(groups.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        note = ""
        if g["shifted"]:
            nominal = d + timedelta(days=(g["wd"] - d.weekday()) % 7)
            note = f"moved from {fmt(nominal)} (exchange holiday)"
        if g["missing"]:
            note = (note + "; " if note else "") + f"holiday list for {d.year} not loaded, date may move"
        out.append(make(d, "expiry", f"{exch} {typ} expiry: {', '.join(g['names'])}", "computed",
                        "high" if typ == "monthly" else "medium", at=CLOSE, note=note,
                        shifted_for_holiday=g["shifted"], holiday_list_missing=g["missing"],
                        exchange=exch, expiry_type=typ, underlyings=g["names"]))
        if exch == "NSE" and typ == "monthly":
            monday = d - timedelta(days=d.weekday())
            out.append(make(monday, "expiry-week", f"NSE monthly expiry week (expiry {fmt(d)})", "computed", end=d,
                            holiday_list_missing=g["missing"]))
    return out


def holiday_events(a: date, b: date) -> list:
    out, d = [], a
    while d <= b:
        if d.weekday() < 5 and nse.is_holiday(d):
            missing = holiday_list_missing(d)
            name = HOLIDAY_NAMES.get(d) or FIXED_NAMES.get((d.month, d.day), "Exchange holiday")
            out.append(make(d, "holiday", f"NSE/BSE closed: {name}", "computed", holiday_list_missing=missing,
                            note=f"fixed-date holiday only; the {d.year} exchange list is not loaded" if missing else ""))
        d += timedelta(days=1)
    # long weekends: 3+ consecutive non-trading days
    d = a - timedelta(days=6)
    while d <= b + timedelta(days=6):
        if nse.is_trading_day(d):
            d += timedelta(days=1)
            continue
        run_end = d
        while not nse.is_trading_day(run_end + timedelta(days=1)):
            run_end += timedelta(days=1)
        days = (run_end - d).days + 1
        if days >= 3 and run_end >= a and d <= b:
            missing = holiday_list_missing(run_end)
            out.append(make(d, "long-weekend", f"Market shut {days} days ({d:%a %d-%b} to {run_end:%a %d-%b})",
                            "computed", end=run_end, holiday_list_missing=missing,
                            note="holiday list not loaded for this year" if missing else ""))
        d = run_end + timedelta(days=1)
    return out


def season_events(a: date, b: date) -> list:
    out = []
    for s in rs.results_seasons(a, b):
        qe, dl = s["quarter_end"], s["deadline"]
        rule = "60 days (Q4 with audited annual results)" if qe.month == 3 else "45 days"
        out.append(make(qe + timedelta(days=1), "results-season", f"{s['quarter']} results season",
                        "computed", end=dl, note=f"SEBI LODR Reg 33: within {rule} of the {qe:%d-%b-%Y} quarter end",
                        quarter=s["quarter"], deadline=dl))
        out.append(make(dl, "results-season", f"{s['quarter']} results deadline (SEBI LODR, {rule})", "computed",
                        quarter=s["quarter"], deadline=dl))
    return out


def tax_events(a: date, b: date) -> list:
    out = []
    for y in range(a.year - 1, b.year + 2):
        for m, pct in ((6, 15), (9, 45), (12, 75), (3, 100)):
            d = date(y, m, 15)
            out.append(make(d, "tax", f"Advance tax instalment: {pct}% of estimated {fy_label(d)} tax due",
                            "computed", note="business income (F&O/intraday) and gains already realised"))
        prev = f"FY{(y - 1) % 100:02d}-{y % 100:02d}"
        out.append(make(date(y, 7, 31), "tax", f"ITR due date for {prev}: ITR-1/ITR-2 (no business income)",
                        "computed", note="capital gains without F&O/intraday business income"))
        if y >= 2026:
            out.append(make(date(y, 8, 31), "tax", f"ITR due date for {prev}: ITR-3/ITR-4 without tax audit",
                            "computed", note="F&O/intraday traders; moved from 31 Jul by Finance Act 2026 as reported, "
                                             "confirm on incometax.gov.in", uncertain=True))
        out.append(make(date(y, 10, 31), "tax", f"ITR due date for {prev}: tax-audit cases", "computed"))
        last = nse.prev_trading_day(date(y, 3, 31))
        missing = holiday_list_missing(date(y, 3, 31))
        out.append(make(last, "tax", f"Last trading session of FY{(y - 1) % 100:02d}-{y % 100:02d}", "computed",
                        holiday_list_missing=missing,
                        note="last chance to book gains/losses in this FY; confirm treatment with a CA"
                             + ("; holiday list not loaded, session may move" if missing else "")))
        out.append(make(date(y, 3, 1), "tax", "LTCG-exemption (₹1.25 lakh) and loss-harvesting window", "computed",
                        end=last, note="see indian-tax-for-traders: tax.py harvest"))
    return out


# ---------------------------------------------------------------------------
# Rule-based recurring releases ("expected, confirm on the day")
# ---------------------------------------------------------------------------
def rule_events(a: date, b: date) -> list:
    out = []
    R = lambda d, cat, title, note="", uncertain=False, at=None, impact=None: out.append(
        make(d, cat, title, "rule", impact, at=at if at else IST_TIMES.get(cat), note=note, uncertain=uncertain))
    for y, m in months_between(a, b):
        R(weekday_on_or_after(date(y, m, 12)), "cpi", f"India CPI inflation ({prev_month_name(y, m)})",
          "MoSPI: 12th at 16:00; a weekend or holiday moves it to the next working day")
        R(weekday_on_or_after(date(y, m, 14)), "wpi", f"India WPI inflation ({prev_month_name(y, m)})",
          "Office of the Economic Adviser: 14th at 12:00; next working day if a holiday")
        R(weekday_on_or_after(date(y, m, min(28, calendar.monthrange(y, m)[1]))), "iip",
          "India IIP (industrial production)",
          "MoSPI calendar: 28th at 16:00, next working day if a holiday; some months differ")
        R(nth_working_day(y, m, 1), "pmi", f"HSBC India Manufacturing PMI ({prev_month_name(y, m)})",
          "S&P Global: first working day, 10:30 IST (pattern, not a published rule)", uncertain=True)
        R(nth_working_day(y, m, 3), "pmi", f"HSBC India Services PMI ({prev_month_name(y, m)})",
          "S&P Global: third working day, 10:30 IST (pattern, not a published rule)", uncertain=True)
        R(date(y, m, 1), "auto-sales", f"Monthly auto sales ({prev_month_name(y, m)})",
          "carmakers and two-wheeler makers report dispatches on the 1st, some a day later")
        R(date(y, m, 1), "gst", f"GST collections ({prev_month_name(y, m)})",
          "usually published on the 1st; timing has varied")
        R(next_session(date(y, m, 10)), "amfi", f"AMFI mutual-fund flows and SIP data ({prev_month_name(y, m)})",
          "AMFI publishes in the second week; the exact day varies (placed on the first session from the 10th)",
          uncertain=True)
        if m in (2, 5, 8, 11):
            q = {2: "Q3", 5: "Q4 + provisional full year", 8: "Q1", 11: "Q2"}[m]
            R(last_weekday_of_month(y, m), "gdp", f"India GDP estimates ({q})",
              "MoSPI: last working day of the month at 16:00" + ("; the 2026 release slipped to 5 Jun" if m == 5 else ""),
              uncertain=(m == 5))
        first_friday = date(y, m, 1) + timedelta(days=(4 - date(y, m, 1).weekday()) % 7)
        R(first_friday, "us-jobs", "US jobs report (estimate: first Friday)",
          "BLS sometimes uses the second Friday; 08:30 New York time", uncertain=True,
          at=et_to_ist(first_friday, 8, 30).time())
        if m == 2:
            d = date(y, 2, 1)
            R(d, "budget", f"Union Budget {y}-{(y + 1) % 100:02d} (expected 1 Feb)",
              "presented on 1 Feb since 2017; a weekend date has meant a special live session (Sat 2025, Sun 2026)")
        if m in (3, 9):
            exp = nse.prev_trading_day(nse.last_weekday(y, m, nse.EXPIRY_RULES["NIFTY"][2]))
            R(exp, "index-rebalance", f"NIFTY indices semi-annual rebalance: passive trades at the close",
              "changes take effect from the next session (Sep 2026: effective 30 Sep after the 29 Sep expiry); "
              "NSE Indices announces about four weeks ahead", at=CLOSE)
        if m in (3, 6, 9, 12):
            d = weekday_on_or_after(date(y, m, 1))
            R(d, "msci", "MSCI quarterly review effective (estimate)",
              "MSCI effective dates are near the 1st of Mar/Jun/Sep/Dec but vary (May 2027: 28 May)", uncertain=True)
    return [e for e in out if a - timedelta(days=1) <= e["date"] <= b + timedelta(days=1)]


# ---------------------------------------------------------------------------
# User events (results/board meetings/ex-dates/AGMs/lock-ins, or any date,category,title row)
# ---------------------------------------------------------------------------
USER_CATS = {
    "result": "results", "results": "results", "earnings": "results", "financial results": "results",
    "board meeting": "board-meeting", "board-meeting": "board-meeting", "bm": "board-meeting",
    "ex-date": "ex-date", "ex date": "ex-date", "exdate": "ex-date", "dividend": "ex-date", "bonus": "ex-date",
    "split": "ex-date", "record date": "ex-date", "agm": "agm", "egm": "agm", "lock-in": "lock-in",
    "lockin": "lock-in", "lock in": "lock-in", "lock-in expiry": "lock-in", "anchor lock-in": "lock-in",
}
USER_LABEL = {"results": "results", "board-meeting": "board meeting", "ex-date": "ex-date", "agm": "AGM",
              "lock-in": "lock-in expiry"}


def _norm_cat(text: str) -> str:
    t = re.sub(r"[_]+", " ", text.strip().lower())
    if t in USER_CATS:
        return USER_CATS[t]
    t2 = t.replace(" ", "-")
    return t2 if t2 in CATEGORIES else "other"


def load_user_events(path: str) -> list:
    p = Path(path)
    if not p.is_file():
        raise EventError(f"events file {path} not found")
    with open(p, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        cols = {c.strip().lower(): c for c in (reader.fieldnames or []) if c}
        sc, dc = rs._col(cols, rs.SYMBOL_COLS), rs._col(cols, rs.DATE_COLS)
        cc = rs._col(cols, ("category", "type", "event", "kind"))
        tc, pc = rs._col(cols, rs.TIMING_COLS), rs._col(cols, rs.PURPOSE_COLS)
        ttl, ic = rs._col(cols, ("title", "description", "details", "name")), rs._col(cols, ("impact",))
        if not dc or not (sc or (cc and ttl)):
            raise EventError(f"{p.name}: needs symbol,date[,timing] or date,category,title columns; found {list(cols)}")
        out = []
        for n, r in enumerate(reader, start=2):
            get = lambda c: (r.get(c) or "").strip() if c else ""
            if not any((v or "").strip() for v in r.values() if isinstance(v, str)):
                continue
            where = f"{p.name} line {n}"
            raw = get(dc)
            if not raw:
                raise EventError(f"{where}: missing date")
            d, clock = rs.parse_date(raw, where)
            sym = get(sc).upper()
            if get(cc):
                cat = _norm_cat(get(cc))
            elif get(pc):
                cat = "results" if re.search(r"result|financ", get(pc), re.I) else "board-meeting"
            else:
                cat = "results"
            timing, clock = rs.resolve_timing(get(tc) if tc else None, clock, where)
            impact = get(ic).lower() or None
            if impact and impact not in IMPACTS:
                raise EventError(f"{where}: impact must be low, medium or high")
            title = get(ttl) or (f"{sym} {USER_LABEL.get(cat, get(cc) or cat)}" if sym else get(cc))
            if sym and sym not in title.upper():
                title = f"{sym}: {title}"
            if timing:
                title += f" ({clock:%H:%M} IST)" if clock else f" ({timing})"
            if cat in ("results", "board-meeting"):
                reacts = rs.exposure_sessions(d, timing)
            elif clock or cat in COMPANY:
                reacts = [d if nse.is_trading_day(d) and (clock is None or clock < CLOSE)
                          else next_session(d + timedelta(days=1))]
            else:
                reacts = None
            e = make(d, cat, title, "user", impact, at=clock, symbol=sym, source=p.name,
                     note="timing unknown: the results day and the next session both count" if cat in ("results", "board-meeting") and not timing else "",
                     timing=timing)
            if reacts is not None:
                e["reacts"] = reacts
            out.append(e)
    if not out:
        raise EventError(f"{p.name}: no events found")
    return out


# ---------------------------------------------------------------------------
# Assemble
# ---------------------------------------------------------------------------
def _key(e: dict):
    if e["category"] in COMPANY:
        return ("co", e["symbol"], e["date"], e["category"])
    if e["category"] in SINGLE:
        return ("one", e["date"], e["category"])
    return ("any", e["date"], e["category"], e["title"].lower(), e["symbol"])


def dedupe(events: list) -> tuple[list, int]:
    seen, out, dups = {}, [], 0
    for e in events:
        k = _key(e)
        if k in seen:
            dups += 1
            first = seen[k]
            if first["kind"] == "user" and e["kind"] == "user" and not first.get("timing") and e.get("timing"):
                first.update({f: e[f] for f in ("title", "time_ist", "timing", "reacts", "note") if f in e})
            continue
        seen[k] = e
        out.append(e)
    return out, dups


def _reacts(e: dict) -> list | None:
    if "reacts" in e:
        return e["reacts"]
    if e["kind"] == "computed" or e["category"] in ("results-season", "tax", "expiry-week", "special-session"):
        return None
    if e["end"]:
        return None
    d, t = e["date"], e["time_ist"]
    if nse.is_trading_day(d) and (t is None or t < CLOSE):
        return [d]
    return [next_session(d + timedelta(days=1))]


def finish(e: dict) -> dict:
    e["weekday"] = e["date"].strftime("%a")
    e["why"] = CATEGORIES[e["category"]][1]
    e["reacts"] = _reacts(e)
    k = e["kind"]
    if k == "computed":
        e["status"] = "computed" + (" (holiday list not loaded for that year)" if e.get("holiday_list_missing") else "")
    elif k == "rule":
        e["status"] = ("rough estimate (uncertain rule), confirm" if e["uncertain"]
                       else "expected by rule, confirm on the day")
    elif k == "verified":
        e["status"] = f"verified {e['verified_on']}"
    elif k == "unverified":
        e["status"] = "UNVERIFIED, confirm with a web search"
    else:
        e["status"] = "your events file"
    return e


def build(start: date, end: date, ref_rows: list, user_events: list) -> tuple[list, int]:
    a, b = start - timedelta(days=100), end + timedelta(days=10)
    cov = coverage(ref_rows)
    rules = [e for e in rule_events(a, b)
             if not (e["category"] in cov and cov[e["category"]][0] <= e["date"] <= cov[e["category"]][1])]
    events = (reference_events(ref_rows) + expiry_events(a, b) + holiday_events(a, b) + season_events(a, b)
              + tax_events(a, b) + rules + list(user_events))
    events, dups = dedupe(events)
    events = [finish(e) for e in events if e["date"] <= end and (e["end"] or e["date"]) >= start]
    events.sort(key=lambda e: (e["date"], e["time_ist"] or time(0), -IMPACTS.index(e["impact"]), e["title"]))
    return events, dups


def expand_categories(spec: str | None) -> set | None:
    if not spec:
        return None
    out = set()
    for item in (s.strip().lower() for s in spec.split(",") if s.strip()):
        if item in ALIASES:
            out |= ALIASES[item]
        elif item in CATEGORIES:
            out.add(item)
        else:
            names = sorted(set(CATEGORIES) | set(ALIASES))
            raise EventError(f"unknown category {item!r}; use one of: {', '.join(names)}")
    return out


def select(events: list, cats: set | None, min_impact: str) -> list:
    floor = IMPACTS.index(min_impact)
    return [e for e in events if (cats is None or e["category"] in cats) and IMPACTS.index(e["impact"]) >= floor]


# ---------------------------------------------------------------------------
# Window (holding-period) check
# ---------------------------------------------------------------------------
def is_risk(e: dict, include_expiry: bool) -> bool:
    if e["category"] in RISK_CATEGORIES:
        return True
    if e["kind"] == "user" and e["impact"] == "high":
        return True
    return include_expiry and e["category"] == "expiry"


def _inside(e: dict, start: date, end: date) -> bool:
    if e["reacts"]:
        return any(start <= r <= end for r in e["reacts"])
    return e["date"] <= end and (e["end"] or e["date"]) >= start


def window(events: list, start: date, end: date, symbols: list | None, include_expiry: bool = False,
           near: int = 2) -> dict:
    if end < start:
        raise EventError("--to is before --from")
    want = set(symbols) if symbols else None
    rows, after = [], []
    horizon, n, d = end, 0, end
    while n < near:
        d += timedelta(days=1)
        if nse.is_trading_day(d):
            n, horizon = n + 1, d
    for e in events:
        if e["category"] in COMPANY and want is not None and e["symbol"] not in want:
            continue
        if _inside(e, start, end):
            rows.append(e)
        elif is_risk(e, include_expiry) and e["reacts"] and all(r > end for r in e["reacts"]) \
                and min(e["reacts"]) <= horizon:
            after.append(e)
    risky = [e for e in rows if is_risk(e, include_expiry)]
    missing = []
    if want:
        have = {e["symbol"] for e in events if e["category"] in COMPANY}
        seasons = rs.results_seasons(start, end)
        for s in sorted(want - have):
            note = "no company event in your events file"
            if seasons:
                note += (f"; {seasons[-1]['quarter']} results are due by {seasons[-1]['deadline']:%d-%b-%Y} "
                         "(SEBI LODR) - check exchange board-meeting announcements")
            missing.append({"symbol": s, "note": note})
    return {"from": start, "to": end, "events": rows, "risk_events": [e["title"] for e in risky],
            "event_risk": "yes" if risky else "no", "discipline_flag": "--event yes" if risky else "--event no",
            "after_exit": after, "missing": missing, "include_expiry": include_expiry}


# ---------------------------------------------------------------------------
# Staleness of the reference file
# ---------------------------------------------------------------------------
def staleness(ref_rows: list, today: date, days: int = 60, max_age: int = 120) -> dict:
    warnings, per = [], {}
    cov = coverage(ref_rows)
    for cat, who in TRACKED.items():
        if cat not in cov:
            warnings.append(f"no {cat} dates in the reference file; {who}")
            per[cat] = None
            continue
        last = cov[cat][1]
        per[cat] = last
        left = (last - today).days
        if left < days:
            warnings.append(f"{cat}: reference dates end {fmt(last)} ({left} days from today); add the next batch - {who}")
    for r in ref_rows:
        if r["date"] < today:
            continue
        if not r["verified_on"]:
            warnings.append(f"unverified: {fmt(r['date'])} {r['title']} - confirm with a web search and fill verified_on")
    old = {}
    for r in ref_rows:
        if r["date"] >= today and r["verified_on"] and (today - r["verified_on"]).days > max_age:
            old.setdefault(r["category"], []).append(r)
    for cat, rs_ in sorted(old.items()):
        oldest = min(r["verified_on"] for r in rs_)
        warnings.append(f"{cat}: {len(rs_)} upcoming date(s) last verified {oldest} ({(today - oldest).days} days ago); "
                        "re-check for reschedules")
    hol = nse.HOLIDAYS_KNOWN_UNTIL
    if (hol - today).days < days:
        warnings.append(f"exchange holiday list ends {fmt(hol)}: add next year's NSE holidays to "
                        "fno-options-analysis/scripts/options.py (expiries and long weekends depend on it)")
    ends = [v for v in per.values() if v]
    return {"today": today, "coverage_end": min(ends) if ends else None, "per_category_last_date": per,
            "holidays_known_until": hol, "warn_within_days": days, "max_verified_age_days": max_age,
            "warnings": warnings, "stale": bool(warnings)}


# ---------------------------------------------------------------------------
# ICS export
# ---------------------------------------------------------------------------
def _ics_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> list:
    out, cur, size = [], "", 0
    for ch in line:
        n = len(ch.encode("utf-8"))
        if size + n > (75 if not out else 74):
            out.append(cur)
            cur, size = "", 0
        cur += ch
        size += n
    out.append(cur)
    return [out[0]] + [" " + x for x in out[1:]]


def ics_text(events: list, stamp: datetime) -> str:
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//claude-trading-skills-india//event-calendar//EN",
             "CALSCALE:GREGORIAN", "METHOD:PUBLISH", "X-WR-CALNAME:India market events", "X-WR-TIMEZONE:Asia/Kolkata"]
    st = stamp.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    for e in events:
        uid = hashlib.sha1(f"{e['date']}|{e['category']}|{e['title']}|{e['symbol']}".encode()).hexdigest()[:20]
        lines += ["BEGIN:VEVENT", f"UID:{uid}@event-calendar", f"DTSTAMP:{st}"]
        if e["time_ist"] and not e["end"]:
            dt = datetime.combine(e["date"], e["time_ist"], IST).astimezone(UTC)
            lines += [f"DTSTART:{dt:%Y%m%dT%H%M%SZ}", f"DTEND:{dt + timedelta(minutes=30):%Y%m%dT%H%M%SZ}"]
        else:
            last = (e["end"] or e["date"]) + timedelta(days=1)
            lines += [f"DTSTART;VALUE=DATE:{e['date']:%Y%m%d}", f"DTEND;VALUE=DATE:{last:%Y%m%d}"]
        desc = f"{e['why']}\nStatus: {e['status']}" + (f"\nNote: {e['note']}" if e["note"] else "") \
            + (f"\nSource: {e['source']}" if e["source"] else "")
        lines += [f"SUMMARY:{_ics_escape('[' + e['impact'].upper() + '] ' + e['title'])}",
                  f"DESCRIPTION:{_ics_escape(desc)}", f"CATEGORIES:{e['category'].upper()}",
                  "TRANSP:TRANSPARENT", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(x for line in lines for x in _fold(line)) + "\r\n"


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def _line(e: dict) -> str:
    at = f"{e['time_ist']:%H:%M}" if e["time_ist"] else "  -  "
    span = f" → {e['end']:%a %d-%b}" if e["end"] else ""
    return f"  {e['impact'].upper():<6} {at}  {e['title']}{span}  [{e['status']}]"


def print_events(events: list, start: date, end: date, heading: str, dups: int = 0) -> None:
    print(f"{heading}: {fmt(start)} → {fmt(end)} (IST)")
    ongoing = [e for e in events if e["end"] and e["date"] < start]
    if ongoing:
        print("Ongoing:")
        for e in ongoing:
            print(_line(e))
    day = None
    for e in events:
        if e in ongoing:
            continue
        if e["date"] != day:
            day = e["date"]
            print(f"\n{fmt(day)}")
        print(_line(e))
        if e["note"]:
            print(f"         {e['note']}")
    if not events:
        print("  no events match.")
    cats = sorted({e["category"] for e in events if e["impact"] != "low"})
    if cats:
        print("\nWhy traders care:")
        for c in cats:
            print(f"  {c}: {CATEGORIES[c][1]}")
    if dups:
        print(f"\n({dups} duplicate event(s) merged)")
    print("\nComputed = exchange calendar/SEBI/tax rules; expected = recurring-release rule; verified = reference "
          "file. Confirm rule-based and unverified dates on the day.")


def print_window(res: dict) -> None:
    print(f"Holding window {fmt(res['from'])} → {fmt(res['to'])}:")
    for e in res["events"]:
        flag = "RISK" if e["title"] in res["risk_events"] else ""
        print(f"  {flag:<4} {e['date']:%a %d-%b} " + _line(e).strip())
    if not res["events"]:
        print("  no events inside the window.")
    for e in res["after_exit"]:
        print(f"  NEAR   {e['title']} reacts {', '.join(f'{r:%a %d-%b}' for r in e['reacts'])}: "
              "if the exit slips, you carry it")
    for m in res["missing"]:
        print(f"  ?      {m['symbol']}: {m['note']}")
    print(f"\nEvent risk: {res['event_risk'].upper()}"
          + (f" ({'; '.join(res['risk_events'])})" if res["risk_events"] else ""))
    print(f"trading-discipline gate: pass {res['discipline_flag']}")


def print_staleness(res: dict) -> None:
    print(f"Reference check as of {fmt(res['today'])}: dated coverage ends {res['coverage_end'] or 'n/a'}; "
          f"holidays loaded to {res['holidays_known_until']}")
    for cat, last in res["per_category_last_date"].items():
        print(f"  {cat:<8} last dated event {last or 'none'}")
    if not res["warnings"]:
        print("OK: nothing to refresh.")
    for w in res["warnings"]:
        print(f"STALE: {w}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--events", action="append", default=[],
                        help="your events CSV: symbol,date[,timing][,category] or date,category,title (repeatable)")
    common.add_argument("--reference", help="reference CSV (default references/events-2026-27.csv)")
    common.add_argument("--category", help="comma list: rbi, expiry, results, macro, us, index, tax, holidays, ...")
    common.add_argument("--min-impact", default="low", choices=IMPACTS)
    common.add_argument("--today", help="pretend today is this date (IST)")
    common.add_argument("--json", action="store_true")

    p = argparse.ArgumentParser(description="Offline India market-event calendar")
    sub = p.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("week", parents=[common], help="this week (Mon-Sun)")
    w.add_argument("--from", dest="start", help="any date in the week (default today)")
    w.add_argument("--next", action="store_true", help="the following week")
    m = sub.add_parser("month", parents=[common], help="one calendar month")
    m.add_argument("--month", help="YYYY-MM (default this month)")
    r = sub.add_parser("range", parents=[common], help="events between two dates")
    r.add_argument("--from", dest="start", required=True)
    r.add_argument("--to", dest="end", required=True)
    n = sub.add_parser("next", parents=[common], help="next events of a category")
    n.add_argument("--from", dest="start")
    n.add_argument("--count", type=int, default=3)
    wn = sub.add_parser("window", parents=[common], help="events inside a holding window, yes/no event risk")
    wn.add_argument("--from", dest="start", help="window start (default today)")
    wn.add_argument("--to", dest="end", required=True, help="planned exit date")
    wn.add_argument("--symbols", help="positions, comma-separated (company events for other symbols are ignored)")
    wn.add_argument("--include-expiry", action="store_true", help="count F&O expiries as event risk (option positions)")
    ic = sub.add_parser("ics", parents=[common], help="export an .ics calendar file")
    ic.add_argument("--out", required=True)
    ic.add_argument("--from", dest="start")
    ic.add_argument("--to", dest="end")
    s = sub.add_parser("staleness", parents=[common], help="warn when reference dates run out or are old")
    s.add_argument("--days", type=int, default=60, help="warn when coverage ends within N days (60)")
    s.add_argument("--max-age", type=int, default=120, help="warn when upcoming rows were verified > N days ago (120)")
    s.add_argument("--strict", action="store_true", help="exit 1 when anything is stale")
    a = p.parse_args(argv)
    try:
        return _run(a)
    except (ValueError, OSError, KeyError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


def _run(a) -> int:
    today = parse_day(a.today, "--today") if a.today else datetime.now(IST).date()
    ref_rows = load_reference(a.reference or DEFAULT_REFERENCE)
    if a.cmd == "staleness":
        if a.days < 0 or a.max_age < 0:
            raise EventError("--days and --max-age must be >= 0")
        res = staleness(ref_rows, today, a.days, a.max_age)
        rs.dump(res) if a.json else print_staleness(res)
        return 1 if a.strict and res["stale"] else 0
    user = [e for f in a.events for e in load_user_events(f)]
    cats = expand_categories(a.category)

    if a.cmd == "week":
        d = parse_day(a.start, "--from") if a.start else today
        start = d - timedelta(days=d.weekday()) + timedelta(days=7 if a.next else 0)
        end, heading = start + timedelta(days=6), "Week"
    elif a.cmd == "month":
        if a.month:
            mm = re.fullmatch(r"(\d{4})-(\d{1,2})", a.month.strip())
            if not mm or not 1 <= int(mm.group(2)) <= 12:
                raise EventError(f"--month: use YYYY-MM, e.g. 2026-12 (got {a.month!r})")
            y, mo = int(mm.group(1)), int(mm.group(2))
        else:
            y, mo = today.year, today.month
        start, end, heading = date(y, mo, 1), date(y, mo, calendar.monthrange(y, mo)[1]), "Month"
    elif a.cmd == "range":
        start, end, heading = parse_day(a.start, "--from"), parse_day(a.end, "--to"), "Range"
        if end < start:
            raise EventError("--to is before --from")
        if (end - start).days > 800:
            raise EventError("range is limited to about two years")
    elif a.cmd == "next":
        if not cats:
            raise EventError("next needs --category (e.g. rbi, expiry, results, fomc, budget, macro)")
        if a.count < 1:
            raise EventError("--count must be >= 1")
        start = parse_day(a.start, "--from") if a.start else today
        end = start + timedelta(days=450)
        events, _ = build(start, end, ref_rows, user)
        hits = [e for e in select(events, cats, a.min_impact) if e["date"] >= start][:a.count]
        if a.json:
            rs.dump({"from": start, "category": sorted(cats), "events": hits})
            return 0
        if not hits:
            print(f"No {', '.join(sorted(cats))} event found from {fmt(start)} in the next 450 days. "
                  "Run `staleness`: the reference file may need new dates.")
            return 0
        print_events(hits, hits[0]["date"], hits[-1]["date"], f"Next {', '.join(sorted(cats))}")
        return 0
    elif a.cmd == "window":
        start = parse_day(a.start, "--from") if a.start else today
        end = parse_day(a.end, "--to")
        events, _ = build(start, end + timedelta(days=10), ref_rows, user)
        syms = sorted({x.strip().upper() for x in a.symbols.split(",") if x.strip()}) if a.symbols else None
        res = window(select(events, cats, a.min_impact), start, end, syms, a.include_expiry)
        rs.dump(res) if a.json else print_window(res)
        return 0
    else:  # ics
        start = parse_day(a.start, "--from") if a.start else today
        end = parse_day(a.end, "--to") if a.end else start + timedelta(days=90)
        if end < start:
            raise EventError("--to is before --from")
        events, _ = build(start, end, ref_rows, user)
        events = select(events, cats, a.min_impact)
        Path(a.out).write_text(ics_text(events, datetime.now(UTC)), encoding="utf-8", newline="")
        if a.json:
            rs.dump({"out": a.out, "events": len(events), "from": start, "to": end})
        else:
            print(f"Wrote {len(events)} event(s) {fmt(start)} → {fmt(end)} to {a.out}")
        return 0

    events, dups = build(start, end, ref_rows, user)
    events = select(events, cats, a.min_impact)
    if a.json:
        rs.dump({"from": start, "to": end, "events": events, "duplicates_merged": dups})
    else:
        print_events(events, start, end, heading, dups)
    return 0


if __name__ == "__main__":
    sys.exit(main())
