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
# Usage (from anywhere - resolves the project root itself):
#   ./bin/restart.sh [--skip-backend] [--skip-dashboard] [--skip-engine] [--skip-research]
# With no flags, asks about each service one by one (Restart backend? [Y/n], etc.) so
# you can leave any of them untouched - e.g. keep an in-progress research run alive
# while just bouncing the backend for a code change. A --skip-* flag answers that one
# service's question in advance (useful for scripting/automation) without being asked;
# any service not flagged is still asked interactively.
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

SKIP_BACKEND=0; BACKEND_SET=0
SKIP_DASHBOARD=0; DASHBOARD_SET=0
SKIP_ENGINE=0; ENGINE_SET=0
SKIP_RESEARCH=0; RESEARCH_SET=0

usage() {
  echo "Usage: $0 [--skip-backend] [--skip-dashboard] [--skip-engine] [--skip-research]"
}

for arg in "$@"; do
  case "$arg" in
    --skip-backend) SKIP_BACKEND=1; BACKEND_SET=1 ;;
    --skip-dashboard) SKIP_DASHBOARD=1; DASHBOARD_SET=1 ;;
    --skip-engine) SKIP_ENGINE=1; ENGINE_SET=1 ;;
    --skip-research) SKIP_RESEARCH=1; RESEARCH_SET=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; usage; exit 1 ;;
  esac
done

# $1 = prompt text, $2 = default ("y" or "n"). Blank Enter takes the default; anything
# starting with y/Y counts as yes, everything else as no.
ask_yes_no() {
  local prompt="$1" default="$2" hint reply
  if [ "$default" = "y" ]; then hint="[Y/n]"; else hint="[y/N]"; fi
  read -r -p "$prompt $hint " reply
  reply="${reply:-$default}"
  case "$reply" in
    [yY]|[yY][eE][sS]) return 0 ;;
    *) return 1 ;;
  esac
}

# A blanket warning rather than a per-broker one: any configured account could be live -
# for Alpaca that's just whichever ALPACA_BASE_URL the account was given (paper vs live
# are different hosts, see engine/brokers/alpaca_broker.py's _is_paper_endpoint; no
# separate paper/live flag to check). Grep .env rather than trying to evaluate every
# account's config in bash.
LIVE_WARNING=""
if grep -qE '_ALPACA_BASE_URL=https://api\.alpaca\.markets' .env 2>/dev/null; then
  LIVE_WARNING=" (at least one account looks configured for LIVE trading)"
fi

if [ "$BACKEND_SET" -eq 0 ]; then
  if ask_yes_no "Restart backend?" "y"; then SKIP_BACKEND=0; else SKIP_BACKEND=1; fi
fi
if [ "$DASHBOARD_SET" -eq 0 ]; then
  if ask_yes_no "Restart dashboard?" "y"; then SKIP_DASHBOARD=0; else SKIP_DASHBOARD=1; fi
fi
if [ "$ENGINE_SET" -eq 0 ]; then
  # Defaults to "no" (requires typing y) when live trading looks configured - every
  # other prompt here defaults to "yes" since bouncing the backend/dashboard/research is
  # cheap and reversible; restarting something that can place real orders isn't.
  engine_default="y"
  if [ -n "$LIVE_WARNING" ]; then engine_default="n"; fi
  if ask_yes_no "Restart trading loop (run_engine.py)?${LIVE_WARNING}" "$engine_default"; then SKIP_ENGINE=0; else SKIP_ENGINE=1; fi
fi
if [ "$RESEARCH_SET" -eq 0 ]; then
  if ask_yes_no "Run research (run_research.py)?" "y"; then SKIP_RESEARCH=0; else SKIP_RESEARCH=1; fi
fi

ACTIONS=()
if [ "$SKIP_BACKEND" -eq 0 ]; then ACTIONS+=("backend"); fi
if [ "$SKIP_DASHBOARD" -eq 0 ]; then ACTIONS+=("dashboard"); fi
if [ "$SKIP_ENGINE" -eq 0 ]; then ACTIONS+=("trading loop"); fi
if [ "$SKIP_RESEARCH" -eq 0 ]; then ACTIONS+=("a research run"); fi

