import pool from "./db.js";

// All SQL for the dashboard lives in this one file -- CLAUDE.md's "db.py is
// the only place SQL lives" becomes "SQL lives in two places, each owning one
// direction: db.py writes, this file reads." See stocki/README.md.

export type Bucket = "15min" | "day";

function bucketExpr(column: string, bucket: Bucket): string {
  // bucket is always one of the two literals above (validated in routes.ts
  // before this is called), never user-interpolated free text.
  if (bucket === "day") return `date_trunc('day', ${column})`;
  return `date_trunc('hour', ${column}) + floor(extract(minute from ${column}) / 15) * interval '15 minutes'`;
}

export async function getUsers(): Promise<string[]> {
  const { rows } = await pool.query(
    `SELECT DISTINCT "user" FROM (
       SELECT "user" FROM news_items
       UNION SELECT "user" FROM nse_filings
       UNION SELECT "user" FROM positions
       UNION SELECT "user" FROM pending_candidates
       UNION SELECT "user" FROM scan_runs
     ) u ORDER BY 1`
  );
  return rows.map((r) => r.user);
}

export async function getSummary(user: string) {
  const [trades, pnl, capital, open, pending, queued] = await Promise.all([
    // Trades ENTERED today, broken down by current status -- same shape as
    // getRangeSummary's totalTrades/closedTrades/openTrades, just scoped to
    // CURRENT_DATE instead of an arbitrary range. Excludes status='queued':
    // a market-closed limit order that hasn't filled yet isn't a trade -- without
    // this filter, a same-day queued row would inflate `total` above
    // closed+open with no visible explanation for the gap.
    pool.query(
      `SELECT COUNT(*)::int AS total,
              COUNT(*) FILTER (WHERE status = 'closed')::int AS closed,
              COUNT(*) FILTER (WHERE status = 'open')::int AS open
         FROM positions
        WHERE "user" = $1 AND entry_time::date = CURRENT_DATE AND status != 'queued'`,
      [user]
    ),
    pool.query(
      `SELECT COALESCE(SUM(pnl_amount), 0)::float AS realized_pnl,
              COUNT(*) FILTER (WHERE pnl_amount > 0)::int AS wins,
              COUNT(*) FILTER (WHERE pnl_amount < 0)::int AS losses,
              COALESCE(SUM(pnl_amount) FILTER (WHERE pnl_amount > 0), 0)::float AS profit_booked,
              COALESCE(SUM(pnl_amount) FILTER (WHERE pnl_amount < 0), 0)::float AS loss_booked,
              COUNT(*)::int AS closed
         FROM positions
        WHERE "user" = $1 AND status = 'closed' AND exit_time::date = CURRENT_DATE`,
      [user]
    ),
    pool.query(
      `SELECT COALESCE(SUM(capital), 0)::float AS deployed FROM positions
        WHERE "user" = $1 AND status = 'open'`,
      [user]
    ),
    pool.query(`SELECT COUNT(*)::int AS n FROM positions WHERE "user" = $1 AND status = 'open'`, [user]),
    pool.query(
      `SELECT COUNT(*)::int AS n FROM pending_candidates WHERE "user" = $1 AND resolved_at IS NULL`,
      [user]
    ),
    // Market-closed AMO-style limit orders (see trading/trade_bot.py's
    // _queue_offhours_order) -- status='queued' reuses the positions table
    // rather than a new one (see db.py's schema notes on positions.limit_price),
    // so this is the one query anywhere that needs to know that status exists.
    pool.query(`SELECT COUNT(*)::int AS n FROM positions WHERE "user" = $1 AND status = 'queued'`, [user]),
  ]);

  return {
    totalTradesToday: trades.rows[0].total,
    closedTradesToday: trades.rows[0].closed,
    openTradesToday: trades.rows[0].open,
    realizedPnlToday: pnl.rows[0].realized_pnl,
    winRateToday: pnl.rows[0].closed > 0 ? pnl.rows[0].wins / pnl.rows[0].closed : null,
    closedToday: pnl.rows[0].closed,
    winsToday: pnl.rows[0].wins,
    lossesToday: pnl.rows[0].losses,
    profitBookedToday: pnl.rows[0].profit_booked,
    lossBookedToday: pnl.rows[0].loss_booked,
    capitalDeployed: capital.rows[0].deployed,
    openPositions: open.rows[0].n,
    pendingReview: pending.rows[0].n,
    queuedOrders: queued.rows[0].n,
  };
}

