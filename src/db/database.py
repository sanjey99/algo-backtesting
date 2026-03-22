"""Database setup — SQLAlchemy 2.0, SQLite dev / PostgreSQL prod."""
from __future__ import annotations

import os
from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.db.tables import Base

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///data/backtester.db")

_engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
    echo=False,
)

_SessionLocal = sessionmaker(bind=_engine, autocommit=False, autoflush=False)


def init_db() -> None:
    """Create all tables if they don't exist."""
    import pathlib

    if DATABASE_URL.startswith("sqlite:///"):
        db_path = DATABASE_URL[len("sqlite:///"):]
        pathlib.Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(bind=_engine)


def get_session() -> Session:
    return _SessionLocal()


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
