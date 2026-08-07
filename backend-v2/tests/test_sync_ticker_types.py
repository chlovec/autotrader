import httpx
import pytest

from data.client import DataClient
from db.models import TickerType
from db.session import SessionLocal, init_db
from jobs.sync_ticker_types import TICKER_TYPES_PATH, sync_ticker_types


@pytest.fixture(autouse=True)
def _clean_db():
    init_db()
    session = SessionLocal()
    session.query(TickerType).delete()
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


async def test_fetches_and_upserts_every_result():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == TICKER_TYPES_PATH
        return httpx.Response(
            200,
            json={
                "results": [
                    {"code": "CS", "description": "Common Stock", "asset_class": "stocks", "locale": "us"},
                    {"code": "ETF", "description": "Exchange Traded Fund", "asset_class": "stocks", "locale": "us"},
                ]
            },
        )

    async with _client_with_handler(handler) as client:
        session = SessionLocal()
        try:
            fetched = await sync_ticker_types(client, session)

            assert fetched == 2
            codes = {row.code for row in session.query(TickerType).all()}
            assert codes == {"CS", "ETF"}
        finally:
            session.close()

    assert len(requests) == 1


async def test_rerun_upserts_rather_than_duplicating():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {"code": "CS", "description": "Common Stock (updated)", "asset_class": "stocks", "locale": "us"},
                ]
            },
        )

    async with _client_with_handler(handler) as client:
        session = SessionLocal()
        try:
            await sync_ticker_types(client, session)
            await sync_ticker_types(client, session)

            rows = session.query(TickerType).all()
            assert len(rows) == 1
            assert rows[0].description == "Common Stock (updated)"
        finally:
            session.close()
