import datetime as dt

import pandas as pd

from engine.brokers.alpaca_broker import AlpacaBroker, _default_start
from engine.brokers.base import Timeframe
from engine.config import Config


def _config() -> Config:
    return Config(
        broker="alpaca",
        alpaca_api_key="key",
        alpaca_secret_key="secret",
        alpaca_base_url="https://paper-api.alpaca.markets",
        alpaca_paper=True,
        ibkr_host="127.0.0.1",
        ibkr_port=7497,
        ibkr_client_id=1,
        questrade_refresh_token="token",
        max_position_size_usd=1000.0,
        max_daily_loss_usd=200.0,
        smtp_host="",
        smtp_port=587,
        smtp_username="",
        smtp_password="",
        alert_email_from="",
        alert_email_to="",
    )


def test_default_start_day_timeframe_covers_limit_trading_days():
    # 100 trading days needs ~140 calendar days accounting for weekends, plus a holiday buffer
    start = _default_start(Timeframe.DAY, 100)
    assert (dt.datetime.utcnow() - start).days >= 150


def test_default_start_is_close_to_now_for_small_limits():
    start = _default_start(Timeframe.DAY, 1)
    assert 10 <= (dt.datetime.utcnow() - start).days <= 25


class _FakeBarsResponse:
    def __init__(self, df: pd.DataFrame):
        self.df = df


def _bars_df(n: int) -> pd.DataFrame:
    index = pd.date_range("2026-01-01", periods=n, freq="D", name="timestamp")
    return pd.DataFrame(
        {"open": range(n), "high": range(n), "low": range(n), "close": range(n), "volume": range(n)}, index=index
    )


def test_get_bars_without_start_ignores_alpacas_limit_and_takes_the_tail(monkeypatch):
    """Regression test: Alpaca's API defaults `start` to ~today when omitted, which
    returns zero bars whenever today's bar isn't published yet (e.g. a weekend) - and its
    default `sort` is ascending, so passing `limit` alongside a synthesized `start` would
    return the *oldest* bars in the window, not the most recent ones. get_bars must fetch
    unbounded and take the tail itself instead of trusting Alpaca's `limit`."""
    broker = AlpacaBroker(_config())
    captured_requests = []

    def fake_get_stock_bars(request):
        captured_requests.append(request)
        return _FakeBarsResponse(_bars_df(10))

    monkeypatch.setattr(broker._data, "get_stock_bars", fake_get_stock_bars)

    bars = broker.get_bars("AAPL", Timeframe.DAY, limit=3)

    assert captured_requests[0].limit is None
    assert captured_requests[0].start is not None
    assert bars["close"].tolist() == [7, 8, 9]  # the most recent 3 of the 10 fake rows


def test_get_bars_with_explicit_start_passes_limit_through_unchanged(monkeypatch):
    """scripts/common.py calls get_bars with an explicit start (backtests) - that path's
    original behavior (limit passed straight through to Alpaca) must be untouched."""
    broker = AlpacaBroker(_config())
    captured_requests = []

    def fake_get_stock_bars(request):
        captured_requests.append(request)
        return _FakeBarsResponse(_bars_df(5))

    monkeypatch.setattr(broker._data, "get_stock_bars", fake_get_stock_bars)

    explicit_start = dt.datetime(2026, 1, 1)
    broker.get_bars("AAPL", Timeframe.DAY, limit=2, start=explicit_start)

    assert captured_requests[0].start == explicit_start
    assert captured_requests[0].limit == 2
