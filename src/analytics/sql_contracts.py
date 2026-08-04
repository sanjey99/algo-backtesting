"""Types describing the closed SQL analytics query interface."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TypeAlias

Scalar: TypeAlias = str | int | float | datetime | None


class QueryId(StrEnum):
    """Identifiers for reviewed packaged SQL statements."""

    STRATEGY_RUN_COMPARISON = "strategy_run_comparison"
    TRADE_SEQUENCE = "trade_sequence"
    EQUITY_DRAWDOWN_AUDIT = "equity_drawdown_audit"
    STRATEGY_COHORT_SUMMARY = "strategy_cohort_summary"


class ColumnKind(StrEnum):
    """Supported result column value kinds."""

    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    DATETIME = "datetime"
    BOOLEAN = "boolean"


@dataclass(frozen=True)
class ColumnSpec:
    """Validation metadata for one result column."""

    name: str
    kind: ColumnKind
    nullable: bool
    minimum: float | None = None
    exclusive_minimum: float | None = None
    maximum: float | None = None


@dataclass(frozen=True)
class ResultContract:
    """Validation metadata for a query result set."""

    columns: tuple[ColumnSpec, ...]
    unique_by: tuple[str, ...] = ()

    @property
    def names(self) -> tuple[str, ...]:
        """Return the result column names in their required order."""
        return tuple(column.name for column in self.columns)


@dataclass(frozen=True)
class QuerySpec:
    """Resource and bind contract for a reviewed SQL statement."""

    resource: str
    required_params: frozenset[str]
    contract: ResultContract
    expanding_params: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ComparisonFilters:
    """Exact dimensions used to select strategy runs for comparison."""

    symbol: str
    start_date: datetime
    end_date: datetime
    strategy_name: str | None = None


COMPARISON_CONTRACT = ResultContract(
    columns=(
        ColumnSpec("run_id", ColumnKind.STRING, False),
        ColumnSpec("strategy_name", ColumnKind.STRING, False),
        ColumnSpec("symbol", ColumnKind.STRING, False),
        ColumnSpec("start_date", ColumnKind.DATETIME, False),
        ColumnSpec("end_date", ColumnKind.DATETIME, False),
        ColumnSpec("initial_capital", ColumnKind.FLOAT, False, exclusive_minimum=0.0),
        ColumnSpec("commission_pct", ColumnKind.FLOAT, False, minimum=0.0),
        ColumnSpec("slippage_pct", ColumnKind.FLOAT, False, minimum=0.0),
        ColumnSpec("sharpe_ratio", ColumnKind.FLOAT, True),
        ColumnSpec("sortino_ratio", ColumnKind.FLOAT, True),
        ColumnSpec("cagr", ColumnKind.FLOAT, True),
        ColumnSpec("max_drawdown", ColumnKind.FLOAT, True),
        ColumnSpec("max_drawdown_duration", ColumnKind.FLOAT, True),
        ColumnSpec("win_rate", ColumnKind.FLOAT, True),
        ColumnSpec("profit_factor", ColumnKind.FLOAT, True),
        ColumnSpec("calmar_ratio", ColumnKind.FLOAT, True),
        ColumnSpec("metric_total_trades", ColumnKind.FLOAT, True, minimum=0.0),
        ColumnSpec("reported_total_return", ColumnKind.FLOAT, True),
        ColumnSpec("closed_trade_count", ColumnKind.INTEGER, False, minimum=0.0),
        ColumnSpec("cumulative_trade_pnl", ColumnKind.FLOAT, False),
        ColumnSpec("closed_trade_commission", ColumnKind.FLOAT, False, minimum=0.0),
        ColumnSpec("latest_equity", ColumnKind.FLOAT, True),
        ColumnSpec("derived_total_return", ColumnKind.FLOAT, True),
        ColumnSpec("total_return_delta", ColumnKind.FLOAT, True),
        ColumnSpec("return_rank", ColumnKind.INTEGER, True, exclusive_minimum=0.0),
        ColumnSpec("sharpe_rank", ColumnKind.INTEGER, True, exclusive_minimum=0.0),
    ),
    unique_by=("run_id",),
)
