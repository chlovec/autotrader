"""backend-v2's dashboard API - HTTP only. Job execution (sync-tickers,
sync-bars-nightly, predict-market-state, etc.) runs in a separate process,
job_runner.py, on its own schedule; this process never runs a job directly. The two
coordinate purely through the database - see jobs/engine.py's module docstring and
jobs/config_store.py (job_is_active, interval_trigger) for how. Launched via
run_jobs.py (uvicorn.run("app.main:app", ...)) so bin/restart-v2.sh's invocation
doesn't need to change.

This split used to be one process (this file also ran the scheduler and every job
directly) - see db/session.py's WAL-mode comment for why that stopped being viable
(a heavy job's CPU/DB usage could make the dashboard unresponsive while it ran).
"""

import datetime as dt
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import Float, case, cast, delete, func, or_, select, text, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.orm import Session, aliased

from db.models import (
    AverageVolume,
    Base,
    CurrentSnapshot,
    JobConfig,
    JobRun,
    LstmInference,
    LstmModelVersion,
    MarketPrediction,
    MarketPrediction10Day,
    MarketPredictionBacktest,
    MarketPredictionMonteCarlo,
    News,
    OhlcBar,
    PredictionAccuracy,
    ResearchPick,
    SyncProgress,
    SyncState,
    TechnicalIndicator,
    Ticker,
    TickerDetail,
    TickerType,
    TopMarketMover,
    UnifiedSnapshot,
    WinRate,
)
from db.session import SessionLocal, init_db
from jobs.config_store import get_or_create_config, interval_trigger, job_is_active, split_csv
from jobs.lstm_common import DEFAULT_WALKFORWARD_NUM_FOLDS
from jobs.registry import (
    AVERAGE_VOLUME_JOB,
    BACKTEST_MARKET_STATE_JOB,
    BARS_JOB,
    DEFAULT_START_TIME,
    ETF_CONSTITUENTS_JOB,
    INDICATOR_NAMES,
    JOB_DEFINITIONS,
    LSTM_INFERENCE_TRAINING_METHODS,
    MOVERS_JOB,
    NEWS_JOB,
    OHLC_BARS_JOB,
    OHLC_UPDATE_JOB,
    PREDICT_10_DAY_MARKET_STATE_JOB,
    PREDICT_LSTM_HOLDOUT_JOB,
    PREDICT_LSTM_WALKFORWARD_JOB,
    PREDICT_MARKET_STATE_JOB,
    PREDICTION_ACCURACY_JOB,
    RESEARCH_PICKS_JOB,
    SNAPSHOT_TYPE_OPTIONS,
    SNAPSHOTS_JOB,
    START_TIME_OPTIONS,
    TICKER_DETAILS_JOB,
    TICKER_TYPES_JOB,
    TICKERS_JOB,
    TRAIN_LSTM_HOLDOUT_JOB,
    TRAIN_LSTM_WALKFORWARD_JOB,
    UNIFIED_SNAPSHOT_JOB,
    WIN_RATE_JOB,
)
from jobs.sync_bars import DEFAULT_MULTIPLIER, DEFAULT_TIMESPAN
from jobs.sync_ohlc_bars import MAX_LIMIT as OHLC_BARS_MAX_LIMIT

logger = logging.getLogger("backend_v2.app")

# Same requirement as backend/app/main.py (v1): no default, since a wrong-but-plausible
# one is how a dashboard silently getting "Backend unreachable" happened before.
_cors_origins_raw = os.environ.get("CORS_ORIGINS")
if not _cors_origins_raw:
    raise RuntimeError(
        "CORS_ORIGINS is not set. Set it to frontend-v2's origin in backend-v2/.env, "
        "e.g. CORS_ORIGINS=http://localhost:5174 - see backend-v2/.env.example."
    )
