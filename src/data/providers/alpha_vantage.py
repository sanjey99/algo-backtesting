"""Capability-aware Alpha Vantage adjusted-daily adapter.

The optional budget and pacing controls are per adapter instance in this
process.  They are intentionally not a distributed or globally coordinated
rate limiter; upstream provider responses remain authoritative.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

import pandas as pd
import requests

from src.data.contracts import (
    AcquisitionRequest,
    ActionCoverage,
    Provider,
    ProviderAuthenticationError,
    ProviderBatch,
    ProviderCapabilities,
    ProviderEntitlementError,
    ProviderQuotaError,
    ProviderSchemaError,
    TransientProviderError,
)
from src.data.providers.base import ProviderEligibility

_BASE_URL = "https://www.alphavantage.co/query"
_DAILY_FUNCTION = "TIME_SERIES_DAILY_ADJUSTED"
_SERIES_KEY = "Time Series (Daily)"
_REQUIRED_FIELDS = frozenset(
    {
        "1. open",
        "2. high",
        "3. low",
        "4. close",
        "5. adjusted close",
        "6. volume",
        "7. dividend amount",
        "8. split coefficient",
    }
)


class HttpResponse(Protocol):
    headers: Mapping[str, str]

    def raise_for_status(self) -> None: ...

    def json(self) -> object: ...


HttpGet = Callable[..., HttpResponse]


class AlphaVantageProvider:
    """Fetch one adjusted-daily response after capability and local-limit checks."""

    def __init__(
        self,
        *,
        api_key: str | None,
        adjusted_daily_entitled: bool,
        output_size: str = "full",
        compact_lookback_days: int = 100,
        daily_budget: int = 25,
        min_interval_seconds: float = 0.0,
        http_get: HttpGet | None = None,
        monotonic_clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        today: Callable[[], date] = date.today,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        key = (api_key or "").strip()
        if output_size not in {"compact", "full"}:
            raise ValueError("output_size must be 'compact' or 'full'")
        if compact_lookback_days < 1 or daily_budget < 1 or min_interval_seconds < 0:
            raise ValueError("Alpha Vantage budget and pacing settings must be positive")
        self._api_key = key
        self._adjusted_daily_entitled = adjusted_daily_entitled
        self._output_size = output_size
        self._compact_lookback_days = compact_lookback_days
        self._daily_budget = daily_budget
        self._min_interval_seconds = min_interval_seconds
        self._http_get = http_get or requests.get
        self._monotonic_clock = monotonic_clock
        self._sleeper = sleeper
        self._today = today
        self._now = now or (lambda: datetime.now(UTC))
        self._calls_today = 0
        self._budget_day: date | None = None
        self._last_call_at: float | None = None

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider=Provider.ALPHA_VANTAGE,
            supports_actions=True,
            requires_api_key=True,
            supports_full_history=self._output_size == "full",
            output_size=self._output_size,
        )

    def eligibility(self, request: AcquisitionRequest) -> ProviderEligibility:
        if request.interval not in self.capabilities.supported_intervals:
            return ProviderEligibility(False, "unsupported interval")
        if not self._api_key:
            return ProviderEligibility(False, "missing API key")
        if not self._adjusted_daily_entitled:
            return ProviderEligibility(False, "adjusted-daily entitlement is required")
        if self._output_size == "compact":
            earliest = self._today() - timedelta(days=self._compact_lookback_days)
            if request.start < earliest:
                return ProviderEligibility(False, "compact output cannot cover requested history")
        return ProviderEligibility(True)

    def request(self, params: Mapping[str, str], *, interval: str = "1d") -> Mapping[str, Any]:
        """Perform the configured transport call without mutating caller parameters."""
        if interval != "1d":
            raise ProviderSchemaError("Alpha Vantage adjusted-daily supports interval '1d' only")
        request_params = dict(params)
        request_params["apikey"] = self._api_key
        self._apply_local_budget_and_pacing()
        try:
            response = self._http_get(_BASE_URL, params=request_params, timeout=30)
            response.raise_for_status()
            payload = response.json()
        except requests.HTTPError as error:
            raise self._map_http_error(error) from error
        except requests.RequestException as error:
            raise TransientProviderError("Alpha Vantage request failed") from error
        except (TypeError, ValueError) as error:
            raise ProviderSchemaError("Alpha Vantage returned invalid JSON") from error
        if not isinstance(payload, Mapping):
            raise ProviderSchemaError("Alpha Vantage returned a non-object JSON response")
        return payload

    def fetch(self, request: AcquisitionRequest) -> ProviderBatch:
        """Perform one HTTP operation, then parse one adjusted-daily response."""
        eligibility = self.eligibility(request)
        if not eligibility.eligible:
            raise ProviderEntitlementError(eligibility.reason or "ineligible Alpha Vantage request")
        payload = self.request(
            {
                "function": _DAILY_FUNCTION,
                "symbol": request.symbol,
                "outputsize": self._output_size,
                "datatype": "json",
            },
            interval=request.interval,
        )
        return self.parse(request, payload)

    def parse(self, request: AcquisitionRequest, payload: Mapping[str, Any]) -> ProviderBatch:
        """Validate a provider envelope and retain its native numbered fields."""
        if "Error Message" in payload:
            raise ProviderSchemaError("Alpha Vantage rejected the request")
        if "Note" in payload:
            raise ProviderQuotaError("Alpha Vantage reported a quota limit")
        if "Information" in payload:
            raise ProviderEntitlementError("Alpha Vantage reported unavailable entitlement")
        series = payload.get(_SERIES_KEY)
        if not isinstance(series, Mapping) or not series:
            raise ProviderSchemaError("Alpha Vantage response is missing daily adjusted data")
        self._validate_series(series)
        native_rows: dict[str, dict[str, Any]] = {}
        for timestamp, values in series.items():
            if isinstance(timestamp, str) and isinstance(values, Mapping):
                native_rows[timestamp] = {str(key): value for key, value in values.items()}
        frame = pd.DataFrame.from_dict(native_rows, orient="index")
        return ProviderBatch(
            provider=Provider.ALPHA_VANTAGE,
            request=request,
            frame=frame,
            received_at=self._now(),
            native_timezone=None,
            raw_row_count=len(frame),
            response_metadata={
                "endpoint": "Alpha Vantage TIME_SERIES_DAILY_ADJUSTED",
                "output_size": self._output_size,
            },
            action_coverage=ActionCoverage.REPRESENTED,
        )

    def _apply_local_budget_and_pacing(self) -> None:
        current_day = self._today()
        if self._budget_day != current_day:
            self._budget_day = current_day
            self._calls_today = 0
        if self._calls_today >= self._daily_budget:
            raise ProviderQuotaError("Alpha Vantage local daily budget is exhausted")
        now = self._monotonic_clock()
        if self._last_call_at is not None:
            remaining = self._min_interval_seconds - (now - self._last_call_at)
            if remaining > 0:
                self._sleeper(remaining)
                now = self._monotonic_clock()
        self._last_call_at = now
        self._calls_today += 1

    @staticmethod
    def _validate_series(series: Mapping[object, object]) -> None:
        for timestamp, values in series.items():
            if not isinstance(timestamp, str) or not isinstance(values, Mapping):
                raise ProviderSchemaError("Alpha Vantage daily data has an invalid row shape")
            if not _REQUIRED_FIELDS.issubset({str(key) for key in values}):
                raise ProviderSchemaError("Alpha Vantage daily data is missing required fields")

    @staticmethod
    def _map_http_error(error: requests.HTTPError) -> Exception:
        response = error.response
        if response is None:
            return TransientProviderError("Alpha Vantage request failed")
        status = response.status_code
        if status in {401, 403}:
            return ProviderAuthenticationError("Alpha Vantage authentication failed")
        if status == 429:
            mapped = TransientProviderError("Alpha Vantage temporarily throttled the request")
            header = response.headers.get("Retry-After")
            try:
                setattr(
                    mapped, "retry_after_seconds", float(header) if header is not None else None
                )
            except ValueError:
                setattr(mapped, "retry_after_seconds", None)
            return mapped
        if 500 <= status <= 599:
            return TransientProviderError("Alpha Vantage server error")
        return ProviderSchemaError("Alpha Vantage HTTP request was rejected")
