#!/usr/bin/env python3
"""Portfolio health check for an Indian equity (delivery/CNC) portfolio.

Python 3.10+ standard library only. Reads a holdings CSV (or a broker XLSX export) and
reuses the sibling skills for tax rules (indian-tax-for-traders/tax.py), charges
(trading-costs/charges.py) and price loading/ATR (technical-analysis/ta.py).

Sub-commands
------------
  review     Value, P&L, weights, concentration, sector / group / market-cap mix, holding-period
             clock per lot, losers, pledged / SME / surveillance flags.
  risk       Volatility, beta vs NIFTY, max drawdown and correlated pairs from price history,
             plus portfolio heat (capital at risk to the stops in a `stop` column).
  rebalance  Whole-share trades toward target weights (file or equal-weight) with charges and
             capital-gains flags (STCG now vs LTCG later, ₹1.25 lakh exemption).

Examples
--------
  python3 portfolio.py review holdings.csv --cash 200000 --as-of 2026-11-01
  python3 portfolio.py risk holdings.csv --prices-dir prices/ --benchmark NIFTY
  python3 portfolio.py rebalance holdings.csv --targets targets.csv --broker zerodha --json

Educational tool, not investment or tax advice.
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import math
import re
import sys
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

SKILLS_DIR = Path(__file__).resolve().parents[2]

DEFAULT_MAX_STOCK = 10.0      # % of capital in one company
DEFAULT_MAX_SECTOR = 25.0     # % of capital in one sector
DEFAULT_MAX_GROUP = 20.0      # % of capital in one business group (Adani, Tata, ...)
DEFAULT_MCAP_TARGET = "60/25/15"
DEFAULT_MCAP_CUTOFFS = "100000,33000"   # ₹ crore; rough proxies for AMFI's rank-based lists
DEFAULT_LOSER_PCT = 20.0      # flag holdings this far below average cost
DEFAULT_CORR = 0.8
DEFAULT_MAX_HEAT = 6.0        # % of capital at risk to stops
PROMOTER_PLEDGE_RED_FLAG = 20.0
TRADING_DAYS = 252

SME_SERIES = {"SM", "ST", "SZ", "M", "MT"}          # NSE Emerge / BSE SME
RESTRICTED_SERIES = {"BE", "BZ", "BT", "T", "TS", "Z"}  # trade-for-trade / restricted


# ---------------------------------------------------------------------------
# Sibling-skill modules, loaded by path (skills are folders, not packages)
# ---------------------------------------------------------------------------
def _sibling(skill: str, script: str):
    name = f"skill_{skill.replace('-', '_')}_{script}"
    if name in sys.modules:
        return sys.modules[name]
    path = SKILLS_DIR / skill / "scripts" / f"{script}.py"
    if not path.exists():
        raise ValueError(f"missing helper script {path}")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


tax = _sibling("indian-tax-for-traders", "tax")


def _charges():
    return _sibling("trading-costs", "charges")


def _ta():
    return _sibling("technical-analysis", "ta")


inr = tax.inr


def short_inr(x: float) -> str:
    """Compact lakh/crore style: 1234567 -> ₹12.35 L, 32000000 -> ₹3.20 Cr."""
    a = abs(x)
    sign = "-" if x < 0 else ""
    if a >= 1e7:
        return f"{sign}₹{a / 1e7:.2f} Cr"
    if a >= 1e5:
        return f"{sign}₹{a / 1e5:.2f} L"
    return inr(x)


# ---------------------------------------------------------------------------
# Built-in reference data (used only when the CSV lacks the column)
# ---------------------------------------------------------------------------
_SECTOR_GROUPS = {
    "Financial Services": "HDFCBANK ICICIBANK SBIN KOTAKBANK AXISBANK INDUSINDBK BAJFINANCE BAJAJFINSV "
                          "SHRIRAMFIN HDFCLIFE SBILIFE JIOFIN",
    "Information Technology": "TCS INFY HCLTECH WIPRO TECHM LTIM",
    "Oil, Gas & Fuels": "RELIANCE ONGC BPCL COALINDIA",
    "Power": "NTPC POWERGRID",
    "FMCG": "HINDUNILVR ITC NESTLEIND BRITANNIA TATACONSUM",
    "Automobile": "MARUTI M&M TATAMOTORS TMPV BAJAJ-AUTO EICHERMOT HEROMOTOCO",
    "Healthcare": "SUNPHARMA DRREDDY CIPLA APOLLOHOSP DIVISLAB MAXHEALTH",
    "Metals & Mining": "TATASTEEL JSWSTEEL HINDALCO ADANIENT",
    "Construction Materials": "ULTRACEMCO GRASIM SHREECEM",
    "Construction": "LT",
    "Consumer Durables": "TITAN ASIANPAINT",
    "Telecommunication": "BHARTIARTL",
    "Services": "ADANIPORTS INDIGO",
    "Consumer Services": "TRENT ETERNAL",
    "Capital Goods": "BEL",
}
BUILTIN_SECTOR = {s: sec for sec, names in _SECTOR_GROUPS.items() for s in names.split()}

_BUSINESS_GROUPS = {
    "Adani": "ADANIENT ADANIPORTS ADANIPOWER ADANIGREEN ADANIENSOL AMBUJACEM ACC",
    "Tata": "TCS TATASTEEL TATAMOTORS TMPV TITAN TRENT TATACONSUM TATAPOWER",
    "Bajaj": "BAJFINANCE BAJAJFINSV BAJAJ-AUTO",
    "Mahindra": "M&M TECHM",
    "Aditya Birla": "GRASIM HINDALCO ULTRACEMCO",
    "L&T": "LT LTIM",
    "HDFC": "HDFCBANK HDFCLIFE",
    "Reliance": "RELIANCE JIOFIN",
}
BUILTIN_GROUP = {s: g for g, names in _BUSINESS_GROUPS.items() for s in names.split()}


# ---------------------------------------------------------------------------
# Input: CSV / XLSX with broker-header mapping
# ---------------------------------------------------------------------------
def norm_header(h) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(h or "").strip().lower()).strip("_")


# canonical field -> accepted normalised headers, in priority order
ALIASES = {
    "symbol": ("symbol", "tradingsymbol", "trading_symbol", "instrument", "scrip", "scrip_name",
               "scrip_code", "stock", "stock_symbol", "stock_name", "company", "company_name",
               "security", "security_name", "name"),
    "qty": ("qty", "quantity", "quantity_available", "available_qty", "net_qty", "holding_qty",
            "holdings_qty", "total_qty", "total_quantity", "shares", "no_of_shares"),
    "t1_qty": ("t1_quantity", "t1_qty"),
    "buy_price": ("buy_price", "avg_price", "average_price", "avg_cost", "average_cost", "avg_buy_price",
                  "average_buy_price", "buy_avg", "buy_average", "avg_cost_price", "cost_price",
                  "purchase_price", "price"),
    "buy_value": ("buy_value", "invested", "invested_value", "investment", "investment_value",
                  "cost_value", "total_cost"),
    "ltp": ("ltp", "last_price", "current_price", "cmp", "market_price", "close", "close_price",
            "closing_price", "previous_closing_price", "prev_close"),
    "cur_value": ("cur_val", "current_value", "closing_value", "market_value", "present_value", "value"),
    "buy_date": ("buy_date", "purchase_date", "acquisition_date", "date", "trade_date"),
    "sector": ("sector", "industry"),
    "mcap": ("mcap", "market_cap", "marketcap", "mcap_cr", "market_cap_cr", "market_cap_crore", "cap",
             "cap_category", "category"),
    "isin": ("isin",),
    "exchange": ("exchange", "exch"),
    "series": ("series",),
    "group": ("group", "business_group", "promoter_group"),
    "pledged_qty": ("pledged_qty", "pledged", "pledged_quantity", "collateral_quantity", "quantity_pledged"),
    "long_term_qty": ("quantity_long_term", "long_term_qty", "lt_qty", "long_term_quantity"),
    "stop": ("stop", "stop_loss", "stoploss", "sl"),
    "sme": ("sme",),
    "illiquid": ("illiquid",),
    "surveillance": ("surveillance", "asm_gsm", "asm", "gsm", "flags", "flag"),
    "promoter_pledge": ("promoter_pledge", "promoter_pledge_pct", "promoter_pledged_pct"),
    "fmv_2018": ("fmv_2018", "fmv_31jan2018"),
}
PLEDGE_PARTS = ("quantity_pledged_margin", "quantity_pledged_loan")


def _num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "").replace("₹", "").replace("%", "").strip()
    if s in ("", "-", "--", "NA", "N/A", "na", "null", "None", "nan"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _date(v):
    if v is None or str(v).strip() in ("", "-"):
        return None
    n = _num(v)
    if n is not None and 20000 < n < 80000 and "-" not in str(v) and "/" not in str(v):
        return date(1899, 12, 30) + timedelta(days=int(n))     # Excel serial date
    return tax.parse_date(v)


def _truthy(v) -> bool:
    return str(v or "").strip().lower() in ("1", "y", "yes", "true", "sme", "x")


def read_xlsx(path: str, sheet: str | None = None) -> list:
    """Minimal .xlsx reader (first sheet or the named one) -> list of row lists of strings."""
    m = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    r_ns = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        shared = []
        if "xl/sharedStrings.xml" in names:
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(f"{m}si"):
                shared.append("".join(t.text or "" for t in si.iter(f"{m}t")))
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        sheets = [(s.get("name"), s.get(f"{r_ns}id")) for s in wb.iter(f"{m}sheet")]
        rels = {r.get("Id"): r.get("Target") for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))}
        if not sheets:
            raise ValueError("workbook has no sheets")
        chosen = sheets[0]
        if sheet:
            match = [s for s in sheets if (s[0] or "").lower() == sheet.lower()]
            if not match:
                raise ValueError(f"sheet {sheet!r} not found; sheets: {', '.join(s[0] for s in sheets)}")
            chosen = match[0]
        target = rels[chosen[1]].lstrip("/")
        target = target if target.startswith("xl/") else "xl/" + target
        rows = []
        for row in ET.fromstring(z.read(target)).iter(f"{m}row"):
            cells = {}
            for c in row.iter(f"{m}c"):
                ref = re.match(r"([A-Z]+)", c.get("r", "A"))
                col = 0
                for ch in ref.group(1):
                    col = col * 26 + ord(ch) - 64
                t, v = c.get("t"), c.find(f"{m}v")
                if t == "s" and v is not None:
                    val = shared[int(v.text)]
                elif t == "inlineStr":
                    val = "".join(x.text or "" for x in c.iter(f"{m}t"))
                else:
                    val = v.text if v is not None else ""
                cells[col - 1] = val
            if cells:
                rows.append([cells.get(i, "") for i in range(max(cells) + 1)])
        return rows


def _read_rows(path: str, sheet: str | None = None) -> list:
    if str(path).lower().endswith((".xlsx", ".xlsm")):
        return read_xlsx(path, sheet)
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return list(csv.reader(fh))


def _find_header(rows: list) -> int:
    sym, qty = set(ALIASES["symbol"]), set(ALIASES["qty"])
    for i, row in enumerate(rows[:40]):
        keys = {norm_header(c) for c in row}
        if keys & sym and keys & qty:
            return i
    raise ValueError("no header row with a symbol column (symbol / tradingsymbol / instrument / stock name) "
                     "and a quantity column (qty / quantity / quantity available)")


def load_holdings(path: str, sheet: str | None = None) -> tuple:
    """Return (lots, notes). One lot per data row; a symbol may span several rows (tax lots)."""
    rows = _read_rows(path, sheet)
    if not rows:
        raise ValueError(f"{path}: file is empty")
    h = _find_header(rows)
    header = [norm_header(c) for c in rows[h]]
    idx = {}
    for field, names in ALIASES.items():
        for n in names:
            if n in header:
                idx[field] = header.index(n)
                break
    pledge_cols = [header.index(p) for p in PLEDGE_PARTS if p in header]
    notes = []
    if "ltp" not in idx and "cur_value" not in idx:
        raise ValueError("need a price column (ltp / last price / closing price) or a current-value column")
    if "buy_price" not in idx and "buy_value" not in idx:
        raise ValueError("need an average-cost column (buy_price / avg cost / average price) or a buy-value column")
    if "buy_date" not in idx:
        notes.append("No buy_date column: holding-period (STCG/LTCG) analysis is limited. Add one row per "
                     "lot with buy_date, or use the tradebook (tax.py tradebook) for open lots.")
    if pledge_cols and "qty" in idx and header[idx["qty"]] == "quantity_available":
        notes.append("Zerodha Console layout detected: check whether 'Quantity Available' already includes "
                     "pledged shares in your export; if not, add them to qty.")

    def cell(row, field):
        i = idx.get(field)
        return row[i].strip() if i is not None and i < len(row) and row[i] is not None else ""

    lots = []
    for n, row in enumerate(rows[h + 1:], start=h + 2):
        if not any(str(c).strip() for c in row):
            continue
        sym = cell(row, "symbol").upper()
        qty = _num(cell(row, "qty"))
        if not sym or sym.startswith("TOTAL") or qty is None:
            continue
        qty += _num(cell(row, "t1_qty")) or 0.0
        if qty <= 0:
            continue
        ltp = _num(cell(row, "ltp"))
        if not ltp and _num(cell(row, "cur_value")):
            ltp = _num(cell(row, "cur_value")) / qty
        bp = _num(cell(row, "buy_price"))
        if not bp and _num(cell(row, "buy_value")):
            bp = _num(cell(row, "buy_value")) / qty
        if not ltp or ltp <= 0:
            raise ValueError(f"row {n} ({sym}): missing or invalid price")
        if bp is None or bp < 0:
            raise ValueError(f"row {n} ({sym}): missing or invalid average cost")
        pledged = _num(cell(row, "pledged_qty")) or 0.0
        pledged += sum(_num(row[i]) or 0.0 for i in pledge_cols if i < len(row))
        try:
            bdate = _date(cell(row, "buy_date"))
        except ValueError:
            raise ValueError(f"row {n} ({sym}): unrecognised buy_date {cell(row, 'buy_date')!r}")
        lots.append({
            "symbol": sym, "qty": qty, "buy_price": bp, "ltp": ltp, "buy_date": bdate,
            "sector": cell(row, "sector"), "mcap": cell(row, "mcap"), "isin": cell(row, "isin").upper(),
            "exchange": cell(row, "exchange").upper(), "series": cell(row, "series").upper(),
            "group": cell(row, "group"), "pledged_qty": pledged,
            "long_term_qty": _num(cell(row, "long_term_qty")), "stop": _num(cell(row, "stop")),
            "sme": _truthy(cell(row, "sme")), "illiquid": _truthy(cell(row, "illiquid")),
            "surveillance": cell(row, "surveillance").upper(),
            "promoter_pledge": _num(cell(row, "promoter_pledge")),
            "fmv_2018": _num(cell(row, "fmv_2018")) or 0.0,
        })
    if not lots:
        raise ValueError(f"{path}: no holdings rows found under the header")
    return lots, notes


# ---------------------------------------------------------------------------
# Aggregation and classification
# ---------------------------------------------------------------------------
def parse_mcap_target(s: str) -> dict:
    parts = [float(x) for x in re.split(r"[/,:]", s) if x.strip()]
    if len(parts) != 3 or any(p < 0 for p in parts) or abs(sum(parts) - 100) > 0.5:
        raise ValueError("--mcap-target must be three percentages summing to 100, e.g. 60/25/15")
    return dict(zip(("large", "mid", "small"), parts))


def parse_cutoffs(s: str) -> tuple:
    parts = [float(x) for x in s.split(",") if x.strip()]
    if len(parts) != 2 or parts[0] <= parts[1] or parts[1] <= 0:
        raise ValueError("--mcap-cutoffs must be 'LARGE_MIN,MID_MIN' in ₹ crore, e.g. 100000,33000")
    return parts[0], parts[1]


def classify_mcap(raw: str, symbol: str, cutoffs: tuple) -> tuple:
    """Return (bucket, source). Text labels win; numbers are ₹ crore against the cut-offs."""
    s = (raw or "").strip().lower().replace("-", " ").replace("_", " ")
    if s:
        n = _num(s)
        if n is not None:
            return ("large" if n >= cutoffs[0] else "mid" if n >= cutoffs[1] else "small"), "mcap ₹cr"
        if s.startswith(("large", "l ", "lc")) or s == "l":
            return "large", "column"
        if s.startswith(("mid", "m ", "mc")) or s == "m":
            return "mid", "column"
        if s.startswith(("small", "micro", "s ", "sc", "sme")) or s == "s":
            return "small", "column"
    if symbol in BUILTIN_SECTOR:
        return "large", "built-in (NIFTY 50 name)"
    return "unknown", "missing"


def aggregate(lots: list, cutoffs: tuple) -> dict:
    pos = {}
    for lot in lots:
        p = pos.setdefault(lot["symbol"], {"symbol": lot["symbol"], "qty": 0.0, "invested": 0.0, "value": 0.0,
                                           "ltp": lot["ltp"], "lots": [], "pledged_qty": 0.0, "stop": None,
                                           "sector": "", "mcap_raw": "", "group": "", "series": "",
                                           "surveillance": "", "sme": False, "illiquid": False,
                                           "promoter_pledge": None, "isin": "", "long_term_qty": None})
        p["qty"] += lot["qty"]
        p["invested"] += lot["qty"] * lot["buy_price"]
        p["ltp"] = lot["ltp"]
        p["value"] = p["qty"] * p["ltp"]
        p["lots"].append(lot)
        p["pledged_qty"] += lot["pledged_qty"]
        p["sme"] = p["sme"] or lot["sme"]
        p["illiquid"] = p["illiquid"] or lot["illiquid"]
        if lot["long_term_qty"] is not None:
            p["long_term_qty"] = (p["long_term_qty"] or 0.0) + lot["long_term_qty"]
        for k, src in (("stop", "stop"), ("sector", "sector"), ("mcap_raw", "mcap"), ("group", "group"),
                       ("series", "series"), ("surveillance", "surveillance"),
                       ("promoter_pledge", "promoter_pledge"), ("isin", "isin")):
            if not p[k] and lot[src]:
                p[k] = lot[src]
    for s, p in pos.items():
        p["value"] = p["qty"] * p["ltp"]
        p["avg_cost"] = p["invested"] / p["qty"] if p["qty"] else 0.0
        p["pnl"] = p["value"] - p["invested"]
        p["pnl_pct"] = (p["pnl"] / p["invested"] * 100) if p["invested"] else 0.0
        if p["sector"]:
            p["sector_source"] = "column"
        elif s in BUILTIN_SECTOR:
            p["sector"], p["sector_source"] = BUILTIN_SECTOR[s], "built-in"
        else:
            p["sector"], p["sector_source"] = "Unknown", "missing"
        if p["group"]:
            p["group_source"] = "column"
        elif s in BUILTIN_GROUP:
            p["group"], p["group_source"] = BUILTIN_GROUP[s], "built-in"
        else:
            p["group_source"] = "missing"
        p["mcap"], p["mcap_source"] = classify_mcap(p["mcap_raw"], s, cutoffs)
    return pos


def _tax_rows(lots: list, as_of: date, realized_stcg=0.0, realized_ltcg=0.0) -> dict:
    """Run tax.harvest on lots that carry a buy date. Returns the harvest result with lot index keys."""
    dated = [{"symbol": l["symbol"], "buy_date": l["buy_date"], "qty": l["qty"], "buy_price": l["buy_price"],
              "ltp": l["ltp"], "fmv_2018": l["fmv_2018"], "lot_id": i}
             for i, l in enumerate(lots) if l["buy_date"]]
    res = tax.harvest(dated, as_of, realized_stcg, realized_ltcg)
    res["by_lot"] = {r["lot_id"]: r for r in res["lots"]}
    return res


# ---------------------------------------------------------------------------
# review
# ---------------------------------------------------------------------------
def review(lots: list, cash: float = 0.0, as_of: date | None = None, max_stock=DEFAULT_MAX_STOCK,
           max_sector=DEFAULT_MAX_SECTOR, max_group=DEFAULT_MAX_GROUP, mcap_target=DEFAULT_MCAP_TARGET,
           mcap_cutoffs=DEFAULT_MCAP_CUTOFFS, loser_pct=DEFAULT_LOSER_PCT,
           realized_stcg=0.0, realized_ltcg=0.0) -> dict:
    as_of = as_of or date.today()
    if cash < 0:
        raise ValueError("--cash cannot be negative")
    target = parse_mcap_target(mcap_target) if isinstance(mcap_target, str) else mcap_target
    pos = aggregate(lots, parse_cutoffs(mcap_cutoffs) if isinstance(mcap_cutoffs, str) else mcap_cutoffs)
    equity = sum(p["value"] for p in pos.values())
    invested = sum(p["invested"] for p in pos.values())
    total = equity + cash
    flags = []

    def flag(level, msg):
        flags.append({"level": level, "message": msg})

    holdings = []
    for p in sorted(pos.values(), key=lambda p: -p["value"]):
        w = p["value"] / total * 100
        holdings.append({"symbol": p["symbol"], "qty": p["qty"], "avg_cost": round(p["avg_cost"], 4),
                         "ltp": p["ltp"], "value": p["value"], "invested": p["invested"], "pnl": p["pnl"],
                         "pnl_pct": p["pnl_pct"], "weight_pct": w, "sector": p["sector"],
                         "sector_source": p["sector_source"], "mcap": p["mcap"], "mcap_source": p["mcap_source"],
                         "group": p["group"] or None, "lots": len(p["lots"])})
        if w > max_stock:
            flag("REVIEW" if w > 2 * max_stock else "WARN",
                 f"{p['symbol']} is {w:.1f}% of capital (single-stock limit {max_stock:g}%).")
        if p["pnl_pct"] <= -loser_pct:
            flag("WARN", f"{p['symbol']} is {-p['pnl_pct']:.1f}% below average cost ({inr(p['pnl'])}): "
                         "re-check the thesis; is it a falling knife or a planned averaging?")
        if p["pledged_qty"] > 0:
            flag("WARN", f"{p['symbol']}: {p['pledged_qty']:g} of {p['qty']:g} shares pledged as margin "
                         "collateral; a sharp fall cuts collateral value and can force square-offs.")
        series = p["series"]
        if p["sme"] or series in SME_SERIES:
            flag("WARN", f"{p['symbol']} is an SME-platform stock: lot-based trading, thin volumes, wide "
                         "spreads; size it as illiquid.")
        elif series in RESTRICTED_SERIES:
            flag("WARN", f"{p['symbol']} trades in series {series} (trade-for-trade / restricted): no intraday "
                         "netting and lower liquidity.")
        if p["illiquid"]:
            flag("WARN", f"{p['symbol']} marked illiquid: exits may take days; avoid market orders.")
        if any(t in p["surveillance"] for t in ("ASM", "GSM", "ESM")):
            flag("REVIEW", f"{p['symbol']} is under exchange surveillance ({p['surveillance']}): higher margins, "
                           "possible trade-for-trade and price bands.")
        if p["promoter_pledge"] is not None and p["promoter_pledge"] > PROMOTER_PLEDGE_RED_FLAG:
            flag("REVIEW", f"{p['symbol']}: promoter pledge {p['promoter_pledge']:g}% of promoter holding "
                           f"(> {PROMOTER_PLEDGE_RED_FLAG:g}% red-flag level).")

    weights = [h["weight_pct"] for h in holdings]
    top5, top10 = sum(weights[:5]), sum(weights[:10])

    def bucket(key):
        out = {}
        for h in holdings:
            k = h[key] or "Unassigned"
            out[k] = out.get(k, 0.0) + h["weight_pct"]
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    sectors = bucket("sector")
    for s, w in sectors.items():
        if s != "Unknown" and w > max_sector:
            flag("REVIEW" if w > 1.6 * max_sector else "WARN",
                 f"Sector {s} is {w:.1f}% of capital (limit {max_sector:g}%).")
    unknown_sector = sectors.get("Unknown", 0.0)
    if unknown_sector:
        flag("INFO", f"{unknown_sector:.1f}% of capital has no sector; add a `sector` column for a full check.")
    members = {}
    for h in holdings:
        if h["group"]:
            members.setdefault(h["group"], []).append(h["symbol"])
    # A "group" of one company is just the single-stock check, so report groups spanning 2+ holdings.
    groups = {g: w for g, w in bucket("group").items() if len(members.get(g, [])) > 1}
    for g, w in groups.items():
        if w > max_group:
            flag("WARN", f"{g} group exposure is {w:.1f}% of capital (limit {max_group:g}%): "
                         "group-level events (governance, debt, regulator) hit all its companies together.")

    mix = {"large": 0.0, "mid": 0.0, "small": 0.0, "unknown": 0.0}
    for h in holdings:
        mix[h["mcap"]] += h["value"] / equity * 100 if equity else 0.0
    mcap = {"of_equity_pct": mix, "target_pct": target,
            "gap_pct": {k: mix[k] - target[k] for k in target}}
    if mix["small"] > target["small"] + 10:
        flag("WARN", f"Small caps are {mix['small']:.1f}% of equity vs target {target['small']:g}%: "
                     "deeper drawdowns and thinner exits in sell-offs.")

    # Holding-period clock per lot (rules from indian-tax-for-traders)
    hv = _tax_rows(lots, as_of, realized_stcg, realized_ltcg)
    lot_rows, soon30, soon60 = [], [], []
    for i, l in enumerate(lots):
        r = hv["by_lot"].get(i)
        if r is None:
            row = {"symbol": l["symbol"], "qty": l["qty"], "buy_date": None, "term": "unknown",
                   "days_to_long_term": None, "unrealised": (l["ltp"] - l["buy_price"]) * l["qty"],
                   "long_term_qty_reported": l["long_term_qty"]}
        else:
            row = {"symbol": l["symbol"], "qty": l["qty"], "buy_date": r["buy_date"], "term": r["term"],
                   "days_to_long_term": r["days_to_long_term"], "unrealised": r["unrealised"],
                   "cost_basis": r["cost_basis"],
                   "long_term_on": tax.add_months(l["buy_date"], 12).isoformat()}
            d = r["days_to_long_term"]
            if r["term"] == "ST" and 0 < d <= 60:
                (soon30 if d <= 30 else soon60).append(row)
                if r["unrealised"] > 0:
                    saving = r["unrealised"] * (tax.STCG_RATE - tax.LTCG_RATE)
                    flag("INFO", f"{l['symbol']} lot of {l['qty']:g} ({r['buy_date']}) turns long-term in {d} "
                                 f"days; selling before that taxes {inr(r['unrealised'])} at "
                                 f"{tax.STCG_RATE * 100:g}% instead of {tax.LTCG_RATE * 100:g}% "
                                 f"(≈{inr(saving)} more).")
        lot_rows.append(row)

    return {
        "as_of": as_of.isoformat(), "stocks": len(holdings), "lots": len(lots),
        "equity_value": equity, "cash": cash, "total_capital": total, "invested": invested,
        "unrealised_pnl": equity - invested,
        "unrealised_pnl_pct": ((equity - invested) / invested * 100) if invested else 0.0,
        "cash_pct": cash / total * 100 if total else 0.0,
        "top5_pct": top5, "top10_pct": top10,
        "limits": {"max_stock_pct": max_stock, "max_sector_pct": max_sector, "max_group_pct": max_group,
                   "loser_pct": loser_pct},
        "holdings": holdings, "sectors_pct": sectors, "groups_pct": groups, "mcap": mcap,
        "tax_lots": lot_rows, "turning_long_term_30d": soon30, "turning_long_term_31_60d": soon60,
        "ltcg_exemption_left_after_ideas": hv["ltcg_exemption_room_left"], "harvest_ideas": hv["ideas"],
        "flags": flags,
    }


# ---------------------------------------------------------------------------
# risk
# ---------------------------------------------------------------------------
def _mean(xs):
    return sum(xs) / len(xs)


def stdev(xs: list) -> float:
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def covariance(xs: list, ys: list) -> float:
    mx, my = _mean(xs), _mean(ys)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (len(xs) - 1)


def correlation(xs: list, ys: list) -> float:
    sx, sy = stdev(xs), stdev(ys)
    return covariance(xs, ys) / (sx * sy) if sx and sy else 0.0


def beta(rs: list, rb: list) -> float:
    vb = covariance(rb, rb)
    return covariance(rs, rb) / vb if vb else 0.0


def max_drawdown(returns: list) -> tuple:
    """Max peak-to-trough fall of the compounded series. Returns (fraction <= 0, peak_i, trough_i)."""
    level, peak, peak_i = 1.0, 1.0, 0
    worst, wp, wt = 0.0, 0, 0
    for i, r in enumerate(returns, start=1):
        level *= 1 + r
        if level > peak:
            peak, peak_i = level, i
        dd = level / peak - 1
        if dd < worst:
            worst, wp, wt = dd, peak_i, i
    return worst, wp, wt


def _closes(rows: list) -> dict:
    return {r["date"].date() if isinstance(r["date"], datetime) else r["date"]: r["close"] for r in rows}


def align_returns(series: dict, lookback: int = TRADING_DAYS) -> tuple:
    """series: name -> {date: close}. Common dates only; returns (dates, name -> daily returns)."""
    common = None
    for s in series.values():
        common = set(s) if common is None else common & set(s)
    dates = sorted(common or [])[-(lookback + 1):]
    rets = {k: [s[dates[i]] / s[dates[i - 1]] - 1 for i in range(1, len(dates))] for k, s in series.items()}
    return dates, rets


def load_price_dir(folder: str, symbols: list) -> tuple:
    d = Path(folder)
    if not d.is_dir():
        raise ValueError(f"--prices-dir {folder} is not a directory")
    files = {}
    for f in d.iterdir():
        if f.suffix.lower() in (".csv", ".json"):
            files.setdefault(f.stem.upper(), f)
    out, missing = {}, []
    ta = _ta()
    for s in symbols:
        f = next((files[c] for c in (s, s.replace("&", "_"), s.replace("&", ""), s + ".NS") if c in files), None)
        if f is None:
            missing.append(s)
            continue
        rows = ta.load_candles_json(str(f)) if f.suffix.lower() == ".json" else ta.load_csv(str(f))
        if rows:
            out[s] = rows
        else:
            missing.append(s)
    return out, missing


def load_yf(symbols: list, period: str = "2y") -> tuple:
    ta = _ta()
    out, missing = {}, []
    for s in symbols:
        try:
            out[s] = ta.fetch_yf(s, "NSE", period)
        except SystemExit as e:                    # ta.fetch_yf exits on missing data / no yfinance
            msg = str(e)
            if "not installed" in msg:
                raise ValueError("yfinance not installed: pip install yfinance, or pass --prices-dir")
            missing.append(s)
    return out, missing


def risk(lots: list, cash: float = 0.0, prices: dict | None = None, benchmark: str = "NIFTY",
         lookback: int = TRADING_DAYS, corr_threshold: float = DEFAULT_CORR, max_heat: float = DEFAULT_MAX_HEAT,
         atr_period: int = 14, missing: list | None = None) -> dict:
    """prices: name -> OHLC rows (ta.py format) for holdings and for the benchmark key."""
    pos = aggregate(lots, parse_cutoffs(DEFAULT_MCAP_CUTOFFS))
    equity = sum(p["value"] for p in pos.values())
    total = equity + cash
    prices = prices or {}
    notes, flags = [], []
    out = {"equity_value": equity, "cash": cash, "total_capital": total, "benchmark": benchmark}

    # --- Portfolio heat: capital at risk to stops ---
    heat_rows, at_risk = [], 0.0
    ta = _ta() if prices else None
    for s, p in sorted(pos.items(), key=lambda kv: -kv[1]["value"]):
        row = {"symbol": s, "ltp": p["ltp"], "qty": p["qty"], "stop": p["stop"]}
        if s in prices and len(prices[s]) > atr_period:
            r = prices[s]
            a = ta.atr([x["high"] for x in r], [x["low"] for x in r], [x["close"] for x in r], atr_period)[-1]
            row["atr"] = a
            row["atr_pct"] = a / p["ltp"] * 100 if a else None
        if p["stop"] is not None:
            if p["stop"] >= p["ltp"]:
                row["status"] = "breached"
                row["risk"] = 0.0
                flags.append({"level": "REVIEW", "message": f"{s}: stop {p['stop']:g} is at/above LTP "
                                                             f"{p['ltp']:g}; the stop has been hit or is stale."})
            else:
                row["risk"] = (p["ltp"] - p["stop"]) * p["qty"]
                row["status"] = "ok"
                if row.get("atr"):
                    row["stop_in_atr"] = (p["ltp"] - p["stop"]) / row["atr"]
            at_risk += row["risk"]
            row["risk_pct_capital"] = row["risk"] / total * 100
        heat_rows.append(row)
    with_stop = [r for r in heat_rows if r["stop"] is not None]
    heat = at_risk / total * 100 if total else 0.0
    out["heat"] = {"at_risk": at_risk, "heat_pct": heat, "max_heat_pct": max_heat,
                   "positions_with_stops": len(with_stop), "positions": heat_rows,
                   "uncovered_value_pct": sum(pos[r["symbol"]]["value"] for r in heat_rows if r["stop"] is None)
                   / total * 100 if total else 0.0}
    if not with_stop:
        notes.append("No `stop` column: heat cannot be measured. Add stop prices (see stop-loss-strategies).")
    elif heat > max_heat:
        flags.append({"level": "WARN", "message": f"Portfolio heat {heat:.1f}% of capital exceeds {max_heat:g}%: "
                                                  "too much at risk to stops at once."})
    notes.append("Heat assumes stops fill at the stop price; overnight gaps (results, news) can cost more.")

    # --- Statistics from price history ---
    have = [s for s in pos if s in prices]
    miss = sorted(set(missing or []) | {s for s in pos if s not in prices})
    if not prices or not have or benchmark not in prices:
        if prices and benchmark not in prices:
            notes.append(f"No price history for benchmark {benchmark}; volatility/beta skipped.")
        elif not prices:
            notes.append("No price history given (--prices-dir or --yf): volatility, beta, drawdown and "
                         "correlation skipped.")
        else:
            notes.append("No price history matched any holding; statistics skipped.")
        out.update({"stats": None, "missing_history": miss, "notes": notes, "flags": flags})
        return out
    series = {s: _closes(prices[s]) for s in have}
    series[benchmark] = _closes(prices[benchmark])
    dates, rets = align_returns(series, lookback)
    if len(dates) < 21:
        raise ValueError(f"only {len(dates)} common trading days across holdings and {benchmark}; need 21+")
    covered = sum(pos[s]["value"] for s in have)
    w = {s: pos[s]["value"] / covered for s in have}
    port = [sum(w[s] * rets[s][t] for s in have) for t in range(len(dates) - 1)]
    rb = rets[benchmark]
    ann = math.sqrt(TRADING_DAYS)
    mdd, pi, ti = max_drawdown(port)
    bmdd = max_drawdown(rb)[0]
    p_beta = beta(port, rb)
    per = []
    for s in sorted(have, key=lambda s: -w[s]):
        per.append({"symbol": s, "weight_in_covered_pct": w[s] * 100, "vol_ann_pct": stdev(rets[s]) * ann * 100,
                    "beta": beta(rets[s], rb), "max_drawdown_pct": max_drawdown(rets[s])[0] * 100})
    pairs = []
    for i, a in enumerate(have):
        for b in have[i + 1:]:
            c = correlation(rets[a], rets[b])
            if c > corr_threshold:
                pairs.append({"a": a, "b": b, "corr": c})
    pairs.sort(key=lambda x: -x["corr"])
    parent = {s: s for s in have}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for pr in pairs:
        parent[find(pr["a"])] = find(pr["b"])
    clusters = {}
    for s in have:
        clusters.setdefault(find(s), []).append(s)
    clusters = [sorted(c) for c in clusters.values() if len(c) > 1]
    for c in clusters:
        cw = sum(pos[s]["value"] for s in c) / total * 100
        flags.append({"level": "WARN" if cw > DEFAULT_MAX_SECTOR else "INFO",
                      "message": f"Correlated cluster {', '.join(c)} (pairwise > {corr_threshold:g}) is {cw:.1f}% "
                                 "of capital; it behaves like one bet."})
    eq_frac = equity / total if total else 1.0
    stats = {
        "from": dates[0].isoformat(), "to": dates[-1].isoformat(), "days": len(dates) - 1,
        "covered_value_pct": covered / equity * 100 if equity else 0.0,
        "vol_ann_pct": stdev(port) * ann * 100, "benchmark_vol_ann_pct": stdev(rb) * ann * 100,
        "beta": p_beta, "beta_incl_cash": p_beta * eq_frac * covered / equity if equity else 0.0,
        "max_drawdown_pct": mdd * 100, "drawdown_peak": dates[pi].isoformat(), "drawdown_trough": dates[ti].isoformat(),
        "benchmark_max_drawdown_pct": bmdd * 100,
        "return_pct": (math.prod(1 + r for r in port) - 1) * 100,
        "benchmark_return_pct": (math.prod(1 + r for r in rb) - 1) * 100,
        "per_stock": per, "correlated_pairs": pairs, "clusters": clusters,
    }
    if p_beta > 1.2:
        flags.append({"level": "WARN", "message": f"Beta {p_beta:.2f}: a 10% NIFTY fall implies roughly "
                                                  f"{p_beta * 10:.0f}% on these holdings."})
    notes.append("Statistics use today's weights held constant over the window (a what-if, not your "
                 "actual past returns).")
    if miss:
        notes.append(f"No price history for {', '.join(miss)}; excluded from statistics.")
    out.update({"stats": stats, "missing_history": miss, "notes": notes, "flags": flags})
    return out


# ---------------------------------------------------------------------------
# rebalance
# ---------------------------------------------------------------------------
def load_targets(path: str) -> tuple:
    """Return (kind, {key: weight_pct}, {key: price}). kind is 'symbol' or 'sector'."""
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.reader(fh))
    if not rows:
        raise ValueError(f"{path}: empty targets file")
    header = [norm_header(c) for c in rows[0]]
    if "symbol" in header:
        kind, ki = "symbol", header.index("symbol")
    elif "sector" in header:
        kind, ki = "sector", header.index("sector")
    else:
        raise ValueError("targets file needs a 'symbol' or 'sector' column plus 'weight'")
    wi = next((header.index(n) for n in ("weight", "weight_pct", "target", "target_pct", "pct") if n in header), None)
    if wi is None:
        raise ValueError("targets file needs a 'weight' column (percent)")
    pi = header.index("price") if "price" in header else None
    weights, px = {}, {}
    for r in rows[1:]:
        if len(r) <= max(ki, wi) or not r[ki].strip():
            continue
        key = r[ki].strip().upper() if kind == "symbol" else r[ki].strip()
        w = _num(r[wi])
        if w is None or w < 0:
            raise ValueError(f"targets: bad weight for {key!r}")
        weights[key] = w
        if pi is not None and pi < len(r) and _num(r[pi]):
            px[key] = _num(r[pi])
    if weights and sum(weights.values()) <= 1.0 + 1e-9 and all(w <= 1 for w in weights.values()):
        weights = {k: w * 100 for k, w in weights.items()}       # fractions -> percent
    return kind, weights, px


def _sell_tax(p: dict, qty: float, as_of: date, by_lot: dict, lot_ids: dict) -> list:
    """FIFO (oldest first, as for demat shares) split of a sale into tax lots."""
    lots = sorted(p["lots"], key=lambda l: (l["buy_date"] is None, l["buy_date"] or date.max))
    pieces, left = [], qty
    lt_hint = p.get("long_term_qty") or 0.0
    for l in lots:
        if left <= 1e-9:
            break
        take = min(left, l["qty"])
        r = by_lot.get(lot_ids[id(l)])
        if r is not None:
            cost, term, days = r["cost_basis"], r["term"], r["days_to_long_term"]
        else:
            cost, days = l["buy_price"], None
            term = "LT" if lt_hint >= take - 1e-9 else "unknown"
            lt_hint = max(0.0, lt_hint - take)
        pieces.append({"qty": take, "buy_date": l["buy_date"].isoformat() if l["buy_date"] else None,
                       "cost": cost, "gain": (p["ltp"] - cost) * take, "term": term, "days_to_long_term": days})
        left -= take
    return pieces


def rebalance(lots: list, cash: float = 0.0, as_of: date | None = None, targets: dict | None = None,
              kind: str = "symbol", target_prices: dict | None = None, cash_target: float = 0.0,
              band: float = 1.0, min_trade: float = 1000.0, broker: str = "zerodha", no_sell: bool = False,
              wait_days: int = 60, realized_stcg: float = 0.0, realized_ltcg: float = 0.0) -> dict:
    as_of = as_of or date.today()
    ch = _charges()
    pos = aggregate(lots, parse_cutoffs(DEFAULT_MCAP_CUTOFFS))
    equity = sum(p["value"] for p in pos.values())
    total = equity + cash
    if total <= 0:
        raise ValueError("portfolio value is zero")
    notes = []
    cur = {s: p["value"] / total * 100 for s, p in pos.items()}
    prices = {s: p["ltp"] for s, p in pos.items()}
    desired = {}
    if not targets:
        if not 0 <= cash_target < 100:
            raise ValueError("--cash-target must be between 0 and 100")
        each = (100 - cash_target) / len(pos)
        desired = {s: each for s in pos}
        mode = f"equal weight ({each:.2f}% each, cash {cash_target:g}%)"
    elif kind == "symbol":
        mode = "symbol targets"
        for s, w in targets.items():
            if s in pos:
                desired[s] = w
            elif (target_prices or {}).get(s):
                desired[s] = w
                prices[s] = target_prices[s]
                cur[s] = 0.0
            else:
                notes.append(f"{s}: not held and no price column in the targets file; skipped.")
        for s in pos:
            if s not in desired:
                desired[s] = cur[s]
                notes.append(f"{s}: no target given; left unchanged at {cur[s]:.1f}%.")
    else:
        mode = "sector targets"
        by_sector = {}
        for s, p in pos.items():
            by_sector.setdefault(p["sector"], []).append(s)
        for sec, w in targets.items():
            members = by_sector.get(sec) or next((v for k, v in by_sector.items() if k.lower() == sec.lower()), None)
            if not members:
                notes.append(f"Sector {sec}: no holdings to scale; add stocks yourself (see sector-rotation).")
                continue
            sv = sum(pos[s]["value"] for s in members)
            for s in members:
                desired[s] = w * pos[s]["value"] / sv
        for s in pos:
            if s not in desired:
                desired[s] = cur[s]
                notes.append(f"{s} ({pos[s]['sector']}): sector not in targets; left unchanged.")
    if sum(desired.values()) > 100 + 1e-6:
        raise ValueError(f"targets add up to {sum(desired.values()):.1f}% of capital (> 100%)")

    lot_ids = {id(l): i for i, l in enumerate(lots)}
    by_lot = _tax_rows(lots, as_of, realized_stcg, realized_ltcg)["by_lot"]
    sells, buys, holds = [], [], []
    for s in sorted(desired, key=lambda s: desired[s] - cur[s]):
        tw, cw, px = desired[s], cur[s], prices[s]
        delta = tw / 100 * total - cur[s] / 100 * total
        exit_all = tw == 0 and s in pos
        if not exit_all and abs(tw - cw) < band:
            holds.append({"symbol": s, "weight_pct": cw, "target_pct": tw, "reason": f"inside ±{band:g}pp band"})
            continue
        qty = pos[s]["qty"] if exit_all else math.floor(abs(delta) / px + 1e-9)
        if qty <= 0 or (qty * px < min_trade and not exit_all):
            holds.append({"symbol": s, "weight_pct": cw, "target_pct": tw,
                          "reason": f"trade below ₹{min_trade:,.0f} minimum"})
            continue
        t = {"symbol": s, "side": "SELL" if delta < 0 else "BUY", "qty": qty, "price": px, "value": qty * px,
             "weight_pct": cw, "target_pct": tw}
        if t["side"] == "SELL" and no_sell:
            holds.append({"symbol": s, "weight_pct": cw, "target_pct": tw, "reason": "--no-sell"})
            continue
        (sells if t["side"] == "SELL" else buys).append(t)

    # Sells: charges + capital-gains split
    st_total = lt_total = unknown_total = 0.0
    tax_flags = []
    for t in sells:
        c = ch.calculate("delivery", 0.0, t["price"], t["qty"], broker=broker)
        t["charges"] = c["total_charges"]
        t["tax_lots"] = _sell_tax(pos[t["symbol"]], t["qty"], as_of, by_lot, lot_ids)
        for piece in t["tax_lots"]:
            g = piece["gain"]
            if piece["term"] == "ST":
                st_total += g
                d = piece["days_to_long_term"]
                if g > 0 and d is not None and 0 < d <= wait_days:
                    tax_flags.append(f"WAIT? {t['symbol']}: {piece['qty']:g} sh bought {piece['buy_date']} turn "
                                     f"long-term in {d} days. Selling now taxes {inr(g)} at "
                                     f"{tax.STCG_RATE * 100:g}% (≈{inr(g * tax.STCG_RATE)}); after that "
                                     f"{tax.LTCG_RATE * 100:g}% (≈{inr(g * tax.LTCG_RATE)}), possibly nil "
                                     "inside the ₹1.25L exemption.")
                elif g > 0:
                    tax_flags.append(f"{t['symbol']}: realises STCG {inr(g)} at {tax.STCG_RATE * 100:g}%.")
                elif g < 0:
                    tax_flags.append(f"{t['symbol']}: books a short-term loss {inr(g)} (offsets STCG or LTCG).")
            elif piece["term"] == "LT":
                lt_total += g
                if g < 0:
                    tax_flags.append(f"{t['symbol']}: books a long-term loss {inr(g)} (offsets LTCG only).")
            else:
                unknown_total += g
                tax_flags.append(f"{t['symbol']}: holding period unknown for {piece['qty']:g} sh (gain {inr(g)}); "
                                 "add buy_date or check the broker's tax P&L.")
    exemption_left = max(0.0, tax.LTCG_EXEMPTION - max(0.0, realized_ltcg))
    net_st, net_lt = st_total, lt_total
    if net_st < 0:
        net_lt += net_st
        net_st = 0.0
    lt_taxable = max(0.0, net_lt - exemption_left)
    if lt_total > 0:
        tax_flags.append(f"Plan realises LTCG {inr(lt_total)}; {inr(min(max(net_lt, 0.0), exemption_left))} "
                         f"falls inside the remaining ₹1.25L exemption, {inr(lt_taxable)} taxed at "
                         f"{tax.LTCG_RATE * 100:g}%.")
    est_tax = net_st * tax.STCG_RATE + lt_taxable * tax.LTCG_RATE

    # Buys: funded by cash + sale proceeds; scale down if short
    proceeds = sum(t["value"] - t["charges"] for t in sells)
    available = cash + proceeds
    for t in buys:
        t["charges"] = ch.calculate("delivery", t["price"], 0.0, t["qty"], broker=broker)["total_charges"]
    need = sum(t["value"] + t["charges"] for t in buys)
    if buys and need > available:
        f = max(0.0, available / need)
        notes.append(f"Buys scaled to {f * 100:.0f}% to fit available cash {inr(available)}.")
        for t in buys:
            t["qty"] = math.floor(t["qty"] * f)
            t["value"] = t["qty"] * t["price"]
            t["charges"] = (ch.calculate("delivery", t["price"], 0.0, t["qty"], broker=broker)["total_charges"]
                            if t["qty"] else 0.0)
        buys = [t for t in buys if t["qty"] > 0]
    trades = sells + buys
    for t in trades:
        sign = -1 if t["side"] == "SELL" else 1
        held = pos[t["symbol"]]["value"] if t["symbol"] in pos else 0.0
        t["weight_after_pct"] = (held + sign * t["value"]) / total * 100
    cash_after = cash + proceeds - sum(t["value"] + t["charges"] for t in buys)
    return {
        "as_of": as_of.isoformat(), "mode": mode, "broker": broker, "total_capital": total, "cash": cash,
        "band_pp": band, "trades": trades, "holds": holds,
        "total_charges": sum(t["charges"] for t in trades), "cash_after": cash_after,
        "tax": {"stcg": st_total, "ltcg": lt_total, "unknown_term_gain": unknown_total,
                "ltcg_exemption_left_before": exemption_left, "estimated_tax": est_tax,
                "basis": "STCG/LTCG rates from indian-tax-for-traders tax.py; before surcharge, 4% cess and "
                         "any unused basic exemption. FIFO lot order as applies to demat shares."},
        "tax_flags": tax_flags, "notes": notes,
    }


# ---------------------------------------------------------------------------
# Printing
# ---------------------------------------------------------------------------
def print_review(r: dict, notes: list) -> None:
    print(f"\nPortfolio review as of {r['as_of']}  ({r['stocks']} stocks, {r['lots']} lots)")
    print(f"  Holdings {inr(r['equity_value'])} | Cash {inr(r['cash'])} ({r['cash_pct']:.1f}%) | "
          f"Total {inr(r['total_capital'])} ({short_inr(r['total_capital'])})")
    print(f"  Invested {inr(r['invested'])} | Unrealised P&L {inr(r['unrealised_pnl'])} "
          f"({r['unrealised_pnl_pct']:+.2f}%)")
    print(f"  Top-5 {r['top5_pct']:.1f}% | Top-10 {r['top10_pct']:.1f}% of capital")
    print(f"\n  {'Symbol':<14}{'Weight':>7}{'Value':>14}{'P&L %':>9}  {'Sector':<24}Cap")
    for h in r["holdings"]:
        sec = h["sector"] + ("*" if h["sector_source"] == "built-in" else "")
        print(f"  {h['symbol']:<14}{h['weight_pct']:>6.1f}%{inr(h['value']):>14}{h['pnl_pct']:>+8.1f}%  "
              f"{sec[:23]:<24}{h['mcap']}")
    if any(h["sector_source"] == "built-in" for h in r["holdings"]):
        print("  * sector from the built-in NIFTY 50 map")
    print(f"\n  Sectors (limit {r['limits']['max_sector_pct']:g}%): "
          + ", ".join(f"{k} {v:.1f}%" for k, v in r["sectors_pct"].items()))
    if r["groups_pct"]:
        print("  Groups: " + ", ".join(f"{k} {v:.1f}%" for k, v in r["groups_pct"].items()))
    m = r["mcap"]
    print("  Market cap (of equity) vs target: " + ", ".join(
        f"{k} {m['of_equity_pct'][k]:.1f}% (target {m['target_pct'][k]:g}%)" for k in ("large", "mid", "small"))
        + (f", unknown {m['of_equity_pct']['unknown']:.1f}%" if m["of_equity_pct"]["unknown"] else ""))
    print(f"\n  Tax clock: {len(r['turning_long_term_30d'])} lot(s) turn long-term within 30 days, "
          f"{len(r['turning_long_term_31_60d'])} in 31-60 days. "
          f"LTCG exemption left after the ideas below: {inr(r['ltcg_exemption_left_after_ideas'])}.")
    for x in r["turning_long_term_30d"] + r["turning_long_term_31_60d"]:
        print(f"    {x['symbol']:<12} {x['qty']:>7g} bought {x['buy_date']} -> long-term on {x['long_term_on']} "
              f"({x['days_to_long_term']}d), unrealised {inr(x['unrealised'])}")
    unknown = [x for x in r["tax_lots"] if x["term"] == "unknown"]
    if unknown:
        print(f"    {len(unknown)} lot(s) without buy_date"
              + (": broker reports long-term qty " + ", ".join(
                  f"{x['symbol']} {x['long_term_qty_reported']:g}" for x in unknown
                  if x["long_term_qty_reported"] is not None) if any(
                  x["long_term_qty_reported"] is not None for x in unknown) else ""))
    for i in r["harvest_ideas"]:
        print("    idea:", i)
    print("\n  Flags:")
    order = {"REVIEW": 0, "WARN": 1, "INFO": 2}
    for f in sorted(r["flags"], key=lambda f: order[f["level"]]) or [{"level": "OK", "message": "none"}]:
        print(f"    {f['level']:<7}{f['message']}")
    for n in notes:
        print("  note:", n)
    print("\nFlags are prompts for review, not sell signals. Educational, not investment advice.")


def print_risk(r: dict, notes: list) -> None:
    h = r["heat"]
    print(f"\nPortfolio risk (capital {inr(r['total_capital'])}, benchmark {r['benchmark']})")
    print(f"  Heat: {inr(h['at_risk'])} at risk to stops = {h['heat_pct']:.2f}% of capital "
          f"(limit {h['max_heat_pct']:g}%), {h['positions_with_stops']} position(s) with stops; "
          f"{h['uncovered_value_pct']:.1f}% of capital has no stop")
    for p in h["positions"]:
        if p["stop"] is None and "atr" not in p:
            continue
        bits = [f"LTP {p['ltp']:g}"]
        if p["stop"] is not None:
            bits.append(f"stop {p['stop']:g}" + (" BREACHED" if p.get("status") == "breached"
                                                  else f" risk {inr(p['risk'])} ({p['risk_pct_capital']:.2f}%)"))
        if p.get("atr"):
            bits.append(f"ATR {p['atr']:.2f} ({p['atr_pct']:.1f}%)")
        if p.get("stop_in_atr"):
            bits.append(f"stop {p['stop_in_atr']:.1f} ATR away")
        print(f"    {p['symbol']:<12} " + " | ".join(bits))
    s = r["stats"]
    if s:
        print(f"\n  Window {s['from']} → {s['to']} ({s['days']} days, {s['covered_value_pct']:.0f}% of holdings covered)")
        print(f"  Volatility {s['vol_ann_pct']:.1f}% a year (NIFTY {s['benchmark_vol_ann_pct']:.1f}%) | "
              f"Beta {s['beta']:.2f} (incl. cash {s['beta_incl_cash']:.2f})")
        print(f"  Max drawdown {s['max_drawdown_pct']:.1f}% ({s['drawdown_peak']} → {s['drawdown_trough']}) vs "
              f"NIFTY {s['benchmark_max_drawdown_pct']:.1f}% | window return {s['return_pct']:+.1f}% vs "
              f"{s['benchmark_return_pct']:+.1f}%")
        for x in s["per_stock"]:
            print(f"    {x['symbol']:<12} vol {x['vol_ann_pct']:>5.1f}%  beta {x['beta']:>5.2f}  "
                  f"maxDD {x['max_drawdown_pct']:>6.1f}%")
        for pr in s["correlated_pairs"]:
            print(f"    corr {pr['a']}–{pr['b']} {pr['corr']:.2f}")
    for f in r["flags"]:
        print(f"  {f['level']:<7}{f['message']}")
    for n in notes + r["notes"]:
        print("  note:", n)
    print("\nPast volatility and correlation change in sell-offs. Educational, not investment advice.")


def print_rebalance(r: dict, notes: list) -> None:
    print(f"\nRebalance plan as of {r['as_of']}: {r['mode']}, band ±{r['band_pp']:g}pp, broker {r['broker']}")
    print(f"  Capital {inr(r['total_capital'])} | cash now {inr(r['cash'])} → after {inr(r['cash_after'])}")
    if not r["trades"]:
        print("  No trades needed.")
    for t in r["trades"]:
        print(f"  {t['side']:<5}{t['qty']:>7g} {t['symbol']:<12} @ {t['price']:<10g} {inr(t['value']):>12}  "
              f"{t['weight_pct']:.1f}% → {t['weight_after_pct']:.1f}% (target {t['target_pct']:.1f}%)  "
              f"charges {inr(t['charges'])}")
    for hld in r["holds"]:
        print(f"  hold {hld['symbol']:<12} {hld['weight_pct']:.1f}% vs target {hld['target_pct']:.1f}% "
              f"({hld['reason']})")
    tx = r["tax"]
    print(f"\n  Charges {inr(r['total_charges'])} | STCG {inr(tx['stcg'])} | LTCG {inr(tx['ltcg'])} | "
          f"est. tax ≈{inr(tx['estimated_tax'])}")
    for f in r["tax_flags"]:
        print("   •", f)
    for n in notes + r["notes"]:
        print("  note:", n)
    print("  Basis:", tx["basis"])
    print("\nPlan only: confirm each order yourself. Estimate, not tax or investment advice.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _dump(obj) -> str:
    return json.dumps(obj, indent=2, default=lambda o: o.isoformat() if isinstance(o, (date, datetime)) else str(o))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Indian equity portfolio review, risk and rebalancing")
    sub = p.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("holdings", help="holdings CSV (or broker .xlsx export)")
    common.add_argument("--sheet", help="sheet name for .xlsx (default: first)")
    common.add_argument("--cash", type=float, default=0.0, help="free cash in ₹ (counts toward capital)")
    common.add_argument("--as-of", default=None, help="date for holding-period maths (default today)")
    common.add_argument("--json", action="store_true")

    rv = sub.add_parser("review", parents=[common], help="weights, concentration, flags, tax clock")
    rv.add_argument("--max-stock", type=float, default=DEFAULT_MAX_STOCK, help="single-stock limit %%")
    rv.add_argument("--max-sector", type=float, default=DEFAULT_MAX_SECTOR, help="sector limit %%")
    rv.add_argument("--max-group", type=float, default=DEFAULT_MAX_GROUP, help="business-group limit %%")
    rv.add_argument("--mcap-target", default=DEFAULT_MCAP_TARGET, help="large/mid/small %% of equity")
    rv.add_argument("--mcap-cutoffs", default=DEFAULT_MCAP_CUTOFFS, help="₹ crore: LARGE_MIN,MID_MIN")
    rv.add_argument("--loser-pct", type=float, default=DEFAULT_LOSER_PCT, help="flag holdings down this %% from cost")
    rv.add_argument("--realized-stcg", type=float, default=0.0)
    rv.add_argument("--realized-ltcg", type=float, default=0.0)

    rk = sub.add_parser("risk", parents=[common], help="volatility, beta, drawdown, correlation, heat")
    src = rk.add_mutually_exclusive_group()
    src.add_argument("--prices-dir", help="folder of SYMBOL.csv / SYMBOL.json daily candles (+ benchmark file)")
    src.add_argument("--yf", action="store_true", help="fetch daily history with yfinance (NSE)")
    rk.add_argument("--benchmark", default="NIFTY")
    rk.add_argument("--period", default="2y", help="yfinance period (default 2y)")
    rk.add_argument("--lookback", type=int, default=TRADING_DAYS, help="trading days used (default 252)")
    rk.add_argument("--corr", type=float, default=DEFAULT_CORR, help="flag pairs above this correlation")
    rk.add_argument("--max-heat", type=float, default=DEFAULT_MAX_HEAT, help="heat limit %% of capital")
    rk.add_argument("--atr-period", type=int, default=14)

    rb = sub.add_parser("rebalance", parents=[common], help="trades toward target weights")
    rb.add_argument("--targets", help="CSV: symbol,weight[,price] or sector,weight (percent)")
    rb.add_argument("--cash-target", type=float, default=0.0, help="cash %% to keep (equal-weight mode)")
    rb.add_argument("--band", type=float, default=1.0, help="skip drifts smaller than this many %% points")
    rb.add_argument("--min-trade", type=float, default=1000.0, help="skip trades below this ₹ value")
    rb.add_argument("--broker", default="zerodha", help="zerodha, groww, angelone, upstox, dhan")
    rb.add_argument("--no-sell", action="store_true", help="buy-only: deploy cash into underweights")
    rb.add_argument("--wait-days", type=int, default=60, help="flag short-term lots this close to long-term")
    rb.add_argument("--realized-stcg", type=float, default=0.0)
    rb.add_argument("--realized-ltcg", type=float, default=0.0)

    a = p.parse_args(argv)
    try:
        as_of = tax.parse_date(a.as_of) if a.as_of else date.today()
        if a.cash < 0:
            raise ValueError("--cash cannot be negative")
        lots, notes = load_holdings(a.holdings, a.sheet)
        if a.cmd == "review":
            r = review(lots, a.cash, as_of, a.max_stock, a.max_sector, a.max_group, a.mcap_target,
                       a.mcap_cutoffs, a.loser_pct, a.realized_stcg, a.realized_ltcg)
            printer = print_review
        elif a.cmd == "risk":
            symbols = sorted({l["symbol"] for l in lots})
            prices, missing = {}, []
            if a.prices_dir:
                prices, missing = load_price_dir(a.prices_dir, symbols + [a.benchmark.upper()])
            elif a.yf:
                prices, missing = load_yf(symbols + [a.benchmark.upper()], a.period)
            if (a.prices_dir or a.yf) and a.benchmark.upper() in missing:
                raise ValueError(f"no price history for benchmark {a.benchmark}")
            r = risk(lots, a.cash, prices, a.benchmark.upper(), a.lookback, a.corr, a.max_heat, a.atr_period,
                     [m for m in missing if m != a.benchmark.upper()])
            printer = print_risk
        else:
            targets, kind, tpx = None, "symbol", {}
            if a.targets:
                kind, targets, tpx = load_targets(a.targets)
            if a.broker not in _charges().BROKERS:
                raise ValueError(f"unknown broker {a.broker!r}; choose from {', '.join(_charges().BROKERS)}")
            r = rebalance(lots, a.cash, as_of, targets, kind, tpx, a.cash_target, a.band, a.min_trade, a.broker,
                          a.no_sell, a.wait_days, a.realized_stcg, a.realized_ltcg)
            printer = print_rebalance
    except (ValueError, OSError, KeyError, zipfile.BadZipFile, ET.ParseError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if a.json:
        print(_dump({**r, "input_notes": notes}))
    else:
        printer(r, notes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
