import os

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from db.models import Base, KillSwitch

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./autotrader.db")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db() -> None:
    Base.metadata.create_all(engine)
    with SessionLocal() as session:
        if session.get(KillSwitch, 1) is None:
            session.add(KillSwitch(id=1, engaged=False))
            session.commit()


def get_session() -> Session:
    return SessionLocal()
