#!/usr/bin/env python3
# Dummy comment: placeholder added for testing the local git identity.
"""
Postgres-backed permanent storage for this project -- replaces the old
flat-file state (.trade_state.json, .trade_pending.json, .trade_seen.json,
.trade_news_seen.json, .nse_seen.json, reports/paper_trades.csv).

Local database, single user, no server-management overhead beyond what's
already running: connects to the existing local Postgres instance (already
running for other work -- hive_metastore, etc.) via a dedicated,
isolated `investment_bot` database created just for this project.

Setup (one-time, already done for this project):
    createdb investment_bot

Usage:
    python3 db.py init      # create tables if they don't exist
    python3 db.py stats     # quick row counts, sanity check
"""
import contextlib
import getpass
import os
import time
import uuid
from datetime import datetime, timedelta, timezone

import psycopg2
import psycopg2.extras

DB_HOST = os.environ.get("INVESTMENT_DB_HOST", "localhost")
DB_NAME = os.environ.get("INVESTMENT_DB_NAME", "investment_bot")
DB_USER = os.environ.get("INVESTMENT_DB_USER", getpass.getuser())

# Attribution for who/what produced a row -- "tester" for mock-server test
# runs, "ashish" for real runs against live NSE/news/Upstox. A label, not a
# partition: doesn't affect uniqueness constraints or trade-quota counting,
# just lets rows from a test run be told apart from (or wiped independent of)
# real ones. companies is the shared Upstox instrument master, not
# user-attributable, so it doesn't get this column.


def _app_user():
    """'tester' whenever this process is clearly not a real production run,
    else 'ashish' -- INVESTMENT_USER always overrides this explicitly when
    set. Two signals, both already used elsewhere for exactly this same
    "are we real or mock/test" question:
      - UPSTOX_API_BASE pointing away from the real Upstox API -- the same
        check broker/upstox_client.py's get_mock_replay_time() already uses
        to decide whether trading against mock_servers/ is happening.
      - PYTEST_CURRENT_TEST -- set by pytest for the duration of every test,
        same signal trade_bot.py's _load_config_defaults already checks.
    Exists because nothing enforced this before: CLAUDE.md documents
    'tester' as the intended attribution for mock-server runs, but every row
    actually got written as 'ashish' unless a human remembered to export
    INVESTMENT_USER=tester by hand first -- exactly the confusion stocki's
    own design doc warns about (an unfiltered SUM(pnl_amount) reporting
    mock-run profit as real).

    A FUNCTION, called fresh at every write site below, not a module-level
    constant frozen at import time -- verified live that PYTEST_CURRENT_TEST
    is NOT yet set when pytest imports this module during collection (only
    once an actual test starts running), so a frozen constant would have
    silently locked in 'ashish' for an entire test session regardless of
    this check, defeating the whole point."""
    if os.environ.get("INVESTMENT_USER"):
        return os.environ["INVESTMENT_USER"]
    if os.environ.get("UPSTOX_API_BASE", "https://api.upstox.com") != "https://api.upstox.com":
        return "tester"
    if "PYTEST_CURRENT_TEST" in os.environ:
        return "tester"
    return "ashish"

# NSE trades in IST, and Postgres's session timezone here is Asia/Kolkata --
# entry_time::date casts in SQL below therefore resolve "today" in IST. Any
# Python-side "what date is this instant" computation MUST also convert to
# IST first (never take .date() on a bare UTC-aware datetime) or the two
# sides can disagree by a full calendar day for the ~5.5 hours daily where
# the UTC date and IST date differ (a real bug this used to have: verified
# live, entry_time::date and a naive Python .date() on the same instant gave
# different dates around 2026-08-01 18:48 UTC / 2026-08-02 00:18 IST).
IST = timezone(timedelta(hours=5, minutes=30))

