"""Offline tests for the closed public cloud-results HTTP boundary."""

from __future__ import annotations

import json
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
from src.cloud.storage import ObjectIntegrityError
from tests.cloud.fakes import FakeObjectStore, FakeRunRepository

NOW = datetime(2024, 3, 29, 12, 0, tzinfo=UTC)
RUN_ID = "123e4567-e89b-12d3-a456-426614174000"
OTHER_RUN_ID = "123e4567-e89b-12d3-a456-426614174001"
PREFIX = f"runs/v1/{RUN_ID}/"


def result_payload(
    *, run_id: str = RUN_ID, metrics: dict[str, float | None] | None = None
) -> dict[str, object]:
    return {
        "schema_version": "1",
        "run_id": run_id,
        "symbol": "SPY",
        "start_date": "2024-01-02",
        "end_date": "2024-03-28",
        "strategy_name": "MovingAverageCrossover",
        "strategy_parameters": {"fast_period": 10, "slow_period": 50},
        "initial_capital": 100000.0,
        "final_equity": 101234.5,
        "metrics": {"sharpe_ratio": 1.25} if metrics is None else metrics,
        "total_trades": 4,
        "image_digest": "sha256:" + "b" * 64,
        "dataset_sha256": "a" * 64,
        "completed_at": "2024-03-29T12:00:00Z",
    }


def public_record(
    *,
    status: RunStatus = RunStatus.SUCCEEDED,
    visibility: Visibility = Visibility.PUBLIC,
    failure_code: FailureCode | None = None,
) -> RunRecord:
    return RunRecord(
        run_id=RUN_ID,
        status=status,
        visibility=visibility,
        dataset_key="datasets/v1/acquisition-1/spy.parquet",
        dataset_sha256="a" * 64,
        run_spec_key=f"{PREFIX}run-spec.json",
        result_prefix=PREFIX,
        image_digest="sha256:" + "b" * 64,
        created_at=NOW,
        started_at=NOW if status is not RunStatus.PENDING else None,
        completed_at=NOW if status in {RunStatus.SUCCEEDED, RunStatus.FAILED} else None,
        failure_code=failure_code,
        expires_at=int((NOW + timedelta(days=45)).timestamp()),
    )


def seed_repository(record: RunRecord) -> FakeRunRepository:
    repository = FakeRunRepository()
    pending = public_record(status=RunStatus.PENDING, visibility=record.visibility)
    repository.create_pending(pending)
    if record.status is RunStatus.RUNNING:
        repository.mark_running(RUN_ID, NOW)
    elif record.status is RunStatus.SUCCEEDED:
        repository.mark_running(RUN_ID, NOW)
        repository.mark_succeeded(RUN_ID, NOW)
    elif record.status is RunStatus.FAILED:
        repository.mark_failed(RUN_ID, FailureCode.WORKER_FAILED, NOW)
    return repository


def seed_artifacts(
    store: FakeObjectStore,
    *,
    result_body: bytes | None = None,
    manifest_body: bytes | None = None,
    result_entry: dict[str, object] | None = None,
) -> bytes:
    body = canonical_json_bytes(result_payload()) if result_body is None else result_body
    artifacts = {
        "run-spec.json": b'{"schema_version":"1"}',
        "result.json": body,
        "trades.parquet": b"parquet-trades",
        "equity-curve.parquet": b"parquet-equity",
        "report.html": b"<html><body>report</body></html>",
    }
    for name, artifact_body in artifacts.items():
        content_type = "application/json" if name.endswith(".json") else "application/octet-stream"
        store.put(f"{PREFIX}{name}", artifact_body, content_type)
    if manifest_body is None:
        entries = [
            result_entry
            if name == "result.json" and result_entry is not None
            else {
                "name": name,
                "byte_length": len(artifact_body),
                "sha256": sha256_hex(artifact_body),
            }
            for name, artifact_body in artifacts.items()
        ]
        manifest_body = canonical_json_bytes({"schema_version": "1", "artifacts": entries})
    store.put(f"{PREFIX}checksums.json", manifest_body, "application/json")
    return body


def result_call(run_id: str, store: FakeObjectStore, repository: FakeRunRepository) -> object:
    from src.cloud.results_handler import get_public_result

    return get_public_result(run_id, object_store=store, run_repository=repository)


