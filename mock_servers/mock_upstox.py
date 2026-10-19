#!/usr/bin/env python3
"""
Local stand-in for the Upstox REST API, backed by fixtures/upstox_fixture.json
(see generate_fixtures.py). Serves exactly the three endpoints
broker/upstox_client.py calls, with the same URL shapes and response JSON
shapes as the real API (verified against upstox_client.py's own code, not
guessed):

    GET /v2/market-quote/ltp?instrument_key=...
    GET /v2/historical-candle/{instrument_key}/day/{to_date}/{from_date}
    GET /v3/historical-candle/intraday/{instrument_key}/minutes/1

Point upstox_client.py at this with:
    UPSTOX_API_BASE=http://localhost:8801

No auth is checked -- any Bearer token is accepted (upstox_client.py's
load_token() still needs a same-day .upstox_token.json to exist locally,
this server doesn't care what's in it).

LTP replays REAL recorded minute-by-minute prices where available (see
generate_minute_fixture.py / mock_price_history table -- real 1-min bars
pulled from Yahoo Finance for these same symbols): the first LTP call after
a symbol enters buy_window starts the replay near the real entry price, and
each subsequent call (as `monitor` polls) advances exactly one real recorded
minute further into that actual trading day -- so watching a position
resolve during a test run means watching what genuinely happened to that
stock, not a fabricated drift. Symbols with no real minute data fall back to
the old synthetic "+2% per call" drift so nothing breaks.

Usage:
    ./.venv/bin/python mock_servers/mock_upstox.py [--port 8801]
"""
import argparse
import json
import os
import random
import re
import sys
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs, unquote

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
import db  # noqa: E402 -- for the ANY-real-symbol fallback below and real minute bars

FIXTURE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "upstox_fixture.json")

LTP_TICK_STEP_PCT = 2.0  # synthetic-fallback-only: each call drifts price up this much more

_HIST_RE = re.compile(r"^/v2/historical-candle/([^/]+)/day/([\d-]+)/([\d-]+)$")
_INTRADAY_RE = re.compile(r"^/v3/historical-candle/intraday/([^/]+)/minutes/1$")


def _load_fixture():
    with open(FIXTURE_PATH) as f:
        by_symbol = json.load(f)
    by_key = {rec["instrument_key"]: rec for rec in by_symbol.values()}
    return by_symbol, by_key


def _load_real_minute_bars(by_symbol):
    """instrument_key -> real 1-min bars (oldest first), for whichever
    symbols generate_minute_fixture.py actually found data for."""
    bars_by_key = {}
    for symbol, rec in by_symbol.items():
        rows = db.get_price_history(symbol)
        if rows:
            bars_by_key[rec["instrument_key"]] = rows
    return bars_by_key


def _find_start_index(bars, prev_close, target_gain_pct):
    """Index of the real bar whose close is nearest prev_close*(1+target_gain_pct/100)
    -- anchors the real-minute replay to start consistent with the entry gain
    the (separate, already-validated) static buy-time fixture established."""
    if target_gain_pct is None:
        target_gain_pct = 0.0
    target_price = prev_close * (1 + target_gain_pct / 100)
    return min(range(len(bars)), key=lambda i: abs(float(bars[i]["close"]) - target_price))


_BUCKET_GAINS = [1.0, 1.2, -0.5, 4.0, 9.0, None]  # spread across generate_fixtures.py's _classify_bucket bands


