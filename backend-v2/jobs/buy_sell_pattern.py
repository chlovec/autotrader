"""Computes, per selected ticker, the set of buy/sell trades across a [start_date,
end_date] range of daily ohlc_bars that maximizes total profit, and stores them in the
buy_sell_patterns table (db/models.py's BuySellPattern) under a run name - one "buy" row
(price = the buy day's low) and one "sell" row (price = the sell day's high) per trade.

Trade rules:

- Buy at the buy day's low; sell at the sell day's high - profit = high[sell] - low[buy].
- A trade may buy and sell on the same day only if that day's close is above its low
  (the purchase price); otherwise the sell must come on a later day.
- Any number of trades per ticker, never overlapping: the next buy is on a day after
  the previous sell day.

Because buys and sells read different price series (low vs high), the classic "sum
every up-move" greedy doesn't apply - see max_profit_trades for the exact DP.

Each run's rows are labelled with a name - by default "<start_date>_<end_date>",
overridable on a manual run (see resolve_pattern_name). A name that already has rows
is a conflict: the run fails unless it was explicitly asked to replace them (see
app/main.py's trigger_job, which warns the dashboard before a manual run gets here).

A run is all-or-nothing: each ticker batch is written to a connection-private TEMP
staging table, and only once every batch has succeeded are the rows copied into
buy_sell_patterns (replacing the name's old rows, if asked) and committed in one short
transaction. An error or cancel part-way through leaves buy_sell_patterns untouched.
Staging in a TEMP table rather than holding one long transaction open on
buy_sell_patterns matters on sqlite: TEMP writes don't take the main database's single
write lock, so the dashboard's Pause/Cancel, progress updates, and other jobs can still
write while this runs.

Purely local - no massive.com call, reads bars already synced by the bars jobs.
"""

import datetime as dt
import itertools
import logging
from dataclasses import dataclass

from sqlalchemy import Column, Date, DateTime, Float, MetaData, String, Table, delete, distinct, insert, select
from sqlalchemy.orm import Session

from db.models import BuySellPattern, OhlcBar
from jobs.average_volume import _apply_ticker_filter
from jobs.control import JobControl, report_job_progress

logger = logging.getLogger("backend_v2.jobs.buy_sell_pattern")

DEFAULT_MULTIPLIER = 1
DEFAULT_TIMESPAN = "day"

# Default number of tickers whose bars are loaded, solved, and committed together -
# keeps memory bounded on an every-ticker run. Overridable per run from the dashboard
# (JobConfig.buy_sell_pattern_batch_size); capped at MAX_TICKER_BATCH_SIZE so the
# IN (...) list stays well under sqlite's variable limit.
TICKER_BATCH_SIZE = 500
MAX_TICKER_BATCH_SIZE = 5000


# Mirrors buy_sell_patterns' columns and primary key (so a duplicate row fails on the
# batch that produced it), minus the tickers FK - a TEMP table can't reference a table
# in the main database.
_STAGING = Table(
    "buy_sell_patterns_staging",
    MetaData(),
    Column("name", String, primary_key=True),
    Column("ticker", String, primary_key=True),
    Column("trade_date", Date, primary_key=True),
    Column("buy_sell", String, primary_key=True),
    Column("price", Float),
    Column("created_at", DateTime(timezone=False)),
    Column("updated_at", DateTime(timezone=False)),
    prefixes=["TEMPORARY"],
)


class PatternNameConflict(ValueError):
    pass


@dataclass(frozen=True)
class DailyBar:
    date: dt.date
    low: float
    high: float
    close: float


@dataclass(frozen=True)
class Trade:
    buy_date: dt.date
    buy_price: float
    sell_date: dt.date
    sell_price: float

    @property
    def profit(self) -> float:
        return self.sell_price - self.buy_price


def default_pattern_name(start_date: dt.date, end_date: dt.date) -> str:
    return f"{start_date.isoformat()}_{end_date.isoformat()}"


def resolve_pattern_name(name: str | None, start_date: dt.date, end_date: dt.date, trigger: str) -> str:
    """A manual run uses the caller's name when one is given; every auto run, and a
    manual run with a blank name, uses default_pattern_name."""
    if trigger == "manual" and name and name.strip():
        return name.strip()
    return default_pattern_name(start_date, end_date)


