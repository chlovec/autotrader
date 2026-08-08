"""Syncs GET /v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{from}/{to}
("OHLC Custom Bars") into the ohlc_bars table.

Two ways to trigger a sync, both built on fetch_and_store_bars:
- sync_bars_manual (run_bars_manual.py): caller gives an explicit start/end date range,
  applied to every selected ticker regardless of what's already synced.
- sync_bars_nightly (run_jobs.py): no date range given - each ticker's start date is
  max(its own most recent ohlc_bars date + 1, backfill_days back from today), through
  yesterday. Reading the actual last bar straight from ohlc_bars (rather than a
  separately-tracked cursor - see _last_bar_dates) means a ticker massive.com returns
  nothing for is never silently marked "caught up"; it's simply retried again next run,
  since its last real bar date hasn't moved. Tickers already synced through yesterday
  are skipped entirely.

Both accept an optional ticker_types or tickers filter (mutually exclusive); if neither
is given, every ticker in the tickers table is selected.

Same shape as jobs/sync_snapshots.py/jobs/sync_indicators.py: a run selecting many
tickers fans out across a ThreadPoolExecutor rather than fetching one ticker at a time,
so a large selection still finishes in reasonable time. Each worker thread opens its own
DataClient (bound to its own asyncio event loop, via asyncio.run) and its own DB
session, so no state - HTTP connection or otherwise - is shared across threads; one
ticker's failure is isolated and logged rather than aborting the rest.
"""

import asyncio
import concurrent.futures
import datetime as dt
import logging
import os
from typing import Any, Callable
from urllib.parse import quote

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from data.client import DataClient
from db.models import OhlcBar, Ticker
from db.session import SessionLocal, init_db
from jobs.control import JobCancelled, JobControl

logger = logging.getLogger("backend_v2.jobs.sync_bars")

DEFAULT_MULTIPLIER = 1
DEFAULT_TIMESPAN = "day"
DEFAULT_BACKFILL_DAYS = int(os.environ.get("BARS_DEFAULT_BACKFILL_DAYS", "730"))
DEFAULT_MAX_WORKERS = 8
PAGE_LIMIT = 50_000

# Polygon-style aggs result field -> OhlcBar column.
_BAR_FIELD_MAP = {
    "o": "open",
    "h": "high",
    "l": "low",
    "c": "close",
    "v": "volume",
    "vw": "vwap",
    "n": "transactions",
}


def _bars_path(ticker: str, multiplier: int, timespan: str, start_date: dt.date, end_date: dt.date) -> str:
    return (
        f"/v2/aggs/ticker/{quote(ticker, safe='')}/range/{multiplier}/{timespan}/"
        f"{start_date.isoformat()}/{end_date.isoformat()}"
    )


def _upsert_bar(session: Session, ticker: str, multiplier: int, timespan: str, result: dict[str, Any]) -> None:
    values: dict[str, Any] = {
        "ticker": ticker,
        "multiplier": multiplier,
        "timespan": timespan,
        "timestamp": dt.datetime.fromtimestamp(result["t"] / 1000, tz=dt.timezone.utc).replace(tzinfo=None),
    }
    for source, field in _BAR_FIELD_MAP.items():
        values[field] = result.get(source)
    stmt = sqlite_insert(OhlcBar).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[OhlcBar.ticker, OhlcBar.multiplier, OhlcBar.timespan, OhlcBar.timestamp],
        set_=values,
    )
    session.execute(stmt)


async def fetch_and_store_bars(
    client: DataClient,
    session: Session,
    ticker: str,
    start_date: dt.date,
    end_date: dt.date,
    multiplier: int = DEFAULT_MULTIPLIER,
    timespan: str = DEFAULT_TIMESPAN,
) -> int:
    """Fetches every page of bars for one ticker over [start_date, end_date] and upserts
    them into ohlc_bars. Nothing else is tracked - a caller that wants to know how
    up-to-date a ticker is can just look at ohlc_bars.MAX(timestamp) itself (see
    _last_bar_dates), rather than a separately-maintained cursor that could say one
    thing while the data says another."""
    params: dict[str, Any] = {"adjusted": "true", "sort": "asc", "limit": PAGE_LIMIT}
    path = _bars_path(ticker, multiplier, timespan, start_date, end_date)

    next_url: str | None = None
    fetched = 0
    while True:
        payload = await (client.get(next_url) if next_url else client.get(path, params=params))
        results = payload.get("results") or []
        for result in results:
            _upsert_bar(session, ticker, multiplier, timespan, result)
        fetched += len(results)

        next_url = payload.get("next_url")
        if not next_url:
            break

    session.commit()
    return fetched


