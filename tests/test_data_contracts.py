"""Tests for the public market-data acquisition contracts."""
from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date, datetime

import pytest

from src.data.contracts import (
    AcquisitionRequest,
    AcquisitionStatus,
    CacheStatus,
    InvalidRequestError,
    Provider,
    QualityPolicy,
    QualitySeverity,
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
