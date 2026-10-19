#!/usr/bin/env python3
"""
Upstox API client -- auth flow + live quotes. This is now trade_bot.py's only
price source (yfinance has been dropped from the project), so paper trading
has no working price feed until your Upstox account is fully active and
`login` succeeds.

Setup (once the account is live, no reactivation errors):
    1. Register an app at developer.upstox.com, note the API key/secret and
       whatever redirect URI you registered (e.g. http://localhost:5000/callback).
    2. Fill in upstox_secrets.json (project root) with those three values --
       or set env vars UPSTOX_API_KEY / UPSTOX_API_SECRET / UPSTOX_REDIRECT_URI,
       which take effect if the JSON file's fields are left blank. Never commit
       upstox_secrets.json or paste its contents anywhere -- it's in .gitignore.
    3. Run: python3 upstox_client.py login
       -- opens the login URL, starts a local server to catch the redirect,
       exchanges the code for an access token, caches it in .upstox_token.json.
    4. Access tokens expire ~3:30am the next day -- rerun `login` each trading day.

Symbol -> instrument_key resolution (Upstox's quote/candle/feed APIs need the
instrument_key, not the plain trading symbol) is handled automatically by
instrument_master.py, which downloads and caches Upstox's published instrument
list -- no manual mapping file needed.

Verify endpoint paths/params against Upstox's current API docs before relying
on this for real trades -- their API has moved from v2 to v3 before, so don't
assume the URLs below are still current without checking.
"""
import argparse
import http.server
import json
import os
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

import sys as _sys
_sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db  # noqa: E402 -- for db.now(), the central mockable clock

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # project root
TOKEN_FILE = os.path.join(BASE_DIR, ".upstox_token.json")
SECRETS_FILE = os.path.join(BASE_DIR, "upstox_secrets.json")

# Overridable so a local mock server (see mock_servers/mock_upstox.py) can
# stand in for the real API during testing -- everything downstream builds
# URLs off this base rather than hardcoding api.upstox.com directly.
UPSTOX_API_BASE = os.environ.get("UPSTOX_API_BASE", "https://api.upstox.com")

AUTH_URL = f"{UPSTOX_API_BASE}/v2/login/authorization/dialog"
TOKEN_URL = f"{UPSTOX_API_BASE}/v2/login/authorization/token"
LTP_URL = f"{UPSTOX_API_BASE}/v2/market-quote/ltp"
HISTORICAL_CANDLE_URL = f"{UPSTOX_API_BASE}/v2/historical-candle"
INTRADAY_CANDLE_URL = f"{UPSTOX_API_BASE}/v3/historical-candle/intraday"
MARKET_STATUS_URL = f"{UPSTOX_API_BASE}/v2/market/status"

# Real, documented Upstox status values (verified against their API docs,
# not guessed) -- only NORMAL_OPEN means live trading is actually happening
# right now. The others (pre-open, closing auction phases, fully closed)
# all mean "don't try to trade this instant". Kept as a set, not a single
# string, in case Upstox ever adds another genuinely-open-for-trading value.
MARKET_OPEN_STATUSES = {"NORMAL_OPEN"}

WEAKNESS_LOOKBACK_DAYS = {"1d": 1, "1w": 7, "1m": 30}


def _fetch_json(req, timeout=20):
    """The one place every real API call goes through -- every call site
    downstream (trade_bot.py) already catches `except RuntimeError` for the
    "no token"/"unresolvable symbol" cases, so normalizing genuine network
    failures (timeout, DNS, connection reset, Upstox 5xx) into RuntimeError
    here means every existing call site starts handling them too, with zero
    changes needed there. Before this, a raw urllib.error.URLError/HTTPError/
    socket.timeout from a transient network blip would slip past every
    `except RuntimeError` guard in trade_bot.py and crash the whole `scan`/
    `monitor` run (including an unattended `--watch` loop meant to run for
    hours) -- verified by tracing get_recent_performance's four unguarded
    urlopen calls straight through to _open_position's callers."""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RuntimeError(f"Upstox API returned HTTP {e.code}: {body}") from e
    except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
        raise RuntimeError(f"Network error reaching Upstox ({e})") from e
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Upstox returned unparseable response ({e})") from e


