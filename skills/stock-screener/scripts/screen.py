#!/usr/bin/env python3
"""Fundamental stock screener for Indian equities: filter and rank a universe from a CSV you export.

Reads one fundamentals CSV (one row per company), typically a Screener.in screen or watchlist export,
maps its column headers by keyword, applies India red-flag gates, then runs a built-in preset or your
own `--where` filters. Every rule is shown pass/fail with the value, and passers are ranked by a
transparent composite score.

Zero dependencies (Python 3.10+ stdlib). Works fully offline; nothing is fetched. Optional
`--prices-dir` re-uses technical-analysis/ta.py for RSI(14) on daily price CSVs.

Sub-commands
  presets   list the built-in screens and their rules
  fields    list field names usable in --where/--sort (with --csv: how the file's headers were mapped)
  run       run a preset and/or --where filters: passers ranked, near-misses, gated-out names
  explain   per-rule breakdown for one company
  query     the equivalent Screener.in query text to paste into "Create a screen"

Presets: canslim-india, value-dividend, dividend-growth-pullback, garp, quality-compounder

Gates (every run unless --no-gates): market cap below ₹500 cr, promoter pledge above 20%, negative
net worth, and missing data for a rule. Banks/NBFCs/insurers are detected from the industry column
(or the name) and skip debt/ROCE/free-cash-flow rules, which do not describe a lender.

Scoring: each rule scores 50 at its threshold and 100 at its "best" level (linear, capped 0-100);
the composite is the weight-averaged rule score.

Examples
  python3 screen.py presets
  python3 screen.py run --csv export.csv --preset quality-compounder
  python3 screen.py run --csv export.csv --where "roce>20" --where "pe<25" --where "pledge<=5" --sort roce --top 20
  python3 screen.py run --csv export.csv --preset dividend-growth-pullback --prices-dir data/ --json
  python3 screen.py explain "Infosys" --csv export.csv --preset garp
  python3 screen.py query --preset value-dividend --gsec 7.15
  python3 screen.py fields --csv export.csv --map "div_growth=Div Gr 3Yrs %"
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parents[2]
DISCLAIMER = "Educational analysis, not investment advice."

DEFAULTS = {
    "min_mcap": 500.0,     # ₹ crore; below this is micro-cap/SME territory: thin floats, operator risk
    "max_pledge": 20.0,    # % of promoter holding pledged; the red-flag line in fundamental-screening
    "pledge_watch": 10.0,  # soft flag between this and max_pledge
    "gsec": 7.0,           # 10-year G-sec yield (%); pass the current one with --gsec
    "gsec_share": 0.4,     # dividend-yield bar for value-dividend = this share of the G-sec yield
    "top": 20,
    "small_cap": 2000.0,   # soft flag: below this check liquidity and the ASM/GSM lists
    "low_base": 200.0,     # soft flag: quarterly profit growth this high is usually a tiny base
}


class ScreenError(Exception):
    """Bad input the user can fix; printed as a single `error:` line with exit code 2."""


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------
def indian(x, d=2) -> str:
    """Indian digit grouping: 12,34,567.89. None -> an em dash."""
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return "—"
    if round(abs(x), d) == 0:
        x = 0.0
    whole, _, frac = f"{abs(x):.{d}f}".partition(".")
    if len(whole) > 3:
        head, tail, groups = whole[:-3], whole[-3:], []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        whole = ",".join(([head] if head else []) + groups + [tail])
    return ("-" if x < 0 else "") + whole + ("." + frac if frac else "")


def crore(x) -> str:
    return "—" if x is None else "₹" + indian(x, 0 if abs(x) >= 100 else 1) + " cr"


def fmt(field: str, v, short=False) -> str:
    """Format a value for its field's unit. `short` drops the unit (table cells)."""
    if v is None:
        return "—"
    kind = FIELDS[field].kind if field in FIELDS else "x"
    if kind == "pct":
        return f"{v:.1f}" + ("" if short else "%")
    if kind == "pp":
        return f"{v:+.2f}" + ("" if short else " pp")
    if kind == "cr":
        return indian(v, 0 if abs(v) >= 100 else 1) if short else crore(v)
    if kind == "rs":
        return ("" if short else "₹") + indian(v, 2)
    if kind == "rank":
        return f"{v:.0f}"
    return f"{v:.2f}"


def fmt_threshold(field: str, v: float) -> str:
    kind = FIELDS[field].kind if field in FIELDS else "x"
    if kind == "pct":
        return f"{v:g}%"
    if kind == "pp":
        return f"{v:g} pp"
    if kind == "cr":
        return "₹" + indian(v, 0) + " cr"
    if kind == "rs":
        return "₹" + indian(v, 2)
    return f"{v:g}"


