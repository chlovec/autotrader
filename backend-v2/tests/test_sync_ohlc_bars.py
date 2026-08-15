import datetime as dt

import httpx
import pytest

from data.client import DataClient
from db.models import JobRun, OhlcBar, Ticker, TickerType
from db.session import SessionLocal, init_db
from jobs.control import JobCancelled, JobControl
from jobs.sync_ohlc_bars import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    _select_tickers_to_sync,
    sync_ohlc_bars,
    sync_ohlc_bars_worker,
)


@pytest.fixture(autouse=True)
def _clean_db():
    init_db()
    session = SessionLocal()
    session.query(OhlcBar).delete()
    session.query(Ticker).delete()
    session.query(TickerType).delete()
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


def test_select_tickers_to_sync_defaults_never_synced_to_start_date():
    session = SessionLocal()
    session.add(TickerType(code="CS", asset_class="stocks", locale="us", rank=1))
    session.add(Ticker(ticker="AAA", type="CS", last_ohlc_sync_date=None))
    session.commit()

    try:
        selected = _select_tickers_to_sync(session, dt.date(2024, 1, 1), dt.date(2024, 6, 1), 1000)
    finally:
        session.close()

    assert selected == {"AAA": dt.date(2024, 1, 1)}


def test_select_tickers_to_sync_resumes_from_last_sync_date_plus_one_day():
    session = SessionLocal()
    session.add(TickerType(code="CS", asset_class="stocks", locale="us", rank=1))
    session.add(Ticker(ticker="AAA", type="CS", last_ohlc_sync_date=dt.date(2024, 3, 1)))
    session.commit()

    try:
        selected = _select_tickers_to_sync(session, dt.date(2024, 1, 1), dt.date(2024, 6, 1), 1000)
    finally:
        session.close()

    # last_ohlc_sync_date + 1 day is later than the requested start_date, so it wins.
    assert selected == {"AAA": dt.date(2024, 3, 2)}


def test_select_tickers_to_sync_uses_requested_start_date_when_it_is_later():
    session = SessionLocal()
    session.add(TickerType(code="CS", asset_class="stocks", locale="us", rank=1))
    session.add(Ticker(ticker="AAA", type="CS", last_ohlc_sync_date=dt.date(2024, 1, 1)))
    session.commit()

    try:
        selected = _select_tickers_to_sync(session, dt.date(2024, 3, 1), dt.date(2024, 6, 1), 1000)
    finally:
        session.close()

    # last_ohlc_sync_date + 1 day (2024-01-02) is earlier than the requested start_date.
    assert selected == {"AAA": dt.date(2024, 3, 1)}


def test_select_tickers_to_sync_excludes_tickers_already_synced_through_end_date():
    session = SessionLocal()
    session.add(TickerType(code="CS", asset_class="stocks", locale="us", rank=1))
    session.add_all(
        [
            Ticker(ticker="CAUGHT_UP", type="CS", last_ohlc_sync_date=dt.date(2024, 6, 1)),
            Ticker(ticker="BEHIND", type="CS", last_ohlc_sync_date=dt.date(2024, 5, 1)),
        ]
    )
    session.commit()

    try:
        selected = _select_tickers_to_sync(session, dt.date(2024, 1, 1), dt.date(2024, 6, 1), 1000)
    finally:
        session.close()

    assert "CAUGHT_UP" not in selected
    assert "BEHIND" in selected


def test_select_tickers_to_sync_excludes_tickers_with_no_matching_ticker_type():
    session = SessionLocal()
    session.add(TickerType(code="CS", asset_class="stocks", locale="us", rank=1))
    session.add(Ticker(ticker="ORPHAN", type="UNKNOWN_TYPE"))
    session.commit()

    try:
        selected = _select_tickers_to_sync(session, dt.date(2024, 1, 1), dt.date(2024, 6, 1), 1000)
    finally:
        session.close()

    assert selected == {}


def test_select_tickers_to_sync_orders_by_ticker_type_rank_then_ticker_and_respects_limit():
    session = SessionLocal()
    session.add_all(
        [
            TickerType(code="ETF", asset_class="etf", locale="us", rank=2),
            TickerType(code="CS", asset_class="stocks", locale="us", rank=1),
        ]
    )
    session.add_all(
        [
            Ticker(ticker="ZZZ", type="CS"),
            Ticker(ticker="AAA", type="CS"),
            Ticker(ticker="BBB", type="ETF"),
        ]
    )
    session.commit()

    try:
        selected_all = _select_tickers_to_sync(session, dt.date(2024, 1, 1), dt.date(2024, 6, 1), 1000)
        selected_limited = _select_tickers_to_sync(session, dt.date(2024, 1, 1), dt.date(2024, 6, 1), 2)
    finally:
        session.close()

    assert list(selected_all.keys()) == ["AAA", "ZZZ", "BBB"]  # rank 1 (CS) before rank 2 (ETF), ticker asc within
    assert list(selected_limited.keys()) == ["AAA", "ZZZ"]


