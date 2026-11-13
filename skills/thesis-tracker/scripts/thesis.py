#!/usr/bin/env python3
"""Thesis tracker for Indian equity and F&O traders.

Keeps every trade idea in ONE human-readable JSON file, from first idea to closing review, so a
plan written today can be checked against what actually happened weeks later.

Lifecycle:  idea -> watching -> entry-ready -> active -> closed
            side exits before entry: invalidated (final), expired (can be revived to watching)

Sub-commands
  add         python3 thesis.py add TCS --direction long --horizon swing --statement "..." \
                  --trigger "close above 3400" --trigger-price 3400 --kill-price 3340 --stop 3360 \
                  --target 3520 --catalyst 2026-11-20:"Q2 results" --source breakout-planner
  update      python3 thesis.py update T0001 --stop 3380 --note "trailed under the swing low"
  transition  python3 thesis.py transition T0001 active --entry-price 3405 --qty 100
              python3 thesis.py transition T0001 closed --exit-price 3520 --reason target
  note        python3 thesis.py note T0001 "Held the 20-DMA on low volume" --reviewed
  list/show   python3 thesis.py list [--state active] [--symbol TCS] [--tag it] [--due]
  due         python3 thesis.py due [--prices ltp.csv] [--days 7] [--ltcg-days 45]
  stale       python3 thesis.py stale [--days 21]
  postmortem  python3 thesis.py postmortem T0001 --thesis right --mistake none --lesson "..."
  stats       python3 thesis.py stats
  export      python3 thesis.py export --journal journal.csv [--closed-only]
  hypothesis  python3 thesis.py hypothesis [--expected-win-rate 45 --margin 10]

The store is ``--store FILE`` (default ./theses.json in the current directory, never inside the
skill folder). Writes are atomic (temp file, then replace) and the previous version is kept as
FILE.bak. Python 3.10+ standard library only; nothing is fetched.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import importlib.util
import json
import math
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
TOOL = "thesis-tracker"
SCHEMA_VERSION = 1
EPS = 0.005  # half a paisa: anything smaller is a breakeven (same rule as trading-discipline)
SKILL_DIR = Path(__file__).resolve().parents[1]
DEFAULT_STORE = "theses.json"

STATES = ("idea", "watching", "entry-ready", "active", "closed", "invalidated", "expired")
PRE_ENTRY = ("idea", "watching", "entry-ready")
OPEN_STATES = PRE_ENTRY + ("active",)
TRANSITIONS = {
    "idea": ("watching", "entry-ready", "invalidated", "expired"),
    "watching": ("entry-ready", "invalidated", "expired"),
    "entry-ready": ("active", "watching", "invalidated", "expired"),
    "active": ("closed",),
    "closed": (),
    "invalidated": (),
    "expired": ("watching",),
}
HORIZONS = {"swing": 7, "positional": 30, "investment": 90}  # default days to the next review
SEGMENTS = ("delivery", "intraday", "futures", "options")
JOURNAL_SEGMENT = {"delivery": "equity", "intraday": "intraday", "futures": "fno", "options": "fno"}
EXIT_REASONS = ("target", "stop", "trailing-stop", "time-stop", "invalidated", "expiry", "manual", "other")
QUADRANTS = {
    "right-good": "right thesis, good result (earned)",
    "right-bad": "right thesis, bad result (variance or execution)",
    "wrong-good": "wrong thesis, good result (luck)",
    "wrong-bad": "wrong thesis, bad result (deserved)",
    "flat": "breakeven (neither good nor bad)",
    "no-trade": "never traded",
}
JOURNAL_FIELDS = ["date", "symbol", "side", "entry", "exit", "stop", "qty", "charges",
                  "exit_date", "setup", "mistake", "segment", "notes"]


# --------------------------------------------------------------------------- formatting
def inr(x: float | None, signed: bool = False, decimals: int | None = None) -> str:
    """Indian digit grouping: 1234567 -> ₹12,34,567. Paise are shown only when present."""
    if x is None:
        return "n/a"
    if decimals is None:
        decimals = 0 if abs(x - round(x)) < EPS else 2
    whole, _, frac = f"{abs(x):.{decimals}f}".partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts + [tail])
    sign = "-" if x < 0 else ("+" if signed and x > 0 else "")
    return f"{sign}₹{whole}" + (f".{frac}" if frac else "")


def rmult(x: float | None) -> str:
    return "n/a" if x is None else f"{x:+.2f}R"


def pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.1f}%"


def yn(v: bool | None) -> str:
    return "unknown" if v is None else ("yes" if v else "no")


def day_label(d: dt.date) -> str:
    return f"{d:%a %d %b %Y} IST"


# --------------------------------------------------------------------------- parsing
def parse_date(text: str, what: str = "date") -> dt.date:
    text = (text or "").strip()
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d %b %Y"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"{what} {text!r} is not a date (use 2026-11-20 or 20-Nov-2026)")


def _date_arg(v: str) -> dt.date:
    try:
        return parse_date(v)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


def _positive(v: str) -> float:
    try:
        x = float(v.replace(",", "").replace("₹", ""))
    except ValueError:
        raise argparse.ArgumentTypeError(f"{v!r} is not a number") from None
    if not math.isfinite(x) or x <= 0:
        raise argparse.ArgumentTypeError(f"{v!r} must be a positive number")
    return x


def _non_negative(v: str) -> float:
    try:
        x = float(v.replace(",", "").replace("₹", ""))
    except ValueError:
        raise argparse.ArgumentTypeError(f"{v!r} is not a number") from None
    if not math.isfinite(x) or x < 0:
        raise argparse.ArgumentTypeError(f"{v!r} must be zero or more")
    return x


def _yesno(v: str) -> bool:
    s = v.strip().lower()
    if s in ("y", "yes", "true", "1"):
        return True
    if s in ("n", "no", "false", "0"):
        return False
    raise argparse.ArgumentTypeError(f"expected yes or no, got {v!r}")


def _catalyst(v: str) -> dict:
    day, sep, label = v.partition(":")
    if not sep or not label.strip():
        raise argparse.ArgumentTypeError(f"catalyst {v!r} must look like 2026-11-20:Q2 results")
    try:
        return {"date": parse_date(day, "catalyst date").isoformat(), "label": label.strip()}
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


def slug(text: str) -> str:
    """Lower-case tag safe for the journal's ';'-separated mistake column."""
    out = "".join(c if c.isalnum() else "-" for c in text.strip().lower())
    return "-".join(p for p in out.split("-") if p)


def as_date(text: str | None) -> dt.date | None:
    return dt.date.fromisoformat(text) if text else None


# --------------------------------------------------------------------------- store
class Store:
    def __init__(self, path: Path, data: dict, stamp: tuple | None):
        self.path, self.data, self.stamp = path, data, stamp

    @property
    def theses(self) -> list[dict]:
        return self.data["theses"]


def resolve_store(arg: str | None) -> Path:
    path = Path(arg or DEFAULT_STORE).expanduser().resolve()
    if path == SKILL_DIR or SKILL_DIR in path.parents:
        raise ValueError(f"{path} is inside the skill folder; keep the store with your own files "
                         "(for example --store ~/trading/theses.json)")
    if path.is_dir():
        raise ValueError(f"{path} is a folder; --store must name a JSON file")
    return path


def _stamp(path: Path) -> tuple:
    st = path.stat()
    return (st.st_mtime_ns, st.st_size)


def load_store(path: Path, create: bool = False, today: dt.date | None = None) -> Store:
    if not path.exists():
        if not create:
            raise ValueError(f"no thesis store at {path} (register the first thesis with `add`, "
                             "or point --store at your file)")
        today = today or dt.datetime.now(IST).date()
        return Store(path, {"schema_version": SCHEMA_VERSION, "tool": TOOL, "created": today.isoformat(),
                            "updated": None, "next_id": 1, "theses": []}, None)
    stamp = _stamp(path)
    bak = path.with_name(path.name + ".bak")
    hint = f" The previous version is in {bak}." if bak.exists() else ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"{path} is not valid JSON ({e.msg}, line {e.lineno}); nothing was changed.{hint}") from None
    except UnicodeDecodeError:
        raise ValueError(f"{path} is not a UTF-8 text file; nothing was changed.{hint}") from None
    if not isinstance(data, dict) or data.get("tool") != TOOL or not isinstance(data.get("theses"), list):
        raise ValueError(f"{path} is not a thesis-tracker store; refusing to touch it")
    version = data.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ValueError(f"{path} has no usable schema_version; refusing to touch it.{hint}")
    if version > SCHEMA_VERSION:
        raise ValueError(f"{path} uses schema {version}, newer than this script understands "
                         f"({SCHEMA_VERSION}). Update the thesis-tracker skill; nothing was read or changed")
    for th in data["theses"]:
        if not isinstance(th, dict) or "id" not in th or th.get("state") not in STATES:
            raise ValueError(f"{path} has a thesis entry this script cannot read; nothing was changed.{hint}")
    data.setdefault("next_id", len(data["theses"]) + 1)
    return Store(path, data, stamp)


