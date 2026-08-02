import asyncio
import datetime as dt
import json
import logging
import os
import threading
from dataclasses import asdict, dataclass

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import BackgroundTasks, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func, select

from backend.app import broker_stream
from db.models import Account, EquitySnapshot, ResearchResult, ResearchSchedule, Signal, SystemEvent, Trade
from db.session import get_session, init_db
from engine.accounts import get_active_accounts, get_all_accounts, get_research_account, sync_accounts_from_env
from engine.brokers import make_broker
from engine.brokers.base import BrokerClient
from engine.config import load_account_credentials, load_config
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


@dataclass
class AccountRuntime:
    """The live pieces one active account gets: its own broker connection and its own
    broker_stream.AccountStream. Built on startup for every active account, and
    added/removed at runtime by the activate/deactivate endpoints below."""

    broker: BrokerClient
    stream: broker_stream.AccountStream


def _get_account_or_404(session, account_id: str) -> Account:
    account = session.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail=f"unknown account {account_id!r}")
    return account


def _start_account_stream(account: Account) -> None:
    broker = make_broker(account.id, load_account_credentials(account.id))
    app.state.accounts[account.id] = AccountRuntime(broker=broker, stream=broker_stream.start(account.id, broker))


async def _stop_account_stream(account_id: str) -> None:
    runtime = app.state.accounts.pop(account_id, None)
    if runtime is not None:
        await runtime.stream.stop()


def _start_broker_streams() -> None:
    """Builds one long-lived broker connection + broker_stream per active account (needed
    for streaming - Alpaca's TradingStream, IBKR's IB(), and Questrade's cached
    token/account id all need to persist across requests). Factored out as its own function
    so tests can monkeypatch it to a no-op instead of touching real brokers/network on
    every TestClient startup."""
    with get_session() as session:
        for account in get_active_accounts(session):
            _start_account_stream(account)


def _run_research() -> None:
    """Assumes the caller already holds _research_lock; releases it when done. Resolves
    the research broker as the first active account's - reuses that account's already-open
    broker connection if the backend has one running, rather than opening a second one."""
    try:
        with get_session() as session:
            account = get_research_account(session)
        if account is None:
            logger.warning("no active accounts configured, skipping research run")
            return
        runtime = app.state.accounts.get(account.id)
        broker = runtime.broker if runtime is not None else make_broker(account.id, load_account_credentials(account.id))
        research_once(DEFAULT_UNIVERSE, DEFAULT_TOP_N, broker)
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


@app.on_event("startup")
async def on_startup() -> None:
    init_db()
    app.state.accounts = {}
    config = load_config(argv=[])
    with get_session() as session:
        sync_accounts_from_env(session, config)
    _start_broker_streams()
    _scheduler.add_job(_nightly_research_job, CronTrigger(hour=2, minute=0, timezone="America/New_York"), id="nightly-research")
    _scheduler.start()


@app.on_event("shutdown")
async def on_shutdown() -> None:
    _scheduler.shutdown(wait=False)
    for account_id in list(app.state.accounts):
        await _stop_account_stream(account_id)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


def _account_summary_dict(account: Account, session, live_unrealized_pl: float | None) -> dict:
    latest = session.execute(
        select(EquitySnapshot).where(EquitySnapshot.account_id == account.id).order_by(EquitySnapshot.timestamp.desc())
    ).scalars().first()
    return {
        "id": account.id,
        "display_name": account.display_name,
        "broker": account.broker,
        "active": account.active,
        "strategy_name": account.strategy_name,
        "equity": latest.equity if latest else None,
        "cash": latest.cash if latest else None,
        "unrealized_pl": live_unrealized_pl,
    }


def _account_detail_dict(account: Account) -> dict:
    return {
        "id": account.id,
        "display_name": account.display_name,
        "broker": account.broker,
        "active": account.active,
        "strategy_name": account.strategy_name,
        "strategy_params": json.loads(account.strategy_params),
        "max_position_size_usd": account.max_position_size_usd,
        "max_daily_loss_usd": account.max_daily_loss_usd,
        "max_total_exposure_usd": account.max_total_exposure_usd,
        "kill_switch_engaged": account.kill_switch_engaged,
        "kill_switch_reason": account.kill_switch_reason,
    }


@app.get("/accounts")
async def list_accounts() -> list[dict]:
    with get_session() as session:
        accounts = get_all_accounts(session)
        result = []
        for account in accounts:
            live_pl = None
            runtime = app.state.accounts.get(account.id)
            if runtime is not None:
                try:
                    positions = await asyncio.to_thread(runtime.broker.get_positions)
                    live_pl = sum(p.unrealized_pl for p in positions)
                except Exception:
                    logger.exception("[%s] failed to fetch live positions for account summary", account.id)
            result.append(_account_summary_dict(account, session, live_pl))
        return result


@app.get("/accounts/{account_id}")
def get_account(account_id: str) -> dict:
    with get_session() as session:
        return _account_detail_dict(_get_account_or_404(session, account_id))


@app.post("/accounts/{account_id}/activate")
async def activate_account(account_id: str) -> dict:
    with get_session() as session:
        account = _get_account_or_404(session, account_id)
        account.active = True
        account.updated_at = dt.datetime.utcnow()
        session.commit()
        # expire_on_commit=False (see db/session.py) - account's attributes stay readable
        # after the session above closes, so this doesn't need a second fetch.
        if account_id not in app.state.accounts:
            _start_account_stream(account)
    return {"active": True}


