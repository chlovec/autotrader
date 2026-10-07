"""Computes, per selected ticker, the set of buy/sell trades across a [start_date,
end_date] range of daily bars - either bound optional, a missing one leaving that side
of the range open that maximizes total profit, and stores them in the
buy_sell_patterns table (db/models.py's BuySellPattern) under a run name - recorded once
in buy_sell_pattern_names (BuySellPatternName), which each trade row points to by
pattern_id - one "buy" row
and one "sell" row per trade, each priced at the open or close it traded at and stamped
with that price point's datetime: the bar's date at MARKET_OPEN for an open, at
MARKET_CLOSE (21:00 UTC, stored naive) for a close - daily bars carry no intraday
time, same reasoning as jobs/predict_market_state.py's ENTRY_TIME/EXIT_TIME.

Bars are read from the tickers_daily_bars_min_60_days_from_latest view (see
db/session.py) - so only tickers with a bar on the latest trading day and a full 60
most recent daily bars are considered - filtered by the run's Start/End date and
ticker type/ticker selection.

Trade rules - each day contributes two price points, its open then its close, giving
one sequence open[0], close[0], open[1], close[1], ...:

- A trade buys at any price point and sells at a later, higher one - so a buy can be
  at a day's open or close, and so can a sell. profit = sell price - buy price.
- A trade may buy and sell on the same day (buy at the open, sell at the close) when
  the close is above the open.
- Any number of trades per ticker, never overlapping: the next buy is at a price point
  after the previous sell - which can be the same day (sell at the open, buy back at
  the close).

Under these rules every up-move between consecutive price points can be captured, so
the classic valley/peak greedy is exact - see max_profit_trades_unlimited.

Each run's rows are labelled with a name - by default "<start_date>_<end_date>", with
"earliest"/"latest" standing in for a missing Start/End date - overridable on a manual run (see resolve_pattern_name). A name that already has rows
is a conflict: the run fails unless it was explicitly asked to replace them (see
app/main.py's trigger_job, which warns the dashboard before a manual run gets here).

A run is all-or-nothing: each ticker batch is written to a connection-private TEMP
staging table, and only once every batch has succeeded are the rows copied into
buy_sell_patterns (replacing the name's old rows, if asked) and committed in one short
transaction, together with the name's buy_sell_pattern_names row. An error or cancel part-way through leaves buy_sell_patterns untouched.
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

from typing import NamedTuple

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    MetaData,
    String,
    Table,
    delete,
    distinct,
    func,
    insert,
    literal,
    select,
    update,
)
from sqlalchemy.orm import Session

from db.models import BuySellPattern, BuySellPatternName, Ticker
from jobs.control import JobControl, report_job_progress

logger = logging.getLogger("backend_v2.jobs.buy_sell_pattern")

# The columns of db/session.py's tickers_daily_bars_min_60_days_from_latest view
# this job reads - already daily bars only (multiplier 1, timespan "day"). Not part of
# Base.metadata, so create_all never tries to create it as a table.
_TICKERS_DAILY_BARS_MIN_60_VIEW = Table(
    "tickers_daily_bars_min_60_days_from_latest",
    MetaData(),
    Column("ticker", String),
    Column("timestamp", DateTime(timezone=False)),
    Column("open", Float),
    Column("close", Float),
)

# Default number of tickers whose bars are loaded, solved, and committed together -
# keeps memory bounded on an every-ticker run. Overridable per run from the dashboard
# (JobConfig.buy_sell_pattern_batch_size); capped at MAX_TICKER_BATCH_SIZE so the
# IN (...) list stays well under sqlite's variable limit.
TICKER_BATCH_SIZE = 500
MAX_TICKER_BATCH_SIZE = 5000

# The time of day a trade at a bar's open/close is stamped with (see the module
# docstring). MARKET_CLOSE is 21:00 UTC on the bar's own date.
MARKET_OPEN = dt.time(9, 30)
MARKET_CLOSE = dt.time(21, 0)


# Mirrors buy_sell_patterns' columns and primary key (so a duplicate row fails on the
# batch that produced it), minus pattern_id - one run writes one pattern, whose id is
# only known in the final transaction - and minus the FKs, since a TEMP table can't
# reference a table in the main database.
_STAGING = Table(
    "buy_sell_patterns_staging",
    MetaData(),
    Column("ticker", String, primary_key=True),
    Column("trade_datetime", DateTime(timezone=False), primary_key=True),
    Column("buy_sell", String, primary_key=True),
    Column("price", Float),
    prefixes=["TEMPORARY"],
)


class PatternNameConflict(ValueError):
    pass


@dataclass(frozen=True)
class DailyBar:
    date: dt.date
    open: float
    close: float


@dataclass(frozen=True)
class Trade:
    buy_datetime: dt.datetime
    buy_price: float
    sell_datetime: dt.datetime
    sell_price: float

    @property
    def profit(self) -> float:
        return self.sell_price - self.buy_price


class PricePoint(NamedTuple):
    at: dt.datetime
    price: float


def open_datetime(date: dt.date) -> dt.datetime:
    return dt.datetime.combine(date, MARKET_OPEN)


def close_datetime(date: dt.date) -> dt.datetime:
    return dt.datetime.combine(date, MARKET_CLOSE)


def default_pattern_name(start_date: dt.date | None, end_date: dt.date | None) -> str:
    start = start_date.isoformat() if start_date else "earliest"
    end = end_date.isoformat() if end_date else "latest"
    return f"{start}_{end}"


def resolve_pattern_name(
    name: str | None, start_date: dt.date | None, end_date: dt.date | None, trigger: str
) -> str:
    """A manual run uses the caller's name when one is given; every auto run, and a
    manual run with a blank name, uses default_pattern_name."""
    if trigger == "manual" and name and name.strip():
        return name.strip()
    return default_pattern_name(start_date, end_date)


def validate_date_range(
    start_date: dt.date | None, end_date: dt.date | None
) -> tuple[dt.date | None, dt.date | None]:
    """Either date may be None - that side of the range is then unbounded."""
    if start_date is not None and end_date is not None and start_date > end_date:
        raise ValueError("buy-sell-pattern's Start date must not be after End date")
    return start_date, end_date


def pattern_name_exists(session: Session, name: str) -> bool:
    return (
        session.execute(
            select(BuySellPatternName.id).where(BuySellPatternName.name == name)
        ).first()
        is not None
    )


def max_profit_trades_unlimited(bars: list[DailyBar]) -> list[Trade]:
    """Calculates all profitable trades to maximize cumulative profit from daily bar data.

    This algorithm uses a two-pointer peak-and-valley strategy across a flattened
    chronological sequence of open and close prices. It supports unlimited
    non-overlapping trades (equivalent to LeetCode 122), entering positions at
    local minimums (valleys) and exiting at local maximums (peaks).

    Algorithm Breakdown:
        1. **Chronological Ordering & Flattening**:
           Sorts bars by date and flattens daily bars into discrete price points
           ordered as [Open_0, Close_0, Open_1, Close_1, ...].
        2. **Valley Detection (Buy Entry)**:
           Advances pointer `i` through non-increasing price segments to locate
           the trough before an upward movement begins.
        3. **Peak Detection (Sell Exit)**:
           Advances pointer `i` through non-decreasing price segments to ride the
           rally up to its crest.
        4. **Validation & Logging**:
           Appends the executed trade to the result list only if `sell_price > buy_price`.
           The outer loop automatically begins looking for the next entry immediately
           from the exit position.

    Complexity:
        - Time Complexity: O(N log N) dominated by the initial date sort, where N is
          the number of daily bars. The peak-valley traversal itself is strictly O(N)
          as each point is visited at most twice.
        - Space Complexity: O(N) to store flattened price points and generated trades.

    Args:
        bars: A list of `DailyBar` records containing date, open, and close prices.

    Returns:
        A list of `Trade` instances representing optimal entries and exits.
        Returns an empty list if input has fewer than 2 price points or no upward trend.

    Example:
        >>> bars = [
        ...     DailyBar(date=dt.date(2026, 1, 1), open=10.0, close=15.0),
        ...     DailyBar(date=dt.date(2026, 1, 2), open=12.0, close=20.0),
        ... ]
        >>> trades = max_profit_trades_unlimited(bars)
        >>> len(trades)
        2
        >>> trades[0]
        Trade(buy_datetime=datetime.datetime(2026, 1, 1, 9, 30), buy_price=10.0, sell_datetime=datetime.datetime(2026, 1, 1, 21, 0), sell_price=15.0)
    """
    if not bars:
        return []

    sorted_bars = sorted(bars, key=lambda b: b.date)

    # Flatten open and close into discrete sequential price points
    points: list[PricePoint] = []
    for b in sorted_bars:
        points.extend(
            (
                PricePoint(at=open_datetime(b.date), price=b.open),
                PricePoint(at=close_datetime(b.date), price=b.close),
            )
        )
    trades: list[Trade] = []
    n = len(points)
    i = 0

    while i < n - 1:
        # Find local minimum (valley)
        while i < n - 1 and points[i].price >= points[i + 1].price:
            i += 1
        if i >= n - 1:
            break
        buy_pt = points[i]

        # Find local maximum (peak)
        while i < n - 1 and points[i].price <= points[i + 1].price:
            i += 1
        sell_pt = points[i]

        if sell_pt.price > buy_pt.price:
            trades.append(
                Trade(
                    buy_datetime=buy_pt.at,
                    buy_price=buy_pt.price,
                    sell_datetime=sell_pt.at,
                    sell_price=sell_pt.price,
                )
            )

    return trades


def _apply_ticker_filter(
    query, ticker_types: list[str] | None, tickers: list[str] | None
):
    """Same shape as jobs/average_volume.py's _apply_ticker_filter, against the view's
    ticker column instead of OhlcBar.ticker."""
    if tickers and ticker_types:
        raise ValueError("specify tickers or ticker_types, not both")
    view = _TICKERS_DAILY_BARS_MIN_60_VIEW.c
    if tickers:
        return query.where(view.ticker.in_(tickers))
    if ticker_types:
        return query.where(
            view.ticker.in_(select(Ticker.ticker).where(Ticker.type.in_(ticker_types)))
        )
    return query


def _date_range_conditions(start: dt.datetime | None, end: dt.datetime | None) -> list:
    """The view's timestamp bounds for whichever of start/end is set."""
    view = _TICKERS_DAILY_BARS_MIN_60_VIEW.c
    conditions = []
    if start is not None:
        conditions.append(view.timestamp >= start)
    if end is not None:
        conditions.append(view.timestamp <= end)
    return conditions


