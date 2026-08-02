import datetime as dt

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.models import Account, Base, EquitySnapshot, OrderSide, SignalAction, Trade
from engine.risk import RiskManager


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _account(
    account_id: str = "acct-1",
    max_position_size_usd: float = 1000.0,
    max_daily_loss_usd: float = 200.0,
    kill_switch_engaged: bool = False,
    kill_switch_reason: str = "",
) -> Account:
    return Account(
        id=account_id,
        broker="alpaca",
        display_name="Test",
        active=True,
        strategy_name="ma_crossover",
        max_position_size_usd=max_position_size_usd,
        max_daily_loss_usd=max_daily_loss_usd,
        kill_switch_engaged=kill_switch_engaged,
        kill_switch_reason=kill_switch_reason,
    )


def test_daily_loss_limit_not_breached_with_no_snapshots():
    risk = RiskManager(_session(), _account())
    assert risk.daily_loss_limit_breached() is False


def test_daily_loss_limit_breached_when_equity_drops_past_limit():
    session = _session()
    today_start = dt.datetime.combine(dt.date.today(), dt.time(9, 30))
    session.add(EquitySnapshot(timestamp=today_start, equity=10000, cash=10000, buying_power=10000, broker="alpaca", broker_account_id="b1", account_id="acct-1"))
    session.add(
        EquitySnapshot(
            timestamp=today_start + dt.timedelta(hours=2), equity=9700, cash=9700, buying_power=9700,
            broker="alpaca", broker_account_id="b1", account_id="acct-1",
        )
    )
    session.commit()

    risk = RiskManager(session, _account(max_daily_loss_usd=200.0))
    assert risk.daily_loss_limit_breached() is True


def test_daily_loss_limit_not_breached_within_tolerance():
    session = _session()
    today_start = dt.datetime.combine(dt.date.today(), dt.time(9, 30))
    session.add(EquitySnapshot(timestamp=today_start, equity=10000, cash=10000, buying_power=10000, broker="alpaca", broker_account_id="b1", account_id="acct-1"))
    session.add(
        EquitySnapshot(
            timestamp=today_start + dt.timedelta(hours=2), equity=9950, cash=9950, buying_power=9950,
            broker="alpaca", broker_account_id="b1", account_id="acct-1",
        )
    )
    session.commit()

    risk = RiskManager(session, _account(max_daily_loss_usd=200.0))
    assert risk.daily_loss_limit_breached() is False


def test_approve_rejects_when_kill_switch_engaged():
    risk = RiskManager(_session(), _account(kill_switch_engaged=True, kill_switch_reason="manual stop"))
    approved, reason = risk.approve("SPY", SignalAction.buy, 100.0)
    assert approved is False
    assert "kill switch" in reason


def test_approve_rejects_hold_signal():
    risk = RiskManager(_session(), _account())
    approved, _ = risk.approve("SPY", SignalAction.hold, 0.0)
    assert approved is False


def test_approve_rejects_buy_exceeding_position_cap():
    session = _session()
    session.add(
        Trade(
            account_id="acct-1", broker="alpaca", broker_account_id="b1", broker_order_id="1",
            symbol="SPY", side=OrderSide.buy, qty=2, fill_price=450.0, status="filled",
        )
    )
    session.commit()

    risk = RiskManager(session, _account(max_position_size_usd=1000.0))
    approved, reason = risk.approve("SPY", SignalAction.buy, 200.0)
    assert approved is False
    assert "max position size" in reason


def test_approve_allows_buy_within_position_cap():
    risk = RiskManager(_session(), _account(max_position_size_usd=1000.0))
    approved, _ = risk.approve("SPY", SignalAction.buy, 500.0)
    assert approved is True


def test_approve_allows_sell_regardless_of_exposure():
    risk = RiskManager(_session(), _account(max_position_size_usd=100.0))
    approved, _ = risk.approve("SPY", SignalAction.sell, 100000.0)
    assert approved is True


def test_current_exposure_ignores_trades_from_a_different_account():
    """The whole point of scoping RiskManager by account: a same-symbol Trade left over
    from a different account must not count toward this account's exposure - otherwise
    two accounts trading the same symbol could wrongly affect each other's risk checks."""
    session = _session()
    session.add(
        Trade(
            account_id="other-acct", broker="ibkr", broker_account_id="b2", broker_order_id="1",
            symbol="SPY", side=OrderSide.buy, qty=2, fill_price=450.0, status="filled",
        )
    )
    session.commit()

    risk = RiskManager(session, _account("acct-1", max_position_size_usd=1000.0))
    approved, _ = risk.approve("SPY", SignalAction.buy, 200.0)
    assert approved is True
