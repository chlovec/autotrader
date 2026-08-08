import datetime as dt
import threading
import time

import httpx
import pytest

from data.client import DataClient
from db.models import OhlcBar, Ticker, TickerBarSyncState
from db.session import SessionLocal, init_db
from jobs.control import JobCancelled, JobControl
from jobs.sync_bars import fetch_and_store_bars, sync_bars_manual, sync_bars_nightly


@pytest.fixture(autouse=True)
def _clean_db():
    init_db()
    session = SessionLocal()
    session.query(OhlcBar).delete()
    session.query(TickerBarSyncState).delete()
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


def _bar(ts: dt.date, close: float) -> dict:
    timestamp_ms = int(dt.datetime(ts.year, ts.month, ts.day, tzinfo=dt.timezone.utc).timestamp() * 1000)
    return {"t": timestamp_ms, "o": close - 1, "h": close + 1, "l": close - 2, "c": close, "v": 1000, "vw": close, "n": 10}


async def test_fetch_and_store_bars_paginates_upserts_and_advances_cursor():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if "cursor" not in request.url.params:
            return httpx.Response(
                200,
                json={
                    "results": [_bar(dt.date(2024, 1, 1), 10.0)],
                    "next_url": "https://api.massive.com/v2/aggs/ticker/AAA/range/1/day/2024-01-01/2024-01-02?cursor=page2",
                },
            )
        return httpx.Response(200, json={"results": [_bar(dt.date(2024, 1, 2), 11.0)]})

    async with _client_with_handler(handler) as client:
        session = SessionLocal()
        try:
            fetched = await fetch_and_store_bars(client, session, "AAA", dt.date(2024, 1, 1), dt.date(2024, 1, 2))

            assert fetched == 2
            bars = session.query(OhlcBar).order_by(OhlcBar.timestamp).all()
            assert [b.close for b in bars] == [10.0, 11.0]
            assert bars[0].multiplier == 1
            assert bars[0].timespan == "day"

            state = session.get(TickerBarSyncState, ("AAA", 1, "day"))
            assert state.synced_through == dt.date(2024, 1, 2)
        finally:
            session.close()

    assert requests[0].url.path == "/v2/aggs/ticker/AAA/range/1/day/2024-01-01/2024-01-02"
    assert requests[1].url.params["cursor"] == "page2"


async def test_fetch_and_store_bars_never_rewinds_cursor():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [_bar(dt.date(2020, 1, 1), 5.0)]})

    session = SessionLocal()
    session.add(TickerBarSyncState(ticker="AAA", multiplier=1, timespan="day", synced_through=dt.date(2026, 1, 1)))
    session.commit()
    session.close()

    async with _client_with_handler(handler) as client:
        session = SessionLocal()
        try:
            # A manual backfill of an old gap must not roll the cursor backwards.
            await fetch_and_store_bars(client, session, "AAA", dt.date(2020, 1, 1), dt.date(2020, 1, 1))
            state = session.get(TickerBarSyncState, ("AAA", 1, "day"))
            assert state.synced_through == dt.date(2026, 1, 1)
        finally:
            session.close()


def test_sync_bars_manual_defaults_to_all_tickers_and_isolates_failures():
    session = SessionLocal()
    session.add_all([Ticker(ticker="AAA", type="CS"), Ticker(ticker="BBB", type="CS")])
    session.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        if "/ticker/AAA/" in request.url.path:
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(200, json={"results": [_bar(dt.date(2024, 1, 1), 1.0)]})

    try:
        results = sync_bars_manual(
            session,
            dt.date(2024, 1, 1),
            dt.date(2024, 1, 1),
            client_factory=lambda: _client_with_handler(handler),
        )
    finally:
        session.close()

    assert results == {"BBB": 1}  # AAA's failure didn't stop BBB or blow up the run


