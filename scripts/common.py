import datetime as dt

import pandas as pd

from engine.brokers import make_broker
from engine.brokers.base import Timeframe
from engine.config import load_account_credentials, load_account_ids


def _backtest_broker():
    """Backtests aren't account-scoped - they just need *a* live broker connection for
    historical bars, same as engine/research_runner.py. Uses the first id in ACCOUNT_IDS."""
    account_ids = load_account_ids()
    if not account_ids:
        raise RuntimeError("ACCOUNT_IDS is empty - backtests need at least one account configured in .env for market data")
    account_id = account_ids[0]
    return make_broker(account_id, load_account_credentials(account_id))


def fetch_daily_bars(symbol: str, years: int) -> pd.DataFrame:
    """Historical daily OHLCV for backtesting, in backtesting.py's expected column casing."""
    broker = _backtest_broker()
    start = dt.datetime.now() - dt.timedelta(days=365 * years)
    df = broker.get_bars(symbol, Timeframe.DAY, start=start)
    df = df.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"})
    return df[["Open", "High", "Low", "Close", "Volume"]]
