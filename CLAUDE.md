# investment — news-driven paper-trading bot for NSE

## What this is

A pipeline that watches Indian stock-market news and NSE's own regulatory
filings for two kinds of signal — **order/contract wins** and **strong
profit/revenue results** — resolves them to real NSE tickers, and paper-trades
them (simulated only, **no real orders are ever placed**) with a small,
rule-based buy/sell strategy. Every decision (what was read, what it was
classified as, why a trade was or wasn't taken) is persisted to Postgres so
it's fully auditable later.

This file exists so a _fresh_ Claude Code session (no memory of building this)
can understand the system well enough to keep working on it. If you're that
session: read this fully before changing anything, especially the "Design
decisions and why" section — several things here look wrong at first glance
and are not.

## Requirements to actually run this

Three external services must be running/configured, none of which ship with
the code:

1. **Postgres**, database `investment_bot` (already created via `createdb`).
   Connects to whatever local Postgres is already running — this machine
   already had one serving other work (a Hive metastore); `investment_bot` is
   an isolated database on that same instance, not a separate server.
2. **The `claude` CLI (Claude Code)**, installed and logged in, used in
   headless mode (`claude -p ...`) for classifying news headlines — reuses
   the existing Claude Code login/subscription, no separate API key or
   billing setup. `classifier.CLAUDE_BIN` resolves it from
   `CLAUDE_CODE_EXECPATH` (set automatically inside a Claude Code session) or
   `claude` on `PATH` otherwise. Everything that needs it fails with a clear,
   actionable error if it's not available — it does not silently degrade for
   the news path (NSE-only signals still work without it).
3. **A live, activated Upstox account**, with API credentials in
   `upstox_secrets.json` (git-ignored) and a same-day access token from
   `python3 broker/upstox_client.py login` (tokens expire ~3:30am daily).
   Without this, there is genuinely no price data — yyyyy was deliberately
   removed from this project (see below).

Python deps live in `.venv` (not system Python — Homebrew blocks global pip
installs). Always run scripts via `./.venv/bin/python`, not bare `python3`.

## Architecture — the pipeline, in order

```
                    ┌─────────────────┐     ┌──────────────────┐
                    │   nse_bot.py     │     │   news_bot.py     │
                    │  (NSE filings,   │     │ (Google News RSS, │
                    │   official,      │     │  trusted-domain   │
                    │   near-real-time)│     │  site: queries,   │
                    └────────┬─────────┘     │  2 direct feeds)  │
                             │                └─────────┬─────────┘
                             │                          │
                             │                 ┌────────▼─────────┐
                             │                 │   classifier.py   │
                             │                 │ (claude CLI: event│
                             │                 │  type, company    │
                             │                 │  names, %ages,    │
                             │                 │  materiality)     │
                             │                 └────────┬─────────┘
                             │                          │
                             │                 ┌────────▼─────────┐
                             │                 │ company_lookup.py │
                             │                 │ (grounds LLM-     │
                             │                 │  extracted names  │
                             │                 │  to REAL tickers) │
                             │                 └────────┬─────────┘
                             │                          │
                             └──────────┬───────────────┘
                                        │
                               ┌────────▼─────────┐
                               │   trade_bot.py    │  <- orchestrator + buy/sell rules
                               │  scan / confirm /  │
                               │  monitor / status   │
                               └────────┬─────────┘
                                        │
                    ┌───────────────────┼───────────────────┐
                    │                   │                   │
             ┌──────▼──────┐    ┌───────▼────────┐   ┌──────▼──────┐
             │    db.py     │    │ upstox_client.py│   │ upstox_feed.py│
             │  (Postgres,  │    │ (REST: login,   │   │ (WebSocket:  │
             │  all state)  │    │  LTP, intraday   │   │  live ticks, │
             │              │    │  high, history)  │   │  used by     │
             └──────────────┘    └────────────────┘   │  monitor      │
                                                        │  --watch)    │
                                                        └──────────────┘
```

## File-by-file reference

### Project root

- **`db.py`** — the only place SQL _lives_ for the Python side, and the only
  place anything ever _writes_. All persistent state: news articles + their
  classifications, NSE filings, companies (Upstox's instrument master),
  open/closed positions, the manual-review queue, and `scan_runs` (pipeline
  health — one row per `scan`/`monitor` invocation, via `start_run`/
  `finish_run`; a row with `finished_at IS NULL` and no `error` means "still
  running or died without saying why" — see `stocki/README.md`).
  `python3 db.py init` creates tables (idempotent); `python3 db.py stats`
  prints row counts. One deliberate exception to "only place SQL lives":
  `stocki/server/src/queries.ts` also reads directly from Postgres (see
  `stocki/` below) — the rule is now "SQL lives in two places, each owning one
  direction: `db.py` writes, `queries.ts` reads."
- **`classifier.py`** — turns a raw headline into structured signal:
  `event_type`, `category`, `signal`, `money`, `mentioned_companies`,
  `profit_pct`, `revenue_pct`, `order_is_material`. Deliberately knows
  nothing about Google News or NSE specifically — takes generic
  `{title, link, source, pub_date}` items, so a future Twitter/other source
  can reuse it unchanged.
- **`upstox_secrets.json`** — `api_key`/`api_secret`/`redirect_uri`. Git-ignored.
  Never paste its contents anywhere.
- **`.upstox_token.json`**, **`.nse_seen.json`** — small local caches, not
  meaningful application data (see "What's NOT in Postgres" below).

### `broker/` — Upstox integration

- **`upstox_client.py`** — REST: OAuth login flow, LTP quotes, historical
  daily candles, intraday 1-minute candles (for "day's max gain so far").
- **`upstox_feed.py`** — WebSocket V3 market-data feed (protobuf). Verified
  live. Used only by `trade_bot.py monitor --watch` for a persistent
  connection instead of REST-polling every symbol every cycle.
- **`MarketDataFeed.proto`** / **`MarketDataFeed_pb2.py`** — the _real_ schema,
  fetched directly from `assets.upstox.com` and compiled with
  `grpc_tools.protoc`, not reconstructed from memory.
- **`instrument_master.py`** — downloads Upstox's full NSE instrument list,
  filters to real equities, upserts into `db.py`'s `companies` table (not a
  JSON file — that was the design before the Postgres migration). Also
  persists each company's `series` (EQ/BE/BZ/SM/ST).
- **`large_cap.py`** — downloads NSE's own Nifty 50 + Nifty Next 50
  constituent lists (SEBI's real top-100-by-market-cap definition, no auth
  needed), marks those symbols `cap_tier='large'` in `companies`. Backs the
  tiered earnings-qualification thresholds in `trade_bot.py` — see the buy/
  sell rules section. Deliberately not yyyyy-sourced (see below).
- **`company_lookup.py`** — `resolve_company(name)`: fuzzy/exact match a
  free-text name to a real, verified ticker. This is the _only_ place a
  ticker gets decided — the LLM in `classifier.py` never invents one.

### `news/` — signal sources

- **`nse_bot.py`** — NSE's own corporate-announcements feed. Official, fast
  (often minutes ahead of news), but only covers filings above SEBI's
  materiality threshold — misses smaller deals entirely (structural
  limitation, not a bug).
- **`news_bot.py`** — Google News RSS (generic + `site:`-restricted queries
  against 10 trusted Indian financial publishers) + 2 direct-publisher feeds
  (Livemint, ET). Fetches and shapes raw items only — no classification logic
  lives here anymore (moved to `classifier.py`).

### `trading/`

- **`trade_bot.py`** — the orchestrator. `scan` (pull + classify + decide),
  `confirm` (manual review queue), `monitor` (check open positions, exit
  logic), `status`. This is where the actual buy/sell rules live (see below).
  `cmd_scan`/`cmd_monitor` are wrapped with `db.start_run`/`db.finish_run` so
  every invocation leaves a `scan_runs` row — purely additive, no change to
  any entry/exit rule.

### `stocki/` — monitoring dashboard (TypeScript/Node/React)

- A read-only web UI over the same Postgres database: Live tab (today,
  polling) and Historic tab (date-ranged), covering P&L, open/closed
  positions, the news/filing signal feed, a news→buy funnel, and pipeline
  health from `scan_runs`. Never talks to Upstox and never writes to the
  database — see `stocki/README.md` for the full design and the reasoning
  behind its deviation from "db.py is the only place SQL lives."

### `analysis/` — research tools, NOT part of the live trading pipeline

- **`backtest.py`** — hypothetical "what if we'd bought every qualifying
  news signal" replay for a given date, against `trading/trade_bot.py`'s own
  real `_qualifies()` gate. Never touches `positions`/`pending_candidates`.
- **`export_minute_candles.py`** — ad-hoc CSV export of Upstox's real
  per-minute OHLCV for an arbitrary date range + symbol list, plus each
  row's `prev_close`/`pct_vs_prev_close` (same convention as
  `RESULTS_TODAY_GAIN_PCT`). Useful for historical (2+ year) research since
  Upstox has no tick-level history at all, only 1-minute candles.
- **`tick_store.py`** — raw per-tick capture for today's results-calendar
  companies, for momentum-feature research (see git/conversation history:
  inferring whether intraday buying pressure is strengthening/weakening
  needs the real tick sequence, not just OHLC). `tick_store.TickStore` is
  what `trading/trade_bot.py`'s `monitor --tick` actually writes to (a
  SEPARATE, isolated TimescaleDB instance — see `analysis/TIMESCALE.md` for
  the full setup, why it's isolated from the real `investment_bot`
  database, and real gotchas verified live on this machine). An earlier
  Parquet-file-based version (`tick_capture.py`/`capture_ticks.py`) was
  removed entirely — Parquet's footer is only written on close(), so a
  long-running `--tick` session couldn't be inspected mid-day at all; that
  was the whole reason for moving to a real DB instead.

