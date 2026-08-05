"""Syncs GET /v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{from}/{to}
("OHLC Custom Bars") into the ohlc_bars table.

Two ways to trigger a sync, both built on fetch_and_store_bars:
- sync_bars_manual (run_bars_manual.py): caller gives an explicit start/end date range,
  applied to every selected ticker regardless of what's already synced.
- sync_bars_nightly (run_jobs.py): no date range given - each ticker's range is derived
  from its own TickerBarSyncState cursor (never synced -> backfill_days back from
  today; otherwise the day after its last synced date), through yesterday. Tickers
  already synced through yesterday are skipped entirely.

Both accept an optional ticker_types or tickers filter (mutually exclusive); if neither
is given, every ticker in the tickers table is selected.
"""

import datetime as dt
import logging
import os
from typing import Any
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from data.client import DataClient
from db.models import OhlcBar, Ticker, TickerBarSyncState
from db.session import SessionLocal, init_db

logger = logging.getLogger("backend_v2.jobs.sync_bars")

DEFAULT_MULTIPLIER = 1
DEFAULT_TIMESPAN = "day"
DEFAULT_BACKFILL_DAYS = int(os.environ.get("BARS_DEFAULT_BACKFILL_DAYS", "730"))
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


def _advance_sync_state(session: Session, ticker: str, multiplier: int, timespan: str, end_date: dt.date) -> None:
    state = session.get(TickerBarSyncState, (ticker, multiplier, timespan))
    if state is None:
        session.add(
            TickerBarSyncState(ticker=ticker, multiplier=multiplier, timespan=timespan, synced_through=end_date)
        )
    elif end_date > state.synced_through:
        state.synced_through = end_date


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
    them, then advances that ticker's (multiplier, timespan) cursor to
    max(current cursor, end_date).

    Advancing only forward means a manual backfill of an older gap (end_date in the
    past relative to what's already synced) can never roll the cursor backwards and
    make the nightly job re-walk years of already-synced history.
    """
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

    _advance_sync_state(session, ticker, multiplier, timespan, end_date)
    session.commit()
    return fetched


def _resolve_tickers(session: Session, ticker_types: list[str] | None, tickers: list[str] | None) -> list[str]:
    if tickers and ticker_types:
        raise ValueError("specify tickers or ticker_types, not both")
    if tickers:
        return list(tickers)
    query = select(Ticker.ticker)
    if ticker_types:
        query = query.where(Ticker.type.in_(ticker_types))
    return list(session.scalars(query))


async def sync_bars_manual(
    client: DataClient,
    session: Session,
    start_date: dt.date,
    end_date: dt.date,
    ticker_types: list[str] | None = None,
    tickers: list[str] | None = None,
    multiplier: int = DEFAULT_MULTIPLIER,
    timespan: str = DEFAULT_TIMESPAN,
) -> dict[str, int]:
    """Fetches [start_date, end_date] for every selected ticker, regardless of what's
    already synced. One ticker's failure doesn't stop the rest - see
    engine/multi_runner.py at the repo root for the same per-item isolation pattern."""
    selected = _resolve_tickers(session, ticker_types, tickers)
    results: dict[str, int] = {}
    for ticker in selected:
        try:
            results[ticker] = await fetch_and_store_bars(
                client, session, ticker, start_date, end_date, multiplier=multiplier, timespan=timespan
            )
        except Exception:
            logger.exception("manual bars sync failed for %s", ticker)
    return results


async def sync_bars_nightly(
    client: DataClient,
    session: Session,
    ticker_types: list[str] | None = None,
    tickers: list[str] | None = None,
    multiplier: int = DEFAULT_MULTIPLIER,
    timespan: str = DEFAULT_TIMESPAN,
    backfill_days: int = DEFAULT_BACKFILL_DAYS,
) -> dict[str, int]:
    """Syncs every selected ticker through yesterday, skipping any already synced
    through yesterday. A ticker with no prior TickerBarSyncState row backfills the last
    backfill_days days; one that has a cursor resumes the day after it."""
    end_date = dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)
    selected = _resolve_tickers(session, ticker_types, tickers)

    results: dict[str, int] = {}
    for ticker in selected:
        state = session.get(TickerBarSyncState, (ticker, multiplier, timespan))
        if state is not None and state.synced_through >= end_date:
            continue  # already up to date

        start_date = (
            state.synced_through + dt.timedelta(days=1)
            if state is not None
            else end_date - dt.timedelta(days=backfill_days)
        )

        try:
            results[ticker] = await fetch_and_store_bars(
                client, session, ticker, start_date, end_date, multiplier=multiplier, timespan=timespan
            )
        except Exception:
            logger.exception("nightly bars sync failed for %s", ticker)
    return results


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
) -> dict[str, int]:
    init_db()
    async with DataClient() as client:
        session = SessionLocal()
        try:
            return await sync_bars_manual(
                client, session, start_date, end_date, ticker_types, tickers, multiplier, timespan
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

    async with DataClient() as client:
        session = SessionLocal()
        try:
            return await sync_bars_nightly(
                client, session, ticker_types, tickers, multiplier=multiplier, timespan=timespan
            )
        finally:
            session.close()
