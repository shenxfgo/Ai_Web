"""全仓唯一把 `status='active'` 翻成"该用哪套档案"的那个读入口（工单 027 / ADR-0013）。

接缝：`read_active_profile()` 收一个 session，回 `ActiveProfile | None`。它是同步链路以后
和检索链路共用的**同一个**读法——027 之后"该用哪套档案"只准有这一处翻译，别处再写一条
`WHERE is_active` 就是 spec §1 问题 2 的那条安静任选。

期望值口径（文档字面，不是从实现反推）：
- ADR-0013：三态词表是 `draft|active|retired`，服务中的那一套**只有** `status='active'` 一个标记；
  `draft` 是"建好等着被激活"，`retired` 是"曾经服务过"，两者都不许被这个入口读成"当前生效"。
- 工单 027 已定口径：没有 active 档案时**不写卡**，但作业仍可以 `success`——所以这个入口必须
  能回 `None`（一个可判的缺席），而不是抛错、更不是"顺手建一套"。
- 工单 027 涉及层那句"返回 id + model/dimensions/card_template_version 三元组"：三元组就是
  §2.6 里 `profile_name()` 的三段，缺一段就没法比漂移。

`docs/verification.md` §2.2 第 2 项：这一格要真库，因为它读的是库里那一列的真实值。
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.kb import KbIndexProfile
from app.services.kb_service import read_active_profile

pytestmark = pytest.mark.pg


def _schema() -> str:
    return os.environ["AIWEB_PG__SCHEMA_NAME"]


def _profile(name: str, **kw: Any) -> KbIndexProfile:
    """造一行档案。三元组用 §2.6 的示例值：这一片测的是"读哪一行"，不是值域。"""
    return KbIndexProfile(
        name=name,
        provider="openai_compatible",
        model="text-embedding-3-small",
        dimensions=1536,
        card_template_version=1,
        **kw,
    )


async def _count(session: AsyncSession) -> int:
    return int(
        (
            await session.execute(select(func.count()).select_from(KbIndexProfile.__table__))
        ).scalar_one()
    )


async def test_有一行_active_时读出它的_id_与三元组(session_factory) -> None:
    """工单 027 验收 4 的前半：卡片要挂的那套档案，来源就是这一行。"""
    async with session_factory() as session:
        profile = _profile("text-embedding-3-small@1536@tplv1", status="active")
        session.add(profile)
        await session.flush()

        active = await read_active_profile(session)
        assert active is not None
        assert active.index_profile_id == profile.id
        assert active.triplet == ("text-embedding-3-small", 1536, 1)


async def test_只有_draft_与_retired_时回_None_不许把在建的读成生效的(session_factory) -> None:
    """ADR-0013 的三态各有用途：draft 是 029 在建的那套，retired 是 030 回滚的去向。

    这个入口若按"NOT active 里挑一行"来读，reindex 建到一半的 draft 就会被同步当成线上基准
    ——卡片写进一套永远没激活过的档案里，检索那边读不到，库里看起来却"有卡"。
    """
    async with session_factory() as session:
        session.add(_profile("building@1024@tplv1", status="draft"))
        session.add(_profile("old@1536@tplv1", status="retired"))
        await session.flush()
        assert await read_active_profile(session) is None


async def test_空库里回_None_而不是抛错(session_factory) -> None:
    """工单 027 验收 3 的地基：新库第一次同步就是"这个入口读不到东西"。

    抛错会让"从没跑过 reindex"看起来像事故，而它恰恰是 027 已定口径里那条预期行为
    （success、零卡片、不建档案）。
    """
    async with session_factory() as session:
        assert await read_active_profile(session) is None


async def test_读入口不往库里落行_两次调用前后行数相同(session_factory) -> None:
    """工单 027 的反面验收：这个入口只有读的权力。

    `ensure_profile()` 那套"读不到就建"的权力正是本工单要关掉的后门（spec §1 问题 3：
    改一行 `.env` 再跑普通同步就绕过切换机制换了全库基准）。所以断的是**库里的行数**，
    不是返回值形状——"幂等地读不到就建"跑一百遍也是在建。
    """
    async with session_factory() as session:
        assert await _count(session) == 0
        assert await read_active_profile(session) is None
        assert await read_active_profile(session) is None
        assert await _count(session) == 0

        # 反向证据：active 在场时同样不许多出一行
        session.add(_profile("serving@1536@tplv1", status="active"))
        await session.flush()
        assert await read_active_profile(session) is not None
        assert await read_active_profile(session) is not None
        assert await _count(session) == 1


async def test_读的尺子是_status_不是那条镜像列(session_factory) -> None:
    """026 的"本片只加列不改读侧"到 027 结束：读侧换尺子，`is_active` 从此只是镜像。

    用例故意造出一行 `is_active=True` 但 `status='retired'` 的不一致行：两列冲突时**以 status
    为准**（ADR-0013，031 删掉镜像列）。如果实现还在读 `is_active`，这一步就红。
    下一步把 status 改成 active、镜像列留在 true 之外（`is_active=False`），读得到才证明
    它认的是 status 而不是"两列都要为真"。

    证据边界：SQL 文本里还有没有 `is_active` 这个词，本用例证不了，它由 `grep` 的调用点清单
    背书（工单 027 验收 1，收口记录）。
    """
    async with session_factory() as session:
        session.add(_profile("mirror@1536@tplv1", status="retired", is_active=True))
        await session.flush()
        assert await read_active_profile(session) is None

        await session.execute(
            text(
                f'UPDATE "{_schema()}".kb_index_profile '
                "SET status = 'active', is_active = false WHERE name = :name"
            ),
            {"name": "mirror@1536@tplv1"},
        )
        active = await read_active_profile(session)
        assert active is not None
        assert active.triplet == ("text-embedding-3-small", 1536, 1)
