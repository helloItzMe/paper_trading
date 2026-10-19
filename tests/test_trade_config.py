"""
Coverage for the config-table-backed knobs (max_trades_per_day,
capital_per_trade, max_capital_allowed) and the atomic quota/capital/
already-bought gating in db.try_open_position() -- added alongside that
locking mechanism itself, since nothing here was previously covered by any
automated test (see CLAUDE.md's "Known untested / open items").

Runs against a dedicated `investment_bot_test` database (created here if
missing via `createdb`, never the real `investment_bot`) -- these tests
insert real `positions` rows and mutate `config`, and doing that against the
live database would corrupt real quota/capital tracking (this nearly bit us
during manual testing: leftover manually-inserted positions left real open
capital sitting exactly at the cap).

Run: ./.venv/bin/python -m pytest tests/test_trade_config.py -v
"""
import argparse
import re
from datetime import datetime, timedelta, timezone

import psycopg2
import pytest

import db
import trade_bot

TEST_DB_NAME = "investment_bot_test"


@pytest.fixture(scope="module", autouse=True)
def isolated_test_db():
    """Point db.DB_NAME/db._conn at a throwaway database for every test in
    this file, then restore whatever they were before -- so a test_classifier.py
    (or anything else) run in the same pytest session before/after this file
    is unaffected. createdb is idempotent here: ignore "already exists"."""
    import subprocess
    subprocess.run(["createdb", TEST_DB_NAME], stderr=subprocess.DEVNULL)

    original_name, original_conn = db.DB_NAME, db._conn
    db.DB_NAME = TEST_DB_NAME
    db._conn = None
    db.init_schema()
    yield
    if db._conn is not None and not db._conn.closed:
        db._conn.close()
    db.DB_NAME = original_name
    db._conn = original_conn


@pytest.fixture(autouse=True)
def clean_state():
    """Reset to a known-fresh state before every test -- truncate positions
    (quota/capital counts must start at zero each time), reset config
    back to the schema's seed values (one test's `config set` shouldn't leak
    into the next), and clear results_calendar (a row seeded as "tomorrow"
    in one run can silently become "today" in a later run once a real
    calendar day rolls over -- verified live: this leaked a stale RESCAL2
    row into test_get_results_calendar_for_date_exact_day_only)."""
    with db.get_connection().cursor() as cur:
        cur.execute("TRUNCATE positions RESTART IDENTITY")
        cur.execute("DELETE FROM config")
        cur.execute("TRUNCATE results_calendar")
    db.init_schema()  # reseeds config to max_trades_per_day=3, capital_per_trade=10000, max_capital_allowed=30000
    yield


def _position_kwargs(symbol="TESTSYM", capital=10000.0, entry_time=None, **overrides):
    kwargs = dict(
        symbol=symbol, company="Test Co", source="news", source_id=None, reason="test signal",
        confidence="auto", entry_price=100.0, entry_time=entry_time or datetime.now(timezone.utc),
        target_min_pct=5.0, target_max_pct=5.0, stop_loss_pct=None, capital=capital,
    )
    kwargs.update(overrides)
    return kwargs


# ---- db.py: config table ----

def test_set_get_config_roundtrip():
    db.set_config("max_trades_per_day", 7)
    assert db.get_config("max_trades_per_day") == "7"
    assert db.get_all_config()["max_trades_per_day"] == "7"


def test_get_config_missing_key_returns_default():
    assert db.get_config("no_such_key", default="fallback") == "fallback"
    assert db.get_config("no_such_key") is None


def test_init_schema_seeds_expected_defaults():
    assert db.get_all_config() == {
        "max_trades_per_day": "3",
        "capital_per_trade": "10000",
        "max_capital_allowed": "30000",
    }


def test_get_config_recovers_after_transient_connection_drop(monkeypatch):
    """A dead cached connection object should be dropped and reconnected,
    not treated as a permanent failure."""
    class DeadConn:
        autocommit = True
        closed = False
        def cursor(self):
            raise psycopg2.OperationalError("simulated drop")
    monkeypatch.setattr(db, "_conn", DeadConn())
    monkeypatch.setattr(db, "_CONFIG_CONNECT_RETRY_SECONDS", 0)
    assert db.get_config("max_trades_per_day") == "3"


def test_get_config_raises_after_persistent_connection_failure(monkeypatch):
    """A connection that never comes back must raise, never silently return
    `default` -- that would be indistinguishable from "key not set"."""
    monkeypatch.setattr(db, "_conn", None)
    monkeypatch.setattr(db, "_CONFIG_CONNECT_RETRY_SECONDS", 0)

    def fail_connect(*args, **kwargs):
        raise psycopg2.OperationalError("simulated: connection refused")
    monkeypatch.setattr(psycopg2, "connect", fail_connect)

    with pytest.raises(RuntimeError, match="Could not reach Postgres"):
        db.get_config("max_trades_per_day", default=999)


