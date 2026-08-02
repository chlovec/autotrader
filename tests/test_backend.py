import datetime as dt
import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import backend.app.main as backend_main
import db.session as db_session
from backend.app.main import AccountRuntime
from db.models import Account, EquitySnapshot, ResearchResult, SystemEvent
from engine.brokers.base import AccountSnapshot, PositionSnapshot


class _FakeScheduler:
    """Stands in for backend_main._scheduler in tests - a real BackgroundScheduler can't
    be cleanly started/shut down/restarted across many TestClient instantiations, and
    tests have no need for the nightly cron job to actually be registered."""

    def add_job(self, *args, **kwargs) -> None:
        pass

    def start(self) -> None:
        pass

    def shutdown(self, wait: bool = True) -> None:
        pass


class FakeBroker:
    name = "fake"

    def __init__(self, positions: list[PositionSnapshot] | None = None):
        self._positions = positions or []

    def get_positions(self) -> list[PositionSnapshot]:
        return self._positions

    def get_account(self) -> AccountSnapshot:
        return AccountSnapshot(equity=1000.0, cash=500.0, buying_power=2000.0, account_id="broker-acct-1")


@pytest.fixture
def client(monkeypatch, tmp_path):
    # File-based sqlite, not :memory: - the backend's request thread and the background
    # task threadpool need to see the same database, which an in-memory sqlite DB doesn't
    # reliably guarantee across threads.
    test_engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db_session, "engine", test_engine)
    monkeypatch.setattr(db_session, "SessionLocal", sessionmaker(bind=test_engine, expire_on_commit=False))
    monkeypatch.setattr(backend_main, "_scheduler", _FakeScheduler())
    # ACCOUNT_IDS would otherwise leak in from a real .env in the project root - tests
    # seed accounts directly instead of relying on env sync.
    monkeypatch.delenv("ACCOUNT_IDS", raising=False)
    # Startup normally builds real, long-lived broker connections and launches
    # network-touching background tasks per active account (see
    # backend/app/broker_stream.py) - none of that is needed or safe to run repeatedly
    # across many TestClient instantiations.
    monkeypatch.setattr(backend_main, "_start_broker_streams", lambda: None)
    with TestClient(backend_main.app) as test_client:
        yield test_client


def _add_account(account_id: str = "acct-1", **overrides) -> None:
    defaults = dict(
        id=account_id, broker="alpaca", display_name="Test Account", active=True,
        strategy_name="rebalancing_portfolio", strategy_params='{"target_weights": {"SPY": 1.0}}',
        max_position_size_usd=1000.0, max_daily_loss_usd=200.0, max_total_exposure_usd=0.0,
    )
    defaults.update(overrides)
    with db_session.get_session() as session:
        session.add(Account(**defaults))
        session.commit()


def test_research_schedule_defaults_enabled(client):
    assert client.get("/research/schedule").json() == {"enabled": True}


def test_research_schedule_round_trip(client):
    assert client.post("/research/schedule", params={"enabled": False}).json() == {"enabled": False}
    assert client.get("/research/schedule").json() == {"enabled": False}


def test_research_empty_with_no_runs(client):
    assert client.get("/research").json() == []


def test_research_returns_latest_run_ordered_by_score(client):
    with db_session.get_session() as session:
        run_at = dt.datetime(2026, 8, 1)
        session.add_all(
            [
                ResearchResult(run_at=run_at, symbol="LOW", technical_score=10, news_score=10, combined_score=20, selected=False),
                ResearchResult(run_at=run_at, symbol="HIGH", technical_score=90, news_score=90, combined_score=90, selected=True),
            ]
        )
        session.commit()

    rows = client.get("/research").json()
    assert [r["symbol"] for r in rows] == ["HIGH", "LOW"]
    assert rows[0]["selected"] is True
    assert rows[1]["selected"] is False


