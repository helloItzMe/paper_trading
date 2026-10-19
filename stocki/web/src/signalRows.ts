import type { Signal, Filing } from "./api";

export type Kind = "news" | "nse_filing";

export type SignalRow = {
  id: string;
  rawId: number;
  kind: Kind;
  ts: string; // when WE first saw this (first_seen_at / processed_at)
  // When the article/filing itself was actually published -- news:
  // pub_date (Google News' own timestamp); filing: filed_at_ts (NSE's
  // parsed timestamp, null if unparseable -- see db.py's
  // _parse_nse_timestamp). Distinct from `ts`: a headline can be published
  // hours before we ever fetch/see it.
  publishedAt: string | null;
  title: string;
  symbols: string[];
  // What KIND of signal this is -- news: classifier.py's event_type
  // (ORDER_WIN/PROFIT_INCREASE/...); filing: nse_bot.classify()'s tag
  // (deal/results/risk/other). Independent of whether anything came of it.
  type: string;
  // What HAPPENED as a result -- traded/queued/confirmed/rejected/not
  // evaluated (backfill)/pending review/no action -- from the USER's
  // perspective (what needs my attention). Was conflated with `type` before
  // (filings used the raw filing category as "outcome", which is actually
  // its type, not what happened to it). 'queued' means a real, still-open
  // market-closed AMO-style limit order (see db.py's positions.limit_price) that
  // hasn't filled yet -- distinct from 'traded', which means an actual buy
  // landed. 'not evaluated (backfill)' means this came from news_bot.py's
  // standalone backfill, which never runs the buy-decision pipeline at all
  // -- distinct from 'pending review', which means it's genuinely sitting
  // in the confirm queue.
  outcome: string;
  link: string | null;
};

export function badgeClass(outcome: string): string {
  if (outcome === "traded" || outcome === "confirmed") return "ok";
  if (outcome === "rejected") return "bad";
  if (outcome === "queued") return "warn";
  return "muted"; // covers 'pending review', 'not evaluated (backfill)', 'no symbol resolved', etc.
}

// Shared between SignalFeed (the full feed) and PositionsTable (looking up
// the exact signal/filing a given position traced back to) so both render
// identical fields for the same underlying row.
export function buildSignalRows(signals: Signal[], filings: Filing[]): SignalRow[] {
  return [
    ...signals.map((s) => ({
      id: `n${s.id}`,
      rawId: s.id,
      kind: "news" as const,
      ts: s.first_seen_at,
      publishedAt: s.pub_date,
      title: s.title,
      symbols: s.symbols,
      type: s.event_type ?? "OTHER",
      outcome: s.outcome,
      link: s.link,
    })),
    ...filings.map((f) => ({
      id: `f${f.id}`,
      rawId: f.id,
      kind: "nse_filing" as const,
      ts: f.processed_at,
      publishedAt: f.filed_at_ts,
      title: f.summary ?? f.category ?? "NSE filing",
      symbols: f.symbol ? [f.symbol] : [],
      type: f.signal ?? "other",
      outcome: f.outcome,
      link: f.filing_url,
    })),
  ].sort((a, b) => new Date(b.ts).getTime() - new Date(a.ts).getTime());
}
