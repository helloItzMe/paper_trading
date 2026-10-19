#!/usr/bin/env python3
"""
Paper-trading bot: turns "order/contract win" signals into simulated trades.

No real orders are ever placed -- this tracks hypothetical buys/sells against
live prices so the strategy can be validated before any real capital is used.

All state (open positions, pending review queue, processed news/filings
history, trade log) lives in Postgres (see db.py) -- there are no more
.trade_state.json/.trade_pending.json/.trade_seen.json/paper_trades.csv files.

Two signal sources, merged in `scan`:
    - nse_bot.py: NSE's own corporate-announcements feed, always carries an
      exact ticker (auto-approved -- there's nothing to verify).
    - news_bot.py + classifier.py: broader Google News scan, classified by
      Claude (via the local `claude` CLI) into an event type (order win/loss,
      profit/revenue up/down) and matched to companies by company_lookup's exact
      full-text scanner. Order-win signals auto-approve (the scanner only
      returns real, verified NSE tickers). Earnings-narrative signals
      auto-approve too, but only when profit_pct/revenue_pct is NOT flagged
      low-confidence -- classifier.enrich() regex-extracts the percentage
      deterministically where the headline states one plainly, since the
      LLM's own extraction was verified to silently miss stated percentages
      some of the time; a number the LLM alone produced still needs the LLM's
      own confidence score to clear 50 first (see classifier._combine_pct).
      Anything that doesn't clear that bar queues for manual review via
      `confirm` instead.

Pipeline:
    scan     Pull fresh signals from both sources; auto-open high-confidence
             ones, queue the rest for your manual review (respects
             --max-trades-per-day across both).
    confirm  Walk through queued (low-confidence) signals one at a time; you
             confirm the ticker and approve/reject each one.
    monitor  Check open paper positions against live prices; auto-close on the
             flat 5% profit target or on stop-loss.
    status   Show open positions (with unrealized P&L) and today's trade count.

Buy criteria (news-sourced signals only -- NSE filings keep their existing
order-win/results-filing confidence logic unchanged):
    - Articles older than 1 hour are discarded before classification (Google
      News ranks by relevance, not recency -- old articles waste an LLM call
      and any price reaction has likely already happened).
    - ORDER_WIN: only if the LLM judges the order size material for that company.
    - PROFIT_INCREASE/REVENUE_GROWTH: only if profit_pct or revenue_pct clears
      the threshold for that company's market-cap tier (see MIN_PROFIT_PCT/
      MIN_REVENUE_PCT, _company_tier) -- 35%/20% for everything outside the
      real top 100 by market cap, 20%/10% for large-caps.
Buy-time execution check (both sources, applied right before opening a position):
    - Skip if we already bought this symbol today (open OR closed).
    - Skip if today's max intraday gain (vs previous close) is <=0% (not
      confirmed positive yet) or >=5% (already up too much, likely missed
      the move).
    - Buy only in the 0-5% window -- still early.

Sell: flat +5% target, stop-loss if the stock was already "recently weak"
going in, or a forced end-of-day exit at NSE close (15:30 IST) on the
position's own entry day if neither of those fired first -- otherwise a
trade that never reaches target/stop would sit open indefinitely.

Usage:
    python3 trade_bot.py scan
    python3 trade_bot.py confirm
    python3 trade_bot.py monitor
    python3 trade_bot.py status
"""
import argparse
import difflib
import os
import re
import signal
import sys
import time
from datetime import datetime, timedelta, timezone, time as dtime

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)
sys.path.insert(0, os.path.join(_PROJECT_ROOT, "news"))
sys.path.insert(0, os.path.join(_PROJECT_ROOT, "broker"))
sys.path.insert(0, os.path.join(_PROJECT_ROOT, "analysis"))

import classifier
import db
import news_bot
import nse_bot
import instrument_master
import tick_store
import upstox_client
import upstox_feed

TARGET_PCT = 5.0
STOP_LOSS_PCT = -2.0  # only applied when the stock is already "recently weak"
WEAKNESS_THRESHOLD = -5.0
# Unconditional worst-case floor, applied to EVERY position regardless of the
# "recently weak" check above -- a not-weak-at-entry position that gets
# STOP_LOSS_PCT's target-only exit can still lose more than this before its
# EOD deadline; this exits it the moment it breaches -3% either way.
HARD_STOP_PCT = -3.0

# max_trades_per_day/capital_per_trade/max_capital_allowed have no hardcoded
# fallback here -- they live in Postgres's `config` table (seeded once by
# db.py's schema, adjustable any time via `trade_bot.py config set`), read
# once at startup by _load_config_defaults() below.

IST = timezone(timedelta(hours=5, minutes=30))
NSE_CLOSE_TIME = dtime(15, 30)  # NSE's regular trading session close
NSE_OPEN_TIME = dtime(9, 15)    # NSE's regular trading session open

# Earnings-qualification thresholds, tiered by real market-cap standing:
# "large" is SEBI's actual legal definition (rank 1-100 by full market cap --
# see broker/large_cap.py, backed by NSE's own Nifty 50 + Nifty Next 50
# lists, refreshed periodically like the rest of the instrument master).
# Everything else -- true SME/Emerge-platform listings, mid-caps, small-caps,
# or any main-board company outside the real top 100 -- gets the stricter
# "sme"-named tier (kept that name to match how this was first specified,
# even though it now covers more than literally-SME-listed companies).
# Rationale: a given % profit/revenue move is proportionally a bigger deal
# for a smaller, less-covered company than for a top-100 one, so the bar
# for everything outside the top 100 stays higher.
MIN_PROFIT_PCT = {"large": 20.0, "sme": 35.0}
MIN_REVENUE_PCT = {"large": 10.0, "sme": 20.0}
MAX_ENTRY_GAIN_PCT = 5.0    # don't buy if today's max gain is already >= this

# Third signal source, independent of NSE filings/news: a company scheduled
# (per NSE's own board-meetings calendar, see nse_bot.results_calendar/
# db.get_results_calendar_for_date) to declare results TODAY is itself worth
# watching -- the market moving the stock ahead of or during the declaration
# is itself the signal, no headline needed. Only monitor --tick wires this
# up (see _ResultsTodayWatch) -- it needs a live, continuous feed to catch
# the moment price crosses the threshold, not a periodic scan cycle.
RESULTS_TODAY_GAIN_PCT = 1.0

# How many days back (inclusive of today) monitor --tick's tick capture
# (see analysis/tick_store.py) keeps recording a company's ticks after its
# results declaration -- separate from RESULTS_TODAY_GAIN_PCT's buy-trigger
# candidate set above, which stays today-only. A market's reaction to
# results isn't always fully priced in on the declaration day itself, so
# capture (research-only, doesn't affect any buy/sell decision) casts a
# wider net than the live trading signal does.
RECENT_RESULTS_CAPTURE_DAYS = 5

NEWS_MAX_AGE = timedelta(hours=1)  # discard/never classify articles older than this


def _format_duration(td):
    """Human-readable duration for log output -- str(timedelta) alone
    prints as raw H:MM:SS (e.g. "1:00:00" for one hour), which reads like a
    clock time, not a threshold. Only handles whole hours/minutes (the only
    granularity NEWS_MAX_AGE is ever set to); falls back to str(td) for
    anything with leftover seconds."""
    total_seconds = int(td.total_seconds())
    if total_seconds % 3600 == 0:
        hours = total_seconds // 3600
        return f"{hours} hr" if hours == 1 else f"{hours} hrs"
    if total_seconds % 60 == 0:
        minutes = total_seconds // 60
        return f"{minutes} min" if minutes == 1 else f"{minutes} mins"
    return str(td)

# How far back to look in NSE's forthcoming-board-meetings calendar (see
# nse_bot.results_calendar) for companies to build targeted news queries for.
# 1 day back, not the full multi-week calendar it can return (669+ companies
# over 2 weeks, verified live) -- querying by company name for hundreds of
# tickers a day that haven't declared yet would be mostly wasted Google News
# traffic. Only 1 covers "declared yesterday, results likely still breaking
# today" -- results routinely get released post-market (see the Bajaj
# Finance case elsewhere in this file's docstring: published 16:13 IST,
# after that day's close), so "today" alone would miss it.
RESULTS_CALENDAR_LOOKBACK_DAYS = 1


OFFHOURS_LIMIT_MARKUP_PCT = 2.0  # market-closed queued orders: limit_price = last close * (1 + this/100)


def _is_weekend(dt):
    return dt.astimezone(IST).weekday() >= 5  # Saturday=5, Sunday=6


def _market_is_closed_by_schedule(dt):
    """Fallback schedule check: true outside NSE's regular trading window --
    weekends, OR a weekday before NSE_OPEN_TIME/after NSE_CLOSE_TIME. Fixed
    and deterministic (good for tests), but has no way to know about a
    special session (e.g. Muhurat trading, held on an evening that's
    otherwise a market holiday) or an unscheduled closure -- see
    _market_is_closed, which prefers the live upstox_client.is_market_open()
    API and only falls back to this when that call fails. Distinct from
    _is_weekend, which _is_fresh_enough still uses on its own for the
    news-staleness weekend-carryover -- that's a different concern (how to
    measure a headline's age), not "can we fetch a live price right now"."""
    dt_ist = dt.astimezone(IST)
    if dt_ist.weekday() >= 5:
        return True
    return not (NSE_OPEN_TIME <= dt_ist.time() <= NSE_CLOSE_TIME)


def _market_is_closed():
    """True if the market is NOT tradeable right now. Prefers the live
    upstox_client.is_market_open() API (reflects whatever NSE is ACTUALLY
    doing this instant -- correctly handles special sessions and
    unscheduled closures a fixed schedule can't) -- only falls back to
    _market_is_closed_by_schedule(db.now()) if that call fails (no token,
    network issue, or the mock server in tests/dev, which doesn't implement
    this endpoint). No dt parameter, unlike the schedule fallback -- "is the
    market open" only ever means "right now" for this call's two real
    callers (_open_position's buy check, _process_queued_orders' fill
    check), both of which used to just pass db.now(symbol)/db.now()."""
    try:
        is_open = upstox_client.is_market_open("NSE")
        # Piggybacks on this call rather than adding a separate one: this is
        # already a real, live Upstox API call made every scan/monitor
        # cycle, so its success/failure IS a live token-health signal for
        # free. Recorded so stocki can surface it -- without this, a dead
        # token (verified real gap: upstox_client.py's own `status`
        # subcommand only checks the token file's `issued` date, not a live
        # call, so it can report "valid" for a token Upstox already
        # invalidated server-side) silently falls through to the schedule
        # fallback below with nothing anywhere saying so, for as long as a
        # --watch/--tick loop keeps running past the ~3:30am daily expiry.
        db.record_system_status("upstox_token", True)
        return not is_open
    except RuntimeError as e:
        print(f"[warn] Live market-status check failed ({e}), falling back to the fixed trading-hours schedule.")
        db.record_system_status("upstox_token", False, str(e))
        return _market_is_closed_by_schedule(db.now())


