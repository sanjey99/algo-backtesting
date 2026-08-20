"""Lifecycle tests for the process-local API job store."""

from __future__ import annotations

from src.api.jobs import InMemoryJobStore
from src.api.schemas import AsyncJobOut, PermutationOut


def _completed_job(job_id: str) -> AsyncJobOut:
    return AsyncJobOut(
        job_id=job_id,
        status="done",
        result=PermutationOut(
            actual_metric=1.0,
            permuted_metrics=[0.1, 0.2],
            p_value=1 / 3,
            is_significant=False,
            percentile=66.7,
        ),
    )


def test_terminal_jobs_expire_without_poll_extending_retention() -> None:
    now = [100.0]
    store = InMemoryJobStore(
        max_terminal_jobs=10,
        terminal_ttl_seconds=60.0,
        clock=lambda: now[0],
    )
    store.set("complete", _completed_job("complete"))

    now[0] = 159.0
    assert store.get("complete") is not None
    now[0] = 160.0
    assert store.get("complete") is None


def test_terminal_capacity_evicts_oldest_result_but_preserves_active_jobs() -> None:
    now = [100.0]
    store = InMemoryJobStore(
        max_terminal_jobs=2,
        terminal_ttl_seconds=3_600.0,
        clock=lambda: now[0],
    )
    store.set("running", AsyncJobOut(job_id="running", status="running"))
    store.set("oldest", _completed_job("oldest"))
    now[0] += 1.0
    store.set("middle", AsyncJobOut(job_id="middle", status="error", error="failed"))
    now[0] += 1.0
    store.set("newest", _completed_job("newest"))

    assert store.get("running") is not None
    assert store.get("oldest") is None
    assert store.get("middle") is not None
    assert store.get("newest") is not None


def test_store_isolated_from_mutation_of_input_and_returned_jobs() -> None:
    store = InMemoryJobStore(max_terminal_jobs=10, terminal_ttl_seconds=60.0)
    supplied = _completed_job("complete")
    store.set("complete", supplied)

    supplied.status = "error"
    retrieved = store.get("complete")
    assert retrieved is not None
    retrieved.status = "error"

    stored = store.get("complete")
    assert stored is not None
    assert stored.status == "done"
    assert stored.result is not None
    assert stored.result.permuted_metrics == [0.1, 0.2]
