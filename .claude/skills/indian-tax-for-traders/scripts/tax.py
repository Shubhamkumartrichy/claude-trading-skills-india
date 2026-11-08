#!/usr/bin/env python3
"""Income-tax estimator for Indian resident individual traders and investors.

Zero dependencies (Python 3.10+ stdlib only). Covers tax years 2025-26 and 2026-27
(identical slabs and capital-gains rates; FY 2026-27 is "tax year 2026-27" under the
Income-tax Act, 2025, which replaced the 1961 Act from 1 April 2026).

Sub-commands
------------
  estimate   Tax on a year's income mix: salary, other income, dividends, F&O
             (non-speculative business), intraday (speculative business), STCG, LTCG.
  tradebook  FIFO capital gains + intraday P&L from an equity tradebook CSV.
  turnover   F&O / intraday turnover (sum of absolute trade P&L) and audit hint.
  harvest    Tax-loss / LTCG-exemption harvesting ideas from a holdings CSV.

Examples
--------
  python3 tax.py estimate --salary 1800000 --stcg 120000 --ltcg 300000 --fno -80000
  python3 tax.py tradebook trades.csv --fy 2026-27
  python3 tax.py turnover --pnl 1200 -800 4500 -300
  python3 tax.py harvest holdings.csv --as-of 2027-03-10 --realized-ltcg 40000

This is an estimate, not tax advice. Surcharge marginal relief, AMT, presumptive
schemes, set-off choices, and edge cases are simplified. Confirm with a CA.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict, deque
from datetime import date, datetime

INF = float("inf")
SUPPORTED_YEARS = ("2025-26", "2026-27")

SLABS = {
    "new": [(400000, 0.0), (800000, 0.05), (1200000, 0.10), (1600000, 0.15),
            (2000000, 0.20), (2400000, 0.25), (INF, 0.30)],
    "old": [(250000, 0.0), (500000, 0.05), (1000000, 0.20), (INF, 0.30)],  # age < 60
}
STD_DEDUCTION = {"new": 75000, "old": 50000}
BASIC_EXEMPTION = {"new": 400000, "old": 250000}
REBATE_87A = {"new": (1200000, 60000), "old": (500000, 12500)}  # (income limit, max rebate)
STCG_RATE = 0.20       # listed equity / equity MF, STT paid: old s.111A (s.196 in IT Act 2025)
LTCG_RATE = 0.125      # old s.112A (s.198 in IT Act 2025)
LTCG_EXEMPTION = 125000
CESS = 0.04
SURCHARGE = {
    "new": [(5000000, 0.0), (10000000, 0.10), (20000000, 0.15), (INF, 0.25)],
    "old": [(5000000, 0.0), (10000000, 0.10), (20000000, 0.15), (50000000, 0.25), (INF, 0.37)],
}
CG_SURCHARGE_CAP = 0.15
GRANDFATHER_DATE = date(2018, 2, 1)   # shares bought before this use FMV of 31 Jan 2018


def inr(x: float) -> str:
    neg = x < 0
    s = f"{abs(x):.0f}"
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ",".join(groups + [tail])
    return ("-" if neg else "") + "₹" + s


def slab_tax(income: float, regime: str) -> float:
    tax, prev = 0.0, 0.0
    for upper, rate in SLABS[regime]:
        if income <= prev:
            break
        tax += (min(income, upper) - prev) * rate
        prev = upper
    return tax


def surcharge_rate(total_income: float, regime: str) -> float:
    for upper, rate in SURCHARGE[regime]:
        if total_income <= upper:
            return rate
    return SURCHARGE[regime][-1][1]


def _use(loss: float, against: float):
    """Set a positive loss against a positive income. Returns (loss_left, income_left, used)."""
    used = min(loss, max(0.0, against))
    return loss - used, against - used, used


def estimate(salary=0.0, other_income=0.0, dividends=0.0, fno=0.0, intraday=0.0,
             stcg=0.0, ltcg=0.0, regime="new", resident=True,
             bf_business_loss=0.0, bf_spec_loss=0.0, bf_stcl=0.0, bf_ltcl=0.0) -> dict:
    """Estimate tax. Inputs are the year's NET figures (losses negative).

    fno       : net F&O profit after deductible expenses (non-speculative business)
    intraday  : net intraday equity P&L (speculative business)
    stcg/ltcg : net listed-equity gains for the year (STT paid)
    bf_*      : brought-forward losses (positive numbers)
    """
    notes, steps = [], []
    std = STD_DEDUCTION[regime] if salary > 0 else 0.0
    salary_net = max(0.0, salary - std)
    other = other_income + dividends

    spec = intraday
    biz = fno
    st, lt = stcg, ltcg

    # --- In-year set-off (old ss.70-71; same scheme carried into IT Act 2025) ---
    spec_loss_cf = 0.0
    if spec < 0:
        spec_loss_cf = -spec          # speculative loss only vs speculative income
        spec = 0.0
        steps.append(f"Intraday loss {inr(spec_loss_cf)} cannot offset any other income; carried forward (4 yrs).")

    # Intra-head first (capital gains: STCL vs LTCG), then inter-head business-loss set-off.
    ltcl_cf = 0.0
    if st < 0:
        loss = -st
        st = 0.0
        loss, lt, u = _use(loss, lt)
        if u: steps.append(f"Short-term capital loss {inr(u)} set off against LTCG.")
        stcl_cf = loss
    else:
        stcl_cf = 0.0
    if lt < 0:
        ltcl_cf = -lt
        lt = 0.0
        steps.append(f"Long-term capital loss {inr(ltcl_cf)} carried forward (8 yrs, vs LTCG only).")

    biz_loss_cf = 0.0
    if biz < 0:
        loss = -biz
        biz = 0.0
        loss, spec, u = _use(loss, spec)
        if u: steps.append(f"F&O loss {inr(u)} set off against intraday profit.")
        loss, other, u = _use(loss, other)
        if u: steps.append(f"F&O loss {inr(u)} set off against other income (interest/dividends etc.).")
        loss, st, u = _use(loss, st)
        if u: steps.append(f"F&O loss {inr(u)} set off against STCG.")
        loss, lt, u = _use(loss, lt)
        if u: steps.append(f"F&O loss {inr(u)} set off against LTCG.")
        biz_loss_cf = loss
        if loss:
            steps.append(f"F&O loss {inr(loss)} left (cannot offset salary); carried forward (8 yrs).")


    # --- Brought-forward losses (old ss.72-74) ---
    bf_spec, spec, u = _use(bf_spec_loss, spec)
    if u: steps.append(f"Brought-forward speculative loss {inr(u)} used against intraday profit.")
    bf_biz, biz, u = _use(bf_business_loss, biz)
    if u: steps.append(f"Brought-forward business loss {inr(u)} used against F&O profit.")
    bf_biz, spec, u = _use(bf_biz, spec)
    if u: steps.append(f"Brought-forward business loss {inr(u)} used against intraday profit.")
    bf_st, st, u = _use(bf_stcl, st)
    if u: steps.append(f"Brought-forward STCL {inr(u)} used against STCG.")
    bf_st, lt, u = _use(bf_st, lt)
    if u: steps.append(f"Brought-forward STCL {inr(u)} used against LTCG.")
    bf_lt, lt, u = _use(bf_ltcl, lt)
    if u: steps.append(f"Brought-forward LTCL {inr(u)} used against LTCG.")

    normal = salary_net + other + biz + spec
    ltcg_taxable = max(0.0, lt - LTCG_EXEMPTION)
    st_taxable = st
    total_income = normal + st + lt

    # Resident individuals may use unused basic exemption against special-rate gains.
    if resident:
        shortfall = max(0.0, BASIC_EXEMPTION[regime] - normal)
        _, st_taxable, u1 = _use(shortfall, st_taxable)
        _, ltcg_taxable, u2 = _use(shortfall - u1, ltcg_taxable)
        if u1 or u2:
            steps.append(f"Unused basic exemption {inr(u1 + u2)} adjusted against capital gains.")

    tax_normal = slab_tax(normal, regime)
    tax_st = st_taxable * STCG_RATE
    tax_lt = ltcg_taxable * LTCG_RATE

    limit, max_rebate = REBATE_87A[regime]
    rebate = 0.0
    if regime == "new":
        if total_income <= limit:
            rebate = min(tax_normal, max_rebate)
        elif normal <= limit + 100000:
            # Marginal relief: normal-income tax may not exceed income above ₹12L.
            rebate = max(0.0, tax_normal - (total_income - limit))
        notes.append("New regime: s.87A rebate is not available against tax on STCG/LTCG at special rates.")
    else:
        if total_income <= limit:
            rebate = min(tax_normal + tax_st, max_rebate)
    tax_before_surcharge = tax_normal + tax_st + tax_lt - rebate

    s_rate = surcharge_rate(total_income, regime)
    normal_share = max(0.0, tax_normal - rebate)
    # Dividend income (inside slab income) also has its surcharge capped at 15%.
    div_in_normal = min(max(0.0, dividends), max(0.0, normal))
    div_tax = min(normal_share, slab_tax(normal, regime) - slab_tax(normal - div_in_normal, regime))
    surcharge = ((normal_share - div_tax) * s_rate + div_tax * min(s_rate, CG_SURCHARGE_CAP)
                 + (tax_st + tax_lt) * min(s_rate, CG_SURCHARGE_CAP))
    if s_rate:
        notes.append("Surcharge computed without marginal relief; actual may be lower near thresholds.")
    cess = (tax_before_surcharge + surcharge) * CESS
    total_tax = tax_before_surcharge + surcharge + cess

    if fno or intraday:
        notes.append("F&O or intraday income: file ITR-3 and keep a P&L statement from your broker.")
    elif stcg or (ltcg and ltcg > LTCG_EXEMPTION) or ltcl_cf or stcl_cf:
        notes.append("Capital gains: ITR-2 (ITR-1 allows only LTCG up to ₹1.25L with no losses to carry).")
    if biz_loss_cf or spec_loss_cf or stcl_cf or ltcl_cf:
        notes.append("Losses carry forward only if the return is filed by the original due date.")

    return {
        "regime": regime,
        "salary_after_std_deduction": salary_net,
        "normal_income": normal,
        "stcg_after_setoff": st, "ltcg_after_setoff": lt,
        "stcg_taxable": st_taxable, "ltcg_taxable": ltcg_taxable,
        "total_income": total_income,
        "tax_on_normal_income": tax_normal, "tax_on_stcg": tax_st, "tax_on_ltcg": tax_lt,
        "rebate_87a": rebate, "surcharge": surcharge, "cess": cess, "total_tax": total_tax,
        "carry_forward": {
            "business_loss_8y": biz_loss_cf + bf_biz, "speculative_loss_4y": spec_loss_cf + bf_spec,
            "stcl_8y": stcl_cf + bf_st, "ltcl_8y": ltcl_cf + bf_lt,
        },
        "steps": steps, "notes": notes,
    }


# ---------------------------------------------------------------------------
# Tradebook processing
# ---------------------------------------------------------------------------
DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y", "%d %b %Y", "%Y/%m/%d",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S")


def parse_date(s: str) -> date:
    s = str(s).strip()
    for candidate in (s, s[:19], s[:10]):
        for fmt in DATE_FORMATS:
            try:
                return datetime.strptime(candidate, fmt).date()
            except ValueError:
                continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        raise ValueError(f"unrecognised date: {s!r}")


def fy_of(d: date) -> str:
    start = d.year if d.month >= 4 else d.year - 1
    return f"{start}-{str(start + 1)[-2:]}"


def add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    y += d.year
    m += 1
    days = [31, 29 if (y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)) else 28,
            31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
    return date(y, m, min(d.day, days))


def is_long_term(buy: date, sell: date) -> bool:
    """Listed equity is long-term if held for MORE than 12 months."""
    return sell > add_months(buy, 12)


def _col(row: dict, *names, default=None):
    for n in names:
        for k, v in row.items():
            if k and k.strip().lower().replace(" ", "_") == n:
                return v
    return default


SYMBOL_COLS = ("symbol", "tradingsymbol", "trading_symbol", "scrip", "scrip_code", "scrip_name", "stock",
               "stock_name", "company", "company_name", "instrument", "security", "isin")


def load_trades(path: str) -> list:
    trades = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for n, row in enumerate(csv.DictReader(f), start=2):
            qty = float(str(_col(row, "qty", "quantity")).replace(",", ""))
            side = (_col(row, "side", "type", "trade_type", "buy/sell", "transaction_type") or "").strip().upper()
            if side.startswith("B"):
                side = "BUY"
            elif side.startswith("S"):
                side = "SELL"
            elif qty:
                side = "SELL" if qty < 0 else "BUY"     # some exports sign the quantity instead
            else:
                continue
            symbol = (_col(row, *SYMBOL_COLS) or "").strip().upper()
            if not symbol:
                raise ValueError(f"row {n}: no symbol — expected a column named one of {', '.join(SYMBOL_COLS)}")
            trades.append({
                "date": parse_date(_col(row, "date", "trade_date", "order_execution_time")),
                "symbol": symbol,
                "side": side,
                "qty": abs(qty),
                "price": float(str(_col(row, "price", "trade_price", "rate")).replace(",", "")),
                "charges": float(str(_col(row, "charges", "expenses", default="0") or 0).replace(",", "")),
                "fmv_2018": float(str(_col(row, "fmv_2018", "fmv_31jan2018", default="0") or 0).replace(",", "")),
            })
    return trades


def process_tradebook(trades: list) -> dict:
    """FIFO matching. Same-day buy+sell in a symbol is intraday (speculative)."""
    by_day = defaultdict(lambda: {"BUY": [], "SELL": []})
    for t in trades:
        by_day[(t["date"], t["symbol"])][t["side"]].append(t)

    lots = defaultdict(deque)   # symbol -> deque of [buy_date, qty, unit_cost, fmv_2018]
    years = defaultdict(lambda: {"stcg": 0.0, "ltcg": 0.0, "intraday": 0.0, "intraday_turnover": 0.0,
                                 "sell_value": 0.0, "realised": []})
    warnings = []

    def sell_from_lots(sym, d, fy, qty, sp, sc):
        remaining = qty
        while remaining > 1e-9:
            if not lots[sym]:
                warnings.append(f"{d} {sym}: sold {remaining:g} more than held in tradebook "
                                "(opening holdings missing?) — ignored.")
                break
            lot = lots[sym][0]
            take = min(remaining, lot[1])
            buy_d, unit_cost, fmv = lot[0], lot[2], lot[3]
            cost_unit = unit_cost
            if buy_d < GRANDFATHER_DATE and fmv > 0:
                cost_unit = max(unit_cost, min(fmv, sp))
            gain = take * ((sp - sc) - cost_unit)
            term = "LTCG" if is_long_term(buy_d, d) else "STCG"
            years[fy]["ltcg" if term == "LTCG" else "stcg"] += gain
            years[fy]["sell_value"] += take * sp
            years[fy]["realised"].append({"symbol": sym, "buy_date": buy_d.isoformat(),
                                          "sell_date": d.isoformat(), "qty": take,
                                          "cost": round(cost_unit, 4), "sale": sp,
                                          "gain": round(gain, 2), "term": term})
            lot[1] -= take
            remaining -= take
            if lot[1] <= 1e-9:
                lots[sym].popleft()

    for (d, sym) in sorted(by_day):
        day = by_day[(d, sym)]
        fy = fy_of(d)
        # [qty_left, price, charge_per_unit, fmv] in file order; intraday matches FIFO within the day
        buys = [[t["qty"], t["price"], t["charges"] / t["qty"] if t["qty"] else 0.0, t["fmv_2018"]]
                for t in day["BUY"]]
        sells = [[t["qty"], t["price"], t["charges"] / t["qty"] if t["qty"] else 0.0] for t in day["SELL"]]
        bi = si = 0
        while bi < len(buys) and si < len(sells):
            q = min(buys[bi][0], sells[si][0])
            if q > 0:
                pnl = q * ((sells[si][1] - sells[si][2]) - (buys[bi][1] + buys[bi][2]))
                years[fy]["intraday"] += pnl
                years[fy]["intraday_turnover"] += abs(pnl)
            buys[bi][0] -= q
            sells[si][0] -= q
            if buys[bi][0] <= 1e-9:
                bi += 1
            if sells[si][0] <= 1e-9:
                si += 1
        for q, price, c, fmv in buys[bi:]:
            if q > 1e-9:
                lots[sym].append([d, q, price + c, fmv])
        for q, price, c in sells[si:]:
            if q > 1e-9:
                sell_from_lots(sym, d, fy, q, price, c)

    open_lots = {s: [[b.isoformat(), q, round(c, 4)] for b, q, c, _ in dq] for s, dq in lots.items() if dq}
    return {"years": dict(years), "open_lots": open_lots, "warnings": warnings}


# ---------------------------------------------------------------------------
# Harvesting
# ---------------------------------------------------------------------------
def harvest(holdings: list, as_of: date, realized_stcg=0.0, realized_ltcg=0.0) -> dict:
    rows = []
    for h in holdings:
        cost = h["buy_price"]
        if h["buy_date"] < GRANDFATHER_DATE and h.get("fmv_2018"):
            cost = max(cost, min(h["fmv_2018"], h["ltp"]))
        gain = (h["ltp"] - cost) * h["qty"]
        lt = is_long_term(h["buy_date"], as_of)
        lt_date = add_months(h["buy_date"], 12)
        days_to_lt = 0 if lt else (lt_date - as_of).days + 1
        rows.append({**h, "buy_date": h["buy_date"].isoformat(), "cost_basis": cost,
                     "unrealised": gain, "term": "LT" if lt else "ST", "days_to_long_term": days_to_lt})

    ideas = []
    room = max(0.0, LTCG_EXEMPTION - max(0.0, realized_ltcg + min(0.0, realized_stcg)))
    for r in sorted([r for r in rows if r["term"] == "LT" and r["unrealised"] > 0],
                    key=lambda r: -r["unrealised"]):
        if room <= 0:
            break
        per_share = r["ltp"] - r["cost_basis"]
        qty = min(r["qty"], int(room // per_share)) if per_share > 0 else 0
        if qty <= 0:
            continue
        g = qty * per_share
        room -= g
        ideas.append(f"LTCG harvest: sell & rebuy {qty:g} {r['symbol']} (gain {inr(g)}) — tax-free within "
                     f"the ₹1.25L exemption; resets cost basis higher (saves ~{inr(g * LTCG_RATE)} later).")
    if realized_stcg > 0:
        for r in sorted([r for r in rows if r["unrealised"] < 0], key=lambda r: r["unrealised"]):
            ideas.append(f"Loss harvest: booking {r['symbol']} ({r['term']}) realises {inr(r['unrealised'])}"
                         + (" — offsets STCG (20%)." if r["term"] == "ST" else " — LT loss offsets only LTCG."))
    for r in rows:
        if r["term"] == "ST" and r["unrealised"] > 0 and 0 < r["days_to_long_term"] <= 45:
            ideas.append(f"Wait: {r['symbol']} turns long-term in {r['days_to_long_term']} days; selling after "
                         f"that cuts the rate on {inr(r['unrealised'])} from 20% to 12.5%.")
    return {"as_of": as_of.isoformat(), "ltcg_exemption_room_left": room, "lots": rows, "ideas": ideas,
            "caveat": "India has no statutory wash-sale rule, but sell-and-rebuy purely for tax can be "
                      "questioned as a sham/GAAR transaction; keep it to genuine trades and count costs "
                      "(~0.23% round trip for delivery)."}


def load_holdings(path: str) -> list:
    out = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            out.append({
                "symbol": (_col(row, "symbol", "tradingsymbol", "stock") or "").strip().upper(),
                "buy_date": parse_date(_col(row, "buy_date", "date")),
                "qty": float(_col(row, "qty", "quantity")),
                "buy_price": float(_col(row, "buy_price", "avg_price", "average_price", "price")),
                "ltp": float(_col(row, "ltp", "current_price", "close")),
                "fmv_2018": float(_col(row, "fmv_2018", default="0") or 0),
            })
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def print_estimate(r: dict) -> None:
    print(f"\nTax estimate — {r['regime']} regime")
    print("-" * 52)
    rows = [("Normal income (slab-taxed)", r["normal_income"]),
            ("STCG after set-off", r["stcg_after_setoff"]),
            ("LTCG after set-off", r["ltcg_after_setoff"]),
            ("  LTCG taxable (above ₹1.25L)", r["ltcg_taxable"]),
            ("Total income", r["total_income"]),
            ("Tax on normal income", r["tax_on_normal_income"]),
            ("Tax on STCG @20%", r["tax_on_stcg"]),
            ("Tax on LTCG @12.5%", r["tax_on_ltcg"]),
            ("Rebate u/s 87A", -r["rebate_87a"]),
            ("Surcharge", r["surcharge"]),
            ("Health & education cess 4%", r["cess"])]
    for label, v in rows:
        print(f"{label:<34}{inr(v):>18}")
    print("-" * 52)
    print(f"{'TOTAL TAX':<34}{inr(r['total_tax']):>18}")
    cf = {k: v for k, v in r["carry_forward"].items() if v}
    if cf:
        print("\nCarry forward:", ", ".join(f"{k} {inr(v)}" for k, v in cf.items()))
    for s in r["steps"]:
        print(" •", s)
    for n in r["notes"]:
        print(" ⚠", n)
    print(" ⚠ Estimate only — confirm with a Chartered Accountant before filing.")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Indian trader tax estimator")
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("estimate")
    for name in ("salary", "other-income", "dividends", "fno", "intraday", "stcg", "ltcg",
                 "bf-business-loss", "bf-spec-loss", "bf-stcl", "bf-ltcl"):
        e.add_argument(f"--{name}", type=float, default=0.0)
    e.add_argument("--regime", choices=["new", "old"], default="new")
    e.add_argument("--non-resident", action="store_true")
    e.add_argument("--json", action="store_true")

    t = sub.add_parser("tradebook")
    t.add_argument("csv")
    t.add_argument("--fy", help="e.g. 2026-27; default: all years")
    t.add_argument("--json", action="store_true")
    t.add_argument("--details", action="store_true", help="list every realised lot")

    tu = sub.add_parser("turnover")
    tu.add_argument("--pnl", type=float, nargs="*", default=[], help="per-trade P&L values")
    tu.add_argument("--csv", help="CSV with a pnl column")

    h = sub.add_parser("harvest")
    h.add_argument("csv")
    h.add_argument("--as-of", default=date.today().isoformat())
    h.add_argument("--realized-stcg", type=float, default=0.0)
    h.add_argument("--realized-ltcg", type=float, default=0.0)
    h.add_argument("--json", action="store_true")

    a = p.parse_args(argv)

    if a.cmd == "estimate":
        r = estimate(a.salary, a.other_income, a.dividends, a.fno, a.intraday, a.stcg, a.ltcg,
                     a.regime, not a.non_resident, a.bf_business_loss, a.bf_spec_loss, a.bf_stcl, a.bf_ltcl)
        print(json.dumps(r, indent=2)) if a.json else print_estimate(r)
        return 0

    if a.cmd == "tradebook":
        res = process_tradebook(load_trades(a.csv))
        years = res["years"]
        if a.fy:
            years = {a.fy: years.get(a.fy, {"stcg": 0, "ltcg": 0, "intraday": 0, "intraday_turnover": 0,
                                              "sell_value": 0, "realised": []})}
        if a.json:
            print(json.dumps({**res, "years": years}, indent=2, default=str))
            return 0
        for fy in sorted(years):
            y = years[fy]
            flag = "" if fy in SUPPORTED_YEARS else "  (rates differ before FY 2025-26; check manually)"
            print(f"\nFY {fy}{flag}")
            print(f"  STCG (≤12 months): {inr(y['stcg'])}")
            print(f"  LTCG (>12 months): {inr(y['ltcg'])}  (exemption ₹1.25L applies on the year's total)")
            print(f"  Intraday P&L     : {inr(y['intraday'])}  (speculative business; turnover {inr(y['intraday_turnover'])})")
            if a.details:
                for x in y["realised"]:
                    print(f"    {x['sell_date']} {x['symbol']:<12} {x['qty']:>8g} {x['term']}  bought {x['buy_date']}"
                          f"  cost {x['cost']:.2f} sale {x['sale']:.2f}  gain {inr(x['gain'])}")
        for w in res["warnings"]:
            print(" ⚠", w)
        if res["open_lots"]:
            print("\nOpen lots:", ", ".join(f"{s} ({sum(q for _, q, _ in v):g})" for s, v in res["open_lots"].items()))
        print("\nNote: 'charges' column should exclude STT (not deductible for capital gains).")
        return 0

    if a.cmd == "turnover":
        pnls = list(a.pnl)
        if a.csv:
            with open(a.csv, newline="", encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    v = _col(row, "pnl", "p&l", "realised_pnl", "realized_pnl", "profit")
                    if v not in (None, ""):
                        pnls.append(float(str(v).replace(",", "")))
        turnover = sum(abs(x) for x in pnls)
        net = sum(pnls)
        print(f"Trades: {len(pnls)}  Net P&L: {inr(net)}  Turnover (Σ|P&L|): {inr(turnover)}")
        if turnover > 10_00_00_000:
            print("Turnover exceeds ₹10 crore: tax audit required (digital-transactions threshold).")
        else:
            print("Below ₹10 crore: no audit on turnover alone. An audit can still apply if you opted for "
                  "presumptive taxation (44AD) in the last 5 years and now declare a lower profit or loss "
                  "while total income exceeds the basic exemption — check with a CA.")
        return 0

    if a.cmd == "harvest":
        r = harvest(load_holdings(a.csv), parse_date(a.as_of), a.realized_stcg, a.realized_ltcg)
        if a.json:
            print(json.dumps(r, indent=2, default=str))
            return 0
        print(f"\nHoldings as of {r['as_of']}  (LTCG exemption room left: {inr(r['ltcg_exemption_room_left'])})")
        for x in r["lots"]:
            extra = f", LT in {x['days_to_long_term']}d" if x["term"] == "ST" else ""
            print(f"  {x['symbol']:<12} {x['qty']:>7g} {x['term']}{extra:<12} unrealised {inr(x['unrealised'])}")
        print()
        for i in r["ideas"] or ["No harvesting opportunities found."]:
            print(" •", i)
        print(" ⚠", r["caveat"])
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
