"""Selects a bounded batch of tickers due for an OHLC bars sync - via the query in
temp_queries/sync_ohlc_bars.sql, reproduced below as a bound-parameter SQL statement -
and hands each one off, together with its own resolved new_start_date and the run's
shared new_end_date, to sync_ohlc_bars_worker, fanned out across a ThreadPoolExecutor.

Distinct from jobs/sync_bars.py's sync_bars_nightly: that job resolves every known
ticker's start date from ohlc_bars.MAX(timestamp) directly and has no selection limit.
This job instead selects up to `limit` tickers - ordered by ticker_types.rank, then
ticker - whose tickers.last_ohlc_sync_date is NULL or before end_date, and each
worker stamps tickers.last_ohlc_sync_date with end_date once its fetch completes, so
the next run's selection naturally rotates on to tickers not yet covered this cycle
once a run's `limit` is smaller than the number of tickers due.
"""

import asyncio
import concurrent.futures
import datetime as dt
import logging
import os
from typing import Callable

from sqlalchemy import text, update
from sqlalchemy.orm import Session

from data.client import DataClient
from db.models import Ticker
from db.session import SessionLocal
from jobs.control import JobCancelled, JobControl, report_job_progress
from jobs.sync_bars import DEFAULT_MULTIPLIER, DEFAULT_TIMESPAN, fetch_and_store_bars

logger = logging.getLogger("backend_v2.jobs.sync_ohlc_bars")

# 2 years, per this job's default start_date ("2 years ago from today").
DEFAULT_LOOKBACK_DAYS = 730
DEFAULT_LIMIT = 8000
MAX_LIMIT = 10_000
# Same env var/default as jobs/sync_bars.py's own DEFAULT_MAX_WORKERS - kept as its own
# module-level constant (not imported) so this job's worker count can be tuned
# independently of sync_bars_nightly's, same reasoning as sync_snapshots.py/
# sync_indicators.py/sync_ticker_details.py each defining their own copy.
DEFAULT_MAX_WORKERS = int(os.environ.get("MASSIVE_MAX_WORKERS", "8"))

# temp_queries/sync_ohlc_bars.sql, translated to a bound-parameter statement: `params`
# becomes a one-row CTE fed by :start_date/:end_date instead of a real table, and the
# literal LIMIT 1000 becomes :limit. Otherwise unchanged - same CASE for new_start_date,
# same WHERE/ORDER BY.
_SELECT_TICKERS_SQL = text(
    """
    WITH params AS (
        SELECT :start_date AS start_date, :end_date AS end_date
    )
    SELECT
        a.ticker,
        CASE
            WHEN a.last_ohlc_sync_date IS NULL THEN p.start_date
            WHEN date(a.last_ohlc_sync_date, '+1 day') > p.start_date THEN date(a.last_ohlc_sync_date, '+1 day')
            ELSE p.start_date
        END AS new_start_date,
        p.end_date AS new_end_date
    FROM tickers AS a
    JOIN ticker_types AS b
        ON a.type = b.code
    CROSS JOIN params AS p
    WHERE
        a.last_ohlc_sync_date IS NULL
        OR a.last_ohlc_sync_date < p.end_date
    ORDER BY b.rank, a.ticker
    LIMIT :limit
    """
)


def _select_tickers_to_sync(
    session: Session, start_date: dt.date, end_date: dt.date, limit: int
) -> dict[str, dt.date]:
    rows = session.execute(
        _SELECT_TICKERS_SQL,
        {"start_date": start_date.isoformat(), "end_date": end_date.isoformat(), "limit": limit},
    ).all()
    return {ticker: dt.date.fromisoformat(new_start_date) for ticker, new_start_date, _new_end_date in rows}


