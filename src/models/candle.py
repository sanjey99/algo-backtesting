"""Candle — immutable OHLCV bar. The atomic unit of market data."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Candle:
    """A single OHLCV price bar.

    Frozen so strategies cannot mutate historical data mid-loop.
    adj_close carries corporate-action-adjusted close; close is raw.
    """

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    adj_close: float

    def __post_init__(self) -> None:
        if not (self.low <= self.open <= self.high):
            raise ValueError(
                f"OHLC invariant violated: low={self.low} open={self.open} high={self.high}"
            )
        if not (self.low <= self.close <= self.high):
            raise ValueError(
                f"OHLC invariant violated: low={self.low} close={self.close} high={self.high}"
            )
        if self.volume < 0:
            raise ValueError(f"Volume cannot be negative: {self.volume}")

    @property
    def typical_price(self) -> float:
        """(H+L+C)/3 — used by some indicators."""
        return (self.high + self.low + self.close) / 3.0

    @property
    def spread(self) -> float:
        return self.high - self.low
