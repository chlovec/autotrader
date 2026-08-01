from engine.config import load_config


def test_paper_mode_uses_paper_keys(monkeypatch):
    monkeypatch.setenv("ALPACA_PAPER", "true")
    monkeypatch.setenv("ALPACA_API_KEY", "paper-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "paper-secret")
    monkeypatch.setenv("ALPACA_LIVE_API_KEY", "live-key")
    monkeypatch.setenv("ALPACA_LIVE_SECRET_KEY", "live-secret")

    config = load_config()

    assert config.alpaca_paper is True
    assert config.alpaca_api_key == "paper-key"
    assert config.alpaca_secret_key == "paper-secret"


def test_live_mode_uses_live_keys(monkeypatch):
    monkeypatch.setenv("ALPACA_PAPER", "false")
    monkeypatch.setenv("ALPACA_API_KEY", "paper-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "paper-secret")
    monkeypatch.setenv("ALPACA_LIVE_API_KEY", "live-key")
    monkeypatch.setenv("ALPACA_LIVE_SECRET_KEY", "live-secret")

    config = load_config()

    assert config.alpaca_paper is False
    assert config.alpaca_api_key == "live-key"
    assert config.alpaca_secret_key == "live-secret"


def test_defaults_to_paper_mode_when_unset(monkeypatch):
    monkeypatch.delenv("ALPACA_PAPER", raising=False)
    monkeypatch.setenv("ALPACA_API_KEY", "paper-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "paper-secret")

    config = load_config()

    assert config.alpaca_paper is True
    assert config.alpaca_api_key == "paper-key"
