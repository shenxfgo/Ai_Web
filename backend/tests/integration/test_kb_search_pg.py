"""`LikeRetriever.search()` 的真跑：一句中文问句 → 候选表。

接缝选在 `search()` 上（工单 009 的"接口层"就是它），因为这一片真正会错的是**装配**：
term 拼进 SQL 的方式、profile 隔离、权限过滤、召回窗口，每一处出错都表现为
"少一张表 / 多一张别人的表 / 分数对不上"，而单条 SQL 本身都是合法的。

期望值口径：`docs/architecture.md` §5.1 L1（排序主键=命中的不同词数，次键=命中列数；
as-built 0009 的口径修正）、§5.2 (2)(4)(5)（三路 max、同表多卡 boost、权限过滤）、
§4.1 的"检索为空 → 不生成 SQL"。播种复用 008 的卡片落库，
这样卡片文本是渲染器产的同一份东西，而不是我给检索器手搓的假料。
"""

from __future__ import annotations

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401
from app.core.security import hash_password
from app.models.datasource import DataSource
from app.models.kb import KbIndexProfile
from app.models.meta import MetaTable
from app.models.user import User
from app.services.nl2sql.retriever import LikeRetriever
from tests.integration.test_kb_cards_pg import _cards, _column, _data_source, _table

pytestmark = pytest.mark.pg


async def _actor(session: AsyncSession, username: str = "Seeker") -> User:
    user = User(username=username, password_hash=hash_password("x"), role="member")
    session.add(user)
    await session.flush()
    return user


async def _source(session: AsyncSession, actor: User, name: str = "demo-mysql") -> DataSource:
    src = _data_source(actor.id, name)
    session.add(src)
    await session.flush()
    return src


async def _seed_order_main(session: AsyncSession, src: DataSource) -> MetaTable:
    tbl = await _table(session, src, "order_main", comment_zh="订单主表")
    session.add_all(
        [
            _column(tbl, 1, "id"),
            _column(tbl, 2, "amount", comment_zh="订单金额"),
        ]
    )
    await session.flush()
    # 复用 008 的落卡：卡片正文由渲染器产，不给检索器手搓假料。返回值这里用不上。
    await _cards(session, src, [tbl.id])
    return tbl


async def test_一句中文问句命中持有该列的表并给出命中理由(session_factory) -> None:
    async with session_factory() as session:
        actor = await _actor(session)
        src = await _source(session, actor)
        tbl = await _seed_order_main(session, src)
        await session.commit()

        found = await LikeRetriever().search(session, actor=actor, question="订单金额是多少", k=5)

    assert [r.table_uid for r in found] == [tbl.table_uid]
    only = found[0]
    # 三个词全对在同一列上：词数按"不同的词"去重、列数按"不同的列"去重，两个数各算各的。
    assert only.matched_term_count == 3
    assert only.matched_column_count == 1
    assert {(h.column_name, h.field) for h in only.hits} == {("amount", "comment_zh")}
    # 汉字段 `订单金额是多少` 有 7 个字，超过整段当词的阈值，只发二字滑窗。
    # 三个滑窗词全对上同一列：词数按**不同的词**去重、列数按**不同的列**去重，两个数是各算各的。
    assert {h.term for h in only.hits} == {"订单", "单金", "金额"}
    assert only.matched_term_count == 3
    # 标题形状来自 metadata-model §2.6：`db.orders（订单表）`
    assert only.title == "ai_web_demo.order_main（订单主表）"
    assert only.score_kw > 0.0


async def test_问句里没有任何词命中时返回空列表而不是猜一张表(session_factory) -> None:
    """§4.1 的 fail-fast 依赖这一条：检索空 → 不生成 SQL（判据在 013 用）。"""
    async with session_factory() as session:
        actor = await _actor(session)
        src = await _source(session, actor)
        await _seed_order_main(session, src)
        await session.commit()

        found = await LikeRetriever().search(session, actor=actor, question="卫星云图降雨量", k=5)

    assert found == []


async def test_空问句不查库直接返回空(session_factory) -> None:
    # 切词后一个词都不剩（全是标点或单字），就不该发 SQL 出去
    async with session_factory() as session:
        actor = await _actor(session)
        found = await LikeRetriever().search(session, actor=actor, question="？！", k=5)
    assert found == []


async def test_别人私有的源里的表不进候选(session_factory) -> None:
    """§5.2 (5)：权限过滤在召回这一层就做，不等 010 拼 prompt 时再补。

    别人那个源里放一张**更能命中**的表（注释一字不差就是问句），如果它出现在候选里，
    说明 SQL 压根没带 datasource_id 条件——这种越权不会报错，只会安静地多返回一行。
    """
    async with session_factory() as session:
        mine = await _actor(session, "Mine")
        other = await _actor(session, "Other")
        my_src = await _source(session, mine, "my-demo")
        their_src = await _source(session, other, "their-demo")

        tbl = await _table(session, my_src, "order_main")
        session.add(_column(tbl, 1, "amount", comment_zh="待结算金额"))
        await session.flush()
        await _cards(session, my_src, [tbl.id])

        theirs = await _table(session, their_src, "ledger")
        session.add(_column(theirs, 1, "amount", comment_zh="订单金额"))
        await session.flush()
        await _cards(session, their_src, [theirs.id])

        await session.commit()

        found = await LikeRetriever().search(session, actor=mine, question="订单金额是多少", k=5)

    # 别人那张表能被 `订单金额` 一字不差命中，分数更高；它没出现在候选里才是重点。
    assert [(r.datasource_id, r.table_uid) for r in found] == [(my_src.id, tbl.table_uid)]


