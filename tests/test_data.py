"""Tests for the Data Ingestion Layer.

All tests use synthetic data or mocks — no real network calls are made.
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from src.data.adjustments import (
    adjust_for_dividends,
    adjust_for_splits,
    validate_adjustment,
)
from src.data.fetcher import AlphaVantageFetcher, DataFetcher, YFinanceFetcher
from src.data.store import DataStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ohlcv(n: int = 5, base_price: float = 100.0) -> pd.DataFrame:
    """Return a synthetic OHLCV DataFrame with n rows."""
    dates = pd.date_range("2023-01-02", periods=n, freq="B")
    data = {
        "open": [base_price + i * 0.5 - 0.3 for i in range(n)],
        "high": [base_price + i * 0.5 + 0.5 for i in range(n)],
        "low": [base_price + i * 0.5 - 0.5 for i in range(n)],
        "close": [base_price + i * 0.5 for i in range(n)],
        "volume": [1_000_000.0 + i * 1000 for i in range(n)],
        "adj_close": [base_price + i * 0.5 for i in range(n)],
    }
    df = pd.DataFrame(data, index=dates)
    df.index.name = "timestamp"
    return df


# ---------------------------------------------------------------------------
# DataStore tests
# ---------------------------------------------------------------------------


class TestDataStore:
    """Round-trip save/load tests for DataStore."""

    def test_save_and_load_roundtrip(self, tmp_path: Path) -> None:
        """Data saved to parquet can be reloaded identically."""
        store = DataStore(cache_dir=str(tmp_path))
        df = _make_ohlcv(10)
        start = datetime(2023, 1, 2)
        end = datetime(2023, 1, 13)

        store.save("AAPL", start, end, df)
        loaded = store.get("AAPL", start, end)

        assert loaded is not None
        # Parquet does not preserve DatetimeIndex frequency metadata, so we
        # compare values only (check_freq=False).
        pd.testing.assert_frame_equal(df, loaded, check_freq=False)

    def test_get_returns_none_on_cache_miss(self, tmp_path: Path) -> None:
        """get() returns None when the parquet file does not exist."""
        store = DataStore(cache_dir=str(tmp_path))
        result = store.get("TSLA", datetime(2023, 1, 1), datetime(2023, 12, 31))
        assert result is None

    def test_cache_path_format(self, tmp_path: Path) -> None:
        """_cache_path produces the expected filename."""
        store = DataStore(cache_dir=str(tmp_path))
        path = store._cache_path("MSFT", datetime(2022, 6, 1), datetime(2022, 6, 30))
        assert path.name == "MSFT_2022-06-01_2022-06-30.parquet"
        assert path.parent == tmp_path

    def test_cache_dir_created_automatically(self, tmp_path: Path) -> None:
        """DataStore creates the cache directory if it doesn't exist."""
        new_dir = tmp_path / "nested" / "cache"
        assert not new_dir.exists()
        DataStore(cache_dir=str(new_dir))
        assert new_dir.exists()

    def test_fetch_or_cache_uses_cache_on_hit(self, tmp_path: Path) -> None:
        """fetch_or_cache returns cached data without calling the fetcher."""
        store = DataStore(cache_dir=str(tmp_path))
        df = _make_ohlcv(5)
        start = datetime(2023, 1, 2)
        end = datetime(2023, 1, 6)

        store.save("AAPL", start, end, df)

        mock_fetcher = MagicMock(spec=DataFetcher)
        result = store.fetch_or_cache("AAPL", start, end, mock_fetcher)

        mock_fetcher.fetch.assert_not_called()
        pd.testing.assert_frame_equal(df, result, check_freq=False)

    def test_fetch_or_cache_calls_fetcher_on_miss(self, tmp_path: Path) -> None:
        """fetch_or_cache invokes the fetcher on a cache miss and caches the result."""
        store = DataStore(cache_dir=str(tmp_path))
        start = datetime(2023, 1, 2)
        end = datetime(2023, 1, 6)
        fetched_df = _make_ohlcv(5)

        mock_fetcher = MagicMock(spec=DataFetcher)
        mock_fetcher.fetch.return_value = fetched_df

        result = store.fetch_or_cache("GOOG", start, end, mock_fetcher)

        mock_fetcher.fetch.assert_called_once_with("GOOG", start, end)
        pd.testing.assert_frame_equal(fetched_df, result, check_freq=False)

        # Subsequent call should use cache, not fetcher
        result2 = store.fetch_or_cache("GOOG", start, end, mock_fetcher)
        mock_fetcher.fetch.assert_called_once()  # still just once
        pd.testing.assert_frame_equal(fetched_df, result2, check_freq=False)

    def test_different_symbols_do_not_collide(self, tmp_path: Path) -> None:
        """Separate symbols have separate cache entries."""
        store = DataStore(cache_dir=str(tmp_path))
        start = datetime(2023, 1, 2)
        end = datetime(2023, 1, 6)

        df_aapl = _make_ohlcv(5, base_price=150.0)
        df_msft = _make_ohlcv(5, base_price=300.0)

        store.save("AAPL", start, end, df_aapl)
        store.save("MSFT", start, end, df_msft)

        loaded_aapl = store.get("AAPL", start, end)
        loaded_msft = store.get("MSFT", start, end)

        assert loaded_aapl is not None
        assert loaded_msft is not None
        pd.testing.assert_frame_equal(df_aapl, loaded_aapl, check_freq=False)
        pd.testing.assert_frame_equal(df_msft, loaded_msft, check_freq=False)


