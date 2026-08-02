import datetime as dt
from types import SimpleNamespace

from db.models import SignalAction
from engine.brokers.base import Timeframe
from engine.brokers.ibkr_broker import IBKRBroker, _duration_string, _parse_trading_hours
from engine.config import Config


def _config() -> Config:
    return Config(
        broker="ibkr",
        alpaca_api_key="",
        alpaca_secret_key="",
        alpaca_base_url="",
        alpaca_paper=True,
        ibkr_host="127.0.0.1",
        ibkr_port=7497,
        ibkr_client_id=1,
        questrade_refresh_token="",
        questrade_poll_interval_seconds=5.0,
        max_position_size_usd=1000.0,
        max_daily_loss_usd=200.0,
        smtp_host="",
        smtp_port=587,
        smtp_username="",
        smtp_password="",
        alert_email_from="",
        alert_email_to="",
    )


# --- _parse_trading_hours ---


def test_trading_hours_open_during_session():
    hours = "20260801:0930-1600"
    now = dt.datetime(2026, 8, 1, 12, 0)
    assert _parse_trading_hours(hours, now) is True


def test_trading_hours_closed_before_open():
    hours = "20260801:0930-1600"
    now = dt.datetime(2026, 8, 1, 8, 0)
    assert _parse_trading_hours(hours, now) is False


def test_trading_hours_closed_after_close():
    hours = "20260801:0930-1600"
    now = dt.datetime(2026, 8, 1, 17, 0)
    assert _parse_trading_hours(hours, now) is False


def test_trading_hours_explicit_closed_segment():
    hours = "20260801:CLOSED"
    now = dt.datetime(2026, 8, 1, 12, 0)
    assert _parse_trading_hours(hours, now) is False


def test_trading_hours_picks_correct_day_from_multiple_segments():
    hours = "20260731:CLOSED;20260801:0930-1600;20260802:CLOSED"
    now = dt.datetime(2026, 8, 1, 10, 0)
    assert _parse_trading_hours(hours, now) is True


def test_trading_hours_no_matching_segment_defaults_closed():
    hours = "20260731:0930-1600"
    now = dt.datetime(2026, 8, 1, 10, 0)
    assert _parse_trading_hours(hours, now) is False


# --- _duration_string ---


def test_duration_string_with_start_date():
    now = dt.datetime(2026, 8, 1)
    start = now - dt.timedelta(days=30)
    assert _duration_string(Timeframe.DAY, None, start, now) == "30 D"


def test_duration_string_with_start_date_floors_to_one_day():
    now = dt.datetime(2026, 8, 1, 12, 0)
    start = now - dt.timedelta(hours=2)
    assert _duration_string(Timeframe.DAY, None, start, now) == "1 D"


def test_duration_string_daily_bars_with_limit():
    now = dt.datetime(2026, 8, 1)
    assert _duration_string(Timeframe.DAY, 50, None, now) == "50 D"


def test_duration_string_daily_bars_default_limit():
    now = dt.datetime(2026, 8, 1)
    assert _duration_string(Timeframe.DAY, None, None, now) == "100 D"


def test_duration_string_minute_bars_converts_to_seconds():
    now = dt.datetime(2026, 8, 1)
    assert _duration_string(Timeframe.MINUTE, 30, None, now) == "1800 S"


# --- translation logic (mocked IB connection, no live TWS/Gateway needed) ---


def _broker_with_fake_ib(fake_ib: SimpleNamespace) -> IBKRBroker:
    broker = IBKRBroker(_config())
    fake_ib.isConnected = lambda: True
    broker._ib = fake_ib
    return broker


def test_get_account_reads_net_liquidation_cash_and_buying_power():
    fake_ib = SimpleNamespace(
        accountSummary=lambda: [
            SimpleNamespace(tag="NetLiquidation", value="10000.5", currency="USD"),
            SimpleNamespace(tag="TotalCashValue", value="4000.0", currency="USD"),
            SimpleNamespace(tag="BuyingPower", value="16000.0", currency="USD"),
            SimpleNamespace(tag="NetLiquidation", value="9000.0", currency="EUR"),  # ignored, wrong currency
        ],
        managedAccounts=lambda: ["DU1234567"],
    )
    account = _broker_with_fake_ib(fake_ib).get_account()
    assert account.equity == 10000.5
    assert account.cash == 4000.0
    assert account.buying_power == 16000.0
    assert account.account_id == "DU1234567"


def test_get_positions_translates_portfolio_items():
    fake_ib = SimpleNamespace(
        portfolio=lambda: [
            SimpleNamespace(
                contract=SimpleNamespace(symbol="SPY"),
                position=10.0,
                averageCost=450.0,
                marketValue=4600.0,
                unrealizedPNL=100.0,
            )
        ]
    )
    positions = _broker_with_fake_ib(fake_ib).get_positions()
    assert len(positions) == 1
    assert positions[0].symbol == "SPY"
    assert positions[0].qty == 10.0
    assert positions[0].unrealized_pl == 100.0


def test_get_position_qty_returns_zero_when_not_held():
    fake_ib = SimpleNamespace(positions=lambda: [])
    assert _broker_with_fake_ib(fake_ib).get_position_qty("SPY") == 0.0


def test_get_position_qty_finds_matching_symbol():
    fake_ib = SimpleNamespace(
        positions=lambda: [SimpleNamespace(contract=SimpleNamespace(symbol="SPY"), position=-5.0)]
    )
    assert _broker_with_fake_ib(fake_ib).get_position_qty("SPY") == 5.0


def test_submit_market_order_translates_trade_result():
    fake_trade = SimpleNamespace(
        order=SimpleNamespace(orderId=42),
        orderStatus=SimpleNamespace(status="Submitted"),
    )
    fake_ib = SimpleNamespace(placeOrder=lambda contract, order: fake_trade, sleep=lambda secs: None)
    result = _broker_with_fake_ib(fake_ib).submit_market_order("SPY", SignalAction.buy, 10.0)
    assert result.broker_order_id == "42"
    assert result.status == "Submitted"
