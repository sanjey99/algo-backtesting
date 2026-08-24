from __future__ import annotations

import io
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd
import pytest
from pydantic import ValidationError

from src.cloud.contracts import canonical_json_bytes, sha256_hex
from src.cloud.ingestion_handler import AcquisitionFailedError, handle_ingestion
from src.data.contracts import (
    AcquisitionManifest,
    AcquisitionRequest,
    AcquisitionResult,
    AcquisitionStatus,
    SourcePreference,
)
from tests.cloud.fakes import FakeObjectStore

NOW = datetime(2024, 3, 29, 12, 0, tzinfo=UTC)
SECRET_VALUE = "provider-response-secret-value"


class FakeAcquisitionService:
    def __init__(self, result: AcquisitionResult) -> None:
        self._result = result
        self.requests: list[AcquisitionRequest] = []

    def acquire(self, request: AcquisitionRequest) -> AcquisitionResult:
        self.requests.append(request)
        return self._result


class FakeServiceFactory:
    def __init__(self, result: AcquisitionResult) -> None:
        self._result = result
        self.calls: list[tuple[Path, Path]] = []
        self.services: list[FakeAcquisitionService] = []

    def __call__(self, *, cache_dir: Path, manifest_dir: Path) -> FakeAcquisitionService:
        self.calls.append((cache_dir, manifest_dir))
        service = FakeAcquisitionService(self._result)
        self.services.append(service)
        return service


def request_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "symbol": "SPY",
        "start": "2024-01-02",
        "end": "2024-03-28",
        "strategy_key": "ma_crossover",
        "strategy_parameters": {"fast_period": 10, "slow_period": 50},
    }
    payload.update(overrides)
    return payload


def acquisition_result(status: AcquisitionStatus = AcquisitionStatus.SUCCESS) -> AcquisitionResult:
    request = AcquisitionRequest("SPY", date(2024, 1, 2), date(2024, 3, 28))
    frame = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2024-01-02", "2024-01-03"]),
            "open": [100.0, 101.0],
            "close": [101.0, 102.0],
        },
        index=pd.Index([41, 42], name="provider_row"),
    )
    manifest = AcquisitionManifest(
        acquisition_id="acquisition-123",
        request=request,
        status=status,
        environment_versions={
            "provider_api_key": SECRET_VALUE,
        },
        completed_at=NOW,
    )
    return AcquisitionResult(frame=frame, manifest=manifest)


def fixed_clock() -> datetime:
    return NOW


@pytest.mark.parametrize("status", [AcquisitionStatus.SUCCESS, AcquisitionStatus.PARTIAL_SUCCESS])
def test_handle_ingestion_publishes_pinned_redacted_artifacts(
    status: AcquisitionStatus,
) -> None:
    result = acquisition_result(status)
    factory = FakeServiceFactory(result)
    store = FakeObjectStore()

    response = handle_ingestion(
        request_payload(),
        service_factory=factory,
        object_store=store,
        bucket="research-artifacts",
        clock=fixed_clock,
    )

    dataset = response["dataset"]
    assert isinstance(dataset, dict)
    dataset_body, manifest_body = (call.body for call in store.put_calls)
    dataset_digest = sha256_hex(dataset_body)
    manifest_digest = sha256_hex(manifest_body)
    assert dataset["key"] == f"datasets/v1/acquisition-123/SPY-{dataset_digest}.parquet"
    assert dataset["sha256"] == dataset_digest
    assert dataset["manifest_key"] == f"datasets/v1/acquisition-123/manifest-{manifest_digest}.json"
    assert dataset["manifest_sha256"] == manifest_digest
    assert dataset["bucket"] == "research-artifacts"
    assert response["request"] == {
        "schema_version": "1",
        "symbol": "SPY",
        "start": "2024-01-02",
        "end": "2024-03-28",
        "strategy_key": "ma_crossover",
        "strategy_parameters": {"fast_period": 10, "slow_period": 50},
        "initial_capital": 100_000.0,
        "commission_pct": 0.001,
        "slippage_pct": 0.0005,
        "visibility": "PRIVATE",
    }

    uploaded_frame = pd.read_parquet(io.BytesIO(dataset_body))
    assert uploaded_frame.to_dict(orient="list") == {
        "timestamp": [pd.Timestamp("2024-01-02"), pd.Timestamp("2024-01-03")],
        "open": [100.0, 101.0],
        "close": [101.0, 102.0],
    }
    assert "provider_row" not in uploaded_frame.columns
    assert manifest_body == canonical_json_bytes(result.manifest.to_dict())
    assert SECRET_VALUE.encode() not in dataset_body
    assert SECRET_VALUE.encode() not in manifest_body
    assert b"raw-provider-response-must-not-publish" not in manifest_body
    assert factory.services[0].requests == [
        AcquisitionRequest(
            "SPY",
            date(2024, 1, 2),
            date(2024, 3, 28),
            source=SourcePreference.YFINANCE,
            calendar="XNYS",
            interval="1d",
            use_cache=True,
            refresh=False,
        )
    ]


def test_handle_ingestion_uses_fresh_temporary_children_for_each_acquisition() -> None:
    factory = FakeServiceFactory(acquisition_result())
    store = FakeObjectStore()

    for _ in range(2):
        handle_ingestion(
            request_payload(),
            service_factory=factory,
            object_store=store,
            bucket="research-artifacts",
            clock=fixed_clock,
        )

    assert len(factory.calls) == 2
    assert {cache_dir.parent for cache_dir, _ in factory.calls} == {
        manifest_dir.parent for _, manifest_dir in factory.calls
    }
    assert factory.calls[0][0].parent != factory.calls[1][0].parent
    assert all(cache_dir.name == "cache" for cache_dir, _ in factory.calls)
    assert all(manifest_dir.name == "manifests" for _, manifest_dir in factory.calls)
    assert all(cache_dir.parent.parent == Path("/tmp") for cache_dir, _ in factory.calls)
    assert all(not cache_dir.parent.exists() for cache_dir, _ in factory.calls)


def test_handle_ingestion_rejects_failed_acquisition_without_publishing() -> None:
    factory = FakeServiceFactory(acquisition_result(AcquisitionStatus.FAILED))
    store = FakeObjectStore()

    with pytest.raises(AcquisitionFailedError):
        handle_ingestion(
            request_payload(),
            service_factory=factory,
            object_store=store,
            bucket="research-artifacts",
            clock=fixed_clock,
        )

    assert store.put_calls == ()


def test_handle_ingestion_validates_event_before_creating_acquisition_service() -> None:
    factory = FakeServiceFactory(acquisition_result())
    store = FakeObjectStore()

    with pytest.raises(ValidationError):
        handle_ingestion(
            request_payload(symbol="unsafe/symbol"),
            service_factory=factory,
            object_store=store,
            bucket="research-artifacts",
            clock=fixed_clock,
        )

    assert factory.calls == []
    assert store.put_calls == ()
