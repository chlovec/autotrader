import asyncio
import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import db.session as db_session
from backend.app import broker_stream
from db.models import Account, EquitySnapshot, OrderSide, Signal, SignalAction, Trade
from engine.brokers.base import AccountSnapshot, BrokerOrder


class FakeBroker:
    """Sync, in-memory BrokerClient stand-in - _reconcile() only needs get_account,
    get_positions, and get_recent_orders; it never calls stream()."""

    name = "fake"

    def __init__(self, account: AccountSnapshot, positions: list, orders: list[BrokerOrder]):
        self._account = account
        self._positions = positions
        self._orders = orders

    def get_account(self) -> AccountSnapshot:
        return self._account

    def get_positions(self) -> list:
        return self._positions

    def get_recent_orders(self, since: dt.datetime) -> list[BrokerOrder]:
        return self._orders


class _RecordingWebSocket:
    def __init__(self, fail: bool = False):
        self.sent: list[dict] = []
        self._fail = fail

    async def accept(self) -> None:
        pass

    async def send_json(self, message: dict) -> None:
        if self._fail:
            raise RuntimeError("connection closed")
        self.sent.append(message)


@pytest.fixture
def db(tmp_path, monkeypatch):
    # File-based sqlite, matching tests/test_backend.py's fixture rationale.
    test_engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db_session, "engine", test_engine)
    monkeypatch.setattr(db_session, "SessionLocal", sessionmaker(bind=test_engine, expire_on_commit=False))
    db_session.init_db()
    with db_session.get_session() as session:
        session.add(Account(id="acct-1", broker="fake", display_name="Acct 1", strategy_name="rebalancing_portfolio",
                             max_position_size_usd=1000.0, max_daily_loss_usd=200.0))
        session.commit()
    return db_session


def test_reconcile_writes_equity_snapshot(db):
    account = AccountSnapshot(equity=1000.0, cash=500.0, buying_power=2000.0, account_id="broker-acct-1")
    broker = FakeBroker(account, [], [])

    asyncio.run(broker_stream._reconcile("acct-1", broker))

    with db.get_session() as session:
        rows = session.query(EquitySnapshot).all()
        assert len(rows) == 1
        assert rows[0].equity == 1000.0
        assert rows[0].cash == 500.0
        assert rows[0].buying_power == 2000.0
        assert rows[0].broker == "fake"
        assert rows[0].account_id == "acct-1"
        assert rows[0].broker_account_id == "broker-acct-1"


def test_reconcile_inserts_unknown_order_as_trade_with_no_signal(db):
    account = AccountSnapshot(equity=1000.0, cash=500.0, buying_power=2000.0, account_id="broker-acct-1")
    order = BrokerOrder(
        broker_order_id="manual-1",
        symbol="AAPL",
        side=OrderSide.buy,
        qty=10,
        status="filled",
        fill_price=150.0,
        submitted_at=dt.datetime.utcnow(),
        filled_at=dt.datetime.utcnow(),
    )
    broker = FakeBroker(account, [], [order])

    asyncio.run(broker_stream._reconcile("acct-1", broker))

    with db.get_session() as session:
        trade = session.query(Trade).filter_by(broker_order_id="manual-1").one()
        assert trade.signal_id is None
        assert trade.broker == "fake"
        assert trade.account_id == "acct-1"
        assert trade.broker_account_id == "broker-acct-1"
        assert trade.symbol == "AAPL"
        assert trade.status == "filled"
        assert trade.fill_price == 150.0


def test_reconcile_never_overwrites_signal_id_or_order_details_on_existing_trade(db):
    account = AccountSnapshot(equity=1000.0, cash=500.0, buying_power=2000.0, account_id="broker-acct-1")
    with db.get_session() as session:
        signal = Signal(account_id="acct-1", symbol="MSFT", strategy_name="test", action=SignalAction.buy, reason="test")
        session.add(signal)
        session.commit()
        session.add(
            Trade(
                signal_id=signal.id,
                account_id="acct-1",
                broker="fake",
                broker_account_id="broker-acct-1",
                broker_order_id="own-1",
                symbol="MSFT",
                side=OrderSide.buy,
                qty=5,
                status="new",
                submitted_at=dt.datetime.utcnow(),
            )
        )
        session.commit()
        signal_id = signal.id

    # The broker now reports this same order as filled - status/fill_price/filled_at
    # should update, but signal_id/symbol/side/qty must survive untouched.
    order = BrokerOrder(
        broker_order_id="own-1",
        symbol="MSFT",
        side=OrderSide.buy,
        qty=5,
        status="filled",
        fill_price=300.0,
        submitted_at=dt.datetime.utcnow() - dt.timedelta(minutes=1),
        filled_at=dt.datetime.utcnow(),
    )
    broker = FakeBroker(account, [], [order])

    asyncio.run(broker_stream._reconcile("acct-1", broker))

    with db.get_session() as session:
        trade = session.query(Trade).filter_by(broker_order_id="own-1").one()
        assert trade.signal_id == signal_id
        assert trade.symbol == "MSFT"
        assert trade.qty == 5
        assert trade.status == "filled"
        assert trade.fill_price == 300.0


def test_reconcile_scopes_trade_lookup_to_account_id(db):
    """A same broker_order_id under a different account_id must not be matched - each
    internal account is its own broker connection, so cross-account collisions would be
    a correctness bug, not just noise."""
    with db.get_session() as session:
        session.add(Account(id="acct-2", broker="fake", display_name="Acct 2", strategy_name="rebalancing_portfolio",
                             max_position_size_usd=1000.0, max_daily_loss_usd=200.0))
        session.add(
            Trade(
                account_id="acct-2", broker="fake", broker_account_id="broker-acct-2", broker_order_id="shared-id",
                symbol="AAPL", side=OrderSide.buy, qty=1, status="new", submitted_at=dt.datetime.utcnow(),
            )
        )
        session.commit()

    account = AccountSnapshot(equity=1000.0, cash=500.0, buying_power=2000.0, account_id="broker-acct-1")
    order = BrokerOrder(
        broker_order_id="shared-id", symbol="AAPL", side=OrderSide.buy, qty=1, status="filled",
        fill_price=150.0, submitted_at=dt.datetime.utcnow(), filled_at=dt.datetime.utcnow(),
    )
    broker = FakeBroker(account, [], [order])

    asyncio.run(broker_stream._reconcile("acct-1", broker))

    with db.get_session() as session:
        trades = session.query(Trade).filter_by(broker_order_id="shared-id").all()
        assert len(trades) == 2
        acct1_trade = next(t for t in trades if t.account_id == "acct-1")
        acct2_trade = next(t for t in trades if t.account_id == "acct-2")
        assert acct1_trade.status == "filled"
        assert acct2_trade.status == "new"  # untouched by acct-1's reconciliation


def test_broadcast_sends_only_to_connections_for_that_account():
    manager = broker_stream.ConnectionManager()
    good = _RecordingWebSocket()
    other_account = _RecordingWebSocket()
    bad = _RecordingWebSocket(fail=True)

    async def scenario() -> None:
        await manager.connect("acct-1", good)
        await manager.connect("acct-1", bad)
        await manager.connect("acct-2", other_account)
        await manager.broadcast("acct-1", {"type": "equity", "equity": 1})
        await manager.broadcast("acct-1", {"type": "equity", "equity": 2})

    asyncio.run(scenario())

    assert good.sent == [{"type": "equity", "equity": 1}, {"type": "equity", "equity": 2}]
    assert other_account.sent == []
    assert bad not in manager._connections.get("acct-1", set())
