"""Database setup — SQLAlchemy 2.0, SQLite dev / PostgreSQL prod."""
from __future__ import annotations

import os
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///data/backtester.db")


def create_db_engine(database_url: str) -> Engine:
    """Create an engine with connection options appropriate to its dialect."""
    kwargs: dict[str, Any] = {"echo": False}
    if database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}

    engine = create_engine(database_url, **kwargs)
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _enable_foreign_keys(dbapi_connection: Any, _: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


_engine = create_db_engine(DATABASE_URL)

_SessionLocal = sessionmaker(bind=_engine, autocommit=False, autoflush=False)


def init_db() -> None:
    """Verify that explicit migrations have brought the schema to head."""
    from src.db.migrate import verify_schema_current

    verify_schema_current(get_engine())


def get_session() -> Session:
    return _SessionLocal()


def get_engine() -> Engine:
    """Return the process-wide SQLAlchemy engine."""
    return _engine


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
