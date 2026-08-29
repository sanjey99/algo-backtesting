from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from io import BytesIO

import pytest
from botocore.exceptions import ClientError

from src.cloud.contracts import FailureCode, RunRecord, RunStatus, Visibility, sha256_hex
from src.cloud.storage import (
    DynamoRunRepository,
    ImmutableObjectConflict,
    LifecycleClass,
    ObjectNotFoundError,
    ObjectSizeLimitError,
    S3ObjectStore,
    StateTransitionError,
)
from tests.cloud.fakes import FakeObjectStore, FakeRunRepository

RUN_ID = "123e4567-e89b-12d3-a456-426614174000"
NOW = datetime(2024, 3, 29, 12, 0, tzinfo=UTC)
DATASET_KEY = "datasets/v1/acquisition-1/spy.parquet"


@dataclass(slots=True)
class RecordingS3Client:
    put_calls: list[dict[str, object]] = field(default_factory=list)
    head_calls: list[dict[str, object]] = field(default_factory=list)
    get_calls: list[dict[str, object]] = field(default_factory=list)
    presign_calls: list[tuple[str, dict[str, object], int]] = field(default_factory=list)
    objects: dict[str, bytes] = field(default_factory=dict)
    content_types: dict[str, str] = field(default_factory=dict)
    fail_put_with: ClientError | None = None
    fail_head_with: ClientError | None = None
    fail_get_with: ClientError | None = None

    def put_object(self, **kwargs: object) -> None:
        self.put_calls.append(dict(kwargs))
        if self.fail_put_with is not None:
            raise self.fail_put_with
        body = kwargs["Body"]
        assert isinstance(body, bytes)
        self.objects[str(kwargs["Key"])] = bytes(body)
        self.content_types[str(kwargs["Key"])] = str(kwargs["ContentType"])

    def head_object(self, **kwargs: object) -> dict[str, object]:
        self.head_calls.append(dict(kwargs))
        if self.fail_head_with is not None:
            raise self.fail_head_with
        key = str(kwargs["Key"])
        body = self.objects[key]
        return {
            "ContentLength": len(body),
            "Metadata": {"sha256": sha256_hex(body)},
            "ContentType": self.content_types.get(key, "application/octet-stream"),
        }

    def get_object(self, **kwargs: object) -> dict[str, object]:
        self.get_calls.append(dict(kwargs))
        if self.fail_get_with is not None:
            raise self.fail_get_with
        return {"Body": BytesIO(self.objects[str(kwargs["Key"])])}

    def generate_presigned_url(self, client_method: str, **kwargs: object) -> str:
        params = kwargs["Params"]
        expires_in = kwargs["ExpiresIn"]
        assert isinstance(params, dict)
        assert isinstance(expires_in, int)
        self.presign_calls.append((client_method, dict(params), expires_in))
        return "https://signed.example/report"


@dataclass(slots=True)
class RecordingTable:
    put_calls: list[dict[str, object]] = field(default_factory=list)
    update_calls: list[dict[str, object]] = field(default_factory=list)
    get_calls: list[dict[str, object]] = field(default_factory=list)
    item: dict[str, object] | None = None
    fail_with: ClientError | None = None

    def put_item(self, **kwargs: object) -> None:
        self.put_calls.append(dict(kwargs))
        if self.fail_with is not None:
            raise self.fail_with

    def update_item(self, **kwargs: object) -> None:
        self.update_calls.append(dict(kwargs))
        if self.fail_with is not None:
            raise self.fail_with

    def get_item(self, **kwargs: object) -> dict[str, object]:
        self.get_calls.append(dict(kwargs))
        return {} if self.item is None else {"Item": dict(self.item)}


def conditional_failure() -> ClientError:
    return ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "AWS internals"}},
        "UpdateItem",
    )


def precondition_failure() -> ClientError:
    return ClientError(
        {"Error": {"Code": "PreconditionFailed", "Message": "AWS internals"}}, "PutObject"
    )


def not_found_failure(operation: str) -> ClientError:
    return ClientError({"Error": {"Code": "NoSuchKey", "Message": "missing"}}, operation)


def service_failure(operation: str) -> ClientError:
    return ClientError({"Error": {"Code": "SlowDown", "Message": "retry later"}}, operation)


def record(**overrides: object) -> RunRecord:
    values: dict[str, object] = {
        "run_id": RUN_ID,
        "status": RunStatus.PENDING,
        "visibility": Visibility.PRIVATE,
        "dataset_key": DATASET_KEY,
        "dataset_sha256": "a" * 64,
        "run_spec_key": f"runs/v1/{RUN_ID}/run-spec.json",
        "result_prefix": f"runs/v1/{RUN_ID}/",
        "image_digest": "sha256:" + "b" * 64,
        "created_at": NOW,
        "expires_at": 1_727_611_200,
    }
    values.update(overrides)
    return RunRecord.model_validate(values)


