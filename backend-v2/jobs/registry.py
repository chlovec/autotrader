"""Central metadata for backend-v2's scheduled jobs - one entry per job, shared by
app/main.py's API endpoints and its scheduler startup so the dashboard's Jobs page and
the cron schedule always agree on what jobs exist.

Job names double as APScheduler job ids and JobConfig/JobRun.job_name values.
"""

import dataclasses

TICKERS_JOB = "sync-tickers"
BARS_JOB = "sync-bars-nightly"
TICKER_TYPES_JOB = "sync-ticker-types"

# (schedule_interval_unit, schedule_interval_value) - only used the first time a job's
# JobConfig row is created (see app/main.py's _get_or_create_config). All jobs default
# to once a day, matching run_jobs.py's old hardcoded daily schedule.
DEFAULT_SCHEDULES: dict[str, tuple[str, int]] = {
    TICKERS_JOB: ("days", 1),
    BARS_JOB: ("days", 1),
    TICKER_TYPES_JOB: ("days", 1),
}


@dataclasses.dataclass(frozen=True)
class JobDefinition:
    name: str
    label: str
    description: str
    # Whether tickers/multiplier/timespan/backfill_days apply to this job (bars-only).
    has_bars_fields: bool
    # Whether ticker_types applies to this job at all - a single type filter for the
    # tickers job (see jobs/sync_tickers.py's ticker_type param) or a multi-select
    # filter for the bars job (see jobs/sync_bars.py's _resolve_tickers). False for a
    # job like ticker-types sync that takes no run parameters whatsoever - see
    # app/main.py's update_job_config, which drops ticker_types on the floor for those.
    has_ticker_type_filter: bool = True
    # Seeded into JobConfig.run_type the first time this job's config row is created
    # (see app/main.py's _get_or_create_config). "auto" unless overridden below.
    default_run_type: str = "auto"


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
    TICKER_TYPES_JOB: JobDefinition(
        name=TICKER_TYPES_JOB,
        label="Sync ticker types",
        description="Syncs GET /v3/reference/tickers/types into the ticker_types table.",
        has_bars_fields=False,
        # No filter to offer - sync_ticker_types always fetches the whole reference list.
        has_ticker_type_filter=False,
        # Reference data that rarely changes - manual by default so it isn't fired
        # daily by the scheduler like the tickers/bars jobs are.
        default_run_type="manual",
    ),
}