# ---------------------------------------------------------------------------
# adjust_for_splits tests
# ---------------------------------------------------------------------------


class TestAdjustForSplits:
    """Unit tests for adjust_for_splits."""

    def test_2_for_1_split_halves_prices(self) -> None:
        """A 2-for-1 split halves all price columns."""
        df = _make_ohlcv(3, base_price=200.0)
        original_close = df["close"].copy()

        result = adjust_for_splits(df, split_factor=2.0)

        pd.testing.assert_series_equal(
            result["close"],
            (original_close / 2.0).rename("close"),
        )

    def test_2_for_1_split_doubles_volume(self) -> None:
        """A 2-for-1 split doubles the volume column."""
        df = _make_ohlcv(3)
        original_volume = df["volume"].copy()

        result = adjust_for_splits(df, split_factor=2.0)

        pd.testing.assert_series_equal(
            result["volume"],
            (original_volume * 2.0).rename("volume"),
        )

    def test_all_price_columns_adjusted(self) -> None:
        """open, high, low, close, and adj_close are all halved for a 2:1 split."""
        df = _make_ohlcv(4, base_price=100.0)
        result = adjust_for_splits(df, split_factor=2.0)

        for col in ("open", "high", "low", "close", "adj_close"):
            pd.testing.assert_series_equal(
                result[col],
                (df[col] / 2.0).rename(col),
            )

    def test_original_df_not_mutated(self) -> None:
        """The input DataFrame is not modified in-place."""
        df = _make_ohlcv(3)
        original_close = df["close"].copy()
        adjust_for_splits(df, split_factor=3.0)
        pd.testing.assert_series_equal(df["close"], original_close)

    def test_reverse_split(self) -> None:
        """A 0.5 factor (1-for-2 reverse split) doubles prices and halves volume."""
        df = _make_ohlcv(3, base_price=50.0)
        result = adjust_for_splits(df, split_factor=0.5)

        pd.testing.assert_series_equal(
            result["close"],
            (df["close"] / 0.5).rename("close"),
        )
        pd.testing.assert_series_equal(
            result["volume"],
            (df["volume"] * 0.5).rename("volume"),
        )

    def test_invalid_split_factor_raises(self) -> None:
        """Negative or zero split factors raise ValueError."""
        df = _make_ohlcv(2)
        with pytest.raises(ValueError):
            adjust_for_splits(df, split_factor=0.0)
        with pytest.raises(ValueError):
            adjust_for_splits(df, split_factor=-1.0)


