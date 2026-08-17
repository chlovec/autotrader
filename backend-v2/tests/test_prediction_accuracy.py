import datetime as dt
import statistics

import pytest

from db.models import (
    JobRun,
    LstmInference,
    LstmModelVersion,
    MarketPrediction,
    MarketPredictionMonteCarlo,
    OhlcBar,
    PredictionAccuracy,
    Ticker,
)
from db.session import SessionLocal, init_db
from jobs.prediction_accuracy import compute_prediction_accuracy


@pytest.fixture(autouse=True)
def _clean_db():
    init_db()
    session = SessionLocal()
    session.query(PredictionAccuracy).delete()
    session.query(LstmInference).delete()
    session.query(LstmModelVersion).delete()
    session.query(JobRun).delete()
    session.query(MarketPredictionMonteCarlo).delete()
    session.query(MarketPrediction).delete()
    session.query(OhlcBar).delete()
    session.query(Ticker).delete()
    session.commit()
    session.close()
    yield


# 70 distinct, always-positive, oscillating-with-drift closes - enough spread for a
# non-degenerate stdev, same shape as other tests' own _CLOSES generators. Entry index
# 60 (the minimum DEFAULT_MIN_HISTORY_DAYS) and predicted index 61 leave exactly enough
# margin for an evaluable pair.
_CLOSES = [100 + ((-1) ** i) * (i % 7) + i * 0.3 for i in range(70)]
_START = dt.date(2026, 1, 1)
_DATES = [_START + dt.timedelta(days=i) for i in range(len(_CLOSES))]
_ENTRY_IDX = 60
_PREDICTED_IDX = 61
_PREDICTED_DATE = _DATES[_PREDICTED_IDX]
_ENTRY_PRICE = _CLOSES[_ENTRY_IDX]
_ACTUAL_EXIT_PRICE = _CLOSES[_PREDICTED_IDX]

_ALL_RETURNS = [_CLOSES[i] / _CLOSES[i - 1] - 1 for i in range(1, len(_CLOSES))]
_EXPECTED_RETURN_STD = statistics.stdev(_ALL_RETURNS[:_ENTRY_IDX])
_EXPECTED_PRICE_STD = _ENTRY_PRICE * _EXPECTED_RETURN_STD


def _seed_bars(session, ticker: str, closes: list[float] = _CLOSES) -> None:
    if session.get(Ticker, ticker) is None:
        session.add(Ticker(ticker=ticker, type="CS"))
    for date, close in zip(_DATES, closes, strict=False):
        session.add(
            OhlcBar(
                ticker=ticker,
                multiplier=1,
                timespan="day",
                timestamp=dt.datetime.combine(date, dt.time.min),
                close=close,
            )
        )
    session.commit()


def _markov(ticker: str, predicted_date: dt.date, exit_price: float) -> MarketPrediction:
    return MarketPrediction(
        ticker=ticker,
        predicted_date=predicted_date,
        current_state="flat",
        predicted_state="up",
        state_confidence=0.5,
        expected_return=(exit_price / _ENTRY_PRICE) - 1,
        entry_price=_ENTRY_PRICE,
        exit_price=exit_price,
        exit_price_confidence=0.5,
        entry_time="09:30:00",
        exit_time="16:00:00",
        history_days=60,
        computed_at=dt.datetime.utcnow(),
    )


def _mcmc(ticker: str, predicted_date: dt.date, exit_price: float) -> MarketPredictionMonteCarlo:
    return MarketPredictionMonteCarlo(
        ticker=ticker,
        predicted_date=predicted_date,
        current_state="flat",
        predicted_state="up",
        state_confidence=0.5,
        expected_return=(exit_price / _ENTRY_PRICE) - 1,
        entry_price=_ENTRY_PRICE,
        exit_price=exit_price,
        exit_price_mean=exit_price,
        exit_price_std=1.0,
        exit_price_confidence=0.5,
        exit_price_p10=exit_price - 1,
        exit_price_p50=exit_price,
        exit_price_p90=exit_price + 1,
        entry_time="09:30:00",
        exit_time="16:00:00",
        num_simulations=2000,
        history_days=60,
        computed_at=dt.datetime.utcnow(),
    )