def test_sync_bars_manual_rejects_tickers_and_ticker_types_together():
    session = SessionLocal()
    try:
        with pytest.raises(ValueError):
            sync_bars_manual(
                session, dt.date(2024, 1, 1), dt.date(2024, 1, 1), ticker_types=["CS"], tickers=["AAA"]
            )
    finally:
        session.close()


def test_sync_bars_manual_cancel_stops_the_run_and_raises_job_cancelled():
    session = SessionLocal()
    session.add_all([Ticker(ticker="AAA"), Ticker(ticker="BBB"), Ticker(ticker="CCC")])
    session.commit()

    control = JobControl()
    control.request_cancel()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [_bar(dt.date(2024, 1, 1), 1.0)]})

    try:
        with pytest.raises(JobCancelled):
            sync_bars_manual(
                session,
                dt.date(2024, 1, 1),
                dt.date(2024, 1, 1),
                tickers=["AAA", "BBB", "CCC"],
                client_factory=lambda: _client_with_handler(handler),
                control=control,
            )

        # Cancelled before any worker's checkpoint let it through, so nothing fetched.
        assert session.query(OhlcBar).count() == 0
    finally:
        session.close()


def test_sync_bars_manual_pause_blocks_a_worker_until_resumed():
    session = SessionLocal()
    session.add(Ticker(ticker="AAA"))
    session.commit()

    control = JobControl()
    control.request_pause()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [_bar(dt.date(2024, 1, 1), 1.0)]})

    def resume_shortly():
        time.sleep(0.05)
        control.request_resume()

    resumer = threading.Thread(target=resume_shortly)
    resumer.start()
    try:
        results = sync_bars_manual(
            session,
            dt.date(2024, 1, 1),
            dt.date(2024, 1, 1),
            tickers=["AAA"],
            client_factory=lambda: _client_with_handler(handler),
            control=control,
        )

        assert results == {"AAA": 1}
        assert session.query(OhlcBar).count() == 1
    finally:
        session.close()
        resumer.join()


def test_sync_bars_nightly_skips_up_to_date_and_resumes_from_cursor():
    yesterday = dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)

    session = SessionLocal()
    session.add_all([Ticker(ticker="UPTODATE"), Ticker(ticker="RESUME"), Ticker(ticker="NEVER")])
    session.add(TickerBarSyncState(ticker="UPTODATE", multiplier=1, timespan="day", synced_through=yesterday))
    session.add(
        TickerBarSyncState(
            ticker="RESUME", multiplier=1, timespan="day", synced_through=yesterday - dt.timedelta(days=5)
        )
    )
    session.commit()

    lock = threading.Lock()
    requested_ranges: dict[str, tuple[str, str]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        parts = request.url.path.split("/")
        ticker, start, end = parts[4], parts[-2], parts[-1]
        with lock:
            requested_ranges[ticker] = (start, end)
        return httpx.Response(200, json={"results": [_bar(dt.date(2024, 1, 1), 1.0)]})

    try:
        results = sync_bars_nightly(
            session, backfill_days=730, client_factory=lambda: _client_with_handler(handler)
        )
    finally:
        session.close()

    assert "UPTODATE" not in results  # already synced through yesterday - skipped entirely
    assert requested_ranges["RESUME"] == ((yesterday - dt.timedelta(days=4)).isoformat(), yesterday.isoformat())
    assert requested_ranges["NEVER"] == ((yesterday - dt.timedelta(days=730)).isoformat(), yesterday.isoformat())


def test_sync_bars_nightly_cancel_stops_the_run_and_raises_job_cancelled():
    session = SessionLocal()
    session.add_all([Ticker(ticker="AAA"), Ticker(ticker="BBB")])
    session.commit()

    control = JobControl()
    control.request_cancel()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [_bar(dt.date(2024, 1, 1), 1.0)]})

    try:
        with pytest.raises(JobCancelled):
            sync_bars_nightly(session, client_factory=lambda: _client_with_handler(handler), control=control)

        assert session.query(OhlcBar).count() == 0
    finally:
        session.close()