def _select_tickers(
    session: Session,
    start: dt.datetime | None,
    end: dt.datetime | None,
    ticker_types: list[str] | None,
    tickers: list[str] | None,
) -> list[str]:
    view = _TICKERS_DAILY_BARS_MIN_60_VIEW.c
    query = select(distinct(view.ticker)).where(*_date_range_conditions(start, end))
    query = _apply_ticker_filter(query, ticker_types, tickers)
    return sorted(session.execute(query).scalars())


def _load_bars(
    session: Session,
    tickers: list[str],
    start: dt.datetime | None,
    end: dt.datetime | None,
) -> dict[str, list[DailyBar]]:
    view = _TICKERS_DAILY_BARS_MIN_60_VIEW.c
    rows = session.execute(
        select(view.ticker, view.timestamp, view.open, view.close)
        .where(
            view.ticker.in_(tickers),
            *_date_range_conditions(start, end)
        )
        .order_by(view.ticker, view.timestamp)
    ).all()
    return {
        ticker: [
            DailyBar(timestamp.date(), open_, close)
            for _, timestamp, open_, close in group
        ]
        for ticker, group in itertools.groupby(rows, key=lambda row: row[0])
    }


def _publish_pattern(staging_conn, name: str, now: dt.datetime) -> None:
    """Swaps the staged trades in as pattern `name`, on staging_conn's open transaction:
    clears the name's old trades (if any), then inserts or updates its
    buy_sell_pattern_names row - keeping its id and created_at on a replace - with the
    staged span, and copies the staged rows under that id. A run that staged no trades
    deletes the name's row instead, so a name never exists without trades (as before
    the name moved out of buy_sell_patterns, when "exists" meant "has rows")."""
    names = BuySellPatternName.__table__
    pattern_id = staging_conn.execute(
        select(names.c.id).where(names.c.name == name)
    ).scalar()
    if pattern_id is not None:
        staging_conn.execute(
            delete(BuySellPattern).where(BuySellPattern.pattern_id == pattern_id)
        )
    first, last = staging_conn.execute(
        select(func.min(_STAGING.c.trade_datetime), func.max(_STAGING.c.trade_datetime))
    ).one()
    if first is None:
        if pattern_id is not None:
            staging_conn.execute(delete(names).where(names.c.id == pattern_id))
        return
    if pattern_id is None:
        pattern_id = staging_conn.execute(
            insert(names).values(
                name=name,
                first_trade_datetime=first,
                last_trade_datetime=last,
                created_at=now,
                updated_at=now,
            )
        ).inserted_primary_key[0]
    else:
        staging_conn.execute(
            update(names)
            .where(names.c.id == pattern_id)
            .values(first_trade_datetime=first, last_trade_datetime=last, updated_at=now)
        )
    staging_conn.execute(
        insert(BuySellPattern.__table__).from_select(
            ["pattern_id", *(column.name for column in _STAGING.columns)],
            select(literal(pattern_id), *_STAGING.columns),
        )
    )