def save_store(store: Store, now: dt.datetime | None = None) -> None:
    """Atomic save: serialise fully, write a temp file beside the store, keep the old version as
    .bak, then rename over the store. A crash at any point leaves the old or the new file intact."""
    path = store.path
    if not path.parent.is_dir():
        raise ValueError(f"folder {path.parent} does not exist")
    store.data["updated"] = (now or dt.datetime.now(IST)).isoformat(timespec="seconds")
    text = json.dumps(store.data, indent=2, ensure_ascii=False) + "\n"
    json.loads(text)  # never replace a good file with something that does not round-trip
    exists = path.exists()
    if store.stamp is not None and (not exists or _stamp(path) != store.stamp):
        raise ValueError(f"{path} changed on disk while this command was running (another session?). "
                         "Nothing was written; run the command again")
    if store.stamp is None and exists:
        raise ValueError(f"{path} appeared while this command was running. Nothing was written; run it again")
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        if exists:
            os.chmod(tmp, stat.S_IMODE(path.stat().st_mode))
            bak = path.with_name(path.name + ".bak")
            bak_tmp = str(bak) + ".tmp"
            shutil.copy2(path, bak_tmp)
            os.replace(bak_tmp, bak)
        os.replace(tmp, path)
    except BaseException:
        for leftover in (tmp, str(path) + ".bak.tmp"):
            try:
                os.unlink(leftover)
            except OSError:
                pass
        raise
    try:  # best effort: make the rename itself durable
        dfd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass
    store.stamp = _stamp(path)


def find(store: Store, tid: str) -> dict:
    want = tid.strip().upper()
    if want.isdigit():
        want = f"T{int(want):04d}"
    for th in store.theses:
        if th["id"].upper() == want:
            return th
    raise ValueError(f"no thesis {tid!r} in {store.path} (see `list --all`)")


def log(th: dict, day: dt.date, event: str, note: str = "", **extra) -> None:
    entry = {"date": day.isoformat(), "event": event}
    entry.update({k: v for k, v in extra.items() if v is not None})
    if note:
        entry["note"] = note.strip()
    th.setdefault("history", []).append(entry)


def last_activity(th: dict) -> dt.date:
    days = [h["date"] for h in th.get("history", []) if h.get("date")] or [th["created"]]
    return dt.date.fromisoformat(max(days))


# --------------------------------------------------------------------------- tax clock (reused)
def tax_module():
    """indian-tax-for-traders/scripts/tax.py, loaded by path. None when that skill is not installed."""
    path = SKILL_DIR.parent / "indian-tax-for-traders" / "scripts" / "tax.py"
    name = "_thesis_tracker_tax"
    if name in sys.modules:
        return sys.modules[name]
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        del sys.modules[name]
        raise
    return mod


def long_term_clock(buy: dt.date, as_of: dt.date) -> dict | None:
    """Holding-period status of a listed-equity lot, using tax.py's own 12-month rule."""
    tax = tax_module()
    if tax is None:
        return None
    first_lt_day = tax.add_months(buy, 12) + dt.timedelta(days=1)  # is_long_term() needs sell > buy + 12 months
    lt = tax.is_long_term(buy, as_of)
    return {"long_term": lt, "long_term_from": first_lt_day, "days_to_long_term": 0 if lt else (first_lt_day - as_of).days,
            "stcg_rate_pct": tax.STCG_RATE * 100, "ltcg_rate_pct": tax.LTCG_RATE * 100,
            "ltcg_exemption": tax.LTCG_EXEMPTION}


# --------------------------------------------------------------------------- thesis logic
def is_long(th: dict) -> bool:
    return th["direction"] == "long"


def premium_at_risk(th: dict) -> bool:
    """A bought option with no stop risks the whole premium (same convention as the pre-trade gate)."""
    return th["segment"] == "options" and is_long(th)


def check_plan_levels(th: dict, ref: float | None, ref_name: str) -> None:
    """Stop and kill level must sit on the losing side of the reference price, targets on the winning side."""
    long = is_long(th)
    lose, win = ("below", "above") if long else ("above", "below")
    stop, kill, targets = th.get("stop"), th.get("kill_price"), th.get("targets") or []
    if ref is not None:
        for name, level in (("stop", stop), ("kill price", kill)):
            if level is not None and ((level >= ref) if long else (level <= ref)):
                raise ValueError(f"{name} {level:g} must be {lose} the {ref_name} {ref:g} for a {th['direction']}")
        for t in targets:
            if (t <= ref) if long else (t >= ref):
                raise ValueError(f"target {t:g} must be {win} the {ref_name} {ref:g} for a {th['direction']}")
    for name, level in (("stop", stop), ("kill price", kill)):
        for t in targets:
            if level is not None and ((level >= t) if long else (level <= t)):
                raise ValueError(f"{name} {level:g} must be {lose} target {t:g} for a {th['direction']}")


def planned_rr(th: dict) -> float | None:
    ref = (th.get("position") or {}).get("entry_price") or th.get("trigger_price")
    stop = (th.get("position") or {}).get("initial_stop", th.get("stop"))
    if ref is None or stop is None or not th.get("targets") or ref == stop:
        return None
    return abs(th["targets"][0] - ref) / abs(ref - stop)


def missing_for_entry(th: dict) -> list[str]:
    miss = []
    if not th.get("entry_trigger") and th.get("trigger_price") is None:
        miss.append("an entry trigger (--trigger and/or --trigger-price)")
    if not th.get("kill_condition") and th.get("kill_price") is None:
        miss.append("kill criteria (--kill and/or --kill-price)")
    if th.get("stop") is None and not premium_at_risk(th):
        miss.append("a stop (--stop)")
    return miss


def risk_amount(th: dict) -> float | None:
    p = th.get("position")
    if not p:
        return None
    stop = p.get("initial_stop")
    unit = abs(p["entry_price"] - stop) if stop is not None else (p["entry_price"] if premium_at_risk(th) else None)
    return unit * p["qty"] if unit else None


def trade_result(th: dict, price: float | None = None) -> dict | None:
    """Realised result of a closed thesis, or the unrealised one at ``price`` for an active thesis."""
    p = th.get("position")
    if not p:
        return None
    exit_price = p.get("exit_price") if price is None else price
    if exit_price is None:
        return None
    charges = (p.get("charges") or 0.0) if price is None else 0.0
    pnl = (exit_price - p["entry_price"]) * p["qty"] * (1 if is_long(th) else -1) - charges
    risk = risk_amount(th)
    outcome = "win" if pnl > EPS else "loss" if pnl < -EPS else "breakeven"
    out = {"pnl": round(pnl, 2), "r": pnl / risk if risk else None, "risk": risk, "outcome": outcome}
    if price is None and p.get("exit_date"):
        buy, sell = as_date(p["entry_date"]), as_date(p["exit_date"])
        out["holding_days"] = (sell - buy).days
        if th["segment"] == "delivery" and is_long(th):
            tax = tax_module()
            if tax is not None:
                out["tax_term"] = "LTCG" if tax.is_long_term(buy, sell) else "STCG"
    return out


def summarise(th: dict) -> dict:
    """Thesis plus derived numbers, for JSON output."""
    out = dict(th)
    out["planned_rr"] = planned_rr(th)
    out["risk_amount"] = risk_amount(th)
    out["result"] = trade_result(th)
    return out


# --------------------------------------------------------------------------- commands: write
def cmd_add(a, today: dt.date) -> dict:
    path = resolve_store(a.store)
    store = load_store(path, create=True, today=today)
    symbol = " ".join(a.symbol.upper().split())
    if not symbol:
        raise ValueError("symbol is empty")
    statement = a.statement.strip()
    if len(statement) < 10:
        raise ValueError("--statement must say what has to be true for the trade to work (10+ characters)")
    if a.direction == "short" and a.segment == "delivery":
        raise ValueError("a short cannot be carried in delivery in India; use --segment intraday, futures or options")
    expiry = a.expiry
    if a.segment in ("futures", "options") and expiry is None:
        raise ValueError(f"a {a.segment} thesis needs --expiry (the contract's expiry date)")
    if a.segment in ("delivery", "intraday") and expiry is not None:
        raise ValueError("--expiry only applies to futures and options")
    if expiry is not None and expiry < today:
        raise ValueError(f"--expiry {expiry} is already past")
    review = a.review_due or (today + dt.timedelta(days=HORIZONS[a.horizon]))
    if review < today:
        raise ValueError(f"--review-due {review} is before {today}")
    th = {
        "id": f"T{store.data['next_id']:04d}", "symbol": symbol, "direction": a.direction,
        "horizon": a.horizon, "segment": a.segment, "source": slug(a.source) if a.source else "own-idea",
        "setup": slug(a.setup) if a.setup else None, "tags": sorted({slug(t) for t in a.tag or [] if slug(t)}),
        "statement": statement, "entry_trigger": (a.trigger or "").strip() or None, "trigger_price": a.trigger_price,
        "kill_condition": (a.kill or "").strip() or None, "kill_price": a.kill_price, "stop": a.stop,
        "targets": list(a.target or []), "risk_pct": a.risk_pct,
        "catalysts": sorted(a.catalyst or [], key=lambda c: c["date"]),
        "expiry": expiry.isoformat() if expiry else None, "review_due": review.isoformat(),
        "state": "idea", "created": today.isoformat(), "position": None, "postmortem": None, "history": [],
    }
    check_plan_levels(th, th["trigger_price"], "trigger price")
    log(th, today, "created", a.note or "", to="idea")
    warnings = []
    twins = [t["id"] for t in store.theses if t["symbol"] == symbol and t["direction"] == a.direction
             and t["state"] in OPEN_STATES]
    if twins:
        warnings.append(f"{', '.join(twins)} already track{'s' if len(twins) == 1 else ''} an open {a.direction} "
                        f"thesis on {symbol}.")
    miss = missing_for_entry(th)
    if miss:
        warnings.append("Not yet tradeable: before entry-ready it needs " + "; ".join(miss) + ".")
    store.data["next_id"] += 1
    store.theses.append(th)
    save_store(store)
    return {"action": "added", "store": str(path), "thesis": summarise(th), "warnings": warnings}


