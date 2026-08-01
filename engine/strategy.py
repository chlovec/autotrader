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


def _rsi(closes: pd.Series, period: int) -> pd.Series:
    """Wilder's RSI: relative strength of average gains vs. average losses over `period` bars."""
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


class MeanReversionStrategy(Strategy):
    """Buy when RSI crosses down into oversold territory, sell (flatten) when it crosses
    back up into overbought territory.

    Cross-based rather than level-based (like the crossover strategy above) so a signal
    fires once at the threshold crossing instead of every bar the RSI happens to sit past
    30 or 70.
    """

    name = "mean_reversion"

    def __init__(self, period: int = 14, oversold: float = 30, overbought: float = 70):
        if oversold >= overbought:
            raise ValueError("oversold must be less than overbought")
        self.period = period
        self.oversold = oversold
        self.overbought = overbought

    def generate_signal(self, symbol: str, bars: pd.DataFrame) -> tuple[SignalAction, str]:
        min_bars = self.period + 2
        if len(bars) < min_bars:
            return SignalAction.hold, f"not enough history ({len(bars)}/{min_bars} bars)"

        rsi = _rsi(bars["close"], self.period)
        prev_rsi, curr_rsi = rsi.iloc[-2], rsi.iloc[-1]

        crossed_into_oversold = prev_rsi >= self.oversold and curr_rsi < self.oversold
        crossed_into_overbought = prev_rsi <= self.overbought and curr_rsi > self.overbought

        if crossed_into_oversold:
            return SignalAction.buy, f"RSI{self.period} crossed below {self.oversold} (oversold): {curr_rsi:.1f}"
        if crossed_into_overbought:
            return SignalAction.sell, f"RSI{self.period} crossed above {self.overbought} (overbought): {curr_rsi:.1f}"
        return SignalAction.hold, f"RSI{self.period}={curr_rsi:.1f}, no threshold cross"


def _adx(bars: pd.DataFrame, period: int) -> pd.Series:
    """Wilder's ADX: trend strength regardless of direction. Requires high/low/close columns."""
    high, low, close = bars["high"], bars["low"], bars["close"]
    prev_close, prev_high, prev_low = close.shift(1), high.shift(1), low.shift(1)

    true_range = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)

    up_move = high - prev_high
    down_move = prev_low - low
    plus_dm = ((up_move > down_move) & (up_move > 0)) * up_move.clip(lower=0)
    minus_dm = ((down_move > up_move) & (down_move > 0)) * down_move.clip(lower=0)

    smoothing = {"alpha": 1 / period, "min_periods": period, "adjust": False}
    smoothed_tr = true_range.ewm(**smoothing).mean()
    plus_di = 100 * plus_dm.ewm(**smoothing).mean() / smoothed_tr
    minus_di = 100 * minus_dm.ewm(**smoothing).mean() / smoothed_tr

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    return dx.ewm(**smoothing).mean()


def _adaptive_rsi_thresholds(adx: float, adx_trend_threshold: float) -> tuple[float, float]:
    """The further ADX sits below the trend threshold, the more confidently range-bound
    the market is, so bands tighten toward 40/60 to catch smaller oscillations. Bands
    widen back to the standard 30/70 as ADX approaches the trend/range boundary."""
    fraction = max(min(adx, adx_trend_threshold), 0) / adx_trend_threshold
    return 40 - 10 * fraction, 60 + 10 * fraction


class RegimeSwitchingStrategy(Strategy):
    """Uses ADX to detect trending vs. range-bound markets and delegates to the strategy
    suited to each: MA crossover while trending, RSI mean-reversion while range-bound.

    Mean-reversion's RSI thresholds adapt with ADX rather than staying fixed at 30/70 -
    see _adaptive_rsi_thresholds.
    """

    name = "regime_switching"

    def __init__(
        self,
        adx_period: int = 14,
        adx_trend_threshold: float = 25,
        ma_short: int = 20,
        ma_long: int = 50,
        rsi_period: int = 14,
    ):
        self.adx_period = adx_period
        self.adx_trend_threshold = adx_trend_threshold
        self.rsi_period = rsi_period
        self._crossover = MovingAverageCrossoverStrategy(ma_short, ma_long)
        self._min_bars = max(ma_long + 1, adx_period * 2 + 1)

    def generate_signal(self, symbol: str, bars: pd.DataFrame) -> tuple[SignalAction, str]:
        if len(bars) < self._min_bars:
            return SignalAction.hold, f"not enough history ({len(bars)}/{self._min_bars} bars)"

        adx = _adx(bars, self.adx_period).iloc[-1]

        if adx > self.adx_trend_threshold:
            action, reason = self._crossover.generate_signal(symbol, bars)
            return action, f"trending (ADX={adx:.1f}): {reason}"

        oversold, overbought = _adaptive_rsi_thresholds(adx, self.adx_trend_threshold)
        mean_reversion = MeanReversionStrategy(self.rsi_period, oversold, overbought)
        action, reason = mean_reversion.generate_signal(symbol, bars)
        return action, f"range-bound (ADX={adx:.1f}, bands={oversold:.0f}/{overbought:.0f}): {reason}"
