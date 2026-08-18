"""RSI Mean Reversion strategy using incremental Wilder smoothing."""
from __future__ import annotations

from typing import Any

from src.engine.context import StrategyContext
from src.engine.event import SignalEvent
from src.models.candle import Candle
from src.models.order import Direction
from src.strategies.base import BaseStrategy


class RSIMeanReversionStrategy(BaseStrategy):
    """Buy when RSI falls below oversold threshold; exit when RSI recovers to 50.

    RSI is seeded once, then updated with Wilder's exponential smoothing:
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
        self._previous_price: float | None = None
        self._seed_gain_total = 0.0
        self._seed_loss_total = 0.0
        self._change_count = 0
        self._avg_gain: float | None = None
        self._avg_loss: float | None = None
        self._rsi: float | None = None

    @staticmethod
    def _rsi_from_averages(avg_gain: float, avg_loss: float) -> float:
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    def on_candle(
        self, candle: Candle, context: StrategyContext
    ) -> SignalEvent | None:
        if self._previous_price is None:
            self._previous_price = candle.adj_close
            return None

        change = candle.adj_close - self._previous_price
        self._previous_price = candle.adj_close
        gain = max(change, 0.0)
        loss = max(-change, 0.0)
        self._change_count += 1

        if self._avg_gain is None or self._avg_loss is None:
            self._seed_gain_total += gain
            self._seed_loss_total += loss
            if self._change_count < self.period:
                return None
            self._avg_gain = self._seed_gain_total / self.period
            self._avg_loss = self._seed_loss_total / self.period
        else:
            self._avg_gain = (self._avg_gain * (self.period - 1) + gain) / self.period
            self._avg_loss = (self._avg_loss * (self.period - 1) + loss) / self.period

        self._rsi = self._rsi_from_averages(self._avg_gain, self._avg_loss)

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
        self._previous_price = None
        self._seed_gain_total = 0.0
        self._seed_loss_total = 0.0
        self._change_count = 0
        self._avg_gain = None
        self._avg_loss = None
        self._rsi = None
