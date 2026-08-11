"""Fits a first-order Markov chain per ticker on its own history of discretized daily
ohlc_bars returns, and upserts each selected ticker's next-session state prediction -
along with a projected entry/exit price - into the market_predictions table.

Purely local aggregation over already-synced bars - no massive.com call involved, so
like jobs/average_volume.py this needs no DataClient or thread pool.

This is deliberately a *daily* prediction: ohlc_bars only carries one bar per ticker
per day (see db/models.py's OhlcBar), so there's no intraday price path to fit a state
transition on, and no timestamp finer than a trading day to predict an entry/exit time
from. entry_time/exit_time in the output are therefore fixed to the regular session's
open/close, not a model output - see MarketPrediction's docstring in db/models.py.

`prediction_date` is caller-chosen (dashboard default: tomorrow, UTC) and the chain is
fit only on ohlc_bars strictly before it - same semantics as
jobs/predict_market_state_mcmc.py's own prediction_date, which jobs/engine.py's run_job
resolves once per run and feeds to both this module's compute_market_state_predictions
and that one's compute_market_state_mcmc_predictions in turn (the Markov chain first,
then the Monte Carlo simulation over it), so a single run's two stored predictions
always target the same session.
"""

import bisect
import datetime as dt
import logging
import statistics
from itertools import groupby

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from db.models import MarketPrediction, OhlcBar, Ticker
from jobs.control import JobControl

logger = logging.getLogger("backend_v2.jobs.predict_market_state")

DEFAULT_MULTIPLIER = 1
DEFAULT_TIMESPAN = "day"

# Quintile buckets over each ticker's own historical daily return distribution -
# relative to its own volatility rather than a fixed absolute-percent threshold, so a
# low-volatility ticker's "strong_up" doesn't require the same move as a high-volatility
# one's.
STATE_LABELS = ["strong_down", "down", "flat", "up", "strong_up"]

# A ticker needs at least this many daily returns (min_history_days + 1 bars, since the
# first bar produces no return) before its transition matrix is fit on more than a
# handful of observations - short of that, quantile buckets end up noisy. ohlc_bars'
# per-ticker bar counts range from 1 to ~500 (see the sizing discussion earlier in this
# conversation), so plenty of tickers will be skipped rather than given an unreliable
# prediction.
DEFAULT_MIN_HISTORY_DAYS = 60

# prediction_date's fallback offset from today (UTC) when JobConfig.
# predicted_date_offset_days is left unset - "tomorrow", same default
# jobs/predict_market_state_mcmc.py's own prediction_date already used before the two
# jobs shared this one offset field (see jobs/registry.py's has_predicted_date_offset_field).
DEFAULT_PREDICTED_DATE_OFFSET_DAYS = 1

# Regular US equity session, fixed - see this module's docstring.
ENTRY_TIME = "09:30:00"
EXIT_TIME = "16:00:00"

# Commits every this-many tickers instead of once at the very end, so a full run (tens
# of thousands of tickers) doesn't hold a single write transaction open for its entire
# duration - see db/session.py's WAL-mode comment for why that matters even with WAL,
# since writers are still serialized against each other.
COMMIT_BATCH_SIZE = 500


def _apply_ticker_filter(query, ticker_types: list[str] | None, tickers: list[str] | None):
    """Same shape as jobs/average_volume.py's _apply_ticker_filter - filters ohlc_bars
    in SQL rather than materializing the tickers table in Python, since binding tens of
    thousands of tickers as a literal IN(...) blows past sqlite's per-statement
    variable limit."""
    if tickers and ticker_types:
        raise ValueError("specify tickers or ticker_types, not both")
    if tickers:
        return query.where(OhlcBar.ticker.in_(tickers))
    if ticker_types:
        return query.where(OhlcBar.ticker.in_(select(Ticker.ticker).where(Ticker.type.in_(ticker_types))))
    return query


