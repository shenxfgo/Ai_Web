"""0008 落进真库之后的三件事：至多一行 active 由**库**背书、词表由那条 CHECK 圈住、
reindex 的载体列可用。

期望值口径（全部是文档字面，不是从模型代码反推的）：
- ADR-0013 + spec 故事 1：`status IN ('draft','active','retired')` 与一条 partial unique
  `WHERE status = 'active'`。用例断的是 **IntegrityError**，不是某个 Python 分支——
  "只有一个 active"如果住在代码里，两个进程各读各的就是 spec §1 问题 2 那条安静任选。
- ADR-0011 + 工单 026/029：`sync_jobs.reindex_profile_id` 可空、外键 SET NULL。

026 那一格原本还在这里验"归置口径真的跟着 `is_active`"，它在 031 的收缩之后搬去了
`test_kb_is_active_shrink_pg.py`：`is_active` 这一列在 head 库里已经不存在，用例造不出前提，
而搬家以后的证据反而更硬——跑的是迁移本体，不是重抄一遍那句 SQL。

`docs/verification.md` §2.2 第 2 项：迁移剩下的那 10% 只能在真 PG 上验。
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from sqlalchemy import CheckConstraint, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401
from app.core.db import Base
from app.models.kb import KbIndexProfile
from app.models.meta import SyncJob

pytestmark = pytest.mark.pg


def _schema() -> str:
    return os.environ["AIWEB_PG__SCHEMA_NAME"]


def _profile(name: str, **kw: Any) -> KbIndexProfile:
    """造一行档案。`dimensions` 固定 1536：这一片测的是状态机，不是向量列。"""
    return KbIndexProfile(
        name=name,
        provider="openai_compatible",
        model="text-embedding-3-small",
        dimensions=1536,
        card_template_version=1,
        **kw,
    )


async def _statuses(session: AsyncSession) -> dict[str, str]:
    rows = (
        await session.execute(
            text(f'SELECT name, status FROM "{_schema()}".kb_index_profile ORDER BY name')
        )
    ).all()
    return dict(rows)


async def _source_id(session: AsyncSession) -> int:
    """一颗作业必须有源：`sync_jobs.datasource_id` 是 NOT NULL 外键。"""
    from app.core.security import encrypt_secret, hash_password
    from app.models.datasource import DataSource
    from app.models.user import User

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


async def test_第二行_active_被库拒_一条也不许靠代码拦(session_factory) -> None:
    """spec 故事 1 的正面：唯一的 active 由 `ux_kb_index_profile_active` 拒，错误里有索引名。

    为什么这条必须在真库上跑：ORM 层看不出这条不变式存在（它不是 NOT NULL 也不是 UNIQUE 列），
    代码 review 只能验"写了没"，验不了"PG 认不认"。
    """
    async with session_factory() as session:
        session.add(_profile("first@1536@tplv1", status="active"))
        await session.flush()
        session.add(_profile("second@1536@tplv1", status="active"))
        with pytest.raises(IntegrityError) as exc:
            await session.flush()
        assert "ux_kb_index_profile_active" in str(exc.value), str(exc.value)


async def test_draft_与_retired_可以并存_谓词只圈_active(session_factory) -> None:
    """partial unique 的"partial"就是它的用途：重建期要同时有 1 个 active 和 1 个 draft。

    反过来说，如果这条索引写成整表 unique(status)，故事 3（reindex 建 draft、active 不动）
    在库里就根本不可能成立。
    """
    async with session_factory() as session:
        session.add(_profile("serving@1536@tplv1", status="active"))
        session.add(_profile("building@1024@tplv1", status="draft"))
        session.add(_profile("old@1536@tplv0", status="retired"))
        await session.flush()
        assert await _statuses(session) == {
            "building@1024@tplv1": "draft",
            "old@1536@tplv0": "retired",
            "serving@1536@tplv1": "active",
        }


async def test_词表之外的_status_由库里那条_check_拒(session_factory) -> None:
    """ADR-0013 的词表是三档。第四档（比如 `pending`）不是"以后再说"，是当场被拒。"""
    async with session_factory() as session:
        session.add(_profile("weird@1536@tplv1", status="pending"))
        with pytest.raises(IntegrityError) as exc:
            await session.flush()
        assert "ck_kb_index_profile_status_allowed" in str(exc.value), str(exc.value)


async def test_没写_status_的档案落库就是_draft(session_factory) -> None:
    """029 的出生默认：reindex 造的就是 draft，调用方不写状态也不该掉进 active。

    默认值方向错了会怎样：新档案一插就 active，等于绕过故事 6 的"必须显式激活"，而线上正在
    服务的那一套当场被顶掉（要么撞 partial unique，要么在有 active 之前先悄悄成为唯一基准）。
    """
    async with session_factory() as session:
        profile = _profile("newcomer@1536@tplv1")
        session.add(profile)
        await session.flush()
        await session.refresh(profile)
        assert profile.status == "draft"


async def test_库里_check_约束的名字与_orm_渲染的那一个逐字相同(session_factory) -> None:
    """本仓库没接 `alembic check`（spec §7），所以"迁移 ↔ ORM 漂移"这一格只能自己钉。

    钉**名字**而不是钉条件：条件红了顶多说"值域改了啊"，名字红了才是真问题——库里的约束
    反查不到，报错文案、pg 里的手工排查、以后想 DROP 它的那一步，全都按名字说话。
    0008 第一版就栽在这儿：alembic 会把 CHECK 名再套一次 `naming_convention`，
    传全名 `ck_kb_index_profile_status_allowed` 落库就成了双前缀。库里现在确实有两条双前缀的
    （0006/0007 留下的），所以这一格只对 0008 的表立，不顺手扩到全库。
    """
    orm_names = {
        str(c.name)
        for c in Base.metadata.tables[f"{_schema()}.kb_index_profile"].constraints
        if isinstance(c, CheckConstraint)
    }
    async with session_factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT conname FROM pg_constraint "
                    "WHERE conrelid = (:s || '.kb_index_profile')::regclass AND contype = 'c'"
                ),
                {"s": _schema()},
            )
        ).all()
    in_db = {name for (name,) in rows}
    assert in_db == {"ck_kb_index_profile_status_allowed"}, in_db
    assert orm_names == in_db, f"ORM 侧的名字变了：{orm_names} vs {in_db}"


async def test_reindex_的载体列在真库里可空_且不许指向不存在的档案(session_factory) -> None:
    """ADR-0011 的落点：普通同步的作业行这一列就是 NULL；写了档案 id 就得真的存在。

    NULL 判定是 029 之后"这颗作业换不换档案"的分岔依据，所以它必须能在库里被看出来；
    而外键如果漏了，029 就会把作业挂到一个已删除的 draft 上，回放时对不上。

    两行都写 status='success'：`ux_sync_running` 只圈 pending/running，
    让用例红在该上的话，报错就跟"载体列有没有外键"这件事毫无关系了。
    """
    async with session_factory() as session:
        ds = await _source_id(session)
        plain = SyncJob(datasource_id=ds, status="success", phase="done")
        session.add(plain)
        await session.flush()
        assert plain.reindex_profile_id is None

        session.add(
            SyncJob(datasource_id=ds, status="success", phase="done", reindex_profile_id=999_999)
        )
        with pytest.raises(IntegrityError) as exc:
            await session.flush()
        assert "fk_sync_jobs_reindex_profile_id" in str(exc.value), str(exc.value)