SCHEMA = """
CREATE TABLE IF NOT EXISTS news_items (
    id SERIAL PRIMARY KEY,
    title TEXT NOT NULL UNIQUE,
    link TEXT,
    source TEXT,
    pub_date TIMESTAMPTZ,
    matched_query TEXT,
    money TEXT,
    event_type TEXT,
    category TEXT,
    signal TEXT,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    "user" TEXT NOT NULL DEFAULT 'ashish'
);

CREATE TABLE IF NOT EXISTS news_item_companies (
    id SERIAL PRIMARY KEY,
    news_item_id INTEGER NOT NULL REFERENCES news_items(id) ON DELETE CASCADE,
    symbol TEXT NOT NULL,
    name TEXT,
    instrument_key TEXT,
    match_type TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(news_item_id, symbol)
);

CREATE TABLE IF NOT EXISTS nse_filings (
    id SERIAL PRIMARY KEY,
    seq_id TEXT NOT NULL UNIQUE,
    symbol TEXT,
    company TEXT,
    isin TEXT,
    category TEXT,
    signal TEXT,
    amount TEXT,
    summary TEXT,
    filing_url TEXT,
    filed_at TEXT,
    filed_at_ts TIMESTAMPTZ,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    "user" TEXT NOT NULL DEFAULT 'ashish'
);

CREATE TABLE IF NOT EXISTS positions (
    id SERIAL PRIMARY KEY,
    transaction_id TEXT NOT NULL UNIQUE,
    symbol TEXT NOT NULL,
    company TEXT,
    source TEXT,
    source_id INTEGER,
    reason TEXT,
    confidence TEXT,
    entry_price NUMERIC,
    entry_time TIMESTAMPTZ NOT NULL,
    target_min_pct NUMERIC,
    target_max_pct NUMERIC,
    stop_loss_pct NUMERIC,
    hard_stop_pct NUMERIC,
    capital NUMERIC,
    status TEXT NOT NULL DEFAULT 'open',
    exit_price NUMERIC,
    exit_time TIMESTAMPTZ,
    exit_reason TEXT,
    pnl_pct NUMERIC,
    pnl_amount NUMERIC,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    "user" TEXT NOT NULL DEFAULT 'ashish'
);

CREATE TABLE IF NOT EXISTS companies (
    symbol TEXT PRIMARY KEY,
    instrument_key TEXT NOT NULL,
    name TEXT,
    isin TEXT,
    series TEXT,
    cap_tier TEXT,
    cap_tier_updated_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pending_candidates (
    id SERIAL PRIMARY KEY,
    source TEXT,
    symbol TEXT NOT NULL UNIQUE,
    company TEXT,
    confidence TEXT,
    detail TEXT,
    summary TEXT,
    link TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at TIMESTAMPTZ,
    resolved_action TEXT,
    "user" TEXT NOT NULL DEFAULT 'ashish'
);

CREATE TABLE IF NOT EXISTS scan_runs (
    id SERIAL PRIMARY KEY,
    command TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    signals_seen INTEGER,
    positions_opened INTEGER,
    queued INTEGER,
    skipped_quota INTEGER,
    error TEXT,
    "user" TEXT NOT NULL DEFAULT 'ashish'
);

-- NSE's forthcoming-board-meetings calendar (see nse_bot.results_calendar) --
-- shared reference data like `companies`, not user-attributed (NSE's real
-- calendar is the same regardless of who's running the bot). Refreshed on a
-- ~24h cadence (nse_bot.refresh_results_calendar), same pattern as
-- companies.cap_tier.
--
-- news_fetched_at is the "already handled, stop querying" flag -- but it's
-- only ever set once we've actually FOUND real matching news for this
-- company (trade_bot.py's _news_candidates, via db.mark_results_news_found),
-- never just because a query attempt happened. Results routinely declare
-- post-market (verified live: MGEL declared at 16:49:45 IST), so a company
-- can sit in the pending window all morning with nothing to find yet --
-- marking it "done" on the first (empty) attempt would mean never querying
-- it again for the rest of the window, permanently missing the real
-- coverage that shows up that same afternoon. So this stays NULL, and the
-- company keeps getting queried every scan cycle, until genuine coverage is
-- actually found.
--
-- earliest_news_at is that finding's evidence: the earliest matching
-- article's pub_date, not just "now" -- when the news genuinely first broke,
-- not when we happened to notice it.
--
-- best_declared_at is a separate, independent signal: NSE's own official
-- filing timestamp (exchdisstime), filled in by insert_nse_filing() the
-- moment the real "Outcome of Board Meeting" filing comes in for that
-- symbol. "best" because it's only as good as whatever filing we've matched
-- it to (see insert_nse_filing's docstring for the one known edge case).
-- Together: best_declared_at is when the company OFFICIALLY declared;
-- earliest_news_at is when the PRESS first covered it -- two different,
-- independently useful timestamps for the same event.
--
-- quarter is a best-effort 'Q1 FY27'-style label (nse_bot._parse_quarter,
-- parsed from NSE's own board-meeting text) -- distinguishes a company's Q1
-- and Q2 rows directly by label instead of only by declaration_date
-- proximity (see insert_nse_filing's docstring for why that proximity
-- match exists as a fallback: quarter can be None when NSE's free text
-- doesn't match any known phrasing).
--
-- earliest_news_item_id / declared_filing_id are the audit trail for the
-- two timestamps above -- the actual news_items.id / nse_filings.id row
-- that produced each one. REAL foreign keys here (unlike positions.source_id
-- elsewhere in this schema, which is plain INTEGER because it can point to
-- EITHER news_items OR nse_filings depending on `source` -- a single column
-- can't have two REFERENCES targets): each of these two always points to
-- exactly one specific table, so there's no polymorphism problem stopping a
-- real constraint.
CREATE TABLE IF NOT EXISTS results_calendar (
    symbol TEXT NOT NULL,
    company TEXT,
    isin TEXT,
    declaration_date DATE NOT NULL,
    quarter TEXT,
    news_fetched_at TIMESTAMPTZ,
    earliest_news_at TIMESTAMPTZ,
    earliest_news_item_id INTEGER REFERENCES news_items(id),
    best_declared_at TIMESTAMPTZ,
    declared_filing_id INTEGER REFERENCES nse_filings(id),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, declaration_date)
);

-- Test-only fixture data for mock_servers/mock_upstox.py -- real 1-minute
-- OHLCV bars (from yfinance, see mock_servers/generate_minute_fixture.py),
-- NOT touched by the live trading path (trade_bot.py/db.py callers never
-- read this table). Shared/reference data like `companies`, not
-- user-attributed, and not wiped by the news_items/positions/etc cleanup.
CREATE TABLE IF NOT EXISTS mock_price_history (
    symbol TEXT NOT NULL,
    ts TIMESTAMPTZ NOT NULL,
    open NUMERIC, high NUMERIC, low NUMERIC, close NUMERIC, volume BIGINT,
    PRIMARY KEY (symbol, ts)
);

-- Hypothetical "what if we'd bought on every news-resolved symbol" backtest
-- (see analysis/backtest.py) -- read-only exercise data, NOT the real
-- trading path (positions/pending_candidates are untouched by this). Two
-- rows per (calendar_date, symbol): one entry clocked off first_seen_at
-- (when our own pipeline noticed it), one off pub_date (when the article
-- itself was published) -- entry_mode distinguishes them. Simple fixed
-- rule (target >=5%, stop <-2%, else EOD) -- deliberately NOT trade_bot.py's
-- real _qualifies()/stop-loss-only-if-recently-weak logic, since the point
-- here is "every symbol news touched today", not "every symbol our real
-- rules would have bought".
CREATE TABLE IF NOT EXISTS analysis_backtest (
    calendar_date DATE NOT NULL,
    symbol TEXT NOT NULL,
    entry_mode TEXT NOT NULL CHECK (entry_mode IN ('first_seen', 'pub_date')),
    company TEXT,
    cap_tier TEXT,
    headline TEXT,
    link TEXT,
    news_item_id INTEGER REFERENCES news_items(id),
    -- 'auto' | 'manual' -- trade_bot._companies_for_item's own real routing
    -- label (order-category always 'auto'; earnings-category 'auto' only
    -- when classifier.enrich() would mark profit_pct/revenue_pct NOT
    -- low-confidence, else 'manual' -- see analysis/backtest.py's
    -- _confidence_for()). 'manual' means the real pipeline would have
    -- queued this for a human to `confirm`, not bought it outright -- the
    -- stocki Analysis tab's "include waiting for approval" toggle decides
    -- whether these count in the total.
    confidence TEXT,
    entry_time TIMESTAMPTZ NOT NULL,
    entry_price NUMERIC,  -- NULL for exit_reason='no_data' (news seen/published after the last available candle)
    exit_time TIMESTAMPTZ,
    exit_price NUMERIC,
    exit_reason TEXT,  -- 'target' | 'stop' | 'eod' | 'no_data'
    pnl_pct NUMERIC,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (calendar_date, symbol, entry_mode)
);

-- Runtime-tunable knobs (max_trades_per_day, capital_per_trade, ...) --
-- see get_config()/set_config() below. Deliberately just key/value, not one
-- column per knob: new knobs shouldn't need a migration, and nothing here
-- is queried by anything but exact key lookup.
CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One-time seed, not a runtime fallback -- trade_bot.py reads these from the
-- table itself with no hardcoded default in Python; ON CONFLICT DO NOTHING
-- means this only ever sets a value the first time init_schema() runs
-- against a fresh database, never overwrites a value changed since via
-- `trade_bot.py config set`. max_capital_allowed starts at
-- capital_per_trade * max_trades_per_day (all daily trade slots filled
-- simultaneously, no tighter cap than the quota already implies) -- adjust
-- with `trade_bot.py config set max_capital_allowed <value>`.
INSERT INTO config (key, value) VALUES
    ('max_trades_per_day', '3'),
    ('capital_per_trade', '10000'),
    ('max_capital_allowed', '30000')
ON CONFLICT (key) DO NOTHING;

-- Retrofit columns for tables that already existed before this schema
-- revision -- CREATE TABLE IF NOT EXISTS alone won't add columns to a table
-- that's already there, so these run every init on top of it.
ALTER TABLE news_item_companies ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE nse_filings ADD COLUMN IF NOT EXISTS filed_at_ts TIMESTAMPTZ;
ALTER TABLE positions ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE pending_candidates ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMPTZ;
ALTER TABLE pending_candidates ADD COLUMN IF NOT EXISTS resolved_action TEXT;
ALTER TABLE news_items ADD COLUMN IF NOT EXISTS "user" TEXT NOT NULL DEFAULT 'ashish';
ALTER TABLE nse_filings ADD COLUMN IF NOT EXISTS "user" TEXT NOT NULL DEFAULT 'ashish';
ALTER TABLE positions ADD COLUMN IF NOT EXISTS "user" TEXT NOT NULL DEFAULT 'ashish';
ALTER TABLE pending_candidates ADD COLUMN IF NOT EXISTS "user" TEXT NOT NULL DEFAULT 'ashish';
-- news_item_companies: same attribution as its parent news_items row --
-- lets a mock/test run's company-mention rows be told apart from (or wiped
-- independent of) real ones, same reasoning as every other per-row "user"
-- column above.
ALTER TABLE news_item_companies ADD COLUMN IF NOT EXISTS "user" TEXT NOT NULL DEFAULT 'ashish';
-- results_calendar: NOTE the semantic difference from every other "user"
-- column here -- this table is genuinely shared/global data (NSE's real
-- calendar doesn't depend on who's running the bot, see the schema comment
-- above), keyed on (symbol, declaration_date) with no per-user split. This
-- column is NOT an ownership partition -- it's "who/what last wrote this
-- row" bookkeeping only (upsert_results_calendar overwrites it on every
-- refresh, same as updated_at), purely so a mock/test run that touches the
-- real database can be told apart from a real cron refresh. It is NOT safe
-- to filter by "user" here expecting a per-user view of the calendar --
-- there's only ever one row per (symbol, declaration_date), shared.
ALTER TABLE results_calendar ADD COLUMN IF NOT EXISTS "user" TEXT NOT NULL DEFAULT 'ashish';
ALTER TABLE companies ADD COLUMN IF NOT EXISTS series TEXT;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS cap_tier TEXT;
ALTER TABLE companies ADD COLUMN IF NOT EXISTS cap_tier_updated_at TIMESTAMPTZ;
ALTER TABLE results_calendar ADD COLUMN IF NOT EXISTS best_declared_at TIMESTAMPTZ;
ALTER TABLE results_calendar ADD COLUMN IF NOT EXISTS earliest_news_at TIMESTAMPTZ;
ALTER TABLE results_calendar ADD COLUMN IF NOT EXISTS quarter TEXT;
ALTER TABLE results_calendar ADD COLUMN IF NOT EXISTS earliest_news_item_id INTEGER;
ALTER TABLE results_calendar ADD COLUMN IF NOT EXISTS declared_filing_id INTEGER;

-- Retrofit the real FK constraints onto those two columns for installs that
-- already had them as plain INTEGER (added before this constraint existed).
-- Postgres has no ADD CONSTRAINT IF NOT EXISTS, hence the DO-block/exception
-- idiom -- makes re-running this on an install that already has the
-- constraint a no-op instead of an error.
DO $$
BEGIN
    ALTER TABLE results_calendar
        ADD CONSTRAINT results_calendar_earliest_news_item_id_fkey
        FOREIGN KEY (earliest_news_item_id) REFERENCES news_items(id);
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    ALTER TABLE results_calendar
        ADD CONSTRAINT results_calendar_declared_filing_id_fkey
        FOREIGN KEY (declared_filing_id) REFERENCES nse_filings(id);
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;
-- Market-closed AMO-style limit orders (weekend OR weekday outside NSE
-- trading hours -- see trading/trade_bot.py's
-- _queue_offhours_order/_process_queued_orders) reuse `positions` itself
-- (status='queued', a value no existing query anywhere -- stocki's
-- queries.ts included -- filters for, so this needs zero changes to any
-- existing query) rather than a new table: entry_price/entry_time/
-- target_*_pct/stop_loss_pct all stay NULL until the order actually fills
-- (entry_price becomes the real fill price then, not the limit), and this
-- one nullable column holds the limit price while status='queued'.
ALTER TABLE positions ADD COLUMN IF NOT EXISTS limit_price NUMERIC;
ALTER TABLE analysis_backtest ADD COLUMN IF NOT EXISTS confidence TEXT;
-- True for rows inserted by news_bot.py's standalone --days backfill, false
-- (the default) for rows from trade_bot.py's live `scan` pipeline. The two
-- are NOT equivalent: news_bot.py only fetches/classifies/stores -- it never
-- calls trade_bot.py's _qualifies()/_decide()/db.add_pending(), so a
-- backfilled item never gets a chance to land in pending_candidates even if
-- it would have qualified. Lets stocki's queries.ts tell "genuinely sitting
-- in the review queue" apart from "never evaluated in the first place",
-- instead of both looking like 'pending review'.
ALTER TABLE news_items ADD COLUMN IF NOT EXISTS via_backfill BOOLEAN NOT NULL DEFAULT false;

-- Heartbeat for long-running scan_runs rows (monitor --tick, scan/monitor
-- --watch) -- a run that's designed to stay alive for days only ever writes
-- finished_at on exit, so stocki's HealthStrip (which flags "no finished_at
-- + started_at is old" as degraded/stuck) couldn't tell a healthy days-old
-- watcher from a dead one; both look identical without this. Written every
-- cycle (see trade_bot.py's watch/tick loops), including cycles where
-- nothing happened -- staying alive with zero open positions is monitor
-- --tick's normal idle state, not evidence of a problem.
ALTER TABLE scan_runs ADD COLUMN IF NOT EXISTS last_heartbeat_at TIMESTAMPTZ;

-- Unconditional worst-case exit floor, unlike stop_loss_pct (which is NULL
-- unless the stock was already "recently weak" at entry -- see trade_bot.py's
-- STOP_LOSS_PCT/WEAKNESS_THRESHOLD notes). hard_stop_pct is set on every
-- position regardless of that check, so a "no stop-loss, target-only exit"
-- position still can't ride a loss indefinitely between now and its EOD
-- deadline -- it exits the moment it breaches this floor either way.
ALTER TABLE positions ADD COLUMN IF NOT EXISTS hard_stop_pct NUMERIC;

-- Cross-cutting health signals that aren't tied to one scan_runs row --
-- currently just whether the last real Upstox API call succeeded. Written
-- from trade_bot.py's _market_is_closed(), which already makes a live
-- Upstox call (is_market_open) every scan/monitor cycle and already has a
-- success branch and a failure-fallback branch -- this just records which
-- one fired, instead of the bot silently running on with a dead token
-- (verified real gap: upstox_client.py's own `status` subcommand only
-- checks the token file's `issued` date, not a live call, so it can report
-- "valid" for a token Upstox has already invalidated server-side; tokens
-- expire ~3:30am daily and a --watch/--tick loop straddling that gets no
-- warning at all today). Plain key/value, not one column per check, same
-- reasoning as `config` above -- a future health signal shouldn't need a
-- migration.
CREATE TABLE IF NOT EXISTS system_status (
    key TEXT PRIMARY KEY,
    ok BOOLEAN NOT NULL,
    detail TEXT,
    checked_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS quarterly_financials (
    id SERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    company TEXT,
    quarter TEXT NOT NULL,
    quarter_end_date DATE,
    -- Income Statement
    revenue NUMERIC,
    cogs NUMERIC,
    gross_profit NUMERIC,
    operating_expenses NUMERIC,
    ebitda NUMERIC,
    depreciation_amortization NUMERIC,
    ebit NUMERIC,
    finance_costs NUMERIC,
    profit_before_tax NUMERIC,
    tax_expense NUMERIC,
    net_income NUMERIC,
    exceptional_items NUMERIC,
    -- Per Share Metrics
    eps_basic NUMERIC,
    eps_diluted NUMERIC,
    book_value_per_share NUMERIC,
    dividend_per_share NUMERIC,
    -- Margins
    gross_margin_pct NUMERIC,
    operating_margin_pct NUMERIC,
    ebitda_margin_pct NUMERIC,
    net_margin_pct NUMERIC,
    -- Balance Sheet
    total_assets NUMERIC,
    current_assets NUMERIC,
    current_liabilities NUMERIC,
    total_liabilities NUMERIC,
    equity NUMERIC,
    -- Ratios
    current_ratio NUMERIC,
    debt_to_equity_ratio NUMERIC,
    interest_coverage_ratio NUMERIC,
    roe_pct NUMERIC,
    roa_pct NUMERIC,
    -- Cash Flow
    operating_cf NUMERIC,
    capex NUMERIC,
    free_cf NUMERIC,
    -- Growth
    qoq_growth_pct NUMERIC,
    yoy_growth_pct NUMERIC,
    filing_url TEXT,
    extraction_quality TEXT,
    extraction_issues TEXT,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    "user" TEXT NOT NULL DEFAULT 'ashish',
    UNIQUE(symbol, quarter)
);
"""

