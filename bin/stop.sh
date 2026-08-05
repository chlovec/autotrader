#!/bin/bash
# Stops every autotrader process this repo can start: the FastAPI backend, the Vite
# dashboard dev server, the trading loop (run_engine.py), and a one-shot research run
# if still in flight. Matches by command line (pgrep -f) rather than tracking PIDs, so
# it works no matter how the processes were started (restart.sh, run by hand, `make
# run-engine`, ...) - independent of the Makefile, but still catches Makefile-launched
# processes since `make` execs into these same underlying commands.
#
# Usage: ./bin/stop.sh [--skip-backend] [--skip-dashboard] [--skip-engine] [--skip-research] [--yes]
# With no --skip-*/--yes flags, asks about each service one by one (Stop backend?
# [Y/n], etc.) so you can leave any of them running. A --skip-* flag answers that one
# service's question in advance without being asked; --yes suppresses all prompts
# (including the final "stop these processes?" list confirmation) - restart.sh uses
# --yes since it already asked the same questions itself before calling here.

set -euo pipefail
cd "$(cd "$(dirname "$0")/.." && pwd)"

SKIP_BACKEND=0; BACKEND_SET=0
SKIP_DASHBOARD=0; DASHBOARD_SET=0
SKIP_ENGINE=0; ENGINE_SET=0
SKIP_RESEARCH=0; RESEARCH_SET=0
ASSUME_YES=0

usage() {
  echo "Usage: $0 [--skip-backend] [--skip-dashboard] [--skip-engine] [--skip-research] [--yes]"
}

for arg in "$@"; do
  case "$arg" in
    --skip-backend) SKIP_BACKEND=1; BACKEND_SET=1 ;;
    --skip-dashboard) SKIP_DASHBOARD=1; DASHBOARD_SET=1 ;;
    --skip-engine) SKIP_ENGINE=1; ENGINE_SET=1 ;;
    --skip-research) SKIP_RESEARCH=1; RESEARCH_SET=1 ;;
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
    if ask_yes_no "Stop backend?" "y"; then SKIP_BACKEND=0; else SKIP_BACKEND=1; fi
  fi
  if [ "$DASHBOARD_SET" -eq 0 ]; then
    if ask_yes_no "Stop dashboard?" "y"; then SKIP_DASHBOARD=0; else SKIP_DASHBOARD=1; fi
  fi
  if [ "$ENGINE_SET" -eq 0 ]; then
    if ask_yes_no "Stop trading loop (run_engine.py)?" "y"; then SKIP_ENGINE=0; else SKIP_ENGINE=1; fi
  fi
  if [ "$RESEARCH_SET" -eq 0 ]; then
    if ask_yes_no "Stop research run (run_research.py)?" "y"; then SKIP_RESEARCH=0; else SKIP_RESEARCH=1; fi
  fi
fi

# Not anchored to this repo's absolute path - run_engine.py is commonly launched with a
# relative path (cwd already at the project root), which wouldn't match an
# absolute-path pattern.
PATTERNS=()
if [ "$SKIP_BACKEND" -eq 0 ]; then PATTERNS+=("uvicorn backend.app.main:app"); fi
if [ "$SKIP_DASHBOARD" -eq 0 ]; then PATTERNS+=("frontend/node_modules/.bin/vite"); fi
if [ "$SKIP_ENGINE" -eq 0 ]; then PATTERNS+=("run_engine\.py"); fi
if [ "$SKIP_RESEARCH" -eq 0 ]; then PATTERNS+=("run_research\.py"); fi

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
