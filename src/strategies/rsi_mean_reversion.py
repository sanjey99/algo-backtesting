"""RSI Mean Reversion strategy — computed from scratch (no TA-Lib)."""
from __future__ import annotations

from collections import deque
from typing import Any

from src.engine.context import StrategyContext
from src.engine.event import SignalEvent
from src.models.candle import Candle
from src.models.order import Direction
from src.strategies.base import BaseStrategy


class RSIMeanReversionStrategy(BaseStrategy):
    """Buy when RSI falls below oversold threshold; exit when RSI recovers to 50.

    RSI is computed from scratch using Wilder's exponential smoothing:
      avg_gain = ((prev_avg_gain * (period-1)) + gain) / period
      avg_loss = ((prev_avg_loss * (period-1)) + loss) / period
      RS = avg_gain / avg_loss
      RSI = 100 - (100 / (1 + RS))

    Interview talking point:
      Computing RSI from scratch (not via ta-lib or pandas_ta) demonstrates
      understanding of the underlying math — a standard quant interview signal.
    """

    name = "rsi_mean_reversion"
    parameter_space: dict[str, tuple[float, float, float]] = {
        "period": (7, 21, 2),
        "oversold": (20, 40, 5),
    }

    def __init__(
        self,
        period: int = 14,
        oversold: float = 30.0,
        exit_level: float = 50.0,
    ) -> None:
        self.period = period
        self.oversold = oversold
        self.exit_level = exit_level
        self.parameters: dict[str, Any] = {
            "period": period,
            "oversold": oversold,
            "exit_level": exit_level,
        }
        self._prices: deque[float] = deque(maxlen=period + 1)
        self._avg_gain: float | None = None
        self._avg_loss: float | None = None
        self._rsi: float | None = None
        self._bar_count: int = 0

    def _compute_rsi(self, prices: list[float]) -> float:
        """Compute RSI from a price series using Wilder's smoothing."""
        gains: list[float] = []
        losses: list[float] = []
        for i in range(1, len(prices)):
            diff = prices[i] - prices[i - 1]
            gains.append(max(diff, 0.0))
            losses.append(max(-diff, 0.0))

        if not gains:
            return 50.0

        # Seed with simple average over first period
        avg_gain = sum(gains[:self.period]) / self.period
        avg_loss = sum(losses[:self.period]) / self.period

        # Wilder's smoothing for remaining periods
        for gain, loss in zip(gains[self.period:], losses[self.period:]):
            avg_gain = (avg_gain * (self.period - 1) + gain) / self.period
            avg_loss = (avg_loss * (self.period - 1) + loss) / self.period

        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    def on_candle(
        self, candle: Candle, context: StrategyContext
    ) -> SignalEvent | None:
        self._prices.append(candle.adj_close)
        self._bar_count += 1

        if self._bar_count < self.period + 1:
            return None

        self._rsi = self._compute_rsi(list(self._prices))

        signal: SignalEvent | None = None

        if (
            context.position_direction is None
            and context.pending_direction is None
            and self._rsi < self.oversold
        ):
            signal = SignalEvent(
                symbol="",
                direction=Direction.LONG,
                strength=(self.oversold - self._rsi) / self.oversold,
                timestamp=candle.timestamp,
            )
        elif (
            context.position_direction is Direction.LONG
            and not context.forced_cover_pending
            and self._rsi >= self.exit_level
        ):
            signal = SignalEvent(
                symbol="",
                direction=Direction.SHORT,
                strength=1.0,
                timestamp=candle.timestamp,
            )

        return signal

    def reset(self) -> None:
        self._prices.clear()
        self._avg_gain = None
        self._avg_loss = None
        self._rsi = None
        self._bar_count = 0
