#!/usr/bin/env python3
"""
Fixture generator for the mock news server (see mock_news.py).

Headlines are a mix:
  - REAL, fetched live from Google News RSS right now (unauthenticated public
    endpoint -- genuinely downloadable, so no reason to fake these).
  - SYNTHETIC, hand-written, referencing real company names from
    fixtures/upstox_fixture.json -- guarantees coverage of every classifier
    branch (material/non-material order, profit/revenue above and below
    threshold, multi-company roundup, negative events, stale) that a live
    pull can't be relied on to happen to contain today.

Stores age_minutes (an offset), NOT an absolute pub_date -- the mock server
rewrites pubDate to "now - age_minutes" at serve time. trade_bot.py discards
anything older than NEWS_MAX_AGE (1 hour) before classifying it
(_is_fresh_enough), so a fixture baked with real historical timestamps would
arrive pre-stale on every replay and silently test nothing.

Usage:
    ./.venv/bin/python mock_servers/generate_news_fixture.py
"""
import json
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "news"))
import news_bot  # noqa: E402

OUT_PATH = os.path.join(BASE_DIR, "mock_servers", "fixtures", "news_fixture.json")
UPSTOX_FIXTURE_PATH = os.path.join(BASE_DIR, "mock_servers", "fixtures", "upstox_fixture.json")

REAL_QUERIES = [
    "wins order worth India",
    "profit jumps India",
    "posts loss India",
]


def _fetch_real_headlines():
    """Live, unauthenticated Google News RSS -- genuinely real headlines,
    whatever's actually running today. Ages set fresh (a few minutes) since
    we care about real TEXT here, not the real publish timestamp."""
    items = []
    for q in REAL_QUERIES:
        try:
            root = news_bot.fetch_rss(q, lang="en-IN", country="IN")
        except Exception as e:
            print(f"  [warn] real fetch '{q}' failed: {e}", file=sys.stderr)
            continue
        for it in news_bot.parse_items(root, q)[:6]:
            items.append({
                "title": it["title"], "source": it["source"] or "Google News",
                "matched_query": q, "age_minutes": 5, "synthetic": False, "case": "real_live_pull",
            })
    return items


def _synthetic_headlines(names):
    """names: list of real company names (from upstox_fixture.json) to
    reference, so these ground through company_lookup exactly like a real
    headline would -- one per classifier branch that a live pull can't be
    counted on to cover today."""
    n = names
    return [
        {"title": f"{n[0]} bags mega Rs 15,000 crore order from Indian Railways for high-speed rail corridor",
         "source": "Test Wire", "matched_query": "synthetic", "age_minutes": 5, "synthetic": True,
         "case": "order_win_material"},
        {"title": f"{n[1]} wins small Rs 2 crore annual maintenance contract from a municipal corporation",
         "source": "Test Wire", "matched_query": "synthetic", "age_minutes": 5, "synthetic": True,
         "case": "order_win_non_material"},
        {"title": f"{n[2]} reports Q1 net profit surges 42% YoY on strong deal wins, beats street estimates",
         "source": "Test Wire", "matched_query": "synthetic", "age_minutes": 5, "synthetic": True,
         "case": "profit_increase_above_threshold"},
        {"title": f"{n[3]} posts modest 8% rise in quarterly net profit amid steady demand",
         "source": "Test Wire", "matched_query": "synthetic", "age_minutes": 5, "synthetic": True,
         "case": "profit_increase_below_threshold"},
        {"title": f"{n[4]} revenue grows 25% YoY driven by strong export demand",
         "source": "Test Wire", "matched_query": "synthetic", "age_minutes": 5, "synthetic": True,
         "case": "revenue_growth_above_threshold"},
        {"title": f"Buzzing stocks: {n[5]} gains on strong festive sales, {n[6]} slips after regulatory setback, "
                  f"{n[7]} up on fresh export order",
         "source": "Test Wire", "matched_query": "synthetic", "age_minutes": 5, "synthetic": True,
         "case": "multi_company_roundup"},
        {"title": f"{n[8]} loses key supply contract to rival bidder, shares under pressure",
         "source": "Test Wire", "matched_query": "synthetic", "age_minutes": 5, "synthetic": True,
         "case": "order_loss_negative"},
        {"title": f"{n[9]} reports 20% drop in quarterly profit amid weak realizations",
         "source": "Test Wire", "matched_query": "synthetic", "age_minutes": 5, "synthetic": True,
         "case": "profit_decrease_negative"},
        {"title": "Gold prices steady as investors await central bank rate decision",
         "source": "Test Wire", "matched_query": "synthetic", "age_minutes": 5, "synthetic": True,
         "case": "other_irrelevant"},
        {"title": f"{n[10]} announces record order win worth Rs 8,000 crore from state highway authority",
         "source": "Test Wire", "matched_query": "synthetic", "age_minutes": 90, "synthetic": True,
         "case": "stale_should_be_discarded"},
    ]


def _special_case_headlines():
    """Handpicked real anecdotes, not from the generic real/synthetic sets
    above -- each pinned for a specific reason (see comments)."""
    return [
        # Real profit-driven price action on 2026-07-31 (see
        # generate_fixtures.py's SNAPSHOT_CUTOFF_OVERRIDES for SHADOWFAX):
        # day's real 1-min data shows a sharp real rally starting ~12:14pm,
        # eventually reaching a +5%+ intraday move -- this is what actually
        # produces a genuine "target" exit in test runs, instead of every
        # position just sitting flat or getting force-closed at EOD.
        {"title": "Shadowfax Technologies shares surge as company reports strong quarterly profit growth",
         "source": "Test Wire", "matched_query": "special_case", "age_minutes": 5, "synthetic": True,
         "case": "shadowfax_profit_exit_demo"},
        # Real article, pasted verbatim (Moneycontrol, 2026-07-30 16:13 IST)
        # -- published AFTER NSE's 15:30 close, so the market had zero
        # chance to react same-day. Tests the "after-hours news" scenario:
        # does the pipeline correctly wait for the next day's reaction
        # instead of forcing an exit before the market ever got to respond?
        {"title": "Bajaj Finance Q1 results: Net profit rises 27% to Rs 5,986 crore, NII up 24%; "
                  "AUM increase 23% to Rs 4 lakh crore, asset quality improves",
         "source": "Moneycontrol", "matched_query": "special_case", "age_minutes": 5, "synthetic": False,
         "case": "bajaj_finance_after_hours_real"},
    ]


def main():
    with open(UPSTOX_FIXTURE_PATH) as f:
        upstox_fixture = json.load(f)
    names = [rec["name"] for rec in upstox_fixture.values() if rec.get("name")]
    if len(names) < 11:
        sys.exit(f"Need at least 11 named symbols in {UPSTOX_FIXTURE_PATH}, found {len(names)}")

    print("Fetching real live headlines from Google News RSS...")
    real_items = _fetch_real_headlines()
    print(f"  got {len(real_items)} real headlines")

    synthetic_items = _synthetic_headlines(names)
    print(f"  built {len(synthetic_items)} synthetic edge-case headlines")

    special_items = _special_case_headlines()
    print(f"  added {len(special_items)} handpicked special-case headlines")

    all_items = real_items + synthetic_items + special_items
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(all_items, f, indent=2)
    print(f"\nWrote {len(all_items)} items to {OUT_PATH}")


if __name__ == "__main__":
    main()