@dataclass
class BuySellPatternResult:
    name: str
    tickers: int
    trades: int
    replaced: bool


def compute_buy_sell_pattern(
    session: Session,
    start_date: dt.date | None,
    end_date: dt.date | None,
    name: str,
    ticker_types: list[str] | None = None,
    tickers: list[str] | None = None,
    replace: bool = False,
    batch_size: int = TICKER_BATCH_SIZE,
    control: JobControl | None = None,
    run_id: int | None = None,
) -> BuySellPatternResult:
    """A None start_date/end_date leaves that side of the range open - every bar in
    the view from the earliest / through the latest.

    Raises PatternNameConflict if `name` already has rows and `replace` is False.
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
        raise ValueError(
            f"buy-sell-pattern's batch size must be between 1 and {MAX_TICKER_BATCH_SIZE}"
        )
    existed = pattern_name_exists(session, name)
    if existed and not replace:
        raise PatternNameConflict(
            f"buy_sell_pattern already has rows named {name!r} - pick another name or choose to replace them"
        )

    start = dt.datetime.combine(start_date, dt.time.min) if start_date else None
    end = dt.datetime.combine(end_date, dt.time.max) if end_date else None
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
                    for trade in max_profit_trades_unlimited(bars):
                        rows.extend(
                            {
                                "ticker": ticker,
                                "trade_datetime": trade_datetime,
                                "buy_sell": buy_sell,
                                "price": price,
                            }
                            for buy_sell, trade_datetime, price in (
                                ("buy", trade.buy_datetime, trade.buy_price),
                                ("sell", trade.sell_datetime, trade.sell_price),
                            )
                        )
                        trade_count += 1
                if rows:
                    staging_conn.execute(insert(_STAGING), rows)
                # Commits only the TEMP table - nothing in buy_sell_patterns yet.
                staging_conn.commit()
                report_job_progress(
                    session, run_id, offset + len(batch), len(selected), force=True
                )

            # End `session`'s read transaction first: under WAL its snapshot predates
            # the commit below, and a later write through it (e.g. engine.py recording
            # the run's result) would otherwise fail on a stale snapshot.
            session.commit()
            _publish_pattern(staging_conn, name, now)
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
        start_date or "earliest",
        end_date or "latest",
    )
    return BuySellPatternResult(
        name=name, tickers=len(selected), trades=trade_count, replaced=existed
    )