_conn = None


def get_connection():
    global _conn
    if _conn is None or _conn.closed:
        # connect_timeout bounds a single connect attempt -- without it, a
        # genuinely unreachable host can hang for OS-level TCP-timeout
        # minutes per attempt, which would make get_config()'s "retry a few
        # times then raise" take far longer than intended.
        _conn = psycopg2.connect(host=DB_HOST, dbname=DB_NAME, user=DB_USER, connect_timeout=5)
        _conn.autocommit = True
    return _conn


@contextlib.contextmanager
def _transaction():
    """Every connection here runs autocommit=True (see get_connection) --
    fine for single-statement functions, but a handful of functions
    (insert_news_item, insert_nse_filing) do MULTIPLE related writes that
    need to land as one unit. Under autocommit, each statement commits the
    instant it runs, so a network drop mid-sequence leaves whatever ran so
    far permanently committed and the rest permanently lost -- verified
    live: a drop between company inserts left a real news_items row stuck
    with 1 of 3 mentioned companies forever (nothing ever retries it, title
    is UNIQUE), and a drop between the nse_filings insert and its
    results_calendar cascade update left that calendar entry orphaned
    forever (is_nse_seen(seq_id) now returns true, so the filing is never
    reprocessed). This wraps a block of statements in a real transaction:
    commit only if the whole block succeeds, rollback (undoing anything
    that ran) otherwise -- so a mid-sequence failure leaves NEITHER write
    applied, instead of a silently partial one.

    Toggles autocommit off/back-on around the block rather than assuming
    callers manage it -- safe here because this project is single-threaded
    per process and reuses one global connection (_conn), so there's no
    concurrent statement interleaving to worry about on the same connection."""
    conn = get_connection()
    conn.autocommit = False
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.autocommit = True


def init_schema():
    with get_connection().cursor() as cur:
        cur.execute(SCHEMA)


# ---- central clock ----
#
# One function for "what time is it right now" that every trading-decision
# timestamp goes through (entry_time, exit_time, the EOD-deadline check,
# same-day-quota checks) -- real wall-clock time by default, everywhere,
# always. Tests mock it in exactly one place (set_clock) instead of each
# call site inventing its own "am I talking to a mock?" check, which is how
# entry_time/exit_time and the daily-quota check ended up silently
# disagreeing with each other before: entry_time started using the mock's
# simulated per-symbol replay time, but bought_today()/count_trades_today()
# were still comparing against Postgres's real CURRENT_DATE, so a
# same-day trade limit silently stopped applying during tests. Routing both
# through the same now(symbol) fixes that at the root instead of patching
# each symptom.
#
# symbol is optional and matters only when a clock override is installed:
# the mock replay clock is per-symbol (each ticker's LTP replay can be at a
# different simulated minute), so the override function takes a symbol and
# may return None (e.g. no replay data for that symbol) -- now() falls back
# to real wall-clock time whenever there's no override, or the override
# doesn't have an answer for that symbol.
_clock_override = None