_cors_origins = [origin.strip() for origin in _cors_origins_raw.split(",") if origin.strip()]
if not _cors_origins:
    raise RuntimeError("CORS_ORIGINS is set but empty after parsing - check its value in backend-v2/.env.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Autotrader Backend v2", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _run_to_dict(run: JobRun) -> dict[str, Any]:
    duration_seconds = (run.finished_at - run.started_at).total_seconds() if run.finished_at else None
    return {
        "id": run.id,
        "trigger": run.trigger,
        "status": run.status,
        "started_at": run.started_at.isoformat(),
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "duration_seconds": duration_seconds,
        "result_summary": run.result_summary,
        "error": run.error,
        "progress_completed": run.progress_completed,
        "progress_total": run.progress_total,
    }


def _next_run_time(config: JobConfig) -> str | None:
    """None for a "manual" job even though job_runner.py still keeps a schedule
    registered for it - its trigger would otherwise report a next fire time that's
    really just going to be skipped by job_runner.py's scheduled_job run_type check,
    which would be misleading on the dashboard.

    Computed independently of job_runner.py's live scheduler - this process doesn't
    have one - via the identical interval_trigger(config) both processes build from the
    same JobConfig row (see jobs/config_store.py's interval_trigger docstring for why
    that's guaranteed to agree with job_runner.py's actual schedule). get_next_fire_time
    with previous_fire_time=None is a pure computation APScheduler's IntervalTrigger
    supports without a running scheduler - it walks forward from start_date."""
    if config.run_type != "auto":
        return None
    next_fire = interval_trigger(config).get_next_fire_time(None, dt.datetime.now(dt.timezone.utc))
    return next_fire.isoformat() if next_fire else None


def _in_progress_run(session: Session, job_name: str) -> JobRun | None:
    return session.execute(
        select(JobRun).where(JobRun.job_name == job_name, JobRun.status == "in_progress").limit(1)
    ).scalar_one_or_none()


def _job_to_dict(session: Session, job_name: str) -> dict[str, Any]:
    definition = JOB_DEFINITIONS[job_name]
    config = get_or_create_config(session, job_name)
    run = _in_progress_run(session, job_name)
    last_run = session.execute(
        select(JobRun).where(JobRun.job_name == job_name).order_by(JobRun.started_at.desc()).limit(1)
    ).scalar_one_or_none()
    return {
        "name": job_name,
        "label": definition.label,
        "description": definition.description,
        "has_bars_fields": definition.has_bars_fields,
        "has_ticker_type_filter": definition.has_ticker_type_filter,
        "has_ticker_selector": definition.has_ticker_selector,
        "has_snapshot_type_filter": definition.has_snapshot_type_filter,
        "has_average_volume_fields": definition.has_average_volume_fields,
        "has_backtest_fields": definition.has_backtest_fields,
        "has_prediction_start_date_field": definition.has_prediction_start_date_field,
        "has_predicted_date_offset_field": definition.has_predicted_date_offset_field,
        "has_monte_carlo_fields": definition.has_monte_carlo_fields,
        "has_ohlc_bars_fields": definition.has_ohlc_bars_fields,
        "has_ohlc_update_fields": definition.has_ohlc_update_fields,
        "has_lstm_training_fields": definition.has_lstm_training_fields,
        "has_lstm_walkforward_fields": definition.has_lstm_walkforward_fields,
        "has_lstm_inference_fields": definition.has_lstm_inference_fields,
        "has_prediction_accuracy_fields": definition.has_prediction_accuracy_fields,
        "snapshot_type_options": SNAPSHOT_TYPE_OPTIONS,
        "run_type": config.run_type,
        "schedule_interval_unit": config.schedule_interval_unit,
        "schedule_interval_value": config.schedule_interval_value,
        "start_time": config.start_time,
        "next_run_time": _next_run_time(config),
        "ticker_types": config.ticker_types,
        "tickers": config.tickers,
        "multiplier": config.multiplier,
        "timespan": config.timespan,
        "backfill_days": config.backfill_days,
        "bars_end_date_offset_days": config.bars_end_date_offset_days,
        "snapshot_types": config.snapshot_types,
        "average_volume_start_date": (
            config.average_volume_start_date.isoformat() if config.average_volume_start_date else None
        ),
        "average_volume_days_interval": config.average_volume_days_interval,
        "backtest_start_date": config.backtest_start_date.isoformat() if config.backtest_start_date else None,
        "backtest_end_date": config.backtest_end_date.isoformat() if config.backtest_end_date else None,
        "prediction_start_date": (
            config.prediction_start_date.isoformat() if config.prediction_start_date else None
        ),
        "predicted_date_offset_days": config.predicted_date_offset_days,
        "mcmc_num_simulations": config.mcmc_num_simulations,
        "ohlc_bars_start_date": config.ohlc_bars_start_date.isoformat() if config.ohlc_bars_start_date else None,
        "ohlc_bars_end_date": config.ohlc_bars_end_date.isoformat() if config.ohlc_bars_end_date else None,
        "ohlc_bars_limit": config.ohlc_bars_limit,
        "ohlc_update_start_date": (
            config.ohlc_update_start_date.isoformat() if config.ohlc_update_start_date else None
        ),
        "ohlc_update_end_date": (
            config.ohlc_update_end_date.isoformat() if config.ohlc_update_end_date else None
        ),
        "lstm_train_start_date": (
            config.lstm_train_start_date.isoformat() if config.lstm_train_start_date else None
        ),
        "lstm_train_end_date": config.lstm_train_end_date.isoformat() if config.lstm_train_end_date else None,
        "lstm_epochs": config.lstm_epochs,
        "lstm_lookback_days": config.lstm_lookback_days,
        "lstm_learning_rate": config.lstm_learning_rate,
        "lstm_batch_size": config.lstm_batch_size,
        "lstm_walkforward_num_folds": config.lstm_walkforward_num_folds,
        "lstm_model_version_id": config.lstm_model_version_id,
        "prediction_accuracy_pass_threshold_std": config.prediction_accuracy_pass_threshold_std,
        "hidden": config.hidden,
        "sort_order": config.sort_order,
        "running": config.run_requested_at is not None or run is not None,
        "paused": run.pause_requested if run is not None else False,
        "last_run": _run_to_dict(last_run) if last_run is not None else None,
    }


def _require_job(job_name: str) -> None:
    if job_name not in JOB_DEFINITIONS:
        raise HTTPException(status_code=404, detail=f"unknown job {job_name!r}")


# job name -> the table(s) that job's data lives in, emptied wholesale by reset_job
# below. The four indicator jobs (SMA_JOB etc.) share a single technical_indicators
# table (see db/models.py's TechnicalIndicator) rather than getting their own entry
# here - reset_job deletes just that job's `indicator` value's rows instead, handled
# as a special case alongside this dict rather than folded into it.
_RESET_TABLES: dict[str, list[type[Base]]] = {
    TICKERS_JOB: [Ticker],
    BARS_JOB: [OhlcBar],
    # Shares ohlc_bars with BARS_JOB (see jobs/sync_ohlc_bars.py) - resetting either
    # empties the whole table, same as resetting BARS_JOB already does today.
    OHLC_BARS_JOB: [OhlcBar],
    # Also shares ohlc_bars (see jobs/sync_bars.py's sync_bars_manual, which this job
    # runs directly) - same reasoning as OHLC_BARS_JOB above.
    OHLC_UPDATE_JOB: [OhlcBar],
    TICKER_TYPES_JOB: [TickerType],
    SNAPSHOTS_JOB: [CurrentSnapshot],
    TICKER_DETAILS_JOB: [TickerDetail],
    MOVERS_JOB: [TopMarketMover],
    UNIFIED_SNAPSHOT_JOB: [UnifiedSnapshot],
    # No DB table - this job only downloads raw files to disk (see
    # jobs/sync_etf_constituents.py's DOWNLOAD_DIR). Still needs an entry here since
    # reset_job below indexes _RESET_TABLES unconditionally for every job not in
    # INDICATOR_NAMES; an empty list just means "nothing to empty" - the downloaded
    # files themselves aren't touched by Reset.
    ETF_CONSTITUENTS_JOB: [],
    NEWS_JOB: [News],
    AVERAGE_VOLUME_JOB: [AverageVolume],
    # predict-market-state now runs both the Markov chain and Monte Carlo phases in
    # one job (see jobs/registry.py's PREDICT_MARKET_STATE_JOB entry) - resetting it
    # empties both tables together.
    PREDICT_MARKET_STATE_JOB: [MarketPrediction, MarketPredictionMonteCarlo],
    PREDICT_10_DAY_MARKET_STATE_JOB: [MarketPrediction10Day],
    BACKTEST_MARKET_STATE_JOB: [MarketPredictionBacktest],
    WIN_RATE_JOB: [WinRate],
    PREDICTION_ACCURACY_JOB: [PredictionAccuracy],
    RESEARCH_PICKS_JOB: [ResearchPick],
    # train-lstm-holdout/train-lstm-walkforward share the lstm_model_versions table
    # (distinguished by training_method), and predict-lstm-market-state-holdout/
    # -walkforward share lstm_inferences (also distinguished by training_method) - the
    # same way the four indicator jobs share technical_indicators. reset_job below
    # special-cases all four the same way it already special-cases INDICATOR_NAMES,
    # filtering by training_method instead of blanket-deleting the other flavor's rows
    # too. Still listed here (with an empty list, same reasoning as
    # ETF_CONSTITUENTS_JOB) since reset_job indexes _RESET_TABLES unconditionally for
    # every job not in INDICATOR_NAMES/_LSTM_MODEL_VERSION_TRAINING_METHODS/
    # LSTM_INFERENCE_TRAINING_METHODS.
    TRAIN_LSTM_HOLDOUT_JOB: [],
    TRAIN_LSTM_WALKFORWARD_JOB: [],
    PREDICT_LSTM_HOLDOUT_JOB: [],
    PREDICT_LSTM_WALKFORWARD_JOB: [],
}

# training_method value for each of the two lstm_model_versions-writing jobs - reset_job
# below filters LstmModelVersion deletes by this, same reasoning as INDICATOR_NAMES'
# TechnicalIndicator.indicator filter. LSTM_INFERENCE_TRAINING_METHODS (imported from
# jobs.registry above) is the same mapping for the two lstm_inferences-writing jobs -
# not redefined here since jobs/engine.py's run_job already needs that exact dict.
_LSTM_MODEL_VERSION_TRAINING_METHODS: dict[str, str] = {
    TRAIN_LSTM_HOLDOUT_JOB: "holdout",
    TRAIN_LSTM_WALKFORWARD_JOB: "walkforward",
}

# job name -> the SyncState/SyncProgress job_name key it syncs incrementally under
# (see jobs/sync_tickers.py's/jobs/sync_news.py's own JOB_NAME constants, which don't
# match registry.py's TICKERS_JOB/NEWS_JOB strings). reset_job also clears these so a
# reset job's next run does a full resync instead of only fetching what's changed
# since the old (now-stale) cutoff - otherwise the table would stay empty until
# something else forced a full resync.
_RESET_SYNC_STATE_KEYS: dict[str, str] = {TICKERS_JOB: "tickers", NEWS_JOB: "news"}


class JobConfigIn(BaseModel):
    run_type: Literal["manual", "auto"]
    schedule_interval_unit: Literal["minutes", "hours", "days"]
    schedule_interval_value: int
    start_time: str = DEFAULT_START_TIME
    ticker_types: str | None = None
    tickers: str | None = None
    multiplier: int | None = None
    timespan: str | None = None
    backfill_days: int | None = None
    bars_end_date_offset_days: int | None = None
    snapshot_types: str | None = None
    average_volume_start_date: str | None = None
    average_volume_days_interval: int | None = None
    backtest_start_date: str | None = None
    backtest_end_date: str | None = None
    prediction_start_date: str | None = None
    predicted_date_offset_days: int | None = None
    mcmc_num_simulations: int | None = None
    ohlc_bars_start_date: str | None = None
    ohlc_bars_end_date: str | None = None
    ohlc_bars_limit: int | None = None
    ohlc_update_start_date: str | None = None
    ohlc_update_end_date: str | None = None
    lstm_train_start_date: str | None = None
    lstm_train_end_date: str | None = None
    lstm_epochs: int | None = None
    lstm_lookback_days: int | None = None
    lstm_learning_rate: float | None = None
    lstm_batch_size: int | None = None
    lstm_walkforward_num_folds: int | None = None
    lstm_model_version_id: int | None = None
    prediction_accuracy_pass_threshold_std: float | None = None


class JobReorderIn(BaseModel):
    job_names: list[str]


class TickerTypeUpdateIn(BaseModel):
    rank: int | None = None
    status: Literal["active", "inactive"]


class AdhocQueryIn(BaseModel):
    sql: str


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/ticker-types/search")
def search_ticker_types(q: str = "", limit: int = 20) -> list[dict]:
    """Backs the Jobs page's searchable ticker-type selects (JobCard's TickerType
    combobox(es)) - looks up the ticker_types reference table itself (synced by
    sync-ticker-types), matching against code, asset_class, or description rather than
    just the codes actually in use on the tickers table."""
    limit = max(1, min(limit, 50))
    with SessionLocal() as session:
        query = select(TickerType.code, TickerType.asset_class, TickerType.description)
        term = q.strip()
        if term:
            pattern = f"%{term}%"
            query = query.where(
                or_(
                    TickerType.code.like(pattern),
                    TickerType.asset_class.like(pattern),
                    TickerType.description.like(pattern),
                )
            )
        query = query.distinct().order_by(TickerType.code).limit(limit)
        rows = session.execute(query).all()
        return [{"code": row.code, "asset_class": row.asset_class, "description": row.description} for row in rows]


def _ticker_type_to_dict(ticker_type: TickerType) -> dict[str, Any]:
    return {
        "code": ticker_type.code,
        "asset_class": ticker_type.asset_class,
        "locale": ticker_type.locale,
        "description": ticker_type.description,
        "rank": ticker_type.rank,
        "status": ticker_type.status,
    }


@app.get("/ticker-types")
def list_ticker_types() -> list[dict]:
    """Backs the Settings > Ticker Types page, where an operator sets rank/status per
    code/asset_class/locale combination. Unlike /ticker-types/search (typeahead for the
    Jobs page's combobox), this returns every row unfiltered and unpaginated - matches
    db/models.py's TickerType docstring describing the table as a short, mostly-static
    reference list, so there's no report-style paging/filtering needed here.

    Ordered by rank ascending with unranked (NULL) rows last, then by code - the same
    "nulls sort last regardless of direction" convention as ReportGrid's compareRows,
    applied here since rank is meant to drive the page's default display order."""
    with SessionLocal() as session:
        rows = (
            session.execute(
                select(TickerType).order_by(TickerType.rank.is_(None), TickerType.rank, TickerType.code)
            )
            .scalars()
            .all()
        )
        return [_ticker_type_to_dict(t) for t in rows]


@app.put("/ticker-types/{code}/{asset_class}/{locale}")
def update_ticker_type(code: str, asset_class: str, locale: str, body: TickerTypeUpdateIn) -> dict:
    if body.rank is not None and body.rank < 1:
        raise HTTPException(status_code=400, detail="rank must be at least 1")
    with SessionLocal() as session:
        ticker_type = session.get(TickerType, (code, asset_class, locale))
        if ticker_type is None:
            raise HTTPException(status_code=404, detail="ticker type not found")
        ticker_type.rank = body.rank
        ticker_type.status = body.status
        session.commit()
        return _ticker_type_to_dict(ticker_type)


@app.get("/tickers/search")
def search_tickers(q: str = "", limit: int = 20) -> list[dict]:
    """Backs the Jobs page's searchable Tickers select (JobCard's Tickers combobox) -
    server-side so the dropdown never has to ship the whole tickers table to the
    browser just to filter it client-side."""
    limit = max(1, min(limit, 50))
    with SessionLocal() as session:
        query = select(Ticker.ticker, Ticker.name)
        term = q.strip()
        if term:
            pattern = f"%{term}%"
            query = query.where(or_(Ticker.ticker.like(pattern), Ticker.name.like(pattern)))
        query = query.order_by(Ticker.ticker).limit(limit)
        rows = session.execute(query).all()
        return [{"ticker": row.ticker, "name": row.name} for row in rows]


def _prediction_fields(prediction: MarketPrediction | None) -> dict[str, Any]:
    """Shared by _mover_to_dict/_symbol_to_dict - the most recently predicted_date row
    (see the latest_prediction_by_ticker lookups below) from market_predictions, joined
    out by ticker the same way average_volume is."""
    return {
        "predicted_date": prediction.predicted_date.isoformat() if prediction else None,
        "current_state": prediction.current_state if prediction else None,
        "predicted_state": prediction.predicted_state if prediction else None,
        "state_confidence": prediction.state_confidence if prediction else None,
        "expected_return": prediction.expected_return if prediction else None,
        "entry_price": prediction.entry_price if prediction else None,
        "exit_price": prediction.exit_price if prediction else None,
        "exit_price_confidence": prediction.exit_price_confidence if prediction else None,
        "entry_time": prediction.entry_time if prediction else None,
        "exit_time": prediction.exit_time if prediction else None,
        "history_days": prediction.history_days if prediction else None,
        "prediction_computed_at": prediction.computed_at.isoformat() if prediction else None,
    }


def _mover_to_dict(
    mover: TopMarketMover,
    ticker: Ticker | None,
    asset_class: str | None,
    average_volume: float | None,
    market_cap: float | None,
    prediction: MarketPrediction | None,
) -> dict[str, Any]:
    return {
        "ticker": mover.ticker,
        "name": ticker.name if ticker else None,
        "type": ticker.type if ticker else None,
        "asset_class": asset_class,
        "average_volume": average_volume,
        "market_cap": market_cap,
        "direction": mover.direction,
        "rank": mover.rank,
        "todays_change": mover.todays_change,
        "todays_change_perc": mover.todays_change_perc,
        "updated": mover.updated.isoformat() if mover.updated else None,
        "day_open": mover.day_open,
        "day_high": mover.day_high,
        "day_low": mover.day_low,
        "day_close": mover.day_close,
        "day_volume": mover.day_volume,
        "day_vwap": mover.day_vwap,
        "min_open": mover.min_open,
        "min_high": mover.min_high,
        "min_low": mover.min_low,
        "min_close": mover.min_close,
        "min_volume": mover.min_volume,
        "min_vwap": mover.min_vwap,
        "min_accumulated_volume": mover.min_accumulated_volume,
        "min_timestamp": mover.min_timestamp.isoformat() if mover.min_timestamp else None,
        "prev_day_open": mover.prev_day_open,
        "prev_day_high": mover.prev_day_high,
        "prev_day_low": mover.prev_day_low,
        "prev_day_close": mover.prev_day_close,
        "prev_day_volume": mover.prev_day_volume,
        "prev_day_vwap": mover.prev_day_vwap,
        "fetched_at": mover.fetched_at.isoformat(),
        **_prediction_fields(prediction),
    }


@app.get("/reports/top-movers")
def top_movers_report(ticker_types: str = "") -> list[dict]:
    """Backs the Analytics > Top Movers page's report grid: today's top_market_movers
    rows (both directions), each joined out to its tickers row for name/type and to its
    most recently computed average_volumes row. Filtering by ticker_types (0 or more,
    comma-separated codes from the same ticker_types reference list as the Jobs page's
    TickerType combobox) inner-joins instead of left-joins - a mover with no matching
    tickers row has no `type` to filter on anyway, so it can never satisfy a non-empty
    filter."""
    types = split_csv(ticker_types)
    with SessionLocal() as session:
        query = select(TopMarketMover, Ticker).join(
            Ticker, Ticker.ticker == TopMarketMover.ticker, isouter=not types
        )
        if types:
            query = query.where(Ticker.type.in_(types))
        query = query.order_by(TopMarketMover.direction, TopMarketMover.rank)
        rows = session.execute(query).all()

        # Loaded once per request rather than joined in, since a ticker `type` code
        # (e.g. "CS") can appear across more than one ticker_types row (distinct
        # locales) - this just takes the first asset_class seen per code rather than
        # letting the join fan out duplicate mover rows.
        asset_class_by_code: dict[str, str] = {}
        for code, asset_class in session.execute(select(TickerType.code, TickerType.asset_class)).all():
            asset_class_by_code.setdefault(code, asset_class)

        # average_volumes accumulates one row per (ticker, start_date, days_interval) -
        # "latest" means the row from that ticker's most recent job run, so this joins
        # each ticker back to whichever row carries the max computed_at rather than the
        # max start_date (which, unlike computed_at, isn't unique per run if a ticker is
        # ever computed under more than one days_interval on the same start_date).
        latest_computed_at = select(
            AverageVolume.ticker, func.max(AverageVolume.computed_at).label("computed_at")
        ).group_by(AverageVolume.ticker)
        latest_computed_at = latest_computed_at.subquery()
        average_volume_by_ticker: dict[str, float | None] = {
            ticker: average_volume
            for ticker, average_volume in session.execute(
                select(AverageVolume.ticker, AverageVolume.average_volume).join(
                    latest_computed_at,
                    (AverageVolume.ticker == latest_computed_at.c.ticker)
                    & (AverageVolume.computed_at == latest_computed_at.c.computed_at),
                )
            ).all()
        }

        # market_predictions accumulates one row per (ticker, predicted_date) - "latest"
        # means the row for whichever predicted_date is furthest out, same
        # latest-row-per-ticker pattern as average_volume_by_ticker above.
        latest_predicted_date = select(
            MarketPrediction.ticker, func.max(MarketPrediction.predicted_date).label("predicted_date")
        ).group_by(MarketPrediction.ticker)
        latest_predicted_date = latest_predicted_date.subquery()
        prediction_by_ticker: dict[str, MarketPrediction] = {
            prediction.ticker: prediction
            for prediction in session.execute(
                select(MarketPrediction).join(
                    latest_predicted_date,
                    (MarketPrediction.ticker == latest_predicted_date.c.ticker)
                    & (MarketPrediction.predicted_date == latest_predicted_date.c.predicted_date),
                )
            ).scalars()
        }

        # ticker_details is upserted wholesale per ticker (no history), unlike
        # average_volumes/market_predictions above - so this is a plain lookup, no
        # latest-row-per-ticker join needed.
        market_cap_by_ticker: dict[str, float | None] = dict(
            session.execute(select(TickerDetail.ticker, TickerDetail.market_cap)).all()
        )

        return [
            _mover_to_dict(
                mover,
                ticker,
                asset_class_by_code.get(ticker.type) if ticker and ticker.type else None,
                average_volume_by_ticker.get(mover.ticker),
                market_cap_by_ticker.get(mover.ticker),
                prediction_by_ticker.get(mover.ticker),
            )
            for mover, ticker in rows
        ]


def _symbol_to_dict(
    ticker: Ticker,
    asset_class: str | None,
    average_volume: float | None,
    market_cap: float | None,
    snapshot: CurrentSnapshot | None,
    prediction: MarketPrediction | None,
) -> dict[str, Any]:
    return {
        "ticker": ticker.ticker,
        "name": ticker.name,
        "type": ticker.type,
        "asset_class": asset_class,
        "average_volume": average_volume,
        "market_cap": market_cap,
        "todays_change": snapshot.todays_change if snapshot else None,
        "todays_change_perc": snapshot.todays_change_perc if snapshot else None,
        "updated": snapshot.updated.isoformat() if snapshot and snapshot.updated else None,
        "day_open": snapshot.day_open if snapshot else None,
        "day_high": snapshot.day_high if snapshot else None,
        "day_low": snapshot.day_low if snapshot else None,
        "day_close": snapshot.day_close if snapshot else None,
        "day_volume": snapshot.day_volume if snapshot else None,
        "day_vwap": snapshot.day_vwap if snapshot else None,
        "min_open": snapshot.min_open if snapshot else None,
        "min_high": snapshot.min_high if snapshot else None,
        "min_low": snapshot.min_low if snapshot else None,
        "min_close": snapshot.min_close if snapshot else None,
        "min_volume": snapshot.min_volume if snapshot else None,
        "min_vwap": snapshot.min_vwap if snapshot else None,
        "min_accumulated_volume": snapshot.min_accumulated_volume if snapshot else None,
        "min_timestamp": snapshot.min_timestamp.isoformat() if snapshot and snapshot.min_timestamp else None,
        "prev_day_open": snapshot.prev_day_open if snapshot else None,
        "prev_day_high": snapshot.prev_day_high if snapshot else None,
        "prev_day_low": snapshot.prev_day_low if snapshot else None,
        "prev_day_close": snapshot.prev_day_close if snapshot else None,
        "prev_day_volume": snapshot.prev_day_volume if snapshot else None,
        "prev_day_vwap": snapshot.prev_day_vwap if snapshot else None,
        "fetched_at": snapshot.fetched_at.isoformat() if snapshot and snapshot.fetched_at else None,
        **_prediction_fields(prediction),
    }


TRADING_SYMBOLS_MAX_PAGE_SIZE = 1000
TRADING_SYMBOLS_DEFAULT_PAGE_SIZE = 500

# order_by field keys accepted by trading_symbols_report, mapped to the column they sort
# on. Ticker/name/type live on Ticker itself; todays_change_perc/day_volume live on
# current_snapshots; abs_expected_return_pct sorts on the same abs(expected_return)
# magnitude the API reports as a percentage (see withAbsExpectedReturnPct in
# frontend-v2/src/api.ts) - all three of the latter are joined into base_query below so
# they're available to order_by before LIMIT/OFFSET slices out the page.
TRADING_SYMBOLS_ORDERABLE_FIELDS: dict[str, ColumnElement] = {
    "ticker": Ticker.ticker,
    "name": Ticker.name,
    "type": Ticker.type,
    "todays_change_perc": CurrentSnapshot.todays_change_perc,
    "day_volume": CurrentSnapshot.day_volume,
    "abs_expected_return_pct": func.abs(MarketPrediction.expected_return),
}


# Shared with the frontend's numericFilter.ts NUMERIC_FILTER_OPS - the four comparison
# operators trading_symbols_report accepts for entry_price_op/market_cap_op.
NUMERIC_FILTER_OPS = ("<", "<=", ">", ">=")


def _numeric_condition_clause(column: ColumnElement, op: str, value: float) -> ColumnElement:
    if op == "<":
        return column < value
    if op == "<=":
        return column <= value
    if op == ">":
        return column > value
    if op == ">=":
        return column >= value
    raise ValueError(f"unsupported numeric filter op: {op}")  # unreachable - op is validated before this is called


def _validate_numeric_filter_pair(op: str, value: float | None, label: str) -> None:
    """Raises 422 if exactly one of op/value is given (both halves of a numeric filter
    are required together, same rule every such filter in this file follows) or if op
    isn't one of NUMERIC_FILTER_OPS. Pulled out as a helper for market_predictions_report,
    which has a dozen-odd numeric filters - inlining this 4-line check that many times
    would drown the function in boilerplate the way trading_symbols_report's three
    inlined copies don't yet."""
    if op or value is not None:
        if not op or value is None:
            raise HTTPException(422, f"{label}_op and {label}_value must be provided together")
        if op not in NUMERIC_FILTER_OPS:
            raise HTTPException(422, f"{label}_op must be one of {', '.join(NUMERIC_FILTER_OPS)}")


def _parse_order_by(order_by: str) -> list[tuple[str, str]]:
    """Parses the `order_by` query param, e.g. "todays_change_perc:desc,ticker:asc",
    into [("todays_change_perc", "desc"), ("ticker", "asc")] - the order of entries is
    the caller's requested sort priority. Raises 422 on an unknown field or direction
    rather than silently ignoring it, since this drives a paginated SQL ORDER BY and a
    silently-dropped clause would be confusing (the report would just look unsorted)."""
    fields: list[tuple[str, str]] = []
    for entry in split_csv(order_by) or []:
        field, _, direction = entry.partition(":")
        direction = direction.lower() or "asc"
        if field not in TRADING_SYMBOLS_ORDERABLE_FIELDS:
            raise HTTPException(422, f"Unknown order_by field: {field}")
        if direction not in ("asc", "desc"):
            raise HTTPException(422, f"Unknown order_by direction: {direction}")
        fields.append((field, direction))
    return fields


@app.get("/reports/trading-symbols")
def trading_symbols_report(
    ticker_types: str = "",
    tickers: str = "",
    page: int = 1,
    page_size: int = TRADING_SYMBOLS_DEFAULT_PAGE_SIZE,
    order_by: str = "",
    entry_price_op: str = "",
    entry_price_value: float | None = None,
    market_cap_op: str = "",
    market_cap_value: float | None = None,
    predicted_states: str = "",
    state_confidence_op: str = "",
    state_confidence_value: float | None = None,
) -> dict[str, Any]:
    """Backs the Analytics > Trading Symbols page's report grid: every synced tickers
    row, joined out to its asset_class, most recently computed average_volumes row, and
    current_snapshots row (both may be missing for a ticker that's never had that job
    run against it, unlike top_movers_report's mover-driven rows which always have a
    tickers row to join against).

    Paginated - `page` (1-based) and `page_size` (capped at
    TRADING_SYMBOLS_MAX_PAGE_SIZE) select a slice of the full tickers table.
    average_volumes is looked up only for that page's tickers rather than loaded in
    full, so page_size bounds the work done per request the same way it bounds the
    response size.

    `tickers` (comma-separated symbols, e.g. "AAPL,MSFT") narrows to just those tickers -
    combined with `ticker_types` as an AND, not mutually exclusive the way jobs' own
    tickers/ticker_types config fields are (see jobs/average_volume.py's
    _apply_ticker_filter): this is a report filter narrowing an already-fetched result
    set, not a job's "which population to run against" selector, so there's no
    ambiguity in letting both apply at once (e.g. "AAPL,MSFT" scoped to type "CS" is a
    perfectly sensible combination, just possibly redundant).

    `order_by` (see _parse_order_by) picks the sort priority among
    TRADING_SYMBOLS_ORDERABLE_FIELDS, applied before the page is sliced out so it
    orders the whole filtered set rather than just the returned page. Defaults to
    ticker ascending, which is also always appended as a final tiebreaker so pagination
    stays stable when the requested fields have ties (e.g. many tickers sharing the
    same day_volume).

    `entry_price_op`/`entry_price_value` (both required together, e.g. ">" / 10) filter
    on the same market_predictions.entry_price the report already joins in for
    abs_expected_return_pct ordering - applied as a real SQL WHERE (unlike ReportGrid's
    client-side numeric column filters), so it narrows `total`/pagination too, not just
    the returned page. A ticker with no market_predictions row (entry_price null) never
    matches any condition, same null-exclusion semantics as the client-side filter.

    `market_cap_op`/`market_cap_value` are the same shape, filtering on
    ticker_details.market_cap - unlike entry_price's MarketPrediction join, base_query
    doesn't otherwise join TickerDetail in (market_cap is normally resolved separately
    below, page-scoped, purely for display), so this filter adds that join itself,
    scoped to this branch, the same "only pay for it when it's actually used" reasoning
    entry_price's count_query join already uses.

    `predicted_states` (comma-separated, e.g. "up,strong_up" - see jobs/
    predict_market_state.py's STATE_LABELS for the full set) filters on the same
    market_predictions.predicted_state base_query already joins in for display/ordering -
    unvalidated against STATE_LABELS the same way `ticker_types`/`tickers` aren't
    validated against their own known values, so an unrecognized state just matches
    nothing rather than 422ing.

    `state_confidence_op`/`state_confidence_value` are the same numeric-filter shape as
    entry_price's, filtering on market_predictions.state_confidence. Both this and
    `predicted_states` reuse entry_price's count_query join (added once, guarded by
    `needs_market_prediction_join` below) rather than each adding their own - joining
    the same latest_predicted_date/MarketPrediction pair into count_query twice would be
    redundant at best and (depending on the SQLAlchemy version) an ambiguous-join error
    at worst."""
    types = split_csv(ticker_types)
    selected_tickers = split_csv(tickers)
    page = max(1, page)
    page_size = max(1, min(page_size, TRADING_SYMBOLS_MAX_PAGE_SIZE))
    order_fields = _parse_order_by(order_by) or [("ticker", "asc")]
    if "ticker" not in {field for field, _ in order_fields}:
        order_fields = [*order_fields, ("ticker", "asc")]
    if entry_price_op or entry_price_value is not None:
        if not entry_price_op or entry_price_value is None:
            raise HTTPException(422, "entry_price_op and entry_price_value must be provided together")
        if entry_price_op not in NUMERIC_FILTER_OPS:
            raise HTTPException(422, f"entry_price_op must be one of {', '.join(NUMERIC_FILTER_OPS)}")
    if market_cap_op or market_cap_value is not None:
        if not market_cap_op or market_cap_value is None:
            raise HTTPException(422, "market_cap_op and market_cap_value must be provided together")
        if market_cap_op not in NUMERIC_FILTER_OPS:
            raise HTTPException(422, f"market_cap_op must be one of {', '.join(NUMERIC_FILTER_OPS)}")
    if state_confidence_op or state_confidence_value is not None:
        if not state_confidence_op or state_confidence_value is None:
            raise HTTPException(422, "state_confidence_op and state_confidence_value must be provided together")
        if state_confidence_op not in NUMERIC_FILTER_OPS:
            raise HTTPException(422, f"state_confidence_op must be one of {', '.join(NUMERIC_FILTER_OPS)}")
    states = split_csv(predicted_states)
    with SessionLocal() as session:
        count_query = select(func.count(Ticker.ticker))
        # current_snapshots and (the latest per ticker, same pattern as
        # top_movers_report) market_predictions are joined in - rather than looked up
        # separately, as average_volumes still is below - so todays_change_perc/
        # day_volume/abs_expected_return_pct are available to order_by before
        # LIMIT/OFFSET slices out the page. Left joins throughout since a ticker that's
        # never had sync-snapshots/predict-market-state run against it still needs to
        # appear.
        latest_predicted_date = select(
            MarketPrediction.ticker, func.max(MarketPrediction.predicted_date).label("predicted_date")
        ).group_by(MarketPrediction.ticker)
        latest_predicted_date = latest_predicted_date.subquery()
        base_query = (
            select(Ticker, CurrentSnapshot, MarketPrediction)
            .outerjoin(CurrentSnapshot, CurrentSnapshot.ticker == Ticker.ticker)
            .outerjoin(latest_predicted_date, latest_predicted_date.c.ticker == Ticker.ticker)
            .outerjoin(
                MarketPrediction,
                (MarketPrediction.ticker == latest_predicted_date.c.ticker)
                & (MarketPrediction.predicted_date == latest_predicted_date.c.predicted_date),
            )
        )
        if types:
            base_query = base_query.where(Ticker.type.in_(types))
            count_query = count_query.where(Ticker.type.in_(types))
        if selected_tickers:
            base_query = base_query.where(Ticker.ticker.in_(selected_tickers))
            count_query = count_query.where(Ticker.ticker.in_(selected_tickers))
        # count_query doesn't join market_predictions by default (nothing else it counts
        # needs to) - only add the join once, shared by entry_price/predicted_states/
        # state_confidence below, so the common no-filter case stays as cheap as it was
        # before these filters existed, and the join isn't added twice (which - joining
        # the same subquery/table pair in twice - is redundant at best and an
        # ambiguous-join error at worst).
        needs_market_prediction_join = bool(entry_price_op or states or state_confidence_op)
        if needs_market_prediction_join:
            count_query = count_query.outerjoin(
                latest_predicted_date, latest_predicted_date.c.ticker == Ticker.ticker
            ).outerjoin(
                MarketPrediction,
                (MarketPrediction.ticker == latest_predicted_date.c.ticker)
                & (MarketPrediction.predicted_date == latest_predicted_date.c.predicted_date),
            )
        if entry_price_op:
            condition = _numeric_condition_clause(MarketPrediction.entry_price, entry_price_op, entry_price_value)
            base_query = base_query.where(condition)
            count_query = count_query.where(condition)
        if states:
            condition = MarketPrediction.predicted_state.in_(states)
            base_query = base_query.where(condition)
            count_query = count_query.where(condition)
        if state_confidence_op:
            condition = _numeric_condition_clause(
                MarketPrediction.state_confidence, state_confidence_op, state_confidence_value
            )
            base_query = base_query.where(condition)
            count_query = count_query.where(condition)
        if market_cap_op:
            condition = _numeric_condition_clause(TickerDetail.market_cap, market_cap_op, market_cap_value)
            # Neither query joins ticker_details by default (market_cap is normally
            # resolved separately below, page-scoped, purely for display) - add it here,
            # scoped to this branch, same reasoning as entry_price's join above. A plain
            # join (not a "latest per ticker" subquery like MarketPrediction's): unlike
            # market_predictions, ticker_details has one row per ticker already.
            base_query = base_query.outerjoin(TickerDetail, TickerDetail.ticker == Ticker.ticker).where(condition)
            count_query = count_query.outerjoin(TickerDetail, TickerDetail.ticker == Ticker.ticker).where(condition)
        total = session.execute(count_query).scalar_one()

        order_clauses = []
        for field, direction in order_fields:
            column = TRADING_SYMBOLS_ORDERABLE_FIELDS[field]
            order_clauses.append((column.desc() if direction == "desc" else column.asc()).nulls_last())
        query = base_query.order_by(*order_clauses).limit(page_size).offset((page - 1) * page_size)
        page_rows = session.execute(query).all()
        tickers = [ticker for ticker, _, _ in page_rows]
        snapshot_by_ticker: dict[str, CurrentSnapshot | None] = {
            ticker.ticker: snapshot for ticker, snapshot, _ in page_rows
        }
        prediction_by_ticker: dict[str, MarketPrediction | None] = {
            ticker.ticker: prediction for ticker, _, prediction in page_rows
        }
        ticker_codes = [ticker.ticker for ticker in tickers]

        # Same first-seen-per-code reasoning as top_movers_report.
        asset_class_by_code: dict[str, str] = {}
        for code, asset_class in session.execute(select(TickerType.code, TickerType.asset_class)).all():
            asset_class_by_code.setdefault(code, asset_class)

        # Same "latest run per ticker" pattern as top_movers_report - scoped to this
        # page's tickers (at most page_size of them) rather than every ticker that's
        # ever had an average-volume run, now that the caller only needs this page.
        latest_computed_at = (
            select(AverageVolume.ticker, func.max(AverageVolume.computed_at).label("computed_at"))
            .where(AverageVolume.ticker.in_(ticker_codes))
            .group_by(AverageVolume.ticker)
        )
        latest_computed_at = latest_computed_at.subquery()
        average_volume_by_ticker: dict[str, float | None] = {
            ticker: average_volume
            for ticker, average_volume in session.execute(
                select(AverageVolume.ticker, AverageVolume.average_volume).join(
                    latest_computed_at,
                    (AverageVolume.ticker == latest_computed_at.c.ticker)
                    & (AverageVolume.computed_at == latest_computed_at.c.computed_at),
                )
            ).all()
        }

        # Same page-scoped lookup reasoning as average_volume_by_ticker above -
        # ticker_details is upserted wholesale per ticker (no history), so no
        # latest-row-per-ticker join is needed, just a plain WHERE ... IN.
        market_cap_by_ticker: dict[str, float | None] = dict(
            session.execute(
                select(TickerDetail.ticker, TickerDetail.market_cap).where(TickerDetail.ticker.in_(ticker_codes))
            ).all()
        )

        rows = [
            _symbol_to_dict(
                ticker,
                asset_class_by_code.get(ticker.type) if ticker.type else None,
                average_volume_by_ticker.get(ticker.ticker),
                market_cap_by_ticker.get(ticker.ticker),
                snapshot_by_ticker.get(ticker.ticker),
                prediction_by_ticker.get(ticker.ticker),
            )
            for ticker in tickers
        ]
        return {"rows": rows, "total": total, "page": page, "page_size": page_size}


STALE_TICKERS_MAX_PAGE_SIZE = 1000
STALE_TICKERS_DEFAULT_PAGE_SIZE = 500
STALE_TICKERS_DEFAULT_STALE_AFTER_DAYS = 1

# One row per ticker that's ever had a daily bar synced, holding its most recent
# timestamp - built once at module level (no per-request parameters, same reasoning as
# TRADING_SYMBOLS_ORDERABLE_FIELDS) since both the WHERE filter and the order_by column
# map below need to reference the same subquery object.
_LAST_OHLC_BAR_SUBQ = (
    select(OhlcBar.ticker, func.max(OhlcBar.timestamp).label("last_ohlc_date"))
    .where(OhlcBar.multiplier == DEFAULT_MULTIPLIER, OhlcBar.timespan == DEFAULT_TIMESPAN)
    .group_by(OhlcBar.ticker)
    .subquery()
)

STALE_TICKERS_ORDERABLE_FIELDS: dict[str, ColumnElement] = {
    "ticker": Ticker.ticker,
    "name": Ticker.name,
    "type": Ticker.type,
    "last_ohlc_date": _LAST_OHLC_BAR_SUBQ.c.last_ohlc_date,
}


def _parse_stale_tickers_order_by(order_by: str) -> list[tuple[str, str]]:
    """Same shape as _parse_order_by, against STALE_TICKERS_ORDERABLE_FIELDS instead of
    TRADING_SYMBOLS_ORDERABLE_FIELDS - kept as its own function rather than
    parameterizing _parse_order_by over an allowed-fields dict, since no report so far
    has needed more than one set of orderable fields."""
    fields: list[tuple[str, str]] = []
    for entry in split_csv(order_by) or []:
        field, _, direction = entry.partition(":")
        direction = direction.lower() or "asc"
        if field not in STALE_TICKERS_ORDERABLE_FIELDS:
            raise HTTPException(422, f"Unknown order_by field: {field}")
        if direction not in ("asc", "desc"):
            raise HTTPException(422, f"Unknown order_by direction: {direction}")
        fields.append((field, direction))
    return fields


def _stale_ticker_to_dict(
    ticker: Ticker, type_class: str | None, type_description: str | None, last_ohlc_date: dt.datetime | None
) -> dict[str, Any]:
    return {
        "ticker": ticker.ticker,
        "name": ticker.name,
        "type": ticker.type,
        "type_class": type_class,
        "type_description": type_description,
        "last_ohlc_date": last_ohlc_date.date().isoformat() if last_ohlc_date else None,
    }


@app.get("/reports/stale-tickers")
def stale_tickers_report(
    ticker_types: str = "",
    stale_after_days: int = STALE_TICKERS_DEFAULT_STALE_AFTER_DAYS,
    page: int = 1,
    page_size: int = STALE_TICKERS_DEFAULT_PAGE_SIZE,
    order_by: str = "",
) -> dict[str, Any]:
    """Backs the Analytics > Stale Tickers page's report grid: every ticker whose most
    recent daily ohlc_bars row is older than `stale_after_days` days ago (UTC), or that
    has never had a daily bar synced at all - the tickers jobs/sync_bars.py's nightly
    run has fallen behind on (or never covers to begin with, e.g. asset classes that
    job has never been configured to sync).

    `stale_after_days` (default STALE_TICKERS_DEFAULT_STALE_AFTER_DAYS) sets the cutoff:
    a ticker counts as "falling behind" once its last bar is older than
    today - stale_after_days (UTC). Compared via SQL date() rather than the raw
    timestamp, since ohlc_bars.timestamp carries a few hours of intraday offset (e.g.
    "04:00:00" vs "05:00:00" depending on the bar) that would otherwise make the cutoff
    off by a day for some tickers.

    Paginated/ordered the same way trading_symbols_report is - see that function's
    docstring for the shared reasoning (page/page_size/order_by semantics, ticker
    always appended as a final tiebreaker)."""
    types = split_csv(ticker_types)
    if stale_after_days < 0:
        raise HTTPException(422, "stale_after_days must not be negative")
    page = max(1, page)
    page_size = max(1, min(page_size, STALE_TICKERS_MAX_PAGE_SIZE))
    order_fields = _parse_stale_tickers_order_by(order_by) or [("ticker", "asc")]
    if "ticker" not in {field for field, _ in order_fields}:
        order_fields = [*order_fields, ("ticker", "asc")]

    cutoff_date = dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=stale_after_days)

    with SessionLocal() as session:
        stale_filter = or_(
            _LAST_OHLC_BAR_SUBQ.c.last_ohlc_date.is_(None),
            func.date(_LAST_OHLC_BAR_SUBQ.c.last_ohlc_date) < cutoff_date.isoformat(),
        )
        count_query = (
            select(func.count(Ticker.ticker))
            .outerjoin(_LAST_OHLC_BAR_SUBQ, _LAST_OHLC_BAR_SUBQ.c.ticker == Ticker.ticker)
            .where(stale_filter)
        )
        base_query = (
            select(Ticker, _LAST_OHLC_BAR_SUBQ.c.last_ohlc_date)
            .outerjoin(_LAST_OHLC_BAR_SUBQ, _LAST_OHLC_BAR_SUBQ.c.ticker == Ticker.ticker)
            .where(stale_filter)
        )
        if types:
            count_query = count_query.where(Ticker.type.in_(types))
            base_query = base_query.where(Ticker.type.in_(types))
        total = session.execute(count_query).scalar_one()

        order_clauses = []
        for field, direction in order_fields:
            column = STALE_TICKERS_ORDERABLE_FIELDS[field]
            order_clauses.append((column.desc() if direction == "desc" else column.asc()).nulls_last())
        query = base_query.order_by(*order_clauses).limit(page_size).offset((page - 1) * page_size)
        page_rows = session.execute(query).all()
        tickers = [ticker for ticker, _ in page_rows]
        last_ohlc_by_ticker = {ticker.ticker: last_ohlc_date for ticker, last_ohlc_date in page_rows}

        # Same first-seen-per-code reasoning as trading_symbols_report's
        # asset_class_by_code, extended to also carry description (unused by any report
        # so far - trading_symbols_report only ever resolves asset_class).
        type_class_by_code: dict[str, str] = {}
        type_description_by_code: dict[str, str | None] = {}
        for code, asset_class, description in session.execute(
            select(TickerType.code, TickerType.asset_class, TickerType.description)
        ).all():
            type_class_by_code.setdefault(code, asset_class)
            type_description_by_code.setdefault(code, description)

        rows = [
            _stale_ticker_to_dict(
                ticker,
                type_class_by_code.get(ticker.type) if ticker.type else None,
                type_description_by_code.get(ticker.type) if ticker.type else None,
                last_ohlc_by_ticker.get(ticker.ticker),
            )
            for ticker in tickers
        ]
        return {"rows": rows, "total": total, "page": page, "page_size": page_size}


