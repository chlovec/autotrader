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
    _add_job_configs_sort_order_column()
    _add_job_configs_backtest_columns()
    _add_job_configs_run_requested_at_column()
    _add_job_configs_prediction_start_date_column()
    _add_job_configs_predicted_date_offset_days_column()
    _add_job_configs_mcmc_num_simulations_column()
    _add_job_configs_ohlc_bars_columns()
    _add_job_configs_lstm_columns()
    _add_job_runs_progress_columns()
    _add_job_runs_control_columns()
    _add_market_predictions_mcmc_columns()
    _add_market_predictions_exit_price_confidence_column()
    _add_ohlc_bars_pcnt_increase_column()
    _convert_ohlc_bars_pcnt_increase_generated_column()
    _add_ohlc_bars_span_timestamp_index()
    _drop_ticker_bar_sync_state_table()
    _add_tickers_last_ohlc_sync_date_column()
    _add_ticker_types_rank_status_columns()
    _add_research_picks_entry_price_column()
    _add_research_picks_entry_price_timestamp_column()
    _migrate_lstm_inferences_training_method_pk()
    _add_lstm_inferences_exit_price_confidence_column()
    _add_job_configs_prediction_accuracy_pass_threshold_std_column()
    _add_job_configs_ohlc_update_columns()
    _add_job_configs_ohlc_update_progress_columns()
    _add_job_configs_run_overrides_column()
    _add_job_configs_win_rate_mcmc_range_confidence_level_column()
    _add_win_rates_mcmc_range_columns()
    _add_ticker_details_columns()
    _add_job_configs_buy_sell_pattern_columns()
    _add_ticker_groups_sort_order_column()
    _add_job_configs_query_export_max_age_hours_column()
    _create_tickers_market_direction_60days_min_from_latest_view()


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


def _add_job_configs_sort_order_column() -> None:
    """See db/models.py's JobConfig.sort_order - left NULL on existing rows, same
    "resolved at read time" fallback as the other nullable job_configs columns."""
    _add_job_configs_column("sort_order", "INTEGER")


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


def _add_job_configs_ohlc_bars_columns() -> None:
    _add_job_configs_column("ohlc_bars_start_date", "DATE")
    _add_job_configs_column("ohlc_bars_end_date", "DATE")
    _add_job_configs_column("ohlc_bars_limit", "INTEGER")


def _add_job_configs_lstm_columns() -> None:
    """See db/models.py's JobConfig.lstm_train_start_date etc. - added after
    job_configs itself (a table with live production data going back to before these
    columns existed), same reasoning as _add_job_configs_ohlc_bars_columns. Left NULL
    on existing rows; each resolves to its own jobs/lstm_common.py DEFAULT_* at run
    time until an operator visits one of the three LSTM jobs' cards on the Jobs page."""
    _add_job_configs_column("lstm_train_start_date", "DATE")
    _add_job_configs_column("lstm_train_end_date", "DATE")
    _add_job_configs_column("lstm_epochs", "INTEGER")
    _add_job_configs_column("lstm_lookback_days", "INTEGER")
    _add_job_configs_column("lstm_learning_rate", "FLOAT")
    _add_job_configs_column("lstm_batch_size", "INTEGER")
    _add_job_configs_column("lstm_walkforward_num_folds", "INTEGER")
    _add_job_configs_column("lstm_model_version_id", "INTEGER")


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


def _add_ohlc_bars_pcnt_increase_column() -> None:
    """pcnt_increase (see db/models.py's OhlcBar) is a plain column populated by
    jobs/sync_bars.py's _upsert_bar on every write, not a DB-level generated one - so a
    database from before this column existed needs both the ALTER TABLE (same as every
    other _add_*_column helper here) and a one-time backfill of existing rows, which
    would otherwise sit at NULL forever until each row's next sync happens to touch it
    again. The backfill uses the same formula _upsert_bar computes in Python, run once
    directly in SQL so it doesn't have to load every row into memory."""
    inspector = inspect(engine)
    if "ohlc_bars" not in inspector.get_table_names():
        return
    columns = {col["name"] for col in inspector.get_columns("ohlc_bars")}
    if "pcnt_increase" in columns:
        return
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE ohlc_bars ADD COLUMN pcnt_increase FLOAT"))
        conn.execute(
            text(
                'UPDATE ohlc_bars SET pcnt_increase = '
                'CASE WHEN "open" IS NULL OR "close" IS NULL OR "open" = 0 THEN NULL '
                'ELSE ("close" - "open") / "open" * 100 END'
            )
        )


