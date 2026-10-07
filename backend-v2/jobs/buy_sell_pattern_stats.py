"""Summarizes buy_sell_patterns into buy_sell_pattern_stats: per (name, ticker), the
trade count, total and average profit as a percentage of each trade's buy price,
first and last trade datetime, and the average/min/max/median buy and sell price
across that ticker's trades stored under that pattern name.

Trades are rebuilt the same way app/main.py's buy_sell_pattern_report does it -
pairing each buy row with the next sell row in trade_datetime order.
jobs/buy_sell_pattern.py only ever stores complete trades, so in practice nothing is
left unpaired; a stray unpaired row is skipped. A trade with a buy price of 0 (a bad
bar) is skipped too, since its profit percentage is undefined - a (name, ticker) left
with no trades gets no row.

Every run rebuilds the whole table in one transaction, so it always matches
buy_sell_patterns as of that run - including names since replaced or removed. Rows
are read in primary-key order, one (pattern_id, ticker) group at a time, so memory
holds one ticker's trades plus the finished summaries, not the whole table; each
pattern_id's name comes from buy_sell_pattern_names.

Purely local - no massive.com call."""

import datetime as dt
import itertools
import logging
import statistics

from sqlalchemy import delete, func, insert, select
from sqlalchemy.orm import Session

from db.models import BuySellPattern, BuySellPatternName, BuySellPatternStat
from jobs.control import JobControl, report_job_progress

logger = logging.getLogger(__name__)

# Rows fetched from the database at a time while streaming buy_sell_patterns.
_FETCH_SIZE = 10_000
# Stats rows per INSERT statement - keeps each well under sqlite's variable limit.
_INSERT_CHUNK = 500


# (buy_datetime, buy_price, sell_datetime, sell_price)
_PairedTrade = tuple[dt.datetime, float, dt.datetime, float]


def _pair_trades(rows) -> list[_PairedTrade]:
    """One entry per trade, from rows ordered by trade_datetime."""
    trades = []
    pending_buy: tuple[dt.datetime, float] | None = None
    for _, _, trade_datetime, buy_sell, price in rows:
        if buy_sell == "buy":
            pending_buy = (trade_datetime, price)
        elif pending_buy is not None:
            trades.append((*pending_buy, trade_datetime, price))
            pending_buy = None
    return trades


def _summarize(name: str, ticker: str, trades: list[_PairedTrade], now: dt.datetime) -> dict:
    buys = [buy for _, buy, _, _ in trades]
    sells = [sell for _, _, _, sell in trades]
    # Each trade's profit as a percentage of its buy price - total_profit sums them.
    profits = [(sell - buy) / buy * 100 for buy, sell in zip(buys, sells)]
    return {
        "name": name,
        "ticker": ticker,
        "trades": len(trades),
        "total_profit": sum(profits),
        "first_trade_datetime": trades[0][0],
        "last_trade_datetime": trades[-1][2],
        "avg_buy_price": statistics.fmean(buys),
        "avg_sell_price": statistics.fmean(sells),
        "avg_profit": statistics.fmean(profits),
        "buy_price_min": min(buys),
        "buy_price_max": max(buys),
        "sell_price_min": min(sells),
        "sell_price_max": max(sells),
        "buy_price_median": statistics.median(buys),
        "sell_price_median": statistics.median(sells),
        "computed_at": now,
    }


def compute_buy_sell_pattern_stats(
    session: Session,
    run_id: int | None = None,
    control: JobControl | None = None,
) -> int:
    """Rebuilds buy_sell_pattern_stats from buy_sell_patterns and returns the number of
    (name, ticker) rows written. Blocking - run it via asyncio.to_thread. `control` is
    checked between groups; progress counts (name, ticker) groups. A cancel or error
    leaves the existing stats untouched."""
    groups = select(BuySellPattern.pattern_id, BuySellPattern.ticker).distinct().subquery()
    total = session.execute(select(func.count()).select_from(groups)).scalar_one()
    names = dict(session.execute(select(BuySellPatternName.id, BuySellPatternName.name)).all())
    report_job_progress(session, run_id, 0, total, force=True)

    now = dt.datetime.utcnow()
    stats: list[dict] = []
    skipped_zero_buy = 0
    # A dedicated connection for the streaming read - report_job_progress commits
    # `session`, which may hand its connection back to the pool mid-iteration.
    with session.get_bind().connect() as reader:
        rows = reader.execution_options(yield_per=_FETCH_SIZE).execute(
            select(
                BuySellPattern.pattern_id,
                BuySellPattern.ticker,
                BuySellPattern.trade_datetime,
                BuySellPattern.buy_sell,
                BuySellPattern.price,
            ).order_by(
                BuySellPattern.pattern_id,
                BuySellPattern.ticker,
                BuySellPattern.trade_datetime,
                BuySellPattern.buy_sell,
            )
        )
        for done, ((pattern_id, ticker), group) in enumerate(
            itertools.groupby(rows, key=lambda row: (row[0], row[1])), start=1
        ):
            if control is not None:
                control.checkpoint_sync()
            paired = _pair_trades(group)
            trades = [trade for trade in paired if trade[1] != 0]
            skipped_zero_buy += len(paired) - len(trades)
            if trades:
                stats.append(_summarize(names[pattern_id], ticker, trades, now))
            report_job_progress(session, run_id, done, total)

    # End `session`'s read transaction first - same stale-WAL-snapshot reasoning as
    # jobs/buy_sell_pattern.py's final commit.
    session.commit()
    session.execute(delete(BuySellPatternStat))
    for offset in range(0, len(stats), _INSERT_CHUNK):
        session.execute(insert(BuySellPatternStat), stats[offset : offset + _INSERT_CHUNK])
    session.commit()
    report_job_progress(session, run_id, total, total, force=True)

    if skipped_zero_buy:
        logger.warning("buy-sell-pattern-stats: skipped %d trade(s) with a buy price of 0", skipped_zero_buy)
    logger.info("buy-sell-pattern-stats: %d (name, ticker) row(s) written", len(stats))
    return len(stats)
