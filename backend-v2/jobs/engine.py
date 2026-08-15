"""Job-execution engine - runs only inside job_runner.py's separate process, never
imported by app/main.py (the API process). Moved here largely verbatim from what used
to be app/main.py's _run_job/_scheduled_job/_run_startup_sync_chain plus _job_locks/
_job_controls, when job execution and the HTTP API shared one process.

The two processes now coordinate purely through the (WAL-mode) SQLite DB via polling,
not a direct call or socket - see poll_run_requests/poll_control_relay/
resync_schedules below, and db/models.py's JobConfig.run_requested_at/JobRun.
pause_requested/JobRun.cancel_requested docstrings for what each column means. This
mirrors jobs/control.py's own JobControl, which already polls every 0.2s in-process
for pause/cancel - the same "polling is simple, correct, and plenty responsive for a
dashboard operated by a human" reasoning extends across the process boundary.
"""

import asyncio
import datetime as dt
import logging
import threading

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from data.client import DataClient
from db.models import JobConfig, JobRun
from db.session import SessionLocal
from jobs.average_volume import DEFAULT_DAYS_INTERVAL, compute_average_volume
from jobs.backtest_market_state import compute_market_state_backtest
from jobs.config_store import get_or_create_config, interval_trigger, split_csv
from jobs.control import JobCancelled, JobControl
from jobs.predict_market_state import (
    DEFAULT_MIN_HISTORY_DAYS,
    DEFAULT_PREDICTED_DATE_OFFSET_DAYS,
    compute_market_state_predictions,
)
from jobs.predict_market_state_10_day import compute_10_day_market_state_predictions
from jobs.predict_market_state_mcmc import DEFAULT_NUM_SIMULATIONS, compute_market_state_mcmc_predictions
from jobs.registry import (
    AVERAGE_VOLUME_JOB,
    BACKTEST_MARKET_STATE_JOB,
    BARS_JOB,
    ETF_CONSTITUENTS_JOB,
    INDICATOR_NAMES,
    JOB_DEFINITIONS,
    MOVERS_JOB,
    NEWS_JOB,
    OHLC_BARS_JOB,
    PREDICT_10_DAY_MARKET_STATE_JOB,
    PREDICT_MARKET_STATE_JOB,
    RESEARCH_PICKS_JOB,
    SNAPSHOTS_JOB,
    TICKER_DETAILS_JOB,
    TICKER_TYPES_JOB,
    TICKERS_JOB,
    UNIFIED_SNAPSHOT_JOB,
    WIN_RATE_JOB,
)
from jobs.research_picks import compute_research_picks
from jobs.sync_bars import (
    DEFAULT_BACKFILL_DAYS,
    DEFAULT_END_DATE_OFFSET_DAYS,
    DEFAULT_MULTIPLIER,
    DEFAULT_TIMESPAN,
    sync_bars_nightly,
)
from jobs.sync_etf_constituents import sync_etf_constituents
from jobs.sync_indicators import sync_indicator
from jobs.sync_news import sync_news
from jobs.sync_ohlc_bars import DEFAULT_LIMIT as DEFAULT_OHLC_BARS_LIMIT
from jobs.sync_ohlc_bars import sync_ohlc_bars
from jobs.sync_snapshots import sync_snapshots
from jobs.sync_ticker_details import sync_ticker_details
from jobs.sync_ticker_types import sync_ticker_types
from jobs.sync_tickers import sync_tickers
from jobs.sync_top_movers import sync_top_movers
from jobs.sync_unified_snapshot import sync_unified_snapshot
from jobs.win_rates import compute_win_rates

logger = logging.getLogger("backend_v2.jobs.engine")

# One lock per job name - guards against a scheduled fire overlapping a manual "run
# now" (or two manual runs) of the *same* job. The two jobs run independently of each
# other's locks. Purely in-process now that job execution lives in exactly one process
# (job_runner.py) - app/main.py never touches this, it only ever reads/writes the DB
# (see jobs/config_store.py's job_is_active).
_job_locks: dict[str, threading.Lock] = {name: threading.Lock() for name in JOB_DEFINITIONS}

# Populated with a fresh JobControl (see jobs/control.py) for the duration of each
# job's run - present in this dict for exactly as long as _job_locks[job_name] is held.
# poll_control_relay below is what now drives pause/resume/cancel into these (relaying
# app/main.py's DB-side pause_requested/cancel_requested writes), since the API process
# can no longer reach into this dict directly the way it could when they shared one
# process.
_job_controls: dict[str, JobControl] = {}

