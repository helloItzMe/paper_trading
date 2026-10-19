import type { Health } from "../api";
import { timeAgo } from "../format";

// A run is "degraded" if it errored, or if it's still running (no
// finished_at) well past a reasonable single-pass duration -- distinguishes
// "still going" from "died silently", which is exactly the gap that made
// pipeline health unobservable before scan_runs existed.
//
// A run that's still going is only judged against this threshold using
// last_heartbeat_at when one exists -- a long-running --watch/--tick session
// (monitor --tick especially, meant to stay alive for days with zero open
// positions as its normal idle state) heartbeats every cycle specifically so
// "still alive" doesn't age out just because started_at is old. Rows from
// before last_heartbeat_at existed, or short-lived one-shot runs that
// finish within a single cycle, fall back to started_at, same as before.
const STALE_RUNNING_MS = 10 * 60 * 1000;

export default function HealthStrip({ health }: { health: Health | undefined }) {
  if (!health) return null;

  const tokenStatus = health.systemStatus.find((s) => s.key === "upstox_token");
  // Stale (not just !ok) also means "not currently proven working" -- if
  // nothing has run recently enough to make a live Upstox call at all, the
  // last recorded result could be hours old and says nothing about right now.
  const tokenStale = tokenStatus && Date.now() - new Date(tokenStatus.checked_at).getTime() > STALE_RUNNING_MS;
  const tokenBad = tokenStatus && (!tokenStatus.ok || tokenStale);

  if (health.latest.length === 0) {
    return (
      <div className="health-strip">
        <div className="health-item degraded">
          <div className="cmd">no runs recorded yet</div>
          <div className="meta">run `trade_bot.py scan` or `monitor` at least once</div>
        </div>
      </div>
    );
  }
  return (
    <div className="health-strip">
      {tokenBad && (
        <div className="health-item degraded">
          <div className="cmd">Upstox token</div>
          <div className="meta">
            {tokenStale
              ? `no live check in ${timeAgo(tokenStatus!.checked_at)} -- nothing has run recently enough to prove it still works`
              : `last check failed ${timeAgo(tokenStatus!.checked_at)}: ${tokenStatus!.detail ?? "unknown error"}`}
          </div>
          <div className="meta" style={{ color: "var(--red)" }}>
            run `./.venv/bin/python broker/upstox_client.py login`
          </div>
        </div>
      )}
      {health.latest.map((run) => {
        const running = run.finished_at === null;
        const lastSeen = run.last_heartbeat_at ?? run.started_at;
        const stale = running && Date.now() - new Date(lastSeen).getTime() > STALE_RUNNING_MS;
        const degraded = !!run.error || stale;
        return (
          <div key={run.command} className={`health-item ${degraded ? "degraded" : ""}`}>
            <div className="cmd">{run.command}</div>
            <div className="meta">
              last started {timeAgo(run.started_at)}
              {running
                ? (stale ? " — still running, looks stuck" : run.last_heartbeat_at ? ` — running (heartbeat ${timeAgo(run.last_heartbeat_at)})` : " — running")
                : ` — finished ${timeAgo(run.finished_at)}`}
            </div>
            {run.error && <div className="meta" style={{ color: "var(--red)" }}>error: {run.error}</div>}
            {run.command === "scan" && !run.error && (
              <div className="meta">
                {/* run.queued is how many THIS run sent to review -- a point-in-time
                    action count, not a live "still pending" status (some may have
                    since been confirmed/rejected) -- don't relabel this as "pending
                    review", that phrase is reserved for the live-unresolved count. */}
                {run.signals_seen ?? 0} seen, {run.positions_opened ?? 0} opened, {run.queued ?? 0} sent to review
              </div>
            )}
            {run.command === "monitor" && !run.error && (
              <div className="meta">{run.signals_seen ?? 0} checked, {run.queued ?? 0} closed</div>
            )}
          </div>
        );
      })}
    </div>
  );
}
