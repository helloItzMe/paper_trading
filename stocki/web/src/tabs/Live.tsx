import { useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api";
import { POLL_MS } from "../constants";
import { liveRange, istDateInput } from "../format";
import KpiRow from "../components/KpiRow";
import HealthStrip from "../components/HealthStrip";
import PositionsTable from "../components/PositionsTable";
import SignalFeed from "../components/SignalFeed";
import ResultsCalendar from "../components/ResultsCalendar";
import { ProfitChart, NewsVolumeChart, FunnelChart, CapitalChart, BoughtSoldChart } from "../charts/Charts";
import type { RefreshStatus } from "../types";

export default function Live({ user, onRefresh }: { user: string; onRefresh: (status: RefreshStatus) => void }) {
  const { from, to } = liveRange();
  // results_calendar.declaration_date is a DATE, not a timestamp -- "today"
  // for this widget means today's IST calendar date specifically, not
  // liveRange()'s pre-market-cutoff trading-day window (which can span into
  // the small hours of the next day). Matches the calendar to the actual
  // declaration date, independent of when we happened to see any news/filing.
  const today = istDateInput(new Date());

  const summary = useQuery({ queryKey: ["summary", user], queryFn: () => api.summary(user), refetchInterval: POLL_MS });
  const health = useQuery({ queryKey: ["health", user], queryFn: () => api.health(user), refetchInterval: POLL_MS });
  // Deliberately NOT keying these on from/to: liveRange()'s `to` is
  // `now.toISOString()`, recomputed fresh on every render (this tab
  // re-renders every second, from the header clock) -- a query keyed on it
  // would look like a brand-new, never-fetched query on every render and
  // `data` would never have a chance to settle. `user` is enough of a key;
  // queryFn still closes over the latest from/to, and refetchInterval calls
  // that latest closure on each poll tick.
  const positions = useQuery({
    queryKey: ["live-positions", user],
    queryFn: () => api.livePositions(user, from, to),
    refetchInterval: POLL_MS,
  });
  const signals = useQuery({
    queryKey: ["signals-today", user],
    queryFn: () => api.signals(user, from, to),
    refetchInterval: POLL_MS,
  });
  const filings = useQuery({
    queryKey: ["filings-today", user],
    queryFn: () => api.filings(user, from, to),
    refetchInterval: POLL_MS,
  });
  const ts = useQuery({
    queryKey: ["timeseries-today", user],
    queryFn: () => api.timeseries(user, from, to, "15min"),
    refetchInterval: POLL_MS,
  });
  const resultsCalendar = useQuery({
    queryKey: ["results-calendar-today", today],
    queryFn: () => api.resultsCalendar(today, today),
    refetchInterval: POLL_MS,
  });

  // Report the actual latest settle across every polled query up to the
  // header, rather than assuming a poll tick == a successful refresh --
  // `dataUpdatedAt` only advances on a query that actually resolved, so a
  // hung/failing request can't fake a "just refreshed" moment.
  const queries = [summary, health, positions, signals, filings, ts, resultsCalendar];
  const lastSuccessAt = Math.max(0, ...queries.map((q) => q.dataUpdatedAt)) || null;
  const ok = queries.every((q) => !q.isError);
  useEffect(() => {
    onRefresh({ lastSuccessAt, ok });
  }, [lastSuccessAt, ok]);

  return (
    <>
      <HealthStrip health={health.data} />
      <KpiRow summary={summary.data} />
      <div className="grid">
        <div className="panel">
          <h3>Cumulative realized profit</h3>
          {ts.data && <ProfitChart data={ts.data.profit} bucket="15min" />}
        </div>
        <div className="panel">
          <h3>Capital deployed</h3>
          {ts.data && <CapitalChart data={ts.data.capital} bucket="15min" />}
        </div>
        <div className="panel">
          <h3>News &amp; filing volume</h3>
          {ts.data && <NewsVolumeChart data={ts.data.newsVolume} bucket="15min" />}
        </div>
        <div className="panel">
          <h3>News → buy funnel</h3>
          {ts.data && <FunnelChart funnel={ts.data.funnel} />}
        </div>
        <div className="panel">
          <h3>Stocks bought vs sold</h3>
          {ts.data && <BoughtSoldChart data={ts.data.boughtSold} bucket="15min" />}
        </div>
      </div>
      <div className="panel" style={{ marginBottom: 20, overflowX: "auto" }}>
        <h3>Positions</h3>
        <PositionsTable positions={positions.data} signals={signals.data} filings={filings.data} />
      </div>
      <div className="panel" style={{ marginBottom: 20, overflowX: "auto" }}>
        <h3>Results calendar (today)</h3>
        <ResultsCalendar entries={resultsCalendar.data} signals={signals.data} filings={filings.data} />
      </div>
      <div className="panel" style={{ overflowX: "auto" }}>
        <h3>Today's signal feed</h3>
        <SignalFeed signals={signals.data} filings={filings.data} />
      </div>
    </>
  );
}