def cmd_update(a, today: dt.date) -> dict:
    store = load_store(resolve_store(a.store))
    th = find(store, a.id)
    if th["state"] not in OPEN_STATES:
        raise ValueError(f"{th['id']} is {th['state']}: its plan is frozen. Use `note` for a later observation")
    before = json.loads(json.dumps(th))
    if a.statement is not None:
        if len(a.statement.strip()) < 10:
            raise ValueError("--statement must say what has to be true (10+ characters)")
        th["statement"] = a.statement.strip()
    for field, value in (("entry_trigger", a.trigger), ("kill_condition", a.kill)):
        if value is not None:
            th[field] = value.strip() or None
    for field, value in (("trigger_price", a.trigger_price), ("kill_price", a.kill_price), ("stop", a.stop),
                         ("risk_pct", a.risk_pct)):
        if value is not None:
            th[field] = value
    if a.target:
        th["targets"] = list(a.target)
    if a.clear_catalysts:
        th["catalysts"] = []
    if a.catalyst:
        th["catalysts"] = sorted(th["catalysts"] + [c for c in a.catalyst if c not in th["catalysts"]],
                                 key=lambda c: c["date"])
    if a.setup is not None:
        th["setup"] = slug(a.setup) or None
    if a.tag:
        th["tags"] = sorted(set(th["tags"]) | {slug(t) for t in a.tag if slug(t)})
    if a.source is not None:
        th["source"] = slug(a.source) or "own-idea"
    if a.horizon is not None:
        th["horizon"] = a.horizon
    if a.expiry is not None:
        if th["segment"] not in ("futures", "options"):
            raise ValueError("--expiry only applies to futures and options")
        th["expiry"] = a.expiry.isoformat()
    if a.review_due is not None:
        th["review_due"] = a.review_due.isoformat()
    changes = {k: {"from": before.get(k), "to": th[k]} for k in th if k != "history" and th[k] != before.get(k)}
    if not changes:
        raise ValueError("nothing to change: give at least one field to update")
    active = th["state"] == "active"
    if not active:
        check_plan_levels(th, th["trigger_price"], "trigger price")
    warnings, flags = [], {}
    if active and "stop" in changes and before.get("stop") is not None:
        widened = th["stop"] < before["stop"] if is_long(th) else th["stop"] > before["stop"]
        if widened:
            flags["stop_widened"] = True
            warnings.append(f"Stop moved AWAY from price while the trade is live ({before['stop']:g} -> "
                            f"{th['stop']:g}). Logged; the postmortem will count it against plan adherence.")
    if active and any(k in changes for k in ("statement", "kill_condition", "kill_price")):
        flags["goalposts_moved"] = True
        warnings.append("Thesis statement or kill criteria changed while the trade is live. The old wording "
                        "stays in the history.")
    log(th, today, "update", a.note or "", changes=changes, **flags)
    save_store(store)
    return {"action": "updated", "store": str(store.path), "thesis": summarise(th),
            "changed": sorted(changes), "warnings": warnings}


def cmd_transition(a, today: dt.date) -> dict:
    store = load_store(resolve_store(a.store))
    th = find(store, a.id)
    cur, new = th["state"], a.state
    if new == cur:
        raise ValueError(f"{th['id']} is already {cur}")
    if new not in TRANSITIONS[cur]:
        allowed = ", ".join(TRANSITIONS[cur]) or "nothing (final state; register a new thesis instead)"
        raise ValueError(f"{th['id']} is {cur}: it cannot move to {new}. Allowed from {cur}: {allowed}")
    entry_flags = {"--entry-price": a.entry_price, "--qty": a.qty}
    exit_flags = {"--exit-price": a.exit_price, "--reason": a.reason, "--charges": a.charges}
    for flags, state in ((entry_flags, "active"), (exit_flags, "closed")):
        stray = [k for k, v in flags.items() if v is not None]
        if stray and new != state:
            raise ValueError(f"{', '.join(stray)} only appl{'ies' if len(stray) == 1 else 'y'} when moving to {state}")
    day = a.date or today
    if day > today:
        raise ValueError(f"--date {day} is in the future (today is {today} IST)")
    note = (a.note or "").strip()
    warnings: list[str] = []
    if new in ("entry-ready", "active"):
        miss = missing_for_entry(th)
        if miss:
            raise ValueError(f"{th['id']} cannot become {new} without " + "; ".join(miss)
                             + f". Add them with `update {th['id']} ...` first")
    if new == "invalidated" and not note:
        raise ValueError("--note is required when invalidating: record what proved the thesis wrong")
    if new == "active":
        if a.entry_price is None or a.qty is None:
            raise ValueError("moving to active needs --entry-price and --qty (total quantity: lots x lot size for F&O)")
        th["position"] = {"entry_date": day.isoformat(), "entry_price": a.entry_price, "qty": a.qty,
                          "initial_stop": th.get("stop"), "exit_date": None, "exit_price": None,
                          "exit_reason": None, "charges": None}
        try:
            check_plan_levels(th, a.entry_price, "entry price")
        except ValueError as e:
            raise ValueError(f"{e}. Fix the plan with `update` or re-check the fill price") from None
        if th.get("trigger_price") and abs(a.entry_price - th["trigger_price"]) / th["trigger_price"] > 0.01:
            warnings.append(f"Entry {a.entry_price:g} is {abs(a.entry_price / th['trigger_price'] - 1) * 100:.1f}% "
                            f"away from the planned trigger {th['trigger_price']:g}.")
    if new == "closed":
        if a.exit_price is None or a.reason is None:
            raise ValueError(f"closing needs --exit-price and --reason ({', '.join(EXIT_REASONS)})")
        pos = th["position"]
        if day < as_date(pos["entry_date"]):
            raise ValueError(f"exit date {day} is before the entry date {pos['entry_date']}")
        pos.update({"exit_date": day.isoformat(), "exit_price": a.exit_price, "exit_reason": a.reason,
                    "charges": a.charges})
        pos["result"] = trade_result(th)
    th["state"] = new
    if new in OPEN_STATES:
        due = as_date(th.get("review_due"))
        if due is None or due <= day:
            th["review_due"] = (day + dt.timedelta(days=HORIZONS[th["horizon"]])).isoformat()
    else:
        th["review_due"] = None
    log(th, day, "transition", note, **{"from": cur, "to": new})
    save_store(store)
    if new == "closed":
        warnings.append(f"Closed. Run `postmortem {th['id']}` while the trade is fresh.")
    return {"action": "transition", "store": str(store.path), "from": cur, "to": new,
            "thesis": summarise(th), "warnings": warnings}


def cmd_note(a, today: dt.date) -> dict:
    store = load_store(resolve_store(a.store))
    th = find(store, a.id)
    text = a.text.strip()
    if not text:
        raise ValueError("the note is empty")
    day = a.date or today
    if day > today:
        raise ValueError(f"--date {day} is in the future (today is {today} IST)")
    extra = {}
    if a.reviewed or a.next_review:
        if th["state"] not in OPEN_STATES:
            raise ValueError(f"{th['id']} is {th['state']}: there is no review to schedule")
        nxt = a.next_review or (day + dt.timedelta(days=HORIZONS[th["horizon"]]))
        if nxt <= day:
            raise ValueError(f"--next-review {nxt} must be after {day}")
        th["review_due"] = nxt.isoformat()
        extra["next_review"] = nxt.isoformat()
    log(th, day, "review" if a.reviewed else "note", text, **extra)
    save_store(store)
    return {"action": "noted", "store": str(store.path), "thesis": summarise(th), "warnings": []}


# --------------------------------------------------------------------------- commands: read
def cmd_list(a, today: dt.date) -> dict:
    store = load_store(resolve_store(a.store))
    rows = store.theses
    if a.state:
        rows = [t for t in rows if t["state"] in a.state]
    elif not a.all:
        rows = [t for t in rows if t["state"] in OPEN_STATES]
    if a.symbol:
        want = " ".join(a.symbol.upper().split())
        rows = [t for t in rows if t["symbol"] == want]
    if a.tag:
        want = slug(a.tag)
        rows = [t for t in rows if want in t["tags"] or want == t.get("setup")]
    if a.source:
        rows = [t for t in rows if t["source"] == slug(a.source)]
    if a.due:
        rows = [t for t in rows if t["state"] in OPEN_STATES and t.get("review_due")
                and as_date(t["review_due"]) <= today]
    counts = {s: sum(1 for t in store.theses if t["state"] == s) for s in STATES}
    return {"as_of": today.isoformat(), "store": str(store.path), "counts": counts,
            "theses": [summarise(t) for t in rows]}


