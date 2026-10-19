#!/usr/bin/env python3
"""
Analyze price and volume patterns 1 hour BEFORE NSE result filings.

For a given date, this script:
1. Finds all NSE result filings (from investment_bot DB)
2. Gets minute-level price/volume data (from investment_features DB)
3. Shows price% gain and volume for each minute in the 60-min window before filing
4. Identifies if there's a detectable pattern of early price/volume increase
5. Saves results to CSV files

Usage:
    ./.venv/bin/python analyze_result_price_volume.py --date 2026-08-05 [--limit 10]

Output:
    Saves to analysis_tools/results_price_analysis/<date>/
    - summary_<date>.csv (one row per company)
    - detailed_<date>.csv (minute-by-minute data)
"""
import sys
import argparse
from datetime import datetime, timedelta, timezone
import psycopg2
import psycopg2.extras
import os
import csv
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))

def get_results_declarations(date_str):
    """Get all result declarations for a given date from results_calendar."""
    conn = psycopg2.connect(
        host="localhost",
        database="investment_bot",
        user=os.getenv("USER")
    )
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    query = """
    SELECT
        symbol,
        best_declared_at as nse_filing_time,
        earliest_news_at,
        quarter,
        company
    FROM results_calendar
    WHERE declaration_date = %s
        AND best_declared_at IS NOT NULL
    ORDER BY best_declared_at;
    """

    cur.execute(query, (date_str,))
    results = cur.fetchall()
    conn.close()

    return results


def get_ltp_at_timestamp(symbol, date_str, target_time):
    """Get exact LTP from tick data at or just before target timestamp (within 5 min)."""
    conn = psycopg2.connect(
        host="localhost",
        port=5433,  # TimescaleDB is on 5433
        database="investment_features",
        user=os.getenv("USER")
    )
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    # Get last tick at or before target_time, within 5 minutes
    query = """
    SELECT ltp
    FROM ticks
    WHERE symbol = %s
        AND received_at >= %s - interval '5 minutes'
        AND received_at <= %s
    ORDER BY received_at DESC
    LIMIT 1;
    """

    try:
        cur.execute(query, (symbol, target_time, target_time))
        result = cur.fetchone()
        ltp = result['ltp'] if result else None
    except psycopg2.Error as e:
        print(f"    [error] ltp query failed: {e}", file=sys.stderr)
        ltp = None

    conn.close()
    return ltp


def get_minute_data(symbol, date_str, start_time, end_time):
    """Get minute-level OHLCV data from investment_features DB (TimescaleDB, port 5433)."""
    conn = psycopg2.connect(
        host="localhost",
        port=5433,  # TimescaleDB is on 5433
        database="investment_features",
        user=os.getenv("USER")
    )
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    # Query the ticks table and aggregate to minute bars
    query = """
    SELECT
        time_bucket('1 minute', received_at) AS minute_ts,
        symbol,
        MIN(ltp) AS low,
        MAX(ltp) AS high,
        FIRST(ltp, received_at) AS open,
        LAST(ltp, received_at) AS close,
        COUNT(*) AS volume
    FROM ticks
    WHERE symbol = %s
        AND received_at::date = %s::date
        AND received_at >= %s
        AND received_at <= %s
    GROUP BY time_bucket('1 minute', received_at), symbol
    ORDER BY minute_ts;
    """

    try:
        cur.execute(query, (symbol, date_str, start_time, end_time))
        data = cur.fetchall()
    except psycopg2.Error as e:
        print(f"    [error] ticks query failed: {e}", file=sys.stderr)
        data = []

    conn.close()
    return data


