import asyncio
import datetime as dt
import threading
import time

import pytest

from db.models import JobRun
from db.session import SessionLocal, engine, init_db
from jobs.control import JobCancelled, JobControl, report_job_progress


@pytest.fixture(autouse=True)
def _clean_db():
    init_db()
    session = SessionLocal()
    session.query(JobRun).delete()
    session.commit()
    session.close()
    yield


async def test_checkpoint_async_passes_through_when_idle():
    control = JobControl()
    await control.checkpoint_async()  # should not raise or block


async def test_checkpoint_async_raises_when_cancelled():
    control = JobControl()
    control.request_cancel()
    with pytest.raises(JobCancelled):
        await control.checkpoint_async()


async def test_checkpoint_async_blocks_until_resumed():
    control = JobControl()
    control.request_pause()
    resumed_before_checkpoint_returned = False

    async def resume_later():
        nonlocal resumed_before_checkpoint_returned
        await asyncio.sleep(0.05)
        control.request_resume()
        resumed_before_checkpoint_returned = True

    task = asyncio.create_task(resume_later())
    await control.checkpoint_async()

    assert resumed_before_checkpoint_returned
    await task


async def test_checkpoint_async_raises_if_cancelled_while_paused():
    control = JobControl()
    control.request_pause()

    async def cancel_later():
        await asyncio.sleep(0.05)
        control.request_cancel()

    task = asyncio.create_task(cancel_later())
    with pytest.raises(JobCancelled):
        await control.checkpoint_async()
    await task


def test_checkpoint_sync_passes_through_when_idle():
    JobControl().checkpoint_sync()  # should not raise or block


def test_checkpoint_sync_raises_when_cancelled():
    control = JobControl()
    control.request_cancel()
    with pytest.raises(JobCancelled):
        control.checkpoint_sync()


def test_checkpoint_sync_blocks_until_resumed():
    control = JobControl()
    control.request_pause()
    resumed_before_checkpoint_returned = False

    def resume_later():
        nonlocal resumed_before_checkpoint_returned
        time.sleep(0.05)
        control.request_resume()
        resumed_before_checkpoint_returned = True

    thread = threading.Thread(target=resume_later)
    thread.start()
    control.checkpoint_sync()

    assert resumed_before_checkpoint_returned
    thread.join()


def test_checkpoint_sync_raises_promptly_if_cancelled_while_paused():
    control = JobControl()
    control.request_pause()

    def cancel_later():
        time.sleep(0.05)
        control.request_cancel()

    thread = threading.Thread(target=cancel_later)
    thread.start()
    started_at = time.monotonic()
    with pytest.raises(JobCancelled):
        control.checkpoint_sync()
    elapsed = time.monotonic() - started_at

    # checkpoint_sync waits on the cancel Event itself (not a fixed poll sleep), so a
    # cancel requested mid-pause should wake it well before the next poll would.
    assert elapsed < 0.2
    thread.join()


def test_request_resume_is_a_noop_when_not_paused():
    control = JobControl()
    control.request_resume()
    assert not control.pause_requested


def test_request_cancel_also_clears_pause():
    control = JobControl()
    control.request_pause()
    control.request_cancel()
    assert control.cancel_requested
    assert not control.pause_requested


def _make_run() -> int:
    session = SessionLocal()
    run = JobRun(job_name="test-job", trigger="manual", status="in_progress", started_at=dt.datetime.utcnow())
    session.add(run)
    session.commit()
    run_id = run.id
    session.close()
    return run_id


def _progress(run_id: int) -> tuple[int | None, int | None]:
    session = SessionLocal()
    try:
        run = session.get(JobRun, run_id)
        return run.progress_completed, run.progress_total
    finally:
        session.close()


def test_report_job_progress_is_noop_when_run_id_is_none():
    session = SessionLocal()
    try:
        report_job_progress(session, None, 0, 10)  # should not raise
    finally:
        session.close()


def test_report_job_progress_always_writes_on_first_and_final_calls():
    run_id = _make_run()
    session = SessionLocal()
    try:
        report_job_progress(session, run_id, 0, 100)
        assert _progress(run_id) == (0, 100)

        report_job_progress(session, run_id, 100, 100)
        assert _progress(run_id) == (100, 100)
    finally:
        session.close()


def test_report_job_progress_throttles_intermediate_calls():
    run_id = _make_run()
    session = SessionLocal()
    try:
        report_job_progress(session, run_id, 0, 100)

        # Not a multiple of the commit interval and not the final value - skipped.
        report_job_progress(session, run_id, 1, 100)
        assert _progress(run_id) == (0, 100)

        # A multiple of the commit interval - written.
        report_job_progress(session, run_id, 25, 100)
        assert _progress(run_id) == (25, 100)

        # Not a multiple, not final - skipped again, even though it's later than 25.
        report_job_progress(session, run_id, 99, 100)
        assert _progress(run_id) == (25, 100)

        # The final value always writes regardless of the throttle.
        report_job_progress(session, run_id, 100, 100)
        assert _progress(run_id) == (100, 100)
    finally:
        session.close()


def test_report_job_progress_is_noop_when_run_missing():
    session = SessionLocal()
    try:
        report_job_progress(session, run_id=999_999, completed=0, total=10)  # should not raise
    finally:
        session.close()


def test_report_job_progress_skips_update_when_database_is_locked(caplog):
    """Another writer holding SQLite's write lock past busy_timeout (e.g.
    predict-market-state mid-batch) shouldn't fail the reporting job - the tick is
    skipped and the session stays usable for the next one."""
    run_id = _make_run()
    session = SessionLocal()
    holder = engine.raw_connection()
    try:
        # Fail fast instead of waiting out the real 30s busy_timeout.
        session.connection().exec_driver_sql("PRAGMA busy_timeout=0")
        holder.cursor().execute("BEGIN IMMEDIATE")

        report_job_progress(session, run_id, 5, 100, force=True)  # should not raise
        assert "database is locked" in caplog.text

        holder.rollback()
        assert _progress(run_id) != (5, 100)

        report_job_progress(session, run_id, 6, 100, force=True)
        assert _progress(run_id) == (6, 100)
    finally:
        holder.close()
        # Pooled connection - put the real timeout back for whichever test reuses it.
        session.connection().exec_driver_sql("PRAGMA busy_timeout=30000")
        session.close()
