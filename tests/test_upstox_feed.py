"""
Coverage for UpstoxFeed's on_tick callback (see trading/trade_bot.py's
`monitor --tick`) -- pure protobuf-parsing + callback-dispatch logic, no
live WebSocket connection needed: _on_message can be called directly with a
hand-built FeedResponse.

Run: ./.venv/bin/python -m pytest tests/test_upstox_feed.py -v
"""
import threading

import MarketDataFeed_pb2 as pb
import upstox_feed


def _bare_feed(on_tick=None, reconnect_delay=0):
    """UpstoxFeed.__new__ instead of UpstoxFeed(...) -- skips __init__'s
    instrument_master.get_instrument_key() lookups (a real DB/network call),
    which _on_message itself never needs."""
    feed = upstox_feed.UpstoxFeed.__new__(upstox_feed.UpstoxFeed)
    feed.symbol_to_key = {"RELIANCE": "NSE_EQ|INE002A01018"}
    feed.key_to_symbol = {"NSE_EQ|INE002A01018": "RELIANCE"}
    feed.on_tick = on_tick
    feed.reconnect_delay = reconnect_delay
    feed._lock = threading.Lock()
    feed._latest = {}
    feed._got_first_message = threading.Event()
    feed._stopping = threading.Event()
    feed._ws = None
    return feed


def _tick_message(instrument_key, ltp, ltt=1700000000000):
    response = pb.FeedResponse()
    response.feeds[instrument_key].ltpc.ltp = ltp
    response.feeds[instrument_key].ltpc.ltt = ltt
    return response.SerializeToString()


def _full_d5_message(instrument_key, ltp, ltt=1700000000000, ltq=10, cp=100.0,
                      vtt=5000, atp=101.5, tbq=200.0, tsq=150.0):
    """Builds a "full_d5"-shaped message (fullFeed.marketFF), not the bare
    ltpc-shaped one _tick_message builds -- see _extract_ltpc_and_extra's
    docstring for why these land in a completely different oneof."""
    response = pb.FeedResponse()
    mff = response.feeds[instrument_key].fullFeed.marketFF
    mff.ltpc.ltp = ltp
    mff.ltpc.ltt = ltt
    mff.ltpc.ltq = ltq
    mff.ltpc.cp = cp
    mff.vtt, mff.atp, mff.tbq, mff.tsq = vtt, atp, tbq, tsq
    ohlc = mff.marketOHLC.ohlc.add()
    ohlc.interval, ohlc.open, ohlc.high, ohlc.low, ohlc.close, ohlc.vol, ohlc.ts = (
        "1d", 99.0, 102.0, 98.5, ltp, vtt, ltt)
    quote = mff.marketLevel.bidAskQuote.add()
    quote.bidQ, quote.bidP, quote.askQ, quote.askP = 50, ltp - 0.05, 60, ltp + 0.05
    return response.SerializeToString()


def test_on_tick_fires_with_symbol_not_instrument_key():
    received = []
    feed = _bare_feed(on_tick=lambda symbol, ltp, ltt, extra: received.append((symbol, ltp, ltt)))
    feed._on_message(None, _tick_message("NSE_EQ|INE002A01018", 1234.5))
    assert received == [("RELIANCE", 1234.5, 1700000000000)]


def test_get_ltp_reflects_latest_tick_regardless_of_on_tick():
    feed = _bare_feed(on_tick=None)  # no callback registered at all
    feed._on_message(None, _tick_message("NSE_EQ|INE002A01018", 999.0))
    assert feed.get_ltp("RELIANCE") == 999.0


def test_on_tick_exception_does_not_break_message_handling():
    """A bad callback must not prevent _latest from updating, or crash the
    caller (the real WebSocket thread) -- see _on_message's try/except."""
    def bad_callback(symbol, ltp, ltt, extra):
        raise ValueError("simulated callback bug")

    feed = _bare_feed(on_tick=bad_callback)
    feed._on_message(None, _tick_message("NSE_EQ|INE002A01018", 500.0))  # must not raise
    assert feed.get_ltp("RELIANCE") == 500.0


# ---- full_d5 extraction (_extract_ltpc_and_extra) -- the richer payload
# trade_bot.py's `monitor --tick` now requests instead of plain ltpc ----

def test_ltpc_mode_extra_has_only_ltq_and_cp():
    """Plain ltpc-mode ticks (the default, and what most existing tests use)
    carry ltq/cp for free (part of LTPC itself) but nothing else -- vtt/atp/
    tbq/tsq/ohlc/depth only exist in full_d5/full_d30's separate oneof."""
    received = []
    feed = _bare_feed(on_tick=lambda symbol, ltp, ltt, extra: received.append(extra))
    response = pb.FeedResponse()
    response.feeds["NSE_EQ|INE002A01018"].ltpc.ltp = 100.0
    response.feeds["NSE_EQ|INE002A01018"].ltpc.ltt = 1700000000000
    response.feeds["NSE_EQ|INE002A01018"].ltpc.ltq = 42
    response.feeds["NSE_EQ|INE002A01018"].ltpc.cp = 99.0
    feed._on_message(None, response.SerializeToString())
    assert received == [{"ltq": 42, "cp": 99.0}]


