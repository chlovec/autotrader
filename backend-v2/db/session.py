import os

from dotenv import load_dotenv
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from .models import Base

load_dotenv()

# Separate from v1's DATABASE_URL (repo root db/session.py) - backend-v2 gets its own
# database rather than sharing v1's.
DATABASE_URL = os.environ.get("BACKEND_V2_DATABASE_URL", "sqlite:///./backend_v2.db")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db() -> None:
    Base.metadata.create_all(engine)
    _add_job_configs_start_time_column()
    _add_job_configs_snapshot_types_column()


def _add_job_configs_column(column: str, ddl_type: str) -> None:
    """Shared by _add_job_configs_start_time_column and
    _add_job_configs_snapshot_types_column below - create_all only creates missing
    *tables*, not missing columns on ones that already exist, so a database from
    before one of these columns was added would otherwise 500 on its first query
    against job_configs. There's no migration tool here (see this module's lack of
    one), so this is a one-off, idempotent ALTER TABLE instead - cheap enough that
    running it unconditionally on every init_db() call beats standing up Alembic for a
    couple of added columns."""
    inspector = inspect(engine)
    if "job_configs" not in inspector.get_table_names():
        return
    columns = {col["name"] for col in inspector.get_columns("job_configs")}
    if column in columns:
        return
    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE job_configs ADD COLUMN {column} {ddl_type}"))


def _add_job_configs_start_time_column() -> None:
    _add_job_configs_column("start_time", "VARCHAR DEFAULT '00:00'")


def _add_job_configs_snapshot_types_column() -> None:
    _add_job_configs_column("snapshot_types", "VARCHAR")


def get_session() -> Session:
    return SessionLocal()
