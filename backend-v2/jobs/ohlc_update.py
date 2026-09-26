"""Auto-run behavior of the ohlc-data-update job (see jobs/registry.py's OHLC_UPDATE_JOB):
each auto run syncs the next batch of tickers - alphabetically, at most
JobConfig.ohlc_update_batch_size (default BATCH_SIZE) - over the job's saved date range,
then remembers where it stopped so the next interval picks up from there.

Selection is the job's saved Tickers or Ticker types, or every ticker in the tickers
table if neither is set (see jobs/sync_bars.py's _resolve_tickers - same mutually
exclusive rule). Progress lives on JobConfig.ohlc_update_cursor (last ticker of the
previous batch) rather than being inferred from ohlc_bars/tickers.last_ohlc_sync_date,
since this job deliberately re-fetches a range regardless of what's already synced.

A cycle is one main pass over the selection followed by up to MAX_RETRIES retry passes:
- Tickers whose fetch fails are collected (JobConfig.ohlc_update_failed_tickers).
- When a pass reaches the end of its selection and something failed, the failures become
  a retry pass over just those tickers, batched exactly like the main pass. It doesn't
  start immediately: it waits 2x the schedule interval (4x for the second retry, 8x for
  the third) after the previous pass ended, held in JobConfig.ohlc_update_next_run_at.
- When a pass ends with nothing failed, or the last retry pass ends, the cycle is
  complete: JobConfig.ohlc_update_completed_at is stamped and every auto run is skipped
  (see engine.py's scheduled_job, via is_paused) until the next day at the job's
  start_time (UTC), when all of the above is cleared and a new cycle begins from the top.

The fetch itself is jobs/sync_bars.py's sync_bars_manual, unchanged.
"""

import datetime as dt
import json
from dataclasses import dataclass
from typing import Callable

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from data.client import DataClient
from db.models import JobConfig, Ticker
from jobs.control import JobControl
from jobs.registry import OHLC_UPDATE_JOB
from jobs.sync_bars import DEFAULT_MAX_WORKERS, sync_bars_manual

BATCH_SIZE = 5000
MAX_RETRIES = 3
# 2 years, same lookback as jobs/sync_ohlc_bars.py's DEFAULT_LOOKBACK_DAYS.
DEFAULT_LOOKBACK_DAYS = 730


@dataclass
class BatchResult:
    results: dict[str, int]
    # Tickers in this run's batch whose fetch failed.
    failed: list[str]
    # True if the cycle is over (nothing more to do until tomorrow) - including a run
    # skipped because the cycle was already complete and hasn't resumed yet.
    cycle_complete: bool
    # Human-readable status for the run's result_summary, or None for a plain
    # mid-pass batch.
    note: str | None = None


def resolve_date_range(
    start_date: dt.date | None, end_date: dt.date | None, today: dt.date | None = None
) -> tuple[dt.date, dt.date]:
    """A None end date is today (UTC); a None start date is DEFAULT_LOOKBACK_DAYS before
    the (resolved) end date, so with neither set the range is the 2 years ending today."""
    today = today if today is not None else dt.datetime.now(dt.timezone.utc).date()
    end_date = end_date if end_date is not None else today
    start_date = start_date if start_date is not None else end_date - dt.timedelta(days=DEFAULT_LOOKBACK_DAYS)
    if start_date > end_date:
        raise ValueError("ohlc-data-update's Start date must not be after End date")
    return start_date, end_date


def resume_at(config: JobConfig) -> dt.datetime | None:
    """The earliest time (naive UTC) the next auto run may do work, or None if it isn't
    held off: a completed cycle waits until the next day at the job's start_time; a
    finished pass with failures waits out its retry delay."""
    if config.ohlc_update_completed_at is not None:
        hour, minute = (int(part) for part in config.start_time.split(":"))
        next_day = config.ohlc_update_completed_at.date() + dt.timedelta(days=1)
        return dt.datetime.combine(next_day, dt.time(hour, minute))
    return config.ohlc_update_next_run_at


def is_paused(config: JobConfig, now: dt.datetime | None = None) -> bool:
    resume = resume_at(config)
    return resume is not None and (now if now is not None else dt.datetime.utcnow()) < resume


def _schedule_interval(config: JobConfig) -> dt.timedelta:
    """The job's schedule interval - schedule_interval_unit doubles as the timedelta
    keyword ("minutes"/"hours"/"days"), same as jobs/config_store.py's interval_trigger."""
    return dt.timedelta(**{config.schedule_interval_unit: config.schedule_interval_value})


def _select_batch(
    session: Session,
    ticker_types: list[str] | None,
    tickers: list[str] | None,
    after: str | None,
    limit: int,
) -> list[str]:
    """Up to `limit` selected tickers sorted alphabetically and strictly after `after`
    (None = from the start). An explicit ticker list is used as given - not filtered
    against the tickers table - same as sync_bars_manual."""
    if tickers and ticker_types:
        raise ValueError("specify tickers or ticker_types, not both")
    if tickers:
        candidates = sorted(set(tickers))
        if after is not None:
            candidates = [ticker for ticker in candidates if ticker > after]
        return candidates[:limit]
    query = select(Ticker.ticker).order_by(Ticker.ticker).limit(limit)
    if ticker_types:
        query = query.where(Ticker.type.in_(ticker_types))
    if after is not None:
        query = query.where(Ticker.ticker > after)
    return list(session.scalars(query))


