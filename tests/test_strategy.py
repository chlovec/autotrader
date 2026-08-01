import pandas as pd

from db.models import SignalAction
from engine.strategy import MovingAverageCrossoverStrategy


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
    try:
        MovingAverageCrossoverStrategy(short_window=10, long_window=5)
        assert False, "expected ValueError"
    except ValueError:
        pass
