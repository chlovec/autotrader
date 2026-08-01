import datetime as dt

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.models import Base, EquitySnapshot, KillSwitch, OrderSide, SignalAction, Trade
from engine.config import Config
from engine.risk import RiskManager


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _config(max_position_size_usd: float = 1000.0, max_daily_loss_usd: float = 200.0) -> Config:
    return Config(
        broker="alpaca",
        alpaca_api_key="",
        alpaca_secret_key="",
        alpaca_base_url="",
        alpaca_paper=True,
        ibkr_host="127.0.0.1",
        ibkr_port=7497,
        ibkr_client_id=1,
        questrade_refresh_token="",
        max_position_size_usd=max_position_size_usd,
        max_daily_loss_usd=max_daily_loss_usd,
        smtp_host="",
        smtp_port=587,
        smtp_username="",
        smtp_password="",
        alert_email_from="",
        alert_email_to="",
    )


def test_daily_loss_limit_not_breached_with_no_snapshots():
    risk = RiskManager(_config(), _session())
    assert risk.daily_loss_limit_breached() is False


def test_daily_loss_limit_breached_when_equity_drops_past_limit():
    session = _session()
    today_start = dt.datetime.combine(dt.date.today(), dt.time(9, 30))
    session.add(EquitySnapshot(timestamp=today_start, equity=10000, cash=10000, buying_power=10000))
    session.add(EquitySnapshot(timestamp=today_start + dt.timedelta(hours=2), equity=9700, cash=9700, buying_power=9700))
    session.commit()

    risk = RiskManager(_config(max_daily_loss_usd=200.0), session)
    assert risk.daily_loss_limit_breached() is True


def test_daily_loss_limit_not_breached_within_tolerance():
    session = _session()
    today_start = dt.datetime.combine(dt.date.today(), dt.time(9, 30))
    session.add(EquitySnapshot(timestamp=today_start, equity=10000, cash=10000, buying_power=10000))
    session.add(EquitySnapshot(timestamp=today_start + dt.timedelta(hours=2), equity=9950, cash=9950, buying_power=9950))
    session.commit()

    risk = RiskManager(_config(max_daily_loss_usd=200.0), session)
    assert risk.daily_loss_limit_breached() is False


def test_approve_rejects_when_kill_switch_engaged():
    session = _session()
    session.add(KillSwitch(id=1, engaged=True, reason="manual stop"))
    session.commit()

    risk = RiskManager(_config(), session)
    approved, reason = risk.approve("SPY", SignalAction.buy, 100.0)
    assert approved is False
    assert "kill switch" in reason


def test_approve_rejects_hold_signal():
    risk = RiskManager(_config(), _session())
    approved, _ = risk.approve("SPY", SignalAction.hold, 0.0)
    assert approved is False


def test_approve_rejects_buy_exceeding_position_cap():
    session = _session()
    session.add(Trade(broker_order_id="1", symbol="SPY", side=OrderSide.buy, qty=2, fill_price=450.0, status="filled"))
    session.commit()

    risk = RiskManager(_config(max_position_size_usd=1000.0), session)
    approved, reason = risk.approve("SPY", SignalAction.buy, 200.0)
    assert approved is False
    assert "max position size" in reason


def test_approve_allows_buy_within_position_cap():
    risk = RiskManager(_config(max_position_size_usd=1000.0), _session())
    approved, _ = risk.approve("SPY", SignalAction.buy, 500.0)
    assert approved is True


def test_approve_allows_sell_regardless_of_exposure():
    risk = RiskManager(_config(max_position_size_usd=100.0), _session())
    approved, _ = risk.approve("SPY", SignalAction.sell, 100000.0)
    assert approved is True
