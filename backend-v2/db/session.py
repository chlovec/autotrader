import os

from dotenv import load_dotenv
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from .models import Base

load_dotenv()

# Separate from v1's DATABASE_URL (repo root db/session.py) - backend-v2 gets its own
# database rather than sharing v1's.
DATABASE_URL = os.environ.get("BACKEND_V2_DATABASE_URL", "sqlite:///./backend_v2.db")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

if DATABASE_URL.startswith("sqlite"):

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, connection_record) -> None:
        """Without these, a long write transaction (e.g. jobs/predict_market_state.py
        looping over tens of thousands of tickers) blocks every reader for the whole
        API - the exact "can't load the dashboard while a job is running" failure this
        was added to fix.

        journal_mode=WAL replaces SQLite's default rollback journal, under which readers
        are blocked at the instant a writer commits (and briefly beforehand at lock
        upgrade). WAL lets readers proceed against the last-committed snapshot while a
        writer is active, with only writer-vs-writer serialized - a straightforward
        FastAPI GET endpoint no longer has to wait on a job's commit. WAL is persisted in
        the database file itself, so this PRAGMA is a one-time no-op after the first
        connection ever sets it, but it's cheap enough to run unconditionally on every
        new connection rather than special-casing "already WAL".

        busy_timeout is a per-connection setting (unlike journal_mode, must be set every
        time) - it makes sqlite3 retry for up to 30s instead of raising "database is
        locked" after its 5s default the instant two writers (still serialized even
        under WAL) briefly overlap, e.g. two jobs' commits landing close together."""
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()


def init_db() -> None:
    Base.metadata.create_all(engine)
    _add_job_configs_start_time_column()
    _add_job_configs_bars_end_date_offset_days_column()
    _add_job_configs_snapshot_types_column()
    _add_job_configs_average_volume_columns()
    _add_job_configs_hidden_column()
    _add_job_configs_backtest_columns()
    _add_job_configs_run_requested_at_column()
    _add_job_configs_prediction_start_date_column()
    _add_job_configs_predicted_date_offset_days_column()
    _add_job_configs_mcmc_num_simulations_column()
    _add_job_runs_progress_columns()
    _add_job_runs_control_columns()
    _add_market_predictions_mcmc_columns()
    _add_market_predictions_exit_price_confidence_column()
    _drop_ticker_bar_sync_state_table()


def _add_column_if_missing(table: str, column: str, ddl_type: str) -> None:
    """Shared by every _add_<table>_*_column helper below - create_all only creates
    missing *tables*, not missing columns on ones that already exist, so a database
    from before one of these columns was added would otherwise 500 on its first query
    against that table. There's no migration tool here (see this module's lack of
    one), so this is a one-off, idempotent ALTER TABLE instead - cheap enough that
    running it unconditionally on every init_db() call beats standing up Alembic for a
    handful of added columns."""
    inspector = inspect(engine)
    if table not in inspector.get_table_names():
        return
    columns = {col["name"] for col in inspector.get_columns(table)}
    if column in columns:
        return
    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"))


def _add_job_configs_column(column: str, ddl_type: str) -> None:
    _add_column_if_missing("job_configs", column, ddl_type)


def _add_job_configs_start_time_column() -> None:
    _add_job_configs_column("start_time", "VARCHAR DEFAULT '00:00'")


def _add_job_configs_bars_end_date_offset_days_column() -> None:
    _add_job_configs_column("bars_end_date_offset_days", "INTEGER")


def _add_job_configs_snapshot_types_column() -> None:
    _add_job_configs_column("snapshot_types", "VARCHAR")


def _add_job_configs_average_volume_columns() -> None:
    _add_job_configs_column("average_volume_start_date", "DATE")
    _add_job_configs_column("average_volume_days_interval", "INTEGER")


def _add_job_configs_hidden_column() -> None:
    _add_job_configs_column("hidden", "BOOLEAN DEFAULT 0")


def _add_job_configs_backtest_columns() -> None:
    _add_job_configs_column("backtest_start_date", "DATE")
    _add_job_configs_column("backtest_end_date", "DATE")


def _add_job_configs_run_requested_at_column() -> None:
    _add_job_configs_column("run_requested_at", "DATETIME")


def _add_job_configs_prediction_start_date_column() -> None:
    _add_job_configs_column("prediction_start_date", "DATE")


def _add_job_configs_predicted_date_offset_days_column() -> None:
    _add_job_configs_column("predicted_date_offset_days", "INTEGER")


def _add_job_configs_mcmc_num_simulations_column() -> None:
    _add_job_configs_column("mcmc_num_simulations", "INTEGER")


def _add_job_runs_column(column: str, ddl_type: str) -> None:
    _add_column_if_missing("job_runs", column, ddl_type)


def _add_job_runs_progress_columns() -> None:
    _add_job_runs_column("progress_completed", "INTEGER")
    _add_job_runs_column("progress_total", "INTEGER")


def _add_job_runs_control_columns() -> None:
    _add_job_runs_column("pause_requested", "BOOLEAN DEFAULT 0")
    _add_job_runs_column("cancel_requested", "BOOLEAN DEFAULT 0")


def _add_market_predictions_mcmc_columns() -> None:
    """exit_price/exit_price_confidence were added after market_predictions_mcmc
    itself - a database that already ran the Monte Carlo job before these existed
    would otherwise 500 on its first query against the table. Existing rows get NULL
    in both new columns until their next run recomputes them, same as any other
    column added to a table with existing data."""
    _add_column_if_missing("market_predictions_mcmc", "exit_price", "FLOAT")
    _add_column_if_missing("market_predictions_mcmc", "exit_price_confidence", "FLOAT")


def _add_market_predictions_exit_price_confidence_column() -> None:
    """exit_price_confidence was added after market_predictions itself (a table with
    live production data going back to before this column existed) - existing rows
    get NULL until their next run recomputes it, same reasoning as
    _add_market_predictions_mcmc_columns above."""
    _add_column_if_missing("market_predictions", "exit_price_confidence", "FLOAT")


def _drop_ticker_bar_sync_state_table() -> None:
    """ticker_bar_sync_state (db/models.py's now-removed TickerBarSyncState) used to
    track each ticker's "synced through" cursor separately from ohlc_bars itself - that
    cursor could silently drift from reality (advanced even when a fetch returned zero
    bars, permanently masking a ticker that had gone stale). jobs/sync_bars.py now
    derives each ticker's start date straight from ohlc_bars.MAX(timestamp) instead, so
    this table has no reader left; dropped here the same idempotent way columns are
    added elsewhere in this module, since a database from before this change would
    otherwise carry it around inertly forever."""
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS ticker_bar_sync_state"))


def get_session() -> Session:
    return SessionLocal()
