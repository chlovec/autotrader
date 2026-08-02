import datetime as dt
import math
from typing import Awaitable, Callable

import pandas as pd
from alpaca.common.exceptions import APIError
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame as AlpacaTimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide as AlpacaOrderSide
from alpaca.trading.enums import QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest
from alpaca.trading.stream import TradingStream

from db.models import OrderSide, SignalAction
from engine.brokers.base import AccountSnapshot, BrokerOrder, ClockSnapshot, OrderResult, PositionSnapshot, Timeframe
from engine.config import Config

_TIMEFRAME_MAP = {
    Timeframe.MINUTE: AlpacaTimeFrame.Minute,
    Timeframe.DAY: AlpacaTimeFrame.Day,
}

_CALENDAR_DAYS_PER_TRADING_DAY = 7 / 5  # weekends alone cut ~5 of every 7 calendar days to trading days
_HOLIDAY_BUFFER_DAYS = 15  # slack for market holidays and "today"'s bar not being published yet
_MINUTES_PER_TRADING_DAY = 390


def _default_start(timeframe: Timeframe, limit: int) -> dt.datetime:
    """Alpaca's /v2/stocks/bars defaults `start` to ~today when omitted - not "the last
    `limit` bars" - so on a day the latest bar isn't published yet (e.g. a weekend), a
    limit-only request silently returns zero bars instead of falling back to the most
    recent trading days. Computes an explicit start wide enough to comfortably cover
    `limit` bars once weekends/holidays are excluded."""
    if timeframe == Timeframe.DAY:
        trading_days_needed = limit
    else:
        trading_days_needed = math.ceil(limit / _MINUTES_PER_TRADING_DAY) or 1
    calendar_days = math.ceil(trading_days_needed * _CALENDAR_DAYS_PER_TRADING_DAY) + _HOLIDAY_BUFFER_DAYS
    return dt.datetime.utcnow() - dt.timedelta(days=calendar_days)


class AlpacaBroker:
    """BrokerClient implementation backed by alpaca-py. All Alpaca SDK usage is
    confined to this file - everything else in the app talks to BrokerClient."""

    name = "alpaca"

    def __init__(self, config: Config):
        self._config = config
        self._trading = TradingClient(config.alpaca_api_key, config.alpaca_secret_key, paper=config.alpaca_paper)
        self._data = StockHistoricalDataClient(config.alpaca_api_key, config.alpaca_secret_key)

    def get_account(self) -> AccountSnapshot:
        account = self._trading.get_account()
        return AccountSnapshot(
            equity=float(account.equity),
            cash=float(account.cash),
            buying_power=float(account.buying_power),
            account_id=account.account_number,
        )

    def get_clock(self) -> ClockSnapshot:
        return ClockSnapshot(is_open=self._trading.get_clock().is_open)

    def get_bars(
        self, symbol: str, timeframe: Timeframe, limit: int | None = None, start: dt.datetime | None = None
    ) -> pd.DataFrame:
        using_default_start = start is None
        request_start = start if start is not None else _default_start(timeframe, limit or 100)
        # When we're the ones synthesizing `start` (see _default_start), don't also pass
        # `limit` to Alpaca - its default `sort` is ascending, so `limit` truncates from the
        # *oldest* end of the window, not the most recent bars. Fetch the whole window
        # unbounded instead and take the most recent `limit` bars ourselves below.
        request = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=_TIMEFRAME_MAP[timeframe],
            start=request_start,
            limit=None if using_default_start else limit,
        )
        df = self._data.get_stock_bars(request).df
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(symbol, level=0)
        df = df.sort_index()[["open", "high", "low", "close", "volume"]]
        if using_default_start and limit is not None:
            df = df.tail(limit)
        return df

    def get_position_qty(self, symbol: str) -> float:
        try:
            return abs(float(self._trading.get_open_position(symbol).qty))
        except APIError as exc:
            if exc.status_code == 404:
                return 0.0
            raise

    def get_positions(self) -> list[PositionSnapshot]:
        return [
            PositionSnapshot(
                symbol=p.symbol,
                qty=float(p.qty),
                avg_entry_price=float(p.avg_entry_price),
                market_value=float(p.market_value),
                unrealized_pl=float(p.unrealized_pl),
            )
            for p in self._trading.get_all_positions()
        ]

    def submit_market_order(self, symbol: str, action: SignalAction, qty: float) -> OrderResult:
        side = AlpacaOrderSide.BUY if action == SignalAction.buy else AlpacaOrderSide.SELL
        request = MarketOrderRequest(symbol=symbol, qty=qty, side=side, time_in_force=TimeInForce.DAY)
        order = self._trading.submit_order(request)
        status = order.status.value if hasattr(order.status, "value") else str(order.status)
        return OrderResult(broker_order_id=str(order.id), status=status)

    def get_recent_orders(self, since: dt.datetime) -> list[BrokerOrder]:
        request = GetOrdersRequest(status=QueryOrderStatus.ALL, after=since)
        orders = self._trading.get_orders(request)
        return [
            BrokerOrder(
                broker_order_id=str(order.id),
                symbol=order.symbol,
                side=OrderSide.buy if order.side == AlpacaOrderSide.BUY else OrderSide.sell,
                qty=float(order.qty),
                status=order.status.value if hasattr(order.status, "value") else str(order.status),
                fill_price=float(order.filled_avg_price) if order.filled_avg_price is not None else None,
                submitted_at=order.submitted_at,
                filled_at=order.filled_at,
            )
            for order in orders
        ]

    async def stream(self, on_change: Callable[[], Awaitable[None]]) -> None:
        """Subscribes to Alpaca's trade-updates websocket and calls on_change() on every
        order event (new/fill/partial_fill/canceled/rejected/...) - the payload itself is
        never inspected here, since the backend re-fetches full account/position/order
        state centrally on every signal (see backend/app/broker_stream.py).

        Uses TradingStream._run_forever() directly rather than TradingStream.run(), which
        wraps everything in asyncio.run() and would fail with "asyncio.run() cannot be
        called from a running event loop" since this runs inside the backend's own loop.
        _run_forever is not part of alpaca-py's public API - revisit this on an alpaca-py
        upgrade in case its name or behavior changes.
        """
        stream = TradingStream(self._config.alpaca_api_key, self._config.alpaca_secret_key, paper=self._config.alpaca_paper)

        async def _handler(data: object) -> None:
            await on_change()

        stream.subscribe_trade_updates(_handler)
        await stream._run_forever()
