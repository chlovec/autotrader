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
    alpaca_api_key: str
    alpaca_secret_key: str
    alpaca_base_url: str
    alpaca_paper: bool

    max_position_size_usd: float
    max_daily_loss_usd: float
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    alert_email_from: str
    alert_email_to: str


def load_config() -> Config:
    return Config(
        broker=os.environ.get("BROKER", "alpaca"),
        alpaca_api_key=os.environ.get("ALPACA_API_KEY", ""),
        alpaca_secret_key=os.environ.get("ALPACA_SECRET_KEY", ""),
        alpaca_base_url=os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets"),
        alpaca_paper=os.environ.get("ALPACA_PAPER", "true").lower() == "true",
        max_position_size_usd=float(os.environ.get("MAX_POSITION_SIZE_USD", "1000")),
        max_daily_loss_usd=float(os.environ.get("MAX_DAILY_LOSS_USD", "200")),
        smtp_host=os.environ.get("SMTP_HOST", ""),
        smtp_port=int(os.environ.get("SMTP_PORT", "587")),
        smtp_username=os.environ.get("SMTP_USERNAME", ""),
        smtp_password=os.environ.get("SMTP_PASSWORD", ""),
        alert_email_from=os.environ.get("ALERT_EMAIL_FROM", ""),
        alert_email_to=os.environ.get("ALERT_EMAIL_TO", ""),
    )
