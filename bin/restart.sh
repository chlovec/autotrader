#!/bin/bash
# Starts the full local dev stack directly - FastAPI backend, Vite dashboard, the
# multi-account trading loop, and one research run - without going through the
# Makefile. Runs stop.sh first so leftover processes from a previous run can't
# collide with these.
#
# Which broker(s)/paper-vs-live each account uses comes entirely from .env's
# ACCOUNT_IDS/ACCOUNT_<id>_* vars (see .env.example) - there's no per-run broker
# selection anymore, since one run now trades every active account, possibly across
# several different brokers at once.
#
# Usage (from anywhere - resolves the project root itself): ./bin/restart.sh
#
# Env overrides (same names the Makefile uses): BACKEND_HOST, BACKEND_PORT.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$(cd "$SCRIPT_DIR/.." && pwd)"

PYTHON=.venv/bin/python
UVICORN=.venv/bin/uvicorn
BACKEND_HOST="${BACKEND_HOST:-127.0.0.1}"
BACKEND_PORT="${BACKEND_PORT:-8000}"
DASHBOARD_PORT="${DASHBOARD_PORT:-5173}"

# A blanket warning rather than a per-broker one: any configured account could be live -
# for Alpaca that's just whichever ALPACA_BASE_URL the account was given (paper vs live
# are different hosts, see engine/brokers/alpaca_broker.py's _is_paper_endpoint; no
# separate paper/live flag to check). Grep .env rather than trying to evaluate every
# account's config in bash.
LIVE_WARNING=""
if grep -qE '_ALPACA_BASE_URL=https://api\.alpaca\.markets' .env 2>/dev/null; then
  LIVE_WARNING=" - at least one account looks configured for LIVE trading"
fi

echo "About to (re)start backend, dashboard, trading loop, and a research run${LIVE_WARNING}."
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

echo "Starting trading loop (run_engine.py, every active account)..."
nohup "$PYTHON" run_engine.py >> "$LOG" 2>&1 &
RUN_PID=$!
disown

echo "Running research once..."
nohup "$PYTHON" run_research.py >> "$LOG" 2>&1 &
RESEARCH_PID=$!
disown

# `nohup ... &` never surfaces the child's exit code - without this check, a trading
# loop that crashes on startup (e.g. an account configured for live trading with no
# live keys, see engine/config.py) would still print as a successful restart below.
# run_engine.py never legitimately exits on its own (it blocks forever in a scheduler)
# and a real research pass takes several seconds minimum (one HTTP round-trip per
# symbol), so either process being dead already means it crashed on startup.
sleep 2
FAILED=0
if ! kill -0 "$RUN_PID" 2>/dev/null; then
  echo "ERROR: trading loop (run_engine.py) exited immediately - check $LOG:" >&2
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
