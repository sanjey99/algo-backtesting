"""Expose only finalized public cloud-run summaries through HTTP API v2."""

from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from types import MappingProxyType
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from src.cloud.contracts import (
    REQUIRED_ARTIFACTS,
    ChecksumsManifest,
    RunRecord,
    RunStatus,
    Visibility,
    _parse_utc_datetime,
    _validate_bucket,
    _validate_image_digest,
    _validate_sha256,
    _validate_uuid,
    canonical_json_bytes,
    sha256_hex,
)
from src.cloud.storage import DynamoRunRepository, ObjectStore, RunRepository, S3ObjectStore
from src.observability import log_event

logger = logging.getLogger(__name__)

_CHECKSUMS_MAXIMUM_BYTES = 32 * 1024
_RESULT_MAXIMUM_BYTES = 64 * 1024
_RESPONSE_MAXIMUM_BYTES = 64 * 1024
_PRESIGN_SECONDS = 300
_TABLE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{3,255}$")
_FORBIDDEN_EVENT_FIELDS = frozenset(
    {
        "artifact_bucket",
        "bucket",
        "key",
        "object_store",
        "prefix",
        "run_repository",
        "run_table",
        "table",
    }
)


@dataclass(frozen=True, slots=True)
class ApiResponse:
    """Immutable byte response that can be converted to Lambda proxy wire form."""

    status_code: int
    headers: Mapping[str, str]
    body: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.status_code, int) or isinstance(self.status_code, bool):
            raise TypeError("status_code must be an integer")
        if not isinstance(self.body, bytes):
            raise TypeError("body must be bytes")
        copied_headers = dict(self.headers)
        if not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in copied_headers.items()
        ):
            raise TypeError("headers must be a string mapping")
        object.__setattr__(self, "headers", MappingProxyType(copied_headers))

    def to_wire(self) -> dict[str, object]:
        """Return the API Gateway v2 proxy representation without binary encoding."""
        return {
            "statusCode": self.status_code,
            "headers": dict(self.headers),
            "body": self.body.decode("utf-8"),
        }


_JSON_HEADERS = MappingProxyType(
    {"content-type": "application/json", "cache-control": "no-store"}
)
NOT_FOUND = ApiResponse(404, _JSON_HEADERS, b'{"error":"run_not_found"}')
BAD_REQUEST = ApiResponse(400, _JSON_HEADERS, b'{"error":"bad_request"}')
METHOD_NOT_ALLOWED = ApiResponse(405, _JSON_HEADERS, b'{"error":"method_not_allowed"}')


