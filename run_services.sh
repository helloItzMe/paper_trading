#!/bin/bash
# Interactive launcher for this project's background services.
# Usage: ./run_services.sh          (shows menu)
#        ./run_services.sh 1|2|3|4  (skips menu, runs that option directly)

set -u
cd "$(dirname "$0")"

# npm/node live under Homebrew, not always on PATH in non-interactive shells.
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

PG_VERSION="postgresql@13"        # investment_bot -- bound to :5432
TIMESCALE_PG_VERSION="postgresql@18"  # investment_features (tick capture) -- bound to :5433
RUN_DIR=".run"
mkdir -p "$RUN_DIR" logs

start_postgres() {
  echo "== postgres (investment_bot, :5432) =="
  if pg_isready -q -h localhost -p 5432; then
    echo "already running"
  else
    brew services start "$PG_VERSION"
    for i in $(seq 1 10); do
      pg_isready -q -h localhost -p 5432 && { echo "up"; break; }
      sleep 1
    done
  fi

  echo "== postgres (investment_features / TimescaleDB, :5433) =="
  if pg_isready -q -h localhost -p 5433; then
    echo "already running"
  else
    brew services start "$TIMESCALE_PG_VERSION"
    for i in $(seq 1 10); do
      pg_isready -q -h localhost -p 5433 && { echo "up"; break; }
      sleep 1
    done
  fi
}

start_scan_monitor() {
  echo "== scan --watch + monitor --tick =="

  # Marker so we can tell trade_bot.py's own logs/<command>_<started-at>.log
  # files apart from stale ones left over from earlier runs (ls -t alone
  # would happily return an old file if the new one hasn't been created yet).
  marker="$RUN_DIR/.log_marker"
  touch "$marker"

  nohup ./.venv/bin/python trading/trade_bot.py scan --watch \
    > /dev/null 2>&1 &
  echo $! > "$RUN_DIR/scan.pid"
  echo "scan --watch started (pid $(cat "$RUN_DIR/scan.pid"))"

  nohup ./.venv/bin/python trading/trade_bot.py monitor --tick \
    > /dev/null 2>&1 &
  echo $! > "$RUN_DIR/monitor.pid"
  echo "monitor --tick started (pid $(cat "$RUN_DIR/monitor.pid"))"

  scan_log=""
  monitor_log=""
  for i in $(seq 1 10); do
    [ -z "$scan_log" ] && scan_log=$(find logs -name 'scan_*.log' -newer "$marker" 2>/dev/null | sort | tail -1)
    [ -z "$monitor_log" ] && monitor_log=$(find logs -name 'monitor_*.log' -newer "$marker" 2>/dev/null | sort | tail -1)
    [ -n "$scan_log" ] && [ -n "$monitor_log" ] && break
    sleep 1
  done
  rm -f "$marker"

  echo
  echo "follow the logs with:"
  [ -n "$scan_log" ] && echo "  tail -f $scan_log" || echo "  (scan log not found yet -- ls -t logs/scan_*.log | head -1)"
  [ -n "$monitor_log" ] && echo "  tail -f $monitor_log" || echo "  (monitor log not found yet -- ls -t logs/monitor_*.log | head -1)"
}

start_stoki() {
  echo "== stocki dashboard =="
  if ! command -v npm >/dev/null 2>&1; then
    echo "npm not found even after adding Homebrew paths -- aborting stocki start"
    return 1
  fi

  (cd stocki/server && nohup npm run dev > /tmp/stocki_server.log 2>&1 &
    echo $! > "../../$RUN_DIR/stocki_server.pid")
  echo "server starting (pid $(cat "$RUN_DIR/stocki_server.pid")) -- log: /tmp/stocki_server.log"

  (cd stocki/web && nohup npm run dev > /tmp/stocki_web.log 2>&1 &
    echo $! > "../../$RUN_DIR/stocki_web.pid")
  echo "web starting (pid $(cat "$RUN_DIR/stocki_web.pid")) -- log: /tmp/stocki_web.log"

  echo "dashboard will be at http://localhost:5173 once both are up"
}

run_choice() {
  case "$1" in
    1) start_scan_monitor ;;
    2) start_postgres ;;
    3) start_stoki ;;
    4) start_postgres; start_scan_monitor; start_stoki ;;
    *) echo "invalid choice: $1"; exit 1 ;;
  esac
}

if [ "${1:-}" != "" ]; then
  run_choice "$1"
  exit 0
fi

echo "What do you want to run?"
echo "  1) scan + monitor (trade_bot.py scan --watch / monitor --tick)"
echo "  2) postgres"
echo "  3) stocki dashboard (server + web)"
echo "  4) all of the above"
read -rp "> " choice
run_choice "$choice"
