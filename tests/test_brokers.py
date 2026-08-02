import pytest

from engine.brokers import make_broker
from engine.brokers.alpaca_broker import AlpacaBroker
from engine.brokers.ibkr_broker import IBKRBroker
from engine.brokers.questrade_broker import QuestradeBroker
from engine.config import AccountCredentials


def _credentials(broker: str) -> AccountCredentials:
    return AccountCredentials(
        broker=broker,
        alpaca_api_key="key",
        alpaca_secret_key="secret",
        alpaca_base_url="https://paper-api.alpaca.markets",
        alpaca_paper=True,
        ibkr_host="127.0.0.1",
        ibkr_port=7497,
        ibkr_client_id=1,
        questrade_refresh_token="token",
        questrade_poll_interval_seconds=5.0,
    )


def test_make_broker_returns_alpaca_by_default():
    assert isinstance(make_broker("acct1", _credentials("alpaca")), AlpacaBroker)


def test_make_broker_returns_ibkr():
    assert isinstance(make_broker("acct1", _credentials("ibkr")), IBKRBroker)


def test_make_broker_returns_questrade():
    assert isinstance(make_broker("acct1", _credentials("questrade")), QuestradeBroker)


def test_make_broker_rejects_unsupported_broker():
    with pytest.raises(ValueError, match="unsupported broker 'schwab'"):
        make_broker("acct1", _credentials("schwab"))


def test_make_broker_scopes_questrade_token_cache_by_account_id():
    broker1 = make_broker("acct1", _credentials("questrade"))
    broker2 = make_broker("acct2", _credentials("questrade"))

    assert broker1._token_cache_path != broker2._token_cache_path
    assert "acct1" in str(broker1._token_cache_path)
    assert "acct2" in str(broker2._token_cache_path)
