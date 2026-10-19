# Analysis Tools

Analysis scripts for correlating pre-filing volume patterns with post-filing profit metrics.

## Folder Structure

```
analysis_tools/
├── analyze_result_price_volume/
│   ├── analyze_result_price_volume.py          # Price/volume patterns BEFORE filing
│   └── results_price_analysis/
│       └── 2026-08-11/
│           ├── summary_2026-08-11.csv
│           └── detailed_2026-08-11.csv
│
├── extract_quarterly_metrics/
│   ├── extract_quarterly_metrics.py             # Financial metrics AFTER filing
│   └── results/
│       └── 2026-08-11/
│
├── combine_analysis/
│   ├── combine_analysis.py                     # Combined price + metrics analysis
│   └── results/
│       └── 2026-08-11/
│           └── combined_2026-08-11.csv
│
└── README.md (this file)
```

## Scripts

### 1. `extract_quarterly_metrics.py`
Extract financial metrics from NSE filing PDFs and store in DB.

**Minimal metrics:**
- Quarter (Q1 FY27)
- Net Income (PAT) - crores
- Profit Before Tax (PBT) - crores
- Revenue - crores
- YoY Growth %
- QoQ Growth %
- Exceptional Items - crores
- Extraction Quality

**Usage:**
```bash
# Process all filings from a date
./.venv/bin/python analysis_tools/extract_quarterly_metrics/extract_quarterly_metrics.py \
    --date 2026-08-11 --limit 10

# Process single filing
./.venv/bin/python analysis_tools/extract_quarterly_metrics/extract_quarterly_metrics.py \
    --symbol KRN \
    --pdf-url https://nsearchives.nseindia.com/.../KRN.pdf \
    --filing-date 2026-08-12
```

**Output:** Populates `quarterly_financials` table in `investment_bot` DB

---

### 2. `analyze_result_price_volume.py` (existing)
Analyze price and volume patterns 1 hour BEFORE NSE result filings.

**Usage:**
```bash
./.venv/bin/python analysis_tools/analyze_result_price_volume/analyze_result_price_volume.py \
    --date 2026-08-05 [--limit 10]
```

**Output:** 
- `results_price_analysis/<date>/summary_<date>.csv` (one row per company)
- `results_price_analysis/<date>/detailed_<date>.csv` (minute-by-minute data)

---

### 3. `combine_analysis.py` (NEW)
Combine price/volume analysis with quarterly financial metrics into a single CSV.

**Columns:**
- From price analysis: symbol, company, quarter, filing_time, volume_spike, price_change_pct
- From metrics: net_income, pbt, revenue, yoy_growth_pct, qoq_growth_pct, exceptional_items, extraction_quality

**Usage:**
```bash
# Both sources (price + metrics)
./.venv/bin/python analysis_tools/combine_analysis/combine_analysis.py \
    --date 2026-08-11

# Price/volume only
./.venv/bin/python analysis_tools/combine_analysis/combine_analysis.py \
    --date 2026-08-11 --source price

# Metrics only
./.venv/bin/python analysis_tools/combine_analysis/combine_analysis.py \
    --date 2026-08-11 --source metrics
```

**Output:** `combine_analysis/results/<date>/combined_<date>.csv`

---

## Workflow Example

**Day 1: Results filed on 2026-08-11**

```bash
# Step 1: Extract financial metrics from PDFs
./.venv/bin/python analysis_tools/extract_quarterly_metrics/extract_quarterly_metrics.py \
    --date 2026-08-11

# Step 2: Analyze price/volume BEFORE filing (from tick data in TimescaleDB)
./.venv/bin/python analysis_tools/analyze_result_price_volume/analyze_result_price_volume.py \
    --date 2026-08-11

# Step 3: Combine both analyses
./.venv/bin/python analysis_tools/combine_analysis/combine_analysis.py \
    --date 2026-08-11
```

**Output:** `combine_analysis/results/2026-08-11/combined_2026-08-11.csv`

Columns:
```
symbol | company | quarter | yoy_growth_pct | volume_spike_detected | pre_filing_price_change_pct | extraction_quality
KRN    | KRN Heat Exchanger | Q1 FY27 | 45.2 | true | 2.3 | high
LYKALABS | LYKA LABS | Q1 FY27 | 28.5 | false | 0.1 | medium
```

---

## Data Flow

```
NSE Filings (PDFs)
    ↓
extract_quarterly_metrics.py
    ↓
quarterly_financials table (investment_bot DB)
    ↓
combine_analysis.py ←→ [analyze_result_price_volume.py ← TimescaleDB ticks]
    ↓
combined_<date>.csv
    ↓
Analysis: Correlation between pre-filing volume and post-filing profit growth
```

---

## Integration with Trading Bot

These metrics can be used to:

1. **Validate signals** — Check if a company with news of profit growth really had 20%+ YoY growth
2. **Detect frontrunning** — Identify if volume/price spike before filing indicates insider knowledge
3. **Refine entry/exit** — Combine fundamental metrics (profit) with technical signals (volume)
4. **Backtest** — Replay trades using actual profit metrics instead of just news

---

## Dependencies

- `pdfplumber` — Extract text from PDFs
- `claude` CLI — Analyze extracted text for financial metrics
- TimescaleDB (`port 5433`) — Tick data for price/volume analysis
- PostgreSQL (`investment_bot` DB) — Store quarterly_financials
