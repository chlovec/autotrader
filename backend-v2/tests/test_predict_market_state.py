import datetime as dt

import pytest

from db.models import MarketPrediction, OhlcBar, Ticker
from db.session import SessionLocal, init_db
from jobs.predict_market_state import (
    STATE_LABELS,
    _bucket_states,
    _fit_transition,
    _next_trading_day,
    _predict_next_state,
    compute_market_state_predictions,
)


@pytest.fixture(autouse=True)
def _clean_db():
    init_db()
    session = SessionLocal()
    session.query(MarketPrediction).delete()
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
# statistics.quantiles(n=5) to produce five non-degenerate buckets.
_CLOSES = [100 + ((-1) ** i) * (i % 7) + i * 0.3 for i in range(31)]


def test_skips_tickers_with_too_little_history():
    session = SessionLocal()
    try:
        _seed_bars(session, "THIN", _CLOSES[:5], dt.date(2026, 1, 1))
        stored = compute_market_state_predictions(session, min_history_days=10)
        assert stored == 0
        assert session.query(MarketPrediction).count() == 0
    finally:
        session.close()


def test_stores_prediction_matching_independent_recomputation():
    session = SessionLocal()
    try:
        start = dt.date(2026, 1, 1)
        _seed_bars(session, "AAA", _CLOSES, start)
        stored = compute_market_state_predictions(session, min_history_days=10)
        assert stored == 1

        row = session.query(MarketPrediction).filter_by(ticker="AAA").one()

        returns = [_CLOSES[i] / _CLOSES[i - 1] - 1 for i in range(1, len(_CLOSES))]
        states, bucket_means = _bucket_states(returns)
        counts = _fit_transition(states)
        predicted_state, confidence = _predict_next_state(counts, states[-1], states)

        assert row.current_state == STATE_LABELS[states[-1]]
        assert row.predicted_state == STATE_LABELS[predicted_state]
        assert row.state_confidence == pytest.approx(confidence)
        assert row.expected_return == pytest.approx(bucket_means[predicted_state])
        assert row.entry_price == pytest.approx(_CLOSES[-1])
        assert row.exit_price == pytest.approx(_CLOSES[-1] * (1 + bucket_means[predicted_state]))
        assert row.entry_time == "09:30:00"
        assert row.exit_time == "16:00:00"
        assert row.history_days == len(returns)

        last_date = start + dt.timedelta(days=len(_CLOSES) - 1)
        assert row.predicted_date == _next_trading_day(last_date)
    finally:
        session.close()


def test_rerun_upserts_rather_than_duplicating():
    session = SessionLocal()
    try:
        _seed_bars(session, "AAA", _CLOSES, dt.date(2026, 1, 1))
        compute_market_state_predictions(session, min_history_days=10)
        compute_market_state_predictions(session, min_history_days=10)
        assert session.query(MarketPrediction).count() == 1
    finally:
        session.close()


def test_tickers_filter_scopes_to_selected_tickers():
    session = SessionLocal()
    try:
        _seed_bars(session, "AAA", _CLOSES, dt.date(2026, 1, 1))
        _seed_bars(session, "BBB", _CLOSES, dt.date(2026, 1, 1))
        stored = compute_market_state_predictions(session, tickers=["AAA"], min_history_days=10)
        assert stored == 1
        assert {row.ticker for row in session.query(MarketPrediction).all()} == {"AAA"}
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

        stored = compute_market_state_predictions(session, ticker_types=["CS"], min_history_days=10)
        assert stored == 1
        assert {row.ticker for row in session.query(MarketPrediction).all()} == {"AAA"}
    finally:
        session.close()


def test_skips_bad_zero_close_bars_instead_of_dividing_by_zero():
    session = SessionLocal()
    try:
        start = dt.date(2026, 1, 1)
        closes = list(_CLOSES)
        closes[10] = 0.0  # a bad $0 print, as seen in the real dataset
        _seed_bars(session, "AAA", closes, start)
        stored = compute_market_state_predictions(session, min_history_days=10)
        assert stored == 1
    finally:
        session.close()


def test_tickers_and_ticker_types_together_raises():
    session = SessionLocal()
    try:
        with pytest.raises(ValueError):
            compute_market_state_predictions(session, ticker_types=["CS"], tickers=["AAA"])
    finally:
        session.close()