def _next_trading_day_open(dt):
    """Roll dt forward to the next trading day's market open (09:15 IST) --
    only skips Saturday/Sunday, no NSE holiday calendar exists anywhere in
    this codebase (same known simplification as the EOD grace period's
    "calendar day, not trading day" note elsewhere in this file).
    TODO: add a real NSE trading-holiday calendar and use it both here and
    in the EOD grace period's deadline calc -- until then, a holiday
    Monday/Tuesday etc. is silently treated as a trading day by both."""
    dt_ist = dt.astimezone(IST)
    while dt_ist.weekday() >= 5:  # Saturday=5, Sunday=6
        dt_ist = (dt_ist + timedelta(days=1)).replace(
            hour=NSE_OPEN_TIME.hour, minute=NSE_OPEN_TIME.minute, second=0, microsecond=0)
    return dt_ist


def _is_fresh_enough(it):
    """TODO(reliability): this entire function trusts it["pub_date"]
    completely, and that value (from Google News RSS, see news_bot.parse_items'
    own TODO) is not reliably an article's TRUE original publish date --
    it's the concrete reason _looks_like_republished_content exists at all.
    Fixing that at the source (news_bot.py) would remove the need for this
    function to defend against a wrong pub_date after the fact.

    NEWS_MAX_AGE is meant to measure "how long has the market had a
    chance to react to this", not raw clock time -- a weekend article
    published Saturday morning has had ZERO market reaction time by Monday,
    same reasoning as the post-close EOD grace period elsewhere in this file
    (news published after a session had zero chance to move that session's
    price either). Without this, a genuinely fresh weekend order-win/
    earnings headline would always be discarded as "stale" the moment `scan`
    next runs on Monday, since real elapsed time easily exceeds NEWS_MAX_AGE
    (1 hour) by then -- it would never get a chance to qualify at all.

    Deliberately requires age >= 0, i.e. NOT fresh until the market has
    actually reached the rolled-forward reference point (previously just
    `age <= NEWS_MAX_AGE`, which let a NEGATIVE age -- checking before that
    point is even reached -- trivially satisfy the comparison regardless of
    how old the article actually is). Verified live and real: a Saturday-
    dated article, checked at 05:54 Monday (before the 09:15 open), computed
    age = -1 day 20:39:14, satisfied the old `<= NEWS_MAX_AGE` check, and let
    a genuinely stale (in fact mis-dated by 2 YEARS -- a republished article,
    see _looks_like_republished_content) headline straight through to an
    auto-queued buy candidate. This is a deliberate behavior change from the
    original design: a weekend article is no longer treated as "fresh" for
    the whole weekend leading up to market open, only from market open
    onward (same 1-hour window, just not starting early) -- accepted
    tradeoff, since there's no way to tell a legitimate weekend scoop
    checked early apart from old content wearing a fake weekend date using
    timing alone; that distinction is what _looks_like_republished_content
    exists for instead."""
    if it["pub_date"] is None:
        return True  # unknown age -- don't discard blindly, we can't tell
    pub_date = it["pub_date"]
    if _is_weekend(pub_date):
        pub_date = _next_trading_day_open(pub_date)
    age = db.now() - pub_date
    return timedelta(0) <= age <= NEWS_MAX_AGE


def is_order_win(item):
    desc = item.get("desc", "").lower()
    return any(cat in desc for cat in nse_bot.ORDER_DESC_CATEGORIES)


def _nse_candidates(ignore_seen=False):
    """Generator, not a list -- yields each candidate the moment it's ready
    (right after the filing is marked seen) so the caller can decide
    open-or-queue immediately instead of waiting for every filing to be
    fetched first. See _decide()'s docstring for why this matters."""
    announcements = nse_bot.fetch_announcements()
    for a in announcements:
        if not ignore_seen and db.is_nse_seen(a["seq_id"]):
            continue
        filing_id = db.insert_nse_filing(a, nse_bot.classify(a))

        if is_order_win(a) and _company_tier(a["symbol"]) == "large":
            # Real top-100-by-market-cap companies win orders constantly as
            # part of normal business -- not the kind of signal that moves
            # their stock (see _qualifies' identical ORDER_WIN/large-cap
            # note). Still recorded in nse_filings above for the audit
            # trail, just never yielded as a buy candidate -- same full-
            # discard treatment as news-sourced large-cap order wins.
            continue
        if is_order_win(a):
            # NSE's own feed always carries the exact ticker -- deal size/direction is unambiguous.
            confidence = "auto"
        elif nse_bot.is_results_filing(a):
            # Confirms results were FILED, not whether profit/revenue rose or fell (that's in
            # the attached PDF, unparsed) -- always manual, this is a "go check" signal only.
            confidence = "manual"
        else:
            continue
        yield {
            "source": "nse", "source_id": filing_id, "symbol": a["symbol"], "company": a["sm_name"],
            "confidence": confidence,
            "detail": a["desc"], "summary": a.get("attchmntText", ""), "link": a.get("attchmntFile", ""),
        }


def _company_tier(symbol):
    """'large' only if genuinely ranked in NSE's real top 100 by market cap
    (Nifty 50 + Nifty Next 50 -- see broker/large_cap.py), else 'sme'. A
    company db.get_company() doesn't know about, or whose cap_tier hasn't
    been refreshed/set, correctly falls through to 'sme' (the stricter
    tier) rather than defaulting to the easier bar."""
    rec = db.get_company(symbol)
    if rec and rec.get("cap_tier") == "large":
        return "large"
    return "sme"


# Verb + percentage close together (e.g. "Share Price Gains 5%", "XYZ Zooms
# 8% On Order Win") -- the headline itself is reporting that the STOCK'S
# PRICE already moved by this much, distinct from profit_pct/revenue_pct
# (which are the actual qualifying signal for PROFIT_INCREASE/REVENUE_GROWTH
# and never get filtered by this). Deterministic regex, not the LLM -- same
# reasoning as MONEY_RE in classifier.py: this is a mechanical text pattern,
# not a judgment call.
_PRICE_MOVE_RE = re.compile(
    r"\b(?:gain|gains|surge|surges|jump|jumps|soar|soars|rally|rallies|climb|climbs|"
    r"spike|spikes|zoom|zooms|rise|rises|rebound|rebounds|advance|advances)\b"
    r"[^%\n]{0,15}?\d+(?:\.\d+)?%",
    re.IGNORECASE,
)


def _price_already_moved(title):
    """True if the headline claims the stock's price has ALREADY moved by
    some percentage -- e.g. real case: "Inox India Share Price Gains 5% On
    Rs 939-Crore Worth Multiple Order Wins". Buying on an order-win headline
    whose own text says the market already reacted is buying after the move,
    which is exactly what this project's paper-trading strategy exists to
    avoid, regardless of what the live Upstox intraday-gain check happens to
    read at that moment (see MAX_ENTRY_GAIN_PCT's known limitation: it can't
    tell an old, already-priced-in move from a brand new one)."""
    return bool(_PRICE_MOVE_RE.search(title))


REPUBLISH_SIMILARITY_THRESHOLD = 0.85
# Deliberately generous -- multiple outlets legitimately covering the SAME
# real, fresh declaration within a day or two of each other is normal and
# must NOT be flagged; only a gap far wider than any real news cycle should
# trigger this (verified live case below: ~2 YEARS).
REPUBLISH_LOOKBACK_MIN_GAP = timedelta(days=7)

_TITLE_SUFFIX_RE = re.compile(r"\s*-\s*[^-]+$")  # trailing " - <source>"
_TITLE_PUNCT_RE = re.compile(r"[‘’′'.,]")


def _normalize_title_core(title):
    """Strip the trailing ' - <source>' suffix and normalize case/
    punctuation, so two publishers' renderings of the SAME underlying
    content compare as near-identical text regardless of apostrophe style,
    period placement, or which outlet ran it."""
    t = _TITLE_SUFFIX_RE.sub("", title)
    t = _TITLE_PUNCT_RE.sub("", t.lower())
    return re.sub(r"\s+", " ", t).strip()


def _looks_like_republished_content(company, title, pub_date, as_of=None):
    """True if a near-identical article for this company already exists in
    news_items with a MEANINGFULLY OLDER pub_date -- catches Google News
    re-surfacing old content with a fresh-looking pub_date, which would
    otherwise sail straight through NEWS_MAX_AGE's freshness gate entirely
    (that gate only ever looks at the claimed pub_date, which is exactly
    what's wrong here). Verified live and real: a genuine 2024-08-02 Dhanuka
    Agritech Q1 profit article ("48.5% YOY to Rs 48.90 Crore") got
    re-surfaced by a different publisher with pub_date 2026-08-01 -- same
    numbers, same story, two years apart (confirmed by opening the actual
    article, which itself read "06 Aug 2024") -- and passed straight
    through to an auto-queued buy candidate before this check existed.

    Searches by COMPANY NAME (db.get_news_titles_matching), not symbol, and
    NOT joined through news_item_companies -- verified live that the real
    2024 duplicate above was itself correctly marked event_type='STALE',
    and STALE articles never get resolved to a company at all (their
    mentioned_companies is cleared), so a company-resolution-based lookup
    would silently miss exactly the rows this check exists to find.

    as_of is passed straight through to db.get_news_titles_matching -- see
    its docstring. Real production callers (cmd_scan) never pass it; only
    analysis/backtest.py does, to replay this check against the DB as it
    existed at the time, not as it looks now."""
    if pub_date is None or not company:
        return False
    search_term = _CORP_SUFFIX_RE.sub("", company).strip()
    normalized = _normalize_title_core(title)
    for other_title, other_pub_date in db.get_news_titles_matching(search_term, as_of=as_of):
        if other_pub_date is None or other_pub_date >= pub_date - REPUBLISH_LOOKBACK_MIN_GAP:
            continue  # not meaningfully older -- legitimate same-week multi-source coverage, not a republish
        if difflib.SequenceMatcher(None, normalized, _normalize_title_core(other_title)).ratio() >= REPUBLISH_SIMILARITY_THRESHOLD:
            return True
    return False


