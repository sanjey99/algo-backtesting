from __future__ import annotations

import sys
from datetime import UTC, datetime
from types import ModuleType
from uuid import UUID

import pytest
from pydantic import ValidationError

from src.cloud.contracts import RunStatus, Visibility, canonical_json_bytes
from src.cloud.prepare_handler import PreparedRun, RunPreparationError, lambda_handler, prepare_run
from src.cloud.storage import StateTransitionError, StoredObject
from tests.cloud.fakes import FakeObjectStore, FakeRunRepository

NOW = datetime(2024, 3, 29, 12, 0, tzinfo=UTC)
RUN_ID = UUID("123e4567-e89b-12d3-a456-426614174000")
IMAGE_DIGEST = "sha256:" + "c" * 64


def ingestion_event(**overrides: object) -> dict[str, object]:
    event: dict[str, object] = {
        "request": {
            "schema_version": "1",
            "symbol": "SPY",
            "start": "2024-01-02",
            "end": "2024-03-28",
            "strategy_key": "ma_crossover",
            "strategy_parameters": {"fast_period": 10, "slow_period": 50},
            "initial_capital": 100_000.0,
            "commission_pct": 0.001,
            "slippage_pct": 0.0005,
            "visibility": "PRIVATE",
        },
        "dataset": {
            "schema_version": "1",
            "bucket": "research-artifacts",
            "key": "datasets/v1/acquisition-1/spy.parquet",
            "sha256": "a" * 64,
            "manifest_key": "datasets/v1/acquisition-1/manifest.json",
            "manifest_sha256": "b" * 64,
            "symbol": "SPY",
            "calendar": "XNYS",
            "interval": "1d",
            "start": "2024-01-02",
            "end": "2024-03-28",
            "acquisition_id": "acquisition-1",
            "completed_at": "2024-03-29T12:00:00Z",
        },
    }
    event.update(overrides)
    return event


def event_component(name: str) -> dict[str, object]:
    component = ingestion_event()[name]
    assert isinstance(component, dict)
    return {key: value for key, value in component.items() if isinstance(key, str)}


def fixed_clock() -> datetime:
    return NOW


def fixed_uuid() -> UUID:
    return RUN_ID


def test_prepare_run_publishes_canonical_pinned_spec_then_creates_private_pending_record() -> None:
    store = FakeObjectStore()
    repository = FakeRunRepository()

    prepared = prepare_run(
        ingestion_event(),
        object_store=store,
        run_repository=repository,
        image_digest=IMAGE_DIGEST,
        clock=fixed_clock,
        uuid_factory=fixed_uuid,
    )

    run_spec_key = "runs/v1/123e4567-e89b-12d3-a456-426614174000/run-spec.json"
    expected_spec = {
        "schema_version": "1",
        "run_id": "123e4567-e89b-12d3-a456-426614174000",
        "dataset": ingestion_event()["dataset"],
        "request": ingestion_event()["request"],
        "image_digest": IMAGE_DIGEST,
        "created_at": "2024-03-29T12:00:00Z",
        "maximum_runtime_seconds": 600,
        "run_spec_key": run_spec_key,
        "result_prefix": "runs/v1/123e4567-e89b-12d3-a456-426614174000/",
    }
    assert prepared == PreparedRun(
        run_id="123e4567-e89b-12d3-a456-426614174000", run_spec_key=run_spec_key
    )
    assert prepared.to_dict() == {
        "run_id": "123e4567-e89b-12d3-a456-426614174000",
        "run_spec_key": run_spec_key,
    }
    assert store.put_calls[0].key == run_spec_key
    assert store.put_calls[0].content_type == "application/json"
    assert store.put_calls[0].body == canonical_json_bytes(expected_spec)

    record = repository.create_pending_calls[0].record
    assert record.run_id == prepared.run_id
    assert record.status is RunStatus.PENDING
    assert record.visibility is Visibility.PRIVATE
    assert record.dataset_key == "datasets/v1/acquisition-1/spy.parquet"
    assert record.dataset_sha256 == "a" * 64
    assert record.run_spec_key == run_spec_key
    assert record.result_prefix == "runs/v1/123e4567-e89b-12d3-a456-426614174000/"
    assert record.image_digest == IMAGE_DIGEST
    assert record.created_at == NOW
    assert record.expires_at == 1_715_601_600
    assert record.started_at is None
    assert record.completed_at is None
    assert record.failure_code is None


@pytest.mark.parametrize(
    "event",
    [
        {"request": event_component("request")},
        {**ingestion_event(), "run_spec_key": "runs/v1/caller-selected/run-spec.json"},
        {**ingestion_event(), "request": {**event_component("request"), "extra": "nope"}},
        {**ingestion_event(), "dataset": {**event_component("dataset"), "key": "unsafe/key"}},
    ],
)
def test_prepare_run_revalidates_complete_ingestion_shape_before_effects(event: object) -> None:
    store = FakeObjectStore()
    repository = FakeRunRepository()
    uuid_calls = 0

    def counting_uuid() -> UUID:
        nonlocal uuid_calls
        uuid_calls += 1
        return RUN_ID

    with pytest.raises(ValidationError):
        prepare_run(
            event,
            object_store=store,
            run_repository=repository,
            image_digest=IMAGE_DIGEST,
            clock=fixed_clock,
            uuid_factory=counting_uuid,
        )

    assert uuid_calls == 0
    assert store.put_calls == ()
    assert repository.create_pending_calls == ()


