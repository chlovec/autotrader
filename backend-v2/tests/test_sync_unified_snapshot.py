import httpx
import pytest

from data.client import DataClient
from db.models import UnifiedSnapshot
from db.session import SessionLocal, init_db
from jobs.control import JobCancelled, JobControl
from jobs.sync_unified_snapshot import UNIFIED_SNAPSHOT_PATH, sync_unified_snapshot


@pytest.fixture(autouse=True)
def _clean_db():
    init_db()
    session = SessionLocal()
    session.query(UnifiedSnapshot).delete()
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


def _stock_result(ticker: str, close: float) -> dict:
    return {
        "ticker": ticker,
        "type": "stocks",
        "name": f"{ticker} Inc.",
        "market_status": "open",
        "session": {"change": 1.0, "change_percent": 2.0, "close": close, "open": close - 1, "volume": 1000},
        "last_quote": {"ask": close + 0.1, "bid": close - 0.1, "last_updated": 1_700_000_000_000_000_000},
        "last_trade": {
            "price": close,
            "size": 100,
            "conditions": [1, 2],
            "sip_timestamp": 1_700_000_000_000_000_000,
        },
    }


def _option_result(ticker: str) -> dict:
    return {
        "ticker": ticker,
        "type": "options",
        "break_even_price": 105.5,
        "implied_volatility": 0.32,
        "open_interest": 500,
        "details": {
            "contract_type": "call",
            "exercise_style": "american",
            "expiration_date": "2026-12-18",
            "shares_per_contract": 100,
            "strike_price": 100.0,
            "ticker": ticker,
        },
        "greeks": {"delta": 0.5, "gamma": 0.1, "theta": -0.05, "vega": 0.2},
        "underlying_asset": {"ticker": "AAA", "price": 102.3, "last_updated": 1_700_000_000_000_000_000},
    }


async def test_fetches_and_upserts_per_type():
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == UNIFIED_SNAPSHOT_PATH
        requests.append(dict(request.url.params))
        if request.url.params["type"] == "stocks":
            return httpx.Response(200, json={"status": "OK", "results": [_stock_result("AAA", 100.0)]})
        if request.url.params["type"] == "options":
            return httpx.Response(200, json={"status": "OK", "results": [_option_result("O:AAA260101C00100000")]})
        return httpx.Response(200, json={"status": "OK", "results": []})

    async with _client_with_handler(handler) as client:
        session = SessionLocal()
        try:
            results = await sync_unified_snapshot(client, session, snapshot_types=["stocks", "options", "indices"])

            assert results == {"stocks": 1, "options": 1, "indices": 0}

            stock_row = session.get(UnifiedSnapshot, "AAA")
            assert stock_row.type == "stocks"
            assert stock_row.session_close == 100.0
            assert stock_row.last_quote_ask == 100.1
            assert stock_row.last_trade_conditions == "1,2"

            option_row = session.get(UnifiedSnapshot, "O:AAA260101C00100000")
            assert option_row.details_contract_type == "call"
            assert option_row.details_strike_price == 100.0
            assert option_row.greeks_delta == 0.5
            assert option_row.underlying_asset_price == 102.3
        finally:
            session.close()

    assert {r["type"] for r in requests} == {"stocks", "options", "indices"}


async def test_defaults_to_every_type_when_none_given():
    fetched_types: set[str] = set()

    def handler(request: httpx.Request) -> httpx.Response:
        fetched_types.add(request.url.params["type"])
        return httpx.Response(200, json={"status": "OK", "results": []})

    async with _client_with_handler(handler) as client:
        session = SessionLocal()
        try:
            await sync_unified_snapshot(client, session)
        finally:
            session.close()

    assert fetched_types == {"stocks", "options", "indices", "fx", "crypto"}


async def test_paginates_via_next_url():
    def handler(request: httpx.Request) -> httpx.Response:
        if "cursor" not in request.url.params:
            return httpx.Response(
                200,
                json={
                    "status": "OK",
                    "results": [_stock_result("AAA", 10.0)],
                    "next_url": "https://api.massive.com/v3/snapshot?type=stocks&cursor=page2",
                },
            )
        return httpx.Response(200, json={"status": "OK", "results": [_stock_result("BBB", 20.0)]})

    async with _client_with_handler(handler) as client:
        session = SessionLocal()
        try:
            results = await sync_unified_snapshot(client, session, snapshot_types=["stocks"])
            assert results == {"stocks": 2}
            assert {row.ticker for row in session.query(UnifiedSnapshot).all()} == {"AAA", "BBB"}
        finally:
            session.close()


async def test_rerun_upserts_rather_than_duplicating():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "OK", "results": [_stock_result("AAA", 50.0)]})

    async with _client_with_handler(handler) as client:
        session = SessionLocal()
        try:
            await sync_unified_snapshot(client, session, snapshot_types=["stocks"])
            await sync_unified_snapshot(client, session, snapshot_types=["stocks"])

            rows = session.query(UnifiedSnapshot).all()
            assert len(rows) == 1
            assert rows[0].session_close == 50.0
        finally:
            session.close()


async def test_one_type_failure_does_not_block_others():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["type"] == "options":
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(200, json={"status": "OK", "results": [_stock_result("AAA", 1.0)]})

    async with _client_with_handler(handler) as client:
        session = SessionLocal()
        try:
            results = await sync_unified_snapshot(client, session, snapshot_types=["stocks", "options"])
            assert results == {"stocks": 1}
        finally:
            session.close()


async def test_cancel_stops_the_run_and_raises_job_cancelled():
    control = JobControl()
    control.request_cancel()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "OK", "results": [_stock_result("AAA", 1.0)]})

    async with _client_with_handler(handler) as client:
        session = SessionLocal()
        try:
            with pytest.raises(JobCancelled):
                await sync_unified_snapshot(client, session, snapshot_types=["stocks"], control=control)

            assert session.query(UnifiedSnapshot).count() == 0
        finally:
            session.close()
