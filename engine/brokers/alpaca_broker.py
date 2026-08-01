import datetime as dt

import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame as AlpacaTimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide as AlpacaOrderSide
from alpaca.trading.enums import TimeInForce
from alpaca.trading.requests import MarketOrderRequest

from db.models import SignalAction
from engine.brokers.base import AccountSnapshot, ClockSnapshot, OrderResult, PositionSnapshot, Timeframe
from engine.config import Config

_TIMEFRAME_MAP = {
    Timeframe.MINUTE: AlpacaTimeFrame.Minute,
    Timeframe.DAY: AlpacaTimeFrame.Day,
}


class AlpacaBroker:
    """BrokerClient implementation backed by alpaca-py. All Alpaca SDK usage is
    confined to this file - everything else in the app talks to BrokerClient."""

    def __init__(self, config: Config):
        self._trading = TradingClient(config.alpaca_api_key, config.alpaca_secret_key, paper=config.alpaca_paper)
        self._data = StockHistoricalDataClient(config.alpaca_api_key, config.alpaca_secret_key)

    def get_account(self) -> AccountSnapshot:
        account = self._trading.get_account()
        return AccountSnapshot(equity=float(account.equity), cash=float(account.cash), buying_power=float(account.buying_power))

    def get_clock(self) -> ClockSnapshot:
        return ClockSnapshot(is_open=self._trading.get_clock().is_open)

    def get_bars(
        self, symbol: str, timeframe: Timeframe, limit: int | None = None, start: dt.datetime | None = None
    ) -> pd.DataFrame:
        request = StockBarsRequest(symbol_or_symbols=symbol, timeframe=_TIMEFRAME_MAP[timeframe], limit=limit, start=start)
        df = self._data.get_stock_bars(request).df
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(symbol, level=0)
        return df.sort_index()[["open", "high", "low", "close", "volume"]]

    def get_position_qty(self, symbol: str) -> float:
        try:
            return abs(float(self._trading.get_open_position(symbol).qty))
        except Exception:
            return 0.0

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
