"""Screens the common-stock ticker universe for liquid candidates with already-synced/
computed data, scores them off a composite of already-computed local signals, and
stores up to MAX_PICKS as a research shortlist for further human investigation - NOT an
autonomous trading signal. Extensive analysis of this database found the underlying
Markov/Monte Carlo predictions (market_predictions/market_predictions_mcmc) carry no
statistically meaningful directional edge on liquid stocks (~49% win rate, a coin
flip) and no correlation between a ticker's backtest track record
(market_prediction_backtests) and its live win rate (win_rates) - so every generated
comment below is phrased as "supports the predicted direction", never "will win", and
the weights below lean on win-rate/backtest track record only lightly.

Purely local: no massive.com call - every input (market_predictions,
market_predictions_mcmc, win_rates, market_prediction_backtests, ticker_details,
average_volumes, technical_indicators, news) is already synced/computed by other jobs.
No v1 Alpaca broker call either - nothing here places or simulates a trade, it only
ranks and records a shortlist.

ETFs are out of scope (see db/models.py's ResearchPick docstring) - the screen below
is restricted to Ticker.type == "CS".
"""

import datetime as dt
import json
import logging

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from db.models import (
    AverageVolume,
    MarketPrediction,
    MarketPredictionBacktest,
    MarketPredictionMonteCarlo,
    News,
    ResearchPick,
    TechnicalIndicator,
    Ticker,
    TickerDetail,
    WinRate,
)
from jobs.control import JobControl

logger = logging.getLogger("backend_v2.jobs.research_picks")

MAX_PICKS = 20

# Liquidity screen - informed by this session's finding that illiquid OTC names were
# driving spurious "high confidence" readings in the raw predictions.
DEFAULT_MIN_MARKET_CAP = 2_000_000_000.0
DEFAULT_MIN_AVERAGE_VOLUME = 1_000_000.0

# Below this many evaluated predictions/backtest rows, a ticker's win-rate/backtest
# track record is treated as neutral (0.5) rather than a real signal - too little
# history to mean anything yet.
DEFAULT_MIN_WIN_RATE_PREDICTIONS = 10
DEFAULT_MIN_BACKTEST_ROWS = 20

DEFAULT_NEWS_LOOKBACK_DAYS = 14

# RSI oversold/overbought thresholds - standard convention, not tuned against this
# dataset (RSI covers too few tickers here to tune anything against).
_RSI_OVERSOLD = 30.0
_RSI_OVERBOUGHT = 70.0
_RSI_ADJUSTMENT = 0.05

_NEWS_ADJUSTMENT_PER_ARTICLE = 0.02
_NEWS_ADJUSTMENT_CAP = 0.05

_EXPECTED_RETURN_SCORE_CAP = 0.05  # a 5%+ average predicted move maxes this component

_DISCLAIMER_CLAUSE = "Research shortlist for further investigation, not a trading signal."


def _resolve_predicted_date(session: Session) -> dt.date | None:
    return session.execute(select(func.max(MarketPrediction.predicted_date))).scalar_one_or_none()


def _screen_candidates(session: Session, predicted_date: dt.date):
    """Every join below is a plain (not outer) join, so a ticker missing any required
    row simply never appears - the join itself is the exclusion filter, same reasoning
    as compute_win_rates' _apply_ticker_filter but expressed structurally rather than
    with a WHERE clause. Ticker/MarketPrediction/MarketPredictionMonteCarlo/
    TickerDetail/latest-AverageVolume - same latest-row-per-ticker subquery pattern
    app/main.py's market_predictions_report uses for AverageVolume."""
    latest_avg_vol_dates = (
        select(AverageVolume.ticker, func.max(AverageVolume.computed_at).label("computed_at"))
        .group_by(AverageVolume.ticker)
        .subquery()
    )
    avg_vol_join = (AverageVolume.ticker == latest_avg_vol_dates.c.ticker) & (
        AverageVolume.computed_at == latest_avg_vol_dates.c.computed_at
    )
    query = (
        select(Ticker, MarketPrediction, MarketPredictionMonteCarlo, TickerDetail, AverageVolume)
        .join(
            MarketPrediction,
            and_(MarketPrediction.ticker == Ticker.ticker, MarketPrediction.predicted_date == predicted_date),
        )
        .join(
            MarketPredictionMonteCarlo,
            and_(
                MarketPredictionMonteCarlo.ticker == Ticker.ticker,
                MarketPredictionMonteCarlo.predicted_date == predicted_date,
            ),
        )
        .join(TickerDetail, TickerDetail.ticker == Ticker.ticker)
        .join(latest_avg_vol_dates, latest_avg_vol_dates.c.ticker == Ticker.ticker)
        .join(AverageVolume, avg_vol_join)
        .where(
            Ticker.type == "CS",
            Ticker.active.is_(True),
            TickerDetail.market_cap >= DEFAULT_MIN_MARKET_CAP,
            AverageVolume.average_volume >= DEFAULT_MIN_AVERAGE_VOLUME,
            # Same agreement condition as app/main.py's market_predictions_report
            # require_consensus - "agreement" means the same thing everywhere.
            MarketPrediction.predicted_state == MarketPredictionMonteCarlo.predicted_state,
            MarketPrediction.expected_return != 0,
        )
    )
    return session.execute(query).all()


