from dataclasses import dataclass

from db.models import SignalAction
from engine.brokers.base import AccountSnapshot, PositionSnapshot


@dataclass(frozen=True)
class RebalanceOrder:
    symbol: str
    action: SignalAction
    qty: float
    reason: str


class RebalancingPortfolio:
    """Periodically rebalances a fixed set of target weights across symbols.

    Unlike the per-symbol Strategy interface (engine/strategy.py), rebalancing needs the
    whole account state at once - every current position and total equity - not just one
    symbol's bar history, so it's deliberately a separate abstraction rather than another
    Strategy subclass.

    No per-symbol dollar cap here the way RiskManager enforces for the directional
    strategies: target weight * equity IS the position size by construction, so that cap
    doesn't apply to this model. The kill switch and daily loss limit still do.
    """

    def __init__(self, target_weights: dict[str, float]):
        total = sum(target_weights.values())
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"target_weights must sum to 1.0, got {total}")
        self.target_weights = target_weights

    def compute_rebalance_orders(
        self,
        account: AccountSnapshot,
        positions: dict[str, PositionSnapshot],
        prices: dict[str, float],
    ) -> list[RebalanceOrder]:
        orders = []
        for symbol, target_weight in self.target_weights.items():
            target_value = account.equity * target_weight
            current_value = positions[symbol].market_value if symbol in positions else 0.0
            diff_value = target_value - current_value
            price = prices[symbol]

            # Skip drift too small to be worth a trade (avoids churn from rounding noise).
            if abs(diff_value) < max(price, 1.0):
                continue

            qty = round(abs(diff_value) / price, 4)
            action = SignalAction.buy if diff_value > 0 else SignalAction.sell
            orders.append(
                RebalanceOrder(
                    symbol=symbol,
                    action=action,
                    qty=qty,
                    reason=f"rebalance: target {target_weight:.1%} (${target_value:,.2f}), actual ${current_value:,.2f}",
                )
            )
        return orders
