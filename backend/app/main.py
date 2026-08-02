import datetime as dt
import logging
import os
import threading
from dataclasses import asdict

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import BackgroundTasks, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func, select

from backend.app import broker_stream
from db.models import EquitySnapshot, KillSwitch, ResearchResult, ResearchSchedule, Signal, SystemEvent, Trade
from db.session import get_session, init_db
from engine.brokers import make_broker
from engine.config import load_config
from engine.research_runner import DEFAULT_TOP_N, DEFAULT_UNIVERSE, research_once

logger = logging.getLogger("autotrader.backend")

app = FastAPI(title="Autotrader API")

# No default on purpose: a wrong-but-plausible default (e.g. localhost:5173) is exactly
# how this broke before - the dashboard silently got "Backend unreachable" instead of a
# real error when it ran on a different origin. Every deployment (dev or prod) must set
# this explicitly. Comma-separated, e.g. "http://localhost:5173,http://localhost:5174".
_cors_origins_raw = os.environ.get("CORS_ORIGINS")
if not _cors_origins_raw:
    raise RuntimeError(
        "CORS_ORIGINS is not set. Set it to the dashboard's origin(s) in .env, e.g. "
        "CORS_ORIGINS=http://localhost:5173 - see .env.example."
    )
_cors_origins = [origin.strip() for origin in _cors_origins_raw.split(",") if origin.strip()]
if not _cors_origins:
    raise RuntimeError("CORS_ORIGINS is set but empty after parsing - check its value in .env.")

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Single-process, in-memory concurrency guard for research runs - correct for the one
# always-on backend instance this app is designed to run as (see README.md; no process
# manager, no multi-worker uvicorn). Running multiple backend workers would each start
# their own nightly job/lock and isn't supported.
_research_lock = threading.Lock()
_scheduler = BackgroundScheduler()


def _run_research() -> None:
    """Assumes the caller already holds _research_lock; releases it when done."""
    try:
        research_once(DEFAULT_UNIVERSE, DEFAULT_TOP_N)
    except Exception:
        logger.exception("research run failed")
    finally:
        _research_lock.release()


def _nightly_research_job() -> None:
    with get_session() as session:
        schedule = session.get(ResearchSchedule, 1)
        if schedule and not schedule.enabled:
            logger.info("nightly research disabled via dashboard toggle, skipping")
            return
    if not _research_lock.acquire(blocking=False):
        logger.info("research already running, skipping nightly trigger")
        return
    _run_research()


def _start_broker_stream() -> None:
    """Builds the one long-lived broker connection this process holds (needed for
    streaming - Alpaca's TradingStream, IBKR's IB(), and Questrade's cached token/account
    id all need to persist across requests, unlike the old per-request make_broker()
    calls) and launches its background stream/reconciliation tasks. Factored out as its
    own function so tests can monkeypatch it to a no-op instead of touching a real
    broker/network on every TestClient startup."""
    app.state.broker = make_broker(load_config(argv=[]))
    app.state.account_id = app.state.broker.get_account().account_id
    app.state.broker_stream_handle = broker_stream.start(app.state.broker)


async def _stop_broker_stream() -> None:
    handle = getattr(app.state, "broker_stream_handle", None)
    if handle is not None:
        await handle.stop()


@app.on_event("startup")
async def on_startup() -> None:
    init_db()
    _scheduler.add_job(_nightly_research_job, CronTrigger(hour=2, minute=0, timezone="America/New_York"), id="nightly-research")
    _scheduler.start()
    _start_broker_stream()


@app.on_event("shutdown")
async def on_shutdown() -> None:
    _scheduler.shutdown(wait=False)
    await _stop_broker_stream()


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/positions")
def positions() -> list[dict]:
    return [asdict(p) for p in app.state.broker.get_positions()]


@app.get("/equity")
def equity(limit: int = 500) -> list[dict]:
    with get_session() as session:
        rows = session.execute(
            select(EquitySnapshot)
            .where(EquitySnapshot.broker == app.state.broker.name, EquitySnapshot.account_id == app.state.account_id)
            .order_by(EquitySnapshot.timestamp.desc())
            .limit(limit)
        ).scalars().all()
        return [{"timestamp": r.timestamp.isoformat(), "equity": r.equity, "cash": r.cash} for r in reversed(rows)]


