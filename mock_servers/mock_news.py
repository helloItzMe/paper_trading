#!/usr/bin/env python3
"""
Local stand-in for Google News RSS + the two direct-publisher feeds,
backed by fixtures/news_fixture.json (see generate_news_fixture.py).

Every fixture item's pubDate is rewritten to "now - age_minutes" ON EACH
REQUEST, not baked in at fixture-generation time -- trade_bot.py discards
anything older than an hour before ever classifying it
(_is_fresh_enough/NEWS_MAX_AGE), so a fixture with a fixed absolute pubDate
would silently go stale the moment enough wall-clock time passed between
generating it and running a test.

Serves the same shapes news_bot.py's parse_items() expects:
    GET /rss/search?q=...   -> Google News RSS style, <item> has a <source> tag
    GET /feed/livemint      -> direct-publisher style, no <source> tag
    GET /feed/et            -> same

Point news_bot.py at this with:
    NEWS_MOCK_BASE=http://localhost:8802

All queries return the full fixture list (real Google News also returns
overlapping results across differently-worded queries; news_bot.dedupe()
already collapses duplicates by title downstream, same as it would for the
real API).

Usage:
    ./.venv/bin/python mock_servers/mock_news.py [--port 8802]
"""
import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from xml.sax.saxutils import escape

FIXTURE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "news_fixture.json")


def _load_fixture():
    with open(FIXTURE_PATH) as f:
        return json.load(f)


def _pub_date(age_minutes):
    dt = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
    return dt.strftime("%a, %d %b %Y %H:%M:%S %z")  # matches parse_items()'s first strptime format


def _rss_xml(items, with_source):
    parts = ['<?xml version="1.0" encoding="UTF-8"?><rss><channel>']
    for it in items:
        title = escape(it["title"])
        link = f"http://mock.test/article/{abs(hash(it['title']))}"
        pub_date = _pub_date(it["age_minutes"])
        source_tag = f'<source url="http://mock.test">{escape(it["source"])}</source>' if with_source else ""
        parts.append(
            f"<item><title>{title}</title><link>{link}</link>"
            f"<pubDate>{pub_date}</pubDate>{source_tag}</item>"
        )
    parts.append("</channel></rss>")
    return "".join(parts).encode()


class Handler(BaseHTTPRequestHandler):
    items = _load_fixture()

    def log_message(self, fmt, *a):
        pass

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/rss/search":
            body = _rss_xml(self.items, with_source=True)
        elif path in ("/feed/livemint", "/feed/et"):
            body = _rss_xml(self.items, with_source=False)
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/xml")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8802)
    args = parser.parse_args()
    print(f"Mock news serving {len(Handler.items)} fixture items on http://localhost:{args.port}")
    ThreadingHTTPServer(("localhost", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
