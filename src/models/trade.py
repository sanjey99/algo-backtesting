"""Trade — a completed or open position from entry to exit."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from src.models.order import Direction


@dataclass
class Trade:
    """Records a round-trip trade (entry → exit).

    is_closed is False until exit_price is set.
    pnl / pnl_pct are only valid when is_closed is True.
    """

    symbol: str
    direction: Direction
    entry_price: float
    quantity: int
    entry_date: datetime
    commission: float = 0.0
    exit_price: float | None = None
    exit_date: datetime | None = None
    trade_id: str = field(default="")

    @property
    def is_closed(self) -> bool:
        return self.exit_price is not None

    @property
    def pnl(self) -> float:
        """Realised PnL after commission. Positive = profit."""
        if self.exit_price is None:
            return 0.0
        raw = (self.exit_price - self.entry_price) * self.quantity
        if self.direction == Direction.SHORT:
            raw = -raw
        return raw - self.commission

    @property
    def pnl_pct(self) -> float:
        """PnL as a fraction of entry cost (before commission)."""
        if self.exit_price is None:
            return 0.0
        cost = self.entry_price * self.quantity
        if cost == 0:
            return 0.0
        return self.pnl / cost

    @property
    def duration_days(self) -> float | None:
        if self.exit_date is None:
            return None
        return (self.exit_date - self.entry_date).total_seconds() / 86400.0
