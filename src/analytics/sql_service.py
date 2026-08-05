"""Execution and validation boundary for reviewed SQL analytics statements."""
from __future__ import annotations

from collections.abc import Collection, Mapping
from datetime import UTC, datetime
from math import isfinite
from typing import Any, cast

import pandas as pd
from sqlalchemy import select
from sqlalchemy.engine import Connection, Engine

from src.analytics.sql_catalog import QueryCatalogue
from src.analytics.sql_contracts import (
    COHORT_SUMMARY_CONTRACT,
    COMPARISON_CONTRACT,
    DEFECT_RECORD_CONTRACT,
    DUPLICATE_CONTRACT,
    EQUITY_DRAWDOWN_AUDIT_CONTRACT,
    PER_RUN_RECONCILIATION_CONTRACT,
    TABLE_COUNTS_CONTRACT,
    TRADE_SEQUENCE_CONTRACT,
    BindValue,
    CohortFilters,
    ColumnKind,
    ComparisonFilters,
    QueryId,
    ResultContract,
    Severity,
    ValidationFinding,
    ValidationReport,
)
from src.db.tables import BacktestRun


class RunNotFoundError(LookupError):
    """Raised when an operation requires a persisted run that does not exist."""


class ContractValidationError(ValueError):
    """Raised when a SQL result does not conform to its reviewed contract."""


class IntegrityFailureError(RuntimeError):
    """Raised when an operation is blocked by a fatal integrity report."""

    def __init__(self, report: ValidationReport) -> None:
        super().__init__("Integrity validation found fatal failures")
        self.report = report


class AnalyticsRepository:
    """Execute only statements from an injected closed query catalogue."""

    def __init__(self, engine: Engine, catalogue: QueryCatalogue | None = None) -> None:
        self._engine = engine
        self._catalogue = catalogue or QueryCatalogue()

    def execute(self, query_id: QueryId, params: Mapping[str, BindValue]) -> pd.DataFrame:
        """Load a reviewed statement and return its raw result frame."""
        with self._engine.connect() as connection:
            return self.execute_on(connection, query_id, params)

    def execute_on(
        self,
        connection: Connection,
        query_id: QueryId,
        params: Mapping[str, BindValue],
    ) -> pd.DataFrame:
        """Execute a reviewed statement on a caller-owned snapshot connection."""
        loaded = self._catalogue.load(query_id)
        if frozenset(params) != loaded.required_params:
            raise ValueError("Query parameters do not match the reviewed bind contract")
        bound_params = cast(Mapping[str, Any], dict(params))
        return pd.read_sql_query(loaded.statement, connection, params=bound_params)


_INTEGRITY_CHECK_SQL = "PRAGMA integrity_check"
_FOREIGN_KEY_CHECK_SQL = "PRAGMA foreign_key_check"
_REPORT_SCHEMA_VERSION = "1.0"
_ALL_SCOPE_PARAMS: Mapping[str, BindValue] = {"scope_all": 1, "run_ids": ("",)}


