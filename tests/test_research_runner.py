import datetime as dt

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.models import Base, ResearchResult
from db.queries import get_watchlist_symbols


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _result(symbol: str, run_at: dt.datetime, combined_score: float, selected: bool) -> ResearchResult:
    return ResearchResult(
        run_at=run_at, symbol=symbol, technical_score=combined_score, news_score=combined_score,
        combined_score=combined_score, selected=selected,
    )


def test_no_research_runs_returns_empty_list():
    session = _session()
    assert get_watchlist_symbols(session) == []


def test_only_latest_run_is_returned():
    session = _session()
    older = dt.datetime(2026, 7, 1)
    latest = dt.datetime(2026, 8, 1)
    session.add_all([
        _result("AAPL", older, 90, selected=True),
        _result("MSFT", latest, 80, selected=True),
        _result("GOOGL", latest, 70, selected=True),
    ])
    session.commit()

    assert get_watchlist_symbols(session) == ["MSFT", "GOOGL"]


def test_unselected_rows_are_excluded():
    session = _session()
    run_at = dt.datetime(2026, 8, 1)
    session.add_all([
        _result("AAPL", run_at, 90, selected=True),
        _result("XYZ", run_at, 10, selected=False),
    ])
    session.commit()

    assert get_watchlist_symbols(session) == ["AAPL"]


def test_ordered_by_combined_score_desc():
    session = _session()
    run_at = dt.datetime(2026, 8, 1)
    session.add_all([
        _result("LOW", run_at, 60, selected=True),
        _result("HIGH", run_at, 95, selected=True),
        _result("MID", run_at, 75, selected=True),
    ])
    session.commit()

    assert get_watchlist_symbols(session) == ["HIGH", "MID", "LOW"]


def test_limit_is_honored():
    session = _session()
    run_at = dt.datetime(2026, 8, 1)
    session.add_all([
        _result("A", run_at, 90, selected=True),
        _result("B", run_at, 80, selected=True),
        _result("C", run_at, 70, selected=True),
    ])
    session.commit()

    assert get_watchlist_symbols(session, limit=2) == ["A", "B"]