if [ ${#ACTIONS[@]} -eq 0 ]; then
  echo "Nothing selected - not restarting anything."
  exit 0
fi

ACTIONS_JOINED="$(IFS=', '; echo "${ACTIONS[*]}")"
echo "Proceeding: ${ACTIONS_JOINED}."

# --yes: the per-service decisions above already got explicit consent, so stop.sh
# shouldn't ask again - it just acts on the same --skip-* flags this script resolved.
STOP_FLAGS=("--yes")
if [ "$SKIP_BACKEND" -eq 1 ]; then STOP_FLAGS+=("--skip-backend"); fi
if [ "$SKIP_DASHBOARD" -eq 1 ]; then STOP_FLAGS+=("--skip-dashboard"); fi
if [ "$SKIP_ENGINE" -eq 1 ]; then STOP_FLAGS+=("--skip-engine"); fi
if [ "$SKIP_RESEARCH" -eq 1 ]; then STOP_FLAGS+=("--skip-research"); fi
"$SCRIPT_DIR/stop.sh" "${STOP_FLAGS[@]}"

LOG="services.log"
: > "$LOG"

RUN_PID=""
RESEARCH_PID=""

if [ "$SKIP_BACKEND" -eq 0 ]; then
  echo "Starting backend..."
  nohup "$UVICORN" backend.app.main:app --host "$BACKEND_HOST" --port "$BACKEND_PORT" >> "$LOG" 2>&1 &
  disown
else
  echo "Skipping backend (left as-is)."
fi

if [ "$SKIP_DASHBOARD" -eq 0 ]; then
  echo "Starting dashboard..."
  nohup bash -c 'cd frontend && exec npm run dev' >> "$LOG" 2>&1 &
  disown
else
  echo "Skipping dashboard (left as-is)."
fi

if [ "$SKIP_ENGINE" -eq 0 ]; then
  echo "Starting trading loop (run_engine.py, every active account)..."
  nohup "$PYTHON" run_engine.py >> "$LOG" 2>&1 &
  RUN_PID=$!
  disown
else
  echo "Skipping trading loop (left as-is)."
fi

if [ "$SKIP_RESEARCH" -eq 0 ]; then
  echo "Running research once..."
  nohup "$PYTHON" run_research.py >> "$LOG" 2>&1 &
  RESEARCH_PID=$!
  disown
else
  echo "Skipping research run (left as-is)."
fi

# `nohup ... &` never surfaces the child's exit code - without this check, a trading
# loop that crashes on startup (e.g. an account configured for live trading with no
# live keys, see engine/config.py) would still print as a successful restart below.
# run_engine.py never legitimately exits on its own (it blocks forever in a scheduler)
# and a real research pass takes several seconds minimum (one HTTP round-trip per
# symbol), so either process being dead already means it crashed on startup. Only
# checked for services actually started this run - a skipped one was never given a PID.
sleep 2
FAILED=0
if [ -n "$RUN_PID" ] && ! kill -0 "$RUN_PID" 2>/dev/null; then
  echo "ERROR: trading loop (run_engine.py) exited immediately - check $LOG:" >&2
  tail -n 20 "$LOG" >&2
  FAILED=1
fi
if [ -n "$RESEARCH_PID" ] && ! kill -0 "$RESEARCH_PID" 2>/dev/null; then
  echo "ERROR: research run exited immediately - check $LOG:" >&2
  tail -n 20 "$LOG" >&2
  FAILED=1
fi
if [ "$FAILED" -eq 1 ]; then
  echo "Backend/dashboard are still running - ./bin/stop.sh to tear everything down." >&2
  exit 1
fi

echo "--- listening ports ---"
WANT_PORTS=0
if [ "$SKIP_DASHBOARD" -eq 0 ]; then WANT_PORTS=$((WANT_PORTS + 1)); fi
if [ "$SKIP_BACKEND" -eq 0 ]; then WANT_PORTS=$((WANT_PORTS + 1)); fi
if [ "$WANT_PORTS" -gt 0 ]; then
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    ports="$(lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null | grep -E ":($DASHBOARD_PORT|$BACKEND_PORT) " || true)"
    both_up="$(echo "$ports" | grep -c -E ":($DASHBOARD_PORT|$BACKEND_PORT) " || true)"
    [ "$both_up" -ge "$WANT_PORTS" ] && break
    sleep 1
  done
  if [ -n "$ports" ]; then
    echo "$ports"
  else
    echo "  still not listening - check $LOG"
  fi
fi
echo "Backend:   http://$BACKEND_HOST:$BACKEND_PORT"
echo "Dashboard: http://localhost:$DASHBOARD_PORT"
echo "Logs: tail -f $LOG"