def _convert_ohlc_bars_pcnt_increase_generated_column() -> None:
    """Some databases created pcnt_increase as a SQLite GENERATED ALWAYS AS (...)
    VIRTUAL column, which SQLite refuses to UPDATE/INSERT directly - that no longer
    matches db/models.py's OhlcBar.pcnt_increase, a plain column meant to be writable
    (see jobs/sync_bars.py's _upsert_bar). SQLite has no ALTER TABLE that turns a
    generated column into a plain one in place, so this adds a plain pcnt_gain column,
    copies over each row's already-computed value, drops the old generated column, and
    renames pcnt_gain back to pcnt_increase - same values, now directly writable."""
    inspector = inspect(engine)
    if "ohlc_bars" not in inspector.get_table_names():
        return
    with engine.begin() as conn:
        create_sql = conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type='table' AND name='ohlc_bars'")
        ).scalar_one()
        if "GENERATED" not in create_sql:
            return
        conn.execute(text("ALTER TABLE ohlc_bars ADD COLUMN pcnt_gain FLOAT"))
        conn.execute(text("UPDATE ohlc_bars SET pcnt_gain = pcnt_increase"))
        conn.execute(text("ALTER TABLE ohlc_bars DROP COLUMN pcnt_increase"))
        conn.execute(text("ALTER TABLE ohlc_bars RENAME COLUMN pcnt_gain TO pcnt_increase"))


def _add_ohlc_bars_span_timestamp_index() -> None:
    """Base.metadata.create_all only creates indexes alongside tables it creates, so an
    existing ohlc_bars (live data predating ix_ohlc_bars_span_ts_ticker in
    db/models.py's OhlcBar) never gets the index from it - this adds it. IF NOT EXISTS
    makes it a no-op on every startup after the first.

    Also drops ix_ohlc_bars_span_ts, the same index without ticker that this one
    replaced - only after the new one exists, so queries are never left without
    either. The new index serves everything the old one did (same leading columns)."""
    inspector = inspect(engine)
    if "ohlc_bars" not in inspector.get_table_names():
        return
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_ohlc_bars_span_ts_ticker "
                "ON ohlc_bars (multiplier, timespan, timestamp, ticker)"
            )
        )
        conn.execute(text("DROP INDEX IF EXISTS ix_ohlc_bars_span_ts"))

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


def _add_tickers_last_ohlc_sync_date_column() -> None:
    """See db/models.py's Ticker.last_ohlc_sync_date - left NULL on existing rows,
    same as any other column added to a table with existing data; nothing writes it
    yet."""
    _add_column_if_missing("tickers", "last_ohlc_sync_date", "DATE")


def _add_ticker_types_rank_status_columns() -> None:
    """See db/models.py's TickerType.rank/status. status gets a DEFAULT in the ALTER
    TABLE itself (unlike rank, which has none) so existing rows are backfilled to
    "active" immediately rather than sitting at NULL until an operator visits the
    Ticker Types page - same pattern as _add_job_configs_hidden_column's "BOOLEAN
    DEFAULT 0"."""
    _add_column_if_missing("ticker_types", "rank", "INTEGER")
    _add_column_if_missing("ticker_types", "status", "VARCHAR DEFAULT 'active'")


def _add_research_picks_entry_price_column() -> None:
    """See db/models.py's ResearchPick.entry_price - left NULL on existing rows, same
    as any other column added to a table with existing data (e.g.
    _add_tickers_last_ohlc_sync_date_column); only jobs/research_picks.py's next run
    populates it."""
    _add_column_if_missing("research_picks", "entry_price", "FLOAT")


def _add_research_picks_entry_price_timestamp_column() -> None:
    """See db/models.py's ResearchPick.entry_price_timestamp - left NULL on existing
    rows, same as _add_research_picks_entry_price_column."""
    _add_column_if_missing("research_picks", "entry_price_timestamp", "DATETIME")


