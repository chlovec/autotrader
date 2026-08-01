import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    alpaca_api_key: str
    alpaca_secret_key: str
    alpaca_base_url: str
    alpaca_paper: bool
    max_position_size_usd: float
    max_daily_loss_usd: float


def load_config() -> Config:
    return Config(
        alpaca_api_key=os.environ.get("ALPACA_API_KEY", ""),
        alpaca_secret_key=os.environ.get("ALPACA_SECRET_KEY", ""),
        alpaca_base_url=os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets"),
        alpaca_paper=os.environ.get("ALPACA_PAPER", "true").lower() == "true",
        max_position_size_usd=float(os.environ.get("MAX_POSITION_SIZE_USD", "1000")),
        max_daily_loss_usd=float(os.environ.get("MAX_DAILY_LOSS_USD", "200")),
    )
