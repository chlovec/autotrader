"""Scores each of the four prediction sources' (Markov/MarketPrediction, Monte
Carlo/MarketPredictionMonteCarlo, LSTM holdout/LstmInference, LSTM walk-forward/
LstmInference) predicted exit price against what actually happened, for every
(ticker, predicted_date) whose actual outcome is already known (an ohlc_bars row
synced for predicted_date) - the accuracy counterpart to
jobs/predict_lstm_market_state.py's/jobs/predict_market_state.py's/jobs/
predict_market_state_mcmc.py's own predictions, and to app/main.py's
prediction_comparison_report, which shows those same four sources' raw predictions
rather than scoring them.

Purely local aggregation over already-computed predictions and already-synced bars -
no massive.com call involved, same reasoning as jobs/win_rates.py.
"""

import datetime as dt
import logging
import statistics
from itertools import groupby

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from db.models import LstmInference, MarketPrediction, MarketPredictionMonteCarlo, OhlcBar, PredictionAccuracy, Ticker
from jobs.control import JobControl
from jobs.predict_market_state import DEFAULT_MIN_HISTORY_DAYS, DEFAULT_MULTIPLIER, DEFAULT_TIMESPAN

logger = logging.getLogger("backend_v2.jobs.prediction_accuracy")

# Actual price within this many standard deviations of predicted price = a pass -
# overridable via JobConfig.prediction_accuracy_pass_threshold_std, same "left None,
# resolved by the job module" convention every other optional JobConfig field follows.
DEFAULT_PASS_THRESHOLD_STD = 1.0

# Commits every this-many (ticker, predicted_date) results instead of once at the very
# end - same reasoning as jobs/predict_market_state.py's COMMIT_BATCH_SIZE.
COMMIT_BATCH_SIZE = 500

# One entry per prediction_accuracy column-quadruple prefix, in the order those
# quadruples are stored/reported in - shared by compute_prediction_accuracy (which
# columns to fill from which source table) and app/main.py's prediction accuracy
# report (which prefixes to serialize).
SOURCE_PREFIXES = ("markov", "mcmc", "lstm_holdout", "lstm_walkforward")


def _apply_ticker_filter(query, ticker_types: list[str] | None, tickers: list[str] | None):
    """Same shape as jobs/win_rates.py's own _apply_ticker_filter, against Ticker
    directly - every query in this module is driven off Ticker or already includes it,
    unlike win_rates.py's own market_predictions-driven query."""
    if tickers and ticker_types:
        raise ValueError("specify tickers or ticker_types, not both")
    if tickers:
        return query.where(Ticker.ticker.in_(tickers))
    if ticker_types:
        return query.where(Ticker.type.in_(ticker_types))
    return query


