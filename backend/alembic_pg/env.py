"""Alembic environment for the PostgreSQL knowledge plane.

Runs migrations through the async asyncpg engine (no second sync driver needed).
URL comes from backend settings (.env), never hardcoded.
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from backend.config import get_settings
from backend.knowledge.models import PGBase

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# configparser treats % as interpolation syntax — escape URL-encoded credentials.
config.set_main_option("sqlalchemy.url", get_settings().postgres_url.replace("%", "%%"))

target_metadata = PGBase.metadata


def _configure(connection=None, url=None):
    context.configure(
        connection=connection,
        url=url,
        target_metadata=target_metadata,
        version_table="alembic_version_pg",
        literal_binds=url is not None,
    )


def run_migrations_offline() -> None:
    _configure(url=config.get_main_option("sqlalchemy.url"))
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection) -> None:
    _configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