def test_sync_ohlc_bars_worker_fetches_bars_and_stamps_last_ohlc_sync_date():
    session = SessionLocal()
    session.add(Ticker(ticker="AAA"))
    session.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [_bar(dt.date(2024, 1, 1), 10.0)]})

    try:
        fetched = sync_ohlc_bars_worker(
            "AAA", dt.date(2024, 1, 1), dt.date(2024, 1, 2), client_factory=lambda: _client_with_handler(handler)
        )
        assert fetched == 1
    finally:
        session.close()

    # The worker commits through its own SessionLocal(), independent of the session
    # above (same pattern as jobs/sync_bars.py's worker) - re-query with a fresh
    # session rather than the identity-mapped (and now stale) one used to insert AAA.
    session = SessionLocal()
    try:
        ticker = session.get(Ticker, "AAA")
        assert ticker.last_ohlc_sync_date == dt.date(2024, 1, 2)
        assert session.query(OhlcBar).filter_by(ticker="AAA").count() == 1
    finally:
        session.close()


def test_sync_ohlc_bars_worker_stamps_last_ohlc_sync_date_even_with_zero_bars_fetched():
    session = SessionLocal()
    session.add(Ticker(ticker="AAA"))
    session.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": []})

    try:
        fetched = sync_ohlc_bars_worker(
            "AAA", dt.date(2024, 1, 1), dt.date(2024, 1, 2), client_factory=lambda: _client_with_handler(handler)
        )
        assert fetched == 0
    finally:
        session.close()

    session = SessionLocal()
    try:
        ticker = session.get(Ticker, "AAA")
        assert ticker.last_ohlc_sync_date == dt.date(2024, 1, 2)
    finally:
        session.close()


def test_sync_ohlc_bars_worker_does_not_stamp_last_ohlc_sync_date_on_fetch_failure():
    session = SessionLocal()
    session.add(Ticker(ticker="AAA"))
    session.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    try:
        with pytest.raises(httpx.HTTPStatusError):
            sync_ohlc_bars_worker(
                "AAA", dt.date(2024, 1, 1), dt.date(2024, 1, 2), client_factory=lambda: _client_with_handler(handler)
            )
    finally:
        session.close()

    session = SessionLocal()
    try:
        ticker = session.get(Ticker, "AAA")
        assert ticker.last_ohlc_sync_date is None
    finally:
        session.close()


