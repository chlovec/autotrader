"""Sweeps ADX period / trend-threshold combinations for RegimeSwitchingStrategy against
the same historical data used in backtest_regime_switching.py, matching the rigor already
applied to the MA crossover windows before trusting the 14/25 defaults.

Usage (from project root): python -m scripts.optimize_regime_switching
"""

import pandas as pd
from backtesting import Backtest

from scripts.backtest_regime_switching import SYMBOL, YEARS_OF_HISTORY, RegimeSwitchingAdapter
from scripts.common import fetch_daily_bars

ADX_PERIODS = [7, 14, 21]
ADX_TREND_THRESHOLDS = [20, 25, 30]


def main() -> None:
    data = fetch_daily_bars(SYMBOL, YEARS_OF_HISTORY)
    bt = Backtest(data, RegimeSwitchingAdapter, cash=10_000, commission=0.0)

    rows = []
    for adx_period in ADX_PERIODS:
        for adx_trend_threshold in ADX_TREND_THRESHOLDS:
            stats = bt.run(adx_period=adx_period, adx_trend_threshold=adx_trend_threshold)
            rows.append(
                {
                    "adx_period": adx_period,
                    "adx_trend_threshold": adx_trend_threshold,
                    "return_pct": stats["Return [%]"],
                    "buy_hold_pct": stats["Buy & Hold Return [%]"],
                    "sharpe": stats["Sharpe Ratio"],
                    "max_dd_pct": stats["Max. Drawdown [%]"],
                    "trades": stats["# Trades"],
                    "win_rate_pct": stats["Win Rate [%]"],
                }
            )

    print(pd.DataFrame(rows).sort_values("sharpe", ascending=False).to_string(index=False))


if __name__ == "__main__":
    main()
