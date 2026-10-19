import {
  ResponsiveContainer,
  LineChart,
  Line,
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  CartesianGrid,
  Legend,
  PieChart,
  Pie,
  Cell,
} from "recharts";
import type { Timeseries } from "../api";
import { bucketLabel, dayLabel } from "../format";

const COLORS = ["#2f6fed", "#1a9e5c", "#d1414a", "#e0a72a", "#8b5cf6", "#0ea5e9"];

function labelFor(bucket: "15min" | "day") {
  return bucket === "day" ? dayLabel : bucketLabel;
}

export function ProfitChart({ data, bucket }: { data: Timeseries["profit"]; bucket: "15min" | "day" }) {
  const fmt = labelFor(bucket);
  if (data.length === 0) return <div className="empty">No closed trades yet.</div>;
  return (
    <div className="chart-wrap">
      <ResponsiveContainer>
        <LineChart data={data.map((d) => ({ ...d, label: fmt(d.bucket) }))}>
          <CartesianGrid strokeDasharray="3 3" opacity={0.2} />
          <XAxis dataKey="label" fontSize={11} />
          <YAxis fontSize={11} />
          <Tooltip formatter={(v) => `₹${Number(v ?? 0).toFixed(0)}`} />
          <Line type="monotone" dataKey="cumulative" name="Cumulative P&L" stroke={COLORS[0]} dot={false} strokeWidth={2} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

export function NewsVolumeChart({ data, bucket }: { data: Timeseries["newsVolume"]; bucket: "15min" | "day" }) {
  const fmt = labelFor(bucket);
  const byBucket = new Map<string, Record<string, number>>();
  for (const row of data.news) {
    const key = row.bucket;
    const entry = byBucket.get(key) ?? {};
    entry[row.event_type ?? "unclassified"] = row.n;
    byBucket.set(key, entry);
  }
  for (const row of data.filings) {
    const key = row.bucket;
    const entry = byBucket.get(key) ?? {};
    entry["nse_filing"] = row.n;
    byBucket.set(key, entry);
  }
  const eventTypes = Array.from(new Set(data.news.map((r) => r.event_type ?? "unclassified")));
  const rows = Array.from(byBucket.entries())
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([bucketKey, vals]) => ({ label: fmt(bucketKey), ...vals }));

  if (rows.length === 0) return <div className="empty">No news/filing activity yet.</div>;
  return (
    <div className="chart-wrap">
      <ResponsiveContainer>
        <BarChart data={rows}>
          <CartesianGrid strokeDasharray="3 3" opacity={0.2} />
          <XAxis dataKey="label" fontSize={11} />
          <YAxis fontSize={11} allowDecimals={false} />
          <Tooltip />
          <Legend />
          {eventTypes.map((et, i) => (
            <Bar key={et} dataKey={et} stackId="a" fill={COLORS[i % COLORS.length]} />
          ))}
          <Bar dataKey="nse_filing" stackId="a" fill="#9797a3" name="NSE filing" />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

export function FunnelChart({ funnel }: { funnel: Timeseries["funnel"] }) {
  const stages = [
    { name: "Seen", value: funnel.seen },
    { name: "Resolved to ticker", value: funnel.resolvedToTicker },
    { name: "Auto-bought", value: funnel.autoBought },
    { name: "Queued", value: funnel.queued },
    { name: "Confirmed", value: funnel.confirmed },
    { name: "Rejected", value: funnel.rejected },
    // Same wording as the "Pending review" KPI/feed outcome, on purpose --
    // this IS that same live-unresolved count, just scoped to this funnel's
    // date range, whereas "Queued" above is a different thing (the total
    // that ever entered the queue in this range, including already-resolved
    // ones) -- don't conflate the two labels.
    { name: "Pending review", value: funnel.still_pending },
  ];
  return (
    <div className="chart-wrap">
      <ResponsiveContainer>
        <BarChart data={stages} layout="vertical" margin={{ left: 30 }}>
          <CartesianGrid strokeDasharray="3 3" opacity={0.2} />
          <XAxis type="number" fontSize={11} allowDecimals={false} />
          <YAxis type="category" dataKey="name" fontSize={11} width={110} />
          <Tooltip />
          <Bar dataKey="value" fill={COLORS[0]} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

export function CapitalChart({ data, bucket }: { data: Timeseries["capital"]; bucket: "15min" | "day" }) {
  const fmt = labelFor(bucket);
  if (data.length === 0) return <div className="empty">No capital deployed yet.</div>;
  return (
    <div className="chart-wrap">
      <ResponsiveContainer>
        <LineChart data={data.map((d) => ({ ...d, label: fmt(d.bucket) }))}>
          <CartesianGrid strokeDasharray="3 3" opacity={0.2} />
          <XAxis dataKey="label" fontSize={11} />
          <YAxis fontSize={11} />
          <Tooltip formatter={(v) => `₹${Number(v ?? 0).toFixed(0)}`} />
          <Line type="stepAfter" dataKey="cumulative" name="Capital deployed" stroke={COLORS[3]} dot={false} strokeWidth={2} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

export function BoughtSoldChart({ data, bucket }: { data: Timeseries["boughtSold"]; bucket: "15min" | "day" }) {
  const fmt = labelFor(bucket);
  const byBucket = new Map<string, { bought: number; sold: number }>();
  for (const r of data.bought) byBucket.set(r.bucket, { ...(byBucket.get(r.bucket) ?? { bought: 0, sold: 0 }), bought: r.n });
  for (const r of data.sold) byBucket.set(r.bucket, { ...(byBucket.get(r.bucket) ?? { bought: 0, sold: 0 }), sold: r.n });
  const rows = Array.from(byBucket.entries())
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([bucketKey, v]) => ({ label: fmt(bucketKey), ...v }));

  if (rows.length === 0) return <div className="empty">No trades yet.</div>;
  return (
    <div className="chart-wrap">
      <ResponsiveContainer>
        <BarChart data={rows}>
          <CartesianGrid strokeDasharray="3 3" opacity={0.2} />
          <XAxis dataKey="label" fontSize={11} />
          <YAxis fontSize={11} allowDecimals={false} />
          <Tooltip />
          <Legend />
          <Bar dataKey="bought" fill={COLORS[0]} name="Bought" />
          <Bar dataKey="sold" fill={COLORS[1]} name="Sold" />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

export function ExitReasonChart({ data }: { data: Timeseries["exitMix"] }) {
  if (data.length === 0) return <div className="empty">No closed trades yet.</div>;
  return (
    <div className="chart-wrap">
      <ResponsiveContainer>
        <PieChart>
          <Pie data={data} dataKey="n" nameKey="exit_reason" outerRadius={80} label>
            {data.map((_, i) => (
              <Cell key={i} fill={COLORS[i % COLORS.length]} />
            ))}
          </Pie>
          <Tooltip />
          <Legend />
        </PieChart>
      </ResponsiveContainer>
    </div>
  );
}
