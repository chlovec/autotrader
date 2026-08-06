#!/bin/bash
# Starts the v2 stack directly - backend-v2's scheduled data-sync jobs (run_jobs.py)
# and the frontend-v2 Vite dashboard - without going through the Makefile. Runs
# stop-v2.sh first so leftover processes from a previous run can't collide with these.
# Mirrors bin/restart.sh's shape for v1; see that file for the fuller v1 stack
# (backend/trading loop/research) this doesn't have a v2 equivalent of yet.
#
# backend-v2 reads its own .env (see backend-v2/.env.example) - MASSIVE_API_KEY,
# BACKEND_V2_DATABASE_URL, etc. - separate from the root .env v1 uses. run_jobs.py is
# launched with its cwd set to backend-v2/ so python-dotenv's load_dotenv() (called
# with no arguments in data/client.py and db/session.py) finds that file instead of
# the root one.
#
# Usage (from anywhere - resolves the project root itself):
#   ./bin/restart-v2.sh [--skip-backend] [--skip-dashboard]
# With no flags, asks about each service one by one (Restart backend-v2? [Y/n], etc.)
# so you can leave either one untouched. A --skip-* flag answers that one service's
# question in advance (useful for scripting/automation) without being asked; any
# service not flagged is still asked interactively.
#
# Env overrides: DASHBOARD_V2_PORT (must match frontend-v2/vite.config.ts's
# strictPort - default 5174, distinct from v1's 5173 so both can run side by side).
# BACKEND_V2_PORT (must match backend-v2/.env's BACKEND_V2_PORT - default 8001, distinct
# from v1's BACKEND_PORT 8000) - run_jobs.py now serves a FastAPI app (job config/
# trigger endpoints for the dashboard's Jobs page) on this port, not just the scheduler.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$(cd "$SCRIPT_DIR/.." && pwd)"

PYTHON="$(pwd)/.venv/bin/python"
DASHBOARD_V2_PORT="${DASHBOARD_V2_PORT:-5174}"
BACKEND_V2_PORT="${BACKEND_V2_PORT:-8001}"

SKIP_BACKEND=0; BACKEND_SET=0
SKIP_DASHBOARD=0; DASHBOARD_SET=0

usage() {
  echo "Usage: $0 [--skip-backend] [--skip-dashboard]"
}

for arg in "$@"; do
  case "$arg" in
    --skip-backend) SKIP_BACKEND=1; BACKEND_SET=1 ;;
    --skip-dashboard) SKIP_DASHBOARD=1; DASHBOARD_SET=1 ;;
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

if [ "$BACKEND_SET" -eq 0 ]; then
  if ask_yes_no "Restart backend-v2 (run_jobs.py)?" "y"; then SKIP_BACKEND=0; else SKIP_BACKEND=1; fi
fi
if [ "$DASHBOARD_SET" -eq 0 ]; then
  if ask_yes_no "Restart dashboard-v2?" "y"; then SKIP_DASHBOARD=0; else SKIP_DASHBOARD=1; fi
fi

ACTIONS=()
if [ "$SKIP_BACKEND" -eq 0 ]; then ACTIONS+=("backend-v2"); fi
if [ "$SKIP_DASHBOARD" -eq 0 ]; then ACTIONS+=("dashboard-v2"); fi

if [ ${#ACTIONS[@]} -eq 0 ]; then
  echo "Nothing selected - not restarting anything."
  exit 0
fi

ACTIONS_JOINED="$(IFS=', '; echo "${ACTIONS[*]}")"
echo "Proceeding: ${ACTIONS_JOINED}."

# --yes: the per-service decisions above already got explicit consent, so stop-v2.sh
# shouldn't ask again - it just acts on the same --skip-* flags this script resolved.
STOP_FLAGS=("--yes")
if [ "$SKIP_BACKEND" -eq 1 ]; then STOP_FLAGS+=("--skip-backend"); fi
if [ "$SKIP_DASHBOARD" -eq 1 ]; then STOP_FLAGS+=("--skip-dashboard"); fi
"$SCRIPT_DIR/stop-v2.sh" "${STOP_FLAGS[@]}"

LOG="services-v2.log"
: > "$LOG"

JOBS_PID=""

if [ "$SKIP_BACKEND" -eq 0 ]; then
  echo "Starting backend-v2 (run_jobs.py)..."
  nohup bash -c "cd backend-v2 && exec \"$PYTHON\" run_jobs.py" >> "$LOG" 2>&1 &
  JOBS_PID=$!
  disown
else
  echo "Skipping backend-v2 (left as-is)."
fi

if [ "$SKIP_DASHBOARD" -eq 0 ]; then
  echo "Starting dashboard-v2..."
  nohup bash -c 'cd frontend-v2 && exec npm run dev' >> "$LOG" 2>&1 &
  disown
else
  echo "Skipping dashboard-v2 (left as-is)."
fi

# `nohup ... &` never surfaces the child's exit code - without this check, backend-v2
# crashing on startup (e.g. a missing MASSIVE_API_KEY) would still print as a
# successful restart below. run_jobs.py never legitimately exits on its own (it blocks
# forever once the scheduler starts), so it being dead already means it crashed.
if [ -n "$JOBS_PID" ]; then
  sleep 2
  if ! kill -0 "$JOBS_PID" 2>/dev/null; then
    echo "ERROR: backend-v2 (run_jobs.py) exited immediately - check $LOG:" >&2
    tail -n 20 "$LOG" >&2
    echo "Dashboard-v2 may still be running - ./bin/stop-v2.sh to tear everything down." >&2
    exit 1
  fi
fi

if [ "$SKIP_BACKEND" -eq 0 ] || [ "$SKIP_DASHBOARD" -eq 0 ]; then
  echo "--- listening ports ---"
fi
if [ "$SKIP_BACKEND" -eq 0 ]; then
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    backend_ports="$(lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null | grep -E ":$BACKEND_V2_PORT " || true)"
    [ -n "$backend_ports" ] && break
    sleep 1
  done
  if [ -n "$backend_ports" ]; then
    echo "$backend_ports"
  else
    echo "  backend-v2 still not listening on $BACKEND_V2_PORT - check $LOG"
  fi
  echo "Backend-v2 API (Jobs page, etc.): http://localhost:$BACKEND_V2_PORT"
fi
if [ "$SKIP_DASHBOARD" -eq 0 ]; then
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    ports="$(lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null | grep -E ":$DASHBOARD_V2_PORT " || true)"
    [ -n "$ports" ] && break
    sleep 1
  done
  if [ -n "$ports" ]; then
    echo "$ports"
  else
    echo "  still not listening - check $LOG"
  fi
  echo "Dashboard-v2: http://localhost:$DASHBOARD_V2_PORT"
fi
echo "Logs: tail -f $LOG"