class IntegrityService:
    """Run deterministic, read-only integrity checks against legacy or hardened SQLite data."""

    def __init__(self, engine: Engine, catalogue: QueryCatalogue | None = None) -> None:
        self._engine = engine
        self._repository = AnalyticsRepository(engine, catalogue)

    def validate(self, tolerance: float, sample_limit: int = 20) -> ValidationReport:
        """Return a versioned database-wide validation report without modifying the database."""
        _validate_tolerance(tolerance)
        if (
            isinstance(sample_limit, bool)
            or not isinstance(sample_limit, int)
            or not 1 <= sample_limit <= 100
        ):
            raise ValueError("sample_limit must be an integer from 1 to 100")

        with self._engine.connect() as connection, connection.begin():
            findings = self._pragma_findings(connection, sample_limit)
            findings += self._catalogue_findings(
                connection,
                scope_params=_ALL_SCOPE_PARAMS,
                tolerance=tolerance,
                sample_limit=sample_limit,
                include_counts=True,
            )
        database = self._engine.url.database or str(self._engine.url)
        return ValidationReport(
            schema_version=_REPORT_SCHEMA_VERSION,
            generated_at=datetime.now(UTC),
            database=database,
            tolerance=tolerance,
            findings=findings,
        )

    def failures_for_run_ids(
        self, run_ids: Collection[str], tolerance: float
    ) -> tuple[ValidationFinding, ...]:
        """Return fatal findings scoped in SQL to selected runs, independent of report samples."""
        _validate_tolerance(tolerance)
        if isinstance(run_ids, (str, bytes)) or any(
            not isinstance(run_id, str) or not run_id for run_id in run_ids
        ):
            raise ValueError("run_ids must contain non-empty strings")
        selected_run_ids = tuple(sorted(frozenset(run_ids)))
        if not selected_run_ids:
            return ()
        with self._engine.connect() as connection, connection.begin():
            findings = self._catalogue_findings(
                connection,
                scope_params={"scope_all": 0, "run_ids": selected_run_ids},
                tolerance=tolerance,
                sample_limit=20,
                include_counts=False,
            )
        return tuple(finding for finding in findings if finding.severity is Severity.FAIL)

    def _pragma_findings(
        self, connection: Connection, sample_limit: int
    ) -> tuple[ValidationFinding, ...]:
        if self._engine.dialect.name != "sqlite":
            return (
                _finding(
                    "SQLITE_INTEGRITY_CHECK",
                    Severity.WARN,
                    None,
                    (),
                    sample_limit,
                    "SQLite integrity_check is unavailable for this database dialect.",
                ),
                _finding(
                    "SQLITE_FOREIGN_KEY_CHECK",
                    Severity.WARN,
                    None,
                    (),
                    sample_limit,
                    "SQLite foreign_key_check is unavailable for this database dialect.",
                ),
            )
        integrity_rows = tuple(
            str(row[0])
            for row in connection.exec_driver_sql(_INTEGRITY_CHECK_SQL)
            if str(row[0]).lower() != "ok"
        )
        foreign_key_rows = tuple(
            f"{row[0]}:{row[1]}" for row in connection.exec_driver_sql(_FOREIGN_KEY_CHECK_SQL)
        )
        return (
            _finding(
                "SQLITE_INTEGRITY_CHECK",
                Severity.FAIL if integrity_rows else Severity.PASS,
                None,
                integrity_rows,
                sample_limit,
                "SQLite integrity_check reported no errors."
                if not integrity_rows
                else f"SQLite integrity_check reported {len(integrity_rows)} errors.",
            ),
            _finding(
                "SQLITE_FOREIGN_KEY_CHECK",
                Severity.FAIL if foreign_key_rows else Severity.PASS,
                None,
                foreign_key_rows,
                sample_limit,
                "SQLite foreign_key_check reported no violations."
                if not foreign_key_rows
                else f"SQLite foreign_key_check reported {len(foreign_key_rows)} violations.",
            ),
        )

    def _catalogue_findings(
        self,
        connection: Connection,
        *,
        scope_params: Mapping[str, BindValue],
        tolerance: float,
        sample_limit: int,
        include_counts: bool,
    ) -> tuple[ValidationFinding, ...]:
        findings: tuple[ValidationFinding, ...] = ()
        if include_counts:
            counts = self._execute_validated(
                connection, QueryId.INTEGRITY_TABLE_COUNTS, {}, TABLE_COUNTS_CONTRACT
            )
            findings += _table_count_findings(counts)

        per_run = self._execute_validated(
            connection,
            QueryId.INTEGRITY_PER_RUN_RECONCILIATION,
            scope_params,
            PER_RUN_RECONCILIATION_CONTRACT,
        )
        findings += _per_run_findings(per_run, sample_limit, include_counts)

        duplicate_metrics = self._execute_validated(
            connection, QueryId.INTEGRITY_DUPLICATE_METRICS, scope_params, DUPLICATE_CONTRACT
        )
        findings += (
            _frame_finding(
                duplicate_metrics,
                code="DUPLICATE_METRIC_KEYS",
                table="metrics",
                samples=tuple(
                    f"{row.run_id}:{row.duplicate_key}"
                    for row in duplicate_metrics.itertuples(index=False)
                ),
                sample_limit=sample_limit,
            ),
        )
        duplicate_equity = self._execute_validated(
            connection, QueryId.INTEGRITY_DUPLICATE_EQUITY, scope_params, DUPLICATE_CONTRACT
        )
        findings += (
            _frame_finding(
                duplicate_equity,
                code="DUPLICATE_EQUITY_TIMESTAMPS",
                table="equity_curve",
                samples=tuple(
                    f"{row.run_id}:{row.duplicate_key}"
                    for row in duplicate_equity.itertuples(index=False)
                ),
                sample_limit=sample_limit,
            ),
        )

        orphan_rows = self._execute_validated(
            connection, QueryId.INTEGRITY_ORPHAN_CHILDREN, scope_params, DEFECT_RECORD_CONTRACT
        )
        findings += _grouped_defect_findings(
            orphan_rows,
            (
                ("ORPHAN_TRADES", "trades"),
                ("ORPHAN_EQUITY_POINTS", "equity_curve"),
                ("ORPHAN_METRICS", "metrics"),
            ),
            sample_limit,
        )

        invalid_rows = self._execute_validated(
            connection, QueryId.INTEGRITY_INVALID_RECORDS, scope_params, DEFECT_RECORD_CONTRACT
        )
        findings += _grouped_defect_findings(
            invalid_rows,
            (
                ("INVALID_RUN_DATE_RANGE", "backtest_runs"),
                ("NONPOSITIVE_INITIAL_CAPITAL", "backtest_runs"),
                ("NEGATIVE_COMMISSION_PCT", "backtest_runs"),
                ("NEGATIVE_SLIPPAGE_PCT", "backtest_runs"),
                ("NONPOSITIVE_TRADE_QUANTITY", "trades"),
                ("INCONSISTENT_TRADE_CLOSE_FIELDS", "trades"),
            ),
            sample_limit,
        )

        reconciliation_params = {**scope_params, "tolerance": tolerance}
        reconciliation_rows = self._execute_validated(
            connection,
            QueryId.INTEGRITY_METRIC_RECONCILIATION,
            reconciliation_params,
            DEFECT_RECORD_CONTRACT,
        )
        findings += _grouped_defect_findings(
            reconciliation_rows,
            (
                ("TOTAL_RETURN_MISMATCH", "metrics"),
                ("DRAWDOWN_MISMATCH", "equity_curve"),
            ),
            sample_limit,
        )
        return findings

    def _execute_validated(
        self,
        connection: Connection,
        query_id: QueryId,
        params: Mapping[str, BindValue],
        contract: ResultContract,
    ) -> pd.DataFrame:
        return validate_frame(self._repository.execute_on(connection, query_id, params), contract)


