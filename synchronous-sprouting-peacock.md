# Two workstreams: prod-readiness fixes, then per-symbol manual review

**Part A** is a set of small, independent production-readiness fixes — do
these first, they stand alone. **Part B** is the multi-company manual-review
feature, planned but deferred ("we'll discuss and implement later").

---

# Part A — production-readiness fixes

Came out of a "are we good for prod?" review. Each item below is derived from
code or live process state, not from table contents (the database holds
testing data and its row counts aren't trustworthy evidence).

Already done, no code change: both duplicate `scan --watch` loops (PIDs 66374
and 71939) were killed. They were doubling Google News RSS query volume and
`claude` CLI classification calls. `monitor --tick` left running.

## A1. `cmd_confirm` can open a duplicate position — fix

`trade_bot.py confirm` is a live documented command, and it calls
`_open_position` without the guard `_decide` uses. `_open_position`'s only
duplicate check is `db.bought_today()`, which (db.py:597-611) matches on
`entry_time::date = today` and **excludes `status='queued'`**. So it misses:

- a position opened on a **previous day** and still `open`
- an off-hours **`queued`** limit order still outstanding

`_decide` (:725) is protected because it checks `c["symbol"] in
state.open_symbols` from `db.get_open_symbols()` first. `cmd_confirm` has no
equivalent.

Fix: add `db.has_active_position(symbol)` → `status IN ('open','queued')`,
and check it in `cmd_confirm`'s loop before calling `_open_position`. Skip
with a clear message and leave the candidate pending rather than resolving
it — nothing was bought.

This is the same helper Part B's `approve` path needs, so build it here and
Part B reuses it.

## A2. `monitor --tick` always looks dead to the dashboard — fix

`HealthStrip.tsx` treats a run as degraded when `finished_at IS NULL` and
`started_at` is older than `STALE_RUNNING_MS` (10 minutes). A `monitor
--tick` session is designed to run for days and only writes `finished_at` on
exit, so it reads "still running, looks stuck" **permanently**. The one
signal meant to distinguish a healthy long-running watcher from a crashed one
is therefore useless for exactly the command most likely to die unnoticed.

Fix: add a heartbeat.

```sql
ALTER TABLE scan_runs ADD COLUMN IF NOT EXISTS last_heartbeat_at TIMESTAMPTZ;
```

- `db.heartbeat_run(run_id)` — one cheap UPDATE.
- Call it from the `--tick` loop and the `--watch` loops each cycle (and
  `monitor --tick` should beat even when idle, since staying alive with zero
  open positions is its normal state).
- `HealthStrip`: a run is healthy while `last_heartbeat_at` is recent;
  degraded only when the heartbeat has gone stale or `error` is set. Keep the
  existing `started_at` rule as the fallback for rows that predate the column
  (`last_heartbeat_at IS NULL`).

## A3. A transient DNS blip kills the whole scan — fix

The 6 recorded scan errors were all `<urlopen error [Errno 8] nodename nor
servname provided>`. Root cause is a single unprotected call:
`nse_bot.fetch_announcements()` (news/nse_bot.py:128-133) does a bare
`urllib.request.urlopen(req, timeout=20)` with no retry, and it is the very
first statement in `_nse_candidates()`. `cmd_scan` calls that generator
outside any network try/except, so the exception escapes and aborts the run —
taking the news path down with it even though news fetching is individually
protected (`_news_candidates` already wraps every query in `try/except
Exception` and logs `[warn] news query '...' failed`).

Two-part fix, mirroring conventions already in the file:

1. A small retry helper (2–3 attempts, short backoff) around the two
   `urlopen` calls in `nse_bot.py` (`fetch_announcements`,
   `fetch_board_meetings`) so a momentary DNS/network failure retries instead
   of failing the cycle.
2. Make an NSE fetch failure **non-fatal to the rest of the scan**: catch it
   in `cmd_scan` around the `_nse_candidates` loop, print the same `[warn]
   Skipping NSE-based signals this run: ...` shape already used for the news
   path, and continue to news. A dead NSE feed should degrade the scan, not
   end it.

## A4. Upstox token can be silently dead — surface it in the UI

`broker/upstox_client.py status` only compares the token's `issued` date to
today and makes no API call, so it reports "valid" for a token Upstox has
already invalidated server-side (already in CLAUDE.md's known issues).
Meanwhile `_market_is_closed()` (:152-167) catches the resulting
`RuntimeError` and **falls back to the fixed trading-hours schedule** — so
the bot keeps running with a dead token and nothing anywhere says so. Tokens
expire ~3:30 AM daily, and a `--watch` loop started before that keeps going.

`_market_is_closed()` is also the ideal hook: it already makes a real Upstox
API call (`is_market_open`) every cycle and already has both success and
failure branches.

- New tiny table, written by Python only:
  ```sql
  CREATE TABLE IF NOT EXISTS system_status (
      key TEXT PRIMARY KEY,
      ok BOOLEAN NOT NULL,
      detail TEXT,
      checked_at TIMESTAMPTZ NOT NULL DEFAULT now()
  );
  ```
  `db.record_system_status('upstox_token', ok, detail)` — upsert on `key`.
- Call it from both branches of `_market_is_closed()`: `ok=True` on a
  successful `is_market_open`, `ok=False` with the error text on the
  `RuntimeError` fallback. This keeps stocki entirely away from Upstox, which
  is the existing architectural rule.
- `getHealth` returns the `system_status` rows alongside `latest`/`recent`.
- `HealthStrip` renders a prominent banner when `upstox_token` is not ok,
  including how long it's been failing and the remedy
  (`./.venv/bin/python broker/upstox_client.py login`). Also flag a *stale*
  check (`checked_at` old) — that means nothing is running at all.

## A5. Deferred — TODO comments only, no behavior change

Add clearly-worded TODOs at the relevant code, no fix now:

- **Off-hours queued orders never expire.** `db.get_queued_positions()`
  (db.py:723-726) has no age filter and `_process_queued_orders` never
  expires one, so a queued order rides forever against a limit price anchored
  to the close of the day it was queued. Note *why* it matters in the TODO:
  `_queue_offhours_order`'s docstring says the limit ceiling "stands in for
  the 0-5% gain-band check," which only holds on the next open — making this
  the one path into `positions` with no freshness gate at all
  (`NEWS_MAX_AGE`, the gain band and `_price_already_moved` are all
  bypassed). Intended fix: expire at the end of the next session, like a real
  AMO. Put the TODO on both `get_queued_positions` and
  `_process_queued_orders`.
- **No NSE holiday calendar.** `_next_trading_day_open` (:170-179) and the
  EOD grace period only skip Saturday/Sunday. Both already carry notes; add
  an explicit shared TODO so it's tracked as one item rather than two asides.

## Part A verification

1. `./.venv/bin/python db.py init` twice — idempotent; confirm
   `scan_runs.last_heartbeat_at` and the `system_status` table exist.
2. `./.venv/bin/python -m pytest tests/ -v` (deselect the known-flaky
   `test_classifier.py::test_profit_above_threshold_qualifies_manual`).
3. **A1**: insert a `positions` row that is (a) open from a previous day and
   (b) separately, `queued`; run `trade_bot.py confirm` against a pending
   candidate for that symbol and assert no new row is created in either case.
4. **A2**: start `monitor --tick`, confirm `last_heartbeat_at` advances each
   cycle with zero open positions, and confirm the dashboard shows it healthy
   past the 10-minute mark. Then `kill -9` it and confirm it flips to
   degraded once the heartbeat goes stale.
5. **A3**: simulate DNS failure (point the NSE host at a black hole via
   `/etc/hosts`, or temporarily override the URL) and confirm the scan logs a
   warning, still runs the news path, and finishes with no `error` on the
   `scan_runs` row.
6. **A4**: move `.upstox_token.json` aside (or corrupt it), run a scan, and
   confirm `system_status` flips to not-ok and the dashboard shows the
   banner; restore the token and confirm it clears. Note the token is
   currently live — verified with a real `get_current_price('RELIANCE')`
   call, not `upstox_client.py status`.
7. Confirm `trade_bot.py status`, a normal `scan`, and `confirm` are
   otherwise unchanged.

---

# Part B — per-symbol manual review for multi-company news headlines

*Deferred. Planned in full below; implement after Part A.*

## Context

Today a single news headline that mentions several companies auto-buys every
symbol that clears the threshold, and auto-marks every one of them as
"results found" in `results_calendar`. That is exactly the shape of article
that produced the false positives fixed earlier in this session — the
Havells/Aarti Surfactants roundup ("Q3 Results impact: Havells India shares
drop 7%...; Aarti Surfactants, Tips Music...") named a company that the
article was not actually about. Clause-based matching fixed the specific
symptom, but the underlying ambiguity remains: when one headline names many
companies, no amount of string matching reliably answers *which company this
news is really about*.

So: when a headline resolves to more than one company, stop deciding
automatically and ask a human — per symbol, not per article. Single-company
headlines keep today's fully-automatic behavior unchanged.

The review happens in the stocki dashboard (expandable row, Approve/Reject
per symbol) because that is where the signal feed already lives. Approving
runs the symbol through the real, existing buy path in Python — the same
`_qualifies()` tier thresholds, the same 0–5% intraday gain band, the same
quota and capital caps — plus a strict re-check that the article is still
within `NEWS_MAX_AGE` (1 hour). Rejecting is just a recorded decision.

### Two architectural rules this deliberately breaks

Both are currently stated as absolutes and both must be amended, not quietly
violated:

- `stocki/README.md` lists "any write action (e.g. confirming a pending
  candidate)" as **out of scope by design**. This feature is that write
  action.
- CLAUDE.md's "SQL lives in two places, each owning one direction: `db.py`
  writes, `queries.ts` reads." Reject becomes a write in `queries.ts`.

The rule becomes: `queries.ts` owns dashboard reads plus the single
review-state write; **every trading decision and every position write stays
in Python**, reached by shelling out to `trade_bot.py`. Node never talks to
Upstox and never opens a position itself.

### Decisions already made (do not re-litigate)

| Decision | Choice |
|---|---|
| Approve execution | Express `execFile`s `trade_bot.py approve` and returns its result |
| Review state storage | New columns on `news_item_companies`; `pending_candidates` untouched |
| Freshness at approve | Strict 1 hour via existing `_is_fresh_enough()`; UI shows a countdown |
| `results_calendar` | Multi-company articles mark it only on approval |

**Accepted consequence of the strict 1-hour rule:** a reviewer who doesn't
click within an hour of publication gets `expired` and no trade. Many rows
will expire unactioned. This is chosen, not overlooked — the countdown and a
visible expired state exist so it's never a surprise.

## Blocking prerequisite found during planning

`_qualifies()` reads `profit_pct`, `revenue_pct` and `order_is_material` —
**none of which are persisted.** Verified against the live schema:
`news_items` has only `title, link, source, pub_date, matched_query, money,
event_type, category, signal, first_seen_at, user, via_backfill`.

At approve time those would all read back as `None`/`False`, so `_qualifies()`
would return `False` for every candidate and the feature would never work.
Three additive columns fix it. Rows created before this change read back
`NULL` and simply can't be approved — correct, since we genuinely don't know
their numbers.

## Implementation

### 1. `db.py` — schema and helpers

Schema additions, following the existing idempotent
`ALTER TABLE ... ADD COLUMN IF NOT EXISTS` convention:

```sql
-- the blocking prerequisite above
ALTER TABLE news_items ADD COLUMN IF NOT EXISTS profit_pct NUMERIC;
ALTER TABLE news_items ADD COLUMN IF NOT EXISTS revenue_pct NUMERIC;
ALTER TABLE news_items ADD COLUMN IF NOT EXISTS order_is_material BOOLEAN;

-- per-symbol review state, at the grain news_item_companies already has
ALTER TABLE news_item_companies ADD COLUMN IF NOT EXISTS
    review_status TEXT NOT NULL DEFAULT 'not_required';   -- |pending|approved|rejected
ALTER TABLE news_item_companies ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMPTZ;
ALTER TABLE news_item_companies ADD COLUMN IF NOT EXISTS review_note TEXT;
```

`NOT NULL DEFAULT 'not_required'` backfills all 289 existing rows in one
statement and leaves `getSignals`' outcome precedence untouched — nothing is
`'pending'` until a new multi-company article arrives.

Real FK on `symbol`, using the same `DO $$ ... EXCEPTION WHEN
duplicate_object THEN NULL; END $$;` idiom already used for the two
`results_calendar` FKs (db.py:281-297):

```sql
ALTER TABLE news_item_companies
  ADD CONSTRAINT news_item_companies_symbol_fkey
  FOREIGN KEY (symbol) REFERENCES companies(symbol);
```

Verified safe: 289 rows, **0 orphans**, and there is no `DELETE FROM
companies` anywhere in the repo (`upsert_companies` is INSERT/ON CONFLICT
UPDATE only), so this can never block an instrument-master refresh. No
`ON DELETE CASCADE` — silently deleting audit rows is worse than an error.

`insert_news_item` (db.py:463) changes:
- persist the three new classifier fields
- set `review_status` per company row:
  `'pending'` when the item resolved **more than one** company **and** isn't a
  backfill; `'not_required'` otherwise. The `via_backfill` gate matters —
  `news_bot.py` also calls this helper, and a backfilled article never went
  through `_qualifies`/`_decide`, so it must not become approvable.

"More than one company" means `len(mentioned_companies) > 1` — the resolution
count, not the count surviving `_qualifies()`. Deliberate: the ambiguity being
mitigated is *which company the news is about*, which qualification doesn't
resolve. Note the behavior change — a roundup where only one company clears
the tier bar used to auto-buy and now needs a click.

New helpers alongside the existing groups:
- `get_news_item(news_item_id)`, `get_news_item_companies(news_item_id)`,
  `count_news_item_companies(news_item_id)`
- `set_news_company_review(news_item_id, symbol, status, note=None)` — a
  compare-and-swap (`WHERE review_status = 'pending' RETURNING id`) so a
  double-click can't be processed twice.
- `has_active_position(symbol)` → `status IN ('open', 'queued')` — **already
  added by Part A1**; reuse it, don't redefine. See the duplicate-position
  gap below for why neither existing helper covers this.

Add nothing to `pending_candidates`.

### 2. `trading/trade_bot.py`

**Suppress the auto path for multi-company items** in `_persist_batch`
(:494) — skip the `on_candidate` call entirely rather than downgrading
confidence to `"manual"`. Downgrading would fall through `_decide` into
`db.add_pending()`, putting the item in `pending_candidates`, which this
design explicitly leaves alone.

`state.seen_symbols` is intentionally left alone: a later single-company
article about the same symbol should still auto-evaluate in the same scan.
Note in passing that `scan_runs.signals_seen` will read lower for
multi-company articles.

**Fix the results-calendar loop** (~:562) so a multi-company article no
longer marks the calendar — but **filter, don't skip**: reduce `matches` to
single-company items and take the earliest of *those*. (§5 additionally has
this loop record *why* it suppressed, so the dashboard can surface it.) Abandoning the whole
entry would let an unrelated roundup published ten minutes earlier block a
perfectly good single-company article from marking it.

Critically, compute the company count from the **database**, not
`len(it["mentioned_companies"])`. That loop iterates `all_items`, which
includes already-seen duplicates from prior scans; those come from
`news_bot.parse_items`, which — verified — produces dicts with only
`title/link/source/pub_date/matched_query` and **no `mentioned_companies`
key at all**. Indexing it would raise `KeyError` and kill the scan. Resolve
the id via the existing `it.get("_news_item_id") or
db.get_news_item_id_by_title(...)` fallback, then
`db.count_news_item_companies(...)`.

**New `cmd_approve(args)`** — must reproduce the *whole* gate, not just
`_qualifies()`. `_persist_batch` checks `signal == "positive"` before
`_companies_for_item`, and `_companies_for_item` itself gates on
`category in ("order", "earnings")`; calling `_qualifies()` alone would buy
items `scan` would never have yielded. Order:

1. load the news item + its companies; refuse unless this symbol's row is
   `review_status = 'pending'`
2. refuse unless `signal == 'positive'`
3. **refuse if `db.has_active_position(symbol)`** — see below
4. refuse unless `_is_fresh_enough(it)` — reuse as-is; it already handles the
   weekend shift and needs only `pub_date`
5. `_companies_for_item(it)`; refuse if this symbol is no longer in it
6. `_open_position(...)` with the reconstructed candidate dict
7. **only if that returned truthy**, flip to `'approved'` and mark the
   calendar

Step 7's ordering mirrors `cmd_confirm` (:826). Approving first would strand
rows as "approved but nothing bought" whenever the buy is refused for a
transient reason (no token, no intraday data yet).

#### Step 3: approving must never open a second position for a symbol

`_open_position`'s own `db.bought_today()` guard is **not sufficient here**,
and this is the one genuinely new hole this feature opens. Verified against
the real implementations (db.py:597 and db.py:773):

| Situation | `bought_today()` | `get_open_symbols()` | guarded today? |
|---|---|---|---|
| Opened today, still open | yes | yes | yes |
| Opened today, already closed | yes | no | yes |
| **Opened days ago, still open** | **no** (`entry_time::date` isn't today) | yes | **no** |
| **Off-hours limit order still queued** | **no** (explicitly excludes `status='queued'`) | **no** (only `'open'`) | **no** |

The scan path never hits those last two because `_decide` (:725) checks
`c["symbol"] in state.open_symbols` *before* calling `_open_position`, using
`db.get_open_symbols()`. `cmd_approve` bypasses `_decide` entirely, so
without an explicit check an approval could open a **duplicate position** on
a symbol that already has one — deploying capital twice on the same signal
and double-counting it against `max_capital_allowed`.

Hence `db.has_active_position(symbol)` covering `status IN ('open',
'queued')`, checked in `cmd_approve` before any Upstox call. This is strictly
wider than `get_open_symbols()` because it also catches a queued off-hours
order — otherwise approving the same symbol from two articles while the
market is shut would stack two resting buy-limits.

Refuse with `already_positioned` (distinct from `already_bought`, which
remains `_open_position`'s same-day guard), record the reason in
`review_note`, and leave the row `pending` rather than marking it approved —
nothing was bought, so claiming otherwise would corrupt the audit trail. The
UI keeps Reject available so the reviewer can close it out deliberately.

`cmd_confirm` had this same gap; it's fixed in **Part A1**, which is where
`has_active_position` comes from.

Two more cases to handle explicitly:
- `_open_position` returns `"queued"` outside market hours (an AMO-style
  buy-limit, not a fill). That is a legitimate success and the UI must show
  it differently from `"opened"`.
- Same symbol pending in two different articles, *nothing yet bought*: the
  first approve wins; the second is caught by step 3 (or by `bought_today`
  if it closed the same day) and refuses.

Cast the new `NUMERIC` columns to `float` on load — psycopg2 returns
`Decimal`.

**New `_mark_results_calendar_on_approve(it, symbol)`** — recompute the match
against the persisted row (`title`, `pub_date`, `matched_query` are all
there) reusing `_item_matches_calendar_entry` and `_RESULTS_CONTEXT_RE`, then
`db.mark_results_news_found(...)`. Call `db.get_pending_results_calendar_news`
**directly**, never `_results_calendar_pending()` — the latter triggers
`nse_bot.refresh_results_calendar()`, a live NSE fetch that must not sit
inside a web-triggered request path.

**Result contract for Node.** stdout is not clean JSON: `_start_logging`
tees output and prints a banner, and `_open_position` prints several lines.
Emit a sentinel final line instead:

```
@@APPROVE_RESULT@@{"status":"opened","symbol":"CDSL","message":"..."}
```

`status` ∈ `opened | queued | refused | error`; `refused` carries a `reason`
(`expired`, `no_longer_qualifies`, `not_a_positive_signal`,
`already_positioned`, `already_bought`, `quota`, `capital`,
`no_intraday_data`, `not_pending`). **Exit 0 whenever a
valid result line was emitted, including refusals** — non-zero only on an
unhandled crash. The UI keys off `status`, never the exit code.

Register `p_approve` in `main()` (:1129) with `--news-item-id` and
`--symbol`; `_load_config_defaults(args)` already runs for every non-`config`
command, so the capital/quota args populate for free.

### 3. `stocki/server` — the first write path

`queries.ts` `getSignals` (:184) gains a second aggregate. It **must** keep
one row per news item — the comment at :184-190 records that finer grouping
previously produced duplicate ids and broke a React key:

```sql
COALESCE(jsonb_agg(DISTINCT jsonb_build_object(
  'symbol', nc.symbol, 'name', nc.name, 'review_status', nc.review_status,
  'has_active_position', ap.symbol IS NOT NULL
)) FILTER (WHERE nc.symbol IS NOT NULL), '[]') AS companies
```

with a `LEFT JOIN (SELECT DISTINCT symbol FROM positions WHERE status IN
('open','queued') AND "user" = $1) ap ON ap.symbol = nc.symbol`. This is what
lets the UI grey out Approve for a symbol that already holds a position,
instead of the reviewer only finding out from a refusal after clicking.
Python still re-checks — the UI hint can be up to one poll stale.

`jsonb_*` not `json_*` — `DISTINCT` over `json` fails with "could not
identify an equality operator for type json". `DISTINCT` is genuinely needed
despite `UNIQUE(news_item_id, symbol)` because the `positions` and
`pending_candidates` joins multiply rows.

Add `WHEN bool_or(nc.review_status = 'pending') THEN 'needs review'`
immediately above the final `ELSE 'pending review'`. The single badge is
inherently lossy for a partially-reviewed row — the expanded row is the
per-symbol truth; comment it.

New `rejectNewsCompany(...)` — the first write in this file; amend its header
comment.

`routes.ts` gets two POSTs, plus a small `approve.ts` for the subprocess:

```
POST /signals/:newsItemId/companies/:symbol/approve
POST /signals/:newsItemId/companies/:symbol/reject
```

Path params, not a JSON body — so `index.ts` needs no `express.json()` and
stays unchanged. Three real hazards to get right in `approve.ts`:

- **cwd**: the server runs from `stocki/server`, but the script path and
  `_start_logging`'s `logs/` are repo-root-relative. Resolve the root
  explicitly and pass absolute paths for both interpreter and script, with
  `cwd` set to the root.
- **no shell**: `execFile` with an argv array, never `exec`. Additionally
  validate at the route (`Number.isInteger(id) && id > 0`,
  `/^[A-Z0-9&.\-]{1,20}$/` on the symbol) so bad input is a 400, not a Python
  traceback.
- **timeout**: approve makes live Upstox REST calls and can hang; set an
  explicit `timeout` and `maxBuffer`.

Note for the record: there is no auth on this server. After this change an
unauthenticated localhost request can trigger a (paper) trade. Acceptable for
a local single-user dashboard, but now a deliberate choice.

### 4. `stocki/web` — expandable review row

- `api.ts`: add a `post<T>` helper mirroring `get<T>`; extend `Signal` with
  `companies: { symbol; name; review_status }[]`; add
  `approveCompany`/`rejectCompany`.
- `signalRows.ts`: carry `companies` and a derived `needsReview` onto
  `SignalRow` (filings get `[]`/`false`); map `'needs review'` → `warn` in
  `badgeClass`.
- `SignalFeed.tsx`: expandable rows copying the existing precedent in
  `PositionsTable.tsx:102-161` (`useState<Set<string>>` + `Fragment` +
  the `expand-toggle` / `expanded-detail` classes already in `index.css`),
  keyed on the prefixed `r.id`. Each expanded line shows symbol + name, a
  countdown to `publishedAt + 1h`, and Approve/Reject. First `useMutation` /
  `useQueryClient` in the app — invalidate `signals`, `filings`, `pending`
  and `summary`.

Three details that matter:
- A symbol whose `has_active_position` is true renders with Approve disabled
  and a "position already open" note — Reject stays enabled.
- Drive the expand affordance off
  `companies.some(c => c.review_status === 'pending')`, **not** off
  `outcome` — otherwise a partially-approved row (outcome `traded`) loses the
  Approve buttons for its remaining symbols.
- The countdown is **advisory only**. Don't reimplement the weekend shift in
  TypeScript; Python is authoritative. Make sure an `expired` refusal renders
  cleanly even when the client timer still shows time left (clock skew, and
  `db.now()`'s mock clock in dev).

### 5. Results calendar widget — Automatic / Manual intervention sections

Split the Results calendar widget into two labelled sections so it's
immediately obvious which declarations the bot handled by itself and which
are stuck waiting on a human. Applies to both tabs, but matters most on
Live's "today" view — that's where a blocked entry is still actionable.

- **Manual intervention** — a matching article was found, but marking was
  suppressed because that article names several companies, so this symbol
  needs per-symbol approval. These are the rows a human can actually unblock.
- **Automatic** — everything else: resolved without help, or still awaiting
  declaration with nothing to act on.

**How an entry lands in "Manual intervention".** Do *not* try to recompute
the match in SQL — the matching rules (`_item_matches_calendar_entry`, the
clause-splitting, `_RESULTS_CONTEXT_RE`, the normalisation) live in Python
and exist precisely because they were hard to get right. Duplicating them in
`queries.ts` would guarantee the two drift.

Instead, have the scan **record the suppression** when it happens. One more
column, same idempotent convention, with a real FK like the two the table
already has:

```sql
ALTER TABLE results_calendar ADD COLUMN IF NOT EXISTS
    pending_review_news_item_id INTEGER REFERENCES news_items(id);
```

In the results-calendar loop (§2), when the matches for an entry are
*all* multi-company — i.e. filtering to single-company items leaves nothing —
write the earliest such article's id here instead of calling
`db.mark_results_news_found`. A later single-company article still marks the
entry normally; `mark_results_news_found` should clear this column at the
same time, so an entry is never in both states.

`getResultsCalendar` then splits on real stored state, no logic duplication:

```sql
CASE WHEN earliest_news_at IS NULL AND pending_review_news_item_id IS NOT NULL
     THEN 'manual' ELSE 'automatic' END AS section
```

plus `pending_review_news_item_id` in the select list so the UI can link
through to the article.

`ResultsCalendar.tsx` renders two `<tbody>` groups (or two tables) under
`Automatic` / `Manual intervention` headings, keeping the existing columns,
`statusBadgeClass`, and expand behaviour unchanged. Manual rows additionally
expand to the blocking article with the same per-symbol Approve/Reject
controls as `SignalFeed` — extract that into a shared component rather than
copying it, since both places now render the identical thing. Hide the
"Manual intervention" heading entirely when that group is empty, so the
normal case still reads as one clean table.

Add a matching `status` value (`'needs review'`) so the badge tells the same
story as the section, and reuse `badgeClass`'s `warn`.

### 6. Docs

`stocki/README.md` (:75) and `CLAUDE.md`: the "read-only / no write action"
and "`db.py` writes, `queries.ts` reads" statements both become false —
update them, and document the multi-company review rule alongside the
existing buy/sell rules.

## Part B verification

1. `./.venv/bin/python db.py init` twice — idempotent; confirm via `\d
   news_item_companies` that the FK and three columns registered, and that
   all 289 pre-existing rows read `not_required`.
2. `./.venv/bin/python -m pytest tests/ -v` (deselect the known-flaky
   `test_classifier.py::test_profit_above_threshold_qualifies_manual`).
3. **Multi-company suppression, against real data**: the DB already holds 12
   multi-company news items. Re-run the scan path and confirm none of them
   auto-open a position or mark `results_calendar`, while a single-company
   item (e.g. the DIVISLAB article, id 4211) still does both exactly as today.
4. **`approve` CLI directly**, before any UI exists — cover `opened`,
   `expired` (an old article), `no_longer_qualifies`, `not_pending`, and
   `already_bought`. Assert the sentinel line parses and the exit code is 0
   for refusals.
5. **No duplicate position, the specific gap this plan closes.** Three cases,
   each asserted to leave `SELECT count(*) FROM positions WHERE symbol = ...`
   unchanged and to return `refused / already_positioned`:
   a. a position opened **on a previous day** and still `open` — the case
      `bought_today()` misses;
   b. an off-hours **`queued`** limit order outstanding — the case both
      existing helpers miss;
   c. the same symbol pending in two different articles, approved twice in a
      row — the second must refuse.
   Set these up directly in `positions` (or via a mock-server off-hours
   scan for (b)) rather than waiting for them to occur naturally.
6. **Reject** via `curl -X POST`; confirm the row flips and no position is
   created.
7. **Approve via `curl`** from a cwd *other than* the repo root, to prove the
   path resolution is right and `logs/` doesn't get scattered into
   `stocki/server`.
8. **Results calendar sections**: force the manual case by pointing a
   calendar entry at one of the 12 known multi-company articles, then confirm
   it renders under **Manual intervention** with the blocking article
   reachable, while untouched entries stay under **Automatic**. Approve it
   and confirm it moves to Automatic with `earliest_news_at` set and
   `pending_review_news_item_id` cleared. Also confirm the Manual heading
   disappears entirely when nothing is blocked.
9. **Browser** (per the project guideline that UI correctness is confirmed in
   a browser, not inferred from a clean typecheck): expand a real
   multi-company row, check the countdown renders and ticks, confirm a symbol
   holding an open position shows Approve disabled, then click Reject on one
   symbol and Approve on another and confirm the feed updates without a
   reload and the badge/outcome changes. Check the console for errors.
10. Confirm the CLI is otherwise unaffected: `trade_bot.py status`, a normal
    `scan`, and `trade_bot.py confirm` all behave as before.