# start_date's fallback when the caller omits it - see backtest_report's docstring.
BACKTEST_REPORT_DEFAULT_DAYS = 15


def _backtest_point_to_dict(row: MarketPredictionBacktest) -> dict[str, Any]:
    return {
        "evaluated_date": row.evaluated_date.isoformat(),
        "predicted_state": row.predicted_state,
        "actual_state": row.actual_state,
        "predicted_correct": row.predicted_correct,
        "expected_return": row.expected_return,
        "entry_price": row.entry_price,
        "predicted_exit_price": row.predicted_exit_price,
        "actual_exit_price": row.actual_exit_price,
        "price_error_pct": row.price_error_pct,
    }


@app.get("/reports/backtest")
def backtest_report(ticker: str, start_date: str = "", end_date: str = "") -> list[dict[str, Any]]:
    """Backs the View Details chart: a single ticker's market_prediction_backtests rows
    (jobs/backtest_market_state.py's compute_market_state_backtest output) within
    [start_date, end_date], ordered by evaluated_date - predicted_exit_price and
    actual_exit_price are the two series the chart plots against each other.

    `end_date` defaults to yesterday (UTC) and `start_date` to
    BACKTEST_REPORT_DEFAULT_DAYS days before that, the same "resolve a None date at
    request time" reasoning as compute_market_state_backtest's own defaulting - except
    resolved here rather than left to the row data, since (unlike a job run) there's no
    guarantee a backtest has ever been computed for the requested window."""
    parsed_end: dt.date
    if end_date:
        try:
            parsed_end = dt.date.fromisoformat(end_date)
        except ValueError as exc:
            raise HTTPException(422, "end_date must be an ISO date, e.g. '2026-08-06'") from exc
    else:
        parsed_end = dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)

    parsed_start: dt.date
    if start_date:
        try:
            parsed_start = dt.date.fromisoformat(start_date)
        except ValueError as exc:
            raise HTTPException(422, "start_date must be an ISO date, e.g. '2026-08-06'") from exc
    else:
        parsed_start = parsed_end - dt.timedelta(days=BACKTEST_REPORT_DEFAULT_DAYS)

    if parsed_start > parsed_end:
        raise HTTPException(422, "start_date must not be after end_date")

    with SessionLocal() as session:
        rows = session.execute(
            select(MarketPredictionBacktest)
            .where(
                MarketPredictionBacktest.ticker == ticker,
                MarketPredictionBacktest.evaluated_date >= parsed_start,
                MarketPredictionBacktest.evaluated_date <= parsed_end,
            )
            .order_by(MarketPredictionBacktest.evaluated_date.asc())
        ).scalars().all()
        return [_backtest_point_to_dict(row) for row in rows]


NEXT_10_DAY_PREDICTIONS_MAX_PAGE_SIZE = 1000
NEXT_10_DAY_PREDICTIONS_DEFAULT_PAGE_SIZE = 500

# order_by field keys accepted by next_10_day_predictions_report. net_return_pct_days_1_10
# is the same ABS(day10_exit_price - day1_entry_price)*100/day10_exit_price expression
# _next_10_day_prediction_fields computes for display - defined once here as a SQL
# expression (rather than only in Python) so it's available to ORDER BY before
# LIMIT/OFFSET slices out the page, same reasoning as TRADING_SYMBOLS_ORDERABLE_FIELDS'
# abs_expected_return_pct.
_NET_RETURN_PCT_DAYS_1_10 = (
    func.abs(MarketPrediction10Day.day10_exit_price - MarketPrediction10Day.day1_entry_price)
    * 100
    / MarketPrediction10Day.day10_exit_price
)

NEXT_10_DAY_PREDICTIONS_ORDERABLE_FIELDS: dict[str, ColumnElement] = {
    "ticker": Ticker.ticker,
    "name": Ticker.name,
    "type": Ticker.type,
    "net_return_pct_days_1_10": _NET_RETURN_PCT_DAYS_1_10,
}


def _parse_next_10_day_order_by(order_by: str) -> list[tuple[str, str]]:
    """Same shape as _parse_stale_tickers_order_by, against
    NEXT_10_DAY_PREDICTIONS_ORDERABLE_FIELDS instead."""
    fields: list[tuple[str, str]] = []
    for entry in split_csv(order_by) or []:
        field, _, direction = entry.partition(":")
        direction = direction.lower() or "asc"
        if field not in NEXT_10_DAY_PREDICTIONS_ORDERABLE_FIELDS:
            raise HTTPException(422, f"Unknown order_by field: {field}")
        if direction not in ("asc", "desc"):
            raise HTTPException(422, f"Unknown order_by direction: {direction}")
        fields.append((field, direction))
    return fields


def _next_10_day_prediction_fields(prediction: MarketPrediction10Day) -> dict[str, Any]:
    """Every market_predictions_10_day column (bar ticker, which the report already
    carries as its own top-level field) plus the 3 net-return summary fields - each
    computed straight from the day1/day5/day6/day10 entry/exit prices already on the
    row rather than by compounding the per-day expected_return_pct values, since the
    price path itself (day N's entry_price is day N-1's exit_price - see
    jobs/predict_market_state_10_day.py) already *is* the compounding walk.

    Each is ABS(exit - entry) * 100 / exit - a magnitude-of-move percentage (always
    non-negative, and normalized against the *exit* price rather than the entry price),
    not a signed return - matches _NET_RETURN_PCT_DAYS_1_10's SQL expression above."""
    fields: dict[str, Any] = {
        "start_date": prediction.start_date.isoformat(),
        "current_state": prediction.current_state,
    }
    for day in range(1, 11):
        for suffix in (
            "predicted_state",
            "state_confidence",
            "entry_price",
            "exit_price",
            "expected_return_pct",
            "entry_time",
            "exit_time",
        ):
            key = f"day{day}_{suffix}"
            fields[key] = getattr(prediction, key)
    fields["net_return_pct_days_1_5"] = (
        abs(prediction.day5_exit_price - prediction.day1_entry_price) * 100 / prediction.day5_exit_price
    )
    fields["net_return_pct_days_6_10"] = (
        abs(prediction.day10_exit_price - prediction.day6_entry_price) * 100 / prediction.day10_exit_price
    )
    fields["net_return_pct_days_1_10"] = (
        abs(prediction.day10_exit_price - prediction.day1_entry_price) * 100 / prediction.day10_exit_price
    )
    fields["computed_at"] = prediction.computed_at.isoformat()
    return fields


def _next_10_day_row_to_dict(
    prediction: MarketPrediction10Day,
    ticker: Ticker,
    asset_class: str | None,
    average_volume: float | None,
    market_cap: float | None,
) -> dict[str, Any]:
    return {
        "ticker": prediction.ticker,
        "name": ticker.name,
        "type": ticker.type,
        "asset_class": asset_class,
        "average_volume": average_volume,
        "market_cap": market_cap,
        **_next_10_day_prediction_fields(prediction),
    }


@app.get("/reports/next-10-day-predictions")
def next_10_day_predictions_report(
    ticker_types: str = "",
    tickers: str = "",
    market_cap_op: str = "",
    market_cap_value: float | None = None,
    page: int = 1,
    page_size: int = NEXT_10_DAY_PREDICTIONS_DEFAULT_PAGE_SIZE,
    order_by: str = "",
) -> dict[str, Any]:
    """Backs the Analytics > Next 10 Day Predictions page's report grid: one row per
    ticker's *most recent* market_predictions_10_day run (jobs/predict_market_state_10_day.py) -
    a ticker with more than one historical start_date only ever shows its latest, same
    "latest row per ticker" pattern trading_symbols_report uses for market_predictions.
    Driven by market_predictions_10_day itself (inner join), unlike trading_symbols_report's
    every-synced-ticker sweep - a ticker this job has never run against has nothing to
    show here, so it's left out entirely rather than appearing as a blank row.

    Paginated/ordered/filtered the same way trading_symbols_report is - see that
    function's docstring for the shared `page`/`page_size`/`order_by` reasoning.
    `ticker_types`/`tickers` (comma-separated, AND-combined - same reasoning as
    trading_symbols_report's `tickers` param) and `market_cap_op`/`market_cap_value`
    (same shape as trading_symbols_report's) narrow the result set as a real SQL WHERE.

    Unlike trading_symbols_report, ticker_details is always joined in (not only when
    filtering) since market_cap is a mandatory display column here, not an optional
    page-scoped lookup."""
    types = split_csv(ticker_types)
    selected_tickers = split_csv(tickers)
    page = max(1, page)
    page_size = max(1, min(page_size, NEXT_10_DAY_PREDICTIONS_MAX_PAGE_SIZE))
    order_fields = _parse_next_10_day_order_by(order_by) or [("ticker", "asc")]
    if "ticker" not in {field for field, _ in order_fields}:
        order_fields = [*order_fields, ("ticker", "asc")]
    if market_cap_op or market_cap_value is not None:
        if not market_cap_op or market_cap_value is None:
            raise HTTPException(422, "market_cap_op and market_cap_value must be provided together")
        if market_cap_op not in NUMERIC_FILTER_OPS:
            raise HTTPException(422, f"market_cap_op must be one of {', '.join(NUMERIC_FILTER_OPS)}")

    with SessionLocal() as session:
        latest_start_date = (
            select(MarketPrediction10Day.ticker, func.max(MarketPrediction10Day.start_date).label("start_date"))
            .group_by(MarketPrediction10Day.ticker)
            .subquery()
        )
        base_query = (
            select(MarketPrediction10Day, Ticker, TickerDetail)
            .join(
                latest_start_date,
                (MarketPrediction10Day.ticker == latest_start_date.c.ticker)
                & (MarketPrediction10Day.start_date == latest_start_date.c.start_date),
            )
            .join(Ticker, Ticker.ticker == MarketPrediction10Day.ticker)
            .outerjoin(TickerDetail, TickerDetail.ticker == MarketPrediction10Day.ticker)
        )
        count_query = (
            select(func.count(MarketPrediction10Day.ticker))
            .select_from(MarketPrediction10Day)
            .join(
                latest_start_date,
                (MarketPrediction10Day.ticker == latest_start_date.c.ticker)
                & (MarketPrediction10Day.start_date == latest_start_date.c.start_date),
            )
            .join(Ticker, Ticker.ticker == MarketPrediction10Day.ticker)
        )
        if types:
            base_query = base_query.where(Ticker.type.in_(types))
            count_query = count_query.where(Ticker.type.in_(types))
        if selected_tickers:
            base_query = base_query.where(Ticker.ticker.in_(selected_tickers))
            count_query = count_query.where(Ticker.ticker.in_(selected_tickers))
        if market_cap_op:
            condition = _numeric_condition_clause(TickerDetail.market_cap, market_cap_op, market_cap_value)
            base_query = base_query.where(condition)
            # Unlike base_query, count_query doesn't join ticker_details by default -
            # only add it here, scoped to this branch, so the common no-filter case
            # stays as cheap as it was before this filter existed (same reasoning as
            # trading_symbols_report's market_cap_op branch).
            count_query = count_query.outerjoin(TickerDetail, TickerDetail.ticker == MarketPrediction10Day.ticker).where(
                condition
            )
        total = session.execute(count_query).scalar_one()

        order_clauses = []
        for field, direction in order_fields:
            column = NEXT_10_DAY_PREDICTIONS_ORDERABLE_FIELDS[field]
            order_clauses.append((column.desc() if direction == "desc" else column.asc()).nulls_last())
        query = base_query.order_by(*order_clauses).limit(page_size).offset((page - 1) * page_size)
        page_rows = session.execute(query).all()
        ticker_codes = [prediction.ticker for prediction, _, _ in page_rows]

        # Same first-seen-per-code reasoning as trading_symbols_report.
        asset_class_by_code: dict[str, str] = {}
        for code, asset_class in session.execute(select(TickerType.code, TickerType.asset_class)).all():
            asset_class_by_code.setdefault(code, asset_class)

        # Same page-scoped "latest run per ticker" pattern as trading_symbols_report.
        latest_computed_at = (
            select(AverageVolume.ticker, func.max(AverageVolume.computed_at).label("computed_at"))
            .where(AverageVolume.ticker.in_(ticker_codes))
            .group_by(AverageVolume.ticker)
        )
        latest_computed_at = latest_computed_at.subquery()
        average_volume_by_ticker: dict[str, float | None] = {
            ticker: average_volume
            for ticker, average_volume in session.execute(
                select(AverageVolume.ticker, AverageVolume.average_volume).join(
                    latest_computed_at,
                    (AverageVolume.ticker == latest_computed_at.c.ticker)
                    & (AverageVolume.computed_at == latest_computed_at.c.computed_at),
                )
            ).all()
        }

        rows = [
            _next_10_day_row_to_dict(
                prediction,
                ticker,
                asset_class_by_code.get(ticker.type) if ticker.type else None,
                average_volume_by_ticker.get(prediction.ticker),
                detail.market_cap if detail else None,
            )
            for prediction, ticker, detail in page_rows
        ]
        return {"rows": rows, "total": total, "page": page, "page_size": page_size}


