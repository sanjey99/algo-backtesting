"""Event hierarchy — the messaging backbone of the event-driven backtest engine."""
from __future__ import annotations

from abc import ABC
from dataclasses import dataclass, field
from datetime import UTC, datetime

from src.models.candle import Candle
from src.models.order import Direction, Order, OrderType


class Event(ABC):
    """Base class for all engine events."""


@dataclass
class MarketEvent(Event):
    """Emitted once per bar for each symbol with the latest candle."""

    symbol: str
    candle: Candle


@dataclass
class SignalEvent(Event):
    """Strategy output: directional signal with optional strength."""

    symbol: str
    direction: Direction
    strength: float = 1.0  # 0.0-1.0, used by position sizer
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    order_type: OrderType = OrderType.MARKET
    limit_price: float | None = None
    stop_price: float | None = None


@dataclass
class OrderEvent(Event):
    """Wraps an Order for routing to the broker."""

    order: Order


@dataclass
class FillEvent(Event):
    """Broker confirmation of a filled order."""

    symbol: str
    direction: Direction
    quantity: int
    fill_price: float
    commission: float
    fill_date: datetime
    order_id: str = ""
