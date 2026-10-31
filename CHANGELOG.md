# Changelog

## 1.1.0 — 2026-11-01

Five new skills, each with a standard-library calculator and tests (23 skills, 198 tests).

- `market-regime` (`regime.py`): NIFTY/BANKNIFTY/SENSEX distribution days, follow-through days with
  an India-calibrated threshold, top-risk score, exposure ceiling. Falls back to NIFTYBEES/BANKBEES
  volume, or a price-only mode, because Yahoo index volume is unreliable.
- `sector-rotation` (`sectors.py`): NSE sectoral indices vs NIFTY with RRG quadrants, risk-on vs
  defensive tilt, midcap/smallcap size read, member breadth. Works offline from NSE index CSVs.
- `breakout-planner` (`breakout.py`): Stage 2 trend template, VCP and flat-base detection, pivot,
  GTT breakout plan with NSE tick sizes, circuit-band and liquidity checks, multi-stock scan.
- `trading-discipline` (`discipline.py`): daily/weekly/monthly loss circuit breaker, losing-streak
  cooldown, pre-trade gate with India checks (F&O ban, ASM/GSM, expiry day, auto square-off),
  performance review by setup and weekday, decision log.
- `portfolio-review` (`portfolio.py`): concentration, sector, group and market-cap limits, LTCG
  countdown, beta/volatility/drawdown, correlation clusters, portfolio heat, tax-aware rebalance.
  Reads Zerodha Console XLSX exports.
- `ta.py`: added NIFTYMIDCAP150 and NIFTYSMALLCAP250 tickers; noted that Yahoo has no history for
  NIFTY MEDIA and MIDCAP 100.

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
