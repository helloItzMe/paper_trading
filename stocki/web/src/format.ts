// Every symbol in this app IS a real, verified NSE trading symbol already
// (see broker/company_lookup.py -- the LLM never gets to invent a ticker,
// only ground a name against the real NSE instrument list), so linking
// straight to NSE's own official quote page is always a valid deep link,
// never a guess.
export function nseQuoteUrl(symbol: string): string {
  return `https://www.nseindia.com/get-quotes/equity?symbol=${encodeURIComponent(symbol)}`;
}

export function inr(n: number | string | null | undefined): string {
  if (n === null || n === undefined) return "—";
  const v = typeof n === "string" ? Number(n) : n;
  return v.toLocaleString("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 0 });
}

export function pct(n: number | string | null | undefined, digits = 1): string {
  if (n === null || n === undefined) return "—";
  const v = typeof n === "string" ? Number(n) : n;
  return `${v >= 0 ? "+" : ""}${v.toFixed(digits)}%`;
}

export function timeAgo(iso: string | null | undefined): string {
  if (!iso) return "—";
  const ms = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(ms / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ${mins % 60}m ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

export function istTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  const datePart = d.toLocaleDateString("en-IN", { timeZone: "Asia/Kolkata", day: "2-digit", month: "short" });
  const timePart = d.toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour12: false });
  return `${datePart}, ${timePart} IST`;
}

export function clock(d: Date): string {
  return d.toLocaleTimeString("en-IN", { hour12: false });
}

export function bucketLabel(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", hour12: false });
}

export function dayLabel(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleDateString("en-IN", { month: "short", day: "numeric" });
}

// Pre-market cutoff a new trading day "begins" at, for the Live tab's
// purposes -- NSE filings and news routinely land after the 15:30 close,
// so the previous session's activity is still "today" to a viewer checking
// in at, say, 2am. Only past this cutoff does a fresh calendar day count as
// the new "today".
const LIVE_CUTOFF_HOUR = 8;
const LIVE_CUTOFF_MINUTE = 30;

export function liveRange(): { from: string; to: string } {
  const now = new Date();
  const beforeCutoff =
    now.getHours() < LIVE_CUTOFF_HOUR ||
    (now.getHours() === LIVE_CUTOFF_HOUR && now.getMinutes() < LIVE_CUTOFF_MINUTE);

  const tradingDayStart = new Date(now);
  if (beforeCutoff) tradingDayStart.setDate(tradingDayStart.getDate() - 1);
  tradingDayStart.setHours(0, 0, 0, 0);

  const upperBound = new Date(tradingDayStart);
  upperBound.setDate(upperBound.getDate() + 1);
  upperBound.setHours(LIVE_CUTOFF_HOUR, LIVE_CUTOFF_MINUTE, 0, 0);

  const to = now < upperBound ? now : upperBound;
  return { from: tradingDayStart.toISOString(), to: to.toISOString() };
}

export function isoDateInput(d: Date): string {
  return d.toISOString().slice(0, 10);
}

// IST calendar date as 'YYYY-MM-DD' -- distinct from isoDateInput, which
// slices the UTC ISO string and can land on the wrong day for IST (UTC+5:30)
// evenings. results_calendar.declaration_date is NSE's own IST trading-day
// date, so matching "today" against it needs the IST date, not UTC's.
// en-CA's locale format is exactly 'YYYY-MM-DD'.
export function istDateInput(d: Date): string {
  return d.toLocaleDateString("en-CA", { timeZone: "Asia/Kolkata" });
}

// Historic only covers days Live doesn't already own -- up to yesterday, so
// the two tabs' windows never overlap or double-count a day's activity.
export function yesterdayInput(): string {
  const d = new Date();
  d.setDate(d.getDate() - 1);
  return isoDateInput(d);
}
