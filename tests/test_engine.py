"""Tests for BacktestEngine."""
from __future__ import annotations

from typing import Optional

import pytest

from src.engine.backtest import BacktestConfig, BacktestEngine, BacktestResult
from src.engine.event import SignalEvent
from src.models.candle import Candle
from src.models.order import Direction
from tests.conftest import make_candle, make_candle_series


# ---------------------------------------------------------------------------
# Stub strategies used across tests
# ---------------------------------------------------------------------------

class _NeverSignalStrategy:
    """Strategy that never emits a signal."""

    name = "NeverSignal"
    parameters: dict = {}
    symbol: str = "UNKNOWN"

    def on_candle(self, candle: Candle) -> Optional[SignalEvent]:
        return None


class _BuyDay0SellDay49Strategy:
    """Buys on bar 0, sells on bar 49 (signals queued for next bar execution)."""

    name = "BuyDay0SellDay49"
    parameters: dict = {}
    symbol: str = "UNKNOWN"

    def __init__(self, symbol: str = "UNKNOWN") -> None:
        self.symbol = symbol
        self._bar = 0

    def on_candle(self, candle: Candle) -> Optional[SignalEvent]:
        bar = self._bar
        self._bar += 1
        if bar == 0:
            return SignalEvent(
                symbol=self.symbol,
                direction=Direction.LONG,
                timestamp=candle.timestamp,
            )
        if bar == 49:
            return SignalEvent(
                symbol=self.symbol,
                direction=Direction.SHORT,
                timestamp=candle.timestamp,
            )
        return None


# ---------------------------------------------------------------------------
# SimulatedBroker fill price tests (via engine integration)
# ---------------------------------------------------------------------------

class TestSimulatedBrokerMarket:
    """Market fill prices — fills at candle.open not close (R-23)."""

    SLIPPAGE = 0.0005
    COMMISSION = 0.001

    def test_market_long_fill_price(self) -> None:
        from src.engine.broker import SimulatedBroker
        from src.models.order import Order

        broker = SimulatedBroker(slippage_pct=self.SLIPPAGE)
        candle = make_candle(close=100.0, open_=99.0, high=101.0, low=98.0)
        order = Order(symbol="TEST", direction=Direction.LONG, quantity=1)
        fill = broker.execute(order, candle)
        assert fill is not None
        expected = 99.0 * (1 + self.SLIPPAGE)
        assert abs(fill.fill_price - expected) < 1e-9

    def test_market_short_fill_price(self) -> None:
        from src.engine.broker import SimulatedBroker
        from src.models.order import Order

        broker = SimulatedBroker(slippage_pct=self.SLIPPAGE)
        candle = make_candle(close=100.0, open_=99.0, high=101.0, low=98.0)
        order = Order(symbol="TEST", direction=Direction.SHORT, quantity=1)
        fill = broker.execute(order, candle)
        assert fill is not None
        expected = 99.0 * (1 - self.SLIPPAGE)
        assert abs(fill.fill_price - expected) < 1e-9

    def test_market_commission_is_percentage_of_notional(self) -> None:
        from src.engine.broker import SimulatedBroker
        from src.models.order import Order

        broker = SimulatedBroker(
            slippage_pct=self.SLIPPAGE, commission_pct=self.COMMISSION
        )
        candle = make_candle(close=100.0, open_=99.0, high=101.0, low=98.0)
        order = Order(symbol="TEST", direction=Direction.LONG, quantity=1)
        fill = broker.execute(order, candle)
        assert fill is not None
        expected_commission = fill.fill_price * 1 * self.COMMISSION
        assert abs(fill.commission - expected_commission) < 1e-9


# ---------------------------------------------------------------------------
# BacktestEngine tests
# ---------------------------------------------------------------------------

