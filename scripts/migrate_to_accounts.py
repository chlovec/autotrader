"""One-off migration from the old single-account schema to the new multi-account one
(db.models.Account). No Alembic in this repo - schema changes normally just mean
Base.metadata.create_all(), which only *creates* missing tables and can't rename/backfill
columns on ones that already exist, so this script does that part by hand with raw SQL.

What it does, in order:
  1. Backs up autotrader.db and .env (timestamped copies, never overwritten).
  2. Reads the current single-account .env (BROKER, ALPACA_*/IBKR_*/QUESTRADE_*,
     MAX_POSITION_SIZE_USD, MAX_DAILY_LOSS_USD) and seeds one Account row from it - id
     defaults to the current BROKER value (e.g. "alpaca"), strategy defaults to
     rebalancing_portfolio with the SPY/TLT/GLD equal-weight split run_portfolio.py used to
     hardcode (docker-compose.yml's default RUN_SCRIPT), since that's what this deployment
     was actually running - edit the seeded account's strategy from the dashboard afterward
     if it was actually running run.py's signal strategy instead.
  3. Renames the old trades/equity_snapshots/signals/system_events tables aside, creates
     the new-shape ones (via Base.metadata.create_all), copies every row across with the
     seeded account's id attached, migrates the single kill_switch row onto that account,
     then drops the old tables.
  4. Rewrites .env to the new ACCOUNT_IDS / ACCOUNT_<id>_* shape, preserving every existing
     credential value under its new namespaced name.

Idempotent: if `accounts` already exists in the DB, step 3 is skipped (already migrated).
If .env already has ACCOUNT_IDS set, step 4 is skipped.

Usage (from project root): python -m scripts.migrate_to_accounts [--dry-run] [--account-id ID]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import sqlite3
from pathlib import Path

from dotenv import dotenv_values

from db.session import DATABASE_URL

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"

_DEFAULT_TARGET_WEIGHTS = {"SPY": 1 / 3, "TLT": 1 / 3, "GLD": 1 / 3}

# .env keys that carry over unchanged (not per-account, not renamed).
_GLOBAL_KEYS = [
    "DATABASE_URL",
    "MAX_POSITION_SIZE_USD",
    "MAX_DAILY_LOSS_USD",
    "QUESTRADE_POLL_INTERVAL_SECONDS",
    "BACKEND_HOST",
    "BACKEND_PORT",
    "CORS_ORIGINS",
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USERNAME",
    "SMTP_PASSWORD",
    "ALERT_EMAIL_FROM",
    "ALERT_EMAIL_TO",
]

# Old top-level key -> new ACCOUNT_<id>_ suffix, for whichever broker was configured.
# Alpaca isn't here - see _resolve_alpaca_pair, since the old paper/live pair-plus-flag
# shape resolves down to a single pair rather than a 1:1 key rename.
_ACCOUNT_KEY_SUFFIXES = {
    "IBKR_HOST": "IBKR_HOST",
    "IBKR_PORT": "IBKR_PORT",
    "IBKR_CLIENT_ID": "IBKR_CLIENT_ID",
    "QUESTRADE_REFRESH_TOKEN": "QUESTRADE_REFRESH_TOKEN",
}


def _resolve_alpaca_pair(values: dict) -> tuple[str, str, str]:
    """The pre-multi-account .env had a paper key pair, a live key pair, and an
    ALPACA_PAPER flag picking between them - the same redundant shape the new per-account
    AccountCredentials dropped (see engine/config.py's docstring: which environment an
    account hits is just whatever alpaca_base_url it's given, no separate flag). Resolves
    down to whichever one pair was actually active, since the new scheme only has room
    for one."""
    is_paper = values.get("ALPACA_PAPER", "true").lower() == "true"
    if is_paper:
        return (
            values.get("ALPACA_API_KEY", ""),
            values.get("ALPACA_SECRET_KEY", ""),
            values.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets"),
        )
    return (
        values.get("ALPACA_LIVE_API_KEY", ""),
        values.get("ALPACA_LIVE_SECRET_KEY", ""),
        values.get("ALPACA_LIVE_BASE_URL", "https://api.alpaca.markets"),
    )


def _sqlite_path(database_url: str) -> Path:
    if not database_url.startswith("sqlite:///"):
        raise RuntimeError(
            f"DATABASE_URL={database_url!r} isn't a local sqlite file - this script only knows how to "
            "migrate a sqlite database; adapt the raw-SQL steps below for your engine."
        )
    return (PROJECT_ROOT / database_url.removeprefix("sqlite:///")).resolve()


def _backup(path: Path) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d%H%M%S")
    backup_path = path.with_name(f"{path.name}.bak-{stamp}")
    shutil.copy2(path, backup_path)
    return backup_path


def _migrate_db(db_path: Path, account_id: str, broker: str, max_position_size_usd: float, max_daily_loss_usd: float, dry_run: bool) -> None:
    conn = sqlite3.connect(db_path)
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "accounts" in tables:
            print("accounts table already exists - DB already migrated, skipping.")
            return

        print(f"Migrating {db_path} (seeding account id={account_id!r}, broker={broker!r})...")
        if dry_run:
            print("--dry-run: no changes written.")
            return

        # Step 1 (its own transaction): rename the old tables aside and drop their
        # indexes. sqlite indexes are named at the database level, not scoped per table -
        # renaming a table doesn't rename its indexes, so create_all() in step 2 would
        # collide with e.g. ix_signals_timestamp still existing on signals_old. Dropped,
        # not renamed, since *_old is dropped in step 3 anyway once its rows are copied.
        try:
            renamed = set()
            for table in ("trades", "equity_snapshots", "signals", "system_events"):
                if table not in tables:
                    continue
                conn.execute(f"ALTER TABLE {table} RENAME TO {table}_old")
                renamed.add(table)
                for (index_name,) in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name = ? AND name NOT LIKE 'sqlite_autoindex_%'",
                    (f"{table}_old",),
                ).fetchall():
                    conn.execute(f"DROP INDEX {index_name}")
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        # Step 2: recreate every table current db.models declares that's still missing -
        # the four renamed above, plus the new `accounts` table. Deferred import: db.models
        # must be importable with the *new* schema already in code by the time this runs.
        # A separate engine/connection, closed immediately after, so it never overlaps
        # with `conn`'s own transactions (two simultaneous writers to one sqlite file
        # would otherwise risk "database is locked").
        from db.models import Base
        from sqlalchemy import create_engine

        engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(engine)
        engine.dispose()

        # Step 3 (its own transaction): seed the account, copy old rows across tagged with
        # its id, drop the *_old tables. If this fails, steps 1-2 already committed (the
        # database is left in a valid, if odd, "renamed but not yet copied" intermediate
        # state) - restore from the timestamped backup taken before this script ran and
        # re-run rather than trying to hand-patch it.
        try:
            now = dt.datetime.utcnow().isoformat(sep=" ")
            kill_switch_engaged, kill_switch_reason = 0, ""
            if "kill_switch" in tables:
                row = conn.execute("SELECT engaged, reason FROM kill_switch WHERE id = 1").fetchone()
                if row:
                    kill_switch_engaged, kill_switch_reason = row

            strategy_params = json.dumps({"target_weights": _DEFAULT_TARGET_WEIGHTS})
            conn.execute(
                "INSERT INTO accounts (id, broker, display_name, active, strategy_name, strategy_params, "
                "max_position_size_usd, max_daily_loss_usd, kill_switch_engaged, kill_switch_reason, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, 'rebalancing_portfolio', ?, ?, ?, ?, ?, ?, ?)",
                (account_id, broker, "Migrated Account", strategy_params, max_position_size_usd, max_daily_loss_usd,
                 kill_switch_engaged, kill_switch_reason, now, now),
            )

            if "signals" in renamed:
                conn.execute(
                    "INSERT INTO signals (id, timestamp, account_id, symbol, strategy_name, action, reason) "
                    "SELECT id, timestamp, ?, symbol, strategy_name, action, reason FROM signals_old",
                    (account_id,),
                )
            if "trades" in renamed:
                conn.execute(
                    "INSERT INTO trades (id, signal_id, account_id, broker, broker_account_id, broker_order_id, symbol, "
                    "side, qty, limit_price, fill_price, status, submitted_at, filled_at) "
                    "SELECT id, signal_id, ?, broker, account_id, broker_order_id, symbol, side, qty, limit_price, "
                    "fill_price, status, submitted_at, filled_at FROM trades_old",
                    (account_id,),
                )
            if "equity_snapshots" in renamed:
                conn.execute(
                    "INSERT INTO equity_snapshots (id, timestamp, account_id, broker, broker_account_id, equity, cash, buying_power) "
                    "SELECT id, timestamp, ?, broker, account_id, equity, cash, buying_power FROM equity_snapshots_old",
                    (account_id,),
                )
            if "system_events" in renamed:
                conn.execute(
                    "INSERT INTO system_events (id, timestamp, account_id, level, source, message) "
                    "SELECT id, timestamp, ?, level, source, message FROM system_events_old",
                    (account_id,),
                )

            for table in ("trades_old", "equity_snapshots_old", "signals_old", "system_events_old", "kill_switch"):
                conn.execute(f"DROP TABLE IF EXISTS {table}")

            conn.commit()
            print("DB migration complete.")
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()


def _rewrite_env(env_path: Path, account_id: str, dry_run: bool) -> None:
    if not env_path.exists():
        print(f"{env_path} not found, skipping .env rewrite.")
        return

    values = dotenv_values(env_path)
    if values.get("ACCOUNT_IDS"):
        print(".env already has ACCOUNT_IDS set - already migrated, skipping.")
        return

    broker = values.get("BROKER", "alpaca")
    lines = [f"ACCOUNT_IDS={account_id}", "", f"ACCOUNT_{account_id}_BROKER={broker}"]

    if broker == "alpaca":
        api_key, secret_key, base_url = _resolve_alpaca_pair(values)
        if api_key:
            lines.append(f"ACCOUNT_{account_id}_ALPACA_API_KEY={api_key}")
        if secret_key:
            lines.append(f"ACCOUNT_{account_id}_ALPACA_SECRET_KEY={secret_key}")
        if base_url:
            lines.append(f"ACCOUNT_{account_id}_ALPACA_BASE_URL={base_url}")

    for old_key, suffix in _ACCOUNT_KEY_SUFFIXES.items():
        if values.get(old_key):
            lines.append(f"ACCOUNT_{account_id}_{suffix}={values[old_key]}")
    lines.append("")

    # Research's news API key: old ALPACA_API_KEY/SECRET_KEY were already documented as
    # "used for news regardless of BROKER" - carry them forward as the new global pair.
    if values.get("ALPACA_API_KEY"):
        lines.append(f"ALPACA_NEWS_API_KEY={values['ALPACA_API_KEY']}")
    if values.get("ALPACA_SECRET_KEY"):
        lines.append(f"ALPACA_NEWS_SECRET_KEY={values['ALPACA_SECRET_KEY']}")
    lines.append("")

    for key in _GLOBAL_KEYS:
        if values.get(key):
            lines.append(f"{key}={values[key]}")

    new_content = "\n".join(lines) + "\n"

    print(f"\n--- new {env_path.name} ---\n{new_content}--- end ---\n")
    if dry_run:
        print("--dry-run: .env not written.")
        return

    backup_path = _backup(env_path)
    env_path.write_text(new_content)
    print(f"Wrote new {env_path} (original backed up to {backup_path}).")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", default=None, help="Defaults to the current BROKER value in .env")
    parser.add_argument("--dry-run", action="store_true", help="Print what would change without writing anything")
    args = parser.parse_args()

    env_values = dotenv_values(ENV_PATH) if ENV_PATH.exists() else {}
    broker = env_values.get("BROKER", "alpaca")
    account_id = args.account_id or broker
    max_position_size_usd = float(env_values.get("MAX_POSITION_SIZE_USD", "1000"))
    max_daily_loss_usd = float(env_values.get("MAX_DAILY_LOSS_USD", "200"))

    db_path = _sqlite_path(env_values.get("DATABASE_URL", DATABASE_URL))
    if db_path.exists() and not args.dry_run:
        backup_path = _backup(db_path)
        print(f"Backed up {db_path} to {backup_path}")

    if db_path.exists():
        _migrate_db(db_path, account_id, broker, max_position_size_usd, max_daily_loss_usd, args.dry_run)
    else:
        print(f"{db_path} doesn't exist yet - nothing to migrate, a fresh multi-account schema will be created on next startup.")

    _rewrite_env(ENV_PATH, account_id, args.dry_run)


if __name__ == "__main__":
    main()
