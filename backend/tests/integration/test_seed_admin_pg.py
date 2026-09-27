"""工单 005：`seed_admin.py` 的三条验收，全在子进程里跑。

进程内 import 私有函数测不到"退出码 + 给人看的中文提示"，而那正是这个脚本的验收项。

期望值口径：
- `docs/verification.md` §3 第 3 步：建 admin；**第二次运行幂等且不重置口令**；库里存 argon2 hash
- `docs/roadmap.md` 分组 5 / `.env.example`：口令为空则拒绝执行
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable
from subprocess import CompletedProcess

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.settings import get_settings

pytestmark = pytest.mark.pg

SCRIPT = "scripts/seed_admin.py"
RunPy = Callable[..., CompletedProcess[str]]
USER_KEY = "AIWEB_BOOTSTRAP_ADMIN_USERNAME"
PWD_KEY = "AIWEB_BOOTSTRAP_ADMIN_PASSWORD"
# 用户名钉死成 admin：不给的话脚本会去读本机 .env，用例就跟着开发者机器的配置跑了
_BOOTSTRAP_USER = {USER_KEY: "admin"}


@pytest.fixture(autouse=True)
async def _clean_users(pg_migrated: None) -> AsyncIterator[None]:
    """每条用例从空表开始：pg_migrated 保证表是真迁移建的（见 conftest）。"""
    schema = get_settings().pg.schema_name
    engine = create_async_engine(os.environ["AIWEB_PG_TEST_DSN"])
    async with engine.begin() as conn:
        await conn.execute(text(f'delete from "{schema}".users'))
    await engine.dispose()
    yield


async def _users() -> list[tuple[str, str]]:
    """库里的 (username, password_hash)，按用户名排序。"""
    schema = get_settings().pg.schema_name
    engine = create_async_engine(os.environ["AIWEB_PG_TEST_DSN"])
    try:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(f'select username, password_hash from "{schema}".users order by 1')
                )
            ).all()
        return [(str(r[0]), str(r[1])) for r in rows]
    finally:
        await engine.dispose()


async def test_口令为空时拒绝执行(run_py: RunPy) -> None:
    """拒绝得干净：退出码 2，且不能顺手建出一个空口令或弱口令的 admin。

    这里给的是**显式空串**，不是"不给这个变量"——后者会让脚本落回本机 .env 找口令，
    测的就不是同一条路径（该走哪条见 test_seed_admin_config.py）。
    """
    done: CompletedProcess[str] = run_py(SCRIPT, env={**_BOOTSTRAP_USER, PWD_KEY: ""})
    assert done.returncode == 2
    assert PWD_KEY in done.stdout + done.stderr
    assert await _users() == []


async def test_建出的admin存的是argon2串且口令不落打印(run_py) -> None:
    secret = "Bootstrap-口令-9"
    done = run_py(SCRIPT, env={**_BOOTSTRAP_USER, PWD_KEY: secret})
    assert done.returncode == 0, done.stdout + done.stderr
    rows = await _users()
    assert [u for u, _ in rows] == ["admin"]
    assert rows[0][1].startswith("$argon2id$")
    # 打印里出现明文口令 = 它会被抄进终端回滚缓冲、CI 日志、截图
    assert secret not in done.stdout + done.stderr


async def test_第二次运行幂等且不重置口令(run_py) -> None:
    """verification.md §3 第 3 步点名的一项：重跑不能把口令换掉。"""
    first = run_py(SCRIPT, env={**_BOOTSTRAP_USER, PWD_KEY: "第一个口令-1"})
    assert first.returncode == 0, first.stdout + first.stderr
    before = await _users()

    again = run_py(SCRIPT, env={**_BOOTSTRAP_USER, PWD_KEY: "第二个口令-2"})
    assert again.returncode == 0, again.stdout + again.stderr
    assert await _users() == before
