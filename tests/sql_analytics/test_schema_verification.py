"""Runtime schema verification and ORM/migration alignment tests."""
from __future__ import annotations

import importlib
from pathlib import Path
from types import ModuleType

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from alembic import command
from src.db import database
from src.db.database import create_db_engine
from src.db.tables import Base

PROJECT_ROOT = Path(__file__).resolve().parents[2]
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


def test_verify_schema_current_rejects_absent_and_stale_revisions(tmp_path: Path) -> None:
    """Revision verification must fail instead of silently creating or accepting stale tables."""
    api = _migration_api()
    empty_path = tmp_path / "empty.db"
    baseline_path = tmp_path / "baseline.db"
    command.upgrade(_config(baseline_path), BASELINE_REVISION)

    for path, current_revision in ((empty_path, None), (baseline_path, BASELINE_REVISION)):
        engine = create_db_engine(_database_url(path))
        with pytest.raises(api.SchemaNotCurrentError) as error:
            api.verify_schema_current(engine)
        assert error.value.current_revision == current_revision
        assert error.value.expected_revision == HEAD_REVISION
        assert "alembic upgrade head" in str(error.value)
        engine.dispose()


def test_verify_schema_current_accepts_head_revision(tmp_path: Path) -> None:
    """A migrated database should pass the startup gate."""
    api = _migration_api()
    path = tmp_path / "current.db"
    command.upgrade(_config(path), "head")
    engine = create_db_engine(_database_url(path))

    api.verify_schema_current(engine)

    engine.dispose()


def test_verify_schema_current_rejects_head_stamp_with_missing_table(tmp_path: Path) -> None:
    """A head stamp cannot make a physically incomplete schema safe for application startup."""
    api = _migration_api()
    path = tmp_path / "head-missing-table.db"
    command.upgrade(_config(path), "head")
    engine = create_db_engine(_database_url(path))
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE metrics")

    with pytest.raises(api.SchemaNotCurrentError) as error:
        api.verify_schema_current(engine)

    assert error.value.current_revision == HEAD_REVISION
    assert error.value.expected_revision == HEAD_REVISION
    engine.dispose()


def test_init_db_verifies_without_creating_unmigrated_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Restoring create_all in startup would silently bypass migration authority."""
    api = _migration_api()
    path = tmp_path / "unmigrated.db"
    engine = create_db_engine(_database_url(path))
    monkeypatch.setattr(database, "get_engine", lambda: engine)

    with pytest.raises(api.SchemaNotCurrentError):
        database.init_db()

    assert inspect(engine).get_table_names() == []
    engine.dispose()


def test_orm_metadata_matches_named_hardening_objects(tmp_path: Path) -> None:
    """Removing ORM declarations would let create_all drift from migrated schemas."""
    path = tmp_path / "orm.db"
    engine = create_engine(_database_url(path))

    Base.metadata.create_all(engine)

    inspector = inspect(engine)
    unique_constraints = {
        (table_name, item["name"], tuple(item["column_names"]))
        for table_name in ("metrics", "equity_curve")
        for item in inspector.get_unique_constraints(table_name)
    }
    indexes = {
        (table_name, item["name"], tuple(item["column_names"]))
        for table_name in ("trades", "backtest_runs")
        for item in inspector.get_indexes(table_name)
    }
    assert unique_constraints == {
        ("metrics", "uq_metrics_backtest_metric", ("backtest_id", "metric_name")),
        ("equity_curve", "uq_equity_curve_backtest_date", ("backtest_id", "date")),
    }
    assert indexes == {
        ("trades", "ix_trades_backtest_exit_id", ("backtest_id", "exit_date", "id")),
        ("backtest_runs", "ix_backtest_runs_symbol_dates", ("symbol", "start_date", "end_date")),
    }
    engine.dispose()
