"""Harden analytics integrity.

Revision ID: 20260804_01
Revises: 455406e2c7ac
Create Date: 2026-08-04 14:05:24.526196
"""
from collections.abc import Sequence

from alembic import op

revision: str = "20260804_01"
down_revision: str | Sequence[str] | None = "455406e2c7ac"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_PREFLIGHT_QUERIES = (
    (
        "DUPLICATE_METRIC_KEYS",
        """
        SELECT COUNT(*)
        FROM (
            SELECT backtest_id, metric_name
            FROM metrics
            GROUP BY backtest_id, metric_name
            HAVING COUNT(*) > 1
        ) AS duplicate_metric_keys
        """,
    ),
    (
        "DUPLICATE_EQUITY_TIMESTAMPS",
        """
        SELECT COUNT(*)
        FROM (
            SELECT backtest_id, date
            FROM equity_curve
            GROUP BY backtest_id, date
            HAVING COUNT(*) > 1
        ) AS duplicate_equity_timestamps
        """,
    ),
    (
        "ORPHAN_TRADES",
        """
        SELECT COUNT(*)
        FROM trades AS child
        LEFT JOIN backtest_runs AS parent ON parent.id = child.backtest_id
        WHERE parent.id IS NULL
        """,
    ),
    (
        "ORPHAN_EQUITY_POINTS",
        """
        SELECT COUNT(*)
        FROM equity_curve AS child
        LEFT JOIN backtest_runs AS parent ON parent.id = child.backtest_id
        WHERE parent.id IS NULL
        """,
    ),
    (
        "ORPHAN_METRICS",
        """
        SELECT COUNT(*)
        FROM metrics AS child
        LEFT JOIN backtest_runs AS parent ON parent.id = child.backtest_id
        WHERE parent.id IS NULL
        """,
    ),
)


def _run_preflight() -> None:
    connection = op.get_bind()
    failures = tuple(
        (name, int(connection.exec_driver_sql(statement).scalar_one()))
        for name, statement in _PREFLIGHT_QUERIES
    )
    nonzero = tuple((name, count) for name, count in failures if count)
    if nonzero:
        raise RuntimeError(", ".join(f"{name}={count}" for name, count in nonzero))


def upgrade() -> None:
    """Reject unsafe data, then add natural keys and analytical indexes."""
    _run_preflight()
    with op.batch_alter_table("metrics") as batch_op:
        batch_op.create_unique_constraint(
            "uq_metrics_backtest_metric", ["backtest_id", "metric_name"]
        )
    with op.batch_alter_table("equity_curve") as batch_op:
        batch_op.create_unique_constraint(
            "uq_equity_curve_backtest_date", ["backtest_id", "date"]
        )
    op.create_index(
        "ix_trades_backtest_exit_id",
        "trades",
        ["backtest_id", "exit_date", "id"],
        unique=False,
    )
    op.create_index(
        "ix_backtest_runs_symbol_dates",
        "backtest_runs",
        ["symbol", "start_date", "end_date"],
        unique=False,
    )


def downgrade() -> None:
    """Remove analytical indexes and natural keys in reverse order."""
    op.drop_index("ix_backtest_runs_symbol_dates", table_name="backtest_runs")
    op.drop_index("ix_trades_backtest_exit_id", table_name="trades")
    with op.batch_alter_table("equity_curve") as batch_op:
        batch_op.drop_constraint("uq_equity_curve_backtest_date", type_="unique")
    with op.batch_alter_table("metrics") as batch_op:
        batch_op.drop_constraint("uq_metrics_backtest_metric", type_="unique")
