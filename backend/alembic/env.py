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

# 不 import 包，Base.metadata 里就是空的：autogenerate 会把所有已有表看成"待删除"
import app.models  # noqa: F401
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
        # DSN 已经带了 ssl，唯独超时只能走 connect_args：不传的话 asyncpg 用默认 60s，
        # host 写错时看着像卡死而不是报错。
        connect_args=settings.pg.connect_args(),
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
        # 实测：不显式 commit 时 aiweb schema 与 alembic_version 都会随连接关闭一起回滚，
        # alembic 退出码却还是 0——看起来迁移成功了，库里什么都没有。SQLAlchemy 2.0 的
        # 异步连接是"commit as you go"，官方模板那段只在 alembic 自己开事务时才成立，
        # 而我们在它之前就先 _ensure_schema() 起了外层隐式事务。
        await connection.commit()
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
