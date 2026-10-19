"""Watch NSE's global corporate-announcements feed (one API call, not
per-symbol) every 5s, and fire a macOS notification the moment any company
on TODAY's results_calendar (db.get_results_calendar_for_date) files its
results -- reuses nse_bot.is_results_filing()'s real "is this actually a
results filing" check (Integrated Filing- Financial, or Outcome of Board
Meeting whose attachment text says 'financial results'), not a symbol-only
match, so an unrelated same-day filing from a results-calendar company
(e.g. an analyst-call transcript) doesn't false-trigger.

Runs until killed -- unlike a single-symbol/single-shot watch, today's
calendar has 118 companies that can each file at any point during market
hours, so this keeps watching after the first hit.

Usage:
    python3 watch_results_today.py              # watch all symbols on today's calendar
    python3 watch_results_today.py KRN          # watch only KRN
"""
import shlex
import subprocess
import sys
import time
import datetime

sys.path.insert(0, "/Users/ashishsaini/workspace/investment")
import db  # noqa: E402
import news.nse_bot as nse_bot  # noqa: E402

POLL_SECONDS = 5
NOTIFY_AFTER = datetime.time(12, 30)


def _filed_after_cutoff(exchdisstime):
    try:
        dt = datetime.datetime.strptime(exchdisstime, "%d-%b-%Y %H:%M:%S")
    except (TypeError, ValueError):
        return False
    return dt.time() > NOTIFY_AFTER


TERMINAL_NOTIFIER = "/opt/homebrew/bin/terminal-notifier"


def notify(title, msg, symbol=None, open_url=None):
    if symbol and open_url:
        # -execute runs on click instead of -open, so we can both copy the
        # symbol to the clipboard AND open the filed PDF in one action.
        click_cmd = f"printf '%s' {shlex.quote(symbol)} | pbcopy && open {shlex.quote(open_url)}"
        subprocess.run([
            TERMINAL_NOTIFIER, "-title", title, "-message", msg,
            "-execute", click_cmd, "-sound", "Glass",
        ])
    elif open_url:
        subprocess.run([
            TERMINAL_NOTIFIER, "-title", title, "-message", msg,
            "-open", open_url, "-sound", "Glass",
        ])
    else:
        subprocess.run([
            "osascript", "-e",
            f'display notification "{msg}" with title "{title}" sound name "Glass"',
        ])


def main(filter_symbol=None):
    today = datetime.date.today()
    calendar_rows = db.get_results_calendar_for_date(today)
    symbols = {r["symbol"]: r for r in calendar_rows}

    if filter_symbol:
        symbols = {k: v for k, v in symbols.items() if k == filter_symbol}
        if not symbols:
            print(f"[{datetime.datetime.now():%H:%M:%S}] {filter_symbol} not in today's calendar")
            return

    print(f"[{datetime.datetime.now():%H:%M:%S}] watching {len(symbols)} "
          f"symbols scheduled to report today ({today})")

    seen_seq_ids = set()
    remaining = set(symbols)

    while remaining:
        try:
            items = nse_bot.fetch_announcements()
        except Exception as e:
            print(f"[{datetime.datetime.now():%H:%M:%S}] fetch failed: {e}")
            time.sleep(POLL_SECONDS)
            continue

        for item in items:
            seq_id = item.get("seq_id")
            symbol = item.get("symbol")
            if seq_id in seen_seq_ids:
                continue
            seen_seq_ids.add(seq_id)

            if symbol not in remaining:
                continue
            if not nse_bot.is_results_filing(item):
                continue
            if not _filed_after_cutoff(item.get("exchdisstime")):
                continue

            calendar_row = symbols[symbol]
            quarter = calendar_row.get("quarter") or "?"
            msg = (f"{item.get('sm_name', symbol)} ({symbol}) filed results "
                   f"for {quarter} at {item.get('exchdisstime')}")
            notify("NSE results filed", msg, symbol=symbol, open_url=item.get("attchmntFile"))
            print(f"[{datetime.datetime.now():%H:%M:%S}] NOTIFIED: {msg} "
                  f"(pdf: {item.get('attchmntFile')})")
            remaining.discard(symbol)

        if remaining:
            time.sleep(POLL_SECONDS)

    print(f"[{datetime.datetime.now():%H:%M:%S}] all {len(symbols)} "
          f"symbols have filed -- done.")


if __name__ == "__main__":
    filter_symbol = sys.argv[1] if len(sys.argv) > 1 else None
    main(filter_symbol)
