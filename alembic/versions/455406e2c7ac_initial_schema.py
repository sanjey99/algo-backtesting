"""Create the exact pre-hardening analytics schema.

Revision ID: 455406e2c7ac
Revises:
Create Date: 2026-03-23 04:48:22.190009
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "455406e2c7ac"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the legacy baseline without hardening-only constraints or indexes."""
    op.create_table(
        "backtest_runs",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("strategy_name", sa.String(), nullable=False),
        sa.Column("symbol", sa.String(), nullable=False),
        sa.Column("start_date", sa.DateTime(), nullable=False),
        sa.Column("end_date", sa.DateTime(), nullable=False),
        sa.Column("params_json", sa.Text(), nullable=False),
        sa.Column("initial_capital", sa.Float(), nullable=False),
        sa.Column("commission_pct", sa.Float(), nullable=False),
        sa.Column("slippage_pct", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "trades",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("backtest_id", sa.String(), nullable=False),
        sa.Column("entry_date", sa.DateTime(), nullable=False),
        sa.Column("exit_date", sa.DateTime(), nullable=True),
        sa.Column("direction", sa.String(), nullable=False),
        sa.Column("entry_price", sa.Float(), nullable=False),
        sa.Column("exit_price", sa.Float(), nullable=True),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("pnl", sa.Float(), nullable=True),
        sa.Column("pnl_pct", sa.Float(), nullable=True),
        sa.Column("commission", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["backtest_id"], ["backtest_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "equity_curve",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("backtest_id", sa.String(), nullable=False),
        sa.Column("date", sa.DateTime(), nullable=False),
        sa.Column("equity", sa.Float(), nullable=False),
        sa.Column("drawdown_pct", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["backtest_id"], ["backtest_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "metrics",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("backtest_id", sa.String(), nullable=False),
        sa.Column("metric_name", sa.String(), nullable=False),
        sa.Column("metric_value", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["backtest_id"], ["backtest_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    """Drop the legacy baseline in dependency order."""
    op.drop_table("metrics")
    op.drop_table("equity_curve")
    op.drop_table("trades")
    op.drop_table("backtest_runs")
