#!/usr/bin/env python3
"""
Upstox V3 Market Data Feed -- live LTP (last traded price) over WebSocket.

Verified against Upstox's real docs/schema (fetched directly, not guessed):
    - authorize endpoint: GET /v3/feed/market-data-feed/authorize (v2 was
      sunset -- confirmed live against the real API, not just docs)
    - proto schema: https://assets.upstox.com/feed/market-data-feed/v3/MarketDataFeed.proto
      (copied into MarketDataFeed.proto, compiled to MarketDataFeed_pb2.py via
      `python -m grpc_tools.protoc -I. --python_out=. MarketDataFeed.proto`)
    - subscribe message: {"guid": ..., "method": "sub", "data": {"mode": "ltpc", "instrumentKeys": [...]}}
    - feed messages arrive as binary Protobuf (FeedResponse), not JSON

Requires a valid cached token (upstox_client.login()). Verified live end-to-end
against a real account -- streamed real ticks for RELIANCE/TCS/INFY.

Standard-account limits (confirmed against Upstox's docs): 5,000 instrument
keys per connection in LTPC mode, 2 connections per user -- nowhere close to
what trade_bot.py needs (a handful of open positions at a time), so add_symbols
below just grows the same connection's subscription rather than reconnecting.

Usage:
    feed = UpstoxFeed(["RELIANCE", "TCS"])
    feed.start()
    feed.wait_for_data(timeout=10)
    print(feed.get_ltp("RELIANCE"))
    feed.add_symbols(["INFY"])   # extend subscriptions without reconnecting
    feed.stop()

    # Or react to every tick instead of polling get_ltp() yourself:
    feed = UpstoxFeed(["RELIANCE"], on_tick=lambda symbol, ltp, ltt, extra: print(symbol, ltp))

`extra` (4th on_tick arg, and also returned by get_extra()) is a dict of
whatever this feed's `mode` carries beyond bare price/time. In "ltpc" mode
(the default) it's always just {"ltq": ..., "cp": ...} -- both fields are
part of the LTPC message itself regardless of mode, so they're free. In
"full"/"full_d30" mode it additionally carries {"vtt", "atp", "tbq",
"tsq", "ohlc": [...], "depth": [...]} -- see _extract_ltpc_and_extra's
docstring for exactly where each comes from in Upstox's real schema.
Note: "full" is the real subscribe-request mode string for 5-level depth --
"full_d5" (which appears as a RequestMode enum NAME in the proto, echoed
back on each response) is NOT a valid value to send in the subscribe
message; sending it gets no feed data back at all, only the periodic
marketInfo heartbeat (verified live -- this broke monitor --tick silently
for a while, see trade_bot.py's monitor --tick call site).
"""
import json
import threading
import time
import urllib.request

import websocket

import instrument_master
import upstox_client
import MarketDataFeed_pb2 as pb

AUTHORIZE_URL = "https://api.upstox.com/v3/feed/market-data-feed/authorize"


