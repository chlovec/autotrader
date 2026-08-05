"""Standalone process for backend-v2's scheduled jobs. Mirrors engine/multi_runner.py's
shape at the repo root: run once immediately on startup (so a freshly-provisioned db
doesn't sit empty until the next scheduled slot), then keep running it on a cron
schedule.
"""

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from jobs.sync_bars import run_nightly as run_bars_nightly
from jobs.sync_tickers import run_once as run_tickers_once

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("backend_v2.run_jobs")


async def main(tickers_hour: int = 1, tickers_minute: int = 0, bars_hour: int = 2, bars_minute: int = 0) -> None:
    # Sequential, not parallel: the bars job selects tickers out of the tickers table,
    # so a startup run needs tickers synced first, not racing it.
    await run_tickers_once()
    await run_bars_nightly()

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        run_tickers_once, CronTrigger(hour=tickers_hour, minute=tickers_minute, timezone="UTC"), id="sync-tickers"
    )
    scheduler.add_job(
        run_bars_nightly, CronTrigger(hour=bars_hour, minute=bars_minute, timezone="UTC"), id="sync-bars-nightly"
    )
    scheduler.start()
    logger.info("scheduled tickers sync daily at %02d:%02d UTC", tickers_hour, tickers_minute)
    logger.info("scheduled nightly bars sync daily at %02d:%02d UTC", bars_hour, bars_minute)

    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