@app.get("/trades")
def trades(limit: int = 200) -> list[dict]:
    with get_session() as session:
        rows = session.execute(
            select(Trade)
            .where(Trade.broker == app.state.broker.name, Trade.account_id == app.state.account_id)
            .order_by(Trade.submitted_at.desc())
            .limit(limit)
        ).scalars().all()
        return [
            {
                "id": t.id,
                "broker_order_id": t.broker_order_id,
                "symbol": t.symbol,
                "side": t.side.value,
                "qty": t.qty,
                "fill_price": t.fill_price,
                "status": t.status,
                "submitted_at": t.submitted_at.isoformat(),
            }
            for t in rows
        ]


@app.get("/signals")
def signals(limit: int = 200) -> list[dict]:
    with get_session() as session:
        rows = session.execute(select(Signal).order_by(Signal.timestamp.desc()).limit(limit)).scalars().all()
        return [
            {"id": s.id, "symbol": s.symbol, "strategy_name": s.strategy_name, "action": s.action.value, "reason": s.reason, "timestamp": s.timestamp.isoformat()}
            for s in rows
        ]


@app.get("/events")
def events(limit: int = 100) -> list[dict]:
    with get_session() as session:
        rows = session.execute(select(SystemEvent).order_by(SystemEvent.timestamp.desc()).limit(limit)).scalars().all()
        return [{"id": e.id, "level": e.level.value, "source": e.source, "message": e.message, "timestamp": e.timestamp.isoformat()} for e in rows]


@app.get("/kill-switch")
def get_kill_switch() -> dict:
    with get_session() as session:
        switch = session.get(KillSwitch, 1)
        return {"engaged": switch.engaged, "reason": switch.reason}


@app.post("/kill-switch")
def set_kill_switch(engaged: bool, reason: str = "") -> dict:
    with get_session() as session:
        switch = session.get(KillSwitch, 1)
        switch.engaged = engaged
        switch.reason = reason
        session.commit()
        return {"engaged": switch.engaged, "reason": switch.reason}


@app.get("/research")
def research(limit: int = 100) -> list[dict]:
    with get_session() as session:
        latest_run = session.execute(select(func.max(ResearchResult.run_at))).scalar_one_or_none()
        if latest_run is None:
            return []
        rows = session.execute(
            select(ResearchResult)
            .where(ResearchResult.run_at == latest_run)
            .order_by(ResearchResult.combined_score.desc())
            .limit(limit)
        ).scalars().all()
        return [
            {
                "id": r.id,
                "run_at": r.run_at.isoformat(),
                "symbol": r.symbol,
                "technical_score": r.technical_score,
                "news_score": r.news_score,
                "combined_score": r.combined_score,
                "rationale": r.rationale,
                "selected": r.selected,
            }
            for r in rows
        ]


@app.get("/research/schedule")
def get_research_schedule() -> dict:
    with get_session() as session:
        schedule = session.get(ResearchSchedule, 1)
        return {"enabled": schedule.enabled}


@app.post("/research/schedule")
def set_research_schedule(enabled: bool) -> dict:
    with get_session() as session:
        schedule = session.get(ResearchSchedule, 1)
        schedule.enabled = enabled
        schedule.updated_at = dt.datetime.utcnow()
        session.commit()
        return {"enabled": schedule.enabled}


@app.get("/research/status")
def research_status() -> dict:
    return {"running": _research_lock.locked()}


@app.post("/research/run")
def trigger_research(background_tasks: BackgroundTasks) -> dict:
    if not _research_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="research already running")
    background_tasks.add_task(_run_research)
    return {"status": "started"}


@app.websocket("/ws")
async def ws_updates(websocket: WebSocket) -> None:
    await broker_stream.manager.connect(websocket)
    await broker_stream.send_snapshot(websocket, app.state.broker, app.state.account_id)
    try:
        while True:
            await websocket.receive_text()  # only used to detect client disconnect
    except WebSocketDisconnect:
        pass
    finally:
        broker_stream.manager.disconnect(websocket)
