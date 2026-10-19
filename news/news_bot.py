#!/usr/bin/env python3
"""
Daily investment-signal news scanner.

Fetches candidate articles from Google News RSS (broad keyword queries, plus
site:-restricted queries against trusted Indian financial publishers) and a
couple of direct-publisher feeds. This module ONLY fetches and shapes raw
items -- classification (event type, company mentions, money amounts) lives
in classifier.py, kept separate so the same enrichment pipeline can run on
items from other sources (e.g. Twitter) later, not just these RSS feeds.

Usage:
    python3 news_bot.py                # scan default queries, write report
    python3 news_bot.py --query "wins order worth"
    python3 news_bot.py --days 2        # only include articles from last N days
"""
import argparse
import html
import os
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # project root
sys.path.insert(0, BASE_DIR)
import classifier  # noqa: E402 -- needs sys.path set up first
import db  # noqa: E402

DEFAULT_QUERIES = [
    "wins order worth India",
    "bags order worth India",
    "secures contract worth India",
    "lands deal worth India",
    "clinches order India",
    "awarded contract worth India",
    "raises funding India",
    "signs deal worth India",
    "receives order worth India",
]

EARNINGS_QUERIES = [
    "profit jumps India",
    "net profit rises India",
    "beats estimates India",
    "revenue grows India",
    "posts loss India",
    "misses estimates India",
]

# NEWS_MOCK_BASE, if set, points every feed below (Google News RSS search AND
# the two direct-publisher feeds) at a local mock server (see
# mock_servers/mock_news.py) instead of the real internet -- for testing
# against a fixed, known corpus instead of whatever's live right now.
NEWS_MOCK_BASE = os.environ.get("NEWS_MOCK_BASE")

# Direct-publisher RSS feeds, faster to update than Google News' own crawl/
# index cycle (Google News RSS can lag real publication by hours). No <source>
# tag in these feeds, so the publisher name is hardcoded per feed instead.
EXTRA_FEEDS = [
    {"url": f"{NEWS_MOCK_BASE}/feed/livemint" if NEWS_MOCK_BASE else "https://www.livemint.com/rss/markets",
     "source": "Livemint Markets"},
    {"url": f"{NEWS_MOCK_BASE}/feed/et" if NEWS_MOCK_BASE else
     "https://economictimes.indiatimes.com/markets/stocks/earnings/rssfeeds/13358319.cms",
     "source": "ET Earnings"},
]

# Indian financial/business publishers that consistently cover order-wins and
# earnings -- verified live, each returns genuinely on-topic results via
# Google News' site: search operator. Restricting to these trades broad
# keyword-only search (misses low-ranking articles, catches almost anything)
# for narrower-but-higher-precision coverage: a plain keyword search competes
# against all of Google News, but site: pre-filters to publishers that reliably
# cover this beat, so more of the top ~100 results per query are relevant.
TRUSTED_DOMAINS = [
    "moneycontrol.com", "economictimes.indiatimes.com", "livemint.com",
    "business-standard.com", "ndtvprofit.com", "cnbctv18.com",
    "zeebiz.com", "financialexpress.com", "businesstoday.in",
    "thehindubusinessline.com",
]
_site_clause = " OR ".join(f"site:{d}" for d in TRUSTED_DOMAINS)
DOMAIN_QUERIES = [
    f"(wins OR bags OR secures OR awarded OR contract OR order) ({_site_clause})",
    f"(profit OR results OR revenue OR earnings) ({_site_clause})",
]

# Global wire services -- NOT India-specific publishers like TRUSTED_DOMAINS
# above (whose entire output is Indian financial news, so a bare site: filter
# is enough). Verified live: an unrestricted site: query against these three
# returns almost entirely US/global stories (Amazon, Boeing, SpaceX, ...).
# Requiring "India" in the query itself (not just the site: filter) narrows
# it to genuinely India-relevant coverage -- verified live too, e.g. Reuters'
# "India's L&T maintains FY27 outlook on strong order inflows...". Kept as a
# separate query group rather than folded into TRUSTED_DOMAINS/_site_clause
# so that group's "site: filter alone is enough" assumption doesn't silently
# change for the original India-only publishers.
GLOBAL_WIRE_DOMAINS = ["reuters.com", "bloomberg.com", "businesswire.com"]
_global_wire_site_clause = " OR ".join(f"site:{d}" for d in GLOBAL_WIRE_DOMAINS)
GLOBAL_WIRE_QUERIES = [
    f"(wins OR bags OR secures OR awarded OR contract OR order) India ({_global_wire_site_clause})",
    f"(profit OR results OR revenue OR earnings) India ({_global_wire_site_clause})",
]