def _migrate_lstm_inferences_training_method_pk() -> None:
    """LstmInference gained a `training_method` column as part of its primary key
    (ticker, predicted_date, training_method) - two independent jobs
    (predict-lstm-market-state-holdout/predict-lstm-market-state-walkforward) now both
    store a prediction for the same (ticker, predicted_date), one per flavor, which the
    original (ticker, predicted_date)-only PK couldn't represent (a re-run of either
    job would have silently overwritten the other's row). SQLite has no ALTER TABLE
    that changes a PRIMARY KEY in place, so - same recreate-copy-drop pattern as
    _convert_ohlc_bars_pcnt_increase_generated_column - this recreates the table under
    db/models.py's current (new) schema, backfilling training_method for any
    pre-existing row by joining back to lstm_model_versions.training_method via
    model_version_id, which already unambiguously recorded which flavor produced it -
    not a guess."""
    inspector = inspect(engine)
    if "lstm_inferences" not in inspector.get_table_names():
        return
    columns = {col["name"] for col in inspector.get_columns("lstm_inferences")}
    if "training_method" in columns:
        return
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE lstm_inferences RENAME TO lstm_inferences_old"))
        Base.metadata.tables["lstm_inferences"].create(conn)
        conn.execute(
            text(
                """
                INSERT INTO lstm_inferences (
                    ticker, predicted_date, training_method, current_state, predicted_state,
                    state_confidence, prob_strong_down, prob_down, prob_flat, prob_up,
                    prob_strong_up, expected_return, entry_price, exit_price, entry_time,
                    exit_time, history_days, model_version_id, computed_at
                )
                SELECT
                    o.ticker, o.predicted_date, v.training_method, o.current_state, o.predicted_state,
                    o.state_confidence, o.prob_strong_down, o.prob_down, o.prob_flat, o.prob_up,
                    o.prob_strong_up, o.expected_return, o.entry_price, o.exit_price, o.entry_time,
                    o.exit_time, o.history_days, o.model_version_id, o.computed_at
                FROM lstm_inferences_old o
                JOIN lstm_model_versions v ON v.id = o.model_version_id
                """
            )
        )
        conn.execute(text("DROP TABLE lstm_inferences_old"))


def _add_job_configs_prediction_accuracy_pass_threshold_std_column() -> None:
    """See db/models.py's JobConfig.prediction_accuracy_pass_threshold_std - added
    after job_configs itself, same "left NULL on existing rows, resolved at run time"
    reasoning as _add_job_configs_mcmc_num_simulations_column."""
    _add_job_configs_column("prediction_accuracy_pass_threshold_std", "FLOAT")


def _add_job_configs_ohlc_update_columns() -> None:
    """See db/models.py's JobConfig.ohlc_update_start_date/ohlc_update_end_date - added
    after job_configs itself, same "left NULL on existing rows" reasoning as
    _add_job_configs_ohlc_bars_columns - a NULL here is resolved to a default at run
    time (see jobs/ohlc_update.py's resolve_date_range)."""
    _add_job_configs_column("ohlc_update_start_date", "DATE")
    _add_job_configs_column("ohlc_update_end_date", "DATE")


def _add_job_configs_ohlc_update_progress_columns() -> None:
    """See db/models.py's JobConfig.ohlc_update_batch_size and ohlc_update_cursor/
    ohlc_update_completed_at/ohlc_update_retry_round/ohlc_update_retry_tickers/
    ohlc_update_failed_tickers/ohlc_update_next_run_at - added after the columns above,
    left NULL on existing rows, which is exactly the "default batch size, no cycle in
    progress, start from the first ticker" state."""
    _add_job_configs_column("ohlc_update_batch_size", "INTEGER")
    _add_job_configs_column("ohlc_update_cursor", "VARCHAR")
    _add_job_configs_column("ohlc_update_completed_at", "DATETIME")
    _add_job_configs_column("ohlc_update_retry_round", "INTEGER")
    _add_job_configs_column("ohlc_update_retry_tickers", "VARCHAR")
    _add_job_configs_column("ohlc_update_failed_tickers", "VARCHAR")
    _add_job_configs_column("ohlc_update_next_run_at", "DATETIME")