# ---- trade_bot.py: _load_config_defaults ----

def test_load_config_defaults_cli_flag_overrides_table():
    args = argparse.Namespace(max_trades_per_day=99, capital_per_trade=None, max_capital_allowed=None)
    trade_bot._load_config_defaults(args)
    assert args.max_trades_per_day == 99          # CLI value untouched
    assert args.capital_per_trade == 10000.0       # from config table
    assert args.max_capital_allowed == 30000.0     # from config table


def test_load_config_defaults_uses_test_fallback_when_key_missing():
    """Running under pytest (PYTEST_CURRENT_TEST is set) -- a missing key
    should resolve from _TEST_ONLY_CONFIG_FALLBACK, not raise."""
    with db.get_connection().cursor() as cur:
        cur.execute("DELETE FROM config WHERE key = 'capital_per_trade'")
    args = argparse.Namespace(max_trades_per_day=None, capital_per_trade=None, max_capital_allowed=None)
    trade_bot._load_config_defaults(args)
    assert args.capital_per_trade == trade_bot._TEST_ONLY_CONFIG_FALLBACK["capital_per_trade"]


def test_load_config_defaults_raises_when_key_missing_and_not_testing(monkeypatch):
    """Simulate a live (non-pytest) run by removing the env var pytest sets
    for the duration of every test -- a missing key must then raise instead
    of silently falling back."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    with db.get_connection().cursor() as cur:
        cur.execute("DELETE FROM config WHERE key = 'max_trades_per_day'")
    args = argparse.Namespace(max_trades_per_day=None, capital_per_trade=None, max_capital_allowed=None)
    with pytest.raises(RuntimeError, match="max_trades_per_day"):
        trade_bot._load_config_defaults(args)


# ---- db.py: try_open_position atomic gating ----

def test_try_open_position_succeeds_within_all_limits():
    position_id, lost_race = db.try_open_position(
        **_position_kwargs(symbol="OPEN1"), max_trades_per_day=3, max_capital_allowed=30000,
    )
    assert lost_race is None
    assert position_id is not None
    open_positions = db.get_open_positions()
    assert len(open_positions) == 1
    assert open_positions[0]["symbol"] == "OPEN1"


def test_try_open_position_blocks_same_symbol_same_day():
    kwargs = _position_kwargs(symbol="DUPSYM")
    first_id, first_reason = db.try_open_position(**kwargs, max_trades_per_day=3, max_capital_allowed=30000)
    assert first_reason is None and first_id is not None

    second_id, second_reason = db.try_open_position(**kwargs, max_trades_per_day=3, max_capital_allowed=30000)
    assert second_id is None
    assert second_reason == "already_bought"


def test_try_open_position_enforces_daily_quota():
    for i in range(2):
        _, reason = db.try_open_position(
            **_position_kwargs(symbol=f"QSYM{i}"), max_trades_per_day=2, max_capital_allowed=1_000_000,
        )
        assert reason is None

    position_id, reason = db.try_open_position(
        **_position_kwargs(symbol="QSYM_OVER"), max_trades_per_day=2, max_capital_allowed=1_000_000,
    )
    assert position_id is None
    assert reason == "quota"


def test_try_open_position_enforces_max_capital_allowed():
    _, reason = db.try_open_position(
        **_position_kwargs(symbol="CAP1", capital=20000.0), max_trades_per_day=10, max_capital_allowed=25000.0,
    )
    assert reason is None

    position_id, reason = db.try_open_position(
        **_position_kwargs(symbol="CAP2", capital=10000.0), max_trades_per_day=10, max_capital_allowed=25000.0,
    )
    assert position_id is None
    assert reason == "capital"


def test_get_open_capital_counts_open_positions_only():
    assert db.get_open_capital() == 0

    position_id, _ = db.try_open_position(
        **_position_kwargs(symbol="SUMSYM", capital=12345.0), max_trades_per_day=10, max_capital_allowed=1_000_000,
    )
    assert db.get_open_capital() == 12345.0

    db.close_position(position_id, exit_price=105.0, exit_time=datetime.now(timezone.utc),
                       exit_reason="target", pnl_pct=5.0, pnl_amount=500.0)
    assert db.get_open_capital() == 0


# ---- trade_bot._check_and_maybe_close (extracted from cmd_monitor so both
# the polling loop and monitor --tick's on_tick callback share identical
# exit logic -- see trading/trade_bot.py) ----

def test_check_and_maybe_close_hits_target():
    db.try_open_position(**_position_kwargs(symbol="TICK1"), max_trades_per_day=3, max_capital_allowed=30000)
    pos = db.get_open_positions()[0]
    assert trade_bot._check_and_maybe_close(pos, price=106.0) is True  # entry 100, target +5%
    assert db.get_open_positions() == []


def test_check_and_maybe_close_leaves_open_when_no_trigger():
    db.try_open_position(**_position_kwargs(symbol="TICK2"), max_trades_per_day=3, max_capital_allowed=30000)
    pos = db.get_open_positions()[0]
    assert trade_bot._check_and_maybe_close(pos, price=101.0) is False  # +1%, no target/stop hit
    assert len(db.get_open_positions()) == 1


def test_check_and_maybe_close_hits_stop_loss():
    db.try_open_position(
        **_position_kwargs(symbol="TICK3", stop_loss_pct=-5.0), max_trades_per_day=3, max_capital_allowed=30000)
    pos = db.get_open_positions()[0]
    assert trade_bot._check_and_maybe_close(pos, price=94.0) is True  # -6%, stop -5%
    assert db.get_open_positions() == []


# ---- market-closed-queued positions (status='queued', reusing `positions`
# -- see db.py schema notes on positions.limit_price for why no new table) ----

def test_queue_offhours_position_is_invisible_to_open_queries():
    """A queued row must not be counted as open capital or show up in
    get_open_positions/get_open_symbols -- those are exactly the existing
    queries (shared with stocki) this design deliberately keeps untouched."""
    db.queue_offhours_position(
        "WKND1", "Weekend Co", "news", None, "some headline", "auto",
        limit_price=105.0, capital=10000.0, queued_at=datetime.now(timezone.utc),
    )
    assert db.is_position_queued("WKND1") is True
    assert db.get_open_capital() == 0
    assert db.get_open_positions() == []
    assert "WKND1" not in db.get_open_symbols()
    queued = db.get_queued_positions()
    assert len(queued) == 1 and queued[0]["symbol"] == "WKND1"


def test_try_fill_queued_position_opens_it():
    order_id = db.queue_offhours_position(
        "WKND2", "Weekend Co", "news", None, "some headline", "auto",
        limit_price=105.0, capital=10000.0, queued_at=datetime.now(timezone.utc),
    )
    position_id, reason = db.try_fill_queued_position(
        order_id, "WKND2", entry_price=102.0, entry_time=datetime.now(timezone.utc),
        target_min_pct=5.0, target_max_pct=5.0, stop_loss_pct=None,
        capital=10000.0, max_trades_per_day=3, max_capital_allowed=30000,
    )
    assert reason is None
    assert position_id == order_id  # same row, updated in place
    assert db.is_position_queued("WKND2") is False
    assert db.get_open_capital() == 10000.0
    open_positions = db.get_open_positions()
    assert len(open_positions) == 1
    assert open_positions[0]["entry_price"] == 102.0


def test_try_fill_queued_position_respects_quota():
    order_id = db.queue_offhours_position(
        "WKND3", "Weekend Co", "news", None, "some headline", "auto",
        limit_price=105.0, capital=10000.0, queued_at=datetime.now(timezone.utc),
    )
    now = datetime.now(timezone.utc)
    for i in range(2):
        _, reason = db.try_open_position(**_position_kwargs(symbol=f"FILLERQ{i}", entry_time=now),
                                          max_trades_per_day=2, max_capital_allowed=1_000_000)
        assert reason is None

    position_id, reason = db.try_fill_queued_position(
        order_id, "WKND3", entry_price=102.0, entry_time=now,
        target_min_pct=5.0, target_max_pct=5.0, stop_loss_pct=None,
        capital=10000.0, max_trades_per_day=2, max_capital_allowed=1_000_000,
    )
    assert position_id is None
    assert reason == "quota"
    assert db.is_position_queued("WKND3") is True  # left queued, not lost


# ---- _market_is_closed_by_schedule (no DB needed -- pure date/time math).
# 2026-07-28 is a real Tuesday, 2026-08-01 a real Saturday -- this is the
# FALLBACK path (see _market_is_closed below for the live-API-preferring
# wrapper that actually gates the live-price call sites).

def test_market_is_closed_by_schedule_on_weekend():
    saturday = datetime(2026, 8, 1, 12, 0, tzinfo=trade_bot.IST)
    assert trade_bot._market_is_closed_by_schedule(saturday) is True


def test_market_is_closed_by_schedule_on_weekday_before_open():
    early = datetime(2026, 7, 28, 8, 0, tzinfo=trade_bot.IST)
    assert trade_bot._market_is_closed_by_schedule(early) is True


def test_market_is_closed_by_schedule_on_weekday_after_close():
    late = datetime(2026, 7, 28, 16, 0, tzinfo=trade_bot.IST)
    assert trade_bot._market_is_closed_by_schedule(late) is True


def test_market_is_open_by_schedule_on_weekday_during_trading_hours():
    midday = datetime(2026, 7, 28, 12, 0, tzinfo=trade_bot.IST)
    assert trade_bot._market_is_closed_by_schedule(midday) is False


def test_market_is_open_by_schedule_at_exact_open_and_close_boundaries():
    at_open = datetime(2026, 7, 28, 9, 15, tzinfo=trade_bot.IST)
    at_close = datetime(2026, 7, 28, 15, 30, tzinfo=trade_bot.IST)
    assert trade_bot._market_is_closed_by_schedule(at_open) is False
    assert trade_bot._market_is_closed_by_schedule(at_close) is False


# ---- _market_is_closed (live upstox_client.is_market_open() API, with the
# schedule above as fallback only if that call fails) ----

def test_market_is_closed_prefers_live_api_result():
    import upstox_client
    original = upstox_client.is_market_open
    upstox_client.is_market_open = lambda exchange="NSE": True  # "NSE says open"
    try:
        assert trade_bot._market_is_closed() is False
    finally:
        upstox_client.is_market_open = original


def test_market_is_closed_live_api_says_closed():
    import upstox_client
    original = upstox_client.is_market_open
    upstox_client.is_market_open = lambda exchange="NSE": False  # "NSE says closed"
    try:
        assert trade_bot._market_is_closed() is True
    finally:
        upstox_client.is_market_open = original


def test_market_is_closed_falls_back_to_schedule_when_live_api_fails():
    """The exact case that matters: no token, network issue, or (as in this
    test) the mock/test environment simply not implementing this endpoint --
    must fall back to the schedule, never raise or silently assume either
    state."""
    import upstox_client
    original = upstox_client.is_market_open

    def raise_runtime_error(exchange="NSE"):
        raise RuntimeError("simulated: no valid token cached for today")

    upstox_client.is_market_open = raise_runtime_error
    try:
        result = trade_bot._market_is_closed()  # falls back to _market_is_closed_by_schedule(db.now())
        assert isinstance(result, bool)
    finally:
        upstox_client.is_market_open = original


# ---- _looks_like_republished_content / _is_fresh_enough's weekend-negative-age
# fix -- both added for the same real, live incident: a genuine 2024-08-02
# Dhanuka Agritech Q1 profit article ("48.5% YOY to Rs 48.90 Crore") got
# re-surfaced by a different publisher (AgroSpectrum India) with pub_date
# 2026-08-01 -- same numbers, same story, two years apart, confirmed by
# opening the real article (it read "06 Aug 2024"). Because 2026-08-01 was a
# Saturday, _is_fresh_enough's weekend-carryover rolled its pub_date forward
# to the next Monday's market open, and the scan that saw it ran BEFORE that
# open -- producing a NEGATIVE age that the old `age <= NEWS_MAX_AGE` check
# treated as trivially fresh regardless of true elapsed time. It sailed
# through to an auto-queued off-hours buy candidate before either fix
# existed. Insert/delete our own news_items rows here (title is UNIQUE, so
# distinct fixture titles avoid collisions with anything else in this
# throwaway investment_bot_test database).

def test_looks_like_republished_content_flags_the_real_duplicate():
    old_title = "Dhanuka Agritech Q1 Net Profit up by 48.5% YOY to Rs. 48.90 crore - Krishi Jagran"
    new_title = "Dhanuka Agritech's Q1 net profit up by 48.5% YOY to Rs 48.90 Crore - AgroSpectrum India"
    old_pub_date = datetime(2024, 8, 2, 12, 30, tzinfo=trade_bot.IST)
    new_pub_date = datetime(2026, 8, 1, 10, 23, 49, tzinfo=trade_bot.IST)
    with db.get_connection().cursor() as cur:
        cur.execute("DELETE FROM news_items WHERE title IN (%s, %s)", (old_title, new_title))
        cur.execute(
            """INSERT INTO news_items (title, pub_date, event_type, category, signal, "user")
               VALUES (%s, %s, 'STALE', 'other', 'neutral', 'ashish')""",
            (old_title, old_pub_date),
        )
    try:
        assert trade_bot._looks_like_republished_content("DHANUKA AGRITECH LTD", new_title, new_pub_date)
    finally:
        with db.get_connection().cursor() as cur:
            cur.execute("DELETE FROM news_items WHERE title = %s", (old_title,))


def test_looks_like_republished_content_no_false_positive_on_genuinely_different_article():
    unrelated_title = "Dhanuka Agritech Q4 PAT rises 30%, approves ₹70 cr buyback - scanx.trade"
    unrelated_pub_date = datetime(2026, 5, 22, 12, 30, tzinfo=trade_bot.IST)
    new_title = "Dhanuka Agritech's Q1 net profit up by 48.5% YOY to Rs 48.90 Crore - AgroSpectrum India"
    new_pub_date = datetime(2026, 8, 1, 10, 23, 49, tzinfo=trade_bot.IST)
    with db.get_connection().cursor() as cur:
        cur.execute("DELETE FROM news_items WHERE title IN (%s, %s)", (unrelated_title, new_title))
        cur.execute(
            """INSERT INTO news_items (title, pub_date, event_type, category, signal, "user")
               VALUES (%s, %s, 'STALE', 'other', 'neutral', 'ashish')""",
            (unrelated_title, unrelated_pub_date),
        )
    try:
        assert not trade_bot._looks_like_republished_content("DHANUKA AGRITECH LTD", new_title, new_pub_date)
    finally:
        with db.get_connection().cursor() as cur:
            cur.execute("DELETE FROM news_items WHERE title = %s", (unrelated_title,))


def test_looks_like_republished_content_ignores_recent_multi_source_coverage():
    """Multiple outlets covering the SAME real, fresh declaration within a
    day or two must NOT be flagged -- only a gap far wider than any real
    news cycle (REPUBLISH_LOOKBACK_MIN_GAP) should trigger this."""
    same_week_title = "Dhanuka Agritech Q1 FY27 Net Profit Rises 48.5% YoY to Rs 48.90 Crore - Business Standard"
    same_week_pub_date = datetime(2026, 7, 31, 9, 0, tzinfo=trade_bot.IST)
    new_title = "Dhanuka Agritech's Q1 net profit up by 48.5% YOY to Rs 48.90 Crore - AgroSpectrum India"
    new_pub_date = datetime(2026, 8, 1, 10, 23, 49, tzinfo=trade_bot.IST)
    with db.get_connection().cursor() as cur:
        cur.execute("DELETE FROM news_items WHERE title IN (%s, %s)", (same_week_title, new_title))
        cur.execute(
            """INSERT INTO news_items (title, pub_date, event_type, category, signal, "user")
               VALUES (%s, %s, 'PROFIT_INCREASE', 'earnings', 'positive', 'ashish')""",
            (same_week_title, same_week_pub_date),
        )
    try:
        assert not trade_bot._looks_like_republished_content("DHANUKA AGRITECH LTD", new_title, new_pub_date)
    finally:
        with db.get_connection().cursor() as cur:
            cur.execute("DELETE FROM news_items WHERE title = %s", (same_week_title,))


def test_is_fresh_enough_negative_age_is_stale_not_infinitely_fresh():
    """The other half of the same fix, at the _is_fresh_enough level: a
    Saturday pub_date rolled forward to Monday's market open, checked
    BEFORE that open, must be stale -- not trivially "fresh" just because
    the computed age happens to be negative. See test_weekend_news_is_stale_
    before_next_market_open in test_classifier.py for the direct version of
    this; duplicated here in spirit as a real numeric case matching the live
    incident (checked ~3.3 hours before the rolled-forward open, not a full
    day before like the other test)."""
    pub_date = datetime(2026, 8, 1, 10, 23, 49, tzinfo=trade_bot.IST)   # Saturday
    fixed_now = datetime(2026, 8, 3, 5, 54, 14, tzinfo=trade_bot.IST)   # Monday, before 09:15 open
    db.set_clock(lambda symbol=None: fixed_now)
    try:
        assert not trade_bot._is_fresh_enough({"pub_date": pub_date})
    finally:
        db.set_clock(None)


# ---- _has_genuine_results_context / _item_matches_calendar_entry -- a
# roundup PREVIEW article ("Stocks to Watch: DLF, Torrent Power, Jindal
# Stainless and 7 Other Companies Announcing Q1 Results Today") matched and
# marked results_calendar "news found" for ALL NINE companies it named, none
# of which had actually reported anything -- the article only previewed who
# was due to report that day. _RESULTS_CONTEXT_RE alone can't tell "results
# were declared, here are the figures" apart from "here's who's expected to
# declare today" -- both contain "results"/"Q1" equally.

def test_preview_roundup_does_not_match_calendar_entry():
    title = ("Stocks to Watch: DLF, Torrent Power, Jindal Stainless and 7 Other "
             "Companies Announcing Q1 Results Today - Trade Brains")
    company_normalized = trade_bot._normalize_company_name("DLF Limited")
    symbol_re = re.compile(r"\bDLF\b", re.IGNORECASE)
    assert not trade_bot._item_matches_calendar_entry({"title": title}, company_normalized, symbol_re)


def test_preview_board_meeting_notice_does_not_match_calendar_entry():
    title = "Jindal Worldwide Board to Meet Tomorrow for Quarterly Results; Shares Fall 2.23% in Previous Session - Moneycontrol.com"
    company_normalized = trade_bot._normalize_company_name("Jindal Worldwide Limited")
    symbol_re = re.compile(r"\bJINDWORLD\b", re.IGNORECASE)
    assert not trade_bot._item_matches_calendar_entry({"title": title}, company_normalized, symbol_re)


def test_genuine_reported_result_still_matches_calendar_entry():
    """The fix must not make genuine, already-reported results stop
    matching -- only preview/calendar language is excluded."""
    title = "Aarti Surfactants Q1 Results: Net Profit Rises 197% YoY To ₹9.08 Cr"
    company_normalized = trade_bot._normalize_company_name("Aarti Surfactants Limited")
    symbol_re = re.compile(r"\bAARTISURF\b", re.IGNORECASE)
    assert trade_bot._item_matches_calendar_entry({"title": title}, company_normalized, symbol_re)


# ---- _decide -- a later, better (auto) candidate for a symbol must not be
# silently dropped just because a weaker (manual) candidate for the SAME
# symbol was processed first this scan, and a genuine auto decision must
# supersede/resolve any stale pending_candidates row for that symbol rather
# than leaving it looking "still needs review" forever. Both mock
# _market_is_closed()=True + upstox_client.get_last_close so _open_position
# takes the deterministic off-hours-queue path rather than needing a live
# intraday-gain/price mock.

def _candidate(symbol, confidence, detail="test signal"):
    return {
        "source": "news", "source_id": None, "symbol": symbol, "company": "Test Co",
        "confidence": confidence, "detail": detail, "summary": "", "link": None,
    }


def test_decide_lets_later_auto_candidate_override_earlier_manual_same_symbol(monkeypatch):
    import upstox_client
    monkeypatch.setattr(trade_bot, "_market_is_closed", lambda: True)
    monkeypatch.setattr(upstox_client, "get_last_close", lambda symbol: 100.0)

    args = argparse.Namespace(max_trades_per_day=3, capital_per_trade=10000.0, max_capital_allowed=30000.0)
    state = trade_bot._ScanState(args)
    try:
        trade_bot._decide(_candidate("OVERRIDE1", "manual", "manual signal"), state)
        assert db.is_pending("OVERRIDE1")  # queued for manual review, as expected

        trade_bot._decide(_candidate("OVERRIDE1", "auto", "auto signal"), state)
        # The real bug: the old seen_symbols check would have silently
        # dropped this second candidate entirely just because a manual one
        # for the same symbol was seen first -- it must actually be
        # evaluated and queue a real off-hours order.
        assert db.is_position_queued("OVERRIDE1")
        assert state.offhours_queued == 1
    finally:
        with db.get_connection().cursor() as cur:
            cur.execute("DELETE FROM pending_candidates WHERE symbol = %s", ("OVERRIDE1",))


def test_decide_resolves_stale_pending_when_auto_candidate_succeeds(monkeypatch):
    import upstox_client
    monkeypatch.setattr(trade_bot, "_market_is_closed", lambda: True)
    monkeypatch.setattr(upstox_client, "get_last_close", lambda symbol: 100.0)

    db.add_pending(_candidate("OVERRIDE2", "manual", "old manual signal"))
    assert db.is_pending("OVERRIDE2")

    args = argparse.Namespace(max_trades_per_day=3, capital_per_trade=10000.0, max_capital_allowed=30000.0)
    state = trade_bot._ScanState(args)
    try:
        trade_bot._decide(_candidate("OVERRIDE2", "auto", "auto signal"), state)
        assert db.is_position_queued("OVERRIDE2")
        # The stale pending row from before this scan must not be left
        # looking "still needs review" once a real decision has been made.
        assert not db.is_pending("OVERRIDE2")
        with db.get_connection().cursor() as cur:
            cur.execute("SELECT resolved_action FROM pending_candidates WHERE symbol = %s", ("OVERRIDE2",))
            assert cur.fetchone()[0] == "superseded_by_auto"
    finally:
        with db.get_connection().cursor() as cur:
            cur.execute("DELETE FROM pending_candidates WHERE symbol = %s", ("OVERRIDE2",))


# ---- results-today price watch (trade_bot._ResultsTodayWatch,
# _open_results_today_position, db.get_results_calendar_for_date) -- the
# third, price-only signal source wired into `monitor --tick` ----

def _seed_calendar_entry(symbol, company, declaration_date):
    db.upsert_results_calendar([{
        "symbol": symbol, "company": company, "isin": None,
        "date": declaration_date.strftime("%d-%b-%Y"), "quarter": "Q1FY27",
    }])


def test_get_results_calendar_for_date_exact_day_only():
    today = db.now().date()
    _seed_calendar_entry("RESCAL1", "Results Watch Co", today)
    _seed_calendar_entry("RESCAL2", "Tomorrow Co", today + timedelta(days=1))

    rows = db.get_results_calendar_for_date(today)
    symbols = {r["symbol"] for r in rows}
    assert "RESCAL1" in symbols
    assert "RESCAL2" not in symbols  # different declaration_date, must not leak in


def test_results_today_watch_refresh_excludes_active_positions(monkeypatch):
    import upstox_client
    today = db.now().date()
    _seed_calendar_entry("RESCAL3", "Already Open Co", today)
    _seed_calendar_entry("RESCAL4", "Fresh Candidate Co", today)
    db.try_open_position(**_position_kwargs(symbol="RESCAL3"), max_trades_per_day=3, max_capital_allowed=30000)

    monkeypatch.setattr(upstox_client, "get_last_close", lambda symbol: 100.0)
    watch = trade_bot._ResultsTodayWatch()
    symbols = watch.refresh()

    assert "RESCAL3" not in symbols  # already has an open position -- not a candidate
    assert "RESCAL4" in symbols
    assert watch.prev_close["RESCAL4"] == 100.0


def test_results_today_watch_check_tick_fires_at_threshold(monkeypatch):
    import upstox_client
    today = db.now().date()
    _seed_calendar_entry("RESCAL5", "Threshold Co", today)
    monkeypatch.setattr(upstox_client, "get_last_close", lambda symbol: 100.0)
    monkeypatch.setattr(upstox_client, "get_recent_performance", lambda symbol: {"1d": 0, "1w": 0, "1m": 0})
    monkeypatch.setattr(trade_bot, "_market_is_closed", lambda: False)

    watch = trade_bot._ResultsTodayWatch()
    watch.refresh()

    # Below RESULTS_TODAY_GAIN_PCT (1%): must not open.
    assert watch.check_tick("RESCAL5", 100.5, capital_per_trade=10000.0,
                             max_trades_per_day=3, max_capital_allowed=30000) is False
    assert db.get_open_positions() == []

    # At/above the threshold: opens, and stops watching the symbol.
    assert watch.check_tick("RESCAL5", 101.5, capital_per_trade=10000.0,
                             max_trades_per_day=3, max_capital_allowed=30000) is True
    positions = db.get_open_positions()
    assert len(positions) == 1
    assert positions[0]["symbol"] == "RESCAL5"
    assert positions[0]["source"] == "results_today"
    assert "RESCAL5" not in watch.prev_close


def test_open_results_today_position_skips_when_already_bought_today(monkeypatch):
    monkeypatch.setattr(trade_bot, "_market_is_closed", lambda: False)
    db.try_open_position(**_position_kwargs(symbol="RESCAL6"), max_trades_per_day=3, max_capital_allowed=30000)

    result = trade_bot._open_results_today_position(
        "RESCAL6", "Dup Co", price=101.0, gain_pct=1.0,
        capital_per_trade=10000.0, max_trades_per_day=3, max_capital_allowed=30000,
    )
    assert result is None
    assert len(db.get_open_positions()) == 1  # no second position opened


def test_open_results_today_position_skips_when_market_closed(monkeypatch):
    monkeypatch.setattr(trade_bot, "_market_is_closed", lambda: True)
    result = trade_bot._open_results_today_position(
        "RESCAL7", "Closed Co", price=101.0, gain_pct=1.0,
        capital_per_trade=10000.0, max_trades_per_day=3, max_capital_allowed=30000,
    )
    assert result is None
    assert db.get_open_positions() == []


# ---- get_results_calendar_since / _ResultsTodayWatch.recent_companies --
# the wider, tick-capture-only window (see CLAUDE.md / trade_bot.py's
# RECENT_RESULTS_CAPTURE_DAYS) -- and the _unresolved permanent-blacklist
# bug that silently excluded a real symbol (EMIL) from an entire session
# after a transient token failure, well past the point the token was
# actually fixed. ----

def test_get_results_calendar_since_multi_day_window():
    today = db.now().date()
    _seed_calendar_entry("RESCAL8", "Three Days Ago Co", today - timedelta(days=3))
    _seed_calendar_entry("RESCAL9", "Today Co", today)
    _seed_calendar_entry("RESCAL10", "Too Old Co", today - timedelta(days=10))
    _seed_calendar_entry("RESCAL11", "Future Co", today + timedelta(days=1))

    rows = db.get_results_calendar_since(today - timedelta(days=4), today)
    symbols = {r["symbol"] for r in rows}
    assert "RESCAL8" in symbols
    assert "RESCAL9" in symbols
    assert "RESCAL10" not in symbols   # outside the window (too far back)
    assert "RESCAL11" not in symbols   # outside the window (in the future)


def test_results_today_watch_recent_companies_wider_than_buy_candidates(monkeypatch):
    """recent_companies (tick-capture universe) must include a symbol from
    a few days back that companies (today-only buy-trigger candidates)
    correctly excludes."""
    import instrument_master
    import upstox_client
    today = db.now().date()
    _seed_calendar_entry("RESCAL12", "Two Days Ago Co", today - timedelta(days=2))
    _seed_calendar_entry("RESCAL13", "Today Co", today)
    monkeypatch.setattr(upstox_client, "get_last_close", lambda symbol: 100.0)
    # RESCAL12 isn't in today's candidates, so recent_companies validates it
    # via a real instrument_master lookup -- fake it instead of hitting the
    # network/real instrument master for a symbol that doesn't exist there.
    monkeypatch.setattr(instrument_master, "get_instrument_key", lambda symbol: "FAKE|KEY")

    watch = trade_bot._ResultsTodayWatch()
    watch.refresh()

    assert "RESCAL12" not in watch.companies       # not today -- not a buy candidate
    assert "RESCAL13" in watch.companies
    assert "RESCAL12" in watch.recent_companies    # but still worth capturing ticks for
    assert "RESCAL13" in watch.recent_companies


def test_results_today_watch_recent_companies_includes_active_positions(monkeypatch):
    """Unlike `companies`, `recent_companies` must NOT drop a symbol just
    because it already has an open position -- that position is exactly
    what capture is meant to be recording ticks for."""
    import instrument_master
    import upstox_client
    today = db.now().date()
    _seed_calendar_entry("RESCAL14", "Already Open Co", today)
    db.try_open_position(**_position_kwargs(symbol="RESCAL14"), max_trades_per_day=3, max_capital_allowed=30000)
    monkeypatch.setattr(upstox_client, "get_last_close", lambda symbol: 100.0)
    # RESCAL14 has an active position, so it's excluded from `candidates`
    # and therefore takes recent_companies' instrument_master validation
    # path too -- same fake-key reasoning as the test above.
    monkeypatch.setattr(instrument_master, "get_instrument_key", lambda symbol: "FAKE|KEY")

    watch = trade_bot._ResultsTodayWatch()
    watch.refresh()

    assert "RESCAL14" not in watch.companies       # excluded from buy candidates
    assert "RESCAL14" in watch.recent_companies    # NOT excluded from capture


def test_results_today_watch_transient_error_is_not_permanently_blacklisted(monkeypatch):
    """The actual bug: a transient failure (expired token, network blip)
    fetching prev_close must NOT permanently exclude the symbol via
    self._unresolved -- unlike a genuinely-missing instrument_key, it
    should be retried on the next refresh() once the transient problem
    clears. Verified live: this exact scenario silently excluded a real
    results-day symbol (EMIL) for an entire session after an early-morning
    Upstox token expiry, even hours after the token was refreshed."""
    import upstox_client
    today = db.now().date()
    _seed_calendar_entry("RESCAL15", "Token Blip Co", today)

    calls = {"n": 0}

    def flaky_get_last_close(symbol):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("No valid token cached for today -- run `python3 upstox_client.py login` first.")
        return 100.0

    monkeypatch.setattr(upstox_client, "get_last_close", flaky_get_last_close)
    watch = trade_bot._ResultsTodayWatch()

    watch.refresh()  # first refresh: transient token failure
    assert "RESCAL15" not in watch.prev_close
    assert "RESCAL15" not in watch._unresolved  # must NOT be permanently blacklisted

    watch.refresh()  # second refresh: token's "fixed" now, must retry and succeed
    assert watch.prev_close.get("RESCAL15") == 100.0
    assert "RESCAL15" in watch.recent_companies  # tick capture picks it up too


def test_results_today_watch_genuinely_missing_instrument_key_is_blacklisted(monkeypatch):
    """Contrast with the transient case above: a genuinely-unresolvable
    symbol (no Upstox instrument_key at all -- e.g. a REIT/InvIT unit) IS
    meant to be cached in _unresolved, so it isn't re-attempted (and
    doesn't trigger a fresh instrument-master force-refresh) every single
    30s cycle forever."""
    import upstox_client
    today = db.now().date()
    _seed_calendar_entry("RESCAL16", "No Instrument Key Co", today)

    calls = {"n": 0}

    def always_missing(symbol):
        calls["n"] += 1
        raise RuntimeError(f"No Upstox instrument_key found for NSE symbol '{symbol}'.")

    monkeypatch.setattr(upstox_client, "get_last_close", always_missing)
    watch = trade_bot._ResultsTodayWatch()

    watch.refresh()
    assert "RESCAL16" in watch._unresolved
    assert calls["n"] == 1

    watch.refresh()  # must NOT retry a genuinely-unresolvable symbol
    assert calls["n"] == 1
