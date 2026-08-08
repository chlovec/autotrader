"""backend-v2's API + scheduled-jobs process. Combines what a bare run_jobs.py used to
do (run sync-tickers and sync-bars-nightly once on startup, then keep them on a
recurring interval schedule - see jobs/registry.py's DEFAULT_SCHEDULES) with a REST API the dashboard's
Jobs page uses to view/edit each job's config, trigger a manual run, and see run
history. Launched via run_jobs.py (uvicorn.run("app.main:app", ...)) so
bin/restart-v2.sh's invocation doesn't need to change.

Mirrors backend/app/main.py's shape at the repo root (v1) for the equivalent research
job: a per-job threading.Lock guards against overlapping runs of the same job, a
BackgroundTasks-triggered run releases its lock in a finally block, and a dashboard
run-type ("manual" vs "auto") gates the *scheduled* trigger without blocking a manual one.
"""

import asyncio
import datetime as dt
import logging
import os
import threading
from contextlib import asynccontextmanager
from typing import Any, Literal

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from data.client import DataClient
from db.models import AverageVolume, JobConfig, JobRun, Ticker, TickerType, TopMarketMover
from db.session import SessionLocal, init_db
from jobs.average_volume import DEFAULT_DAYS_INTERVAL, compute_average_volume
from jobs.control import JobCancelled, JobControl
from jobs.registry import (
    AVERAGE_VOLUME_JOB,
    BARS_JOB,
    DEFAULT_SCHEDULES,
    DEFAULT_START_TIME,
    INDICATOR_NAMES,
    JOB_DEFINITIONS,
    MOVERS_JOB,
    NEWS_JOB,
    SNAPSHOT_TYPE_OPTIONS,
    SNAPSHOTS_JOB,
    START_TIME_OPTIONS,
    TICKER_TYPES_JOB,
    TICKERS_JOB,
    UNIFIED_SNAPSHOT_JOB,
)
from jobs.sync_bars import DEFAULT_BACKFILL_DAYS, DEFAULT_MULTIPLIER, DEFAULT_TIMESPAN, sync_bars_nightly
from jobs.sync_indicators import sync_indicator
from jobs.sync_news import sync_news
from jobs.sync_snapshots import sync_snapshots
from jobs.sync_ticker_types import sync_ticker_types
from jobs.sync_tickers import sync_tickers
from jobs.sync_top_movers import sync_top_movers
from jobs.sync_unified_snapshot import sync_unified_snapshot

logger = logging.getLogger("backend_v2.app")

# Same requirement as backend/app/main.py (v1): no default, since a wrong-but-plausible
# one is how a dashboard silently getting "Backend unreachable" happened before.
_cors_origins_raw = os.environ.get("CORS_ORIGINS")
if not _cors_origins_raw:
    raise RuntimeError(
        "CORS_ORIGINS is not set. Set it to frontend-v2's origin in backend-v2/.env, "
        "e.g. CORS_ORIGINS=http://localhost:5174 - see backend-v2/.env.example."
    )
_cors_origins = [origin.strip() for origin in _cors_origins_raw.split(",") if origin.strip()]
if not _cors_origins:
    raise RuntimeError("CORS_ORIGINS is set but empty after parsing - check its value in backend-v2/.env.")

# One lock per job name - guards against a scheduled fire overlapping a manual "run
# now" (or two manual runs) of the *same* job. The two jobs run independently of each
# other's locks.
_job_locks: dict[str, threading.Lock] = {name: threading.Lock() for name in JOB_DEFINITIONS}

# Populated with a fresh JobControl (see jobs/control.py) for the duration of each
# job's run - present in this dict for exactly as long as _job_locks[job_name] is held.
# pause_job/resume_job/cancel_job below look a job up here rather than through the lock
# since the lock alone can't carry a pause/cancel signal into the running job's loop.
# Plain dict, not a Lock-guarded one: entries are only ever set/popped by _run_job on
# the event loop thread, and read (via .get()) from FastAPI's sync-endpoint threadpool -
# CPython dict reads/writes are atomic per-operation under the GIL, same assumption
# _job_locks itself already relies on.
_job_controls: dict[str, JobControl] = {}

# Set by lifespan on startup. update_job_config reschedules through this so an edited
# schedule_interval_unit/schedule_interval_value takes effect immediately instead of
# only on the next backend-v2 restart.
_scheduler: AsyncIOScheduler | None = None