def _add_job_configs_run_overrides_column() -> None:
    """See db/models.py's JobConfig.run_overrides - added after job_configs itself,
    same "left NULL on existing rows" reasoning as _add_job_configs_ohlc_update_columns."""
    _add_job_configs_column("run_overrides", "VARCHAR")


def _add_lstm_inferences_exit_price_confidence_column() -> None:
    """See db/models.py's LstmInference.exit_price_confidence - added after
    lstm_inferences itself, same reasoning as _add_market_predictions_exit_price_confidence_column.
    Existing rows get NULL until their next predict-lstm-market-state-holdout/-walkforward
    run recomputes them."""
    _add_column_if_missing("lstm_inferences", "exit_price_confidence", "FLOAT")


def _add_job_configs_win_rate_mcmc_range_confidence_level_column() -> None:
    """See db/models.py's JobConfig.win_rate_mcmc_range_confidence_level - added after
    job_configs itself, same "left NULL on existing rows, resolved at run time"
    reasoning as _add_job_configs_prediction_accuracy_pass_threshold_std_column."""
    _add_job_configs_column("win_rate_mcmc_range_confidence_level", "FLOAT")


def _add_win_rates_mcmc_range_columns() -> None:
    """See db/models.py's WinRate.mcmc_range_win_count/mcmc_range_win_rate/
    mcmc_range_confidence_level - added after win_rates itself (a table with live
    production data going back to before these columns existed), same reasoning as
    _add_lstm_inferences_exit_price_confidence_column. Existing rows get NULL until
    their next compute-win-rates run recomputes them."""
    _add_column_if_missing("win_rates", "mcmc_range_win_count", "INTEGER")
    _add_column_if_missing("win_rates", "mcmc_range_win_rate", "FLOAT")
    _add_column_if_missing("win_rates", "mcmc_range_confidence_level", "FLOAT")


def _add_ticker_details_columns() -> None:
    """See db/models.py's TickerDetail docstring - active/delisted_utc/phone_number/
    description/ticker_root/address_*/branding_* all added after ticker_details
    itself (a table with live production data going back to before these columns
    existed), same reasoning as _add_lstm_inferences_exit_price_confidence_column.
    Existing rows get NULL until their next sync-ticker-details run repopulates them -
    in particular, active/delisted_utc stay NULL (not "confirmed active") for any
    ticker not yet re-synced since this migration, so a NULL here means "unknown",
    not "active"."""
    _add_column_if_missing("ticker_details", "active", "BOOLEAN")
    _add_column_if_missing("ticker_details", "delisted_utc", "DATETIME")
    _add_column_if_missing("ticker_details", "phone_number", "VARCHAR")
    _add_column_if_missing("ticker_details", "description", "VARCHAR")
    _add_column_if_missing("ticker_details", "ticker_root", "VARCHAR")
    _add_column_if_missing("ticker_details", "address_line1", "VARCHAR")
    _add_column_if_missing("ticker_details", "address_city", "VARCHAR")
    _add_column_if_missing("ticker_details", "address_state", "VARCHAR")
    _add_column_if_missing("ticker_details", "address_postal_code", "VARCHAR")
    _add_column_if_missing("ticker_details", "branding_logo_url", "VARCHAR")
    _add_column_if_missing("ticker_details", "branding_icon_url", "VARCHAR")


def _add_job_configs_buy_sell_pattern_columns() -> None:
    """See db/models.py's JobConfig.buy_sell_pattern_* - added after job_configs itself,
    left NULL on existing rows."""
    _add_job_configs_column("buy_sell_pattern_start_date", "DATE")
    _add_job_configs_column("buy_sell_pattern_end_date", "DATE")
    _add_job_configs_column("buy_sell_pattern_name", "VARCHAR")
    _add_job_configs_column("buy_sell_pattern_replace", "BOOLEAN")
    _add_job_configs_column("buy_sell_pattern_batch_size", "INTEGER")


def _add_ticker_groups_sort_order_column() -> None:
    """See db/models.py's TickerGroup.sort_order - added after ticker_groups itself,
    left NULL on existing rows, which sort after every explicitly ordered row."""
    _add_column_if_missing("ticker_groups", "sort_order", "INTEGER")


