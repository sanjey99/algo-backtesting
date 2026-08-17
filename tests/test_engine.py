"""Tests for the event system and backtest engine (Step 3)."""
from __future__ import annotations

from datetime import datetime

import pytest

from src.engine.backtest import BacktestConfig, BacktestEngine
from src.engine.broker import SimulatedBroker
from src.engine.event import FillEvent, MarketEvent, OrderEvent, SignalEvent
from src.models.candle import Candle
from src.models.order import Direction, Order, OrderType

# Re-use conftest helpers directly (not as fixtures) for unit-level tests
from tests.conftest import make_candle, make_candle_series

# ---------------------------------------------------------------------------
# Event dataclass instantiation
# ---------------------------------------------------------------------------


class TestEventInstantiation:
    def test_market_event(self) -> None:
        candle = make_candle()
        event = MarketEvent(symbol="AAPL", candle=candle)
        assert event.symbol == "AAPL"
        assert event.candle is candle

    def test_signal_event_defaults(self) -> None:
        event = SignalEvent(symbol="AAPL", direction=Direction.LONG)
        assert event.strength == 1.0
        assert isinstance(event.timestamp, datetime)

    def test_signal_event_explicit(self) -> None:
        ts = datetime(2023, 6, 1)
        event = SignalEvent(
            symbol="TSLA", direction=Direction.SHORT, strength=0.7, timestamp=ts
        )
        assert event.direction == Direction.SHORT
        assert event.strength == 0.7
        assert event.timestamp == ts

    def test_order_event(self) -> None:
        order = Order(symbol="AAPL", direction=Direction.LONG, quantity=10)
        event = OrderEvent(order=order)
        assert event.order is order

    def test_fill_event(self) -> None:
        ts = datetime(2023, 6, 1)
        event = FillEvent(
            symbol="AAPL",
            direction=Direction.LONG,
            quantity=5,
            fill_price=150.0,
            commission=0.75,
            fill_date=ts,
            order_id="abc123",
        )
        assert event.fill_price == 150.0
        assert event.commission == 0.75
        assert event.order_id == "abc123"

    def test_fill_event_optional_order_id(self) -> None:
        ts = datetime(2023, 6, 1)
        event = FillEvent(
            symbol="AAPL",
            direction=Direction.SHORT,
            quantity=1,
            fill_price=100.0,
            commission=0.1,
            fill_date=ts,
        )
        assert event.order_id == ""


# ---------------------------------------------------------------------------
# SimulatedBroker
# ---------------------------------------------------------------------------


class TestSimulatedBrokerMarket:
    """MARKET order fill price tests."""

    SLIPPAGE = 0.0005
    COMMISSION = 0.001

    def setup_method(self) -> None:
        self.broker = SimulatedBroker(
            slippage_pct=self.SLIPPAGE, commission_pct=self.COMMISSION
        )
        self.candle = make_candle(open_=90.0, close=100.0, high=101.0, low=89.0)

    def test_market_long_fills_at_open(self) -> None:
        order = Order(symbol="X", direction=Direction.LONG, quantity=1)
        fill = self.broker.execute(order, self.candle)
        assert fill is not None
        assert fill.fill_price == pytest.approx(90.0 * (1 + self.SLIPPAGE))

    def test_market_short_fill_price(self) -> None:
        order = Order(symbol="X", direction=Direction.SHORT, quantity=1)
        fill = self.broker.execute(order, self.candle)
        assert fill is not None
        assert fill.fill_price == pytest.approx(90.0 * (1 - self.SLIPPAGE))

    def test_market_commission_is_percentage_of_notional(self) -> None:
        order = Order(symbol="X", direction=Direction.LONG, quantity=10)
        fill = self.broker.execute(order, self.candle)
        assert fill is not None
        expected_commission = fill.fill_price * 10 * self.COMMISSION
        assert abs(fill.commission - expected_commission) < 1e-9

    def test_market_fill_populates_metadata(self) -> None:
        order = Order(symbol="X", direction=Direction.LONG, quantity=5)
        fill = self.broker.execute(order, self.candle)
        assert fill is not None
        assert fill.symbol == "X"
        assert fill.direction == Direction.LONG
        assert fill.quantity == 5
        assert fill.fill_date == self.candle.timestamp


