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


def _next_trading_day(date: dt.date) -> dt.date:
    """Calendar-naive - skips weekends but not market holidays, since there's no
    trading-calendar table to consult here. Good enough for predicted_date's purpose
    (labeling which session a prediction targets), not load-bearing for the prediction
    itself."""
    if date.weekday() == 4:  # Friday
        return date + dt.timedelta(days=3)
    if date.weekday() == 5:  # Saturday (shouldn't occur for a real trading day, but safe)
        return date + dt.timedelta(days=2)
    return date + dt.timedelta(days=1)


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


def _bucket_states(returns: list[float]) -> tuple[list[int], list[float]]:
    """Buckets each return into one of len(STATE_LABELS) quantiles of `returns` itself,
    and returns each bucket's historical mean return alongside the per-return bucket
    assignment."""
    cut_points = _cut_points(returns)
    buckets = [_bucket_one(r, cut_points) for r in returns]
    bucket_returns: list[list[float]] = [[] for _ in STATE_LABELS]
    for bucket, r in zip(buckets, returns):
        bucket_returns[bucket].append(r)
    bucket_means = [statistics.mean(values) if values else 0.0 for values in bucket_returns]
    return buckets, bucket_means


def _fit_transition(states: list[int]) -> list[list[int]]:
    """counts[i][j] = number of times state i was immediately followed by state j."""
    counts = [[0] * len(STATE_LABELS) for _ in STATE_LABELS]
    for current, nxt in zip(states, states[1:]):
        counts[current][nxt] += 1
    return counts


def _predict_next_state(counts: list[list[int]], current_state: int, states: list[int]) -> tuple[int, float]:
    """Most-likely next state out of current_state's observed transitions, falling back
    to the unconditional (marginal) distribution of states seen historically if
    current_state was never observed as a "from" state before - e.g. the most recent
    day is that bucket's only occurrence so far."""
    row = counts[current_state]
    total = sum(row)
    if total == 0:
        row = [states.count(i) for i in range(len(STATE_LABELS))]
        total = sum(row)
    predicted_state = max(range(len(STATE_LABELS)), key=lambda i: row[i])
    confidence = row[predicted_state] / total
    return predicted_state, confidence


def compute_market_state_predictions(
    session: Session,
    ticker_types: list[str] | None = None,
    tickers: list[str] | None = None,
    min_history_days: int = DEFAULT_MIN_HISTORY_DAYS,
    multiplier: int = DEFAULT_MULTIPLIER,
    timespan: str = DEFAULT_TIMESPAN,
    control: JobControl | None = None,
) -> int:
    """Runs off the event loop (see app/main.py's _run_job, which calls this via
    asyncio.to_thread) - control.checkpoint_sync is called once per ticker, the unit of
    work here, same granularity jobs/sync_indicators.py checkpoints at between tickers.

    Fetches every matching ticker's full close-price history in one ordered query
    rather than one query per ticker - same reasoning as jobs/average_volume.py's
    single grouped query, just grouped in Python (via itertools.groupby) instead of SQL
    since building a return series and quantile buckets isn't expressible as a single
    aggregate.

    Returns the number of tickers a prediction was stored for (skips tickers with fewer
    than min_history_days + 1 bars)."""
    if min_history_days < 2:
        raise ValueError("min_history_days must be at least 2")

    query = (
        select(OhlcBar.ticker, OhlcBar.timestamp, OhlcBar.close)
        .where(
            OhlcBar.multiplier == multiplier,
            OhlcBar.timespan == timespan,
            # A handful of tickers have a bad $0 print in a bar (bad data or a
            # delisting artifact) - excluded rather than left in, since a $0 close
            # would otherwise divide-by-zero computing the following day's return.
            OhlcBar.close > 0,
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
        predicted_state, confidence = _predict_next_state(counts, current_state, states)

        entry_price = closes[-1]
        expected_return = bucket_means[predicted_state]
        exit_price = entry_price * (1 + expected_return)
        predicted_date = _next_trading_day(bars[-1].timestamp.date())

        values = {
            "ticker": ticker,
            "predicted_date": predicted_date,
            "current_state": STATE_LABELS[current_state],
            "predicted_state": STATE_LABELS[predicted_state],
            "state_confidence": confidence,
            "expected_return": expected_return,
            "entry_price": entry_price,
            "exit_price": exit_price,
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
        "predicted market state for %d ticker(s), skipped %d with under %d day(s) of history",
        stored,
        skipped,
        min_history_days,
    )
    return stored
