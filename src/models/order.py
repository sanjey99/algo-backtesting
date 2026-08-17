"""Order — the instruction sent from strategy to broker."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum


class OrderType(Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"


class Direction(Enum):
    LONG = "LONG"
    SHORT = "SHORT"

    def opposite(self) -> Direction:
        return Direction.SHORT if self == Direction.LONG else Direction.LONG


@dataclass
class Order:
    """A trading instruction.

    limit_price / stop_price are only meaningful for LIMIT / STOP orders.
    order_id is assigned by the engine on creation.
    """

    symbol: str
    direction: Direction
    quantity: int
    order_type: OrderType = OrderType.MARKET
    limit_price: float | None = None
    stop_price: float | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    order_id: str = field(default="")

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError(f"Quantity must be positive: {self.quantity}")
        if self.order_type == OrderType.LIMIT and self.limit_price is None:
            raise ValueError("LIMIT order requires limit_price")
        if self.order_type == OrderType.STOP and self.stop_price is None:
            raise ValueError("STOP order requires stop_price")