def set_clock(fn):
    """Test-only. fn(symbol) -> datetime or None; None resets to always-real
    wall-clock time. Typically: db.set_clock(upstox_client.get_mock_replay_time),
    which already returns None by itself whenever UPSTOX_API_BASE is the
    real API -- so installing it is harmless even outside tests, but nothing
    in the live trading path does that automatically; only test code should
    call set_clock at all."""
    global _clock_override
    _clock_override = fn


def now(symbol=None):
    """The central 'current time' -- see module notes above. Use this
    everywhere a trading decision needs to know "what time is it", instead
    of calling datetime.now() directly."""
    if _clock_override is not None:
        overridden = _clock_override(symbol)
        if overridden is not None:
            return overridden
    return datetime.now(timezone.utc)


def _dict_cursor():
    return get_connection().cursor(cursor_factory=psycopg2.extras.RealDictCursor)


# ---- config ----
#
# Runtime knobs that used to be CLI-flag-only (--max-trades-per-day,
# --capital-per-trade), now DB-backed so they persist across invocations
# without having to remember/retype a flag every time, and so `scan --watch`
# (long-running) and a one-off `confirm` agree on the same value without
# either having been started with matching flags. Values are always stored
# as text; callers cast to whatever type they need (see trade_bot.py's
# _load_config_defaults). An explicit CLI flag still overrides the stored
# value for that one invocation -- this table only supplies the default.

_CONFIG_CONNECT_ATTEMPTS = 3
_CONFIG_CONNECT_RETRY_SECONDS = 2


def get_config(key, default=None):
    """`default` covers only "this key genuinely isn't in the table" -- a
    real connection failure retries a few times and then raises, it never
    falls back to `default` silently. A missing config value and a database
    that's actually unreachable are different failure modes and shouldn't
    look the same to the caller (trade_bot.py's _load_config_defaults relies
    on this: it treats a raised exception here as "can't start", not as
    another reason to apply its own test-only fallback)."""
    global _conn
    last_error = None
    for attempt in range(_CONFIG_CONNECT_ATTEMPTS):
        try:
            with get_connection().cursor() as cur:
                cur.execute("SELECT value FROM config WHERE key = %s", (key,))
                row = cur.fetchone()
                return row[0] if row else default
        except psycopg2.OperationalError as e:
            last_error = e
            _conn = None  # drop the dead connection so the next attempt reconnects
            if attempt < _CONFIG_CONNECT_ATTEMPTS - 1:
                time.sleep(_CONFIG_CONNECT_RETRY_SECONDS)
    raise RuntimeError(
        f"Could not reach Postgres to read config key {key!r} after {_CONFIG_CONNECT_ATTEMPTS} attempts: {last_error}"
    )


def get_all_config():
    with get_connection().cursor() as cur:
        cur.execute("SELECT key, value FROM config ORDER BY key")
        return {k: v for k, v in cur.fetchall()}


