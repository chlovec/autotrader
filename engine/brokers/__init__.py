from engine.brokers.alpaca_broker import AlpacaBroker
from engine.brokers.base import AccountSnapshot, BrokerClient, ClockSnapshot, OrderResult, PositionSnapshot, Timeframe
from engine.config import Config

__all__ = [
    "AccountSnapshot",
    "BrokerClient",
    "ClockSnapshot",
    "OrderResult",
    "PositionSnapshot",
    "Timeframe",
    "make_broker",
]


def make_broker(config: Config) -> BrokerClient:
    """Alpaca is the only broker implemented today. When a second one exists,
    branch on a BROKER env var here - callers only ever depend on BrokerClient."""
    return AlpacaBroker(config)
