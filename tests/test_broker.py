"""Tests for SimulatedBroker."""
from __future__ import annotations

import pytest

from src.engine.broker import SimulatedBroker
from src.models.candle import Candle
from src.models.order import Direction, Order, OrderType
from tests.conftest import make_candle


class TestSimulatedBrokerMarket:
    """Market order fill logic — fills at candle.open with slippage."""

    SLIPPAGE = 0.001
    COMMISSION = 0.002

    def _broker(self) -> SimulatedBroker:
        return SimulatedBroker(
            slippage_pct=self.SLIPPAGE,
            commission_pct=self.COMMISSION,
        )

    def _candle(self) -> Candle:
        # open_=99.0, close=100.0
        return make_candle(close=100.0, open_=99.0, high=101.0, low=98.0)

    def _long_order(self) -> Order:
        return Order(symbol="TEST", direction=Direction.LONG, quantity=10)

    def _short_order(self) -> Order:
        return Order(symbol="TEST", direction=Direction.SHORT, quantity=10)

    def test_market_long_fill_price(self) -> None:
        broker = self._broker()
        fill = broker.execute(self._long_order(), self._candle())
        assert fill is not None
        expected = 99.0 * (1 + self.SLIPPAGE)
        assert abs(fill.fill_price - expected) < 1e-9

    def test_market_short_fill_price(self) -> None:
        broker = self._broker()
        fill = broker.execute(self._short_order(), self._candle())
        assert fill is not None
        expected = 99.0 * (1 - self.SLIPPAGE)
        assert abs(fill.fill_price - expected) < 1e-9

    def test_market_commission_is_percentage_of_notional(self) -> None:
        broker = self._broker()
        fill = broker.execute(self._long_order(), self._candle())
        assert fill is not None
        expected_commission = fill.fill_price * 10 * self.COMMISSION
        assert abs(fill.commission - expected_commission) < 1e-9

    def test_fill_symbol_matches_order(self) -> None:
        broker = self._broker()
        fill = broker.execute(self._long_order(), self._candle())
        assert fill is not None
        assert fill.symbol == "TEST"

    def test_fill_direction_matches_order(self) -> None:
        broker = self._broker()
        fill = broker.execute(self._long_order(), self._candle())
        assert fill is not None
        assert fill.direction == Direction.LONG


class TestSimulatedBrokerLimit:
    """Limit order fill logic."""

    def _broker(self) -> SimulatedBroker:
        return SimulatedBroker()

    def test_long_limit_fills_when_price_drops_to_bid(self) -> None:
        candle = make_candle(close=100.0, open_=99.0, high=101.0, low=95.0)
        order = Order(
            symbol="TEST",
            direction=Direction.LONG,
            quantity=1,
            order_type=OrderType.LIMIT,
            limit_price=96.0,
        )
        fill = self._broker().execute(order, candle)
        assert fill is not None
        assert fill.fill_price == 96.0

    def test_long_limit_does_not_fill_if_price_stays_above(self) -> None:
        candle = make_candle(close=100.0, open_=99.0, high=101.0, low=98.0)
        order = Order(
            symbol="TEST",
            direction=Direction.LONG,
            quantity=1,
            order_type=OrderType.LIMIT,
            limit_price=97.0,
        )
        fill = self._broker().execute(order, candle)
        assert fill is None

    def test_short_limit_fills_when_price_rises_to_ask(self) -> None:
        candle = make_candle(close=100.0, open_=99.0, high=105.0, low=98.0)
        order = Order(
            symbol="TEST",
            direction=Direction.SHORT,
            quantity=1,
            order_type=OrderType.LIMIT,
            limit_price=104.0,
        )
        fill = self._broker().execute(order, candle)
        assert fill is not None
        assert fill.fill_price == 104.0

    def test_short_limit_does_not_fill_if_price_stays_below(self) -> None:
        candle = make_candle(close=100.0, open_=99.0, high=103.0, low=98.0)
        order = Order(
            symbol="TEST",
            direction=Direction.SHORT,
            quantity=1,
            order_type=OrderType.LIMIT,
            limit_price=104.0,
        )
        fill = self._broker().execute(order, candle)
        assert fill is None


class TestSimulatedBrokerStop:
    """Stop order fill logic."""

    def _broker(self) -> SimulatedBroker:
        return SimulatedBroker()

    def test_long_stop_fills_when_price_breaks_above_trigger(self) -> None:
        candle = make_candle(close=106.0, open_=99.0, high=107.0, low=98.0)
        order = Order(
            symbol="TEST",
            direction=Direction.LONG,
            quantity=1,
            order_type=OrderType.STOP,
            stop_price=105.0,
        )
        fill = self._broker().execute(order, candle)
        assert fill is not None
        assert fill.fill_price == 105.0

    def test_long_stop_does_not_fill_if_price_stays_below(self) -> None:
        candle = make_candle(close=100.0, open_=99.0, high=104.0, low=98.0)
        order = Order(
            symbol="TEST",
            direction=Direction.LONG,
            quantity=1,
            order_type=OrderType.STOP,
            stop_price=105.0,
        )
        fill = self._broker().execute(order, candle)
        assert fill is None

    def test_short_stop_fills_when_price_breaks_below_trigger(self) -> None:
        candle = make_candle(close=93.0, open_=99.0, high=100.0, low=92.0)
        order = Order(
            symbol="TEST",
            direction=Direction.SHORT,
            quantity=1,
            order_type=OrderType.STOP,
            stop_price=94.0,
        )
        fill = self._broker().execute(order, candle)
        assert fill is not None
        assert fill.fill_price == 94.0
