#!/bin/bash
# Poll NSE's integrated-filing API for a symbol every 5s. Notify (macOS terminal
# notification) the first time we see a results filing.
# Usage: ./watch_iolcp_filing.sh [SYMBOL]
# Default: IOLCP
# Ctrl+C to stop.

SYMBOL="${1:-IOLCP}"
URL="https://www.nseindia.com/api/NextApi/apiClient/GetQuoteApi?functionName=getIntegratedFilingData&symbol=$SYMBOL"
UA='Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:153.0) Gecko/20100101 Firefox/153.0'

notify() {
  local msg="$1"
  osascript -e "display notification \"$msg\" with title \"$SYMBOL filing update\" sound name \"Glass\""
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] NOTIFIED: $msg"
}

echo "Watching $SYMBOL filings every 5s. Ctrl+C to stop."

while true; do
  resp=$(curl --location "$URL" \
    --header "User-Agent: $UA" \
    --header 'Accept: */*' \
    --header 'Accept-Language: en-US,en;q=0.9' \
    --compressed -s)

  if [ -n "$resp" ]; then
    hit=$(echo "$resp" | python3 -c '
import sys, json
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for row in data:
    systym = row.get("gfSystym") or ""
    qtr = row.get("gfrQuaterEnded") or ""
    cons = row.get("gfrConsolidated") or ""
    if "10-Aug" in systym or qtr == "30 Jun 2026":
        print(f"{cons}|{qtr}|{systym}")
        break
')
    if [ -n "$hit" ]; then
      cons=$(echo "$hit" | cut -d'|' -f1)
      qtr=$(echo "$hit" | cut -d'|' -f2)
      systym=$(echo "$hit" | cut -d'|' -f3)
      notify "IOLCP: $cons filing for Q ended $qtr, filed $systym"
      break
    fi
  fi

  echo "[$(date '+%H:%M:%S')] no match yet"
  sleep 5
done
