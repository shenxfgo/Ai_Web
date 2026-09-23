from __future__ import annotations

import asyncio
import re
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.db import Base
from app.core.logging import reconfigure_std_streams
from app.settings import get_settings

reconfigure_std_streams()

config = context.config
settings = get_settings()
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

SCHEMA = settings.pg.schema_name
if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", SCHEMA):
    raise SystemExit(f"AIWEB_PG__SCHEMA_NAME 不是合法标识符：{SCHEMA!r}")

config.set_main_option("sqlalchemy.url", settings.pg.dsn())
target_metadata = Base.metadata


def _ensure_schema(connection: Connection) -> None:
    # Alembic 在跑任何 migration 之前就要建 version 表，schema 必须先存在
    connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"'))


def run_migrations_offline() -> None:
    context.configure(
        url=settings.pg.dsn(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        version_table_schema=SCHEMA,
        schema=SCHEMA,
    )
    with context.begin_transaction():
        # 离线 SQL 是给 DBA 审的完整脚本，少了建 schema 就不可执行
        context.execute(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"')
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    _ensure_schema(connection)
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        version_table_schema=SCHEMA,
        schema=SCHEMA,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
