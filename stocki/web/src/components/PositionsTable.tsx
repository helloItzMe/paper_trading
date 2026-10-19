import { Fragment, useMemo, useState } from "react";
import type { Position, Signal, Filing } from "../api";
import { inr, pct, istTime, nseQuoteUrl } from "../format";
import { buildSignalRows, badgeClass, type SignalRow } from "../signalRows";
import CopyButton from "./CopyButton";

type SortKey = "entry_time" | "exit_time" | "pnl_amount" | "pnl_pct" | "capital";

function sortValue(p: Position, key: SortKey): number {
  const raw = p[key];
  if (raw === null || raw === undefined) return -Infinity;
  if (key === "entry_time" || key === "exit_time") return new Date(raw as string).getTime();
  return Number(raw);
}

function sourceKind(source: string): "news" | "nse_filing" {
  return source === "nse" ? "nse_filing" : "news";
}

export default function PositionsTable({
  positions,
  signals,
  filings,
}: {
  positions: Position[] | undefined;
  signals: Signal[] | undefined;
  filings: Filing[] | undefined;
}) {
  const [sortKey, setSortKey] = useState<SortKey>("entry_time");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");
  const [expanded, setExpanded] = useState<Set<number>>(new Set());

  const signalLookup = useMemo(() => {
    const map = new Map<string, SignalRow>();
    if (signals && filings) {
      for (const row of buildSignalRows(signals, filings)) map.set(`${row.kind}:${row.rawId}`, row);
    }
    return map;
  }, [signals, filings]);

  if (!positions) return null;
  if (positions.length === 0) return <div className="empty">No positions in this window.</div>;

  function handleSort(key: SortKey) {
    if (key === sortKey) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir("desc");
    }
  }

  function toggleExpand(id: number) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  const factor = sortDir === "asc" ? 1 : -1;
  const sorted = [...positions].sort((a, b) => factor * (sortValue(a, sortKey) - sortValue(b, sortKey)));

  function arrow(key: SortKey) {
    if (key !== sortKey) return "";
    return sortDir === "asc" ? " ▲" : " ▼";
  }

  return (
    <div className="table-scroll">
      <table>
        <thead>
          <tr>
            <th>Symbol</th>
            <th>Entry</th>
            <th onClick={() => handleSort("entry_time")} style={{ cursor: "pointer" }}>
              Entry time{arrow("entry_time")}
            </th>
            <th>Target / Stop</th>
            <th onClick={() => handleSort("capital")} style={{ cursor: "pointer" }}>
              Capital{arrow("capital")}
            </th>
            <th>Exit</th>
            <th onClick={() => handleSort("exit_time")} style={{ cursor: "pointer" }}>
              Exit time{arrow("exit_time")}
            </th>
            <th>Exit reason</th>
            <th onClick={() => handleSort("pnl_pct")} style={{ cursor: "pointer" }}>
              Gain{arrow("pnl_pct")}
            </th>
            <th onClick={() => handleSort("pnl_amount")} style={{ cursor: "pointer" }}>
              P&amp;L{arrow("pnl_amount")}
            </th>
            <th>Source</th>
          </tr>
        </thead>
        <tbody>
          {sorted.map((p) => {
            const pnl = p.pnl_amount === null ? null : Number(p.pnl_amount);
            const signalRow = p.source_id !== null ? signalLookup.get(`${sourceKind(p.source)}:${p.source_id}`) : undefined;
            const isExpanded = expanded.has(p.id);
            const stale = p.stale_days;
            return (
              <Fragment key={p.id}>
                <tr className={stale !== null ? "stale-row" : undefined}>
                  <td>
                    <a href={nseQuoteUrl(p.symbol)} target="_blank" rel="noreferrer" title={`Open ${p.symbol} on NSE`}>
                      <strong>{p.symbol}</strong>
                    </a>
                    <CopyButton text={p.symbol} />
                    {p.company && <div style={{ color: "var(--muted)", fontSize: 11 }}>{p.company}</div>}
                  </td>
                  <td>
                    {p.status === "queued" ? (
                      <span title="Buy-limit price -- not filled yet">limit {inr(p.limit_price)}</span>
                    ) : (
                      inr(p.entry_price)
                    )}
                  </td>
                  <td>{istTime(p.entry_time)}</td>
                  <td>
                    {p.status === "queued" ? "—" : `+${p.target_max_pct}% / ${p.stop_loss_pct ? `${p.stop_loss_pct}%` : "none"}`}
                  </td>
                  <td>{inr(p.capital)}</td>
                  <td>{p.exit_price ? inr(p.exit_price) : "—"}</td>
                  <td>{p.exit_time ? istTime(p.exit_time) : "—"}</td>
                  <td>
                    {p.status === "queued" ? (
                      <span className="badge warn" title="Buy-limit order placed while market was closed -- fills at next market open if price is at or below the limit">
                        queued
                      </span>
                    ) : p.exit_reason ? (
                      <span className="badge muted">{p.exit_reason}</span>
                    ) : stale !== null ? (
                      <span className="badge warn" title={`Open ${stale}+ days with no exit -- monitor may not be running`}>
                        stale ({stale}d)
                      </span>
                    ) : (
                      "open"
                    )}
                  </td>
                  <td style={{ color: p.pnl_pct === null ? undefined : Number(p.pnl_pct) >= 0 ? "var(--green)" : "var(--red)" }}>
                    {p.pnl_pct === null ? "—" : pct(p.pnl_pct)}
                  </td>
                  <td style={{ color: pnl === null ? undefined : pnl >= 0 ? "var(--green)" : "var(--red)" }}>
                    {pnl === null ? "—" : inr(pnl)}
                  </td>
                  <td>
                    {signalRow ? (
                      <button className="expand-toggle" onClick={() => toggleExpand(p.id)}>
                        {isExpanded ? "▾" : "▸"} {p.source}
                      </button>
                    ) : (
                      p.source
                    )}
                  </td>
                </tr>
                {isExpanded && signalRow && (
                  <tr className="expanded-detail">
                    <td colSpan={11}>
                      <table style={{ width: "100%", tableLayout: "fixed" }}>
                        <colgroup>
                          <col style={{ width: 160 }} />
                          <col style={{ width: 90 }} />
                          <col />
                          <col style={{ width: 140 }} />
                          <col style={{ width: 160 }} />
                        </colgroup>
                        <thead>
                          <tr>
                            <th>When</th>
                            <th>Source</th>
                            <th>Headline / filing</th>
                            <th>Symbols</th>
                            <th>Outcome</th>
                          </tr>
                        </thead>
                        <tbody>
                          <tr>
                            <td>{istTime(signalRow.ts)}</td>
                            <td><span className="badge muted">{signalRow.kind}</span></td>
                            <td>
                              {signalRow.link ? (
                                <a className="truncate-1" href={signalRow.link} target="_blank" rel="noreferrer" title={signalRow.title}>
                                  {signalRow.title}
                                </a>
                              ) : (
                                <span className="truncate-1" title={signalRow.title}>{signalRow.title}</span>
                              )}
                            </td>
                            <td>{signalRow.symbols.join(", ") || "—"}</td>
                            <td>
                              {signalRow.kind === "news" ? (
                                <span className={`badge ${badgeClass(signalRow.outcome)}`}>{signalRow.outcome}</span>
                              ) : (
                                signalRow.outcome
                              )}
                            </td>
                          </tr>
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
    </div>
  );
}
