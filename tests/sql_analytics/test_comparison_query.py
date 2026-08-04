"""Integration tests for the persisted strategy-run comparison query."""
from __future__ import annotations

from datetime import timedelta

import pytest
from pandas import isna
from pandas.testing import assert_frame_equal
from sqlalchemy import delete, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from src.analytics.sql_contracts import COMPARISON_CONTRACT, ComparisonFilters
from src.analytics.sql_service import AnalyticsService, ContractValidationError, validate_frame
from src.db.tables import BacktestRun, EquityCurvePoint, MetricRecord
from tests.sql_analytics.fixture_data import COMPARISON_END, COMPARISON_START, RUN_RSI_ID

COMPARISON_FILTERS = ComparisonFilters(
    symbol="SPY",
    start_date=COMPARISON_START,
    end_date=COMPARISON_END,
    strategy_name=None,
)


def test_comparison_returns_one_row_per_run_with_correct_ranks(analytics_db: Engine) -> None:
    """A comparison has hand-derived values and cohort ranks at the run grain."""
    frame = AnalyticsService(analytics_db).compare_runs(COMPARISON_FILTERS)

    assert tuple(frame.columns) == COMPARISON_CONTRACT.names
    assert frame["run_id"].tolist() == ["run-ma", "run-rsi"]
    assert frame["return_rank"].tolist() == [1, 2]
    assert frame.loc[0, "cumulative_trade_pnl"] == pytest.approx(150.0)
    assert frame.loc[0, "derived_total_return"] == pytest.approx(0.02)
    assert frame.loc[0, "total_return_delta"] == pytest.approx(0.0)


def test_comparison_preserves_microsecond_precision_in_date_filters(analytics_db: Engine) -> None:
    """A run differing only by a microsecond must not join the selected comparison cohort."""
    with Session(analytics_db) as session:
        session.add(
            BacktestRun(
                id="run-microsecond",
                strategy_name="moving_average",
                symbol="SPY",
                start_date=COMPARISON_START + timedelta(microseconds=1),
                end_date=COMPARISON_END,
                params_json="{}",
                initial_capital=10_000.0,
                commission_pct=0.001,
                slippage_pct=0.0005,
            )
        )
        session.commit()

    frame = AnalyticsService(analytics_db).compare_runs(COMPARISON_FILTERS)

    assert frame["run_id"].tolist() == ["run-ma", "run-rsi"]


def test_comparison_preaggregates_children_without_trade_fan_out(analytics_db: Engine) -> None:
    """Extra child rows cannot multiply a run's independently aggregated trade facts."""
    before = AnalyticsService(analytics_db).compare_runs(COMPARISON_FILTERS)
    with Session(analytics_db) as session:
        session.add(MetricRecord(backtest_id="run-ma", metric_name="diagnostic", metric_value=99.0))
        session.add(
            EquityCurvePoint(
                backtest_id="run-ma",
                date=COMPARISON_START,
                equity=9_900.0,
                drawdown_pct=-0.01,
            )
        )
        session.commit()

    after = AnalyticsService(analytics_db).compare_runs(COMPARISON_FILTERS)

    assert after.loc[0, "cumulative_trade_pnl"] == before.loc[0, "cumulative_trade_pnl"] == 150.0
    assert after.loc[0, "closed_trade_count"] == before.loc[0, "closed_trade_count"] == 2


def test_comparison_assigns_equal_rank_to_tied_returns(analytics_db: Engine) -> None:
    """Tied returns share rank because run id is presentation-only, not a window tiebreaker."""
    with Session(analytics_db) as session:
        session.execute(
            update(EquityCurvePoint)
            .where(EquityCurvePoint.backtest_id == RUN_RSI_ID)
            .where(EquityCurvePoint.date == COMPARISON_END)
            .values(equity=10_200.0)
        )
        session.execute(
            update(MetricRecord)
            .where(MetricRecord.backtest_id == RUN_RSI_ID)
            .where(MetricRecord.metric_name == "total_return")
            .values(metric_value=0.02)
        )
        session.commit()

    frame = AnalyticsService(analytics_db).compare_runs(COMPARISON_FILTERS)

    assert frame["return_rank"].tolist() == [1, 1]