def not_found() -> object:
    from src.cloud.results_handler import NOT_FOUND

    return NOT_FOUND


def test_public_result_returns_only_verified_summary_and_five_minute_report_url() -> None:
    store = FakeObjectStore()
    repository = seed_repository(public_record())
    original_result = seed_artifacts(store)

    response = result_call(RUN_ID, store, repository)

    assert response.status_code == 200
    assert response.headers == {"content-type": "application/json", "cache-control": "no-store"}
    payload = json.loads(response.body)
    assert payload == {
        **result_payload(),
        "report_url": f"https://fake.invalid/{PREFIX}report.html?expires=300",
        "report_url_expires_seconds": 300,
    }
    assert b"parquet" not in response.body
    assert store._objects[f"{PREFIX}result.json"] == original_result
    assert [(call.run_id, call.consistent) for call in repository.get_calls[-1:]] == [
        (RUN_ID, True)
    ]


@pytest.mark.parametrize(
    "run_id",
    ["not-a-uuid", RUN_ID.upper(), "123e4567-e89b-12d3-a456-426614174000/extra"],
)
def test_invalid_uuid_returns_not_found_without_repository_or_storage_calls(run_id: str) -> None:
    store = FakeObjectStore()
    repository = FakeRunRepository()

    assert result_call(run_id, store, repository) == not_found()
    assert repository.get_calls == ()


@pytest.mark.parametrize(
    "record",
    [
        pytest.param(public_record(visibility=Visibility.PRIVATE), id="private"),
        pytest.param(public_record(status=RunStatus.PENDING), id="pending"),
        pytest.param(public_record(status=RunStatus.RUNNING), id="running"),
        pytest.param(
            public_record(
                status=RunStatus.FAILED,
                failure_code=FailureCode.WORKER_FAILED,
            ),
            id="failed",
        ),
    ],
)
def test_non_public_or_nonterminal_records_are_byte_identical_not_found(record: RunRecord) -> None:
    store = FakeObjectStore()
    repository = seed_repository(record)

    response = result_call(RUN_ID, store, repository)

    assert response == not_found()
    assert store._objects == {}


class RawRepository:
    def __init__(self, value: object) -> None:
        self.value = value
        self.calls: list[tuple[str, bool]] = []

    def get(self, run_id: str, consistent: bool = False) -> object:
        self.calls.append((run_id, consistent))
        return self.value


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(None, id="absent"),
        pytest.param({"run_id": RUN_ID}, id="malformed-mapping"),
        pytest.param(object(), id="malformed-object"),
        pytest.param(
            {**public_record().model_dump(mode="json"), "run_id": OTHER_RUN_ID}, id="wrong-run-id"
        ),
        pytest.param(
            {**public_record().model_dump(mode="json"), "completed_at": None},
            id="incomplete-terminal-record",
        ),
    ],
)
def test_absent_or_malformed_record_is_not_found(value: object) -> None:
    store = FakeObjectStore()
    repository = RawRepository(value)

    assert result_call(RUN_ID, store, repository) == not_found()
    assert repository.calls == [(RUN_ID, True)]
    assert store._objects == {}


def test_incomplete_succeeded_record_is_hidden_before_artifact_reads() -> None:
    store = FakeObjectStore()
    seed_artifacts(store)
    incomplete = {**public_record().model_dump(mode="json"), "completed_at": None}
    repository = RawRepository(incomplete)

    assert result_call(RUN_ID, store, repository) == not_found()
    assert repository.calls == [(RUN_ID, True)]


@pytest.mark.parametrize(
    "manifest_body",
    [
        pytest.param(b"not-json", id="malformed"),
        pytest.param(
            b'{"schema_version":"1","schema_version":"1","artifacts":[]}',
            id="duplicate-key",
        ),
        pytest.param(b'{"artifacts":[],"schema_version":"1"}', id="noncanonical"),
        pytest.param(
            canonical_json_bytes({"schema_version": "1", "artifacts": []}),
            id="wrong-set",
        ),
    ],
)
def test_invalid_or_noncanonical_manifest_is_not_found(manifest_body: bytes) -> None:
    store = FakeObjectStore()
    repository = seed_repository(public_record())
    seed_artifacts(store, manifest_body=manifest_body)

    assert result_call(RUN_ID, store, repository) == not_found()


