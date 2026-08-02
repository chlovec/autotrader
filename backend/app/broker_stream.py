"""Keeps each active account's equity/cash/positions/trades in sync with its broker in real
time. One AccountStream per active account (backend/app/main.py creates/tears these down as
accounts are activated/deactivated) - each holds its own BrokerClient.stream() connection (or,
for Questrade, a simulated poll loop - see engine/brokers/questrade_broker.py) that tells this
module *that* something may have changed for *that* account; _reconcile() is the one place
that re-fetches the actual truth from that account's broker, writes it to the DB, and pushes it
to every dashboard connected to that account's /ws/accounts/{id}. This "dumb signal in, smart
handling once centrally" split means the three very different broker event shapes (Alpaca order
events, IBKR's several account/order callbacks, Questrade's plain timer) never need their own
bespoke handling downstream - now just replicated once per active account instead of once per
process.
"""

import asyncio
import datetime as dt
import logging
from dataclasses import asdict

from fastapi import WebSocket
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import EquitySnapshot, Trade
from db.session import get_session
from engine.brokers.base import BrokerClient, BrokerOrder

logger = logging.getLogger("autotrader.backend.broker_stream")

# Protects against a dropped/reconnecting stream (Questrade's poll loop especially, but
# also Alpaca/IBKR if their connection silently dies) leaving a dashboard stale forever.
RECONCILE_SAFETY_NET_INTERVAL_SECONDS = 30
# How far back to look for orders on a cold start / after downtime, when there's no
# Trade row yet to derive "since" from.
_ORDER_LOOKBACK = dt.timedelta(hours=24)
# Alpaca fires one event per partial fill and IBKR fires several per order lifecycle -
# absorb the rest of a burst instead of reconciling once per individual event.
_BURST_COALESCE_SECONDS = 0.5
_STREAM_RECONNECT_DELAY_SECONDS = 5