def clean_json(o):
    """Strict JSON: NaN/Infinity -> null, floats rounded."""
    if isinstance(o, dict):
        return {str(k): clean_json(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean_json(v) for v in o]
    if isinstance(o, float):
        return round(o, 4) if math.isfinite(o) else None
    return o


def emit_json(res: dict) -> None:
    print(json.dumps(clean_json(res), indent=2, ensure_ascii=False, allow_nan=False))


# ---------------------------------------------------------------------------
# Field registry
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Field:
    label: str            # human name used in rule text
    col: str              # short table-column heading
    kind: str             # pct | pp | x | cr | rs | rank
    header: str = ""      # the Screener.in column header this usually arrives under
    header_ok: bool = False   # header seen on public Screener.in screen pages (see SKILL.md)
    query: str = ""       # Screener.in query field name
    query_ok: bool = False    # query name seen in a public Screener.in screen query
    derived: str = ""     # how it is computed when the file has no column for it


FIELDS = {
    "cmp": Field("Price", "Price", "rs", "CMP Rs.", True, "Current price", True),
    "mcap": Field("Market cap", "Mcap cr", "cr", "Mar Cap Rs.Cr.", True, "Market Capitalization", True),
    "pe": Field("P/E", "P/E", "x", "P/E", True, "Price to Earning", True),
    "pb": Field("P/B", "P/B", "x", "CMP / BV", True, "Price to book value", True,
                "price ÷ book value when both columns exist"),
    "peg": Field("PEG", "PEG", "x", "PEG", False, "PEG Ratio", True,
                 "P/E ÷ 3-year profit growth when growth is positive"),
    "div_yield": Field("Dividend yield", "Yld%", "pct", "Div Yld %", True, "Dividend yield", True),
    "payout": Field("Dividend payout", "Pay%", "pct", "Dividend Payout %", False, "Dividend Payout Ratio", True),
    "div_growth": Field("Dividend growth", "DivG%", "pct", "(no Screener column verified)", False, "", False,
                        "change from div_prev to div_last when both columns exist (one year)"),
    "div_last": Field("Dividend last year", "DivLY", "x", "Dividend last year", False, "Dividend last year", False),
    "div_prev": Field("Dividend preceding year", "DivPY", "x", "Dividend preceding year", False,
                      "Dividend preceding year", False),
    "np_qtr": Field("Net profit (quarter)", "NPQ cr", "cr", "NP Qtr Rs.Cr.", True, "Net Profit latest quarter", True),
    "qtr_profit_var": Field("Quarterly profit growth YoY", "QPAT%", "pct", "Qtr Profit Var %", True,
                            "YOY Quarterly profit growth", True),
    "sales_qtr": Field("Sales (quarter)", "SalesQ cr", "cr", "Sales Qtr Rs.Cr.", True, "Sales latest quarter", True),
    "qtr_sales_var": Field("Quarterly sales growth YoY", "QSales%", "pct", "Qtr Sales Var %", True,
                           "YOY Quarterly sales growth", True),
    "roce": Field("ROCE", "ROCE%", "pct", "ROCE %", True, "Return on capital employed", True),
    "roce_3y": Field("ROCE 3-yr avg", "ROCE3%", "pct", "ROCE 3Yr %", False,
                     "Average return on capital employed 3Years", False),
    "roce_5y": Field("ROCE 5-yr avg", "ROCE5%", "pct", "ROCE 5Yr %", False,
                     "Average return on capital employed 5Years", True),
    "roe": Field("ROE", "ROE%", "pct", "ROE %", True, "Return on equity", True),
    "roe_3y": Field("ROE 3-yr avg", "ROE3%", "pct", "ROE 3Yr %", False, "Average return on equity 3Years", True),
    "roe_5y": Field("ROE 5-yr avg", "ROE5%", "pct", "ROE 5Yr %", False, "Average return on equity 5Years", True),
    "roa": Field("ROA", "ROA%", "pct", "ROA %", False, "Return on assets", False),
    "de": Field("Debt/equity", "D/E", "x", "Debt / Eq", True, "Debt to equity", True),
    "eps": Field("EPS (12 months)", "EPS", "rs", "EPS 12M Rs.", False, "EPS", False),
    "eps_g3": Field("EPS growth 3-yr", "EPSG3%", "pct", "EPS Var 3Yrs %", False, "EPS growth 3Years", False),
    "sales_g1": Field("Sales growth (TTM)", "SalesG%", "pct", "Sales Var %", False, "Sales growth", True),
    "sales_g3": Field("Sales growth 3-yr", "SalesG3%", "pct", "Sales Var 3Yrs %", False, "Sales growth 3Years", True),
    "sales_g5": Field("Sales growth 5-yr", "SalesG5%", "pct", "Sales Var 5Yrs %", False, "Sales growth 5Years", True),
    "profit_g1": Field("Profit growth (TTM)", "PATG%", "pct", "Profit Var %", False, "Profit growth", True),
    "profit_g3": Field("Profit growth 3-yr", "PATG3%", "pct", "Profit Var 3Yrs %", False, "Profit growth 3Years", True),
    "profit_g5": Field("Profit growth 5-yr", "PATG5%", "pct", "Profit Var 5Yrs %", False, "Profit growth 5Years", True),
    "promoter": Field("Promoter holding", "Prom%", "pct", "Prom. Hold. %", False, "Promoter holding", True),
    "promoter_chg": Field("Promoter holding change", "PromΔ", "pp", "Change in Prom Hold %", False,
                          "Change in promoter holding", False),
    "pledge": Field("Promoter pledge", "Pledge%", "pct", "Pledged %", False, "Pledged percentage", True),
    "fii": Field("FII holding", "FII%", "pct", "FII Hold %", False, "FII holding", True),
    "fii_chg": Field("FII holding change", "FIIΔ", "pp", "Chg in FII Hold %", False, "Change in FII holding", True),
    "dii": Field("DII holding", "DII%", "pct", "DII Hold %", False, "DII holding", True),
    "dii_chg": Field("DII holding change", "DIIΔ", "pp", "Chg in DII Hold %", False, "Change in DII holding", True),
    "inst_chg": Field("FII+DII holding change", "InstΔ", "pp", "", False,
                      "Change in FII holding + Change in DII holding", True,
                      "fii_chg + dii_chg (whichever exist)"),
    "high52": Field("52-week high", "52wH", "rs", "52w High Rs.", False, "High price", False),
    "low52": Field("52-week low", "52wL", "rs", "52w Low Rs.", False, "Low price", False),
    "down_from_high": Field("Below 52-week high", "Off52wH%", "pct", "Down %", False, "Down from 52w high", True,
                            "(1 − price ÷ 52-week high) × 100 when both columns exist"),
    "ret_1y": Field("1-year return", "1Y%", "pct", "1Yr return %", False, "Return over 1year", False),
    "ret_6m": Field("6-month return", "6M%", "pct", "6mth return %", False, "Return over 6months", False),
    "ret_3m": Field("3-month return", "3M%", "pct", "3mth return %", False, "Return over 3months", False),
    "rs_pct": Field("RS percentile in this file", "RS", "rank", "", False, "", False,
                    "1-99 rank of the 1-year return among the companies in the file"),
    "rsi": Field("RSI(14)", "RSI", "x", "RSI", False, "RSI", True,
                 "from --prices-dir daily closes when given (overrides the column)"),
    "fcf": Field("Free cash flow", "FCF cr", "cr", "Free Cash Flow Rs.Cr.", False, "Free cash flow last year", False),
    "net_worth": Field("Net worth", "NW cr", "cr", "Net worth Rs.Cr.", False, "Net worth", False),
    "book_value": Field("Book value/share", "BV", "rs", "B.V. Rs.", False, "Book value", True),
    "int_cov": Field("Interest coverage", "IntCov", "x", "Int Coverage", False, "Interest Coverage Ratio", True),
    "opm": Field("Operating margin", "OPM%", "pct", "OPM %", False, "OPM", True),
    "industry_pe": Field("Industry P/E", "IndPE", "x", "Ind PE", False, "Industry PE", True),
}

# Extra spellings accepted in --where/--sort/--map and as snake_case CSV headers.
ALIASES = {
    "price": "cmp", "close": "cmp", "ltp": "cmp", "current_price": "cmp",
    "market_cap": "mcap", "marketcap": "mcap", "market_cap_cr": "mcap", "mcap_cr": "mcap",
    "p_e": "pe", "pe_ratio": "pe", "p_b": "pb", "pbv": "pb", "price_to_book": "pb", "peg_ratio": "peg",
    "dividend_yield": "div_yield", "div_yld": "div_yield", "yield": "div_yield",
    "dividend_payout": "payout", "payout_ratio": "payout", "dividend_growth": "div_growth",
    "qtr_profit_growth": "qtr_profit_var", "quarterly_profit_growth": "qtr_profit_var",
    "qtr_sales_growth": "qtr_sales_var", "quarterly_sales_growth": "qtr_sales_var",
    "debt_equity": "de", "debt_to_equity": "de", "d_e": "de",
    "sales_growth": "sales_g1", "profit_growth": "profit_g1",
    "sales_growth_3y": "sales_g3", "profit_growth_3y": "profit_g3",
    "sales_growth_5y": "sales_g5", "profit_growth_5y": "profit_g5", "eps_growth_3y": "eps_g3",
    "promoter_holding": "promoter", "promoters": "promoter", "promoter_change": "promoter_chg",
    "pledged": "pledge", "pledged_pct": "pledge", "pledge_pct": "pledge", "promoter_pledge": "pledge",
    "fii_holding": "fii", "dii_holding": "dii", "fii_change": "fii_chg", "dii_change": "dii_chg",
    "institutional_change": "inst_chg", "high_52w": "high52", "low_52w": "low52",
    "off_high": "down_from_high", "down_from_52w_high": "down_from_high",
    "return_1y": "ret_1y", "ret_1yr": "ret_1y", "return_6m": "ret_6m", "return_3m": "ret_3m",
    "rs": "rs_pct", "rs_rank": "rs_pct", "free_cash_flow": "fcf", "networth": "net_worth",
    "bv": "book_value", "interest_coverage": "int_cov", "roce_avg_5y": "roce_5y", "roe_avg_5y": "roe_5y",
}
TEXT_FIELDS = ("name", "symbol", "industry")
# Rules on these fields are skipped for banks/NBFCs/insurers: leverage is their raw material, and
# ROCE/free cash flow/interest cover are not defined the way they are for an operating company.
FIN_SKIP = {"de", "roce", "roce_3y", "roce_5y", "fcf", "int_cov", "opm"}
FIN_NOTE = ("financial: debt, ROCE and free-cash-flow rules skipped; judge it on ROA (> 1.2%), GNPA/NNPA, "
            "NIM and capital adequacy (> 15%) with fundamental-screening")
_FIN_INDUSTRY = ("bank", "nbfc", "non banking", "financ", "insurance", "microfin", "housing fin", "lending")
_FIN_NAME = ("bank", "finance", "finserv", "fincorp", "financial", "insurance", "housing fin", "microfin")


def resolve_field(raw: str):
    f = re.sub(r"[^a-z0-9]+", "_", str(raw).strip().lower()).strip("_")
    if f in FIELDS:
        return f
    return ALIASES.get(f)


# ---------------------------------------------------------------------------
# Header mapping
# ---------------------------------------------------------------------------
_YEARS = re.compile(r"(?<![a-z0-9])(\d{1,2}) ?(?:years?|yrs?|y)(?![a-z0-9])")
_MONTHS = re.compile(r"(?<![a-z0-9])(\d{1,2}) ?(?:months?|mths?|mth|mo|m)(?![a-z0-9])")
_NAME_H = {"name", "company", "company name", "stock", "stock name", "security", "security name", "scrip"}
_SYMBOL_H = {"symbol", "ticker", "nse code", "nse symbol", "bse code", "code", "tradingsymbol", "trading symbol",
             "scrip code", "nse", "isin"}
_PE_H = {"p e", "pe", "pe ratio", "p e ratio", "price to earning", "price to earnings", "price earning",
         "price earnings", "stock p e", "pe ttm", "p e ttm"}
_PB_H = {"p b", "pb", "p bv", "pbv", "cmp bv", "price to book value", "price to book", "price book", "p b ratio"}
_CMP_H = {"cmp", "cmp rs", "current price", "price", "ltp", "close", "last price", "current price rs", "price rs"}
_SKIP_H = {"s no", "sno", "sr no", "sl no", "sr", "no", "", "s n"}


def norm(h) -> str:
    """'Mar Cap Rs.Cr.' -> 'mar cap rs cr'; 'Debt / Eq' -> 'debt eq'; 'profit_growth_3y' -> 'profit growth 3y'."""
    return re.sub(r"[^a-z0-9]+", " ", str(h or "").lower()).strip()


def classify(header: str):
    """Map one CSV header to a canonical field name, or None when it is not one we use."""
    h = norm(header)
    if h in _SKIP_H:
        return None
    if h in _NAME_H:
        return "name"
    if h in _SYMBOL_H:
        return "symbol"
    direct = resolve_field(h)
    if direct:
        return direct
    t = set(h.split())
    ym, mm = _YEARS.search(h), _MONTHS.search(h)
    yrs = int(ym.group(1)) if ym else None
    mon = int(mm.group(1)) if mm else None
    chg = bool(t & {"chg", "change", "changes"})
    growth = bool(t & {"growth", "var", "cagr", "gr", "grth", "variation"})
    qtr = bool(t & {"qtr", "quarter", "quarterly", "quarters"})
    profit = bool(t & {"profit", "profits", "np", "pat", "earnings"})
    sales = bool(t & {"sales", "revenue", "revenues"})

    if "industry" in t and (t & {"pe", "p"}):
        return "industry_pe"
    if t & {"industry", "sector", "industries"}:
        return "industry"
    if any(w.startswith("pledg") for w in t):
        return "pledge"
    if t & {"promoter", "promoters", "prom"}:
        return "promoter_chg" if chg else "promoter"
    if t & {"fii", "fiis", "fpi", "fpis", "foreign"}:
        return "fii_chg" if chg else "fii"
    if t & {"dii", "diis", "domestic"}:
        return "dii_chg" if chg else "dii"
    if "mcap" in t or ("cap" in t and t & {"mar", "mkt", "market"}) or any(w.startswith("capitali") for w in t):
        return "mcap"
    if "peg" in t:
        return "peg"
    if h in _PB_H or ("book" in t and t & {"price", "cmp"}) or {"cmp", "bv"} <= t:
        return "pb"
    if "book" in t or h in ("bv", "b v", "b v rs", "bv rs"):
        return "book_value"
    if h in _PE_H:
        return "pe"
    if "payout" in t:
        return "payout"
    if t & {"div", "dividend", "dividends"}:
        if t & {"yld", "yield"}:
            return "div_yield"
        if growth:
            return "div_growth"
        if t & {"preceding", "previous", "prev"}:
            return "div_prev"
        if "last" in t:
            return "div_last"
        return None
    if "rsi" in t:
        return "rsi"
    if "roce" in t or "return on capital" in h:
        return {None: "roce", 3: "roce_3y", 5: "roce_5y"}.get(yrs)
    if "roe" in t or "return on equity" in h:
        return {None: "roe", 3: "roe_3y", 5: "roe_5y"}.get(yrs)
    if "roa" in t or "return on assets" in h:
        return "roa" if yrs is None else None
    if "debt" in t and t & {"eq", "equity"}:
        return "de"
    if "eps" in t:
        if growth:
            return "eps_g3" if yrs == 3 else None
        return "eps" if not qtr else None
    if growth and (profit or sales) and "qoq" not in t:
        stem = "profit" if profit else "sales"
        if qtr:
            return f"qtr_{stem}_var"
        return {None: f"{stem}_g1", 1: f"{stem}_g1", 3: f"{stem}_g3", 5: f"{stem}_g5"}.get(yrs)
    if qtr and profit and "qoq" not in t:
        return "np_qtr"
    if qtr and sales and "qoq" not in t:
        return "sales_qtr"
    if t & {"52w", "52wk", "52week"} or ("52" in t and t & {"w", "wk", "week", "weeks"}):
        if "down" in t:
            return "down_from_high"
        if "from" in t:
            return None                      # Screener's "From 52w high" is a ratio, not a percentage
        if "high" in t:
            return "high52"
        if "low" in t:
            return "low52"
        return None
    if t & {"return", "returns", "ret"}:
        if yrs == 1 or mon == 12:
            return "ret_1y"
        if mon == 6:
            return "ret_6m"
        if mon == 3:
            return "ret_3m"
        return None
    if "fcf" in t or "free cash flow" in h:
        return "fcf"
    if "net worth" in h or "networth" in t:
        return "net_worth"
    if "interest" in t and any(w.startswith("cov") for w in t):
        return "int_cov"
    if "opm" in t:
        return "opm"
    if h in _CMP_H:
        return "cmp"
    return None


def to_num(raw, where: str):
    """'1,23,456.70' / '45.1%' / '(512.3)' / '₹ 1,480' / '1.2x' -> float; blank, '-' or NA -> None."""
    if raw is None:
        return None
    s = str(raw).strip().replace("−", "-").replace("–", "-").replace(",", "").replace("₹", "")
    s = re.sub(r"(?i)^rs\.?\s*", "", s).replace("%", "").strip()
    if s.lower() in ("", "-", "--", "—", "na", "n/a", "n.a.", "nil", "null", "none", "nan", "#n/a"):
        return None
    neg = s.startswith("(") and s.endswith(")")
    if neg:
        s = s[1:-1].strip()
    if s[-1:] in ("x", "X") and len(s) > 1:
        s = s[:-1]
    try:
        x = float(s)
    except ValueError:
        raise ScreenError(f"{where}: not a number: {str(raw).strip()!r}")
    if not math.isfinite(x):
        return None
    return -x if neg else x


def parse_maps(items) -> dict:
    """--map "field=Header Name" (repeatable) -> {canonical_field: header text}."""
    out = {}
    for item in items or []:
        left, sep, right = str(item).partition("=")
        f = left.strip().lower()
        field = f if f in TEXT_FIELDS else resolve_field(f)
        if not sep or not right.strip() or not field:
            raise ScreenError(f"--map {item!r}: expected FIELD=Header Name with a known field "
                              "(run `fields` for the list)")
        out[field] = right.strip()
    return out


def load_companies(path, maps: dict | None = None) -> dict:
    """Read the fundamentals CSV. Returns {companies, columns{field: header}, unmapped, notes, file}."""
    p = Path(path)
    if not p.is_file():
        raise ScreenError(f"{path}: file not found")
    with open(p, "rb") as fh:
        if fh.read(2) == b"PK":
            raise ScreenError(f"{p.name}: this is an Excel workbook; open it and save the sheet as CSV first")
    with open(p, newline="", encoding="utf-8-sig", errors="replace") as fh:
        raw = [r for r in csv.reader(fh)]
    raw = [[c.strip() for c in r] for r in raw if any(c.strip() for c in r)]
    if not raw:
        raise ScreenError(f"{p.name}: empty file")
    hi = next((i for i, r in enumerate(raw[:10])
               if sum(1 for c in r if classify(c)) >= 2 and any(classify(c) in ("name", "symbol") for c in r)), None)
    if hi is None:
        raise ScreenError(f"{p.name}: no header row with a Name/Symbol column and at least one recognised "
                          f"metric in the first 10 lines (first line was {raw[0][:6]})")
    header = raw[hi]
    notes, columns, col_idx, unmapped = [], {}, {}, []
    forced = {}
    for field, want in (maps or {}).items():
        idx = next((i for i, c in enumerate(header) if norm(c) == norm(want)), None)
        if idx is None:
            raise ScreenError(f"--map {field}={want}: no column named {want!r} in {p.name} "
                              f"(columns: {', '.join(c for c in header if c)})")
        forced[idx] = field
    for i, c in enumerate(header):
        if not c:
            continue
        field = forced.get(i) or (None if classify(c) in forced.values() else classify(c))
        if field is None:
            if norm(c) not in _SKIP_H:
                unmapped.append(c)
            continue
        if field in col_idx:
            notes.append(f"column {c!r} also maps to {field}; kept {columns[field]!r}")
            continue
        col_idx[field], columns[field] = i, c
    if "name" not in col_idx and "symbol" not in col_idx:
        raise ScreenError(f"{p.name}: need a Name or Symbol column")
    header_norm = [norm(c) for c in header]
    companies, seen = [], set()
    for n, r in enumerate(raw[hi + 1:], start=hi + 2):
        if [norm(c) for c in r] == header_norm:
            continue                                   # header repeated when pages are pasted together
        cell = lambda f: (r[col_idx[f]] if f in col_idx and col_idx[f] < len(r) else "")
        name = cell("name") or cell("symbol")
        if not name or norm(name).startswith(("median", "total", "average")):
            continue
        key = norm(cell("symbol") or name)
        if key in seen:
            notes.append(f"duplicate row for {name!r} ignored")
            continue
        seen.add(key)
        vals = {}
        for f, i in col_idx.items():
            if f in TEXT_FIELDS:
                continue
            vals[f] = to_num(r[i] if i < len(r) else "", f"{p.name} row {n} ({name}), column {columns[f]!r}")
        companies.append({"name": name, "symbol": cell("symbol") or None, "industry": cell("industry") or None,
                          "v": vals, "financial": False, "fin_source": None})
    if not companies:
        raise ScreenError(f"{p.name}: header found but no company rows")
    return {"companies": companies, "columns": columns, "unmapped": unmapped, "notes": notes, "file": p.name}


# ---------------------------------------------------------------------------
# Derived fields, financial tagging, RSI from price files
# ---------------------------------------------------------------------------
def mark_financials(data: dict, mode: str = "auto") -> None:
    mode = (mode or "auto").strip()
    low = mode.lower()
    forced = set() if low in ("auto", "none") else {norm(x) for x in mode.split(",") if x.strip()}
    has_industry = any(c["industry"] for c in data["companies"])
    hits = set()
    for c in data["companies"]:
        if norm(c["name"]) in forced or (c["symbol"] and norm(c["symbol"]) in forced):
            c["financial"], c["fin_source"] = True, "--financials"
            hits.add(norm(c["name"]))
            if c["symbol"]:
                hits.add(norm(c["symbol"]))
        elif low == "none":
            continue
        elif has_industry:
            ind = norm(c["industry"])
            if any(k in ind for k in _FIN_INDUSTRY):
                c["financial"], c["fin_source"] = True, "industry column"
        elif any(k in norm(c["name"]) for k in _FIN_NAME):
            c["financial"], c["fin_source"] = True, "name (no industry column)"
    missing = sorted(forced - hits)
    if missing:
        raise ScreenError(f"--financials: no company named {', '.join(missing)} in {data['file']}")
    if low != "none" and not has_industry and any(c["financial"] for c in data["companies"]):
        data["notes"].append("no Industry column: banks/NBFCs/insurers guessed from the company name; add the "
                             "column or pass --financials NAME,NAME / --financials none")


def _ta():
    name = "_stock_screener_dep_ta"
    if name in sys.modules:
        return sys.modules[name]
    path = SKILLS_DIR / "technical-analysis" / "scripts" / "ta.py"
    if not path.is_file():
        raise ScreenError(f"--prices-dir needs {path} (the technical-analysis skill)")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _keys(c: dict) -> list:
    """File-name stems a company's price CSV may carry: symbol, name, name without Ltd/Limited."""
    squash = lambda s: re.sub(r"[^A-Z0-9]+", "", str(s).upper())
    out = [squash(c["symbol"])] if c["symbol"] else []
    out.append(squash(c["name"]))
    out.append(squash(re.sub(r"(?i)\b(ltd|limited|ltd\.)\s*$", "", c["name"].strip())))
    return [k for k in dict.fromkeys(out) if k]


def apply_prices(data: dict, prices_dir: str) -> None:
    """Set v['rsi'] from daily closes in prices_dir (file name = symbol or company name)."""
    d = Path(prices_dir)
    if not d.is_dir():
        raise ScreenError(f"--prices-dir {prices_dir} is not a folder")
    files = {re.sub(r"[^A-Z0-9]+", "", f.stem.upper()): f for f in sorted(d.glob("*.csv")) if f.is_file()}
    if not files:
        raise ScreenError(f"--prices-dir {prices_dir}: no .csv files")
    ta = _ta()
    got, missing, bad, last = 0, [], [], None
    for c in data["companies"]:
        c["v"]["rsi"] = None
        f = next((files[k] for k in _keys(c) if k in files), None)
        if f is None:
            missing.append(c["name"])
            continue
        try:
            rows = ta.load_csv(str(f))
            val = ta.rsi([r["close"] for r in rows], 14)[-1] if rows else None
        except (ValueError, OSError, KeyError, UnicodeDecodeError, csv.Error) as e:
            bad.append(f"{f.name}: {e}")
            continue
        if val is None:
            bad.append(f"{f.name}: fewer than 15 daily bars")
            continue
        c["v"]["rsi"] = float(val)
        got += 1
        day = rows[-1]["date"].date()
        last = day if last is None or day > last else last
    note = f"RSI(14) from daily closes in {d.name}/ for {got} of {len(data['companies'])} companies"
    if last:
        note += f" (latest bar {last.strftime('%d-%b-%Y')})"
    data["notes"].append(note)
    if missing:
        data["notes"].append("no price file for: " + ", ".join(missing[:8]) + (" …" if len(missing) > 8 else "")
                             + " (name the file after the Symbol or Name column)")
    for b in bad[:5]:
        data["notes"].append(f"price file skipped: {b}")


def derive(data: dict) -> None:
    """Fill fields the file does not carry but that follow from the ones it does."""
    cos = data["companies"]
    for c in cos:
        v = c["v"]
        g = v.get
        if g("down_from_high") is None and g("cmp") is not None and g("high52"):
            v["down_from_high"] = max(0.0, (1 - g("cmp") / g("high52")) * 100)
        if g("pb") is None and g("cmp") is not None and g("book_value"):
            v["pb"] = g("cmp") / g("book_value")
        if g("peg") is None and g("pe") is not None and (g("profit_g3") or 0) > 0:
            v["peg"] = g("pe") / g("profit_g3")
        if g("div_growth") is None and g("div_last") is not None and (g("div_prev") or 0) > 0:
            v["div_growth"] = (g("div_last") / g("div_prev") - 1) * 100
        parts = [x for x in (g("fii_chg"), g("dii_chg")) if x is not None]
        if g("inst_chg") is None and parts:
            v["inst_chg"] = sum(parts)
    rets = sorted(c["v"]["ret_1y"] for c in cos if c["v"].get("ret_1y") is not None)
    n = len(rets)
    for c in cos:
        r = c["v"].get("ret_1y")
        if r is None or n < 2:
            c["v"]["rs_pct"] = None
            continue
        below = sum(1 for x in rets if x < r)
        ties = sum(1 for x in rets if x == r)
        c["v"]["rs_pct"] = float(round(1 + 98 * (below + (ties - 1) / 2) / (n - 1)))
    data["rs_universe"] = n
    data["available"] = {f for f in FIELDS if any(c["v"].get(f) is not None for c in cos)}


def prepare(a) -> dict:
    data = load_companies(a.csv, parse_maps(getattr(a, "map", None)))
    mark_financials(data, getattr(a, "financials", "auto"))
    if getattr(a, "prices_dir", None):
        apply_prices(data, a.prices_dir)
    derive(data)
    return data


# ---------------------------------------------------------------------------
# Rules, the --where filter language, presets
# ---------------------------------------------------------------------------
OPS = {
    ">": lambda a, b: a > b, ">=": lambda a, b: a >= b, "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b, "==": lambda a, b: a == b, "!=": lambda a, b: a != b,
}
OP_TEXT = {">": ">", ">=": "≥", "<": "<", "<=": "≤", "==": "=", "!=": "≠"}


@dataclass(frozen=True)
class Rule:
    tag: str                     # short id shown in output
    field: str
    op: str
    value: float
    weight: float = 0.0          # share of the composite score (0 = pass/fail only)
    best: float | None = None    # value that earns full marks; None = pass/fail scoring
    why: str = ""
    q: str | None = None         # Screener.in query text override ("" = cannot be expressed)
    qnote: str = ""
    user: bool = False           # a --where filter: applied to banks/NBFCs too (no financial exemption)

    @property
    def text(self) -> str:
        return f"{FIELDS[self.field].label} {OP_TEXT[self.op]} {fmt_threshold(self.field, self.value)}"

    def passes(self, v: float) -> bool:
        return OPS[self.op](v, self.value)

    def score(self, v: float) -> float:
        if self.best is None or self.best == self.value or self.op in ("==", "!="):
            return 100.0 if self.passes(v) else 0.0
        return max(0.0, min(100.0, 50 + 50 * (v - self.value) / (self.best - self.value)))


_WHERE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_]*)\s*(>=|<=|==|!=|>|<|=)\s*([+-]?(?:\d+\.?\d*|\.\d+))\s*%?\s*$")


