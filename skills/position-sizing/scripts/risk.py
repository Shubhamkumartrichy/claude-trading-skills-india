#!/usr/bin/env python3
"""Risk toolkit for Indian equity & F&O traders: position size, R:R / expectancy,
trailing-stop ladder, Kelly sizing, and trade-journal statistics.

Zero dependencies (Python 3.10+ stdlib).

Sub-commands
  size     python3 risk.py size --capital 500000 --risk-pct 1 --entry 1450 --stop 1410
           python3 risk.py size --capital 500000 --risk-pct 1 --entry 1450 --atr 28 --atr-mult 2
           python3 risk.py size --capital 800000 --risk-pct 1 --entry 24950 --stop 24800 --lot-size 65 --margin-per-lot 165000
  rr       python3 risk.py rr --entry 1800 --stop 1700 --target 1900:50 --target 2000:30 --target 2200:20 --win-rate 45
  trail    python3 risk.py trail --entry 750 --stop 720 --highest-close 820 --atr 14 --swing-low 790 [--sma20 801]
  kelly    python3 risk.py kelly --win-rate 48 --avg-win 9000 --avg-loss 5000
  journal  python3 risk.py journal trades.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys

# Typical worst-case gap for a no-F&O stock that hits its circuit band (price band ±%).
DEFAULT_GAP_PCT = {"largecap": 3.0, "midcap": 5.0, "smallcap": 10.0}


def inr(x: float, decimals: int = 0) -> str:
    neg = x < 0
    s = f"{abs(x):.{decimals}f}"
    whole, _, frac = s.partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return ("-" if neg else "") + "₹" + whole + ("." + frac if frac else "")


# ---------------------------------------------------------------------------
def position_size(capital: float, risk_pct: float, entry: float, stop: float | None = None,
                  atr: float | None = None, atr_mult: float = 2.0, lot_size: int = 1,
                  margin_per_lot: float | None = None, max_alloc_pct: float = 20.0,
                  leverage: float = 1.0, cost_pct: float = 0.25, gap_pct: float | None = None,
                  side: str = "long", max_margin_pct: float = 50.0) -> dict:
    """Fixed-fractional sizing. Risk/share = |entry-stop| + round-trip costs.

    leverage>1 (MTF / intraday margin): the loss at the stop does not depend on leverage, but
    leverage lets the position outgrow your cash, which magnifies gap and margin-call risk. As a
    deliberate safety haircut the risk budget is divided by the leverage (pass 1 to disable).
    lot_size>1 rounds DOWN to whole lots (F&O); with margin_per_lot, lots are also capped so
    margin stays within max_margin_pct of capital.
    """
    notes = []
    if stop is None:
        if not atr:
            raise ValueError("give --stop or --atr")
        stop = entry - atr * atr_mult if side == "long" else entry + atr * atr_mult
        notes.append(f"Stop from {atr_mult}x ATR({atr:g}) = {stop:.2f}")
    per_unit_risk = abs(entry - stop)
    if per_unit_risk <= 0:
        raise ValueError("stop must differ from entry")
    if (side == "long" and stop > entry) or (side == "short" and stop < entry):
        raise ValueError(f"stop is on the wrong side of entry for a {side}")
    cost_per_unit = entry * cost_pct / 100
    effective_risk_pct = risk_pct / max(1.0, leverage)
    if leverage > 1:
        notes.append(f"Leverage {leverage:g}x: risk budget cut from {risk_pct}% to {effective_risk_pct:.2f}%")
    budget = capital * effective_risk_pct / 100
    units = math.floor(budget / (per_unit_risk + cost_per_unit))
    if lot_size > 1:
        lots = units // lot_size
        if margin_per_lot:
            max_lots = int(capital * max_margin_pct / 100 // margin_per_lot)
            if lots > max_lots:
                notes.append(f"Capped at {max_lots} lots so margin stays within {max_margin_pct:g}% of capital "
                             f"(risk budget alone allowed {lots}).")
                lots = max_lots
        units = lots * lot_size
        if lots == 0:
            min_risk = lot_size * (per_unit_risk + cost_per_unit)
            notes.append(f"Even 1 lot ({lot_size}) risks {inr(min_risk)} = {min_risk / capital * 100:.2f}% of "
                         "capital — above budget. Skip, tighten the stop, or use an option spread.")
    else:
        lots = None
    position_value = units * entry
    alloc_cap_value = capital * max_alloc_pct / 100 * (leverage if lot_size == 1 else 1)
    capped = False
    if lot_size == 1 and position_value > alloc_cap_value:
        units = math.floor(alloc_cap_value / entry)
        position_value = units * entry
        capped = True
        notes.append(f"Capped by {max_alloc_pct:g}% single-stock limit")
    capital_needed = position_value / max(1.0, leverage) if lot_size == 1 else (
        (lots or 0) * margin_per_lot if margin_per_lot else None)
    if lot_size > 1 and margin_per_lot and capital_needed and capital_needed > capital * 0.5:
        notes.append("Margin uses over 50% of capital — a margin spike (e.g. expiry week, physical "
                     "settlement) could force a square-off.")
    if lot_size > 1 and margin_per_lot and lots == 0 and margin_per_lot > capital * max_margin_pct / 100:
        notes.append(f"One lot's margin ({inr(margin_per_lot)}) exceeds {max_margin_pct:g}% of capital.")
    risk_amt = units * (per_unit_risk + cost_per_unit)
    gp = gap_pct if gap_pct is not None else None
    gap_loss = None
    if gp and units:
        # Price drifts to the stop, then the next open gaps a further gp% beyond it.
        gap_loss = units * (per_unit_risk + stop * gp / 100 + cost_per_unit)
        notes.append(f"Gap risk: if price gaps {gp:g}% beyond the stop, the loss is {inr(gap_loss)} "
                     f"({gap_loss / capital * 100:.2f}% of capital). Stop orders fill at the open after a gap, "
                     "and not at all while a stock is locked in its lower circuit.")
    return {"side": side, "entry": entry, "stop": stop, "risk_per_unit": per_unit_risk,
            "cost_per_unit": cost_per_unit, "units": units, "lots": lots, "lot_size": lot_size,
            "position_value": position_value, "capital_needed": capital_needed,
            "risk_amount": risk_amt, "risk_pct_of_capital": risk_amt / capital * 100,
            "allocation_pct": position_value / capital * 100, "stop_distance_pct": per_unit_risk / entry * 100,
            "capped_by_allocation": capped, "gap_loss": gap_loss, "notes": notes}


# ---------------------------------------------------------------------------
def rr_analysis(entry: float, stop: float, targets: list, win_rate: float | None = None,
                cost_pct: float = 0.25) -> dict:
    """targets: list of (price, weight%). Weights are normalised."""
    if not targets:
        raise ValueError("give at least one target")
    risk = abs(entry - stop)
    if risk <= 0:
        raise ValueError("stop must differ from entry")
    long = stop < entry
    if any((p <= entry) if long else (p >= entry) for p, _ in targets):
        raise ValueError("every target must be beyond entry, on the opposite side from the stop")
    cost = entry * cost_pct / 100
    tw = sum(w for _, w in targets) or 1
    rows, weighted, weighted_net = [], 0.0, 0.0
    for price, w in targets:
        reward = (price - entry) if long else (entry - price)
        r = reward / risk
        r_net = (reward - cost) / (risk + cost)
        rows.append({"target": price, "weight_pct": w / tw * 100, "R": r, "R_after_costs": r_net})
        weighted += r * w / tw
        weighted_net += r_net * w / tw
    breakeven_wr = 1 / (1 + weighted_net) * 100 if weighted_net > 0 else 100.0
    out = {"direction": "long" if long else "short", "risk_per_unit": risk, "targets": rows,
           "weighted_R": weighted, "weighted_R_after_costs": weighted_net,
           "breakeven_win_rate_pct": breakeven_wr}
    if win_rate is not None:
        w = win_rate / 100
        out["expectancy_R"] = w * weighted_net - (1 - w)
        out["verdict"] = ("positive edge" if out["expectancy_R"] > 0.1 else
                          "marginal" if out["expectancy_R"] > 0 else "negative — skip")
    r = weighted_net
    out["filter"] = ("skip (<1:1)" if r < 1 else "only with >55% win rate" if r < 1.5 else
                     "acceptable" if r < 2 else "good" if r < 3 else "excellent")
    return out


# ---------------------------------------------------------------------------
def trail(entry: float, stop: float, highest_close: float, atr: float | None = None,
          swing_low: float | None = None, highest_high: float | None = None,
          sma20: float | None = None, ema50: float | None = None, current_stop: float | None = None,
          side: str = "long") -> dict:
    """Hybrid ladder: <1R fixed | 1R breakeven | 2R ATR trail (1.5x) | 3R+ structure trail.
    Long side only for structure/MA logic; short side mirrors ATR/breakeven."""
    r1 = abs(entry - stop)
    if r1 <= 0:
        raise ValueError("initial stop must differ from entry")
    sign = 1 if side == "long" else -1
    gained_r = sign * (highest_close - entry) / r1
    cands = {"initial": stop}
    stage = "0-1R: hold initial stop"
    if gained_r >= 1:
        cands["breakeven"] = entry
        stage = "1R+: stop to breakeven"
    if gained_r >= 2 and atr:
        cands["atr_trail_1.5x"] = highest_close - sign * 1.5 * atr
        stage = "2R+: ATR trail (highest close − 1.5×ATR)"
    if gained_r >= 3 and swing_low is not None and side == "long":
        cands["structure"] = swing_low
        stage = "3R+: structure trail (below latest higher low)"
    if atr and highest_high:
        cands["chandelier_3x (reference)"] = highest_high - sign * 3 * atr
    if sma20:
        cands["sma20 (reference)"] = sma20
    if ema50:
        cands["ema50 (reference)"] = ema50
    active_keys = [k for k in cands if "(reference)" not in k]
    pick = max if side == "long" else min
    recommended = pick(cands[k] for k in active_keys)
    if current_stop is not None:
        recommended = pick(recommended, current_stop)  # ratchet: never loosen
    locked = sign * (recommended - entry) / r1
    return {"R_gained": gained_r, "stage": stage, "candidates": cands, "recommended_stop": recommended,
            "R_locked": locked, "rule": "Ratchet only; evaluate on daily close; exit next open if closed beyond stop."}


# ---------------------------------------------------------------------------
def kelly(win_rate: float, avg_win: float, avg_loss: float) -> dict:
    if avg_win <= 0 or avg_loss <= 0:
        raise ValueError("average win and average loss must both be positive")
    if not 0 <= win_rate <= 100:
        raise ValueError("win rate is a percentage between 0 and 100")
    w = win_rate / 100
    b = avg_win / avg_loss
    k = w - (1 - w) / b
    return {"win_rate": win_rate, "payoff_ratio": b, "kelly_pct": k * 100, "half_kelly_pct": k * 50,
            "quarter_kelly_pct": k * 25,
            "advice": ("No edge — do not trade this system." if k <= 0 else
                       "Kelly is the fraction of capital to RISK per trade. Use ≤ half-Kelly and cap at 2%.")}


def journal(path: str) -> dict:
    trades = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            r = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
            if r.get("pnl"):
                pnl = float(r["pnl"].replace(",", ""))
            else:
                qty = float(r.get("qty", 0) or 0)
                e, x = float(r.get("entry", 0) or 0), float(r.get("exit", 0) or 0)
                pnl = (x - e) * qty * (-1 if r.get("side", "long").lower().startswith("s") else 1)
                pnl -= float(r.get("charges", 0) or 0)
            risk = None
            if r.get("stop") and r.get("entry") and r.get("qty"):
                risk = abs(float(r["entry"]) - float(r["stop"])) * float(r["qty"])
            trades.append({"date": r.get("date", ""), "symbol": r.get("symbol", ""), "pnl": pnl,
                           "R": pnl / risk if risk else None})
    if not trades:
        raise ValueError("no trades found (need pnl, or entry/exit/qty columns)")
    wins = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses = [t["pnl"] for t in trades if t["pnl"] <= 0]
    equity, peak, dd = 0.0, 0.0, 0.0
    streak = max_ls = 0
    for t in trades:
        equity += t["pnl"]
        peak = max(peak, equity)
        dd = min(dd, equity - peak)
        streak = streak + 1 if t["pnl"] <= 0 else 0
        max_ls = max(max_ls, streak)
    rs = [t["R"] for t in trades if t["R"] is not None]
    n = len(trades)
    wr = len(wins) / n * 100
    aw = sum(wins) / len(wins) if wins else 0.0
    al = -sum(losses) / len(losses) if losses else 0.0
    out = {"trades": n, "win_rate_pct": wr, "avg_win": aw, "avg_loss": al,
           "payoff_ratio": aw / al if al else None, "net_pnl": sum(t["pnl"] for t in trades),
           "profit_factor": sum(wins) / -sum(losses) if losses and sum(losses) else None,
           "expectancy_per_trade": sum(t["pnl"] for t in trades) / n,
           "avg_R": sum(rs) / len(rs) if rs else None, "max_drawdown": dd, "max_losing_streak": max_ls,
           "sample_warning": n < 30}
    if al and aw:
        out["kelly"] = kelly(wr, aw, al)
    return out


# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Risk toolkit")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("size")
    s.add_argument("--capital", type=float, required=True)
    s.add_argument("--risk-pct", type=float, default=1.0)
    s.add_argument("--entry", type=float, required=True)
    s.add_argument("--stop", type=float)
    s.add_argument("--atr", type=float)
    s.add_argument("--atr-mult", type=float, default=2.0)
    s.add_argument("--side", choices=["long", "short"], default="long")
    s.add_argument("--lot-size", type=int, default=1)
    s.add_argument("--margin-per-lot", type=float)
    s.add_argument("--max-margin-pct", type=float, default=50.0, help="cap F&O margin at this %% of capital")
    s.add_argument("--max-alloc-pct", type=float, default=20.0)
    s.add_argument("--leverage", type=float, default=1.0, help="MTF/intraday leverage multiple")
    s.add_argument("--cost-pct", type=float, default=0.25, help="round-trip cost %% (delivery≈0.25, intraday≈0.05)")
    s.add_argument("--cap", choices=list(DEFAULT_GAP_PCT), help="market-cap bucket for gap-risk check")
    s.add_argument("--gap-pct", type=float, help="override overnight gap %% for gap-risk check")
    s.add_argument("--json", action="store_true")

    r = sub.add_parser("rr")
    r.add_argument("--entry", type=float, required=True)
    r.add_argument("--stop", type=float, required=True)
    r.add_argument("--target", action="append", required=True, help="PRICE[:WEIGHT%%]")
    r.add_argument("--win-rate", type=float)
    r.add_argument("--cost-pct", type=float, default=0.25)
    r.add_argument("--json", action="store_true")

    t = sub.add_parser("trail")
    for name in ("entry", "stop", "highest-close"):
        t.add_argument(f"--{name}", type=float, required=True)
    for name in ("atr", "swing-low", "highest-high", "sma20", "ema50", "current-stop"):
        t.add_argument(f"--{name}", type=float)
    t.add_argument("--side", choices=["long", "short"], default="long")
    t.add_argument("--json", action="store_true")

    k = sub.add_parser("kelly")
    k.add_argument("--win-rate", type=float, required=True)
    k.add_argument("--avg-win", type=float, required=True)
    k.add_argument("--avg-loss", type=float, required=True)

    j = sub.add_parser("journal")
    j.add_argument("csv")
    j.add_argument("--json", action="store_true")
    a = p.parse_args(argv)

    try:
        if a.cmd == "size":
            gap = a.gap_pct if a.gap_pct is not None else (DEFAULT_GAP_PCT.get(a.cap) if a.cap else None)
            res = position_size(a.capital, a.risk_pct, a.entry, a.stop, a.atr, a.atr_mult, a.lot_size,
                                a.margin_per_lot, a.max_alloc_pct, a.leverage, a.cost_pct, gap, a.side,
                                a.max_margin_pct)
            if a.json:
                print(json.dumps(res, indent=2))
                return 0
            unit = "lots" if res["lots"] is not None else "shares"
            qty = f"{res['lots']} lots ({res['units']} qty)" if res["lots"] is not None else f"{res['units']} shares"
            print(f"\nPosition size ({res['side']}): {qty}")
            print(f"  Entry {res['entry']:,.2f}  Stop {res['stop']:,.2f}  ({res['stop_distance_pct']:.2f}% away)")
            print(f"  Position value   : {inr(res['position_value'])} ({res['allocation_pct']:.1f}% of capital)")
            if res["capital_needed"] is not None:
                print(f"  Capital/margin   : {inr(res['capital_needed'])}")
            print(f"  Risk if stopped  : {inr(res['risk_amount'])} ({res['risk_pct_of_capital']:.2f}% of capital, incl. costs)")
            if res["gap_loss"]:
                print(f"  Gap-through loss : {inr(res['gap_loss'])} ({res['gap_loss'] / a.capital * 100:.2f}% of capital)")
            for n in res["notes"]:
                print("  •", n)
            return 0
        if a.cmd == "rr":
            tg = []
            for spec in a.target:
                price, _, w = spec.partition(":")
                tg.append((float(price), float(w) if w else 100.0))
            res = rr_analysis(a.entry, a.stop, tg, a.win_rate, a.cost_pct)
            if a.json:
                print(json.dumps(res, indent=2))
                return 0
            print(f"\nR:R ({res['direction']}) — risk {res['risk_per_unit']:,.2f}/unit")
            for x in res["targets"]:
                print(f"  Target {x['target']:,.2f} ({x['weight_pct']:.0f}%): {x['R']:.2f}R  (after costs {x['R_after_costs']:.2f}R)")
            print(f"  Weighted: {res['weighted_R']:.2f}R  | after costs {res['weighted_R_after_costs']:.2f}R → {res['filter']}")
            print(f"  Breakeven win rate: {res['breakeven_win_rate_pct']:.1f}%")
            if "expectancy_R" in res:
                print(f"  Expectancy @ {a.win_rate}% wins: {res['expectancy_R']:+.2f}R per trade → {res['verdict']}")
            return 0
        if a.cmd == "trail":
            res = trail(a.entry, a.stop, a.highest_close, a.atr, a.swing_low, a.highest_high, a.sma20,
                        a.ema50, a.current_stop, a.side)
            if a.json:
                print(json.dumps(res, indent=2))
                return 0
            print(f"\nGained {res['R_gained']:.2f}R → {res['stage']}")
            for kk, v in res["candidates"].items():
                print(f"  {kk:<28} {v:,.2f}")
            print(f"Recommended stop: {res['recommended_stop']:,.2f} (locks {res['R_locked']:+.2f}R)")
            print(res["rule"])
            return 0
        if a.cmd == "kelly":
            res = kelly(a.win_rate, a.avg_win, a.avg_loss)
            print(f"Kelly {res['kelly_pct']:.1f}% | half {res['half_kelly_pct']:.1f}% | quarter "
                  f"{res['quarter_kelly_pct']:.1f}% (payoff {res['payoff_ratio']:.2f})\n{res['advice']}")
            return 0
        if a.cmd == "journal":
            res = journal(a.csv)
            if a.json:
                print(json.dumps(res, indent=2))
                return 0
            print(f"\nTrades {res['trades']} | win rate {res['win_rate_pct']:.1f}% | net {inr(res['net_pnl'])}")
            print(f"Avg win {inr(res['avg_win'])} | avg loss {inr(res['avg_loss'])}"
                  + (f" | payoff {res['payoff_ratio']:.2f}" if res["payoff_ratio"] else ""))
            if res["profit_factor"]:
                print(f"Profit factor {res['profit_factor']:.2f} | expectancy {inr(res['expectancy_per_trade'])}/trade")
            if res["avg_R"] is not None:
                print(f"Average R {res['avg_R']:+.2f}")
            print(f"Max drawdown {inr(res['max_drawdown'])} | longest losing streak {res['max_losing_streak']}")
            if "kelly" in res:
                kk = res["kelly"]
                print(f"Kelly {kk['kelly_pct']:.1f}% → half-Kelly {kk['half_kelly_pct']:.1f}% risk per trade (cap at 2%)")
            if res["sample_warning"]:
                print("⚠ Fewer than 30 trades — statistics are unreliable.")
            return 0
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
