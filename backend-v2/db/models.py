"""backend-v2's own schema - a separate database from v1's db/models.py at the repo root."""

import datetime as dt

from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Ticker(Base):
    """One row per ticker returned by GET /v3/reference/tickers, keyed by the ticker
    symbol itself. Upserted by jobs/sync_tickers.py - a re-fetched ticker overwrites
    the existing row rather than creating a new one."""

    __tablename__ = "tickers"

    ticker: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str | None] = mapped_column(String)
    market: Mapped[str | None] = mapped_column(String)
    locale: Mapped[str | None] = mapped_column(String)
    primary_exchange: Mapped[str | None] = mapped_column(String)
    type: Mapped[str | None] = mapped_column(String)
    active: Mapped[bool | None] = mapped_column()
    currency_name: Mapped[str | None] = mapped_column(String)
    cik: Mapped[str | None] = mapped_column(String)
    composite_figi: Mapped[str | None] = mapped_column(String)
    share_class_figi: Mapped[str | None] = mapped_column(String)
    last_updated_utc: Mapped[str | None] = mapped_column(String)


class SyncState(Base):
    """One row per job name, tracking when that job last completed successfully so the
    next run can ask the upstream API for only what changed since then. See
    jobs/sync_tickers.py's sync_tickers for how the tickers job uses this.

    last_synced_at is stored naive but is always UTC - sqlite drops tzinfo on
    round-trip, so keeping an explicit timezone on the column would just mean an aware
    datetime went in and a naive one came back out."""

    __tablename__ = "sync_state"

    job_name: Mapped[str] = mapped_column(String, primary_key=True)
    last_synced_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=False))


class OhlcBar(Base):
    """One row per bar returned by GET /v2/aggs/ticker/{ticker}/range/{multiplier}/
    {timespan}/{from}/{to}, keyed by (ticker, multiplier, timespan, timestamp) so the
    same ticker can hold bars of more than one granularity (e.g. daily and 5-minute)
    without colliding. Upserted by jobs/sync_bars.py."""

    __tablename__ = "ohlc_bars"

    ticker: Mapped[str] = mapped_column(ForeignKey("tickers.ticker"), primary_key=True)
    multiplier: Mapped[int] = mapped_column(Integer, primary_key=True)
    timespan: Mapped[str] = mapped_column(String, primary_key=True)
    timestamp: Mapped[dt.datetime] = mapped_column(DateTime(timezone=False), primary_key=True)
    open: Mapped[float | None] = mapped_column(Float)
    high: Mapped[float | None] = mapped_column(Float)
    low: Mapped[float | None] = mapped_column(Float)
    close: Mapped[float | None] = mapped_column(Float)
    volume: Mapped[float | None] = mapped_column(Float)
    vwap: Mapped[float | None] = mapped_column(Float)
    transactions: Mapped[int | None] = mapped_column(Integer)


class TickerBarSyncState(Base):
    """Per (ticker, multiplier, timespan) cursor: the last date whose bars are known to
    be fully synced. jobs/sync_bars.py's sync_bars_nightly reads this to find tickers
    that aren't up to date and to pick up where each one left off; every successful
    fetch_and_store_bars call advances it (never rewinds it - see that function's
    docstring)."""

    __tablename__ = "ticker_bar_sync_state"

    ticker: Mapped[str] = mapped_column(ForeignKey("tickers.ticker"), primary_key=True)
    multiplier: Mapped[int] = mapped_column(Integer, primary_key=True)
    timespan: Mapped[str] = mapped_column(String, primary_key=True)
    synced_through: Mapped[dt.date] = mapped_column(Date)


class JobConfig(Base):
    """Per-job settings, editable from the dashboard's Jobs page (see app/main.py).
    Seeded with defaults (app/main.py's _get_or_create_config) the first time a job's
    config is read - there's one row per jobs/registry.py entry once that happens.

    `run_type` is "manual" (runnable from the dashboard only) or "auto" (also eligible
    for the scheduled trigger - see app/main.py's _scheduled_job). A manual trigger
    (the dashboard's "Run now" button) works regardless of run_type, same as v1's
    ResearchSchedule.enabled bypass at the repo root. `schedule_interval_unit` ("minutes",
    "hours", or "days") and `schedule_interval_value` (the N) together describe a
    recurring "every N <unit>" cadence - app/main.py builds an APScheduler
    IntervalTrigger straight from these two (the unit string doubles as the trigger's
    keyword argument name). update_job_config reschedules the live APScheduler job
    when these change, so edits take effect immediately rather than needing a
    backend-v2 restart; the schedule is kept (and shown) even for a "manual" job, so
    switching it to "auto" later doesn't need re-entering it.

    tickers/multiplier/timespan/backfill_days only apply to the bars job
    (registry.JobDefinition.has_bars_fields) and are left None for the tickers job.
    ticker_types applies to both jobs: a single upstream `type` filter for the tickers
    job (jobs/sync_tickers.py's ticker_type param - None syncs every type), or a
    multi-select filter for the bars job, mutually exclusive with tickers there - see
    jobs/sync_bars.py's _resolve_tickers. Comma-separated either way."""

    __tablename__ = "job_configs"

    job_name: Mapped[str] = mapped_column(String, primary_key=True)
    run_type: Mapped[str] = mapped_column(String, default="auto")
    schedule_interval_unit: Mapped[str] = mapped_column(String, default="days")
    schedule_interval_value: Mapped[int] = mapped_column(Integer, default=1)
    ticker_types: Mapped[str | None] = mapped_column(String)
    tickers: Mapped[str | None] = mapped_column(String)
    multiplier: Mapped[int | None] = mapped_column(Integer)
    timespan: Mapped[str | None] = mapped_column(String)
    backfill_days: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=False), default=dt.datetime.utcnow)


class JobRun(Base):
    """One row per job execution, manual or scheduled - backs the dashboard's Jobs page
    run history and "currently running" status. Written by app/main.py's _run_job:
    inserted with status="in_progress" before the job starts, then updated to
    "completed" or "failed" when it finishes.

    `trigger` is "manual" (dashboard "Run now"/play-button run) or "auto" (fired by
    the schedule) - this doubles as the dashboard's "last run mode" display."""

    __tablename__ = "job_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_name: Mapped[str] = mapped_column(String, index=True)
    trigger: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=False))
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=False))
    result_summary: Mapped[str | None] = mapped_column(String)
    error: Mapped[str | None] = mapped_column(String)
