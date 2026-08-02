#!/bin/bash
# Starts the full local dev stack directly - FastAPI backend, Vite dashboard, the
# trading loop, and one research run - without going through the Makefile. Runs
# stop.sh first so leftover processes from a previous run can't collide with these.
#
# Usage (from anywhere - resolves the project root itself):
#   ./bin/restart.sh                  # prompts for broker, then live/sim
#   ./bin/restart.sh alpaca sim
#   ./bin/restart.sh ibkr live
#   ./bin/restart.sh alpaca           # broker given, still prompts for live/sim
#
# Env overrides (same names the Makefile uses): RUN_SCRIPT, BACKEND_HOST, BACKEND_PORT,
# IBKR_SIM_PORT, IBKR_LIVE_PORT.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$(cd "$SCRIPT_DIR/.." && pwd)"

PYTHON=.venv/bin/python
UVICORN=.venv/bin/uvicorn
RUN_SCRIPT="${RUN_SCRIPT:-run_portfolio.py}"
BACKEND_HOST="${BACKEND_HOST:-127.0.0.1}"
BACKEND_PORT="${BACKEND_PORT:-8000}"
DASHBOARD_PORT="${DASHBOARD_PORT:-5173}"
IBKR_SIM_PORT="${IBKR_SIM_PORT:-7497}"
IBKR_LIVE_PORT="${IBKR_LIVE_PORT:-7496}"

BROKER="${1:-}"
MODE="${2:-}"

if [ -z "$BROKER" ]; then
  PS3="Which broker? "
  select choice in alpaca ibkr questrade; do
    if [ -n "$choice" ]; then
      BROKER="$choice"
      break
    fi
    echo "Pick 1, 2, or 3."
  done
fi

case "$BROKER" in
  alpaca|ibkr|questrade) ;;
  *)
    echo "Unknown broker '$BROKER' - must be alpaca, ibkr, or questrade." >&2
    exit 1
    ;;
esac

if [ -z "$MODE" ]; then
  PS3="Live or sim? "
  select choice in sim live; do
    if [ -n "$choice" ]; then
      MODE="$choice"
      break
    fi
    echo "Pick 1 or 2."
  done
fi

case "$MODE" in
  sim|live) ;;
  *)
    echo "Unknown mode '$MODE' - must be sim or live." >&2
    exit 1
    ;;
esac

# Build the broker-specific env for the trading loop.
BROKER_ENV=(BROKER="$BROKER")
case "$BROKER" in
  alpaca) BROKER_ENV+=(ALPACA_PAPER=$([ "$MODE" = "sim" ] && echo true || echo false)) ;;
  ibkr) BROKER_ENV+=(IBKR_PORT=$([ "$MODE" = "sim" ] && echo "$IBKR_SIM_PORT" || echo "$IBKR_LIVE_PORT")) ;;
  questrade) ;;  # sim vs live is just which QUESTRADE_REFRESH_TOKEN is configured
esac

echo "About to (re)start backend, dashboard, trading loop (broker=$BROKER, mode=$MODE), and a research run."
read -r -p "Continue? [y/N] " reply
case "$reply" in
  [yY]|[yY][eE][sS]) ;;
  *)
    echo "Aborted - nothing started."
    exit 0
    ;;
esac

"$SCRIPT_DIR/stop.sh"

LOG="services.log"
: > "$LOG"

echo "Starting backend..."
nohup "$UVICORN" backend.app.main:app --host "$BACKEND_HOST" --port "$BACKEND_PORT" >> "$LOG" 2>&1 &
disown

echo "Starting dashboard..."
nohup bash -c 'cd frontend && exec npm run dev' >> "$LOG" 2>&1 &
disown

echo "Starting trading loop (${BROKER_ENV[*]}, $RUN_SCRIPT)..."
nohup env "${BROKER_ENV[@]}" "$PYTHON" "$RUN_SCRIPT" >> "$LOG" 2>&1 &
disown

echo "Running research once..."
nohup "$PYTHON" run_research.py >> "$LOG" 2>&1 &
disown

echo "--- listening ports ---"
for _ in 1 2 3 4 5 6 7 8 9 10; do
  ports="$(lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null | grep -E ":($DASHBOARD_PORT|$BACKEND_PORT) " || true)"
  both_up="$(echo "$ports" | grep -c -E ":($DASHBOARD_PORT|$BACKEND_PORT) " || true)"
  [ "$both_up" -ge 2 ] && break
  sleep 1
done
if [ -n "$ports" ]; then
  echo "$ports"
else
  echo "  still not listening - check $LOG"
fi
echo "Backend:   http://$BACKEND_HOST:$BACKEND_PORT"
echo "Dashboard: http://localhost:$DASHBOARD_PORT"
echo "Logs: tail -f $LOG"