def _validate_tolerance(tolerance: float) -> None:
    if isinstance(tolerance, bool) or not isfinite(tolerance) or tolerance < 0:
        raise ValueError("tolerance must be a finite non-negative number")


def _finding(
    code: str,
    severity: Severity,
    table: str | None,
    samples: tuple[str, ...],
    sample_limit: int,
    message: str,
    *,
    observed_count: int | None = None,
) -> ValidationFinding:
    return ValidationFinding(
        code=code,
        severity=severity,
        table=table,
        observed_count=len(samples) if observed_count is None else observed_count,
        sample_ids=samples[:sample_limit],
        message=message,
    )


def _table_count_findings(frame: pd.DataFrame) -> tuple[ValidationFinding, ...]:
    code_by_table = {
        "backtest_runs": "TABLE_COUNT_BACKTEST_RUNS",
        "trades": "TABLE_COUNT_TRADES",
        "equity_curve": "TABLE_COUNT_EQUITY_CURVE",
        "metrics": "TABLE_COUNT_METRICS",
    }
    counts = {
        str(row.table_name): int(cast(Any, row.row_count)) for row in frame.itertuples(index=False)
    }
    return tuple(
        _finding(
            code_by_table[table],
            Severity.PASS,
            table,
            (),
            1,
            f"Counted {counts[table]} rows in {table}.",
            observed_count=counts[table],
        )
        for table in ("backtest_runs", "trades", "equity_curve", "metrics")
    )


