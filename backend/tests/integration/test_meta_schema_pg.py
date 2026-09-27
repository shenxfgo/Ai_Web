"""0004 落进真库之后的四件事：列齐全、table_uid 算得对、四元组不互相覆盖、job 互斥。

期望值口径：
- `docs/metadata-model.md` §1（四元组唯一 + 稳定指纹）、§2.4、§2.5、§6（互斥靠部分唯一索引）
- ADR-0005：`md5(concat_ws(chr(31), datasource_id, catalog_name, schema_name, table_name))`
- `docs/verification.md` §2.2 第 2 项：迁移剩下的那 10% 只能靠真 PG 验

第一条用例是**迁移 ↔ ORM 漂移闸**：本仓库没接 `alembic check`，0004 是手写的七张表，
少写一列不会让单测变红，只会在第一次真同步写到那列时才炸。
"""

from __future__ import annotations

import hashlib
import os

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase

import app.models  # noqa: F401  # 先看一眼所有表，再谈漂移
from app.core.db import Base
from app.core.security import encrypt_secret, hash_password
from app.models.datasource import DataSource
from app.models.meta import MetaDatabase, MetaTable, SyncJob
from app.models.user import User

pytestmark = pytest.mark.pg


def _mapper(table_name: str) -> type[DeclarativeBase]:
    model = next(m.class_ for m in Base.registry.mappers if m.class_.__tablename__ == table_name)
    return model


async def _source_id(session: AsyncSession) -> int:
    user = User(username="Owner", password_hash=hash_password("x"), role="admin")
    session.add(user)
    await session.flush()
    src = DataSource(
        name="demo-mysql",
        kind="mysql",
        host="127.0.0.1",
        port=3306,
        connect_user="aiweb_ro",
        secret_enc=encrypt_secret("x"),
        created_by=user.id,
    )
    session.add(src)
    await session.flush()
    return src.id


async def test_迁移建的列与_orm_声明一一对应(session_factory) -> None:
    """缺列的迁移是"离线全绿、第一次真同步炸"的经典形状。"""
    cols_in_db = {}
    async with session_factory() as session:
        schema = os.environ["AIWEB_PG__SCHEMA_NAME"]
        rows = (
            await session.execute(
                text(
                    "SELECT table_name, column_name FROM information_schema.columns "
                    "WHERE table_schema = :s"
                ),
                {"s": schema},
            )
        ).all()
    for table_name, column_name in rows:
        cols_in_db.setdefault(table_name, set()).add(column_name)

    drifted: dict[str, dict[str, set[str]]] = {}
    for table_name in (
        "meta_database",
        "meta_table",
        "meta_column",
        "meta_index",
        "meta_index_column",
        "meta_relation",
        "sync_jobs",
    ):
        orm_cols = {c.name for c in _mapper(table_name).__table__.columns}
        db_cols = cols_in_db.get(table_name, set())
        if orm_cols != db_cols:
            drifted[table_name] = {"orm_only": orm_cols - db_cols, "db_only": db_cols - orm_cols}
    assert not drifted, f"迁移与 ORM 漂移：{drifted}"


async def test_table_uid_是四元组以单元分隔符拼接的_md5(session_factory) -> None:
    """ADR-0005：分隔符换成 `_` 就撞 md5，所以必须按 `\\x1f` 独立算一遍来对。"""
    async with session_factory() as session:
        ds = await _source_id(session)
        db_row = MetaDatabase(datasource_id=ds, catalog_name="", schema_name="ai_web_demo")
        session.add(db_row)
        await session.flush()
        tbl = MetaTable(
            datasource_id=ds,
            database_id=db_row.id,
            catalog_name="",
            schema_name="ai_web_demo",
            table_name="order_main",
            table_type="BASE TABLE",
        )
        session.add(tbl)
        await session.flush()
        uid = tbl.table_uid
        assert uid is not None

        expected = hashlib.md5(
            "\x1f".join([str(ds), "", "ai_web_demo", "order_main"]).encode()
        ).hexdigest()
        assert uid == expected
        assert len(uid) == 32


