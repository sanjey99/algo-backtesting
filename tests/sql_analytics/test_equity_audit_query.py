"""Integration tests for the ordered equity and drawdown audit query."""
from __future__ import annotations

from datetime import datetime

import pytest
from pandas import isna
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from src.analytics.sql_contracts import EQUITY_DRAWDOWN_AUDIT_CONTRACT
from src.analytics.sql_service import AnalyticsService, RunNotFoundError
from src.db.crud import save_backtest_run

AUDIT_RUN_ID = "audit-run"


def _seed_audit_run(engine: Engine) -> list[int]:
    """Persist an equity path containing a tie, a peak, and an inconsistent drawdown."""
    with Session(engine) as session:
        save_backtest_run(
            session,
            run_id=AUDIT_RUN_ID,
            strategy_name="audit_strategy",
            symbol="DIA",
            start_date=datetime(2024, 3, 1),
            end_date=datetime(2024, 3, 31),
            params={},
            initial_capital=10_000.0,
            commission_pct=0.001,
            slippage_pct=0.0005,
            trades=[],
            equity_curve=[
                {"date": datetime(2024, 3, 1), "equity": 100.0, "drawdown_pct": 0.0},
                {"date": datetime(2024, 3, 2), "equity": 110.0, "drawdown_pct": 0.0},
                {"date": datetime(2024, 3, 3), "equity": 99.0, "drawdown_pct": -0.10},
                {"date": datetime(2024, 3, 3), "equity": 120.0, "drawdown_pct": 0.0},
                {"date": datetime(2024, 3, 4), "equity": 108.0, "drawdown_pct": -0.05},
            ],
            metrics={},
        )
        point_ids = list(
            session.execute(
                text(
                    "SELECT id FROM equity_curve WHERE backtest_id = :run_id ORDER BY id",
                ),
                {"run_id": AUDIT_RUN_ID},
            ).scalars()
        )
    return point_ids


def test_equity_audit_calculates_ordered_drawdown_reconciliation(analytics_db: Engine) -> None:
    """The audit derives each point from preceding equity and a running high-water mark."""
    point_ids = _seed_audit_run(analytics_db)

    frame = AnalyticsService(analytics_db).equity_drawdown_audit(AUDIT_RUN_ID, tolerance=0.0)

    assert tuple(frame.columns) == EQUITY_DRAWDOWN_AUDIT_CONTRACT.names
    assert frame["equity_point_id"].tolist() == point_ids
    assert frame["audit_sequence"].tolist() == [1, 2, 3, 4, 5]
    assert isna(frame.loc[0, "prior_equity"])
    assert frame["prior_equity"].iloc[1:].tolist() == pytest.approx([100.0, 110.0, 99.0, 120.0])
    assert isna(frame.loc[0, "point_return"])
    assert frame["point_return"].iloc[1:].tolist() == pytest.approx(
        [0.1, -0.1, 120.0 / 99.0 - 1.0, -0.1]
    )
    assert frame["running_peak"].tolist() == pytest.approx([100.0, 110.0, 110.0, 120.0, 120.0])
    assert frame["derived_drawdown_pct"].tolist() == pytest.approx([0.0, 0.0, -0.1, 0.0, -0.1])
    assert frame["drawdown_delta_abs"].tolist() == pytest.approx([0.0, 0.0, 0.0, 0.0, 0.05])
    assert frame["is_mismatch"].tolist() == [False, False, False, False, True]


def test_equity_audit_honors_tolerance_and_rejects_negative_values(analytics_db: Engine) -> None:
    """Tolerance is a bound numeric policy, not SQL text interpolation."""
    _seed_audit_run(analytics_db)

    within_tolerance = AnalyticsService(analytics_db).equity_drawdown_audit(
        AUDIT_RUN_ID, tolerance=0.05
    )

    assert within_tolerance["is_mismatch"].tolist() == [False, False, False, False, False]
    with pytest.raises(ValueError, match="tolerance"):
        AnalyticsService(analytics_db).equity_drawdown_audit(AUDIT_RUN_ID, tolerance=-0.01)


def test_equity_audit_rejects_a_non_finite_tolerance(analytics_db: Engine) -> None:
    """A comparison tolerance must be an ordinary non-negative finite number."""
    _seed_audit_run(analytics_db)

    with pytest.raises(ValueError, match="tolerance"):
        AnalyticsService(analytics_db).equity_drawdown_audit(AUDIT_RUN_ID, tolerance=float("nan"))


def test_equity_audit_rejects_an_unknown_run(analytics_db: Engine) -> None:
    """An audit cannot silently turn a missing persisted parent into an empty report."""
    with pytest.raises(RunNotFoundError, match="does not exist"):
        AnalyticsService(analytics_db).equity_drawdown_audit("missing-run", tolerance=0.0)