class TestBacktestEngine:
    SLIPPAGE = 0.0005
    COMMISSION = 0.001
    INITIAL_CAPITAL = 100_000.0

    def _config(self) -> BacktestConfig:
        return BacktestConfig(
            initial_capital=self.INITIAL_CAPITAL,
            slippage_pct=self.SLIPPAGE,
            commission_pct=self.COMMISSION,
        )

    def test_buy_day0_sell_day49_pnl_matches_expected(self) -> None:
        candles = make_candle_series(n=51, base=100.0)
        strategy = _BuyDay0SellDay49Strategy(symbol="TEST")
        engine = BacktestEngine()
        result = engine.run(strategy, candles, self._config(), symbol="TEST")

        # Entry at bar 1 open (next bar after signal on bar 0)
        buy_open = candles[1].open   # 100.5 - 0.3 = 100.2
        # Exit at bar 50 open (next bar after signal on bar 49)
        sell_open = candles[50].open  # 125.0 - 0.3 = 124.7
        qty = 1

        entry_price = buy_open * (1 + self.SLIPPAGE)
        exit_price = sell_open * (1 - self.SLIPPAGE)
        entry_comm = entry_price * qty * self.COMMISSION
        exit_comm = exit_price * qty * self.COMMISSION

        expected_pnl = (exit_price - entry_price) * qty - entry_comm - exit_comm
        expected_equity = self.INITIAL_CAPITAL + expected_pnl

        assert abs(result.final_equity - expected_equity) < 1e-6

    def test_buy_day0_sell_day49_final_equity_gt_initial(self) -> None:
        candles = make_candle_series(n=51, base=100.0)
        strategy = _BuyDay0SellDay49Strategy(symbol="TEST")
        engine = BacktestEngine()
        result = engine.run(strategy, candles, self._config(), symbol="TEST")
        assert result.final_equity > self.INITIAL_CAPITAL

    def test_result_has_one_closed_trade(self) -> None:
        candles = make_candle_series(n=51, base=100.0)
        strategy = _BuyDay0SellDay49Strategy(symbol="TEST")
        engine = BacktestEngine()
        result = engine.run(strategy, candles, self._config(), symbol="TEST")
        assert len(result.trades) == 1

    def test_result_equity_curve_length(self) -> None:
        candles = make_candle_series(n=51, base=100.0)
        strategy = _BuyDay0SellDay49Strategy(symbol="TEST")
        engine = BacktestEngine()
        result = engine.run(strategy, candles, self._config(), symbol="TEST")
        assert len(result.equity_curve) == 51  # one point per candle

    def test_result_metadata(self) -> None:
        candles = make_candle_series(n=51, base=100.0)
        strategy = _BuyDay0SellDay49Strategy(symbol="TEST")
        engine = BacktestEngine()
        result = engine.run(strategy, candles, self._config(), symbol="TEST")
        assert result.strategy_name == "BuyDay0SellDay49"
        assert result.symbol == "TEST"
        assert result.start_date == candles[0].timestamp
        assert result.end_date == candles[-1].timestamp
        assert result.initial_capital == self.INITIAL_CAPITAL

    def test_no_signal_strategy_preserves_capital(self) -> None:
        candles = make_candle_series(n=20)
        strategy = _NeverSignalStrategy()
        engine = BacktestEngine()
        result = engine.run(strategy, candles, self._config(), symbol="TEST")
        assert result.final_equity == self.INITIAL_CAPITAL

    def test_empty_candles_raises(self) -> None:
        strategy = _NeverSignalStrategy()
        engine = BacktestEngine()
        with pytest.raises(ValueError, match="candles list must not be empty"):
            engine.run(strategy, [], self._config())

    def test_pending_orders_cleared_between_runs(self) -> None:
        """Re-using an engine instance should not carry over pending orders."""
        candles = make_candle_series(n=51, base=100.0)
        strategy1 = _BuyDay0SellDay49Strategy(symbol="TEST")
        strategy2 = _NeverSignalStrategy()
        engine = BacktestEngine()
        engine.run(strategy1, candles, self._config(), symbol="TEST")
        # Second run with a strategy that never signals — should preserve capital
        candles2 = make_candle_series(n=10, base=100.0)
        result2 = engine.run(strategy2, candles2, self._config(), symbol="TEST")
        assert result2.final_equity == self.INITIAL_CAPITAL

    def test_fixed_fraction_sizer_is_respected(self) -> None:
        """FixedFractionSizer with 10% equity at price ~100 should buy ~100 shares."""
        from src.engine.position_sizer import FixedFractionSizer
        candles = make_candle_series(n=51, base=100.0)
        strategy = _BuyDay0SellDay49Strategy(symbol="TEST")
        config = BacktestConfig(
            initial_capital=100_000.0,
            position_sizer=FixedFractionSizer(fraction=0.10),
        )
        result = BacktestEngine().run(strategy, candles, config, symbol="TEST")
        assert len(result.trades) == 1
        trade = result.trades[0]
        # 10% of 100_000 / 100.0 = 100 shares
        assert trade.quantity == 100
