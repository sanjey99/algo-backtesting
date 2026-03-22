"""Data fetcher abstractions and concrete implementations.

Provides a DataFetcher ABC plus two concrete implementations:
- YFinanceFetcher  — wraps yfinance.download()
- AlphaVantageFetcher — wraps the Alpha Vantage TIME_SERIES_DAILY_ADJUSTED endpoint
"""
from __future__ import annotations

import logging
import os
import time
from abc import ABC, abstractmethod
from datetime import datetime

import pandas as pd
import requests

logger = logging.getLogger(__name__)


_ALPHA_VANTAGE_BASE = "https://www.alphavantage.co/query"
_ALPHA_VANTAGE_CALLS_PER_MIN = 5
_ALPHA_VANTAGE_MIN_INTERVAL = 60.0 / _ALPHA_VANTAGE_CALLS_PER_MIN  # seconds between calls


class DataFetcher(ABC):
    """Abstract base class for market-data fetchers.

    All concrete implementations must return a :class:`pandas.DataFrame` with:
    - A :class:`pandas.DatetimeIndex` named ``timestamp``
    - Lowercase columns: ``open``, ``high``, ``low``, ``close``, ``volume``, ``adj_close``
    """

    @abstractmethod
    def fetch(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        interval: str = "1d",
    ) -> pd.DataFrame:
        """Fetch OHLCV data for *symbol* between *start* and *end*.

        Parameters
        ----------
        symbol:
            Ticker symbol (e.g. ``"AAPL"``).
        start:
            Inclusive start date/time.
        end:
            Inclusive end date/time.
        interval:
            Bar interval string (e.g. ``"1d"``, ``"1h"``).  Interpretation is
            provider-specific.

        Returns
        -------
        pd.DataFrame
            DataFrame with DatetimeIndex and columns
            ``open, high, low, close, volume, adj_close``.
        """

    # ------------------------------------------------------------------
    # Helpers shared by subclasses
    # ------------------------------------------------------------------

    @staticmethod
    def _normalise_columns(df: pd.DataFrame) -> pd.DataFrame:
        """Lower-case all column names and rename *adj close* variants."""
        df = df.copy()
        df.columns = [c.lower().strip() for c in df.columns]
        # Rename common variant spellings produced by data providers
        rename_map = {
            "adj close": "adj_close",
            "adjclose": "adj_close",
            "adjusted close": "adj_close",
        }
        df = df.rename(columns=rename_map)
        if df.index.name:
            df.index.name = df.index.name.lower()
        return df


class YFinanceFetcher(DataFetcher):
    """Fetches data from Yahoo Finance via :mod:`yfinance`."""

    def fetch(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        interval: str = "1d",
    ) -> pd.DataFrame:
        import yfinance as yf  # lazy import so the class is mock-friendly in tests

        logger.warning(
            "YFinanceFetcher: %s data from Yahoo Finance may be subject to "
            "survivorship bias — only currently-listed companies are available.",
            symbol,
        )

        raw: pd.DataFrame = yf.download(
            symbol,
            start=start,
            end=end,
            interval=interval,
            auto_adjust=False,
            progress=False,
        )

        if raw.empty:
            raise ValueError(f"yfinance returned no data for {symbol!r}")

        # yfinance may return a MultiIndex when downloading a single ticker
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)

        df = self._normalise_columns(raw)

        # Ensure adj_close exists (auto_adjust=False preserves "Adj Close")
        if "adj_close" not in df.columns:
            if "close" in df.columns:
                df["adj_close"] = df["close"]
            else:
                raise KeyError("Could not locate adjusted-close column in yfinance output.")

        # Keep only the canonical columns (drop extras like "dividends", "stock splits")
        canonical = ["open", "high", "low", "close", "volume", "adj_close"]
        df = df[[c for c in canonical if c in df.columns]]

        df.index.name = "timestamp"
        return df


