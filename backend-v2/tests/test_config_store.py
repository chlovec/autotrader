import datetime as dt

import pytest
from sqlalchemy import inspect

from db.models import JobConfig, JobRun
from db.session import SessionLocal, engine, init_db
from jobs.config_store import get_or_create_config, interval_trigger, job_is_active
from jobs.registry import PREDICT_MARKET_STATE_JOB


@pytest.fixture(autouse=True)
def _clean_db():
    init_db()
    session = SessionLocal()
    session.query(JobRun).delete()
    session.query(JobConfig).delete()
    session.commit()
    session.close()
    yield


def test_init_db_adds_run_requested_at_column():
    inspector = inspect(engine)
    columns = {col["name"] for col in inspector.get_columns("job_configs")}
    assert "run_requested_at" in columns


def test_init_db_adds_job_runs_control_columns():
    inspector = inspect(engine)
    columns = {col["name"] for col in inspector.get_columns("job_runs")}
    assert {"pause_requested", "cancel_requested"} <= columns


def test_get_or_create_config_seeds_a_new_row_with_run_requested_at_unset():
    session = SessionLocal()
    try:
        config = get_or_create_config(session, PREDICT_MARKET_STATE_JOB)
        assert config.run_requested_at is None
    finally:
        session.close()


def test_interval_trigger_anchors_to_config_updated_at_not_wall_clock_now():
    """The regression this guards against: job_runner.py (the process that actually
    schedules jobs) and app/main.py (which independently recomputes the same trigger to
    display next_run_time - see app/main.py's _next_run_time) must always agree on the
    trigger's phase. If this were anchored to "today" (dt.datetime.now()) instead of
    the config's own updated_at, the two processes could disagree - and for an interval
    that doesn't evenly divide a day (e.g. every 5 hours, exercised here), that
    disagreement would actually be a different set of fire times, not just cosmetic."""
    config = JobConfig(
        job_name="_test-anchor",
        run_type="auto",
        schedule_interval_unit="hours",
        schedule_interval_value=5,
        start_time="03:15",
        updated_at=dt.datetime(2020, 1, 1, 9, 0, 0),
    )
    trigger = interval_trigger(config)
    assert trigger.start_date == dt.datetime(2020, 1, 1, 3, 15, tzinfo=dt.timezone.utc)
    assert trigger.interval == dt.timedelta(hours=5)


def test_interval_trigger_is_deterministic_regardless_of_when_its_called():
    config = JobConfig(
        job_name="_test-determinism",
        run_type="auto",
        schedule_interval_unit="minutes",
        schedule_interval_value=17,
        start_time="00:05",
        updated_at=dt.datetime(2024, 6, 1, 12, 0, 0),
    )
    first = interval_trigger(config)
    second = interval_trigger(config)
    now = dt.datetime(2030, 3, 4, 5, 6, 7, tzinfo=dt.timezone.utc)
    assert first.get_next_fire_time(None, now) == second.get_next_fire_time(None, now)


def test_job_is_active_false_by_default():
    session = SessionLocal()
    try:
        assert job_is_active(session, PREDICT_MARKET_STATE_JOB) is False
    finally:
        session.close()


def test_job_is_active_true_when_run_requested():
    session = SessionLocal()
    try:
        config = get_or_create_config(session, PREDICT_MARKET_STATE_JOB)
        config.run_requested_at = dt.datetime.utcnow()
        session.commit()
        assert job_is_active(session, PREDICT_MARKET_STATE_JOB) is True
    finally:
        session.close()


def test_job_is_active_true_when_in_progress_run_exists():
    session = SessionLocal()
    try:
        session.add(
            JobRun(
                job_name=PREDICT_MARKET_STATE_JOB,
                trigger="manual",
                status="in_progress",
                started_at=dt.datetime.utcnow(),
            )
        )
        session.commit()
        assert job_is_active(session, PREDICT_MARKET_STATE_JOB) is True
    finally:
        session.close()


def test_job_is_active_false_when_run_is_completed():
    session = SessionLocal()
    try:
        session.add(
            JobRun(
                job_name=PREDICT_MARKET_STATE_JOB,
                trigger="manual",
                status="completed",
                started_at=dt.datetime.utcnow(),
                finished_at=dt.datetime.utcnow(),
            )
        )
        session.commit()
        assert job_is_active(session, PREDICT_MARKET_STATE_JOB) is False
    finally:
        session.close()
