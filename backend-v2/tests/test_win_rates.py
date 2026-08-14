import datetime as dt

import pytest

from db.models import MarketPrediction, MarketPredictionMonteCarlo, OhlcBar, Ticker, WinRate
from db.session import SessionLocal, init_db
from jobs.win_rates import compute_win_rates


@pytest.fixture(autouse=True)
def _clean_db():
    init_db()
    session = SessionLocal()
    session.query(WinRate).delete()
    session.query(MarketPredictionMonteCarlo).delete()
    session.query(MarketPrediction).delete()
    session.query(OhlcBar).delete()
    session.query(Ticker).delete()
    session.commit()
    session.close()
    yield


def _prediction(ticker: str, predicted_date: dt.date, expected_return: float) -> MarketPrediction:
    return MarketPrediction(
        ticker=ticker,
        predicted_date=predicted_date,
        current_state="flat",
        predicted_state="up" if expected_return >= 0 else "down",
        state_confidence=0.5,
        expected_return=expected_return,
        entry_price=100.0,
        exit_price=100.0 * (1 + expected_return),
        exit_price_confidence=0.5,
        entry_time="09:30:00",
        exit_time="16:00:00",
        history_days=60,
        computed_at=dt.datetime.utcnow(),
    )


def _mcmc_prediction(ticker: str, predicted_date: dt.date, expected_return: float) -> MarketPredictionMonteCarlo:
    return MarketPredictionMonteCarlo(
        ticker=ticker,
        predicted_date=predicted_date,
        current_state="flat",
        predicted_state="up" if expected_return >= 0 else "down",
        state_confidence=0.5,
        expected_return=expected_return,
        entry_price=100.0,
        exit_price=100.0 * (1 + expected_return),
        exit_price_mean=100.0 * (1 + expected_return),
        exit_price_std=1.0,
        exit_price_confidence=0.5,
        exit_price_p10=99.0,
        exit_price_p50=100.0,
        exit_price_p90=101.0,
        entry_time="09:30:00",
        exit_time="16:00:00",
        num_simulations=2000,
        history_days=60,
        computed_at=dt.datetime.utcnow(),
    )


def _bar(ticker: str, date: dt.date, pcnt_increase: float) -> OhlcBar:
    return OhlcBar(
        ticker=ticker,
        multiplier=1,
        timespan="day",
        timestamp=dt.datetime.combine(date, dt.time.min),
        pcnt_increase=pcnt_increase,
    )


_D1 = dt.date(2026, 1, 2)
_D2 = dt.date(2026, 1, 3)
_D3 = dt.date(2026, 1, 4)
_D4 = dt.date(2026, 1, 5)  # never gets an ohlc_bars row - not yet evaluable


def test_computes_win_rate_per_ticker():
    session = SessionLocal()
    try:
        session.add(Ticker(ticker="AAA", type="CS"))
        # D1: markov predicts up, mcmc predicts up, actual is up - both win.
        session.add(_prediction("AAA", _D1, 0.02))
        session.add(_mcmc_prediction("AAA", _D1, 0.01))
        session.add(_bar("AAA", _D1, 2.0))
        # D2: markov predicts down, actual is down - markov wins; no mcmc row at all,
        # which still counts as an evaluated (losing) mcmc prediction.
        session.add(_prediction("AAA", _D2, -0.01))
        session.add(_bar("AAA", _D2, -2.0))
        # D3: markov predicts up (wins, actual up), mcmc predicts down (loses, actual up).
        session.add(_prediction("AAA", _D3, 0.03))
        session.add(_mcmc_prediction("AAA", _D3, -0.02))
        session.add(_bar("AAA", _D3, 5.0))
        # D4: predictions exist for both models but the actual outcome hasn't synced
        # yet - excluded from both counts entirely.
        session.add(_prediction("AAA", _D4, 0.01))
        session.add(_mcmc_prediction("AAA", _D4, 0.01))
        session.commit()

        stored = compute_win_rates(session)
        assert stored == 1

        row = session.query(WinRate).filter_by(ticker="AAA").one()
        assert (row.markov_win_count, row.markov_predictions_count) == (3, 3)
        assert row.markov_win_rate == pytest.approx(1.0)
        assert (row.mcmc_win_count, row.mcmc_predictions_count) == (1, 3)
        assert row.mcmc_win_rate == pytest.approx(1 / 3)
        assert row.last_updated is not None
    finally:
        session.close()


def test_zero_evaluable_predictions_leaves_rate_null():
    session = SessionLocal()
    try:
        session.add(Ticker(ticker="AAA", type="CS"))
        session.add(_prediction("AAA", _D1, 0.01))  # no ohlc_bars row for _D1
        session.commit()

        compute_win_rates(session)

        row = session.query(WinRate).filter_by(ticker="AAA").one()
        assert (row.markov_win_count, row.markov_predictions_count) == (0, 0)
        assert row.markov_win_rate is None
        assert (row.mcmc_win_count, row.mcmc_predictions_count) == (0, 0)
        assert row.mcmc_win_rate is None
    finally:
        session.close()


def test_zero_actual_and_zero_expected_counts_as_a_win():
    session = SessionLocal()
    try:
        session.add(Ticker(ticker="AAA", type="CS"))
        session.add(_prediction("AAA", _D1, 0.0))
        session.add(_bar("AAA", _D1, 0.0))
        session.commit()

        compute_win_rates(session)

        row = session.query(WinRate).filter_by(ticker="AAA").one()
        assert (row.markov_win_count, row.markov_predictions_count) == (1, 1)
    finally:
        session.close()


def test_tickers_filter_scopes_to_selected_tickers():
    session = SessionLocal()
    try:
        session.add_all([Ticker(ticker="AAA", type="CS"), Ticker(ticker="BBB", type="CS")])
        session.add(_prediction("AAA", _D1, 0.02))
        session.add(_bar("AAA", _D1, 2.0))
        session.add(_prediction("BBB", _D1, 0.02))
        session.add(_bar("BBB", _D1, 2.0))
        session.commit()

        stored = compute_win_rates(session, tickers=["AAA"])
        assert stored == 1
        assert session.query(WinRate).filter_by(ticker="BBB").count() == 0
    finally:
        session.close()


def test_rejects_tickers_and_ticker_types_together():
    session = SessionLocal()
    try:
        with pytest.raises(ValueError):
            compute_win_rates(session, ticker_types=["CS"], tickers=["AAA"])
    finally:
        session.close()


def test_rerun_upserts_rather_than_duplicating():
    session = SessionLocal()
    try:
        session.add(Ticker(ticker="AAA", type="CS"))
        session.add(_prediction("AAA", _D1, 0.02))
        session.add(_bar("AAA", _D1, 2.0))
        session.commit()

        compute_win_rates(session)
        compute_win_rates(session)

        assert session.query(WinRate).count() == 1
    finally:
        session.close()
