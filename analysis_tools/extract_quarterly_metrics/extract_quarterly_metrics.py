#!/usr/bin/env python3
"""
Extract quarterly financial metrics from NSE filing PDFs and store in DB.

Minimal set for trading bot:
- Quarter (Q1 FY27)
- Net Income (PAT) - crores
- PBT (Profit Before Tax) - crores
- Revenue - crores
- YoY Growth %
- QoQ Growth %
- Exceptional Items - crores
- Extraction Quality (high/medium/low)

Integration with analyze_result_price_volume.py:
This populates quarterly_financials table which can be JOINed with
price/volume analysis by (symbol, quarter).

Usage:
    ./.venv/bin/python analysis_tools/extract_quarterly_metrics.py \\
        --date 2026-08-11 --limit 10

    Or process a single filing:
    ./.venv/bin/python analysis_tools/extract_quarterly_metrics.py \\
        --symbol KRN --pdf-url <URL> --filing-date 2026-08-12
"""
import argparse
import json
import logging
import os
import subprocess
import sys
from datetime import datetime
from io import BytesIO
from pathlib import Path

try:
    import pdfplumber
except ImportError:
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "pdfplumber"], check=True)
    import pdfplumber

sys.path.insert(0, "/Users/ashishsaini/workspace/investment")
import db  # noqa: E402

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RESOURCES_DIR = os.path.join(BASE_DIR, "resources")

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(message)s',
    datefmt='%H:%M:%S'
)


def ensure_resources_folder(filing_date):
    """Create resources/<YYYY-MM-DD>/ folder."""
    date_folder = os.path.join(RESOURCES_DIR, filing_date)
    Path(date_folder).mkdir(parents=True, exist_ok=True)
    return date_folder


def download_pdf(url, symbol, filing_date):
    """Download PDF from NSE filing URL."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:153.0) Gecko/20100101 Firefox/153.0"
    }

    date_folder = ensure_resources_folder(filing_date)
    local_path = os.path.join(date_folder, f"{symbol}.pdf")

    if os.path.exists(local_path):
        with open(local_path, "rb") as f:
            return local_path, f.read()

    import urllib.request
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            pdf_bytes = resp.read()
        with open(local_path, "wb") as f:
            f.write(pdf_bytes)
        return local_path, pdf_bytes
    except Exception as e:
        print(f"Failed to download {url}: {e}")
        return None, None


def extract_pdf_content(pdf_input):
    """Extract financial data from PDF with aggressive text search for key metrics."""
    try:
        if isinstance(pdf_input, bytes):
            pdf_input = BytesIO(pdf_input)

        with pdfplumber.open(pdf_input) as pdf:
            pages = pdf.pages
            total_pages = len(pages)

            # Scan pages for financial statement content (aggressive - capture more)
            consolidated_pages = []
            standalone_pages = []
            financial_pages = []

            for i, page in enumerate(pages):
                text = page.extract_text() or ""
                text_lower = text.lower()

                # Look for financial statement keywords
                has_bs = "balance sheet" in text_lower
                has_pl = "profit and loss" in text_lower or "statement of profit" in text_lower or "results" in text_lower
                has_cf = "cash flow" in text_lower
                has_total = "total" in text_lower and any(word in text_lower for word in ["revenue", "assets", "income", "profit"])
                has_financial = has_bs or has_pl or has_cf or has_total

                if not has_financial:
                    continue

                financial_pages.append((i, page))

                # Further categorize by consolidated vs standalone
                if "consolidated" in text_lower:
                    consolidated_pages.append((i, page))
                elif "standalone" in text_lower:
                    standalone_pages.append((i, page))

            # Use consolidated if found, otherwise financial pages, otherwise standalone
            if consolidated_pages:
                selected_pages = consolidated_pages
            elif financial_pages:
                selected_pages = financial_pages
            else:
                selected_pages = standalone_pages

            # If still nothing found, take pages from middle of PDF (skip cover/intro)
            if not selected_pages:
                start_idx = max(1, total_pages // 4)  # Skip first quarter (cover)
                end_idx = min(total_pages, total_pages - 1)  # Take most of the document
                for i in range(start_idx, end_idx):
                    selected_pages.append((i, pages[i]))

            # Extract text + key metrics from selected pages
            if selected_pages:
                extracted_content = []
                for page_idx, page in selected_pages:
                    text = page.extract_text()
                    if text and text.strip():
                        extracted_content.append(text)

                extracted_text = "\n\n".join(extracted_content)
                pages_info = [f"Page {i+1}" for i, _ in selected_pages]
                logging.info(f"  Extracted {len(selected_pages)} pages: {', '.join(pages_info[:5])}")
                return extracted_text, True
            else:
                # Fallback: take all pages
                all_text = []
                for page in pages:
                    text = page.extract_text()
                    if text and text.strip():
                        all_text.append(text)
                return "\n\n".join(all_text), True

    except Exception as e:
        logging.error(f"Error extracting PDF: {e}")
        return None, False


def send_to_claude(extracted_text, symbol, company):
    """Send extracted content to Claude for financial metric extraction."""
    claude_bin = os.environ.get("CLAUDE_CODE_EXECPATH", "claude")
    if not os.path.exists(claude_bin):
        logging.error(f"Claude not found at: {claude_bin}")
        return None

    prompt = f"""Extract financial metrics from {company} ({symbol}) CONSOLIDATED quarterly results.

