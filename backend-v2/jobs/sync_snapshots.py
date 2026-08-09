"""Syncs GET /v2/snapshot/locale/us/markets/stocks/tickers/{ticker} into the
current_snapshots table.

Unlike sync_tickers/sync_bars there's no paged bulk endpoint here - each ticker is its
own request. A run selecting many tickers/ticker_types therefore fans out across a
ThreadPoolExecutor instead of the sequential loop sync_bars.py uses, so a large
selection still finishes in reasonable time. Each worker thread opens its own DataClient
(bound to its own asyncio event loop, via asyncio.run) and its own DB session, so no
state - HTTP connection or otherwise - is shared across threads; one ticker's failure is
isolated and logged rather than aborting the rest, same as sync_bars.py's per-ticker
try/except.
"""

import asyncio
import concurrent.futures
import datetime as dt
import logging
import os
from typing import Any, Callable
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from data.client import DataClient
from db.models import CurrentSnapshot, Ticker
from db.session import SessionLocal, init_db
from jobs.control import JobCancelled, JobControl, report_job_progress

logger = logging.getLogger("backend_v2.jobs.sync_snapshots")

JOB_NAME = "snapshots"
SNAPSHOT_PATH_TEMPLATE = "/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}"
# Overridable via env since a lower worker count reduces the aggregate request rate
# hitting massive.com's rate limiter (see data/client.py's MIN_REQUEST_INTERVAL_SECONDS
# for the per-client-instance pacing knob, which this compounds with - N workers means
# up to N near-simultaneous first requests regardless of pacing).
DEFAULT_MAX_WORKERS = int(os.environ.get("MASSIVE_MAX_WORKERS", "8"))


def _snapshot_path(ticker: str) -> str:
    return SNAPSHOT_PATH_TEMPLATE.format(ticker=quote(ticker, safe=""))


def _to_datetime(value: int | float | None, divisor: float) -> dt.datetime | None:
    """massive.com reports `updated` as epoch nanoseconds and `min.t` as epoch
    milliseconds - divisor converts either to epoch seconds before conversion."""
    if value is None:
        return None
    return dt.datetime.fromtimestamp(value / divisor, tz=dt.timezone.utc).replace(tzinfo=None)


def _upsert_snapshot(session: Session, ticker: str, result: dict[str, Any]) -> None:
    day = result.get("day") or {}
    prev_day = result.get("prevDay") or {}
    minute = result.get("min") or {}
    values: dict[str, Any] = {
        "ticker": ticker,
        "todays_change": result.get("todaysChange"),
        "todays_change_perc": result.get("todaysChangePerc"),
        "updated": _to_datetime(result.get("updated"), 1_000_000_000),
        "day_open": day.get("o"),
        "day_high": day.get("h"),
        "day_low": day.get("l"),
        "day_close": day.get("c"),
        "day_volume": day.get("v"),
        "day_vwap": day.get("vw"),
        "min_open": minute.get("o"),
        "min_high": minute.get("h"),
        "min_low": minute.get("l"),
        "min_close": minute.get("c"),
        "min_volume": minute.get("v"),
        "min_vwap": minute.get("vw"),
        "min_accumulated_volume": minute.get("av"),
        "min_timestamp": _to_datetime(minute.get("t"), 1_000),
        "prev_day_open": prev_day.get("o"),
        "prev_day_high": prev_day.get("h"),
        "prev_day_low": prev_day.get("l"),
        "prev_day_close": prev_day.get("c"),
        "prev_day_volume": prev_day.get("v"),
        "prev_day_vwap": prev_day.get("vw"),
        "fetched_at": dt.datetime.now(dt.timezone.utc).replace(tzinfo=None),
    }
    stmt = sqlite_insert(CurrentSnapshot).values(**values)
    stmt = stmt.on_conflict_do_update(index_elements=[CurrentSnapshot.ticker], set_=values)
    session.execute(stmt)


