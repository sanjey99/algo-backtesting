"""SimulatedBroker — converts OrderEvents to FillEvents with slippage and commission."""
from __future__ import annotations

from typing import Optional

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
        Fill at close * (1 + slippage) for LONG, close * (1 - slippage) for SHORT.

    LIMIT orders
        BUY  fills when candle.low  <= limit_price  (price came down to our bid)
        SELL fills when candle.high >= limit_price  (price came up  to our ask)
        Fill price is the limit_price itself (conservative; no price improvement).

    STOP orders
        BUY  fills when candle.high >= stop_price   (price broke above our trigger)
        SELL fills when candle.low  <= stop_price   (price broke below our trigger)
        Fill price is the stop_price (market-order at trigger).
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

    def execute(self, order: Order, candle: Candle) -> Optional[FillEvent]:
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

    def _calculate_fill_price(self, order: Order, candle: Candle) -> Optional[float]:
        if order.order_type == OrderType.MARKET:
            return self._market_fill(order, candle)
        if order.order_type == OrderType.LIMIT:
            return self._limit_fill(order, candle)
        if order.order_type == OrderType.STOP:
            return self._stop_fill(order, candle)
        return None  # unknown order type

    def _market_fill(self, order: Order, candle: Candle) -> float:
        """Slip away from close in the direction that is adverse to the trader."""
        if order.direction == Direction.LONG:
            return candle.close * (1.0 + self.slippage_pct)
        return candle.close * (1.0 - self.slippage_pct)

    def _limit_fill(self, order: Order, candle: Candle) -> Optional[float]:
        assert order.limit_price is not None  # guaranteed by Order.__post_init__
        limit = order.limit_price
        if order.direction == Direction.LONG:
            # We want to buy: fill only if market dropped to our bid
            if candle.low <= limit:
                return limit
        else:
            # We want to sell: fill only if market rose to our ask
            if candle.high >= limit:
                return limit
        return None

    def _stop_fill(self, order: Order, candle: Candle) -> Optional[float]:
        assert order.stop_price is not None  # guaranteed by Order.__post_init__
        stop = order.stop_price
        if order.direction == Direction.LONG:
            # Buy stop: triggered when price breaks *above* the stop level
            if candle.high >= stop:
                return stop
        else:
            # Sell stop: triggered when price breaks *below* the stop level
            if candle.low <= stop:
                return stop
        return None
