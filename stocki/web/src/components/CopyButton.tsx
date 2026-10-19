import { useState } from "react";

// Small inline "copy to clipboard" button -- meant to sit right next to a
// symbol (PositionsTable, SignalFeed) so a user can grab the exact ticker
// without selecting/triple-clicking text. Self-contained: owns its own
// "just copied" flash, no parent state needed.
export default function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);

  async function handleCopy(e: React.MouseEvent) {
    e.preventDefault();
    e.stopPropagation();
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1200);
    } catch {
      // Clipboard API unavailable (e.g. non-HTTPS context, permission
      // denied) -- nothing else this button can usefully do, fail silently.
    }
  }

  return (
    <button className="copy-btn" onClick={handleCopy} title={`Copy ${text}`} aria-label={`Copy ${text}`}>
      {copied ? "✓" : "⧉"}
    </button>
  );
}