MARKET_PREDICTIONS_MAX_PAGE_SIZE = 1000
MARKET_PREDICTIONS_DEFAULT_PAGE_SIZE = 500

# Backtest hit-rate per ticker (jobs/backtest_market_state.py's walk-forward
# evaluation of predict-market-state), aggregated once as a reusable subquery rather
# than built per-request - a static aggregate over market_prediction_backtests with no
# dependency on any request parameter, so it's safe to build at import time and join
# into market_predictions_report's queries by reference wherever validation_score is
# needed (SELECT/WHERE/ORDER BY). predicted_correct is a Boolean column (stored as
# 0/1 in sqlite), so func.avg over it directly yields a 0-1 hit-rate fraction with no
# case() needed. NULL for a ticker with zero backtest rows - deliberately left
# ungrouped-away so validation_score can be empty (see MarketPredictionsPage.tsx) for
# any ticker the backtest job has never covered, rather than defaulting to 0 and
# unfairly reading as "predicted wrong every time."
_BACKTEST_STATS_SUBQ = (
    select(
        MarketPredictionBacktest.ticker.label("ticker"),
        func.avg(MarketPredictionBacktest.predicted_correct).label("validation_score"),
    )
    .group_by(MarketPredictionBacktest.ticker)
    .subquery("backtest_stats")
)

# Blended risk-adjusted attractiveness score: expected_return * exit_price_confidence
# (a return scaled down by how tight/confident the prediction is) averaged across
# whichever of markov/mcmc a ticker actually has a prediction for on the requested
# date, rather than always dividing by 2 - a ticker with only one side present
# shouldn't be penalized against one with both just for missing a job run.
# market_predictions_report's inclusion_condition guarantees at least one side is
# present for any row this is evaluated against, so the divisor is never 0 in
# practice; func.nullif still guards it defensively rather than relying on that.
# _market_prediction_row_to_dict's _survivor_score is the Python-side mirror of this
# same formula, used for display once markov/mcmc rows are already fetched.
_MARKOV_SURVIVOR_COMPONENT = MarketPrediction.expected_return * MarketPrediction.exit_price_confidence
_MCMC_SURVIVOR_COMPONENT = MarketPredictionMonteCarlo.expected_return * MarketPredictionMonteCarlo.exit_price_confidence
_SURVIVOR_SIDE_COUNT = case((MarketPrediction.ticker.isnot(None), 1), else_=0) + case(
    (MarketPredictionMonteCarlo.ticker.isnot(None), 1), else_=0
)
SURVIVOR_SCORE_EXPR = (
    func.coalesce(_MARKOV_SURVIVOR_COMPONENT, 0.0) + func.coalesce(_MCMC_SURVIVOR_COMPONENT, 0.0)
) / func.nullif(cast(_SURVIVOR_SIDE_COUNT, Float), 0)

# MCMC's 10th-percentile simulated exit price, expressed as a return percentage off
# entry_price rather than a raw price - a downside/VaR-style check ("even a
# bad-percentile simulated outcome is still profitable") distinct from
# exit_price_confidence (which scores distribution tightness, not direction). Markov
# has no percentile bands of its own (see MarketPrediction vs MarketPredictionMonteCarlo's
# docstrings in db/models.py), so this filter only exists for the MCMC side.
MCMC_P10_RETURN_PCT_EXPR = (MarketPredictionMonteCarlo.exit_price_p10 / MarketPredictionMonteCarlo.entry_price - 1) * 100.0

# order_by field keys accepted by market_predictions_report, mapped to the column they
# sort on - both prediction tables are unconditionally left-joined into base_query
# below (this report's whole point is showing both side by side), so unlike
# trading_symbols_report's optional entry_price_op join, no extra count_query join is
# needed to make these orderable. average_volume/market_cap/validation_score are also
# unconditionally joined below (see market_predictions_report's docstring for why they
# stopped being page-scoped-only lookups), so they're orderable/filterable the same way.
MARKET_PREDICTIONS_ORDERABLE_FIELDS: dict[str, ColumnElement] = {
    "ticker": Ticker.ticker,
    "name": Ticker.name,
    "average_volume": AverageVolume.average_volume,
    "market_cap": TickerDetail.market_cap,
    "markov_state_confidence": MarketPrediction.state_confidence,
    "markov_expected_return": MarketPrediction.expected_return,
    "markov_exit_price_confidence": MarketPrediction.exit_price_confidence,
    "markov_history_days": MarketPrediction.history_days,
    "mcmc_state_confidence": MarketPredictionMonteCarlo.state_confidence,
    "mcmc_expected_return": MarketPredictionMonteCarlo.expected_return,
    "mcmc_exit_price_confidence": MarketPredictionMonteCarlo.exit_price_confidence,
    "mcmc_history_days": MarketPredictionMonteCarlo.history_days,
    "validation_score": _BACKTEST_STATS_SUBQ.c.validation_score,
    "survivor_score": SURVIVOR_SCORE_EXPR,
}


def _parse_market_predictions_order_by(order_by: str) -> list[tuple[str, str]]:
    """Same shape as _parse_order_by/_parse_next_10_day_order_by, against
    MARKET_PREDICTIONS_ORDERABLE_FIELDS instead."""
    fields: list[tuple[str, str]] = []
    for entry in split_csv(order_by) or []:
        field, _, direction = entry.partition(":")
        direction = direction.lower() or "asc"
        if field not in MARKET_PREDICTIONS_ORDERABLE_FIELDS:
            raise HTTPException(422, f"Unknown order_by field: {field}")
        if direction not in ("asc", "desc"):
            raise HTTPException(422, f"Unknown order_by direction: {direction}")
        fields.append((field, direction))
    return fields


def _markov_prediction_fields(prediction: MarketPrediction | None) -> dict[str, Any]:
    """markov_-prefixed fields from jobs/predict_market_state.py's MarketPrediction -
    prefixed (rather than reusing _prediction_fields' unprefixed keys) since
    market_predictions_report shows the predict-market-state job's two phases - the
    Markov chain prediction and the Monte Carlo simulation that runs immediately after
    it - side by side, and the two tables share column names. predicted_date isn't
    included here - unlike every other
    consumer of MarketPrediction, this report already knows it (the caller-supplied
    predicted_date every row is pinned to), so it's a single top-level field instead
    (see market_predictions_report)."""
    return {
        "markov_current_state": prediction.current_state if prediction else None,
        "markov_predicted_state": prediction.predicted_state if prediction else None,
        "markov_state_confidence": prediction.state_confidence if prediction else None,
        "markov_expected_return": prediction.expected_return if prediction else None,
        "markov_entry_price": prediction.entry_price if prediction else None,
        "markov_exit_price": prediction.exit_price if prediction else None,
        "markov_exit_price_confidence": prediction.exit_price_confidence if prediction else None,
        "markov_entry_time": prediction.entry_time if prediction else None,
        "markov_exit_time": prediction.exit_time if prediction else None,
        "markov_history_days": prediction.history_days if prediction else None,
        "markov_computed_at": prediction.computed_at.isoformat() if prediction else None,
    }


def _mcmc_prediction_fields(prediction: MarketPredictionMonteCarlo | None) -> dict[str, Any]:
    """Same shape/reasoning as _markov_prediction_fields above, mcmc_-prefixed, over
    jobs/predict_market_state_mcmc.py's MarketPredictionMonteCarlo."""
    return {
        "mcmc_current_state": prediction.current_state if prediction else None,
        "mcmc_predicted_state": prediction.predicted_state if prediction else None,
        "mcmc_state_confidence": prediction.state_confidence if prediction else None,
        "mcmc_expected_return": prediction.expected_return if prediction else None,
        "mcmc_entry_price": prediction.entry_price if prediction else None,
        "mcmc_exit_price": prediction.exit_price if prediction else None,
        "mcmc_exit_price_mean": prediction.exit_price_mean if prediction else None,
        "mcmc_exit_price_std": prediction.exit_price_std if prediction else None,
        "mcmc_exit_price_confidence": prediction.exit_price_confidence if prediction else None,
        "mcmc_exit_price_p10": prediction.exit_price_p10 if prediction else None,
        "mcmc_exit_price_p50": prediction.exit_price_p50 if prediction else None,
        "mcmc_exit_price_p90": prediction.exit_price_p90 if prediction else None,
        "mcmc_entry_time": prediction.entry_time if prediction else None,
        "mcmc_exit_time": prediction.exit_time if prediction else None,
        "mcmc_num_simulations": prediction.num_simulations if prediction else None,
        "mcmc_history_days": prediction.history_days if prediction else None,
        "mcmc_computed_at": prediction.computed_at.isoformat() if prediction else None,
    }


def _lstm_prediction_fields(prediction: LstmInference | None, prefix: str) -> dict[str, Any]:
    """Same shape/reasoning as _markov_prediction_fields/_mcmc_prediction_fields above,
    `prefix`-prefixed, over jobs/predict_lstm_market_state.py's LstmInference - used by
    prediction_comparison_report, called once per training_method flavor
    ("lstm_holdout"/"lstm_walkforward") since predict-lstm-market-state-holdout and
    predict-lstm-market-state-walkforward are independent jobs whose predictions for
    the same (ticker, predicted_date) both need to be shown, not just whichever is
    newest - see LstmInference's own docstring for why training_method is part of its
    primary key."""
    return {
        f"{prefix}_current_state": prediction.current_state if prediction else None,
        f"{prefix}_predicted_state": prediction.predicted_state if prediction else None,
        f"{prefix}_state_confidence": prediction.state_confidence if prediction else None,
        # Full softmax output, richer than state_confidence alone (which only surfaces
        # the predicted state's own probability) - unique to LSTM among the three
        # models, which is why _markov_prediction_fields/_mcmc_prediction_fields have no
        # equivalent.
        f"{prefix}_prob_strong_down": prediction.prob_strong_down if prediction else None,
        f"{prefix}_prob_down": prediction.prob_down if prediction else None,
        f"{prefix}_prob_flat": prediction.prob_flat if prediction else None,
        f"{prefix}_prob_up": prediction.prob_up if prediction else None,
        f"{prefix}_prob_strong_up": prediction.prob_strong_up if prediction else None,
        f"{prefix}_expected_return": prediction.expected_return if prediction else None,
        f"{prefix}_entry_price": prediction.entry_price if prediction else None,
        f"{prefix}_exit_price": prediction.exit_price if prediction else None,
        f"{prefix}_exit_price_confidence": prediction.exit_price_confidence if prediction else None,
        f"{prefix}_entry_time": prediction.entry_time if prediction else None,
        f"{prefix}_exit_time": prediction.exit_time if prediction else None,
        f"{prefix}_history_days": prediction.history_days if prediction else None,
        f"{prefix}_model_version_id": prediction.model_version_id if prediction else None,
        f"{prefix}_computed_at": prediction.computed_at.isoformat() if prediction else None,
    }


def _survivor_score(markov: MarketPrediction | None, mcmc: MarketPredictionMonteCarlo | None) -> float | None:
    """Python-side mirror of the module-level SURVIVOR_SCORE_EXPR SQL expression -
    expected_return * exit_price_confidence averaged across whichever of markov/mcmc
    is present, rather than always /2. Recomputed here (not read back from the SQL
    query) since this runs per already-fetched row purely for display; kept
    formula-identical to SURVIVOR_SCORE_EXPR on purpose so a row's displayed
    survivor_score always matches what the survivor_score_op/value filter and
    order_by=survivor_score actually sorted/filtered on.

    Checks expected_return/exit_price_confidence for None despite both being typed
    Mapped[float] (NOT NULL) in db/models.py - some rows predate that constraint and
    genuinely carry a NULL in one of these columns, which SURVIVOR_SCORE_EXPR's SQL
    already tolerates via func.coalesce but a bare Python `*` doesn't."""
    components = []
    if markov is not None and markov.expected_return is not None and markov.exit_price_confidence is not None:
        components.append(markov.expected_return * markov.exit_price_confidence)
    if mcmc is not None and mcmc.expected_return is not None and mcmc.exit_price_confidence is not None:
        components.append(mcmc.expected_return * mcmc.exit_price_confidence)
    return sum(components) / len(components) if components else None


def _market_prediction_row_to_dict(
    predicted_date: dt.date,
    ticker: Ticker,
    average_volume: float | None,
    market_cap: float | None,
    validation_score: float | None,
    markov: MarketPrediction | None,
    mcmc: MarketPredictionMonteCarlo | None,
) -> dict[str, Any]:
    markov_fields = _markov_prediction_fields(markov)
    mcmc_fields = _mcmc_prediction_fields(mcmc)
    return {
        "ticker": ticker.ticker,
        "name": ticker.name,
        "average_volume": average_volume,
        "market_cap": market_cap,
        # Backtest hit-rate (validation_score, empty/None when the ticker has no
        # market_prediction_backtests rows) and the blended expected_return x
        # exit_price_confidence blend (survivor_score, see _survivor_score) - both
        # ticker-level scoring fields like average_volume/market_cap above, not paired
        # markov_x/mcmc_x columns, so they sit alongside those rather than interleaved
        # into the markov/mcmc block below.
        "validation_score": validation_score,
        "survivor_score": _survivor_score(markov, mcmc),
        # Not markov's/mcmc's own predicted_date (there's only one column - see
        # _markov_prediction_fields) - always the requested date, since every row is
        # pinned to it regardless of which side(s) actually matched.
        "predicted_date": predicted_date.isoformat(),
        # Interleaved markov_x/mcmc_x pairs (not two grouped blocks) so the JSON key
        # order matches the report grid's column order (see MarketPredictionsPage.tsx's
        # COLUMNS) - mcmc_exit_price_mean/std/p10/p50/p90 and mcmc_num_simulations have
        # no markov_ counterpart to pair with, so they're interleaved right after the
        # shared field they elaborate on instead (exit_price_confidence, history_days).
        "markov_current_state": markov_fields["markov_current_state"],
        "mcmc_current_state": mcmc_fields["mcmc_current_state"],
        "markov_predicted_state": markov_fields["markov_predicted_state"],
        "mcmc_predicted_state": mcmc_fields["mcmc_predicted_state"],
        "markov_state_confidence": markov_fields["markov_state_confidence"],
        "mcmc_state_confidence": mcmc_fields["mcmc_state_confidence"],
        "markov_expected_return": markov_fields["markov_expected_return"],
        "mcmc_expected_return": mcmc_fields["mcmc_expected_return"],
        "markov_entry_price": markov_fields["markov_entry_price"],
        "mcmc_entry_price": mcmc_fields["mcmc_entry_price"],
        "markov_exit_price": markov_fields["markov_exit_price"],
        "mcmc_exit_price": mcmc_fields["mcmc_exit_price"],
        "markov_exit_price_confidence": markov_fields["markov_exit_price_confidence"],
        "mcmc_exit_price_confidence": mcmc_fields["mcmc_exit_price_confidence"],
        "mcmc_exit_price_mean": mcmc_fields["mcmc_exit_price_mean"],
        "mcmc_exit_price_std": mcmc_fields["mcmc_exit_price_std"],
        "mcmc_exit_price_p10": mcmc_fields["mcmc_exit_price_p10"],
        "mcmc_exit_price_p50": mcmc_fields["mcmc_exit_price_p50"],
        "mcmc_exit_price_p90": mcmc_fields["mcmc_exit_price_p90"],
        "markov_entry_time": markov_fields["markov_entry_time"],
        "mcmc_entry_time": mcmc_fields["mcmc_entry_time"],
        "markov_exit_time": markov_fields["markov_exit_time"],
        "mcmc_exit_time": mcmc_fields["mcmc_exit_time"],
        "mcmc_num_simulations": mcmc_fields["mcmc_num_simulations"],
        "markov_history_days": markov_fields["markov_history_days"],
        "mcmc_history_days": mcmc_fields["mcmc_history_days"],
        "markov_computed_at": markov_fields["markov_computed_at"],
        "mcmc_computed_at": mcmc_fields["mcmc_computed_at"],
    }


