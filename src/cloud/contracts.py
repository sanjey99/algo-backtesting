"""Immutable, offline-safe admission contracts for cloud research runs."""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from datetime import UTC, date, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.strategies import STRATEGY_REGISTRY

SCHEMA_VERSION = "1"
REQUIRED_ARTIFACTS = frozenset(
    {
        "run-spec.json",
        "result.json",
        "trades.parquet",
        "equity-curve.parquet",
        "report.html",
    }
)

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9._-]{0,31}$")
_CONTROL_CHARACTER_PATTERN = re.compile(r"[\x00-\x1f\x7f]")
_BUCKET_PATTERN = re.compile(r"^(?=.{3,63}$)[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")


class Visibility(StrEnum):
    PRIVATE = "PRIVATE"
    PUBLIC = "PUBLIC"


class RunStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class FailureCode(StrEnum):
    ACQUISITION_FAILED = "ACQUISITION_FAILED"
    PREPARATION_FAILED = "PREPARATION_FAILED"
    WORKER_FAILED = "WORKER_FAILED"
    ARTIFACT_VERIFICATION_FAILED = "ARTIFACT_VERIFICATION_FAILED"
    WORKFLOW_TIMED_OUT = "WORKFLOW_TIMED_OUT"


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def _parse_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value)
    raise TypeError("must be an ISO date")


def _parse_utc_datetime(value: object) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != UTC.utcoffset(value)
    ):
        raise ValueError("must be a UTC timestamp")
    return value.astimezone(UTC)


def _validate_sha256(value: object) -> str:
    if not isinstance(value, str) or not _SHA256_PATTERN.fullmatch(value):
        raise ValueError("must be a 64-character lowercase SHA-256 hexadecimal value")
    return value


def _validate_image_digest(value: object) -> str:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        raise ValueError("must be a pinned sha256 image digest")
    _validate_sha256(value.removeprefix("sha256:"))
    return value