def cmd_show(a, today: dt.date) -> dict:
    store = load_store(resolve_store(a.store))
    th = find(store, a.id)
    return {"as_of": today.isoformat(), "store": str(store.path), "thesis": summarise(th)}


def load_prices(path: str) -> dict[str, float]:
    if not os.path.isfile(path):
        raise ValueError(f"prices file not found: {path}")
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        cols = {(c or "").strip().lower().replace(" ", "_"): c for c in reader.fieldnames or []}
        sym = next((cols[c] for c in ("symbol", "tradingsymbol", "trading_symbol", "scrip", "ticker") if c in cols), None)
        ltp = next((cols[c] for c in ("ltp", "last_price", "last", "close", "price", "cmp") if c in cols), None)
        if not sym or not ltp:
            raise ValueError(f"{path} needs a header with symbol and ltp columns")
        prices = {}
        for i, row in enumerate(reader, start=2):
            name = " ".join((row.get(sym) or "").upper().split())
            raw = (row.get(ltp) or "").replace(",", "").replace("₹", "").strip()
            if not name and not raw:
                continue
            try:
                value = float(raw)
            except ValueError:
                raise ValueError(f"{path} row {i}: ltp {row.get(ltp)!r} is not a number") from None
            if not name or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{path} row {i}: need a symbol and a positive ltp")
            prices[name] = value
    if not prices:
        raise ValueError(f"no prices in {path}")
    return prices


def _head(th: dict) -> dict:
    return {"id": th["id"], "symbol": th["symbol"], "direction": th["direction"], "state": th["state"],
            "horizon": th["horizon"], "segment": th["segment"]}


def cmd_due(a, today: dt.date) -> dict:
    if a.days < 0 or a.ltcg_days < 0:
        raise ValueError("--days and --ltcg-days must be zero or more")
    store = load_store(resolve_store(a.store))
    prices = load_prices(a.prices) if a.prices else None
    open_ = [t for t in store.theses if t["state"] in OPEN_STATES]
    reviews, kills, targets, catalysts, ltcg, notes = [], [], [], [], [], []
    missing = []
    horizon_end = today + dt.timedelta(days=a.days)
    for th in open_:
        long, active = is_long(th), th["state"] == "active"
        due = as_date(th.get("review_due"))
        if due and due <= today:
            reviews.append({**_head(th), "review_due": due.isoformat(), "days_overdue": (today - due).days})
        ltp = prices.get(th["symbol"]) if prices is not None else None
        if prices is not None and ltp is None:
            missing.append(th["symbol"])
        if ltp is not None:
            for kind, level in (("kill", th.get("kill_price")), ("stop", th.get("stop"))):
                if level is not None and ((ltp <= level) if long else (ltp >= level)):
                    action = (f"exit as planned, then: transition {th['id']} closed --exit-price <fill> --reason "
                              f"{'invalidated' if kind == 'kill' else 'stop'}" if active else
                              f"thesis broke before entry: transition {th['id']} invalidated --note \"...\"")
                    kills.append({**_head(th), "level": kind, "level_price": level, "ltp": ltp,
                                  "kill_condition": th.get("kill_condition"), "action": action})
                    break
            if active and th.get("targets"):
                hit = [t for t in th["targets"] if ((ltp >= t) if long else (ltp <= t))]
                if hit:
                    targets.append({**_head(th), "targets_hit": hit, "ltp": ltp,
                                    "unrealised": trade_result(th, ltp)})
        events = [dict(c, kind="catalyst") for c in th.get("catalysts", [])]
        if th.get("expiry"):
            events.append({"date": th["expiry"], "label": f"{th['segment']} contract expiry", "kind": "expiry"})
        for ev in events:
            day = as_date(ev["date"])
            overdue_expiry = ev["kind"] == "expiry" and day < today
            if today <= day <= horizon_end or overdue_expiry:
                hint = None
                if overdue_expiry:
                    hint = "contract has expired: close or expire this thesis"
                elif ev["kind"] == "expiry":
                    hint = "roll, close, or let it settle: decide before expiry day"
                elif active:
                    hint = "event falls inside the holding period: gap risk, the stop may not hold"
                catalysts.append({**_head(th), "date": ev["date"], "label": ev["label"], "kind": ev["kind"],
                                  "in_days": (day - today).days, "hint": hint})
        if active and th["segment"] == "delivery" and long:
            clock = long_term_clock(as_date(th["position"]["entry_date"]), today)
            if clock is None:
                if "tax" not in notes:
                    notes.append("tax")
            elif not clock["long_term"] and clock["days_to_long_term"] <= a.ltcg_days:
                item = {**_head(th), "entry_date": th["position"]["entry_date"],
                        "long_term_from": clock["long_term_from"].isoformat(),
                        "days_to_long_term": clock["days_to_long_term"], "ltp": ltp, "unrealised": None,
                        "tax_saved_estimate": None}
                n, stcg, ltcg_rate = clock["days_to_long_term"], clock["stcg_rate_pct"], clock["ltcg_rate_pct"]
                item["hint"] = (f"wait {n} day{'s' if n != 1 else ''} (sell on or after {clock['long_term_from']}) "
                                f"to convert STCG {stcg:g}% into LTCG {ltcg_rate:g}%")
                if ltp is not None:
                    gain = trade_result(th, ltp)["pnl"]
                    item["unrealised"] = gain
                    if gain > EPS:
                        item["tax_saved_estimate"] = round(gain * (stcg - ltcg_rate) / 100, 2)
                    elif gain < -EPS:
                        item["hint"] = (f"at a loss: a short-term loss offsets both STCG and LTCG, a long-term loss "
                                        f"only LTCG. Booking before {clock['long_term_from']} keeps it flexible")
                ltcg.append(item)
    notes = ["indian-tax-for-traders skill not found beside this one: long-term clock skipped."
             if n == "tax" else n for n in notes]
    if missing:
        notes.append("No price supplied for: " + ", ".join(sorted(set(missing))) + ".")
    if prices is None and any(t.get("kill_price") is not None or t.get("stop") is not None for t in open_):
        notes.append("Kill levels not checked: pass --prices FILE (symbol,ltp).")
    reviews.sort(key=lambda r: -r["days_overdue"])
    catalysts.sort(key=lambda c: c["date"])
    ltcg.sort(key=lambda c: c["days_to_long_term"])
    return {"as_of": today.isoformat(), "store": str(store.path), "open_theses": len(open_),
            "window_days": a.days, "ltcg_window_days": a.ltcg_days, "kill_hits": kills,
            "reviews_due": reviews, "targets_hit": targets, "catalysts": catalysts, "turning_long_term": ltcg,
            "notes": notes}


def cmd_stale(a, today: dt.date) -> dict:
    if a.days < 1:
        raise ValueError("--days must be at least 1")
    store = load_store(resolve_store(a.store))
    rows = []
    for th in store.theses:
        if th["state"] not in PRE_ENTRY:
            continue
        quiet = (today - last_activity(th)).days
        if quiet >= a.days:
            rows.append({**_head(th), "created": th["created"], "age_days": (today - as_date(th["created"])).days,
                         "quiet_days": quiet, "statement": th["statement"],
                         "prompt": f"Still valid? If not: transition {th['id']} expired --note \"no trigger in "
                                   f"{quiet} days\""})
    rows.sort(key=lambda r: -r["quiet_days"])
    return {"as_of": today.isoformat(), "store": str(store.path), "threshold_days": a.days, "stale": rows}


# --------------------------------------------------------------------------- postmortem
def plan_checks(th: dict, tolerance_pct: float = 1.0) -> dict:
    """What the record itself says about plan adherence. None means 'cannot tell from the numbers'."""
    p, long = th["position"], is_long(th)
    tol = tolerance_pct / 100
    entry_ok = None
    if th.get("trigger_price"):
        entry_ok = abs(p["entry_price"] - th["trigger_price"]) / th["trigger_price"] <= tol
    stop_ok = None
    stop = p.get("initial_stop")
    if stop is not None:
        beyond = (stop - p["exit_price"]) if long else (p["exit_price"] - stop)
        stop_ok = beyond <= stop * tol
    widened = any(h.get("stop_widened") for h in th.get("history", []))
    early = None
    if p.get("exit_reason") in ("target", "stop", "trailing-stop"):
        early = False
    elif p.get("exit_reason") == "manual" and th.get("targets"):
        first = th["targets"][0]
        short_of_target = p["exit_price"] < first if long else p["exit_price"] > first
        in_profit = p["exit_price"] > p["entry_price"] if long else p["exit_price"] < p["entry_price"]
        if short_of_target and in_profit:
            early = True
    return {"entry_at_trigger": entry_ok, "stop_honoured": stop_ok, "exited_early": early, "stop_widened": widened}