def _win_rate_component(win_rate: float | None, count: int | None) -> float | None:
    if win_rate is None or count is None or count < DEFAULT_MIN_WIN_RATE_PREDICTIONS:
        return None
    return win_rate


def _rsi_adjustment(direction: str, rsi_value: float | None) -> float:
    if rsi_value is None:
        return 0.0
    if direction == "up":
        if rsi_value < _RSI_OVERSOLD:
            return _RSI_ADJUSTMENT
        if rsi_value > _RSI_OVERBOUGHT:
            return -_RSI_ADJUSTMENT
    else:
        if rsi_value > _RSI_OVERBOUGHT:
            return _RSI_ADJUSTMENT
        if rsi_value < _RSI_OVERSOLD:
            return -_RSI_ADJUSTMENT
    return 0.0


def _news_adjustment(sentiment_lean: int | None) -> float:
    if sentiment_lean is None:
        return 0.0
    return max(-_NEWS_ADJUSTMENT_CAP, min(_NEWS_ADJUSTMENT_CAP, sentiment_lean * _NEWS_ADJUSTMENT_PER_ARTICLE))


def _news_signals(session: Session, candidate_tickers: set[str]) -> dict[str, tuple[int, int]]:
    """ticker -> (article_count, sentiment_lean). Matches tickers by exact comma-token
    membership, never SQL LIKE - News.tickers is a comma-joined string, not JSON, so a
    LIKE '%AA%' would false-positive "AA" inside "AAPL". News volume in this database is
    small enough (a few thousand rows total) that fetching the whole lookback window and
    filtering in Python is simpler than a per-ticker query, and avoids that
    substring-matching bug entirely."""
    cutoff = dt.datetime.utcnow() - dt.timedelta(days=DEFAULT_NEWS_LOOKBACK_DAYS)
    rows = session.execute(select(News).where(News.published_utc >= cutoff)).scalars().all()
    counts: dict[str, int] = {}
    leans: dict[str, int] = {}
    for row in rows:
        row_tickers = {t.strip() for t in (row.tickers or "").split(",") if t.strip()}
        matched = row_tickers & candidate_tickers
        if not matched:
            continue
        insights = []
        if row.insights:
            try:
                insights = json.loads(row.insights)
            except (ValueError, TypeError):
                insights = []
        sentiment_by_ticker = {
            entry.get("ticker"): entry.get("sentiment") for entry in insights if isinstance(entry, dict)
        }
        for ticker in matched:
            counts[ticker] = counts.get(ticker, 0) + 1
            sentiment = sentiment_by_ticker.get(ticker)
            if sentiment == "positive":
                leans[ticker] = leans.get(ticker, 0) + 1
            elif sentiment == "negative":
                leans[ticker] = leans.get(ticker, 0) - 1
            else:
                leans.setdefault(ticker, 0)
    return {ticker: (count, leans.get(ticker, 0)) for ticker, count in counts.items()}


