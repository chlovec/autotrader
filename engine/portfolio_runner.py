import datetime as dt
import logging

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import EquitySnapshot, Signal
from db.session import get_session, init_db
from engine.brokers import make_broker
from engine.brokers.base import Timeframe
from engine.config import Config, load_config
from engine.execution import ExecutionEngine
from engine.notifications import log_and_notify, make_notifier
from engine.portfolio import RebalancingPortfolio
from engine.risk import RiskManager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("autotrader.portfolio_runner")

REBALANCE_STRATEGY_NAME = "rebalancing_portfolio"


def _already_rebalanced_this_month(session: Session, now: dt.datetime | None = None) -> bool:
    now = now or dt.datetime.utcnow()
    latest = session.execute(
        select(Signal).where(Signal.strategy_name == REBALANCE_STRATEGY_NAME).order_by(Signal.timestamp.desc())
    ).scalars().first()
    return latest is not None and latest.timestamp.year == now.year and latest.timestamp.month == now.month


def rebalance_once(portfolio: RebalancingPortfolio, config: Config | None = None) -> None:
    config = config or load_config(argv=[])
    broker = make_broker(config)
    notifier = make_notifier(config)

    # Recorded before the market-hours gate below so the dashboard's equity/cash tiles
    # reflect the account even when called outside trading hours (evenings, weekends),
    # instead of only updating whenever a rebalance actually runs.
    account = broker.get_account()
    with get_session() as session:
        session.add(EquitySnapshot(
            equity=account.equity, cash=account.cash, buying_power=account.buying_power,
            broker=broker.name, account_id=account.account_id,
        ))
        session.commit()

    if not broker.get_clock().is_open:
        logger.info("market closed, skipping rebalance")
        return

    with get_session() as session:
        risk = RiskManager(config, session, broker.name, account.account_id)
        execution = ExecutionEngine(broker, account, session, notifier)

        engaged, reason = risk.kill_switch_engaged()
        if engaged:
            log_and_notify(session, notifier, "warning", "portfolio_runner", f"kill switch engaged, skipping rebalance: {reason}")
            return

        if _already_rebalanced_this_month(session):
            logger.info("already rebalanced this month, skipping")
            return

        if risk.daily_loss_limit_breached():
            log_and_notify(
                session, notifier, "critical", "portfolio_runner",
                f"daily loss limit breached (limit=${config.max_daily_loss_usd}), skipping this month's rebalance",
            )
            return

        positions = {p.symbol: p for p in broker.get_positions()}
        prices = {
            symbol: float(broker.get_bars(symbol, Timeframe.DAY, limit=1)["close"].iloc[-1])
            for symbol in portfolio.target_weights
        }

        orders = portfolio.compute_rebalance_orders(account, positions, prices)
        if not orders:
            logger.info("portfolio already within target weights, nothing to rebalance")
            return

        for order in orders:
            signal = Signal(symbol=order.symbol, strategy_name=REBALANCE_STRATEGY_NAME, action=order.action, reason=order.reason)
            session.add(signal)
            session.commit()

            trade = execution.submit_market_order(order.symbol, order.action, order.qty, signal_id=signal.id)
            logger.info("rebalance: %s %s qty=%s (%s)", order.action.value, order.symbol, trade.qty, order.reason)


def main(target_weights: dict[str, float], config: Config | None = None, day: str = "1-4", hour: int = 9, minute: int = 35) -> None:
    """Runs once a month, on the first trading day at or after `day` (a range like "1-4"
    covers a day-1 weekend without skipping the whole month - _already_rebalanced_this_month
    then stops it firing again on day 2-4 once day 1 or the first open day after it succeeds).
    Also runs once immediately on startup, in case the account needs its initial allocation
    before the next scheduled slot.

    config is resolved once, here, and reused for every scheduled rebalance - see
    engine/runner.py's main() docstring for why that matters (fail-fast at startup)."""
    config = config or load_config(argv=[])
    init_db()
    portfolio = RebalancingPortfolio(target_weights)

    rebalance_once(portfolio, config=config)

    scheduler = BlockingScheduler()
    trigger = CronTrigger(day=day, hour=hour, minute=minute, timezone="America/New_York")
    scheduler.add_job(rebalance_once, trigger, args=[portfolio], kwargs={"config": config})
    logger.info("starting monthly rebalance loop on day %s at %02d:%02d America/New_York, weights=%s", day, hour, minute, target_weights)
    scheduler.start()
