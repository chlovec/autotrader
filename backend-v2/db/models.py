"""backend-v2's own schema - a separate database from v1's db/models.py at the repo root."""

import datetime as dt

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String
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

    # Last date this ticker's daily OHLC bars were synced, distinct from ohlc_bars'
    # own MAX(timestamp) (which the stale-tickers report computes live - see
    # app/main.py's stale_tickers_report) - this tracks when a sync last ran for the
    # ticker, not the date of its most recent bar. Left NULL for now; not yet written
    # by jobs/sync_bars.py - a future job will populate it.
    last_ohlc_sync_date: Mapped[dt.date | None] = mapped_column(Date)


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

    # Operator-set display/priority ordering and active/inactive flag, edited from the
    # dashboard's Settings > Ticker Types page (see app/main.py's ticker-types
    # endpoints) - unrelated to massive.com's own data, so untouched by
    # sync-ticker-types' upsert. rank is nullable (unranked sorts last); status
    # defaults to "active" for both new rows and existing ones backfilled by this
    # column's migration (see db/session.py's _add_ticker_types_rank_status_columns).
    rank: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String, default="active")


class TickerGroup(Base):
    """One row per (ticker, group) pairing - a plain many-to-many junction letting a
    ticker belong to more than one group (e.g. "watchlist", "core_holdings"). group is
    just a caller-chosen label rather than a foreign key into a separate group-catalog
    table, since a group here carries no metadata of its own beyond its name.

    created_at records when the ticker was added to the group."""

    __tablename__ = "ticker_groups"

    ticker: Mapped[str] = mapped_column(ForeignKey("tickers.ticker"), primary_key=True)
    group: Mapped[str] = mapped_column(String, primary_key=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=False))


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

    # Percent change from open to close, e.g. 1.5 for a 1.5% gain. Computed in Python
    # and stored plainly (see jobs/sync_bars.py's _upsert_bar) rather than as a
    # DB-level generated column, so a raw INSERT/UPDATE bypassing that helper wouldn't
    # populate it - but every write to this table already goes through _upsert_bar,
    # and a plain column keeps this ordinary rather than needing sqlite's generated-
    # column support (which some tooling doesn't handle well) - see db/session.py's
    # _add_ohlc_bars_pcnt_increase_column for the migration that adds it (and backfills
    # existing rows) on a database created before this column existed.
    pcnt_increase: Mapped[float | None] = mapped_column(Float)


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


class TickerDetail(Base):
    """One row per ticker's company/fundamentals data from GET /v3/reference/
    tickers/{ticker} - massive.com's *singular* ticker-details endpoint, distinct from
    the paged list endpoint (GET /v3/reference/tickers) jobs/sync_tickers.py already
    syncs into Ticker itself. The list endpoint's per-ticker payload doesn't carry
    market_cap/shares-outstanding at all; this is the only sync job that fetches them.

    Kept as its own table rather than added onto Ticker, same reasoning as
    CurrentSnapshot: a separate per-ticker fetch with its own cadence, upserted
    wholesale on every run (there's no history kept) rather than accumulated - see
    jobs/sync_ticker_details.py."""

    __tablename__ = "ticker_details"

    ticker: Mapped[str] = mapped_column(ForeignKey("tickers.ticker"), primary_key=True)
    market_cap: Mapped[float | None] = mapped_column(Float)
    share_class_shares_outstanding: Mapped[float | None] = mapped_column(Float)
    weighted_shares_outstanding: Mapped[float | None] = mapped_column(Float)
    sic_code: Mapped[str | None] = mapped_column(String)
    sic_description: Mapped[str | None] = mapped_column(String)
    homepage_url: Mapped[str | None] = mapped_column(String)
    total_employees: Mapped[int | None] = mapped_column(Integer)
    list_date: Mapped[dt.date | None] = mapped_column(Date)
    round_lot: Mapped[int | None] = mapped_column(Integer)
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


