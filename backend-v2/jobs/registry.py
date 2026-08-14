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
TICKER_DETAILS_JOB = "sync-ticker-details"
MOVERS_JOB = "sync-top-movers"
UNIFIED_SNAPSHOT_JOB = "sync-unified-snapshot"
NEWS_JOB = "sync-news"
SMA_JOB = "sync-sma"
EMA_JOB = "sync-ema"
MACD_JOB = "sync-macd"
RSI_JOB = "sync-rsi"
AVERAGE_VOLUME_JOB = "average-volume"
# Runs jobs/predict_market_state.py's Markov chain prediction followed immediately by
# jobs/predict_market_state_mcmc.py's Monte Carlo simulation over that same chain, in
# that order, in a single job run - see JOB_DEFINITIONS' entry below and
# jobs/engine.py's run_job. There is no separate "predict-market-state-mcmc" job id;
# the Monte Carlo phase is folded into this one.
PREDICT_MARKET_STATE_JOB = "predict-market-state"
BACKTEST_MARKET_STATE_JOB = "backtest-market-state"
PREDICT_10_DAY_MARKET_STATE_JOB = "predict-10-day-market-state"
WIN_RATE_JOB = "compute-win-rates"
RESEARCH_PICKS_JOB = "research-picks"

# job name -> massive.com indicator path segment (GET /v1/indicators/{indicator}/
# {ticker}) - jobs/sync_indicators.py's sync_indicator takes the latter, app/main.py's
# _run_job looks up which of the four a given job name is via this dict.
INDICATOR_NAMES: dict[str, str] = {
    SMA_JOB: "sma",
    EMA_JOB: "ema",
    MACD_JOB: "macd",
    RSI_JOB: "rsi",
}

# massive.com's own asset-class filter for GET /v3/snapshot (jobs/sync_unified_snapshot.py) -
# unrelated to the tickers table's `type` column (CS, ETF, ...) that ticker_types/
# has_ticker_type_filter/has_ticker_selector filter against elsewhere in this file.
SNAPSHOT_TYPE_OPTIONS: list[str] = ["stocks", "options", "indices", "fx", "crypto"]

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
    TICKER_DETAILS_JOB: ("days", 1),
    MOVERS_JOB: ("days", 1),
    UNIFIED_SNAPSHOT_JOB: ("days", 1),
    NEWS_JOB: ("days", 1),
    SMA_JOB: ("days", 1),
    EMA_JOB: ("days", 1),
    MACD_JOB: ("days", 1),
    RSI_JOB: ("days", 1),
    AVERAGE_VOLUME_JOB: ("days", 1),
    PREDICT_MARKET_STATE_JOB: ("days", 1),
    BACKTEST_MARKET_STATE_JOB: ("days", 1),
    PREDICT_10_DAY_MARKET_STATE_JOB: ("days", 1),
    WIN_RATE_JOB: ("days", 1),
    RESEARCH_PICKS_JOB: ("days", 1),
}


