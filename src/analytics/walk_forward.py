"""Walk-Forward Analysis — rolling in-sample/out-of-sample optimization.

ADR-002: Random search over 50 trials is statistically equivalent to grid
search (Bergstra & Bengio 2012) but 10-100x faster for multi-parameter spaces.

The combined OOS equity curve chains only out-of-sample segments — giving a
realistic live performance estimate that cannot be cherry-picked.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from src.analytics.metrics import compute_all_metrics, equity_curve_to_returns, sharpe_ratio
from src.engine.backtest import BacktestConfig, BacktestEngine, BacktestResult
from src.models.candle import Candle
from src.models.portfolio import EquityPoint
from src.strategies.base import BaseStrategy


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


@dataclass
class WalkForwardResult:
    windows: list[WindowResult]
    combined_equity_curve: list[EquityPoint]
    optimization_stability: float  # std / mean of OOS Sharpe across windows
    combined_sharpe: float
    combined_metrics: dict[str, float]


class WalkForwardAnalyzer:
    """Rolling train/test walk-forward optimizer.

    For each window:
      1. Optimize strategy parameters on in-sample data (random search)
      2. Run with best params on OOS data
      3. Append OOS equity curve to combined curve

    combined_equity_curve = chained OOS equity segments (no look-ahead)
    """

    def __init__(
        self,
        strategy_cls: type[BaseStrategy],
        in_sample_days: int = 252,
        out_of_sample_days: int = 63,
        step_days: int = 63,
        optimization_metric: str = "sharpe_ratio",
        n_optimization_trials: int = 50,
        seed: int = 42,
        config: BacktestConfig | None = None,
    ) -> None:
        self.strategy_cls = strategy_cls
        self.in_sample_days = in_sample_days
        self.out_of_sample_days = out_of_sample_days
        self.step_days = step_days
        self.optimization_metric = optimization_metric
        self.n_optimization_trials = n_optimization_trials
        self.seed = seed
        self.config = config or BacktestConfig()
        self._rng = random.Random(seed)

    def _sample_params(self) -> dict[str, Any]:
        """Random sample from strategy parameter_space."""
        dummy = self.strategy_cls()
        params: dict[str, Any] = {}
        for name, (low, high, step) in dummy.parameter_space.items():
            n_steps = int((high - low) / step) + 1
            chosen = low + self._rng.randint(0, n_steps - 1) * step
            params[name] = int(chosen) if step == int(step) else chosen
        return params

    def _run_backtest(self, candles: list[Candle], params: dict[str, Any]) -> BacktestResult:
        try:
            strategy = self.strategy_cls(**params)
            engine = BacktestEngine()
            return engine.run(strategy, candles, self.config)
        except Exception:
            # Invalid param combo (e.g., fast >= slow) — return empty result
            from datetime import datetime

            from src.engine.backtest import BacktestResult
            return BacktestResult(
                strategy_name=(
                    self.strategy_cls.name if hasattr(self.strategy_cls, "name") else "unknown"
                ),
                symbol="",
                start_date=candles[0].timestamp if candles else datetime.utcnow(),
                end_date=candles[-1].timestamp if candles else datetime.utcnow(),
                parameters=params,
                trades=[],
                equity_curve=[],
                final_equity=self.config.initial_capital,
                initial_capital=self.config.initial_capital,
            )

    def _optimize_window(self, train_candles: list[Candle]) -> dict[str, Any]:
        """Random search over parameter_space; return best params."""
        best_params: dict[str, Any] = {}
        best_metric = float("-inf")

        for _ in range(self.n_optimization_trials):
            params = self._sample_params()
            result = self._run_backtest(train_candles, params)
            if not result.equity_curve:
                continue
            metrics = compute_all_metrics(result)
            metric_value = metrics.get(self.optimization_metric, float("-inf"))
            if metric_value > best_metric:
                best_metric = metric_value
                best_params = params

        return best_params if best_params else self._sample_params()

    def run(self, candles: list[Candle]) -> WalkForwardResult:
        """Execute walk-forward analysis over the candle series."""
        windows: list[WindowResult] = []
        combined_equity: list[EquityPoint] = []
        window_idx = 0
        oos_sharpes: list[float] = []

        start = 0
        while True:
            is_end = start + self.in_sample_days
            oos_end = is_end + self.out_of_sample_days

            if oos_end > len(candles):
                break

            train_candles = candles[start:is_end]
            oos_candles = candles[is_end:oos_end]

            best_params = self._optimize_window(train_candles)
            oos_result = self._run_backtest(oos_candles, best_params)

            oos_returns = equity_curve_to_returns(oos_result.equity_curve)
            oos_sharpe = sharpe_ratio(oos_returns)
            oos_sharpes.append(oos_sharpe)

            # Rescale OOS equity to chain from last combined point
            if combined_equity and oos_result.equity_curve:
                last_equity = combined_equity[-1].equity
                first_oos = oos_result.equity_curve[0].equity
                scale = last_equity / first_oos if first_oos > 0 else 1.0
                for pt in oos_result.equity_curve:
                    combined_equity.append(
                        EquityPoint(
                            date=pt.date,
                            equity=pt.equity * scale,
                            drawdown_pct=pt.drawdown_pct,
                        )
                    )
            else:
                combined_equity.extend(oos_result.equity_curve)

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

            window_idx += 1
            start += self.step_days

        # Compute combined metrics
        if oos_sharpes and len(oos_sharpes) > 1:
            import numpy as np
            arr = np.array(oos_sharpes)
            mean = float(np.mean(arr))
            std = float(np.std(arr))
            opt_stability = std / abs(mean) if mean != 0 else float("inf")
        else:
            opt_stability = 0.0

        combined_returns = equity_curve_to_returns(combined_equity)
        combined_sharpe = sharpe_ratio(combined_returns)

        # Build a minimal BacktestResult for combined metrics
        from datetime import datetime

        from src.engine.backtest import BacktestResult
        all_trades = [t for w in windows for t in w.oos_result.trades]
        combined_result = BacktestResult(
            strategy_name=getattr(self.strategy_cls, 'name', 'unknown'),
            symbol="combined",
            start_date=combined_equity[0].date if combined_equity else datetime.utcnow(),
            end_date=combined_equity[-1].date if combined_equity else datetime.utcnow(),
            parameters={},
            trades=all_trades,
            equity_curve=combined_equity,
            final_equity=(
                combined_equity[-1].equity if combined_equity else self.config.initial_capital
            ),
            initial_capital=self.config.initial_capital,
        )
        combined_metrics = compute_all_metrics(combined_result)

        return WalkForwardResult(
            windows=windows,
            combined_equity_curve=combined_equity,
            optimization_stability=opt_stability,
            combined_sharpe=combined_sharpe,
            combined_metrics=combined_metrics,
        )
