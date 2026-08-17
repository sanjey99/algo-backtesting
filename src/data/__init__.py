"""Data pipeline utilities."""

from __future__ import annotations

import math
from typing import Any, cast

import pandas as pd

from src.data.contracts import ContractViolationError
from src.models.candle import Candle

_CANDLE_COLUMNS = ("open", "high", "low", "close", "volume", "adj_close")


def df_to_candles(df: pd.DataFrame) -> list[Candle]:
    """Convert every row to a Candle or fail the whole boundary atomically.

    Canonical acquisition frames carry ``timestamp`` as a column. Legacy
    ``DataFetcher`` frames with a ``DatetimeIndex`` remain accepted during the
    migration, but malformed rows are never skipped.
    """
    if not isinstance(df, pd.DataFrame):
        raise ContractViolationError("Candle conversion requires a DataFrame")
    missing = [column for column in _CANDLE_COLUMNS if column not in df.columns]
    if missing:
        raise ContractViolationError(f"Candle frame is missing field {missing[0]!r}")
    timestamp_column = "timestamp" in df.columns
    if not timestamp_column and not isinstance(df.index, pd.DatetimeIndex):
        raise ContractViolationError("Candle frame is missing field 'timestamp'")

    candles: list[Candle] = []
    for position, (index_value, row) in enumerate(df.iterrows()):
        try:
            timestamp_value = row["timestamp"] if timestamp_column else index_value
            timestamp = pd.Timestamp(cast(Any, timestamp_value))
            if pd.isna(timestamp):
                raise ValueError("timestamp is missing")
            values = {column: float(row[column]) for column in _CANDLE_COLUMNS}
            if not all(math.isfinite(value) for value in values.values()):
                raise ValueError("Candle values must be finite")
            if any(values[column] <= 0 for column in ("open", "high", "low", "close", "adj_close")):
                raise ValueError("Candle prices must be positive")
            if values["volume"] < 0:
                raise ValueError("Candle volume must be non-negative")
            candles.append(
                Candle(
                    timestamp=timestamp.to_pydatetime(),
                    open=values["open"],
                    high=values["high"],
                    low=values["low"],
                    close=values["close"],
                    volume=values["volume"],
                    adj_close=values["adj_close"],
                )
            )
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            raise ContractViolationError(f"Candle conversion failed at row {position}") from error
    return candles
