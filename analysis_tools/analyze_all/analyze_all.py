#!/usr/bin/env python3
"""
Master analysis script: Analyze price/volume patterns and financial metrics
for result declarations, with options to populate on demand.

Single entry point for:
1. Extract quarterly metrics from PDFs (extract_quarterly_metrics.py)
2. Analyze price/volume before filings (analyze_result_price_volume.py)
3. Combine both analyses into CSV (optional)

Usage:
    # Extract financial results only
    ./.venv/bin/python analysis_tools/analyze_all.py \\
        --date 2026-08-11 --analyze results

    # Analyze price/volume only
    ./.venv/bin/python analysis_tools/analyze_all.py \\
        --date 2026-08-11 --analyze price

    # Both analyses
    ./.venv/bin/python analysis_tools/analyze_all.py \\
        --date 2026-08-11 --analyze both

    # Both + combine to CSV
    ./.venv/bin/python analysis_tools/analyze_all.py \\
        --date 2026-08-11 --analyze both --combine

    # With limit
    ./.venv/bin/python analysis_tools/analyze_all.py \\
        --date 2026-08-11 --analyze both --limit 10

Output:
    - Metrics: quarterly_financials table (investment_bot DB)
    - Price: analysis_tools/analyze_result_price_volume/results/<date>/
    - Combined: analysis_tools/combine_analysis/results/<date>/combined_<date>.csv
"""
import argparse
import os
import subprocess
import sys
from datetime import datetime

sys.path.insert(0, "/Users/ashishsaini/workspace/investment")
import db  # noqa: E402

ANALYZE_ALL_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(os.path.dirname(ANALYZE_ALL_DIR))  # project root


def run_extract_metrics(date_str, limit=None, retry_failed=False):
    """Run extract_quarterly_metrics.py"""
    print(f"\n{'='*80}")
    print(f"[{datetime.now():%H:%M:%S}] STEP 1: Extract Quarterly Metrics from PDFs")
    print(f"{'='*80}")

    cmd = [
        sys.executable,
        os.path.join(BASE_DIR, "analysis_tools", "extract_quarterly_metrics", "extract_quarterly_metrics.py"),
        "--date", date_str,
    ]
    if limit:
        cmd.extend(["--limit", str(limit)])
    if retry_failed:
        cmd.append("--retry-failed")

    result = subprocess.run(cmd)
    return result.returncode == 0


def run_price_volume(date_str, limit=None):
    """Run analyze_result_price_volume.py"""
    print(f"\n{'='*80}")
    print(f"[{datetime.now():%H:%M:%S}] STEP 2: Analyze Price/Volume Patterns")
    print(f"{'='*80}")

    cmd = [
        sys.executable,
        os.path.join(BASE_DIR, "analysis_tools", "analyze_result_price_volume", "analyze_result_price_volume.py"),
        "--date", date_str,
    ]
    if limit:
        cmd.extend(["--limit", str(limit)])

    result = subprocess.run(cmd)
    return result.returncode == 0


def run_combine(date_str, source="both"):
    """Run combine_analysis.py"""
    print(f"\n{'='*80}")
    print(f"[{datetime.now():%H:%M:%S}] STEP 3: Combine Analyses")
    print(f"{'='*80}")

    cmd = [
        sys.executable,
        os.path.join(BASE_DIR, "analysis_tools", "combine_analysis", "combine_analysis.py"),
        "--date", date_str,
        "--source", source,
    ]

    result = subprocess.run(cmd)
    return result.returncode == 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", required=True, help="Date (YYYY-MM-DD)")
    parser.add_argument(
        "--analyze",
        choices=["price", "results", "both"],
        default="both",
        help="What to analyze: price (volume only), results (financial only), or both",
    )
    parser.add_argument(
        "--combine",
        action="store_true",
        help="Combine analyses into CSV after completion",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Limit number of companies to process",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Only retry filings where extraction failed",
    )
    args = parser.parse_args()

    print(f"\n{'='*80}")
    print(f"ANALYSIS PIPELINE: {args.date}")
    print(f"Mode: {args.analyze} | Combine: {args.combine} | Limit: {args.limit or 'None'} | Retry Failed: {args.retry_failed}")
    print(f"{'='*80}")

    success_count = 0
    total_steps = 0

    # Step 1: Extract Results
    if args.analyze in ["results", "both"]:
        total_steps += 1
        if run_extract_metrics(args.date, args.limit, retry_failed=args.retry_failed):
            success_count += 1
        else:
            print("⚠ Extract results step failed")

    # Step 2: Analyze Price/Volume
    if args.analyze in ["price", "both"]:
        total_steps += 1
        if run_price_volume(args.date, args.limit):
            success_count += 1
        else:
            print("⚠ Price/volume analysis step failed")

    # Step 3: Combine (optional, only if requested and both data sources are ready)
    if args.combine and args.analyze == "both":
        total_steps += 1
        if run_combine(args.date, "both"):
            success_count += 1
        else:
            print("⚠ Combine step failed")
    elif args.combine and args.analyze in ["price", "results"]:
        print(f"\n⚠ Warning: --combine requires both analyses. Skipping combine step.")

    # Summary
    print(f"\n{'='*80}")
    print(f"[{datetime.now():%H:%M:%S}] ANALYSIS COMPLETE")
    print(f"{'='*80}")
    print(f"Completed: {success_count}/{total_steps} steps")

    if success_count == total_steps:
        print("✓ All steps completed successfully")

        # Show output locations
        print(f"\nOutput locations:")
        if args.analyze in ["results", "both"]:
            print(f"  Results: quarterly_financials table (investment_bot DB)")
        if args.analyze in ["price", "both"]:
            print(f"  Price:   analysis_tools/analyze_result_price_volume/results/{args.date}/")
        if args.combine and args.analyze == "both":
            print(f"  Combined: analysis_tools/combine_analysis/results/{args.date}/combined_{args.date}.csv")

        return 0
    else:
        print("✗ Some steps failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
