"""Walks jobs/predict_market_state.py's fitted per-ticker Markov chain forward 10
trading days from a caller-chosen start date, and upserts the full 10-day projection -
along with per-day projected entry/exit prices - into the market_predictions_10_day
table.

Purely local aggregation over already-synced bars - no massive.com call involved, same
reasoning as jobs/predict_market_state.py, whose STATE_LABELS/quantile-bucketing/
transition-matrix helpers this module reuses directly rather than refitting its own.

Unlike jobs/predict_market_state.py (which always predicts off the *latest* synced
bar), start_date is caller-chosen and usually in the future (dashboard default:
tomorrow, UTC) - the chain is fit only on ohlc_bars strictly before it. Day 1 is
predicted from that fit the same way predict_market_state.py's single prediction is.
Days 2-10 are NOT re-fit against new data - there isn't any yet for a future
start_date - each is a further one-step walk of the *same* transition matrix,
starting from the *previous* day's predicted state, the standard way to project a
Markov chain forward multiple steps without new observations. Entry/exit prices
compound along that same walk (day N's entry price is day N-1's exit price).
"""

import datetime as dt
import logging
from itertools import groupby

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from db.models import MarketPrediction10Day, OhlcBar
from jobs.control import JobControl
from jobs.predict_market_state import (
    DEFAULT_MIN_HISTORY_DAYS,
    DEFAULT_MULTIPLIER,
    DEFAULT_TIMESPAN,
    ENTRY_TIME,
    EXIT_TIME,
    STATE_LABELS,
    _apply_ticker_filter,
    _bucket_states,
    _fit_transition,
    _predict_next_state,
)

logger = logging.getLogger("backend_v2.jobs.predict_market_state_10_day")

# Fixed by market_predictions_10_day's schema (day1_* through day10_*) - not a runtime
# parameter, since the table has a column group per day rather than a child table.
DAYS_AHEAD = 10

# Commits every this-many tickers instead of once at the very end - same reasoning as
# jobs/predict_market_state.py's COMMIT_BATCH_SIZE.
COMMIT_BATCH_SIZE = 500


def compute_10_day_market_state_predictions(
    session: Session,
    start_date: dt.date | None = None,
    ticker_types: list[str] | None = None,
    tickers: list[str] | None = None,
    min_history_days: int = DEFAULT_MIN_HISTORY_DAYS,
    multiplier: int = DEFAULT_MULTIPLIER,
    timespan: str = DEFAULT_TIMESPAN,
    control: JobControl | None = None,
) -> int:
    """`start_date` defaults to tomorrow (UTC) - resolved at run time, same "don't
    freeze into a stale config-creation default" reasoning as
    jobs/average_volume.py's compute_average_volume.

    Runs off the event loop (see jobs/engine.py's run_job, which calls this via
    asyncio.to_thread) - control.checkpoint_sync is called once per ticker, same
    granularity as jobs/predict_market_state.py.

    Fetches every matching ticker's full close-price history strictly before
    start_date in one ordered query - same reasoning as
    jobs/predict_market_state.py's single grouped query - so the fit never sees data
    that wouldn't actually be available yet as of start_date.

    Returns the number of tickers a prediction was stored for (skips tickers with
    fewer than min_history_days + 1 qualifying bars)."""
    if start_date is None:
        start_date = dt.datetime.now(dt.timezone.utc).date() + dt.timedelta(days=1)
    if min_history_days < 2:
        raise ValueError("min_history_days must be at least 2")

    # Bars are timestamped at their own trading day (no later) - comparing against
    # that day's midnight is equivalent to "every bar dated strictly before
    # start_date" regardless of what time-of-day component a daily bar happens to
    # carry.
    cutoff = dt.datetime.combine(start_date, dt.time.min)

    query = (
        select(OhlcBar.ticker, OhlcBar.timestamp, OhlcBar.close)
        .where(
            OhlcBar.multiplier == multiplier,
            OhlcBar.timespan == timespan,
            # Same bad-$0-print guard as jobs/predict_market_state.py.
            OhlcBar.close > 0,
            OhlcBar.timestamp < cutoff,
        )
        .order_by(OhlcBar.ticker, OhlcBar.timestamp)
    )
    query = _apply_ticker_filter(query, ticker_types, tickers)
    rows = session.execute(query).all()

    computed_at = dt.datetime.utcnow()
    stored = 0
    skipped = 0
    for ticker, group in groupby(rows, key=lambda row: row.ticker):
        if control is not None:
            control.checkpoint_sync()

        bars = list(group)
        closes = [bar.close for bar in bars]
        if len(closes) < min_history_days + 1:
            skipped += 1
            continue

        returns = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
        states, bucket_means = _bucket_states(returns)
        counts = _fit_transition(states)
        current_state = states[-1]

        values: dict[str, object] = {
            "ticker": ticker,
            "start_date": start_date,
            "current_state": STATE_LABELS[current_state],
            "computed_at": computed_at,
        }

        # Walked forward from current_state, one step per day - day N's "from" state
        # is day N-1's predicted state (current_state itself for day 1), and its
        # entry price is day N-1's exit price (day 1's is the last close before
        # start_date). See this module's docstring for why days 2-10 reuse the same
        # transition matrix rather than refitting.
        day_state = current_state
        day_entry_price = closes[-1]
        for day in range(1, DAYS_AHEAD + 1):
            predicted_state, confidence = _predict_next_state(counts, day_state, states)
            expected_return = bucket_means[predicted_state]
            day_exit_price = day_entry_price * (1 + expected_return)

            values[f"day{day}_predicted_state"] = STATE_LABELS[predicted_state]
            values[f"day{day}_state_confidence"] = confidence
            values[f"day{day}_entry_price"] = day_entry_price
            values[f"day{day}_exit_price"] = day_exit_price
            # A percentage (e.g. -2.5), not a fraction - unlike
            # MarketPrediction.expected_return, which is a fraction converted to a
            # percent only client-side (see db/models.py's MarketPrediction10Day
            # docstring).
            values[f"day{day}_expected_return_pct"] = expected_return * 100
            values[f"day{day}_entry_time"] = ENTRY_TIME
            values[f"day{day}_exit_time"] = EXIT_TIME

            day_state = predicted_state
            day_entry_price = day_exit_price

        stmt = sqlite_insert(MarketPrediction10Day).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[MarketPrediction10Day.ticker, MarketPrediction10Day.start_date],
            set_=values,
        )
        session.execute(stmt)
        stored += 1
        if stored % COMMIT_BATCH_SIZE == 0:
            session.commit()
    session.commit()

    logger.info(
        "predicted %d-day market state for %d ticker(s) starting %s, skipped %d with under %d day(s) of history",
        DAYS_AHEAD,
        stored,
        start_date,
        skipped,
        min_history_days,
    )
    return stored