def build_postmortem(th: dict, verdict: str, day: dt.date, overrides: dict, mistakes: list[str], lesson: str,
                     tolerance_pct: float = 1.0) -> dict:
    traded = th["state"] == "closed"
    res = trade_result(th) if traded else None
    if traded:
        checks = plan_checks(th, tolerance_pct)
        source = {k: "record" for k, v in checks.items() if v is not None}
        for key, value in overrides.items():
            if value is not None:
                checks[key], source[key] = value, "user"
        bad = (checks["entry_at_trigger"] is False or checks["stop_honoured"] is False
               or checks["exited_early"] is True or checks["stop_widened"])
        known = any(checks[k] is not None for k in ("entry_at_trigger", "stop_honoured", "exited_early"))
        checks["followed"] = False if bad else (True if known else None)
        checks["answered_by"] = source
        quadrant = "flat" if res["outcome"] == "breakeven" else \
            f"{verdict}-{'good' if res['outcome'] == 'win' else 'bad'}"
    else:
        checks, quadrant = None, "no-trade"
    tags = [m for m in dict.fromkeys(slug(m) for m in mistakes) if m and m != "none"]
    return {"date": day.isoformat(), "thesis": verdict, "result": res["outcome"] if res else "no-trade",
            "pnl": res["pnl"] if res else None, "r": res["r"] if res else None,
            "quadrant": quadrant, "quadrant_label": QUADRANTS[quadrant], "plan": checks,
            "mistakes": tags, "lesson": lesson.strip() or None}


def cmd_postmortem(a, today: dt.date) -> dict:
    store = load_store(resolve_store(a.store))
    th = find(store, a.id)
    if th["state"] in OPEN_STATES:
        raise ValueError(f"{th['id']} is still {th['state']}: a postmortem needs a closed, invalidated or "
                         "expired thesis")
    traded = th["state"] == "closed"
    overrides = {"entry_at_trigger": a.entry_at_trigger, "stop_honoured": a.stop_honoured,
                 "exited_early": a.exited_early}
    if a.thesis is None:  # read-only: show the saved review, or the facts and the questions still to answer
        if any(v is not None for v in overrides.values()) or a.mistake or a.lesson or a.revise:
            raise ValueError("--thesis right|wrong is required to save a postmortem")
        out = {"action": "postmortem-draft" if th.get("postmortem") is None else "postmortem-saved",
               "store": str(store.path), "thesis": summarise(th), "postmortem": th.get("postmortem")}
        if th.get("postmortem") is None:
            out["facts"] = {"result": trade_result(th), "plan": plan_checks(th, a.tolerance_pct) if traded else None}
            out["questions"] = [
                "Was the thesis right? Did the written statement turn out true, whatever the P&L? (--thesis right|wrong)",
                "Was the entry taken at the planned trigger? (--entry-at-trigger yes|no)",
                "Was the stop honoured, without widening or hoping? (--stop-honoured yes|no)",
                "Was the exit earlier than the plan allowed? (--exited-early yes|no)",
                "One mistake tag, or none (--mistake), and one lesson to carry forward (--lesson)."]
        return out
    if not traded and any(v is not None for v in overrides.values()):
        raise ValueError(f"{th['id']} was never traded: the plan-adherence flags do not apply")
    if th.get("postmortem") is not None and not a.revise:
        raise ValueError(f"{th['id']} already has a postmortem dated {th['postmortem']['date']}. "
                         "Add --revise to replace it (the earlier one stays in the history)")
    pm = build_postmortem(th, a.thesis, today, overrides, a.mistake or [], a.lesson or "", a.tolerance_pct)
    previous = th.get("postmortem")
    th["postmortem"] = pm
    log(th, today, "postmortem", pm["quadrant_label"], replaced=previous)
    save_store(store)
    return {"action": "postmortem-saved", "store": str(store.path), "thesis": summarise(th), "postmortem": pm}


# --------------------------------------------------------------------------- stats
def _brief(items: list[tuple[dict, dict]]) -> dict:
    n = len(items)
    pnls = [r["pnl"] for _, r in items]
    rs = [r["r"] for _, r in items if r["r"] is not None]
    wins = [p for p in pnls if p > EPS]
    losses = [p for p in pnls if p < -EPS]
    return {"trades": n, "wins": len(wins), "losses": len(losses), "breakevens": n - len(wins) - len(losses),
            "win_rate_pct": len(wins) / n * 100 if n else None, "net_pnl": sum(pnls),
            "expectancy": sum(pnls) / n if n else None, "avg_R": sum(rs) / len(rs) if rs else None,
            "trades_with_R": len(rs)}


def compute_stats(theses: list[dict]) -> dict:
    closed = [(t, trade_result(t)) for t in theses if t["state"] == "closed"]
    out = {"counts": {s: sum(1 for t in theses if t["state"] == s) for s in STATES}, "summary": _brief(closed)}
    wins = [r["pnl"] for _, r in closed if r["outcome"] == "win"]
    losses = [r["pnl"] for _, r in closed if r["outcome"] == "loss"]
    out["summary"].update({
        "avg_win": sum(wins) / len(wins) if wins else None,
        "avg_loss": -sum(losses) / len(losses) if losses else None,
        "profit_factor": sum(wins) / -sum(losses) if losses else None,
        "expectancy_R": out["summary"]["avg_R"]})

    def grouped(key) -> dict:
        buckets: dict = {}
        for item in closed:
            for k in key(item[0]):
                buckets.setdefault(k, []).append(item)
        return {k: _brief(v) for k, v in sorted(buckets.items(), key=lambda kv: -sum(r["pnl"] for _, r in kv[1]))}

    out["by_source"] = grouped(lambda t: [t["source"]])
    out["by_setup"] = grouped(lambda t: [t.get("setup") or "untagged"])
    out["by_tag"] = grouped(lambda t: t["tags"])
    out["by_horizon"] = grouped(lambda t: [t["horizon"]])
    out["by_direction"] = grouped(lambda t: [t["direction"]])
    out["by_segment"] = grouped(lambda t: [t["segment"]])

    reviewed = [(t, r) for t, r in closed if t.get("postmortem")]
    quad = {k: 0 for k in ("right-good", "right-bad", "wrong-good", "wrong-bad", "flat")}
    for t, _ in reviewed:
        quad[t["postmortem"]["quadrant"]] = quad.get(t["postmortem"]["quadrant"], 0) + 1
    out["quadrants"] = quad
    out["postmortems"] = {"closed": len(closed), "reviewed": len(reviewed),
                          "not_reviewed": [t["id"] for t, _ in closed if not t.get("postmortem")]}
    followed = [(t, r) for t, r in reviewed if (t["postmortem"].get("plan") or {}).get("followed") is True]
    broke = [(t, r) for t, r in reviewed if (t["postmortem"].get("plan") or {}).get("followed") is False]
    judged = len(followed) + len(broke)
    out["plan_adherence"] = {"judged": judged, "followed": len(followed), "broken": len(broke),
                             "rate_pct": len(followed) / judged * 100 if judged else None,
                             "followed_stats": _brief(followed), "broken_stats": _brief(broke)}
    mistakes: dict = {}
    for t, r in reviewed:
        for m in t["postmortem"].get("mistakes", []):
            e = mistakes.setdefault(m, {"count": 0, "net_pnl": 0.0, "loss_cost": 0.0})
            e["count"] += 1
            e["net_pnl"] += r["pnl"]
            e["loss_cost"] += -min(0.0, r["pnl"])
    out["mistakes"] = dict(sorted(mistakes.items(), key=lambda kv: (-kv[1]["count"], -kv[1]["loss_cost"], kv[0])))
    skipped = [t for t in theses if t["state"] in ("invalidated", "expired")]
    out["not_traded"] = {"invalidated": sum(1 for t in skipped if t["state"] == "invalidated"),
                         "expired": sum(1 for t in skipped if t["state"] == "expired"),
                         "judged_wrong": sum(1 for t in skipped if (t.get("postmortem") or {}).get("thesis") == "wrong"),
                         "judged_right": sum(1 for t in skipped if (t.get("postmortem") or {}).get("thesis") == "right")}
    notes = []
    n = len(closed)
    if n == 0:
        notes.append("No closed theses yet: nothing to measure.")
    elif n < 30:
        notes.append(f"Only {n} closed theses: treat every number as provisional until there are 30 or more.")
    if quad["wrong-good"] and quad["wrong-good"] >= quad["right-good"]:
        notes.append("As many lucky wins as earned ones: the P&L is flattering the process.")
    if quad["right-bad"] > quad["wrong-bad"] and quad["right-bad"] >= 2:
        notes.append("Most losses came with a correct thesis: look at entries, stops and sizing, not at idea selection.")
    if judged and broke and followed:
        a_, b_ = out["plan_adherence"]["followed_stats"]["expectancy"], out["plan_adherence"]["broken_stats"]["expectancy"]
        notes.append(f"Plan followed: {inr(a_, True)} per trade. Plan broken: {inr(b_, True)} per trade.")
    if out["postmortems"]["not_reviewed"]:
        notes.append(f"{len(out['postmortems']['not_reviewed'])} closed thesis(es) have no postmortem yet.")
    out["notes"] = notes
    return out


