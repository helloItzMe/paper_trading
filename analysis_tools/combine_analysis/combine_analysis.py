#!/usr/bin/env python3
"""
Combine price/volume analysis with quarterly financial metrics.

Creates a single CSV with columns from both:
- analyze_result_price_volume.py (price gains, volume patterns before filing)
- extract_quarterly_metrics.py (profit growth, revenue, margins after filing)

This allows correlation analysis: Did pre-filing volume spike correlate with
post-filing profit growth?

Usage:
    # Both sources (price + metrics)
    ./.venv/bin/python analysis_tools/combine_analysis/combine_analysis.py \\
        --date 2026-08-11

    # Price/volume only
    ./.venv/bin/python analysis_tools/combine_analysis/combine_analysis.py \\
        --date 2026-08-11 --source price

    # Metrics only
    ./.venv/bin/python analysis_tools/combine_analysis/combine_analysis.py \\
        --date 2026-08-11 --source metrics

Output:
    analysis_tools/combine_analysis/results/<date>/combined_<date>.csv
"""
import argparse
import csv
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, "/Users/ashishsaini/workspace/investment")
import db  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RESULTS_DIR = os.path.join(os.path.dirname(__file__), "results")


def ensure_results_folder(date_str):
    """Create results/<YYYY-MM-DD>/ folder."""
    date_folder = os.path.join(RESULTS_DIR, date_str)
    Path(date_folder).mkdir(parents=True, exist_ok=True)
    return date_folder


def get_price_volume_data(date_str):
    """Get price/volume analysis from results_calendar and ticks."""
    conn = db.get_connection()
    cursor = conn.cursor()

    # Get result declarations for the date
    query = """
    SELECT
        symbol,
        company,
        quarter,
        declaration_date,
        best_declared_at as filing_time
    FROM results_calendar
    WHERE declaration_date = %s
    ORDER BY symbol
    """
    cursor.execute(query, (date_str,))
    results = cursor.fetchall()

    data = {}
    for symbol, company, quarter, decl_date, filing_time in results:
        data[symbol] = {
            "symbol": symbol,
            "company": company,
            "quarter": quarter,
            "declaration_date": decl_date,
            "filing_time": filing_time.isoformat() if filing_time else None,
            # Price/volume fields will be empty unless we have TimescaleDB data
            "pre_filing_volume_avg": None,
            "pre_filing_price_change_pct": None,
            "volume_spike_detected": None,
        }

    return data


def get_quarterly_metrics(date_str):
    """Get quarterly financial metrics for companies that filed on this date."""
    conn = db.get_connection()
    cursor = conn.cursor()

    query = """
    SELECT
        symbol,
        company,
        quarter,
        net_income,
        profit_before_tax,
        revenue,
        yoy_growth_pct,
        qoq_growth_pct,
        exceptional_items,
        extraction_quality,
        filing_url
    FROM quarterly_financials
    WHERE DATE(fetched_at AT TIME ZONE 'Asia/Kolkata') = %s
    ORDER BY symbol
    """
    cursor.execute(query, (date_str,))
    results = cursor.fetchall()

    data = {}
    for (
        symbol, company, quarter, net_income, pbt, revenue,
        yoy_pct, qoq_pct, exceptional, quality, url
    ) in results:
        data[symbol] = {
            "symbol": symbol,
            "company": company,
            "quarter": quarter,
            "net_income_crores": net_income,
            "profit_before_tax_crores": pbt,
            "revenue_crores": revenue,
            "yoy_growth_pct": yoy_pct,
            "qoq_growth_pct": qoq_pct,
            "exceptional_items_crores": exceptional,
            "extraction_quality": quality,
            "filing_url": url,
        }

    return data


def combine_data(price_data, metrics_data):
    """Combine price/volume and metrics data by symbol."""
    all_symbols = set(price_data.keys()) | set(metrics_data.keys())
    combined = []

    for symbol in sorted(all_symbols):
        row = {}

        # Add price/volume data
        if symbol in price_data:
            row.update(price_data[symbol])
        else:
            row["symbol"] = symbol

        # Add metrics data
        if symbol in metrics_data:
            row.update(metrics_data[symbol])

        combined.append(row)

    return combined


def export_to_csv(data, date_str, output_path):
    """Export combined data to CSV."""
    if not data:
        print("No data to export")
        return

    # Define column order
    columns = [
        "symbol",
        "company",
        "quarter",
        "declaration_date",
        "filing_time",
        "pre_filing_volume_avg",
        "pre_filing_price_change_pct",
        "volume_spike_detected",
        "net_income_crores",
        "profit_before_tax_crores",
        "revenue_crores",
        "yoy_growth_pct",
        "qoq_growth_pct",
        "exceptional_items_crores",
        "extraction_quality",
        "filing_url",
    ]

    # Add any extra columns from data that aren't in columns list
    for row in data:
        for key in row.keys():
            if key not in columns:
                columns.append(key)

    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(data)

    print(f"✓ Exported {len(data)} rows to {output_path}")
    return output_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, help="Date (YYYY-MM-DD)")
    parser.add_argument(
        "--source",
        choices=["price", "metrics", "both"],
        default="both",
        help="Data source: price (volume), metrics (profit), or both",
    )
    args = parser.parse_args()

    results_folder = ensure_results_folder(args.date)
    output_path = os.path.join(results_folder, f"combined_{args.date}.csv")

    print(f"\n[{datetime.now():%H:%M:%S}] Combining analysis for {args.date}...")
    print(f"Source: {args.source}")

    combined_data = []

    if args.source in ["price", "both"]:
        print("  Fetching price/volume data...")
        price_data = get_price_volume_data(args.date)
        print(f"    Found {len(price_data)} companies with price data")
        combined_data.append(price_data)

    if args.source in ["metrics", "both"]:
        print("  Fetching quarterly metrics...")
        metrics_data = get_quarterly_metrics(args.date)
        print(f"    Found {len(metrics_data)} companies with metrics")
        combined_data.append(metrics_data)

    if len(combined_data) == 2:
        final_data = combine_data(combined_data[0], combined_data[1])
    elif len(combined_data) == 1:
        final_data = list(combined_data[0].values())
    else:
        print("No data found")
        return

    export_to_csv(final_data, args.date, output_path)
    print(f"\nOutput saved to: {output_path}")


if __name__ == "__main__":
    main()