def _lstm_model_version(session, training_method: str) -> int:
    run = JobRun(
        job_name=f"train-lstm-{training_method}", trigger="manual", status="completed", started_at=dt.datetime.utcnow()
    )
    session.add(run)
    session.commit()
    version = LstmModelVersion(
        training_method=training_method,
        job_run_id=run.id,
        trained_at=dt.datetime.utcnow(),
        train_start_date=_START,
        train_end_date=_DATES[-1],
        lookback_days=60,
        epochs=1,
        learning_rate=0.001,
        batch_size=8,
        num_folds=None,
        train_loss=0.1,
        val_loss=0.1,
        val_accuracy=0.5,
        duration_seconds=1.0,
        model_path="/dev/null",
    )
    session.add(version)
    session.commit()
    return version.id


def _lstm(ticker: str, predicted_date: dt.date, exit_price: float, training_method: str, model_version_id: int) -> LstmInference:
    return LstmInference(
        ticker=ticker,
        predicted_date=predicted_date,
        training_method=training_method,
        current_state="flat",
        predicted_state="up",
        state_confidence=0.5,
        prob_strong_down=0.1,
        prob_down=0.1,
        prob_flat=0.2,
        prob_up=0.3,
        prob_strong_up=0.3,
        expected_return=(exit_price / _ENTRY_PRICE) - 1,
        entry_price=_ENTRY_PRICE,
        exit_price=exit_price,
        exit_price_confidence=0.5,
        entry_time="09:30:00",
        exit_time="16:00:00",
        history_days=60,
        model_version_id=model_version_id,
        computed_at=dt.datetime.utcnow(),
    )


def test_scores_all_four_sources_against_actual():
    session = SessionLocal()
    try:
        _seed_bars(session, "AAA")
        holdout_version_id = _lstm_model_version(session, "holdout")
        walkforward_version_id = _lstm_model_version(session, "walkforward")

        # markov's error is 0.5 price_std off (a pass at the default 1.0 threshold);
        # mcmc's is 2.0 price_std off (a fail).
        markov_exit_price = _ACTUAL_EXIT_PRICE - 0.5 * _EXPECTED_PRICE_STD
        mcmc_exit_price = _ACTUAL_EXIT_PRICE - 2.0 * _EXPECTED_PRICE_STD
        session.add(_markov("AAA", _PREDICTED_DATE, markov_exit_price))
        session.add(_mcmc("AAA", _PREDICTED_DATE, mcmc_exit_price))
        session.add(_lstm("AAA", _PREDICTED_DATE, _ACTUAL_EXIT_PRICE, "holdout", holdout_version_id))
        session.add(_lstm("AAA", _PREDICTED_DATE, _ACTUAL_EXIT_PRICE, "walkforward", walkforward_version_id))
        session.commit()

        stored = compute_prediction_accuracy(session)
        assert stored == 1

        row = session.query(PredictionAccuracy).filter_by(ticker="AAA", predicted_date=_PREDICTED_DATE).one()
        assert row.actual_exit_price == pytest.approx(_ACTUAL_EXIT_PRICE)
        assert row.price_std == pytest.approx(_EXPECTED_PRICE_STD)
        assert row.history_days == _ENTRY_IDX
        assert row.pass_threshold_std == pytest.approx(1.0)

        assert row.markov_predicted_exit_price == pytest.approx(markov_exit_price)
        assert row.markov_error == pytest.approx(_ACTUAL_EXIT_PRICE - markov_exit_price)
        assert row.markov_error_std == pytest.approx(0.5)
        assert row.markov_passed is True

        assert row.mcmc_error_std == pytest.approx(2.0)
        assert row.mcmc_passed is False

        # LSTM predictions land exactly on the actual price - a perfect (0-std) hit.
        assert row.lstm_holdout_error_std == pytest.approx(0.0)
        assert row.lstm_holdout_passed is True
        assert row.lstm_walkforward_error_std == pytest.approx(0.0)
        assert row.lstm_walkforward_passed is True
    finally:
        session.close()


