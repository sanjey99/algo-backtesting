"""Position sizing strategies for the algorithmic trading backtester."""
from __future__ import annotations

import math
from abc import ABC, abstractmethod

from src.engine.event import SignalEvent
from src.models.portfolio import Portfolio


class PositionSizer(ABC):
    """Determines how many shares to buy/sell for a given signal."""

    @abstractmethod
    def calculate(
        self,
        signal: SignalEvent,
        portfolio: Portfolio,
        current_price: float,
    ) -> int:
        """Return the number of shares to trade. Always >= 1."""
        ...


class FixedQuantitySizer(PositionSizer):
    """Always trades a fixed number of shares regardless of portfolio size."""

    def __init__(self, quantity: int = 100) -> None:
        if quantity < 1:
            raise ValueError("quantity must be >= 1")
        self._quantity = quantity

    def calculate(
        self,
        signal: SignalEvent,
        portfolio: Portfolio,
        current_price: float,
    ) -> int:
        return self._quantity


class FixedFractionSizer(PositionSizer):
    """Risk a fixed fraction of current equity per trade.

    shares = floor((portfolio.equity * fraction) / current_price)
    Minimum 1 share.
    fraction default: 0.02 (2% of equity per trade)
    """

    def __init__(self, fraction: float = 0.02) -> None:
        if not (0.0 < fraction <= 1.0):
            raise ValueError("fraction must be in (0, 1]")
        self._fraction = fraction

    def calculate(
        self,
        signal: SignalEvent,
        portfolio: Portfolio,
        current_price: float,
    ) -> int:
        if current_price <= 0:
            raise ValueError("current_price must be positive")
        shares = math.floor((portfolio.equity * self._fraction) / current_price)
        return max(1, shares)


class KellyCriterionSizer(PositionSizer):
    """Kelly Criterion position sizing.

    Uses the last `lookback` closed trades to estimate win_rate and avg win/loss ratio.
    Kelly fraction = win_rate - (1 - win_rate) / (avg_win / avg_loss)

    Applies half-Kelly (multiply by fraction) and caps at max_pct of equity.
    Falls back to FixedFractionSizer(0.02) if fewer than `lookback` closed trades exist.

    Args:
        lookback: number of recent closed trades to use (default 20)
        fraction: half-kelly multiplier (default 0.5)
        max_pct: maximum fraction of equity in any single trade (default 0.25)
    """

    def __init__(
        self,
        lookback: int = 20,
        fraction: float = 0.5,
        max_pct: float = 0.25,
    ) -> None:
        if lookback < 1:
            raise ValueError("lookback must be >= 1")
        if not (0.0 < fraction <= 1.0):
            raise ValueError("fraction must be in (0, 1]")
        if not (0.0 < max_pct <= 1.0):
            raise ValueError("max_pct must be in (0, 1]")
        self._lookback = lookback
        self._fraction = fraction
        self._max_pct = max_pct
        self._fallback = FixedFractionSizer(0.02)

    def calculate(
        self,
        signal: SignalEvent,
        portfolio: Portfolio,
        current_price: float,
    ) -> int:
        if current_price <= 0:
            raise ValueError("current_price must be positive")

        closed_trades = [t for t in portfolio.trades if t.is_closed]
        recent = closed_trades[-self._lookback :]

        if len(recent) < self._lookback:
            return self._fallback.calculate(signal, portfolio, current_price)

        wins = [t for t in recent if t.pnl > 0]
        losses = [t for t in recent if t.pnl <= 0]

        win_rate = len(wins) / len(recent)

        # Need at least one win and one loss to compute a meaningful ratio;
        # if all wins or all losses, fall back
        if not wins or not losses:
            return self._fallback.calculate(signal, portfolio, current_price)

        avg_win = sum(t.pnl for t in wins) / len(wins)
        avg_loss = abs(sum(t.pnl for t in losses) / len(losses))

        if avg_loss == 0:
            return self._fallback.calculate(signal, portfolio, current_price)

        win_loss_ratio = avg_win / avg_loss
        kelly_fraction = win_rate - (1.0 - win_rate) / win_loss_ratio

        # Apply half-Kelly multiplier and cap at max_pct
        effective_fraction = kelly_fraction * self._fraction
        effective_fraction = min(max(effective_fraction, 0.0), self._max_pct)

        if effective_fraction <= 0:
            return 1

        shares = math.floor((portfolio.equity * effective_fraction) / current_price)
        return max(1, shares)
