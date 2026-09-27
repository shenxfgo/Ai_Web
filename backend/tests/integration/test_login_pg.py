"""第一条真正穿过 PG 的登录链路。

打的是真库（`AIWEB_PG_TEST_DSN`，schema 由 conftest 按会话随机生成），不是 sqlite 模拟：
`users` 用 citext / identity / CHECK 约束，这些在 sqlite 里一律测不出来。

期望值口径：
- `docs/metadata-model.md` §2.1：users 列定义
- `docs/architecture.md` §7：`/auth/login` → access_token，`/auth/me` → 五个用户字段
- `docs/verification.md` §2.2：pg 用例只准跑在随机 schema 里
"""

from __future__ import annotations

import logging
import os

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.security import TIMING_FILLER_DIGEST, hash_password
from app.models.user import User

pytestmark = pytest.mark.pg

# session_factory / client 两个夹具在 tests/integration/conftest.py 里


async def test_口令正确时登录换到令牌并访问受保护端点(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session:
        session.add(
            User(
                username="Alice",
                display_name="爱丽丝",
                password_hash=hash_password("w0rd-口令"),
                role="admin",
            )
        )
        await session.commit()

    resp = await client.post("/api/auth/login", json={"username": "alice", "password": "w0rd-口令"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["access_token"].count(".") == 2
    # citext 的语义在这里被钉住：匹配大小写不敏感，但存的是原样
    assert body["user"]["username"] == "Alice"
    assert body["user"]["role"] == "admin"
    assert "password" not in resp.text and "口令" not in resp.text

    me = await client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"}
    )
    assert me.status_code == 200
    assert me.json()["display_name"] == "爱丽丝"
    assert me.json()["token_version"] == 0


async def test_不带令牌访问受保护端点被拒(client: AsyncClient) -> None:
    resp = await client.get("/api/auth/me")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"


async def test_口令错与账号不存在给同一句文案(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """两句不同的文案 = 一个"用户名是否存在"探测器，白送枚举。"""
    async with session_factory() as session:
        session.add(User(username="Bob", password_hash=hash_password("right-口令"), role="member"))
        await session.commit()

    wrong_pwd = await client.post("/api/auth/login", json={"username": "Bob", "password": "X"})
    no_such = await client.post("/api/auth/login", json={"username": "Nobody", "password": "X"})
    assert wrong_pwd.status_code == no_such.status_code == 401
    assert wrong_pwd.json() == no_such.json()
    assert "口令" in wrong_pwd.json()["error"]["message"]


async def test_账号不存在时同样花掉一次口令校验(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """文案合并只堵住了一半的枚举：查无此人就直接返回，"快了多少"本身就是答案。

    计时断言在 CI 上一定会飘，所以钉的是校验调用的次数与参数——把登录口那步假串验证
    删掉，本用例当场变红。
    """
    import app.api.endpoints.auth as auth_endpoint

    digests: list[str] = []
    monkeypatch.setattr(auth_endpoint, "verify_password", lambda d, p: digests.append(d) or False)

    known_hash = hash_password("p-1")
    async with session_factory() as session:
        session.add(User(username="Erin", password_hash=known_hash, role="member"))
        await session.commit()

    unknown = await client.post("/api/auth/login", json={"username": "Nobody", "password": "p-1"})
    assert unknown.status_code == 401
    wrong = await client.post("/api/auth/login", json={"username": "erin", "password": "p-2"})
    assert wrong.status_code == 401
    assert digests == [TIMING_FILLER_DIGEST, known_hash]


async def test_登录成功会记下最近登录时间(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """`users.last_login_at` 取的是服务端时钟，且经 timestamptz 往返后仍带时区。"""
    async with session_factory() as session:
        user = User(username="Frank", password_hash=hash_password("p-1"), role="member")
        session.add(user)
        await session.commit()
        uid = user.id

    assert user.last_login_at is None  # 登录前没有痕迹，下面的断言才有意义
    resp = await client.post("/api/auth/login", json={"username": "frank", "password": "p-1"})
    assert resp.status_code == 200

    async with session_factory() as session:
        row = await session.get(User, uid)
    assert row is not None and row.last_login_at is not None
    assert row.last_login_at.utcoffset() is not None


async def test_令牌在手期间被停用也进不来(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """停用有两道门：登录口挡新的会话，get_current_user 挡已经在手的令牌。"""
    async with session_factory() as session:
        user = User(username="Grace", password_hash=hash_password("p-1"), role="member")
        session.add(user)
        await session.commit()
        uid = user.id
    resp = await client.post("/api/auth/login", json={"username": "grace", "password": "p-1"})
    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    assert (await client.get("/api/auth/me", headers=headers)).status_code == 200

    async with session_factory() as session:
        row = await session.get(User, uid)
        assert row is not None
        row.is_active = False
        await session.commit()

    denied = await client.get("/api/auth/me", headers=headers)
    assert denied.status_code == 401
    assert "停用" in denied.json()["error"]["message"]


async def test_口令不出现在任何日志记录里(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """绊线：登录链路今天一条日志都不打，所以它现在不会红。

    它挡的是以后有人顺手写 `logger.info("登录失败：%s", payload)` ——那条日志会把明文口令
    抄进 stdout、容器日志和 CI 归档。验收第 4 条的另一半（不落进响应体）由上面的用例钉。
    """
    secret = "日志-口令-9"
    async with session_factory() as session:
        session.add(User(username="Heidi", password_hash=hash_password(secret), role="member"))
        await session.commit()

    with caplog.at_level(logging.DEBUG):
        await client.post("/api/auth/login", json={"username": "heidi", "password": secret})
        await client.post("/api/auth/login", json={"username": "heidi", "password": "错的"})
        await client.post("/api/auth/login", json={"username": "查无此人", "password": "错的"})

    assert secret not in caplog.text
    assert "$argon2id$" not in caplog.text


async def test_改密后旧令牌立即失效(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """`users.token_version` 与令牌里的 `tv` 不一致就作废（metadata-model §2.1）。"""
    async with session_factory() as session:
        session.add(User(username="Carol", password_hash=hash_password("p-1"), role="member"))
        await session.commit()
    resp = await client.post("/api/auth/login", json={"username": "carol", "password": "p-1"})
    token = resp.json()["access_token"]
    ok = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert ok.status_code == 200

    async with session_factory() as session:
        row = await session.get(User, int(resp.json()["user"]["id"]))
        row.token_version += 1  # 真实改密口会做同样的 bump，这里直接改列
        await session.commit()

    denied = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert denied.status_code == 401
    assert "失效" in denied.json()["error"]["message"]


async def test_停用账号进不来(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session:
        session.add(
            User(
                username="Dave",
                password_hash=hash_password("p-1"),
                role="member",
                is_active=False,
            )
        )
        await session.commit()
    resp = await client.post("/api/auth/login", json={"username": "dave", "password": "p-1"})
    assert resp.status_code == 401
    assert "停用" in resp.json()["error"]["message"]


async def test_时间列在库里真的是带时区的(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """文档写的是 timestamptz；`Mapped[datetime]` 不带 timezone=True 会静默建成 timestamp，
    存进去的时刻到展示层整体偏一个时区，而且不报错。"""
    schema = os.environ["AIWEB_PG__SCHEMA_NAME"]
    async with session_factory() as session:
        rows = (
            await session.execute(
                text(
                    "select column_name, data_type from information_schema.columns "
                    "where table_schema = :s and table_name = 'users'"
                ),
                {"s": schema},
            )
        ).all()
    types = dict(rows)
    assert {c: types[c] for c in ("created_at", "updated_at", "last_login_at")} == {
        "created_at": "timestamp with time zone",
        "updated_at": "timestamp with time zone",
        "last_login_at": "timestamp with time zone",
    }