def test_sync_ohlc_bars_end_to_end_selects_fetches_and_stamps():
    session = SessionLocal()
    session.add(TickerType(code="CS", asset_class="stocks", locale="us", rank=1))
    session.add_all([Ticker(ticker="AAA", type="CS"), Ticker(ticker="BBB", type="CS")])
    session.commit()

    requested: dict[str, tuple[str, str]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        parts = request.url.path.split("/")
        ticker, start, end = parts[4], parts[-2], parts[-1]
        requested[ticker] = (start, end)
        return httpx.Response(200, json={"results": [_bar(dt.date(2024, 1, 1), 1.0)]})

    try:
        results = sync_ohlc_bars(
            session,
            start_date=dt.date(2024, 1, 1),
            end_date=dt.date(2024, 6, 1),
            client_factory=lambda: _client_with_handler(handler),
        )
    finally:
        session.close()

    assert results == {"AAA": 1, "BBB": 1}
    assert requested["AAA"] == ("2024-01-01", "2024-06-01")
    assert requested["BBB"] == ("2024-01-01", "2024-06-01")

    session = SessionLocal()
    try:
        assert session.get(Ticker, "AAA").last_ohlc_sync_date == dt.date(2024, 6, 1)
        assert session.get(Ticker, "BBB").last_ohlc_sync_date == dt.date(2024, 6, 1)
    finally:
        session.close()


def test_sync_ohlc_bars_rejects_end_date_after_today():
    session = SessionLocal()
    tomorrow = dt.datetime.now(dt.timezone.utc).date() + dt.timedelta(days=1)
    try:
        with pytest.raises(ValueError):
            sync_ohlc_bars(session, end_date=tomorrow)
    finally:
        session.close()


def test_sync_ohlc_bars_defaults_start_date_to_two_years_before_today_and_end_date_to_today():
    today = dt.datetime.now(dt.timezone.utc).date()
    session = SessionLocal()
    session.add(TickerType(code="CS", asset_class="stocks", locale="us", rank=1))
    session.add(Ticker(ticker="AAA", type="CS"))
    session.commit()

    requested: dict[str, tuple[str, str]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        parts = request.url.path.split("/")
        ticker, start, end = parts[4], parts[-2], parts[-1]
        requested[ticker] = (start, end)
        return httpx.Response(200, json={"results": []})

    try:
        sync_ohlc_bars(session, client_factory=lambda: _client_with_handler(handler))
    finally:
        session.close()

    assert requested["AAA"] == ((today - dt.timedelta(days=730)).isoformat(), today.isoformat())


def test_sync_ohlc_bars_limit_is_capped_at_max_limit():
    session = SessionLocal()
    session.add(TickerType(code="CS", asset_class="stocks", locale="us", rank=1))
    session.add(Ticker(ticker="AAA", type="CS"))
    session.commit()

    try:
        # limit far above MAX_LIMIT still succeeds - just gets clamped, not rejected.
        results = sync_ohlc_bars(
            session,
            start_date=dt.date(2024, 1, 1),
            end_date=dt.date(2024, 1, 2),
            limit=MAX_LIMIT + 5000,
            client_factory=lambda: _client_with_handler(lambda r: httpx.Response(200, json={"results": []})),
        )
    finally:
        session.close()

    assert results == {"AAA": 0}


def test_sync_ohlc_bars_default_limit_constant_matches_spec():
    assert DEFAULT_LIMIT == 8000
    assert MAX_LIMIT == 10_000


def test_sync_ohlc_bars_cancel_stops_the_run_and_raises_job_cancelled():
    session = SessionLocal()
    session.add(TickerType(code="CS", asset_class="stocks", locale="us", rank=1))
    session.add_all([Ticker(ticker="AAA", type="CS"), Ticker(ticker="BBB", type="CS")])
    session.commit()

    control = JobControl()
    control.request_cancel()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [_bar(dt.date(2024, 1, 1), 1.0)]})

    try:
        with pytest.raises(JobCancelled):
            sync_ohlc_bars(
                session,
                start_date=dt.date(2024, 1, 1),
                end_date=dt.date(2024, 1, 2),
                client_factory=lambda: _client_with_handler(handler),
                control=control,
            )
        assert session.query(OhlcBar).count() == 0
    finally:
        session.close()


def test_sync_ohlc_bars_isolates_a_failing_ticker():
    session = SessionLocal()
    session.add(TickerType(code="CS", asset_class="stocks", locale="us", rank=1))
    session.add_all([Ticker(ticker="AAA", type="CS"), Ticker(ticker="BBB", type="CS")])
    session.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        if "/ticker/AAA/" in request.url.path:
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(200, json={"results": [_bar(dt.date(2024, 1, 1), 1.0)]})

    try:
        results = sync_ohlc_bars(
            session,
            start_date=dt.date(2024, 1, 1),
            end_date=dt.date(2024, 1, 2),
            client_factory=lambda: _client_with_handler(handler),
        )
    finally:
        session.close()

    assert results == {"BBB": 1}  # AAA's failure didn't stop BBB or blow up the run

    session = SessionLocal()
    try:
        assert session.get(Ticker, "AAA").last_ohlc_sync_date is None
        assert session.get(Ticker, "BBB").last_ohlc_sync_date == dt.date(2024, 1, 2)
    finally:
        session.close()


def test_sync_ohlc_bars_reports_progress_on_job_run():
    session = SessionLocal()
    session.add(TickerType(code="CS", asset_class="stocks", locale="us", rank=1))
    session.add_all([Ticker(ticker="AAA", type="CS"), Ticker(ticker="BBB", type="CS")])
    run = JobRun(job_name="sync-ohlc-bars", trigger="manual", status="in_progress", started_at=dt.datetime.utcnow())
    session.add(run)
    session.commit()
    run_id = run.id

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [_bar(dt.date(2024, 1, 1), 1.0)]})

    try:
        results = sync_ohlc_bars(
            session,
            start_date=dt.date(2024, 1, 1),
            end_date=dt.date(2024, 1, 2),
            client_factory=lambda: _client_with_handler(handler),
            run_id=run_id,
        )

        assert results == {"AAA": 1, "BBB": 1}
        updated_run = session.get(JobRun, run_id)
        assert (updated_run.progress_completed, updated_run.progress_total) == (2, 2)
    finally:
        session.close()
