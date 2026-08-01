"""Backtests the same long/flat MA-crossover logic used live in engine/strategy.py.

Mirrors the live rules exactly (buy on golden cross, flatten on death cross, no shorting)
so the backtest result is actually predictive of what the runner would have done.

Usage: python scripts/backtest_ma_crossover.py
Requires ALPACA_API_KEY / ALPACA_SECRET_KEY in .env (market data access works with paper keys).
"""

import datetime as dt

import pandas as pd
from backtesting import Backtest, Strategy
from backtesting.lib import crossover

from engine.brokers import make_broker
from engine.brokers.base import Timeframe
from engine.config import load_config

SYMBOL = "SPY"
SHORT_WINDOW = 20
LONG_WINDOW = 50
YEARS_OF_HISTORY = 3


def _sma(values, window: int) -> pd.Series:
    return pd.Series(values).rolling(window).mean()


class MaCross(Strategy):
    n1 = SHORT_WINDOW
    n2 = LONG_WINDOW

    def init(self) -> None:
        self.sma1 = self.I(_sma, self.data.Close, self.n1)
        self.sma2 = self.I(_sma, self.data.Close, self.n2)

    def next(self) -> None:
        if crossover(self.sma1, self.sma2):
            self.buy()
        elif crossover(self.sma2, self.sma1):
            self.position.close()


def fetch_daily_bars(symbol: str, years: int) -> pd.DataFrame:
    broker = make_broker(load_config())
    start = dt.datetime.now() - dt.timedelta(days=365 * years)
    df = broker.get_bars(symbol, Timeframe.DAY, start=start)
    df = df.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"})
    return df[["Open", "High", "Low", "Close", "Volume"]]


def main() -> None:
    data = fetch_daily_bars(SYMBOL, YEARS_OF_HISTORY)
    # commission=0.0: Alpaca charges no commission on stock trades; slippage is still
    # implicitly ignored here, so treat these results as optimistic vs. live fills.
    bt = Backtest(data, MaCross, cash=10_000, commission=0.0)
    stats = bt.run()
    print(stats)
    bt.plot(filename="backtest_result.html", open_browser=False)


if __name__ == "__main__":
    main()
