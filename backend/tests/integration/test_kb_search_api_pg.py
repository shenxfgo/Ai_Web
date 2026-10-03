"""检索预览接口：`POST /api/kb/search`（工单 009 的接口层）。

接缝选在 HTTP 而不是 service，因为这一片真正会错的是**边界**：`datasource_ids` 里塞别人
私有源时该 403 而不是安静少一张、问句长度该有上限（它直接变成 SQL 里的 ILIKE 参数）、
响应里不该出现数据源字段。命中算法本身在 `test_kb_search_pg.py` 那一侧验。

期望值口径：
- `docs/architecture.md` §7 端点表：read 权、body `{query,datasource_ids[],k}` 加 P4 的
  三个检索旋钮（`mode/top_vector/ef_search`，工单 031 接的那一格）
- `docs/architecture.md` §5.2 (5)：权限过滤在召回这一层就做
- 工单 006 那把尺：口令与 `connect_user` 不进响应体
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.models  # noqa: F401
from app.models.datasource import DataSource
from app.models.kb import KbIndexProfile
from app.models.user import User
from app.services import kb_service
from app.services.kb_service import ActiveProfile
from app.services.nl2sql import join_graph, retriever
from app.services.nl2sql.retriever import RetrievedTable
from tests.integration.conftest import Account
from tests.integration.test_join_graph_rows import _relation
from tests.integration.test_kb_cards_pg import _cards, _column, _data_source, _table

pytestmark = pytest.mark.pg

Login = Callable[..., Awaitable[Account]]


@pytest.fixture(autouse=True)
def _empty_join_graph_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """图缓存是模块级全局：不清掉的话，前一条用例建的图会让本文件看不见刚插的边。

    与 `test_join_graph_rows.py` 同一条理由（那里的注释写着"不撞是靠运气，清掉才是靠断言"）。
    `hops_used` 这一族用例插的边就在同一张 `meta_relation` 里，键按 `(datasource_id,
    database_id)` 分，新 `datasource_id` 天然不撞——但每条用例都拿到全新的源，缓存只会越攒越多。
    """
    monkeypatch.setattr(join_graph, "_CACHE", {})


# 换基准那一族用例的第二套档案：维度与本机 `.env`（1536）**不同**，否则"读库"与"读配置"
# 在同一发请求里给的是同一个答案，`used_profile` 是不是从那一行来的就分不出来。
_MODEL_1024 = "bge-large-zh-v1.5"
_DIMENSION_1024 = 1024


async def _seed(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    owner_id: int,
    name: str,
    table: str = "order_main",
    allow_global: bool = False,
) -> dict[str, Any]:
    """一条源 + 一张两列的中文注释表 + 真跑一次卡片落库，把 id/uid 带回来。"""
    async with session_factory() as session:
        src = _data_source(
            owner_id,
            name,
            allow_global=allow_global,
            # 假的哨兵口令：专门用来断言它不会出现在响应体里
            secret="p@ss-not-for-response",
        )
        session.add(src)
        await session.flush()
        tbl = await _table(session, src, table, comment_zh="订单主表")
        session.add_all(
            [
                _column(tbl, 1, "amount", comment_zh="订单金额（元，优惠前）"),
                _column(tbl, 2, "status", comment_zh="订单状态"),
            ]
        )
        await session.flush()
        await _cards(session, src, [tbl.id])
        await session.commit()
        return {"ds_id": src.id, "table_uid": tbl.table_uid, "table_ids": [tbl.id]}


async def _seed_two_tables_with_bridge(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    owner_id: int,
    name: str,
) -> dict[str, Any]:
    """两张能被同一句问句召回的表，中间隔一张**没有卡片**的桥表。

    这是 `hops_used` 唯一能非 0 的形状：召回只认 `kb_card`，所以 `order_item` 进不了候选，
    只能由 014 的图带回来——它同时也就是"聚合这一步"与"图扩展这一步"的分界线，
    `aggregated_tables` 必须把它排除在外（桥表不是聚合的产物）。

    两张表的注释都含 `订单`：`order_main` 靠表注释、`product` 也靠表注释，列注释再各给一次
    `订单金额`，这样两条候选来自同一份召回而不是"一张真命中 + 一张勉强够格"。
    """
    async with session_factory() as session:
        src = _data_source(owner_id, name)
        session.add(src)
        await session.flush()
        main = await _table(session, src, "order_main", comment_zh="订单主表")
        session.add(_column(main, 1, "amount", comment_zh="订单金额（元，优惠前）"))
        product = await _table(session, src, "product", comment_zh="订单商品表")
        session.add(_column(product, 1, "price", comment_zh="订单金额（含税）"))
        bridge = await _table(session, src, "order_item", comment_zh="订单明细行")
        session.add_all(
            [
                _relation(src, bridge, "order_id", main, "id"),
                _relation(src, bridge, "product_id", product, "id"),
            ]
        )
        await session.flush()
        await _cards(session, src, [main.id, product.id])
        await session.commit()
        return {
            "ds_id": src.id,
            "main_uid": main.table_uid,
            "product_uid": product.table_uid,
            "bridge_uid": bridge.table_uid,
        }


async def test_一句问句换来一张候选表并带命中理由(
    active_profile: ActiveProfile,
    client: AsyncClient,
    login: Login,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """预览端点存在的意义就是让人肉验证命中：分数、命中词数/列数、哪个词对上哪个字段都要看得见。"""
    acct = await login(username="searcher", role="member")
    seeded = await _seed(session_factory, owner_id=acct.user_id, name="demo-search")

    resp = await client.post(
        "/api/kb/search",
        json={"query": "订单金额", "datasource_ids": [seeded["ds_id"]], "k": 5},
        headers=acct.headers,
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert [i["table_uid"] for i in items] == [seeded["table_uid"]]
    only = items[0]
    # 问句切成 `订单金额/订单/单金/金额`：amount 的注释全对上，status 只对上 `订单`。
    assert only["matched_term_count"] == 4
    assert only["matched_column_count"] == 2
    assert {(h["column_name"], h["field"]) for h in only["hits"]} == {
        ("amount", "comment_zh"),
        ("status", "comment_zh"),
    }
    assert {"订单金额", "订单"} <= {h["term"] for h in only["hits"]}
    assert only["score_kw"] > 0


async def test_点名的源里没有我的权限时回403(
    active_profile: ActiveProfile,
    client: AsyncClient,
    login: Login,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """预览端点把越权抬成 403，而不是安静少一张。

    它的全部用途就是让人判断"为什么没命中"，把越权降级成空结果等于把这个诊断废掉。
    真检索路径（012 的 `/chat/ask`）反过来——那边按 §5.2 (5) 静默过滤，不能因为一个坏 id
    就整条问数失败。
    """
    owner = await login(username="search-owner", role="member")
    seeded = await _seed(session_factory, owner_id=owner.user_id, name="demo-private")
    stranger = await login(username="search-stranger", role="member")

    resp = await client.post(
        "/api/kb/search",
        json={"query": "订单金额", "datasource_ids": [seeded["ds_id"]], "k": 5},
        headers=stranger.headers,
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "forbidden"


async def test_不点名源时在我看得见的全范围里找_global档算看得见(
    active_profile: ActiveProfile,
    client: AsyncClient,
    login: Login,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """`datasource_ids` 省略 = 我的可见集合，走的是同一把 `access_of` 尺。

    global 档那条是关键：owner 判据写错成"只能搜自己的源"，这一条就会红。
    """
    owner = await login(username="global-search-owner", role="member")
    seeded = await _seed(
        session_factory, owner_id=owner.user_id, name="demo-global-search", allow_global=True
    )
    reader = await login(username="global-reader", role="member")

    resp = await client.post("/api/kb/search", json={"query": "订单金额"}, headers=reader.headers)
    assert resp.status_code == 200, resp.text
    assert [i["table_uid"] for i in resp.json()["items"]] == [seeded["table_uid"]]


async def test_零命中时items是空数组(
    active_profile: ActiveProfile,
    client: AsyncClient,
    login: Login,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """§4.1 的 fail-fast 靠这个形状：空集合是合法答案，不是 404 也不是 500。"""
    acct = await login(username="search-miss", role="member")
    await _seed(session_factory, owner_id=acct.user_id, name="demo-miss")

    resp = await client.post(
        "/api/kb/search", json={"query": "卫星云图降雨量"}, headers=acct.headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["items"] == []


async def test_响应里不含数据源字段与口令(
    active_profile: ActiveProfile,
    client: AsyncClient,
    login: Login,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """检索打的是元数据库，但候选表挂在源上——响应里不能顺手漏出源的任何身份。"""
    acct = await login(username="search-leak", role="member")
    await _seed(session_factory, owner_id=acct.user_id, name="demo-leak")

    resp = await client.post("/api/kb/search", json={"query": "订单金额"}, headers=acct.headers)
    assert resp.status_code == 200, resp.text
    assert "p@ss-not-for-response" not in resp.text
    assert "connect_user" not in resp.text
    assert "aiweb_ro" not in resp.text
    assert "datasource_id" not in resp.text


async def test_问句空或过长回422(
    active_profile: ActiveProfile,
    client: AsyncClient,
    login: Login,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """问句直接变成 SQL 里的 ILIKE 参数，长度必须有上限；空问句则什么都切不出来。"""
    acct = await login(username="search-422", role="member")
    await _seed(session_factory, owner_id=acct.user_id, name="demo-422")

    for bad in ("", "订" * 201):
        resp = await client.post("/api/kb/search", json={"query": bad}, headers=acct.headers)
        assert resp.status_code == 422, resp.text


async def test_P4的检索旋钮收了_没发明的那个仍拒(
    active_profile: ActiveProfile,
    client: AsyncClient,
    login: Login,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """工单 031 之后 `mode / top_vector / ef_search` 收了；`rrf_k` 与 `trgm_threshold` 仍不收。

    三条界线各有各的"收了反而更坏"：
    - `rrf_k` 是**全库融合次序**的常数（§5.2 (3)），进请求体等于让一次调用改写排序规则；
      spec 故事 27 把它钉在 `.env` 里。
    - `trgm_threshold` 同一条理由，且它是 (2b) 那一路的分数门槛（P2 起就只取配置值）。
    - 收的方式是**收下并按它跑**，不是收下就丢：静默忽略会留下最难查的那种"没生效"
      （前端传 `mode=vector`，接口回的还是 ILIKE 的结果，看上去一切正常）。
      按它跑的证据在 `test_kb_search_hybrid_pg.py`（ef/维度/三路模式各有一条），这里只钉契约。
    """
    acct = await login(username="search-knobs", role="member")
    await _seed(session_factory, owner_id=acct.user_id, name="demo-knobs")

    for knob in ({"mode": "keyword"}, {"top_vector": 20}, {"ef_search": 400}):
        resp = await client.post(
            "/api/kb/search", json={"query": "订单金额", **knob}, headers=acct.headers
        )
        assert resp.status_code == 200, f"{knob} → {resp.status_code} {resp.text}"

    for rejected in ({"mode": "semantic"}, {"rrf_k": 10}, {"trgm_threshold": 0.3}):
        resp = await client.post(
            "/api/kb/search", json={"query": "订单金额", **rejected}, headers=acct.headers
        )
        assert resp.status_code == 422, f"{rejected} → {resp.status_code} {resp.text}"


async def test_请求体不给旋钮时结果与给了keyword旋钮逐字相同(
    active_profile: ActiveProfile,
    client: AsyncClient,
    login: Login,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """schema **不带默认值**（已定口径：`top_vector`/`ef_search` 都是 `| None`）。

    roadmap 验收 3 写 `ef_search=64`、§7 的示例写 100——那场冲突的收口方式是"请求体不给就是
    配置说了算"，所以这里不许有第三条答案（schema 里写死的第三个数）。
    本机没配向量端点（`unconfigured` 那一格），缺省模式因此就是 keyword（§5.1 的降级判据），
    于是"不给"与"明说 keyword"必须端出同一份东西。
    """
    acct = await login(username="search-nodefault", role="member")
    await _seed(session_factory, owner_id=acct.user_id, name="demo-nodefault")

    async def _items(payload: dict[str, Any]) -> list[dict[str, Any]]:
        resp = await client.post("/api/kb/search", json=payload, headers=acct.headers)
        assert resp.status_code == 200, resp.text
        return resp.json()["items"]

    plain = await _items({"query": "订单金额"})
    told = await _items(
        {"query": "订单金额", "mode": "keyword", "top_vector": 20, "ef_search": 400}
    )
    assert [i["table_uid"] for i in plain] == [i["table_uid"] for i in told]
    assert plain == told, "向量旋钮在 keyword 路上不该改变任何东西——改了就说明它们被当成了分数参数"


async def test_换实现只改那一处_端点经缝取实现而不是自己new(
    active_profile: ActiveProfile,
    client: AsyncClient,
    login: Login,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """工单 031 验收 5 的"换实现"那半：把缝里换掉一份假实现，端点就照着假的答。

    这一条证的正是 031 整片的风险所在——**装配**。`search_preview()` 里那句
    `_retriever_for(payload)` 是端点侧唯一的取用点；它一旦自己 new 了一份真实现，缝换了它照旧
    返回真命中，而"换实现只改一行"就是一句空话。这种错法只有真 HTTP 打得出来，所以挂在 pg 这侧；
    不碰库的那半（实现字面扫描与构造点计数）住在 `tests/unit/test_retriever_seam.py`。

    两次请求用的是**同一份** payload：换缝之前先跑一次，端出的是真卡片的标题，换完再跑一次端出
    假标题——差别的唯一来源就是被换掉的那一行构造，这排除了"断言本来就空转"。
    """
    acct = await login(username="search-swap", role="member")
    seeded = await _seed(session_factory, owner_id=acct.user_id, name="demo-swap")
    body = {"query": "订单金额", "datasource_ids": [seeded["ds_id"]], "k": 5}

    class StubRetriever:
        """只会说一句话的实现：形状照 `Retriever` 协议，内容全是假的。"""

        async def search(
            self,
            session: AsyncSession,
            *,
            actor: User,
            question: str,
            datasource_ids: Sequence[int] = (),
            k: int | None = None,
        ) -> list[RetrievedTable]:
            return [
                RetrievedTable(
                    table_uid=seeded["table_uid"],
                    table_id=999_999,
                    datasource_id=seeded["ds_id"],
                    title="假实现的答案",
                    score_kw=0.0,
                    matched_term_count=0,
                    matched_column_count=0,
                    hits=(),
                    cards=(),
                )
            ]

    async def _titles() -> list[str]:
        resp = await client.post("/api/kb/search", json=body, headers=acct.headers)
        assert resp.status_code == 200, resp.text
        return [item["title"] for item in resp.json()["items"]]

    real = await _titles()
    # 这里不断言真标题长什么样（卡片标题的渲染格式归 008 的用例管），只要求两件事：
    # 换缝之前端出来的是**真查到的东西**，且它不是假实现那句自报家门。
    assert real, "真实现这一发就该有命中，否则下面的对比是空的"
    assert real != ["假实现的答案"], "换缝之前就出现了假答案——端点没走缝"
    monkeypatch.setattr(retriever, "_retriever_for", lambda payload: StubRetriever())
    assert await _titles() == ["假实现的答案"]


async def test_真链路单路模式下三列名次齐_没跑的那一路是null而不是0(
    active_profile: ActiveProfile,
    client: AsyncClient,
    login: Login,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """工单 032 验收 1 的对外那一半（单路档）：`_item_out` 端出三列名次，NULL 不许长成 0。

    本机没配向量端点（`unconfigured` 那一格），缺省模式因此落在 `keyword`，所以这一发走的是
    **真链路**：`keyword_rank` 由关键词路的计数键次序给定（这里只有一张表 → 第 1 名），
    而向量腿整个没跑 → `vector_rank` / `rrf_score` 都是 `null`。写成 `0` 的话响应里就出现了
    "向量第 0 名"这句谎话，与故事 27 反对的静默降级同一种病（012 那句"留 null 才能把'没命中'
    和'没算'分开"是这条口径的出处）。
    """
    acct = await login(username="search-ranks", role="member")
    seeded = await _seed(session_factory, owner_id=acct.user_id, name="demo-ranks")

    resp = await client.post(
        "/api/kb/search",
        json={"query": "订单金额", "datasource_ids": [seeded["ds_id"]], "k": 5},
        headers=acct.headers,
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert [i["table_uid"] for i in items] == [seeded["table_uid"]]
    assert items[0]["keyword_rank"] == 1
    assert items[0]["vector_rank"] is None
    assert items[0]["rrf_score"] is None
    # 009 交付的字段一个不删（验收 7）：这一发响应的键集合 = 旧的七列 + 新的三列。
    assert set(items[0]) == {
        "table_uid",
        "title",
        "score_kw",
        "matched_term_count",
        "matched_column_count",
        "hits",
        "cards",
        "keyword_rank",
        "vector_rank",
        "rrf_score",
    }


async def test_契约把三列名次原样端出_rrf_score不许被四舍五入(
    active_profile: ActiveProfile,
    client: AsyncClient,
    login: Login,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验收 1 的另一半（两路都非空那一档）钉的是**映射**，所以把料自己摆进缝里。

    真跑 hybrid 要替身 + 摆向量，那一档的证据已经住在 `test_kb_search_hybrid_pg.py`
    （名次由 `_place_by_order` 摆出来、次序手算）；这里要单独钉的是"名次到了响应里还是名次"：
    `score_kw` 那一路有 `round(…, 4)`，融合分被同样的手法抹一遍就会丢掉 1e-5 量级那次差别
    （`test_retriever_rrf.py` 里 t_a 与 t_b 的先后正靠那个量级说话）。
    期望值由本用例自己写死，不经过融合层，因此不是同义反复。
    """
    acct = await login(username="search-rank-out", role="member")
    seeded = await _seed(session_factory, owner_id=acct.user_id, name="demo-rank-out")
    fused_score = 1 / 62 + 1 / 61

    class RankedStub:
        """三份已知形状的名次：两路都命中 / 只在向量 / 只在关键词且融合没跑。"""

        async def search(
            self,
            session: AsyncSession,
            *,
            actor: User,
            question: str,
            datasource_ids: Sequence[int] = (),
            k: int | None = None,
        ) -> list[RetrievedTable]:
            return [
                RetrievedTable(
                    table_uid=seeded["table_uid"],
                    table_id=1,
                    datasource_id=seeded["ds_id"],
                    title="两路都命中",
                    score_kw=1.5,
                    matched_term_count=4,
                    matched_column_count=1,
                    hits=(),
                    cards=(),
                    keyword_rank=2,
                    vector_rank=1,
                    rrf_score=fused_score,
                ),
                RetrievedTable(
                    table_uid="只在向量路",
                    table_id=2,
                    datasource_id=seeded["ds_id"],
                    title="只在向量路",
                    score_kw=0.0,
                    matched_term_count=0,
                    matched_column_count=0,
                    hits=(),
                    cards=(),
                    keyword_rank=None,
                    vector_rank=3,
                    rrf_score=1 / 63,
                ),
            ]

    monkeypatch.setattr(retriever, "_retriever_for", lambda payload: RankedStub())
    resp = await client.post(
        "/api/kb/search", json={"query": "订单金额", "k": 5}, headers=acct.headers
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert [(i["keyword_rank"], i["vector_rank"]) for i in items] == [(2, 1), (None, 3)]
    # 逐位相等，不是 `approx`：这个数在响应里就是拿来比较名次的，抹掉尾数等于抹掉结论。
    assert items[0]["rrf_score"] == fused_score
    assert items[1]["rrf_score"] == 1 / 63


async def test_used_profile端出的是active那一行的三格而不是配置里的字面(
    active_profile: ActiveProfile,
    client: AsyncClient,
    login: Login,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """工单 032 验收 4 的前半：顶层 `used_profile` 第一次有来源（spec 故事 29）。

    §7 那行从 P2 起就写着这一格，而当时"给了就是编的"（向量路没接、档案行也没有对外说过话）。
    031 之后融合次序算出来了但没有地方放；032 落契约的这一格，报的是**库里那一行**：
    id、拼出来的名字、维度。`name` 由 `profile_name()` 现拼而不是读实现给的字面——那三格
    （`model@dimensions@card_template_version`）是 §2.6 的拼法，夹具摆档案行时用的就是它。
    """
    acct = await login(username="search-used", role="member")
    seeded = await _seed(session_factory, owner_id=acct.user_id, name="demo-used")

    resp = await client.post(
        "/api/kb/search",
        json={"query": "订单金额", "datasource_ids": [seeded["ds_id"]], "k": 5},
        headers=acct.headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["used_profile"] == {
        "id": active_profile.index_profile_id,
        "name": kb_service.profile_name(
            active_profile.model,
            dimension=active_profile.dimensions,
            card_template_version=active_profile.card_template_version,
        ),
        "dimensions": active_profile.dimensions,
    }


async def test_库里没有active档案时used_profile是null而不是零值摘要(
    client: AsyncClient,
    login: Login,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """没有那一行时不许编一个 `{id: 0, dimensions: 1536}` 出来
    （029 的 202 只回两颗 id 同一条口径）。

    这条同时钉住既有的降级形状：027 关掉后门之后，库里没有 active 档案 = 空候选而不是 500
    （`_search_scope` 那句"没有生效 profile 和问句没命中在 P2 是同一个答案"）。这里连卡片都不摆，
    为的就是让"没有 active"成为本次请求唯一的事实。
    """
    acct = await login(username="search-noprofile", role="member")
    async with session_factory() as session:
        src = _data_source(acct.user_id, "demo-noprofile")
        session.add(src)
        await session.flush()
        tbl = await _table(session, src, "order_main", comment_zh="订单主表")
        session.add(_column(tbl, 1, "amount", comment_zh="订单金额"))
        await session.commit()

    resp = await client.post("/api/kb/search", json={"query": "订单金额"}, headers=acct.headers)
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["items"] == []
    assert out["used_profile"] is None


async def test_used_profile随激活的档案变_激活1024报1024回滚之后报回1536(
    active_profile: ActiveProfile,
    client: AsyncClient,
    login: Login,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """验收 4 的正身：这一格是"P4 起 `used_profile` 不再是编的"的证据，所以它必须**跟着换基准动**。

    与 030 那条"回滚"用例连起来跑（工单 032 的字面）：同一个会话里先上线 1024 那套 draft、
    再把 1536 那套顶回去，三次请求报的分别是 A / B / A。只看一次激活的话，"实现报的其实是
    settings 那一格"这种错法抓不住——本机 `.env` 的维度正是 1536，A 与服务中的那一行同值，
    读配置与读库在这一发上给的是同一个答案。只有 B 那一发（库里 1024、配置 1536）分得开。

    B 的卡片由 `sync_cards(index_profile_id=B)` 直接建：activate 的第三道前置是"整档案卡片行数齐"
    （metadata-model §2.6 第 4 条），缺它的话这里红在 400 而不是红在响应体。
    """
    acct = await login(username="search-switch", role="admin")
    seeded = await _seed(session_factory, owner_id=acct.user_id, name="demo-switch")

    async def _used() -> dict[str, Any]:
        resp = await client.post(
            "/api/kb/search",
            json={"query": "订单金额", "datasource_ids": [seeded["ds_id"]], "k": 5},
            headers=acct.headers,
        )
        assert resp.status_code == 200, resp.text
        return resp.json()["used_profile"]

    before = await _used()
    assert before["dimensions"] == active_profile.dimensions

    async with session_factory() as session:
        src = await session.get(DataSource, seeded["ds_id"])
        assert src is not None
        second = KbIndexProfile(
            name=kb_service.profile_name(
                _MODEL_1024, dimension=_DIMENSION_1024, card_template_version=1
            ),
            provider="openai_compatible",
            model=_MODEL_1024,
            dimensions=_DIMENSION_1024,
            card_template_version=1,
            status="draft",
            built_at=func.now(),
        )
        session.add(second)
        await session.flush()
        second_id = int(second.id)
        await kb_service.sync_cards(
            session,
            index_profile_id=second_id,
            datasource_id=seeded["ds_id"],
            job_id=None,
            table_ids=seeded["table_ids"],
            dialect_name=src.kind,
            server_version=src.server_version or "",
        )
        await kb_service.activate_profile(session, profile_id=second_id)
        await session.commit()

    switched = await _used()
    assert switched == {
        "id": second_id,
        "name": kb_service.profile_name(
            _MODEL_1024, dimension=_DIMENSION_1024, card_template_version=1
        ),
        "dimensions": _DIMENSION_1024,
    }

    async with session_factory() as session:
        await kb_service.activate_profile(session, profile_id=active_profile.index_profile_id)
        await session.commit()
    # 回滚（spec 故事 6）之后报回的必须是 A 那一行，且它连 id 都和切换前那发一致——
    # 这一格防的是"响应里的 id 是这次请求现算的"，那与"哪套在服务"不是同一句话。
    assert await _used() == before


async def test_hops_used在需要桥表的问句上是1_单表问句上是0(
    active_profile: ActiveProfile,
    client: AsyncClient,
    login: Login,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """工单 032 验收 5 的后半：`hops_used` 的原料是 014 的图，本片只把它端出去（不改尺）。

    口径跟着 014 —— 数的是**桥表张数**不是边数（`settings.retrieval.join_hops` 那条注释），
    所以 `order_main ↔ product` 之间那一条穿一张 `order_item` 的路径报 `1`。
    两次请求放在同一条用例里，是为了让"非 0"与"为 0"只差在图上而不是差在问句、档案或权限上；
    桥表那张源用 `datasource_ids` 收窄，否则两张源会一起进候选，跨源那一对会进
    `needs_cartesian`（图对跨库/跨源一律判"连不上"），`hops_used` 反而被压成 0——
    那抓不住"参数没传下去"这种错法，因为它错得让两个数都是 0。
    """
    acct = await login(username="search-hops", role="member")
    bridged = await _seed_two_tables_with_bridge(
        session_factory, owner_id=acct.user_id, name="demo-hops-bridge"
    )
    single = await _seed(session_factory, owner_id=acct.user_id, name="demo-hops-single")

    async def _top(ds_id: int) -> dict[str, Any]:
        resp = await client.post(
            "/api/kb/search",
            json={"query": "订单金额", "datasource_ids": [ds_id], "k": 5},
            headers=acct.headers,
        )
        assert resp.status_code == 200, resp.text
        return resp.json()

    both = await _top(bridged["ds_id"])
    assert {i["table_uid"] for i in both["items"]} == {bridged["main_uid"], bridged["product_uid"]}
    assert both["hops_used"] == 1, "两张候选之间穿了一张桥表：这一格报的必须是桥表张数"

    only = await _top(single["ds_id"])
    assert [i["table_uid"] for i in only["items"]] == [single["table_uid"]]
    assert only["hops_used"] == 0, "单表问句没有跨表路径：这一格是 0，不是 null 也不是 -1"


async def test_aggregated_tables是聚合到表的那一步_桥表不在里面(
    active_profile: ActiveProfile,
    client: AsyncClient,
    login: Login,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """验收 5 的前半：`aggregated_tables` 的形状与 009 的 `hits`/`cards` 聚合一致。

    "一致"在这里是可核对的那件事：**这一格就是 `items` 那张清单的 uid 序列**，同一份原料、
    同一个次序（§5.2 (4) 的聚合产物），因此它不含桥表——桥表是 (6) 图扩展带回来的人，
    从来没被聚合过，它出现在这里就等于把两步的产物混进一个格子。
    """
    acct = await login(username="search-agg", role="member")
    bridged = await _seed_two_tables_with_bridge(
        session_factory, owner_id=acct.user_id, name="demo-agg"
    )

    resp = await client.post(
        "/api/kb/search",
        json={"query": "订单金额", "datasource_ids": [bridged["ds_id"]], "k": 5},
        headers=acct.headers,
    )
    assert resp.status_code == 200, resp.text
    out = resp.json()
    items = out["items"]
    assert out["aggregated_tables"] == [i["table_uid"] for i in items]
    # 逐字段比对（009 的聚合形状）：每一张被聚合出来的表都带着自己的命中理由与卡片，
    # 而桥表那张 `order_item` 一张卡都没有——它没进过召回，自然也没被聚合过。
    assert all(i["hits"] and i["cards"] for i in items), items
    assert bridged["bridge_uid"] not in out["aggregated_tables"]


async def test_点名不可见源在vector模式下同样是403_那道门在取实现之前(
    active_profile: ActiveProfile,
    client: AsyncClient,
    login: Login,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """工单 032 验收 3 的另一半：403 与"不点名时只在我看得见的源里找"是两件事，各断一条。

    既有的那条 403 用例（`test_点名的源里没有我的权限时回403`）跑的是缺省模式，也就是关键词腿；
    本片把向量路接上之后，多了一种"403 被别的错顶掉"的错法：那道门一旦挪到 `_retriever_for`
    **之后**，本机（`.env` 没配向量端点）点一个不可见源就会先抬 503
    （`resolve_tuning` 对明确给出的 `mode` 不改口，失败留给那一次真调用去说——`EmbeddingClient`
    在未配置时抬 `EmbeddingNotConfigured`），越权这件事反而没人说了：用户看见的是"服务端没配好"，
    而真相是"你想查别人的源"。所以这一条钉的是**顺序**——门在取实现之前，与 `mode` 无关。
    """
    owner = await login(username="vec-owner", role="member")
    seeded = await _seed(session_factory, owner_id=owner.user_id, name="demo-vec-private")
    stranger = await login(username="vec-stranger", role="member")

    resp = await client.post(
        "/api/kb/search",
        json={"query": "订单金额", "datasource_ids": [seeded["ds_id"]], "mode": "vector"},
        headers=stranger.headers,
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "forbidden"


async def test_未登录回401(client: AsyncClient) -> None:
    resp = await client.post("/api/kb/search", json={"query": "订单金额"})
    assert resp.status_code == 401, resp.text
