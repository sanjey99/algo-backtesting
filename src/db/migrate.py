"""Explicit, mutation-safe schema assessment and Alembic upgrade workflow."""
from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect
from sqlalchemy.engine import Connection, Engine

from alembic import command
from src.db.database import create_db_engine

BASELINE_REVISION = "455406e2c7ac"
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_APPLICATION_TABLES = ("backtest_runs", "equity_curve", "metrics", "trades")


class SchemaState(StrEnum):
    """Closed set of schema states accepted by the migration workflow."""

    EMPTY = "empty"
    BASELINE_UNVERSIONED = "baseline_unversioned"
    BASELINE_VERSIONED = "baseline_versioned"
    CURRENT = "current"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SchemaAssessment:
    """Read-only schema classification with deterministic diagnostics."""

    state: SchemaState
    current_revision: str | None
    differences: tuple[str, ...]


@dataclass(frozen=True)
class MigrationOutcome:
    """Immutable record of an explicit migration workflow."""

    initial_state: SchemaState
    previous_revision: str | None
    current_revision: str
    actions: tuple[str, ...]


class SchemaNotCurrentError(RuntimeError):
    """Raised when application startup observes a stale or absent schema revision."""

    def __init__(self, current_revision: str | None, expected_revision: str) -> None:
        self.current_revision = current_revision
        self.expected_revision = expected_revision
        current = current_revision if current_revision is not None else "unversioned"
        super().__init__(
            f"Database schema is not current (revision {current}; expected {expected_revision}). "
            "Run `python -m src.db.migrate --database PATH` to classify and safely upgrade "
            "SQLite databases. For a configured versioned database, `alembic upgrade head` "
            "is also available."
        )


@dataclass(frozen=True)
class _ColumnSpec:
    name: str
    affinity: str
    nullable: bool
    primary_key_position: int = 0


_BASELINE_COLUMNS: dict[str, tuple[_ColumnSpec, ...]] = {
    "backtest_runs": (
        _ColumnSpec("id", "TEXT", False, 1),
        _ColumnSpec("strategy_name", "TEXT", False),
        _ColumnSpec("symbol", "TEXT", False),
        _ColumnSpec("start_date", "NUMERIC", False),
        _ColumnSpec("end_date", "NUMERIC", False),
        _ColumnSpec("params_json", "TEXT", False),
        _ColumnSpec("initial_capital", "REAL", False),
        _ColumnSpec("commission_pct", "REAL", False),
        _ColumnSpec("slippage_pct", "REAL", False),
        _ColumnSpec("created_at", "NUMERIC", False),
    ),
    "trades": (
        _ColumnSpec("id", "INTEGER", False, 1),
        _ColumnSpec("backtest_id", "TEXT", False),
        _ColumnSpec("entry_date", "NUMERIC", False),
        _ColumnSpec("exit_date", "NUMERIC", True),
        _ColumnSpec("direction", "TEXT", False),
        _ColumnSpec("entry_price", "REAL", False),
        _ColumnSpec("exit_price", "REAL", True),
        _ColumnSpec("quantity", "INTEGER", False),
        _ColumnSpec("pnl", "REAL", True),
        _ColumnSpec("pnl_pct", "REAL", True),
        _ColumnSpec("commission", "REAL", False),
    ),
    "equity_curve": (
        _ColumnSpec("id", "INTEGER", False, 1),
        _ColumnSpec("backtest_id", "TEXT", False),
        _ColumnSpec("date", "NUMERIC", False),
        _ColumnSpec("equity", "REAL", False),
        _ColumnSpec("drawdown_pct", "REAL", False),
    ),
    "metrics": (
        _ColumnSpec("id", "INTEGER", False, 1),
        _ColumnSpec("backtest_id", "TEXT", False),
        _ColumnSpec("metric_name", "TEXT", False),
        _ColumnSpec("metric_value", "REAL", False),
    ),
}

