import os
import tempfile

_db_fd, _db_path = tempfile.mkstemp(suffix=".db")
os.close(_db_fd)
os.environ["BACKEND_V2_DATABASE_URL"] = f"sqlite:///{_db_path}"

import datetime as dt  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402

from data.client import DataClient  # noqa: E402
from db.models import SyncState, Ticker  # noqa: E402
from db.session import SessionLocal, init_db  # noqa: E402
from jobs.sync_tickers import TICKERS_PATH, sync_tickers  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_db():
    init_db()
    session = SessionLocal()
    session.query(Ticker).delete()
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
