"""Tests for the event system and backtest engine (Step 3)."""
from __future__ import annotations

import logging
from datetime import datetime

import pytest

from src.engine.backtest import BacktestConfig, BacktestEngine
from src.engine.broker import SimulatedBroker
from src.engine.context import StrategyContext
from src.engine.event import FillEvent, MarketEvent, OrderEvent, SignalEvent
from src.engine.position_sizer import (
    FixedFractionSizer,
    FixedQuantitySizer,
    KellyCriterionSizer,
)
from src.models.candle import Candle
from src.models.order import Direction, Order, OrderType

# Re-use conftest helpers directly (not as fixtures) for unit-level tests
from tests.conftest import make_candle, make_candle_series


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("short_initial_margin", 0.99),
        ("short_maintenance_margin", -0.01),
        ("annual_short_borrow_rate", -0.01),
        ("borrow_day_count", 0.0),
    ],
)
def test_backtest_config_rejects_invalid_margin_values(field: str, value: float) -> None:
    with pytest.raises(ValueError):
        BacktestConfig(**{field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("short_initial_margin", float("inf")),
        ("short_maintenance_margin", float("nan")),
        ("annual_short_borrow_rate", float("inf")),
        ("borrow_day_count", float("nan")),
    ],
)
def test_backtest_config_rejects_non_finite_margin_values(field: str, value: float) -> None:
    with pytest.raises(ValueError):
        BacktestConfig(**{field: value})


def _bar(index: int, open_: float, high: float, low: float, close: float) -> Candle:
    return Candle(
        timestamp=datetime(2023, 1, index + 1),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=1_000.0,
        adj_close=close,
    )


