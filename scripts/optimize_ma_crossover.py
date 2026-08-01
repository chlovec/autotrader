"""Sweeps MA crossover window combinations against the same historical data used in
backtest_ma_crossover.py, to check whether 20/50 was just a bad parameter choice
before concluding the strategy type itself doesn't work here.

Usage (from project root): python -m scripts.optimize_ma_crossover
"""

import pandas as pd
from backtesting import Backtest

from scripts.backtest_ma_crossover import SYMBOL, YEARS_OF_HISTORY, MaCross
from scripts.common import fetch_daily_bars

WINDOW_COMBOS = [(5, 20), (10, 30), (10, 50), (20, 50), (20, 100), (50, 200)]


def main() -> None:
    data = fetch_daily_bars(SYMBOL, YEARS_OF_HISTORY)
    bt = Backtest(data, MaCross, cash=10_000, commission=0.0)

    rows = []
    for short, long in WINDOW_COMBOS:
        stats = bt.run(n1=short, n2=long)
        rows.append(
            {
                "short": short,
                "long": long,
                "return_pct": stats["Return [%]"],
                "buy_hold_pct": stats["Buy & Hold Return [%]"],
                "sharpe": stats["Sharpe Ratio"],
                "max_dd_pct": stats["Max. Drawdown [%]"],
                "trades": stats["# Trades"],
                "win_rate_pct": stats["Win Rate [%]"],
            }
        )

    print(pd.DataFrame(rows).sort_values("return_pct", ascending=False).to_string(index=False))


if __name__ == "__main__":
    main()