def test_missing_source_leaves_its_fields_null():
    session = SessionLocal()
    try:
        _seed_bars(session, "AAA")
        session.add(_markov("AAA", _PREDICTED_DATE, _ACTUAL_EXIT_PRICE))
        session.commit()

        compute_prediction_accuracy(session)

        row = session.query(PredictionAccuracy).filter_by(ticker="AAA", predicted_date=_PREDICTED_DATE).one()
        assert row.markov_passed is True
        for prefix in ("mcmc", "lstm_holdout", "lstm_walkforward"):
            assert getattr(row, f"{prefix}_predicted_exit_price") is None
            assert getattr(row, f"{prefix}_error") is None
            assert getattr(row, f"{prefix}_error_std") is None
            assert getattr(row, f"{prefix}_passed") is None
    finally:
        session.close()


def test_actual_not_yet_synced_is_skipped():
    session = SessionLocal()
    try:
        # Bars only through _PREDICTED_IDX - 1 - the actual close for _PREDICTED_DATE
        # hasn't synced yet.
        _seed_bars(session, "AAA", closes=_CLOSES[:_PREDICTED_IDX])
        session.add(_markov("AAA", _PREDICTED_DATE, _ACTUAL_EXIT_PRICE))
        session.commit()

        stored = compute_prediction_accuracy(session)
        assert stored == 0
        assert session.query(PredictionAccuracy).count() == 0
    finally:
        session.close()


def test_insufficient_history_before_entry_is_skipped():
    session = SessionLocal()
    try:
        _seed_bars(session, "AAA")
        # Predicted date far too early in the series for DEFAULT_MIN_HISTORY_DAYS of
        # prior history to exist.
        early_predicted_date = _DATES[10]
        session.add(_markov("AAA", early_predicted_date, _CLOSES[10]))
        session.commit()

        stored = compute_prediction_accuracy(session)
        assert stored == 0
    finally:
        session.close()


def test_pass_threshold_is_configurable():
    session = SessionLocal()
    try:
        _seed_bars(session, "AAA")
        # 1.5 price_std off - fails the default 1.0 threshold, passes a 2.0 threshold.
        exit_price = _ACTUAL_EXIT_PRICE - 1.5 * _EXPECTED_PRICE_STD
        session.add(_markov("AAA", _PREDICTED_DATE, exit_price))
        session.commit()

        compute_prediction_accuracy(session, pass_threshold_std=2.0)

        row = session.query(PredictionAccuracy).filter_by(ticker="AAA", predicted_date=_PREDICTED_DATE).one()
        assert row.pass_threshold_std == pytest.approx(2.0)
        assert row.markov_error_std == pytest.approx(1.5)
        assert row.markov_passed is True
    finally:
        session.close()


def test_tickers_filter_scopes_to_selected_tickers():
    session = SessionLocal()
    try:
        _seed_bars(session, "AAA")
        _seed_bars(session, "BBB")
        session.add(_markov("AAA", _PREDICTED_DATE, _ACTUAL_EXIT_PRICE))
        session.add(_markov("BBB", _PREDICTED_DATE, _ACTUAL_EXIT_PRICE))
        session.commit()

        stored = compute_prediction_accuracy(session, tickers=["AAA"])
        assert stored == 1
        assert session.query(PredictionAccuracy).filter_by(ticker="BBB").count() == 0
    finally:
        session.close()


def test_rejects_tickers_and_ticker_types_together():
    session = SessionLocal()
    try:
        with pytest.raises(ValueError):
            compute_prediction_accuracy(session, ticker_types=["CS"], tickers=["AAA"])
    finally:
        session.close()


def test_rerun_upserts_rather_than_duplicating():
    session = SessionLocal()
    try:
        _seed_bars(session, "AAA")
        session.add(_markov("AAA", _PREDICTED_DATE, _ACTUAL_EXIT_PRICE))
        session.commit()

        compute_prediction_accuracy(session)
        compute_prediction_accuracy(session)

        assert session.query(PredictionAccuracy).count() == 1
    finally:
        session.close()
