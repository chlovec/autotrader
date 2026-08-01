import pytest

from engine.brokers import make_broker
from engine.brokers.alpaca_broker import AlpacaBroker
from engine.brokers.ibkr_broker import IBKRBroker
from engine.brokers.questrade_broker import QuestradeBroker
from engine.config import Config


def _config(broker: str) -> Config:
    return Config(
        broker=broker,
        alpaca_api_key="key",
        alpaca_secret_key="secret",
        alpaca_base_url="https://paper-api.alpaca.markets",
        alpaca_paper=True,
        ibkr_host="127.0.0.1",
        ibkr_port=7497,
        ibkr_client_id=1,
        questrade_refresh_token="token",
        max_position_size_usd=1000.0,
        max_daily_loss_usd=200.0,
        smtp_host="",
        smtp_port=587,
        smtp_username="",
        smtp_password="",
        alert_email_from="",
        alert_email_to="",
    )


def test_make_broker_returns_alpaca_by_default():
    assert isinstance(make_broker(_config("alpaca")), AlpacaBroker)


def test_make_broker_returns_ibkr():
    assert isinstance(make_broker(_config("ibkr")), IBKRBroker)


def test_make_broker_returns_questrade():
    assert isinstance(make_broker(_config("questrade")), QuestradeBroker)


def test_make_broker_rejects_unsupported_broker():
    with pytest.raises(ValueError, match="unsupported broker 'schwab'"):
        make_broker(_config("schwab"))
