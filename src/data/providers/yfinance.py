"""Single-operation yfinance adapter returning provider-native batches."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import cast

import pandas as pd

from src.data.contracts import (
    AcquisitionRequest,
    ActionCoverage,
    Provider,
    ProviderBatch,
    ProviderCapabilities,
    ProviderSchemaError,
    TransientProviderError,
)
from src.data.providers.base import ProviderEligibility

Download = Callable[..., pd.DataFrame]

_REQUIRED_COLUMNS = frozenset({"open", "high", "low", "close", "adj close", "volume"})


def _column_names(frame: pd.DataFrame) -> frozenset[str]:
    columns = frame.columns
    if isinstance(columns, pd.MultiIndex):
        columns = columns.get_level_values(0)
    return frozenset(str(column).strip().lower() for column in columns)


class YFinanceProvider:
    """Fetch daily prices from yfinance while retaining its native frame shape."""

    def __init__(
        self,
        *,
        download: Download | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._download = download
        self._now = now or (lambda: datetime.now(UTC))

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider=Provider.YFINANCE,
            supports_actions=True,
            supports_full_history=True,
        )

    def eligibility(self, request: AcquisitionRequest) -> ProviderEligibility:
        if request.interval not in self.capabilities.supported_intervals:
            return ProviderEligibility(False, "unsupported interval")
        return ProviderEligibility(True)

    def fetch(self, request: AcquisitionRequest) -> ProviderBatch:
        """Perform one yfinance download and validate only its provider shape."""
        eligibility = self.eligibility(request)
        if not eligibility.eligible:
            raise ProviderSchemaError(eligibility.reason or "ineligible yfinance request")
        download = self._download or self._default_download()
        try:
            frame = download(
                request.symbol,
                start=request.start,
                end=request.end + timedelta(days=1),
                interval=request.interval,
                actions=True,
                auto_adjust=False,
                progress=False,
            )
        except (ProviderSchemaError, TransientProviderError):
            raise
        except Exception as error:
            raise TransientProviderError("yfinance download failed") from error
        self._validate_frame(frame)
        timezone = getattr(frame.index, "tz", None)
        return ProviderBatch(
            provider=Provider.YFINANCE,
            request=request,
            frame=frame,
            received_at=self._now(),
            native_timezone=str(timezone) if timezone is not None else None,
            raw_row_count=len(frame),
            response_metadata={
                "endpoint": "yfinance.download",
                "actions_requested": True,
                "auto_adjust": False,
            },
            action_coverage=ActionCoverage.REPRESENTED,
        )

    @staticmethod
    def _default_download() -> Download:
        import yfinance

        return cast(Download, yfinance.download)

    @staticmethod
    def _validate_frame(frame: object) -> None:
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            raise ProviderSchemaError("yfinance returned an empty or non-tabular response")
        if not _REQUIRED_COLUMNS.issubset(_column_names(frame)):
            raise ProviderSchemaError("yfinance response is missing required daily price fields")
