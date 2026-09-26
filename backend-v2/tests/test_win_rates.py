import datetime as dt

import pytest

from db.models import MarketPrediction, MarketPredictionMonteCarlo, OhlcBar, Ticker, WinRate
from db.session import SessionLocal, init_db
from jobs.win_rates import DEFAULT_MCMC_RANGE_CONFIDENCE_LEVEL, compute_win_rates


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


def _bar(ticker: str, date: dt.date, pcnt_increase: float, close: float | None = None) -> OhlcBar:
    return OhlcBar(
        ticker=ticker,
        multiplier=1,
        timespan="day",
        timestamp=dt.datetime.combine(date, dt.time.min),
        pcnt_increase=pcnt_increase,
        close=close,
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


def test_mcmc_range_win_counts_actual_within_confidence_interval():
    """_mcmc_prediction fixes exit_price_std at 1.0, so at the default 95% level
    (Z ~= 1.96) the interval around a 0.0-expected-return prediction (mean 100) is
    ~[98.04, 101.96] - a close of 100 lands well inside it."""
    session = SessionLocal()
    try:
        session.add(Ticker(ticker="AAA", type="CS"))
        session.add(_prediction("AAA", _D1, 0.0))
        session.add(_mcmc_prediction("AAA", _D1, 0.0))
        session.add(_bar("AAA", _D1, 0.0, close=100.0))
        session.commit()

        compute_win_rates(session)

        row = session.query(WinRate).filter_by(ticker="AAA").one()
        assert (row.mcmc_range_win_count, row.mcmc_predictions_count) == (1, 1)
        assert row.mcmc_range_win_rate == pytest.approx(1.0)
        assert row.mcmc_range_confidence_level == pytest.approx(DEFAULT_MCMC_RANGE_CONFIDENCE_LEVEL)
    finally:
        session.close()


def test_mcmc_range_loss_when_actual_outside_confidence_interval():
    session = SessionLocal()
    try:
        session.add(Ticker(ticker="AAA", type="CS"))
        session.add(_prediction("AAA", _D1, 0.0))
        session.add(_mcmc_prediction("AAA", _D1, 0.0))
        # Far outside the ~[98.04, 101.96] 95% interval around mean 100, std 1.0.
        session.add(_bar("AAA", _D1, 0.0, close=90.0))
        session.commit()

        compute_win_rates(session)

        row = session.query(WinRate).filter_by(ticker="AAA").one()
        assert (row.mcmc_range_win_count, row.mcmc_predictions_count) == (0, 1)
        assert row.mcmc_range_win_rate == pytest.approx(0.0)
    finally:
        session.close()


def test_mcmc_range_win_is_independent_of_direction_win():
    """A wide-enough interval can contain the actual price even though its own mean
    predicted the wrong direction - direction and range are genuinely separate axes,
    not just two views of the same outcome."""
    session = SessionLocal()
    try:
        session.add(Ticker(ticker="AAA", type="CS"))
        session.add(_prediction("AAA", _D1, 0.0))
        # mcmc predicts up (mean 101) but actual pcnt_increase is negative - a
        # direction loss - while close=100.5 still falls inside [~99.04, ~102.96].
        session.add(_mcmc_prediction("AAA", _D1, 0.01))
        session.add(_bar("AAA", _D1, -1.0, close=100.5))
        session.commit()

        compute_win_rates(session)

        row = session.query(WinRate).filter_by(ticker="AAA").one()
        assert row.mcmc_win_count == 0  # direction: predicted up, actual down - a loss
        assert row.mcmc_range_win_count == 1  # range: actual still inside the CI - a win
    finally:
        session.close()


def test_mcmc_range_win_excludes_pairs_with_no_mcmc_prediction():
    """Same "counts toward mcmc_predictions_count as a loss" treatment as the
    direction-based mcmc win - there's no simulated distribution to check the actual
    price against, so it can't be a range win either."""
    session = SessionLocal()
    try:
        session.add(Ticker(ticker="AAA", type="CS"))
        session.add(_prediction("AAA", _D1, 0.0))
        session.add(_bar("AAA", _D1, 0.0, close=100.0))
        session.commit()

        compute_win_rates(session)

        row = session.query(WinRate).filter_by(ticker="AAA").one()
        assert (row.mcmc_range_win_count, row.mcmc_predictions_count) == (0, 1)
        assert row.mcmc_range_win_rate == pytest.approx(0.0)
    finally:
        session.close()


def test_mcmc_range_confidence_level_is_configurable():
    """A tighter (lower) confidence level shrinks the interval enough to exclude a
    close that the default 95% level would have included."""
    session = SessionLocal()
    try:
        session.add(Ticker(ticker="AAA", type="CS"))
        session.add(_prediction("AAA", _D1, 0.0))
        session.add(_mcmc_prediction("AAA", _D1, 0.0))
        # 1.7 std out - inside the default 95% (~1.96 std) interval, outside a much
        # tighter 50% (~0.67 std) one.
        session.add(_bar("AAA", _D1, 0.0, close=101.7))
        session.commit()

        compute_win_rates(session, mcmc_range_confidence_level=0.95)
        assert session.query(WinRate).filter_by(ticker="AAA").one().mcmc_range_win_count == 1

        compute_win_rates(session, mcmc_range_confidence_level=0.5)
        row = session.query(WinRate).filter_by(ticker="AAA").one()
        assert row.mcmc_range_win_count == 0
        assert row.mcmc_range_confidence_level == pytest.approx(0.5)
    finally:
        session.close()


def test_rejects_confidence_level_outside_open_unit_interval():
    session = SessionLocal()
    try:
        with pytest.raises(ValueError):
            compute_win_rates(session, mcmc_range_confidence_level=1.0)
        with pytest.raises(ValueError):
            compute_win_rates(session, mcmc_range_confidence_level=0.0)
    finally:
        session.close()
