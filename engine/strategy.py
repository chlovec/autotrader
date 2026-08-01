from abc import ABC, abstractmethod

import pandas as pd

from db.models import SignalAction


class Strategy(ABC):
    """Interface every concrete strategy implements."""

    name: str

    @abstractmethod
    def generate_signal(self, symbol: str, bars: pd.DataFrame) -> tuple[SignalAction, str]:
        """Given recent OHLCV bars for a symbol (ascending by time, 'close' column required),
        return (action, reason)."""
        raise NotImplementedError


class MovingAverageCrossoverStrategy(Strategy):
    """Buy on a golden cross (short SMA crosses above long SMA), sell on a death cross.

    Classic trend-following strategy. Defaults (20/50) are a common moderate-speed pairing
    for daily bars on liquid ETFs - fast enough to catch trends, slow enough to avoid
    whipsawing on every few days of noise.
    """

    name = "ma_crossover"

    def __init__(self, short_window: int = 20, long_window: int = 50):
        if short_window >= long_window:
            raise ValueError("short_window must be less than long_window")
        self.short_window = short_window
        self.long_window = long_window

    def generate_signal(self, symbol: str, bars: pd.DataFrame) -> tuple[SignalAction, str]:
        if len(bars) < self.long_window + 1:
            return SignalAction.hold, f"not enough history ({len(bars)}/{self.long_window + 1} bars)"

        short_sma = bars["close"].rolling(self.short_window).mean()
        long_sma = bars["close"].rolling(self.long_window).mean()

        prev_short, prev_long = short_sma.iloc[-2], long_sma.iloc[-2]
        curr_short, curr_long = short_sma.iloc[-1], long_sma.iloc[-1]

        crossed_up = prev_short <= prev_long and curr_short > curr_long
        crossed_down = prev_short >= prev_long and curr_short < curr_long

        if crossed_up:
            return SignalAction.buy, f"golden cross: SMA{self.short_window}={curr_short:.2f} > SMA{self.long_window}={curr_long:.2f}"
        if crossed_down:
            return SignalAction.sell, f"death cross: SMA{self.short_window}={curr_short:.2f} < SMA{self.long_window}={curr_long:.2f}"
        return SignalAction.hold, f"no cross: SMA{self.short_window}={curr_short:.2f}, SMA{self.long_window}={curr_long:.2f}"
