"""Tests for the public market-data acquisition contracts."""
from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date, datetime

import pandas as pd
import pytest

from src.data.contracts import (
    AcquisitionManifest,
    AcquisitionRequest,
    AcquisitionStatus,
    ActionCoverage,
    AttemptEvidence,
    CacheStatus,
    InvalidRequestError,
    Provider,
    ProviderBatch,
    QualityFinding,
    QualityPolicy,
    QualitySeverity,
    RejectedRow,
    RetryPolicy,
    SourcePreference,
)


class TestAcquisitionRequest:
    def test_normalizes_a_safe_symbol_and_date_values(self) -> None:
        request = AcquisitionRequest(
            symbol=" spy ",
            start=datetime(2024, 1, 2, 16, 0),
            end=date(2024, 1, 31),
        )

        assert request.symbol == "SPY"
        assert request.start == date(2024, 1, 2)
        assert request.end == date(2024, 1, 31)
        assert request.interval == "1d"
        assert request.calendar == "XNYS"
        assert request.source is SourcePreference.AUTO

    @pytest.mark.parametrize("symbol", ["", "   ", "../SPY", "SPY/../../secret", "SPY\\data"])
    def test_rejects_unsafe_symbols(self, symbol: str) -> None:
        with pytest.raises(InvalidRequestError):
            AcquisitionRequest(symbol=symbol, start=date(2024, 1, 2), end=date(2024, 1, 3))

    def test_rejects_an_inverted_range(self) -> None:
        with pytest.raises(InvalidRequestError, match="start"):
            AcquisitionRequest(symbol="SPY", start=date(2024, 1, 3), end=date(2024, 1, 2))

    @pytest.mark.parametrize(
        ("field", "value"),
        [("interval", "1h"), ("calendar", "NYSE"), ("source", "unknown")],
    )
    def test_rejects_unsupported_v1_options(self, field: str, value: str) -> None:
        arguments = {"symbol": "SPY", "start": date(2024, 1, 2), "end": date(2024, 1, 3)}
        arguments[field] = value

        with pytest.raises(InvalidRequestError):
            AcquisitionRequest(**arguments)


def test_public_enums_have_stable_string_values() -> None:
    assert {item.value for item in Provider} == {"yfinance", "alpha_vantage"}
    assert {item.value for item in SourcePreference} == {"auto", "yfinance", "alpha_vantage"}
    assert {item.value for item in AcquisitionStatus} == {"success", "partial_success", "failed"}
    assert {item.value for item in CacheStatus} == {
        "miss",
        "full_hit",
        "partial_hit",
        "stale_refresh",
        "forced_refresh",
        "invalidated",
    }
    assert {item.value for item in QualitySeverity} == {"info", "warning", "fatal"}


def test_policies_have_documented_defaults_and_are_immutable() -> None:
    quality = QualityPolicy()
    retry = RetryPolicy()

    assert quality.minimum_coverage == 0.98
    assert quality.max_consecutive_missing_sessions == 2
    assert quality.action_relative_tolerance == 1e-9
    assert quality.action_absolute_tolerance == 1e-12
    assert retry.max_attempts == 3
    assert retry.base_delay_seconds == 0.5
    assert retry.max_delay_seconds == 8.0

    with pytest.raises(FrozenInstanceError):
        quality.minimum_coverage = 0.5  # type: ignore[misc]


def test_contract_metadata_and_error_evidence_are_recursively_redacted() -> None:
    request = AcquisitionRequest("SPY", date(2024, 1, 2), date(2024, 1, 3))
    redaction_value = "redaction-test-value"
    batch = ProviderBatch(
        provider=Provider.YFINANCE,
        request=request,
        frame=pd.DataFrame(),
        response_metadata={
            "headers": {"Authorization": f"Bearer {redaction_value}"},
            "callback": f"https://example.test/data?apikey={redaction_value}&page=1",
        },
        action_coverage=ActionCoverage.REPRESENTED,
    )
    manifest = AcquisitionManifest(
        acquisition_id="acquisition-1",
        request=request,
        status=AcquisitionStatus.FAILED,
        environment_versions={"nested": {"x-api-key": redaction_value}},
        attempts=(
            AttemptEvidence(
                provider=Provider.YFINANCE,
                attempt_number=1,
                started_at=datetime(2024, 1, 3),
                duration_seconds=0.1,
                outcome="failed",
                error_message=f"request failed: https://example.test/?token={redaction_value}",
            ),
        ),
        findings=(
            QualityFinding(
                QualitySeverity.FATAL,
                "provider_error",
                "provider error",
                {"response": {"password": redaction_value}},
            ),
        ),
        rejected_rows=(RejectedRow(1, "bad row", details={"secret": redaction_value}),),
    )

    serialized = f"{batch.to_dict()} {manifest.to_dict()}"

    assert redaction_value not in serialized
    assert "[REDACTED]" in serialized
    assert "page=1" in serialized


def test_contract_mapping_evidence_is_defensively_immutable() -> None:
    request = AcquisitionRequest("SPY", date(2024, 1, 2), date(2024, 1, 3))
    metadata = {"nested": {"value": "before"}}
    details = {"nested": {"value": "before"}}
    environment = {"python": "3.12"}
    counters = {"accepted": 1}
    batch = ProviderBatch(Provider.YFINANCE, request, pd.DataFrame(), response_metadata=metadata)
    finding = QualityFinding(QualitySeverity.INFO, "mapped", "mapped", details)
    rejected = RejectedRow(1, "bad row", details=details)
    manifest = AcquisitionManifest(
        "acquisition-1",
        request,
        AcquisitionStatus.SUCCESS,
        environment_versions=environment,
        counters=counters,
    )

    metadata["nested"]["value"] = "after"
    details["nested"]["value"] = "after"
    environment["python"] = "3.13"
    counters["accepted"] = 2

    assert batch.response_metadata["nested"]["value"] == "before"
    assert finding.details["nested"]["value"] == "before"
    assert rejected.details["nested"]["value"] == "before"
    assert manifest.environment_versions["python"] == "3.12"
    assert manifest.counters["accepted"] == 1
    with pytest.raises(TypeError):
        batch.response_metadata["new"] = "value"  # type: ignore[index]
    with pytest.raises(TypeError):
        manifest.counters["accepted"] = 2  # type: ignore[index]