def _synthesize(instrument_key, all_companies_by_key):
    """A real headline (especially from the live/real news fetch) can name
    any of ~2945 real NSE companies, not just our curated 100 -- returning a
    hard 404 for everything else crashes the whole scan on an unhandled
    urllib error (get_intraday_high doesn't wrap network errors in
    RuntimeError). Any REAL instrument_key gets a synthetic-but-plausible
    scenario instead, generated once and cached, so the pipeline can run to
    completion against realistic live-fetched headlines without needing
    real price data for every company that might ever be mentioned."""
    if instrument_key not in all_companies_by_key:
        return None  # genuinely not a real instrument_key -- a real 404 is correct
    rng = random.Random(hash(instrument_key) & 0xFFFFFFFF)
    prev_close = round(rng.uniform(50, 3000), 2)
    gain = rng.choice(_BUCKET_GAINS)
    today = datetime.now().date()
    daily_closes = [[str(today - timedelta(days=d)), round(prev_close * (1 + rng.uniform(-0.01, 0.01) * d), 2)]
                     for d in range(35, 0, -1)]
    if gain is None:
        candles = []
    else:
        day_high = prev_close * (1 + gain / 100)
        candles = [[
            (datetime.now() - timedelta(minutes=5 - i)).strftime("%Y-%m-%dT%H:%M:%S+05:30"),
            day_high * 0.995, day_high * (0.9 + 0.1 * (i + 1) / 5), day_high * 0.99, day_high * 0.998, 50000, 0,
        ] for i in range(5)]
    return {
        "instrument_key": instrument_key, "name": None, "bucket": "synthesized_fallback",
        "prev_close": prev_close, "intraday_candles": candles, "daily_closes": daily_closes,
    }


