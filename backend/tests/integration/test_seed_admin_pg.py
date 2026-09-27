"""`seed_admin.py` 建出的 admin 到底能不能用。

建号那步在子进程里跑：这个脚本的验收项就是"退出码 + 给人看的中文提示"，进程内 import
私有函数测不到它。但建完之后拿它登一次走的是应用内的会话，那条用例直接打 ASGI 客户端。

期望值口径：
- `docs/verification.md` §3 第 3 步：建 admin；**第二次运行幂等且不重置口令**；库里存 argon2 hash
- `docs/roadmap.md` 分组 5 / `.env.example`：口令为空则拒绝执行
"""

from __future__ import annotations

import os
from collections.abc import Callable
from subprocess import CompletedProcess

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.pg

SCRIPT = "scripts/seed_admin.py"
RunPy = Callable[..., CompletedProcess[str]]
USER_KEY = "AIWEB_BOOTSTRAP_ADMIN_USERNAME"
PWD_KEY = "AIWEB_BOOTSTRAP_ADMIN_PASSWORD"
# 用户名钉死成 admin：不给的话脚本会去读本机 .env，用例就跟着开发者机器的配置跑了
_BOOTSTRAP_USER = {USER_KEY: "admin"}


@pytest.fixture(autouse=True)
def _empty_from(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """空表起步：清表由 session_factory 的建立步骤做（它串着 pg_migrated，见 conftest）。

    挂在 autouse 上，是为了让下面每条用例的签名里只出现它真正要用的夹具。
    """


async def _users(session_factory: async_sessionmaker[AsyncSession]) -> list[tuple[str, str]]:
    """库里的 (username, password_hash)，按用户名排序。"""
    schema = os.environ["AIWEB_PG__SCHEMA_NAME"]
    async with session_factory() as session:
        rows = (
            await session.execute(
                text(f'select username, password_hash from "{schema}".users order by 1')
            )
        ).all()
    return [(str(r[0]), str(r[1])) for r in rows]


async def test_口令为空时拒绝执行(
    run_py: RunPy, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """拒绝得干净：退出码 2，且不能顺手建出一个空口令或弱口令的 admin。

    这里给的是**显式空串**，不是"不给这个变量"——后者会让脚本落回本机 .env 找口令，
    测的就不是同一条路径（该走哪条见 test_seed_admin_config.py）。
    """
    done: CompletedProcess[str] = run_py(SCRIPT, env={**_BOOTSTRAP_USER, PWD_KEY: ""})
    assert done.returncode == 2
    assert PWD_KEY in done.stdout + done.stderr
    assert await _users(session_factory) == []


async def test_建出的admin存的是argon2串且口令不落打印(
    run_py: RunPy, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    secret = "Bootstrap-口令-9"
    done = run_py(SCRIPT, env={**_BOOTSTRAP_USER, PWD_KEY: secret})
    assert done.returncode == 0, done.stdout + done.stderr
    rows = await _users(session_factory)
    assert [u for u, _ in rows] == ["admin"]
    assert rows[0][1].startswith("$argon2id$")
    # 打印里出现明文口令 = 它会被抄进终端回滚缓冲、CI 日志、截图
    assert secret not in done.stdout + done.stderr


async def test_建出的admin拿口令真的登得进去(run_py: RunPy, client: AsyncClient) -> None:
    """串长得像 argon2id 不等于验得过——上一条只在看列里的文本形状。

    pwdlib 的签名是 verify(password, hash)，我们对外收成 verify_password(digest, password)；
    两个实参传反不报错，只会永远返回 False。所以必须有一条从建号走到登录的往返。
    """
    secret = "Bootstrap-口令-9"
    done = run_py(SCRIPT, env={**_BOOTSTRAP_USER, PWD_KEY: secret})
    assert done.returncode == 0, done.stdout + done.stderr

    resp = await client.post("/api/auth/login", json={"username": "admin", "password": secret})
    assert resp.status_code == 200, resp.text
    assert resp.json()["user"]["role"] == "admin"


async def test_第二次运行幂等且不重置口令(
    run_py: RunPy, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """verification.md §3 第 3 步点名的一项：重跑不能把口令换掉。"""
    first = run_py(SCRIPT, env={**_BOOTSTRAP_USER, PWD_KEY: "第一个口令-1"})
    assert first.returncode == 0, first.stdout + first.stderr
    before = await _users(session_factory)

    again = run_py(SCRIPT, env={**_BOOTSTRAP_USER, PWD_KEY: "第二个口令-2"})
    assert again.returncode == 0, again.stdout + again.stderr
    assert await _users(session_factory) == before
