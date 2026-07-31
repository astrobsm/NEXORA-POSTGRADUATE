"""Alembic environment.

The database URL always comes from application settings, so migrations can never be
run against a different database than the application itself uses.
"""

from __future__ import annotations

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

# Importing the model package registers every table on Base.metadata.
import app.models  # noqa: F401
from alembic import context
from app.core.config import settings
from app.db.base import Base, UtcDateTime

config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def include_object(obj, name, type_, reflected, compare_to) -> bool:
    """Skip anything the application does not own (e.g. PostGIS, extension tables)."""
    if type_ == "table" and name.startswith(("spatial_ref_sys", "pg_")):
        return False
    return True


def render_item(type_, obj, autogen_context) -> str | bool:
    """Render the project's custom column types with their import.

    Without this, autogenerate emits a bare ``app.db.base.UtcDateTime(...)`` into the
    migration and the module fails at import time with a NameError.
    """
    if type_ == "type" and isinstance(obj, UtcDateTime):
        autogen_context.imports.add("from app.db.base import UtcDateTime")
        return "UtcDateTime()"
    return False


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        include_object=include_object,
        render_item=render_item,
        # SQLite cannot ALTER most things in place; batch mode rewrites the table.
        render_as_batch=settings.is_sqlite,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            include_object=include_object,
            render_item=render_item,
            render_as_batch=settings.is_sqlite,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
