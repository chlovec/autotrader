import datetime as dt
import os

import pytest

import jobs.train_lstm_walkforward as job_module
from db.models import JobRun, LstmModelVersion, OhlcBar, Ticker
from db.session import SessionLocal, init_db
from jobs.train_lstm_walkforward import train_lstm_walkforward


@pytest.fixture(autouse=True)
def _clean_db(monkeypatch, tmp_path):
    # Same MODEL_DIR isolation reasoning as tests/test_train_lstm_holdout.py.
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
    run = JobRun(
        job_name="train-lstm-walkforward", trigger="manual", status="in_progress", started_at=dt.datetime.utcnow()
    )
    session.add(run)
    session.commit()
    return run.id


_CLOSES = [100 + ((-1) ** i) * (i % 7) + i * 0.3 for i in range(120)]


def test_trains_num_folds_and_stores_final_fold_as_model_version():
    session = SessionLocal()
    try:
        start = dt.date(2026, 1, 1)
        _seed_bars(session, "AAA", _CLOSES, start)
        end_date = start + dt.timedelta(days=len(_CLOSES) - 1)
        run_id = _make_job_run(session)

        version, fold_lines = train_lstm_walkforward(
            session,
            run_id,
            train_start_date=start,
            train_end_date=end_date,
            epochs=1,
            lookback_days=5,
            batch_size=8,
            num_folds=2,
        )

        assert len(fold_lines) == 2
        assert version.training_method == "walkforward"
        assert version.num_folds == 2
        assert version.job_run_id == run_id
        assert os.path.exists(version.model_path)
        assert session.query(LstmModelVersion).count() == 1
    finally:
        session.close()


def test_num_folds_must_be_at_least_one():
    session = SessionLocal()
    try:
        run_id = _make_job_run(session)
        with pytest.raises(ValueError):
            train_lstm_walkforward(
                session,
                run_id,
                train_start_date=dt.date(2026, 1, 1),
                train_end_date=dt.date(2026, 3, 1),
                num_folds=0,
            )
    finally:
        session.close()


def test_start_date_not_before_end_date_raises():
    session = SessionLocal()
    try:
        run_id = _make_job_run(session)
        with pytest.raises(ValueError):
            train_lstm_walkforward(
                session, run_id, train_start_date=dt.date(2026, 2, 1), train_end_date=dt.date(2026, 1, 1)
            )
    finally:
        session.close()
