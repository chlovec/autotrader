"""Aggregates each ticker's Markov-chain and Monte Carlo market-state predictions
(market_predictions/market_predictions_mcmc) against what actually happened
(ohlc_bars.pcnt_increase/close on the predicted date), and upserts one row per ticker
into the win_rates table - see db/models.py's WinRate.

Scores two independent notions of a Monte Carlo "win" over the same evaluable
(ticker, predicted_date) pairs: direction agreement (same as the Markov chain's own
win, see _is_win below) and, separately, whether the actual close fell within the
simulated exit-price distribution's own confidence interval (mean +/- Z * std - see
_mcmc_range_win below) - see db/models.py's WinRate.mcmc_range_win_rate for why
they're kept independent rather than folded into one number.

Purely local aggregation over already-computed predictions and already-synced bars -
no massive.com call involved, same reasoning as jobs/average_volume.py.
"""

import datetime as dt
import logging
import statistics

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from db.models import MarketPrediction, MarketPredictionMonteCarlo, OhlcBar, Ticker, WinRate
from jobs.control import JobControl

logger = logging.getLogger("backend_v2.jobs.win_rates")

DEFAULT_MULTIPLIER = 1
DEFAULT_TIMESPAN = "day"

# Two-sided confidence level for the MCMC range win check below (see
# db/models.py's WinRate.mcmc_range_win_rate) - overridable via JobConfig.
# win_rate_mcmc_range_confidence_level, same "left None, resolved by the job module"
# convention every other optional JobConfig field follows.
DEFAULT_MCMC_RANGE_CONFIDENCE_LEVEL = 0.95

# Commits every this-many tickers instead of once at the very end - same reasoning as
# jobs/average_volume.py's COMMIT_BATCH_SIZE.
COMMIT_BATCH_SIZE = 500


def _apply_ticker_filter(query, ticker_types: list[str] | None, tickers: list[str] | None):
    """Same shape as jobs/average_volume.py's/jobs/predict_market_state.py's own
    _apply_ticker_filter, filtering MarketPrediction.ticker instead of OhlcBar.ticker -
    this query is built off market_predictions, not ohlc_bars (see compute_win_rates)."""
    if tickers and ticker_types:
        raise ValueError("specify tickers or ticker_types, not both")
    if tickers:
        return query.where(MarketPrediction.ticker.in_(tickers))
    if ticker_types:
        return query.where(MarketPrediction.ticker.in_(select(Ticker.ticker).where(Ticker.type.in_(ticker_types))))
    return query


