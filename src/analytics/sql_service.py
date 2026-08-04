"""Execution and validation boundary for reviewed SQL analytics statements."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

import pandas as pd
from sqlalchemy.engine import Engine

from src.analytics.sql_catalog import QueryCatalogue
from src.analytics.sql_contracts import (
    COMPARISON_CONTRACT,
    ColumnKind,
    ComparisonFilters,
    QueryId,
    ResultContract,
    Scalar,
)


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
                "start_date": filters.start_date,
                "end_date": filters.end_date,
                "strategy_name": filters.strategy_name,
            },
        )
        return validate_frame(raw_frame, COMPARISON_CONTRACT)


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
                if not values.map(lambda value: isinstance(value, bool)).all():
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
