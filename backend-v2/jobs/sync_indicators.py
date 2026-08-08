"""Syncs GET /v1/indicators/{sma,ema,macd,rsi}/{ticker} into the technical_indicators
table, one indicator at a time (see jobs/registry.py's INDICATOR_NAMES for the
job-name -> indicator-name mapping).

Same shape as jobs/sync_snapshots.py: massive.com has no bulk endpoint here either -
each ticker is its own request - so a run fans out across a ThreadPoolExecutor, one
worker per ticker, each opening its own DataClient (bound to its own asyncio event loop
via asyncio.run) and its own DB session, so no state is shared across threads. One
ticker's failure is isolated and logged rather than aborting the rest.

SMA/EMA/RSI each return a plain {timestamp, value} series; MACD additionally carries
signal/histogram - see db/models.py's TechnicalIndicator for why one table covers all
four rather than a MACD-specific one.
"""

import asyncio
import concurrent.futures
import datetime as dt
import logging
from typing import Any, Callable
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from data.client import DataClient
from db.models import TechnicalIndicator, Ticker
from db.session import SessionLocal, init_db
from jobs.control import JobCancelled, JobControl

logger = logging.getLogger("backend_v2.jobs.sync_indicators")

INDICATOR_PATH_TEMPLATE = "/v1/indicators/{indicator}/{ticker}"
DEFAULT_MAX_WORKERS = 8


def _indicator_path(indicator: str, ticker: str) -> str:
    return INDICATOR_PATH_TEMPLATE.format(indicator=indicator, ticker=quote(ticker, safe=""))


def _to_datetime(value: int | float | None) -> dt.datetime | None:
    """massive.com reports each value's `timestamp` as epoch milliseconds, same unit as
    jobs/sync_snapshots.py's `min.t`."""
    if value is None:
        return None
    return dt.datetime.fromtimestamp(value / 1_000, tz=dt.timezone.utc).replace(tzinfo=None)


def _upsert_value(session: Session, ticker: str, indicator: str, entry: dict[str, Any]) -> bool:
    timestamp = _to_datetime(entry.get("timestamp"))
    if timestamp is None:
        return False
    values: dict[str, Any] = {
        "ticker": ticker,
        "indicator": indicator,
        "timestamp": timestamp,
        "value": entry.get("value"),
        "signal": entry.get("signal"),
        "histogram": entry.get("histogram"),
        "fetched_at": dt.datetime.now(dt.timezone.utc).replace(tzinfo=None),
    }
    stmt = sqlite_insert(TechnicalIndicator).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[TechnicalIndicator.ticker, TechnicalIndicator.indicator, TechnicalIndicator.timestamp],
        set_=values,
    )
    session.execute(stmt)
    return True


def _resolve_tickers(session: Session, ticker_types: list[str] | None, tickers: list[str] | None) -> list[str]:
    """Same shape as sync_snapshots.py's/sync_bars.py's _resolve_tickers: explicit
    tickers win outright, otherwise ticker_types filters the tickers table, otherwise
    every known ticker is selected."""
    if tickers and ticker_types:
        raise ValueError("specify tickers or ticker_types, not both")
    if tickers:
        return list(tickers)
    query = select(Ticker.ticker)
    if ticker_types:
        query = query.where(Ticker.type.in_(ticker_types))
    return list(session.scalars(query))


def _fetch_and_store_one(
    indicator: str,
    ticker: str,
    client_factory: Callable[[], DataClient],
    control: JobControl | None = None,
) -> int:
    """Runs in a worker thread - see jobs/sync_snapshots.py's _fetch_and_store_one,
    which this mirrors: `control`, if given, is checked before doing anything else (safe
    to block here since this runs off the event loop), and commits its own session
    independently of every other ticker's fetch."""
    if control is not None:
        control.checkpoint_sync()

    async def _fetch() -> Any:
        async with client_factory() as client:
            return await client.get(_indicator_path(indicator, ticker))

    payload = asyncio.run(_fetch())
    entries = ((payload.get("results") or {}).get("values")) or []

    session = SessionLocal()
    try:
        stored = 0
        for entry in entries:
            if _upsert_value(session, ticker, indicator, entry):
                stored += 1
        session.commit()
        return stored
    finally:
        session.close()


def sync_indicator(
    indicator: str,
    session: Session,
    ticker_types: list[str] | None = None,
    tickers: list[str] | None = None,
    max_workers: int = DEFAULT_MAX_WORKERS,
    client_factory: Callable[[], DataClient] = DataClient,
    control: JobControl | None = None,
) -> int:
    """Fetches and upserts every value point for every selected ticker, in parallel
    across a thread pool of `max_workers` workers - see jobs/sync_snapshots.py's
    sync_snapshots, which this mirrors including its JobCancelled handling: once any
    worker raises it, the rest of the (already-submitted) queue is cancelled outright
    rather than left to run down one near-instant checkpoint-raise at a time, and it's
    re-raised here so the caller (app/main.py's _run_job) marks the run cancelled
    instead of completed."""
    selected = _resolve_tickers(session, ticker_types, tickers)
    fetched = 0
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
    try:
        futures = {
            executor.submit(_fetch_and_store_one, indicator, ticker, client_factory, control): ticker
            for ticker in selected
        }
        for future in concurrent.futures.as_completed(futures):
            ticker = futures[future]
            try:
                fetched += future.result()
            except JobCancelled:
                executor.shutdown(wait=False, cancel_futures=True)
                raise
            except Exception:
                logger.exception("%s sync failed for %s", indicator, ticker)
    finally:
        executor.shutdown(wait=True)

    logger.info("synced %d %s value(s) across %d ticker(s)", fetched, indicator, len(selected))
    return fetched


async def run_once(
    indicator: str,
    ticker_types: list[str] | None = None,
    tickers: list[str] | None = None,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> int:
    init_db()
    session = SessionLocal()
    try:
        return await asyncio.to_thread(sync_indicator, indicator, session, ticker_types, tickers, max_workers)
    finally:
        session.close()
