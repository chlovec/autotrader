from sqlalchemy.orm import Session

from db.models import OrderSide, SignalAction, Trade
from engine.brokers.base import AccountSnapshot, BrokerClient
from engine.notifications import Notifier, log_and_notify


class ExecutionEngine:
    """Translates an approved signal into a broker order and records the result.

    account_id is our internal account id (db.models.Account.id) - what every table now
    scopes rows by. account_snapshot is the broker's own AccountSnapshot, kept only for
    broker_account_id (display/audit)."""

    def __init__(self, broker: BrokerClient, account_id: str, account_snapshot: AccountSnapshot, session: Session, notifier: Notifier):
        self.broker = broker
        self.account_id = account_id
        self.account_snapshot = account_snapshot
        self.session = session
        self.notifier = notifier

    def submit_market_order(self, symbol: str, action: SignalAction, qty: float, signal_id: int | None = None) -> Trade:
        try:
            result = self.broker.submit_market_order(symbol, action, qty)
        except Exception as exc:
            log_and_notify(
                self.session, self.notifier, "error", "execution",
                f"order submit failed for {symbol} ({action.value} {qty}): {exc}", account_id=self.account_id,
            )
            raise

        trade = Trade(
            signal_id=signal_id,
            account_id=self.account_id,
            broker=self.broker.name,
            broker_account_id=self.account_snapshot.account_id,
            broker_order_id=result.broker_order_id,
            symbol=symbol,
            side=OrderSide.buy if action == SignalAction.buy else OrderSide.sell,
            qty=qty,
            status=result.status,
        )
        self.session.add(trade)
        self.session.commit()
        return trade
