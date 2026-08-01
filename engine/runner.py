import logging

import pandas as pd
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from db.models import EquitySnapshot, Signal, SignalAction
from db.session import get_session, init_db
from engine.clients import make_data_client, make_trading_client
from engine.config import load_config
from engine.execution import ExecutionEngine
from engine.risk import RiskManager
from engine.strategy import Strategy

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("autotrader.runner")


def _extract_symbol_bars(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """alpaca-py's BarSet.df is multi-indexed by (symbol, timestamp) even for one symbol."""
    if isinstance(df.index, pd.MultiIndex):
        df = df.xs(symbol, level=0)
    return df.sort_index()


def _current_position_qty(trading_client, symbol: str) -> float:
    try:
        return abs(float(trading_client.get_open_position(symbol).qty))
    except Exception:
        return 0.0


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
            request = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Day, limit=100)
            bars = _extract_symbol_bars(data_client.get_stock_bars(request).df, symbol)

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
                qty = _current_position_qty(trading_client, symbol)

            approved, why = risk.approve(symbol, action, order_value_usd)
            if not approved:
                logger.info("signal for %s not executed: %s", symbol, why)
                continue

            if action == SignalAction.sell and qty == 0:
                logger.info("sell signal for %s but no open position, skipping", symbol)
                continue

            trade = execution.submit_market_order(symbol, action, qty, signal_id=signal.id)
            logger.info("submitted %s order for %s qty=%s", action.value, symbol, trade.qty)


def main(symbols: list[str], strategy: Strategy, hour: int = 9, minute: int = 35) -> None:
    """Runs once per weekday at hour:minute America/New_York, using the latest completed
    daily bars. Default of 9:35 gives the market a few minutes to open before trading."""
    init_db()
    scheduler = BlockingScheduler()
    trigger = CronTrigger(day_of_week="mon-fri", hour=hour, minute=minute, timezone="America/New_York")
    scheduler.add_job(run_once, trigger, args=[symbols, strategy])
    logger.info("starting autotrader daily loop at %02d:%02d America/New_York, symbols=%s", hour, minute, symbols)
    scheduler.start()
