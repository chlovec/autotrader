"""Syncs GET /v2/snapshot/locale/us/markets/stocks/{direction} ("gainers"/"losers")
into the top_market_movers table - one row per (ticker, direction), both directions
sharing the same table (see db/models.py's TopMarketMover) rather than one table each.

Unlike sync_snapshots (one ticker per request, fanned out across a thread pool), each
direction here is a single request that already returns the whole ranked list, so this
runs sequentially on the event loop, the same shape as sync_ticker_types. And because a
top-N list's *membership* changes from run to run (a ticker can drop off the list
entirely), each run replaces that direction's rows outright rather than upserting on
top of whatever was there before - upsert-only would let a ticker that's no longer a
mover linger in the table forever.
"""

import datetime as dt
import logging
from typing import Any

from sqlalchemy import delete
from sqlalchemy.orm import Session

from data.client import DataClient
from db.models import TopMarketMover
from db.session import SessionLocal, init_db
from jobs.control import JobCancelled, JobControl

logger = logging.getLogger("backend_v2.jobs.sync_top_movers")

JOB_NAME = "top_movers"
DIRECTIONS = ["gainers", "losers"]
MOVERS_PATH_TEMPLATE = "/v2/snapshot/locale/us/markets/stocks/{direction}"


def _movers_path(direction: str) -> str:
    return MOVERS_PATH_TEMPLATE.format(direction=direction)


def _to_datetime(value: int | float | None, divisor: float) -> dt.datetime | None:
    """massive.com reports `updated` as epoch nanoseconds and `min.t` as epoch
    milliseconds - divisor converts either to epoch seconds before conversion. Same
    helper as jobs/sync_snapshots.py's _to_datetime."""
    if value is None:
        return None
    return dt.datetime.fromtimestamp(value / divisor, tz=dt.timezone.utc).replace(tzinfo=None)


def _values_for(direction: str, rank: int, ticker: str, result: dict[str, Any]) -> dict[str, Any]:
    day = result.get("day") or {}
    prev_day = result.get("prevDay") or {}
    minute = result.get("min") or {}
    return {
        "ticker": ticker,
        "direction": direction,
        "rank": rank,
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


async def _sync_direction(client: DataClient, session: Session, direction: str) -> int:
    """Fetches one direction's whole list and replaces its rows in one go: delete
    everything currently stored for `direction`, then insert the freshly-ranked list -
    see this module's docstring for why a plain upsert isn't enough here."""
    payload = await client.get(_movers_path(direction))
    entries = payload.get("tickers") or []

    session.execute(delete(TopMarketMover).where(TopMarketMover.direction == direction))
    for rank, entry in enumerate(entries, start=1):
        ticker = entry.get("ticker")
        if not ticker:
            continue
        session.add(TopMarketMover(**_values_for(direction, rank, ticker, entry)))
    session.commit()
    return len(entries)


async def sync_top_movers(client: DataClient, session: Session, control: JobControl | None = None) -> dict[str, int]:
    """Fetches and replaces both directions' rows. `control`, if given, is checked
    before each direction (see jobs/control.py) - a pause blocks right here until
    resumed, and a cancel raises JobCancelled, left uncaught so it unwinds the whole
    run instead of being treated as a per-direction failure."""
    results: dict[str, int] = {}
    for direction in DIRECTIONS:
        if control is not None:
            await control.checkpoint_async()
        try:
            results[direction] = await _sync_direction(client, session, direction)
        except JobCancelled:
            raise
        except Exception:
            logger.exception("top movers sync failed for direction %s", direction)

    logger.info("synced %d gainer(s), %d loser(s)", results.get("gainers", 0), results.get("losers", 0))
    return results


async def run_once() -> dict[str, int]:
    init_db()
    async with DataClient() as client:
        session = SessionLocal()
        try:
            return await sync_top_movers(client, session)
        finally:
            session.close()