// CLAUDE.md's EOD/grace-period exit logic guarantees an open position
// resolves within about 2 trading days -- one still open past that is a
// signal `monitor` isn't running or is stuck, not a normal outcome.
// Computed here (not client-side) so the API response itself is the
// evidence when debugging why a position looks stale.
const STALE_DAYS_EXPR = `
  CASE WHEN p.status = 'open' AND now() - p.entry_time > interval '2 days'
       THEN FLOOR(EXTRACT(EPOCH FROM (now() - p.entry_time)) / 86400)::int
       ELSE NULL END AS stale_days`;

// The Live tab's positions feed: every currently-open position (regardless
// of when it was bought) plus anything closed within the live window, PLUS
// any still-queued market-closed limit order (status='queued' -- see db.py's
// positions.limit_price notes) -- full entry+exit lifecycle, not just
// "still open", since a position that closed five minutes ago is exactly
// what a live feed should still show, and a queued order is exactly what
// the user needs visibility into before it fills. The client renders
// status='queued' rows distinctly (limit_price, not entry_price; a
// "queued" badge, not "open") -- see PositionsTable.tsx.
export async function getLivePositions(user: string, from: string, to: string) {
  const { rows } = await pool.query(
    `SELECT p.*,
            CASE WHEN p.source = 'news' THEN n.title WHEN p.source = 'nse' THEN f.summary END AS source_title,
            CASE WHEN p.source = 'news' THEN n.link WHEN p.source = 'nse' THEN f.filing_url END AS source_link,
            ${STALE_DAYS_EXPR}
       FROM positions p
       LEFT JOIN news_items n ON p.source = 'news' AND p.source_id = n.id
       LEFT JOIN nse_filings f ON p.source = 'nse' AND p.source_id = f.id
      WHERE p."user" = $1 AND (p.status = 'open' OR p.status = 'queued' OR p.exit_time BETWEEN $2 AND $3)
      ORDER BY p.entry_time DESC`,
    [user, from, to]
  );
  return rows;
}

export async function getClosedPositions(user: string, from: string, to: string) {
  const { rows } = await pool.query(
    `SELECT p.*, ${STALE_DAYS_EXPR}
       FROM positions p
      WHERE "user" = $1 AND status = 'closed' AND exit_time BETWEEN $2 AND $3
      ORDER BY exit_time DESC`,
    [user, from, to]
  );
  return rows;
}

// Whole-range totals for the Historic tab's KPI row -- getSummary above is
// the same shape but hardcoded to CURRENT_DATE; this is the same
// computation parameterized over an arbitrary date range instead.
export async function getRangeSummary(user: string, from: string, to: string) {
  const [bookedResult, tradesResult] = await Promise.all([
    // P&L booked THIS period -- exit_time-based, feeds "Total realized P&L".
    pool.query(
      `SELECT COALESCE(SUM(pnl_amount), 0)::float AS realized_pnl,
              COUNT(*) FILTER (WHERE pnl_amount > 0)::int AS wins,
              COUNT(*) FILTER (WHERE pnl_amount < 0)::int AS losses,
              COALESCE(SUM(pnl_amount) FILTER (WHERE pnl_amount > 0), 0)::float AS profit_booked,
              COALESCE(SUM(pnl_amount) FILTER (WHERE pnl_amount < 0), 0)::float AS loss_booked,
              COUNT(*)::int AS closed
         FROM positions
        WHERE "user" = $1 AND status = 'closed' AND exit_time BETWEEN $2 AND $3`,
      [user, from, to]
    ),
    // Trades OPENED this period -- entry_time-based, independent of the
    // query above: a position entered near the end of the range may still
    // be open (or may close after the range ends, e.g. via the EOD grace
    // period), so "total" here is broken down by CURRENT status, not
    // whether it happened to book P&L inside this same window. Excludes
    // status='queued' -- see getSummary's identical note above.
    pool.query(
      `SELECT COUNT(*)::int AS total,
              COUNT(*) FILTER (WHERE status = 'closed')::int AS closed,
              COUNT(*) FILTER (WHERE status = 'open')::int AS open
         FROM positions
        WHERE "user" = $1 AND entry_time BETWEEN $2 AND $3 AND status != 'queued'`,
      [user, from, to]
    ),
  ]);
  const b = bookedResult.rows[0];
  const t = tradesResult.rows[0];
  return {
    realizedPnl: b.realized_pnl,
    wins: b.wins,
    losses: b.losses,
    profitBooked: b.profit_booked,
    lossBooked: b.loss_booked,
    winRate: b.closed > 0 ? b.wins / b.closed : null,
    totalTrades: t.total,
    closedTrades: t.closed,
    openTrades: t.open,
  };
}

