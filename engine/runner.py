import logging

from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from apscheduler.schedulers.blocking import BlockingScheduler

from db.models import EquitySnapshot, Signal
from db.session import get_session, init_db
from engine.clients import make_data_client, make_trading_client
from engine.config import load_config
from engine.execution import ExecutionEngine
from engine.risk import RiskManager
from engine.strategy import Strategy

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("autotrader.runner")


def run_once(symbols: list[str], strategy: Strategy) -> None:
    config = load_config()
    trading_client = make_trading_client(config)
    data_client = make_data_client(config)

    clock = trading_client.get_clock()
    if not clock.is_open:
        logger.info("market closed, skipping cycle")
        return

    with get_session() as session:
        risk = RiskManager(config, session)
        execution = ExecutionEngine(trading_client, session)

        account = trading_client.get_account()
        session.add(EquitySnapshot(equity=float(account.equity), cash=float(account.cash), buying_power=float(account.buying_power)))
        session.commit()

        for symbol in symbols:
            request = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Minute, limit=200)
            bars = data_client.get_stock_bars(request).df

            action, reason = strategy.generate_signal(symbol, bars)
            signal = Signal(symbol=symbol, strategy_name=strategy.name, action=action, reason=reason)
            session.add(signal)
            session.commit()

            order_value_usd = config.max_position_size_usd  # Phase 1 defines real position sizing
            approved, why = risk.approve(symbol, action, order_value_usd)
            if not approved:
                logger.info("signal for %s not executed: %s", symbol, why)
                continue

            qty = round(order_value_usd / float(bars["close"].iloc[-1]), 4)
            trade = execution.submit_market_order(symbol, action, qty, signal_id=signal.id)
            logger.info("submitted %s order for %s qty=%s", action.value, symbol, trade.qty)


def main(symbols: list[str], strategy: Strategy, interval_seconds: int = 60) -> None:
    init_db()
    scheduler = BlockingScheduler()
    scheduler.add_job(run_once, "interval", seconds=interval_seconds, args=[symbols, strategy])
    logger.info("starting autotrader loop, interval=%ss, symbols=%s", interval_seconds, symbols)
    scheduler.start()