def _save_progress(
    session: Session,
    cursor: str | None = None,
    completed_at: dt.datetime | None = None,
    retry_round: int = 0,
    retry_tickers: list[str] | None = None,
    failed_tickers: list[str] | None = None,
    next_run_at: dt.datetime | None = None,
) -> None:
    """Writes every progress column at once - a bare call resets them all (a fresh
    cycle)."""
    session.execute(
        update(JobConfig)
        .where(JobConfig.job_name == OHLC_UPDATE_JOB)
        .values(
            ohlc_update_cursor=cursor,
            ohlc_update_completed_at=completed_at,
            ohlc_update_retry_round=retry_round,
            ohlc_update_retry_tickers=json.dumps(retry_tickers) if retry_tickers else None,
            ohlc_update_failed_tickers=json.dumps(failed_tickers) if failed_tickers else None,
            ohlc_update_next_run_at=next_run_at,
        )
    )
    session.commit()


def sync_ohlc_update_batch(
    session: Session,
    config: JobConfig,
    ticker_types: list[str] | None,
    tickers: list[str] | None,
    batch_size: int | None = None,
    max_workers: int = DEFAULT_MAX_WORKERS,
    client_factory: Callable[[], DataClient] = DataClient,
    control: JobControl | None = None,
    run_id: int | None = None,
) -> BatchResult:
    """One auto run: starts a new cycle if the previous one's pause has ended, syncs the
    next batch (`batch_size`, else the config's ohlc_update_batch_size, else BATCH_SIZE)
    of the current pass over the config's resolved date range, then advances the
    cursor - or, if that batch was the last of the pass, moves on to a retry pass or
    completes the cycle (see the module docstring).

    Progress is only saved after the batch finishes, so a cancelled or crashed run
    re-syncs the same batch next time (a harmless re-upsert)."""
    now = dt.datetime.utcnow()
    if is_paused(config, now):
        return BatchResult({}, [], cycle_complete=config.ohlc_update_completed_at is not None)
    if config.ohlc_update_completed_at is not None:
        # Yesterday's cycle finished and today's start time has arrived - start over.
        _save_progress(session)

    cursor = config.ohlc_update_cursor
    retry_round = config.ohlc_update_retry_round or 0
    retry_tickers = json.loads(config.ohlc_update_retry_tickers) if config.ohlc_update_retry_tickers else []
    failed: list[str] = json.loads(config.ohlc_update_failed_tickers) if config.ohlc_update_failed_tickers else []
    batch_size = batch_size or config.ohlc_update_batch_size or BATCH_SIZE

    start_date, end_date = resolve_date_range(config.ohlc_update_start_date, config.ohlc_update_end_date)

    # One extra row tells us whether anything is left in this pass after this batch
    # without a second query. A retry pass selects out of its saved failure list
    # instead of the tickers table.
    if retry_round == 0:
        selected = _select_batch(session, ticker_types, tickers, cursor, batch_size + 1)
    else:
        selected = _select_batch(session, None, retry_tickers, cursor, batch_size + 1)
    batch = selected[:batch_size]
    pass_complete = len(selected) <= batch_size

    results: dict[str, int] = {}
    if batch:
        results = sync_bars_manual(
            session,
            start_date,
            end_date,
            tickers=batch,
            max_workers=max_workers,
            client_factory=client_factory,
            control=control,
            run_id=run_id,
        )
    # sync_bars_manual logs and omits any ticker whose fetch failed.
    batch_failed = [ticker for ticker in batch if ticker not in results]
    failed = sorted(set(failed) | set(batch_failed))

    if not pass_complete:
        _save_progress(
            session, cursor=batch[-1], retry_round=retry_round, retry_tickers=retry_tickers, failed_tickers=failed
        )
        return BatchResult(results, batch_failed, cycle_complete=False)

    finished_at = dt.datetime.utcnow()
    if not failed:
        _save_progress(session, completed_at=finished_at)
        return BatchResult(
            results,
            batch_failed,
            cycle_complete=True,
            note="all tickers updated, paused until the next day at the start time",
        )
    if retry_round >= MAX_RETRIES:
        _save_progress(session, completed_at=finished_at)
        return BatchResult(
            results,
            batch_failed,
            cycle_complete=True,
            note=(
                f"{len(failed)} ticker(s) still failing after {MAX_RETRIES} retries, giving up - "
                "paused until the next day at the start time"
            ),
        )

    retry_round += 1
    next_run_at = finished_at + _schedule_interval(config) * 2**retry_round
    _save_progress(session, retry_round=retry_round, retry_tickers=failed, next_run_at=next_run_at)
    return BatchResult(
        results,
        batch_failed,
        cycle_complete=False,
        note=(
            f"{len(failed)} ticker(s) failed - retry {retry_round} of {MAX_RETRIES} "
            f"not before {next_run_at:%Y-%m-%d %H:%M} UTC"
        ),
    )