class MarketPrediction(Base):
    """One row per (ticker, predicted_date), the next-session state prediction produced
    by jobs/predict_market_state.py's compute_market_state_predictions. Purely local -
    no massive.com call, reads daily bars already synced by jobs/sync_bars.py into
    ohlc_bars, same reasoning as AverageVolume.

    A first-order Markov chain is fit per ticker on its own history of daily
    close-to-close returns, each bucketed into one of five quantile-based states
    (strong_down/down/flat/up/strong_up - see predict_market_state.STATE_LABELS).
    current_state is the bucket the most recent bar's return fell into;
    predicted_state is the state with the highest observed transition probability out
    of current_state (state_confidence), falling back to the unconditional state
    distribution if current_state was never observed as a "from" state before.

    entry_price/exit_price are today's close and that close projected forward by
    predicted_state's historical mean return (expected_return) - not a forecast of an
    actual next-session open/close, since daily bars carry no intraday price path.
    entry_time/exit_time are therefore fixed constants (regular-session open/close),
    not predicted values - see the module docstring for why daily-bar data can't
    support a real time-of-day prediction.

    exit_price_confidence is 1 - (predicted_state's historical return std / (1 +
    expected_return)) - the same coefficient-of-variation formula shape as
    MarketPredictionMonteCarlo.exit_price_confidence, rescaled from return-space to
    price-space, so the two are directly comparable. Unlike the Monte Carlo job's
    (which simulates the state draw too, so its spread reflects both which state and
    what return within it), this only reflects the predicted state's own historical
    return spread - predicted_state itself is a single deterministic argmax pick with
    no uncertainty of its own to add in.

    Upserted - a re-run for the same (ticker, predicted_date) overwrites the prior
    prediction rather than accumulating rows, same semantics as AverageVolume."""

    __tablename__ = "market_predictions"

    ticker: Mapped[str] = mapped_column(ForeignKey("tickers.ticker"), primary_key=True)
    predicted_date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    current_state: Mapped[str] = mapped_column(String)
    predicted_state: Mapped[str] = mapped_column(String)
    state_confidence: Mapped[float] = mapped_column(Float)
    expected_return: Mapped[float] = mapped_column(Float)
    entry_price: Mapped[float] = mapped_column(Float)
    exit_price: Mapped[float] = mapped_column(Float)
    exit_price_confidence: Mapped[float] = mapped_column(Float)
    entry_time: Mapped[str] = mapped_column(String)
    exit_time: Mapped[str] = mapped_column(String)
    history_days: Mapped[int] = mapped_column(Integer)
    computed_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=False))


class MarketPredictionBacktest(Base):
    """One row per (ticker, evaluated_date), a walk-forward evaluation of
    predict_market_state.py's approach against history that had already happened by
    the time of the run - produced by jobs/backtest_market_state.py's
    compute_market_state_backtest. Purely local, same reasoning as MarketPrediction.

    For each evaluated_date within the requested range, the model is re-fit exactly as
    MarketPrediction does, but using only ohlc_bars up through as_of_date (the prior
    trading day) - current_state/predicted_state/state_confidence/expected_return/
    entry_price mean the same thing as MarketPrediction's columns of the same name,
    computed the same way. actual_state is evaluated_date's *actually realized* return
    bucketed against the cut points fit on history through as_of_date (not refit to
    include evaluated_date - the walk-forward equivalent of no lookahead bias), and
    actual_exit_price is evaluated_date's real close. predicted_correct is just
    predicted_state == actual_state; price_error_pct is
    (predicted_exit_price - actual_exit_price) / actual_exit_price.

    Upserted - a re-run for the same (ticker, evaluated_date) overwrites the prior
    result rather than accumulating rows, same semantics as MarketPrediction."""

    __tablename__ = "market_prediction_backtests"

    ticker: Mapped[str] = mapped_column(ForeignKey("tickers.ticker"), primary_key=True)
    evaluated_date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    as_of_date: Mapped[dt.date] = mapped_column(Date)
    current_state: Mapped[str] = mapped_column(String)
    predicted_state: Mapped[str] = mapped_column(String)
    actual_state: Mapped[str] = mapped_column(String)
    predicted_correct: Mapped[bool] = mapped_column(Boolean)
    state_confidence: Mapped[float] = mapped_column(Float)
    expected_return: Mapped[float] = mapped_column(Float)
    entry_price: Mapped[float] = mapped_column(Float)
    predicted_exit_price: Mapped[float] = mapped_column(Float)
    actual_exit_price: Mapped[float] = mapped_column(Float)
    price_error_pct: Mapped[float] = mapped_column(Float)
    history_days: Mapped[int] = mapped_column(Integer)
    computed_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=False))