_EXPECTED_FOREIGN_KEYS = {
    "trades": (("backtest_id", "backtest_runs", "id"),),
    "equity_curve": (("backtest_id", "backtest_runs", "id"),),
    "metrics": (("backtest_id", "backtest_runs", "id"),),
}
_HARDENED_UNIQUES: dict[str, dict[str, tuple[str, ...]]] = {
    "metrics": {"uq_metrics_backtest_metric": ("backtest_id", "metric_name")},
    "equity_curve": {"uq_equity_curve_backtest_date": ("backtest_id", "date")},
}
_HARDENED_INDEXES: dict[str, dict[str, tuple[str, ...]]] = {
    "trades": {"ix_trades_backtest_exit_id": ("backtest_id", "exit_date", "id")},
    "backtest_runs": {
        "ix_backtest_runs_symbol_dates": ("symbol", "start_date", "end_date")
    },
}
_PREFLIGHT_QUERIES = (
    (
        "DUPLICATE_METRIC_KEYS",
        """SELECT COUNT(*) FROM (
               SELECT backtest_id, metric_name FROM metrics
               GROUP BY backtest_id, metric_name HAVING COUNT(*) > 1
           ) AS duplicate_metric_keys""",
    ),
    (
        "DUPLICATE_EQUITY_TIMESTAMPS",
        """SELECT COUNT(*) FROM (
               SELECT backtest_id, date FROM equity_curve
               GROUP BY backtest_id, date HAVING COUNT(*) > 1
           ) AS duplicate_equity_timestamps""",
    ),
    (
        "ORPHAN_TRADES",
        """SELECT COUNT(*) FROM trades AS child
           LEFT JOIN backtest_runs AS parent ON parent.id = child.backtest_id
           WHERE parent.id IS NULL""",
    ),
    (
        "ORPHAN_EQUITY_POINTS",
        """SELECT COUNT(*) FROM equity_curve AS child
           LEFT JOIN backtest_runs AS parent ON parent.id = child.backtest_id
           WHERE parent.id IS NULL""",
    ),
    (
        "ORPHAN_METRICS",
        """SELECT COUNT(*) FROM metrics AS child
           LEFT JOIN backtest_runs AS parent ON parent.id = child.backtest_id
           WHERE parent.id IS NULL""",
    ),
)


def _config(database_url: str | None = None) -> Config:
    config = Config(str(_PROJECT_ROOT / "alembic.ini"))
    if database_url is not None:
        config.attributes["database_url"] = database_url
    return config


def _head_revision() -> str:
    head = ScriptDirectory.from_config(_config()).get_current_head()
    if head is None:
        raise RuntimeError("Alembic has no head revision")
    return head


def _sqlite_affinity(declared_type: str) -> str:
    normalized = declared_type.upper()
    if "INT" in normalized:
        return "INTEGER"
    if any(token in normalized for token in ("CHAR", "CLOB", "TEXT")):
        return "TEXT"
    if not normalized or "BLOB" in normalized:
        return "BLOB"
    if any(token in normalized for token in ("REAL", "FLOA", "DOUB")):
        return "REAL"
    return "NUMERIC"


def _version_metadata(
    connection: Connection, *, has_version_table: bool
) -> tuple[str | None, tuple[str, ...]]:
    if not has_version_table:
        return None, ()
    columns = tuple(connection.exec_driver_sql('PRAGMA table_info("alembic_version")'))
    if (
        len(columns) != 1
        or str(columns[0][1]) != "version_num"
        or _sqlite_affinity(str(columns[0][2])) != "TEXT"
        or not bool(columns[0][3])
        or int(columns[0][5]) != 1
    ):
        return None, ("alembic_version table is malformed",)
    rows = tuple(connection.exec_driver_sql("SELECT version_num FROM alembic_version"))
    if not rows:
        return None, ("alembic_version table has no revision",)
    if len(rows) != 1:
        return None, (f"alembic_version table has {len(rows)} rows, expected 1",)
    revision = rows[0][0]
    if not isinstance(revision, str) or not revision:
        return None, ("alembic_version table is malformed",)
    return revision, ()


def _unexpected_object_differences(
    objects: tuple[tuple[str, str, str], ...], *, hardened: bool
) -> tuple[str, ...]:
    expected_indexes = {
        name
        for table_indexes in (_HARDENED_INDEXES if hardened else {}).values()
        for name in table_indexes
    }
    return tuple(
        f"unexpected {object_type}: {name}"
        for object_type, name, _ in objects
        if object_type != "table" and not (object_type == "index" and name in expected_indexes)
    )


def _column_differences(connection: Connection, table_name: str) -> list[str]:
    rows = tuple(connection.exec_driver_sql(f'PRAGMA table_info("{table_name}")'))
    actual = {
        str(row[1]): _ColumnSpec(
            str(row[1]),
            _sqlite_affinity(str(row[2])),
            not bool(row[3]),
            int(row[5]),
        )
        for row in rows
    }
    expected = {column.name: column for column in _BASELINE_COLUMNS[table_name]}
    differences = [
        f"{table_name}: missing column {name}" for name in sorted(expected.keys() - actual.keys())
    ]
    differences.extend(
        f"{table_name}: unexpected column {name}"
        for name in sorted(actual.keys() - expected.keys())
    )
    if actual.keys() == expected.keys():
        actual_order = tuple(str(row[1]) for row in rows)
        expected_order = tuple(expected)
        if actual_order != expected_order:
            differences.append(
                f"{table_name}: column order {actual_order!r}, expected {expected_order!r}"
            )
    for name in sorted(actual.keys() & expected.keys()):
        observed = actual[name]
        wanted = expected[name]
        if observed.affinity != wanted.affinity:
            differences.append(
                f"{table_name}.{name}: type {observed.affinity}, expected {wanted.affinity}"
            )
        if observed.nullable != wanted.nullable:
            differences.append(
                f"{table_name}.{name}: nullable {observed.nullable}, expected {wanted.nullable}"
            )
        if observed.primary_key_position != wanted.primary_key_position:
            differences.append(
                f"{table_name}.{name}: primary key position "
                f"{observed.primary_key_position}, expected {wanted.primary_key_position}"
            )
    return differences


