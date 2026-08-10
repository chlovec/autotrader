"""backend-v2's job-execution process - runs every job in jobs/registry.py's
JOB_DEFINITIONS on its own schedule (see jobs/engine.py for the actual execution
logic). Deliberately separate from run_jobs.py (the FastAPI dashboard API process):
the two used to be one process, but a heavy job's CPU/GIL usage and DB writes could
then make the dashboard unresponsive while it ran (see db/session.py's WAL-mode
comment for that history). They now coordinate purely through the database - see
jobs/engine.py's module docstring - so neither process's lifetime or load affects the
other's.

Usage (from backend-v2/, matching how bin/restart-v2.sh runs it):
    python job_runner.py

Env: everything app/main.py itself reads except CORS_ORIGINS (BACKEND_V2_DATABASE_URL,
MASSIVE_API_KEY, MASSIVE_*_SECONDS, ...) - this process serves no HTTP of its own.
"""

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from dotenv import load_dotenv

from db.session import SessionLocal, init_db
from jobs.config_store import get_or_create_config, interval_trigger
from jobs.engine import (
    _last_scheduled,
    poll_control_relay,
    poll_run_requests,
    reconcile_orphaned_runs,
    resync_schedules,
    run_startup_sync_chain,
    schedule_key,
    scheduled_job,
)
from jobs.registry import JOB_DEFINITIONS

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("backend_v2.job_runner")

# How often each poll job below re-checks the DB for cross-process signals from
# app/main.py - see jobs/engine.py's module docstring for why polling (rather than a
# direct call/socket) is enough here.
RUN_REQUEST_POLL_SECONDS = 2
CONTROL_RELAY_POLL_SECONDS = 1
SCHEDULE_RESYNC_POLL_SECONDS = 5

# Held so the startup sync chain's task can't be garbage-collected mid-run - same
# "weakly referenced otherwise" pitfall jobs/engine.py's _running_tasks guards against
# for run-request-triggered tasks.
_startup_sync_task: asyncio.Task | None = None


async def main() -> None:
    global _startup_sync_task
    init_db()

    reconciled = reconcile_orphaned_runs()
    if reconciled:
        logger.warning(
            "reconciled %d job run(s) left in_progress by a previous instance - marked failed", reconciled
        )

    with SessionLocal() as session:
        schedules = {name: get_or_create_config(session, name) for name in JOB_DEFINITIONS}

    # Background, not awaited: this chain can take a long time (thousands of tickers,
    # one HTTP call each, plus 429 backoff), and the scheduler below - including the
    # poll jobs app/main.py depends on to see run/pause/cancel requests - shouldn't
    # sit idle waiting for it.
    _startup_sync_task = asyncio.create_task(run_startup_sync_chain())

    scheduler = AsyncIOScheduler()
    for name, config in schedules.items():
        scheduler.add_job(scheduled_job, interval_trigger(config), args=[name], id=name)
        _last_scheduled[name] = schedule_key(config)
        logger.info(
            "scheduled %s every %d %s starting %s UTC",
            name,
            config.schedule_interval_value,
            config.schedule_interval_unit,
            config.start_time,
        )

    scheduler.add_job(poll_run_requests, IntervalTrigger(seconds=RUN_REQUEST_POLL_SECONDS), id="_poll_run_requests")
    scheduler.add_job(
        poll_control_relay, IntervalTrigger(seconds=CONTROL_RELAY_POLL_SECONDS), id="_poll_control_relay"
    )
    scheduler.add_job(
        lambda: resync_schedules(scheduler),
        IntervalTrigger(seconds=SCHEDULE_RESYNC_POLL_SECONDS),
        id="_resync_schedules",
    )

    scheduler.start()
    logger.info("job_runner started")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
