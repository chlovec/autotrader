import datetime as dt

import pytest

from db.models import MarketPrediction10Day, OhlcBar, Ticker
from db.session import SessionLocal, init_db
from jobs.predict_market_state import (
    ENTRY_TIME,
    EXIT_TIME,
    STATE_LABELS,
    _bucket_states,
    _fit_transition,
    _predict_next_state,
)
from jobs.predict_market_state_10_day import (
    DAYS_AHEAD,
    compute_10_day_market_state_predictions,
)


@pytest.fixture(autouse=True)
def _clean_db():
    init_db()
    session = SessionLocal()
    session.query(MarketPrediction10Day).delete()
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


def _independent_10_day_walk(closes: list[float]) -> dict:
    """Recomputes the expected day1..day10 values independently of
    compute_10_day_market_state_predictions, using the same lower-level helpers -
    mirrors test_predict_market_state.py's
    test_stores_prediction_matching_independent_recomputation."""
    returns = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
    states, bucket_means, _bucket_stds = _bucket_states(returns)
    counts = _fit_transition(states)

    expected: dict = {"current_state": STATE_LABELS[states[-1]]}
    day_state = states[-1]
    day_entry_price = closes[-1]
    for day in range(1, DAYS_AHEAD + 1):
        predicted_state, confidence = _predict_next_state(counts, day_state, states)
        expected_return = bucket_means[predicted_state]
        day_exit_price = day_entry_price * (1 + expected_return)
        expected[f"day{day}_predicted_state"] = STATE_LABELS[predicted_state]
        expected[f"day{day}_state_confidence"] = confidence
        expected[f"day{day}_entry_price"] = day_entry_price
        expected[f"day{day}_exit_price"] = day_exit_price
        expected[f"day{day}_expected_return_pct"] = expected_return * 100
        day_state = predicted_state
        day_entry_price = day_exit_price
    return expected


def test_skips_tickers_with_too_little_history():
    session = SessionLocal()
    try:
        _seed_bars(session, "THIN", _CLOSES[:5], dt.date(2026, 1, 1))
        stored = compute_10_day_market_state_predictions(session, min_history_days=10)
        assert stored == 0
        assert session.query(MarketPrediction10Day).count() == 0
    finally:
        session.close()


def test_stores_10_day_walk_matching_independent_recomputation():
    session = SessionLocal()
    try:
        start = dt.date(2026, 1, 1)
        _seed_bars(session, "AAA", _CLOSES, start)
        start_date = start + dt.timedelta(days=len(_CLOSES))  # one day after the last seeded bar
        stored = compute_10_day_market_state_predictions(session, start_date=start_date, min_history_days=10)
        assert stored == 1

        row = session.query(MarketPrediction10Day).filter_by(ticker="AAA").one()
        assert row.start_date == start_date

        expected = _independent_10_day_walk(_CLOSES)
        assert row.current_state == expected["current_state"]
        for day in range(1, DAYS_AHEAD + 1):
            assert getattr(row, f"day{day}_predicted_state") == expected[f"day{day}_predicted_state"]
            assert getattr(row, f"day{day}_state_confidence") == pytest.approx(expected[f"day{day}_state_confidence"])
            assert getattr(row, f"day{day}_entry_price") == pytest.approx(expected[f"day{day}_entry_price"])
            assert getattr(row, f"day{day}_exit_price") == pytest.approx(expected[f"day{day}_exit_price"])
            assert getattr(row, f"day{day}_expected_return_pct") == pytest.approx(
                expected[f"day{day}_expected_return_pct"]
            )
            assert getattr(row, f"day{day}_entry_time") == ENTRY_TIME
            assert getattr(row, f"day{day}_exit_time") == EXIT_TIME

        # Prices compound day-over-day, not reset each day.
        assert row.day2_entry_price == pytest.approx(row.day1_exit_price)
        assert row.day10_entry_price != row.day1_entry_price
    finally:
        session.close()