class TestSimulatedBrokerLimit:
    """LIMIT order conditional fill tests."""

    def setup_method(self) -> None:
        self.broker = SimulatedBroker()
        # candle: open=99, high=101, low=98, close=100
        self.candle = make_candle(close=100.0, open_=99.0, high=101.0, low=98.0)

    # -- BUY LIMIT -----------------------------------------------------------

    def test_limit_buy_fills_when_low_at_limit(self) -> None:
        order = Order(
            symbol="X",
            direction=Direction.LONG,
            quantity=1,
            order_type=OrderType.LIMIT,
            limit_price=98.0,  # exactly at candle.low
        )
        fill = self.broker.execute(order, self.candle)
        assert fill is not None
        assert fill.fill_price == 98.0

    def test_limit_buy_fills_when_low_below_limit(self) -> None:
        order = Order(
            symbol="X",
            direction=Direction.LONG,
            quantity=1,
            order_type=OrderType.LIMIT,
            limit_price=99.0,  # candle.low=98 < 99 -> fills
        )
        fill = self.broker.execute(order, self.candle)
        assert fill is not None

    def test_limit_buy_does_not_fill_when_low_above_limit(self) -> None:
        order = Order(
            symbol="X",
            direction=Direction.LONG,
            quantity=1,
            order_type=OrderType.LIMIT,
            limit_price=97.0,  # candle.low=98 > 97 -> no fill
        )
        fill = self.broker.execute(order, self.candle)
        assert fill is None

    def test_buy_limit_gap_gets_protected_slipped_open(self) -> None:
        broker = SimulatedBroker(slippage_pct=0.01, commission_pct=0.0)
        candle = make_candle(open_=90.0, high=96.0, low=89.0, close=95.0)
        order = Order("X", Direction.LONG, 1, OrderType.LIMIT, limit_price=95.0)
        fill = broker.execute(order, candle)
        assert fill is not None
        assert fill.fill_price == pytest.approx(90.9)

    # -- SELL LIMIT ----------------------------------------------------------

    def test_limit_sell_fills_when_high_at_limit(self) -> None:
        order = Order(
            symbol="X",
            direction=Direction.SHORT,
            quantity=1,
            order_type=OrderType.LIMIT,
            limit_price=101.0,  # exactly at candle.high
        )
        fill = self.broker.execute(order, self.candle)
        assert fill is not None
        assert fill.fill_price == 101.0

    def test_limit_sell_does_not_fill_when_high_below_limit(self) -> None:
        order = Order(
            symbol="X",
            direction=Direction.SHORT,
            quantity=1,
            order_type=OrderType.LIMIT,
            limit_price=102.0,  # candle.high=101 < 102 -> no fill
        )
        fill = self.broker.execute(order, self.candle)
        assert fill is None

    def test_sell_limit_gap_gets_protected_slipped_open(self) -> None:
        broker = SimulatedBroker(slippage_pct=0.01, commission_pct=0.0)
        candle = make_candle(open_=110.0, high=111.0, low=104.0, close=105.0)
        order = Order("X", Direction.SHORT, 1, OrderType.LIMIT, limit_price=105.0)
        fill = broker.execute(order, candle)
        assert fill is not None
        assert fill.fill_price == pytest.approx(108.9)


class TestSimulatedBrokerStop:
    """STOP order conditional fill tests."""

    def setup_method(self) -> None:
        self.broker = SimulatedBroker()
        self.candle = make_candle(close=100.0, open_=99.0, high=101.0, low=98.0)

    def test_stop_buy_fills_when_high_at_stop(self) -> None:
        order = Order(
            symbol="X",
            direction=Direction.LONG,
            quantity=1,
            order_type=OrderType.STOP,
            stop_price=101.0,  # exactly at candle.high
        )
        fill = self.broker.execute(order, self.candle)
        assert fill is not None
        assert fill.fill_price == pytest.approx(101.0 * (1 + self.broker.slippage_pct))

    def test_stop_buy_fills_when_high_above_stop(self) -> None:
        order = Order(
            symbol="X",
            direction=Direction.LONG,
            quantity=1,
            order_type=OrderType.STOP,
            stop_price=100.0,  # candle.high=101 > 100 -> fills
        )
        fill = self.broker.execute(order, self.candle)
        assert fill is not None

    def test_stop_buy_does_not_fill_when_high_below_stop(self) -> None:
        order = Order(
            symbol="X",
            direction=Direction.LONG,
            quantity=1,
            order_type=OrderType.STOP,
            stop_price=102.0,  # candle.high=101 < 102 -> no fill
        )
        fill = self.broker.execute(order, self.candle)
        assert fill is None

    def test_buy_stop_gap_pays_open_plus_slippage(self) -> None:
        broker = SimulatedBroker(slippage_pct=0.01, commission_pct=0.0)
        candle = make_candle(open_=110.0, high=112.0, low=109.0, close=111.0)
        order = Order("X", Direction.LONG, 1, OrderType.STOP, stop_price=105.0)
        fill = broker.execute(order, candle)
        assert fill is not None
        assert fill.fill_price == pytest.approx(111.1)

    def test_stop_sell_fills_when_low_at_stop(self) -> None:
        order = Order(
            symbol="X",
            direction=Direction.SHORT,
            quantity=1,
            order_type=OrderType.STOP,
            stop_price=98.0,
        )
        fill = self.broker.execute(order, self.candle)
        assert fill is not None

    def test_stop_sell_does_not_fill_when_low_above_stop(self) -> None:
        order = Order(
            symbol="X",
            direction=Direction.SHORT,
            quantity=1,
            order_type=OrderType.STOP,
            stop_price=97.0,  # candle.low=98 > 97 -> no fill
        )
        fill = self.broker.execute(order, self.candle)
        assert fill is None

    def test_sell_stop_gap_pays_open_minus_slippage(self) -> None:
        broker = SimulatedBroker(slippage_pct=0.01, commission_pct=0.0)
        candle = make_candle(open_=90.0, high=91.0, low=88.0, close=89.0)
        order = Order("X", Direction.SHORT, 1, OrderType.STOP, stop_price=95.0)
        fill = broker.execute(order, candle)
        assert fill is not None
        assert fill.fill_price == pytest.approx(89.1)


