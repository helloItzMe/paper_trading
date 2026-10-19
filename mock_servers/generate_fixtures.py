#!/usr/bin/env python3
"""
Fixture generator for the mock Upstox server (see mock_upstox.py).

Fully yfinance-based, on purpose: an earlier version fetched the intraday
snapshot live from the Upstox API, which only works while a market session
is actually open "today" -- meaning it could never be regenerated once that
day had passed (e.g. rerunning it on a Saturday gets zero intraday candles
for everyone, since nothing's trading). Sourcing everything -- prev-close,
daily history, AND the intraday snapshot -- from yfinance's 1-minute/1-day
history instead means the exact same fixture can be rebuilt identically at
any time, which is what makes test runs against it actually deterministic.

For one TARGET_DATE (a specific past trading day):
  - prev_close / daily_closes: real daily closes, yfinance daily history.
  - intraday_candles: real 1-minute bars from market open up to
    SNAPSHOT_CUTOFF -- an arbitrary-but-fixed "as of this time of day" point,
    not the whole day (the buy-time gain check is supposed to see "today's
    max gain SO FAR", not the day's eventual high).
  - bucket: computed after the fact, descriptive only (see
    _classify_bucket) -- this script doesn't steer toward any particular
    distribution, it just fetches what really happened.

Usage:
    ./.venv/bin/python mock_servers/generate_fixtures.py [--date 2026-07-31]
"""
import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
import db  # noqa: E402

OUT_PATH = os.path.join(BASE_DIR, "mock_servers", "fixtures", "upstox_fixture.json")
DEFAULT_DATE = "2026-07-31"  # last real trading day this project's fixtures are pinned to
SNAPSHOT_CUTOFF = "11:30"    # "as of this time of day" for the intraday buy-check snapshot
HISTORY_DAYS = 40

# A curated set of well-known, liquid large/mid-caps -- these are the names
# most likely to show up in real fetched news headlines too (see
# generate_news_fixture.py), so buy-candidate symbols and news-mentioned
# symbols actually overlap instead of being two disjoint universes.
KNOWN_SYMBOLS = [
    "RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "LT", "SBIN", "ITC",
    "HINDUNILVR", "BHARTIARTL", "KOTAKBANK", "AXISBANK", "MARUTI", "SUNPHARMA",
    "TATAMOTORS", "TATASTEEL", "WIPRO", "ADANIPORTS", "ONGC", "NTPC",
    "POWERGRID", "TITAN", "ULTRACEMCO", "ASIANPAINT", "BAJFINANCE",
    "BAJAJFINSV", "HCLTECH", "TECHM", "GRASIM", "JSWSTEEL", "COALINDIA",
    "DRREDDY", "CIPLA", "DIVISLAB", "EICHERMOT", "HEROMOTOCO", "BPCL",
    "IOC", "HINDALCO", "APOLLOHOSP", "ZOMATO", "DMART", "PIDILITIND",
    "SIEMENS", "DLF", "GODREJCP", "MOTHERSON", "MTAR", "RATEGAIN", "IRCTC",
]

# Companies our FIXED news_fixture.json's real (live-fetched-once) headlines
# actually resolve to as trade candidates -- observed consistently across
# repeated real scan runs against that fixture. Pinning these here means the
# mock never has to fall back to mock_upstox.py's fake-data synthesizer for
# our actual test corpus; every symbol our news fixture can ever produce has
# real price data behind it.
NEWS_FIXTURE_SYMBOLS = ["INOXWIND", "NLCINDIA", "HIRECT", "IRFC", "IEML", "INOXINDIA", "APOLLO"]

# Handpicked real anecdotes, deliberately included for specific test coverage
# (see per-symbol comments) rather than picked at random like the rest.
SPECIAL_CASE_SYMBOLS = ["SHADOWFAX"]

# Per-symbol override of SNAPSHOT_CUTOFF -- SHADOWFAX had TWO real rallies on
# 2026-07-31: an earlier one (day's cumulative high +4.19% by 09:45, unrelated
# to the news this fixture is meant to test) and a second, sharper one
# starting ~12:14 driven by real profit news. 12:16 -- shortly after the news
# broke, as if a scan caught it moments later -- is used here specifically
# because MAX_ENTRY_GAIN_PCT was raised from 2% to 5% (see CLAUDE.md): under
# the OLD 2% ceiling, the earlier rally alone already exceeded it, so this
# stock could never re-qualify for the rest of the day no matter when fresh
# news landed on top of it -- forcing an artificial pre-market snapshot to
# get a buy at all. Under 5%, cumulative gain-so-far doesn't cross that
# ceiling until 12:18 (verified against the real data), so a snapshot at
# 12:16 -- genuinely close to the real news timing -- now organically
# qualifies. The (unmodified, full-day) per-minute replay then carries the
# resulting position through the rest of the real spike to a real +5%+
# target hit -- real news, real reaction, real timing, no artificial anchor.
SNAPSHOT_CUTOFF_OVERRIDES = {"SHADOWFAX": "12:16"}

SEED_SYMBOLS = KNOWN_SYMBOLS + NEWS_FIXTURE_SYMBOLS + SPECIAL_CASE_SYMBOLS
NUM_RANDOM_FILL = 100 - len(set(SEED_SYMBOLS))  # top up to 100 total with a reproducible random sample
RANDOM_SEED = 20260731


def _pick_symbols(companies):
    import random
    symbols = [s for s in SEED_SYMBOLS if s in companies]
    rest = sorted(set(companies) - set(symbols))
    rng = random.Random(RANDOM_SEED)
    rng.shuffle(rest)
    symbols.extend(rest[:max(0, NUM_RANDOM_FILL)])
    return symbols