## The buy/sell rules (as of last edit — verify against the code, this doc can drift)

**Signal qualification** (before a headline/filing is even a candidate):

- `ORDER_WIN` (both NSE filings and news): **never** qualifies for a genuinely
  `large`-cap company (real top-100 by market cap, same tier definition as
  the profit/revenue thresholds below — `trade_bot._company_tier`) — real
  top-100 companies win orders constantly as part of normal business, so one
  more order win isn't a signal that moves their stock. Fully discarded, not
  downgraded to manual review: still recorded in `news_items`/`nse_filings`
  for the audit trail, just never yielded as a buy candidate at all. A
  symbol with no `cap_tier` data falls back to `sme` (i.e. NOT excluded by
  this rule) — the "unresolved company gets the stricter treatment" default
  points the other way here than it does for profit/revenue (see below),
  since being outside the large-cap exclusion is itself the stricter/harder
  outcome for this specific rule.
- NSE filings: order-win categories (that survive the large-cap exclusion
  above) → `auto` confidence; results-filing categories → `manual` (confirms
  results were filed, not the direction — that's in an unparsed PDF).
- News-sourced `ORDER_WIN` (that survives the large-cap exclusion above):
  only qualifies if the LLM judged the order size material _for that
  specific company_ (no hardcoded rupee threshold), AND the headline itself
  doesn't already claim the stock's price moved (`trade_bot._price_already_moved`
  — a real headline can literally say "Share Price Gains 5% On ... Order
  Wins", meaning the market already reacted; buying on that is buying after
  the move, not before it).
- News-sourced `PROFIT_INCREASE`/`REVENUE_GROWTH`: threshold is tiered by
  the specific company's real market-cap standing (`trade_bot._company_tier`,
  `MIN_PROFIT_PCT`/`MIN_REVENUE_PCT`):
  - **`large`** (genuinely ranked in NSE's real top 100 by market cap —
    Nifty 50 + Nifty Next 50, SEBI's actual legal large-cap definition; see
    `broker/large_cap.py`, cached in `companies.cap_tier`, refreshed ~24h
    like the rest of the instrument master): qualifies at `profit_pct >= 20`
    or `revenue_pct >= 10`.
  - **`sme`** (everything else — true SME/Emerge-platform listings,
    mid-caps, small-caps, or any main-board company outside the real top
    100; kept this name to match how the split was first specified, though
    it now covers more than literally-SME-listed companies): keeps the
    original, stricter `profit_pct >= 35` or `revenue_pct >= 20`.
  - Rationale: a given % profit/revenue move is proportionally a bigger
    deal for a smaller, less-covered company than for a real top-100 one,
    so the bar for everything outside the top 100 stays higher.
  - A symbol with no `cap_tier` data (not yet resolved/refreshed) falls back
    to the **stricter** `sme` bar, never the easier `large` one — an
    unverified company should never get the benefit of the doubt.
  - Deliberately NOT yyyyy-sourced market cap: yyyyy was removed from
    this project's live trading path on purpose (see below) and its
    fundamental-data coverage has real gaps for smaller/illiquid names
    anyway (verified live: it returned nothing for a real SME-listed
    company during evaluation). NSE's own index lists are free, need no
    such dependency, and are the actual authoritative source SEBI's
    definition is based on.
  - Auto vs. manual for this category (`trade_bot._companies_for_item`):
    clearing the tiered threshold above only makes the item a _candidate_ —
    whether it can auto-buy also depends on `pct_low_confidence`, set by
    `classifier.enrich()`. `profit_pct`/`revenue_pct` are regex-extracted
    (`classifier.extract_profit_pct_regex`/`extract_revenue_pct_regex`)
    whenever the headline states a bare percentage near the relevant keyword
    — deterministic, and overrides the LLM's own number if they disagree by
    more than 2 points (disagreement itself is treated as low-confidence,
    not silently resolved either way). This exists because the LLM's own
    extraction was verified inconsistent across identical runs (a
    clearly-stated "5%" missed 1-in-3 tries — see "Design decisions" below)
    — a _silent miss_, which asking the LLM to self-rate its own confidence
    can't catch on its own; regex is the actual fix, LLM confidence
    (`pct_confidence` in the classifier prompt, threshold 50) only covers
    headlines with no bare percentage to parse at all (e.g. "profit rises to
    Rs 120 crore from Rs 54 crore"). A `manual`-routed item isn't rejected —
    it still queues for `confirm`, same as before this distinction existed.
  - That per-field confidence is combined **asymmetrically** across
    profit_pct/revenue_pct (`classifier._item_pct_confident`), not just
    ANDed: if both are stated, the item is only confident when BOTH clear
    individually; if only `profit_pct` is stated, confidence rests on
    `profit_pct` alone; if only `revenue_pct` is stated (no profit figure at
    all), the item is **always** treated as low-confidence regardless of how
    sure regex/the LLM are about that revenue number — a revenue-only
    headline isn't trusted enough to auto-buy on by itself.

**Buy-time execution check** (right before opening a position, both sources):

1. Skip if we already bought this symbol today (open or already closed).
2. Compute today's max intraday gain so far vs. previous close.
3. Skip if that's `<= 0%` (not confirmed positive yet) or `>= 5%` (already up
   too much, likely missed the move).
