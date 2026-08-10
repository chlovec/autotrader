"""Shared, lightweight helpers both app/main.py (the API process) and jobs/engine.py
(job_runner.py's execution process) need against JobConfig/JobRun - deliberately kept
free of DataClient/JobControl/sync_*/compute_* imports so app/main.py importing this
module doesn't drag the whole job-execution stack back into the API process.
"""

import datetime as dt

from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import JobConfig, JobRun
from jobs.registry import BARS_JOB, DEFAULT_SCHEDULES, DEFAULT_START_TIME, JOB_DEFINITIONS
from jobs.sync_bars import DEFAULT_BACKFILL_DAYS, DEFAULT_MULTIPLIER, DEFAULT_TIMESPAN


def split_csv(value: str | None) -> list[str] | None:
    if not value:
        return None
    items = [item.strip() for item in value.split(",") if item.strip()]
    return items or None


def get_or_create_config(session: Session, job_name: str) -> JobConfig:
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


def interval_trigger(config: JobConfig) -> IntervalTrigger:
    """`schedule_interval_unit` ("minutes"/"hours"/"days") doubles as the IntervalTrigger
    keyword arg name. `start_time` ("HH:MM" UTC, one of registry.START_TIME_OPTIONS)
    anchors the trigger's phase via start_date, combined with `updated_at`'s *date* -
    not "today" - so that this function returns the exact same trigger (and therefore
    the exact same fire-time schedule) no matter which process or which day calls it.

    That distinction only matters for interval units that don't evenly divide a
    calendar day (e.g. "every 5 hours", "every 17 minutes" - both allowed today,
    schedule_interval_value isn't validated against divisibility): for those, anchoring
    to "today at call time" would give a different phase depending on which day you
    call it from, which would make job_runner.py's live schedule (anchored once, at
    whatever day it was last (re)scheduled) and app/main.py's independently-recomputed
    next_run_time display (see app/main.py's _next_run_time) silently disagree.
    updated_at already records the literal day this config was last saved, so using its
    date as the anchor is both processes reading the identical value - they can't drift
    apart - and is a strict improvement over "today," which was never actually correct
    for non-24h-divisible intervals to begin with."""
    hour, minute = (int(part) for part in config.start_time.split(":"))
    start_date = dt.datetime.combine(
        config.updated_at.date(), dt.time(hour, minute), tzinfo=dt.timezone.utc
    )
    return IntervalTrigger(
        **{config.schedule_interval_unit: config.schedule_interval_value},
        start_date=start_date,
        timezone="UTC",
    )


def job_is_active(session: Session, job_name: str) -> bool:
    """True if job_name has a pending manual run request (JobConfig.run_requested_at -
    set by app/main.py's trigger_job, cleared by job_runner.py's poll_run_requests once
    it picks the request up) or an in_progress JobRun row - the two states between them
    cover "queued" and "actually running" without needing a live lock object shared
    across processes. Single source of truth for the "is this job running" check
    trigger_job/reset_job/_job_to_dict's "running" field in app/main.py all need."""
    config = get_or_create_config(session, job_name)
    if config.run_requested_at is not None:
        return True
    return (
        session.execute(
            select(JobRun.id).where(JobRun.job_name == job_name, JobRun.status == "in_progress").limit(1)
        ).first()
        is not None
    )
