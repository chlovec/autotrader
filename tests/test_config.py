import pytest

from engine.config import load_config


def test_paper_mode_uses_paper_keys():
    config = load_config(
        argv=[
            "--alpaca-paper", "true",
            "--alpaca-api-key", "paper-key",
            "--alpaca-secret-key", "paper-secret",
        ]
    )

    assert config.alpaca_paper is True
    assert config.alpaca_api_key == "paper-key"
    assert config.alpaca_secret_key == "paper-secret"


def test_live_mode_uses_live_keys():
    config = load_config(
        argv=[
            "--alpaca-paper", "false",
            "--alpaca-api-key", "live-key",
            "--alpaca-secret-key", "live-secret",
        ]
    )

    assert config.alpaca_paper is False
    assert config.alpaca_api_key == "live-key"
    assert config.alpaca_secret_key == "live-secret"


def test_paper_mode_defaults_to_paper_base_url(monkeypatch):
    monkeypatch.delenv("ALPACA_BASE_URL", raising=False)

    config = load_config(
        argv=[
            "--alpaca-paper", "true",
            "--alpaca-api-key", "paper-key",
            "--alpaca-secret-key", "paper-secret",
        ]
    )

    assert config.alpaca_base_url == "https://paper-api.alpaca.markets"


def test_live_mode_defaults_to_live_base_url(monkeypatch):
    monkeypatch.delenv("ALPACA_LIVE_BASE_URL", raising=False)

    config = load_config(
        argv=[
            "--alpaca-paper", "false",
            "--alpaca-api-key", "live-key",
            "--alpaca-secret-key", "live-secret",
        ]
    )

    assert config.alpaca_base_url == "https://api.alpaca.markets"


def test_live_mode_uses_live_base_url_env_var(monkeypatch):
    monkeypatch.setenv("ALPACA_LIVE_BASE_URL", "https://env-live.alpaca.markets")

    config = load_config(
        argv=[
            "--alpaca-paper", "false",
            "--alpaca-api-key", "live-key",
            "--alpaca-secret-key", "live-secret",
        ]
    )

    assert config.alpaca_base_url == "https://env-live.alpaca.markets"


def test_defaults_to_paper_mode_when_unset(monkeypatch):
    monkeypatch.delenv("ALPACA_PAPER", raising=False)

    config = load_config(argv=["--alpaca-api-key", "paper-key", "--alpaca-secret-key", "paper-secret"])

    assert config.alpaca_paper is True
    assert config.alpaca_api_key == "paper-key"


def test_live_mode_without_live_keys_raises_instead_of_falling_back():
    with pytest.raises(RuntimeError, match="ALPACA_LIVE_API_KEY"):
        load_config(argv=["--alpaca-paper", "false"])


def test_falls_back_to_env_var_when_arg_not_passed(monkeypatch):
    """No CLI flags at all (argv=[]) - every field must come from the environment,
    same as before argparse was introduced."""
    monkeypatch.setenv("ALPACA_PAPER", "false")
    monkeypatch.setenv("ALPACA_LIVE_API_KEY", "env-live-key")
    monkeypatch.setenv("ALPACA_LIVE_SECRET_KEY", "env-live-secret")

    config = load_config(argv=[])

    assert config.alpaca_paper is False
    assert config.alpaca_api_key == "env-live-key"
    assert config.alpaca_secret_key == "env-live-secret"
