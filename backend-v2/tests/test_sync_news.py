import datetime as dt
import json

import httpx
import pytest

from data.client import DataClient
from db.models import News, SyncState
from db.session import SessionLocal, init_db
from jobs.control import JobCancelled, JobControl
from jobs.sync_news import NEWS_PATH, sync_news


@pytest.fixture(autouse=True)
def _clean_db():
    init_db()
    session = SessionLocal()
    session.query(News).delete()
    session.query(SyncState).delete()
    session.commit()
    session.close()
    yield


def _client_with_handler(handler) -> DataClient:
    client = DataClient(api_key="test-key")
    client._client = httpx.AsyncClient(
        base_url=client._client.base_url,
        headers=client._client.headers,
        transport=httpx.MockTransport(handler),
    )
    return client


def _article(article_id: str, title: str) -> dict:
    return {
        "id": article_id,
        "publisher": {
            "name": "Example Wire",
            "homepage_url": "https://example.com",
            "logo_url": "https://example.com/logo.png",
            "favicon_url": "https://example.com/favicon.ico",
        },
        "title": title,
        "author": "Jane Reporter",
        "published_utc": "2026-01-01T13:16:44Z",
        "article_url": f"https://example.com/{article_id}",
        "amp_url": f"https://example.com/amp/{article_id}",
        "image_url": f"https://example.com/{article_id}.jpg",
        "description": "A description.",
        "keywords": ["markets", "stocks"],
        "tickers": ["AAA", "BBB"],
        "insights": [{"ticker": "AAA", "sentiment": "positive", "sentiment_reasoning": "good news"}],
    }


async def test_first_run_paginates_and_only_backfills_the_given_window():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == NEWS_PATH and "cursor" not in request.url.params:
            return httpx.Response(
                200,
                json={
                    "results": [_article("1", "First")],
                    "next_url": "https://api.massive.com/v2/reference/news?cursor=page2",
                },
            )
        return httpx.Response(200, json={"results": [_article("2", "Second")]})

    async with _client_with_handler(handler) as client:
        session = SessionLocal()
        try:
            before = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
            fetched = await sync_news(client, session, backfill_days=7)

            assert fetched == 2
            ids = {row.id for row in session.query(News).all()}
            assert ids == {"1", "2"}

            row = session.get(News, "1")
            assert row.publisher_name == "Example Wire"
            assert row.tickers == "AAA,BBB"
            assert row.keywords == "markets,stocks"
            assert json.loads(row.insights) == [
                {"ticker": "AAA", "sentiment": "positive", "sentiment_reasoning": "good news"}
            ]
            assert row.published_utc == dt.datetime(2026, 1, 1, 13, 16, 44)

            state = session.get(SyncState, "news")
            assert state is not None
        finally:
            session.close()

    # First run only reaches back 7 days, not massive.com's entire news history.
    cutoff = dt.datetime.fromisoformat(requests[0].url.params["published_utc.gte"].rstrip("Z"))
    assert abs((before - dt.timedelta(days=7) - cutoff).total_seconds()) < 5
    assert requests[1].url.params["cursor"] == "page2"


async def test_second_run_requests_only_articles_published_since_last_sync():
    last_synced_at = dt.datetime(2026, 1, 1)  # naive, treated as UTC (see db/models.py)
    session = SessionLocal()
    session.add(SyncState(job_name="news", last_synced_at=last_synced_at))
    session.commit()
    session.close()

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"results": [_article("1", "Fresh")]})

    async with _client_with_handler(handler) as client:
        session = SessionLocal()
        try:
            fetched = await sync_news(client, session)

            assert fetched == 1
            state = session.get(SyncState, "news")
            assert state.last_synced_at > last_synced_at
        finally:
            session.close()

    assert requests[0].url.params["published_utc.gte"] == f"{last_synced_at.isoformat()}Z"


async def test_rerun_upserts_rather_than_duplicating():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [_article("1", "Updated title")]})

    async with _client_with_handler(handler) as client:
        session = SessionLocal()
        try:
            await sync_news(client, session)
            await sync_news(client, session)

            rows = session.query(News).all()
            assert len(rows) == 1
            assert rows[0].title == "Updated title"
        finally:
            session.close()


async def test_cancel_between_pages_leaves_cutoff_unmoved():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == NEWS_PATH and "cursor" not in request.url.params:
            return httpx.Response(
                200,
                json={
                    "results": [_article("1", "First")],
                    "next_url": "https://api.massive.com/v2/reference/news?cursor=page2",
                },
            )
        return httpx.Response(200, json={"results": [_article("2", "Second")]})

    control = JobControl()
    control.request_cancel()

    async with _client_with_handler(handler) as client:
        session = SessionLocal()
        try:
            with pytest.raises(JobCancelled):
                await sync_news(client, session, control=control)

            # Page 1 already committed before the cancel checkpoint was reached.
            assert session.get(News, "1") is not None
            assert session.get(News, "2") is None
            assert session.get(SyncState, "news") is None
        finally:
            session.close()