class MarketPrediction10Day(Base):
    """One row per (ticker, start_date) - a 10-trading-day-ahead projection produced by
    jobs/predict_market_state_10_day.py's compute_10_day_market_state_predictions.
    Purely local, same reasoning as MarketPrediction; reuses that module's fitted
    per-ticker Markov chain (STATE_LABELS, quantile buckets, transition matrix) rather
    than fitting its own.

    Unlike MarketPrediction (which always predicts off the *latest* synced bar),
    start_date is caller-chosen (dashboard default: tomorrow, UTC) and the chain is fit
    only on ohlc_bars strictly before it - current_state is the bucket of the last such
    bar's return, matching MarketPrediction's current_state semantics but anchored to
    start_date's cutoff instead of "now".

    day1_* is predicted the same way MarketPrediction's single prediction is (from
    current_state, via the transition matrix). day2_* through day10_* are NOT re-fit
    against new data - there isn't any yet for a start_date that's usually in the
    future - each is a further one-step walk of the *same* transition matrix, starting
    from the *previous* day's predicted_state, exactly like a Markov chain's stationary
    forward simulation. entry_price/exit_price compound along that same walk (day N's
    entry_price is day N-1's exit_price; day 1's is the last close before start_date).
    entry_time/exit_time are fixed per day (regular session open/close), same reasoning
    as MarketPrediction's columns of the same name - no intraday path to predict from.

    expected_return_pct is a percentage (e.g. -2.5), NOT a fraction - unlike
    MarketPrediction.expected_return, which is a fraction converted to a percent only
    client-side. There's no per-day date column: "Day N" is implicitly start_date
    walked forward N-1 trading days (see predict_market_state.py's _next_trading_day),
    not persisted.

    Upserted - a re-run for the same (ticker, start_date) overwrites the prior
    prediction rather than accumulating rows, same semantics as MarketPrediction."""

    __tablename__ = "market_predictions_10_day"

    ticker: Mapped[str] = mapped_column(ForeignKey("tickers.ticker"), primary_key=True)
    start_date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    current_state: Mapped[str] = mapped_column(String)

    day1_predicted_state: Mapped[str] = mapped_column(String)
    day1_state_confidence: Mapped[float] = mapped_column(Float)
    day1_entry_price: Mapped[float] = mapped_column(Float)
    day1_exit_price: Mapped[float] = mapped_column(Float)
    day1_expected_return_pct: Mapped[float] = mapped_column(Float)
    day1_entry_time: Mapped[str] = mapped_column(String)
    day1_exit_time: Mapped[str] = mapped_column(String)

    day2_predicted_state: Mapped[str] = mapped_column(String)
    day2_state_confidence: Mapped[float] = mapped_column(Float)
    day2_entry_price: Mapped[float] = mapped_column(Float)
    day2_exit_price: Mapped[float] = mapped_column(Float)
    day2_expected_return_pct: Mapped[float] = mapped_column(Float)
    day2_entry_time: Mapped[str] = mapped_column(String)
    day2_exit_time: Mapped[str] = mapped_column(String)

    day3_predicted_state: Mapped[str] = mapped_column(String)
    day3_state_confidence: Mapped[float] = mapped_column(Float)
    day3_entry_price: Mapped[float] = mapped_column(Float)
    day3_exit_price: Mapped[float] = mapped_column(Float)
    day3_expected_return_pct: Mapped[float] = mapped_column(Float)
    day3_entry_time: Mapped[str] = mapped_column(String)
    day3_exit_time: Mapped[str] = mapped_column(String)

    day4_predicted_state: Mapped[str] = mapped_column(String)
    day4_state_confidence: Mapped[float] = mapped_column(Float)
    day4_entry_price: Mapped[float] = mapped_column(Float)
    day4_exit_price: Mapped[float] = mapped_column(Float)
    day4_expected_return_pct: Mapped[float] = mapped_column(Float)
    day4_entry_time: Mapped[str] = mapped_column(String)
    day4_exit_time: Mapped[str] = mapped_column(String)

    day5_predicted_state: Mapped[str] = mapped_column(String)
    day5_state_confidence: Mapped[float] = mapped_column(Float)
    day5_entry_price: Mapped[float] = mapped_column(Float)
    day5_exit_price: Mapped[float] = mapped_column(Float)
    day5_expected_return_pct: Mapped[float] = mapped_column(Float)
    day5_entry_time: Mapped[str] = mapped_column(String)
    day5_exit_time: Mapped[str] = mapped_column(String)

    day6_predicted_state: Mapped[str] = mapped_column(String)
    day6_state_confidence: Mapped[float] = mapped_column(Float)
    day6_entry_price: Mapped[float] = mapped_column(Float)
    day6_exit_price: Mapped[float] = mapped_column(Float)
    day6_expected_return_pct: Mapped[float] = mapped_column(Float)
    day6_entry_time: Mapped[str] = mapped_column(String)
    day6_exit_time: Mapped[str] = mapped_column(String)

    day7_predicted_state: Mapped[str] = mapped_column(String)
    day7_state_confidence: Mapped[float] = mapped_column(Float)
    day7_entry_price: Mapped[float] = mapped_column(Float)
    day7_exit_price: Mapped[float] = mapped_column(Float)
    day7_expected_return_pct: Mapped[float] = mapped_column(Float)
    day7_entry_time: Mapped[str] = mapped_column(String)
    day7_exit_time: Mapped[str] = mapped_column(String)

    day8_predicted_state: Mapped[str] = mapped_column(String)
    day8_state_confidence: Mapped[float] = mapped_column(Float)
    day8_entry_price: Mapped[float] = mapped_column(Float)
    day8_exit_price: Mapped[float] = mapped_column(Float)
    day8_expected_return_pct: Mapped[float] = mapped_column(Float)
    day8_entry_time: Mapped[str] = mapped_column(String)
    day8_exit_time: Mapped[str] = mapped_column(String)

    day9_predicted_state: Mapped[str] = mapped_column(String)
    day9_state_confidence: Mapped[float] = mapped_column(Float)
    day9_entry_price: Mapped[float] = mapped_column(Float)
    day9_exit_price: Mapped[float] = mapped_column(Float)
    day9_expected_return_pct: Mapped[float] = mapped_column(Float)
    day9_entry_time: Mapped[str] = mapped_column(String)
    day9_exit_time: Mapped[str] = mapped_column(String)

    day10_predicted_state: Mapped[str] = mapped_column(String)
    day10_state_confidence: Mapped[float] = mapped_column(Float)
    day10_entry_price: Mapped[float] = mapped_column(Float)
    day10_exit_price: Mapped[float] = mapped_column(Float)
    day10_expected_return_pct: Mapped[float] = mapped_column(Float)
    day10_entry_time: Mapped[str] = mapped_column(String)
    day10_exit_time: Mapped[str] = mapped_column(String)

    computed_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=False))


