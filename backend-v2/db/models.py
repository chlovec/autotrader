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


class TickerType(Base):
    """One row per code/asset_class/locale combination returned by GET
    /v3/reference/tickers/types, describing what a Ticker.type value means (e.g. "CS" ->
    "Common Stock"). Upserted by jobs/sync_ticker_types.py - unlike Ticker, this is a
    short, mostly-static reference list re-fetched in full on every run rather than
    incrementally."""

    __tablename__ = "ticker_types"

    code: Mapped[str] = mapped_column(String, primary_key=True)
    asset_class: Mapped[str] = mapped_column(String, primary_key=True)
    locale: Mapped[str] = mapped_column(String, primary_key=True)
    description: Mapped[str | None] = mapped_column(String)


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


class SyncProgress(Base):
    """Cursor checkpoint for a sync_tickers run that's still in flight, so a run
    interrupted mid-page (e.g. DataClient exhausts its 429 retries) resumes from the
    failed page's cursor on the next call instead of re-paging everything already
    fetched. Written after every successful page, deleted once the run finishes and
    SyncState.last_synced_at is written - see jobs/sync_tickers.py's sync_tickers.

    ticker_type records which type filter the checkpointed cursor was issued under -
    massive.com bakes query filters into the opaque cursor itself, so a checkpoint from
    a since-changed ticker_type filter can't be resumed and is discarded instead."""

    __tablename__ = "sync_progress"

    job_name: Mapped[str] = mapped_column(String, primary_key=True)
    next_url: Mapped[str] = mapped_column(String)
    run_started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=False))
    ticker_type: Mapped[str | None] = mapped_column(String)


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


class CurrentSnapshot(Base):
    """One row per ticker's latest market snapshot from GET /v2/snapshot/locale/us/
    markets/stocks/tickers/{ticker}, keyed by the ticker symbol. Upserted by
    jobs/sync_snapshots.py - a re-fetched ticker overwrites the existing row (there's no
    history kept), same PK-overwrite semantics as Ticker."""

    __tablename__ = "current_snapshots"

    ticker: Mapped[str] = mapped_column(ForeignKey("tickers.ticker"), primary_key=True)
    todays_change: Mapped[float | None] = mapped_column(Float)
    todays_change_perc: Mapped[float | None] = mapped_column(Float)
    updated: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=False))
    day_open: Mapped[float | None] = mapped_column(Float)
    day_high: Mapped[float | None] = mapped_column(Float)
    day_low: Mapped[float | None] = mapped_column(Float)
    day_close: Mapped[float | None] = mapped_column(Float)
    day_volume: Mapped[float | None] = mapped_column(Float)
    day_vwap: Mapped[float | None] = mapped_column(Float)
    min_open: Mapped[float | None] = mapped_column(Float)
    min_high: Mapped[float | None] = mapped_column(Float)
    min_low: Mapped[float | None] = mapped_column(Float)
    min_close: Mapped[float | None] = mapped_column(Float)
    min_volume: Mapped[float | None] = mapped_column(Float)
    min_vwap: Mapped[float | None] = mapped_column(Float)
    min_accumulated_volume: Mapped[float | None] = mapped_column(Float)
    min_timestamp: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=False))
    prev_day_open: Mapped[float | None] = mapped_column(Float)
    prev_day_high: Mapped[float | None] = mapped_column(Float)
    prev_day_low: Mapped[float | None] = mapped_column(Float)
    prev_day_close: Mapped[float | None] = mapped_column(Float)
    prev_day_volume: Mapped[float | None] = mapped_column(Float)
    prev_day_vwap: Mapped[float | None] = mapped_column(Float)
    fetched_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=False))


