import datetime as dt
import os

import pytest

import jobs.train_lstm_holdout as job_module
from db.models import JobRun, LstmModelVersion, OhlcBar, Ticker
from db.session import SessionLocal, init_db
from jobs.lstm_common import load_checkpoint
from jobs.train_lstm_holdout import train_lstm_holdout


@pytest.fixture(autouse=True)
def _clean_db(monkeypatch, tmp_path):
    # Checkpoints are real files on disk (see jobs/train_lstm_holdout.py's MODEL_DIR) -
    # redirected to pytest's tmp_path so a test run never writes into the repo's real
    # backend-v2/data/lstm_models/, same pattern tests/test_sync_etf_constituents.py
    # uses for its own DOWNLOAD_DIR.
    monkeypatch.setattr(job_module, "MODEL_DIR", str(tmp_path / "lstm_models"))
    init_db()
    session = SessionLocal()
    session.query(LstmModelVersion).delete()
    session.query(OhlcBar).delete()
    session.query(Ticker).delete()
    session.query(JobRun).delete()
    session.commit()
    session.close()
    yield


def _seed_bars(session, ticker: str, closes: list[float], start: dt.date) -> None:
    session.add(Ticker(ticker=ticker))
    for i, close in enumerate(closes):
        session.add(
            OhlcBar(
                ticker=ticker,
                multiplier=1,
                timespan="day",
                timestamp=dt.datetime.combine(start + dt.timedelta(days=i), dt.time.min),
                open=close,
                high=close * 1.01,
                low=close * 0.99,
                close=close,
                volume=1_000_000 + i * 100,
                pcnt_increase=0.1 * ((-1) ** i),
            )
        )
    session.commit()


def _make_job_run(session) -> int:
    run = JobRun(job_name="train-lstm-holdout", trigger="manual", status="in_progress", started_at=dt.datetime.utcnow())
    session.add(run)
    session.commit()
    return run.id


_CLOSES = [100 + ((-1) ** i) * (i % 7) + i * 0.3 for i in range(80)]


def test_trains_and_stores_model_version_with_loadable_checkpoint():
    session = SessionLocal()
    try:
        start = dt.date(2026, 1, 1)
        _seed_bars(session, "AAA", _CLOSES, start)
        end_date = start + dt.timedelta(days=len(_CLOSES) - 1)
        run_id = _make_job_run(session)

        version = train_lstm_holdout(
            session,
            run_id,
            train_start_date=start,
            train_end_date=end_date,
            epochs=1,
            lookback_days=5,
            batch_size=8,
        )

        assert version.id is not None
        assert version.training_method == "holdout"
        assert version.num_folds is None
        assert version.job_run_id == run_id
        assert 0.0 <= version.val_accuracy <= 1.0
        assert version.duration_seconds >= 0.0
        assert os.path.exists(version.model_path)

        # The stored checkpoint round-trips into a usable model.
        model = load_checkpoint(version.model_path)
        assert model is not None

        assert session.query(LstmModelVersion).count() == 1
    finally:
        session.close()


def test_start_date_not_before_end_date_raises():
    session = SessionLocal()
    try:
        run_id = _make_job_run(session)
        with pytest.raises(ValueError):
            train_lstm_holdout(
                session, run_id, train_start_date=dt.date(2026, 2, 1), train_end_date=dt.date(2026, 1, 1)
            )
    finally:
        session.close()


def test_no_qualifying_tickers_raises():
    session = SessionLocal()
    try:
        run_id = _make_job_run(session)
        with pytest.raises(ValueError):
            train_lstm_holdout(
                session,
                run_id,
                train_start_date=dt.date(2026, 1, 1),
                train_end_date=dt.date(2026, 3, 1),
                lookback_days=5,
            )
    finally:
        session.close()


def test_tickers_filter_scopes_training_data():
    session = SessionLocal()
    try:
        start = dt.date(2026, 1, 1)
        _seed_bars(session, "AAA", _CLOSES, start)
        _seed_bars(session, "BBB", _CLOSES, start)
        end_date = start + dt.timedelta(days=len(_CLOSES) - 1)
        run_id = _make_job_run(session)

        version = train_lstm_holdout(
            session,
            run_id,
            train_start_date=start,
            train_end_date=end_date,
            epochs=1,
            lookback_days=5,
            batch_size=8,
            tickers=["AAA"],
        )
        assert version.tickers == "AAA"
    finally:
        session.close()