def _resolve_tickers(session: Session, ticker_types: list[str] | None, tickers: list[str] | None) -> list[str]:
    """Same shape as sync_bars.py's _resolve_tickers: explicit tickers win outright,
    otherwise ticker_types filters the tickers table, otherwise every known ticker is
    selected."""
    if tickers and ticker_types:
        raise ValueError("specify tickers or ticker_types, not both")
    if tickers:
        return list(tickers)
    query = select(Ticker.ticker)
    if ticker_types:
        query = query.where(Ticker.type.in_(ticker_types))
    return list(session.scalars(query))


def _fetch_and_store_one(
    ticker: str, client_factory: Callable[[], DataClient], control: JobControl | None = None
) -> None:
    """Runs in a worker thread. `client_factory` builds a fresh DataClient - each one
    owns an httpx.AsyncClient bound to the event loop asyncio.run() spins up here, so it
    can't be shared with another thread's loop. Commits its own session independently of
    every other ticker's fetch.

    `control`, if given, is checked before doing anything else (see jobs/control.py,
    checkpoint_sync - safe to block here since this runs off the event loop) - a pause
    blocks this worker right here until resumed; a cancel raises JobCancelled, which
    sync_snapshots below treats as "stop the whole run" rather than "this ticker
    failed" the way every other exception here is treated.
    """
    if control is not None:
        control.checkpoint_sync()

    async def _fetch() -> Any:
        async with client_factory() as client:
            return await client.get(_snapshot_path(ticker))

    payload = asyncio.run(_fetch())
    result = payload.get("ticker")
    if result is None:
        raise ValueError(f"no snapshot data returned for {ticker!r}")

    session = SessionLocal()
    try:
        _upsert_snapshot(session, ticker, result)
        session.commit()
    finally:
        session.close()


def sync_snapshots(
    session: Session,
    ticker_types: list[str] | None = None,
    tickers: list[str] | None = None,
    max_workers: int = DEFAULT_MAX_WORKERS,
    client_factory: Callable[[], DataClient] = DataClient,
    control: JobControl | None = None,
    run_id: int | None = None,
) -> int:
    """Fetches and upserts a current-snapshot row for every selected ticker, in
    parallel across a thread pool of `max_workers` workers. `session` is used here to
    resolve the ticker selection (see _resolve_tickers) and to report_job_progress (see
    jobs/control.py) as futures complete, otherwise idle for the duration of this call -
    each worker fetches and commits through its own session, not this one. This is a
    blocking call; callers on an event loop should run it via asyncio.to_thread (see
    app/main.py's _run_job).

    `control`, if given, is handed to every worker (see _fetch_and_store_one) so each
    checks it before starting its ticker. Once any worker raises JobCancelled, the rest
    of the (already-submitted) queue is cancelled outright via cancel_futures rather
    than left to run down one near-instant checkpoint-raise at a time, and this
    re-raises JobCancelled itself so the caller (app/main.py's _run_job) marks the run
    cancelled instead of completed.
    """
    selected = _resolve_tickers(session, ticker_types, tickers)
    fetched = 0
    completed = 0
    total = len(selected)
    report_job_progress(session, run_id, completed, total)
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
    try:
        futures = {
            executor.submit(_fetch_and_store_one, ticker, client_factory, control): ticker for ticker in selected
        }
        for future in concurrent.futures.as_completed(futures):
            ticker = futures[future]
            try:
                future.result()
                fetched += 1
            except JobCancelled:
                executor.shutdown(wait=False, cancel_futures=True)
                raise
            except Exception:
                logger.exception("snapshot sync failed for %s", ticker)
            completed += 1
            report_job_progress(session, run_id, completed, total)
    finally:
        executor.shutdown(wait=True)

    logger.info("synced %d/%d snapshot(s)", fetched, len(selected))
    return fetched


async def run_once(
    ticker_types: list[str] | None = None,
    tickers: list[str] | None = None,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> int:
    init_db()
    session = SessionLocal()
    try:
        return await asyncio.to_thread(sync_snapshots, session, ticker_types, tickers, max_workers)
    finally:
        session.close()
