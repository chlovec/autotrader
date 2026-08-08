import httpx
import pytest

from data.client import DataClient
from db.models import TopMarketMover
from db.session import SessionLocal, init_db
from jobs.control import JobCancelled, JobControl
from jobs.sync_top_movers import sync_top_movers


@pytest.fixture(autouse=True)
def _clean_db():
    init_db()
    session = SessionLocal()
    session.query(TopMarketMover).delete()
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


def _mover(ticker: str, close: float) -> dict:
    return {
        "ticker": ticker,
        "todaysChange": 1.23,
        "todaysChangePerc": 4.56,
        "updated": 1_700_000_000_000_000_000,
        "day": {"o": 1.0, "h": 2.0, "l": 0.5, "c": close, "v": 1000.0, "vw": 1.5},
        "min": {"o": 1.0, "h": 2.0, "l": 0.5, "c": close, "v": 10.0, "vw": 1.5, "av": 500.0, "t": 1_700_000_000_000},
        "prevDay": {"o": 1.0, "h": 2.0, "l": 0.5, "c": close - 1, "v": 900.0, "vw": 1.4},
    }


async def test_fetches_both_directions_into_one_table_with_rank():
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path.endswith("/gainers"):
            return httpx.Response(200, json={"status": "OK", "tickers": [_mover("AAA", 20.0), _mover("BBB", 15.0)]})
        return httpx.Response(200, json={"status": "OK", "tickers": [_mover("CCC", 5.0)]})

    async with _client_with_handler(handler) as client:
        session = SessionLocal()
        try:
            results = await sync_top_movers(client, session)

            assert results == {"gainers": 2, "losers": 1}
            rows = {row.ticker: row for row in session.query(TopMarketMover).all()}
            assert rows["AAA"].direction == "gainers"
            assert rows["AAA"].rank == 1
            assert rows["AAA"].day_close == 20.0
            assert rows["BBB"].rank == 2
            assert rows["CCC"].direction == "losers"
            assert rows["CCC"].rank == 1
        finally:
            session.close()

    assert sorted(requests) == [
        "/v2/snapshot/locale/us/markets/stocks/gainers",
        "/v2/snapshot/locale/us/markets/stocks/losers",
    ]


async def test_rerun_replaces_rather_than_accumulating_stale_tickers():
    call_count = {"gainers": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        direction = request.url.path.rsplit("/", 1)[-1]
        if direction == "gainers":
            call_count["gainers"] += 1
            if call_count["gainers"] == 1:
                return httpx.Response(200, json={"status": "OK", "tickers": [_mover("AAA", 10.0)]})
            return httpx.Response(200, json={"status": "OK", "tickers": [_mover("BBB", 10.0)]})
        return httpx.Response(200, json={"status": "OK", "tickers": []})

    async with _client_with_handler(handler) as client:
        session = SessionLocal()
        try:
            await sync_top_movers(client, session)
            await sync_top_movers(client, session)

            gainers = session.query(TopMarketMover).filter(TopMarketMover.direction == "gainers").all()
            # AAA dropped off the list on the second run - it shouldn't linger.
            assert [row.ticker for row in gainers] == ["BBB"]
        finally:
            session.close()


async def test_cancel_stops_the_run_and_raises_job_cancelled():
    control = JobControl()
    control.request_cancel()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "OK", "tickers": [_mover("AAA", 10.0)]})

    async with _client_with_handler(handler) as client:
        session = SessionLocal()
        try:
            with pytest.raises(JobCancelled):
                await sync_top_movers(client, session, control=control)

            assert session.query(TopMarketMover).count() == 0
        finally:
            session.close()