def cmd_stats(a, today: dt.date) -> dict:
    store = load_store(resolve_store(a.store))
    theses = store.theses
    if a.since:
        theses = [t for t in theses if t["state"] != "closed" or as_date(t["position"]["exit_date"]) >= a.since]
    out = compute_stats(theses)
    out.update({"as_of": today.isoformat(), "store": str(store.path), "since": a.since.isoformat() if a.since else None})
    return out


# --------------------------------------------------------------------------- export / hypothesis
def _g(x: float | None) -> str:
    return "" if x is None else f"{x:.10g}"


def journal_rows(theses: list[dict], closed_only: bool = False) -> list[dict]:
    rows = []
    for th in theses:
        if th["state"] not in (("closed",) if closed_only else ("closed", "active")):
            continue
        p = th["position"]
        stop = p.get("initial_stop")
        if stop is None and premium_at_risk(th):
            stop = 0.0  # whole premium at risk: |entry - 0| x qty
        pm = th.get("postmortem") or {}
        notes = [th["id"], f"source {th['source']}", th["horizon"]]
        if p.get("exit_reason"):
            notes.append(f"exit {p['exit_reason']}")
        if pm.get("quadrant"):
            notes.append(pm["quadrant_label"])
        if pm.get("lesson"):
            notes.append(f"lesson: {pm['lesson']}")
        rows.append({"date": p["entry_date"], "symbol": th["symbol"], "side": th["direction"],
                     "entry": _g(p["entry_price"]), "exit": _g(p.get("exit_price")), "stop": _g(stop),
                     "qty": _g(p["qty"]), "charges": _g(p.get("charges") or 0.0) if p.get("exit_price") is not None else "",
                     "exit_date": p.get("exit_date") or "", "setup": th.get("setup") or (th["tags"][0] if th["tags"] else ""),
                     "mistake": ";".join(pm.get("mistakes", [])), "segment": JOURNAL_SEGMENT[th["segment"]],
                     "notes": " | ".join(notes)})
    rows.sort(key=lambda r: (r["exit_date"] or "9999", r["date"], r["symbol"]))
    return rows


def cmd_export(a, today: dt.date) -> dict:
    store = load_store(resolve_store(a.store))
    out = Path(a.journal).expanduser().resolve()
    if out == store.path or out == store.path.with_name(store.path.name + ".bak"):
        raise ValueError("--journal must not be the thesis store itself")
    if out == SKILL_DIR or SKILL_DIR in out.parents:
        raise ValueError(f"{out} is inside the skill folder; write the journal next to your own files")
    if not out.parent.is_dir():
        raise ValueError(f"folder {out.parent} does not exist")
    if out.exists() and not a.overwrite:
        raise ValueError(f"{out} already exists; add --overwrite to replace it (a hand-kept journal would be lost)")
    rows = journal_rows(store.theses, a.closed_only)
    if not rows:
        raise ValueError("nothing to export: no active or closed theses" if not a.closed_only
                         else "nothing to export: no closed theses")
    fd, tmp = tempfile.mkstemp(prefix=out.name + ".", suffix=".tmp", dir=str(out.parent))
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=JOURNAL_FIELDS)
            w.writeheader()
            w.writerows(rows)
        os.replace(tmp, out)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    open_rows = sum(1 for r in rows if not r["exit"])
    notes = []
    if open_rows:
        notes.append(f"{open_rows} open row(s) have a blank exit. discipline.py treats them as open trades; "
                     "risk.py journal does not, so export with --closed-only for it.")
    return {"as_of": today.isoformat(), "store": str(store.path), "journal": str(out), "rows": len(rows),
            "closed": len(rows) - open_rows, "open": open_rows, "columns": JOURNAL_FIELDS, "notes": notes}


CARD_FIELDS = [
    ("hypothesis", "One sentence that can be proved wrong: IF <observable condition> THEN <instrument> "
                   "<measurable move> WITHIN <time>, BECAUSE <mechanism>."),
    ("universe", "Where it should hold: NIFTY 500 / F&O list / one sector; delivery, futures or options; market regime."),
    ("evidence_for", "Up to three observations, each with a source and a date."),
    ("evidence_against", "What a sceptic would say. Who is on the other side of the trade, and why might they be right?"),
    ("base_rate", "How often has this worked before? If unknown, that is what the experiment is for."),
    ("experiment", "The backtest-lab run that could kill it: strategy, parameters, years, charges on, next-open fills."),
    ("pass_marks", "Written BEFORE running: e.g. expectancy after charges > 0, profit factor >= 1.3, drawdown you "
                   "can sit through, still positive walk-forward."),
    ("sample_size", "Trades needed before believing the win rate (see the table below). Fewer trades = an anecdote."),
    ("kill_criteria", "What result, price or event ends the idea. Include India triggers: ASM/GSM entry, F&O ban, "
                      "results miss, pledge jump, auditor exit."),
    ("next_step", "If it survives: register it with `thesis.py add`. If not: write down why and move on."),
]


def sample_size(win_rate_pct: float, margin_pct: float) -> int:
    """Trades needed to pin a win rate within +/- margin at roughly 95% confidence (normal approximation)."""
    p, m = win_rate_pct / 100, margin_pct / 100
    return math.ceil(1.96 ** 2 * p * (1 - p) / (m * m))


def cmd_hypothesis(a, today: dt.date) -> dict:
    if not 0 < a.expected_win_rate < 100:
        raise ValueError("--expected-win-rate must be between 0 and 100")
    if not 0 < a.margin <= 50:
        raise ValueError("--margin must be between 0 and 50 (percentage points)")
    margins = sorted({5.0, 10.0, 15.0, float(a.margin)})
    return {"as_of": today.isoformat(), "symbol": " ".join(a.symbol.upper().split()) if a.symbol else None,
            "card": {key: "" for key, _ in CARD_FIELDS}, "prompts": dict(CARD_FIELDS),
            "sample_size": {"expected_win_rate_pct": a.expected_win_rate, "margin_pct": a.margin,
                            "trades_needed": sample_size(a.expected_win_rate, a.margin),
                            "table": [{"margin_pct": m, "trades_needed": sample_size(a.expected_win_rate, m)}
                                      for m in margins]},
            "experiment_example": "python3 ../backtest-lab/scripts/backtest.py run --csv SYMBOL.csv "
                                  "--strategy donchian-breakout --risk-pct 1"}


# --------------------------------------------------------------------------- text output
def _line(th: dict) -> str:
    return f"{th['id']}  {th['symbol']} {th['direction']} {th['horizon']} [{th['state']}]"


def print_thesis(th: dict, today: dt.date) -> None:
    print(f"\n{th['id']}  {th['symbol']}  {th['direction']}  {th['horizon']}  {th['segment']}  [{th['state']}]")
    print(f"  Statement   {th['statement']}")
    extra = [f"source {th['source']}"]
    if th.get("setup"):
        extra.append(f"setup {th['setup']}")
    if th["tags"]:
        extra.append("tags " + ", ".join(th["tags"]))
    print(f"  From        {' | '.join(extra)} | created {th['created']}")
    trig = " @ ".join(x for x in (th.get("entry_trigger"), inr(th["trigger_price"]) if th.get("trigger_price") else None) if x)
    print(f"  Trigger     {trig or 'not set'}")
    kill = " | ".join(x for x in (inr(th["kill_price"]) if th.get("kill_price") is not None else None,
                                  th.get("kill_condition")) if x)
    print(f"  Kill        {kill or 'NOT SET: no kill criteria, no trade'}")
    plan = [f"stop {inr(th['stop'])}" if th.get("stop") is not None else "stop not set"]
    if th["targets"]:
        plan.append("targets " + ", ".join(inr(t) for t in th["targets"]))
    if th.get("planned_rr") is not None:
        plan.append(f"R:R {th['planned_rr']:.2f}")
    if th.get("risk_pct") is not None:
        plan.append(f"risk {th['risk_pct']:g}% of capital")
    print(f"  Plan        {' | '.join(plan)}")
    for c in th.get("catalysts", []):
        gap = (as_date(c["date"]) - today).days
        when = f"in {gap} days" if gap > 0 else "today" if gap == 0 else f"{-gap} days ago"
        print(f"  Catalyst    {c['date']} ({when})  {c['label']}")
    if th.get("expiry"):
        print(f"  Expiry      {th['expiry']}")
    if th.get("review_due"):
        late = (today - as_date(th["review_due"])).days
        print(f"  Review due  {th['review_due']}" + (f"  ({late} days overdue)" if late > 0 else ""))
    p = th.get("position")
    if p:
        risk = f", risk {inr(th['risk_amount'])}" if th.get("risk_amount") else ""
        stop = f"initial stop {inr(p['initial_stop'])}" if p.get("initial_stop") is not None else "no stop: premium at risk"
        print(f"  Position    {p['qty']:g} @ {inr(p['entry_price'])} on {p['entry_date']} ({stop}{risk})")
    r = th.get("result")
    if r:
        tail = f" | held {r['holding_days']} days" if "holding_days" in r else ""
        tail += f", {r['tax_term']}" if r.get("tax_term") else ""
        print(f"  Result      {inr(r['pnl'], True)} ({rmult(r['r'])}) {r['outcome']} | exit {inr(p['exit_price'])} on "
              f"{p['exit_date']} ({p['exit_reason']}){tail}")
    pm = th.get("postmortem")
    if pm:
        print_postmortem(pm)
    print("  History")
    for h in th.get("history", []):
        if h["event"] == "transition":
            what = f"{h['from']} -> {h['to']}"
        elif h["event"] == "created":
            what = "created as idea"
        elif h["event"] == "update":
            what = "updated " + ", ".join(f"{k}: {v['from']} -> {v['to']}" for k, v in h.get("changes", {}).items())
            what += "  [STOP WIDENED]" if h.get("stop_widened") else ""
        else:
            what = h["event"]
        print(f"    {h['date']}  {what}" + (f": {h['note']}" if h.get("note") else ""))


