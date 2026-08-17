import datetime as dt

import pytest

import jobs.train_lstm_holdout as holdout_job_module
import jobs.train_lstm_walkforward as walkforward_job_module
from db.models import JobRun, LstmInference, LstmModelVersion, OhlcBar, Ticker
from db.session import SessionLocal, init_db
from jobs.predict_lstm_market_state import compute_lstm_market_state_predictions
from jobs.predict_market_state import STATE_LABELS, _bucket_states
from jobs.train_lstm_holdout import train_lstm_holdout
from jobs.train_lstm_walkforward import train_lstm_walkforward


@pytest.fixture(autouse=True)
def _clean_db(monkeypatch, tmp_path):
    # Same MODEL_DIR isolation reasoning as tests/test_train_lstm_holdout.py - this
    # file trains real models (via _train_holdout_model/_train_walkforward_model below)
    # to predict against.
    monkeypatch.setattr(holdout_job_module, "MODEL_DIR", str(tmp_path / "lstm_models"))
    monkeypatch.setattr(walkforward_job_module, "MODEL_DIR", str(tmp_path / "lstm_models"))
    init_db()
    session = SessionLocal()
    session.query(LstmInference).delete()
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


def _make_job_run(session, job_name: str) -> int:
    run = JobRun(job_name=job_name, trigger="manual", status="in_progress", started_at=dt.datetime.utcnow())
    session.add(run)
    session.commit()
    return run.id


_CLOSES = [100 + ((-1) ** i) * (i % 7) + i * 0.3 for i in range(120)]


def _train_holdout_model(session, ticker: str = "AAA") -> LstmModelVersion:
    start = dt.date(2026, 1, 1)
    if session.get(Ticker, ticker) is None:
        _seed_bars(session, ticker, _CLOSES, start)
    end_date = start + dt.timedelta(days=len(_CLOSES) - 1)
    run_id = _make_job_run(session, "train-lstm-holdout")
    return train_lstm_holdout(
        session, run_id, train_start_date=start, train_end_date=end_date, epochs=1, lookback_days=5, batch_size=8
    )


def _train_walkforward_model(session, ticker: str = "AAA") -> LstmModelVersion:
    start = dt.date(2026, 1, 1)
    if session.get(Ticker, ticker) is None:
        _seed_bars(session, ticker, _CLOSES, start)
    end_date = start + dt.timedelta(days=len(_CLOSES) - 1)
    run_id = _make_job_run(session, "train-lstm-walkforward")
    version, _fold_lines = train_lstm_walkforward(
        session,
        run_id,
        train_start_date=start,
        train_end_date=end_date,
        epochs=1,
        lookback_days=5,
        batch_size=8,
        num_folds=2,
    )
    return version


def test_stores_prediction_with_valid_probabilities():
    session = SessionLocal()
    try:
        version = _train_holdout_model(session)
        prediction_date = dt.date(2026, 1, 1) + dt.timedelta(days=len(_CLOSES))

        stored = compute_lstm_market_state_predictions(session, "holdout", prediction_date=prediction_date)
        assert stored == 1

        row = session.query(LstmInference).filter_by(ticker="AAA").one()
        assert row.predicted_date == prediction_date
        assert row.training_method == "holdout"
        assert row.current_state in STATE_LABELS
        assert row.predicted_state in STATE_LABELS
        assert row.model_version_id == version.id
        probs = [row.prob_strong_down, row.prob_down, row.prob_flat, row.prob_up, row.prob_strong_up]
        assert sum(probs) == pytest.approx(1.0, abs=1e-4)
        assert row.state_confidence == pytest.approx(max(probs))
        assert row.exit_price == pytest.approx(row.entry_price * (1 + row.expected_return))

        # exit_price_confidence is derived from this ticker's own historical return
        # spread for the predicted bucket, not from the softmax probabilities - same
        # formula shape as tests/test_predict_market_state.py's own independent
        # recomputation.
        returns = [_CLOSES[i] / _CLOSES[i - 1] - 1 for i in range(1, len(_CLOSES))]
        _buckets, _bucket_means, bucket_stds = _bucket_states(returns)
        predicted_index = STATE_LABELS.index(row.predicted_state)
        assert row.exit_price_confidence == pytest.approx(
            1 - bucket_stds[predicted_index] / (1 + row.expected_return)
        )
    finally:
        session.close()


def test_no_trained_model_of_that_flavor_raises():
    session = SessionLocal()
    try:
        with pytest.raises(ValueError):
            compute_lstm_market_state_predictions(session, "holdout")
    finally:
        session.close()


def test_walkforward_inference_does_not_fall_back_to_a_holdout_model():
    session = SessionLocal()
    try:
        _train_holdout_model(session)
        with pytest.raises(ValueError):
            compute_lstm_market_state_predictions(session, "walkforward")
    finally:
        session.close()


def test_explicit_model_version_override_must_match_training_method():
    session = SessionLocal()
    try:
        holdout_version = _train_holdout_model(session)
        with pytest.raises(ValueError):
            compute_lstm_market_state_predictions(session, "walkforward", model_version_id=holdout_version.id)
    finally:
        session.close()


def test_explicit_model_version_override_is_used():
    session = SessionLocal()
    try:
        first = _train_holdout_model(session, ticker="AAA")
        second = _train_holdout_model(session, ticker="BBB")
        prediction_date = dt.date(2026, 1, 1) + dt.timedelta(days=len(_CLOSES))

        compute_lstm_market_state_predictions(
            session, "holdout", prediction_date=prediction_date, model_version_id=first.id
        )
        rows = session.query(LstmInference).all()
        assert all(row.model_version_id == first.id for row in rows)
        assert second.id != first.id
    finally:
        session.close()


def test_rerun_same_prediction_date_upserts_rather_than_duplicating():
    session = SessionLocal()
    try:
        _train_holdout_model(session)
        prediction_date = dt.date(2026, 1, 1) + dt.timedelta(days=len(_CLOSES))
        compute_lstm_market_state_predictions(session, "holdout", prediction_date=prediction_date)
        compute_lstm_market_state_predictions(session, "holdout", prediction_date=prediction_date)
        assert session.query(LstmInference).count() == 1
    finally:
        session.close()


def test_both_flavors_coexist_for_the_same_ticker_and_date():
    """The whole point of splitting inference into two independent jobs: a holdout
    prediction and a walkforward prediction for the same (ticker, predicted_date) must
    both be storable, not overwrite each other."""
    session = SessionLocal()
    try:
        _train_holdout_model(session)
        _train_walkforward_model(session)
        prediction_date = dt.date(2026, 1, 1) + dt.timedelta(days=len(_CLOSES))

        compute_lstm_market_state_predictions(session, "holdout", prediction_date=prediction_date)
        compute_lstm_market_state_predictions(session, "walkforward", prediction_date=prediction_date)

        rows = session.query(LstmInference).filter_by(ticker="AAA", predicted_date=prediction_date).all()
        assert {row.training_method for row in rows} == {"holdout", "walkforward"}
    finally:
        session.close()
