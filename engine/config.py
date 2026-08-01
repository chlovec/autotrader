import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    broker: str  # which BrokerClient implementation to use - see engine/brokers/make_broker()

    # Alpaca-specific. Only read/used when broker == "alpaca" - a different broker
    # would have its own fields here (IBKR has no API key at all; Questrade uses an
    # OAuth refresh token), not a shared "generic" shape, since brokers genuinely
    # don't share an auth model.
    #
    # alpaca_api_key/alpaca_secret_key are already resolved to whichever pair
    # matches alpaca_paper (see load_config()) - paper and live are separate
    # Alpaca accounts with separate keys, so AlpacaBroker just uses these two
    # fields without needing to know there were ever two pairs to choose from.
    alpaca_api_key: str
    alpaca_secret_key: str
    alpaca_base_url: str
    alpaca_paper: bool

    # IBKR-specific. Only read when broker == "ibkr". No API key - auth happens by
    # logging into a locally running TWS/Gateway instance yourself; this just
    # connects to its socket.
    ibkr_host: str
    ibkr_port: int
    ibkr_client_id: int

    # Questrade-specific. Only read when broker == "questrade". Auth is an OAuth
    # refresh token - see engine/brokers/questrade_broker.py for why this single
    # field isn't the whole story (Questrade rotates it on every use).
    questrade_refresh_token: str

    max_position_size_usd: float
    max_daily_loss_usd: float
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    alert_email_from: str
    alert_email_to: str


def load_config() -> Config:
    alpaca_paper = os.environ.get("ALPACA_PAPER", "true").lower() == "true"
    if alpaca_paper:
        alpaca_api_key = os.environ.get("ALPACA_API_KEY", "")
        alpaca_secret_key = os.environ.get("ALPACA_SECRET_KEY", "")
    else:
        alpaca_api_key = os.environ.get("ALPACA_LIVE_API_KEY", "")
        alpaca_secret_key = os.environ.get("ALPACA_LIVE_SECRET_KEY", "")

    return Config(
        broker=os.environ.get("BROKER", "alpaca"),
        alpaca_api_key=alpaca_api_key,
        alpaca_secret_key=alpaca_secret_key,
        alpaca_base_url=os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets"),
        alpaca_paper=alpaca_paper,
        ibkr_host=os.environ.get("IBKR_HOST", "127.0.0.1"),
        ibkr_port=int(os.environ.get("IBKR_PORT", "7497")),
        ibkr_client_id=int(os.environ.get("IBKR_CLIENT_ID", "1")),
        questrade_refresh_token=os.environ.get("QUESTRADE_REFRESH_TOKEN", ""),
        max_position_size_usd=float(os.environ.get("MAX_POSITION_SIZE_USD", "1000")),
        max_daily_loss_usd=float(os.environ.get("MAX_DAILY_LOSS_USD", "200")),
        smtp_host=os.environ.get("SMTP_HOST", ""),
        smtp_port=int(os.environ.get("SMTP_PORT", "587")),
        smtp_username=os.environ.get("SMTP_USERNAME", ""),
        smtp_password=os.environ.get("SMTP_PASSWORD", ""),
        alert_email_from=os.environ.get("ALERT_EMAIL_FROM", ""),
        alert_email_to=os.environ.get("ALERT_EMAIL_TO", ""),
    )