@pytest.mark.parametrize(
    "result_body",
    [
        pytest.param(b"not-json", id="malformed"),
        pytest.param(
            b'{"run_id":"123e4567-e89b-12d3-a456-426614174000","run_id":"123e4567-e89b-12d3-a456-426614174000"}',
            id="duplicate-key",
        ),
        pytest.param(
            canonical_json_bytes(result_payload(run_id=OTHER_RUN_ID)), id="wrong-run-id"
        ),
        pytest.param(
            canonical_json_bytes({**result_payload(), "extra": "not-public"}),
            id="extra-field",
        ),
    ],
)
def test_malformed_or_wrong_result_is_not_found(result_body: bytes) -> None:
    store = FakeObjectStore()
    repository = seed_repository(public_record())
    seed_artifacts(store, result_body=result_body)

    assert result_call(RUN_ID, store, repository) == not_found()


@pytest.mark.parametrize(
    ("field", "value"),
    [("byte_length", 1), ("sha256", "f" * 64)],
)
def test_manifest_result_length_or_digest_mismatch_is_not_found(field: str, value: object) -> None:
    store = FakeObjectStore()
    repository = seed_repository(public_record())
    result = canonical_json_bytes(result_payload())
    entry = {"name": "result.json", "byte_length": len(result), "sha256": sha256_hex(result)}
    entry[field] = value
    seed_artifacts(store, result_entry=entry)

    assert result_call(RUN_ID, store, repository) == not_found()


def test_tampered_result_or_report_entry_is_not_found() -> None:
    store = FakeObjectStore()
    repository = seed_repository(public_record())
    seed_artifacts(store)
    store._objects[f"{PREFIX}result.json"] = b'{"tampered":true}'

    assert result_call(RUN_ID, store, repository) == not_found()

    store = FakeObjectStore()
    repository = seed_repository(public_record())
    result = canonical_json_bytes(result_payload())
    entries = [
        {"name": name, "byte_length": len(body), "sha256": sha256_hex(body)}
        for name, body in {
            "run-spec.json": b'{"schema_version":"1"}',
            "result.json": result,
            "trades.parquet": b"parquet-trades",
            "equity-curve.parquet": b"parquet-equity",
            "report.html": b"<html><body>report</body></html>",
        }.items()
    ]
    report_entry = next(entry for entry in entries if entry["name"] == "report.html")
    report_entry["sha256"] = "f" * 64
    seed_artifacts(
        store,
        result_body=result,
        manifest_body=canonical_json_bytes({"schema_version": "1", "artifacts": entries}),
    )

    assert result_call(RUN_ID, store, repository) == not_found()


def test_missing_or_oversized_manifest_or_result_is_not_found() -> None:
    store = FakeObjectStore()
    repository = seed_repository(public_record())
    seed_artifacts(store)
    del store._objects[f"{PREFIX}checksums.json"]

    assert result_call(RUN_ID, store, repository) == not_found()

    store = FakeObjectStore()
    repository = seed_repository(public_record())
    seed_artifacts(store, result_body=b"x" * (64 * 1024 + 1))
    assert result_call(RUN_ID, store, repository) == not_found()


class BrokenObjectStore(FakeObjectStore):
    def get(self, key: str, maximum_bytes: int) -> bytes:
        raise ObjectIntegrityError("untrusted storage details")


def test_storage_verification_failure_is_not_found() -> None:
    assert result_call(RUN_ID, BrokenObjectStore(), seed_repository(public_record())) == not_found()


class RecordingPresignStore(FakeObjectStore):
    def __init__(self) -> None:
        super().__init__()
        self.presign_calls: list[tuple[str, int]] = []

    def presign_get(self, key: str, expires_seconds: int) -> str:
        self.presign_calls.append((key, expires_seconds))
        return super().presign_get(key, expires_seconds)


def test_report_key_is_derived_and_exact_manifest_entry_is_required() -> None:
    store = RecordingPresignStore()
    repository = seed_repository(public_record())
    seed_artifacts(store)

    assert result_call(RUN_ID, store, repository).status_code == 200
    assert store.presign_calls == [(f"{PREFIX}report.html", 300)]