def _last_bar_dates(session: Session, multiplier: int, timespan: str) -> dict[str, dt.date]:
    """Every ticker's most recent (multiplier, timespan) bar date, in one grouped query
    rather than one per ticker - same reasoning as jobs/average_volume.py's
    _apply_ticker_filter docstring: sync_bars_nightly's selection can be tens of
    thousands of tickers, too many to bind as a literal IN(...) even if this were
    scoped to just the selected ones. ohlc_bars' primary key leads with
    (ticker, multiplier, timespan, timestamp), so grouping by ticker after filtering on
    multiplier/timespan is a single ordered index scan, not a table scan + sort."""
    query = (
        select(OhlcBar.ticker, func.max(OhlcBar.timestamp))
        .where(OhlcBar.multiplier == multiplier, OhlcBar.timespan == timespan)
        .group_by(OhlcBar.ticker)
    )
    return {ticker: timestamp.date() for ticker, timestamp in session.execute(query).all()}


def _resolve_tickers(session: Session, ticker_types: list[str] | None, tickers: list[str] | None) -> list[str]:
    if tickers and ticker_types:
        raise ValueError("specify tickers or ticker_types, not both")
    if tickers:
        return list(tickers)
    query = select(Ticker.ticker)
    if ticker_types:
        query = query.where(Ticker.type.in_(ticker_types))
    return list(session.scalars(query))


def _fetch_and_store_one(
    ticker: str,
    start_date: dt.date,
    end_date: dt.date,
    multiplier: int,
    timespan: str,
    client_factory: Callable[[], DataClient],
    control: JobControl | None = None,
) -> int:
    """Runs in a worker thread - mirrors jobs/sync_snapshots.py's _fetch_and_store_one:
    `client_factory` builds a fresh DataClient bound to the event loop asyncio.run()
    spins up here, so it can't be shared with another thread's loop, and this opens its
    own DB session, committed independently of every other ticker's fetch.

    `control`, if given, is checked before doing anything else (safe to block here since
    this runs off the event loop) - a pause blocks this worker right here until resumed;
    a cancel raises JobCancelled, which the caller below treats as "stop the whole run"
    rather than "this ticker failed" the way every other exception here is treated.
    """
    if control is not None:
        control.checkpoint_sync()

    async def _fetch(session: Session) -> int:
        async with client_factory() as client:
            return await fetch_and_store_bars(
                client, session, ticker, start_date, end_date, multiplier=multiplier, timespan=timespan
            )

    session = SessionLocal()
    try:
        return asyncio.run(_fetch(session))
    finally:
        session.close()