# job_name -> (schedule_interval_unit, schedule_interval_value, start_time) as of the
# last time this job's trigger was (re)scheduled - populated by job_runner.py right
# after each initial scheduler.add_job call, updated by resync_schedules below. Used
# instead of comparing full IntervalTrigger objects: interval_trigger(config)'s
# start_date is anchored to config.updated_at's date (see jobs/config_store.py), which
# changes on *any* config edit, not just a schedule edit - comparing trigger objects
# directly would reschedule (and, for a "every N hours/minutes" interval that doesn't
# evenly divide a day, actually shift *when* it next fires) on an unrelated field edit
# like ticker_types. Comparing just the three fields that actually define the schedule
# avoids that.
_last_scheduled: dict[str, tuple[str, int, str]] = {}


def schedule_key(config: JobConfig) -> tuple[str, int, str]:
    return (config.schedule_interval_unit, config.schedule_interval_value, config.start_time)


# asyncio.create_task's result must be referenced somewhere or it can be garbage
# collected mid-run (the task is only weakly held otherwise) - same pitfall
# app/main.py's old lifespan() already worked around for its startup sync chain.
# poll_run_requests below is the only thing that creates fire-and-forget tasks, since
# unlike scheduled_job (awaited inline by APScheduler, which holds its own reference)
# it must be able to kick off more than one job concurrently without blocking on
# whichever it started first.
_running_tasks: set[asyncio.Task] = set()


def reconcile_orphaned_runs() -> int:
    """Called once by job_runner.py at startup, before the scheduler starts. run_job
    below is the only thing that ever sets a JobRun's status to "in_progress" - so any
    row still in that state when this process is only just starting up can't be a run
    this process is actually tracking (its _job_locks/_job_controls are freshly empty
    dicts, populated only by run_job itself). It can only be left over from a *previous*
    instance of this same process that was killed or crashed mid-run - a hard kill
    (e.g. bin/restart-v2.sh, a crash) never reaches run_job's try/except/finally, which
    is the only code that would otherwise mark it failed/cancelled/completed.

    Left unreconciled, that job would appear permanently "running" on the dashboard
    (see jobs/config_store.py's job_is_active, which treats any in_progress row as
    "still active") and every future trigger_job call for it would 409 forever, since
    nothing would ever clear the row.

    Returns the number of rows reconciled, purely so job_runner.py can log it."""
    with SessionLocal() as session:
        orphaned = session.execute(select(JobRun).where(JobRun.status == "in_progress")).scalars().all()
        for run in orphaned:
            run.status = "failed"
            run.error = "job_runner.py restarted or crashed while this run was in progress"
            run.finished_at = dt.datetime.utcnow()
        session.commit()
        return len(orphaned)