def _build_comment(
    direction: str,
    markov_er: float,
    mcmc_er: float,
    markov_conf: float,
    mcmc_conf: float,
    win_rate_values: list[float],
    backtest_hit_rate: float | None,
    backtest_n: int | None,
    rsi_value: float | None,
    rsi_adj: float,
    news_count: int | None,
    news_lean: int | None,
) -> str:
    clauses = [
        f"Markov and Monte Carlo agree on '{direction}' (expected return "
        f"{markov_er:+.1%}/{mcmc_er:+.1%}, confidence {markov_conf:.0%}/{mcmc_conf:.0%})."
    ]
    if win_rate_values:
        avg_win_rate = sum(win_rate_values) / len(win_rate_values)
        clauses.append(f"Live track record supports it: {avg_win_rate:.0%} win rate so far.")
    if backtest_hit_rate is not None and backtest_n is not None and backtest_n >= DEFAULT_MIN_BACKTEST_ROWS:
        clauses.append(
            f"Backtest track record: {backtest_hit_rate:.0%} directional accuracy over {backtest_n} evaluated day(s)."
        )
    if rsi_value is not None:
        if rsi_adj > 0:
            clauses.append(f"RSI {rsi_value:.0f} supports the predicted direction.")
        elif rsi_adj < 0:
            clauses.append(f"RSI {rsi_value:.0f} cuts against the predicted direction.")
        else:
            clauses.append(f"RSI {rsi_value:.0f} is neutral.")
    if news_count:
        lean_word = "positive" if news_lean and news_lean > 0 else "negative" if news_lean and news_lean < 0 else "mixed/neutral"
        clauses.append(f"{news_count} recent article(s) in the last {DEFAULT_NEWS_LOOKBACK_DAYS} days, sentiment lean {lean_word}.")
    clauses.append(_DISCLAIMER_CLAUSE)
    return " ".join(clauses)


