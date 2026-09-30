"""Syncs GET /v2/aggs/grouped/locale/us/market/stocks/{date} ("Daily Market Summary")
into ohlc_bars - one request returns every US stock ticker's daily bar for that single
date, so a date range costs one request per weekday instead of one per ticker (see
jobs/sync_bars.py's per-ticker /v2/aggs/ticker/... path, which the ohlc-data-update job
uses: a 1-week range there is ~5,000+ requests, here it's 5).

Only daily (multiplier 1, timespan "day") bars exist on this endpoint - rows land in
ohlc_bars exactly where sync_bars.py's default daily bars do, so both paths overwrite
each other's rows for the same (ticker, day) rather than duplicating them.

The endpoint returns the whole market, including tickers the tickers table doesn't
have (ohlc_bars.ticker is a foreign key to it), so results are filtered down to the
selection: the saved Tickers or Ticker types, or every non-forex ticker in the tickers
table if neither is set - same rule as sync_bars.py's _resolve_tickers.

Weekends are skipped without a request. A market holiday just returns no results, so
it costs one request and stores nothing. Days whose request fails are retried at the
end of the run with exponential backoff - see sync_grouped_daily.
"""

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from data.client import DataClient
from db.models import OhlcBar
from jobs.control import JobControl, report_job_progress
from jobs.sync_bars import _BAR_FIELD_MAP, DEFAULT_MULTIPLIER, DEFAULT_TIMESPAN, _pcnt_increase, _resolve_tickers

logger = logging.getLogger("backend_v2.jobs.sync_grouped_daily")

# Blank End date = today (UTC); blank Start date = this many days before End date - a
# week, enough for a daily auto run to also backfill anything a missed day left behind.
DEFAULT_LOOKBACK_DAYS = 7
# One response is the whole market (~10k+ rows), much larger than a per-ticker page -
# DataClient's 10s default is too tight for it.
REQUEST_TIMEOUT_SECONDS = 60.0

# Failed days are retried after the main pass, up to MAX_RETRIES rounds, waiting
# RETRY_BASE_DELAY_SECONDS before the first and doubling each round (1, 2, 4 minutes).
MAX_RETRIES = 3
RETRY_BASE_DELAY_SECONDS = 60.0

_UPSERT_COLUMNS = ["open", "high", "low", "close", "volume", "vwap", "transactions", "pcnt_increase"]


def _default_client() -> DataClient:
    return DataClient(timeout=REQUEST_TIMEOUT_SECONDS)


def resolve_date_range(
    start_date: dt.date | None, end_date: dt.date | None, today: dt.date | None = None
) -> tuple[dt.date, dt.date]:
    """A None end date is today (UTC); a None start date is DEFAULT_LOOKBACK_DAYS before
    the (resolved) end date."""
    today = today if today is not None else dt.datetime.now(dt.timezone.utc).date()
    end_date = end_date if end_date is not None else today
    start_date = start_date if start_date is not None else end_date - dt.timedelta(days=DEFAULT_LOOKBACK_DAYS)
    if start_date > end_date:
        raise ValueError("sync-grouped-daily's Start date must not be after End date")
    return start_date, end_date


def weekdays(start_date: dt.date, end_date: dt.date) -> list[dt.date]:
    """Every Monday-Friday in [start_date, end_date] - calendar-naive about market
    holidays, same caveat as sync_bars.py's _last_trading_day_on_or_before."""
    days = []
    day = start_date
    while day <= end_date:
        if day.weekday() < 5:
            days.append(day)
        day += dt.timedelta(days=1)
    return days


def _bar_row(result: dict[str, Any]) -> dict[str, Any]:
    row: dict[str, Any] = {
        "ticker": result["T"],
        "multiplier": DEFAULT_MULTIPLIER,
        "timespan": DEFAULT_TIMESPAN,
        "timestamp": dt.datetime.fromtimestamp(result["t"] / 1000, tz=dt.timezone.utc).replace(tzinfo=None),
    }
    for source, field in _BAR_FIELD_MAP.items():
        row[field] = result.get(source)
    row["pcnt_increase"] = _pcnt_increase(row["open"], row["close"])
    return row


