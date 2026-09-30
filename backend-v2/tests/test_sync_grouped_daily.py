import datetime as dt

import httpx
import pytest

from data.client import DataClient
from db.models import JobConfig, JobRun, OhlcBar, Ticker
from db.session import SessionLocal, init_db
from jobs.control import JobCancelled, JobControl
from jobs.sync_grouped_daily import (
    DEFAULT_LOOKBACK_DAYS,
    MAX_RETRIES,
    resolve_date_range,
    sync_grouped_daily,
    weekdays,
)


@pytest.fixture(autouse=True)
def _clean_db():
    init_db()
    session = SessionLocal()
    session.query(OhlcBar).delete()
    session.query(Ticker).delete()
    session.query(JobRun).delete()
    session.query(JobConfig).delete()
    session.commit()
    session.close()
    yield


def _ms(day: dt.date) -> int:
    return int(dt.datetime.combine(day, dt.time(), tzinfo=dt.timezone.utc).timestamp() * 1000)


def _bar(ticker: str, day: dt.date, open_: float = 10.0, close: float = 11.0) -> dict:
    return {"T": ticker, "o": open_, "h": 12.0, "l": 9.0, "c": close, "v": 1000.0, "vw": 10.5, "n": 7, "t": _ms(day)}


def _client_factory(
    requested: list[str], bars_by_day: dict[str, list[dict]], failing: dict[str, int] | None = None
):
    """`failing` maps a day to how many of its requests fail before it starts succeeding
    (a large number = always fails)."""

    def handler(request: httpx.Request) -> httpx.Response:
        day = request.url.path.rsplit("/", 1)[1]
        requested.append(day)
        if failing and failing.get(day, 0) > 0:
            failing[day] -= 1
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(200, json={"results": bars_by_day.get(day, [])})

    def factory() -> DataClient:
        client = DataClient(api_key="test-key")
        client._client = httpx.AsyncClient(
            base_url=client._client.base_url,
            headers=client._client.headers,
            transport=httpx.MockTransport(handler),
        )
        return client

    return factory


def test_resolve_date_range_defaults_to_the_week_ending_today():
    today = dt.date(2026, 9, 25)
    assert resolve_date_range(None, None, today) == (today - dt.timedelta(days=DEFAULT_LOOKBACK_DAYS), today)
    assert resolve_date_range(dt.date(2026, 9, 1), None, today) == (dt.date(2026, 9, 1), today)
    with pytest.raises(ValueError):
        resolve_date_range(dt.date(2026, 9, 26), dt.date(2026, 9, 25), today)


def test_weekdays_skips_weekends():
    # Fri 2026-09-18 .. Tue 2026-09-22
    assert weekdays(dt.date(2026, 9, 18), dt.date(2026, 9, 22)) == [
        dt.date(2026, 9, 18),
        dt.date(2026, 9, 21),
        dt.date(2026, 9, 22),
    ]


def test_one_request_per_weekday_storing_only_selected_non_forex_tickers():
    session = SessionLocal()
    session.add_all(
        [
            Ticker(ticker="AAA", type="CS", market="stocks"),
            Ticker(ticker="BBB", type="ETF", market="stocks"),
            Ticker(ticker="C:EURUSD", market="fx"),
        ]
    )
    session.commit()
    fri, mon = dt.date(2026, 9, 18), dt.date(2026, 9, 21)
    bars = {
        fri.isoformat(): [_bar("AAA", fri), _bar("BBB", fri), _bar("ZZZ", fri), _bar("C:EURUSD", fri)],
        mon.isoformat(): [_bar("AAA", mon, open_=20.0, close=21.0)],
    }
    requested: list[str] = []

    try:
        results = sync_grouped_daily(session, fri, mon, client_factory=_client_factory(requested, bars))
        assert requested == [fri.isoformat(), mon.isoformat()]  # the weekend isn't requested
        assert results.results == {fri: 2, mon: 1}
        assert results.failed == []
        assert results.retry_rounds == 0
        stored = session.query(OhlcBar.ticker, OhlcBar.timestamp).order_by(OhlcBar.timestamp, OhlcBar.ticker).all()
        assert stored == [
            ("AAA", dt.datetime(2026, 9, 18)),
            ("BBB", dt.datetime(2026, 9, 18)),
            ("AAA", dt.datetime(2026, 9, 21)),
        ]
        monday = session.get(OhlcBar, ("AAA", 1, "day", dt.datetime(2026, 9, 21)))
        assert monday.pcnt_increase == pytest.approx(5.0)
    finally:
        session.close()


