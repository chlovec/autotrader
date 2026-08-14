import datetime as dt
import json

import pytest

from db.models import (
    AverageVolume,
    JobRun,
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
from db.session import SessionLocal, init_db
from jobs.research_picks import DEFAULT_MIN_AVERAGE_VOLUME, DEFAULT_MIN_MARKET_CAP, compute_research_picks


@pytest.fixture(autouse=True)
def _clean_db():
    init_db()
    session = SessionLocal()
    session.query(ResearchPick).delete()
    session.query(JobRun).delete()
    session.query(News).delete()
    session.query(TechnicalIndicator).delete()
    session.query(MarketPredictionBacktest).delete()
    session.query(WinRate).delete()
    session.query(AverageVolume).delete()
    session.query(TickerDetail).delete()
    session.query(MarketPredictionMonteCarlo).delete()
    session.query(MarketPrediction).delete()
    session.query(Ticker).delete()
    session.commit()
    session.close()
    yield


_PREDICTED_DATE = dt.date(2026, 1, 2)


def _ticker(ticker: str, active: bool = True, type_: str = "CS") -> Ticker:
    return Ticker(ticker=ticker, name=f"{ticker} Inc", type=type_, active=active)


def _ticker_detail(ticker: str, market_cap: float) -> TickerDetail:
    return TickerDetail(ticker=ticker, market_cap=market_cap, fetched_at=dt.datetime.utcnow())


def _avg_volume(ticker: str, average_volume: float) -> AverageVolume:
    return AverageVolume(
        ticker=ticker,
        start_date=_PREDICTED_DATE,
        days_interval=50,
        average_volume=average_volume,
        bar_count=50,
        computed_at=dt.datetime.utcnow(),
    )


def _prediction(ticker: str, expected_return: float, predicted_state: str | None = None) -> MarketPrediction:
    state = predicted_state or ("up" if expected_return >= 0 else "down")
    return MarketPrediction(
        ticker=ticker,
        predicted_date=_PREDICTED_DATE,
        current_state="flat",
        predicted_state=state,
        state_confidence=0.6,
        expected_return=expected_return,
        entry_price=100.0,
        exit_price=100.0 * (1 + expected_return),
        exit_price_confidence=0.6,
        entry_time="09:30:00",
        exit_time="16:00:00",
        history_days=60,
        computed_at=dt.datetime.utcnow(),
    )


def _mcmc_prediction(
    ticker: str, expected_return: float, predicted_state: str | None = None
) -> MarketPredictionMonteCarlo:
    state = predicted_state or ("up" if expected_return >= 0 else "down")
    return MarketPredictionMonteCarlo(
        ticker=ticker,
        predicted_date=_PREDICTED_DATE,
        current_state="flat",
        predicted_state=state,
        state_confidence=0.55,
        expected_return=expected_return,
        entry_price=100.0,
        exit_price=100.0 * (1 + expected_return),
        exit_price_mean=100.0 * (1 + expected_return),
        exit_price_std=1.0,
        exit_price_confidence=0.55,
        exit_price_p10=99.0,
        exit_price_p50=100.0,
        exit_price_p90=101.0,
        entry_time="09:30:00",
        exit_time="16:00:00",
        num_simulations=2000,
        history_days=60,
        computed_at=dt.datetime.utcnow(),
    )


def _job_run(job_name: str = "research-picks") -> JobRun:
    return JobRun(job_name=job_name, trigger="manual", status="in_progress", started_at=dt.datetime.utcnow())


def _qualifying_ticker(session, ticker: str, expected_return: float = 0.03) -> None:
    """Adds a ticker with everything needed to pass the liquidity/agreement screen -
    used by tests that don't care about the specific values, just that the candidate
    qualifies."""
    session.add(_ticker(ticker))
    session.add(_ticker_detail(ticker, DEFAULT_MIN_MARKET_CAP * 2))
    session.add(_avg_volume(ticker, DEFAULT_MIN_AVERAGE_VOLUME * 2))
    session.add(_prediction(ticker, expected_return))
    session.add(_mcmc_prediction(ticker, expected_return))


def test_liquidity_filter_excludes_illiquid_candidate():
    session = SessionLocal()
    try:
        _qualifying_ticker(session, "AAA")
        # BBB has agreeing predictions but its average_volume sits below the floor.
        session.add(_ticker("BBB"))
        session.add(_ticker_detail("BBB", DEFAULT_MIN_MARKET_CAP * 2))
        session.add(_avg_volume("BBB", DEFAULT_MIN_AVERAGE_VOLUME / 2))
        session.add(_prediction("BBB", 0.03))
        session.add(_mcmc_prediction("BBB", 0.03))
        session.commit()

        run = _job_run()
        session.add(run)
        session.commit()

        stored = compute_research_picks(session, run.id)
        assert stored == 1
        assert {p.ticker for p in session.query(ResearchPick).all()} == {"AAA"}
    finally:
        session.close()


def test_agreement_required_filter_excludes_disagreement():
    session = SessionLocal()
    try:
        _qualifying_ticker(session, "AAA")
        # BBB is liquid but markov predicts "up" while mcmc predicts "down".
        session.add(_ticker("BBB"))
        session.add(_ticker_detail("BBB", DEFAULT_MIN_MARKET_CAP * 2))
        session.add(_avg_volume("BBB", DEFAULT_MIN_AVERAGE_VOLUME * 2))
        session.add(_prediction("BBB", 0.03, predicted_state="up"))
        session.add(_mcmc_prediction("BBB", -0.02, predicted_state="down"))
        session.commit()

        run = _job_run()
        session.add(run)
        session.commit()

        stored = compute_research_picks(session, run.id)
        assert stored == 1
        assert {p.ticker for p in session.query(ResearchPick).all()} == {"AAA"}
    finally:
        session.close()


def test_caps_at_20_picks():
    session = SessionLocal()
    try:
        for i in range(25):
            _qualifying_ticker(session, f"T{i:02d}", expected_return=0.001 * (i + 1))
        session.commit()

        run = _job_run()
        session.add(run)
        session.commit()

        stored = compute_research_picks(session, run.id)
        assert stored == 20

        picks = session.query(ResearchPick).filter_by(run_id=run.id).order_by(ResearchPick.rank).all()
        assert [p.rank for p in picks] == list(range(1, 21))
        # Highest expected_return wins the highest score - the 5 lowest (T00..T04)
        # should be excluded from the top 20.
        stored_tickers = {p.ticker for p in picks}
        assert stored_tickers.isdisjoint({"T00", "T01", "T02", "T03", "T04"})
    finally:
        session.close()


def test_fewer_than_20_qualify_stores_all_with_no_padding():
    session = SessionLocal()
    try:
        for ticker in ("AAA", "BBB", "CCC"):
            _qualifying_ticker(session, ticker)
        session.commit()

        run = _job_run()
        session.add(run)
        session.commit()

        stored = compute_research_picks(session, run.id)
        assert stored == 3
        assert session.query(ResearchPick).filter_by(run_id=run.id).count() == 3
    finally:
        session.close()


def test_comment_omits_neutral_or_absent_clauses_and_includes_present_ones():
    session = SessionLocal()
    try:
        _qualifying_ticker(session, "NOWR", expected_return=0.03)  # no win-rate/RSI/news data
        _qualifying_ticker(session, "FULL", expected_return=0.03)
        session.add(
            WinRate(
                ticker="FULL",
                last_updated=dt.datetime.utcnow(),
                mcmc_win_count=8,
                mcmc_predictions_count=12,
                mcmc_win_rate=8 / 12,
                markov_win_count=9,
                markov_predictions_count=12,
                markov_win_rate=9 / 12,
            )
        )
        session.add(
            TechnicalIndicator(
                ticker="FULL",
                indicator="rsi",
                timestamp=dt.datetime.utcnow(),
                value=25.0,  # oversold - confirms an "up" prediction
                fetched_at=dt.datetime.utcnow(),
            )
        )
        session.add(
            News(
                id="n1",
                tickers="FULL,OTHER",
                insights=json.dumps([{"ticker": "FULL", "sentiment": "positive"}]),
                published_utc=dt.datetime.utcnow(),
                fetched_at=dt.datetime.utcnow(),
            )
        )
        session.commit()

        run = _job_run()
        session.add(run)
        session.commit()

        compute_research_picks(session, run.id)

        no_data = session.query(ResearchPick).filter_by(run_id=run.id, ticker="NOWR").one()
        assert no_data.win_rate_score == pytest.approx(0.5)
        assert "win rate" not in no_data.comment.lower()
        assert "rsi" not in no_data.comment.lower()
        assert "article" not in no_data.comment.lower()

        full = session.query(ResearchPick).filter_by(run_id=run.id, ticker="FULL").one()
        assert "win rate" in full.comment.lower()
        assert "rsi 25" in full.comment.lower()
        assert "article" in full.comment.lower()
        assert full.rsi_value == pytest.approx(25.0)
        assert full.rsi_adjustment == pytest.approx(0.05)
        assert full.news_article_count == 1
        assert full.news_sentiment_lean == 1
    finally:
        session.close()


def test_second_run_accumulates_rather_than_overwrites():
    session = SessionLocal()
    try:
        _qualifying_ticker(session, "AAA")
        session.commit()

        run1 = _job_run()
        session.add(run1)
        session.commit()
        compute_research_picks(session, run1.id)

        run2 = _job_run()
        session.add(run2)
        session.commit()
        compute_research_picks(session, run2.id)

        assert session.query(ResearchPick).count() == 2
        assert session.query(ResearchPick).filter_by(run_id=run1.id).count() == 1
        assert session.query(ResearchPick).filter_by(run_id=run2.id).count() == 1
    finally:
        session.close()


def test_news_matching_does_not_false_positive_on_substring():
    session = SessionLocal()
    try:
        _qualifying_ticker(session, "AA")
        _qualifying_ticker(session, "AAPL")
        session.add(
            News(
                id="n1",
                tickers="AA,MSFT",
                insights=json.dumps([{"ticker": "AA", "sentiment": "positive"}]),
                published_utc=dt.datetime.utcnow(),
                fetched_at=dt.datetime.utcnow(),
            )
        )
        session.commit()

        run = _job_run()
        session.add(run)
        session.commit()
        compute_research_picks(session, run.id)

        aa = session.query(ResearchPick).filter_by(run_id=run.id, ticker="AA").one()
        aapl = session.query(ResearchPick).filter_by(run_id=run.id, ticker="AAPL").one()
        assert aa.news_article_count == 1
        assert aapl.news_article_count is None
    finally:
        session.close()


def test_zero_predictions_returns_zero_without_raising():
    session = SessionLocal()
    try:
        run = _job_run()
        session.add(run)
        session.commit()

        stored = compute_research_picks(session, run.id)
        assert stored == 0
        assert session.query(ResearchPick).count() == 0
    finally:
        session.close()
