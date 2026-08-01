import datetime as dt
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from db.models import SignalAction
from engine.brokers import questrade_broker
from engine.brokers.questrade_broker import QuestradeBroker
from engine.config import Config


def _config(refresh_token: str = "seed-token") -> Config:
    return Config(
        broker="questrade",
        alpaca_api_key="",
        alpaca_secret_key="",
        alpaca_base_url="",
        alpaca_paper=True,
        ibkr_host="127.0.0.1",
        ibkr_port=7497,
        ibkr_client_id=1,
        questrade_refresh_token=refresh_token,
        max_position_size_usd=1000.0,
        max_daily_loss_usd=200.0,
        smtp_host="",
        smtp_port=587,
        smtp_username="",
        smtp_password="",
        alert_email_from="",
        alert_email_to="",
    )


def _fake_response(json_data: dict, status_code: int = 200) -> SimpleNamespace:
    def raise_for_status():
        if status_code >= 400:
            raise Exception(f"HTTP {status_code}")

    return SimpleNamespace(status_code=status_code, json=lambda: json_data, raise_for_status=raise_for_status)


@pytest.fixture(autouse=True)
def _isolated_token_cache(tmp_path, monkeypatch):
    cache_path = tmp_path / "questrade_token.json"
    monkeypatch.setattr(questrade_broker, "_TOKEN_CACHE_PATH", cache_path)
    return cache_path


AUTH_RESPONSE = {
    "access_token": "access-1",
    "api_server": "https://api01.iq.questrade.com/",
    "refresh_token": "rotated-token-1",
}


def test_authenticate_stores_new_refresh_token(_isolated_token_cache):
    with patch("engine.brokers.questrade_broker.requests.get", return_value=_fake_response(AUTH_RESPONSE)) as mock_get:
        broker = QuestradeBroker(_config())
        broker._authenticate()

    mock_get.assert_called_once()
    assert mock_get.call_args.kwargs["params"]["refresh_token"] == "seed-token"
    assert broker._access_token == "access-1"
    assert broker._api_server == "https://api01.iq.questrade.com"  # trailing slash stripped
    assert json.loads(_isolated_token_cache.read_text()) == {"refresh_token": "rotated-token-1"}


def test_second_instance_uses_cached_rotated_token(_isolated_token_cache):
    _isolated_token_cache.write_text(json.dumps({"refresh_token": "rotated-token-1"}))

    with patch("engine.brokers.questrade_broker.requests.get", return_value=_fake_response(AUTH_RESPONSE)) as mock_get:
        broker = QuestradeBroker(_config(refresh_token="seed-token"))
        broker._authenticate()

    assert mock_get.call_args.kwargs["params"]["refresh_token"] == "rotated-token-1"


def test_get_account_parses_usd_combined_balance():
    balances = {
        "combinedBalances": [
            {"currency": "CAD", "totalEquity": 500.0, "cash": 100.0, "buyingPower": 200.0},
            {"currency": "USD", "totalEquity": 10000.0, "cash": 4000.0, "buyingPower": 16000.0},
        ]
    }
    broker = QuestradeBroker(_config())
    broker._access_token = "token"
    broker._api_server = "https://api.example.com"
    broker._account_id = "123"

    with patch("engine.brokers.questrade_broker.requests.request", return_value=_fake_response(balances)):
        account = broker.get_account()

    assert account.equity == 10000.0
    assert account.cash == 4000.0
    assert account.buying_power == 16000.0


def test_get_positions_translates_openpnl_field():
    positions = {
        "positions": [
            {
                "symbol": "SPY",
                "openQuantity": 10.0,
                "averageEntryPrice": 450.0,
                "currentMarketValue": 4600.0,
                "openPnl": 100.0,
            }
        ]
    }
    broker = QuestradeBroker(_config())
    broker._access_token = "token"
    broker._api_server = "https://api.example.com"
    broker._account_id = "123"

    with patch("engine.brokers.questrade_broker.requests.request", return_value=_fake_response(positions)):
        result = broker.get_positions()

    assert len(result) == 1
    assert result[0].symbol == "SPY"
    assert result[0].unrealized_pl == 100.0


def test_get_clock_open_during_market_hours():
    markets = {
        "markets": [
            {
                "name": "NASDAQ",
                "startTime": "2026-08-03T13:30:00.000000-00:00",
                "endTime": "2026-08-03T20:00:00.000000-00:00",
            }
        ]
    }
    broker = QuestradeBroker(_config())
    broker._access_token = "token"
    broker._api_server = "https://api.example.com"

    fake_now = dt.datetime(2026, 8, 3, 15, 0, tzinfo=dt.timezone.utc)
    with (
        patch("engine.brokers.questrade_broker.requests.request", return_value=_fake_response(markets)),
        patch("engine.brokers.questrade_broker.dt") as mock_dt,
    ):
        mock_dt.datetime.now.return_value = fake_now
        mock_dt.datetime.fromisoformat = dt.datetime.fromisoformat
        mock_dt.timezone = dt.timezone
        clock = broker.get_clock()

    assert clock.is_open is True


def test_submit_market_order_builds_correct_request_body():
    broker = QuestradeBroker(_config())
    broker._access_token = "token"
    broker._api_server = "https://api.example.com"
    broker._account_id = "123"
    broker._symbol_id_cache["SPY"] = 8049

    order_response = {"orders": [{"id": 555, "state": "Accepted"}]}
    with patch("engine.brokers.questrade_broker.requests.request", return_value=_fake_response(order_response)) as mock_request:
        result = broker.submit_market_order("SPY", SignalAction.buy, 10.0)

    assert result.broker_order_id == "555"
    assert result.status == "Accepted"
    sent_body = mock_request.call_args.kwargs["json"]
    assert sent_body["symbolId"] == 8049
    assert sent_body["action"] == "Buy"
    assert sent_body["orderType"] == "Market"


def test_request_reauthenticates_once_on_401():
    broker = QuestradeBroker(_config())
    broker._access_token = "stale-token"
    broker._api_server = "https://api.example.com"

    responses = [_fake_response({}, status_code=401), _fake_response({"ok": True})]

    def fake_request(method, url, **kwargs):
        return responses.pop(0)

    with (
        patch("engine.brokers.questrade_broker.requests.request", side_effect=fake_request),
        patch("engine.brokers.questrade_broker.requests.get", return_value=_fake_response(AUTH_RESPONSE)),
    ):
        result = broker._request("GET", "/v1/accounts")

    assert result == {"ok": True}
    assert broker._access_token == "access-1"
