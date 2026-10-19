// Single source of truth for the Live tab's poll cadence. 10s, not slower:
// `monitor --watch` (which closes positions and writes realized P&L) is
// routinely run at 60s or faster, so the dashboard needs to track that, not
// `scan --watch`'s much slower 300s default. See stocki/README.md.
export const POLL_MS = 10_000;