EXTRACTION RULES - TEXT SEARCH:
1. Search for these EXACT text patterns (case-insensitive) in CONSOLIDATED sections:
   - "Total Revenue" or "Revenue from operations" → FIRST LARGE NUMBER after it
   - "Profit before tax" or "(A-B)" where A=Income, B=Expenses → FIRST LARGE NUMBER
   - "Profit/(loss) after tax" or "PAT" → FIRST LARGE NUMBER
   - "Total Assets" → FIRST NUMBER
   - "Total Liabilities" → FIRST NUMBER
   - "Equity" or "Shareholders' Funds" → FIRST NUMBER

2. CRITICAL: Use CONSOLIDATED results ONLY - skip "Segment Revenue", "Segment Results"
3. All numbers in CRORES:
   - If number > 100,000 it's in LAKHS → divide by 100
   - If number < 100,000 it's already in CRORES → use as-is
4. Look for numbers next to the exact row labels (sometimes same line, sometimes next column)

EXAMPLES:
- If document shows: "Total Revenue from operations    3,71,275.88" (in lakhs) → 3712.76 crores
- If document shows: "Profit before tax                 8,640.11" (in crores) → 8640.11 crores
- If document shows: "Profit/(loss) after tax           6,585.33" (in crores) → 6585.33 crores

Return ONLY this JSON (null if truly absent):
{{
  "quarter": "Q1 FY27",
  "quarter_end_date": "2026-06-30",
  "revenue_crores": 1794.798,
  "cogs_crores": 1200.0,
  "gross_profit_crores": 594.798,
  "operating_expenses_crores": 150.0,
  "ebitda_crores": 300.0,
  "depreciation_amortization_crores": 50.0,
  "ebit_crores": 250.0,
  "finance_costs_crores": 10.0,
  "profit_before_tax_crores": 268.493,
  "tax_expense_crores": 35.0,
  "net_income_crores": 233.615,
  "exceptional_items_crores": 0.0,
  "eps_basic": 2.5,
  "eps_diluted": 2.4,
  "book_value_per_share": 25.0,
  "dividend_per_share": 1.0,
  "gross_margin_pct": 33.1,
  "operating_margin_pct": 13.9,
  "ebitda_margin_pct": 16.7,
  "net_margin_pct": 13.0,
  "total_assets_crores": 5000.0,
  "current_assets_crores": 2000.0,
  "current_liabilities_crores": 1000.0,
  "total_liabilities_crores": 2500.0,
  "equity_crores": 2500.0,
  "current_ratio": 2.0,
  "debt_to_equity_ratio": 1.0,
  "interest_coverage_ratio": 25.0,
  "roe_pct": 9.3,
  "roa_pct": 4.7,
  "operating_cf_crores": 300.0,
  "capex_crores": 100.0,
  "free_cf_crores": 200.0,
  "yoy_growth_pct": 70.5,
  "qoq_growth_pct": 3.1,
  "extraction_quality": "high"
}}