async def run_job(job_name: str, trigger: str) -> None:
    """Assumes the caller already holds _job_locks[job_name]; releases it when done.
    `trigger` is "manual" or "auto" - see db/models.py's JobRun.

    Publishes a fresh JobControl to _job_controls for the run's duration -
    poll_control_relay below relays app/main.py's pause/resume/cancel requests into it,
    and every sync_* call is handed it so it can check in between units of work (see
    jobs/control.py). A pause blocks inside that call, still holding this job's lock,
    so nothing here needs to change to support it. A cancel raises JobCancelled, caught
    separately below so a user-requested stop is recorded as "cancelled" rather than
    logged and stored as a "failed" run."""
    session = SessionLocal()
    run = JobRun(job_name=job_name, trigger=trigger, status="in_progress", started_at=dt.datetime.utcnow())
    session.add(run)
    session.commit()
    run_id = run.id
    control = JobControl()
    _job_controls[job_name] = control
    try:
        config = get_or_create_config(session, job_name)
        if job_name == SNAPSHOTS_JOB:
            # No shared DataClient here - sync_snapshots fans out across a thread pool
            # where each worker opens its own DataClient bound to its own event loop
            # (see jobs/sync_snapshots.py). Runs via asyncio.to_thread so this blocking
            # call doesn't stall this process's event loop.
            count = await asyncio.to_thread(
                sync_snapshots,
                session,
                split_csv(config.ticker_types),
                split_csv(config.tickers),
                control=control,
                run_id=run_id,
            )
            summary = f"{count} snapshot(s) synced"
        elif job_name == TICKER_DETAILS_JOB:
            # Same no-shared-DataClient, thread-pool-fan-out reasoning as the
            # snapshots branch above (see jobs/sync_ticker_details.py).
            count = await asyncio.to_thread(
                sync_ticker_details,
                session,
                split_csv(config.ticker_types),
                split_csv(config.tickers),
                control=control,
                run_id=run_id,
            )
            summary = f"{count} ticker detail(s) synced"
        elif job_name == AVERAGE_VOLUME_JOB:
            # Purely local aggregation over already-synced bars - no DataClient needed,
            # just a blocking DB query, so this runs via asyncio.to_thread the same way
            # the snapshots/indicator branches do to keep it off the event loop.
            count = await asyncio.to_thread(
                compute_average_volume,
                session,
                config.average_volume_start_date,
                config.average_volume_days_interval or DEFAULT_DAYS_INTERVAL,
                split_csv(config.ticker_types),
                split_csv(config.tickers),
                control=control,
            )
            summary = f"{count} ticker(s) average volume computed"
        elif job_name == PREDICT_MARKET_STATE_JOB:
            # Merged run: the deterministic Markov chain prediction always runs first,
            # then a Monte Carlo simulation over that same fitted chain - both purely
            # local, off the event loop via asyncio.to_thread, same reasoning as the
            # average-volume branch above. Both phases share one predicted date,
            # resolved here (as an offset in days from today - see JobConfig.
            # predicted_date_offset_days's docstring) rather than twice, so they can
            # never target different sessions.
            offset_days = (
                config.predicted_date_offset_days
                if config.predicted_date_offset_days is not None
                else DEFAULT_PREDICTED_DATE_OFFSET_DAYS
            )
            prediction_date = dt.datetime.now(dt.timezone.utc).date() + dt.timedelta(days=offset_days)
            ticker_types = split_csv(config.ticker_types)
            tickers = split_csv(config.tickers)
            markov_count = await asyncio.to_thread(
                compute_market_state_predictions,
                session,
                prediction_date,
                ticker_types,
                tickers,
                DEFAULT_MIN_HISTORY_DAYS,
                control=control,
            )
            mcmc_count = await asyncio.to_thread(
                compute_market_state_mcmc_predictions,
                session,
                prediction_date,
                ticker_types,
                tickers,
                DEFAULT_MIN_HISTORY_DAYS,
                num_simulations=config.mcmc_num_simulations or DEFAULT_NUM_SIMULATIONS,
                control=control,
            )
            summary = (
                f"{markov_count} ticker(s) market state predicted, "
                f"{mcmc_count} ticker(s) Monte Carlo market state predicted"
            )
        elif job_name == PREDICT_10_DAY_MARKET_STATE_JOB:
            # Same reasoning as the predict-market-state branch above - purely local,
            # off the event loop via asyncio.to_thread.
            count = await asyncio.to_thread(
                compute_10_day_market_state_predictions,
                session,
                config.prediction_start_date,
                split_csv(config.ticker_types),
                split_csv(config.tickers),
                DEFAULT_MIN_HISTORY_DAYS,
                control=control,
            )
            summary = f"{count} ticker(s) 10-day market state predicted"
        elif job_name == WIN_RATE_JOB:
            # Same reasoning as the average-volume/backtest branches above - purely
            # local, off the event loop via asyncio.to_thread.
            count = await asyncio.to_thread(
                compute_win_rates,
                session,
                split_csv(config.ticker_types),
                split_csv(config.tickers),
                control=control,
            )
            summary = f"{count} ticker(s) win rate computed"
        elif job_name == RESEARCH_PICKS_JOB:
            # Same reasoning as the win-rate/average-volume branches above - purely
            # local, off the event loop via asyncio.to_thread.
            count = await asyncio.to_thread(compute_research_picks, session, run_id, control=control)
            summary = f"{count} research pick(s) selected"
        elif job_name == ETF_CONSTITUENTS_JOB:
            # No DB session and no massive.com DataClient here - this job only
            # downloads raw files from SSGA to disk (see jobs/sync_etf_constituents.py),
            # so unlike the branches in the `else` block below it doesn't need this
            # run's shared DataClient, and unlike the asyncio.to_thread branches above
            # it's already async and lightweight (a handful of tickers), so it runs
            # directly on the event loop the same way sync_ticker_types does.
            tickers = split_csv(config.tickers) or []
            count = await sync_etf_constituents(tickers, control=control)
            summary = f"{count}/{len(tickers)} etf holdings file(s) downloaded"
        elif job_name == BACKTEST_MARKET_STATE_JOB:
            # Same reasoning as the average-volume/predict-market-state branches above -
            # purely local, off the event loop via asyncio.to_thread.
            count = await asyncio.to_thread(
                compute_market_state_backtest,
                session,
                config.backtest_start_date,
                config.backtest_end_date,
                split_csv(config.ticker_types),
                split_csv(config.tickers),
                DEFAULT_MIN_HISTORY_DAYS,
                control=control,
            )
            summary = f"{count} result(s) backtested"
        elif job_name in INDICATOR_NAMES:
            # Same no-shared-DataClient reasoning as the snapshots branch above -
            # sync_indicator fans out across its own thread pool of per-worker clients.
            indicator = INDICATOR_NAMES[job_name]
            count = await asyncio.to_thread(
                sync_indicator,
                indicator,
                session,
                split_csv(config.ticker_types),
                split_csv(config.tickers),
                control=control,
                run_id=run_id,
            )
            summary = f"{count} {indicator} value(s) synced"
        elif job_name == BARS_JOB:
            # No shared DataClient here - sync_bars_nightly fans out across a thread
            # pool the same way sync_snapshots/sync_indicator do (see
            # jobs/sync_bars.py), so this runs via asyncio.to_thread too.
            results = await asyncio.to_thread(
                sync_bars_nightly,
                session,
                split_csv(config.ticker_types),
                split_csv(config.tickers),
                multiplier=config.multiplier or DEFAULT_MULTIPLIER,
                timespan=config.timespan or DEFAULT_TIMESPAN,
                # `or` would wrongly fall back to the default on a deliberate 0
                # ("no backfill, just the end date itself") - 0 is falsy but a valid
                # value now that Backfill days' UI minimum is 0, not 1.
                backfill_days=config.backfill_days if config.backfill_days is not None else DEFAULT_BACKFILL_DAYS,
                # Same reasoning as backfill_days above - 0 ("through today") is a
                # valid offset, not a "use the default" signal.
                end_date_offset_days=(
                    config.bars_end_date_offset_days
                    if config.bars_end_date_offset_days is not None
                    else DEFAULT_END_DATE_OFFSET_DAYS
                ),
                control=control,
                run_id=run_id,
            )
            summary = f"{len(results)} ticker(s) synced, {sum(results.values())} bar(s) fetched"
        elif job_name == OHLC_BARS_JOB:
            # Same no-shared-DataClient, thread-pool-fan-out reasoning as the BARS_JOB
            # branch above (see jobs/sync_ohlc_bars.py) - selection-query-driven and
            # limit-bounded rather than covering every known ticker every run.
            results = await asyncio.to_thread(
                sync_ohlc_bars,
                session,
                start_date=config.ohlc_bars_start_date,
                end_date=config.ohlc_bars_end_date,
                limit=config.ohlc_bars_limit or DEFAULT_OHLC_BARS_LIMIT,
                control=control,
                run_id=run_id,
            )
            summary = f"{len(results)} ticker(s) synced, {sum(results.values())} bar(s) fetched"
        else:
            async with DataClient() as client:
                if job_name == TICKERS_JOB:
                    count = await sync_tickers(
                        client, session, ticker_type=config.ticker_types or None, control=control
                    )
                    summary = f"{count} ticker(s) synced"
                elif job_name == TICKER_TYPES_JOB:
                    count = await sync_ticker_types(client, session, control=control)
                    summary = f"{count} ticker type(s) synced"
                elif job_name == MOVERS_JOB:
                    results = await sync_top_movers(client, session, control=control)
                    summary = f"{results.get('gainers', 0)} gainer(s), {results.get('losers', 0)} loser(s) synced"
                elif job_name == NEWS_JOB:
                    count = await sync_news(client, session, control=control)
                    summary = f"{count} news article(s) synced"
                else:
                    assert job_name == UNIFIED_SNAPSHOT_JOB
                    results = await sync_unified_snapshot(
                        client, session, split_csv(config.snapshot_types), control=control
                    )
                    summary = ", ".join(f"{t}: {n}" for t, n in results.items())
        run = session.get(JobRun, run_id)
        assert run is not None
        run.status = "completed"
        run.result_summary = summary
        run.finished_at = dt.datetime.utcnow()
        session.commit()
    except JobCancelled:
        logger.info("%s job cancelled", job_name)
        session.rollback()
        run = session.get(JobRun, run_id)
        assert run is not None
        run.status = "cancelled"
        run.finished_at = dt.datetime.utcnow()
        session.commit()
    except Exception as exc:
        logger.exception("%s job failed", job_name)
        session.rollback()
        run = session.get(JobRun, run_id)
        assert run is not None
        run.status = "failed"
        run.error = str(exc)
        run.finished_at = dt.datetime.utcnow()
        session.commit()
    finally:
        session.close()
        _job_controls.pop(job_name, None)
        _job_locks[job_name].release()