export async function getSignals(user: string, from: string, to: string) {
  // GROUP BY n.id alone (safe -- it's news_items' primary key, so every
  // other n.* column is functionally dependent per Postgres's rules).
  // Grouping on p.id/pc.resolved_action too, as an earlier version of this
  // query did, produced one row per (symbol, pending-candidate) combination
  // for headlines mentioning multiple companies -- duplicate news_items.id
  // values feeding a React list key downstream. traded/resolved_action are
  // aggregated across all of a headline's matched symbols instead.
  const { rows } = await pool.query(
    `SELECT n.*,
            COALESCE(json_agg(DISTINCT nc.symbol) FILTER (WHERE nc.symbol IS NOT NULL), '[]') AS symbols,
            bool_or(p.status IN ('open', 'closed')) AS traded,
            CASE
              WHEN bool_or(pc.resolved_action = 'confirmed') THEN 'confirmed'
              WHEN bool_or(pc.resolved_action = 'rejected') THEN 'rejected'
              ELSE NULL
            END AS resolved_action,
            -- Computed here, not client-side, so the API response IS the
            -- explanation for "why does this headline say X" -- same
            -- precedence the old client-side outcomeOf() used: an actual
            -- FILLED trade beats a still-queued market-closed limit order beats a
            -- review decision beats "never resolved to a ticker at all"
            -- beats "backfilled, never evaluated" beats the default "sat in
            -- the queue". 'traded' checks p.status IN ('open','closed')
            -- specifically, NOT just "p.id IS NOT NULL" -- a queued-but-
            -- unfilled market-closed order (status='queued', see db.py's
            -- positions.limit_price notes) also LEFT JOINs a real p.id, and
            -- showing that as "traded" is wrong: nothing's been bought yet.
            -- n.via_backfill: a headline from news_bot.py's standalone
            -- --days backfill never goes through trade_bot.py's _qualifies/
            -- _decide/add_pending -- it may have RESOLVED to a company yet
            -- still never had a chance to actually land in
            -- pending_candidates, which is a different thing from
            -- genuinely "pending review" (nothing to review via the confirm
            -- command for it). Outcome strings are user-facing labels (this is what
            -- renders directly in the feed) -- 'pending review' matches the
            -- KPI's wording exactly, on purpose: this and "Pending review:
            -- N" up top must always read as the same concept, not two
            -- different words for the one state (was "queued / no action"
            -- here, "Pending review" there).
            CASE
              WHEN bool_or(p.status IN ('open', 'closed')) THEN 'traded'
              WHEN bool_or(p.status = 'queued') THEN 'queued'
              WHEN bool_or(pc.resolved_action = 'confirmed') THEN 'confirmed'
              WHEN bool_or(pc.resolved_action = 'rejected') THEN 'rejected'
              WHEN NOT bool_or(nc.symbol IS NOT NULL) THEN 'no symbol resolved'
              WHEN n.via_backfill THEN 'not evaluated (backfill)'
              ELSE 'pending review'
            END AS outcome
       FROM news_items n
       LEFT JOIN news_item_companies nc ON nc.news_item_id = n.id
       LEFT JOIN positions p ON p.source = 'news' AND p.source_id = n.id AND p."user" = n."user"
       LEFT JOIN pending_candidates pc ON pc.symbol = nc.symbol AND pc."user" = n."user"
      WHERE n."user" = $1 AND n.first_seen_at BETWEEN $2 AND $3 AND n.event_type != 'STALE'
      GROUP BY n.id
      ORDER BY n.first_seen_at DESC`,
    [user, from, to]
  );
  return rows;
}

