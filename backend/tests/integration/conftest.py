"""pg 用例共用的"真库 + ASGI 客户端"夹具。

放在 integration/ 这一层而不是顶层 conftest：只有打真库的用例才需要它们，
顶层那份管的是"随机 schema + 真迁移"（见 `tests/conftest.py::pg_migrated`）。
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app import deps
from app.core.security import hash_password
from app.main import create_app
from app.models.user import User


@pytest.fixture
async def session_factory(
    pg_migrated: None, fernet_key: str
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """真迁移之后的会话工厂；每条用例从空表开始。

    `fernet_key` 挂在这里是必须的：不显式给一把，加解密就会去用开发者机器上的真密钥，
    而 `backend/.setup` 里那把是要能解真库口令的。
    """
    schema = os.environ["AIWEB_PG__SCHEMA_NAME"]
    engine = create_async_engine(os.environ["AIWEB_PG_TEST_DSN"], poolclass=NullPool)
    # 先删 data_sources 再删 users：前者 FK 指向后者
    async with engine.begin() as conn:
        await conn.execute(text(f'DELETE FROM "{schema}".data_sources'))
        await conn.execute(text(f'DELETE FROM "{schema}".users'))
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession], jwt_secret: str
) -> AsyncIterator[AsyncClient]:
    """打真 ASGI 应用，但 `get_db` 指向测试库的引擎。

    走 `dependency_overrides` 而不是改环境变量：应用自己的连接池会连到本机 .env
    那套 host/库上，测试就会写到真元数据库去。
    """

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    application = create_app()
    application.dependency_overrides[deps.get_db] = override_get_db
    async with AsyncClient(transport=ASGITransport(app=application), base_url="http://t") as c:
        yield c


@dataclass
class Account:
    """一个已登录身份：headers 直接拿去发请求，user_id 用来断言 owner 判定。"""

    user_id: int
    headers: dict[str, str]


@pytest.fixture
def login(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> Callable[..., Awaitable[Account]]:
    """建号 + 真走一次 `/auth/login`，返回可直接用的身份。

    走真登录而不是手搓 JWT：令牌里的 `tv`、citext 的大小写匹配、get_current_user 的
    三道门都顺带被验一遍。桩出来的身份会让"改密后旧令牌失效"那类用例失去对象。
    """

    async def _login(*, username: str, role: str = "member", password: str = "p-12345") -> Account:
        async with session_factory() as session:
            user = User(username=username, role=role, password_hash=hash_password(password))
            session.add(user)
            await session.commit()
            uid = user.id
        resp = await client.post(
            "/api/auth/login", json={"username": username, "password": password}
        )
        assert resp.status_code == 200, resp.text
        return Account(
            user_id=uid, headers={"Authorization": f"Bearer {resp.json()['access_token']}"}
        )

    return _login