# ---------------------------------------------------------------------------
# adjust_for_dividends tests
# ---------------------------------------------------------------------------


class TestAdjustForDividends:
    """Unit tests for adjust_for_dividends."""

    def _make_straddled_df(self) -> tuple[pd.DataFrame, datetime]:
        """Return a 6-row DataFrame that straddles an ex-date in the middle."""
        dates = pd.date_range("2023-06-01", periods=6, freq="B")
        close_prices = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
        df = pd.DataFrame(
            {
                "open": [p - 0.3 for p in close_prices],
                "high": [p + 0.5 for p in close_prices],
                "low": [p - 0.5 for p in close_prices],
                "close": close_prices,
                "volume": [1_000_000.0] * 6,
                "adj_close": close_prices,
            },
            index=dates,
        )
        df.index.name = "timestamp"
        # ex-date falls on the 4th business day
        ex_date = dates[3].to_pydatetime()
        return df, ex_date

    def test_prices_before_ex_date_are_adjusted(self) -> None:
        """Prices strictly before ex_date are scaled down."""
        df, ex_date = self._make_straddled_df()
        dividend = 2.0
        result = adjust_for_dividends(df, dividend=dividend, ex_date=ex_date)

        # Reference close is the first close on or after ex_date = 103.0
        ref_close = 103.0
        factor = (ref_close - dividend) / ref_close  # ~0.9806

        # Rows 0, 1, 2 are before ex_date
        for i in range(3):
            expected = df["close"].iloc[i] * factor
            assert abs(result["close"].iloc[i] - expected) < 1e-9, (
                f"Row {i}: expected {expected}, got {result['close'].iloc[i]}"
            )

    def test_prices_on_or_after_ex_date_unchanged(self) -> None:
        """Prices on or after ex_date are not modified."""
        df, ex_date = self._make_straddled_df()
        result = adjust_for_dividends(df, dividend=2.0, ex_date=ex_date)

        # Rows 3, 4, 5 are on/after ex_date
        for i in range(3, 6):
            assert result["close"].iloc[i] == df["close"].iloc[i], (
                f"Row {i} should be unchanged"
            )

    def test_original_df_not_mutated(self) -> None:
        """The input DataFrame is not modified in-place."""
        df, ex_date = self._make_straddled_df()
        original_close = df["close"].copy()
        adjust_for_dividends(df, dividend=1.0, ex_date=ex_date)
        pd.testing.assert_series_equal(df["close"], original_close)

    def test_zero_dividend_leaves_prices_unchanged(self) -> None:
        """A dividend of 0 produces no change to prices."""
        df, ex_date = self._make_straddled_df()
        original_close = df["close"].copy()
        result = adjust_for_dividends(df, dividend=0.0, ex_date=ex_date)
        pd.testing.assert_series_equal(result["close"], original_close)

    def test_negative_dividend_raises(self) -> None:
        """Negative dividend raises ValueError."""
        df, ex_date = self._make_straddled_df()
        with pytest.raises(ValueError):
            adjust_for_dividends(df, dividend=-1.0, ex_date=ex_date)

    def test_ex_date_before_all_data_leaves_prices_unchanged(self) -> None:
        """If ex_date is before all rows, no prices are adjusted."""
        df, _ = self._make_straddled_df()
        early_ex = datetime(2000, 1, 1)
        original_close = df["close"].copy()
        result = adjust_for_dividends(df, dividend=1.0, ex_date=early_ex)
        pd.testing.assert_series_equal(result["close"], original_close)

    def test_ex_date_after_all_data_adjusts_all_prices(self) -> None:
        """If ex_date is after all rows, all prices are adjusted."""
        df, _ = self._make_straddled_df()
        late_ex = datetime(2099, 1, 1)
        result = adjust_for_dividends(df, dividend=2.0, ex_date=late_ex)

        # factor uses first close as reference (fallback since no row >= ex_date)
        ref_close = float(df["close"].iloc[0])
        factor = (ref_close - 2.0) / ref_close
        for i in range(len(df)):
            expected = df["close"].iloc[i] * factor
            assert abs(result["close"].iloc[i] - expected) < 1e-9


