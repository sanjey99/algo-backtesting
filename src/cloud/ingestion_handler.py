"""Publish immutable, redacted acquisition artifacts for the cloud workflow."""

from __future__ import annotations

import io
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Protocol

from src.cloud.contracts import (
    DatasetRef,
    FailureCode,
    ResearchRequest,
    canonical_json_bytes,
    sha256_hex,
)
from src.cloud.storage import LifecycleClass, ObjectStore
from src.data.contracts import (
    AcquisitionRequest,
    AcquisitionResult,
    AcquisitionStatus,
    SourcePreference,
)
from src.data.wiring import create_acquisition_service
from src.observability import log_event

logger = logging.getLogger(__name__)


class IngestionError(Exception):
    """Base error for the closed cloud-ingestion boundary."""


class AcquisitionFailedError(IngestionError):
    """The acquisition service did not produce a publishable result."""


class ArtifactPublicationError(IngestionError):
    """A normalized acquisition artifact could not be published."""


class AcquisitionService(Protocol):
    """The small existing-service surface used by the ingestion adapter."""

    def acquire(self, request: AcquisitionRequest) -> AcquisitionResult: ...


class AcquisitionServiceFactory(Protocol):
    """Factory that keeps handler tests offline and acquisition state isolated."""

    def __call__(self, *, cache_dir: Path, manifest_dir: Path) -> AcquisitionService: ...


Clock = Callable[[], datetime]


def _utc_timestamp(clock: Clock) -> str:
    timestamp = clock()
    if not isinstance(timestamp, datetime):
        raise TypeError("clock must return a datetime")
    if timestamp.tzinfo is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _acquisition_request(request: ResearchRequest) -> AcquisitionRequest:
    return AcquisitionRequest(
        symbol=request.symbol,
        start=request.start,
        end=request.end,
        source=SourcePreference.YFINANCE,
        calendar="XNYS",
        interval="1d",
        use_cache=True,
        refresh=False,
    )


def _log_failure() -> None:
    log_event(
        logger,
        logging.ERROR,
        "cloud.acquisition.failed",
        failure_code=FailureCode.ACQUISITION_FAILED,
    )


def _parquet_bytes(result: AcquisitionResult) -> bytes:
    buffer = io.BytesIO()
    result.frame.to_parquet(buffer, index=False)
    return buffer.getvalue()


def _request_json(request: ResearchRequest) -> dict[str, object]:
    """Convert the immutable admitted request into Step Functions primitives."""
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


def acquire_dataset(
    request: ResearchRequest,
    *,
    service_factory: AcquisitionServiceFactory,
    object_store: ObjectStore,
    bucket: str,
    clock: Clock,
) -> DatasetRef:
    """Acquire one admitted request and publish only immutable redacted artifacts."""
    try:
        started_at = _utc_timestamp(clock)
        log_event(
            logger,
            logging.INFO,
            "cloud.acquisition.started",
            symbol=request.symbol,
            started_at=started_at,
        )
        with TemporaryDirectory(dir="/tmp") as temporary_directory:
            root = Path(temporary_directory)
            service = service_factory(cache_dir=root / "cache", manifest_dir=root / "manifests")
            result = service.acquire(_acquisition_request(request))
    except Exception:
        _log_failure()
        raise AcquisitionFailedError("acquisition failed") from None

    if result.manifest.status not in {
        AcquisitionStatus.SUCCESS,
        AcquisitionStatus.PARTIAL_SUCCESS,
    }:
        _log_failure()
        raise AcquisitionFailedError("acquisition did not produce a publishable result")
    if result.manifest.completed_at is None:
        _log_failure()
        raise AcquisitionFailedError("acquisition did not produce completion evidence")

    try:
        dataset_body = _parquet_bytes(result)
        manifest_body = canonical_json_bytes(result.manifest.to_dict())
        dataset_digest = sha256_hex(dataset_body)
        manifest_digest = sha256_hex(manifest_body)
        dataset_key = (
            f"datasets/v1/{result.manifest.acquisition_id}/"
            f"{request.symbol}-{dataset_digest}.parquet"
        )
        manifest_key = (
            f"datasets/v1/{result.manifest.acquisition_id}/"
            f"manifest-{manifest_digest}.json"
        )
        object_store.put(
            dataset_key,
            dataset_body,
            "application/vnd.apache.parquet",
            lifecycle_class=LifecycleClass.TRANSIENT,
        )
        object_store.put(
            manifest_key,
            manifest_body,
            "application/json",
            lifecycle_class=LifecycleClass.TRANSIENT,
        )
        dataset = DatasetRef(
            bucket=bucket,
            key=dataset_key,
            sha256=dataset_digest,
            manifest_key=manifest_key,
            manifest_sha256=manifest_digest,
            symbol=request.symbol,
            calendar="XNYS",
            interval="1d",
            start=request.start,
            end=request.end,
            acquisition_id=result.manifest.acquisition_id,
            completed_at=result.manifest.completed_at,
        )
    except Exception:
        _log_failure()
        raise ArtifactPublicationError("artifact publication failed") from None

    log_event(
        logger,
        logging.INFO,
        "cloud.acquisition.succeeded",
        acquisition_id=dataset.acquisition_id,
        dataset_sha256=dataset.sha256,
    )
    return dataset


def handle_ingestion(
    event: object,
    *,
    service_factory: AcquisitionServiceFactory,
    object_store: ObjectStore,
    bucket: str,
    clock: Clock,
) -> dict[str, object]:
    """Validate a workflow event before acquisition or cloud publication effects."""
    request = ResearchRequest.model_validate(event)
    dataset = acquire_dataset(
        request,
        service_factory=service_factory,
        object_store=object_store,
        bucket=bucket,
        clock=clock,
    )
    return {
        "dataset": dataset.model_dump(mode="json"),
        "request": _request_json(request),
    }


def lambda_handler(event: object, context: object) -> dict[str, object]:
    """Assemble Lambda-only AWS dependencies at the runtime boundary."""
    del context
    import os

    import boto3

    from src.cloud.storage import S3ObjectStore

    bucket = os.environ["ARTIFACT_BUCKET"]
    object_store = S3ObjectStore(client=boto3.client("s3"), bucket=bucket)
    return handle_ingestion(
        event,
        service_factory=create_acquisition_service,
        object_store=object_store,
        bucket=bucket,
        clock=lambda: datetime.now(UTC),
    )