def _per_run_findings(
    frame: pd.DataFrame, sample_limit: int, include_counts: bool
) -> tuple[ValidationFinding, ...]:
    rows = tuple(frame.itertuples(index=False))
    count_samples = tuple(
        f"{row.run_id}:trades={row.trade_count},equity={row.equity_count},metrics={row.metric_count}"
        for row in rows
    )
    count_findings = (
        (
            _finding(
                "PER_RUN_CHILD_COUNTS",
                Severity.PASS,
                None,
                count_samples,
                sample_limit,
                f"Reconciled child-row counts for {len(rows)} backtest runs.",
                observed_count=len(rows),
            ),
        )
        if include_counts
        else ()
    )
    missing_equity = tuple(
        str(row.run_id) for row in rows if int(cast(Any, row.equity_count)) == 0
    )
    no_closed_trades = tuple(
        str(row.run_id) for row in rows if int(cast(Any, row.closed_trade_count)) == 0
    )
    missing_metrics = tuple(
        str(row.run_id) for row in rows if int(cast(Any, row.optional_metric_count)) < 10
    )
    metric_count_mismatches = tuple(
        str(row.run_id)
        for row in rows
        if int(cast(Any, row.metric_count)) != int(cast(Any, row.distinct_metric_count))
    )
    sparse_equity = tuple(
        str(row.run_id) for row in rows if int(cast(Any, row.equity_count)) == 1
    )
    open_trades = tuple(str(row.run_id) for row in rows if int(cast(Any, row.open_trade_count)) > 0)
    return count_findings + (
        _finding(
            "MISSING_REQUIRED_EQUITY_HISTORY",
            Severity.FAIL if missing_equity else Severity.PASS,
            "equity_curve",
            missing_equity,
            sample_limit,
            _count_message("runs have no required equity history", len(missing_equity)),
        ),
        _finding(
            "NO_CLOSED_TRADES",
            Severity.WARN if no_closed_trades else Severity.PASS,
            "trades",
            no_closed_trades,
            sample_limit,
            _count_message("runs have no closed trades", len(no_closed_trades)),
        ),
        _finding(
            "MISSING_OPTIONAL_METRICS",
            Severity.WARN if missing_metrics else Severity.PASS,
            "metrics",
            missing_metrics,
            sample_limit,
            _count_message("runs are missing optional metrics", len(missing_metrics)),
        ),
        _finding(
            "METRIC_ROW_COUNT_RECONCILIATION",
            Severity.FAIL if metric_count_mismatches else Severity.PASS,
            "metrics",
            metric_count_mismatches,
            sample_limit,
            _count_message(
                "runs have more metric rows than distinct metric names",
                len(metric_count_mismatches),
            ),
        ),
        _finding(
            "SPARSE_EQUITY_HISTORY",
            Severity.WARN if sparse_equity else Severity.PASS,
            "equity_curve",
            sparse_equity,
            sample_limit,
            _count_message("runs have sparse equity history", len(sparse_equity)),
        ),
        _finding(
            "OPEN_TRADES_EXCLUDED",
            Severity.WARN if open_trades else Severity.PASS,
            "trades",
            open_trades,
            sample_limit,
            _count_message(
                "runs have open trades excluded from realized analytics", len(open_trades)
            ),
        ),
    )


