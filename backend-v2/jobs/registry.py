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
PREDICTION_ACCURACY_JOB = "compute-prediction-accuracy"
RESEARCH_PICKS_JOB = "research-picks"
ETF_CONSTITUENTS_JOB = "sync-etf-constituents"
OHLC_BARS_JOB = "sync-ohlc-bars"
# Distinct from OHLC_BARS_JOB (incremental, tickers.last_ohlc_sync_date-driven) and
# BARS_JOB (incremental, ohlc_bars.MAX(timestamp)-driven) - this one always re-fetches
# and overwrites its whole caller-given [start_date, end_date] for the selected
# tickers, regardless of what's already synced (see jobs/sync_bars.py's
# sync_bars_manual, which this job runs directly). Exists for deliberate backfills/
# corrections over a known range - e.g. re-pulling a range after a bad sync - where
# the incremental jobs' "skip what's already there" logic is exactly what's unwanted.
OHLC_UPDATE_JOB = "ohlc-data-update"
# Two separate training jobs - one per jobs/lstm_common.py validation flavor - rather
# than one job with a mode switch, specifically so their JobRun.started_at/finished_at
# wall-clock time can be compared directly on the Jobs page (see jobs/train_lstm_holdout.py
# and jobs/train_lstm_walkforward.py's module docstrings).
TRAIN_LSTM_HOLDOUT_JOB = "train-lstm-holdout"
TRAIN_LSTM_WALKFORWARD_JOB = "train-lstm-walkforward"
# Two separate inference jobs - one per training flavor - rather than one job that
# picks "whichever model is newest": that would let one flavor's re-run silently start
# feeding the comparison report instead of the other, defeating the entire point of
# comparing them. Each always resolves its LstmModelVersion by its own training_method
# (see jobs/predict_lstm_market_state.py) and stores into its own
# (ticker, predicted_date, training_method) row - see db/models.py's LstmInference.
PREDICT_LSTM_HOLDOUT_JOB = "predict-lstm-market-state-holdout"
PREDICT_LSTM_WALKFORWARD_JOB = "predict-lstm-market-state-walkforward"