def print_postmortem(pm: dict) -> None:
    print(f"  Postmortem  {pm['quadrant_label']} | thesis {pm['thesis']}, result {pm['result']}"
          + (f" {inr(pm['pnl'], True)} ({rmult(pm['r'])})" if pm.get("pnl") is not None else ""))
    plan = pm.get("plan")
    if plan:
        print(f"              plan followed: {yn(plan['followed'])} (entry at trigger {yn(plan['entry_at_trigger'])}, "
              f"stop honoured {yn(plan['stop_honoured'])}, exited early {yn(plan['exited_early'])}"
              + (", stop widened" if plan.get("stop_widened") else "") + ")")
    print(f"              mistakes: {', '.join(pm['mistakes']) or 'none'}" + (f" | lesson: {pm['lesson']}" if pm.get("lesson") else ""))


def print_group(title: str, g: dict) -> None:
    if not g:
        return
    print(f"\n{title}")
    for k, v in g.items():
        print(f"  {k:<20} n={v['trades']:<3} win {pct(v['win_rate_pct']):>6}  net {inr(v['net_pnl'], True):>11}"
              f"  exp {inr(v['expectancy'], True):>9}  avg {rmult(v['avg_R']):>7}")


def render(cmd: str, res: dict, today: dt.date) -> None:
    if cmd in ("add", "update", "transition", "note"):
        th = res["thesis"]
        verb = {"added": "Registered", "updated": "Updated", "noted": "Noted on",
                "transition": f"Moved {res.get('from')} -> {res.get('to')}:"}[res["action"]]
        print(f"{verb} {_line(th)} in {res['store']}")
        if res["action"] == "transition" and th.get("result"):
            r = th["result"]
            print(f"  Result {inr(r['pnl'], True)} ({rmult(r['r'])}) {r['outcome']}")
        if th.get("review_due"):
            print(f"  Next review due {th['review_due']}")
        for w in res["warnings"]:
            print("  •", w)
    elif cmd == "list":
        c = res["counts"]
        print(f"\nTheses in {res['store']} (as of {day_label(today)}): "
              + ", ".join(f"{c[s]} {s}" for s in STATES if c[s]) if any(c.values()) else f"\nNo theses in {res['store']} yet.")
        if res["theses"]:
            print(f"  {'ID':<6} {'STATE':<12} {'SYMBOL':<14} {'DIR':<6} {'HORIZON':<11} {'SOURCE':<18} {'REVIEW DUE':<11} STATEMENT")
        elif any(c.values()):
            print("  Nothing matches these filters (closed, invalidated and expired need --all or --state).")
        for t in res["theses"]:
            text = t["statement"] if len(t["statement"]) <= 60 else t["statement"][:57] + "..."
            print(f"  {t['id']:<6} {t['state']:<12} {t['symbol'][:14]:<14} {t['direction']:<6} {t['horizon']:<11} "
                  f"{t['source'][:18]:<18} {t.get('review_due') or '-':<11} {text}")
    elif cmd == "show":
        print_thesis(res["thesis"], today)
    elif cmd == "due":
        print(f"\nThesis check as of {day_label(today)} ({res['open_theses']} open in {res['store']})")
        if res["kill_hits"]:
            print(f"\nKill level hit ({len(res['kill_hits'])}): confirm with the trader before changing any state")
            for k in res["kill_hits"]:
                sign = "<=" if k["direction"] == "long" else ">="
                print(f"  {k['id']} {k['symbol']} {k['direction']} [{k['state']}]  LTP {inr(k['ltp'])} {sign} "
                      f"{k['level']} {inr(k['level_price'])}")
                print(f"      {k['action']}")
        if res["targets_hit"]:
            print(f"\nTarget reached ({len(res['targets_hit'])})")
            for t in res["targets_hit"]:
                u = t["unrealised"]
                print(f"  {t['id']} {t['symbol']}  LTP {inr(t['ltp'])} past {', '.join(inr(x) for x in t['targets_hit'])}"
                      f"  unrealised {inr(u['pnl'], True)} ({rmult(u['r'])}): book, trail, or restate the thesis")
        if res["reviews_due"]:
            print(f"\nReviews due ({len(res['reviews_due'])})")
            for r in res["reviews_due"]:
                late = f"{r['days_overdue']} days overdue" if r["days_overdue"] else "due today"
                print(f"  {r['id']} {r['symbol']} {r['horizon']} [{r['state']}]  due {r['review_due']} ({late})")
        if res["catalysts"]:
            print(f"\nCatalysts and expiries in the next {res['window_days']} days ({len(res['catalysts'])})")
            for c in res["catalysts"]:
                when = "today" if c["in_days"] == 0 else f"in {c['in_days']} days" if c["in_days"] > 0 else f"{-c['in_days']} days ago"
                print(f"  {c['id']} {c['symbol']} [{c['state']}]  {c['date']} ({when})  {c['label']}"
                      + (f": {c['hint']}" if c["hint"] else ""))
        if res["turning_long_term"]:
            print(f"\nTurning long-term within {res['ltcg_window_days']} days ({len(res['turning_long_term'])})")
            for x in res["turning_long_term"]:
                saved = (f" (about {inr(x['tax_saved_estimate'])} less tax on the {inr(x['unrealised'])} unrealised gain, "
                         "before cess and the ₹1.25 lakh exemption)") if x.get("tax_saved_estimate") else ""
                print(f"  {x['id']} {x['symbol']}  bought {x['entry_date']}: {x['hint']}{saved}")
        if not any(res[k] for k in ("kill_hits", "targets_hit", "reviews_due", "catalysts", "turning_long_term")):
            print("  Nothing due.")
        for n in res["notes"]:
            print("  •", n)
        if res["turning_long_term"]:
            print("  • The tax clock never overrides a kill level: a broken thesis is exited whatever the holding period.")
    elif cmd == "stale":
        print(f"\nIdeas with no activity for {res['threshold_days']}+ days (as of {day_label(today)}): {len(res['stale'])}")
        for s in res["stale"]:
            print(f"  {s['id']} {s['symbol']} {s['direction']} [{s['state']}]  quiet {s['quiet_days']} days "
                  f"(created {s['created']})")
            print(f"      {s['prompt']}")
        if res["stale"]:
            print("  Ask before expiring anything; an expired thesis can be revived to watching later.")
    elif cmd == "postmortem":
        th = res["thesis"]
        if res["action"] == "postmortem-draft":
            print(f"\nPostmortem draft for {_line(th)} (nothing saved yet)")
            f = res["facts"]
            if f["result"]:
                r = f["result"]
                print(f"  Result {inr(r['pnl'], True)} ({rmult(r['r'])}) {r['outcome']}"
                      + (f" | held {r['holding_days']} days" if "holding_days" in r else ""))
                p = f["plan"]
                print(f"  From the record: entry at trigger {yn(p['entry_at_trigger'])}, stop honoured "
                      f"{yn(p['stop_honoured'])}, exited early {yn(p['exited_early'])}"
                      + (", STOP WAS WIDENED" if p["stop_widened"] else ""))
            print(f"  Statement: {th['statement']}")
            print("  Ask the trader:")
            for q in res["questions"]:
                print("   -", q)
        else:
            print(f"\nPostmortem for {_line(th)}")
            print_postmortem(res["postmortem"])
    elif cmd == "stats":
        s, c = res["summary"], res["counts"]
        print(f"\nThesis statistics as of {day_label(today)}" + (f" (closed since {res['since']})" if res["since"] else ""))
        print("  Pipeline: " + ", ".join(f"{c[k]} {k}" for k in STATES))
        if s["trades"]:
            print(f"  Closed {s['trades']} (W{s['wins']}/L{s['losses']}/BE{s['breakevens']}) | win rate {pct(s['win_rate_pct'])}"
                  f" | net {inr(s['net_pnl'], True)}")
            print(f"  Avg win {inr(s['avg_win'])} | avg loss {inr(s['avg_loss'])} | profit factor "
                  f"{'n/a' if s['profit_factor'] is None else format(s['profit_factor'], '.2f')} | expectancy "
                  f"{inr(s['expectancy'], True)}/trade ({rmult(s['avg_R'])} over {s['trades_with_R']} with a stop)")
            for title, key in (("By source skill", "by_source"), ("By setup", "by_setup"), ("By tag", "by_tag"),
                               ("By horizon", "by_horizon"), ("By direction", "by_direction"),
                               ("By segment", "by_segment")):
                print_group(title, res[key])
            q, pmc = res["quadrants"], res["postmortems"]
            print(f"\nThesis vs result ({pmc['reviewed']} of {pmc['closed']} reviewed)")
            print(f"  {'':<14}{'good result':>12}{'bad result':>12}")
            print(f"  {'right thesis':<14}{q['right-good']:>12}{q['right-bad']:>12}")
            print(f"  {'wrong thesis':<14}{q['wrong-good']:>12}{q['wrong-bad']:>12}   breakeven {q['flat']}")
            pa = res["plan_adherence"]
            print(f"\nPlan adherence: {pct(pa['rate_pct'])} ({pa['followed']} of {pa['judged']} judged)")
            if res["mistakes"]:
                print("\nMost common mistakes")
                for m, e in res["mistakes"].items():
                    print(f"  {m:<20} n={e['count']:<3} lost {inr(e['loss_cost']):>9}  net {inr(e['net_pnl'], True)}")
        nt = res["not_traded"]
        if nt["invalidated"] or nt["expired"]:
            print(f"\nNever traded: {nt['invalidated']} invalidated, {nt['expired']} expired")
        for n in res["notes"]:
            print("  •", n)
    elif cmd == "export":
        print(f"Wrote {res['rows']} journal row(s) to {res['journal']} ({res['closed']} closed, {res['open']} open)")
        print("  Read it with: python3 ../trading-discipline/scripts/discipline.py review " + res["journal"])
        for n in res["notes"]:
            print("  •", n)
    elif cmd == "hypothesis":
        print("\nFALSIFIABLE HYPOTHESIS CARD" + (f": {res['symbol']}" if res["symbol"] else "") + f"   ({today})")
        for i, (key, prompt) in enumerate(CARD_FIELDS, start=1):
            print(f"\n{i:>2}. {key.replace('_', ' ').upper()}\n    {prompt}\n    > ")
        ss = res["sample_size"]
        print(f"\nSample size for a {ss['expected_win_rate_pct']:g}% win rate (95% confidence)")
        for row in ss["table"]:
            print(f"  within ±{row['margin_pct']:g} points: {row['trades_needed']} trades")
        print(f"\nExperiment: {res['experiment_example']}")
        print("Fill the card in conversation. Nothing is saved until the idea is registered with `add`.")