class ConnectionManager:
    """Tracks connected dashboard websocket clients per account_id, so _reconcile() can
    push only to the clients watching that account - replacing the old model of one global
    connection set for the single account this backend used to run."""

    def __init__(self) -> None:
        self._connections: dict[str, set[WebSocket]] = {}

    async def connect(self, account_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.setdefault(account_id, set()).add(websocket)

    def disconnect(self, account_id: str, websocket: WebSocket) -> None:
        self._connections.get(account_id, set()).discard(websocket)

    async def broadcast(self, account_id: str, message: dict) -> None:
        for websocket in list(self._connections.get(account_id, set())):
            try:
                await websocket.send_json(message)
            except Exception:
                self.disconnect(account_id, websocket)


manager = ConnectionManager()


def _orders_since(session: Session, account_id: str) -> dt.datetime:
    """Derived from the DB, not in-memory state, so a restarted backend self-heals over
    any downtime instead of missing orders placed while it was down."""
    latest = session.execute(select(func.max(Trade.submitted_at)).where(Trade.account_id == account_id)).scalar_one_or_none()
    floor = dt.datetime.utcnow() - _ORDER_LOOKBACK
    return max(latest, floor) if latest else floor


def _upsert_trades(session: Session, account_id: str, broker_name: str, broker_account_id: str, orders: list[BrokerOrder]) -> list[Trade]:
    """Inserts unknown broker_order_ids as new Trade rows with signal_id=None (a trade
    this app didn't place, e.g. a customer trading directly through the broker's own
    UI) and patches status/fill_price/filled_at on rows that already exist. Never
    touches signal_id/symbol/side/qty on an existing row - those may have been set by
    ExecutionEngine.submit_market_order (engine/execution.py) and must survive.

    Matches (and inserts) are always scoped to account_id - broker_order_id alone isn't a
    safe key across accounts (see db/models.py's Trade.__table_args__)."""
    changed: list[Trade] = []
    for order in orders:
        existing = session.execute(
            select(Trade).where(Trade.account_id == account_id, Trade.broker_order_id == order.broker_order_id)
        ).scalar_one_or_none()
        if existing is None:
            trade = Trade(
                signal_id=None,
                account_id=account_id,
                broker=broker_name,
                broker_account_id=broker_account_id,
                broker_order_id=order.broker_order_id,
                symbol=order.symbol,
                side=order.side,
                qty=order.qty,
                fill_price=order.fill_price,
                status=order.status,
                submitted_at=order.submitted_at,
                filled_at=order.filled_at,
            )
            session.add(trade)
            changed.append(trade)
        elif (existing.status, existing.fill_price, existing.filled_at) != (order.status, order.fill_price, order.filled_at):
            existing.status = order.status
            existing.fill_price = order.fill_price
            existing.filled_at = order.filled_at
            changed.append(existing)
    if changed:
        session.flush()
    return changed


def _trade_dict(trade: Trade) -> dict:
    return {
        "id": trade.id,
        "broker_order_id": trade.broker_order_id,
        "symbol": trade.symbol,
        "side": trade.side.value,
        "qty": trade.qty,
        "fill_price": trade.fill_price,
        "status": trade.status,
        "submitted_at": trade.submitted_at.isoformat(),
    }


async def _reconcile(account_id: str, broker: BrokerClient) -> None:
    # get_account/get_positions/get_recent_orders are blocking REST/SDK calls with no
    # async variant for Alpaca/Questrade - run them off-loop so a reconciliation pass
    # doesn't stall /health or any other connected /ws/accounts/{id} client.
    account = await asyncio.to_thread(broker.get_account)
    positions = await asyncio.to_thread(broker.get_positions)
    with get_session() as session:
        snapshot = EquitySnapshot(
            account_id=account_id, broker=broker.name, broker_account_id=account.account_id,
            equity=account.equity, cash=account.cash, buying_power=account.buying_power,
        )
        session.add(snapshot)
        session.flush()
        since = _orders_since(session, account_id)
        orders = await asyncio.to_thread(broker.get_recent_orders, since)
        changed_trades = _upsert_trades(session, account_id, broker.name, account.account_id, orders)
        timestamp = snapshot.timestamp.isoformat()
        trade_payloads = [_trade_dict(t) for t in changed_trades]
        session.commit()

    await manager.broadcast(
        account_id,
        {"type": "equity", "equity": account.equity, "cash": account.cash, "buying_power": account.buying_power, "timestamp": timestamp},
    )
    await manager.broadcast(account_id, {"type": "positions", "positions": [asdict(p) for p in positions]})
    if trade_payloads:
        await manager.broadcast(account_id, {"type": "trades", "trades": trade_payloads})


class AccountStream:
    """One account's live-update pipeline: its broker stream/poll connection, the consumer
    that coalesces bursts of change events into a single reconciliation pass, and a safety
    net that reconciles on a fixed interval regardless (in case the stream connection drops
    silently). Each active account gets its own instance - state (the change event, the
    reconcile lock) used to be module-level globals when this backend only ever ran one
    account; now it's per-instance so accounts' reconciliation passes never block or race
    each other."""

    def __init__(self, account_id: str, broker: BrokerClient) -> None:
        self.account_id = account_id
        self.broker = broker
        self._change_event = asyncio.Event()
        self._reconcile_lock = asyncio.Lock()
        self._stream_task = asyncio.create_task(self._run_stream())
        self._consumer_task = asyncio.create_task(self._reconcile_consumer())
        self._safety_net_task = asyncio.create_task(self._safety_net_loop())

    async def _on_change(self) -> None:
        self._change_event.set()

    async def _run_stream(self) -> None:
        """broker.stream() can and will drop (network blip, broker-side restart) -
        reconnect rather than letting this account's live-update pipeline die silently."""
        while True:
            try:
                await self.broker.stream(self._on_change)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[%s] broker stream died, reconnecting in %ss", self.account_id, _STREAM_RECONNECT_DELAY_SECONDS)
                await asyncio.sleep(_STREAM_RECONNECT_DELAY_SECONDS)

    async def _reconcile_locked(self) -> None:
        async with self._reconcile_lock:
            await _reconcile(self.account_id, self.broker)

    async def _reconcile_consumer(self) -> None:
        while True:
            await self._change_event.wait()
            self._change_event.clear()
            await asyncio.sleep(_BURST_COALESCE_SECONDS)
            self._change_event.clear()
            try:
                await self._reconcile_locked()
            except Exception:
                logger.exception("[%s] reconciliation failed", self.account_id)

    async def _safety_net_loop(self) -> None:
        while True:
            await asyncio.sleep(RECONCILE_SAFETY_NET_INTERVAL_SECONDS)
            try:
                await self._reconcile_locked()
            except Exception:
                logger.exception("[%s] safety-net reconciliation failed", self.account_id)

    async def stop(self) -> None:
        for task in (self._stream_task, self._consumer_task, self._safety_net_task):
            task.cancel()
        await asyncio.gather(self._stream_task, self._consumer_task, self._safety_net_task, return_exceptions=True)


def start(account_id: str, broker: BrokerClient) -> AccountStream:
    return AccountStream(account_id, broker)


async def send_snapshot(websocket: WebSocket, account_id: str, broker: BrokerClient) -> None:
    """Sent once, right after a client connects, so the dashboard shows current data
    immediately instead of waiting for the next reconciliation event."""
    positions = await asyncio.to_thread(broker.get_positions)
    with get_session() as session:
        latest_equity = session.execute(
            select(EquitySnapshot).where(EquitySnapshot.account_id == account_id).order_by(EquitySnapshot.timestamp.desc())
        ).scalars().first()
        trades = session.execute(
            select(Trade).where(Trade.account_id == account_id).order_by(Trade.submitted_at.desc()).limit(200)
        ).scalars().all()
        equity_payload = (
            {
                "equity": latest_equity.equity,
                "cash": latest_equity.cash,
                "buying_power": latest_equity.buying_power,
                "timestamp": latest_equity.timestamp.isoformat(),
            }
            if latest_equity
            else None
        )
        trade_payloads = [_trade_dict(t) for t in reversed(trades)]
    await websocket.send_json(
        {"type": "snapshot", "equity": equity_payload, "positions": [asdict(p) for p in positions], "trades": trade_payloads}
    )
