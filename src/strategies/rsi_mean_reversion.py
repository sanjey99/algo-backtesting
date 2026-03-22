"""RSI Mean-Reversion strategy."""
from __future__ import annotations

from typing import Any, Optional

from src.engine.event import SignalEvent
from src.models.candle import Candle
from src.models.order import Direction
from src.strategies.base import BaseStrategy


class RSIMeanReversion(BaseStrategy):
    """Emit LONG when RSI drops below *oversold* and SHORT when RSI
    rises above *exit_level*.

    Parameters
    ----------
    period : int
        RSI lookback window (default 14).
    oversold : float
        RSI level below which we buy (default 30).
    exit_level : float
        RSI level above which we sell (default 70).
    """

    name: str = "rsi_mean_reversion"

    def __init__(
        self,
        period: int = 14,
        oversold: float = 30.0,
        exit_level: float = 70.0,
        symbol: str = "UNKNOWN",
    ) -> None:
        super().__init__()
        self.parameters: dict[str, Any] = {
            "period": period,
            "oversold": oversold,
            "exit_level": exit_level,
        }
        self.parameter_space: dict[str, tuple[float, float, float]] = {
            "period": (5.0, 30.0, 1.0),
            "oversold": (20.0, 40.0, 5.0),
            "exit_level": (60.0, 80.0, 5.0),
        }
        self.symbol = symbol

        self._period = period
        self.oversold = oversold
        self.exit_level = exit_level
        self._closes: list[float] = []
        self._rsi: Optional[float] = None

    # ------------------------------------------------------------------
    # BaseStrategy interface
    # ------------------------------------------------------------------

    def on_candle(self, candle: Candle) -> Optional[SignalEvent]:
        self._closes.append(candle.close)
        if len(self._closes) < self._period + 1:
            return None

        self._rsi = self._calc_rsi()

        if self._rsi < self.oversold:
            return SignalEvent(
                symbol=self.symbol,
                direction=Direction.LONG,
                timestamp=candle.timestamp,
            )
        if self._rsi >= self.exit_level:
            return SignalEvent(
                symbol=self.symbol,
                direction=Direction.SHORT,
                timestamp=candle.timestamp,
            )
        return None

    def reset(self) -> None:
        self._closes = []
        self._rsi = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _calc_rsi(self) -> float:
        """Compute RSI using Wilder's smoothing over the last *period* changes."""
        changes = [
            self._closes[i] - self._closes[i - 1]
            for i in range(len(self._closes) - self._period, len(self._closes))
        ]
        gains = [c for c in changes if c > 0]
        losses = [-c for c in changes if c < 0]

        avg_gain = sum(gains) / self._period
        avg_loss = sum(losses) / self._period

        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))
