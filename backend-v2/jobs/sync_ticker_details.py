"""Syncs GET /v3/reference/tickers/{ticker} into the ticker_details table.

The *singular* ticker-details endpoint, distinct from the paged list endpoint
jobs/sync_tickers.py already syncs - this is the only endpoint that carries
market_cap/shares-outstanding, at the cost of one request per ticker rather than one
request per thousand-ticker page. Same shape as jobs/sync_snapshots.py: no bulk
endpoint to page through, so a run fans out across a ThreadPoolExecutor instead of the
sequential loop sync_bars.py uses. Each worker thread opens its own DataClient (bound to
its own asyncio event loop, via asyncio.run) and its own DB session, so no state - HTTP
connection or otherwise - is shared across threads; one ticker's failure is isolated and
logged rather than aborting the rest.
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
from db.models import Ticker, TickerDetail
from db.session import SessionLocal, init_db
from jobs.control import JobCancelled, JobControl

logger = logging.getLogger("backend_v2.jobs.sync_ticker_details")

JOB_NAME = "ticker_details"
TICKER_DETAILS_PATH_TEMPLATE = "/v3/reference/tickers/{ticker}"
DEFAULT_MAX_WORKERS = 8


def _ticker_details_path(ticker: str) -> str:
    return TICKER_DETAILS_PATH_TEMPLATE.format(ticker=quote(ticker, safe=""))


def _parse_list_date(value: Any) -> dt.date | None:
    """massive.com reports list_date as an ISO "YYYY-MM-DD" string, same shape as
    every other date-only field this codebase parses (e.g. JobConfig's backtest
    dates)."""
    if not value:
        return None
    return dt.date.fromisoformat(value)


def _upsert_ticker_detail(session: Session, ticker: str, result: dict[str, Any]) -> None:
    values: dict[str, Any] = {
        "ticker": ticker,
        "market_cap": result.get("market_cap"),
        "share_class_shares_outstanding": result.get("share_class_shares_outstanding"),
        "weighted_shares_outstanding": result.get("weighted_shares_outstanding"),
        "sic_code": result.get("sic_code"),
        "sic_description": result.get("sic_description"),
        "homepage_url": result.get("homepage_url"),
        "total_employees": result.get("total_employees"),
        "list_date": _parse_list_date(result.get("list_date")),
        "round_lot": result.get("round_lot"),
        "fetched_at": dt.datetime.now(dt.timezone.utc).replace(tzinfo=None),
    }
    stmt = sqlite_insert(TickerDetail).values(**values)
    stmt = stmt.on_conflict_do_update(index_elements=[TickerDetail.ticker], set_=values)
    session.execute(stmt)


def _resolve_tickers(session: Session, ticker_types: list[str] | None, tickers: list[str] | None) -> list[str]:
    """Same shape as sync_snapshots.py's _resolve_tickers: explicit tickers win
    outright, otherwise ticker_types filters the tickers table, otherwise every known
    ticker is selected."""
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
    """Runs in a worker thread - see jobs/sync_snapshots.py's _fetch_and_store_one for
    the full reasoning on client_factory/control, identical here."""
    if control is not None:
        control.checkpoint_sync()

    async def _fetch() -> Any:
        async with client_factory() as client:
            return await client.get(_ticker_details_path(ticker))

    payload = asyncio.run(_fetch())
    result = payload.get("results")
    if result is None:
        raise ValueError(f"no ticker details returned for {ticker!r}")

    session = SessionLocal()
    try:
        _upsert_ticker_detail(session, ticker, result)
        session.commit()
    finally:
        session.close()


def sync_ticker_details(
    session: Session,
    ticker_types: list[str] | None = None,
    tickers: list[str] | None = None,
    max_workers: int = DEFAULT_MAX_WORKERS,
    client_factory: Callable[[], DataClient] = DataClient,
    control: JobControl | None = None,
) -> int:
    """Fetches and upserts a ticker_details row for every selected ticker, in parallel
    across a thread pool of `max_workers` workers - see jobs/sync_snapshots.py's
    sync_snapshots for the full reasoning on `session`/`control`/cancellation, identical
    here."""
    selected = _resolve_tickers(session, ticker_types, tickers)
    fetched = 0
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
                logger.exception("ticker details sync failed for %s", ticker)
    finally:
        executor.shutdown(wait=True)

    logger.info("synced %d/%d ticker detail(s)", fetched, len(selected))
    return fetched


async def run_once(
    ticker_types: list[str] | None = None,
    tickers: list[str] | None = None,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> int:
    init_db()
    session = SessionLocal()
    try:
        return await asyncio.to_thread(sync_ticker_details, session, ticker_types, tickers, max_workers)
    finally:
        session.close()