@app.post("/accounts/{account_id}/deactivate")
async def deactivate_account(account_id: str) -> dict:
    with get_session() as session:
        account = _get_account_or_404(session, account_id)
        account.active = False
        account.updated_at = dt.datetime.utcnow()
        session.commit()
    await _stop_account_stream(account_id)
    return {"active": False}


@app.patch("/accounts/{account_id}/limits")
def set_account_limits(account_id: str, max_position_size_usd: float, max_daily_loss_usd: float, max_total_exposure_usd: float) -> dict:
    with get_session() as session:
        account = _get_account_or_404(session, account_id)
        account.max_position_size_usd = max_position_size_usd
        account.max_daily_loss_usd = max_daily_loss_usd
        account.max_total_exposure_usd = max_total_exposure_usd
        account.updated_at = dt.datetime.utcnow()
        session.commit()
        return {
            "max_position_size_usd": account.max_position_size_usd,
            "max_daily_loss_usd": account.max_daily_loss_usd,
            "max_total_exposure_usd": account.max_total_exposure_usd,
        }


@app.get("/accounts/{account_id}/kill-switch")
def get_account_kill_switch(account_id: str) -> dict:
    with get_session() as session:
        account = _get_account_or_404(session, account_id)
        return {"engaged": account.kill_switch_engaged, "reason": account.kill_switch_reason}


@app.post("/accounts/{account_id}/kill-switch")
def set_account_kill_switch(account_id: str, engaged: bool, reason: str = "") -> dict:
    with get_session() as session:
        account = _get_account_or_404(session, account_id)
        account.kill_switch_engaged = engaged
        account.kill_switch_reason = reason
        account.updated_at = dt.datetime.utcnow()
        session.commit()
        return {"engaged": account.kill_switch_engaged, "reason": account.kill_switch_reason}


@app.get("/accounts/{account_id}/positions")
async def account_positions(account_id: str) -> list[dict]:
    runtime = app.state.accounts.get(account_id)
    if runtime is None:
        with get_session() as session:
            _get_account_or_404(session, account_id)
        raise HTTPException(status_code=409, detail=f"account {account_id!r} is inactive - no live broker connection")
    positions = await asyncio.to_thread(runtime.broker.get_positions)
    return [asdict(p) for p in positions]


@app.get("/accounts/{account_id}/equity")
def account_equity(account_id: str, limit: int = 500) -> list[dict]:
    with get_session() as session:
        _get_account_or_404(session, account_id)
        rows = session.execute(
            select(EquitySnapshot).where(EquitySnapshot.account_id == account_id).order_by(EquitySnapshot.timestamp.desc()).limit(limit)
        ).scalars().all()
        return [{"timestamp": r.timestamp.isoformat(), "equity": r.equity, "cash": r.cash} for r in reversed(rows)]


@app.get("/accounts/{account_id}/trades")
def account_trades(account_id: str, limit: int = 200) -> list[dict]:
    with get_session() as session:
        _get_account_or_404(session, account_id)
        rows = session.execute(
            select(Trade).where(Trade.account_id == account_id).order_by(Trade.submitted_at.desc()).limit(limit)
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


@app.get("/accounts/{account_id}/signals")
def account_signals(account_id: str, limit: int = 200) -> list[dict]:
    with get_session() as session:
        _get_account_or_404(session, account_id)
        rows = session.execute(
            select(Signal).where(Signal.account_id == account_id).order_by(Signal.timestamp.desc()).limit(limit)
        ).scalars().all()
        return [
            {
                "id": s.id, "symbol": s.symbol, "strategy_name": s.strategy_name, "action": s.action.value,
                "reason": s.reason, "timestamp": s.timestamp.isoformat(),
            }
            for s in rows
        ]


@app.get("/events")
def events(limit: int = 100) -> list[dict]:
    with get_session() as session:
        rows = session.execute(select(SystemEvent).order_by(SystemEvent.timestamp.desc()).limit(limit)).scalars().all()
        return [
            {
                "id": e.id, "account_id": e.account_id, "level": e.level.value, "source": e.source,
                "message": e.message, "timestamp": e.timestamp.isoformat(),
            }
            for e in rows
        ]


@app.get("/research")
def research(limit: int = 100) -> list[dict]:
    with get_session() as session:
        latest_run = session.execute(select(func.max(ResearchResult.run_at))).scalar_one_or_none()
        if latest_run is None:
            return []
        rows = session.execute(
            select(ResearchResult).where(ResearchResult.run_at == latest_run).order_by(ResearchResult.combined_score.desc()).limit(limit)
        ).scalars().all()
        return [
            {
                "id": r.id, "run_at": r.run_at.isoformat(), "symbol": r.symbol, "technical_score": r.technical_score,
                "news_score": r.news_score, "combined_score": r.combined_score, "rationale": r.rationale, "selected": r.selected,
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


@app.websocket("/ws/accounts/{account_id}")
async def ws_account_updates(websocket: WebSocket, account_id: str) -> None:
    runtime = app.state.accounts.get(account_id)
    if runtime is None:
        await websocket.close(code=4404)
        return
    await broker_stream.manager.connect(account_id, websocket)
    await broker_stream.send_snapshot(websocket, account_id, runtime.broker)
    try:
        while True:
            await websocket.receive_text()  # only used to detect client disconnect
    except WebSocketDisconnect:
        pass
    finally:
        broker_stream.manager.disconnect(account_id, websocket)
