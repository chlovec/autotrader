import datetime as dt

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.models import Base, Signal, SignalAction
from engine.portfolio_runner import REBALANCE_STRATEGY_NAME, _already_rebalanced_this_month


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_no_prior_signal_returns_false():
    session = _session()
    assert _already_rebalanced_this_month(session) is False


def test_signal_earlier_this_month_returns_true():
    session = _session()
    session.add(
        Signal(symbol="SPY", strategy_name=REBALANCE_STRATEGY_NAME, action=SignalAction.buy, timestamp=dt.datetime(2026, 8, 1))
    )
    session.commit()
    assert _already_rebalanced_this_month(session, now=dt.datetime(2026, 8, 15)) is True


def test_signal_last_month_returns_false():
    session = _session()
    session.add(
        Signal(symbol="SPY", strategy_name=REBALANCE_STRATEGY_NAME, action=SignalAction.buy, timestamp=dt.datetime(2026, 7, 15))
    )
    session.commit()
    assert _already_rebalanced_this_month(session, now=dt.datetime(2026, 8, 1)) is False
