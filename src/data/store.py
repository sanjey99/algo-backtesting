"""Parquet-backed local cache for OHLCV DataFrames.

The :class:`DataStore` handles persistence so that repeated back-tests
over the same date range do not require redundant network calls.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd

from src.data.fetcher import DataFetcher


class DataStore:
    """Simple parquet-backed cache for market data.

    Directory layout::

        <cache_dir>/
            AAPL_2023-01-01_2023-12-31.parquet
            MSFT_2022-01-01_2022-12-31.parquet
            ...

    Parameters
    ----------
    cache_dir:
        Path to the directory where parquet files are stored.
        Defaults to ``"data/raw"``.  Created automatically if it does not
        exist.
    """

    def __init__(self, cache_dir: str = "data/raw") -> None:
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def _cache_path(self, symbol: str, start: datetime, end: datetime) -> Path:
        """Return the canonical parquet file path for the given parameters."""
        fname = f"{symbol}_{start.date()}_{end.date()}.parquet"
        return self._cache_dir / fname

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame | None:
        """Load a cached DataFrame from parquet, or return ``None`` on cache miss.

        Parameters
        ----------
        symbol:
            Ticker symbol.
        start:
            Start of the requested range.
        end:
            End of the requested range.

        Returns
        -------
        pd.DataFrame or None
            The cached DataFrame if the parquet file exists, otherwise ``None``.
        """
        path = self._cache_path(symbol, start, end)
        if not path.exists():
            return None
        df: pd.DataFrame = pd.read_parquet(path)
        return df

    def save(self, symbol: str, start: datetime, end: datetime, df: pd.DataFrame) -> None:
        """Persist *df* to a parquet file.

        Parameters
        ----------
        symbol:
            Ticker symbol.
        start:
            Start of the stored range (used only to construct the filename).
        end:
            End of the stored range (used only to construct the filename).
        df:
            DataFrame to persist.
        """
        path = self._cache_path(symbol, start, end)
        df.to_parquet(path)

    def fetch_or_cache(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        fetcher: DataFetcher,
    ) -> pd.DataFrame:
        """Return cached data if available; otherwise fetch, cache, and return.

        Parameters
        ----------
        symbol:
            Ticker symbol.
        start:
            Inclusive start date/time.
        end:
            Inclusive end date/time.
        fetcher:
            A :class:`~src.data.fetcher.DataFetcher` instance used to
            retrieve data on a cache miss.

        Returns
        -------
        pd.DataFrame
            OHLCV DataFrame, sourced from cache or freshly fetched.
        """
        cached = self.get(symbol, start, end)
        if cached is not None:
            return cached

        df = fetcher.fetch(symbol, start, end)
        self.save(symbol, start, end, df)
        return df