RSS_BASE = f"{NEWS_MOCK_BASE}/rss/search" if NEWS_MOCK_BASE else "https://news.google.com/rss/search"


def fetch_rss(query, lang="en-US", country="US"):
    params = {
        "q": query,
        "hl": lang,
        "gl": country,
        "ceid": f"{country}:{lang.split('-')[0]}",
    }
    url = f"{RSS_BASE}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = resp.read()
    return ET.fromstring(data)


def fetch_direct_feed(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = resp.read()
    return ET.fromstring(data)


# TODO(reliability): pub_date below is trusted as-is from Google News RSS's
# <pubDate>, and it is NOT reliably the article's true original publish
# date -- verified live and real: a genuine 2024-08-02 Dhanuka Agritech Q1
# profit article got re-surfaced by a different publisher with pubDate
# 2026-08-01 (same numbers, same story, two years apart -- confirmed by
# opening the real article, which itself read "06 Aug 2024"). This slipped
# straight through trade_bot.py's NEWS_MAX_AGE freshness gate, which has no
# other source of truth for "when was this actually published" and trusts
# this field completely. trade_bot.py's _looks_like_republished_content
# mitigates the specific symptom (same content, wildly older duplicate
# already on file) after the fact, but there's still no actual reliable way
# to know an article's real publish date up front. Worth investigating:
# fetching the article page itself and parsing its own stated date/byline
# (like the one that exposed this bug), a publisher-reliability allowlist,
# or cross-checking multiple RSS fields/sources before trusting any single
# pubDate.
def parse_items(xml_root, query, source_override=None):
    items = []
    for item in xml_root.findall(".//item"):
        title = html.unescape((item.findtext("title") or "").strip())
        link = (item.findtext("link") or "").strip()
        pub_date_raw = (item.findtext("pubDate") or "").strip()
        source = item.findtext("source")
        source = source.strip() if source else (source_override or "")

        pub_date = None
        for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z"):
            try:
                pub_date = datetime.strptime(pub_date_raw, fmt)
                pub_date = pub_date.astimezone(timezone.utc) if pub_date.tzinfo else pub_date.replace(tzinfo=timezone.utc)
                break
            except ValueError:
                continue

        items.append({
            "title": title,
            "link": link,
            "source": source,
            "pub_date": pub_date,
            "matched_query": query,
        })
    return items


def dedupe(items):
    seen = set()
    unique = []
    for it in items:
        key = it["title"].strip().lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(it)
    return unique


def within_days(items, days):
    """Unlike trade_bot.py's _is_fresh_enough (which keeps unknown-age items
    -- "can't tell, don't discard blindly", appropriate for its narrow 1hr
    buy-decision window), a multi-day backfill has no such excuse: an
    article with no parseable pub_date could just as easily be weeks old as
    recent, and this is explicitly about only storing CONFIRMED-recent news
    -- so an unverifiable age is discarded here, not kept."""
    if days is None:
        return items
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return [it for it in items if it["pub_date"] is not None and it["pub_date"] >= cutoff]


def group_by_company(items):
    """One article can mention multiple companies (roundup headlines) -- it
    appears under every symbol it mentions, not just one. Unmatched articles
    go under "Unclassified"."""
    groups = {}
    for it in items:
        mentions = it["mentioned_companies"]
        keys = [(m["symbol"], m["name"]) for m in mentions] or [("Unclassified", "Unclassified")]
        for symbol, name in keys:
            groups.setdefault((symbol, name), []).append(it)
    return dict(sorted(groups.items(), key=lambda kv: len(kv[1]), reverse=True))


def render_markdown(groups, run_date, days):
    lines = []
    lines.append(f"# Daily investment-signal news digest — {run_date}")
    lines.append("")
    scope = f"last {days} day(s)" if days else "no date filter (Google News default freshness)"
    lines.append(f"_Scope: {scope}. Event classification, company mentions, and deal sizes are best-effort — verify before acting._")
    lines.append("")

    total = sum(len(v) for v in groups.values())
    lines.append(f"**{total} article-mentions across {len(groups)} companies/topics**")
    lines.append("")

    event_emoji = {
        "ORDER_WIN": "🟢", "PROFIT_INCREASE": "🟢", "REVENUE_GROWTH": "🟢",
        "ORDER_LOSS": "🔴", "PROFIT_DECREASE": "🔴", "REVENUE_DECLINE": "🔴",
        "OTHER": "⚪",
    }

    for (symbol, name), arts in groups.items():
        header = f"## {symbol} ({len(arts)})" if symbol == "Unclassified" else f"## {symbol} — {name} ({len(arts)})"
        lines.append(header)
        lines.append("")
        for a in sorted(arts, key=lambda x: x["pub_date"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True):
            tag = event_emoji.get(a["event_type"], "⚪")
            money = f" — **{a['money']}**" if a["money"] else ""
            date_str = a["pub_date"].strftime("%Y-%m-%d") if a["pub_date"] else "unknown date"
            lines.append(f"- {tag} [{a['event_type']}] [{a['title']}]({a['link']}){money}")
            lines.append(f"  - {a['source']} · {date_str} · matched query: \"{a['matched_query']}\"")
        lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", action="append", help="Extra search query (repeatable)")
    parser.add_argument("--only-query", action="store_true", help="Use only --query values, skip defaults")
    parser.add_argument("--days", type=int, default=None, help="Only include articles from the last N days")
    parser.add_argument("--country", default="IN")
    parser.add_argument("--lang", default="en-IN")
    parser.add_argument("--out", default=None, help="Output markdown path (default: reports/<date>.md)")
    parser.add_argument("--model", default=classifier.DEFAULT_MODEL, help="claude CLI model alias for event classification")
    args = parser.parse_args()

    # Fail fast, before spending time on the RSS fetch loop below, if the
    # `claude` CLI isn't usable -- enrich() checks this too, but only after
    # every query/feed above has already been fetched.
    classifier.ensure_available()

    queries = (
        list(args.query or [])
        if args.only_query
        else DEFAULT_QUERIES + EARNINGS_QUERIES + DOMAIN_QUERIES + GLOBAL_WIRE_QUERIES + list(args.query or [])
    )

    all_items = []
    for q in queries:
        try:
            root = fetch_rss(q, lang=args.lang, country=args.country)
        except Exception as e:
            print(f"[warn] query '{q}' failed: {e}", file=sys.stderr)
            continue
        all_items.extend(parse_items(root, q))

    if not args.only_query:
        for feed in EXTRA_FEEDS:
            try:
                root = fetch_direct_feed(feed["url"])
            except Exception as e:
                print(f"[warn] feed '{feed['url']}' failed: {e}", file=sys.stderr)
                continue
            all_items.extend(parse_items(root, query=f"feed:{feed['source']}", source_override=feed["source"]))

    all_items = dedupe(all_items)
    all_items = within_days(all_items, args.days)

    db.init_schema()
    total = len(all_items)
    classified = 0

    def _persist_batch(batch_items):
        nonlocal classified
        for it in batch_items:
            # This standalone tool never runs trade_bot.py's _qualifies()/
            # _decide()/db.add_pending() -- a qualifying item here doesn't
            # get a chance to land in pending_candidates the way it would
            # via a live `scan`. Flagged so stocki's UI doesn't call that
            # "pending review" (see news_items.via_backfill schema notes).
            it["via_backfill"] = True
            db.insert_news_item(it)
        classified += len(batch_items)
        print(f"[classify] {classified}/{total} headlines processed", file=sys.stderr)

    try:
        all_items = classifier.enrich(all_items, model=args.model, on_batch=_persist_batch)
    except RuntimeError as e:
        print(f"[error] {e}", file=sys.stderr)
        raise SystemExit(1)

    groups = group_by_company(all_items)

    run_date = datetime.now().strftime("%Y-%m-%d")
    md = render_markdown(groups, run_date, args.days)

    out_path = args.out or os.path.join(BASE_DIR, "reports", f"{run_date}.md")
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w") as f:
        f.write(md)

    print(f"Wrote {len(all_items)} articles across {len(groups)} groups to {out_path}")


if __name__ == "__main__":
    main()