RULES:
- ALL amounts in CRORES (divide lakhs by 100)
- Margins & % in actual percent (33.1 not 0.331)
- extraction_quality: "high"=all/most found, "medium"=partial, "low"=sparse
- Return null ONLY if metric is truly absent, not if you missed it
- PREFER CONSOLIDATED over standalone, but use standalone if consolidated not found

Document:
{extracted_text}
"""
    logging.info(f"  {symbol}: Prompt size = {len(prompt)} chars")

    try:
        result = subprocess.run(
            [claude_bin, "-p", "--model", "claude-sonnet-5", "--no-session-persistence", "--allowedTools", "", "--setting-sources", "", "--strict-mcp-config"],
            input=prompt.encode(),
            capture_output=True,
            timeout=60,
            env=os.environ.copy()
        )

        if result.returncode != 0:
            logging.error(f"Claude error: {result.stderr.decode()}")
            return None

        response = result.stdout.decode()

        # Log token usage if available
        stderr_output = result.stderr.decode()
        if stderr_output and ("tokens" in stderr_output.lower() or "usage" in stderr_output.lower()):
            logging.info(f"  Claude usage: {stderr_output[:200]}")

        # Extract JSON
        try:
            if "```json" in response:
                json_str = response.split("```json")[1].split("```")[0]
            elif "{" in response:
                start = response.find("{")
                end = response.rfind("}") + 1
                json_str = response[start:end]
            else:
                json_str = response

            return json.loads(json_str)
        except json.JSONDecodeError:
            logging.error(f"Failed to parse JSON response")
            logging.error(f"Response (first 500 chars): {response[:500]}")
            return None
    except Exception as e:
        print(f"Error calling Claude: {e}")
        return None


def store_in_db(symbol, company, metrics, filing_url):
    """Store extracted metrics in quarterly_financials table."""
    if not metrics:
        return False

    try:
        conn = db.get_connection()
        cursor = conn.cursor()

        columns = [
            "symbol", "company", "quarter", "quarter_end_date",
            "revenue", "cogs", "gross_profit", "operating_expenses", "ebitda",
            "depreciation_amortization", "ebit", "finance_costs", "profit_before_tax",
            "tax_expense", "net_income", "exceptional_items",
            "eps_basic", "eps_diluted", "book_value_per_share", "dividend_per_share",
            "gross_margin_pct", "operating_margin_pct", "ebitda_margin_pct", "net_margin_pct",
            "total_assets", "current_assets", "current_liabilities", "total_liabilities", "equity",
            "current_ratio", "debt_to_equity_ratio", "interest_coverage_ratio", "roe_pct", "roa_pct",
            "operating_cf", "capex", "free_cf",
            "yoy_growth_pct", "qoq_growth_pct", "filing_url", "extraction_quality"
        ]

        values = [
            symbol, company, metrics.get("quarter"), metrics.get("quarter_end_date"),
            metrics.get("revenue_crores"), metrics.get("cogs_crores"), metrics.get("gross_profit_crores"),
            metrics.get("operating_expenses_crores"), metrics.get("ebitda_crores"),
            metrics.get("depreciation_amortization_crores"), metrics.get("ebit_crores"),
            metrics.get("finance_costs_crores"), metrics.get("profit_before_tax_crores"),
            metrics.get("tax_expense_crores"), metrics.get("net_income_crores"),
            metrics.get("exceptional_items_crores"),
            metrics.get("eps_basic"), metrics.get("eps_diluted"),
            metrics.get("book_value_per_share"), metrics.get("dividend_per_share"),
            metrics.get("gross_margin_pct"), metrics.get("operating_margin_pct"),
            metrics.get("ebitda_margin_pct"), metrics.get("net_margin_pct"),
            metrics.get("total_assets_crores"), metrics.get("current_assets_crores"),
            metrics.get("current_liabilities_crores"), metrics.get("total_liabilities_crores"),
            metrics.get("equity_crores"),
            metrics.get("current_ratio"), metrics.get("debt_to_equity_ratio"),
            metrics.get("interest_coverage_ratio"), metrics.get("roe_pct"), metrics.get("roa_pct"),
            metrics.get("operating_cf_crores"), metrics.get("capex_crores"), metrics.get("free_cf_crores"),
            metrics.get("yoy_growth_pct"), metrics.get("qoq_growth_pct"),
            filing_url, metrics.get("extraction_quality", "low")
        ]

        placeholders = ", ".join(["%s"] * len(values))
        update_clauses = ", ".join([f"{col} = EXCLUDED.{col}" for col in columns[4:]])  # Skip symbol, company, quarter, date

        # Check if critical metrics are present
        has_income = metrics.get("net_income_crores") is not None
        has_growth = metrics.get("yoy_growth_pct") is not None
        extraction_ok = has_income or has_growth  # At least one critical metric

        # If extraction is incomplete, keep failed = true
        update_failed_clause = "false" if extraction_ok else "true"

        cursor.execute(f"""
            INSERT INTO quarterly_financials ({", ".join(columns)})
            VALUES ({placeholders})
            ON CONFLICT (symbol, quarter) DO UPDATE SET
                {update_clauses},
                failed = {update_failed_clause},
                fetched_at = now()
        """, values)

        status = "✓" if extraction_ok else "⚠"
        print(f"{status} {symbol}: Q={metrics.get('quarter')}, PAT={metrics.get('net_income_crores')}Cr, YoY={metrics.get('yoy_growth_pct')}%")
        return extraction_ok
    except Exception as e:
        print(f"DB error for {symbol}: {e}")
        return False


def create_failed_stub(symbol, company, filing_date, filing_url):
    """Create a stub row marked as failed, only if no successful extraction exists."""
    try:
        conn = db.get_connection()
        cursor = conn.cursor()
        from datetime import datetime as dt
        date_obj = dt.strptime(filing_date, "%Y-%m-%d")
        quarter_date = date_obj.strftime("%Y-%m-%d")

        # Extract quarter from filing_date (rough: Aug=Q2, May=Q1, etc)
        month = date_obj.month
        if month in [4, 5, 6]:
            quarter = f"Q1 FY{date_obj.year % 100 + (1 if month >= 4 else 0)}"
        elif month in [7, 8, 9]:
            quarter = f"Q2 FY{date_obj.year % 100 + 1}"
        elif month in [10, 11, 12]:
            quarter = f"Q3 FY{date_obj.year % 100 + 1}"
        else:
            quarter = f"Q4 FY{date_obj.year % 100}"

        # Create or update stub (always, for full runs to repopulate)
        cursor.execute("""
            INSERT INTO quarterly_financials (symbol, company, quarter, quarter_end_date, filing_url, failed)
            VALUES (%s, %s, %s, %s, %s, true)
            ON CONFLICT (symbol, quarter) DO UPDATE SET
                failed = true,
                fetched_at = now()
        """, (symbol, company, quarter, quarter_date, filing_url))
    except Exception as e:
        logging.warning(f"  {symbol}: Could not create stub: {e}")


def process_filing(symbol, company, pdf_url, filing_date):
    """Download, extract, analyze, and store a single filing."""
    logging.info(f"Processing {symbol}...")

    # Create stub row marked as failed (will be updated if extraction succeeds)
    create_failed_stub(symbol, company, filing_date, pdf_url)

    local_path, pdf_bytes = download_pdf(pdf_url, symbol, filing_date)
    if not pdf_bytes:
        logging.warning(f"  {symbol}: Failed to download PDF")
        return False

    text, success = extract_pdf_content(pdf_bytes)
    if not success or not text:
        logging.warning(f"  {symbol}: No text extracted (image-only PDF?)")
        return False

    logging.info(f"  {symbol}: Extracted {len(text)} chars")
    metrics = send_to_claude(text, symbol, company)
    if metrics:
        store_in_db(symbol, company, metrics, pdf_url)
        logging.info(f"✓ {symbol}: Q={metrics.get('quarter')}, PAT={metrics.get('net_income_crores')}Cr, YoY={metrics.get('yoy_growth_pct')}%")
        return True
    logging.warning(f"  {symbol}: Failed to extract metrics from Claude")
    return False


def process_date(filing_date, limit=None, retry_failed=False):
    """Download and process all result filings for a given date."""
    logging.info(f"Processing filings for {filing_date}..." + (" (retry failed only)" if retry_failed else ""))

    # Convert YYYY-MM-DD to DD-Mon-YYYY
    from datetime import datetime as dt
    date_obj = dt.strptime(filing_date, "%Y-%m-%d")
    nse_date_format = date_obj.strftime("%d-%b-%Y")

    conn = db.get_connection()
    cursor = conn.cursor()

    if retry_failed:
        # Only get ones marked as failed
        query = """
            SELECT rc.symbol, rc.company, nf.filing_url
            FROM results_calendar rc
            LEFT JOIN nse_filings nf ON nf.id = rc.declared_filing_id
            LEFT JOIN quarterly_financials qf ON qf.symbol = rc.symbol
                AND qf.quarter = rc.quarter
            WHERE rc.declaration_date = %s
            AND nf.filing_url IS NOT NULL
            AND (qf.failed = true OR qf.id IS NULL)
            ORDER BY rc.symbol
        """
        cursor.execute(query, (filing_date,))
    else:
        query = """
            SELECT rc.symbol, rc.company, nf.filing_url
            FROM results_calendar rc
            LEFT JOIN nse_filings nf ON nf.id = rc.declared_filing_id
            WHERE rc.declaration_date = %s
            AND nf.filing_url IS NOT NULL
            ORDER BY rc.symbol
        """
        if limit:
            query += " LIMIT %s"
            cursor.execute(query, (filing_date, limit))
        else:
            cursor.execute(query, (filing_date,))

    filings = cursor.fetchall()
    logging.info(f"Found {len(filings)} filings to process")

    success = 0
    for i, (symbol, company, url) in enumerate(filings, 1):
        logging.info(f"[{i}/{len(filings)}] {symbol}")
        if process_filing(symbol, company, url, filing_date):
            success += 1

    logging.info(f"✓ Completed: {success}/{len(filings)} successfully")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="Filing date (YYYY-MM-DD) - process all filings for this date")
    parser.add_argument("--symbol", help="Single symbol to process")
    parser.add_argument("--pdf-url", help="PDF URL (required with --symbol)")
    parser.add_argument("--filing-date", help="Filing date for single symbol (YYYY-MM-DD)")
    parser.add_argument("--limit", type=int, help="Limit number of filings to process")
    parser.add_argument("--retry-failed", action="store_true", help="Only retry filings where extraction failed (net_income or yoy_growth_pct is NULL)")
    args = parser.parse_args()

    if args.symbol:
        if not args.pdf_url or not args.filing_date:
            print("Error: --pdf-url and --filing-date required with --symbol")
            sys.exit(1)
        process_filing(args.symbol, args.symbol, args.pdf_url, args.filing_date)
    elif args.date:
        process_date(args.date, args.limit, retry_failed=args.retry_failed)
    else:
        print("Error: provide --date or --symbol")
        parser.print_help()


if __name__ == "__main__":
    main()
