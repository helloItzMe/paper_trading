"""
Coverage for upstox_client._fetch_json's network-error handling.

Before this, every real API call (get_ltp, get_historical_close,
get_daily_candles, get_intraday_candles, get_intraday_high) used a raw
urllib.request.urlopen with no exception handling at all -- a transient
network failure (timeout, DNS blip, connection reset, Upstox 5xx) raised a
raw urllib.error.URLError/HTTPError/socket.timeout, none of which are
RuntimeError, so they slipped past every `except RuntimeError` guard in
trade_bot.py (e.g. cmd_monitor's price-fetch loop, _open_position's
get_intraday_gain_pct/get_current_price checks) and crashed the whole
scan/monitor run -- including an unattended `--watch` loop meant to run for
hours. Worst offender: get_recent_performance (used unguarded in
_open_position at the "is this stock recently weak" check) made four such
unprotected calls per candidate.

The fix funnels every real call through _fetch_json, which normalizes
network/HTTP/decode failures into RuntimeError -- so every existing
`except RuntimeError` call site starts handling them too, with zero changes
needed there. These tests verify that normalization directly, plus one
regression test on get_recent_performance itself (the function that was
fully unguarded).

No real network access, no Postgres, no token file needed -- urlopen and
load_token are monkeypatched throughout.
"""
import json
import socket
import urllib.error
import urllib.request

import pytest

import upstox_client


class _FakeResponse:
    """Minimal stand-in for the context-manager object urlopen() returns."""
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


@pytest.fixture(autouse=True)
def fake_token(monkeypatch):
    """Every real-API function checks load_token() first -- stub it so tests
    exercise the network-error path itself, not the "no token" one."""
    monkeypatch.setattr(upstox_client, "load_token", lambda: "fake-token-for-tests")


# ---- _fetch_json: the shared normalization point ----

def test_fetch_json_returns_parsed_payload_on_success(monkeypatch):
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda req, timeout=20: _FakeResponse(b'{"status": "success", "data": {"ok": true}}'),
    )
    req = urllib.request.Request("http://example.invalid")
    assert upstox_client._fetch_json(req) == {"status": "success", "data": {"ok": True}}


def test_fetch_json_wraps_http_error_as_runtime_error(monkeypatch):
    def raise_http_error(req, timeout=20):
        raise urllib.error.HTTPError(
            "http://example.invalid", 503, "Service Unavailable",
            hdrs=None, fp=__import__("io").BytesIO(b'{"error": "upstream down"}'),
        )
    monkeypatch.setattr(urllib.request, "urlopen", raise_http_error)
    req = urllib.request.Request("http://example.invalid")
    with pytest.raises(RuntimeError, match="HTTP 503"):
        upstox_client._fetch_json(req)


def test_fetch_json_wraps_url_error_as_runtime_error(monkeypatch):
    def raise_url_error(req, timeout=20):
        raise urllib.error.URLError("Connection refused")
    monkeypatch.setattr(urllib.request, "urlopen", raise_url_error)
    req = urllib.request.Request("http://example.invalid")
    with pytest.raises(RuntimeError, match="Network error reaching Upstox"):
        upstox_client._fetch_json(req)


def test_fetch_json_wraps_socket_timeout_as_runtime_error(monkeypatch):
    def raise_timeout(req, timeout=20):
        raise socket.timeout("timed out")
    monkeypatch.setattr(urllib.request, "urlopen", raise_timeout)
    req = urllib.request.Request("http://example.invalid")
    with pytest.raises(RuntimeError, match="Network error reaching Upstox"):
        upstox_client._fetch_json(req)


def test_fetch_json_wraps_malformed_response_as_runtime_error(monkeypatch):
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda req, timeout=20: _FakeResponse(b"not json at all"),
    )
    req = urllib.request.Request("http://example.invalid")
    with pytest.raises(RuntimeError, match="unparseable response"):
        upstox_client._fetch_json(req)


# ---- Regression: get_recent_performance was fully unguarded before this fix
# (called with no try/except at all in trade_bot.py's _open_position) ----

def test_get_recent_performance_raises_runtime_error_on_network_failure(monkeypatch):
    monkeypatch.setattr(upstox_client, "_resolve_instrument_key", lambda symbol: "NSE_EQ|FAKE00000001")

    def raise_connection_refused(req, timeout=20):
        raise urllib.error.URLError(ConnectionRefusedError("Connection refused"))
    monkeypatch.setattr(urllib.request, "urlopen", raise_connection_refused)

    with pytest.raises(RuntimeError, match="Network error reaching Upstox"):
        upstox_client.get_recent_performance("RELIANCE")


def test_get_ltp_raises_runtime_error_on_network_failure(monkeypatch):
    def raise_connection_refused(req, timeout=20):
        raise urllib.error.URLError(ConnectionRefusedError("Connection refused"))
    monkeypatch.setattr(urllib.request, "urlopen", raise_connection_refused)

    with pytest.raises(RuntimeError, match="Network error reaching Upstox"):
        upstox_client.get_ltp("NSE_EQ|FAKE00000001")


def test_get_intraday_high_raises_runtime_error_on_http_error(monkeypatch):
    def raise_http_error(req, timeout=20):
        raise urllib.error.HTTPError(
            "http://example.invalid", 500, "Internal Server Error",
            hdrs=None, fp=__import__("io").BytesIO(b"{}"),
        )
    monkeypatch.setattr(urllib.request, "urlopen", raise_http_error)

    with pytest.raises(RuntimeError, match="HTTP 500"):
        upstox_client.get_intraday_high("NSE_EQ|FAKE00000001")