def test_full_d5_mode_extra_carries_depth_volume_and_ohlc():
    received = []
    feed = _bare_feed(on_tick=lambda symbol, ltp, ltt, extra: received.append(extra))
    feed._on_message(None, _full_d5_message("NSE_EQ|INE002A01018", 100.0))
    (extra,) = received
    assert extra["ltq"] == 10 and extra["cp"] == 100.0
    assert extra["vtt"] == 5000 and extra["atp"] == 101.5
    assert extra["tbq"] == 200.0 and extra["tsq"] == 150.0
    assert extra["ohlc"] == [{"interval": "1d", "open": 99.0, "high": 102.0,
                               "low": 98.5, "close": 100.0, "vol": 5000, "ts": 1700000000000}]
    assert extra["depth"] == [{"bid_qty": 50, "bid_price": 99.95, "ask_qty": 60, "ask_price": 100.05}]


def test_full_d5_get_ltp_still_works():
    """get_ltp/get_ltt must keep working regardless of which oneof the
    price actually came from -- they only ever read _latest's ltp/ltt keys."""
    feed = _bare_feed(on_tick=None)
    feed._on_message(None, _full_d5_message("NSE_EQ|INE002A01018", 250.0, ltt=1700000001000))
    assert feed.get_ltp("RELIANCE") == 250.0
    assert feed.get_ltt("RELIANCE") == 1700000001000


def test_on_tick_not_called_for_unknown_instrument_key():
    """A tick for an instrument this feed never subscribed to (shouldn't
    happen in practice, but _on_message iterates whatever the server sends)
    must not crash key_to_symbol.get() or fire a callback with a bogus symbol."""
    received = []
    feed = _bare_feed(on_tick=lambda *a: received.append(a))
    feed._on_message(None, _tick_message("NSE_EQ|UNKNOWN00000", 1.0))
    assert received == []


# ---- auto-reconnect (see _run_with_reconnect) -- a dropped/failed
# connection used to just silently end the background thread, meaning
# `monitor --tick` (meant to run unattended for hours) could go dark forever
# with no indication anything was wrong. ----

def test_run_with_reconnect_retries_on_connect_failure(monkeypatch):
    """Simulates repeated connect failures (e.g. an expired token) -- the
    loop must retry rather than give up after one failure. Never touches a
    real socket: _get_feed_ws_url is monkeypatched to just raise."""
    feed = _bare_feed(reconnect_delay=0)
    attempts = {"n": 0}

    def fake_get_url():
        attempts["n"] += 1
        if attempts["n"] >= 3:
            feed._stopping.set()  # stop the test after the 3rd attempt
        raise RuntimeError("simulated connect failure")

    monkeypatch.setattr(upstox_feed, "_get_feed_ws_url", fake_get_url)
    feed._run_with_reconnect()
    assert attempts["n"] == 3


def test_run_with_reconnect_picks_up_fresh_token_after_failure(monkeypatch):
    """The exact scenario the user asked for: token was bad, gets replaced
    in the file mid-session -- the very next reconnect attempt must succeed
    without needing a restart. _get_feed_ws_url stands in for "the token
    file now has a working token" by just succeeding on a later call."""
    feed = _bare_feed(reconnect_delay=0)
    attempts = {"n": 0}

    class FakeWSApp:
        def __init__(self, *a, **kw):
            pass

        def run_forever(self, **kw):
            feed._stopping.set()  # "connected successfully" -- end the test here
            return None

    def fake_get_url():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("simulated: old token still expired")
        return "wss://fake"  # "simulated: fresh token from the file now works"

    monkeypatch.setattr(upstox_feed, "_get_feed_ws_url", fake_get_url)
    monkeypatch.setattr(upstox_feed.websocket, "WebSocketApp", FakeWSApp)
    feed._run_with_reconnect()
    assert attempts["n"] == 2  # failed once, succeeded on the reconnect


def test_stop_sets_stopping_flag_before_closing():
    feed = _bare_feed()
    assert not feed._stopping.is_set()
    feed._ws = None  # stop() must handle "never actually connected" gracefully
    feed._thread = None
    feed.stop()
    assert feed._stopping.is_set()


def test_on_close_does_not_warn_when_stop_was_intentional(capsys):
    feed = _bare_feed()
    feed._stopping.set()  # simulates stop() having already been called
    feed._on_close(None, 1000, "normal closure")
    assert "will reconnect" not in capsys.readouterr().out
