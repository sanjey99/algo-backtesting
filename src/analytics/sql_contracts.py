"""Types describing the closed SQL analytics query interface."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import TypeAlias

Scalar: TypeAlias = str | int | float | datetime | None
BindValue: TypeAlias = Scalar | tuple[Scalar, ...]


class QueryId(StrEnum):
    """Identifiers for reviewed packaged SQL statements."""

    STRATEGY_RUN_COMPARISON = "strategy_run_comparison"
    TRADE_SEQUENCE = "trade_sequence"
    EQUITY_DRAWDOWN_AUDIT = "equity_drawdown_audit"
    STRATEGY_COHORT_SUMMARY = "strategy_cohort_summary"
    INTEGRITY_TABLE_COUNTS = "integrity_table_counts"
    INTEGRITY_PER_RUN_RECONCILIATION = "integrity_per_run_reconciliation"
    INTEGRITY_DUPLICATE_METRICS = "integrity_duplicate_metrics"
    INTEGRITY_DUPLICATE_EQUITY = "integrity_duplicate_equity"
    INTEGRITY_ORPHAN_CHILDREN = "integrity_orphan_children"
    INTEGRITY_INVALID_RECORDS = "integrity_invalid_records"
    INTEGRITY_METRIC_RECONCILIATION = "integrity_metric_reconciliation"


class Severity(StrEnum):
    """Trust level assigned to one integrity invariant."""

    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass(frozen=True)
class ValidationFinding:
    """Immutable outcome for one stable integrity finding code."""

    code: str
    severity: Severity
    table: str | None
    observed_count: int
    sample_ids: tuple[str, ...]
    message: str


@dataclass(frozen=True)
class ValidationReport:
    """Versioned immutable integrity report for one database snapshot."""

    schema_version: str
    generated_at: datetime
    database: str
    tolerance: float
    findings: tuple[ValidationFinding, ...]

    @property
    def has_failures(self) -> bool:
        """Return whether any finding makes analytical output untrustworthy."""
        return any(finding.severity is Severity.FAIL for finding in self.findings)


@dataclass(frozen=True)
class ArtifactInfo:
    """Identity and content fingerprint for one published artifact."""

    path: Path
    byte_count: int
    sha256: str


@dataclass(frozen=True)
class ComparisonMetadata:
    """Versioned provenance for a validated comparison CSV."""

    schema_version: str
    generated_at: datetime
    query_id: QueryId
    sql_sha256: str
    bound_params: Mapping[str, Scalar]
    database_identifier: str
    row_count: int
    ordered_columns: tuple[str, ...]
    contract_version: str
    contract_valid: bool
    validation_report_path: str
    diagnostic_override: bool

    def __post_init__(self) -> None:
        """Snapshot external bind provenance behind an immutable mapping."""
        object.__setattr__(self, "bound_params", MappingProxyType(dict(self.bound_params)))


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


@dataclass(frozen=True)
class CohortFilters:
    """Optional selection bounds and a per-strategy completed-run threshold."""

    symbol: str | None
    start_date: datetime | None
    end_date: datetime | None
    minimum_run_count: int


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
        ColumnSpec("winning_trade_count", ColumnKind.INTEGER, False, minimum=0.0),
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


TRADE_SEQUENCE_CONTRACT = ResultContract(
    columns=(
        ColumnSpec("trade_id", ColumnKind.INTEGER, False, exclusive_minimum=0.0),
        ColumnSpec("exit_date", ColumnKind.DATETIME, False),
        ColumnSpec("pnl", ColumnKind.FLOAT, False),
        ColumnSpec("trade_sequence", ColumnKind.INTEGER, False, exclusive_minimum=0.0),
        ColumnSpec("cumulative_pnl", ColumnKind.FLOAT, False),
        ColumnSpec("cumulative_wins", ColumnKind.INTEGER, False, minimum=0.0),
        ColumnSpec("cumulative_win_rate", ColumnKind.FLOAT, False, minimum=0.0, maximum=1.0),
        ColumnSpec("rolling_5_trade_avg_pnl", ColumnKind.FLOAT, False),
    ),
    unique_by=("trade_id",),
)


EQUITY_DRAWDOWN_AUDIT_CONTRACT = ResultContract(
    columns=(
        ColumnSpec("equity_point_id", ColumnKind.INTEGER, False, exclusive_minimum=0.0),
        ColumnSpec("date", ColumnKind.DATETIME, False),
        ColumnSpec("equity", ColumnKind.FLOAT, False),
        ColumnSpec("stored_drawdown_pct", ColumnKind.FLOAT, False),
        ColumnSpec("audit_sequence", ColumnKind.INTEGER, False, exclusive_minimum=0.0),
        ColumnSpec("prior_equity", ColumnKind.FLOAT, True),
        ColumnSpec("point_return", ColumnKind.FLOAT, True),
        ColumnSpec("running_peak", ColumnKind.FLOAT, False),
        ColumnSpec("derived_drawdown_pct", ColumnKind.FLOAT, False),
        ColumnSpec("drawdown_delta_abs", ColumnKind.FLOAT, False, minimum=0.0),
        ColumnSpec("is_mismatch", ColumnKind.BOOLEAN, False),
    ),
    unique_by=("equity_point_id",),
)


COHORT_SUMMARY_CONTRACT = ResultContract(
    columns=(
        ColumnSpec("strategy_name", ColumnKind.STRING, False),
        ColumnSpec("symbol", ColumnKind.STRING, False),
        ColumnSpec("start_date", ColumnKind.DATETIME, False),
        ColumnSpec("end_date", ColumnKind.DATETIME, False),
        ColumnSpec("initial_capital", ColumnKind.FLOAT, False, exclusive_minimum=0.0),
        ColumnSpec("commission_pct", ColumnKind.FLOAT, False, minimum=0.0),
        ColumnSpec("slippage_pct", ColumnKind.FLOAT, False, minimum=0.0),
        ColumnSpec("run_count", ColumnKind.INTEGER, False, exclusive_minimum=0.0),
        ColumnSpec("average_derived_return", ColumnKind.FLOAT, True),
        ColumnSpec("average_sharpe_ratio", ColumnKind.FLOAT, True),
        ColumnSpec("worst_drawdown", ColumnKind.FLOAT, True),
        ColumnSpec("aggregate_closed_trade_count", ColumnKind.INTEGER, False, minimum=0.0),
        ColumnSpec("return_rank", ColumnKind.INTEGER, False, exclusive_minimum=0.0),
    ),
    unique_by=(
        "strategy_name",
        "symbol",
        "start_date",
        "end_date",
        "initial_capital",
        "commission_pct",
        "slippage_pct",
    ),
)


TABLE_COUNTS_CONTRACT = ResultContract(
    columns=(
        ColumnSpec("table_name", ColumnKind.STRING, False),
        ColumnSpec("row_count", ColumnKind.INTEGER, False, minimum=0.0),
    ),
    unique_by=("table_name",),
)


PER_RUN_RECONCILIATION_CONTRACT = ResultContract(
    columns=(
        ColumnSpec("run_id", ColumnKind.STRING, False),
        ColumnSpec("trade_count", ColumnKind.INTEGER, False, minimum=0.0),
        ColumnSpec("closed_trade_count", ColumnKind.INTEGER, False, minimum=0.0),
        ColumnSpec("open_trade_count", ColumnKind.INTEGER, False, minimum=0.0),
        ColumnSpec("equity_count", ColumnKind.INTEGER, False, minimum=0.0),
        ColumnSpec("metric_count", ColumnKind.INTEGER, False, minimum=0.0),
        ColumnSpec("distinct_metric_count", ColumnKind.INTEGER, False, minimum=0.0),
        ColumnSpec("optional_metric_count", ColumnKind.INTEGER, False, minimum=0.0),
    ),
    unique_by=("run_id",),
)


DUPLICATE_CONTRACT = ResultContract(
    columns=(
        ColumnSpec("run_id", ColumnKind.STRING, False),
        ColumnSpec("duplicate_key", ColumnKind.STRING, False),
        ColumnSpec("duplicate_count", ColumnKind.INTEGER, False, exclusive_minimum=1.0),
    ),
    unique_by=("run_id", "duplicate_key"),
)


DEFECT_RECORD_CONTRACT = ResultContract(
    columns=(
        ColumnSpec("defect_code", ColumnKind.STRING, False),
        ColumnSpec("table_name", ColumnKind.STRING, False),
        ColumnSpec("record_id", ColumnKind.STRING, False),
        ColumnSpec("run_id", ColumnKind.STRING, False),
    ),
    unique_by=("defect_code", "table_name", "record_id"),
)
