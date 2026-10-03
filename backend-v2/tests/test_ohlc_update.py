import datetime as dt

import httpx
import pytest
from sqlalchemy import update

from data.client import DataClient
from db.models import JobConfig, JobRun, OhlcBar, Ticker
from db.session import SessionLocal, init_db
from jobs.ohlc_update import (
    DEFAULT_LOOKBACK_DAYS,
    is_paused,
    resolve_date_range,
    resume_at,
    sync_ohlc_update_batch,
)
from jobs.registry import OHLC_UPDATE_JOB


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


def _client_factory(requested: list[str], failing: set[str] | None = None):
    """`failing` is read at request time, so a test can clear it between runs to let a
    previously failing ticker succeed on retry."""

    def handler(request: httpx.Request) -> httpx.Response:
        ticker = request.url.path.split("/ticker/")[1].split("/")[0]
        requested.append(ticker)
        if failing and ticker in failing:
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(200, json={"results": []})

    def factory() -> DataClient:
        client = DataClient(api_key="test-key")
        client._client = httpx.AsyncClient(
            base_url=client._client.base_url,
            headers=client._client.headers,
            transport=httpx.MockTransport(handler),
        )
        return client

    return factory


def _config(session, **fields) -> JobConfig:
    fields = {
        "run_type": "auto",
        "schedule_interval_unit": "days",
        "schedule_interval_value": 1,
        "start_time": "06:30",
        **fields,
    }
    config = JobConfig(job_name=OHLC_UPDATE_JOB, **fields)
    session.add(config)
    session.commit()
    return config


def _run(session, config, requested, batch_size=None, ticker_types=None, tickers=None, failing=None):
    return sync_ohlc_update_batch(
        session,
        config,
        ticker_types,
        tickers,
        batch_size=batch_size,
        max_workers=1,
        client_factory=_client_factory(requested, failing),
    )


def _saved(session) -> JobConfig:
    session.expire_all()
    return session.get(JobConfig, OHLC_UPDATE_JOB)


def _elapse_retry_delay(session) -> None:
    """Pretend the pending retry delay has passed."""
    session.execute(
        update(JobConfig)
        .where(JobConfig.job_name == OHLC_UPDATE_JOB)
        .values(ohlc_update_next_run_at=dt.datetime.utcnow() - dt.timedelta(seconds=1))
    )
    session.commit()


def test_resolve_date_range_defaults_to_two_years_ending_today():
    today = dt.date(2026, 9, 25)
    start, end = resolve_date_range(None, None, today)
    assert end == today
    assert start == today - dt.timedelta(days=DEFAULT_LOOKBACK_DAYS)


def test_resolve_date_range_keeps_given_dates_and_defaults_the_missing_one():
    assert resolve_date_range(dt.date(2026, 1, 1), dt.date(2026, 2, 1), dt.date(2026, 9, 25)) == (
        dt.date(2026, 1, 1),
        dt.date(2026, 2, 1),
    )
    # Start given, no end -> ends today.
    assert resolve_date_range(dt.date(2026, 1, 1), None, dt.date(2026, 9, 25)) == (
        dt.date(2026, 1, 1),
        dt.date(2026, 9, 25),
    )
    # End given, no start -> 2 years before that end.
    start, end = resolve_date_range(None, dt.date(2026, 2, 1), dt.date(2026, 9, 25))
    assert end == dt.date(2026, 2, 1)
    assert start == end - dt.timedelta(days=DEFAULT_LOOKBACK_DAYS)


def test_resolve_date_range_rejects_start_after_end():
    with pytest.raises(ValueError):
        resolve_date_range(dt.date(2026, 3, 1), dt.date(2026, 2, 1))


def test_resume_at_is_next_day_at_start_time():
    session = SessionLocal()
    try:
        config = _config(session)
        assert resume_at(config) is None
        assert not is_paused(config)

        config.ohlc_update_completed_at = dt.datetime(2026, 9, 25, 23, 50)
        assert resume_at(config) == dt.datetime(2026, 9, 26, 6, 30)
        assert is_paused(config, dt.datetime(2026, 9, 26, 6, 29))
        assert not is_paused(config, dt.datetime(2026, 9, 26, 6, 30))
    finally:
        session.close()


