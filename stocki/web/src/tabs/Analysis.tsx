import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api";
import type { AnalysisBacktestRow } from "../api";
import { istDateInput, istTime, inr, pct, nseQuoteUrl } from "../format";
import CopyButton from "../components/CopyButton";

function exitBadgeClass(reason: string | null): string {
  if (reason === "target") return "ok";
  if (reason === "stop") return "bad";
  if (reason === "no_data") return "muted";
  return "warn"; // 'eod'
}

// One hypothetical trade per row, capital_per_trade-sized (matches
// trade_bot.py's real per-trade sizing) -- lets the totals row show a
// rupee amount, not just an average percentage that hides how many
// symbols actually contributed.
const HYPOTHETICAL_CAPITAL_PER_TRADE = 10000;

function confidenceBadgeClass(confidence: string | null): string {
  return confidence === "manual" ? "warn" : "ok";
}

function Table({ rows, includeManual }: { rows: AnalysisBacktestRow[]; includeManual: boolean }) {
  const shown = includeManual ? rows : rows.filter((r) => r.confidence !== "manual");
  if (shown.length === 0) return <div className="empty">No qualifying signal this basis.</div>;
  const rowsToRender = shown;

  const realized = rowsToRender.filter((r) => r.exit_reason !== "no_data" && r.pnl_pct !== null);
  const totalPnlPct = realized.reduce((sum, r) => sum + Number(r.pnl_pct), 0);
  const totalPnlAmount = realized.reduce((sum, r) => sum + (Number(r.pnl_pct) / 100) * HYPOTHETICAL_CAPITAL_PER_TRADE, 0);

  return (
    <table>
      <thead>
        <tr>
          <th>Symbol</th>
          <th>Company</th>
          <th>Confidence</th>
          <th>Entry time</th>
          <th>Entry price</th>
          <th>Exit time</th>
          <th>Exit price</th>
          <th>Exit reason</th>
          <th>P&amp;L %</th>
          <th>Headline</th>
        </tr>
      </thead>
      <tbody>
        {rowsToRender.map((r) => (
          <tr key={`${r.symbol}-${r.entry_mode}`}>
            <td>
              <a href={nseQuoteUrl(r.symbol)} target="_blank" rel="noreferrer" title={`Open ${r.symbol} on NSE`}>
                {r.symbol}
              </a>
              <CopyButton text={r.symbol} />
            </td>
            <td className="truncate-1" title={r.company ?? undefined}>{r.company ?? "—"}</td>
            <td><span className={`badge ${confidenceBadgeClass(r.confidence)}`}>{r.confidence ?? "—"}</span></td>
            <td>{istTime(r.entry_time)}</td>
            <td>{r.entry_price !== null ? inr(r.entry_price) : "—"}</td>
            <td>{r.exit_time ? istTime(r.exit_time) : "—"}</td>
            <td>{r.exit_price !== null ? inr(r.exit_price) : "—"}</td>
            <td><span className={`badge ${exitBadgeClass(r.exit_reason)}`}>{r.exit_reason ?? "—"}</span></td>
            <td className={r.pnl_pct !== null ? (Number(r.pnl_pct) >= 0 ? "positive" : "negative") : undefined}>
              {r.pnl_pct !== null ? pct(r.pnl_pct) : "—"}
            </td>
            <td className="truncate-1" title={r.headline ?? undefined}>
              {r.link ? (
                <a href={r.link} target="_blank" rel="noreferrer">{r.headline}</a>
              ) : (
                r.headline ?? "—"
              )}
            </td>
          </tr>
        ))}
      </tbody>
      <tfoot>
        <tr>
          <td colSpan={8}>
            <strong>Total ({realized.length} realized of {rowsToRender.length}, at {inr(HYPOTHETICAL_CAPITAL_PER_TRADE)}/trade)</strong>
          </td>
          <td colSpan={2}>
            <strong className={totalPnlAmount >= 0 ? "positive" : "negative"}>
              {pct(totalPnlPct)} · {inr(totalPnlAmount)}
            </strong>
          </td>
        </tr>
      </tfoot>
    </table>
  );
}

export default function Analysis() {
  const [date, setDate] = useState(istDateInput(new Date()));
  // Off by default: 'manual'-confidence signals (order-category items
  // never get this label; earnings-category ones do whenever
  // profit_pct/revenue_pct wouldn't have cleared classifier's own
  // confidence bar -- see analysis/backtest.py's _confidence_for()) are
  // ones the real pipeline would only buy after a human ran `confirm`, not
  // automatically -- so by default they're excluded from both the row
  // list and the total, same as they'd be excluded from a real auto-buy
  // count. Toggling this on treats them as if auto-approved instead, for
  // both tables at once (one shared toggle, not per-table) since the
  // question ("what if manual review always said yes") is the same one
  // either way the entry is clocked.
  const [includeManual, setIncludeManual] = useState(false);

  const backtest = useQuery({
    queryKey: ["analysis-backtest", date],
    queryFn: () => api.analysisBacktest(date),
  });

  const rows = backtest.data ?? [];
  const bySeen = rows.filter((r) => r.entry_mode === "first_seen");
  const byPub = rows.filter((r) => r.entry_mode === "pub_date");

  return (
    <>
      <div className="range-picker" style={{ marginBottom: 20 }}>
        <label>Date <input type="date" value={date} onChange={(e) => setDate(e.target.value)} /></label>
        <label>
          <input
            type="checkbox"
            checked={includeManual}
            onChange={(e) => setIncludeManual(e.target.checked)}
          />
          {" "}Include waiting for approval (manual-confidence signals, as if auto-approved)
        </label>
      </div>

      <div className="panel" style={{ marginBottom: 20, overflowX: "auto" }}>
        <h3>If bought when we first saw the news (first_seen_at)</h3>
        <Table rows={bySeen} includeManual={includeManual} />
      </div>
      <div className="panel" style={{ overflowX: "auto" }}>
        <h3>If bought when the article was actually published (pub_date)</h3>
        <Table rows={byPub} includeManual={includeManual} />
      </div>
    </>
  );
}
