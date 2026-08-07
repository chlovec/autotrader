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

from sqlalchemy import delete
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from data.client import DataClient
from db.models import SyncProgress, SyncState, Ticker
from db.session import SessionLocal, init_db
from jobs.control import JobControl

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


async def sync_tickers(
    client: DataClient, session: Session, ticker_type: str | None = None, control: JobControl | None = None
) -> int:
    """Fetches every page of /v3/reference/tickers, upserting each result into the
    tickers table, then records this run's start time as the new sync cutoff.

    The cutoff saved is when this run STARTED, not when it finished - using the finish
    time would miss any ticker massive.com updates while this run was still paging
    through results, since the next run would only ask for updates after that later
    point.

    `ticker_type` restricts the sync to one upstream `type` (e.g. "CS") - set from the
    dashboard's Jobs page (JobConfig.ticker_types for this job is a single value, unlike
    the bars job's multi-select use of the same column); None syncs every type.

    Progress is checkpointed to sync_progress after every page - if the job dies
    partway (e.g. DataClient exhausts its 429 retries), the *next* call to sync_tickers
    resumes from the failed page's cursor instead of re-paging everything already
    fetched. A checkpoint left behind by a different ticker_type filter is discarded
    rather than resumed, since massive.com bakes the filter into the cursor itself.

    `control`, if given, is checked between pages (see jobs/control.py) - a pause
    blocks right here until resumed, and a cancel raises JobCancelled once the
    in-flight page's results are already committed, so the same sync_progress cursor
    above is what the next run (paused-then-resumed or a fresh trigger) continues from.
    """
    state = session.get(SyncState, JOB_NAME)
    is_first_run = state is None
    progress = session.get(SyncProgress, JOB_NAME)

    if progress is not None and progress.ticker_type == ticker_type:
        run_started_at = progress.run_started_at
        next_url: str | None = progress.next_url
        params: dict[str, Any] = {}
    else:
        # A stale checkpoint (different ticker_type) is left in place rather than
        # deleted here - the next page fetched under the current filter overwrites it
        # via the merge() below, or the final cleanup clears it once this run completes.
        # Naive but always UTC - matches SyncState.last_synced_at, which sqlite would
        # hand back naive on the next read regardless of what tzinfo went in.
        run_started_at = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
        next_url = None
        params = {"limit": PAGE_LIMIT, "sort": "ticker", "order": "asc"}
        if ticker_type:
            params["type"] = ticker_type
        if state is not None:
            params["updated_since"] = f"{state.last_synced_at.isoformat()}Z"

    fetched = 0
    while True:
        payload = await (client.get(next_url) if next_url else client.get(TICKERS_PATH, params=params))
        results = payload.get("results", [])
        for result in results:
            _upsert_ticker(session, result)
        fetched += len(results)

        next_url = payload.get("next_url")
        if next_url:
            session.merge(
                SyncProgress(
                    job_name=JOB_NAME, next_url=next_url, run_started_at=run_started_at, ticker_type=ticker_type
                )
            )
        session.commit()
        if not next_url:
            break
        if control is not None:
            await control.checkpoint_async()

    if is_first_run:
        session.add(SyncState(job_name=JOB_NAME, last_synced_at=run_started_at))
    else:
        state.last_synced_at = run_started_at
    session.execute(delete(SyncProgress).where(SyncProgress.job_name == JOB_NAME))

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
