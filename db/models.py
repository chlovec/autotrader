"""Shared schema written by the trading engine and read by the FastAPI backend."""

import datetime as dt
import enum

from sqlalchemy import DateTime, Enum, Float, ForeignKey, Integer, String, Text
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

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"), nullable=True)
    broker_order_id: Mapped[str] = mapped_column(String, unique=True, index=True)
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


class KillSwitch(Base):
    """Single-row table the trading loop polls each cycle; flipped by the dashboard."""

    __tablename__ = "kill_switch"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    engaged: Mapped[bool] = mapped_column(default=False)
    reason: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