def _get_feed_ws_url():
    token = upstox_client.load_token()
    if not token:
        raise RuntimeError("No valid token cached for today -- run `python3 upstox_client.py login` first.")
    req = urllib.request.Request(
        AUTHORIZE_URL,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": "Mozilla/5.0"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        payload = json.loads(resp.read())
    return payload["data"]["authorized_redirect_uri"]


def _extract_ltpc_and_extra(feed):
    """Returns (ltpc, extra) for whichever oneof variant this Feed message
    actually carries -- see MarketDataFeed.proto's `Feed`/`FullFeed` oneofs.
    ltpc is always an LTPC submessage (or None if this feed has neither --
    shouldn't happen in practice, but the oneof is technically optional);
    extra is a dict of the richer fields ONLY "full"/"full_d30" mode
    carries (empty in plain "ltpc" mode, where feed.ltpc is the top-level
    field directly and there's nothing more to extract).

    "full"/"full_d30" wraps an equity's data in fullFeed.marketFF (a
    DIFFERENT oneof, FullFeedUnion, one level down) -- an index subscribed
    in the same mode instead carries fullFeed.indexFF, which has no
    order-book depth/volume/buy-sell-quantity fields at all (indices don't
    trade directly), so extra stays empty for those. ohlc/depth are
    flattened into plain dicts (not raw protobuf message objects) so
    callers (e.g. tick_store.py's JSON serialization) don't need to know
    anything about protobuf."""
    if feed.HasField("ltpc"):
        return feed.ltpc, {}
    if feed.HasField("fullFeed"):
        full = feed.fullFeed
        if full.HasField("marketFF"):
            mff = full.marketFF
            extra = {
                "vtt": mff.vtt, "atp": mff.atp, "tbq": mff.tbq, "tsq": mff.tsq,
                "ohlc": [{"interval": o.interval, "open": o.open, "high": o.high,
                          "low": o.low, "close": o.close, "vol": o.vol, "ts": o.ts}
                         for o in mff.marketOHLC.ohlc],
                "depth": [{"bid_qty": q.bidQ, "bid_price": q.bidP,
                           "ask_qty": q.askQ, "ask_price": q.askP}
                          for q in mff.marketLevel.bidAskQuote],
            }
            return mff.ltpc, extra
        if full.HasField("indexFF"):
            return full.indexFF.ltpc, {}
    if feed.HasField("firstLevelWithGreeks"):
        fg = feed.firstLevelWithGreeks
        extra = {
            "vtt": fg.vtt,
            "depth": [{"bid_qty": fg.firstDepth.bidQ, "bid_price": fg.firstDepth.bidP,
                       "ask_qty": fg.firstDepth.askQ, "ask_price": fg.firstDepth.askP}],
        }
        return fg.ltpc, extra
    return None, {}


class UpstoxFeed:
    def __init__(self, symbols, mode="ltpc", on_tick=None, reconnect_delay=5):
        """on_tick(symbol, ltp, ltt, extra), if given, is called for every
        real tick as it arrives (from the WebSocket thread, not the
        caller's) -- lets trade_bot.py's `monitor --tick` react instantly
        instead of polling get_ltp() on a timer. See _extract_ltpc_and_extra
        for exactly what `extra` contains for a given `mode`.

        reconnect_delay: seconds to wait between reconnect attempts after an
        unexpected disconnect (bad/expired token, network blip, Upstox-side
        restart, etc.) -- see _run_with_reconnect. Previously there was NO
        reconnect at all: a dropped connection just silently ended the
        background thread, and `monitor --tick` (meant to run unattended,
        potentially for hours) would sit there forever looking alive while
        never receiving another tick. Each reconnect attempt re-fetches the
        auth URL (_get_feed_ws_url -> load_token(), which always reads
        .upstox_token.json fresh off disk) -- so replacing an expired token
        file mid-session is picked up automatically on the next attempt,
        no restart needed."""
        self.mode = mode
        self.symbol_to_key = {s: instrument_master.get_instrument_key(s) for s in symbols}
        self.key_to_symbol = {v: k for k, v in self.symbol_to_key.items()}
        self.on_tick = on_tick
        self.reconnect_delay = reconnect_delay
        self._latest = {}  # instrument_key -> {"ltp": float, "ltt": int}
        self._lock = threading.Lock()
        self._ws = None
        self._thread = None
        self._got_first_message = threading.Event()
        self._stopping = threading.Event()  # set by stop() -- tells the reconnect loop to give up, not retry

    def _send_subscribe(self, instrument_keys):
        sub_msg = {
            "guid": str(int(time.time() * 1000)),
            "method": "sub",
            "data": {"mode": self.mode, "instrumentKeys": instrument_keys},
        }
        self._ws.send(json.dumps(sub_msg).encode("utf-8"), opcode=websocket.ABNF.OPCODE_BINARY)

    def _on_open(self, ws):
        self._send_subscribe(list(self.symbol_to_key.values()))

    def add_symbols(self, symbols):
        """Extend this connection's subscriptions without reconnecting."""
        new = {s: instrument_master.get_instrument_key(s) for s in symbols if s not in self.symbol_to_key}
        if not new:
            return
        self.symbol_to_key.update(new)
        self.key_to_symbol.update({v: k for k, v in new.items()})
        if self._ws and self._ws.sock and self._ws.sock.connected:
            self._send_subscribe(list(new.values()))

    def _on_message(self, ws, message):
        response = pb.FeedResponse()
        response.ParseFromString(message)
        ticks = []
        with self._lock:
            for instrument_key, feed in response.feeds.items():
                ltpc, extra = _extract_ltpc_and_extra(feed)
                if ltpc is not None and ltpc.ltp:
                    extra["ltq"], extra["cp"] = ltpc.ltq, ltpc.cp
                    self._latest[instrument_key] = {"ltp": ltpc.ltp, "ltt": ltpc.ltt, "extra": extra}
                    symbol = self.key_to_symbol.get(instrument_key)
                    if symbol is not None:
                        ticks.append((symbol, ltpc.ltp, ltpc.ltt, extra))
        self._got_first_message.set()
        # Fire callbacks AFTER releasing _lock -- on_tick calling back into
        # get_ltp()/get_ltt() (which also acquire _lock) would otherwise
        # deadlock, since threading.Lock isn't reentrant.
        if self.on_tick is not None:
            for symbol, ltp, ltt, extra in ticks:
                try:
                    self.on_tick(symbol, ltp, ltt, extra)
                except Exception as e:
                    # Never let a callback failure kill the WebSocket's
                    # message-handling thread -- one bad tick shouldn't end
                    # the whole live feed.
                    print(f"[warn] on_tick callback failed for {symbol}: {e}")

    def _on_error(self, ws, error):
        print(f"[warn] UpstoxFeed connection error: {error}")

    def _on_close(self, ws, close_status_code, close_msg):
        if not self._stopping.is_set():
            print(f"[warn] UpstoxFeed connection closed unexpectedly "
                  f"(code={close_status_code}, msg={close_msg}) -- will reconnect.")

    def _run_with_reconnect(self):
        """Keep (re)connecting until stop() is called -- one connection
        attempt's failure/drop must not end the whole feed. _on_open already
        re-subscribes every symbol currently in self.symbol_to_key on each
        successful (re)connect, including any added via add_symbols() since
        the last connection, so resubscription needs no extra handling here."""
        while not self._stopping.is_set():
            try:
                ws_url = _get_feed_ws_url()
                self._ws = websocket.WebSocketApp(
                    ws_url, on_open=self._on_open, on_message=self._on_message,
                    on_error=self._on_error, on_close=self._on_close,
                )
                self._ws.run_forever(ping_interval=20)  # blocks until the connection closes or errors out
            except Exception as e:
                print(f"[warn] UpstoxFeed failed to connect: {e}")
            if self._stopping.is_set():
                break
            print(f"[info] UpstoxFeed reconnecting in {self.reconnect_delay}s...")
            self._stopping.wait(self.reconnect_delay)  # interruptible -- wakes immediately if stop() is called

    def start(self):
        self._thread = threading.Thread(target=self._run_with_reconnect, daemon=True)
        self._thread.start()

    def wait_for_data(self, timeout=15):
        return self._got_first_message.wait(timeout)

    def get_ltp(self, symbol):
        instrument_key = self.symbol_to_key.get(symbol)
        if instrument_key is None:
            raise KeyError(f"'{symbol}' wasn't subscribed on this feed.")
        with self._lock:
            entry = self._latest.get(instrument_key)
        return entry["ltp"] if entry else None

    def get_ltt(self, symbol):
        """Last-traded time (epoch ms) for `symbol`, or None if no tick yet."""
        instrument_key = self.symbol_to_key.get(symbol)
        if instrument_key is None:
            raise KeyError(f"'{symbol}' wasn't subscribed on this feed.")
        with self._lock:
            entry = self._latest.get(instrument_key)
        return entry["ltt"] if entry else None

    def stop(self):
        self._stopping.set()  # tells _run_with_reconnect this close is intentional -- don't reconnect
        if self._ws:
            self._ws.close()
        if self._thread:
            self._thread.join(timeout=5)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Print live LTP for given NSE symbols via Upstox WebSocket feed")
    parser.add_argument("symbols", nargs="+", help="NSE trading symbols, e.g. RELIANCE TCS INFY")
    parser.add_argument("--seconds", type=int, default=15, help="How long to watch before exiting")
    args = parser.parse_args()

    try:
        feed = UpstoxFeed(args.symbols)
        feed.start()
    except (RuntimeError, KeyError) as e:
        print(f"[error] {e}")
        raise SystemExit(1)

    print("Waiting for first tick...")
    if not feed.wait_for_data(timeout=15):
        print("No data received within timeout.")
    else:
        end = time.time() + args.seconds
        while time.time() < end:
            for s in args.symbols:
                print(f"{s}: {feed.get_ltp(s)}")
            time.sleep(1)
    feed.stop()