async def test_两个库里的同名表不会互相覆盖(session_factory) -> None:
    """§1 点名的反面做法：`UNIQUE (datasource_id, table_name)` 会让两个 `orders` 叠在一起。"""
    async with session_factory() as session:
        ds = await _source_id(session)
        for schema in ("shop_a", "shop_b"):
            db_row = MetaDatabase(datasource_id=ds, catalog_name="", schema_name=schema)
            session.add(db_row)
            await session.flush()
            session.add(
                MetaTable(
                    datasource_id=ds,
                    database_id=db_row.id,
                    catalog_name="",
                    schema_name=schema,
                    table_name="orders",
                    table_type="BASE TABLE",
                )
            )
        await session.flush()
        # 裸 SQL 必须显式带 schema：会话的 search_path 里没有本用例独占的随机 schema
        schema = os.environ["AIWEB_PG__SCHEMA_NAME"]
        n, uids = (
            await session.execute(
                text(
                    "SELECT count(*), array_agg(table_uid ORDER BY schema_name) "
                    f"FROM \"{schema}\".meta_table WHERE table_name='orders'"
                )
            )
        ).one()
        assert n == 2, "同名不同库必须各占一行"
        assert len(set(uids)) == 2, "四元组不同则指纹必须不同"


async def test_人工列与陈旧标记的默认值不越权(session_factory) -> None:
    """§3：同步只写 comment_raw；人工列默认为空、is_hidden/is_stale 默认 false。"""
    async with session_factory() as session:
        ds = await _source_id(session)
        db_row = MetaDatabase(datasource_id=ds, catalog_name="", schema_name="ai_web_demo")
        session.add(db_row)
        await session.flush()
        tbl = MetaTable(
            datasource_id=ds,
            database_id=db_row.id,
            catalog_name="",
            schema_name="ai_web_demo",
            table_name="product",
            table_type="BASE TABLE",
            comment_raw="商品主表（SKU 粒度）",
        )
        session.add(tbl)
        await session.flush()
        assert tbl.comment_zh is None and tbl.business_desc is None and tbl.granularity is None
        assert tbl.is_hidden is False and tbl.is_stale is False


async def test_同一数据源的两个未结束_job_被数据库挡住(session_factory) -> None:
    """§6：409 `sync_already_running` 的依据是部分唯一索引，不是应用里的 bool。"""
    async with session_factory() as session:
        ds = await _source_id(session)
        session.add(SyncJob(datasource_id=ds, status="running", phase="tables"))
        await session.flush()
        session.add(SyncJob(datasource_id=ds, status="pending", phase="connect"))
        with pytest.raises(IntegrityError) as exc:
            await session.flush()
        assert "ux_sync_running" in str(exc.value)


async def test_已结束的_job_不占用互斥位(session_factory) -> None:
    """部分索引的 WHERE 只要写错，第二次同步就会永远 409。"""
    async with session_factory() as session:
        ds = await _source_id(session)
        for status in ("success", "failed", "partial", "cancelled"):
            session.add(SyncJob(datasource_id=ds, status=status, phase="done"))
        await session.flush()
        running = SyncJob(datasource_id=ds, status="running", phase="tables")
        session.add(running)
        await session.flush()
        assert running.id is not None, "四个已结束 job 之后还能开新 job"


async def test_删数据源把整套快照一起带走(session_factory) -> None:
    """§2.4：六张表全部 CASCADE 到 data_sources，不留孤儿。"""
    schema = os.environ["AIWEB_PG__SCHEMA_NAME"]
    async with session_factory() as session:
        ds = await _source_id(session)
        db_row = MetaDatabase(datasource_id=ds, catalog_name="", schema_name="ai_web_demo")
        session.add(db_row)
        await session.flush()
        tbl = MetaTable(
            datasource_id=ds,
            database_id=db_row.id,
            catalog_name="",
            schema_name="ai_web_demo",
            table_name="product",
            table_type="BASE TABLE",
        )
        session.add(tbl)
        await session.flush()
        await session.execute(text(f'DELETE FROM "{schema}".data_sources'))
        n = (
            await session.execute(
                text(f'SELECT count(*) FROM "{schema}".meta_table WHERE datasource_id = :d'),
                {"d": ds},
            )
        ).scalar_one()
        assert n == 0