# ---------------------------------------------------------------------------
# validate_adjustment tests
# ---------------------------------------------------------------------------


class TestValidateAdjustment:
    """Unit tests for validate_adjustment."""

    def _make_df_with_gap(self, pct: float, at_index: int = 1) -> pd.DataFrame:
        """Return a small DataFrame with exactly one deliberate price gap.

        All rows before the gap are at 100.0; at *at_index* and after the
        price steps to 100*(1+pct), producing exactly one gap in pct_change.
        """
        dates = pd.date_range("2023-01-02", periods=5, freq="B")
        new_price = 100.0 * (1 + pct)
        closes = [100.0 if i < at_index else new_price for i in range(5)]
        df = pd.DataFrame({"close": closes}, index=dates)
        df.index.name = "timestamp"
        return df

    def test_flags_gap_above_threshold(self) -> None:
        """A 25% single-day gap is flagged by default threshold=0.20."""
        df = self._make_df_with_gap(0.25, at_index=2)
        warnings = validate_adjustment(df)
        assert len(warnings) == 1
        assert "25.0%" in warnings[0]

    def test_ignores_gap_below_threshold(self) -> None:
        """A 10% single-day gap is NOT flagged by default threshold=0.20."""
        df = self._make_df_with_gap(0.10, at_index=2)
        warnings = validate_adjustment(df)
        assert len(warnings) == 0

    def test_exact_threshold_not_flagged(self) -> None:
        """A gap equal to the threshold is not flagged (strictly greater than)."""
        df = self._make_df_with_gap(0.20, at_index=2)
        warnings = validate_adjustment(df)
        assert len(warnings) == 0

    def test_multiple_gaps_flagged(self) -> None:
        """Multiple gaps above threshold all appear in the warnings list."""
        dates = pd.date_range("2023-01-02", periods=5, freq="B")
        # Gaps at positions 1 and 3
        closes = [100.0, 130.0, 130.0, 170.0, 170.0]
        df = pd.DataFrame({"close": closes}, index=dates)
        df.index.name = "timestamp"
        warnings = validate_adjustment(df)
        assert len(warnings) == 2

    def test_custom_threshold(self) -> None:
        """A custom threshold of 0.05 flags a 10% gap."""
        df = self._make_df_with_gap(0.10, at_index=2)
        warnings = validate_adjustment(df, threshold=0.05)
        assert len(warnings) == 1

    def test_no_close_column_returns_empty(self) -> None:
        """DataFrame without a 'close' column returns an empty warning list."""
        df = pd.DataFrame({"price": [100.0, 110.0, 120.0]})
        warnings = validate_adjustment(df)
        assert warnings == []

    def test_single_row_returns_empty(self) -> None:
        """A single-row DataFrame cannot have a gap; returns empty list."""
        df = pd.DataFrame({"close": [100.0]}, index=pd.date_range("2023-01-02", periods=1))
        warnings = validate_adjustment(df)
        assert warnings == []

    def test_warning_message_format(self) -> None:
        """Warning strings contain 'Large price gap' and a date."""
        df = self._make_df_with_gap(0.50, at_index=2)
        warnings = validate_adjustment(df)
        assert len(warnings) == 1
        assert "Large price gap" in warnings[0]


# ---------------------------------------------------------------------------
# YFinanceFetcher tests (mocked)
# ---------------------------------------------------------------------------


