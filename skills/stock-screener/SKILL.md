---
name: stock-screener
description: |
  Filter and rank a universe of Indian stocks from a fundamentals CSV (a Screener.in screen or
  watchlist export, or your own snake_case file). Five built-in screens with India thresholds:
  canslim-india, value-dividend (yield bar tied to the 10-year G-sec), dividend-growth-pullback
  (RSI from price CSVs), garp and quality-compounder. Also a safe --where filter language, near-miss
  lists, a per-company rule breakdown and the matching Screener.in query text. Red-flag gates: promoter
  pledge, ₹500 cr market-cap floor, negative net worth, missing data, and bank/NBFC handling.
  screen.py works offline.

  Use when the user asks: "screen stocks for me", "find quality compounders", "CANSLIM stocks in
  India", "high dividend yield stocks that are safe", "GARP / low PEG stocks", "filter my Screener.in
  export", "ROCE > 20 and low debt", "which stocks almost qualified", "why did X fail the screen", or
  "write me a Screener.in query".
---

# Stock Screener (India)

`fundamental-screening` explains how to read one company. This skill is the calculator: it runs a
whole universe through explicit rules, shows each pass/fail with the value, and ranks what survives.
A screen produces a reading list, not a buy list.

## Commands

```bash
python3 scripts/screen.py presets                        # the five screens, rules, weights, reasons
python3 scripts/screen.py fields --csv export.csv        # how your headers mapped; field names
python3 scripts/screen.py run --csv export.csv --preset quality-compounder
python3 scripts/screen.py run --csv export.csv --where "roce>20" --where "pe<25" --where "pledge<=5" --sort roce --top 20
python3 scripts/screen.py run --csv export.csv --preset dividend-growth-pullback --prices-dir data/
python3 scripts/screen.py explain "Infosys" --csv export.csv --preset garp
python3 scripts/screen.py query --preset value-dividend --gsec 7.15
```
Common flags: `--json` (strict JSON, missing values are `null`), `--gsec` (current 10-year G-sec
yield, default 7.0), `--min-mcap` (₹ crore, 500), `--max-pledge` (%, 20), `--no-gates`,
`--map "field=Header"` (repeatable), `--financials auto|none|NAME,NAME`. A preset and `--where` can
be combined. Bad input exits 2 with a one-line `error:`.

## Getting the CSV out of Screener.in

1. Run a screen (or open a watchlist) on screener.in. Use **Edit Columns** to add what the preset
   needs; `screen.py presets` and the "NOT CHECKED" notes tell you which.
2. Click **Export** and save as CSV (if you get an .xlsx, open it and save the sheet as CSV).
3. Add an `Industry` column if you can: it is how banks and NBFCs are recognised.

Headers are matched by keyword, so units, `%`, commas (`1,23,456.70`), `₹`, blanks, `-` and `NA`
are fine. **Verified** on public Screener.in screen tables (Nov 2026): `S.No.`, `Company`, `CMP Rs.`,
`P/E`, `Mar Cap Rs.Cr.`, `Div Yld %`, `NP Qtr Rs.Cr.`, `Qtr Profit Var %`, `Sales Qtr Rs.Cr.`,
`Qtr Sales Var %`, `ROCE %`, `ROE %`, `Debt / Eq`, `CMP / BV`. **Assumed** (column names vary with
what you add): EPS, 3/5-year growth, promoter/pledge/FII/DII holding and change, PEG, payout,
dividend growth, free cash flow, 52-week high, returns. We have only seen the web tables, not an
exported file, so if a header is not recognised, `fields --csv` lists it as unmapped. Fix it with
`--map pledge="Pledged %"`. Snake_case files work too: `name,symbol,industry,price,market_cap_cr,pe,
pb,dividend_yield,payout_ratio,roce,roe,debt_to_equity,profit_growth_3y,promoter_holding,pledge_pct`.

## Gates (every run unless `--no-gates`)

| Gate | Default | Why |
|---|---|---|
| Market cap | ≥ ₹500 cr | Below this: SME boards, thin floats, operator stocks, ASM/GSM lists |
| Promoter pledge | ≤ 20% | Red-flag line in `fundamental-screening`; 10–20% is flagged `pledge` |
| Net worth | not negative | Negative book value, P/B or D/E means losses have eaten the equity |
| Missing data | any rule's value blank | Listed as INCOMPLETE, never silently passed (blank P/E usually = loss) |

