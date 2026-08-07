import asyncio
import datetime as dt

import httpx
import pytest

from data.client import DataClient
from db.models import SyncProgress, SyncState, Ticker
from db.session import SessionLocal, init_db
from jobs.control import JobCancelled, JobControl
from jobs.sync_tickers import TICKERS_PATH, sync_tickers


@pytest.fixture(autouse=True)
def _clean_db():
    init_db()
    session = SessionLocal()
    session.query(Ticker).delete()
    session.query(SyncState).delete()
    session.query(SyncProgress).delete()
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


async def test_first_run_paginates_and_fetches_everything():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == TICKERS_PATH and "cursor" not in request.url.params:
            return httpx.Response(
                200,
                json={
                    "results": [{"ticker": "AAA", "name": "Alpha", "last_updated_utc": "2026-01-01"}],
                    "next_url": "https://api.massive.com/v3/reference/tickers?cursor=page2",
                },
            )
        return httpx.Response(
            200,
            json={"results": [{"ticker": "BBB", "name": "Beta", "last_updated_utc": "2026-01-02"}]},
        )

    async with _client_with_handler(handler) as client:
        session = SessionLocal()
        try:
            fetched = await sync_tickers(client, session)

            assert fetched == 2
            tickers = {row.ticker for row in session.query(Ticker).all()}
            assert tickers == {"AAA", "BBB"}

            state = session.get(SyncState, "tickers")
            assert state is not None
        finally:
            session.close()

    # First request is the full sync (no updated_since); second follows next_url verbatim.
    assert "updated_since" not in requests[0].url.params
    assert requests[1].url.params["cursor"] == "page2"


async def test_second_run_requests_only_updates_since_last_sync():
    last_synced_at = dt.datetime(2026, 1, 1)  # naive, treated as UTC (see db/models.py)
    session = SessionLocal()
    session.add(SyncState(job_name="tickers", last_synced_at=last_synced_at))
    session.add(Ticker(ticker="AAA", name="Alpha (stale)"))
    session.commit()
    session.close()

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"results": [{"ticker": "AAA", "name": "Alpha (updated)", "last_updated_utc": "2026-02-01"}]},
        )

    async with _client_with_handler(handler) as client:
        session = SessionLocal()
        try:
            fetched = await sync_tickers(client, session)

            assert fetched == 1
            updated = session.get(Ticker, "AAA")
            assert updated.name == "Alpha (updated)"

            state = session.get(SyncState, "tickers")
            assert state.last_synced_at > last_synced_at
        finally:
            session.close()

    assert requests[0].url.params["updated_since"] == f"{last_synced_at.isoformat()}Z"


async def test_resumes_from_checkpoint_after_page_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TICKERS_PATH and "cursor" not in request.url.params:
            return httpx.Response(
                200,
                json={
                    "results": [{"ticker": "AAA", "name": "Alpha"}],
                    "next_url": "https://api.massive.com/v3/reference/tickers?cursor=page2",
                },
            )
        return httpx.Response(500, json={"error": "boom"})  # page 2 always fails

    async with _client_with_handler(handler) as client:
        session = SessionLocal()
        try:
            with pytest.raises(httpx.HTTPStatusError):
                await sync_tickers(client, session)

            # Page 1's ticker survives even though the run as a whole failed.
            assert session.get(Ticker, "AAA") is not None
            # The run never finished, so no cutoff was recorded yet...
            assert session.get(SyncState, "tickers") is None
            # ...but the failed page's cursor was checkpointed.
            progress = session.get(SyncProgress, "tickers")
            assert progress is not None
            assert progress.next_url == "https://api.massive.com/v3/reference/tickers?cursor=page2"
        finally:
            session.close()

    requests: list[httpx.Request] = []

    def resumed_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"results": [{"ticker": "BBB", "name": "Beta"}]})

    async with _client_with_handler(resumed_handler) as client:
        session = SessionLocal()
        try:
            fetched = await sync_tickers(client, session)

            # Only page 2's result - page 1 wasn't re-fetched.
            assert fetched == 1
            assert session.get(Ticker, "BBB") is not None
            assert session.get(SyncState, "tickers") is not None
            assert session.get(SyncProgress, "tickers") is None
        finally:
            session.close()

    assert len(requests) == 1
    assert requests[0].url.params["cursor"] == "page2"


