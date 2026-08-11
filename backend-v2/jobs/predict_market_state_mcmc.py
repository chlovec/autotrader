"""Runs a Monte Carlo simulation over jobs/predict_market_state.py's fitted per-ticker
Markov chain to produce a next-session exit-price *distribution* (not a single point
estimate), and upserts a summary of that distribution - percentile bands, mean, std -
into the market_predictions_mcmc table.

Purely local aggregation over already-synced bars - no massive.com call involved, same
reasoning as jobs/predict_market_state.py, whose STATE_LABELS/quantile-bucketing/
transition-matrix helpers this module reuses directly rather than refitting its own.

This is Monte Carlo simulation *of* an already-fitted Markov chain, not "MCMC" in the
stricter Metropolis-Hastings/Gibbs-sampling sense (those sample from an otherwise
intractable posterior; there's no such posterior here - the transition matrix is fully
known once fit). The job/module names use "mcmc" as that's the term this was requested
under, but what actually happens is: for each of num_simulations independent paths,
draw a next state weighted by the fitted transition probabilities
(predict_market_state._transition_row) rather than always taking the single most likely
one, as jobs/predict_market_state.py's argmax does, then draw that state's return from
Normal(bucket_mean, bucket_std) - explicitly incorporating the bucket's own historical
spread, not just its mean. Aggregating the resulting num_simulations simulated exit
prices gives a real, sampled price distribution to report a confidence interval from,
rather than the single deterministic projection jobs/predict_market_state.py produces.

Like jobs/predict_market_state_10_day.py, prediction_date is caller-chosen (dashboard
default: tomorrow, UTC) and the chain is fit only on ohlc_bars strictly before it.
"""

import datetime as dt
import logging
import random
import statistics
from itertools import groupby

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from db.models import MarketPredictionMonteCarlo, OhlcBar
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
    _transition_row,
)

logger = logging.getLogger("backend_v2.jobs.predict_market_state_mcmc")

# Default number of simulated paths per ticker when JobConfig.mcmc_num_simulations is
# left unset - resolved at run time, same "don't freeze a config-creation default"
# reasoning as jobs/average_volume.py's DEFAULT_DAYS_INTERVAL.
DEFAULT_NUM_SIMULATIONS = 2000

