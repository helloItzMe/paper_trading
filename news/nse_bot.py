#!/usr/bin/env python3
"""
NSE corporate-announcements watcher.

Polls NSE's public corporate-announcements feed (the same regulatory
disclosures listed companies must file for material events -- new orders,
contracts, fundraises, etc.) and prints/writes the ones matching deal-type
keywords. This is a primary source, not aggregated news, so it's typically
minutes ahead of any news article about the same filing.

Usage:
    python3 nse_bot.py                     # scan once, append new order/contract/funding filings to reports/nse.csv
    python3 nse_bot.py --all               # include every announcement, no keyword filter
    python3 nse_bot.py --watch 60          # poll every 60s, append newly-seen filings each cycle
    python3 nse_bot.py --out custom.csv    # write to a different CSV path
    python3 nse_bot.py --refresh-calendar  # force-refresh the results calendar (see below), print a summary, exit

State: seen filing IDs are persisted to .nse_seen.json next to this script so
repeated/--watch runs only append filings you haven't seen yet.

Results calendar: refresh_results_calendar() below fetches NSE's forthcoming-
board-meetings feed and caches it in Postgres (results_calendar table),
refreshed at most once every CACHE_MAX_AGE (same ~24h pattern as
broker/large_cap.py/instrument_master.py) -- meant to be run once daily via
cron/launchd (`--refresh-calendar`), with trade_bot.py's own scan cycle
calling refresh_results_calendar() too (as a no-op safety net if that cron
isn't set up) rather than re-fetching NSE's calendar itself every 5 minutes.
"""
import argparse
import csv
import json
import os
import re
import sys as _sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

_sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db  # noqa: E402

API_URL = "https://www.nseindia.com/api/corporate-announcements?index=equities"

# Forthcoming board meetings -- a genuinely different feed from the one above:
# corporate-announcements tells you a company ALREADY reported results
# ("Outcome of Board Meeting", after the fact); this one is the advance
# calendar -- when a company PLANS to declare, before it happens. Verified
# live: a 2-week window returned 1,448 rows across 669 companies. from_date/
# to_date are DD-MM-YYYY (matches bm_date's own format), same convention as
# API_URL above.
BOARD_MEETINGS_API_URL = "https://www.nseindia.com/api/corporate-board-meetings?index=equities"
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # project root
SEEN_FILE = os.path.join(BASE_DIR, ".nse_seen.json")
DEFAULT_OUT = os.path.join(BASE_DIR, "reports", "nse.csv")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-announcements",
}

# NSE's own disclosure-category taxonomy for order/contract wins -- matching
# on this (rather than free-text "order") avoids false positives like
# "Action(s) taken or orders passed" (legal/regulatory rulings) or "in order
# to ensure..." boilerplate in price-movement alerts.
ORDER_DESC_CATEGORIES = [
    "bagging/receiving of orders/contracts",
    "awarding of order",
]
DEAL_DESC_CATEGORIES = ORDER_DESC_CATEGORIES + [
    "acquisition", "merger", "amalgamation", "investment",
]
RISK_KEYWORDS = [
    "termination", "cancel", "loss", "default", "resign", "fraud",
    "investigation", "penalty", "downgrade",
]

# Quarterly results filings. "Outcome of Board Meeting" also covers unrelated
# things (buybacks, dividends, appointments), so it only counts as a results
# filing when the attached text says so; "Integrated Filing- Financial" is
# unambiguous on its own. NOTE: this only tells you a company REPORTED
# results, not whether profit/revenue rose or fell -- that's in the attached
# PDF, which isn't parsed here. Always route results filings to manual
# review, never auto-trade them.
RESULTS_DESC_CATEGORIES = [
    "integrated filing- financial",
]
RESULTS_BOARD_MEETING_HINT = "financial results"

# Best-effort fiscal-quarter label ("Q1 FY27") parsed from NSE's free-text
# board-meeting description -- "quarter ended <Month> [<Day>,] <Year>" in
# various real phrasings: "for the Quarterly ended June 2026", "for the
# period ended Jun 30, 2026", "for the quarter ended on June 30, 2026" (all
# verified live, 61/65 real rows parsed cleanly in one sample window).
# Indian fiscal year runs Apr-Mar and is conventionally named by the
# calendar year it ENDS in -- e.g. the quarter ending June 2026 is Q1 of
# FY27 (Apr 2026-Mar 2027), matching real headlines' own "Q1 FY27" wording.
QUARTER_END_RE = re.compile(
    r"(?:quarter(?:ly)?|period)\s+ended\s+(?:on\s+)?([A-Za-z]+)\.?\s+(?:\d{1,2}\s*,?\s*)?(\d{4})",
    re.IGNORECASE,
)
_MONTH_TO_QUARTER = {
    1: (4, 0), 2: (4, 0), 3: (4, 0),      # Jan-Mar -> Q4, same calendar-year FY
    4: (1, 1), 5: (1, 1), 6: (1, 1),      # Apr-Jun -> Q1, FY = year + 1
    7: (2, 1), 8: (2, 1), 9: (2, 1),      # Jul-Sep -> Q2, FY = year + 1
    10: (3, 1), 11: (3, 1), 12: (3, 1),   # Oct-Dec -> Q3, FY = year + 1
}