def parse_where(items) -> list:
    """Each item is FIELD OP NUMBER (several may be joined with 'and'). Matched by regex; never evaluated."""
    rules = []
    for item in items or []:
        for part in re.split(r"(?i)\s+and\s+|\s*&&\s*", str(item).strip()):
            m = _WHERE.match(part)
            if not m:
                raise ScreenError(f"--where {part!r}: expected FIELD OP NUMBER, e.g. \"roce>20\" "
                                  "(operators > >= < <= == !=)")
            field = resolve_field(m.group(1))
            if field is None:
                raise ScreenError(f"--where {part!r}: unknown field {m.group(1)!r} (run `fields` for the list)")
            op = "==" if m.group(2) == "=" else m.group(2)
            rules.append(Rule(tag=field, field=field, op=op, value=float(m.group(3)), why="your filter", user=True))
    return rules


def yield_bar(cfg: dict) -> float:
    return round(cfg["gsec"] * cfg["gsec_share"], 1)


def build_presets(cfg: dict) -> dict:
    bar = yield_bar(cfg)
    R = Rule
    return {
        "canslim-india": {
            "title": "CANSLIM adapted for India",
            "idea": "Growth leaders: earnings accelerating now, a multi-year growth record, price near its "
                    "highs, outperforming, institutions adding.",
            "rules": [
                R("C", "qtr_profit_var", ">=", 25, 20, 60, "latest quarter's net profit up 25%+ YoY"),
                R("C-sales", "qtr_sales_var", ">=", 15, 10, 35,
                  "sales must back the profit; filters other-income and one-off driven quarters"),
                R("A", "profit_g3", ">=", 20, 15, 40, "3-year profit CAGR about twice nominal GDP growth"),
                R("A-roe", "roe", ">=", 15, 10, 25, "growth earned on equity, not bought with dilution"),
                R("N", "down_from_high", "<=", 15, 15, 3, "within 15% of the 52-week high"),
                R("L", "rs_pct", ">=", 70, 20, 95, "1-year return in the top 30% of the file: leader, not laggard",
                  q="Return over 1year > 20",
                  qnote="L uses a percentile inside your file; `Return over 1year > 20` is a stand-in, set it "
                        "about 10 points above NIFTY 500's own 1-year return"),
                R("I", "inst_chg", ">", 0, 10, 2, "FII + DII holding rose over the last quarter"),
            ],
            "notes": [
                "M (market direction) is not in a fundamentals file: run ../market-regime/scripts/regime.py "
                "status and buy leaders only when it allows new longs.",
                "S (supply/demand) is a price-volume read: check passers with momentum-scanner and "
                "breakout-planner.",
                "No free consensus estimates in India, so C uses reported YoY growth, not an earnings surprise.",
            ],
        },
        "value-dividend": {
            "title": "Value with dividend income",
            "idea": "Cheap enough, pays a real yield out of profits it can sustain, little debt, profits not "
                    "shrinking.",
            "rules": [
                R("PE", "pe", "<=", 20, 15, 10, "at or below NIFTY's long-run P/E of about 20-22"),
                R("PB", "pb", "<=", 3, 10, 1, "below the index's usual 3-3.5x book"),
                R("Yield", "div_yield", ">=", bar, 25, round(bar * 2, 1),
                  f"{cfg['gsec_share']:.0%} of the {cfg['gsec']:g}% 10-year G-sec, about twice NIFTY's own yield"),
                R("Payout", "payout", "<=", 75, 15, 40, "dividend covered by profit with room to spare"),
                R("Debt", "de", "<=", 0.5, 15, 0, "same debt/equity line as fundamental-screening"),
                R("Growth", "profit_g3", ">=", 5, 10, 15, "profits at least keep pace with inflation: not a yield trap"),
                R("ROE", "roe", ">=", 12, 10, 20, "earns roughly its cost of equity"),
            ],
            "notes": [
                "Dividends are taxed at your slab rate; the company deducts 10% TDS once it pays you more than "
                "₹10,000 in a financial year. A 3% yield is about 2.1% after tax in the 30% slab.",
                "PSU yields follow government payout guidelines and commodity cycles: check that the last "
                "dividend was not a one-off special.",
            ],
        },
        "dividend-growth-pullback": {
            "title": "Dividend growers on a pullback",
            "idea": "Quality companies raising dividends, bought when RSI(14) says the price has pulled back.",
            "rules": [
                R("Yield", "div_yield", ">=", 1.5, 15, 3.5, "at or above NIFTY's own yield of about 1.2-1.5%"),
                R("DivGrowth", "div_growth", ">=", 10, 20, 20, "dividend growing faster than nominal GDP",
                  q="", qnote="dividend growth has no verified Screener.in field; add it as your own column"),
                R("Growth", "profit_g3", ">=", 12, 15, 20, "dividends can only grow as fast as profits"),
                R("Payout", "payout", "<=", 60, 10, 30, "room left to raise the dividend"),
                R("ROE", "roe", ">=", 15, 15, 25, "quality bar from fundamental-screening"),
                R("Debt", "de", "<=", 0.5, 10, 0, "dividends are not funded by borrowing"),
                R("Pullback", "rsi", "<=", 40, 15, 30, "RSI(14) at or below 40: a pullback, not a breakdown call"),
            ],
            "notes": [
                "Pullback needs --prices-dir (daily CSVs named after the symbol or company) or an RSI column; "
                "without either the rule is left out and the screen is only the quality half.",
                "An RSI of 40 in a stock below its 200 DMA is a downtrend, not a pullback: confirm the trend "
                "with ../technical-analysis/scripts/ta.py before acting.",
            ],
        },
        "garp": {
            "title": "Growth at a reasonable price",
            "idea": "Pay for growth, but not any price: PEG, profit and sales growth, return on capital.",
            "rules": [
                R("PEG", "peg", "<=", 1.5, 25, 0.75, "P/E no more than 1.5x the growth rate"),
                R("PEG>0", "peg", ">", 0, 5, None, "a negative PEG means shrinking or negative earnings"),
                R("Growth", "profit_g3", ">=", 15, 20, 30, "3-year profit CAGR well above nominal GDP"),
                R("Sales", "sales_g3", ">=", 10, 15, 20, "top line growing at least with nominal GDP"),
                R("ROCE", "roce", ">=", 18, 20, 30, "growth that earns more than its cost of capital"),
                R("ROE", "roe", ">=", 15, 10, 25, "returns test that also works for banks and NBFCs"),
                R("PE", "pe", "<=", 40, 10, 20, "a ceiling: Indian growth names trade rich, 40x is the limit"),
            ],
            "notes": [
                "PEG here is trailing (P/E over 3-year profit growth). Forward PEG needs analyst estimates, "
                "which are not freely available for Indian stocks.",
            ],
        },
        "quality-compounder": {
            "title": "Quality compounders",
            "idea": "High returns on capital, almost no debt, steady growth, committed promoters with "
                    "nothing pledged.",
            "rules": [
                R("ROCE", "roce", ">=", 20, 20, 35, "well above the 15% quality line in fundamental-screening"),
                R("ROE", "roe", ">=", 18, 15, 28, "high returns without leverage doing the work"),
                R("Debt", "de", "<=", 0.3, 10, 0, "near debt-free"),
                R("Sales", "sales_g3", ">=", 10, 10, 20, "sales growing at least with nominal GDP"),
                R("Growth3", "profit_g3", ">=", 12, 10, 22, "profit growth ahead of sales"),
                R("Growth5", "profit_g5", ">=", 12, 10, 22, "and the same over 5 years: consistency, not one cycle"),
                R("Promoter", "promoter", ">=", 40, 10, 60, "owner-operators with real skin in the game"),
                R("Pledge", "pledge", "<=", 1, 10, 0, "pledge about zero"),
                R("FCF", "fcf", ">", 0, 5, None, "profits turn into cash"),
            ],
            "notes": [
                "Board-run companies with no promoter (ITC, L&T, some private banks) fail the Promoter rule by "
                "design: screen them with --where instead.",
                "Quality is not a price: a compounder at 80x earnings can still lose money for years. Read "
                "valuation with fundamental-screening.",
            ],
        },
    }


