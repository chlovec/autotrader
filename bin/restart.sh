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

# Build the broker-specific CLI args for the trading loop (and research run below) -
# passed as flags to load_config() rather than env vars, so a plain `ps` doesn't need to
# guess what a running process was launched with.
RUN_ARGS=(--broker "$BROKER")
case "$BROKER" in
  alpaca) RUN_ARGS+=(--alpaca-paper $([ "$MODE" = "sim" ] && echo true || echo false)) ;;
  ibkr) RUN_ARGS+=(--ibkr-port $([ "$MODE" = "sim" ] && echo "$IBKR_SIM_PORT" || echo "$IBKR_LIVE_PORT")) ;;
  questrade) ;;  # sim vs live is just which QUESTRADE_REFRESH_TOKEN is configured
esac

# The backend (uvicorn) can't be handed the same CLI flags - it's not our entrypoint, so
# there's nowhere to pass --broker/--ibkr-port through to. Its load_config(argv=[]) only
# ever reads the environment/.env, so without this export it would silently keep
# whatever broker/mode was last configured there - completely ignoring the selection
# above - and the dashboard would show a different broker/account than the trading loop
# and research run actually use.
export BROKER="$BROKER"
case "$BROKER" in
  alpaca) export ALPACA_PAPER=$([ "$MODE" = "sim" ] && echo true || echo false) ;;
  ibkr) export IBKR_PORT=$([ "$MODE" = "sim" ] && echo "$IBKR_SIM_PORT" || echo "$IBKR_LIVE_PORT") ;;
  questrade) ;;
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

echo "Starting trading loop (${RUN_ARGS[*]}, $RUN_SCRIPT)..."
nohup "$PYTHON" "$RUN_SCRIPT" "${RUN_ARGS[@]}" >> "$LOG" 2>&1 &
RUN_PID=$!
disown

echo "Running research once..."
nohup "$PYTHON" run_research.py "${RUN_ARGS[@]}" >> "$LOG" 2>&1 &
RESEARCH_PID=$!
disown

# `nohup ... &` never surfaces the child's exit code - without this check, a trading
# loop that crashes on startup (e.g. live mode with no live keys configured, see
# engine/config.py) would still print as a successful restart below. run_portfolio.py
# and run.py never legitimately exit on their own (they block forever in a scheduler)
# and a real research pass takes several seconds minimum (one HTTP round-trip per
# symbol), so either process being dead already means it crashed on startup.
sleep 2
FAILED=0
if ! kill -0 "$RUN_PID" 2>/dev/null; then
  echo "ERROR: trading loop ($RUN_SCRIPT) exited immediately - check $LOG:" >&2
  tail -n 20 "$LOG" >&2
  FAILED=1
fi
if ! kill -0 "$RESEARCH_PID" 2>/dev/null; then
  echo "ERROR: research run exited immediately - check $LOG:" >&2
  tail -n 20 "$LOG" >&2
  FAILED=1
fi
if [ "$FAILED" -eq 1 ]; then
  echo "Backend/dashboard are still running - ./bin/stop.sh to tear everything down." >&2
  exit 1
fi

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
