"""Backtests the same long/flat RSI mean-reversion logic used live in engine/strategy.py.

Mirrors the live rules exactly (buy crossing into oversold, flatten crossing into
overbought, no shorting) so the backtest result is actually predictive of what the
runner would have done.

Usage (from project root): python -m scripts.backtest_mean_reversion
Requires ALPACA_API_KEY / ALPACA_SECRET_KEY in .env (market data access works with paper keys).
"""

import pandas as pd
from backtesting import Backtest, Strategy

from engine.strategy import _rsi
from scripts.common import fetch_daily_bars

SYMBOL = "SPY"
RSI_PERIOD = 14
OVERSOLD = 30
OVERBOUGHT = 70
YEARS_OF_HISTORY = 12  # Alpaca's free data actually starts 2016-01, so this captures everything available


class MeanReversion(Strategy):
    period = RSI_PERIOD
    oversold = OVERSOLD
    overbought = OVERBOUGHT

    def init(self) -> None:
        self.rsi = self.I(_rsi, pd.Series(self.data.Close), self.period)

    def next(self) -> None:
        if len(self.rsi) < 2:
            return
        prev_rsi, curr_rsi = self.rsi[-2], self.rsi[-1]
        # Guard against pyramiding into a second position on a repeat oversold cross
        # while still holding one - matches the live RiskManager, which rejects a new
        # buy once existing exposure plus the fixed order size would exceed the cap.
        if not self.position and prev_rsi >= self.oversold and curr_rsi < self.oversold:
            self.buy()
        elif self.position and prev_rsi <= self.overbought and curr_rsi > self.overbought:
            self.position.close()


def main() -> None:
    data = fetch_daily_bars(SYMBOL, YEARS_OF_HISTORY)
    # commission=0.0: Alpaca charges no commission on stock trades; slippage is still
    # implicitly ignored here, so treat these results as optimistic vs. live fills.
    bt = Backtest(data, MeanReversion, cash=10_000, commission=0.0)
    stats = bt.run()
    print(stats)
    bt.plot(filename="backtest_result.html", open_browser=False)


if __name__ == "__main__":
    main()
