from __future__ import annotations

import datetime as dt
import enum
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol

import pandas as pd

from db.models import OrderSide, SignalAction


class Timeframe(enum.Enum):
    MINUTE = "minute"
    DAY = "day"


@dataclass(frozen=True)
class AccountSnapshot:
    equity: float
    cash: float
    buying_power: float
    account_id: str


@dataclass(frozen=True)
class ClockSnapshot:
    is_open: bool


@dataclass(frozen=True)
class PositionSnapshot:
    symbol: str
    qty: float
    avg_entry_price: float
    market_value: float
    unrealized_pl: float


@dataclass(frozen=True)
class OrderResult:
    broker_order_id: str
    status: str


@dataclass(frozen=True)
class BrokerOrder:
    """One order on the account, as reported by the broker - regardless of whether this
    app or something else (the broker's own UI, another client) submitted it. Used by
    the backend's reconciliation routine to detect trades this app didn't place itself."""

    broker_order_id: str
    symbol: str
    side: OrderSide
    qty: float
    status: str
    fill_price: float | None
    submitted_at: dt.datetime
    filled_at: dt.datetime | None


class BrokerClient(Protocol):
    """Everything the engine and backend need from a broker.

    AlpacaBroker is the only implementation today. Adding IBKR or Questrade means
    writing a new class against this interface - strategy, risk, execution, the
    backend API, and the backtest script never import a broker SDK directly.
    """

    name: str
    """One of "alpaca"/"ibkr"/"questrade" - used to tag EquitySnapshot/Trade rows so
    data from different brokers/accounts sharing the same DB never gets mixed together."""

    def get_account(self) -> AccountSnapshot: ...

    def get_clock(self) -> ClockSnapshot: ...

    def get_bars(
        self, symbol: str, timeframe: Timeframe, limit: int | None = None, start: dt.datetime | None = None
    ) -> pd.DataFrame:
        """Returns a DataFrame indexed ascending by time with open/high/low/close/volume columns."""
        ...

    def get_position_qty(self, symbol: str) -> float:
        """Returns 0.0 if there is no open position."""
        ...

    def get_positions(self) -> list[PositionSnapshot]: ...

    def submit_market_order(self, symbol: str, action: SignalAction, qty: float) -> OrderResult: ...

    def get_recent_orders(self, since: dt.datetime) -> list[BrokerOrder]:
        """Every order on the account submitted at or after `since`, including ones this
        app didn't place - not just ones ExecutionEngine.submit_market_order created."""
        ...

    async def stream(self, on_change: Callable[[], Awaitable[None]]) -> None:
        """Runs until cancelled, calling on_change() whenever the broker signals that
        something (an order, a fill, account state) may have changed. Never inspects or
        relays the event payload itself - callers are expected to re-fetch get_account()/
        get_positions()/get_recent_orders() for the current truth, since Alpaca, IBKR,
        and Questrade each expose completely different event shapes (or none at all, in
        Questrade's case - its "stream" is an internal poll loop calling on_change() on a
        timer)."""
        ...
