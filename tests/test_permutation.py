"""Tests for Permutation Testing — Step 8."""
from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime, timedelta

import pytest

from src.analytics.permutation_test import (
    PermutationResult,
    PermutationTester,
    _run_single_permutation,
)
from src.engine.backtest import BacktestConfig
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
    def test_permutation_worker_receives_margin_configuration(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = BacktestConfig(
            short_initial_margin=1.6,
            short_maintenance_margin=0.35,
            annual_short_borrow_rate=0.05,
        )
        captured: dict[str, object] = {}
        original_config = BacktestConfig

        def recording_config(**kwargs: object) -> BacktestConfig:
            captured.update(kwargs)
            return original_config(**kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr("src.engine.backtest.BacktestConfig", recording_config)

        _run_single_permutation(
            "src.strategies.ma_crossover.MACrossoverStrategy",
            {"fast_period": 2, "slow_period": 3},
            [0.01, -0.01, 0.02],
            [100.0, 101.0, 100.0, 102.0],
            [datetime(2023, 1, day) for day in range(1, 5)],
            "sharpe_ratio",
            asdict(config),
            42,
        )

        assert captured["short_initial_margin"] == 1.6
        assert captured["short_maintenance_margin"] == 0.35
        assert captured["annual_short_borrow_rate"] == 0.05
        assert captured["borrow_day_count"] == 365.0

    def test_instantiation(self) -> None:
        strategy = MACrossoverStrategy(fast_period=5, slow_period=20)
        candles = make_candles(100)
        tester = PermutationTester(strategy, candles, n_permutations=10)
        assert tester.n_permutations == 10

    def test_failed_worker_is_logged_without_changing_fallback_metric(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        class FailingFuture:
            def result(self) -> float:
                raise RuntimeError("worker failure")

        class Executor:
            def __init__(self, *, max_workers: int) -> None:
                self.max_workers = max_workers

            def __enter__(self) -> Executor:
                return self

            def __exit__(self, *_: object) -> None:
                return None

            def submit(self, *_: object) -> FailingFuture:
                return FailingFuture()

        monkeypatch.setattr("src.analytics.permutation_test.ProcessPoolExecutor", Executor)
        monkeypatch.setattr(
            "src.analytics.permutation_test.as_completed", lambda futures: list(futures)
        )
        tester = PermutationTester(
            MACrossoverStrategy(fast_period=5, slow_period=20),
            make_candles(20),
            n_permutations=1,
            seed=23,
            max_workers=1,
        )

        with caplog.at_level(logging.WARNING):
            result = tester.run()

        record = next(
            record
            for record in caplog.records
            if getattr(record, "event", None) == "permutation.failed"
        )
        fields = getattr(record, "event_fields", {})
        assert record.levelno == logging.WARNING
        assert fields == {"seed": 23, "exception_type": "RuntimeError"}
        assert result.permuted_metrics == [0.0]

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