def _get_or_create_config(session: Session, job_name: str) -> JobConfig:
    config = session.get(JobConfig, job_name)
    if config is not None:
        return config
    unit, value = DEFAULT_SCHEDULES[job_name]
    config = JobConfig(
        job_name=job_name,
        run_type=JOB_DEFINITIONS[job_name].default_run_type,
        schedule_interval_unit=unit,
        schedule_interval_value=value,
        start_time=DEFAULT_START_TIME,
        multiplier=DEFAULT_MULTIPLIER if job_name == BARS_JOB else None,
        timespan=DEFAULT_TIMESPAN if job_name == BARS_JOB else None,
        backfill_days=DEFAULT_BACKFILL_DAYS if job_name == BARS_JOB else None,
    )
    session.add(config)
    session.commit()
    return config


def _interval_trigger(unit: str, value: int, start_time: str) -> IntervalTrigger:
    """`unit` ("minutes"/"hours"/"days") doubles as the IntervalTrigger keyword arg name -
    see db/models.py's JobConfig.schedule_interval_unit. `start_time` ("HH:MM" UTC, one
    of registry.START_TIME_OPTIONS) anchors the trigger's phase via start_date: combined
    with *today's* UTC date, since only the time-of-day component matters here -
    APScheduler's IntervalTrigger walks forward from start_date in `value`-`unit` steps
    to find the next fire time >= now regardless of whether start_date itself is in the
    past, so "today" is just as good an anchor date as the literal day the schedule was
    first configured."""
    hour, minute = (int(part) for part in start_time.split(":"))
    today = dt.datetime.now(dt.timezone.utc).date()
    start_date = dt.datetime.combine(today, dt.time(hour, minute), tzinfo=dt.timezone.utc)
    return IntervalTrigger(**{unit: value}, start_date=start_date, timezone="UTC")


def _split_csv(value: str | None) -> list[str] | None:
    if not value:
        return None
    items = [item.strip() for item in value.split(",") if item.strip()]
    return items or None