def _load_secrets():
    """upstox_secrets.json fields take priority; blank fields fall back to
    UPSTOX_API_KEY / UPSTOX_API_SECRET / UPSTOX_REDIRECT_URI env vars."""
    secrets = {}
    if os.path.exists(SECRETS_FILE):
        with open(SECRETS_FILE) as f:
            secrets = json.load(f)
    return {
        "api_key": secrets.get("api_key") or os.environ.get("UPSTOX_API_KEY"),
        "api_secret": secrets.get("api_secret") or os.environ.get("UPSTOX_API_SECRET"),
        "redirect_uri": secrets.get("redirect_uri") or os.environ.get("UPSTOX_REDIRECT_URI"),
    }


_secrets = _load_secrets()
API_KEY = _secrets["api_key"]
API_SECRET = _secrets["api_secret"]
REDIRECT_URI = _secrets["redirect_uri"]


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    code = None

    def do_GET(self):
        query = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(query)
        if "code" in params:
            _CallbackHandler.code = params["code"][0]
            body = b"Login captured, you can close this tab."
        else:
            body = b"No code in redirect -- check the URL bar and paste it manually."
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *a):
        pass  # keep stdout quiet


def build_login_url():
    if not (API_KEY and REDIRECT_URI):
        raise RuntimeError(
            f"Fill in api_key and redirect_uri in {SECRETS_FILE} "
            f"(or set UPSTOX_API_KEY / UPSTOX_REDIRECT_URI env vars) first."
        )
    params = {"client_id": API_KEY, "redirect_uri": REDIRECT_URI, "response_type": "code"}
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


def wait_for_code(port, timeout=180):
    server = http.server.HTTPServer(("localhost", port), _CallbackHandler)
    thread = threading.Thread(target=server.handle_request)
    thread.start()
    thread.join(timeout=timeout)
    server.server_close()
    return _CallbackHandler.code