def validate_date_range(start_date: dt.date | None, end_date: dt.date | None) -> tuple[dt.date, dt.date]:
    if start_date is None or end_date is None:
        raise ValueError("buy-sell-pattern needs both a Start date and an End date")
    if start_date > end_date:
        raise ValueError("buy-sell-pattern's Start date must not be after End date")
    return start_date, end_date


def pattern_name_exists(session: Session, name: str) -> bool:
    return session.execute(select(BuySellPattern.name).where(BuySellPattern.name == name).limit(1)).first() is not None


def max_profit_trades(bars: list[DailyBar]) -> list[Trade]:
    """Exact maximum-total-profit set of non-overlapping trades over `bars` (sorted by
    date), under the module docstring's rules.

    DP over days, two states at the end of day t:
      cash[t] - best profit holding nothing
      hold[t] - best profit holding one share (bought on some day <= t, not yet sold)
    transitions:
      hold[t] = max(hold[t-1], cash[t-1] - low[t])                    buy today
      cash[t] = max(cash[t-1],
                    hold[t-1] + high[t],                              sell a prior buy
                    cash[t-1] + high[t] - low[t]  if close[t] > low[t])  same-day trade
    Buying from cash[t-1] (not cash[t]) is what keeps the next buy after the previous
    sell day. Ties keep the option with fewer trades. Each day's choices are recorded
    and walked backwards from cash[last] to recover the trades."""
    if not bars:
        return []
    cash, hold = 0.0, float("-inf")
    # Per day: cash choice ("carry" | "sell" | "same_day"), hold choice ("carry" | "buy").
    cash_choices: list[str] = []
    hold_choices: list[str] = []
    for bar in bars:
        buy = cash - bar.low
        new_hold, hold_choice = (buy, "buy") if buy > hold else (hold, "carry")

        new_cash, cash_choice = cash, "carry"
        if hold + bar.high > new_cash:
            new_cash, cash_choice = hold + bar.high, "sell"
        if bar.close > bar.low and cash + bar.high - bar.low > new_cash:
            new_cash, cash_choice = cash + bar.high - bar.low, "same_day"

        cash, hold = new_cash, new_hold
        cash_choices.append(cash_choice)
        hold_choices.append(hold_choice)

    trades: list[Trade] = []
    state = "cash"
    sell_index: int | None = None
    for t in range(len(bars) - 1, -1, -1):
        if state == "cash":
            choice = cash_choices[t]
            if choice == "sell":
                sell_index, state = t, "hold"
            elif choice == "same_day":
                bar = bars[t]
                trades.append(Trade(bar.date, bar.low, bar.date, bar.high))
        elif hold_choices[t] == "buy":
            assert sell_index is not None
            buy_bar, sell_bar = bars[t], bars[sell_index]
            trades.append(Trade(buy_bar.date, buy_bar.low, sell_bar.date, sell_bar.high))
            sell_index, state = None, "cash"
    trades.reverse()
    return trades


def _select_tickers(
    session: Session,
    start: dt.datetime,
    end: dt.datetime,
    ticker_types: list[str] | None,
    tickers: list[str] | None,
) -> list[str]:
    query = select(distinct(OhlcBar.ticker)).where(
        OhlcBar.multiplier == DEFAULT_MULTIPLIER,
        OhlcBar.timespan == DEFAULT_TIMESPAN,
        OhlcBar.timestamp >= start,
        OhlcBar.timestamp <= end,
    )
    query = _apply_ticker_filter(query, ticker_types, tickers)
    return sorted(session.execute(query).scalars())


def _load_bars(session: Session, tickers: list[str], start: dt.datetime, end: dt.datetime) -> dict[str, list[DailyBar]]:
    rows = session.execute(
        select(OhlcBar.ticker, OhlcBar.timestamp, OhlcBar.low, OhlcBar.high, OhlcBar.close)
        .where(
            OhlcBar.ticker.in_(tickers),
            OhlcBar.multiplier == DEFAULT_MULTIPLIER,
            OhlcBar.timespan == DEFAULT_TIMESPAN,
            OhlcBar.timestamp >= start,
            OhlcBar.timestamp <= end,
            # A bar missing a price, or with a non-positive one, can't be traded on.
            OhlcBar.low > 0,
            OhlcBar.high > 0,
            OhlcBar.close > 0,
        )
        .order_by(OhlcBar.ticker, OhlcBar.timestamp)
    ).all()
    return {
        ticker: [DailyBar(timestamp.date(), low, high, close) for _, timestamp, low, high, close in group]
        for ticker, group in itertools.groupby(rows, key=lambda row: row[0])
    }


