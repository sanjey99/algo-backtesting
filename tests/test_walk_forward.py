"""Tests for WalkForwardAnalyzer."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from src.analytics.walk_forward import WalkForwardAnalyzer, WindowResult
from src.engine.backtest import BacktestConfig
from src.models.candle import Candle
from src.strategies.ma_crossover import MACrossover


def _make_candles(n: int = 200, base: float = 100.0) -> list[Candle]:
    start = datetime(2022, 1, 3)
    candles = []
    price = base
    for i in range(n):
        price = price * (1 + 0.001 * (1 if i % 2 == 0 else -0.5))
        candles.append(
            Candle(
                timestamp=start + timedelta(days=i),
                open=price - 0.1,
                high=price + 0.5,
                low=price - 0.5,
                close=price,
                volume=1_000_000,
                adj_close=price,
            )
        )
    return candles


class TestWalkForwardAnalyzer:
    def test_run_returns_window_results(self) -> None:
        candles = _make_candles(200)
        config = BacktestConfig(initial_capital=10_000.0)
        wfa = WalkForwardAnalyzer(
            MACrossover,
            config=config,
            n_windows=4,
            n_trials=5,
        )
        results = wfa.run(candles)
        assert len(results) == 4
        for r in results:
            assert isinstance(r, WindowResult)

    def test_window_result_has_datetime_dates(self) -> None:
        """R-12: WindowResult stores datetime, not int indices."""
        candles = _make_candles(100)
        wfa = WalkForwardAnalyzer(MACrossover, n_windows=3, n_trials=3)
        results = wfa.run(candles)
        for r in results:
            assert isinstance(r.in_sample_start, datetime)
            assert isinstance(r.in_sample_end, datetime)
            assert isinstance(r.oos_start, datetime)
            assert isinstance(r.oos_end, datetime)

    def test_oos_dates_after_is_dates(self) -> None:
        """OOS window starts after in-sample window ends."""
        candles = _make_candles(120)
        wfa = WalkForwardAnalyzer(MACrossover, n_windows=3, n_trials=3)
        results = wfa.run(candles)
        for r in results:
            assert r.oos_start > r.in_sample_end

    def test_parameter_space_cached(self) -> None:
        """R-32: _parameter_space is populated in __init__."""
        wfa = WalkForwardAnalyzer(MACrossover)
        assert hasattr(wfa, "_parameter_space")
        assert "fast_period" in wfa._parameter_space
        assert "slow_period" in wfa._parameter_space

    def test_raises_on_too_few_candles(self) -> None:
        wfa = WalkForwardAnalyzer(MACrossover, n_windows=5)
        with pytest.raises(ValueError):
            wfa.run([])

    def test_window_index_sequential(self) -> None:
        candles = _make_candles(150)
        wfa = WalkForwardAnalyzer(MACrossover, n_windows=3, n_trials=3)
        results = wfa.run(candles)
        indices = [r.window_index for r in results]
        assert indices == list(range(len(results)))

    def test_oos_sharpe_is_float(self) -> None:
        candles = _make_candles(150)
        wfa = WalkForwardAnalyzer(MACrossover, n_windows=3, n_trials=3)
        results = wfa.run(candles)
        for r in results:
            assert isinstance(r.oos_sharpe, float)