class AlphaVantageFetcher(DataFetcher):
    """Fetches daily-adjusted data from the Alpha Vantage REST API.

    Reads the API key from the ``ALPHA_VANTAGE_API_KEY`` environment variable.
    Enforces a rate limit of 5 calls per minute using :func:`time.sleep`.

    Raises
    ------
    ValueError
        If ``ALPHA_VANTAGE_API_KEY`` is not set in the environment.
    """

    def __init__(self) -> None:
        api_key = os.environ.get("ALPHA_VANTAGE_API_KEY", "").strip()
        if not api_key:
            raise ValueError(
                "ALPHA_VANTAGE_API_KEY environment variable is not set. "
                "Obtain a free key at https://www.alphavantage.co/support/#api-key"
            )
        self._api_key = api_key
        self._last_call_ts: float = 0.0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _rate_limit(self) -> None:
        """Sleep if needed to stay within the 5-calls/min rate limit."""
        elapsed = time.monotonic() - self._last_call_ts
        if elapsed < _ALPHA_VANTAGE_MIN_INTERVAL:
            time.sleep(_ALPHA_VANTAGE_MIN_INTERVAL - elapsed)
        self._last_call_ts = time.monotonic()

    def _request(self, params: dict[str, str]) -> dict:  # type: ignore[type-arg]
        """Make a rate-limited GET request and return the JSON body."""
        self._rate_limit()
        params["apikey"] = self._api_key
        response = requests.get(_ALPHA_VANTAGE_BASE, params=params, timeout=30)
        response.raise_for_status()
        data: dict = response.json()  # type: ignore[type-arg]
        if "Error Message" in data:
            raise ValueError(f"Alpha Vantage API error: {data['Error Message']}")
        if "Note" in data:
            # Rate-limit note from the API
            raise RuntimeError(f"Alpha Vantage rate-limit note: {data['Note']}")
        return data

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def fetch(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        interval: str = "1d",
    ) -> pd.DataFrame:
        """Fetch daily adjusted OHLCV data from Alpha Vantage.

        Note: Alpha Vantage free tier only supports daily intervals via
        ``TIME_SERIES_DAILY_ADJUSTED``. The *interval* parameter is accepted
        for API compatibility but only ``"1d"`` is currently supported.
        """
        # Coerce start/end to datetime (R-09: fix str/datetime comparison)
        start_dt: datetime = datetime.fromisoformat(str(start)) if isinstance(start, str) else start
        end_dt: datetime = datetime.fromisoformat(str(end)) if isinstance(end, str) else end

        logger.info("Fetching %s from Alpha Vantage (%s to %s)", symbol, start_dt.date(), end_dt.date())

        params = {
            "function": "TIME_SERIES_DAILY_ADJUSTED",
            "symbol": symbol,
            "outputsize": "full",
            "datatype": "json",
        }
        data = self._request(params)

        key = "Time Series (Daily)"
        if key not in data:
            raise ValueError(
                f"Unexpected Alpha Vantage response structure for {symbol!r}. "
                f"Keys: {list(data.keys())}"
            )

        ts_data: dict[str, dict[str, str]] = data[key]
        rows = []
        for date_str, values in ts_data.items():
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            if start_dt <= dt <= end_dt:
                rows.append(
                    {
                        "timestamp": dt,
                        "open": float(values["1. open"]),
                        "high": float(values["2. high"]),
                        "low": float(values["3. low"]),
                        "close": float(values["4. close"]),
                        "volume": float(values["6. volume"]),
                        "adj_close": float(values["5. adjusted close"]),
                    }
                )

        if not rows:
            raise ValueError(
                f"Alpha Vantage returned no data for {symbol!r} "
                f"between {start_dt.date()} and {end_dt.date()}."
            )

        df = pd.DataFrame(rows).set_index("timestamp").sort_index()
        df.index = pd.to_datetime(df.index)
        df.index.name = "timestamp"
        return df
