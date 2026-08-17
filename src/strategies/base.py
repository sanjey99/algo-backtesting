"""BaseStrategy — ABC that all concrete strategies inherit from."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from src.engine.context import StrategyContext
from src.engine.event import SignalEvent
from src.models.candle import Candle


class BaseStrategy(ABC):
    """Abstract trading strategy.

    Concrete strategies must implement:
    - on_candle(candle, context) -> Optional[SignalEvent]
    - reset() — clear all indicator state for a fresh backtest

    They should set:
    - name: human-readable identifier
    - parameters: current hyperparameter values
    - parameter_space: dict[str, (min, max, step)] — used by WalkForwardAnalyzer
    """

    name: str = "base_strategy"
    parameters: dict[str, Any] = {}
    parameter_space: dict[str, tuple[float, float, float]] = {}

    @abstractmethod
    def on_candle(
        self, candle: Candle, context: StrategyContext
    ) -> SignalEvent | None:
        """Process one bar; return a SignalEvent or None."""
        ...

    @abstractmethod
    def reset(self) -> None:
        """Clear all indicator state. Called before each backtest run."""
        ...

    def vectorized_signal(
        self, candles: list[Candle], context: StrategyContext
    ) -> list[SignalEvent]:
        """Generate signals for a list of candles (fast path for optimization sweeps).

        Default implementation loops on_candle(). Override for vectorized computation.
        """
        signals = []
        for candle in candles:
            signal = self.on_candle(candle, context)
            if signal is not None:
                signals.append(signal)
        return signals

    def generate_orders(
        self, candle: Candle, context: StrategyContext
    ) -> SignalEvent | None:
        """Alias for on_candle — kept for API compatibility."""
        return self.on_candle(candle, context)