async def test_stale_checkpoint_from_different_ticker_type_is_discarded():
    session = SessionLocal()
    session.add(
        SyncProgress(
            job_name="tickers",
            next_url="https://api.massive.com/v3/reference/tickers?cursor=stale&type=ETF",
            run_started_at=dt.datetime(2026, 1, 1),
            ticker_type="ETF",
        )
    )
    session.commit()
    session.close()

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"results": [{"ticker": "AAA", "name": "Alpha"}]})

    async with _client_with_handler(handler) as client:
        session = SessionLocal()
        try:
            fetched = await sync_tickers(client, session, ticker_type="CS")
            assert fetched == 1
        finally:
            session.close()

    # Started a fresh full request for the new filter, not the stale ETF cursor.
    assert requests[0].url.path == TICKERS_PATH
    assert "cursor" not in requests[0].url.params
    assert requests[0].url.params["type"] == "CS"


async def test_cancel_between_pages_leaves_a_checkpoint_the_next_run_resumes_from():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TICKERS_PATH and "cursor" not in request.url.params:
            return httpx.Response(
                200,
                json={
                    "results": [{"ticker": "AAA", "name": "Alpha"}],
                    "next_url": "https://api.massive.com/v3/reference/tickers?cursor=page2",
                },
            )
        return httpx.Response(200, json={"results": [{"ticker": "BBB", "name": "Beta"}]})

    control = JobControl()
    control.request_cancel()

    async with _client_with_handler(handler) as client:
        session = SessionLocal()
        try:
            with pytest.raises(JobCancelled):
                await sync_tickers(client, session, control=control)

            # Page 1 already committed before the cancel checkpoint was reached.
            assert session.get(Ticker, "AAA") is not None
            assert session.get(Ticker, "BBB") is None
            assert session.get(SyncState, "tickers") is None
            progress = session.get(SyncProgress, "tickers")
            assert progress is not None
            assert progress.next_url == "https://api.massive.com/v3/reference/tickers?cursor=page2"
        finally:
            session.close()

    # A fresh run (no cancellation this time) resumes from page 2, same as crash recovery.
    requests: list[httpx.Request] = []

    def resumed_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"results": [{"ticker": "BBB", "name": "Beta"}]})

    async with _client_with_handler(resumed_handler) as client:
        session = SessionLocal()
        try:
            fetched = await sync_tickers(client, session)

            assert fetched == 1
            assert session.get(Ticker, "BBB") is not None
            assert session.get(SyncProgress, "tickers") is None
        finally:
            session.close()

    assert requests[0].url.params["cursor"] == "page2"


async def test_pause_blocks_between_pages_until_resumed():
    fetched_page_two = asyncio.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TICKERS_PATH and "cursor" not in request.url.params:
            return httpx.Response(
                200,
                json={
                    "results": [{"ticker": "AAA", "name": "Alpha"}],
                    "next_url": "https://api.massive.com/v3/reference/tickers?cursor=page2",
                },
            )
        fetched_page_two.set()
        return httpx.Response(200, json={"results": [{"ticker": "BBB", "name": "Beta"}]})

    control = JobControl()
    control.request_pause()

    async def resume_after_page_one_commits():
        # Page 1 must already be committed (the job is parked at the checkpoint, not
        # mid-fetch) before this resumes it.
        while session_scoped.get(Ticker, "AAA") is None:
            await asyncio.sleep(0.01)
        assert not fetched_page_two.is_set()
        control.request_resume()

    async with _client_with_handler(handler) as client:
        session_scoped = SessionLocal()
        try:
            resume_task = asyncio.create_task(resume_after_page_one_commits())
            fetched = await sync_tickers(client, session_scoped, control=control)
            await resume_task

            assert fetched == 2
            assert session_scoped.get(Ticker, "BBB") is not None
        finally:
            session_scoped.close()