class FailingObjectStore(FakeObjectStore):
    def put(self, key: str, body: bytes, content_type: str) -> StoredObject:
        del key, body, content_type
        raise RuntimeError("storage-token=secret-value")


def test_prepare_run_does_not_create_metadata_when_spec_publication_fails() -> None:
    repository = FakeRunRepository()

    with pytest.raises(
        RunPreparationError, match="^run specification publication failed$"
    ) as error:
        prepare_run(
            ingestion_event(),
            object_store=FailingObjectStore(),
            run_repository=repository,
            image_digest=IMAGE_DIGEST,
            clock=fixed_clock,
            uuid_factory=fixed_uuid,
        )

    assert "secret-value" not in str(error.value)
    assert repository.create_pending_calls == ()


def test_prepare_run_duplicate_uuid_does_not_overwrite_first_pending_record() -> None:
    store = FakeObjectStore()
    repository = FakeRunRepository()

    first = prepare_run(
        ingestion_event(),
        object_store=store,
        run_repository=repository,
        image_digest=IMAGE_DIGEST,
        clock=fixed_clock,
        uuid_factory=fixed_uuid,
    )
    with pytest.raises(StateTransitionError):
        prepare_run(
            ingestion_event(),
            object_store=store,
            run_repository=repository,
            image_digest=IMAGE_DIGEST,
            clock=fixed_clock,
            uuid_factory=fixed_uuid,
        )

    assert repository.get(first.run_id, consistent=True) == (
        repository.create_pending_calls[0].record
    )
    assert len(repository.create_pending_calls) == 1
    assert len(store.put_calls) == 2
    assert store.put_calls[0].body == store.put_calls[1].body


@pytest.mark.parametrize("ttl_days", [0, -1, True, 1.5])
def test_prepare_run_rejects_invalid_ttl_before_effects(ttl_days: object) -> None:
    store = FakeObjectStore()
    repository = FakeRunRepository()

    with pytest.raises(ValueError, match="ttl_days must be a positive integer"):
        prepare_run(
            ingestion_event(),
            object_store=store,
            run_repository=repository,
            image_digest=IMAGE_DIGEST,
            clock=fixed_clock,
            uuid_factory=fixed_uuid,
            ttl_days=ttl_days,  # type: ignore[arg-type]
        )

    assert store.put_calls == ()
    assert repository.create_pending_calls == ()


class RecordingS3Client:
    def __init__(self) -> None:
        self.put_calls: list[dict[str, object]] = []

    def put_object(self, **kwargs: object) -> None:
        self.put_calls.append(dict(kwargs))


class RecordingTable:
    def __init__(self) -> None:
        self.put_calls: list[dict[str, object]] = []

    def put_item(self, **kwargs: object) -> None:
        self.put_calls.append(dict(kwargs))


class RecordingBoto3(ModuleType):
    def __init__(self) -> None:
        super().__init__("boto3")
        self.s3 = RecordingS3Client()
        self.table = RecordingTable()

    def client(self, name: str) -> RecordingS3Client:
        assert name == "s3"
        return self.s3

    def resource(self, name: str) -> RecordingBoto3:
        assert name == "dynamodb"
        return self

    def Table(self, name: str) -> RecordingTable:  # noqa: N802
        assert name == "research-runs"
        return self.table


def test_lambda_handler_builds_aws_adapters_only_after_validating_runtime_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_boto3 = RecordingBoto3()
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    monkeypatch.setenv("ENGINE_IMAGE_DIGEST", IMAGE_DIGEST)
    monkeypatch.setenv("ARTIFACT_BUCKET", "research-artifacts")
    monkeypatch.setenv("RUN_TABLE", "research-runs")
    monkeypatch.setenv("RUN_TTL_DAYS", "7")

    response = lambda_handler(ingestion_event(), object())

    run_id = response["run_id"]
    assert isinstance(run_id, str)
    assert str(UUID(run_id)) == run_id
    assert response["run_spec_key"] == f"runs/v1/{run_id}/run-spec.json"
    assert len(fake_boto3.s3.put_calls) == 1
    assert fake_boto3.s3.put_calls[0]["Key"] == response["run_spec_key"]
    item = fake_boto3.table.put_calls[0]["Item"]
    assert isinstance(item, dict)
    assert item["PK"] == f"RUN#{run_id}"
    assert item["status"] == "PENDING"
    assert item["visibility"] == "PRIVATE"
    assert item["image_digest"] == IMAGE_DIGEST
    assert fake_boto3.table.put_calls[0]["ConditionExpression"] == "attribute_not_exists(PK)"


def test_lambda_handler_rejects_mutable_image_and_bad_ttl_without_constructing_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_boto3 = RecordingBoto3()
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    monkeypatch.setenv("ENGINE_IMAGE_DIGEST", "latest")
    monkeypatch.setenv("ARTIFACT_BUCKET", "research-artifacts")
    monkeypatch.setenv("RUN_TABLE", "research-runs")
    monkeypatch.setenv("RUN_TTL_DAYS", "0")

    with pytest.raises(ValueError):
        lambda_handler(ingestion_event(), object())

    assert fake_boto3.s3.put_calls == []
    assert fake_boto3.table.put_calls == []
