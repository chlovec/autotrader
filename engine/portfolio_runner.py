import logging

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from db.models import EquitySnapshot, Signal
from db.session import get_session, init_db
from engine.brokers import make_broker
from engine.brokers.base import Timeframe
from engine.config import load_config
from engine.execution import ExecutionEngine
from engine.portfolio import RebalancingPortfolio
from engine.risk import RiskManager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("autotrader.portfolio_runner")


def rebalance_once(portfolio: RebalancingPortfolio) -> None:
    config = load_config()
    broker = make_broker(config)

    if not broker.get_clock().is_open:
        logger.info("market closed, skipping rebalance")
        return

    with get_session() as session:
        risk = RiskManager(config, session)
        execution = ExecutionEngine(broker, session)

        engaged, reason = risk.kill_switch_engaged()
        if engaged:
            logger.info("kill switch engaged, skipping rebalance: %s", reason)
            return

        account = broker.get_account()
        session.add(EquitySnapshot(equity=account.equity, cash=account.cash, buying_power=account.buying_power))
        session.commit()

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
            signal = Signal(symbol=order.symbol, strategy_name="rebalancing_portfolio", action=order.action, reason=order.reason)
            session.add(signal)
            session.commit()

            trade = execution.submit_market_order(order.symbol, order.action, order.qty, signal_id=signal.id)
            logger.info("rebalance: %s %s qty=%s (%s)", order.action.value, order.symbol, trade.qty, order.reason)


def main(target_weights: dict[str, float], day: int = 1, hour: int = 9, minute: int = 35) -> None:
    """Runs once a month on `day` at hour:minute America/New_York. If `day` falls on a
    non-trading day the job just no-ops (market closed) until the scheduler's next monthly
    firing - fine for a rebalance cadence, no need to hunt for the nearest trading day."""
    init_db()
    portfolio = RebalancingPortfolio(target_weights)
    scheduler = BlockingScheduler()
    trigger = CronTrigger(day=day, hour=hour, minute=minute, timezone="America/New_York")
    scheduler.add_job(rebalance_once, trigger, args=[portfolio])
    logger.info("starting monthly rebalance loop on day %d at %02d:%02d America/New_York, weights=%s", day, hour, minute, target_weights)
    scheduler.start()