def compute_research_picks(session: Session, run_id: int, control: JobControl | None = None) -> int:
    """For the most recent predicted_date market_predictions has coverage for, screens
    the CS ticker universe down to liquid candidates whose Markov and Monte Carlo
    predictions agree on direction, scores each by a composite of expected-return
    magnitude, confidence, live/backtest win-rate track record, RSI, and recent news
    sentiment, and stores the top MAX_PICKS as ResearchPick rows stamped with run_id.

    Single grouped query plus a handful of batch lookups over the (typically small)
    screened candidate set, so control.checkpoint_sync() is checked once up front,
    same granularity as jobs/win_rates.py's compute_win_rates."""
    if control is not None:
        control.checkpoint_sync()

    predicted_date = _resolve_predicted_date(session)
    if predicted_date is None:
        logger.info("no market_predictions rows exist yet - nothing to research")
        return 0

    candidates = _screen_candidates(session, predicted_date)
    if not candidates:
        logger.info("no candidates passed the liquidity/agreement screen for %s", predicted_date)
        return 0

    candidate_tickers = {ticker.ticker for ticker, *_ in candidates}

    win_rates_by_ticker = {
        row.ticker: row
        for row in session.execute(select(WinRate).where(WinRate.ticker.in_(candidate_tickers))).scalars()
    }
    backtest_rows = session.execute(
        select(
            MarketPredictionBacktest.ticker,
            func.avg(MarketPredictionBacktest.predicted_correct),
            func.count(),
        )
        .where(MarketPredictionBacktest.ticker.in_(candidate_tickers))
        .group_by(MarketPredictionBacktest.ticker)
    ).all()
    backtest_by_ticker = {ticker: (hit_rate, n) for ticker, hit_rate, n in backtest_rows}

    latest_rsi_ts = (
        select(TechnicalIndicator.ticker, func.max(TechnicalIndicator.timestamp).label("timestamp"))
        .where(TechnicalIndicator.indicator == "rsi", TechnicalIndicator.ticker.in_(candidate_tickers))
        .group_by(TechnicalIndicator.ticker)
        .subquery()
    )
    rsi_rows = session.execute(
        select(TechnicalIndicator.ticker, TechnicalIndicator.value).join(
            latest_rsi_ts,
            and_(
                TechnicalIndicator.ticker == latest_rsi_ts.c.ticker,
                TechnicalIndicator.timestamp == latest_rsi_ts.c.timestamp,
                TechnicalIndicator.indicator == "rsi",
            ),
        )
    ).all()
    rsi_by_ticker = dict(rsi_rows)

    news_by_ticker = _news_signals(session, candidate_tickers)

    scored = []
    for ticker, markov, mcmc, ticker_detail, avg_vol in candidates:
        direction = "up" if markov.expected_return > 0 else "down"

        expected_return_score = min(
            (abs(markov.expected_return) + abs(mcmc.expected_return)) / 2 / _EXPECTED_RETURN_SCORE_CAP, 1.0
        )
        confidence_score = (markov.state_confidence + mcmc.state_confidence) / 2

        win_rate_row = win_rates_by_ticker.get(ticker.ticker)
        win_rate_values = [
            v
            for v in (
                _win_rate_component(
                    win_rate_row.markov_win_rate if win_rate_row else None,
                    win_rate_row.markov_predictions_count if win_rate_row else None,
                ),
                _win_rate_component(
                    win_rate_row.mcmc_win_rate if win_rate_row else None,
                    win_rate_row.mcmc_predictions_count if win_rate_row else None,
                ),
            )
            if v is not None
        ]
        win_rate_score = sum(win_rate_values) / len(win_rate_values) if win_rate_values else 0.5

        backtest_hit_rate, backtest_n = backtest_by_ticker.get(ticker.ticker, (None, None))
        backtest_score = (
            backtest_hit_rate if backtest_hit_rate is not None and backtest_n is not None and backtest_n >= DEFAULT_MIN_BACKTEST_ROWS else 0.5
        )

        rsi_value = rsi_by_ticker.get(ticker.ticker)
        rsi_adj = _rsi_adjustment(direction, rsi_value)

        news_count, news_lean = news_by_ticker.get(ticker.ticker, (None, None))
        news_adj = _news_adjustment(news_lean if news_count else None)

        score = (
            0.40 * expected_return_score
            + 0.20 * confidence_score
            + 0.15 * win_rate_score
            + 0.10 * backtest_score
            + rsi_adj
            + news_adj
        )

        comment = _build_comment(
            direction,
            markov.expected_return,
            mcmc.expected_return,
            markov.state_confidence,
            mcmc.state_confidence,
            win_rate_values,
            backtest_hit_rate,
            backtest_n,
            rsi_value,
            rsi_adj,
            news_count,
            news_lean,
        )

        scored.append(
            {
                "ticker": ticker.ticker,
                "score": score,
                "direction": direction,
                "markov": markov,
                "mcmc": mcmc,
                "ticker_detail": ticker_detail,
                "avg_vol": avg_vol,
                "win_rate_row": win_rate_row,
                "backtest_hit_rate": backtest_hit_rate,
                "backtest_n": backtest_n,
                "rsi_value": rsi_value,
                "expected_return_score": expected_return_score,
                "confidence_score": confidence_score,
                "win_rate_score": win_rate_score,
                "backtest_score": backtest_score,
                "rsi_adj": rsi_adj,
                "news_adj": news_adj,
                "news_count": news_count,
                "news_lean": news_lean,
                "comment": comment,
            }
        )

    scored.sort(key=lambda entry: (-entry["score"], entry["ticker"]))
    picks = scored[:MAX_PICKS]

    computed_at = dt.datetime.utcnow()
    for rank, entry in enumerate(picks, start=1):
        win_rate_row = entry["win_rate_row"]
        session.add(
            ResearchPick(
                run_id=run_id,
                ticker=entry["ticker"],
                rank=rank,
                predicted_date=predicted_date,
                predicted_direction=entry["direction"],
                score=entry["score"],
                expected_return_score=entry["expected_return_score"],
                confidence_score=entry["confidence_score"],
                win_rate_score=entry["win_rate_score"],
                backtest_score=entry["backtest_score"],
                rsi_adjustment=entry["rsi_adj"],
                news_adjustment=entry["news_adj"],
                entry_price=entry["markov"].entry_price,
                markov_predicted_state=entry["markov"].predicted_state,
                markov_expected_return=entry["markov"].expected_return,
                markov_state_confidence=entry["markov"].state_confidence,
                mcmc_predicted_state=entry["mcmc"].predicted_state,
                mcmc_expected_return=entry["mcmc"].expected_return,
                mcmc_state_confidence=entry["mcmc"].state_confidence,
                market_cap=entry["ticker_detail"].market_cap,
                average_volume=entry["avg_vol"].average_volume,
                markov_win_rate=win_rate_row.markov_win_rate if win_rate_row else None,
                markov_predictions_count=win_rate_row.markov_predictions_count if win_rate_row else None,
                mcmc_win_rate=win_rate_row.mcmc_win_rate if win_rate_row else None,
                mcmc_predictions_count=win_rate_row.mcmc_predictions_count if win_rate_row else None,
                backtest_win_rate=entry["backtest_hit_rate"],
                backtest_evaluated_count=entry["backtest_n"],
                rsi_value=entry["rsi_value"],
                news_article_count=entry["news_count"],
                news_sentiment_lean=entry["news_lean"],
                comment=entry["comment"],
                computed_at=computed_at,
            )
        )
    session.commit()

    logger.info("selected %d research pick(s) for %s", len(picks), predicted_date)
    return len(picks)
