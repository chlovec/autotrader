"""Walk-forward backtest of jobs/predict_market_state.py's approach: for each trading
day within a requested date range, re-fits the same per-ticker Markov chain using only
ohlc_bars through the prior trading day, generates the same prediction
predict_market_state.py would have made at that point in time, and compares it against
what actually happened next - the day being evaluated never influences its own
prediction (including the quantile cut points its actual outcome is bucketed against),
so this is a fair no-lookahead evaluation.

Purely local aggregation over already-synced bars - no massive.com call involved, same
reasoning as jobs/average_volume.py and jobs/predict_market_state.py.
"""

import bisect
import datetime as dt
import logging
import statistics
from itertools import groupby

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from db.models import MarketPredictionBacktest, OhlcBar
from jobs.control import JobControl
from jobs.predict_market_state import (
    DEFAULT_MIN_HISTORY_DAYS,
    DEFAULT_MULTIPLIER,
    DEFAULT_TIMESPAN,
    STATE_LABELS,
    _apply_ticker_filter,
    _bucket_one,
    _cut_points,
    _fit_transition,
    _predict_next_state,
)

logger = logging.getLogger("backend_v2.jobs.backtest_market_state")

# start_date's fallback when unset - see compute_market_state_backtest's docstring.
DEFAULT_BACKTEST_DAYS = 90


def _next_trading_day(date: dt.date, all_dates: list[dt.date]) -> dt.date | None:
    """The earliest date in `all_dates` (sorted, deduped, spanning every ticker) that's
    after `date` - unlike predict_market_state.py's calendar-naive helper of the same
    name, this is grounded in ohlc_bars itself, so it's correct across market holidays
    as long as at least one ticker in the fetched set traded that day. Only usable in a
    backtest: it requires the next session's data to already be synced, which a live
    prediction (jobs/predict_market_state.py) never has for the day it's predicting."""
    i = bisect.bisect_right(all_dates, date)
    return all_dates[i] if i < len(all_dates) else date


def compute_market_state_backtest(
    session: Session,
    start_date: dt.date | None = None,
    end_date: dt.date | None = None,
    ticker_types: list[str] | None = None,
    tickers: list[str] | None = None,
    min_history_days: int = DEFAULT_MIN_HISTORY_DAYS,
    multiplier: int = DEFAULT_MULTIPLIER,
    timespan: str = DEFAULT_TIMESPAN,
    control: JobControl | None = None,
) -> int:
    """`end_date` defaults to yesterday (UTC), `start_date` to DEFAULT_BACKTEST_DAYS
    calendar days before that - same "resolve a None date at run time" reasoning as
    jobs/average_volume.py's compute_average_volume.

    Runs off the event loop (see app/main.py's _run_job) - control.checkpoint_sync is
    called once per ticker, same granularity as jobs/predict_market_state.py.

    For each ticker, walks its full close history but only does the expensive
    quantile/transition-matrix refit for a historical point once its *next* trading
    day's date is confirmed to fall within [start_date, end_date] (checked first, since
    dates only increase as the walk proceeds) - so a run's cost scales with the size of
    the requested date range, not with how much history a ticker happens to have.

    Returns the number of (ticker, evaluated_date) results stored."""
    if end_date is None:
        end_date = dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)
    if start_date is None:
        start_date = end_date - dt.timedelta(days=DEFAULT_BACKTEST_DAYS)
    if start_date > end_date:
        raise ValueError("start_date must not be after end_date")
    if min_history_days < 2:
        raise ValueError("min_history_days must be at least 2")

    query = (
        select(OhlcBar.ticker, OhlcBar.timestamp, OhlcBar.close)
        .where(
            OhlcBar.multiplier == multiplier,
            OhlcBar.timespan == timespan,
            # Same bad-$0-print guard as jobs/predict_market_state.py.
            OhlcBar.close > 0,
        )
        .order_by(OhlcBar.ticker, OhlcBar.timestamp)
    )
    query = _apply_ticker_filter(query, ticker_types, tickers)
    rows = session.execute(query).all()

    # Every date any ticker in the fetched set actually traded on - the market's real
    # trading calendar as observed in already-synced data, not a calendar-arithmetic
    # guess. Used below to catch a ticker's own gaps (halts, thin data) so a skipped
    # session isn't mistaken for a fair next-day evaluation.
    all_trading_dates = sorted({row.timestamp.date() for row in rows})

    computed_at = dt.datetime.now(dt.timezone.utc)
    stored = 0
    skipped = 0
    skipped_gaps = 0
    for ticker, group in groupby(rows, key=lambda row: row.ticker):
        if control is not None:
            control.checkpoint_sync()

        bars = list(group)
        closes = [bar.close for bar in bars]
        dates = [bar.timestamp.date() for bar in bars]
        if len(closes) < min_history_days + 2:
            skipped += 1
            continue

        # all_returns[k] is the return realized moving from closes[k] to closes[k + 1] -
        # i.e. the return "on" dates[k + 1].
        all_returns = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]

        ticker_stored = False
        for i in range(min_history_days, len(closes) - 1):
            evaluated_date = dates[i + 1]
            if evaluated_date < start_date:
                continue
            if evaluated_date > end_date:
                break

            # dates[i + 1] is only a fair "next session" outcome for dates[i] if no
            # other ticker traded in between - otherwise this ticker sat out a real
            # trading day (halt, thin data) and closes[i + 1] is actually a multi-day
            # return being scored as if it were one session.
            if evaluated_date != _next_trading_day(dates[i], all_trading_dates):
                skipped_gaps += 1
                continue

            # Fit only on history through as_of_date (dates[i]) - evaluated_date's own
            # outcome (all_returns[i]) never enters the cut points or transition matrix
            # it's about to be scored against.
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

            entry_price = closes[i]
            expected_return = bucket_means[predicted_state]
            predicted_exit_price = entry_price * (1 + expected_return)

            actual_state = _bucket_one(all_returns[i], cut_points)
            actual_exit_price = closes[i + 1]

            values = {
                "ticker": ticker,
                "evaluated_date": evaluated_date,
                "as_of_date": dates[i],
                "current_state": STATE_LABELS[current_state],
                "predicted_state": STATE_LABELS[predicted_state],
                "actual_state": STATE_LABELS[actual_state],
                "predicted_correct": predicted_state == actual_state,
                "state_confidence": confidence,
                "expected_return": expected_return,
                "entry_price": entry_price,
                "predicted_exit_price": predicted_exit_price,
                "actual_exit_price": actual_exit_price,
                "price_error_pct": (predicted_exit_price - actual_exit_price) / actual_exit_price,
                "history_days": i,
                "computed_at": computed_at,
            }
            stmt = sqlite_insert(MarketPredictionBacktest).values(**values)
            stmt = stmt.on_conflict_do_update(
                index_elements=[MarketPredictionBacktest.ticker, MarketPredictionBacktest.evaluated_date],
                set_=values,
            )
            session.execute(stmt)
            stored += 1
            ticker_stored = True

        if not ticker_stored:
            skipped += 1
    session.commit()

    logger.info(
        "backtested market state for %d result(s) between %s and %s, skipped %d ticker(s) with no evaluable "
        "day and %d day-gap(s) where the ticker didn't trade on the actual next session",
        stored,
        start_date,
        end_date,
        skipped,
        skipped_gaps,
    )
    return stored