def test_data_before_start_date_cutoff_is_excluded():
    """Bars on/after start_date must not influence the fit - a real prediction can
    never see data from a day it hasn't happened yet."""
    session = SessionLocal()
    try:
        start = dt.date(2026, 1, 1)
        _seed_bars(session, "AAA", _CLOSES, start)
        start_date = start + dt.timedelta(days=len(_CLOSES))
        compute_10_day_market_state_predictions(session, start_date=start_date, min_history_days=10)
        baseline = session.query(MarketPrediction10Day).filter_by(ticker="AAA").one()
        baseline_day1_entry = baseline.day1_entry_price

        # A wild, obviously-influential bar landing exactly on start_date.
        session.add(
            OhlcBar(
                ticker="AAA",
                multiplier=1,
                timespan="day",
                timestamp=dt.datetime.combine(start_date, dt.time.min),
                close=999999.0,
            )
        )
        session.commit()

        session.query(MarketPrediction10Day).delete()
        session.commit()
        compute_10_day_market_state_predictions(session, start_date=start_date, min_history_days=10)
        after = session.query(MarketPrediction10Day).filter_by(ticker="AAA").one()
        assert after.day1_entry_price == pytest.approx(baseline_day1_entry)
    finally:
        session.close()


def test_rerun_same_start_date_upserts_rather_than_duplicating():
    session = SessionLocal()
    try:
        _seed_bars(session, "AAA", _CLOSES, dt.date(2026, 1, 1))
        compute_10_day_market_state_predictions(session, min_history_days=10)
        compute_10_day_market_state_predictions(session, min_history_days=10)
        assert session.query(MarketPrediction10Day).count() == 1
    finally:
        session.close()


def test_different_start_dates_accumulate_separate_rows():
    session = SessionLocal()
    try:
        _seed_bars(session, "AAA", _CLOSES, dt.date(2026, 1, 1))
        compute_10_day_market_state_predictions(session, start_date=dt.date(2026, 3, 1), min_history_days=10)
        compute_10_day_market_state_predictions(session, start_date=dt.date(2026, 3, 2), min_history_days=10)
        assert session.query(MarketPrediction10Day).count() == 2
    finally:
        session.close()


def test_start_date_defaults_to_tomorrow_utc():
    session = SessionLocal()
    try:
        _seed_bars(session, "AAA", _CLOSES, dt.date(2026, 1, 1))
        compute_10_day_market_state_predictions(session, min_history_days=10)
        row = session.query(MarketPrediction10Day).filter_by(ticker="AAA").one()
        assert row.start_date == dt.datetime.now(dt.timezone.utc).date() + dt.timedelta(days=1)
    finally:
        session.close()


def test_tickers_filter_scopes_to_selected_tickers():
    session = SessionLocal()
    try:
        _seed_bars(session, "AAA", _CLOSES, dt.date(2026, 1, 1))
        _seed_bars(session, "BBB", _CLOSES, dt.date(2026, 1, 1))
        stored = compute_10_day_market_state_predictions(session, tickers=["AAA"], min_history_days=10)
        assert stored == 1
        assert {row.ticker for row in session.query(MarketPrediction10Day).all()} == {"AAA"}
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

        stored = compute_10_day_market_state_predictions(session, ticker_types=["CS"], min_history_days=10)
        assert stored == 1
        assert {row.ticker for row in session.query(MarketPrediction10Day).all()} == {"AAA"}
    finally:
        session.close()


def test_skips_bad_zero_close_bars_instead_of_dividing_by_zero():
    session = SessionLocal()
    try:
        start = dt.date(2026, 1, 1)
        closes = list(_CLOSES)
        closes[10] = 0.0  # a bad $0 print, as seen in the real dataset
        _seed_bars(session, "AAA", closes, start)
        stored = compute_10_day_market_state_predictions(session, min_history_days=10)
        assert stored == 1
    finally:
        session.close()


def test_tickers_and_ticker_types_together_raises():
    session = SessionLocal()
    try:
        with pytest.raises(ValueError):
            compute_10_day_market_state_predictions(session, ticker_types=["CS"], tickers=["AAA"])
    finally:
        session.close()
