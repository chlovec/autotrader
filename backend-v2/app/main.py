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
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from data.client import DataClient
from db.models import JobConfig, JobRun, Ticker, TickerType
from db.session import SessionLocal, init_db
from jobs.registry import BARS_JOB, DEFAULT_SCHEDULES, JOB_DEFINITIONS, TICKER_TYPES_JOB, TICKERS_JOB
from jobs.sync_bars import DEFAULT_BACKFILL_DAYS, DEFAULT_MULTIPLIER, DEFAULT_TIMESPAN, sync_bars_nightly
from jobs.sync_ticker_types import sync_ticker_types
from jobs.sync_tickers import sync_tickers

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
        multiplier=DEFAULT_MULTIPLIER if job_name == BARS_JOB else None,
        timespan=DEFAULT_TIMESPAN if job_name == BARS_JOB else None,
        backfill_days=DEFAULT_BACKFILL_DAYS if job_name == BARS_JOB else None,
    )
    session.add(config)
    session.commit()
    return config


def _interval_trigger(unit: str, value: int) -> IntervalTrigger:
    """`unit` ("minutes"/"hours"/"days") doubles as the IntervalTrigger keyword arg name -
    see db/models.py's JobConfig.schedule_interval_unit."""
    return IntervalTrigger(**{unit: value}, timezone="UTC")


def _split_csv(value: str | None) -> list[str] | None:
    if not value:
        return None
    items = [item.strip() for item in value.split(",") if item.strip()]
    return items or None


async def _run_job(job_name: str, trigger: str) -> None:
    """Assumes the caller already holds _job_locks[job_name]; releases it when done.
    `trigger` is "manual" or "auto" - see db/models.py's JobRun."""
    session = SessionLocal()
    run = JobRun(job_name=job_name, trigger=trigger, status="in_progress", started_at=dt.datetime.utcnow())
    session.add(run)
    session.commit()
    run_id = run.id
    try:
        config = _get_or_create_config(session, job_name)
        async with DataClient() as client:
            if job_name == TICKERS_JOB:
                count = await sync_tickers(client, session, ticker_type=config.ticker_types or None)
                summary = f"{count} ticker(s) synced"
            elif job_name == TICKER_TYPES_JOB:
                count = await sync_ticker_types(client, session)
                summary = f"{count} ticker type(s) synced"
            else:
                results = await sync_bars_nightly(
                    client,
                    session,
                    _split_csv(config.ticker_types),
                    _split_csv(config.tickers),
                    multiplier=config.multiplier or DEFAULT_MULTIPLIER,
                    timespan=config.timespan or DEFAULT_TIMESPAN,
                    backfill_days=config.backfill_days or DEFAULT_BACKFILL_DAYS,
                )
                summary = f"{len(results)} ticker(s) synced, {sum(results.values())} bar(s) fetched"
        run = session.get(JobRun, run_id)
        assert run is not None
        run.status = "completed"
        run.result_summary = summary
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
            _interval_trigger(config.schedule_interval_unit, config.schedule_interval_value),
            args=[name],
            id=name,
        )
        logger.info(
            "scheduled %s every %d %s", name, config.schedule_interval_value, config.schedule_interval_unit
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
        "run_type": config.run_type,
        "schedule_interval_unit": config.schedule_interval_unit,
        "schedule_interval_value": config.schedule_interval_value,
        "next_run_time": _next_run_time(job_name, config.run_type),
        "ticker_types": config.ticker_types,
        "tickers": config.tickers,
        "multiplier": config.multiplier,
        "timespan": config.timespan,
        "backfill_days": config.backfill_days,
        "running": _job_locks[job_name].locked(),
        "last_run": _run_to_dict(last_run) if last_run is not None else None,
    }


def _require_job(job_name: str) -> None:
    if job_name not in JOB_DEFINITIONS:
        raise HTTPException(status_code=404, detail=f"unknown job {job_name!r}")


class JobConfigIn(BaseModel):
    run_type: Literal["manual", "auto"]
    schedule_interval_unit: Literal["minutes", "hours", "days"]
    schedule_interval_value: int
    ticker_types: str | None = None
    tickers: str | None = None
    multiplier: int | None = None
    timespan: str | None = None
    backfill_days: int | None = None


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

    with SessionLocal() as session:
        config = _get_or_create_config(session, job_name)
        config.run_type = body.run_type
        config.schedule_interval_unit = body.schedule_interval_unit
        config.schedule_interval_value = body.schedule_interval_value
        # ticker_types applies to jobs with has_ticker_type_filter - a single type
        # filter for the tickers job (see sync_tickers's ticker_type param), a
        # multi-select filter for the bars job. Dropped for a job like ticker-types
        # sync that takes no run parameters at all, even if the caller sent one.
        # tickers/multiplier/timespan/backfill_days stay bars-only.
        config.ticker_types = body.ticker_types if JOB_DEFINITIONS[job_name].has_ticker_type_filter else None
        if JOB_DEFINITIONS[job_name].has_bars_fields:
            config.tickers = body.tickers
            config.multiplier = body.multiplier
            config.timespan = body.timespan
            config.backfill_days = body.backfill_days
        config.updated_at = dt.datetime.utcnow()
        session.commit()

        # Reschedule before building the response dict - _job_to_dict reads the live
        # scheduler's next_run_time (see _next_run_time), so doing this after would
        # hand the caller a stale pre-reschedule value until their next GET /jobs.
        if _scheduler is not None:
            _scheduler.reschedule_job(
                job_name, trigger=_interval_trigger(body.schedule_interval_unit, body.schedule_interval_value)
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
