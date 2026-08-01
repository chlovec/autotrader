import datetime as dt

import pandas as pd
from ib_async import IB, MarketOrder, Stock

from db.models import SignalAction
from engine.brokers.base import AccountSnapshot, ClockSnapshot, OrderResult, PositionSnapshot, Timeframe
from engine.config import Config

_BAR_SIZE = {
    Timeframe.MINUTE: "1 min",
    Timeframe.DAY: "1 day",
}


def _parse_trading_hours(trading_hours: str, now: dt.datetime) -> bool:
    """Parses IB's ContractDetails.tradingHours format: a semicolon-separated list
    of "YYYYMMDD:HHMM-HHMM" (or "YYYYMMDD:CLOSED") segments, one per upcoming day,
    in the contract's local timezone. `now` must already be in that timezone.

    NOTE: built from IB's documented format, not verified against a live TWS
    connection - the exact segment format has historically varied by contract type,
    so treat this as a starting point to confirm once real API access exists.
    """
    today = now.strftime("%Y%m%d")
    for segment in trading_hours.split(";"):
        if not segment.startswith(today + ":"):
            continue
        _, hours = segment.split(":", 1)
        if hours == "CLOSED":
            return False
        open_str, close_str = hours.split("-")
        open_time = dt.datetime.strptime(open_str, "%H%M").time()
        close_time = dt.datetime.strptime(close_str, "%H%M").time()
        return open_time <= now.time() <= close_time
    return False


def _duration_string(timeframe: Timeframe, limit: int | None, start: dt.datetime | None, now: dt.datetime) -> str:
    """Builds the durationStr IB's reqHistoricalData expects (e.g. "30 D", "3600 S")."""
    if start is not None:
        days = max((now - start).days, 1)
        return f"{days} D"
    count = limit or 100
    if timeframe == Timeframe.DAY:
        return f"{count} D"
    return f"{count * 60} S"  # minute bars: ask for `count` minutes worth of seconds


class IBKRBroker:
    """BrokerClient implementation backed by Interactive Brokers via ib_async.

    Requires Trader Workstation (TWS) or IB Gateway running locally with API access
    enabled (File > Global Configuration > API > Settings > "Enable ActiveX and
    Socket Clients", and "Read-Only API" unchecked so orders can be submitted).
    There's no API key/secret - authentication happens by logging into the running
    TWS/Gateway instance yourself; this class just connects to its local socket.

    Untested against a live connection (no TWS/Gateway instance was available while
    writing this) - method and field names were verified against the installed
    ib_async package's actual signatures, but behavior against a real IB account
    needs confirming.
    """

    def __init__(self, config: Config):
        self._config = config
        self._ib = IB()

    def _ensure_connected(self) -> IB:
        if not self._ib.isConnected():
            self._ib.connect(
                self._config.ibkr_host,
                self._config.ibkr_port,
                clientId=self._config.ibkr_client_id,
                timeout=10,
            )
        return self._ib

    def _contract(self, symbol: str) -> Stock:
        return Stock(symbol, "SMART", "USD")

    def get_account(self) -> AccountSnapshot:
        ib = self._ensure_connected()
        values = {v.tag: v.value for v in ib.accountSummary() if v.currency in ("USD", "BASE")}
        return AccountSnapshot(
            equity=float(values.get("NetLiquidation", 0.0)),
            cash=float(values.get("TotalCashValue", 0.0)),
            buying_power=float(values.get("BuyingPower", 0.0)),
        )

    def get_clock(self) -> ClockSnapshot:
        ib = self._ensure_connected()
        details = ib.reqContractDetails(self._contract("SPY"))
        if not details:
            return ClockSnapshot(is_open=False)
        return ClockSnapshot(is_open=_parse_trading_hours(details[0].tradingHours, dt.datetime.now()))

    def get_bars(self, symbol: str, timeframe: Timeframe, limit: int | None = None, start: dt.datetime | None = None) -> pd.DataFrame:
        ib = self._ensure_connected()
        duration = _duration_string(timeframe, limit, start, dt.datetime.now())
        bars = ib.reqHistoricalData(
            self._contract(symbol),
            endDateTime="",
            durationStr=duration,
            barSizeSetting=_BAR_SIZE[timeframe],
            whatToShow="TRADES",
            useRTH=True,
            formatDate=1,
        )
        df = pd.DataFrame(
            {
                "open": [b.open for b in bars],
                "high": [b.high for b in bars],
                "low": [b.low for b in bars],
                "close": [b.close for b in bars],
                "volume": [b.volume for b in bars],
            },
            index=pd.to_datetime([b.date for b in bars]),
        )
        return df.sort_index()

    def get_position_qty(self, symbol: str) -> float:
        ib = self._ensure_connected()
        for position in ib.positions():
            if position.contract.symbol == symbol:
                return abs(float(position.position))
        return 0.0

    def get_positions(self) -> list[PositionSnapshot]:
        ib = self._ensure_connected()
        return [
            PositionSnapshot(
                symbol=item.contract.symbol,
                qty=float(item.position),
                avg_entry_price=float(item.averageCost),
                market_value=float(item.marketValue),
                unrealized_pl=float(item.unrealizedPNL),
            )
            for item in ib.portfolio()
        ]

    def submit_market_order(self, symbol: str, action: SignalAction, qty: float) -> OrderResult:
        ib = self._ensure_connected()
        order = MarketOrder("BUY" if action == SignalAction.buy else "SELL", qty)
        trade = ib.placeOrder(self._contract(symbol), order)
        ib.sleep(1)  # pump the event loop so the order's initial ack has arrived
        return OrderResult(broker_order_id=str(trade.order.orderId), status=trade.orderStatus.status or "submitted")
