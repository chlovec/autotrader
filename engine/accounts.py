"""Turns the ACCOUNT_IDS env var into db.models.Account rows, and turns an Account's
strategy assignment into the Strategy/RebalancingPortfolio object that actually runs it.

Accounts are dashboard-owned once created: sync_accounts_from_env only ever inserts a row
the first time an id is seen (seeded from env) and refreshes broker/display_name on every
subsequent sync - active/limits/strategy/kill-switch are never overwritten from env again,
so a dashboard edit can't be silently reverted by a restart.
"""

import json
import os

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import Account
from engine.config import Config, load_account_ids
from engine.portfolio import RebalancingPortfolio
from engine.strategy import MeanReversionStrategy, MovingAverageCrossoverStrategy, RegimeSwitchingStrategy, Strategy

REBALANCE_STRATEGY_NAME = "rebalancing_portfolio"

_DEFAULT_STRATEGY_NAME = REBALANCE_STRATEGY_NAME
_DEFAULT_STRATEGY_PARAMS = {"target_weights": {"SPY": 1 / 3, "TLT": 1 / 3, "GLD": 1 / 3}}

_STRATEGY_FACTORIES = {
    "ma_crossover": MovingAverageCrossoverStrategy,
    "mean_reversion": MeanReversionStrategy,
    "regime_switching": RegimeSwitchingStrategy,
}


def build_strategy(strategy_name: str, strategy_params: dict) -> Strategy | RebalancingPortfolio:
    """Factory over the four existing strategy shapes - three per-symbol Strategy
    subclasses (engine/strategy.py) plus RebalancingPortfolio (engine/portfolio.py), which
    is deliberately not a Strategy (see its docstring). strategy_params is passed as
    kwargs/positional as each constructor expects."""
    if strategy_name == REBALANCE_STRATEGY_NAME:
        return RebalancingPortfolio(strategy_params["target_weights"])
    try:
        factory = _STRATEGY_FACTORIES[strategy_name]
    except KeyError:
        supported = ", ".join(sorted([*_STRATEGY_FACTORIES, REBALANCE_STRATEGY_NAME]))
        raise ValueError(f"unsupported strategy_name {strategy_name!r} - supported: {supported}") from None
    return factory(**strategy_params)


def _account_env(account_id: str, suffix: str, default: str = "") -> str:
    return os.environ.get(f"ACCOUNT_{account_id}_{suffix}", default)


def sync_accounts_from_env(session: Session, config: Config) -> None:
    """For each id in ACCOUNT_IDS not yet in the DB, inserts an Account row seeded from
    env (broker, display name, strategy, and the config's default risk limits). For ids
    already present, only broker/display_name are refreshed - everything else is
    dashboard-owned from that point on."""
    account_ids = load_account_ids()
    existing = {a.id: a for a in session.execute(select(Account)).scalars().all()}

    for account_id in account_ids:
        broker = _account_env(account_id, "BROKER")
        display_name = _account_env(account_id, "DISPLAY_NAME", account_id)

        if account_id in existing:
            account = existing[account_id]
            account.broker = broker
            account.display_name = display_name
            continue

        strategy_name = _account_env(account_id, "STRATEGY", _DEFAULT_STRATEGY_NAME)
        raw_params = _account_env(account_id, "STRATEGY_PARAMS")
        strategy_params = json.loads(raw_params) if raw_params else _DEFAULT_STRATEGY_PARAMS

        session.add(
            Account(
                id=account_id,
                broker=broker,
                display_name=display_name,
                active=True,
                strategy_name=strategy_name,
                strategy_params=json.dumps(strategy_params),
                max_position_size_usd=config.max_position_size_usd,
                max_daily_loss_usd=config.max_daily_loss_usd,
            )
        )

    session.commit()


def get_active_accounts(session: Session) -> list[Account]:
    return list(session.execute(select(Account).where(Account.active.is_(True)).order_by(Account.id)).scalars().all())


def get_research_account(session: Session) -> Account | None:
    """Research (engine/research_runner.py) is global - one screen shared by every
    signal-strategy account - but still needs *a* live broker connection for get_bars.
    Resolved as the first active account by id: a reasonable, deterministic default, not
    something the account itself opts into."""
    accounts = get_active_accounts(session)
    return accounts[0] if accounts else None


def get_all_accounts(session: Session) -> list[Account]:
    return list(session.execute(select(Account).order_by(Account.id)).scalars().all())
