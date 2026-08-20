"""Offline startup smoke coverage for the Streamlit dashboard."""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, date, datetime
from typing import cast

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from src.dashboard.app import _fetch_candles
from src.data.acquisition import AcquisitionService
from src.data.contracts import (
    AcquisitionManifest,
    AcquisitionRequest,
    AcquisitionResult,
    AcquisitionStatus,
    QualityError,
)


class _RecordingAcquisitionService:
    def __init__(self, result: AcquisitionResult) -> None:
        self._result = result
        self.requests: tuple[AcquisitionRequest, ...] = ()

    def acquire(self, request: AcquisitionRequest) -> AcquisitionResult:
        self.requests = (*self.requests, request)
        return self._result


class _RejectingAcquisitionService:
    def acquire(self, request: AcquisitionRequest) -> AcquisitionResult:
        del request
        raise QualityError("canonical quality policy rejected the data")


def _successful_acquisition() -> AcquisitionResult:
    request = AcquisitionRequest("SPY", date(2024, 1, 2), date(2024, 1, 3))
    completed_at = datetime(2024, 1, 4, tzinfo=UTC)
    frame = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2024-01-02", "2024-01-03"]),
            "symbol": ["SPY", "SPY"],
            "open": [100.0, 101.0],
            "high": [102.0, 103.0],
            "low": [99.0, 100.0],
            "close": [101.0, 102.0],
            "volume": [1_000_000.0, 1_100_000.0],
            "adj_close": [101.0, 102.0],
            "dividend_amount": [0.0, 0.0],
            "split_coefficient": [1.0, 1.0],
            "source": ["yfinance", "yfinance"],
        }
    )
    return AcquisitionResult(
        frame,
        AcquisitionManifest(
            "dashboard-test",
            request,
            AcquisitionStatus.SUCCESS,
            started_at=completed_at,
            completed_at=completed_at,
        ),
    )


def test_dashboard_starts_offline_with_expected_controls() -> None:
    app = AppTest.from_file("src/dashboard/app.py", default_timeout=10).run()

    assert not app.exception
    assert [title.value for title in app.title] == ["📈 Algorithmic Backtester"]
    assert app.selectbox[0].value == "ma_crossover"
    assert app.selectbox[0].options == [
        "Ma Crossover",
        "Rsi Mean Reversion",
        "Breakout",
    ]
    assert app.text_input[0].value == "SPY"
    assert [button.label for button in app.button] == ["▶ Run Backtest"]
    assert [message.value for message in app.info] == [
        "Configure and click **▶ Run Backtest** to get started."
    ]


def test_dashboard_fetches_candles_through_canonical_acquisition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _RecordingAcquisitionService(_successful_acquisition())
    monkeypatch.setattr(
        "src.data.wiring.get_acquisition_service",
        lambda: cast(AcquisitionService, service),
    )

    candles = _fetch_candles(" spy ", "2024-01-02", "2024-01-03")

    assert service.requests == (
        AcquisitionRequest("SPY", date(2024, 1, 2), date(2024, 1, 3)),
    )
    assert [(candle.timestamp.date(), candle.close) for candle in candles] == [
        (date(2024, 1, 2), 101.0),
        (date(2024, 1, 3), 102.0),
    ]


def test_dashboard_does_not_hide_canonical_quality_failure() -> None:
    with pytest.raises(QualityError, match="quality policy rejected"):
        _fetch_candles(
            "SPY",
            "2024-01-02",
            "2024-01-03",
            service=cast(AcquisitionService, _RejectingAcquisitionService()),
        )


def test_injected_dashboard_acquisition_does_not_load_database_configuration() -> None:
    script = """
from typing import cast

from src.dashboard.app import _fetch_candles
from src.data.acquisition import AcquisitionService
from src.data.contracts import AcquisitionRequest, AcquisitionResult, QualityError


class RejectingService:
    def acquire(self, request: AcquisitionRequest) -> AcquisitionResult:
        del request
        raise QualityError("expected rejection")


try:
    _fetch_candles(
        "SPY",
        "2024-01-02",
        "2024-01-03",
        service=cast(AcquisitionService, RejectingService()),
    )
except QualityError:
    pass
else:
    raise AssertionError("quality failure did not propagate")
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        env={**os.environ, "DATABASE_URL": "invalid://"},
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