@dataclass
class BuySellPatternResult:
    name: str
    tickers: int
    trades: int
    replaced: bool


def compute_buy_sell_pattern(
    session: Session,
    start_date: dt.date,
    end_date: dt.date,
    name: str,
    ticker_types: list[str] | None = None,
    tickers: list[str] | None = None,
    replace: bool = False,
    batch_size: int = TICKER_BATCH_SIZE,
    control: JobControl | None = None,
    run_id: int | None = None,
) -> BuySellPatternResult:
    """Raises PatternNameConflict if `name` already has rows and `replace` is False.
    With `replace`, that name's existing rows are deleted (every ticker, not just this
    run's selection) in the same final transaction that inserts the new ones.

    All-or-nothing - see the module docstring: batches are staged in a TEMP table on a
    connection of their own and only copied into buy_sell_patterns and committed once
    every batch has succeeded; on any error (or cancel) nothing is written.

    Blocking - run it via asyncio.to_thread. Tickers are processed `batch_size` at a
    time (1..MAX_TICKER_BATCH_SIZE) - smaller uses less memory per batch, larger means
    fewer queries. `control` is checked between ticker batches; progress counts
    tickers."""
    start_date, end_date = validate_date_range(start_date, end_date)
    if not 1 <= batch_size <= MAX_TICKER_BATCH_SIZE:
        raise ValueError(f"buy-sell-pattern's batch size must be between 1 and {MAX_TICKER_BATCH_SIZE}")
    existed = pattern_name_exists(session, name)
    if existed and not replace:
        raise PatternNameConflict(
            f"buy_sell_pattern already has rows named {name!r} - pick another name or choose to replace them"
        )

    start = dt.datetime.combine(start_date, dt.time.min)
    end = dt.datetime.combine(end_date, dt.time.max)
    selected = _select_tickers(session, start, end, ticker_types, tickers)
    report_job_progress(session, run_id, 0, len(selected), force=True)

    now = dt.datetime.utcnow()
    trade_count = 0
    # A dedicated connection, held for the whole run, because a TEMP table only exists
    # on the connection that created it - `session` may hand its connection back to the
    # pool on every report_job_progress commit.
    with session.get_bind().connect() as staging_conn:
        _STAGING.drop(staging_conn, checkfirst=True)
        _STAGING.create(staging_conn)
        staging_conn.commit()
        try:
            for offset in range(0, len(selected), batch_size):
                if control is not None:
                    control.checkpoint_sync()
                batch = selected[offset : offset + batch_size]
                rows = []
                for ticker, bars in _load_bars(session, batch, start, end).items():
                    for trade in max_profit_trades(bars):
                        for buy_sell, trade_date, price in (
                            ("buy", trade.buy_date, trade.buy_price),
                            ("sell", trade.sell_date, trade.sell_price),
                        ):
                            rows.append(
                                {
                                    "name": name,
                                    "ticker": ticker,
                                    "trade_date": trade_date,
                                    "buy_sell": buy_sell,
                                    "price": price,
                                    "created_at": now,
                                    "updated_at": now,
                                }
                            )
                        trade_count += 1
                if rows:
                    staging_conn.execute(insert(_STAGING), rows)
                # Commits only the TEMP table - nothing in buy_sell_patterns yet.
                staging_conn.commit()
                report_job_progress(session, run_id, offset + len(batch), len(selected), force=True)

            # End `session`'s read transaction first: under WAL its snapshot predates
            # the commit below, and a later write through it (e.g. engine.py recording
            # the run's result) would otherwise fail on a stale snapshot.
            session.commit()
            if existed:
                staging_conn.execute(delete(BuySellPattern).where(BuySellPattern.name == name))
            staging_conn.execute(
                insert(BuySellPattern.__table__).from_select(
                    [column.name for column in _STAGING.columns], select(_STAGING)
                )
            )
            staging_conn.commit()
        except BaseException:
            staging_conn.rollback()
            raise
        finally:
            _STAGING.drop(staging_conn, checkfirst=True)
            staging_conn.commit()

    logger.info(
        "buy-sell-pattern %r: %d trade(s) across %d ticker(s), %s to %s",
        name,
        trade_count,
        len(selected),
        start_date,
        end_date,
    )
    return BuySellPatternResult(name=name, tickers=len(selected), trades=trade_count, replaced=existed)