4. Buy only in the `0% < gain < 5%` window — still early.

(Raised from an original `2%` ceiling to `5%` — the tighter ceiling was
rejecting real, still-tradeable moves. Real case that exposed this: a stock
with an earlier, unrelated intraday rally sitting at +4.19% cumulative gain
for the rest of the day, on top of which fresh, genuinely new order/profit
news broke — the 2% ceiling rejected it outright regardless of how fresh
the news was, since the _cumulative_ max-since-open metric doesn't
distinguish an old, already-priced-in move from a brand new one layered on
top of it. `get_intraday_gain_pct` still measures cumulative gain since
market open, not gain-since-the-news specifically, so this is a real
constraint, not fully solved by raising the number — just relaxed enough
that a single earlier same-day move up to ~5% no longer permanently locks
the stock out of qualifying for the rest of the day.)

**Sell** (via `monitor`): flat `+5%` target. Stop-loss at `-2%` applies _only_
if the stock was already "recently weak" (down ≥5% over 1d/1w/1m) at entry —
otherwise no stop-loss, target-only exit. Independent of that, every position
also carries an unconditional `hard_stop_pct` (`trade_bot.HARD_STOP_PCT`,
`-3%`, `exit_reason='hard_stop'`) — a worst-case floor that applies whether
or not the stock was "recently weak" at entry, so a target-only position
still can't ride a loss past `-3%` between now and its EOD deadline. When
both apply (a "recently weak" position has `stop_loss_pct=-2%` _and_
`hard_stop_pct=-3%`), the looser `-2%` stop-loss fires first in practice —
`hard_stop_pct` is the floor for positions that would otherwise have no
stop-loss at all. If neither target, stop-loss, nor hard stop fires, the
position is force-closed (`exit_reason='eod'`) at NSE close
(15:30 IST) **on its own entry day** — keyed off `entry_time`, not "is it
past 15:30 right now", so a position opened days ago and never checked since
closes out the next time `monitor` runs regardless of when that is, instead
of riding indefinitely. Without this, a trade that never reaches target or
stop-loss would sit open forever (verified against real historical data:
several real e2e test positions moved <2.5% for the rest of the session and
would never have resolved without this rule).

