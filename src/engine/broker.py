"""SimulatedBroker — converts OrderEvents to FillEvents with slippage and commission."""
from __future__ import annotations

from src.engine.event import FillEvent
from src.models.candle import Candle
from src.models.order import Direction, Order, OrderType


class SimulatedBroker:
    """Converts an Order to a FillEvent with slippage and commission.

    slippage_pct:   adverse price impact per trade  (default 0.0005 = 5 bp)
    commission_pct: commission as fraction of notional (default 0.001 = 10 bp)

    Fill logic
    ----------
    MARKET orders
        Fill at open * (1 + slippage) for LONG, open * (1 - slippage) for SHORT.

    LIMIT orders
        BUY  fills at an adverse slipped open on a favorable down gap; otherwise,
        when candle.low <= limit_price, fills at the limit price.
        SELL fills at an adverse slipped open on a favorable up gap; otherwise,
        when candle.high >= limit_price, fills at the limit price.

    STOP orders
        BUY  fills at the open when it gaps above the stop; otherwise, when
        candle.high >= stop_price, fills at the stop. Both receive slippage.
        SELL fills at the open when it gaps below the stop; otherwise, when
        candle.low <= stop_price, fills at the stop. Both receive slippage.
    """

    def __init__(
        self,
        slippage_pct: float = 0.0005,
        commission_pct: float = 0.001,
    ) -> None:
        self.slippage_pct = slippage_pct
        self.commission_pct = commission_pct

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def execute(self, order: Order, candle: Candle) -> FillEvent | None:
        """Try to fill *order* against *candle*. Returns None if unfilled."""
        fill_price = self._calculate_fill_price(order, candle)
        if fill_price is None:
            return None

        commission = fill_price * order.quantity * self.commission_pct
        return FillEvent(
            symbol=order.symbol,
            direction=order.direction,
            quantity=order.quantity,
            fill_price=fill_price,
            commission=commission,
            fill_date=candle.timestamp,
            order_id=order.order_id,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _calculate_fill_price(self, order: Order, candle: Candle) -> float | None:
        if order.order_type == OrderType.MARKET:
            return self._market_fill(order, candle)
        if order.order_type == OrderType.LIMIT:
            return self._limit_fill(order, candle)
        if order.order_type == OrderType.STOP:
            return self._stop_fill(order, candle)
        return None  # unknown order type

    def _market_fill(self, order: Order, candle: Candle) -> float:
        return self._adverse_price(candle.open, order.direction)

    def _limit_fill(self, order: Order, candle: Candle) -> float | None:
        assert order.limit_price is not None  # guaranteed by Order.__post_init__
        limit = order.limit_price
        if order.direction is Direction.LONG:
            if candle.open <= limit:
                return min(limit, self._adverse_price(candle.open, order.direction))
            return limit if candle.low <= limit else None
        if candle.open >= limit:
            return max(limit, self._adverse_price(candle.open, order.direction))
        return limit if candle.high >= limit else None

    def _stop_fill(self, order: Order, candle: Candle) -> float | None:
        assert order.stop_price is not None  # guaranteed by Order.__post_init__
        stop = order.stop_price
        if order.direction is Direction.LONG:
            raw = candle.open if candle.open >= stop else stop if candle.high >= stop else None
        else:
            raw = candle.open if candle.open <= stop else stop if candle.low <= stop else None
        return None if raw is None else self._adverse_price(raw, order.direction)

    def _adverse_price(self, price: float, direction: Direction) -> float:
        multiplier = (
            1.0 + self.slippage_pct
            if direction is Direction.LONG
            else 1.0 - self.slippage_pct
        )
        return price * multiplier