def test_research_status_reflects_lock(client):
    assert client.get("/research/status").json() == {"running": False}

    backend_main._research_lock.acquire()
    try:
        assert client.get("/research/status").json() == {"running": True}
    finally:
        backend_main._research_lock.release()


def test_trigger_research_returns_409_when_already_running(client):
    backend_main._research_lock.acquire()
    try:
        response = client.post("/research/run")
        assert response.status_code == 409
    finally:
        backend_main._research_lock.release()


def test_trigger_research_runs_in_background_and_releases_lock(client, monkeypatch):
    completed = threading.Event()

    def fake_run_research() -> None:
        completed.set()
        backend_main._research_lock.release()

    monkeypatch.setattr(backend_main, "_run_research", fake_run_research)

    response = client.post("/research/run")
    assert response.status_code == 200
    assert response.json() == {"status": "started"}
    assert completed.wait(timeout=2)
    assert not backend_main._research_lock.locked()


def test_list_accounts_empty_when_none_configured(client):
    assert client.get("/accounts").json() == []


def test_list_accounts_includes_inactive_and_active(client):
    _add_account("acct-1", active=True)
    _add_account("acct-2", active=False)

    rows = client.get("/accounts").json()
    assert {r["id"] for r in rows} == {"acct-1", "acct-2"}
    assert next(r for r in rows if r["id"] == "acct-1")["active"] is True
    assert next(r for r in rows if r["id"] == "acct-2")["active"] is False


def test_list_accounts_includes_live_unrealized_pl_for_active_accounts(client):
    _add_account("acct-1", active=True)
    backend_main.app.state.accounts["acct-1"] = AccountRuntime(
        broker=FakeBroker([PositionSnapshot(symbol="SPY", qty=1, avg_entry_price=100, market_value=110, unrealized_pl=10.0)]),
        stream=_NoOpStream(),
    )

    row = next(r for r in client.get("/accounts").json() if r["id"] == "acct-1")
    assert row["unrealized_pl"] == 10.0


def test_list_accounts_includes_latest_equity_snapshot(client):
    _add_account("acct-1")
    with db_session.get_session() as session:
        session.add(EquitySnapshot(account_id="acct-1", broker="alpaca", broker_account_id="b1", equity=5000.0, cash=1000.0, buying_power=2000.0))
        session.commit()

    row = next(r for r in client.get("/accounts").json() if r["id"] == "acct-1")
    assert row["equity"] == 5000.0
    assert row["cash"] == 1000.0


def test_get_account_404_for_unknown_id(client):
    assert client.get("/accounts/nope").status_code == 404


def test_get_account_detail(client):
    _add_account("acct-1", display_name="My Account", strategy_name="ma_crossover")

    detail = client.get("/accounts/acct-1").json()
    assert detail["display_name"] == "My Account"
    assert detail["strategy_name"] == "ma_crossover"
    assert detail["max_position_size_usd"] == 1000.0


def test_deactivate_account_flips_active_and_removes_runtime(client):
    _add_account("acct-1", active=True)
    backend_main.app.state.accounts["acct-1"] = AccountRuntime(broker=FakeBroker(), stream=_NoOpStream())

    response = client.post("/accounts/acct-1/deactivate")
    assert response.json() == {"active": False}
    assert client.get("/accounts/acct-1").json()["active"] is False
    assert "acct-1" not in backend_main.app.state.accounts


def test_activate_account_flips_active(client, monkeypatch):
    _add_account("acct-1", active=False)
    monkeypatch.setattr(backend_main, "_start_account_stream", lambda account: None)

    response = client.post("/accounts/acct-1/activate")
    assert response.json() == {"active": True}
    assert client.get("/accounts/acct-1").json()["active"] is True


