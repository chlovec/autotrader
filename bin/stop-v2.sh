#!/bin/bash
# Stops the v2 processes this repo can start: backend-v2's scheduled data-sync jobs
# (run_jobs.py) and the frontend-v2 Vite dashboard dev server. Matches by command line
# (pgrep -f) rather than tracking PIDs, so it works no matter how the processes were
# started (restart-v2.sh, run by hand, `make backend-v2`, ...). Mirrors bin/stop.sh's
# shape for v1; entirely independent of it - stopping v2 never touches v1's backend,
# dashboard, trading loop, or research run, and vice versa.
#
# Usage: ./bin/stop-v2.sh [--skip-backend] [--skip-dashboard] [--yes]
# With no --skip-*/--yes flags, asks about each service one by one (Stop backend-v2?
# [Y/n], etc.) so you can leave either one running. A --skip-* flag answers that one
# service's question in advance without being asked; --yes suppresses all prompts
# (including the final "stop these processes?" list confirmation) - restart-v2.sh uses
# --yes since it already asked the same questions itself before calling here.

set -euo pipefail
cd "$(cd "$(dirname "$0")/.." && pwd)"

SKIP_BACKEND=0; BACKEND_SET=0
SKIP_DASHBOARD=0; DASHBOARD_SET=0
ASSUME_YES=0

usage() {
  echo "Usage: $0 [--skip-backend] [--skip-dashboard] [--yes]"
}

for arg in "$@"; do
  case "$arg" in
    --skip-backend) SKIP_BACKEND=1; BACKEND_SET=1 ;;
    --skip-dashboard) SKIP_DASHBOARD=1; DASHBOARD_SET=1 ;;
    --yes) ASSUME_YES=1 ;;
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

if [ "$ASSUME_YES" -eq 0 ]; then
  if [ "$BACKEND_SET" -eq 0 ]; then
    if ask_yes_no "Stop backend-v2 (run_jobs.py)?" "y"; then SKIP_BACKEND=0; else SKIP_BACKEND=1; fi
  fi
  if [ "$DASHBOARD_SET" -eq 0 ]; then
    if ask_yes_no "Stop dashboard-v2?" "y"; then SKIP_DASHBOARD=0; else SKIP_DASHBOARD=1; fi
  fi
fi

# Not anchored to this repo's absolute path - run_jobs.py is commonly launched with a
# relative path (cwd already at backend-v2/), which wouldn't match an absolute-path
# pattern.
PATTERNS=()
if [ "$SKIP_BACKEND" -eq 0 ]; then PATTERNS+=("run_jobs\.py"); fi
if [ "$SKIP_DASHBOARD" -eq 0 ]; then PATTERNS+=("frontend-v2/node_modules/.bin/vite"); fi

if [ ${#PATTERNS[@]} -eq 0 ]; then
  echo "Nothing selected - not stopping anything."
  exit 0
fi

collect_pids() {
  for pattern in "${PATTERNS[@]}"; do
    pgrep -f "$pattern" 2>/dev/null || true
  done | sort -u
}

pids="$(collect_pids)"

if [ -z "$pids" ]; then
  echo "Nothing running (for the services in scope)."
  exit 0
fi

echo "Stopping:"
ps -o pid,command -p $pids 2>/dev/null | tail -n +2 | sed 's/^/  /'

if [ "$ASSUME_YES" -eq 0 ]; then
  read -r -p "Stop these processes? [y/N] " reply
  case "$reply" in
    [yY]|[yY][eE][sS]) ;;
    *)
      echo "Aborted - nothing stopped."
      exit 0
      ;;
  esac
fi

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
