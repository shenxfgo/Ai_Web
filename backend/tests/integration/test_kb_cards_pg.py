"""卡片落库的真跑：`meta_*` 里的一堆行 → `kb_card` 里的那些卡。

接缝选在"整段同步落库"上（而不是逐条 SQL），因为这一片真正会错的是**装配**：
少 JOIN 一张子表、注释优先级搞反、doc_uid 算错，全都表现为"卡片少一张/内容旧"，
而语句本身合法。期望值口径：kb-workflow §5（文本形状）、§6（切表与视图降权）、
§2.6（doc_uid 与幂等）、ADR-0003（未配向量端点时 embedding 为 NULL）。
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401
from app.core.security import encrypt_secret, hash_password
from app.models.datasource import DataSource
from app.models.kb import KbCard
from app.models.meta import (
    MetaColumn,
    MetaDatabase,
    MetaIndex,
    MetaIndexColumn,
    MetaRelation,
    MetaTable,
)
from app.models.user import User
from app.services.kb_service import card_doc_uid, sync_cards

pytestmark = pytest.mark.pg


def _data_source(
    owner_id: int,
    name: str = "demo-mysql",
    *,
    allow_global: bool = False,
    secret: str = "x",
) -> DataSource:
    """一条 MySQL 源的公共形状（不入 session，由调用方决定何时 flush）。

    为什么要抽出来：`secret_enc` 这一列是"永远不该出现在响应里"的那一列，各测试文件各写一份
    字面量的话，改的人只改到自己眼前那份，别处就悄悄少了越权/漏口令的断言面。
    """
    return DataSource(
        name=name,
        kind="mysql",
        host="127.0.0.1",
        port=3306,
        connect_user="aiweb_ro",
        secret_enc=encrypt_secret(secret),
        server_version="5.7.44-log",
        allow_global_access=allow_global,
        created_by=owner_id,
    )


async def _source(session: AsyncSession) -> DataSource:
    user = User(username="Owner", password_hash=hash_password("x"), role="admin")
    session.add(user)
    await session.flush()
    src = _data_source(user.id)
    session.add(src)
    await session.flush()
    return src


async def _table(
    session: AsyncSession,
    src: DataSource,
    name: str,
    *,
    table_type: str = "BASE TABLE",
    **kw: object,
) -> MetaTable:
    db_row = await session.scalar(
        select(MetaDatabase).where(
            MetaDatabase.datasource_id == src.id, MetaDatabase.schema_name == "ai_web_demo"
        )
    )
    if db_row is None:
        db_row = MetaDatabase(datasource_id=src.id, catalog_name="", schema_name="ai_web_demo")
        session.add(db_row)
        await session.flush()
    tbl = MetaTable(
        datasource_id=src.id,
        database_id=db_row.id,
        catalog_name="",
        schema_name="ai_web_demo",
        table_name=name,
        table_type=table_type,
        **kw,
    )
    session.add(tbl)
    await session.flush()
    return tbl


def _column(table: MetaTable, position: int, name: str, **kw: object) -> MetaColumn:
    return MetaColumn(
        table_id=table.id,
        ordinal_position=position,
        column_name=name,
        data_type=str(kw.pop("data_type", "bigint")),
        raw_data_type=str(kw.pop("raw_data_type", "bigint")),
        **kw,
    )


async def _cards(session: AsyncSession, src: DataSource, table_ids: list[int]) -> list[KbCard]:
    """跑一次卡片落库并把**全库**的卡片按段号读回来（用例都只喂一张表，所以全库=这张）。

    排序键里不能出现 `doc_uid`：它是 md5，而 md5 拌进了 `index_profile_id`——profile 的 id
    在同一轮测试会话里是递增的（随机 schema 全会话共用，`Identity(always=True)`），
    前面多用例多建一条 profile 就会翻转 doc_uid 的字典序。按它排序的断言等于在赌运气。
    """
    await sync_cards(
        session,
        datasource_id=src.id,
        job_id=None,
        table_ids=table_ids,
        dialect_name=src.kind,
        server_version=src.server_version or "",
    )
    rows = await session.scalars(select(KbCard).order_by(KbCard.table_id, KbCard.seq))
    return list(rows)


async def test_一张窄表落一条table卡_中文注释进正文(session_factory) -> None:
    """§5 的"一表一卡"：卡片文本取 `meta_table.comment_zh`，源库原文注释只是兜底。"""
    async with session_factory() as session:
        src = await _source(session)
        tbl = await _table(
            session, src, "order_main", comment_raw="Order header", comment_zh="订单主表"
        )
        session.add_all(
            [
                _column(tbl, 1, "id", nullable=False, is_primary_key=True, comment_zh="订单ID"),
                _column(
                    tbl,
                    2,
                    "status",
                    data_type="enum",
                    raw_data_type="enum('pending','paid')",
                    enum_values=["pending", "paid"],
                    comment_zh="订单状态",
                ),
            ]
        )
        await session.flush()

        cards = await _cards(session, src, [tbl.id])

        assert [c.kind for c in cards] == ["table"]
        assert cards[0].seq == 0
        assert cards[0].table_id == tbl.id
        assert cards[0].title == "ai_web_demo.order_main（订单主表）"
        assert "【表】ai_web_demo.order_main" in cards[0].text_md
        assert "- id bigint NOT NULL 主键: 订单ID" in cards[0].text_md
        assert "取值: pending / paid" in cards[0].text_md
        # §5 设计要点第 3 条：5.7 的方言约束必须带在卡片尾部
        assert "【方言】mysql 5.7 — 不支持 CTE" in cards[0].text_md
        assert cards[0].doc_uid == card_doc_uid(
            "table", tbl.table_uid, seq=0, index_profile_id=cards[0].index_profile_id
        )
        # ADR-0003：没配 embedding 端点，向量列 NULL，但卡片照落
        embedding = await session.scalar(select(KbCard.embedding).where(KbCard.id == cards[0].id))
        assert embedding is None
        assert cards[0].embedded_at is None


async def test_同一轮跑两遍卡片不翻倍(session_factory) -> None:
    """§2.6 的 `doc_uid UNIQUE` + 落库走 upsert：幂等这一条只能在真库里证。

    第二遍还必须**洗掉** embedding——文本重算过了，旧向量已经描述不了新文本（§5）。
    """
    async with session_factory() as session:
        src = await _source(session)
        tbl = await _table(session, src, "user", comment_zh="用户表")
        session.add(_column(tbl, 1, "id", nullable=False, is_primary_key=True))
        await session.flush()

        first = await _cards(session, src, [tbl.id])
        # 假装这一批卡已经被向量化过了：下一轮重建必须把它洗回 NULL
        await session.execute(update(KbCard).values(embedded_at=func.now()))
        await session.commit()
        second = await _cards(session, src, [tbl.id])

        assert len(second) == len(first) == 1
        assert [c.doc_uid for c in second] == [c.doc_uid for c in first]
        assert second[0].embedded_at is None


async def test_视图卡片带降权系数并降级规模行(session_factory) -> None:
    """§6 末行 + verification §1 的考点：视图卡与表卡同构，但 `meta.weight=0.6`。

    视图在 `information_schema.TABLES` 里没有 TABLE_ROWS，所以【规模】整行降级——
    这条只能在真库里验，因为 `approx_rows` 为 NULL 时模板走的是另一个分支。
    """
    async with session_factory() as session:
        src = await _source(session)
        view = await _table(session, src, "v_daily_sales", table_type="VIEW")
        session.add(_column(view, 1, "day", data_type="date", raw_data_type="date"))
        await session.flush()

        cards = await _cards(session, src, [view.id])

        assert cards[0].meta["weight"] == 0.6
        assert "【规模】行数未知（视图或未分析）" in cards[0].text_md
        assert "【表】ai_web_demo.v_daily_sales（视图）" in cards[0].text_md


async def test_索引与关系一起进主卡_目标表带库名(session_factory) -> None:
    """§5 的【索引】/【可关联】两段。

    `sub_part` 只能从 `meta_index_column` 读（父表没有这一列），关系的目标表全名只能
    从 JOIN 读——这两处正是"少 JOIN 一张子表"的出错点。
    """
    async with session_factory() as session:
        src = await _source(session)
        tbl = await _table(session, src, "order_main")
        other = await _table(session, src, "user")
        session.add_all(
            [
                _column(tbl, 1, "id", nullable=False, is_primary_key=True),
                _column(tbl, 2, "tags", data_type="varchar", raw_data_type="varchar(255)"),
                _column(tbl, 3, "user_id", data_type="bigint", raw_data_type="bigint"),
            ]
        )
        idx = MetaIndex(
            table_id=tbl.id, index_name="idx_tags", index_type="FULLTEXT", is_unique=False
        )
        pk = MetaIndex(
            table_id=tbl.id,
            index_name="PRIMARY",
            index_type="BTREE",
            is_unique=True,
            is_primary=True,
        )
        session.add_all([idx, pk])
        await session.flush()
        session.add_all(
            [
                MetaIndexColumn(index_id=idx.id, seq_in_index=1, column_name="tags", sub_part=100),
                MetaIndexColumn(index_id=pk.id, seq_in_index=1, column_name="id"),
            ]
        )
        session.add(
            MetaRelation(
                datasource_id=src.id,
                source_kind="extracted",
                from_table_id=tbl.id,
                from_column_name="user_id",
                to_table_id=other.id,
                to_column_name="id",
            )
        )
        await session.flush()

        cards = await _cards(session, src, [tbl.id])

        text_md = cards[0].text_md
        assert "- PRIMARY（唯一 BTREE）: id" in text_md
        assert "- idx_tags（FULLTEXT）: tags 前缀 100" in text_md
        assert "- user_id → ai_web_demo.user.id" in text_md
        # 工单验收 ③：`tags` 是 varchar，看着像枚举也不是枚举——渲染侧不许造取值段
        tags_line = next(line for line in text_md.splitlines() if line.startswith("- tags"))
        assert "取值" not in tags_line


async def test_宽表落主卡加切片(session_factory) -> None:
    """§6：>40 列 → 主卡 + 每 30 列一张 `table_columns`，切片各自一条 `kb_card` 行。

    doc_uid 里拌了 seq，所以三张卡的 uid 互不相同——如果算 uid 时漏了 seq，
    真库会直接撞 UNIQUE，而这正是"只有真跑才能发现"的那类错。
    """
    async with session_factory() as session:
        src = await _source(session)
        tbl = await _table(session, src, "product_stats_wide", comment_zh="商品统计宽表")
        session.add_all(
            [
                _column(
                    tbl,
                    i + 1,
                    f"c{i}",
                    data_type="decimal(18,4)",
                    raw_data_type="decimal(18,4)",
                    comment_zh=f"指标{i}的中文口径说明",
                )
                for i in range(68)
            ]
        )
        await session.flush()

        cards = await _cards(session, src, [tbl.id])

        assert [c.kind for c in cards] == ["table", "table_columns", "table_columns"]
        assert [c.seq for c in cards] == [0, 1, 2]
        assert len({c.doc_uid for c in cards}) == 3
        assert all(c.table_id == tbl.id for c in cards)


async def test_只给本轮同步到的表建卡(session_factory) -> None:
    """§5/§7 的作用域：卡片是为"这一轮确认过的表"生成的，陈旧表不许被刷新。

    传全量 table_ids 的话，一张源库早已删掉的表（`is_stale=true`）会每轮拿到一张新卡，
    而它已经不在库里了——AI 会照着它写 SQL。
    """
    async with session_factory() as session:
        src = await _source(session)
        kept = await _table(session, src, "order_main")
        stale = await _table(session, src, "gone_table", is_stale=True)
        session.add_all(
            [
                _column(kept, 1, "id", nullable=False, is_primary_key=True),
                _column(stale, 1, "id", nullable=False, is_primary_key=True),
            ]
        )
        await session.flush()

        await _cards(session, src, [kept.id])

        total = await session.scalar(select(func.count()).select_from(KbCard))
        assert total == 1
        assert (
            await session.scalar(
                select(func.count()).select_from(KbCard).where(KbCard.table_id == stale.id)
            )
            == 0
        )
