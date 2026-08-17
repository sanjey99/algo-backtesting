"""Alembic environment with explicit, injection-safe database routing."""
from __future__ import annotations

import logging
import os
from logging.config import fileConfig
from typing import Any

from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import Connection

from alembic import context
from src.db.tables import Base

config = context.config

# ``fileConfig`` closes and replaces every existing handler, including the
# embedding application's root handler. Let a preconfigured process own its
# logging boundary; retain Alembic's ini configuration for standalone commands.
if config.config_file_name is not None and not logging.getLogger().handlers:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _database_url() -> str:
    injected_url = config.attributes.get("database_url")
    if injected_url is not None:
        return str(injected_url)
    environment_url = os.environ.get("DATABASE_URL")
    if environment_url is not None:
        return environment_url
    configured_url = config.get_main_option("sqlalchemy.url")
    if configured_url is None:
        raise RuntimeError("Alembic sqlalchemy.url is not configured")
    return configured_url


def run_migrations_offline() -> None:
    """Run migrations without creating an Engine."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def _run_with_connection(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations on an injected connection or an explicitly resolved URL."""
    injected_connection: Any = config.attributes.get("connection")
    if injected_connection is not None:
        _run_with_connection(injected_connection)
        return

    section = dict(config.get_section(config.config_ini_section) or {})
    section["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    try:
        with connectable.connect() as connection:
            _run_with_connection(connection)
    finally:
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
