"""Shared database fixture for SQL analytics tests."""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from alembic import command
from src.db.database import create_db_engine
from src.db.tables import Base
from tests.sql_analytics.fixture_data import seed_comparison_runs

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BASELINE_REVISION = "455406e2c7ac"


def _baseline_engine(path: Path) -> Engine:
    database_url = f"sqlite:///{path}"
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["database_url"] = database_url
    command.upgrade(config, BASELINE_REVISION)
    return create_db_engine(database_url)


@pytest.fixture()
def analytics_db(tmp_path: Path) -> Iterator[Engine]:
    """Return a file-backed database seeded with deterministic comparison runs."""
    engine = create_db_engine(f"sqlite:///{tmp_path / 'analytics.db'}")
    try:
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            seed_comparison_runs(session)
        yield engine
    finally:
        engine.dispose()


@pytest.fixture()
def legacy_analytics_db(tmp_path: Path) -> Iterator[Engine]:
    """Return the explicit baseline for tests that intentionally need duplicate keys."""
    engine = _baseline_engine(tmp_path / "legacy-analytics.db")
    try:
        with Session(engine) as session:
            seed_comparison_runs(session)
        yield engine
    finally:
        engine.dispose()