class MarketPredictionMonteCarlo(Base):
    """One row per (ticker, predicted_date), produced by jobs/predict_market_state_mcmc.py's
    compute_market_state_mcmc_predictions. Purely local, same reasoning as
    MarketPrediction; reuses jobs/predict_market_state.py's fitted per-ticker Markov
    chain (STATE_LABELS, quantile buckets, transition matrix) rather than fitting its
    own.

    Unlike MarketPrediction's single deterministic argmax step, this runs
    num_simulations independent random walks: each path draws a next state weighted by
    the fitted transition probabilities (predict_market_state._transition_row) rather
    than always taking the most likely one, then draws that state's return from
    Normal(bucket_mean, bucket_std) - a genuine Monte Carlo simulation *of* the fitted
    Markov chain (not "MCMC" in the stricter Metropolis-Hastings/Gibbs sampling sense).

    current_state/entry_price mean the same thing as MarketPrediction's columns of the
    same name (entry_price is deterministic - today's/predicted_date's last close
    before the cutoff, no simulation involved). predicted_state is the most-frequently
    simulated next state across all paths; state_confidence is its frequency /
    num_simulations - a simulated estimate of the same transition probability
    MarketPrediction.state_confidence computes analytically. expected_return is a
    fraction (exit_price_mean / entry_price - 1), same convention as
    MarketPrediction.expected_return; exit_price is entry_price * (1 + expected_return) -
    same formula, and therefore the same value, as exit_price_mean, kept as its own
    column so this table carries a single-point exit price the same shape as
    MarketPrediction.exit_price for consumers that want one. exit_price_mean/
    exit_price_std/exit_price_p10/exit_price_p50/exit_price_p90 summarize the full
    simulated exit-price distribution across all paths.

    exit_price_confidence is 1 - (exit_price_std / exit_price_mean) - a 0-1 score (can
    go negative if std exceeds mean) where a tight simulated spread scores near 1 and a
    wide/uncertain one scores lower; 0.0 if exit_price_mean is 0. Distinct from
    state_confidence, which measures confidence in the predicted *state*, not the
    price. There is no entry_price_confidence - entry_price is today's/
    predicted_date's already-observed last close before the cutoff, not simulated, so
    it carries no uncertainty to quantify.

    entry_time/exit_time are fixed constants, same reasoning as MarketPrediction's
    columns of the same name.

    Upserted - a re-run for the same (ticker, predicted_date) overwrites the prior
    prediction rather than accumulating rows, same semantics as MarketPrediction."""

    __tablename__ = "market_predictions_mcmc"

    ticker: Mapped[str] = mapped_column(ForeignKey("tickers.ticker"), primary_key=True)
    predicted_date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    current_state: Mapped[str] = mapped_column(String)
    predicted_state: Mapped[str] = mapped_column(String)
    state_confidence: Mapped[float] = mapped_column(Float)
    expected_return: Mapped[float] = mapped_column(Float)
    entry_price: Mapped[float] = mapped_column(Float)
    exit_price: Mapped[float] = mapped_column(Float)
    exit_price_mean: Mapped[float] = mapped_column(Float)
    exit_price_std: Mapped[float] = mapped_column(Float)
    exit_price_confidence: Mapped[float] = mapped_column(Float)
    exit_price_p10: Mapped[float] = mapped_column(Float)
    exit_price_p50: Mapped[float] = mapped_column(Float)
    exit_price_p90: Mapped[float] = mapped_column(Float)
    entry_time: Mapped[str] = mapped_column(String)
    exit_time: Mapped[str] = mapped_column(String)
    # Persisted (rather than assumed constant) since it's a per-run JobConfig knob - see
    # JobConfig.mcmc_num_simulations - and can therefore differ between two runs of the
    # same ticker/predicted_date.
    num_simulations: Mapped[int] = mapped_column(Integer)
    history_days: Mapped[int] = mapped_column(Integer)
    computed_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=False))