def test_ticker_types_limit_what_is_stored():
    session = SessionLocal()
    session.add_all([Ticker(ticker="AAA", type="CS"), Ticker(ticker="BBB", type="ETF")])
    session.commit()
    day = dt.date(2026, 9, 18)
    bars = {day.isoformat(): [_bar("AAA", day), _bar("BBB", day)]}

    try:
        sync_grouped_daily(session, day, day, ticker_types=["ETF"], client_factory=_client_factory([], bars))
        assert [row.ticker for row in session.query(OhlcBar).all()] == ["BBB"]
    finally:
        session.close()


def test_existing_bar_is_overwritten():
    session = SessionLocal()
    session.add(Ticker(ticker="AAA", type="CS"))
    session.commit()
    day = dt.date(2026, 9, 18)

    try:
        sync_grouped_daily(session, day, day, client_factory=_client_factory([], {day.isoformat(): [_bar("AAA", day)]}))
        sync_grouped_daily(
            session, day, day, client_factory=_client_factory([], {day.isoformat(): [_bar("AAA", day, close=15.0)]})
        )
        session.expire_all()
        bars = session.query(OhlcBar).all()
        assert len(bars) == 1
        assert bars[0].close == 15.0
    finally:
        session.close()


def _two_day_session():
    session = SessionLocal()
    session.add(Ticker(ticker="AAA", type="CS"))
    session.commit()
    fri, mon = dt.date(2026, 9, 18), dt.date(2026, 9, 21)
    return session, fri, mon, {fri.isoformat(): [_bar("AAA", fri)], mon.isoformat(): [_bar("AAA", mon)]}


def test_failed_day_retried_after_the_main_pass():
    session, fri, mon, bars = _two_day_session()
    requested: list[str] = []

    try:
        result = sync_grouped_daily(
            session,
            fri,
            mon,
            client_factory=_client_factory(requested, bars, failing={fri.isoformat(): 1}),
            retry_base_delay_seconds=0,
        )
        # Friday fails, Monday still runs, then Friday is retried once and succeeds.
        assert requested == [fri.isoformat(), mon.isoformat(), fri.isoformat()]
        assert result.results == {fri: 1, mon: 1}
        assert result.failed == []
        assert result.retry_rounds == 1
    finally:
        session.close()


def test_gives_up_after_max_retries():
    session, fri, mon, bars = _two_day_session()
    requested: list[str] = []

    try:
        result = sync_grouped_daily(
            session,
            fri,
            mon,
            client_factory=_client_factory(requested, bars, failing={fri.isoformat(): 99}),
            retry_base_delay_seconds=0,
        )
        assert requested.count(fri.isoformat()) == 1 + MAX_RETRIES
        assert requested.count(mon.isoformat()) == 1
        assert result.results == {mon: 1}
        assert result.failed == [fri]
        assert result.retry_rounds == MAX_RETRIES
    finally:
        session.close()


def test_retry_delays_double_each_round(monkeypatch):
    session, fri, mon, bars = _two_day_session()
    waits: list[float] = []

    async def fake_wait(seconds, control):
        waits.append(seconds)

    monkeypatch.setattr("jobs.sync_grouped_daily._wait", fake_wait)
    try:
        sync_grouped_daily(
            session,
            fri,
            mon,
            client_factory=_client_factory([], bars, failing={fri.isoformat(): 99}),
            retry_base_delay_seconds=60,
        )
        assert waits == [60, 120, 240]
    finally:
        session.close()


def test_cancel_during_a_retry_wait_stops_the_run():
    session, fri, mon, bars = _two_day_session()
    control = JobControl()
    requested: list[str] = []
    factory = _client_factory(requested, bars, failing={fri.isoformat(): 99})

    def cancelling_factory():
        client = factory()
        original_get = client.get

        async def get(path, params=None):
            try:
                return await original_get(path, params=params)
            finally:
                if len(requested) == 2:  # main pass done
                    control.request_cancel()

        client.get = get
        return client

    try:
        with pytest.raises(JobCancelled):
            sync_grouped_daily(
                session, fri, mon, client_factory=cancelling_factory, control=control, retry_base_delay_seconds=60
            )
        assert len(requested) == 2  # no retry request went out
    finally:
        session.close()


def test_progress_is_reported_per_day():
    session = SessionLocal()
    run = JobRun(
        job_name="sync-grouped-daily", trigger="manual", status="in_progress", started_at=dt.datetime.utcnow()
    )
    session.add(run)
    session.commit()
    fri, mon = dt.date(2026, 9, 18), dt.date(2026, 9, 21)

    try:
        sync_grouped_daily(session, fri, mon, client_factory=_client_factory([], {}), run_id=run.id)
        session.expire_all()
        saved = session.get(JobRun, run.id)
        assert (saved.progress_completed, saved.progress_total) == (2, 2)
    finally:
        session.close()
