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
checkpoints to the database along the way (sync_tickers' SyncProgress cursor,
sync_bars' TickerBarSyncState) is what the *next* run resumes from - cancellation
doesn't need its own resumption bookkeeping on top of that.
"""

import asyncio
import threading

# How often a blocked checkpoint re-checks pause/cancel state. Polling instead of a
# single blocking wait because there's no "wait on any of several Events" primitive in
# the stdlib threading module - this is simple, correct, and plenty responsive for a
# dashboard operated by a human (worst case: a fifth of a second to notice a resume or
# a cancel requested while still paused).
_POLL_INTERVAL_SECONDS = 0.2


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
