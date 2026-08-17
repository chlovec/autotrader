import datetime as dt

import pytest

from db.models import OhlcBar, Ticker
from db.session import SessionLocal, init_db
from jobs.lstm_common import (
    NUM_FEATURES,
    build_inference_windows,
    build_training_sequences,
    split_by_date,
)
from jobs.predict_market_state import STATE_LABELS


@pytest.fixture(autouse=True)
def _clean_db():
    init_db()
    session = SessionLocal()
    session.query(OhlcBar).delete()
    session.query(Ticker).delete()
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


# 60 distinct, always-positive, oscillating-with-drift closes - plenty of spread for
# statistics.quantiles(n=5) to produce five non-degenerate buckets, same series shape
# jobs/predict_market_state_mcmc.py's own tests use, just longer.
_CLOSES = [100 + ((-1) ** i) * (i % 7) + i * 0.3 for i in range(60)]


def test_build_training_sequences_shapes_and_target_date_bounds():
    session = SessionLocal()
    try:
        start = dt.date(2026, 1, 1)
        _seed_bars(session, "AAA", _CLOSES, start)
        end_date = start + dt.timedelta(days=len(_CLOSES) - 1)
        batch = build_training_sequences(session, start, end_date, lookback_days=5)
        assert len(batch) > 0
        assert batch.features.shape == (len(batch), 5, NUM_FEATURES)
        assert batch.state_targets.shape == (len(batch),)
        assert batch.return_targets.shape == (len(batch),)
        assert all(t.item() in range(len(STATE_LABELS)) for t in batch.state_targets)
        assert all(start <= d <= end_date for d in batch.target_dates)
    finally:
        session.close()


def test_build_training_sequences_respects_narrower_date_range():
    session = SessionLocal()
    try:
        start = dt.date(2026, 1, 1)
        _seed_bars(session, "AAA", _CLOSES, start)
        narrow_start = start + dt.timedelta(days=40)
        narrow_end = start + dt.timedelta(days=50)
        batch = build_training_sequences(session, narrow_start, narrow_end, lookback_days=5)
        assert len(batch) > 0
        assert all(narrow_start <= d <= narrow_end for d in batch.target_dates)
    finally:
        session.close()


def test_build_training_sequences_skips_tickers_with_too_little_history():
    session = SessionLocal()
    try:
        start = dt.date(2026, 1, 1)
        _seed_bars(session, "THIN", _CLOSES[:5], start)
        end_date = start + dt.timedelta(days=4)
        batch = build_training_sequences(session, start, end_date, lookback_days=5)
        assert len(batch) == 0
    finally:
        session.close()


def test_build_training_sequences_start_after_end_raises():
    session = SessionLocal()
    try:
        with pytest.raises(ValueError):
            build_training_sequences(session, dt.date(2026, 2, 1), dt.date(2026, 1, 1), lookback_days=5)
    finally:
        session.close()


def test_split_by_date_partitions_without_overlap():
    session = SessionLocal()
    try:
        start = dt.date(2026, 1, 1)
        _seed_bars(session, "AAA", _CLOSES, start)
        end_date = start + dt.timedelta(days=len(_CLOSES) - 1)
        batch = build_training_sequences(session, start, end_date, lookback_days=5)
        split_date = start + dt.timedelta(days=45)
        before, on_or_after = split_by_date(batch, split_date)
        assert len(before) + len(on_or_after) == len(batch)
        assert all(d < split_date for d in before.target_dates)
        assert all(d >= split_date for d in on_or_after.target_dates)
    finally:
        session.close()


def test_build_inference_windows_uses_only_bars_strictly_before_prediction_date():
    session = SessionLocal()
    try:
        start = dt.date(2026, 1, 1)
        _seed_bars(session, "AAA", _CLOSES, start)
        prediction_date = start + dt.timedelta(days=len(_CLOSES))
        windows = build_inference_windows(session, prediction_date, lookback_days=5)
        assert len(windows) == 1
        window = windows[0]
        assert window.ticker == "AAA"
        assert window.entry_price == pytest.approx(_CLOSES[-1])
        assert window.current_state in STATE_LABELS
        assert window.features.shape == (5, NUM_FEATURES)

        # A wild bar landing exactly on prediction_date must not influence the window.
        session.add(
            OhlcBar(
                ticker="AAA",
                multiplier=1,
                timespan="day",
                timestamp=dt.datetime.combine(prediction_date, dt.time.min),
                open=999999.0,
                high=999999.0,
                low=999999.0,
                close=999999.0,
                volume=1.0,
                pcnt_increase=0.0,
            )
        )
        session.commit()
        after = build_inference_windows(session, prediction_date, lookback_days=5)
        assert after[0].entry_price == pytest.approx(_CLOSES[-1])
    finally:
        session.close()


def test_build_inference_windows_skips_tickers_with_too_little_history():
    session = SessionLocal()
    try:
        start = dt.date(2026, 1, 1)
        _seed_bars(session, "THIN", _CLOSES[:4], start)
        prediction_date = start + dt.timedelta(days=10)
        windows = build_inference_windows(session, prediction_date, lookback_days=5)
        assert windows == []
    finally:
        session.close()
