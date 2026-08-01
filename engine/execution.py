from sqlalchemy.orm import Session

from db.models import OrderSide, SignalAction, SystemEvent, Trade
from engine.brokers.base import BrokerClient


class ExecutionEngine:
    """Translates an approved signal into a broker order and records the result."""

    def __init__(self, broker: BrokerClient, session: Session):
        self.broker = broker
        self.session = session

    def submit_market_order(self, symbol: str, action: SignalAction, qty: float, signal_id: int | None = None) -> Trade:
        try:
            result = self.broker.submit_market_order(symbol, action, qty)
        except Exception as exc:
            self.session.add(SystemEvent(level="error", source="execution", message=f"order submit failed for {symbol}: {exc}"))
            self.session.commit()
            raise

        trade = Trade(
            signal_id=signal_id,
            broker_order_id=result.broker_order_id,
            symbol=symbol,
            side=OrderSide.buy if action == SignalAction.buy else OrderSide.sell,
            qty=qty,
            status=result.status,
        )
        self.session.add(trade)
        self.session.commit()
        return trade
