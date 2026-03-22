"""BaseStrategy — abstract base class for all concrete strategies."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional

from src.engine.event import SignalEvent
from src.models.candle import Candle


class BaseStrategy(ABC):
    """Abstract base that concrete strategies inherit from.

    Provides instance-level dicts so subclasses never accidentally share
    mutable class-level state (R-14).  The ``symbol`` attribute is set by
    the engine before the bar-loop begins (R-03).
    """

    name: str = "base_strategy"

    def __init__(self) -> None:
        self.parameters: dict[str, Any] = {}
        self.parameter_space: dict[str, tuple[float, float, float]] = {}
        self.symbol: str = "UNKNOWN"

    @abstractmethod
    def on_candle(self, candle: Candle) -> Optional[SignalEvent]:
        """Process one OHLCV candle and optionally emit a signal."""

    def reset(self) -> None:
        """Reset all internal state so the strategy can be re-used."""
