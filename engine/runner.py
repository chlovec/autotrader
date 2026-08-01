import logging

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from db.models import EquitySnapshot, Signal, SignalAction
from db.queries import get_watchlist_symbols
from db.session import get_session, init_db
from engine.brokers import make_broker
from engine.brokers.base import Timeframe
from engine.config import load_config
from engine.execution import ExecutionEngine
from engine.notifications import log_and_notify, make_notifier
from engine.risk import RiskManager
from engine.strategy import Strategy

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("autotrader.runner")


def run_once(strategy: Strategy, symbols: list[str] | None = None) -> None:
    config = load_config()
    broker = make_broker(config)
    notifier = make_notifier(config)

    # Recorded before the market-hours gate below so the dashboard's equity/cash tiles
    # reflect the account even when called outside trading hours (evenings, weekends),
    # instead of only updating whenever a cycle actually trades.
    account = broker.get_account()
    with get_session() as session:
        session.add(EquitySnapshot(equity=account.equity, cash=account.cash, buying_power=account.buying_power))
        session.commit()

    if not broker.get_clock().is_open:
        logger.info("market closed, skipping cycle")
        return

    with get_session() as session:
        risk = RiskManager(config, session)
        execution = ExecutionEngine(broker, session, notifier)

        engaged, reason = risk.kill_switch_engaged()
        if engaged:
            log_and_notify(session, notifier, "warning", "runner", f"kill switch engaged, skipping cycle: {reason}")
            return

        if risk.daily_loss_limit_breached():
            log_and_notify(
                session, notifier, "critical", "runner",
                f"daily loss limit breached (limit=${config.max_daily_loss_usd}), halting trading for today",
            )
            return

        watchlist_symbols = symbols if symbols is not None else get_watchlist_symbols(session)
        if not watchlist_symbols:
            log_and_notify(session, notifier, "warning", "runner", "no researched symbols in watchlist, skipping cycle")
            return

        for symbol in watchlist_symbols:
            bars = broker.get_bars(symbol, Timeframe.DAY, limit=100)

            action, reason = strategy.generate_signal(symbol, bars)
            signal = Signal(symbol=symbol, strategy_name=strategy.name, action=action, reason=reason)
            session.add(signal)
            session.commit()

            last_price = float(bars["close"].iloc[-1])
            if action == SignalAction.buy:
                order_value_usd = config.max_position_size_usd  # Phase 1 keeps sizing simple: one fixed-size position
                qty = round(order_value_usd / last_price, 4)
            else:
                order_value_usd = 0.0
                qty = broker.get_position_qty(symbol)

            approved, why = risk.approve(symbol, action, order_value_usd)
            if not approved:
                logger.info("signal for %s not executed: %s", symbol, why)
                continue

            if action == SignalAction.sell and qty == 0:
                logger.info("sell signal for %s but no open position, skipping", symbol)
                continue

            trade = execution.submit_market_order(symbol, action, qty, signal_id=signal.id)
            logger.info("submitted %s order for %s qty=%s", action.value, symbol, trade.qty)


def main(strategy: Strategy, symbols: list[str] | None = None, hour: int = 9, minute: int = 35) -> None:
    """Runs once per weekday at hour:minute America/New_York, using the latest completed
    daily bars. Default of 9:35 gives the market a few minutes to open before trading.

    symbols defaults to None, meaning each cycle trades whatever engine/research_runner.py
    most recently selected (db.queries.get_watchlist_symbols) - pass an explicit list to
    override, e.g. for tests or a one-off manual run."""
    init_db()
    scheduler = BlockingScheduler()
    trigger = CronTrigger(day_of_week="mon-fri", hour=hour, minute=minute, timezone="America/New_York")
    scheduler.add_job(run_once, trigger, kwargs={"strategy": strategy, "symbols": symbols})
    logger.info(
        "starting autotrader daily loop at %02d:%02d America/New_York, symbols=%s",
        hour, minute, symbols if symbols is not None else "from research watchlist",
    )
    scheduler.start()