def _foreign_key_differences(connection: Connection, table_name: str) -> list[str]:
    expected = set(_EXPECTED_FOREIGN_KEYS.get(table_name, ()))
    actual = {
        (str(row[3]), str(row[2]), str(row[4]))
        for row in connection.exec_driver_sql(f'PRAGMA foreign_key_list("{table_name}")')
    }
    differences = [
        f"{table_name}: missing foreign key {source} -> {target}.{column}"
        for source, target, column in sorted(expected - actual)
    ]
    differences.extend(
        f"{table_name}: unexpected foreign key {source} -> {target}.{column}"
        for source, target, column in sorted(actual - expected)
    )
    return differences


def _named_objects(
    connection: Connection, *, hardened: bool
) -> tuple[dict[str, dict[str, tuple[str, ...]]], dict[str, dict[str, tuple[str, ...]]]]:
    inspector = inspect(connection)
    unique_constraints = {
        table_name: {
            str(item["name"]): tuple(str(column) for column in item["column_names"])
            for item in inspector.get_unique_constraints(table_name)
        }
        for table_name in _APPLICATION_TABLES
    }
    indexes = {
        table_name: {
            str(item["name"]): tuple(str(column) for column in item["column_names"])
            for item in inspector.get_indexes(table_name)
        }
        for table_name in _APPLICATION_TABLES
    }
    expected_uniques = _HARDENED_UNIQUES if hardened else {}
    expected_indexes = _HARDENED_INDEXES if hardened else {}
    return (
        _object_differences(unique_constraints, expected_uniques, "unique constraint"),
        _object_differences(indexes, expected_indexes, "index"),
    )


def _object_differences(
    actual: dict[str, dict[str, tuple[str, ...]]],
    expected: dict[str, dict[str, tuple[str, ...]]],
    kind: str,
) -> dict[str, dict[str, tuple[str, ...]]]:
    differences: dict[str, dict[str, tuple[str, ...]]] = {}
    for table_name in _APPLICATION_TABLES:
        observed = actual.get(table_name, {})
        wanted = expected.get(table_name, {})
        table_differences: dict[str, tuple[str, ...]] = {}
        for name in sorted(wanted.keys() - observed.keys()):
            table_differences[f"missing {kind} {name}"] = wanted[name]
        for name in sorted(observed.keys() - wanted.keys()):
            table_differences[f"unexpected {kind} {name}"] = observed[name]
        for name in sorted(observed.keys() & wanted.keys()):
            if observed[name] != wanted[name]:
                table_differences[f"wrong {kind} {name}"] = observed[name]
        if table_differences:
            differences[table_name] = table_differences
    return differences


def _format_object_differences(
    differences: dict[str, dict[str, tuple[str, ...]]]
) -> list[str]:
    return [
        f"{table_name}: {description} ({', '.join(columns)})"
        for table_name, table_differences in differences.items()
        for description, columns in table_differences.items()
    ]


def _structural_differences(
    connection: Connection, table_names: set[str], *, hardened: bool
) -> tuple[str, ...]:
    expected_tables = set(_APPLICATION_TABLES)
    differences = [
        f"missing table: {table_name}" for table_name in sorted(expected_tables - table_names)
    ]
    differences.extend(
        f"unexpected table: {table_name}" for table_name in sorted(table_names - expected_tables)
    )
    if table_names == expected_tables:
        for table_name in _APPLICATION_TABLES:
            differences.extend(_column_differences(connection, table_name))
            differences.extend(_foreign_key_differences(connection, table_name))
        unique_differences, index_differences = _named_objects(connection, hardened=hardened)
        differences.extend(_format_object_differences(unique_differences))
        differences.extend(_format_object_differences(index_differences))
    return tuple(sorted(differences))


