from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.db import database
from src.db.database import create_db_engine


def test_sqlite_engine_enables_foreign_keys(tmp_path: Path) -> None:
    """A SQLite engine enables referential integrity for every connection."""
    engine = create_db_engine(f"sqlite:///{tmp_path / 'test.db'}")

    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1


def test_non_sqlite_engine_has_no_sqlite_connect_args(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-SQLite URL does not receive SQLite-only connection options."""
    captured: dict[str, object] = {}
    fake_engine = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    def fake_create_engine(url: str, **kwargs: object) -> object:
        captured.update(kwargs)
        return fake_engine

    monkeypatch.setattr(database, "create_engine", fake_create_engine)

    database.create_db_engine("postgresql://example.invalid/db")

    assert "connect_args" not in captured
