"""Offline tests for closed cloud-run terminal finalization."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from types import ModuleType

import pytest

from src.cloud.contracts import (
    FailureCode,
    RunRecord,
    RunStatus,
    Visibility,
    canonical_json_bytes,
    sha256_hex,
)
from src.cloud.storage import ObjectSizeLimitError, StateTransitionError
from tests.cloud.fakes import FakeObjectStore, FakeRunRepository

NOW = datetime(2024, 3, 29, 12, 0, tzinfo=UTC)
RUN_ID = "123e4567-e89b-12d3-a456-426614174000"
REQUIRED_BODIES = {
    "run-spec.json": b'{"schema_version":"1"}',
    "result.json": canonical_json_bytes({"run_id": RUN_ID, "schema_version": "1"}),
    "trades.parquet": b"parquet-trades",
    "equity-curve.parquet": b"parquet-equity",
    "report.html": b"<html><body>report</body></html>",
}


def fixed_clock() -> datetime:
    return NOW


def record(*, status: RunStatus = RunStatus.RUNNING) -> RunRecord:
    return RunRecord(
        run_id=RUN_ID,
        status=status,
        visibility=Visibility.PRIVATE,
        dataset_key="datasets/v1/acquisition-1/spy.parquet",
        dataset_sha256="a" * 64,
        run_spec_key=f"runs/v1/{RUN_ID}/run-spec.json",
        result_prefix=f"runs/v1/{RUN_ID}/",
        image_digest="sha256:" + "b" * 64,
        created_at=NOW,
        started_at=NOW if status is not RunStatus.PENDING else None,
        expires_at=int((NOW + timedelta(days=45)).timestamp()),
    )


def seed_repository(*, status: RunStatus = RunStatus.RUNNING) -> FakeRunRepository:
    repository = FakeRunRepository()
    pending = record(status=RunStatus.PENDING)
    repository.create_pending(pending)
    if status is RunStatus.RUNNING:
        repository.mark_running(RUN_ID, NOW)
    return repository


def seed_artifacts(
    store: FakeObjectStore,
    *,
    bodies: dict[str, bytes] | None = None,
    entries: list[dict[str, object]] | None = None,
    manifest_body: bytes | None = None,
) -> None:
    artifact_bodies = REQUIRED_BODIES if bodies is None else bodies
    prefix = f"runs/v1/{RUN_ID}/"
    for name, body in artifact_bodies.items():
        content_type = "application/json" if name.endswith(".json") else "application/octet-stream"
        store.put(f"{prefix}{name}", body, content_type)
    if manifest_body is None:
        manifest_entries = entries or [
            {"name": name, "byte_length": len(body), "sha256": sha256_hex(body)}
            for name, body in artifact_bodies.items()
        ]
        manifest_body = canonical_json_bytes({"schema_version": "1", "artifacts": manifest_entries})
    store.put(f"{prefix}checksums.json", manifest_body, "application/json")


def test_finalize_success_verifies_exact_artifacts_before_marking_succeeded() -> None:
    from src.cloud.finalize_handler import finalize_success

    store = FakeObjectStore()
    repository = seed_repository()
    seed_artifacts(store)

    finalized = finalize_success(
        RUN_ID, object_store=store, run_repository=repository, clock=fixed_clock
    )

    assert finalized.status is RunStatus.SUCCEEDED
    assert finalized.completed_at == NOW
    assert [(call.run_id, call.completed_at) for call in repository.mark_succeeded_calls] == [
        (RUN_ID, NOW)
    ]
    assert [(call.run_id, call.consistent) for call in repository.get_calls] == [(RUN_ID, True)]


@pytest.mark.parametrize(
    ("entries", "expected"),
    [
        pytest.param(
            [
                {"name": name, "byte_length": len(body), "sha256": sha256_hex(body)}
                for name, body in REQUIRED_BODIES.items()
                if name != "report.html"
            ],
            "artifact verification failed",
            id="missing",
        ),
        pytest.param(
            [
                *[
                    {"name": name, "byte_length": len(body), "sha256": sha256_hex(body)}
                    for name, body in REQUIRED_BODIES.items()
                ],
                {"name": "untrusted.json", "byte_length": 1, "sha256": "a" * 64},
            ],
            "artifact verification failed",
            id="extra",
        ),
        pytest.param(
            [
                *[
                    {"name": name, "byte_length": len(body), "sha256": sha256_hex(body)}
                    for name, body in REQUIRED_BODIES.items()
                ],
                {
                    "name": "report.html",
                    "byte_length": len(REQUIRED_BODIES["report.html"]),
                    "sha256": sha256_hex(REQUIRED_BODIES["report.html"]),
                },
            ],
            "artifact verification failed",
            id="duplicate",
        ),
    ],
)
def test_finalize_success_rejects_non_exact_manifest_before_terminal_transition(
    entries: list[dict[str, object]], expected: str
) -> None:
    from src.cloud.finalize_handler import ArtifactVerificationError, finalize_success

    store = FakeObjectStore()
    repository = seed_repository()
    seed_artifacts(store, entries=entries)

    with pytest.raises(ArtifactVerificationError, match=expected):
        finalize_success(RUN_ID, object_store=store, run_repository=repository, clock=fixed_clock)

    assert repository.mark_succeeded_calls == ()


@pytest.mark.parametrize(
    ("name", "body", "entries", "expected_error"),
    [
        pytest.param(
            "checksums.json",
            b"x" * (32 * 1024 + 1),
            None,
            ObjectSizeLimitError,
            id="oversized-checksums",
        ),
        pytest.param(
            "result.json",
            b"x" * (64 * 1024 + 1),
            None,
            ObjectSizeLimitError,
            id="oversized-result",
        ),
        pytest.param(
            "run-spec.json",
            b"x" * (64 * 1024 + 1),
            None,
            ObjectSizeLimitError,
            id="oversized-run-spec",
        ),
        pytest.param(
            "trades.parquet",
            b"x" * (16 * 1024 * 1024 + 1),
            None,
            ObjectSizeLimitError,
            id="oversized-trades",
        ),
        pytest.param(
            "equity-curve.parquet",
            b"x" * (32 * 1024 * 1024 + 1),
            None,
            ObjectSizeLimitError,
            id="oversized-equity",
        ),
        pytest.param(
            "report.html",
            b"x" * (8 * 1024 * 1024 + 1),
            None,
            ObjectSizeLimitError,
            id="oversized-report",
        ),
        pytest.param(
            "report.html",
            b"tampered",
            None,
            None,
            id="wrong-digest-and-length",
        ),
    ],
)
def test_finalize_success_rejects_oversized_or_tampered_artifacts_without_transition(
    name: str,
    body: bytes,
    entries: list[dict[str, object]] | None,
    expected_error: type[Exception] | None,
) -> None:
    from src.cloud.finalize_handler import ArtifactVerificationError, finalize_success

    store = FakeObjectStore()
    repository = seed_repository()
    if name == "checksums.json":
        seed_artifacts(store, manifest_body=body)
    elif expected_error is None:
        seed_artifacts(store)
        store._objects[f"runs/v1/{RUN_ID}/{name}"] = body
    else:
        altered = dict(REQUIRED_BODIES)
        altered[name] = body
        seed_artifacts(store, bodies=altered, entries=entries)

    error = expected_error or ArtifactVerificationError
    with pytest.raises(error):
        finalize_success(RUN_ID, object_store=store, run_repository=repository, clock=fixed_clock)

    assert repository.mark_succeeded_calls == ()


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        pytest.param("byte_length", 1, id="wrong-length"),
        pytest.param("sha256", "f" * 64, id="wrong-digest"),
    ],
)
def test_finalize_success_rejects_a_manifest_entry_that_does_not_match_the_body(
    field: str, replacement: int | str
) -> None:
    from src.cloud.finalize_handler import ArtifactVerificationError, finalize_success

    store = FakeObjectStore()
    repository = seed_repository()
    entries = [
        {"name": name, "byte_length": len(body), "sha256": sha256_hex(body)}
        for name, body in REQUIRED_BODIES.items()
    ]
    report_entry = next(entry for entry in entries if entry["name"] == "report.html")
    report_entry[field] = replacement
    seed_artifacts(store, entries=entries)

    with pytest.raises(ArtifactVerificationError, match="artifact verification failed"):
        finalize_success(RUN_ID, object_store=store, run_repository=repository, clock=fixed_clock)

    assert repository.mark_succeeded_calls == ()


@pytest.mark.parametrize(
    "manifest_body",
    [b"not-json", b'{"schema_version":"1","artifacts":{}}'],
)
def test_finalize_success_rejects_invalid_manifest_before_terminal_transition(
    manifest_body: bytes,
) -> None:
    from src.cloud.finalize_handler import ArtifactVerificationError, finalize_success

    store = FakeObjectStore()
    repository = seed_repository()
    seed_artifacts(store, manifest_body=manifest_body)

    with pytest.raises(ArtifactVerificationError):
        finalize_success(RUN_ID, object_store=store, run_repository=repository, clock=fixed_clock)

    assert repository.mark_succeeded_calls == ()


def test_finalize_success_rejects_result_for_a_different_run_before_terminal_transition() -> None:
    from src.cloud.finalize_handler import ArtifactVerificationError, finalize_success

    store = FakeObjectStore()
    repository = seed_repository()
    altered = dict(REQUIRED_BODIES)
    altered["result.json"] = canonical_json_bytes(
        {"run_id": "123e4567-e89b-12d3-a456-426614174001", "schema_version": "1"}
    )
    seed_artifacts(store, bodies=altered)

    with pytest.raises(ArtifactVerificationError, match="result run identifier mismatch"):
        finalize_success(RUN_ID, object_store=store, run_repository=repository, clock=fixed_clock)

    assert repository.mark_succeeded_calls == ()


def test_finalize_success_returns_existing_canonical_success_on_replay() -> None:
    from src.cloud.finalize_handler import finalize_success

    store = FakeObjectStore()
    repository = seed_repository()
    seed_artifacts(store)
    first = finalize_success(
        RUN_ID, object_store=store, run_repository=repository, clock=fixed_clock
    )
    replay = finalize_success(
        RUN_ID, object_store=store, run_repository=repository, clock=fixed_clock
    )

    assert replay == first
    assert len(repository.mark_succeeded_calls) == 2


def test_finalize_success_rejects_a_conflicting_terminal_run() -> None:
    from src.cloud.finalize_handler import finalize_success

    store = FakeObjectStore()
    repository = seed_repository()
    repository.mark_failed(RUN_ID, FailureCode.WORKER_FAILED, NOW)
    seed_artifacts(store)

    with pytest.raises(StateTransitionError):
        finalize_success(RUN_ID, object_store=store, run_repository=repository, clock=fixed_clock)


@pytest.mark.parametrize("status", [RunStatus.PENDING, RunStatus.RUNNING])
def test_finalize_failure_transitions_pending_or_running_with_only_closed_code(
    status: RunStatus,
) -> None:
    from src.cloud.finalize_handler import finalize_failure

    repository = seed_repository(status=status)

    finalized = finalize_failure(
        RUN_ID,
        FailureCode.WORKER_FAILED,
        run_repository=repository,
        clock=fixed_clock,
    )

    assert finalized.status is RunStatus.FAILED
    assert finalized.failure_code is FailureCode.WORKER_FAILED
    assert finalized.completed_at == NOW


def test_finalize_failure_replays_only_the_same_closed_failure() -> None:
    from src.cloud.finalize_handler import finalize_failure

    repository = seed_repository()
    first = finalize_failure(
        RUN_ID, FailureCode.WORKER_FAILED, run_repository=repository, clock=fixed_clock
    )
    replay = finalize_failure(
        RUN_ID, FailureCode.WORKER_FAILED, run_repository=repository, clock=fixed_clock
    )

    assert replay == first
    with pytest.raises(StateTransitionError):
        finalize_failure(
            RUN_ID,
            FailureCode.WORKFLOW_TIMED_OUT,
            run_repository=repository,
            clock=fixed_clock,
        )


def test_handle_finalization_accepts_only_closed_routing_shapes() -> None:
    from src.cloud.finalize_handler import handle_finalization

    store = FakeObjectStore()
    repository = seed_repository()
    seed_artifacts(store)

    response = handle_finalization(
        {"run_id": RUN_ID, "outcome": "SUCCEEDED"},
        object_store=store,
        run_repository=repository,
        clock=fixed_clock,
    )

    assert response == {"run_id": RUN_ID, "status": "SUCCEEDED"}
    with pytest.raises(ValueError):
        handle_finalization(
            {
                "run_id": RUN_ID,
                "outcome": "FAILED",
                "failure_code": "WORKER_FAILED",
                "Cause": "secret",
            },
            object_store=store,
            run_repository=repository,
            clock=fixed_clock,
        )


class RecordingBoto3(ModuleType):
    def __init__(self) -> None:
        super().__init__("boto3")
        self.calls: list[str] = []

    def client(self, name: str) -> object:
        self.calls.append(f"client:{name}")
        raise AssertionError("client creation is not expected for invalid runtime configuration")

    def resource(self, name: str) -> object:
        self.calls.append(f"resource:{name}")
        raise AssertionError("resource creation is not expected for invalid runtime configuration")


def test_lambda_handler_defers_boto_and_rejects_invalid_environment_before_client_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.cloud.finalize_handler import lambda_handler

    fake_boto3 = RecordingBoto3()
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    monkeypatch.setenv("ARTIFACT_BUCKET", "not valid bucket!")
    monkeypatch.setenv("RUN_TABLE", "research-runs")

    with pytest.raises(ValueError):
        lambda_handler(
            {"run_id": RUN_ID, "outcome": "FAILED", "failure_code": "WORKER_FAILED"},
            object(),
        )

    assert fake_boto3.calls == []


def test_finalize_handler_import_does_not_load_boto3(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(sys.modules, "src.cloud.finalize_handler", raising=False)
    monkeypatch.delitem(sys.modules, "boto3", raising=False)

    __import__("src.cloud.finalize_handler")

    assert "boto3" not in sys.modules