async def _run_job(job_name: str, trigger: str) -> None:
    """Assumes the caller already holds _job_locks[job_name]; releases it when done.
    `trigger` is "manual" or "auto" - see db/models.py's JobRun.

    Publishes a fresh JobControl to _job_controls for the run's duration - pause_job/
    resume_job/cancel_job below signal through it, and every sync_* call is handed it
    so it can check in between units of work (see jobs/control.py). A pause blocks
    inside that call, still holding this job's lock, so nothing here needs to change to
    support it. A cancel raises JobCancelled, caught separately below so a
    user-requested stop is recorded as "cancelled" rather than logged and stored as a
    "failed" run."""
    session = SessionLocal()
    run = JobRun(job_name=job_name, trigger=trigger, status="in_progress", started_at=dt.datetime.utcnow())
    session.add(run)
    session.commit()
    run_id = run.id
    control = JobControl()
    _job_controls[job_name] = control
    try:
        config = _get_or_create_config(session, job_name)
        if job_name == SNAPSHOTS_JOB:
            # No shared DataClient here - sync_snapshots fans out across a thread pool
            # where each worker opens its own DataClient bound to its own event loop
            # (see jobs/sync_snapshots.py). Runs via asyncio.to_thread so this blocking
            # call doesn't stall the server's event loop.
            count = await asyncio.to_thread(
                sync_snapshots,
                session,
                _split_csv(config.ticker_types),
                _split_csv(config.tickers),
                control=control,
            )
            summary = f"{count} snapshot(s) synced"
        elif job_name == AVERAGE_VOLUME_JOB:
            # Purely local aggregation over already-synced bars - no DataClient needed,
            # just a blocking DB query, so this runs via asyncio.to_thread the same way
            # the snapshots/indicator branches do to keep it off the event loop.
            count = await asyncio.to_thread(
                compute_average_volume,
                session,
                config.average_volume_start_date,
                config.average_volume_days_interval or DEFAULT_DAYS_INTERVAL,
                _split_csv(config.ticker_types),
                _split_csv(config.tickers),
                control=control,
            )
            summary = f"{count} ticker(s) average volume computed"
        elif job_name in INDICATOR_NAMES:
            # Same no-shared-DataClient reasoning as the snapshots branch above -
            # sync_indicator fans out across its own thread pool of per-worker clients.
            indicator = INDICATOR_NAMES[job_name]
            count = await asyncio.to_thread(
                sync_indicator,
                indicator,
                session,
                _split_csv(config.ticker_types),
                _split_csv(config.tickers),
                control=control,
            )
            summary = f"{count} {indicator} value(s) synced"
        elif job_name == BARS_JOB:
            # No shared DataClient here - sync_bars_nightly fans out across a thread
            # pool the same way sync_snapshots/sync_indicator do (see
            # jobs/sync_bars.py), so this runs via asyncio.to_thread too.
            results = await asyncio.to_thread(
                sync_bars_nightly,
                session,
                _split_csv(config.ticker_types),
                _split_csv(config.tickers),
                multiplier=config.multiplier or DEFAULT_MULTIPLIER,
                timespan=config.timespan or DEFAULT_TIMESPAN,
                backfill_days=config.backfill_days or DEFAULT_BACKFILL_DAYS,
                control=control,
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
                        client, session, _split_csv(config.snapshot_types), control=control
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


async def _scheduled_job(job_name: str) -> None:
    with SessionLocal() as session:
        run_type = _get_or_create_config(session, job_name).run_type
    if run_type != "auto":
        logger.info("%s is manual-only, skipping scheduled run", job_name)
        return
    if not _job_locks[job_name].acquire(blocking=False):
        logger.info("%s already running, skipping scheduled trigger", job_name)
        return
    await _run_job(job_name, "auto")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _scheduler
    init_db()
    with SessionLocal() as session:
        schedules = {name: _get_or_create_config(session, name) for name in JOB_DEFINITIONS}

    # Sequential, not parallel: the bars job selects tickers out of the tickers table,
    # so a startup run needs tickers synced first, not racing it. ticker-types has no
    # such dependency. Each call also respects that job's `run_type`, same as a
    # scheduled cron fire would - ticker-types defaults to manual, so this is normally
    # a no-op at startup.
    await _scheduled_job(TICKER_TYPES_JOB)
    await _scheduled_job(TICKERS_JOB)
    await _scheduled_job(BARS_JOB)

    scheduler = AsyncIOScheduler()
    for name, config in schedules.items():
        scheduler.add_job(
            _scheduled_job,
            _interval_trigger(config.schedule_interval_unit, config.schedule_interval_value, config.start_time),
            args=[name],
            id=name,
        )
        logger.info(
            "scheduled %s every %d %s starting %s UTC",
            name,
            config.schedule_interval_value,
            config.schedule_interval_unit,
            config.start_time,
        )
    scheduler.start()
    _scheduler = scheduler

    yield

    scheduler.shutdown(wait=False)
    _scheduler = None


app = FastAPI(title="Autotrader Backend v2", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _run_to_dict(run: JobRun) -> dict[str, Any]:
    duration_seconds = (run.finished_at - run.started_at).total_seconds() if run.finished_at else None
    return {
        "id": run.id,
        "trigger": run.trigger,
        "status": run.status,
        "started_at": run.started_at.isoformat(),
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "duration_seconds": duration_seconds,
        "result_summary": run.result_summary,
        "error": run.error,
    }


def _next_run_time(job_name: str, run_type: str) -> str | None:
    """None for a "manual" job even though it's still registered with APScheduler (see
    lifespan) - its trigger would otherwise report a next fire time that's really just
    going to be skipped by _scheduled_job's run_type check, which would be misleading
    on the dashboard."""
    if run_type != "auto" or _scheduler is None:
        return None
    job = _scheduler.get_job(job_name)
    if job is None or job.next_run_time is None:
        return None
    return job.next_run_time.isoformat()


def _job_to_dict(session: Session, job_name: str) -> dict[str, Any]:
    definition = JOB_DEFINITIONS[job_name]
    config = _get_or_create_config(session, job_name)
    last_run = session.execute(
        select(JobRun).where(JobRun.job_name == job_name).order_by(JobRun.started_at.desc()).limit(1)
    ).scalar_one_or_none()
    return {
        "name": job_name,
        "label": definition.label,
        "description": definition.description,
        "has_bars_fields": definition.has_bars_fields,
        "has_ticker_type_filter": definition.has_ticker_type_filter,
        "has_ticker_selector": definition.has_ticker_selector,
        "has_snapshot_type_filter": definition.has_snapshot_type_filter,
        "has_average_volume_fields": definition.has_average_volume_fields,
        "snapshot_type_options": SNAPSHOT_TYPE_OPTIONS,
        "run_type": config.run_type,
        "schedule_interval_unit": config.schedule_interval_unit,
        "schedule_interval_value": config.schedule_interval_value,
        "start_time": config.start_time,
        "next_run_time": _next_run_time(job_name, config.run_type),
        "ticker_types": config.ticker_types,
        "tickers": config.tickers,
        "multiplier": config.multiplier,
        "timespan": config.timespan,
        "backfill_days": config.backfill_days,
        "snapshot_types": config.snapshot_types,
        "average_volume_start_date": (
            config.average_volume_start_date.isoformat() if config.average_volume_start_date else None
        ),
        "average_volume_days_interval": config.average_volume_days_interval,
        "hidden": config.hidden,
        "running": _job_locks[job_name].locked(),
        "paused": job_name in _job_controls and _job_controls[job_name].pause_requested,
        "last_run": _run_to_dict(last_run) if last_run is not None else None,
    }


def _require_job(job_name: str) -> None:
    if job_name not in JOB_DEFINITIONS:
        raise HTTPException(status_code=404, detail=f"unknown job {job_name!r}")


class JobConfigIn(BaseModel):
    run_type: Literal["manual", "auto"]
    schedule_interval_unit: Literal["minutes", "hours", "days"]
    schedule_interval_value: int
    start_time: str = DEFAULT_START_TIME
    ticker_types: str | None = None
    tickers: str | None = None
    multiplier: int | None = None
    timespan: str | None = None
    backfill_days: int | None = None
    snapshot_types: str | None = None
    average_volume_start_date: str | None = None
    average_volume_days_interval: int | None = None


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/ticker-types/search")
def search_ticker_types(q: str = "", limit: int = 20) -> list[dict]:
    """Backs the Jobs page's searchable ticker-type selects (JobCard's TickerType
    combobox(es)) - looks up the ticker_types reference table itself (synced by
    sync-ticker-types), matching against code, asset_class, or description rather than
    just the codes actually in use on the tickers table."""
    limit = max(1, min(limit, 50))
    with SessionLocal() as session:
        query = select(TickerType.code, TickerType.asset_class, TickerType.description)
        term = q.strip()
        if term:
            pattern = f"%{term}%"
            query = query.where(
                or_(
                    TickerType.code.like(pattern),
                    TickerType.asset_class.like(pattern),
                    TickerType.description.like(pattern),
                )
            )
        query = query.distinct().order_by(TickerType.code).limit(limit)
        rows = session.execute(query).all()
        return [{"code": row.code, "asset_class": row.asset_class, "description": row.description} for row in rows]


@app.get("/tickers/search")
def search_tickers(q: str = "", limit: int = 20) -> list[dict]:
    """Backs the Jobs page's searchable Tickers select (JobCard's Tickers combobox) -
    server-side so the dropdown never has to ship the whole tickers table to the
    browser just to filter it client-side."""
    limit = max(1, min(limit, 50))
    with SessionLocal() as session:
        query = select(Ticker.ticker, Ticker.name)
        term = q.strip()
        if term:
            pattern = f"%{term}%"
            query = query.where(or_(Ticker.ticker.like(pattern), Ticker.name.like(pattern)))
        query = query.order_by(Ticker.ticker).limit(limit)
        rows = session.execute(query).all()
        return [{"ticker": row.ticker, "name": row.name} for row in rows]


def _mover_to_dict(
    mover: TopMarketMover, ticker: Ticker | None, asset_class: str | None, average_volume: float | None
) -> dict[str, Any]:
    return {
        "ticker": mover.ticker,
        "name": ticker.name if ticker else None,
        "type": ticker.type if ticker else None,
        "asset_class": asset_class,
        "average_volume": average_volume,
        "direction": mover.direction,
        "rank": mover.rank,
        "todays_change": mover.todays_change,
        "todays_change_perc": mover.todays_change_perc,
        "updated": mover.updated.isoformat() if mover.updated else None,
        "day_open": mover.day_open,
        "day_high": mover.day_high,
        "day_low": mover.day_low,
        "day_close": mover.day_close,
        "day_volume": mover.day_volume,
        "day_vwap": mover.day_vwap,
        "min_open": mover.min_open,
        "min_high": mover.min_high,
        "min_low": mover.min_low,
        "min_close": mover.min_close,
        "min_volume": mover.min_volume,
        "min_vwap": mover.min_vwap,
        "min_accumulated_volume": mover.min_accumulated_volume,
        "min_timestamp": mover.min_timestamp.isoformat() if mover.min_timestamp else None,
        "prev_day_open": mover.prev_day_open,
        "prev_day_high": mover.prev_day_high,
        "prev_day_low": mover.prev_day_low,
        "prev_day_close": mover.prev_day_close,
        "prev_day_volume": mover.prev_day_volume,
        "prev_day_vwap": mover.prev_day_vwap,
        "fetched_at": mover.fetched_at.isoformat(),
    }


@app.get("/reports/top-movers")
def top_movers_report(ticker_types: str = "") -> list[dict]:
    """Backs the Analytics > Top Movers page's report grid: today's top_market_movers
    rows (both directions), each joined out to its tickers row for name/type and to its
    most recently computed average_volumes row. Filtering by ticker_types (0 or more,
    comma-separated codes from the same ticker_types reference list as the Jobs page's
    TickerType combobox) inner-joins instead of left-joins - a mover with no matching
    tickers row has no `type` to filter on anyway, so it can never satisfy a non-empty
    filter."""
    types = _split_csv(ticker_types)
    with SessionLocal() as session:
        query = select(TopMarketMover, Ticker).join(
            Ticker, Ticker.ticker == TopMarketMover.ticker, isouter=not types
        )
        if types:
            query = query.where(Ticker.type.in_(types))
        query = query.order_by(TopMarketMover.direction, TopMarketMover.rank)
        rows = session.execute(query).all()

        # Loaded once per request rather than joined in, since a ticker `type` code
        # (e.g. "CS") can appear across more than one ticker_types row (distinct
        # locales) - this just takes the first asset_class seen per code rather than
        # letting the join fan out duplicate mover rows.
        asset_class_by_code: dict[str, str] = {}
        for code, asset_class in session.execute(select(TickerType.code, TickerType.asset_class)).all():
            asset_class_by_code.setdefault(code, asset_class)

        # average_volumes accumulates one row per (ticker, start_date, days_interval) -
        # "latest" means the row from that ticker's most recent job run, so this joins
        # each ticker back to whichever row carries the max computed_at rather than the
        # max start_date (which, unlike computed_at, isn't unique per run if a ticker is
        # ever computed under more than one days_interval on the same start_date).
        latest_computed_at = select(
            AverageVolume.ticker, func.max(AverageVolume.computed_at).label("computed_at")
        ).group_by(AverageVolume.ticker)
        latest_computed_at = latest_computed_at.subquery()
        average_volume_by_ticker: dict[str, float | None] = {
            ticker: average_volume
            for ticker, average_volume in session.execute(
                select(AverageVolume.ticker, AverageVolume.average_volume).join(
                    latest_computed_at,
                    (AverageVolume.ticker == latest_computed_at.c.ticker)
                    & (AverageVolume.computed_at == latest_computed_at.c.computed_at),
                )
            ).all()
        }

        return [
            _mover_to_dict(
                mover,
                ticker,
                asset_class_by_code.get(ticker.type) if ticker and ticker.type else None,
                average_volume_by_ticker.get(mover.ticker),
            )
            for mover, ticker in rows
        ]


@app.get("/jobs")
def list_jobs() -> list[dict]:
    with SessionLocal() as session:
        return [_job_to_dict(session, name) for name in JOB_DEFINITIONS]


@app.get("/jobs/{job_name}")
def get_job(job_name: str) -> dict:
    _require_job(job_name)
    with SessionLocal() as session:
        return _job_to_dict(session, job_name)


@app.put("/jobs/{job_name}/config")
def update_job_config(job_name: str, body: JobConfigIn) -> dict:
    _require_job(job_name)
    if body.schedule_interval_value < 1:
        raise HTTPException(status_code=400, detail="schedule_interval_value must be at least 1")
    if body.ticker_types and body.tickers:
        raise HTTPException(status_code=400, detail="specify ticker_types or tickers, not both")
    if body.start_time not in START_TIME_OPTIONS:
        raise HTTPException(status_code=400, detail="start_time must be a quarter-hour UTC time, e.g. '00:15'")
    if body.snapshot_types and (
        invalid := set(_split_csv(body.snapshot_types) or []) - set(SNAPSHOT_TYPE_OPTIONS)
    ):
        raise HTTPException(status_code=400, detail=f"invalid snapshot_types: {', '.join(sorted(invalid))}")
    if body.average_volume_days_interval is not None and body.average_volume_days_interval < 1:
        raise HTTPException(status_code=400, detail="average_volume_days_interval must be at least 1")
    average_volume_start_date: dt.date | None = None
    if body.average_volume_start_date is not None:
        try:
            average_volume_start_date = dt.date.fromisoformat(body.average_volume_start_date)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="average_volume_start_date must be an ISO date, e.g. '2026-08-06'"
            ) from exc

    with SessionLocal() as session:
        definition = JOB_DEFINITIONS[job_name]
        config = _get_or_create_config(session, job_name)
        config.run_type = body.run_type
        config.schedule_interval_unit = body.schedule_interval_unit
        config.schedule_interval_value = body.schedule_interval_value
        config.start_time = body.start_time
        # ticker_types applies to jobs with has_ticker_type_filter (a single type
        # filter - see sync_tickers's ticker_type param) or has_ticker_selector (a
        # multi-select filter - see sync_bars/sync_snapshots's _resolve_tickers).
        # Dropped for a job like ticker-types sync that takes no run parameters at
        # all, even if the caller sent one.
        config.ticker_types = (
            body.ticker_types if (definition.has_ticker_type_filter or definition.has_ticker_selector) else None
        )
        # tickers is only meaningful alongside the multi-select ticker_types above.
        config.tickers = body.tickers if definition.has_ticker_selector else None
        if definition.has_bars_fields:
            config.multiplier = body.multiplier
            config.timespan = body.timespan
            config.backfill_days = body.backfill_days
        config.snapshot_types = body.snapshot_types if definition.has_snapshot_type_filter else None
        if definition.has_average_volume_fields:
            config.average_volume_start_date = average_volume_start_date
            config.average_volume_days_interval = body.average_volume_days_interval
        else:
            config.average_volume_start_date = None
            config.average_volume_days_interval = None
        config.updated_at = dt.datetime.utcnow()
        session.commit()

        # Reschedule before building the response dict - _job_to_dict reads the live
        # scheduler's next_run_time (see _next_run_time), so doing this after would
        # hand the caller a stale pre-reschedule value until their next GET /jobs.
        if _scheduler is not None:
            _scheduler.reschedule_job(
                job_name,
                trigger=_interval_trigger(body.schedule_interval_unit, body.schedule_interval_value, body.start_time),
            )
        result = _job_to_dict(session, job_name)

    return result


@app.post("/jobs/{job_name}/run")
def trigger_job(job_name: str, background_tasks: BackgroundTasks) -> dict:
    _require_job(job_name)
    if not _job_locks[job_name].acquire(blocking=False):
        raise HTTPException(status_code=409, detail=f"{job_name} is already running")
    background_tasks.add_task(_run_job, job_name, "manual")
    return {"status": "started"}


def _require_running(job_name: str) -> JobControl:
    """Shared guard for pause/resume/cancel below - all three only make sense against
    a run that's actually in flight (see _run_job, which is the only thing that ever
    populates _job_controls)."""
    _require_job(job_name)
    control = _job_controls.get(job_name)
    if control is None:
        raise HTTPException(status_code=409, detail=f"{job_name} is not running")
    return control


@app.post("/jobs/{job_name}/pause")
def pause_job(job_name: str) -> dict:
    control = _require_running(job_name)
    control.request_pause()
    return {"status": "pause-requested"}


@app.post("/jobs/{job_name}/resume")
def resume_job(job_name: str) -> dict:
    control = _require_running(job_name)
    control.request_resume()
    return {"status": "resumed"}


@app.post("/jobs/{job_name}/cancel")
def cancel_job(job_name: str) -> dict:
    control = _require_running(job_name)
    control.request_cancel()
    return {"status": "cancel-requested"}


@app.post("/jobs/{job_name}/hide")
def hide_job(job_name: str) -> dict:
    _require_job(job_name)
    with SessionLocal() as session:
        config = _get_or_create_config(session, job_name)
        config.hidden = True
        session.commit()
        return _job_to_dict(session, job_name)


@app.post("/jobs/{job_name}/unhide")
def unhide_job(job_name: str) -> dict:
    _require_job(job_name)
    with SessionLocal() as session:
        config = _get_or_create_config(session, job_name)
        config.hidden = False
        session.commit()
        return _job_to_dict(session, job_name)


@app.get("/jobs/{job_name}/runs")
def job_runs(job_name: str, limit: int = 20) -> list[dict]:
    _require_job(job_name)
    with SessionLocal() as session:
        rows = (
            session.execute(
                select(JobRun).where(JobRun.job_name == job_name).order_by(JobRun.started_at.desc()).limit(limit)
            )
            .scalars()
            .all()
        )
        return [_run_to_dict(run) for run in rows]
