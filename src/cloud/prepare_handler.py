"""Prepare one immutable backtest run and conditionally record its pending state."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, model_validator

from src.cloud.contracts import (
    DatasetRef,
    ResearchRequest,
    RunRecord,
    RunSpec,
    RunStatus,
    Visibility,
    _parse_utc_datetime,
    _validate_bucket,
    _validate_image_digest,
    canonical_json_bytes,
)
from src.cloud.storage import (
    DynamoRunRepository,
    LifecycleClass,
    ObjectStore,
    RunRepository,
    StateTransitionError,
)
from src.observability import log_event

logger = logging.getLogger(__name__)

_DEFAULT_TTL_DAYS = 45

Clock = Callable[[], datetime]
UUIDFactory = Callable[[], UUID]


class RunPreparationError(Exception):
    """A run specification or its pending metadata could not be prepared safely."""


class _PreparationEvent(BaseModel):
    """The complete, closed hand-off shape emitted by ingestion."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    request: ResearchRequest
    dataset: DatasetRef

    @model_validator(mode="after")
    def _closed_shape(self) -> Self:
        return self


@dataclass(frozen=True, slots=True)
class PreparedRun:
    """Derived locations that the workflow needs to begin and finalize a run."""

    run_id: str
    run_spec_key: str

    def to_dict(self) -> dict[str, str]:
        return {
            "run_id": self.run_id,
            "run_spec_key": self.run_spec_key,
        }


def _validate_ttl_days(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("ttl_days must be a positive integer")
    return value


def _read_now(clock: Clock) -> datetime:
    return _parse_utc_datetime(clock())


def _request_payload(request: ResearchRequest) -> dict[str, object]:
    return {
        "schema_version": request.schema_version,
        "symbol": request.symbol,
        "start": request.start.isoformat(),
        "end": request.end.isoformat(),
        "strategy_key": request.strategy_key,
        "strategy_parameters": dict(request.strategy_parameters),
        "initial_capital": request.initial_capital,
        "commission_pct": request.commission_pct,
        "slippage_pct": request.slippage_pct,
        "visibility": request.visibility.value,
    }


def _dataset_payload(dataset: DatasetRef) -> dict[str, object]:
    return {
        "schema_version": dataset.schema_version,
        "bucket": dataset.bucket,
        "key": dataset.key,
        "sha256": dataset.sha256,
        "manifest_key": dataset.manifest_key,
        "manifest_sha256": dataset.manifest_sha256,
        "symbol": dataset.symbol,
        "calendar": dataset.calendar,
        "interval": dataset.interval,
        "start": dataset.start.isoformat(),
        "end": dataset.end.isoformat(),
        "acquisition_id": dataset.acquisition_id,
        "completed_at": dataset.completed_at.isoformat().replace("+00:00", "Z"),
    }


def _run_spec_payload(run_spec: RunSpec) -> dict[str, object]:
    return {
        "schema_version": run_spec.schema_version,
        "run_id": run_spec.run_id,
        "dataset": _dataset_payload(run_spec.dataset),
        "request": _request_payload(run_spec.request),
        "image_digest": run_spec.image_digest,
        "created_at": run_spec.created_at.isoformat().replace("+00:00", "Z"),
        "maximum_runtime_seconds": run_spec.maximum_runtime_seconds,
        "run_spec_key": run_spec.run_spec_key,
        "result_prefix": run_spec.result_prefix,
    }


def _log_failure(run_id: str) -> None:
    log_event(logger, logging.ERROR, "cloud.run.preparation.failed", run_id=run_id)


def prepare_run(
    event: object,
    *,
    object_store: ObjectStore,
    run_repository: RunRepository,
    image_digest: str,
    clock: Clock,
    uuid_factory: UUIDFactory,
    ttl_days: int = _DEFAULT_TTL_DAYS,
) -> PreparedRun:
    """Publish an immutable run specification before conditionally creating its metadata."""
    admitted_event = _PreparationEvent.model_validate(event)
    request = ResearchRequest.model_validate(_request_payload(admitted_event.request))
    dataset = DatasetRef.model_validate(_dataset_payload(admitted_event.dataset))
    admitted_ttl_days = _validate_ttl_days(ttl_days)
    now = _read_now(clock)
    run_spec = RunSpec.create(
        request=request,
        dataset=dataset,
        image_digest=image_digest,
        now=now,
        run_id=uuid_factory(),
    )
    run_record = RunRecord(
        run_id=run_spec.run_id,
        status=RunStatus.PENDING,
        visibility=request.visibility,
        dataset_key=dataset.key,
        dataset_sha256=dataset.sha256,
        run_spec_key=run_spec.run_spec_key,
        result_prefix=run_spec.result_prefix,
        image_digest=run_spec.image_digest,
        created_at=run_spec.created_at,
        expires_at=int((now + timedelta(days=admitted_ttl_days)).timestamp()),
    )

    try:
        lifecycle_class = (
            LifecycleClass.SELECTED_PUBLIC
            if request.visibility is Visibility.PUBLIC
            else LifecycleClass.TRANSIENT
        )
        object_store.put(
            run_spec.run_spec_key,
            canonical_json_bytes(_run_spec_payload(run_spec)),
            "application/json",
            lifecycle_class=lifecycle_class,
        )
    except Exception:
        _log_failure(run_spec.run_id)
        raise RunPreparationError("run specification publication failed") from None

    try:
        run_repository.create_pending(run_record)
    except StateTransitionError:
        _log_failure(run_spec.run_id)
        raise
    except Exception:
        _log_failure(run_spec.run_id)
        raise RunPreparationError("pending run metadata creation failed") from None

    return PreparedRun(run_id=run_spec.run_id, run_spec_key=run_spec.run_spec_key)


def _ttl_days_from_environment(value: object) -> int:
    if not isinstance(value, str) or not value.isdecimal():
        raise ValueError("RUN_TTL_DAYS must be a positive integer")
    return _validate_ttl_days(int(value))


def lambda_handler(event: object, context: object) -> dict[str, object]:
    """Create Lambda-only AWS dependencies after validating runtime configuration."""
    del context
    import os

    image_digest = os.environ["ENGINE_IMAGE_DIGEST"]
    bucket = os.environ["ARTIFACT_BUCKET"]
    table_name = os.environ["RUN_TABLE"]
    ttl_days = _ttl_days_from_environment(os.environ["RUN_TTL_DAYS"])
    _validate_image_digest(image_digest)
    _validate_bucket(bucket)
    if not table_name:
        raise ValueError("RUN_TABLE must not be empty")

    import boto3

    from src.cloud.storage import S3ObjectStore

    object_store = S3ObjectStore(client=boto3.client("s3"), bucket=bucket)
    run_repository = DynamoRunRepository(table=boto3.resource("dynamodb").Table(table_name))
    prepared = prepare_run(
        event,
        object_store=object_store,
        run_repository=run_repository,
        image_digest=image_digest,
        clock=lambda: datetime.now(UTC),
        uuid_factory=uuid4,
        ttl_days=ttl_days,
    )
    response: dict[str, object] = {
        "run_id": prepared.run_id,
        "run_spec_key": prepared.run_spec_key,
    }
    return response
