import datetime as dt
from typing import Awaitable, Callable

import pandas as pd
from ib_async import IB, MarketOrder, Stock

from db.models import OrderSide, SignalAction
from engine.brokers.base import AccountSnapshot, BrokerOrder, ClockSnapshot, OrderResult, PositionSnapshot, Timeframe
from engine.config import Config


def _as_utc(moment: dt.datetime) -> dt.datetime:
    """ib_async's own timestamps (Fill.time, TradeLogEntry.time) are timezone-aware UTC;
    `since` arrives from the backend's reconciliation routine as a naive UTC datetime
    (dt.datetime.utcnow()) - normalize so comparisons between the two don't raise."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=dt.timezone.utc)

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

    name = "ibkr"

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
        managed_accounts = ib.managedAccounts()
        return AccountSnapshot(
            equity=float(values.get("NetLiquidation", 0.0)),
            cash=float(values.get("TotalCashValue", 0.0)),
            buying_power=float(values.get("BuyingPower", 0.0)),
            account_id=managed_accounts[0] if managed_accounts else "",
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

    def get_recent_orders(self, since: dt.datetime) -> list[BrokerOrder]:
        """Combines still-open orders (reqAllOpenOrders - includes orders placed by any
        client on the account, e.g. someone trading directly in TWS) with completed fills
        (fills - cached, no request-and-wait), keyed by IB's orderId so a fill overwrites
        the corresponding still-open entry with its final fill_price/filled_at."""
        ib = self._ensure_connected()
        since = _as_utc(since)
        orders_by_id: dict[int, BrokerOrder] = {}

        for trade in ib.reqAllOpenOrders():
            submitted_at = _as_utc(trade.log[0].time) if trade.log else dt.datetime.now(dt.timezone.utc)
            if submitted_at < since:
                continue
            orders_by_id[trade.order.orderId] = BrokerOrder(
                broker_order_id=str(trade.order.orderId),
                symbol=trade.contract.symbol,
                side=OrderSide.buy if trade.order.action == "BUY" else OrderSide.sell,
                qty=float(trade.order.totalQuantity),
                status=trade.orderStatus.status,
                fill_price=trade.orderStatus.avgFillPrice or None,
                submitted_at=submitted_at,
                filled_at=None,
            )

        for fill in ib.fills():
            filled_at = _as_utc(fill.time)
            if filled_at < since:
                continue
            order_id = fill.execution.orderId
            existing = orders_by_id.get(order_id)
            orders_by_id[order_id] = BrokerOrder(
                broker_order_id=str(order_id),
                symbol=fill.contract.symbol,
                side=OrderSide.buy if fill.execution.side == "BOT" else OrderSide.sell,
                qty=float(fill.execution.shares),
                status="filled",
                fill_price=float(fill.execution.avgPrice or fill.execution.price),
                submitted_at=existing.submitted_at if existing else filled_at,
                filled_at=filled_at,
            )

        return list(orders_by_id.values())

    async def stream(self, on_change: Callable[[], Awaitable[None]]) -> None:
        """ib_async is asyncio-native and already maintains a persistent socket to
        TWS/Gateway - this registers on_change against the account/order callbacks it
        already fires, rather than opening any new connection. Payloads are never
        inspected here; the backend re-fetches full state centrally on every signal (see
        backend/app/broker_stream.py)."""
        ib = self._ib
        if not ib.isConnected():
            await ib.connectAsync(
                self._config.ibkr_host,
                self._config.ibkr_port,
                clientId=self._config.ibkr_client_id,
                timeout=10,
            )

        async def _handler(*_args: object) -> None:
            await on_change()

        ib.orderStatusEvent += _handler
        ib.execDetailsEvent += _handler
        ib.positionEvent += _handler
        ib.accountValueEvent += _handler
        try:
            await ib.disconnectedEvent
        finally:
            ib.orderStatusEvent -= _handler
            ib.execDetailsEvent -= _handler
            ib.positionEvent -= _handler
            ib.accountValueEvent -= _handler
