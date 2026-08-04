from __future__ import annotations

import pytest

from src.analytics.sql_catalog import QueryCatalogue
from src.analytics.sql_contracts import ColumnKind, ColumnSpec, QueryId, QuerySpec, ResultContract


def test_catalogue_loads_packaged_statement() -> None:
    """A configured query loads only its packaged resource and exposes its bind contract."""
    spec = QuerySpec(
        resource="smoke_select.sql",
        required_params=frozenset({"value"}),
        contract=ResultContract(columns=(ColumnSpec("value", ColumnKind.INTEGER, False),)),
    )
    catalogue = QueryCatalogue(
        specs={QueryId.STRATEGY_RUN_COMPARISON: spec},
        package="tests.sql_analytics.sql_resources",
    )

    loaded = catalogue.load(QueryId.STRATEGY_RUN_COMPARISON)

    assert str(loaded.statement).strip() == "SELECT :value AS value"
    assert loaded.required_params == frozenset({"value"})
    assert len(loaded.sha256) == 64


def test_query_id_rejects_arbitrary_resource_path() -> None:
    """The closed enum rejects an unreviewed path as a query identifier."""
    with pytest.raises(ValueError):
        QueryId("../../secrets.sql")
