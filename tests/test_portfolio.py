import pytest

from db.models import SignalAction
from engine.brokers.base import AccountSnapshot, PositionSnapshot
from engine.portfolio import RebalancingPortfolio


def _account(equity: float) -> AccountSnapshot:
    return AccountSnapshot(equity=equity, cash=equity, buying_power=equity, account_id="acct-1")


def test_rejects_weights_not_summing_to_one():
    with pytest.raises(ValueError):
        RebalancingPortfolio({"SPY": 0.5, "TLT": 0.4})


def test_initial_allocation_from_all_cash():
    portfolio = RebalancingPortfolio({"SPY": 1 / 3, "TLT": 1 / 3, "GLD": 1 / 3})
    orders = portfolio.compute_rebalance_orders(
        account=_account(9000),
        positions={},
        prices={"SPY": 450.0, "TLT": 90.0, "GLD": 180.0},
    )

    assert {o.symbol for o in orders} == {"SPY", "TLT", "GLD"}
    assert all(o.action == SignalAction.buy for o in orders)
    spy_order = next(o for o in orders if o.symbol == "SPY")
    assert spy_order.qty == pytest.approx(3000 / 450.0, abs=1e-4)


def test_at_target_weights_generates_no_orders():
    portfolio = RebalancingPortfolio({"SPY": 0.5, "TLT": 0.5})
    positions = {
        "SPY": PositionSnapshot(symbol="SPY", qty=10, avg_entry_price=450, market_value=4500, unrealized_pl=0),
        "TLT": PositionSnapshot(symbol="TLT", qty=50, avg_entry_price=90, market_value=4500, unrealized_pl=0),
    }
    orders = portfolio.compute_rebalance_orders(
        account=_account(9000), positions=positions, prices={"SPY": 450.0, "TLT": 90.0}
    )
    assert orders == []


def test_drifted_position_generates_sell_to_rebalance():
    # SPY ran up and is now overweight relative to its 50% target; TLT is correspondingly underweight.
    portfolio = RebalancingPortfolio({"SPY": 0.5, "TLT": 0.5})
    positions = {
        "SPY": PositionSnapshot(symbol="SPY", qty=10, avg_entry_price=450, market_value=6000, unrealized_pl=1500),
        "TLT": PositionSnapshot(symbol="TLT", qty=50, avg_entry_price=90, market_value=3000, unrealized_pl=-1500),
    }
    orders = portfolio.compute_rebalance_orders(
        account=_account(9000), positions=positions, prices={"SPY": 600.0, "TLT": 60.0}
    )

    spy_order = next(o for o in orders if o.symbol == "SPY")
    tlt_order = next(o for o in orders if o.symbol == "TLT")
    assert spy_order.action == SignalAction.sell
    assert tlt_order.action == SignalAction.buy


def test_tiny_drift_is_skipped():
    portfolio = RebalancingPortfolio({"SPY": 1.0})
    positions = {"SPY": PositionSnapshot(symbol="SPY", qty=10, avg_entry_price=450, market_value=4499.5, unrealized_pl=0)}
    orders = portfolio.compute_rebalance_orders(account=_account(4500), positions=positions, prices={"SPY": 450.0})
    assert orders == []