def _qualifies(it, symbol=None, company=None, as_of=None):
    """New-signal quality bar for news-sourced candidates (NSE filings keep
    their own existing confidence logic, unaffected by this):
        - Never qualifies if _looks_like_republished_content flags it (using
          `company`, not `symbol` -- see that function's docstring for why)
          as a near-duplicate of a MUCH older article -- checked first,
          before any event-type-specific logic, since it's a "this isn't
          actually fresh information at all" problem, not a category-
          specific one. No-op when company isn't provided. `as_of` passes
          straight through to it -- see _looks_like_republished_content's
          and db.get_news_titles_matching's docstrings; real production
          calls never set this.
        - ORDER_WIN: never qualifies for a genuinely large-cap company (see
          _company_tier) -- real top-100-by-market-cap companies win orders
          constantly as part of normal business, so one more order win isn't
          the kind of signal that moves their stock; only material for
          companies outside the real top 100 (symbol=None falls back to
          'sme', i.e. NOT excluded by this check, matching the
          "unresolved company never gets the easier bar" rule below --
          being outside the large-cap exclusion is the stricter/harder
          outcome here, the opposite of the profit/revenue bar). Also
          requires the LLM judged the order size material, AND that the
          headline itself doesn't already claim the stock's price moved (see
          _price_already_moved) -- a headline announcing its own price
          reaction is reporting a move that already happened, not a fresh one
          to trade on.
        - PROFIT_INCREASE/REVENUE_GROWTH: only if profit_pct or revenue_pct
          clears the threshold for THIS company's market-cap tier (see
          MIN_PROFIT_PCT/MIN_REVENUE_PCT) -- symbol=None (e.g. no company
          resolved yet) falls back to the STRICTER 'sme' bar (35%/20%), not
          'large' (20%/10%): an unresolved/unverified company should never
          get the easier bar by default, only the harder one. profit_pct/
          revenue_pct are themselves percentages, but they're the qualifying
          signal here, not a price-move claim, so _price_already_moved is
          never applied to this branch.
        - anything else: never qualifies as a buy candidate."""
    if company and _looks_like_republished_content(company, it["title"], it.get("pub_date"), as_of=as_of):
        return False
    if it["event_type"] == "ORDER_WIN":
        tier = _company_tier(symbol) if symbol else "sme"
        if tier == "large":
            return False
        return it.get("order_is_material", False) and not _price_already_moved(it["title"])
    if it["event_type"] in ("PROFIT_INCREASE", "REVENUE_GROWTH"):
        # TODO(decision quality): profit_pct/revenue_pct is trusted as-is from
        # whichever single article the classifier happened to read, with no
        # independent verification against a real historical baseline (last
        # year's/last quarter's actual filed numbers) and no reconciliation
        # across sources when they disagree. Verified live and disagreeing
        # hard: for the same MGEL Q1 FY27 declaration, one article reported
        # "profit at Rs.8.43 Cr", another "profit rises 28% YoY to Rs.7.53
        # lakh" -- an ~112x discrepancy for supposedly the same figure
        # (standalone vs consolidated, or a real reporting error somewhere --
        # either way, nothing here can tell which). Before this qualifies a
        # real position, worth cross-checking multiple sources for the same
        # declaration (results_calendar.earliest_news_item_id only tracks
        # ONE, the earliest, not all matches) and/or computing the % change
        # from actual historical data instead of trusting an article's stated
        # number. Same underlying risk CLAUDE.md already flags for LLM
        # extraction generally; this is the concrete case that proves it.
        #
        # Related sub-issue, same root cause, not yet covered above: neither
        # classifier.extract_profit_pct_regex/extract_revenue_pct_regex nor
        # the LLM extraction distinguish WHAT the extracted percentage is
        # actually comparing against -- year-over-year (YoY), quarter-over-
        # quarter (QoQ/sequential), or something else entirely (verified:
        # zero YoY/QoQ-aware logic anywhere in classifier.py). A "44%" pulled
        # from a headline is trusted as-is regardless of which comparison
        # basis it's on, and QoQ vs YoY for the SAME company's SAME quarter
        # can differ wildly (a strong YoY number can mask a weak or negative
        # QoQ trend, or vice versa) -- MIN_PROFIT_PCT/MIN_REVENUE_PCT apply
        # the same threshold regardless. Worth extracting and requiring a
        # specific, known comparison basis (or computing QoQ/YoY directly
        # from real historical filed figures, same fix as the cross-source
        # verification note above) rather than trusting an ambiguous
        # "X% growth" claim at face value.
        tier = _company_tier(symbol) if symbol else "sme"
        profit_pct, revenue_pct = it.get("profit_pct"), it.get("revenue_pct")
        return (profit_pct is not None and profit_pct >= MIN_PROFIT_PCT[tier]) or \
               (revenue_pct is not None and revenue_pct >= MIN_REVENUE_PCT[tier])
    return False


def _companies_for_item(it):
    """One headline can mention multiple companies (roundup-style headlines) --
    yield a candidate per company mentioned, not just one per article.
    Confidence follows the LLM's category call: order-wins are unambiguous
    enough to auto-approve. Earnings narratives auto-approve too, but only
    when classifier.enrich() marked the extracted profit_pct/revenue_pct as
    NOT low-confidence (see classifier._combine_pct) -- a regex-backed number,
    or an LLM number the LLM itself was >=50% confident in. A low-confidence
    number (or the LLM missing it while regex also found nothing) still lands
    on manual review, same as before this distinction existed. Qualification
    is checked PER COMPANY, not once for the whole headline -- a roundup could
    mention companies of different listing tiers, each with its own threshold."""
    if it["category"] == "order":
        confidence = "auto"
    elif it["category"] == "earnings":
        confidence = "manual" if it.get("pct_low_confidence", True) else "auto"
    else:
        return {}
    return {m["symbol"]: (m["name"], confidence, f"{it['event_type']} ({m['match_type']} mention)")
            for m in it["mentioned_companies"] if _qualifies(it, m["symbol"], m["name"])}


_APOSTROPHE_RE = re.compile(r"[‘’'`]")  # straight ', curly '/', backtick
_CORP_SUFFIX_RE = re.compile(r"\b(limited|ltd\.?|private|pvt\.?)\b", re.IGNORECASE)

# A bare symbol match is unreliable whenever the ticker happens to be a
# common word/acronym -- verified live and wrong: symbol IPL (India
# Pesticides Limited) matched a real-but-irrelevant headline ("Google's AI
# platform Gemini signs three-year IPL sponsorship deal"), because "IPL"
# also means the cricket league. Same risk exists for other real tickers in
# this data (SUN, GOLD, RAIN, SAIL, NH, ...). Requiring an earnings-context
# word alongside the symbol match filters that out -- a genuine
# ticker-abbreviated results headline ("CDSL Q1 Results...", "...to announce
# earnings") always carries one of these; a coincidental word match doesn't.
_RESULTS_CONTEXT_RE = re.compile(r"\b(results?|profit|revenue|earnings|pat|q[1-4]|fy\d{2}|quarter)\b", re.IGNORECASE)

# _RESULTS_CONTEXT_RE alone can't tell "results were actually declared, here
# are the real figures" apart from "here's who's expected to declare
# results today/soon" -- both contain "results"/"earnings"/"Q1" equally.
# Verified live and real, and much wider-reaching than it first looked: one
# roundup article ("Stocks to Watch: DLF, Torrent Power, Jindal Stainless
# and 7 Other Companies Announcing Q1 Results Today") matched and marked
# results_calendar "news found" for ALL NINE companies it named -- DLF,
# TORNTPOWER, JSL, IREDA, KEI, ESCORTS, UPL, SUNDARMFIN, INOXINDIA -- none
# of which had actually reported anything; the article only previewed who
# was due to report that day. This is worse than a no-op: marking
# news_fetched_at/earliest_news_at from a preview STOPS that company from
# ever being re-queried once its real declaration actually breaks later
# (see db.mark_results_news_found's docstring on why "found" must mean
# genuinely found, not merely attempted).
_PREVIEW_LANGUAGE_RE = re.compile(
    r"stocks? to watch|to announce|to hold\b.*\bcall|announcing.*(today|tomorrow)|"
    r"among (the )?(firms|companies) to announce|scheduled (for|to)|"
    r"set to (announce|declare)|yet to announce|board (to meet|to convene)|"
    r"meets? tomorrow|convenes? tomorrow|earnings call scheduled",
    re.IGNORECASE,
)


def _has_genuine_results_context(text):
    """True only if `text` carries a results-context word (_RESULTS_CONTEXT_RE)
    AND doesn't read as a preview/calendar mention (_PREVIEW_LANGUAGE_RE) --
    see that regex's docstring for the real false-positive class this
    guards against. Shared by both places that need "is this genuinely
    reporting a declared result" -- the dedicated-query branch and
    _item_matches_calendar_entry's per-clause check below."""
    return bool(_RESULTS_CONTEXT_RE.search(text)) and not _PREVIEW_LANGUAGE_RE.search(text)


