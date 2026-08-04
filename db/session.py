import os
import time

from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from db.models import Base, ResearchSchedule

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./autotrader.db")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

_CREATE_ALL_RETRIES = 5
_CREATE_ALL_RETRY_DELAY_SECONDS = 0.5


def _create_all_with_retry() -> None:
    """bin/restart.sh launches the backend, trading loop, and a research run nearly
    simultaneously - against a completely fresh DB (e.g. right after a schema wipe),
    each independently calls init_db(), and create_all()'s check-then-create isn't
    protected by any cross-process lock, so two processes can both decide the same
    table is missing and race to CREATE TABLE it - whichever loses gets "table X
    already exists" and would otherwise crash on startup. Retrying converges once
    whichever process won has committed its CREATE TABLE."""
    for attempt in range(_CREATE_ALL_RETRIES):
        try:
            Base.metadata.create_all(engine)
            return
        except OperationalError as exc:
            if "already exists" not in str(exc) or attempt == _CREATE_ALL_RETRIES - 1:
                raise
            time.sleep(_CREATE_ALL_RETRY_DELAY_SECONDS)


def _add_column_if_missing(table: str, column: str, ddl_type: str) -> None:
    """sqlite-specific poor-man's migration: create_all() only creates missing *tables*,
    never adds columns to ones that already exist. This project has no Alembic (see
    ARCHITECTURE.md) - additive, defaulted columns (like Account.max_total_exposure_usd)
    self-heal here on every startup instead of needing a bespoke one-off script each time
    one gets added. Renames/drops/type changes still need a hand-written migration (see
    scripts/migrate_to_accounts.py for that shape) - this only ever adds a column."""
    if not DATABASE_URL.startswith("sqlite"):
        return  # PRAGMA table_info is sqlite-specific; other engines need a real migration tool
    with engine.connect() as conn:
        existing = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
        if not existing or column in existing:
            return  # table doesn't exist yet (create_all() will make it) or already has the column
        conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")
        conn.commit()


def init_db() -> None:
    _create_all_with_retry()
    _add_column_if_missing("accounts", "max_total_exposure_usd", "FLOAT DEFAULT 0.0")
    _add_column_if_missing("accounts", "pending_strategy_name", "VARCHAR")
    _add_column_if_missing("accounts", "pending_strategy_params", "TEXT")
    _add_column_if_missing("research_schedule", "selected_count", "INTEGER DEFAULT 10")
    with SessionLocal() as session:
        if session.get(ResearchSchedule, 1) is None:
            session.add(ResearchSchedule(id=1, enabled=True))
        session.commit()


def get_session() -> Session:
    return SessionLocal()
