#!/usr/bin/env python3
"""
NSE's official market-cap tiers -- Nifty 50 + Nifty Next 50 (large, rank
1-100), Nifty Midcap 150 (mid, rank 101-250), Nifty Smallcap 250 (small,
rank 251-500). This is SEBI's actual legal large/mid/small-cap definition,
not an approximation -- these lists are exactly that ranking, published free
by NSE with no auth needed.

Fetches and stores all three tiers. Only 'large' currently feeds any
decision in trade_bot.py (see MIN_PROFIT_PCT/MIN_REVENUE_PCT there) -- mid
and small are stored for completeness/future use, not wired into any
qualification threshold. Don't assume mid/mid-cap-tagged companies get
different treatment than "sme" until that's actually built.

Deliberately NOT yfinance: this project removed yfinance from the live
trading path on purpose (see CLAUDE.md) -- reusing it here, even just for
company classification rather than pricing, would reintroduce the same
dependency this project already decided against, and yfinance's market-cap
coverage has real gaps for smaller/less-liquid names anyway (verified: it
returned nothing for a real small SME-listed company during evaluation).
NSE's own index lists are free, authoritative, and need no such dependency.

Source (NSE's own archive, no auth):
    https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv
    https://nsearchives.nseindia.com/content/indices/ind_niftynext50list.csv
    https://nsearchives.nseindia.com/content/indices/ind_niftymidcap150list.csv
    https://nsearchives.nseindia.com/content/indices/ind_niftysmallcap250list.csv

Stored in Postgres (companies.cap_tier) alongside the rest of the instrument
master -- see db.py. Refreshed on the same ~24h cadence as instrument_master.py.
"""
import csv
import io
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db  # noqa: E402

NIFTY_50_URL = "https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv"
NIFTY_NEXT_50_URL = "https://nsearchives.nseindia.com/content/indices/ind_niftynext50list.csv"
NIFTY_MIDCAP_150_URL = "https://nsearchives.nseindia.com/content/indices/ind_niftymidcap150list.csv"
NIFTY_SMALLCAP_250_URL = "https://nsearchives.nseindia.com/content/indices/ind_niftysmallcap250list.csv"
CACHE_MAX_AGE = timedelta(hours=24)


def _fetch_symbols(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        text = resp.read().decode()
    return {row["Symbol"].strip() for row in csv.DictReader(io.StringIO(text))}


def fetch_large_cap_symbols():
    """Union of Nifty 50 + Nifty Next 50 -- the real top-100-by-market-cap set."""
    return _fetch_symbols(NIFTY_50_URL) | _fetch_symbols(NIFTY_NEXT_50_URL)


def fetch_mid_cap_symbols():
    """Nifty Midcap 150 -- real rank 101-250 by market cap."""
    return _fetch_symbols(NIFTY_MIDCAP_150_URL)


def fetch_small_cap_symbols():
    """Nifty Smallcap 250 -- real rank 251-500 by market cap."""
    return _fetch_symbols(NIFTY_SMALLCAP_250_URL)


def _is_fresh():
    last_updated = db.cap_tier_last_updated()
    if last_updated is None:
        return False
    return (datetime.now(timezone.utc) - last_updated) < CACHE_MAX_AGE


def refresh(force_refresh=False):
    """Idempotent, safe to call often -- no-ops if already refreshed within
    CACHE_MAX_AGE unless force_refresh. Resets every company's cap_tier
    before re-marking each fetched set, so a company dropped from a real
    index (reconstitution happens periodically) doesn't stay incorrectly
    tagged forever from a stale previous fetch. Returns {tier: symbol_set}."""
    if not force_refresh and _is_fresh():
        return None
    tiers = {
        "large": fetch_large_cap_symbols(),
        "mid": fetch_mid_cap_symbols(),
        "small": fetch_small_cap_symbols(),
    }
    db.set_cap_tiers(tiers)
    return tiers


if __name__ == "__main__":
    tiers = refresh(force_refresh=True)
    for tier, symbols in tiers.items():
        print(f"Marked {len(symbols)} symbols as '{tier}'.")
    for s in ("RELIANCE", "TCS", "PRIMECAB", "SHADOWFAX"):
        found = next((t for t, syms in tiers.items() if s in syms), "none of the three")
        print(f"  {s}: {found}")
