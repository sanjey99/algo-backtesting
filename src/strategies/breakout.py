"""Breakout strategy — buy on new high, sell on new low."""
from __future__ import annotations

from collections import deque
from typing import Any, Optional

from src.engine.event import SignalEvent
from src.models.candle import Candle
from src.models.order import Direction
from src.strategies.base import BaseStrategy


class Breakout(BaseStrategy):
    """Emit LONG when close breaks above the prior *period*-bar high.
    Emit SHORT when close breaks below the prior *period*-bar low.

    Parameters
    ----------
    period : int
        Lookback window for computing the high/low channel (default 20).
    """

    name: str = "breakout"

    def __init__(
        self,
        period: int = 20,
        symbol: str = "UNKNOWN",
    ) -> None:
        super().__init__()
        self.parameters: dict[str, Any] = {"period": period}
        self.parameter_space: dict[str, tuple[float, float, float]] = {
            "period": (5.0, 60.0, 5.0),
        }
        self.symbol = symbol

        self._period = period
        self._highs: deque[float] = deque(maxlen=period)
        self._lows: deque[float] = deque(maxlen=period)
        self._prev_high: Optional[float] = None
        self._prev_low: Optional[float] = None

    # ------------------------------------------------------------------
    # BaseStrategy interface
    # ------------------------------------------------------------------

    def on_candle(self, candle: Candle) -> Optional[SignalEvent]:
        # Capture previous channel before updating
        prev_high = self._prev_high
        prev_low = self._prev_low

        # Update rolling high/low with completed bar
        self._highs.append(candle.high)
        self._lows.append(candle.low)

        if len(self._highs) >= self._period:
            self._prev_high = max(self._highs)
            self._prev_low = min(self._lows)

        if prev_high is None or prev_low is None:
            return None

        if candle.close > prev_high:
            return SignalEvent(
                symbol=self.symbol,
                direction=Direction.LONG,
                timestamp=candle.timestamp,
            )
        if candle.close < prev_low:
            return SignalEvent(
                symbol=self.symbol,
                direction=Direction.SHORT,
                timestamp=candle.timestamp,
            )
        return None

    def reset(self) -> None:
        self._highs.clear()
        self._lows.clear()
        self._prev_high = None
        self._prev_low = None
