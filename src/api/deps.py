"""FastAPI dependency injection — DB session, job store."""

from __future__ import annotations

from collections.abc import Generator

from sqlalchemy.orm import Session

from src.api.jobs import InMemoryJobStore
from src.api.schemas import AsyncJobOut
from src.data.wiring import create_acquisition_service, get_acquisition_service
from src.db.database import _SessionLocal

__all__ = (
    "create_acquisition_service",
    "get_acquisition_service",
    "get_db",
    "get_job",
    "set_job",
)


def get_db() -> Generator[Session, None, None]:
    db = _SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# ---------------------------------------------------------------------------
# In-memory job store for background permutation tasks
# ---------------------------------------------------------------------------

_job_store = InMemoryJobStore()


def get_job(job_id: str) -> AsyncJobOut | None:
    return _job_store.get(job_id)


def set_job(job_id: str, job: AsyncJobOut) -> None:
    _job_store.set(job_id, job)
