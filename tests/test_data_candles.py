"""Strict tests for the canonical DataFrame-to-Candle boundary."""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from src.data import df_to_candles
from src.data.contracts import ContractViolationError


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2024-01-02", "2024-01-03"]),
            "symbol": pd.Series(["SPY", "SPY"], dtype="string"),
            "open": [100.0, 101.0],
            "high": [102.0, 103.0],
            "low": [99.0, 100.0],
            "close": [101.5, 102.5],
            "volume": [1_000.0, 2_000.0],
            "adj_close": [101.25, 102.25],
            "dividend_amount": [0.0, 0.0],
            "split_coefficient": [1.0, 1.0],
            "source": pd.Series(["yfinance", "yfinance"], dtype="string"),
        }
    )


def test_df_to_candles_converts_every_row_in_exact_order() -> None:
    candles = df_to_candles(_frame())

    assert [candle.timestamp for candle in candles] == [
        datetime(2024, 1, 2),
        datetime(2024, 1, 3),
    ]
    assert [
        (
            candle.open,
            candle.high,
            candle.low,
            candle.close,
            candle.volume,
            candle.adj_close,
        )
        for candle in candles
    ] == [
        (100.0, 102.0, 99.0, 101.5, 1_000.0, 101.25),
        (101.0, 103.0, 100.0, 102.5, 2_000.0, 102.25),
    ]


@pytest.mark.parametrize(
    "missing",
    ["timestamp", "open", "high", "low", "close", "volume", "adj_close"],
)
def test_df_to_candles_rejects_missing_fields(missing: str) -> None:
    with pytest.raises(ContractViolationError, match=missing):
        df_to_candles(_frame().drop(columns=[missing]))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("timestamp", "not-a-date"),
        ("open", "not-a-number"),
        ("close", float("nan")),
        ("high", float("inf")),
        ("volume", -1.0),
        ("low", 105.0),
    ],
)
def test_df_to_candles_rejects_invalid_rows_without_skipping(field: str, value: object) -> None:
    frame = _frame()
    frame[field] = frame[field].astype(object)
    frame.loc[0, field] = value

    with pytest.raises(ContractViolationError, match="row 0"):
        df_to_candles(frame)


def test_df_to_candles_keeps_legacy_datetime_index_compatibility() -> None:
    frame = _frame().drop(
        columns=["timestamp", "symbol", "dividend_amount", "split_coefficient", "source"]
    )
    frame.index = pd.DatetimeIndex(["2024-01-02", "2024-01-03"], name="timestamp")

    assert len(df_to_candles(frame)) == 2