def _frame_finding(
    frame: pd.DataFrame,
    *,
    code: str,
    table: str,
    samples: tuple[str, ...],
    sample_limit: int,
) -> ValidationFinding:
    count = len(frame.index)
    return _finding(
        code,
        Severity.FAIL if count else Severity.PASS,
        table,
        samples,
        sample_limit,
        _count_message(f"{code.lower().replace('_', ' ')} groups found", count),
        observed_count=count,
    )


def _grouped_defect_findings(
    frame: pd.DataFrame,
    codes_and_tables: tuple[tuple[str, str], ...],
    sample_limit: int,
) -> tuple[ValidationFinding, ...]:
    return tuple(
        _defect_finding(frame, code, table, sample_limit) for code, table in codes_and_tables
    )


def _defect_finding(
    frame: pd.DataFrame, code: str, table: str, sample_limit: int
) -> ValidationFinding:
    matching = frame.loc[frame["defect_code"] == code]
    samples = tuple(str(value) for value in matching["record_id"].tolist())
    return _finding(
        code,
        Severity.FAIL if samples else Severity.PASS,
        table,
        samples,
        sample_limit,
        _count_message(f"{code.lower().replace('_', ' ')} records found", len(samples)),
    )


def _count_message(description: str, count: int) -> str:
    return f"{count} {description}."


class AnalyticsService:
    """Validate comparison inputs and normalize results at the service boundary."""

    def __init__(self, engine: Engine, catalogue: QueryCatalogue | None = None) -> None:
        self._repository = AnalyticsRepository(engine, catalogue)

    def compare_runs(self, filters: ComparisonFilters) -> pd.DataFrame:
        """Return one validated row per selected persisted strategy run."""
        if not filters.symbol:
            raise ValueError("symbol must not be empty")
        raw_frame = self._repository.execute(
            QueryId.STRATEGY_RUN_COMPARISON,
            {
                "symbol": filters.symbol,
                "start_date": _sqlite_datetime_text(filters.start_date),
                "end_date": _sqlite_datetime_text(filters.end_date),
                "strategy_name": filters.strategy_name,
            },
        )
        return validate_frame(raw_frame, COMPARISON_CONTRACT)

    def trade_sequence(self, run_id: str) -> pd.DataFrame:
        """Return deterministic cumulative and rolling realized-trade facts for one run."""
        self._require_run(run_id)
        raw_frame = self._repository.execute(QueryId.TRADE_SEQUENCE, {"run_id": run_id})
        return validate_frame(raw_frame, TRADE_SEQUENCE_CONTRACT)

    def equity_drawdown_audit(self, run_id: str, tolerance: float) -> pd.DataFrame:
        """Return a stored-versus-derived drawdown reconciliation for one persisted run."""
        if isinstance(tolerance, bool) or not isfinite(tolerance) or tolerance < 0:
            raise ValueError("tolerance must be a finite non-negative number")
        self._require_run(run_id)
        raw_frame = self._repository.execute(
            QueryId.EQUITY_DRAWDOWN_AUDIT,
            {"run_id": run_id, "tolerance": tolerance},
        )
        return validate_frame(raw_frame, EQUITY_DRAWDOWN_AUDIT_CONTRACT)

    def cohort_summary(self, filters: CohortFilters) -> pd.DataFrame:
        """Aggregate comparable selected runs and rank strategies inside each cohort."""
        if filters.symbol == "":
            raise ValueError("symbol must not be empty when provided")
        if (
            isinstance(filters.minimum_run_count, bool)
            or not isinstance(filters.minimum_run_count, int)
            or filters.minimum_run_count < 1
        ):
            raise ValueError("minimum_run_count must be an integer of at least one")
        if filters.start_date is not None and filters.end_date is not None:
            if filters.start_date > filters.end_date:
                raise ValueError("start_date must not be after end_date")
        raw_frame = self._repository.execute(
            QueryId.STRATEGY_COHORT_SUMMARY,
            {
                "symbol": filters.symbol,
                "start_date": _sqlite_datetime_text(filters.start_date)
                if filters.start_date is not None
                else None,
                "end_date": _sqlite_datetime_text(filters.end_date)
                if filters.end_date is not None
                else None,
                "minimum_run_count": filters.minimum_run_count,
            },
        )
        return validate_frame(raw_frame, COHORT_SUMMARY_CONTRACT)

    def _require_run(self, run_id: str) -> None:
        """Reject missing parent rows while allowing known parents with empty children."""
        if not run_id:
            raise ValueError("run_id must not be empty")
        with self._repository._engine.connect() as connection:
            exists = connection.execute(
                select(BacktestRun.id).where(BacktestRun.id == run_id).limit(1)
            ).scalar_one_or_none()
        if exists is None:
            raise RunNotFoundError(f"Backtest run {run_id!r} does not exist")


