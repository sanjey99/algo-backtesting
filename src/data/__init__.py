"""Data pipeline utilities."""
from __future__ import annotations

import pandas as pd

from src.models.candle import Candle


def df_to_candles(df: pd.DataFrame) -> list[Candle]:
    """Convert a normalised OHLCV DataFrame (from DataFetcher) to list[Candle].

    The DataFrame must have:
    - A DatetimeIndex (named 'timestamp' or 'date' or unnamed)
    - Columns: open, high, low, close, volume, adj_close
    """
    candles: list[Candle] = []
    for ts, row in df.iterrows():
        try:
            candles.append(
                Candle(
                    timestamp=pd.Timestamp(ts).to_pydatetime(),  # type: ignore[arg-type]
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["volume"]),
                    adj_close=float(row["adj_close"]),
                )
            )
        except (KeyError, ValueError):
            continue
    return candles
