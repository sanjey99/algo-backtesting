"""Execution and validation boundary for reviewed SQL analytics statements."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from math import isfinite
from typing import Any, cast

import pandas as pd
from sqlalchemy import select
from sqlalchemy.engine import Engine

from src.analytics.sql_catalog import QueryCatalogue
from src.analytics.sql_contracts import (
    COHORT_SUMMARY_CONTRACT,
    COMPARISON_CONTRACT,
    EQUITY_DRAWDOWN_AUDIT_CONTRACT,
    TRADE_SEQUENCE_CONTRACT,
    CohortFilters,
    ColumnKind,
    ComparisonFilters,
    QueryId,
    ResultContract,
    Scalar,
)
from src.db.tables import BacktestRun


class RunNotFoundError(LookupError):
    """Raised when an operation requires a persisted run that does not exist."""


class ContractValidationError(ValueError):
    """Raised when a SQL result does not conform to its reviewed contract."""


class AnalyticsRepository:
    """Execute only statements from an injected closed query catalogue."""

    def __init__(self, engine: Engine, catalogue: QueryCatalogue | None = None) -> None:
        self._engine = engine
        self._catalogue = catalogue or QueryCatalogue()

    def execute(self, query_id: QueryId, params: Mapping[str, Scalar]) -> pd.DataFrame:
        """Load a reviewed statement and return its raw result frame."""
        loaded = self._catalogue.load(query_id)
        if frozenset(params) != loaded.required_params:
            raise ValueError("Query parameters do not match the reviewed bind contract")
        with self._engine.connect() as connection:
            bound_params = cast(Mapping[str, Any], dict(params))
            return pd.read_sql_query(loaded.statement, connection, params=bound_params)


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