def compute_prediction_accuracy(
    session: Session,
    pass_threshold_std: float = DEFAULT_PASS_THRESHOLD_STD,
    ticker_types: list[str] | None = None,
    tickers: list[str] | None = None,
    multiplier: int = DEFAULT_MULTIPLIER,
    timespan: str = DEFAULT_TIMESPAN,
    control: JobControl | None = None,
) -> int:
    """For every (ticker, predicted_date) pair appearing in any of MarketPrediction/
    MarketPredictionMonteCarlo/LstmInference (both training_methods) whose
    predicted_date already has a matching ohlc_bars row (the actual close is known),
    scores each of the four sources' own predicted exit_price (null if that source has
    no prediction for this pair) against that actual close, storing one
    prediction_accuracy row per pair with all four side by side - see
    db/models.py's PredictionAccuracy docstring for exactly what each column means.

    The standard deviation each source is graded against (PredictionAccuracy.price_std)
    is this ticker's own historical close-to-close return standard deviation, fit only
    on bars strictly before predicted_date (no lookahead - same causal-cutoff
    discipline jobs/backtest_market_state.py's own walk-forward evaluation already
    uses) and requiring at least DEFAULT_MIN_HISTORY_DAYS returns, same minimum every
    other prediction job already requires before trusting a fit. A pair whose ticker
    doesn't have that much history before predicted_date - or no bar immediately
    preceding predicted_date to use as entry_price - isn't evaluable and is skipped
    (not stored), same "not yet evaluable, not a failure" reasoning as
    jobs/win_rates.py's own unevaluable-prediction handling.

    Runs off the event loop (see jobs/engine.py's run_job) - control.checkpoint_sync is
    called once per ticker, same granularity as jobs/win_rates.py's own
    compute_win_rates and jobs/predict_market_state.py's compute_market_state_predictions.

    Returns the number of (ticker, predicted_date) results stored."""
    if control is not None:
        control.checkpoint_sync()

    # Every (ticker, predicted_date) pair any of the four sources has a prediction
    # for, and (for markov/mcmc/lstm_holdout/lstm_walkforward respectively) that
    # source's own predicted exit_price - fetched as plain dicts rather than joined
    # in SQL, since a given pair may have anywhere from one to all four sources
    # present and an outer join across all four (like prediction_comparison_report's)
    # would need the same Ticker-driven shape this module doesn't otherwise need.
    markov_query = _apply_ticker_filter(
        select(MarketPrediction.ticker, MarketPrediction.predicted_date, MarketPrediction.exit_price).join(
            Ticker, Ticker.ticker == MarketPrediction.ticker
        ),
        ticker_types,
        tickers,
    )
    mcmc_query = _apply_ticker_filter(
        select(
            MarketPredictionMonteCarlo.ticker, MarketPredictionMonteCarlo.predicted_date, MarketPredictionMonteCarlo.exit_price
        ).join(Ticker, Ticker.ticker == MarketPredictionMonteCarlo.ticker),
        ticker_types,
        tickers,
    )
    lstm_holdout_query = _apply_ticker_filter(
        select(LstmInference.ticker, LstmInference.predicted_date, LstmInference.exit_price)
        .join(Ticker, Ticker.ticker == LstmInference.ticker)
        .where(LstmInference.training_method == "holdout"),
        ticker_types,
        tickers,
    )
    lstm_walkforward_query = _apply_ticker_filter(
        select(LstmInference.ticker, LstmInference.predicted_date, LstmInference.exit_price)
        .join(Ticker, Ticker.ticker == LstmInference.ticker)
        .where(LstmInference.training_method == "walkforward"),
        ticker_types,
        tickers,
    )

    predicted_prices: dict[str, dict[tuple[str, dt.date], float]] = {
        prefix: {(row.ticker, row.predicted_date): row.exit_price for row in session.execute(query).all()}
        for prefix, query in zip(
            SOURCE_PREFIXES, (markov_query, mcmc_query, lstm_holdout_query, lstm_walkforward_query), strict=True
        )
    }

    evaluable_pairs: dict[str, set[dt.date]] = {}
    for prefix in SOURCE_PREFIXES:
        for ticker, predicted_date in predicted_prices[prefix]:
            evaluable_pairs.setdefault(ticker, set()).add(predicted_date)

    if not evaluable_pairs:
        logger.info("no predictions from any of the four sources to score")
        return 0

    # Already scoped to exactly the tickers that survived ticker_types/tickers above
    # (evaluable_pairs' keys) - no need to re-apply _apply_ticker_filter/join Ticker
    # again here.
    bars_query = (
        select(OhlcBar.ticker, OhlcBar.timestamp, OhlcBar.close)
        .where(
            OhlcBar.multiplier == multiplier,
            OhlcBar.timespan == timespan,
            # Same bad-$0-print guard as jobs/predict_market_state.py.
            OhlcBar.close > 0,
            OhlcBar.ticker.in_(evaluable_pairs.keys()),
        )
        .order_by(OhlcBar.ticker, OhlcBar.timestamp)
    )
    bar_rows = session.execute(bars_query).all()

    computed_at = dt.datetime.utcnow()
    stored = 0
    for ticker, group in groupby(bar_rows, key=lambda row: row.ticker):
        if control is not None:
            control.checkpoint_sync()

        predicted_dates = evaluable_pairs.get(ticker)
        if not predicted_dates:
            continue

        bars = list(group)
        closes = [bar.close for bar in bars]
        dates = [bar.timestamp.date() for bar in bars]
        date_to_index = {d: i for i, d in enumerate(dates)}

        # all_returns[m] is the return realized moving from closes[m] to closes[m + 1] -
        # i.e. the return "on" dates[m + 1] - same indexing convention as
        # jobs/backtest_market_state.py's own all_returns.
        all_returns = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]

        for predicted_date in sorted(predicted_dates):
            predicted_idx = date_to_index.get(predicted_date)
            if predicted_idx is None:
                continue  # actual not yet synced - not evaluable
            entry_idx = predicted_idx - 1
            if entry_idx < DEFAULT_MIN_HISTORY_DAYS:
                continue  # no entry price, or not enough history to fit a std

            entry_price = closes[entry_idx]
            actual_exit_price = closes[predicted_idx]
            # Fit only on history through entry_idx (all_returns[:entry_idx], i.e.
            # returns realized on dates[1]..dates[entry_idx]) - predicted_date's own
            # outcome never enters the std it's about to be scored against, same
            # no-lookahead discipline as jobs/backtest_market_state.py's own
            # history_returns.
            history_returns = all_returns[:entry_idx]
            return_std = statistics.stdev(history_returns)
            price_std = entry_price * return_std

            values: dict = {
                "ticker": ticker,
                "predicted_date": predicted_date,
                "actual_exit_price": actual_exit_price,
                "price_std": price_std,
                "history_days": len(history_returns),
                "pass_threshold_std": pass_threshold_std,
                "computed_at": computed_at,
            }
            for prefix in SOURCE_PREFIXES:
                predicted_price = predicted_prices[prefix].get((ticker, predicted_date))
                if predicted_price is None:
                    values[f"{prefix}_predicted_exit_price"] = None
                    values[f"{prefix}_error"] = None
                    values[f"{prefix}_error_std"] = None
                    values[f"{prefix}_passed"] = None
                    continue
                error = actual_exit_price - predicted_price
                error_std = abs(error) / price_std if price_std > 0 else None
                values[f"{prefix}_predicted_exit_price"] = predicted_price
                values[f"{prefix}_error"] = error
                values[f"{prefix}_error_std"] = error_std
                values[f"{prefix}_passed"] = error_std is not None and error_std <= pass_threshold_std

            stmt = sqlite_insert(PredictionAccuracy).values(**values)
            stmt = stmt.on_conflict_do_update(
                index_elements=[PredictionAccuracy.ticker, PredictionAccuracy.predicted_date], set_=values
            )
            session.execute(stmt)
            stored += 1
            if stored % COMMIT_BATCH_SIZE == 0:
                session.commit()
    session.commit()

    logger.info("scored prediction accuracy for %d (ticker, predicted_date) result(s)", stored)
    return stored