class WinRate(Base):
    """One row per ticker, produced by jobs/win_rates.py's compute_win_rates. Purely
    local - no massive.com call, reads market_predictions/market_predictions_mcmc
    (predicted direction) joined against ohlc_bars.pcnt_increase (actual direction) for
    every predicted_date whose outcome is already known.

    A prediction "wins" (WON if both are <= 0, WIN if both are >= 0 - same two-label
    split as jobs/win_rates.py's SQL, both meaning the predicted and actual direction
    agreed) when its predicted_state's expected_return and the actual close-to-close
    pcnt_increase on predicted_date are on the same side of zero; otherwise it's a
    loss. *_predictions_count only counts predictions with a known actual outcome
    (i.e. an ohlc_bars row already synced for predicted_date) - not every prediction
    ever made for the ticker, some of which may still be pending. *_win_rate is
    win_count / predictions_count, left NULL when predictions_count is 0 rather than
    dividing by zero.

    mcmc_result is scored against market_predictions_mcmc the same way markov_result is
    scored against market_predictions - a ticker with a market_predictions row but no
    matching market_predictions_mcmc row still counts toward mcmc_predictions_count (as
    a loss), mirroring jobs/win_rates.py's SQL exactly rather than excluding it.

    Upserted - a re-run overwrites the prior row for a ticker rather than accumulating
    history, same semantics as AverageVolume."""

    __tablename__ = "win_rates"

    ticker: Mapped[str] = mapped_column(ForeignKey("tickers.ticker"), primary_key=True)
    last_updated: Mapped[dt.datetime] = mapped_column(DateTime(timezone=False))
    mcmc_win_count: Mapped[int] = mapped_column(Integer)
    mcmc_predictions_count: Mapped[int] = mapped_column(Integer)
    mcmc_win_rate: Mapped[float | None] = mapped_column(Float)
    markov_win_count: Mapped[int] = mapped_column(Integer)
    markov_predictions_count: Mapped[int] = mapped_column(Integer)
    markov_win_rate: Mapped[float | None] = mapped_column(Float)


