#!/usr/bin/env python3
"""
Resolve a free-text company name (as regex-guessed from a news headline by
news_bot.py, e.g. "Reliance Industries" or "Tata Consultancy Services") to an
NSE ticker, using Upstox's instrument master (which has each company's listed
name alongside its trading symbol -- see instrument_master.py).

This is a best-effort fuzzy match, not an exact lookup: headline text rarely
matches the exchange's listed name verbatim (abbreviations, "Ltd" vs "Limited",
missing "Industries"/"Technologies" suffixes, etc). Treat matches below
DEFAULT_CUTOFF as unreliable, and per this project's existing risk rules,
never auto-open a trade from a fuzzy match alone -- always show it to the user
for manual confirmation (matching news_bot.py's own "verify before acting"
disclaimer and trade_bot.py's manual-ticker-confirmation requirement).
"""
import difflib
import re

import instrument_master

DEFAULT_CUTOFF = 0.6

_SUFFIX_RE = re.compile(
    r"\b(LIMITED|LTD|LIMITE|LIMIT|LTD\.?|PVT|PRIVATE|CO|COMPANY|CORPORATION|CORP|INDIA|"
    r"INDUSTRIES|IND|ENTERPRISES|ENTERPRISE|GROUP|HOLDINGS|SERVICES|SERVICE|BANK)\b\.?"
)
_PUNCT_RE = re.compile(r"[^\w\s]")


def normalize(name):
    name = name.upper()
    name = _PUNCT_RE.sub(" ", name)
    name = _SUFFIX_RE.sub(" ", name)
    return re.sub(r"\s+", " ", name).strip()


_index_cache = None


def _build_index():
    global _index_cache
    if _index_cache is not None:
        return _index_cache
    equities = instrument_master.get_all_nse_equities()
    _index_cache = [
        {
            "symbol": symbol,
            "name": rec["name"],
            "instrument_key": rec["instrument_key"],
            "normalized": normalize(rec["name"]),
        }
        for symbol, rec in equities.items()
    ]
    return _index_cache


# Whole-string SequenceMatcher.ratio() alone is fooled by two DIFFERENT
# companies that happen to share a generic industry-descriptor word --
# verified live against real headlines, e.g. "Avanse Financial Services" vs
# the real-but-unrelated "ABANS FINANCIAL SRVCS LTD" (shared "FINANCIAL"
# inflates the whole-string ratio to 0.75-0.8+ even though the actual
# distinguishing word, Avanse/Abans, is what should decide this) -- same
# pattern hit Jio/JM Financial, Dixon/KFin Technologies, Real Touch/L&T
# Finance, PNG/PC Jeweller, Colinz/Valiant Laboratories, and (single-word
# case) Nalco/Nelco, Alphabet/Alphageo. Requiring the FIRST normalized word
# to independently clear its own, higher cutoff fixes this: every genuine
# match tested (Reliance, Sun Pharma, Larsen & Toubro, Tata Consultancy
# Services, State Bank of India, HDFC Bank) scores a clean 1.0 on this
# first-word check, while every collision found tops out at 0.8 -- a wide,
# well-separated margin below FIRST_WORD_CUTOFF.
FIRST_WORD_CUTOFF = 0.85


def resolve_company(company_guess, top_n=3, cutoff=DEFAULT_CUTOFF):
    """Return up to top_n candidates [{symbol, name, instrument_key, score}],
    best score first, scored by similarity to `company_guess`. Empty list if
    nothing clears `cutoff` (or FIRST_WORD_CUTOFF -- see its notes)."""
    index = _build_index()

    # Bare-ticker fast path: headlines often use the exchange symbol itself
    # ("TCS wins...", "L&T bags...") rather than the full listed name, which
    # difflib's name-similarity scoring handles poorly (acronym vs. full name).
    exact_symbol = re.sub(r"[^\w]", "", company_guess.upper())
    for entry in index:
        if entry["symbol"] == exact_symbol:
            return [{"symbol": entry["symbol"], "name": entry["name"],
                      "instrument_key": entry["instrument_key"], "score": 1.0}]

    target = normalize(company_guess)
    if not target:
        return []
    target_first_word = target.split()[0]

    scored = []
    for entry in index:
        score = difflib.SequenceMatcher(None, target, entry["normalized"]).ratio()
        if score < cutoff:
            continue
        entry_words = entry["normalized"].split()
        if not entry_words:
            continue
        first_word_score = difflib.SequenceMatcher(None, target_first_word, entry_words[0]).ratio()
        if first_word_score < FIRST_WORD_CUTOFF:
            continue
        scored.append({**entry, "score": round(score, 3)})

    scored.sort(key=lambda e: e["score"], reverse=True)
    return [
        {"symbol": e["symbol"], "name": e["name"], "instrument_key": e["instrument_key"], "score": e["score"]}
        for e in scored[:top_n]
    ]


MIN_TICKER_LEN = 4  # avoid short tickers colliding with common English words
MIN_NAME_LEN = 6    # avoid degenerate normalized names (e.g. "INDIA") matching everywhere

# Real NSE tickers that are also ordinary English words -- length/normalized-
# name filtering alone can't catch these (found via testing against real
# headlines: "shares TAKE worst hit" -> Take Solutions Ltd, "GDP advance
# estimates" -> Advance Agrolife Ltd). Excluded from full-text scanning only;
# these companies are still resolvable via resolve_company() from an explicit
# leading-phrase guess like "Take Solutions wins...".
EXCLUDE_FROM_SCAN = {"TAKE", "ADVANCE"}


def find_mentions(text):
    """Scan free text for ANY known NSE company mention, not just a leading
    phrase -- catches multi-stock roundup headlines like "Buzzing Stocks: X
    gains...; MOIL jumps..." where the company of interest isn't at the start.

    Exact matching only (ticker as a standalone token, or full normalized
    company name as a substring) -- no fuzzy scoring, since approximating
    across an entire headline against ~2,400 names would produce far more
    false positives than matching a single extracted phrase would.
    Returns a deduped list of {symbol, name, instrument_key, match_type}."""
    index = _build_index()
    tokens = set(re.findall(r"[A-Z0-9&]+", text.upper()))
    normalized_text = normalize(text)

    found = {}
    for entry in index:
        symbol = entry["symbol"]
        if symbol in EXCLUDE_FROM_SCAN:
            continue
        if len(symbol) >= MIN_TICKER_LEN and symbol in tokens:
            found[symbol] = {"symbol": symbol, "name": entry["name"],
                              "instrument_key": entry["instrument_key"], "match_type": "ticker"}
            continue
        norm_name = entry["normalized"]
        if len(norm_name) >= MIN_NAME_LEN and norm_name in normalized_text:
            found[symbol] = {"symbol": symbol, "name": entry["name"],
                              "instrument_key": entry["instrument_key"], "match_type": "name"}
    return list(found.values())


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Fuzzy-match a company name to an NSE ticker")
    parser.add_argument("company_name")
    parser.add_argument("--top", type=int, default=3)
    parser.add_argument("--cutoff", type=float, default=DEFAULT_CUTOFF)
    args = parser.parse_args()

    matches = resolve_company(args.company_name, top_n=args.top, cutoff=args.cutoff)
    if not matches:
        print(f"No match >= {args.cutoff} for '{args.company_name}'.")
    for m in matches:
        print(f"  {m['score']:.3f}  {m['symbol']:12s} {m['name']}")