def test_batches_walk_all_tickers_alphabetically_then_complete():
    session = SessionLocal()
    session.add_all([Ticker(ticker=t, type="CS") for t in ["CCC", "AAA", "EEE", "BBB", "DDD"]])
    session.commit()
    config = _config(session)
    requested: list[str] = []

    try:
        first = _run(session, config, requested, batch_size=2)
        assert sorted(requested) == ["AAA", "BBB"]
        assert not first.cycle_complete
        assert _saved(session).ohlc_update_cursor == "BBB"

        requested.clear()
        second = _run(session, config, requested, batch_size=2)
        assert sorted(requested) == ["CCC", "DDD"]
        assert not second.cycle_complete

        requested.clear()
        third = _run(session, config, requested, batch_size=2)
        assert requested == ["EEE"]
        assert third.cycle_complete
        saved = _saved(session)
        assert saved.ohlc_update_cursor is None
        assert saved.ohlc_update_completed_at is not None
    finally:
        session.close()


def test_batch_exactly_filling_the_last_page_completes_the_cycle():
    session = SessionLocal()
    session.add_all([Ticker(ticker=t, type="CS") for t in ["AAA", "BBB"]])
    session.commit()
    config = _config(session)

    try:
        result = _run(session, config, [], batch_size=2)
        assert result.cycle_complete
    finally:
        session.close()


def test_completed_cycle_does_nothing_until_next_day_at_start_time_then_restarts():
    session = SessionLocal()
    session.add_all([Ticker(ticker=t, type="CS") for t in ["AAA", "BBB"]])
    session.commit()
    config = _config(session, ohlc_update_completed_at=dt.datetime.utcnow())
    requested: list[str] = []

    try:
        paused = _run(session, config, requested, batch_size=5000)
        assert paused.cycle_complete
        assert requested == []

        # Backdate the completion far enough that the next-day start time has passed.
        config.ohlc_update_completed_at = dt.datetime.utcnow() - dt.timedelta(days=2)
        session.commit()
        resumed = _run(session, config, requested, batch_size=5000)
        assert sorted(requested) == ["AAA", "BBB"]
        assert resumed.cycle_complete
    finally:
        session.close()


def test_ticker_types_limit_the_selection():
    session = SessionLocal()
    session.add_all([Ticker(ticker="AAA", type="ETF"), Ticker(ticker="BBB", type="CS"), Ticker(ticker="CCC", type="ETF")])
    session.commit()
    config = _config(session)
    requested: list[str] = []

    try:
        _run(session, config, requested, batch_size=5000, ticker_types=["ETF"])
        assert sorted(requested) == ["AAA", "CCC"]
    finally:
        session.close()


def test_forex_tickers_are_excluded_from_the_tickers_table_selection():
    session = SessionLocal()
    session.add_all(
        [
            Ticker(ticker="AAA", type="CS", market="stocks"),
            Ticker(ticker="BBB", type="CS", market=None),
            Ticker(ticker="C:EURUSD", market="fx"),
        ]
    )
    session.commit()
    config = _config(session)
    requested: list[str] = []

    try:
        _run(session, config, requested, batch_size=5000)
        assert sorted(requested) == ["AAA", "BBB"]
    finally:
        session.close()


def test_explicit_tickers_are_batched_alphabetically():
    session = SessionLocal()
    config = _config(session)
    requested: list[str] = []

    try:
        result = _run(session, config, requested, batch_size=2, tickers=["CCC", "AAA", "BBB"])
        assert sorted(requested) == ["AAA", "BBB"]
        assert not result.cycle_complete
    finally:
        session.close()


def test_empty_selection_completes_the_cycle_immediately():
    session = SessionLocal()
    config = _config(session)

    try:
        result = _run(session, config, [], batch_size=5000)
        assert result.cycle_complete
        assert _saved(session).ohlc_update_completed_at is not None
    finally:
        session.close()