def test_s3_put_is_fixed_bucket_private_encrypted_and_digest_bearing() -> None:
    client = RecordingS3Client()
    store = S3ObjectStore(client=client, bucket="research-artifacts")

    stored = store.put(DATASET_KEY, b"parquet", "application/x-parquet")

    assert stored.key == DATASET_KEY
    assert stored.byte_length == 7
    assert stored.sha256 == sha256_hex(b"parquet")
    assert client.put_calls == [
        {
            "Bucket": "research-artifacts",
            "Key": DATASET_KEY,
            "Body": b"parquet",
            "ContentType": "application/x-parquet",
            "ServerSideEncryption": "AES256",
            "Metadata": {"sha256": sha256_hex(b"parquet")},
            "Tagging": "LifecycleClass=transient",
            "IfNoneMatch": "*",
        }
    ]


def test_s3_put_rejects_non_enum_lifecycle_class() -> None:
    client = RecordingS3Client()
    store = S3ObjectStore(client=client, bucket="research-artifacts")

    with pytest.raises(TypeError, match="lifecycle_class must be a LifecycleClass"):
        store.put(DATASET_KEY, b"parquet", "application/x-parquet", lifecycle_class="transient")  # type: ignore[arg-type]

    assert client.put_calls == []


def test_s3_put_accepts_transient_and_selected_public_lifecycle_tags() -> None:
    client = RecordingS3Client()
    store = S3ObjectStore(client=client, bucket="research-artifacts")

    store.put(DATASET_KEY, b"parquet", "application/x-parquet")
    store.put(
        DATASET_KEY.replace("spy", "selected"),
        b"parquet",
        "application/x-parquet",
        lifecycle_class=LifecycleClass.SELECTED_PUBLIC,
    )

    assert client.put_calls[0]["Tagging"] == "LifecycleClass=transient"
    assert client.put_calls[1]["Tagging"] == "LifecycleClass=selected-public"


def test_s3_put_only_accepts_byte_identical_immutable_replays() -> None:
    client = RecordingS3Client(objects={DATASET_KEY: b"same"}, fail_put_with=precondition_failure())
    store = S3ObjectStore(client=client, bucket="research-artifacts")

    assert store.put(DATASET_KEY, b"same", "application/octet-stream").sha256 == sha256_hex(b"same")
    with pytest.raises(ImmutableObjectConflict) as error:
        store.put(DATASET_KEY, b"different", "application/octet-stream")

    assert "AWS internals" not in str(error.value)
    assert client.head_calls == [
        {"Bucket": "research-artifacts", "Key": DATASET_KEY},
        {"Bucket": "research-artifacts", "Key": DATASET_KEY},
    ]
    assert client.get_calls == [
        {"Bucket": "research-artifacts", "Key": DATASET_KEY},
        {"Bucket": "research-artifacts", "Key": DATASET_KEY},
    ]


def test_s3_put_maps_an_oversized_preexisting_object_to_immutable_conflict() -> None:
    client = RecordingS3Client(
        objects={DATASET_KEY: b"larger-than-request"}, fail_put_with=precondition_failure()
    )
    store = S3ObjectStore(client=client, bucket="research-artifacts")

    with pytest.raises(ImmutableObjectConflict):
        store.put(DATASET_KEY, b"short", "application/octet-stream")

    assert client.get_calls == []


@pytest.mark.parametrize("failure_location", ["head", "get"])
def test_s3_put_closes_replay_client_errors_as_immutable_conflicts(
    failure_location: str,
) -> None:
    client = RecordingS3Client(
        objects={DATASET_KEY: b"same"},
        fail_put_with=precondition_failure(),
        fail_head_with=conditional_failure() if failure_location == "head" else None,
        fail_get_with=conditional_failure() if failure_location == "get" else None,
    )
    store = S3ObjectStore(client=client, bucket="research-artifacts")

    with pytest.raises(ImmutableObjectConflict) as error:
        store.put(DATASET_KEY, b"same", "application/octet-stream")

    assert "AWS internals" not in str(error.value)


def test_s3_get_bounds_before_read_and_verifies_returned_length() -> None:
    client = RecordingS3Client(objects={DATASET_KEY: b"oversized"})
    store = S3ObjectStore(client=client, bucket="research-artifacts")

    with pytest.raises(ObjectSizeLimitError):
        store.get(DATASET_KEY, maximum_bytes=8)
    with pytest.raises(ValueError):
        store.get(DATASET_KEY, maximum_bytes=-1)

    assert client.get_calls == []
    assert client.head_calls == [{"Bucket": "research-artifacts", "Key": DATASET_KEY}]