def _normalize_company_name(name):
    """Lowercase, strip apostrophe-variant characters and common corporate
    suffixes -- title-matching a company name against real headlines needs
    this to survive real-world inconsistencies. Verified live: NSE's own
    `sm_name` for DIVISLAB is "Divi's Laboratories Limited" (straight
    apostrophe, U+0027), but a real CNBC TV18 headline covering its actual
    Q1 declaration read "Divi's Laboratories Q1 profit surges..." with a
    CURLY apostrophe (U+2019) and no "Limited" -- looked like a non-match to
    a plain substring check even though it's obviously the same company, and
    silently left a real, already-fetched, on-topic article unmatched."""
    normalized = _APOSTROPHE_RE.sub("", name.lower())
    normalized = _CORP_SUFFIX_RE.sub("", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def _item_matches_calendar_entry(it, company_normalized, symbol_re):
    """True if `it`'s title is genuinely about this calendar entry's
    company: the company name or symbol co-occurs with a GENUINE
    results-context word (_has_genuine_results_context -- not just any
    results-context word, see its docstring for why) within the SAME
    semicolon-separated clause of the title. Per-clause, not anywhere-in-
    title, on purpose -- verified live and wrong before this: "Q3 Results
    impact: Havells India shares drop 7% as profit misses estimates; Aarti
    Surfactants, Tips Music... - Moneycontrol.com" matched Aarti
    Surfactants' calendar entry even though the article is about Havells --
    Aarti Surfactants is only named in the second clause, alongside other
    unrelated movers, with no results context of its own there. A
    multi-company "today's reporters" list headline (no semicolon) stays
    one clause and still matches correctly -- PROVIDED it's a genuine
    report, not a preview (see _has_genuine_results_context)."""
    for clause in it["title"].split(";"):
        if not _has_genuine_results_context(clause):
            continue
        clause_normalized = _normalize_company_name(clause)
        if company_normalized in clause_normalized or symbol_re.search(clause):
            return True
    return False


def _results_calendar_pending():
    """Symbols whose NSE-scheduled results declaration falls within
    RESULTS_CALENDAR_LOOKBACK_DAYS of today and for whom matching news
    hasn't been found yet -- see db.get_pending_results_calendar_news's
    docstring for why "not found yet" (not "not queried yet") is the right
    condition. Returns [{symbol, company, declaration_date, query}], `query`
    being a company-name-quoted search -- rather than the generic keyword/site:
    patterns above -- since the whole point is surfacing coverage of
    smaller/less-covered companies a broad query would miss.

    Reads the DB-cached calendar (db.get_pending_results_calendar_news), not
    a live NSE fetch -- refresh_results_calendar() below is a cheap no-op
    unless the cache is >24h stale, so the real NSE board-meetings fetch
    happens ~once/day (via cron running `nse_bot.py --refresh-calendar`, or
    this call as a safety net if that cron isn't set up), not every 5-minute
    scan cycle. Does NOT mark anything as resolved -- that only happens in
    _news_candidates, once a query here has actually found something."""
    try:
        nse_bot.refresh_results_calendar()
    except Exception as e:
        print(f"[warn] results calendar refresh failed: {e}", file=sys.stderr)
        # Still worth trying the DB read below -- a failed refresh just means
        # we're working off whatever was cached from a previous successful run.

    to_date = db.now().date()
    from_date = to_date - timedelta(days=RESULTS_CALENDAR_LOOKBACK_DAYS)
    pending = db.get_pending_results_calendar_news(from_date, to_date)
    return [
        {"symbol": row["symbol"], "company": row["company"], "declaration_date": row["declaration_date"],
         "query": f'"{row["company"]}" results'}
        for row in pending
    ]


def _news_candidates(on_candidate, model=None, ignore_seen=False):
    """Calls on_candidate(c) as each classified batch is persisted, instead of
    returning a list -- see _decide()'s docstring for why decisions can't wait
    for the whole run (NSE + every news batch) to finish first."""
    # Fail fast, before spending time on the RSS fetch loop below, if the
    # `claude` CLI isn't usable -- enrich() checks this too, but that's after
    # already fetching every query/feed.
    classifier.ensure_available()

    calendar_pending = _results_calendar_pending()

    all_items = []
    queries = (news_bot.DEFAULT_QUERIES + news_bot.EARNINGS_QUERIES + news_bot.DOMAIN_QUERIES +
               news_bot.GLOBAL_WIRE_QUERIES + [c["query"] for c in calendar_pending])
    for q in queries:
        try:
            root = news_bot.fetch_rss(q, lang="en-IN", country="IN")
        except Exception as e:
            print(f"[warn] news query '{q}' failed: {e}", file=sys.stderr)
            continue
        all_items.extend(news_bot.parse_items(root, q))
    for feed in news_bot.EXTRA_FEEDS:
        try:
            root = news_bot.fetch_direct_feed(feed["url"])
        except Exception as e:
            print(f"[warn] news feed '{feed['url']}' failed: {e}", file=sys.stderr)
            continue
        all_items.extend(news_bot.parse_items(root, query=f"feed:{feed['source']}", source_override=feed["source"]))

    all_items = news_bot.dedupe(all_items)
    fresh = all_items if ignore_seen else [it for it in all_items if not db.is_news_seen(it["title"])]

    # Google News search returns whatever ranks for the query regardless of
    # date, not just recent articles -- a large chunk of "fresh" (not-yet-seen)
    # items are often old news we just haven't processed yet, not new
    # signals. Discard anything older than NEWS_MAX_AGE before spending an
    # LLM call on it: by the time we'd see week-old news, any price reaction
    # already happened. Still recorded (event_type="STALE") so we stop
    # re-considering the same old article every cycle.
    stale = [it for it in fresh if not _is_fresh_enough(it)]
    fresh = [it for it in fresh if _is_fresh_enough(it)]
    for it in stale:
        it["money"], it["mentioned_companies"] = None, []
        it["event_type"], it["category"], it["signal"] = "STALE", "other", "neutral"
        it["profit_pct"], it["revenue_pct"], it["order_is_material"] = None, None, False
        it["_news_item_id"] = db.insert_news_item(it)
    if stale:
        noun = "article" if len(stale) == 1 else "articles"
        print(f"[skip] {len(stale)} {noun} older than {_format_duration(NEWS_MAX_AGE)} discarded, not classified.", file=sys.stderr)

    total = len(fresh)
    classified = 0

    def _persist_batch(batch_items):
        nonlocal classified
        for it in batch_items:
            it["_news_item_id"] = db.insert_news_item(it)
        classified += len(batch_items)
        print(f"[classify] {classified}/{total} headlines processed", file=sys.stderr)

        # Decide each of this batch's candidates now, right after they're
        # marked seen -- not after every remaining batch finishes classifying
        # too. A crash further into the run no longer strands this batch's
        # signals in seen-but-never-decided limbo.
        for it in batch_items:
            if it["signal"] != "positive":
                continue
            for symbol, (company, confidence, basis) in _companies_for_item(it).items():
                on_candidate({
                    "source": "news", "source_id": it["_news_item_id"], "symbol": symbol, "company": company,
                    "confidence": confidence,
                    "detail": it["title"], "summary": basis, "link": it["link"],
                })

    classifier.enrich(fresh, model=model or classifier.DEFAULT_MODEL, on_batch=_persist_batch)

    # Resolve results-calendar hits against EVERY fetched item (all_items,
    # post-dedupe -- items in `stale` and `fresh` are the SAME dict objects,
    # just partitioned, so mutations from the loops above are already
    # visible here), not just the dedicated per-company query's own results
    # -- a well-covered company (e.g. a large-cap like MUTHOOTFIN/CDSL)
    # routinely shows up in the generic EARNINGS_QUERIES/DOMAIN_QUERIES too,
    # sometimes with an earlier pub_date than whatever the dedicated query
    # happens to surface.
    #
    # Runs AFTER classifier.enrich() (not right after fetching, like the
    # first version of this did) specifically so every matched item already
    # has a real news_items.id to link -- db.mark_results_news_found's
    # earliest_news_item_id needs one. Items go one of three ways by the
    # time this runs: stale (inserted just above, id captured), classified
    # (inserted via _persist_batch, id captured), or an already-seen
    # duplicate from a prior scan (never inserted THIS cycle at all, so no
    # id was captured here) -- that last case falls back to
    # db.get_news_item_id_by_title, since its row already exists from
    # whichever earlier scan first saw it (title is UNIQUE).
    #
    # Two ways an item can count, verified live against real headlines:
    #   1. it came from the dedicated query itself (matched_query == c["query"])
    #      AND carries a results-context word. The query being dedicated
    #      only proves the article is about the right COMPANY, not that
    #      it's about RESULTS -- verified live and wrong before this: the
    #      dedicated queries for Sejal Glass / MIC Electronics / Bluspring
    #      Enterprises each matched a real, on-topic, correctly-attributed
    #      article that was actually about an unrelated ORDER-WIN contract,
    #      not a results declaration, and got marked "results found" anyway.
    #   2. the company name or symbol CO-OCCURS WITH a results-context word
    #      in the SAME clause of the title (see _item_matches_calendar_entry)
    #      -- catches two real false-positive classes, both verified live:
    #      a bare symbol match with no context at all (ticker IPL matching
    #      an article about the IPL cricket league, not India Pesticides
    #      Limited), and a company name appearing in a DIFFERENT clause than
    #      the article's actual subject (a roundup headline -- "Q3 Results
    #      impact: Havells India shares drop 7%...; Aarti Surfactants, Tips
    #      Music..." -- naming Aarti Surfactants only as an unrelated "also
    #      moving" aside, not because ITS results were declared).
    #
    # Only counts articles published on/after the declaration date itself --
    # excludes old, unrelated matches (verified live: a quoted-company-name
    # query can still surface stale noise, e.g. a loosely-matched article
    # about a different company entirely) from corrupting "earliest" or
    # triggering a false "found it" on pure noise.
    for c in calendar_pending:
        cutoff = datetime.combine(c["declaration_date"], dtime.min, tzinfo=IST)
        company_normalized = _normalize_company_name(c["company"])
        symbol_re = re.compile(rf'\b{re.escape(c["symbol"])}\b', re.IGNORECASE)
        matches = [
            it for it in all_items
            if it["pub_date"] and it["pub_date"] >= cutoff
            and ((it["matched_query"] == c["query"] and _has_genuine_results_context(it["title"]))
                 or _item_matches_calendar_entry(it, company_normalized, symbol_re))
        ]
        # Filter OUT genuine multi-company roundup articles -- verified live
        # and real: a single "Stocks to Watch: DLF, Torrent Power, Jindal
        # Stainless and 7 Other Companies Announcing Q1 Results Today"
        # article ended up marking NINE unrelated companies' calendar
        # entries as "news found" here, none of which had actually
        # reported anything. _has_genuine_results_context (preview-language
        # exclusion, above) closes the specific case where the article ALSO
        # reads as a preview, but a genuinely-worded multi-company article
        # (several real companies' results reported together, no preview
        # language) would still slip through without this separate check --
        # an article resolving to MORE THAN ONE company at all is
        # inherently ambiguous about which one this specific match is
        # really about, same reasoning CLAUDE.md's manual-review plan
        # already called for. Filter, don't skip the whole entry: if the
        # only match happens to be multi-company, a later single-company
        # article about this same declaration should still be able to mark
        # it. A company-less match (0 rows -- e.g. a STALE article that
        # never went through company resolution at all, see
        # _looks_like_republished_content's docstring for why that
        # matters) is NOT itself multi-company ambiguity, so it stays
        # eligible.
        matches = [
            it for it in matches
            if db.count_news_item_companies(it.get("_news_item_id") or db.get_news_item_id_by_title(it["title"]) or -1) <= 1
        ]
        if matches:
            earliest = min(matches, key=lambda it: it["pub_date"])
            news_item_id = earliest.get("_news_item_id") or db.get_news_item_id_by_title(earliest["title"])
            db.mark_results_news_found(c["symbol"], c["declaration_date"], earliest["pub_date"], news_item_id)


def _queue_offhours_order(candidate, capital_per_trade):
    """Market-closed equivalent of the live-price path below (weekend OR a
    weekday outside 09:15-15:30 IST -- see _market_is_closed): there's no
    live intraday data to run the usual gain-band check against, but real
    brokers don't make you wait until the next session to act either -- they
    accept an After Market Order now and fill it at the next session's open.
    Mirrors that: place a buy-LIMIT order immediately at last close * (1 +
    OFFHOURS_LIMIT_MARKUP_PCT/100). _process_queued_orders() fills it once
    the market reopens, ONLY if the live price then is at or below this
    limit (a real limit order never fills worse than its limit) -- that
    ceiling is what stands in for the 0-5% gain-band check here, since
    there's no live gain to check yet."""
    symbol, company = candidate["symbol"], candidate["company"]
    if db.is_position_queued(symbol):
        print(f"[skip] {symbol}: already has a queued order.")
        return False
    try:
        last_close = upstox_client.get_last_close(symbol)
    except RuntimeError as e:
        print(f"[skip] Can't fetch a last close for {symbol}: {e}")
        return False
    if not last_close:
        print(f"[skip] No historical close for {symbol} -- can't set a limit order.")
        return False
    limit_price = last_close * (1 + OFFHOURS_LIMIT_MARKUP_PCT / 100)
    db.queue_offhours_position(
        symbol, company, candidate["source"], candidate.get("source_id"), candidate.get("detail"),
        candidate["confidence"], limit_price, capital_per_trade, db.now(symbol),
    )
    print(f"[queued] {symbol}: market closed -- buy-limit order placed at Rs.{limit_price:.2f} "
          f"(last close Rs.{last_close:.2f} +{OFFHOURS_LIMIT_MARKUP_PCT:.0f}%); "
          f"fills at next market open only if price is at or below this.")
    return True


def _open_position(candidate, capital_per_trade, max_trades_per_day, max_capital_allowed, interactive=False):
    """Returns None on failure/skip, "opened" if a real position was bought,
    or "queued" if the market's closed and a limit order was placed instead
    (see _queue_offhours_order) -- callers that only care "did something
    happen" can still just check truthiness, but _decide() needs the
    distinction to report accurate opened-vs-queued counts."""
    symbol, company = candidate["symbol"], candidate["company"]

    if db.bought_today(symbol):
        print(f"[skip] Already bought {symbol} today.")
        return None

    if _market_is_closed():
        return "queued" if _queue_offhours_order(candidate, capital_per_trade) else None

    try:
        max_gain_pct = upstox_client.get_intraday_gain_pct(symbol)
    except RuntimeError as e:
        print(f"[skip] Can't fetch intraday data for {symbol}: {e}")
        return None
    if max_gain_pct is None:
        print(f"[skip] No intraday data for {symbol} yet (e.g. before market open).")
        return None
    if not (0 < max_gain_pct < MAX_ENTRY_GAIN_PCT):
        reason = "already up too much today, likely missed the move" if max_gain_pct >= MAX_ENTRY_GAIN_PCT \
            else "flat or negative today, not confirmed higher yet"
        print(f"[skip] {symbol}: today's max gain so far is {max_gain_pct:+.2f}% -- {reason}.")
        return None

    try:
        price = upstox_client.get_current_price(symbol)
    except RuntimeError as e:
        print(f"[skip] Can't fetch a price for {symbol}: {e}")
        return None
    if price is None:
        print(f"[skip] No price data for {symbol} -- can't confirm this ticker.")
        return None

    perf = upstox_client.get_recent_performance(symbol)
    perf_str = ", ".join(f"{k}: {v:+.1f}%" if v is not None else f"{k}: n/a" for k, v in perf.items())
    weak = upstox_client.is_recently_weak(perf, WEAKNESS_THRESHOLD)
    print(f"Current price: Rs.{price:.2f} | Today's max gain so far: {max_gain_pct:+.2f}% | Recent perf: {perf_str}")
    print(f"[risk] Stop-loss at {STOP_LOSS_PCT:.0f}% will apply." if weak else
          "[risk] No recent weakness detected -> no stop-loss, target-only exit.")
    print(f"[risk] Hard stop at {HARD_STOP_PCT:.0f}% will apply regardless.")

    if interactive:
        answer = input(f"Confirm ticker {symbol} and open paper position? [y/N/skip]: ").strip().lower()
        if answer != "y":
            print("Skipped.")
            return None

    stop_loss_pct = STOP_LOSS_PCT if weak else None
    position_id, lost_race = db.try_open_position(
        symbol, company, candidate["source"], candidate.get("source_id"), candidate.get("detail"),
        candidate["confidence"], price, db.now(symbol), TARGET_PCT, TARGET_PCT,
        stop_loss_pct, capital_per_trade, max_trades_per_day, max_capital_allowed,
        hard_stop_pct=HARD_STOP_PCT,
    )
    if position_id is None:
        why = {"already_bought": "already bought today", "quota": "daily trade quota reached",
               "capital": "max capital allowed would be exceeded"}[lost_race]
        print(f"[skip] {symbol}: {why} (by a concurrent run, since this check).")
        return None
    print(f"[opened] Paper position: {symbol} bought at Rs.{price:.2f}, target +{TARGET_PCT}%, "
          f"stop-loss {stop_loss_pct}, hard stop {HARD_STOP_PCT}%")
    return "opened"


def _open_results_today_position(symbol, company, price, gain_pct, capital_per_trade, max_trades_per_day, max_capital_allowed):
    """Open a position on the results-today price trigger (see
    RESULTS_TODAY_GAIN_PCT) -- called from a live tick, so `price` is
    already known and doesn't need a fresh REST fetch. Deliberately skips
    _open_position's own MAX_ENTRY_GAIN_PCT gain-BAND check: that band exists
    to catch a NEWS-sourced candidate that's already moved too far before we
    even saw the headline, but here the live gain crossing
    RESULTS_TODAY_GAIN_PCT IS the signal itself, not a staleness filter on
    a separately-sourced one -- there's no "too early" state to wait out and
    no upper band to miss, the trigger only fires once, right at the cross.
    Still runs every other real safety check _open_position does: same-day
    dedup, market-hours, recent-weakness-based stop-loss, and the same
    atomic db.try_open_position race guard."""
    if db.bought_today(symbol):
        return None
    if _market_is_closed():
        return None

    perf = upstox_client.get_recent_performance(symbol)
    weak = upstox_client.is_recently_weak(perf, WEAKNESS_THRESHOLD)
    stop_loss_pct = STOP_LOSS_PCT if weak else None
    reason = f"Results-today watch: {symbol} up {gain_pct:+.2f}% vs previous close ahead of today's declared results"
    position_id, lost_race = db.try_open_position(
        symbol, company, "results_today", None, reason, "auto", price, db.now(symbol),
        TARGET_PCT, TARGET_PCT, stop_loss_pct, capital_per_trade, max_trades_per_day, max_capital_allowed,
        hard_stop_pct=HARD_STOP_PCT,
    )
    if position_id is None:
        why = {"already_bought": "already bought today", "quota": "daily trade quota reached",
               "capital": "max capital allowed would be exceeded"}[lost_race]
        print(f"[skip] {symbol}: {why} (by a concurrent run, since this check).")
        return None
    print(f"[opened] Paper position: {symbol} bought at Rs.{price:.2f} on results-today watch "
          f"({gain_pct:+.2f}% vs previous close), target +{TARGET_PCT}%, stop-loss {stop_loss_pct}, "
          f"hard stop {HARD_STOP_PCT}%")
    return "opened"


class _ResultsTodayWatch:
    """Tracks companies scheduled (per NSE's results_calendar) to declare
    results TODAY, each with a cached previous close, so monitor --tick can
    fire RESULTS_TODAY_GAIN_PCT's buy trigger the instant a live tick crosses
    it -- see _open_results_today_position. Only meant to be refreshed
    roughly once per outer loop cycle (same cadence as the open-positions
    symbol refresh in main()'s --tick loop): the calendar itself doesn't
    change intraday, and re-fetching a previous close every tick would be a
    wasted REST call per tick for no benefit.

    prev_close is deliberately cached per-symbol and never re-fetched once
    set (refresh() only fills in symbols missing from the cache) -- a
    previous close is a fixed historical fact for the whole day, unlike
    "still a candidate" (which can change the moment a position opens for
    that symbol, via this run or a concurrent scan/confirm)."""
    def __init__(self):
        self.companies = {}    # symbol -> company name, current candidates
        self.prev_close = {}   # symbol -> previous close, cached once per symbol
        # symbol -> company name, every results_calendar entry from the last
        # RECENT_RESULTS_CAPTURE_DAYS days -- the tick-capture universe, a
        # strict superset of `companies` (see refresh()'s docstring for why
        # it deliberately doesn't share `companies`' active-position
        # exclusion or prev_close requirement).
        self.recent_companies = {}
        # Symbols confirmed to have no Upstox instrument_key at all (e.g. a
        # REIT/InvIT unit outside instrument_master's equity-series filter --
        # verified live: ANZEN, SHREMINVIT, INDUSINVIT, MINDSPACE, SECURCRED
        # all NSE-scheduled to declare results but never resolvable this
        # way). get_instrument_key force-refreshes the ENTIRE instrument
        # master from the network on every miss (see instrument_master.
        # get_instrument_key) -- without this cache, a permanently-
        # unresolvable symbol would trigger a full CSV re-download every
        # single 30s refresh() cycle, forever, for no possible benefit.
        # Shared between the today-only candidate set and the wider
        # recent-days capture set below -- an unresolvable symbol is
        # unresolvable regardless of which list found it first.
        self._unresolved = set()

    def refresh(self):
        """Rebuilds the candidate set from today's calendar minus anything
        that already has an active (open or queued) position -- symbols this
        watch itself just opened a position for drop out here too, since
        has_active_position/get_open_symbols will already reflect that.

        Calls nse_bot.refresh_results_calendar() itself (same safety-net
        pattern as _results_calendar_pending) so `monitor --tick` alone --
        without `scan` ever having run -- still populates the calendar; the
        24h cache inside that call makes this a cheap no-op on every other
        cycle (this runs once per 30s outer-loop pass, not once per tick)."""
        try:
            nse_bot.refresh_results_calendar()
        except Exception as e:
            print(f"[warn] results calendar refresh failed: {e}", file=sys.stderr)
        rows = db.get_results_calendar_for_date(db.now().date())
        candidates = {row["symbol"]: row["company"] for row in rows
                      if not db.has_active_position(row["symbol"])}
        self.companies = candidates
        self.prev_close = {s: c for s, c in self.prev_close.items() if s in candidates}
        for symbol in candidates:
            if symbol in self.prev_close or symbol in self._unresolved:
                continue
            try:
                close = upstox_client.get_last_close(symbol)
            except Exception as e:
                # Deliberately broad, not just RuntimeError -- verified live
                # and real: a network blip (wifi drop) surfaces here as a raw
                # urllib.error.URLError/OSError from deep inside
                # instrument_master._download(). That escaped uncaught once
                # already and killed this ENTIRE --tick process (and every
                # open position it was watching) over a single symbol's
                # transient lookup failure -- a one-symbol problem must
                # never be allowed to end the whole session like that again.
                #
                # upstox_client._resolve_instrument_key wraps BOTH a
                # genuinely-missing instrument_key (KeyError) AND
                # get_historical_close's own token-expiry RuntimeError into
                # the SAME RuntimeError type -- so type alone can't tell
                # "will never resolve" apart from "will resolve fine once
                # the token's refreshed / the network's back". Only the
                # former belongs in self._unresolved; caching the latter
                # there blacklists the symbol for the rest of this
                # (potentially days-long) --tick session even after the
                # underlying problem is fixed. Verified live: exactly this
                # happened to EMIL after an early-morning token expiry --
                # still excluded from both the buy-trigger and tick-capture
                # universes hours after the token was refreshed. Message
                # text is the only signal available to split the two cases.
                if "No Upstox instrument_key found" in str(e):
                    self._unresolved.add(symbol)
                print(f"[warn] results-today watch: can't fetch a previous close for {symbol}: {e}")
                continue
            if close:
                self.prev_close[symbol] = close
            else:
                self._unresolved.add(symbol)

        # Tick-capture universe: every symbol with a results declaration in
        # the last RECENT_RESULTS_CAPTURE_DAYS days (today included),
        # deliberately NOT filtered by has_active_position (an open
        # position from a recent results signal is exactly what's worth
        # capturing ticks for) or by whether a previous close could be
        # fetched (capture just records ltp as-is, no gain-vs-prev-close
        # math needed the way the buy-trigger candidates above require).
        today = db.now().date()
        try:
            recent_rows = db.get_results_calendar_since(
                today - timedelta(days=RECENT_RESULTS_CAPTURE_DAYS - 1), today)
        except Exception as e:
            print(f"[warn] results-recent watch: calendar lookup failed: {e}", file=sys.stderr)
            recent_rows = []
        recent = {}
        for row in recent_rows:
            symbol = row["symbol"]
            if symbol in self._unresolved:
                continue
            if symbol not in candidates:
                # Not already validated via the buy-trigger path above --
                # confirm it actually has a usable instrument key before
                # trusting it for feed subscription. get_instrument_key
                # raises KeyError for a genuinely unresolvable symbol (see
                # the _unresolved comment in __init__) -- that's the only
                # case worth caching permanently. A force-refresh network
                # blip inside get_instrument_key surfaces as a raw
                # URLError/OSError, not KeyError -- same "transient, don't
                # permanently blacklist" reasoning as the RuntimeError-
                # message split in the prev_close loop above applies here,
                # just with a distinguishable exception type this time
                # instead of shared-type message-matching.
                try:
                    instrument_master.get_instrument_key(symbol)
                except KeyError:
                    self._unresolved.add(symbol)
                    continue
                except Exception as e:
                    print(f"[warn] results-recent watch: instrument lookup failed for {symbol} (will retry): {e}")
                    continue
            recent[symbol] = row["company"]
        self.recent_companies = recent

        return list(set(self.prev_close.keys()) | set(self.recent_companies.keys()))

    def check_tick(self, symbol, price, capital_per_trade, max_trades_per_day, max_capital_allowed):
        """Returns True if this tick opened a position, else False (not a
        candidate right now, or gain hasn't crossed the threshold yet)."""
        prev_close = self.prev_close.get(symbol)
        if prev_close is None:
            return False
        gain_pct = (price - prev_close) / prev_close * 100
        if gain_pct < RESULTS_TODAY_GAIN_PCT:
            return False
        company = self.companies.get(symbol)
        opened = _open_results_today_position(
            symbol, company, price, gain_pct, capital_per_trade, max_trades_per_day, max_capital_allowed)
        if opened:
            # Stop watching immediately -- db.bought_today() would block a
            # second buy anyway, but dropping it here also means the next
            # refresh() doesn't waste a has_active_position() round trip on it.
            del self.prev_close[symbol]
        return bool(opened)


class _ScanState:
    """Tracks everything _decide() needs across both sources in one scan.
    open_symbols/resolved_symbols are local caches purely to skip redundant
    work (avoid re-evaluating a symbol that's already been fully acted on
    this scan, avoid a wasted Upstox call for a symbol already open) -- they
    are NOT the safety mechanism against double-buying or blowing the daily
    quota. The db.count_trades_today()/db.bought_today() calls in this file
    are fast-path checks only, so caching these sets can't cause a
    double-buy even if it's ever stale; the actual gate is
    db.try_open_position()'s atomic recheck-and-insert under an advisory
    lock, which is what closes the race between concurrent trade_bot.py
    processes (see db.py's _QUOTA_LOCK_KEY notes).

    resolved_symbols (was seen_symbols) only gets a symbol added once it's
    been FULLY acted on -- opened, queued, or already had an open position
    -- NOT merely once any candidate for it has been seen. Verified live and
    real: the old seen_symbols marked a symbol the moment ANY candidate for
    it was processed, so if a same-cycle MANUAL candidate for a symbol
    happened to be generated before a same-cycle AUTO candidate for the
    SAME symbol (order depends on fetch/classify batch ordering, not
    confidence), the auto candidate -- which should always take priority --
    got silently skipped entirely, never even evaluated. Only a genuinely
    resolved outcome should block further candidates for a symbol within
    the same scan; an unresolved manual candidate must not."""
    def __init__(self, args):
        self.args = args
        self.open_symbols = db.get_open_symbols()
        self.resolved_symbols = set()
        self.total = 0
        self.opened = 0
        self.offhours_queued = 0  # distinct from self.queued (list) -- that's the manual-review queue
        self.queued = []
        self.skipped_quota = 0


def _decide(c, state):
    """Open-or-queue a single candidate immediately, right as it's generated.

    This used to happen in a separate pass after EVERY candidate from BOTH
    sources had already been fetched and marked seen (db.is_nse_seen /
    db.is_news_seen) in Postgres. That's a resumability bug: is_*_seen()
    means "we wrote a row for this", not "we decided what to do about it" --
    if the process died anywhere between those two points, the signal was
    gone for good on the next scan (already seen, so never regenerated as a
    candidate, but never actually evaluated for a buy either). Deciding
    inline means "seen" and "decided" happen atomically from the caller's
    perspective: any prefix of a scan that completes is fully consistent, and
    whatever's left just gets picked up next run.

    Bonus effect, not the original motivation: a high-confidence NSE filing
    no longer waits behind the entire news RSS-fetch-and-classify pipeline
    (which can take tens of seconds to minutes) before it's actually acted on."""
    args = state.args
    state.total += 1

    if c["symbol"] in state.resolved_symbols:
        return  # this symbol already got a REAL decision this scan (opened/queued/already-open)

    if c["symbol"] in state.open_symbols:
        state.resolved_symbols.add(c["symbol"])
        return  # already have an open position for this symbol

    if c["confidence"] == "auto":
        under_quota = db.count_trades_today(c["symbol"]) < args.max_trades_per_day
        under_capital = db.get_open_capital() + args.capital_per_trade <= args.max_capital_allowed
        if under_quota and under_capital:
            print(f"\n--- auto-approving {c['symbol']} ({c['source']}): {c['detail']} ---")
            result = _open_position(c, args.capital_per_trade, args.max_trades_per_day, args.max_capital_allowed, interactive=False)
            if result in ("opened", "queued"):
                state.resolved_symbols.add(c["symbol"])
                state.open_symbols.add(c["symbol"])
                if result == "opened":
                    state.opened += 1
                else:
                    state.offhours_queued += 1
                # A stale manual-review entry for this SAME symbol (from an
                # earlier, weaker signal -- possibly this very cycle, if a
                # manual candidate for it happened to be generated first;
                # possibly an earlier day's scan) is now superseded by a
                # real decision -- verified live and real: a symbol sitting
                # unresolved in pending_candidates, with no code path ever
                # clearing it once a genuine auto signal came in and opened/
                # queued a position for that same symbol, misleadingly kept
                # showing as "still needs review" even though the actual
                # decision had already been made. Resolve it so `confirm`/
                # the dashboard's pending queue don't keep showing it.
                if db.is_pending(c["symbol"]):
                    db.resolve_pending(c["symbol"], "superseded_by_auto")
            return
        why = "Daily trade quota" if not under_quota else "Max capital allowed"
        print(f"{why} reached; queuing {c['symbol']} for manual review instead.")
        # falls through to the manual/queue handling below -- deliberately
        # NOT added to resolved_symbols: this candidate wasn't acted on, so
        # a later, better candidate for the same symbol this cycle should
        # still get its own chance (and db.is_pending below already makes a
        # repeat manual candidate for the same symbol a safe no-op).

    if db.is_pending(c["symbol"]):
        return
    remaining_quota = max(0, args.max_trades_per_day - db.count_trades_today(c["symbol"]))
    if len(state.queued) >= remaining_quota:
        state.skipped_quota += 1
        return
    db.add_pending(c)
    state.queued.append(c)


def _process_queued_orders(max_trades_per_day, max_capital_allowed):
    """Fill market-closed-queued limit orders (see _queue_offhours_order) now
    that the market's open -- same idea as a real broker executing a resting
    AMO the moment its limit price is met. Called at the start of every
    scan, so queued orders get first crack at execution before any new
    signal from this cycle is even considered.

    TODO(prod-readiness): this never expires a queued order -- see
    db.get_queued_positions' docstring for why that's more than hygiene (the
    limit price is the ONLY freshness gate this path has, and it's only
    valid on the next open, not indefinitely). Deferred, not fixed here."""
    if _market_is_closed():
        return  # nothing to do until the market reopens
    for order in db.get_queued_positions():
        symbol = order["symbol"]
        try:
            price = upstox_client.get_current_price(symbol)
        except RuntimeError as e:
            print(f"[warn] Can't fetch a price for queued order {symbol} ({e}), leaving queued.")
            continue
        if price is None:
            print(f"[warn] No price data for queued order {symbol} yet, leaving queued.")
            continue
        limit_price = float(order["limit_price"])
        if price > limit_price:
            print(f"[queued] {symbol}: price Rs.{price:.2f} still above limit Rs.{limit_price:.2f}, leaving queued.")
            continue

        perf = upstox_client.get_recent_performance(symbol)
        weak = upstox_client.is_recently_weak(perf, WEAKNESS_THRESHOLD)
        stop_loss_pct = STOP_LOSS_PCT if weak else None
        position_id, lost_race = db.try_fill_queued_position(
            order["id"], symbol, price, db.now(symbol), TARGET_PCT, TARGET_PCT,
            stop_loss_pct, float(order["capital"]), max_trades_per_day, max_capital_allowed,
            hard_stop_pct=HARD_STOP_PCT,
        )
        if position_id is None:
            why = {"already_bought": "already bought today", "quota": "daily trade quota reached",
                   "capital": "max capital allowed would be exceeded"}[lost_race]
            print(f"[skip] queued order {symbol}: {why}; left queued (will retry next cycle).")
            continue
        print(f"[filled] {symbol}: bought at Rs.{price:.2f} "
              f"(limit was Rs.{limit_price:.2f}), target +{TARGET_PCT}%, stop-loss {stop_loss_pct}, "
              f"hard stop {HARD_STOP_PCT}%")


def cmd_scan(args):
    run_id = db.start_run("scan")
    state = _ScanState(args)
    error = None
    try:
        _process_queued_orders(args.max_trades_per_day, args.max_capital_allowed)
        try:
            # _nse_candidates is a generator -- fetch_announcements() (its
            # first statement) only actually runs once this loop starts
            # iterating, so the try has to wrap the loop itself, not just
            # the call. Verified live: a bare DNS failure here
            # (`<urlopen error [Errno 8] nodename nor servname provided...>`)
            # used to abort the whole scan, taking the news path down with
            # it too, even though a dead NSE feed should degrade the scan
            # (same "warn and continue" treatment the news path already
            # gets below), not end it. fetch_announcements/
            # fetch_board_meetings now also retry transient failures
            # themselves (see nse_bot._urlopen_json) -- this is the backstop
            # for whatever still gets through that.
            for c in _nse_candidates(ignore_seen=args.reset_seen):
                _decide(c, state)
        except Exception as e:
            print(f"[warn] Skipping NSE-based signals this run: {e}")

        try:
            _news_candidates(lambda c: _decide(c, state), model=args.model, ignore_seen=args.reset_seen)
        except RuntimeError as e:
            print(f"[warn] Skipping news-based signals this run: {e}")

        if state.total == 0:
            print("No new signals.")
            return

        print(f"\n{state.opened} position(s) auto-opened, {state.offhours_queued} limit order(s) queued "
              f"(market closed, fills on next market open), {len(state.queued)} queued for manual review (run `confirm` next).")
        for c in state.queued:
            print(f"  - {c['symbol']} ({c['source']}, {c['company']}): {c['detail']}")
        if state.skipped_quota > 0:
            print(f"  [{state.skipped_quota} more found but skipped -- daily quota reached]")
    except Exception as e:
        error = str(e)
        raise
    finally:
        db.finish_run(run_id, signals_seen=state.total, positions_opened=state.opened,
                       queued=len(state.queued), skipped_quota=state.skipped_quota, error=error)


def cmd_confirm(args):
    pending = db.get_pending()
    if not pending:
        print("No pending signals. Run `scan` first.")
        return

    for c in pending:
        if db.count_trades_today(c["symbol"]) >= args.max_trades_per_day:
            print(f"Daily trade quota ({args.max_trades_per_day}) reached; leaving rest queued.")
            continue
        if db.get_open_capital() + args.capital_per_trade > args.max_capital_allowed:
            print(f"Max capital allowed ({args.max_capital_allowed}) reached; leaving rest queued.")
            continue
        # _decide() (the auto-buy path) never reaches _open_position for a
        # symbol that already has one without first checking
        # get_open_symbols() -- this loop had no equivalent, a real gap:
        # confirming a pending candidate for a symbol that already has an
        # open position (from a previous day -- bought_today() only covers
        # "today") or a resting off-hours queued order (bought_today()
        # explicitly excludes 'queued') would open a SECOND position for it.
        # has_active_position() covers both cases _open_position's own
        # bought_today() check doesn't.
        if db.has_active_position(c["symbol"]):
            print(f"[skip] {c['symbol']}: already has an open or queued position -- leaving pending.")
            continue

        print(f"\n--- {c['symbol']} ({c['source']}: {c['company']}) ---")
        print(f"Signal: {c['detail']}")
        if c.get("summary"):
            print(f"Summary: {c['summary'][:300]}")
        if c.get("link"):
            print(f"Link: {c['link']}")

        if _open_position(c, args.capital_per_trade, args.max_trades_per_day, args.max_capital_allowed, interactive=True):
            db.resolve_pending(c["symbol"], "confirmed")
        # else: stays pending, re-asked next `confirm` run (existing behavior, unchanged)


def _get_price(symbol, feed=None):
    """Prefer the live WebSocket feed (no REST round-trip, has ltt) when one's
    running; fall back to REST if the feed has no tick yet or isn't in use."""
    if feed is not None:
        try:
            price = feed.get_ltp(symbol)
        except KeyError:
            price = None
        if price is not None:
            return price
    return upstox_client.get_current_price(symbol)


def _install_mock_clock():
    """If UPSTOX_API_BASE points at a mock (not the real Upstox API), route
    db.now()'s central clock through the mock's per-symbol simulated replay
    time instead of always answering with real wall-clock time -- see
    db.py's central-clock notes. get_mock_replay_time already returns None
    by itself whenever UPSTOX_API_BASE is the real API, so calling this
    unconditionally is harmless in production; it just never has any effect
    there. Called at import time (not just from main()) so a test script
    that imports trade_bot and calls _open_position()/cmd_monitor() directly
    -- without ever going through main() -- still gets the mock clock wired
    up automatically, matching whatever UPSTOX_API_BASE is already set to."""
    db.set_clock(upstox_client.get_mock_replay_time)


_install_mock_clock()


EOD_GRACE_BAND_LOW = -2.0  # below this at entry-day's close, cut losses now -- don't wait


def _eod_deadline(entry_time, gain_pct, target_pct):
    """NSE close (15:30 IST) on the calendar day `entry_time` falls on, in
    IST -- a position is never meant to ride past the session it was opened
    in indefinitely (flat target-only exit with no time-based rule otherwise
    means a trade that never reaches +5%/stop-loss sits open forever).
    Keyed off each position's OWN entry date, not "is it past 15:30 right
    now", so this fires correctly however monitor happens to be run: right
    at close ("on the spot"), or only picked up again the next time it runs
    (e.g. the next morning) if nothing was polling overnight.

    One exception: if gain is still "undecided" (between EOD_GRACE_BAND_LOW
    and target_pct -- hasn't hit target, hasn't dropped badly either) right
    at that entry-day deadline, give it one more full day before forcing an
    exit. This matters for news that breaks after NSE's 15:30 close (a very
    common real pattern -- results are routinely released post-market) --
    the market has zero chance to react on the day of the news, so forcing
    an exit at that same day's close would cut the trade before the market
    ever got to respond to what it was actually opened for. A clearly bad
    outcome (below EOD_GRACE_BAND_LOW) still exits at the normal deadline --
    this is a grace period for "still plausible", not "ride out any loss"."""
    entry_date = entry_time.astimezone(IST).date()
    deadline = datetime.combine(entry_date, NSE_CLOSE_TIME, tzinfo=IST)
    if EOD_GRACE_BAND_LOW <= gain_pct < target_pct:
        deadline += timedelta(days=1)
    return deadline


def _check_and_maybe_close(pos, price):
    """Evaluate one open position against a given live price -- close it if
    target/stop-loss/EOD deadline is hit, else just report it's still open.
    Returns True if closed, False if left open. Shared by cmd_monitor's
    per-cycle polling loop (checks every open position on a timer) and
    monitor --tick's on_tick callback (checks just the one position whose
    price just updated, the instant a real tick arrives) -- same exit logic
    either way, only the trigger differs."""
    entry_price = float(pos["entry_price"])
    target_pct = float(pos["target_min_pct"])  # target_min_pct == target_max_pct == TARGET_PCT
    stop_loss_pct = float(pos["stop_loss_pct"]) if pos["stop_loss_pct"] is not None else None
    hard_stop_pct = float(pos["hard_stop_pct"]) if pos.get("hard_stop_pct") is not None else None

    gain_pct = (price - entry_price) / entry_price * 100
    exit_reason = None
    if gain_pct >= target_pct:
        exit_reason = "target"
    elif stop_loss_pct is not None and gain_pct <= stop_loss_pct:
        exit_reason = "stop_loss"
    elif hard_stop_pct is not None and gain_pct <= hard_stop_pct:
        # Unconditional floor -- fires even for a not-recently-weak position
        # that has no stop_loss_pct at all (target-only exit otherwise).
        exit_reason = "hard_stop"
    elif db.now(pos["symbol"]).astimezone(IST) >= _eod_deadline(pos["entry_time"], gain_pct, target_pct):
        # db.now(symbol) here, not datetime.now(IST) directly: during
        # tests entry_time is stamped with the mock's simulated
        # (historical) replay time (see db.py's central-clock notes) --
        # comparing that against real wall-clock "now" would make every
        # test position look instantly past its deadline, since
        # simulated time is always in the past relative to whenever the
        # test actually runs. Both sides of this comparison must be on
        # the same clock.
        exit_reason = "eod"

    if exit_reason:
        pnl_amount = float(pos["capital"]) * gain_pct / 100
        db.close_position(pos["id"], price, db.now(pos["symbol"]), exit_reason,
                           round(gain_pct, 2), round(pnl_amount, 2))
        print(f"[closed] {pos['symbol']}: bought at Rs.{entry_price:.2f}, sold at Rs.{price:.2f} "
              f"({gain_pct:+.2f}%, {exit_reason}) -> P&L Rs.{pnl_amount:+.2f}")
        return True
    print(f"[open]   {pos['symbol']}: bought at Rs.{entry_price:.2f} "
          f"({gain_pct:+.2f}%, target +{target_pct}%, stop {stop_loss_pct}, hard stop {hard_stop_pct})")
    return False


def cmd_monitor(args, feed=None):
    run_id = db.start_run("monitor")
    positions = []
    closed = 0
    error = None
    try:
        positions = db.get_open_positions()
        if not positions:
            print("No open positions.")
            return

        if feed is not None:
            feed.add_symbols([p["symbol"] for p in positions])

        for pos in positions:
            try:
                price = _get_price(pos["symbol"], feed)
            except RuntimeError as e:
                print(f"[warn] Can't fetch a price for {pos['symbol']} ({e}), leaving open.")
                continue
            if price is None:
                print(f"[warn] No price data for {pos['symbol']}, leaving open.")
                continue
            if _check_and_maybe_close(pos, price):
                closed += 1
    except Exception as e:
        error = str(e)
        raise
    finally:
        # scan_runs' columns are named for `scan`'s vocabulary; for a
        # `monitor` run, signals_seen is positions checked and `queued`
        # is repurposed to mean positions closed this pass (`stocki`'s
        # health panel reads command alongside these to interpret them).
        db.finish_run(run_id, signals_seen=len(positions), queued=closed, error=error)


def cmd_status(args):
    print(f"Trades today: {db.count_trades_today()}/{args.max_trades_per_day}")
    queued = db.get_queued_positions()
    if queued:
        print(f"Queued orders (market closed) ({len(queued)}):")
        for order in queued:
            print(f"  {order['symbol']}: limit Rs.{float(order['limit_price']):.2f}, "
                  f"capital Rs.{float(order['capital']):.2f}, queued {order['entry_time']}")
    positions = db.get_open_positions()
    if not positions:
        print("No open positions.")
        return
    for pos in positions:
        entry_price = float(pos["entry_price"])
        try:
            price = upstox_client.get_current_price(pos["symbol"])
        except RuntimeError as e:
            print(f"  {pos['symbol']}: entry Rs.{entry_price:.2f}, price unavailable ({e})")
            continue
        if price is None:
            print(f"  {pos['symbol']}: entry Rs.{entry_price:.2f}, price unavailable")
            continue
        gain_pct = (price - entry_price) / entry_price * 100
        print(f"  {pos['symbol']}: entry Rs.{entry_price:.2f} -> now Rs.{price:.2f} "
              f"({gain_pct:+.2f}%), stop-loss {pos['stop_loss_pct']}, hard stop {pos.get('hard_stop_pct')}")


_CONFIG_KEYS = ("max_trades_per_day", "capital_per_trade", "max_capital_allowed")


def cmd_config(args):
    if args.action == "show":
        stored = db.get_all_config()
        for key in _CONFIG_KEYS:
            print(f"{key} = {stored.get(key, '(not set)')}")
    else:  # "set"
        db.set_config(args.key, args.value)
        print(f"{args.key} = {args.value}")


# Test-only fallback -- NEVER consulted during a live run (see
# _load_config_defaults: gated on PYTEST_CURRENT_TEST, which pytest sets for
# the duration of every test and nothing else ever sets). Exists so tests
# that spin up a throwaway database don't all need to call
# `trade_bot.py config set` before exercising the buy path; a real run always
# resolves these from Postgres or fails loudly, never from this dict.
_TEST_ONLY_CONFIG_FALLBACK = {"max_trades_per_day": 3, "capital_per_trade": 10000.0, "max_capital_allowed": 30000.0}


def _load_config_defaults(cli_args):
    """CLI flag (if the user passed one this run) wins; otherwise pull from
    Postgres's `config` table -- db.py's schema seeds it once for a fresh
    database and `trade_bot.py config set` changes it from then on. Read once
    at startup, not on every scan/monitor cycle -- a long-running `--watch`
    process won't pick up a `config set` made while it's running until it's
    restarted.

    db.get_config() itself retries a few times and raises on an actual
    connection failure -- that propagates here unchanged, never treated as
    "key missing". A key that's genuinely missing from both the CLI and the
    table raises too, UNLESS running under pytest, in which case
    _TEST_ONLY_CONFIG_FALLBACK fills it in so tests don't need DB setup for
    values this specific test doesn't care about."""
    testing = "PYTEST_CURRENT_TEST" in os.environ
    casts = {"max_trades_per_day": int, "capital_per_trade": float, "max_capital_allowed": float}
    for key, cast in casts.items():
        if getattr(cli_args, key) is None:
            value = db.get_config(key)
            if value is None:
                if testing:
                    setattr(cli_args, key, _TEST_ONLY_CONFIG_FALLBACK[key])
                    continue
                raise RuntimeError(f"No --{key.replace('_', '-')} flag and no `config` table row for {key} -- "
                                    f"run `trade_bot.py config set {key} <value>` once.")
            setattr(cli_args, key, cast(value))


LOG_DIR = os.path.join(_PROJECT_ROOT, "logs")


class _TeeLogger:
    """Duplicates writes to the real stream AND a log file, stamping each
    line with a wall-clock timestamp in the file only (the terminal keeps
    its plain look). Installed around EVERY invocation (see _start_logging
    below), one-shot or --watch -- history for later debugging shouldn't
    depend on having remembered to pass --watch. This project is print()-
    based throughout (trade_bot.py, news_bot.py, nse_bot.py, classifier.py,
    upstox_client.py, upstox_feed.py, db.py) -- wrapping sys.stdout/sys.stderr
    here is what makes every one of those existing print() calls show up in
    the log file too, without rewriting any of them to a logging module."""
    def __init__(self, stream, log_file):
        self.stream = stream
        self.log_file = log_file
        self._at_line_start = True

    def write(self, data):
        self.stream.write(data)
        for line in data.splitlines(keepends=True):
            if self._at_line_start and line.strip():
                self.log_file.write(f"[{datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')}] ")
            self.log_file.write(line)
            self._at_line_start = line.endswith("\n")
        # log_file is a plain open() file -- Python fully-buffers that by
        # default (unlike an interactive terminal, which line-buffers), and
        # print() doesn't force a flush on its own. Without flushing here, a
        # --watch session's log file can sit empty for a long time (until the
        # internal buffer happens to fill or the process exits) even though
        # the terminal itself shows output immediately -- verified live:
        # `tail -f` on a freshly-started --watch log showed nothing for
        # several minutes despite the process visibly running.
        self.log_file.flush()

    def flush(self):
        self.stream.flush()
        self.log_file.flush()


def _start_logging(command):
    """Tee stdout/stderr to logs/<command>_<started-at>.log for this process's
    entire run -- one-shot command or --watch session alike, see _TeeLogger.
    Returns a restore() callback the caller must invoke in a `finally` so the
    file handle is always released, even on a crash. Deliberately one file
    per invocation (not one growing file per command) -- never overwritten or
    rotated, so every past run stays around for historic debugging until
    someone cleans up logs/ manually."""
    os.makedirs(LOG_DIR, exist_ok=True)
    started_at = datetime.now(IST).strftime("%Y%m%d_%H%M%S")
    path = os.path.join(LOG_DIR, f"{command}_{started_at}.log")
    log_file = open(path, "a")
    original_stdout, original_stderr = sys.stdout, sys.stderr
    sys.stdout = _TeeLogger(original_stdout, log_file)
    sys.stderr = _TeeLogger(original_stderr, log_file)
    print(f"Logging this run to {path}")

    def restore():
        sys.stdout, sys.stderr = original_stdout, original_stderr
        log_file.close()
    return restore


def main():
    db.init_schema()

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--max-trades-per-day", type=int, default=None,
                         help="default: `config` table value (see `trade_bot.py config show`)")
    parser.add_argument("--capital-per-trade", type=float, default=None,
                         help="default: `config` table value (see `trade_bot.py config show`)")
    parser.add_argument("--max-capital-allowed", type=float, default=None,
                         help="default: `config` table value (see `trade_bot.py config show`); "
                              "total capital across open positions is kept under this")
    parser.add_argument("--model", default=classifier.DEFAULT_MODEL, help="claude CLI model alias for news event classification")
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="Pull fresh order-win filings and queue them for review")
    p_scan.add_argument("--reset-seen", action="store_true",
                         help="Reprocess items already in the DB history this run (data is kept either way -- inserts are idempotent)")
    p_scan.add_argument("--watch", type=int, nargs="?", const=300, default=None,
                         help="Poll every N seconds instead of running once (default 300s/5min if flag given with no value)")
    p_scan.set_defaults(func=cmd_scan)

    p_confirm = sub.add_parser("confirm", help="Review queued signals and open paper positions")
    p_confirm.set_defaults(func=cmd_confirm)

    p_monitor = sub.add_parser("monitor", help="Check open positions against live prices")
    p_monitor.add_argument("--watch", type=int, default=None, help="Poll every N seconds instead of running once")
    p_monitor.add_argument("--tick", action="store_true",
                            help="React to every live WebSocket tick instead of polling on a --watch timer "
                                 "(requires the live feed -- no REST-polling fallback)")
    p_monitor.set_defaults(func=cmd_monitor)

    p_status = sub.add_parser("status", help="Show open positions and today's trade count")
    p_status.set_defaults(func=cmd_status)

    p_config = sub.add_parser("config", help="View or set persisted defaults (max_trades_per_day, capital_per_trade, max_capital_allowed)")
    config_sub = p_config.add_subparsers(dest="action", required=True)
    config_sub.add_parser("show", help="Print current effective values")
    p_config_set = config_sub.add_parser("set", help="Persist a value to the `config` table")
    p_config_set.add_argument("key", choices=list(_CONFIG_KEYS))
    p_config_set.add_argument("value")
    p_config.set_defaults(func=cmd_config)

    args = parser.parse_args()
    if args.command != "config":
        _load_config_defaults(args)

    restore_logging = _start_logging(args.command)
    try:
        watch = getattr(args, "watch", None)
        if args.command == "monitor" and getattr(args, "tick", False):
            # Runs forever, regardless of whether any position is open right
            # now or whether the market's even open -- a prod tick-watcher
            # shouldn't need a manual restart every time a position opens or
            # NSE's session starts. The feed itself only connects once
            # there's at least one open position to watch (no point opening
            # a WebSocket for nothing); until then, and whenever the market's
            # closed, this just logs why nothing's happening and keeps
            # waiting -- see _market_is_closed for the live-status check.
            run_id = db.start_run("monitor")
            stats = {"ticks": 0, "closed": 0, "results_today_opened": 0}
            results_watch = _ResultsTodayWatch()
            # Raw-tick capture for research (see analysis/tick_store.py and
            # analysis/TIMESCALE.md) -- piggybacks on THIS process's own
            # already-running feed and already-resolved results-today
            # candidate list instead of a second WebSocket connection / a
            # second instrument-key resolution pass for the same symbols.
            # Deliberately keyed off results_watch.companies (today's
            # results-calendar candidates), not every subscribed symbol --
            # an open position from a PRIOR day's results-calendar entry is
            # still subscribed for the close-check above, but isn't what
            # this capture is for. Writes to a SEPARATE, isolated TimescaleDB
            # instance (investment_features, port 5433) -- not the live
            # trading investment_bot database -- so rows are queryable
            # immediately (unlike the Parquet approach this replaced, whose
            # files aren't valid/readable until the whole session closes).
            capture = tick_store.TickStore()

            def on_tick(symbol, ltp, ltt, extra):
                stats["ticks"] += 1
                # Re-fetched fresh every tick, not cached -- a position
                # closed/opened by a concurrent `scan`/`confirm` process
                # must be picked up immediately, not just at the next
                # subscription refresh below.
                for pos in db.get_open_positions():
                    if pos["symbol"] == symbol:
                        if _check_and_maybe_close(pos, ltp):
                            stats["closed"] += 1
                # A symbol can be both an open position and (stale, one
                # refresh() cycle behind) a results-today candidate at the
                # same instant -- check_tick's own prev_close lookup is the
                # real guard there (refresh() already drops has_active_
                # position() symbols, so this is just closing that window),
                # not mutual exclusivity with the block above.
                if results_watch.check_tick(symbol, ltp, args.capital_per_trade,
                                             args.max_trades_per_day, args.max_capital_allowed):
                    stats["results_today_opened"] += 1
                if symbol in results_watch.recent_companies:
                    capture.record(symbol, ltp, ltt, datetime.now(IST).isoformat(), extra)

            feed = None
            error = None
            # `kill <pid>` sends SIGTERM by default, which Python does NOT
            # translate into KeyboardInterrupt on its own -- without this,
            # stopping this process any way other than Ctrl+C would skip the
            # `finally` below entirely, leaving whatever's still buffered in
            # `capture` (see analysis/tick_store.py) un-flushed and the
            # TimescaleDB connection unclosed.
            signal.signal(signal.SIGTERM, lambda signum, frame: (_ for _ in ()).throw(KeyboardInterrupt()))
            try:
                print("Watching ticks (Ctrl+C to stop)...")
                while True:
                    position_symbols = set(db.get_open_symbols())
                    watch_symbols = set(results_watch.refresh())
                    symbols = list(position_symbols | watch_symbols)
                    if feed is None and symbols:
                        # "full" (5-level order-book depth) instead of the
                        # default ltpc mode -- carries vtt/atp/tbq/tsq/OHLC/
                        # bid-ask depth alongside ltp/ltt/ltq/cp, all of
                        # which `capture` now records (see analysis/
                        # tick_store.py). Only this feed (not the plain
                        # --watch REST-polling fallback below, which never
                        # touches `extra` at all) needs the richer payload.
                        # NOTE: "full_d5" (the response-side protobuf enum
                        # name) is NOT a valid subscribe-request mode string
                        # -- sending it gets zero feed data back (verified
                        # live: only the periodic marketInfo heartbeat
                        # arrives). "full" is the real mode string for this.
                        feed = upstox_feed.UpstoxFeed(symbols, mode="full", on_tick=on_tick)
                        feed.start()
                        if not feed.wait_for_data(timeout=15):
                            print("[warn] No initial tick received within 15s -- still watching.")
                        print(f"Connected -- watching {len(position_symbols)} open position(s), "
                              f"{len(results_watch.companies)} results-today buy candidate(s), and "
                              f"{len(results_watch.recent_companies)} recent-results tick-capture symbol(s).")
                    elif feed is not None:
                        feed.add_symbols(symbols)

                    if _market_is_closed():
                        print("[info] Market closed -- no live NSE signal right now, watching quietly.")
                    elif not symbols:
                        print("[info] No open positions or results candidates yet -- watching for one to appear.")
                    # This run_id's scan_runs row stays open for the entire
                    # (potentially days-long) --tick session -- finished_at
                    # only gets set on exit -- so without a per-cycle
                    # heartbeat it's indistinguishable from a process that
                    # died silently, which is exactly the ambiguity
                    # scan_runs exists to remove. Heartbeat every cycle even
                    # when idle (zero open positions, market closed): that's
                    # this loop's normal state, not a problem.
                    db.heartbeat_run(run_id)
                    flushed = capture.flush_all()
                    if flushed:
                        print(f"[capture] {flushed} tick(s) written to TimescaleDB this cycle.")
                    time.sleep(30)
            except KeyboardInterrupt:
                print(f"\nStopped. Processed {stats['ticks']} tick(s), closed {stats['closed']} position(s), "
                      f"opened {stats['results_today_opened']} results-today position(s).")
            except Exception as e:
                error = str(e)
                raise
            finally:
                if feed:
                    feed.stop()
                capture.close_all()  # final flush + close the TimescaleDB connection
                db.finish_run(run_id, signals_seen=stats["ticks"], positions_opened=stats["results_today_opened"],
                               queued=stats["closed"], error=error)
        elif watch and args.command == "monitor":
            # One persistent WebSocket connection for the whole watch session,
            # instead of a REST call per symbol per poll -- see upstox_feed.py.
            symbols = list(db.get_open_symbols())
            feed = upstox_feed.UpstoxFeed(symbols) if symbols else None
            if feed:
                try:
                    feed.start()
                    feed.wait_for_data(timeout=15)
                except (RuntimeError, KeyError) as e:
                    print(f"[warn] Live feed unavailable ({e}), falling back to REST polling.")
                    feed = None

            print(f"Watching (every {watch}s). Ctrl+C to stop.")
            try:
                while True:
                    run_time = time.strftime("%Y-%m-%d %H:%M:%S")
                    print(f"\n[{run_time}] running {args.command}...")
                    try:
                        cmd_monitor(args, feed=feed)
                    except Exception as e:
                        # A single cycle's failure (transient network blip, etc.)
                        # shouldn't end an unattended --watch session -- cmd_monitor
                        # already recorded this into scan_runs.error before
                        # re-raising; here we just log it and keep the loop alive.
                        print(f"[error] monitor cycle failed, continuing: {e}")
                    time.sleep(watch)
            except KeyboardInterrupt:
                print("\nStopped.")
            finally:
                if feed:
                    feed.stop()
        elif watch:
            print(f"Watching (every {watch}s). Ctrl+C to stop.")
            try:
                while True:
                    run_time = time.strftime("%Y-%m-%d %H:%M:%S")
                    print(f"\n[{run_time}] running {args.command}...")
                    try:
                        args.func(args)
                    except Exception as e:
                        print(f"[error] {args.command} cycle failed, continuing: {e}")
                    time.sleep(watch)
            except KeyboardInterrupt:
                print("\nStopped.")
        else:
            args.func(args)
    finally:
        restore_logging()


if __name__ == "__main__":
    main()
