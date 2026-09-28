"""010 的全文加载：把检索给的 `table_uid` 补成 prompt 素材。

单独有这一步的原因写在工单里：009 的 `CardMatch.text_preview` 只有 200 字
（检索只需要判别力，不需要全文），而 prompt 要全文；关联边压根不在召回查询里，
得回 `meta_relation` 取。这两件事都属于"检索的输出要给 prompt 消费"，所以落在检索侧。
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401
from app.models.meta import MetaRelation
from app.services.nl2sql.retriever import load_schema_tables
from tests.integration.test_kb_cards_pg import _cards, _column, _table
from tests.integration.test_kb_search_pg import _actor, _source

pytestmark = pytest.mark.pg


async def _wide_and_target(session: AsyncSession) -> tuple:
    """一张 45 列的宽表（会切成主卡 + 切片）+ 一张被指向的目标表 + 它们之间的直连边。"""
    actor = await _actor(session)
    src = await _source(session, actor)
    wide = await _table(session, src, "order_main", comment_zh="订单主表")
    target = await _table(session, src, "customer", comment_zh="客户表")
    session.add(_column(target, 1, "id", nullable=False, is_primary_key=True, comment_zh="客户ID"))
    session.add_all(
        [
            _column(
                wide,
                i,
                f"col_{i:03d}",
                nullable=False,
                comment_zh=f"第{i}个字段，注释写得长一点好让全文与 200 字预览能被区分开",
            )
            for i in range(1, 46)
        ]
    )
    await session.flush()
    session.add(
        MetaRelation(
            datasource_id=src.id,
            source_kind="extracted",
            from_table_id=wide.id,
            from_column_name="col_001",
            to_table_id=target.id,
            to_column_name="id",
        )
    )
    await session.flush()
    await _cards(session, src, [wide.id, target.id])
    return actor, [wide.table_uid, target.table_uid]


async def test_加载给的是卡片全文而不是检索侧的二百字预览(session_factory) -> None:
    async with session_factory() as session:
        actor, uids = await _wide_and_target(session)
        tables = await load_schema_tables(session, actor=actor, table_uids=uids)

        assert [t.full_name for t in tables] == ["ai_web_demo.order_main", "ai_web_demo.customer"]
        joined = "\n".join(tables[0].segments)
        assert len(joined) > 200, "检索侧只给 200 字预览，prompt 必须是全文"
        assert "col_045" in joined, "宽表最后一列在切片里——切片没被丢掉才说明全文齐了"


async def test_顺序按传入的_table_uid_而不是数据库返回顺序(session_factory) -> None:
    """传入顺序就是检索的相关度顺序，prompt 里的【候选表】要按它排（§4.1 ④）。"""
    async with session_factory() as session:
        actor, uids = await _wide_and_target(session)
        tables = await load_schema_tables(session, actor=actor, table_uids=list(reversed(uids)))
        assert [t.full_name for t in tables] == ["ai_web_demo.customer", "ai_web_demo.order_main"]


async def test_关联边带上目标表全名与来源档位(session_factory) -> None:
    async with session_factory() as session:
        actor, uids = await _wide_and_target(session)
        tables = await load_schema_tables(session, actor=actor, table_uids=uids)
        rel = tables[0].relations[0]
        assert rel.from_column == "col_001"
        assert rel.to_table_full == "ai_web_demo.customer"
        assert rel.to_column == "id"
        assert rel.kind == "extracted"
        # 真外键不打折：confidence 列的 server_default 是 1.0（§2.4）
        assert rel.confidence == 1.0
        # 目标表自己不该带上这条边（边是从 from_table 出发的直连边，反向遍历归工单 014）
        assert tables[1].relations == ()
