"""Syncs GET /v3/reference/tickers into the tickers table.

The first run (no SyncState row for JOB_NAME yet) has nothing to compare against, so it
pages through every ticker massive.com has. Every run after that passes
updated_since=<the previous run's start time>, so only tickers changed since then come
back - the table converges to the upstream state incrementally instead of re-fetching
everything on every run.
"""

import datetime as dt
import logging
from typing import Any

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from data.client import DataClient
from db.models import SyncState, Ticker
from db.session import SessionLocal, init_db

logger = logging.getLogger("backend_v2.jobs.sync_tickers")

JOB_NAME = "tickers"
TICKERS_PATH = "/v3/reference/tickers"
PAGE_LIMIT = 1000

_TICKER_FIELDS = (
    "ticker",
    "name",
    "market",
    "locale",
    "primary_exchange",
    "type",
    "active",
    "currency_name",
    "cik",
    "composite_figi",
    "share_class_figi",
    "last_updated_utc",
)


def _upsert_ticker(session: Session, result: dict[str, Any]) -> None:
    values = {field: result.get(field) for field in _TICKER_FIELDS}
    stmt = sqlite_insert(Ticker).values(**values)
    stmt = stmt.on_conflict_do_update(index_elements=[Ticker.ticker], set_=values)
    session.execute(stmt)


async def sync_tickers(client: DataClient, session: Session) -> int:
    """Fetches every page of /v3/reference/tickers, upserting each result into the
    tickers table, then records this run's start time as the new sync cutoff.

    The cutoff saved is when this run STARTED, not when it finished - using the finish
    time would miss any ticker massive.com updates while this run was still paging
    through results, since the next run would only ask for updates after that later
    point.
    """
    # Naive but always UTC - matches SyncState.last_synced_at, which sqlite would hand
    # back naive on the next read regardless of what tzinfo went in.
    run_started_at = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    state = session.get(SyncState, JOB_NAME)
    is_first_run = state is None

    params: dict[str, Any] = {"limit": PAGE_LIMIT, "sort": "ticker", "order": "asc"}
    if state is not None:
        params["updated_since"] = f"{state.last_synced_at.isoformat()}Z"

    next_url: str | None = None
    fetched = 0
    while True:
        payload = await (client.get(next_url) if next_url else client.get(TICKERS_PATH, params=params))
        results = payload.get("results", [])
        for result in results:
            _upsert_ticker(session, result)
        fetched += len(results)

        next_url = payload.get("next_url")
        if not next_url:
            break

    if is_first_run:
        session.add(SyncState(job_name=JOB_NAME, last_synced_at=run_started_at))
    else:
        state.last_synced_at = run_started_at

    session.commit()
    logger.info("synced %d ticker(s) (%s)", fetched, "full" if is_first_run else "incremental")
    return fetched


async def run_once() -> int:
    init_db()
    async with DataClient() as client:
        session = SessionLocal()
        try:
            return await sync_tickers(client, session)
        finally:
            session.close()
