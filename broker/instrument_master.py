#!/usr/bin/env python3
"""
Upstox instrument master -- maps NSE trading symbols (e.g. "TCS", "RELIANCE",
the same symbols nse_bot.py returns) to Upstox instrument_keys (e.g.
"NSE_EQ|INE467B01029"), which every Upstox quote/feed/candle API needs instead
of the plain symbol. Also keeps each company's listed name and ISIN, used by
company_lookup.py to resolve free-text company names (from news headlines)
to a ticker.

Source: https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz
-- Upstox's own published instrument list, refreshed on their end regularly.
Stored in Postgres (see db.py's `companies` table) instead of a local JSON
cache -- refreshed at most once a day, same as the old cache's TTL.
"""
import gzip
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db  # noqa: E402

SOURCE_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
CACHE_MAX_AGE = timedelta(hours=24)


def _download():
    req = urllib.request.Request(SOURCE_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = gzip.decompress(resp.read())
    return json.loads(raw)


# NSE "instrument_type" here is really the trading series. Verified against
# real data (not guessed): EQ/BE/BZ/SM/ST are genuine equity companies on
# different listing segments (BE = trade-for-trade, SM/ST = SME platform) --
# excluding all but "EQ" silently dropped real, currently-listed companies
# (e.g. MTAR Technologies is "BE", not "EQ", and was missing entirely, which
# caused a wrong grounding match downstream). The many N-/Y-/Z-series codes
# are bonds/NCDs (e.g. "IRFC 7.37% 2029"), correctly excluded -- not companies.
EQUITY_SERIES = {"EQ", "BE", "BZ", "SM", "ST"}


def _build_symbol_map(records):
    mapping = {}
    for rec in records:
        if rec.get("segment") != "NSE_EQ" or rec.get("instrument_type") not in EQUITY_SERIES:
            continue
        isin = rec.get("isin", "")
        if not isin.startswith("INE"):
            # India's ISIN convention: INE = equity, INF = mutual fund/ETF unit
            # (AMC schemes like "BIRLASLAMC - TECH" show up as NSE_EQ/EQ too,
            # with generic-word trading symbols like "TECH"/"VALUE" that would
            # otherwise collide with real words in free-text company scanning).
            continue
        mapping[rec["trading_symbol"]] = {
            "instrument_key": rec["instrument_key"],
            "name": rec.get("name", ""),
            "isin": isin,
            "series": rec.get("instrument_type", ""),
        }
    return mapping


def _is_fresh():
    last_updated = db.companies_last_updated()
    if last_updated is None:
        return False
    return (datetime.now(timezone.utc) - last_updated) < CACHE_MAX_AGE


def _load_or_refresh(force_refresh=False):
    if not force_refresh and _is_fresh():
        mapping = db.get_all_companies()
        if mapping:
            return mapping
    mapping = _build_symbol_map(_download())
    db.upsert_companies(mapping)
    return mapping


def get_instrument_key(symbol, force_refresh=False):
    mapping = _load_or_refresh(force_refresh)
    record = mapping.get(symbol)
    if record is None and not force_refresh:
        # Symbol might be genuinely missing, or the cache might just be a day
        # stale and it's a new listing -- try one forced refresh before giving up.
        mapping = _load_or_refresh(force_refresh=True)
        record = mapping.get(symbol)
    if record is None:
        raise KeyError(f"No Upstox instrument_key found for NSE symbol '{symbol}'.")
    return record["instrument_key"]


def get_all_nse_equities(force_refresh=False):
    """symbol -> {instrument_key, name, isin} for every NSE-listed equity."""
    return dict(_load_or_refresh(force_refresh))


if __name__ == "__main__":
    mapping = _load_or_refresh(force_refresh=True)
    print(f"Refreshed {len(mapping)} NSE equity records into Postgres.")
    for symbol in ("RELIANCE", "TCS", "INFY"):
        print(f"  {symbol} -> {mapping.get(symbol, 'NOT FOUND')}")
