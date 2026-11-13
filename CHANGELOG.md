# Changelog

## 1.3.0 — 2026-11-13

Six new skills, each with a standard-library calculator and tests (35 skills, 560 tests).

- `stock-screener` (`screen.py`): CANSLIM-India, value-dividend, dividend-growth-pullback, GARP and
  quality-compounder screens on a Screener.in export, a safe `--where` filter language, near-misses,
  pledge/small-cap/negative-net-worth gates, bank and NBFC handling, and Screener.in query text.
- `macro-regime` (`macro.py`): a 6–24 month backdrop from crude, USD/INR, G-sec yields and curve,
  US 10-year, India VIX, smallcap and bank leadership, cyclicals vs defensives and equity vs gold,
  with regime labels (inflationary squeeze, easing tailwind, broadening, narrow, risk-off).
- `market-valuation` (`valuation.py`): NIFTY P/E, P/B and dividend-yield percentiles with the 2021
  consolidated-earnings break handled, yield gap vs the 10-year G-sec, market cap to GDP, and a 0–100
  froth score (valuation, retail activity, IPOs, complacency, MTF leverage) with a posture.
- `event-calendar` (`events.py`): holiday-adjusted expiries, exchange holidays, verified RBI MPC,
  FOMC, US CPI/jobs and MSCI dates, rule-based India macro releases, tax dates, results windows,
  a holding-window event-risk answer for `trading-discipline`, .ics export and a staleness check.
- `thesis-tracker` (`thesis.py`): trade-idea lifecycle with an enforced state machine, written kill
  criteria, review dates, catalysts and the LTCG countdown, post-mortems with luck-vs-skill
  quadrants, stats, and export to the trading journal. Crash-safe JSON store with a backup.
- `exhaustion-reversal` (`exhaustion.py`): parabolic-top score with upper-circuit streaks, holder
  scale-out plans, F&O-only short plans in whole lots, washout bottoms and news-reaction failures.

## 1.2.0 — 2026-11-02

Six new skills, each with a standard-library calculator and tests (29 skills, 378 tests).

- `fii-dii-flows` (`flows.py`): FII/DII cash-flow streaks and DII absorption, the FII index-futures
  long/short ratio from NSE's participant-wise OI files (format checked against NSE's archive),
  quarterly shareholding changes and pledge flags, bulk/block deal aggregation.
- `market-breadth` (`breadth.py`): % of stocks above 20/50/200 DMA, A/D line with index divergence,
  new 52-week highs/lows, McClellan oscillator and summation index, Zweig breadth thrust, uptrend
  ratio, sector participation and a 0–100 health score. Also works from an exchange A/D file.
- `momentum-scanner` (`momentum.py`): 4% momentum bursts, episodic-pivot gaps on 3× volume, a 1–99
  relative-strength rating, upper-circuit and surveillance-band flags, liquidity filter, and a
  hand-off to `breakout-planner`.
- `results-season` (`results.py`): results-day reaction session from announcement timing and the
  NSE holiday list, a 0–100 reaction score (A–D), post-results weekly drift setups, and a check for
  results inside a holding window.
- `pair-trading` (`pairs.py`): Engle–Granger cointegration with MacKinnon critical values, hedge
  ratio stability, half-life, z-score signals, and a stock-futures execution plan with whole-lot
  rounding, rollover and F&O-ban checks.
- `backtest-lab` (`backtest.py`): six built-in strategies, next-bar-open fills, gap-through stops,
  exact Indian charges per trade, parameter sweeps with plateau checks, walk-forward and Monte Carlo.

## 1.1.0 — 2026-11-01

Five new skills, each with a standard-library calculator and tests (23 skills, 200 tests).

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
- README demo: `tools/make_demo.py` renders `docs/assets/demo.svg`, an animated terminal recording
  built from real calculator output.
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