export async function getFilings(user: string, from: string, to: string) {
  // Mirrors getSignals' outcome logic above -- NSE filings always carry a
  // real, exact ticker already (no LLM extraction/grounding step, unlike
  // news), so this joins directly on f.symbol instead of going through an
  // extracted-companies table. Previously this query had NO join to
  // positions/pending_candidates at all, so a filing's "outcome" was
  // whatever silently fell back to client-side (the raw filing category) --
  // a genuinely still-pending filing was indistinguishable from a traded or
  // ignored one.
  const { rows } = await pool.query(
    `SELECT f.*,
            bool_or(p.status IN ('open', 'closed')) AS traded,
            CASE
              WHEN bool_or(pc.resolved_action = 'confirmed') THEN 'confirmed'
              WHEN bool_or(pc.resolved_action = 'rejected') THEN 'rejected'
              ELSE NULL
            END AS resolved_action,
            -- 'traded' checks p.status IN ('open','closed'), NOT just
            -- "p.id IS NOT NULL" -- see getSignals' identical note above on
            -- why a queued-but-unfilled market-closed order must not show as traded.
            CASE
              WHEN bool_or(p.status IN ('open', 'closed')) THEN 'traded'
              WHEN bool_or(p.status = 'queued') THEN 'queued'
              WHEN bool_or(pc.resolved_action = 'confirmed') THEN 'confirmed'
              WHEN bool_or(pc.resolved_action = 'rejected') THEN 'rejected'
              WHEN bool_or(pc.symbol IS NOT NULL) THEN 'pending review'
              ELSE 'no action'
            END AS outcome
       FROM nse_filings f
       LEFT JOIN positions p ON p.source = 'nse' AND p.source_id = f.id AND p."user" = f."user"
       LEFT JOIN pending_candidates pc ON pc.symbol = f.symbol AND pc."user" = f."user"
      WHERE f."user" = $1 AND f.processed_at BETWEEN $2 AND $3
      GROUP BY f.id
      ORDER BY f.processed_at DESC`,
    [user, from, to]
  );
  return rows;
}

export async function getPending(user: string) {
  const { rows } = await pool.query(
    `SELECT * FROM pending_candidates WHERE "user" = $1 AND resolved_at IS NULL ORDER BY created_at`,
    [user]
  );
  return rows;
}

export async function getHealth(user: string) {
  const { rows } = await pool.query(
    `SELECT DISTINCT ON (command) *
       FROM scan_runs
      WHERE "user" = $1
      ORDER BY command, started_at DESC`,
    [user]
  );
  const { rows: recent } = await pool.query(
    `SELECT * FROM scan_runs WHERE "user" = $1 ORDER BY started_at DESC LIMIT 20`,
    [user]
  );
  // Not user-scoped -- like `companies`, this is a fact about the shared
  // Upstox token/API, not about which user's scan wrote it. Written by
  // trade_bot.py's _market_is_closed(), which already makes a real Upstox
  // API call every scan/monitor cycle -- see db.py's system_status schema
  // comment for why this is the one place that call's result was worth
  // recording instead of silently falling through to the schedule fallback.
  const { rows: systemStatus } = await pool.query(`SELECT * FROM system_status`);
  return { latest: rows, recent, systemStatus };
}

export async function getProfitSeries(user: string, from: string, to: string, bucket: Bucket) {
  const expr = bucketExpr("exit_time", bucket);
  const { rows } = await pool.query(
    `SELECT ${expr} AS bucket,
            SUM(pnl_amount)::float AS pnl,
            COUNT(*) FILTER (WHERE pnl_amount > 0)::int AS wins,
            COUNT(*) FILTER (WHERE pnl_amount < 0)::int AS losses,
            COALESCE(SUM(pnl_amount) FILTER (WHERE pnl_amount > 0), 0)::float AS profit_booked,
            COALESCE(SUM(pnl_amount) FILTER (WHERE pnl_amount < 0), 0)::float AS loss_booked
       FROM positions
      WHERE "user" = $1 AND status = 'closed' AND exit_time BETWEEN $2 AND $3
      GROUP BY 1 ORDER BY 1`,
    [user, from, to]
  );
  let running = 0;
  return rows.map((r) => {
    running += Number(r.pnl);
    return {
      bucket: r.bucket,
      pnl: Number(r.pnl),
      cumulative: running,
      wins: r.wins,
      losses: r.losses,
      profitBooked: r.profit_booked,
      lossBooked: r.loss_booked,
    };
  });
}

