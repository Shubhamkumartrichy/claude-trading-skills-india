# Contributing

Thanks for helping Indian traders get correct answers. The most valuable contributions are, in order:

1. **Rate and rule updates** with an official source (they go stale every few months)
2. **Bug reports with a concrete wrong number** (and the contract note or circular that proves it)
3. **New broker profiles** for the charges calculator
4. **New skills** from the [roadmap](README.md#roadmap)

## Repository layout

```
skills/<skill-name>/SKILL.md        instructions Claude loads (frontmatter: name, description)
skills/<skill-name>/scripts/*.py    standard-library calculators that skill (and siblings) run
skills/<skill-name>/references/     long reference material loaded on demand
tests/                              unittest suite + fixtures (run before every PR)
tools/check_freshness.py            monthly staleness check used by CI
examples/                           synthetic sample data for trying the scripts
docs/assets/                        README images
```

## Updating rates and rules

| What | File | Constant |
|---|---|---|
| STT, exchange, SEBI, stamp, GST | `skills/trading-costs/scripts/charges.py` | `STT`, `EXCHANGE_TXN`, `SEBI_FEE`, `STAMP`, `GST` |
| Broker brokerage, DP, MTF | `charges.py` | `BROKERS` |
| Lot sizes | `skills/fno-options-analysis/scripts/options.py` | `LOT_SIZES` |
| Holidays | `options.py` + `skills/indian-market-structure/references/holidays-2026.md` | `HOLIDAYS`, `HOLIDAYS_KNOWN_UNTIL` |
| Expiry weekdays | `options.py` | `EXPIRY_RULES` |
| Tax slabs and rates | `skills/indian-tax-for-traders/scripts/tax.py` | `SLABS`, `STCG_RATE`, `LTCG_RATE`, `LTCG_EXEMPTION`, `REBATE_87A`, `SUPPORTED_YEARS` |

Then:

1. Bump the as-of constant (`RATES_AS_OF` in `charges.py` or `DATA_AS_OF` in `options.py`).
2. Update the matching table in the relevant `SKILL.md` and the "India facts" table in `README.md`.
3. Fix or add the test that pins the value, and run `python3 -m unittest discover -s tests`.
4. Put the official source link in the PR description.

Please don't use blogs or aggregator sites as the only source for a rate. Exchange circulars
(nseindia.com, bseindia.com), SEBI and CBDT notifications, and the broker's own pricing page are
the sources that count.

## Writing or changing a skill

- `name` in the frontmatter must equal the folder name. `description` must be 100–1024 characters
  and say **what** the skill does and **when** to use it, with example phrasings. Claude decides
  when to load a skill from that description.
- Write instructions for Claude: imperative, concrete, India-specific. Show commands that run, and
  output templates.
- Reference scripts relative to the skill folder (`scripts/x.py`, or `../other-skill/scripts/x.py`).
  `tests/test_skills.py` checks that every referenced path exists.
- Use ₹ with Indian digit grouping (₹1,25,000), lakh/crore, IST, and NSE symbols.
- Say when facts were verified, and point to the official source for anything that changes.
- No stock tips, no target prices for named stocks, no referral links.

## Scripts

- **Standard library only**, Python 3.10+. Optional imports (like yfinance) must fail with a clear
  message.
- Every function that produces a number gets a test with a hand-checked value.
- Keep CLIs friendly: `--json` for machine output, human-readable tables by default.

## Running tests

```bash
python3 -m unittest discover -s tests -v
python3 tools/check_freshness.py
```

## Commit and PR style

Small, focused PRs with a clear title ("Update NSE transaction charges from 1 Jan 2027").
Fill in the PR template checklist.
