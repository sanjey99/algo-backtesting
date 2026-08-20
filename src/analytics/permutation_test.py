"""Permutation Testing — statistical significance via shuffled return series.

ADR-004: Non-parametric. Shuffling log-returns preserves the full return
distribution (mean, variance, skew, kurtosis) but destroys all temporal
structure (momentum, mean-reversion). A p-value < 0.05 means the strategy
ranks in the top 5% of a null distribution where no temporal pattern exists.

Parallelized with ProcessPoolExecutor for ~N_CPU speedup.
"""
from __future__ import annotations

import logging
import math
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from os import cpu_count
from typing import Any

import numpy as np

from src.analytics.metrics import (
    MetricName,
    compute_all_metrics,
    parse_metric_name,
    require_metric,
)
from src.engine.backtest import BacktestConfig, BacktestEngine
from src.models.candle import Candle
from src.observability import log_event
from src.strategies.base import BaseStrategy

logger = logging.getLogger(__name__)


class _PermutationWorkerError(RuntimeError):
    """A completed permutation task failed and must not trigger a fallback."""


@dataclass
class PermutationResult:
    actual_metric: float
    permuted_metrics: list[float]
    p_value: float           # fraction of permuted metrics >= actual
    is_significant: bool     # p_value < 0.05
    percentile: float        # actual's percentile rank among permuted + actual


