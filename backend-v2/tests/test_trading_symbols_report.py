import datetime as dt

import pytest

from db.models import CurrentSnapshot, MarketPrediction, Ticker
from db.session import SessionLocal, init_db


@pytest.fixture(autouse=True)
def _clean_db():
    init_db()
    session = SessionLocal()
    session.query(MarketPrediction).delete()
    session.query(CurrentSnapshot).delete()
    session.query(Ticker).delete()
    session.commit()
    session.close()
    yield


_LATEST = dt.date(2026, 9, 30)


def _prediction(ticker: str, predicted_date: dt.date, state: str = "up") -> MarketPrediction:
    return MarketPrediction(
        ticker=ticker,
        predicted_date=predicted_date,
        current_state="flat",
        predicted_state=state,
        state_confidence=0.5,
        expected_return=0.01,
        entry_price=10.0,
        exit_price=10.1,
        exit_price_confidence=0.9,
        entry_time="09:30",
        exit_time="16:00",
        history_days=100,
        computed_at=dt.datetime(2026, 9, 30),
    )


def _seed():
    with SessionLocal() as session:
        for ticker in ("AAA", "BBB", "CCC", "DDD"):
            session.add(Ticker(ticker=ticker, type="CS"))
        session.add_all(
            [
                _prediction("AAA", _LATEST),
                _prediction("AAA", _LATEST - dt.timedelta(days=1)),
                _prediction("BBB", _LATEST, state="down"),
                # CCC was only predicted on an older date; DDD never.
                _prediction("CCC", _LATEST - dt.timedelta(days=1)),
            ]
        )
        session.commit()


def test_only_tickers_in_latest_predictions():
    from app.main import trading_symbols_report

    _seed()
    result = trading_symbols_report()
    assert [row["ticker"] for row in result["rows"]] == ["AAA", "BBB"]
    assert result["total"] == 2
    assert all(row["predicted_date"] == _LATEST.isoformat() for row in result["rows"])


def test_prediction_filters_still_apply_and_count_matches():
    from app.main import trading_symbols_report

    _seed()
    result = trading_symbols_report(predicted_states="down")
    assert [row["ticker"] for row in result["rows"]] == ["BBB"]
    assert result["total"] == 1


def test_no_predictions_means_no_rows():
    from app.main import trading_symbols_report

    with SessionLocal() as session:
        session.add(Ticker(ticker="AAA", type="CS"))
        session.commit()
    assert trading_symbols_report() == {"rows": [], "total": 0, "page": 1, "page_size": 500}