class TestYFinanceFetcher:
    """Unit tests for YFinanceFetcher — yfinance.download is fully mocked."""

    def _mock_yf_output(self) -> pd.DataFrame:
        """Simulate the DataFrame returned by yfinance.download()."""
        dates = pd.date_range("2023-01-02", periods=5, freq="B")
        data = {
            "Open": [149.0, 150.0, 151.0, 152.0, 153.0],
            "High": [150.0, 151.0, 152.0, 153.0, 154.0],
            "Low": [148.0, 149.0, 150.0, 151.0, 152.0],
            "Close": [149.5, 150.5, 151.5, 152.5, 153.5],
            "Adj Close": [149.0, 150.0, 151.0, 152.0, 153.0],
            "Volume": [50_000_000, 51_000_000, 52_000_000, 53_000_000, 54_000_000],
        }
        df = pd.DataFrame(data, index=dates)
        df.index.name = "Date"
        return df

    @patch("yfinance.download")
    def test_returns_expected_columns(self, mock_download: Any) -> None:
        """YFinanceFetcher.fetch() returns exactly the 6 canonical columns."""
        mock_download.return_value = self._mock_yf_output()

        fetcher = YFinanceFetcher()
        result = fetcher.fetch(
            "AAPL",
            start=datetime(2023, 1, 2),
            end=datetime(2023, 1, 6),
        )

        assert set(result.columns) == {"open", "high", "low", "close", "volume", "adj_close"}

    @patch("yfinance.download")
    def test_index_is_datetime(self, mock_download: Any) -> None:
        """The returned DataFrame has a DatetimeIndex named 'timestamp'."""
        mock_download.return_value = self._mock_yf_output()

        fetcher = YFinanceFetcher()
        result = fetcher.fetch("AAPL", datetime(2023, 1, 2), datetime(2023, 1, 6))

        assert isinstance(result.index, pd.DatetimeIndex)
        assert result.index.name == "timestamp"

    @patch("yfinance.download")
    def test_column_values_are_numeric(self, mock_download: Any) -> None:
        """All columns contain numeric (float-compatible) values."""
        mock_download.return_value = self._mock_yf_output()

        fetcher = YFinanceFetcher()
        result = fetcher.fetch("AAPL", datetime(2023, 1, 2), datetime(2023, 1, 6))

        for col in result.columns:
            assert pd.api.types.is_numeric_dtype(result[col]), f"Column {col!r} is not numeric"

    @patch("yfinance.download")
    def test_empty_response_raises_value_error(self, mock_download: Any) -> None:
        """An empty yfinance response raises ValueError."""
        mock_download.return_value = pd.DataFrame()

        fetcher = YFinanceFetcher()
        with pytest.raises(ValueError):
            fetcher.fetch("BADTICKER", datetime(2023, 1, 2), datetime(2023, 1, 6))

    @patch("yfinance.download")
    def test_adj_close_maps_correctly(self, mock_download: Any) -> None:
        """'Adj Close' from yfinance maps to 'adj_close' in the output."""
        raw = self._mock_yf_output()
        mock_download.return_value = raw

        fetcher = YFinanceFetcher()
        result = fetcher.fetch("AAPL", datetime(2023, 1, 2), datetime(2023, 1, 6))

        # adj_close values should equal the original "Adj Close" column
        expected = raw["Adj Close"].values
        assert list(result["adj_close"].values) == list(expected)

    @patch("yfinance.download")
    def test_multiindex_columns_handled(self, mock_download: Any) -> None:
        """YFinanceFetcher handles MultiIndex columns (as produced by multi-ticker downloads)."""
        raw = self._mock_yf_output()
        # Simulate MultiIndex like ("Open", "AAPL"), ("High", "AAPL"), ...
        raw.columns = pd.MultiIndex.from_tuples(
            [(col, "AAPL") for col in raw.columns]
        )
        mock_download.return_value = raw

        fetcher = YFinanceFetcher()
        result = fetcher.fetch("AAPL", datetime(2023, 1, 2), datetime(2023, 1, 6))

        assert "close" in result.columns
        assert "adj_close" in result.columns


# ---------------------------------------------------------------------------
# AlphaVantageFetcher tests
# ---------------------------------------------------------------------------


