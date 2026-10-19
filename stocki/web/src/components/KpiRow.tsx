import type { Summary } from "../api";
import { inr, pct } from "../format";

export default function KpiRow({ summary }: { summary: Summary | undefined }) {
  if (!summary) return null;
  const pnlClass = summary.realizedPnlToday > 0 ? "positive" : summary.realizedPnlToday < 0 ? "negative" : "";
  return (
    <div className="kpi-row">
      <div className="kpi">
        <div className="label">Realized P&amp;L today</div>
        <div className={`value ${pnlClass}`}>{inr(summary.realizedPnlToday)}</div>
        <div className="sub">
          {summary.closedToday} closed{summary.winRateToday !== null ? `, ${pct(summary.winRateToday * 100, 0)} win rate` : ""}
        </div>
        <div className="sub" style={{ marginTop: 4 }}>
          <span style={{ color: "var(--green)" }}>
            {summary.winsToday} win{summary.winsToday === 1 ? "" : "s"} ({inr(summary.profitBookedToday)})
          </span>
          {" · "}
          <span style={{ color: "var(--red)" }}>
            {summary.lossesToday} loss{summary.lossesToday === 1 ? "" : "es"} ({inr(summary.lossBookedToday)})
          </span>
        </div>
      </div>
      <div className="kpi">
        <div className="label">Total trades</div>
        <div className="value">{summary.totalTradesToday}</div>
        <div className="sub">{summary.closedTradesToday} closed, {summary.openTradesToday} open</div>
      </div>
      <div className="kpi">
        <div className="label">Capital deployed</div>
        <div className="value">{inr(summary.capitalDeployed)}</div>
        <div className="sub">across {summary.openPositions} open position{summary.openPositions === 1 ? "" : "s"}</div>
      </div>
      <div className="kpi">
        <div className="label">Pending review</div>
        <div className="value">{summary.pendingReview}</div>
      </div>
      <div className="kpi">
        <div className="label">Queued (market closed)</div>
        <div className="value">{summary.queuedOrders}</div>
        <div className="sub">limit orders awaiting next market open</div>
      </div>
    </div>
  );
}
