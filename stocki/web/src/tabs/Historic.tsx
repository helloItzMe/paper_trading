import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api";
import { isoDateInput, yesterdayInput, inr } from "../format";
import PositionsTable from "../components/PositionsTable";
import SignalFeed from "../components/SignalFeed";
import ResultsCalendar from "../components/ResultsCalendar";
import { ProfitChart, NewsVolumeChart, FunnelChart, CapitalChart, BoughtSoldChart, ExitReasonChart } from "../charts/Charts";
import { DailyWinLossCountChart, DailyPnlBookedChart } from "../charts/DailyPnlCharts";

function daysAgo(n: number): Date {
  const d = new Date();
  d.setDate(d.getDate() - n);
  return d;
}

export default function Historic({ user }: { user: string }) {
  // Historic only covers days the Live tab doesn't already own -- up to
  // yesterday (Live's window can reach into the small hours of "today"
  // before the pre-market cutoff, see liveRange()), so both `max` and the
  // default `to` stop there rather than at today.
  const maxDate = yesterdayInput();
  const [fromDate, setFromDate] = useState(isoDateInput(daysAgo(30)));
  const [toDate, setToDate] = useState(maxDate);

  const from = new Date(fromDate).toISOString();
  const to = new Date(new Date(toDate).getTime() + 24 * 60 * 60 * 1000 - 1).toISOString();

  const closed = useQuery({ queryKey: ["closed", user, from, to], queryFn: () => api.closedPositions(user, from, to) });
  const signals = useQuery({ queryKey: ["signals", user, from, to], queryFn: () => api.signals(user, from, to) });
  const filings = useQuery({ queryKey: ["filings", user, from, to], queryFn: () => api.filings(user, from, to) });
  const ts = useQuery({ queryKey: ["timeseries", user, from, to], queryFn: () => api.timeseries(user, from, to, "day") });
  // All P&L math (net, wins/losses, profit/loss booked, win rate) is
  // computed server-side over this exact range -- see queries.ts's
  // getRangeSummary -- rather than reduced from `closed` here, so this
  // response is the thing to check first if a number ever looks wrong.
  const rangeSummary = useQuery({
    queryKey: ["range-summary", user, from, to],
    queryFn: () => api.rangeSummary(user, from, to),
  });
  // Not user-scoped, and keyed on the plain date-input strings (not the
  // ISO from/to above) -- results_calendar.declaration_date is a DATE,
  // matched directly against fromDate/toDate, same as Live's istDateInput
  // approach.
  const resultsCalendar = useQuery({
    queryKey: ["results-calendar", fromDate, toDate],
    queryFn: () => api.resultsCalendar(fromDate, toDate),
  });

  return (
    <>
      <div className="range-picker" style={{ marginBottom: 20 }}>
        <label>From <input type="date" value={fromDate} max={maxDate} onChange={(e) => setFromDate(e.target.value)} /></label>
        <label>To <input type="date" value={toDate} max={maxDate} onChange={(e) => setToDate(e.target.value)} /></label>
      </div>

      <div className="kpi-row">
        <div className="kpi">
          <div className="label">Total realized P&amp;L</div>
          <div className={`value ${(rangeSummary.data?.realizedPnl ?? 0) > 0 ? "positive" : (rangeSummary.data?.realizedPnl ?? 0) < 0 ? "negative" : ""}`}>
            {inr(rangeSummary.data?.realizedPnl ?? 0)}
          </div>
          {rangeSummary.data && (
            <div className="sub" style={{ marginTop: 4 }}>
              <span style={{ color: "var(--green)" }}>
                {rangeSummary.data.wins} win{rangeSummary.data.wins === 1 ? "" : "s"} ({inr(rangeSummary.data.profitBooked)})
              </span>
              {" · "}
              <span style={{ color: "var(--red)" }}>
                {rangeSummary.data.losses} loss{rangeSummary.data.losses === 1 ? "" : "es"} ({inr(rangeSummary.data.lossBooked)})
              </span>
            </div>
          )}
        </div>
        <div className="kpi">
          <div className="label">Total trades</div>
          <div className="value">{rangeSummary.data?.totalTrades ?? 0}</div>
          {rangeSummary.data && (
            <div className="sub">
              {rangeSummary.data.closedTrades} closed, {rangeSummary.data.openTrades} open
            </div>
          )}
        </div>
        <div className="kpi">
          <div className="label">Win rate</div>
          <div className="value">
            {rangeSummary.data?.winRate !== null && rangeSummary.data?.winRate !== undefined
              ? `${(rangeSummary.data.winRate * 100).toFixed(0)}%`
              : "—"}
          </div>
        </div>
      </div>

      <div className="grid">
        <div className="panel">
          <h3>Cumulative realized profit</h3>
          {ts.data && <ProfitChart data={ts.data.profit} bucket="day" />}
        </div>
        <div className="panel">
          <h3>Capital deployed</h3>
          {ts.data && <CapitalChart data={ts.data.capital} bucket="day" />}
        </div>
        <div className="panel">
          <h3>News &amp; filing volume</h3>
          {ts.data && <NewsVolumeChart data={ts.data.newsVolume} bucket="day" />}
        </div>
        <div className="panel">
          <h3>News → buy funnel</h3>
          {ts.data && <FunnelChart funnel={ts.data.funnel} />}
        </div>
        <div className="panel">
          <h3>Stocks bought vs sold</h3>
          {ts.data && <BoughtSoldChart data={ts.data.boughtSold} bucket="day" />}
        </div>
        <div className="panel">
          <h3>Exit-reason mix</h3>
          {ts.data && <ExitReasonChart data={ts.data.exitMix} />}
        </div>
        <div className="panel">
          <h3>Daily win/loss count</h3>
          <DailyWinLossCountChart data={ts.data?.profit} />
        </div>
        <div className="panel">
          <h3>Daily profit/loss booked</h3>
          <DailyPnlBookedChart data={ts.data?.profit} />
        </div>
      </div>

      <div className="panel" style={{ marginBottom: 20, overflowX: "auto" }}>
        <h3>Positions</h3>
        <PositionsTable positions={closed.data} signals={signals.data} filings={filings.data} />
      </div>
      <div className="panel" style={{ marginBottom: 20, overflowX: "auto" }}>
        <h3>Signal archive</h3>
        <SignalFeed signals={signals.data} filings={filings.data} />
      </div>
      <div className="panel" style={{ overflowX: "auto" }}>
        <h3>Results calendar</h3>
        <ResultsCalendar entries={resultsCalendar.data} signals={signals.data} filings={filings.data} />
      </div>
    </>
  );
}