MONEY_RE = re.compile(
    r"(?:US\$|\$|₹|Rs\.?|INR|EUR|€|£)\s?[\d,]+(?:\.\d+)?\s?"
    r"(?:crore|lakh|million|billion|bn|mn|M|B|K|Cr\.?)?",
    re.IGNORECASE,
)

CSV_COLUMNS = [
    "timestamp", "symbol", "company", "isin", "category", "signal",
    "amount", "summary", "filing_url", "seq_id",
]


def extract_amount(text):
    matches = MONEY_RE.findall(text or "")
    return matches[0] if matches else ""


# A transient DNS/network blip used to kill an entire `scan` cycle outright --
# verified live: 6 real scan_runs rows recorded the exact same
# `<urlopen error [Errno 8] nodename nor servname provided...>`, and
# fetch_announcements() is the very first thing _nse_candidates() does, with
# no try/except anywhere between it and cmd_scan -- so one bad DNS lookup
# took the news path down with it too, even though news fetching is
# individually protected per-query (_news_candidates already catches and
# logs a failed query rather than aborting). A short retry here fixes the
# common transient case at the source, rather than only papering over it
# with a broader try/except in cmd_scan (added separately, see there).
_FETCH_RETRIES = 3
_FETCH_RETRY_DELAY = 2  # seconds


def _urlopen_json(url):
    last_err = None
    for attempt in range(_FETCH_RETRIES):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read())
        except Exception as e:
            last_err = e
            if attempt < _FETCH_RETRIES - 1:
                time.sleep(_FETCH_RETRY_DELAY)
    raise last_err


def fetch_announcements(from_date=None, to_date=None):
    url = API_URL
    if from_date and to_date:
        url += f"&from_date={from_date}&to_date={to_date}"
    return _urlopen_json(url)


def load_seen():
    if not os.path.exists(SEEN_FILE):
        return set()
    with open(SEEN_FILE) as f:
        return set(json.load(f))


def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        json.dump(sorted(seen), f)


def fetch_board_meetings(from_date, to_date):
    """from_date/to_date: 'DD-MM-YYYY' strings. Raw rows, unfiltered --
    results_calendar() below narrows this to genuine results declarations."""
    url = f"{BOARD_MEETINGS_API_URL}&from_date={from_date}&to_date={to_date}"
    return _urlopen_json(url)


def _parse_quarter(text):
    """Best-effort 'Q1 FY27'-style label from NSE's free-text board-meeting
    description -- see QUARTER_END_RE's comment above for phrasings/
    verification. None if the text doesn't match any known phrasing (rare,
    ~4/65 in the verified sample) -- best-effort, not guaranteed, same
    spirit as results_calendar.best_declared_at."""
    if not text:
        return None
    m = QUARTER_END_RE.search(text)
    if not m:
        return None
    try:
        month_num = datetime.strptime(m.group(1)[:3], "%b").month
    except ValueError:
        return None
    quarter, fy_offset = _MONTH_TO_QUARTER[month_num]
    fy = int(m.group(2)) + fy_offset
    return f"Q{quarter} FY{fy % 100:02d}"


def results_calendar(from_date, to_date):
    """Board meetings in [from_date, to_date] that are actually about
    financial results (not dividends/buybacks/appointments/etc -- same
    RESULTS_BOARD_MEETING_HINT text check as is_results_filing uses for the
    after-the-fact filings feed), deduped to one entry per symbol+date. A
    single company routinely has multiple rows for the same meeting (a
    generic "Board Meeting Intimation" plus a specific "Financial Results/
    Other business matters" row, verified live) -- callers want one
    query-worthy entry per company, not a duplicate per row.

    Returns a list of {symbol, company, isin, date, quarter} dicts, `date`
    still the raw 'DD-Mon-YYYY' string NSE returns (e.g. '07-Aug-2026'),
    `quarter` the best-effort 'Q1 FY27'-style label (or None) from
    _parse_quarter -- distinguishes a company's Q1 and Q2 calendar rows from
    each other directly, rather than only by declaration_date proximity."""
    rows = fetch_board_meetings(from_date, to_date)
    seen = set()
    calendar = []
    for r in rows:
        purpose_text = f"{r.get('bm_purpose', '')} {r.get('bm_desc', '')}"
        if RESULTS_BOARD_MEETING_HINT not in purpose_text.lower():
            continue
        key = (r["bm_symbol"], r["bm_date"])
        if key in seen:
            continue
        seen.add(key)
        calendar.append({
            "symbol": r["bm_symbol"], "company": r["sm_name"],
            "isin": r.get("sm_isin"), "date": r["bm_date"],
            "quarter": _parse_quarter(purpose_text),
        })
    return calendar