def analyze_filing(symbol, nse_time, minute_data, date_str):
    """
    Analyze price/volume for entire trading day, with focus on before/after filing.
    Uses 9:16 as first candle (skip 9:15).
    Uses exact LTP at filing timestamp from tick data.
    Returns dict with timing and pattern info.
    """
    if not minute_data:
        return None

    # Filter to skip 9:15 candle (start from 9:16)
    market_start_time = datetime.combine(
        minute_data[0]['minute_ts'].date(),
        datetime.min.time()
    ).replace(hour=9, minute=16, tzinfo=IST)

    minute_data_from_916 = [c for c in minute_data if c['minute_ts'] >= market_start_time]
    if not minute_data_from_916:
        return None

    # Find key price points from first candle (9:16)
    first_candle = minute_data_from_916[0]
    day_start_price_close = first_candle['close'] if first_candle['close'] else first_candle['open']
    day_start_price_open = first_candle['open'] if first_candle['open'] else first_candle['close']

    # Get exact LTP at filing time from tick data (much more precise than minute candle close)
    price_at_filing = get_ltp_at_timestamp(symbol, date_str, nse_time)
    if symbol == 'DVL':
        print(f"[DEBUG DVL] filing={nse_time}, price_from_tick={price_at_filing}", file=sys.stderr)

    # Fallback to minute candle close if tick data unavailable
    if price_at_filing is None:
        for candle in minute_data_from_916:
            ts = candle['minute_ts']
            if ts <= nse_time:
                price_at_filing = candle['close'] if candle['close'] else candle['open']

    # Final fallback to day start
    if price_at_filing is None:
        price_at_filing = day_start_price_close

    # Calculate metrics
    analysis = {
        'symbol': symbol,
        'nse_time': nse_time,
        'day_start_price_close': day_start_price_close,
        'day_start_price_open': day_start_price_open,
        'candles': []
    }

    for candle in minute_data_from_916:
        ts = candle['minute_ts']
        minutes_before = (ts - nse_time).total_seconds() / 60
        close_price = candle['close'] if candle['close'] else candle['open']

        # Calculate gain from day start (for all-day reference) - using close
        pct_gain = ((close_price - day_start_price_close) / day_start_price_close * 100) if day_start_price_close and day_start_price_close > 0 else 0

        analysis['candles'].append({
            'timestamp': ts,
            'minutes_before_nse': minutes_before,
            'open': candle['open'],
            'close': close_price,
            'high': candle['high'],
            'low': candle['low'],
            'volume': candle['volume'],
            'pct_gain': pct_gain,
            'price_at_filing': price_at_filing,
        })

    return analysis


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--limit", type=int, default=None, help="Max filings to show (default: all)")
    args = parser.parse_args()

    target_date = args.date

    # Create output directory (relative to script location)
    script_dir = Path(__file__).parent
    output_dir = script_dir / "results" / target_date
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n=== PRICE/VOLUME ANALYSIS FOR RESULTS ON {target_date} ===\n")
    print(f"Output directory: {output_dir}\n")

    # Get results declarations
    declarations = get_results_declarations(target_date)
    print(f"Found {len(declarations)} result declarations\n")

    if not declarations:
        print("No declarations found.")
        return

    # Analyze each declaration
    results = []

    for declaration in declarations[:args.limit] if args.limit else declarations:
        symbol = declaration['symbol']
        nse_time = declaration['nse_filing_time']

        # Window: entire trading day (9:15 AM to 4:00 PM IST)
        # But first, we need the timezone-aware dates
        date_obj = datetime.strptime(target_date, '%Y-%m-%d').date()
        market_start = datetime.combine(date_obj, datetime.min.time()).replace(hour=9, minute=15, tzinfo=IST)
        market_end = datetime.combine(date_obj, datetime.min.time()).replace(hour=16, minute=0, tzinfo=IST)

        minute_data = get_minute_data(symbol, target_date, market_start, market_end)

        if not minute_data:
            print(f"  [skip] {symbol} - no tick data for {target_date}", file=sys.stderr)
            continue

        analysis = analyze_filing(symbol, nse_time, minute_data, target_date)
        if analysis:
            results.append(analysis)
        else:
            print(f"  [skip] {symbol} - analyze_filing returned None", file=sys.stderr)

    print(f"Retrieved data for {len(results)} companies\n")

    # Map results by symbol for quick lookup
    results_by_symbol = {r['symbol']: r for r in results}

    # Print summary table
    print(f"{'Symbol':<12} {'NSE Filed':<25} {'60-min %':<12} {'30-min %':<12} {'At Filing %':<12} {'Max Gain %':<12} {'Avg Vol':<12}")
    print("-" * 115)

    for declaration in declarations[:args.limit] if args.limit else declarations:
        symbol = declaration['symbol']
        nse_str = declaration['nse_filing_time'].strftime('%Y-%m-%d %H:%M:%S') if declaration['nse_filing_time'] else "N/A"

        if symbol in results_by_symbol:
            analysis = results_by_symbol[symbol]
            candles = analysis['candles']

            if candles:
                # Find prices at key time points
                prices_60 = [c['pct_gain'] for c in candles if c['minutes_before_nse'] <= -60]
                prices_30 = [c['pct_gain'] for c in candles if -60 <= c['minutes_before_nse'] <= -30]
                prices_at = [c['pct_gain'] for c in candles if c['minutes_before_nse'] >= -1]

                price_60 = f"{prices_60[0]:.2f}%" if prices_60 else "N/A"
                price_30 = f"{prices_30[0]:.2f}%" if prices_30 else "N/A"
                price_at = f"{prices_at[0]:.2f}%" if prices_at else "N/A"

                max_gain = max([c['pct_gain'] for c in candles])
                max_gain_str = f"{max_gain:.2f}%"

                avg_vol = sum(c['volume'] for c in candles) / len(candles) if candles else 0
                avg_vol_str = f"{avg_vol:,.0f}"

                print(f"{symbol:<12} {nse_str:<25} {price_60:<12} {price_30:<12} {price_at:<12} {max_gain_str:<12} {avg_vol_str:<12}")
        else:
            # No tick data for this symbol
            print(f"{symbol:<12} {nse_str:<25} {'-':<12} {'-':<12} {'-':<12} {'-':<12} {'-':<12}")

    # Detailed breakdown for first result
    if results:
        print(f"\n=== DETAILED: {results[0]['symbol']} ===\n")
        print(f"{'Min Before':<15} {'Time':<25} {'Close':<12} {'Pct Gain':<12} {'Volume':<15}")
        print("-" * 85)

        for candle in results[0]['candles']:
            min_before = f"{candle['minutes_before_nse']:.0f}m"
            time_str = candle['timestamp'].strftime('%Y-%m-%d %H:%M:%S')
            close_str = f"{candle['close']:.2f}"
            pct_str = f"{candle['pct_gain']:.2f}%"
            vol_str = f"{candle['volume']:,.0f}"

            print(f"{min_before:<15} {time_str:<25} {close_str:<12} {pct_str:<12} {vol_str:<15}")

    # Write summary CSV
    summary_csv = output_dir / f"summary_{target_date}.csv"
    with open(summary_csv, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow([
            'symbol', 'nse_filing_time', 'price_60min_before', 'price_30min_before',
            'price_at_filing', 'max_gain_pct', 'avg_volume', 'num_candles'
        ])

        for declaration in declarations[:args.limit] if args.limit else declarations:
            symbol = declaration['symbol']
            nse_str = declaration['nse_filing_time'].strftime('%Y-%m-%d %H:%M:%S') if declaration['nse_filing_time'] else "-"

            if symbol in results_by_symbol:
                analysis = results_by_symbol[symbol]
                candles = analysis['candles']

                if candles:
                    prices_60 = [c['pct_gain'] for c in candles if c['minutes_before_nse'] <= -60]
                    prices_30 = [c['pct_gain'] for c in candles if -60 <= c['minutes_before_nse'] <= -30]
                    prices_at = [c['pct_gain'] for c in candles if c['minutes_before_nse'] >= -1]

                    price_60 = prices_60[0] if prices_60 else None
                    price_30 = prices_30[0] if prices_30 else None
                    price_at = prices_at[0] if prices_at else None
                    max_gain = max([c['pct_gain'] for c in candles])
                    avg_vol = sum(c['volume'] for c in candles) / len(candles) if candles else 0

                    writer.writerow([
                        symbol,
                        nse_str,
                        f"{price_60:.2f}" if price_60 is not None else "-",
                        f"{price_30:.2f}" if price_30 is not None else "-",
                        f"{price_at:.2f}" if price_at is not None else "-",
                        f"{max_gain:.2f}",
                        f"{avg_vol:.0f}",
                        len(candles)
                    ])
                else:
                    writer.writerow([symbol, nse_str, "-", "-", "-", "-", "-", "-"])
            else:
                # No tick data for this symbol
                writer.writerow([symbol, nse_str, "-", "-", "-", "-", "-", "-"])

    print(f"\n✓ Summary CSV saved: {summary_csv}")

    # Write detailed CSV (all minute-by-minute data)
    detailed_csv = output_dir / f"detailed_{target_date}.csv"
    with open(detailed_csv, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow([
            'symbol', 'nse_filing_time', 'minute_timestamp', 'minutes_before_nse',
            'open', 'high', 'low', 'close', 'pct_gain', 'volume'
        ])

        for declaration in declarations[:args.limit] if args.limit else declarations:
            symbol = declaration['symbol']
            nse_str = declaration['nse_filing_time'].strftime('%Y-%m-%d %H:%M:%S') if declaration['nse_filing_time'] else "-"

            if symbol in results_by_symbol:
                analysis = results_by_symbol[symbol]
                for candle in analysis['candles']:
                    time_str = candle['timestamp'].strftime('%Y-%m-%d %H:%M:%S')
                    min_before = f"{candle['minutes_before_nse']:.0f}"

                    writer.writerow([
                        symbol,
                        nse_str,
                        time_str,
                        min_before,
                        f"{candle['open']:.2f}" if candle['open'] else "",
                        f"{candle['high']:.2f}" if candle['high'] else "",
                        f"{candle['low']:.2f}" if candle['low'] else "",
                        f"{candle['close']:.2f}" if candle['close'] else "",
                        f"{candle['pct_gain']:.2f}",
                        int(candle['volume'])
                    ])
            else:
                # No tick data for this symbol - write one row with dashes
                writer.writerow([symbol, nse_str, "-", "-", "-", "-", "-", "-", "-", "-"])

    print(f"✓ Detailed CSV saved: {detailed_csv}")

    # Write gain timing CSV (when max gains occurred relative to filing)
    # Format: value(value_with_open_baseline) where close-based is main, open-based in brackets
    timing_csv = output_dir / f"gain_timing_{target_date}.csv"
    with open(timing_csv, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow([
            'symbol', 'nse_filing_time',
            'max_gain_before_pct', 'min_before_filing_when_max', 'time_of_max_before',
            'max_gain_after_pct', 'min_after_filing_when_max', 'time_of_max_after',
            'max_gain_whole_day_before_pct', 'min_before_filing_when_max_whole_day', 'time_of_max_whole_day_before',
            'max_gain_whole_day_after_pct', 'min_after_filing_when_max_whole_day', 'time_of_max_whole_day_after',
            'max_gain_through_day_pct', 'when_max_through_day'
        ])

        for declaration in declarations[:args.limit] if args.limit else declarations:
            symbol = declaration['symbol']
            nse_str = declaration['nse_filing_time'].strftime('%Y-%m-%d %H:%M:%S') if declaration['nse_filing_time'] else "-"

            if symbol not in results_by_symbol:
                # No tick data for this symbol
                writer.writerow([symbol, nse_str, "-", "-", "-", "-", "-", "-", "-", "-", "-", "-", "-", "-", "-", "-"])
                continue

            analysis = results_by_symbol[symbol]
            candles = analysis['candles']

            if not candles:
                writer.writerow([symbol, nse_str, "-", "-", "-", "-", "-", "-", "-", "-", "-", "-", "-", "-", "-", "-"])
                continue

            # Separate before and after filing
            before_filing = [c for c in candles if c['minutes_before_nse'] <= 0]
            after_filing = [c for c in candles if c['minutes_before_nse'] > 0]

            # Also separate 60-min windows
            before_60min = [c for c in candles if -60 <= c['minutes_before_nse'] <= 0]
            after_60min = [c for c in candles if 0 < c['minutes_before_nse'] <= 60]

            # --- 60-MIN BEFORE FILING (always use dual baseline format) ---
            max_before_pct = None
            max_before_pct_open = None
            min_before_max = None
            time_before_max = None

            if before_60min:
                # Get earliest price in 60-min window as base (both close and open)
                earliest_in_window = before_60min[0]
                price_at_60min_close = earliest_in_window['close']
                price_at_60min_open = earliest_in_window['open']

                # Calculate gains from both close and open baselines
                max_before_candle = None
                max_before_gain_close = float('-inf')
                max_before_gain_open = float('-inf')

                for c in before_60min:
                    # Gain from close baseline (using HIGH to capture true intraday peak)
                    gain_close = ((c['high'] - price_at_60min_close) / price_at_60min_close * 100) if price_at_60min_close and price_at_60min_close > 0 else 0
                    if gain_close > max_before_gain_close:
                        max_before_gain_close = gain_close
                        max_before_candle = c

                    # Gain from open baseline (using HIGH to capture true intraday peak)
                    gain_open = ((c['high'] - price_at_60min_open) / price_at_60min_open * 100) if price_at_60min_open and price_at_60min_open > 0 else 0
                    if gain_open > max_before_gain_open:
                        max_before_gain_open = gain_open

                if max_before_candle:
                    max_before_pct = max_before_gain_close
                    max_before_pct_open = max_before_gain_open
                    min_before_max = abs(max_before_candle['minutes_before_nse'])
                    time_before_max = max_before_candle['timestamp'].strftime('%Y-%m-%d %H:%M:%S')

            # --- 60-MIN AFTER FILING (using filing price as base) ---
            max_after_pct = None
            min_after_max = None
            time_after_max = None

            if after_60min:
                price_at_filing = after_60min[0]['price_at_filing'] if 'price_at_filing' in after_60min[0] else None

                if price_at_filing and price_at_filing > 0:
                    max_after_candle = None
                    max_after_gain = float('-inf')
                    for c in after_60min:
                        gain = ((c['high'] - price_at_filing) / price_at_filing * 100)
                        if gain > max_after_gain:
                            max_after_gain = gain
                            max_after_candle = c

                    if max_after_candle:
                        max_after_pct = max_after_gain
                        min_after_max = max_after_candle['minutes_before_nse']
                        time_after_max = max_after_candle['timestamp'].strftime('%Y-%m-%d %H:%M:%S')

            # --- WHOLE DAY BEFORE FILING (both close and open baselines) ---
            max_whole_before_pct_close = None
            max_whole_before_pct_open = None
            min_whole_before_max = None
            time_whole_before_max = None

            if before_filing:
                price_at_day_start_close = analysis['day_start_price_close']
                price_at_day_start_open = analysis['day_start_price_open']

                max_whole_before_candle = None
                max_whole_before_gain_close = float('-inf')
                max_whole_before_gain_open = float('-inf')

                for c in before_filing:
                    # Using close baseline (using HIGH to capture true intraday peak)
                    gain_close = ((c['high'] - price_at_day_start_close) / price_at_day_start_close * 100) if price_at_day_start_close and price_at_day_start_close > 0 else 0
                    # Using open baseline (using HIGH to capture true intraday peak)
                    gain_open = ((c['high'] - price_at_day_start_open) / price_at_day_start_open * 100) if price_at_day_start_open and price_at_day_start_open > 0 else 0

                    if gain_close > max_whole_before_gain_close:
                        max_whole_before_gain_close = gain_close
                        max_whole_before_candle = c
                    if gain_open > max_whole_before_gain_open:
                        max_whole_before_gain_open = gain_open

                if max_whole_before_candle:
                    max_whole_before_pct_close = max_whole_before_gain_close
                    max_whole_before_pct_open = max_whole_before_gain_open
                    min_whole_before_max = abs(max_whole_before_candle['minutes_before_nse'])
                    time_whole_before_max = max_whole_before_candle['timestamp'].strftime('%Y-%m-%d %H:%M:%S')

            # --- WHOLE DAY AFTER FILING (using filing price as base, both close and open of 9:16) ---
            max_whole_after_pct_close = None
            max_whole_after_pct_open = None
            min_whole_after_max = None
            time_whole_after_max = None

            if after_filing:
                price_at_filing = after_filing[0]['price_at_filing'] if 'price_at_filing' in after_filing[0] else None

                if price_at_filing and price_at_filing > 0:
                    max_whole_after_candle = None
                    max_whole_after_gain = float('-inf')

                    for c in after_filing:
                        gain = ((c['high'] - price_at_filing) / price_at_filing * 100)
                        if gain > max_whole_after_gain:
                            max_whole_after_gain = gain
                            max_whole_after_candle = c

                    if max_whole_after_candle:
                        max_whole_after_pct_close = max_whole_after_gain
                        max_whole_after_pct_open = max_whole_after_gain  # Same for after since filing price is constant baseline
                        min_whole_after_max = max_whole_after_candle['minutes_before_nse']
                        time_whole_after_max = max_whole_after_candle['timestamp'].strftime('%Y-%m-%d %H:%M:%S')

            # --- MAX GAIN THROUGH ENTIRE DAY (from 9:16 to peak) ---
            max_through_day_pct_close = None
            max_through_day_pct_open = None
            time_max_through_day = None

            if candles:
                price_at_day_start_close = analysis['day_start_price_close']
                price_at_day_start_open = analysis['day_start_price_open']

                max_through_day_gain_close = float('-inf')
                max_through_day_gain_open = float('-inf')
                max_through_day_candle = None

                for c in candles:
                    gain_close = ((c['high'] - price_at_day_start_close) / price_at_day_start_close * 100) if price_at_day_start_close and price_at_day_start_close > 0 else 0
                    gain_open = ((c['high'] - price_at_day_start_open) / price_at_day_start_open * 100) if price_at_day_start_open and price_at_day_start_open > 0 else 0

                    if gain_close > max_through_day_gain_close:
                        max_through_day_gain_close = gain_close
                        max_through_day_candle = c
                    if gain_open > max_through_day_gain_open:
                        max_through_day_gain_open = gain_open

                if max_through_day_candle:
                    max_through_day_pct_close = max_through_day_gain_close
                    max_through_day_pct_open = max_through_day_gain_open
                    time_max_through_day = max_through_day_candle['timestamp'].strftime('%Y-%m-%d %H:%M:%S')

            # Format 60-min before as dual if filing at or before 10:16
            before_formatted = ""
            if max_before_pct is not None:
                if max_before_pct_open is not None:
                    before_formatted = f"{max_before_pct:.2f}({max_before_pct_open:.2f})"
                else:
                    before_formatted = f"{max_before_pct:.2f}"

            # Format whole day metrics as close(open)
            whole_day_before_formatted = ""
            if max_whole_before_pct_close is not None and max_whole_before_pct_open is not None:
                whole_day_before_formatted = f"{max_whole_before_pct_close:.2f}({max_whole_before_pct_open:.2f})"
            elif max_whole_before_pct_close is not None:
                whole_day_before_formatted = f"{max_whole_before_pct_close:.2f}"

            whole_day_after_formatted = ""
            if max_whole_after_pct_close is not None and max_whole_after_pct_open is not None:
                whole_day_after_formatted = f"{max_whole_after_pct_close:.2f}({max_whole_after_pct_open:.2f})"
            elif max_whole_after_pct_close is not None:
                whole_day_after_formatted = f"{max_whole_after_pct_close:.2f}"

            # Format through day metrics as close(open)
            through_day_formatted = ""
            if max_through_day_pct_close is not None and max_through_day_pct_open is not None:
                through_day_formatted = f"{max_through_day_pct_close:.2f}({max_through_day_pct_open:.2f})"
            elif max_through_day_pct_close is not None:
                through_day_formatted = f"{max_through_day_pct_close:.2f}"

            writer.writerow([
                symbol,
                nse_str,
                before_formatted,
                f"{min_before_max:.0f}" if min_before_max is not None else "",
                time_before_max if time_before_max else "",
                f"{max_after_pct:.2f}" if max_after_pct is not None else "",
                f"{min_after_max:.0f}" if min_after_max is not None else "",
                time_after_max if time_after_max else "",
                whole_day_before_formatted,
                f"{min_whole_before_max:.0f}" if min_whole_before_max is not None else "",
                time_whole_before_max if time_whole_before_max else "",
                whole_day_after_formatted,
                f"{min_whole_after_max:.0f}" if min_whole_after_max is not None else "",
                time_whole_after_max if time_whole_after_max else "",
                through_day_formatted,
                time_max_through_day if time_max_through_day else ""
            ])

    print(f"✓ Gain timing CSV saved: {timing_csv}")
    print(f"\nTotal declarations: {len(declarations)}")
    print(f"With tick data: {len(results)}")
    print(f"Output files:\n  - {summary_csv.name}\n  - {detailed_csv.name}\n  - {timing_csv.name}\n")


if __name__ == "__main__":
    main()