def test_set_account_limits(client):
    _add_account("acct-1")

    response = client.patch(
        "/accounts/acct-1/limits",
        params={"max_position_size_usd": 2000.0, "max_daily_loss_usd": 300.0, "max_total_exposure_usd": 5000.0},
    )
    assert response.json() == {"max_position_size_usd": 2000.0, "max_daily_loss_usd": 300.0, "max_total_exposure_usd": 5000.0}

    detail = client.get("/accounts/acct-1").json()
    assert detail["max_position_size_usd"] == 2000.0
    assert detail["max_daily_loss_usd"] == 300.0
    assert detail["max_total_exposure_usd"] == 5000.0


def test_kill_switch_round_trip(client):
    _add_account("acct-1")

    assert client.get("/accounts/acct-1/kill-switch").json() == {"engaged": False, "reason": ""}

    response = client.post("/accounts/acct-1/kill-switch", params={"engaged": True, "reason": "manual stop"})
    assert response.json() == {"engaged": True, "reason": "manual stop"}
    assert client.get("/accounts/acct-1/kill-switch").json() == {"engaged": True, "reason": "manual stop"}


def _add_event(**overrides) -> int:
    defaults = dict(level="info", source="test", message="something happened", account_id=None)
    defaults.update(overrides)
    with db_session.get_session() as session:
        event = SystemEvent(**defaults)
        session.add(event)
        session.commit()
        return event.id


def test_events_empty_with_no_events(client):
    assert client.get("/events").json() == []


def test_events_returns_newest_first(client):
    _add_event(message="first")
    _add_event(message="second")
    rows = client.get("/events").json()
    assert [r["message"] for r in rows] == ["second", "first"]


def test_clear_event_removes_just_that_one(client):
    keep_id = _add_event(message="keep")
    clear_id = _add_event(message="clear me")

    response = client.delete(f"/events/{clear_id}")
    assert response.json() == {"cleared": 1}

    rows = client.get("/events").json()
    assert [r["id"] for r in rows] == [keep_id]


def test_clear_event_404_for_unknown_id(client):
    assert client.delete("/events/999").status_code == 404


def test_clear_events_removes_everything(client):
    _add_event(message="one")
    _add_event(message="two")

    response = client.delete("/events")
    assert response.json() == {"cleared": 2}
    assert client.get("/events").json() == []


def test_clear_events_scoped_to_account(client):
    _add_account("acct-1")
    _add_account("acct-2")
    _add_event(account_id="acct-1", message="acct-1 event")
    _add_event(account_id="acct-2", message="acct-2 event")
    _add_event(account_id=None, message="general event")

    response = client.delete("/events", params={"account_id": "acct-1"})
    assert response.json() == {"cleared": 1}

    rows = client.get("/events").json()
    assert {r["message"] for r in rows} == {"acct-2 event", "general event"}


def test_clear_events_scoped_to_unassigned(client):
    _add_account("acct-1")
    _add_event(account_id="acct-1", message="acct-1 event")
    _add_event(account_id=None, message="general event")

    response = client.delete("/events", params={"unassigned": True})
    assert response.json() == {"cleared": 1}

    rows = client.get("/events").json()
    assert [r["message"] for r in rows] == ["acct-1 event"]


def test_positions_returns_409_for_inactive_account(client):
    _add_account("acct-1", active=False)
    response = client.get("/accounts/acct-1/positions")
    assert response.status_code == 409


def test_positions_returns_404_for_unknown_account(client):
    assert client.get("/accounts/nope/positions").status_code == 404


def test_positions_returns_live_positions_for_active_account(client):
    _add_account("acct-1", active=True)
    backend_main.app.state.accounts["acct-1"] = AccountRuntime(
        broker=FakeBroker([PositionSnapshot(symbol="SPY", qty=1, avg_entry_price=100, market_value=110, unrealized_pl=10.0)]),
        stream=_NoOpStream(),
    )

    rows = client.get("/accounts/acct-1/positions").json()
    assert rows == [{"symbol": "SPY", "qty": 1, "avg_entry_price": 100, "market_value": 110, "unrealized_pl": 10.0}]


class _NoOpStream:
    async def stop(self) -> None:
        pass