class TopMarketMover(Base):
    """One row per ticker currently on the top-gainers or top-losers list from GET
    /v2/snapshot/locale/us/markets/stocks/{direction}, keyed by (ticker, direction) -
    both directions share this table, distinguished by the direction column, rather
    than each getting its own table. Unlike CurrentSnapshot's upsert-forever
    semantics, jobs/sync_top_movers.py replaces a direction's rows outright on every
    run: list membership is the point of a top-N list, so a ticker that drops off
    shouldn't linger here the way a re-fetched single ticker's old snapshot data
    would. rank is this ticker's position (1 = biggest mover) in the list as
    massive.com returned it."""

    __tablename__ = "top_market_movers"

    ticker: Mapped[str] = mapped_column(String, primary_key=True)
    direction: Mapped[str] = mapped_column(String, primary_key=True)
    rank: Mapped[int] = mapped_column(Integer)
    todays_change: Mapped[float | None] = mapped_column(Float)
    todays_change_perc: Mapped[float | None] = mapped_column(Float)
    updated: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=False))
    day_open: Mapped[float | None] = mapped_column(Float)
    day_high: Mapped[float | None] = mapped_column(Float)
    day_low: Mapped[float | None] = mapped_column(Float)
    day_close: Mapped[float | None] = mapped_column(Float)
    day_volume: Mapped[float | None] = mapped_column(Float)
    day_vwap: Mapped[float | None] = mapped_column(Float)
    min_open: Mapped[float | None] = mapped_column(Float)
    min_high: Mapped[float | None] = mapped_column(Float)
    min_low: Mapped[float | None] = mapped_column(Float)
    min_close: Mapped[float | None] = mapped_column(Float)
    min_volume: Mapped[float | None] = mapped_column(Float)
    min_vwap: Mapped[float | None] = mapped_column(Float)
    min_accumulated_volume: Mapped[float | None] = mapped_column(Float)
    min_timestamp: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=False))
    prev_day_open: Mapped[float | None] = mapped_column(Float)
    prev_day_high: Mapped[float | None] = mapped_column(Float)
    prev_day_low: Mapped[float | None] = mapped_column(Float)
    prev_day_close: Mapped[float | None] = mapped_column(Float)
    prev_day_volume: Mapped[float | None] = mapped_column(Float)
    prev_day_vwap: Mapped[float | None] = mapped_column(Float)
    fetched_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=False))


class UnifiedSnapshot(Base):
    """One row per ticker returned by GET /v3/snapshot ("Unified Snapshot"), keyed by
    the ticker symbol alone - massive.com's ticker scheme is already unique across
    asset classes (plain "AAPL" for stocks, "O:..." for options, "I:..." for indices,
    "C:..." for fx, "X:..." for crypto), so unlike TopMarketMover there's no need for
    a second primary-key column to keep classes from colliding. Upserted by
    jobs/sync_unified_snapshot.py, one asset-class `type` at a time (see
    jobs/registry.py's SNAPSHOT_TYPE_OPTIONS) - same upsert-forever semantics as
    CurrentSnapshot, not TopMarketMover's replace-on-run.

    Which columns are populated depends on `type`: session_*/last_quote_*/last_trade_*
    apply broadly; greeks_*/details_*/underlying_asset_*/break_even_price/
    implied_volatility/open_interest are options-only; value is indices-only.
    last_trade_conditions is stored comma-joined - massive.com returns it as a list of
    condition codes and there's no array column type to reach for on sqlite."""

    __tablename__ = "unified_snapshots"

    ticker: Mapped[str] = mapped_column(String, primary_key=True)
    type: Mapped[str | None] = mapped_column(String)
    name: Mapped[str | None] = mapped_column(String)
    market_status: Mapped[str | None] = mapped_column(String)
    error: Mapped[str | None] = mapped_column(String)
    message: Mapped[str | None] = mapped_column(String)
    value: Mapped[float | None] = mapped_column(Float)
    fmv: Mapped[float | None] = mapped_column(Float)

    # Options only.
    break_even_price: Mapped[float | None] = mapped_column(Float)
    implied_volatility: Mapped[float | None] = mapped_column(Float)
    open_interest: Mapped[float | None] = mapped_column(Float)

    # `session` block.
    session_change: Mapped[float | None] = mapped_column(Float)
    session_change_percent: Mapped[float | None] = mapped_column(Float)
    session_early_trading_change: Mapped[float | None] = mapped_column(Float)
    session_early_trading_change_percent: Mapped[float | None] = mapped_column(Float)
    session_late_trading_change: Mapped[float | None] = mapped_column(Float)
    session_late_trading_change_percent: Mapped[float | None] = mapped_column(Float)
    session_close: Mapped[float | None] = mapped_column(Float)
    session_high: Mapped[float | None] = mapped_column(Float)
    session_low: Mapped[float | None] = mapped_column(Float)
    session_open: Mapped[float | None] = mapped_column(Float)
    session_previous_close: Mapped[float | None] = mapped_column(Float)
    session_volume: Mapped[float | None] = mapped_column(Float)

    # `last_quote` block.
    last_quote_ask: Mapped[float | None] = mapped_column(Float)
    last_quote_ask_size: Mapped[float | None] = mapped_column(Float)
    last_quote_ask_exchange: Mapped[int | None] = mapped_column(Integer)
    last_quote_bid: Mapped[float | None] = mapped_column(Float)
    last_quote_bid_size: Mapped[float | None] = mapped_column(Float)
    last_quote_bid_exchange: Mapped[int | None] = mapped_column(Integer)
    last_quote_exchange: Mapped[int | None] = mapped_column(Integer)
    last_quote_midpoint: Mapped[float | None] = mapped_column(Float)
    last_quote_timeframe: Mapped[str | None] = mapped_column(String)
    last_quote_last_updated: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=False))

    # `last_trade` block.
    last_trade_conditions: Mapped[str | None] = mapped_column(String)
    last_trade_exchange: Mapped[int | None] = mapped_column(Integer)
    last_trade_id: Mapped[str | None] = mapped_column(String)
    last_trade_price: Mapped[float | None] = mapped_column(Float)
    last_trade_size: Mapped[float | None] = mapped_column(Float)
    last_trade_timeframe: Mapped[str | None] = mapped_column(String)
    last_trade_sip_timestamp: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=False))

    # `details` block (options only).
    details_contract_type: Mapped[str | None] = mapped_column(String)
    details_exercise_style: Mapped[str | None] = mapped_column(String)
    details_expiration_date: Mapped[dt.date | None] = mapped_column(Date)
    details_shares_per_contract: Mapped[float | None] = mapped_column(Float)
    details_strike_price: Mapped[float | None] = mapped_column(Float)
    details_ticker: Mapped[str | None] = mapped_column(String)

    # `greeks` block (options only).
    greeks_delta: Mapped[float | None] = mapped_column(Float)
    greeks_gamma: Mapped[float | None] = mapped_column(Float)
    greeks_theta: Mapped[float | None] = mapped_column(Float)
    greeks_vega: Mapped[float | None] = mapped_column(Float)

    # `underlying_asset` block (options only).
    underlying_asset_change_to_break_even: Mapped[float | None] = mapped_column(Float)
    underlying_asset_last_updated: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=False))
    underlying_asset_price: Mapped[float | None] = mapped_column(Float)
    underlying_asset_ticker: Mapped[str | None] = mapped_column(String)
    underlying_asset_timeframe: Mapped[str | None] = mapped_column(String)
    underlying_asset_value: Mapped[float | None] = mapped_column(Float)

    fetched_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=False))