export async function getNewsVolumeSeries(user: string, from: string, to: string, bucket: Bucket) {
  const newsExpr = bucketExpr("first_seen_at", bucket);
  const filingExpr = bucketExpr("processed_at", bucket);
  const [news, filings] = await Promise.all([
    pool.query(
      `SELECT ${newsExpr} AS bucket, event_type, COUNT(*)::int AS n
         FROM news_items WHERE "user" = $1 AND first_seen_at BETWEEN $2 AND $3
        GROUP BY 1, 2 ORDER BY 1`,
      [user, from, to]
    ),
    pool.query(
      `SELECT ${filingExpr} AS bucket, COUNT(*)::int AS n
         FROM nse_filings WHERE "user" = $1 AND processed_at BETWEEN $2 AND $3
        GROUP BY 1 ORDER BY 1`,
      [user, from, to]
    ),
  ]);
  return { news: news.rows, filings: filings.rows };
}

export async function getBuyFunnel(user: string, from: string, to: string) {
  const [seen, resolved, autoBought, queued, reviewOutcomes] = await Promise.all([
    pool.query(
      `SELECT COUNT(*)::int AS n FROM news_items WHERE "user" = $1 AND first_seen_at BETWEEN $2 AND $3`,
      [user, from, to]
    ),
    pool.query(
      `SELECT COUNT(DISTINCT n.id)::int AS n FROM news_items n
         JOIN news_item_companies nc ON nc.news_item_id = n.id
        WHERE n."user" = $1 AND n.first_seen_at BETWEEN $2 AND $3`,
      [user, from, to]
    ),
    // Excludes status='queued' -- a still-queued market-closed limit order hasn't
    // actually been bought yet (see getSummary's identical note above).
    pool.query(
      `SELECT COUNT(*)::int AS n FROM positions
        WHERE "user" = $1 AND source = 'news' AND entry_time BETWEEN $2 AND $3 AND status != 'queued'`,
      [user, from, to]
    ),
    pool.query(
      `SELECT COUNT(*)::int AS n FROM pending_candidates
        WHERE "user" = $1 AND source = 'news' AND created_at BETWEEN $2 AND $3`,
      [user, from, to]
    ),
    pool.query(
      `SELECT resolved_action, COUNT(*)::int AS n FROM pending_candidates
        WHERE "user" = $1 AND source = 'news' AND created_at BETWEEN $2 AND $3
        GROUP BY resolved_action`,
      [user, from, to]
    ),
  ]);
  const outcomes: Record<string, number> = { confirmed: 0, rejected: 0, still_pending: 0 };
  for (const r of reviewOutcomes.rows) {
    if (r.resolved_action === "confirmed") outcomes.confirmed = r.n;
    else if (r.resolved_action === "rejected") outcomes.rejected = r.n;
    else outcomes.still_pending = r.n; // resolved_action IS NULL -> still unresolved
  }
  return {
    seen: seen.rows[0].n,
    resolvedToTicker: resolved.rows[0].n,
    autoBought: autoBought.rows[0].n,
    queued: queued.rows[0].n,
    ...outcomes,
  };
}

export async function getCapitalSeries(user: string, from: string, to: string, bucket: Bucket) {
  const expr = bucketExpr("entry_time", bucket);
  const { rows } = await pool.query(
    `SELECT ${expr} AS bucket, SUM(capital)::float AS capital
       FROM positions
      WHERE "user" = $1 AND entry_time BETWEEN $2 AND $3
      GROUP BY 1 ORDER BY 1`,
    [user, from, to]
  );
  let running = 0;
  return rows.map((r) => {
    running += Number(r.capital);
    return { bucket: r.bucket, capitalAdded: Number(r.capital), cumulative: running };
  });
}