def get_rules(a, cfg: dict) -> tuple:
    """(preset name or None, preset dict or None, rules)."""
    presets = build_presets(cfg)
    name = getattr(a, "preset", None)
    preset = None
    if name:
        preset = presets.get(name.strip().lower())
        if preset is None:
            raise ScreenError(f"unknown preset {name!r}; choose from {', '.join(presets)}")
        name = name.strip().lower()
    rules = list(preset["rules"]) if preset else []
    rules += parse_where(getattr(a, "where", None))
    if not rules:
        raise ScreenError("give --preset NAME and/or --where \"field>number\" (run `presets` to list the screens)")
    return name, preset, rules


# ---------------------------------------------------------------------------
# Gates, evaluation, ranking
# ---------------------------------------------------------------------------
def gate_reasons(c: dict, cfg: dict, avail: set) -> list:
    v, out = c["v"], []
    if "mcap" in avail:
        if v.get("mcap") is None:
            out.append("market cap missing")
        elif v["mcap"] < cfg["min_mcap"]:
            out.append(f"market cap {crore(v['mcap'])} below the {crore(cfg['min_mcap'])} floor (micro-cap/SME risk)")
    if v.get("pledge") is not None and v["pledge"] > cfg["max_pledge"]:
        out.append(f"promoter pledge {v['pledge']:.1f}% above {cfg['max_pledge']:g}%")
    for f, what in (("net_worth", "net worth"), ("book_value", "book value"), ("pb", "P/B"), ("de", "debt/equity")):
        if v.get(f) is not None and v[f] < 0:
            out.append(f"negative net worth ({what} {fmt(f, v[f])})")
            break
    return out


