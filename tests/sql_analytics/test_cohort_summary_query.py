"""Integration tests for strategy summaries within explicitly labeled cohorts."""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from src.analytics.sql_contracts import COHORT_SUMMARY_CONTRACT, CohortFilters
from src.analytics.sql_service import AnalyticsService
from src.db.crud import save_backtest_run
from tests.sql_analytics.fixture_data import COMPARISON_END, COMPARISON_START


def _seed_cohort_runs(engine: Engine) -> None:
    """Add same-cohort runs so aggregation and ranks have hand-derived values."""
    with Session(engine) as session:
        _save_run(session, "run-ma-second", "moving_average", 10_300.0, 2.0, -0.04, [15.0, -5.0])
        _save_run(session, "run-breakout-spy", "breakout", 10_150.0, 1.25, -0.08, [8.0])


def _save_run(
    session: Session,
    run_id: str,
    strategy_name: str,
    final_equity: float,
    sharpe_ratio: float,
    max_drawdown: float,
    pnls: list[float],
) -> None:
    save_backtest_run(
        session,
        run_id=run_id,
        strategy_name=strategy_name,
        symbol="SPY",
        start_date=COMPARISON_START,
        end_date=COMPARISON_END,
        params={},
        initial_capital=10_000.0,
        commission_pct=0.001,
        slippage_pct=0.0005,
        trades=[
            {
                "entry_date": datetime(2024, 1, 2),
                "exit_date": datetime(2024, 1, 3),
                "direction": "LONG",
                "entry_price": 100.0,
                "exit_price": 100.0 + pnl,
                "quantity": 1,
                "pnl": pnl,
            }
            for pnl in pnls
        ],
        equity_curve=[
            {"date": COMPARISON_START, "equity": 10_000.0},
            {"date": COMPARISON_END, "equity": final_equity},
        ],
        metrics={"sharpe_ratio": sharpe_ratio, "max_drawdown": max_drawdown},
    )


def test_cohort_summary_aggregates_and_ranks_within_exact_dimensions(
    analytics_db: Engine,
) -> None:
    """Strategies aggregate only comparable runs and rank only inside that labeled cohort."""
    _seed_cohort_runs(analytics_db)

    frame = AnalyticsService(analytics_db).cohort_summary(
        CohortFilters(
            symbol="SPY",
            start_date=COMPARISON_START,
            end_date=COMPARISON_END,
            minimum_run_count=1,
        )
    )

    assert tuple(frame.columns) == COHORT_SUMMARY_CONTRACT.names
    assert frame["strategy_name"].tolist() == ["moving_average", "breakout", "rsi_reversion"]
    assert frame["symbol"].tolist() == ["SPY", "SPY", "SPY"]
    assert frame["start_date"].tolist() == [COMPARISON_START] * 3
    assert frame["end_date"].tolist() == [COMPARISON_END] * 3
    assert frame["initial_capital"].tolist() == [10_000.0] * 3
    assert frame["commission_pct"].tolist() == [0.001] * 3
    assert frame["slippage_pct"].tolist() == [0.0005] * 3
    assert frame["run_count"].tolist() == [2, 1, 1]
    assert frame["average_derived_return"].tolist() == pytest.approx([0.025, 0.015, 0.01])
    assert frame["average_sharpe_ratio"].tolist() == pytest.approx([1.75, 1.25, 1.0])
    assert frame["worst_drawdown"].tolist() == pytest.approx([-0.05, -0.08, -0.05])
    assert frame["aggregate_closed_trade_count"].tolist() == [4, 1, 1]
    assert frame["return_rank"].tolist() == [1, 2, 3]


def test_cohort_summary_filters_by_minimum_count_and_optional_bounds(analytics_db: Engine) -> None:
    """Bound optional filters select runs before aggregation and counts gate each strategy."""
    _seed_cohort_runs(analytics_db)

    frame = AnalyticsService(analytics_db).cohort_summary(
        CohortFilters(symbol=None, start_date=None, end_date=None, minimum_run_count=2)
    )

    assert frame["strategy_name"].tolist() == ["moving_average"]
    assert frame["run_count"].tolist() == [2]
    with pytest.raises(ValueError, match="minimum_run_count"):
        AnalyticsService(analytics_db).cohort_summary(CohortFilters(None, None, None, 0))
    with pytest.raises(ValueError, match="minimum_run_count"):
        AnalyticsService(analytics_db).cohort_summary(CohortFilters(None, None, None, True))
