"""RSI Mean-Reversion strategy — incremental Wilder's RSI, O(1) per bar."""
from __future__ import annotations

from typing import Any, Optional

from src.engine.event import SignalEvent
from src.models.candle import Candle
from src.models.order import Direction
from src.strategies.base import BaseStrategy


class RSIMeanReversionStrategy(BaseStrategy):
    """Emit LONG when RSI drops below *oversold* and SHORT when RSI
    rises above *exit_level*.

    Uses true incremental Wilder's smoothing — O(1) per bar after the
    seeding phase (first ``period`` price changes).

    Parameters
    ----------
    period : int
        RSI lookback window (default 14).
    oversold : float
        RSI level below which we buy (default 30).
    exit_level : float
        RSI level above which we sell (default 50).
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
        super().__init__()
        self.period = period
        self.oversold = oversold
        self.exit_level = exit_level
        self.parameters: dict[str, Any] = {
            "period": period,
            "oversold": oversold,
            "exit_level": exit_level,
        }
        # Seeding phase: collect first `period` price changes
        self._seed_gains: list[float] = []
        self._seed_losses: list[float] = []
        self._prev_price: float | None = None
        self._avg_gain: float | None = None
        self._avg_loss: float | None = None
        self._rsi: float | None = None
        self._bar_count: int = 0

    # ------------------------------------------------------------------
    # BaseStrategy interface
    # ------------------------------------------------------------------

    def on_candle(self, candle: Candle) -> Optional[SignalEvent]:
        price = candle.adj_close
        self._bar_count += 1

        if self._prev_price is None:
            # First bar — just record price
            self._prev_price = price
            return None

        gain = max(price - self._prev_price, 0.0)
        loss = max(self._prev_price - price, 0.0)
        self._prev_price = price

        if self._avg_gain is None:
            # Phase 1: accumulate seed data
            self._seed_gains.append(gain)
            self._seed_losses.append(loss)

            if len(self._seed_gains) == self.period:
                # Seed complete — compute initial averages
                self._avg_gain = sum(self._seed_gains) / self.period
                self._avg_loss = sum(self._seed_losses) / self.period
                # Clear seed lists (no longer needed)
                self._seed_gains.clear()
                self._seed_losses.clear()
                self._rsi = self._rsi_from_averages()
        else:
            # Phase 2: incremental Wilder update
            self._avg_gain = (self._avg_gain * (self.period - 1) + gain) / self.period
            self._avg_loss = (self._avg_loss * (self.period - 1) + loss) / self.period
            self._rsi = self._rsi_from_averages()

        if self._rsi is None:
            return None

        signal: Optional[SignalEvent] = None
        if self._rsi < self.oversold:
            signal = SignalEvent(
                symbol=self.symbol,
                direction=Direction.LONG,
                strength=(self.oversold - self._rsi) / self.oversold,
                timestamp=candle.timestamp,
            )
        elif self._rsi >= self.exit_level:
            signal = SignalEvent(
                symbol=self.symbol,
                direction=Direction.SHORT,
                strength=1.0,
                timestamp=candle.timestamp,
            )
        return signal

    def reset(self) -> None:
        self._seed_gains.clear()
        self._seed_losses.clear()
        self._prev_price = None
        self._avg_gain = None
        self._avg_loss = None
        self._rsi = None
        self._bar_count = 0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _rsi_from_averages(self) -> float:
        """Compute RSI from current avg_gain and avg_loss."""
        if self._avg_loss == 0.0:
            return 100.0
        rs = self._avg_gain / self._avg_loss  # type: ignore[operator]
        return 100.0 - (100.0 / (1.0 + rs))


# ---------------------------------------------------------------------------
# Backwards-compatible alias (existing code that imports RSIMeanReversion
# by the old name will still work).
# ---------------------------------------------------------------------------
RSIMeanReversion = RSIMeanReversionStrategy