class ResearchPick(Base):
    """One row per (job_runs.id, ticker) - one of jobs/research_picks.py's
    compute_research_picks' up-to-20 selections for that run, screened and scored
    purely from already-synced/computed local tables (market_predictions,
    market_predictions_mcmc, win_rates, market_prediction_backtests, ticker_details,
    average_volumes, technical_indicators, news) - no massive.com call, no v1 Alpaca
    broker call, and no trade is placed or simulated. This is a research shortlist for
    further human investigation, not an autonomous trading signal - see that module's
    docstring.

    Keyed by (run_id, ticker) rather than upserted by ticker alone: unlike WinRate/
    AverageVolume (a single current snapshot per ticker), a human needs to compare
    today's picks against a prior run's, so every run's selections accumulate as their
    own rows rather than overwriting the last run's - same reasoning as
    MarketPredictionBacktest accumulating one row per (ticker, evaluated_date) rather
    than upserting. run_id ties each row back to the job_runs row that produced it, so
    the dashboard can list distinct past runs and show one run's picks at a time.

    rank is this ticker's 1-based position within its run (1 = highest score) - at most
    20 rows share a run_id. score is the final composite score picks were ranked by;
    the *_score/*_adjustment columns below are its components, persisted individually
    so the results page can show a breakdown rather than just the final number. Every
    win-rate/backtest/RSI/news field is nullable - each is an optional, neutral-when-
    absent signal (see jobs/research_picks.py), not a required input."""

    __tablename__ = "research_picks"

    run_id: Mapped[int] = mapped_column(ForeignKey("job_runs.id"), primary_key=True)
    ticker: Mapped[str] = mapped_column(ForeignKey("tickers.ticker"), primary_key=True)
    rank: Mapped[int] = mapped_column(Integer)
    predicted_date: Mapped[dt.date] = mapped_column(Date)
    predicted_direction: Mapped[str] = mapped_column(String)
    score: Mapped[float] = mapped_column(Float)

    # Score breakdown - persisted individually for the results page's per-pick detail.
    expected_return_score: Mapped[float] = mapped_column(Float)
    confidence_score: Mapped[float] = mapped_column(Float)
    win_rate_score: Mapped[float] = mapped_column(Float)
    backtest_score: Mapped[float] = mapped_column(Float)
    rsi_adjustment: Mapped[float] = mapped_column(Float)
    news_adjustment: Mapped[float] = mapped_column(Float)

    # Raw values behind the scores above, for display.
    markov_predicted_state: Mapped[str] = mapped_column(String)
    markov_expected_return: Mapped[float] = mapped_column(Float)
    markov_state_confidence: Mapped[float] = mapped_column(Float)
    mcmc_predicted_state: Mapped[str] = mapped_column(String)
    mcmc_expected_return: Mapped[float] = mapped_column(Float)
    mcmc_state_confidence: Mapped[float] = mapped_column(Float)
    market_cap: Mapped[float] = mapped_column(Float)
    average_volume: Mapped[float] = mapped_column(Float)
    markov_win_rate: Mapped[float | None] = mapped_column(Float)
    markov_predictions_count: Mapped[int | None] = mapped_column(Integer)
    mcmc_win_rate: Mapped[float | None] = mapped_column(Float)
    mcmc_predictions_count: Mapped[int | None] = mapped_column(Integer)
    backtest_win_rate: Mapped[float | None] = mapped_column(Float)
    backtest_evaluated_count: Mapped[int | None] = mapped_column(Integer)
    rsi_value: Mapped[float | None] = mapped_column(Float)
    news_article_count: Mapped[int | None] = mapped_column(Integer)
    # Count of positive-sentiment articles minus negative-sentiment ones, among the
    # ticker's matched articles within the lookback window - see
    # jobs/research_picks.py's _news_signal.
    news_sentiment_lean: Mapped[int | None] = mapped_column(Integer)

    comment: Mapped[str] = mapped_column(String)
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

    tickers/multiplier/timespan/backfill_days/bars_end_date_offset_days only apply to
    the bars job (registry.JobDefinition.has_bars_fields) and are left None for the
    tickers job. ticker_types applies to both jobs: a single upstream `type` filter for
    the tickers job (jobs/sync_tickers.py's ticker_type param - None syncs every type),
    or a multi-select filter for the bars job, mutually exclusive with tickers there -
    see jobs/sync_bars.py's _resolve_tickers. Comma-separated either way.

    bars_end_date_offset_days is a signed integer offset in days from today (UTC) -
    e.g. 1 for "through yesterday" (sync_bars_nightly's historical fixed behavior), 0
    for "through today" - rather than a literal date, same "left None, resolved at run
    time" reasoning as average_volume_start_date below, defaulting to
    jobs/sync_bars.py's DEFAULT_END_DATE_OFFSET_DAYS (1) when unset.

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
    config row happened to be created on.

    backtest_start_date/backtest_end_date only apply to the backtest job
    (registry.JobDefinition.has_backtest_fields), same "left None, resolved at run time"
    reasoning as average_volume_start_date - jobs/backtest_market_state.py resolves a
    None end date to "yesterday" and a None start date to 90 days before that.

    prediction_start_date only applies to the predict-10-day-market-state job
    (registry.JobDefinition.has_prediction_start_date_field), same "left None, resolved
    at run time" reasoning as average_volume_start_date -
    jobs/predict_market_state_10_day.py resolves a None start date to tomorrow (UTC).

    predicted_date_offset_days only applies to the predict-market-state job
    (registry.JobDefinition.has_predicted_date_offset_field) - unlike
    prediction_start_date above, it's an integer *offset* in days from today (UTC)
    rather than a literal date (e.g. +1 for tomorrow, 0 for today, -1 for yesterday),
    resolved to a concrete date once per run by jobs/engine.py's run_job. Left None,
    same "resolved at run time" reasoning as average_volume_start_date, defaulting to
    jobs/predict_market_state.py's DEFAULT_PREDICTED_DATE_OFFSET_DAYS (+1, tomorrow).
    That resolved date feeds both phases of the predict-market-state job's run - the
    Markov chain prediction (jobs/predict_market_state.py) and, immediately after it,
    the Monte Carlo simulation over that same chain (jobs/predict_market_state_mcmc.py) -
    so a single run's two stored predictions always target the same session.

    mcmc_num_simulations only applies to the predict-market-state job's Monte Carlo
    phase (registry.JobDefinition.has_monte_carlo_fields), same "left None, resolved at
    run time" reasoning as average_volume_start_date -
    jobs/predict_market_state_mcmc.py resolves a None value to
    DEFAULT_NUM_SIMULATIONS (2000).

    ohlc_bars_start_date/ohlc_bars_end_date/ohlc_bars_limit only apply to the
    sync-ohlc-bars job (registry.JobDefinition.has_ohlc_bars_fields), same "left None,
    resolved at run time" reasoning as average_volume_start_date -
    jobs/sync_ohlc_bars.py's sync_ohlc_bars resolves a None ohlc_bars_start_date to 2
    years before today (UTC), a None ohlc_bars_end_date to today (UTC; a caller-given
    date after today is rejected, not clamped), and a None ohlc_bars_limit to 8000
    (capped at 10000 regardless of what's stored here).

    run_requested_at is how app/main.py (the API process) asks job_runner.py (the
    separate process that actually executes jobs - see jobs/engine.py) to run this job
    now: POST /jobs/{name}/run sets it, job_runner.py's poll_run_requests clears it
    once it picks the request up and starts the run. The two processes coordinate
    purely through this DB row rather than a direct call/socket - see jobs/engine.py's
    module docstring for why polling is enough here."""

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
    bars_end_date_offset_days: Mapped[int | None] = mapped_column(Integer)
    snapshot_types: Mapped[str | None] = mapped_column(String)
    average_volume_start_date: Mapped[dt.date | None] = mapped_column(Date)
    average_volume_days_interval: Mapped[int | None] = mapped_column(Integer)
    backtest_start_date: Mapped[dt.date | None] = mapped_column(Date)
    backtest_end_date: Mapped[dt.date | None] = mapped_column(Date)
    prediction_start_date: Mapped[dt.date | None] = mapped_column(Date)
    predicted_date_offset_days: Mapped[int | None] = mapped_column(Integer)
    mcmc_num_simulations: Mapped[int | None] = mapped_column(Integer)
    ohlc_bars_start_date: Mapped[dt.date | None] = mapped_column(Date)
    ohlc_bars_end_date: Mapped[dt.date | None] = mapped_column(Date)
    ohlc_bars_limit: Mapped[int | None] = mapped_column(Integer)
    # Hides the job's card from the Jobs page's default list (see app/main.py's
    # list_jobs) without affecting its schedule - a hidden job still runs normally.
    hidden: Mapped[bool] = mapped_column(default=False)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=False), default=dt.datetime.utcnow)
    run_requested_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=False))


class JobRun(Base):
    """One row per job execution, manual or scheduled - backs the dashboard's Jobs page
    run history and "currently running" status. Written by app/main.py's _run_job:
    inserted with status="in_progress" before the job starts, then updated to
    "completed" or "failed" when it finishes.

    `trigger` is "manual" (dashboard "Run now"/play-button run) or "auto" (fired by
    the schedule) - this doubles as the dashboard's "last run mode" display.

    pause_requested/cancel_requested are how app/main.py's POST /jobs/{name}/pause|
    resume|cancel signal a run in progress in job_runner.py's separate process: the API
    process UPDATEs these directly on the job's current in_progress row, and
    job_runner.py's poll_control_relay polls them and mirrors them into that run's
    in-memory JobControl (see jobs/control.py) - only the relay poll touches the DB;
    JobControl's own checkpoint_sync/checkpoint_async (called once per unit of work,
    potentially tens of thousands of times per run) stay a plain in-memory check."""

    __tablename__ = "job_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_name: Mapped[str] = mapped_column(String, index=True)
    trigger: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=False))
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=False))
    result_summary: Mapped[str | None] = mapped_column(String)
    error: Mapped[str | None] = mapped_column(String)
    # Only set for jobs that know their full unit count up front and report as they go
    # (see jobs/control.py's report_job_progress) - sync-bars-nightly, sync-snapshots,
    # sync-ticker-details, the technical-indicator jobs. Null for every other job.
    progress_completed: Mapped[int | None] = mapped_column(Integer)
    progress_total: Mapped[int | None] = mapped_column(Integer)
    pause_requested: Mapped[bool] = mapped_column(default=False)
    cancel_requested: Mapped[bool] = mapped_column(default=False)
