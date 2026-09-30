"""pg 用例共用的"真库 + ASGI 客户端"夹具。

放在 integration/ 这一层而不是顶层 conftest：只有打真库的用例才需要它们，
顶层那份管的是"随机 schema + 真迁移"（见 `tests/conftest.py::pg_migrated`）。
"""

from __future__ import annotations

import configparser
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app import deps
from app.core.security import hash_password
from app.main import create_app
from app.models.user import User
from app.settings import get_settings

BACKEND = Path(__file__).resolve().parents[2]
CNF = BACKEND / ".setup" / "aiweb_ro.cnf"


class DbAccount(NamedTuple):
    """演示库的连接四元组，**口令不参与 repr**。

    为什么要自己写 `__repr__`：pytest 一失败就会把 fixture 的实参和整条调用栈的帧局部变量
    倒进终端——用 tuple 的默认 repr，那条输出里第二位就是真口令，一次红测等于把口令贴进
    控制台、CI 日志和会话记录。它仍是 tuple 的子类，解包和 `tuple[str, str, str, int]`
    的注解都照旧。
    """

    user: str
    password: str
    host: str
    port: int

    def __repr__(self) -> str:
        host, port = self.host, self.port
        return f"DbAccount(user={self.user!r}, password=<redacted>, host={host!r}, port={port})"


@pytest.fixture(scope="module")
def account() -> DbAccount:
    """演示库的 `(user, password, host, port)`——口令只活在这条调用链里。

    读的是 gitignored 的 `.setup/aiweb_ro.cnf`（工单 002 生成的只读账号）：把它抄进
    `.env` 或测试常量里，就等于把一台真库的入口写进会被 push 的文件。
    缺文件就 skip，换台机器不该把整个闸口卡红。
    """
    if not CNF.exists():
        pytest.skip(f"缺少 {CNF}：要先跑工单 002 的建库脚本")
    cp = configparser.ConfigParser()
    cp.read(CNF, encoding="utf-8")
    c = cp["client"]
    return DbAccount(
        c["user"], c["password"], c.get("host", "127.0.0.1"), int(c.get("port", "3306"))
    )


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
    # 删除顺序按 FK 来：kb_card 挂在 data_sources 上级联，而它对 kb_index_profile 是 RESTRICT，
    # 所以必须先清 data_sources，profile 才删得掉。
    #
    # profile 为什么也在这一张清单上：随机 schema 是**整轮会话共用**的，用例造的第二个
    # profile（例如把老 profile 摘掉 active 再建一套新的那种）不会随 data_sources 一起走，
    # 于是全会话后头每一条"取当前生效 profile"的语句都会撞上它——表现是检索莫名其妙返回空。
    #
    # 往这张清单里加东西的时机：新表一有 DDL 就要跟上，否则"每条用例从空表开始"这句话
    # 会从这行注释开始骗人。012 的 chat_sessions/chat_messages 已跟上（删序照 FK：
    # messages → sessions → data_sources，sessions 对 users 是 CASCADE，但显式删才不依赖
    # 外键方向对不对）。还没建表的 datasource_grants 在这儿排队。
    #
    # 017 的 `sync_job_event` **不在**这张清单上，是有意的而不是漏了：它对 `sync_jobs` 是
    # `ON DELETE CASCADE`，而 `sync_jobs.datasource_id` 对 `data_sources` 同样是 CASCADE，
    # 所以上面那句 `DELETE FROM data_sources` 把它们一起带走了。判断"要不要往清单里加一行"
    # 的依据是 FK 的 ondelete，不是表名新不新。
    async with engine.begin() as conn:
        await conn.execute(text(f'DELETE FROM "{schema}".chat_messages'))
        await conn.execute(text(f'DELETE FROM "{schema}".chat_sessions'))
        await conn.execute(text(f'DELETE FROM "{schema}".data_sources'))
        await conn.execute(text(f'DELETE FROM "{schema}".kb_index_profile'))
        await conn.execute(text(f'DELETE FROM "{schema}".users'))
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture
def application(session_factory: async_sessionmaker[AsyncSession], jwt_secret: str) -> FastAPI:
    """装配好依赖 override 的应用实例，`client` 与各片自己的夹具**共用同一个对象**。

    单独成一个夹具是因为 `dependency_overrides` 是应用上的可变字典：SSE 那一片要在 `get_db`
    之外再 override 一个会话来源（流用的不是请求级会话，见 `deps.get_stream_sessionmaker`），
    加在同一个实例上才等于"两个夹具打的是同一个应用"，而不是各搓一个客户端去连不同的库。
    """

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app = create_app()
    app.dependency_overrides[deps.get_db] = override_get_db
    return app


@pytest.fixture
async def client(application: FastAPI) -> AsyncIterator[AsyncClient]:
    """打真 ASGI 应用，但 `get_db` 指向测试库的引擎。

    走 `dependency_overrides` 而不是改环境变量：应用自己的连接池会连到本机 .env
    那套 host/库上，测试就会写到真元数据库去。

    注意 `ASGITransport` 是**整响应缓冲**的（它把 body 片段攒成一个 list 才返回），所以它
    测不出"流式"——帧的顺序与内容照样能测，但"首帧早于作业结束"那种时序只能打真 HTTP
    服务（见 `test_sync_events_live.py`）。
    """

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


@pytest.fixture
def results_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """把结果目录钉进 tmp_path：早退用例要断言"一根 csv 都没落"，就得知道该看哪个目录。

    012 起放在这里而不是某个测试模块里：编排用例与 live 用例都要它，而 fixture 靠
    参数名解析，跨模块 import 会被 ruff 判成 F811（同名重定义）。
    """
    target = tmp_path / "results"
    monkeypatch.setenv("AIWEB_RESULT__DIR", str(target))
    get_settings.cache_clear()
    try:
        yield target
    finally:
        get_settings.cache_clear()
