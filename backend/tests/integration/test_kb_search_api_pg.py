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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.models  # noqa: F401
from app.models.user import User
from app.services.kb_service import ActiveProfile
from app.services.nl2sql import retriever
from app.services.nl2sql.retriever import RetrievedTable
from tests.integration.conftest import Account
from tests.integration.test_kb_cards_pg import _cards, _column, _data_source, _table

pytestmark = pytest.mark.pg

Login = Callable[..., Awaitable[Account]]


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
        return {"ds_id": src.id, "table_uid": tbl.table_uid}


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


async def test_未登录回401(client: AsyncClient) -> None:
    resp = await client.post("/api/kb/search", json={"query": "订单金额"})
    assert resp.status_code == 401, resp.text