class Handler(BaseHTTPRequestHandler):
    by_symbol, by_key = _load_fixture()
    all_companies_by_key = {rec["instrument_key"]: rec for rec in db.get_all_companies().values()}
    real_bars_by_key = _load_real_minute_bars(by_symbol)
    tick_counts = {}
    replay_index = {}  # instrument_key -> current index into real_bars_by_key, once a replay has started
    lock = threading.Lock()

    def log_message(self, fmt, *a):
        pass  # keep stdout quiet -- errors still print via log_error

    def _json(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status, message):
        self._json({"status": "error", "errors": [{"message": message}]}, status)

    def _lookup(self, instrument_key):
        rec = self.by_key.get(instrument_key)
        if rec is not None:
            return rec
        with self.lock:
            rec = self.by_key.get(instrument_key)  # re-check inside the lock
            if rec is None:
                rec = _synthesize(instrument_key, self.all_companies_by_key)
                if rec is not None:
                    self.by_key[instrument_key] = rec  # cache -- consistent across calls this run
        return rec

    def do_GET(self):
        path = unquote(urlsplit(self.path).path)
        query = parse_qs(urlsplit(self.path).query)

        if path == "/v2/market-quote/ltp":
            return self._handle_ltp(query)
        m = _HIST_RE.match(path)
        if m:
            return self._handle_historical(*m.groups())
        m = _INTRADAY_RE.match(path)
        if m:
            return self._handle_intraday(m.group(1))
        if path == "/mock/replay-time":
            return self._handle_replay_time(query)
        self._error(404, f"No mock route for {path}")

    def _handle_replay_time(self, query):
        """NOT a real Upstox endpoint -- exposes which real historical minute
        the LTP replay is currently on for this symbol, so trade_bot.py can
        stamp entry_time/exit_time with the actual simulated market time
        instead of real wall-clock time (see upstox_client.get_mock_replay_time).

        Answers even before any LTP call has happened for this symbol yet --
        falls back to bars[0] (the start of this symbol's simulated day),
        not null. This matters for same-day-quota checks specifically:
        db.count_trades_today(symbol) is called BEFORE _open_position() ever
        queries a fresh candidate's price (the quota decides whether to
        query it at all), so no replay position exists yet at that point --
        answering null there would silently fall back to real wall-clock
        "today" for the quota check even though this symbol's eventual
        entry_time will land on the simulated day, making the quota
        undercount how many positions already opened today. Only truly
        unknown symbols (no real replay data at all -- synthesized-fallback
        ones) get ts=null."""
        instrument_key = (query.get("instrument_key") or [None])[0]
        bars = self.real_bars_by_key.get(instrument_key)
        with self.lock:
            idx = self.replay_index.get(instrument_key)
        if bars:
            ts = bars[idx if idx is not None else 0]["ts"].isoformat()
        else:
            ts = None
        self._json({"status": "success", "ts": ts})

    def _handle_ltp(self, query):
        instrument_key = (query.get("instrument_key") or [None])[0]
        rec = self._lookup(instrument_key)
        if rec is None:
            return self._error(404, f"Unknown instrument_key {instrument_key}")

        bars = self.real_bars_by_key.get(instrument_key)
        if bars:
            price = self._real_replay_price(instrument_key, rec, bars)
        else:
            price = self._synthetic_drift_price(instrument_key, rec)
        self._json({"status": "success", "data": {instrument_key: {"last_price": price}}})

    def _real_replay_price(self, instrument_key, rec, bars):
        """First call: nearest real bar to the entry gain already established
        by the static buy-time fixture. Every call after: exactly one real
        recorded minute further into that actual trading day -- so a
        `monitor` loop watches the real intraday path play out, not a
        fabricated one."""
        with self.lock:
            idx = self.replay_index.get(instrument_key)
            if idx is None:
                idx = _find_start_index(bars, rec["prev_close"], self._entry_gain_pct(rec))
            else:
                idx = min(idx + 1, len(bars) - 1)
            self.replay_index[instrument_key] = idx
        return round(float(bars[idx]["close"]), 2)

    def _synthetic_drift_price(self, instrument_key, rec):
        """Fallback for symbols with no real minute data (outside the 100-
        symbol fixture, or a yfinance gap) -- same synthetic "+2% per call"
        drift as before, just to keep the pipeline from stalling on these."""
        with self.lock:
            tick = self.tick_counts.get(instrument_key, 0)
            self.tick_counts[instrument_key] = tick + 1
        prev_close = rec["prev_close"]
        gain_pct = self._entry_gain_pct(rec) + tick * LTP_TICK_STEP_PCT
        return round(prev_close * (1 + gain_pct / 100), 2)

    def _entry_gain_pct(self, rec):
        """Reproduce the day-high gain -- what get_intraday_gain_pct() already
        computed from intraday_candles -- so the FIRST LTP call (used as the
        actual entry price by _open_position) lands consistent with the
        candles that justified opening the position in the first place."""
        candles = rec["intraday_candles"]
        if not candles:
            return 0.0
        day_high = max(c[2] for c in candles)
        return (day_high - rec["prev_close"]) / rec["prev_close"] * 100

    def _handle_historical(self, instrument_key, to_date, from_date):
        rec = self._lookup(instrument_key)
        if rec is None:
            return self._error(404, f"Unknown instrument_key {instrument_key}")
        # Real endpoint returns every day in [from_date, to_date], newest
        # first; callers here (get_historical_close) only ever read
        # candles[0][4], so returning just the closest date <= to_date as a
        # single-element list is behaviorally equivalent.
        eligible = [(d, c) for d, c in rec["daily_closes"] if d <= to_date]
        if not eligible:
            return self._json({"status": "success", "data": {"candles": []}})
        date, close = max(eligible, key=lambda dc: dc[0])
        candle = [f"{date}T00:00:00+05:30", close, close, close, close, 100000, 0]
        self._json({"status": "success", "data": {"candles": [candle]}})

    def _handle_intraday(self, instrument_key):
        rec = self._lookup(instrument_key)
        if rec is None:
            return self._error(404, f"Unknown instrument_key {instrument_key}")
        self._json({"status": "success", "data": {"candles": rec["intraday_candles"]}})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8801)
    args = parser.parse_args()
    print(f"Mock Upstox serving {len(Handler.by_symbol)} symbols on http://localhost:{args.port} "
          f"({len(Handler.real_bars_by_key)} with real per-minute replay data)")
    ThreadingHTTPServer(("localhost", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