@pytest.mark.parametrize("operation", ["head", "get"])
def test_s3_get_translates_only_s3_not_found_conditions(
    operation: str,
) -> None:
    client = RecordingS3Client(
        objects={DATASET_KEY: b"safe"},
        fail_head_with=not_found_failure("HeadObject") if operation == "head" else None,
        fail_get_with=not_found_failure("GetObject") if operation == "get" else None,
    )
    store = S3ObjectStore(client=client, bucket="research-artifacts")

    with pytest.raises(ObjectNotFoundError) as error:
        store.get(DATASET_KEY, maximum_bytes=4)

    assert isinstance(error.value.__cause__, ClientError)


@pytest.mark.parametrize("operation", ["head", "get"])
def test_s3_get_preserves_non_not_found_client_errors(operation: str) -> None:
    client = RecordingS3Client(
        objects={DATASET_KEY: b"safe"},
        fail_head_with=service_failure("HeadObject") if operation == "head" else None,
        fail_get_with=service_failure("GetObject") if operation == "get" else None,
    )
    store = S3ObjectStore(client=client, bucket="research-artifacts")

    with pytest.raises(ClientError, match="retry later"):
        store.get(DATASET_KEY, maximum_bytes=4)


@pytest.mark.parametrize("value", [1.5, True])
def test_s3_get_rejects_non_integer_maximum_bytes_before_head(value: object) -> None:
    client = RecordingS3Client(objects={DATASET_KEY: b"safe"})
    store = S3ObjectStore(client=client, bucket="research-artifacts")

    with pytest.raises(ValueError):
        store.get(DATASET_KEY, maximum_bytes=value)  # type: ignore[arg-type]

    assert client.head_calls == []


def test_s3_head_and_presign_validate_fixed_key_and_five_minute_ceiling() -> None:
    client = RecordingS3Client(objects={DATASET_KEY: b"safe"})
    store = S3ObjectStore(client=client, bucket="research-artifacts")

    assert store.head(DATASET_KEY).byte_length == 4
    assert store.presign_get(DATASET_KEY, expires_seconds=300) == "https://signed.example/report"
    with pytest.raises(ValueError):
        store.presign_get(DATASET_KEY, expires_seconds=301)
    with pytest.raises(ValueError):
        store.head("untrusted/key")

    assert client.presign_calls == [
        ("get_object", {"Bucket": "research-artifacts", "Key": DATASET_KEY}, 300)
    ]


@pytest.mark.parametrize("value", [1.5, True])
def test_s3_presign_rejects_non_integer_expiries(value: object) -> None:
    client = RecordingS3Client()
    store = S3ObjectStore(client=client, bucket="research-artifacts")

    with pytest.raises(ValueError):
        store.presign_get(DATASET_KEY, expires_seconds=value)  # type: ignore[arg-type]

    assert client.presign_calls == []


def test_dynamo_create_pending_writes_derived_key_and_exact_condition() -> None:
    table = RecordingTable()
    repository = DynamoRunRepository(table=table)

    repository.create_pending(record())

    assert table.put_calls == [
        {
            "Item": {
                "PK": f"RUN#{RUN_ID}",
                "status": "PENDING",
                "visibility": "PRIVATE",
                "dataset_key": DATASET_KEY,
                "dataset_sha256": "a" * 64,
                "run_spec_key": f"runs/v1/{RUN_ID}/run-spec.json",
                "result_prefix": f"runs/v1/{RUN_ID}/",
                "image_digest": "sha256:" + "b" * 64,
                "created_at": "2024-03-29T12:00:00Z",
                "expires_at": 1_727_611_200,
            },
            "ConditionExpression": "attribute_not_exists(PK)",
        }
    ]