def soft_flags(c: dict, cfg: dict) -> list:
    v, out = c["v"], []
    if c["financial"]:
        out.append("FIN")
    if v.get("pledge") is not None and cfg["pledge_watch"] <= v["pledge"] <= cfg["max_pledge"]:
        out.append(f"pledge {v['pledge']:.0f}%")
    if v.get("mcap") is not None and v["mcap"] < cfg["small_cap"]:
        out.append("small-cap")
    if v.get("qtr_profit_var") is not None and v["qtr_profit_var"] >= cfg["low_base"]:
        out.append("low-base?")
    return out


def judge(c: dict, rules: list, cfg: dict, avail: set) -> dict:
    checks, wsum, ssum = [], 0.0, 0.0
    for r in rules:
        v = c["v"].get(r.field)
        sc = None
        if r.field not in avail:
            status = "no-column"
        elif c["financial"] and r.field in FIN_SKIP and not r.user:
            status = "skipped"
        elif v is None:
            status = "na"
        else:
            status = "pass" if r.passes(v) else "fail"
            sc = r.score(v)
            if r.weight > 0:
                wsum += r.weight
                ssum += r.weight * sc
        checks.append({"rule": r.tag, "field": r.field, "text": r.text, "value": v, "status": status,
                       "score": sc, "weight": r.weight})
    gates = [] if cfg["no_gates"] else gate_reasons(c, cfg, avail)
    fails = [k for k in checks if k["status"] == "fail"]
    missing = [k for k in checks if k["status"] == "na"]
    judged = sum(1 for k in checks if k["status"] in ("pass", "fail"))
    if gates:
        bucket = "gated"
    elif (missing and not cfg["no_gates"]) or (judged == 0 and any(k["status"] == "na" for k in checks)):
        bucket = "incomplete"
    elif not fails:
        bucket = "passed"
    elif len(fails) == 1:
        bucket = "near_miss"
    else:
        bucket = "failed"
    return {"name": c["name"], "symbol": c["symbol"], "industry": c["industry"], "financial": c["financial"],
            "mcap": c["v"].get("mcap"), "price": c["v"].get("cmp"),
            "score": (ssum / wsum) if wsum else None, "bucket": bucket, "checks": checks,
            "passed_rules": sum(1 for k in checks if k["status"] == "pass"),
            "judged_rules": sum(1 for k in checks if k["status"] in ("pass", "fail")),
            "failed": [k["rule"] for k in fails], "missing": [k["field"] for k in missing],
            "gates": gates, "flags": soft_flags(c, cfg), "values": c["v"]}


