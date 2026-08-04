"""Shared database fixture for SQL analytics tests."""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from src.db.database import create_db_engine
from src.db.tables import Base
from tests.sql_analytics.fixture_data import seed_comparison_runs


@pytest.fixture()
def analytics_db(tmp_path: Path) -> Engine:
    """Return a file-backed database seeded with deterministic comparison runs."""
    engine = create_db_engine(f"sqlite:///{tmp_path / 'analytics.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        seed_comparison_runs(session)
    return engine
