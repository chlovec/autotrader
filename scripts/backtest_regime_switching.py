"""Backtests the actual RegimeSwitchingStrategy from engine/strategy.py by calling its
generate_signal() at each bar, rather than reimplementing the ADX regime logic a third
time in backtesting.py's own indicator idioms - the composite is complex enough that a
from-scratch reimplementation would risk silently diverging from what the live runner
actually does.

Usage (from project root): python -m scripts.backtest_regime_switching
Requires ALPACA_API_KEY / ALPACA_SECRET_KEY in .env (market data access works with paper keys).
"""

import pandas as pd
from backtesting import Backtest
from backtesting import Strategy as BacktestingStrategy

from db.models import SignalAction
from engine.strategy import RegimeSwitchingStrategy
from scripts.common import fetch_daily_bars

SYMBOL = "SPY"
YEARS_OF_HISTORY = 12  # Alpaca's free data actually starts 2016-01, so this captures everything available


class RegimeSwitchingAdapter(BacktestingStrategy):
    adx_period = 14
    adx_trend_threshold = 25

    def init(self) -> None:
        self.strategy = RegimeSwitchingStrategy(adx_period=self.adx_period, adx_trend_threshold=self.adx_trend_threshold)

    def next(self) -> None:
        bars = pd.DataFrame(
            {
                "open": self.data.Open,
                "high": self.data.High,
                "low": self.data.Low,
                "close": self.data.Close,
                "volume": self.data.Volume,
            },
            index=self.data.index,
        )
        action, _ = self.strategy.generate_signal(SYMBOL, bars)
        if action == SignalAction.buy and not self.position:
            self.buy()
        elif action == SignalAction.sell and self.position:
            self.position.close()


def main() -> None:
    data = fetch_daily_bars(SYMBOL, YEARS_OF_HISTORY)
    # commission=0.0: Alpaca charges no commission on stock trades; slippage is still
    # implicitly ignored here, so treat these results as optimistic vs. live fills.
    bt = Backtest(data, RegimeSwitchingAdapter, cash=10_000, commission=0.0)
    stats = bt.run()
    print(stats)
    bt.plot(filename="backtest_result.html", open_browser=False)


if __name__ == "__main__":
    main()
