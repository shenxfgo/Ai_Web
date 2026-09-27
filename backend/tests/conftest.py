from __future__ import annotations

import asyncio
import importlib.util
import os
import subprocess
import sys
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from subprocess import CompletedProcess
from types import ModuleType

import pytest
from dotenv import dotenv_values
from sqlalchemy.engine import make_url

from app.core.logging import reconfigure_std_streams

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

# 测试名是中文的，cp936 控制台下 pytest 的 ID 输出会成乱码
reconfigure_std_streams()

if sys.platform == "win32":  # asyncpg 与 Proactor 事件循环不兼容
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# AIWEB_PG_TEST_DSN 不是 Settings 字段（Settings 是 extra="ignore"），只有下面的 skip 判定读它。
# 从 .env 兜一份进 os.environ，免得跑 pg 用例要每次手工 export；真实环境变量始终优先。
_TEST_ONLY = ("AIWEB_PG_TEST_DSN", "AIWEB_REQUIRE_PG_TESTS")
for _key, _value in dotenv_values(BACKEND_ROOT / ".env").items():
    if _key in _TEST_ONLY and _value:
        os.environ.setdefault(_key, _value)

# docs/verification.md §2.2：每个 pg 测试会话独占随机 schema，绝不动 `aiweb`（那里面有真卡片）。
# 必须在本文件 import 任何 app.models / app.core.db 之前写死：`Base.metadata` 的 schema
# 是在 app.core.db 导入那一刻从 Settings 读一次就固定的，晚一步 ORM 就会去写真 schema。
# 未配 DSN 时整段跳过——单测里的 Settings 应当还是开发者机器上的真实值。
PG_TEST_SCHEMA: str | None = None
if os.environ.get("AIWEB_PG_TEST_DSN"):
    PG_TEST_SCHEMA = f"aiweb_test_{uuid.uuid4().hex[:8]}"
    os.environ["AIWEB_PG__SCHEMA_NAME"] = PG_TEST_SCHEMA


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """真实 PG 用例默认 skip；配了 DSN 才跑；CI 里可要求不 skip 而是失败。"""
    if os.environ.get("AIWEB_PG_TEST_DSN"):
        return
    if os.environ.get("AIWEB_REQUIRE_PG_TESTS") == "1":
        return
    skip = pytest.mark.skip(reason="未设 AIWEB_PG_TEST_DSN")
    for item in items:
        if "pg" in item.keywords:
            item.add_marker(skip)


def load_script(name: str, path: Path) -> ModuleType:
    """按文件路径加载脚本模块：`scripts/` 不是包，塞 sys.path 会污染后续用例的导入。"""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # 先登记再 exec：模块里的 @dataclass 解析注解时要按 __module__ 找到这个模块对象
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def pg_test_env() -> dict[str, str]:
    """把 `AIWEB_PG_TEST_DSN` 摊成 `AIWEB_PG__*` 环境变量，给子进程跑脚本用。

    为什么走子进程 + 环境变量而不是 import 脚本：CLI 脚本的验收项就是"退出码 + 打印"，
    而它们只认 Settings；进程内 import 要么绕开真实入口，要么改开发者机器的 .env 指向。
    """
    url = make_url(os.environ["AIWEB_PG_TEST_DSN"])
    parts = {"host": url.host, "user": url.username, "db": url.database}
    missing = [k for k, v in parts.items() if not v]
    if missing:
        # 静默让某一项为空，子进程就会拿 .env 里的生产库继续跑——测试打在真库上是最坏的失败模式
        raise RuntimeError(f"AIWEB_PG_TEST_DSN 缺字段：{missing}")
    return {
        **os.environ,
        "AIWEB_PG__HOST": str(url.host),
        "AIWEB_PG__PORT": str(url.port or 5432),
        "AIWEB_PG__USER": str(url.username),
        "AIWEB_PG__PASSWORD": str(url.password or ""),
        "AIWEB_PG__DATABASE": str(url.database),
        # 迁移子进程与脚本子进程都必须落在这个随机 schema 里，
        # 否则它们会继承 .env 的 aiweb，把测试写进真元数据库。
        "AIWEB_PG__SCHEMA_NAME": PG_TEST_SCHEMA,
    }


