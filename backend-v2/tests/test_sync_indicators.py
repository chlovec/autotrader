import datetime as dt
import threading
import time

import httpx
import pytest

from data.client import DataClient
from db.models import JobRun, TechnicalIndicator, Ticker
from db.session import SessionLocal, init_db
from jobs.control import JobCancelled, JobControl
from jobs.sync_indicators import sync_indicator


@pytest.fixture(autouse=True)
def _clean_db():
    init_db()
    session = SessionLocal()
    session.query(TechnicalIndicator).delete()
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


def _sma_payload(values: list[dict]) -> dict:
    return {"status": "OK", "results": {"underlying": {}, "values": values}}


def test_fetches_and_upserts_explicit_tickers():
    session = SessionLocal()
    session.add_all([Ticker(ticker="AAA"), Ticker(ticker="BBB")])
    session.commit()

    lock = threading.Lock()
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        with lock:
            paths.append(request.url.path)
        return httpx.Response(200, json=_sma_payload([{"timestamp": 1_700_000_000_000, "value": 10.5}]))

    try:
        fetched = sync_indicator(
            "sma", session, tickers=["AAA", "BBB"], client_factory=lambda: _client_with_handler(handler)
        )

        assert fetched == 2
        rows = {row.ticker: row for row in session.query(TechnicalIndicator).all()}
        assert set(rows) == {"AAA", "BBB"}
        assert rows["AAA"].indicator == "sma"
        assert rows["AAA"].value == 10.5
        assert rows["AAA"].signal is None
    finally:
        session.close()

    assert sorted(paths) == ["/v1/indicators/sma/AAA", "/v1/indicators/sma/BBB"]


def test_ticker_types_resolve_from_tickers_table():
    session = SessionLocal()
    session.add_all([Ticker(ticker="AAA", type="CS"), Ticker(ticker="BBB", type="ETF")])
    session.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_sma_payload([{"timestamp": 1_700_000_000_000, "value": 1.0}]))

    try:
        fetched = sync_indicator(
            "ema", session, ticker_types=["CS"], client_factory=lambda: _client_with_handler(handler)
        )

        assert fetched == 1
        assert [row.ticker for row in session.query(TechnicalIndicator).all()] == ["AAA"]
    finally:
        session.close()


def test_macd_stores_signal_and_histogram():
    session = SessionLocal()
    session.add(Ticker(ticker="AAA"))
    session.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_sma_payload([{"timestamp": 1_700_000_000_000, "value": 1.5, "signal": 1.2, "histogram": 0.3}]),
        )

    try:
        fetched = sync_indicator("macd", session, tickers=["AAA"], client_factory=lambda: _client_with_handler(handler))

        assert fetched == 1
        row = session.query(TechnicalIndicator).one()
        assert row.indicator == "macd"
        assert row.value == 1.5
        assert row.signal == 1.2
        assert row.histogram == 0.3
    finally:
        session.close()


def test_rerun_upserts_rather_than_duplicating_same_timestamp():
    session = SessionLocal()
    session.add(Ticker(ticker="AAA"))
    session.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_sma_payload([{"timestamp": 1_700_000_000_000, "value": 99.0}]))

    try:
        sync_indicator("rsi", session, tickers=["AAA"], client_factory=lambda: _client_with_handler(handler))
        sync_indicator("rsi", session, tickers=["AAA"], client_factory=lambda: _client_with_handler(handler))

        rows = session.query(TechnicalIndicator).all()
        assert len(rows) == 1
        assert rows[0].value == 99.0
    finally:
        session.close()


def test_distinct_timestamps_accumulate_rather_than_overwrite():
    session = SessionLocal()
    session.add(Ticker(ticker="AAA"))
    session.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_sma_payload(
                [
                    {"timestamp": 1_700_000_000_000, "value": 1.0},
                    {"timestamp": 1_700_000_060_000, "value": 2.0},
                ]
            ),
        )

    try:
        fetched = sync_indicator("rsi", session, tickers=["AAA"], client_factory=lambda: _client_with_handler(handler))

        assert fetched == 2
        assert session.query(TechnicalIndicator).count() == 2
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
        return httpx.Response(200, json=_sma_payload([{"timestamp": 1_700_000_000_000, "value": 1.0}]))

    try:
        fetched = sync_indicator(
            "sma", session, tickers=["AAA", "BAD"], client_factory=lambda: _client_with_handler(handler)
        )

        assert fetched == 1
        assert [row.ticker for row in session.query(TechnicalIndicator).all()] == ["AAA"]
    finally:
        session.close()


def test_tickers_and_ticker_types_together_raises():
    session = SessionLocal()
    try:
        with pytest.raises(ValueError):
            sync_indicator("sma", session, ticker_types=["CS"], tickers=["AAA"])
    finally:
        session.close()


def test_cancel_stops_the_run_and_raises_job_cancelled():
    session = SessionLocal()
    session.add_all([Ticker(ticker="AAA"), Ticker(ticker="BBB"), Ticker(ticker="CCC")])
    session.commit()

    control = JobControl()
    control.request_cancel()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_sma_payload([{"timestamp": 1_700_000_000_000, "value": 1.0}]))

    try:
        with pytest.raises(JobCancelled):
            sync_indicator(
                "sma",
                session,
                tickers=["AAA", "BBB", "CCC"],
                client_factory=lambda: _client_with_handler(handler),
                control=control,
            )

        assert session.query(TechnicalIndicator).count() == 0
    finally:
        session.close()


def test_pause_blocks_a_worker_until_resumed():
    session = SessionLocal()
    session.add(Ticker(ticker="AAA"))
    session.commit()

    control = JobControl()
    control.request_pause()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_sma_payload([{"timestamp": 1_700_000_000_000, "value": 1.0}]))

    def resume_shortly():
        time.sleep(0.05)
        control.request_resume()

    resumer = threading.Thread(target=resume_shortly)
    resumer.start()
    try:
        fetched = sync_indicator(
            "sma", session, tickers=["AAA"], client_factory=lambda: _client_with_handler(handler), control=control
        )

        assert fetched == 1
        assert session.query(TechnicalIndicator).count() == 1
    finally:
        session.close()
        resumer.join()


def test_run_id_records_final_progress_on_job_run():
    session = SessionLocal()
    session.add_all([Ticker(ticker="AAA"), Ticker(ticker="BBB")])
    run = JobRun(job_name="rsi", trigger="manual", status="in_progress", started_at=dt.datetime.utcnow())
    session.add(run)
    session.commit()
    run_id = run.id

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_sma_payload([{"timestamp": 1_700_000_000_000, "value": 1.0}]))

    try:
        fetched = sync_indicator(
            "rsi", session, tickers=["AAA", "BBB"], client_factory=lambda: _client_with_handler(handler), run_id=run_id
        )

        assert fetched == 2
        updated_run = session.get(JobRun, run_id)
        assert (updated_run.progress_completed, updated_run.progress_total) == (2, 2)
    finally:
        session.close()
