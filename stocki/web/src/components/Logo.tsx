// Ascending bar-chart glyph -- three bars trending up plus a trendline dot,
// reusing the app's own accent/green CSS variables (var(--accent)/var(--green)/
// var(--muted)) so it matches the current theme exactly, light or dark,
// instead of hardcoding colors the way the static public/favicon.svg has to
// (favicons load outside the app, no access to these variables -- that one
// uses its own @media (prefers-color-scheme) block to the same effect).
export default function Logo({ size = 28 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" aria-hidden="true">
      <rect width="32" height="32" rx="7" fill="var(--panel)" stroke="var(--border)" />
      <rect x="7" y="18" width="4" height="8" rx="1.2" fill="var(--muted)" />
      <rect x="14" y="12" width="4" height="14" rx="1.2" fill="var(--accent)" />
      <rect x="21" y="6" width="4" height="20" rx="1.2" fill="var(--green)" />
      <path
        d="M7 17.5 14 11.5 21 5.5"
        fill="none"
        stroke="var(--green)"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <circle cx="21" cy="5.5" r="1.6" fill="var(--green)" />
    </svg>
  );
}
