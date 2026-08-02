#!/bin/bash
# Stops every autotrader process this repo can start: the FastAPI backend, the Vite
# dashboard dev server, the trading loop (run_portfolio.py / run.py), and a one-shot
# research run if still in flight. Matches by command line (pgrep -f) rather than
# tracking PIDs, so it works no matter how the processes were started (restart.sh, run
# by hand, an old `make run-alpaca-sim` from before these scripts existed, ...) -
# independent of the Makefile, but still catches Makefile-launched processes since
# `make` execs into these same underlying commands.
#
# Usage: ./bin/stop.sh (from anywhere - resolves the project root itself)

set -euo pipefail
cd "$(cd "$(dirname "$0")/.." && pwd)"

# Not anchored to this repo's absolute path - RUN_SCRIPT/run_portfolio.py are commonly
# launched with a relative path (cwd already at the project root), which wouldn't
# match an absolute-path pattern.
PATTERNS=(
  "uvicorn backend.app.main:app"
  "frontend/node_modules/.bin/vite"
  "run_portfolio\.py"
  "run_research\.py"
  "python.*/run\.py|python run\.py"  # RUN_SCRIPT=run.py override
)

collect_pids() {
  for pattern in "${PATTERNS[@]}"; do
    pgrep -f "$pattern" 2>/dev/null || true
  done | sort -u
}

pids="$(collect_pids)"

if [ -z "$pids" ]; then
  echo "Nothing running."
  exit 0
fi

echo "Stopping:"
ps -o pid,command -p $pids 2>/dev/null | tail -n +2 | sed 's/^/  /'

read -r -p "Stop these processes? [y/N] " reply
case "$reply" in
  [yY]|[yY][eE][sS]) ;;
  *)
    echo "Aborted - nothing stopped."
    exit 0
    ;;
esac

kill $pids 2>/dev/null || true

for _ in 1 2 3 4 5; do
  sleep 1
  pids="$(collect_pids)"
  [ -z "$pids" ] && break
done

pids="$(collect_pids)"
if [ -n "$pids" ]; then
  echo "Still alive after 5s, sending SIGKILL:"
  ps -o pid,command -p $pids 2>/dev/null | tail -n +2 | sed 's/^/  /'
  kill -9 $pids 2>/dev/null || true
fi

echo "Done."