def _upsert_bars(session: Session, rows: list[dict[str, Any]]) -> None:
    """One executemany for the whole day rather than sync_bars.py's one statement per
    bar - a day here is ~10k rows, not a single ticker's handful."""
    if not rows:
        return
    stmt = sqlite_insert(OhlcBar)
    stmt = stmt.on_conflict_do_update(
        index_elements=[OhlcBar.ticker, OhlcBar.multiplier, OhlcBar.timespan, OhlcBar.timestamp],
        set_={column: stmt.excluded[column] for column in _UPSERT_COLUMNS},
    )
    session.execute(stmt, rows)


async def _fetch_day(client: DataClient, day: dt.date) -> list[dict[str, Any]]:
    payload = await client.get(
        f"/v2/aggs/grouped/locale/us/market/stocks/{day.isoformat()}",
        params={"adjusted": "true", "include_otc": "true"},
    )
    return payload.get("results") or []


@dataclass
class GroupedDailyResult:
    # Bars stored per day that succeeded (on the main pass or a retry).
    results: dict[dt.date, int]
    # Days still failing after the last retry round.
    failed: list[dt.date]
    # Retry rounds actually run (0 if the main pass had no failures).
    retry_rounds: int


async def _wait(seconds: float, control: JobControl | None) -> None:
    """Sleeps in 1s slices so a cancel lands within a second rather than after the whole
    retry delay; a pause holds the wait (checkpoint_async blocks while paused)."""
    deadline = asyncio.get_running_loop().time() + seconds
    while (remaining := deadline - asyncio.get_running_loop().time()) > 0:
        if control is not None:
            await control.checkpoint_async()
        await asyncio.sleep(min(1.0, remaining))


def sync_grouped_daily(
    session: Session,
    start_date: dt.date,
    end_date: dt.date,
    ticker_types: list[str] | None = None,
    tickers: list[str] | None = None,
    client_factory: Callable[[], DataClient] = _default_client,
    control: JobControl | None = None,
    run_id: int | None = None,
    retry_base_delay_seconds: float = RETRY_BASE_DELAY_SECONDS,
) -> GroupedDailyResult:
    """Fetches each weekday in [start_date, end_date] in turn and upserts the selected
    tickers' bars, committing once per day. A day whose request fails is logged and
    skipped (the rest still run); once the main pass is done, the failed days are
    retried in up to MAX_RETRIES further rounds, waiting retry_base_delay_seconds before
    the first and doubling each time (1, 2, then 4 minutes by default). Blocking - run
    it via asyncio.to_thread from the event loop.

    `control` is checked before each day's request and throughout each retry wait, so
    pause/cancel take effect between days (see jobs/control.py). Progress counts the
    main pass's days only."""
    selected = set(_resolve_tickers(session, ticker_types, tickers))
    days = weekdays(start_date, end_date)

    async def _sync_days(client: DataClient, pending: list[dt.date], results: dict[dt.date, int], report: bool):
        failed: list[dt.date] = []
        for completed, day in enumerate(pending, start=1):
            if control is not None:
                await control.checkpoint_async()
            try:
                rows = [_bar_row(result) for result in await _fetch_day(client, day) if result.get("T") in selected]
            except Exception:
                logger.exception("grouped daily sync failed for %s", day)
                failed.append(day)
            else:
                _upsert_bars(session, rows)
                session.commit()
                results[day] = len(rows)
            if report:
                report_job_progress(session, run_id, completed, len(pending), force=True)
        return failed

    async def _run() -> GroupedDailyResult:
        results: dict[dt.date, int] = {}
        report_job_progress(session, run_id, 0, len(days), force=True)
        async with client_factory() as client:
            failed = await _sync_days(client, days, results, report=True)
            retry_round = 0
            while failed and retry_round < MAX_RETRIES:
                retry_round += 1
                delay = retry_base_delay_seconds * 2 ** (retry_round - 1)
                logger.warning(
                    "%d day(s) failed, retry %d of %d in %.0fs", len(failed), retry_round, MAX_RETRIES, delay
                )
                await _wait(delay, control)
                failed = await _sync_days(client, failed, results, report=False)
        return GroupedDailyResult(results, failed, retry_round)

    return asyncio.run(_run())