def screen(data: dict, rules: list, cfg: dict, sort: str | None = None, asc: bool = False) -> dict:
    avail = data["available"]
    judged = [judge(c, rules, cfg, avail) for c in data["companies"]]
    scored = any(r.weight > 0 and r.field in avail for r in rules)
    if sort and sort != "score":
        key_field = resolve_field(sort)
        if key_field is None:
            raise ScreenError(f"--sort {sort!r}: unknown field (run `fields` for the list)")
    elif sort == "score" or scored:
        if not scored:
            raise ScreenError("--sort score needs a preset; with only --where filters sort by a field")
        key_field = None
    else:
        first = next((r for r in rules if r.field in avail), rules[0])
        key_field, asc = first.field, (first.op in ("<", "<=")) if not asc else asc

    def key(j):
        x = j["score"] if key_field is None else j["values"].get(key_field)
        return (x is None, (x if asc else -x) if x is not None else 0.0, -(j["mcap"] or 0.0), j["name"])

    buckets = {b: sorted((j for j in judged if j["bucket"] == b), key=key)
               for b in ("passed", "near_miss", "failed", "gated", "incomplete")}
    for i, j in enumerate(buckets["passed"], 1):
        j["rank"] = i
    notes = list(data["notes"])
    absent = [r for r in rules if r.field not in avail]
    for r in absent:
        f = FIELDS[r.field]
        hint = f.derived or (f"add the column in Screener.in (Edit Columns → {f.query})" if f.query else "add a column")
        notes.append(f"rule {r.tag} ({r.text}) NOT CHECKED: no {r.field} data in the file; {hint}"
                     + (f", or --map {r.field}=\"Your Header\"" if not f.derived else ""))
    if not cfg["no_gates"]:
        if "mcap" not in avail:
            notes.append("market-cap floor NOT CHECKED: no market-cap column")
        if "pledge" not in avail:
            notes.append("pledge gate NOT CHECKED: no pledge column (Screener.in field: Pledged percentage)")
    if any(r.field == "rs_pct" for r in rules) and "rs_pct" in avail and data.get("rs_universe", 0) < 30:
        notes.append(f"RS percentile ranks only the {data['rs_universe']} companies in this file; export a broad "
                     "universe (NIFTY 500 or wider) for it to mean leadership")
    if any(j["financial"] and j["bucket"] in ("passed", "near_miss") for j in judged):
        notes.append("FIN = " + FIN_NOTE)
    return {"rules": rules, "absent": [r.tag for r in absent], "sort": "score" if key_field is None else key_field,
            "ascending": bool(asc) if key_field else False, "notes": notes, **buckets}


def find_company(data: dict, query: str) -> dict:
    q = norm(query)
    if not q:
        raise ScreenError("explain needs a company name or symbol")
    cos = data["companies"]
    exact = [c for c in cos if (c["symbol"] and norm(c["symbol"]) == q) or norm(c["name"]) == q]
    if len(exact) == 1:
        return exact[0]
    part = exact or [c for c in cos if norm(c["name"]).startswith(q)] or [c for c in cos if q in norm(c["name"])]
    if len(part) == 1:
        return part[0]
    if not part:
        raise ScreenError(f"{query!r} not found in {data['file']} ({len(cos)} companies; names come from the "
                          "Name/Symbol column)")
    raise ScreenError(f"{query!r} matches several companies: {', '.join(c['name'] for c in part[:6])}")


# ---------------------------------------------------------------------------
# Screener.in query text
# ---------------------------------------------------------------------------
_Q_OP = {">": ">", ">=": ">", "<": "<", "<=": "<", "==": "="}


def to_query(rules: list, cfg: dict) -> dict:
    lines, unverified, notes = [], [], []
    for r in rules:
        f = FIELDS[r.field]
        if r.q is not None:
            if r.q:
                lines.append(r.q)
            if r.qnote:
                notes.append(r.qnote)
            if r.q and r.q.startswith("Return over 1year"):
                unverified.append("Return over 1year")
            continue
        if not f.query or r.op not in _Q_OP:
            notes.append(f"rule {r.tag} ({r.text}) has no Screener.in equivalent and is left out")
            continue
        lines.append(f"{f.query} {_Q_OP[r.op]} {r.value:g}")
        if not f.query_ok:
            unverified.append(f.query)
    if not cfg["no_gates"]:
        lines.append(f"Market Capitalization > {cfg['min_mcap']:g}")
        lines.append(f"Pledged percentage < {cfg['max_pledge']:g}")
    seen = []
    for ln in lines:
        if ln not in seen:
            seen.append(ln)
    if any(r.op in (">=", "<=") for r in rules):
        notes.append("≥ and ≤ are written as > and <, the operators seen in public Screener.in screens; "
                     "a value exactly on a threshold can differ")
    notes.append("Screener.in does not apply the bank/NBFC exemption or the negative-net-worth and missing-data "
                 "gates: export the result and run it through `run` for those")
    return {"query": " AND\n".join(seen), "one_line": " AND ".join(seen),
            "unverified_fields": list(dict.fromkeys(unverified)), "notes": notes}


# ---------------------------------------------------------------------------
# Text output
# ---------------------------------------------------------------------------
def _table(headers: list, rows: list, left: int = 2) -> str:
    """First `left` columns left-aligned, the rest right-aligned."""
    widths = [max(len(str(h)), *(len(str(r[i])) for r in rows)) if rows else len(str(h))
              for i, h in enumerate(headers)]
    def line(cells):
        out = []
        for i, c in enumerate(cells):
            out.append(str(c).ljust(widths[i]) if 0 < i < left else str(c).rjust(widths[i]))
        return " " + "  ".join(out).rstrip()
    return "\n".join([line(headers)] + [line(r) for r in rows])


def _short(name: str, n: int = 26) -> str:
    return name if len(name) <= n else name[: n - 1] + "…"


def _gates_line(cfg: dict) -> str:
    if cfg["no_gates"]:
        return "Gates: OFF (--no-gates): missing values are ignored, pledge/market-cap/net-worth not checked"
    return (f"Gates: market cap ≥ {crore(cfg['min_mcap'])} · promoter pledge ≤ {cfg['max_pledge']:g}% · "
            "no negative net worth · no missing data · banks/NBFCs skip debt, ROCE and FCF rules")


