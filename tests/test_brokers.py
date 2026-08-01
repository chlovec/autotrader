import pytest

from engine.brokers import make_broker
from engine.brokers.alpaca_broker import AlpacaBroker
from engine.config import Config


def _config(broker: str) -> Config:
    return Config(
        broker=broker,
        alpaca_api_key="key",
        alpaca_secret_key="secret",
        alpaca_base_url="https://paper-api.alpaca.markets",
        alpaca_paper=True,
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
    broker = make_broker(_config("alpaca"))
    assert isinstance(broker, AlpacaBroker)


def test_make_broker_rejects_unsupported_broker():
    with pytest.raises(ValueError, match="unsupported broker 'ibkr'"):
        make_broker(_config("ibkr"))
