"""Central metadata for backend-v2's scheduled jobs - one entry per job, shared by
app/main.py's API endpoints and its scheduler startup so the dashboard's Jobs page and
the cron schedule always agree on what jobs exist.

Job names double as APScheduler job ids and JobConfig/JobRun.job_name values.
"""

import dataclasses

TICKERS_JOB = "sync-tickers"
BARS_JOB = "sync-bars-nightly"
TICKER_TYPES_JOB = "sync-ticker-types"
SNAPSHOTS_JOB = "sync-snapshots"

# Quarter-hour UTC time-of-day options for JobConfig.start_time - the dashboard's "Start
# time" select for an auto job (app/main.py's _interval_trigger anchors the recurring
# IntervalTrigger's phase to this) offers exactly these 96 slots, "00:00".."23:45".
START_TIME_OPTIONS: list[str] = [f"{hour:02d}:{minute:02d}" for hour in range(24) for minute in (0, 15, 30, 45)]
DEFAULT_START_TIME = "00:00"

# (schedule_interval_unit, schedule_interval_value) - only used the first time a job's
# JobConfig row is created (see app/main.py's _get_or_create_config). All jobs default
# to once a day, matching run_jobs.py's old hardcoded daily schedule.
DEFAULT_SCHEDULES: dict[str, tuple[str, int]] = {
    TICKERS_JOB: ("days", 1),
    BARS_JOB: ("days", 1),
    TICKER_TYPES_JOB: ("days", 1),
    SNAPSHOTS_JOB: ("days", 1),
}


@dataclasses.dataclass(frozen=True)
class JobDefinition:
    name: str
    label: str
    description: str
    # Whether multiplier/timespan/backfill_days apply to this job (bars-only).
    has_bars_fields: bool
    # Whether ticker_types applies to this job as a *single* type filter - the tickers
    # job's only selection mechanism (see jobs/sync_tickers.py's ticker_type param).
    # Superseded by has_ticker_selector below when both are true - see JobCard.tsx's
    # render order. False for a job like ticker-types sync that takes no run
    # parameters whatsoever - see app/main.py's update_job_config, which drops
    # ticker_types on the floor for those.
    has_ticker_type_filter: bool = True
    # Whether this job offers the multi-select "Ticker types" + "Tickers" pair (see
    # jobs/sync_bars.py's/jobs/sync_snapshots.py's _resolve_tickers, both of which
    # accept a *list* for either) - mutually exclusive, leaving both blank selects
    # every known ticker. Distinct from has_bars_fields so a job like sync-snapshots
    # can offer this picker without also carrying bars-only multiplier/timespan/
    # backfill_days fields.
    has_ticker_selector: bool = False
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
        has_ticker_selector=True,
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
    SNAPSHOTS_JOB: JobDefinition(
        name=SNAPSHOTS_JOB,
        label="Sync current snapshots",
        description=(
            "Syncs GET /v2/snapshot/locale/us/markets/stocks/tickers/{ticker} into the "
            "current_snapshots table for the selected tickers or ticker types."
        ),
        has_bars_fields=False,
        has_ticker_selector=True,
        # Fetches one ticker at a time with no natural daily cadence of its own -
        # manual by default, same reasoning as ticker-types.
        default_run_type="manual",
    ),
}