@app.get("/reports/market-predictions")
def market_predictions_report(
    predicted_date: str,
    ticker_types: str = "",
    tickers: str = "",
    markov_exit_price_confidence_op: str = "",
    markov_exit_price_confidence_value: float | None = None,
    mcmc_exit_price_confidence_op: str = "",
    mcmc_exit_price_confidence_value: float | None = None,
    average_volume_op: str = "",
    average_volume_value: float | None = None,
    market_cap_op: str = "",
    market_cap_value: float | None = None,
    markov_history_days_op: str = "",
    markov_history_days_value: float | None = None,
    mcmc_history_days_op: str = "",
    mcmc_history_days_value: float | None = None,
    markov_state_confidence_op: str = "",
    markov_state_confidence_value: float | None = None,
    mcmc_state_confidence_op: str = "",
    mcmc_state_confidence_value: float | None = None,
    markov_expected_return_op: str = "",
    markov_expected_return_value: float | None = None,
    mcmc_expected_return_op: str = "",
    mcmc_expected_return_value: float | None = None,
    markov_predicted_states: str = "",
    mcmc_predicted_states: str = "",
    require_consensus: bool = False,
    mcmc_p10_return_pct_op: str = "",
    mcmc_p10_return_pct_value: float | None = None,
    validation_score_op: str = "",
    validation_score_value: float | None = None,
    survivor_score_op: str = "",
    survivor_score_value: float | None = None,
    page: int = 1,
    page_size: int = MARKET_PREDICTIONS_DEFAULT_PAGE_SIZE,
    order_by: str = "",
) -> dict[str, Any]:
    """Backs the Analytics > Market Predictions page's report grid.

    `predicted_date` (ISO date, e.g. "2026-08-11") is required, unlike every other
    report in this file - every row this endpoint returns is for exactly this one
    date. The caller picks the date up front and the report shows whichever of the two
    jobs' predictions exist for it, so `predicted_date` in the response is a single,
    always-populated column (just the requested value echoed back, never null) rather
    than separate markov_predicted_date/mcmc_predicted_date columns that could each be
    null or disagree.

    One query, driven from Ticker: MarketPrediction and MarketPredictionMonteCarlo are
    both LEFT JOINed in, each pinned to predicted_date *in the join's ON clause*
    (`base_query`/`count_query` below) rather than filtered via WHERE - the ON clause
    is what lets a ticker whose Markov row doesn't exist for this date still show its
    Monte Carlo row (and vice versa) instead of the join silently degrading into an
    inner join, which is what would happen if that condition were a WHERE instead.
    AverageVolume (latest-per-ticker, joined the same "max computed_at" way
    trading_symbols_report resolves it), TickerDetail, and _BACKTEST_STATS_SUBQ
    (validation_score) are also unconditionally left-joined now - unlike this report's
    earlier version, average_volume/market_cap/validation_score are no longer
    page-scoped display-only lookups, so they can be filtered/ordered on before
    pagination the same way the markov_/mcmc_ columns already were.

    A ticker only needs a prediction from *either* source to appear at all -
    `inclusion_condition` below starts as
    `MarketPrediction.ticker IS NOT NULL OR MarketPredictionMonteCarlo.ticker IS NOT NULL`.
    Every markov_*/mcmc_* filter pair (exit_price_confidence, history_days,
    state_confidence, expected_return, predicted_states, plus mcmc's own
    p10_return_pct) is then AND'd onto that directly as an independent global condition,
    not split per-side and OR'd together: setting markov_expected_return_op filters on
    Markov's expected_return regardless of what MCMC's row looks like, and vice versa. A
    ticker missing the relevant side's row for this date has that column as NULL, and any
    op comparison against NULL is false, so a set filter naturally excludes tickers that
    don't have a satisfying value on that side rather than passing them through via the
    other side.

    `average_volume_op`/`market_cap_op`/`validation_score_op`/`survivor_score_op` (each
    paired with `..._value`) and `require_consensus` are different: they're global
    AND conditions applied on top of inclusion_condition, not gated to one side, since
    liquidity/backtest-accuracy/blended-score/agreement aren't markov- or mcmc-specific
    concepts. `require_consensus=true` adds
    `MarketPrediction.ticker IS NOT NULL AND MarketPredictionMonteCarlo.ticker IS NOT NULL
     AND MarketPrediction.predicted_state = MarketPredictionMonteCarlo.predicted_state`
    - both sides must be present *and* agree on the predicted state. Combined with a
    one-sided `markov_predicted_states`/`mcmc_predicted_states` filter (e.g.
    markov_predicted_states=up,strong_up), this also pins the *other* side to the same
    state set transitively, without needing to duplicate the filter on both sides.

    `validation_score_op`/`validation_score_value` filter on _BACKTEST_STATS_SUBQ's
    per-ticker backtest hit rate (jobs/backtest_market_state.py's walk-forward
    predicted_correct, averaged) - NULL for a ticker with no backtest rows, which never
    matches any numeric comparison (same null-exclusion semantics every filter here
    has), so leaving this filter unset is required to see tickers the backtest job
    hasn't covered.

    `survivor_score_op`/`survivor_score_value` filter on SURVIVOR_SCORE_EXPR, the
    blended expected_return x exit_price_confidence score averaged across whichever of
    markov/mcmc is present (see that expression's own comment).

    `mcmc_p10_return_pct_op`/`mcmc_p10_return_pct_value` filter on
    MCMC_P10_RETURN_PCT_EXPR - MCMC's simulated 10th-percentile exit price as a %
    return off entry_price, e.g. `>= 0` to require even a bad-percentile simulated
    outcome to still be profitable. Gated inside mcmc_condition (mcmc-only, no Markov
    equivalent - see that expression's own comment).

    Otherwise ordered/paginated/filtered the same way trading_symbols_report is - see
    that function's docstring for the shared `page`/`page_size`/`order_by`/
    `ticker_types`/`tickers` reasoning. `order_by` picks among
    MARKET_PREDICTIONS_ORDERABLE_FIELDS, defaulting to ticker ascending, always
    appended as a tiebreaker (same reasoning as trading_symbols_report)."""
    try:
        parsed_predicted_date = dt.date.fromisoformat(predicted_date)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="predicted_date must be an ISO date, e.g. '2026-08-11'") from exc

    types = split_csv(ticker_types)
    selected_tickers = split_csv(tickers)
    markov_states = split_csv(markov_predicted_states)
    mcmc_states = split_csv(mcmc_predicted_states)
    page = max(1, page)
    page_size = max(1, min(page_size, MARKET_PREDICTIONS_MAX_PAGE_SIZE))
    order_fields = _parse_market_predictions_order_by(order_by) or [("ticker", "asc")]
    if "ticker" not in {field for field, _ in order_fields}:
        order_fields = [*order_fields, ("ticker", "asc")]

    for op, value, label in [
        (markov_exit_price_confidence_op, markov_exit_price_confidence_value, "markov_exit_price_confidence"),
        (mcmc_exit_price_confidence_op, mcmc_exit_price_confidence_value, "mcmc_exit_price_confidence"),
        (average_volume_op, average_volume_value, "average_volume"),
        (market_cap_op, market_cap_value, "market_cap"),
        (markov_history_days_op, markov_history_days_value, "markov_history_days"),
        (mcmc_history_days_op, mcmc_history_days_value, "mcmc_history_days"),
        (markov_state_confidence_op, markov_state_confidence_value, "markov_state_confidence"),
        (mcmc_state_confidence_op, mcmc_state_confidence_value, "mcmc_state_confidence"),
        (markov_expected_return_op, markov_expected_return_value, "markov_expected_return"),
        (mcmc_expected_return_op, mcmc_expected_return_value, "mcmc_expected_return"),
        (mcmc_p10_return_pct_op, mcmc_p10_return_pct_value, "mcmc_p10_return_pct"),
        (validation_score_op, validation_score_value, "validation_score"),
        (survivor_score_op, survivor_score_value, "survivor_score"),
    ]:
        _validate_numeric_filter_pair(op, value, label)

    with SessionLocal() as session:
        # Pinned in the ON clause (not a WHERE) so a ticker missing one side's row for
        # this date still keeps the other side's - see this function's docstring.
        markov_join = (MarketPrediction.ticker == Ticker.ticker) & (MarketPrediction.predicted_date == parsed_predicted_date)
        mcmc_join = (MarketPredictionMonteCarlo.ticker == Ticker.ticker) & (
            MarketPredictionMonteCarlo.predicted_date == parsed_predicted_date
        )

        # A ticker only needs a prediction from *either* source to appear at all - this
        # presence check is deliberately separate from (and unaffected by) the markov_*/
        # mcmc_* filters below, which are now independent global AND conditions rather
        # than being split per-side and OR'd together. Previously each markov_*/mcmc_*
        # filter only gated its own side of a `markov_condition | mcmc_condition` OR, so
        # e.g. setting only markov_expected_return_op let any ticker with an MCMC row in
        # regardless of its Markov expected_return (and vice versa) - a row could be
        # admitted via one side while displaying an unfiltered, filter-failing value from
        # the other. Every filter below now applies directly to its column: unset filters
        # are simply not added (equivalent to `... IS NULL OR column op value`, but
        # skipping the clause entirely reads clearer than spelling out the null-check),
        # and a set filter always constrains that column - if the relevant prediction row
        # doesn't exist, the column is NULL and any op comparison against it is false,
        # which correctly excludes the ticker rather than vacuously passing.
        inclusion_condition = MarketPrediction.ticker.isnot(None) | MarketPredictionMonteCarlo.ticker.isnot(None)
        if markov_exit_price_confidence_op:
            inclusion_condition = inclusion_condition & _numeric_condition_clause(
                MarketPrediction.exit_price_confidence, markov_exit_price_confidence_op, markov_exit_price_confidence_value
            )
        if markov_history_days_op:
            inclusion_condition = inclusion_condition & _numeric_condition_clause(
                MarketPrediction.history_days, markov_history_days_op, markov_history_days_value
            )
        if markov_state_confidence_op:
            inclusion_condition = inclusion_condition & _numeric_condition_clause(
                MarketPrediction.state_confidence, markov_state_confidence_op, markov_state_confidence_value
            )
        if markov_expected_return_op:
            inclusion_condition = inclusion_condition & _numeric_condition_clause(
                MarketPrediction.expected_return, markov_expected_return_op, markov_expected_return_value
            )
        if markov_states:
            inclusion_condition = inclusion_condition & MarketPrediction.predicted_state.in_(markov_states)

        if mcmc_exit_price_confidence_op:
            inclusion_condition = inclusion_condition & _numeric_condition_clause(
                MarketPredictionMonteCarlo.exit_price_confidence, mcmc_exit_price_confidence_op, mcmc_exit_price_confidence_value
            )
        if mcmc_history_days_op:
            inclusion_condition = inclusion_condition & _numeric_condition_clause(
                MarketPredictionMonteCarlo.history_days, mcmc_history_days_op, mcmc_history_days_value
            )
        if mcmc_state_confidence_op:
            inclusion_condition = inclusion_condition & _numeric_condition_clause(
                MarketPredictionMonteCarlo.state_confidence, mcmc_state_confidence_op, mcmc_state_confidence_value
            )
        if mcmc_expected_return_op:
            inclusion_condition = inclusion_condition & _numeric_condition_clause(
                MarketPredictionMonteCarlo.expected_return, mcmc_expected_return_op, mcmc_expected_return_value
            )
        if mcmc_states:
            inclusion_condition = inclusion_condition & MarketPredictionMonteCarlo.predicted_state.in_(mcmc_states)
        if mcmc_p10_return_pct_op:
            inclusion_condition = inclusion_condition & _numeric_condition_clause(
                MCMC_P10_RETURN_PCT_EXPR, mcmc_p10_return_pct_op, mcmc_p10_return_pct_value
            )

        # Latest AverageVolume row per ticker - same "max computed_at" subquery+join
        # pattern trading_symbols_report/this report's own predecessor used page-scoped,
        # now built unconditionally (not scoped to a known page of tickers, since
        # filtering/ordering on it has to happen before pagination picks a page).
        latest_avg_vol_dates = (
            select(AverageVolume.ticker, func.max(AverageVolume.computed_at).label("computed_at"))
            .group_by(AverageVolume.ticker)
            .subquery()
        )
        avg_vol_join = (AverageVolume.ticker == latest_avg_vol_dates.c.ticker) & (
            AverageVolume.computed_at == latest_avg_vol_dates.c.computed_at
        )

        count_query = (
            select(func.count(Ticker.ticker))
            .outerjoin(MarketPrediction, markov_join)
            .outerjoin(MarketPredictionMonteCarlo, mcmc_join)
            .outerjoin(latest_avg_vol_dates, latest_avg_vol_dates.c.ticker == Ticker.ticker)
            .outerjoin(AverageVolume, avg_vol_join)
            .outerjoin(TickerDetail, TickerDetail.ticker == Ticker.ticker)
            .outerjoin(_BACKTEST_STATS_SUBQ, _BACKTEST_STATS_SUBQ.c.ticker == Ticker.ticker)
            .where(inclusion_condition)
        )
        base_query = (
            select(Ticker, MarketPrediction, MarketPredictionMonteCarlo, AverageVolume, TickerDetail, _BACKTEST_STATS_SUBQ.c.validation_score)
            .outerjoin(MarketPrediction, markov_join)
            .outerjoin(MarketPredictionMonteCarlo, mcmc_join)
            .outerjoin(latest_avg_vol_dates, latest_avg_vol_dates.c.ticker == Ticker.ticker)
            .outerjoin(AverageVolume, avg_vol_join)
            .outerjoin(TickerDetail, TickerDetail.ticker == Ticker.ticker)
            .outerjoin(_BACKTEST_STATS_SUBQ, _BACKTEST_STATS_SUBQ.c.ticker == Ticker.ticker)
            .where(inclusion_condition)
        )
        if types:
            base_query = base_query.where(Ticker.type.in_(types))
            count_query = count_query.where(Ticker.type.in_(types))
        if selected_tickers:
            base_query = base_query.where(Ticker.ticker.in_(selected_tickers))
            count_query = count_query.where(Ticker.ticker.in_(selected_tickers))
        if average_volume_op:
            condition = _numeric_condition_clause(AverageVolume.average_volume, average_volume_op, average_volume_value)
            base_query = base_query.where(condition)
            count_query = count_query.where(condition)
        if market_cap_op:
            condition = _numeric_condition_clause(TickerDetail.market_cap, market_cap_op, market_cap_value)
            base_query = base_query.where(condition)
            count_query = count_query.where(condition)
        if validation_score_op:
            condition = _numeric_condition_clause(
                _BACKTEST_STATS_SUBQ.c.validation_score, validation_score_op, validation_score_value
            )
            base_query = base_query.where(condition)
            count_query = count_query.where(condition)
        if survivor_score_op:
            condition = _numeric_condition_clause(SURVIVOR_SCORE_EXPR, survivor_score_op, survivor_score_value)
            base_query = base_query.where(condition)
            count_query = count_query.where(condition)
        if require_consensus:
            condition = (
                MarketPrediction.ticker.isnot(None)
                & MarketPredictionMonteCarlo.ticker.isnot(None)
                & (MarketPrediction.predicted_state == MarketPredictionMonteCarlo.predicted_state)
            )
            base_query = base_query.where(condition)
            count_query = count_query.where(condition)
        total = session.execute(count_query).scalar_one()

        order_clauses = []
        for field, direction in order_fields:
            column = MARKET_PREDICTIONS_ORDERABLE_FIELDS[field]
            order_clauses.append((column.desc() if direction == "desc" else column.asc()).nulls_last())
        query = base_query.order_by(*order_clauses).limit(page_size).offset((page - 1) * page_size)
        page_rows = session.execute(query).all()
        page_tickers = [row[0] for row in page_rows]
        markov_by_ticker = {row[0].ticker: row[1] for row in page_rows}
        mcmc_by_ticker = {row[0].ticker: row[2] for row in page_rows}
        average_volume_by_ticker = {row[0].ticker: row[3].average_volume if row[3] is not None else None for row in page_rows}
        market_cap_by_ticker = {row[0].ticker: row[4].market_cap if row[4] is not None else None for row in page_rows}
        validation_score_by_ticker = {row[0].ticker: row[5] for row in page_rows}

        rows = [
            _market_prediction_row_to_dict(
                parsed_predicted_date,
                ticker,
                average_volume_by_ticker.get(ticker.ticker),
                market_cap_by_ticker.get(ticker.ticker),
                validation_score_by_ticker.get(ticker.ticker),
                markov_by_ticker.get(ticker.ticker),
                mcmc_by_ticker.get(ticker.ticker),
            )
            for ticker in page_tickers
        ]
        return {"rows": rows, "total": total, "page": page, "page_size": page_size}


# Two independent aliases of LstmInference, one per training_method flavor - predict-
# lstm-market-state-holdout/-walkforward each write their own (ticker, predicted_date,
# training_method) rows (see LstmInference's docstring), so a single unaliased join
# would fan out into two rows per ticker whenever both flavors have a prediction for
# the same date. Joining each alias with training_method pinned in its own ON clause
# keeps this report at one row per ticker, with both flavors' columns side by side -
# same "pin the filter in the join's ON clause, not a WHERE" reasoning every other join
# in this report already uses for predicted_date.
LstmInferenceHoldout = aliased(LstmInference, name="lstm_holdout")
LstmInferenceWalkforward = aliased(LstmInference, name="lstm_walkforward")

# Orderable fields for prediction_comparison_report - a much smaller set than
# MARKET_PREDICTIONS_ORDERABLE_FIELDS above, since this report is a focused four-way
# comparison view, not another screening tool (see prediction_comparison_report's
# docstring for why it deliberately doesn't replicate that report's dozen filters).
PREDICTION_COMPARISON_ORDERABLE_FIELDS: dict[str, ColumnElement] = {
    "ticker": Ticker.ticker,
    "name": Ticker.name,
    "markov_predicted_state": MarketPrediction.predicted_state,
    "markov_state_confidence": MarketPrediction.state_confidence,
    "markov_expected_return": MarketPrediction.expected_return,
    "mcmc_predicted_state": MarketPredictionMonteCarlo.predicted_state,
    "mcmc_state_confidence": MarketPredictionMonteCarlo.state_confidence,
    "mcmc_expected_return": MarketPredictionMonteCarlo.expected_return,
    "lstm_holdout_predicted_state": LstmInferenceHoldout.predicted_state,
    "lstm_holdout_state_confidence": LstmInferenceHoldout.state_confidence,
    "lstm_holdout_expected_return": LstmInferenceHoldout.expected_return,
    "lstm_walkforward_predicted_state": LstmInferenceWalkforward.predicted_state,
    "lstm_walkforward_state_confidence": LstmInferenceWalkforward.state_confidence,
    "lstm_walkforward_expected_return": LstmInferenceWalkforward.expected_return,
}


def _parse_prediction_comparison_order_by(order_by: str) -> list[tuple[str, str]]:
    """Same shape as _parse_market_predictions_order_by, against
    PREDICTION_COMPARISON_ORDERABLE_FIELDS instead."""
    fields: list[tuple[str, str]] = []
    for entry in split_csv(order_by) or []:
        field, _, direction = entry.partition(":")
        direction = direction.lower() or "asc"
        if field not in PREDICTION_COMPARISON_ORDERABLE_FIELDS:
            raise HTTPException(422, f"Unknown order_by field: {field}")
        if direction not in ("asc", "desc"):
            raise HTTPException(422, f"Unknown order_by direction: {direction}")
        fields.append((field, direction))
    return fields


@app.get("/reports/prediction-comparison")
def prediction_comparison_report(
    predicted_date: str,
    ticker_types: str = "",
    tickers: str = "",
    page: int = 1,
    page_size: int = MARKET_PREDICTIONS_DEFAULT_PAGE_SIZE,
    order_by: str = "",
) -> dict[str, Any]:
    """Backs the Prediction Comparison page's report grid - the Markov chain
    (MarketPrediction), Monte Carlo (MarketPredictionMonteCarlo), and both LSTM flavors'
    (LstmInferenceHoldout/LstmInferenceWalkforward - see those aliases' comment)
    predictions for a chosen predicted date, shown side by side per ticker, so all four
    can be compared directly by ticker and prediction date.

    Deliberately a focused comparison view rather than a fifth copy of
    market_predictions_report's dozen screening filters (exit-price-confidence,
    survivor-score, consensus, average-volume/market-cap, ...) - those stay specific to
    that report; this one only takes ticker_types/tickers/page/page_size/order_by, same
    reasoning as trading_symbols_report's shared params (see that function's docstring).

    One query, driven from Ticker: all four prediction sources are LEFT JOINed in, each
    pinned to predicted_date *in the join's ON clause* (same reasoning as
    market_predictions_report's markov_join/mcmc_join - an inner-join-shaped WHERE would
    silently drop a ticker missing one source's row for this date); the two LSTM aliases
    additionally pin their own training_method, so a ticker with both flavors' rows for
    this date still comes back as one row, not two. A ticker only needs a prediction
    from *any one* of the four sources to appear at all.

    `order_by` picks among PREDICTION_COMPARISON_ORDERABLE_FIELDS, defaulting to ticker
    ascending, always appended as a tiebreaker (same reasoning as
    market_predictions_report)."""
    try:
        parsed_predicted_date = dt.date.fromisoformat(predicted_date)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="predicted_date must be an ISO date, e.g. '2026-08-11'") from exc

    types = split_csv(ticker_types)
    selected_tickers = split_csv(tickers)
    page = max(1, page)
    page_size = max(1, min(page_size, MARKET_PREDICTIONS_MAX_PAGE_SIZE))
    order_fields = _parse_prediction_comparison_order_by(order_by) or [("ticker", "asc")]
    if "ticker" not in {field for field, _ in order_fields}:
        order_fields = [*order_fields, ("ticker", "asc")]

    with SessionLocal() as session:
        markov_join = (MarketPrediction.ticker == Ticker.ticker) & (
            MarketPrediction.predicted_date == parsed_predicted_date
        )
        mcmc_join = (MarketPredictionMonteCarlo.ticker == Ticker.ticker) & (
            MarketPredictionMonteCarlo.predicted_date == parsed_predicted_date
        )
        lstm_holdout_join = (
            (LstmInferenceHoldout.ticker == Ticker.ticker)
            & (LstmInferenceHoldout.predicted_date == parsed_predicted_date)
            & (LstmInferenceHoldout.training_method == "holdout")
        )
        lstm_walkforward_join = (
            (LstmInferenceWalkforward.ticker == Ticker.ticker)
            & (LstmInferenceWalkforward.predicted_date == parsed_predicted_date)
            & (LstmInferenceWalkforward.training_method == "walkforward")
        )
        inclusion_condition = (
            MarketPrediction.ticker.isnot(None)
            | MarketPredictionMonteCarlo.ticker.isnot(None)
            | LstmInferenceHoldout.ticker.isnot(None)
            | LstmInferenceWalkforward.ticker.isnot(None)
        )

        count_query = (
            select(func.count(Ticker.ticker))
            .outerjoin(MarketPrediction, markov_join)
            .outerjoin(MarketPredictionMonteCarlo, mcmc_join)
            .outerjoin(LstmInferenceHoldout, lstm_holdout_join)
            .outerjoin(LstmInferenceWalkforward, lstm_walkforward_join)
            .where(inclusion_condition)
        )
        base_query = (
            select(Ticker, MarketPrediction, MarketPredictionMonteCarlo, LstmInferenceHoldout, LstmInferenceWalkforward)
            .outerjoin(MarketPrediction, markov_join)
            .outerjoin(MarketPredictionMonteCarlo, mcmc_join)
            .outerjoin(LstmInferenceHoldout, lstm_holdout_join)
            .outerjoin(LstmInferenceWalkforward, lstm_walkforward_join)
            .where(inclusion_condition)
        )
        if types:
            base_query = base_query.where(Ticker.type.in_(types))
            count_query = count_query.where(Ticker.type.in_(types))
        if selected_tickers:
            base_query = base_query.where(Ticker.ticker.in_(selected_tickers))
            count_query = count_query.where(Ticker.ticker.in_(selected_tickers))
        total = session.execute(count_query).scalar_one()

        order_clauses = []
        for field, direction in order_fields:
            column = PREDICTION_COMPARISON_ORDERABLE_FIELDS[field]
            order_clauses.append((column.desc() if direction == "desc" else column.asc()).nulls_last())
        query = base_query.order_by(*order_clauses).limit(page_size).offset((page - 1) * page_size)
        page_rows = session.execute(query).all()

        rows = [
            {
                "ticker": ticker.ticker,
                "name": ticker.name,
                "predicted_date": parsed_predicted_date.isoformat(),
                **_markov_prediction_fields(markov),
                **_mcmc_prediction_fields(mcmc),
                **_lstm_prediction_fields(lstm_holdout, "lstm_holdout"),
                **_lstm_prediction_fields(lstm_walkforward, "lstm_walkforward"),
            }
            for ticker, markov, mcmc, lstm_holdout, lstm_walkforward in page_rows
        ]
        return {"rows": rows, "total": total, "page": page, "page_size": page_size}


