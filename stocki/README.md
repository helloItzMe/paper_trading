# stocki — monitoring dashboard

A read-only web dashboard over the same `investment_bot` Postgres database
`db.py` writes. TypeScript/Node/React, kept deliberately separate from the
Python trading pipeline: it never touches Upstox, never places or influences a
trade, and adds nothing to the buy/sell decision path.

## Why this exists

`trade_bot.py status` prints one line per open position and nothing else --
no P&L totals, no history, no signal feed, no sense of whether the pipeline is
even alive. See the top-level `CLAUDE.md` for full pipeline context.

## Architecture

```
Postgres (investment_bot)
  ├── written by:  Python CLI (db.py)               -- unchanged, plus scan_runs
  └── read by:     stocki/server (Express + pg)     -- SELECT only
                        ▲ REST /api/*
                   stocki/web (Vite + React + TS)
```

**Deviation from CLAUDE.md's "db.py is the only place SQL lives":** all
dashboard SQL lives in exactly one file, `server/src/queries.ts`. The rule
becomes "SQL lives in two places, each owning one direction: `db.py` writes,
`queries.ts` reads."

**No live prices.** The dashboard never calls Upstox -- no token, no second
WebSocket connection, no REST rate limits. Every metric is a count or sum over
rows the bot already writes, bucketed by their existing timestamps. The one
consequence: open positions show entry price/target/stop-loss, not a current
price or unrealized P&L (that number isn't persisted anywhere).

**Test-data attribution.** Every table has a `"user"` column ('tester' for
`mock_servers/` runs, 'ashish' for real ones). Every API query takes `?user=`
(default `ashish`); the header has a toggle so mock data stays visible during
development without polluting the default "real" view.

**Polling: 10s, everything.** `scan --watch` defaults to 300s but `monitor
--watch` (which closes positions and writes realized P&L) is routinely run
much tighter -- the dashboard tracks the faster writer.

## Running it

Requires the same Postgres setup as the rest of the project (`db.py init`
already run) -- no separate config.

```bash
# backend, port 4000
cd stocki/server && npm install && npm run dev

# frontend, port 5173, proxies /api -> localhost:4000 (see vite.config.ts)
cd stocki/web && npm install && npm run dev
```

Open http://localhost:5173. Use the header's user toggle to switch between
`ashish` (real runs) and `tester` (mock_servers/ runs) -- useful for seeing
populated charts without a live Upstox account.

## Pipeline health (`scan_runs`)

`db.py`'s `start_run`/`finish_run` and `trade_bot.py`'s wrapped `cmd_scan`/
`cmd_monitor` write a row per invocation: started_at, finished_at, counts, and
an error message on failure. A row with `finished_at IS NULL` and no error
means "still running or died without saying why" -- that ambiguity used to be
unresolvable (a scan that finds nothing writes no other row at all). The
dashboard's health strip flags a run as degraded if it errored or has been
"running" implausibly long.

## Out of scope (by design)

- Per-stock intraday price charts / unrealized P&L -- would require fetching
  prices, the one thing this design avoids.
- Any write action (e.g. confirming a pending candidate) -- read-only keeps
  `trade_bot.py confirm` the single place trades are approved.
- Auth -- single-user, intended for localhost only.
