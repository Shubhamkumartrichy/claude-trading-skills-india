#!/usr/bin/env python3
"""Trading-discipline toolkit for Indian equity and F&O traders.

Reads the same trade-journal CSV as ``position-sizing/scripts/risk.py journal``
(date,symbol,side,entry,exit,stop,qty,charges, or a pnl column) plus optional
columns: exit_date, time (HH:MM, IST), setup/tag, mistake, segment, notes.
A row with a blank exit and blank pnl is an OPEN trade. A zero-P&L trade is a
breakeven, never a loss.

Sub-commands
  breaker  python3 discipline.py breaker trades.csv --capital 500000 [--as-of 2026-10-29]
  gate     python3 discipline.py gate --capital 500000 --entry 1450 --stop 1410 --target 1550 \
               --qty 120 --plan-written yes --minutes-since-last-loss 90 [--journal trades.csv]
  review   python3 discipline.py review trades.csv [--recent 10] [--week [2026-W43]]
  log      python3 discipline.py log --log decisions.csv --symbol INFY <same flags as gate>

Python 3.10+ standard library only. Nothing is fetched; nothing is written
except by ``log``.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import sys

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
EPS = 0.005  # half a paisa: anything smaller is a breakeven

# Column-name aliases -> canonical name.
ALIASES = {
    "tag": "setup", "setup_tag": "setup", "strategy": "setup", "setup/tag": "setup",
    "mistakes": "mistake", "error": "mistake",
    "close_date": "exit_date", "exitdate": "exit_date", "date_closed": "exit_date",
    "entry_date": "date", "trade_date": "date",
    "entry_time": "time", "product": "segment", "note": "notes",
    "price": "entry", "sl": "stop", "stoploss": "stop", "stop_loss": "stop", "quantity": "qty",
}
SEGMENTS = {
    "fno": {"fno", "f&o", "fo", "nfo", "bfo", "derivatives", "futures", "future", "fut", "options",
            "option", "opt", "options-buy", "options-sell", "option-buy", "option-sell"},
    "intraday": {"intraday", "intra", "mis"},
    "equity": {"equity", "delivery", "cnc", "swing", "positional", "cash", "eq", "mtf", "btst"},
}


# --------------------------------------------------------------------------- formatting
def inr(x: float | None, signed: bool = False) -> str:
    """Indian digit grouping: 1234567 -> ₹12,34,567."""
    if x is None:
        return "n/a"
    neg = x < 0
    whole = f"{abs(x):.0f}"
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts + [tail])
    sign = "-" if neg else ("+" if signed and x > 0 else "")
    return f"{sign}₹{whole}"


def pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.1f}%"


def num(x: float | None, fmt: str = "+.2f") -> str:
    return "n/a" if x is None else format(x, fmt)


# --------------------------------------------------------------------------- parsing
def parse_date(text: str, what: str = "date") -> dt.date:
    text = (text or "").strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y", "%d %b %Y", "%Y/%m/%d"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"{what} {text!r} is not a date (use YYYY-MM-DD)")


def parse_hhmm(text: str, what: str = "time") -> dt.time:
    try:
        return dt.datetime.strptime(text.strip(), "%H:%M").time()
    except ValueError:
        raise ValueError(f"{what} {text!r} is not HH:MM (24-hour, IST)") from None


def _num(value: str, field: str, rownum: int) -> float | None:
    v = (value or "").replace(",", "").replace("₹", "").strip()
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        raise ValueError(f"row {rownum}: {field} {value!r} is not a number") from None


def _segment(raw: str) -> str:
    s = (raw or "").strip().lower()
    if not s:
        return "unspecified"
    for name, words in SEGMENTS.items():
        if s in words:
            return name
    return s


def _canon(key: str) -> str:
    k = key.strip().lower().replace(" ", "_")
    return ALIASES.get(k, ALIASES.get(k.replace("_", "/"), k))


def load_journal(path: str) -> list[dict]:
    """Parse a journal CSV into trade dicts, sorted by close date (stable)."""
    if not os.path.isfile(path):
        raise ValueError(f"journal not found: {path}")
    trades = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            raise ValueError(f"{path} is empty")
        for i, raw in enumerate(reader, start=2):  # row 1 is the header
            r = {_canon(k): (v or "").strip() for k, v in raw.items() if k}
            if not any(r.values()):
                continue
            if not r.get("date"):
                raise ValueError(f"row {i}: missing date")
            entry_day = parse_date(r["date"], f"row {i}: date")
            exit_day = parse_date(r["exit_date"], f"row {i}: exit_date") if r.get("exit_date") else None
            entry, exit_, stop = (_num(r.get(f, ""), f, i) for f in ("entry", "exit", "stop"))
            qty, charges, pnl = (_num(r.get(f, ""), f, i) for f in ("qty", "charges", "pnl"))
            side = "short" if r.get("side", "long").lower().startswith("s") else "long"
            if pnl is None and exit_ is not None:
                if entry is None or qty is None:
                    raise ValueError(f"row {i}: need entry and qty (or a pnl value)")
                pnl = (exit_ - entry) * qty * (1 if side == "long" else -1) - (charges or 0.0)
            hhmm = None
            if r.get("time"):
                hhmm = parse_hhmm(r["time"], f"row {i}: time")
            risk = None
            if entry is not None and stop is not None and qty:
                risk = abs(entry - stop) * qty or None
            closed = pnl is not None
            close_day = (exit_day or entry_day) if closed else None
            outcome = None
            if closed:
                outcome = "win" if pnl > EPS else "loss" if pnl < -EPS else "breakeven"
            mistakes = [m.strip().lower() for m in
                        r.get("mistake", "").replace("|", ";").replace("/", ";").split(";") if m.strip()]
            trades.append({
                "row": i, "date": entry_day, "exit_date": exit_day, "close_date": close_day,
                "time": hhmm.strftime("%H:%M") if hhmm else None, "hour": hhmm.hour if hhmm else None,
                "symbol": r.get("symbol", ""), "side": side, "entry": entry, "exit": exit_, "stop": stop,
                "qty": qty, "charges": charges, "pnl": pnl, "risk": risk,
                "r": (pnl / risk) if (closed and risk) else None,
                "status": "closed" if closed else "open", "outcome": outcome,
                "setup": r.get("setup", "").lower() or "untagged", "segment": _segment(r.get("segment", "")),
                "mistakes": mistakes, "notes": r.get("notes", ""),
                "holding_days": (exit_day - entry_day).days if (closed and exit_day) else None,
            })
    if not trades:
        raise ValueError(f"no trades in {path}")
    trades.sort(key=lambda t: (t["close_date"] or t["date"], t["time"] or "", t["row"]))
    return trades


def closed_trades(trades: list[dict]) -> list[dict]:
    return [t for t in trades if t["status"] == "closed"]


# --------------------------------------------------------------------------- statistics
def stats(trades: list[dict]) -> dict:
    """Realised statistics over CLOSED trades. Breakevens count as trades, not as losses."""
    c = closed_trades(trades)
    n = len(c)
    wins = [t["pnl"] for t in c if t["outcome"] == "win"]
    losses = [t["pnl"] for t in c if t["outcome"] == "loss"]
    rs = [t["r"] for t in c if t["r"] is not None]
    gp, gl = sum(wins), -sum(losses)
    equity = peak = dd = 0.0
    run = longest = 0
    for t in c:
        equity += t["pnl"]
        peak = max(peak, equity)
        dd = min(dd, equity - peak)
        if t["outcome"] == "loss":
            run += 1
            longest = max(longest, run)
        elif t["outcome"] == "win":
            run = 0  # breakevens neither extend nor reset a streak
    net = sum(t["pnl"] for t in c)
    aw = gp / len(wins) if wins else None
    al = gl / len(losses) if losses else None
    return {
        "trades": n, "wins": len(wins), "losses": len(losses), "breakevens": n - len(wins) - len(losses),
        "win_rate_pct": len(wins) / n * 100 if n else None, "net_pnl": net,
        "gross_profit": gp, "gross_loss": gl, "avg_win": aw, "avg_loss": al,
        "payoff_ratio": aw / al if (aw and al) else None,
        "profit_factor": gp / gl if gl else None,
        "expectancy": net / n if n else None,
        "avg_R": sum(rs) / len(rs) if rs else None, "trades_with_R": len(rs),
        "max_drawdown": dd, "longest_losing_streak": longest,
    }


def group(trades: list[dict], key) -> dict:
    buckets: dict = {}
    for t in closed_trades(trades):
        for k in key(t):
            buckets.setdefault(k, []).append(t)
    return {k: _brief(v) for k, v in buckets.items()}


def _brief(ts: list[dict]) -> dict:
    s = stats(ts)
    return {k: s[k] for k in ("trades", "wins", "losses", "breakevens", "win_rate_pct", "net_pnl",
                              "expectancy", "avg_R", "profit_factor")}


# --------------------------------------------------------------------------- breaker
def next_weekday(d: dt.date) -> dt.date:
    d += dt.timedelta(days=1)
    while d.weekday() >= 5:
        d += dt.timedelta(days=1)
    return d


def breaker(trades: list[dict], capital: float, as_of: dt.date, risk_pct: float = 1.0,
            daily_pct: float = 2.0, weekly_pct: float = 5.0, monthly_pct: float = 10.0,
            stop_streak: int = 3, reduce_streak: int = 5, reduce_trades: int = 5,
            reduce_factor: float = 0.5) -> dict:
    """Account-level circuit breaker. Only trades closed on or before ``as_of`` count."""
    if capital <= 0:
        raise ValueError("capital must be positive")
    if stop_streak < 1 or reduce_streak < 1 or reduce_trades < 0:
        raise ValueError("streak settings must be positive")
    past = [t for t in closed_trades(trades) if t["close_date"] <= as_of]
    future = sum(1 for t in closed_trades(trades) if t["close_date"] > as_of)
    open_ = [t for t in trades if t["status"] == "open" and t["date"] <= as_of]

    week_start = as_of - dt.timedelta(days=as_of.weekday())
    month_start = as_of.replace(day=1)
    periods = []
    for name, start, limit_pct, release in (
            ("day", as_of, daily_pct, next_weekday(as_of)),
            ("week", week_start, weekly_pct, week_start + dt.timedelta(days=7)),
            ("month", month_start, monthly_pct,
             (month_start + dt.timedelta(days=32)).replace(day=1))):
        pnl = sum(t["pnl"] for t in past if t["close_date"] >= start)
        limit = capital * limit_pct / 100
        used = max(0.0, -pnl)
        periods.append({"period": name, "from": start, "realised_pnl": pnl, "limit": limit,
                        "limit_pct": limit_pct, "used_pct_of_limit": used / limit * 100 if limit else 0.0,
                        "room": max(0.0, limit - used), "tripped": used >= limit - EPS,
                        "resumes": release})

    # Losing streak (breakevens are neutral) and the 50%-size recovery window.
    streak, anchor = 0, None
    for i, t in enumerate(past):
        if t["outcome"] == "loss":
            streak += 1
            if streak >= reduce_streak:
                anchor = i
        elif t["outcome"] == "win":
            streak = 0
    since_anchor = len(past) - 1 - anchor if anchor is not None else None
    reduce_left = max(0, reduce_trades - since_anchor) if anchor is not None else 0
    # The streak "ends today" if the most recent non-breakeven trade is today's loss.
    last_decided = next((t for t in reversed(past) if t["outcome"] != "breakeven"), None)
    streak_today = (streak >= stop_streak and last_decided is not None
                    and last_decided["close_date"] == as_of)

    open_risk = sum(t["risk"] for t in open_ if t["risk"])
    open_no_stop = [t["symbol"] or f"row {t['row']}" for t in open_ if not t["risk"]]

    base = capital * risk_pct / 100
    mult = reduce_factor if reduce_left > 0 else 1.0
    room = min(p["room"] for p in periods)
    allowed = max(0.0, min(base * mult, room - open_risk))

    red, amber = [], []
    for p in periods:
        if p["tripped"]:
            red.append(f"{p['period'].replace('day', 'daily').capitalize()} loss limit hit: realised {inr(p['realised_pnl'], True)} "
                       f"vs limit -{inr(p['limit'])} ({p['limit_pct']:g}% of capital). "
                       f"No new trades until {p['resumes']:%a %d %b %Y}.")
        elif p["used_pct_of_limit"] >= 50:
            amber.append(f"{p['period'].capitalize()} loss is {p['used_pct_of_limit']:.0f}% of its limit "
                         f"({inr(p['room'])} room left).")
    if streak_today:
        red.append(f"{streak} losses in a row, the latest today. Stop for the day; "
                   f"resume {next_weekday(as_of):%a %d %b %Y} after reviewing the losses.")
    elif streak >= stop_streak:
        amber.append(f"Losing streak of {streak} is still open. First trade back: only your best setup.")
    if reduce_left > 0:
        amber.append(f"After a {reduce_streak}+ loss streak, risk is cut to {reduce_factor:.0%} for "
                     f"{reduce_left} more trade(s).")
    if open_risk and allowed < base * mult:
        amber.append(f"Open trades already risk {inr(open_risk)} against the remaining room.")
    if open_no_stop:
        amber.append(f"Open trade(s) without a stop: {', '.join(open_no_stop)}. Their risk is unknown.")
    if not red and allowed <= EPS:
        red.append("No loss room left once open-trade risk is counted.")
    if not red and allowed < base * mult - EPS:
        amber.append(f"Next-trade risk capped at {inr(allowed)} so a stop-out cannot breach a limit.")
    status = "RED" if red else "AMBER" if amber else "GREEN"
    if status == "RED":
        allowed = 0.0
    return {
        "status": status, "as_of": as_of, "capital": capital, "base_risk": base, "risk_pct": risk_pct,
        "size_multiplier": mult, "allowed_risk": allowed,
        "allowed_risk_pct": allowed / capital * 100, "periods": periods,
        "losing_streak": streak, "stop_streak": stop_streak, "reduce_streak": reduce_streak,
        "reduced_trades_left": reduce_left, "open_trades": len(open_), "open_risk": open_risk,
        "open_without_stop": open_no_stop, "closed_trades_counted": len(past),
        "ignored_after_as_of": future, "reasons": red + amber,
    }


# --------------------------------------------------------------------------- gate
REGIME_BLOCK = ("crash", "panic", "capitulation", "no-trade", "notrade", "cash")
REGIME_BEAR = ("risk-off", "riskoff", "bear", "downtrend", "correction", "distribution", "defensive")
REGIME_BULL = ("risk-on", "riskon", "bull", "uptrend", "expansion")
REGIME_CHOP = ("sideways", "range", "chop", "volatile", "high-vol", "transition", "mixed", "neutral",
               "cautious", "under-pressure", "rally-attempt")
FNO_SEGMENTS = ("futures", "options-buy", "options-sell")


def _item(items: list, name: str, status: str, detail: str) -> None:
    items.append({"check": name, "status": status, "detail": detail})


def gate(entry: float, stop: float | None = None, target: float | None = None, qty: float | None = None,
         capital: float | None = None, risk_pct_limit: float = 1.0, side: str | None = None,
         segment: str = "equity", charges: float = 0.0, regime: str | None = None,
         minutes_since_last_loss: float | None = None, revenge_minutes: float = 30.0,
         plan_written: bool | None = None, fno_ban: bool | None = None, surveillance: bool | None = None,
         event_in_window: bool | None = None, circuit_locked: bool | None = None,
         expiry_day: bool | None = None, now: dt.time | None = None, as_of: dt.date | None = None,
         breaker_result: dict | None = None, journal_trades: list[dict] | None = None) -> dict:
    """Evaluate the pre-trade checklist. Returns verdict PASS / PASS-WITH-WARNINGS / BLOCK."""
    if entry is None or entry <= 0:
        raise ValueError("--entry must be a positive price")
    if qty is not None and qty <= 0:
        raise ValueError("--qty must be positive")
    if capital is not None and capital <= 0:
        raise ValueError("--capital must be positive")
    items: list = []
    is_fno = segment in FNO_SEGMENTS
    if side is None:
        side = "short" if (stop is not None and stop > entry) else "long"

    # 1. Stop-loss
    unit_risk = None
    if stop is None:
        if segment == "options-buy":
            unit_risk = entry
            _item(items, "stop-loss", "WARN", "No premium stop: the whole premium is at risk. Size on that, "
                  "or set a premium stop (for example -40%).")
        else:
            _item(items, "stop-loss", "BLOCK", "No stop-loss. Decide the exit-if-wrong price before entry.")
    elif stop == entry or (side == "long" and stop > entry) or (side == "short" and stop < entry):
        _item(items, "stop-loss", "BLOCK", f"Stop {stop:g} is on the wrong side of entry {entry:g} for a {side}.")
    else:
        unit_risk = abs(entry - stop)
        _item(items, "stop-loss", "PASS", f"Stop {stop:g} ({unit_risk / entry * 100:.2f}% from entry).")

    # 2. Rupee risk vs per-trade limit and the circuit breaker's allowance
    risk = None
    if unit_risk is not None and qty and capital:
        risk = unit_risk * qty + (charges or 0.0)
        rp = risk / capital * 100
        if rp > risk_pct_limit + 1e-9:
            _item(items, "risk-size", "BLOCK", f"Risk {inr(risk)} = {rp:.2f}% of capital, above the "
                  f"{risk_pct_limit:g}% limit. Cut qty to {int((capital * risk_pct_limit / 100 - (charges or 0)) // unit_risk)}.")
        else:
            _item(items, "risk-size", "PASS", f"Risk {inr(risk)} = {rp:.2f}% of capital (limit {risk_pct_limit:g}%).")
    elif unit_risk is not None:
        _item(items, "risk-size", "BLOCK", "Rupee risk unknown: give --qty and --capital (size it with "
              "position-sizing first).")

    # 3. Target / reward-to-risk
    if target is None:
        _item(items, "target", "WARN", "No target or exit plan for the winning case.")
    elif unit_risk:
        reward = (target - entry) if side == "long" else (entry - target)
        if reward <= 0:
            _item(items, "target", "BLOCK", f"Target {target:g} is not beyond entry for a {side}.")
        else:
            rr = reward / unit_risk
            st = "BLOCK" if rr < 1 else "WARN" if rr < 1.5 else "PASS"
            _item(items, "target", st, f"Reward:risk {rr:.2f}:1" + (" (below 1:1)" if rr < 1 else
                                                                   " (thin after costs)" if rr < 1.5 else ""))

    # 4. Written plan
    if plan_written is None:
        _item(items, "written-plan", "WARN", "Plan not confirmed. Write entry, stop, target and size first.")
    else:
        _item(items, "written-plan", "PASS" if plan_written else "BLOCK",
              "Plan written down." if plan_written else "No written plan: an unplanned trade is an impulse.")

    # 5. Revenge-trade window
    if minutes_since_last_loss is not None:
        if minutes_since_last_loss < revenge_minutes:
            _item(items, "revenge-window", "BLOCK", f"Only {minutes_since_last_loss:g} min since the last loss "
                  f"(cool-off {revenge_minutes:g} min). Step away before the next order.")
        else:
            _item(items, "revenge-window", "PASS", f"{minutes_since_last_loss:g} min since the last loss.")
    elif journal_trades and as_of:
        today = [t for t in closed_trades(journal_trades) if t["close_date"] == as_of]
        if today and today[-1]["outcome"] == "loss":
            _item(items, "revenge-window", "WARN", "Your last trade today was a loss. Confirm at least "
                  f"{revenge_minutes:g} min have passed (--minutes-since-last-loss).")

    # 6. Market regime (label from the market-regime skill)
    if regime:
        lab = regime.strip().lower().replace("_", "-").replace(" ", "-")
        if any(w in lab for w in REGIME_BLOCK):
            st = "BLOCK" if side == "long" else "WARN"
            _item(items, "regime", st, f"Regime '{regime}': stand aside from new longs; protect capital.")
        elif any(w in lab for w in REGIME_BEAR):
            _item(items, "regime", "WARN" if side == "long" else "PASS",
                  f"Regime '{regime}': " + ("long against the tide; halve size, fewer trades."
                                            if side == "long" else "short with the tide."))
        elif any(w in lab for w in REGIME_CHOP):
            # Checked before BULL so market-regime's "Uptrend under pressure" is not read as a clean uptrend.
            _item(items, "regime", "WARN", f"Regime '{regime}': breakouts fail more often; be selective, "
                  "smaller size.")
        elif any(w in lab for w in REGIME_BULL):
            _item(items, "regime", "WARN" if side == "short" else "PASS",
                  f"Regime '{regime}': " + ("short against the tide; halve size." if side == "short"
                                            else "long with the tide."))
        else:
            _item(items, "regime", "WARN", f"Regime label '{regime}' not recognised; judge it manually.")

    # 7. India-specific checks
    if fno_ban:
        _item(items, "fno-ban", "BLOCK" if is_fno else "WARN",
              "Stock is in the F&O ban period: only position-reducing F&O trades are allowed; fresh "
              "positions are penalised." if is_fno else
              "Stock is in the F&O ban period: open interest is crowded, expect sharp moves.")
    elif fno_ban is None and is_fno:
        _item(items, "fno-ban", "WARN", "Ban status not confirmed. Check today's NSE ban list.")
    if surveillance:
        _item(items, "asm-gsm-t2t", "BLOCK" if segment == "intraday" else "WARN",
              "ASM/GSM/T2T stock: intraday square-off not allowed in T2T/GSM, margins up to 100%."
              if segment == "intraday" else
              "ASM/GSM/T2T stock: tight bands, high margin, thin exits. Reduce size or skip.")
    if event_in_window:
        _item(items, "event-risk", "WARN", "Results/board meeting inside the holding window: overnight gap "
              "can jump the stop" + (" and IV can collapse after the event." if segment.startswith("options")
                                     else ". Halve size or wait until after the event."))
    if circuit_locked:
        _item(items, "circuit", "BLOCK", "Stock is locked at its circuit limit: orders may not fill and the "
              "stop cannot protect you.")
    weekday = as_of.weekday() if as_of else None
    if segment == "options-buy":
        if expiry_day:
            _item(items, "expiry-day", "WARN", "Expiry-day option buy: time decay is fastest today and "
                  "out-of-the-money premium often ends at zero. Small size, hard premium stop.")
        elif expiry_day is None and weekday in (1, 3):
            _item(items, "expiry-day", "WARN", f"Today is {WEEKDAYS[weekday]} ("
                  f"{'NIFTY' if weekday == 1 else 'SENSEX'} weekly expiry). Confirm with --expiry-day.")
    elif segment == "options-sell" and expiry_day:
        _item(items, "expiry-day", "WARN", "Expiry-day option sell: extra 2% ELM on shorts and gamma risk.")
    if segment == "intraday" and now is not None:
        if now >= dt.time(15, 0):
            _item(items, "intraday-cutoff", "BLOCK", f"{now:%H:%M} IST: brokers auto square-off MIS around "
                  "15:15-15:20. Too little time for the trade to work.")
        elif now < dt.time(9, 15):
            _item(items, "intraday-cutoff", "WARN", f"{now:%H:%M} IST is before the 09:15 open.")
        else:
            _item(items, "intraday-cutoff", "PASS", f"{now:%H:%M} IST, before the 15:00 cut-off.")

    # 8. Circuit breaker from the journal
    if breaker_result:
        b = breaker_result
        reason = "; ".join(b["reasons"][:2]) or "no limits near."
        if b["status"] == "RED":
            _item(items, "circuit-breaker", "BLOCK", f"Breaker RED: {reason}")
        else:
            if risk is not None and risk > b["allowed_risk"] + EPS:
                _item(items, "circuit-breaker", "BLOCK", f"Breaker {b['status']} allows {inr(b['allowed_risk'])} "
                      f"risk; this trade risks {inr(risk)}. {reason}")
            else:
                _item(items, "circuit-breaker", "WARN" if b["status"] == "AMBER" else "PASS",
                      f"Breaker {b['status']}: allowed risk {inr(b['allowed_risk'])}. {reason}")

    sts = {i["status"] for i in items}
    verdict = "BLOCK" if "BLOCK" in sts else "PASS-WITH-WARNINGS" if "WARN" in sts else "PASS"
    return {"verdict": verdict, "side": side, "segment": segment, "entry": entry, "stop": stop,
            "target": target, "qty": qty, "risk": risk,
            "risk_pct": (risk / capital * 100) if (risk is not None and capital) else None,
            "as_of": as_of, "time": now.strftime("%H:%M") if now else None, "regime": regime,
            "items": items, "blocks": [i["check"] for i in items if i["status"] == "BLOCK"],
            "warnings": [i["check"] for i in items if i["status"] == "WARN"]}


# --------------------------------------------------------------------------- review
def review(trades: list[dict], recent: int = 10, as_of: dt.date | None = None) -> dict:
    if as_of:
        trades = [t for t in trades if (t["close_date"] or t["date"]) <= as_of]
    c = closed_trades(trades)
    if not c:
        raise ValueError("no closed trades to review")
    out = {"summary": stats(c)}
    out["by_setup"] = group(c, lambda t: [t["setup"]])
    out["by_weekday"] = group(c, lambda t: [WEEKDAYS[t["date"].weekday()]])
    if any(t["hour"] is not None for t in c):
        out["by_hour"] = group([t for t in c if t["hour"] is not None], lambda t: [f"{t['hour']:02d}:00"])
    segs = {t["segment"] for t in c}
    if segs != {"unspecified"}:
        out["by_segment"] = group(c, lambda t: [t["segment"]])
        fno = [t for t in c if t["segment"] == "fno"]
        cash = [t for t in c if t["segment"] in ("equity", "intraday")]
        if fno and cash:
            out["fno_vs_cash"] = {"fno": _brief(fno), "cash": _brief(cash)}
    tagged = [t for t in c if t["mistakes"]]
    if tagged:
        mist = {}
        for t in tagged:
            for m in t["mistakes"]:
                e = mist.setdefault(m, {"trades": 0, "net_pnl": 0.0, "loss_cost": 0.0})
                e["trades"] += 1
                e["net_pnl"] += t["pnl"]
                e["loss_cost"] += -min(0.0, t["pnl"])
        out["mistakes"] = dict(sorted(mist.items(), key=lambda kv: (-kv[1]["loss_cost"], kv[0])))
        out["clean_vs_mistake"] = {"clean": _brief([t for t in c if not t["mistakes"]]),
                                   "with_mistake": _brief(tagged)}
    held = [t for t in c if t["holding_days"] is not None]
    if held:
        def avg_days(ts):
            return sum(t["holding_days"] for t in ts) / len(ts) if ts else None
        out["holding_days"] = {"winners": avg_days([t for t in held if t["outcome"] == "win"]),
                               "losers": avg_days([t for t in held if t["outcome"] == "loss"]),
                               "all": avg_days(held)}
    if recent > 0 and len(c) >= 2 * recent:
        out["trend"] = {"window": recent, "recent": _brief(c[-recent:]),
                        "prior": _brief(c[-2 * recent:-recent])}
    elif recent > 0 and len(c) >= 4:
        half = len(c) // 2
        out["trend"] = {"window": half, "recent": _brief(c[-half:]), "prior": _brief(c[-2 * half:-half])}
    out["open_trades"] = [{"symbol": t["symbol"], "date": t["date"], "side": t["side"], "entry": t["entry"],
                           "stop": t["stop"], "qty": t["qty"], "risk": t["risk"]}
                          for t in trades if t["status"] == "open"]
    out["coach_notes"] = coach_notes(out)
    return out


def coach_notes(rv: dict) -> list[str]:
    s, notes = rv["summary"], []
    if s["expectancy"] is not None:
        notes.append(f"Expectancy {inr(s['expectancy'], True)} per trade over {s['trades']} closed trades"
                     + (f" ({num(s['avg_R'])}R)." if s["avg_R"] is not None else "."))
    setups = {k: v for k, v in rv["by_setup"].items() if v["trades"] >= 2}
    if setups:
        best = max(setups.items(), key=lambda kv: kv[1]["net_pnl"])
        worst = min(setups.items(), key=lambda kv: kv[1]["net_pnl"])
        if best[1]["net_pnl"] > 0:
            notes.append(f"Best setup: '{best[0]}' ({inr(best[1]['net_pnl'], True)} over {best[1]['trades']}). "
                         "Do more of this.")
        if worst[1]["net_pnl"] < 0:
            notes.append(f"Worst setup: '{worst[0]}' ({inr(worst[1]['net_pnl'], True)} over "
                         f"{worst[1]['trades']}). Pause it or paper-trade it until it proves itself.")
    fc = rv.get("fno_vs_cash")
    if fc and fc["fno"]["net_pnl"] < 0 <= fc["cash"]["net_pnl"]:
        notes.append(f"F&O trades are net {inr(fc['fno']['net_pnl'], True)} while cash trades are "
                     f"{inr(fc['cash']['net_pnl'], True)}. Consider stopping F&O until the cash edge is proven.")
    if rv.get("mistakes"):
        top, e = next(iter(rv["mistakes"].items()))
        cm = rv["clean_vs_mistake"]
        if e["loss_cost"] > 0:
            notes.append(f"Costliest mistake: '{top}' ({inr(e['loss_cost'])} lost over {e['trades']} trade(s)).")
        notes.append(f"Clean trades {inr(cm['clean']['net_pnl'], True)} vs trades with a mistake "
                     f"{inr(cm['with_mistake']['net_pnl'], True)}.")
    h = rv.get("holding_days")
    if h and h["winners"] is not None and h["losers"] is not None and h["losers"] > h["winners"]:
        notes.append(f"Losers are held longer ({h['losers']:.1f} d) than winners ({h['winners']:.1f} d): "
                     "honour stops, let winners run.")
    hours = {k: v for k, v in rv.get("by_hour", {}).items() if v["trades"] >= 2 and v["net_pnl"] < 0}
    if hours:
        k, v = min(hours.items(), key=lambda kv: kv[1]["net_pnl"])
        notes.append(f"Weakest hour: {k} IST ({inr(v['net_pnl'], True)} over {v['trades']}).")
    tr = rv.get("trend")
    if tr and tr["recent"]["expectancy"] is not None and tr["prior"]["expectancy"] is not None:
        d = tr["recent"]["expectancy"] - tr["prior"]["expectancy"]
        notes.append(f"Last {tr['window']} vs previous {tr['window']}: expectancy "
                     f"{inr(tr['recent']['expectancy'], True)} vs {inr(tr['prior']['expectancy'], True)} "
                     f"({'improving' if d > 0 else 'declining' if d < 0 else 'flat'}).")
    if s["longest_losing_streak"] >= 3:
        notes.append(f"Longest losing streak {s['longest_losing_streak']}: run `breaker` before trading.")
    if s["trades"] < 30:
        notes.append("Fewer than 30 closed trades: treat every number here as provisional.")
    return notes


def iso_week_label(d: dt.date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def week_digest(trades: list[dict], week: str | None = None, as_of: dt.date | None = None) -> dict:
    c = [t for t in closed_trades(trades) if as_of is None or t["close_date"] <= as_of]
    if not c:
        raise ValueError("no closed trades for a weekly digest")
    if week in (None, "latest"):
        week = iso_week_label(max(t["close_date"] for t in c))
    try:
        y, w = week.upper().split("-W")
        monday = dt.date.fromisocalendar(int(y), int(w), 1)
    except ValueError:
        raise ValueError(f"week {week!r} must look like 2026-W43") from None
    sunday = monday + dt.timedelta(days=6)
    wk = [t for t in c if monday <= t["close_date"] <= sunday]
    prev = [t for t in c if monday - dt.timedelta(days=7) <= t["close_date"] < monday]
    out = {"week": week, "from": monday, "to": sunday, "summary": stats(wk) if wk else None,
           "previous_week_net": sum(t["pnl"] for t in prev) if prev else None,
           "by_setup": group(wk, lambda t: [t["setup"]]) if wk else {}}
    if wk:
        best = max(wk, key=lambda t: t["pnl"])
        worst = min(wk, key=lambda t: t["pnl"])
        out["best"] = {"symbol": best["symbol"], "pnl": best["pnl"], "setup": best["setup"]}
        out["worst"] = {"symbol": worst["symbol"], "pnl": worst["pnl"], "setup": worst["setup"]}
        mist = {}
        for t in wk:
            for m in t["mistakes"]:
                mist[m] = mist.get(m, 0.0) + (-min(0.0, t["pnl"]))
        out["mistakes"] = dict(sorted(mist.items(), key=lambda kv: -kv[1]))
    out["open_trades"] = [t["symbol"] for t in trades if t["status"] == "open" and t["date"] <= sunday]
    return out


# --------------------------------------------------------------------------- log
LOG_FIELDS = ["logged_at_ist", "as_of", "symbol", "side", "segment", "entry", "stop", "target", "qty",
              "risk_inr", "risk_pct", "regime", "verdict", "blocks", "warnings", "action", "note"]


def append_log(path: str, res: dict, symbol: str, action: str = "", note: str = "",
               now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now(IST)
    row = {"logged_at_ist": now.strftime("%Y-%m-%d %H:%M"), "as_of": str(res["as_of"] or ""),
           "symbol": symbol, "side": res["side"], "segment": res["segment"], "entry": f"{res['entry']:g}",
           "stop": "" if res["stop"] is None else f"{res['stop']:g}",
           "target": "" if res["target"] is None else f"{res['target']:g}",
           "qty": "" if res["qty"] is None else f"{res['qty']:g}",
           "risk_inr": "" if res["risk"] is None else f"{res['risk']:.0f}",
           "risk_pct": "" if res["risk_pct"] is None else f"{res['risk_pct']:.2f}",
           "regime": res["regime"] or "", "verdict": res["verdict"], "blocks": ";".join(res["blocks"]),
           "warnings": ";".join(res["warnings"]), "action": action, "note": note}
    exists = os.path.isfile(path) and os.path.getsize(path) > 0
    if exists:
        with open(path, newline="", encoding="utf-8") as fh:
            header = next(csv.reader(fh), [])
        if header != LOG_FIELDS:
            raise ValueError(f"{path} has a different header; refusing to append to a file that is not a decision log")
    elif os.path.dirname(path) and not os.path.isdir(os.path.dirname(path)):
        raise ValueError(f"folder for {path} does not exist")
    with open(path, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=LOG_FIELDS)
        if not exists:
            w.writeheader()
        w.writerow(row)
    return row


# --------------------------------------------------------------------------- CLI
def _yesno(v: str) -> bool:
    s = v.strip().lower()
    if s in ("y", "yes", "true", "1"):
        return True
    if s in ("n", "no", "false", "0"):
        return False
    raise argparse.ArgumentTypeError(f"expected yes or no, got {v!r}")


def _date_arg(v: str) -> dt.date:
    try:
        return parse_date(v, "--as-of")
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


def _time_arg(v: str) -> dt.time:
    try:
        return parse_hhmm(v, "--time")
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


def _breaker_args(p: argparse.ArgumentParser, risk_default: bool = True) -> None:
    if risk_default:
        p.add_argument("--risk-pct", type=float, default=1.0, help="normal risk per trade, %% of capital")
    p.add_argument("--daily-pct", type=float, default=2.0, help="daily loss limit, %% of capital")
    p.add_argument("--weekly-pct", type=float, default=5.0, help="weekly loss limit (Mon-Sun)")
    p.add_argument("--monthly-pct", type=float, default=10.0, help="calendar-month loss limit")
    p.add_argument("--stop-streak", type=int, default=3, help="consecutive losses that stop the day")
    p.add_argument("--reduce-streak", type=int, default=5, help="consecutive losses that halve size")
    p.add_argument("--reduce-trades", type=int, default=5, help="trades to stay at reduced size")


def _gate_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--entry", type=float, required=True)
    p.add_argument("--stop", type=float)
    p.add_argument("--target", type=float)
    p.add_argument("--qty", type=float, help="shares, or total quantity for F&O (lots x lot size)")
    p.add_argument("--capital", type=float)
    p.add_argument("--risk-pct-limit", type=float, default=1.0, help="max risk per trade, %% of capital")
    p.add_argument("--side", choices=["long", "short"])
    p.add_argument("--segment", choices=["equity", "intraday", "futures", "options-buy", "options-sell"],
                   default="equity")
    p.add_argument("--charges", type=float, default=0.0, help="estimated round-trip charges in ₹")
    p.add_argument("--regime", help="label from the market-regime skill, e.g. risk-on / risk-off / sideways")
    p.add_argument("--minutes-since-last-loss", type=float)
    p.add_argument("--revenge-minutes", type=float, default=30.0)
    p.add_argument("--plan-written", type=_yesno)
    p.add_argument("--fno-ban", type=_yesno, help="stock in F&O ban period")
    p.add_argument("--asm-gsm", type=_yesno, help="stock under ASM/GSM or in trade-for-trade (T2T)")
    p.add_argument("--event", type=_yesno, help="results/board meeting inside the holding window")
    p.add_argument("--circuit-locked", type=_yesno, help="stock locked at upper/lower circuit")
    p.add_argument("--expiry-day", type=_yesno, help="today is expiry for this option's underlying")
    p.add_argument("--time", type=_time_arg, help="order time HH:MM IST (default: now, unless --as-of)")
    p.add_argument("--as-of", type=_date_arg, help="decision date YYYY-MM-DD (default: today IST)")
    p.add_argument("--journal", help="journal CSV: also apply the circuit breaker")
    _breaker_args(p, risk_default=False)
    p.add_argument("--json", action="store_true")


def _run_gate(a) -> dict:
    now_ist = dt.datetime.now(IST)
    as_of = a.as_of or now_ist.date()
    t = a.time if a.time is not None else (None if a.as_of else now_ist.time().replace(second=0, microsecond=0))
    br, trades = None, None
    if a.journal:
        if not a.capital:
            raise ValueError("--journal needs --capital for the circuit breaker")
        trades = load_journal(a.journal)
        br = breaker(trades, a.capital, as_of, a.risk_pct_limit, a.daily_pct, a.weekly_pct, a.monthly_pct,
                     a.stop_streak, a.reduce_streak, a.reduce_trades)
    return gate(a.entry, a.stop, a.target, a.qty, a.capital, a.risk_pct_limit, a.side, a.segment, a.charges,
                a.regime, a.minutes_since_last_loss, a.revenge_minutes, a.plan_written, a.fno_ban, a.asm_gsm,
                a.event, a.circuit_locked, a.expiry_day, t, as_of, br, trades)


def _print_gate(res: dict, symbol: str = "") -> None:
    when = f"{res['as_of']}" + (f" {res['time']} IST" if res["time"] else " IST")
    print(f"\nPre-trade gate: {res['verdict']}  ({res['side']} {symbol + ' ' if symbol else ''}"
          f"{res['segment']}, {when})")
    for i in res["items"]:
        print(f"  [{i['status']:<5}] {i['check']:<16} {i['detail']}")
    if res["verdict"] == "BLOCK":
        print("Do not draft an order ticket. Fix the BLOCK items or skip the trade.")
    elif res["verdict"] == "PASS-WITH-WARNINGS":
        print("Proceed only if each warning is consciously accepted; then the broker order protocol applies.")
    else:
        print("Checklist clear; the broker order protocol (ticket + explicit yes) still applies.")


def _print_group(title: str, g: dict) -> None:
    if not g:
        return
    print(f"\n{title}")
    for k, v in sorted(g.items(), key=lambda kv: -kv[1]["net_pnl"]):
        print(f"  {k:<16} n={v['trades']:<3} win {pct(v['win_rate_pct']):>6}  net {inr(v['net_pnl'], True):>11}"
              f"  exp {inr(v['expectancy'], True):>9}  avgR {num(v['avg_R']):>6}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Trading discipline: circuit breaker, pre-trade gate, review")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("breaker", help="account-level loss limits and losing-streak cooldown")
    b.add_argument("csv")
    b.add_argument("--capital", type=float, required=True)
    b.add_argument("--as-of", type=_date_arg, help="YYYY-MM-DD (default: today IST)")
    _breaker_args(b)
    b.add_argument("--json", action="store_true")

    g = sub.add_parser("gate", help="pre-trade checklist; never writes anything")
    _gate_args(g)

    lg = sub.add_parser("log", help="run the gate and append the decision to a CSV log")
    _gate_args(lg)
    lg.add_argument("--log", required=True, help="decision-log CSV to append to")
    lg.add_argument("--symbol", required=True)
    lg.add_argument("--action", choices=["taken", "skipped", "pending"], default="pending")
    lg.add_argument("--note", default="")

    r = sub.add_parser("review", help="performance coaching from the journal")
    r.add_argument("csv")
    r.add_argument("--recent", type=int, default=10, help="compare the last N trades with the N before")
    r.add_argument("--week", nargs="?", const="latest", help="weekly digest; optional YYYY-Www")
    r.add_argument("--as-of", type=_date_arg, help="ignore trades closed after this date")
    r.add_argument("--json", action="store_true")
    a = p.parse_args(argv)

    try:
        if a.cmd == "breaker":
            as_of = a.as_of or dt.datetime.now(IST).date()
            res = breaker(load_journal(a.csv), a.capital, as_of, a.risk_pct, a.daily_pct, a.weekly_pct,
                          a.monthly_pct, a.stop_streak, a.reduce_streak, a.reduce_trades)
            if a.json:
                print(json.dumps(res, indent=2, default=str))
                return 0
            print(f"\nCircuit breaker: {res['status']}  (as of {as_of:%a %d %b %Y} IST, capital "
                  f"{inr(res['capital'])})")
            for pr in res["periods"]:
                print(f"  {('This ' + pr['period']) if pr['period'] != 'day' else 'Today':<11}"
                      f"realised {inr(pr['realised_pnl'], True):>10} | limit -{inr(pr['limit'])} "
                      f"({pr['limit_pct']:g}%) | room {inr(pr['room'])}")
            print(f"  Losing streak {res['losing_streak']} (stop day at {res['stop_streak']}, half size at "
                  f"{res['reduce_streak']}) | open trades {res['open_trades']}"
                  + (f", open risk {inr(res['open_risk'])}" if res["open_risk"] else ""))
            print(f"Allowed risk on next trade: {inr(res['allowed_risk'])} ({res['allowed_risk_pct']:.2f}% "
                  f"of capital; normal {inr(res['base_risk'])})")
            for reason in res["reasons"]:
                print("  •", reason)
            if res["ignored_after_as_of"]:
                print(f"  ({res['ignored_after_as_of']} trade(s) dated after {as_of} ignored)")
            print("Holidays are not checked: 'resume' dates are the next weekday.")
            return 0

        if a.cmd in ("gate", "log"):
            res = _run_gate(a)
            if a.cmd == "log":
                row = append_log(a.log, res, a.symbol, a.action, a.note)
                if a.json:
                    print(json.dumps({"gate": res, "logged": row}, indent=2, default=str))
                    return 0
                _print_gate(res, a.symbol)
                print(f"Logged 1 decision to {a.log} ({row['verdict']}, action={row['action']}).")
                return 0
            if a.json:
                print(json.dumps(res, indent=2, default=str))
                return 0
            _print_gate(res)
            return 0

        if a.cmd == "review":
            trades = load_journal(a.csv)
            if a.week:
                res = week_digest(trades, a.week, a.as_of)
                if a.json:
                    print(json.dumps(res, indent=2, default=str))
                    return 0
                print(f"\nWeekly digest {res['week']} ({res['from']:%d %b} - {res['to']:%d %b %Y})")
                s = res["summary"]
                if not s:
                    print("  No trades closed this week.")
                else:
                    print(f"  Closed {s['trades']} (W{s['wins']}/L{s['losses']}/BE{s['breakevens']}) | win "
                          f"{pct(s['win_rate_pct'])} | net {inr(s['net_pnl'], True)} | avg R {num(s['avg_R'])}")
                    print(f"  Best {res['best']['symbol']} {inr(res['best']['pnl'], True)} | worst "
                          f"{res['worst']['symbol']} {inr(res['worst']['pnl'], True)}")
                    if res["previous_week_net"] is not None:
                        print(f"  Previous week net {inr(res['previous_week_net'], True)}")
                    for m, cost in res["mistakes"].items():
                        print(f"  Mistake '{m}': {inr(cost)} lost")
                    _print_group("By setup", res["by_setup"])
                if res["open_trades"]:
                    print(f"  Open: {', '.join(res['open_trades'])}")
                return 0
            res = review(trades, a.recent, a.as_of)
            if a.json:
                print(json.dumps(res, indent=2, default=str))
                return 0
            s = res["summary"]
            print(f"\nClosed {s['trades']} (W{s['wins']}/L{s['losses']}/BE{s['breakevens']}) | win rate "
                  f"{pct(s['win_rate_pct'])} | net {inr(s['net_pnl'], True)}")
            print(f"Avg win {inr(s['avg_win'])} | avg loss {inr(s['avg_loss'])} | profit factor "
                  f"{num(s['profit_factor'], '.2f')} | expectancy {inr(s['expectancy'], True)}/trade | "
                  f"avg R {num(s['avg_R'])}")
            print(f"Max drawdown {inr(s['max_drawdown'])} | longest losing streak {s['longest_losing_streak']}")
            _print_group("By setup", res["by_setup"])
            _print_group("By weekday (entry)", res["by_weekday"])
            _print_group("By hour (entry, IST)", res.get("by_hour", {}))
            _print_group("By segment", res.get("by_segment", {}))
            if res.get("mistakes"):
                print("\nMistakes by rupee cost (losses on tagged trades)")
                for m, e in res["mistakes"].items():
                    print(f"  {m:<16} n={e['trades']:<3} lost {inr(e['loss_cost']):>9}  net "
                          f"{inr(e['net_pnl'], True)}")
            if res.get("holding_days"):
                h = res["holding_days"]
                print(f"\nHolding days: winners {num(h['winners'], '.1f')} | losers {num(h['losers'], '.1f')}")
            if res["open_trades"]:
                print("\nOpen (excluded from stats): " + ", ".join(
                    f"{o['symbol']} {o['date']} risk {inr(o['risk'])}" for o in res["open_trades"]))
            print("\nCoach notes")
            for n in res["coach_notes"]:
                print("  •", n)
            return 0
    except (ValueError, OSError, csv.Error) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
