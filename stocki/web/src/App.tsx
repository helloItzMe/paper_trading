import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "./api";
import { clock } from "./format";
import { POLL_MS } from "./constants";
import Live from "./tabs/Live";
import Historic from "./tabs/Historic";
import Analysis from "./tabs/Analysis";
import Logo from "./components/Logo";
import type { RefreshStatus } from "./types";

export default function App() {
  const [tab, setTab] = useState<"live" | "historic" | "analysis">("live");
  const [user, setUser] = useState<string | null>(null);
  const [now, setNow] = useState(new Date());

  const users = useQuery({ queryKey: ["users"], queryFn: api.users });

  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(t);
  }, []);

  // Sourced from Live.tsx's actual query results (dataUpdatedAt/isError), not
  // a fixed timer -- so the countdown/flash reflect whether a poll really
  // landed, not just that POLL_MS elapsed.
  const [refresh, setRefresh] = useState<RefreshStatus>({ lastSuccessAt: null, ok: true });
  const pollSeconds = POLL_MS / 1000;
  const secondsToRefresh = refresh.lastSuccessAt
    ? Math.max(0, pollSeconds - Math.floor((now.getTime() - refresh.lastSuccessAt) / 1000))
    : null;

  const userOptions = users.data && users.data.length > 0 ? users.data : ["ashish"];

  // Keep the selection valid as the real user list loads/changes -- default
  // to 'ashish' when it actually has rows, otherwise fall back to whatever
  // does exist (e.g. 'tester'-only during development). Without this, a
  // stale/absent selection silently queries a user with no data and every
  // panel looks empty with no indication why.
  useEffect(() => {
    if (user !== null && userOptions.includes(user)) return;
    setUser(userOptions.includes("ashish") ? "ashish" : userOptions[0]);
  }, [users.data]);

  return (
    <>
      <div className="header">
        <div className="brand">
          <Logo />
          <h1>stocki</h1>
        </div>
        <div className="tabs">
          <button className={tab === "live" ? "active" : ""} onClick={() => setTab("live")}>Live</button>
          <button className={tab === "historic" ? "active" : ""} onClick={() => setTab("historic")}>Historic</button>
          <button className={tab === "analysis" ? "active" : ""} onClick={() => setTab("analysis")}>Analysis</button>
        </div>
        <div className="controls">
          <select value={user ?? ""} onChange={(e) => setUser(e.target.value)}>
            {userOptions.map((u) => (
              <option key={u} value={u}>{u}</option>
            ))}
          </select>
          <span className="as-of">as of {clock(now)}</span>
          {tab === "live" && (
            <span className={"as-of" + (!refresh.ok ? " refresh-error" : "")}>
              {!refresh.ok
                ? "refresh failed, retrying…"
                : secondsToRefresh !== null
                  ? `next refresh in ${secondsToRefresh}s`
                  : "refreshing…"}
              {refresh.ok && refresh.lastSuccessAt && (
                // key forces remount on every successful refresh, replaying the flash animation
                <span key={refresh.lastSuccessAt} className="refresh-flash" />
              )}
            </span>
          )}
        </div>
      </div>
      {user &&
        (tab === "live" ? (
          <Live user={user} onRefresh={setRefresh} />
        ) : tab === "historic" ? (
          <Historic user={user} />
        ) : (
          <Analysis />
        ))}
    </>
  );
}