def sync_ohlc_bars_worker(
    ticker: str,
    start_date: dt.date,
    end_date: dt.date,
    client_factory: Callable[[], DataClient] = DataClient,
    control: JobControl | None = None,
) -> int:
    """Runs in a worker thread - same shape as jobs/sync_bars.py's
    _fetch_and_store_one: `client_factory` builds a fresh DataClient bound to the event
    loop asyncio.run() spins up here, and this opens its own DB session, committed
    independently of every other ticker's fetch.

    Reuses fetch_and_store_bars (jobs/sync_bars.py) for the actual massive.com fetch
    and ohlc_bars upsert - same endpoint, same upsert semantics, no reason to duplicate
    it here. Once that fetch succeeds (regardless of how many bars came back - even
    zero, e.g. a range with no trading days), stamps tickers.last_ohlc_sync_date with
    end_date, in the same transaction as the fetch's own commit, so a ticker whose
    fetch raises never has its sync date advanced."""
    if control is not None:
        control.checkpoint_sync()

    async def _fetch(session: Session) -> int:
        async with client_factory() as client:
            return await fetch_and_store_bars(
                client, session, ticker, start_date, end_date, multiplier=DEFAULT_MULTIPLIER, timespan=DEFAULT_TIMESPAN
            )

    session = SessionLocal()
    try:
        fetched = asyncio.run(_fetch(session))
        session.execute(update(Ticker).where(Ticker.ticker == ticker).values(last_ohlc_sync_date=end_date))
        session.commit()
        return fetched
    finally:
        session.close()


def _run_pool(
    jobs: dict[str, dt.date],
    end_date: dt.date,
    max_workers: int,
    client_factory: Callable[[], DataClient],
    control: JobControl | None,
    session: Session,
    run_id: int | None,
) -> dict[str, int]:
    """Shared executor-driving loop - see jobs/sync_bars.py's _run_pool, which this
    mirrors including its JobCancelled handling: once any worker raises it, the rest of
    the (already-submitted) queue is cancelled outright rather than left to run down one
    near-instant checkpoint-raise at a time."""
    results: dict[str, int] = {}
    total = len(jobs)
    completed = 0
    report_job_progress(session, run_id, completed, total)
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
    try:
        futures = {
            executor.submit(sync_ohlc_bars_worker, ticker, start_date, end_date, client_factory, control): ticker
            for ticker, start_date in jobs.items()
        }
        for future in concurrent.futures.as_completed(futures):
            ticker = futures[future]
            try:
                results[ticker] = future.result()
            except JobCancelled:
                executor.shutdown(wait=False, cancel_futures=True)
                raise
            except Exception:
                logger.exception("sync_ohlc_bars failed for %s", ticker)
            completed += 1
            report_job_progress(session, run_id, completed, total)
    finally:
        executor.shutdown(wait=True)
    return results


def sync_ohlc_bars(
    session: Session,
    start_date: dt.date | None = None,
    end_date: dt.date | None = None,
    limit: int | None = None,
    max_workers: int = DEFAULT_MAX_WORKERS,
    client_factory: Callable[[], DataClient] = DataClient,
    control: JobControl | None = None,
    run_id: int | None = None,
) -> dict[str, int]:
    """Selects up to `limit` tickers (default DEFAULT_LIMIT, 8000; capped at MAX_LIMIT,
    10000) due for an OHLC bars sync - via _select_tickers_to_sync - and fans their
    fetches out across a thread pool of `max_workers` workers, same as
    jobs/sync_bars.py's sync_bars_manual/sync_bars_nightly.

    end_date defaults to today (UTC) and may not be given as a date after today.
    start_date defaults to DEFAULT_LOOKBACK_DAYS (730, i.e. 2 years) before today - not
    before end_date, matching this job's own default_run_type/description: "2 years ago
    from today", not "2 years before end_date"."""
    today = dt.datetime.now(dt.timezone.utc).date()
    end_date = end_date if end_date is not None else today
    if end_date > today:
        raise ValueError("end_date cannot be greater than today")
    start_date = start_date if start_date is not None else today - dt.timedelta(days=DEFAULT_LOOKBACK_DAYS)
    limit = min(limit, MAX_LIMIT) if limit is not None else DEFAULT_LIMIT

    jobs = _select_tickers_to_sync(session, start_date, end_date, limit)
    return _run_pool(jobs, end_date, max_workers, client_factory, control, session, run_id)