def _sqlite_datetime_text(value: datetime) -> str:
    """Render a naive datetime exactly as SQLAlchemy's SQLite DateTime storage does."""
    if value.tzinfo is not None:
        raise ValueError("filter datetimes must be timezone-naive")
    return value.strftime("%Y-%m-%d %H:%M:%S.%f")


def validate_frame(frame: pd.DataFrame, contract: ResultContract) -> pd.DataFrame:
    """Return a normalized copy after enforcing a result contract exactly."""
    if tuple(frame.columns) != contract.names:
        raise ContractValidationError("Result columns do not match the required names and order")

    normalized = frame.copy()
    for column in contract.columns:
        name = column.name
        try:
            if column.kind is ColumnKind.STRING:
                normalized[name] = normalized[name].astype("string")
            elif column.kind is ColumnKind.INTEGER:
                numeric = pd.to_numeric(normalized[name], errors="raise")
                if ((numeric.dropna() % 1) != 0).any():
                    raise ContractValidationError(f"Column {name} contains non-integer values")
                normalized[name] = numeric.astype("Int64")
            elif column.kind is ColumnKind.FLOAT:
                normalized[name] = pd.to_numeric(normalized[name], errors="raise").astype("Float64")
            elif column.kind is ColumnKind.DATETIME:
                normalized[name] = pd.to_datetime(normalized[name], errors="raise").astype(
                    "datetime64[ns]"
                )
            elif column.kind is ColumnKind.BOOLEAN:
                values = normalized[name].dropna()
                if not values.map(lambda value: isinstance(value, bool) or value in {0, 1}).all():
                    raise ContractValidationError(f"Column {name} contains non-boolean values")
                normalized[name] = normalized[name].astype("boolean")
        except (TypeError, ValueError) as error:
            message = f"Column {name} cannot be coerced to {column.kind.value}"
            raise ContractValidationError(message) from error

        values = normalized[name]
        if not column.nullable and values.isna().any():
            raise ContractValidationError(f"Column {name} must not contain nulls")
        if column.kind in {ColumnKind.INTEGER, ColumnKind.FLOAT}:
            numeric_values = values.dropna()
            if column.minimum is not None and (numeric_values < column.minimum).any():
                raise ContractValidationError(f"Column {name} is below its minimum")
            if column.exclusive_minimum is not None and (
                numeric_values <= column.exclusive_minimum
            ).any():
                raise ContractValidationError(f"Column {name} is below its exclusive minimum")
            if column.maximum is not None and (numeric_values > column.maximum).any():
                raise ContractValidationError(f"Column {name} is above its maximum")

    if contract.unique_by and normalized.loc[:, list(contract.unique_by)].duplicated().any():
        raise ContractValidationError("Result contains duplicate unique keys")
    return normalized
