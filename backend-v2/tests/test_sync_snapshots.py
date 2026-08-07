import threading
import time

import httpx
import pytest

from data.client import DataClient
from db.models import CurrentSnapshot, Ticker
from db.session import SessionLocal, init_db
from jobs.control import JobCancelled, JobControl
from jobs.sync_snapshots import sync_snapshots


@pytest.fixture(autouse=True)
def _clean_db():
    init_db()
    session = SessionLocal()
    session.query(CurrentSnapshot).delete()
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


def _snapshot_payload(ticker: str, close: float) -> dict:
    return {
        "status": "OK",
        "ticker": {
            "ticker": ticker,
            "todaysChange": 1.23,
            "todaysChangePerc": 0.45,
            "updated": 1_700_000_000_000_000_000,
            "day": {"o": 1.0, "h": 2.0, "l": 0.5, "c": close, "v": 1000.0, "vw": 1.5},
            "min": {"o": 1.0, "h": 2.0, "l": 0.5, "c": close, "v": 10.0, "vw": 1.5, "av": 500.0, "t": 1_700_000_000_000},
            "prevDay": {"o": 1.0, "h": 2.0, "l": 0.5, "c": close - 1, "v": 900.0, "vw": 1.4},
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
        return httpx.Response(200, json=_snapshot_payload(ticker, close=10.0))

    try:
        fetched = sync_snapshots(
            session, tickers=["AAA", "BBB"], client_factory=lambda: _client_with_handler(handler)
        )

        assert fetched == 2
        rows = {row.ticker: row for row in session.query(CurrentSnapshot).all()}
        assert set(rows) == {"AAA", "BBB"}
        assert rows["AAA"].day_close == 10.0
        assert rows["AAA"].todays_change == 1.23
        assert rows["AAA"].min_accumulated_volume == 500.0
        assert rows["AAA"].prev_day_close == 9.0
    finally:
        session.close()

    assert sorted(paths) == [
        "/v2/snapshot/locale/us/markets/stocks/tickers/AAA",
        "/v2/snapshot/locale/us/markets/stocks/tickers/BBB",
    ]


def test_ticker_types_resolve_from_tickers_table():
    session = SessionLocal()
    session.add_all([Ticker(ticker="AAA", type="CS"), Ticker(ticker="BBB", type="ETF")])
    session.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        ticker = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json=_snapshot_payload(ticker, close=5.0))

    try:
        fetched = sync_snapshots(session, ticker_types=["CS"], client_factory=lambda: _client_with_handler(handler))

        assert fetched == 1
        assert [row.ticker for row in session.query(CurrentSnapshot).all()] == ["AAA"]
    finally:
        session.close()


def test_rerun_overwrites_rather_than_duplicating():
    session = SessionLocal()
    session.add(Ticker(ticker="AAA"))
    session.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_snapshot_payload("AAA", close=99.0))

    try:
        sync_snapshots(session, tickers=["AAA"], client_factory=lambda: _client_with_handler(handler))
        sync_snapshots(session, tickers=["AAA"], client_factory=lambda: _client_with_handler(handler))

        rows = session.query(CurrentSnapshot).all()
        assert len(rows) == 1
        assert rows[0].day_close == 99.0
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
        return httpx.Response(200, json=_snapshot_payload(ticker, close=1.0))

    try:
        fetched = sync_snapshots(
            session, tickers=["AAA", "BAD"], client_factory=lambda: _client_with_handler(handler)
        )

        assert fetched == 1
        assert [row.ticker for row in session.query(CurrentSnapshot).all()] == ["AAA"]
    finally:
        session.close()


def test_tickers_and_ticker_types_together_raises():
    session = SessionLocal()
    try:
        with pytest.raises(ValueError):
            sync_snapshots(session, ticker_types=["CS"], tickers=["AAA"])
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
        return httpx.Response(200, json=_snapshot_payload(ticker, close=1.0))

    try:
        with pytest.raises(JobCancelled):
            sync_snapshots(
                session,
                tickers=["AAA", "BBB", "CCC"],
                client_factory=lambda: _client_with_handler(handler),
                control=control,
            )

        # Cancelled before any worker's checkpoint let it through, so nothing fetched.
        assert session.query(CurrentSnapshot).count() == 0
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
        return httpx.Response(200, json=_snapshot_payload(ticker, close=1.0))

    def resume_shortly():
        time.sleep(0.05)
        control.request_resume()

    resumer = threading.Thread(target=resume_shortly)
    resumer.start()
    try:
        fetched = sync_snapshots(
            session, tickers=["AAA"], client_factory=lambda: _client_with_handler(handler), control=control
        )

        assert fetched == 1
        assert session.get(CurrentSnapshot, "AAA") is not None
    finally:
        session.close()
        resumer.join()
