# Example data

Every file here is **synthetic** sample data for trying the calculators. None of it is real market data.

| File | Use with |
|---|---|
| `sample_ohlcv.csv` | `ta.py summary / mtf / divergence / fib --csv` (contains a planted bullish RSI divergence) |
| `sample_option_chain.csv` | `options.py chain --spot 23050` |
| `sample_tradebook.csv` | `tax.py tradebook` (intraday, STCG, LTCG, grandfathered lot, LT loss) |
| `sample_holdings.csv` | `tax.py harvest --as-of 2027-03-10 --realized-ltcg 40000` |
| `sample_journal.csv` | `risk.py journal` |

Run from the repository root:

```bash
python3 skills/technical-analysis/scripts/ta.py summary --csv examples/sample_ohlcv.csv
python3 skills/technical-analysis/scripts/ta.py mtf --csv examples/sample_ohlcv.csv
python3 skills/fno-options-analysis/scripts/options.py chain examples/sample_option_chain.csv --spot 23050
python3 skills/indian-tax-for-traders/scripts/tax.py tradebook examples/sample_tradebook.csv --details
python3 skills/indian-tax-for-traders/scripts/tax.py harvest examples/sample_holdings.csv --as-of 2027-03-10 --realized-ltcg 40000
python3 skills/position-sizing/scripts/risk.py journal examples/sample_journal.csv
```