def compute_win_rates(
    session: Session,
    ticker_types: list[str] | None = None,
    tickers: list[str] | None = None,
    multiplier: int = DEFAULT_MULTIPLIER,
    timespan: str = DEFAULT_TIMESPAN,
    mcmc_range_confidence_level: float = DEFAULT_MCMC_RANGE_CONFIDENCE_LEVEL,
    control: JobControl | None = None,
) -> int:
    """For each (ticker, predicted_date) in market_predictions whose actual outcome is
    already known - an ohlc_bars row already synced for that ticker on predicted_date -
    scores whether the predicted direction (expected_return's sign) agreed with the
    actual direction realized (ohlc_bars.pcnt_increase's sign): a "win". Scores both
    the Markov chain prediction itself (market_predictions.expected_return) and, where
    a matching (ticker, predicted_date) row exists, the paired Monte Carlo prediction
    (market_predictions_mcmc.expected_return) - a market_predictions row with no
    matching Monte Carlo row still counts toward mcmc_predictions_count as a loss
    (there's no expected_return to agree in sign with anything), same as the raw SQL
    this mirrors (see db/models.py's WinRate docstring).

    Over that same evaluable set, also scores a second, independent Monte Carlo "win":
    whether the actual close (ohlc_bars.close, not pcnt_increase) on predicted_date
    fell within [exit_price_mean - Z*exit_price_std, exit_price_mean + Z*exit_price_std] -
    the simulated exit-price distribution's own confidence interval at
    mcmc_range_confidence_level (e.g. 0.95 for a 95% interval; must be strictly
    between 0 and 1), Z being that level's two-sided normal quantile
    (statistics.NormalDist().inv_cdf(0.5 + mcmc_range_confidence_level / 2)). A
    market_predictions row with no matching Monte Carlo row can't be range-scored at
    all (there's no distribution to check against) - same "counts toward
    mcmc_predictions_count as a loss" treatment as the direction-based mcmc win above,
    sharing that same count rather than a second one of its own.

    A prediction with no known actual outcome yet (predicted_date hasn't synced in
    ohlc_bars) contributes to neither the win count nor the predictions count - it's
    simply not yet evaluable, not a loss.

    Groups by ticker and upserts one win_rates row per ticker - a re-run overwrites the
    prior row, same semantics as jobs/average_volume.py's compute_average_volume.

    Runs off the event loop (see app/main.py's _run_job) as a single grouped query, so
    control.checkpoint_sync is checked once up front rather than per ticker, same
    granularity as compute_average_volume."""
    if not 0 < mcmc_range_confidence_level < 1:
        raise ValueError("mcmc_range_confidence_level must be strictly between 0 and 1")
    if control is not None:
        control.checkpoint_sync()

    range_z = statistics.NormalDist().inv_cdf(0.5 + mcmc_range_confidence_level / 2)

    actual_positive = OhlcBar.pcnt_increase >= 0
    actual_negative = OhlcBar.pcnt_increase <= 0

    def _is_win(expected_return):
        # Same-side-of-zero agreement between predicted and actual direction - the
        # union of the raw SQL's 'WON' (both <= 0) and 'WIN' (both >= 0) branches;
        # NULL (no actual outcome, or no matching prediction row) never satisfies
        # either comparison, so this falls through to "not a win" via ordinary SQL
        # three-valued logic.
        return or_(
            and_(actual_negative, expected_return <= 0),
            and_(actual_positive, expected_return >= 0),
        )

    has_actual = case((OhlcBar.pcnt_increase.is_not(None), 1), else_=0)
    # NULL-safe the same way _is_win is: if exit_price_mean/exit_price_std/OhlcBar.close
    # is NULL (no MCMC match, or no actual outcome yet), .between() itself evaluates to
    # NULL, which case() below treats as "not a win" via ordinary SQL three-valued logic.
    is_range_win = OhlcBar.close.between(
        MarketPredictionMonteCarlo.exit_price_mean - range_z * MarketPredictionMonteCarlo.exit_price_std,
        MarketPredictionMonteCarlo.exit_price_mean + range_z * MarketPredictionMonteCarlo.exit_price_std,
    )
    query = (
        select(
            MarketPrediction.ticker,
            func.sum(case((_is_win(MarketPrediction.expected_return), 1), else_=0)),
            func.sum(has_actual),
            func.sum(case((_is_win(MarketPredictionMonteCarlo.expected_return), 1), else_=0)),
            func.sum(has_actual),
            func.sum(case((is_range_win, 1), else_=0)),
        )
        .select_from(MarketPrediction)
        .outerjoin(
            MarketPredictionMonteCarlo,
            and_(
                MarketPredictionMonteCarlo.ticker == MarketPrediction.ticker,
                MarketPredictionMonteCarlo.predicted_date == MarketPrediction.predicted_date,
            ),
        )
        .outerjoin(
            OhlcBar,
            and_(
                OhlcBar.ticker == MarketPrediction.ticker,
                OhlcBar.multiplier == multiplier,
                OhlcBar.timespan == timespan,
                func.date(OhlcBar.timestamp) == MarketPrediction.predicted_date,
            ),
        )
        .group_by(MarketPrediction.ticker)
    )
    query = _apply_ticker_filter(query, ticker_types, tickers)
    rows = session.execute(query).all()

    last_updated = dt.datetime.utcnow()
    stored = 0
    for (
        ticker,
        markov_win_count,
        markov_predictions_count,
        mcmc_win_count,
        mcmc_predictions_count,
        mcmc_range_win_count,
    ) in rows:
        values = {
            "ticker": ticker,
            "last_updated": last_updated,
            "markov_win_count": markov_win_count,
            "markov_predictions_count": markov_predictions_count,
            "markov_win_rate": (markov_win_count / markov_predictions_count if markov_predictions_count else None),
            "mcmc_win_count": mcmc_win_count,
            "mcmc_predictions_count": mcmc_predictions_count,
            "mcmc_win_rate": (mcmc_win_count / mcmc_predictions_count if mcmc_predictions_count else None),
            "mcmc_range_win_count": mcmc_range_win_count,
            "mcmc_range_win_rate": (
                mcmc_range_win_count / mcmc_predictions_count if mcmc_predictions_count else None
            ),
            "mcmc_range_confidence_level": mcmc_range_confidence_level,
        }
        stmt = sqlite_insert(WinRate).values(**values)
        stmt = stmt.on_conflict_do_update(index_elements=[WinRate.ticker], set_=values)
        session.execute(stmt)
        stored += 1
        if stored % COMMIT_BATCH_SIZE == 0:
            session.commit()
    session.commit()

    logger.info("computed win rates for %d ticker(s)", stored)
    return stored
