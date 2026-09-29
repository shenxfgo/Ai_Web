"""JOIN 图的**真库那一半**：读 `meta_relation` 的那三条查询（工单 014）。

①~⑦ 的口径全钉在 `tests/unit/test_join_graph.py` 的手算图上（verification §2.1 本行要的
就是"扩展逻辑不依赖真库"）。这里只钉纯函数钉不住的四件事：

1. 边是怎么从两张 `meta_table` 连出来的——节点 id 是 `table_uid` 而不是 `table_id`，
   方向按 `from_*`/`to_*` 存，档位与 confidence 一起带回来；
2. `database_id` 的收窄（拍板 3"不跨库建图"落到 SQL 上的形状）与 `is_stale` 的排除；
3. 缓存按 `(datasource_id, database_id)` 分键、同步终局后失效（拍板 3）；
4. 演示库的**形状**：`payment_record ↔ product` 要穿两张桥表、`category` 有自环、
   `user_activity_log → product` 是一条 1.000 的推断边。

表名照演示库的真表名，但行是用例自己插的：真同步跑没跑过、演示库现在到底有几张表，
都不该决定这里的红绿（工单 014 的"真库那一半"要的是查询形状，不是演示库的当前状态）。
期望值口径：`docs/architecture.md` §5.3 + 工单 014 的「开工前拍板」。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401
from app.models.datasource import DataSource
from app.models.meta import MetaDatabase, MetaRelation, MetaTable
from app.services.nl2sql import join_graph as jg
from tests.integration.test_kb_search_pg import _actor, _source

pytestmark = pytest.mark.pg

DEMO_SCHEMA = "ai_web_demo"

# 门槛 8 与 hops 2 出自 architecture §5.3 与开工前拍板，也是 `Settings` 的两个默认值。
# 用例从 Settings 引默认值等于断"它等于它"，所以这里写字面量（单测那份同一口径）。
_MAX_DEGREE = 8
_HOPS = 2


@pytest.fixture(autouse=True)
def _empty_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """图缓存是模块级全局：不清掉的话，前一条用例建的图会让后一条看不见自己刚插的边。

    每条用例本来就拿到新的 `datasource_id`（Identity 只增不减），键天然不会撞——
    但"不撞"是靠运气，清掉才是靠断言。
    """
    monkeypatch.setattr(jg, "_CACHE", {})


async def _table(
    session: AsyncSession,
    src: DataSource,
    name: str,
    *,
    schema: str = DEMO_SCHEMA,
    stale: bool = False,
) -> MetaTable:
    """一张表 + 它所属的库行（`database_id` 是这一片的新变量，所以库必须能换）。"""
    db_row = await session.scalar(
        select(MetaDatabase).where(
            MetaDatabase.datasource_id == src.id, MetaDatabase.schema_name == schema
        )
    )
    if db_row is None:
        db_row = MetaDatabase(datasource_id=src.id, catalog_name="", schema_name=schema)
        session.add(db_row)
        await session.flush()
    tbl = MetaTable(
        datasource_id=src.id,
        database_id=db_row.id,
        catalog_name="",
        schema_name=schema,
        table_name=name,
        table_type="BASE TABLE",
        is_stale=stale,
    )
    session.add(tbl)
    await session.flush()
    return tbl


def _relation(
    src: DataSource,
    from_tbl: MetaTable,
    from_column: str,
    to_tbl: MetaTable,
    to_column: str,
    *,
    kind: str = "extracted",
    confidence: float | None = None,
) -> MetaRelation:
    kw: dict[str, float | None] = {} if confidence is None else {"confidence": confidence}
    return MetaRelation(
        datasource_id=src.id,
        source_kind=kind,
        from_table_id=from_tbl.id,
        from_column_name=from_column,
        to_table_id=to_tbl.id,
        to_column_name=to_column,
        **kw,
    )


async def test_一条边读出来带方向档位和_uid_当节点(session_factory) -> None:
    async with session_factory() as session:
        actor = await _actor(session)
        src = await _source(session, actor)
        child = await _table(session, src, "order_item")
        parent = await _table(session, src, "order_main")
        session.add(_relation(src, child, "order_id", parent, "id"))
        await session.flush()

        edges = await jg.load_relations(
            session, datasource_id=src.id, database_id=child.database_id
        )

        assert [(e.from_uid, e.from_column, e.to_uid, e.to_column) for e in edges] == [
            (child.table_uid, "order_id", parent.table_uid, "id")
        ], "节点 id 必须是 table_uid：跨源/跨库它才唯一，table_id 只在元数据库内唯一"
        assert [(e.source_kind, e.confidence) for e in edges] == [("extracted", 1.0)], (
            "confidence 的默认值 1.0 来自 §2.4 的列默认值，不是建边时代码填的"
        )
        # catalog 在 MySQL 恒为空串，名字不能打头（`.ai_web_demo.order_item` 会被模型照抄进 SQL）
        assert [(e.from_name, e.to_name) for e in edges] == [
            ("ai_web_demo.order_item", "ai_web_demo.order_main")
        ]


async def test_跨库的边两端都不进来(session_factory) -> None:
    """拍板 3"不跨库建图"落到 SQL 上的形状：`database_id` 要卡两端。

    只卡 from 端时，"from 在本库、to 在别的库"的那条边会进图。它在图上是一条能走的边，
    但目标表的卡片不在同一批候选里、桥表补查也补不到，最后渲染进 prompt 的是一条执行不了的 JOIN。
    真外键恒在同库，所以这条边只可能来自人工补录的逻辑外键——而那正是它最像能用的时候。
    """
    async with session_factory() as session:
        actor = await _actor(session)
        src = await _source(session, actor)
        demo_item = await _table(session, src, "order_item")
        demo_parent = await _table(session, src, "order_main")
        report_parent = await _table(session, src, "order_main", schema="reporting")
        session.add_all(
            [
                _relation(src, demo_item, "order_id", demo_parent, "id"),
                _relation(src, demo_item, "report_id", report_parent, "id"),
            ]
        )
        await session.flush()

        in_demo = await jg.load_relations(
            session, datasource_id=src.id, database_id=demo_item.database_id
        )
        assert [(e.from_uid, e.to_uid) for e in in_demo] == [
            (demo_item.table_uid, demo_parent.table_uid)
        ], "对端在 reporting 库的那条边不该出现在 ai_web_demo 的图里"

        in_reporting = await jg.load_relations(
            session, datasource_id=src.id, database_id=report_parent.database_id
        )
        assert in_reporting == [], "起点在 ai_web_demo 的那条边也不该出现在 reporting 的图里"


async def test_陈旧表当断点时整条边不进来(session_factory) -> None:
    """`is_stale` 的排除与 `kb_service.sync_cards` 同一条理由：源库里已经没有实体了。

    两端各测一次——只筛 from 端会漏掉"表被删了但别人还指着它"的那种边，而那种边恰恰最多
    （被删的是父表时，所有子表的外键都指向了空气）。
    """
    async with session_factory() as session:
        actor = await _actor(session)
        src = await _source(session, actor)
        child = await _table(session, src, "order_item")
        gone_parent = await _table(session, src, "coupon", stale=True)
        gone_child = await _table(session, src, "draft_row", stale=True)
        session.add_all(
            [
                _relation(src, child, "coupon_id", gone_parent, "id"),
                _relation(src, gone_child, "order_id", child, "id"),
            ]
        )
        await session.flush()

        edges = await jg.load_relations(
            session, datasource_id=src.id, database_id=child.database_id
        )
        assert edges == [], f"陈旧表还在当边的某一端：{edges}"


async def _demo_chain(session: AsyncSession, src: DataSource) -> dict[str, MetaTable]:
    """按 `scripts/init_demo_mysql.sql` 的真外键拓扑插五行。

    表名与列名照抄建库脚本（`fk_payment_order` / `fk_item_order` / `fk_item_product` 三条约束），
    这样这一片在真演示库上跑出来的形状与这里插的是一回事；但不跑真同步——
    演示库现在到底有几张表、同步过没有，不该决定这里的红绿。
    """
    tables = {
        name: await _table(session, src, name)
        for name in ("payment_record", "order_main", "order_item", "product")
    }
    session.add_all(
        [
            _relation(src, tables["payment_record"], "order_id", tables["order_main"], "id"),
            _relation(src, tables["order_item"], "order_id", tables["order_main"], "id"),
            _relation(src, tables["order_item"], "product_id", tables["product"], "id"),
        ]
    )
    await session.flush()
    return tables


async def test_演示库形状_payment_record_与_product_要穿两张桥表(session_factory) -> None:
    """`expansion_for` 的整条真链路：查库 → 按库分组 → 建图 → 扩展（拍板 3 的 hops 数桥表）。

    只召回 `payment_record` 和 `product` 时，`order_main`、`order_item` 两张桥表要靠
    `Expansion.bridge_uids` 交给 pipeline 补查卡片（拍板 9），所以那一份清单必须是 uid。
    """
    async with session_factory() as session:
        actor = await _actor(session)
        src = await _source(session, actor)
        t = await _demo_chain(session, src)

        result = await jg.expansion_for(
            session,
            table_uids=[t["payment_record"].table_uid, t["product"].table_uid],
            hops=_HOPS,
            max_degree=_MAX_DEGREE,
        )

        assert len(result.paths) == 1
        path = result.paths[0]
        assert path.uids == (
            t["payment_record"].table_uid,
            t["order_main"].table_uid,
            t["order_item"].table_uid,
            t["product"].table_uid,
        )
        # 中间那一跳是逆着存的方向走的，列名不许跟着 uid 换边（单测钉的是纯函数，这里是真行）
        assert [(s.from_uid, s.from_column, s.to_uid, s.to_column) for s in path.steps] == [
            (t["payment_record"].table_uid, "order_id", t["order_main"].table_uid, "id"),
            (t["order_item"].table_uid, "order_id", t["order_main"].table_uid, "id"),
            (t["order_item"].table_uid, "product_id", t["product"].table_uid, "id"),
        ]
        assert set(result.bridge_uids) == {t["order_main"].table_uid, t["order_item"].table_uid}
        assert result.ambiguous == ()
        assert result.needs_cartesian == ()
        # 桥表没被召回、没有卡片，但渲染路径要写它的名字——名字随图一起回来，不回库查第二趟
        assert result.names[t["order_main"].table_uid] == "ai_web_demo.order_main"
        assert result.names[t["order_item"].table_uid] == "ai_web_demo.order_item"

        # 桥表张数 2 > hops=1：这一对在 1 跳里就是"连不上"，而不是"给一条半个路径"
        tight = await jg.expansion_for(
            session,
            table_uids=[t["payment_record"].table_uid, t["product"].table_uid],
            hops=1,
            max_degree=_MAX_DEGREE,
        )
        assert tight.paths == ()
        assert tight.needs_cartesian == ((t["payment_record"].table_uid, t["product"].table_uid),)


async def test_候选跨两个库时那一对按连不上报(session_factory) -> None:
    """`expansion_for` 的分组那一半：跨库/跨源的表对一律 `needs_cartesian`，不去猜路。

    单测钉不住这一条，因为它要的是"候选属于两个 database_id"这个前提，而 `_uid_groups` 是 IO。
    这也是最容易被写错的一种：两个库各自扩展完就把结果并起来，跨库那一对既不在 paths 也不在
    cartesian 里，pipeline 于是**什么也不说**，模型看见两张毫不相干的表自己硬拼 JOIN。
    """
    async with session_factory() as session:
        actor = await _actor(session)
        src = await _source(session, actor)
        demo = await _table(session, src, "order_item")
        report = await _table(session, src, "orders", schema="reporting")
        # 各自库内都连得上：只有"跨过去"这一对是连不上的
        demo_peer = await _table(session, src, "order_main")
        report_peer = await _table(session, src, "shipments", schema="reporting")
        session.add_all(
            [
                _relation(src, demo, "order_id", demo_peer, "id"),
                _relation(src, report, "shipment_id", report_peer, "id"),
            ]
        )
        await session.flush()

        result = await jg.expansion_for(
            session,
            table_uids=[
                demo.table_uid,
                demo_peer.table_uid,
                report.table_uid,
                report_peer.table_uid,
            ],
            hops=_HOPS,
            max_degree=_MAX_DEGREE,
        )

        assert [p.uids for p in result.paths] == [
            (demo.table_uid, demo_peer.table_uid),
            (report.table_uid, report_peer.table_uid),
        ]
        assert result.needs_cartesian == (
            (demo.table_uid, report.table_uid),
            (demo.table_uid, report_peer.table_uid),
            (demo_peer.table_uid, report.table_uid),
            (demo_peer.table_uid, report_peer.table_uid),
        ), "两个库之间四对候选，一对都不许漏报"


async def test_自环表照常参与跨表扩展(session_factory) -> None:
    """`category.parent_id → category.id` 在真库里存在，它只该丢掉自己那一条边。

    丢掉整张表是不行的：`product.category_id → category.id` 是演示库里的真外键，
    自引用层级表往往同时是别的表的父表。
    """
    async with session_factory() as session:
        actor = await _actor(session)
        src = await _source(session, actor)
        category = await _table(session, src, "category")
        product = await _table(session, src, "product")
        session.add_all(
            [
                _relation(src, category, "parent_id", category, "id"),
                _relation(src, product, "category_id", category, "id"),
            ]
        )
        await session.flush()

        result = await jg.expansion_for(
            session,
            table_uids=[category.table_uid, product.table_uid],
            hops=_HOPS,
            max_degree=_MAX_DEGREE,
        )
        assert [p.uids for p in result.paths] == [(category.table_uid, product.table_uid)]
        # 等值条件仍然按存的方向给（外键侧在前），与遍历从哪一头开始无关
        steps = [(s.from_uid, s.from_column, s.to_uid, s.to_column) for s in result.paths[0].steps]
        assert steps == [(product.table_uid, "category_id", category.table_uid, "id")]


async def test_推断边的_0_8_门槛筛的是真库里的行(session_factory) -> None:
    """1.000 那条（演示库实测，见 `test_sync_live.py` 验收 4）可用，0.700 那条不可用。

    0.700 不是编的档位：007 落库时打的是常数 0.7，那些历史行今天还在表里，这道门槛筛的正是它们。
    """
    async with session_factory() as session:
        actor = await _actor(session)
        src = await _source(session, actor)
        log = await _table(session, src, "user_activity_log")
        product = await _table(session, src, "product")
        customer = await _table(session, src, "customer")
        session.add_all(
            [
                _relation(src, log, "product_id", product, "id", kind="inferred", confidence=1.0),
                _relation(src, log, "customer_id", customer, "id", kind="inferred", confidence=0.7),
            ]
        )
        await session.flush()

        result = await jg.expansion_for(
            session,
            table_uids=[log.table_uid, product.table_uid, customer.table_uid],
            hops=_HOPS,
            max_degree=_MAX_DEGREE,
        )
        assert [(p.uids[0], p.uids[-1]) for p in result.paths] == [
            (log.table_uid, product.table_uid)
        ]
        # 逐对报事实：`product`↔`customer` 之间本来就没有边，它和"被门槛筛掉的"那一格
        # 都是 needs_cartesian——图只说"连不上"，为什么连不上不归它管。
        assert result.needs_cartesian == (
            (log.table_uid, customer.table_uid),
            (product.table_uid, customer.table_uid),
        )


async def test_新落库的边要等同步失效才进图(session_factory) -> None:
    """缓存的**后果**而不是实现：图分不清"同步前有的边"和"刚刚被删掉的边"。

    这里用"插入一条新边"当"同步跑完了"的替身——真同步那一头由 `run_sync` 的终局调用
    `invalidate`（工单 014 接线义务），本用例钉的是"不清就看不见、清了才看得见"。
    """
    async with session_factory() as session:
        actor = await _actor(session)
        src = await _source(session, actor)
        t = await _demo_chain(session, src)
        db = t["payment_record"].database_id

        first = await jg.graph_for(session, datasource_id=src.id, database_id=db)
        assert first.number_of_edges() == 3

        extra = await _table(session, src, "refund_order")
        session.add(_relation(src, extra, "order_id", t["order_main"], "id"))
        await session.flush()

        cached = await jg.graph_for(session, datasource_id=src.id, database_id=db)
        assert cached is first, "同键第二次不该重新建图"
        assert extra.table_uid not in cached

        jg.invalidate(src.id, db)
        fresh = await jg.graph_for(session, datasource_id=src.id, database_id=db)
        assert fresh.number_of_edges() == 4
        assert extra.table_uid in fresh