class TestAlphaVantageFetcher:
    """Unit tests for AlphaVantageFetcher."""

    def test_raises_without_api_key(self) -> None:
        """AlphaVantageFetcher raises ValueError when API key is missing."""
        env = {k: v for k, v in os.environ.items() if k != "ALPHA_VANTAGE_API_KEY"}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(ValueError, match="ALPHA_VANTAGE_API_KEY"):
                AlphaVantageFetcher()

    def test_instantiates_with_api_key(self) -> None:
        """AlphaVantageFetcher instantiates successfully when API key is present."""
        with patch.dict(os.environ, {"ALPHA_VANTAGE_API_KEY": "DUMMYKEY"}):
            fetcher = AlphaVantageFetcher()
            assert fetcher._api_key == "DUMMYKEY"

    def test_fetch_returns_expected_columns(self) -> None:
        """AlphaVantageFetcher.fetch() returns the 6 canonical columns."""
        av_response = {
            "Time Series (Daily)": {
                "2023-01-03": {
                    "1. open": "130.0",
                    "2. high": "131.0",
                    "3. low": "129.0",
                    "4. close": "130.5",
                    "5. adjusted close": "129.0",
                    "6. volume": "80000000",
                },
                "2023-01-04": {
                    "1. open": "131.0",
                    "2. high": "132.0",
                    "3. low": "130.0",
                    "4. close": "131.5",
                    "5. adjusted close": "130.0",
                    "6. volume": "82000000",
                },
            }
        }

        with patch.dict(os.environ, {"ALPHA_VANTAGE_API_KEY": "DUMMYKEY"}):
            fetcher = AlphaVantageFetcher()

            with patch.object(fetcher, "_request", return_value=av_response):
                result = fetcher.fetch(
                    "AAPL",
                    start=datetime(2023, 1, 1),
                    end=datetime(2023, 1, 31),
                )

        assert set(result.columns) == {"open", "high", "low", "close", "volume", "adj_close"}

    def test_fetch_filters_by_date_range(self) -> None:
        """Only rows within [start, end] are returned."""
        av_response = {
            "Time Series (Daily)": {
                "2022-12-30": {
                    "1. open": "120.0", "2. high": "121.0", "3. low": "119.0",
                    "4. close": "120.5", "5. adjusted close": "120.0", "6. volume": "70000000",
                },
                "2023-01-03": {
                    "1. open": "130.0", "2. high": "131.0", "3. low": "129.0",
                    "4. close": "130.5", "5. adjusted close": "129.0", "6. volume": "80000000",
                },
                "2023-02-01": {
                    "1. open": "140.0", "2. high": "141.0", "3. low": "139.0",
                    "4. close": "140.5", "5. adjusted close": "139.0", "6. volume": "90000000",
                },
            }
        }

        with patch.dict(os.environ, {"ALPHA_VANTAGE_API_KEY": "DUMMYKEY"}):
            fetcher = AlphaVantageFetcher()
            with patch.object(fetcher, "_request", return_value=av_response):
                result = fetcher.fetch(
                    "AAPL",
                    start=datetime(2023, 1, 1),
                    end=datetime(2023, 1, 31),
                )

        assert len(result) == 1
        assert result.index[0] == pd.Timestamp("2023-01-03")

    def test_fetch_raises_on_no_data_in_range(self) -> None:
        """ValueError is raised when no rows fall within [start, end]."""
        av_response = {
            "Time Series (Daily)": {
                "2020-01-02": {
                    "1. open": "100.0", "2. high": "101.0", "3. low": "99.0",
                    "4. close": "100.5", "5. adjusted close": "100.0", "6. volume": "1000000",
                },
            }
        }

        with patch.dict(os.environ, {"ALPHA_VANTAGE_API_KEY": "DUMMYKEY"}):
            fetcher = AlphaVantageFetcher()
            with patch.object(fetcher, "_request", return_value=av_response):
                with pytest.raises(ValueError):
                    fetcher.fetch(
                        "AAPL",
                        start=datetime(2023, 1, 1),
                        end=datetime(2023, 12, 31),
                    )