def _failed_text(j: dict) -> str:
    bits = []
    for k in j["checks"]:
        if k["status"] == "fail":
            bits.append(f"{k['rule']}: {FIELDS[k['field']].label} {fmt(k['field'], k['value'])} "
                        f"(needs {k['text'].split(FIELDS[k['field']].label + ' ', 1)[-1]})")
    return "; ".join(bits)


def print_presets(presets: dict, cfg: dict) -> None:
    print("Built-in screens (thresholds for Indian equities; run with: screen.py run --csv FILE --preset NAME)")
    for name, p in presets.items():
        print(f"\n{name} — {p['title']}")
        print(f"  {p['idea']}")
        rows = [[r.tag, r.text, f"{r.weight:g}", r.why] for r in p["rules"]]
        for line in _table(["Rule", "Test", "Wt", "Why"], rows, left=2).splitlines():
            print(" " + line)
        for n in p["notes"]:
            print(f"  - {n}")
    print(f"\n{_gates_line(cfg)}")
    print("Score: 50 at the threshold, 100 at the rule's best level, linear and capped; composite = "
          "weight-averaged rule score.")
    print(DISCLAIMER)


def print_fields(data: dict | None) -> None:
    print("Fields for --where / --sort / --map (Screener.in header and query name; * = verified on public "
          "Screener.in pages, others assumed)")
    rows = []
    for f, d in FIELDS.items():
        mapped = "" if data is None else (data["columns"].get(f) or ("(derived)" if f in data["available"] else "—"))
        rows.append([f, d.label, (d.header + ("*" if d.header_ok else "")) or "—",
                     (d.query + ("*" if d.query_ok else "")) or "—"] + ([mapped] if data else []))
    heads = ["Field", "Meaning", "Screener header", "Screener query name"] + (["In your file"] if data else [])
    print(_table(heads, rows, left=5))
    if data:
        print(f"\n{data['file']}: {len(data['companies'])} companies, {len(data['columns'])} columns mapped")
        if data["unmapped"]:
            print("Not mapped (use --map FIELD=\"Header\" if one of these is a field above): "
                  + ", ".join(data["unmapped"]))
        fins = [c["name"] for c in data["companies"] if c["financial"]]
        if fins:
            print("Treated as financials: " + ", ".join(fins))
        for n in data["notes"]:
            print(f"Note: {n}")


def print_run(res: dict, data: dict, name, preset, cfg: dict, top: int) -> None:
    title = f"{name} ({preset['title']})" if preset else "custom filters"
    print(f"Stock screen — {title}   file: {data['file']}   {len(data['companies'])} companies")
    print("Rules: " + " · ".join(("" if r.field in data["available"] else "[not checked] ") + f"{r.tag}: {r.text}"
                                 for r in res["rules"]))
    print(_gates_line(cfg))
    order = "composite score" if res["sort"] == "score" else \
        f"{FIELDS[res['sort']].label} ({'low to high' if res['ascending'] else 'high to low'})"
    cols = list(dict.fromkeys(r.field for r in res["rules"] if r.field in data["available"]))
    if res["sort"] != "score" and res["sort"] not in cols and res["sort"] in data["available"]:
        cols.append(res["sort"])
    passed = res["passed"]
    skipped_fields = {r.field for r in res["rules"] if not r.user} & FIN_SKIP
    show_score = any(j["score"] is not None for j in passed)
    print(f"\nPASSED ({len(passed)}) — ranked by {order}")
    if passed:
        rows = []
        for j in passed[:top]:
            rows.append([j["rank"], _short(j["name"])]
                        + (["—" if j["score"] is None else f"{j['score']:.1f}"] if show_score else [])
                        + [fmt("mcap", j["mcap"], short=True)]
                        + [("skip" if j["financial"] and f in skipped_fields else fmt(f, j["values"].get(f), short=True))
                           for f in cols]
                        + [" ".join(j["flags"])])
        print(_table(["#", "Company"] + (["Score"] if show_score else []) + ["Mcap ₹cr"]
                     + [FIELDS[f].col for f in cols] + ["Flags"], rows))
        if len(passed) > top:
            print(f" … {len(passed) - top} more (raise --top)")
    else:
        print(" none")
    near = res["near_miss"]
    print(f"\nNEAR MISSES — failed exactly one rule ({len(near)})")
    for j in near[:top]:
        sc = "" if j["score"] is None else f"  score {j['score']:.1f}"
        print(f" {_short(j['name'])}{sc}  failed {_failed_text(j)}")
    if not near:
        print(" none")
    if res["gated"]:
        print(f"\nGATED OUT — red flags ({len(res['gated'])})")
        for j in res["gated"][:top]:
            print(f" {_short(j['name'])}: {'; '.join(j['gates'])}")
        if len(res["gated"]) > top:
            print(f" … {len(res['gated']) - top} more")
    if res["incomplete"]:
        print(f"\nINCOMPLETE DATA ({len(res['incomplete'])}) — cannot be judged; --no-gates ignores blanks")
        groups = {}
        for j in res["incomplete"]:
            groups.setdefault(tuple(dict.fromkeys(j["missing"])), []).append(j["name"])
        for miss, names in list(groups.items())[:top]:
            hint = " (a blank P/E usually means a loss)" if "pe" in miss else ""
            who = ", ".join(_short(n) for n in names[:6]) + (f" and {len(names) - 6} more" if len(names) > 6 else "")
            print(f" {who}: no value for {', '.join(FIELDS[f].label for f in miss)}{hint}")
    print(f"\nFailed two or more rules: {len(res['failed'])}")
    if res["notes"] or (preset and preset["notes"]):
        print("\nNotes")
        for n in res["notes"] + (preset["notes"] if preset else []):
            print(f" - {n}")
    print("\nNext: read each passer's filings and red flags with fundamental-screening; time entries with "
          "breakout-planner or momentum-scanner; check portfolio fit with portfolio-review.")
    print(DISCLAIMER)


def print_explain(j: dict, res: dict, data: dict, name, preset, cfg: dict) -> None:
    title = f"{name} ({preset['title']})" if preset else "custom filters"
    print(f"{j['name']} — {title}")
    bits = [f"Industry: {j['industry']}" if j["industry"] else None,
            f"Market cap {crore(j['mcap'])}" if j["mcap"] is not None else None,
            f"Price {fmt('cmp', j['price'])}" if j["price"] is not None else None]
    if any(bits):
        print(" · ".join(b for b in bits if b))
    verdict = {"passed": f"PASS — rank {j.get('rank')} of {len(res['passed'])} passers",
               "near_miss": "NEAR MISS — failed exactly one rule", "failed": f"FAIL — {len(j['failed'])} rules failed",
               "gated": "GATED OUT — red flag", "incomplete": "INCOMPLETE DATA — cannot be judged"}[j["bucket"]]
    score = "" if j["score"] is None else f" · score {j['score']:.1f}/100"
    print(f"Verdict: {verdict}{score} · {j['passed_rules']} of {j['judged_rules']} rules passed")
    word = {"pass": "pass", "fail": "FAIL", "na": "no data", "skipped": "skipped (financial)",
            "no-column": "not checked (no column)"}
    rows = []
    for k, r in zip(j["checks"], res["rules"]):
        rows.append([k["rule"], FIELDS[k["field"]].label, fmt(k["field"], k["value"]),
                     f"{OP_TEXT[r.op]} {fmt_threshold(r.field, r.value)}", word[k["status"]],
                     "—" if k["score"] is None else f"{k['score']:.0f}", f"{k['weight']:g}", r.why])
    print(_table(["Rule", "Metric", "Value", "Needs", "Result", "Score", "Wt", "Why"], rows, left=2))
    if cfg["no_gates"]:
        print("Gates: off (--no-gates)")
    else:
        print("Gates: " + ("; ".join(j["gates"]) if j["gates"] else "clear (market cap, pledge, net worth)"))
    if j["flags"]:
        print("Flags: " + ", ".join(j["flags"]))
    if j["financial"]:
        print("Note: " + FIN_NOTE)
    for n in res["notes"]:
        if "NOT CHECKED" in n:
            print(f"Note: {n}")
    print("Score: 50 at the threshold, 100 at the rule's best level; composite = weight-averaged rule score.")
    print(DISCLAIMER)


def print_query(q: dict, name, preset) -> None:
    title = f"{name} ({preset['title']})" if preset else "custom filters"
    print(f"Screener.in query — {title}")
    print("Paste into screener.in → Screens → Create new screen → Query box:\n")
    print(q["query"])
    print()
    if q["unverified_fields"]:
        print("Unverified field names (not seen in a public screen; fix in the query box if Screener.in rejects "
              "them): " + ", ".join(q["unverified_fields"]))
    for n in q["notes"]:
        print(f"- {n}")
    print(DISCLAIMER)