def _validate_uuid(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("must be a canonical UUID string")
    parsed = UUID(value)
    if str(parsed) != value:
        raise ValueError("must be a canonical UUID string")
    return value


def _validate_object_key(value: object, *, prefix: str, allow_trailing_slash: bool = False) -> str:
    if not isinstance(value, str) or not value.startswith(prefix) or len(value) > 1024:
        raise ValueError(f"must be a {prefix!r} object key no longer than 1024 characters")
    if "\\" in value or _CONTROL_CHARACTER_PATTERN.search(value):
        raise ValueError("must not contain backslashes or control characters")
    components = value.split("/")
    if allow_trailing_slash and components[-1] == "":
        components = components[:-1]
    if not components or any(component in {"", ".", ".."} for component in components):
        raise ValueError("must not contain empty or dot path components")
    return value


def _validate_symbol(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("symbol must be a string")
    symbol = value.strip().upper()
    if not _SYMBOL_PATTERN.fullmatch(symbol):
        raise ValueError("symbol must use the safe V1 symbol grammar")
    return symbol


def _validate_bucket(value: object) -> str:
    if not isinstance(value, str) or not _BUCKET_PATTERN.fullmatch(value):
        raise ValueError("bucket must use the safe S3 bucket grammar")
    return value


def _ten_years_after(start: date) -> date:
    try:
        return start.replace(year=start.year + 10)
    except ValueError:
        return start.replace(year=start.year + 10, day=28)


class ResearchRequest(_Contract):
    schema_version: str = SCHEMA_VERSION
    symbol: str
    start: date
    end: date
    strategy_key: str
    strategy_parameters: Mapping[str, int | float] = Field(default_factory=dict)
    initial_capital: float = 100_000.0
    commission_pct: float = 0.001
    slippage_pct: float = 0.0005
    visibility: Visibility = Visibility.PRIVATE

    @field_validator("schema_version")
    @classmethod
    def _validate_schema_version(cls, value: str) -> str:
        if value != SCHEMA_VERSION:
            raise ValueError("unsupported schema version")
        return value

    @field_validator("symbol", mode="before")
    @classmethod
    def _admit_symbol(cls, value: object) -> str:
        return _validate_symbol(value)

    @field_validator("visibility", mode="before")
    @classmethod
    def _admit_visibility(cls, value: object) -> Visibility:
        if not isinstance(value, str):
            raise TypeError("visibility must be a string")
        return Visibility(value)

    @field_validator("start", "end", mode="before")
    @classmethod
    def _admit_dates(cls, value: object) -> date:
        return _parse_date(value)

    @field_validator("strategy_parameters")
    @classmethod
    def _admit_parameters(cls, value: Mapping[str, int | float]) -> Mapping[str, int | float]:
        if len(value) > 16:
            raise ValueError("at most 16 strategy parameters are permitted")
        admitted: dict[str, int | float] = {}
        for key, parameter in value.items():
            if not isinstance(key, str) or not key:
                raise ValueError("strategy parameter names must be non-empty strings")
            if isinstance(parameter, bool) or not isinstance(parameter, int | float):
                raise ValueError("strategy parameter values must be finite scalar numbers")
            if not math.isfinite(parameter):
                raise ValueError("strategy parameter values must be finite")
            admitted[key] = parameter
        return MappingProxyType(admitted)

    @field_validator("initial_capital", "commission_pct", "slippage_pct")
    @classmethod
    def _admit_finite_values(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("must be finite")
        return value

    @model_validator(mode="after")
    def _admit_request(self) -> Self:
        if self.start > self.end:
            raise ValueError("start must not be after end")
        if self.end > _ten_years_after(self.start):
            raise ValueError("date range must not exceed ten inclusive years")
        if self.initial_capital <= 0:
            raise ValueError("initial_capital must be positive")
        if self.commission_pct < 0 or self.slippage_pct < 0:
            raise ValueError("commission_pct and slippage_pct must be non-negative")
        strategy = STRATEGY_REGISTRY.get(self.strategy_key)
        if strategy is None:
            raise ValueError("unsupported strategy_key")
        try:
            strategy(**dict(self.strategy_parameters))
        except (TypeError, ValueError) as error:
            raise ValueError("unsupported strategy parameters") from error
        return self


class DatasetRef(_Contract):
    schema_version: str = SCHEMA_VERSION
    bucket: str
    key: str
    sha256: str
    manifest_key: str
    manifest_sha256: str
    symbol: str
    calendar: str
    interval: str
    start: date
    end: date
    acquisition_id: str
    completed_at: datetime

    @field_validator("schema_version")
    @classmethod
    def _validate_schema_version(cls, value: str) -> str:
        if value != SCHEMA_VERSION:
            raise ValueError("unsupported schema version")
        return value

    @field_validator("bucket", mode="before")
    @classmethod
    def _admit_bucket(cls, value: object) -> str:
        return _validate_bucket(value)

    @field_validator("key", "manifest_key", mode="before")
    @classmethod
    def _admit_dataset_keys(cls, value: object) -> str:
        return _validate_object_key(value, prefix="datasets/v1/")

    @field_validator("sha256", "manifest_sha256", mode="before")
    @classmethod
    def _admit_hashes(cls, value: object) -> str:
        return _validate_sha256(value)

    @field_validator("symbol", mode="before")
    @classmethod
    def _admit_symbol(cls, value: object) -> str:
        return _validate_symbol(value)

    @field_validator("start", "end", mode="before")
    @classmethod
    def _admit_dates(cls, value: object) -> date:
        return _parse_date(value)

    @field_validator("completed_at", mode="before")
    @classmethod
    def _admit_completed_at(cls, value: object) -> datetime:
        return _parse_utc_datetime(value)

    @model_validator(mode="after")
    def _validate_range(self) -> Self:
        if self.start > self.end:
            raise ValueError("start must not be after end")
        return self


class RunSpec(_Contract):
    schema_version: str = SCHEMA_VERSION
    run_id: str
    dataset: DatasetRef
    request: ResearchRequest
    image_digest: str
    created_at: datetime
    maximum_runtime_seconds: int = 600
    run_spec_key: str
    result_prefix: str

    @classmethod
    def create(
        cls,
        *,
        request: ResearchRequest,
        dataset: DatasetRef,
        image_digest: str,
        now: datetime,
        run_id: UUID,
        maximum_runtime_seconds: int = 600,
    ) -> RunSpec:
        run_id_text = str(run_id)
        return cls(
            run_id=run_id_text,
            dataset=dataset,
            request=request,
            image_digest=image_digest,
            created_at=now,
            maximum_runtime_seconds=maximum_runtime_seconds,
            run_spec_key=f"runs/v1/{run_id_text}/run-spec.json",
            result_prefix=f"runs/v1/{run_id_text}/",
        )

    @field_validator("schema_version")
    @classmethod
    def _validate_schema_version(cls, value: str) -> str:
        if value != SCHEMA_VERSION:
            raise ValueError("unsupported schema version")
        return value

    @field_validator("run_id", mode="before")
    @classmethod
    def _admit_run_id(cls, value: object) -> str:
        return _validate_uuid(value)

    @field_validator("image_digest", mode="before")
    @classmethod
    def _admit_image_digest(cls, value: object) -> str:
        return _validate_image_digest(value)

    @field_validator("created_at", mode="before")
    @classmethod
    def _admit_created_at(cls, value: object) -> datetime:
        return _parse_utc_datetime(value)

    @field_validator("run_spec_key", mode="before")
    @classmethod
    def _admit_run_spec_key(cls, value: object) -> str:
        return _validate_object_key(value, prefix="runs/v1/")

    @field_validator("result_prefix", mode="before")
    @classmethod
    def _admit_result_prefix(cls, value: object) -> str:
        return _validate_object_key(value, prefix="runs/v1/", allow_trailing_slash=True)

    @model_validator(mode="after")
    def _validate_derived_values(self) -> Self:
        if self.maximum_runtime_seconds <= 0:
            raise ValueError("maximum_runtime_seconds must be positive")
        expected_prefix = f"runs/v1/{self.run_id}/"
        if (
            self.result_prefix != expected_prefix
            or self.run_spec_key != f"{expected_prefix}run-spec.json"
        ):
            raise ValueError("run output locations must be derived from run_id")
        return self


class ArtifactDigest(_Contract):
    name: str
    byte_length: int
    sha256: str

    @field_validator("name")
    @classmethod
    def _admit_name(cls, value: str) -> str:
        if value not in REQUIRED_ARTIFACTS:
            raise ValueError("artifact name is not required")
        return value

    @field_validator("byte_length")
    @classmethod
    def _admit_byte_length(cls, value: int) -> int:
        if value < 0:
            raise ValueError("byte_length must be non-negative")
        return value

    @field_validator("sha256", mode="before")
    @classmethod
    def _admit_sha256(cls, value: object) -> str:
        return _validate_sha256(value)


class ChecksumsManifest(_Contract):
    schema_version: str = SCHEMA_VERSION
    artifacts: tuple[ArtifactDigest, ...]

    @field_validator("schema_version")
    @classmethod
    def _validate_schema_version(cls, value: str) -> str:
        if value != SCHEMA_VERSION:
            raise ValueError("unsupported schema version")
        return value

    @field_validator("artifacts", mode="before")
    @classmethod
    def _admit_artifacts(cls, value: object) -> tuple[object, ...]:
        if not isinstance(value, list | tuple):
            raise TypeError("artifacts must be an array")
        return tuple(value)

    @model_validator(mode="after")
    def _require_exact_artifacts(self) -> Self:
        names = {artifact.name for artifact in self.artifacts}
        if len(names) != len(self.artifacts) or names != REQUIRED_ARTIFACTS:
            raise ValueError("checksums must contain every required artifact exactly once")
        return self


class RunRecord(_Contract):
    run_id: str
    status: RunStatus
    visibility: Visibility
    dataset_key: str
    dataset_sha256: str
    run_spec_key: str
    result_prefix: str
    image_digest: str
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    failure_code: FailureCode | None = None
    expires_at: int

    @field_validator("run_id", mode="before")
    @classmethod
    def _admit_run_id(cls, value: object) -> str:
        return _validate_uuid(value)

    @field_validator("status", mode="before")
    @classmethod
    def _admit_status(cls, value: object) -> RunStatus:
        if not isinstance(value, str):
            raise TypeError("status must be a string")
        return RunStatus(value)

    @field_validator("visibility", mode="before")
    @classmethod
    def _admit_visibility(cls, value: object) -> Visibility:
        if not isinstance(value, str):
            raise TypeError("visibility must be a string")
        return Visibility(value)

    @field_validator("failure_code", mode="before")
    @classmethod
    def _admit_failure_code(cls, value: object) -> FailureCode | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise TypeError("failure_code must be a string")
        return FailureCode(value)

    @field_validator("dataset_key", mode="before")
    @classmethod
    def _admit_dataset_key(cls, value: object) -> str:
        return _validate_object_key(value, prefix="datasets/v1/")

    @field_validator("dataset_sha256", mode="before")
    @classmethod
    def _admit_dataset_sha256(cls, value: object) -> str:
        return _validate_sha256(value)

    @field_validator("run_spec_key", mode="before")
    @classmethod
    def _admit_run_spec_key(cls, value: object) -> str:
        return _validate_object_key(value, prefix="runs/v1/")

    @field_validator("result_prefix", mode="before")
    @classmethod
    def _admit_result_prefix(cls, value: object) -> str:
        return _validate_object_key(value, prefix="runs/v1/", allow_trailing_slash=True)

    @field_validator("image_digest", mode="before")
    @classmethod
    def _admit_image_digest(cls, value: object) -> str:
        return _validate_image_digest(value)

    @field_validator("created_at", "started_at", "completed_at", mode="before")
    @classmethod
    def _admit_timestamps(cls, value: object) -> datetime | None:
        if value is None:
            return None
        return _parse_utc_datetime(value)

    @model_validator(mode="after")
    def _validate_record(self) -> Self:
        if self.expires_at < 0:
            raise ValueError("expires_at must be a non-negative epoch second")
        expected_prefix = f"runs/v1/{self.run_id}/"
        if (
            self.result_prefix != expected_prefix
            or self.run_spec_key != f"{expected_prefix}run-spec.json"
        ):
            raise ValueError("run output locations must be derived from run_id")
        if self.status is RunStatus.FAILED and self.failure_code is None:
            raise ValueError("failed records require a failure_code")
        if self.status is not RunStatus.FAILED and self.failure_code is not None:
            raise ValueError("only failed records may contain a failure_code")
        return self


def canonical_json_bytes(value: BaseModel | Mapping[str, object]) -> bytes:
    """Serialize a contract or mapping as deterministic standards-compliant JSON."""
    payload: object = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    return json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def sha256_hex(value: bytes) -> str:
    """Return the lowercase SHA-256 digest for immutable bytes."""
    return hashlib.sha256(value).hexdigest()
