from abc import ABC, abstractmethod

import pandas as pd

from db.models import SignalAction


class Strategy(ABC):
    """Interface every concrete strategy implements. Phase 1 fills in a real subclass."""

    name: str

    @abstractmethod
    def generate_signal(self, symbol: str, bars: pd.DataFrame) -> tuple[SignalAction, str]:
        """Given recent OHLCV bars for a symbol, return (action, reason)."""
        raise NotImplementedError
