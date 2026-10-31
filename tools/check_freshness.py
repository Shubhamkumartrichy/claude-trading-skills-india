#!/usr/bin/env python3
"""Flag market data that may be stale: rates as-of dates, holiday calendar coverage, tax years.

Used by the monthly GitHub Action (.github/workflows/freshness.yml). Exit code is 0 unless
--strict is passed and something is stale.

  python3 tools/check_freshness.py                       # report to stdout
  python3 tools/check_freshness.py --max-age-days 120 --report freshness.md
"""
from __future__ import annotations

import argparse
import os
import pathlib
import re
import sys
from datetime import date

ROOT = pathlib.Path(__file__).resolve().parents[1]
CHARGES = ROOT / "skills/trading-costs/scripts/charges.py"
OPTIONS = ROOT / "skills/fno-options-analysis/scripts/options.py"
TAX = ROOT / "skills/indian-tax-for-traders/scripts/tax.py"


def _find(path: pathlib.Path, pattern: str) -> str:
    m = re.search(pattern, path.read_text(encoding="utf-8"))
    if not m:
        raise SystemExit(f"pattern {pattern!r} not found in {path}")
    return m.group(1)


def current_tax_year(today: date) -> str:
    start = today.year if today.month >= 4 else today.year - 1
    return f"{start}-{str(start + 1)[-2:]}"


def check(today: date, max_age_days: int = 120) -> list:
    problems = []
    rates = date.fromisoformat(_find(CHARGES, r'RATES_AS_OF\s*=\s*"([\d-]+)"'))
    if (today - rates).days > max_age_days:
        problems.append(f"Trading charges and broker pricing were last verified on {rates} "
                        f"({(today - rates).days} days ago). Check STT, exchange charges and broker pages "
                        f"(`{CHARGES.relative_to(ROOT)}`).")
    data = date.fromisoformat(_find(OPTIONS, r'DATA_AS_OF\s*=\s*"([\d-]+)"'))
    if (today - data).days > max_age_days:
        problems.append(f"F&O lot sizes were last verified on {data} ({(today - data).days} days ago). "
                        f"Check the exchange lot-size circulars (`{OPTIONS.relative_to(ROOT)}`).")
    y, m, d = map(int, re.search(r"HOLIDAYS_KNOWN_UNTIL\s*=\s*date\((\d+),\s*(\d+),\s*(\d+)\)",
                                 OPTIONS.read_text(encoding="utf-8")).groups())
    until = date(y, m, d)
    if (until - today).days < 45:
        problems.append(f"The holiday calendar ends on {until}. Add next year's NSE trading holidays to "
                        f"`HOLIDAYS` in `{OPTIONS.relative_to(ROOT)}` and the market-structure reference.")
    years = re.findall(r'"(\d{4}-\d{2})"', _find(TAX, r"SUPPORTED_YEARS\s*=\s*\(([^)]*)\)"))
    ty = current_tax_year(today)
    if ty not in years:
        problems.append(f"Tax year {ty} is not in SUPPORTED_YEARS {years}. Review the Finance Act / Budget "
                        f"changes in `{TAX.relative_to(ROOT)}`.")
    return problems


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--max-age-days", type=int, default=120)
    p.add_argument("--today", help="YYYY-MM-DD (default: today)")
    p.add_argument("--report", help="write a markdown report here")
    p.add_argument("--strict", action="store_true")
    a = p.parse_args(argv)
    today = date.fromisoformat(a.today) if a.today else date.today()
    problems = check(today, a.max_age_days)
    if problems:
        body = "The monthly data check found items to review:\n\n" + "\n".join(f"- [ ] {x}" for x in problems)
        body += ("\n\nPlease update the values from official sources (exchange circulars, broker pricing pages, "
                 "the Finance Act), bump the as-of dates, and run `python3 -m unittest discover -s tests`.")
        for x in problems:
            print(f"::warning::{x}" if os.environ.get("GITHUB_ACTIONS") else f"STALE: {x}")
    else:
        body = "All market data is within the freshness window."
        print(body)
    if a.report:
        pathlib.Path(a.report).write_text(body + "\n", encoding="utf-8")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"stale={'true' if problems else 'false'}\n")
    return 1 if (problems and a.strict) else 0


if __name__ == "__main__":
    sys.exit(main())