@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        (
            "running",
            {
                "UpdateExpression": "SET #status = :running, started_at = :started",
                "ConditionExpression": "#status = :pending",
                "ExpressionAttributeNames": {"#status": "status"},
                "ExpressionAttributeValues": {
                    ":pending": "PENDING",
                    ":running": "RUNNING",
                    ":started": "2024-03-29T12:00:00Z",
                },
            },
        ),
        (
            "succeeded",
            {
                "UpdateExpression": "SET #status = :succeeded, completed_at = :completed",
                "ConditionExpression": (
                    "#status = :running OR (#status = :succeeded AND completed_at = :completed)"
                ),
                "ExpressionAttributeNames": {"#status": "status"},
                "ExpressionAttributeValues": {
                    ":running": "RUNNING",
                    ":succeeded": "SUCCEEDED",
                    ":completed": "2024-03-29T12:00:00Z",
                },
            },
        ),
        (
            "failed",
            {
                "UpdateExpression": (
                    "SET #status = :failed, failure_code = :code, completed_at = :completed"
                ),
                "ConditionExpression": (
                    "#status IN (:pending, :running) OR "
                    "(#status = :failed AND failure_code = :code AND completed_at = :completed)"
                ),
                "ExpressionAttributeNames": {"#status": "status"},
                "ExpressionAttributeValues": {
                    ":pending": "PENDING",
                    ":running": "RUNNING",
                    ":failed": "FAILED",
                    ":code": "WORKER_FAILED",
                    ":completed": "2024-03-29T12:00:00Z",
                },
            },
        ),
    ],
)
def test_dynamo_state_updates_have_exact_conditions_and_values(
    operation: str, expected: dict[str, object]
) -> None:
    table = RecordingTable()
    repository = DynamoRunRepository(table=table)

    if operation == "running":
        repository.mark_running(RUN_ID, NOW)
    elif operation == "succeeded":
        repository.mark_succeeded(RUN_ID, NOW)
    else:
        repository.mark_failed(RUN_ID, FailureCode.WORKER_FAILED, NOW)

    assert table.update_calls == [{"Key": {"PK": f"RUN#{RUN_ID}"}, **expected}]


def test_dynamo_forbids_invalid_create_and_maps_conditional_conflicts_without_aws_details() -> None:
    table = RecordingTable(fail_with=conditional_failure())
    repository = DynamoRunRepository(table=table)

    with pytest.raises(ValueError):
        repository.create_pending(record(status=RunStatus.RUNNING, started_at=NOW))
    with pytest.raises(StateTransitionError) as error:
        repository.mark_running(RUN_ID, NOW)

    assert "AWS internals" not in str(error.value)


@pytest.mark.parametrize("transition", ["create", "running", "succeeded", "failed"])
def test_dynamo_maps_every_forbidden_conditional_transition(transition: str) -> None:
    repository = DynamoRunRepository(table=RecordingTable(fail_with=conditional_failure()))

    with pytest.raises(StateTransitionError) as error:
        if transition == "create":
            repository.create_pending(record())
        elif transition == "running":
            repository.mark_running(RUN_ID, NOW)
        elif transition == "succeeded":
            repository.mark_succeeded(RUN_ID, NOW)
        else:
            repository.mark_failed(RUN_ID, FailureCode.WORKER_FAILED, NOW)

    assert "AWS internals" not in str(error.value)


def test_dynamo_get_uses_consistent_read_and_reconstructs_contract() -> None:
    table = RecordingTable(
        item={
            "PK": f"RUN#{RUN_ID}",
            **record().model_dump(mode="json", exclude={"run_id"}),
        }
    )
    repository = DynamoRunRepository(table=table)

    assert repository.get(RUN_ID, consistent=True) == record()
    assert repository.get(RUN_ID, consistent=False) == record()
    assert table.get_calls == [
        {"Key": {"PK": f"RUN#{RUN_ID}"}, "ConsistentRead": True},
        {"Key": {"PK": f"RUN#{RUN_ID}"}, "ConsistentRead": False},
    ]


def test_offline_fakes_copy_records_and_allow_only_contract_transitions() -> None:
    objects = FakeObjectStore()
    objects.put(DATASET_KEY, b"safe", "application/octet-stream")

    assert objects.get(DATASET_KEY, maximum_bytes=4) == b"safe"
    assert objects.put_calls[0].body == b"safe"
    with pytest.raises(ImmutableObjectConflict):
        objects.put(DATASET_KEY, b"other", "application/octet-stream")

    repository = FakeRunRepository()
    pending = record()
    repository.create_pending(pending)
    assert repository.create_pending_calls[0].record == pending
    assert repository.create_pending_calls[0].record is not pending
    repository.mark_running(RUN_ID, NOW)
    repository.mark_succeeded(RUN_ID, NOW)
    repository.mark_succeeded(RUN_ID, NOW)
    with pytest.raises(StateTransitionError):
        repository.mark_failed(RUN_ID, FailureCode.WORKER_FAILED, NOW)

    failed = FakeRunRepository()
    failed.create_pending(record())
    failed.mark_failed(RUN_ID, FailureCode.WORKER_FAILED, NOW)
    failed.mark_failed(RUN_ID, FailureCode.WORKER_FAILED, NOW)
    with pytest.raises(StateTransitionError):
        failed.mark_succeeded(RUN_ID, NOW)
