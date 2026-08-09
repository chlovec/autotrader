import datetime as dt
import threading
import time

import httpx
import pytest

from data.client import DataClient
from db.models import JobRun, OhlcBar, Ticker
from db.session import SessionLocal, init_db
from jobs.control import JobCancelled, JobControl
from jobs.sync_bars import _last_trading_day_on_or_before, fetch_and_store_bars, sync_bars_manual, sync_bars_nightly


@pytest.fixture(autouse=True)
def _clean_db():
    init_db()
    session = SessionLocal()
    session.query(OhlcBar).delete()
    session.query(Ticker).delete()
    session.query(JobRun).delete()
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


async def test_fetch_and_store_bars_paginates_and_upserts():
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
        finally:
            session.close()

    assert requests[0].url.path == "/v2/aggs/ticker/AAA/range/1/day/2024-01-01/2024-01-02"
    assert requests[1].url.params["cursor"] == "page2"


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


def test_sync_bars_nightly_skips_up_to_date_and_resumes_from_last_bar():
    # end_date, not raw "yesterday" - matches sync_bars_nightly's own
    # _last_trading_day_on_or_before rollback, so this test is correct regardless of
    # which real-world weekday it happens to run on.
    yesterday = dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)
    end_date = _last_trading_day_on_or_before(yesterday)
    resume_last_bar = end_date - dt.timedelta(days=5)

    session = SessionLocal()
    session.add_all([Ticker(ticker="UPTODATE"), Ticker(ticker="RESUME"), Ticker(ticker="NEVER")])
    session.add(
        OhlcBar(
            ticker="UPTODATE",
            multiplier=1,
            timespan="day",
            timestamp=dt.datetime.combine(end_date, dt.time.min),
            close=1.0,
        )
    )
    session.add(
        OhlcBar(
            ticker="RESUME",
            multiplier=1,
            timespan="day",
            timestamp=dt.datetime.combine(resume_last_bar, dt.time.min),
            close=1.0,
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

    assert "UPTODATE" not in results  # last bar is already end_date - skipped entirely
    assert requested_ranges["RESUME"] == ((resume_last_bar + dt.timedelta(days=1)).isoformat(), end_date.isoformat())
    assert requested_ranges["NEVER"] == ((end_date - dt.timedelta(days=730)).isoformat(), end_date.isoformat())


def test_sync_bars_nightly_retries_a_ticker_that_gets_no_new_bars_back():
    """The bug this design replaces: a ticker massive.com returned nothing for used to
    get its cursor silently advanced anyway, so it was never retried again even though
    its real ohlc_bars data stayed stale forever. Now there's no cursor to wrongly
    advance - start_date is recomputed from ohlc_bars itself every run, so a ticker
    with no fresh data keeps being retried run after run."""
    yesterday = dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)
    old_bar_date = yesterday - dt.timedelta(days=10)

    session = SessionLocal()
    session.add(Ticker(ticker="QUIET"))
    session.add(
        OhlcBar(
            ticker="QUIET",
            multiplier=1,
            timespan="day",
            timestamp=dt.datetime.combine(old_bar_date, dt.time.min),
            close=1.0,
        )
    )
    session.commit()

    def empty_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": []})

    try:
        results = sync_bars_nightly(
            session, tickers=["QUIET"], backfill_days=730, client_factory=lambda: _client_with_handler(empty_handler)
        )
        assert results == {"QUIET": 0}  # attempted, but nothing came back

        last_bar = session.query(OhlcBar).filter_by(ticker="QUIET").order_by(OhlcBar.timestamp.desc()).first()
        assert last_bar.timestamp.date() == old_bar_date  # still stale - unchanged

        # Run again - the old cursor-based design would have marked this "caught up"
        # after the first attempt and skipped it here.
        results_again = sync_bars_nightly(
            session, tickers=["QUIET"], backfill_days=730, client_factory=lambda: _client_with_handler(empty_handler)
        )
        assert results_again == {"QUIET": 0}
    finally:
        session.close()


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


def test_last_trading_day_on_or_before_rolls_weekends_back_to_friday():
    friday = dt.date(2026, 8, 7)
    saturday = dt.date(2026, 8, 8)
    sunday = dt.date(2026, 8, 9)
    monday = dt.date(2026, 8, 10)

    assert _last_trading_day_on_or_before(saturday) == friday
    assert _last_trading_day_on_or_before(sunday) == friday
    assert _last_trading_day_on_or_before(friday) == friday  # already a weekday - unchanged
    assert _last_trading_day_on_or_before(monday) == monday  # already a weekday - unchanged


def test_run_id_records_final_progress_on_job_run():
    session = SessionLocal()
    session.add_all([Ticker(ticker="AAA"), Ticker(ticker="BBB")])
    run = JobRun(job_name="bars", trigger="manual", status="in_progress", started_at=dt.datetime.utcnow())
    session.add(run)
    session.commit()
    run_id = run.id

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [_bar(dt.date(2024, 1, 1), 1.0)]})

    try:
        results = sync_bars_manual(
            session,
            dt.date(2024, 1, 1),
            dt.date(2024, 1, 1),
            tickers=["AAA", "BBB"],
            client_factory=lambda: _client_with_handler(handler),
            run_id=run_id,
        )

        assert results == {"AAA": 1, "BBB": 1}
        updated_run = session.get(JobRun, run_id)
        assert (updated_run.progress_completed, updated_run.progress_total) == (2, 2)
    finally:
        session.close()
