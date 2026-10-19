import { useMemo, useRef, useState } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import type { Signal, Filing } from "../api";
import { istTime, nseQuoteUrl } from "../format";
import { buildSignalRows, badgeClass, type Kind, type SignalRow } from "../signalRows";
import SearchableSelect from "./SearchableSelect";
import CopyButton from "./CopyButton";

type SortKey = "ts" | "publishedAt";

function sortValue(r: SignalRow, key: SortKey): number {
  const raw = r[key];
  return raw === null ? -Infinity : new Date(raw).getTime();
}

// Verified live: a single trading day's Live-tab range returned 273 news
// items + 1475 filings, ~1750 rows -- rendering all of them as plain
// <table> rows (every one a real, always-mounted DOM node) made scrolling
// visibly laggy. Virtualizing keeps only the rows in/near the viewport
// mounted at any moment; the visible content on screen is identical either
// way. estimateSize is a starting guess only -- measureElement below
// corrects it per-row from the real rendered height, so a row wrapping to
// two lines doesn't throw off the rest of the list.
const ROW_ESTIMATE_PX = 33;
const OVERSCAN = 12;

export default function SignalFeed({ signals, filings }: { signals: Signal[] | undefined; filings: Filing[] | undefined }) {
  const [source, setSource] = useState<"all" | Kind>("all");
  const [types, setTypes] = useState<string[]>([]); // empty = no filter (same meaning as the others' "all")
  const [outcome, setOutcome] = useState<string>("all");
  const [symbol, setSymbol] = useState<string>("all");
  const [sortKey, setSortKey] = useState<SortKey>("ts");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");
  const scrollRef = useRef<HTMLDivElement>(null);

  const rows = useMemo(() => (signals && filings ? buildSignalRows(signals, filings) : []), [signals, filings]);

  const typeOptions = useMemo(() => Array.from(new Set(rows.map((r) => r.type))).sort(), [rows]);
  const outcomeOptions = useMemo(() => Array.from(new Set(rows.map((r) => r.outcome))).sort(), [rows]);
  const symbolOptions = useMemo(
    () => Array.from(new Set(rows.flatMap((r) => r.symbols))).sort(),
    [rows]
  );

  const factor = sortDir === "asc" ? 1 : -1;
  // Memoized (wasn't before) -- with ~1750 rows, re-filtering/re-sorting on
  // every unrelated re-render (e.g. Live's 10s poll ticking a sibling query)
  // was its own avoidable cost on top of the render volume itself.
  const filtered = useMemo(
    () =>
      rows
        .filter(
          (r) =>
            (source === "all" || r.kind === source) &&
            (types.length === 0 || types.includes(r.type)) &&
            (outcome === "all" || r.outcome === outcome) &&
            (symbol === "all" || r.symbols.includes(symbol))
        )
        .sort((a, b) => factor * (sortValue(a, sortKey) - sortValue(b, sortKey))),
    [rows, source, types, outcome, symbol, sortKey, factor]
  );

  // Called unconditionally, before the signals/filings early-return below --
  // hooks can't be called conditionally, and filtered is already a safe []
  // when data hasn't loaded yet.
  const rowVirtualizer = useVirtualizer({
    count: filtered.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => ROW_ESTIMATE_PX,
    overscan: OVERSCAN,
  });

  if (!signals || !filings) return null;

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

  const virtualRows = rowVirtualizer.getVirtualItems();
  // Two spacer rows stand in for everything above/below the rendered
  // window, so the table's scrollHeight (and scrollbar) still reflect the
  // FULL row count -- a real <tr> can't be position:absolute without
  // breaking column alignment, so this is the standard way to virtualize
  // inside an actual <table>/<tbody> rather than a div-grid layout.
  const paddingTop = virtualRows.length > 0 ? virtualRows[0].start : 0;
  const paddingBottom = virtualRows.length > 0 ? rowVirtualizer.getTotalSize() - virtualRows[virtualRows.length - 1].end : 0;

  return (
    <>
      <div className="filter-row">
        <select value={source} onChange={(e) => setSource(e.target.value as "all" | Kind)}>
          <option value="all">All sources</option>
          <option value="news">News</option>
          <option value="nse_filing">NSE filing</option>
        </select>
        <SearchableSelect multi options={typeOptions} value={types} onChange={setTypes} allLabel="All types" width={280} />
        <SearchableSelect options={outcomeOptions} value={outcome} onChange={setOutcome} allLabel="All outcomes" width={320} />
        <SearchableSelect options={symbolOptions} value={symbol} onChange={setSymbol} allLabel="All symbols" />
      </div>
      <div className="table-scroll feed-scroll" ref={scrollRef}>
        {filtered.length === 0 ? (
          <div className="empty">No signals in this range.</div>
        ) : (
          <table>
            <thead>
              <tr>
                <th onClick={() => handleSort("ts")} style={{ cursor: "pointer" }}>
                  Seen{arrow("ts")}
                </th>
                <th onClick={() => handleSort("publishedAt")} style={{ cursor: "pointer" }}>
                  Published{arrow("publishedAt")}
                </th>
                <th>Source</th>
                <th>Headline / filing</th>
                <th>Symbols</th>
                <th>Type</th>
                <th>Outcome</th>
              </tr>
            </thead>
            <tbody>
              {paddingTop > 0 && (
                <tr>
                  <td colSpan={7} style={{ height: paddingTop, padding: 0, border: "none" }} />
                </tr>
              )}
              {virtualRows.map((virtualRow) => {
                const r = filtered[virtualRow.index];
                return (
                  <tr key={r.id} ref={rowVirtualizer.measureElement} data-index={virtualRow.index}>
                    <td>{istTime(r.ts)}</td>
                    <td>{istTime(r.publishedAt)}</td>
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
                      {r.symbols.length === 0
                        ? "—"
                        : r.symbols.map((sym, i) => (
                            <span key={sym}>
                              {i > 0 && ", "}
                              <a href={nseQuoteUrl(sym)} target="_blank" rel="noreferrer" title={`Open ${sym} on NSE`}>
                                {sym}
                              </a>
                              <CopyButton text={sym} />
                            </span>
                          ))}
                    </td>
                    <td><span className="badge muted">{r.type}</span></td>
                    <td><span className={`badge ${badgeClass(r.outcome)}`}>{r.outcome}</span></td>
                  </tr>
                );
              })}
              {paddingBottom > 0 && (
                <tr>
                  <td colSpan={7} style={{ height: paddingBottom, padding: 0, border: "none" }} />
                </tr>
              )}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}
