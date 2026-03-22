"""Tests for Permutation Testing — Step 8."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from src.analytics.permutation_test import PermutationResult, PermutationTester
from src.models.candle import Candle
from src.strategies.ma_crossover import MACrossoverStrategy


def make_candles(n: int = 100, drift: float = 0.001, seed: int = 42) -> list[Candle]:
    import random
    rng = random.Random(seed)
    candles = []
    price = 100.0
    start = datetime(2020, 1, 2)
    for i in range(n):
        change = drift + rng.gauss(0, 0.015)
        price *= (1 + change)
        candles.append(Candle(
            timestamp=start + timedelta(days=i),
            open=price * 0.999,
            high=price * 1.005,
            low=price * 0.994,
            close=price,
            volume=1_000_000.0,
            adj_close=price,
        ))
    return candles


class TestPermutationResult:
    def test_dataclass_fields(self) -> None:
        r = PermutationResult(
            actual_metric=1.5,
            permuted_metrics=[0.1, 0.3, 0.2],
            p_value=0.0,
            is_significant=True,
            percentile=100.0,
        )
        assert r.actual_metric == 1.5
        assert r.is_significant is True

    def test_p_value_range(self) -> None:
        """p-value must be in [0, 1]."""
        r = PermutationResult(
            actual_metric=0.5,
            permuted_metrics=[0.1] * 10,
            p_value=0.0,
            is_significant=True,
            percentile=90.0,
        )
        assert 0.0 <= r.p_value <= 1.0


class TestPermutationTester:
    def test_instantiation(self) -> None:
        strategy = MACrossoverStrategy(fast_period=5, slow_period=20)
        candles = make_candles(100)
        tester = PermutationTester(strategy, candles, n_permutations=10)
        assert tester.n_permutations == 10

    def test_returns_permutation_result(self) -> None:
        strategy = MACrossoverStrategy(fast_period=5, slow_period=20)
        candles = make_candles(150)
        tester = PermutationTester(strategy, candles, n_permutations=10, seed=42)
        result = tester.run()
        assert isinstance(result, PermutationResult)

    def test_permuted_metrics_length(self) -> None:
        """Should return exactly n_permutations permuted metrics."""
        strategy = MACrossoverStrategy(fast_period=5, slow_period=20)
        candles = make_candles(150)
        tester = PermutationTester(strategy, candles, n_permutations=20, seed=42)
        result = tester.run()
        assert len(result.permuted_metrics) == 20

    def test_p_value_in_range(self) -> None:
        strategy = MACrossoverStrategy(fast_period=5, slow_period=20)
        candles = make_candles(150)
        tester = PermutationTester(strategy, candles, n_permutations=10, seed=42)
        result = tester.run()
        assert 0.0 <= result.p_value <= 1.0

    def test_is_significant_matches_p_value(self) -> None:
        strategy = MACrossoverStrategy(fast_period=5, slow_period=20)
        candles = make_candles(150)
        tester = PermutationTester(strategy, candles, n_permutations=10, seed=42)
        result = tester.run()
        assert result.is_significant == (result.p_value < 0.05)

    def test_percentile_in_range(self) -> None:
        strategy = MACrossoverStrategy(fast_period=5, slow_period=20)
        candles = make_candles(150)
        tester = PermutationTester(strategy, candles, n_permutations=10, seed=42)
        result = tester.run()
        assert 0.0 <= result.percentile <= 100.0

    def test_actual_metric_is_float(self) -> None:
        strategy = MACrossoverStrategy(fast_period=5, slow_period=20)
        candles = make_candles(150)
        tester = PermutationTester(strategy, candles, n_permutations=10, seed=42)
        result = tester.run()
        assert isinstance(result.actual_metric, float)

    @pytest.mark.timeout(60)
    def test_small_permutation_count_fast(self) -> None:
        """Permutation test with n=10 should complete quickly."""
        strategy = MACrossoverStrategy(fast_period=5, slow_period=20)
        candles = make_candles(200)
        tester = PermutationTester(strategy, candles, n_permutations=10, seed=42)
        result = tester.run()
        assert result is not None
