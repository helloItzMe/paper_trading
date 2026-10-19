import { Fragment, useState } from "react";
import type { ResultsCalendarEntry, Signal, Filing } from "../api";
import { istTime, nseQuoteUrl } from "../format";
import { badgeClass } from "../signalRows";
import CopyButton from "./CopyButton";

// Status values come straight from queries.ts's getResultsCalendar --
// mirrors badgeClass's ok/warn/muted convention used elsewhere (SignalFeed,
// PositionsTable) so this widget reads as the same visual language.
function statusBadgeClass(status: string): string {
  if (status === "declared + news found") return "ok";
  if (status === "declared (no news yet)" || status === "news found (no filing matched)") return "warn";
  return "muted"; // 'awaiting declaration'
}

function dateOnly(iso: string): string {
  // declaration_date is a DATE column -- format as a plain date, no IST
  // time-of-day to show (there isn't one).
  return new Date(iso).toLocaleDateString("en-IN", { timeZone: "Asia/Kolkata", day: "2-digit", month: "short", year: "numeric" });
}

// SEBI's real market-cap tier (see cap_tier's type comment in api.ts) --
// shown as plain text (not a badge) since it's descriptive, not a good/bad
// outcome judgment the way Status is. "—" for null is deliberately NOT
// relabeled "SME" here -- that collapse belongs only to trade_bot.py's
// trading-logic fallback, not to this display of the actual raw data.
function capTierLabel(tier: "large" | "mid" | "small" | null): string {
  if (tier === "large") return "Large";
  if (tier === "mid") return "Mid";
  if (tier === "small") return "Small";
  return "—";
}

// No sort applied at all (sortKey === null) shows entries in the server's
// own order (declaration_date, symbol) -- exactly today's behavior, so
// clicking a header is the only thing that changes the default view.
type SortKey = "best_declared_at" | "earliest_news_at";

function sortValue(e: ResultsCalendarEntry, key: SortKey): number {
  const raw = e[key];
  // Not-yet-resolved entries (still null) sort to the bottom on first click
  // (descending, most-recent-first) rather than the top -- same convention
  // SignalFeed's sortValue already uses for its own null timestamps.
  return raw === null ? -Infinity : new Date(raw).getTime();
}

type DetailRow = {
  kind: "news" | "nse_filing";
  ts: string;
  title: string;
  link: string | null;
  outcome: string;
};

// Mirrors PositionsTable's own signal-lookup pattern (source_id -> the real
// news_items/nse_filings row), just keyed off results_calendar's own two IDs
// instead of positions.source_id -- earliest_news_item_id (the article that
// first covered this declaration) and declared_filing_id (NSE's own
// "Outcome of Board Meeting" filing), which can each independently exist,
// be missing, or (rarely) both be present for the same entry.
function buildDetailRows(entry: ResultsCalendarEntry, signals: Signal[] | undefined, filings: Filing[] | undefined): DetailRow[] {
  const rows: DetailRow[] = [];
  if (entry.earliest_news_item_id !== null && signals) {
    const s = signals.find((x) => x.id === entry.earliest_news_item_id);
    if (s) rows.push({ kind: "news", ts: s.first_seen_at, title: s.title, link: s.link, outcome: s.outcome });
  }
  if (entry.declared_filing_id !== null && filings) {
    const f = filings.find((x) => x.id === entry.declared_filing_id);
    if (f) rows.push({ kind: "nse_filing", ts: f.processed_at, title: f.summary || f.category || "NSE filing", link: f.filing_url, outcome: f.outcome });
  }
  return rows;
}

export default function ResultsCalendar({
  entries,
  signals,
  filings,
}: {
  entries: ResultsCalendarEntry[] | undefined;
  signals: Signal[] | undefined;
  filings: Filing[] | undefined;
}) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [sortKey, setSortKey] = useState<SortKey | null>(null);
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");

  if (!entries) return null;

  function toggleExpand(key: string) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  function handleSort(key: SortKey) {
    if (key === sortKey) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir("desc");
    }
  }

  function arrow(key: SortKey) {
    if (key !== sortKey) return "";
    return sortDir === "asc" ? " ▲" : " ▼";
  }

  const factor = sortDir === "asc" ? 1 : -1;
  const sorted = sortKey === null ? entries : [...entries].sort((a, b) => factor * (sortValue(a, sortKey) - sortValue(b, sortKey)));

  return (
    <div className="table-scroll">
      {entries.length === 0 ? (
        <div className="empty">No companies due to declare results in this range.</div>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Declaration date</th>
              <th>Symbol</th>
              <th>Company</th>
              <th>Size</th>
              <th>Quarter</th>
              <th>Status</th>
              <th onClick={() => handleSort("best_declared_at")} style={{ cursor: "pointer" }}>
                Declared at (NSE){arrow("best_declared_at")}
              </th>
              <th onClick={() => handleSort("earliest_news_at")} style={{ cursor: "pointer" }}>
                News found at{arrow("earliest_news_at")}
              </th>
              <th>News / filing</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((e) => {
              const key = `${e.symbol}-${e.declaration_date}`;
              const detailRows = buildDetailRows(e, signals, filings);
              const isExpanded = expanded.has(key);
              return (
                <Fragment key={key}>
                  <tr>
                    <td>{dateOnly(e.declaration_date)}</td>
                    <td>
                      <a href={nseQuoteUrl(e.symbol)} target="_blank" rel="noreferrer" title={`Open ${e.symbol} on NSE`}>
                        {e.symbol}
                      </a>
                      <CopyButton text={e.symbol} />
                    </td>
                    <td className="truncate-1" title={e.company ?? undefined}>{e.company ?? "—"}</td>
                    <td>{capTierLabel(e.cap_tier)}</td>
                    <td>{e.quarter ?? "—"}</td>
                    <td><span className={`badge ${statusBadgeClass(e.status)}`}>{e.status}</span></td>
                    <td>{istTime(e.best_declared_at)}</td>
                    <td>{istTime(e.earliest_news_at)}</td>
                    <td>
                      {detailRows.length > 0 ? (
                        <button className="expand-toggle" onClick={() => toggleExpand(key)}>
                          {isExpanded ? "▾" : "▸"} {detailRows.length}
                        </button>
                      ) : (
                        "—"
                      )}
                    </td>
                  </tr>
                  {isExpanded && detailRows.length > 0 && (
                    <tr className="expanded-detail">
                      <td colSpan={9}>
                        <table style={{ width: "100%", tableLayout: "fixed" }}>
                          <colgroup>
                            <col style={{ width: 160 }} />
                            <col style={{ width: 90 }} />
                            <col />
                            <col style={{ width: 160 }} />
                          </colgroup>
                          <thead>
                            <tr>
                              <th>When</th>
                              <th>Source</th>
                              <th>Headline / filing</th>
                              <th>Outcome</th>
                            </tr>
                          </thead>
                          <tbody>
                            {detailRows.map((r, i) => (
                              <tr key={i}>
                                <td>{istTime(r.ts)}</td>
                                <td><span className="badge muted">{r.kind}</span></td>
                                <td>
                                  {r.link ? (
                                    <a className="truncate-1" href={r.link} target="_blank" rel="noreferrer" title={r.title}>
                                      {r.title}
                                    </a>
                                  ) : (
                                    <span className="truncate-1" title={r.title}>{r.title}</span>
                                  )}
                                </td>
                                <td>
                                  {r.kind === "news" ? (
                                    <span className={`badge ${badgeClass(r.outcome)}`}>{r.outcome}</span>
                                  ) : (
                                    r.outcome
                                  )}
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}
