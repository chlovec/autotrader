import pytest
from sqlalchemy.exc import OperationalError

import db.session as db_session


def _fake_operational_error(message: str) -> OperationalError:
    return OperationalError("CREATE TABLE signals (...)", {}, Exception(message))


def test_create_all_with_retry_recovers_from_concurrent_table_creation(monkeypatch):
    """Simulates the bin/restart.sh race: another process's CREATE TABLE lands between
    this process's check and its own CREATE TABLE attempt."""
    calls = {"count": 0}

    def flaky_create_all(engine):
        calls["count"] += 1
        if calls["count"] == 1:
            raise _fake_operational_error("table signals already exists")

    monkeypatch.setattr(db_session.Base.metadata, "create_all", flaky_create_all)
    monkeypatch.setattr(db_session.time, "sleep", lambda _seconds: None)

    db_session._create_all_with_retry()

    assert calls["count"] == 2


def test_create_all_with_retry_reraises_unrelated_errors(monkeypatch):
    def broken_create_all(engine):
        raise _fake_operational_error("database is locked")

    monkeypatch.setattr(db_session.Base.metadata, "create_all", broken_create_all)
    monkeypatch.setattr(db_session.time, "sleep", lambda _seconds: None)

    with pytest.raises(OperationalError, match="database is locked"):
        db_session._create_all_with_retry()


def test_create_all_with_retry_gives_up_after_max_attempts(monkeypatch):
    calls = {"count": 0}

    def always_flaky_create_all(engine):
        calls["count"] += 1
        raise _fake_operational_error("table signals already exists")

    monkeypatch.setattr(db_session.Base.metadata, "create_all", always_flaky_create_all)
    monkeypatch.setattr(db_session.time, "sleep", lambda _seconds: None)

    with pytest.raises(OperationalError, match="already exists"):
        db_session._create_all_with_retry()

    assert calls["count"] == db_session._CREATE_ALL_RETRIES