**One-day EOD grace period**: if gain at that entry-day deadline is still
"undecided" (`EOD_GRACE_BAND_LOW = -2.0` `<= gain_pct < target_pct` — hasn't
hit target, hasn't dropped badly either), the deadline extends to the
_next_ day's close instead of forcing an exit immediately. This exists
because earnings/results news routinely gets published AFTER NSE's 15:30
close (verified live: the real Bajaj Finance Q1 results headline used in
testing was published 2026-07-30 16:13 IST, after that day's close) — the
market has zero chance to react on the day of the news, so forcing an exit
at that same day's close would cut the position before the market ever got
to respond to what it was opened for. A clearly bad outcome (below
`EOD_GRACE_BAND_LOW`) still exits at the normal same-day deadline — this is
a grace period for "still plausible", not license to ride out any loss.
Known simplification: "next day" is a calendar day, not the next _trading_
day — a Friday entry's grace period lands on Saturday, not Monday. No NSE
holiday/weekend calendar exists anywhere else in this codebase either, so
this is consistent with the rest of the project's current sophistication
level, not a new gap specific to this rule.

**Third signal source — results-today price watch** (`source='results_today'`,
`trade_bot._ResultsTodayWatch`/`_open_results_today_position`): independent of
the NSE-filings/news paths above, and independent of any headline at all.
NSE's own forthcoming-board-meetings calendar (`nse_bot.results_calendar`,
cached in `db.results_calendar`, refreshed ~24h like the rest of this
project's NSE data) already lists every company scheduled to declare results
on a given date — `db.get_results_calendar_for_date(today)` is that list for
today specifically. Only wired into `monitor --tick` (not `scan` or plain
`monitor --watch`): this needs a live, continuous feed to catch the moment
price crosses the threshold, not a periodic poll. Each --tick cycle
subscribes the live WebSocket feed to the union of open-position symbols
_and_ today's results-calendar symbols (minus anything that already has an
active position, via `db.has_active_position`); a `_ResultsTodayWatch`
instance caches each candidate's previous close once (`upstox_client.
get_last_close`, refetched only when a symbol newly enters the candidate
set, not every tick) and fires the moment a live tick shows the current
price up `RESULTS_TODAY_GAIN_PCT` (`1%`) or more versus that previous close.
Deliberately does NOT reuse `_open_position`'s `MAX_ENTRY_GAIN_PCT` 0–5% gain
band: that band exists to catch a NEWS-sourced candidate that already moved
too far before the headline was even seen — here, the live gain crossing 1%
_is_ the signal itself, not a staleness filter on a separately-sourced one;
there's no "too early" state to wait out and no upper band to miss, since the
trigger only fires once, right at the cross. Every other real safety check
still applies unchanged: same-day dedup (`db.bought_today`), market-hours
check, the same recently-weak-based stop-loss, the same `+5%` target/`-3%`
hard stop/EOD-deadline exit logic, and the same atomic
`db.try_open_position` quota/capital race guard. `source_id` is `NULL` for
this source (no `news_items`/`nse_filings` row backs it) — the audit trail
is the human-readable `reason` string plus the symbol/date lookup into
`results_calendar` itself.

**Audit trail**: every `positions` row carries `source`
('news'/'nse'/'results_today'), `source_id` (the actual `news_items.id` /
`nse_filings.id` row — join to get
the full original headline/filing), `reason` (human-readable summary), and a
`transaction_id` (UUID) — "why was this trade taken" is always answerable.

## Design decisions and why (the non-obvious stuff)

- **yyyyy was removed entirely.** Upstox is the only price source. This
  was deliberate — see git/conversation history if curious, not re-litigated
  here.
- **Classification runs via the local `claude` CLI (`claude -p`), not the
  Ollama/qwen3:4b setup from earlier in the project, and not the Anthropic
  API directly.** Reuses the existing Claude Code login — no separate API
  key or billing. `classifier._call_claude()` shells out with
  `--no-session-persistence --allowedTools "" --setting-sources ""
--strict-mcp-config` so each batch call is a clean, isolated, tool-free
  request (no session files piling up, no accidental tool use, no this
  project's own CLAUDE.md/settings leaking into the classification prompt).
  Output is parsed as plain JSON text (stripping an occasional markdown code
  fence) — the CLI's `-p` mode has no equivalent to Ollama's schema-
  constrained `format` field, so array-shape is enforced by parsing/
  validation in `classify_headlines_llm_batches`, not by the model. The old
  Ollama-specific notes (explicit JSON schema to stop `qwen3:4b` degenerating
  mid-array, `think: false` to unhide its answer from a `thinking` field) no
  longer apply and were removed from here — they're `qwen3:4b`-specific
  failure modes, not relevant to Claude.
- **`instrument_master.py` includes `EQ`/`BE`/`BZ`/`SM`/`ST` series, not just
  `EQ`.** Verified against real Upstox data: excluding all but `EQ` silently
  dropped 871 real, currently-listed companies (e.g. MTAR Technologies is
  series `BE`). The many `N`-/`Y`-/`Z`-series codes are bonds/NCDs, correctly
  excluded. If this filter looks over-broad, it isn't — it was under-broad
  before and caused a real wrong-ticker bug.
- **The ISIN prefix filter (`INE` only) matters too**: `INF`-prefixed
  "equities" in Upstox's feed are mutual fund/ETF units (e.g. "BIRLASLAMC -
  TECH" with trading symbol literally `TECH`) that would otherwise collide
  with ordinary English words during free-text company scanning.
- **The LLM extracts company _names_ as free text; it never gets to name a
  ticker.** Every extracted name is grounded through
  `company_lookup.resolve_company()` against the real, verified company list.
  This fixed real bugs a pure substring/token scanner had: "Bank of India"
  matching inside "Union Bank of India" (containment), and ordinary words
  like "TOTAL"/"WEALTH"/"RETAIL" matching real-but-irrelevant tickers (word
  collision). The old `company_lookup.find_mentions()` scanner still exists
  and works, just isn't called by `classifier.enrich()` anymore.
- **`profit_pct`/`revenue_pct`/`order_is_material` extraction is
  LLM-judgment, not deterministic.** Verified inconsistent across identical
  runs (a clearly-stated "5%" missed 1-in-3 tries; materiality judgment
  flipped between runs for the same headline). Accepted as a known risk since
  this is paper trading — if this project ever handles real capital, revisit
  this (e.g., regex-extract the actual percentage instead of trusting the
  LLM's number).
- **`classifier.enrich()` takes an `on_batch` callback and processes
  incrementally, not all-at-once.** A cold-start/large backlog can be
  ~1,500+ headlines (~12+ minutes at ~15s/batch of 30) — without incremental
  persistence, killing the process mid-run (or it crashing) loses everything
  classified so far. `trade_bot.py` and `news_bot.py` both pass `on_batch` to
  persist + log progress (`[classify] N/total headlines processed`) after
  every batch.
- **`upstox_feed.py`'s authorize endpoint is `/v3/...`, not `/v2/...`.**
  Verified live: v2 returns `HTTP 410 Gone` with an error message explicitly
  saying to use v3. Upstox's API has moved versions before; if something
  suddenly 404s/410s, check their current docs before assuming the code is
  wrong.
- **Money amounts and instrument-key resolution use regex/exact-match, not
  the LLM** — deterministic parsing where determinism is achievable and
  cheap, LLM only where judgment is genuinely needed (event classification,
  entity extraction, materiality).

## What's in Postgres vs. what's still a local file

**In Postgres** (`db.py`): `news_items`, `news_item_companies`, `nse_filings`,
`positions`, `pending_candidates`, `companies` (Upstox instrument master).
This is the actual source of truth for everything the bot has seen and done.

**Still local files** (deliberately, not an oversight):

- `upstox_secrets.json`, `.upstox_token.json` — credentials/tokens. Never
  belong in a database that might get inspected/shared/backed up more widely.
- `.nse_seen.json` — only used by `nse_bot.py` when run _standalone_ (outside
  `trade_bot.py`) to dedupe its own CSV report output. `trade_bot.py`'s own
  NSE dedup goes through `db.is_nse_seen()`, independent of this file.

## Running it

**Default to running continuously.** When starting `scan` or `monitor` for
the user (not just checking status), use the long-running forms — `scan
--watch` and `monitor --tick` — in the background, not the one-shot forms,
unless the user specifically asks for a single one-shot run.

```bash
# one-time per day (tokens expire ~3:30am) -- .upstox_token.json is read
# fresh off disk on every single API call (no in-memory caching anywhere),
# so re-running this mid-session to replace a dead/expired token is picked
# up immediately by every already-running scan/monitor/UpstoxFeed process,
# no restart needed
./.venv/bin/python broker/upstox_client.py login

# `upstox_secrets.json`'s redirect_uri is `https://127.0.0.1` (no port), so
# login()'s auto-capture path never applies here -- it always falls to the
# manual-paste `code` prompt. If a Claude Code session needs a fresh token
# (e.g. before running analysis/backtest.py against real prices), it can
# drive this without blocking on that prompt: build the URL itself
# (upstox_client.build_login_url()) and print it instead of running the
# blocking `login` command directly, ask the user to open it, log in, and
# paste back the `code=...` value from the (broken, that's expected --
# nothing listens on 127.0.0.1) redirect URL's address bar, then finish
# with `broker/upstox_client.py login --code <that value>` -- this both
# exchanges the code AND caches the token in one step, non-interactively.

# see what's going on
./.venv/bin/python trading/trade_bot.py status

# view/change the persisted knobs (max_trades_per_day, capital_per_trade,
# max_capital_allowed -- see db.py's `config` table)
./.venv/bin/python trading/trade_bot.py config show
./.venv/bin/python trading/trade_bot.py config set max_trades_per_day 5

# pull signals; auto-open qualifying ones (or, if the market's closed right
# now -- weekend or weekday outside 09:15-15:30 IST, see trade_bot._market_is_closed
# -- queue an AMO-style buy-limit order instead, filled automatically once the
# market reopens); queue the rest for manual review
./.venv/bin/python trading/trade_bot.py scan            # one-shot
./.venv/bin/python trading/trade_bot.py scan --watch     # every 5 min (default), forever

# review the manual queue (earnings-narrative signals, results filings)
./.venv/bin/python trading/trade_bot.py confirm

# check/close open positions against live prices -- --tick ALSO watches
# (and can open) today's results-calendar companies for the +1%-vs-previous-
# close signal (see CLAUDE.md's "results-today price watch" section);
# --watch is close-only, it never opens a new position on this signal
./.venv/bin/python trading/trade_bot.py monitor --tick        # react to every live WebSocket
                                                               # tick instead of polling on a
                                                               # timer -- runs forever, stays
                                                               # alive with zero positions open,
                                                               # auto-reconnects on any drop
./.venv/bin/python trading/trade_bot.py monitor --watch 60    # or: poll every N seconds instead

# standalone digest/backfill -- fetches, classifies, and stores news, but
# NEVER calls trade_bot.py's _qualifies()/_decide()/db.add_pending(): a
# headline pulled this way can resolve to a real company and still never get
# a chance to auto-open or queue (news_items.via_backfill marks these rows so
# stocki's dashboard doesn't call that "pending review")
./.venv/bin/python news/news_bot.py --days 2

# monitoring dashboard -- read-only, never touches Upstox or writes to the DB
# (see stocki/README.md); open http://localhost:5173 once both are running
cd stocki/server && npm run dev   # backend, port 4000
cd stocki/web && npm run dev      # frontend, port 5173
```

**Checking logs**: every `trade_bot.py` invocation (one-shot, `--watch`, or
`--tick` alike) tees its output to its own timestamped file under `logs/`
(`<command>_<started-at>.log`) and prints that path at startup — `tail -f
logs/<that-file>.log` to follow a run live. A one-shot command's log file
stops growing the moment the process exits (nothing more is coming); a
`--watch`/`--tick` session keeps appending for as long as it runs. `scan`/
`monitor` also each leave a row in `scan_runs` (db.py) for pipeline-health
auditing independent of the log files.

To tail the most recent log for a given command, use `ls -t logs/ | grep
<command>` to find the latest timestamped file, then:

```bash
tail -f /Users/ashishsaini/workspace/investment/logs/scan_{timestamp}.log
tail -f /Users/ashishsaini/workspace/investment/logs/monitor_{timestamp}.log
```

## Known untested / open items (as of last edit)

- The full merged `scan` pipeline (NSE + news + qualifying filters +
  buy-time gain-band check, end to end against a live Upstox account) hasn't
  completed a full run yet — was mid-verification when this doc was written.
  Check `git log` / conversation history for the latest status, or just run
  `trading/trade_bot.py scan` and see what happens.
- Automated tests exist under `tests/` (pytest, 77 as of last edit):
  `test_classifier.py` (LLM classification + company grounding — makes a
  real `claude` CLI call, skips cleanly if it's unavailable),
  `test_trade_config.py` (config table, quota/capital locking, off-hours
  queuing, live-vs-schedule market-status check — runs against a throwaway
  `investment_bot_test` database, created automatically, never the real
  one), `test_upstox_client.py`/`test_upstox_feed.py` (network-failure
  handling, tick callbacks, auto-reconnect — no live network needed). Run
  with `./.venv/bin/python -m pytest tests/ -v`.
- `broker/upstox_client.py status` only checks whether `.upstox_token.json`'s
  `issued` date is today's date — it does NOT make a real API call, so it can
  report "valid" for a token Upstox has actually already invalidated
  server-side (verified live: this happened mid-session). Don't trust it as
  proof a token actually still works; `trade_bot._market_is_closed()`'s
  live-status check (and every other real API call) will surface the real
  401 if so.
- **TODO, relevant once real order placement is added**: right now "opening a
  position" (`db.open_position`/`try_open_position`/`try_fill_queued_position`)
  just means a `positions` row was inserted with `status='open'` and its
  `capital` counted by `get_open_capital()` — it does NOT mean an order was
  actually placed on NSE, let alone filled. That's fine for pure paper
  trading (nothing to reconcile against), but the moment real order
  placement is wired in, a placed-but-rejected/failed order must NOT keep
  counting its `capital` against `max_capital_allowed` — that capital needs
  to be freed back up (e.g. close the position immediately with a distinct
  `exit_reason` like `'order_rejected'`, or don't mark it 'open' until NSE
  actually confirms the fill) instead of silently sitting there as phantom
  deployed capital that was never really at risk.
