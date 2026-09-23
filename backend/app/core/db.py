"""元数据库（远端 PostgreSQL）连接与 ORM 基类。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.settings import get_settings

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s",
    "pk": "pk_%(table_name)s",
}


def _make_metadata() -> MetaData:
    # schema 在建表时就固定，Alembic 与应用必须看到同一个值，否则会"看起来没迁移"
    return MetaData(schema=get_settings().pg.schema_name, naming_convention=NAMING_CONVENTION)


class Base(DeclarativeBase):
    metadata = _make_metadata()


def create_engine(**overrides: Any) -> AsyncEngine:
    pg = get_settings().pg
    kwargs: dict[str, Any] = {
        "pool_pre_ping": True,
        "pool_size": pg.pool_size,
        "max_overflow": pg.max_overflow,
        "echo": pg.echo,
        "connect_args": pg.connect_args(),
    }
    kwargs.update(overrides)
    return create_async_engine(pg.dsn(), **kwargs)


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_engine()
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _sessionmaker


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """非请求上下文（后台任务、脚本）里用；请求内走 deps.get_db。"""
    session = get_sessionmaker()()
    try:
        yield session
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    finally:
        await session.close()


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
