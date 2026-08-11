import datetime as dt
import random

import pytest

from db.models import MarketPredictionMonteCarlo, OhlcBar, Ticker
from db.session import SessionLocal, init_db
from jobs.predict_market_state import STATE_LABELS
from jobs.predict_market_state_mcmc import (
    DEFAULT_NUM_SIMULATIONS,
    compute_market_state_mcmc_predictions,
)


@pytest.fixture(autouse=True)
def _clean_db():
    init_db()
    session = SessionLocal()
    session.query(MarketPredictionMonteCarlo).delete()
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
                close=close,
            )
        )
    session.commit()


# 31 distinct, always-positive, oscillating-with-drift closes - enough spread for
# statistics.quantiles(n=5) to produce five non-degenerate buckets. Same series
# predict_market_state.py's own tests use.
_CLOSES = [100 + ((-1) ** i) * (i % 7) + i * 0.3 for i in range(31)]


def test_skips_tickers_with_too_little_history():
    session = SessionLocal()
    try:
        _seed_bars(session, "THIN", _CLOSES[:5], dt.date(2026, 1, 1))
        stored = compute_market_state_mcmc_predictions(session, min_history_days=10, rng=random.Random(1))
        assert stored == 0
        assert session.query(MarketPredictionMonteCarlo).count() == 0
    finally:
        session.close()


def test_stores_prediction_with_sane_distribution_shape():
    session = SessionLocal()
    try:
        start = dt.date(2026, 1, 1)
        _seed_bars(session, "AAA", _CLOSES, start)
        prediction_date = start + dt.timedelta(days=len(_CLOSES))  # one day after the last seeded bar
        stored = compute_market_state_mcmc_predictions(
            session, prediction_date=prediction_date, min_history_days=10, num_simulations=500, rng=random.Random(1)
        )
        assert stored == 1

        row = session.query(MarketPredictionMonteCarlo).filter_by(ticker="AAA").one()
        assert row.predicted_date == prediction_date
        assert row.current_state in STATE_LABELS
        assert row.predicted_state in STATE_LABELS
        assert 0.0 <= row.state_confidence <= 1.0
        assert row.entry_price == pytest.approx(_CLOSES[-1])
        assert row.exit_price_p10 <= row.exit_price_p50 <= row.exit_price_p90
        assert row.exit_price_std >= 0.0
        assert row.expected_return == pytest.approx(row.exit_price_mean / row.entry_price - 1)
        assert row.exit_price == pytest.approx(row.entry_price * (1 + row.expected_return))
        assert row.exit_price == pytest.approx(row.exit_price_mean)
        assert row.exit_price_confidence == pytest.approx(1 - row.exit_price_std / row.exit_price_mean)
        assert row.num_simulations == 500
        assert row.entry_time == "09:30:00"
        assert row.exit_time == "16:00:00"
    finally:
        session.close()


def test_data_on_or_after_prediction_date_cutoff_is_excluded():
    """Bars on/after prediction_date must not influence the fit - a real prediction can
    never see data from a day it hasn't happened yet."""
    session = SessionLocal()
    try:
        start = dt.date(2026, 1, 1)
        _seed_bars(session, "AAA", _CLOSES, start)
        prediction_date = start + dt.timedelta(days=len(_CLOSES))
        compute_market_state_mcmc_predictions(
            session, prediction_date=prediction_date, min_history_days=10, rng=random.Random(1)
        )
        baseline = session.query(MarketPredictionMonteCarlo).filter_by(ticker="AAA").one()
        baseline_entry_price = baseline.entry_price

        # A wild, obviously-influential bar landing exactly on prediction_date.
        session.add(
            OhlcBar(
                ticker="AAA",
                multiplier=1,
                timespan="day",
                timestamp=dt.datetime.combine(prediction_date, dt.time.min),
                close=999999.0,
            )
        )
        session.commit()

        session.query(MarketPredictionMonteCarlo).delete()
        session.commit()
        compute_market_state_mcmc_predictions(
            session, prediction_date=prediction_date, min_history_days=10, rng=random.Random(1)
        )
        after = session.query(MarketPredictionMonteCarlo).filter_by(ticker="AAA").one()
        assert after.entry_price == pytest.approx(baseline_entry_price)
    finally:
        session.close()


def test_rerun_same_prediction_date_upserts_rather_than_duplicating():
    session = SessionLocal()
    try:
        _seed_bars(session, "AAA", _CLOSES, dt.date(2026, 1, 1))
        compute_market_state_mcmc_predictions(session, min_history_days=10, rng=random.Random(1))
        compute_market_state_mcmc_predictions(session, min_history_days=10, rng=random.Random(2))
        assert session.query(MarketPredictionMonteCarlo).count() == 1
    finally:
        session.close()


