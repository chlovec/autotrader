"""The trading loop, now driving every active account instead of one. Replaces the old
engine/runner.py (signal strategies) + engine/portfolio_runner.py (rebalancing) - those were
one broker/account per process; run_all_accounts_once loops over every active db.models.Account
and dispatches each to the strategy shape it's assigned (see engine/accounts.py's
build_strategy), building that account's own broker connection from its own credentials.

One account's exception never blocks the others' cycles - each iteration of the loop below is
independently try/excepted.

Since _already_rebalanced_this_month makes a monthly rebalance idempotent under repeated calls,
one daily mon-fri 9:35am trigger safely drives every account regardless of whether it's a
daily-signal or monthly-rebalance strategy - no need for two separate cron schedules the way
run.py (daily) and run_portfolio.py (monthly) used to have.
"""

import datetime as dt
import json
import logging

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from db.models import Account, EquitySnapshot, Signal, SignalAction
from db.queries import get_watchlist_symbols
from db.session import get_session, init_db
from engine.accounts import REBALANCE_STRATEGY_NAME, build_strategy, get_active_accounts, sync_accounts_from_env
from engine.brokers import make_broker
from engine.brokers.base import Timeframe
from engine.config import Config, load_account_credentials, load_config
from engine.execution import ExecutionEngine
from engine.notifications import log_and_notify, make_notifier
from engine.portfolio import RebalancingPortfolio
from engine.risk import RiskManager
from engine.strategy import Strategy

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("autotrader.multi_runner")


def _already_rebalanced_this_month(session, account_id: str, now: dt.datetime | None = None) -> bool:
    now = now or dt.datetime.utcnow()
    latest = session.execute(
        select(Signal)
        .where(Signal.account_id == account_id, Signal.strategy_name == REBALANCE_STRATEGY_NAME)
        .order_by(Signal.timestamp.desc())
    ).scalars().first()
    return latest is not None and latest.timestamp.year == now.year and latest.timestamp.month == now.month


def _run_signal_account_once(account: Account, strategy: Strategy, config: Config) -> None:
    broker = make_broker(account.id, load_account_credentials(account.id))
    notifier = make_notifier(config)

    # Recorded before the market-hours gate below so the dashboard's equity/cash tiles
    # reflect the account even when called outside trading hours, instead of only
    # updating whenever a cycle actually trades.
    account_snapshot = broker.get_account()
    with get_session() as session:
        session.add(EquitySnapshot(
            account_id=account.id, broker=broker.name, broker_account_id=account_snapshot.account_id,
            equity=account_snapshot.equity, cash=account_snapshot.cash, buying_power=account_snapshot.buying_power,
        ))
        session.commit()

    if not broker.get_clock().is_open:
        logger.info("[%s] market closed, skipping cycle", account.id)
        return

    with get_session() as session:
        db_account = session.get(Account, account.id)
        risk = RiskManager(session, db_account)
        execution = ExecutionEngine(broker, account.id, account_snapshot, session, notifier)

        engaged, reason = risk.kill_switch_engaged()
        if engaged:
            log_and_notify(
                session, notifier, "warning", "multi_runner", f"kill switch engaged, skipping cycle: {reason}", account_id=account.id
            )
            return

        if risk.daily_loss_limit_breached():
            log_and_notify(
                session, notifier, "critical", "multi_runner",
                f"daily loss limit breached (limit=${db_account.max_daily_loss_usd}), halting trading for today",
                account_id=account.id,
            )
            return

        watchlist_symbols = get_watchlist_symbols(session)
        if not watchlist_symbols:
            log_and_notify(
                session, notifier, "warning", "multi_runner", "no researched symbols in watchlist, skipping cycle", account_id=account.id
            )
            return

        # Tracked locally rather than re-derived from Trade history each iteration - see
        # RiskManager.approve's docstring for why (a just-submitted order's fill_price
        # isn't in the DB yet, so consecutive buys within this loop would under-count
        # each other if re-queried). Seeded from a live snapshot, then updated in step
        # with each order actually submitted below.
        current_positions = {p.symbol: p for p in broker.get_positions()}
        running_total_exposure = sum(p.market_value for p in current_positions.values())

        for symbol in watchlist_symbols:
            bars = broker.get_bars(symbol, Timeframe.DAY, limit=100)

            action, reason = strategy.generate_signal(symbol, bars)
            signal = Signal(account_id=account.id, symbol=symbol, strategy_name=strategy.name, action=action, reason=reason)
            session.add(signal)
            session.commit()

            last_price = float(bars["close"].iloc[-1])
            if action == SignalAction.buy:
                order_value_usd = db_account.max_position_size_usd  # Phase 1 keeps sizing simple: one fixed-size position
                qty = round(order_value_usd / last_price, 4)
            else:
                order_value_usd = 0.0
                qty = broker.get_position_qty(symbol)

            approved, why = risk.approve(symbol, action, order_value_usd, running_total_exposure)
            if not approved:
                logger.info("[%s] signal for %s not executed: %s", account.id, symbol, why)
                continue

            if action == SignalAction.sell and qty == 0:
                logger.info("[%s] sell signal for %s but no open position, skipping", account.id, symbol)
                continue

            trade = execution.submit_market_order(symbol, action, qty, signal_id=signal.id)
            logger.info("[%s] submitted %s order for %s qty=%s", account.id, action.value, symbol, trade.qty)

            if action == SignalAction.buy:
                running_total_exposure += order_value_usd
            else:
                running_total_exposure -= current_positions[symbol].market_value if symbol in current_positions else qty * last_price


