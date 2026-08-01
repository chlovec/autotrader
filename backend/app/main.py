import asyncio

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select

from db.models import EquitySnapshot, KillSwitch, Signal, SystemEvent, Trade
from db.session import get_session, init_db
from engine.clients import make_trading_client
from engine.config import load_config

app = FastAPI(title="Autotrader API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup() -> None:
    init_db()


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/positions")
def positions() -> list[dict]:
    config = load_config()
    client = make_trading_client(config)
    return [
        {"symbol": p.symbol, "qty": p.qty, "avg_entry_price": p.avg_entry_price, "market_value": p.market_value, "unrealized_pl": p.unrealized_pl}
        for p in client.get_all_positions()
    ]


@app.get("/equity")
def equity(limit: int = 500) -> list[dict]:
    with get_session() as session:
        rows = session.execute(select(EquitySnapshot).order_by(EquitySnapshot.timestamp.desc()).limit(limit)).scalars().all()
        return [{"timestamp": r.timestamp.isoformat(), "equity": r.equity, "cash": r.cash} for r in reversed(rows)]


@app.get("/trades")
def trades(limit: int = 200) -> list[dict]:
    with get_session() as session:
        rows = session.execute(select(Trade).order_by(Trade.submitted_at.desc()).limit(limit)).scalars().all()
        return [
            {"symbol": t.symbol, "side": t.side.value, "qty": t.qty, "fill_price": t.fill_price, "status": t.status, "submitted_at": t.submitted_at.isoformat()}
            for t in rows
        ]


@app.get("/signals")
def signals(limit: int = 200) -> list[dict]:
    with get_session() as session:
        rows = session.execute(select(Signal).order_by(Signal.timestamp.desc()).limit(limit)).scalars().all()
        return [{"symbol": s.symbol, "strategy_name": s.strategy_name, "action": s.action.value, "reason": s.reason, "timestamp": s.timestamp.isoformat()} for s in rows]


@app.get("/events")
def events(limit: int = 100) -> list[dict]:
    with get_session() as session:
        rows = session.execute(select(SystemEvent).order_by(SystemEvent.timestamp.desc()).limit(limit)).scalars().all()
        return [{"level": e.level.value, "source": e.source, "message": e.message, "timestamp": e.timestamp.isoformat()} for e in rows]


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


@app.websocket("/ws")
async def ws_updates(websocket: WebSocket) -> None:
    await websocket.accept()
    try:
        while True:
            with get_session() as session:
                latest = session.execute(select(EquitySnapshot).order_by(EquitySnapshot.timestamp.desc())).scalars().first()
                if latest:
                    await websocket.send_json({"type": "equity", "equity": latest.equity, "timestamp": latest.timestamp.isoformat()})
            await asyncio.sleep(5)
    except WebSocketDisconnect:
        pass
