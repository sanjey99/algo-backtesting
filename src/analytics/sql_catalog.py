"""Closed catalogue for loading reviewed packaged SQL statements."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from importlib import resources
from pathlib import PureWindowsPath
from types import MappingProxyType

from sqlalchemy import bindparam, text
from sqlalchemy.sql.elements import TextClause

from src.analytics.sql_contracts import QueryId, QuerySpec, ResultContract


@dataclass(frozen=True)
class LoadedQuery:
    """A packaged SQL statement together with its integrity and bind metadata."""

    statement: TextClause
    sha256: str
    required_params: frozenset[str]


_DEFAULT_SPECS: Mapping[QueryId, QuerySpec] = MappingProxyType(
    {
        QueryId.STRATEGY_RUN_COMPARISON: QuerySpec(
            resource="strategy_run_comparison.sql",
            required_params=frozenset(),
            contract=ResultContract(columns=()),
        ),
        QueryId.TRADE_SEQUENCE: QuerySpec(
            resource="trade_sequence.sql",
            required_params=frozenset(),
            contract=ResultContract(columns=()),
        ),
        QueryId.EQUITY_DRAWDOWN_AUDIT: QuerySpec(
            resource="equity_drawdown_audit.sql",
            required_params=frozenset(),
            contract=ResultContract(columns=()),
        ),
        QueryId.STRATEGY_COHORT_SUMMARY: QuerySpec(
            resource="strategy_cohort_summary.sql",
            required_params=frozenset(),
            contract=ResultContract(columns=()),
        ),
    }
)


def _validate_resource_path(resource: str) -> None:
    """Reject resource names that could resolve outside the configured package."""
    path_parts = resource.split("/")
    if (
        not resource
        or "\\" in resource
        or PureWindowsPath(resource).is_absolute()
        or any(part in {"", ".", ".."} for part in path_parts)
        or not resource.endswith(".sql")
    ):
        raise ValueError("SQL resource must be a non-empty package-relative .sql path")


class QueryCatalogue:
    """Load only preconfigured SQL resources from an importable package."""

    def __init__(
        self,
        specs: Mapping[QueryId, QuerySpec] | None = None,
        package: str = "src.analytics.sql",
    ) -> None:
        self._specs = MappingProxyType(dict(_DEFAULT_SPECS if specs is None else specs))
        self._package = package

    def load(self, query_id: QueryId) -> LoadedQuery:
        """Load, validate, and fingerprint the configured statement for ``query_id``."""
        spec = self._specs[query_id]
        _validate_resource_path(spec.resource)
        extra_expanding = spec.expanding_params - spec.required_params
        if extra_expanding:
            names = ", ".join(sorted(extra_expanding))
            raise ValueError(f"Expanding parameters must be required parameters: {names}")

        source = resources.files(self._package).joinpath(spec.resource).read_text(encoding="utf-8")
        statement = text(source)
        actual_params = frozenset(statement.compile().params)
        if actual_params != spec.required_params:
            raise ValueError(
                f"Named binds for {query_id.value} do not match required parameters: "
                f"expected {sorted(spec.required_params)}, found {sorted(actual_params)}"
            )

        if spec.expanding_params:
            statement = statement.bindparams(
                *(bindparam(name, expanding=True) for name in sorted(spec.expanding_params))
            )

        return LoadedQuery(
            statement=statement,
            sha256=sha256(source.encode("utf-8")).hexdigest(),
            required_params=spec.required_params,
        )