class TechnicalIndicator(Base):
    """One row per (ticker, indicator, timestamp) value returned by GET
    /v1/indicators/{sma,ema,macd,rsi}/{ticker} ("Technical Indicators"), `indicator`
    holding which of the four ("sma"/"ema"/"macd"/"rsi") the row came from - one shared
    table rather than four near-identical ones, same reasoning as UnifiedSnapshot
    covering every asset-class `type` in one table. Upserted by
    jobs/sync_indicators.py - a re-fetched (ticker, indicator, timestamp) overwrites the
    existing row, but unlike CurrentSnapshot's single-row-per-ticker semantics, distinct
    timestamps accumulate as separate rows since each is its own point in the
    indicator's time series.

    signal/histogram are MACD-only (massive.com's SMA/EMA/RSI responses carry just
    `value`) and left None for the other three indicators."""

    __tablename__ = "technical_indicators"

    ticker: Mapped[str] = mapped_column(ForeignKey("tickers.ticker"), primary_key=True)
    indicator: Mapped[str] = mapped_column(String, primary_key=True)
    timestamp: Mapped[dt.datetime] = mapped_column(DateTime(timezone=False), primary_key=True)
    value: Mapped[float | None] = mapped_column(Float)
    signal: Mapped[float | None] = mapped_column(Float)
    histogram: Mapped[float | None] = mapped_column(Float)
    fetched_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=False))


class AverageVolume(Base):
    """One row per (ticker, start_date, days_interval), the mean daily `ohlc_bars.volume`
    across the `days_interval` calendar days ending on (and including) `start_date`, for
    daily (multiplier=1, timespan="day") bars. Computed locally by
    jobs/average_volume.py from bars already synced by jobs/sync_bars.py - no massive.com
    call involved. Upserted - a re-run with the same start_date/days_interval overwrites
    the prior average rather than accumulating rows, since it's a derived statistic
    recomputed from ohlc_bars, not a fetched time series point.

    bar_count is how many of the window's daily bars actually existed for this ticker -
    useful to tell a full 50-day average apart from one computed off a newly-listed
    ticker with only a handful of days synced."""

    __tablename__ = "average_volumes"

    ticker: Mapped[str] = mapped_column(ForeignKey("tickers.ticker"), primary_key=True)
    start_date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    days_interval: Mapped[int] = mapped_column(Integer, primary_key=True)
    average_volume: Mapped[float | None] = mapped_column(Float)
    bar_count: Mapped[int] = mapped_column(Integer)
    computed_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=False))


