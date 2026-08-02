import datetime as dt

import pandas as pd

from engine.brokers import make_broker
from engine.brokers.base import Timeframe
from engine.config import load_config


def fetch_daily_bars(symbol: str, years: int) -> pd.DataFrame:
    """Historical daily OHLCV for backtesting, in backtesting.py's expected column casing."""
    broker = make_broker(load_config(argv=[]))
    start = dt.datetime.now() - dt.timedelta(days=365 * years)
    df = broker.get_bars(symbol, Timeframe.DAY, start=start)
    df = df.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"})
    return df[["Open", "High", "Low", "Close", "Volume"]]