def _run_pool(
    jobs: dict[str, dt.date],
    end_date: dt.date,
    multiplier: int,
    timespan: str,
    max_workers: int,
    client_factory: Callable[[], DataClient],
    control: JobControl | None,
    log_label: str,
) -> dict[str, int]:
    """Shared executor-driving loop for sync_bars_manual/sync_bars_nightly below - see
    jobs/sync_snapshots.py's sync_snapshots, which this mirrors including its
    JobCancelled handling: once any worker raises it, the rest of the
    (already-submitted) queue is cancelled outright rather than left to run down one
    near-instant checkpoint-raise at a time, and it's re-raised here so the caller
    (app/main.py's _run_job) marks the run cancelled instead of completed. `jobs` maps
    each selected ticker to its own start_date."""
    results: dict[str, int] = {}
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
    try:
        futures = {
            executor.submit(
                _fetch_and_store_one, ticker, start_date, end_date, multiplier, timespan, client_factory, control
            ): ticker
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
                logger.exception("%s bars sync failed for %s", log_label, ticker)
    finally:
        executor.shutdown(wait=True)
    return results


def sync_bars_manual(
    session: Session,
    start_date: dt.date,
    end_date: dt.date,
    ticker_types: list[str] | None = None,
    tickers: list[str] | None = None,
    multiplier: int = DEFAULT_MULTIPLIER,
    timespan: str = DEFAULT_TIMESPAN,
    max_workers: int = DEFAULT_MAX_WORKERS,
    client_factory: Callable[[], DataClient] = DataClient,
    control: JobControl | None = None,
) -> dict[str, int]:
    """Fetches [start_date, end_date] for every selected ticker, regardless of what's
    already synced, in parallel across a thread pool of `max_workers` workers - one
    ticker's failure doesn't stop the rest - see engine/multi_runner.py at the repo root
    for the same per-item isolation pattern. This is a blocking call; callers on an
    event loop should run it via asyncio.to_thread (see app/main.py's _run_job)."""
    selected = _resolve_tickers(session, ticker_types, tickers)
    jobs = {ticker: start_date for ticker in selected}
    return _run_pool(jobs, end_date, multiplier, timespan, max_workers, client_factory, control, "manual")


def sync_bars_nightly(
    session: Session,
    ticker_types: list[str] | None = None,
    tickers: list[str] | None = None,
    multiplier: int = DEFAULT_MULTIPLIER,
    timespan: str = DEFAULT_TIMESPAN,
    backfill_days: int = DEFAULT_BACKFILL_DAYS,
    max_workers: int = DEFAULT_MAX_WORKERS,
    client_factory: Callable[[], DataClient] = DataClient,
    control: JobControl | None = None,
) -> dict[str, int]:
    """Syncs every selected ticker through yesterday, skipping any already synced
    through yesterday, in parallel across a thread pool of `max_workers` workers.

    Each ticker's start date is max(its own most recent ohlc_bars date + 1,
    backfill_floor), where backfill_floor = end_date - backfill_days - the same floor a
    ticker with no bars at all backfills from, so "never synced" and "last bar predates
    the backfill window" both naturally resolve to the same starting point. Reading the
    actual last bar from ohlc_bars (see _last_bar_dates) rather than a
    separately-tracked cursor means a ticker massive.com returns nothing for is never
    silently marked "caught up" - its last real bar date hasn't moved, so it's simply
    selected again next run.

    Each selected ticker's start date is resolved up front (one grouped query over
    ohlc_bars, not a per-ticker lookup, not worth a checkpoint of its own) before
    fanning out - see _fetch_and_store_one for where `control` is actually checked,
    once per ticker, right before its (slow) HTTP fetch."""
    end_date = dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)
    backfill_floor = end_date - dt.timedelta(days=backfill_days)
    selected = _resolve_tickers(session, ticker_types, tickers)
    last_bar_by_ticker = _last_bar_dates(session, multiplier, timespan)

    jobs: dict[str, dt.date] = {}
    for ticker in selected:
        last_bar_date = last_bar_by_ticker.get(ticker)
        start_date = max(last_bar_date + dt.timedelta(days=1), backfill_floor) if last_bar_date else backfill_floor
        if start_date > end_date:
            continue  # already up to date
        jobs[ticker] = start_date

    return _run_pool(jobs, end_date, multiplier, timespan, max_workers, client_factory, control, "nightly")


def _env_list(var_name: str) -> list[str] | None:
    raw = os.environ.get(var_name, "").strip()
    return [item.strip() for item in raw.split(",") if item.strip()] or None


async def run_manual(
    start_date: dt.date,
    end_date: dt.date,
    ticker_types: list[str] | None = None,
    tickers: list[str] | None = None,
    multiplier: int = DEFAULT_MULTIPLIER,
    timespan: str = DEFAULT_TIMESPAN,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> dict[str, int]:
    init_db()
    session = SessionLocal()
    try:
        return await asyncio.to_thread(
            sync_bars_manual,
            session,
            start_date,
            end_date,
            ticker_types,
            tickers,
            multiplier,
            timespan,
            max_workers,
        )
    finally:
        session.close()


async def run_nightly() -> dict[str, int]:
    """Reads BARS_NIGHTLY_TICKER_TYPES / BARS_NIGHTLY_TICKERS / BARS_MULTIPLIER /
    BARS_TIMESPAN from the environment - this is what run_jobs.py schedules."""
    init_db()
    ticker_types = _env_list("BARS_NIGHTLY_TICKER_TYPES")
    tickers = _env_list("BARS_NIGHTLY_TICKERS")
    multiplier = int(os.environ.get("BARS_MULTIPLIER", str(DEFAULT_MULTIPLIER)))
    timespan = os.environ.get("BARS_TIMESPAN", DEFAULT_TIMESPAN)

    session = SessionLocal()
    try:
        return await asyncio.to_thread(
            sync_bars_nightly, session, ticker_types, tickers, multiplier=multiplier, timespan=timespan
        )
    finally:
        session.close()