def _prediction_accuracy_source_fields(row: PredictionAccuracy, prefix: str) -> dict[str, Any]:
    """One quadruple's worth of columns off a prediction_accuracy row, `prefix`-
    prefixed - used by prediction_accuracy_report, called once per source
    ("markov"/"mcmc"/"lstm_holdout"/"lstm_walkforward") since PredictionAccuracy
    itself stores all four side by side rather than one row per source (see that
    table's own docstring)."""
    return {
        f"{prefix}_predicted_exit_price": getattr(row, f"{prefix}_predicted_exit_price"),
        f"{prefix}_error": getattr(row, f"{prefix}_error"),
        f"{prefix}_error_std": getattr(row, f"{prefix}_error_std"),
        f"{prefix}_passed": getattr(row, f"{prefix}_passed"),
    }


# Orderable fields for prediction_accuracy_report - same "focused view, not a dozen
# screening filters" scope as PREDICTION_COMPARISON_ORDERABLE_FIELDS above.
PREDICTION_ACCURACY_ORDERABLE_FIELDS: dict[str, ColumnElement] = {
    "ticker": Ticker.ticker,
    "name": Ticker.name,
    "predicted_date": PredictionAccuracy.predicted_date,
    "price_std": PredictionAccuracy.price_std,
    "markov_error_std": PredictionAccuracy.markov_error_std,
    "markov_passed": PredictionAccuracy.markov_passed,
    "mcmc_error_std": PredictionAccuracy.mcmc_error_std,
    "mcmc_passed": PredictionAccuracy.mcmc_passed,
    "lstm_holdout_error_std": PredictionAccuracy.lstm_holdout_error_std,
    "lstm_holdout_passed": PredictionAccuracy.lstm_holdout_passed,
    "lstm_walkforward_error_std": PredictionAccuracy.lstm_walkforward_error_std,
    "lstm_walkforward_passed": PredictionAccuracy.lstm_walkforward_passed,
}


def _parse_prediction_accuracy_order_by(order_by: str) -> list[tuple[str, str]]:
    """Same shape as _parse_prediction_comparison_order_by, against
    PREDICTION_ACCURACY_ORDERABLE_FIELDS instead."""
    fields: list[tuple[str, str]] = []
    for entry in split_csv(order_by) or []:
        field, _, direction = entry.partition(":")
        direction = direction.lower() or "asc"
        if field not in PREDICTION_ACCURACY_ORDERABLE_FIELDS:
            raise HTTPException(422, f"Unknown order_by field: {field}")
        if direction not in ("asc", "desc"):
            raise HTTPException(422, f"Unknown order_by direction: {direction}")
        fields.append((field, direction))
    return fields


@app.get("/reports/prediction-accuracy")
def prediction_accuracy_report(
    start_date: str = "",
    end_date: str = "",
    ticker_types: str = "",
    tickers: str = "",
    page: int = 1,
    page_size: int = MARKET_PREDICTIONS_DEFAULT_PAGE_SIZE,
    order_by: str = "",
) -> dict[str, Any]:
    """Backs the Prediction Accuracy page's report grid - jobs/prediction_accuracy.py's
    compute_prediction_accuracy already stores all four sources' (Markov/Monte
    Carlo/LSTM holdout/LSTM walk-forward) predicted-vs-actual scoring side by side per
    (ticker, predicted_date) in the prediction_accuracy table, so this report is a
    straight paginated read off it (joined to Ticker for name/type filtering) rather
    than an ORM aliased-join assembly the way prediction_comparison_report's is - the
    join work already happened once, at compute time, not on every report read.

    `start_date`/`end_date` (ISO dates, each independently optional) narrow to
    predicted_date within [start_date, end_date] - blank means unbounded on that side,
    so a bare request shows every scored (ticker, predicted_date) rather than
    requiring a single date the way prediction_comparison_report does (this table
    accumulates one row per evaluation event over time, not a single date's
    snapshot).

    `order_by` picks among PREDICTION_ACCURACY_ORDERABLE_FIELDS, defaulting to
    predicted_date descending (most recent evaluation first) then ticker ascending as
    a tiebreaker."""
    parsed_start_date: dt.date | None = None
    if start_date:
        try:
            parsed_start_date = dt.date.fromisoformat(start_date)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="start_date must be an ISO date, e.g. '2026-08-11'") from exc
    parsed_end_date: dt.date | None = None
    if end_date:
        try:
            parsed_end_date = dt.date.fromisoformat(end_date)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="end_date must be an ISO date, e.g. '2026-08-11'") from exc
    if parsed_start_date is not None and parsed_end_date is not None and parsed_start_date > parsed_end_date:
        raise HTTPException(status_code=422, detail="start_date must not be after end_date")

    types = split_csv(ticker_types)
    selected_tickers = split_csv(tickers)
    page = max(1, page)
    page_size = max(1, min(page_size, MARKET_PREDICTIONS_MAX_PAGE_SIZE))
    order_fields = _parse_prediction_accuracy_order_by(order_by) or [("predicted_date", "desc")]
    if "ticker" not in {field for field, _ in order_fields}:
        order_fields = [*order_fields, ("ticker", "asc")]

    with SessionLocal() as session:
        base_query = select(Ticker, PredictionAccuracy).join(
            PredictionAccuracy, PredictionAccuracy.ticker == Ticker.ticker
        )
        count_query = select(func.count(PredictionAccuracy.ticker)).select_from(PredictionAccuracy).join(
            Ticker, Ticker.ticker == PredictionAccuracy.ticker
        )
        if parsed_start_date is not None:
            base_query = base_query.where(PredictionAccuracy.predicted_date >= parsed_start_date)
            count_query = count_query.where(PredictionAccuracy.predicted_date >= parsed_start_date)
        if parsed_end_date is not None:
            base_query = base_query.where(PredictionAccuracy.predicted_date <= parsed_end_date)
            count_query = count_query.where(PredictionAccuracy.predicted_date <= parsed_end_date)
        if types:
            base_query = base_query.where(Ticker.type.in_(types))
            count_query = count_query.where(Ticker.type.in_(types))
        if selected_tickers:
            base_query = base_query.where(Ticker.ticker.in_(selected_tickers))
            count_query = count_query.where(Ticker.ticker.in_(selected_tickers))
        total = session.execute(count_query).scalar_one()

        order_clauses = []
        for field, direction in order_fields:
            column = PREDICTION_ACCURACY_ORDERABLE_FIELDS[field]
            order_clauses.append((column.desc() if direction == "desc" else column.asc()).nulls_last())
        query = base_query.order_by(*order_clauses).limit(page_size).offset((page - 1) * page_size)
        page_rows = session.execute(query).all()

        rows = [
            {
                "ticker": ticker.ticker,
                "name": ticker.name,
                "predicted_date": row.predicted_date.isoformat(),
                "actual_exit_price": row.actual_exit_price,
                "price_std": row.price_std,
                "history_days": row.history_days,
                "pass_threshold_std": row.pass_threshold_std,
                "computed_at": row.computed_at.isoformat(),
                **_prediction_accuracy_source_fields(row, "markov"),
                **_prediction_accuracy_source_fields(row, "mcmc"),
                **_prediction_accuracy_source_fields(row, "lstm_holdout"),
                **_prediction_accuracy_source_fields(row, "lstm_walkforward"),
            }
            for ticker, row in page_rows
        ]
        return {"rows": rows, "total": total, "page": page, "page_size": page_size}


# Copied verbatim from temp_queries/market_prediction_performance.sql (see
# market_predictions_performance_report) - not reassembled through the ORM the way
# every other report in this file is, so the query stays exactly what was authored and
# validated there. No trailing semicolon: it's used as a subquery below, which a
# trailing semicolon would break.
MARKET_PREDICTIONS_PERFORMANCE_SQL = """
SELECT
	a.ticker,
	a.name,
	a.market,
	a.locale,
	a.type,
	b.description,
	a.active,
	a.currency_name,
	a.primary_exchange,
	g.market_cap,
	h.average_volume,
	c.predicted_date,
	c.current_state as markov_current_state,
	c.predicted_state as markov_predicted_state,
	c.state_confidence as markov_state_confidence,
	c.expected_return as markov_expected_return,
	c.entry_price as markov_entry_price,
	c.exit_price as markov_exit_price,
	c.history_days as markov_history_days,
	c.exit_price_confidence as markov_exit_price_confidence,
	d.current_state as mcmc_current_state,
	d.state_confidence as mcmc_state_confidence,
	d.expected_return as mcmc_expected_return,
	d.entry_price as mcmc_entry_price,
	d.exit_price as mcmc_exit_price,
	d.history_days as mcmc_history_days,
	d.exit_price_confidence as mcmc_exit_price_confidence,
	e.open as actual_entry_price,
	e.close as actual_exit_price,
	e.pcnt_increase as actual_gain,
	(
		SELECT i.vwap
		FROM ohlc_bars i
		WHERE i.ticker = a.ticker
			AND date(i.timestamp) <= c.predicted_date
		ORDER BY i.timestamp DESC
		LIMIT 1
	) as vwap,
	CASE
		WHEN e.pcnt_increase IS NULL THEN NULL
		WHEN e.pcnt_increase <= 0  AND c.expected_return <= 0 THEN 'WON'
		WHEN e.pcnt_increase >= 0  AND c.expected_return >= 0 THEN 'WIN'
		ELSE 'FAILED'
	END as markov_result,
	CASE
		WHEN e.pcnt_increase IS NULL THEN NULL
		WHEN e.pcnt_increase <= 0  AND d.expected_return <= 0 THEN 'WON'
		WHEN e.pcnt_increase >= 0  AND d.expected_return >= 0 THEN 'WIN'
		ELSE 'FAILED'
	END as mcmc_result,
	f.mcmc_win_count,
	f.mcmc_win_rate,
	f.mcmc_predictions_count,
	f.markov_win_count,
	f.markov_win_rate,
	f.markov_predictions_count
FROM tickers a
JOIN ticker_types b
	on a.type = b.code
JOIN market_predictions c
	on a.ticker = c.ticker
LEFT JOIN market_predictions_mcmc d
	on c.ticker = d.ticker and c.predicted_date = d.predicted_date
LEFT OUTER JOIN ohlc_bars e
	on c.ticker = e.ticker and c.ticker = e.ticker and c.predicted_date = date(e.timestamp)
LEFT OUTER JOIN win_rates f
	on a.ticker = f.ticker
LEFT OUTER JOIN ticker_details g
	on a.ticker = g.ticker
LEFT OUTER JOIN (
	SELECT av.ticker, av.average_volume
	FROM average_volumes av
	JOIN (
		SELECT ticker, MAX(computed_at) as computed_at
		FROM average_volumes
		GROUP BY ticker
	) latest
		on av.ticker = latest.ticker and av.computed_at = latest.computed_at
) h
	on a.ticker = h.ticker
WHERE c.predicted_date BETWEEN :start_date AND :end_date
	AND (:types IS NULL OR a.type IN (SELECT value FROM json_each(:types)))
	AND (:tickers IS NULL OR a.ticker IN (SELECT value FROM json_each(:tickers)))
"""

MARKET_PREDICTIONS_PERFORMANCE_MAX_PAGE_SIZE = 1000
MARKET_PREDICTIONS_PERFORMANCE_DEFAULT_PAGE_SIZE = 500

# Every column MARKET_PREDICTIONS_PERFORMANCE_SQL's SELECT produces (by its alias) -
# what market_predictions_performance_report's order_by accepts. Doubles as the
# allowlist that keeps order_by from being a SQL injection vector into the raw ORDER BY
# clause built below, since (unlike every other report's order_by, which resolves a
# field name to a SQLAlchemy ColumnElement) there's no query-builder step here to
# escape a field name for us.
MARKET_PREDICTIONS_PERFORMANCE_ORDERABLE_FIELDS = frozenset(
    {
        "ticker",
        "name",
        "market",
        "locale",
        "type",
        "description",
        "active",
        "currency_name",
        "primary_exchange",
        "market_cap",
        "average_volume",
        "predicted_date",
        "markov_current_state",
        "markov_predicted_state",
        "markov_state_confidence",
        "markov_expected_return",
        "markov_entry_price",
        "markov_exit_price",
        "markov_history_days",
        "markov_exit_price_confidence",
        "mcmc_current_state",
        "mcmc_state_confidence",
        "mcmc_expected_return",
        "mcmc_entry_price",
        "mcmc_exit_price",
        "mcmc_history_days",
        "mcmc_exit_price_confidence",
        "actual_entry_price",
        "actual_exit_price",
        "actual_gain",
        "vwap",
        "markov_result",
        "mcmc_result",
        "mcmc_win_count",
        "mcmc_win_rate",
        "mcmc_predictions_count",
        "markov_win_count",
        "markov_win_rate",
        "markov_predictions_count",
    }
)


def _parse_market_predictions_performance_order_by(order_by: str) -> list[tuple[str, str]]:
    """Same shape as _parse_order_by, validated against
    MARKET_PREDICTIONS_PERFORMANCE_ORDERABLE_FIELDS instead."""
    fields: list[tuple[str, str]] = []
    for entry in split_csv(order_by) or []:
        field, _, direction = entry.partition(":")
        direction = direction.lower() or "asc"
        if field not in MARKET_PREDICTIONS_PERFORMANCE_ORDERABLE_FIELDS:
            raise HTTPException(422, f"Unknown order_by field: {field}")
        if direction not in ("asc", "desc"):
            raise HTTPException(422, f"Unknown order_by direction: {direction}")
        fields.append((field, direction))
    return fields


@app.get("/reports/market-predictions-performance")
def market_predictions_performance_report(
    start_date: str = "",
    end_date: str = "",
    ticker_types: str = "",
    tickers: str = "",
    page: int = 1,
    page_size: int = MARKET_PREDICTIONS_PERFORMANCE_DEFAULT_PAGE_SIZE,
    order_by: str = "",
    market_cap_op: str = "",
    market_cap_value: float | None = None,
    markov_exit_price_confidence_op: str = "",
    markov_exit_price_confidence_value: float | None = None,
    mcmc_exit_price_confidence_op: str = "",
    mcmc_exit_price_confidence_value: float | None = None,
    markov_win_rate_op: str = "",
    markov_win_rate_value: float | None = None,
    mcmc_win_rate_op: str = "",
    mcmc_win_rate_value: float | None = None,
) -> dict[str, Any]:
    """Backs the Analytics > Market Prediction Performance page's report grid: runs
    MARKET_PREDICTIONS_PERFORMANCE_SQL (temp_queries/market_prediction_performance.sql,
    unmodified apart from the outer market_cap filter below) directly rather than
    reassembling it through the ORM - one row per (ticker, predicted_date) with a
    market_predictions row in [start_date, end_date], each ticker's type description,
    its paired market_predictions_mcmc row (if any), the ohlc_bars row realized on
    predicted_date (if synced yet, which is what markov_result/mcmc_result score the
    prediction against), `vwap` (the latest ohlc_bars.vwap for the ticker on or before
    predicted_date, via a correlated subquery - not tied to the same bar as
    actual_entry_price/actual_exit_price), win_rates' ticker-level running tallies,
    ticker_details' market_cap, and average_volumes' most recently computed average_volume (same
    latest-row-per-ticker join as market_predictions_report's average_volume_by_ticker).

    `start_date`/`end_date` (ISO dates) each independently default to today (UTC) when
    omitted, so a bare request ("today's performance") is the common case, not an
    error, rather than requiring both or neither.

    `ticker_types`/`tickers` (comma-separated, e.g. "CS,ETF" / "AAPL,MSFT") bind to the
    SQL's :types/:tickers params as JSON arrays - what its own json_each(...) clauses
    expect - or NULL (matching every type/ticker) when left blank. Same
    comma-separated-list-or-everything convention every other report in this file uses,
    just serialized differently to match what the raw SQL parses.

    `market_cap_op`/`market_cap_value`, `markov_exit_price_confidence_op`/`_value`,
    `mcmc_exit_price_confidence_op`/`_value`, `markov_win_rate_op`/`_value`, and
    `mcmc_win_rate_op`/`_value` are the same shape as trading_symbols_report's - each
    pair both or neither, op restricted to NUMERIC_FILTER_OPS. Unlike start_date/
    end_date/types/tickers, these aren't baked into MARKET_PREDICTIONS_PERFORMANCE_SQL
    itself (that query stays exactly what was authored and validated in the .sql file);
    instead each active filter contributes a `column {op} :value` condition, ANDed
    together into a single WHERE on the outer wrapper query, op safe to interpolate
    directly since it's validated against the fixed NUMERIC_FILTER_OPS allowlist first -
    same reasoning as order_by's field/direction interpolation below. A row with a NULL
    value for a filtered column (e.g. market_cap with no ticker_details row, or
    mcmc_exit_price_confidence with no market_predictions_mcmc row) is excluded whenever
    that filter is active, same as any other NULL loses a numeric comparison.

    Paginated/ordered around the query rather than baked into it, so the query text
    itself stays untouched: `order_by` (see MARKET_PREDICTIONS_PERFORMANCE_ORDERABLE_FIELDS)
    and LIMIT/OFFSET wrap it in an outer `SELECT * FROM (<query>) ORDER BY ... LIMIT ...
    OFFSET ...`, and `total` comes from `SELECT COUNT(*) FROM (<query>)` over the same
    unmodified inner query. Defaults to ticker ascending, always appended as a
    tiebreaker - same reasoning as trading_symbols_report."""
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    parsed_start = start_date or today
    parsed_end = end_date or today
    try:
        dt.date.fromisoformat(parsed_start)
    except ValueError as exc:
        raise HTTPException(422, "start_date must be an ISO date, e.g. '2026-08-12'") from exc
    try:
        dt.date.fromisoformat(parsed_end)
    except ValueError as exc:
        raise HTTPException(422, "end_date must be an ISO date, e.g. '2026-08-12'") from exc
    if parsed_start > parsed_end:
        raise HTTPException(422, "start_date must not be after end_date")

    types = split_csv(ticker_types)
    selected_tickers = split_csv(tickers)
    page = max(1, page)
    page_size = max(1, min(page_size, MARKET_PREDICTIONS_PERFORMANCE_MAX_PAGE_SIZE))
    order_fields = _parse_market_predictions_performance_order_by(order_by) or [("ticker", "asc")]
    if "ticker" not in {field for field, _ in order_fields}:
        order_fields = [*order_fields, ("ticker", "asc")]
    order_clause = ", ".join(f"{field} {direction.upper()}" for field, direction in order_fields)

    _validate_numeric_filter_pair(market_cap_op, market_cap_value, "market_cap")
    _validate_numeric_filter_pair(
        markov_exit_price_confidence_op, markov_exit_price_confidence_value, "markov_exit_price_confidence"
    )
    _validate_numeric_filter_pair(
        mcmc_exit_price_confidence_op, mcmc_exit_price_confidence_value, "mcmc_exit_price_confidence"
    )
    _validate_numeric_filter_pair(markov_win_rate_op, markov_win_rate_value, "markov_win_rate")
    _validate_numeric_filter_pair(mcmc_win_rate_op, mcmc_win_rate_value, "mcmc_win_rate")
    numeric_filters = (
        (market_cap_op, "market_cap", "market_cap_value"),
        (markov_exit_price_confidence_op, "markov_exit_price_confidence", "markov_exit_price_confidence_value"),
        (mcmc_exit_price_confidence_op, "mcmc_exit_price_confidence", "mcmc_exit_price_confidence_value"),
        (markov_win_rate_op, "markov_win_rate", "markov_win_rate_value"),
        (mcmc_win_rate_op, "mcmc_win_rate", "mcmc_win_rate_value"),
    )
    filter_conditions = [f"{column} {op} :{param}" for op, column, param in numeric_filters if op]
    filter_clause = f"WHERE {' AND '.join(filter_conditions)}" if filter_conditions else ""

    params = {
        "start_date": parsed_start,
        "end_date": parsed_end,
        "types": json.dumps(types) if types else None,
        "tickers": json.dumps(selected_tickers) if selected_tickers else None,
        "market_cap_value": market_cap_value,
        "markov_exit_price_confidence_value": markov_exit_price_confidence_value,
        "mcmc_exit_price_confidence_value": mcmc_exit_price_confidence_value,
        "markov_win_rate_value": markov_win_rate_value,
        "mcmc_win_rate_value": mcmc_win_rate_value,
    }
    with SessionLocal() as session:
        total = session.execute(
            text(
                f"SELECT COUNT(*) FROM ({MARKET_PREDICTIONS_PERFORMANCE_SQL}) AS performance "
                f"{filter_clause}"
            ),
            params,
        ).scalar_one()
        page_rows = (
            session.execute(
                text(
                    f"SELECT * FROM ({MARKET_PREDICTIONS_PERFORMANCE_SQL}) AS performance "
                    f"{filter_clause} "
                    f"ORDER BY {order_clause} LIMIT :limit OFFSET :offset"
                ),
                {**params, "limit": page_size, "offset": (page - 1) * page_size},
            )
            .mappings()
            .all()
        )
        return {"rows": [dict(row) for row in page_rows], "total": total, "page": page, "page_size": page_size}


