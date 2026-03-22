"""Moving Average Crossover strategy — fast/slow SMA crossover."""
from __future__ import annotations

from collections import deque
from typing import Any

from src.engine.event import SignalEvent
from src.models.candle import Candle
from src.models.order import Direction
from src.strategies.base import BaseStrategy


class MACrossoverStrategy(BaseStrategy):
    """Long when fast SMA crosses above slow SMA; exit (short signal) when it crosses below.

    Uses simple arithmetic mean (SMA), not EMA, for simplicity and reproducibility.
    Warmup period = slow_period bars during which no signals are emitted.

    Interview talking point:
      MA crossover generates 10-100 trades on SPY over a multi-year period —
      enough for statistical analysis but not so many as to be noise-driven.
    """

    name = "ma_crossover"
    parameter_space: dict[str, tuple[float, float, float]] = {
        "fast_period": (5, 50, 5),
        "slow_period": (20, 200, 10),
    }

    def __init__(self, fast_period: int = 10, slow_period: int = 50) -> None:
        if fast_period >= slow_period:
            raise ValueError(
                f"fast_period ({fast_period}) must be < slow_period ({slow_period})"
            )
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.parameters: dict[str, Any] = {
            "fast_period": fast_period,
            "slow_period": slow_period,
        }
        self._fast_window: deque[float] = deque(maxlen=fast_period)
        self._slow_window: deque[float] = deque(maxlen=slow_period)
        self._prev_fast_sma: float | None = None
        self._prev_slow_sma: float | None = None
        self._in_position: bool = False

    def on_candle(self, candle: Candle) -> SignalEvent | None:
        price = candle.adj_close
        self._fast_window.append(price)
        self._slow_window.append(price)

        # Not enough data yet
        if len(self._slow_window) < self.slow_period:
            return None

        fast_sma = sum(self._fast_window) / len(self._fast_window)
        slow_sma = sum(self._slow_window) / len(self._slow_window)

        signal: SignalEvent | None = None

        if self._prev_fast_sma is not None and self._prev_slow_sma is not None:
            was_above = self._prev_fast_sma > self._prev_slow_sma
            is_above = fast_sma > slow_sma

            if not was_above and is_above and not self._in_position:
                # Golden cross — go LONG
                signal = SignalEvent(
                    symbol=candle.timestamp.strftime("%Y-%m-%d"),
                    direction=Direction.LONG,
                    strength=1.0,
                    timestamp=candle.timestamp,
                )
                self._in_position = True
            elif was_above and not is_above and self._in_position:
                # Death cross — exit LONG (SHORT signal closes position)
                signal = SignalEvent(
                    symbol=candle.timestamp.strftime("%Y-%m-%d"),
                    direction=Direction.SHORT,
                    strength=1.0,
                    timestamp=candle.timestamp,
                )
                self._in_position = False

        self._prev_fast_sma = fast_sma
        self._prev_slow_sma = slow_sma
        return signal

    def reset(self) -> None:
        self._fast_window.clear()
        self._slow_window.clear()
        self._prev_fast_sma = None
        self._prev_slow_sma = None
        self._in_position = False
