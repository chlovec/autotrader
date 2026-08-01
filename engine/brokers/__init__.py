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

_BROKERS = {
    "alpaca": AlpacaBroker,
    # Add a new entry here (e.g. "ibkr": IBKRBroker) once a second BrokerClient
    # implementation exists. Its config fields go in engine/config.py alongside
    # (not replacing) the alpaca_* fields - see the comment there.
}


def make_broker(config: Config) -> BrokerClient:
    """Selects a BrokerClient implementation based on the BROKER env var (config.broker).
    Callers only ever depend on the BrokerClient interface, never a specific broker."""
    try:
        broker_cls = _BROKERS[config.broker]
    except KeyError:
        supported = ", ".join(sorted(_BROKERS))
        raise ValueError(f"unsupported broker {config.broker!r} (BROKER env var) - supported: {supported}") from None
    return broker_cls(config)
