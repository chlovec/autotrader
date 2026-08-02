import os
import time

from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from db.models import Base, KillSwitch, ResearchSchedule

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./autotrader.db")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

_CREATE_ALL_RETRIES = 5
_CREATE_ALL_RETRY_DELAY_SECONDS = 0.5


def _create_all_with_retry() -> None:
    """bin/restart.sh launches the backend, trading loop, and a research run nearly
    simultaneously - against a completely fresh DB (e.g. right after a schema wipe),
    each independently calls init_db(), and create_all()'s check-then-create isn't
    protected by any cross-process lock, so two processes can both decide the same
    table is missing and race to CREATE TABLE it - whichever loses gets "table X
    already exists" and would otherwise crash on startup. Retrying converges once
    whichever process won has committed its CREATE TABLE."""
    for attempt in range(_CREATE_ALL_RETRIES):
        try:
            Base.metadata.create_all(engine)
            return
        except OperationalError as exc:
            if "already exists" not in str(exc) or attempt == _CREATE_ALL_RETRIES - 1:
                raise
            time.sleep(_CREATE_ALL_RETRY_DELAY_SECONDS)


def init_db() -> None:
    _create_all_with_retry()
    with SessionLocal() as session:
        if session.get(KillSwitch, 1) is None:
            session.add(KillSwitch(id=1, engaged=False))
        if session.get(ResearchSchedule, 1) is None:
            session.add(ResearchSchedule(id=1, enabled=True))
        session.commit()


def get_session() -> Session:
    return SessionLocal()
