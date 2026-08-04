"""Compatibility fetchers backed by the provider-adapter boundary.

New acquisition code should use :mod:`src.data.providers` and retain
``ProviderBatch`` values until normalization.  The classes here preserve the
original dataframe-returning API for dashboard, route, and store callers.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any

import pandas as pd

from src.data.contracts import AcquisitionRequest, DataAcquisitionError, ProviderSchemaError
from src.data.providers import AlphaVantageProvider, YFinanceProvider

DateLike = str | datetime


def _as_date(value: DateLike) -> date:
    if isinstance(value, str):
        return datetime.fromisoformat(value).date()
    return value.date()


class DataFetcher(ABC):
    """Legacy dataframe-returning fetcher contract."""

    @abstractmethod
    def fetch(
        self,
        symbol: str,
        start: DateLike,
        end: DateLike,
        interval: str = "1d",
    ) -> pd.DataFrame:
        """Fetch a normalized legacy OHLCV frame for an inclusive range."""

    @staticmethod
    def _normalise_columns(df: pd.DataFrame) -> pd.DataFrame:
        """Lower-case legacy columns without changing the adapter's raw frame."""
        normalized = df.copy()
        normalized.columns = [str(column).lower().strip() for column in normalized.columns]
        normalized = normalized.rename(
            columns={
                "adj close": "adj_close",
                "adjclose": "adj_close",
                "adjusted close": "adj_close",
            }
        )
        if normalized.index.name:
            normalized.index.name = str(normalized.index.name).lower()
        return normalized


class YFinanceFetcher(DataFetcher):
    """Compatibility wrapper for :class:`src.data.providers.YFinanceProvider`."""

    def fetch(
        self,
        symbol: str,
        start: DateLike,
        end: DateLike,
        interval: str = "1d",
    ) -> pd.DataFrame:
        try:
            request = AcquisitionRequest(symbol, _as_date(start), _as_date(end), interval=interval)
            raw = YFinanceProvider().fetch(request).frame
        except DataAcquisitionError as error:
            raise ValueError(str(error)) from error
        frame = raw.copy()
        if isinstance(frame.columns, pd.MultiIndex):
            frame.columns = frame.columns.get_level_values(0)
        normalized = self._normalise_columns(frame)
        if "adj_close" not in normalized.columns:
            if "close" not in normalized.columns:
                raise KeyError("Could not locate adjusted-close column in yfinance output.")
            normalized["adj_close"] = normalized["close"]
        canonical = ["open", "high", "low", "close", "volume", "adj_close"]
        normalized = normalized[[column for column in canonical if column in normalized.columns]]
        normalized.index.name = "timestamp"
        return normalized


class AlphaVantageFetcher(DataFetcher):
    """Compatibility wrapper for the adjusted-daily Alpha Vantage adapter."""

    def __init__(self) -> None:
        api_key = os.environ.get("ALPHA_VANTAGE_API_KEY", "").strip()
        if not api_key:
            raise ValueError("ALPHA_VANTAGE_API_KEY environment variable is not set")
        self._api_key = api_key
        # The legacy class historically attempted the endpoint with a key.  New
        # capability-aware orchestration uses AlphaVantageProvider directly and
        # must opt into entitlement explicitly.
        self._provider = AlphaVantageProvider(api_key=api_key, adjusted_daily_entitled=True)

    def _request(self, params: dict[str, str]) -> dict[str, Any]:
        """Legacy test seam delegating transport to the new provider adapter."""
        payload = self._provider.request(params)
        return dict(payload)

    def fetch(
        self,
        symbol: str,
        start: DateLike,
        end: DateLike,
        interval: str = "1d",
    ) -> pd.DataFrame:
        if interval != "1d":
            raise ValueError("Alpha Vantage adjusted-daily supports interval '1d' only")
        try:
            request = AcquisitionRequest(symbol, _as_date(start), _as_date(end), interval=interval)
            payload = self._request(
                {
                    "function": "TIME_SERIES_DAILY_ADJUSTED",
                    "symbol": request.symbol,
                    "outputsize": "full",
                    "datatype": "json",
                }
            )
            batch = self._provider.parse(request, self._with_legacy_action_defaults(payload))
        except DataAcquisitionError as error:
            raise ValueError(str(error)) from error
        return self._legacy_frame(batch.frame, request)

    @staticmethod
    def _legacy_frame(frame: pd.DataFrame, request: AcquisitionRequest) -> pd.DataFrame:
        indexed = frame.copy()
        try:
            indexed.index = pd.to_datetime(indexed.index)
        except (TypeError, ValueError) as error:
            raise ValueError("Alpha Vantage response contains invalid dates") from error
        in_range = (indexed.index.date >= request.start) & (indexed.index.date <= request.end)
        indexed = indexed[in_range]
        if indexed.empty:
            raise ValueError("Alpha Vantage returned no data for the requested range")
        columns = {
            "1. open": "open",
            "2. high": "high",
            "3. low": "low",
            "4. close": "close",
            "5. adjusted close": "adj_close",
            "6. volume": "volume",
        }
        try:
            result = indexed[list(columns)].rename(columns=columns).astype(float)
        except (KeyError, TypeError, ValueError) as error:
            message = "Alpha Vantage response cannot form a legacy OHLCV frame"
            raise ProviderSchemaError(message) from error
        result.index.name = "timestamp"
        return result.sort_index()

    @staticmethod
    def _with_legacy_action_defaults(payload: dict[str, Any]) -> dict[str, Any]:
        """Copy older legacy payloads and supply their historic action defaults.

        The new adapter correctly rejects adjusted-daily payloads without
        action fields.  The long-standing wrapper accepted six-field payloads,
        so it retains that API by enriching a private copy only.
        """
        copied = dict(payload)
        series = payload.get("Time Series (Daily)")
        if not isinstance(series, Mapping):
            return copied
        copied["Time Series (Daily)"] = {
            timestamp: (
                {
                    **values,
                    "7. dividend amount": values.get("7. dividend amount", "0.0"),
                    "8. split coefficient": values.get("8. split coefficient", "1.0"),
                }
                if isinstance(values, Mapping)
                else values
            )
            for timestamp, values in series.items()
        }
        return copied
