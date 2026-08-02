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


class Account(Base):
    """One tradeable account: a broker connection plus the dashboard-editable state that
    controls it (active flag, strategy assignment, risk limits, kill switch).

    `id` is a stable slug taken from the `ACCOUNT_IDS` .env var (e.g. "alpaca_paper") - it's
    also the foreign key every other table scopes its rows by. `broker`/`display_name` are
    synced from .env on every backend/engine startup (see engine/accounts.py's
    sync_accounts_from_env) and aren't dashboard-editable. Everything else here - `active`,
    `strategy_name`/`strategy_params`, the two limits, and the kill switch - is seeded from
    .env the first time an id is seen and owned by the dashboard from then on; a later env
    change to those values is deliberately ignored so a dashboard edit can't be silently
    reverted by a restart.

    max_total_exposure_usd caps the total value of open positions across every symbol at
    once (as opposed to max_position_size_usd, which caps one symbol) - 0 means no cap,
    which is also the backfilled value for any account that existed before this column
    did, so adding it never silently starts throttling an account that hasn't opted in.
    """

    __tablename__ = "accounts"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    broker: Mapped[str] = mapped_column(String, index=True)
    display_name: Mapped[str] = mapped_column(String)
    active: Mapped[bool] = mapped_column(default=True)
    strategy_name: Mapped[str] = mapped_column(String)
    strategy_params: Mapped[str] = mapped_column(Text, default="{}")
    max_position_size_usd: Mapped[float] = mapped_column(Float)
    max_daily_loss_usd: Mapped[float] = mapped_column(Float)
    max_total_exposure_usd: Mapped[float] = mapped_column(Float, default=0.0)
    kill_switch_engaged: Mapped[bool] = mapped_column(default=False)
    kill_switch_reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)

    signals: Mapped[list["Signal"]] = relationship(back_populates="account")
    trades: Mapped[list["Trade"]] = relationship(back_populates="account")


class Signal(Base):
    """A decision the strategy engine produced, whether or not it resulted in a trade."""

    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow, index=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    symbol: Mapped[str] = mapped_column(String, index=True)
    strategy_name: Mapped[str] = mapped_column(String)
    action: Mapped[SignalAction] = mapped_column(Enum(SignalAction))
    reason: Mapped[str] = mapped_column(Text, default="")

    account: Mapped[Account] = relationship(back_populates="signals")
    trades: Mapped[list["Trade"]] = relationship(back_populates="signal")


class Trade(Base):
    """An order placed with the broker, and its lifecycle."""

    __tablename__ = "trades"
    # broker_order_id is only unique within a given broker's own ID space (Alpaca uses
    # UUIDs, Questrade its own numeric IDs, but IBKR's orderId is a small sequential
    # integer scoped to a client session). Scoping by our own account_id rather than
    # (broker, broker_account_id) is simpler and still correct - one internal account maps
    # to exactly one broker connection.
    __table_args__ = (UniqueConstraint("account_id", "broker_order_id", name="uq_trades_account_order"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"), nullable=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    broker: Mapped[str] = mapped_column(String, index=True)
    # The broker's own account identifier (an Alpaca account UUID, etc.) - kept for
    # display/audit, distinct from account_id above which is our internal, dashboard-facing id.
    broker_account_id: Mapped[str] = mapped_column(String, index=True)
    broker_order_id: Mapped[str] = mapped_column(String, index=True)
    symbol: Mapped[str] = mapped_column(String, index=True)
    side: Mapped[OrderSide] = mapped_column(Enum(OrderSide))
    qty: Mapped[float] = mapped_column(Float)
    limit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String, default="new")
    submitted_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    filled_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    account: Mapped[Account] = relationship(back_populates="trades")
    signal: Mapped[Signal | None] = relationship(back_populates="trades")


class EquitySnapshot(Base):
    """Periodic snapshot of account equity, used to draw the equity curve."""

    __tablename__ = "equity_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow, index=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    broker: Mapped[str] = mapped_column(String, index=True)
    broker_account_id: Mapped[str] = mapped_column(String, index=True)
    equity: Mapped[float] = mapped_column(Float)
    cash: Mapped[float] = mapped_column(Float)
    buying_power: Mapped[float] = mapped_column(Float)


class SystemEvent(Base):
    """Errors, risk-limit trips, kill-switch activations, and other operational events.

    account_id is nullable - some events (a CORS misconfiguration, a research run) aren't
    about any one account.
    """

    __tablename__ = "system_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow, index=True)
    account_id: Mapped[str | None] = mapped_column(ForeignKey("accounts.id"), nullable=True, index=True)
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


class ResearchSchedule(Base):
    """Single-row toggle for the backend's automatic nightly research job (see
    backend/app/main.py's BackgroundScheduler). The dashboard's "Run research now"
    button bypasses this - it's a deliberate one-off action, not something the
    auto-schedule toggle should block."""

    __tablename__ = "research_schedule"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(default=True)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
