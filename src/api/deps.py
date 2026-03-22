"""FastAPI dependency injection — DB session, job store."""
from __future__ import annotations

import threading
from collections.abc import Generator

from sqlalchemy.orm import Session

from src.api.schemas import AsyncJobOut
from src.db.database import _SessionLocal


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

_jobs: dict[str, AsyncJobOut] = {}
_jobs_lock = threading.Lock()


def get_job(job_id: str) -> AsyncJobOut | None:
    with _jobs_lock:
        return _jobs.get(job_id)


def set_job(job_id: str, job: AsyncJobOut) -> None:
    with _jobs_lock:
        _jobs[job_id] = job
