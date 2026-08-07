"""Syncs GET /v3/reference/tickers/types into the ticker_types table.

Unlike sync_tickers, this endpoint doesn't support updated_since/pagination - it's a
short, mostly-static reference list of every code/asset_class/locale combination
massive.com's tickers can carry, so every run just re-fetches and upserts the whole
list.
"""

import logging
from typing import Any

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from data.client import DataClient
from db.models import TickerType
from db.session import SessionLocal, init_db
from jobs.control import JobControl

logger = logging.getLogger("backend_v2.jobs.sync_ticker_types")

JOB_NAME = "ticker_types"
TICKER_TYPES_PATH = "/v3/reference/tickers/types"

_TICKER_TYPE_FIELDS = ("code", "description", "asset_class", "locale")


def _upsert_ticker_type(session: Session, result: dict[str, Any]) -> None:
    values = {field: result.get(field) for field in _TICKER_TYPE_FIELDS}
    stmt = sqlite_insert(TickerType).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[TickerType.code, TickerType.asset_class, TickerType.locale], set_=values
    )
    session.execute(stmt)


async def sync_ticker_types(client: DataClient, session: Session, control: JobControl | None = None) -> int:
    """Fetches GET /v3/reference/tickers/types and upserts every result into the
    ticker_types table.

    `control`, if given, is checked once up front (see jobs/control.py) - there's only
    the one upstream call here, so that's the only point a pause or cancel requested
    before this run even started actually has anywhere to take effect."""
    if control is not None:
        await control.checkpoint_async()
    payload = await client.get(TICKER_TYPES_PATH)
    results = payload.get("results", [])
    for result in results:
        _upsert_ticker_type(session, result)

    session.commit()
    logger.info("synced %d ticker type(s)", len(results))
    return len(results)


async def run_once() -> int:
    init_db()
    async with DataClient() as client:
        session = SessionLocal()
        try:
            return await sync_ticker_types(client, session)
        finally:
            session.close()
