"""Tests for Walk-Forward Analysis — Step 7."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.analytics.walk_forward import WalkForwardAnalyzer
from src.models.candle import Candle
from src.strategies.ma_crossover import MACrossoverStrategy


def make_trending_candles(n: int = 500, start_price: float = 100.0) -> list[Candle]:
    """Generate n daily candles with a gentle uptrend + noise."""
    import random
    rng = random.Random(42)
    candles = []
    price = start_price
    start = datetime(2015, 1, 2)
    for i in range(n):
        change = 0.001 + rng.gauss(0, 0.015)  # slight upward drift
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


class TestWalkForwardAnalyzer:
    def test_produces_at_least_one_window(self) -> None:
        candles = make_trending_candles(400)
        analyzer = WalkForwardAnalyzer(
            MACrossoverStrategy,
            in_sample_days=200,
            out_of_sample_days=100,
            step_days=100,
            n_optimization_trials=3,
        )
        result = analyzer.run(candles)
        assert len(result.windows) >= 1

    def test_three_windows_on_long_series(self) -> None:
        """Blueprint requires at least 3 windows on SPY 10yr data."""
        candles = make_trending_candles(800)
        analyzer = WalkForwardAnalyzer(
            MACrossoverStrategy,
            in_sample_days=252,
            out_of_sample_days=63,
            step_days=63,
            n_optimization_trials=3,
        )
        result = analyzer.run(candles)
        assert len(result.windows) >= 3

    def test_combined_equity_curve_nonempty(self) -> None:
        candles = make_trending_candles(450)
        analyzer = WalkForwardAnalyzer(
            MACrossoverStrategy,
            in_sample_days=200,
            out_of_sample_days=100,
            step_days=100,
            n_optimization_trials=2,
        )
        result = analyzer.run(candles)
        assert len(result.combined_equity_curve) > 0

    def test_combined_sharpe_is_float(self) -> None:
        candles = make_trending_candles(450)
        analyzer = WalkForwardAnalyzer(
            MACrossoverStrategy,
            in_sample_days=200,
            out_of_sample_days=100,
            step_days=100,
            n_optimization_trials=2,
        )
        result = analyzer.run(candles)
        assert isinstance(result.combined_sharpe, float)

    def test_optimization_stability_computed(self) -> None:
        candles = make_trending_candles(700)
        analyzer = WalkForwardAnalyzer(
            MACrossoverStrategy,
            in_sample_days=200,
            out_of_sample_days=100,
            step_days=100,
            n_optimization_trials=2,
        )
        result = analyzer.run(candles)
        assert isinstance(result.optimization_stability, float)

    def test_window_results_have_best_params(self) -> None:
        candles = make_trending_candles(450)
        analyzer = WalkForwardAnalyzer(
            MACrossoverStrategy,
            in_sample_days=200,
            out_of_sample_days=100,
            step_days=100,
            n_optimization_trials=3,
        )
        result = analyzer.run(candles)
        for window in result.windows:
            assert isinstance(window.best_params, dict)

    def test_combined_metrics_all_keys_present(self) -> None:
        candles = make_trending_candles(450)
        analyzer = WalkForwardAnalyzer(
            MACrossoverStrategy,
            in_sample_days=200,
            out_of_sample_days=100,
            step_days=100,
            n_optimization_trials=2,
        )
        result = analyzer.run(candles)
        for key in ["sharpe_ratio", "cagr", "max_drawdown", "win_rate"]:
            assert key in result.combined_metrics

    def test_insufficient_data_returns_empty(self) -> None:
        """If data too short for even one window, return empty result."""
        candles = make_trending_candles(50)
        analyzer = WalkForwardAnalyzer(
            MACrossoverStrategy,
            in_sample_days=200,
            out_of_sample_days=100,
            step_days=100,
            n_optimization_trials=2,
        )
        result = analyzer.run(candles)
        assert len(result.windows) == 0
        assert result.combined_equity_curve == []
        assert result.combined_metrics["total_return"] == 0.0

    def test_empty_backtest_fallback_uses_utc_timestamps(self) -> None:
        analyzer = WalkForwardAnalyzer(MACrossoverStrategy)

        result = analyzer._run_backtest([], {})

        assert result.start_date.tzinfo is UTC
        assert result.end_date.tzinfo is UTC
