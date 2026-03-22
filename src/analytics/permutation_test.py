"""Permutation / randomisation test for strategy significance.

The test shuffles the price series N times and re-runs the strategy on each
synthetic series, then computes the fraction of synthetic runs that beat the
actual strategy metric (the p-value).
"""
from __future__ import annotations

import logging
import random
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Type

from src.analytics.metrics import compute_all_metrics, equity_returns
from src.engine.backtest import BacktestConfig, BacktestEngine, BacktestResult
from src.models.candle import Candle
from src.strategies.base import BaseStrategy

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _shuffle_candles(candles: list[Candle], seed: int) -> list[Candle]:
    """Return a copy of candles with OHLCV values shuffled bar-by-bar.

    Timestamps are preserved in order so the equity curve has valid dates.
    """
    rng = random.Random(seed)
    indices = list(range(len(candles)))
    rng.shuffle(indices)
    shuffled_prices = [candles[i] for i in indices]
    return [
        Candle(
            timestamp=candles[j].timestamp,
            open=shuffled_prices[j].open,
            high=shuffled_prices[j].high,
            low=shuffled_prices[j].low,
            close=shuffled_prices[j].close,
            volume=shuffled_prices[j].volume,
            adj_close=shuffled_prices[j].adj_close,
        )
        for j in range(len(candles))
    ]


def _run_single_permutation_worker(
    strategy_cls: Type[BaseStrategy],
    strategy_params: dict[str, Any],
    candles: list[Candle],
    config: BacktestConfig,
    metric: str,
    seed: int,
) -> float:
    """Top-level function for ProcessPoolExecutor (must be picklable)."""
    synth_candles = _shuffle_candles(candles, seed)
    strategy = strategy_cls(**strategy_params)
    # No try/except — let construction errors propagate to caller
    engine = BacktestEngine()
    result = engine.run(strategy, synth_candles, config)
    metrics = compute_all_metrics(result)
    return metrics.get(metric, 0.0)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class PermutationTestResult:
    actual_metric: float
    permuted_metrics: list[float]
    p_value: float
    n_permutations: int
    metric: str
    failed_count: int = 0

    @property
    def is_significant(self) -> bool:
        """True if p_value < 0.05."""
        return self.p_value < 0.05


# ---------------------------------------------------------------------------
# Main test class
# ---------------------------------------------------------------------------


class PermutationTest:
    """Run a permutation significance test for a strategy on a candle series.

    Parameters
    ----------
    strategy:
        An instantiated strategy (will be reset before each permutation).
    candles:
        Historical candle data.
    config:
        BacktestConfig (default if None).
    n_permutations:
        Number of shuffles (default 200).
    metric:
        Metric name from compute_all_metrics to test (default "sharpe_ratio").
    n_jobs:
        Number of parallel workers (default 1 = single-process).
    random_seed:
        Seed for reproducibility (default 0).
    """

    def __init__(
        self,
        strategy: BaseStrategy,
        candles: list[Candle],
        config: BacktestConfig | None = None,
        n_permutations: int = 200,
        metric: str = "sharpe_ratio",
        n_jobs: int = 1,
        random_seed: int = 0,
    ) -> None:
        self.strategy = strategy
        self.candles = candles
        self.config = config or BacktestConfig()
        self.n_permutations = n_permutations
        self.metric = metric
        self.n_jobs = n_jobs
        self.random_seed = random_seed

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def run(self) -> PermutationTestResult:
        """Execute the permutation test and return results."""
        # 1. Run actual strategy
        self.strategy.reset()
        engine = BacktestEngine()
        actual_result = engine.run(self.strategy, self.candles, self.config)
        self.strategy.reset()
        actual_metrics = compute_all_metrics(actual_result)
        actual_metric = actual_metrics.get(self.metric, 0.0)

        # 2. Run permutations
        seeds = [self.random_seed + i for i in range(self.n_permutations)]
        permuted_metrics: list[float] = []
        failed_count = 0

        cls = type(self.strategy)
        strategy_params = dict(self.strategy.parameters)

        if self.n_jobs > 1:
            try:
                with ProcessPoolExecutor(max_workers=self.n_jobs) as executor:
                    futures = {
                        executor.submit(
                            _run_single_permutation_worker,
                            cls,
                            strategy_params,
                            self.candles,
                            self.config,
                            self.metric,
                            seed,
                        ): seed
                        for seed in seeds
                    }
                    for future in as_completed(futures):
                        try:
                            permuted_metrics.append(future.result())
                        except (TypeError, ValueError, RuntimeError) as e:
                            failed_count += 1
                            logger.warning("Permutation failed: %s", e)
                        except Exception as e:
                            failed_count += 1
                            logger.warning("Unexpected permutation error: %s", e)
            except Exception as e:
                logger.warning(
                    "ProcessPoolExecutor failed, falling back to single-process: %s", e
                )
                # Fall back to single-process
                permuted_metrics = []
                failed_count = 0
                for seed in seeds:
                    try:
                        self.strategy.reset()
                        synth_candles = _shuffle_candles(self.candles, seed)
                        result = engine.run(self.strategy, synth_candles, self.config)
                        self.strategy.reset()
                        m = compute_all_metrics(result)
                        permuted_metrics.append(m.get(self.metric, 0.0))
                    except (ValueError, RuntimeError) as e:
                        failed_count += 1
                        logger.warning("Permutation (single) failed: %s", e)
        else:
            # Single-process path
            for seed in seeds:
                try:
                    self.strategy.reset()
                    synth_candles = _shuffle_candles(self.candles, seed)
                    result = engine.run(self.strategy, synth_candles, self.config)
                    self.strategy.reset()
                    m = compute_all_metrics(result)
                    permuted_metrics.append(m.get(self.metric, 0.0))
                except (ValueError, RuntimeError) as e:
                    failed_count += 1
                    logger.warning("Permutation (single) failed: %s", e)

        # 3. Compute p-value (R-33): standard Monte Carlo — include actual observation
        n = len(permuted_metrics)
        count_gte = sum(1 for m in permuted_metrics if m >= actual_metric)
        p_value = (count_gte + 1) / (n + 1) if n > 0 else 1.0

        return PermutationTestResult(
            actual_metric=actual_metric,
            permuted_metrics=permuted_metrics,
            p_value=p_value,
            n_permutations=self.n_permutations,
            metric=self.metric,
            failed_count=failed_count,
        )
