from __future__ import annotations

import datetime as dt
import enum
from dataclasses import dataclass
from typing import Protocol

import pandas as pd

from db.models import SignalAction


class Timeframe(enum.Enum):
    MINUTE = "minute"
    DAY = "day"


@dataclass(frozen=True)
class AccountSnapshot:
    equity: float
    cash: float
    buying_power: float


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


class BrokerClient(Protocol):
    """Everything the engine and backend need from a broker.

    AlpacaBroker is the only implementation today. Adding IBKR or Questrade means
    writing a new class against this interface - strategy, risk, execution, the
    backend API, and the backtest script never import a broker SDK directly.
    """

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
