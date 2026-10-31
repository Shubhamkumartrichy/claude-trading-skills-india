# Changelog

## 1.0.0 — 2026-10-31

First public release.

- 18 skills: orchestrator, market pulse, technical analysis, multi-timeframe, RSI divergence,
  Fibonacci, position sizing, stop-loss, trailing stops, risk-reward, F&O analysis, options
  strategies, trading costs, Indian tax, market structure, broker integration, fundamental
  screening, IPO analysis.
- 5 standard-library calculators: `charges.py`, `tax.py`, `options.py`, `ta.py`, `risk.py`.
- Rates verified on 31 Oct 2026: F&O STT from 1 Apr 2026, NSE transaction charges from 1 Mar 2026,
  index lot sizes from the Jan 2026 series, the 2026 holiday calendar including the 15 Jan closure.
- Official broker MCP setup for Groww, Zerodha Kite, Dhan, Upstox, Fyers and Kotak Neo, with an
  order-confirmation protocol.
- 80 unit tests (including regression tests from an independent review), CI on Python 3.10–3.14,
  and a monthly data-freshness check.
