"""Thin market-data acquisition and report routes."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Annotated, Any, NoReturn

from fastapi import APIRouter, Depends, HTTPException

from src.api.deps import get_acquisition_service
from src.api.schemas import DataAcquisitionSummary, DataFetchOut, DataFetchRequest
from src.data import df_to_candles
from src.data.acquisition import AcquisitionService
from src.data.contracts import (
    AcquisitionRequest,
    AcquisitionResult,
    CacheError,
    CachePublicationError,
    CacheStatus,
    ContractViolationError,
    DataAcquisitionError,
    InvalidRequestError,
    ManifestError,
    NoUsableDataError,
    ProviderExhaustedError,
    ProviderQuotaError,
    QualityError,
    QualitySeverity,
    json_safe,
)

router = APIRouter(prefix="/api/data", tags=["data"])
AcquisitionDep = Annotated[AcquisitionService, Depends(get_acquisition_service)]
_SAFE_ACQUISITION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@router.post("/fetch", response_model=DataFetchOut)
def fetch_data(req: DataFetchRequest, service: AcquisitionDep) -> DataFetchOut:
    request = _request_from_api(req)
    result = acquire_result(service, request)
    try:
        candles = df_to_candles(result.frame)
    except ContractViolationError as error:
        error.assign_acquisition_id(result.manifest.acquisition_id)
        _raise_api_error(error)
    if not candles:
        _raise_api_error(
            NoUsableDataError(
                "validated acquisition returned no rows",
                acquisition_id=result.manifest.acquisition_id,
            )
        )
    return DataFetchOut(
        symbol=request.symbol,
        n_candles=len(candles),
        start=candles[0].timestamp.date().isoformat(),
        end=candles[-1].timestamp.date().isoformat(),
        from_cache=result.manifest.cache.status is CacheStatus.FULL_HIT,
        summary=_compact_summary(result),
    )


@router.get("/reports/{acquisition_id}", response_model=dict[str, Any])
def get_acquisition_report(acquisition_id: str, service: AcquisitionDep) -> dict[str, Any]:
    if not _SAFE_ACQUISITION_ID.fullmatch(acquisition_id):
        _raise_api_error(InvalidRequestError("acquisition identifier is invalid"))
    try:
        document = service.lookup_manifest(acquisition_id)
    except DataAcquisitionError as error:
        _raise_api_error(error)
    if document is None:
        raise HTTPException(status_code=404, detail="Acquisition report not found")
    safe = json_safe(document)
    if not isinstance(safe, dict):
        _raise_api_error(ManifestError("acquisition report is invalid"))
    return safe


def acquire_result(service: AcquisitionService, request: AcquisitionRequest) -> AcquisitionResult:
    """Invoke the service and expose only stable transport failures."""
    try:
        return service.acquire(request)
    except DataAcquisitionError as error:
        _raise_api_error(error)


def _request_from_api(req: DataFetchRequest) -> AcquisitionRequest:
    try:
        return AcquisitionRequest(
            symbol=req.symbol,
            start=_iso_date(req.start),
            end=_iso_date(req.end),
            source=req.source,
            calendar=req.calendar,
            use_cache=req.use_cache,
            refresh=req.refresh or not req.use_cache,
        )
    except InvalidRequestError as error:
        _raise_api_error(error)


def _iso_date(value: str) -> date:
    try:
        return datetime.fromisoformat(value).date()
    except ValueError as error:
        raise InvalidRequestError("start and end must use YYYY-MM-DD") from error


def _compact_summary(result: AcquisitionResult) -> DataAcquisitionSummary:
    manifest = result.manifest
    counters = manifest.counters
    successful_sources = _ordered_unique(
        attempt.provider.value for attempt in manifest.attempts if attempt.outcome == "success"
    )
    sources_used = _ordered_unique(segment.provider.value for segment in manifest.lineage)
    duplicates = _counter(counters, "exact_duplicate_rows_removed")
    if not duplicates:
        duplicates = sum(
            int(finding.details.get("rows", 0))
            for finding in manifest.findings
            if finding.code == "exact_duplicates_removed"
        )
    return DataAcquisitionSummary(
        acquisition_id=manifest.acquisition_id,
        status=manifest.status.value,
        sources_used=sources_used,
        selected_source=successful_sources[0] if len(successful_sources) == 1 else None,
        cache_status=manifest.cache.status.value,
        requested_sessions=_counter(counters, "expected_sessions"),
        accepted_rows=_counter(counters, "accepted_expected_sessions"),
        rejected_rows=len(manifest.rejected_rows),
        missing_sessions=_counter(counters, "missing_sessions"),
        duplicates_removed=duplicates,
        coverage=manifest.coverage,
        warnings=(
            sum(finding.severity is QualitySeverity.WARNING for finding in manifest.findings)
            + len(result.warnings)
        ),
    )


def _ordered_unique(values: Any) -> list[str]:
    return list(dict.fromkeys(values))


def _counter(counters: Any, name: str) -> int:
    value = counters.get(name, 0)
    return int(value) if isinstance(value, int | float) else 0


def _raise_api_error(error: DataAcquisitionError) -> NoReturn:
    status, code, message = _error_mapping(error)
    detail: dict[str, Any] = {"code": code, "message": message}
    if error.acquisition_id is not None:
        detail["acquisition_id"] = error.acquisition_id
    raise HTTPException(status_code=status, detail=detail) from None


def _error_mapping(error: DataAcquisitionError) -> tuple[int, str, str]:
    if isinstance(error, InvalidRequestError):
        return 400, "invalid_request", "The acquisition request is invalid."
    if isinstance(error, ProviderQuotaError):
        return 429, "provider_quota", "The requested provider quota is exhausted."
    if isinstance(error, ProviderExhaustedError):
        return 502, "provider_exhausted", "Market data providers are unavailable."
    if isinstance(error, CachePublicationError | CacheError | ManifestError):
        return 500, "cache_publication_failed", "Market data artifacts could not be published."
    if isinstance(error, NoUsableDataError | QualityError | ContractViolationError):
        return 422, "no_usable_data", "No usable market data satisfied quality requirements."
    return 500, "acquisition_failed", "Market data acquisition failed."
