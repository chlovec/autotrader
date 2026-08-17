"""Cooperative pause/cancel signal for one running job (see app/main.py's _run_job and
its _job_controls registry). A job's loop calls checkpoint_async() (jobs that run
directly on the FastAPI event loop - sync_tickers, sync_ticker_types, sync_bars) or
checkpoint_sync() (jobs that run off it, on their own thread - sync_snapshots' per-
ticker ThreadPoolExecutor workers) between safely-interruptible units of work (a page,
a ticker, ...).

A paused checkpoint blocks in place - same call stack, same DB session, same
_job_locks entry held throughout - so resuming just continues the loop exactly where
it left off; there's no separate "resume" code path to get right. A cancelled
checkpoint raises JobCancelled, which unwinds the whole run. Whatever that job already
committed to the database along the way (sync_tickers' SyncProgress cursor, sync_bars'
own upserted ohlc_bars rows - see jobs/sync_bars.py's _last_bar_dates) is what the
*next* run resumes from - cancellation doesn't need its own resumption bookkeeping on
top of that.
"""

import asyncio
import threading

from sqlalchemy.orm import Session

from db.models import JobRun

# How often a blocked checkpoint re-checks pause/cancel state. Polling instead of a
# single blocking wait because there's no "wait on any of several Events" primitive in
# the stdlib threading module - this is simple, correct, and plenty responsive for a
# dashboard operated by a human (worst case: a fifth of a second to notice a resume or
# a cancel requested while still paused).
_POLL_INTERVAL_SECONDS = 0.2

# How often report_job_progress actually commits, in units of work completed - a large
# fan-out (e.g. sync-bars-nightly's ~46k tickers) would otherwise commit on every
# single completion.
_PROGRESS_COMMIT_INTERVAL = 25


class JobCancelled(Exception):
    """Raised by JobControl.checkpoint_async/checkpoint_sync once a cancel has been
    requested, so a job's loop can unwind instead of continuing to fetch/upsert once
    nobody wants the run to keep going."""


class JobControl:
    """One instance per in-flight job run (created and discarded by app/main.py's
    _run_job). Backed by threading.Event rather than asyncio.Event: sync_snapshots'
    worker threads each spin up their own event loop (see jobs/sync_snapshots.py's
    _fetch_and_store_one), and an asyncio.Event created on one loop can't be awaited
    safely from another - but Event.is_set()/set()/clear()/wait() are thread-safe no
    matter which thread calls them, which is all checkpoint_async/checkpoint_sync need.
    """

    def __init__(self) -> None:
        self._pause = threading.Event()
        self._cancel = threading.Event()

    def request_pause(self) -> None:
        self._pause.set()

    def request_resume(self) -> None:
        self._pause.clear()

    def request_cancel(self) -> None:
        # Clearing _pause too unblocks anything parked in a pause wait so it observes
        # the cancellation immediately instead of waiting for a resume that isn't coming.
        self._cancel.set()
        self._pause.clear()

    @property
    def pause_requested(self) -> bool:
        return self._pause.is_set()

    @property
    def cancel_requested(self) -> bool:
        return self._cancel.is_set()

    async def checkpoint_async(self) -> None:
        """For jobs running directly on the event loop. Never blocks the loop itself -
        parks via asyncio.sleep, not a synchronous wait."""
        if self._cancel.is_set():
            raise JobCancelled()
        while self._pause.is_set():
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)
            if self._cancel.is_set():
                raise JobCancelled()

    def checkpoint_sync(self) -> None:
        """For jobs running off the event loop, on their own thread - safe to block
        the calling thread here. Waits on _cancel (not a plain sleep) so a cancel
        requested while paused wakes this up immediately rather than up to
        _POLL_INTERVAL_SECONDS late."""
        if self._cancel.is_set():
            raise JobCancelled()
        while self._pause.is_set():
            if self._cancel.wait(timeout=_POLL_INTERVAL_SECONDS):
                raise JobCancelled()


def report_job_progress(
    session: Session, run_id: int | None, completed: int, total: int, *, force: bool = False
) -> None:
    """Persists (completed, total) onto the JobRun row identified by run_id, so the
    dashboard's Jobs page can show a live "N / M" progress bar (see app/main.py's
    _run_to_dict) - throttled to every _PROGRESS_COMMIT_INTERVAL calls, plus always on
    the final one, so a large fan-out doesn't commit on every single completion.

    Called from the single consumer thread driving each of sync_bars.py/
    sync_snapshots.py/sync_ticker_details.py/sync_indicators.py's
    concurrent.futures.as_completed loop - `session` is the same one those functions
    already receive for their own (non-per-ticker) reads/writes, otherwise idle for
    the duration of that loop, so reusing it here doesn't race any worker thread's own
    SessionLocal().

    force=True bypasses the throttle and always commits - for callers whose own units
    of work are already coarse-grained and individually slow enough (a training epoch,
    a walk-forward fold - see jobs/train_lstm_holdout.py/jobs/train_lstm_walkforward.py)
    that a total under _PROGRESS_COMMIT_INTERVAL would otherwise mean every update
    except the very last gets silently dropped by the modulo check below, leaving the
    dashboard's progress bar frozen at 0 until the run's final unit completes.

    No-op if run_id is None - callers (e.g. sync_bars_manual's CLI entry point) that
    don't have a JobRun row simply don't get progress tracked."""
    if run_id is None:
        return
    if not force and completed != total and completed % _PROGRESS_COMMIT_INTERVAL != 0:
        return
    run = session.get(JobRun, run_id)
    if run is None:
        return
    run.progress_completed = completed
    run.progress_total = total
    session.commit()