export async function getBoughtSoldSeries(user: string, from: string, to: string, bucket: Bucket) {
  const buyExpr = bucketExpr("entry_time", bucket);
  const sellExpr = bucketExpr("exit_time", bucket);
  const [bought, sold] = await Promise.all([
    pool.query(
      `SELECT ${buyExpr} AS bucket, COUNT(*)::int AS n FROM positions
        WHERE "user" = $1 AND entry_time BETWEEN $2 AND $3 GROUP BY 1 ORDER BY 1`,
      [user, from, to]
    ),
    pool.query(
      `SELECT ${sellExpr} AS bucket, COUNT(*)::int AS n FROM positions
        WHERE "user" = $1 AND status = 'closed' AND exit_time BETWEEN $2 AND $3 GROUP BY 1 ORDER BY 1`,
      [user, from, to]
    ),
  ]);
  return { bought: bought.rows, sold: sold.rows };
}

// NSE's forthcoming-board-meetings calendar (results_calendar, see db.py's
// schema notes and nse_bot.results_calendar) -- shared reference data like
// `companies`, NOT user-attributed (NSE's real calendar is the same
// regardless of who's running the bot), so this takes a plain date range,
// no `user` param. from/to are DATE strings ('YYYY-MM-DD'), matched against
// declaration_date directly -- unlike every other query here, this is NOT
// filtered by first_seen_at/entry_time/etc, on purpose: the whole point of
// this widget is "which companies are DUE to declare in this window",
// independent of when we happened to see any resulting news/filing.
export async function getResultsCalendar(from: string, to: string) {
  const { rows } = await pool.query(
    `SELECT rc.symbol, rc.company, rc.declaration_date, rc.quarter,
            rc.best_declared_at, rc.declared_filing_id,
            rc.earliest_news_at, rc.earliest_news_item_id,
            rc.news_fetched_at,
            -- SEBI's real market-cap tier -- 'large' (Nifty 50+Next 50) |
            -- 'mid' (Midcap 150) | 'small' (Smallcap 250) | NULL (not in
            -- any of the three, or not yet refreshed) -- see
            -- broker/large_cap.py, refreshed ~24h alongside the rest of the
            -- instrument master. LEFT JOIN since a calendar symbol isn't
            -- guaranteed to be in companies yet. This is the raw tier, NOT
            -- trade_bot.py's simplified trading view -- _company_tier()
            -- only distinguishes 'large' vs everything else (collapsing
            -- mid/small/null into one stricter qualification bar), which is
            -- a trading-logic default, not a claim about actual company size.
            c.cap_tier,
            CASE
              WHEN rc.best_declared_at IS NOT NULL AND rc.earliest_news_at IS NOT NULL THEN 'declared + news found'
              WHEN rc.best_declared_at IS NOT NULL THEN 'declared (no news yet)'
              WHEN rc.earliest_news_at IS NOT NULL THEN 'news found (no filing matched)'
              ELSE 'awaiting declaration'
            END AS status
       FROM results_calendar rc
       LEFT JOIN companies c ON c.symbol = rc.symbol
      WHERE rc.declaration_date BETWEEN $1 AND $2
      ORDER BY rc.declaration_date, rc.symbol`,
    [from, to]
  );
  return rows;
}

export async function getExitReasonMix(user: string, from: string, to: string) {
  const { rows } = await pool.query(
    `SELECT exit_reason, COUNT(*)::int AS n FROM positions
      WHERE "user" = $1 AND status = 'closed' AND exit_time BETWEEN $2 AND $3
      GROUP BY exit_reason`,
    [user, from, to]
  );
  return rows;
}

// Hypothetical "what if we'd bought every news signal that actually
// qualifies" exercise (see analysis/backtest.py) -- precomputed and
// persisted there, not computed here. Not user-scoped, same reasoning as
// getResultsCalendar: the underlying news/prices are the same regardless
// of who's running the bot. calendar_date is a plain DATE, matched
// directly against the `date` param ('YYYY-MM-DD').
export async function getAnalysisBacktest(date: string) {
  const { rows } = await pool.query(
    `SELECT * FROM analysis_backtest WHERE calendar_date = $1
      ORDER BY entry_mode, entry_time`,
    [date]
  );
  return rows;
}