async def scheduled_job(job_name: str) -> None:
    with SessionLocal() as session:
        run_type = get_or_create_config(session, job_name).run_type
    if run_type != "auto":
        logger.info("%s is manual-only, skipping scheduled run", job_name)
        return
    if not _job_locks[job_name].acquire(blocking=False):
        logger.info("%s already running, skipping scheduled trigger", job_name)
        return
    await run_job(job_name, "auto")


async def run_startup_sync_chain() -> None:
    # Sequential, not parallel: the bars job selects tickers out of the tickers table,
    # so a startup run needs tickers synced first, not racing it. ticker-types has no
    # such dependency. Each call also respects that job's `run_type`, same as a
    # scheduled cron fire would - ticker-types defaults to manual, so this is normally
    # a no-op at startup.
    await scheduled_job(TICKER_TYPES_JOB)
    await scheduled_job(TICKERS_JOB)
    await scheduled_job(BARS_JOB)


async def poll_run_requests() -> None:
    """Relays app/main.py's POST /jobs/{name}/run (which just sets JobConfig.
    run_requested_at - see jobs/config_store.py's job_is_active) into an actual run.
    Fire-and-forget via asyncio.create_task rather than awaited inline: unlike
    scheduled_job (one job id per real job, so APScheduler already runs each
    concurrently with the others), this single poll tick may see more than one
    pending request at once and must be able to start all of them without waiting for
    the first to finish."""
    with SessionLocal() as session:
        pending = list(
            session.execute(select(JobConfig.job_name).where(JobConfig.run_requested_at.is_not(None))).scalars()
        )
    for job_name in pending:
        if job_name not in JOB_DEFINITIONS:
            continue
        if not _job_locks[job_name].acquire(blocking=False):
            # Already running (e.g. a scheduled fire got there first) - leave
            # run_requested_at set so the next poll tick retries once it's free.
            continue
        cleared = False
        try:
            with SessionLocal() as session:
                config = session.get(JobConfig, job_name)
                assert config is not None
                config.run_requested_at = None
                session.commit()
            cleared = True
        finally:
            if not cleared:
                _job_locks[job_name].release()
        task = asyncio.create_task(run_job(job_name, "manual"))
        _running_tasks.add(task)
        task.add_done_callback(_running_tasks.discard)


