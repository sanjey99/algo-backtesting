"""Classification and mutation-safety coverage for legacy SQLite upgrades."""
from __future__ import annotations

import importlib
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from alembic import command
from src.db.database import create_db_engine

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TRACKED_LEGACY_DATABASE = PROJECT_ROOT / "tests" / "data" / "backtester.db"
BASELINE_REVISION = "455406e2c7ac"
HEAD_REVISION = "20260804_01"


def _migration_api() -> ModuleType:
    return importlib.import_module("src.db.migrate")


def _database_url(path: Path) -> str:
    return f"sqlite:///{path}"


def _config(path: Path) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["database_url"] = _database_url(path)
    return config


def _baseline(path: Path, *, versioned: bool = True) -> None:
    command.upgrade(_config(path), BASELINE_REVISION)
    if not versioned:
        with sqlite3.connect(path) as connection:
            connection.execute("DROP TABLE alembic_version")


def _current(path: Path) -> None:
    command.upgrade(_config(path), "head")


def _assessment(path: Path) -> Any:
    api = _migration_api()
    engine = create_db_engine(_database_url(path))
    try:
        return api.assess_schema(engine)
    finally:
        engine.dispose()


def _schema_snapshot(path: Path) -> tuple[tuple[object, ...], ...]:
    with sqlite3.connect(path) as connection:
        schema = connection.execute(
            """
            SELECT type, name, tbl_name, sql
            FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%'
            ORDER BY type, name
            """
        ).fetchall()
        counts = tuple(
            (table_name, connection.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0])
            for table_name in ("backtest_runs", "trades", "equity_curve", "metrics")
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table_name,)
            ).fetchone()
        )
    return tuple(schema) + counts