def test_comparison_keeps_missing_metric_rank_nullable(analytics_db: Engine) -> None:
    """A missing Sharpe metric produces a nullable rank instead of a fabricated rank."""
    with Session(analytics_db) as session:
        session.execute(delete(MetricRecord).where(MetricRecord.backtest_id == RUN_RSI_ID))
        session.commit()

    frame = AnalyticsService(analytics_db).compare_runs(COMPARISON_FILTERS)

    assert str(frame["return_rank"].dtype) == "Int64"
    assert str(frame["sharpe_rank"].dtype) == "Int64"
    assert isna(frame.loc[1, "sharpe_rank"])


def test_comparison_returns_empty_frame_with_exact_contract_dtypes(analytics_db: Engine) -> None:
    """An unmatched selection remains an empty, contract-shaped nullable frame."""
    frame = AnalyticsService(analytics_db).compare_runs(
        ComparisonFilters("MISSING", COMPARISON_START, COMPARISON_END)
    )

    assert tuple(frame.columns) == COMPARISON_CONTRACT.names
    assert {name: str(dtype) for name, dtype in frame.dtypes.items()} == {
        "run_id": "string",
        "strategy_name": "string",
        "symbol": "string",
        "start_date": "datetime64[ns]",
        "end_date": "datetime64[ns]",
        "initial_capital": "Float64",
        "commission_pct": "Float64",
        "slippage_pct": "Float64",
        "sharpe_ratio": "Float64",
        "sortino_ratio": "Float64",
        "cagr": "Float64",
        "max_drawdown": "Float64",
        "max_drawdown_duration": "Float64",
        "win_rate": "Float64",
        "profit_factor": "Float64",
        "calmar_ratio": "Float64",
        "metric_total_trades": "Float64",
        "reported_total_return": "Float64",
        "closed_trade_count": "Int64",
        "cumulative_trade_pnl": "Float64",
        "closed_trade_commission": "Float64",
        "latest_equity": "Float64",
        "derived_total_return": "Float64",
        "total_return_delta": "Float64",
        "return_rank": "Int64",
        "sharpe_rank": "Int64",
    }


def test_comparison_optional_strategy_filter_excludes_other_strategies(
    analytics_db: Engine,
) -> None:
    """The optional bound strategy filter limits selected runs without changing SQL text."""
    frame = AnalyticsService(analytics_db).compare_runs(
        ComparisonFilters("SPY", COMPARISON_START, COMPARISON_END, "moving_average")
    )

    assert frame["run_id"].tolist() == ["run-ma"]


def test_validate_frame_returns_normalized_copy_without_mutating_input(
    analytics_db: Engine,
) -> None:
    """Result validation protects the repository-owned raw frame from mutation."""
    frame = AnalyticsService(analytics_db).compare_runs(COMPARISON_FILTERS)
    original = frame.copy(deep=True)

    normalized = validate_frame(frame, COMPARISON_CONTRACT)

    assert normalized is not frame
    assert_frame_equal(frame, original)


@pytest.mark.parametrize(
    ("column", "invalid_value"),
    (("initial_capital", 0.0), ("closed_trade_count", -1), ("closed_trade_commission", -0.01)),
)
def test_validate_frame_rejects_positive_and_nonnegative_constraint_violations(
    analytics_db: Engine, column: str, invalid_value: float
) -> None:
    """Capital, counts, and fees cannot violate their reviewed column constraints."""
    frame = AnalyticsService(analytics_db).compare_runs(COMPARISON_FILTERS)
    invalid = frame.copy()
    invalid.loc[0, column] = invalid_value

    with pytest.raises(ContractValidationError):
        validate_frame(invalid, COMPARISON_CONTRACT)