def exchange_code(code):
    data = urllib.parse.urlencode({
        "code": code,
        "client_id": API_KEY,
        "client_secret": API_SECRET,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
    }).encode()
    req = urllib.request.Request(
        TOKEN_URL, data=data,
        headers={
            "accept": "application/json", "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "Mozilla/5.0",  # Cloudflare blocks Python's default urllib UA outright
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RuntimeError(f"Token exchange failed (HTTP {e.code}): {body}")
    except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
        raise RuntimeError(f"Token exchange failed (network error reaching Upstox: {e})")


def save_token(token_response):
    with open(TOKEN_FILE, "w") as f:
        json.dump({"issued": _today(), "access_token": token_response["access_token"]}, f)


def _today():
    import time
    return time.strftime("%Y-%m-%d")


def load_token():
    """Return today's cached access token, or None if missing/stale (a new day)."""
    if not os.path.exists(TOKEN_FILE):
        return None
    with open(TOKEN_FILE) as f:
        cached = json.load(f)
    if cached.get("issued") != _today():
        return None
    return cached.get("access_token")


def login(port=5000, code=None):
    """Login: open URL, get the auth code (via local capture, a provided
    --code, or manual paste), exchange it, cache the token."""
    if code:
        return _finish_login(code)

    url = build_login_url()
    print(f"Open this URL, log in, and approve the app:\n  {url}")
    if REDIRECT_URI and ("localhost" in REDIRECT_URI or "127.0.0.1" in REDIRECT_URI) and f":{port}" in REDIRECT_URI:
        print(f"Waiting for redirect on http://localhost:{port}/ ...")
        code = wait_for_code(port)
    if not code:
        print(f"redirect_uri is '{REDIRECT_URI}' -- can't auto-capture that locally.")
        print("After approving, copy the `code=...` value from the browser's URL bar and paste it here,")
        print("or rerun as: python3 upstox_client.py login --code <code>")
        code = input("code: ").strip()
    return _finish_login(code)


def _finish_login(code):
    token_response = exchange_code(code)
    if "access_token" not in token_response:
        print(f"[error] Token exchange failed: {token_response}")
        return None
    save_token(token_response)
    print("Access token cached for today.")
    return token_response["access_token"]


def get_market_status(exchange="NSE"):
    """Live status straight from the exchange, via Upstox's own market-status
    endpoint (GET /v2/market/status/{exchange}) -- verified against Upstox's
    real API docs: real values include NORMAL_OPEN, PRE_OPEN_START/END,
    CLOSING_START/END, NORMAL_CLOSE, plus NSE-equity-only closing-auction
    (CAS) sub-statuses. Deliberately NOT a hardcoded weekday+09:15-15:30
    schedule (see trade_bot.py's _market_is_closed, kept as a fallback for
    when this call fails) -- a fixed schedule has no way to know about a
    special session (e.g. Muhurat trading, held on an evening that's
    otherwise a market holiday) or an unscheduled closure; this reflects
    whatever the exchange is ACTUALLY doing right now."""
    token = load_token()
    if not token:
        raise RuntimeError("No valid token cached for today -- run `python3 upstox_client.py login` first.")
    req = urllib.request.Request(
        f"{MARKET_STATUS_URL}/{exchange}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": "Mozilla/5.0"},
    )
    payload = _fetch_json(req)
    return payload["data"]["status"]


def is_market_open(exchange="NSE"):
    """True only if get_market_status() reports live trading is actually
    happening right now (see MARKET_OPEN_STATUSES)."""
    return get_market_status(exchange) in MARKET_OPEN_STATUSES


def get_ltp(instrument_key):
    """instrument_key format e.g. 'NSE_EQ|INE002A01018' -- look these up from
    Upstox's instrument master CSV, not from the trading symbol directly."""
    token = load_token()
    if not token:
        raise RuntimeError("No valid token cached for today -- run `python3 upstox_client.py login` first.")
    params = urllib.parse.urlencode({"instrument_key": instrument_key})
    req = urllib.request.Request(
        f"{LTP_URL}?{params}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": "Mozilla/5.0"},
    )
    payload = _fetch_json(req)
    (quote,) = payload["data"].values()
    return quote["last_price"]


def _resolve_instrument_key(symbol):
    import instrument_master
    try:
        return instrument_master.get_instrument_key(symbol)
    except KeyError as e:
        raise RuntimeError(str(e))


def get_historical_close(instrument_key, on_or_before):
    """Closing price from the daily candle on/nearest before `on_or_before` (a date)."""
    token = load_token()
    if not token:
        raise RuntimeError("No valid token cached for today -- run `python3 upstox_client.py login` first.")
    from_date = (on_or_before - timedelta(days=5)).strftime("%Y-%m-%d")
    to_date = on_or_before.strftime("%Y-%m-%d")
    url = f"{HISTORICAL_CANDLE_URL}/{instrument_key}/day/{to_date}/{from_date}"
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": "Mozilla/5.0"},
    )
    payload = _fetch_json(req)
    candles = payload["data"]["candles"]  # each: [timestamp, open, high, low, close, volume, oi]
    if not candles:
        return None
    return candles[0][4]  # most recent candle <= to_date, closes come back newest-first


