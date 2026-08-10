"""Computes each selected ticker's average daily `ohlc_bars.volume` across a trailing
window of `days_interval` calendar days ending on `start_date` (inclusive), and upserts
the result into the average_volumes table.

Purely local aggregation over already-synced bars - no massive.com call involved, so
unlike jobs/sync_indicators.py this needs no DataClient or thread pool: a single GROUP BY
query covers every selected ticker at once.
"""

import datetime as dt
import logging

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from db.models import AverageVolume, OhlcBar, Ticker
from jobs.control import JobControl

logger = logging.getLogger("backend_v2.jobs.average_volume")

DEFAULT_DAYS_INTERVAL = 50
DEFAULT_MULTIPLIER = 1
DEFAULT_TIMESPAN = "day"

# Commits every this-many tickers instead of once at the very end - same reasoning as
# jobs/predict_market_state.py's COMMIT_BATCH_SIZE.
COMMIT_BATCH_SIZE = 500


def _apply_ticker_filter(query, ticker_types: list[str] | None, tickers: list[str] | None):
    """Unlike jobs/sync_indicators.py's _resolve_tickers - which has to materialize the
    selected list in Python either way, since each ticker becomes its own HTTP request -
    this filters ohlc_bars in SQL directly, without ever pulling the tickers table into
    Python. That matters at this table's scale: "every ticker" (both filters left unset)
    can be tens of thousands of rows, and binding that many as literal `IN (?, ?, ...)`
    parameters blows past sqlite's ~32766-variable-per-statement limit. A ticker_types
    filter instead becomes a correlated `IN (SELECT ticker FROM tickers WHERE type IN
    (...))` subquery - still one statement, no matter how many tickers that type
    matches. tickers (explicit, user-typed) stays a literal IN - it's never large enough
    to matter."""
    if tickers and ticker_types:
        raise ValueError("specify tickers or ticker_types, not both")
    if tickers:
        return query.where(OhlcBar.ticker.in_(tickers))
    if ticker_types:
        return query.where(OhlcBar.ticker.in_(select(Ticker.ticker).where(Ticker.type.in_(ticker_types))))
    return query


def compute_average_volume(
    session: Session,
    start_date: dt.date | None = None,
    days_interval: int = DEFAULT_DAYS_INTERVAL,
    ticker_types: list[str] | None = None,
    tickers: list[str] | None = None,
    multiplier: int = DEFAULT_MULTIPLIER,
    timespan: str = DEFAULT_TIMESPAN,
    control: JobControl | None = None,
) -> int:
    """`start_date` defaults to yesterday (UTC) - the most recent date a full day's bars
    are expected to already be synced by the nightly bars job. The averaging window is
    the `days_interval` calendar days ending on (and including) start_date.

    Runs off the event loop (see app/main.py's _run_job, which calls this via
    asyncio.to_thread), so control.checkpoint_sync is used rather than checkpoint_async -
    checked once up front since the whole computation is one grouped query, not a loop
    over per-ticker calls the way sync_indicators/sync_snapshots need to checkpoint
    between."""
    if start_date is None:
        start_date = dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)
    if days_interval < 1:
        raise ValueError("days_interval must be at least 1")

    if control is not None:
        control.checkpoint_sync()

    range_start = dt.datetime.combine(start_date - dt.timedelta(days=days_interval - 1), dt.time.min)
    range_end = dt.datetime.combine(start_date, dt.time.max)

    query = (
        select(OhlcBar.ticker, func.avg(OhlcBar.volume), func.count(OhlcBar.volume))
        .where(
            OhlcBar.multiplier == multiplier,
            OhlcBar.timespan == timespan,
            OhlcBar.timestamp >= range_start,
            OhlcBar.timestamp <= range_end,
        )
        .group_by(OhlcBar.ticker)
    )
    query = _apply_ticker_filter(query, ticker_types, tickers)
    rows = session.execute(query).all()

    computed_at = dt.datetime.utcnow()
    stored = 0
    for ticker, average_volume, bar_count in rows:
        values = {
            "ticker": ticker,
            "start_date": start_date,
            "days_interval": days_interval,
            "average_volume": average_volume,
            "bar_count": bar_count,
            "computed_at": computed_at,
        }
        stmt = sqlite_insert(AverageVolume).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[AverageVolume.ticker, AverageVolume.start_date, AverageVolume.days_interval],
            set_=values,
        )
        session.execute(stmt)
        stored += 1
        if stored % COMMIT_BATCH_SIZE == 0:
            session.commit()
    session.commit()

    logger.info(
        "computed average volume for %d ticker(s) over %d day(s) ending %s",
        stored,
        days_interval,
        start_date,
    )
    return stored