def assess_schema(engine: Engine) -> SchemaAssessment:
    """Classify a schema exactly without issuing stamps or DDL."""
    head_revision = _head_revision()
    with engine.connect() as connection:
        objects = tuple(
            (str(row[0]), str(row[1]), str(row[2]))
            for row in connection.exec_driver_sql(
                "SELECT type, name, tbl_name FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            )
        )
        raw_table_names = {name for object_type, name, _ in objects if object_type == "table"}
        has_version_table = "alembic_version" in raw_table_names
        current_revision, metadata_differences = _version_metadata(
            connection, has_version_table=has_version_table
        )
        table_names = raw_table_names - {"alembic_version"}
        if not objects:
            return SchemaAssessment(SchemaState.EMPTY, None, ())
        hardened = current_revision == head_revision
        differences = list(
            _structural_differences(connection, table_names, hardened=hardened)
        )
        differences.extend(metadata_differences)
        differences.extend(_unexpected_object_differences(objects, hardened=hardened))
        if current_revision not in (None, BASELINE_REVISION, head_revision):
            differences.append(f"unexpected revision: {current_revision}")
        sorted_differences = tuple(sorted(differences))

    if sorted_differences:
        state = SchemaState.UNKNOWN
    elif current_revision is None:
        state = SchemaState.BASELINE_UNVERSIONED
    elif current_revision == BASELINE_REVISION:
        state = SchemaState.BASELINE_VERSIONED
    elif current_revision == head_revision:
        state = SchemaState.CURRENT
    else:
        state = SchemaState.UNKNOWN
    return SchemaAssessment(state, current_revision, sorted_differences)


def _run_preflight(engine: Engine) -> None:
    with engine.connect() as connection:
        failures = tuple(
            (name, int(connection.exec_driver_sql(statement).scalar_one()))
            for name, statement in _PREFLIGHT_QUERIES
        )
    nonzero = tuple((name, count) for name, count in failures if count)
    if nonzero:
        raise RuntimeError(", ".join(f"{name}={count}" for name, count in nonzero))


def upgrade_database(database_url: str) -> MigrationOutcome:
    """Upgrade only EMPTY or exact baseline schemas through a closed state machine."""
    engine = create_db_engine(database_url)
    try:
        initial = assess_schema(engine)
        actions: list[str] = []
        config = _config(database_url)
        head_revision = _head_revision()
        if initial.state is SchemaState.UNKNOWN:
            detail = "; ".join(initial.differences) or "unrecognized schema state"
            raise RuntimeError(f"UNKNOWN database schema: {detail}")
        if initial.state is SchemaState.EMPTY:
            command.upgrade(config, "head")
            actions.append(f"upgrade:{head_revision}")
        elif initial.state is SchemaState.BASELINE_UNVERSIONED:
            _run_preflight(engine)
            actions.append("preflight")
            command.stamp(config, BASELINE_REVISION)
            actions.append(f"stamp:{BASELINE_REVISION}")
            command.upgrade(config, "head")
            actions.append(f"upgrade:{head_revision}")
        elif initial.state is SchemaState.BASELINE_VERSIONED:
            _run_preflight(engine)
            actions.append("preflight")
            command.upgrade(config, "head")
            actions.append(f"upgrade:{head_revision}")

        final = assess_schema(engine)
        if final.state is not SchemaState.CURRENT or final.current_revision is None:
            raise RuntimeError("Migration did not produce the exact current schema")
        return MigrationOutcome(
            initial.state,
            initial.current_revision,
            final.current_revision,
            tuple(actions),
        )
    finally:
        engine.dispose()


def verify_schema_current(engine: Engine) -> None:
    """Require the revision and physical schema to match the sole Alembic head."""
    expected_revision = _head_revision()
    assessment = assess_schema(engine)
    if assessment.state is not SchemaState.CURRENT:
        raise SchemaNotCurrentError(assessment.current_revision, expected_revision)


def _database_argument_to_url(database: str) -> str:
    if "://" in database:
        return database
    return f"sqlite:///{Path(database).expanduser().resolve()}"


def main(argv: Sequence[str] | None = None) -> int:
    """Run an explicit migration and print its classified state and applied actions."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, help="SQLite database path or SQLAlchemy URL")
    arguments = parser.parse_args(argv)
    database_url = _database_argument_to_url(arguments.database)
    engine = create_db_engine(database_url)
    try:
        assessment = assess_schema(engine)
    finally:
        engine.dispose()
    print(f"state={assessment.state.value}")
    print(f"current_revision={assessment.current_revision or 'unversioned'}")
    try:
        outcome = upgrade_database(database_url)
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 1
    for action in outcome.actions:
        print(f"action={action}")
    print(f"current_revision={outcome.current_revision}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
