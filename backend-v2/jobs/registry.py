"""Central metadata for backend-v2's scheduled jobs - one entry per job, shared by
app/main.py's API endpoints and its scheduler startup so the dashboard's Jobs page and
the cron schedule always agree on what jobs exist.

Job names double as APScheduler job ids and JobConfig/JobRun.job_name values.
"""

import dataclasses

TICKERS_JOB = "sync-tickers"
BARS_JOB = "sync-bars-nightly"

# (schedule_interval_unit, schedule_interval_value) - only used the first time a job's
# JobConfig row is created (see app/main.py's _get_or_create_config). Both jobs default
# to once a day, matching run_jobs.py's old hardcoded daily schedule.
DEFAULT_SCHEDULES: dict[str, tuple[str, int]] = {
    TICKERS_JOB: ("days", 1),
    BARS_JOB: ("days", 1),
}


@dataclasses.dataclass(frozen=True)
class JobDefinition:
    name: str
    label: str
    description: str
    # Whether tickers/multiplier/timespan/backfill_days apply to this job (bars-only).
    # ticker_types applies to both jobs regardless of this flag - a single type filter
    # for the tickers job (see jobs/sync_tickers.py's ticker_type param), a multi-select
    # filter for the bars job (see jobs/sync_bars.py's _resolve_tickers).
    has_bars_fields: bool


JOB_DEFINITIONS: dict[str, JobDefinition] = {
    TICKERS_JOB: JobDefinition(
        name=TICKERS_JOB,
        label="Sync tickers",
        description="Syncs GET /v3/reference/tickers into the tickers table.",
        has_bars_fields=False,
    ),
    BARS_JOB: JobDefinition(
        name=BARS_JOB,
        label="Sync bars (nightly)",
        description="Syncs OHLC bars through yesterday for every ticker not already up to date.",
        has_bars_fields=True,
    ),
}
