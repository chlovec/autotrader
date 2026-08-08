"""Syncs GET /v2/reference/news into the news table.

The first run (no SyncState row for JOB_NAME yet) only reaches back DEFAULT_BACKFILL_DAYS
- unlike sync_tickers, where "everything" is a bounded, roughly fixed-size universe of
tickers, massive.com's news archive goes back years and keeps growing, so an unbounded
first run would page through the entire history before ever catching up to "now". Every
run after that passes published_utc.gte=<the previous run's start time>, so only
articles published since then come back - same incremental cutoff shape as
jobs/sync_tickers.py's sync_tickers, minus that job's SyncProgress mid-run checkpoint: a
run that dies partway just leaves the cutoff unmoved, so the next run re-fetches (and
idempotently re-upserts) that same window rather than resuming a saved cursor.
"""

import datetime as dt
import json
import logging
import os
from typing import Any

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from data.client import DataClient
from db.models import News, SyncState
from db.session import SessionLocal, init_db
from jobs.control import JobControl

logger = logging.getLogger("backend_v2.jobs.sync_news")

JOB_NAME = "news"
NEWS_PATH = "/v2/reference/news"
PAGE_LIMIT = 1000
# How far back the very first run reaches - see this module's docstring. Same
# env-configurable-constant shape as jobs/sync_bars.py's DEFAULT_BACKFILL_DAYS, just a
# much shorter default since news, unlike bars, isn't needed in bulk historical depth.
DEFAULT_BACKFILL_DAYS = int(os.environ.get("NEWS_DEFAULT_BACKFILL_DAYS", "30"))


def _to_datetime(value: str | None) -> dt.datetime | None:
    """massive.com reports published_utc as an ISO-8601 string (e.g.
    "2021-04-26T13:16:44Z") rather than the epoch numbers other endpoints here use."""
    if value is None:
        return None
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(dt.timezone.utc).replace(tzinfo=None)


def _join(values: list[str] | None) -> str | None:
    return ",".join(values) if values else None


def _values_for(result: dict[str, Any]) -> dict[str, Any]:
    publisher = result.get("publisher") or {}
    insights = result.get("insights")
    return {
        "id": result.get("id"),
        "publisher_name": publisher.get("name"),
        "publisher_homepage_url": publisher.get("homepage_url"),
        "publisher_logo_url": publisher.get("logo_url"),
        "publisher_favicon_url": publisher.get("favicon_url"),
        "title": result.get("title"),
        "author": result.get("author"),
        "published_utc": _to_datetime(result.get("published_utc")),
        "article_url": result.get("article_url"),
        "amp_url": result.get("amp_url"),
        "image_url": result.get("image_url"),
        "description": result.get("description"),
        "keywords": _join(result.get("keywords")),
        "tickers": _join(result.get("tickers")),
        "insights": json.dumps(insights) if insights else None,
        "fetched_at": dt.datetime.now(dt.timezone.utc).replace(tzinfo=None),
    }


def _upsert_news(session: Session, result: dict[str, Any]) -> None:
    values = _values_for(result)
    if not values["id"]:
        return
    stmt = sqlite_insert(News).values(**values)
    stmt = stmt.on_conflict_do_update(index_elements=[News.id], set_=values)
    session.execute(stmt)


async def sync_news(
    client: DataClient,
    session: Session,
    backfill_days: int = DEFAULT_BACKFILL_DAYS,
    control: JobControl | None = None,
) -> int:
    """Fetches every page of /v2/reference/news published since the last successful
    run - or, on the first run, since `backfill_days` ago (see this module's
    docstring for why the first run isn't unbounded like sync_tickers's) - upserting
    each into the news table, then records this run's start time as the new cutoff.

    The cutoff saved is when this run STARTED, not when it finished - same reasoning
    as sync_tickers: using the finish time would miss anything published while this
    run was still paging through results.

    `control`, if given, is checked between pages (see jobs/control.py) - a pause
    blocks right here until resumed, and a cancel raises JobCancelled, left uncaught
    so it unwinds the whole run (and leaves the cutoff unmoved) instead of being
    treated as a partial success.
    """
    state = session.get(SyncState, JOB_NAME)
    is_first_run = state is None
    run_started_at = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    cutoff = state.last_synced_at if state is not None else run_started_at - dt.timedelta(days=backfill_days)

    params: dict[str, Any] = {
        "limit": PAGE_LIMIT,
        "sort": "published_utc",
        "order": "asc",
        "published_utc.gte": f"{cutoff.isoformat()}Z",
    }
    next_url: str | None = None

    fetched = 0
    while True:
        payload = await (client.get(next_url) if next_url else client.get(NEWS_PATH, params=params))
        results = payload.get("results", [])
        for result in results:
            _upsert_news(session, result)
        fetched += len(results)
        session.commit()

        next_url = payload.get("next_url")
        if not next_url:
            break
        if control is not None:
            await control.checkpoint_async()

    if is_first_run:
        session.add(SyncState(job_name=JOB_NAME, last_synced_at=run_started_at))
    else:
        state.last_synced_at = run_started_at
    session.commit()

    logger.info("synced %d news article(s) (%s)", fetched, "backfill" if is_first_run else "incremental")
    return fetched


async def run_once() -> int:
    init_db()
    async with DataClient() as client:
        session = SessionLocal()
        try:
            return await sync_news(client, session)
        finally:
            session.close()
