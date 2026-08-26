"""Immutable in-memory cloud adapters for offline handler tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from src.cloud.contracts import FailureCode, RunRecord, RunStatus, sha256_hex
from src.cloud.storage import (
    ImmutableObjectConflict,
    LifecycleClass,
    ObjectNotFoundError,
    ObjectSizeLimitError,
    StateTransitionError,
    StoredObject,
    _copy_bytes,
    _validate_storage_key,
)


@dataclass(frozen=True, slots=True)
class PutCall:
    key: str
    body: bytes
    content_type: str
    lifecycle_class: LifecycleClass


@dataclass(frozen=True, slots=True)
class CreatePendingCall:
    record: RunRecord


@dataclass(frozen=True, slots=True)
class MarkRunningCall:
    run_id: str
    started_at: datetime


@dataclass(frozen=True, slots=True)
class MarkSucceededCall:
    run_id: str
    completed_at: datetime


@dataclass(frozen=True, slots=True)
class MarkFailedCall:
    run_id: str
    code: FailureCode
    completed_at: datetime


@dataclass(frozen=True, slots=True)
class GetCall:
    run_id: str
    consistent: bool


class FakeObjectStore:
    """Offline immutable object store with safely exposed call records."""

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}
        self._content_types: dict[str, str] = {}
        self._put_calls: list[PutCall] = []

    @property
    def put_calls(self) -> tuple[PutCall, ...]:
        return tuple(
            PutCall(
                call.key,
                _copy_bytes(call.body),
                call.content_type,
                call.lifecycle_class,
            )
            for call in self._put_calls
        )

    def put(
        self,
        key: str,
        body: bytes,
        content_type: str,
        *,
        lifecycle_class: LifecycleClass = LifecycleClass.TRANSIENT,
    ) -> StoredObject:
        admitted_key = _validate_storage_key(key)
        copied_body = _copy_bytes(body)
        if not isinstance(content_type, str) or not content_type:
            raise ValueError("content_type must be a non-empty string")
        if not isinstance(lifecycle_class, LifecycleClass):
            raise TypeError("lifecycle_class must be a LifecycleClass")
        existing = self._objects.get(admitted_key)
        if existing is not None and existing != copied_body:
            raise ImmutableObjectConflict("immutable object key contains different content")
        self._objects.setdefault(admitted_key, copied_body)
        self._content_types.setdefault(admitted_key, content_type)
        self._put_calls.append(
            PutCall(admitted_key, _copy_bytes(copied_body), content_type, lifecycle_class)
        )
        return StoredObject(admitted_key, len(copied_body), sha256_hex(copied_body))

    def get(self, key: str, maximum_bytes: int) -> bytes:
        if isinstance(maximum_bytes, bool) or maximum_bytes < 0:
            raise ValueError("maximum_bytes must be a non-negative integer")
        stored = self.head(key)
        if stored.byte_length > maximum_bytes:
            raise ObjectSizeLimitError("object exceeds the requested download limit")
        return _copy_bytes(self._objects[stored.key])

    def head(self, key: str) -> StoredObject:
        admitted_key = _validate_storage_key(key)
        try:
            body = self._objects[admitted_key]
        except KeyError as error:
            raise ObjectNotFoundError("immutable object was not found") from error
        return StoredObject(admitted_key, len(body), sha256_hex(body))

    def presign_get(self, key: str, expires_seconds: int) -> str:
        admitted_key = _validate_storage_key(key)
        if isinstance(expires_seconds, bool) or not 0 < expires_seconds <= 300:
            raise ValueError("expires_seconds must be between 1 and 300")
        return f"https://fake.invalid/{admitted_key}?expires={expires_seconds}"


class FakeRunRepository:
    """Offline conditional run-state repository with immutable record copies."""

    def __init__(self) -> None:
        self._records: dict[str, RunRecord] = {}
        self._create_pending_calls: list[CreatePendingCall] = []
        self._mark_running_calls: list[MarkRunningCall] = []
        self._mark_succeeded_calls: list[MarkSucceededCall] = []
        self._mark_failed_calls: list[MarkFailedCall] = []
        self._get_calls: list[GetCall] = []

    @property
    def create_pending_calls(self) -> tuple[CreatePendingCall, ...]:
        return tuple(
            CreatePendingCall(_copy_record(call.record)) for call in self._create_pending_calls
        )

    @property
    def mark_running_calls(self) -> tuple[MarkRunningCall, ...]:
        return tuple(self._mark_running_calls)

    @property
    def mark_succeeded_calls(self) -> tuple[MarkSucceededCall, ...]:
        return tuple(self._mark_succeeded_calls)

    @property
    def mark_failed_calls(self) -> tuple[MarkFailedCall, ...]:
        return tuple(self._mark_failed_calls)

    @property
    def get_calls(self) -> tuple[GetCall, ...]:
        return tuple(self._get_calls)

    def create_pending(self, record: RunRecord) -> None:
        copied = _copy_record(record)
        if copied.status is not RunStatus.PENDING:
            raise ValueError("only PENDING run records may be created")
        if copied.run_id in self._records:
            raise StateTransitionError("run state transition rejected")
        self._records[copied.run_id] = copied
        self._create_pending_calls.append(CreatePendingCall(_copy_record(copied)))

    def mark_running(self, run_id: str, started_at: datetime) -> None:
        record = self._require(run_id)
        if record.status is not RunStatus.PENDING:
            raise StateTransitionError("run state transition rejected")
        updated = _updated_record(record, status=RunStatus.RUNNING, started_at=started_at)
        self._records[run_id] = _copy_record(updated)
        self._mark_running_calls.append(MarkRunningCall(run_id, started_at))

    def mark_succeeded(self, run_id: str, completed_at: datetime) -> None:
        record = self._require(run_id)
        is_replay = record.status is RunStatus.SUCCEEDED and record.completed_at == completed_at
        if record.status is not RunStatus.RUNNING and not is_replay:
            raise StateTransitionError("run state transition rejected")
        updated = _updated_record(record, status=RunStatus.SUCCEEDED, completed_at=completed_at)
        self._records[run_id] = _copy_record(updated)
        self._mark_succeeded_calls.append(MarkSucceededCall(run_id, completed_at))

    def mark_failed(self, run_id: str, code: FailureCode, completed_at: datetime) -> None:
        record = self._require(run_id)
        is_replay = (
            record.status is RunStatus.FAILED
            and record.failure_code is code
            and record.completed_at == completed_at
        )
        if record.status not in {RunStatus.PENDING, RunStatus.RUNNING} and not is_replay:
            raise StateTransitionError("run state transition rejected")
        updated = _updated_record(
            record,
            status=RunStatus.FAILED,
            failure_code=code,
            completed_at=completed_at,
        )
        self._records[run_id] = _copy_record(updated)
        self._mark_failed_calls.append(MarkFailedCall(run_id, code, completed_at))

    def get(self, run_id: str, consistent: bool = False) -> RunRecord | None:
        self._get_calls.append(GetCall(run_id, consistent))
        record = self._records.get(run_id)
        return None if record is None else _copy_record(record)

    def _require(self, run_id: str) -> RunRecord:
        record = self._records.get(run_id)
        if record is None:
            raise StateTransitionError("run state transition rejected")
        return record


def _copy_record(record: RunRecord) -> RunRecord:
    return RunRecord.model_validate(record.model_dump(mode="python"))


def _updated_record(record: RunRecord, **changes: object) -> RunRecord:
    values = record.model_dump(mode="python")
    values.update(changes)
    return RunRecord.model_validate(values)
