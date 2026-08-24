"""Verify immutable cloud-run artifacts before making a terminal transition."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from pydantic import ValidationError

from src.cloud.contracts import (
    REQUIRED_ARTIFACTS,
    ChecksumsManifest,
    FailureCode,
    RunRecord,
    RunStatus,
    _parse_utc_datetime,
    _validate_bucket,
    _validate_uuid,
    canonical_json_bytes,
    sha256_hex,
)
from src.cloud.storage import DynamoRunRepository, ObjectStore, RunRepository, S3ObjectStore
from src.observability import log_event

logger = logging.getLogger(__name__)

ARTIFACT_MAXIMUM_BYTES = {
    "run-spec.json": 64 * 1024,
    "result.json": 64 * 1024,
    "trades.parquet": 16 * 1024 * 1024,
    "equity-curve.parquet": 32 * 1024 * 1024,
    "report.html": 8 * 1024 * 1024,
    "checksums.json": 32 * 1024,
}

Clock = Callable[[], datetime]


class ArtifactVerificationError(Exception):
    """A required immutable result artifact cannot prove a successful run."""


def _read_now(clock: Clock) -> datetime:
    return _parse_utc_datetime(clock())


def _result_prefix(run_id: str) -> str:
    return f"runs/v1/{_validate_uuid(run_id)}/"


def _strict_json_object(body: bytes, *, subject: str) -> dict[str, object]:
    try:
        decoded = body.decode("utf-8")
        pairs: list[tuple[str, object]] = []

        def reject_duplicate_keys(value: list[tuple[str, object]]) -> dict[str, object]:
            names = [name for name, _ in value]
            if len(names) != len(set(names)):
                raise ValueError("duplicate JSON object keys")
            pairs.extend(value)
            return dict(value)

        parsed = json.loads(decoded, object_pairs_hook=reject_duplicate_keys)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
        raise ArtifactVerificationError(f"{subject} is not valid canonical JSON") from error
    if not isinstance(parsed, dict) or not all(isinstance(key, str) for key in parsed):
        raise ArtifactVerificationError(f"{subject} must be a JSON object")
    try:
        if canonical_json_bytes(parsed) != body:
            raise ValueError("non-canonical JSON")
    except (TypeError, ValueError) as error:
        raise ArtifactVerificationError(f"{subject} is not valid canonical JSON") from error
    return parsed


def _read_manifest(object_store: ObjectStore, *, run_id: str) -> ChecksumsManifest:
    prefix = _result_prefix(run_id)
    body = object_store.get(f"{prefix}checksums.json", ARTIFACT_MAXIMUM_BYTES["checksums.json"])
    payload = _strict_json_object(body, subject="checksums manifest")
    try:
        manifest = ChecksumsManifest.model_validate(payload)
    except ValidationError as error:
        raise ArtifactVerificationError("artifact verification failed") from error
    if {artifact.name for artifact in manifest.artifacts} != REQUIRED_ARTIFACTS:
        raise ArtifactVerificationError("artifact verification failed")
    return manifest


def _verify_required_artifacts(
    *, run_id: str, object_store: ObjectStore, manifest: ChecksumsManifest
) -> dict[str, bytes]:
    prefix = _result_prefix(run_id)
    verified: dict[str, bytes] = {}
    for artifact in manifest.artifacts:
        name = artifact.name
        try:
            body = object_store.get(f"{prefix}{name}", ARTIFACT_MAXIMUM_BYTES[name])
        except KeyError as error:
            raise ArtifactVerificationError("artifact verification failed") from error
        if len(body) != artifact.byte_length or sha256_hex(body) != artifact.sha256:
            raise ArtifactVerificationError("artifact verification failed")
        verified[name] = body
    if set(verified) != REQUIRED_ARTIFACTS:
        raise ArtifactVerificationError("artifact verification failed")
    return verified


def _verify_result_run_id(result_body: bytes, *, run_id: str) -> None:
    payload = _strict_json_object(result_body, subject="result artifact")
    value = payload.get("run_id")
    try:
        admitted = _validate_uuid(value)
    except (TypeError, ValueError) as error:
        raise ArtifactVerificationError("result artifact has an invalid run identifier") from error
    if admitted != run_id:
        raise ArtifactVerificationError("result run identifier mismatch")


def _canonical_record(run_id: str, *, run_repository: RunRepository) -> RunRecord:
    record = run_repository.get(run_id, consistent=True)
    if record is None:
        raise ArtifactVerificationError("run record is unavailable after finalization")
    return record


def finalize_success(
    run_id: str,
    *,
    object_store: ObjectStore,
    run_repository: RunRepository,
    clock: Clock,
) -> RunRecord:
    """Verify every required artifact, then conditionally persist SUCCEEDED."""
    admitted_run_id = _validate_uuid(run_id)
    manifest = _read_manifest(object_store, run_id=admitted_run_id)
    artifacts = _verify_required_artifacts(
        run_id=admitted_run_id, object_store=object_store, manifest=manifest
    )
    _verify_result_run_id(artifacts["result.json"], run_id=admitted_run_id)
    run_repository.mark_succeeded(admitted_run_id, _read_now(clock))
    record = _canonical_record(admitted_run_id, run_repository=run_repository)
    if record.status is not RunStatus.SUCCEEDED:
        raise ArtifactVerificationError("run record is incompatible with success finalization")
    return record


def finalize_failure(
    run_id: str,
    code: FailureCode,
    *,
    run_repository: RunRepository,
    clock: Clock,
) -> RunRecord:
    """Conditionally persist one closed terminal failure code."""
    admitted_run_id = _validate_uuid(run_id)
    if not isinstance(code, FailureCode):
        raise TypeError("code must be a FailureCode")
    run_repository.mark_failed(admitted_run_id, code, _read_now(clock))
    record = _canonical_record(admitted_run_id, run_repository=run_repository)
    if record.status is not RunStatus.FAILED or record.failure_code is not code:
        raise ArtifactVerificationError("run record is incompatible with failure finalization")
    return record


def _closed_event(event: object) -> tuple[str, FailureCode | None]:
    if not isinstance(event, Mapping) or not all(isinstance(key, str) for key in event):
        raise ValueError("finalization event must be a closed object")
    outcome = event.get("outcome")
    run_id = event.get("run_id")
    if outcome == "SUCCEEDED" and set(event) == {"run_id", "outcome"}:
        return _validate_uuid(run_id), None
    if outcome == "FAILED" and set(event) == {"run_id", "outcome", "failure_code"}:
        code = event.get("failure_code")
        if not isinstance(code, str):
            raise ValueError("failure_code must be a closed string code")
        return _validate_uuid(run_id), FailureCode(code)
    raise ValueError("finalization event contains unsupported routing")


def _response(record: RunRecord) -> dict[str, object]:
    response: dict[str, object] = {"run_id": record.run_id, "status": record.status.value}
    if record.failure_code is not None:
        response["failure_code"] = record.failure_code.value
    return response


def handle_finalization(
    event: object,
    *,
    object_store: ObjectStore,
    run_repository: RunRepository,
    clock: Clock,
) -> dict[str, object]:
    """Map a closed workflow branch to exactly one terminal finalization operation."""
    run_id: str | None = None
    try:
        run_id, code = _closed_event(event)
        record = (
            finalize_success(
                run_id, object_store=object_store, run_repository=run_repository, clock=clock
            )
            if code is None
            else finalize_failure(run_id, code, run_repository=run_repository, clock=clock)
        )
    except Exception as error:
        log_event(
            logger,
            logging.ERROR,
            "cloud.run.finalization.failed",
            error_type=type(error).__name__,
            **({"run_id": run_id} if run_id is not None else {}),
        )
        raise
    return _response(record)


def lambda_handler(event: object, context: object) -> dict[str, object]:
    """Build AWS adapters only after validating local runtime configuration."""
    del context
    import os

    bucket = os.environ["ARTIFACT_BUCKET"]
    table_name = os.environ["RUN_TABLE"]
    _validate_bucket(bucket)
    if not table_name:
        raise ValueError("RUN_TABLE must not be empty")

    import boto3

    object_store = S3ObjectStore(client=boto3.client("s3"), bucket=bucket)
    run_repository = DynamoRunRepository(table=boto3.resource("dynamodb").Table(table_name))
    return handle_finalization(
        event,
        object_store=object_store,
        run_repository=run_repository,
        clock=lambda: datetime.now(UTC),
    )
