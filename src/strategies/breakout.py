"""Donchian Channel Breakout strategy."""
from __future__ import annotations

from collections import deque
from typing import Any

from src.engine.context import StrategyContext
from src.engine.event import SignalEvent
from src.models.candle import Candle
from src.models.order import Direction, OrderType
from src.strategies.base import BaseStrategy


class BreakoutStrategy(BaseStrategy):
    """Long when close breaks above Donchian channel high; exit on channel low break.

    The Donchian channel upper band = highest high of the last N bars.
    The lower band = lowest low of the last N bars.

    Entry: close > rolling max high (breakout) → LONG
    Exit: close < rolling min low (breakdown) → SHORT (exit)

    Interview talking point:
      Donchian channels are used by the original Turtle Traders. The channel
      width is a direct proxy for recent volatility — tighter channels signal
      range-bound markets; wider channels signal trending ones.
    """

    name = "breakout"
    parameter_space: dict[str, tuple[float, float, float]] = {
        "lookback": (10, 60, 5),
    }

    def __init__(self, lookback: int = 20) -> None:
        self.lookback = lookback
        self.parameters: dict[str, Any] = {"lookback": lookback}
        self._highs: deque[float] = deque(maxlen=lookback)
        self._lows: deque[float] = deque(maxlen=lookback)
        self._prev_high: float | None = None
        self._prev_low: float | None = None

    def on_candle(
        self, candle: Candle, context: StrategyContext
    ) -> SignalEvent | None:
        # Record previous channel levels BEFORE updating
        if len(self._highs) >= self.lookback:
            self._prev_high = max(self._highs)
            self._prev_low = min(self._lows)

        self._highs.append(candle.high)
        self._lows.append(candle.low)

        if (
            len(self._highs) < self.lookback
            or self._prev_high is None
            or self._prev_low is None
        ):
            return None

        signal: SignalEvent | None = None

        if (
            context.position_direction is None
            and context.pending_direction is None
            and candle.close > self._prev_high
        ):
            signal = SignalEvent(
                symbol="",
                direction=Direction.LONG,
                strength=1.0,
                timestamp=candle.timestamp,
                order_type=OrderType.STOP,
                stop_price=self._prev_high,
            )
        elif (
            context.position_direction is Direction.LONG
            and not context.forced_cover_pending
            and candle.close < self._prev_low
        ):
            signal = SignalEvent(
                symbol="",
                direction=Direction.SHORT,
                strength=1.0,
                timestamp=candle.timestamp,
            )

        return signal

    def reset(self) -> None:
        self._highs.clear()
        self._lows.clear()
        self._prev_high = None
        self._prev_low = None
