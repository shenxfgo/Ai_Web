"""数据源登记：真迁移、密文落库、接口与授权。

期望值口径：
- `docs/metadata-model.md` §2.2：`data_sources` 的列与约束
- `docs/roadmap.md` P2 验收 3：库里 `secret_enc` 以 `gAAAAAB` 开头，不是明文
- `docs/nl2sql-safety.md` §6：任何响应都不回传口令；DSN 拼接必须 `quote_plus`
- `docs/adr/0008-permission-grain-is-datasource.md`：权限粒度止于数据源级
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.security import encrypt_secret, hash_password
from app.models.datasource import DataSource
from app.models.user import User

pytestmark = pytest.mark.pg

# session_factory 在 tests/integration/conftest.py 里（含 fernet_key 与清表）


def _row(created_by: int, **overrides: object) -> DataSource:
    """一条最小合法记录：只给 NOT NULL 列，其余留给库里的 DEFAULT 去证明。"""
    values: dict[str, object] = {
        "name": "demo-mysql",
        "kind": "mysql",
        "host": "127.0.0.1",
        "port": 3306,
        "connect_user": "aiweb_ro",
        "secret_enc": encrypt_secret("口令-不进库"),
        "created_by": created_by,
    }
    values.update(overrides)
    return DataSource(**values)  # type: ignore[arg-type]


async def _owner(factory: async_sessionmaker[AsyncSession]) -> int:
    async with factory() as session:
        user = User(username="Owner", password_hash=hash_password("x"), role="admin")
        session.add(user)
        await session.commit()
        return user.id


async def test_列类型对得上元数据模型(session_factory) -> None:
    """§2.2 写 bytea/jsonb/text[]/timestamptz，库里就得真是那四种。

    尤其 bytea：写成 text 的话 Fernet 的 base64 也能存下，测试全绿，
    直到有人拿验收那句 `left(convert_from(secret_enc,'UTF8'),12)` 去核对才发现存的不是二进制。
    """
    schema = os.environ["AIWEB_PG__SCHEMA_NAME"]
    async with session_factory() as session:
        rows = (
            await session.execute(
                text(
                    "select column_name, data_type, is_nullable "
                    "from information_schema.columns "
                    "where table_schema = :s and table_name = 'data_sources'"
                ),
                {"s": schema},
            )
        ).all()
    types = {r[0]: r[1] for r in rows}
    nullable = {r[0]: r[2] for r in rows}
    assert types["secret_enc"] == "bytea", "密文列必须是二进制"
    assert types["params"] == "jsonb"
    assert types["include_schemas"] == "ARRAY" and types["exclude_tables"] == "ARRAY"
    assert types["last_sync_at"] == "timestamp with time zone"
    assert types["deleted_at"] == "timestamp with time zone"
    assert types["catalog_name"] == "text" and nullable["catalog_name"] == "NO"
    assert types["readonly_enforced"] == "boolean"


async def test_不填的那些列走库里默认的(session_factory) -> None:
    """故意用裸 INSERT 绕开 ORM：证明 DEFAULT 真的建在库里，而不是只在 Python 侧兜着。

    DBA 手工补一条记录、或以后有别的写入路径时，ORM 的那些 default 都不在场。
    """
    uid = await _owner(session_factory)
    schema = os.environ["AIWEB_PG__SCHEMA_NAME"]
    async with session_factory() as session:
        await session.execute(
            text(
                f'insert into "{schema}".data_sources '
                "(name,kind,host,port,connect_user,secret_enc,created_by) "
                "values (:n,:k,:h,:p,:u,:s,:c)"
            ),
            {
                "n": "raw-insert",
                "k": "mysql",
                "h": "127.0.0.1",
                "p": 3306,
                "u": "aiweb_ro",
                "s": b"\x00\x01",  # bytea：这一列只要求非空，内容不参与本用例
                "c": uid,
            },
        )
        await session.commit()
        row = (
            await session.execute(
                text(
                    f"select status, params, catalog_name, readonly_enforced, row_limit, "
                    f"timeout_ms, allow_global_access, include_schemas, server_version "
                    f"from \"{schema}\".data_sources where name = 'raw-insert'"
                )
            )
        ).one()
    assert tuple(row) == (
        "draft",
        {},
        "",
        True,
        1000,
        15000,
        False,
        None,
        None,
    )


async def test_kind_只认_mysql_与_postgres(session_factory) -> None:
    """§2.2 的 CHECK：预留 mssql/oracle 不等于现在就能写进去。"""
    uid = await _owner(session_factory)
    async with session_factory() as session:
        session.add(_row(uid, kind="oracle"))
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_name_全局唯一(session_factory) -> None:
    """kb-workflow.md：知识库目录名直接取 data_sources.name，重名会把两个源的卡片叠在一起。"""
    uid = await _owner(session_factory)
    async with session_factory() as session:
        session.add(_row(uid))
        await session.commit()
    async with session_factory() as session:
        session.add(_row(uid))
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_没有密文就不许建数据源(session_factory) -> None:
    uid = await _owner(session_factory)
    async with session_factory() as session:
        session.add(_row(uid, secret_enc=None))
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_created_by_必须指向真实用户(session_factory) -> None:
    """`created_by` 是 owner 判定唯一的依据，指向不存在的 id 等于凭空造出无人能管的源。"""
    async with session_factory() as session:
        session.add(_row(999999))
        with pytest.raises(IntegrityError):
            await session.commit()
