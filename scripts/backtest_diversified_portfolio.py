"""Tests whether simple diversification alone - no market timing, no signals, just an
equal-weight portfolio rebalanced periodically - improves on buy-and-hold SPY's
risk-adjusted return. This is the benchmark to beat before building any timing logic
on top of a multi-asset basket: if plain rebalancing already helps, that's the
well-evidenced effect to lean on rather than more technical-indicator tuning.

Usage (from project root): python -m scripts.backtest_diversified_portfolio
Requires ALPACA_API_KEY / ALPACA_SECRET_KEY in .env (market data access works with paper keys).
"""

import numpy as np
import pandas as pd

from scripts.common import fetch_daily_bars

SYMBOLS = ["SPY", "TLT", "GLD"]  # equities, long-term treasuries, gold - historically low correlation
YEARS_OF_HISTORY = 12  # Alpaca's free data actually starts 2016-01, so this captures everything available
REBALANCE_FREQUENCY = "ME"  # rebalance back to equal weight at each month end


def build_portfolio_equity(closes: pd.DataFrame, rebalance_freq: str) -> pd.Series:
    daily_returns = closes.pct_change().dropna()
    n_assets = len(closes.columns)
    weights = pd.Series(1 / n_assets, index=closes.columns)

    rebalance_dates = set(daily_returns.resample(rebalance_freq).last().index)

    equity = [1.0]
    holdings = weights.copy()  # fraction of current equity held in each asset
    for date, day_returns in daily_returns.iterrows():
        holdings = holdings * (1 + day_returns)
        equity.append(equity[-1] * holdings.sum())
        holdings = holdings / holdings.sum()  # renormalize to fractions of new equity
        if date in rebalance_dates:
            holdings = weights.copy()

    return pd.Series(equity[1:], index=daily_returns.index)


def sharpe_and_drawdown(equity: pd.Series) -> tuple[float, float]:
    daily_returns = equity.pct_change().dropna()
    sharpe = daily_returns.mean() / daily_returns.std() * np.sqrt(252)
    running_max = equity.cummax()
    max_dd = ((equity - running_max) / running_max).min()
    return sharpe, max_dd


def main() -> None:
    closes = pd.DataFrame({symbol: fetch_daily_bars(symbol, YEARS_OF_HISTORY)["Close"] for symbol in SYMBOLS}).dropna()

    portfolio_equity = build_portfolio_equity(closes, REBALANCE_FREQUENCY)
    portfolio_sharpe, portfolio_dd = sharpe_and_drawdown(portfolio_equity)
    portfolio_return = (portfolio_equity.iloc[-1] - 1) * 100

    spy_equity = closes["SPY"] / closes["SPY"].iloc[0]
    spy_sharpe, spy_dd = sharpe_and_drawdown(spy_equity)
    spy_return = (spy_equity.iloc[-1] - 1) * 100

    print(f"{'':30s} {'Return':>10s} {'Sharpe':>8s} {'Max DD':>8s}")
    print(f"{'Buy & Hold SPY':30s} {spy_return:9.2f}% {spy_sharpe:8.3f} {spy_dd * 100:7.2f}%")
    print(f"{'Equal-weight SPY/TLT/GLD':30s} {portfolio_return:9.2f}% {portfolio_sharpe:8.3f} {portfolio_dd * 100:7.2f}%")


if __name__ == "__main__":
    main()
