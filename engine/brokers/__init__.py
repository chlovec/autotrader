from engine.brokers.alpaca_broker import AlpacaBroker
from engine.brokers.base import AccountSnapshot, BrokerClient, ClockSnapshot, OrderResult, PositionSnapshot, Timeframe
from engine.brokers.ibkr_broker import IBKRBroker
from engine.brokers.questrade_broker import QuestradeBroker
from engine.config import AccountCredentials

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
    "ibkr": IBKRBroker,
    "questrade": QuestradeBroker,
}


def make_broker(account_id: str, credentials: AccountCredentials) -> BrokerClient:
    """Selects a BrokerClient implementation based on credentials.broker (see
    engine/config.py's load_account_credentials, which reads ACCOUNT_<account_id>_BROKER).
    Callers only ever depend on the BrokerClient interface, never a specific broker.

    account_id is only actually used by QuestradeBroker (to scope its rotating-token
    cache file per account) - passed to every broker regardless so make_broker's signature
    doesn't need to know which brokers care."""
    try:
        broker_cls = _BROKERS[credentials.broker]
    except KeyError:
        supported = ", ".join(sorted(_BROKERS))
        raise ValueError(f"unsupported broker {credentials.broker!r} (ACCOUNT_{account_id}_BROKER) - supported: {supported}") from None
    if broker_cls is QuestradeBroker:
        return QuestradeBroker(credentials, account_id=account_id)
    return broker_cls(credentials)
