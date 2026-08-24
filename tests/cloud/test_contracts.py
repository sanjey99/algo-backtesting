from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from src.cloud.contracts import (
    ArtifactDigest,
    ChecksumsManifest,
    DatasetRef,
    ResearchRequest,
    RunRecord,
    RunSpec,
    RunStatus,
    Visibility,
    canonical_json_bytes,
    sha256_hex,
)


def request_payload(**overrides: object) -> dict[str, object]:
    """Return an independently written valid public admission payload."""
    payload: dict[str, object] = {
        "symbol": "SPY",
        "start": "2024-01-02",
        "end": "2024-03-28",
        "strategy_key": "ma_crossover",
        "strategy_parameters": {"fast_period": 10, "slow_period": 50},
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize(
    ("strategy_key", "parameters"),
    [
        ("ma_crossover", {"fast_period": 10, "slow_period": 50}),
        ("rsi_mean_reversion", {"period": 14, "oversold": 30.0, "exit_level": 50.0}),
        ("breakout", {"lookback": 20}),
    ],
)
def test_research_request_admits_every_registered_strategy(
    strategy_key: str, parameters: dict[str, object]
) -> None:
    request = ResearchRequest.model_validate(
        request_payload(strategy_key=strategy_key, strategy_parameters=parameters)
    )

    assert request.symbol == "SPY"
    assert request.visibility is Visibility.PRIVATE


def test_research_request_freezes_omitted_strategy_parameters() -> None:
    request = ResearchRequest.model_validate(
        {
            "symbol": "SPY",
            "start": "2024-01-02",
            "end": "2024-03-28",
            "strategy_key": "breakout",
        }
    )

    with pytest.raises(TypeError):
        request.strategy_parameters["lookback"] = 20  # type: ignore[index]


@pytest.mark.parametrize(
    "payload",
    [
        request_payload(strategy_key="not_registered"),
        request_payload(strategy_parameters={"fast_period": 50, "slow_period": 10}),
        request_payload(strategy_parameters={"unknown": 1}),
        request_payload(start="2024-02-30"),
        request_payload(start="2024-03-28", end="2024-01-02"),
        request_payload(start="2014-01-01", end="2024-01-02"),
        request_payload(initial_capital=float("inf")),
        request_payload(commission_pct=float("nan")),
        request_payload(slippage_pct=-0.01),
        request_payload(strategy_parameters={"fast_period": float("nan"), "slow_period": 50}),
        request_payload(strategy_parameters={str(index): index for index in range(17)}),
    ],
)
def test_research_request_rejects_invalid_admission_values(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ResearchRequest.model_validate(payload)


def test_research_request_rejects_output_location_and_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ResearchRequest.model_validate(
            {
                "symbol": "SPY",
                "start": "2024-01-02",
                "end": "2024-03-28",
                "strategy_key": "ma_crossover",
                "strategy_parameters": {"fast_period": 10, "slow_period": 50},
                "output_prefix": "runs/v1/chosen-by-caller/",
            }
        )


def dataset_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "bucket": "research-artifacts",
        "key": "datasets/v1/acquisition-1/spy.parquet",
        "sha256": "a" * 64,
        "manifest_key": "datasets/v1/acquisition-1/manifest.json",
        "manifest_sha256": "b" * 64,
        "symbol": "SPY",
        "calendar": "XNYS",
        "interval": "1d",
        "start": "2024-01-02",
        "end": "2024-03-28",
        "acquisition_id": "acquisition-1",
        "completed_at": "2024-03-29T12:00:00Z",
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize(
    "payload",
    [
        dataset_payload(key="datasets/v1//spy.parquet"),
        dataset_payload(key="datasets/v1/acquisition-1/../spy.parquet"),
        dataset_payload(key="datasets/v1/acquisition-1\\spy.parquet"),
        dataset_payload(key="runs/v1/acquisition-1/spy.parquet"),
        dataset_payload(bucket="Invalid bucket"),
        dataset_payload(bucket="a..b"),
        dataset_payload(sha256="A" * 64),
        dataset_payload(manifest_sha256="short"),
        dataset_payload(completed_at="2024-03-29T12:00:00+01:00"),
    ],
)
def test_dataset_ref_rejects_unsafe_keys_hashes_and_non_utc_timestamps(
    payload: dict[str, object]
) -> None:
    with pytest.raises(ValidationError):
        DatasetRef.model_validate(payload)


def test_run_spec_derives_only_safe_run_locations_and_utc_creation_time() -> None:
    request = ResearchRequest.model_validate(request_payload())
    dataset = DatasetRef.model_validate(dataset_payload())
    run_id = UUID("123e4567-e89b-12d3-a456-426614174000")

    run_spec = RunSpec.create(
        request=request,
        dataset=dataset,
        image_digest="sha256:" + "c" * 64,
        now=datetime(2024, 3, 29, 12, 0, tzinfo=UTC),
        run_id=run_id,
    )

    assert run_spec.run_id == str(run_id)
    assert run_spec.run_spec_key == "runs/v1/123e4567-e89b-12d3-a456-426614174000/run-spec.json"
    assert run_spec.result_prefix == "runs/v1/123e4567-e89b-12d3-a456-426614174000/"
    assert run_spec.created_at.tzinfo is UTC


@pytest.mark.parametrize(
    ("run_id", "image_digest", "now"),
    [
        ("not-a-uuid", "sha256:" + "c" * 64, datetime(2024, 3, 29, 12, 0, tzinfo=UTC)),
        (
            "123e4567-e89b-12d3-a456-426614174000",
            "latest",
            datetime(2024, 3, 29, 12, 0, tzinfo=UTC),
        ),
        (
            "123e4567-e89b-12d3-a456-426614174000",
            "sha256:" + "C" * 64,
            datetime(2024, 3, 29, 12, 0, tzinfo=UTC),
        ),
        (
            "123e4567-e89b-12d3-a456-426614174000",
            "sha256:" + "c" * 64,
            datetime(2024, 3, 29, 12, 0),
        ),
    ],
)
def test_run_spec_rejects_invalid_uuid_image_digest_and_timestamps(
    run_id: str, image_digest: str, now: datetime
) -> None:
    payload = {
        "run_id": run_id,
        "dataset": dataset_payload(),
        "request": request_payload(),
        "image_digest": image_digest,
        "created_at": now.isoformat(),
        "maximum_runtime_seconds": 600,
        "run_spec_key": "runs/v1/123e4567-e89b-12d3-a456-426614174000/run-spec.json",
        "result_prefix": "runs/v1/123e4567-e89b-12d3-a456-426614174000/",
    }
    with pytest.raises(ValidationError):
        RunSpec.model_validate(payload)


def test_checksums_manifest_requires_exact_required_artifacts() -> None:
    artifacts = [
        {"name": name, "byte_length": 1, "sha256": "d" * 64}
        for name in (
            "run-spec.json",
            "result.json",
            "trades.parquet",
            "equity-curve.parquet",
            "report.html",
        )
    ]
    manifest = ChecksumsManifest.model_validate({"artifacts": artifacts})

    assert {artifact.name for artifact in manifest.artifacts} == {
        "run-spec.json",
        "result.json",
        "trades.parquet",
        "equity-curve.parquet",
        "report.html",
    }
    with pytest.raises(ValidationError):
        ChecksumsManifest.model_validate({"artifacts": artifacts[:-1]})
    with pytest.raises(ValidationError):
        ArtifactDigest.model_validate({"name": "result.json", "byte_length": 1, "sha256": "D" * 64})


def test_run_record_requires_canonical_uuid_and_utc_timestamps() -> None:
    record = RunRecord.model_validate(
        {
            "run_id": "123e4567-e89b-12d3-a456-426614174000",
            "status": "PENDING",
            "visibility": "PRIVATE",
            "dataset_key": "datasets/v1/acquisition-1/spy.parquet",
            "dataset_sha256": "a" * 64,
            "run_spec_key": "runs/v1/123e4567-e89b-12d3-a456-426614174000/run-spec.json",
            "result_prefix": "runs/v1/123e4567-e89b-12d3-a456-426614174000/",
            "image_digest": "sha256:" + "c" * 64,
            "created_at": "2024-03-29T12:00:00Z",
            "expires_at": 1_727_611_200,
        }
    )

    assert record.status is RunStatus.PENDING
    with pytest.raises(ValidationError):
        RunRecord.model_validate({**record.model_dump(), "run_id": "not-a-uuid"})


def test_canonical_json_is_deterministic_and_rejects_nonfinite_values() -> None:
    assert canonical_json_bytes({"z": 1, "a": "λ"}) == b'{"a":"\xce\xbb","z":1}'
    assert sha256_hex(b"contract") == (
        "cc8321d6375c494d043fdd0260f21bc0ec51dacc9f6abb7f909cdcd3041b78bf"
    )
    with pytest.raises(ValueError):
        canonical_json_bytes({"metric": float("inf")})
