"""Alembic integration coverage for fresh and reversible analytics schemas."""
from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from alembic import command
from src.db import database

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BASELINE_REVISION = "455406e2c7ac"
HEAD_REVISION = "20260804_01"
APPLICATION_TABLES = {"backtest_runs", "equity_curve", "metrics", "trades"}


def alembic_config(database_url: str) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["database_url"] = database_url
    return config


def sqlite_url(path: Path) -> str:
    return f"sqlite:///{path}"


@contextmanager
def _safe_legacy_default(database_url: str) -> Iterator[None]:
    """Keep pre-fix Alembic behavior confined to the test's temporary database."""
    with (
        patch.dict(os.environ, {"DATABASE_URL": database_url}),
        patch.object(database, "DATABASE_URL", database_url),
    ):
        yield


def _upgrade(config: Config, revision: str, fallback_url: str) -> None:
    with _safe_legacy_default(fallback_url):
        command.upgrade(config, revision)


def _downgrade(config: Config, revision: str, fallback_url: str) -> None:
    with _safe_legacy_default(fallback_url):
        command.downgrade(config, revision)


def _schema_names(path: Path) -> set[str]:
    inspector = inspect(create_engine(sqlite_url(path)))
    return set(inspector.get_table_names())


def _unique_constraints(path: Path, table_name: str) -> dict[str, tuple[str, ...]]:
    inspector = inspect(create_engine(sqlite_url(path)))
    return {
        str(item["name"]): tuple(str(column) for column in item["column_names"])
        for item in inspector.get_unique_constraints(table_name)
    }


def _indexes(path: Path, table_name: str) -> dict[str, tuple[str, ...]]:
    inspector = inspect(create_engine(sqlite_url(path)))
    return {
        str(item["name"]): tuple(str(column) for column in item["column_names"])
        for item in inspector.get_indexes(table_name)
    }


def test_upgrade_head_builds_fresh_hardened_schema(tmp_path: Path) -> None:
    """Removing either revision leaves required tables or hardening absent."""
    database_path = tmp_path / "fresh.db"

    database_url = sqlite_url(database_path)
    _upgrade(alembic_config(database_url), "head", database_url)

    inspector = inspect(create_engine(sqlite_url(database_path)))
    assert set(inspector.get_table_names()) == APPLICATION_TABLES | {"alembic_version"}
    assert {
        tuple(foreign_key["constrained_columns"])
        for table_name in ("trades", "equity_curve", "metrics")
        for foreign_key in inspector.get_foreign_keys(table_name)
    } == {("backtest_id",)}
    assert _unique_constraints(database_path, "metrics") == {
        "uq_metrics_backtest_metric": ("backtest_id", "metric_name")
    }
    assert _unique_constraints(database_path, "equity_curve") == {
        "uq_equity_curve_backtest_date": ("backtest_id", "date")
    }
    assert _indexes(database_path, "trades") == {
        "ix_trades_backtest_exit_id": ("backtest_id", "exit_date", "id")
    }
    assert _indexes(database_path, "backtest_runs") == {
        "ix_backtest_runs_symbol_dates": ("symbol", "start_date", "end_date")
    }
    with create_engine(sqlite_url(database_path)).connect() as connection:
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert revision == HEAD_REVISION


def test_initial_revision_is_exact_unhardened_baseline(tmp_path: Path) -> None:
    """Moving hardening into the baseline would invalidate benchmark comparability."""
    database_path = tmp_path / "baseline.db"

    database_url = sqlite_url(database_path)
    _upgrade(alembic_config(database_url), BASELINE_REVISION, database_url)

    assert _schema_names(database_path) == APPLICATION_TABLES | {"alembic_version"}
    assert _unique_constraints(database_path, "metrics") == {}
    assert _unique_constraints(database_path, "equity_curve") == {}
    assert _indexes(database_path, "trades") == {}
    assert _indexes(database_path, "backtest_runs") == {}


def test_explicit_target_wins_over_environment_decoy_in_subprocess(tmp_path: Path) -> None:
    """Falling back to DATABASE_URL would migrate the operator's unrelated database."""
    target_path = tmp_path / "target.db"
    decoy_path = tmp_path / "decoy.db"
    with create_engine(sqlite_url(decoy_path)).begin() as connection:
        connection.exec_driver_sql("CREATE TABLE sentinel (id INTEGER PRIMARY KEY)")

    script = """
from alembic import command
from alembic.config import Config

config = Config('alembic.ini')
config.attributes['database_url'] = __import__('sys').argv[1]
command.upgrade(config, 'head')
"""
    environment = {**os.environ, "DATABASE_URL": sqlite_url(decoy_path)}
    subprocess.run(
        [sys.executable, "-c", script, sqlite_url(target_path)],
        cwd=PROJECT_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    assert _schema_names(target_path) == APPLICATION_TABLES | {"alembic_version"}
    assert _schema_names(decoy_path) == {"sentinel"}


def test_injected_connection_is_used_without_touching_configured_decoy(tmp_path: Path) -> None:
    """Ignoring a caller-owned connection would migrate the configured URL instead."""
    target_path = tmp_path / "connection-target.db"
    decoy_path = tmp_path / "configured-decoy.db"
    target_engine = create_engine(sqlite_url(target_path))
    config = alembic_config(sqlite_url(decoy_path))

    with target_engine.begin() as connection:
        config.attributes["connection"] = connection
        _upgrade(config, "head", sqlite_url(decoy_path))

    assert _schema_names(target_path) == APPLICATION_TABLES | {"alembic_version"}
    assert not decoy_path.exists()


def test_downgrade_and_reupgrade_preserve_revision_chain(tmp_path: Path) -> None:
    """A missing downgrade operation makes the hardening revision irreversible."""
    database_path = tmp_path / "round-trip.db"
    database_url = sqlite_url(database_path)
    config = alembic_config(database_url)
    _upgrade(config, "head", database_url)

    _downgrade(config, BASELINE_REVISION, database_url)

    assert _unique_constraints(database_path, "metrics") == {}
    assert _unique_constraints(database_path, "equity_curve") == {}
    assert _indexes(database_path, "trades") == {}
    assert _indexes(database_path, "backtest_runs") == {}

    _upgrade(config, "head", database_url)

    assert _unique_constraints(database_path, "metrics") == {
        "uq_metrics_backtest_metric": ("backtest_id", "metric_name")
    }
    with create_engine(sqlite_url(database_path)).connect() as connection:
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert revision == HEAD_REVISION