# ---------------------------------------------------------------------------
# BacktestEngine integration test
# ---------------------------------------------------------------------------


class _BuyDay0SellDay49Strategy:
    """Signals LONG on the first candle, SHORT on the 50th candle (index 49).

    Uses a bar counter so it is self-contained and repeatable.
    """

    name: str = "BuyDay0SellDay49"
    parameters: dict = {}

    def __init__(self, symbol: str = "TEST") -> None:
        self._symbol = symbol
        self._bar_count = 0

    def on_candle(self, candle: Candle) -> SignalEvent | None:
        idx = self._bar_count
        self._bar_count += 1

        if idx == 0:
            return SignalEvent(
                symbol=self._symbol,
                direction=Direction.LONG,
                timestamp=candle.timestamp,
            )
        if idx == 49:
            return SignalEvent(
                symbol=self._symbol,
                direction=Direction.SHORT,
                timestamp=candle.timestamp,
            )
        return None


class TestBacktestEngine:
    SLIPPAGE = 0.0005
    COMMISSION = 0.001
    INITIAL_CAPITAL = 100_000.0

    def _config(self) -> BacktestConfig:
        return BacktestConfig(
            initial_capital=self.INITIAL_CAPITAL,
            commission_pct=self.COMMISSION,
            slippage_pct=self.SLIPPAGE,
        )

    def test_buy_day0_sell_day49_final_equity_gt_initial(self) -> None:
        """End-to-end: uptrend series -> PnL positive."""
        candles = make_candle_series(n=50, base=100.0)
        strategy = _BuyDay0SellDay49Strategy(symbol="TEST")
        engine = BacktestEngine()

        result = engine.run(strategy, candles, self._config())

        assert result.final_equity > self.INITIAL_CAPITAL

    def test_buy_day0_sell_day49_pnl_matches_expected(self) -> None:
        """Verify exact PnL accounting for slippage and commission."""
        candles = make_candle_series(n=50, base=100.0)
        strategy = _BuyDay0SellDay49Strategy(symbol="TEST")
        engine = BacktestEngine()

        result = engine.run(strategy, candles, self._config())

        # Manual calculation matching broker logic
        buy_open = candles[0].open  # 99.7
        sell_open = candles[49].open  # 100 + 49*0.5 - 0.3 = 124.2
        qty = 1

        entry_price = buy_open * (1 + self.SLIPPAGE)
        exit_price = sell_open * (1 - self.SLIPPAGE)
        entry_comm = entry_price * qty * self.COMMISSION
        exit_comm = exit_price * qty * self.COMMISSION

        expected_pnl = (exit_price - entry_price) * qty - entry_comm - exit_comm
        expected_equity = self.INITIAL_CAPITAL + expected_pnl

        assert abs(result.final_equity - expected_equity) < 1e-6

    def test_result_has_one_closed_trade(self) -> None:
        candles = make_candle_series(n=50, base=100.0)
        strategy = _BuyDay0SellDay49Strategy(symbol="TEST")
        engine = BacktestEngine()

        result = engine.run(strategy, candles, self._config())

        assert len(result.trades) == 1
        trade = result.trades[0]
        assert trade.is_closed
        assert trade.direction == Direction.LONG

    def test_result_equity_curve_length(self) -> None:
        """Equity curve has one point per candle."""
        candles = make_candle_series(n=50, base=100.0)
        strategy = _BuyDay0SellDay49Strategy(symbol="TEST")
        engine = BacktestEngine()

        result = engine.run(strategy, candles, self._config())

        assert len(result.equity_curve) == 50

    def test_result_metadata(self) -> None:
        candles = make_candle_series(n=50, base=100.0)
        strategy = _BuyDay0SellDay49Strategy(symbol="TEST")
        engine = BacktestEngine()

        result = engine.run(strategy, candles, self._config())

        assert result.strategy_name == "BuyDay0SellDay49"
        assert result.start_date == candles[0].timestamp
        assert result.end_date == candles[-1].timestamp
        assert result.initial_capital == self.INITIAL_CAPITAL
        assert result.run_id  # non-empty UUID string

    def test_no_signal_strategy_preserves_capital(self) -> None:
        """A strategy that never signals should leave equity unchanged."""

        class _NullStrategy:
            name = "Null"
            parameters: dict = {}

            def on_candle(self, candle: Candle) -> SignalEvent | None:
                return None

        candles = make_candle_series(n=10, base=100.0)
        result = BacktestEngine().run(_NullStrategy(), candles, self._config())

        assert result.final_equity == self.INITIAL_CAPITAL
        assert result.trades == []

    def test_empty_candles_raises(self) -> None:
        class _NullStrategy:
            name = "Null"
            parameters: dict = {}

            def on_candle(self, candle: Candle) -> SignalEvent | None:
                return None

        with pytest.raises(ValueError, match="candles list must not be empty"):
            BacktestEngine().run(_NullStrategy(), [], self._config())
