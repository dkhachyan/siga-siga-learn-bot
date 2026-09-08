"""Окружение Alembic. Адрес базы берётся из настроек приложения, не из alembic.ini."""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection

from siga.config import get_settings
from siga.db import models  # noqa: F401  # регистрирует таблицы в Base.metadata
from siga.db.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    """Адрес базы: -x db_url=..., затем DATABASE_URL, затем настройки приложения.

    DATABASE_URL проверяется раньше `get_settings()` намеренно: полные настройки
    требуют BOT_TOKEN, а накатывать миграции можно и без него.
    """
    override = context.get_x_argument(as_dictionary=True).get("db_url")
    if override:
        return str(override)
    return os.environ.get("DATABASE_URL") or get_settings().database_url


def run_migrations_offline() -> None:
    """Сгенерировать SQL без подключения к базе (`alembic upgrade head --sql`)."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    from siga.db.session import create_engine

    engine = create_engine(_database_url())
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_do_run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