def test_failed_tickers_are_retried_after_double_the_interval():
    session = SessionLocal()
    session.add_all([Ticker(ticker=t, type="CS") for t in ["AAA", "BBB", "CCC"]])
    session.commit()
    config = _config(session, schedule_interval_unit="minutes", schedule_interval_value=5)
    failing = {"BBB"}
    requested: list[str] = []

    try:
        first = _run(session, config, requested, failing=failing)
        assert sorted(requested) == ["AAA", "BBB", "CCC"]
        assert first.failed == ["BBB"]
        assert not first.cycle_complete
        saved = _saved(session)
        assert saved.ohlc_update_completed_at is None
        assert saved.ohlc_update_retry_round == 1
        assert saved.ohlc_update_retry_tickers == '["BBB"]'
        delay = saved.ohlc_update_next_run_at - dt.datetime.utcnow()
        assert abs(delay - dt.timedelta(minutes=10)) < dt.timedelta(seconds=5)

        # Still inside the delay: nothing runs.
        requested.clear()
        assert is_paused(saved)
        waiting = _run(session, saved, requested, failing=failing)
        assert requested == []
        assert not waiting.cycle_complete

        # Delay over: only the failed ticker is retried, and this time it works.
        _elapse_retry_delay(session)
        failing.clear()
        retry = _run(session, _saved(session), requested, failing=failing)
        assert requested == ["BBB"]
        assert retry.cycle_complete
        saved = _saved(session)
        assert saved.ohlc_update_completed_at is not None
        assert saved.ohlc_update_retry_round == 0
        assert saved.ohlc_update_retry_tickers is None
    finally:
        session.close()


def test_retry_delay_doubles_each_round_and_stops_after_three_retries():
    session = SessionLocal()
    session.add(Ticker(ticker="AAA", type="CS"))
    session.commit()
    config = _config(session, schedule_interval_unit="minutes", schedule_interval_value=5)
    failing = {"AAA"}
    requested: list[str] = []

    try:
        _run(session, config, requested, failing=failing)
        for expected_minutes, expected_round in [(10, 1), (20, 2), (40, 3)]:
            saved = _saved(session)
            assert saved.ohlc_update_retry_round == expected_round
            delay = saved.ohlc_update_next_run_at - dt.datetime.utcnow()
            assert abs(delay - dt.timedelta(minutes=expected_minutes)) < dt.timedelta(seconds=5)
            _elapse_retry_delay(session)
            result = _run(session, _saved(session), requested, failing=failing)

        # The third retry failed too - no fourth one; the cycle ends until tomorrow.
        assert requested == ["AAA"] * 4
        assert result.cycle_complete
        assert "giving up" in result.note
        assert _saved(session).ohlc_update_completed_at is not None
    finally:
        session.close()


def test_retry_pass_is_batched_like_the_main_pass():
    session = SessionLocal()
    session.add_all([Ticker(ticker=t, type="CS") for t in ["AAA", "BBB", "CCC"]])
    session.commit()
    config = _config(session)
    failing = {"AAA", "BBB", "CCC"}
    requested: list[str] = []

    try:
        _run(session, config, requested, batch_size=5000, failing=failing)
        assert _saved(session).ohlc_update_retry_round == 1
        _elapse_retry_delay(session)

        requested.clear()
        failing.clear()
        first = _run(session, _saved(session), requested, batch_size=2, failing=failing)
        assert sorted(requested) == ["AAA", "BBB"]
        assert not first.cycle_complete
        saved = _saved(session)
        # Later batches of the same retry pass follow the normal interval, no extra delay.
        assert saved.ohlc_update_next_run_at is None
        assert saved.ohlc_update_cursor == "BBB"

        requested.clear()
        second = _run(session, saved, requested, batch_size=2, failing=failing)
        assert requested == ["CCC"]
        assert second.cycle_complete
    finally:
        session.close()


def test_batch_size_comes_from_the_config_when_not_given():
    session = SessionLocal()
    session.add_all([Ticker(ticker=t, type="CS") for t in ["AAA", "BBB", "CCC"]])
    session.commit()
    config = _config(session, ohlc_update_batch_size=1)
    requested: list[str] = []

    try:
        result = _run(session, config, requested)
        assert requested == ["AAA"]
        assert not result.cycle_complete
    finally:
        session.close()
