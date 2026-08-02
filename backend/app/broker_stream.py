"""Keeps the dashboard's equity/cash/positions/trades in sync with the broker in real
time. A single BrokerClient.stream() connection (or, for Questrade, a simulated poll
loop - see engine/brokers/questrade_broker.py) tells this module *that* something may
have changed; _reconcile() is the one place that re-fetches the actual truth from the
broker, writes it to the DB, and pushes it to every connected dashboard over /ws. This
"dumb signal in, smart handling once centrally" split means the three very different
broker event shapes (Alpaca order events, IBKR's several account/order callbacks,
Questrade's plain timer) never need their own bespoke handling downstream.
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
# also Alpaca/IBKR if their connection silently dies) leaving the dashboard stale forever.
RECONCILE_SAFETY_NET_INTERVAL_SECONDS = 30
# How far back to look for orders on a cold start / after downtime, when there's no
# Trade row yet to derive "since" from.
_ORDER_LOOKBACK = dt.timedelta(hours=24)
# Alpaca fires one event per partial fill and IBKR fires several per order lifecycle -
# absorb the rest of a burst instead of reconciling once per individual event.
_BURST_COALESCE_SECONDS = 0.5
_STREAM_RECONNECT_DELAY_SECONDS = 5


class ConnectionManager:
    """Tracks connected dashboard websocket clients so _reconcile() can push to all of
    them, replacing the old model of each connection polling the DB on its own timer."""

    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self._connections.discard(websocket)

    async def broadcast(self, message: dict) -> None:
        for websocket in list(self._connections):
            try:
                await websocket.send_json(message)
            except Exception:
                self.disconnect(websocket)


manager = ConnectionManager()

_change_event = asyncio.Event()
_reconcile_lock = asyncio.Lock()


async def on_change() -> None:
    """Passed to BrokerClient.stream() - called on every broker signal, regardless of
    what it was. Never carries a payload; _reconcile_consumer re-fetches full state."""
    _change_event.set()


def _orders_since(session: Session, broker_name: str, account_id: str) -> dt.datetime:
    """Derived from the DB, not in-memory state, so a restarted backend self-heals over
    any downtime instead of missing orders placed while it was down. Scoped to the
    currently active broker/account so a prior broker/account's order history never
    influences this one's lookback window."""
    latest = session.execute(
        select(func.max(Trade.submitted_at)).where(Trade.broker == broker_name, Trade.account_id == account_id)
    ).scalar_one_or_none()
    floor = dt.datetime.utcnow() - _ORDER_LOOKBACK
    return max(latest, floor) if latest else floor


