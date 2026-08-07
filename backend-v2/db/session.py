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


def _add_job_configs_start_time_column() -> None:
    """create_all only creates missing *tables*, not missing columns on ones that
    already exist - a database from before JobConfig.start_time was added would
    otherwise 500 on its first query against job_configs. There's no migration tool
    here (see this module's lack of one), so this is a one-off, idempotent ALTER TABLE
    instead - cheap enough that running it unconditionally on every init_db() call
    beats standing up Alembic for a single added column."""
    inspector = inspect(engine)
    if "job_configs" not in inspector.get_table_names():
        return
    columns = {col["name"] for col in inspector.get_columns("job_configs")}
    if "start_time" in columns:
        return
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE job_configs ADD COLUMN start_time VARCHAR DEFAULT '00:00'"))


def get_session() -> Session:
    return SessionLocal()