def _seed_parent(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO backtest_runs (
                id, strategy_name, symbol, start_date, end_date, params_json,
                initial_capital, commission_pct, slippage_pct, created_at
            ) VALUES ('run-1', 'strategy', 'SPY', '2024-01-01', '2024-12-31', '{}',
                      100000.0, 0.001, 0.0005, '2024-01-01')
            """
        )


def test_empty_database_classifies_without_creating_schema(tmp_path: Path) -> None:
    """A classifier that creates tables would turn inspection into a mutation."""
    path = tmp_path / "empty.db"

    assessment = _assessment(path)

    assert assessment.state.value == "empty"
    assert assessment.current_revision is None
    assert assessment.differences == ()
    assert inspect(create_engine(_database_url(path))).get_table_names() == []


def test_view_only_database_is_unknown_and_refused_without_mutation(tmp_path: Path) -> None:
    """Ignoring non-table objects would mutate an unrelated view-only database as EMPTY."""
    api = _migration_api()
    path = tmp_path / "view-only.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE VIEW sentinel_view AS SELECT 1 AS value")
    before = path.read_bytes()

    assessment = _assessment(path)

    assert assessment.state.value == "unknown"
    assert assessment.current_revision is None
    assert assessment.differences == (
        "missing table: backtest_runs",
        "missing table: equity_curve",
        "missing table: metrics",
        "missing table: trades",
        "unexpected view: sentinel_view",
    )
    with pytest.raises(RuntimeError, match="UNKNOWN"):
        api.upgrade_database(_database_url(path))
    assert path.read_bytes() == before


def test_tracked_database_classifies_as_exact_unversioned_baseline(tmp_path: Path) -> None:
    """Rejecting the shipped exact baseline would block the supported legacy workflow."""
    path = tmp_path / "tracked-copy.db"
    shutil.copyfile(TRACKED_LEGACY_DATABASE, path)

    assessment = _assessment(path)

    assert assessment.state.value == "baseline_unversioned"
    assert assessment.current_revision is None
    assert assessment.differences == ()


def test_extra_view_and_trigger_make_exact_schema_unknown(tmp_path: Path) -> None:
    """Exact tables and revision cannot hide unrelated SQLite objects."""
    api = _migration_api()
    path = tmp_path / "extra-objects.db"
    _current(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE VIEW run_symbols AS SELECT id, symbol FROM backtest_runs"
        )
        connection.execute(
            """
            CREATE TRIGGER observe_runs AFTER INSERT ON backtest_runs
            BEGIN
                SELECT NEW.id;
            END
            """
        )
    before = _schema_snapshot(path)

    assessment = _assessment(path)

    assert assessment.state.value == "unknown"
    assert assessment.current_revision == HEAD_REVISION
    assert assessment.differences == (
        "unexpected trigger: observe_runs",
        "unexpected view: run_symbols",
    )
    with pytest.raises(RuntimeError, match="UNKNOWN"):
        api.upgrade_database(_database_url(path))
    assert _schema_snapshot(path) == before


def test_empty_version_table_is_unknown_and_refused_without_mutation(tmp_path: Path) -> None:
    """An empty Alembic table is partial migration metadata, not an unversioned baseline."""
    api = _migration_api()
    path = tmp_path / "empty-version-table.db"
    _baseline(path)
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM alembic_version")
    before = _schema_snapshot(path)

    assessment = _assessment(path)

    assert assessment.state.value == "unknown"
    assert assessment.current_revision is None
    assert assessment.differences == ("alembic_version table has no revision",)
    with pytest.raises(RuntimeError, match="UNKNOWN"):
        api.upgrade_database(_database_url(path))
    assert _schema_snapshot(path) == before


def test_multiple_version_rows_are_unknown_and_refused_without_mutation(tmp_path: Path) -> None:
    """Multiple Alembic heads are malformed metadata, not an exception or upgrade target."""
    api = _migration_api()
    path = tmp_path / "multiple-version-rows.db"
    _baseline(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO alembic_version (version_num) VALUES ('second_revision')"
        )
    before = _schema_snapshot(path)

    assessment = _assessment(path)

    assert assessment.state.value == "unknown"
    assert assessment.current_revision is None
    assert assessment.differences == (
        "alembic_version table has 2 rows, expected 1",
    )
    with pytest.raises(RuntimeError, match="UNKNOWN"):
        api.upgrade_database(_database_url(path))
    assert _schema_snapshot(path) == before


def test_cli_cleanly_refuses_malformed_version_metadata(tmp_path: Path) -> None:
    """Malformed metadata must return a sanitized nonzero CLI result without traceback."""
    path = tmp_path / "malformed-version-table.db"
    _baseline(path)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE alembic_version")
        connection.execute("CREATE TABLE alembic_version (wrong_column TEXT)")
    before = _schema_snapshot(path)

    result = subprocess.run(
        [sys.executable, "-m", "src.db.migrate", "--database", str(path)],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "state=unknown" in result.stdout
    assert "UNKNOWN database schema" in result.stderr
    assert "Traceback" not in result.stderr
    assert _schema_snapshot(path) == before


def test_cli_rejects_non_sqlite_url_without_disclosing_credentials() -> None:
    """Operator input validation must not reflect credentials from unsupported URLs."""
    redaction_marker = "do-not-echo-marker-42"
    database_url = f"postgresql://operator:{redaction_marker}@localhost/backtests"

    result = subprocess.run(
        [sys.executable, "-m", "src.db.migrate", "--database", database_url],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "filesystem path or sqlite:// URL" in result.stderr
    assert redaction_marker not in result.stdout
    assert redaction_marker not in result.stderr
    assert "Traceback" not in result.stderr


def test_cli_sanitizes_initial_sqlite_open_failure(tmp_path: Path) -> None:
    """An initial connection failure must not expose driver details or local paths."""
    path = tmp_path / "missing-parent" / "private-database-name.db"

    result = subprocess.run(
        [sys.executable, "-m", "src.db.migrate", "--database", str(path)],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr.strip() == "Unable to open or assess the SQLite database."
    assert str(path) not in result.stderr
    assert "OperationalError" not in result.stderr
    assert "Traceback" not in result.stderr


def test_versioned_baseline_and_current_have_closed_states(tmp_path: Path) -> None:
    """Conflating baseline and head would skip required hardening."""
    baseline_path = tmp_path / "baseline.db"
    current_path = tmp_path / "current.db"
    _baseline(baseline_path)
    _current(current_path)

    baseline = _assessment(baseline_path)
    current = _assessment(current_path)

    assert (baseline.state.value, baseline.current_revision, baseline.differences) == (
        "baseline_versioned",
        BASELINE_REVISION,
        (),
    )
    assert (current.state.value, current.current_revision, current.differences) == (
        "current",
        HEAD_REVISION,
        (),
    )


@pytest.mark.parametrize(
    ("mutate", "expected_differences"),
    [
        (
            lambda connection: connection.execute("DROP TABLE metrics"),
            ("missing table: metrics",),
        ),
        (
            lambda connection: connection.execute(
                "ALTER TABLE backtest_runs ADD COLUMN unexpected TEXT"
            ),
            ("backtest_runs: unexpected column unexpected",),
        ),
    ],
)
def test_stamped_structural_mismatches_are_unknown(
    tmp_path: Path, mutate: object, expected_differences: tuple[str, ...]
) -> None:
    """Trusting a stamp alone would upgrade an incompatible schema."""
    path = tmp_path / "structural-mismatch.db"
    _baseline(path)
    with sqlite3.connect(path) as connection:
        mutate(connection)  # type: ignore[operator]

    assessment = _assessment(path)

    assert assessment.state.value == "unknown"
    assert assessment.current_revision == BASELINE_REVISION
    assert assessment.differences == expected_differences


def test_partial_unversioned_schema_reports_sorted_missing_tables(tmp_path: Path) -> None:
    """Matching one table name must not be enough to stamp a partial schema."""
    path = tmp_path / "partial.db"
    _baseline(path, versioned=False)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE metrics")
        connection.execute("DROP TABLE equity_curve")
        connection.execute("DROP TABLE trades")

    assessment = _assessment(path)

    assert assessment.state.value == "unknown"
    assert assessment.differences == (
        "missing table: equity_curve",
        "missing table: metrics",
        "missing table: trades",
    )


def test_wrong_sqlite_affinity_is_reported_exactly(tmp_path: Path) -> None:
    """Comparing only column names would accept a semantically wrong metric type."""
    path = tmp_path / "wrong-type.db"
    _baseline(path)
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE metrics RENAME TO old_metrics")
        connection.execute(
            """
            CREATE TABLE metrics (
                id INTEGER NOT NULL PRIMARY KEY,
                backtest_id VARCHAR NOT NULL REFERENCES backtest_runs (id),
                metric_name VARCHAR NOT NULL,
                metric_value TEXT NOT NULL
            )
            """
        )
        connection.execute("DROP TABLE old_metrics")

    assessment = _assessment(path)

    assert assessment.state.value == "unknown"
    assert assessment.differences == ("metrics.metric_value: type TEXT, expected REAL",)


def test_unknown_revision_is_not_inferred_from_matching_tables(tmp_path: Path) -> None:
    """Known table names must not make an unknown migration lineage safe."""
    path = tmp_path / "unknown-revision.db"
    _baseline(path)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE alembic_version SET version_num = 'mystery_revision'")

    assessment = _assessment(path)

    assert assessment.state.value == "unknown"
    assert assessment.current_revision == "mystery_revision"
    assert assessment.differences == ("unexpected revision: mystery_revision",)


def test_upgrade_database_handles_each_allowed_state(tmp_path: Path) -> None:
    """Omitting a state-machine branch can silently skip a stamp or hardening revision."""
    api = _migration_api()
    unversioned_path = tmp_path / "unversioned.db"
    stamped_path = tmp_path / "stamped.db"
    empty_path = tmp_path / "empty.db"
    current_path = tmp_path / "current.db"
    shutil.copyfile(TRACKED_LEGACY_DATABASE, unversioned_path)
    _baseline(stamped_path)
    _current(current_path)

    unversioned = api.upgrade_database(_database_url(unversioned_path))
    stamped = api.upgrade_database(_database_url(stamped_path))
    empty = api.upgrade_database(_database_url(empty_path))
    current = api.upgrade_database(_database_url(current_path))

    assert (
        unversioned.initial_state.value,
        unversioned.previous_revision,
        unversioned.current_revision,
        unversioned.actions,
    ) == (
        "baseline_unversioned",
        None,
        HEAD_REVISION,
        ("preflight", f"stamp:{BASELINE_REVISION}", f"upgrade:{HEAD_REVISION}"),
    )
    assert stamped.actions == ("preflight", f"upgrade:{HEAD_REVISION}")
    assert empty.actions == (f"upgrade:{HEAD_REVISION}",)
    assert current.actions == ()
    assert all(
        _assessment(path).state.value == "current"
        for path in (unversioned_path, stamped_path, empty_path, current_path)
    )


def test_unknown_schema_refusal_is_byte_for_byte_immutable(tmp_path: Path) -> None:
    """An UNKNOWN branch that stamps before refusing corrupts operator evidence."""
    api = _migration_api()
    path = tmp_path / "unknown.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE sentinel (id INTEGER PRIMARY KEY)")
    before = path.read_bytes()

    with pytest.raises(RuntimeError, match="UNKNOWN"):
        api.upgrade_database(_database_url(path))

    assert path.read_bytes() == before


def test_stamped_missing_table_refusal_preserves_schema_and_rows(tmp_path: Path) -> None:
    """A valid stamp must not override a missing baseline table."""
    api = _migration_api()
    path = tmp_path / "stamped-missing.db"
    _baseline(path)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE metrics")
    before = _schema_snapshot(path)

    with pytest.raises(RuntimeError, match="UNKNOWN"):
        api.upgrade_database(_database_url(path))

    assert _schema_snapshot(path) == before


@pytest.mark.parametrize(
    ("statement", "invariant"),
    [
        (
            "INSERT INTO metrics (backtest_id, metric_name, metric_value) "
            "VALUES ('run-1', 'sharpe', 1.0), ('run-1', 'sharpe', 2.0)",
            "DUPLICATE_METRIC_KEYS=1",
        ),
        (
            "INSERT INTO equity_curve (backtest_id, date, equity, drawdown_pct) "
            "VALUES ('run-1', '2024-02-01', 100.0, 0.0), "
            "('run-1', '2024-02-01', 101.0, 0.0)",
            "DUPLICATE_EQUITY_TIMESTAMPS=1",
        ),
        (
            "INSERT INTO trades (backtest_id, entry_date, direction, entry_price, quantity, "
            "commission) VALUES ('missing', '2024-02-01', 'LONG', 100.0, 1, 0.0)",
            "ORPHAN_TRADES=1",
        ),
        (
            "INSERT INTO equity_curve (backtest_id, date, equity, drawdown_pct) "
            "VALUES ('missing', '2024-02-01', 100.0, 0.0)",
            "ORPHAN_EQUITY_POINTS=1",
        ),
        (
            "INSERT INTO metrics (backtest_id, metric_name, metric_value) "
            "VALUES ('missing', 'sharpe', 1.0)",
            "ORPHAN_METRICS=1",
        ),
    ],
)
def test_hardening_preflight_fails_before_any_ddl(
    tmp_path: Path, statement: str, invariant: str
) -> None:
    """Moving a preflight after batch DDL would partially mutate unsafe databases."""
    path = tmp_path / f"unsafe-{invariant}.db"
    _baseline(path)
    _seed_parent(path)
    with sqlite3.connect(path) as connection:
        connection.execute(statement)
    before = _schema_snapshot(path)

    with pytest.raises(RuntimeError, match=invariant):
        command.upgrade(_config(path), "head")

    assert _schema_snapshot(path) == before
    assert _assessment(path).state.value == "baseline_versioned"


def test_normal_connections_reject_orphans_and_duplicate_natural_keys(tmp_path: Path) -> None:
    """Declared constraints are ineffective if normal SQLite connections bypass enforcement."""
    path = tmp_path / "enforced.db"
    _current(path)
    engine = create_db_engine(_database_url(path))
    _insert_parent(engine)

    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO metrics (backtest_id, metric_name, metric_value) "
                "VALUES ('missing', 'sharpe', 1.0)"
            )
        )

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO metrics (backtest_id, metric_name, metric_value) "
                "VALUES ('run-1', 'sharpe', 1.0)"
            )
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO metrics (backtest_id, metric_name, metric_value) "
                "VALUES ('run-1', 'sharpe', 2.0)"
            )
        )
    engine.dispose()


def _insert_parent(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO backtest_runs (
                    id, strategy_name, symbol, start_date, end_date, params_json,
                    initial_capital, commission_pct, slippage_pct, created_at
                ) VALUES ('run-1', 'strategy', 'SPY', '2024-01-01', '2024-12-31', '{}',
                          100000.0, 0.001, 0.0005, '2024-01-01')
                """
            )
        )
