#!/bin/bash
# Takes an online, consistent snapshot of both of this repo's live SQLite databases -
# v1's ./autotrader.db and v2's ./backend-v2/backend_v2.db - via sqlite3's `.backup`
# command, which is safe to run while the app is actively reading/writing (unlike a
# plain `cp`, which can copy a torn/inconsistent file mid-write).
#
# Written after an incident where a careless `rm` on backend_v2.db destroyed the only
# copy of ~46k synced tickers' worth of OHLC bars, snapshots, and predictions, with no
# backup to fall back on - see CLAUDE.md's "Live databases" section. Neither database
# is ever supposed to be deleted/truncated to get a "clean" state - ask before doing
# either, and take a backup first regardless.
#
# Reads each database's path out of its own .env (DATABASE_URL / BACKEND_V2_DATABASE_URL),
# same resolution v1/v2's own db/session.py use - sqlite:/// URLs only, since that's all
# either stack is configured for today. Writes to
# backups/<db-file-name>.bak-<UTC timestamp>, matching .gitignore's existing
# *.db.bak-* pattern so a backup can never accidentally get committed.
#
# Usage: ./bin/backup-db.sh [--keep N]
# Prunes down to the N most recent backups *per database* after taking a new one
# (default 14) - pass --keep 0 to disable pruning entirely.

set -euo pipefail
cd "$(cd "$(dirname "$0")/.." && pwd)"

KEEP=14
while [ $# -gt 0 ]; do
  case "$1" in
    --keep)
      KEEP="$2"
      shift 2
      ;;
    -h|--help)
      echo "Usage: $0 [--keep N]"
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 1
      ;;
  esac
done

mkdir -p backups
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
STATUS=0

# $1 = directory to resolve the env file/DATABASE_URL relative to (also where the
# sqlite path itself is relative to, same as the app's own db/session.py), $2 = env
# var name, $3 = default sqlite:/// URL if the var is unset/the .env file is missing.
backup_one() {
  local dir="$1" var_name="$2" default_url="$3"
  local env_file="$dir/.env"
  local db_url="$default_url"
  if [ -f "$env_file" ]; then
    local found
    found="$(grep -E "^${var_name}=" "$env_file" | tail -n1 | cut -d= -f2-)"
    [ -n "$found" ] && db_url="$found"
  fi

  case "$db_url" in
    sqlite:///*)
      local db_rel_path="${db_url#sqlite:///}"
      ;;
    *)
      echo "SKIP: $var_name=$db_url is not a sqlite:/// URL - backup-db.sh only knows how to back up sqlite" >&2
      return 0
      ;;
  esac

  local db_path="$dir/$db_rel_path"
  if [ ! -f "$db_path" ]; then
    # Refuse rather than silently creating an empty database at the live path -
    # sqlite3 .backup on a nonexistent source would otherwise do exactly that.
    echo "SKIP: $db_path does not exist - nothing to back up" >&2
    return 0
  fi

  local db_name dest
  db_name="$(basename "$db_rel_path")"
  dest="backups/${db_name}.bak-${STAMP}"
  if sqlite3 "$db_path" ".backup '$dest'"; then
    echo "Backed up $db_path -> $dest"
  else
    echo "FAILED to back up $db_path" >&2
    STATUS=1
    return 0
  fi

  if [ "$KEEP" -gt 0 ]; then
    find backups -maxdepth 1 -name "${db_name}.bak-*" -type f 2>/dev/null \
      | sort -r \
      | tail -n +"$((KEEP + 1))" \
      | while IFS= read -r old; do
          rm -f "$old"
          echo "Pruned old backup: $old"
        done
  fi
}

backup_one "." "DATABASE_URL" "sqlite:///./autotrader.db"
backup_one "backend-v2" "BACKEND_V2_DATABASE_URL" "sqlite:///./backend_v2.db"

exit "$STATUS"
