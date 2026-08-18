"""API integration tests for the shared acquisition service boundary."""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.api.deps import get_acquisition_service, get_db
from src.api.main import app
from src.api.schemas import DataFetchRequest
from src.data.contracts import (
    REPORT_ARCHIVE_DEFERRED_WARNING,
    AcquisitionManifest,
    AcquisitionRequest,
    AcquisitionResult,
    AcquisitionStatus,
    AcquisitionWarning,
    ActionCoverage,
    ArtifactError,
    AttemptEvidence,
    CacheEvidence,
    CachePublicationError,
    CacheStatus,
    InvalidRequestError,
    LineageSegment,
    NoUsableDataError,
    Provider,
    ProviderExhaustedError,
    ProviderQuotaError,
    QualityFinding,
    QualitySeverity,
)
from src.db.tables import Base


class FakeAcquisitionService:
    def __init__(
        self,
        result: AcquisitionResult | None = None,
        error: Exception | None = None,
        report: dict[str, Any] | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.report = report
        self.requests: list[AcquisitionRequest] = []

    def acquire(self, request: AcquisitionRequest) -> AcquisitionResult:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result

    def lookup_manifest(self, acquisition_id: str) -> dict[str, Any] | None:
        del acquisition_id
        return self.report


def _canonical_frame(rows: int = 2) -> pd.DataFrame:
    timestamps = pd.date_range("2020-01-02", periods=rows, freq="D")
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "symbol": pd.Series(["SPY"] * rows, dtype="string"),
            "open": pd.Series([100.0 + i for i in range(rows)], dtype="float64"),
            "high": pd.Series([102.0 + i for i in range(rows)], dtype="float64"),
            "low": pd.Series([99.0 + i for i in range(rows)], dtype="float64"),
            "close": pd.Series([101.0 + i for i in range(rows)], dtype="float64"),
            "volume": pd.Series([1_000.0] * rows, dtype="float64"),
            "adj_close": pd.Series([101.0 + i for i in range(rows)], dtype="float64"),
            "dividend_amount": pd.Series([0.0] * rows, dtype="float64"),
            "split_coefficient": pd.Series([1.0] * rows, dtype="float64"),
            "source": pd.Series(["alpha_vantage"] * rows, dtype="string"),
        }
    )


def _result(
    rows: int = 2,
    warnings: tuple[AcquisitionWarning, ...] = (),
) -> AcquisitionResult:
    request = AcquisitionRequest("SPY", date(2020, 1, 1), date(2022, 12, 31))
    now = datetime(2024, 1, 5, tzinfo=UTC)
    manifest = AcquisitionManifest(
        "acquisition-123",
        request,
        AcquisitionStatus.PARTIAL_SUCCESS,
        cache=CacheEvidence(CacheStatus.PARTIAL_HIT),
        attempts=(AttemptEvidence(Provider.ALPHA_VANTAGE, 1, now, 0.1, "success"),),
        findings=(QualityFinding(QualitySeverity.WARNING, "missing_sessions", "one gap"),),
        lineage=(
            LineageSegment(
                date(2020, 1, 1),
                date(2020, 1, 1),
                Provider.YFINANCE,
                now - timedelta(days=1),
                ActionCoverage.REPRESENTED,
                "a" * 64,
                "cached-actions",
            ),
            LineageSegment(
                date(2020, 1, 2),
                date(2020, 1, 3),
                Provider.ALPHA_VANTAGE,
                now,
                ActionCoverage.REPRESENTED,
                "b" * 64,
                "fresh-actions",
            ),
        ),
        counters={
            "expected_sessions": 3,
            "accepted_expected_sessions": 2,
            "missing_sessions": 1,
            "exact_duplicate_rows_removed": 2,
        },
        coverage=2 / 3,
        started_at=now,
        completed_at=now,
    )
    return AcquisitionResult(_canonical_frame(rows), manifest, warnings=warnings)


@pytest.fixture()
def api_client(tmp_path: Path) -> Generator[TestClient, None, None]:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    def override_db():
        db = session_factory()
        try:
            yield db
            db.commit()
        finally:
            db.close()

    del tmp_path
    try:
        Base.metadata.create_all(bind=engine)
        app.dependency_overrides[get_db] = override_db
        with patch("src.api.main.init_db"):
            with TestClient(app) as client:
                yield client
    finally:
        app.dependency_overrides.clear()
        try:
            Base.metadata.drop_all(bind=engine)
        finally:
            engine.dispose()


def _override_service(service: FakeAcquisitionService) -> None:
    app.dependency_overrides[get_acquisition_service] = lambda: service


def test_data_fetch_request_keeps_old_body_and_stable_defaults() -> None:
    request = DataFetchRequest(symbol="SPY", start="2024-01-01", end="2024-01-05")

    assert request.source == "auto"
    assert request.calendar == "XNYS"
    assert request.refresh is False
    assert request.use_cache is True


def test_fetch_keeps_legacy_iso_datetime_body_compatible(api_client: TestClient) -> None:
    service = FakeAcquisitionService(result=_result())
    _override_service(service)

    response = api_client.post(
        "/api/data/fetch",
        json={
            "symbol": "SPY",
            "start": "2020-01-01T00:00:00",
            "end": "2022-12-31T23:59:59",
        },
    )

    assert response.status_code == 200
    assert service.requests[0].start == date(2020, 1, 1)
    assert service.requests[0].end == date(2022, 12, 31)


