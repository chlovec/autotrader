import threading
import time

import httpx
import pytest

from data.client import DataClient
from db.models import Ticker, TickerDetail
from db.session import SessionLocal, init_db
from jobs.control import JobCancelled, JobControl
from jobs.sync_ticker_details import sync_ticker_details


@pytest.fixture(autouse=True)
def _clean_db():
    init_db()
    session = SessionLocal()
    session.query(TickerDetail).delete()
    session.query(Ticker).delete()
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


def _ticker_details_payload(ticker: str, market_cap: float) -> dict:
    return {
        "status": "OK",
        "results": {
            "ticker": ticker,
            "market_cap": market_cap,
            "share_class_shares_outstanding": 1_000_000.0,
            "weighted_shares_outstanding": 990_000.0,
            "sic_code": "7372",
            "sic_description": "SERVICES-PREPACKAGED SOFTWARE",
            "homepage_url": "https://example.com",
            "total_employees": 500,
            "list_date": "2010-01-15",
            "round_lot": 100,
        },
    }


def test_fetches_and_upserts_explicit_tickers():
    session = SessionLocal()
    session.add_all([Ticker(ticker="AAA"), Ticker(ticker="BBB")])
    session.commit()

    lock = threading.Lock()
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        with lock:
            paths.append(request.url.path)
        ticker = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json=_ticker_details_payload(ticker, market_cap=1_000_000_000.0))

    try:
        fetched = sync_ticker_details(
            session, tickers=["AAA", "BBB"], client_factory=lambda: _client_with_handler(handler)
        )

        assert fetched == 2
        rows = {row.ticker: row for row in session.query(TickerDetail).all()}
        assert set(rows) == {"AAA", "BBB"}
        assert rows["AAA"].market_cap == 1_000_000_000.0
        assert rows["AAA"].share_class_shares_outstanding == 1_000_000.0
        assert rows["AAA"].weighted_shares_outstanding == 990_000.0
        assert rows["AAA"].sic_code == "7372"
        assert rows["AAA"].sic_description == "SERVICES-PREPACKAGED SOFTWARE"
        assert rows["AAA"].homepage_url == "https://example.com"
        assert rows["AAA"].total_employees == 500
        assert rows["AAA"].list_date.isoformat() == "2010-01-15"
        assert rows["AAA"].round_lot == 100
        assert rows["AAA"].fetched_at is not None
    finally:
        session.close()

    assert sorted(paths) == [
        "/v3/reference/tickers/AAA",
        "/v3/reference/tickers/BBB",
    ]


def test_ticker_types_resolve_from_tickers_table():
    session = SessionLocal()
    session.add_all([Ticker(ticker="AAA", type="CS"), Ticker(ticker="BBB", type="ETF")])
    session.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        ticker = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json=_ticker_details_payload(ticker, market_cap=5.0))

    try:
        fetched = sync_ticker_details(
            session, ticker_types=["CS"], client_factory=lambda: _client_with_handler(handler)
        )

        assert fetched == 1
        assert [row.ticker for row in session.query(TickerDetail).all()] == ["AAA"]
    finally:
        session.close()


def test_rerun_overwrites_rather_than_duplicating():
    session = SessionLocal()
    session.add(Ticker(ticker="AAA"))
    session.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ticker_details_payload("AAA", market_cap=99.0))

    try:
        sync_ticker_details(session, tickers=["AAA"], client_factory=lambda: _client_with_handler(handler))
        sync_ticker_details(session, tickers=["AAA"], client_factory=lambda: _client_with_handler(handler))

        rows = session.query(TickerDetail).all()
        assert len(rows) == 1
        assert rows[0].market_cap == 99.0
    finally:
        session.close()


def test_one_ticker_failure_does_not_block_others():
    session = SessionLocal()
    session.add_all([Ticker(ticker="AAA"), Ticker(ticker="BAD")])
    session.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        ticker = request.url.path.rsplit("/", 1)[-1]
        if ticker == "BAD":
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(200, json=_ticker_details_payload(ticker, market_cap=1.0))

    try:
        fetched = sync_ticker_details(
            session, tickers=["AAA", "BAD"], client_factory=lambda: _client_with_handler(handler)
        )

        assert fetched == 1
        assert [row.ticker for row in session.query(TickerDetail).all()] == ["AAA"]
    finally:
        session.close()


def test_missing_results_key_counts_as_a_failure():
    session = SessionLocal()
    session.add(Ticker(ticker="AAA"))
    session.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "NOT_FOUND"})

    try:
        fetched = sync_ticker_details(
            session, tickers=["AAA"], client_factory=lambda: _client_with_handler(handler)
        )

        assert fetched == 0
        assert session.query(TickerDetail).count() == 0
    finally:
        session.close()


def test_tickers_and_ticker_types_together_raises():
    session = SessionLocal()
    try:
        with pytest.raises(ValueError):
            sync_ticker_details(session, ticker_types=["CS"], tickers=["AAA"])
    finally:
        session.close()


def test_cancel_stops_the_run_and_raises_job_cancelled():
    session = SessionLocal()
    session.add_all([Ticker(ticker="AAA"), Ticker(ticker="BBB"), Ticker(ticker="CCC")])
    session.commit()

    control = JobControl()
    control.request_cancel()

    def handler(request: httpx.Request) -> httpx.Response:
        ticker = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json=_ticker_details_payload(ticker, market_cap=1.0))

    try:
        with pytest.raises(JobCancelled):
            sync_ticker_details(
                session,
                tickers=["AAA", "BBB", "CCC"],
                client_factory=lambda: _client_with_handler(handler),
                control=control,
            )

        # Cancelled before any worker's checkpoint let it through, so nothing fetched.
        assert session.query(TickerDetail).count() == 0
    finally:
        session.close()


def test_pause_blocks_a_worker_until_resumed():
    session = SessionLocal()
    session.add(Ticker(ticker="AAA"))
    session.commit()

    control = JobControl()
    control.request_pause()

    def handler(request: httpx.Request) -> httpx.Response:
        ticker = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json=_ticker_details_payload(ticker, market_cap=1.0))

    def resume_shortly():
        time.sleep(0.05)
        control.request_resume()

    resumer = threading.Thread(target=resume_shortly)
    resumer.start()
    try:
        fetched = sync_ticker_details(
            session, tickers=["AAA"], client_factory=lambda: _client_with_handler(handler), control=control
        )

        assert fetched == 1
        assert session.get(TickerDetail, "AAA") is not None
    finally:
        session.close()
        resumer.join()
