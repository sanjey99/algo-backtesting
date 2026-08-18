"""Tests for strategy framework and all three concrete strategies."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from src.engine.context import StrategyContext
from src.engine.event import SignalEvent
from src.models.candle import Candle
from src.models.order import Direction, OrderType
from src.strategies import (
    STRATEGY_REGISTRY,
    BreakoutStrategy,
    MACrossoverStrategy,
    RSIMeanReversionStrategy,
)


def make_candle_with_price(
    price: float, dt: datetime, high_offset: float = 0.5, low_offset: float = 0.5
) -> Candle:
    return Candle(
        timestamp=dt,
        open=price - 0.1,
        high=price + high_offset,
        low=price - low_offset,
        close=price,
        volume=1_000_000.0,
        adj_close=price,
    )


def _ma_crossing_candles() -> list[Candle]:
    prices = (1.0, 2.0, 3.0, 4.0, 1.0)
    return [
        make_candle_with_price(price, datetime(2023, 1, index + 1))
        for index, price in enumerate(prices)
    ]


# ── Registry ─────────────────────────────────────────────────────────────────


class TestStrategyRegistry:
    def test_all_strategies_registered(self) -> None:
        assert "ma_crossover" in STRATEGY_REGISTRY
        assert "rsi_mean_reversion" in STRATEGY_REGISTRY
        assert "breakout" in STRATEGY_REGISTRY

    def test_registry_returns_correct_classes(self) -> None:
        assert STRATEGY_REGISTRY["ma_crossover"] is MACrossoverStrategy
        assert STRATEGY_REGISTRY["rsi_mean_reversion"] is RSIMeanReversionStrategy
        assert STRATEGY_REGISTRY["breakout"] is BreakoutStrategy

    def test_instantiate_from_registry(self) -> None:
        strategy = STRATEGY_REGISTRY["ma_crossover"]()
        assert strategy.name == "ma_crossover"


# ── BaseStrategy ──────────────────────────────────────────────────────────────


class TestBaseStrategy:
    def test_has_parameter_space(self) -> None:
        s = MACrossoverStrategy()
        assert "fast_period" in s.parameter_space

    def test_has_parameters(self) -> None:
        s = MACrossoverStrategy(fast_period=5, slow_period=20)
        assert s.parameters["fast_period"] == 5
        assert s.parameters["slow_period"] == 20


# ── MACrossoverStrategy ───────────────────────────────────────────────────────


class TestMACrossoverStrategy:
    def test_death_cross_without_filled_long_does_not_open_short(self) -> None:
        strategy = MACrossoverStrategy(fast_period=2, slow_period=3)
        context = StrategyContext()

        signals = [
            strategy.on_candle(candle, context) for candle in _ma_crossing_candles()
        ]

        assert all(
            signal is None or signal.direction is not Direction.SHORT
            for signal in signals
        )

    def test_death_cross_with_filled_long_emits_close(self) -> None:
        strategy = MACrossoverStrategy(fast_period=2, slow_period=3)
        long_context = StrategyContext(
            position_direction=Direction.LONG, position_quantity=1
        )

        signals = [
            strategy.on_candle(candle, long_context)
            for candle in _ma_crossing_candles()
        ]

        assert any(
            signal is not None and signal.direction is Direction.SHORT
            for signal in signals
        )

    def test_fast_must_be_less_than_slow(self) -> None:
        with pytest.raises(ValueError, match="fast_period"):
            MACrossoverStrategy(fast_period=50, slow_period=20)

    def test_equal_periods_raises(self) -> None:
        with pytest.raises(ValueError):
            MACrossoverStrategy(fast_period=20, slow_period=20)

    def test_no_signal_during_warmup(self) -> None:
        """No signals during the slow_period warmup bars."""
        s = MACrossoverStrategy(fast_period=5, slow_period=20)
        context = StrategyContext()
        start = datetime(2023, 1, 2)
        for i in range(20):
            candle = make_candle_with_price(100.0, start + timedelta(days=i))
            signal = s.on_candle(candle, context)
            assert signal is None, f"Expected None at bar {i}"

    def test_golden_cross_generates_long(self) -> None:
        """Price rising strongly should generate a LONG signal."""
        s = MACrossoverStrategy(fast_period=3, slow_period=10)
        start = datetime(2023, 1, 2)
        context = StrategyContext()
        signals: list[SignalEvent | None] = []

        # First 10 bars: flat price (no cross)
        for i in range(10):
            c = make_candle_with_price(100.0, start + timedelta(days=i))
            signals.append(s.on_candle(c, context))

        # Next bars: sharply rising price — forces fast SMA above slow SMA
        for i in range(10):
            c = make_candle_with_price(120.0 + i * 2, start + timedelta(days=10 + i))
            signals.append(s.on_candle(c, context))

        long_signals = [
            sig for sig in signals if sig is not None and sig.direction == Direction.LONG
        ]
        assert len(long_signals) >= 1

    def test_reset_clears_state(self) -> None:
        s = MACrossoverStrategy(fast_period=3, slow_period=10)
        start = datetime(2023, 1, 2)
        context = StrategyContext()
        for i in range(15):
            s.on_candle(
                make_candle_with_price(100.0 + i, start + timedelta(days=i)), context
            )

        s.reset()
        # After reset, warmup should restart
        for i in range(10):
            sig = s.on_candle(
                make_candle_with_price(100.0, start + timedelta(days=i)), context
            )
            assert sig is None

    def test_name_and_parameters(self) -> None:
        s = MACrossoverStrategy(fast_period=10, slow_period=50)
        assert s.name == "ma_crossover"
        assert s.parameters["fast_period"] == 10
        assert s.parameters["slow_period"] == 50

    def test_parameter_space_defined(self) -> None:
        s = MACrossoverStrategy()
        assert "fast_period" in s.parameter_space
        assert "slow_period" in s.parameter_space


# ── RSIMeanReversionStrategy ──────────────────────────────────────────────────


class TestRSIMeanReversionStrategy:
    def test_no_signal_during_warmup(self) -> None:
        s = RSIMeanReversionStrategy(period=14)
        context = StrategyContext()
        start = datetime(2023, 1, 2)
        for i in range(14):
            sig = s.on_candle(
                make_candle_with_price(100.0, start + timedelta(days=i)), context
            )
            assert sig is None

    def test_long_signal_on_oversold(self) -> None:
        """A seeded zero RSI opens a long position below the oversold threshold."""
        s = RSIMeanReversionStrategy(period=3, oversold=30.0)
        context = StrategyContext()
        prices = (100.0, 98.0, 96.0, 94.0)

        signals = [
            s.on_candle(
                make_candle_with_price(price, datetime(2023, 1, index + 2)), context
            )
            for index, price in enumerate(prices)
        ]

        assert signals[-1] is not None
        assert signals[-1].direction is Direction.LONG
        assert signals[-1].strength == pytest.approx(1.0)

    def test_exit_signal_when_rsi_recovers(self) -> None:
        """A long position closes when the Wilder-smoothed RSI recovers."""
        s = RSIMeanReversionStrategy(period=3, oversold=30.0, exit_level=50.0)
        start = datetime(2023, 1, 2)
        empty_context = StrategyContext()
        long_context = StrategyContext(
            position_direction=Direction.LONG, position_quantity=1
        )

        for index, price in enumerate((100.0, 98.0, 96.0, 94.0)):
            s.on_candle(
                make_candle_with_price(price, start + timedelta(days=index)), empty_context
            )
        exit_signal = s.on_candle(
            make_candle_with_price(100.0, start + timedelta(days=4)), long_context
        )

        assert exit_signal is not None
        assert exit_signal.direction is Direction.SHORT

    def test_rsi_uses_wilder_smoothing_after_the_initial_seed(self) -> None:
        """Later values must retain the initial averages instead of reseeding a window."""
        s = RSIMeanReversionStrategy(period=3, oversold=0.0)
        context = StrategyContext()
        prices = (100.0, 102.0, 101.0, 104.0, 103.0, 105.0)
        observed: list[float] = []

        for index, price in enumerate(prices):
            s.on_candle(
                make_candle_with_price(price, datetime(2023, 1, index + 2)), context
            )
            if s._rsi is not None:
                observed.append(s._rsi)

        assert observed == pytest.approx([83.3333333333, 66.6666666667, 79.1666666667])

    def test_reset_clears_state(self) -> None:
        s = RSIMeanReversionStrategy(period=5, oversold=30.0)
        start = datetime(2023, 1, 2)
        context = StrategyContext()
        for i in range(20):
            s.on_candle(
                make_candle_with_price(100.0 - i, start + timedelta(days=i)), context
            )
        s.reset()
        assert s._avg_gain is None
        assert s._avg_loss is None
        assert s._rsi is None
        assert all(
            s.on_candle(
                make_candle_with_price(100.0, start + timedelta(days=i)), context
            )
            is None
            for i in range(3)
        )

    def test_name_and_parameters(self) -> None:
        s = RSIMeanReversionStrategy(period=14, oversold=30.0)
        assert s.name == "rsi_mean_reversion"
        assert s.parameters["period"] == 14

    def test_parameter_space_defined(self) -> None:
        s = RSIMeanReversionStrategy()
        assert "period" in s.parameter_space


# ── BreakoutStrategy ──────────────────────────────────────────────────────────


class TestBreakoutStrategy:
    def test_no_signal_during_warmup(self) -> None:
        s = BreakoutStrategy(lookback=20)
        context = StrategyContext()
        start = datetime(2023, 1, 2)
        for i in range(20):
            sig = s.on_candle(
                make_candle_with_price(100.0, start + timedelta(days=i)), context
            )
            assert sig is None

    def test_breakout_generates_long(self) -> None:
        """Price breaking above Donchian channel high → LONG signal."""
        s = BreakoutStrategy(lookback=10)
        start = datetime(2023, 1, 2)
        context = StrategyContext()
        signals = []

        # Flat period (channels at 100 high / 99.5 low)
        for i in range(10):
            signals.append(
                s.on_candle(
                    make_candle_with_price(100.0, start + timedelta(days=i)), context
                )
            )

        # Breakout: price surges to 110 — above previous high of 100.5
        candle = make_candle_with_price(110.0, start + timedelta(days=10), high_offset=0.5)
        signals.append(s.on_candle(candle, context))

        long_signals = [
            sig for sig in signals if sig is not None and sig.direction == Direction.LONG
        ]
        assert len(long_signals) == 1
        assert long_signals[0].order_type is OrderType.STOP
        assert long_signals[0].stop_price == pytest.approx(100.5)

    def test_breakout_stop_uses_the_prior_channel_high_not_current_bar_high(self) -> None:
        s = BreakoutStrategy(lookback=3)
        context = StrategyContext()
        start = datetime(2023, 1, 2)

        for index, (price, high) in enumerate(((10.0, 11.0), (11.0, 14.0), (12.0, 13.0))):
            s.on_candle(
                make_candle_with_price(
                    price, start + timedelta(days=index), high_offset=high - price
                ),
                context,
            )
        signal = s.on_candle(
            make_candle_with_price(15.0, start + timedelta(days=3), high_offset=35.0),
            context,
        )

        assert signal is not None
        assert signal.order_type is OrderType.STOP
        assert signal.stop_price == 14.0

    def test_breakdown_generates_short_exit(self) -> None:
        """After LONG entry, price breaking below channel low → SHORT (exit)."""
        s = BreakoutStrategy(lookback=5)
        start = datetime(2023, 1, 2)
        empty_context = StrategyContext()
        long_context = StrategyContext(
            position_direction=Direction.LONG, position_quantity=1
        )

        # Warmup at 100
        for i in range(5):
            s.on_candle(
                make_candle_with_price(100.0, start + timedelta(days=i)), empty_context
            )

        # Trigger LONG
        s.on_candle(
            make_candle_with_price(110.0, start + timedelta(days=5)), empty_context
        )

        # Price breaks below channel low
        exit_signal = s.on_candle(
            make_candle_with_price(85.0, start + timedelta(days=6), low_offset=5.0),
            long_context,
        )
        assert exit_signal is not None
        assert exit_signal.direction == Direction.SHORT
        assert exit_signal.order_type is OrderType.MARKET
        assert exit_signal.stop_price is None

    def test_reset_clears_state(self) -> None:
        s = BreakoutStrategy(lookback=5)
        start = datetime(2023, 1, 2)
        context = StrategyContext()
        for i in range(15):
            s.on_candle(
                make_candle_with_price(100.0 + i, start + timedelta(days=i)), context
            )

        s.reset()
        # After reset, need warmup again
        for i in range(5):
            sig = s.on_candle(
                make_candle_with_price(100.0, start + timedelta(days=i)), context
            )
            assert sig is None

    def test_name_and_parameters(self) -> None:
        s = BreakoutStrategy(lookback=20)
        assert s.name == "breakout"
        assert s.parameters["lookback"] == 20

    def test_parameter_space_defined(self) -> None:
        s = BreakoutStrategy()
        assert "lookback" in s.parameter_space