def test_response_limit_is_enforced_after_adding_presigned_url() -> None:
    class LongUrlStore(RecordingPresignStore):
        def presign_get(self, key: str, expires_seconds: int) -> str:
            super().presign_get(key, expires_seconds)
            return "https://fake.invalid/" + "x" * (64 * 1024)

    store = LongUrlStore()
    repository = seed_repository(public_record())
    seed_artifacts(store)

    assert result_call(RUN_ID, store, repository) == not_found()


def event(*, method: str = "GET", run_id: object = RUN_ID, **changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "version": "2.0",
        "rawQueryString": "",
        "body": None,
        "isBase64Encoded": False,
        "pathParameters": {"run_id": run_id},
        "requestContext": {"http": {"method": method}},
    }
    value.update(changes)
    return value


def _unexpected_dependencies() -> tuple[object, object]:
    raise AssertionError("dependency construction was not expected")


def test_lambda_adapter_rejects_non_get_without_business_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.cloud.results_handler import lambda_handler

    monkeypatch.setattr("src.cloud.results_handler._runtime_dependencies", _unexpected_dependencies)

    response = lambda_handler(event(method="POST"), object())

    assert response == {
        "statusCode": 405,
        "headers": {"content-type": "application/json", "cache-control": "no-store"},
        "body": '{"error":"method_not_allowed"}',
    }
    assert "access-control-allow-origin" not in response["headers"]


def test_lambda_adapter_returns_the_core_wire_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.cloud.results_handler import lambda_handler

    store = FakeObjectStore()
    repository = seed_repository(public_record())
    seed_artifacts(store)
    monkeypatch.setattr(
        "src.cloud.results_handler._runtime_dependencies",
        lambda: (store, repository),
    )

    response = lambda_handler(event(), object())

    assert response["statusCode"] == 200
    assert response["headers"] == {
        "content-type": "application/json",
        "cache-control": "no-store",
    }
    assert isinstance(response["body"], str)
    assert "access-control-allow-origin" not in response["headers"]


@pytest.mark.parametrize(
    "invalid_event",
    [
        pytest.param(event(version="1.0"), id="wrong-version"),
        pytest.param(event(pathParameters={"run_id": RUN_ID, "extra": "x"}), id="extra-path"),
        pytest.param(event(rawQueryString="x=1"), id="query"),
        pytest.param(event(body="{}"), id="body"),
        pytest.param(event(bucket="chosen-by-caller"), id="caller-bucket"),
        pytest.param(event(requestContext={"http": {"method": 1}}), id="bad-method-type"),
    ],
)
def test_lambda_adapter_returns_closed_bad_request_for_invalid_event_shapes(
    monkeypatch: pytest.MonkeyPatch, invalid_event: object
) -> None:
    from src.cloud.results_handler import lambda_handler

    monkeypatch.setattr("src.cloud.results_handler._runtime_dependencies", _unexpected_dependencies)

    assert lambda_handler(invalid_event, object()) == {
        "statusCode": 400,
        "headers": {"content-type": "application/json", "cache-control": "no-store"},
        "body": '{"error":"bad_request"}',
    }


class RecordingBoto3(ModuleType):
    def __init__(self) -> None:
        super().__init__("boto3")
        self.calls: list[str] = []

    def client(self, name: str) -> object:
        self.calls.append(f"client:{name}")
        raise AssertionError("client construction was not expected")

    def resource(self, name: str) -> object:
        self.calls.append(f"resource:{name}")
        raise AssertionError("resource construction was not expected")


def test_lambda_handler_is_import_inert_and_validates_environment_before_boto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delitem(sys.modules, "src.cloud.results_handler", raising=False)
    monkeypatch.delitem(sys.modules, "boto3", raising=False)
    __import__("src.cloud.results_handler")
    assert "boto3" not in sys.modules

    from src.cloud.results_handler import lambda_handler

    fake_boto3 = RecordingBoto3()
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    monkeypatch.setenv("ARTIFACT_BUCKET", "not valid bucket!")
    monkeypatch.setenv("RUN_TABLE", "research-runs")

    with pytest.raises(ValueError):
        lambda_handler(event(), object())

    assert fake_boto3.calls == []