def get_daily_candles(instrument_key, from_date, to_date):
    """Raw daily candles (newest-first) over an arbitrary [from_date, to_date]
    window -- get_historical_close() only ever needs the single closest
    candle, so it doesn't expose the full list; this does, for callers (e.g.
    mock-fixture generation) that want real historical closes across a
    longer range instead of just one lookup."""
    token = load_token()
    if not token:
        raise RuntimeError("No valid token cached for today -- run `python3 upstox_client.py login` first.")
    url = f"{HISTORICAL_CANDLE_URL}/{instrument_key}/day/{to_date.strftime('%Y-%m-%d')}/{from_date.strftime('%Y-%m-%d')}"
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": "Mozilla/5.0"},
    )
    payload = _fetch_json(req)
    return payload["data"]["candles"]  # each: [timestamp, open, high, low, close, volume, oi]


def get_intraday_candles(instrument_key):
    """Raw today-so-far 1-minute candles -- get_intraday_high() only ever
    needs the max, so it doesn't expose the full list; this does, for callers
    that want the actual real intraday shape instead of just the high.
    TODAY ONLY -- see get_historical_minute_candles() for any other date."""
    token = load_token()
    if not token:
        raise RuntimeError("No valid token cached for today -- run `python3 upstox_client.py login` first.")
    url = f"{INTRADAY_CANDLE_URL}/{instrument_key}/minutes/1"
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": "Mozilla/5.0"},
    )
    payload = _fetch_json(req)
    return payload["data"]["candles"]  # each: [timestamp, open, high, low, close, volume, oi]


def get_historical_minute_candles(instrument_key, date):
    """1-minute candles for a specific PAST calendar date (`date`, a
    date object) -- distinct from get_intraday_candles(), which only ever
    returns TODAY's candles-so-far and 404s/empties for any other day.
    Upstox's v3 historical-candle endpoint (unit='minutes', interval='1')
    covers minute-level history back to January 2022 per Upstox's own docs.

    Verified live: a single-day range (from_date == to_date) on this
    endpoint returns an empty candle list even for a date that genuinely
    has data -- a real Upstox quirk, not a data-availability lag (a wider
    range covering the same date returns it correctly, confirmed against
    get_intraday_candles()'s own same-day values). Works around this by
    requesting a week-wide window ending on `date` (covers weekends) and
    filtering down to just that calendar date's candles client-side.
    Same newest-first raw shape as get_intraday_candles()."""
    token = load_token()
    if not token:
        raise RuntimeError("No valid token cached for today -- run `python3 upstox_client.py login` first.")
    from_date = (date - timedelta(days=7)).strftime("%Y-%m-%d")
    to_date = date.strftime("%Y-%m-%d")
    url = f"{UPSTOX_API_BASE}/v3/historical-candle/{instrument_key}/minutes/1/{to_date}/{from_date}"
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": "Mozilla/5.0"},
    )
    payload = _fetch_json(req)
    candles = payload["data"]["candles"]  # each: [timestamp, open, high, low, close, volume, oi]
    return [c for c in candles if c[0].startswith(to_date)]


def get_minute_candles_for_date(symbol, date):
    """Single source of truth for "give me this symbol's 1-minute candles
    for `date`" regardless of whether that date is today or in the past --
    callers (analysis/backtest.py, anything else that needs a day's candle
    series) should go through this, not call get_intraday_candles()/
    get_historical_minute_candles() directly, so the today-vs-past dispatch
    only lives in one place. Compares against db.now()'s IST calendar date,
    not a naive today check, for the same reason the rest of this project's
    date math goes through db.now()/IST (see db.py's IST constant)."""
    instrument_key = _resolve_instrument_key(symbol)
    if date == db.now().astimezone(db.IST).date():
        return get_intraday_candles(instrument_key)
    return get_historical_minute_candles(instrument_key, date)


def get_current_price(symbol):
    """Matches the old market_data.py interface, backed by Upstox instead of yfinance."""
    instrument_key = _resolve_instrument_key(symbol)
    return get_ltp(instrument_key)


