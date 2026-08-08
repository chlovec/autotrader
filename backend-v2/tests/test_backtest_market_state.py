import datetime as dt
import statistics

import pytest

from db.models import MarketPredictionBacktest, OhlcBar, Ticker
from db.session import SessionLocal, init_db
from jobs.backtest_market_state import compute_market_state_backtest
from jobs.predict_market_state import (
    STATE_LABELS,
    _bucket_one,
    _cut_points,
    _fit_transition,
    _predict_next_state,
)


@pytest.fixture(autouse=True)
def _clean_db():
    init_db()
    session = SessionLocal()
    session.query(MarketPredictionBacktest).delete()
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


# 41 distinct, always-positive, oscillating-with-drift closes - enough spread for
# statistics.quantiles(n=5) to produce five non-degenerate buckets, and enough length
# for several evaluable walk-forward days at a small min_history_days.
_CLOSES = [100 + ((-1) ** i) * (i % 7) + i * 0.3 for i in range(41)]
_START = dt.date(2026, 1, 1)
_WIDE_RANGE = (dt.date(2020, 1, 1), dt.date(2030, 1, 1))


def test_skips_tickers_with_too_little_history():
    session = SessionLocal()
    try:
        _seed_bars(session, "THIN", _CLOSES[:11], _START)  # min_history_days=10 needs 12
        stored = compute_market_state_backtest(session, *_WIDE_RANGE, min_history_days=10)
        assert stored == 0
        assert session.query(MarketPredictionBacktest).count() == 0
    finally:
        session.close()


def test_stores_one_result_per_evaluable_day():
    session = SessionLocal()
    try:
        _seed_bars(session, "AAA", _CLOSES, _START)
        stored = compute_market_state_backtest(session, *_WIDE_RANGE, min_history_days=10)
        # len(closes) = 41, min_history_days = 10 -> i ranges over range(10, 40) = 30 days.
        assert stored == 30
        assert session.query(MarketPredictionBacktest).filter_by(ticker="AAA").count() == 30
    finally:
        session.close()


def test_result_matches_independent_walk_forward_recomputation():
    session = SessionLocal()
    try:
        _seed_bars(session, "AAA", _CLOSES, _START)
        compute_market_state_backtest(session, *_WIDE_RANGE, min_history_days=10)

        i = 20  # an arbitrary evaluated step within range(10, 40)
        evaluated_date = _START + dt.timedelta(days=i + 1)
        row = session.query(MarketPredictionBacktest).filter_by(ticker="AAA", evaluated_date=evaluated_date).one()

        all_returns = [_CLOSES[k] / _CLOSES[k - 1] - 1 for k in range(1, len(_CLOSES))]
        history_returns = all_returns[:i]
        cut_points = _cut_points(history_returns)
        states = [_bucket_one(r, cut_points) for r in history_returns]
        bucket_returns: list[list[float]] = [[] for _ in STATE_LABELS]
        for state, r in zip(states, history_returns):
            bucket_returns[state].append(r)
        bucket_means = [statistics.mean(v) if v else 0.0 for v in bucket_returns]
        counts = _fit_transition(states)
        current_state = states[-1]
        predicted_state, confidence = _predict_next_state(counts, current_state, states)
        actual_state = _bucket_one(all_returns[i], cut_points)

        assert row.as_of_date == _START + dt.timedelta(days=i)
        assert row.current_state == STATE_LABELS[current_state]
        assert row.predicted_state == STATE_LABELS[predicted_state]
        assert row.actual_state == STATE_LABELS[actual_state]
        assert row.predicted_correct == (predicted_state == actual_state)
        assert row.state_confidence == pytest.approx(confidence)
        assert row.expected_return == pytest.approx(bucket_means[predicted_state])
        assert row.entry_price == pytest.approx(_CLOSES[i])
        assert row.predicted_exit_price == pytest.approx(_CLOSES[i] * (1 + bucket_means[predicted_state]))
        assert row.actual_exit_price == pytest.approx(_CLOSES[i + 1])
        expected_error_pct = (row.predicted_exit_price - _CLOSES[i + 1]) / _CLOSES[i + 1]
        assert row.price_error_pct == pytest.approx(expected_error_pct)
        assert row.history_days == i
    finally:
        session.close()


def test_date_range_filters_evaluated_dates():
    session = SessionLocal()
    try:
        _seed_bars(session, "AAA", _CLOSES, _START)
        # Only evaluated_date == _START + 20 falls inside this one-day window.
        window_date = _START + dt.timedelta(days=20)
        stored = compute_market_state_backtest(session, window_date, window_date, min_history_days=10)
        assert stored == 1
        row = session.query(MarketPredictionBacktest).one()
        assert row.evaluated_date == window_date
    finally:
        session.close()