def _cut_points(returns: list[float]) -> list[float]:
    """The len(STATE_LABELS)-1 quantile boundaries of `returns` - shared by
    _bucket_states (which fits and buckets the same series) and
    jobs/backtest_market_state.py (which fits cut points on history through a given
    day, then buckets the *next* day's actually-realized return against them, without
    that return itself influencing the boundaries - the walk-forward equivalent of
    "no lookahead bias")."""
    return statistics.quantiles(returns, n=len(STATE_LABELS))


def _bucket_one(value: float, cut_points: list[float]) -> int:
    return bisect.bisect_right(cut_points, value)


def _bucket_states(returns: list[float]) -> tuple[list[int], list[float], list[float]]:
    """Buckets each return into one of len(STATE_LABELS) quantiles of `returns` itself,
    and returns each bucket's historical mean and standard deviation of return
    alongside the per-return bucket assignment. A bucket with fewer than 2 historical
    returns gets std 0.0 (statistics.stdev needs at least 2 points) - a deterministic
    point mass at its mean, same "not enough data" fallback used for an empty bucket's
    mean (0.0).

    bucket_stds has no effect on this module's own argmax prediction, but is what lets
    jobs/predict_market_state_mcmc.py sample a return via Normal(mean, std) and this
    module's own compute_market_state_predictions derive exit_price_confidence, both
    from the same historical spread rather than each fitting their own."""
    cut_points = _cut_points(returns)
    buckets = [_bucket_one(r, cut_points) for r in returns]
    bucket_returns: list[list[float]] = [[] for _ in STATE_LABELS]
    for bucket, r in zip(buckets, returns):
        bucket_returns[bucket].append(r)
    bucket_means = [statistics.mean(values) if values else 0.0 for values in bucket_returns]
    bucket_stds = [statistics.stdev(values) if len(values) >= 2 else 0.0 for values in bucket_returns]
    return buckets, bucket_means, bucket_stds


def _fit_transition(states: list[int]) -> list[list[int]]:
    """counts[i][j] = number of times state i was immediately followed by state j."""
    counts = [[0] * len(STATE_LABELS) for _ in STATE_LABELS]
    for current, nxt in zip(states, states[1:]):
        counts[current][nxt] += 1
    return counts


def _transition_row(counts: list[list[int]], current_state: int, states: list[int]) -> list[int]:
    """current_state's observed transition counts, falling back to the unconditional
    (marginal) distribution of states seen historically if current_state was never
    observed as a "from" state before - e.g. the most recent day is that bucket's only
    occurrence so far. Shared by _predict_next_state's argmax below and
    jobs/predict_market_state_mcmc.py's weighted random draw - both need the same
    fallback rule, just a different way of picking a state out of the resulting row."""
    row = counts[current_state]
    if sum(row) == 0:
        row = [states.count(i) for i in range(len(STATE_LABELS))]
    return row


def _predict_next_state(counts: list[list[int]], current_state: int, states: list[int]) -> tuple[int, float]:
    """Most-likely next state out of current_state's observed transitions - see
    _transition_row for the fallback rule."""
    row = _transition_row(counts, current_state, states)
    total = sum(row)
    predicted_state = max(range(len(STATE_LABELS)), key=lambda i: row[i])
    confidence = row[predicted_state] / total
    return predicted_state, confidence


