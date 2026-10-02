"""那条镜像列的**一整条链**在真库上走一遍：0007 出生 → 0008 归置 → 0009 出清 → 回滚 → 再出清。

工单 031 验收 7 要的正是"upgrade/downgrade 在真库各跑一次"，而这里把它写成用例而不是实录，
理由有两条：`is_active` 已经没有任何读侧了（027 关写侧、031 关读侧），它留在库里的唯一后果
就是"下一版迁移 referencing 一个没人维护的布尔"，而这种事只有跑过链才知道；实录则每次收口都要
人重跑一遍、且没人保证下次还在。

**为什么要另起一个 schema**：会话共用的那个随机 schema 是 `pg_migrated` 一次性 `upgrade head`
建出来的，链条在它脚下已经跑完了，中途那一格（`is_active` 还在、归置刚发生）在里面根本不存在。
要看见归置真的发生，就得在一个空 schema 里**把链条走回去**：`upgrade 0007` → 摆两行档案 →
`upgrade 0008`。这一步取代的是 026 那条 `test_归置口径那句_update_只跟着_is_active_走`——
它手工重跑了迁移用的那句 SQL，而这里跑的是迁移本体。

期望值口径（都是文档字面，不是从迁移代码反推）：
- 026 / ADR-0013 的归置口径：`is_active=true` → `active`，其余 → `retired`（**不是 `draft`**）。
- 工单 031 验收 7：0009 之后 `kb_index_profile` 不再有 `is_active` 这一列。
- 工单 026 的双写口径反过来用就是回滚的形状：`is_active` 跟着 `status` 的字面走，
  所以 0008 那一世看到的应当是 `active` 那行为真。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

pytestmark = pytest.mark.pg

# 两行的名字与 026 那条用例同源：一行的镜像列是真（正在服务），一行是假（历史）。
_SERVING = "serving@1536@tplv1"
_HISTORY = "history@1536@tplv0"


async def _fetch(sql: str, **params: Any) -> list[tuple[Any, ...]]:
    """必须走 `AIWEB_PG_TEST_DSN`：进程内的 Settings 只被覆盖了 schema 一项，
    拿 `app.core.db` 的引擎去连就是在真元数据库 `aiweb` 上跑 DDL。"""
    engine = create_async_engine(os.environ["AIWEB_PG_TEST_DSN"], poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            return [(tuple(r)) for r in (await conn.execute(text(sql), params)).all()]
    finally:
        await engine.dispose()


async def _execute(sql: str) -> None:
    engine = create_async_engine(os.environ["AIWEB_PG_TEST_DSN"], poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(sql))
    finally:
        await engine.dispose()


def _alembic(run_py: Any, schema: str, *targets: str) -> None:
    """跑一次 alembic，非零就把两边的输出一起摊开——链跑断的时候只看退出码没有用。

    `env` 只盖 schema 那一项：`run_py` 自己会把它摊在 `pg_test_env` 之上（host/user/database
    仍然来自 `AIWEB_PG_TEST_DSN`），所以"必须落在测试库"那句话由夹具负责，不由这里复制一遍。
    而 schema 必须盖：不盖就是继承 `.env` 里的 `aiweb`，那条链会在真元数据库上把人家的表改没。
    """
    done: subprocess.CompletedProcess[str] = run_py(
        "-m", "alembic", *targets, env={"AIWEB_PG__SCHEMA_NAME": schema}
    )
    assert done.returncode == 0, f"{' '.join(targets)} 失败：\n{done.stdout}\n{done.stderr}"


@pytest.fixture
def chain_schema(run_py: Any) -> Iterator[str]:
    """一条空的、除了名字不与任何东西相干的 schema。

    schema 本身交给 `alembic/env.py` 的 `_ensure_schema()` 建：它比任何用例都早，
    自己先建再迁移就是两套"什么时候这个 schema 存在"的口径。用例只负责收尾删掉它。
    """
    name = f"aiweb_test_chain_{uuid.uuid4().hex[:8]}"
    try:
        yield name
    finally:
        asyncio.run(_execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE'))


def _columns(schema: str) -> set[str]:
    return {
        str(r[0])
        for r in asyncio.run(
            _fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = :s AND table_name = 'kb_index_profile'",
                s=schema,
            )
        )
    }


def _status_map(schema: str) -> dict[str, str]:
    return {
        str(r[0]): str(r[1])
        for r in asyncio.run(
            _fetch(f'SELECT name, status FROM "{schema}".kb_index_profile ORDER BY name')
        )
    }


def _mirror_map(schema: str) -> dict[str, bool]:
    return {
        str(r[0]): bool(r[1])
        for r in asyncio.run(
            _fetch(
                f'SELECT name, is_active FROM "{schema}".kb_index_profile ORDER BY name',
            )
        )
    }


def test_整条扩收链在真库上走通_含回滚再走一遍(run_py: Any, chain_schema: str) -> None:
    """0007（镜像列在场）→ 0008（三态 + 归置）→ 0009（列没了）→ downgrade（列按 status 回来）。

    每一格都不可替代：
    - 第 ① 步之后没有第 ② 步，就只是"新库长什么样"，026 的归置口径没被跑过；
    - 第 ③ 步是验收 7 的正身；
    - 第 ④ 步钉的是**回滚不抹现状**：`downgrade` 只 `ADD COLUMN … DEFAULT false` 也能过第 ③ 步，
      但那一世的服务就查不到任何在服务的档案了（第 ⑤ 步再往前走一次，证明收缩不是单向门）。
    """
    # ① 走到 0007：`kb_index_profile` 已存在（0005 建的），而 `status` 还没有。
    _alembic(run_py, chain_schema, "upgrade", "0007")
    assert "status" not in _columns(chain_schema), "0008 之前就冒出 status，归置这一格就没内容了"

    asyncio.run(
        _execute(
            f'INSERT INTO "{chain_schema}".kb_index_profile '
            "(name, provider, model, dimensions, card_template_version, is_active) VALUES "
            f"('{_SERVING}', 'openai_compatible', 'text-embedding-3-small', 1536, 1, true), "
            f"('{_HISTORY}', 'openai_compatible', 'text-embedding-3-small', 1536, 0, false)"
        )
    )

    # ② 0008：加列 + 归置 + CHECK + partial unique。跑的是迁移本体，不是重抄一遍那句 SQL。
    _alembic(run_py, chain_schema, "upgrade", "0008")
    assert _status_map(chain_schema) == {_HISTORY: "retired", _SERVING: "active"}, (
        "026 写死的归置口径：镜像列为真的那行是 active，其余是 retired——不是 draft"
    )

    # ③ 0009（head）：那一列出清。
    _alembic(run_py, chain_schema, "upgrade", "head")
    assert "is_active" not in _columns(chain_schema), "工单 031 验收 7：收缩那一版没把列删掉"
    assert "status" in _columns(chain_schema), "读侧唯一的尺子不该跟着一起走"

    # ④ 回滚到 0008：列回来了，而且**内容按 status 补**，不是一色 false。
    _alembic(run_py, chain_schema, "downgrade", "0008")
    assert _mirror_map(chain_schema) == {_HISTORY: False, _SERVING: True}, (
        "回滚把镜像列的内容抹成假的，等于让 0008 那一世读不到任何在服务的档案"
    )

    # ⑤ 再往前走一次：收缩不是单向门，链可以从任意一格重进。
    _alembic(run_py, chain_schema, "upgrade", "head")
    assert "is_active" not in _columns(chain_schema)