# --------------------------------------------------------------------------- CLI
class _Parser(argparse.ArgumentParser):
    def error(self, message):
        print(f"error: {message}", file=sys.stderr)
        raise SystemExit(2)


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--store", help=f"thesis store JSON (default ./{DEFAULT_STORE} in the current folder)")
    common.add_argument("--as-of", type=_date_arg, help="treat this date as today, YYYY-MM-DD (default: today IST)")
    common.add_argument("--json", action="store_true")

    p = _Parser(description="Thesis lifecycle tracker: idea -> watching -> entry-ready -> active -> closed")
    sub = p.add_subparsers(dest="cmd", required=True)

    def plan_args(sp, adding: bool) -> None:
        sp.add_argument("--statement", required=adding, help="what must be true for this trade to work")
        sp.add_argument("--trigger", help="entry trigger in words")
        sp.add_argument("--trigger-price", type=_positive)
        sp.add_argument("--kill", help="condition that proves the thesis wrong (results miss, ASM/GSM entry, ...)")
        sp.add_argument("--kill-price", type=_positive, help="price that proves the thesis wrong")
        sp.add_argument("--stop", type=_positive, help="planned stop-loss price")
        sp.add_argument("--target", type=_positive, action="append", help="target price (repeat for several)")
        sp.add_argument("--risk-pct", type=_positive, help="planned risk, %% of capital")
        sp.add_argument("--catalyst", type=_catalyst, action="append", metavar="DATE:LABEL",
                        help="dated event, e.g. 2026-11-20:Q2 results (repeatable)")
        sp.add_argument("--setup", help="setup name; becomes the journal's setup column")
        sp.add_argument("--tag", action="append", help="free tag (repeatable)")
        sp.add_argument("--source", help="skill or place the idea came from, e.g. breakout-planner")
        sp.add_argument("--expiry", type=_date_arg, help="contract expiry (futures / options)")
        sp.add_argument("--review-due", type=_date_arg, help="next review date (default by horizon: 7 / 30 / 90 days)")
        sp.add_argument("--note", default="", help="why, in a sentence")

    a = sub.add_parser("add", parents=[common], help="register a thesis as an idea")
    a.add_argument("symbol")
    a.add_argument("--direction", choices=["long", "short"], required=True)
    a.add_argument("--horizon", choices=list(HORIZONS), required=True)
    a.add_argument("--segment", choices=SEGMENTS, default="delivery")
    plan_args(a, adding=True)

    u = sub.add_parser("update", parents=[common], help="change the plan; old values stay in the history")
    u.add_argument("id")
    u.add_argument("--horizon", choices=list(HORIZONS))
    u.add_argument("--clear-catalysts", action="store_true")
    plan_args(u, adding=False)

    t = sub.add_parser("transition", parents=[common], help="move a thesis to another lifecycle state")
    t.add_argument("id")
    t.add_argument("state", choices=STATES)
    t.add_argument("--note", default="")
    t.add_argument("--date", type=_date_arg, help="date it happened (default: today IST)")
    t.add_argument("--entry-price", type=_positive)
    t.add_argument("--qty", type=_positive, help="shares, or total quantity for F&O (lots x lot size)")
    t.add_argument("--exit-price", type=_positive)
    t.add_argument("--reason", choices=EXIT_REASONS)
    t.add_argument("--charges", type=_non_negative, help="round-trip charges in ₹ (see trading-costs)")

    n = sub.add_parser("note", parents=[common], help="append a dated observation")
    n.add_argument("id")
    n.add_argument("text")
    n.add_argument("--date", type=_date_arg)
    n.add_argument("--reviewed", action="store_true", help="count this as the review and schedule the next one")
    n.add_argument("--next-review", type=_date_arg)

    ls = sub.add_parser("list", parents=[common], help="list theses (open ones by default)")
    ls.add_argument("--state", action="append", choices=STATES)
    ls.add_argument("--symbol")
    ls.add_argument("--tag", help="matches a tag or the setup")
    ls.add_argument("--source")
    ls.add_argument("--due", action="store_true", help="only theses whose review is due")
    ls.add_argument("--all", action="store_true", help="include closed, invalidated and expired")

    sh = sub.add_parser("show", parents=[common], help="one thesis with its full history")
    sh.add_argument("id")

    d = sub.add_parser("due", parents=[common], help="what needs attention today")
    d.add_argument("--prices", help="CSV with symbol,ltp columns: checks kill levels, stops and targets")
    d.add_argument("--days", type=int, default=7, help="look-ahead for catalysts and expiries")
    d.add_argument("--ltcg-days", type=int, default=45, help="look-ahead for holdings turning long-term")

    st = sub.add_parser("stale", parents=[common], help="ideas never acted on")
    st.add_argument("--days", type=int, default=21)

    pm = sub.add_parser("postmortem", parents=[common], help="closing review of a finished thesis")
    pm.add_argument("id")
    pm.add_argument("--thesis", choices=["right", "wrong"], help="did the written statement turn out true?")
    pm.add_argument("--entry-at-trigger", type=_yesno)
    pm.add_argument("--stop-honoured", type=_yesno)
    pm.add_argument("--exited-early", type=_yesno)
    pm.add_argument("--mistake", action="append", help="mistake tag (repeatable; 'none' for a clean trade)")
    pm.add_argument("--lesson", default="")
    pm.add_argument("--tolerance-pct", type=_non_negative, default=1.0,
                    help="slack when comparing fills with the trigger and stop")
    pm.add_argument("--revise", action="store_true", help="replace an existing postmortem")

    ss = sub.add_parser("stats", parents=[common], help="results across closed theses")
    ss.add_argument("--since", type=_date_arg, help="only theses closed on or after this date")

    ex = sub.add_parser("export", parents=[common], help="write theses as trading-discipline journal rows")
    ex.add_argument("--journal", required=True, metavar="OUT.csv")
    ex.add_argument("--closed-only", action="store_true", help="leave out open positions (needed for risk.py journal)")
    ex.add_argument("--overwrite", action="store_true")

    hy = sub.add_parser("hypothesis", parents=[common], help="print a blank falsifiable-hypothesis card")
    hy.add_argument("--symbol")
    hy.add_argument("--expected-win-rate", type=float, default=50.0)
    hy.add_argument("--margin", type=float, default=10.0, help="acceptable error on the win rate, percentage points")
    return p


COMMANDS = {"add": cmd_add, "update": cmd_update, "transition": cmd_transition, "note": cmd_note,
            "list": cmd_list, "show": cmd_show, "due": cmd_due, "stale": cmd_stale,
            "postmortem": cmd_postmortem, "stats": cmd_stats, "export": cmd_export, "hypothesis": cmd_hypothesis}


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    today = a.as_of or dt.datetime.now(IST).date()
    try:
        res = COMMANDS[a.cmd](a, today)
    except (ValueError, OSError, csv.Error) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(res, indent=2, ensure_ascii=False, default=str))
    else:
        render(a.cmd, res, today)
    return 0


if __name__ == "__main__":
    sys.exit(main())