**Banks, NBFCs, insurers** (from the Industry column, otherwise guessed from the name) skip the
debt, ROCE, free-cash-flow, interest-cover and margin rules in presets, and are tagged `FIN`. Judge
them on ROA (> 1.2%), GNPA/NNPA, NIM and capital adequacy with `fundamental-screening`. Your own
`--where` rules apply to them unchanged. Soft flags: `small-cap` (< ₹2,000 cr) and `low-base?`
(quarterly profit up 200%+).

## Presets (how each is adapted for India)

- **canslim-india**: C = quarterly profit ≥ 25% YoY with sales ≥ 15% (no free consensus
  estimates in India, so reported growth stands in for a surprise). A = 3-year profit CAGR ≥ 20% and
  ROE ≥ 15%. N = within 15% of the 52-week high. L = 1-year return in the top 30% *of your file*, so
  export NIFTY 500 or wider. I = FII + DII holding rose last quarter (`fii-dii-flows` for detail). M is
  not in a fundamentals file: run `../market-regime/scripts/regime.py status` (`market-regime`) first.
- **value-dividend**: P/E ≤ 20 (NIFTY's long-run average is about 20–22), P/B ≤ 3, D/E ≤ 0.5, payout ≤
  75%, 3-year profit growth ≥ 5% (no yield traps), ROE ≥ 12%. Yield ≥ 40% of the 10-year G-sec (2.8%
  at 7%, about twice NIFTY's yield). Pass `--gsec` with today's yield. Dividends are taxed at your slab
  rate, with 10% TDS once a company pays you more than ₹10,000 in a financial year (s.194, from
  FY 2025-26). In the 30% slab a 3% yield nets about 2.1% (`indian-tax-for-traders`).
- **dividend-growth-pullback**: yield ≥ 1.5%, dividend growth ≥ 10%, profit growth ≥ 12%, payout ≤ 60%,
  ROE ≥ 15%, D/E ≤ 0.5, and RSI(14) ≤ 40 from `--prices-dir` (daily CSVs named after the symbol or
  company, read with `../technical-analysis/scripts/ta.py`) or an RSI column. Without either, the
  pullback rule is reported as not checked.
- **garp**: trailing PEG between 0 and 1.5 (P/E ÷ 3-year profit growth if no PEG column), profit
  growth ≥ 15%, sales growth ≥ 10%, ROCE ≥ 18%, ROE ≥ 15%, P/E ≤ 40.
- **quality-compounder**: ROCE ≥ 20%, ROE ≥ 18%, D/E ≤ 0.3, sales growth ≥ 10%, profit growth ≥ 12% over
  both 3 and 5 years, promoter ≥ 40%, pledge ≤ 1%, positive free cash flow. Board-run companies with no
  promoter fail the promoter rule by design.

**Score**: each rule scores 50 at its threshold and 100 at its "best" value (linear, capped), and
the composite is the weighted average. Weights are in `presets`. **Near misses** failed exactly one
rule and show which, with the value.

## Filters and Screener.in queries

`--where` takes `FIELD OP NUMBER` only (`> >= < <= == !=`; `and` joins several). It is parsed by a
regular expression and never evaluated, so `__import__(...)`, function calls and field-vs-field
comparisons are rejected. `query` prints the same rules as Screener.in query text, e.g.
`Return on capital employed > 20 AND Debt to equity < 0.3 AND Pledged percentage < 1`. Query
names seen in public Screener.in screens are marked verified (`fields`). `Return over 1year` was
not found and is flagged unverified. Dividend growth has no known field and is left out. `≥` is
written as `>`.

## Hand-offs

1. Read each passer's filings, governance and red flags with `fundamental-screening`. Check results
   timing with `results-season`.
2. Time entries with `breakout-planner` (base and pivot) or `momentum-scanner` (RS and bursts).
3. Before buying, check fit and concentration with `portfolio-review`.

Limits: the file is a snapshot from the export date, and stale columns give stale answers. RS is
relative to your file only. Thresholds are judgement calls you can override. A screen cannot see
related-party deals, auditor changes or surveillance status.

_Educational analysis, not investment advice._
