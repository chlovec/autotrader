import datetime as dt
import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import backend.app.main as backend_main
import db.session as db_session
from db.models import ResearchResult


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


@pytest.fixture
def client(monkeypatch, tmp_path):
    # File-based sqlite, not :memory: - the backend's request thread and the background
    # task threadpool need to see the same database, which an in-memory sqlite DB doesn't
    # reliably guarantee across threads.
    test_engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db_session, "engine", test_engine)
    monkeypatch.setattr(db_session, "SessionLocal", sessionmaker(bind=test_engine, expire_on_commit=False))
    monkeypatch.setattr(backend_main, "_scheduler", _FakeScheduler())
    # Startup normally builds a real, long-lived broker connection and launches
    # network-touching background tasks (see backend/app/broker_stream.py) - none of
    # that is needed or safe to run repeatedly across many TestClient instantiations.
    monkeypatch.setattr(backend_main, "_start_broker_stream", lambda: None)
    with TestClient(backend_main.app) as test_client:
        yield test_client


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