@dataclasses.dataclass(frozen=True)
class JobDefinition:
    name: str
    label: str
    description: str
    # Whether multiplier/timespan/backfill_days/bars_end_date_offset_days apply to
    # this job (bars-only). bars_end_date_offset_days is a signed offset in days from
    # today (UTC) - e.g. 1 for "through yesterday" (the historical default), 0 for
    # "through today" - resolved to a concrete end date once per run by
    # jobs/sync_bars.py's sync_bars_nightly, same "left None, resolved at run time"
    # reasoning backfill_days already uses.
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
    # Whether this job offers the asset-class "Snapshot types" multi-select (see
    # SNAPSHOT_TYPE_OPTIONS above and jobs/sync_unified_snapshot.py) - only the
    # unified-snapshot job takes this; every other job leaves it False. Unlike
    # has_ticker_type_filter/has_ticker_selector this doesn't touch the tickers table
    # at all, so it's an independent flag rather than layered onto those.
    has_snapshot_type_filter: bool = False
    # Whether this job offers the "Start date"/"Days interval" pair (see
    # jobs/average_volume.py) - only the average-volume job takes this. Independent of
    # the other flags: it doesn't touch the tickers table's type filter at all, though
    # the average-volume job also layers has_ticker_selector on top to scope which
    # tickers get computed.
    has_average_volume_fields: bool = False
    # Whether this job offers the "Start date"/"End date" pair (see
    # jobs/backtest_market_state.py) - only the backtest job takes this. Independent of
    # the other flags, same layering as has_average_volume_fields: the backtest job
    # also sets has_ticker_selector to scope which tickers get backtested.
    has_backtest_fields: bool = False
    # Whether this job offers a single "Start date" field (see
    # jobs/predict_market_state_10_day.py) - only the 10-day-prediction job takes this.
    # Unlike has_backtest_fields, just one date (the prediction's own start date, not a
    # range) - independent of the other flags, same layering as has_average_volume_fields.
    has_prediction_start_date_field: bool = False
    # Whether this job offers a "Predicted date" field expressed as an offset in days
    # from today (e.g. +1 for tomorrow, 0 for today, -1 for yesterday) rather than a
    # literal date - only the predict-market-state job takes this. jobs/engine.py's
    # run_job resolves the offset to a concrete date once per run and feeds that same
    # date to both phases of that job (see has_monte_carlo_fields below), so the
    # Markov chain and Monte Carlo predictions it stores always target the same
    # session. Never coexists with has_prediction_start_date_field above - each
    # caller-chosen-date job takes exactly one of the two field shapes.
    has_predicted_date_offset_field: bool = False
    # Whether this job offers a "Simulated paths" field (see
    # jobs/predict_market_state_mcmc.py) - only the predict-market-state job takes
    # this, for the Monte Carlo phase it runs immediately after its deterministic
    # Markov chain phase (see PREDICT_MARKET_STATE_JOB's description and
    # jobs/engine.py's run_job). Independent of the other flags; always paired with
    # has_predicted_date_offset_field above, since both phases share the same
    # resolved predicted date.
    has_monte_carlo_fields: bool = False
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
        description=(
            "Syncs OHLC bars through today minus the End date offset field (default: "
            "1, i.e. through yesterday) for every ticker not already up to date."
        ),
        has_bars_fields=True,
        has_ticker_selector=True,
    ),
    PREDICT_MARKET_STATE_JOB: JobDefinition(
        name=PREDICT_MARKET_STATE_JOB,
        label="Predict market state",
        description=(
            "Fits a first-order Markov chain per selected ticker on its history of "
            "discretized daily ohlc_bars returns for a chosen predicted date (default: "
            "tomorrow, UTC, expressed as an offset in days from today - see the "
            "Predicted date field), storing each ticker's predicted state "
            "(strong_down/down/flat/up/strong_up), a confidence score, and a projected "
            "entry/exit price in the market_predictions table - then immediately runs "
            "a Monte Carlo simulation over that same fitted chain for the same "
            "predicted date, where each of a configurable number of simulated paths "
            "draws a next state weighted by the fitted transition probabilities "
            "(rather than always the most likely one) and a return from that state's "
            "own historical mean/standard deviation, storing the resulting simulated "
            "exit-price distribution - mean, standard deviation, and 10th/50th/90th "
            "percentile bands - per ticker in the market_predictions_mcmc table. The "
            "two phases always run in that order within a single job run, sharing the "
            "same ticker selection, predicted date, and bar history. Purely local - no "
            "massive.com call, reads bars already synced by the bars job, and only "
            "bars from before the predicted date. Entry/exit time are fixed to the "
            "regular session's open/close, not predicted - daily bars carry no "
            "intraday timestamp to predict from."
        ),
        has_bars_fields=False,
        has_ticker_selector=True,
        has_predicted_date_offset_field=True,
        has_monte_carlo_fields=True,
        # Run-on-demand statistic over already-synced bars, no natural daily cadence of
        # its own - manual by default, same reasoning as average-volume.
        default_run_type="manual",
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
    TICKER_DETAILS_JOB: JobDefinition(
        name=TICKER_DETAILS_JOB,
        label="Sync ticker details",
        description=(
            "Syncs GET /v3/reference/tickers/{ticker} into the ticker_details table for "
            "the selected tickers or ticker types - market_cap, shares outstanding, "
            "SIC code/description, and other company fundamentals not carried by the "
            "sync-tickers job's paged list endpoint."
        ),
        has_bars_fields=False,
        has_ticker_selector=True,
        # Fetches one ticker at a time with no natural daily cadence of its own -
        # manual by default, same reasoning as snapshots.
        default_run_type="manual",
    ),
    MOVERS_JOB: JobDefinition(
        name=MOVERS_JOB,
        label="Sync top market movers",
        description=(
            "Syncs GET /v2/snapshot/locale/us/markets/stocks/{gainers,losers} into the "
            "top_market_movers table - both directions every run, replacing each "
            "direction's rows outright since a top-N list's membership is the point."
        ),
        has_bars_fields=False,
        # No filter to offer - every run always fetches both directions in full.
        has_ticker_type_filter=False,
        # Current market data with no natural daily cadence of its own - manual by
        # default, same reasoning as ticker-types/snapshots.
        default_run_type="manual",
    ),
    UNIFIED_SNAPSHOT_JOB: JobDefinition(
        name=UNIFIED_SNAPSHOT_JOB,
        label="Sync unified snapshot",
        description=(
            "Syncs GET /v3/snapshot into the unified_snapshots table for the selected "
            "asset-class types (stocks/options/indices/fx/crypto) - every type if none "
            "is selected."
        ),
        has_bars_fields=False,
        has_ticker_type_filter=False,
        has_snapshot_type_filter=True,
        # Current market data with no natural daily cadence of its own - manual by
        # default, same reasoning as ticker-types/snapshots.
        default_run_type="manual",
    ),
    NEWS_JOB: JobDefinition(
        name=NEWS_JOB,
        label="Sync news",
        description="Syncs GET /v2/reference/news into the news table.",
        has_bars_fields=False,
        # No filter to offer - every run fetches every article published since the
        # last sync, not scoped to a ticker or ticker type.
        has_ticker_type_filter=False,
        # Incremental like tickers/bars (jobs/sync_news.py tracks a SyncState cutoff),
        # so it keeps the same "auto" daily default those two use.
    ),
    **{
        job_name: JobDefinition(
            name=job_name,
            label=f"Sync {indicator.upper()}",
            description=(
                f"Syncs GET /v1/indicators/{indicator}/{{ticker}} into the "
                "technical_indicators table for the selected tickers or ticker types."
            ),
            has_bars_fields=False,
            # Ticker-driven, same picker as the snapshots job (has_ticker_type_filter
            # stays at its True default, superseded by has_ticker_selector below - see
            # JobDefinition.has_ticker_selector's docstring).
            has_ticker_selector=True,
            # Current market data with no natural daily cadence of its own - manual by
            # default, same reasoning as ticker-types/snapshots/movers.
            default_run_type="manual",
        )
        for job_name, indicator in INDICATOR_NAMES.items()
    },
    AVERAGE_VOLUME_JOB: JobDefinition(
        name=AVERAGE_VOLUME_JOB,
        label="Average volume",
        description=(
            "Computes each selected ticker's average daily ohlc_bars.volume across the "
            "days-interval calendar days ending on start date, and stores it in the "
            "average_volumes table. Purely local - no massive.com call, reads bars "
            "already synced by the bars job."
        ),
        has_bars_fields=False,
        has_ticker_selector=True,
        has_average_volume_fields=True,
        # Run-on-demand statistic over already-synced bars, no natural daily cadence of
        # its own - manual by default, same reasoning as ticker-types/snapshots/movers.
        default_run_type="manual",
    ),
    BACKTEST_MARKET_STATE_JOB: JobDefinition(
        name=BACKTEST_MARKET_STATE_JOB,
        label="Backtest market state",
        description=(
            "Walk-forward backtests the predict-market-state job's approach over a "
            "start date/end date range: for each evaluated day, re-fits the same "
            "per-ticker Markov chain on ohlc_bars history through the prior day only, "
            "and compares the resulting prediction to what actually happened, storing "
            "one result per (ticker, evaluated date) in the market_prediction_backtests "
            "table. Purely local - no massive.com call, reads bars already synced by "
            "the bars job."
        ),
        has_bars_fields=False,
        has_ticker_selector=True,
        has_backtest_fields=True,
        # Run-on-demand evaluation over an explicit date range, no natural daily
        # cadence of its own - manual by default, same reasoning as average-volume/
        # predict-market-state.
        default_run_type="manual",
    ),
    PREDICT_10_DAY_MARKET_STATE_JOB: JobDefinition(
        name=PREDICT_10_DAY_MARKET_STATE_JOB,
        label="Predict next 10 trading days",
        description=(
            "Walks the predict-market-state job's fitted per-ticker Markov chain "
            "forward 10 trading days from a chosen start date (default: tomorrow, "
            "UTC), storing each ticker's full 10-day projection - predicted state, "
            "confidence, and projected entry/exit price per day - in the "
            "market_predictions_10_day table. Purely local - no massive.com call, "
            "reads bars already synced by the bars job, and only bars from before the "
            "start date."
        ),
        has_bars_fields=False,
        has_ticker_selector=True,
        has_prediction_start_date_field=True,
        # Run-on-demand statistic over already-synced bars, no natural daily cadence of
        # its own - manual by default, same reasoning as predict-market-state.
        default_run_type="manual",
    ),
    WIN_RATE_JOB: JobDefinition(
        name=WIN_RATE_JOB,
        label="Compute win rates",
        description=(
            "Aggregates each selected ticker's Markov chain and Monte Carlo "
            "market-state predictions against what actually happened "
            "(ohlc_bars.pcnt_increase on the predicted date), storing per-ticker win "
            "counts, evaluated-prediction counts, and win rates for each model in the "
            "win_rates table. Purely local - no massive.com call, reads predictions "
            "and bars already computed/synced by other jobs."
        ),
        has_bars_fields=False,
        has_ticker_selector=True,
        # Run-on-demand aggregation over already-computed predictions, no natural
        # daily cadence of its own - manual by default, same reasoning as
        # average-volume/predict-market-state.
        default_run_type="manual",
    ),
    RESEARCH_PICKS_JOB: JobDefinition(
        name=RESEARCH_PICKS_JOB,
        label="Research picks",
        description=(
            "Screens the CS ticker universe for liquid candidates (market cap >= $2B, "
            "average volume >= 1M/day) whose Markov and Monte Carlo predictions agree "
            "on direction, scores them by a composite of expected-return magnitude, "
            "confidence, live/backtest win-rate track record, RSI, and recent news "
            "sentiment, and stores up to 20 as a research shortlist for further "
            "investigation - not a trading signal. Purely local - no massive.com call, "
            "reads predictions/indicators/news already synced or computed by other "
            "jobs."
        ),
        has_bars_fields=False,
        # No filter to offer - this job's whole purpose is screening the *entire* CS
        # universe by its own liquidity/agreement criteria, same reasoning as
        # ticker-types/sync-news taking has_ticker_type_filter=False. A ticker/type
        # picker here would let a caller filter away exactly the candidates it exists
        # to discover.
        has_ticker_type_filter=False,
        # Run-on-demand screen over already-computed predictions, no natural daily
        # cadence of its own - manual by default, same reasoning as win-rates/backtest.
        default_run_type="manual",
    ),
}
