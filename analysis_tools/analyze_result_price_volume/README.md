# Result Price & Volume Analysis Tool

Analyzes NSE result declarations to understand price and volume patterns before and after the official filing timestamp.

## Usage

```bash
./.venv/bin/python analysis_tools/analyze_result_price_volume/analyze_result_price_volume.py \
  --date 2026-08-10 \
  --limit 50
```

**Parameters:**
- `--date` (required): Date in YYYY-MM-DD format (declaration date)
- `--limit` (optional): Maximum number of companies to analyze (default: all)

## Output Files

Three CSV files are generated in `results/<date>/`:

### 1. `summary_<date>.csv`

Overview of price action for each company.

**Columns:**
- `symbol`: Stock ticker
- `nse_filing_time`: When NSE officially filed the result
- `price_60min_before`: Price % gain 60 minutes before filing
- `price_30min_before`: Price % gain 30 minutes before filing
- `price_at_filing`: Price % gain at filing time
- `max_gain_pct`: Maximum % gain (across full 60 min before + 60 min after)
- `avg_volume`: Average trades per minute
- `num_candles`: Number of 1-minute bars in dataset

### 2. `detailed_<date>.csv`

Minute-by-minute price and volume data for each company.

**Columns:**
- `symbol`: Stock ticker
- `nse_filing_time`: NSE filing timestamp
- `minute_timestamp`: Time of this minute bar
- `minutes_before_nse`: Offset from filing (negative = before, positive = after)
- `open`, `high`, `low`, `close`: OHLCV price data
- `pct_gain`: % gain vs previous close
- `volume`: Number of trades in this minute

### 3. `gain_timing_<date>.csv`

**When did the maximum gains occur relative to the NSE filing?**

**Format Note:** Many values use dual-baseline format: `close_baseline(open_baseline)`
- First number: Gain from **close of 9:16** candle
- In brackets: Gain from **open of 9:16** candle
- Market opens at 9:15; first tracked candle is 9:16

**Columns:**
- `symbol`: Stock ticker
- `nse_filing_time`: When NSE officially filed the result

**60-MINUTE WINDOW (±60 min from filing):**
- **BEFORE FILING (60 min before):**
  - `max_gain_before_pct`: Maximum % gain in the 60 min BEFORE filing
    - **Format**: Single value if filing > 10:16 AM, Dual format `close(open)` if filing ≤ 10:16 AM
    - **Why**: If filing within 60 min of 9:16 open, baseline might be 9:16 open/close prices
  - `min_before_filing_when_max`: How many minutes BEFORE filing did this peak occur
  - `time_of_max_before`: Exact timestamp of the peak
- **AFTER FILING (60 min after):**
  - `max_gain_after_pct`: Maximum % gain in the 60 min AFTER filing (vs. price AT filing time)
  - `min_after_filing_when_max`: How many minutes AFTER filing did this peak occur
  - `time_of_max_after`: Exact timestamp of the peak

**WHOLE TRADING DAY:**
- **BEFORE FILING (entire day until filing):**
  - `max_gain_whole_day_before_pct`: Maximum % gain from 9:16 open until filing - **Format**: `close(open)`
  - `min_before_filing_when_max_whole_day`: How many minutes BEFORE filing did this peak occur
  - `time_of_max_whole_day_before`: Exact timestamp of the peak
- **AFTER FILING (entire day after filing):**
  - `max_gain_whole_day_after_pct`: Maximum % gain from filing until market close - **Format**: `close(open)`
  - `min_after_filing_when_max_whole_day`: How many minutes AFTER filing did this peak occur
  - `time_of_max_whole_day_after`: Exact timestamp of the peak

**THROUGH ENTIRE DAY:**
- `max_gain_through_day_pct`: Absolute peak gain from 9:16 open to any point that day - **Format**: `close(open)`
- `when_max_through_day`: Timestamp of when that peak was reached

## Data Source

- **NSE filings**: `investment_bot` database (PostgreSQL, port 5432)
- **Tick data**: `investment_features` database (TimescaleDB, port 5433)
- **Price aggregation**: 1-minute OHLCV bars derived from tick-level data

## Example Insights

From `gain_timing_<date>.csv`, you can see patterns like:

**60-Minute Window Around Filing (filing times > 10:16 AM):**

| Symbol | Before (60m) | When | After (60m) | When |
|--------|-------------|------|-----------|------|
| GARUDA | 3.38% | 1 min before | 3.06% | 14 min after |
| RAMCOIND | 4.48% | At filing | 3.88% | 46 min after |

**Whole Trading Day (9:16 AM open to market close):**

| Symbol | Before Filing | When | After Filing | When |
|--------|--------------|------|--------------|------|
| GARUDA | 3.68(4.84)% | 1 min before | 3.06(3.06)% | 14 min after |
| RAMCOIND | 7.55(6.48)% | At filing | 7.49(7.49)% | 146 min after (2:41 PM) |

**Absolute Peak (Through Entire Day):**

| Symbol | Peak Gain | When |
|--------|-----------|------|
| GARUDA | 6.85(8.05)% | 10:55 AM (+14 min) |
| RAMCOIND | 15.60(14.46)% | 2:41 PM (+146 min) |

**Key Insights:**
- Format `close(open)`: Shows sensitivity to whether stock opened higher or lower than close of 9:15
- RAMCOIND shows strong afternoon rally (+15.6% from 9:16 close), suggesting market kept digesting results throughout the day
- GARUDA peaks relatively quickly after filing (+14 min), then consolidates

## Hypothesis

**Stocks jump 2-3 minutes before NSE announces** because:
1. Company/press release announces results
2. Market reacts immediately → price jumps
3. NSE's official filing timestamp comes later → documentation of what already happened

This tool quantifies that pattern with actual tick data.
