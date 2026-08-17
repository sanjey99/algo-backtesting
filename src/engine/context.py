"""Immutable execution state supplied to strategies for each candle."""
from __future__ import annotations

from dataclasses import dataclass

from src.models.order import Direction, OrderType


@dataclass(frozen=True, slots=True)
class StrategyContext:
    """Filled position and pending-order state visible to a strategy."""

    position_direction: Direction | None = None
    position_quantity: int = 0
    pending_direction: Direction | None = None
    pending_order_type: OrderType | None = None
    forced_cover_pending: bool = False

    def __post_init__(self) -> None:
        if self.position_quantity < 0:
            raise ValueError("position_quantity must be nonnegative")
        if self.position_direction is None and self.position_quantity != 0:
            raise ValueError(
                "position_quantity must be zero when position_direction is None"
            )
        if self.position_direction is not None and self.position_quantity == 0:
            raise ValueError(
                "position_quantity must be positive when position_direction is set"
            )
