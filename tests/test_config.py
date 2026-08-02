import pytest

from engine.config import load_account_credentials, load_account_ids


def test_load_account_ids_parses_comma_separated(monkeypatch):
    monkeypatch.setenv("ACCOUNT_IDS", "alpaca_paper, ibkr_main ,")
    assert load_account_ids() == ["alpaca_paper", "ibkr_main"]


def test_load_account_ids_empty_when_unset(monkeypatch):
    monkeypatch.delenv("ACCOUNT_IDS", raising=False)
    assert load_account_ids() == []


def test_alpaca_reads_a_single_key_pair(monkeypatch):
    """No separate paper/live flag or second key pair - whichever credentials the
    account is given are the environment it trades in."""
    monkeypatch.setenv("ACCOUNT_acct1_BROKER", "alpaca")
    monkeypatch.setenv("ACCOUNT_acct1_ALPACA_API_KEY", "some-key")
    monkeypatch.setenv("ACCOUNT_acct1_ALPACA_SECRET_KEY", "some-secret")
    monkeypatch.setenv("ACCOUNT_acct1_ALPACA_BASE_URL", "https://api.alpaca.markets")

    credentials = load_account_credentials("acct1")

    assert credentials.alpaca_api_key == "some-key"
    assert credentials.alpaca_secret_key == "some-secret"
    assert credentials.alpaca_base_url == "https://api.alpaca.markets"
    assert not hasattr(credentials, "alpaca_paper")


def test_alpaca_defaults_to_paper_base_url_when_unset(monkeypatch):
    monkeypatch.setenv("ACCOUNT_acct1_BROKER", "alpaca")
    monkeypatch.setenv("ACCOUNT_acct1_ALPACA_API_KEY", "some-key")
    monkeypatch.setenv("ACCOUNT_acct1_ALPACA_SECRET_KEY", "some-secret")
    monkeypatch.delenv("ACCOUNT_acct1_ALPACA_BASE_URL", raising=False)

    credentials = load_account_credentials("acct1")

    assert credentials.alpaca_base_url == "https://paper-api.alpaca.markets"


def test_alpaca_without_credentials_raises_instead_of_connecting_empty(monkeypatch):
    monkeypatch.setenv("ACCOUNT_acct1_BROKER", "alpaca")
    monkeypatch.delenv("ACCOUNT_acct1_ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ACCOUNT_acct1_ALPACA_SECRET_KEY", raising=False)

    with pytest.raises(RuntimeError, match="ACCOUNT_acct1_ALPACA_API_KEY"):
        load_account_credentials("acct1")


def test_two_alpaca_accounts_can_be_paper_and_live_independently(monkeypatch):
    monkeypatch.setenv("ACCOUNT_paper_acct_BROKER", "alpaca")
    monkeypatch.setenv("ACCOUNT_paper_acct_ALPACA_API_KEY", "paper-key")
    monkeypatch.setenv("ACCOUNT_paper_acct_ALPACA_SECRET_KEY", "paper-secret")
    monkeypatch.setenv("ACCOUNT_paper_acct_ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

    monkeypatch.setenv("ACCOUNT_live_acct_BROKER", "alpaca")
    monkeypatch.setenv("ACCOUNT_live_acct_ALPACA_API_KEY", "live-key")
    monkeypatch.setenv("ACCOUNT_live_acct_ALPACA_SECRET_KEY", "live-secret")
    monkeypatch.setenv("ACCOUNT_live_acct_ALPACA_BASE_URL", "https://api.alpaca.markets")

    paper = load_account_credentials("paper_acct")
    live = load_account_credentials("live_acct")

    assert paper.alpaca_base_url == "https://paper-api.alpaca.markets"
    assert live.alpaca_base_url == "https://api.alpaca.markets"
    assert paper.alpaca_api_key == "paper-key"
    assert live.alpaca_api_key == "live-key"


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
