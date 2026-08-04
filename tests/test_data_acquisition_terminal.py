"""Terminal acquisition failure integration tests."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from src.data.contracts import (
    AcquisitionRequest,
    ContractViolationError,
    Provider,
)
from tests.test_data_acquisition import FakeProvider, native_batch, service


def test_default_terminal_error_is_archived_without_fallback(tmp_path: Path) -> None:
    request = AcquisitionRequest("AAPL", date(2024, 1, 9), date(2024, 1, 10))
    yfinance = FakeProvider(
        Provider.YFINANCE, lambda _: (_ for _ in ()).throw(RuntimeError("unexpected"))
    )
    alpha = FakeProvider(
        Provider.ALPHA_VANTAGE, lambda item: native_batch(Provider.ALPHA_VANTAGE, item)
    )
    acquisition, _, repository = service(
        tmp_path, {Provider.YFINANCE: lambda: yfinance, Provider.ALPHA_VANTAGE: lambda: alpha}
    )

    with pytest.raises(RuntimeError):
        acquisition.acquire(request)

    assert alpha.requests == []
    report = repository.lookup("acquisition-1")
    assert report is not None
    assert report["status"] == "failed"


def test_mismatched_provider_batch_is_terminal_without_fallback(tmp_path: Path) -> None:
    request = AcquisitionRequest("AAPL", date(2024, 1, 9), date(2024, 1, 10))
    yfinance = FakeProvider(
        Provider.YFINANCE, lambda item: native_batch(Provider.ALPHA_VANTAGE, item)
    )
    alpha = FakeProvider(
        Provider.ALPHA_VANTAGE, lambda item: native_batch(Provider.ALPHA_VANTAGE, item)
    )
    acquisition, _, repository = service(
        tmp_path, {Provider.YFINANCE: lambda: yfinance, Provider.ALPHA_VANTAGE: lambda: alpha}
    )

    with pytest.raises(ContractViolationError):
        acquisition.acquire(request)

    assert alpha.requests == []
    report = repository.lookup("acquisition-1")
    assert report is not None
    assert report["status"] == "failed"
