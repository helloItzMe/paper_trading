const BASE = "/api";

async function get<T>(path: string, params: Record<string, string | undefined> = {}): Promise<T> {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v) qs.set(k, v);
  const url = `${BASE}${path}${qs.toString() ? `?${qs}` : ""}`;
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${path} -> HTTP ${res.status}`);
  return res.json();
}

export type Summary = {
  totalTradesToday: number; // entered today (entry_time-based)
  closedTradesToday: number; // of totalTradesToday, currently closed
  openTradesToday: number; // of totalTradesToday, currently still open
  realizedPnlToday: number; // booked today (exit_time-based)
  winRateToday: number | null;
  closedToday: number;
  winsToday: number;
  lossesToday: number;
  profitBookedToday: number;
  lossBookedToday: number; // negative
  capitalDeployed: number;
  openPositions: number;
  pendingReview: number;
  queuedOrders: number; // market-closed AMO-style limit orders not yet filled (status='queued')
};

export type Position = {
  id: number;
  symbol: string;
  company: string | null;
  source: string;
  source_id: number | null;
  source_title: string | null;
  source_link: string | null;
  reason: string | null;
  confidence: string | null;
  // null while status='queued' -- a market-closed limit order hasn't actually
  // filled at a real price yet (see limit_price below). Same for
  // target_min_pct/target_max_pct/stop_loss_pct: set only at fill time.
  entry_price: string | null;
  entry_time: string;
  target_min_pct: string | null;
  target_max_pct: string | null;
  stop_loss_pct: string | null;
  capital: string;
  status: string;
  // Only set while status='queued' -- the buy-limit price a market-closed order
  // will fill at (or better) once the market reopens. See db.py's
  // positions.limit_price schema notes.
  limit_price: string | null;
  exit_price: string | null;
  exit_time: string | null;
  exit_reason: string | null;
  pnl_pct: string | null;
  pnl_amount: string | null;
  // Computed server-side: days past the ~2-day EOD/grace-period window an
  // OPEN position has sat unresolved, or null if not stale (or not open).
  stale_days: number | null;
};

export type Signal = {
  id: number;
  title: string;
  link: string | null;
  source: string | null;
  event_type: string | null;
  category: string | null;
  signal: string | null;
  first_seen_at: string;
  pub_date: string | null;
  // True if this row came from news_bot.py's standalone --days backfill,
  // not trade_bot.py's live `scan` -- the backfill never runs the buy-
  // decision pipeline, so a backfilled item can resolve to a real company
  // and still never have had a chance to land in pending_candidates. See
  // `outcome` below.
  via_backfill: boolean;
  symbols: string[];
  traded: boolean;
  resolved_action: string | null;
  // Computed server-side (same precedence as the raw traded/resolved_action
  // fields above imply): traded > confirmed > rejected > no symbol resolved
  // > not evaluated (backfill) > pending review. Same wording as the
  // "Pending review" KPI on purpose -- one concept, one label, not two
  // words for the same state. Kept alongside the raw fields so a debugging
  // session can compare "what we conclude" against "what we based it on"
  // in one API response.
  outcome: string;
};

export type Filing = {
  id: number;
  symbol: string | null;
  company: string | null;
  category: string | null;
  signal: string | null;
  summary: string | null;
  filing_url: string | null;
  processed_at: string;
  // filed_at_ts is NULL when NSE's raw filed_at text couldn't be parsed
  // (see db.py's _parse_nse_timestamp) -- filed_at (raw string) is kept as
  // a fallback for display in that case.
  filed_at_ts: string | null;
  filed_at: string | null;
  traded: boolean;
  resolved_action: string | null;
  // Same computed field/precedence as Signal.outcome above.
  outcome: string;
};

export type Pending = {
  id: number;
  source: string;
  symbol: string;
  company: string | null;
  confidence: string | null;
  detail: string | null;
  created_at: string;
};

export type ResultsCalendarEntry = {
  symbol: string;
  company: string | null;
  // DATE column ('YYYY-MM-DD') -- NSE's own forthcoming-board-meeting date
  // for this company, not when we happened to see any resulting news/filing.
  declaration_date: string;
  quarter: string | null; // best-effort 'Q1 FY27'-style label, null if unparseable
  // NSE's own official filing timestamp for the "Outcome of Board Meeting"
  // filing, once matched -- null until then.
  best_declared_at: string | null;
  declared_filing_id: number | null;
  // Earliest matching news article's own pub_date, once found -- null until then.
  earliest_news_at: string | null;
  earliest_news_item_id: number | null;
  news_fetched_at: string | null;
  // SEBI's real market-cap tiers (Nifty 50+Next 50 / Midcap 150 / Smallcap
  // 250 -- see broker/large_cap.py), refreshed ~24h. null means unresolved:
  // not in any of the three fetched indices (could be a genuinely tiny/
  // SME-Emerge-platform company, or just not yet refreshed). This is the
  // RAW companies.cap_tier value, NOT trade_bot.py's simplified trading-view
  // -- _company_tier() only ever distinguishes 'large' vs "everything else
  // (including null)", collapsing mid/small/null into a single stricter
  // "sme" qualification bar. Don't conflate the two: that collapse is a
  // deliberate trading-logic default, not a claim that a 'mid'/'small'/null
  // company is actually SME-sized.
  cap_tier: "large" | "mid" | "small" | null;
  // Computed server-side: 'declared + news found' | 'declared (no news yet)'
  // | 'news found (no filing matched)' | 'awaiting declaration'.
  status: string;
};

// Hypothetical "what if we'd bought every news signal that actually
// qualifies" exercise -- see analysis/backtest.py, which computes and
// persists these rows; this UI only reads them. entry_mode distinguishes
// the same symbol's two possible entry clocks: 'first_seen' (when our own
// pipeline noticed the qualifying article) vs 'pub_date' (when the article
// was actually published). exit_reason 'no_data' means the entry timestamp
// had no candle coverage at all (published after market close, or Upstox's
// historical minute-candle backfill hadn't reached that symbol/date yet).
export type AnalysisBacktestRow = {
  calendar_date: string;
  symbol: string;
  entry_mode: "first_seen" | "pub_date";
  company: string | null;
  cap_tier: "large" | "mid" | "small" | null;
  // 'auto' would have bought outright; 'manual' would have queued for a
  // human `confirm` -- i.e. "waiting for approval". See
  // analysis/backtest.py's _confidence_for().
  confidence: "auto" | "manual" | null;
  headline: string | null;
  link: string | null;
  news_item_id: number | null;
  entry_time: string;
  entry_price: string | null;
  exit_time: string | null;
  exit_price: string | null;
  exit_reason: "target" | "stop" | "eod" | "no_data" | null;
  pnl_pct: string | null;
};

export type ScanRun = {
  id: number;
  command: string;
  started_at: string;
  finished_at: string | null;
  // Set every cycle by a long-running --watch/--tick session (monitor
  // --tick in particular can stay open for days) -- distinguishes "still
  // alive, just hasn't finished yet" from "died without saying why", which
  // finished_at alone can't for a run that's designed to never finish until
  // it's stopped.
  last_heartbeat_at: string | null;
  signals_seen: number | null;
  positions_opened: number | null;
  queued: number | null;
  skipped_quota: number | null;
  error: string | null;
};

// Not tied to a specific scan_runs row -- a fact about the shared Upstox
// token/API right now. Currently just one key, 'upstox_token'.
export type SystemStatus = { key: string; ok: boolean; detail: string | null; checked_at: string };

export type Health = { latest: ScanRun[]; recent: ScanRun[]; systemStatus: SystemStatus[] };

export type TimeseriesPoint = { bucket: string; [k: string]: unknown };

export type RangeSummary = {
  realizedPnl: number; // booked this period (exit_time-based)
  wins: number;
  losses: number;
  profitBooked: number;
  lossBooked: number; // negative
  winRate: number | null;
  totalTrades: number; // entered this period (entry_time-based)
  closedTrades: number; // of totalTrades, currently closed
  openTrades: number; // of totalTrades, currently still open
};

export type Timeseries = {
  profit: {
    bucket: string;
    pnl: number;
    cumulative: number;
    wins: number;
    losses: number;
    profitBooked: number;
    lossBooked: number; // negative
  }[];
  newsVolume: { news: { bucket: string; event_type: string | null; n: number }[]; filings: { bucket: string; n: number }[] };
  funnel: {
    seen: number;
    resolvedToTicker: number;
    autoBought: number;
    queued: number;
    confirmed: number;
    rejected: number;
    still_pending: number;
  };
  capital: { bucket: string; capitalAdded: number; cumulative: number }[];
  boughtSold: { bought: { bucket: string; n: number }[]; sold: { bucket: string; n: number }[] };
  exitMix: { exit_reason: string; n: number }[];
};

export const api = {
  users: () => get<string[]>("/users"),
  summary: (user: string) => get<Summary>("/summary", { user }),
  rangeSummary: (user: string, from: string, to: string) =>
    get<RangeSummary>("/summary/range", { user, from, to }),
  livePositions: (user: string, from: string, to: string) =>
    get<Position[]>("/positions/live", { user, from, to }),
  closedPositions: (user: string, from: string, to: string) =>
    get<Position[]>("/positions/closed", { user, from, to }),
  signals: (user: string, from: string, to: string) => get<Signal[]>("/signals", { user, from, to }),
  filings: (user: string, from: string, to: string) => get<Filing[]>("/filings", { user, from, to }),
  pending: (user: string) => get<Pending[]>("/pending", { user }),
  // Not user-scoped -- see queries.ts's getResultsCalendar. from/to are
  // 'YYYY-MM-DD' DATE strings, matched against declaration_date directly.
  resultsCalendar: (from: string, to: string) => get<ResultsCalendarEntry[]>("/results-calendar", { from, to }),
  // Not user-scoped -- see queries.ts's getAnalysisBacktest. `date` is a
  // 'YYYY-MM-DD' DATE string, matched against calendar_date directly.
  analysisBacktest: (date: string) => get<AnalysisBacktestRow[]>("/analysis", { date }),
  health: (user: string) => get<Health>("/health", { user }),
  timeseries: (user: string, from: string, to: string, bucket: "15min" | "day") =>
    get<Timeseries>("/timeseries", { user, from, to, bucket }),
};
