"""010 的全文加载：把检索给的 `table_uid` 补成 prompt 素材。

单独有这一步的原因写在工单里：009 的 `CardMatch.text_preview` 只有 200 字
（检索只需要判别力，不需要全文），而 prompt 要全文。

这里**不查关联边了**（010 时查过）：【可 JOIN】的唯一来源是 `join_graph`，
真库那一半钉在 `tests/integration/test_join_graph_rows.py`。留着两处事实源的话，
`is_stale` 与 inferred ≥0.8 两道门槛就得各写一份，而两边的口径并不一样。
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


async def test_装载侧不带关联边而节点身份是_table_uid(session_factory) -> None:
    """库里那条边还在，这里一条都不带（工单 014）：【可 JOIN】的唯一来源是 `join_graph`。

    两句各钉一半：
    - `relations` 没了 —— 两处各有一份边事实源时，`is_stale` 与 inferred ≥0.8 两道门槛就得写
      两份，而两份口径本来就不一样（010 那份 SQL 连 `is_stale` 都不看）。
    - `uid` 就是 `meta_table.table_uid` —— 它是 `build_prompt` 判"这行 JOIN 引用的表还在不在"
      唯一能对上号的身份：按 `full_name` 对不行，两张源库同名的表会同名。
    """
    async with session_factory() as session:
        actor, uids = await _wide_and_target(session)
        tables = await load_schema_tables(session, actor=actor, table_uids=uids)

        assert [t.uid for t in tables] == uids
        assert not any(hasattr(t, "relations") for t in tables)