class _PublicResult(BaseModel):
    """The exact bounded summary that Task 5 is allowed to publish."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["1"]
    run_id: str
    symbol: str
    start_date: str
    end_date: str
    strategy_name: str
    strategy_parameters: dict[str, int | float]
    initial_capital: float
    final_equity: float
    metrics: dict[str, float | None]
    total_trades: int
    image_digest: str
    dataset_sha256: str
    completed_at: str

    @field_validator("run_id", mode="before")
    @classmethod
    def _admit_run_id(cls, value: object) -> str:
        return _validate_uuid(value)

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def _admit_dates(cls, value: object) -> str:
        if not isinstance(value, str):
            raise TypeError("date must be an ISO string")
        return date.fromisoformat(value).isoformat()

    @field_validator("completed_at", mode="before")
    @classmethod
    def _admit_completed_at(cls, value: object) -> str:
        return _parse_utc_datetime(value).isoformat().replace("+00:00", "Z")

    @field_validator("image_digest", mode="before")
    @classmethod
    def _admit_image_digest(cls, value: object) -> str:
        return _validate_image_digest(value)

    @field_validator("dataset_sha256", mode="before")
    @classmethod
    def _admit_dataset_sha256(cls, value: object) -> str:
        return _validate_sha256(value)

    @field_validator("strategy_parameters", mode="before")
    @classmethod
    def _admit_parameters(cls, value: object) -> dict[str, int | float]:
        if not isinstance(value, Mapping) or len(value) > 16:
            raise ValueError("strategy parameters must be a bounded object")
        admitted: dict[str, int | float] = {}
        for name, parameter in value.items():
            if (
                not isinstance(name, str)
                or not name
                or isinstance(parameter, bool)
                or not isinstance(parameter, int | float)
                or not math.isfinite(parameter)
            ):
                raise ValueError("strategy parameters must be finite scalar values")
            admitted[name] = parameter
        return admitted

    @field_validator("metrics", mode="before")
    @classmethod
    def _admit_metrics(cls, value: object) -> dict[str, float | None]:
        if not isinstance(value, Mapping) or len(value) > 128:
            raise ValueError("metrics must be a bounded object")
        admitted: dict[str, float | None] = {}
        for name, metric in value.items():
            if not isinstance(name, str) or not name:
                raise ValueError("metric names must be non-empty strings")
            if metric is None:
                admitted[name] = None
            elif (
                isinstance(metric, bool)
                or not isinstance(metric, float)
                or not math.isfinite(metric)
            ):
                raise ValueError("metrics must be finite floats or null")
            else:
                admitted[name] = metric
        return admitted

    @field_validator("initial_capital", "final_equity")
    @classmethod
    def _admit_finite_floats(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("summary values must be finite")
        return value

    @field_validator("total_trades")
    @classmethod
    def _admit_trade_count(cls, value: int) -> int:
        if isinstance(value, bool) or value < 0:
            raise ValueError("total_trades must be a non-negative integer")
        return value


class _HiddenResultError(Exception):
    """A non-public outcome that must collapse to the uniform 404 response."""


def _result_prefix(run_id: str) -> str:
    return f"runs/v1/{_validate_uuid(run_id)}/"


def _strict_json_object(body: bytes) -> dict[str, object]:
    """Parse exactly canonical JSON and reject duplicate keys before Pydantic sees it."""
    try:
        decoded = body.decode("utf-8")

        def reject_duplicate_keys(value: list[tuple[str, object]]) -> dict[str, object]:
            names = [name for name, _ in value]
            if len(names) != len(set(names)):
                raise ValueError("duplicate JSON object keys")
            return dict(value)

        parsed = json.loads(decoded, object_pairs_hook=reject_duplicate_keys)
        if not isinstance(parsed, dict) or not all(isinstance(key, str) for key in parsed):
            raise ValueError("JSON value is not an object")
        if canonical_json_bytes(parsed) != body:
            raise ValueError("JSON is not canonical")
        return parsed
    except (UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise _HiddenResultError("artifact JSON is invalid") from error


def _canonical_record(value: object, *, run_id: str) -> RunRecord:
    if isinstance(value, RunRecord):
        payload = value.model_dump(mode="python")
    elif isinstance(value, Mapping):
        payload = dict(value)
    else:
        raise _HiddenResultError("record has an unsupported type")
    try:
        record = RunRecord.model_validate(payload)
    except (TypeError, ValidationError, ValueError) as error:
        raise _HiddenResultError("record is malformed") from error
    if record.run_id != run_id:
        raise _HiddenResultError("record identifier mismatch")
    return record


def _public_record(run_id: str, *, run_repository: RunRepository) -> RunRecord:
    value = run_repository.get(run_id, consistent=True)
    if value is None:
        raise _HiddenResultError("record is absent")
    record = _canonical_record(value, run_id=run_id)
    if (
        record.status is not RunStatus.SUCCEEDED
        or record.visibility is not Visibility.PUBLIC
        or record.failure_code is not None
        or record.started_at is None
        or record.completed_at is None
    ):
        raise _HiddenResultError("record is not publicly finalized")
    return record


def _manifest(object_store: ObjectStore, *, run_id: str) -> ChecksumsManifest:
    body = object_store.get(f"{_result_prefix(run_id)}checksums.json", _CHECKSUMS_MAXIMUM_BYTES)
    payload = _strict_json_object(body)
    try:
        manifest = ChecksumsManifest.model_validate(payload)
    except (TypeError, ValidationError, ValueError) as error:
        raise _HiddenResultError("checksums manifest is malformed") from error
    if {artifact.name for artifact in manifest.artifacts} != REQUIRED_ARTIFACTS:
        raise _HiddenResultError("checksums manifest has the wrong artifact set")
    return manifest


def _entry(manifest: ChecksumsManifest, name: str) -> tuple[int, str]:
    matching = [artifact for artifact in manifest.artifacts if artifact.name == name]
    if len(matching) != 1:
        raise _HiddenResultError("checksums manifest entry is absent")
    artifact = matching[0]
    return artifact.byte_length, artifact.sha256


def _result_summary(
    object_store: ObjectStore, *, run_id: str, manifest: ChecksumsManifest
) -> _PublicResult:
    expected_length, expected_digest = _entry(manifest, "result.json")
    body = object_store.get(f"{_result_prefix(run_id)}result.json", _RESULT_MAXIMUM_BYTES)
    if len(body) != expected_length or sha256_hex(body) != expected_digest:
        raise _HiddenResultError("result artifact does not match checksums")
    payload = _strict_json_object(body)
    try:
        summary = _PublicResult.model_validate(payload)
    except (TypeError, ValidationError, ValueError) as error:
        raise _HiddenResultError("result summary is malformed") from error
    if summary.run_id != run_id:
        raise _HiddenResultError("result identifier mismatch")
    return summary


def _verify_report_reference(
    object_store: ObjectStore, *, run_id: str, manifest: ChecksumsManifest
) -> str:
    expected_length, expected_digest = _entry(manifest, "report.html")
    key = f"{_result_prefix(run_id)}report.html"
    stored = object_store.head(key)
    if stored.byte_length != expected_length or stored.sha256 != expected_digest:
        raise _HiddenResultError("report artifact does not match checksums")
    return key


def _success_response(summary: _PublicResult, *, report_url: str) -> ApiResponse:
    if not isinstance(report_url, str) or not report_url:
        raise _HiddenResultError("presigned URL is invalid")
    payload = {
        **summary.model_dump(mode="json"),
        "report_url": report_url,
        "report_url_expires_seconds": _PRESIGN_SECONDS,
    }
    body = canonical_json_bytes(payload)
    if len(body) > _RESPONSE_MAXIMUM_BYTES:
        raise _HiddenResultError("public response is too large")
    return ApiResponse(200, _JSON_HEADERS, body)


def get_public_result(
    run_id: str,
    *,
    object_store: ObjectStore,
    run_repository: RunRepository,
) -> ApiResponse:
    """Return only a verified PUBLIC/SUCCEEDED summary or the uniform hidden response."""
    try:
        admitted_run_id = _validate_uuid(run_id)
    except (TypeError, ValueError):
        return NOT_FOUND
    try:
        _public_record(admitted_run_id, run_repository=run_repository)
        manifest = _manifest(object_store, run_id=admitted_run_id)
        summary = _result_summary(object_store, run_id=admitted_run_id, manifest=manifest)
        report_key = _verify_report_reference(
            object_store, run_id=admitted_run_id, manifest=manifest
        )
        return _success_response(
            summary,
            report_url=object_store.presign_get(report_key, _PRESIGN_SECONDS),
        )
    except Exception as error:
        log_event(
            logger,
            logging.INFO,
            "cloud.results.hidden",
            run_id=admitted_run_id,
            error_type=type(error).__name__,
        )
        return NOT_FOUND


def _event_method(event: object) -> str:
    if not isinstance(event, Mapping) or not all(isinstance(key, str) for key in event):
        raise ValueError("event must be an object")
    if event.get("version") != "2.0":
        raise ValueError("event must be HTTP API v2")
    context = event.get("requestContext")
    if not isinstance(context, Mapping):
        raise ValueError("request context is required")
    http = context.get("http")
    if not isinstance(http, Mapping):
        raise ValueError("HTTP request context is required")
    method = http.get("method")
    if not isinstance(method, str):
        raise ValueError("HTTP method is required")
    return method


def _event_run_id(event: Mapping[str, object]) -> str:
    if any(name in event for name in _FORBIDDEN_EVENT_FIELDS):
        raise ValueError("caller storage routing is forbidden")
    raw_query = event.get("rawQueryString", "")
    if not isinstance(raw_query, str) or raw_query:
        raise ValueError("query strings are not supported")
    query = event.get("queryStringParameters")
    if query not in (None, {}):
        raise ValueError("query parameters are not supported")
    if event.get("body") is not None or event.get("isBase64Encoded", False) is not False:
        raise ValueError("request bodies are not supported")
    path_parameters = event.get("pathParameters")
    if not isinstance(path_parameters, Mapping) or set(path_parameters) != {"run_id"}:
        raise ValueError("exactly one path parameter is required")
    run_id = path_parameters.get("run_id")
    if not isinstance(run_id, str):
        raise ValueError("run_id must be a string")
    route_key = event.get("routeKey")
    if route_key is not None and route_key != "GET /runs/{run_id}":
        raise ValueError("route key is invalid")
    return run_id


def _runtime_dependencies() -> tuple[ObjectStore, RunRepository]:
    """Create trusted AWS adapters only once request routing has been admitted."""
    import os

    bucket = os.environ["ARTIFACT_BUCKET"]
    table_name = os.environ["RUN_TABLE"]
    _validate_bucket(bucket)
    if not isinstance(table_name, str) or not _TABLE_NAME_PATTERN.fullmatch(table_name):
        raise ValueError("RUN_TABLE must use the DynamoDB table-name grammar")

    import boto3

    return (
        S3ObjectStore(client=boto3.client("s3"), bucket=bucket),
        DynamoRunRepository(table=boto3.resource("dynamodb").Table(table_name)),
    )


def lambda_handler(event: object, context: object) -> dict[str, object]:
    """Adapt one closed HTTP API v2 GET event to the verified public result response."""
    del context
    try:
        method = _event_method(event)
    except ValueError:
        return BAD_REQUEST.to_wire()
    if method != "GET":
        return METHOD_NOT_ALLOWED.to_wire()
    if not isinstance(event, Mapping):  # _event_method proved this; narrow for static typing.
        return BAD_REQUEST.to_wire()
    try:
        run_id = _event_run_id(event)
    except ValueError:
        return BAD_REQUEST.to_wire()
    try:
        admitted_run_id = _validate_uuid(run_id)
    except (TypeError, ValueError):
        return NOT_FOUND.to_wire()
    object_store, run_repository = _runtime_dependencies()
    return get_public_result(
        admitted_run_id,
        object_store=object_store,
        run_repository=run_repository,
    ).to_wire()
