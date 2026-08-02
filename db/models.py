"""Shared schema written by the trading engine and read by the FastAPI backend."""

import datetime as dt
import enum

from sqlalchemy import DateTime, Enum, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class OrderSide(str, enum.Enum):
    buy = "buy"
    sell = "sell"


class SignalAction(str, enum.Enum):
    buy = "buy"
    sell = "sell"
    hold = "hold"


class EventLevel(str, enum.Enum):
    info = "info"
    warning = "warning"
    error = "error"
    critical = "critical"


class Signal(Base):
    """A decision the strategy engine produced, whether or not it resulted in a trade."""

    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow, index=True)
    symbol: Mapped[str] = mapped_column(String, index=True)
    strategy_name: Mapped[str] = mapped_column(String)
    action: Mapped[SignalAction] = mapped_column(Enum(SignalAction))
    reason: Mapped[str] = mapped_column(Text, default="")

    trades: Mapped[list["Trade"]] = relationship(back_populates="signal")


class Trade(Base):
    """An order placed with the broker, and its lifecycle."""

    __tablename__ = "trades"
    # broker_order_id is only unique within a given broker's own ID space (Alpaca uses
    # UUIDs, Questrade its own numeric IDs, but IBKR's orderId is a small sequential
    # integer scoped to a client session) - a single-column unique constraint would
    # wrongly reject a legitimate insert once multiple brokers/accounts share this table.
    __table_args__ = (UniqueConstraint("broker", "account_id", "broker_order_id", name="uq_trades_broker_account_order"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"), nullable=True)
    broker: Mapped[str] = mapped_column(String, index=True)
    account_id: Mapped[str] = mapped_column(String, index=True)
    broker_order_id: Mapped[str] = mapped_column(String, index=True)
    symbol: Mapped[str] = mapped_column(String, index=True)
    side: Mapped[OrderSide] = mapped_column(Enum(OrderSide))
    qty: Mapped[float] = mapped_column(Float)
    limit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String, default="new")
    submitted_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    filled_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    signal: Mapped[Signal | None] = relationship(back_populates="trades")


class EquitySnapshot(Base):
    """Periodic snapshot of account equity, used to draw the equity curve."""

    __tablename__ = "equity_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow, index=True)
    broker: Mapped[str] = mapped_column(String, index=True)
    account_id: Mapped[str] = mapped_column(String, index=True)
    equity: Mapped[float] = mapped_column(Float)
    cash: Mapped[float] = mapped_column(Float)
    buying_power: Mapped[float] = mapped_column(Float)


class SystemEvent(Base):
    """Errors, risk-limit trips, kill-switch activations, and other operational events."""

    __tablename__ = "system_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow, index=True)
    level: Mapped[EventLevel] = mapped_column(Enum(EventLevel))
    source: Mapped[str] = mapped_column(String)
    message: Mapped[str] = mapped_column(Text)


class ResearchResult(Base):
    """A symbol's screening score from one research run. Written for every symbol in the
    research universe (not just winners), so a symbol that didn't make the cut is still
    auditable - same philosophy as Signal logging even when no trade results.

    `run_at` has no column default: research_runner.research_once() generates one
    timestamp per run and passes it to every row explicitly, so "give me the latest run"
    queries (see db/queries.get_watchlist_symbols) can match on equality. A per-row
    default would give each row a microsecond-different timestamp and silently break that
    query into matching at most one row.
    """

    __tablename__ = "research_results"
    __table_args__ = (UniqueConstraint("run_at", "symbol", name="uq_research_results_run_symbol"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_at: Mapped[dt.datetime] = mapped_column(DateTime, index=True)
    symbol: Mapped[str] = mapped_column(String, index=True)
    technical_score: Mapped[float] = mapped_column(Float)
    news_score: Mapped[float] = mapped_column(Float)
    combined_score: Mapped[float] = mapped_column(Float, index=True)
    rationale: Mapped[str] = mapped_column(Text, default="")
    selected: Mapped[bool] = mapped_column(default=False)


class KillSwitch(Base):
    """Single-row table the trading loop polls each cycle; flipped by the dashboard."""

    __tablename__ = "kill_switch"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    engaged: Mapped[bool] = mapped_column(default=False)
    reason: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)


class ResearchSchedule(Base):
    """Single-row toggle for the backend's automatic nightly research job (see
    backend/app/main.py's BackgroundScheduler). The dashboard's "Run research now"
    button bypasses this - it's a deliberate one-off action, not something the
    auto-schedule toggle should block."""

    __tablename__ = "research_schedule"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(default=True)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