def _permute_returns(log_returns: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """Shuffle log-returns (destroys temporal structure, preserves distribution)."""
    shuffled = log_returns.copy()
    rng.shuffle(shuffled)
    return shuffled


def _reconstruct_prices(log_returns: np.ndarray, initial_price: float = 100.0) -> np.ndarray:
    """Reconstruct a price series from log-returns starting at initial_price."""
    prices = np.empty(len(log_returns) + 1)
    prices[0] = initial_price
    for i, lr in enumerate(log_returns):
        prices[i + 1] = prices[i] * math.exp(lr)
    return prices


def _candles_from_prices(prices: np.ndarray, original_candles: list[Candle]) -> list[Candle]:
    """Build synthetic Candle list from a price series (preserves timestamps)."""
    from src.models.candle import Candle

    result = []
    for i, candle in enumerate(original_candles):
        if i >= len(prices):
            break
        p = float(prices[i])
        # Synthetic OHLC: all equal to close (no intrabar structure in permuted data)
        result.append(
            Candle(
                timestamp=candle.timestamp,
                open=p,
                high=p,
                low=p,
                close=p,
                volume=candle.volume,
                adj_close=p,
            )
        )
    return result


def _run_single_permutation(
    strategy_cls_name: str,
    strategy_params: dict[str, Any],
    log_returns: list[float],
    original_prices: list[float],
    timestamps: list[Any],
    metric: MetricName | str,
    config_dict: dict[str, float],
    seed: int,
    symbol: str | None = None,
) -> float:
    """Run one permutation — designed to run in a subprocess (no shared state)."""
    metric_name = parse_metric_name(metric)
    import importlib
    import math

    import numpy as np

    # Re-import in subprocess context
    from src.analytics.metrics import compute_all_metrics
    from src.engine.backtest import BacktestConfig, BacktestEngine
    from src.models.candle import Candle

    rng = np.random.default_rng(seed)
    lr_arr = np.array(log_returns)
    rng.shuffle(lr_arr)

    # Reconstruct prices
    prices = np.empty(len(lr_arr) + 1)
    prices[0] = original_prices[0]
    for i, lr in enumerate(lr_arr):
        prices[i + 1] = prices[i] * math.exp(lr)

    # Build synthetic candles
    from datetime import datetime
    candles = []
    for i, ts in enumerate(timestamps):
        if i >= len(prices):
            break
        p = float(prices[i])
        candles.append(Candle(
            timestamp=ts if isinstance(ts, datetime) else datetime(2020, 1, 1),
            open=p, high=p, low=p, close=p, volume=1_000_000.0, adj_close=p,
        ))

    # Dynamically load strategy class
    parts = strategy_cls_name.rsplit(".", 1)
    mod = importlib.import_module(parts[0])
    cls = getattr(mod, parts[1])

    strategy = cls(**strategy_params)

    config = BacktestConfig(
        initial_capital=config_dict.get("initial_capital", 100_000.0),
        commission_pct=config_dict.get("commission_pct", 0.001),
        slippage_pct=config_dict.get("slippage_pct", 0.0005),
        short_initial_margin=config_dict.get("short_initial_margin", 1.50),
        short_maintenance_margin=config_dict.get("short_maintenance_margin", 0.30),
        annual_short_borrow_rate=config_dict.get("annual_short_borrow_rate", 0.03),
        borrow_day_count=config_dict.get("borrow_day_count", 365.0),
    )
    engine = BacktestEngine()
    result = engine.run(strategy, candles, config, symbol=symbol)
    metrics = compute_all_metrics(result)
    return require_metric(metrics, metric_name)


class PermutationTester:
    """Statistical significance testing via return shuffling.

    Usage:
        tester = PermutationTester(strategy, candles, n_permutations=1000)
        result = tester.run()
        print(f"p={result.p_value:.3f}, significant={result.is_significant}")
    """

    def __init__(
        self,
        strategy: BaseStrategy,
        candles: list[Candle],
        n_permutations: int = 1000,
        metric: MetricName | str = MetricName.SHARPE_RATIO,
        seed: int = 42,
        config: BacktestConfig | None = None,
        max_workers: int | None = None,
        symbol: str | None = None,
    ) -> None:
        self.strategy = strategy
        self.candles = candles
        self.n_permutations = n_permutations
        self.metric = parse_metric_name(metric)
        self.seed = seed
        self.config = config or BacktestConfig()
        self.max_workers = max_workers or max(1, (cpu_count() or 2) - 1)
        self.symbol = symbol

    def _compute_log_returns(self) -> np.ndarray:
        prices = np.array([c.adj_close for c in self.candles], dtype=float)
        return np.log(prices[1:] / prices[:-1])  # type: ignore[no-any-return]

    def run(self) -> PermutationResult:
        """Execute permutation test; return PermutationResult."""
        # 1. Actual metric on real data
        engine = BacktestEngine()
        actual_result = engine.run(self.strategy, self.candles, self.config, symbol=self.symbol)
        self.strategy.reset()
        actual_metrics = compute_all_metrics(actual_result)
        actual_metric = require_metric(actual_metrics, self.metric)

        # 2. Build log-returns and original prices for subprocess
        log_returns = self._compute_log_returns().tolist()
        original_prices = [c.adj_close for c in self.candles]
        timestamps = [c.timestamp for c in self.candles]

        # Strategy class path for subprocess import
        cls = type(self.strategy)
        strategy_cls_name = f"{cls.__module__}.{cls.__name__}"
        config_dict = {
            "initial_capital": self.config.initial_capital,
            "commission_pct": self.config.commission_pct,
            "slippage_pct": self.config.slippage_pct,
            "short_initial_margin": self.config.short_initial_margin,
            "short_maintenance_margin": self.config.short_maintenance_margin,
            "annual_short_borrow_rate": self.config.annual_short_borrow_rate,
            "borrow_day_count": self.config.borrow_day_count,
        }

        # 3. Parallel permutations
        seeds = [self.seed + i for i in range(self.n_permutations)]

        try:
            with ProcessPoolExecutor(max_workers=self.max_workers) as executor:
                futures = {
                    executor.submit(
                        _run_single_permutation,
                        strategy_cls_name,
                        self.strategy.parameters,
                        log_returns,
                        original_prices,
                        timestamps,
                        self.metric,
                        config_dict,
                        s,
                        self.symbol,
                    ): s
                    for s in seeds
                }
                permuted_metrics: list[float] = []
                for future in as_completed(futures):
                    try:
                        permuted_metrics.append(future.result())
                    except Exception as error:
                        log_event(
                            logger,
                            logging.WARNING,
                            "permutation.failed",
                            seed=futures[future],
                            exception_type=type(error).__name__,
                        )
                        raise _PermutationWorkerError(str(error)) from error
        except _PermutationWorkerError:
            raise
        except Exception:
            # Process-pool infrastructure can fail after some completions.  Restart
            # every seed sequentially so partial samples cannot be duplicated.
            permuted_metrics = [
                _run_single_permutation(
                    strategy_cls_name,
                    self.strategy.parameters,
                    log_returns,
                    original_prices,
                    timestamps,
                    self.metric,
                    config_dict,
                    s,
                    self.symbol,
                )
                for s in seeds
            ]

        # 4. Compute p-value
        n = len(permuted_metrics)
        count_gte = sum(1 for m in permuted_metrics if m >= actual_metric)
        p_value = (count_gte + 1) / (n + 1)

        all_metrics = sorted(permuted_metrics + [actual_metric])
        rank = all_metrics.index(actual_metric)
        percentile = (rank / len(all_metrics)) * 100

        return PermutationResult(
            actual_metric=actual_metric,
            permuted_metrics=permuted_metrics,
            p_value=p_value,
            is_significant=p_value < 0.05,
            percentile=percentile,
        )