def set_config(key, value):
    with get_connection().cursor() as cur:
        cur.execute(
            """INSERT INTO config (key, value, updated_at) VALUES (%s, %s, now())
               ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
            (key, str(value)),
        )


# ---- news_items ----

def is_news_seen(title):
    with get_connection().cursor() as cur:
        cur.execute("SELECT 1 FROM news_items WHERE title = %s", (title,))
        return cur.fetchone() is not None


def get_news_item_id_by_title(title):
    """title is UNIQUE on news_items -- used by results-calendar resolution
    to link an already-seen article (one not inserted THIS scan cycle, so
    trade_bot.py never captured its id) back to its existing row."""
    with get_connection().cursor() as cur:
        cur.execute("SELECT id FROM news_items WHERE title = %s", (title,))
        row = cur.fetchone()
        return row[0] if row else None


def count_news_item_companies(news_item_id):
    """How many DIFFERENT companies this article resolved to -- used to
    suppress results_calendar auto-marking for a roundup/multi-company
    article (see trade_bot.py's results-calendar resolution loop). Verified
    live and real: a single "Stocks to Watch: DLF, Torrent Power, Jindal
    Stainless and 7 Other Companies..." article resolved to 3 companies
    here (DLF, TORNTPOWER, JSL -- the LLM only extracts explicitly NAMED
    companies, not the unnamed "7 Other Companies") and, before this
    existed, still ended up marking NINE calendar entries as "news found"
    via the separate title-text-substring matching path in
    _item_matches_calendar_entry (which doesn't go through this table at
    all) -- none of which had actually reported anything, since the article
    was a pure preview of who was due to report that day."""
    with get_connection().cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM news_item_companies WHERE news_item_id = %s", (news_item_id,))
        return cur.fetchone()[0]


def get_news_titles_matching(name_substring, as_of=None):
    """[(title, pub_date), ...] for every news_items row whose title
    contains name_substring (case-insensitive) -- used to detect a
    republished/re-indexed article passing itself off as fresh (see
    trade_bot.py's republish-detection docstring). Deliberately searches
    ALL of news_items, NOT joined through news_item_companies -- verified
    live that the real duplicate this exists for (a 2024 Dhanuka Agritech
    Q1 result, re-surfaced by Google News with a 2026 pub_date, identical
    headline and numbers) was itself correctly marked event_type='STALE',
    and STALE articles never get resolved to a company at all (their
    mentioned_companies is cleared) -- so a join through
    news_item_companies would silently miss exactly the rows this check
    most needs to find.

    as_of, if given, restricts the search to rows with first_seen_at <=
    as_of -- i.e. only what would ALREADY have existed in this table at
    that moment. Real production calls (trade_bot.py's live scan) never
    pass this, since "right now" and "everything currently in the table"
    are the same thing for them. It exists for analysis/backtest.py, which
    replays this exact check hours/days after the fact: without it, a
    duplicate article that got inserted LATER THE SAME DAY (after the real
    decision already happened) would retroactively flag a headline as
    republished that the real live scan correctly did NOT flag at the time
    -- verified live and real: this happened for the exact Dhanuka Agritech
    case in this docstring's other example, once replayed after a same-day
    duplicate landed."""
    with get_connection().cursor() as cur:
        if as_of is not None:
            cur.execute(
                "SELECT title, pub_date FROM news_items WHERE title ILIKE %s AND first_seen_at <= %s",
                (f"%{name_substring}%", as_of),
            )
        else:
            cur.execute(
                "SELECT title, pub_date FROM news_items WHERE title ILIKE %s",
                (f"%{name_substring}%",),
            )
        return cur.fetchall()


def insert_news_item(item):
    """item: dict with title, link, source, pub_date, matched_query, money,
    event_type, category, signal, mentioned_companies (list of {symbol, name,
    instrument_key, match_type}), via_backfill (optional, default False --
    see news_items.via_backfill schema notes). Idempotent on title -- safe
    to call from both news_bot.py and trade_bot.py without double-inserting.

    Wrapped in _transaction() -- the news_items row and every one of its
    mentioned_companies rows must land together or not at all. Verified
    live: without this, a network drop mid-loop left a real headline
    permanently attributed to only 1 of its 3 actual mentioned companies
    (nothing ever retries this function for the same title), which would
    have silently defeated any "more than one company -> needs review"
    check relying on that count."""
    with _transaction() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO news_items
               (title, link, source, pub_date, matched_query, money, event_type, category, signal, via_backfill, "user")
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (title) DO NOTHING
               RETURNING id""",
            (item["title"], item.get("link"), item.get("source"), item.get("pub_date"),
             item.get("matched_query"), item.get("money"), item.get("event_type"),
             item.get("category"), item.get("signal"), item.get("via_backfill", False), _app_user()),
        )
        row = cur.fetchone()
        if row is None:
            cur.execute("SELECT id FROM news_items WHERE title = %s", (item["title"],))
            row = cur.fetchone()
        news_item_id = row[0]

        for m in item.get("mentioned_companies", []):
            cur.execute(
                """INSERT INTO news_item_companies (news_item_id, symbol, name, instrument_key, match_type, "user")
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (news_item_id, symbol) DO NOTHING""",
                (news_item_id, m["symbol"], m.get("name"), m.get("instrument_key"), m.get("match_type"), _app_user()),
            )
    return news_item_id


# ---- nse_filings ----

def is_nse_seen(seq_id):
    with get_connection().cursor() as cur:
        cur.execute("SELECT 1 FROM nse_filings WHERE seq_id = %s", (str(seq_id),))
        return cur.fetchone() is not None


def _parse_nse_timestamp(raw):
    """NSE's exchdisstime is a free-text timestamp string whose exact format
    isn't guaranteed stable across endpoints -- use a lenient parser rather
    than a hardcoded strptime format, and just leave filed_at_ts NULL (raw
    filed_at is kept either way) if it can't be parsed."""
    if not raw:
        return None
    from dateutil import parser as dateutil_parser
    try:
        dt = dateutil_parser.parse(raw, dayfirst=True)
    except (ValueError, OverflowError):
        return None
    if dt.tzinfo is None:
        from datetime import timezone, timedelta
        dt = dt.replace(tzinfo=timezone(timedelta(hours=5, minutes=30)))  # NSE times are IST
    return dt


def insert_nse_filing(item, signal_tag):
    """signal_tag == 'results' also opportunistically fills in
    results_calendar.best_declared_at for this symbol, using this filing's
    real exchdisstime -- the moment NSE's own "Outcome of Board Meeting"
    filing actually lands, not a guess.

    A symbol can have more than one pending (best_declared_at IS NULL) row
    at once -- most commonly Q1's row never got resolved (e.g. this process
    wasn't running when its real filing came in) and Q2's row has since
    appeared. Matching by symbol alone in that case would UPDATE both rows
    with the SAME (Q2's) timestamp, silently corrupting Q1's -- Postgres
    updates every row a WHERE clause matches, not just one. So the inner
    SELECT picks the single unresolved row whose declaration_date is
    CLOSEST to this filing's actual date (typically the same day or within
    a couple, since board meetings rarely slip far from schedule) -- in the
    normal case there's only one unresolved row anyway and this is a no-op
    difference; it only matters once two are pending simultaneously.

    Wrapped in _transaction() -- the nse_filings insert and its
    results_calendar cascade update must land together. Verified live:
    without this, a network drop between the two left the filing committed
    but the cascade lost, and since is_nse_seen(seq_id) now returns true for
    this filing, no future scan ever reprocesses it -- the results_calendar
    row stayed orphaned (best_declared_at/declared_filing_id stuck NULL)
    permanently, with nothing anywhere flagging it as broken."""
    with _transaction() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO nse_filings (seq_id, symbol, company, isin, category, signal, amount, summary, filing_url, filed_at, filed_at_ts, "user")
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (seq_id) DO NOTHING
               RETURNING id""",
            (str(item.get("seq_id")), item.get("symbol"), item.get("sm_name"), item.get("sm_isin"),
             item.get("desc"), signal_tag, item.get("amount"), item.get("attchmntText"),
             item.get("attchmntFile"), item.get("exchdisstime"), _parse_nse_timestamp(item.get("exchdisstime")), _app_user()),
        )
        row = cur.fetchone()
        if row is None:
            cur.execute("SELECT id FROM nse_filings WHERE seq_id = %s", (str(item.get("seq_id")),))
            row = cur.fetchone()

        if signal_tag == "results":
            filed_at_ts = _parse_nse_timestamp(item.get("exchdisstime"))
            if filed_at_ts is not None and item.get("symbol"):
                cur.execute(
                    """UPDATE results_calendar SET best_declared_at = %s, declared_filing_id = %s
                       WHERE symbol = %s AND declaration_date = (
                           SELECT declaration_date FROM results_calendar
                            WHERE symbol = %s AND best_declared_at IS NULL
                            ORDER BY ABS(declaration_date - %s::date)
                            LIMIT 1
                       )""",
                    (filed_at_ts, row[0], item["symbol"], item["symbol"], filed_at_ts),
                )
        return row[0]


# ---- positions ----

def open_position(symbol, company, source, source_id, reason, confidence, entry_price, entry_time,
                   target_min_pct, target_max_pct, stop_loss_pct, hard_stop_pct, capital):
    """source: 'news' or 'nse'. source_id: the originating news_items.id or
    nse_filings.id row -- the audit trail for "why was this trade taken".

    TODO once real order placement exists: status='open' here currently just
    means "we decided to buy", not "NSE confirmed a fill" -- get_open_capital()
    counts this row's capital immediately. A real placed-but-rejected/failed
    order must free that capital back up (see CLAUDE.md's "Known untested /
    open items" for the fuller note) instead of leaving it stuck as phantom
    deployed capital against max_capital_allowed."""
    transaction_id = uuid.uuid4().hex
    with get_connection().cursor() as cur:
        cur.execute(
            """INSERT INTO positions
               (transaction_id, symbol, company, source, source_id, reason, confidence, entry_price, entry_time,
                target_min_pct, target_max_pct, stop_loss_pct, hard_stop_pct, capital, status, "user")
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'open', %s)
               RETURNING id""",
            (transaction_id, symbol, company, source, source_id, reason, confidence, entry_price, entry_time,
             target_min_pct, target_max_pct, stop_loss_pct, hard_stop_pct, capital, _app_user()),
        )
        return cur.fetchone()[0]


def bought_today(symbol):
    """True if we opened ANY position (open or already closed) for this
    symbol today -- don't re-buy the same stock twice in one day. "Today" is
    now(symbol)'s date, not Postgres's CURRENT_DATE -- see the central-clock
    notes above init_schema for why that distinction matters. Converted to
    IST before taking .date() -- see the IST constant's notes on why (must
    match entry_time::date's Asia/Kolkata-based cast in the SQL below).
    Excludes status='queued': a market-closed-queued-but-not-yet-filled order
    is only an intent, not an actual buy, and shouldn't block a real one."""
    reference_date = now(symbol).astimezone(IST).date()
    with get_connection().cursor() as cur:
        cur.execute(
            "SELECT 1 FROM positions WHERE symbol = %s AND entry_time::date = %s AND status != 'queued'",
            (symbol, reference_date),
        )
        return cur.fetchone() is not None


def has_active_position(symbol):
    """True if `symbol` already has a position that's open OR queued (a
    resting off-hours limit order). Wider than both bought_today() (only
    "today", and explicitly excludes 'queued') and get_open_symbols() (only
    'open') -- neither alone is enough to stop a SECOND position being
    opened for a symbol that already has one from a previous day, or that
    already has a queued limit order outstanding. _decide() (trade_bot.py)
    is protected today only because it separately checks
    get_open_symbols() before ever calling _open_position(); cmd_confirm
    had no equivalent check at all -- a real gap, since it's a live command
    that opens real (paper) positions -- which is what this function now
    closes at the call site in cmd_confirm."""
    with get_connection().cursor() as cur:
        cur.execute("SELECT 1 FROM positions WHERE symbol = %s AND status IN ('open', 'queued')", (symbol,))
        return cur.fetchone() is not None


# bought_today()/count_trades_today() are live DB reads, but reading and
# then acting on them (trade_bot._open_position) are two separate round
# trips -- fine within one process, but if a second process is also running
# (e.g. `confirm` invoked while `scan --watch` is mid-cycle) both can read
# "not bought yet"/"under quota" before either has inserted its row, and both
# end up opening a position. pg_advisory_lock is session-scoped, i.e. one per
# process's connection, which is exactly the granularity needed to serialize
# across separate trade_bot.py invocations -- an arbitrary constant key is
# fine since there's only ever one kind of critical section to protect.
_QUOTA_LOCK_KEY = 918273645


def get_open_capital():
    """Sum of `capital` across currently-open positions -- the live exposure
    figure max_capital_allowed caps. Closed positions don't count: a closed
    trade's capital is back in the (paper) pool, free to redeploy."""
    with get_connection().cursor() as cur:
        cur.execute("SELECT COALESCE(SUM(capital), 0) FROM positions WHERE status = 'open'")
        return float(cur.fetchone()[0])


def _locked_quota_check(symbol, reference_date, capital, max_trades_per_day, max_capital_allowed):
    """The actual bought_today()/count_trades_today()/get_open_capital()
    re-check shared by try_open_position and try_fill_queued_position --
    caller must already hold _QUOTA_LOCK_KEY. Returns a reason string in
    ('already_bought', 'quota', 'capital') if this isn't allowed right now,
    else None.

    Both queries exclude status='queued': a merely-queued market-closed
    intent hasn't actually been bought yet and shouldn't consume a same-day quota
    slot or count as "already bought" for its own symbol -- this also
    correctly excludes try_fill_queued_position's own row from self-matching
    itself (it's still 'queued' at the moment this check runs, before the
    UPDATE that flips it to 'open')."""
    with get_connection().cursor() as cur:
        cur.execute(
            "SELECT 1 FROM positions WHERE symbol = %s AND entry_time::date = %s AND status != 'queued'",
            (symbol, reference_date),
        )
        if cur.fetchone() is not None:
            return "already_bought"
        cur.execute(
            "SELECT COUNT(*) FROM positions WHERE entry_time::date = %s AND status != 'queued'",
            (reference_date,),
        )
        if cur.fetchone()[0] >= max_trades_per_day:
            return "quota"
    if get_open_capital() + capital > max_capital_allowed:
        return "capital"
    return None


def try_open_position(symbol, company, source, source_id, reason, confidence, entry_price, entry_time,
                       target_min_pct, target_max_pct, stop_loss_pct, capital, max_trades_per_day,
                       max_capital_allowed, hard_stop_pct=None):
    """Re-checks bought_today()/count_trades_today()/get_open_capital() and
    inserts, atomically, under a Postgres advisory lock -- see
    _QUOTA_LOCK_KEY above for why. Returns (position_id, None) on success, or
    (None, block_reason) with block_reason in
    ('already_bought', 'quota', 'capital') if this call lost that race (or
    simply wasn't first) since the caller's own last check.

    hard_stop_pct defaults to None (not TARGET_PCT/STOP_LOSS_PCT's tighter
    default) purely so existing keyword-arg call sites/tests that predate
    this column don't break -- trade_bot.py's real call site always passes
    its HARD_STOP_PCT constant explicitly."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(%s)", (_QUOTA_LOCK_KEY,))
    try:
        block_reason = _locked_quota_check(
            symbol, entry_time.astimezone(IST).date(), capital, max_trades_per_day, max_capital_allowed)
        if block_reason:
            return None, block_reason
        position_id = open_position(
            symbol, company, source, source_id, reason, confidence, entry_price, entry_time,
            target_min_pct, target_max_pct, stop_loss_pct, hard_stop_pct, capital,
        )
        return position_id, None
    finally:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (_QUOTA_LOCK_KEY,))


# ---- market-closed-queued positions (status='queued' -- see schema notes above) ----

def queue_offhours_position(symbol, company, source, source_id, reason, confidence, limit_price, capital, queued_at):
    """Insert a 'queued' row -- no atomic quota/capital check here, since
    queuing an intent isn't buying; try_fill_queued_position does that
    check at the moment it actually fills."""
    transaction_id = uuid.uuid4().hex
    with get_connection().cursor() as cur:
        cur.execute(
            """INSERT INTO positions
               (transaction_id, symbol, company, source, source_id, reason, confidence,
                entry_time, capital, limit_price, status, "user")
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'queued', %s)
               RETURNING id""",
            (transaction_id, symbol, company, source, source_id, reason, confidence,
             queued_at, capital, limit_price, _app_user()),
        )
        return cur.fetchone()[0]


def is_position_queued(symbol):
    """True if `symbol` already has a not-yet-filled market-closed order --
    avoids re-queuing (and re-computing a fresh limit price for) the same
    symbol on every --watch cycle while the market stays closed."""
    with get_connection().cursor() as cur:
        cur.execute("SELECT 1 FROM positions WHERE symbol = %s AND status = 'queued'", (symbol,))
        return cur.fetchone() is not None


def get_queued_positions():
    """TODO(prod-readiness): no age filter here, and nothing anywhere ever
    expires a queued row -- a resting off-hours limit order (see
    trade_bot.py's _queue_offhours_order/_process_queued_orders) can sit
    queued indefinitely, filling weeks later against a limit price still
    anchored to the close of the day it was queued. That limit ceiling was
    only ever meant to "stand in for the 0-5% gain-band check ... since
    there's no live gain to check yet" (see _queue_offhours_order's
    docstring) -- a substitution that's only valid on the NEXT open, not
    indefinitely. This makes the off-hours path the one route into
    `positions` with no freshness gate at all (NEWS_MAX_AGE, the intraday
    gain band, and _price_already_moved are all bypassed for it). Intended
    fix: expire a queued order at the end of the next trading session it
    could have filled in, same idea as a real broker's AMO. Deliberately not
    fixed yet -- flagged and deferred."""
    with _dict_cursor() as cur:
        cur.execute("SELECT * FROM positions WHERE status = 'queued' ORDER BY entry_time")
        return [dict(r) for r in cur.fetchall()]


def try_fill_queued_position(position_id, symbol, entry_price, entry_time, target_min_pct, target_max_pct,
                              stop_loss_pct, capital, max_trades_per_day, max_capital_allowed, hard_stop_pct=None):
    """Fill a queued market-closed position now that the market's open and its
    limit condition is met -- this IS the moment the "buy" actually happens,
    so it gets the exact same atomic quota/capital re-check as
    try_open_position, just UPDATEing the existing queued row instead of
    inserting a new one. Returns (position_id, None) on success, or
    (None, block_reason). Same real-order-placement TODO as open_position
    applies here too -- see its docstring. hard_stop_pct defaults to None for
    the same pre-existing-call-site reason as try_open_position's."""
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(%s)", (_QUOTA_LOCK_KEY,))
    try:
        block_reason = _locked_quota_check(
            symbol, entry_time.astimezone(IST).date(), capital, max_trades_per_day, max_capital_allowed)
        if block_reason:
            return None, block_reason
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE positions SET entry_price = %s, entry_time = %s, target_min_pct = %s,
                   target_max_pct = %s, stop_loss_pct = %s, hard_stop_pct = %s, status = 'open' WHERE id = %s""",
                (entry_price, entry_time, target_min_pct, target_max_pct, stop_loss_pct, hard_stop_pct, position_id),
            )
        return position_id, None
    finally:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (_QUOTA_LOCK_KEY,))


def close_position(position_id, exit_price, exit_time, exit_reason, pnl_pct, pnl_amount):
    with get_connection().cursor() as cur:
        cur.execute(
            """UPDATE positions SET status='closed', exit_price=%s, exit_time=%s,
               exit_reason=%s, pnl_pct=%s, pnl_amount=%s WHERE id=%s""",
            (exit_price, exit_time, exit_reason, pnl_pct, pnl_amount, position_id),
        )


def get_open_positions():
    with _dict_cursor() as cur:
        cur.execute("SELECT * FROM positions WHERE status = 'open' ORDER BY entry_time")
        return [dict(r) for r in cur.fetchall()]


def get_open_symbols():
    with get_connection().cursor() as cur:
        cur.execute("SELECT symbol FROM positions WHERE status = 'open'")
        return {r[0] for r in cur.fetchall()}


def count_trades_today(symbol=None):
    """How many trades landed on "today" -- symbol's effective day if given
    (see now()'s per-symbol override), else real wall-clock today. Pass the
    candidate symbol being evaluated when checking it against the daily
    quota, so the quota's notion of "today" agrees with whatever entry_time
    that candidate would actually be stamped with if opened. Converted to
    IST before taking .date() -- see the IST constant's notes on why.
    Excludes status='queued': the quota caps actual fills, not merely-queued
    market-closed intents that might never fill (verified live: without this, a
    still-queued order could eat a same-day quota slot from an unrelated
    symbol's real buy attempt)."""
    reference_date = now(symbol).astimezone(IST).date()
    with get_connection().cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM positions WHERE entry_time::date = %s AND status != 'queued'",
            (reference_date,),
        )
        return cur.fetchone()[0]


# ---- companies (Upstox instrument master) ----

def upsert_companies(records):
    """records: dict of symbol -> {instrument_key, name, isin, series}, as
    produced by instrument_master.py's Upstox download. Replaces the old
    JSON cache file. series: NSE trading series (EQ/BE/BZ = main board,
    SM/ST = SME/Emerge platform) -- used to tier earnings-qualification
    thresholds in trade_bot.py."""
    with get_connection().cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """INSERT INTO companies (symbol, instrument_key, name, isin, series, updated_at)
               VALUES %s
               ON CONFLICT (symbol) DO UPDATE SET
                   instrument_key = EXCLUDED.instrument_key,
                   name = EXCLUDED.name,
                   isin = EXCLUDED.isin,
                   series = EXCLUDED.series,
                   updated_at = EXCLUDED.updated_at""",
            [(symbol, rec["instrument_key"], rec.get("name"), rec.get("isin"), rec.get("series"), _now())
             for symbol, rec in records.items()],
        )


def get_company(symbol):
    with _dict_cursor() as cur:
        cur.execute("SELECT * FROM companies WHERE symbol = %s", (symbol,))
        row = cur.fetchone()
        return dict(row) if row else None


def get_all_companies():
    with _dict_cursor() as cur:
        cur.execute("SELECT * FROM companies")
        return {r["symbol"]: dict(r) for r in cur.fetchall()}


def companies_last_updated():
    """Most recent updated_at across all companies, or None if the table's empty."""
    with get_connection().cursor() as cur:
        cur.execute("SELECT MAX(updated_at) FROM companies")
        return cur.fetchone()[0]


def set_cap_tiers(tiers):
    """tiers: {tier_name: symbol_set}, e.g. {'large': {...}, 'mid': {...},
    'small': {...}} -- see broker/large_cap.py. A full reset-then-mark on
    every refresh (not just an upsert of the new sets) so a company dropped
    from a real index (reconstitution happens periodically) doesn't stay
    incorrectly tagged forever from a stale previous fetch."""
    with get_connection().cursor() as cur:
        cur.execute('UPDATE companies SET cap_tier = NULL, cap_tier_updated_at = now()')
        for tier, symbols in tiers.items():
            if not symbols:
                continue
            cur.execute(
                "UPDATE companies SET cap_tier = %s, cap_tier_updated_at = now() WHERE symbol = ANY(%s)",
                (tier, list(symbols)),
            )


def cap_tier_last_updated():
    with get_connection().cursor() as cur:
        cur.execute("SELECT MAX(cap_tier_updated_at) FROM companies")
        return cur.fetchone()[0]


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


# ---- results_calendar (NSE forthcoming board meetings, see nse_bot.py) ----

def results_calendar_last_updated():
    with get_connection().cursor() as cur:
        cur.execute("SELECT MAX(updated_at) FROM results_calendar")
        return cur.fetchone()[0]


def upsert_results_calendar(entries):
    """entries: list of {symbol, company, isin, date, quarter} as produced by
    nse_bot.results_calendar() -- `date` is NSE's raw 'DD-Mon-YYYY' string,
    parsed to a real DATE here; `quarter` may be None (best-effort parse,
    not guaranteed). news_fetched_at is deliberately NOT reset on conflict
    -- re-running the daily refresh must not un-mark a company whose
    dedicated news query already ran for that declaration."""
    if not entries:
        return
    from datetime import datetime as _dt
    # "user" is overwritten on every conflict, same as updated_at -- it
    # tags whoever last refreshed this row, not who "owns" it (see the
    # schema comment on this column for why there's no per-user split here).
    user = _app_user()
    with get_connection().cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            '''INSERT INTO results_calendar (symbol, company, isin, declaration_date, quarter, updated_at, "user")
               VALUES %s
               ON CONFLICT (symbol, declaration_date) DO UPDATE SET
                   company = EXCLUDED.company, isin = EXCLUDED.isin, quarter = EXCLUDED.quarter,
                   updated_at = EXCLUDED.updated_at, "user" = EXCLUDED."user"''',
            [(e["symbol"], e["company"], e.get("isin"), _dt.strptime(e["date"], "%d-%b-%Y").date(),
              e.get("quarter"), _now(), user)
             for e in entries],
        )


def get_pending_results_calendar_news(from_date, to_date):
    """Symbols whose declaration_date falls in [from_date, to_date] and for
    whom genuine matching news hasn't been FOUND yet (news_fetched_at IS
    NULL) -- not "queried yet"; see mark_results_news_found's docstring for
    why that distinction matters. A company with no coverage yet keeps
    coming back here every scan cycle, for as long as it's within the
    window. Takes explicit dates from the caller (db.now()-derived), not
    CURRENT_DATE -- same reasoning as bought_today()/count_trades_today()
    above: comparing a mock-clock value on one side against Postgres's real
    CURRENT_DATE on the other would silently break during tests."""
    with _dict_cursor() as cur:
        cur.execute(
            """SELECT * FROM results_calendar
               WHERE declaration_date BETWEEN %s AND %s AND news_fetched_at IS NULL
               ORDER BY declaration_date""",
            (from_date, to_date),
        )
        return [dict(r) for r in cur.fetchall()]


def get_results_calendar_since(from_date, to_date):
    """Every company with an NSE-scheduled/declared board meeting for
    results anywhere in [from_date, to_date] -- same "declared or not,
    doesn't care about news_fetched_at" semantics as
    get_results_calendar_for_date, just windowed instead of a single exact
    date. Backs trade_bot's --tick tick-capture universe (see
    _ResultsTodayWatch.refresh): a market's reaction to results routinely
    plays out over more than just the declaration day itself, so capture
    keeps recording a company's ticks for a few days after, not only on
    the day it declared."""
    with _dict_cursor() as cur:
        cur.execute(
            "SELECT * FROM results_calendar WHERE declaration_date BETWEEN %s AND %s",
            (from_date, to_date),
        )
        return [dict(r) for r in cur.fetchall()]


def get_results_calendar_for_date(declaration_date):
    """Every company with an NSE-scheduled board meeting for results ON this
    exact date -- unlike get_pending_results_calendar_news (a multi-day
    window, filtered to news_fetched_at IS NULL, for the news-query path),
    this is trade_bot's results-today price-watch signal: it doesn't care
    about news_fetched_at at all (that column tracks a completely different
    concern, whether coverage of an already-declared result was found), and
    it always returns every symbol scheduled for exactly this date, declared
    or not -- the watch is keyed on the market's live reaction, not on
    whether an article has shown up yet."""
    with _dict_cursor() as cur:
        cur.execute("SELECT * FROM results_calendar WHERE declaration_date = %s", (declaration_date,))
        return [dict(r) for r in cur.fetchall()]


def mark_results_news_found(symbol, declaration_date, earliest_news_at, news_item_id):
    """Call only once genuine matching news has actually been found for this
    company's declaration -- not merely because a query was attempted.
    Marking "done" on an empty attempt would permanently stop querying a
    company whose results haven't been covered YET (declared post-market,
    e.g., or just slow to get picked up) for the rest of its 2-day window,
    missing the real coverage that shows up later. earliest_news_at is the
    earliest matching article's own pub_date, not now() -- when the news
    genuinely first broke, not when this scan happened to notice it.
    news_item_id is that article's news_items.id -- the audit trail back to
    the actual headline, same spirit as positions.source_id. Required, not
    optional: a "found" event only ever happens because of a real matched
    article, which by the time this is called already has a row in
    news_items (trade_bot.py's caller resolves it via the item's own
    _news_item_id, falling back to get_news_item_id_by_title for an
    already-seen article -- either way, always a real id)."""
    with get_connection().cursor() as cur:
        cur.execute(
            """UPDATE results_calendar SET news_fetched_at = now(), earliest_news_at = %s, earliest_news_item_id = %s
               WHERE symbol = %s AND declaration_date = %s""",
            (earliest_news_at, news_item_id, symbol, declaration_date),
        )


# ---- analysis_backtest (see analysis/backtest.py) ----

def news_items_for_backtest(calendar_date):
    """One row per news_item first seen on this calendar date that resolved
    to at least one real NSE equity (JOIN companies, same series filter as
    instrument_master.py -- excludes an AMC/index a headline happened to
    mention but Upstox has no candle data for). `companies` is the item's
    ALREADY-GROUNDED, already-audited symbol/name matches from
    news_item_companies -- analysis/backtest.py reuses this grounding as-is
    rather than re-deriving it (a fresh LLM extraction pass could
    reintroduce a bad match this project's own manual audit already fixed),
    but re-classifies event_type/profit_pct/revenue_pct/order_is_material
    fresh via classifier.enrich() rather than trusting the persisted
    event_type column, since that's the same method trading/trade_bot.py's
    real scan itself uses."""
    with _dict_cursor() as cur:
        cur.execute(
            """
            SELECT n.id, n.title, n.link, n.source, n.first_seen_at, n.pub_date, n.event_type,
                   json_agg(DISTINCT jsonb_build_object(
                       'symbol', nc.symbol, 'name', nc.name, 'cap_tier', c.cap_tier
                   )) AS companies
            FROM news_items n
            JOIN news_item_companies nc ON nc.news_item_id = n.id
            JOIN companies c ON c.symbol = nc.symbol AND c.series IN ('EQ', 'BE', 'BZ', 'SM', 'ST')
            WHERE n.first_seen_at >= %(date)s::date AND n.first_seen_at < %(date)s::date + 1
            GROUP BY n.id
            ORDER BY n.first_seen_at
            """,
            {"date": calendar_date},
        )
        return [dict(r) for r in cur.fetchall()]


def upsert_analysis_backtest(calendar_date, rows):
    """rows: list of dicts with symbol, entry_mode, company, cap_tier,
    headline, link, news_item_id, entry_time, entry_price, exit_time,
    exit_price, exit_reason, pnl_pct (calendar_date is passed separately,
    not per-row -- see below for why). This is a recomputed view, not an
    append log: re-running the exercise for a date must fully REPLACE that
    date's rows, not just upsert whatever symbols happen to qualify this
    time. A plain per-row ON CONFLICT upsert can't do that on its own -- a
    symbol that qualified on a looser filter (or a since-fixed bug) in an
    earlier run but no longer qualifies now would silently stay behind
    forever, since it just never appears in `rows` to be overwritten
    (verified live: this happened for real once qualification logic
    changed mid-session, leaving ~80 stale non-qualifying symbols sitting
    alongside the correct, much smaller set). DELETE-then-INSERT inside one
    transaction avoids that -- takes calendar_date explicitly (rather than
    trusting every row to agree on it) so a date with zero qualifying rows
    still correctly wipes whatever was there before."""
    with _transaction() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM analysis_backtest WHERE calendar_date = %s", (calendar_date,))
            for r in rows:
                cur.execute(
                    """INSERT INTO analysis_backtest
                           (calendar_date, symbol, entry_mode, company, cap_tier, headline, link,
                            news_item_id, confidence, entry_time, entry_price, exit_time, exit_price, exit_reason, pnl_pct, updated_at)
                       VALUES (%(calendar_date)s, %(symbol)s, %(entry_mode)s, %(company)s, %(cap_tier)s,
                               %(headline)s, %(link)s, %(news_item_id)s, %(confidence)s, %(entry_time)s, %(entry_price)s,
                               %(exit_time)s, %(exit_price)s, %(exit_reason)s, %(pnl_pct)s, now())""",
                    {**r, "calendar_date": calendar_date},
                )


def get_analysis_backtest(calendar_date):
    with _dict_cursor() as cur:
        cur.execute(
            """SELECT * FROM analysis_backtest WHERE calendar_date = %s
               ORDER BY entry_mode, entry_time""",
            (calendar_date,),
        )
        return [dict(r) for r in cur.fetchall()]


# ---- pending_candidates ----

def add_pending(candidate):
    """symbol is UNIQUE, so a previously-resolved candidate (confirmed/
    rejected -- see resolve_pending) can be re-queued by a fresh signal on a
    later day; a still-unresolved one is left untouched (matches the old
    ON CONFLICT DO NOTHING behavior)."""
    with get_connection().cursor() as cur:
        cur.execute(
            """INSERT INTO pending_candidates (source, symbol, company, confidence, detail, summary, link, "user")
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (symbol) DO UPDATE SET
                   source = EXCLUDED.source, company = EXCLUDED.company, confidence = EXCLUDED.confidence,
                   detail = EXCLUDED.detail, summary = EXCLUDED.summary, link = EXCLUDED.link,
                   created_at = now(), resolved_at = NULL, resolved_action = NULL, "user" = EXCLUDED."user"
               WHERE pending_candidates.resolved_at IS NOT NULL""",
            (candidate["source"], candidate["symbol"], candidate["company"], candidate["confidence"],
             candidate.get("detail"), candidate.get("summary"), candidate.get("link"), _app_user()),
        )


def is_pending(symbol):
    with get_connection().cursor() as cur:
        cur.execute("SELECT 1 FROM pending_candidates WHERE symbol = %s AND resolved_at IS NULL", (symbol,))
        return cur.fetchone() is not None


def get_pending():
    with _dict_cursor() as cur:
        cur.execute("SELECT * FROM pending_candidates WHERE resolved_at IS NULL ORDER BY created_at")
        return [dict(r) for r in cur.fetchall()]


def resolve_pending(symbol, action):
    """Mark a pending candidate resolved instead of deleting it, so
    pending_candidates keeps a permanent record of what was decided and when
    -- previously the row just vanished on confirm, losing that history.
    action: e.g. 'confirmed', 'rejected'."""
    with get_connection().cursor() as cur:
        cur.execute(
            "UPDATE pending_candidates SET resolved_at = now(), resolved_action = %s WHERE symbol = %s",
            (action, symbol),
        )


def count_pending():
    with get_connection().cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM pending_candidates WHERE resolved_at IS NULL")
        return cur.fetchone()[0]


# ---- scan_runs (pipeline health, read by stocki/) ----

def start_run(command):
    """command: 'scan' or 'monitor'. Call at the top of cmd_scan/cmd_monitor;
    pair with finish_run in a finally so a crash still leaves a row --
    finished_at IS NULL with no error is itself the "still running or died
    without saying why" signal a health dashboard needs."""
    with get_connection().cursor() as cur:
        cur.execute(
            """INSERT INTO scan_runs (command, started_at, "user")
               VALUES (%s, %s, %s) RETURNING id""",
            (command, now(), _app_user()),
        )
        return cur.fetchone()[0]


def finish_run(run_id, signals_seen=None, positions_opened=None, queued=None,
               skipped_quota=None, error=None):
    with get_connection().cursor() as cur:
        cur.execute(
            """UPDATE scan_runs SET finished_at=%s, signals_seen=%s, positions_opened=%s,
               queued=%s, skipped_quota=%s, error=%s WHERE id=%s""",
            (now(), signals_seen, positions_opened, queued, skipped_quota, error, run_id),
        )


def heartbeat_run(run_id):
    """Call once per cycle from a long-running scan/monitor loop (--watch,
    --tick) so a run that's designed to stay alive for days doesn't look
    indistinguishable from a dead one -- see last_heartbeat_at's schema
    comment. Deliberately does NOT touch finished_at/error; a heartbeat means
    "still alive", not "done"."""
    with get_connection().cursor() as cur:
        cur.execute("UPDATE scan_runs SET last_heartbeat_at=%s WHERE id=%s", (now(), run_id))


# ---- system_status (cross-cutting health signals, e.g. Upstox token) ----

def record_system_status(key, ok, detail=None):
    with get_connection().cursor() as cur:
        cur.execute(
            """INSERT INTO system_status (key, ok, detail, checked_at) VALUES (%s, %s, %s, %s)
               ON CONFLICT (key) DO UPDATE SET ok=EXCLUDED.ok, detail=EXCLUDED.detail,
                   checked_at=EXCLUDED.checked_at""",
            (key, ok, detail, now()),
        )


def get_system_status():
    with _dict_cursor() as cur:
        cur.execute("SELECT * FROM system_status")
        return [dict(r) for r in cur.fetchall()]


# ---- mock_price_history (test fixture data only, see mock_servers/) ----

def upsert_price_history(symbol, bars):
    """bars: list of (ts, open, high, low, close, volume) tuples."""
    if not bars:
        return
    with get_connection().cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """INSERT INTO mock_price_history (symbol, ts, open, high, low, close, volume)
               VALUES %s
               ON CONFLICT (symbol, ts) DO UPDATE SET
                   open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
                   close = EXCLUDED.close, volume = EXCLUDED.volume""",
            [(symbol, ts, o, h, l, c, v) for ts, o, h, l, c, v in bars],
        )


def get_price_history(symbol):
    """Real 1-minute bars for `symbol`, oldest first."""
    with _dict_cursor() as cur:
        cur.execute("SELECT * FROM mock_price_history WHERE symbol = %s ORDER BY ts", (symbol,))
        return [dict(r) for r in cur.fetchall()]


def get_price_history_symbols():
    with get_connection().cursor() as cur:
        cur.execute("SELECT DISTINCT symbol FROM mock_price_history")
        return {r[0] for r in cur.fetchall()}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["init", "stats"])
    args = parser.parse_args()

    if args.command == "init":
        init_schema()
        print(f"Schema ready in database '{DB_NAME}' on {DB_HOST}.")
    elif args.command == "stats":
        with get_connection().cursor() as cur:
            for table in ("news_items", "news_item_companies", "nse_filings", "positions", "pending_candidates", "companies", "scan_runs", "results_calendar"):
                cur.execute(f"SELECT COUNT(*) FROM {table}")
                print(f"{table:20s} {cur.fetchone()[0]}")