def _upsert_trades(session: Session, broker_name: str, account_id: str, orders: list[BrokerOrder]) -> list[Trade]:
    """Inserts unknown broker_order_ids as new Trade rows with signal_id=None (a trade
    this app didn't place, e.g. a customer trading directly through the broker's own
    UI) and patches status/fill_price/filled_at on rows that already exist. Never
    touches signal_id/symbol/side/qty on an existing row - those may have been set by
    ExecutionEngine.submit_market_order (engine/execution.py) and must survive.

    Matches (and inserts) are always scoped to broker+account_id - broker_order_id alone
    isn't a safe key across brokers (see db/models.py's Trade.__table_args__)."""
    changed: list[Trade] = []
    for order in orders:
        existing = session.execute(
            select(Trade).where(
                Trade.broker == broker_name,
                Trade.account_id == account_id,
                Trade.broker_order_id == order.broker_order_id,
            )
        ).scalar_one_or_none()
        if existing is None:
            trade = Trade(
                signal_id=None,
                broker=broker_name,
                account_id=account_id,
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


async def _reconcile(broker: BrokerClient) -> None:
    async with _reconcile_lock:
        # get_account/get_positions/get_recent_orders are blocking REST/SDK calls with no
        # async variant for Alpaca/Questrade - run them off-loop so a reconciliation pass
        # doesn't stall /health, /positions, or every other connected /ws client.
        account = await asyncio.to_thread(broker.get_account)
        positions = await asyncio.to_thread(broker.get_positions)
        with get_session() as session:
            snapshot = EquitySnapshot(
                equity=account.equity, cash=account.cash, buying_power=account.buying_power,
                broker=broker.name, account_id=account.account_id,
            )
            session.add(snapshot)
            session.flush()
            since = _orders_since(session, broker.name, account.account_id)
            orders = await asyncio.to_thread(broker.get_recent_orders, since)
            changed_trades = _upsert_trades(session, broker.name, account.account_id, orders)
            timestamp = snapshot.timestamp.isoformat()
            trade_payloads = [_trade_dict(t) for t in changed_trades]
            session.commit()

        await manager.broadcast(
            {
                "type": "equity",
                "equity": account.equity,
                "cash": account.cash,
                "buying_power": account.buying_power,
                "timestamp": timestamp,
            }
        )
        await manager.broadcast({"type": "positions", "positions": [asdict(p) for p in positions]})
        if trade_payloads:
            await manager.broadcast({"type": "trades", "trades": trade_payloads})


async def _reconcile_consumer(broker: BrokerClient) -> None:
    while True:
        await _change_event.wait()
        _change_event.clear()
        await asyncio.sleep(_BURST_COALESCE_SECONDS)
        _change_event.clear()
        try:
            await _reconcile(broker)
        except Exception:
            logger.exception("reconciliation failed")


async def _safety_net_loop(broker: BrokerClient) -> None:
    while True:
        await asyncio.sleep(RECONCILE_SAFETY_NET_INTERVAL_SECONDS)
        try:
            await _reconcile(broker)
        except Exception:
            logger.exception("safety-net reconciliation failed")


async def _run_broker_stream(broker: BrokerClient) -> None:
    """broker.stream() can and will drop (network blip, broker-side restart) - reconnect
    rather than letting the whole live-update pipeline die silently."""
    while True:
        try:
            await broker.stream(on_change)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("broker stream died, reconnecting in %ss", _STREAM_RECONNECT_DELAY_SECONDS)
            await asyncio.sleep(_STREAM_RECONNECT_DELAY_SECONDS)


class BrokerStreamHandle:
    def __init__(self, stream_task: asyncio.Task, consumer_task: asyncio.Task, safety_net_task: asyncio.Task) -> None:
        self.stream_task = stream_task
        self.consumer_task = consumer_task
        self.safety_net_task = safety_net_task

    async def stop(self) -> None:
        for task in (self.stream_task, self.consumer_task, self.safety_net_task):
            task.cancel()
        await asyncio.gather(self.stream_task, self.consumer_task, self.safety_net_task, return_exceptions=True)


def start(broker: BrokerClient) -> BrokerStreamHandle:
    return BrokerStreamHandle(
        stream_task=asyncio.create_task(_run_broker_stream(broker)),
        consumer_task=asyncio.create_task(_reconcile_consumer(broker)),
        safety_net_task=asyncio.create_task(_safety_net_loop(broker)),
    )


async def send_snapshot(websocket: WebSocket, broker: BrokerClient, account_id: str) -> None:
    """Sent once, right after a client connects, so the dashboard shows current data
    immediately instead of waiting for the next reconciliation event. account_id is
    passed in (cached once at backend startup) rather than re-fetched here, so a
    dashboard reconnect/page-load never costs an extra broker round-trip just to learn
    an account id that hasn't changed since the process started."""
    positions = await asyncio.to_thread(broker.get_positions)
    with get_session() as session:
        latest_equity = session.execute(
            select(EquitySnapshot)
            .where(EquitySnapshot.broker == broker.name, EquitySnapshot.account_id == account_id)
            .order_by(EquitySnapshot.timestamp.desc())
        ).scalars().first()
        trades = session.execute(
            select(Trade)
            .where(Trade.broker == broker.name, Trade.account_id == account_id)
            .order_by(Trade.submitted_at.desc())
            .limit(200)
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