def test_fetch_uses_service_forced_refresh_and_exact_compact_summary(
    api_client: TestClient,
) -> None:
    service = FakeAcquisitionService(result=_result())
    _override_service(service)

    response = api_client.post(
        "/api/data/fetch",
        json={
            "symbol": "spy",
            "start": "2020-01-01",
            "end": "2022-12-31",
            "use_cache": False,
        },
    )

    assert response.status_code == 200
    request = service.requests[0]
    assert request.use_cache is False
    assert request.refresh is True
    body = response.json()
    assert set(body) == {"symbol", "n_candles", "start", "end", "from_cache", "summary"}
    assert set(body["summary"]) == {
        "acquisition_id",
        "status",
        "sources_used",
        "selected_source",
        "cache_status",
        "requested_sessions",
        "accepted_rows",
        "rejected_rows",
        "missing_sessions",
        "duplicates_removed",
        "coverage",
        "warnings",
    }
    assert body["summary"] == {
        "acquisition_id": "acquisition-123",
        "status": "partial_success",
        "sources_used": ["yfinance", "alpha_vantage"],
        "selected_source": "alpha_vantage",
        "cache_status": "partial_hit",
        "requested_sessions": 3,
        "accepted_rows": 2,
        "rejected_rows": 0,
        "missing_sessions": 1,
        "duplicates_removed": 2,
        "coverage": 2 / 3,
        "warnings": 1,
    }
    assert "frame" not in str(body).lower()


def test_post_commit_archive_warning_keeps_api_200_and_increments_summary(
    api_client: TestClient,
) -> None:
    service = FakeAcquisitionService(
        result=_result(warnings=(REPORT_ARCHIVE_DEFERRED_WARNING,))
    )
    _override_service(service)

    response = api_client.post(
        "/api/data/fetch",
        json={"symbol": "SPY", "start": "2020-01-01", "end": "2022-12-31"},
    )

    assert response.status_code == 200
    assert response.json()["summary"]["warnings"] == 2


def test_standalone_archive_failure_maps_api_500(api_client: TestClient) -> None:
    service = FakeAcquisitionService(
        error=ArtifactError("secret=archive-failure", acquisition_id="standalone")
    )
    _override_service(service)

    response = api_client.post(
        "/api/data/fetch",
        json={"symbol": "SPY", "start": "2020-01-01", "end": "2022-12-31"},
    )

    assert response.status_code == 500
    assert response.json()["detail"] == {
        "code": "cache_publication_failed",
        "message": "Market data artifacts could not be published.",
        "acquisition_id": "standalone",
    }
    assert "archive-failure" not in response.text


def test_report_lookup_returns_redacted_full_manifest(api_client: TestClient) -> None:
    report = _result().manifest.to_dict()
    report["provider_metadata"] = {"api_key": "known-secret", "page": 1}
    service = FakeAcquisitionService(report=report)
    _override_service(service)

    response = api_client.get("/api/data/reports/acquisition-123")

    assert response.status_code == 200
    payload = response.json()
    assert payload["acquisition_id"] == "acquisition-123"
    assert "known-secret" not in response.text
    assert payload["provider_metadata"]["api_key"] == "[REDACTED]"


def test_report_lookup_rejects_unsafe_identifier_as_bad_request(api_client: TestClient) -> None:
    service = FakeAcquisitionService(report=None)
    _override_service(service)

    response = api_client.get("/api/data/reports/bad!")

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_request"


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_code"),
    [
        (InvalidRequestError("bad request", acquisition_id="request-1"), 400, "invalid_request"),
        (NoUsableDataError("fatal quality", acquisition_id="request-2"), 422, "no_usable_data"),
        (ProviderQuotaError("local quota", acquisition_id="request-3"), 429, "provider_quota"),
        (
            ProviderExhaustedError("secret=should-not-leak", acquisition_id="request-4"),
            502,
            "provider_exhausted",
        ),
        (
            CachePublicationError("internal/path/should-not-leak", acquisition_id="request-5"),
            500,
            "cache_publication_failed",
        ),
    ],
)
def test_fetch_maps_typed_errors_without_internal_details(
    api_client: TestClient,
    error: Exception,
    expected_status: int,
    expected_code: str,
) -> None:
    service = FakeAcquisitionService(error=error)
    _override_service(service)

    response = api_client.post(
        "/api/data/fetch",
        json={"symbol": "SPY", "start": "2020-01-01", "end": "2020-01-03"},
    )

    assert response.status_code == expected_status
    assert response.json()["detail"] == {
        "code": expected_code,
        "message": {
            "invalid_request": "The acquisition request is invalid.",
            "no_usable_data": "No usable market data satisfied quality requirements.",
            "provider_quota": "The requested provider quota is exhausted.",
            "provider_exhausted": "Market data providers are unavailable.",
            "cache_publication_failed": "Market data artifacts could not be published.",
        }[expected_code],
        "acquisition_id": getattr(error, "acquisition_id"),
    }
    assert "should-not-leak" not in response.text


def test_backtest_route_uses_injected_service_without_response_change(
    api_client: TestClient,
) -> None:
    service = FakeAcquisitionService(result=_result(rows=150))
    _override_service(service)

    response = api_client.post(
        "/api/backtest",
        json={
            "strategy": "ma_crossover",
            "symbol": "SPY",
            "start": "2020-01-01",
            "end": "2022-12-31",
            "params": {"fast_period": 5, "slow_period": 20},
        },
    )

    assert response.status_code == 201
    assert len(service.requests) == 1
    assert set(response.json()) == {
        "run_id",
        "strategy_name",
        "symbol",
        "start_date",
        "end_date",
        "final_equity",
        "initial_capital",
        "metrics",
    }