def get_mock_replay_time(symbol):
    """Test-only: the real historical minute mock_upstox.py's LTP replay is
    currently on for `symbol`, or None if we're not pointed at a mock
    (UPSTOX_API_BASE is the real API -- production trading always uses real
    wall-clock time, this never applies there) or the mock has no replay
    data for this symbol yet. Lets trade_bot.py stamp entry_time/exit_time
    with the actual simulated market time during tests, instead of whatever
    real wall-clock moment the test script happened to execute at."""
    if UPSTOX_API_BASE == "https://api.upstox.com":
        return None
    try:
        instrument_key = _resolve_instrument_key(symbol)
    except RuntimeError:
        return None
    params = urllib.parse.urlencode({"instrument_key": instrument_key})
    req = urllib.request.Request(
        f"{UPSTOX_API_BASE}/mock/replay-time?{params}",
        headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            payload = json.loads(resp.read())
    except (urllib.error.URLError, urllib.error.HTTPError):
        return None
    ts = payload.get("ts")
    return datetime.fromisoformat(ts) if ts else None


def get_intraday_high(instrument_key):
    """Highest price so far today, from 1-minute candles (includes the
    in-progress day, verified live -- not just completed days like the daily
    /historical-candle endpoint). None if no candles yet (e.g. before market open)."""
    token = load_token()
    if not token:
        raise RuntimeError("No valid token cached for today -- run `python3 upstox_client.py login` first.")
    url = f"{INTRADAY_CANDLE_URL}/{instrument_key}/minutes/1"
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": "Mozilla/5.0"},
    )
    payload = _fetch_json(req)
    candles = payload["data"]["candles"]  # each: [timestamp, open, high, low, close, volume, oi]
    if not candles:
        return None
    return max(c[2] for c in candles)


def get_intraday_gain_pct(symbol):
    """Day's max gain so far, as % vs the PREVIOUS trading day's close (the
    standard "stock up X% today" convention) -- not vs today's own open.
    None if either the intraday high or the previous close isn't available."""
    instrument_key = _resolve_instrument_key(symbol)
    day_high = get_intraday_high(instrument_key)
    prev_close = get_historical_close(instrument_key, db.now(symbol).date() - timedelta(days=1))
    if day_high is None or not prev_close:
        return None
    return (day_high - prev_close) / prev_close * 100


def get_last_close(symbol):
    """Most recent completed trading day's close -- unlike get_intraday_gain_pct
    (which needs a live intraday candle, unavailable while the market's
    closed), this is historical daily-candle data and works fine on a
    weekend too. Used for weekend limit-order queuing (see trade_bot.py's
    _queue_weekend_order) -- the "last traded value" a real broker's AMO
    limit price would be based off. None if no historical candle is found."""
    instrument_key = _resolve_instrument_key(symbol)
    return get_historical_close(instrument_key, db.now(symbol).date())


def get_recent_performance(symbol):
    """Return {'1d': pct_change, '1w': pct_change, '1m': pct_change}, None entries if no data."""
    instrument_key = _resolve_instrument_key(symbol)
    current = get_ltp(instrument_key)
    today = db.now(symbol).date()
    result = {}
    for label, days_back in WEAKNESS_LOOKBACK_DAYS.items():
        past_close = get_historical_close(instrument_key, today - timedelta(days=days_back))
        result[label] = ((current - past_close) / past_close * 100) if past_close else None
    return result


def is_recently_weak(performance, threshold_pct=-5.0):
    """True if the stock already fell >= 5% over 1d, 1w, or 1m."""
    return any(v is not None and v <= threshold_pct for v in performance.values())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p_login = sub.add_parser("login", help="Run the OAuth login flow and cache today's access token")
    p_login.add_argument("--port", type=int, default=5000)
    p_login.add_argument("--code", default=None, help="Auth code copied from the redirect URL, skips local capture")
    sub.add_parser("status", help="Show whether a valid cached token exists for today")
    args = parser.parse_args()

    if args.command == "login":
        login(args.port, code=args.code)
    elif args.command == "status":
        token = load_token()
        print("Valid token cached for today." if token else "No valid token for today -- run `login`.")


if __name__ == "__main__":
    main()
