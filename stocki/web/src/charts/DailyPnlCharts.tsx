import { ResponsiveContainer, BarChart, Bar, XAxis, YAxis, Tooltip, CartesianGrid, Legend } from "recharts";
import type { Timeseries } from "../api";
import { dayLabel, inr } from "../format";

const WIN_COLOR = "#1a9e5c";
const LOSS_COLOR = "#d1414a";

// Both charts read straight off ts.data.profit (bucket=day) -- wins/losses/
// profitBooked/lossBooked are computed server-side per bucket (queries.ts's
// getProfitSeries), not re-derived here, so the API response is the full
// explanation if a number here ever looks wrong.
type ProfitSeries = Timeseries["profit"];

export function DailyWinLossCountChart({ data }: { data: ProfitSeries | undefined }) {
  if (!data || data.length === 0) return <div className="empty">No closed trades in this range.</div>;
  const rows = data.map((d) => ({ ...d, label: dayLabel(d.bucket) }));
  return (
    <div className="chart-wrap">
      <ResponsiveContainer>
        <BarChart data={rows}>
          <CartesianGrid strokeDasharray="3 3" opacity={0.2} />
          <XAxis dataKey="label" fontSize={11} />
          <YAxis fontSize={11} allowDecimals={false} />
          <Tooltip />
          <Legend />
          <Bar dataKey="wins" name="Wins" fill={WIN_COLOR} />
          <Bar dataKey="losses" name="Losses" fill={LOSS_COLOR} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

export function DailyPnlBookedChart({ data }: { data: ProfitSeries | undefined }) {
  if (!data || data.length === 0) return <div className="empty">No closed trades in this range.</div>;
  const rows = data.map((d) => ({ ...d, label: dayLabel(d.bucket) }));
  return (
    <div className="chart-wrap">
      <ResponsiveContainer>
        <BarChart data={rows}>
          <CartesianGrid strokeDasharray="3 3" opacity={0.2} />
          <XAxis dataKey="label" fontSize={11} />
          <YAxis fontSize={11} />
          <Tooltip formatter={(v) => inr(Number(v ?? 0))} />
          <Legend />
          <Bar dataKey="profitBooked" name="Profit booked" fill={WIN_COLOR} />
          <Bar dataKey="lossBooked" name="Loss booked" fill={LOSS_COLOR} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
