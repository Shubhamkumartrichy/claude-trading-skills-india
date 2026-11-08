#!/usr/bin/env python3
"""Technical-analysis engine for NSE/BSE stocks and indices.

Zero dependencies (Python 3.10+ stdlib). Optional: `pip install yfinance` to fetch data.

Input (pick one)
  --csv FILE          OHLCV CSV (Date/Time, Open, High, Low, Close, Volume; any case;
                      TradingView/Kite/Groww/yfinance exports work)
  --candles FILE      JSON candles from a broker MCP: [[ts,o,h,l,c,v], ...] or [{...}, ...]
  --symbol NAME       fetch via yfinance (RELIANCE, TCS, NIFTY, BANKNIFTY, SENSEX, INDIAVIX ...)

Sub-commands
  summary      full dashboard: trend, indicators, levels, ATR stops, divergences, gaps (default)
  levels       support/resistance clusters
  divergence   RSI divergences (regular + hidden)
  fib          Fibonacci retracements/extensions (auto-swing or --high/--low)
  mtf          weekly + daily (+ monthly) three-screen view with confluence score
  indicators   last N rows of computed indicators
  fetch        download OHLCV to CSV (needs yfinance)

Examples
  python3 ta.py summary --symbol RELIANCE
  python3 ta.py mtf --csv hdfcbank.csv
  python3 ta.py fib --high 1608 --low 1372 --trend up
  python3 ta.py fetch --symbol NIFTY --period 5y --out nifty.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from datetime import date, datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

# Yahoo Finance tickers for common Indian indices. Stocks: SYMBOL.NS (NSE) / SYMBOL.BO (BSE).
YF_INDEX = {
    "NIFTY": "^NSEI", "NIFTY50": "^NSEI", "BANKNIFTY": "^NSEBANK", "NIFTYBANK": "^NSEBANK",
    "SENSEX": "^BSESN", "INDIAVIX": "^INDIAVIX", "VIX": "^INDIAVIX",
    "FINNIFTY": "NIFTY_FIN_SERVICE.NS", "MIDCPNIFTY": "NIFTY_MID_SELECT.NS",  # sparse on Yahoo
    "NIFTYIT": "^CNXIT", "NIFTYAUTO": "^CNXAUTO", "NIFTYPHARMA": "^CNXPHARMA",
    "NIFTYFMCG": "^CNXFMCG", "NIFTYMETAL": "^CNXMETAL", "NIFTYREALTY": "^CNXREALTY",
    "NIFTYENERGY": "^CNXENERGY", "NIFTYPSUBANK": "^CNXPSUBANK", "NIFTYINFRA": "^CNXINFRA",
    "NIFTYMIDCAP150": "NIFTYMIDCAP150.NS", "NIFTYSMALLCAP250": "NIFTYSMLCAP250.NS",
    # Quote only on Yahoo (no daily history as of Nov 2026): use a broker MCP or an NSE CSV instead.
    "NIFTYMEDIA": "^CNXMEDIA", "NIFTYMIDCAP100": "NIFTY_MIDCAP_100.NS",
}
CIRCUIT_BANDS = (2, 5, 10, 20)

FIB_RETR = (0.236, 0.382, 0.5, 0.618, 0.786)
FIB_EXT = (1.0, 1.272, 1.618, 2.0, 2.618)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def parse_ts(v) -> datetime:
    raw = str(v).strip()
    if len(raw) == 8 and raw.isdigit() and 1900 <= int(raw[:4]) <= 2200:   # YYYYMMDD, not epoch seconds
        try:
            return datetime.strptime(raw, "%Y%m%d")
        except ValueError:
            pass
    if isinstance(v, (int, float)) or (isinstance(v, str) and v.strip().lstrip("-").replace(".", "", 1).isdigit()):
        x = float(v)
        if x > 1e12:
            x /= 1000
        return datetime.fromtimestamp(x, tz=IST).replace(tzinfo=None)
    s = str(v).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo:
            dt = dt.astimezone(IST).replace(tzinfo=None)
        return dt
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S%z"):   # "+0530" offsets on Python 3.10
        try:
            return datetime.strptime(s, fmt).astimezone(IST).replace(tzinfo=None)
        except ValueError:
            continue
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y", "%d %b %Y", "%Y/%m/%d", "%m/%d/%Y",
                "%d-%m-%Y %H:%M", "%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M", "%d-%m-%Y %H:%M:%S",
                "%d/%m/%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"unrecognised timestamp {v!r}")


def _f(x) -> float:
    s = str(x).strip().replace(",", "")
    return float(s) if s not in ("", "-", "null", "None", "nan") else math.nan


def load_csv(path: str) -> list:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        cols = {c.strip().lower(): c for c in reader.fieldnames or []}

        def pick(*names):
            for n in names:
                if n in cols:
                    return cols[n]
            raise ValueError(f"CSV needs one of {names}; found {list(cols)}")
        cd = pick("date", "datetime", "time", "timestamp", "date time")
        ct = cols.get("time") if cols.get("time") and cols.get("time") != cd else None
        co, chh, cl, cc = pick("open"), pick("high"), pick("low"), pick("close", "close price", "ltp")
        cv = next((cols[n] for n in ("volume", "vol", "total traded quantity", "qty") if n in cols), None)
        rows = []
        for r in reader:
            try:
                stamp = f"{r[cd].strip()} {r[ct].strip()}" if ct and r.get(ct) else r[cd]
                row = {"date": parse_ts(stamp), "open": _f(r[co]), "high": _f(r[chh]),
                       "low": _f(r[cl]), "close": _f(r[cc]), "volume": _f(r[cv]) if cv else 0.0}
            except (ValueError, KeyError):
                continue
            if not any(math.isnan(row[k]) for k in ("open", "high", "low", "close")):
                if math.isnan(row["volume"]):
                    row["volume"] = 0.0
                rows.append(row)
    return _clean(rows)


def load_candles_json(path: str) -> list:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict):
        for key in ("candles", "data", "result", "payload"):
            if key in data:
                data = data[key]
                if isinstance(data, dict) and "candles" in data:
                    data = data["candles"]
                break
    if isinstance(data, dict):
        low = {k.lower(): v for k, v in data.items()}
        tkey = next((k for k in ("timestamp", "time", "date", "datetime", "start_time") if k in low), None)
        if tkey and isinstance(low.get("open"), list):
            data = [[t, o, h, l, c, v] for t, o, h, l, c, v in
                    zip(low[tkey], low["open"], low["high"], low["low"], low["close"],
                        low.get("volume") or [0] * len(low["open"]))]
        else:
            raise ValueError("unrecognised candle JSON: expected a list of candles or column arrays")
    rows = []
    for c in data:
        if isinstance(c, (list, tuple)):
            ts, o, h, l, cl = c[:5]
            v = c[5] if len(c) > 5 else 0
        else:
            low = {k.lower(): val for k, val in c.items()}
            ts = low.get("date") or low.get("timestamp") or low.get("time") or low.get("datetime")
            o, h, l, cl = low["open"], low["high"], low["low"], low["close"]
            v = low.get("volume", 0)
        rows.append({"date": parse_ts(ts), "open": float(o), "high": float(h), "low": float(l),
                     "close": float(cl), "volume": float(v or 0)})
    return _clean(rows)


def _clean(rows: list) -> list:
    rows.sort(key=lambda r: r["date"])
    out, seen = [], set()
    for r in rows:
        if r["date"] in seen:
            continue
        seen.add(r["date"])
        out.append(r)
    return out


def yf_ticker(symbol: str, exchange: str = "NSE") -> str:
    s = symbol.upper().replace(" ", "")
    if s in YF_INDEX:
        return YF_INDEX[s]
    if s.startswith("^") or s.endswith((".NS", ".BO")):
        return s
    return s + (".BO" if exchange.upper() == "BSE" else ".NS")


def fetch_yf(symbol: str, exchange="NSE", period="2y", interval="1d") -> list:
    try:
        import yfinance as yf  # type: ignore
    except ImportError:
        raise SystemExit("yfinance not installed: pip install yfinance  (or pass --csv / --candles)")
    t = yf_ticker(symbol, exchange)
    # auto_adjust=False keeps split-adjusted (not dividend-adjusted) prices, matching broker charts.
    df = yf.Ticker(t).history(period=period, interval=interval, auto_adjust=False)
    if df is None or df.empty:
        raise SystemExit(f"no data for {t}; check the symbol (Yahoo uses .NS/.BO suffixes)")
    rows = []
    for idx, r in df.iterrows():
        d = idx.to_pydatetime()
        if d.tzinfo:
            d = d.astimezone(IST).replace(tzinfo=None)
        rows.append({"date": d, "open": float(r["Open"]), "high": float(r["High"]), "low": float(r["Low"]),
                     "close": float(r["Close"]), "volume": float(r.get("Volume", 0) or 0)})
    return _clean(rows)


# ---------------------------------------------------------------------------
# Indicators (lists aligned to bars; None during warm-up)
# ---------------------------------------------------------------------------
def sma(vals: list, n: int) -> list:
    out = [None] * len(vals)
    for i in range(n - 1, len(vals)):
        w = vals[i - n + 1:i + 1]
        if None not in w:
            out[i] = sum(w) / n
    return out


def ema(vals: list, n: int) -> list:
    out = [None] * len(vals)
    start = next((i for i, v in enumerate(vals) if v is not None), None)
    if start is None or len(vals) - start < n:
        return out
    k = 2 / (n + 1)
    prev = sum(vals[start:start + n]) / n
    out[start + n - 1] = prev
    for i in range(start + n, len(vals)):
        prev = vals[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(closes: list, n: int = 14) -> list:
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    losses = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n

    def val(g, l):
        if l == 0:
            return 50.0 if g == 0 else 100.0
        return 100 - 100 / (1 + g / l)
    out[n] = val(ag, al)
    for i in range(n + 1, len(closes)):
        ag = (ag * (n - 1) + gains[i - 1]) / n
        al = (al * (n - 1) + losses[i - 1]) / n
        out[i] = val(ag, al)
    return out


def true_range(h, l, c) -> list:
    return [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
                            for i in range(1, len(c))]


def rma(vals: list, n: int) -> list:
    """Wilder's moving average (seeded with SMA)."""
    out = [None] * len(vals)
    if len(vals) < n:
        return out
    prev = sum(vals[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(vals)):
        prev = (prev * (n - 1) + vals[i]) / n
        out[i] = prev
    return out


def atr(h, l, c, n=14) -> list:
    return rma(true_range(h, l, c), n)


def macd(closes, fast=12, slow=26, signal=9):
    ef, es = ema(closes, fast), ema(closes, slow)
    line = [a - b if a is not None and b is not None else None for a, b in zip(ef, es)]
    sig = ema(line, signal)
    hist = [a - b if a is not None and b is not None else None for a, b in zip(line, sig)]
    return line, sig, hist


def bollinger(closes, n=20, k=2.0):
    mid = sma(closes, n)
    up, lo = [None] * len(closes), [None] * len(closes)
    for i in range(n - 1, len(closes)):
        w = closes[i - n + 1:i + 1]
        m = mid[i]
        sd = math.sqrt(sum((x - m) ** 2 for x in w) / n)
        up[i], lo[i] = m + k * sd, m - k * sd
    return mid, up, lo


def adx(h, l, c, n=14):
    length = len(c)
    plus_di, minus_di, adx_v = [None] * length, [None] * length, [None] * length
    if length < 2 * n + 1:
        return plus_di, minus_di, adx_v
    tr = true_range(h, l, c)
    pdm, mdm = [0.0], [0.0]
    for i in range(1, length):
        up, dn = h[i] - h[i - 1], l[i - 1] - l[i]
        pdm.append(up if up > dn and up > 0 else 0.0)
        mdm.append(dn if dn > up and dn > 0 else 0.0)
    s_tr, s_p, s_m = sum(tr[1:n + 1]), sum(pdm[1:n + 1]), sum(mdm[1:n + 1])
    dx = [None] * length
    for i in range(n, length):
        if i > n:
            s_tr = s_tr - s_tr / n + tr[i]
            s_p = s_p - s_p / n + pdm[i]
            s_m = s_m - s_m / n + mdm[i]
        p = 100 * s_p / s_tr if s_tr else 0.0
        m = 100 * s_m / s_tr if s_tr else 0.0
        plus_di[i], minus_di[i] = p, m
        dx[i] = 100 * abs(p - m) / (p + m) if (p + m) else 0.0
    first = 2 * n - 1
    prev = sum(dx[n:first + 1]) / n
    adx_v[first] = prev
    for i in range(first + 1, length):
        prev = (prev * (n - 1) + dx[i]) / n
        adx_v[i] = prev
    return plus_di, minus_di, adx_v


def stochastic(h, l, c, n=14, smooth_k=3, d=3):
    raw = [None] * len(c)
    for i in range(n - 1, len(c)):
        hh, ll = max(h[i - n + 1:i + 1]), min(l[i - n + 1:i + 1])
        raw[i] = 50.0 if hh == ll else 100 * (c[i] - ll) / (hh - ll)
    k = sma(raw, smooth_k)
    return k, sma(k, d)


def compute(rows: list) -> dict:
    o = [r["open"] for r in rows]
    h = [r["high"] for r in rows]
    l = [r["low"] for r in rows]
    c = [r["close"] for r in rows]
    v = [r["volume"] for r in rows]
    ind = {"open": o, "high": h, "low": l, "close": c, "volume": v,
           "date": [r["date"] for r in rows]}
    for n in (20, 50, 100, 200):
        ind[f"sma{n}"] = sma(c, n)
    ind["ema20"], ind["ema50"] = ema(c, 20), ema(c, 50)
    ind["rsi"] = rsi(c)
    ind["macd"], ind["macd_signal"], ind["macd_hist"] = macd(c)
    ind["bb_mid"], ind["bb_up"], ind["bb_lo"] = bollinger(c)
    ind["atr"] = atr(h, l, c)
    ind["plus_di"], ind["minus_di"], ind["adx"] = adx(h, l, c)
    ind["stoch_k"], ind["stoch_d"] = stochastic(h, l, c)
    ind["vol_avg20"] = sma(v, 20)
    return ind


# ---------------------------------------------------------------------------
# Structure: swings, levels, gaps, divergences, Fibonacci
# ---------------------------------------------------------------------------
def swings(h: list, l: list, k: int = 5):
    sh, sl = [], []
    for i in range(k, len(h) - k):
        if h[i] > max(h[i - k:i]) and h[i] >= max(h[i + 1:i + k + 1]):
            sh.append(i)
        if l[i] < min(l[i - k:i]) and l[i] <= min(l[i + 1:i + k + 1]):
            sl.append(i)
    return sh, sl


def structure(h, l, sh, sl) -> str:
    if len(sh) < 2 or len(sl) < 2:
        return "insufficient swings"
    hh = h[sh[-1]] > h[sh[-2]]
    hl = l[sl[-1]] > l[sl[-2]]
    if hh and hl:
        return "uptrend (higher highs & higher lows)"
    if not hh and not hl:
        return "downtrend (lower highs & lower lows)"
    return "mixed / range (" + ("higher high, lower low" if hh else "lower high, higher low") + ")"


def sr_levels(ind: dict, k: int = 5, tol_pct: float = 1.5, lookback: int = 250) -> dict:
    h, l, c = ind["high"], ind["low"], ind["close"]
    start = max(0, len(c) - lookback)
    sh, sl = swings(h, l, k)
    pts = sorted([(h[i], i) for i in sh if i >= start] + [(l[i], i) for i in sl if i >= start])
    clusters = []
    for p, i in pts:
        if clusters and abs(p - clusters[-1]["level"]) / clusters[-1]["level"] * 100 <= tol_pct:
            cl = clusters[-1]
            cl["prices"].append(p)
            cl["idx"].append(i)
            cl["level"] = sum(cl["prices"]) / len(cl["prices"])
        else:
            clusters.append({"level": p, "prices": [p], "idx": [i]})
    price = c[-1]
    dates = ind["date"]
    fmt = lambda cl: {"level": round(cl["level"], 2), "touches": len(cl["prices"]),
                      "last_touch": dates[max(cl["idx"])].date().isoformat()}
    sup = sorted([cl for cl in clusters if cl["level"] < price], key=lambda x: -x["level"])
    res = sorted([cl for cl in clusters if cl["level"] >= price], key=lambda x: x["level"])
    # Add 52-week extremes and round numbers as context
    yr = min(len(c), 252)
    hi52, lo52 = max(h[-yr:]), min(l[-yr:])
    step = 10 ** max(1, int(math.log10(price)) - 1) * (5 if price > 5000 else 1)
    rn_up = math.ceil(price / step) * step
    rn_dn = math.floor(price / step) * step
    return {"supports": [fmt(x) for x in sup[:4]], "resistances": [fmt(x) for x in res[:4]],
            "high_52w": hi52, "low_52w": lo52,
            "round_numbers": [rn_dn if rn_dn != price else rn_dn - step, rn_up if rn_up != price else rn_up + step]}


def unfilled_gaps(ind: dict, lookback: int = 120, limit: int = 5) -> list:
    h, l, dates = ind["high"], ind["low"], ind["date"]
    n = len(h)
    out = []
    for i in range(max(1, n - lookback), n):
        if l[i] > h[i - 1]:      # gap up: zone (prev high, this low)
            later_low = min(l[i + 1:], default=math.inf)
            if later_low > h[i - 1]:
                out.append({"date": dates[i].date().isoformat(), "type": "gap-up (support)",
                            "zone": [round(h[i - 1], 2), round(min(l[i], later_low), 2)]})
        elif h[i] < l[i - 1]:    # gap down: zone (this high, prev low)
            later_high = max(h[i + 1:], default=-math.inf)
            if later_high < l[i - 1]:
                out.append({"date": dates[i].date().isoformat(), "type": "gap-down (resistance)",
                            "zone": [round(max(h[i], later_high), 2), round(l[i - 1], 2)]})
    return out[-limit:]


def divergences(ind: dict, k: int = 3, lookback: int = 90, min_sep: int = 5) -> list:
    h, l, r, dates = ind["high"], ind["low"], ind["rsi"], ind["date"]
    n = len(h)
    sh, sl = swings(h, l, k)
    sh = [i for i in sh if i >= n - lookback and r[i] is not None]
    sl = [i for i in sl if i >= n - lookback and r[i] is not None]
    out = []

    def add(kind, i, j, pa, pb, ra, rb):
        zone = rb
        strength = "standard"
        if kind.startswith("bullish regular") and min(ra, rb) < 30:
            strength = "strong (oversold)"
        if kind.startswith("bearish regular") and max(ra, rb) > 70:
            strength = "strong (overbought)"
        out.append({"type": kind, "from": dates[i].date().isoformat(), "to": dates[j].date().isoformat(),
                    "price": [round(pa, 2), round(pb, 2)], "rsi": [round(ra, 1), round(rb, 1)],
                    "strength": strength, "bars_ago": n - 1 - j,
                    "neutral_zone_warning": 40 <= zone <= 60})
    for a, b in zip(sl, sl[1:]):
        if b - a < min_sep:
            continue
        if l[b] < l[a] and r[b] > r[a]:
            add("bullish regular (reversal)", a, b, l[a], l[b], r[a], r[b])
        elif l[b] > l[a] and r[b] < r[a]:
            add("bullish hidden (continuation)", a, b, l[a], l[b], r[a], r[b])
    for a, b in zip(sh, sh[1:]):
        if b - a < min_sep:
            continue
        if h[b] > h[a] and r[b] < r[a]:
            add("bearish regular (reversal)", a, b, h[a], h[b], r[a], r[b])
        elif h[b] < h[a] and r[b] > r[a]:
            add("bearish hidden (continuation)", a, b, h[a], h[b], r[a], r[b])
    out.sort(key=lambda x: x["bars_ago"])
    return out


def fib_levels(high: float, low: float, trend: str = "up", pivot: float | None = None) -> dict:
    """trend='up': swing low -> swing high (retracements are pullback entries below the high).
    trend='down': swing high -> swing low (retracements are bounce levels above the low).
    pivot: retracement extreme (point C) for trend-based extensions."""
    diff = high - low
    if trend == "up":
        retr = {f"{r * 100:.1f}%": high - diff * r for r in FIB_RETR}
        base = pivot if pivot is not None else low
        ext = {f"{e * 100:.1f}%": base + diff * e for e in FIB_EXT}
    else:
        retr = {f"{r * 100:.1f}%": low + diff * r for r in FIB_RETR}
        base = pivot if pivot is not None else high
        ext = {f"{e * 100:.1f}%": base - diff * e for e in FIB_EXT}
    return {"trend": trend, "high": high, "low": low, "pivot_C": pivot,
            "extension_base": "C (trend-based)" if pivot is not None else ("A (swing low)" if trend == "up" else "A (swing high)"),
            "retracements": retr, "extensions": ext}


def auto_fib(ind: dict, lookback: int = 120) -> dict:
    h, l, c = ind["high"], ind["low"], ind["close"]
    n = len(c)
    s = max(0, n - lookback)
    ih = max(range(s, n), key=lambda i: h[i])
    il = min(range(s, n), key=lambda i: l[i])
    if il < ih:   # up-swing: A=low, B=high, C=lowest low after B
        pivot = min(l[ih + 1:], default=None) if ih < n - 1 else None
        res = fib_levels(h[ih], l[il], "up", pivot if pivot is not None and pivot > l[il] else None)
    else:         # down-swing: A=high, B=low, C=highest high after B
        pivot = max(h[il + 1:], default=None) if il < n - 1 else None
        res = fib_levels(h[ih], l[il], "down", pivot if pivot is not None and pivot < h[ih] else None)
    res["swing_dates"] = {"high": ind["date"][ih].date().isoformat(), "low": ind["date"][il].date().isoformat()}
    price = c[-1]
    lv = sorted(res["retracements"].items(), key=lambda kv: kv[1])
    below = [k for k, v in lv if v <= price]
    above = [k for k, v in lv if v > price]
    res["price_position"] = (f"between {below[-1] if below else 'swing low'} and "
                             f"{above[0] if above else 'swing high'} retracement")
    return res


# ---------------------------------------------------------------------------
# Resampling for multi-timeframe
# ---------------------------------------------------------------------------
def resample(rows: list, rule: str = "W") -> list:
    groups = {}
    for r in rows:
        d = r["date"]
        key = tuple(d.isocalendar()[:2]) if rule == "W" else (d.year, d.month)
        g = groups.get(key)
        if g is None:
            groups[key] = dict(r)
        else:
            g["high"] = max(g["high"], r["high"])
            g["low"] = min(g["low"], r["low"])
            g["close"] = r["close"]
            g["volume"] += r["volume"]
            g["date"] = r["date"]
    return [groups[k] for k in sorted(groups)]


# ---------------------------------------------------------------------------
# Interpretation
# ---------------------------------------------------------------------------
def _last(x):
    return x[-1] if x and x[-1] is not None else None


def _prev(x, back=1):
    return x[-1 - back] if len(x) > back and x[-1 - back] is not None else None


def dashboard(ind: dict) -> dict:
    c = ind["close"][-1]
    sig = {}
    r, rp = _last(ind["rsi"]), _prev(ind["rsi"], 3)
    if r is not None:
        rising = rp is not None and r > rp
        if r >= 70:
            sig["RSI(14)"] = (f"{r:.1f} overbought", "bearish" if not rising else "neutral")
        elif r <= 30:
            sig["RSI(14)"] = (f"{r:.1f} oversold", "bullish" if rising else "neutral")
        elif r > 50:
            sig["RSI(14)"] = (f"{r:.1f} {'rising' if rising else 'falling'}", "bullish" if rising or r > 55 else "neutral")
        else:
            sig["RSI(14)"] = (f"{r:.1f} {'rising' if rising else 'falling'}", "bearish" if not rising or r < 45 else "neutral")
    m, s_, hst, hp = _last(ind["macd"]), _last(ind["macd_signal"]), _last(ind["macd_hist"]), _prev(ind["macd_hist"])
    if hst is not None:
        growing = hp is not None and abs(hst) > abs(hp)
        if hst > 0:
            sig["MACD(12,26,9)"] = (f"line {'>' if m > s_ else '<'} signal, hist {hst:+.2f} {'growing' if growing else 'shrinking'}",
                                   "bullish" if growing or m > 0 else "neutral")
        else:
            sig["MACD(12,26,9)"] = (f"line < signal, hist {hst:+.2f} {'growing' if growing else 'shrinking'}",
                                   "bearish" if growing or m < 0 else "neutral")
    up, lo, mid = _last(ind["bb_up"]), _last(ind["bb_lo"]), _last(ind["bb_mid"])
    if up is not None:
        pb = (c - lo) / (up - lo) if up != lo else 0.5
        width = (up - lo) / mid * 100
        state = "bullish" if 0.5 < pb < 1.0 else "bearish" if 0.0 < pb < 0.5 else "neutral"
        note = "above upper band" if pb >= 1 else "below lower band" if pb <= 0 else f"%B {pb:.2f}"
        sig["Bollinger(20,2)"] = (f"{note}, width {width:.1f}%", state)
    a, p, mi = _last(ind["adx"]), _last(ind["plus_di"]), _last(ind["minus_di"])
    if a is not None:
        if a < 20:
            sig["ADX(14)"] = (f"{a:.1f} (no trend; +DI {p:.0f} / -DI {mi:.0f})", "neutral")
        else:
            sig["ADX(14)"] = (f"{a:.1f} {'trending' if a >= 25 else 'emerging'} (+DI {p:.0f} / -DI {mi:.0f})",
                              "bullish" if p > mi else "bearish")
    k, d = _last(ind["stoch_k"]), _last(ind["stoch_d"])
    if k is not None and d is not None:
        if k > d and k < 30:
            sig["Stoch(14,3,3)"] = (f"%K {k:.0f} crossing up from oversold", "bullish")
        elif k < d and k > 70:
            sig["Stoch(14,3,3)"] = (f"%K {k:.0f} crossing down from overbought", "bearish")
        else:
            sig["Stoch(14,3,3)"] = (f"%K {k:.0f} / %D {d:.0f}", "bullish" if k > d else "bearish" if k < d else "neutral")
    s20, s50, s200 = _last(ind["sma20"]), _last(ind["sma50"]), _last(ind["sma200"])
    if s20 and s50 and s200:
        if c > s20 > s50 > s200:
            sig["MA alignment"] = ("price > SMA20 > SMA50 > SMA200", "bullish")
        elif c < s20 < s50 < s200:
            sig["MA alignment"] = ("price < SMA20 < SMA50 < SMA200", "bearish")
        else:
            above = sum(c > x for x in (s20, s50, s200))
            sig["MA alignment"] = (f"mixed (above {above}/3 of SMA20/50/200)",
                                   "bullish" if above == 3 else "bearish" if above == 0 else "neutral")
    elif s20 and s50:
        sig["MA alignment"] = ("price vs SMA20/50 (no 200 yet)",
                               "bullish" if c > s20 > s50 else "bearish" if c < s20 < s50 else "neutral")
    counts = {"bullish": 0, "bearish": 0, "neutral": 0}
    for _, (_, s) in sig.items():
        counts[s] += 1
    return {"signals": sig, "counts": counts}


def cross_signal(ind: dict, lookback: int = 20):
    s50, s200 = ind["sma50"], ind["sma200"]
    n = len(s50)
    for i in range(n - 1, max(0, n - lookback) - 1, -1):
        if None in (s50[i], s200[i], s50[i - 1], s200[i - 1]):
            break
        if s50[i] > s200[i] and s50[i - 1] <= s200[i - 1]:
            return f"golden cross {n - 1 - i} bars ago"
        if s50[i] < s200[i] and s50[i - 1] >= s200[i - 1]:
            return f"death cross {n - 1 - i} bars ago"
    if s50[-1] is not None and s200[-1] is not None:
        return "SMA50 above SMA200" if s50[-1] > s200[-1] else "SMA50 below SMA200"
    return None


def india_checks(ind: dict) -> list:
    c, h, l, o = ind["close"], ind["high"], ind["low"], ind["open"]
    notes = []
    if len(c) >= 2:
        chg = (c[-1] / c[-2] - 1) * 100
        for band in CIRCUIT_BANDS:
            if abs(abs(chg) - band) < 0.15:
                if chg > 0 and abs(c[-1] - h[-1]) < 1e-9:
                    notes.append(f"Closed at day high, +{band}%: possible UPPER CIRCUIT — buyers may be stuck in "
                                 "queue; next-day gap risk both ways.")
                if chg < 0 and abs(c[-1] - l[-1]) < 1e-9:
                    notes.append(f"Closed at day low, −{band}%: possible LOWER CIRCUIT — exits may be impossible "
                                 "until the band opens; stop-losses can't fill.")
    gaps = [abs(o[i] / c[i - 1] - 1) * 100 for i in range(max(1, len(c) - 60), len(c))]
    if gaps:
        avg_gap, max_gap = sum(gaps) / len(gaps), max(gaps)
        notes.append(f"Overnight gaps (60 bars): avg {avg_gap:.2f}%, max {max_gap:.2f}% — stops sit behind this "
                     "gap risk; size for it.")
    return notes


def atr_stops(ind: dict, sh, sl, buffer_pct: float = 0.5) -> dict:
    c, a = ind["close"][-1], _last(ind["atr"])
    out = {"atr": a, "atr_pct": a / c * 100 if a else None}
    if a:
        out["long"] = {f"{m}x ATR": round(c - m * a, 2) for m in (1.5, 2.0, 2.5)}
        out["short"] = {f"{m}x ATR": round(c + m * a, 2) for m in (1.5, 2.0, 2.5)}
    if sl:
        lo = ind["low"][sl[-1]]
        out["structure_long"] = {"swing_low": lo, "stop": round(lo * (1 - buffer_pct / 100), 2),
                                 "date": ind["date"][sl[-1]].date().isoformat()}
    if sh:
        hi = ind["high"][sh[-1]]
        out["structure_short"] = {"swing_high": hi, "stop": round(hi * (1 + buffer_pct / 100), 2),
                                  "date": ind["date"][sh[-1]].date().isoformat()}
    return out


def tf_view(rows: list, label: str, swing_k: int) -> dict:
    ind = compute(rows)
    sh, sl = swings(ind["high"], ind["low"], swing_k)
    d = dashboard(ind)
    cnt = d["counts"]
    bias = ("bullish" if cnt["bullish"] > cnt["bearish"] + 1 else
            "bearish" if cnt["bearish"] > cnt["bullish"] + 1 else "neutral")
    st = structure(ind["high"], ind["low"], sh, sl)
    if st.startswith("uptrend") and bias != "bearish":
        bias = "bullish"
    elif st.startswith("downtrend") and bias != "bullish":
        bias = "bearish"
    return {"timeframe": label, "bars": len(rows), "close": ind["close"][-1], "structure": st,
            "bias": bias, "dashboard": d, "ind": ind, "sh": sh, "sl": sl}


def confluence(weekly: dict, daily: dict, direction: str = "long") -> dict:
    ind = daily["ind"]
    c = ind["close"][-1]
    a = _last(ind["atr"]) or c * 0.02
    want = "bullish" if direction == "long" else "bearish"
    pts = {}
    pts["Weekly trend agrees"] = weekly["bias"] == want
    s20, s50 = _last(ind["sma20"]), _last(ind["sma50"])
    lv = sr_levels(ind)
    near_level = lv["supports"] if direction == "long" else lv["resistances"]
    pullback = any(x is not None and abs(c - x) <= a for x in (s20, s50)) or \
        any(abs(c - x["level"]) <= a for x in near_level[:2])
    pts["Daily setup (pullback to MA/level in trend)"] = pullback and daily["bias"] != ("bearish" if direction == "long" else "bullish")
    r, rp = _last(ind["rsi"]), _prev(ind["rsi"], 3)
    pts["RSI supports"] = r is not None and rp is not None and (
        (r > rp and 40 <= r <= 70) if direction == "long" else (r < rp and 30 <= r <= 60))
    hst, hp = _last(ind["macd_hist"]), _prev(ind["macd_hist"])
    pts["MACD supports"] = hst is not None and hp is not None and (hst > hp if direction == "long" else hst < hp)
    pts["Clear S/R for stop/target"] = bool(near_level) and abs(c - near_level[0]["level"]) <= 2 * a
    v, va = ind["volume"][-1], _last(ind["vol_avg20"])
    up_day = c >= ind["close"][-2]
    pts["Volume confirms"] = bool(va) and ((v > va * 1.2 and up_day) or (v < va * 0.8 and not up_day)) \
        if direction == "long" else bool(va) and ((v > va * 1.2 and not up_day) or (v < va * 0.8 and up_day))
    score = sum(pts.values())
    action = ("strong setup — full size" if score >= 5 else "good setup — full size" if score == 4 else
              "marginal — half size" if score == 3 else "weak — skip or paper trade" if score == 2 else "no setup")
    return {"direction": direction, "score": score, "factors": pts, "action": action}


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def fnum(x, d=2):
    return "—" if x is None else f"{x:,.{d}f}"


def freshness(rows: list) -> str | None:
    last = rows[-1]["date"].date()
    today = datetime.now(IST).date()
    gap = (today - last).days
    if gap > 4:
        return f"⚠ Data ends {last} ({gap} days ago) — refresh before acting."
    return None


def summary(rows: list, name: str, swing_k: int = 5) -> dict:
    ind = compute(rows)
    sh, sl = swings(ind["high"], ind["low"], swing_k)
    c = ind["close"]
    yr = min(len(c), 252)
    hi52, lo52 = max(ind["high"][-yr:]), min(ind["low"][-yr:])
    vol, va = ind["volume"][-1], _last(ind["vol_avg20"])
    recent = rows[-10:]
    intraday = len({r["date"].date() for r in recent}) < len(recent)   # several bars share a day
    return {
        "range_label": f"{yr}-bar H/L" if intraday else "52w H/L",
        "name": name, "last_date": rows[-1]["date"].isoformat(sep=" ")[:16], "bars": len(rows),
        "close": c[-1], "change_pct": (c[-1] / c[-2] - 1) * 100 if len(c) > 1 else 0.0,
        "high_52w": hi52, "low_52w": lo52, "from_high_pct": (c[-1] / hi52 - 1) * 100,
        "structure": structure(ind["high"], ind["low"], sh, sl), "ma_cross": cross_signal(ind),
        "smas": {k: _last(ind[k]) for k in ("sma20", "sma50", "sma200")},
        "dashboard": dashboard(ind),
        "volume_ratio": (vol / va) if va else None,
        "levels": sr_levels(ind, swing_k),
        "gaps": unfilled_gaps(ind),
        "divergences": divergences(ind)[:3],
        "stops": atr_stops(ind, sh, sl),
        "india_checks": india_checks(ind),
        "freshness": freshness(rows),
    }


def print_summary(s: dict) -> None:
    print(f"\n{s['name']} — last bar {s['last_date']} ({s['bars']} bars)")
    if s["freshness"]:
        print(s["freshness"])
    print(f"Close {fnum(s['close'])} ({s['change_pct']:+.2f}%) | {s['range_label']} {fnum(s['high_52w'])} / "
          f"{fnum(s['low_52w'])} ({s['from_high_pct']:+.1f}% from high)")
    sm = s["smas"]
    print(f"SMA20 {fnum(sm['sma20'])} | SMA50 {fnum(sm['sma50'])} | SMA200 {fnum(sm['sma200'])}"
          + (f" | {s['ma_cross']}" if s["ma_cross"] else ""))
    print(f"Structure: {s['structure']}")
    print("\nIndicator dashboard:")
    for k, (desc, state) in s["dashboard"]["signals"].items():
        print(f"  {k:<16} {state:<8} {desc}")
    cn = s["dashboard"]["counts"]
    print(f"  → {cn['bullish']} bullish / {cn['bearish']} bearish / {cn['neutral']} neutral")
    if s["volume_ratio"]:
        vr = s["volume_ratio"]
        print(f"Volume: {vr:.2f}x 20-day average" + (" (significant)" if vr >= 1.5 else ""))
    lv = s["levels"]
    print("\nResistance:", ", ".join(f"{x['level']:,.2f} ({x['touches']}x)" for x in lv["resistances"]) or "none (near highs)")
    print("Support:   ", ", ".join(f"{x['level']:,.2f} ({x['touches']}x)" for x in lv["supports"]) or "none (near lows)")
    print(f"Round numbers nearby: {', '.join(f'{x:,.0f}' for x in lv['round_numbers'])}")
    for g in s["gaps"]:
        print(f"Unfilled {g['type']} from {g['date']}: {g['zone'][0]:,.2f} – {g['zone'][1]:,.2f}")
    st = s["stops"]
    if st.get("atr"):
        print(f"\nATR(14) {st['atr']:.2f} ({st['atr_pct']:.2f}% of price)")
        print("  Long stops : " + " | ".join(f"{k} {v:,.2f}" for k, v in st["long"].items()))
        if "structure_long" in st:
            x = st["structure_long"]
            print(f"  Structure  : below swing low {x['swing_low']:,.2f} ({x['date']}) → {x['stop']:,.2f}")
    if s["divergences"]:
        print("\nRSI divergences (most recent first):")
        for d in s["divergences"]:
            warn = " — RSI in 40-60 zone, weaker" if d["neutral_zone_warning"] else ""
            print(f"  {d['type']}: {d['from']} → {d['to']} price {d['price'][0]:,.2f}→{d['price'][1]:,.2f}, "
                  f"RSI {d['rsi'][0]}→{d['rsi'][1]} [{d['strength']}, {d['bars_ago']} bars ago]{warn}")
    else:
        print("\nRSI divergences: none in the last 90 bars")
    for n in s["india_checks"]:
        print("•", n)


def print_mtf(views: list, conf_long: dict, conf_short: dict) -> None:
    for v in views:
        cn = v["dashboard"]["counts"]
        print(f"\n[{v['timeframe']}] {v['bars']} bars, close {fnum(v['close'])} → bias {v['bias'].upper()}")
        print(f"  structure: {v['structure']}")
        for k, (desc, state) in v["dashboard"]["signals"].items():
            print(f"  {k:<16} {state:<8} {desc}")
        print(f"  ({cn['bullish']} bull / {cn['bearish']} bear / {cn['neutral']} neutral)")
    for cf in (conf_long, conf_short):
        print(f"\nConfluence score for a {cf['direction'].upper()}: {cf['score']}/6 → {cf['action']}")
        for k, ok in cf["factors"].items():
            print(f"  [{'x' if ok else ' '}] {k}")
    print("\nMechanical score — confirm visually; for entry timing use the hourly/15-min chart.")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Technical analysis for NSE/BSE")
    p.add_argument("cmd", nargs="?", default="summary",
                   choices=["summary", "levels", "divergence", "fib", "mtf", "indicators", "fetch"])
    p.add_argument("--csv")
    p.add_argument("--candles")
    p.add_argument("--symbol")
    p.add_argument("--exchange", default="NSE")
    p.add_argument("--period", default="2y")
    p.add_argument("--interval", default="1d")
    p.add_argument("--swing", type=int, default=5, help="bars each side for swing points")
    p.add_argument("--rows", type=int, default=10)
    p.add_argument("--high", type=float)
    p.add_argument("--low", type=float)
    p.add_argument("--pivot", type=float, help="point C for trend-based extensions")
    p.add_argument("--trend", choices=["up", "down"], default="up")
    p.add_argument("--out")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)

    if a.cmd == "fib" and a.high is not None and a.low is not None:
        res = fib_levels(a.high, a.low, a.trend, a.pivot)
        _print_fib(res, a.json)
        return 0

    if a.csv:
        rows, name = load_csv(a.csv), a.symbol or a.csv
    elif a.candles:
        rows, name = load_candles_json(a.candles), a.symbol or a.candles
    elif a.symbol:
        rows, name = fetch_yf(a.symbol, a.exchange, a.period, a.interval), yf_ticker(a.symbol, a.exchange)
    else:
        p.error("give --csv, --candles or --symbol")
        return 2
    if a.cmd == "fetch":
        out = a.out or f"{(a.symbol or 'data').upper()}.csv"
        with open(out, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["Date", "Open", "High", "Low", "Close", "Volume"])
            for r in rows:
                w.writerow([r["date"].isoformat(sep=" "), round(r["open"], 2), round(r["high"], 2),
                            round(r["low"], 2), round(r["close"], 2), int(r["volume"])])
        print(f"wrote {len(rows)} bars to {out} (last {rows[-1]['date']:%Y-%m-%d})")
        return 0

    if len(rows) < 30:
        print(f"only {len(rows)} bars — need at least 30 (200+ for SMA200)", file=sys.stderr)
        return 2

    if a.cmd == "summary":
        s = summary(rows, name, a.swing)
        if a.json:
            print(json.dumps(s, indent=2, default=str))
        else:
            print_summary(s)
        return 0

    ind = compute(rows)
    if a.cmd == "levels":
        res = sr_levels(ind, a.swing)
        res["gaps"] = unfilled_gaps(ind)
        print(json.dumps(res, indent=2, default=str))
        return 0
    if a.cmd == "divergence":
        res = divergences(ind)
        if a.json:
            print(json.dumps(res, indent=2))
        elif not res:
            print("No RSI divergences in the last 90 bars.")
        else:
            for d in res:
                print(f"{d['type']}: {d['from']} → {d['to']} | price {d['price']} | RSI {d['rsi']} | "
                      f"{d['strength']} | {d['bars_ago']} bars ago" + (" | RSI 40-60: weak" if d["neutral_zone_warning"] else ""))
        return 0
    if a.cmd == "fib":
        _print_fib(auto_fib(ind), a.json, ind["close"][-1])
        return 0
    if a.cmd == "mtf":
        daily_rows = rows
        weekly = tf_view(resample(daily_rows, "W"), "WEEKLY (screen 1: bias)", 2)
        daily = tf_view(daily_rows, "DAILY (screen 2: setup)", a.swing)
        views = [weekly, daily]
        monthly_rows = resample(daily_rows, "M")
        if len(monthly_rows) >= 30:
            views.insert(0, tf_view(monthly_rows, "MONTHLY (context)", 2))
        cl, cs = confluence(weekly, daily, "long"), confluence(weekly, daily, "short")
        if a.json:
            strip = lambda v: {k: val for k, val in v.items() if k not in ("ind", "sh", "sl")}
            print(json.dumps({"views": [strip(v) for v in views], "long": cl, "short": cs}, indent=2, default=str))
        else:
            print_mtf(views, cl, cs)
        return 0
    if a.cmd == "indicators":
        keys = ["close", "sma20", "sma50", "sma200", "rsi", "macd", "macd_signal", "macd_hist",
                "bb_up", "bb_lo", "atr", "adx", "plus_di", "minus_di", "stoch_k", "stoch_d"]
        if a.json:
            out = [{"date": ind["date"][i].isoformat(), **{k: ind[k][i] for k in keys}}
                   for i in range(max(0, len(rows) - a.rows), len(rows))]
            print(json.dumps(out, indent=2))
        else:
            print("date        " + " ".join(f"{k[:9]:>10}" for k in keys))
            for i in range(max(0, len(rows) - a.rows), len(rows)):
                print(f"{ind['date'][i]:%Y-%m-%d}  " + " ".join(f"{fnum(ind[k][i]):>10}" for k in keys))
        return 0
    return 1


def _print_fib(res: dict, as_json: bool, price: float | None = None) -> None:
    if as_json:
        print(json.dumps(res, indent=2))
        return
    t = res["trend"]
    print(f"Fibonacci ({'up-swing: low→high' if t == 'up' else 'down-swing: high→low'}) "
          f"high {res['high']:,.2f} / low {res['low']:,.2f}"
          + (f"  [swings: high {res['swing_dates']['high']}, low {res['swing_dates']['low']}]" if "swing_dates" in res else ""))
    if price is not None:
        print(f"Current {price:,.2f}: {res.get('price_position', '')}")
    print("Retracements (" + ("pullback buy zones" if t == "up" else "bounce sell zones") + "):")
    for k, v in res["retracements"].items():
        print(f"  {k:>6}  {v:,.2f}")
    print(f"Extensions (targets, measured from {res['extension_base']}):")
    for k, v in res["extensions"].items():
        print(f"  {k:>6}  {v:,.2f}")
    print("Treat each level as a zone (±1%); act only on a reaction candle there.")


if __name__ == "__main__":
    sys.exit(main())