def test_rerun_upserts_rather_than_duplicating():
    session = SessionLocal()
    try:
        _seed_bars(session, "AAA", _CLOSES, _START)
        compute_market_state_backtest(session, *_WIDE_RANGE, min_history_days=10)
        compute_market_state_backtest(session, *_WIDE_RANGE, min_history_days=10)
        assert session.query(MarketPredictionBacktest).count() == 30
    finally:
        session.close()


def test_tickers_filter_scopes_to_selected_tickers():
    session = SessionLocal()
    try:
        _seed_bars(session, "AAA", _CLOSES, _START)
        _seed_bars(session, "BBB", _CLOSES, _START)
        stored = compute_market_state_backtest(session, *_WIDE_RANGE, tickers=["AAA"], min_history_days=10)
        assert stored == 30
        assert {row.ticker for row in session.query(MarketPredictionBacktest).all()} == {"AAA"}
    finally:
        session.close()


def test_ticker_types_resolve_from_tickers_table():
    session = SessionLocal()
    try:
        session.query(Ticker).delete()
        session.add(Ticker(ticker="AAA", type="CS"))
        session.add(Ticker(ticker="BBB", type="ETF"))
        for i, close in enumerate(_CLOSES):
            ts = dt.datetime.combine(_START + dt.timedelta(days=i), dt.time.min)
            session.add(OhlcBar(ticker="AAA", multiplier=1, timespan="day", timestamp=ts, close=close))
            session.add(OhlcBar(ticker="BBB", multiplier=1, timespan="day", timestamp=ts, close=close))
        session.commit()

        stored = compute_market_state_backtest(session, *_WIDE_RANGE, ticker_types=["CS"], min_history_days=10)
        assert stored == 30
        assert {row.ticker for row in session.query(MarketPredictionBacktest).all()} == {"AAA"}
    finally:
        session.close()


def test_tickers_and_ticker_types_together_raises():
    session = SessionLocal()
    try:
        with pytest.raises(ValueError):
            compute_market_state_backtest(session, ticker_types=["CS"], tickers=["AAA"])
    finally:
        session.close()


def test_start_date_after_end_date_raises():
    session = SessionLocal()
    try:
        with pytest.raises(ValueError):
            compute_market_state_backtest(session, dt.date(2026, 6, 1), dt.date(2026, 1, 1))
    finally:
        session.close()


def test_skips_evaluation_across_a_gap_versus_the_global_trading_calendar():
    session = SessionLocal()
    try:
        _seed_bars(session, "AAA", _CLOSES, _START)  # trades every day - completes the global calendar

        # BBB trades every day except day 20 (a halt). AAA's bar on day 20 still puts
        # that date in the global trading calendar, so BBB's day19 -> day21 pair spans
        # a gap and should be skipped rather than scored as a fair next-day evaluation.
        session.add(Ticker(ticker="BBB"))
        for i, close in enumerate(_CLOSES):
            if i == 20:
                continue
            session.add(
                OhlcBar(
                    ticker="BBB",
                    multiplier=1,
                    timespan="day",
                    timestamp=dt.datetime.combine(_START + dt.timedelta(days=i), dt.time.min),
                    close=close,
                )
            )
        session.commit()

        stored = compute_market_state_backtest(session, *_WIDE_RANGE, min_history_days=10)

        aaa_dates = {r.evaluated_date for r in session.query(MarketPredictionBacktest).filter_by(ticker="AAA")}
        bbb_dates = {r.evaluated_date for r in session.query(MarketPredictionBacktest).filter_by(ticker="BBB")}
        assert len(aaa_dates) == 30
        # 39 bars -> range(10, 38) = 28 candidate pairs, minus the one spanning the gap.
        assert len(bbb_dates) == 28
        assert stored == 30 + 28
        assert (_START + dt.timedelta(days=21)) not in bbb_dates
    finally:
        session.close()


def test_skips_bad_zero_close_bars_instead_of_dividing_by_zero():
    session = SessionLocal()
    try:
        closes = list(_CLOSES)
        closes[10] = 0.0  # a bad $0 print, as seen in the real dataset
        _seed_bars(session, "AAA", closes, _START)
        stored = compute_market_state_backtest(session, *_WIDE_RANGE, min_history_days=10)
        assert stored >= 0  # ran to completion without a ZeroDivisionError
    finally:
        session.close()