def test_different_prediction_dates_accumulate_separate_rows():
    session = SessionLocal()
    try:
        _seed_bars(session, "AAA", _CLOSES, dt.date(2026, 1, 1))
        compute_market_state_mcmc_predictions(
            session, prediction_date=dt.date(2026, 3, 1), min_history_days=10, rng=random.Random(1)
        )
        compute_market_state_mcmc_predictions(
            session, prediction_date=dt.date(2026, 3, 2), min_history_days=10, rng=random.Random(1)
        )
        assert session.query(MarketPredictionMonteCarlo).count() == 2
    finally:
        session.close()


def test_prediction_date_defaults_to_tomorrow_utc():
    session = SessionLocal()
    try:
        _seed_bars(session, "AAA", _CLOSES, dt.date(2026, 1, 1))
        compute_market_state_mcmc_predictions(session, min_history_days=10, rng=random.Random(1))
        row = session.query(MarketPredictionMonteCarlo).filter_by(ticker="AAA").one()
        assert row.predicted_date == dt.datetime.now(dt.timezone.utc).date() + dt.timedelta(days=1)
    finally:
        session.close()


def test_tickers_filter_scopes_to_selected_tickers():
    session = SessionLocal()
    try:
        _seed_bars(session, "AAA", _CLOSES, dt.date(2026, 1, 1))
        _seed_bars(session, "BBB", _CLOSES, dt.date(2026, 1, 1))
        stored = compute_market_state_mcmc_predictions(
            session, tickers=["AAA"], min_history_days=10, rng=random.Random(1)
        )
        assert stored == 1
        assert {row.ticker for row in session.query(MarketPredictionMonteCarlo).all()} == {"AAA"}
    finally:
        session.close()


def test_ticker_types_resolve_from_tickers_table():
    session = SessionLocal()
    try:
        session.query(Ticker).delete()
        session.add(Ticker(ticker="AAA", type="CS"))
        session.add(Ticker(ticker="BBB", type="ETF"))
        for i, close in enumerate(_CLOSES):
            ts = dt.datetime.combine(dt.date(2026, 1, 1) + dt.timedelta(days=i), dt.time.min)
            session.add(OhlcBar(ticker="AAA", multiplier=1, timespan="day", timestamp=ts, close=close))
            session.add(OhlcBar(ticker="BBB", multiplier=1, timespan="day", timestamp=ts, close=close))
        session.commit()

        stored = compute_market_state_mcmc_predictions(
            session, ticker_types=["CS"], min_history_days=10, rng=random.Random(1)
        )
        assert stored == 1
        assert {row.ticker for row in session.query(MarketPredictionMonteCarlo).all()} == {"AAA"}
    finally:
        session.close()


def test_skips_bad_zero_close_bars_instead_of_dividing_by_zero():
    session = SessionLocal()
    try:
        start = dt.date(2026, 1, 1)
        closes = list(_CLOSES)
        closes[10] = 0.0  # a bad $0 print, as seen in the real dataset
        _seed_bars(session, "AAA", closes, start)
        stored = compute_market_state_mcmc_predictions(session, min_history_days=10, rng=random.Random(1))
        assert stored == 1
    finally:
        session.close()


def test_tickers_and_ticker_types_together_raises():
    session = SessionLocal()
    try:
        with pytest.raises(ValueError):
            compute_market_state_mcmc_predictions(session, ticker_types=["CS"], tickers=["AAA"])
    finally:
        session.close()


def test_num_simulations_must_be_at_least_one():
    session = SessionLocal()
    try:
        _seed_bars(session, "AAA", _CLOSES, dt.date(2026, 1, 1))
        with pytest.raises(ValueError):
            compute_market_state_mcmc_predictions(session, num_simulations=0, min_history_days=10)
    finally:
        session.close()


def test_default_num_simulations_is_used_when_unspecified():
    session = SessionLocal()
    try:
        _seed_bars(session, "AAA", _CLOSES, dt.date(2026, 1, 1))
        compute_market_state_mcmc_predictions(session, min_history_days=10, rng=random.Random(1))
        row = session.query(MarketPredictionMonteCarlo).filter_by(ticker="AAA").one()
        assert row.num_simulations == DEFAULT_NUM_SIMULATIONS
    finally:
        session.close()


def test_single_simulation_produces_degenerate_but_valid_distribution():
    """num_simulations == 1 - the std/percentile edge case with only one sample."""
    session = SessionLocal()
    try:
        _seed_bars(session, "AAA", _CLOSES, dt.date(2026, 1, 1))
        stored = compute_market_state_mcmc_predictions(
            session, num_simulations=1, min_history_days=10, rng=random.Random(1)
        )
        assert stored == 1
        row = session.query(MarketPredictionMonteCarlo).filter_by(ticker="AAA").one()
        assert row.exit_price_std == 0.0
        assert row.exit_price_p10 == row.exit_price_p50 == row.exit_price_p90 == row.exit_price_mean
        # Zero std against a nonzero mean -> perfect (1.0) tightness score.
        assert row.exit_price_confidence == pytest.approx(1.0)
    finally:
        session.close()
