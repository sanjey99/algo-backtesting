"""Injectable, offline-safe S3 artifact and DynamoDB run-state adapters."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from botocore.exceptions import ClientError

from src.cloud.contracts import (
    FailureCode,
    RunRecord,
    RunStatus,
    _parse_utc_datetime,
    _validate_bucket,
    _validate_object_key,
    _validate_sha256,
    _validate_uuid,
    sha256_hex,
)

_KEY_PREFIXES = ("datasets/v1/", "runs/v1/")
_MAXIMUM_PRESIGN_SECONDS = 300


class StorageError(Exception):
    """Base class for closed storage-boundary errors."""


class ImmutableObjectConflict(StorageError):  # noqa: N818
    """An immutable object key already contains different content."""


class ObjectSizeLimitError(StorageError):
    """An object exceeds its caller-provided download limit."""


class ObjectIntegrityError(StorageError):
    """An object response does not match its immutable metadata."""


class StateTransitionError(StorageError):
    """A DynamoDB conditional state transition was rejected."""


@dataclass(frozen=True, slots=True)
class StoredObject:
    """Verified immutable object metadata."""

    key: str
    byte_length: int
    sha256: str

    def __post_init__(self) -> None:
        _validate_storage_key(self.key)
        if isinstance(self.byte_length, bool) or self.byte_length < 0:
            raise ValueError("byte_length must be a non-negative integer")
        _validate_sha256(self.sha256)


class ObjectStore(Protocol):
    """Immutable object storage required by cloud workflow handlers."""

    def put(self, key: str, body: bytes, content_type: str) -> StoredObject: ...

    def get(self, key: str, maximum_bytes: int) -> bytes: ...

    def head(self, key: str) -> StoredObject: ...

    def presign_get(self, key: str, expires_seconds: int) -> str: ...


class RunRepository(Protocol):
    """Workflow run-state persistence required by cloud workflow handlers."""

    def create_pending(self, record: RunRecord) -> None: ...

    def mark_running(self, run_id: str, started_at: datetime) -> None: ...

    def mark_succeeded(self, run_id: str, completed_at: datetime) -> None: ...

    def mark_failed(self, run_id: str, code: FailureCode, completed_at: datetime) -> None: ...

    def get(self, run_id: str, consistent: bool = False) -> RunRecord | None: ...


class _S3Client(Protocol):
    def put_object(self, **kwargs: object) -> object: ...

    def head_object(self, **kwargs: object) -> Mapping[str, object]: ...

    def get_object(self, **kwargs: object) -> Mapping[str, object]: ...

    def generate_presigned_url(self, client_method: str, **kwargs: object) -> str: ...


class _DynamoTable(Protocol):
    def put_item(self, **kwargs: object) -> object: ...

    def update_item(self, **kwargs: object) -> object: ...

    def get_item(self, **kwargs: object) -> Mapping[str, object]: ...


@runtime_checkable
class _ReadableBody(Protocol):
    def read(self) -> bytes: ...


def _validate_storage_key(key: str) -> str:
    """Validate one of the closed artifact key namespaces using contract grammar."""
    if not isinstance(key, str):
        raise TypeError("object key must be a string")
    for prefix in _KEY_PREFIXES:
        if key.startswith(prefix):
            return _validate_object_key(key, prefix=prefix)
    raise ValueError("object key must be under a supported immutable artifact prefix")


def _copy_bytes(value: bytes) -> bytes:
    if not isinstance(value, bytes):
        raise TypeError("object body must be bytes")
    return memoryview(value).tobytes()


def _response_mapping(value: object, *, operation: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ObjectIntegrityError(f"{operation} returned an invalid storage response")
    return value


def _utc_text(value: datetime) -> str:
    admitted = _parse_utc_datetime(value)
    return admitted.isoformat().replace("+00:00", "Z")


def _run_key(run_id: str) -> dict[str, str]:
    return {"PK": f"RUN#{_validate_uuid(run_id)}"}


def _is_conditional_failure(error: ClientError) -> bool:
    response = error.response
    details = response.get("Error")
    return isinstance(details, Mapping) and details.get("Code") == "ConditionalCheckFailedException"


class S3ObjectStore:
    """S3 object store with fixed bucket, immutable writes, and bounded reads."""

    def __init__(self, *, client: _S3Client, bucket: str) -> None:
        self._client = client
        self._bucket = _validate_bucket(bucket)

    def put(self, key: str, body: bytes, content_type: str) -> StoredObject:
        admitted_key = _validate_storage_key(key)
        copied_body = _copy_bytes(body)
        if not isinstance(content_type, str) or not content_type:
            raise ValueError("content_type must be a non-empty string")
        stored = StoredObject(
            key=admitted_key,
            byte_length=len(copied_body),
            sha256=sha256_hex(copied_body),
        )
        try:
            self._client.put_object(
                Bucket=self._bucket,
                Key=admitted_key,
                Body=copied_body,
                ContentType=content_type,
                ServerSideEncryption="AES256",
                Metadata={"sha256": stored.sha256},
                IfNoneMatch="*",
            )
        except ClientError as error:
            if not _is_precondition_failed(error):
                raise
            try:
                existing = self.get(admitted_key, maximum_bytes=len(copied_body))
            except (ClientError, StorageError):
                raise ImmutableObjectConflict(
                    "immutable object key cannot be verified as byte-identical"
                ) from None
            if sha256_hex(existing) != stored.sha256:
                raise ImmutableObjectConflict(
                    "immutable object key contains different content"
                ) from None
        return stored

    def get(self, key: str, maximum_bytes: int) -> bytes:
        admitted_key = _validate_storage_key(key)
        if (
            isinstance(maximum_bytes, bool)
            or not isinstance(maximum_bytes, int)
            or maximum_bytes < 0
        ):
            raise ValueError("maximum_bytes must be a non-negative integer")
        stored = self.head(admitted_key)
        if stored.byte_length > maximum_bytes:
            raise ObjectSizeLimitError("object exceeds the requested download limit")
        response = _response_mapping(
            self._client.get_object(Bucket=self._bucket, Key=admitted_key), operation="get_object"
        )
        raw_body = response.get("Body")
        if not isinstance(raw_body, _ReadableBody):
            raise ObjectIntegrityError("get_object returned a non-readable body")
        body = raw_body.read()
        if not isinstance(body, bytes):
            raise ObjectIntegrityError("get_object returned a non-bytes body")
        copied_body = _copy_bytes(body)
        if len(copied_body) != stored.byte_length or sha256_hex(copied_body) != stored.sha256:
            raise ObjectIntegrityError("object body does not match immutable metadata")
        return copied_body

    def head(self, key: str) -> StoredObject:
        admitted_key = _validate_storage_key(key)
        response = _response_mapping(
            self._client.head_object(Bucket=self._bucket, Key=admitted_key), operation="head_object"
        )
        byte_length = response.get("ContentLength")
        if isinstance(byte_length, bool) or not isinstance(byte_length, int) or byte_length < 0:
            raise ObjectIntegrityError("head_object returned an invalid content length")
        metadata = response.get("Metadata")
        if not isinstance(metadata, Mapping):
            raise ObjectIntegrityError("head_object returned no immutable digest metadata")
        digest = metadata.get("sha256")
        try:
            if not isinstance(digest, str):
                raise ValueError("missing digest")
            return StoredObject(key=admitted_key, byte_length=byte_length, sha256=digest)
        except ValueError as error:
            raise ObjectIntegrityError(
                "head_object returned invalid immutable digest metadata"
            ) from error

    def presign_get(self, key: str, expires_seconds: int) -> str:
        admitted_key = _validate_storage_key(key)
        if (
            isinstance(expires_seconds, bool)
            or not isinstance(expires_seconds, int)
            or not 0 < expires_seconds <= _MAXIMUM_PRESIGN_SECONDS
        ):
            raise ValueError("expires_seconds must be between 1 and 300")
        return self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._bucket, "Key": admitted_key},
            ExpiresIn=expires_seconds,
        )


def _is_precondition_failed(error: ClientError) -> bool:
    response = error.response
    details = response.get("Error")
    return isinstance(details, Mapping) and details.get("Code") == "PreconditionFailed"


class DynamoRunRepository:
    """DynamoDB run-state adapter with closed conditional transitions."""

    def __init__(self, *, table: _DynamoTable) -> None:
        self._table = table

    def create_pending(self, record: RunRecord) -> None:
        if record.status is not RunStatus.PENDING:
            raise ValueError("only PENDING run records may be created")
        item = record.model_dump(mode="json")
        item.pop("run_id")
        item = {"PK": _run_key(record.run_id)["PK"], **item}
        try:
            self._table.put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
        except ClientError as error:
            self._raise_transition_error(error)

    def mark_running(self, run_id: str, started_at: datetime) -> None:
        self._update(
            run_id,
            update_expression="SET #status = :running, started_at = :started",
            condition_expression="#status = :pending",
            values={
                ":pending": RunStatus.PENDING.value,
                ":running": RunStatus.RUNNING.value,
                ":started": _utc_text(started_at),
            },
        )

    def mark_succeeded(self, run_id: str, completed_at: datetime) -> None:
        self._update(
            run_id,
            update_expression="SET #status = :succeeded, completed_at = :completed",
            condition_expression=(
                "#status = :running OR (#status = :succeeded AND completed_at = :completed)"
            ),
            values={
                ":running": RunStatus.RUNNING.value,
                ":succeeded": RunStatus.SUCCEEDED.value,
                ":completed": _utc_text(completed_at),
            },
        )

    def mark_failed(self, run_id: str, code: FailureCode, completed_at: datetime) -> None:
        if not isinstance(code, FailureCode):
            raise TypeError("code must be a FailureCode")
        self._update(
            run_id,
            update_expression=(
                "SET #status = :failed, failure_code = :code, completed_at = :completed"
            ),
            condition_expression=(
                "#status IN (:pending, :running) OR "
                "(#status = :failed AND failure_code = :code AND completed_at = :completed)"
            ),
            values={
                ":pending": RunStatus.PENDING.value,
                ":running": RunStatus.RUNNING.value,
                ":failed": RunStatus.FAILED.value,
                ":code": code.value,
                ":completed": _utc_text(completed_at),
            },
        )

    def get(self, run_id: str, consistent: bool = False) -> RunRecord | None:
        key = _run_key(run_id)
        response = _response_mapping(
            self._table.get_item(Key=key, ConsistentRead=consistent), operation="get_item"
        )
        item = response.get("Item")
        if item is None:
            return None
        if not isinstance(item, Mapping):
            raise StateTransitionError("run record response is invalid")
        values = dict(item)
        if values.pop("PK", None) != key["PK"]:
            raise StateTransitionError("run record key is invalid")
        values["run_id"] = run_id
        return RunRecord.model_validate(values)

    def _update(
        self,
        run_id: str,
        *,
        update_expression: str,
        condition_expression: str,
        values: Mapping[str, str],
    ) -> None:
        try:
            self._table.update_item(
                Key=_run_key(run_id),
                UpdateExpression=update_expression,
                ConditionExpression=condition_expression,
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues=dict(values),
            )
        except ClientError as error:
            self._raise_transition_error(error)

    @staticmethod
    def _raise_transition_error(error: ClientError) -> None:
        if _is_conditional_failure(error):
            raise StateTransitionError("run state transition rejected") from None
        raise error