def compute_market_state_predictions(
    session: Session,
    prediction_date: dt.date | None = None,
    ticker_types: list[str] | None = None,
    tickers: list[str] | None = None,
    min_history_days: int = DEFAULT_MIN_HISTORY_DAYS,
    multiplier: int = DEFAULT_MULTIPLIER,
    timespan: str = DEFAULT_TIMESPAN,
    control: JobControl | None = None,
) -> int:
    """`prediction_date` defaults to tomorrow (UTC) - resolved at run time, same "don't
    freeze into a stale config-creation default" reasoning as
    jobs/predict_market_state_mcmc.py's own prediction_date.

    Runs off the event loop (see jobs/engine.py's run_job, which calls this via
    asyncio.to_thread) - control.checkpoint_sync is called once per ticker, the unit of
    work here, same granularity jobs/sync_indicators.py checkpoints at between tickers.

    Fetches every matching ticker's full close-price history strictly before
    prediction_date in one ordered query rather than one query per ticker - same
    reasoning as jobs/predict_market_state_mcmc.py's single grouped query, so the fit
    never sees data that wouldn't actually be available yet as of prediction_date.

    Returns the number of tickers a prediction was stored for (skips tickers with fewer
    than min_history_days + 1 bars)."""
    if prediction_date is None:
        prediction_date = dt.datetime.now(dt.timezone.utc).date() + dt.timedelta(
            days=DEFAULT_PREDICTED_DATE_OFFSET_DAYS
        )
    if min_history_days < 2:
        raise ValueError("min_history_days must be at least 2")

    # Bars are timestamped at their own trading day (no later) - comparing against
    # that day's midnight is equivalent to "every bar dated strictly before
    # prediction_date" regardless of what time-of-day component a daily bar happens to
    # carry - same reasoning as jobs/predict_market_state_mcmc.py's cutoff.
    cutoff = dt.datetime.combine(prediction_date, dt.time.min)

    query = (
        select(OhlcBar.ticker, OhlcBar.timestamp, OhlcBar.close)
        .where(
            OhlcBar.multiplier == multiplier,
            OhlcBar.timespan == timespan,
            # A handful of tickers have a bad $0 print in a bar (bad data or a
            # delisting artifact) - excluded rather than left in, since a $0 close
            # would otherwise divide-by-zero computing the following day's return.
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
        states, bucket_means, bucket_stds = _bucket_states(returns)
        counts = _fit_transition(states)
        current_state = states[-1]
        predicted_state, confidence = _predict_next_state(counts, current_state, states)

        entry_price = closes[-1]
        expected_return = bucket_means[predicted_state]
        exit_price = entry_price * (1 + expected_return)
        # 1 - coefficient of variation of the predicted state's historical returns,
        # linearly rescaled from return-space to price-space (exit_price =
        # entry_price * (1 + expected_return), so a return std of bucket_stds[predicted_state]
        # corresponds to a price std of entry_price * bucket_stds[predicted_state], and
        # dividing that by exit_price cancels entry_price out to
        # bucket_stds[predicted_state] / (1 + expected_return)) - same formula shape as
        # jobs/predict_market_state_mcmc.py's exit_price_confidence, so the two are
        # directly comparable, though this one only reflects the predicted state's own
        # return spread, not any uncertainty in *which* state was predicted (unlike the
        # Monte Carlo job's, which simulates the state draw too). 0.0 rather than a
        # ZeroDivisionError on the (extreme, but not impossible) expected_return == -1 case.
        exit_price_confidence = (
            1 - (bucket_stds[predicted_state] / (1 + expected_return)) if expected_return != -1 else 0.0
        )

        values = {
            "ticker": ticker,
            "predicted_date": prediction_date,
            "current_state": STATE_LABELS[current_state],
            "predicted_state": STATE_LABELS[predicted_state],
            "state_confidence": confidence,
            "expected_return": expected_return,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "exit_price_confidence": exit_price_confidence,
            "entry_time": ENTRY_TIME,
            "exit_time": EXIT_TIME,
            "history_days": len(returns),
            "computed_at": computed_at,
        }
        stmt = sqlite_insert(MarketPrediction).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[MarketPrediction.ticker, MarketPrediction.predicted_date],
            set_=values,
        )
        session.execute(stmt)
        stored += 1
        if stored % COMMIT_BATCH_SIZE == 0:
            session.commit()
    session.commit()

    logger.info(
        "predicted market state for %d ticker(s) targeting %s, skipped %d with under %d day(s) of history",
        stored,
        prediction_date,
        skipped,
        min_history_days,
    )
    return stored
