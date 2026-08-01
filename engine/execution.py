from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide as AlpacaOrderSide
from alpaca.trading.enums import TimeInForce
from alpaca.trading.requests import MarketOrderRequest
from sqlalchemy.orm import Session

from db.models import OrderSide, SignalAction, SystemEvent, Trade


class ExecutionEngine:
    """Translates an approved signal into a broker order and records the result."""

    def __init__(self, trading_client: TradingClient, session: Session):
        self.trading_client = trading_client
        self.session = session

    def submit_market_order(self, symbol: str, action: SignalAction, qty: float, signal_id: int | None = None) -> Trade:
        side = AlpacaOrderSide.BUY if action == SignalAction.buy else AlpacaOrderSide.SELL
        request = MarketOrderRequest(symbol=symbol, qty=qty, side=side, time_in_force=TimeInForce.DAY)

        try:
            order = self.trading_client.submit_order(request)
        except Exception as exc:
            self.session.add(SystemEvent(level="error", source="execution", message=f"order submit failed for {symbol}: {exc}"))
            self.session.commit()
            raise

        trade = Trade(
            signal_id=signal_id,
            broker_order_id=str(order.id),
            symbol=symbol,
            side=OrderSide.buy if action == SignalAction.buy else OrderSide.sell,
            qty=qty,
            status=order.status.value if hasattr(order.status, "value") else str(order.status),
        )
        self.session.add(trade)
        self.session.commit()
        return trade