# ---------------------------------------------------------------------------
# JSON shapes
# ---------------------------------------------------------------------------
def _rule_json(r: Rule, avail=None) -> dict:
    out = {"rule": r.tag, "field": r.field, "op": r.op, "value": r.value, "text": r.text, "weight": r.weight,
           "best": r.best, "why": r.why}
    if avail is not None:
        out["checked"] = r.field in avail
    return out


def _co_json(j: dict) -> dict:
    keep = ("name", "symbol", "industry", "financial", "mcap", "price", "score", "bucket", "passed_rules",
            "judged_rules", "failed", "missing", "gates", "flags")
    out = {k: j[k] for k in keep}
    if "rank" in j:
        out["rank"] = j["rank"]
    out["checks"] = [{k: c[k] for k in ("rule", "field", "value", "status", "score")} for c in j["checks"]]
    return out


def _cfg_json(cfg: dict) -> dict:
    return {"enabled": not cfg["no_gates"], "min_mcap_cr": cfg["min_mcap"], "max_pledge_pct": cfg["max_pledge"],
            "gsec_yield_pct": cfg["gsec"], "dividend_yield_bar_pct": yield_bar(cfg)}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _config(a) -> dict:
    cfg = dict(DEFAULTS)
    cfg["min_mcap"] = getattr(a, "min_mcap", DEFAULTS["min_mcap"])
    cfg["max_pledge"] = getattr(a, "max_pledge", DEFAULTS["max_pledge"])
    cfg["gsec"] = getattr(a, "gsec", DEFAULTS["gsec"])
    cfg["no_gates"] = bool(getattr(a, "no_gates", False))
    if cfg["min_mcap"] < 0 or cfg["max_pledge"] < 0:
        raise ScreenError("--min-mcap and --max-pledge cannot be negative")
    if not 0 < cfg["gsec"] <= 25:
        raise ScreenError("--gsec is the 10-year G-sec yield in percent, e.g. 7.1")
    return cfg


def main(argv=None) -> int:
    tune = argparse.ArgumentParser(add_help=False)
    tune.add_argument("--json", action="store_true", help="machine-readable output")
    tune.add_argument("--gsec", type=float, default=DEFAULTS["gsec"],
                      help="current 10-year G-sec yield %% (7.0); sets the value-dividend yield bar at 40%% of it")
    tune.add_argument("--min-mcap", type=float, default=DEFAULTS["min_mcap"], help="market-cap floor, ₹ crore (500)")
    tune.add_argument("--max-pledge", type=float, default=DEFAULTS["max_pledge"],
                      help="promoter pledge %% above which a company is gated out (20)")
    tune.add_argument("--no-gates", action="store_true", help="switch off the red-flag gates")
    src = argparse.ArgumentParser(add_help=False)
    src.add_argument("--csv", required=True, help="fundamentals CSV, one row per company (Screener.in export "
                                                  "or snake_case columns)")
    src.add_argument("--map", action="append", metavar="FIELD=HEADER",
                     help="force a column mapping, e.g. --map \"div_growth=Div Gr 3Yrs %%\" (repeatable)")
    src.add_argument("--financials", default="auto",
                     help="auto (industry column, else name), none, or a comma list of names to treat as "
                          "banks/NBFCs")
    src.add_argument("--prices-dir", help="folder of daily OHLCV CSVs (file name = symbol or company name) "
                                          "for RSI(14)")
    pick = argparse.ArgumentParser(add_help=False)
    pick.add_argument("--preset", help="canslim-india | value-dividend | dividend-growth-pullback | garp | "
                                       "quality-compounder")
    pick.add_argument("--where", action="append", metavar="\"FIELD OP NUMBER\"",
                      help="extra filter, e.g. \"roce>20\" (repeatable; operators > >= < <= == !=)")

    p = argparse.ArgumentParser(description="Fundamental stock screener for Indian equities (offline, CSV in)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("presets", parents=[tune], help="list the built-in screens and their rules")
    f = sub.add_parser("fields", help="field names for --where/--sort and how a file's headers map")
    f.add_argument("--csv", help="show how this file's columns were mapped")
    f.add_argument("--map", action="append", metavar="FIELD=HEADER")
    f.add_argument("--json", action="store_true")
    r = sub.add_parser("run", parents=[tune, src, pick], help="filter and rank the universe")
    r.add_argument("--sort", help="field to rank passers by (default: composite score, or the first --where field)")
    r.add_argument("--asc", action="store_true", help="with --sort: lowest first")
    r.add_argument("--top", type=int, default=DEFAULTS["top"], help="rows shown per list (20)")
    e = sub.add_parser("explain", parents=[tune, src, pick], help="per-rule breakdown for one company")
    e.add_argument("symbol", help="company name or symbol as it appears in the CSV")
    sub.add_parser("query", parents=[tune, pick], help="Screener.in query text for a preset and/or --where filters")

    a = p.parse_args(argv)
    try:
        return _run(a)
    except ScreenError as err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    except (OSError, UnicodeDecodeError, csv.Error) as err:
        print(f"error: {err}", file=sys.stderr)
        return 2


def _run(a) -> int:
    if a.cmd == "fields":
        data = None
        if a.csv:
            data = load_companies(a.csv, parse_maps(a.map))
            mark_financials(data)
            derive(data)
        if a.json:
            emit_json({"command": "fields",
                       "fields": {k: {"label": d.label, "unit": d.kind, "screener_header": d.header or None,
                                      "header_verified": d.header_ok, "screener_query": d.query or None,
                                      "query_verified": d.query_ok, "derived": d.derived or None}
                                  for k, d in FIELDS.items()},
                       "file": data["file"] if data else None,
                       "columns": data["columns"] if data else None,
                       "unmapped": data["unmapped"] if data else None,
                       "financials": [c["name"] for c in data["companies"] if c["financial"]] if data else None})
        else:
            print_fields(data)
        return 0

    cfg = _config(a)
    if a.cmd == "presets":
        presets = build_presets(cfg)
        if a.json:
            emit_json({"command": "presets", "gates": _cfg_json(cfg),
                       "presets": {n: {"title": p["title"], "idea": p["idea"],
                                       "rules": [_rule_json(r) for r in p["rules"]], "notes": p["notes"]}
                                   for n, p in presets.items()},
                       "disclaimer": DISCLAIMER})
        else:
            print_presets(presets, cfg)
        return 0

    name, preset, rules = get_rules(a, cfg)
    if a.cmd == "query":
        q = to_query(rules, cfg)
        if a.json:
            emit_json({"command": "query", "preset": name, **q, "disclaimer": DISCLAIMER})
        else:
            print_query(q, name, preset)
        return 0

    if a.cmd == "run" and a.top < 1:
        raise ScreenError("--top must be at least 1")
    data = prepare(a)
    res = screen(data, rules, cfg, getattr(a, "sort", None), getattr(a, "asc", False))
    base = {"preset": name, "title": preset["title"] if preset else "custom filters", "file": data["file"],
            "companies": len(data["companies"]), "columns": data["columns"],
            "rules": [_rule_json(r, data["available"]) for r in rules], "gates": _cfg_json(cfg)}
    if a.cmd == "explain":
        target = find_company(data, a.symbol)
        j = next(x for b in ("passed", "near_miss", "failed", "gated", "incomplete") for x in res[b]
                 if x["name"] == target["name"])
        if a.json:
            emit_json({"command": "explain", **base, "company": _co_json(j), "passers": len(res["passed"]),
                       "notes": res["notes"] + (preset["notes"] if preset else []), "disclaimer": DISCLAIMER})
        else:
            print_explain(j, res, data, name, preset, cfg)
        return 0

    if a.json:
        emit_json({"command": "run", **base, "sort": res["sort"], "ascending": res["ascending"],
                   "counts": {b: len(res[b]) for b in ("passed", "near_miss", "failed", "gated", "incomplete")},
                   "passed": [_co_json(j) for j in res["passed"][:a.top]],
                   "near_misses": [_co_json(j) for j in res["near_miss"][:a.top]],
                   "gated": [{"name": j["name"], "reasons": j["gates"]} for j in res["gated"]],
                   "incomplete": [{"name": j["name"], "missing": j["missing"]} for j in res["incomplete"]],
                   "failed": [{"name": j["name"], "failed": j["failed"]} for j in res["failed"]],
                   "notes": res["notes"] + (preset["notes"] if preset else []), "disclaimer": DISCLAIMER})
    else:
        print_run(res, data, name, preset, cfg, a.top)
    return 0


if __name__ == "__main__":
    sys.exit(main())
