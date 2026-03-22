"""PositionSizer — determines how many shares/contracts to trade per signal."""
from __future__ import annotations

import math
from abc import ABC, abstractmethod

from src.engine.event import SignalEvent
from src.models.portfolio import Portfolio


class PositionSizer(ABC):
    """Abstract base class for position sizing strategies."""

    @abstractmethod
    def calculate(self, signal: SignalEvent, portfolio: Portfolio, price: float) -> int:
        """Return the number of units to trade (always positive)."""


class FixedQuantitySizer(PositionSizer):
    """Always trade a fixed quantity regardless of portfolio size."""

    def __init__(self, quantity: int = 1) -> None:
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        self._quantity = quantity

    def calculate(self, signal: SignalEvent, portfolio: Portfolio, price: float) -> int:
        return self._quantity


class FixedFractionSizer(PositionSizer):
    """Risk a fixed fraction of current equity per trade.

    quantity = floor(equity * fraction / price)
    """

    def __init__(self, fraction: float = 0.02) -> None:
        if not (0.0 < fraction <= 1.0):
            raise ValueError("fraction must be between 0 and 1 (exclusive/inclusive)")
        self._fraction = fraction

    def calculate(self, signal: SignalEvent, portfolio: Portfolio, price: float) -> int:
        if price <= 0:
            return 0
        qty = math.floor(portfolio.equity * self._fraction / price)
        return max(qty, 1)
