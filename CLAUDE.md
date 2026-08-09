# CLAUDE.md

Guidance for Claude Code (and other agents) working in this repo.

## Live databases — never delete, truncate, or reset

- `./autotrader.db` (v1) and `./backend-v2/backend_v2.db` (v2) are live, gitignored
  SQLite databases holding real synced/computed data — tickers, OHLC bars, snapshots,
  predictions, backtests, job config/history. They are **not** disposable build or test
  artifacts, even though they're gitignored and look like local cruft.
- Never run `rm`, `TRUNCATE`, `DROP TABLE`, or any bulk-delete against either file (or
  its tables) to "get a clean state." Tests never need this: `backend-v2/tests/conftest.py`
  already redirects `BACKEND_V2_DATABASE_URL` to an isolated tempfile before any test
  module is collected, so a clean pytest run never touches the real v2 database. v1's
  test setup should be checked the same way before assuming it needs a live DB reset.
- If a database genuinely needs to be reset or migrated (e.g. schema rework), take a
  backup first via `./bin/backup-db.sh`, and confirm with the user before proceeding —
  don't infer "safe to delete" from a file being gitignored or absent from `git status`.
- Backups run automatically (see `bin/backup-db.sh` and the `launchd` job that
  schedules it — `com.autotrader.backup-db`) into `./backups/`, pruned to the last 14
  per database. Run `./bin/backup-db.sh` manually before anything risky.

This note exists because an agent once ran `rm -f backend_v2.db` to "clean up" before a
test run, not realizing it was the live database and that the cleanup was unnecessary —
destroying ~46k synced tickers' worth of data with no backup in place. Don't repeat it.
