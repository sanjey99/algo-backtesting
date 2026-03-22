"""Tests for PermutationTest."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from src.analytics.permutation_test import PermutationTest, PermutationTestResult
from src.engine.backtest import BacktestConfig
from src.models.candle import Candle
from src.strategies.ma_crossover import MACrossover


def _make_candles(n: int = 100, base: float = 100.0) -> list[Candle]:
    start = datetime(2022, 1, 3)
    candles = []
    price = base
    for i in range(n):
        price = price * (1 + 0.001 * (1 if i % 3 == 0 else -0.3))
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


class TestPermutationTest:
    def test_run_returns_result(self) -> None:
        candles = _make_candles(80)
        strategy = MACrossover(fast_period=5, slow_period=20)
        pt = PermutationTest(strategy, candles, n_permutations=10)
        result = pt.run()
        assert isinstance(result, PermutationTestResult)

    def test_p_value_in_range(self) -> None:
        candles = _make_candles(80)
        strategy = MACrossover(fast_period=5, slow_period=20)
        pt = PermutationTest(strategy, candles, n_permutations=20)
        result = pt.run()
        assert 0.0 < result.p_value <= 1.0

    def test_p_value_never_zero(self) -> None:
        """R-33: p-value formula includes +1 in numerator/denominator."""
        candles = _make_candles(80)
        strategy = MACrossover(fast_period=5, slow_period=20)
        pt = PermutationTest(strategy, candles, n_permutations=20)
        result = pt.run()
        # With (count+1)/(n+1), p-value can never be exactly 0
        assert result.p_value > 0.0

    def test_permuted_metrics_count(self) -> None:
        candles = _make_candles(80)
        strategy = MACrossover(fast_period=5, slow_period=20)
        n = 15
        pt = PermutationTest(strategy, candles, n_permutations=n)
        result = pt.run()
        assert result.n_permutations == n
        assert len(result.permuted_metrics) == n

    def test_is_significant_property(self) -> None:
        result = PermutationTestResult(
            actual_metric=1.0,
            permuted_metrics=[0.5] * 10,
            p_value=0.03,
            n_permutations=10,
            metric="sharpe_ratio",
        )
        assert result.is_significant is True

    def test_not_significant(self) -> None:
        result = PermutationTestResult(
            actual_metric=0.1,
            permuted_metrics=[0.5] * 10,
            p_value=0.8,
            n_permutations=10,
            metric="sharpe_ratio",
        )
        assert result.is_significant is False

    def test_reproducible_with_seed(self) -> None:
        candles = _make_candles(80)
        strategy1 = MACrossover(fast_period=5, slow_period=20)
        strategy2 = MACrossover(fast_period=5, slow_period=20)
        pt1 = PermutationTest(strategy1, candles, n_permutations=10, random_seed=42)
        pt2 = PermutationTest(strategy2, candles, n_permutations=10, random_seed=42)
        r1 = pt1.run()
        r2 = pt2.run()
        assert r1.permuted_metrics == r2.permuted_metrics
        assert r1.p_value == r2.p_value
