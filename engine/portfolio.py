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
        max_total_exposure_usd: float = 0.0,
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

        if max_total_exposure_usd > 0:
            orders = self._cap_buys_to_total_exposure(orders, positions, prices, max_total_exposure_usd)
        return orders

    @staticmethod
    def _cap_buys_to_total_exposure(
        orders: list[RebalanceOrder],
        positions: dict[str, PositionSnapshot],
        prices: dict[str, float],
        max_total_exposure_usd: float,
    ) -> list[RebalanceOrder]:
        """Throttles this cycle's buy orders (never sells - selling is always what frees
        up room under the cap, never what should be blocked by it) so total exposure after
        this rebalance never exceeds max_total_exposure_usd. Existing overweight positions
        aren't force-sold down to the cap on their own - only new buys that would push
        the account further over it are held back."""
        current_total = sum(p.market_value for p in positions.values())
        sells = [o for o in orders if o.action == SignalAction.sell]
        buys = [o for o in orders if o.action == SignalAction.buy]

        sell_value = sum(o.qty * prices[o.symbol] for o in sells)
        buy_value = sum(o.qty * prices[o.symbol] for o in buys)
        headroom = max_total_exposure_usd - (current_total - sell_value)

        if headroom <= 0 or not buys:
            return sells

        if buy_value <= headroom:
            return orders

        scale = headroom / buy_value
        scaled_buys = []
        for order in buys:
            price = prices[order.symbol]
            qty = round(order.qty * scale, 4)
            if qty * price < max(price, 1.0):  # dust after scaling - not worth trading
                continue
            scaled_buys.append(RebalanceOrder(symbol=order.symbol, action=order.action, qty=qty, reason=f"{order.reason} (scaled down to stay within max total exposure)"))
        return sells + scaled_buys