def _rebalance_account_once(account: Account, portfolio: RebalancingPortfolio, config: Config) -> None:
    broker = make_broker(account.id, load_account_credentials(account.id))
    notifier = make_notifier(config)

    account_snapshot = broker.get_account()
    with get_session() as session:
        session.add(EquitySnapshot(
            account_id=account.id, broker=broker.name, broker_account_id=account_snapshot.account_id,
            equity=account_snapshot.equity, cash=account_snapshot.cash, buying_power=account_snapshot.buying_power,
        ))
        session.commit()

    if not broker.get_clock().is_open:
        logger.info("[%s] market closed, skipping rebalance", account.id)
        return

    with get_session() as session:
        db_account = session.get(Account, account.id)
        risk = RiskManager(session, db_account)
        execution = ExecutionEngine(broker, account.id, account_snapshot, session, notifier)

        engaged, reason = risk.kill_switch_engaged()
        if engaged:
            log_and_notify(
                session, notifier, "warning", "multi_runner", f"kill switch engaged, skipping rebalance: {reason}", account_id=account.id
            )
            return

        if _already_rebalanced_this_month(session, account.id):
            logger.info("[%s] already rebalanced this month, skipping", account.id)
            return

        if risk.daily_loss_limit_breached():
            log_and_notify(
                session, notifier, "critical", "multi_runner",
                f"daily loss limit breached (limit=${db_account.max_daily_loss_usd}), skipping this month's rebalance",
                account_id=account.id,
            )
            return

        positions = {p.symbol: p for p in broker.get_positions()}
        prices = {
            symbol: float(broker.get_bars(symbol, Timeframe.DAY, limit=1)["close"].iloc[-1])
            for symbol in portfolio.target_weights
        }

        orders = portfolio.compute_rebalance_orders(account_snapshot, positions, prices, db_account.max_total_exposure_usd)
        if not orders:
            logger.info("[%s] portfolio already within target weights, nothing to rebalance", account.id)
            return

        for order in orders:
            signal = Signal(
                account_id=account.id, symbol=order.symbol, strategy_name=REBALANCE_STRATEGY_NAME, action=order.action, reason=order.reason
            )
            session.add(signal)
            session.commit()

            trade = execution.submit_market_order(order.symbol, order.action, order.qty, signal_id=signal.id)
            logger.info("[%s] rebalance: %s %s qty=%s (%s)", account.id, order.action.value, order.symbol, trade.qty, order.reason)


def run_all_accounts_once(config: Config | None = None) -> None:
    config = config or load_config(argv=[])
    with get_session() as session:
        sync_accounts_from_env(session, config)
        accounts = get_active_accounts(session)

    for account in accounts:
        try:
            strategy_or_portfolio = build_strategy(account.strategy_name, json.loads(account.strategy_params))
            if account.strategy_name == REBALANCE_STRATEGY_NAME:
                _rebalance_account_once(account, strategy_or_portfolio, config)
            else:
                _run_signal_account_once(account, strategy_or_portfolio, config)
        except Exception:
            logger.exception("[%s] cycle failed, continuing with other accounts", account.id)
            try:
                with get_session() as session:
                    log_and_notify(
                        session, make_notifier(config), "error", "multi_runner",
                        f"account {account.id} cycle raised an unhandled exception - see logs", account_id=account.id,
                    )
            except Exception:
                logger.exception("[%s] also failed to record the cycle failure", account.id)


def main(config: Config | None = None, hour: int = 9, minute: int = 35) -> None:
    """Runs once immediately on startup (so a freshly-activated account doesn't wait for
    the next scheduled slot), then once per weekday at hour:minute America/New_York.

    config is resolved once, here, and reused for every scheduled cycle - not just left for
    run_all_accounts_once to load fresh on the first scheduled fire. Resolving it eagerly
    means a bad global config fails at startup instead of surfacing only at the next
    weekday 9:35am, and even then only as a logged APScheduler job error rather than a
    startup crash."""
    config = config or load_config(argv=[])
    init_db()

    run_all_accounts_once(config)

    scheduler = BlockingScheduler()
    trigger = CronTrigger(day_of_week="mon-fri", hour=hour, minute=minute, timezone="America/New_York")
    scheduler.add_job(run_all_accounts_once, trigger, kwargs={"config": config})
    logger.info("starting multi-account daily loop at %02d:%02d America/New_York", hour, minute)
    scheduler.start()