async def test_命中词数多的表排在前面(session_factory) -> None:
    """§5.1 L1 的排序主键是"命中的不同词数"（as-built 0009），列数只是次键。

    `payment_record` 的卡片文本里"金额"出现得更密（表注释和列注释都有），单看分数它可能反超；
    但只有 `order_main` 被多个词各自对上。真库上跑出这个次序才算数——纯函数那一关验的是装配，
    这一关验的是 SQL 召回回来的卡确实进了同一个排序器。
    """
    async with session_factory() as session:
        actor = await _actor(session)
        src = await _source(session, actor)

        orders = await _table(session, src, "order_main")
        session.add_all(
            [
                _column(orders, 1, "amount", comment_zh="订单金额（元，优惠前）"),
                _column(orders, 2, "status", comment_zh="订单状态：待付款、已付款"),
            ]
        )
        await session.flush()

        payments = await _table(session, src, "payment_record", comment_zh="支付金额流水表")
        session.add(_column(payments, 1, "amount", comment_zh="本次实付金额（元）"))
        await session.flush()

        await _cards(session, src, [orders.id, payments.id])
        await session.commit()

        found = await LikeRetriever().search(session, actor=actor, question="订单金额", k=5)

    assert [r.table_uid for r in found] == [orders.table_uid, payments.table_uid]
    # 问句"订单金额"是 4 字 CJK 整段 → 词 = {订单金额, 订单, 单金, 金额}。
    # amount 的注释含"订单金额"，四个词全对上；payment_record 只有"金额"一个词对上。
    assert [r.matched_term_count for r in found] == [4, 1]
    assert [r.matched_column_count for r in found] == [2, 1]


async def test_宽表用命中列数换词数换不来头名(session_factory) -> None:
    """§5.1 那条 as-built 注在真 SQL 召回上的回归位。

    `stats_wide` 拿 6 列去对，全是同一个词 `金额`；`order_main` 只有 2 列对上，
    但对上了 4 个不同的词。真跑一遍才算数：ILIKE 那一路给的分数是"每词取最大再求和"，
    词越杂分数越高，可头名要由**词数**决定，不能由列数或分数决定。
    """
    async with session_factory() as session:
        actor = await _actor(session)
        src = await _source(session, actor)

        wide = await _table(session, src, "stats_wide", comment_zh="统计宽表")
        session.add_all(
            [
                _column(wide, i, f"refund_amt_{n}d", comment_zh="退款金额（元）")
                for i, n in enumerate((1, 7, 14, 30, 60, 90), start=1)
            ]
        )
        orders = await _table(session, src, "order_main", comment_zh="订单主表")
        session.add_all(
            [
                _column(orders, 1, "amount", comment_zh="订单金额（元，优惠前）"),
                _column(orders, 2, "status", comment_zh="订单状态：待付款、已付款"),
            ]
        )
        await session.flush()
        await _cards(session, src, [wide.id, orders.id])
        await session.commit()

        found = await LikeRetriever().search(session, actor=actor, question="订单金额", k=5)

    assert [r.table_uid for r in found] == [orders.table_uid, wide.table_uid]
    assert [r.matched_term_count for r in found] == [4, 1]
    # 旧口径（主键=命中列数）就是在这里把 6 列的宽表提到了头名
    assert [r.matched_column_count for r in found] == [2, 6]


async def test_只召回当前生效profile的卡(session_factory) -> None:
    """§2.6 末：换 profile = 全量重建，新旧两套混在一个索引里不可解释。

    做法是把老 profile 摘掉 active，再灌一套**空的**新 profile 顶上。此时库里"卡片有、
    生效 profile 也有"，如果召回没带 `index_profile_id` 条件，老卡就会照样返回——
    而"没有生效 profile 时返回空"这条更弱的判据根本分不出这两种写法。
    """
    async with session_factory() as session:
        actor = await _actor(session)
        src = await _source(session, actor)
        await _seed_order_main(session, src)

        await session.execute(
            update(KbIndexProfile).where(KbIndexProfile.is_active).values(is_active=False)
        )
        session.add(
            KbIndexProfile(
                name="text-embedding-3-small@1536@tplv9",
                provider="openai_compatible",
                model="text-embedding-3-small",
                dimensions=1536,
                card_template_version=9,
                is_active=True,
            )
        )
        await session.commit()

        found = await LikeRetriever().search(session, actor=actor, question="订单金额是多少", k=5)

    assert found == []