CALENDAR_CACHE_MAX_AGE = timedelta(hours=24)
CALENDAR_LOOKAHEAD_DAYS = 14  # how far forward to cache upcoming declarations


def _is_calendar_fresh():
    last_updated = db.results_calendar_last_updated()
    if last_updated is None:
        return False
    return (datetime.now(timezone.utc) - last_updated) < CALENDAR_CACHE_MAX_AGE


def refresh_results_calendar(force_refresh=False):
    """Idempotent, safe to call every scan cycle -- no-ops if already
    refreshed within CALENDAR_CACHE_MAX_AGE unless force_refresh (same
    pattern as broker/large_cap.py's refresh()). Fetches a forward window
    (today .. +CALENDAR_LOOKAHEAD_DAYS) so upcoming declarations are cached
    well before they enter the 2-day query window trade_bot.py actually
    queries against, plus a small backward buffer (today - 2) so a same-day
    refresh doesn't miss a just-filed intimation for a meeting that already
    happened. Returns the fetched entries, or None if it no-op'd."""
    if not force_refresh and _is_calendar_fresh():
        return None
    today = db.now().date()
    from_date = (today - timedelta(days=2)).strftime("%d-%m-%Y")
    to_date = (today + timedelta(days=CALENDAR_LOOKAHEAD_DAYS)).strftime("%d-%m-%Y")
    entries = results_calendar(from_date, to_date)
    db.upsert_results_calendar(entries)
    return entries


def is_results_filing(item):
    desc = item.get("desc", "").lower()
    if any(cat in desc for cat in RESULTS_DESC_CATEGORIES):
        return True
    if desc == "outcome of board meeting":
        return RESULTS_BOARD_MEETING_HINT in (item.get("attchmntText") or "").lower()
    return False


def classify(item):
    desc = item.get("desc", "").lower()
    text = f"{desc} {item.get('attchmntText', '')}".lower()
    if any(k in text for k in RISK_KEYWORDS):
        return "risk"
    if any(k in desc for k in DEAL_DESC_CATEGORIES):
        return "deal"
    if is_results_filing(item):
        return "results"
    return "other"


def to_csv_row(item, tag):
    return {
        "timestamp": item["exchdisstime"],
        "symbol": item["symbol"],
        "company": item["sm_name"],
        "isin": item["sm_isin"],
        "category": item["desc"],
        "signal": tag,
        "amount": extract_amount(item["attchmntText"]),
        "summary": item["attchmntText"],
        "filing_url": item["attchmntFile"],
        "seq_id": item["seq_id"],
    }


def write_csv(items_with_tags, path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    write_header = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        if write_header:
            writer.writeheader()
        for item, tag in items_with_tags:
            writer.writerow(to_csv_row(item, tag))


def run_once(show_all, seen, from_date=None, to_date=None):
    announcements = fetch_announcements(from_date, to_date)
    fresh = [a for a in announcements if a["seq_id"] not in seen]
    for a in announcements:
        seen.add(a["seq_id"])

    results = []
    for a in fresh:
        tag = classify(a)
        if show_all or tag in ("deal", "risk", "results"):
            results.append((a, tag))
    results.sort(key=lambda x: x[0]["sort_date"], reverse=True)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true", help="Include every announcement, skip keyword filter")
    parser.add_argument("--watch", type=int, default=None, help="Poll every N seconds instead of running once")
    parser.add_argument("--out", default=DEFAULT_OUT, help=f"CSV path to append to (default: {DEFAULT_OUT})")
    parser.add_argument("--reset-seen", action="store_true", help="Forget previously seen filings before running")
    parser.add_argument("--from-date", default=None, help="Start date DD-MM-YYYY (default: latest 20 filings only)")
    parser.add_argument("--to-date", default=None, help="End date DD-MM-YYYY (required with --from-date)")
    parser.add_argument("--refresh-calendar", action="store_true",
                         help="Force-refresh the results calendar (results_calendar table), print a summary, and exit -- run this daily via cron/launchd")
    args = parser.parse_args()

    if args.refresh_calendar:
        entries = refresh_results_calendar(force_refresh=True)
        print(f"Refreshed results calendar: {len(entries)} companies with results declarations "
              f"in the next {CALENDAR_LOOKAHEAD_DAYS} days.")
        return

    seen = set() if args.reset_seen else load_seen()

    def cycle():
        results = run_once(args.all, seen, args.from_date, args.to_date)
        save_seen(seen)
        run_time = time.strftime("%Y-%m-%d %H:%M:%S")
        if not results:
            print(f"[{run_time}] no new matching filings")
        else:
            write_csv(results, args.out)
            print(f"[{run_time}] appended {len(results)} filings to {args.out}")
        return results

    if args.watch:
        print(f"Polling NSE every {args.watch}s. Ctrl+C to stop.")
        while True:
            cycle()
            time.sleep(args.watch)
    else:
        cycle()


if __name__ == "__main__":
    main()