class News(Base):
    """One row per article returned by GET /v2/reference/news, keyed by massive.com's
    own article `id` rather than by ticker - a single article often covers more than
    one ticker (see `tickers` below), so unlike CurrentSnapshot/UnifiedSnapshot there's
    no natural ticker-scoped primary key here. Upserted by jobs/sync_news.py.

    tickers/keywords are stored comma-joined - same reasoning as UnifiedSnapshot's
    last_trade_conditions, massive.com returns them as lists and there's no array
    column type on sqlite. insights (per-ticker sentiment) is a list of objects rather
    than of plain strings, so it's stored as a JSON-encoded string instead."""

    __tablename__ = "news"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    publisher_name: Mapped[str | None] = mapped_column(String)
    publisher_homepage_url: Mapped[str | None] = mapped_column(String)
    publisher_logo_url: Mapped[str | None] = mapped_column(String)
    publisher_favicon_url: Mapped[str | None] = mapped_column(String)
    title: Mapped[str | None] = mapped_column(String)
    author: Mapped[str | None] = mapped_column(String)
    published_utc: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=False))
    article_url: Mapped[str | None] = mapped_column(String)
    amp_url: Mapped[str | None] = mapped_column(String)
    image_url: Mapped[str | None] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(String)
    keywords: Mapped[str | None] = mapped_column(String)
    tickers: Mapped[str | None] = mapped_column(String)
    insights: Mapped[str | None] = mapped_column(String)
    fetched_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=False))


class JobConfig(Base):
    """Per-job settings, editable from the dashboard's Jobs page (see app/main.py).
    Seeded with defaults (app/main.py's _get_or_create_config) the first time a job's
    config is read - there's one row per jobs/registry.py entry once that happens.

    `run_type` is "manual" (runnable from the dashboard only) or "auto" (also eligible
    for the scheduled trigger - see app/main.py's _scheduled_job). A manual trigger
    (the dashboard's "Run now" button) works regardless of run_type, same as v1's
    ResearchSchedule.enabled bypass at the repo root. `schedule_interval_unit` ("minutes",
    "hours", or "days") and `schedule_interval_value` (the N) together describe a
    recurring "every N <unit>" cadence, anchored to `start_time` - a UTC "HH:MM" time of
    day (see registry.START_TIME_OPTIONS for the quarter-hour slots the dashboard offers)
    that app/main.py's _interval_trigger feeds to APScheduler's IntervalTrigger as its
    start_date, so the first fire lands on that time of day and every fire after that is
    exactly N <unit> later - e.g. start_time="00:15" with schedule_interval_unit="hours"
    fires at :15 past every hour. update_job_config reschedules the live APScheduler job
    when any of these change, so edits take effect immediately rather than needing a
    backend-v2 restart; the schedule is kept (and shown) even for a "manual" job, so
    switching it to "auto" later doesn't need re-entering it.

    tickers/multiplier/timespan/backfill_days only apply to the bars job
    (registry.JobDefinition.has_bars_fields) and are left None for the tickers job.
    ticker_types applies to both jobs: a single upstream `type` filter for the tickers
    job (jobs/sync_tickers.py's ticker_type param - None syncs every type), or a
    multi-select filter for the bars job, mutually exclusive with tickers there - see
    jobs/sync_bars.py's _resolve_tickers. Comma-separated either way.

    snapshot_types is unrelated to ticker_types - it only applies to the unified-
    snapshot job (registry.JobDefinition.has_snapshot_type_filter), filtering by
    massive.com's own asset-class `type` query param (see registry.SNAPSHOT_TYPE_OPTIONS
    and jobs/sync_unified_snapshot.py) rather than by anything in the tickers table.
    Comma-separated; left None to sync every asset class.

    average_volume_start_date/average_volume_days_interval only apply to the average-
    volume job (registry.JobDefinition.has_average_volume_fields). Both are left None by
    default rather than seeded with a concrete value at config-creation time the way
    bars' multiplier/timespan/backfill_days are - jobs/average_volume.py resolves a None
    start date to "yesterday" and a None days interval to 50 at *run* time, so a job left
    on defaults tracks "yesterday" on every run instead of freezing to whatever date its
    config row happened to be created on."""

    __tablename__ = "job_configs"

    job_name: Mapped[str] = mapped_column(String, primary_key=True)
    run_type: Mapped[str] = mapped_column(String, default="auto")
    schedule_interval_unit: Mapped[str] = mapped_column(String, default="days")
    schedule_interval_value: Mapped[int] = mapped_column(Integer, default=1)
    start_time: Mapped[str] = mapped_column(String, default="00:00")
    ticker_types: Mapped[str | None] = mapped_column(String)
    tickers: Mapped[str | None] = mapped_column(String)
    multiplier: Mapped[int | None] = mapped_column(Integer)
    timespan: Mapped[str | None] = mapped_column(String)
    backfill_days: Mapped[int | None] = mapped_column(Integer)
    snapshot_types: Mapped[str | None] = mapped_column(String)
    average_volume_start_date: Mapped[dt.date | None] = mapped_column(Date)
    average_volume_days_interval: Mapped[int | None] = mapped_column(Integer)
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