# job name -> training_method - jobs/engine.py's run_job looks up which of the two
# flavors a given predict-lstm-market-state-* job name is via this dict, same "job name
# -> parameter" dispatch pattern INDICATOR_NAMES already uses for the four
# sync-{sma,ema,macd,rsi} jobs sharing one sync_indicator(indicator, ...) function.
LSTM_INFERENCE_TRAINING_METHODS: dict[str, str] = {
    PREDICT_LSTM_HOLDOUT_JOB: "holdout",
    PREDICT_LSTM_WALKFORWARD_JOB: "walkforward",
}

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
    PREDICTION_ACCURACY_JOB: ("days", 1),
    RESEARCH_PICKS_JOB: ("days", 1),
    ETF_CONSTITUENTS_JOB: ("days", 1),
    OHLC_BARS_JOB: ("days", 1),
    OHLC_UPDATE_JOB: ("days", 1),
    TRAIN_LSTM_HOLDOUT_JOB: ("days", 1),
    TRAIN_LSTM_WALKFORWARD_JOB: ("days", 1),
    PREDICT_LSTM_HOLDOUT_JOB: ("days", 1),
    PREDICT_LSTM_WALKFORWARD_JOB: ("days", 1),
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
    # Whether this job offers the "Start date"/"End date"/"Limit" trio (see
    # jobs/sync_ohlc_bars.py) - only the sync-ohlc-bars job takes this. Independent of
    # the other flags, same layering as has_backtest_fields; unlike has_bars_fields
    # (sync-bars-nightly's multiplier/timespan/backfill_days/end-date-offset), this job
    # takes no ticker_types/tickers selector at all - its own query
    # (jobs/sync_ohlc_bars.py's _select_tickers_to_sync) is the only ticker selection
    # mechanism it has.
    has_ohlc_bars_fields: bool = False
    # Whether this job offers the "Start date"/"End date" pair (see
    # jobs/sync_bars.py's sync_bars_manual) - only the ohlc-data-update job takes this.
    # Independent of the other flags, same layering as has_backtest_fields; also paired
    # with has_ticker_selector on that job, to scope which tickers get overwritten.
    # Unlike has_backtest_fields/has_ohlc_bars_fields' date pairs, both fields here are
    # required at run time rather than resolved to a default - see db/models.py's
    # JobConfig.ohlc_update_start_date docstring for why.
    has_ohlc_update_fields: bool = False
    # Whether this job offers the "Start date"/"End date"/"Epochs"/"Lookback days"/
    # "Learning rate"/"Batch size" group (see jobs/lstm_common.py) - shared by both
    # train-lstm-holdout and train-lstm-walkforward, since both train the same
    # LstmModel over the same date-range-and-hyperparameters shape, just with a
    # different validation strategy applied on top. Independent of the other flags,
    # same layering as has_backtest_fields; also paired with has_ticker_selector on
    # both jobs that set it, so a first exploratory run can scope down to a handful of
    # tickers for a fast timing comparison.
    has_lstm_training_fields: bool = False
    # Whether this job offers the one extra "Number of folds" field (see
    # jobs/train_lstm_walkforward.py) - only the train-lstm-walkforward job takes this,
    # layered on top of has_lstm_training_fields above, same layering pattern as
    # has_average_volume_fields on has_ticker_selector.
    has_lstm_walkforward_fields: bool = False
    # Whether this job offers the optional "Model version" override field (see
    # jobs/predict_lstm_market_state.py) - only the predict-lstm-market-state job takes
    # this. Independent of the other flags; always paired with
    # has_predicted_date_offset_field on that job, since both fields resolve at run
    # time (which model to use, which session to predict) rather than being fixed at
    # config-creation time.
    has_lstm_inference_fields: bool = False
    # Whether this job offers the single "Pass threshold (std devs)" field (see
    # jobs/prediction_accuracy.py) - only the compute-prediction-accuracy job takes
    # this. Independent of the other flags, same layering as has_average_volume_fields;
    # also paired with has_ticker_selector, so a run can be scoped to a handful of
    # tickers.
    has_prediction_accuracy_fields: bool = False
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
    PREDICTION_ACCURACY_JOB: JobDefinition(
        name=PREDICTION_ACCURACY_JOB,
        label="Compute prediction accuracy",
        description=(
            "For each (ticker, predicted_date) with a known actual outcome "
            "(ohlc_bars.close already synced for predicted_date), scores every one of "
            "the four prediction sources' (Markov, Monte Carlo, LSTM holdout, LSTM "
            "walk-forward) predicted exit price against that actual close: a pass if "
            "the actual price falls within Pass threshold standard deviations of the "
            "predicted price, a fail otherwise. The standard deviation used is this "
            "ticker's own historical return volatility (fit on bar history strictly "
            "before predicted_date, no lookahead), not any model's own self-reported "
            "confidence - the same yardstick for all four, so no source grades itself "
            "against its own uncertainty estimate. Stores one row per (ticker, "
            "predicted_date) with all four sources' predicted price/error/pass-fail "
            "side by side in the prediction_accuracy table. Purely local - no "
            "massive.com call, reads predictions and bars already computed/synced by "
            "other jobs."
        ),
        has_bars_fields=False,
        has_ticker_selector=True,
        has_prediction_accuracy_fields=True,
        # Run-on-demand aggregation over already-computed predictions, no natural
        # daily cadence of its own - manual by default, same reasoning as win-rates.
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
    ETF_CONSTITUENTS_JOB: JobDefinition(
        name=ETF_CONSTITUENTS_JOB,
        label="Download ETF holdings",
        description=(
            "Downloads each selected ticker's daily holdings workbook from SSGA "
            "(holdings-daily-us-en-{ticker}.xlsx) and saves the raw file under "
            "backend-v2/data/etf_holdings/, overwriting whatever was saved for that "
            "ticker last run. Parsing the workbook into structured rows is a separate, "
            "not-yet-built step - this job only fetches and saves the raw files. Only "
            "works for ETFs State Street itself manages (SPY and other SPDR funds) - "
            "the Ticker types field below has no effect on this job (there's no "
            "meaningful asset-class filter for a per-ticker file URL); only the "
            "Tickers field matters, and leaving it blank downloads nothing rather than "
            "'every ticker' the way it does for sync-bars/sync-snapshots."
        ),
        has_bars_fields=False,
        # Reuses sync_bars/sync_snapshots' dual ticker_types+tickers picker purely for
        # its Tickers half - see this job's description above for why ticker_types
        # itself is inert here. has_ticker_type_filter is left at its default True,
        # same as BARS_JOB/SNAPSHOTS_JOB, since has_ticker_selector supersedes it in
        # JobCard.tsx's render order regardless of this value.
        has_ticker_selector=True,
        # Fetches whatever's on the Tickers field with no natural daily cadence of its
        # own (and an empty field is a no-op run, not "sync everything") - manual by
        # default, same reasoning as ticker-types/top-movers.
        default_run_type="manual",
    ),
    OHLC_BARS_JOB: JobDefinition(
        name=OHLC_BARS_JOB,
        label="Sync OHLC bars (batch)",
        description=(
            "Selects up to Limit tickers (default 8000, max 10000) whose "
            "tickers.last_ohlc_sync_date is NULL or before End date - ordered by "
            "ticker_types.rank, then ticker - computes each one's own new_start_date "
            "(Start date, or last_ohlc_sync_date + 1 day if that's later) and syncs GET "
            "/v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{from}/{to} for "
            "[new_start_date, End date] into ohlc_bars, fanned out across a thread "
            "pool. Each ticker's worker then stamps tickers.last_ohlc_sync_date with "
            "End date, so a run smaller than the full backlog naturally rotates on to "
            "different tickers next time rather than reselecting the same batch. "
            "Distinct from the sync-bars-nightly job: this one is limit-bounded and "
            "selection-query-driven rather than covering every known ticker every run."
        ),
        has_bars_fields=False,
        has_ticker_type_filter=False,
        has_ohlc_bars_fields=True,
        # Batch-limited backfill run with no natural daily cadence of its own (a limit
        # smaller than the backlog needs several runs to catch up) - manual by default,
        # same reasoning as ticker-types/snapshots/movers.
        default_run_type="manual",
    ),
    OHLC_UPDATE_JOB: JobDefinition(
        name=OHLC_UPDATE_JOB,
        label="OHLC data update",
        description=(
            "Syncs GET /v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{from}/{to} "
            "into ohlc_bars for every selected ticker over the exact Start date/End "
            "date range given, overwriting any bar already stored for a day in that "
            "range and adding one where none existed - regardless of what's already "
            "synced. Unlike sync-ohlc-bars/sync-bars-nightly, this job does no "
            "incremental check against tickers.last_ohlc_sync_date or ohlc_bars' own "
            "most recent bar - every run re-fetches the whole given range from "
            "scratch. Meant for deliberate backfills/corrections over a known range "
            "(e.g. re-pulling a range after a bad sync), not routine syncing."
        ),
        has_bars_fields=False,
        has_ticker_selector=True,
        has_ohlc_update_fields=True,
        # Deliberate, caller-scoped overwrite with no natural daily cadence of its own
        # (running it automatically every day would defeat the point of it being a
        # targeted correction) - manual by default, same reasoning as
        # ticker-types/snapshots/movers.
        default_run_type="manual",
    ),
    TRAIN_LSTM_HOLDOUT_JOB: JobDefinition(
        name=TRAIN_LSTM_HOLDOUT_JOB,
        label="Train LSTM (holdout)",
        description=(
            "Trains a pooled (cross-ticker) LSTM over the selected tickers' ohlc_bars "
            "history within the chosen date range, reserving the last ~15% of days as "
            "a single chronological validation holdout. Predicts the same STATE_LABELS "
            "quantile bucket predict-market-state fits, plus a regression head for the "
            "raw next-period return, from features engineered fresh from ohlc_bars - no "
            "dependency on technical_indicators/average_volumes' sparser coverage. "
            "Stores the trained checkpoint and its holdout metrics as a new row in the "
            "lstm_model_versions table. Fast - one training pass - but only tests one "
            "train/validation boundary; see train-lstm-walkforward for the more "
            "rigorous (and much slower) alternative. Purely local - no massive.com "
            "call, reads bars already synced by the bars job."
        ),
        has_bars_fields=False,
        has_ticker_selector=True,
        has_lstm_training_fields=True,
        # Expensive, run-on-demand training run - manual by default, same reasoning as
        # predict-market-state/backtest-market-state.
        default_run_type="manual",
    ),
    TRAIN_LSTM_WALKFORWARD_JOB: JobDefinition(
        name=TRAIN_LSTM_WALKFORWARD_JOB,
        label="Train LSTM (walk-forward)",
        description=(
            "Same pooled LSTM as train-lstm-holdout, but validated the more rigorous "
            "way: splits the chosen date range into Number of folds rolling cutoffs, "
            "retraining from scratch on data through each cutoff and evaluating on the "
            "block up to the next one - the same expanding-window, no-lookahead "
            "principle backtest-market-state applies to the Markov chain, coarsened to "
            "per-fold blocks since retraining a network daily would be prohibitively "
            "slow. Only the final fold's weights (fit on the most data) are saved as "
            "this run's usable lstm_model_versions row; every fold's metrics are logged "
            "in this run's history entry. Exists as a separate job from train-lstm-"
            "holdout specifically so the two flavors' run times can be compared before "
            "deciding whether walk-forward retraining is affordable to run regularly. "
            "Purely local - no massive.com call, reads bars already synced by the bars "
            "job."
        ),
        has_bars_fields=False,
        has_ticker_selector=True,
        has_lstm_training_fields=True,
        has_lstm_walkforward_fields=True,
        # Expensive, run-on-demand training run - manual by default, same reasoning as
        # train-lstm-holdout.
        default_run_type="manual",
    ),
    PREDICT_LSTM_HOLDOUT_JOB: JobDefinition(
        name=PREDICT_LSTM_HOLDOUT_JOB,
        label="Predict market state (LSTM, holdout)",
        description=(
            "Runs the most recently trained train-lstm-holdout model (or a specific "
            "holdout-trained lstm_model_versions row, via the optional Model version "
            "field) over the selected tickers' ohlc_bars history for a chosen predicted "
            "date (default: tomorrow, UTC - see the Predicted date field), storing each "
            "ticker's predicted state, full softmax state probabilities, and expected "
            "return in the lstm_inferences table. Always uses a 'holdout'-flavor model - "
            "see predict-market-state-lstm-walkforward for the walk-forward-trained "
            "counterpart, kept as a fully independent job (own schedule, own run "
            "history, own inference rows) specifically so the two flavors' predictions "
            "for the same ticker/date can be compared side by side on the Prediction "
            "Comparison report rather than one silently overwriting the other. Purely "
            "local - no massive.com call, reads bars already synced by the bars job, "
            "and only bars from before the predicted date."
        ),
        has_bars_fields=False,
        has_ticker_selector=True,
        has_predicted_date_offset_field=True,
        has_lstm_inference_fields=True,
        # Run-on-demand inference over an already-trained model, no natural daily
        # cadence of its own - manual by default, same reasoning as predict-market-state.
        default_run_type="manual",
    ),
    PREDICT_LSTM_WALKFORWARD_JOB: JobDefinition(
        name=PREDICT_LSTM_WALKFORWARD_JOB,
        label="Predict market state (LSTM, walk-forward)",
        description=(
            "Same as predict-market-state-lstm-holdout, but always uses the most "
            "recently trained train-lstm-walkforward model (or a specific "
            "walkforward-flavor lstm_model_versions row, via the optional Model "
            "version field) - see that job's description for why the two are kept as "
            "fully independent jobs rather than one job that just picks whichever "
            "model is newest."
        ),
        has_bars_fields=False,
        has_ticker_selector=True,
        has_predicted_date_offset_field=True,
        has_lstm_inference_fields=True,
        default_run_type="manual",
    ),
}