# Aggregated on top of the tickers_daily_market_direction view (see
# temp_queries/stock_price_direction.sql, the view's source query) - one row per ticker
# rather than that view's one row per (ticker, day), same "copy the validated .sql
# verbatim" convention as MARKET_PREDICTIONS_PERFORMANCE_SQL above. No trailing
# semicolon: it's used as a subquery below, which a trailing semicolon would break.
MARKET_DIRECTION_SQL = """
SELECT
    t.ticker,
    t.name,
    t.type,
    t.market,
    MAX(t.latest_price) AS latest_price,
    MAX(d.market_cap) AS market_cap,
    COUNT(*) AS total_records,
    100.0 * SUM(CASE WHEN t.market_type = 'strong down' THEN 1 ELSE 0 END) / COUNT(*) AS pcnt_strong_down,
    100.0 * SUM(CASE WHEN t.market_type = 'down' THEN 1 ELSE 0 END) / COUNT(*) AS pcnt_down,
    100.0 * SUM(CASE WHEN t.market_type = 'neutral' THEN 1 ELSE 0 END) / COUNT(*) AS pcnt_neutral,
    100.0 * SUM(CASE WHEN t.market_type = 'up' THEN 1 ELSE 0 END) / COUNT(*) AS pcnt_up,
    100.0 * SUM(CASE WHEN t.market_type = 'strong up' THEN 1 ELSE 0 END) / COUNT(*) AS pcnt_strong_up
FROM tickers_daily_market_direction t
LEFT JOIN ticker_details d ON d.ticker = t.ticker
WHERE date(t.timestamp) BETWEEN :start_date AND :end_date
    AND (:types IS NULL OR t.type IN (SELECT value FROM json_each(:types)))
    AND (:tickers IS NULL OR t.ticker IN (SELECT value FROM json_each(:tickers)))
GROUP BY t.ticker, t.name, t.type, t.market
"""

MARKET_DIRECTION_MAX_PAGE_SIZE = 1000
MARKET_DIRECTION_DEFAULT_PAGE_SIZE = 500

# Every column MARKET_DIRECTION_SQL's SELECT produces (by its alias) - what
# market_direction_report's order_by accepts, and (since there's no query-builder step
# here the way every ORM-assembled report has) the allowlist that keeps order_by from
# being a SQL injection vector into the raw ORDER BY clause built below - same reasoning
# as MARKET_PREDICTIONS_PERFORMANCE_ORDERABLE_FIELDS.
MARKET_DIRECTION_ORDERABLE_FIELDS = frozenset(
    {
        "ticker",
        "name",
        "type",
        "market",
        "latest_price",
        "market_cap",
        "total_records",
        "pcnt_strong_down",
        "pcnt_down",
        "pcnt_neutral",
        "pcnt_up",
        "pcnt_strong_up",
    }
)


def _parse_market_direction_order_by(order_by: str) -> list[tuple[str, str]]:
    """Same shape as _parse_market_predictions_performance_order_by, validated against
    MARKET_DIRECTION_ORDERABLE_FIELDS instead."""
    fields: list[tuple[str, str]] = []
    for entry in split_csv(order_by) or []:
        field, _, direction = entry.partition(":")
        direction = direction.lower() or "asc"
        if field not in MARKET_DIRECTION_ORDERABLE_FIELDS:
            raise HTTPException(422, f"Unknown order_by field: {field}")
        if direction not in ("asc", "desc"):
            raise HTTPException(422, f"Unknown order_by direction: {direction}")
        fields.append((field, direction))
    return fields


@app.get("/reports/market-direction")
def market_direction_report(
    start_date: str = "",
    end_date: str = "",
    ticker_types: str = "",
    tickers: str = "",
    market_cap_op: str = "",
    market_cap_value: float | None = None,
    page: int = 1,
    page_size: int = MARKET_DIRECTION_DEFAULT_PAGE_SIZE,
    order_by: str = "",
) -> dict[str, Any]:
    """Backs the Analytics > Market Direction report grid: one row per ticker showing
    what share of its daily bars in [start_date, end_date] fell into each
    tickers_daily_market_direction.market_type bucket (strong down/down/neutral/up/
    strong up - see temp_queries/stock_price_direction.sql for the open->close pcnt_diff
    thresholds that decide the bucket), alongside latest_price - the view's own
    "most recent ohlc_bars.close for this ticker" (not bounded by start_date/end_date;
    joined in the view from a latest-bar-per-ticker derived table rather than a
    per-row correlated subquery, which was a ~40s regression at this table's size - same
    "latest row per ticker" join shape as MARKET_PREDICTIONS_PERFORMANCE_SQL's
    average_volumes join) and market_cap (LEFT JOINed in from ticker_details, which is
    upserted wholesale per ticker with no history - same plain lookup as
    market_direction_report's market_cap_by_ticker elsewhere, just done as a join here
    since this whole report is raw SQL rather than ORM rows; NULL for a ticker with no
    ticker_details row, e.g. most ETFs). Both are wrapped in MAX() here only because they
    have to be some aggregate to sit alongside the pcnt_* SUMs under GROUP BY ticker -
    every row for a ticker carries the same value already, so MAX is just a pass-through.
    Each pcnt_* column is
    that bucket's share of the ticker's total bars in range, as a percentage (0-100) -
    the five sum to 100 for every returned ticker; a ticker with no bars in range is
    simply absent (a GROUP BY row can't have COUNT(*) = 0). total_records is that same
    COUNT(*) - the number of daily bars the pcnt_* columns were computed over - so a
    ticker with, say, 2 records in range can be sanity-checked against one with 200
    rather than trusting the percentages blind.

    `start_date`/`end_date` (ISO dates) each independently default to today (UTC) when
    omitted, same as market_predictions_performance_report - a bare request still runs,
    just against a single day.

    `ticker_types`/`tickers` (comma-separated, e.g. "CS,ETF" / "AAPL,MSFT") bind to
    MARKET_DIRECTION_SQL's :types/:tickers params as JSON arrays, or NULL (matching
    everything) when left blank - same convention as
    market_predictions_performance_report's.

    `market_cap_op`/`market_cap_value` (e.g. ">=" / 2000000000) are the same shape as
    trading_symbols_report's - both required together, op one of NUMERIC_FILTER_OPS -
    but applied as a WHERE on the outer paginated/counted query rather than inside
    MARKET_DIRECTION_SQL itself, since market_cap there is a MAX() sitting under
    GROUP BY ticker; filtering it pre-aggregation would need a HAVING clause instead,
    and doing it post-aggregation on the subquery's own market_cap column is simpler and
    identical in effect. A ticker with no ticker_details row (NULL market_cap) never
    matches any comparison, same as SQL NULL semantics everywhere else in this file.

    Paginated/ordered around the query rather than baked into it, same as
    market_predictions_performance_report: `order_by` (see
    MARKET_DIRECTION_ORDERABLE_FIELDS) and LIMIT/OFFSET wrap it in an outer `SELECT *
    FROM (<query>) ORDER BY ... LIMIT ... OFFSET ...`, and `total` comes from `SELECT
    COUNT(*) FROM (<query>)` over the same unmodified inner query. Defaults to ticker
    ascending, always appended as a tiebreaker."""
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    parsed_start = start_date or today
    parsed_end = end_date or today
    try:
        dt.date.fromisoformat(parsed_start)
    except ValueError as exc:
        raise HTTPException(422, "start_date must be an ISO date, e.g. '2026-08-12'") from exc
    try:
        dt.date.fromisoformat(parsed_end)
    except ValueError as exc:
        raise HTTPException(422, "end_date must be an ISO date, e.g. '2026-08-12'") from exc
    if parsed_start > parsed_end:
        raise HTTPException(422, "start_date must not be after end_date")
    _validate_numeric_filter_pair(market_cap_op, market_cap_value, "market_cap")

    types = split_csv(ticker_types)
    selected_tickers = split_csv(tickers)
    page = max(1, page)
    page_size = max(1, min(page_size, MARKET_DIRECTION_MAX_PAGE_SIZE))
    order_fields = _parse_market_direction_order_by(order_by) or [("ticker", "asc")]
    if "ticker" not in {field for field, _ in order_fields}:
        order_fields = [*order_fields, ("ticker", "asc")]
    order_clause = ", ".join(f"{field} {direction.upper()}" for field, direction in order_fields)
    # market_cap_op is validated above against NUMERIC_FILTER_OPS, a fixed allowlist of
    # comparison symbols - safe to interpolate directly, same reasoning as order_clause.
    market_cap_where = f" WHERE market_cap {market_cap_op} :market_cap_value" if market_cap_op else ""

    params = {
        "start_date": parsed_start,
        "end_date": parsed_end,
        "types": json.dumps(types) if types else None,
        "tickers": json.dumps(selected_tickers) if selected_tickers else None,
        "market_cap_value": market_cap_value,
    }
    with SessionLocal() as session:
        total = session.execute(
            text(f"SELECT COUNT(*) FROM ({MARKET_DIRECTION_SQL}) AS market_direction{market_cap_where}"),
            params,
        ).scalar_one()
        page_rows = (
            session.execute(
                text(
                    f"SELECT * FROM ({MARKET_DIRECTION_SQL}) AS market_direction{market_cap_where} "
                    f"ORDER BY {order_clause} LIMIT :limit OFFSET :offset"
                ),
                {**params, "limit": page_size, "offset": (page - 1) * page_size},
            )
            .mappings()
            .all()
        )
        return {"rows": [dict(row) for row in page_rows], "total": total, "page": page, "page_size": page_size}


# One row per (ticker, day) rather than MARKET_DIRECTION_SQL's one row per ticker -
# the tickers_daily_market_direction view's own rows for a single ticker, scoped by
# date range (see temp_queries/tickers_daily_market_direction.sql, copied verbatim,
# same convention as MARKET_DIRECTION_SQL above). No trailing semicolon, same reasoning.
MARKET_DIRECTION_DAILY_SQL = """
SELECT
    ticker,
    date(timestamp) AS date,
    Start_Price AS open_price,
    close_Price AS close_price,
    pcnt_diff,
    market_type
FROM tickers_daily_market_direction
WHERE ticker = :ticker
    AND date(timestamp) BETWEEN :start_date AND :end_date
"""

MARKET_DIRECTION_DAILY_ORDERABLE_FIELDS = frozenset(
    {"date", "open_price", "close_price", "pcnt_diff", "market_type"}
)


def _parse_market_direction_daily_order_by(order_by: str) -> list[tuple[str, str]]:
    """Same shape as _parse_market_direction_order_by, validated against
    MARKET_DIRECTION_DAILY_ORDERABLE_FIELDS instead."""
    fields: list[tuple[str, str]] = []
    for entry in split_csv(order_by) or []:
        field, _, direction = entry.partition(":")
        direction = direction.lower() or "asc"
        if field not in MARKET_DIRECTION_DAILY_ORDERABLE_FIELDS:
            raise HTTPException(422, f"Unknown order_by field: {field}")
        if direction not in ("asc", "desc"):
            raise HTTPException(422, f"Unknown order_by direction: {direction}")
        fields.append((field, direction))
    return fields


@app.get("/reports/market-direction/daily")
def market_direction_daily_report(
    ticker: str,
    start_date: str = "",
    end_date: str = "",
    order_by: str = "",
) -> dict[str, Any]:
    """Drill-down behind one market_direction_report row: every daily bar for `ticker`
    in [start_date, end_date] with that day's open->close pcnt_diff and which
    market_type bucket it fell into (see temp_queries/tickers_daily_market_direction.sql
    for the thresholds) - what market_direction_report's pcnt_* columns summarize for
    that ticker. Opened from the Market Direction report grid's row context menu.

    `start_date`/`end_date` (ISO dates) each independently default to today (UTC) when
    omitted, same as market_direction_report.

    Not paginated: scoped to a single ticker, so the row count is bounded by trading
    days in range rather than the whole tickers table. Defaults to date ascending,
    always appended as a tiebreaker (see MARKET_DIRECTION_DAILY_ORDERABLE_FIELDS)."""
    if not ticker:
        raise HTTPException(422, "ticker is required")
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    parsed_start = start_date or today
    parsed_end = end_date or today
    try:
        dt.date.fromisoformat(parsed_start)
    except ValueError as exc:
        raise HTTPException(422, "start_date must be an ISO date, e.g. '2026-08-12'") from exc
    try:
        dt.date.fromisoformat(parsed_end)
    except ValueError as exc:
        raise HTTPException(422, "end_date must be an ISO date, e.g. '2026-08-12'") from exc
    if parsed_start > parsed_end:
        raise HTTPException(422, "start_date must not be after end_date")

    order_fields = _parse_market_direction_daily_order_by(order_by) or [("date", "asc")]
    if "date" not in {field for field, _ in order_fields}:
        order_fields = [*order_fields, ("date", "asc")]
    order_clause = ", ".join(f"{field} {direction.upper()}" for field, direction in order_fields)

    params = {"ticker": ticker, "start_date": parsed_start, "end_date": parsed_end}
    with SessionLocal() as session:
        rows = (
            session.execute(
                text(
                    f"SELECT * FROM ({MARKET_DIRECTION_DAILY_SQL}) AS market_direction_daily "
                    f"ORDER BY {order_clause}"
                ),
                params,
            )
            .mappings()
            .all()
        )
        return {"rows": [dict(row) for row in rows]}


def _research_pick_to_dict(pick: ResearchPick, ticker: Ticker) -> dict[str, Any]:
    return {
        "ticker": pick.ticker,
        "name": ticker.name,
        "rank": pick.rank,
        "predicted_date": pick.predicted_date.isoformat(),
        "predicted_direction": pick.predicted_direction,
        "score": pick.score,
        "expected_return_score": pick.expected_return_score,
        "confidence_score": pick.confidence_score,
        "win_rate_score": pick.win_rate_score,
        "backtest_score": pick.backtest_score,
        "rsi_adjustment": pick.rsi_adjustment,
        "news_adjustment": pick.news_adjustment,
        "entry_price": pick.entry_price,
        "markov_predicted_state": pick.markov_predicted_state,
        "markov_expected_return": pick.markov_expected_return,
        "markov_state_confidence": pick.markov_state_confidence,
        "mcmc_predicted_state": pick.mcmc_predicted_state,
        "mcmc_expected_return": pick.mcmc_expected_return,
        "mcmc_state_confidence": pick.mcmc_state_confidence,
        "market_cap": pick.market_cap,
        "average_volume": pick.average_volume,
        "markov_win_rate": pick.markov_win_rate,
        "markov_predictions_count": pick.markov_predictions_count,
        "mcmc_win_rate": pick.mcmc_win_rate,
        "mcmc_predictions_count": pick.mcmc_predictions_count,
        "backtest_win_rate": pick.backtest_win_rate,
        "backtest_evaluated_count": pick.backtest_evaluated_count,
        "rsi_value": pick.rsi_value,
        "news_article_count": pick.news_article_count,
        "news_sentiment_lean": pick.news_sentiment_lean,
        "comment": pick.comment,
    }


@app.get("/reports/research-picks")
def research_picks_report(run_id: int | None = None) -> dict[str, Any]:
    """Backs the Research page: one run's ResearchPick rows (jobs/research_picks.py's
    compute_research_picks), ordered by rank. Omitting run_id resolves to the most
    recent completed research-picks JobRun that actually produced picks (a completed
    run that found zero qualifying candidates is skipped, not shown as an empty
    "latest" run), so a bare request always shows the latest real shortlist without the
    frontend having to look up a run id first - same "resolve at request time" pattern
    other reports in this file use for dates. Returns run_id/generated_at alongside
    rows so the page can show which run it's displaying even when run_id wasn't passed
    explicitly, and {"run_id": None, ...} with empty rows if no run has ever produced
    picks yet (e.g. predict-market-state hasn't run yet either)."""
    with SessionLocal() as session:
        resolved_run_id = run_id
        if resolved_run_id is None:
            resolved_run_id = session.execute(
                select(JobRun.id)
                .join(ResearchPick, ResearchPick.run_id == JobRun.id)
                .where(JobRun.job_name == RESEARCH_PICKS_JOB, JobRun.status == "completed")
                .order_by(JobRun.started_at.desc())
                .limit(1)
            ).scalar_one_or_none()
        if resolved_run_id is None:
            return {"run_id": None, "generated_at": None, "rows": []}
        run = session.get(JobRun, resolved_run_id)
        pick_rows = session.execute(
            select(ResearchPick, Ticker)
            .join(Ticker, Ticker.ticker == ResearchPick.ticker)
            .where(ResearchPick.run_id == resolved_run_id)
            .order_by(ResearchPick.rank.asc())
        ).all()
        return {
            "run_id": resolved_run_id,
            "generated_at": run.finished_at.isoformat() if run and run.finished_at else None,
            "rows": [_research_pick_to_dict(pick, ticker) for pick, ticker in pick_rows],
        }


