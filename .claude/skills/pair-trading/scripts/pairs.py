#!/usr/bin/env python3
"""Statistical pair trading for NSE stocks.

Answers four questions about two stocks: do their prices share a long-run equilibrium
(cointegration), how quickly does the gap close when it opens (half-life), where is the gap today
(z-score), and what does it take to hold the trade in India, where an overnight short has to sit
in stock futures (whole lots, margin, charges, monthly rollover, F&O ban, lot-size revisions).

Zero dependencies (Python 3.10+ stdlib). Optional: `pip install yfinance` to fetch NSE history.
Re-uses ../../technical-analysis/scripts/ta.py (date parsing, Yahoo fetch),
../../trading-costs/scripts/charges.py (futures and delivery charges) and
../../fno-options-analysis/scripts/options.py (stock-futures expiry calendar and holidays).

Input
  --csv-dir DIR   one CSV per stock, file stem = NSE symbol (TCS.csv or TCS.NS.csv). A date and a
                  close column are required; a volume column enables the liquidity filter. Rows
                  with an empty, zero or "null" close are skipped.
  (default)       fetch SYMBOL.NS through yfinance (--period, default 3y)

Sub-commands
  test A B        statistics for one pair: return correlation, hedge ratios, Engle-Granger
                  cointegration test (both directions), half-life, current z-score, stability
  screen          every same-sector pair from --members (symbol,sector), ranked
  signal A B      z-score rules (enter |z| >= 2, exit near 0, stop |z| >= 3.5 or 2 x half-life)
                  with the in-sample trade list and hit rate
  plan A B        India execution: futures lots, residual hedge mismatch, margin, charges,
                  rollover dates, F&O ban and lot-revision risk

Conventions
  The spread is s_t = ln A_t - beta * ln B_t - alpha, from OLS of ln A on ln B over the look-back.
  A hedge ratio beta means: for every Rs 1 of A, hold Rs beta of B on the opposite side.
  "Long the spread" = long A / short B (taken when z is very negative); "short" is the reverse.

Examples
  python3 pairs.py test HDFCBANK ICICIBANK --csv-dir stocks/
  python3 pairs.py screen --members members.csv --csv-dir stocks/ --top 15
  python3 pairs.py signal BPCL HPCL --csv-dir stocks/ --exit 0.5
  python3 pairs.py plan BPCL HPCL --csv-dir stocks/ --lots-csv fo_mktlots.csv --ban-list fo_secban.csv
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import itertools
import json
import math
import re
import statistics
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

_SKILLS = Path(__file__).resolve().parents[2]


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(f"pair_trading_{name}", _SKILLS / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ta = _load("ta", "technical-analysis/scripts/ta.py")
charges = _load("charges", "trading-costs/scripts/charges.py")
options = _load("options", "fno-options-analysis/scripts/options.py")


class PairError(Exception):
    """Bad input the user can fix; printed as one `error:` line with exit code 2."""


# ---------------------------------------------------------------------------
# Critical values and p-values for the Engle-Granger test (2 variables, constant)
# ---------------------------------------------------------------------------
# The ADF statistic computed on OLS residuals is NOT judged against ordinary Dickey-Fuller tables:
# the cointegrating regression has already picked the beta that makes the residual look as
# stationary as possible, so the null distribution sits further left.
#
# Critical values: J. G. MacKinnon (2010), "Critical Values for Cointegration Tests", Queen's
# Economics Department Working Paper No. 1227, Table 2 (response surfaces, updating MacKinnon
# 1991), case N = 2 variables with a constant in the cointegrating regression:
#     C(T) = b_inf + b1/T + b2/T^2 + b3/T^3
EG_CRIT_N2 = {
    "1%": (-3.89644, -10.9519, -33.527, 0.0),
    "5%": (-3.33613, -6.1101, -6.823, 0.0),
    "10%": (-3.04445, -4.2412, -2.720, 0.0),
}
# Approximate p-values: J. G. MacKinnon (1994), "Approximate Asymptotic Distribution Functions for
# Unit-Root and Cointegration Tests", Journal of Business & Economic Statistics 12(2), 167-176.
# p = Phi(polynomial in tau), N = 2, constant. Asymptotic, so slightly optimistic in short samples;
# the finite-sample critical values above are the yardstick, the p-value is for ranking.
EG_P_SMALL = (2.92, 1.5012, 0.039796)               # tau <= -2.62
EG_P_LARGE = (2.1945, 0.64695, -0.29198, -0.042377)  # tau  > -2.62
EG_P_TAU_STAR, EG_P_TAU_MIN, EG_P_TAU_MAX = -2.62, -18.86, 0.92

LEVELS = ("1%", "5%", "10%")
LIQ_SESSIONS = 60


def crit_value(coefs: tuple, T: int) -> float:
    b0, b1, b2, b3 = coefs
    return b0 + b1 / T + b2 / T ** 2 + b3 / T ** 3


def eg_critical(T: int) -> dict:
    return {k: crit_value(v, T) for k, v in EG_CRIT_N2.items()}


def eg_pvalue(tau: float | None) -> float | None:
    if tau is None or math.isnan(tau):
        return None
    if tau > EG_P_TAU_MAX:
        return 1.0
    if tau < EG_P_TAU_MIN:
        return 0.0
    coefs = EG_P_SMALL if tau <= EG_P_TAU_STAR else EG_P_LARGE
    z = sum(c * tau ** i for i, c in enumerate(coefs))
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
DATE_COLS = ("date", "timestamp", "datetime", "time", "date time")
CLOSE_COLS = ("close", "close price", "closing price", "ltp", "last price", "price")
VOLUME_COLS = ("volume", "vol", "total traded quantity", "shares traded", "qty", "traded qty")


def norm(sym: str) -> str:
    s = sym.strip().upper()
    for suf in (".NS", ".BO"):
        if s.endswith(suf):
            s = s[: -len(suf)]
    return re.sub(r"[^A-Z0-9&]", "", s)


def _parse_date(raw: str) -> date:
    try:
        return ta.parse_ts(raw).date()
    except ValueError:
        for fmt in ("%b %d, %Y", "%d-%b-%y", "%d %b %y", "%d-%B-%Y", "%d %B %Y"):
            try:
                return datetime.strptime(raw, fmt).date()
            except ValueError:
                continue
    raise ValueError(raw)


def load_prices(path: str | Path) -> list:
    """[(date, close, volume or None)] sorted by date; null/zero closes and bad dates skipped."""
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        cols = {c.strip().lower(): c for c in reader.fieldnames or [] if c}
        cd = next((cols[c] for c in DATE_COLS if c in cols), None)
        cc = next((cols[c] for c in CLOSE_COLS if c in cols), None)
        cv = next((cols[c] for c in VOLUME_COLS if c in cols), None)
        if not cd or not cc:
            raise PairError(f"{Path(path).name}: need a date and a close column; found {list(cols)}")
        out = {}
        for r in reader:
            raw = (r.get(cd) or "").strip()
            if not raw:
                continue
            try:
                d = _parse_date(raw)
                c = ta._f(r.get(cc) or "")
                v = ta._f(r.get(cv) or "") if cv else math.nan
            except ValueError:
                continue
            if math.isnan(c) or c <= 0:
                continue
            out[d] = (c, None if math.isnan(v) else v)
    return [(d, c, v) for d, (c, v) in sorted(out.items())]


def find_csv(directory: str, symbol: str) -> Path | None:
    d = Path(directory)
    if not d.is_dir():
        raise PairError(f"--csv-dir {directory}: not a directory")
    want = norm(symbol)
    for p in sorted(d.iterdir()):
        if p.suffix.lower() == ".csv" and norm(p.stem) == want:
            return p
    return None


def get_series(symbol: str, csv_dir: str | None, period: str) -> tuple:
    """(rows, source) for one symbol, from --csv-dir or Yahoo."""
    if csv_dir:
        p = find_csv(csv_dir, symbol)
        if p is None:
            raise PairError(f"{symbol}: no CSV named {norm(symbol)}.csv in {csv_dir}")
        rows = load_prices(p)
        src = p.name
    else:
        try:
            raw = ta.fetch_yf(symbol, period=period)
        except SystemExit as e:
            msg = str(e)
            if "not installed" in msg:
                raise PairError("yfinance not installed: pip install yfinance  (or pass --csv-dir)")
            raise PairError(f"{symbol}: {msg}")
        rows = [(r["date"].date(), r["close"], r["volume"] or None) for r in raw
                if r["close"] == r["close"] and r["close"] > 0]
        src = ta.yf_ticker(symbol)
    if not rows:
        raise PairError(f"{symbol}: no usable price rows")
    return rows, src


def liquidity_cr(rows: list) -> float | None:
    """Median daily traded value (close x volume) over the last 60 sessions, in Rs crore."""
    vals = [c * v for _, c, v in rows[-LIQ_SESSIONS:] if v]
    return statistics.median(vals) / 1e7 if vals else None


def align(ra: list, rb: list, lookback: int, end: date | None = None) -> tuple:
    """Common dates only (a date missing in either file is dropped), up to `end`, then the last
    `lookback` of them."""
    mb = {d: c for d, c, _ in rb}
    dates = [d for d, _, _ in ra if d in mb and (end is None or d <= end)]
    ma = {d: c for d, c, _ in ra}
    if lookback:
        dates = dates[-lookback:]
    return dates, [ma[d] for d in dates], [mb[d] for d in dates]


# ---------------------------------------------------------------------------
# Statistics (stdlib only)
# ---------------------------------------------------------------------------
def _mean(xs) -> float:
    xs = list(xs)
    return math.fsum(xs) / len(xs)


def ols(x: list, y: list) -> tuple:
    """Ordinary least squares y = alpha + beta*x. Returns (alpha, beta).

    beta = sum((x-mx)(y-my)) / sum((x-mx)^2), alpha = my - beta*mx.
    """
    if len(x) != len(y) or len(x) < 3:
        raise PairError("regression needs at least 3 paired observations")
    mx, my = _mean(x), _mean(y)
    sxx = math.fsum((a - mx) ** 2 for a in x)
    if sxx == 0:
        raise PairError("one series is constant over the sample; regression undefined")
    sxy = math.fsum((a - mx) * (b - my) for a, b in zip(x, y))
    beta = sxy / sxx
    return my - beta * mx, beta


def pearson(x: list, y: list) -> float | None:
    mx, my = _mean(x), _mean(y)
    sxx = math.fsum((a - mx) ** 2 for a in x)
    syy = math.fsum((b - my) ** 2 for b in y)
    if sxx == 0 or syy == 0:
        return None
    return math.fsum((a - mx) * (b - my) for a, b in zip(x, y)) / math.sqrt(sxx * syy)


def tls_slope(x: list, y: list) -> float | None:
    """Total-least-squares (orthogonal) slope of y on x.

    OLS puts all the noise in y, so y-on-x and the inverted x-on-y slopes disagree; the
    orthogonal fit minimises perpendicular distance and lies between them. It is symmetric:
    swapping the legs gives exactly the reciprocal slope.
    """
    mx, my = _mean(x), _mean(y)
    sxx = math.fsum((a - mx) ** 2 for a in x)
    syy = math.fsum((b - my) ** 2 for b in y)
    sxy = math.fsum((a - mx) * (b - my) for a, b in zip(x, y))
    if sxy == 0:
        return None
    d = syy - sxx
    return (d + math.sqrt(d * d + 4 * sxy * sxy)) / (2 * sxy)


def log_returns(p: list) -> list:
    return [math.log(p[i] / p[i - 1]) for i in range(1, len(p))]


def _inverse(m: list) -> list:
    """Gauss-Jordan inverse with partial pivoting for the small normal-equation matrices here."""
    k = len(m)
    scale = max(abs(m[i][i]) for i in range(k)) or 1.0
    a = [list(row) + [1.0 if i == j else 0.0 for j in range(k)] for i, row in enumerate(m)]
    for col in range(k):
        piv = max(range(col, k), key=lambda r: abs(a[r][col]))
        if abs(a[piv][col]) <= 1e-13 * scale:
            raise ValueError("singular matrix")
        a[col], a[piv] = a[piv], a[col]
        pv = a[col][col]
        a[col] = [v / pv for v in a[col]]
        for r in range(k):
            f = a[r][col]
            if r != col and f != 0.0:
                a[r] = [v - f * w for v, w in zip(a[r], a[col])]
    return [row[k:] for row in a]


def schwert_max_lag(n: int) -> int:
    """Schwert (1989) rule of thumb: floor(12 * (n/100)^(1/4))."""
    return int(12 * (n / 100) ** 0.25)


def adf(e: list, lag: int | None = None, max_lag: int | None = None) -> dict:
    """Augmented Dickey-Fuller regression with NO constant on a residual series:

        de_t = gamma * e_(t-1) + sum_{i=1..p} phi_i * de_(t-i) + u_t

    The statistic is t = gamma_hat / se(gamma_hat); strongly negative = the residual is pulled
    back towards zero (stationary). No constant or trend is added because OLS residuals already
    have mean zero; the constant sits in the cointegrating regression, which is the case the
    MacKinnon N = 2 'constant' critical values describe.

    Lag p: `lag` fixes it; otherwise p is chosen by AIC = nobs*ln(SSR/nobs) + 2k over 0..max_lag
    (default: Schwert's rule) on one common sample, then the chosen model is re-fitted on every
    usable row.
    """
    n = len(e)
    de = [e[t] - e[t - 1] for t in range(1, n)]          # de[t-1] is the change into bar t
    fixed = lag is not None
    top = lag if fixed else (schwert_max_lag(n) if max_lag is None else max_lag)
    top = max(0, min(top, (n - 10) // 3))

    def cross(p: int, start: int):
        k = p + 1
        xtx = [[0.0] * k for _ in range(k)]
        xty = [0.0] * k
        yty = 0.0
        for t in range(start, n):
            x = [e[t - 1]] + [de[t - 1 - i] for i in range(1, p + 1)]
            y = de[t - 1]
            yty += y * y
            for i in range(k):
                xi = x[i]
                xty[i] += xi * y
                row = xtx[i]
                for j in range(i, k):
                    row[j] += xi * x[j]
        for i in range(k):
            for j in range(i):
                xtx[i][j] = xtx[j][i]
        return xtx, xty, yty, n - start

    def fit(xtx, xty, yty, k):
        inv = _inverse([row[:k] for row in xtx[:k]])
        b = [math.fsum(inv[i][j] * xty[j] for j in range(k)) for i in range(k)]
        ssr = max(yty - math.fsum(b[i] * xty[i] for i in range(k)), 0.0)
        return b, inv, ssr

    if n < 20:
        raise PairError(f"ADF needs at least 20 observations (have {n})")
    if fixed:
        p = top
    else:
        xtx, xty, yty, nobs = cross(top, top + 1)
        best = None
        for p_try in range(top + 1):
            try:
                _, _, ssr = fit(xtx, xty, yty, p_try + 1)
            except ValueError:
                continue
            if ssr <= 0:
                continue
            aic = nobs * math.log(ssr / nobs) + 2 * (p_try + 1)
            if best is None or aic < best[0]:
                best = (aic, p_try)
        p = best[1] if best else 0
    xtx, xty, yty, nobs = cross(p, p + 1)
    k = p + 1
    try:
        b, inv, ssr = fit(xtx, xty, yty, k)
    except ValueError:
        return {"t_stat": None, "gamma": None, "lag": p, "lag_method": "fixed" if fixed else "AIC",
                "max_lag": top, "nobs": nobs, "note": "singular ADF regression (constant residual?)"}
    sigma2 = ssr / (nobs - k) if nobs > k else 0.0
    se = math.sqrt(sigma2 * inv[0][0]) if sigma2 > 0 and inv[0][0] > 0 else 0.0
    out = {"t_stat": b[0] / se if se > 0 else None, "gamma": b[0], "lag": p,
           "lag_method": "fixed" if fixed else "AIC", "max_lag": top, "nobs": nobs}
    if se == 0:
        out["note"] = "residual fitted exactly; t-statistic undefined"
    return out


def engle_granger(ly: list, lx: list, lag: int | None = None, max_lag: int | None = None) -> dict:
    """Engle-Granger two-step test: OLS ln y = alpha + beta ln x, then ADF on the residuals,
    judged against MacKinnon's N = 2 cointegration critical values with T = sessions - 1."""
    alpha, beta = ols(lx, ly)
    resid = [y - alpha - beta * x for x, y in zip(lx, ly)]
    a = adf(resid, lag, max_lag)
    T = len(ly) - 1
    crit = eg_critical(T)
    t = a["t_stat"]
    reject = next((lv for lv in LEVELS if t is not None and t < crit[lv]), None)
    return {"alpha": alpha, "beta": beta, "resid": resid, "adf": a, "T": T, "crit": crit,
            "p_value": eg_pvalue(t), "reject_at": reject}


def half_life(s: list) -> dict:
    """Speed of mean reversion from an AR(1) fit s_t = c + phi * s_(t-1) + e.

    This is the discrete Ornstein-Uhlenbeck model (ds_t = c + lambda * s_(t-1), lambda = phi - 1).
    A deviation decays like phi^k, so it halves after k = ln(0.5) / ln(phi) sessions.
    phi >= 1: no pull back to the mean in this sample. phi < 0: the spread flips sign bar to bar
    (noise); |phi| is used and flagged.
    """
    _, phi = ols(s[:-1], s[1:])
    out = {"phi": phi, "lambda": phi - 1}
    if phi >= 1:
        out.update(sessions=None, note="phi >= 1: no mean reversion in this sample")
    elif phi == 0:
        out.update(sessions=0.0)
    else:
        out["sessions"] = math.log(0.5) / math.log(abs(phi))
        if phi < 0:
            out["note"] = "phi < 0: spread alternates sign; reversion is mostly bar-to-bar noise"
    return out


def rolling_z(s: list, window: int) -> list:
    """z_t = (s_t - mean) / sd over the `window` bars ending at t (sample sd); None in warm-up."""
    out = [None] * len(s)
    for i in range(window - 1, len(s)):
        w = s[i - window + 1:i + 1]
        m = math.fsum(w) / window
        sd = math.sqrt(math.fsum((v - m) ** 2 for v in w) / (window - 1))
        out[i] = (s[i] - m) / sd if sd > 0 else None
    return out


def window_sd(s: list, window: int) -> float | None:
    if len(s) < window:
        return None
    w = s[-window:]
    m = math.fsum(w) / window
    return math.sqrt(math.fsum((v - m) ** 2 for v in w) / (window - 1))


def stability(dates: list, ly: list, lx: list, beta_full: float, splits: int) -> dict:
    """OLS hedge ratio in each of `splits` consecutive sub-periods. drift = (max - min) / |full beta|.
    Up to 25% = stable, up to 50% = drifting, beyond = unstable (the relationship is changing)."""
    n = len(ly)
    size = n // splits
    if size < 30:
        return {"label": "n/a", "periods": [], "drift_pct": None,
                "note": f"need {30 * splits}+ sessions for {splits} sub-periods"}
    periods = []
    for k in range(splits):
        lo, hi = k * size, (n if k == splits - 1 else (k + 1) * size)
        try:
            _, b = ols(lx[lo:hi], ly[lo:hi])
        except PairError:
            b = None
        periods.append({"from": dates[lo].isoformat(), "to": dates[hi - 1].isoformat(), "beta": b})
    betas = [p["beta"] for p in periods if p["beta"] is not None]
    if len(betas) < 2 or beta_full == 0:
        return {"label": "n/a", "periods": periods, "drift_pct": None}
    drift = (max(betas) - min(betas)) / abs(beta_full) * 100
    label = "stable" if drift <= 25 else "drifting" if drift <= 50 else "unstable"
    if any(b <= 0 for b in betas) and beta_full > 0:
        label = "unstable"
    return {"label": label, "periods": periods, "drift_pct": drift}


def zone(z: float | None, p: dict) -> str:
    if z is None:
        return "n/a"
    if z >= p["stop"] or z <= -p["stop"]:
        return "beyond the stop band: relationship may be breaking"
    if z >= p["entry"]:
        return "short-spread zone (A rich vs B)"
    if z <= -p["entry"]:
        return "long-spread zone (A cheap vs B)"
    if abs(z) <= max(abs(p["exit"]), 0.5):
        return "near the mean"
    return "inside the bands"


# ---------------------------------------------------------------------------
# Pair analysis
# ---------------------------------------------------------------------------
def load_pair(a: str, b: str, args) -> dict:
    if norm(a) == norm(b):
        raise PairError("A and B must be different symbols")
    ra, src_a = get_series(a, args.csv_dir, args.period)
    rb, src_b = get_series(b, args.csv_dir, args.period)
    dates, pa, pb = align(ra, rb, args.lookback, args.end_date)
    need = max(args.window + 20, 60)
    if len(dates) < need:
        raise PairError(f"{a}/{b}: only {len(dates)} common sessions (need {need}+ for window "
                        f"{args.window})")
    warnings = []
    stale = ta.freshness([{"date": datetime(dates[-1].year, dates[-1].month, dates[-1].day)}])
    if stale:
        warnings.append(stale.lstrip("⚠ "))
    return {"a": norm(a), "b": norm(b), "dates": dates, "pa": pa, "pb": pb,
            "source": {"a": src_a, "b": src_b}, "liquidity_cr": {"a": liquidity_cr(ra), "b": liquidity_cr(rb)},
            "warnings": warnings}


def params_of(args) -> dict:
    return {"window": args.window, "entry": args.entry, "exit": args.exit, "stop": args.stop,
            "lookback": args.lookback, "splits": args.splits, "max_half_life": args.max_half_life,
            "adf_lag": args.adf_lag, "max_lag": args.max_lag}


def direction_stats(dates, ly, lx, eg, p) -> dict:
    spread = eg["resid"]
    hl = half_life(spread)
    z = rolling_z(spread, p["window"])
    return {"half_life": hl, "z": z, "spread": spread,
            "stability": stability(dates, ly, lx, eg["beta"], p["splits"]),
            "spread_sd": window_sd(spread, p["window"])}


def analyse_pair(data: dict, p: dict) -> dict:
    dates, pa, pb = data["dates"], data["pa"], data["pb"]
    la, lb = [math.log(x) for x in pa], [math.log(x) for x in pb]
    fwd = engle_granger(la, lb, p["adf_lag"], p["max_lag"])
    rev = engle_granger(lb, la, p["adf_lag"], p["max_lag"])
    ds = direction_stats(dates, la, lb, fwd, p)
    corr = pearson(log_returns(pa), log_returns(pb))
    z_last = ds["z"][-1]
    hl = ds["half_life"]["sessions"]
    res = {
        "pair": {"a": data["a"], "b": data["b"]}, "source": data["source"],
        "sessions": len(dates), "from": dates[0].isoformat(), "to": dates[-1].isoformat(),
        "last": {"a": pa[-1], "b": pb[-1]}, "params": p,
        "correlation_returns": corr,
        "hedge": {"beta_ols": fwd["beta"], "alpha": fwd["alpha"],
                  "beta_reverse_ols": 1 / rev["beta"] if rev["beta"] else None,
                  "beta_tls": tls_slope(lb, la),
                  "meaning": f"Rs {fwd['beta']:.3f} of {data['b']} against every Rs 1 of {data['a']}"},
        "engle_granger": {"t_stat": fwd["adf"]["t_stat"], "lag": fwd["adf"]["lag"],
                          "lag_method": fwd["adf"]["lag_method"], "nobs": fwd["adf"]["nobs"], "T": fwd["T"],
                          "critical_values": fwd["crit"], "p_value": fwd["p_value"],
                          "reject_unit_root_at": fwd["reject_at"]},
        "engle_granger_reverse": {"t_stat": rev["adf"]["t_stat"], "p_value": rev["p_value"],
                                  "reject_unit_root_at": rev["reject_at"], "beta": rev["beta"]},
        "half_life": ds["half_life"],
        "spread": {"last": ds["spread"][-1], "sd_window": ds["spread_sd"], "z": z_last,
                   "window": p["window"], "zone": zone(z_last, p)},
        "stability": ds["stability"],
        "liquidity_cr": data["liquidity_cr"],
        "warnings": list(data["warnings"]),
    }
    if res["correlation_returns"] is None:
        res["warnings"].append("correlation undefined: one leg has constant returns")
    res["verdict"] = verdict(res, p)
    res["_z_series"], res["_spread"] = ds["z"], ds["spread"]
    return res


def verdict(r: dict, p: dict) -> str:
    eg, rev = r["engle_granger"], r["engle_granger_reverse"]
    lvl, hl = eg["reject_unit_root_at"], r["half_life"]["sessions"]
    stab = r["stability"]["label"]
    if lvl in ("1%", "5%"):
        if hl is None:
            v = f"Cointegrated at {lvl} but the AR(1) fit shows no mean reversion; treat as suspect"
        elif hl > p["max_half_life"]:
            v = (f"Cointegrated at {lvl} but slow (half-life {hl:.0f} sessions > {p['max_half_life']:g}): "
                 "capital is tied up across several futures rollovers")
        elif stab == "unstable":
            v = (f"Cointegrated at {lvl} on the full sample, but the hedge ratio is unstable across "
                 "sub-periods; the relationship may be shifting")
        else:
            v = f"Cointegrated at {lvl}, half-life {hl:.1f} sessions, hedge ratio {stab}: a candidate"
    elif lvl == "10%":
        v = "Weak evidence (10% only): not enough to trade mean reversion on its own"
    else:
        v = "Not cointegrated at 10%: the spread can drift indefinitely; no statistical edge"
    if (lvl in ("1%", "5%")) != (rev["reject_unit_root_at"] in ("1%", "5%")):
        v += ". The reverse regression disagrees, so the result is fragile"
    return v


# ---------------------------------------------------------------------------
# Signals (in-sample)
# ---------------------------------------------------------------------------
def simulate(dates: list, pa: list, pb: list, beta: float, z: list, entry: float, exit_: float,
             stop: float, max_hold: int | None) -> dict:
    """Walk the z-score bar by bar with these rules (fills at the signal bar's close):

    enter   short spread when z >= entry, long spread when z <= -entry, only after |z| has been
            back inside the entry band (no re-entry straight after an exit or stop) and only
            while |z| < stop (a jump straight past the stop also needs a reset first)
    exit    short: z <= exit; long: z >= -exit                        -> "target"
    stop    short: z >= stop; long: z <= -stop                         -> "stop"
            held max_hold bars without either                          -> "time"
    Return per trade = [(A_exit/A_entry - 1) - beta (B_exit/B_entry - 1)] x side / (1 + |beta|),
    i.e. % of gross notional with Rs 1 in A and Rs beta in B.
    """
    trades, pos, armed = [], None, False
    for i, zi in enumerate(z):
        if zi is None:
            continue
        if pos is not None:
            side = pos["side"]
            held = i - pos["i"]
            reason = None
            if (side < 0 and zi >= stop) or (side > 0 and zi <= -stop):
                reason = "stop"
            elif (side < 0 and zi <= exit_) or (side > 0 and zi >= -exit_):
                reason = "target"
            elif max_hold and held >= max_hold:
                reason = "time"
            if reason:
                trades.append(_close(pos, i, dates, pa, pb, beta, zi, reason))
                pos = None
                armed = abs(zi) < entry
            continue
        if abs(zi) < entry:
            armed = True
            continue
        if armed and abs(zi) < stop:
            pos = {"i": i, "side": -1 if zi > 0 else 1, "z": zi}
        armed = False                            # entered, or jumped past the stop: wait for a reset
    open_trade = None
    if pos is not None:
        last = len(z) - 1
        open_trade = _close(pos, last, dates, pa, pb, beta, z[last], "open")
    closed = trades
    wins = [t for t in closed if t["return_pct"] > 0]
    by = {}
    for t in closed:
        by[t["exit_reason"]] = by.get(t["exit_reason"], 0) + 1
    rets = [t["return_pct"] for t in closed]
    summary = {"closed_trades": len(closed), "wins": len(wins),
               "hit_rate_pct": len(wins) / len(closed) * 100 if closed else None,
               "avg_return_pct": _mean(rets) if rets else None,
               "total_return_pct": math.fsum(rets) if rets else None,
               "avg_bars_held": _mean(t["bars"] for t in closed) if closed else None,
               "worst_pct": min(rets) if rets else None, "best_pct": max(rets) if rets else None,
               "exits": by}
    if not closed:
        summary["note"] = "no completed trades in the sample; hit rate undefined"
    return {"trades": closed, "open_trade": open_trade, "summary": summary}


def _close(pos, i, dates, pa, pb, beta, zi, reason) -> dict:
    j, side = pos["i"], pos["side"]
    ra, rb = pa[i] / pa[j] - 1, pb[i] / pb[j] - 1
    ret = side * (ra - beta * rb) / (1 + abs(beta)) * 100
    return {"side": "long spread" if side > 0 else "short spread",
            "entry_date": dates[j].isoformat(), "exit_date": dates[i].isoformat(),
            "z_entry": pos["z"], "z_exit": zi, "bars": i - j, "exit_reason": reason, "return_pct": ret}


def run_signal(data: dict, p: dict, max_hold: int | None, mult: float) -> dict:
    res = analyse_pair(data, p)
    hl = res["half_life"]["sessions"]
    notes = []
    if max_hold is None:
        if hl is not None and hl > 0:
            max_hold = max(1, math.ceil(mult * hl))
        else:
            notes.append("half-life undefined, so no time stop (pass --max-hold)")
    sim = simulate(data["dates"], data["pa"], data["pb"], res["hedge"]["beta_ols"], res["_z_series"],
                   p["entry"], p["exit"], p["stop"], max_hold)
    z = res["spread"]["z"]
    if sim["open_trade"]:
        t = sim["open_trade"]
        state = (f"{t['side']} open since {t['entry_date']} (z {t['z_entry']:+.2f} -> {z:+.2f}, "
                 f"{t['bars']} bars, {t['return_pct']:+.2f}% so far)")
    elif z is not None and p["entry"] <= abs(z) < p["stop"]:
        state = ("signal: short the spread" if z > 0 else "signal: long the spread") + \
                f" (z {z:+.2f}); a new entry also needs |z| to have been inside {p['entry']:g} first"
    else:
        state = "flat: no signal"
    notes.append("IN-SAMPLE: the hedge ratio, cointegration test and half-life all use the same data "
                 "the trades are scored on (look-ahead). Treat the hit rate as an upper bound, not a "
                 "forecast; costs and slippage are excluded.")
    return {"pair": res["pair"], "sessions": res["sessions"], "from": res["from"], "to": res["to"],
            "beta": res["hedge"]["beta_ols"], "half_life_sessions": hl,
            "engle_granger": {k: res["engle_granger"][k] for k in ("t_stat", "p_value", "reject_unit_root_at")},
            "rules": {"entry": p["entry"], "exit": p["exit"], "stop": p["stop"], "window": p["window"],
                      "max_hold_bars": max_hold},
            "trades": sim["trades"], "open_trade": sim["open_trade"], "summary": sim["summary"],
            "current": {"date": res["to"], "z": z, "state": state},
            "notes": notes, "warnings": res["warnings"]}


# ---------------------------------------------------------------------------
# Screen
# ---------------------------------------------------------------------------
def load_members(path: str) -> dict:
    p = Path(path)
    if not p.is_file():
        raise PairError(f"--members {path}: file not found")
    with open(p, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        cols = {c.strip().lower(): c for c in reader.fieldnames or [] if c}
        cs = next((cols[c] for c in ("sector", "industry", "group", "index") if c in cols), None)
        cy = next((cols[c] for c in ("symbol", "ticker", "stock", "tradingsymbol") if c in cols), None)
        if not cs or not cy:
            raise PairError(f"--members needs 'symbol' and 'sector' columns; found {list(cols)}")
        out = {}
        for r in reader:
            sec, sym = (r.get(cs) or "").strip().upper(), norm(r.get(cy) or "")
            if sec and sym and sym not in out.setdefault(sec, []):
                out[sec].append(sym)
    if not out:
        raise PairError(f"--members {path}: no symbol/sector rows")
    return out


def run_screen(members: dict, args, p: dict) -> dict:
    loaded, filtered, warnings = {}, [], []
    no_volume = []
    for sym in dict.fromkeys(s for syms in members.values() for s in syms):
        try:
            rows, _ = get_series(sym, args.csv_dir, args.period)
            if args.end_date:
                rows = [r for r in rows if r[0] <= args.end_date]
        except PairError as e:
            if "yfinance not installed" in str(e) or "not a directory" in str(e):
                raise
            filtered.append({"symbol": sym, "reason": str(e).split(": ", 1)[-1]})
            continue
        if len(rows) < args.min_bars:
            filtered.append({"symbol": sym, "reason": f"{len(rows)} sessions < --min-bars {args.min_bars}"})
            continue
        liq = liquidity_cr(rows)
        if liq is None:
            no_volume.append(sym)
        elif liq < args.min_turnover:
            filtered.append({"symbol": sym, "reason": f"median traded value Rs {liq:.2f} cr/day < "
                                                      f"--min-turnover {args.min_turnover:g}"})
            continue
        loaded[sym] = (rows, liq)
    if no_volume:
        warnings.append(f"no volume column for {', '.join(no_volume)}: liquidity not checked")
    results, skipped_pairs = [], []
    for sector, syms in members.items():
        usable = sorted(s for s in syms if s in loaded)
        for a, b in itertools.combinations(usable, 2):
            dates, pa, pb = align(loaded[a][0], loaded[b][0], args.lookback, args.end_date)
            if len(dates) < args.min_bars:
                skipped_pairs.append({"pair": f"{a}/{b}", "reason": f"{len(dates)} common sessions"})
                continue
            try:
                results.append(screen_pair(sector, a, b, dates, pa, pb, p, loaded))
            except PairError as e:                # e.g. identical series: spread is constant
                skipped_pairs.append({"pair": f"{a}/{b}", "reason": str(e)})
    m = len(results)
    if not m:
        raise PairError("no pair had enough clean, liquid, overlapping history to test "
                        "(check --members, --csv-dir, --min-bars, --min-turnover)")
    bonf = args.alpha / m
    for r in results:
        r["passes_bonferroni"] = r["p_value"] is not None and r["p_value"] < bonf
        r["status"] = status(r, args, p)
    results.sort(key=lambda r: rank_key(r, args, p))
    for i, r in enumerate(results, 1):
        r["rank"] = i
    return {"pairs_tested": m, "sectors": {s: [x for x in v if x in loaded] for s, v in members.items()},
            "bonferroni_threshold": bonf, "alpha": args.alpha, "params": p,
            "filters": {"min_bars": args.min_bars, "min_turnover_cr": args.min_turnover,
                        "min_corr": args.min_corr, "min_half_life": args.min_half_life},
            "results": results, "filtered": filtered, "skipped_pairs": skipped_pairs, "warnings": warnings,
            "note": (f"{m} pairs tested: at a 5% level about {m * 0.05:.1f} would pass by chance alone. "
                     f"Bonferroni: require p < {bonf:.4g} for a family-wise {args.alpha:g} level.")}


def screen_pair(sector, a, b, dates, pa, pb, p, loaded) -> dict:
    la, lb = [math.log(x) for x in pa], [math.log(x) for x in pb]
    f = engle_granger(la, lb, p["adf_lag"], p["max_lag"])
    r = engle_granger(lb, la, p["adf_lag"], p["max_lag"])
    tf, tr = f["adf"]["t_stat"], r["adf"]["t_stat"]
    swap = tr is not None and (tf is None or tr < tf)
    y, x, eg, ly, lx = (b, a, r, lb, la) if swap else (a, b, f, la, lb)
    ds = direction_stats(dates, ly, lx, eg, p)
    pvals = [v for v in (f["p_value"], r["p_value"]) if v is not None]
    return {"sector": sector, "a": y, "b": x, "sessions": len(dates),
            "p_value": max(pvals) if len(pvals) == 2 else None,
            "p_value_best_direction": eg["p_value"], "t_stat": eg["adf"]["t_stat"],
            "reject_at": eg["reject_at"], "reverse_reject_at": (f if swap else r)["reject_at"],
            "beta": eg["beta"], "half_life": ds["half_life"]["sessions"],
            "correlation": pearson(log_returns(pa), log_returns(pb)),
            "beta_drift_pct": ds["stability"]["drift_pct"], "stability": ds["stability"]["label"],
            "z": ds["z"][-1], "liquidity_cr": {y: loaded[y][1], x: loaded[x][1]}}


def _hl_ok(r, args, p) -> bool:
    hl = r["half_life"]
    return hl is not None and args.min_half_life <= hl <= p["max_half_life"]


def status(r, args, p) -> str:
    pv = r["p_value"]
    if pv is None or r["beta"] <= 0:              # negative beta: both legs same side, not a pair
        return "reject"
    if pv < 0.05 and _hl_ok(r, args, p) and (r["correlation"] or 0) >= args.min_corr \
            and r["stability"] != "unstable":
        return "candidate"
    return "watch" if pv < 0.10 else "reject"


def rank_key(r, args, p) -> tuple:
    """Significance bucket of the conservative p (1% / 5% / 10% / rest; non-positive beta last),
    then usable half-life first and shorter first, then higher return correlation."""
    pv = r["p_value"]
    bucket = 4 if pv is None or r["beta"] <= 0 else 0 if pv < 0.01 else 1 if pv < 0.05 else \
        2 if pv < 0.10 else 3
    hl = r["half_life"]
    return (bucket, 0 if _hl_ok(r, args, p) else 1, hl if hl is not None else 1e9,
            -(r["correlation"] if r["correlation"] is not None else -1))


# ---------------------------------------------------------------------------
# India execution plan
# ---------------------------------------------------------------------------
def load_lots(path: str) -> dict:
    """Lot sizes from a simple symbol,lot_size CSV or NSE's fo_mktlots.csv (one column per
    contract month). Returns {SYMBOL: {"lot": nearest-month lot, "months": [(label, lot), ...]}}."""
    p = Path(path)
    if not p.is_file():
        raise PairError(f"--lots-csv {path}: file not found")
    with open(p, newline="", encoding="utf-8-sig") as fh:
        rows = [[c.strip() for c in r] for r in csv.reader(fh)]
    header = next((r for r in rows if any(c.upper() == "SYMBOL" for c in r)), None)
    if header is None:
        raise PairError(f"--lots-csv {path}: needs a SYMBOL column")
    hu = [c.upper() for c in header]
    si = hu.index("SYMBOL")
    single = next((i for i, c in enumerate(hu) if c in ("LOT", "LOT_SIZE", "LOTSIZE", "LOT SIZE",
                                                        "MARKET LOT", "MARKET_LOT")), None)
    cols = [single] if single is not None else [i for i in range(si + 1, len(hu)) if hu[i]]
    out = {}
    for r in rows:
        if len(r) <= si or r[si].upper() in ("SYMBOL", ""):
            continue
        months = []
        for i in cols:
            if i < len(r) and re.fullmatch(r"\d+(\.0+)?", r[i] or ""):
                months.append((header[i], int(float(r[i]))))
        if months:
            out[norm(r[si])] = {"lot": months[0][1], "months": months}
    if not out:
        raise PairError(f"--lots-csv {path}: no symbol rows with a numeric lot size")
    return out


def load_ban_list(path: str | None, inline: str | None) -> set | None:
    if not path and not inline:
        return None
    out = set()
    if path:
        p = Path(path)
        if not p.is_file():
            raise PairError(f"--ban-list {path}: file not found")
        for row in csv.reader(p.read_text(encoding="utf-8-sig").splitlines()):
            for cell in row:
                tok = cell.strip().upper()
                if re.fullmatch(r"[A-Z][A-Z0-9&-]*", tok) and len(row) > 1:
                    out.add(norm(tok))
    if inline:
        out |= {norm(s) for s in inline.split(",") if s.strip()}
    return out


def add_sessions(d: date, n: int) -> date:
    while n > 0:
        d += timedelta(days=1)
        if options.is_trading_day(d):
            n -= 1
    return d


def sub_sessions(d: date, n: int) -> date:
    while n > 0:
        d -= timedelta(days=1)
        if options.is_trading_day(d):
            n -= 1
    return d


def sessions_between(a: date, b: date) -> int:
    n, d = 0, a
    while d < b:
        d += timedelta(days=1)
        n += options.is_trading_day(d)
    return n


def lot_combos(beta, pa, pb, lot_a, lot_b, max_lots) -> list:
    out = []
    for na in range(1, max_lots + 1):
        va = na * lot_a * pa
        nb = max(1, round(beta * va / (lot_b * pb)))
        vb = nb * lot_b * pb
        out.append({"lots_a": na, "lots_b": nb, "notional_a": va, "notional_b": vb,
                    "mismatch_pct": (vb / (beta * va) - 1) * 100})
    return out


def size_legs(beta, pa, pb, lot_a, lot_b, lots_a, mode, side) -> dict:
    """Quantities for each leg. futures: both legs in whole lots, B rounded to the nearest lot.
    cash-long: the long leg is bought for delivery (any share count) to match the futures leg."""
    if mode == "futures":
        c = lot_combos(beta, pa, pb, lot_a, lot_b, lots_a)[-1]
        qa, qb = c["lots_a"] * lot_a, c["lots_b"] * lot_b
        la_, lb_ = c["lots_a"], c["lots_b"]
    elif side < 0:                               # short spread: A short in futures, B long in cash
        la_, qa = lots_a, lots_a * lot_a
        qb = max(1, round(beta * qa * pa / pb))
        lb_ = None
    else:                                        # long spread: B short in futures, A long in cash
        lb_ = max(1, round(beta * lots_a * lot_a * pa / (lot_b * pb)))
        qb = lb_ * lot_b
        qa = max(1, round(qb * pb / (beta * pa)))
        la_ = None
    va, vb = qa * pa, qb * pb
    return {"qty_a": qa, "qty_b": qb, "lots_a": la_, "lots_b": lb_, "notional_a": va, "notional_b": vb,
            "effective_ratio": vb / va, "mismatch_pct": (vb / (beta * va) - 1) * 100,
            "unhedged_b_rs": vb - beta * va}


def run_plan(args, p: dict) -> dict:
    a, b = norm(args.a), norm(args.b)
    warnings, assumptions = [], []
    overrides = args.beta is not None and args.price_a is not None and args.price_b is not None
    if overrides:
        beta, pa, pb = args.beta, args.price_a, args.price_b
        z, hl, sd, source, last = args.z, args.half_life, None, "user inputs", None
        eg = None
    else:
        data = load_pair(a, b, args)
        res = analyse_pair(data, p)
        warnings += res["warnings"]
        beta = args.beta if args.beta is not None else res["hedge"]["beta_ols"]
        pa = args.price_a or res["last"]["a"]
        pb = args.price_b or res["last"]["b"]
        z = args.z if args.z is not None else res["spread"]["z"]
        hl = args.half_life if args.half_life is not None else res["half_life"]["sessions"]
        sd = res["spread"]["sd_window"]
        source, last = res["source"], res["to"]
        eg = res["engle_granger"]
        if eg["reject_unit_root_at"] not in ("1%", "5%"):
            warnings.append(f"{res['verdict']}. Planning anyway, but there is no statistical case")
    if beta is None or beta <= 0:
        raise PairError("hedge ratio must be positive for a long/short pair (got "
                        f"{beta}); a negative beta means both legs move opposite ways")
    if pa <= 0 or pb <= 0:
        raise PairError("prices must be positive")

    if args.side == "auto":
        if z is None:
            raise PairError("z-score unknown: pass --side long|short (or --z)")
        side = -1 if z > 0 else 1
        if abs(z) < p["entry"]:
            warnings.append(f"|z| = {abs(z):.2f} is inside the entry band ({p['entry']:g}): no signal today; "
                            "sizes shown for the side the spread is leaning to")
    else:
        side = 1 if args.side == "long" else -1
    mode = args.legs
    lots = load_lots(args.lots_csv) if args.lots_csv else {}

    def lot_for(sym, given, needed):
        if given:
            return given
        if sym in lots:
            return lots[sym]["lot"]
        if needed:
            raise PairError(f"no lot size for {sym}: pass --lot-{'a' if sym == a else 'b'} or a --lots-csv "
                            "that lists it (only F&O stocks have futures)")
        return None

    fut_a = mode == "futures" or side < 0       # A is in futures unless it is the cash long leg
    fut_b = mode == "futures" or side > 0
    lot_a = lot_for(a, args.lot_a, fut_a or mode == "futures")
    lot_b = lot_for(b, args.lot_b, fut_b)
    if lot_a is None:                            # cash-long with A in cash: size by value only
        lot_a = 1
    legs = size_legs(beta, pa, pb, lot_a, lot_b, args.lots_a, mode, side)

    def leg(sym, qty, lots_, lot, price, buy, fut):
        return {"symbol": sym, "action": "BUY" if buy else "SELL",
                "instrument": "stock futures (NRML)" if fut else "cash (CNC delivery)",
                "lots": lots_ if fut else None, "lot_size": lot if fut else None, "qty": qty,
                "price": price, "notional": qty * price}
    leg_a = leg(a, legs["qty_a"], legs["lots_a"], lot_a, pa, side > 0, fut_a)
    leg_b = leg(b, legs["qty_b"], legs["lots_b"], lot_b, pb, side < 0, fut_b)

    # Margin
    margin = {}
    for L, per_lot in ((leg_a, args.margin_a), (leg_b, args.margin_b)):
        if L["instrument"].startswith("cash"):
            margin[L["symbol"]] = L["notional"]
        elif per_lot:
            margin[L["symbol"]] = per_lot * L["lots"]
        else:
            margin[L["symbol"]] = L["notional"] * args.margin_pct / 100
    if not (args.margin_a and args.margin_b):
        assumptions.append(f"futures margin assumed {args.margin_pct:g}% of contract value per leg where "
                           "--margin-a/--margin-b not given; take SPAN + exposure from your broker's "
                           "margin calculator")
    margin_note = ("Summed leg by leg. A long/short in two DIFFERENT stocks is not a recognised spread, "
                   "so a hedged-position margin benefit is not guaranteed; some brokers' basket "
                   "margin may show less.")

    # Charges
    if args.broker not in charges.BROKERS:
        raise PairError(f"unknown --broker {args.broker}; known: {', '.join(charges.BROKERS)}")

    def rt(L):
        seg = "futures" if L["instrument"].startswith("stock futures") else "delivery"
        return charges.calculate(seg, L["price"], L["price"], L["qty"], broker=args.broker)["total_charges"]
    rt_a, rt_b = rt(leg_a), rt(leg_b)
    per_roll = (rt_a if leg_a["lots"] else 0.0) + (rt_b if leg_b["lots"] else 0.0)

    # Rollover calendar
    as_of = date.fromisoformat(args.as_of) if args.as_of else datetime.now(options.IST).date()
    if hl is not None and hl > 0:
        hold = args.hold or max(1, math.ceil(2 * hl))
    else:
        hold = args.hold or 20
        if not args.hold:
            assumptions.append("half-life unknown: holding period assumed 20 sessions (pass --hold)")
    exps = options.expiries("STOCK", start=as_of, count=8)
    first = date.fromisoformat(exps[0]["date"])
    left = sessions_between(as_of, first)
    idx = 1 if left <= args.roll_days else 0
    horizon = add_sessions(as_of, hold)
    rolls = []
    for e in exps[idx:]:
        ed = date.fromisoformat(e["date"])
        rd = sub_sessions(ed, args.roll_days)
        if rd >= horizon:
            break
        rolls.append({"expiry": e["date"], "roll_by": rd.isoformat(),
                      "shifted_for_holiday": e["shifted_for_holiday"]})
    contract = exps[idx]
    n_rolls = len(rolls)
    if any(e.get("holiday_list_missing") for e in exps[idx:idx + n_rolls + 1]):
        warnings.append("expiry dates past the known NSE holiday list may move; re-check the calendar")
    v_long = leg_a["notional"] if side > 0 else leg_b["notional"]
    v_short = leg_b["notional"] if side > 0 else leg_a["notional"]
    long_fut = (leg_a if side > 0 else leg_b)["lots"] is not None
    # Futures trade at a premium (cost of carry) that decays to spot by expiry: the long futures leg
    # pays it, the short futures leg earns it. Net cost over the hold, positive = cost.
    carry = args.carry_pct / 100 * ((v_long if long_fut else 0.0) - v_short) * hold / 21
    rollover = {"as_of": as_of.isoformat(), "contract_expiry": contract["date"],
                "sessions_to_first_expiry": left, "hold_sessions": hold, "horizon": horizon.isoformat(),
                "rolls": rolls, "roll_charges_each": per_roll,
                "net_carry_rs": carry,
                "note": ("NSE stock futures expire on the last Tuesday of the month (previous trading day "
                         "if a holiday) and are physically settled: close or roll before expiry.")}
    if idx == 1:
        rollover["note"] += f" Only {left} sessions left in the near month, so the plan uses the next one."
    assumptions.append(f"roll {args.roll_days} sessions before expiry; each roll = one extra round trip per "
                       f"futures leg; futures basis (cost of carry) {args.carry_pct:g}% a month, paid on the "
                       "long futures notional and earned on the short")

    # Reward / risk in rupees (P&L ~= notional of A x change in spread)
    expected = None
    if sd and z is not None:
        z_from = max(abs(z), p["entry"])
        reward = (z_from - p["exit"]) * sd * leg_a["notional"]
        risk = (p["stop"] - z_from) * sd * leg_a["notional"]
        costs = rt_a + rt_b + n_rolls * per_roll + carry
        expected = {"from_z": z_from, "spread_sd": sd, "reward_to_exit_rs": reward, "risk_to_stop_rs": risk,
                    "total_costs_rs": costs,
                    "costs_pct_of_reward": costs / reward * 100 if reward > 0 else None}
    else:
        expected = {"note": "spread volatility unknown (user-supplied inputs): reward/risk not estimated"}

    # Ban, MWPL, lot revisions
    ban = load_ban_list(args.ban_list, args.banned)
    risk = {}
    for sym, mw, fut in ((a, args.mwpl_a, leg_a["lots"] is not None), (b, args.mwpl_b, leg_b["lots"] is not None)):
        if not fut:
            risk[sym] = "cash leg: F&O ban does not restrict delivery buying"
            continue
        if ban is not None and sym in ban:
            risk[sym] = "IN F&O BAN: no fresh positions until OI falls below 80% of MWPL; only reduce"
        elif mw is not None and mw >= 95:
            risk[sym] = f"MWPL {mw:g}% (ban threshold 95%): effectively in ban"
        elif mw is not None and mw >= 80:
            risk[sym] = f"MWPL {mw:g}%: close to the 95% ban trigger; a ban would block rolling"
        elif ban is None and mw is None:
            risk[sym] = "unknown: check NSE's daily F&O ban list and MWPL utilisation"
        else:
            risk[sym] = "not in the supplied ban list" + (f" (MWPL {mw:g}%)" if mw is not None else "")
    if any(v.startswith(("IN F&O BAN", "MWPL 9")) for v in risk.values()):
        warnings.append("a leg is in (or at) the F&O ban: you cannot open this pair in futures now")
    revisions = []
    for sym, fut in ((a, leg_a["lots"] is not None), (b, leg_b["lots"] is not None)):
        if fut and sym in lots and len({v for _, v in lots[sym]["months"]}) > 1:
            m = lots[sym]["months"]
            revisions.append(f"{sym}: lot size {' / '.join(f'{lbl} {v}' for lbl, v in m)}; re-size the "
                             "hedge when you roll into the new lot size")
    if not revisions:
        revisions.append("no change seen in the supplied lot data. NSE reviews stock F&O lot sizes "
                         "periodically and applies new sizes from a future contract month, so a roll can "
                         "change the hedge; re-run plan after each revision")

    mm = {"target_ratio": beta, "effective_ratio": legs["effective_ratio"],
          "mismatch_pct": legs["mismatch_pct"], "unhedged_b_rs": legs["unhedged_b_rs"]}
    if abs(legs["mismatch_pct"]) > 10:
        warnings.append(f"whole-lot rounding leaves a {legs['mismatch_pct']:+.1f}% hedge mismatch: see the lot "
                        "combinations, or use --legs cash-long so the long leg can be sized in shares")
    combos = lot_combos(beta, pa, pb, lot_a, lot_b, args.max_lots) if mode == "futures" else []
    return {"pair": {"a": a, "b": b}, "source": source, "data_to": last, "as_of": as_of.isoformat(),
            "side": "long spread" if side > 0 else "short spread", "z": z, "half_life_sessions": hl,
            "beta": beta, "engle_granger": eg, "legs_mode": mode, "legs": [leg_a, leg_b], "hedge": mm,
            "lot_combos": combos,
            "margin": {"by_leg": margin, "total": sum(margin.values()), "note": margin_note},
            "charges": {"broker": args.broker, "rates_as_of": charges.RATES_AS_OF,
                        "round_trip": {a: rt_a, b: rt_b}, "per_roll": per_roll, "rolls": n_rolls,
                        "total": rt_a + rt_b + n_rolls * per_roll},
            "rollover": rollover, "expected": expected,
            "levels": {"entry_z": p["entry"], "exit_z": p["exit"], "stop_z": p["stop"],
                       "time_stop_sessions": hold},
            "fo_ban": risk, "lot_revision": revisions, "assumptions": assumptions, "warnings": warnings,
            "india_notes": [
                "A cash-market short must be bought back the same day (MIS); an undelivered short goes "
                "to NSE's auction with a penalty, so a multi-day pair holds its short leg in futures.",
                "Futures P&L is settled daily (MTM): keep a buffer above the initial margin for both legs."]}


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def _safe(o):
    """JSON-ready copy: dates to ISO strings, NaN/Infinity to null, private keys dropped."""
    if isinstance(o, dict):
        return {k: _safe(v) for k, v in o.items() if not str(k).startswith("_")}
    if isinstance(o, (list, tuple)):
        return [_safe(v) for v in o]
    if isinstance(o, float) and not math.isfinite(o):
        return None
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    return o


def emit_json(res) -> None:
    print(json.dumps(_safe(res), indent=2, allow_nan=False))


def f(x, d=2, sign=False) -> str:
    if x is None:
        return "—"
    return f"{x:+.{d}f}" if sign else f"{x:.{d}f}"


def rs(x) -> str:
    return "—" if x is None else charges.inr(x, 0)


def print_test(r: dict) -> None:
    a, b = r["pair"]["a"], r["pair"]["b"]
    eg, rev, h, sp = r["engle_granger"], r["engle_granger_reverse"], r["hedge"], r["spread"]
    print(f"Pair {a} / {b}: {r['sessions']} common sessions {r['from']} → {r['to']} "
          f"({r['source']['a']}, {r['source']['b']})")
    for w in r["warnings"]:
        print(f"⚠ {w}")
    print(f"Return correlation (daily log): {f(r['correlation_returns'])}")
    print(f"Hedge ratio β (OLS ln {a} on ln {b}): {f(h['beta_ols'], 3)}  [reverse OLS {f(h['beta_reverse_ols'], 3)}, "
          f"TLS {f(h['beta_tls'], 3)}] → {h['meaning']}")
    cv = eg["critical_values"]
    lvl = eg["reject_unit_root_at"]
    print(f"Engle–Granger ADF on residuals: t = {f(eg['t_stat'])} (lag {eg['lag']}, {eg['lag_method']}, "
          f"T={eg['T']}); MacKinnon N=2 critical 1% {cv['1%']:.2f} / 5% {cv['5%']:.2f} / 10% {cv['10%']:.2f} "
          f"→ {'unit root rejected at ' + lvl if lvl else 'unit root NOT rejected'} (p≈{f(eg['p_value'], 4)})")
    print(f"Reverse direction ({b} on {a}): t = {f(rev['t_stat'])}, p≈{f(rev['p_value'], 4)}, "
          f"{'rejects at ' + rev['reject_unit_root_at'] if rev['reject_unit_root_at'] else 'does not reject'}")
    hl = r["half_life"]
    hl_txt = f"{hl['sessions']:.1f} sessions" if hl["sessions"] is not None else "undefined"
    print(f"Half-life: {hl_txt} (AR(1) φ = {f(hl['phi'], 3)})" + (f"; {hl['note']}" if hl.get("note") else ""))
    print(f"Spread now: z = {f(sp['z'], 2, True)} (window {sp['window']}) → {sp['zone']}")
    st = r["stability"]
    if st["periods"]:
        bs = " / ".join(f(x["beta"], 3) for x in st["periods"])
        print(f"Hedge-ratio stability ({len(st['periods'])} sub-periods): {bs} → drift {f(st['drift_pct'], 0)}% "
              f"of β, {st['label']}")
    else:
        print(f"Hedge-ratio stability: n/a ({st.get('note', '')})")
    lq = r["liquidity_cr"]
    print(f"Liquidity (median traded value, last {LIQ_SESSIONS}): {a} {f(lq['a'], 1)} cr, {b} {f(lq['b'], 1)} cr")
    print(f"\nVerdict: {r['verdict']}.")
    print("Critical values: MacKinnon (2010) Table 2, N=2 with constant; p-value: MacKinnon (1994) "
          "asymptotic surface.")


def print_signal(r: dict) -> None:
    a, b = r["pair"]["a"], r["pair"]["b"]
    ru, s = r["rules"], r["summary"]
    print(f"Signals {a} / {b}: {r['sessions']} sessions {r['from']} → {r['to']}, β {f(r['beta'], 3)}, "
          f"half-life {f(r['half_life_sessions'], 1)}")
    for w in r["warnings"]:
        print(f"⚠ {w}")
    print(f"Rules: enter |z| ≥ {ru['entry']:g}, exit when z is back to ±{ru['exit']:g} (mean side), "
          f"stop |z| ≥ {ru['stop']:g}, "
          f"time stop {ru['max_hold_bars'] or 'none'} bars, z window {ru['window']}")
    if r["trades"]:
        print(f"{'Entry':<11} {'Exit':<11} {'Side':<13} {'z in':>6} {'z out':>6} {'Bars':>4} {'Why':<7} {'Ret%':>7}")
        for t in r["trades"]:
            print(f"{t['entry_date']:<11} {t['exit_date']:<11} {t['side']:<13} {t['z_entry']:>+6.2f} "
                  f"{t['z_exit']:>+6.2f} {t['bars']:>4} {t['exit_reason']:<7} {t['return_pct']:>+7.2f}")
    hr = s["hit_rate_pct"]
    print(f"Closed trades {s['closed_trades']}, wins {s['wins']}, hit rate {f(hr, 0)}%, avg {f(s['avg_return_pct'], 2, True)}% "
          f"of gross, total {f(s['total_return_pct'], 2, True)}%, avg hold {f(s['avg_bars_held'], 1)} bars; "
          f"exits {s['exits'] or '—'}")
    print(f"Now ({r['current']['date']}): {r['current']['state']}")
    for n in r["notes"]:
        print(f"Note: {n}")


def print_screen(r: dict, top: int) -> None:
    print(f"Pair screen: {r['pairs_tested']} same-sector pairs tested "
          f"(look-back {r['params']['lookback'] or 'all'} sessions, z window {r['params']['window']})")
    for w in r["warnings"]:
        print(f"⚠ {w}")
    print(f"{'#':>2} {'Sector':<10} {'Pair (A/B)':<20} {'p(EG)':>7} {'t':>6} {'HL':>5} {'corr':>5} {'β':>6} "
          f"{'drift':>6} {'z':>6}  Status")
    for x in r["results"][:top]:
        tag = " ✓Bonferroni" if x["passes_bonferroni"] else ""
        print(f"{x['rank']:>2} {x['sector'][:10]:<10} {(x['a'] + '/' + x['b'])[:20]:<20} {f(x['p_value'], 4):>7} "
              f"{f(x['t_stat']):>6} {f(x['half_life'], 1):>5} {f(x['correlation']):>5} {f(x['beta'], 2):>6} "
              f"{f(x['beta_drift_pct'], 0):>5}% {f(x['z'], 2, True):>6}  {x['status']}{tag}")
    if len(r["results"]) > top:
        print(f"… {len(r['results']) - top} more (raise --top or use --json)")
    for x in r["filtered"]:
        print(f"Filtered {x['symbol']}: {x['reason']}")
    for x in r["skipped_pairs"]:
        print(f"Skipped {x['pair']}: {x['reason']}")
    print(f"\n{r['note']}")
    print("p(EG) = the larger of the two regression directions (both must reject); A/B is the direction "
          "with the stronger test. Confirm each candidate with `test` and out-of-sample data.")


def print_plan(r: dict) -> None:
    a, b = r["pair"]["a"], r["pair"]["b"]
    zt = f"z {f(r['z'], 2, True)}" if r["z"] is not None else "z n/a"
    print(f"Plan {a} / {b}: {r['side']} ({zt}, β {f(r['beta'], 3)}, half-life {f(r['half_life_sessions'], 1)}), "
          f"legs: {r['legs_mode']}")
    for w in r["warnings"]:
        print(f"⚠ {w}")
    for L in r["legs"]:
        lots = f"{L['lots']} lot{'s' if L['lots'] > 1 else ''} × {L['lot_size']} = " if L["lots"] else ""
        print(f"  {L['action']:<4} {L['symbol']:<12} {L['instrument']:<21} {lots}{L['qty']:g} @ {charges.inr(L['price'])}"
              f"  notional {rs(L['notional'])}")
    h = r["hedge"]
    print(f"Hedge: target {f(h['target_ratio'], 3)}, achieved {f(h['effective_ratio'], 3)} → mismatch "
          f"{f(h['mismatch_pct'], 1, True)}% ({b} leg {'over' if h['unhedged_b_rs'] > 0 else 'under'} by "
          f"{rs(abs(h['unhedged_b_rs']))})")
    if r["lot_combos"]:
        best = min(r["lot_combos"], key=lambda c: (abs(c["mismatch_pct"]), c["lots_a"]))
        combos = ", ".join(f"{c['lots_a']}:{c['lots_b']} ({c['mismatch_pct']:+.1f}%)" for c in r["lot_combos"])
        print(f"Lot combos {a}:{b} → {combos}; tightest {best['lots_a']}:{best['lots_b']}")
    m = r["margin"]
    print(f"Margin ≈ {rs(m['total'])} (" + ", ".join(f"{k} {rs(v)}" for k, v in m["by_leg"].items()) + ")")
    c = r["charges"]
    print(f"Charges ({c['broker']}, rates {c['rates_as_of']}): round trip " +
          ", ".join(f"{k} {charges.inr(v)}" for k, v in c["round_trip"].items()) +
          f"; per roll {charges.inr(c['per_roll'])} × {c['rolls']} → total {charges.inr(c['total'])}; "
          f"net futures carry over the hold ≈ {charges.inr(r['rollover']['net_carry_rs'])}")
    ro = r["rollover"]
    rolls = "; ".join(f"roll by {x['roll_by']} (expiry {x['expiry']}{', holiday-shifted' if x['shifted_for_holiday'] else ''})"
                      for x in ro["rolls"]) or "none expected"
    print(f"Contract: expiry {ro['contract_expiry']}; hold ≈ {ro['hold_sessions']} sessions to {ro['horizon']}; {rolls}")
    print(f"  {ro['note']}")
    e = r["expected"]
    if "reward_to_exit_rs" in e:
        print(f"From z {e['from_z']:.2f}: reward to exit ≈ {rs(e['reward_to_exit_rs'])}, risk to stop ≈ "
              f"{rs(e['risk_to_stop_rs'])}, costs {rs(e['total_costs_rs'])} ({f(e['costs_pct_of_reward'], 1)}% of reward)")
    else:
        print(e["note"])
    for sym, s in r["fo_ban"].items():
        print(f"F&O ban {sym}: {s}")
    for x in r["lot_revision"]:
        print(f"Lot size: {x}")
    for x in r["assumptions"]:
        print(f"Assumed: {x}")
    print(f"Margin note: {m['note']}")
    for x in r["india_notes"]:
        print(f"India: {x}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise PairError(message)


def _common() -> argparse.ArgumentParser:
    c = _Parser(add_help=False)
    c.add_argument("--csv-dir", help="folder with one CSV per stock (default: fetch via yfinance)")
    c.add_argument("--period", default="3y", help="yfinance history period (default 3y)")
    c.add_argument("--lookback", type=int, default=500, help="last N common sessions to use (0 = all; default 500)")
    c.add_argument("--end", help="use data up to this date YYYY-MM-DD (re-test an earlier window)")
    c.add_argument("--window", type=int, default=60, help="rolling z-score window in sessions (default 60)")
    c.add_argument("--entry", type=float, default=2.0, help="entry |z| (default 2)")
    c.add_argument("--exit", type=float, default=0.0, help="exit z level, e.g. 0 or 0.5 (default 0)")
    c.add_argument("--stop", type=float, default=3.5, help="stop |z| (default 3.5)")
    c.add_argument("--adf-lag", type=int, help="fixed ADF lag (default: AIC selection)")
    c.add_argument("--max-lag", type=int, help="largest lag AIC may pick (default Schwert: 12*(n/100)^0.25)")
    c.add_argument("--splits", type=int, default=3, help="sub-periods for hedge-ratio stability (default 3)")
    c.add_argument("--max-half-life", type=float, default=40.0, help="slowest usable half-life, sessions (default 40)")
    c.add_argument("--json", action="store_true", help="print JSON")
    return c


def build_parser() -> argparse.ArgumentParser:
    p = _Parser(prog="pairs.py", description="Pair trading for NSE stocks: cointegration, signals, India execution")
    sub = p.add_subparsers(dest="cmd", required=True, parser_class=_Parser)
    common = _common()
    t = sub.add_parser("test", parents=[common], help="full statistics for one pair")
    t.add_argument("a")
    t.add_argument("b")
    s = sub.add_parser("screen", parents=[common], help="rank all same-sector pairs")
    s.add_argument("--members", required=True, help="CSV with symbol and sector columns")
    s.add_argument("--min-bars", type=int, default=250, help="minimum sessions per stock and pair (default 250)")
    s.add_argument("--min-turnover", type=float, default=10.0,
                   help="minimum median daily traded value, Rs crore (default 10)")
    s.add_argument("--min-corr", type=float, default=0.5, help="minimum return correlation for 'candidate' (0.5)")
    s.add_argument("--min-half-life", type=float, default=1.0, help="fastest usable half-life (default 1)")
    s.add_argument("--alpha", type=float, default=0.05, help="family-wise level for the Bonferroni note")
    s.add_argument("--top", type=int, default=20, help="rows to print (default 20)")
    g = sub.add_parser("signal", parents=[common], help="z-score rules and in-sample trade list")
    g.add_argument("a")
    g.add_argument("b")
    g.add_argument("--max-hold", type=int, help="time stop in sessions (default: 2 x half-life)")
    g.add_argument("--time-stop-mult", type=float, default=2.0, help="time stop as a multiple of half-life (2)")
    n = sub.add_parser("plan", parents=[common], help="India execution plan in stock futures")
    n.add_argument("a")
    n.add_argument("b")
    n.add_argument("--lot-a", type=int, help="futures lot size of A")
    n.add_argument("--lot-b", type=int, help="futures lot size of B")
    n.add_argument("--lots-csv", help="lot sizes: symbol,lot_size CSV or NSE fo_mktlots.csv")
    n.add_argument("--lots-a", type=int, default=1, help="lots of A that set the trade size (default 1)")
    n.add_argument("--max-lots", type=int, default=5, help="lot combinations to list (default up to 5 of A)")
    n.add_argument("--legs", choices=["futures", "cash-long"], default="futures",
                   help="futures = both legs in stock futures; cash-long = long leg bought for delivery")
    n.add_argument("--side", choices=["auto", "long", "short"], default="auto",
                   help="long = long A/short B; auto follows the sign of z")
    n.add_argument("--margin-pct", type=float, default=25.0, help="futures margin as %% of contract value (25)")
    n.add_argument("--margin-a", type=float, help="Rs margin per lot of A (from your broker)")
    n.add_argument("--margin-b", type=float, help="Rs margin per lot of B (from your broker)")
    n.add_argument("--broker", default="zerodha", help=f"charges profile: {', '.join(charges.BROKERS)}")
    n.add_argument("--as-of", help="planning date YYYY-MM-DD for the expiry calendar (default today, IST)")
    n.add_argument("--hold", type=int, help="expected holding period, sessions (default 2 x half-life)")
    n.add_argument("--roll-days", type=int, default=3, help="roll this many sessions before expiry (3)")
    n.add_argument("--carry-pct", type=float, default=0.5, help="futures basis per month, %% (0.5)")
    n.add_argument("--banned", help="comma list of symbols in today's F&O ban")
    n.add_argument("--ban-list", help="NSE fo_secban.csv (or any CSV listing banned symbols)")
    n.add_argument("--mwpl-a", type=float, help="A's open interest as %% of MWPL")
    n.add_argument("--mwpl-b", type=float, help="B's open interest as %% of MWPL")
    n.add_argument("--beta", type=float, help="override the hedge ratio")
    n.add_argument("--price-a", type=float, help="override A's price (futures price to trade)")
    n.add_argument("--price-b", type=float, help="override B's price")
    n.add_argument("--z", type=float, help="override the current z-score")
    n.add_argument("--half-life", type=float, help="override the half-life, sessions")
    return p


def validate(a) -> None:
    a.end_date = None
    if a.end:
        try:
            a.end_date = date.fromisoformat(a.end)
        except ValueError:
            raise PairError(f"--end {a.end!r}: use YYYY-MM-DD")
    if a.window < 10:
        raise PairError("--window must be at least 10")
    if a.lookback and a.lookback < max(60, a.window + 20):
        raise PairError(f"--lookback must be 0 (all) or at least {max(60, a.window + 20)}")
    if a.lookback < 0:
        raise PairError("--lookback cannot be negative")
    if not 0 < a.entry < a.stop:
        raise PairError("need 0 < --entry < --stop")
    if not -a.entry < a.exit < a.entry:
        raise PairError("--exit must lie strictly between -entry and +entry")
    if not 2 <= a.splits <= 10:
        raise PairError("--splits must be 2..10")
    if a.max_half_life <= 0:
        raise PairError("--max-half-life must be positive")
    if a.adf_lag is not None and a.adf_lag < 0 or a.max_lag is not None and a.max_lag < 0:
        raise PairError("ADF lags cannot be negative")
    if a.cmd == "screen" and (a.min_bars < 60 or a.top < 1 or not 0 < a.alpha < 1):
        raise PairError("--min-bars must be >= 60, --top >= 1 and 0 < --alpha < 1")
    if a.cmd == "signal" and (a.max_hold is not None and a.max_hold < 1 or a.time_stop_mult <= 0):
        raise PairError("--max-hold must be >= 1 and --time-stop-mult > 0")
    if a.cmd == "plan":
        for name in ("lot_a", "lot_b", "margin_a", "margin_b", "hold", "price_a", "price_b"):
            v = getattr(a, name)
            if v is not None and v <= 0:
                raise PairError(f"--{name.replace('_', '-')} must be positive")
        if a.lots_a < 1 or a.max_lots < 1 or a.roll_days < 0 or a.margin_pct <= 0:
            raise PairError("--lots-a and --max-lots must be >= 1, --roll-days >= 0, --margin-pct > 0")
        if a.as_of:
            try:
                date.fromisoformat(a.as_of)
            except ValueError:
                raise PairError(f"--as-of {a.as_of!r}: use YYYY-MM-DD")


def main(argv=None) -> int:
    try:
        a = build_parser().parse_args(argv)
        validate(a)
        return _run(a)
    except PairError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except (OSError, UnicodeDecodeError, csv.Error) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


def _run(a) -> int:
    p = params_of(a)
    if a.cmd == "test":
        res = analyse_pair(load_pair(a.a, a.b, a), p)
        emit_json(res) if a.json else print_test(res)
    elif a.cmd == "signal":
        res = run_signal(load_pair(a.a, a.b, a), p, a.max_hold, a.time_stop_mult)
        emit_json(res) if a.json else print_signal(res)
    elif a.cmd == "screen":
        res = run_screen(load_members(a.members), a, p)
        emit_json(res) if a.json else print_screen(res, a.top)
    else:
        res = run_plan(a, p)
        emit_json(res) if a.json else print_plan(res)
    return 0


if __name__ == "__main__":
    sys.exit(main())