# Commits every this-many tickers instead of once at the very end - same reasoning as
# jobs/predict_market_state.py's COMMIT_BATCH_SIZE.
COMMIT_BATCH_SIZE = 500


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Linear-interpolated percentile (0-100) over an already-sorted, non-empty list -
    used to summarize the simulated exit-price distribution."""
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (pct / 100) * (len(sorted_values) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = rank - lo
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac


def compute_market_state_mcmc_predictions(
    session: Session,
    prediction_date: dt.date | None = None,
    ticker_types: list[str] | None = None,
    tickers: list[str] | None = None,
    min_history_days: int = DEFAULT_MIN_HISTORY_DAYS,
    multiplier: int = DEFAULT_MULTIPLIER,
    timespan: str = DEFAULT_TIMESPAN,
    num_simulations: int = DEFAULT_NUM_SIMULATIONS,
    control: JobControl | None = None,
    rng: random.Random | None = None,
) -> int:
    """`prediction_date` defaults to tomorrow (UTC) - resolved at run time, same
    "don't freeze into a stale config-creation default" reasoning as
    jobs/predict_market_state_10_day.py's start_date.

    Runs off the event loop (see jobs/engine.py's run_job, which calls this via
    asyncio.to_thread) - control.checkpoint_sync is called once per ticker, same
    granularity as jobs/predict_market_state.py.

    Fetches every matching ticker's full close-price history strictly before
    prediction_date in one ordered query - same reasoning as
    jobs/predict_market_state_10_day.py's single grouped query, so the fit never sees
    data that wouldn't actually be available yet as of prediction_date.

    For each qualifying ticker, runs num_simulations independent paths: each draws a
    next state weighted by the fitted transition probabilities
    (predict_market_state._transition_row) rather than always the most likely one, then
    draws that state's return from Normal(bucket_mean, bucket_std). Aggregates the
    resulting simulated exit prices into mean/std/p10/p50/p90 plus a single-point
    exit_price (entry_price * (1 + expected_return), same formula as the deterministic
    job's) and exit_price_confidence (1 - std/mean, a distribution-tightness score
    distinct from state_confidence), and the simulated states into predicted_state (the
    modal one) plus state_confidence (its frequency / num_simulations).

    `rng` defaults to a fresh random.Random() - accepts a seeded instance so callers
    (tests) can get deterministic output.

    Returns the number of tickers a prediction was stored for (skips tickers with fewer
    than min_history_days + 1 qualifying bars)."""
    if prediction_date is None:
        prediction_date = dt.datetime.now(dt.timezone.utc).date() + dt.timedelta(days=1)
    if min_history_days < 2:
        raise ValueError("min_history_days must be at least 2")
    if num_simulations < 1:
        raise ValueError("num_simulations must be at least 1")
    if rng is None:
        rng = random.Random()

    # Bars are timestamped at their own trading day (no later) - comparing against
    # that day's midnight is equivalent to "every bar dated strictly before
    # prediction_date" regardless of what time-of-day component a daily bar happens to
    # carry - same reasoning as jobs/predict_market_state_10_day.py's cutoff.
    cutoff = dt.datetime.combine(prediction_date, dt.time.min)

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

    state_range = range(len(STATE_LABELS))
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
        row = _transition_row(counts, current_state, states)

        entry_price = closes[-1]
        state_counts = [0] * len(STATE_LABELS)
        exit_prices: list[float] = []
        for _ in range(num_simulations):
            sim_state = rng.choices(state_range, weights=row)[0]
            sim_return = rng.gauss(bucket_means[sim_state], bucket_stds[sim_state])
            exit_prices.append(entry_price * (1 + sim_return))
            state_counts[sim_state] += 1

        predicted_state = max(state_range, key=lambda i: state_counts[i])
        state_confidence = state_counts[predicted_state] / num_simulations
        exit_prices.sort()
        exit_price_mean = statistics.mean(exit_prices)
        exit_price_std = statistics.stdev(exit_prices) if num_simulations >= 2 else 0.0
        expected_return = exit_price_mean / entry_price - 1
        # 1 - coefficient of variation - a 0-1 tightness score (can go negative if std
        # exceeds mean) distinct from state_confidence, which scores the predicted
        # *state* rather than the simulated price spread. 0.0 rather than a
        # ZeroDivisionError on the (implausible for a real price, but not impossible
        # from an unbounded Gaussian draw) exit_price_mean == 0 case.
        exit_price_confidence = 1 - (exit_price_std / exit_price_mean) if exit_price_mean != 0 else 0.0

        values = {
            "ticker": ticker,
            "predicted_date": prediction_date,
            "current_state": STATE_LABELS[current_state],
            "predicted_state": STATE_LABELS[predicted_state],
            "state_confidence": state_confidence,
            "expected_return": expected_return,
            "entry_price": entry_price,
            # Same formula as MarketPrediction.exit_price - algebraically identical to
            # exit_price_mean (both reduce to entry_price * exit_price_mean / entry_price),
            # kept as its own column for consumers that want a single point estimate
            # shaped like the deterministic job's table.
            "exit_price": entry_price * (1 + expected_return),
            "exit_price_mean": exit_price_mean,
            "exit_price_std": exit_price_std,
            "exit_price_confidence": exit_price_confidence,
            "exit_price_p10": _percentile(exit_prices, 10),
            "exit_price_p50": _percentile(exit_prices, 50),
            "exit_price_p90": _percentile(exit_prices, 90),
            "entry_time": ENTRY_TIME,
            "exit_time": EXIT_TIME,
            "num_simulations": num_simulations,
            "history_days": len(returns),
            "computed_at": computed_at,
        }
        stmt = sqlite_insert(MarketPredictionMonteCarlo).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[MarketPredictionMonteCarlo.ticker, MarketPredictionMonteCarlo.predicted_date],
            set_=values,
        )
        session.execute(stmt)
        stored += 1
        if stored % COMMIT_BATCH_SIZE == 0:
            session.commit()
    session.commit()

    logger.info(
        "Monte Carlo predicted market state for %d ticker(s) targeting %s with %d simulation(s) each, "
        "skipped %d with under %d day(s) of history",
        stored,
        prediction_date,
        num_simulations,
        skipped,
        min_history_days,
    )
    return stored
