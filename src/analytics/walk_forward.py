"""Walk-Forward Analysis (WFA) — out-of-sample validation framework."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Type

import numpy as np

from src.analytics.metrics import compute_all_metrics, sharpe_ratio, equity_returns
from src.engine.backtest import BacktestConfig, BacktestEngine, BacktestResult
from src.models.candle import Candle
from src.strategies.base import BaseStrategy

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class WindowResult:
    window_index: int
    in_sample_start: datetime
    in_sample_end: datetime
    oos_start: datetime
    oos_end: datetime
    best_params: dict[str, Any]
    oos_sharpe: float
    oos_result: BacktestResult


# ---------------------------------------------------------------------------
# Analyser
# ---------------------------------------------------------------------------


class WalkForwardAnalyzer:
    """Performs walk-forward analysis by rolling in-sample/out-of-sample windows.

    Parameters
    ----------
    strategy_cls:
        Strategy class (subclass of BaseStrategy).
    config:
        BacktestConfig shared across all runs.
    in_sample_pct:
        Fraction of each window used as in-sample data (default 0.7).
    n_windows:
        Number of rolling windows (default 5).
    n_trials:
        Random-search trials per in-sample optimisation (default 50).
    metric:
        Metric to optimise in-sample (default "sharpe_ratio").
    random_seed:
        Seed for reproducibility (default 42).
    """

    def __init__(
        self,
        strategy_cls: Type[BaseStrategy],
        config: BacktestConfig | None = None,
        in_sample_pct: float = 0.7,
        n_windows: int = 5,
        n_trials: int = 50,
        metric: str = "sharpe_ratio",
        random_seed: int = 42,
    ) -> None:
        self.strategy_cls = strategy_cls
        self.config = config or BacktestConfig()
        self.in_sample_pct = in_sample_pct
        self.n_windows = n_windows
        self.n_trials = n_trials
        self.metric = metric
        self._rng = np.random.RandomState(random_seed)

        # R-32: Cache parameter_space once (avoids creating N_trials * N_windows dummy instances)
        _dummy = self.strategy_cls()
        self._parameter_space: dict[str, tuple[float, float, float]] = dict(
            _dummy.parameter_space
        )

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def run(self, candles: list[Candle]) -> list[WindowResult]:
        """Execute WFA over the full candle series."""
        if len(candles) < 10:
            raise ValueError("Need at least 10 candles for walk-forward analysis")

        n = len(candles)
        window_size = n // self.n_windows
        if window_size < 4:
            raise ValueError(
                f"Window size {window_size} too small (need ≥ 4). "
                f"Reduce n_windows or provide more candles."
            )

        windows: list[WindowResult] = []

        for window_idx in range(self.n_windows):
            start = window_idx * window_size
            end = start + window_size if window_idx < self.n_windows - 1 else n

            is_size = int((end - start) * self.in_sample_pct)
            is_end = start + is_size
            oos_end = end

            if is_end >= oos_end:
                logger.warning("Window %d: no OOS data, skipping", window_idx)
                continue

            is_candles = candles[start:is_end]
            oos_candles = candles[is_end:oos_end]

            # In-sample optimisation: random search
            best_params: dict[str, Any] = {}
            best_score = float("-inf")

            for _ in range(self.n_trials):
                params = self._sample_params()
                result = self._run_backtest(is_candles, params)
                returns = equity_returns(result.equity_curve)
                score = sharpe_ratio(returns)
                if score > best_score:
                    best_score = score
                    best_params = params

            # OOS evaluation
            oos_result = self._run_backtest(oos_candles, best_params)
            oos_returns = equity_returns(oos_result.equity_curve)
            oos_sharpe = sharpe_ratio(oos_returns)

            windows.append(
                WindowResult(
                    window_index=window_idx,
                    in_sample_start=candles[start].timestamp,
                    in_sample_end=candles[is_end - 1].timestamp,
                    oos_start=candles[is_end].timestamp,
                    oos_end=candles[oos_end - 1].timestamp,
                    best_params=best_params,
                    oos_sharpe=oos_sharpe,
                    oos_result=oos_result,
                )
            )

        return windows

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _sample_params(self) -> dict[str, Any]:
        """Random sample from strategy parameter_space."""
        params: dict[str, Any] = {}
        for name, (low, high, step) in self._parameter_space.items():
            n_steps = int((high - low) / step) + 1
            chosen = low + self._rng.randint(0, n_steps - 1) * step
            params[name] = int(chosen) if step == int(step) else chosen
        return params

    def _run_backtest(self, candles: list[Candle], params: dict[str, Any]) -> BacktestResult:
        """Run backtest with given params; return empty result on ValueError."""
        try:
            strategy = self.strategy_cls(**params)
            engine = BacktestEngine()
            return engine.run(strategy, candles, self.config)
        except ValueError as e:
            # Invalid param combo (e.g., fast >= slow) — return empty result
            logger.debug("WFA trial skipped (invalid params %s): %s", params, e)
            return BacktestResult(
                strategy_name=getattr(self.strategy_cls, "name", "unknown"),
                symbol="",
                start_date=candles[0].timestamp if candles else datetime.now(),
                end_date=candles[-1].timestamp if candles else datetime.now(),
                parameters=params,
                trades=[],
                equity_curve=[],
                final_equity=self.config.initial_capital,
                initial_capital=self.config.initial_capital,
            )