def _classify_bucket(gain_pct, weak):
    """Descriptive only -- matches trade_bot.py's actual buy-gate threshold
    (MAX_ENTRY_GAIN_PCT=5, the only boundary the real code enforces)."""
    if gain_pct is None:
        return "no_data"
    if gain_pct <= 0:
        return "flat_negative"
    if gain_pct < 5:
        return "buy_window_weak" if weak else "buy_window_normal"
    return "missed"


def _flatten_columns(df, ticker):
    """yfinance returns MultiIndex columns (Price, Ticker) even for a single
    ticker string -- drop the ticker level so df["Close"] etc. is a plain Series."""
    if isinstance(df.columns, __import__("pandas").MultiIndex):
        df = df.copy()
        df.columns = df.columns.droplevel(1)
    return df


def _fetch_symbol(yf, symbol, target_date, from_date):
    ticker = f"{symbol}.NS"
    daily = yf.download(ticker, start=from_date.isoformat(),
                         end=(target_date + timedelta(days=1)).isoformat(),
                         interval="1d", progress=False)
    if daily.empty:
        return None
    daily = _flatten_columns(daily, ticker)
    daily.index = daily.index.tz_localize(None) if daily.index.tz is None else daily.index
    daily_str = daily.copy()
    daily_str.index = daily_str.index.strftime("%Y-%m-%d")
    all_dates = sorted(daily_str.index)
    if target_date.isoformat() not in all_dates:
        return None
    prior_dates = [d for d in all_dates if d < target_date.isoformat()]
    if not prior_dates:
        return None
    prev_close = float(daily_str.loc[prior_dates[-1], "Close"])
    daily_closes = [[d, float(daily_str.loc[d, "Close"])] for d in prior_dates]

    minute = yf.download(ticker, period="7d", interval="1m", progress=False)
    if minute.empty:
        return {"prev_close": prev_close, "daily_closes": daily_closes, "intraday_candles": []}
    minute = _flatten_columns(minute, ticker)
    minute.index = minute.index.tz_convert("Asia/Kolkata")
    day_minutes = minute[minute.index.strftime("%Y-%m-%d") == target_date.isoformat()]
    cutoff = SNAPSHOT_CUTOFF_OVERRIDES.get(symbol, SNAPSHOT_CUTOFF)
    snapshot = day_minutes[day_minutes.index.strftime("%H:%M") <= cutoff]
    intraday_candles = [
        [ts.strftime("%Y-%m-%dT%H:%M:%S+05:30"), float(row["Open"]), float(row["High"]),
         float(row["Low"]), float(row["Close"]), int(row["Volume"]), 0]
        for ts, row in snapshot.iterrows()
    ]
    return {"prev_close": prev_close, "daily_closes": daily_closes, "intraday_candles": intraday_candles}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default=DEFAULT_DATE)
    args = parser.parse_args()
    target_date = datetime.strptime(args.date, "%Y-%m-%d").date()
    from_date = target_date - timedelta(days=HISTORY_DAYS)

    try:
        import yfinance as yf
    except ImportError:
        sys.exit("yfinance isn't installed in this venv (dev/test only): ./.venv/bin/pip install yfinance")

    companies = db.get_all_companies()
    symbols = _pick_symbols(companies)

    fixture = {}
    print(f"Fetching real daily + intraday-snapshot-as-of-{SNAPSHOT_CUTOFF} data for "
          f"{len(symbols)} symbols, target date {target_date}, from Yahoo Finance...")
    for i, symbol in enumerate(symbols):
        rec = companies[symbol]
        try:
            result = _fetch_symbol(yf, symbol, target_date, from_date)
        except Exception as e:
            print(f"  [warn] {symbol}: fetch failed ({e}), skipping", file=sys.stderr)
            continue
        if result is None:
            print(f"  [warn] {symbol}: no data for {target_date}, skipping", file=sys.stderr)
            continue

        gain_pct = None
        if result["intraday_candles"]:
            day_high = max(c[2] for c in result["intraday_candles"])
            gain_pct = (day_high - result["prev_close"]) / result["prev_close"] * 100
        weak = False
        for days_back in (1, 7, 30):
            cutoff = str(target_date - timedelta(days=days_back))
            past = [c for d, c in result["daily_closes"] if d <= cutoff]
            if past and (result["prev_close"] - past[-1]) / past[-1] * 100 <= -5.0:
                weak = True
                break

        bucket = _classify_bucket(gain_pct, weak)
        fixture[symbol] = {
            "instrument_key": rec["instrument_key"], "name": rec.get("name"), "bucket": bucket,
            "prev_close": round(result["prev_close"], 2),
            "intraday_candles": result["intraday_candles"],
            "daily_closes": [[d, round(c, 2)] for d, c in result["daily_closes"]],
        }
        gain_str = f"{gain_pct:+.2f}%" if gain_pct is not None else "n/a"
        print(f"  [{i+1}/{len(symbols)}] {symbol}: bucket={bucket:20s} prev_close={result['prev_close']:.2f} gain={gain_str}")

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(fixture, f, indent=2)

    counts = Counter(rec["bucket"] for rec in fixture.values())
    print(f"\nWrote {len(fixture)} symbols to {OUT_PATH}")
    print(f"Real bucket distribution for {target_date}: {dict(counts)}")
    missing = {"buy_window_normal", "buy_window_weak", "flat_negative", "already_up", "missed", "no_data"} - set(counts)
    if missing:
        print(f"[note] No real symbol landed in: {sorted(missing)}")
    not_covered = set(NEWS_FIXTURE_SYMBOLS) - set(fixture)
    if not_covered:
        print(f"[warn] News-fixture symbols still missing real data: {sorted(not_covered)} "
              f"-- mock_upstox.py will fall back to synthesized data for these.")


if __name__ == "__main__":
    main()
