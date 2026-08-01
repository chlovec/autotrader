import datetime as dt
import json
from pathlib import Path

import pandas as pd
import requests

from db.models import SignalAction
from engine.brokers.base import AccountSnapshot, ClockSnapshot, OrderResult, PositionSnapshot, Timeframe
from engine.config import Config

_AUTH_URL = "https://login.questrade.com/oauth2/token"
_TOKEN_CACHE_PATH = Path("questrade_token.json")

_INTERVAL = {
    Timeframe.MINUTE: "OneMinute",
    Timeframe.DAY: "OneDay",
}

_US_MARKETS = ("NASDAQ", "NYSE")


class QuestradeBroker:
    """BrokerClient implementation backed by Questrade's REST API via `requests` -
    Questrade has no official Python SDK.

    Auth is an OAuth2 refresh token obtained manually from
    https://login.questrade.com/APIAccess/UserApps.aspx (use the practice portal
    instead for a simulated/practice account - same API, separate token). Questrade
    refresh tokens are single-use: every refresh returns a *new* refresh token and
    invalidates the old one. QUESTRADE_REFRESH_TOKEN in .env only has to be valid
    for the first run - after that, the current token lives in
    `questrade_token.json` (gitignored), which takes precedence once it exists.

    Untested against a live account (none was available while writing this) -
    endpoint paths and response field names are from Questrade's public API
    documentation, not verified against a real response. Confirm field names
    (particularly `openPnl`, `combinedBalances`) against a real account before
    trusting this with money.
    """

    def __init__(self, config: Config):
        self._config = config
        self._access_token: str | None = None
        self._api_server: str | None = None
        self._account_id: str | None = None
        self._symbol_id_cache: dict[str, int] = {}

    def _current_refresh_token(self) -> str:
        if _TOKEN_CACHE_PATH.exists():
            return json.loads(_TOKEN_CACHE_PATH.read_text())["refresh_token"]
        return self._config.questrade_refresh_token

    def _authenticate(self) -> None:
        response = requests.get(
            _AUTH_URL,
            params={"grant_type": "refresh_token", "refresh_token": self._current_refresh_token()},
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
        self._access_token = data["access_token"]
        self._api_server = data["api_server"].rstrip("/")
        _TOKEN_CACHE_PATH.write_text(json.dumps({"refresh_token": data["refresh_token"]}))

    def _request(self, method: str, path: str, **kwargs) -> dict:
        if self._access_token is None:
            self._authenticate()
        url = f"{self._api_server}{path}"
        response = requests.request(method, url, headers={"Authorization": f"Bearer {self._access_token}"}, timeout=10, **kwargs)
        if response.status_code == 401:
            self._authenticate()
            url = f"{self._api_server}{path}"
            response = requests.request(method, url, headers={"Authorization": f"Bearer {self._access_token}"}, timeout=10, **kwargs)
        response.raise_for_status()
        return response.json()

    def _get_account_id(self) -> str:
        if self._account_id is None:
            accounts = self._request("GET", "/v1/accounts")["accounts"]
            self._account_id = accounts[0]["number"]
        return self._account_id

    def _get_symbol_id(self, symbol: str) -> int:
        if symbol not in self._symbol_id_cache:
            data = self._request("GET", "/v1/symbols", params={"names": symbol})
            self._symbol_id_cache[symbol] = data["symbols"][0]["symbolId"]
        return self._symbol_id_cache[symbol]

    def get_account(self) -> AccountSnapshot:
        account_id = self._get_account_id()
        balances = self._request("GET", f"/v1/accounts/{account_id}/balances")
        combined = next(b for b in balances["combinedBalances"] if b["currency"] == "USD")
        return AccountSnapshot(
            equity=float(combined["totalEquity"]),
            cash=float(combined["cash"]),
            buying_power=float(combined["buyingPower"]),
        )

    def get_clock(self) -> ClockSnapshot:
        markets = self._request("GET", "/v1/markets")["markets"]
        market = next((m for m in markets if m["name"] in _US_MARKETS), None)
        if not market:
            return ClockSnapshot(is_open=False)
        now = dt.datetime.now(dt.timezone.utc)
        start = dt.datetime.fromisoformat(market["startTime"])
        end = dt.datetime.fromisoformat(market["endTime"])
        return ClockSnapshot(is_open=start <= now <= end)

    def get_bars(self, symbol: str, timeframe: Timeframe, limit: int | None = None, start: dt.datetime | None = None) -> pd.DataFrame:
        symbol_id = self._get_symbol_id(symbol)
        end_time = dt.datetime.now(dt.timezone.utc)
        if start is None:
            days = (limit or 100) if timeframe == Timeframe.DAY else 1
            start = end_time - dt.timedelta(days=days)
        data = self._request(
            "GET",
            f"/v1/markets/candles/{symbol_id}",
            params={"startTime": start.isoformat(), "endTime": end_time.isoformat(), "interval": _INTERVAL[timeframe]},
        )
        candles = data["candles"]
        df = pd.DataFrame(
            {
                "open": [c["open"] for c in candles],
                "high": [c["high"] for c in candles],
                "low": [c["low"] for c in candles],
                "close": [c["close"] for c in candles],
                "volume": [c["volume"] for c in candles],
            },
            index=pd.to_datetime([c["start"] for c in candles]),
        )
        return df.sort_index()

    def get_position_qty(self, symbol: str) -> float:
        for position in self.get_positions():
            if position.symbol == symbol:
                return abs(position.qty)
        return 0.0

    def get_positions(self) -> list[PositionSnapshot]:
        account_id = self._get_account_id()
        positions = self._request("GET", f"/v1/accounts/{account_id}/positions")["positions"]
        return [
            PositionSnapshot(
                symbol=p["symbol"],
                qty=float(p["openQuantity"]),
                avg_entry_price=float(p["averageEntryPrice"]),
                market_value=float(p["currentMarketValue"]),
                unrealized_pl=float(p["openPnl"]),
            )
            for p in positions
        ]

    def submit_market_order(self, symbol: str, action: SignalAction, qty: float) -> OrderResult:
        account_id = self._get_account_id()
        symbol_id = self._get_symbol_id(symbol)
        body = {
            "symbolId": symbol_id,
            "quantity": qty,
            "orderType": "Market",
            "timeInForce": "Day",
            "action": "Buy" if action == SignalAction.buy else "Sell",
        }
        data = self._request("POST", f"/v1/accounts/{account_id}/orders", json=body)
        order = data["orders"][0]
        return OrderResult(broker_order_id=str(order["id"]), status=order.get("state", "submitted"))
