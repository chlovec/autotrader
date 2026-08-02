import pytest

from engine.config import load_account_credentials, load_account_ids


def test_load_account_ids_parses_comma_separated(monkeypatch):
    monkeypatch.setenv("ACCOUNT_IDS", "alpaca_paper, ibkr_main ,")
    assert load_account_ids() == ["alpaca_paper", "ibkr_main"]


def test_load_account_ids_empty_when_unset(monkeypatch):
    monkeypatch.delenv("ACCOUNT_IDS", raising=False)
    assert load_account_ids() == []


def test_paper_mode_uses_paper_keys(monkeypatch):
    monkeypatch.setenv("ACCOUNT_acct1_BROKER", "alpaca")
    monkeypatch.setenv("ACCOUNT_acct1_ALPACA_PAPER", "true")
    monkeypatch.setenv("ACCOUNT_acct1_ALPACA_API_KEY", "paper-key")
    monkeypatch.setenv("ACCOUNT_acct1_ALPACA_SECRET_KEY", "paper-secret")

    credentials = load_account_credentials("acct1")

    assert credentials.alpaca_paper is True
    assert credentials.alpaca_api_key == "paper-key"
    assert credentials.alpaca_secret_key == "paper-secret"


def test_live_mode_uses_live_keys(monkeypatch):
    monkeypatch.setenv("ACCOUNT_acct1_BROKER", "alpaca")
    monkeypatch.setenv("ACCOUNT_acct1_ALPACA_PAPER", "false")
    monkeypatch.setenv("ACCOUNT_acct1_ALPACA_LIVE_API_KEY", "live-key")
    monkeypatch.setenv("ACCOUNT_acct1_ALPACA_LIVE_SECRET_KEY", "live-secret")

    credentials = load_account_credentials("acct1")

    assert credentials.alpaca_paper is False
    assert credentials.alpaca_api_key == "live-key"
    assert credentials.alpaca_secret_key == "live-secret"


def test_paper_mode_defaults_to_paper_base_url(monkeypatch):
    monkeypatch.setenv("ACCOUNT_acct1_BROKER", "alpaca")
    monkeypatch.delenv("ACCOUNT_acct1_ALPACA_BASE_URL", raising=False)

    credentials = load_account_credentials("acct1")

    assert credentials.alpaca_base_url == "https://paper-api.alpaca.markets"


def test_live_mode_defaults_to_live_base_url(monkeypatch):
    monkeypatch.setenv("ACCOUNT_acct1_BROKER", "alpaca")
    monkeypatch.setenv("ACCOUNT_acct1_ALPACA_PAPER", "false")
    monkeypatch.setenv("ACCOUNT_acct1_ALPACA_LIVE_API_KEY", "live-key")
    monkeypatch.setenv("ACCOUNT_acct1_ALPACA_LIVE_SECRET_KEY", "live-secret")
    monkeypatch.delenv("ACCOUNT_acct1_ALPACA_LIVE_BASE_URL", raising=False)

    credentials = load_account_credentials("acct1")

    assert credentials.alpaca_base_url == "https://api.alpaca.markets"


def test_live_mode_uses_live_base_url_env_var(monkeypatch):
    monkeypatch.setenv("ACCOUNT_acct1_BROKER", "alpaca")
    monkeypatch.setenv("ACCOUNT_acct1_ALPACA_PAPER", "false")
    monkeypatch.setenv("ACCOUNT_acct1_ALPACA_LIVE_API_KEY", "live-key")
    monkeypatch.setenv("ACCOUNT_acct1_ALPACA_LIVE_SECRET_KEY", "live-secret")
    monkeypatch.setenv("ACCOUNT_acct1_ALPACA_LIVE_BASE_URL", "https://env-live.alpaca.markets")

    credentials = load_account_credentials("acct1")

    assert credentials.alpaca_base_url == "https://env-live.alpaca.markets"


def test_defaults_to_paper_mode_when_unset(monkeypatch):
    monkeypatch.setenv("ACCOUNT_acct1_BROKER", "alpaca")
    monkeypatch.delenv("ACCOUNT_acct1_ALPACA_PAPER", raising=False)
    monkeypatch.setenv("ACCOUNT_acct1_ALPACA_API_KEY", "paper-key")

    credentials = load_account_credentials("acct1")

    assert credentials.alpaca_paper is True
    assert credentials.alpaca_api_key == "paper-key"


def test_live_mode_without_live_keys_raises_instead_of_falling_back(monkeypatch):
    monkeypatch.setenv("ACCOUNT_acct1_BROKER", "alpaca")
    monkeypatch.setenv("ACCOUNT_acct1_ALPACA_PAPER", "false")
    monkeypatch.delenv("ACCOUNT_acct1_ALPACA_LIVE_API_KEY", raising=False)
    monkeypatch.delenv("ACCOUNT_acct1_ALPACA_LIVE_SECRET_KEY", raising=False)

    with pytest.raises(RuntimeError, match="ACCOUNT_acct1_ALPACA_LIVE_API_KEY"):
        load_account_credentials("acct1")


def test_ibkr_fields_are_isolated_per_account(monkeypatch):
    """Two IBKR accounts on the same host need distinct client ids - confirms each
    account's fields come only from its own ACCOUNT_<id>_ prefix."""
    monkeypatch.setenv("ACCOUNT_acct1_BROKER", "ibkr")
    monkeypatch.setenv("ACCOUNT_acct1_IBKR_CLIENT_ID", "1")
    monkeypatch.setenv("ACCOUNT_acct2_BROKER", "ibkr")
    monkeypatch.setenv("ACCOUNT_acct2_IBKR_CLIENT_ID", "2")

    creds1 = load_account_credentials("acct1")
    creds2 = load_account_credentials("acct2")

    assert creds1.ibkr_client_id == 1
    assert creds2.ibkr_client_id == 2