def _add_job_configs_query_export_max_age_hours_column() -> None:
    """See db/models.py's JobConfig.query_export_max_age_hours - added after
    job_configs itself, left NULL on existing rows (resolved at run time)."""
    _add_job_configs_column("query_export_max_age_hours", "FLOAT")


_TICKERS_MARKET_DIRECTION_VIEW = "tickers_market_direction_60days_min_from_latest"

# No ORDER BY on purpose: it would stop SQLite from merging the view into the query
# reading it, so a reader's WHERE (one ticker, a date range) would only be applied
# after the whole view was built. Readers order the rows themselves. Filter date
# ranges on `timestamp` (indexed) rather than the computed `date` column, which
# can't use an index.
_TICKERS_MARKET_DIRECTION_VIEW_SQL = f"""CREATE VIEW {_TICKERS_MARKET_DIRECTION_VIEW} AS
WITH
-- Only daily bars matter; every step below reads from this.
daily_bars AS (
    SELECT *
    FROM ohlc_bars
    WHERE multiplier = 1 AND timespan = 'day'
),

-- The most recent trading day in the data. ORDER BY ... LIMIT 1 rather than MAX() so
-- it's guaranteed to be a single seek to the end of ix_ohlc_bars_span_ts_ticker.
latest_day AS (
    SELECT timestamp AS ts
    FROM daily_bars
    ORDER BY timestamp DESC
    LIMIT 1
),

-- Filter 1: tickers that have a bar on the latest day.
current_tickers AS (
    SELECT ticker
    FROM daily_bars
    WHERE timestamp = (SELECT ts FROM latest_day)
),

-- Filter 2: of those, tickers whose 60 most recent bars all exist.
eligible_tickers AS (
    SELECT c.ticker
    FROM current_tickers c
    WHERE (
        SELECT COUNT(*)
        FROM (
            SELECT 1
            FROM daily_bars d
            WHERE d.ticker = c.ticker
            ORDER BY d.timestamp DESC
            LIMIT 60
        ) AS recent
    ) = 60
)

SELECT
    b.ticker,
    b.timestamp,
    date(b.timestamp)                            AS date,
    CAST(strftime('%Y', b.timestamp) AS INTEGER) AS year,
    CAST(strftime('%m', b.timestamp) AS INTEGER) AS month,
    CAST(strftime('%d', b.timestamp) AS INTEGER) AS day,
    b.open,
    b.close,
    b.pcnt_increase,
    CASE
        WHEN b.pcnt_increase <  -20  THEN -3  -- Very Strong Down
        WHEN b.pcnt_increase <  -5   THEN -2  -- Strong Down
        WHEN b.pcnt_increase <  -0.1 THEN -1  -- Down
        WHEN b.pcnt_increase <=  0.1 THEN  0  -- Neutral
        WHEN b.pcnt_increase <=  5   THEN  1  -- Up
        WHEN b.pcnt_increase <=  20  THEN  2  -- Strong Up
        WHEN b.pcnt_increase >   20  THEN  3  -- Very Strong Up
    END AS direction
FROM daily_bars b
WHERE b.ticker IN (SELECT ticker FROM eligible_tickers)"""


def _create_tickers_market_direction_60days_min_from_latest_view() -> None:
    """Every daily bar for tickers that have a bar on the latest trading day and a full
    60 most recent daily bars, bucketed by pcnt_increase into a -3..3 direction score.
    create_all doesn't manage views, so this is created here instead - and dropped and
    recreated whenever the definition above changes, which is safe since a view holds
    no data, just the query. SQLite stores a view's CREATE statement verbatim in
    sqlite_master, so comparing it to the text above is enough to detect a change."""
    with engine.begin() as conn:
        existing = conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type = 'view' AND name = :name"),
            {"name": _TICKERS_MARKET_DIRECTION_VIEW},
        ).scalar()
        if existing == _TICKERS_MARKET_DIRECTION_VIEW_SQL:
            return
        conn.execute(text(f"DROP VIEW IF EXISTS {_TICKERS_MARKET_DIRECTION_VIEW}"))
        conn.execute(text(_TICKERS_MARKET_DIRECTION_VIEW_SQL))

def get_session() -> Session:
    return SessionLocal()
