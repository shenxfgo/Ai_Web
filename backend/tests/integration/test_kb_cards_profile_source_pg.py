"""卡片构建的**档案来源**换了，构造没换（工单 027-B / 验收 4）。

接缝：`sync_cards(..., index_profile_id=...)` 收一个已经定好的档案 id。027 之前它内部
`ensure_profile()`——"按 settings 算一套、库里没有就建一套"，那条路就是 spec §1 问题 3 的后门。
本文件证的是来源这一侧：传进来谁就挂谁，而且它从此**没有**建档案的权力。

期望值口径（文档字面）：
- §2.6：`doc_uid = md5(kind|table_uid|seq|index_profile_id)`——档案 id 拌进身份键，
  所以"换了来源"必须能在 doc_uid 上被看见；看不见就是实现把传入的值丢了。
- 工单 027 已定口径："不许留一条'没有 active 就自动建一套'的兜底"。这条断的是**库里的行数**。
- 工单 027 验收 5：改的是来源不是构造，所以 008 那三张纯函数 golden 用例一个字都不动（不在本文件）。
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.core.security import encrypt_secret, hash_password
from app.models.datasource import DataSource
from app.models.kb import KbCard, KbIndexProfile
from app.models.meta import MetaColumn, MetaDatabase, MetaTable
from app.models.user import User
from app.services.kb_service import card_doc_uid, sync_cards

pytestmark = pytest.mark.pg


async def _source(session) -> tuple[DataSource, MetaTable]:
    """一颗源 + 一张一列的表：卡片构建的最小原料。"""
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
        server_version="5.7.44-log",
        created_by=user.id,
    )
    session.add(src)
    await session.flush()
    db_row = MetaDatabase(datasource_id=src.id, catalog_name="", schema_name="ai_web_demo")
    session.add(db_row)
    await session.flush()
    tbl = MetaTable(
        datasource_id=src.id,
        database_id=db_row.id,
        catalog_name="",
        schema_name="ai_web_demo",
        table_name="order_main",
        table_type="BASE TABLE",
        comment_zh="订单主表",
    )
    session.add(tbl)
    await session.flush()
    session.add(
        MetaColumn(
            table_id=tbl.id,
            ordinal_position=1,
            column_name="id",
            data_type="bigint",
            raw_data_type="bigint",
            nullable=False,
            is_primary_key=True,
        )
    )
    await session.flush()
    return src, tbl


async def _profile(session, name: str, **kw: object) -> int:
    profile = KbIndexProfile(
        name=name,
        provider="openai_compatible",
        model="bge-large-zh-v1.5",
        dimensions=1024,
        card_template_version=1,
        **kw,  # type: ignore[arg-type]
    )
    session.add(profile)
    await session.flush()
    return int(profile.id)


async def test_卡片挂在传进来的那一套档案上_而不是settings算出来的那一套(session_factory) -> None:
    """工单 027 验收 4：`index_profile_id` 等于调用方读到的那一行 active。

    这一行档案是 `1024 / bge-large-zh-v1.5`，而测试会话钉住的 `AIWEB_EMBEDDING__DIMENSION`
    是 1536（conftest）——两者故意不同：若来源仍走 `ensure_profile`，卡片会挂到一套
    settings 算出来的新档案上，这里的 id 就对不上。
    """
    async with session_factory() as session:
        src, tbl = await _source(session)
        profile_id = await _profile(session, "bge-large-zh-v1.5@1024@tplv1", status="active")

        await sync_cards(
            session,
            index_profile_id=profile_id,
            datasource_id=src.id,
            job_id=None,
            table_ids=[tbl.id],
            dialect_name=src.kind,
            server_version=src.server_version or "",
        )

        cards = list(await session.scalars(select(KbCard).order_by(KbCard.seq)))
        assert [c.index_profile_id for c in cards] == [profile_id], cards


async def test_构建卡片不再往档案表里落行(session_factory) -> None:
    """工单 027 已定口径："不许留一条'没有 active 就自动建一套'的兜底"。

    断的是**行数**而不是返回值：`ensure_profile` 名义上幂等，跑一百遍也确实在建第一行。
    这张表在 027 之后只准由 reindex（029）写，同步链路上任何一环多出一行都是绕过激活机制。
    """
    async with session_factory() as session:
        src, tbl = await _source(session)
        profile_id = await _profile(session, "bge-large-zh-v1.5@1024@tplv1", status="active")

        await sync_cards(
            session,
            index_profile_id=profile_id,
            datasource_id=src.id,
            job_id=None,
            table_ids=[tbl.id],
            dialect_name=src.kind,
            server_version=src.server_version or "",
        )

        count = (
            await session.execute(select(func.count()).select_from(KbIndexProfile.__table__))
        ).scalar_one()
        assert count == 1, count


async def test_档案id一路拌进doc_uid_来源变了身份键跟着变(session_factory) -> None:
    """§2.6：`doc_uid = md5(kind|table_uid|seq|index_profile_id)`。

    这一条是"来源换了但算法没换"的反证：如果实现把传入的 id 只用在写 `index_profile_id`
    列上、doc_uid 里仍拌 settings 那套，那么换档案时卡片会**覆盖**旧档案的卡（同 doc_uid），
    而 §2.6 要的是"两套 profile 各一批行、可回滚"。
    """
    async with session_factory() as session:
        src, tbl = await _source(session)
        profile_id = await _profile(session, "bge-large-zh-v1.5@1024@tplv1", status="active")

        await sync_cards(
            session,
            index_profile_id=profile_id,
            datasource_id=src.id,
            job_id=None,
            table_ids=[tbl.id],
            dialect_name=src.kind,
            server_version=src.server_version or "",
        )

        cards = list(await session.scalars(select(KbCard)))
        assert len(cards) == 1
        assert cards[0].doc_uid == card_doc_uid(
            "table", tbl.table_uid, seq=0, index_profile_id=profile_id
        )
