"""Thread-safe, bounded storage for process-local background jobs."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final

from src.api.schemas import AsyncJobOut

MAX_TERMINAL_JOBS: Final = 100
TERMINAL_JOB_TTL_SECONDS: Final = 24 * 60 * 60
_TERMINAL_STATUSES: Final = frozenset({"done", "error"})


@dataclass(frozen=True, slots=True)
class _JobEntry:
    job: AsyncJobOut
    updated_at: float


class InMemoryJobStore:
    """Retain active jobs and a bounded, expiring set of terminal results."""

    def __init__(
        self,
        *,
        max_terminal_jobs: int = MAX_TERMINAL_JOBS,
        terminal_ttl_seconds: float = TERMINAL_JOB_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_terminal_jobs < 1:
            raise ValueError("max_terminal_jobs must be positive")
        if not math.isfinite(terminal_ttl_seconds) or terminal_ttl_seconds <= 0:
            raise ValueError("terminal_ttl_seconds must be finite and positive")
        self._max_terminal_jobs = max_terminal_jobs
        self._terminal_ttl_seconds = terminal_ttl_seconds
        self._clock = clock
        self._entries: dict[str, _JobEntry] = {}
        self._lock = threading.Lock()

    def get(self, job_id: str) -> AsyncJobOut | None:
        with self._lock:
            entries = self._without_expired(self._entries, self._clock())
            self._entries = entries
            entry = entries.get(job_id)
            return entry.job.model_copy(deep=True) if entry is not None else None

    def set(self, job_id: str, job: AsyncJobOut) -> None:
        with self._lock:
            now = self._clock()
            entries = self._without_expired(self._entries, now)
            entries = {
                **entries,
                job_id: _JobEntry(job=job.model_copy(deep=True), updated_at=now),
            }
            self._entries = self._within_terminal_capacity(entries)

    def _without_expired(
        self, entries: Mapping[str, _JobEntry], now: float
    ) -> dict[str, _JobEntry]:
        return {
            job_id: entry
            for job_id, entry in entries.items()
            if entry.job.status not in _TERMINAL_STATUSES
            or now - entry.updated_at < self._terminal_ttl_seconds
        }

    def _within_terminal_capacity(
        self, entries: Mapping[str, _JobEntry]
    ) -> dict[str, _JobEntry]:
        terminal = sorted(
            (
                (job_id, entry)
                for job_id, entry in entries.items()
                if entry.job.status in _TERMINAL_STATUSES
            ),
            key=lambda item: item[1].updated_at,
        )
        excess = len(terminal) - self._max_terminal_jobs
        if excess <= 0:
            return dict(entries)
        evicted = frozenset(job_id for job_id, _ in terminal[:excess])
        return {job_id: entry for job_id, entry in entries.items() if job_id not in evicted}
