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