def test_backtest_emits_start_fill_and_completion(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The run lifecycle exposes its authoritative order transitions."""
    with caplog.at_level(logging.DEBUG):
        BacktestEngine().run(
            _BuyOnZeroSellOnOne(),
            [
                _bar(0, 100, 101, 99, 100),
                _bar(1, 101, 102, 100, 101),
                _bar(2, 102, 103, 101, 102),
            ],
            BacktestConfig(),
        )

    events = [getattr(record, "event", None) for record in caplog.records]
    assert events[0] == "backtest.started"
    assert "order.queued" in events
    assert "order.filled" in events
    assert events[-1] == "backtest.completed"
    records_by_event = {getattr(record, "event", None): record for record in caplog.records}
    assert records_by_event["backtest.started"].levelno == logging.INFO
    assert records_by_event["order.queued"].levelno == logging.DEBUG
    assert records_by_event["order.filled"].levelno == logging.DEBUG
    assert records_by_event["backtest.completed"].levelno == logging.INFO


def test_requested_symbol_overrides_a_strategy_signal_symbol() -> None:
    """A run's requested instrument is authoritative over strategy placeholders."""
    result = BacktestEngine().run(
        _BuyOnZeroSellOnOne(),
        [
            _bar(0, 100, 101, 99, 100),
            _bar(1, 101, 102, 100, 101),
            _bar(2, 102, 103, 101, 102),
        ],
        BacktestConfig(),
        symbol="AAPL",
    )

    assert result.symbol == "AAPL"
    assert result.trades[0].symbol == "AAPL"


def test_rejected_order_logs_reason_not_payload(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Expected rejection is observable through its stable typed reason."""
    with caplog.at_level(logging.WARNING):
        BacktestEngine().run(
            _BuyOnZeroSellOnOne(),
            [_bar(0, 100, 101, 99, 100), _bar(1, 101, 102, 100, 101)],
            BacktestConfig(initial_capital=1.0),
        )

    record = next(
        record
        for record in caplog.records
        if getattr(record, "event", None) == "order.rejected"
    )
    fields = getattr(record, "event_fields")
    assert isinstance(fields, dict)
    assert fields["reason"] == "insufficient_buying_power"
    assert record.levelno == logging.WARNING


def test_pending_order_transitions_are_logged(caplog: pytest.LogCaptureFixture) -> None:
    """Conditional execution records retry, replacement, and final cancellation."""
    strategy = _ScheduledSignals(
        {
            0: (Direction.LONG, OrderType.LIMIT, 90.0),
            1: (Direction.LONG, OrderType.LIMIT, 95.0),
        }
    )
    candles = [_bar(0, 100, 101, 99, 100), _bar(1, 100, 101, 98, 100)]

    with caplog.at_level(logging.DEBUG):
        BacktestEngine().run(strategy, candles, BacktestConfig())

    events = [getattr(record, "event", None) for record in caplog.records]
    assert "order.untriggered" in events
    assert "order.replaced" in events
    assert "order.cancelled_end_of_data" in events
    records_by_event = {getattr(record, "event", None): record for record in caplog.records}
    assert records_by_event["order.untriggered"].levelno == logging.DEBUG
    assert records_by_event["order.replaced"].levelno == logging.DEBUG
    assert records_by_event["order.cancelled_end_of_data"].levelno == logging.DEBUG


def test_margin_lifecycle_transitions_are_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Borrow and margin calls remain visible without INFO-level bar noise."""
    with caplog.at_level(logging.DEBUG):
        BacktestEngine().run(_OpenShortOnce(), _margin_breach_candles(), _margin_config())

    events = [getattr(record, "event", None) for record in caplog.records]
    assert "margin.borrow_accrued" in events
    assert "margin.call_queued" in events
    records_by_event = {getattr(record, "event", None): record for record in caplog.records}
    assert records_by_event["margin.borrow_accrued"].levelno == logging.DEBUG
    assert records_by_event["margin.call_queued"].levelno == logging.WARNING

    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        BacktestEngine().run(
            _OpenShortOnce(), _margin_breach_candles(False), _margin_config()
        )

    events = [getattr(record, "event", None) for record in caplog.records]
    assert "order.cancelled_end_of_data" in events
    assert "margin.call_unresolved" in events
    records_by_event = {getattr(record, "event", None): record for record in caplog.records}
    assert records_by_event["margin.call_unresolved"].levelno == logging.WARNING


def test_backtest_failure_logs_safe_type_and_reraises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Unexpected strategy errors retain failure propagation without raw diagnostics."""

    class _FailingStrategy:
        name = "failing"
        parameters: dict[str, object] = {}

        def on_candle(
            self, candle: Candle, context: StrategyContext
        ) -> SignalEvent | None:
            del candle, context
            raise RuntimeError("fixture-sensitive-error")

    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError, match="sensitive"):
        BacktestEngine().run(
            _FailingStrategy(), [_bar(0, 100, 101, 99, 100)], BacktestConfig()
        )

    record = next(
        record
        for record in caplog.records
        if getattr(record, "event", None) == "backtest.failed"
    )
    fields = getattr(record, "event_fields")
    assert isinstance(fields, dict)
    assert fields["error_type"] == "RuntimeError"
    assert record.levelno == logging.ERROR
    assert "fixture-sensitive-error" not in record.getMessage()

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
    """Signals LONG on the first candle, SHORT before the 50th candle.

    Uses a bar counter so it is self-contained and repeatable.
    """

    name: str = "BuyDay0SellDay49"
    parameters: dict[str, object] = {}

    def __init__(self, symbol: str = "TEST") -> None:
        self._symbol = symbol
        self._bar_count = 0

    def on_candle(
        self, candle: Candle, context: StrategyContext
    ) -> SignalEvent | None:
        idx = self._bar_count
        self._bar_count += 1

        if idx == 0:
            return SignalEvent(
                symbol=self._symbol,
                direction=Direction.LONG,
                timestamp=candle.timestamp,
            )
        if idx == 48:
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
        buy_open = candles[1].open  # 100.2
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
            parameters: dict[str, object] = {}

            def on_candle(
                self, candle: Candle, context: StrategyContext
            ) -> SignalEvent | None:
                return None

        candles = make_candle_series(n=10, base=100.0)
        result = BacktestEngine().run(_NullStrategy(), candles, self._config())

        assert result.final_equity == self.INITIAL_CAPITAL
        assert result.trades == []

    def test_empty_candles_raises(self) -> None:
        class _NullStrategy:
            name = "Null"
            parameters: dict[str, object] = {}

            def on_candle(
                self, candle: Candle, context: StrategyContext
            ) -> SignalEvent | None:
                return None

        with pytest.raises(ValueError, match="candles list must not be empty"):
            BacktestEngine().run(_NullStrategy(), [], self._config())


class _BuyOnZeroSellOnOne:
    name = "timing"
    parameters: dict[str, object] = {}

    def __init__(self) -> None:
        self.index = 0

    def on_candle(
        self, candle: Candle, context: StrategyContext
    ) -> SignalEvent | None:
        index = self.index
        self.index += 1
        if index == 0:
            return SignalEvent("X", Direction.LONG, timestamp=candle.timestamp)
        if index == 1 and context.position_direction is Direction.LONG:
            return SignalEvent("X", Direction.SHORT, timestamp=candle.timestamp)
        return None


def _sizing_candles() -> list[Candle]:
    return [
        _bar(0, 80.0, 101.0, 79.0, 100.0),
        _bar(1, 120.0, 151.0, 119.0, 150.0),
        _bar(2, 150.0, 151.0, 149.0, 150.0),
    ]


def _sizing_config() -> BacktestConfig:
    return BacktestConfig(initial_capital=10_000.0, commission_pct=0.0, slippage_pct=0.0)


class TestBacktestEnginePositionSizing:
    def test_default_sizer_retains_one_share_trade_compatibility(self) -> None:
        result = BacktestEngine().run(_BuyOnZeroSellOnOne(), _sizing_candles(), _sizing_config())

        assert result.trades[0].quantity == 1
        assert result.final_equity == pytest.approx(10_030.0)

    def test_fixed_quantity_sizer_controls_the_opening_fill(self) -> None:
        result = BacktestEngine().run(
            _BuyOnZeroSellOnOne(),
            _sizing_candles(),
            _sizing_config(),
            position_sizer=FixedQuantitySizer(7),
        )

        assert result.trades[0].quantity == 7
        assert result.trades[0].entry_date == _sizing_candles()[1].timestamp
        assert result.final_equity == pytest.approx(10_210.0)

    def test_fixed_fraction_sizer_uses_the_signal_bar_close_for_opening(self) -> None:
        result = BacktestEngine().run(
            _BuyOnZeroSellOnOne(),
            _sizing_candles(),
            _sizing_config(),
            position_sizer=FixedFractionSizer(0.10),
        )

        assert result.trades[0].quantity == 10
        assert result.final_equity == pytest.approx(10_300.0)

    def test_kelly_fallback_sizes_from_the_fresh_run_portfolio(self) -> None:
        result = BacktestEngine().run(
            _BuyOnZeroSellOnOne(),
            _sizing_candles(),
            _sizing_config(),
            position_sizer=KellyCriterionSizer(lookback=1),
        )

        assert result.trades[0].quantity == 2
        assert result.final_equity == pytest.approx(10_060.0)

    def test_close_uses_the_exact_filled_quantity_instead_of_resizing(self) -> None:
        result = BacktestEngine().run(
            _BuyOnZeroSellOnOne(),
            _sizing_candles(),
            _sizing_config(),
            position_sizer=FixedFractionSizer(0.10),
        )

        assert result.trades[0].quantity == 10
        assert result.trades[0].is_closed


class TestBacktestEngineEventQueue:
    def test_strategy_executes_while_its_market_event_is_dispatched(self) -> None:
        class RecordingEngine(BacktestEngine):
            def __init__(self) -> None:
                self.dispatching_market = False

            def _dispatch_event(self, event, queue, state, candle) -> None:
                self.dispatching_market = isinstance(event, MarketEvent)
                try:
                    super()._dispatch_event(event, queue, state, candle)
                finally:
                    self.dispatching_market = False

        class MarketPhaseStrategy:
            name = "market-phase"
            parameters: dict[str, object] = {}

            def __init__(self, engine: RecordingEngine) -> None:
                self.engine = engine
                self.observed_market_dispatch: list[bool] = []

            def on_candle(
                self, candle: Candle, context: StrategyContext
            ) -> SignalEvent | None:
                del candle, context
                self.observed_market_dispatch.append(self.engine.dispatching_market)
                return None

        engine = RecordingEngine()
        strategy = MarketPhaseStrategy(engine)
        engine.run(strategy, make_candle_series(3), BacktestConfig())

        assert strategy.observed_market_dispatch == [True, True, True]

    def test_dispatches_a_fresh_run_local_event_queue_in_execution_order(self) -> None:
        class RecordingEngine(BacktestEngine):
            def __init__(self) -> None:
                self.dispatches: list[tuple[str, type[object], object, int]] = []

            def _dispatch_event(self, event, queue, state, candle) -> None:
                self.dispatches.append((state.run_id, type(event), queue, len(queue)))
                super()._dispatch_event(event, queue, state, candle)

        engine = RecordingEngine()
        candles = [
            _bar(0, 100.0, 101.0, 99.0, 100.0),
            _bar(1, 110.0, 111.0, 109.0, 110.0),
            _bar(2, 120.0, 121.0, 119.0, 120.0),
        ]

        first = engine.run(_BuyOnZeroSellOnOne(), candles, BacktestConfig())
        second = engine.run(_BuyOnZeroSellOnOne(), candles, BacktestConfig())

        dispatches_by_run: dict[str, list[tuple[type[object], object, int]]] = {}
        for run_id, event_type, queue, queue_length in engine.dispatches:
            dispatches_by_run.setdefault(run_id, []).append((event_type, queue, queue_length))

        assert set(dispatches_by_run) == {first.run_id, second.run_id}
        first_dispatches = dispatches_by_run[first.run_id]
        first_types = [event_type for event_type, _, _ in first_dispatches]
        first_queue = first_dispatches[0][1]
        second_queue = dispatches_by_run[second.run_id][0][1]

        assert first_types.index(MarketEvent) < first_types.index(SignalEvent)
        assert first_types.index(SignalEvent) < first_types.index(OrderEvent)
        assert first_types.index(OrderEvent) < first_types.index(FillEvent)
        assert first_types.count(MarketEvent) == len(candles)
        assert first_queue is not second_queue
        assert all(queue_length == 0 for _, _, _, queue_length in engine.dispatches)


class _BuyLimitThenCloseAfterFill:
    name = "limit-timing"
    parameters: dict[str, object] = {}

    def __init__(self, limit: float) -> None:
        self.limit = limit
        self.submitted = False

    def on_candle(
        self, candle: Candle, context: StrategyContext
    ) -> SignalEvent | None:
        if not self.submitted:
            self.submitted = True
            return SignalEvent(
                "X",
                Direction.LONG,
                timestamp=candle.timestamp,
                order_type=OrderType.LIMIT,
                limit_price=self.limit,
            )
        if context.position_direction is Direction.LONG:
            return SignalEvent("X", Direction.SHORT, timestamp=candle.timestamp)
        return None


class _SignalOnlyOnLastBar:
    name = "last-bar"
    parameters: dict[str, object] = {}

    def __init__(self, last_index: int = 2) -> None:
        self.index = 0
        self.last_index = last_index

    def on_candle(
        self, candle: Candle, context: StrategyContext
    ) -> SignalEvent | None:
        index = self.index
        self.index += 1
        if index == self.last_index:
            return SignalEvent("X", Direction.LONG, timestamp=candle.timestamp)
        return None


class _OpenShortOnce:
    name = "short-once"
    parameters: dict[str, object] = {}

    def __init__(self) -> None:
        self.submitted = False

    def on_candle(
        self, candle: Candle, context: StrategyContext
    ) -> SignalEvent | None:
        if self.submitted:
            return None
        self.submitted = True
        return SignalEvent("X", Direction.SHORT, timestamp=candle.timestamp)


class _OpenShortThenLimitCover:
    name = "short-then-limit-cover"
    parameters: dict[str, object] = {}

    def __init__(self) -> None:
        self.index = 0

    def on_candle(
        self, candle: Candle, context: StrategyContext
    ) -> SignalEvent | None:
        index = self.index
        self.index += 1
        if index == 0:
            return SignalEvent("X", Direction.SHORT, timestamp=candle.timestamp)
        if index == 1:
            return SignalEvent(
                "X",
                Direction.LONG,
                timestamp=candle.timestamp,
                order_type=OrderType.LIMIT,
                limit_price=900.0,
            )
        return None


def _margin_config() -> BacktestConfig:
    return BacktestConfig(
        initial_capital=1_000.0,
        commission_pct=0.0,
        slippage_pct=0.0,
        short_maintenance_margin=0.30,
    )


def _margin_breach_candles(include_cover_bar: bool = True) -> list[Candle]:
    candles = [
        _bar(0, 100, 100, 100, 100),
        _bar(1, 100, 1_000, 100, 1_000),
    ]
    if include_cover_bar:
        candles.append(_bar(2, 1_100, 1_110, 1_090, 1_100))
    return candles


def test_margin_breach_forces_cover_at_following_open() -> None:
    candles = _margin_breach_candles()
    result = BacktestEngine().run(_OpenShortOnce(), candles, _margin_config())

    assert len(result.trades) == 1
    assert result.trades[0].exit_date == candles[2].timestamp
    assert result.trades[0].exit_price == 1_100.0


def test_final_bar_margin_breach_does_not_invent_cover() -> None:
    result = BacktestEngine().run(
        _OpenShortOnce(), _margin_breach_candles(False), _margin_config()
    )

    assert result.trades == []


def test_margin_forced_cover_cannot_be_replaced_by_strategy_order() -> None:
    candles = _margin_breach_candles()
    result = BacktestEngine().run(
        _OpenShortThenLimitCover(), candles, _margin_config()
    )

    assert len(result.trades) == 1
    assert result.trades[0].exit_price == 1_100.0


def test_margin_call_replaces_unfilled_gtc_cover_in_lifecycle_order(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A margin call supersedes a live conditional cover before the next open."""
    strategy = _ScheduledSignals(
        {
            0: (Direction.SHORT, OrderType.MARKET, None),
            1: (Direction.LONG, OrderType.LIMIT, 1.0),
        }
    )
    candles = [
        _bar(0, 100, 100, 100, 100),
        _bar(1, 100, 100, 100, 100),
        _bar(2, 1_000, 1_000, 1_000, 1_000),
        _bar(3, 1_100, 1_100, 1_100, 1_100),
    ]

    with caplog.at_level(logging.DEBUG):
        result = BacktestEngine().run(strategy, candles, _margin_config())

    assert len(result.trades) == 1
    assert result.trades[0].direction is Direction.SHORT
    assert result.trades[0].exit_date == candles[3].timestamp
    assert result.trades[0].exit_price == 1_100.0

    lifecycle = [
        (index, getattr(record, "event", None), getattr(record, "event_fields", {}))
        for index, record in enumerate(caplog.records)
    ]

    def event_index(event: str, **expected_fields: object) -> int:
        return next(
            index
            for index, actual_event, fields in lifecycle
            if actual_event == event
            and isinstance(fields, dict)
            and all(fields.get(name) == value for name, value in expected_fields.items())
        )

    untriggered = event_index("order.untriggered", order_type="LIMIT", direction="LONG")
    replaced = event_index(
        "order.replaced",
        order_type="LIMIT",
        direction="LONG",
        replacement_order_type="MARKET",
        replacement_direction="LONG",
        replacement_quantity=1,
    )
    forced_queued = event_index("order.queued", order_type="MARKET", direction="LONG")
    margin_queued = event_index("margin.call_queued", symbol="X", quantity=1)
    forced_filled = event_index("order.filled", order_type="MARKET", direction="LONG")

    assert untriggered < replaced < forced_queued < margin_queued < forced_filled
    events = [event for _, event, _ in lifecycle]
    assert "margin.call_unresolved" not in events
    assert "order.cancelled_end_of_data" not in events


class _ScheduledSignals:
    name = "scheduled"
    parameters: dict[str, object] = {}

    def __init__(
        self,
        schedule: dict[int, tuple[Direction, OrderType, float | None]],
    ) -> None:
        self.index = 0
        self.schedule = schedule

    def on_candle(
        self, candle: Candle, context: StrategyContext
    ) -> SignalEvent | None:
        index = self.index
        self.index += 1
        instruction = self.schedule.get(index)
        if instruction is None:
            return None
        direction, order_type, price = instruction
        return SignalEvent(
            "X",
            direction,
            timestamp=candle.timestamp,
            order_type=order_type,
            limit_price=price if order_type is OrderType.LIMIT else None,
            stop_price=price if order_type is OrderType.STOP else None,
        )


class _CaptureContexts:
    name = "contexts"
    parameters: dict[str, object] = {}

    def __init__(self) -> None:
        self.index = 0
        self.contexts: list[StrategyContext] = []

    def on_candle(
        self, candle: Candle, context: StrategyContext
    ) -> SignalEvent | None:
        index = self.index
        self.index += 1
        self.contexts.append(context)
        if index == 0:
            return SignalEvent(
                "X",
                Direction.LONG,
                timestamp=candle.timestamp,
                order_type=OrderType.LIMIT,
                limit_price=90.0,
            )
        if index == 1:
            return SignalEvent("X", Direction.LONG, timestamp=candle.timestamp)
        if index == 2:
            return SignalEvent("WRONG", Direction.SHORT, timestamp=candle.timestamp)
        return None


class _CaptureRejectedOrderContext:
    name = "rejected-order-context"
    parameters: dict[str, object] = {}

    def __init__(self) -> None:
        self.index = 0
        self.contexts: list[StrategyContext] = []

    def on_candle(
        self, candle: Candle, context: StrategyContext
    ) -> SignalEvent | None:
        index = self.index
        self.index += 1
        self.contexts.append(context)
        if index == 0:
            return SignalEvent("X", Direction.LONG, timestamp=candle.timestamp)
        return None


class TestBacktestEnginePendingOrders:
    def test_engine_accrues_short_borrow_from_previous_close_before_next_fill_phase(self) -> None:
        candles = [
            _bar(0, 100.0, 101.0, 99.0, 100.0),
            _bar(1, 100.0, 201.0, 99.0, 200.0),
            _bar(2, 50.0, 51.0, 49.0, 50.0),
        ]
        strategy = _ScheduledSignals({0: (Direction.SHORT, OrderType.MARKET, None)})

        result = BacktestEngine().run(
            strategy,
            candles,
            BacktestConfig(
                initial_capital=1_000.0,
                commission_pct=0.0,
                slippage_pct=0.0,
                annual_short_borrow_rate=0.365,
                borrow_day_count=365.0,
            ),
        )

        assert result.final_equity == pytest.approx(1_049.8)

    def test_signal_on_bar_n_fills_at_next_bar_open(self) -> None:
        candles = [
            _bar(0, 100.0, 101.0, 99.0, 100.0),
            _bar(1, 110.0, 111.0, 109.0, 110.0),
            _bar(2, 120.0, 121.0, 119.0, 120.0),
        ]

        result = BacktestEngine().run(
            _BuyOnZeroSellOnOne(), candles, BacktestConfig()
        )

        trade = result.trades[0]
        assert trade.entry_date == candles[1].timestamp
        assert trade.entry_price == pytest.approx(candles[1].open * 1.0005)
        assert trade.exit_date == candles[2].timestamp

    def test_limit_order_persists_until_later_bar_crosses(self) -> None:
        candles = [
            _bar(0, 100, 105, 99, 103),
            _bar(1, 103, 104, 101, 102),
            _bar(2, 98, 100, 95, 99),
            _bar(3, 100, 102, 99, 101),
        ]

        result = BacktestEngine().run(
            _BuyLimitThenCloseAfterFill(limit=99.0), candles, BacktestConfig()
        )

        assert result.trades[0].entry_date == candles[2].timestamp
        assert result.trades[0].exit_date == candles[3].timestamp

    def test_stop_order_persists_until_later_bar_crosses(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        candles = [
            _bar(0, 100, 101, 99, 100),
            _bar(1, 100, 104, 99, 100),
            _bar(2, 100, 105, 99, 104),
            _bar(3, 110, 111, 109, 110),
        ]
        strategy = _ScheduledSignals(
            {
                0: (Direction.LONG, OrderType.STOP, 105.0),
                2: (Direction.SHORT, OrderType.MARKET, None),
            }
        )

        with caplog.at_level(logging.DEBUG):
            result = BacktestEngine().run(strategy, candles, BacktestConfig())

        trade = result.trades[0]
        assert trade.entry_date == candles[2].timestamp
        assert trade.entry_price == pytest.approx(105.0 * 1.0005)
        assert trade.exit_date == candles[3].timestamp

        stop_events = [
            getattr(record, "event", None)
            for record in caplog.records
            if getattr(record, "event_fields", {}).get("order_type") == "STOP"
        ]
        assert stop_events == ["order.queued", "order.untriggered", "order.filled"]

    def test_final_bar_signal_is_not_filled(self) -> None:
        result = BacktestEngine().run(
            _SignalOnlyOnLastBar(), make_candle_series(3), BacktestConfig()
        )

        assert result.trades == []
        assert result.final_equity == result.initial_capital

    def test_newer_same_direction_limit_replaces_stale_pending_order(self) -> None:
        strategy = _ScheduledSignals(
            {
                0: (Direction.LONG, OrderType.LIMIT, 90.0),
                1: (Direction.LONG, OrderType.LIMIT, 95.0),
                2: (Direction.SHORT, OrderType.MARKET, None),
            }
        )
        candles = [
            _bar(0, 100, 101, 99, 100),
            _bar(1, 100, 101, 98, 100),
            _bar(2, 94, 96, 93, 95),
            _bar(3, 96, 97, 95, 96),
        ]

        result = BacktestEngine().run(strategy, candles, BacktestConfig())

        assert result.trades[0].entry_date == candles[2].timestamp

    def test_opposite_signal_replaces_unfilled_entry(self) -> None:
        strategy = _ScheduledSignals(
            {
                0: (Direction.LONG, OrderType.LIMIT, 90.0),
                1: (Direction.SHORT, OrderType.MARKET, None),
                2: (Direction.LONG, OrderType.MARKET, None),
            }
        )
        candles = [
            _bar(0, 100, 101, 99, 100),
            _bar(1, 100, 101, 98, 100),
            _bar(2, 99, 100, 98, 99),
            _bar(3, 98, 99, 97, 98),
        ]

        result = BacktestEngine().run(strategy, candles, BacktestConfig())

        assert result.trades[0].direction is Direction.SHORT
        assert result.trades[0].entry_date == candles[2].timestamp

    def test_context_reflects_pending_and_filled_execution_state(self) -> None:
        strategy = _CaptureContexts()
        candles = [
            _bar(0, 100, 101, 99, 100),
            _bar(1, 100, 101, 99, 100),
            _bar(2, 101, 102, 100, 101),
            _bar(3, 102, 103, 101, 102),
        ]

        result = BacktestEngine().run(strategy, candles, BacktestConfig())

        assert strategy.contexts[1] == StrategyContext(
            pending_direction=Direction.LONG,
            pending_order_type=OrderType.LIMIT,
        )
        assert strategy.contexts[2] == StrategyContext(
            position_direction=Direction.LONG,
            position_quantity=1,
        )
        assert strategy.contexts[2].forced_cover_pending is False
        assert result.trades[0].symbol == "X"

    def test_conditional_signal_requires_a_price(self) -> None:
        strategy = _ScheduledSignals(
            {0: (Direction.LONG, OrderType.LIMIT, None)}
        )

        with pytest.raises(ValueError, match="LIMIT order requires limit_price"):
            BacktestEngine().run(strategy, make_candle_series(2), BacktestConfig())

    def test_rejected_order_is_absent_from_next_strategy_context(self) -> None:
        strategy = _CaptureRejectedOrderContext()
        candles = [
            _bar(0, 100, 101, 99, 100),
            _bar(1, 100, 101, 99, 100),
        ]

        result = BacktestEngine().run(
            strategy,
            candles,
            BacktestConfig(initial_capital=50.0),
        )

        assert strategy.contexts[1] == StrategyContext()
        assert result.trades == []
        assert result.final_equity == 50.0

    def test_candles_require_strictly_increasing_timestamps(self) -> None:
        candles = make_candle_series(2)
        candles[1] = Candle(
            timestamp=candles[0].timestamp,
            open=candles[1].open,
            high=candles[1].high,
            low=candles[1].low,
            close=candles[1].close,
            volume=candles[1].volume,
            adj_close=candles[1].adj_close,
        )

        with pytest.raises(
            ValueError, match="candles must have strictly increasing timestamps"
        ):
            BacktestEngine().run(_SignalOnlyOnLastBar(), candles, BacktestConfig())