@app.get("/jobs")
def list_jobs() -> list[dict]:
    with SessionLocal() as session:
        jobs = [_job_to_dict(session, name) for name in JOB_DEFINITIONS]
    # Sorted by sort_order (set by reorder_jobs below) rather than returned in
    # JOB_DEFINITIONS' fixed insertion order, so a drag-reordered Jobs page survives a
    # reload. A job never dragged has sort_order None and sorts after every explicitly
    # ordered job, in JOB_DEFINITIONS order among themselves - covers both a fresh
    # database (nothing ordered yet) and a job added to JOB_DEFINITIONS after the last
    # reorder (nothing to place it correctly by number, so it falls back to the end).
    default_index = {name: i for i, name in enumerate(JOB_DEFINITIONS)}
    jobs.sort(key=lambda job: (job["sort_order"] is None, job["sort_order"] or 0, default_index[job["name"]]))
    return jobs


@app.get("/jobs/{job_name}")
def get_job(job_name: str) -> dict:
    _require_job(job_name)
    with SessionLocal() as session:
        return _job_to_dict(session, job_name)


@app.put("/jobs/{job_name}/config")
def update_job_config(job_name: str, body: JobConfigIn) -> dict:
    _require_job(job_name)
    if body.schedule_interval_value < 1:
        raise HTTPException(status_code=400, detail="schedule_interval_value must be at least 1")
    if body.ticker_types and body.tickers:
        raise HTTPException(status_code=400, detail="specify ticker_types or tickers, not both")
    if body.start_time not in START_TIME_OPTIONS:
        raise HTTPException(status_code=400, detail="start_time must be a quarter-hour UTC time, e.g. '00:15'")
    if body.snapshot_types and (
        invalid := set(split_csv(body.snapshot_types) or []) - set(SNAPSHOT_TYPE_OPTIONS)
    ):
        raise HTTPException(status_code=400, detail=f"invalid snapshot_types: {', '.join(sorted(invalid))}")
    if body.bars_end_date_offset_days is not None and body.bars_end_date_offset_days < 0:
        raise HTTPException(status_code=400, detail="bars_end_date_offset_days must be at least 0")
    if body.average_volume_days_interval is not None and body.average_volume_days_interval < 1:
        raise HTTPException(status_code=400, detail="average_volume_days_interval must be at least 1")
    average_volume_start_date: dt.date | None = None
    if body.average_volume_start_date is not None:
        try:
            average_volume_start_date = dt.date.fromisoformat(body.average_volume_start_date)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="average_volume_start_date must be an ISO date, e.g. '2026-08-06'"
            ) from exc
    backtest_start_date: dt.date | None = None
    if body.backtest_start_date is not None:
        try:
            backtest_start_date = dt.date.fromisoformat(body.backtest_start_date)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="backtest_start_date must be an ISO date, e.g. '2026-08-06'"
            ) from exc
    backtest_end_date: dt.date | None = None
    if body.backtest_end_date is not None:
        try:
            backtest_end_date = dt.date.fromisoformat(body.backtest_end_date)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="backtest_end_date must be an ISO date, e.g. '2026-08-06'"
            ) from exc
    if backtest_start_date is not None and backtest_end_date is not None and backtest_start_date > backtest_end_date:
        raise HTTPException(status_code=400, detail="backtest_start_date must not be after backtest_end_date")
    prediction_start_date: dt.date | None = None
    if body.prediction_start_date is not None:
        try:
            prediction_start_date = dt.date.fromisoformat(body.prediction_start_date)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="prediction_start_date must be an ISO date, e.g. '2026-08-06'"
            ) from exc
    if body.mcmc_num_simulations is not None and body.mcmc_num_simulations < 1:
        raise HTTPException(status_code=400, detail="mcmc_num_simulations must be at least 1")
    ohlc_bars_start_date: dt.date | None = None
    if body.ohlc_bars_start_date is not None:
        try:
            ohlc_bars_start_date = dt.date.fromisoformat(body.ohlc_bars_start_date)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="ohlc_bars_start_date must be an ISO date, e.g. '2026-08-06'"
            ) from exc
    ohlc_bars_end_date: dt.date | None = None
    if body.ohlc_bars_end_date is not None:
        try:
            ohlc_bars_end_date = dt.date.fromisoformat(body.ohlc_bars_end_date)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="ohlc_bars_end_date must be an ISO date, e.g. '2026-08-06'"
            ) from exc
        if ohlc_bars_end_date > dt.datetime.now(dt.timezone.utc).date():
            raise HTTPException(status_code=400, detail="ohlc_bars_end_date cannot be greater than today")
    if (
        ohlc_bars_start_date is not None
        and ohlc_bars_end_date is not None
        and ohlc_bars_start_date > ohlc_bars_end_date
    ):
        raise HTTPException(status_code=400, detail="ohlc_bars_start_date must not be after ohlc_bars_end_date")
    if body.ohlc_bars_limit is not None and not (1 <= body.ohlc_bars_limit <= OHLC_BARS_MAX_LIMIT):
        raise HTTPException(status_code=400, detail=f"ohlc_bars_limit must be between 1 and {OHLC_BARS_MAX_LIMIT}")
    ohlc_update_start_date: dt.date | None = None
    if body.ohlc_update_start_date is not None:
        try:
            ohlc_update_start_date = dt.date.fromisoformat(body.ohlc_update_start_date)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="ohlc_update_start_date must be an ISO date, e.g. '2026-08-06'"
            ) from exc
    ohlc_update_end_date: dt.date | None = None
    if body.ohlc_update_end_date is not None:
        try:
            ohlc_update_end_date = dt.date.fromisoformat(body.ohlc_update_end_date)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="ohlc_update_end_date must be an ISO date, e.g. '2026-08-06'"
            ) from exc
    if (
        ohlc_update_start_date is not None
        and ohlc_update_end_date is not None
        and ohlc_update_start_date > ohlc_update_end_date
    ):
        raise HTTPException(status_code=400, detail="ohlc_update_start_date must not be after ohlc_update_end_date")
    lstm_train_start_date: dt.date | None = None
    if body.lstm_train_start_date is not None:
        try:
            lstm_train_start_date = dt.date.fromisoformat(body.lstm_train_start_date)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="lstm_train_start_date must be an ISO date, e.g. '2026-08-06'"
            ) from exc
    lstm_train_end_date: dt.date | None = None
    if body.lstm_train_end_date is not None:
        try:
            lstm_train_end_date = dt.date.fromisoformat(body.lstm_train_end_date)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="lstm_train_end_date must be an ISO date, e.g. '2026-08-06'"
            ) from exc
    if (
        lstm_train_start_date is not None
        and lstm_train_end_date is not None
        and lstm_train_start_date >= lstm_train_end_date
    ):
        raise HTTPException(status_code=400, detail="lstm_train_start_date must be before lstm_train_end_date")
    if body.lstm_epochs is not None and body.lstm_epochs < 1:
        raise HTTPException(status_code=400, detail="lstm_epochs must be at least 1")
    if body.lstm_lookback_days is not None and body.lstm_lookback_days < 2:
        raise HTTPException(status_code=400, detail="lstm_lookback_days must be at least 2")
    if body.lstm_learning_rate is not None and body.lstm_learning_rate <= 0:
        raise HTTPException(status_code=400, detail="lstm_learning_rate must be greater than 0")
    if body.lstm_batch_size is not None and body.lstm_batch_size < 1:
        raise HTTPException(status_code=400, detail="lstm_batch_size must be at least 1")
    if body.lstm_walkforward_num_folds is not None and body.lstm_walkforward_num_folds < 1:
        raise HTTPException(status_code=400, detail="lstm_walkforward_num_folds must be at least 1")
    if body.prediction_accuracy_pass_threshold_std is not None and body.prediction_accuracy_pass_threshold_std <= 0:
        raise HTTPException(status_code=400, detail="prediction_accuracy_pass_threshold_std must be greater than 0")

    with SessionLocal() as session:
        definition = JOB_DEFINITIONS[job_name]
        config = get_or_create_config(session, job_name)
        config.run_type = body.run_type
        config.schedule_interval_unit = body.schedule_interval_unit
        config.schedule_interval_value = body.schedule_interval_value
        config.start_time = body.start_time
        # ticker_types applies to jobs with has_ticker_type_filter (a single type
        # filter - see sync_tickers's ticker_type param) or has_ticker_selector (a
        # multi-select filter - see sync_bars/sync_snapshots's _resolve_tickers).
        # Dropped for a job like ticker-types sync that takes no run parameters at
        # all, even if the caller sent one.
        config.ticker_types = (
            body.ticker_types if (definition.has_ticker_type_filter or definition.has_ticker_selector) else None
        )
        # tickers is only meaningful alongside the multi-select ticker_types above.
        config.tickers = body.tickers if definition.has_ticker_selector else None
        if definition.has_bars_fields:
            config.multiplier = body.multiplier
            config.timespan = body.timespan
            config.backfill_days = body.backfill_days
            config.bars_end_date_offset_days = body.bars_end_date_offset_days
        config.snapshot_types = body.snapshot_types if definition.has_snapshot_type_filter else None
        if definition.has_average_volume_fields:
            config.average_volume_start_date = average_volume_start_date
            config.average_volume_days_interval = body.average_volume_days_interval
        else:
            config.average_volume_start_date = None
            config.average_volume_days_interval = None
        if definition.has_backtest_fields:
            config.backtest_start_date = backtest_start_date
            config.backtest_end_date = backtest_end_date
        else:
            config.backtest_start_date = None
            config.backtest_end_date = None
        if definition.has_prediction_start_date_field:
            config.prediction_start_date = prediction_start_date
        else:
            config.prediction_start_date = None
        config.predicted_date_offset_days = (
            body.predicted_date_offset_days if definition.has_predicted_date_offset_field else None
        )
        config.mcmc_num_simulations = body.mcmc_num_simulations if definition.has_monte_carlo_fields else None
        if definition.has_ohlc_bars_fields:
            config.ohlc_bars_start_date = ohlc_bars_start_date
            config.ohlc_bars_end_date = ohlc_bars_end_date
            config.ohlc_bars_limit = body.ohlc_bars_limit
        else:
            config.ohlc_bars_start_date = None
            config.ohlc_bars_end_date = None
            config.ohlc_bars_limit = None
        if definition.has_ohlc_update_fields:
            config.ohlc_update_start_date = ohlc_update_start_date
            config.ohlc_update_end_date = ohlc_update_end_date
        else:
            config.ohlc_update_start_date = None
            config.ohlc_update_end_date = None
        if definition.has_lstm_training_fields:
            config.lstm_train_start_date = lstm_train_start_date
            config.lstm_train_end_date = lstm_train_end_date
            config.lstm_epochs = body.lstm_epochs
            config.lstm_lookback_days = body.lstm_lookback_days
            config.lstm_learning_rate = body.lstm_learning_rate
            config.lstm_batch_size = body.lstm_batch_size
        else:
            config.lstm_train_start_date = None
            config.lstm_train_end_date = None
            config.lstm_epochs = None
            config.lstm_lookback_days = None
            config.lstm_learning_rate = None
            config.lstm_batch_size = None
        config.lstm_walkforward_num_folds = (
            body.lstm_walkforward_num_folds if definition.has_lstm_walkforward_fields else None
        )
        config.lstm_model_version_id = (
            body.lstm_model_version_id if definition.has_lstm_inference_fields else None
        )
        config.prediction_accuracy_pass_threshold_std = (
            body.prediction_accuracy_pass_threshold_std if definition.has_prediction_accuracy_fields else None
        )
        config.updated_at = dt.datetime.utcnow()
        session.commit()

        # No live scheduler to reschedule here anymore - job_runner.py's separate
        # process owns that. It notices this edit (updated_at changed) via its own
        # resync_schedules poll (jobs/engine.py) within that poll's interval instead of
        # instantly - see jobs/engine.py's module docstring for why polling is enough.
        result = _job_to_dict(session, job_name)

    return result


@app.post("/jobs/{job_name}/run")
def trigger_job(job_name: str) -> dict:
    """Doesn't run anything itself - job execution lives in job_runner.py's separate
    process. Setting run_requested_at here is the request; job_runner.py's
    poll_run_requests (jobs/engine.py) picks it up, typically within a couple of
    seconds, clears it, and starts the run.

    The UPDATE ... WHERE run_requested_at IS NULL is atomic at the DB row level, so two
    near-simultaneous clicks (e.g. two dashboard tabs) can't both queue a request - only
    one succeeds (rowcount 1), the other sees rowcount 0 and gets the same 409 a
    genuinely-already-running job would give."""
    _require_job(job_name)
    with SessionLocal() as session:
        if job_is_active(session, job_name):
            raise HTTPException(status_code=409, detail=f"{job_name} is already running")
        result = session.execute(
            update(JobConfig)
            .where(JobConfig.job_name == job_name, JobConfig.run_requested_at.is_(None))
            .values(run_requested_at=dt.datetime.utcnow())
        )
        session.commit()
        if result.rowcount == 0:
            raise HTTPException(status_code=409, detail=f"{job_name} is already running")
    return {"status": "started"}


def _require_running(job_name: str, session: Session) -> JobRun:
    """Shared guard for pause/resume/cancel below - all three only make sense against a
    run that's actually in flight. job_runner.py's poll_control_relay (jobs/engine.py)
    is what actually relays the flags this sets into the running job's JobControl."""
    _require_job(job_name)
    run = _in_progress_run(session, job_name)
    if run is None:
        raise HTTPException(status_code=409, detail=f"{job_name} is not running")
    return run


@app.post("/jobs/{job_name}/pause")
def pause_job(job_name: str) -> dict:
    with SessionLocal() as session:
        run = _require_running(job_name, session)
        run.pause_requested = True
        session.commit()
    return {"status": "pause-requested"}


@app.post("/jobs/{job_name}/resume")
def resume_job(job_name: str) -> dict:
    with SessionLocal() as session:
        run = _require_running(job_name, session)
        run.pause_requested = False
        session.commit()
    return {"status": "resumed"}


@app.post("/jobs/{job_name}/cancel")
def cancel_job(job_name: str) -> dict:
    with SessionLocal() as session:
        run = _require_running(job_name, session)
        run.cancel_requested = True
        session.commit()
    return {"status": "cancel-requested"}


@app.post("/jobs/{job_name}/hide")
def hide_job(job_name: str) -> dict:
    _require_job(job_name)
    with SessionLocal() as session:
        config = get_or_create_config(session, job_name)
        config.hidden = True
        session.commit()
        return _job_to_dict(session, job_name)


@app.post("/jobs/{job_name}/unhide")
def unhide_job(job_name: str) -> dict:
    _require_job(job_name)
    with SessionLocal() as session:
        config = get_or_create_config(session, job_name)
        config.hidden = False
        session.commit()
        return _job_to_dict(session, job_name)


@app.post("/jobs/reorder")
def reorder_jobs(body: JobReorderIn) -> list[dict]:
    """Persists the Jobs page's drag-and-drop card order: body.job_names is every job
    name in JOB_DEFINITIONS, in the new desired order (the frontend rebuilds this full
    list - including hidden jobs, kept at their existing relative positions - from just
    the visible cards' drop, so hidden jobs never lose their place). Stores each job's
    list index as its sort_order, which list_jobs above then sorts by."""
    if set(body.job_names) != set(JOB_DEFINITIONS):
        raise HTTPException(status_code=400, detail="job_names must be exactly the set of known jobs")
    with SessionLocal() as session:
        for index, job_name in enumerate(body.job_names):
            config = get_or_create_config(session, job_name)
            config.sort_order = index
        session.commit()
        jobs = [_job_to_dict(session, name) for name in JOB_DEFINITIONS]
    order = {name: index for index, name in enumerate(body.job_names)}
    jobs.sort(key=lambda job: order[job["name"]])
    return jobs


@app.post("/jobs/{job_name}/reset")
def reset_job(job_name: str) -> dict:
    """Empties the table(s) that job_name's data lives in - see _RESET_TABLES/
    _RESET_SYNC_STATE_KEYS above - and wipes its job_runs history so the dashboard's
    status badge/last-run panel goes back to "Never run" instead of continuing to show
    a stale "Succeeded" run whose summary (e.g. "36266 ticker(s) synced") no longer
    matches the now-empty table.

    Checks job_is_active (same check trigger_job uses) rather than acquiring a lock -
    there's no lock to acquire in this process anymore, execution lives in
    job_runner.py. This leaves a small window (check, then delete) where a run could
    start between the two - acceptable for a dashboard operated by a human, same
    "polling is enough" trade-off as everywhere else this split touches (see
    jobs/engine.py's module docstring)."""
    _require_job(job_name)
    with SessionLocal() as session:
        if job_is_active(session, job_name):
            raise HTTPException(status_code=409, detail=f"{job_name} is running - stop it before resetting")
        if job_name in INDICATOR_NAMES:
            session.execute(
                delete(TechnicalIndicator).where(TechnicalIndicator.indicator == INDICATOR_NAMES[job_name])
            )
        elif job_name in _LSTM_MODEL_VERSION_TRAINING_METHODS:
            session.execute(
                delete(LstmModelVersion).where(
                    LstmModelVersion.training_method == _LSTM_MODEL_VERSION_TRAINING_METHODS[job_name]
                )
            )
        elif job_name in LSTM_INFERENCE_TRAINING_METHODS:
            session.execute(
                delete(LstmInference).where(
                    LstmInference.training_method == LSTM_INFERENCE_TRAINING_METHODS[job_name]
                )
            )
        else:
            for model in _RESET_TABLES[job_name]:
                session.execute(delete(model))
        if (sync_key := _RESET_SYNC_STATE_KEYS.get(job_name)) is not None:
            session.execute(delete(SyncState).where(SyncState.job_name == sync_key))
            session.execute(delete(SyncProgress).where(SyncProgress.job_name == sync_key))
        session.execute(delete(JobRun).where(JobRun.job_name == job_name))
        session.commit()
        return _job_to_dict(session, job_name)


@app.get("/jobs/{job_name}/runs")
def job_runs(job_name: str, limit: int = 20) -> list[dict]:
    _require_job(job_name)
    with SessionLocal() as session:
        rows = (
            session.execute(
                select(JobRun).where(JobRun.job_name == job_name).order_by(JobRun.started_at.desc()).limit(limit)
            )
            .scalars()
            .all()
        )
        return [_run_to_dict(run) for run in rows]


# Row cap for /admin/query's SELECT results - guards the response/browser against a
# query like "SELECT * FROM ohlc_bars" with no LIMIT returning millions of rows. Purely
# a transport cap on what's shipped back, not a rewrite of the query itself - the SQL
# text sent by the caller runs against the database exactly as written.
ADHOC_QUERY_MAX_ROWS = 1000


@app.post("/admin/query")
def run_adhoc_query(body: AdhocQueryIn) -> dict:
    """Runs exactly one arbitrary SQL statement (SELECT or DML/DDL) against
    backend_v2.db for the SQL console page. Statement type isn't sniffed from the SQL
    text - CursorResult.returns_rows (set from the DBAPI cursor description after
    execute) tells us whether to fetch rows or report a rowcount, which also means
    this doesn't need to special-case WITH/PRAGMA/RETURNING or any other row-returning
    statement shape. sqlite3's DBAPI itself rejects more than one statement per
    execute() call, so no separate multi-statement guard is needed here."""
    sql = body.sql.strip()
    if not sql:
        raise HTTPException(status_code=400, detail="SQL statement is required.")
    with SessionLocal() as session:
        try:
            result = session.execute(text(sql))
            if result.returns_rows:
                columns = list(result.keys())
                fetched = result.fetchmany(ADHOC_QUERY_MAX_ROWS + 1)
                truncated = len(fetched) > ADHOC_QUERY_MAX_ROWS
                fetched = fetched[:ADHOC_QUERY_MAX_ROWS]
                session.commit()
                return {
                    "kind": "rows",
                    "columns": columns,
                    "rows": [dict(zip(columns, row, strict=True)) for row in fetched],
                    "row_count": len(fetched),
                    "truncated": truncated,
                }
            rowcount = result.rowcount
            session.commit()
            return {
                "kind": "statement",
                "rowcount": rowcount if rowcount is not None and rowcount >= 0 else None,
            }
        except SQLAlchemyError as exc:
            session.rollback()
            detail = str(getattr(exc, "orig", None) or exc)
            raise HTTPException(status_code=400, detail=detail) from exc
