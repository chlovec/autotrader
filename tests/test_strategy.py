import math

import pandas as pd
import pytest

from db.models import SignalAction
from engine.strategy import (
    MeanReversionStrategy,
    MovingAverageCrossoverStrategy,
    RegimeSwitchingStrategy,
    _adaptive_rsi_thresholds,
    _adx,
    _rsi,
)


def _bars(closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame({"close": closes}, index=pd.date_range("2026-01-01", periods=len(closes), freq="D"))


def test_not_enough_history_holds():
    strategy = MovingAverageCrossoverStrategy(short_window=3, long_window=5)
    action, reason = strategy.generate_signal("SPY", _bars([10, 10, 10, 10, 10]))
    assert action == SignalAction.hold
    assert "not enough history" in reason


def test_golden_cross_triggers_buy():
    strategy = MovingAverageCrossoverStrategy(short_window=3, long_window=5)
    action, _ = strategy.generate_signal("SPY", _bars([10, 10, 10, 10, 10, 10, 20]))
    assert action == SignalAction.buy


def test_death_cross_triggers_sell():
    strategy = MovingAverageCrossoverStrategy(short_window=3, long_window=5)
    action, _ = strategy.generate_signal("SPY", _bars([20, 20, 20, 20, 20, 20, 10]))
    assert action == SignalAction.sell


def test_flat_prices_hold():
    strategy = MovingAverageCrossoverStrategy(short_window=3, long_window=5)
    action, reason = strategy.generate_signal("SPY", _bars([15, 15, 15, 15, 15, 15, 15]))
    assert action == SignalAction.hold
    assert "no cross" in reason


def test_rejects_invalid_windows():
    with pytest.raises(ValueError):
        MovingAverageCrossoverStrategy(short_window=10, long_window=5)


def test_mean_reversion_not_enough_history_holds():
    strategy = MeanReversionStrategy(period=5)
    action, reason = strategy.generate_signal("SPY", _bars([100, 100, 100, 100, 100]))
    assert action == SignalAction.hold
    assert "not enough history" in reason


def test_mean_reversion_buys_on_oversold_cross():
    period = 14
    strategy = MeanReversionStrategy(period=period)
    # Rises long enough for RSI to reach a high baseline, then declines long enough
    # for RSI to fall monotonically through the oversold threshold.
    rising = [100 + i for i in range(period + 5)]
    falling = [rising[-1] - i for i in range(1, 41)]
    bars = _bars(rising + falling)

    rsi = _rsi(bars["close"], period)
    crossing_idx = next(
        i for i in range(1, len(rsi)) if rsi.iloc[i - 1] >= strategy.oversold and rsi.iloc[i] < strategy.oversold
    )

    action, reason = strategy.generate_signal("SPY", bars.iloc[: crossing_idx + 1])
    assert action == SignalAction.buy
    assert "oversold" in reason

    action_before, _ = strategy.generate_signal("SPY", bars.iloc[:crossing_idx])
    assert action_before == SignalAction.hold


def test_mean_reversion_sells_on_overbought_cross():
    period = 14
    strategy = MeanReversionStrategy(period=period)
    # Mirror of the oversold test: decline first, then rise monotonically through
    # the overbought threshold.
    falling = [100 - i for i in range(period + 5)]
    rising = [falling[-1] + i for i in range(1, 41)]
    bars = _bars(falling + rising)

    rsi = _rsi(bars["close"], period)
    crossing_idx = next(
        i for i in range(1, len(rsi)) if rsi.iloc[i - 1] <= strategy.overbought and rsi.iloc[i] > strategy.overbought
    )

    action, reason = strategy.generate_signal("SPY", bars.iloc[: crossing_idx + 1])
    assert action == SignalAction.sell
    assert "overbought" in reason

    action_before, _ = strategy.generate_signal("SPY", bars.iloc[:crossing_idx])
    assert action_before == SignalAction.hold


def test_mean_reversion_rejects_invalid_thresholds():
    with pytest.raises(ValueError):
        MeanReversionStrategy(oversold=70, overbought=30)


def _ohlc_bars(closes: list[float], half_range: float = 0.5) -> pd.DataFrame:
    return pd.DataFrame(
        {"high": [c + half_range for c in closes], "low": [c - half_range for c in closes], "close": closes},
        index=pd.date_range("2026-01-01", periods=len(closes), freq="D"),
    )


def test_adaptive_rsi_thresholds():
    assert _adaptive_rsi_thresholds(25, 25) == (30, 70)
    assert _adaptive_rsi_thresholds(0, 25) == (40, 60)
    assert _adaptive_rsi_thresholds(12.5, 25) == (35, 65)
    assert _adaptive_rsi_thresholds(50, 25) == (30, 70)  # clamped: never called above threshold in practice


def test_regime_switching_not_enough_history_holds():
    strategy = RegimeSwitchingStrategy(adx_period=5, ma_short=3, ma_long=5)
    action, reason = strategy.generate_signal("SPY", _ohlc_bars([100, 100, 100]))
    assert action == SignalAction.hold
    assert "not enough history" in reason


def test_trending_regime_delegates_to_crossover():
    period = 14
    bars = _ohlc_bars([100 + 0.8 * i for i in range(90)])

    strategy = RegimeSwitchingStrategy(adx_period=period, adx_trend_threshold=25, ma_short=20, ma_long=50)
    adx = _adx(bars, period).iloc[-1]
    assert adx > 25, "fixture should produce a clearly trending ADX reading"

    action, reason = strategy.generate_signal("SPY", bars)
    expected_action, expected_reason = MovingAverageCrossoverStrategy(20, 50).generate_signal("SPY", bars)
    assert action == expected_action
    assert "trending" in reason
    assert expected_reason in reason


def test_range_bound_regime_delegates_to_mean_reversion_with_adapted_thresholds():
    period = 14
    closes = [100 + 5 * math.sin(2 * math.pi * i / 10) for i in range(90)]
    bars = _ohlc_bars(closes, half_range=0.3)

    strategy = RegimeSwitchingStrategy(adx_period=period, adx_trend_threshold=25, rsi_period=period)
    adx = _adx(bars, period).iloc[-1]
    assert adx < 25, "fixture should produce a clearly range-bound ADX reading"

    oversold, overbought = _adaptive_rsi_thresholds(adx, 25)
    action, reason = strategy.generate_signal("SPY", bars)
    expected_action, expected_reason = MeanReversionStrategy(period, oversold, overbought).generate_signal("SPY", bars)
    assert action == expected_action
    assert "range-bound" in reason
    assert expected_reason in reason
