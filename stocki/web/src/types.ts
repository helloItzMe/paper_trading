export type RefreshStatus = {
  // Timestamp (ms) of the most recent successful settle across all of the
  // Live tab's polled queries -- null until the very first fetch resolves.
  lastSuccessAt: number | null;
  // False if any polled query is currently in an error state, so a poll
  // that actually failed can be told apart from one that's merely pending.
  ok: boolean;
};
