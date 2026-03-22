"""Moving-Average Crossover strategy."""
from __future__ import annotations

from collections import deque
from typing import Any, Optional

from src.engine.event import SignalEvent
from src.models.candle import Candle
from src.models.order import Direction
from src.strategies.base import BaseStrategy


class MACrossover(BaseStrategy):
    """Emit LONG on golden cross (fast MA crosses above slow MA) and
    SHORT on death cross (fast MA crosses below slow MA).

    Parameters
    ----------
    fast_period : int
        Lookback window for the fast moving average (default 10).
    slow_period : int
        Lookback window for the slow moving average (default 30).
    """

    name: str = "ma_crossover"

    def __init__(
        self,
        fast_period: int = 10,
        slow_period: int = 30,
        symbol: str = "UNKNOWN",
    ) -> None:
        super().__init__()
        self.parameters: dict[str, Any] = {
            "fast_period": fast_period,
            "slow_period": slow_period,
        }
        self.parameter_space: dict[str, tuple[float, float, float]] = {
            "fast_period": (5.0, 50.0, 5.0),
            "slow_period": (20.0, 200.0, 10.0),
        }
        self.symbol = symbol

        self._fast_period = fast_period
        self._slow_period = slow_period
        self._fast_window: deque[float] = deque(maxlen=fast_period)
        self._slow_window: deque[float] = deque(maxlen=slow_period)
        self._prev_fast_ma: Optional[float] = None
        self._prev_slow_ma: Optional[float] = None

    # ------------------------------------------------------------------
    # BaseStrategy interface
    # ------------------------------------------------------------------

    def on_candle(self, candle: Candle) -> Optional[SignalEvent]:
        self._fast_window.append(candle.close)
        self._slow_window.append(candle.close)

        if (
            len(self._fast_window) < self._fast_period
            or len(self._slow_window) < self._slow_period
        ):
            self._prev_fast_ma = None
            self._prev_slow_ma = None
            return None

        fast_ma = sum(self._fast_window) / self._fast_period
        slow_ma = sum(self._slow_window) / self._slow_period

        signal: Optional[SignalEvent] = None

        if self._prev_fast_ma is not None and self._prev_slow_ma is not None:
            prev_above = self._prev_fast_ma > self._prev_slow_ma
            curr_above = fast_ma > slow_ma

            if not prev_above and curr_above:
                # Golden cross — emit LONG
                signal = SignalEvent(
                    symbol=self.symbol,
                    direction=Direction.LONG,
                    timestamp=candle.timestamp,
                )
            elif prev_above and not curr_above:
                # Death cross — emit SHORT
                signal = SignalEvent(
                    symbol=self.symbol,
                    direction=Direction.SHORT,
                    timestamp=candle.timestamp,
                )

        self._prev_fast_ma = fast_ma
        self._prev_slow_ma = slow_ma
        return signal

    def reset(self) -> None:
        self._fast_window.clear()
        self._slow_window.clear()
        self._prev_fast_ma = None
        self._prev_slow_ma = None