@pytest.fixture(scope="session")
def run_py(pg_test_env: dict[str, str]) -> Callable[..., CompletedProcess[str]]:
    """在"指向测试库"的环境里跑一个后端脚本，返回完整的 stdout/stderr/退出码。

    脚本类验收项（退出码、给人看的中文提示）只有在子进程里才真实：import 进来调用
    私有函数既绕开了 `__main__` 入口，也测不到打印。
    """

    def run(*argv: str, env: dict[str, str] | None = None) -> CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, *argv],
            cwd=BACKEND_ROOT,
            env={**pg_test_env, **(env or {})},
            capture_output=True,
            # 子进程输出里有中文，Windows 默认按 cp936 解码会炸在读线程里，
            # 失败信息变成 UnicodeDecodeError，真正的报错反而看不见。
            encoding="utf-8",
            errors="replace",
            # host 写错或被防火墙丢包时，TCP 是静默黑洞而不是拒绝连接；
            # 没有这个超时的话整条测试会话看起来像挂死，Ctrl+C 才能脱身。
            timeout=300,
        )

    return run


@pytest.fixture(scope="session")
def pg_migrated(run_py: Callable[..., CompletedProcess[str]]) -> Iterator[None]:
    """把真迁移打到本会话独占的随机 schema，会话结束整体 DROP。

    为什么不让 fixture 自己 `CREATE EXTENSION` + `create_all`：那等于把迁移的前置条件
    抄一份，0002 漏了 citext 或漏了哪一列，用例照样全绿，第一次 `alembic upgrade head`
    才在真库上炸。

    为什么要随机 schema：`aiweb` 里可能已经有真同步出来的表和卡片配置，用例往里
    DELETE 就是在删开发者自己的资产（docs/verification.md §2.2）。
    """
    done = run_py("-m", "alembic", "upgrade", "head")
    if done.returncode != 0:
        raise RuntimeError(f"alembic upgrade head 失败：\n{done.stdout}\n{done.stderr}")
    from app.core.db import Base

    # 子进程按随机 schema 建了表，进程内的 ORM 也必须在同一个 schema 里查。
    # 对不上就说明 conftest 顶部写环境变量的时机晚于 app.core.db 首次 import，
    # ORM 会带着 .env 里的 `aiweb` 去读写真数据。
    assert Base.metadata.schema == PG_TEST_SCHEMA, (
        f"ORM schema={Base.metadata.schema}，迁移 schema={PG_TEST_SCHEMA}"
    )
    try:
        yield
    finally:
        asyncio.run(_drop_pg_test_schema())


async def _drop_pg_test_schema() -> None:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    assert PG_TEST_SCHEMA is not None
    # 必须走 AIWEB_PG_TEST_DSN，不能用 app.core.db.create_engine()：进程内的 Settings 只被
    # 覆盖了 schema 一项，host/user/database 仍是本机 .env 那套——拿它连上去就是在
    # 真元数据库上跑 DDL（这里 DROP IF EXISTS 找不到同名 schema，会静默什么都不做）。
    engine = create_async_engine(os.environ["AIWEB_PG_TEST_DSN"], poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            # schema 名是本文件生成的 `aiweb_test_<uuid8>`，不是用户输入
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{PG_TEST_SCHEMA}" CASCADE'))
    finally:
        await engine.dispose()


@pytest.fixture
def jwt_secret(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """把签名密钥钉成测试值——测试不能跟着开发者机器上的 .env 走。

    走真实配置通道（环境变量 + 清缓存）而不是给模块属性打桩：签发、验签、响应里的
    expires_in 三处都各自读一次 Settings，桩只能盖住其中两处的来源。
    """
    from app.settings import get_settings

    secret = "t" * 48
    monkeypatch.setenv("AIWEB_JWT__SECRET", secret)
    get_settings.cache_clear()
    try:
        yield secret
    finally:
        # 缓存不清回去，后面的用例会拿着这个已撤销的环境变量继续用
        get_settings.cache_clear()
