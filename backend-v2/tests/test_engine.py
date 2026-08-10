import datetime as dt

import pytest

from db.models import JobRun
from db.session import SessionLocal, init_db
from jobs.engine import reconcile_orphaned_runs
from jobs.registry import PREDICT_MARKET_STATE_JOB


@pytest.fixture(autouse=True)
def _clean_db():
    init_db()
    session = SessionLocal()
    session.query(JobRun).delete()
    session.commit()
    session.close()
    yield


def test_reconcile_marks_in_progress_runs_as_failed():
    session = SessionLocal()
    try:
        session.add(
            JobRun(
                job_name=PREDICT_MARKET_STATE_JOB,
                trigger="manual",
                status="in_progress",
                started_at=dt.datetime.utcnow(),
                progress_completed=2250,
                progress_total=37216,
            )
        )
        session.commit()

        reconciled = reconcile_orphaned_runs()
        assert reconciled == 1

        run = session.query(JobRun).filter_by(job_name=PREDICT_MARKET_STATE_JOB).one()
        assert run.status == "failed"
        assert run.error is not None
        assert run.finished_at is not None
        # Progress from the orphaned attempt is left as-is - it's historical context for
        # why the run failed, not something reconcile_orphaned_runs needs to touch.
        assert run.progress_completed == 2250
    finally:
        session.close()


def test_reconcile_leaves_completed_and_failed_runs_alone():
    session = SessionLocal()
    try:
        session.add(
            JobRun(
                job_name=PREDICT_MARKET_STATE_JOB,
                trigger="manual",
                status="completed",
                started_at=dt.datetime.utcnow(),
                finished_at=dt.datetime.utcnow(),
                result_summary="0 ticker(s) market state predicted",
            )
        )
        session.commit()

        reconciled = reconcile_orphaned_runs()
        assert reconciled == 0

        run = session.query(JobRun).filter_by(job_name=PREDICT_MARKET_STATE_JOB).one()
        assert run.status == "completed"
    finally:
        session.close()


def test_reconcile_is_a_noop_with_no_runs():
    assert reconcile_orphaned_runs() == 0
