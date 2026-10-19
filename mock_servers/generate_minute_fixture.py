#!/usr/bin/env python3
"""
Fetches REAL 1-minute OHLCV bars (via yfinance -- Yahoo Finance keeps ~7 days
of 1-minute history) for the same 100 symbols already in
fixtures/upstox_fixture.json, for one target trading day, and saves them into
Postgres (mock_price_history table -- see db.py).

This is a test-fixture-only script, same status as generate_fixtures.py and
generate_news_fixture.py -- it doesn't touch the live trading path
(trade_bot.py never imports yfinance; per CLAUDE.md, yfinance was
deliberately removed from the actual bot and stays removed there).

mock_upstox.py then REPLAYS this real per-minute tape: each request for a
symbol advances one bar further into its real recorded day, instead of the
previous engineered "+2% per call" synthetic drift -- so a `monitor --watch`
loop watches an actual real intraday price path unfold, not a fabricated one.

Usage:
    ./.venv/bin/python mock_servers/generate_minute_fixture.py [--date 2026-07-31]
"""
import argparse
import json
import os
import sys
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
import db  # noqa: E402

UPSTOX_FIXTURE_PATH = os.path.join(BASE_DIR, "mock_servers", "fixtures", "upstox_fixture.json")
BATCH_SIZE = 20  # yfinance multi-ticker downloads get flaky/slow much past this


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default=None, help="Trading day to fetch, YYYY-MM-DD (default: most recent day yfinance has)")
    args = parser.parse_args()

    try:
        import yfinance as yf
    except ImportError:
        sys.exit("yfinance isn't installed in this venv. Install it (dev/test only -- "
                  "the live trading path never imports it): ./.venv/bin/pip install yfinance")

    with open(UPSTOX_FIXTURE_PATH) as f:
        upstox_fixture = json.load(f)
    symbols = list(upstox_fixture.keys())
    tickers = [f"{s}.NS" for s in symbols]

    print(f"Fetching real 1-minute bars for {len(symbols)} symbols from Yahoo Finance...")
    total_bars = 0
    covered = set()
    for i in range(0, len(tickers), BATCH_SIZE):
        batch_tickers = tickers[i:i + BATCH_SIZE]
        batch_symbols = symbols[i:i + BATCH_SIZE]
        data = yf.download(tickers=batch_tickers, period="7d", interval="1m",
                            group_by="ticker", progress=False, threads=True)
        for symbol, ticker in zip(batch_symbols, batch_tickers):
            try:
                df = data[ticker] if len(batch_tickers) > 1 else data
            except KeyError:
                print(f"  [warn] {symbol}: no data returned", file=sys.stderr)
                continue
            df = df.dropna(how="all")
            if df.empty:
                print(f"  [warn] {symbol}: empty series", file=sys.stderr)
                continue
            df.index = df.index.tz_convert("Asia/Kolkata")
            if args.date:
                df = df[df.index.strftime("%Y-%m-%d") == args.date]
            else:
                latest_date = df.index.strftime("%Y-%m-%d").max()
                df = df[df.index.strftime("%Y-%m-%d") == latest_date]
            if df.empty:
                print(f"  [warn] {symbol}: no bars for the target date", file=sys.stderr)
                continue

            bars = [
                (ts.to_pydatetime(), float(row["Open"]), float(row["High"]),
                 float(row["Low"]), float(row["Close"]), int(row["Volume"]))
                for ts, row in df.iterrows()
            ]
            db.upsert_price_history(symbol, bars)
            total_bars += len(bars)
            covered.add(symbol)
            print(f"  [{i + batch_symbols.index(symbol) + 1}/{len(symbols)}] {symbol}: "
                  f"{len(bars)} real 1-min bars ({bars[0][0].strftime('%H:%M')}-{bars[-1][0].strftime('%H:%M')})")

    missing = set(symbols) - covered
    print(f"\nSaved {total_bars} real 1-minute bars across {len(covered)}/{len(symbols)} symbols to mock_price_history.")
    if missing:
        print(f"[note] No real minute data for: {sorted(missing)} -- mock_upstox.py falls back to the "
              f"static/synthesized fixture for these.")


if __name__ == "__main__":
    main()