async def poll_control_relay() -> None:
    """Relays app/main.py's POST /jobs/{name}/pause|resume|cancel (which UPDATE the
    in-progress JobRun row's pause_requested/cancel_requested - see db/models.py's
    JobRun docstring) into the matching in-memory JobControl. JobControl is backed by
    threading.Event (documented thread-safe), so calling its request_* methods from
    this poll (which runs as one of AsyncIOScheduler's own tasks) is safe."""
    if not _job_controls:
        return
    with SessionLocal() as session:
        rows = session.execute(
            select(JobRun.job_name, JobRun.pause_requested, JobRun.cancel_requested).where(
                JobRun.job_name.in_(list(_job_controls.keys())), JobRun.status == "in_progress"
            )
        ).all()
    for job_name, pause_requested, cancel_requested in rows:
        control = _job_controls.get(job_name)
        if control is None:
            continue
        if cancel_requested:
            control.request_cancel()
        elif pause_requested and not control.pause_requested:
            control.request_pause()
        elif not pause_requested and control.pause_requested:
            control.request_resume()


def resync_schedules(scheduler: AsyncIOScheduler) -> None:
    """Replaces app/main.py's old direct `_scheduler.reschedule_job(...)` call inside
    PUT /jobs/{name}/config - that process no longer holds a live scheduler reference,
    so instead this compares each JobConfig's (unit, value, start_time) against
    _last_scheduled and reschedules whatever drifted. Edits now take effect within this
    poll's interval instead of instantly - an accepted trade-off (see the plan this was
    built from) of not sharing a scheduler object across processes."""
    with SessionLocal() as session:
        for job_name in JOB_DEFINITIONS:
            config = get_or_create_config(session, job_name)
            key = schedule_key(config)
            if _last_scheduled.get(job_name) == key:
                continue
            if scheduler.get_job(job_name) is None:
                continue
            scheduler.reschedule_job(job_name, trigger=interval_trigger(config))
            _last_scheduled[job_name] = key
            logger.info(
                "rescheduled %s: now every %d %s starting %s UTC",
                job_name,
                config.schedule_interval_value,
                config.schedule_interval_unit,
                config.start_time,
            )
