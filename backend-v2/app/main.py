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
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.orm import Session

from db.models import (
    AverageVolume,
    Base,
    CurrentSnapshot,
    JobConfig,
    JobRun,
    MarketPrediction,
    MarketPrediction10Day,
    MarketPredictionBacktest,
    News,
    OhlcBar,
    SyncProgress,
    SyncState,
    TechnicalIndicator,
    Ticker,
    TickerDetail,
    TickerType,
    TopMarketMover,
    UnifiedSnapshot,
)
from db.session import SessionLocal, init_db
from jobs.config_store import get_or_create_config, interval_trigger, job_is_active, split_csv
from jobs.registry import (
    AVERAGE_VOLUME_JOB,
    BACKTEST_MARKET_STATE_JOB,
    BARS_JOB,
    DEFAULT_START_TIME,
    INDICATOR_NAMES,
    JOB_DEFINITIONS,
    MOVERS_JOB,
    NEWS_JOB,
    PREDICT_10_DAY_MARKET_STATE_JOB,
    PREDICT_MARKET_STATE_JOB,
    SNAPSHOT_TYPE_OPTIONS,
    SNAPSHOTS_JOB,
    START_TIME_OPTIONS,
    TICKER_DETAILS_JOB,
    TICKER_TYPES_JOB,
    TICKERS_JOB,
    UNIFIED_SNAPSHOT_JOB,
)
from jobs.sync_bars import DEFAULT_MULTIPLIER, DEFAULT_TIMESPAN

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
        "hidden": config.hidden,
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
    TICKER_TYPES_JOB: [TickerType],
    SNAPSHOTS_JOB: [CurrentSnapshot],
    TICKER_DETAILS_JOB: [TickerDetail],
    MOVERS_JOB: [TopMarketMover],
    UNIFIED_SNAPSHOT_JOB: [UnifiedSnapshot],
    NEWS_JOB: [News],
    AVERAGE_VOLUME_JOB: [AverageVolume],
    PREDICT_MARKET_STATE_JOB: [MarketPrediction],
    PREDICT_10_DAY_MARKET_STATE_JOB: [MarketPrediction10Day],
    BACKTEST_MARKET_STATE_JOB: [MarketPredictionBacktest],
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
    snapshot_types: str | None = None
    average_volume_start_date: str | None = None
    average_volume_days_interval: int | None = None
    backtest_start_date: str | None = None
    backtest_end_date: str | None = None
    prediction_start_date: str | None = None


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
    entry_price's count_query join already uses."""
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
        if entry_price_op:
            condition = _numeric_condition_clause(MarketPrediction.entry_price, entry_price_op, entry_price_value)
            base_query = base_query.where(condition)
            # count_query doesn't join market_predictions by default (nothing else it
            # counts needs to) - only add the join here, scoped to this branch, so the
            # common no-filter case stays as cheap as it was before this filter existed.
            count_query = (
                count_query.outerjoin(latest_predicted_date, latest_predicted_date.c.ticker == Ticker.ticker)
                .outerjoin(
                    MarketPrediction,
                    (MarketPrediction.ticker == latest_predicted_date.c.ticker)
                    & (MarketPrediction.predicted_date == latest_predicted_date.c.predicted_date),
                )
                .where(condition)
            )
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


@app.get("/jobs")
def list_jobs() -> list[dict]:
    with SessionLocal() as session:
        return [_job_to_dict(session, name) for name in JOB_DEFINITIONS]


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
